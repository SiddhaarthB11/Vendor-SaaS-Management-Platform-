"""Copilot evaluation suite — seeded fixture org, programmatic answer checkers, teardown."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Json

EVAL_ORG_CODE = "copilot_eval_org"
EVAL_ORG_NAME = "Copilot Eval Organisation"
EVAL_EMAIL_DOMAIN = "@copilot-eval.internal"

REFUSAL_PHRASES = (
    "don't have",
    "do not have",
    "no record",
    "not available",
    "cannot find",
    "can't find",
    "unable to find",
    "no data",
    "doesn't exist",
    "does not exist",
    "not found",
    "no information",
    "don't see",
    "do not see",
    "not in the database",
    "not in our",
    "no budget",
    "no matching",
    "couldn't find",
    "could not find",
    "doesn't appear",
    "does not appear",
    "not in",
    "no subscription",
    "no such",
)

INTERPRETATION_PHRASES = (
    "assuming",
    "interpret",
    "if you mean",
    "clarify",
    "could refer",
    "understand",
    "which department",
    "do you mean",
    "please specify",
    "ambiguous",
    "not entirely clear",
    "could mean",
    "interpreting",
    "total spend",
    "active subscriptions",
    "across the organisation",
    "overall",
    "software spend",
    "subscription spend",
    "organisation-wide",
    "org-wide",
    "interpreting your question",
)

PLATFORM_KEYWORDS = (
    "software",
    "license",
    "licence",
    "subscription",
    "slmct",
    "derisk",
    "vendor",
    "procurement",
    "compliance",
    "spend",
    "budget",
)

# Fixture names planted in the eval org — used for honesty/meta checks.
FIXTURE_NAMES = (
    "EvalSub Eng Slack",
    "EvalSub Eng Zoom",
    "EvalSub Eng Jira",
    "EvalSub Eng GitHub",
    "EvalSub Eng Notion",
    "EvalSub Design Figma",
    "EvalSub Design Adobe",
    "EvalSub Eng Cancelled A",
    "EvalSub Design Cancelled B",
    "EvalSub Design Cancelled C",
    "EvalSub Eng Expired",
    "EvalSub Design Trial",
    "Eval Person Active",
    "Eval Person Inactive",
    "Eval Vendor Alpha",
    "EvalPay Alpha",
    "EvalPay Beta",
    "EvalPay Gamma",
    "EvalPay Delta",
    "EvalLic Figma Pro",
    "EvalLic Expired Seat",
    "EvalLic Inactive A",
    "EvalLic Inactive B",
    "EvalLic Available Slack",
    "Eval Workflow Pending A",
    "Eval Workflow Pending B",
)


def _extract_numbers(text: str) -> list[float]:
    cleaned = text.replace(",", "")
    matches = re.findall(r"\d+(?:\.\d+)?", cleaned)
    return [float(m) for m in matches]


def _extract_answer_count_numbers(text: str) -> list[float]:
    """Strip timeframe phrases like '30 days' so counts are not confused with windows."""
    stripped = re.sub(r"\b\d+\s*days?\b", "", text, flags=re.IGNORECASE)
    stripped = re.sub(r"\bnext\s+\d+\b", "", stripped, flags=re.IGNORECASE)
    return _extract_numbers(stripped)


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in phrases)


def _contains_all_names(text: str, names: list[str]) -> bool:
    lower = text.lower()
    return all(name.lower() in lower for name in names)


def _contains_any_name(text: str, names: list[str]) -> bool:
    lower = text.lower()
    return any(name.lower() in lower for name in names)


def _number_matches(answer: str, expected: float, *, tolerance: float = 0.01) -> bool:
    numbers = _extract_answer_count_numbers(answer)
    if expected == 0:
        if _contains_any(
            answer,
            (
                "no licences",
                "no license",
                "0 licence",
                "0 license",
                "none expire",
                "do not expire",
                "don't expire",
                "not expiring",
                "no records",
                "no matching",
            ),
        ):
            return True
    if not numbers:
        return False
    return any(abs(n - expected) <= tolerance or abs(n - expected) / max(abs(expected), 1) <= tolerance for n in numbers)


def _cleanup_eval_org(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM slmct.organisations WHERE code = %s AND is_active = true", (EVAL_ORG_CODE,))
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
        cur.execute("DELETE FROM slmct.licences WHERE organisation_id = %s", (org_id,))
        cur.execute("DELETE FROM slmct.payments WHERE organisation_id = %s", (org_id,))
        cur.execute("DELETE FROM slmct.contracts WHERE organisation_id = %s", (org_id,))
        cur.execute("DELETE FROM slmct.subscriptions WHERE organisation_id = %s", (org_id,))
        cur.execute("DELETE FROM slmct.budgets WHERE organisation_id = %s", (org_id,))
        cur.execute("DELETE FROM slmct.workflow_requests WHERE organisation_id = %s", (org_id,))
        cur.execute("DELETE FROM slmct.audit_logs WHERE organisation_id = %s", (org_id,))
        cur.execute("DELETE FROM slmct.people WHERE organisation_id = %s", (org_id,))
        cur.execute("DELETE FROM slmct.vendors WHERE organisation_id = %s", (org_id,))
        cur.execute("DELETE FROM slmct.organisations WHERE id = %s", (org_id,))
    conn.commit()


def _seed_eval_org(conn: Connection) -> dict[str, Any]:
    today = date.today()
    renewal_15 = today + timedelta(days=15)
    renewal_25 = today + timedelta(days=25)
    renewal_60 = today + timedelta(days=60)
    renewal_90 = today + timedelta(days=90)
    expired_date = today - timedelta(days=30)

    fixture: dict[str, Any] = {"renewal_within_30_count": 2}

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO slmct.organisations (code, name, is_active)
            VALUES (%s, %s, true)
            RETURNING id
            """,
            (EVAL_ORG_CODE, EVAL_ORG_NAME),
        )
        org_id = cur.fetchone()["id"]
        fixture["org_id"] = str(org_id)

        cur.execute(
            """
            INSERT INTO slmct.vendors (organisation_id, name, status)
            VALUES (%s, 'Eval Vendor Alpha', 'active')
            RETURNING id
            """,
            (org_id,),
        )
        vendor_id = cur.fetchone()["id"]
        fixture["vendor_id"] = str(vendor_id)

        cur.execute(
            """
            INSERT INTO slmct.people (organisation_id, employee_number, full_name, work_email, department, status)
            VALUES
              (%s, 'EVAL-001', 'Eval Person Active', %s, 'Engineering', 'active'),
              (%s, 'EVAL-002', 'Eval Person Inactive', %s, 'Design', 'inactive')
            RETURNING id, full_name
            """,
            (
                org_id,
                f"eval.active{EVAL_EMAIL_DOMAIN}",
                org_id,
                f"eval.inactive{EVAL_EMAIL_DOMAIN}",
            ),
        )
        people = cur.fetchall()
        active_person_id = people[0]["id"]
        inactive_person_id = people[1]["id"]
        fixture["active_person_name"] = people[0]["full_name"]
        fixture["inactive_person_id"] = str(inactive_person_id)

        subscriptions_spec = [
            ("EvalSub Eng Slack", "Engineering", "active", 1000, renewal_15),
            ("EvalSub Eng Zoom", "Engineering", "active", 500, renewal_25),
            ("EvalSub Eng Jira", "Engineering", "active", 2000, renewal_90),
            ("EvalSub Eng GitHub", "Engineering", "active", 800, renewal_90),
            ("EvalSub Eng Notion", "Engineering", "active", 600, renewal_90),
            ("EvalSub Design Figma", "Design", "active", 1500, renewal_60),
            ("EvalSub Design Adobe", "Design", "active", 3000, renewal_60),
            ("EvalSub Eng Cancelled A", "Engineering", "cancelled", 400, None),
            ("EvalSub Design Cancelled B", "Design", "cancelled", 700, None),
            ("EvalSub Design Cancelled C", "Design", "cancelled", 900, None),
            ("EvalSub Eng Expired", "Engineering", "expired", 300, expired_date),
            ("EvalSub Design Trial", "Design", "trial", 250, None),
        ]
        sub_ids: dict[str, str] = {}
        for name, department, status, amount, renewal in subscriptions_spec:
            cur.execute(
                """
                INSERT INTO slmct.subscriptions (
                  organisation_id, vendor_id, name, department, billing_cycle,
                  amount, currency_code, status, renewal_date
                )
                VALUES (%s, %s, %s, %s, 'monthly', %s, 'AED', %s, %s)
                RETURNING id
                """,
                (org_id, vendor_id, name, department, amount, status, renewal),
            )
            sub_ids[name] = str(cur.fetchone()["id"])
        fixture["figma_sub_id"] = sub_ids["EvalSub Design Figma"]

        cur.execute(
            """
            INSERT INTO slmct.budgets (organisation_id, fiscal_year, department, allocated_amount, currency_code, status)
            VALUES
              (%s, 2026, 'Engineering', 50000, 'AED', 'approved'),
              (%s, 2027, 'Engineering', 60000, 'AED', 'approved')
            """,
            (org_id, org_id),
        )

        licences_spec = [
            ("EvalLic Figma Pro", sub_ids["EvalSub Design Figma"], active_person_id, "assigned", today + timedelta(days=365)),
            ("EvalLic Expired Seat", sub_ids["EvalSub Eng Slack"], None, "expired", expired_date),
            ("EvalLic Inactive A", sub_ids["EvalSub Eng Zoom"], inactive_person_id, "assigned", today + timedelta(days=180)),
            ("EvalLic Inactive B", sub_ids["EvalSub Eng Jira"], inactive_person_id, "assigned", today + timedelta(days=180)),
            ("EvalLic Available Slack", sub_ids["EvalSub Eng Slack"], None, "available", today + timedelta(days=365)),
        ]
        for licence_name, subscription_id, person_id, status, expires_at in licences_spec:
            cur.execute(
                """
                INSERT INTO slmct.licences (
                  organisation_id, subscription_id, assigned_to_person_id,
                  licence_name, status, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (org_id, subscription_id, person_id, licence_name, status, expires_at),
            )

        payments_spec = [
            ("EvalPay Alpha", 1000, "paid"),
            ("EvalPay Beta", 2000, "paid"),
            ("EvalPay Gamma", 500, "pending"),
            ("EvalPay Delta", 750, "paid"),
        ]
        for pay_name, amount, status in payments_spec:
            cur.execute(
                """
                INSERT INTO slmct.payments (
                  organisation_id, vendor_id, name, amount, currency_code, status, payment_date, reference
                )
                VALUES (%s, %s, %s, %s, 'AED', %s, CURRENT_DATE, %s)
                """,
                (org_id, vendor_id, pay_name, amount, status, pay_name.replace(" ", "-").upper()),
            )

        for wf_name, module in (
            ("Eval Workflow Pending A", "subscriptions"),
            ("Eval Workflow Pending B", "budgets"),
        ):
            cur.execute(
                """
                INSERT INTO slmct.workflow_requests (
                  organisation_id, requested_module, requested_action, payload, status,
                  requested_by_email, notes, workflow_type
                )
                VALUES (%s, %s, 'create', %s, 'submitted', %s, %s, 'generic_procurement')
                RETURNING id
                """,
                (
                    org_id,
                    module,
                    Json({"name": wf_name, "notes": wf_name}),
                    f"eval.requester{EVAL_EMAIL_DOMAIN}",
                    wf_name,
                ),
            )

        audit_specs = [
            ("create", "subscription", "Eval audit: subscription created"),
            ("update", "budget", "Eval audit: budget updated"),
            ("approve", "workflow_request", "Eval audit: workflow approved"),
        ]
        for action, entity_type, note in audit_specs:
            cur.execute(
                """
                INSERT INTO slmct.audit_logs (organisation_id, action, entity_type, metadata)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (org_id, action, entity_type, f'{{"note":"{note}","source":"copilot_eval_seed"}}'),
            )
        fixture["audit_note"] = audit_specs[-1][2]

    conn.commit()
    return fixture


