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


# Functions where a diff is structurally risky even if the Reviewer approves
# it: they define classification/routing logic that governs every future row
# or request of a given type, not just the one failing case. A real example
# from this project: an autofix attempt "fixed" a stuck hr-onboarding workflow
# by making resolve_workflow_type() reclassify hr_onboarding_request as
# license_assignment_request going forward — approved by the Reviewer because
# it read as plausible, but it silently changed how EVERY future HR onboarding
# request would be categorized. This is a cheap, non-AI static check — not a
# substitute for reviewer judgment, a second signal surfaced alongside it so
# a human sees the risk before merging even if the Reviewer approved anyway.
SENSITIVE_FUNCTIONS: tuple[str, ...] = (
    "resolve_workflow_type",
    "finance_validation_statuses",
    "requires_line_manager",
    "get_current_approver",
    "WORKFLOW_TYPES",
)

# Variable/branch names that suggest a diff is compensating for an earlier
# change rather than fixing or reverting it (see reviewer.py's matching rule).
COMPENSATING_NAME_HINTS: tuple[str, ...] = ("original_", "legacy_", "real_")


def _sensitive_touch_flags(diff: str) -> list[str]:
    flags: list[str] = []
    for name in SENSITIVE_FUNCTIONS:
        if name in diff:
            flags.append(f"touches sensitive function/constant: {name}")
    for hint in COMPENSATING_NAME_HINTS:
        if hint in diff:
            flags.append(f"introduces a '{hint}*' variable — possible workaround for an earlier change rather than a fix")
    return flags


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

        from devtools.failure_clustering import cluster_failures, task_failure_report
        from devtools.repair_memory import (
            already_tried_diff,
            prior_attempts_context,
            record_attempt,
            signature_for,
        )

        tasks = cluster_failures(report)
        max_tasks = int(os.environ.get("AUTOFIX_MAX_TASKS_PER_CYCLE", "6"))
        tasks = tasks[:max_tasks]
        task_plan = [
            {"task_id": t["task_id"], "suites": t["suites"], "confidence": t["confidence"], "num_failures": len(t["failures"])}
            for t in tasks
        ]
        attempt_row["task_plan"] = task_plan
        attempt_row["tasks"] = []
        _emit(on_progress, {"phase": "plan", "attempt": attempt, "task_plan": task_plan})
        print(f"[autofix] planned {len(tasks)} repair task(s): {[t['task_id'] for t in tasks]}", flush=True)

        any_committed = False
        for task_index, task in enumerate(tasks, 1):
            task_id = task["task_id"]
            task_report = task_failure_report(report, task)
            signatures = sorted({signature_for(f) for f in task["failures"]})
            primary_signature = signatures[0] if signatures else task_id
            safe_task_id = task_id.replace(":", "_").replace("/", "_")

            memory_context = "\n".join(prior_attempts_context(root, sig) for sig in signatures).strip()

            task_prompt = build_fixer_prompt(
                failure_report=task_report,
                git_diff_main=git.diff_vs_base(),
                rejection_reason=memory_context or None,
                attempt=attempt,
            )
            task_prompt += _planted_break_hint(root, task_report)

            _emit(on_progress, {"phase": "fixer", "attempt": attempt, "task": task_id})
            print(f"[autofix] task {task_index}/{len(tasks)} ({task_id}): invoking fixer...", flush=True)
            fixer_ok, fixer_output = invoke_fixer(
                task_prompt,
                repo_root=root,
                failure_report=task_report,
                rejection_reason=memory_context or None,
                attempt=attempt,
            )
            (git.state_dir / f"fixer_output_attempt_{attempt}_{safe_task_id}.txt").write_text(
                fixer_output, encoding="utf-8"
            )
            task_row: dict[str, Any] = {
                "task_id": task_id,
                "suites": task["suites"],
                "confidence": task["confidence"],
                "fixer_ok": fixer_ok,
                "fixer_output": fixer_output[:2000],
            }
            print(fixer_output[:800], flush=True)

            if not git.has_local_changes():
                task_row["outcome"] = "no_change"
                for sig in signatures:
                    record_attempt(root, sig, diff="", outcome="no_change", reason="fixer produced no file changes")
                attempt_row["tasks"].append(task_row)
                continue

            diff = git.diff_staged_and_worktree()
            sensitive_flags = _sensitive_touch_flags(diff)
            if sensitive_flags:
                task_row["sensitive_flags"] = sensitive_flags
                print(f"[autofix] task {task_index}/{len(tasks)} ({task_id}): ⚠ {'; '.join(sensitive_flags)}", flush=True)

            repeat = already_tried_diff(root, primary_signature, diff)
            if repeat:
                task_row["outcome"] = "skipped_repeat"
                task_row["skip_reason"] = f"identical diff already tried (outcome={repeat.get('outcome')}) — not retrying blind"
                git.revert_worktree()
                attempt_row["tasks"].append(task_row)
                print(f"[autofix] task {task_index}/{len(tasks)} ({task_id}): {task_row['skip_reason']}", flush=True)
                continue

            _emit(on_progress, {"phase": "review", "attempt": attempt, "task": task_id})
            print(f"[autofix] task {task_index}/{len(tasks)} ({task_id}): running reviewer...", flush=True)
            from devtools.reviewer import review_fix

            review = review_fix(failure_report=task_report, git_diff=diff)
            (git.state_dir / f"review_attempt_{attempt}_{safe_task_id}.json").write_text(
                json.dumps(review, indent=2), encoding="utf-8"
            )
            task_row["review"] = review
            print(
                f"[autofix] task {task_index}/{len(tasks)} ({task_id}): approve={review.get('approve')} — "
                f"{review.get('reason', '')[:200]}",
                flush=True,
            )

            if not review.get("approve"):
                reason = str(review.get("reason") or "Reviewer rejected the fix.")
                git.revert_worktree()
                task_row["outcome"] = "rejected"
                for sig in signatures:
                    record_attempt(root, sig, diff=diff, outcome="rejected", reason=reason)
                attempt_row["tasks"].append(task_row)
                continue

            committed, commit_err = git.commit_all(f"autofix attempt {attempt} — {task_id}")
            if not committed:
                task_row["outcome"] = "commit_failed"
                task_row["commit_err"] = commit_err
                attempt_row["tasks"].append(task_row)
                continue

            task_row["outcome"] = "committed"
            any_committed = True
            for sig in signatures:
                record_attempt(root, sig, diff=diff, outcome="fixed", reason="approved and committed")
            attempt_row["tasks"].append(task_row)

            reload_wait = int(os.environ.get("AUTOFIX_RELOAD_WAIT_SEC", "4"))
            if reload_wait > 0:
                import time

                print(f"[autofix] waiting {reload_wait}s for API hot reload...", flush=True)
                time.sleep(reload_wait)

        attempt_row["committed"] = any_committed
        attempt_row["fixer_ok"] = any(t.get("fixer_ok") for t in attempt_row["tasks"])
        attempt_row["fixer_output"] = "\n---\n".join(
            f"[{t['task_id']}] {t.get('outcome')}: {(t.get('fixer_output') or '')[:300]}"
            for t in attempt_row["tasks"]
        )[:4000]
        attempts.append(attempt_row)
        _emit(on_progress, {"phase": "attempt_done", "attempt": attempt, "attempts": attempts})

        if not any_committed:
            # Every task this cycle was a no-op, a repeat, or rejected — nothing
            # changed on disk, so re-running judge would just reproduce the same
            # failure report. Stop instead of burning the remaining budget.
            break

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
