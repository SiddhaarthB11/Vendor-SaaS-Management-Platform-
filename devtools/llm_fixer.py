#!/usr/bin/env python3
"""LLM-based fixer — applies file edits when no headless coding CLI is available."""

from __future__ import annotations

import concurrent.futures
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


# Delimiter-based SEARCH/REPLACE edit format — deliberately NOT JSON, and
# deliberately NOT full-file-content. Two failure modes ruled this out:
#   1. Embedding a full file as a JSON string requires perfect escaping of every
#      quote/backslash/newline in arbitrary source code — reliably breaks.
#   2. Asking for a full rewrite of a large file (main.py is ~9,300 lines) means
#      the response is often truncated by the output token limit before the
#      closing marker is ever written — confirmed live via a saved raw output
#      that started correctly but never reached @@FIXER_EDIT_END@@.
# A search/replace block's output size is proportional to the actual change,
# not the file size, so it can never hit this ceiling on any file.
_EDIT_START = "@@FIXER_EDIT_START@@"
_EDIT_PATH_PREFIX = "PATH:"
_SEARCH_MARK = "@@FIXER_SEARCH@@"
_REPLACE_MARK = "@@FIXER_REPLACE@@"
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


def _strip_one_boundary_newline(s: str) -> str:
    if s.startswith("\n"):
        s = s[1:]
    if s.endswith("\n"):
        s = s[:-1]
    return s


def _parse_edit_blocks(text: str) -> tuple[list[dict[str, str]], str]:
    """Parse the delimiter-based SEARCH/REPLACE fixer output into edits + notes.

    Models occasionally stop generating partway through a trailing block (a
    natural end-of-response quirk, not a token-limit truncation — observed
    live at ~1,200 output tokens, far under any limit). Rather than discard
    the whole response over one incomplete trailing block, keep every block
    that parsed completely and drop only the dangling one; the loop's next
    attempt will pick up whatever that block was trying to fix. Only raise if
    NOT ONE complete block was found.
    """
    text = _strip_markdown_fence(text)
    edits: list[dict[str, str]] = []
    pos = 0
    incomplete_at: int | None = None
    while True:
        start = text.find(_EDIT_START, pos)
        if start == -1:
            break
        path_marker = text.find(_EDIT_PATH_PREFIX, start)
        search_marker = text.find(_SEARCH_MARK, start)
        replace_marker = text.find(_REPLACE_MARK, start)
        end_marker = text.find(_EDIT_END, start)
        if path_marker == -1 or search_marker == -1 or replace_marker == -1 or end_marker == -1:
            incomplete_at = start
            break
        path = text[path_marker + len(_EDIT_PATH_PREFIX):search_marker].strip()
        search_text = _strip_one_boundary_newline(text[search_marker + len(_SEARCH_MARK):replace_marker])
        replace_text = _strip_one_boundary_newline(text[replace_marker + len(_REPLACE_MARK):end_marker])
        edits.append({"path": path, "search": search_text, "replace": replace_text})
        pos = end_marker + len(_EDIT_END)

    if not edits and incomplete_at is not None:
        raise ValueError(f"Malformed edit block starting at offset {incomplete_at} — missing markers.")

    notes = ""
    n_start = text.find(_NOTES_START)
    if n_start != -1:
        n_end = text.find(_NOTES_END, n_start)
        notes = text[n_start + len(_NOTES_START):n_end if n_end != -1 else None].strip()

    if not edits and not notes:
        raise ValueError("No edit blocks or notes found in fixer output — unexpected format.")
    return edits, notes


def _apply_search_replace(repo_root: Path, edits: list[dict[str, str]]) -> tuple[list[str], list[str]]:
    """Apply search/replace edits in place. Returns (changed_paths, problems).

    A search string that doesn't match exactly once is a problem, not a crash —
    it's reported back so the loop can feed it to the next fixer attempt.
    """
    changed: list[str] = []
    problems: list[str] = []
    # Group by path so multiple edits to the same file apply against its
    # current (already-edited-this-round) content, in order.
    by_path: dict[str, list[dict[str, str]]] = {}
    for edit in edits:
        by_path.setdefault(edit["path"], []).append(edit)

    for rel, file_edits in by_path.items():
        target = _safe_repo_path(repo_root, rel)
        if not target or not target.is_file():
            problems.append(f"{rel}: not a valid/existing file under api/ or ui/")
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{rel}: could not read file ({exc})")
            continue

        file_changed = False
        for edit in file_edits:
            search_text = edit["search"]
            replace_text = edit["replace"]
            if not search_text:
                problems.append(f"{rel}: empty SEARCH block, skipped")
                continue
            occurrences = content.count(search_text)
            if occurrences == 0:
                problems.append(f"{rel}: SEARCH text not found (whitespace/content mismatch) — {search_text[:80]!r}")
                continue
            if occurrences > 1:
                problems.append(f"{rel}: SEARCH text matched {occurrences} times, expected exactly 1 — {search_text[:80]!r}")
                continue
            content = content.replace(search_text, replace_text, 1)
            file_changed = True

        if file_changed:
            if target.suffix == ".py":
                import ast

                try:
                    ast.parse(content, filename=rel)
                except SyntaxError as exc:
                    # Reject the whole file's edits rather than write code that won't
                    # import — a plausible-looking SEARCH/REPLACE can still land in
                    # the wrong place (e.g. mid-function) and silently corrupt it.
                    problems.append(f"{rel}: edits would break Python syntax ({exc.msg} at line {exc.lineno}), rejected")
                    continue
            target.write_text(content, encoding="utf-8")
            changed.append(rel)

    return changed, problems


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

