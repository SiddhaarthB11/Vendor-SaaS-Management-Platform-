"""Reversible one-line breaks mapped to judge diagnostic suites.

Every entry is verified to fail its suite's deterministic checks when applied.
Only suites exercised by the judge are included (see JUDGE_SUITE_NAMES).
"""

from __future__ import annotations

from typing import Any, TypedDict


class BreakSpec(TypedDict):
    id: str
    suite: str
    feature: str
    path: str
    old: str
    new: str
    replace_all: bool
    slow: bool  # suite skipped in quick judge when True


# Names must match devtools/judge.py DIAGNOSTIC_SUITES.
JUDGE_SUITE_NAMES: tuple[str, ...] = (
    "workflow",
    "it-subscription",
    "ai-test",
    "licence-assignment",
    "renewal",
    "master-admin",
    "hr-onboarding",
    "url-scraping",
    "renewal-alerts",
    "offboarding",
    "full-lifecycle",
    "slack",
    "upload-export",
    "copilot-eval",
    "operator-eval",
)

QUICK_JUDGE_SUITE_NAMES: tuple[str, ...] = tuple(
    name for name in JUDGE_SUITE_NAMES if name != "copilot-eval"
)


# Each entry causes its matching judge suite to fail until restored.
BREAK_CATALOG: list[BreakSpec] = [
    {
        "id": "workflow-esr-lm",
        "suite": "workflow",
        "feature": "Employee software request requires line manager approval",
        "path": "api/app/workflow_governance.py",
        "old": '"employee_software_request": {\n        "label": "Employee Software Request",\n        "stages": [\n            "submitted",\n            "line_manager_approved",\n            "finance_approved",\n            "completed",\n        ],\n        "requires_line_manager": True,',
        "new": '"employee_software_request": {\n        "label": "Employee Software Request",\n        "stages": [\n            "submitted",\n            "line_manager_approved",\n            "finance_approved",\n            "completed",\n        ],\n        "requires_line_manager": False,',
        "replace_all": False,
        "slow": False,
    },
    {
        "id": "it-subscription-finance-gate",
        "suite": "it-subscription",
        "feature": "IT subscription finance validation from submitted",
        "path": "api/app/workflow_governance.py",
        "old": '    if workflow_type == "new_subscription_request":\n        return ("submitted", "reopened", "master_approved")',
        "new": '    if workflow_type == "new_subscription_request":\n        return ("master_approved",)',
        "replace_all": False,
        "slow": False,
    },
    {
        "id": "ai-test-tool-summary",
        "suite": "ai-test",
        "feature": "AI tool summary provider",
        "path": "api/app/main.py",
        "old": '        return {"summary": summary, "provider": "gemini"}',
        "new": '        return {"summary": summary, "provider": "fallbac"}',
        "replace_all": False,
        "slow": False,
    },
    {
        "id": "licence-assignment-finance-gate",
        "suite": "licence-assignment",
        "feature": "Licence assignment finance validation from submitted",
        "path": "api/app/workflow_governance.py",
        "old": '    if workflow_type == "license_assignment_request":\n        return ("submitted", "reopened", "master_approved")',
        "new": '    if workflow_type == "license_assignment_request":\n        return ("master_approved",)',
        "replace_all": False,
        "slow": False,
    },
    {
        "id": "renewal-finance-gate",
        "suite": "renewal",
        "feature": "Renewal request finance validation from submitted",
        "path": "api/app/workflow_governance.py",
        "old": '    if workflow_type == "renewal_request":\n        return ("submitted", "reopened", "master_approved")',
        "new": '    if workflow_type == "renewal_request":\n        return ("master_approved",)',
        "replace_all": False,
        "slow": False,
    },
    {
        "id": "master-admin-approve-status",
        "suite": "master-admin",
        "feature": "Master admin workflow approval status",
        "path": "api/app/main.py",
        "old": "                SET status = 'master_approved',",
        "new": "                SET status = 'master_approvd',",
        "replace_all": False,
        "slow": False,
    },
    {
        "id": "hr-onboarding-finance-gate",
        "suite": "hr-onboarding",
        "feature": "HR onboarding finance validation from submitted",
        "path": "api/app/workflow_governance.py",
        "old": '    if workflow_type == "hr_onboarding_request":\n        return ("submitted", "reopened", "master_approved")',
        "new": '    if workflow_type == "hr_onboarding_request":\n        return ("master_approved",)',
        "replace_all": False,
        "slow": False,
    },
    {
        "id": "url-scraping-catalogue",
        "suite": "url-scraping",
        "feature": "Vendor catalogue pricing URL prerequisite",
        "path": "api/app/main.py",
        "old": '    chk("Vendor catalogue: entries with a pricing URL", total_entries > 0,',
        "new": '    chk("Vendor catalogue: entries with a pricing URL", total_entries > 999999,',
        "replace_all": False,
        "slow": False,
    },
    {
        "id": "renewal-alerts-thresholds",
        "suite": "renewal-alerts",
        "feature": "Renewal alert day thresholds",
        "path": "api/app/notifications.py",
        "old": "    THRESHOLDS = [30, 60, 90]",
        "new": "    THRESHOLDS = [31, 61, 91]",
        "replace_all": False,
        "slow": False,
    },
    {
        "id": "offboarding-it-confirm",
        "suite": "offboarding",
        "feature": "Offboarding IT confirmation status",
        "path": "api/app/main.py",
        "old": '            _next_status = "it_confirmed" if _wf_type_check == "employee_offboarding" else "finance_approved"',
        "new": '            _next_status = "it_confirm" if _wf_type_check == "employee_offboarding" else "finance_approved"',
        "replace_all": False,
        "slow": False,
    },
    {
        "id": "full-lifecycle-onboard-complete",
        "suite": "full-lifecycle",
        "feature": "HR onboarding procurement completion gate",
        "path": "api/app/main.py",
        "old": '            elif _wf_type_complete == "hr_onboarding_request":\n                _allowed_complete = {"finance_approved"}',
        "new": '            elif _wf_type_complete == "hr_onboarding_request":\n                _allowed_complete = {"finance_approvd"}',
        "replace_all": False,
        "slow": False,
    },
    {
        "id": "slack-webhook-env",
        "suite": "slack",
        "feature": "Slack webhook env var lookup in diagnostic",
        "path": "api/app/main.py",
        "old": '    WEBHOOK_URL = _os.environ.get("SLACK_WEBHOOK_SOFTWARE_REQUESTS", "")',
        "new": '    WEBHOOK_URL = _os.environ.get("SLACK_WEBHOOK_SOFTWARE_REQUESTSS", "")',
        "replace_all": False,
        "slow": False,
    },
    {
        "id": "upload-export-modules",
        "suite": "upload-export",
        "feature": "Bulk export module list",
        "path": "api/app/main.py",
        "old": 'exportable = {"vendors", "subscriptions", "licences", "budgets", "payments", "employees", "contracts"}',
        "new": 'exportable = {"subscriptions", "licences", "budgets", "payments", "employees", "contracts"}',
        "replace_all": True,
        "slow": False,
    },
    {
        "id": "copilot-eval-fixture-count",
        "suite": "copilot-eval",
        "feature": "Copilot eval seeded subscription count check",
        "path": "api/app/copilot_eval.py",
        "old": "            len(state.get(\"subscriptions\") or []) == 12 and len(state.get(\"licences\") or []) == 5,",
        "new": "            len(state.get(\"subscriptions\") or []) == 13 and len(state.get(\"licences\") or []) == 5,",
        "replace_all": False,
        "slow": True,
    },
    {
        "id": "operator-eval-budget-dept",
        "suite": "operator-eval",
        "feature": "Operator eval create_budget department field",
        "path": "api/app/operator_eval.py",
        "old": '            department="Marketing",',
        "new": '            department="Marketin",',
        "replace_all": False,
        "slow": False,
    },
]

BREAKS_BY_ID: dict[str, BreakSpec] = {b["id"]: b for b in BREAK_CATALOG}
BREAKS_BY_SUITE: dict[str, BreakSpec] = {b["suite"]: b for b in BREAK_CATALOG}


def catalog_for_judge(*, include_slow: bool = False) -> list[BreakSpec]:
    """Catalog entries whose suite is run by the judge (quick mode excludes slow by default)."""
    allowed = set(JUDGE_SUITE_NAMES if include_slow else QUICK_JUDGE_SUITE_NAMES)
    return [b for b in BREAK_CATALOG if b["suite"] in allowed]


def catalog_summary() -> list[dict[str, Any]]:
    return [
        {
            "id": b["id"],
            "suite": b["suite"],
            "feature": b["feature"],
            "path": b["path"],
            "slow": b.get("slow", False),
        }
        for b in BREAK_CATALOG
    ]
