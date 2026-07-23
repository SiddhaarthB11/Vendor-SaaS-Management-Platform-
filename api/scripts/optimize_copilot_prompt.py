#!/usr/bin/env python3
"""
Phase 1c — Copilot prompt optimization via Not Diamond + eval validation.

Safety: Not Diamond API calls are blocked unless BOTH are set:
  - NOTDIAMOND_OPTIMIZE_ENABLED=true in infra/.env
  - --confirm-nd-spend on the command line

Zero-ND path (Gemini free tier only):
  docker exec slmct-api python /app/api/scripts/optimize_copilot_prompt.py --tournament --skip-nd

When you are ready for your ONE planned ND optimization run:
  1. Set in infra/.env: NOTDIAMOND_OPTIMIZE_ENABLED=true  (turn off after)
  2. docker compose -f infra/docker-compose.yml up -d api
  3. docker exec slmct-api python /app/api/scripts/optimize_copilot_prompt.py --nd-optimize --apply --confirm-nd-spend
  4. Set NOTDIAMOND_OPTIMIZE_ENABLED=false again
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.settings import get_settings

from app.copilot_eval import (
    _cleanup_eval_org,
    _seed_eval_org,
    build_eval_goldens,
    run_copilot_eval,
)
from app.llm import DEFAULT_CANDIDATES, notdiamond_configured, notdiamond_optimize_allowed
from app.notdiamond_optimize import NDPromptOptimizeResult, optimize_copilot_prompts_via_nd
from app.prompts import (
    COPILOT_MODEL_WINNERS,
    COPILOT_VARIANTS,
    apply_copilot_model_winners,
    append_copilot_variants_to_file,
    list_copilot_variant_names,
)

MODELS = list(DEFAULT_CANDIDATES)
MANUAL_VARIANTS = ["default", "strict", "interpretation", "formatting"]

COST_NOTICE = """
=== Not Diamond cost safety ===
ND prompt optimization uses a billable ND job (1 successful run ≈ 1 free unit; 11+ runs cost $20 each).
Eval / tournament calls use Gemini directly (your free API key) — not ND LLM Usage Tracker.

Blocked by default. To run ND once, set BOTH:
  NOTDIAMOND_OPTIMIZE_ENABLED=true   (in infra/.env)
  --confirm-nd-spend                 (on this command)