def build_copilot_state(conn: Connection, org_id: str) -> dict[str, Any]:
    org_uuid = UUID(org_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, currency_code FROM slmct.organisations WHERE id = %s",
            (org_uuid,),
        )
        org = cur.fetchone()

        cur.execute(
            """
            SELECT name, category, amount, currency_code, renewal_date, billing_cycle, status, department
            FROM slmct.subscriptions WHERE organisation_id = %s ORDER BY name
            """,
            (org_uuid,),
        )
        subscriptions = [_row_dict(row) for row in cur.fetchall()]

        cur.execute(
            """
            SELECT l.licence_name, l.status, l.expires_at, l.assigned_to_person_id,
                   p.full_name AS assignee_name
            FROM slmct.licences l
            LEFT JOIN slmct.people p ON p.id = l.assigned_to_person_id
            WHERE l.organisation_id = %s ORDER BY l.licence_name
            """,
            (org_uuid,),
        )
        licences = []
        for row in cur.fetchall():
            licences.append(
                {
                    "name": row["licence_name"],
                    "status": row["status"],
                    "expires_at": str(row["expires_at"]) if row["expires_at"] else None,
                    "is_assigned": row["assigned_to_person_id"] is not None,
                    "assignee_name": row["assignee_name"],
                }
            )

        cur.execute(
            """
            SELECT department, allocated_amount, currency_code, status, fiscal_year
            FROM slmct.budgets WHERE organisation_id = %s ORDER BY fiscal_year, department
            """,
            (org_uuid,),
        )
        budgets = [_row_dict(row) for row in cur.fetchall()]

        cur.execute(
            """
            SELECT name, due_date, amount, currency_code, status, reference
            FROM slmct.payments WHERE organisation_id = %s ORDER BY name
            """,
            (org_uuid,),
        )
        payments = [_row_dict(row) for row in cur.fetchall()]

        cur.execute(
            """
            SELECT full_name, department, job_title, status
            FROM slmct.people WHERE organisation_id = %s ORDER BY full_name
            """,
            (org_uuid,),
        )
        employees = [_row_dict(row) for row in cur.fetchall()]

        cur.execute(
            "SELECT name, status FROM slmct.vendors WHERE organisation_id = %s ORDER BY name",
            (org_uuid,),
        )
        vendors = [_row_dict(row) for row in cur.fetchall()]

        cur.execute(
            """
            SELECT status, requested_module, workflow_type, notes
            FROM slmct.workflow_requests WHERE organisation_id = %s ORDER BY created_at
            """,
            (org_uuid,),
        )
        workflows = [_row_dict(row) for row in cur.fetchall()]

        cur.execute(
            """
            SELECT action, entity_type, metadata, created_at
            FROM slmct.audit_logs WHERE organisation_id = %s ORDER BY created_at
            """,
            (org_uuid,),
        )
        audit_logs = [_row_dict(row) for row in cur.fetchall()]

    return {
        "selectedOrganisation": {
            "id": str(org["id"]),
            "name": org["name"],
            "currency": org.get("currency_code") or "AED",
        },
        "vendors": vendors,
        "subscriptions": subscriptions,
        "licences": licences,
        "budgets": budgets,
        "payments": payments,
        "employees": employees,
        "workflows": workflows,
        "audit_logs": audit_logs,
        "organisation_id": org_id,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _row_dict(row) -> dict[str, Any]:
    return {key: _json_safe(row[key]) for key in row.keys()}


def _sql_scalar(conn: Connection, query: str, params: tuple) -> Any:
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        if not row:
            return None
        return next(iter(row.values()))


class EvalQuestion:
    def __init__(
        self,
        question_id: str,
        question: str,
        checker: Callable[[Connection, str, str, dict[str, Any]], tuple[bool, str]],
        *,
        holdout: bool = False,
        requires_no_tool_calls: bool = False,
        requires_tool_calls: list[str] | None = None,
    ):
        self.question_id = question_id
        self.question = question
        self.checker = checker
        self.holdout = holdout
        self.requires_no_tool_calls = requires_no_tool_calls
        self.requires_tool_calls = list(requires_tool_calls or [])


def _build_questions() -> list[EvalQuestion]:
    today = date.today()
    renewal_cutoff = today + timedelta(days=30)

    def _resolve_params(params_factory, org_id: str) -> tuple:
        return params_factory(org_id) if callable(params_factory) else params_factory

    def check_number(sql: str, params_factory, label: str):
        def _golden(conn: Connection, org_id: str, _fixture: dict[str, Any]) -> str:
            expected = float(_sql_scalar(conn, sql, _resolve_params(params_factory, org_id)) or 0)
            if expected == int(expected):
                return f"The answer is {int(expected)}."
            return f"The answer is {expected:g}."

        def _checker(conn: Connection, org_id: str, answer: str, _fixture: dict[str, Any]) -> tuple[bool, str]:
            expected = float(_sql_scalar(conn, sql, _resolve_params(params_factory, org_id)) or 0)
            ok = _number_matches(answer, expected)
            detail = f"expected {label}={expected}, numbers_in_answer={_extract_answer_count_numbers(answer)}"
            return ok, detail

        _checker.golden = _golden  # type: ignore[attr-defined]
        return _checker

    def check_names(sql: str, params_factory, *, min_count: int | None = None):
        def _golden(conn: Connection, org_id: str, _fixture: dict[str, Any]) -> str:
            with conn.cursor() as cur:
                cur.execute(sql, _resolve_params(params_factory, org_id))
                names = [str(row[next(iter(row.keys()))]) for row in cur.fetchall()]
            if not names:
                return "No matching records were found in the database."
            return "The names are: " + ", ".join(names) + "."

        def _checker(conn: Connection, org_id: str, answer: str, _fixture: dict[str, Any]) -> tuple[bool, str]:
            with conn.cursor() as cur:
                cur.execute(sql, _resolve_params(params_factory, org_id))
                names = [str(row[next(iter(row.keys()))]) for row in cur.fetchall()]
            if min_count is not None:
                ok = _contains_any_name(answer, names) and len(names) >= min_count
            else:
                ok = _contains_all_names(answer, names) if names else False
            return ok, f"expected names: {names}"

        _checker.golden = _golden  # type: ignore[attr-defined]
        return _checker

    def check_honesty(forbidden: list[str] | None = None):
        def _checker(conn: Connection, org_id: str, answer: str, _fixture: dict[str, Any]) -> tuple[bool, str]:
            refused = _contains_any(answer, REFUSAL_PHRASES)
            banned = forbidden or list(FIXTURE_NAMES)
            invented = _contains_any_name(answer, banned)
            ok = refused and not invented
            return ok, f"refusal={refused}, invented_fixture={invented}"

        return _checker

    def check_meta():
        def _checker(conn: Connection, org_id: str, answer: str, _fixture: dict[str, Any]) -> tuple[bool, str]:
            has_platform = _contains_any(answer, PLATFORM_KEYWORDS)
            mentions_fixture = _contains_any_name(answer, list(FIXTURE_NAMES))
            ok = has_platform and not mentions_fixture and len(answer.strip()) > 80
            return ok, f"platform_keywords={has_platform}, fixture_leak={mentions_fixture}, len={len(answer)}"

        return _checker

    def check_interpretation():
        def _checker(conn: Connection, org_id: str, answer: str, _fixture: dict[str, Any]) -> tuple[bool, str]:
            ok = _contains_any(answer, INTERPRETATION_PHRASES) or len(answer.strip()) > 40
            return ok, "expects interpretation/clarification language"

        return _checker

    def check_text(required_phrase: str):
        def _golden(_conn: Connection, _org_id: str, _fixture: dict[str, Any]) -> str:
            return f"The audit log includes an action related to {required_phrase}."

        def _checker(conn: Connection, org_id: str, answer: str, _fixture: dict[str, Any]) -> tuple[bool, str]:
            ok = required_phrase.lower() in answer.lower()
            return ok, f"expected phrase: {required_phrase}"

        _checker.golden = _golden  # type: ignore[attr-defined]
        return _checker

    def check_clarify_or_refuse():
        def _checker(conn: Connection, org_id: str, answer: str, _fixture: dict[str, Any]) -> tuple[bool, str]:
            ok = _contains_any(answer, INTERPRETATION_PHRASES) or _contains_any(answer, REFUSAL_PHRASES)
            return ok, "expects clarification or a plain missing-data answer"

        return _checker

    def check_budget_vs_spend():
        def _checker(conn: Connection, org_id: str, answer: str, _fixture: dict[str, Any]) -> tuple[bool, str]:
            lower = answer.lower()
            has_eng = "engineering" in lower
            has_compare = any(
                token in lower
                for token in (
                    "variance",
                    "under budget",
                    "over budget",
                    "compared",
                    " vs ",
                    "versus",
                    "allocated",
                    "remaining",
                    "tracked spend",
                    "difference",
                )
            )
            has_numbers = (
                _number_matches(answer, 4900)
                or _number_matches(answer, 50000)
                or _number_matches(answer, 60000)
            )
            ok = has_eng and has_compare and has_numbers and len(answer.strip()) > 80
            return ok, f"eng={has_eng}, compare={has_compare}, numbers={has_numbers}, len={len(answer)}"

        return _checker

    def org_param(org_id: str) -> tuple:
        return (UUID(org_id),)

    return [
        EvalQuestion(
            "q01_active_sub_count",
            "How many active subscriptions are in the portfolio?",
            check_number(
                "SELECT COUNT(*) FROM slmct.subscriptions WHERE organisation_id = %s AND status = 'active'",
                org_param,
                "active_subscriptions",
            ),
            holdout=True,
        ),
        EvalQuestion(
            "q02_cancelled_sub_count",
            "How many cancelled subscriptions do we have?",
            check_number(
                "SELECT COUNT(*) FROM slmct.subscriptions WHERE organisation_id = %s AND status = 'cancelled'",
                org_param,
                "cancelled_subscriptions",
            ),
        ),
        EvalQuestion(
            "q03_renewing_within_30",
            "How many active subscriptions have a renewal date within the next 30 days?",
            check_number(
                """
                SELECT COUNT(*) FROM slmct.subscriptions
                WHERE organisation_id = %s AND status = 'active'
                  AND renewal_date IS NOT NULL
                  AND renewal_date <= %s
                """,
                (lambda org_id: (UUID(org_id), renewal_cutoff)),
                "renewing_within_30",
            ),
        ),
        EvalQuestion(
            "q03b_licences_expiring_30",
            "How many licences expire within the next 30 days?",
            check_number(
                """
                SELECT COUNT(*) FROM slmct.licences
                WHERE organisation_id = %s
                  AND status NOT IN ('revoked', 'expired')
                  AND expires_at IS NOT NULL
                  AND expires_at >= CURRENT_DATE
                  AND expires_at <= %s
                """,
                (lambda org_id: (UUID(org_id), renewal_cutoff)),
                "licences_expiring_30",
            ),
        ),
        EvalQuestion(
            "q04_eng_active_names",
            "List the names of all active subscriptions in the Engineering department.",
            check_names(
                """
                SELECT name FROM slmct.subscriptions
                WHERE organisation_id = %s AND department = 'Engineering' AND status = 'active'
                ORDER BY name
                """,
                org_param,
            ),
        ),
        EvalQuestion(
            "q05_eng_active_spend",
            "What is the total amount of all active Engineering subscriptions (sum of their amounts)?",
            check_number(
                """
                SELECT COALESCE(SUM(amount), 0) FROM slmct.subscriptions
                WHERE organisation_id = %s AND department = 'Engineering' AND status = 'active'
                """,
                org_param,
                "engineering_active_spend",
            ),
            holdout=True,
        ),
        EvalQuestion(
            "q06_design_active_count",
            "How many active subscriptions are in the Design department?",
            check_number(
                """
                SELECT COUNT(*) FROM slmct.subscriptions
                WHERE organisation_id = %s AND department = 'Design' AND status = 'active'
                """,
                org_param,
                "design_active_count",
            ),
        ),
        EvalQuestion(
            "q07_total_licences",
            "How many licences are recorded in total?",
            check_number(
                "SELECT COUNT(*) FROM slmct.licences WHERE organisation_id = %s",
                org_param,
                "licence_count",
            ),
        ),
        EvalQuestion(
            "q08_figma_assignee",
            "Who is the EvalLic Figma Pro licence assigned to?",
            check_text("Eval Person Active"),
            holdout=True,
        ),
        EvalQuestion(
            "q09_inactive_assignments",
            "How many licences are assigned to employees with inactive status?",
            check_number(
                """
                SELECT COUNT(*) FROM slmct.licences l
                JOIN slmct.people p ON p.id = l.assigned_to_person_id
                WHERE l.organisation_id = %s AND p.status = 'inactive'
                """,
                org_param,
                "inactive_assignments",
            ),
        ),
        EvalQuestion(
            "q10_expired_licences",
            "How many licences have expired status?",
            check_number(
                "SELECT COUNT(*) FROM slmct.licences WHERE organisation_id = %s AND status = 'expired'",
                org_param,
                "expired_licences",
            ),
        ),
        EvalQuestion(
            "q11_budget_2026",
            "What is the Engineering department budget allocated amount for fiscal year 2026?",
            check_number(
                """
                SELECT allocated_amount FROM slmct.budgets
                WHERE organisation_id = %s AND department = 'Engineering' AND fiscal_year = 2026
                """,
                org_param,
                "budget_2026",
            ),
        ),
        EvalQuestion(
            "q12_budget_2027",
            "What is the Engineering department budget allocated amount for fiscal year 2027?",
            check_number(
                """
                SELECT allocated_amount FROM slmct.budgets
                WHERE organisation_id = %s AND department = 'Engineering' AND fiscal_year = 2027
                """,
                org_param,
                "budget_2027",
            ),
            holdout=True,
        ),
        EvalQuestion(
            "q13_payment_count",
            "How many payments are recorded?",
            check_number(
                "SELECT COUNT(*) FROM slmct.payments WHERE organisation_id = %s",
                org_param,
                "payment_count",
            ),
        ),
        EvalQuestion(
            "q14_payment_total",
            "What is the total amount across all payments?",
            check_number(
                "SELECT COALESCE(SUM(amount), 0) FROM slmct.payments WHERE organisation_id = %s",
                org_param,
                "payment_total",
            ),
            holdout=True,
        ),
        EvalQuestion(
            "q15_pending_workflows",
            "How many workflow requests are still pending (status submitted)?",
            check_number(
                """
                SELECT COUNT(*) FROM slmct.workflow_requests
                WHERE organisation_id = %s AND status = 'submitted'
                """,
                org_param,
                "pending_workflows",
            ),
        ),
        EvalQuestion(
            "q16_workflow_modules",
            "Which requested modules appear in our pending workflow requests?",
            check_names(
                """
                SELECT DISTINCT requested_module FROM slmct.workflow_requests
                WHERE organisation_id = %s AND status = 'submitted'
                ORDER BY requested_module
                """,
                org_param,
                min_count=1,
            ),
        ),
        EvalQuestion(
            "q17_audit_count",
            "How many audit log entries exist for this organisation?",
            check_number(
                "SELECT COUNT(*) FROM slmct.audit_logs WHERE organisation_id = %s",
                org_param,
                "audit_count",
            ),
        ),
        EvalQuestion(
            "q18_audit_action",
            "What audit action type appears in the audit logs for workflow approvals in this org?",
            check_text("approve"),
            holdout=True,
        ),
        EvalQuestion(
            "q19_design_active_spend",
            "What is the total amount of active subscriptions in the Design department?",
            check_number(
                """
                SELECT COALESCE(SUM(amount), 0) FROM slmct.subscriptions
                WHERE organisation_id = %s AND department = 'Design' AND status = 'active'
                """,
                org_param,
                "design_active_spend",
            ),
        ),
        EvalQuestion(
            "q20_cancelled_names",
            "Name all cancelled subscriptions.",
            check_names(
                """
                SELECT name FROM slmct.subscriptions
                WHERE organisation_id = %s AND status = 'cancelled'
                ORDER BY name
                """,
                org_param,
            ),
            holdout=True,
        ),
        EvalQuestion(
            "q21_phantom_sub",
            "Tell me about our EvalSub Phantom SaaS subscription — seats, cost, and renewal date.",
            check_honesty(),
            holdout=True,
        ),
        EvalQuestion(
            "q22_nonexistent_name",
            "How many subscriptions are named Nonexistent Enterprise Tool?",
            check_honesty(forbidden=list(FIXTURE_NAMES)),
        ),
        EvalQuestion(
            "q23_meta_platform",
            "What does this platform do? Describe its purpose in 2-3 sentences.",
            check_meta(),
            holdout=True,
            requires_no_tool_calls=True,
        ),
        EvalQuestion(
            "q24_ambiguous_spend",
            "How much are we spending?",
            check_interpretation(),
        ),
        EvalQuestion(
            "q25_garbled_eng_count",
            "subs eng active count??",
            check_interpretation(),
        ),
        EvalQuestion(
            "q26_ambiguous_budget",
            "What's the budget for Marketing?",
            check_clarify_or_refuse(),
        ),
        EvalQuestion(
            "q27_trial_expired_subs",
            "How many subscriptions are in trial or expired status?",
            check_number(
                """
                SELECT COUNT(*) FROM slmct.subscriptions
                WHERE organisation_id = %s AND status IN ('trial', 'expired')
                """,
                org_param,
                "trial_or_expired",
            ),
        ),
        EvalQuestion(
            "q28_vendor_name",
            "What is the name of our vendor?",
            check_text("Eval Vendor Alpha"),
        ),
        EvalQuestion(
            "q29_paid_payments",
            "How many payments have status paid?",
            check_number(
                "SELECT COUNT(*) FROM slmct.payments WHERE organisation_id = %s AND status = 'paid'",
                org_param,
                "paid_payments",
            ),
        ),
        EvalQuestion(
            "q30_ambiguous_licences",
            "licences figma slack seats?",
            check_interpretation(),
            holdout=True,
        ),
        EvalQuestion(
            "q31_budget_vs_spend",
            "Analyze budget allocations vs tracked spend by department",
            check_budget_vs_spend(),
            requires_tool_calls=["get_budgets", "get_spend_summary"],
        ),
    ]


def _score_subset(checks: list[dict], holdout: bool | None) -> dict[str, Any]:
    if holdout is None:
        subset = [c for c in checks if c.get("kind") == "question"]
    elif holdout:
        subset = [c for c in checks if c.get("kind") == "question" and c.get("holdout")]
    else:
        subset = [c for c in checks if c.get("kind") == "question" and not c.get("holdout")]
    total = len(subset)
    passed = sum(1 for c in subset if c["status"] == "pass")
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "score": round(passed / total, 4) if total else 0.0,
    }


