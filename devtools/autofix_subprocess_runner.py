#!/usr/bin/env python3
"""Out-of-process entrypoint for the autofix loop.

Why this exists: the API container runs uvicorn with --reload (so Judge picks up
the Fixer's edits without a manual rebuild). WatchFiles restarts the uvicorn
worker process the instant a watched file changes on disk — which is exactly
what the Fixer does every attempt. If the autofix loop runs as a background
thread *inside* that worker process (as Judge jobs safely do, since Judge never
edits files), the restart kills the thread mid-run with no error surfaced: the
job just hangs at status="running" forever.

Running the loop as a separate OS process sidesteps this entirely — WatchFiles
only signals the uvicorn server child it manages, not an unrelated subprocess.
This process opens its own DB connection and writes progress straight to the
devtools_jobs row, independent of whatever uvicorn worker happens to be alive.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import UUID


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 2:
        print("usage: autofix_subprocess_runner.py <job_id> <params_json>", file=sys.stderr)
        return 2

    job_id = UUID(argv[0])
    params = json.loads(argv[1])

    repo_root = Path(os.environ.get("DEVTOOLS_REPO_ROOT", "/app")).resolve()
    root_str = str(repo_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    api_dir = str(repo_root / "api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)

    import psycopg
    from psycopg.rows import dict_row

    from app.devtools_autofix import run_panel_autofix
    from app.devtools_panel import _update_job
    from app.settings import get_settings

    conn = psycopg.connect(get_settings().database_url, row_factory=dict_row)
    try:
        _update_job(conn, job_id, status="running")
        try:
            report = run_panel_autofix(conn, job_id, params, update_job=_update_job)
        except Exception as exc:  # noqa: BLE001 - surface any crash as a failed job, never hang
            _update_job(conn, job_id, status="failed", error=str(exc)[:2000], finished=True)
            return 1

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
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
