"""Master admin devtools control panel — background jobs for judge/autofix/watch."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from psycopg import Connection
from psycopg.types.json import Json

from app.devtools_autofix import autofix_capabilities, resolve_repo_root, run_panel_autofix
from app.devtools_judge import run_judge
from app.diagnostics_cleanup import cleanup_all_diagnostic_artifacts
from app.settings import get_settings

REPO_ROOT = resolve_repo_root()
DEFAULT_BASE_URL = os.environ.get("DEVTOOLS_BASE_URL", "http://127.0.0.1:8000")
STALE_QUEUED_SEC = 120
STALE_RUNNING_SEC = 5400

_lock = threading.Lock()
_watch_stop = threading.Event()
_watch_thread: threading.Thread | None = None
_watch_state: dict[str, Any] = {
    "enabled": False,
    "interval_sec": 300,
    "last_run_job_id": None,
    "last_autofix_job_id": None,
    "autofix_on_failure": True,
}


def _db_conn():
    import psycopg
    from psycopg.rows import dict_row

    from app.settings import get_settings

    return psycopg.connect(get_settings().database_url, row_factory=dict_row)


def _expire_stale_jobs(conn: Connection) -> int:
    """Mark orphaned queued/running jobs failed (e.g. after API container restart)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE slmct.devtools_jobs
            SET status = 'failed',
                error = COALESCE(
                    error,
                    'Job timed out or was orphaned (API restarted). Safe to retry.'
                ),
                finished_at = COALESCE(finished_at, now()),
                updated_at = now()
            WHERE status = 'queued'
              AND created_at < now() - make_interval(secs => %s)
            """,
            (STALE_QUEUED_SEC,),
        )
        queued = cur.rowcount
        cur.execute(
            """
            UPDATE slmct.devtools_jobs
            SET status = 'failed',
                error = COALESCE(
                    error,
                    'Job exceeded maximum runtime. Safe to retry.'
                ),
                finished_at = COALESCE(finished_at, now()),
                updated_at = now()
            WHERE status = 'running'
              AND updated_at < now() - make_interval(secs => %s)
            """,
            (STALE_RUNNING_SEC,),
        )
        running = cur.rowcount
    conn.commit()
    return queued + running


def cancel_job(conn: Connection, job_id: UUID, *, actor_roles: list[str] | None) -> dict[str, Any]:
    _require_master(actor_roles)
    _expire_stale_jobs(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT pid FROM slmct.devtools_jobs WHERE id = %s", (job_id,))
        pid_row = cur.fetchone()
        cur.execute(
            """
            UPDATE slmct.devtools_jobs
            SET status = 'cancelled',
                error = COALESCE(error, 'Cancelled by master admin.'),
                finished_at = now(),
                updated_at = now()
            WHERE id = %s AND status IN ('queued', 'running')
            RETURNING id
            """,
            (job_id,),
        )
        row = cur.fetchone()
    conn.commit()
    if row and pid_row:
        _kill_job_process(pid_row.get("pid"))
    if not row:
        job = get_job(conn, job_id)
        if not job:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is already {job.get('status')} and cannot be cancelled.",
        )
    return get_job(conn, job_id) or {"id": str(job_id), "status": "cancelled"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_master(actor_roles: list[str] | None) -> None:
    roles = {str(r).strip().lower() for r in (actor_roles or [])}
    if "master_admin" not in roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="master_admin role required")


def _row_to_job(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "status": row["status"],
        "params": row.get("params") or {},
        "report": row.get("report"),
        "error": row.get("error"),
        "actor_user_id": str(row["actor_user_id"]) if row.get("actor_user_id") else None,
        "actor_email": row.get("actor_email"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "finished_at": row["finished_at"].isoformat() if row.get("finished_at") else None,
    }


def _insert_job(
    conn: Connection,
    *,
    kind: str,
    params: dict[str, Any],
    actor_user_id: str | None,
    actor_email: str | None,
) -> UUID:
    job_id = uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO slmct.devtools_jobs (
                id, kind, status, params, actor_user_id, actor_email
            )
            VALUES (%s, %s, 'queued', %s, %s, %s)
            """,
            (job_id, kind, Json(params), actor_user_id, actor_email),
        )
    conn.commit()
    return job_id


def _update_job(
    conn: Connection,
    job_id: UUID,
    *,
    status: str | None = None,
    report: dict[str, Any] | None = None,
    error: str | None = None,
    finished: bool = False,
) -> None:
    sets = ["updated_at = now()"]
    params: list[Any] = []
    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if report is not None:
        sets.append("report = %s")
        params.append(Json(report))
    if error is not None:
        sets.append("error = %s")
        params.append(error)
    if finished:
        sets.append("finished_at = now()")
    params.append(job_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE slmct.devtools_jobs SET {', '.join(sets)} WHERE id = %s",
            params,
        )
    conn.commit()


