#!/usr/bin/env python3
"""Autofix loop: judge → fixer → reviewer → commit → repeat."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from devtools.git_helpers import (
    commit_changes,
    default_diff_base,
    ensure_baseline_commit,
    ensure_git_repo,
    git_available,
    has_changes,
    has_commits,
    normalize_main_branch,
    run_git,
    stage_autofix_changes,
)


def _default_repo_root() -> Path:
    env = os.environ.get("DEVTOOLS_REPO_ROOT", "").strip()
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[1]


class GitRepo:
    """Repo-scoped git operations for a single autofix run."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.state_dir = self.root / "devtools" / "state"

    def run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run_git(self.root, args, check=check)

    def ensure_ready(self) -> tuple[bool, str]:
        if not git_available():
            return False, "git is not installed"
        ensure_git_repo(self.root)
        return ensure_baseline_commit(self.root)

    def current_branch(self) -> str:
        return self.run(["branch", "--show-current"], check=False).stdout.strip() or "main"

    def diff_base(self) -> str:
        return default_diff_base(self.root)

    def diff_vs_base(self) -> str:
        base = self.diff_base()
        return self.run(["diff", base], check=False).stdout or ""

    def diff_staged_and_worktree(self) -> str:
        staged = self.run(["diff", "--cached"], check=False).stdout or ""
        worktree = self.run(["diff"], check=False).stdout or ""
        return f"{staged}\n{worktree}".strip()

    def has_local_changes(self) -> bool:
        return has_changes(self.root)

    def commit_all(self, message: str) -> tuple[bool, str]:
        return commit_changes(self.root, message)

    def create_autofix_branch(self) -> str:
        normalize_main_branch(self.root)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        branch = f"autofix/{ts}"
        self.run(["checkout", "-b", branch])
        return branch

    def revert_worktree(self) -> None:
        self.run(["checkout", "--", "."], check=False)
        self.run(["clean", "-fd", "--exclude=devtools/state/*"], check=False)


def _emit(on_progress: Callable[[dict[str, Any]], None] | None, event: dict[str, Any]) -> None:
    if on_progress:
        on_progress(event)


def _planted_break_hint(repo_root: Path, failure_report: dict[str, Any]) -> str:
    """If demo breaks are active, tell the fixer to restore known originals."""
    state_path = repo_root / "devtools" / "state" / "planted_breaks.json"
    if not state_path.is_file():
        return ""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    if not state.get("breaks"):
        return ""

    lines = [
        "\n\nACTIVE PLANTED DEMO BREAKS (restore these exact originals in the listed files):\n"
    ]
    failed_suites = {f.get("suite") for f in failure_report.get("failures") or []}
    for entry in state.get("breaks") or []:
        if entry.get("suite") not in failed_suites:
            continue
        lines.append(f"- {entry.get('id')} ({entry.get('suite')}): {entry.get('path')}")
        lines.append(f"  REPLACE: {repr(entry.get('broken', '')[:120])}")
        lines.append(f"  WITH:    {repr(entry.get('original', '')[:120])}")
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


