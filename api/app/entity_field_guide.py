"""Cross-module field importance for AI Operator planning."""

from __future__ import annotations

from typing import Any

# Recommended (not required) fields per write tool — surfaced in planner prompts.
WRITE_TOOL_RECOMMENDED: dict[str, list[str]] = {
    "create_vendor": [
        "legal_name",
        "website_url",
        "contact_name",
        "contact_email",
    ],
    "create_vendor_catalogue_item": [
        "scrape_url (pricing page — auto-fills workflow amounts and IT purchase links)",
        "price + currency_code (monthly price shown on subscription workflow form and approvals)",
        "password_change_url (used in offboarding emails)",
    ],
    "create_subscription": [
        "vendor_id",
        "amount + currency_code",
        "department",
        "billing_cycle",
        "renewal_date",
    ],
    "submit_workflow_request (subscriptions)": [
        "payload.vendor_id",
        "payload.name (must match vendor_catalogue name for auto-pricing)",
        "payload.amount + payload.currency_code + payload.department",
    ],
    "submit_workflow_request (licences)": [
        "payload.licence_id or subscription context",
        "payload.person_id for assignment workflows",
    ],
    "create_budget": [
        "currency_code",
        "notes",
    ],
    "create_employee": [
        "department",
        "job_title",
        "line_manager_email (routes line-manager workflow approvals)",
    ],
    "assign_licence": [
        "Requires master_admin — finance/IT submit license_assignment_request workflow instead",
    ],
}

ENTITY_GUIDE: dict[str, dict[str, Any]] = {
    "vendor": {
        "summary": "Vendor master record on the Vendors tab.",
        "required_for_create": ["name"],
        "recommended": ["legal_name", "website_url", "contact_name", "contact_email"],
        "surfaces_in": [
            "Subscription and workflow vendor picker",
            "Spend summaries grouped by vendor",
            "Vendor catalogue (linked by vendor_id)",
        ],
        "related_entities": ["vendor_catalogue"],
        "planner_hints": [
            "Call lookup_vendor_autofill when the user only provides a product or company name.",
            "If subscriptions or procurement workflows are involved, also plan vendor_catalogue items.",
        ],
    },
    "vendor_catalogue": {
        "summary": "Pricing catalogue entries under each vendor (name, monthly price, pricing URL).",
        "required_for_create": ["vendor_id", "name", "price"],
        "recommended": ["scrape_url", "currency_code", "password_change_url"],
        "surfaces_in": [
            "Subscription workflow submit form — dropdown auto-fills amount from catalogue price",
            "Admin/finance approval screens — amount from workflow payload",
            "IT procurement completion — purchase link from scrape_url via /vendor-purchase-url",
            "Nightly price scraper — keeps price fresh from scrape_url",
            "Offboarding emails — password_change_url lookup",
        ],
        "related_entities": ["vendor", "subscription", "workflow"],
        "planner_hints": [
            "Call get_vendor_catalogue before creating duplicates.",
            "Use lookup_pricing_url when scrape_url is missing.",
            "After create_vendor, reference vendor id as $step:0:vendor.id in catalogue/subscription steps.",
        ],
    },
    "subscription": {
        "summary": "Active software subscription records.",
        "required_for_create": ["name"],
        "recommended": ["vendor_id", "amount", "currency_code", "department", "billing_cycle", "renewal_date"],
        "surfaces_in": [
            "Licence pool creation after workflow completion",
            "Spend summaries and budget validation",
            "Renewal alerts and Insights",
        ],
        "related_entities": ["vendor", "vendor_catalogue", "licence", "budget"],
        "planner_hints": [
            "Non-master roles should use submit_workflow_request with workflow_type new_subscription_request.",
            "Include amount and department in workflow payload for finance budget checks.",
        ],
    },
    "workflow": {
        "summary": "Approval-chain requests; no record is created until the workflow completes.",
        "required_for_submit": ["workflow_type", "requested_module", "payload"],
        "surfaces_in": [
            "Line manager, master admin, finance, and IT approval queues",
            "Email notifications at each stage",
            "Final activation creates subscriptions/licences/employees",
        ],
        "planner_hints": [
            "Never plan approve/reject actions — Operator only submits.",
            "Finance cannot assign licences directly; use license_assignment_request workflow.",
        ],
    },
    "budget": {
        "summary": "Department fiscal-year budget allocations.",
        "required_for_create": ["fiscal_year", "department", "allocated_amount"],
        "surfaces_in": [
            "Finance budget validation on subscription workflows",
            "Co-pilot budget vs spend comparisons",
        ],
        "planner_hints": [
            "Use create_budget directly for finance/auditor/master_admin — department is a string label, not a new entity.",
            "Never submit_workflow_request for budgets — SLMCT has no budget allocation workflow.",
            "IT Admin and other roles: refuse budget create instructions in warnings[]; ask Finance.",
        ],
    },
    "employee": {
        "summary": "People records used for licence assignment and workflow routing.",
        "required_for_create": ["full_name", "work_email"],
        "recommended": ["department", "line_manager_email", "job_title"],
        "surfaces_in": [
            "Licence assignee display",
            "Line-manager approval routing",
            "Offboarding workflows",
        ],
    },
    "licence": {
        "summary": "Individual software seats linked to subscriptions.",
        "surfaces_in": [
            "Insights utilization",
            "Assignment/revocation audit trail",
            "Employee offboarding",
        ],
        "planner_hints": [
            "assign_licence and revoke_licence are master_admin only.",
        ],
    },
}

GOAL_FIELD_HINTS: dict[str, list[str]] = {
    "subscription_request": [
        "Need vendor (or create_vendor), catalogue item with price + scrape_url, amount, department.",
    ],
    "procurement": [
        "Catalogue scrape_url drives IT purchase link at workflow completion.",
    ],
    "licence_assignment": [
        "Need licence_id and person_id; finance/IT use workflow not direct assign unless master_admin.",
    ],
    "budget_tracking": [
        "Budget needs fiscal_year, department, allocated_amount; compare with get_spend_summary.",
    ],
    "onboarding": [
        "Employee department and line_manager_email affect approval routing.",
    ],
}


def get_field_guide(*, entity: str | None = None, goal: str | None = None) -> dict[str, Any]:
    """Return cross-module field guidance for Operator planning."""
    entity_key = (entity or "").strip().lower().replace(" ", "_")
    goal_key = (goal or "").strip().lower().replace(" ", "_")

    if entity_key:
        aliases = {
            "vendors": "vendor",
            "catalogue": "vendor_catalogue",
            "catalog": "vendor_catalogue",
            "subscriptions": "subscription",
            "workflows": "workflow",
            "budgets": "budget",
            "employees": "employee",
            "licences": "licence",
            "licenses": "licence",
        }
        entity_key = aliases.get(entity_key, entity_key)
        if entity_key in ENTITY_GUIDE:
            out: dict[str, Any] = {"entity": entity_key, **ENTITY_GUIDE[entity_key]}
            if goal_key and goal_key in GOAL_FIELD_HINTS:
                out["goal_hints"] = GOAL_FIELD_HINTS[goal_key]
            return out
        return {
            "error": f"Unknown entity '{entity}'.",
            "available_entities": sorted(ENTITY_GUIDE.keys()),
        }

    result: dict[str, Any] = {
        "entities": ENTITY_GUIDE,
        "write_tool_recommended_fields": WRITE_TOOL_RECOMMENDED,
    }
    if goal_key and goal_key in GOAL_FIELD_HINTS:
        result["goal_hints"] = GOAL_FIELD_HINTS[goal_key]
    return result