def build_eval_goldens(conn: Connection, org_id: str, fixture: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Build train/test goldens for Not Diamond prompt optimization."""
    train: list[dict] = []
    test: list[dict] = []
    for question in _build_questions():
        checker = question.checker
        if hasattr(checker, "golden"):
            answer = checker.golden(conn, org_id, fixture)  # type: ignore[attr-defined]
        elif question.question_id == "q23_meta_platform":
            answer = (
                "Derisk360 SLMCT is a software license management and compliance platform. "
                "It helps organizations track subscriptions, licences, budgets, payments, and procurement workflows."
            )
        elif question.question_id in ("q21_phantom_sub", "q22_nonexistent_name"):
            answer = "I don't have any record of that in the database."
        elif question.question_id in ("q24_ambiguous_spend", "q25_garbled_eng_count", "q26_ambiguous_budget", "q30_ambiguous_licences"):
            answer = "I'll interpret your question as referring to the organisation's software estate and answer using available data."
        else:
            answer = "Answer using SQL-backed tool results only; do not invent data."

        sample = {"fields": {"question": question.question}, "answer": answer}
        (test if question.holdout else train).append(sample)
    return train, test


def _count_logged_tool_calls(conn: Connection, since) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM slmct.llm_calls
            WHERE task = 'copilot'
              AND created_at >= %s
              AND jsonb_array_length(COALESCE(metadata->'function_calls', '[]'::jsonb)) > 0
            """,
            (since,),
        )
        row = cur.fetchone()
    return int(row["cnt"]) if row else 0


