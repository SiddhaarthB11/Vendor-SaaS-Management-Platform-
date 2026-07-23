"""AI Operator — planner, executor, and verifier."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from google.genai import types as gtypes
from psycopg import Connection
from psycopg.types.json import Json

from app.llm import complete, extract_function_calls
from app.operator_autofill import autofill_plan_gaps, is_placeholder_value, is_step_ref
from app.operator_entity_resolver import (
    normalize_budget_direct_create,
    resolve_budget_permission_gaps,
    resolve_employee_software_request_gaps,
    resolve_entity_lookup_gaps,
    resolve_licence_assignment_gaps,
)
from app.prompts import format_prompt
from app.tools import Tool, execute_tool, get_tool_by_name, get_toolset, get_write_toolset, toolset_to_gemini_tools

MAX_PLAN_ITERATIONS = 8
PLAN_MAX_AGE = timedelta(minutes=10)

APPROVAL_FORBIDDEN = frozenset(
    {
        "approve",
        "reject",
        "line_manager_approve",
        "master_approve",
        "finance_approve",
        "procurement_complete",
    }
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_STEP_REF_RE = re.compile(r"^\$step:(\d+):([\w.]+)$", re.IGNORECASE)
_DIRECT_CREATE_RE = re.compile(
    r"\b(directly|direct|straight into|skip approval|without approval|into the tab|add it to the tab)\b",
    re.IGNORECASE,
)
_ID_PARAM_SUFFIX = "_id"
_DIRECT_CREATE_BY_MODULE = {
    "subscriptions": "create_subscription",
    "vendors": "create_vendor",
    "budgets": "create_budget",
}
# Modules where finance/master roles always use direct write tools — never approval workflows.
_ALWAYS_DIRECT_MODULES = frozenset({"budgets"})
# Budget/payment rows are finance tab actions — not operator workflow submits (no such approval chain).
_NO_OPERATOR_WORKFLOW_MODULES = frozenset({"budgets", "payments"})


@dataclass
class OperatorPlanRequest:
    instruction: str
    organisation_id: str
    actor_roles: list[str]
    actor_user_id: str | None = None
    actor_email: str | None = None
    answers: dict[str, str] | None = None
    answer_fields: dict[str, str] | None = None


@dataclass
class OperatorExecuteRequest:
    plan_id: str
    confirmed: bool
    organisation_id: str | None = None
    actor_user_id: str | None = None
    actor_roles: list[str] | None = None
    sabotage_verifier: bool = False


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _record_row(row: dict | None) -> dict[str, Any]:
    if not row:
        return {}
    return {key: _json_safe(value) for key, value in row.items()}


def _resolve_actor_email(conn: Connection, actor_user_id: str | None, actor_email: str | None) -> str | None:
    if actor_email:
        return actor_email
    if not actor_user_id:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT email::text AS email FROM slmct.app_users WHERE id = %s LIMIT 1",
            (actor_user_id,),
        )
        row = cur.fetchone()
    return str(row["email"]) if row and row.get("email") else None


def _write_tools_spec(write_tools: list[Tool]) -> str:
    from app.entity_field_guide import WRITE_TOOL_RECOMMENDED

    lines: list[str] = []
    for tool in write_tools:
        required = tool.parameters.get("required") or []
        req = ", ".join(required) if required else "(none)"
        recommended = WRITE_TOOL_RECOMMENDED.get(tool.name) or []
        rec = "; ".join(recommended) if recommended else "(see get_entity_field_guide)"
        lines.append(f"- {tool.name}: required [{req}] | recommended: {rec}")
    return "\n".join(lines)


def _parse_plan_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Empty planner response")
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\n?```$", "", raw)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Planner output must be a JSON object")
    data.setdefault("steps", [])
    data.setdefault("questions", [])
    data.setdefault("warnings", [])
    if not isinstance(data["steps"], list):
        raise ValueError("steps must be a list")
    if not isinstance(data["questions"], list):
        raise ValueError("questions must be a list")
    if not isinstance(data["warnings"], list):
        raise ValueError("warnings must be a list")
    return data


def _workflow_modules() -> frozenset[str]:
    from app.main import WORKFLOW_MODULES

    return WORKFLOW_MODULES


def _is_uuid(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return bool(_UUID_RE.match(text))


def _is_step_ref(value: Any) -> bool:
    if value is None:
        return False
    return bool(_STEP_REF_RE.match(str(value).strip()))


def _step_ref_index(value: Any) -> int | None:
    match = _STEP_REF_RE.match(str(value).strip())
    if not match:
        return None
    return int(match.group(1))


def _resolve_path(obj: Any, path: str) -> Any:
    current = obj
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _resolve_step_refs(value: Any, step_results: list[dict[str, Any]]) -> Any:
    if isinstance(value, str):
        match = _STEP_REF_RE.match(value.strip())
        if not match:
            return value
        index = int(match.group(1))
        path = match.group(2)
        if index < 0 or index >= len(step_results):
            raise ValueError(f"Step reference {value!r} points to missing step {index}")
        step = step_results[index]
        if step.get("status") != "passed":
            raise ValueError(f"Step reference {value!r} requires step {index} to have passed")
        resolved = _resolve_path(step.get("result") or {}, path)
        if resolved in (None, ""):
            raise ValueError(f"Step reference {value!r} could not be resolved")
        return resolved
    if isinstance(value, dict):
        return {key: _resolve_step_refs(item, step_results) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_step_refs(item, step_results) for item in value]
    return value


def _iter_id_params(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if not isinstance(value, dict):
        return found
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if str(key).endswith(_ID_PARAM_SUFFIX) or str(key) == "request_id":
            found.append((path, item))
        if isinstance(item, dict):
            found.extend(_iter_id_params(item, path))
    return found


def merge_operator_instruction(
    instruction: str,
    *,
    answers: dict[str, str] | None = None,
    answer_fields: dict[str, str] | None = None,
) -> str:
    """Combine base instruction with structured Q&A from the UI."""
    parts = [instruction.strip()]
    if answer_fields:
        field_lines = [f"{key}: {value.strip()}" for key, value in answer_fields.items() if str(value or "").strip()]
        if field_lines:
            parts.append("Structured details:\n" + "\n".join(field_lines))
    if answers:
        answer_lines = [
            f'Answer to "{question}": {answer.strip()}'
            for question, answer in answers.items()
            if str(question or "").strip() and str(answer or "").strip()
        ]
        if answer_lines:
            parts.append("\n".join(answer_lines))
    return "\n\n".join(part for part in parts if part)


def validate_plan_steps(
    plan: dict[str, Any],
    write_tools: list[Tool],
    *,
    instruction: str | None = None,
    actor_roles: list[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    write_by_name = {tool.name: tool for tool in write_tools}
    roles = {str(role).strip().lower() for role in (actor_roles or [])}
    wants_direct = bool(instruction and _DIRECT_CREATE_RE.search(instruction))
    is_master = "master_admin" in roles
    workflow_modules = _workflow_modules()
    steps = plan.get("steps") or []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step {index + 1}: must be an object")
            continue
        tool_name = str(step.get("tool") or "").strip()
        if not tool_name:
            errors.append(f"step {index + 1}: missing tool name")
            continue
        if any(token in tool_name.lower() for token in APPROVAL_FORBIDDEN):
            errors.append(f"step {index + 1}: approval actions are forbidden")
            continue
        tool = write_by_name.get(tool_name)
        if tool is None:
            errors.append(f"step {index + 1}: tool '{tool_name}' is not in the actor's write toolset")
            continue
        params = step.get("params") or {}
        if not isinstance(params, dict):
            errors.append(f"step {index + 1}: params must be an object")
            continue
        required = tool.parameters.get("required") or []
        for field in required:
            if field not in params or params[field] in (None, ""):
                errors.append(f"step {index + 1}: missing required param '{field}' for {tool_name}")
            elif is_placeholder_value(params[field]):
                errors.append(
                    f"step {index + 1}: param '{field}' is an unresolved placeholder — lookup or replan required"
                )
            elif field in {"price", "amount", "allocated_amount"} and isinstance(params.get(field), str):
                try:
                    float(str(params[field]).replace(",", ""))
                except ValueError:
                    errors.append(
                        f"step {index + 1}: param '{field}' must be numeric, got {params[field]!r}"
                    )
        if not str(step.get("description") or "").strip():
            errors.append(f"step {index + 1}: missing description")

        for path, id_value in _iter_id_params(params):
            if id_value in (None, ""):
                continue
            if is_placeholder_value(id_value):
                errors.append(
                    f"step {index + 1}: {path} contains unresolved placeholder {id_value!r} — use lookup tools or concrete values"
                )
                continue
            if _is_step_ref(id_value):
                ref_index = _step_ref_index(id_value)
                if ref_index is None or ref_index >= index:
                    errors.append(
                        f"step {index + 1}: {path} step reference must point to an earlier step, got {id_value!r}"
                    )
                continue
            if not _is_uuid(id_value):
                errors.append(
                    f"step {index + 1}: {path} must be a UUID from a read-tool lookup, got {id_value!r}"
                )

        if tool_name == "submit_workflow_request":
            requested_module = str(params.get("requested_module") or "").strip()
            if requested_module and requested_module not in workflow_modules:
                hint = ""
                if requested_module.rstrip("s") + "s" in workflow_modules:
                    hint = f" (did you mean '{requested_module.rstrip('s')}s'?)"
                errors.append(
                    f"step {index + 1}: requested_module '{requested_module}' is unsupported{hint}; "
                    f"use one of: {', '.join(sorted(workflow_modules))}"
                )
            requested_action = str(params.get("requested_action") or "create").strip().lower()
            if requested_module in _NO_OPERATOR_WORKFLOW_MODULES:
                errors.append(
                    f"step {index + 1}: {requested_module} is not submitted via workflow — "
                    "budget/payment allocation is a Finance-tab action (create_budget for finance, auditor, "
                    "or master_admin only); other roles cannot create budgets and there is no budget workflow"
                )
            if requested_module == "licences":
                roles = {str(role).strip().lower() for role in (actor_roles or [])}
                can_submit_licence_wf = bool(roles.intersection({"it_admin", "hr_admin", "master_admin"}))
                if not can_submit_licence_wf and roles.intersection({"finance", "auditor", "employee", "line_manager"}):
                    errors.append(
                        f"step {index + 1}: finance and other non-IT/HR roles cannot submit licence assignment "
                        "workflows — ask IT Admin or HR to submit; Finance only validates budget"
                    )
                payload = params.get("payload") or {}
                if isinstance(payload, dict):
                    if payload.get("employee_id") and not payload.get("assigned_to_person_id"):
                        errors.append(
                            f"step {index + 1}: use payload.assigned_to_person_id (not employee_id) for licence workflows"
                        )
                    if payload.get("licence_id"):
                        errors.append(
                            f"step {index + 1}: licence workflows create a licence on completion — "
                            "do not pass licence_id; use subscription_id + licence_name + assigned_to_person_id"
                        )
                    if not str(payload.get("licence_name") or "").strip():
                        errors.append(f"step {index + 1}: licence workflow payload requires licence_name")
                    if not payload.get("assigned_to_person_id"):
                        errors.append(
                            f"step {index + 1}: licence workflow payload requires assigned_to_person_id "
                            "(resolve via get_employees / get_module_record during planning)"
                        )
            if requested_module == "subscriptions":
                wf_type = str(params.get("workflow_type") or "").strip()
                roles = {str(role).strip().lower() for role in (actor_roles or [])}
                if wf_type == "employee_software_request" and not roles.intersection({"employee", "master_admin"}):
                    errors.append(
                        f"step {index + 1}: employee_software_request can only be submitted by the employee "
                        "who needs the software (or master admin) — not Finance/IT/HR; use Software Request in the app"
                    )
                payload = params.get("payload") or {}
                if wf_type == "employee_software_request" and isinstance(payload, dict):
                    if not str(payload.get("name") or "").strip():
                        errors.append(f"step {index + 1}: employee software request payload requires name (software/product)")
            direct_tool = _DIRECT_CREATE_BY_MODULE.get(requested_module)
            if direct_tool and direct_tool in write_by_name and requested_action == "create":
                force_direct = requested_module in _ALWAYS_DIRECT_MODULES
                keyword_direct = wants_direct and is_master and requested_module in ("subscriptions", "vendors")
                if force_direct or keyword_direct:
                    reason = (
                        "budget allocation belongs on the Finance tab — department is a label on the budget row, "
                        "not a new org entity"
                        if force_direct
                        else "instruction requests direct create"
                    )
                    errors.append(
                        f"step {index + 1}: {reason} — use {direct_tool}, not submit_workflow_request"
                    )
    return errors


def _build_actor(actor_roles: list[str], actor_user_id: str | None, actor_email: str | None) -> dict[str, Any]:
    return {
        "roles": list(actor_roles or []),
        "user_id": actor_user_id,
        "email": actor_email,
    }


def _model_turn_content(response: Any) -> gtypes.Content | None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    return getattr(candidates[0], "content", None)


def _read_tool_args_contain_step_ref(value: Any, path: str = "") -> str | None:
    if isinstance(value, str) and is_step_ref(value):
        return path or "argument"
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            found = _read_tool_args_contain_step_ref(item, child)
            if found:
                return found
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _read_tool_args_contain_step_ref(item, f"{path}[{index}]")
            if found:
                return found
    return None


def _planner_tool_loop(
    conn: Connection,
    *,
    instruction: str,
    organisation_id: str,
    actor: dict[str, Any],
    write_tools: list[Tool],
) -> dict[str, Any]:
    read_tools = get_toolset(actor.get("roles"))
    read_by_name = {tool.name: tool for tool in read_tools}
    gemini_tools = toolset_to_gemini_tools(read_tools)

    system_prompt = format_prompt(
        "operator_plan",
        write_tools_spec=_write_tools_spec(write_tools),
    )
    contents: list[gtypes.Content] = [
        gtypes.Content(
            role="user",
            parts=[gtypes.Part.from_text(text=f"Instruction:\n{instruction.strip()}")],
        )
    ]

    tool_calls_used: list[str] = []
    planner_model: str | None = None
    for iteration in range(MAX_PLAN_ITERATIONS):
        result = complete(
            "operator_plan",
            contents=contents,
            system_instruction=system_prompt,
            model=planner_model,
            temperature=0.1,
            tools=gemini_tools if gemini_tools else None,
            conn=conn,
            metadata={"phase": "operator_plan", "iteration": iteration, "organisation_id": organisation_id},
        )
        if planner_model is None:
            planner_model = result.model
        function_calls = result.function_calls or extract_function_calls(result.raw_response)
        if not function_calls:
            try:
                plan = _parse_plan_json(result.text or "")
                plan["_meta"] = {
                    "read_tools_used": tool_calls_used,
                    "planner_iterations": iteration + 1,
                    "planner_model": planner_model or result.model,
                }
                return plan
            except (json.JSONDecodeError, ValueError) as exc:
                if iteration >= MAX_PLAN_ITERATIONS - 1:
                    raise ValueError(f"Planner did not return valid JSON: {exc}") from exc
                contents.append(
                    gtypes.Content(
                        role="user",
                        parts=[
                            gtypes.Part.from_text(
                                text=(
                                    "Your last response was not valid JSON. "
                                    "Reply with ONLY the JSON plan object — no markdown, no prose."
                                )
                            )
                        ],
                    )
                )
                continue

        model_turn = _model_turn_content(result.raw_response)
        if model_turn is not None:
            contents.append(model_turn)

        response_parts: list[gtypes.Part] = []
        for fc in function_calls:
            name = str(fc.get("name") or "")
            tool_calls_used.append(name)
            args = fc.get("args") or {}
            if not isinstance(args, dict):
                try:
                    args = dict(args)
                except Exception:
                    args = {}
            tool = read_by_name.get(name)
            if tool is None:
                payload = {
                    "error": (
                        f"Tool '{name}' cannot be invoked during planning — use read/lookup tools only "
                        f"(get_vendors, lookup_vendor_autofill, get_entity_field_guide, etc.). "
                        f"Put write actions like {name!r} in the final plan JSON steps array."
                    )
                }
            else:
                bad_ref = _read_tool_args_contain_step_ref(args)
                if bad_ref:
                    payload = {
                        "error": (
                            f"{bad_ref} uses a step reference — those are only valid in write steps "
                            "of the final plan JSON (e.g. vendor_id: \"$step:0:vendor.id\" after create_vendor). "
                            "During research use get_vendors(name=...) or get_module_record instead."
                        )
                    }
                else:
                    try:
                        payload = execute_tool(tool, conn, organisation_id, actor, **args)
                    except Exception as exc:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        payload = {"error": str(exc)}
            response_parts.append(gtypes.Part.from_function_response(name=name, response={"result": payload}))
        contents.append(gtypes.Content(role="user", parts=response_parts))

    raise RuntimeError("Planner exceeded maximum read-tool iterations without returning JSON")


def _repair_plan_once(
    conn: Connection,
    *,
    instruction: str,
    organisation_id: str,
    draft_plan: dict[str, Any],
    validation_errors: list[str],
    write_tools: list[Tool],
) -> dict[str, Any]:
    system_prompt = format_prompt(
        "operator_plan",
        write_tools_spec=_write_tools_spec(write_tools),
    )
    repair_prompt = (
        f"Original instruction:\n{instruction.strip()}\n\n"
        f"Draft plan JSON:\n{json.dumps({k: draft_plan[k] for k in ('steps', 'questions', 'warnings') if k in draft_plan}, indent=2)}\n\n"
        f"Validation errors:\n- " + "\n- ".join(validation_errors) + "\n\n"
        "Fix the plan and return ONLY corrected JSON with keys steps, questions, warnings."
    )
    repair_model = (draft_plan.get("_meta") or {}).get("planner_model")
    result = complete(
        "operator_plan",
        contents=repair_prompt,
        system_instruction=system_prompt,
        model=str(repair_model) if repair_model else None,
        temperature=0.0,
        conn=conn,
        metadata={"phase": "operator_plan_repair", "organisation_id": organisation_id},
    )
    repaired = _parse_plan_json(result.text or "")
    meta = dict(draft_plan.get("_meta") or {})
    meta["repaired"] = True
    repaired["_meta"] = meta
    return repaired


def _store_plan(
    conn: Connection,
    *,
    instruction: str,
    organisation_id: UUID,
    actor_user_id: UUID | None,
    plan: dict[str, Any],
    actor_roles: list[str],
    actor_email: str | None,
) -> dict[str, Any]:
    meta = dict(plan.get("_meta") or {})
    meta["actor_roles"] = list(actor_roles or [])
    if actor_email:
        meta["actor_email"] = actor_email
    plan_to_store = {
        "steps": plan.get("steps") or [],
        "questions": plan.get("questions") or [],
        "warnings": plan.get("warnings") or [],
        "_meta": meta,
    }
    has_steps = bool(plan_to_store["steps"])
    has_questions = bool(plan_to_store["questions"])
    status_value = "draft" if has_questions or not has_steps else "awaiting_confirmation"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO slmct.operator_plans (
                actor_user_id, organisation_id, instruction, plan, status
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (actor_user_id, organisation_id, instruction.strip(), Json(plan_to_store), status_value),
        )
        row = cur.fetchone()
        conn.commit()
    return _record_row(row)


def plan_operator(conn: Connection, req: OperatorPlanRequest) -> dict[str, Any]:
    if not req.instruction or not str(req.instruction).strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="instruction is required")
    if not req.organisation_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="organisation_id is required")
    if not req.actor_roles:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="actor_roles is required")

    org_id = UUID(str(req.organisation_id))
    actor_user_id = UUID(str(req.actor_user_id)) if req.actor_user_id else None
    actor_email = _resolve_actor_email(conn, req.actor_user_id, req.actor_email)
    actor = _build_actor(req.actor_roles, req.actor_user_id, actor_email)
    write_tools = get_write_toolset(req.actor_roles)
    merged_instruction = merge_operator_instruction(
        req.instruction,
        answers=req.answers,
        answer_fields=req.answer_fields,
    )

    try:
        draft = _planner_tool_loop(
            conn,
            instruction=merged_instruction,
            organisation_id=str(org_id),
            actor=actor,
            write_tools=write_tools,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    draft = resolve_entity_lookup_gaps(conn, draft, merged_instruction, org_id, actor)
    draft = resolve_employee_software_request_gaps(
        conn, draft, merged_instruction, org_id, actor, write_tools
    )
    draft = resolve_licence_assignment_gaps(conn, draft, merged_instruction, org_id, actor, write_tools)
    draft = normalize_budget_direct_create(draft, write_tools, merged_instruction)
    draft = resolve_budget_permission_gaps(draft, write_tools, merged_instruction)
    draft = autofill_plan_gaps(conn, draft, merged_instruction, org_id)

    errors = validate_plan_steps(
        draft,
        write_tools,
        instruction=merged_instruction,
        actor_roles=req.actor_roles,
    )
    if errors:
        try:
            draft = _repair_plan_once(
                conn,
                instruction=merged_instruction,
                organisation_id=str(org_id),
                draft_plan=draft,
                validation_errors=errors,
                write_tools=write_tools,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Plan validation failed: {'; '.join(errors)}",
            ) from exc
        errors = validate_plan_steps(
            draft,
            write_tools,
            instruction=merged_instruction,
            actor_roles=req.actor_roles,
        )
        if errors:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Plan validation failed after repair: {'; '.join(errors)}",
            )

    draft = resolve_entity_lookup_gaps(conn, draft, merged_instruction, org_id, actor)
    draft = resolve_employee_software_request_gaps(
        conn, draft, merged_instruction, org_id, actor, write_tools
    )
    draft = resolve_licence_assignment_gaps(conn, draft, merged_instruction, org_id, actor, write_tools)
    draft = normalize_budget_direct_create(draft, write_tools, merged_instruction)
    draft = resolve_budget_permission_gaps(draft, write_tools, merged_instruction)
    draft = autofill_plan_gaps(conn, draft, merged_instruction, org_id)
    errors = validate_plan_steps(
        draft,
        write_tools,
        instruction=merged_instruction,
        actor_roles=req.actor_roles,
    )
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Plan validation failed after autofill: {'; '.join(errors)}",
        )

    stored = _store_plan(
        conn,
        instruction=merged_instruction,
        organisation_id=org_id,
        actor_user_id=actor_user_id,
        plan=draft,
        actor_roles=req.actor_roles,
        actor_email=actor_email,
    )
    return {
        "plan_id": stored["id"],
        "status": stored["status"],
        "instruction": stored["instruction"],
        "plan": {
            "steps": draft.get("steps") or [],
            "questions": draft.get("questions") or [],
            "warnings": draft.get("warnings") or [],
        },
        "validation_errors": [],
    }


def _load_plan(conn: Connection, plan_id: UUID) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM slmct.operator_plans WHERE id = %s", (plan_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    return dict(row)


def _plan_age(plan_row: dict[str, Any]) -> timedelta:
    created = plan_row.get("created_at")
    if not isinstance(created, datetime):
        return timedelta.max
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created


def _verify_step(
    conn: Connection,
    *,
    organisation_id: UUID,
    tool_name: str,
    params: dict[str, Any],
    result: dict[str, Any],
    sabotage: bool = False,
) -> tuple[bool, str]:
    if sabotage:
        return False, "Verifier sabotaged for test — step reported as failed"

    if isinstance(result, dict) and result.get("error"):
        return False, str(result["error"])

    org_id = str(organisation_id)

    def _one(sql: str, values: tuple[Any, ...]) -> dict | None:
        with conn.cursor() as cur:
            cur.execute(sql, values)
            row = cur.fetchone()
        return dict(row) if row else None

    if tool_name == "create_budget":
        budget = (result.get("budget") or {}) if isinstance(result, dict) else {}
        budget_id = budget.get("id")
        if not budget_id:
            return False, "create_budget returned no budget id"
        row = _one(
            """
            SELECT department, fiscal_year, allocated_amount, currency_code
            FROM slmct.budgets
            WHERE id = %s AND organisation_id = %s
            """,
            (budget_id, org_id),
        )
        if not row:
            return False, "Budget row not found after create"
        if params.get("department") and str(row.get("department")) != str(params.get("department")):
            return False, "Budget department mismatch"
        if params.get("fiscal_year") is not None and int(row.get("fiscal_year")) != int(params.get("fiscal_year")):
            return False, "Budget fiscal_year mismatch"
        return True, "Budget verified in database"

    if tool_name == "update_budget":
        budget_id = params.get("budget_id")
        row = _one(
            "SELECT id, status FROM slmct.budgets WHERE id = %s AND organisation_id = %s",
            (budget_id, org_id),
        )
        if not row:
            return False, "Updated budget not found"
        if params.get("status") and str(row.get("status")) != str(params.get("status")):
            return False, "Budget status not updated"
        return True, "Budget update verified"

    if tool_name == "create_vendor":
        vendor = result.get("vendor") or {}
        vendor_id = vendor.get("id")
        row = _one(
            "SELECT name FROM slmct.vendors WHERE id = %s AND organisation_id = %s",
            (vendor_id, org_id),
        )
        return (True, "Vendor verified") if row else (False, "Vendor not found after create")

    if tool_name == "create_vendor_catalogue_item":
        item = result.get("catalogue_item") or {}
        item_id = item.get("id")
        row = _one(
            """
            SELECT vc.name, vc.price
            FROM slmct.vendor_catalogue vc
            JOIN slmct.vendors v ON v.id = vc.vendor_id
            WHERE vc.id = %s AND v.organisation_id = %s
            """,
            (item_id, org_id),
        )
        return (True, "Catalogue item verified") if row else (False, "Catalogue item not found after create")

    if tool_name == "create_subscription":
        sub = result.get("subscription") or {}
        sub_id = sub.get("id")
        row = _one(
            "SELECT name FROM slmct.subscriptions WHERE id = %s AND organisation_id = %s",
            (sub_id, org_id),
        )
        return (True, "Subscription verified") if row else (False, "Subscription not found after create")

    if tool_name == "assign_licence":
        person_id = str(params.get("person_id") or "")
        row = _one(
            """
            SELECT assigned_to_person_id::text, status
            FROM slmct.licences
            WHERE id = %s AND organisation_id = %s
            """,
            (params.get("licence_id"), org_id),
        )
        if not row:
            return False, "Licence not found after assign"
        if str(row.get("assigned_to_person_id") or "") != person_id:
            return False, "Licence assignee mismatch"
        return True, "Licence assignment verified"

    if tool_name == "revoke_licence":
        row = _one(
            "SELECT status FROM slmct.licences WHERE id = %s AND organisation_id = %s",
            (params.get("licence_id"), org_id),
        )
        if not row:
            return False, "Licence not found after revoke"
        if str(row.get("status")) != "revoked":
            return False, "Licence status is not revoked"
        return True, "Licence revocation verified"

    if tool_name == "create_employee":
        employee = result.get("employee") or {}
        employee_id = employee.get("id")
        row = _one(
            "SELECT work_email::text FROM slmct.people WHERE id = %s AND organisation_id = %s",
            (employee_id, org_id),
        )
        return (True, "Employee verified") if row else (False, "Employee not found after create")

    if tool_name == "update_employee_status":
        row = _one(
            "SELECT status FROM slmct.people WHERE id = %s AND organisation_id = %s",
            (params.get("employee_id"), org_id),
        )
        if not row:
            return False, "Employee not found after status update"
        if params.get("status") and str(row.get("status")) != str(params.get("status")):
            return False, "Employee status mismatch"
        return True, "Employee status verified"

    if tool_name == "submit_workflow_request":
        wf = result.get("workflow_request") or {}
        wf_id = wf.get("id")
        row = _one(
            """
            SELECT workflow_type, status
            FROM slmct.workflow_requests
            WHERE id = %s AND organisation_id = %s
            """,
            (wf_id, org_id),
        )
        return (True, "Workflow request verified") if row else (False, "Workflow request not found")

    if tool_name == "respond_info_request":
        wf = result.get("workflow_request") or {}
        wf_id = wf.get("id") or params.get("request_id")
        row = _one(
            "SELECT status FROM slmct.workflow_requests WHERE id = %s AND organisation_id = %s",
            (wf_id, org_id),
        )
        if not row:
            return False, "Workflow not found after resubmit"
        if str(row.get("status")) == "info_requested":
            return False, "Workflow still awaiting info after resubmit"
        return True, "Workflow resubmission verified"

    if isinstance(result, dict) and not result.get("error"):
        return True, "Step completed (generic verification)"
    return False, f"No verifier for tool '{tool_name}'"


def execute_operator(conn: Connection, req: OperatorExecuteRequest) -> dict[str, Any]:
    if not req.confirmed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="confirmed must be true to execute")

    plan_id = UUID(str(req.plan_id))
    plan_row = _load_plan(conn, plan_id)

    if plan_row.get("status") in {"completed", "cancelled"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Plan has already finished")
    if plan_row.get("status") == "executing":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Plan is already executing")

    if _plan_age(plan_row) > PLAN_MAX_AGE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Plan expired (older than 10 minutes)")

    stored_plan = plan_row.get("plan") or {}
    meta = stored_plan.get("_meta") or {}
    actor_roles = list(req.actor_roles or meta.get("actor_roles") or [])
    actor_email = meta.get("actor_email")
    actor_user_id = req.actor_user_id or (str(plan_row["actor_user_id"]) if plan_row.get("actor_user_id") else None)

    if req.organisation_id and str(plan_row["organisation_id"]) != str(req.organisation_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="organisation_id does not match plan")
    if req.actor_user_id and plan_row.get("actor_user_id"):
        if str(plan_row["actor_user_id"]) != str(req.actor_user_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="actor_user_id does not match plan")

    org_id = UUID(str(plan_row["organisation_id"]))
    steps = stored_plan.get("steps") or []
    questions = stored_plan.get("questions") or []
    if questions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Plan has unanswered questions — replan with answers before executing",
        )
    if not steps:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Plan has no executable steps")

    write_tools = get_write_toolset(actor_roles)
    validation_errors = validate_plan_steps(
        stored_plan,
        write_tools,
        instruction=str(plan_row.get("instruction") or ""),
        actor_roles=actor_roles,
    )
    if validation_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Stored plan failed validation: {'; '.join(validation_errors)}",
        )

    actor = _build_actor(actor_roles, actor_user_id, actor_email)
    write_by_name = {tool.name: tool for tool in write_tools}

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE slmct.operator_plans
            SET status = 'executing', updated_at = now()
            WHERE id = %s
            """,
            (plan_id,),
        )
        conn.commit()

    step_results: list[dict[str, Any]] = []
    final_status = "completed"

    for index, step in enumerate(steps):
        tool_name = str(step.get("tool") or "")
        params = dict(step.get("params") or {})
        description = str(step.get("description") or "")
        tool = write_by_name.get(tool_name)
        step_record: dict[str, Any] = {
            "index": index,
            "tool": tool_name,
            "description": description,
            "status": "pending",
        }
        if tool is None:
            step_record.update({"status": "failed", "error": f"Tool '{tool_name}' not permitted"})
            step_results.append(step_record)
            final_status = "failed"
            break

        try:
            resolved_params = _resolve_step_refs(params, step_results)
        except ValueError as exc:
            step_record.update({"status": "failed", "error": str(exc)})
            step_results.append(step_record)
            final_status = "failed"
            break

        try:
            result = execute_tool(tool, conn, org_id, actor, **resolved_params)
        except Exception as exc:
            step_record.update({"status": "failed", "error": str(exc)})
            step_results.append(step_record)
            final_status = "failed"
            break

        ok, verify_msg = _verify_step(
            conn,
            organisation_id=org_id,
            tool_name=tool_name,
            params=resolved_params,
            result=result if isinstance(result, dict) else {"result": result},
            sabotage=req.sabotage_verifier,
        )
        step_record["result"] = result
        step_record["verification"] = verify_msg
        if not ok:
            step_record["status"] = "failed"
            step_results.append(step_record)
            final_status = "failed"
            break

        step_record["status"] = "passed"
        step_results.append(step_record)

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE slmct.operator_plans
            SET status = %s, step_results = %s, updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (final_status, Json(step_results), plan_id),
        )
        updated = cur.fetchone()
        conn.commit()

    passed = sum(1 for row in step_results if row.get("status") == "passed")
    return {
        "plan_id": str(plan_id),
        "status": final_status,
        "steps_executed": len(step_results),
        "steps_passed": passed,
        "step_results": step_results,
        "partial_completion": final_status == "failed",
        "plan": _record_row(updated),
    }
