"""Tool registry for Copilot read access and future Operator write tools."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID

from psycopg import Connection

FINANCE_ROLES = {"master_admin", "finance", "auditor"}
AUDIT_ROLES = {"master_admin", "auditor"}
ALL_COPILOT_ROLES = frozenset(
    {"master_admin", "finance", "it_admin", "hr_admin", "auditor", "line_manager", "employee"}
)
IT_READ_ROLES = frozenset({"master_admin", "it_admin", "finance", "auditor"})


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    required_roles: frozenset[str]
    handler: Callable[..., Any]

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def to_gemini_declaration(self):
        from google.genai import types as gtypes

        return gtypes.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


def _normalize_roles(actor_roles: list[str] | None) -> set[str]:
    if not actor_roles:
        return set()
    return {str(role).strip().lower() for role in actor_roles if str(role).strip()}


def _json_safe(value: Any) -> Any:
    """Recursively convert DB types (UUID, Decimal, dates) for JSON / Gemini tool responses."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return value


def _rows_to_dicts(rows: list) -> list[dict[str, Any]]:
    return [{key: _json_safe(row[key]) for key in row.keys()} for row in rows]


def _org_uuid(organisation_id: str | UUID) -> UUID:
    return organisation_id if isinstance(organisation_id, UUID) else UUID(str(organisation_id))


def _actor_has_any(actor_roles: list[str] | None, allowed: set[str]) -> bool:
    roles = _normalize_roles(actor_roles)
    return bool(roles.intersection(allowed)) or "master_admin" in roles


def get_subscriptions(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    status: str | None = None,
    statuses: str | None = None,
    department: str | None = None,
    renewal_before: str | None = None,
    renewal_after: str | None = None,
    renewing_within_days: int | None = None,
    name: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    clauses = ["organisation_id = %s"]
    params: list[Any] = [org_id]
    if statuses:
        parts = [s.strip() for s in str(statuses).split(",") if s.strip()]
        if parts:
            clauses.append("status = ANY(%s)")
            params.append(parts)
    elif status:
        clauses.append("status = %s")
        params.append(status)
    if department:
        clauses.append("department = %s")
        params.append(department)
    if renewing_within_days is not None:
        from datetime import date, timedelta

        cutoff = date.today() + timedelta(days=int(renewing_within_days))
        clauses.append("status = 'active'")
        clauses.append("renewal_date IS NOT NULL AND renewal_date <= %s")
        params.append(cutoff.isoformat())
    elif renewal_before:
        clauses.append("renewal_date IS NOT NULL AND renewal_date <= %s")
        params.append(renewal_before)
    if renewal_after:
        clauses.append("renewal_date IS NOT NULL AND renewal_date >= %s")
        params.append(renewal_after)
    if name:
        clauses.append("name ILIKE %s")
        params.append(f"%{str(name).strip()}%")
    params.append(min(max(int(limit), 1), 500))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, name, department, status, amount, currency_code, billing_cycle,
                   renewal_date, vendor_id, category
            FROM slmct.subscriptions
            WHERE {" AND ".join(clauses)}
            ORDER BY name
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "subscriptions": rows}


def get_vendors(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    status: str | None = "active",
    name: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    clauses = ["organisation_id = %s"]
    params: list[Any] = [org_id]
    if status:
        clauses.append("status = %s")
        params.append(status)
    if name:
        clauses.append("name ILIKE %s")
        params.append(f"%{str(name).strip()}%")
    params.append(min(max(int(limit), 1), 500))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, name, legal_name, status, website_url, contact_name, contact_email,
                   soc2_certified, iso27001_certified, gdpr_compliant, data_residency
            FROM slmct.vendors
            WHERE {" AND ".join(clauses)}
            ORDER BY name
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "vendors": rows}


def get_licences(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    include_assignee: bool = True,
    status: str | None = None,
    assignee_status: str | None = None,
    licence_name: str | None = None,
    expiring_within_days: int | None = None,
    exclude_revoked: bool = True,
    order_by: str = "name",
    limit: int = 200,
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    clauses = ["l.organisation_id = %s"]
    params: list[Any] = [org_id]
    if status:
        clauses.append("l.status = %s")
        params.append(status)
    elif exclude_revoked:
        clauses.append("l.status NOT IN ('revoked', 'expired')")
    if assignee_status:
        clauses.append("p.status = %s")
        params.append(assignee_status)
    if licence_name:
        clauses.append("l.licence_name ILIKE %s")
        params.append(f"%{licence_name.strip()}%")
    if expiring_within_days is not None:
        from datetime import date, timedelta

        cutoff = date.today() + timedelta(days=int(expiring_within_days))
        clauses.append("l.expires_at IS NOT NULL")
        clauses.append("l.expires_at <= %s")
        params.append(cutoff.isoformat())
        clauses.append("l.expires_at >= CURRENT_DATE")
    order_key = str(order_by or "name").strip().lower()
    if order_key == "assigned_at_desc":
        order_sql = "l.assigned_at DESC NULLS LAST, l.created_at DESC, l.licence_name"
    elif order_key == "created_at_desc":
        order_sql = "l.created_at DESC NULLS LAST, l.licence_name"
    elif order_key == "name":
        order_sql = "l.licence_name"
    else:
        order_sql = "l.licence_name"
    params.append(min(max(int(limit), 1), 500))
    assignee_sql = ", p.full_name AS assignee_name, p.work_email AS assignee_email, p.status AS assignee_status" if include_assignee else ""
    join_sql = "LEFT JOIN slmct.people p ON p.id = l.assigned_to_person_id"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT l.id, l.licence_name, l.status, l.expires_at, l.subscription_id,
                   l.assigned_to_person_id, l.assigned_at, l.created_at{assignee_sql}
            FROM slmct.licences l
            {join_sql}
            WHERE {" AND ".join(clauses)}
            ORDER BY {order_sql}
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "licences": rows, "order_by": order_key}


def get_budgets(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    fiscal_year: int | None = None,
    department: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    if not _actor_has_any(actor.get("roles"), FINANCE_ROLES):
        return {"error": "You do not have permission to view budgets."}
    org_id = _org_uuid(organisation_id)
    clauses = ["organisation_id = %s", "status != 'closed'"]
    params: list[Any] = [org_id]
    if fiscal_year is not None:
        clauses.append("fiscal_year = %s")
        params.append(int(fiscal_year))
    if department:
        clauses.append("department = %s")
        params.append(department)
    params.append(min(max(int(limit), 1), 200))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, department, fiscal_year, allocated_amount, currency_code, status
            FROM slmct.budgets
            WHERE {" AND ".join(clauses)}
            ORDER BY fiscal_year DESC, department
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "budgets": rows}


