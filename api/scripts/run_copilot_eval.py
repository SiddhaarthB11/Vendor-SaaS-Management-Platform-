#!/usr/bin/env python3
"""Run Copilot eval suite (tools mode by default)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.copilot_eval import run_copilot_eval
from app.settings import get_settings


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "tools"
    payload = {"copilot_mode": mode}
    if mode == "routing":
        payload = {"routing_benchmark": True, "copilot_mode": "tools"}

    conn = psycopg.connect(get_settings().database_url, row_factory=dict_row)
    try:
        result = run_copilot_eval(conn, payload)
    finally:
        conn.close()

    print(json.dumps({"summary": result.get("summary"), "scores": result.get("scores")}, indent=2))
    if result.get("comparison"):
        print(json.dumps({"comparison": result.get("comparison")}, indent=2))

    summary = result.get("summary") or {}
    if result.get("status") == "skipped":
        print(f"Skipped: {result.get('reason')}", file=sys.stderr)
        return 1

    if payload.get("routing_benchmark"):
        comparison = result.get("comparison") or {}
        routed_cost = (result.get("routed") or {}).get("cost") or {}
        ok = bool(comparison.get("score_ok")) and int(routed_cost.get("routed_calls") or 0) > 0
        return 0 if ok else 1

    return 0 if summary.get("overall") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
