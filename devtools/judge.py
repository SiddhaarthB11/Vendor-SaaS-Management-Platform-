#!/usr/bin/env python3
"""Run SLMCT diagnostic suites and emit a structured JSON report."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 900.0

# All POST /api/diagnostics/* test suites (excludes reload-prompts utility).
DIAGNOSTIC_SUITES: list[dict[str, Any]] = [
    {"name": "workflow", "path": "/api/diagnostics/run-workflow-test"},
    {"name": "it-subscription", "path": "/api/diagnostics/run-it-subscription-test"},
    {"name": "ai-test", "path": "/api/diagnostics/run-ai-test"},
    {"name": "licence-assignment", "path": "/api/diagnostics/run-licence-assignment-test"},
    {"name": "renewal", "path": "/api/diagnostics/run-renewal-test"},
    {"name": "master-admin", "path": "/api/diagnostics/run-master-admin-test"},
    {"name": "hr-onboarding", "path": "/api/diagnostics/run-hr-onboarding-test"},
    {"name": "url-scraping", "path": "/api/diagnostics/run-url-scraping-test"},
    {"name": "renewal-alerts", "path": "/api/diagnostics/run-renewal-alerts-test"},
    {"name": "offboarding", "path": "/api/diagnostics/run-offboarding-test"},
    {"name": "full-lifecycle", "path": "/api/diagnostics/run-full-lifecycle-test"},
    {"name": "slack", "path": "/api/diagnostics/run-slack-test"},
    {"name": "upload-export", "path": "/api/diagnostics/run-upload-export-test"},
    {
        "name": "copilot-eval",
        "path": "/api/diagnostics/run-copilot-eval",
        "payload": {},
        "slow": True,
        "tags": ["llm"],
    },
    {
        "name": "operator-eval",
        "path": "/api/diagnostics/run-operator-eval",
        "payload": {"skip_llm": True},
        "tags": ["llm"],
    },
]

SUITES_BY_NAME = {s["name"]: s for s in DIAGNOSTIC_SUITES}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _failed_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in checks if c.get("status") == "fail"]


def _suite_overall(result: dict[str, Any]) -> str:
    if result.get("status") == "skipped":
        return "skip"
    summary = result.get("summary") or {}
    if isinstance(summary, dict) and summary.get("overall"):
        return str(summary["overall"])
    checks = result.get("checks") or []
    fails = _failed_checks(checks)
    if result.get("status") == "error":
        return "fail"
    return "fail" if fails else "pass"


def _normalize_suite_result(name: str, raw: dict[str, Any] | None, *, error: str | None = None) -> dict[str, Any]:
    if error:
        return {
            "name": name,
            "overall": "fail",
            "error": error,
            "checks": [],
            "summary": {"overall": "fail", "failed": 1, "passed": 0, "total": 1},
            "failures": [{"name": "suite execution", "detail": error}],
        }
    raw = raw or {}
    checks = list(raw.get("checks") or [])
    failures = _failed_checks(checks)
    overall = _suite_overall(raw)
    return {
        "name": name,
        "overall": overall,
        "status": raw.get("status"),
        "reason": raw.get("reason"),
        "summary": raw.get("summary"),
        "checks": checks,
        "failures": [{"name": f.get("name"), "detail": f.get("detail", "")} for f in failures],
        "mode": raw.get("mode"),
    }


def run_suite(
    client: httpx.Client,
    suite: dict[str, Any],
    *,
    operator_skip_llm: bool = True,
    copilot_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = suite["name"]
    payload = dict(suite.get("payload") or {})
    if name == "operator-eval":
        payload["skip_llm"] = operator_skip_llm
    if name == "copilot-eval" and copilot_payload:
        payload.update(copilot_payload)

    started = time.perf_counter()
    try:
        response = client.post(suite["path"], json=payload)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            return _normalize_suite_result(
                name,
                None,
                error=f"HTTP {response.status_code}: {response.text[:500]}",
            )
        raw = response.json()
        result = _normalize_suite_result(name, raw)
        result["elapsed_ms"] = elapsed_ms
        return result
    except httpx.RequestError as exc:
        return _normalize_suite_result(name, None, error=str(exc))


def build_report(
    suite_results: list[dict[str, Any]],
    *,
    base_url: str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    failed_suites = [s for s in suite_results if s.get("overall") == "fail"]
    skipped_suites = [s for s in suite_results if s.get("overall") == "skip"]
    passed_suites = [s for s in suite_results if s.get("overall") == "pass"]

    all_failures: list[dict[str, Any]] = []
    for suite in failed_suites:
        for failure in suite.get("failures") or []:
            all_failures.append(
                {
                    "suite": suite["name"],
                    "check": failure.get("name"),
                    "detail": failure.get("detail", ""),
                }
            )
        if suite.get("error"):
            all_failures.append(
                {"suite": suite["name"], "check": "suite execution", "detail": suite["error"]}
            )

    report = {
        "tool": "judge",
        "generated_at": _utc_now(),
        "base_url": base_url,
        "overall": "pass" if not failed_suites else "fail",
        "summary": {
            "total_suites": len(suite_results),
            "passed": len(passed_suites),
            "failed": len(failed_suites),
            "skipped": len(skipped_suites),
            "total_check_failures": len(all_failures),
        },
        "suites": suite_results,
        "failures": all_failures,
    }
    if repo_root is not None:
        from devtools.planted_break_correlation import enrich_report_with_planted_breaks

        enrich_report_with_planted_breaks(report, repo_root)
    return report


def select_suites(
    *,
    suite_name: str | None,
    quick: bool,
    include_slow: bool,
) -> list[dict[str, Any]]:
    if suite_name:
        if suite_name not in SUITES_BY_NAME:
            known = ", ".join(sorted(SUITES_BY_NAME))
            raise SystemExit(f"Unknown suite {suite_name!r}. Known: {known}")
        return [SUITES_BY_NAME[suite_name]]

    suites = list(DIAGNOSTIC_SUITES)
    if quick or not include_slow:
        suites = [s for s in suites if not s.get("slow")]
    return suites


def run_judge(
    *,
    base_url: str = DEFAULT_BASE_URL,
    suite_name: str | None = None,
    quick: bool = False,
    include_slow: bool = False,
    operator_skip_llm: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    output_path: Path | None = None,
) -> dict[str, Any]:
    suites = select_suites(suite_name=suite_name, quick=quick, include_slow=include_slow)
    results: list[dict[str, Any]] = []

    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        try:
            health = client.get("/health", timeout=10.0)
            if health.status_code >= 400:
                raise httpx.RequestError(f"/health returned HTTP {health.status_code}")
        except httpx.RequestError as exc:
            report = build_report([], base_url=base_url)
            report["overall"] = "fail"
            report["error"] = f"API unreachable at {base_url}: {exc}"
            report["failures"] = [{"suite": "health", "check": "connectivity", "detail": report["error"]}]
            if output_path:
                output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return report

        for suite in suites:
            print(f"[judge] running {suite['name']}...", flush=True)
            results.append(run_suite(client, suite, operator_skip_llm=operator_skip_llm))

    report = build_report(results, base_url=base_url, repo_root=Path(__file__).resolve().parents[1])
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run SLMCT diagnostic suites (judge).")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base URL")
    parser.add_argument("--suite", dest="suite_name", help="Run a single suite by name")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip slow LLM suites (copilot-eval). Default for watch/autofix loops.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include slow suites (copilot-eval).",
    )
    parser.add_argument(
        "--operator-llm",
        action="store_true",
        help="Run operator-eval planner LLM checks (skip_llm=false).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "devtools" / "state" / "judge_report.json",
        help="Write JSON report to this path",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    include_slow = args.full and not args.quick
    report = run_judge(
        base_url=args.base_url.rstrip("/"),
        suite_name=args.suite_name,
        quick=args.quick and not args.full,
        include_slow=include_slow,
        operator_skip_llm=not args.operator_llm,
        timeout=args.timeout,
        output_path=args.output,
    )

    print(json.dumps(report["summary"], indent=2))
    if report.get("failures"):
        print("\nFailures:")
        for item in report["failures"][:30]:
            print(f"  - [{item['suite']}] {item['check']}: {str(item.get('detail', ''))[:200]}")
        if len(report["failures"]) > 30:
            print(f"  ... and {len(report['failures']) - 30} more")
    print(f"\nReport written to {args.output}")
    try:
        httpx.post(f"{args.base_url.rstrip('/')}/api/diagnostics/cleanup-all", timeout=60.0)
        print("[judge] diagnostic cleanup sweep completed")
    except Exception as exc:
        print(f"[judge] cleanup sweep failed: {exc}")
    return 0 if report.get("overall") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
