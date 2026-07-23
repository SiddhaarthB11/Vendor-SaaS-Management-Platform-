"""Operator evaluation suite — fixture org, programmatic plan/execute checks, teardown."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg import Connection
from psycopg.types.json import Json

from app.agents import (
    OperatorExecuteRequest,
    OperatorPlanRequest,
    execute_operator,
    merge_operator_instruction,
    plan_operator,
    validate_plan_steps,
)
from app.tools import execute_tool, get_tool_by_name, get_write_toolset

EVAL_ORG_CODE = "operator_eval_org"
EVAL_ORG_NAME = "Operator Eval Organisation"


def _eval_summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(1 for c in checks if c.get("status") == "pass")
    failed = sum(1 for c in checks if c.get("status") == "fail")
    warned = sum(1 for c in checks if c.get("status") == "warn")
    skipped = sum(1 for c in checks if c.get("status") == "skip")
    total = len(checks)
    overall = "pass" if failed == 0 else "fail"
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "warned": warned,
        "skipped": skipped,
        "overall": overall,
    }


def _chk(checks: list[dict], name: str, ok: bool, detail: str = "", *, warning: bool = False) -> None:
    checks.append(
        {
            "name": name,
            "status": "warn" if warning and ok else ("pass" if ok else "fail"),
            "detail": detail[:500],
        }
    )


def _cleanup_eval_org(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM slmct.organisations WHERE code = %s", (EVAL_ORG_CODE,))
        row = cur.fetchone()
        if not row:
            return
        org_id = row["id"]
        cur.execute(
            """
            DELETE FROM slmct.workflow_status_history
            WHERE workflow_request_id IN (
                SELECT id FROM slmct.workflow_requests WHERE organisation_id = %s
            )
            """,
            (org_id,),
        )
        cur.execute("DELETE FROM slmct.operator_plans WHERE organisation_id = %s", (org_id,))
        cur.execute(
            """
            DELETE FROM slmct.vendor_catalogue
            WHERE vendor_id IN (SELECT id FROM slmct.vendors WHERE organisation_id = %s)
            """,
            (org_id,),
        )
        for table in (
            "slmct.audit_logs",
            "slmct.workflow_requests",
            "slmct.licences",
            "slmct.subscriptions",
            "slmct.vendors",
            "slmct.budgets",
            "slmct.people",
            "slmct.payments",
        ):
            cur.execute(f"DELETE FROM {table} WHERE organisation_id = %s", (org_id,))
        cur.execute("DELETE FROM slmct.organisations WHERE id = %s", (org_id,))
        conn.commit()


def _purge_eval_vendor(conn: Connection, org_id: UUID, vendor_name: str) -> None:
    """Remove a test vendor and linked catalogue rows/subscriptions (eval org only)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM slmct.vendors WHERE organisation_id = %s AND name = %s LIMIT 1",
            (org_id, vendor_name),
        )
        row = cur.fetchone()
        if not row:
            return
        vendor_id = row["id"]
        cur.execute("DELETE FROM slmct.licences WHERE subscription_id IN (SELECT id FROM slmct.subscriptions WHERE vendor_id = %s)", (vendor_id,))
        cur.execute("DELETE FROM slmct.subscriptions WHERE vendor_id = %s", (vendor_id,))
        cur.execute("DELETE FROM slmct.vendor_catalogue WHERE vendor_id = %s", (vendor_id,))
        cur.execute("DELETE FROM slmct.vendors WHERE id = %s", (vendor_id,))
        conn.commit()


