"""Group a flat Judge failure list into independent repair tasks.

Why this exists: handing the Fixer 8-15 unrelated failures in one prompt and
asking for one combined diff is why fixes come back bundled — and why a
single bad change in that bundle costs you every good change alongside it
when the Reviewer rejects the whole thing. Clustering failures by their
actual shared root cause first means each repair task can be fixed, reviewed,
and committed independently: a rejection only costs that one task's attempt,
never a sibling task's already-approved fix.

This is intentionally simple — no embeddings, no ML. Two strong, cheap
signals are enough for this codebase's failure shapes:
  1. Shared planted_break_id (when present) is a near-certain same-root-cause
     signal — Judge already correlates failures to the break that caused them.
  2. Otherwise, group by suite: within one suite, a cluster of failing checks
     is usually one underlying bug cascading through that suite's assertions,
     not several independent bugs.
Tasks are ordered smallest/most-confident first — the planted_break_id
clusters (a strong, narrow signal) go before the murkier suite-only ones.
"""

from __future__ import annotations

from typing import Any


def cluster_failures(failure_report: dict[str, Any]) -> list[dict[str, Any]]:
    failures = list(failure_report.get("failures") or [])
    if not failures:
        return []

    by_break_id: dict[str, list[dict[str, Any]]] = {}
    unattributed: list[dict[str, Any]] = []
    for f in failures:
        bid = str(f.get("planted_break_id") or "").strip()
        if bid:
            by_break_id.setdefault(bid, []).append(f)
        else:
            unattributed.append(f)

    by_suite: dict[str, list[dict[str, Any]]] = {}
    for f in unattributed:
        suite = str(f.get("suite") or "unknown")
        by_suite.setdefault(suite, []).append(f)

    tasks: list[dict[str, Any]] = []

    for bid, group in by_break_id.items():
        tasks.append(
            {
                "task_id": f"break:{bid}",
                "failures": group,
                "suites": sorted({str(f.get("suite") or "") for f in group}),
                "confidence": "high",
                "reason": f"all share planted_break_id={bid}",
            }
        )

    for suite, group in by_suite.items():
        tasks.append(
            {
                "task_id": f"suite:{suite}",
                "failures": group,
                "suites": [suite],
                "confidence": "medium",
                "reason": f"co-failing checks within suite={suite}, no shared break id",
            }
        )

    tasks.sort(key=lambda t: (0 if t["confidence"] == "high" else 1, len(t["failures"])))
    return tasks


def task_failure_report(parent_report: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """Build a Fixer/Reviewer-facing failure_report scoped to just this task —
    same shape as a full Judge report, but with only this task's failures."""
    failures = task["failures"]
    return {
        "summary": {
            "total_suites": parent_report.get("summary", {}).get("total_suites"),
            "failed": len(task["suites"]),
            "total_check_failures": len(failures),
        },
        "failures": failures,
    }
