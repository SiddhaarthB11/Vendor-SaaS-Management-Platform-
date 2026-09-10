#!/usr/bin/env python3
"""Plant and restore intentional breaks for judge/autofix demos."""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from devtools.break_catalog import BREAK_CATALOG, BREAKS_BY_ID, BreakSpec, catalog_for_judge

ALLOWED_PREFIXES = ("api/", "ui/")
STATE_FILENAME = "planted_breaks.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(repo_root: Path) -> Path:
    return repo_root / "devtools" / "state" / STATE_FILENAME


def _safe_path(repo_root: Path, rel: str) -> Path | None:
    rel = rel.replace("\\", "/").lstrip("/")
    if not any(rel.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        return None
    full = (repo_root / rel).resolve()
    try:
        full.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return full


def get_planted_breaks(repo_root: Path) -> dict[str, Any] | None:
    path = _state_path(repo_root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def restore_breaks(repo_root: Path) -> dict[str, Any]:
    """Revert all planted breaks. Idempotent."""
    state = get_planted_breaks(repo_root)
    if not state:
        return {"restored": 0, "errors": [], "message": "No active planted breaks."}

    restored = 0
    errors: list[str] = []
    for entry in state.get("breaks") or []:
        rel = str(entry.get("path") or "")
        broken = str(entry.get("broken") or "")
        original = str(entry.get("original") or "")
        target = _safe_path(repo_root, rel)
        if not target or not target.is_file():
            errors.append(f"Missing file: {rel}")
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"Read failed {rel}: {exc}")
            continue
        if broken not in text:
            if original in text:
                restored += 1
            else:
                errors.append(f"Break not found in {rel} (may already be restored manually)")
            continue
        count = text.count(broken)
        text = text.replace(broken, original)
        try:
            target.write_text(text, encoding="utf-8")
            restored += count
        except OSError as exc:
            errors.append(f"Write failed {rel}: {exc}")

    try:
        _state_path(repo_root).unlink(missing_ok=True)
    except OSError as exc:
        errors.append(f"Could not delete state file: {exc}")

    return {
        "restored": restored,
        "errors": errors,
        "message": f"Restored {restored} break(s).",
    }


def _apply_one(repo_root: Path, spec: BreakSpec) -> dict[str, Any] | None:
    target = _safe_path(repo_root, spec["path"])
    if not target or not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None

    old = spec["old"]
    new = spec["new"]
    if new in text and old not in text:
        return None
    if old not in text:
        return None

    if spec.get("replace_all"):
        updated = text.replace(old, new)
        if updated == text:
            return None
    else:
        updated = text.replace(old, new, 1)
        if updated == text:
            return None

    try:
        target.write_text(updated, encoding="utf-8")
    except OSError:
        return None

    return {
        "id": spec["id"],
        "suite": spec["suite"],
        "feature": spec["feature"],
        "path": spec["path"],
        "original": old,
        "broken": new,
    }


def plant_random_breaks(
    repo_root: Path,
    *,
    count: int = 5,
    seed: int | None = None,
    include_slow: bool = False,
) -> dict[str, Any]:
    """
    Plant up to `count` random breaks from the judge-aligned catalog.
    Restores any previously planted breaks first.
    By default only picks suites run in quick judge (excludes copilot-eval).
    """
    repo_root = repo_root.resolve()
    pool = catalog_for_judge(include_slow=include_slow)
    if not pool:
        return {
            "ok": False,
            "planted_count": 0,
            "requested_count": count,
            "breaks": [],
            "skipped": [],
            "message": "No judge-aligned breaks available in catalog.",
        }

    count = max(1, min(int(count), len(pool)))

    prior = get_planted_breaks(repo_root)
    if prior and (prior.get("breaks") or []):
        restore_breaks(repo_root)

    rng = random.Random(seed)
    candidates = list(pool)
    rng.shuffle(candidates)

    planted: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for spec in candidates:
        if len(planted) >= count:
            break
        applied = _apply_one(repo_root, spec)
        if applied:
            planted.append(applied)
        else:
            skipped.append({"id": spec["id"], "suite": spec["suite"], "reason": "string not found or already broken"})

    if not planted:
        return {
            "ok": False,
            "planted_count": 0,
            "requested_count": count,
            "breaks": [],
            "skipped": skipped,
            "message": "Could not plant any breaks — source files may have changed.",
        }

    state = {
        "planted_at": _utc_now(),
        "requested_count": count,
        "breaks": planted,
    }
    state_path = _state_path(repo_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "planted_count": len(planted),
        "requested_count": count,
        "breaks": planted,
        "skipped": skipped,
        "message": (
            f"Planted {len(planted)} intentional break(s). Run Judge — expect failures — then Autofix."
        ),
    }


def _break_still_present(repo_root: Path, b: dict[str, Any]) -> bool:
    """Whether this break's broken marker text is still literally in the file.

    The state file only records what we THOUGHT we planted at plant-time —
    it's never updated afterward. If Autofix fixes the underlying bug via a
    different diff than the exact "restore" pattern (the common case — the
    Fixer rewrites the surrounding logic rather than reverting verbatim),
    this file has no way to know and reports "active" forever, even though
    the break is long gone. Checking the actual file content is the only way
    to know if a break is still real right now.
    """
    target = _safe_path(repo_root, b.get("path") or "")
    if not target or not target.is_file():
        return False
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return False
    broken = b.get("broken") or ""
    return bool(broken) and broken in text


def status(repo_root: Path) -> dict[str, Any]:
    state = get_planted_breaks(repo_root)
    if not state:
        return {"active": False, "count": 0, "breaks": [], "catalog_size": len(catalog_for_judge(include_slow=True)), "quick_catalog_size": len(catalog_for_judge(include_slow=False))}
    breaks = state.get("breaks") or []
    live_breaks = [b for b in breaks if _break_still_present(repo_root, b)]
    resolved_breaks = [b for b in breaks if not _break_still_present(repo_root, b)]
    return {
        "active": bool(live_breaks),
        "planted_at": state.get("planted_at"),
        "count": len(live_breaks),
        "breaks": [
            {
                "id": b.get("id"),
                "suite": b.get("suite"),
                "feature": b.get("feature"),
                "path": b.get("path"),
            }
            for b in live_breaks
        ],
        "resolved_count": len(resolved_breaks),
        "resolved_breaks": [
            {
                "id": b.get("id"),
                "suite": b.get("suite"),
                "feature": b.get("feature"),
                "path": b.get("path"),
            }
            for b in resolved_breaks
        ],
        "catalog_size": len(catalog_for_judge(include_slow=True)),
        "quick_catalog_size": len(catalog_for_judge(include_slow=False)),
    }


if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Plant or restore intentional test breaks.")
    parser.add_argument("--restore", action="store_true", help="Restore all planted breaks")
    parser.add_argument("--count", type=int, default=5, help="Number of breaks to plant")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducible selection")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    if args.restore:
        print(restore_breaks(root))
        sys.exit(0)
    result = plant_random_breaks(root, count=args.count, seed=args.seed)
    print(result.get("message", result))
    sys.exit(0 if result.get("ok") else 1)
