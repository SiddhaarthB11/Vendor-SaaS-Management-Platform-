#!/usr/bin/env python3
"""LLM-based fixer — applies file edits when no headless coding CLI is available."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

ALLOWED_PREFIXES = ("api/", "ui/")


def _load_gemini_api_key(repo_root: Path) -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    env_path = repo_root / "infra" / ".env"
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


def _safe_repo_path(repo_root: Path, rel: str) -> Path | None:
    rel = rel.replace("\\", "/").lstrip("/")
    if not any(rel.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        return None
    full = (repo_root / rel).resolve()
    root = repo_root.resolve()
    try:
        full.relative_to(root)
    except ValueError:
        return None
    return full


def _guess_files(failure_report: dict[str, Any], repo_root: Path) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    failed_suites = {str(f.get("suite") or "") for f in failure_report.get("failures") or []}

    def add(rel: str) -> None:
        rel = rel.replace("\\", "/").lstrip("/")
        if rel in seen:
            return
        full = _safe_repo_path(repo_root, rel)
        if full and full.is_file():
            seen.add(rel)
            paths.append(full)

    state_path = repo_root / "devtools" / "state" / "planted_breaks.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            for entry in state.get("breaks") or []:
                if entry.get("suite") in failed_suites and entry.get("path"):
                    add(str(entry["path"]))
        except (json.JSONDecodeError, OSError):
            pass

    planted = failure_report.get("planted_breaks") or {}
    for corr in planted.get("correlations") or []:
        if corr.get("detected") and corr.get("path"):
            add(str(corr["path"]))

    for failure in failure_report.get("failures") or []:
        detail = str(failure.get("detail") or "")
        suite = str(failure.get("suite") or "")
        bid = str(failure.get("planted_break_id") or "")
        if bid and state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                for entry in state.get("breaks") or []:
                    if entry.get("id") == bid and entry.get("path"):
                        add(str(entry["path"]))
            except (json.JSONDecodeError, OSError):
                pass
        for match in re.findall(r"(?:api|ui)/[\w./_-]+\.(?:py|tsx|ts|jsx|js)", detail):
            add(match)
        if suite in {"it-subscription", "renewal", "licence-assignment", "hr-onboarding", "workflow", "offboarding", "full-lifecycle", "master-admin"}:
            add("api/app/workflow_governance.py")
        if "hr-onboarding" in suite or "onboarding" in suite.lower() or suite == "full-lifecycle":
            add("api/app/main.py")
        if suite == "renewal-alerts":
            add("api/app/notifications.py")
        if suite == "upload-export" or "export" in suite:
            add("api/app/main.py")
        if suite == "url-scraping":
            add("api/app/main.py")
        if suite == "operator-eval":
            add("api/app/operator_eval.py")
        if "email" in detail.lower() or "email" in suite.lower():
            add("api/app/main.py")
        if "copilot" in suite:
            add("api/app/agents.py")
            add("api/app/tools.py")
            add("api/app/prompts.py")
        if "operator" in suite:
            add("api/app/agents.py")
            add("api/app/operator_entity_resolver.py")

    for default in (
        "api/app/main.py",
        "api/app/agents.py",
        "api/app/tools.py",
    ):
        add(default)

    return paths[:12]


def _read_file_snippets(repo_root: Path, files: list[Path], *, max_chars: int = 120_000) -> str:
    chunks: list[str] = []
    used = 0
    for path in files:
        rel = path.relative_to(repo_root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if used + len(text) > max_chars:
            text = text[: max(0, max_chars - used)]
        chunks.append(f"--- FILE: {rel} ---\n{text}\n")
        used += len(text)
        if used >= max_chars:
            break
    return "\n".join(chunks)


def apply_llm_fix(
    *,
    failure_report: dict[str, Any],
    repo_root: Path,
    rejection_reason: str | None = None,
    attempt: int = 1,
) -> tuple[bool, str, list[str]]:
    """
    Ask Gemini for minimal file edits and apply them under api/ or ui/.
    Returns (success, message, changed_relative_paths).
    """
    api_key = _load_gemini_api_key(repo_root)
    if not api_key:
        return False, "GEMINI_API_KEY not configured — LLM fixer cannot run.", []

    files = _guess_files(failure_report, repo_root)
    if not files:
        return False, "No candidate source files found for LLM fixer.", []

    file_context = _read_file_snippets(repo_root, files)
    failures = failure_report.get("failures") or []
    rejection_block = ""
    if rejection_reason:
        rejection_block = f"\nPREVIOUS FIX REJECTED:\n{rejection_reason}\n"

    prompt = f"""You are the FIXER in an autonomous test-repair loop for Derisk360 SLMCT.

Fix the ROOT CAUSE minimally in api/ or ui/ source files. Do not weaken features or edit tests unless they are objectively wrong.

Return STRICT JSON only:
{{
  "edits": [
    {{"path": "api/app/example.py", "content": "full new file contents as a string"}}
  ],
  "notes": "one sentence on what you changed"
}}

Rules:
- Include only files you actually change.
- Paths must start with api/ or ui/.
- Provide FULL file content for each edited file (not a diff).
- Do not touch .env, secrets, or devtools/.
{rejection_block}
ATTEMPT: {attempt}

FAILURE SUMMARY:
{json.dumps(failure_report.get("summary") or {}, indent=2)}

FAILURES:
{json.dumps(failures[:40], indent=2)}

SOURCE FILES (read-only context — rewrite only what you change):
{file_context}
"""

    model = os.environ.get("AUTOFIX_FIXER_MODEL", "gemini-2.5-flash")
    try:
        from google import genai
        from google.genai import types as gtypes
    except ImportError as exc:
        return False, f"google-genai not installed: {exc}", []

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=gtypes.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )
    text = (response.text or "").strip()
    try:
        parsed = _parse_json_response(text)
    except json.JSONDecodeError as exc:
        return False, f"LLM fixer returned invalid JSON: {exc}", []

    edits = parsed.get("edits") or []
    if not isinstance(edits, list) or not edits:
        notes = str(parsed.get("notes") or "LLM fixer returned no edits.")
        return False, notes, []

    changed: list[str] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        rel = str(edit.get("path") or "").strip()
        content = edit.get("content")
        if not rel or not isinstance(content, str):
            continue
        target = _safe_repo_path(repo_root, rel)
        if not target:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        changed.append(rel.replace("\\", "/"))

    if not changed:
        return False, str(parsed.get("notes") or "LLM fixer produced no applicable edits."), []

    notes = str(parsed.get("notes") or f"Updated {len(changed)} file(s).")
    return True, notes, changed