def get_payments(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    status: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    if not _actor_has_any(actor.get("roles"), FINANCE_ROLES):
        return {"error": "You do not have permission to view payments."}
    org_id = _org_uuid(organisation_id)
    clauses = ["organisation_id = %s"]
    params: list[Any] = [org_id]
    if status:
        clauses.append("status = %s")
        params.append(status)
    params.append(min(max(int(limit), 1), 500))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, name, amount, currency_code, status, payment_date, due_date, reference, subscription_id, vendor_id
            FROM slmct.payments
            WHERE {" AND ".join(clauses)}
            ORDER BY payment_date DESC NULLS LAST, name
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    total_amount = sum(float(row.get("amount") or 0) for row in rows)
    return {"count": len(rows), "total_amount": total_amount, "payments": rows}


def get_employees(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    status: str | None = None,
    department: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    clauses = ["organisation_id = %s"]
    params: list[Any] = [org_id]
    if status:
        clauses.append("status = %s")
        params.append(status)
    if department:
        clauses.append("department = %s")
        params.append(department)
    params.append(min(max(int(limit), 1), 500))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, full_name, work_email, department, job_title, status, employee_number
            FROM slmct.people
            WHERE {" AND ".join(clauses)}
            ORDER BY full_name
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "employees": rows}


def get_contracts(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    status: str | None = None,
    expiring_within_days: int | None = None,
    title: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    clauses = ["organisation_id = %s"]
    params: list[Any] = [org_id]
    if status:
        clauses.append("status = %s")
        params.append(status)
    else:
        clauses.append("status != 'terminated'")
    if title:
        clauses.append("title ILIKE %s")
        params.append(f"%{str(title).strip()}%")
    if expiring_within_days is not None:
        from datetime import date, timedelta

        cutoff = date.today() + timedelta(days=int(expiring_within_days))
        clauses.append("end_date IS NOT NULL")
        clauses.append("end_date <= %s")
        params.append(cutoff.isoformat())
        clauses.append("end_date >= CURRENT_DATE")
    params.append(min(max(int(limit), 1), 200))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, title, contract_number, contract_type, vendor_id, subscription_id,
                   start_date, end_date, value, currency_code, status, auto_renew
            FROM slmct.contracts
            WHERE {" AND ".join(clauses)}
            ORDER BY end_date DESC NULLS LAST, title
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "contracts": rows}


def get_workflows(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    status: str | None = None,
    workflow_type: str | None = None,
    requested_module: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    clauses = ["organisation_id = %s"]
    params: list[Any] = [org_id]
    if status:
        clauses.append("status = %s")
        params.append(status)
    if workflow_type:
        clauses.append("workflow_type = %s")
        params.append(workflow_type)
    if requested_module:
        clauses.append("requested_module = %s")
        params.append(requested_module)
    params.append(min(max(int(limit), 1), 200))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, workflow_type, requested_module, requested_action, status,
                   requested_by_email, notes, created_at
            FROM slmct.workflow_requests
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "workflows": rows}


def get_audit_logs(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    limit: int = 50,
) -> dict[str, Any]:
    if not _actor_has_any(actor.get("roles"), AUDIT_ROLES):
        return {"error": "You do not have permission to view audit logs."}
    org_id = _org_uuid(organisation_id)
    safe_limit = min(max(int(limit), 1), 200)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, action, entity_type, entity_id, actor_email, metadata, created_at
            FROM slmct.audit_logs
            WHERE organisation_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (org_id, safe_limit),
        )
        rows = _rows_to_dicts(cur.fetchall())
        if not rows:
            # Legacy rows from _audit() omit organisation_id — fallback for demo data only.
            cur.execute(
                """
                SELECT id, action, entity_type, entity_id, actor_email, metadata, created_at
                FROM slmct.audit_logs
                WHERE organisation_id IS NULL
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (safe_limit,),
            )
            rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "audit_logs": rows}


def get_spend_summary(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    group_by: str = "department",
    status: str = "active",
) -> dict[str, Any]:
    if not _actor_has_any(actor.get("roles"), FINANCE_ROLES):
        return {"error": "You do not have permission to view spend summaries."}
    org_id = _org_uuid(organisation_id)
    group_by = str(group_by or "department").strip().lower()
    if group_by == "vendor":
        sql = """
            SELECT COALESCE(v.name, 'Unknown vendor') AS group_key,
                   s.currency_code,
                   COUNT(*) AS subscription_count,
                   COALESCE(SUM(s.amount), 0) AS total_amount
            FROM slmct.subscriptions s
            LEFT JOIN slmct.vendors v ON v.id = s.vendor_id
            WHERE s.organisation_id = %s AND s.status = %s
            GROUP BY COALESCE(v.name, 'Unknown vendor'), s.currency_code
            ORDER BY total_amount DESC
        """
    elif group_by == "org":
        sql = """
            SELECT o.name AS group_key,
                   s.currency_code,
                   COUNT(*) AS subscription_count,
                   COALESCE(SUM(s.amount), 0) AS total_amount
            FROM slmct.subscriptions s
            JOIN slmct.organisations o ON o.id = s.organisation_id
            WHERE s.organisation_id = %s AND s.status = %s
            GROUP BY o.name, s.currency_code
            ORDER BY total_amount DESC
        """
    else:
        sql = """
            SELECT COALESCE(s.department, 'Unassigned') AS group_key,
                   s.currency_code,
                   COUNT(*) AS subscription_count,
                   COALESCE(SUM(s.amount), 0) AS total_amount
            FROM slmct.subscriptions s
            WHERE s.organisation_id = %s AND s.status = %s
            GROUP BY COALESCE(s.department, 'Unassigned'), s.currency_code
            ORDER BY total_amount DESC
        """
    with conn.cursor() as cur:
        cur.execute(sql, (org_id, status))
        rows = _rows_to_dicts(cur.fetchall())
    return {"group_by": group_by, "status": status, "rows": rows}


_RENEWALS_INNER_SQL = """
    SELECT
        s.id,
        s.organisation_id,
        'subscription'::text AS renewal_type,
        s.name,
        v.name AS vendor_name,
        s.renewal_date AS renewal_date,
        s.amount,
        s.currency_code,
        s.status::text AS status
    FROM slmct.subscriptions s
    LEFT JOIN slmct.vendors v ON v.id = s.vendor_id
    WHERE s.renewal_date IS NOT NULL
    UNION ALL
    SELECT
        l.id,
        l.organisation_id,
        'licence'::text AS renewal_type,
        l.licence_name AS name,
        sub.name AS vendor_name,
        l.expires_at AS renewal_date,
        NULL::numeric AS amount,
        NULL::char(3) AS currency_code,
        l.status::text AS status
    FROM slmct.licences l
    LEFT JOIN slmct.subscriptions sub ON sub.id = l.subscription_id
    WHERE l.expires_at IS NOT NULL
    UNION ALL
    SELECT
        v.id,
        v.organisation_id,
        'vendor'::text AS renewal_type,
        v.name,
        v.legal_name AS vendor_name,
        MIN(s.renewal_date) AS renewal_date,
        SUM(s.amount) AS amount,
        MAX(s.currency_code)::char(3) AS currency_code,
        v.status::text AS status
    FROM slmct.vendors v
    JOIN slmct.subscriptions s ON s.vendor_id = v.id
    WHERE s.renewal_date IS NOT NULL
    GROUP BY v.id, v.organisation_id, v.name, v.legal_name, v.status
"""


def get_renewals(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    within_days: int | None = None,
    renewal_type: str | None = None,
    include_past: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """Unified renewals feed: subscriptions, licences (expires_at), and vendor rollups."""
    org_id = _org_uuid(organisation_id)
    clauses = ["organisation_id = %s"]
    params: list[Any] = [org_id]
    if renewal_type:
        clauses.append("renewal_type = %s")
        params.append(str(renewal_type).strip().lower())
    if within_days is not None:
        from datetime import date, timedelta

        cutoff = date.today() + timedelta(days=int(within_days))
        clauses.append("renewal_date IS NOT NULL")
        clauses.append("renewal_date <= %s")
        params.append(cutoff.isoformat())
        if not include_past:
            clauses.append("renewal_date >= CURRENT_DATE")
    safe_limit = min(max(int(limit), 1), 500)
    params.append(safe_limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, renewal_type, name, vendor_name, renewal_date, amount, currency_code, status
            FROM ({_RENEWALS_INNER_SQL}) renewals
            WHERE {" AND ".join(clauses)}
            ORDER BY renewal_date ASC NULLS LAST, name ASC
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "renewals": rows}


def get_dashboard_summary(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              (SELECT count(*) FROM slmct.subscriptions
               WHERE organisation_id = %s AND status IN ('active', 'trial', 'pending_renewal')) AS active_subscriptions,
              (SELECT count(*) FROM slmct.subscriptions
               WHERE organisation_id = %s AND renewal_date BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '90 days') AS renewals_due_90_days,
              (SELECT COALESCE(sum(allocated_amount), 0) FROM slmct.budgets
               WHERE organisation_id = %s AND status IN ('approved', 'locked')) AS allocated_budget,
              (SELECT COALESCE(sum(amount), 0) FROM slmct.payments
               WHERE organisation_id = %s AND status IN ('planned', 'pending', 'paid')) AS tracked_payments_total,
              (SELECT count(*) FROM slmct.licences
               WHERE organisation_id = %s AND status = 'assigned') AS assigned_licences,
              (SELECT count(*) FROM slmct.licences
               WHERE organisation_id = %s AND status = 'available') AS available_licences,
              (SELECT count(*) FROM slmct.workflow_requests
               WHERE organisation_id = %s AND status IN ('submitted', 'reopened', 'line_manager_approved', 'master_approved', 'finance_approved')) AS pending_workflows
            """,
            (org_id, org_id, org_id, org_id, org_id, org_id, org_id),
        )
        row = cur.fetchone()
    return {"summary": _json_safe(dict(row)) if row else {}}