def _llm_cost_summary(conn: Connection, since: datetime) -> dict[str, Any]:
    """Aggregate copilot llm_calls since a timestamp (Phase 1d cost report)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) AS calls,
              COALESCE(SUM(input_tokens), 0) AS input_tokens,
              COALESCE(SUM(output_tokens), 0) AS output_tokens,
              COALESCE(SUM(total_tokens), 0) AS total_tokens,
              COUNT(*) FILTER (WHERE metadata->>'routed_by' = 'notdiamond') AS routed_calls
            FROM slmct.llm_calls
            WHERE task = 'copilot' AND created_at >= %s
            """,
            (since,),
        )
        totals = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT model,
                   COUNT(*) AS calls,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM slmct.llm_calls
            WHERE task = 'copilot' AND created_at >= %s
            GROUP BY model
            ORDER BY calls DESC
            """,
            (since,),
        )
        by_model = [dict(row) for row in cur.fetchall()]
    return {
        "calls": int(totals.get("calls") or 0),
        "input_tokens": int(totals.get("input_tokens") or 0),
        "output_tokens": int(totals.get("output_tokens") or 0),
        "total_tokens": int(totals.get("total_tokens") or 0),
        "routed_calls": int(totals.get("routed_calls") or 0),
        "by_model": by_model,
    }


def _llm_calls_for_question(conn: Connection, since: datetime) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT model, total_tokens, metadata
            FROM slmct.llm_calls
            WHERE task = 'copilot' AND created_at >= %s
            ORDER BY created_at
            """,
            (since,),
        )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        out.append(
            {
                "model": row["model"],
                "total_tokens": int(row["total_tokens"] or 0),
                "routed_by": meta.get("routed_by"),
            }
        )
    return out