Set NOTDIAMOND_OPTIMIZE_ENABLED=false again immediately after your run.
"""


@contextmanager
def _db_conn() -> Iterator[Any]:
    conn = psycopg.connect(get_settings().database_url, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


def _prepare_goldens(conn) -> tuple[list[dict], list[dict]]:
    _cleanup_eval_org(conn)
    fixture = _seed_eval_org(conn)
    train, test = build_eval_goldens(conn, fixture["org_id"], fixture)
    _cleanup_eval_org(conn)
    return train, test


def _reload_api_prompts(base_url: str = "http://localhost:8000") -> None:
    import httpx

    try:
        response = httpx.post(f"{base_url.rstrip('/')}/api/diagnostics/reload-prompts", timeout=30)
        if response.status_code == 200:
            data = response.json()
            print(f"Reloaded API prompts ({len(data.get('variants', []))} variants)")
        else:
            print(f"Warning: reload-prompts returned HTTP {response.status_code}", file=sys.stderr)
    except Exception as exc:
        print(f"Warning: could not reload API prompts: {exc}", file=sys.stderr)


def _eval_counts(result: dict) -> tuple[int, int, int, int]:
    main = result.get("scores", {}).get("main", {})
    holdout = result.get("scores", {}).get("holdout", {})
    return (
        int(main.get("passed", 0)),
        int(main.get("total", 0)),
        int(holdout.get("passed", 0)),
        int(holdout.get("total", 0)),
    )


def _run_eval(conn, **payload) -> tuple[dict, int, int, int, int]:
    result = run_copilot_eval(conn, payload)
    main_p, main_t, hold_p, hold_t = _eval_counts(result)
    return result, main_p, main_t, hold_p, hold_t


def _print_nd_result(result: NDPromptOptimizeResult) -> None:
    if result.error:
        print(f"Not Diamond error: {result.error}", file=sys.stderr)
        return
    print(f"ND run_id: {result.run_id or 'n/a'}")
    print(f"Per-model prompts: {len(result.model_prompts)}")
    for model, prompt in result.model_prompts.items():
        print(f"  {model}: {len(prompt)} chars")
    print(f"Tournament variants: {len(result.variant_prompts)}")


def _nd_safety_status(*, confirm: bool) -> tuple[bool, str]:
    settings = get_settings()
    if not notdiamond_configured():
        return False, "NOTDIAMOND_API_KEY is not set."
    if not settings.notdiamond_optimize_enabled:
        return False, "NOTDIAMOND_OPTIMIZE_ENABLED is false (safe default)."
    if not confirm:
        return False, "Pass --confirm-nd-spend to authorize the ND API call."
    return True, "ND optimization authorized."


def _require_nd_authorization(*, confirm: bool) -> bool:
    ok, reason = _nd_safety_status(confirm=confirm)
    if ok:
        print(f"ND safety check: {reason}")
        return True
    print(COST_NOTICE, file=sys.stderr)
    print(f"ND blocked: {reason}", file=sys.stderr)
    return False


def run_preflight() -> int:
    settings = get_settings()
    print(COST_NOTICE)
    print("Preflight (no ND or Gemini calls):\n")
    print(f"  NOTDIAMOND_API_KEY set: {bool(settings.notdiamond_api_key)}")
    print(f"  NOTDIAMOND_OPTIMIZE_ENABLED: {settings.notdiamond_optimize_enabled}")
    print(f"  NOTDIAMOND_ROUTING_ENABLED: {settings.notdiamond_routing_enabled}")
    print(f"  Copilot variants: {list_copilot_variant_names(include_legacy=True)}")
    print(f"  Model winners: {list(COPILOT_MODEL_WINNERS.keys()) or '(none)'}")
    nd_variants = [k for k in COPILOT_VARIANTS if k.startswith('nd_')]
    print(f"  Existing ND variants on disk: {nd_variants or '(none)'}")
    if nd_variants:
        print("  Note: ND variants already exist — you may not need another ND run.")
    print("\nSafe commands:")
    print("  --preflight                          this check")
    print("  --tournament --skip-nd               manual variants only (Gemini eval)")
    print("  --nd-optimize --apply --confirm-nd-spend   ONE ND run (after enabling env flag)")
    return 0


def run_nd_optimize(*, apply: bool, dry_run: bool, confirm: bool) -> int:
    if not _require_nd_authorization(confirm=confirm):
        return 1

    print("Building eval goldens for Not Diamond…")
    with _db_conn() as conn:
        train, test = _prepare_goldens(conn)
    print(f"  train={len(train)} test={len(test)}")

    print("Running Not Diamond prompt optimization (this may take several minutes)…")
    result = optimize_copilot_prompts_via_nd(
        train,
        test,
        target_model_slugs=MODELS,
    )
    _print_nd_result(result)
    if result.error or not result.model_prompts:
        return 1

    if result.variant_prompts and not dry_run:
        append_copilot_variants_to_file(result.variant_prompts)
        print(f"Appended {len(result.variant_prompts)} ND variants to prompts.py")
        _reload_api_prompts()

    print("\nBaseline eval (default prompt, tools mode, Gemini only)…")
    with _db_conn() as conn:
        _, baseline_score, baseline_total, baseline_hold, baseline_hold_t = _run_eval(
            conn, copilot_mode="tools", prompt_variant="default"
        )
    print(f"  baseline: {baseline_score}/{baseline_total}")

    if apply and not dry_run:
        apply_copilot_model_winners(result.model_prompts)
        _reload_api_prompts()
        print(f"\nApplied ND per-model prompts to COPILOT_MODEL_WINNERS: {list(result.model_prompts.keys())}")

        print("Post-apply eval (ND prompts via model winners)…")
        with _db_conn() as conn:
            _, after_score, after_total, _, _ = _run_eval(conn, copilot_mode="tools", prompt_variant="default")
        print(f"  after ND apply: {after_score}/{after_total}")
        delta = after_score - baseline_score
        sign = "+" if delta >= 0 else ""
        print(f"  delta: {sign}{delta}")
        print("\nReminder: set NOTDIAMOND_OPTIMIZE_ENABLED=false in infra/.env now.")
    elif dry_run:
        print("\nDry run — ND results not written to prompts.py")
    else:
        print("\nRe-run with --apply to write ND per-model prompts to COPILOT_MODEL_WINNERS")

    return 0


def run_tournament(*, apply: bool, skip_nd: bool, dry_run: bool, confirm: bool) -> int:
    variants: dict[str, str] = {k: COPILOT_VARIANTS[k] for k in MANUAL_VARIANTS if k in COPILOT_VARIANTS}

    if not skip_nd:
        if not _require_nd_authorization(confirm=confirm):
            print("Continuing tournament with manual variants only (--skip-nd implied).", file=sys.stderr)
            skip_nd = True
        else:
            print("Fetching Not Diamond optimized variants…")
            with _db_conn() as conn:
                train, test = _prepare_goldens(conn)
            nd_result = optimize_copilot_prompts_via_nd(train, test, target_model_slugs=MODELS)
            _print_nd_result(nd_result)
            if nd_result.variant_prompts:
                variants.update(nd_result.variant_prompts)
                if not dry_run:
                    append_copilot_variants_to_file(nd_result.variant_prompts)
                    _reload_api_prompts()

    print(f"\nVariants in tournament: {list(variants.keys())}")
    print(f"Models: {MODELS}\n")

    grid: dict[str, dict[str, dict]] = {}
    default_holdout: dict[str, int] = {}
    with _db_conn() as conn:
        for model in MODELS:
            grid[model] = {}
            for variant in variants:
                print(f"Evaluating {model} × {variant}…", flush=True)
                _, score, total, hold_score, hold_total = _run_eval(
                    conn,
                    copilot_mode="tools",
                    prompt_variant=variant,
                    model=model,
                )
                grid[model][variant] = {
                    "score": score,
                    "total": total,
                    "holdout_score": hold_score,
                    "holdout_total": hold_total,
                }
                if variant == "default":
                    default_holdout[model] = hold_score
                print(f"  → main {score}/{total}, holdout {hold_score}/{hold_total}")

    print("\n=== Score grid (main / holdout) ===")
    header = ["model"] + list(variants.keys())
    print("\t".join(header))
    winners: dict[str, str] = {}
    for model in MODELS:
        row = [model]
        best_variant = "default"
        best_score = -1
        baseline_hold = default_holdout.get(model, 0)
        for variant in variants:
            cell = grid[model][variant]
            score = cell["score"]
            hold = cell["holdout_score"]
            eligible = hold >= baseline_hold
            row.append(f"{score}/{cell['total']} ({hold}/{cell['holdout_total']})")
            if eligible and score > best_score:
                best_score = score
                best_variant = variant
            elif not eligible and variant == best_variant:
                pass
        if best_score < 0:
            best_variant = "default"
        winners[model] = best_variant
        row.append(f"→ {best_variant}")
        print("\t".join(row))

    winner_prompts: dict[str, str] = {}
    for model, variant in winners.items():
        if variant in COPILOT_VARIANTS:
            winner_prompts[model] = COPILOT_VARIANTS[variant]
        elif variant in variants:
            winner_prompts[model] = variants[variant]

    print("\n=== Winners ===")
    print(json.dumps({m: {"variant": winners[m], "chars": len(winner_prompts.get(m, ""))} for m in MODELS}, indent=2))

    if apply and not dry_run and winner_prompts:
        apply_copilot_model_winners(winner_prompts)
        _reload_api_prompts()
        print("Applied tournament winners to COPILOT_MODEL_WINNERS")
    elif not apply:
        print("Re-run with --apply to persist winners")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1c copilot prompt optimization (Not Diamond)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--nd-optimize",
        action="store_true",
        help="Run Not Diamond prompt optimization and apply per-model prompts",
    )
    mode.add_argument(
        "--tournament",
        action="store_true",
        help="Run variant × model eval grid (manual + optional ND variants)",
    )
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="Show safety status and exit without calling ND or Gemini",
    )
    parser.add_argument("--apply", action="store_true", help="Write winners to prompts.py")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to prompts.py")
    parser.add_argument("--skip-nd", action="store_true", help="Tournament: never call ND")
    parser.add_argument(
        "--confirm-nd-spend",
        action="store_true",
        help="Required with --nd-optimize (and for ND fetch in --tournament) to allow billable ND API calls",
    )
    args = parser.parse_args()

    if args.preflight:
        return run_preflight()

    print(f"Copilot variants on disk: {list_copilot_variant_names(include_legacy=True)}")
    print(f"Current model winners: {list(COPILOT_MODEL_WINNERS.keys()) or '(none)'}")
    print(f"ND optimize allowed: {notdiamond_optimize_allowed()}\n")

    if args.tournament:
        return run_tournament(
            apply=args.apply,
            skip_nd=args.skip_nd,
            dry_run=args.dry_run,
            confirm=args.confirm_nd_spend,
        )

    if args.nd_optimize:
        return run_nd_optimize(apply=args.apply, dry_run=args.dry_run, confirm=args.confirm_nd_spend)

    # Safe default: preflight instead of silently attempting ND
    print("No mode selected — running preflight (use --nd-optimize or --tournament).", file=sys.stderr)
    return run_preflight()


if __name__ == "__main__":
    raise SystemExit(main())