def get_organisations(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    include_all_active: bool = False,
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, parent_id, code, name, legal_name, country_code, currency_code,
                   is_sister_entity, is_active, website_url
            FROM slmct.organisations
            WHERE id = %s
            """,
            (org_id,),
        )
        current = cur.fetchone()
        organisations = [_json_safe(dict(current))] if current else []
        if include_all_active and _actor_has_any(
            actor.get("roles"), {"master_admin", "finance", "auditor", "it_admin"}
        ):
            cur.execute(
                """
                SELECT id, parent_id, code, name, legal_name, country_code, currency_code,
                       is_sister_entity, is_active
                FROM slmct.organisations
                WHERE is_active = true
                ORDER BY name
                """
            )
            organisations = _rows_to_dicts(cur.fetchall())
    return {"count": len(organisations), "organisations": organisations}


def get_tool_requests(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    clauses = ["organisation_id = %s"]
    params: list[Any] = [org_id]
    if status:
        clauses.append("status = %s")
        params.append(status)
    params.append(min(max(int(limit), 1), 200))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, requester_name, requester_email, department, requested_tool, vendor_name,
                   category, estimated_amount, currency_code, status, created_at
            FROM slmct.tool_requests
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "tool_requests": rows}


def get_recycle_bin(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    module: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    from app.main import MODULES

    org_id = _org_uuid(organisation_id)
    items: list[dict[str, Any]] = []
    modules = [module] if module and module in MODULES else list(MODULES.keys())
    with conn.cursor() as cur:
        for mod in modules:
            config = MODULES.get(mod) or {}
            soft = config.get("soft_delete")
            if not soft:
                continue
            status_field, delete_value = soft
            name_col = "name"
            fields = config.get("fields") or []
            if "name" not in fields:
                if "licence_name" in fields:
                    name_col = "licence_name"
                elif "full_name" in fields:
                    name_col = "full_name"
                elif "title" in fields:
                    name_col = "title"
                else:
                    name_col = "id::text"
            cur.execute(
                f"""
                SELECT %s AS module, id, {name_col} AS name, updated_at AS deleted_at,
                       {status_field} AS status
                FROM {config["table"]}
                WHERE organisation_id = %s AND {status_field} = %s
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (mod, org_id, delete_value, min(max(int(limit), 1), 500)),
            )
            items.extend(_rows_to_dicts(cur.fetchall()))
    return {"count": len(items), "recycle_bin_items": items[:limit]}


def get_fx_rates(conn: Connection, organisation_id: str | UUID, actor: dict[str, Any]) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT currency_code, rate_from_usd, fetched_at FROM slmct.fx_rates ORDER BY currency_code"
        )
        rows = _rows_to_dicts(cur.fetchall())
    rates = {str(r["currency_code"]): float(r["rate_from_usd"]) for r in rows}
    fetched_at = max((r.get("fetched_at") for r in rows if r.get("fetched_at")), default=None)
    return {
        "rates": rates,
        "fetched_at": fetched_at.isoformat() if hasattr(fetched_at, "isoformat") else None,
        "count": len(rates),
    }


def get_subscription_creation_log(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    limit: int = 100,
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    safe_limit = min(max(int(limit), 1), 200)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                wr.id AS workflow_id,
                wr.requested_by_email AS created_by,
                wr.workflow_type,
                COALESCE(wr.completed_at, wr.updated_at) AS created_date,
                s.id AS subscription_id,
                s.name AS subscription_name,
                s.status AS subscription_status,
                s.amount,
                s.currency_code,
                v.name AS vendor_name,
                wr.assigned_employee_name AS assigned_user
            FROM slmct.workflow_requests wr
            LEFT JOIN slmct.subscriptions s ON s.id = wr.activated_entity_id
            LEFT JOIN slmct.vendors v ON v.id = s.vendor_id
            WHERE wr.organisation_id = %s
              AND wr.status IN ('completed', 'finance_closed')
              AND wr.activated_entity_id IS NOT NULL
            ORDER BY COALESCE(wr.completed_at, wr.updated_at) DESC NULLS LAST
            LIMIT %s
            """,
            (org_id, safe_limit),
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "subscription_creation_log": rows}


def get_email_logs(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    workflow_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if not _actor_has_any(actor.get("roles"), {"master_admin", "it_admin", "auditor", "finance"}):
        return {"error": "You do not have permission to view email logs."}
    org_id = _org_uuid(organisation_id)
    safe_limit = min(max(int(limit), 1), 200)
    if workflow_id:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT el.id, el.workflow_request_id, el.event_type, el.from_email, el.to_email,
                       el.subject, el.status, el.workflow_stage, el.created_at, el.sent_at
                FROM slmct.email_logs el
                JOIN slmct.workflow_requests wr ON wr.id = el.workflow_request_id
                WHERE el.workflow_request_id = %s AND wr.organisation_id = %s
                ORDER BY el.created_at DESC
                LIMIT %s
                """,
                (str(workflow_id), org_id, safe_limit),
            )
            rows = _rows_to_dicts(cur.fetchall())
    else:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT el.id, el.workflow_request_id, el.tool_request_id, el.event_type,
                       el.from_email, el.to_email, el.subject, el.status, el.workflow_stage,
                       el.created_at, el.sent_at
                FROM slmct.email_logs el
                LEFT JOIN slmct.workflow_requests wr ON wr.id = el.workflow_request_id
                LEFT JOIN slmct.tool_requests tr ON tr.id = el.tool_request_id
                WHERE wr.organisation_id = %s OR tr.organisation_id = %s
                ORDER BY el.created_at DESC
                LIMIT %s
                """,
                (org_id, org_id, safe_limit),
            )
            rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "email_logs": rows}


def get_workflow_detail(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    workflow_id: str,
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, workflow_type, requested_module, requested_action, status, payload,
                   requested_by_email, notes, created_at, updated_at, completed_at,
                   assigned_employee_name, assigned_employee_email
            FROM slmct.workflow_requests
            WHERE id = %s AND organisation_id = %s
            LIMIT 1
            """,
            (str(workflow_id), org_id),
        )
        row = cur.fetchone()
        if not row:
            return {"error": "Workflow request not found in this organisation."}
        cur.execute(
            """
            SELECT from_status, to_status, actor_email, notes, created_at
            FROM slmct.workflow_status_history
            WHERE workflow_request_id = %s
            ORDER BY created_at ASC
            LIMIT 50
            """,
            (str(workflow_id),),
        )
        history = _rows_to_dicts(cur.fetchall())
    result = _json_safe(dict(row))
    return {"workflow": result, "status_history": history}


def get_licence_pool_summary(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
) -> dict[str, Any]:
    """Seat utilization counts by licence status."""
    org_id = _org_uuid(organisation_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, COUNT(*) AS licence_count
            FROM slmct.licences
            WHERE organisation_id = %s
            GROUP BY status
            ORDER BY status
            """,
            (org_id,),
        )
        rows = _rows_to_dicts(cur.fetchall())
    total = sum(int(r.get("licence_count") or 0) for r in rows)
    return {"total": total, "by_status": rows}


USER_ADMIN_ROLES = frozenset({"master_admin", "it_admin", "hr_admin"})

_MODULE_NAME_COLUMNS: dict[str, str] = {
    "vendors": "name",
    "subscriptions": "name",
    "licences": "licence_name",
    "budgets": "department",
    "payments": "name",
    "employees": "full_name",
    "contracts": "title",
}


def get_users(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    status: str | None = "active",
    limit: int = 200,
) -> dict[str, Any]:
    if not _actor_has_any(actor.get("roles"), USER_ADMIN_ROLES):
        return {"error": "You do not have permission to view platform users."}
    org_id = _org_uuid(organisation_id)
    clauses = ["p.organisation_id = %s", "au.status = 'active'"]
    params: list[Any] = [org_id]
    if status:
        clauses.append("p.status = %s")
        params.append(status)
    params.append(min(max(int(limit), 1), 500))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              p.id AS person_id,
              p.full_name,
              p.work_email,
              p.department,
              p.job_title,
              p.status AS person_status,
              au.id AS user_id,
              au.status AS user_status,
              COALESCE(
                jsonb_agg(DISTINCT r.code) FILTER (WHERE r.code IS NOT NULL AND ur.revoked_at IS NULL),
                '[]'::jsonb
              ) AS roles
            FROM slmct.people p
            JOIN slmct.app_users au ON au.person_id = p.id
            LEFT JOIN slmct.user_roles ur ON ur.user_id = au.id AND ur.revoked_at IS NULL
            LEFT JOIN slmct.roles r ON r.id = ur.role_id
            WHERE {" AND ".join(clauses)}
            GROUP BY p.id, au.id, au.status
            ORDER BY p.full_name
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "users": rows}


def get_roles(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT code, name, description, can_login
            FROM slmct.roles
            ORDER BY
              CASE code
                WHEN 'master_admin' THEN 1
                WHEN 'it_admin' THEN 2
                WHEN 'finance' THEN 3
                WHEN 'hr_admin' THEN 4
                WHEN 'auditor' THEN 5
                WHEN 'line_manager' THEN 6
                WHEN 'employee' THEN 7
                ELSE 99
              END
            """
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "roles": rows}


