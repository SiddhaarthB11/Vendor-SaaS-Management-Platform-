"""Run the full autofix loop from the master admin control panel."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg import Connection

DEFAULT_BASE_URL = os.environ.get("DEVTOOLS_BASE_URL", "http://127.0.0.1:8000")


def resolve_repo_root() -> Path:
    env = os.environ.get("DEVTOOLS_REPO_ROOT", "").strip()
    if env:
        return Path(env).resolve()
    # repo/api/app/devtools_autofix.py -> repo
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "devtools" / "autofix.py").is_file():
        return candidate
    docker_root = Path("/app")
    if (docker_root / "devtools" / "autofix.py").is_file():
        return docker_root
    return candidate


def autofix_capabilities(repo_root: Path | None = None) -> dict[str, Any]:
    root = (repo_root or resolve_repo_root()).resolve()
    autofix_script = root / "devtools" / "autofix.py"
    git_ok = shutil.which("git") is not None
    gemini = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    fixer_mode = os.environ.get("AUTOFIX_FIXER_MODE", "auto").strip().lower() or "auto"
    writable_api = (root / "api" / "app").is_dir() and os.access(root / "api" / "app", os.W_OK)
    ready = autofix_script.is_file() and git_ok and gemini and writable_api
    return {
        "ready": ready,
        "repo_root": str(root),
        "devtools_present": autofix_script.is_file(),
        "git_available": git_ok,
        "gemini_configured": gemini,
        "api_writable": writable_api,
        "fixer_mode": fixer_mode,
    }


def _import_autofix(repo_root: Path):
    os.environ["DEVTOOLS_REPO_ROOT"] = str(repo_root)
    root_str = str(repo_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    from devtools.autofix import run_autofix_loop_report

    return run_autofix_loop_report


def run_panel_autofix(
    conn: Connection,
    job_id: UUID,
    params: dict[str, Any],
    *,
    update_job,
) -> dict[str, Any]:
    repo_root = resolve_repo_root()
    caps = autofix_capabilities(repo_root)
    if not caps["ready"]:
        missing = []
        if not caps["devtools_present"]:
            missing.append("devtools/ not available (mount repo into API container)")
        if not caps["git_available"]:
            missing.append("git not installed")
        if not caps["gemini_configured"]:
            missing.append("GEMINI_API_KEY not set")
        if not caps["api_writable"]:
            missing.append("api/ source not writable (mount ../api into container)")
        return {
            "mode": "autofix",
            "overall": "fail",
            "error": "Autofix prerequisites missing: " + "; ".join(missing),
            "capabilities": caps,
        }

    run_autofix_loop_report = _import_autofix(repo_root)
    base_url = params.get("base_url") or DEFAULT_BASE_URL
    quick = bool(params.get("quick", True))
    full = bool(params.get("full", False))
    max_attempts = int(params.get("max_attempts", 5))

    report_state: dict[str, Any] = {
        "mode": "autofix",
        "overall": "running",
        "phase": "starting",
        "attempts": [],
        "capabilities": caps,
    }

    def on_progress(event: dict[str, Any]) -> None:
        phase = event.get("phase")
        if phase:
            report_state["phase"] = phase
        if event.get("branch"):
            report_state["branch"] = event["branch"]
        if event.get("attempt") is not None:
            report_state["current_attempt"] = event["attempt"]
        if event.get("max_attempts") is not None:
            report_state["max_attempts"] = event["max_attempts"]
        if event.get("attempts") is not None:
            report_state["attempts"] = event["attempts"]
        if phase == "judge_done":
            report_state["last_judge_overall"] = event.get("overall")
            report_state["last_judge_summary"] = event.get("summary")
        update_job(conn, job_id, report=dict(report_state))

    try:
        result = run_autofix_loop_report(
            max_attempts=max_attempts,
            base_url=base_url,
            quick_judge=quick and not full,
            on_progress=on_progress,
            repo_root=repo_root,
        )
    except Exception as exc:
        result = {
            "mode": "autofix",
            "overall": "fail",
            "error": str(exc),
            "attempts": report_state.get("attempts") or [],
            "phase": report_state.get("phase") or "error",
        }
    finally:
        from app.diagnostics_cleanup import cleanup_all_diagnostic_artifacts

        cleanup = cleanup_all_diagnostic_artifacts(conn)
        report_state["cleanup"] = cleanup
        update_job(conn, job_id, report=dict(report_state))

    result["capabilities"] = caps
    if "cleanup" in report_state:
        result["cleanup"] = report_state["cleanup"]
    return result
