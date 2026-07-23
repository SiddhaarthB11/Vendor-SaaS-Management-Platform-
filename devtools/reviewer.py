#!/usr/bin/env python3
"""Separate LLM reviewer for autofix — sees diff + failures only, not fixer reasoning."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

REVIEW_RULES = """
You are an independent code reviewer for an autofix pipeline.
You see ONLY: test failures, git diff, and these rules. You do NOT see the fixer's reasoning.

Return STRICT JSON only: {"approve": true|false, "reason": "..."}

Rules:
- Approve only if the diff fixes the root cause, not a symptom.
- The fix must be minimal and scoped to api/ or ui/.
- Reject if the diff deletes or weakens product features to make tests pass.
- Reject if tests were modified unless the failure clearly proves the test was wrong
  (requires explicit justification in the reason field).
- Reject if the diff is unrelated to the reported failures.
- Reject if secrets or .env files are changed.
""".strip()


def _load_gemini_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    env_path = REPO_ROOT / "infra" / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _parse_json_response(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return json.loads(match.group(0))
        raise


def review_fix(
    *,
    failure_report: dict[str, Any],
    git_diff: str,
    model: str | None = None,
) -> dict[str, Any]:
    """
    Review a proposed fix. Uses a different model than the fixer by default (gemini flash).
    Returns {"approve": bool, "reason": str}.
    """
    api_key = _load_gemini_api_key()
    if not api_key:
        return {
            "approve": False,
            "reason": "GEMINI_API_KEY not configured — reviewer cannot run.",
        }

    reviewer_model = model or os.environ.get("AUTOFIX_REVIEWER_MODEL", "gemini-2.0-flash")

    failures = failure_report.get("failures") or []
    failure_text = json.dumps(failures[:40], indent=2)
    if len(failures) > 40:
        failure_text += f"\n... and {len(failures) - 40} more failures"

    prompt = f"""{REVIEW_RULES}

TEST FAILURES:
{failure_text}

GIT DIFF (staged + unstaged):
```diff
{git_diff[:120000]}
```

Respond with JSON only."""

    try:
        from google import genai
        from google.genai import types as gtypes
    except ImportError as exc:
        return {"approve": False, "reason": f"google-genai not installed: {exc}"}

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=reviewer_model,
        contents=prompt,
        config=gtypes.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
        ),
    )
    text = (response.text or "").strip()
    parsed = _parse_json_response(text)
    approve = bool(parsed.get("approve"))
    reason = str(parsed.get("reason") or "").strip() or ("Approved." if approve else "Rejected.")
    return {"approve": approve, "reason": reason, "model": reviewer_model}