def get_module_record(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    module: str,
    record_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    from app.main import MODULES

    mod = str(module or "").strip().lower()
    if mod not in MODULES:
        return {"error": f"Unknown module '{module}'. Valid: {', '.join(sorted(MODULES.keys()))}"}
    if not record_id and not name:
        return {"error": "Provide record_id or name to look up a record."}
    org_id = _org_uuid(organisation_id)
    config = MODULES[mod]
    table = config["table"]
    clauses = ["organisation_id = %s"]
    params: list[Any] = [org_id]
    if record_id:
        try:
            UUID(str(record_id))
        except (ValueError, AttributeError):
            return {"error": "record_id must be a valid UUID."}
        clauses.append("id = %s")
        params.append(str(record_id))
    else:
        name_col = _MODULE_NAME_COLUMNS.get(mod, "name")
        clauses.append(f"{name_col} ILIKE %s")
        params.append(f"%{str(name).strip()}%")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC NULLS LAST
            LIMIT 5
            """,
            params,
        )
        rows = cur.fetchall()
    if not rows:
        return {"error": "No matching record found.", "module": mod}
    records = []
    for row in rows:
        rec = _json_safe(dict(row))
        if mod == "contracts" and rec.get("document_data"):
            rec["document_data"] = "[binary omitted]"
        records.append(rec)
    if len(records) == 1:
        return {"module": mod, "record": records[0]}
    return {"module": mod, "count": len(records), "records": records}


def get_employee_team(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    manager_email: str,
    limit: int = 200,
) -> dict[str, Any]:
    if not manager_email or not str(manager_email).strip():
        return {"error": "manager_email is required."}
    org_id = _org_uuid(organisation_id)
    params = [str(manager_email).strip(), org_id, min(max(int(limit), 1), 500)]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, full_name, work_email, department, job_title, status, employee_number
            FROM slmct.people
            WHERE lower(line_manager_email) = lower(%s)
              AND organisation_id = %s
              AND status != 'inactive'
            ORDER BY full_name
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "team": rows, "manager_email": str(manager_email).strip().lower()}


AI_OPERATOR_AUDIT = '{"source":"ai_operator"}'
AI_OPERATOR_RESUBMIT_AUDIT = '{"source":"ai_operator","event":"resubmitted"}'
ADMIN_WRITE_ROLES = frozenset({"master_admin", "it_admin", "finance", "hr_admin"})
MASTER_ADMIN_ROLES = frozenset({"master_admin"})
HR_WRITE_ROLES = frozenset({"master_admin", "it_admin", "hr_admin"})
WORKFLOW_SUBMIT_ROLES = frozenset({"master_admin", "finance", "it_admin", "hr_admin", "line_manager", "employee"})


def _deny_if_not(actor: dict[str, Any], allowed: set[str] | frozenset[str]) -> dict[str, str] | None:
    if not _actor_has_any(actor.get("roles"), set(allowed)):
        return {"error": "You do not have permission to perform this action."}
    return None


def _actor_payload(actor: dict[str, Any], organisation_id: str | UUID, **fields: Any) -> dict[str, Any]:
    payload = {key: value for key, value in fields.items() if value is not None}
    payload["organisation_id"] = str(_org_uuid(organisation_id))
    payload["actor_roles"] = list(actor.get("roles") or [])
    if actor.get("user_id"):
        payload["actor_user_id"] = str(actor["user_id"])
    if actor.get("email"):
        payload["actor_email"] = actor["email"]
    return payload


def _record_result(row: dict[str, Any], key: str) -> dict[str, Any]:
    return {key: {field: _json_safe(value) for field, value in row.items()}}


def _handle_write(action: Callable[[], Any]) -> Any:
    from fastapi import HTTPException

    try:
        return action()
    except HTTPException as exc:
        detail = exc.detail
        if not isinstance(detail, str):
            detail = str(detail)
        return {"error": detail}
    except PermissionError as exc:
        return {"error": str(exc)}


def _verify_budget_in_org(conn: Connection, budget_id: str | UUID, org_id: UUID) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM slmct.budgets WHERE id = %s AND organisation_id = %s",
            (str(budget_id), org_id),
        )
        return cur.fetchone() is not None


def _verify_licence_in_org(conn: Connection, licence_id: str | UUID, org_id: UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM slmct.licences WHERE id = %s AND organisation_id = %s",
            (str(licence_id), org_id),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _verify_person_in_org(conn: Connection, person_id: str | UUID, org_id: UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM slmct.people WHERE id = %s AND organisation_id = %s",
            (str(person_id), org_id),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _verify_vendor_in_org(conn: Connection, vendor_id: str | UUID, org_id: UUID) -> dict[str, Any] | None:
    if str(vendor_id).strip().startswith("$step:"):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name FROM slmct.vendors WHERE id = %s AND organisation_id = %s",
            (str(vendor_id), org_id),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_entity_field_guide(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    entity: str | None = None,
    goal: str | None = None,
) -> dict[str, Any]:
    from app.entity_field_guide import get_field_guide

    return get_field_guide(entity=entity, goal=goal)


def get_vendor_catalogue(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    vendor_id: str | None = None,
    name: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    clauses = ["v.organisation_id = %s"]
    params: list[Any] = [org_id]
    if vendor_id:
        if str(vendor_id).strip().startswith("$step:"):
            return {"error": "vendor_id step references are resolved at execute time — use get_vendors(name=...) during planning."}
        clauses.append("vc.vendor_id = %s")
        params.append(str(vendor_id))
    if name:
        clauses.append("vc.name ILIKE %s")
        params.append(f"%{str(name).strip()}%")
    params.append(min(max(int(limit), 1), 500))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT vc.id, vc.vendor_id, vc.name, vc.price, vc.currency_code,
                   vc.scrape_url, vc.password_change_url, v.name AS vendor_name
            FROM slmct.vendor_catalogue vc
            JOIN slmct.vendors v ON v.id = vc.vendor_id
            WHERE {" AND ".join(clauses)}
            ORDER BY vc.name
            LIMIT %s
            """,
            params,
        )
        rows = _rows_to_dicts(cur.fetchall())
    return {"count": len(rows), "catalogue_items": rows}


def lookup_vendor_autofill(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    name: str,
) -> dict[str, Any]:
    import json
    import re

    from app.llm import complete
    from app.prompts import format_prompt

    vendor_name = (name or "").strip()
    if not vendor_name:
        return {"error": "name is required"}
    try:
        prompt = format_prompt("vendor_autofill", name=vendor_name)
        result = complete(
            "vendor_autofill",
            contents=prompt,
            google_search=True,
            temperature=0,
            conn=conn,
            metadata={"phase": "operator_lookup", "lookup": "vendor_autofill"},
        )
        raw = (result.text or "").strip()
        if not raw:
            return {"not_found": True}
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        data = json.loads(raw)
        if data.get("not_found"):
            return {"not_found": True}
        return {
            "not_found": False,
            "vendor_name": str(data.get("vendor_name") or ""),
            "legal_name": str(data.get("legal_name") or ""),
            "website_url": str(data.get("website_url") or ""),
            "contact_name": str(data.get("contact_name") or ""),
            "contact_email": str(data.get("contact_email") or ""),
        }
    except json.JSONDecodeError:
        return {"error": "Could not parse vendor autofill response"}
    except Exception as exc:
        return {"error": str(exc)}


def lookup_pricing_url(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    vendor_name: str,
    subscription_name: str | None = None,
) -> dict[str, Any]:
    import json
    import re

    from app.llm import complete
    from app.prompts import format_prompt

    vendor = (vendor_name or "").strip()
    if not vendor:
        return {"error": "vendor_name is required"}
    sub = (subscription_name or vendor).strip()
    product_hint = f'"{sub}" for ' if subscription_name else ""
    try:
        prompt = format_prompt(
            "vendor_purchase_url",
            product_hint=product_hint,
            vendor_name=vendor,
            subscription_name=sub,
        )
        result = complete(
            "vendor_purchase_url",
            contents=prompt,
            google_search=True,
            temperature=0,
            thinking_budget=0,
            conn=conn,
            metadata={"phase": "operator_lookup", "lookup": "pricing_url"},
        )
        raw = (result.text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        data = json.loads(raw)
        purchase_url = str(data.get("purchase_url") or "").strip()
        if not purchase_url:
            return {"error": "No pricing URL found"}
        return {
            "purchase_url": purchase_url,
            "label": str(data.get("label") or f"{sub} — Pricing Page"),
        }
    except json.JSONDecodeError:
        return {"error": "Could not parse pricing URL response"}
    except Exception as exc:
        return {"error": str(exc)}


def lookup_subscription_pricing(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    subscription_name: str,
    vendor_name: str | None = None,
    scrape_url: str | None = None,
) -> dict[str, Any]:
    """Resolve monthly price, currency, and pricing URL for a subscription/product."""
    from app.operator_autofill import resolve_subscription_pricing

    name = (subscription_name or "").strip()
    if not name:
        return {"error": "subscription_name is required"}
    result = resolve_subscription_pricing(
        conn,
        subscription_name=name,
        vendor_name=(vendor_name or "").strip(),
        scrape_url=(scrape_url or "").strip() or None,
    )
    if not result:
        return {"error": f"Could not resolve pricing for '{name}'."}
    return result
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM slmct.people WHERE id = %s AND organisation_id = %s",
            (str(employee_id), org_id),
        )
        return cur.fetchone() is not None


