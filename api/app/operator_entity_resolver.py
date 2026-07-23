"""Resolve entity IDs from org data when the Operator planner asks the user unnecessarily."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

BULK_REVOKE_INTENT_RE = re.compile(
    r"\b(revoke|remove|delete|decommission|cancel|drop)\b.{0,40}\b(\d+|top\s+\d+|first\s+\d+)\b.{0,40}\blicen[cs]e",
    re.IGNORECASE,
)
BULK_REVOKE_INTENT_ALT_RE = re.compile(
    r"\b(top|first|latest)\s+(\d+)\s+licen[cs]e",
    re.IGNORECASE,
)
COUNT_IN_INSTRUCTION_RE = re.compile(
    r"\b(?:top|first|latest|recent(?:ly)?(?:\s+assigned)?)\s*(\d+)\b|\b(\d+)\s+licen[cs]e",
    re.IGNORECASE,
)
RECENT_ASSIGNMENT_RE = re.compile(
    r"\b(recent(?:ly)?(?:\s+assigned)?|latest\s+assigned|most\s+recent(?:ly)?\s+assigned)\b",
    re.IGNORECASE,
)
LICENCE_ID_QUESTION_RE = re.compile(
    r"\b(licen[cs]e\s+ids?|specific\s+ids?|provide\s+the\s+.*ids?|cannot\s+determine)\b",
    re.IGNORECASE,
)
LICENCE_ASSIGN_INTENT_RE = re.compile(
    r"\b(assign|give|allocate|grant|provide)\b.{0,72}\blicen[cs]e\b|\blicen[cs]e\b.{0,48}\b(assign|to|for)\b",
    re.IGNORECASE,
)
EMPLOYEE_SOFTWARE_REQUEST_RE = re.compile(
    r"\b(?:employee\s+software\s+request|raise\s+(?:a\s+)?(?:employee\s+)?software\s+request|software\s+request)\b",
    re.IGNORECASE,
)
_ESR_PRODUCT_RE = re.compile(
    r"(?:employee\s+software\s+request\s+for|software\s+request\s+for|request\s+for)\s+(.+?)(?:\s*$|[\.\,])",
    re.IGNORECASE,
)
_EMPLOYEE_SOFTWARE_SUBMIT_ROLES = frozenset({"employee", "master_admin"})
_EMPLOYEE_SOFTWARE_REFUSAL = (
    "Employee software requests can only be submitted by the employee who needs the software "
    "(or Master Admin on their behalf). Finance, IT, and HR cannot raise this workflow type — "
    "the employee submits from Software Request, then line manager → Finance (budget) → IT."
)
_LICENCE_PRODUCT_RE = re.compile(
    r"\b(?:assign|give|allocate|grant|provide)\b.{0,24}\b([a-z0-9][a-z0-9\s'\-]{1,40}?)\s+licen[cs]e\b",
    re.IGNORECASE,
)
_ASSIGN_TO_PERSON_RE = re.compile(
    r"\bto\s+([A-Za-z0-9][A-Za-z0-9\s'\-\.]+?)(?:\s*$|\s*[\.\,]|\s+for\s+|\s+with\s+)",
    re.IGNORECASE,
)
_EMPLOYEE_LOOKUP_QUESTION_RE = re.compile(
    r"\b(full name|work email|exact name|provide.*email|could you please provide|which employee|who should receive)\b",
    re.IGNORECASE,
)
_LICENCE_WORKFLOW_SUBMIT_ROLES = frozenset({"it_admin", "hr_admin", "master_admin"})
_FINANCE_LICENCE_REFUSAL = (
    "Finance cannot assign licences or submit licence assignment workflows. "
    "Ask IT Admin or HR to submit the request — Finance approves budget when the workflow reaches you."
)


def _is_uuid(value: Any) -> bool:
    if value is None:
        return False
    return bool(_UUID_RE.match(str(value).strip()))


def _instruction_wants_bulk_revoke(instruction: str) -> bool:
    text = instruction or ""
    return bool(BULK_REVOKE_INTENT_RE.search(text) or BULK_REVOKE_INTENT_ALT_RE.search(text))


def _extract_revoke_count(instruction: str, *, default: int = 5) -> int:
    for match in COUNT_IN_INSTRUCTION_RE.finditer(instruction or ""):
        raw = match.group(1) or match.group(2)
        if raw:
            try:
                return max(1, min(int(raw), 50))
            except ValueError:
                continue
    return default


def _licence_order_for_instruction(instruction: str) -> str:
    text = instruction or ""
    if RECENT_ASSIGNMENT_RE.search(text) or re.search(r"\btop\s+\d+\b", text, re.IGNORECASE):
        return "assigned_at_desc"
    if re.search(r"\b(newest|recently created|latest created)\b", text, re.IGNORECASE):
        return "created_at_desc"
    return "assigned_at_desc"


def _is_resolvable_id_question(question: str) -> bool:
    return bool(LICENCE_ID_QUESTION_RE.search(question or ""))


def _plan_has_valid_revoke_steps(steps: list[Any]) -> bool:
    revoke_steps = [step for step in steps if isinstance(step, dict) and step.get("tool") == "revoke_licence"]
    if not revoke_steps:
        return False
    for step in revoke_steps:
        licence_id = (step.get("params") or {}).get("licence_id")
        if not _is_uuid(licence_id):
            return False
    return True


def resolve_entity_lookup_gaps(
    conn,
    plan: dict[str, Any],
    instruction: str,
    organisation_id: UUID,
    actor: dict[str, Any],
) -> dict[str, Any]:
    """
    When the user asks to revoke/remove N licences, query get_licences and build revoke steps.
    Strips questions that ask for licence UUIDs the platform already has.
    """
    result = dict(plan)
    steps = list(result.get("steps") or [])
    warnings = list(result.get("warnings") or [])
    questions = list(result.get("questions") or [])
    meta = dict(result.get("_meta") or {})

    if not _instruction_wants_bulk_revoke(instruction):
        return result

    if _plan_has_valid_revoke_steps(steps):
        questions = [q for q in questions if not _is_resolvable_id_question(q)]
        result["questions"] = questions
        return result

    from app.tools import get_licences

    count = _extract_revoke_count(instruction)
    order_by = _licence_order_for_instruction(instruction)
    lookup = get_licences(
        conn,
        organisation_id,
        actor,
        order_by=order_by,
        limit=count,
        include_assignee=True,
    )
    if lookup.get("error"):
        warnings.append(str(lookup["error"]))
        result["warnings"] = warnings
        return result

    licences = lookup.get("licences") or []
    if not licences:
        warnings.append("No active licences found in this organisation to revoke.")
        questions = [q for q in questions if not _is_resolvable_id_question(q)]
        result["steps"] = []
        result["questions"] = questions
        result["warnings"] = warnings
        return result

    new_steps: list[dict[str, Any]] = []
    for lic in licences[:count]:
        name = lic.get("licence_name") or lic.get("id")
        assignee = lic.get("assignee_name") or lic.get("assignee_email") or "unassigned"
        new_steps.append(
            {
                "tool": "revoke_licence",
                "params": {"licence_id": str(lic["id"])},
                "description": f"Revoke licence '{name}' ({assignee})",
            }
        )

    removed_questions = [q for q in questions if _is_resolvable_id_question(q)]
    questions = [q for q in questions if not _is_resolvable_id_question(q)]

    sort_label = order_by.replace("_", " ")
    warnings.append(
        f"Auto-selected {len(new_steps)} licence(s) from org data (sorted by {sort_label}): "
        + ", ".join(str(lic.get("licence_name") or lic.get("id")) for lic in licences[:count])
        + "."
    )
    if removed_questions:
        meta["dropped_id_questions"] = removed_questions

    result["steps"] = new_steps
    result["questions"] = questions
    result["warnings"] = warnings
    result["_meta"] = meta
    meta["entity_resolver"] = {"licences_selected": len(new_steps), "order_by": order_by}
    return result


_BUDGET_YEAR_RE = re.compile(r"\b(?:year|fiscal\s+year|fy)\s*(20\d{2})\b", re.IGNORECASE)
_BUDGET_YEAR_FALLBACK_RE = re.compile(r"\b(20\d{2})\b")
_BUDGET_AMOUNT_RE = re.compile(r"(\d[\d,]*)\s*(AED|USD|GBP|EUR|INR)?", re.IGNORECASE)
_BUDGET_DEPT_PATTERNS = (
    re.compile(r"([\w\s]+?)\s+department\s+budget", re.IGNORECASE),
    re.compile(r"budget\s+for\s+(?:the\s+)?([\w\s]+?)\s+department", re.IGNORECASE),
    re.compile(r"([\w\s]+?)\s+department\s+(?:budget|allocation)", re.IGNORECASE),
)
_BUDGET_CREATE_INTENT_RE = re.compile(
    r"\b(create|add|set|allocate|new|update)\b.{0,48}\bbudget\b|\bbudget\b.{0,48}\b(create|add|allocate|set)\b|\bdepartment\s+budget\b",
    re.IGNORECASE,
)

_BUDGET_PERMISSION_WARNING = (
    "Changing department budgets requires Finance, Auditor, or Master Admin access. "
    "Your role cannot create or update budgets, and SLMCT has no budget allocation workflow — "
    "ask your Finance team to adjust this on the Budgets tab."
)


def _budget_params_from_payload_and_instruction(payload: dict[str, Any], instruction: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key in ("fiscal_year", "department", "allocated_amount", "currency_code", "notes", "status"):
        value = payload.get(key)
        if value not in (None, ""):
            params[key] = value
    if "allocated_amount" not in params and payload.get("amount") not in (None, ""):
        params["allocated_amount"] = payload.get("amount")

    text = instruction or ""

    if "fiscal_year" not in params:
        year_match = _BUDGET_YEAR_RE.search(text) or _BUDGET_YEAR_FALLBACK_RE.search(text)
        if year_match:
            params["fiscal_year"] = int(year_match.group(1))

    if "allocated_amount" not in params:
        amount_match = _BUDGET_AMOUNT_RE.search(text)
        if amount_match:
            params["allocated_amount"] = float(amount_match.group(1).replace(",", ""))
            if amount_match.group(2) and "currency_code" not in params:
                params["currency_code"] = amount_match.group(2).upper()

    if "department" not in params:
        for pattern in _BUDGET_DEPT_PATTERNS:
            dept_match = pattern.search(text)
            if dept_match:
                params["department"] = dept_match.group(1).strip()
                break

    if "currency_code" not in params:
        params["currency_code"] = "AED"

    return params


def normalize_budget_direct_create(
    plan: dict[str, Any],
    write_tools: list[Any],
    instruction: str,
) -> dict[str, Any]:
    """Convert mistaken budget workflow submits into direct create_budget steps."""
    write_by_name = {tool.name: tool for tool in write_tools}
    if "create_budget" not in write_by_name:
        return plan

    steps = plan.get("steps") or []
    if not steps:
        return plan

    new_steps: list[Any] = []
    changed = False
    for step in steps:
        if not isinstance(step, dict) or step.get("tool") != "submit_workflow_request":
            new_steps.append(step)
            continue
        params = step.get("params") or {}
        if str(params.get("requested_module") or "").strip() != "budgets":
            new_steps.append(step)
            continue

        payload = dict(params.get("payload") or {})
        budget_params = _budget_params_from_payload_and_instruction(payload, instruction)
        required = {"fiscal_year", "department", "allocated_amount"}
        if not required.issubset(budget_params.keys()):
            new_steps.append(step)
            continue

        dept = budget_params.get("department")
        year = budget_params.get("fiscal_year")
        new_steps.append(
            {
                "tool": "create_budget",
                "params": budget_params,
                "description": step.get("description")
                or f"Create {dept} department budget for fiscal year {year}",
            }
        )
        changed = True

    if not changed:
        return plan

    result = dict(plan)
    result["steps"] = new_steps
    warnings = list(result.get("warnings") or [])
    warnings.append(
        "Converted budget workflow submit to direct create_budget — department is a budget label, not a new entity."
    )
    result["warnings"] = warnings
    return result


def instruction_wants_budget_mutation(instruction: str) -> bool:
    """True when the user is asking to create, increase, or change a department budget."""
    return bool(_BUDGET_CREATE_INTENT_RE.search(instruction or ""))


def _instruction_wants_budget_create(instruction: str) -> bool:
    return instruction_wants_budget_mutation(instruction)


def resolve_budget_permission_gaps(
    plan: dict[str, Any],
    write_tools: list[Any],
    instruction: str,
) -> dict[str, Any]:
    """Refuse budget-create plans when the actor lacks create_budget — never fall back to workflow."""
    if not _instruction_wants_budget_create(instruction):
        return plan

    write_by_name = {tool.name: tool for tool in write_tools}
    if "create_budget" in write_by_name:
        return plan

    result = dict(plan)
    result["steps"] = []
    result["questions"] = []
    warnings = list(result.get("warnings") or [])
    if _BUDGET_PERMISSION_WARNING not in warnings:
        warnings.append(_BUDGET_PERMISSION_WARNING)
    result["warnings"] = warnings
    meta = dict(result.get("_meta") or {})
    meta["budget_permission_refusal"] = True
    result["_meta"] = meta
    return result


def instruction_wants_licence_assignment(instruction: str) -> bool:
    if instruction_wants_employee_software_request(instruction):
        return False
    return bool(LICENCE_ASSIGN_INTENT_RE.search(instruction or ""))


def instruction_wants_employee_software_request(instruction: str) -> bool:
    return bool(EMPLOYEE_SOFTWARE_REQUEST_RE.search(instruction or ""))


def _extract_software_product_hint(instruction: str) -> str:
    match = _ESR_PRODUCT_RE.search(instruction or "")
    if match:
        hint = match.group(1).strip()
    else:
        hint = ""
    if not hint:
        return ""
    hint = re.sub(r"\s+seat\s*$", "", hint, flags=re.IGNORECASE).strip()
    hint = re.sub(r"\s+licen[cs]e\s*$", "", hint, flags=re.IGNORECASE).strip()
    return hint


def _actor_roles_set(actor: dict[str, Any]) -> set[str]:
    return {str(role).strip().lower() for role in (actor.get("roles") or []) if str(role).strip()}


def _extract_licence_product_hint(instruction: str) -> str:
    match = _LICENCE_PRODUCT_RE.search(instruction or "")
    if match:
        return match.group(1).strip()
    return ""


def _extract_assignee_name(instruction: str) -> str:
    match = _ASSIGN_TO_PERSON_RE.search(instruction or "")
    if match:
        return match.group(1).strip()
    return ""


def _is_resolvable_employee_question(question: str) -> bool:
    return bool(_EMPLOYEE_LOOKUP_QUESTION_RE.search(question or ""))


def _lookup_employee_by_name(conn, organisation_id: UUID, actor: dict[str, Any], name: str) -> dict[str, Any] | None:
    from app.tools import get_module_record

    if not name.strip():
        return None
    result = get_module_record(conn, organisation_id, actor, module="employees", name=name.strip())
    if result.get("error"):
        return None
    if result.get("record"):
        return dict(result["record"])
    records = result.get("records") or []
    if len(records) == 1:
        return dict(records[0])
    target = name.strip().lower()
    for row in records:
        full_name = str(row.get("full_name") or "").strip().lower()
        if full_name == target:
            return dict(row)
    return None


def _lookup_subscription_for_hint(conn, organisation_id: UUID, actor: dict[str, Any], hint: str) -> dict[str, Any] | None:
    from app.tools import get_subscriptions

    if not hint.strip():
        return None
    result = get_subscriptions(conn, organisation_id, actor, name=hint.strip(), status="active", limit=20)
    subs = result.get("subscriptions") or []
    if subs:
        return dict(subs[0])
    result = get_subscriptions(conn, organisation_id, actor, name=hint.strip(), limit=20)
    subs = result.get("subscriptions") or []
    return dict(subs[0]) if subs else None


def _lookup_licence_name_hint(conn, organisation_id: UUID, actor: dict[str, Any], hint: str) -> str | None:
    from app.tools import get_licences

    if not hint.strip():
        return None
    result = get_licences(
        conn,
        organisation_id,
        actor,
        licence_name=hint.strip(),
        status="available",
        limit=5,
    )
    licences = result.get("licences") or []
    if licences:
        return str(licences[0].get("licence_name") or "").strip() or None
    return None


def _normalize_licence_workflow_payload(payload: dict[str, Any], employee: dict[str, Any] | None) -> dict[str, Any]:
    normalized = dict(payload or {})
    if employee:
        person_id = str(employee.get("id") or "")
        if person_id and not normalized.get("assigned_to_person_id"):
            normalized["assigned_to_person_id"] = person_id
        for legacy_key in ("employee_id", "person_id", "assignee_id"):
            legacy_val = normalized.get(legacy_key)
            if legacy_val and not normalized.get("assigned_to_person_id"):
                normalized["assigned_to_person_id"] = str(legacy_val)
            normalized.pop(legacy_key, None)
        if employee.get("department") and not normalized.get("department"):
            normalized["department"] = employee.get("department")
    normalized.pop("licence_id", None)
    return normalized


def _licence_workflow_step(
    payload: dict[str, Any],
    *,
    description: str,
) -> dict[str, Any]:
    return {
        "tool": "submit_workflow_request",
        "params": {
            "workflow_type": "license_assignment_request",
            "requested_module": "licences",
            "payload": payload,
        },
        "description": description,
    }


def _plan_has_valid_licence_workflow(steps: list[Any], employee: dict[str, Any] | None) -> bool:
    for step in steps:
        if not isinstance(step, dict) or step.get("tool") != "submit_workflow_request":
            continue
        params = step.get("params") or {}
        if str(params.get("requested_module") or "").strip() != "licences":
            continue
        if str(params.get("workflow_type") or "").strip() not in {
            "license_assignment_request",
            "hr_onboarding_request",
        }:
            continue
        payload = _normalize_licence_workflow_payload(dict(params.get("payload") or {}), employee)
        if not _is_uuid(payload.get("assigned_to_person_id")):
            continue
        if not str(payload.get("licence_name") or "").strip():
            continue
        return True
    return False


def resolve_licence_assignment_gaps(
    conn,
    plan: dict[str, Any],
    instruction: str,
    organisation_id: UUID,
    actor: dict[str, Any],
    write_tools: list[Any],
) -> dict[str, Any]:
    """
    IT/HR licence assignment: resolve employee + subscription from org data and build
    license_assignment_request workflow steps. Refuse finance-only actors.
    """
    if not instruction_wants_licence_assignment(instruction):
        return plan

    write_by_name = {tool.name: tool for tool in write_tools}
    roles = _actor_roles_set(actor)

    if "assign_licence" in write_by_name:
        return plan

    if roles.intersection({"finance", "auditor"}) and not roles.intersection(_LICENCE_WORKFLOW_SUBMIT_ROLES):
        result = dict(plan)
        result["steps"] = []
        result["questions"] = []
        warnings = list(result.get("warnings") or [])
        if _FINANCE_LICENCE_REFUSAL not in warnings:
            warnings.append(_FINANCE_LICENCE_REFUSAL)
        result["warnings"] = warnings
        return result

    if "submit_workflow_request" not in write_by_name:
        return plan

    if not roles.intersection(_LICENCE_WORKFLOW_SUBMIT_ROLES):
        return plan

    result = dict(plan)
    steps = list(result.get("steps") or [])
    warnings = list(result.get("warnings") or [])
    questions = list(result.get("questions") or [])
    meta = dict(result.get("_meta") or {})

    assignee_name = _extract_assignee_name(instruction)
    product_hint = _extract_licence_product_hint(instruction)
    employee = _lookup_employee_by_name(conn, organisation_id, actor, assignee_name) if assignee_name else None

    if _plan_has_valid_licence_workflow(steps, employee):
        new_steps = []
        changed = False
        for step in steps:
            if not isinstance(step, dict) or step.get("tool") != "submit_workflow_request":
                new_steps.append(step)
                continue
            params = dict(step.get("params") or {})
            if str(params.get("requested_module") or "").strip() != "licences":
                new_steps.append(step)
                continue
            payload = _normalize_licence_workflow_payload(dict(params.get("payload") or {}), employee)
            params["workflow_type"] = params.get("workflow_type") or "license_assignment_request"
            params["payload"] = payload
            new_steps.append({**step, "params": params})
            changed = True
        if changed:
            result["steps"] = new_steps
            warnings.append("Normalized licence assignment workflow payload field names from org lookup.")
            result["warnings"] = warnings
        questions = [q for q in questions if not _is_resolvable_employee_question(q)]
        result["questions"] = questions
        return result

    if not employee:
        return result

    subscription = _lookup_subscription_for_hint(conn, organisation_id, actor, product_hint)
    licence_name = _lookup_licence_name_hint(conn, organisation_id, actor, product_hint)
    if not licence_name:
        if subscription and subscription.get("name"):
            licence_name = f"{subscription['name']} Seat"
        elif product_hint:
            licence_name = f"{product_hint.title()} Seat"

    payload: dict[str, Any] = {
        "assigned_to_person_id": str(employee["id"]),
        "licence_name": licence_name or "Licence Seat",
    }
    if employee.get("department"):
        payload["department"] = employee["department"]
    if subscription:
        payload["subscription_id"] = str(subscription["id"])
        if subscription.get("amount") is not None:
            payload["amount"] = subscription["amount"]
        if subscription.get("currency_code"):
            payload["currency_code"] = subscription["currency_code"]

    removed_questions = [q for q in questions if _is_resolvable_employee_question(q)]
    questions = [q for q in questions if not _is_resolvable_employee_question(q)]

    emp_label = employee.get("full_name") or assignee_name
    product_label = product_hint or licence_name or "licence"
    new_step = _licence_workflow_step(
        payload,
        description=f"Submit licence assignment workflow — assign {product_label} to {emp_label}",
    )

    result["steps"] = [new_step]
    result["questions"] = questions
    warnings.append(
        f"Resolved assignee '{emp_label}' from org data"
        + (f" and matched subscription '{subscription.get('name')}'" if subscription else "")
        + " — submitting license_assignment_request for Finance approval then IT completion."
    )
    if not subscription:
        warnings.append(
            "No active Figma/subscription match found in org data — workflow payload may need subscription_id before Finance can validate budget."
        )
    if removed_questions:
        meta["dropped_employee_questions"] = removed_questions
    meta["licence_assignment_resolver"] = {
        "employee_id": str(employee["id"]),
        "subscription_id": str(subscription["id"]) if subscription else None,
    }
    result["warnings"] = warnings
    result["_meta"] = meta
    return result


def _lookup_actor_employee(conn, organisation_id: UUID, actor: dict[str, Any]) -> dict[str, Any] | None:
    email = str(actor.get("email") or "").strip().lower()
    if not email:
        return None
    from app.tools import get_employees

    result = get_employees(conn, organisation_id, actor, status="active", limit=500)
    for row in result.get("employees") or []:
        if str(row.get("work_email") or "").strip().lower() == email:
            return dict(row)
    return None


def _lookup_catalogue_product(
    conn, organisation_id: UUID, actor: dict[str, Any], hint: str
) -> dict[str, Any] | None:
    from app.tools import get_vendor_catalogue

    if not hint.strip():
        return None
    searches = [hint.strip()]
    if " " in hint.strip():
        searches.append(hint.strip().split()[0])
    for search in searches:
        result = get_vendor_catalogue(conn, organisation_id, actor, name=search, limit=5)
        items = result.get("catalogue_items") or []
        if items:
            return dict(items[0])
    return None


def _employee_software_workflow_step(payload: dict[str, Any], *, description: str) -> dict[str, Any]:
    return {
        "tool": "submit_workflow_request",
        "params": {
            "workflow_type": "employee_software_request",
            "requested_module": "subscriptions",
            "payload": payload,
        },
        "description": description,
    }


def _plan_has_valid_employee_software_request(steps: list[Any]) -> bool:
    for step in steps:
        if not isinstance(step, dict) or step.get("tool") != "submit_workflow_request":
            continue
        params = step.get("params") or {}
        if str(params.get("workflow_type") or "").strip() != "employee_software_request":
            continue
        if str(params.get("requested_module") or "").strip() != "subscriptions":
            continue
        payload = params.get("payload") or {}
        if str(payload.get("name") or "").strip():
            return True
    return False


def resolve_employee_software_request_gaps(
    conn,
    plan: dict[str, Any],
    instruction: str,
    organisation_id: UUID,
    actor: dict[str, Any],
    write_tools: list[Any],
) -> dict[str, Any]:
    """
    Employee software requests use workflow_type employee_software_request on subscriptions —
    NOT license_assignment_request (that is for IT/HR assigning spare seats).
    """
    if not instruction_wants_employee_software_request(instruction):
        return plan

    write_by_name = {tool.name: tool for tool in write_tools}
    roles = _actor_roles_set(actor)

    if "submit_workflow_request" not in write_by_name:
        return plan

    result = dict(plan)
    warnings = list(result.get("warnings") or [])
    questions = list(result.get("questions") or [])
    meta = dict(result.get("_meta") or {})

    if not roles.intersection(_EMPLOYEE_SOFTWARE_SUBMIT_ROLES):
        result["steps"] = []
        result["questions"] = []
        if _EMPLOYEE_SOFTWARE_REFUSAL not in warnings:
            warnings.append(_EMPLOYEE_SOFTWARE_REFUSAL)
        result["warnings"] = warnings
        return result

    steps = list(result.get("steps") or [])
    if _plan_has_valid_employee_software_request(steps):
        normalized_steps = []
        for step in steps:
            if not isinstance(step, dict) or step.get("tool") != "submit_workflow_request":
                normalized_steps.append(step)
                continue
            params = dict(step.get("params") or {})
            if str(params.get("workflow_type") or "").strip() != "employee_software_request":
                normalized_steps.append(step)
                continue
            params["workflow_type"] = "employee_software_request"
            params["requested_module"] = "subscriptions"
            normalized_steps.append({**step, "params": params})
        result["steps"] = normalized_steps
        return result

    product_hint = _extract_software_product_hint(instruction)
    catalogue = _lookup_catalogue_product(conn, organisation_id, actor, product_hint) if product_hint else None
    employee = _lookup_actor_employee(conn, organisation_id, actor)

    product_name = (catalogue or {}).get("name") or product_hint or "Software Request"
    payload: dict[str, Any] = {
        "name": product_name,
        "justification": f"Employee software request via AI Operator: {product_name}",
        "billing_cycle": "monthly",
        "currency_code": "AED",
    }
    if catalogue:
        if catalogue.get("price") is not None:
            payload["amount"] = catalogue["price"]
        if catalogue.get("currency_code"):
            payload["currency_code"] = catalogue["currency_code"]
    if employee and employee.get("department"):
        payload["department"] = employee["department"]

    product_label = payload["name"]
    description = f"Submit employee software request for {product_label}"
    result["steps"] = [_employee_software_workflow_step(payload, description=description)]
    result["questions"] = [q for q in questions if not _is_resolvable_employee_question(q)]
    warnings.append(
        f"Using employee_software_request (subscriptions module) — line manager, then Finance, then IT. "
        f"Not licence assignment (IT/HR only)."
    )
    if catalogue:
        warnings.append(f"Matched catalogue item '{catalogue.get('name')}' for pricing.")
    elif product_hint:
        warnings.append(
            f"No catalogue match for '{product_hint}' — submit with stated name; Finance may ask for amount."
        )
    meta["employee_software_request_resolver"] = {
        "product_hint": product_hint,
        "catalogue_id": str(catalogue["id"]) if catalogue and catalogue.get("id") else None,
    }
    result["warnings"] = warnings
    result["_meta"] = meta
    return result