def run_autofix_loop_report(
    *,
    max_attempts: int = 5,
    base_url: str = "http://localhost:8000",
    quick_judge: bool = True,
    dry_run: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Full judge → fixer → reviewer → commit loop. Returns structured report for the control panel."""
    root = (repo_root or _default_repo_root()).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from devtools.fix_runner import build_fixer_prompt, invoke_fixer
    from devtools.judge import run_judge

    git = GitRepo(root)
    attempts: list[dict[str, Any]] = []

    git_ok, git_err = git.ensure_ready()
    if not git_ok:
        return {
            "mode": "autofix",
            "overall": "fail",
            "error": f"Git setup failed: {git_err}",
            "attempts": [],
            "git_hint": "Autofix needs a baseline commit. Ensure /app is a git repo with api/ and devtools/ mounted.",
        }

    original_branch = git.current_branch()
    try:
        branch = git.create_autofix_branch()
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc)).strip()
        return {
            "mode": "autofix",
            "overall": "fail",
            "error": f"Could not create autofix branch: {err}",
            "attempts": [],
        }

    _emit(on_progress, {"phase": "branch", "branch": branch, "from": original_branch})

    rejection_reason: str | None = None

    for attempt in range(1, max_attempts + 1):
        report_path = git.state_dir / f"judge_report_attempt_{attempt}.json"
        _emit(on_progress, {"phase": "judge", "attempt": attempt, "max_attempts": max_attempts})
        print(f"\n[autofix] attempt {attempt}/{max_attempts} — running judge...", flush=True)
        report = run_judge(
            base_url=base_url,
            quick=quick_judge,
            output_path=report_path,
        )
        attempt_row: dict[str, Any] = {
            "attempt": attempt,
            "judge_overall": report.get("overall"),
            "summary": report.get("summary"),
            "branch": branch,
        }
        _emit(
            on_progress,
            {
                "phase": "judge_done",
                "attempt": attempt,
                "overall": report.get("overall"),
                "summary": report.get("summary"),
                "attempts": attempts + [attempt_row],
            },
        )

        if report.get("overall") == "pass":
            attempts.append(attempt_row)
            diff_stat = git.run(["diff", git.diff_base(), "--stat"], check=False)
            result = {
                "mode": "autofix",
                "overall": "pass",
                "branch": branch,
                "attempts": attempts,
                "judge": report,
                "diff_stat": (diff_stat.stdout or "").strip(),
            }
            _request_cleanup_sweep(base_url)
            return result

        if dry_run:
            attempts.append(attempt_row)
            return {
                "mode": "autofix",
                "overall": "fail",
                "branch": branch,
                "attempts": attempts,
                "judge": report,
                "dry_run": True,
            }

        prompt = build_fixer_prompt(
            failure_report=report,
            git_diff_main=git.diff_vs_base(),
            rejection_reason=rejection_reason,
            attempt=attempt,
        )
        prompt += _planted_break_hint(root, report)

        _emit(on_progress, {"phase": "fixer", "attempt": attempt})
        print("[autofix] invoking fixer...", flush=True)
        fixer_ok, fixer_output = invoke_fixer(
            prompt,
            repo_root=root,
            failure_report=report,
            rejection_reason=rejection_reason,
            attempt=attempt,
        )
        (git.state_dir / f"fixer_output_attempt_{attempt}.txt").write_text(fixer_output, encoding="utf-8")
        attempt_row["fixer_ok"] = fixer_ok
        attempt_row["fixer_output"] = fixer_output[:4000]
        print(fixer_output[:2000], flush=True)

        if not git.has_local_changes():
            attempt_row["stopped"] = "fixer produced no file changes"
            attempts.append(attempt_row)
            _emit(on_progress, {"phase": "fixer_done", "attempt": attempt, "changed": False, "attempts": attempts})
            break

        diff = git.diff_staged_and_worktree()
        _emit(on_progress, {"phase": "review", "attempt": attempt})
        print("[autofix] running reviewer...", flush=True)
        from devtools.reviewer import review_fix

        review = review_fix(failure_report=report, git_diff=diff)
        (git.state_dir / f"review_attempt_{attempt}.json").write_text(
            json.dumps(review, indent=2),
            encoding="utf-8",
        )
        attempt_row["review"] = review
        print(
            f"[autofix] reviewer: approve={review.get('approve')} — {review.get('reason', '')[:300]}",
            flush=True,
        )

        if not review.get("approve"):
            rejection_reason = str(review.get("reason") or "Reviewer rejected the fix.")
            git.revert_worktree()
            attempt_row["reverted"] = True
            attempts.append(attempt_row)
            _emit(on_progress, {"phase": "review_rejected", "attempt": attempt, "attempts": attempts})
            print("[autofix] reverted unapproved changes; retrying fixer with rejection reason", flush=True)
            continue

        _emit(on_progress, {"phase": "commit", "attempt": attempt})
        committed, commit_err = git.commit_all(f"autofix attempt {attempt}")
        if not committed:
            attempt_row["stopped"] = commit_err or "commit failed"
            attempts.append(attempt_row)
            _emit(on_progress, {"phase": "commit_failed", "attempt": attempt, "attempts": attempts})
            break

        rejection_reason = None
        attempt_row["committed"] = True
        attempts.append(attempt_row)
        _emit(on_progress, {"phase": "attempt_done", "attempt": attempt, "attempts": attempts})
        reload_wait = int(os.environ.get("AUTOFIX_RELOAD_WAIT_SEC", "4"))
        if reload_wait > 0:
            import time

            print(f"[autofix] waiting {reload_wait}s for API hot reload...", flush=True)
            time.sleep(reload_wait)

    final_judge = run_judge(base_url=base_url, quick=quick_judge)
    overall = final_judge.get("overall") or "fail"
    result = {
        "mode": "autofix",
        "overall": overall,
        "branch": branch,
        "attempts": attempts,
        "judge": final_judge,
        "note": f"Branch `{branch}` left for inspection. Merge when satisfied.",
    }
    _request_cleanup_sweep(base_url)
    return result


def _request_cleanup_sweep(base_url: str) -> None:
    try:
        import httpx

        httpx.post(f"{base_url.rstrip('/')}/api/diagnostics/cleanup-all", timeout=60.0)
    except Exception as exc:
        print(f"[autofix] post-run cleanup request failed: {exc}", flush=True)


def run_autofix_loop(
    *,
    max_attempts: int = 5,
    base_url: str = "http://localhost:8000",
    quick_judge: bool = True,
    dry_run: bool = False,
    repo_root: Path | None = None,
) -> int:
    report = run_autofix_loop_report(
        max_attempts=max_attempts,
        base_url=base_url,
        quick_judge=quick_judge,
        dry_run=dry_run,
        repo_root=repo_root,
    )
    if report.get("overall") == "pass":
        print("\n[autofix] judge GREEN", flush=True)
        if report.get("diff_stat"):
            print(report["diff_stat"], flush=True)
        print(f"\nBranch `{report.get('branch')}` is ready for human review/merge.", flush=True)
        return 0
    if report.get("error"):
        print(f"[autofix] {report['error']}", flush=True)
        return 2
    print(
        f"\n[autofix] finished overall={report.get('overall')}. Branch `{report.get('branch')}`.",
        flush=True,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autonomous test-fix loop (judge → fixer → reviewer).")
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--full-judge", action="store_true", help="Include copilot-eval (slow).")
    parser.add_argument("--dry-run", action="store_true", help="Run judge once; do not invoke fixer.")
    args = parser.parse_args(argv)

    return run_autofix_loop(
        max_attempts=args.max_attempts,
        base_url=args.base_url.rstrip("/"),
        quick_judge=not args.full_judge,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
