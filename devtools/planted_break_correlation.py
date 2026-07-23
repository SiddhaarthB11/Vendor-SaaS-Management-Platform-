"""Correlate judge failures with intentionally planted breaks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from devtools.break_catalog import BREAKS_BY_SUITE
from devtools.plant_breaks import get_planted_breaks


def enrich_report_with_planted_breaks(report: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    """Attach planted-break detection summary when demo breaks are active."""
    state = get_planted_breaks(repo_root)
    if not state or not (state.get("breaks") or []):
        return report

    suites_run = {str(s.get("name")): s for s in report.get("suites") or []}
    failed_suites = {name for name, s in suites_run.items() if s.get("overall") == "fail"}

    correlations: list[dict[str, Any]] = []
    for entry in state.get("breaks") or []:
        suite = str(entry.get("suite") or "")
        suite_result = suites_run.get(suite)
        if suite_result is None:
            run_status = "not_run"
            detected = False
        elif suite in failed_suites:
            run_status = "failed_as_expected"
            detected = True
        else:
            run_status = "passed_unexpectedly"
            detected = False

        spec = BREAKS_BY_SUITE.get(suite) or {}
        failures = [
            {
                "check": f.get("name") or f.get("check"),
                "detail": f.get("detail", ""),
            }
            for f in (suite_result or {}).get("failures") or []
        ]

        correlations.append(
            {
                "break_id": entry.get("id"),
                "suite": suite,
                "feature": entry.get("feature") or spec.get("feature"),
                "path": entry.get("path") or spec.get("path"),
                "run_status": run_status,
                "detected": detected,
                "failures": failures[:5],
            }
        )

    detected_count = sum(1 for c in correlations if c["detected"])
    planted_count = len(correlations)
    missed = [c for c in correlations if c["run_status"] == "passed_unexpectedly"]
    not_run = [c for c in correlations if c["run_status"] == "not_run"]

    report["planted_breaks"] = {
        "active": True,
        "planted_at": state.get("planted_at"),
        "count": planted_count,
        "detected_count": detected_count,
        "missed_count": len(missed),
        "not_run_count": len(not_run),
        "detection_rate": round(detected_count / planted_count, 3) if planted_count else None,
        "correlations": correlations,
    }

    # Tag top-level failures with the planted break id when suite matches.
    by_suite = {c["suite"]: c["break_id"] for c in correlations}
    for failure in report.get("failures") or []:
        suite = str(failure.get("suite") or "")
        if suite in by_suite and suite in failed_suites:
            failure["planted_break_id"] = by_suite[suite]

    if missed:
        report.setdefault("notes", []).append(
            f"{len(missed)} planted break(s) did not fail their suite — catalog or diagnostics may be out of sync."
        )
    if not_run:
        report.setdefault("notes", []).append(
            f"{len(not_run)} planted break(s) were not exercised (suite not run — e.g. copilot-eval in quick judge)."
        )

    return report