def _purge_eval_budget(conn: Connection, org_id: UUID, *, department: str, fiscal_year: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM slmct.budgets
            WHERE organisation_id = %s AND department = %s AND fiscal_year = %s
            """,
            (org_id, department, fiscal_year),
        )
        conn.commit()


def _purge_ephemeral_eval_subscriptions(conn: Connection, org_id: UUID) -> None:
    """Drop execute-test subscriptions; keep seeded OpEval Slack Sub for licence checks."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM slmct.licences
            WHERE subscription_id IN (
                SELECT id FROM slmct.subscriptions
                WHERE organisation_id = %s AND name <> 'OpEval Slack Sub'
            )
            """,
            (org_id,),
        )
        cur.execute(
            """
            DELETE FROM slmct.subscriptions
            WHERE organisation_id = %s
              AND name <> 'OpEval Slack Sub'
              AND (
                name LIKE 'OpEval%%'
                OR name ILIKE 'Apple Plus%%'
                OR name ILIKE 'Apple TV%%'
              )
            """,
            (org_id,),
        )
        conn.commit()


def _purge_ephemeral_eval_vendors(conn: Connection, org_id: UUID) -> None:
    """Remove all OpEval-prefixed test vendors (and catalogue/subs) except none required mid-suite."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM slmct.vendors
            WHERE organisation_id = %s AND name LIKE 'OpEval%%'
            """,
            (org_id,),
        )
        vendor_ids = [row["id"] for row in cur.fetchall()]
        for vendor_id in vendor_ids:
            cur.execute(
                "DELETE FROM slmct.licences WHERE subscription_id IN (SELECT id FROM slmct.subscriptions WHERE vendor_id = %s)",
                (vendor_id,),
            )
            cur.execute("DELETE FROM slmct.subscriptions WHERE vendor_id = %s", (vendor_id,))
            cur.execute("DELETE FROM slmct.vendor_catalogue WHERE vendor_id = %s", (vendor_id,))
            cur.execute("DELETE FROM slmct.vendors WHERE id = %s", (vendor_id,))
        conn.commit()


def _seed_eval_org(conn: Connection) -> dict[str, Any]:
    _cleanup_eval_org(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO slmct.organisations (code, name, currency_code, is_active)
            VALUES (%s, %s, 'AED', true)
            RETURNING id
            """,
            (EVAL_ORG_CODE, EVAL_ORG_NAME),
        )
        org_id = cur.fetchone()["id"]

        cur.execute(
            """
            INSERT INTO slmct.subscriptions (
                organisation_id, name, department, status, amount, currency_code, billing_cycle
            )
            VALUES (%s, 'OpEval Slack Sub', 'Engineering', 'active', 1200, 'AED', 'monthly')
            RETURNING id
            """,
            (org_id,),
        )
        sub_id = cur.fetchone()["id"]

        cur.execute(
            """
            INSERT INTO slmct.subscriptions (
                organisation_id, name, department, status, amount, currency_code, billing_cycle
            )
            VALUES (%s, 'OpEval Figma', 'Engineering', 'active', 10000, 'AED', 'annual')
            RETURNING id
            """,
            (org_id,),
        )
        figma_sub_id = cur.fetchone()["id"]

        cur.execute(
            """
            INSERT INTO slmct.people (
                organisation_id, full_name, work_email, department, status
            )
            VALUES
              (%s, 'OpEval Person A', 'opeval.a@operator-eval.internal', 'Engineering', 'active'),
              (%s, 'OpEval Person B', 'opeval.b@operator-eval.internal', 'Sales', 'active')
            RETURNING id, full_name
            """,
            (org_id, org_id),
        )
        people = {row["full_name"]: row["id"] for row in cur.fetchall()}

        cur.execute(
            """
            INSERT INTO slmct.licences (
                organisation_id, subscription_id, licence_name, status
            )
            VALUES (%s, %s, 'OpEval Slack Spare 1', 'available'),
                   (%s, %s, 'OpEval Slack Spare 2', 'available')
            RETURNING id, licence_name
            """,
            (org_id, sub_id, org_id, sub_id),
        )
        licences = {row["licence_name"]: row["id"] for row in cur.fetchall()}
        conn.commit()

    return {
        "org_id": org_id,
        "sub_id": sub_id,
        "figma_sub_id": figma_sub_id,
        "people": people,
        "licences": licences,
    }


