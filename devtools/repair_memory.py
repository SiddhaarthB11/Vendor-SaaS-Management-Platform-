"""Persistent record of repair attempts, keyed by failure signature.

Why this exists: without it, every autofix cycle re-derives everything from
scratch. If a fix gets rejected, the next cycle has no way to know "this
exact approach was already tried and rejected for this reason" — it can only
see one attempt's worth of prose (rejection_reason) that describes a whole
bundled diff, not a specific failure. This gives the loop a persistent,
per-failure history so it can avoid repeating an identical rejected diff and
can tell the fixer explicitly what's already been tried.

Storage is a single JSON file under devtools/state/ — deliberately not a
database. This is local, single-repo, single-process history; a file is
sufficient and keeps the dependency list at zero.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_MEMORY_FILENAME = "repair_memory.json"
_MAX_ATTEMPTS_PER_SIGNATURE = 20


def signature_for(failure: dict[str, Any]) -> str:
    """Stable id for 'this specific failing check' — the unit repair memory tracks.

    Suite + check name is the right granularity: it's stable across runs (the
    detail text often includes volatile values like counts or UUIDs), and it's
    the same granularity failures are clustered into repair tasks at.
    """
    suite = str(failure.get("suite") or "unknown-suite")
    check = str(failure.get("check") or "unknown-check")
    return f"{suite}::{check}"


def _diff_hash(diff: str) -> str:
    return hashlib.sha256(diff.strip().encode("utf-8")).hexdigest()[:16]


def _memory_path(repo_root: Path) -> Path:
    return repo_root / "devtools" / "state" / _MEMORY_FILENAME


def load_memory(repo_root: Path) -> dict[str, Any]:
    path = _memory_path(repo_root)
    if not path.is_file():
        return {"signatures": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"signatures": {}}
    if "signatures" not in data:
        data["signatures"] = {}
    return data


def save_memory(repo_root: Path, memory: dict[str, Any]) -> None:
    path = _memory_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(memory, indent=2), encoding="utf-8")


def get_history(repo_root: Path, signature: str) -> list[dict[str, Any]]:
    memory = load_memory(repo_root)
    return memory.get("signatures", {}).get(signature, {}).get("attempts", [])


def already_tried_diff(repo_root: Path, signature: str, diff: str) -> dict[str, Any] | None:
    """If this exact diff was already tried for this failure and rejected/regressed,
    return that prior attempt record so the caller can skip regenerating it blindly."""
    target_hash = _diff_hash(diff)
    for attempt in get_history(repo_root, signature):
        if attempt.get("diff_hash") == target_hash and attempt.get("outcome") != "fixed":
            return attempt
    return None


def record_attempt(
    repo_root: Path,
    signature: str,
    *,
    diff: str,
    outcome: str,
    reason: str = "",
) -> None:
    """outcome: 'fixed' | 'rejected' | 'regressed' | 'no_change'"""
    memory = load_memory(repo_root)
    sigs = memory.setdefault("signatures", {})
    entry = sigs.setdefault(signature, {"attempts": []})
    entry["attempts"].append(
        {
            "diff_hash": _diff_hash(diff),
            "diff_preview": diff[:800],
            "outcome": outcome,
            "reason": reason[:500],
        }
    )
    entry["attempts"] = entry["attempts"][-_MAX_ATTEMPTS_PER_SIGNATURE:]
    save_memory(repo_root, memory)


def prior_attempts_context(repo_root: Path, signature: str) -> str:
    """Human-readable block for the fixer prompt: what's already been tried and
    failed for this exact failure, so it doesn't repeat itself blind."""
    history = [a for a in get_history(repo_root, signature) if a.get("outcome") != "fixed"]
    if not history:
        return ""
    lines = [f"\nPRIOR ATTEMPTS FOR THIS FAILURE ({signature}) — do not repeat these:"]
    for i, attempt in enumerate(history[-5:], 1):
        lines.append(f"  {i}. outcome={attempt.get('outcome')} — {attempt.get('reason') or 'no reason recorded'}")
    return "\n".join(lines)
