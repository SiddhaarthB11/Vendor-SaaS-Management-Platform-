"""Plant / restore intentional breaks for judge and autofix demos."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from app.devtools_autofix import resolve_repo_root


def _import_plant_breaks():
    root = resolve_repo_root()
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    from devtools.plant_breaks import plant_random_breaks, restore_breaks, status

    return plant_random_breaks, restore_breaks, status, root


def _require_master(actor_roles: list[str] | None) -> None:
    roles = {str(r).strip().lower() for r in (actor_roles or [])}
    if "master_admin" not in roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="master_admin role required")


def plant_breaks(
    *,
    count: int = 5,
    seed: int | None = None,
    actor_roles: list[str] | None,
) -> dict[str, Any]:
    _require_master(actor_roles)
    plant_random_breaks, _, _, repo_root = _import_plant_breaks()
    script = repo_root / "devtools" / "plant_breaks.py"
    if not script.is_file():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="devtools/plant_breaks.py not found — mount repo into API container.",
        )
    if not (repo_root / "api" / "app").is_dir():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="api/ source not writable or not mounted.",
        )
    return plant_random_breaks(repo_root, count=count, seed=seed)


def restore_planted_breaks(*, actor_roles: list[str] | None) -> dict[str, Any]:
    _require_master(actor_roles)
    _, restore_breaks, _, repo_root = _import_plant_breaks()
    return restore_breaks(repo_root)


def breaks_status() -> dict[str, Any]:
    _, _, status_fn, repo_root = _import_plant_breaks()
    from devtools.break_catalog import catalog_summary

    result = status_fn(repo_root)
    result["catalog"] = catalog_summary()
    return result