def _run_question_suite(
    conn: Connection,
    *,
    base_url: str,
    org_id: str,
    fixture: dict[str, Any],
    state: dict[str, Any],
    questions: list[Any],
    copilot_mode: str,
    prompt_variant: str,
    model_override: str | None,
    label_prefix: str = "",
) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """Run all eval questions once; return checks, routing report, and cost summary."""
    import httpx

    checks: list[dict] = []
    routing_report: list[dict] = []
    suite_started = datetime.now(timezone.utc)
    prefix = f"{label_prefix} " if label_prefix else ""

    def chk(name: str, passed: bool, detail: str = "", **extra):
        checks.append(
            {
                "name": name,
                "status": "pass" if passed else "fail",
                "detail": detail,
                **extra,
            }
        )

    for question in questions:
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": question.question, "id": question.question_id}],
            "prompt_variant": prompt_variant,
            "copilot_mode": copilot_mode,
        }
        if copilot_mode == "legacy":
            body["state"] = state
        else:
            body["organisation_id"] = org_id
            body["actor_roles"] = ["master_admin"]
        if model_override:
            body["model"] = model_override

        call_started = datetime.now(timezone.utc)
        try:
            timeout = 180 if copilot_mode == "tools" else 90
            response = httpx.post(f"{base_url}/api/copilot", json=body, timeout=timeout)
            if response.status_code != 200:
                chk(
                    f"{prefix}Copilot eval [{question.question_id}]",
                    False,
                    f"HTTP {response.status_code}: {response.text[:200]}",
                    kind="question",
                    holdout=question.holdout,
                    question=question.question,
                    suite=label_prefix or "default",
                )
                continue
            data = response.json()
            answer = str(data.get("responseText") or "")
            used_model = str(data.get("model") or model_override or "unknown")
            tool_calls = list(data.get("toolCalls") or [])
            llm_calls = _llm_calls_for_question(conn, call_started)
            models_used = [str(c["model"]) for c in llm_calls] or [used_model]
            routed_by = next((c.get("routed_by") for c in llm_calls if c.get("routed_by")), None)
            routing_report.append(
                {
                    "question_id": question.question_id,
                    "model": used_model,
                    "models_used": models_used,
                    "routed_by": routed_by,
                    "llm_calls": len(llm_calls),
                    "tokens": sum(int(c.get("total_tokens") or 0) for c in llm_calls),
                    "mode": str(data.get("mode") or copilot_mode),
                    "tool_calls": tool_calls,
                    "suite": label_prefix or "default",
                }
            )
            passed, detail = question.checker(conn, org_id, answer, fixture)
            chk(
                f"{prefix}Copilot eval [{question.question_id}]",
                passed,
                detail,
                kind="question",
                holdout=question.holdout,
                question=question.question,
                answer_preview=answer[:240],
                model=used_model,
                tool_calls=tool_calls,
                suite=label_prefix or "default",
            )
            if question.requires_no_tool_calls:
                logged_tool_turns = _count_logged_tool_calls(conn, call_started)
                no_tools = len(tool_calls) == 0 and logged_tool_turns == 0
                chk(
                    f"{prefix}Copilot eval [{question.question_id} no_tool_calls]",
                    no_tools,
                    f"response_toolCalls={tool_calls}, llm_logged_tool_turns={logged_tool_turns}",
                    kind="question",
                    holdout=question.holdout,
                    suite=label_prefix or "default",
                )
            if question.requires_tool_calls:
                missing = [name for name in question.requires_tool_calls if name not in tool_calls]
                chk(
                    f"{prefix}Copilot eval [{question.question_id} tool_calls]",
                    not missing,
                    f"required={question.requires_tool_calls}, got={tool_calls}, missing={missing}",
                    kind="question",
                    holdout=question.holdout,
                    suite=label_prefix or "default",
                )
        except Exception as exc:
            chk(
                f"{prefix}Copilot eval [{question.question_id}]",
                False,
                str(exc)[:200],
                kind="question",
                holdout=question.holdout,
                question=question.question,
                suite=label_prefix or "default",
            )

    return checks, routing_report, _llm_cost_summary(conn, suite_started)