def get_job(conn: Connection, job_id: UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM slmct.devtools_jobs WHERE id = %s", (job_id,))
        return _row_to_job(cur.fetchone())


def list_jobs(conn: Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM slmct.devtools_jobs
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [_row_to_job(row) for row in cur.fetchall() if row]


def get_latest_job(conn: Connection, kind: str | None = None) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        if kind:
            cur.execute(
                """
                SELECT * FROM slmct.devtools_jobs
                WHERE kind = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (kind,),
            )
        else:
            cur.execute(
                """
                SELECT * FROM slmct.devtools_jobs
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        return _row_to_job(cur.fetchone())


def _run_job_worker(job_id: UUID, kind: str, params: dict[str, Any]) -> None:
    conn = _db_conn()
    try:
        _update_job(conn, job_id, status="running")
        if kind == "judge":
            report = run_judge(
                base_url=params.get("base_url") or DEFAULT_BASE_URL,
                suite_name=params.get("suite_name"),
                quick=bool(params.get("quick", True)),
                include_slow=bool(params.get("full", False)),
                operator_skip_llm=not bool(params.get("operator_llm")),
            )
            cleanup = cleanup_all_diagnostic_artifacts(conn)
            report["cleanup"] = cleanup
            overall = report.get("overall")
            _update_job(
                conn,
                job_id,
                status="completed" if overall == "pass" else "failed",
                report=report,
                finished=True,
            )
        elif kind == "autofix":
            report = run_panel_autofix(conn, job_id, params, update_job=_update_job)
            overall = report.get("overall")
            if report.get("error") and overall != "pass":
                _update_job(
                    conn,
                    job_id,
                    status="failed",
                    report=report,
                    error=str(report.get("error"))[:2000],
                    finished=True,
                )
            else:
                _update_job(
                    conn,
                    job_id,
                    status="completed" if overall == "pass" else "failed",
                    report=report,
                    finished=True,
                )
            return
        else:
            _update_job(conn, job_id, status="failed", error=f"Unknown job kind {kind}", finished=True)
    except Exception as exc:
        try:
            _update_job(conn, job_id, status="failed", error=str(exc)[:2000], finished=True)
        except Exception:
            pass
    finally:
        conn.close()


def start_job(
    conn: Connection,
    *,
    kind: str,
    params: dict[str, Any],
    actor_roles: list[str] | None,
    actor_user_id: str | None,
    actor_email: str | None,
) -> dict[str, Any]:
    _require_master(actor_roles)
    _expire_stale_jobs(conn)

    with _lock:
        running = get_latest_job(conn, kind)
        if running and running.get("status") in {"queued", "running"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A {kind} job is already running (id={running['id']}). Cancel it or wait for completion.",
            )

    job_id = _insert_job(
        conn,
        kind=kind,
        params=params,
        actor_user_id=actor_user_id,
        actor_email=actor_email,
    )
    if kind == "autofix":
        # Autofix must survive uvicorn's --reload restarts: the Fixer edits files
        # under the watched --reload-dir, which makes WatchFiles kill the worker
        # process mid-run. A background thread dies with it silently (job hangs
        # forever at "running"). A detached subprocess is immune to that restart
        # and writes its own progress straight to the devtools_jobs row.
        pid = _spawn_autofix_subprocess(job_id, params)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE slmct.devtools_jobs SET pid = %s WHERE id = %s",
                (pid, job_id),
            )
        conn.commit()
    else:
        thread = threading.Thread(
            target=_run_job_worker,
            args=(job_id, kind, params),
            daemon=True,
            name=f"devtools-{kind}-{job_id}",
        )
        thread.start()
    job = get_job(conn, job_id)
    return job or {"id": str(job_id), "kind": kind, "status": "queued"}


def _spawn_autofix_subprocess(job_id: UUID, params: dict[str, Any]) -> int:
    import json
    import subprocess
    import sys

    kwargs: dict[str, Any] = {
        "cwd": str(REPO_ROOT),
        "env": os.environ.copy(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    # Fully detach from this process's group so a uvicorn --reload restart
    # (which only signals its own server child) can never take this down with it.
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "devtools.autofix_subprocess_runner",
            str(job_id),
            json.dumps(params),
        ],
        **kwargs,
    )
    return proc.pid


def _kill_job_process(pid: int | None) -> None:
    """Best-effort termination of a detached autofix subprocess.

    Same detachment that protects the subprocess from --reload restarts
    (start_new_session / CREATE_NEW_PROCESS_GROUP) means a plain os.kill on
    the PID only signals that one process, not a child tree, which is fine
    here since autofix_subprocess_runner does the work itself rather than
    forking further children.
    """
    if not pid:
        return
    import signal

    try:
        if os.name == "posix":
            os.kill(pid, signal.SIGTERM)
        else:
            import subprocess

            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], check=False, capture_output=True)
    except (ProcessLookupError, OSError):
        pass


