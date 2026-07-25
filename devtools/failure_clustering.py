"""Group a flat Judge failure list into independent repair tasks.

Why this exists: handing the Fixer 8-15 unrelated failures in one prompt and
asking for one combined diff is why fixes come back bundled — and why a
single bad change in that bundle costs you every good change alongside it
when the Reviewer rejects the whole thing. Clustering failures by their
actual shared root cause first means each repair task can be fixed, reviewed,
and committed independently: a rejection only costs that one task's attempt,
never a sibling task's already-approved fix.

This is intentionally simple — no embeddings, no ML, no filesystem access.
Three cheap signals, strongest first:
  1. Shared planted_break_id (when present) is a near-certain same-root-cause
     signal — Judge already correlates failures to the break that caused them.
  2. Otherwise, within a suite, group by "topic" — the prefix before the first
     colon in the check name (e.g. "Finance: IT procurement email" and
     "Finance: finance confirmation email" share topic "finance"). This
     naming convention already exists consistently across every diagnostic
     suite's checks (Submit:, Finance:, Downstream:, Completion:, ...), so
     it's a real, free signal for "these failures likely share a root cause"
     without inventing new machinery — and it's meaningfully finer-grained
     than suite alone, which would otherwise bundle unrelated bugs that
     happen to live in the same suite back into one task.
  3. Checks with no colon in the name fall back to one task per suite.
Tasks are ordered smallest/most-confident first — planted_break_id clusters
go before topic clusters, which go before the coarse suite-only fallback.
"""

from __future__ import annotations

from typing import Any


def _topic(check_name: str) -> str | None:
    if ":" not in check_name:
        return None
    prefix = check_name.split(":", 1)[0].strip().lower()
    return prefix or None


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

    by_suite_topic: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_suite_only: dict[str, list[dict[str, Any]]] = {}
    for f in unattributed:
        suite = str(f.get("suite") or "unknown")
        topic = _topic(str(f.get("check") or ""))
        if topic:
            by_suite_topic.setdefault((suite, topic), []).append(f)
        else:
            by_suite_only.setdefault(suite, []).append(f)

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

    for (suite, topic), group in by_suite_topic.items():
        tasks.append(
            {
                "task_id": f"topic:{suite}:{topic}",
                "failures": group,
                "suites": [suite],
                "confidence": "medium-high",
                "reason": f"co-failing checks sharing topic '{topic}:' within suite={suite}",
            }
        )

    for suite, group in by_suite_only.items():
        tasks.append(
            {
                "task_id": f"suite:{suite}",
                "failures": group,
                "suites": [suite],
                "confidence": "medium",
                "reason": f"co-failing checks within suite={suite}, no shared break id or check topic",
            }
        )

    confidence_rank = {"high": 0, "medium-high": 1, "medium": 2}
    tasks.sort(key=lambda t: (confidence_rank.get(t["confidence"], 3), len(t["failures"])))
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