def run_routing_benchmark(conn: Connection, payload: dict | None = None) -> dict[str, Any]:
    """
    Phase 1d — run eval twice on one fixture: single strong model vs ND-routed copilot.
    Requires NOTDIAMOND_API_KEY + NOTDIAMOND_ROUTING_ENABLED on the API container.
    """
    from app.llm import get_primary_copilot_model, notdiamond_routing_active
    from app.settings import get_settings

    payload = payload or {}
    settings = get_settings()
    base_url = str(payload.get("base_url") or "http://localhost:8000")
    prompt_variant = str(payload.get("prompt_variant") or "default")
    copilot_mode = str(payload.get("copilot_mode") or "tools").strip().lower()
    if copilot_mode not in ("tools", "legacy"):
        copilot_mode = "tools"
    primary_model = str(payload.get("baseline_model") or get_primary_copilot_model())
    checks: list[dict] = []

    def chk(name: str, passed: bool, detail: str = "", *, warning: bool = False, **extra):
        checks.append(
            {
                "name": name,
                "status": "pass" if passed else ("warn" if warning else "fail"),
                "detail": detail,
                **extra,
            }
        )

    if not settings.gemini_api_key:
        return {
            "status": "skipped",
            "reason": "GEMINI_API_KEY not configured.",
            "mode": "routing_benchmark",
            "checks": [],
            "summary": _eval_summary([]),
        }

    if not notdiamond_routing_active():
        return {
            "status": "skipped",
            "reason": "Routing benchmark requires NOTDIAMOND_API_KEY and NOTDIAMOND_ROUTING_ENABLED=true on the API.",
            "mode": "routing_benchmark",
            "checks": [],
            "summary": _eval_summary([]),
        }

    org_id: str | None = None
    try:
        _cleanup_eval_org(conn)
        fixture = _seed_eval_org(conn)
        org_id = fixture["org_id"]
        chk("Seed: eval organisation created", True, f"org_id={org_id}")

        state = build_copilot_state(conn, org_id)
        questions = _build_questions()

        baseline_checks, _, baseline_cost = _run_question_suite(
            conn,
            base_url=base_url,
            org_id=org_id,
            fixture=fixture,
            state=state,
            questions=questions,
            copilot_mode=copilot_mode,
            prompt_variant=prompt_variant,
            model_override=primary_model,
            label_prefix="Baseline",
        )
        checks.extend(baseline_checks)

        routed_checks, routing_report, routed_cost = _run_question_suite(
            conn,
            base_url=base_url,
            org_id=org_id,
            fixture=fixture,
            state=state,
            questions=questions,
            copilot_mode=copilot_mode,
            prompt_variant=prompt_variant,
            model_override=None,
            label_prefix="Routed",
        )
        checks.extend(routed_checks)

        baseline_scores = {
            "main": _score_subset(baseline_checks, holdout=False),
            "holdout": _score_subset(baseline_checks, holdout=True),
            "all": _score_subset(baseline_checks, holdout=None),
        }
        routed_scores = {
            "main": _score_subset(routed_checks, holdout=False),
            "holdout": _score_subset(routed_checks, holdout=True),
            "all": _score_subset(routed_checks, holdout=None),
        }
        baseline_passed = int(baseline_scores["all"]["passed"])
        routed_passed = int(routed_scores["all"]["passed"])
        score_ok = routed_passed >= baseline_passed
        token_delta = int(routed_cost["total_tokens"]) - int(baseline_cost["total_tokens"])

        chk(
            "Routing benchmark: routed score >= single-model baseline",
            score_ok,
            f"baseline={baseline_passed}/{baseline_scores['all']['total']}, "
            f"routed={routed_passed}/{routed_scores['all']['total']}",
        )
        chk(
            "Routing benchmark: ND routed at least one copilot call",
            int(routed_cost["routed_calls"]) > 0,
            f"routed_calls={routed_cost['routed_calls']}, total_calls={routed_cost['calls']}",
        )
        model_mix = ", ".join(f"{row['model']}:{row['calls']}" for row in routed_cost.get("by_model") or [])
        chk(
            "Routing benchmark: cost logged to llm_calls",
            int(routed_cost["calls"]) > 0,
            f"baseline_tokens={baseline_cost['total_tokens']}, routed_tokens={routed_cost['total_tokens']}, "
            f"delta={token_delta:+d}, routed_models=[{model_mix}]",
        )
        cost_ok = token_delta <= 0 or int(routed_cost.get("routed_calls") or 0) > 0
        chk(
            "Routing benchmark: routed cost acceptable (<= baseline or ND routing active)",
            cost_ok,
            f"token_delta={token_delta:+d}, routed_calls={routed_cost.get('routed_calls', 0)}",
            warning=not cost_ok,
        )

        if org_id:
            _cleanup_eval_org(conn)
            chk("Teardown: eval organisation removed", True, f"org_id={org_id}")

        return {
            "mode": "routing_benchmark",
            "status": "ok",
            "checks": checks,
            "summary": _eval_summary(checks),
            "baseline": {
                "model": primary_model,
                "scores": baseline_scores,
                "cost": baseline_cost,
            },
            "routed": {
                "scores": routed_scores,
                "cost": routed_cost,
                "routing_report": routing_report,
            },
            "comparison": {
                "score_ok": score_ok,
                "baseline_passed": baseline_passed,
                "routed_passed": routed_passed,
                "token_delta": token_delta,
                "cheaper_or_equal_tokens": token_delta <= 0,
            },
            "prompt_variant": prompt_variant,
            "copilot_mode": copilot_mode,
            "use_nd_routing": True,
        }
    except Exception as exc:
        if org_id:
            try:
                _cleanup_eval_org(conn)
            except Exception:
                pass
        chk("Routing benchmark: suite execution", False, str(exc)[:300])
        return {
            "mode": "routing_benchmark",
            "status": "error",
            "checks": checks,
            "summary": _eval_summary(checks),
            "error": str(exc),
        }