def get_watch_status() -> dict[str, Any]:
    with _lock:
        return dict(_watch_state)


def _watch_loop(interval_sec: int, params: dict[str, Any]) -> None:
    # NOTE: deliberately NOT app.db.get_connection() here. That's a generator
    # (`yield conn; finally: conn.close()`) meant for FastAPI's dependency
    # injection, which keeps a reference to the generator object for the
    # whole request and closes it at the right time. `next(get_connection())`
    # only keeps the yielded connection, not the generator itself — with
    # nothing referencing it, it can be garbage-collected immediately, which
    # runs its `finally: conn.close()` right away and closes the connection
    # before first use. Confirmed live: every tick failed with "the
    # connection is closed" the instant it ran. _db_conn() is a plain
    # function returning a connection directly — no generator, no gotcha.
    print(f"[watch] loop thread starting, interval={interval_sec}s params={params}", flush=True)
    autofix_enabled = bool(params.get("autofix_on_failure", True))
    autofix_max_attempts = int(params.get("autofix_max_attempts", 5))

    while not _watch_stop.is_set():
        print("[watch] tick", flush=True)
        conn = _db_conn()
        try:
            # Never overlap with an autofix already in flight (manually
            # started, or from a previous watch tick) — its own internal
            # judge/fixer/reviewer loop already re-verifies as it goes, so a
            # fresh top-level judge run here would just race it.
            running_autofix = get_latest_job(conn, "autofix")
            if running_autofix and running_autofix.get("status") in {"queued", "running"}:
                if _watch_stop.wait(interval_sec):
                    break
                continue

            job_id = _insert_job(
                conn,
                kind="judge",
                params={**params, "watch_triggered": True},
                actor_user_id=None,
                actor_email="watch@system",
            )
            with _lock:
                _watch_state["last_run_job_id"] = str(job_id)
            _run_job_worker(job_id, "judge", {**params, "watch_triggered": True})

            if autofix_enabled:
                judge_job = get_job(conn, job_id)
                judge_report = (judge_job or {}).get("report") or {}
                if judge_report.get("overall") == "fail":
                    autofix_params = {
                        "quick": bool(params.get("quick", True)),
                        "full": bool(params.get("full", False)),
                        "max_attempts": autofix_max_attempts,
                        "operator_llm": False,
                        "base_url": params.get("base_url") or DEFAULT_BASE_URL,
                    }
                    autofix_job_id = _insert_job(
                        conn,
                        kind="autofix",
                        params=autofix_params,
                        actor_user_id=None,
                        actor_email="watch@system",
                    )
                    with _lock:
                        _watch_state["last_autofix_job_id"] = str(autofix_job_id)
                    autofix_pid = _spawn_autofix_subprocess(autofix_job_id, autofix_params)
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE slmct.devtools_jobs SET pid = %s WHERE id = %s",
                            (autofix_pid, autofix_job_id),
                        )
                    conn.commit()
        except Exception as exc:
            import traceback

            print(f"[watch] tick failed: {exc!r}", flush=True)
            traceback.print_exc()
        finally:
            conn.close()
        if _watch_stop.wait(interval_sec):
            break


def set_watch(
    conn: Connection,
    *,
    enabled: bool,
    interval_sec: int,
    params: dict[str, Any],
    actor_roles: list[str] | None,
) -> dict[str, Any]:
    global _watch_thread
    _require_master(actor_roles)

    with _lock:
        if enabled:
            _watch_stop.clear()
            _watch_state.update(
                {
                    "enabled": True,
                    "interval_sec": max(60, interval_sec),
                    "params": params,
                    "autofix_on_failure": bool(params.get("autofix_on_failure", True)),
                }
            )
            if _watch_thread is None or not _watch_thread.is_alive():
                _watch_thread = threading.Thread(
                    target=_watch_loop,
                    args=(_watch_state["interval_sec"], params),
                    daemon=True,
                    name="devtools-watch",
                )
                _watch_thread.start()
        else:
            _watch_stop.set()
            _watch_state["enabled"] = False

    _persist_watch_state(conn, enabled=enabled, interval_sec=_watch_state["interval_sec"], params=params)
    return get_watch_status()


