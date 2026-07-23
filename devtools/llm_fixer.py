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


# Delimiter-based edit format — deliberately NOT JSON. Embedding full source files
# (which may be 300KB+, e.g. main.py) as a JSON string value requires the model to
# perfectly escape every quote/backslash/newline in that content; on large files
# this reliably breaks. Plain-text markers carry raw file content verbatim with no
# escaping step, so there is nothing for the model to get wrong here.
_EDIT_START = "@@FIXER_EDIT_START@@"
_EDIT_PATH_PREFIX = "PATH:"
_EDIT_CONTENT_MARK = "@@FIXER_CONTENT@@"
_EDIT_END = "@@FIXER_EDIT_END@@"
_NOTES_START = "@@FIXER_NOTES@@"
_NOTES_END = "@@FIXER_NOTES_END@@"


def _strip_markdown_fence(text: str) -> str:
    """Models sometimes wrap the whole answer in a ``` fence despite instructions
    not to. Strip a single outer fence if the entire response is wrapped in one."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return text


def _parse_edit_blocks(text: str) -> tuple[list[dict[str, str]], str]:
    """Parse the delimiter-based fixer output into edits + notes.

    Raises ValueError with a clear message if the expected markers are missing —
    callers should treat that the same as a JSON parse failure (retry/report).
    """
    text = _strip_markdown_fence(text)
    edits: list[dict[str, str]] = []
    pos = 0
    while True:
        start = text.find(_EDIT_START, pos)
        if start == -1:
            break
        path_marker = text.find(_EDIT_PATH_PREFIX, start)
        content_marker = text.find(_EDIT_CONTENT_MARK, start)
        end_marker = text.find(_EDIT_END, start)
        if path_marker == -1 or content_marker == -1 or end_marker == -1:
            raise ValueError(f"Malformed edit block starting at offset {start} — missing markers.")
        path = text[path_marker + len(_EDIT_PATH_PREFIX):content_marker].strip()
        content = text[content_marker + len(_EDIT_CONTENT_MARK):end_marker]
        # Strip exactly one leading/trailing newline the model conventionally adds
        # around the content block, without touching intentional blank lines.
        if content.startswith("\n"):
            content = content[1:]
        if content.endswith("\n"):
            content = content[:-1]
        edits.append({"path": path, "content": content})
        pos = end_marker + len(_EDIT_END)

    notes = ""
    n_start = text.find(_NOTES_START)
    if n_start != -1:
        n_end = text.find(_NOTES_END, n_start)
        notes = text[n_start + len(_NOTES_START):n_end if n_end != -1 else None].strip()

    if not edits and not notes:
        raise ValueError("No edit blocks or notes found in fixer output — unexpected format.")
    return edits, notes


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

Do NOT use JSON. Output each changed file as a block in EXACTLY this plain-text
format (no markdown fences, no extra escaping — write the raw file content
verbatim between the markers):

{_EDIT_START}
{_EDIT_PATH_PREFIX} api/app/example.py
{_EDIT_CONTENT_MARK}
<full new file content goes here, verbatim, unescaped>
{_EDIT_END}

Repeat one block per changed file. After all edit blocks, add:

{_NOTES_START}
one sentence on what you changed
{_NOTES_END}

Rules:
- Include only files you actually change.
- Paths must start with api/ or ui/.
- Provide FULL file content for each edited file (not a diff).
- Do not touch .env, secrets, or devtools/.
- Do not wrap file content in markdown code fences.
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
        import httpx
        from google import genai
        from google.genai import types as gtypes
    except ImportError as exc:
        return False, f"google-genai not installed: {exc}", []

    # Explicit timeout — a stalled network call must not hang the whole job.
    client = genai.Client(api_key=api_key, http_options={"httpx_client": httpx.Client(timeout=120.0)})
    config = gtypes.GenerateContentConfig(temperature=0.1, max_output_tokens=32768)

    fallback_models = ["gemini-2.5-flash", "gemini-2.5-pro"]
    models_to_try = [model] + [m for m in fallback_models if m != model]

    text = ""
    last_error: Exception | None = None
    for candidate in models_to_try:
        try:
            response = client.models.generate_content(model=candidate, contents=prompt, config=config)
            text = (response.text or "").strip()
            if text:
                break
        except Exception as exc:
            last_error = exc
            continue
    if not text:
        return False, f"LLM fixer got no response from any model ({models_to_try}): {last_error}", []

    try:
        edits, notes = _parse_edit_blocks(text)
    except ValueError as exc:
        return False, f"LLM fixer returned unparseable output: {exc}", []

    if not edits:
        return False, notes or "LLM fixer returned no edits.", []

    changed: list[str] = []
    for edit in edits:
        rel = edit.get("path", "").strip()
        content = edit.get("content")
        if not rel or content is None:
            continue
        target = _safe_repo_path(repo_root, rel)
        if not target:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        changed.append(rel.replace("\\", "/"))

    if not changed:
        return False, notes or "LLM fixer produced no applicable edits.", []

    return True, notes or f"Updated {len(changed)} file(s).", changed
