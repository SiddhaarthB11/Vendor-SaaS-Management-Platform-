#!/usr/bin/env python3
"""Invoke the headless fixer agent with failure context."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = REPO_ROOT / "devtools" / "state"

FIXER_INSTRUCTIONS = """
You are the FIXER agent in an autonomous test-repair loop for Derisk360 SLMCT.

Goal: make diagnostic suites pass by fixing the ROOT CAUSE in api/ or ui/.

Constraints:
- Fix minimally. Do not refactor unrelated code.
- Never delete or weaken a feature just to make a test pass.
- Do not modify diagnostic tests unless they are objectively wrong (rare).
- Do not touch .env, secrets, or devtools/state/.
- The API may hot-reload — edit files under api/app/ or ui/app/ directly.

Workflow:
1. Read the failure JSON below.
2. Inspect relevant source (main.py monolith, agents.py, tools.py, email_templates.py, ui components).
3. Apply the smallest correct fix.
4. Save files. Do not run judge yourself unless asked.
""".strip()


def build_fixer_prompt(
    *,
    failure_report: dict[str, Any],
    git_diff_main: str,
    rejection_reason: str | None = None,
    attempt: int = 1,
) -> str:
    failures = failure_report.get("failures") or []
    failure_json = json_dumps(failures[:50])
    extra = ""
    if rejection_reason:
        extra = f"\n\nPREVIOUS FIX REJECTED BY REVIEWER:\n{rejection_reason}\n"

    return f"""{FIXER_INSTRUCTIONS}
{extra}
ATTEMPT: {attempt}

JUDGE FAILURE REPORT (summary):
{json_dumps(failure_report.get('summary') or {})}

FAILURES (suite / check / detail):
{failure_json}

GIT DIFF vs main (context — do not revert unrelated work):
```diff
{git_diff_main[:80000]}
```

Fix the failures now.
"""


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, indent=2)


def _split_cmd(value: str) -> list[str]:
    if sys.platform == "win32":
        return value.split()
    return shlex.split(value)


def _fixer_mode() -> str:
    mode = os.environ.get("AUTOFIX_FIXER_MODE", "auto").strip().lower()
    if mode in {"cli", "llm", "auto"}:
        return mode
    return "auto"


def invoke_fixer(
    prompt: str,
    *,
    repo_root: Path = REPO_ROOT,
    failure_report: dict[str, Any] | None = None,
    rejection_reason: str | None = None,
    attempt: int = 1,
) -> tuple[bool, str]:
    """
    Run the configured fixer (CLI and/or LLM). Returns (success, combined_output).
    Set AUTOFIX_FIXER_CMD to override CLI; AUTOFIX_FIXER_MODE=cli|llm|auto (default auto).
    """
    mode = _fixer_mode()
    if mode == "llm":
        return _invoke_llm_fixer(
            failure_report=failure_report or {},
            repo_root=repo_root,
            rejection_reason=rejection_reason,
            attempt=attempt,
        )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    prompt_path = STATE_DIR / "fixer_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    cmd_env = os.environ.get("AUTOFIX_FIXER_CMD", "").strip()
    if cmd_env:
        cmd = _split_cmd(cmd_env)
    else:
        # Try Cursor agent CLI, then Claude Code, then fallback message.
        for candidate in (
            ["cursor", "agent", "--print"],
            ["cursor", "agent"],
            ["claude", "-p"],
        ):
            try:
                subprocess.run(
                    [candidate[0], "--help"],
                    capture_output=True,
                    timeout=5,
                    cwd=repo_root,
                )
                cmd = candidate
                break
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                continue
        else:
            if mode == "auto" and failure_report is not None:
                return _invoke_llm_fixer(
                    failure_report=failure_report,
                    repo_root=repo_root,
                    rejection_reason=rejection_reason,
                    attempt=attempt,
                )
            return (
                False,
                "No fixer CLI found. Install Cursor CLI, set AUTOFIX_FIXER_CMD, "
                f"or use AUTOFIX_FIXER_MODE=llm (prompt saved to {prompt_path}).",
            )

    # Pass prompt via stdin for CLIs that support it; also write file path in env.
    env = os.environ.copy()
    env["AUTOFIX_PROMPT_FILE"] = str(prompt_path)

    try:
        completed = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=repo_root,
            env=env,
            timeout=int(os.environ.get("AUTOFIX_FIXER_TIMEOUT", "600")),
        )
    except FileNotFoundError:
        return False, f"Fixer command not found: {' '.join(cmd)}"
    except subprocess.TimeoutExpired:
        return False, f"Fixer timed out after {os.environ.get('AUTOFIX_FIXER_TIMEOUT', '600')}s"

    output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
    ok = completed.returncode == 0
    if not ok and not output.strip():
        output = f"Fixer exited with code {completed.returncode}"
    if not ok and mode == "auto" and failure_report is not None:
        llm_ok, llm_out = _invoke_llm_fixer(
            failure_report=failure_report,
            repo_root=repo_root,
            rejection_reason=rejection_reason,
            attempt=attempt,
        )
        return llm_ok, f"{output}\n\n[LLM fixer fallback]\n{llm_out}"
    return ok, output.strip()


def _invoke_llm_fixer(
    *,
    failure_report: dict[str, Any],
    repo_root: Path,
    rejection_reason: str | None,
    attempt: int,
) -> tuple[bool, str]:
    from devtools.llm_fixer import apply_llm_fix

    ok, message, changed = apply_llm_fix(
        failure_report=failure_report,
        repo_root=repo_root,
        rejection_reason=rejection_reason,
        attempt=attempt,
    )
    if changed:
        message = f"{message}\nChanged: {', '.join(changed)}"
    return ok, message