def _persist_watch_state(conn: Connection, *, enabled: bool, interval_sec: int, params: dict[str, Any]) -> None:
    """Save watch on/off state to Postgres so it survives a uvicorn --reload
    restart — see devtools_watch_state migration for why this matters: the
    file changes Autofix itself makes are exactly what triggers a reload."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO slmct.devtools_watch_state (id, enabled, interval_sec, params, updated_at)
                VALUES (1, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    interval_sec = EXCLUDED.interval_sec,
                    params = EXCLUDED.params,
                    updated_at = now()
                """,
                (enabled, interval_sec, Json(params)),
            )
        conn.commit()
    except Exception:
        conn.rollback()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if os.name == "posix":
            os.kill(pid, 0)
        else:
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, check=False
            ).stdout
            return str(pid) in out
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists, just owned by someone else — still alive.
        return True
    except OSError:
        return False


def reap_orphaned_jobs_at_startup() -> int:
    """Call once at API startup, before resume_watch_from_db().

    judge jobs run as an in-process thread — no restart of any kind (worker
    reload or full container recreate) leaves that thread alive, so any judge
    row still 'queued'/'running' at startup is unconditionally orphaned.

    autofix jobs are deliberately different: they run as a *detached*
    subprocess (start_new_session / CREATE_NEW_PROCESS_GROUP) specifically so
    they survive a uvicorn --reload worker restart — that's the whole reason
    it isn't a thread like judge. Blanket-reaping every 'running' autofix row
    on every startup would defeat that: a routine reload (the fixer editing
    its own watched files) would falsely kill a still-working subprocess.
    So autofix rows are only reaped if their recorded pid is actually dead —
    true after a full container restart/recreate, false after a worker reload.

    Without this, an orphaned 'running' autofix row (pid genuinely dead)
    permanently jams watch's own overlap guard — get_latest_job(conn,
    "autofix") sees it as still in flight forever, so the self-heal loop
    silently no-ops on every tick until the 5400s stale-job timeout clears
    it. Confirmed live: a container restart mid-autofix left the loop dead
    for 35+ minutes with no visible error, just silent no-op ticks.
    """
    print("[startup] reap_orphaned_jobs_at_startup() called", flush=True)
    conn = _db_conn()
    reaped_ids: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, kind, pid FROM slmct.devtools_jobs WHERE status IN ('queued', 'running')"
            )
            rows = cur.fetchall()
            for row in rows:
                if row["kind"] == "autofix" and _pid_alive(row.get("pid")):
                    print(f"[startup] autofix job {row['id']} pid={row['pid']} still alive, not reaping", flush=True)
                    continue
                cur.execute(
                    """
                    UPDATE slmct.devtools_jobs
                    SET status = 'failed',
                        error = COALESCE(error, 'Orphaned: process restarted while this job was in flight.'),
                        finished_at = COALESCE(finished_at, now()),
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (row["id"],),
                )
                reaped_ids.append(str(row["id"]))
        conn.commit()
        if reaped_ids:
            print(f"[startup] reaped {len(reaped_ids)} orphaned job(s): {reaped_ids}", flush=True)
        return len(reaped_ids)
    except Exception as exc:
        print(f"[startup] reap_orphaned_jobs_at_startup failed: {exc!r}", flush=True)
        return 0
    finally:
        conn.close()


def resume_watch_from_db() -> None:
    """Call once at API startup. If watch was left enabled before the last
    restart (very possibly caused by Autofix's own edits triggering a
    reload), resume it automatically instead of silently staying off until
    a human notices and re-enables it by hand."""
    global _watch_thread
    print("[watch] resume_watch_from_db() called", flush=True)
    try:
        conn = _db_conn()
    except Exception as exc:
        print(f"[watch] resume: could not connect to db: {exc!r}", flush=True)
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT enabled, interval_sec, params FROM slmct.devtools_watch_state WHERE id = 1")
            row = cur.fetchone()
    except Exception as exc:
        print(f"[watch] resume: query failed: {exc!r}", flush=True)
        row = None
    finally:
        conn.close()

    print(f"[watch] resume: row={row}", flush=True)
    if not row or not row.get("enabled"):
        print("[watch] resume: not enabled in db, staying off", flush=True)
        return

    interval_sec = max(60, int(row.get("interval_sec") or 300))
    params = row.get("params") or {}
    with _lock:
        _watch_stop.clear()
        _watch_state.update(
            {
                "enabled": True,
                "interval_sec": interval_sec,
                "params": params,
                "autofix_on_failure": bool(params.get("autofix_on_failure", True)),
            }
        )
        if _watch_thread is None or not _watch_thread.is_alive():
            print("[watch] resume: spawning watch thread", flush=True)
            _watch_thread = threading.Thread(
                target=_watch_loop,
                args=(interval_sec, params),
                daemon=True,
                name="devtools-watch",
            )
            _watch_thread.start()
        else:
            print("[watch] resume: thread already alive, not spawning a new one", flush=True)


def merge_branch(conn: Connection, *, branch: str, actor_roles: list[str] | None) -> dict[str, Any]:
    """Merge an autofix branch into main. Master-admin only — this is the
    deliberate human-approval action; it is never triggered automatically."""
    _require_master(actor_roles)
    from app.devtools_autofix import merge_autofix_branch

    return merge_autofix_branch(branch)


def panel_status(conn: Connection) -> dict[str, Any]:
    _expire_stale_jobs(conn)
    latest_judge = get_latest_job(conn, "judge")
    latest_autofix = get_latest_job(conn, "autofix")
    settings = get_settings()
    caps = autofix_capabilities()
    from app.devtools_breaks import breaks_status

    break_state = breaks_status()
    return {
        "watch": get_watch_status(),
        "latest_judge": latest_judge,
        "latest_autofix": latest_autofix,
        "capabilities": {
            "gemini_configured": bool(settings.gemini_api_key),
            "autofix_ready": caps["ready"],
            "autofix": caps,
            "repo_root": caps["repo_root"],
        },
        "planted_breaks": break_state,
        "suites": __import__("app.devtools_judge", fromlist=["list_suites"]).list_suites(include_slow=True),
    }


def compute_autofix_metrics(conn: Connection, *, limit: int = 50) -> dict[str, Any]:
    """Aggregate outcomes across recent autofix runs from job history already
    sitting in Postgres — no new instrumentation, just reading what
    run_panel_autofix already records in devtools_jobs.report.

    This exists so architecture changes can be evaluated with a number
    instead of a vibe: e.g. "did tightening the clustering heuristic actually
    raise the per-task repair rate, or just move the same outcomes around."
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, report, created_at, finished_at
            FROM slmct.devtools_jobs
            WHERE kind = 'autofix' AND report IS NOT NULL
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    runs_total = len(rows)
    runs_pass = 0
    task_outcomes: dict[str, int] = {}
    attempts_per_run: list[int] = []
    tasks_per_run: list[int] = []
    run_summaries: list[dict[str, Any]] = []

    for row in rows:
        report = row.get("report") or {}
        overall = report.get("overall")
        if overall == "pass":
            runs_pass += 1
        attempts = report.get("attempts") or []
        attempts_per_run.append(len(attempts))

        run_task_count = 0
        for attempt in attempts:
            tasks = attempt.get("tasks")
            if tasks is None:
                # Pre-redesign job shape (one bundled attempt, no per-task list) —
                # count it as a single legacy task so old runs aren't dropped
                # from the attempts/committed-rate stats, just excluded from
                # the per-task outcome breakdown.
                run_task_count += 1
                continue
            for task in tasks:
                outcome = str(task.get("outcome") or "unknown")
                task_outcomes[outcome] = task_outcomes.get(outcome, 0) + 1
                run_task_count += 1
        tasks_per_run.append(run_task_count)

        run_summaries.append(
            {
                "id": str(row["id"]),
                "status": row.get("status"),
                "overall": overall,
                "attempts": len(attempts),
                "tasks": run_task_count,
                "branch": report.get("branch"),
                "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            }
        )

    committed = task_outcomes.get("committed", 0)
    rejected = task_outcomes.get("rejected", 0)
    reviewed = committed + rejected
    total_tasks = sum(task_outcomes.values())

    return {
        "runs_analyzed": runs_total,
        "runs_passed": runs_pass,
        "run_pass_rate": round(runs_pass / runs_total, 3) if runs_total else None,
        "avg_attempts_per_run": round(sum(attempts_per_run) / len(attempts_per_run), 2) if attempts_per_run else None,
        "avg_tasks_per_run": round(sum(tasks_per_run) / len(tasks_per_run), 2) if tasks_per_run else None,
        "task_outcomes": task_outcomes,
        "task_repair_rate": round(committed / total_tasks, 3) if total_tasks else None,
        "reviewer_approval_rate": round(committed / reviewed, 3) if reviewed else None,
        "recent_runs": run_summaries,
    }