def run_copilot_eval(conn: Connection, payload: dict | None = None) -> dict[str, Any]:
    from app.llm import notdiamond_routing_active
    from app.settings import get_settings

    payload = payload or {}
    if bool(payload.get("routing_benchmark")):
        return run_routing_benchmark(conn, payload)

    settings = get_settings()
    checks: list[dict] = []
    base_url = str(payload.get("base_url") or "http://localhost:8000")
    prompt_variant = str(payload.get("prompt_variant") or "default")
    use_nd_routing = bool(payload.get("use_nd_routing"))
    model_override = None if use_nd_routing else payload.get("model")
    copilot_mode = str(payload.get("copilot_mode") or "tools").strip().lower()
    if copilot_mode not in ("tools", "legacy"):
        copilot_mode = "tools"

    def chk(name: str, passed: bool, detail: str = "", *, warning: bool = False, **extra):
        entry = {
            "name": name,
            "status": "pass" if passed else ("warn" if warning else "fail"),
            "detail": detail,
            **extra,
        }
        checks.append(entry)

    skip_response = {
        "summary": {"total": 0, "passed": 0, "failed": 0, "warned": 0, "skipped": 0, "overall": "skip"},
        "scores": {
            "main": _score_subset([], False),
            "holdout": _score_subset([], True),
            "all": _score_subset([], None),
        },
        "prompt_variant": prompt_variant,
        "model": model_override,
        "copilot_mode": copilot_mode,
        "use_nd_routing": use_nd_routing,
        "routing_report": [],
    }

    if not settings.gemini_api_key:
        return {
            **skip_response,
            "status": "skipped",
            "reason": "GEMINI_API_KEY not configured — copilot eval requires a live LLM.",
            "checks": [],
        }

    if use_nd_routing and not notdiamond_routing_active():
        return {
            **skip_response,
            "status": "skipped",
            "reason": "use_nd_routing requires NOTDIAMOND_API_KEY and NOTDIAMOND_ROUTING_ENABLED=true on the API.",
            "model": None,
            "use_nd_routing": True,
            "checks": [],
        }

    org_id: str | None = None
    eval_started = datetime.now(timezone.utc)
    try:
        _cleanup_eval_org(conn)
        fixture = _seed_eval_org(conn)
        org_id = fixture["org_id"]
        chk("Seed: eval organisation created", True, f"org_id={org_id}")

        state = build_copilot_state(conn, org_id)
        chk(
            "Seed: fixture counts",
            len(state.get("subscriptions") or []) == 12 and len(state.get("licences") or []) == 5,
            f"subscriptions={len(state.get('subscriptions') or [])}, licences={len(state.get('licences') or [])}",
        )

        questions = _build_questions()
        suite_label = "Routed" if use_nd_routing else "Eval"
        question_checks, routing_report, _suite_cost = _run_question_suite(
            conn,
            base_url=base_url,
            org_id=org_id,
            fixture=fixture,
            state=state,
            questions=questions,
            copilot_mode=copilot_mode,
            prompt_variant=prompt_variant,
            model_override=str(model_override) if model_override else None,
            label_prefix=suite_label,
        )
        checks.extend(question_checks)

        if org_id:
            _cleanup_eval_org(conn)
            chk("Teardown: eval organisation removed", True, f"org_id={org_id}")

        cost = _llm_cost_summary(conn, eval_started)
        main_score = _score_subset(checks, holdout=False)
        holdout_score = _score_subset(checks, holdout=True)
        all_score = _score_subset(checks, holdout=None)
        summary = _eval_summary(checks)
        return {
            "checks": checks,
            "summary": summary,
            "scores": {
                "main": main_score,
                "holdout": holdout_score,
                "all": all_score,
            },
            "prompt_variant": prompt_variant,
            "model": model_override,
            "copilot_mode": copilot_mode,
            "use_nd_routing": use_nd_routing,
            "routing_report": routing_report,
            "cost": cost,
            "baseline_note": (
                "Phase 1d: POST with routing_benchmark=true to compare single-model vs ND-routed runs."
            ),
        }
    except Exception as exc:
        if org_id:
            try:
                _cleanup_eval_org(conn)
            except Exception:
                pass
        chk("Copilot eval: suite execution", False, str(exc)[:300])
        return {
            "checks": checks,
            "summary": _eval_summary(checks),
            "scores": {
                "main": _score_subset(checks, holdout=False),
                "holdout": _score_subset(checks, holdout=True),
                "all": _score_subset(checks, holdout=None),
            },
            "prompt_variant": prompt_variant,
            "model": model_override,
            "copilot_mode": copilot_mode,
            "routing_report": [],
            "cost": _llm_cost_summary(conn, eval_started),
            "error": str(exc),
        }


def _eval_summary(checks: list[dict]) -> dict[str, Any]:
    active = [c for c in checks if c["status"] != "skip"]
    total = len(active)
    passed = sum(1 for c in active if c["status"] == "pass")
    failed = sum(1 for c in active if c["status"] == "fail")
    warned = sum(1 for c in active if c["status"] == "warn")
    skipped = sum(1 for c in checks if c["status"] == "skip")
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "warned": warned,
        "skipped": skipped,
        "overall": "pass" if failed == 0 else "fail",
    }
