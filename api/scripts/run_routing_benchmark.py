#!/usr/bin/env python3
"""
Phase 1d — Not Diamond model routing benchmark.

Compares single-model copilot eval vs ND-routed eval on the same fixture.
Requires NOTDIAMOND_API_KEY and NOTDIAMOND_ROUTING_ENABLED=true in infra/.env.

  docker exec slmct-api python /app/api/scripts/run_routing_benchmark.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.copilot_eval import run_routing_benchmark
from app.settings import get_settings


def main() -> int:
    settings = get_settings()
    print("Phase 1d routing benchmark")
    print(f"  NOTDIAMOND_API_KEY set: {bool(settings.notdiamond_api_key)}")
    print(f"  NOTDIAMOND_ROUTING_ENABLED: {settings.notdiamond_routing_enabled}\n")

    conn = psycopg.connect(settings.database_url, row_factory=dict_row)
    try:
        result = run_routing_benchmark(conn, {"copilot_mode": "tools"})
    finally:
        conn.close()

    print(json.dumps(
        {
            "status": result.get("status"),
            "mode": result.get("mode"),
            "summary": result.get("summary"),
            "comparison": result.get("comparison"),
            "baseline": {
                "model": result.get("baseline", {}).get("model"),
                "scores": result.get("baseline", {}).get("scores"),
                "cost": result.get("baseline", {}).get("cost"),
            },
            "routed": {
                "scores": result.get("routed", {}).get("scores"),
                "cost": result.get("routed", {}).get("cost"),
            },
        },
        indent=2,
    ))

    if result.get("status") == "skipped":
        print(f"\nSkipped: {result.get('reason')}", file=sys.stderr)
        return 1

    comparison = result.get("comparison") or {}
    routed_cost = (result.get("routed") or {}).get("cost") or {}
    score_ok = bool(comparison.get("score_ok"))
    routed_calls = int(routed_cost.get("routed_calls") or 0)

    if score_ok:
        print("\nRouting benchmark PASSED (routed score >= baseline).")
    else:
        print("\nRouting benchmark FAILED (routed score below baseline).", file=sys.stderr)

    if routed_calls <= 0:
        print("Routing benchmark FAILED (no ND-routed calls logged).", file=sys.stderr)

    routing_report = (result.get("routed") or {}).get("routing_report") or []
    if routing_report:
        print("\nRouting report (sample):")
        for row in routing_report[:5]:
            print(
                f"  {row['question_id']}: model={row['model']} "
                f"routed_by={row.get('routed_by')} tokens={row.get('tokens')}"
            )
        if len(routing_report) > 5:
            print(f"  ... and {len(routing_report) - 5} more")

    # Phase 1d acceptance: routed score >= baseline and ND routing active — not every
    # per-question check must pass in both runs (LLM variance on baseline vs routed).
    phase_ok = score_ok and routed_calls > 0 and result.get("status") == "ok"
    summary = result.get("summary") or {}
    if phase_ok:
        print(
            f"\nOverall eval checks: {summary.get('passed')}/{summary.get('total')} passed "
            f"({summary.get('failed')} question-level failures in combined runs)."
        )
    return 0 if phase_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