Do NOT use JSON and do NOT rewrite whole files. Output each change as a
SEARCH/REPLACE block in EXACTLY this plain-text format (no markdown fences,
no escaping — write the code verbatim between the markers):

{_EDIT_START}
{_EDIT_PATH_PREFIX} api/app/example.py
{_SEARCH_MARK}
<the EXACT existing lines to find — copy them verbatim from SOURCE FILES below,
including original indentation. Keep this block as SHORT as possible while still
being unique in the file (a few lines of tight context around the bug is enough
— do NOT include the whole function or file).>
{_REPLACE_MARK}
<the new lines that should replace the SEARCH block>
{_EDIT_END}

Repeat one block per change (multiple blocks per file are fine — one per
distinct location). After all edit blocks, add:

{_NOTES_START}
one sentence on what you changed
{_NOTES_END}

Rules:
- The SEARCH block must match EXACTLY ONE location in the file, verbatim
  (exact whitespace/indentation) — it will be rejected otherwise.
- Keep SEARCH blocks small and targeted at the actual bug, not whole functions.
- Paths must start with api/ or ui/.
- Do not touch .env, secrets, or devtools/.
- Do not wrap output in markdown code fences.
- Every edit must trace directly to one of the entries in FAILURES below. If you
  see other things you'd like to improve, leave them alone — this loop grades
  you strictly on making the listed failures pass, and unrelated changes only
  add risk of new breakage with no credit.
- Do NOT add new functions, endpoints, parameters, or fields that nothing in
  FAILURES asked for. A missing check almost always means a small, existing
  conditional or value is wrong — not that a new code path needs to be built.
- Never insert a REPLACE block in the middle of another function's body. If
  you're inserting a new top-level statement (e.g. a new route), your SEARCH
  block must be the line or blank line directly BEFORE or AFTER an existing
  function definition, never a line from inside one.
- Prefer the smallest attempt that could plausibly work: one or two SEARCH/REPLACE
  blocks fixing the specific broken condition, value, or comparison named in
  the failure detail. Only touch more than one file if the failures clearly
  span multiple files.
{rejection_block}
ATTEMPT: {attempt}

FAILURE SUMMARY:
{json.dumps(failure_report.get("summary") or {}, indent=2)}

FAILURES:
{json.dumps(failures[:40], indent=2)}

SOURCE FILES (read-only context — find the exact text to match from here):
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
        # Hard timeout at the thread level — the SDK's own httpx client timeout
        # is not reliably honored on every code path, so a stalled call must be
        # abandoned regardless of what the SDK does. IMPORTANT: do NOT use
        # ThreadPoolExecutor as a context manager here — `with` calls
        # shutdown(wait=True) on exit, which blocks until the worker thread
        # finishes even after future.result(timeout=...) has already raised
        # TimeoutError, silently defeating the timeout. Create it directly and
        # never call shutdown(wait=True) on a thread we've given up on.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(client.models.generate_content, model=candidate, contents=prompt, config=config)
            response = future.result(timeout=150)
            text = (response.text or "").strip()
            pool.shutdown(wait=False)
            if text:
                break
        except concurrent.futures.TimeoutError:
            pool.shutdown(wait=False)  # abandon the stuck worker thread, don't wait on it
            last_error = TimeoutError(f"{candidate} did not respond within 150s")
            continue
        except Exception as exc:
            pool.shutdown(wait=False)
            last_error = exc
            continue
    if not text:
        return False, f"LLM fixer got no response from any model ({models_to_try}): {last_error}", []

    try:
        edits, notes = _parse_edit_blocks(text)
    except ValueError as exc:
        # Dump the raw model output so a malformed-response failure is
        # diagnosable instead of just "missing markers" with no context.
        debug_path = repo_root / "devtools" / "state" / f"fixer_raw_output_attempt_{attempt}.txt"
        try:
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(text, encoding="utf-8")
        except OSError:
            pass
        return False, f"LLM fixer returned unparseable output: {exc} (raw output saved to {debug_path.name})", []

    if not edits:
        return False, notes or "LLM fixer returned no edits.", []

    changed, problems = _apply_search_replace(repo_root, edits)

    if not changed:
        detail = "; ".join(problems) if problems else "LLM fixer produced no applicable edits."
        return False, detail, []
    if problems:
        # Partial success — some edits applied, some didn't match. Report both
        # so the reviewer/next attempt sees the full picture.
        notes = f"{notes or f'Updated {len(changed)} file(s).'} (unapplied: {'; '.join(problems)})"

    return True, notes or f"Updated {len(changed)} file(s).", changed