def _verify_workflow_in_org(conn: Connection, request_id: str | UUID, org_id: UUID) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM slmct.workflow_requests WHERE id = %s AND organisation_id = %s",
            (str(request_id), org_id),
        )
        return cur.fetchone() is not None


def create_budget(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    fiscal_year: int,
    department: str,
    allocated_amount: float,
    currency_code: str = "AED",
    status: str = "approved",
    notes: str | None = None,
) -> dict[str, Any]:
    denied = _deny_if_not(actor, FINANCE_ROLES)
    if denied:
        return denied

    def _run() -> dict[str, Any]:
        from app.main import _create_module

        payload = _actor_payload(
            actor,
            organisation_id,
            fiscal_year=fiscal_year,
            department=department,
            allocated_amount=allocated_amount,
            currency_code=currency_code,
            status=status,
            notes=notes,
        )
        row = _create_module("budgets", payload, conn, audit_metadata=AI_OPERATOR_AUDIT)
        return _record_result(row, "budget")

    return _handle_write(_run)


def update_budget(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    budget_id: str,
    fiscal_year: int | None = None,
    department: str | None = None,
    allocated_amount: float | None = None,
    currency_code: str | None = None,
    status: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    denied = _deny_if_not(actor, FINANCE_ROLES)
    if denied:
        return denied
    org_id = _org_uuid(organisation_id)
    if not _verify_budget_in_org(conn, budget_id, org_id):
        return {"error": "Budget not found in this organisation."}

    def _run() -> dict[str, Any]:
        from app.main import _update_module

        updates = {
            key: value
            for key, value in {
                "fiscal_year": fiscal_year,
                "department": department,
                "allocated_amount": allocated_amount,
                "currency_code": currency_code,
                "status": status,
                "notes": notes,
            }.items()
            if value is not None
        }
        if not updates:
            return {"error": "No valid fields supplied for update."}
        row = _update_module(
            "budgets",
            UUID(str(budget_id)),
            updates,
            conn,
            audit_metadata=AI_OPERATOR_AUDIT,
        )
        return _record_result(row, "budget")

    return _handle_write(_run)


def create_vendor(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    name: str,
    legal_name: str | None = None,
    website_url: str | None = None,
    contact_name: str | None = None,
    contact_email: str | None = None,
    status: str = "active",
) -> dict[str, Any]:
    denied = _deny_if_not(actor, ADMIN_WRITE_ROLES)
    if denied:
        return denied

    def _run() -> dict[str, Any]:
        from app.main import _create_module

        payload = _actor_payload(
            actor,
            organisation_id,
            name=name,
            legal_name=legal_name,
            website_url=website_url,
            contact_name=contact_name,
            contact_email=contact_email,
            status=status,
        )
        row = _create_module("vendors", payload, conn, audit_metadata=AI_OPERATOR_AUDIT)
        return _record_result(row, "vendor")

    return _handle_write(_run)


def create_vendor_catalogue_item(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    vendor_id: str,
    name: str,
    price: float,
    currency_code: str = "AED",
    scrape_url: str | None = None,
    password_change_url: str | None = None,
) -> dict[str, Any]:
    denied = _deny_if_not(actor, ADMIN_WRITE_ROLES)
    if denied:
        return denied
    org_id = _org_uuid(organisation_id)
    if not _verify_vendor_in_org(conn, vendor_id, org_id):
        return {"error": "Vendor not found in this organisation."}

    def _run() -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO slmct.vendor_catalogue (
                    vendor_id, name, price, currency_code, scrape_url, password_change_url
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    str(vendor_id),
                    name,
                    price,
                    currency_code,
                    scrape_url or None,
                    password_change_url or None,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return _record_result(dict(row), "catalogue_item")

    return _handle_write(_run)


def create_subscription(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    name: str,
    vendor_id: str | None = None,
    category: str | None = None,
    department: str | None = None,
    amount: float | None = None,
    currency_code: str = "AED",
    billing_cycle: str | None = None,
    start_date: str | None = None,
    renewal_date: str | None = None,
    status: str = "active",
    notes: str | None = None,
) -> dict[str, Any]:
    denied = _deny_if_not(actor, MASTER_ADMIN_ROLES)
    if denied:
        return denied

    def _run() -> dict[str, Any]:
        from app.main import _create_module
        from app.workflow_submission_permissions import validate_direct_software_create

        payload = _actor_payload(
            actor,
            organisation_id,
            name=name,
            vendor_id=vendor_id,
            category=category,
            department=department,
            amount=amount,
            currency_code=currency_code,
            billing_cycle=billing_cycle,
            start_date=start_date,
            renewal_date=renewal_date,
            status=status,
            notes=notes,
        )
        validate_direct_software_create(payload.get("actor_roles"), "subscriptions")
        row = _create_module("subscriptions", payload, conn, audit_metadata=AI_OPERATOR_AUDIT)
        return _record_result(row, "subscription")

    return _handle_write(_run)


def assign_licence(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    licence_id: str,
    person_id: str,
) -> dict[str, Any]:
    denied = _deny_if_not(actor, MASTER_ADMIN_ROLES)
    if denied:
        return denied
    org_id = _org_uuid(organisation_id)
    licence = _verify_licence_in_org(conn, licence_id, org_id)
    if not licence:
        return {"error": "Licence not found in this organisation."}
    if str(licence.get("status")) in {"revoked", "expired"}:
        return {"error": "Cannot assign a revoked or expired licence."}
    person = _verify_person_in_org(conn, person_id, org_id)
    if not person:
        return {"error": "Employee not found in this organisation."}

    def _run() -> dict[str, Any]:
        from datetime import date

        from app.main import _update_module
        from app.workflow_submission_permissions import validate_direct_software_create

        payload = _actor_payload(actor, organisation_id)
        validate_direct_software_create(payload.get("actor_roles"), "licences")
        row = _update_module(
            "licences",
            UUID(str(licence_id)),
            {
                "assigned_to_person_id": str(person_id),
                "assigned_at": date.today().isoformat(),
                "status": "assigned",
            },
            conn,
            audit_metadata=AI_OPERATOR_AUDIT,
        )
        return _record_result(row, "licence")

    return _handle_write(_run)


def revoke_licence(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    licence_id: str,
) -> dict[str, Any]:
    denied = _deny_if_not(actor, MASTER_ADMIN_ROLES)
    if denied:
        return denied
    org_id = _org_uuid(organisation_id)
    if not _verify_licence_in_org(conn, licence_id, org_id):
        return {"error": "Licence not found in this organisation."}

    def _run() -> dict[str, Any]:
        from app.main import _update_module
        from app.workflow_submission_permissions import validate_direct_software_create

        payload = _actor_payload(actor, organisation_id)
        validate_direct_software_create(payload.get("actor_roles"), "licences")
        row = _update_module(
            "licences",
            UUID(str(licence_id)),
            {"status": "revoked", "assigned_to_person_id": None},
            conn,
            audit_metadata=AI_OPERATOR_AUDIT,
        )
        return _record_result(row, "licence")

    return _handle_write(_run)


def create_employee(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    full_name: str,
    work_email: str,
    department: str | None = None,
    job_title: str | None = None,
    employee_number: str | None = None,
    line_manager_email: str | None = None,
    status: str = "active",
) -> dict[str, Any]:
    denied = _deny_if_not(actor, HR_WRITE_ROLES)
    if denied:
        return denied

    def _run() -> dict[str, Any]:
        from app.main import _create_module

        payload = _actor_payload(
            actor,
            organisation_id,
            full_name=full_name,
            work_email=work_email,
            department=department,
            job_title=job_title,
            employee_number=employee_number,
            line_manager_email=line_manager_email,
            status=status,
        )
        row = _create_module("employees", payload, conn, audit_metadata=AI_OPERATOR_AUDIT)
        return _record_result(row, "employee")

    return _handle_write(_run)


def update_employee_status(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    employee_id: str,
    status: str,
) -> dict[str, Any]:
    denied = _deny_if_not(actor, HR_WRITE_ROLES)
    if denied:
        return denied
    org_id = _org_uuid(organisation_id)
    if not _verify_employee_in_org(conn, employee_id, org_id):
        return {"error": "Employee not found in this organisation."}

    def _run() -> dict[str, Any]:
        from app.main import _update_module

        row = _update_module(
            "employees",
            UUID(str(employee_id)),
            {"status": status},
            conn,
            audit_metadata=AI_OPERATOR_AUDIT,
        )
        return _record_result(row, "employee")

    return _handle_write(_run)


def submit_workflow_request(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    workflow_type: str,
    requested_module: str,
    payload: dict[str, Any],
    requested_action: str = "create",
    notes: str | None = None,
) -> dict[str, Any]:
    denied = _deny_if_not(actor, WORKFLOW_SUBMIT_ROLES)
    if denied:
        return denied

    def _run() -> dict[str, Any]:
        from app.main import _create_workflow_request

        inner = dict(payload or {})
        inner["organisation_id"] = str(_org_uuid(organisation_id))
        wf_payload = _actor_payload(
            actor,
            organisation_id,
            workflow_type=workflow_type,
            requested_module=requested_module,
            requested_action=requested_action,
            payload=inner,
            notes=notes,
        )
        row = _create_workflow_request(conn, wf_payload, audit_metadata=AI_OPERATOR_AUDIT)
        return _record_result(row, "workflow_request")

    return _handle_write(_run)


def respond_info_request(
    conn: Connection,
    organisation_id: str | UUID,
    actor: dict[str, Any],
    *,
    request_id: str,
    payload: dict[str, Any] | None = None,
    response_notes: str | None = None,
) -> dict[str, Any]:
    org_id = _org_uuid(organisation_id)
    if not _verify_workflow_in_org(conn, request_id, org_id):
        return {"error": "Workflow request not found in this organisation."}

    def _run() -> dict[str, Any]:
        from app.main import _resubmit_workflow_request

        merged = dict(payload or {})
        if response_notes:
            merged["notes"] = response_notes
        wf_payload = _actor_payload(actor, organisation_id, payload=merged)
        row = _resubmit_workflow_request(
            conn,
            UUID(str(request_id)),
            wf_payload,
            audit_metadata=AI_OPERATOR_RESUBMIT_AUDIT,
        )
        return _record_result(row, "workflow_request")

    return _handle_write(_run)


def _allowed_tool_params(tool: Tool) -> set[str]:
    props = (tool.parameters or {}).get("properties") or {}
    return set(props.keys())


def execute_tool(tool: Tool, conn: Connection, organisation_id: str | UUID, actor: dict[str, Any], **params: Any) -> Any:
    from app.operator_autofill import is_step_ref

    for key, value in params.items():
        if is_step_ref(value):
            return {
                "error": (
                    f"Parameter '{key}' uses unresolved step reference {value!r}. "
                    "Step references are resolved at execute time after earlier plan steps complete."
                )
            }
    allowed = _allowed_tool_params(tool)
    if allowed:
        params = {key: value for key, value in params.items() if key in allowed}
    result = tool.handler(conn, organisation_id, actor, **params)
    return _json_safe(result)


READ_TOOLS: list[Tool] = [
    Tool(
        name="get_subscriptions",
        description="List subscriptions. Use renewing_within_days=30 for active subs renewing soon. Use statuses='trial,expired' for multiple statuses; use count field for totals.",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by one subscription status, e.g. active, cancelled, trial"},
                "statuses": {"type": "string", "description": "Comma-separated statuses, e.g. trial,expired"},
                "department": {"type": "string", "description": "Filter by department name"},
                "renewing_within_days": {"type": "integer", "description": "Active subscriptions renewing within N days from today"},
                "renewal_before": {"type": "string", "description": "ISO date YYYY-MM-DD — only subscriptions renewing on or before this date"},
                "renewal_after": {"type": "string", "description": "ISO date YYYY-MM-DD — only subscriptions renewing on or after this date"},
                "name": {"type": "string", "description": "Filter by subscription name (partial match)"},
                "limit": {"type": "integer", "description": "Maximum rows to return (default 200)"},
            },
        },
        required_roles=frozenset({"master_admin", "finance", "it_admin", "hr_admin", "auditor", "line_manager"}),
        handler=get_subscriptions,
    ),
    Tool(
        name="get_vendors",
        description="List vendors. Includes compliance fields (SOC2, ISO27001, GDPR). Filter by name or status.",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Vendor status filter (default active)"},
                "name": {"type": "string", "description": "Filter by vendor name (partial match)"},
                "limit": {"type": "integer", "description": "Maximum rows to return (default 200)"},
            },
        },
        required_roles=frozenset({"master_admin", "finance", "it_admin", "auditor", "hr_admin"}),
        handler=get_vendors,
    ),
    Tool(
        name="get_licences",
        description=(
            "List licences/seats with assignee details. "
            "Use expiring_within_days=30 for licences expiring soon (expires_at). "
            "Use order_by=assigned_at_desc for recently assigned; created_at_desc for newest records. "
            "Use assignee_status='inactive' for licences held by inactive employees."
        ),
        parameters={
            "type": "object",
            "properties": {
                "include_assignee": {"type": "boolean", "description": "Include assignee name/email/status (default true)"},
                "status": {"type": "string", "description": "Filter by licence status, e.g. assigned, available"},
                "assignee_status": {"type": "string", "description": "Filter by assignee employee status, e.g. inactive"},
                "licence_name": {"type": "string", "description": "Filter by licence name substring (case-insensitive)"},
                "expiring_within_days": {
                    "type": "integer",
                    "description": "Licences with expires_at within N days from today (upcoming only)",
                },
                "exclude_revoked": {"type": "boolean", "description": "Exclude revoked/expired unless status set (default true)"},
                "order_by": {
                    "type": "string",
                    "enum": ["name", "assigned_at_desc", "created_at_desc"],
                    "description": "Sort order — use assigned_at_desc for 'recently assigned' or 'top N' revoke picks",
                },
                "limit": {"type": "integer", "description": "Maximum rows to return (default 200)"},
            },
        },
        required_roles=frozenset({"master_admin", "finance", "it_admin", "hr_admin", "auditor", "line_manager"}),
        handler=get_licences,
    ),
    Tool(
        name="get_budgets",
        description="List department budgets. Requires finance/master/auditor access.",
        parameters={
            "type": "object",
            "properties": {
                "fiscal_year": {"type": "integer", "description": "Filter by fiscal year"},
                "department": {"type": "string", "description": "Filter by department"},
                "limit": {"type": "integer", "description": "Maximum rows to return (default 100)"},
            },
        },
        required_roles=FINANCE_ROLES,
        handler=get_budgets,
    ),
    Tool(
        name="get_payments",
        description="List payments for the organisation. Response includes total_amount (SQL sum). Use total_amount for payment totals — never sum subscription spend.",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by payment status, e.g. paid, pending"},
                "limit": {"type": "integer", "description": "Maximum rows to return (default 200)"},
            },
        },
        required_roles=FINANCE_ROLES,
        handler=get_payments,
    ),
    Tool(
        name="get_employees",
        description="List employees/people records for the organisation.",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by employee status, e.g. active, inactive"},
                "department": {"type": "string", "description": "Filter by department"},
                "limit": {"type": "integer", "description": "Maximum rows to return (default 200)"},
            },
        },
        required_roles=frozenset({"master_admin", "it_admin", "hr_admin", "auditor"}),
        handler=get_employees,
    ),
    Tool(
        name="get_contracts",
        description="List contracts. Use expiring_within_days for contracts ending soon; title for partial name match.",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by contract status"},
                "expiring_within_days": {"type": "integer", "description": "Contracts with end_date within N days"},
                "title": {"type": "string", "description": "Filter by contract title (partial match)"},
                "limit": {"type": "integer", "description": "Maximum rows to return (default 100)"},
            },
        },
        required_roles=frozenset({"master_admin", "it_admin", "finance", "auditor"}),
        handler=get_contracts,
    ),
    Tool(
        name="get_workflows",
        description="List workflow/approval requests. Use get_workflow_detail for full payload on one request.",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by workflow status, e.g. submitted, completed"},
                "workflow_type": {"type": "string", "description": "Filter by type, e.g. new_subscription_request"},
                "requested_module": {"type": "string", "description": "Filter by module, e.g. subscriptions, licences"},
                "limit": {"type": "integer", "description": "Maximum rows to return (default 100)"},
            },
        },
        required_roles=ALL_COPILOT_ROLES,
        handler=get_workflows,
    ),
    Tool(
        name="get_audit_logs",
        description="List recent audit log entries. Requires auditor or master admin access.",
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum rows to return (default 50)"},
            },
        },
        required_roles=AUDIT_ROLES,
        handler=get_audit_logs,
    ),
    Tool(
        name="get_spend_summary",
        description="Return SQL-computed spend totals grouped by department, vendor, or org. Always use this for spend totals instead of doing arithmetic.",
        parameters={
            "type": "object",
            "properties": {
                "group_by": {
                    "type": "string",
                    "enum": ["department", "vendor", "org"],
                    "description": "How to group spend totals",
                },
                "status": {"type": "string", "description": "Subscription status to include (default active)"},
            },
        },
        required_roles=FINANCE_ROLES,
        handler=get_spend_summary,
    ),
    Tool(
        name="get_renewals",
        description=(
            "Unified renewals feed for subscriptions (renewal_date), licences (expires_at), and vendor rollups. "
            "Use for ANY renewal question including licences — prefer over get_subscriptions alone when user says licences or renewals generally. "
            "Use within_days=30 and renewal_type=licence|subscription|vendor to filter."
        ),
        parameters={
            "type": "object",
            "properties": {
                "within_days": {"type": "integer", "description": "Only items renewing/expiring within N days from today"},
                "renewal_type": {
                    "type": "string",
                    "enum": ["subscription", "licence", "vendor"],
                    "description": "Filter to one renewal type",
                },
                "include_past": {"type": "boolean", "description": "Include already-past dates (default false)"},
                "limit": {"type": "integer", "description": "Maximum rows (default 200)"},
            },
        },
        required_roles=frozenset({"master_admin", "finance", "it_admin", "hr_admin", "auditor", "line_manager"}),
        handler=get_renewals,
    ),
    Tool(
        name="get_dashboard_summary",
        description="Organisation dashboard KPIs: active subscriptions, renewals due 90d, budget allocated, payments tracked, licence counts, pending workflows.",
        parameters={"type": "object", "properties": {}},
        required_roles=frozenset({"master_admin", "finance", "it_admin", "auditor", "line_manager"}),
        handler=get_dashboard_summary,
    ),
    Tool(
        name="get_organisations",
        description="Current organisation metadata; set include_all_active=true (admin/finance) to list all active orgs.",
        parameters={
            "type": "object",
            "properties": {
                "include_all_active": {"type": "boolean", "description": "List all active organisations (admin roles)"},
            },
        },
        required_roles=ALL_COPILOT_ROLES,
        handler=get_organisations,
    ),
    Tool(
        name="get_tool_requests",
        description="Employee software/tool access requests (Tool Requests module).",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by status, e.g. new, approved"},
                "limit": {"type": "integer", "description": "Maximum rows (default 100)"},
            },
        },
        required_roles=ALL_COPILOT_ROLES,
        handler=get_tool_requests,
    ),
    Tool(
        name="get_recycle_bin",
        description="Soft-deleted records across modules (cancelled subs, revoked licences, inactive vendors, etc.).",
        parameters={
            "type": "object",
            "properties": {
                "module": {
                    "type": "string",
                    "enum": ["vendors", "subscriptions", "licences", "budgets", "payments", "employees", "contracts"],
                    "description": "Optional single module filter",
                },
                "limit": {"type": "integer", "description": "Maximum rows (default 200)"},
            },
        },
        required_roles=IT_READ_ROLES,
        handler=get_recycle_bin,
    ),
    Tool(
        name="get_fx_rates",
        description="Current FX rates (currency_code → rate from USD) used for catalogue/workflow price conversion.",
        parameters={"type": "object", "properties": {}},
        required_roles=frozenset({"master_admin", "finance", "it_admin", "auditor"}),
        handler=get_fx_rates,
    ),
    Tool(
        name="get_subscription_creation_log",
        description="Subscriptions created via completed approval workflows (audit trail of workflow-activated subs).",
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum rows (default 100)"},
            },
        },
        required_roles=frozenset({"master_admin", "finance", "it_admin", "auditor"}),
        handler=get_subscription_creation_log,
    ),
    Tool(
        name="get_email_logs",
        description="Workflow/tool-request notification emails sent by the platform.",
        parameters={
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string", "description": "Optional workflow UUID to filter emails"},
                "limit": {"type": "integer", "description": "Maximum rows (default 50)"},
            },
        },
        required_roles=frozenset({"master_admin", "it_admin", "finance", "auditor"}),
        handler=get_email_logs,
    ),
    Tool(
        name="get_workflow_detail",
        description="Single workflow request with full payload and status history timeline.",
        parameters={
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string", "description": "Workflow request UUID"},
            },
            "required": ["workflow_id"],
        },
        required_roles=ALL_COPILOT_ROLES,
        handler=get_workflow_detail,
    ),
    Tool(
        name="get_licence_pool_summary",
        description="Licence seat counts grouped by status (assigned, available, revoked, etc.).",
        parameters={"type": "object", "properties": {}},
        required_roles=frozenset({"master_admin", "finance", "it_admin", "hr_admin", "auditor", "line_manager"}),
        handler=get_licence_pool_summary,
    ),
    Tool(
        name="get_users",
        description="Platform login users with assigned roles (Users admin module). Requires master/it/hr admin.",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Person status filter (default active)"},
                "limit": {"type": "integer", "description": "Maximum rows (default 200)"},
            },
        },
        required_roles=USER_ADMIN_ROLES,
        handler=get_users,
    ),
    Tool(
        name="get_roles",
        description="SLMCT role definitions (master_admin, finance, it_admin, etc.) and can_login flags.",
        parameters={"type": "object", "properties": {}},
        required_roles=ALL_COPILOT_ROLES,
        handler=get_roles,
    ),
    Tool(
        name="get_module_record",
        description=(
            "Fetch one record by module + record_id or name. "
            "Modules: vendors, subscriptions, licences, budgets, payments, employees, contracts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "module": {
                    "type": "string",
                    "enum": ["vendors", "subscriptions", "licences", "budgets", "payments", "employees", "contracts"],
                    "description": "Module/table name",
                },
                "record_id": {"type": "string", "description": "Record UUID"},
                "name": {"type": "string", "description": "Partial name/title/email lookup when record_id unknown"},
            },
            "required": ["module"],
        },
        required_roles=ALL_COPILOT_ROLES,
        handler=get_module_record,
    ),
    Tool(
        name="get_employee_team",
        description="Direct reports for a line manager (employees where line_manager_email matches).",
        parameters={
            "type": "object",
            "properties": {
                "manager_email": {"type": "string", "description": "Line manager work email"},
                "limit": {"type": "integer", "description": "Maximum rows (default 200)"},
            },
            "required": ["manager_email"],
        },
        required_roles=frozenset({"master_admin", "it_admin", "hr_admin", "line_manager", "auditor"}),
        handler=get_employee_team,
    ),
    Tool(
        name="get_entity_field_guide",
        description=(
            "Cross-module field guide: which fields matter beyond the create form "
            "(e.g. vendor catalogue pricing appears in workflows, approvals, IT purchase). "
            "Call early when planning creates or workflows."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Entity key: vendor, vendor_catalogue, subscription, workflow, budget, employee, licence",
                },
                "goal": {
                    "type": "string",
                    "description": "Optional goal hint: subscription_request, procurement, licence_assignment, budget_tracking, onboarding",
                },
            },
        },
        required_roles=frozenset({"master_admin", "finance", "it_admin", "hr_admin", "auditor", "line_manager", "employee"}),
        handler=get_entity_field_guide,
    ),
    Tool(
        name="get_vendor_catalogue",
        description="List vendor catalogue pricing entries (name, price, scrape_url) scoped to the organisation.",
        parameters={
            "type": "object",
            "properties": {
                "vendor_id": {"type": "string", "description": "Filter by vendor UUID"},
                "name": {"type": "string", "description": "Filter by catalogue item name (partial match)"},
                "limit": {"type": "integer", "description": "Maximum rows (default 200)"},
            },
        },
        required_roles=frozenset({"master_admin", "finance", "it_admin", "auditor"}),
        handler=get_vendor_catalogue,
    ),
    Tool(
        name="lookup_vendor_autofill",
        description=(
            "Look up vendor profile fields (legal_name, website_url, contacts) from a name using web search. "
            "Use before create_vendor when the user only gave a product or company name."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Vendor or product name to look up"},
            },
            "required": ["name"],
        },
        required_roles=frozenset({"master_admin", "finance", "it_admin", "hr_admin", "auditor"}),
        handler=lookup_vendor_autofill,
    ),
    Tool(
        name="lookup_pricing_url",
        description=(
            "Find official pricing/purchase page URL for a vendor and optional subscription/plan name. "
            "Use when planning create_vendor_catalogue_item without scrape_url."
        ),
        parameters={
            "type": "object",
            "properties": {
                "vendor_name": {"type": "string", "description": "Vendor company name"},
                "subscription_name": {"type": "string", "description": "Plan or product name, e.g. Figma Professional"},
            },
            "required": ["vendor_name"],
        },
        required_roles=frozenset({"master_admin", "finance", "it_admin", "auditor"}),
        handler=lookup_pricing_url,
    ),
    Tool(
        name="lookup_subscription_pricing",
        description=(
            "Resolve monthly price, currency code, and pricing URL for a subscription/product name. "
            "Use when the user asks to find/look up pricing instead of asking them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "subscription_name": {"type": "string", "description": "Product or plan name, e.g. Apple TV+"},
                "vendor_name": {"type": "string", "description": "Vendor company name"},
                "scrape_url": {"type": "string", "description": "Optional known pricing page URL to scrape first"},
            },
            "required": ["subscription_name"],
        },
        required_roles=frozenset({"master_admin", "finance", "it_admin", "auditor"}),
        handler=lookup_subscription_pricing,
    ),
]