def _insert_plan(
    conn: Connection,
    *,
    org_id: UUID,
    instruction: str,
    plan: dict[str, Any],
    actor_roles: list[str],
    actor_user_id: UUID | None = None,
    created_at: datetime | None = None,
) -> UUID:
    meta = {"actor_roles": actor_roles}
    stored = {
        "steps": plan.get("steps") or [],
        "questions": plan.get("questions") or [],
        "warnings": plan.get("warnings") or [],
        "_meta": meta,
    }
    status = "awaiting_confirmation" if stored["steps"] and not stored["questions"] else "draft"
    plan_id = uuid4()
    with conn.cursor() as cur:
        if created_at:
            cur.execute(
                """
                INSERT INTO slmct.operator_plans (
                    id, actor_user_id, organisation_id, instruction, plan, status, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (plan_id, actor_user_id, org_id, instruction, Json(stored), status, created_at, created_at),
            )
        else:
            cur.execute(
                """
                INSERT INTO slmct.operator_plans (
                    id, actor_user_id, organisation_id, instruction, plan, status
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (plan_id, actor_user_id, org_id, instruction, Json(stored), status),
            )
        conn.commit()
    return plan_id


def run_operator_eval(conn: Connection, payload: dict | None = None) -> dict[str, Any]:
    payload = payload or {}
    checks: list[dict[str, Any]] = []
    org_id: UUID | None = None
    teardown_ok = False

    try:
        fixture = _seed_eval_org(conn)
        org_id = fixture["org_id"]
        person_a = fixture["people"]["OpEval Person A"]
        licence_id = fixture["licences"]["OpEval Slack Spare 1"]

        _chk(checks, "Seed: eval organisation created", True, f"org_id={org_id}")

        # 1. Direct write tool + audit source
        budget_tool = get_tool_by_name("create_budget")
        actor_finance = {"roles": ["finance"], "email": "finance-eval@operator-eval.internal"}
        budget_result = execute_tool(
            budget_tool,
            conn,
            org_id,
            actor_finance,
            fiscal_year=2027,
            department="Marketing",
            allocated_amount=30000,
            currency_code="AED",
        )
        budget_ok = "budget" in budget_result and not budget_result.get("error")
        _chk(checks, "Write tool: create_budget succeeds for finance", budget_ok, str(budget_result)[:200])
        if budget_ok:
            budget_id = budget_result["budget"]["id"]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT allocated_amount, department, fiscal_year, currency_code
                    FROM slmct.budgets WHERE id = %s
                    """,
                    (budget_id,),
                )
                row = cur.fetchone()
            _chk(
                checks,
                "SQL: Marketing 2027 budget exists with exact fields",
                row
                and float(row["allocated_amount"]) == 30000
                and row["department"] == "Marketing"
                and int(row["fiscal_year"]) == 2027
                and row["currency_code"] == "AED",
                str(dict(row) if row else {}),
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT metadata->>'source' AS source
                    FROM slmct.audit_logs
                    WHERE entity_id = %s
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (budget_id,),
                )
                audit = cur.fetchone()
            _chk(checks, "Audit: create_budget logged source ai_operator", audit and audit.get("source") == "ai_operator")
            _purge_eval_budget(conn, org_id, department="Marketing", fiscal_year=2027)

        # 2. assign_licence direct
        assign_result = execute_tool(
            get_tool_by_name("assign_licence"),
            conn,
            org_id,
            {"roles": ["master_admin"], "email": "master@operator-eval.internal"},
            licence_id=str(licence_id),
            person_id=str(person_a),
        )
        assign_ok = "licence" in assign_result and not assign_result.get("error")
        _chk(checks, "Write tool: assign_licence to OpEval Person A", assign_ok, str(assign_result)[:200])
        if assign_ok:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT assigned_to_person_id FROM slmct.licences WHERE id = %s",
                    (licence_id,),
                )
                lic = cur.fetchone()
            _chk(
                checks,
                "SQL: spare Slack licence assigned to OpEval Person A",
                lic and str(lic["assigned_to_person_id"]) == str(person_a),
            )

        # 3. RBAC denial — employee cannot create_budget
        denied = execute_tool(
            get_tool_by_name("create_budget"),
            conn,
            org_id,
            {"roles": ["employee"]},
            fiscal_year=2027,
            department="Denied",
            allocated_amount=1,
        )
        _chk(checks, "RBAC: employee denied create_budget", bool(denied.get("error")))

        # 4. Plan validation rejects unknown tool
        bad_plan = {
            "steps": [{"tool": "delete_everything", "params": {}, "description": "bad"}],
            "questions": [],
            "warnings": [],
        }
        val_errors = validate_plan_steps(bad_plan, get_write_toolset(["master_admin"]))
        _chk(checks, "Validation: unknown write tool rejected", len(val_errors) > 0, "; ".join(val_errors))

        # 4b. Invalid workflow module name rejected
        bad_module_plan = {
            "steps": [
                {
                    "tool": "submit_workflow_request",
                    "params": {
                        "workflow_type": "new_subscription_request",
                        "requested_module": "subscription",
                        "payload": {"name": "Apple Plus", "vendor_id": "Apple123"},
                    },
                    "description": "Submit subscription workflow",
                }
            ],
            "questions": [],
            "warnings": [],
        }
        module_errors = validate_plan_steps(bad_module_plan, get_write_toolset(["master_admin"]))
        _chk(
            checks,
            "Validation: unsupported requested_module rejected",
            any("requested_module" in err for err in module_errors),
            "; ".join(module_errors),
        )

        # 4c. Fake vendor_id rejected
        fake_id_plan = {
            "steps": [
                {
                    "tool": "create_subscription",
                    "params": {"name": "Apple Plus", "vendor_id": "Apple123"},
                    "description": "Create Apple Plus subscription",
                }
            ],
            "questions": [],
            "warnings": [],
        }
        id_errors = validate_plan_steps(fake_id_plan, get_write_toolset(["master_admin"]))
        _chk(
            checks,
            "Validation: non-UUID vendor_id rejected",
            any("vendor_id" in err and "UUID" in err for err in id_errors),
            "; ".join(id_errors),
        )

        # 4d. Direct-create instruction must not use workflow submit for subscriptions
        direct_plan = {
            "steps": [
                {
                    "tool": "submit_workflow_request",
                    "params": {
                        "workflow_type": "new_subscription_request",
                        "requested_module": "subscriptions",
                        "payload": {"name": "Apple Plus"},
                    },
                    "description": "Submit subscription workflow",
                }
            ],
            "questions": [],
            "warnings": [],
        }
        direct_errors = validate_plan_steps(
            direct_plan,
            get_write_toolset(["master_admin"]),
            instruction="Directly create Apple Plus subscription in the subscriptions tab",
            actor_roles=["master_admin"],
        )
        _chk(
            checks,
            "Validation: direct create rejects submit_workflow_request for subscriptions",
            any("direct create" in err.lower() for err in direct_errors),
            "; ".join(direct_errors),
        )

        # 4d2. Budget allocation must use create_budget, not workflow submit
        budget_workflow_plan = {
            "steps": [
                {
                    "tool": "submit_workflow_request",
                    "params": {
                        "workflow_type": "budget_request",
                        "requested_module": "budgets",
                        "payload": {
                            "fiscal_year": 2026,
                            "department": "iphone",
                            "allocated_amount": 30000,
                            "currency_code": "AED",
                        },
                    },
                    "description": "Submit budget workflow",
                }
            ],
            "questions": [],
            "warnings": [],
        }
        budget_wf_errors = validate_plan_steps(
            budget_workflow_plan,
            get_write_toolset(["master_admin"]),
            instruction="Create a 30000 AED iphone department budget for year 2026",
            actor_roles=["master_admin"],
        )
        _chk(
            checks,
            "Validation: budget allocation rejects submit_workflow_request for master_admin",
            any("not submitted via workflow" in err for err in budget_wf_errors),
            "; ".join(budget_wf_errors),
        )
        from app.operator_entity_resolver import normalize_budget_direct_create, resolve_budget_permission_gaps

        normalized = normalize_budget_direct_create(
            budget_workflow_plan,
            get_write_toolset(["master_admin"]),
            "Create a 30000 AED iphone department budget for year 2026",
        )
        norm_steps = normalized.get("steps") or []
        _chk(
            checks,
            "Normalizer: budget workflow plan becomes create_budget",
            len(norm_steps) == 1 and norm_steps[0].get("tool") == "create_budget",
            str(norm_steps)[:240],
        )
        refused = resolve_budget_permission_gaps(
            budget_workflow_plan,
            get_write_toolset(["it_admin"]),
            "Create a 30000 AED iphone department budget for year 2026",
        )
        _chk(
            checks,
            "Permission: IT Admin budget create refused with Finance warning",
            not (refused.get("steps") or [])
            and any("Finance" in w for w in (refused.get("warnings") or [])),
            str(refused.get("warnings", []))[:240],
        )

        from app.operator_entity_resolver import resolve_licence_assignment_gaps

        bad_lic_plan = {
            "steps": [],
            "questions": ["Could you please provide the exact full name or work email of 'OpEval Person A'?"],
            "warnings": [],
        }
        lic_fixed = resolve_licence_assignment_gaps(
            conn,
            bad_lic_plan,
            "assign a figma license to OpEval Person A",
            org_id,
            {"roles": ["it_admin"]},
            get_write_toolset(["it_admin"]),
        )
        lic_steps = lic_fixed.get("steps") or []
        _chk(
            checks,
            "Resolver: IT Admin figma assign builds licence workflow step",
            len(lic_steps) == 1 and lic_steps[0].get("tool") == "submit_workflow_request",
            str(lic_steps)[:240],
        )
        if lic_steps:
            payload = (lic_steps[0].get("params") or {}).get("payload") or {}
            _chk(
                checks,
                "Resolver: licence workflow uses assigned_to_person_id + licence_name",
                bool(payload.get("assigned_to_person_id")) and bool(payload.get("licence_name")),
                str(payload)[:240],
            )

        # 4e. Structured instruction merge helper
        merged = merge_operator_instruction(
            "Create Apple Plus subscription directly",
            answer_fields={"vendor": "Apple", "amount": "99 AED", "department": "Operations"},
            answers={"What vendor?": "Apple"},
        )
        _chk(
            checks,
            "Instruction merge: structured details included",
            "Structured details:" in merged and "vendor: Apple" in merged and "department: Operations" in merged,
            merged[:240],
        )

        # 4f. Entity field guide
        from app.entity_field_guide import get_field_guide

        vendor_guide = get_field_guide(entity="vendor")
        _chk(
            checks,
            "Field guide: vendor entity includes surfaces_in",
            "surfaces_in" in vendor_guide and "vendor_catalogue" in str(vendor_guide.get("related_entities", [])),
            str(vendor_guide.get("surfaces_in", []))[:240],
        )

        # 4g. Step reference validation
        step_ref_plan = {
            "steps": [
                {
                    "tool": "create_vendor",
                    "params": {"name": "OpEval StepRef Vendor"},
                    "description": "Create vendor",
                },
                {
                    "tool": "create_vendor_catalogue_item",
                    "params": {
                        "vendor_id": "$step:0:vendor.id",
                        "name": "OpEval Plan",
                        "price": 49,
                        "currency_code": "AED",
                        "scrape_url": "https://example.com/pricing",
                    },
                    "description": "Add catalogue pricing",
                },
            ],
            "questions": [],
            "warnings": [],
        }
        step_ref_errors = validate_plan_steps(step_ref_plan, get_write_toolset(["master_admin"]))
        _chk(
            checks,
            "Validation: multi-step plan with $step refs passes",
            not step_ref_errors,
            "; ".join(step_ref_errors),
        )
        bad_ref_plan = {
            "steps": [
                {
                    "tool": "create_vendor_catalogue_item",
                    "params": {
                        "vendor_id": "$step:1:vendor.id",
                        "name": "Bad",
                        "price": 1,
                    },
                    "description": "bad ref",
                }
            ],
            "questions": [],
            "warnings": [],
        }
        bad_ref_errors = validate_plan_steps(bad_ref_plan, get_write_toolset(["master_admin"]))
        _chk(
            checks,
            "Validation: forward step reference rejected",
            any("earlier step" in err.lower() for err in bad_ref_errors),
            "; ".join(bad_ref_errors),
        )

        # 4h. Execute multi-step vendor + catalogue via step refs
        step_ref_plan_id = _insert_plan(
            conn,
            org_id=org_id,
            instruction="Create vendor with catalogue pricing",
            plan=step_ref_plan,
            actor_roles=["master_admin"],
        )
        step_ref_exec = execute_operator(
            conn,
            OperatorExecuteRequest(
                plan_id=str(step_ref_plan_id),
                confirmed=True,
                organisation_id=str(org_id),
                actor_roles=["master_admin"],
            ),
        )
        _chk(checks, "Execute: vendor + catalogue step-ref plan completes", step_ref_exec.get("status") == "completed")
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT vc.name, vc.price
                FROM slmct.vendor_catalogue vc
                JOIN slmct.vendors v ON v.id = vc.vendor_id
                WHERE v.organisation_id = %s AND v.name = 'OpEval StepRef Vendor'
                """,
                (org_id,),
            )
            cat_row = cur.fetchone()
        _chk(
            checks,
            "SQL: catalogue item exists after step-ref execute",
            cat_row is not None and str(cat_row["name"]) == "OpEval Plan",
            str(dict(cat_row) if cat_row else {})[:240],
        )
        _purge_eval_vendor(conn, org_id, "OpEval StepRef Vendor")

        # 4i. Autofill resolves placeholders when user asks to find pricing
        from app.operator_autofill import autofill_plan_gaps

        placeholder_plan = {
            "steps": [
                {
                    "tool": "create_vendor_catalogue_item",
                    "params": {
                        "vendor_id": str(org_id),
                        "name": "OpEval Autofill Product",
                        "price": "$question:What is the monthly price?",
                        "scrape_url": "https://example.com/pricing",
                    },
                    "description": "Catalogue with placeholder price",
                }
            ],
            "questions": ["What is the monthly price?", "What is the billing cycle?"],
            "warnings": [],
        }

        def _fake_pricing(_name: str, _vendor: str, _url: str | None) -> dict:
            return {"price": 12.5, "currency_code": "USD", "scrape_url": "https://example.com/pricing"}

        autofilled = autofill_plan_gaps(
            conn,
            placeholder_plan,
            "Add OpEval Autofill Product and find the price for Software Engineering",
            org_id,
            pricing_resolver=_fake_pricing,
        )
        cat_step = (autofilled.get("steps") or [{}])[0]
        cat_params = cat_step.get("params") or {}
        _chk(
            checks,
            "Autofill: resolves $question price placeholders",
            cat_params.get("price") == 12.5,
            str(cat_params)[:240],
        )
        _chk(
            checks,
            "Autofill: drops lookup-resolvable questions",
            not autofilled.get("questions"),
            str(autofilled.get("questions")),
        )

        # 5. Execute pre-built plan (create vendor)
        vendor_plan_id = _insert_plan(
            conn,
            org_id=org_id,
            instruction="Create vendor OpEval Vendor Z",
            plan={
                "steps": [
                    {
                        "tool": "create_vendor",
                        "params": {"name": "OpEval Vendor Z", "status": "active"},
                        "description": "Create vendor OpEval Vendor Z",
                    }
                ],
                "questions": [],
                "warnings": [],
            },
            actor_roles=["master_admin"],
        )
        vendor_exec = execute_operator(
            conn,
            OperatorExecuteRequest(
                plan_id=str(vendor_plan_id),
                confirmed=True,
                organisation_id=str(org_id),
                actor_roles=["master_admin"],
            ),
        )
        _chk(checks, "Execute: create vendor plan completes", vendor_exec.get("status") == "completed")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM slmct.vendors WHERE organisation_id = %s AND name = 'OpEval Vendor Z'",
                (org_id,),
            )
            vendor_row = cur.fetchone()
        _chk(checks, "SQL: OpEval Vendor Z exists after execute", vendor_row is not None)
        _purge_eval_vendor(conn, org_id, "OpEval Vendor Z")

        # 6. Verifier sabotage
        sabotage_plan_id = _insert_plan(
            conn,
            org_id=org_id,
            instruction="Create budget sabotage test",
            plan={
                "steps": [
                    {
                        "tool": "create_budget",
                        "params": {
                            "fiscal_year": 2028,
                            "department": "SabotageDept",
                            "allocated_amount": 100,
                            "currency_code": "AED",
                        },
                        "description": "Create sabotage test budget",
                    }
                ],
                "questions": [],
                "warnings": [],
            },
            actor_roles=["finance"],
        )
        sabotage_exec = execute_operator(
            conn,
            OperatorExecuteRequest(
                plan_id=str(sabotage_plan_id),
                confirmed=True,
                organisation_id=str(org_id),
                actor_roles=["finance"],
                sabotage_verifier=True,
            ),
        )
        _chk(
            checks,
            "Verifier: sabotaged step reports failure not silent success",
            sabotage_exec.get("status") == "failed"
            and sabotage_exec.get("partial_completion") is True
            and (sabotage_exec.get("step_results") or [{}])[0].get("status") == "failed",
            json.dumps(sabotage_exec.get("step_results") or [])[:300],
        )
        _purge_eval_budget(conn, org_id, department="SabotageDept", fiscal_year=2028)

        # 7. Stale plan rejected
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=15)
        stale_plan_id = _insert_plan(
            conn,
            org_id=org_id,
            instruction="Stale plan",
            plan={
                "steps": [
                    {
                        "tool": "create_vendor",
                        "params": {"name": "Stale Vendor"},
                        "description": "stale",
                    }
                ],
                "questions": [],
                "warnings": [],
            },
            actor_roles=["master_admin"],
            created_at=stale_at,
        )
        stale_rejected = False
        try:
            execute_operator(
                conn,
                OperatorExecuteRequest(
                    plan_id=str(stale_plan_id),
                    confirmed=True,
                    organisation_id=str(org_id),
                    actor_roles=["master_admin"],
                ),
            )
        except HTTPException as exc:
            stale_rejected = "expired" in str(exc.detail).lower()
        _chk(checks, "Execute: plan older than 10 minutes rejected", stale_rejected)

        # 8. Execute refuses plan with unanswered questions
        questions_plan_id = _insert_plan(
            conn,
            org_id=org_id,
            instruction="add a budget for Legal",
            plan={
                "steps": [],
                "questions": ["What fiscal year and amount should the Legal budget use?"],
                "warnings": [],
            },
            actor_roles=["finance"],
        )
        questions_rejected = False
        try:
            execute_operator(
                conn,
                OperatorExecuteRequest(
                    plan_id=str(questions_plan_id),
                    confirmed=True,
                    organisation_id=str(org_id),
                    actor_roles=["finance"],
                ),
            )
        except HTTPException as exc:
            questions_rejected = "question" in str(exc.detail).lower()
        _chk(checks, "Execute: plan with questions cannot run", questions_rejected)

        # 9. LLM planner — missing detail should ask questions (or warn, not execute-ready)
        if not payload.get("skip_llm"):
            try:
                missing = plan_operator(
                    conn,
                    OperatorPlanRequest(
                        instruction="Add a budget for Legal",
                        organisation_id=str(org_id),
                        actor_roles=["finance"],
                        actor_email="finance-planner@operator-eval.internal",
                    ),
                )
                plan_body = missing.get("plan") or {}
                has_questions = bool(plan_body.get("questions"))
                no_steps = not plan_body.get("steps")
                _chk(
                    checks,
                    "Planner: missing-detail instruction returns questions",
                    has_questions or no_steps,
                    f"questions={plan_body.get('questions')}, steps={len(plan_body.get('steps') or [])}",
                )
            except Exception as exc:
                _chk(checks, "Planner: missing-detail instruction returns questions", False, str(exc)[:300])

            # 10. LLM planner — employee approve request should refuse
            try:
                refuse = plan_operator(
                    conn,
                    OperatorPlanRequest(
                        instruction="Approve the pending subscription workflow request",
                        organisation_id=str(org_id),
                        actor_roles=["employee"],
                        actor_email="employee@operator-eval.internal",
                    ),
                )
                plan_body = refuse.get("plan") or {}
                steps = plan_body.get("steps") or []
                warnings = " ".join(str(w) for w in (plan_body.get("warnings") or [])).lower()
                refused = (not steps) or any(
                    token in warnings for token in ("cannot", "forbidden", "not allowed", "approve", "permission")
                )
                _chk(
                    checks,
                    "Planner: employee approve instruction refused",
                    refused,
                    f"steps={len(steps)}, warnings={plan_body.get('warnings')}",
                )
            except Exception as exc:
                _chk(checks, "Planner: employee approve instruction refused", False, str(exc)[:300])

            # 11. LLM plan + execute create budget instruction
            try:
                planned = plan_operator(
                    conn,
                    OperatorPlanRequest(
                        instruction="Create a 45000 AED Operations budget for fiscal year 2029",
                        organisation_id=str(org_id),
                        actor_roles=["finance"],
                        actor_email="finance-planner@operator-eval.internal",
                    ),
                )
                plan_body = planned.get("plan") or {}
                has_budget_step = any(
                    (step.get("tool") == "create_budget") for step in (plan_body.get("steps") or [])
                )
                _chk(checks, "Planner: finance budget instruction yields create_budget step", has_budget_step)
                if has_budget_step and planned.get("plan_id") and not plan_body.get("questions"):
                    executed = execute_operator(
                        conn,
                        OperatorExecuteRequest(
                            plan_id=str(planned["plan_id"]),
                            confirmed=True,
                            organisation_id=str(org_id),
                            actor_roles=["finance"],
                        ),
                    )
                    _chk(checks, "Execute: LLM-planned budget completes", executed.get("status") == "completed")
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT 1 FROM slmct.budgets
                            WHERE organisation_id = %s AND department = 'Operations' AND fiscal_year = 2029
                            """,
                            (org_id,),
                        )
                        ops_budget = cur.fetchone()
                    _chk(checks, "SQL: Operations 2029 budget exists after LLM plan", ops_budget is not None)
                    _purge_eval_budget(conn, org_id, department="Operations", fiscal_year=2029)
            except Exception as exc:
                _chk(checks, "Planner/Execute: LLM budget flow", False, str(exc)[:300])

            _purge_ephemeral_eval_subscriptions(conn, org_id)
            _purge_ephemeral_eval_vendors(conn, org_id)

            # 12. LLM planner — direct subscription should prefer create_subscription (master admin)
            try:
                direct_sub = plan_operator(
                    conn,
                    OperatorPlanRequest(
                        instruction="Directly create Apple Plus subscription for vendor Apple at 99 AED/month in Operations",
                        organisation_id=str(org_id),
                        actor_roles=["master_admin"],
                        actor_email="master-planner@operator-eval.internal",
                    ),
                )
                plan_body = direct_sub.get("plan") or {}
                steps = plan_body.get("steps") or []
                tools = [str(step.get("tool") or "") for step in steps]
                has_direct = "create_subscription" in tools or "create_vendor" in tools
                uses_workflow_only = bool(steps) and all(tool == "submit_workflow_request" for tool in tools)
                has_questions = bool(plan_body.get("questions"))
                _chk(
                    checks,
                    "Planner: direct subscription prefers create_subscription/create_vendor",
                    has_direct or has_questions or not uses_workflow_only,
                    f"tools={tools}, questions={plan_body.get('questions')}",
                )
            except HTTPException as exc:
                detail = str(exc.detail)
                rejected_bad_plan = "validation failed" in detail.lower()
                _chk(
                    checks,
                    "Planner: direct subscription prefers create_subscription/create_vendor",
                    rejected_bad_plan,
                    detail[:300],
                )
            except Exception as exc:
                _chk(checks, "Planner: direct subscription prefers create_subscription/create_vendor", False, str(exc)[:300])
        else:
            _chk(checks, "Planner LLM checks", True, "skipped via skip_llm=true", warning=True)

        return {
            "mode": "operator_eval",
            "status": "ok",
            "checks": checks,
            "summary": _eval_summary(checks),
        }
    except Exception as exc:
        _chk(checks, "Operator eval: suite execution", False, str(exc)[:300])
        return {
            "mode": "operator_eval",
            "status": "error",
            "checks": checks,
            "summary": _eval_summary(checks),
        }
    finally:
        if org_id:
            try:
                _cleanup_eval_org(conn)
                teardown_ok = True
            except Exception as exc:
                _chk(checks, "Teardown: eval organisation removed", False, str(exc)[:300])
        if teardown_ok:
            _chk(checks, "Teardown: eval organisation removed", True)