WRITE_TOOLS: list[Tool] = [
    Tool(
        name="create_budget",
        description="Create a department budget. Requires finance, auditor, or master admin access.",
        parameters={
            "type": "object",
            "properties": {
                "fiscal_year": {"type": "integer", "description": "Budget fiscal year, e.g. 2027"},
                "department": {"type": "string", "description": "Department name"},
                "allocated_amount": {"type": "number", "description": "Budget amount"},
                "currency_code": {"type": "string", "description": "Currency code (default AED)"},
                "status": {"type": "string", "description": "Budget status (default approved)"},
                "notes": {"type": "string", "description": "Optional notes"},
            },
            "required": ["fiscal_year", "department", "allocated_amount"],
        },
        required_roles=FINANCE_ROLES,
        handler=create_budget,
    ),
    Tool(
        name="update_budget",
        description="Update an existing budget by id.",
        parameters={
            "type": "object",
            "properties": {
                "budget_id": {"type": "string", "description": "Budget UUID"},
                "fiscal_year": {"type": "integer"},
                "department": {"type": "string"},
                "allocated_amount": {"type": "number"},
                "currency_code": {"type": "string"},
                "status": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["budget_id"],
        },
        required_roles=FINANCE_ROLES,
        handler=update_budget,
    ),
    Tool(
        name="create_vendor",
        description=(
            "Create a vendor record. Only name is required; prefer legal_name, website_url, and contacts "
            "from lookup_vendor_autofill. For subscription/procurement goals, also plan create_vendor_catalogue_item."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Vendor display name"},
                "legal_name": {"type": "string"},
                "website_url": {"type": "string"},
                "contact_name": {"type": "string"},
                "contact_email": {"type": "string"},
                "status": {"type": "string", "description": "Vendor status (default active)"},
            },
            "required": ["name"],
        },
        required_roles=ADMIN_WRITE_ROLES,
        handler=create_vendor,
    ),
    Tool(
        name="create_vendor_catalogue_item",
        description=(
            "Add a vendor catalogue pricing entry (subscription name, monthly price, pricing URL). "
            "Catalogue data auto-fills subscription workflow amounts and IT purchase links."
        ),
        parameters={
            "type": "object",
            "properties": {
                "vendor_id": {"type": "string", "description": "Vendor UUID (use $step:N:vendor.id after create_vendor)"},
                "name": {"type": "string", "description": "Subscription/plan name, e.g. Figma Professional"},
                "price": {"type": "number", "description": "Monthly price"},
                "currency_code": {"type": "string", "description": "Currency code (default AED)"},
                "scrape_url": {"type": "string", "description": "Official pricing page URL"},
                "password_change_url": {"type": "string", "description": "Optional password reset URL for offboarding emails"},
            },
            "required": ["vendor_id", "name", "price"],
        },
        required_roles=ADMIN_WRITE_ROLES,
        handler=create_vendor_catalogue_item,
    ),
    Tool(
        name="create_subscription",
        description="Directly create a subscription. Restricted to master admin; others should use submit_workflow_request.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Subscription name"},
                "vendor_id": {"type": "string"},
                "category": {"type": "string"},
                "department": {"type": "string"},
                "amount": {"type": "number"},
                "currency_code": {"type": "string"},
                "billing_cycle": {"type": "string"},
                "start_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "renewal_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "status": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["name"],
        },
        required_roles=MASTER_ADMIN_ROLES,
        handler=create_subscription,
    ),
    Tool(
        name="assign_licence",
        description="Assign an existing licence to an employee. Restricted to master admin.",
        parameters={
            "type": "object",
            "properties": {
                "licence_id": {"type": "string", "description": "Licence UUID"},
                "person_id": {"type": "string", "description": "Employee/person UUID"},
            },
            "required": ["licence_id", "person_id"],
        },
        required_roles=MASTER_ADMIN_ROLES,
        handler=assign_licence,
    ),
    Tool(
        name="revoke_licence",
        description="Revoke a licence. Restricted to master admin.",
        parameters={
            "type": "object",
            "properties": {
                "licence_id": {"type": "string", "description": "Licence UUID"},
            },
            "required": ["licence_id"],
        },
        required_roles=MASTER_ADMIN_ROLES,
        handler=revoke_licence,
    ),
    Tool(
        name="create_employee",
        description="Create an employee/people record.",
        parameters={
            "type": "object",
            "properties": {
                "full_name": {"type": "string"},
                "work_email": {"type": "string"},
                "department": {"type": "string"},
                "job_title": {"type": "string"},
                "employee_number": {"type": "string"},
                "line_manager_email": {"type": "string"},
                "status": {"type": "string", "description": "Employee status (default active)"},
            },
            "required": ["full_name", "work_email"],
        },
        required_roles=HR_WRITE_ROLES,
        handler=create_employee,
    ),
    Tool(
        name="update_employee_status",
        description="Update an employee's status, e.g. active or inactive.",
        parameters={
            "type": "object",
            "properties": {
                "employee_id": {"type": "string", "description": "Employee UUID"},
                "status": {"type": "string", "description": "New status, e.g. active or inactive"},
            },
            "required": ["employee_id", "status"],
        },
        required_roles=HR_WRITE_ROLES,
        handler=update_employee_status,
    ),
    Tool(
        name="submit_workflow_request",
        description="Submit a workflow request into the normal approval chain. Never approves — only submits.",
        parameters={
            "type": "object",
            "properties": {
                "workflow_type": {"type": "string", "description": "e.g. employee_software_request, new_subscription_request, license_assignment_request"},
                "requested_module": {
                    "type": "string",
                    "description": "Target module: vendors, subscriptions, licences, employees, contracts, organisations. Not budgets or payments.",
                },
                "payload": {"type": "object", "description": "Workflow payload fields for the requested record/action"},
                "requested_action": {"type": "string", "description": "Action (default create)"},
                "notes": {"type": "string", "description": "Optional submission notes"},
            },
            "required": ["workflow_type", "requested_module", "payload"],
        },
        required_roles=WORKFLOW_SUBMIT_ROLES,
        handler=submit_workflow_request,
    ),
    Tool(
        name="respond_info_request",
        description="Respond to a workflow information request by resubmitting updated payload.",
        parameters={
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "Workflow request UUID"},
                "payload": {"type": "object", "description": "Updated workflow payload fields"},
                "response_notes": {"type": "string", "description": "Optional notes for the resubmission"},
            },
            "required": ["request_id"],
        },
        required_roles=WORKFLOW_SUBMIT_ROLES,
        handler=respond_info_request,
    ),
]


def get_toolset(roles: list[str] | None) -> list[Tool]:
    normalized = _normalize_roles(roles)
    if "master_admin" in normalized:
        return list(READ_TOOLS)
    return [tool for tool in READ_TOOLS if normalized.intersection(tool.required_roles)]


def get_write_toolset(roles: list[str] | None) -> list[Tool]:
    normalized = _normalize_roles(roles)
    if "master_admin" in normalized:
        return list(WRITE_TOOLS)
    return [tool for tool in WRITE_TOOLS if normalized.intersection(tool.required_roles)]


def get_tool_by_name(name: str) -> Tool | None:
    for tool in READ_TOOLS + WRITE_TOOLS:
        if tool.name == name:
            return tool
    return None


def toolset_to_gemini_tools(toolset: list[Tool]) -> list:
    from google.genai import types as gtypes

    if not toolset:
        return []
    declarations = [tool.to_gemini_declaration() for tool in toolset]
    return [gtypes.Tool(function_declarations=declarations)]
