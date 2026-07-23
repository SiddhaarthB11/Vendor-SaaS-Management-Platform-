"""Sweep leftover rows created by diagnostic / eval / autofix test runs."""

from __future__ import annotations

import logging
from typing import Any

from psycopg import Connection

log = logging.getLogger(__name__)

# Payload fragments written by diagnostic suites (case-insensitive match).
_WORKFLOW_PAYLOAD_MARKERS = (
    "%diagnostic test%",
    "%diagnostics test%",
    "%full lifecycle diagnostic%",
    "%ai diagnostics test%",
    "%opeval%",
)

# Named artefacts created by upload-export and workflow suites.
_SUBSCRIPTION_NAME_PATTERNS = (
    "[DIAG]%",
    "Lifecycle Diag%",
    "Diag Upload Test Sub",
    "Diag Sub%",
)
_LICENCE_NAME_PATTERNS = ("Diag Licence%", "Lifecycle Diag Seat%")
_VENDOR_NAME_PATTERNS = ("Diag Vendor%",)
_CONTRACT_TITLE_PATTERNS = ("Diag Contract%",)
_PAYMENT_NAME_PATTERNS = ("Diag Payment%",)
_BUDGET_DEPARTMENT_PATTERNS = ("DiagDept%",)

# People / portal users created only for diagnostics (never production seed accounts).
_PEOPLE_EMAIL_PATTERNS = (
    "diag.%@derisk360.test",
    "diag.%@diagtest.internal",
    "%@diagtest.internal",
    "%@operator-eval.internal",
    "%@copilot-eval.internal",
)
_PEOPLE_NAME_PATTERNS = (
    "Diag Offboard%",
    "Diag Lifecycle%",
    "Diag Employee%",
)


def _rowcount(cur) -> int:
    return int(cur.rowcount or 0)


def _cleanup_eval_fixture_orgs(conn: Connection, stats: dict[str, int]) -> None:
    try:
        from app.copilot_eval import _cleanup_eval_org as cleanup_copilot

        cleanup_copilot(conn)
        stats["copilot_eval_org"] = 1
    except Exception as exc:
        log.warning("diagnostics cleanup: copilot eval org — %s", exc)
        stats["copilot_eval_org_error"] = 1

    try:
        from app.operator_eval import _cleanup_eval_org as cleanup_operator

        cleanup_operator(conn)
        stats["operator_eval_org"] = 1
    except Exception as exc:
        log.warning("diagnostics cleanup: operator eval org — %s", exc)
        stats["operator_eval_org_error"] = 1


def _delete_workflow_graph(conn: Connection, workflow_ids: list[str], stats: dict[str, int]) -> None:
    if not workflow_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM slmct.email_logs WHERE workflow_request_id = ANY(%s::uuid[])",
            (workflow_ids,),
        )
        stats["email_logs"] = stats.get("email_logs", 0) + _rowcount(cur)
        cur.execute(
            "DELETE FROM slmct.workflow_status_history WHERE workflow_request_id = ANY(%s::uuid[])",
            (workflow_ids,),
        )
        stats["workflow_status_history"] = stats.get("workflow_status_history", 0) + _rowcount(cur)
        for wf_id in workflow_ids:
            cur.execute(
                "DELETE FROM slmct.payments WHERE reference LIKE %s",
                (f"WF-{wf_id[:8].upper()}%",),
            )
            stats["payments"] = stats.get("payments", 0) + _rowcount(cur)
        cur.execute(
            "DELETE FROM slmct.audit_logs WHERE entity_id::text = ANY(%s)",
            (workflow_ids,),
        )
        stats["audit_logs"] = stats.get("audit_logs", 0) + _rowcount(cur)
        cur.execute(
            "DELETE FROM slmct.workflow_requests WHERE id = ANY(%s::uuid[])",
            (workflow_ids,),
        )
        stats["workflow_requests"] = stats.get("workflow_requests", 0) + _rowcount(cur)


def _collect_diag_workflow_ids(conn: Connection, org_id: str) -> list[str]:
    ids: set[str] = set()
    with conn.cursor() as cur:
        for marker in _WORKFLOW_PAYLOAD_MARKERS:
            cur.execute(
                """
                SELECT id::text
                FROM slmct.workflow_requests
                WHERE organisation_id = %s AND payload::text ILIKE %s
                """,
                (org_id, marker),
            )
            ids.update(str(row["id"]) for row in cur.fetchall())

        for pattern in _SUBSCRIPTION_NAME_PATTERNS:
            cur.execute(
                """
                SELECT wr.id::text
                FROM slmct.workflow_requests wr
                WHERE wr.organisation_id = %s
                  AND wr.payload ->> 'subscription_id' IN (
                      SELECT id::text FROM slmct.subscriptions
                      WHERE organisation_id = %s AND name ILIKE %s
                  )
                """,
                (org_id, org_id, pattern),
            )
            ids.update(str(row["id"]) for row in cur.fetchall())
    return sorted(ids)


def _delete_pattern_rows(
    conn: Connection,
    *,
    table: str,
    column: str,
    patterns: tuple[str, ...],
    org_id: str | None = None,
    stats_key: str,
    stats: dict[str, int],
) -> None:
    with conn.cursor() as cur:
        for pattern in patterns:
            if org_id:
                cur.execute(
                    f"DELETE FROM {table} WHERE organisation_id = %s AND {column} ILIKE %s",
                    (org_id, pattern),
                )
            else:
                cur.execute(f"DELETE FROM {table} WHERE {column} ILIKE %s", (pattern,))
            stats[stats_key] = stats.get(stats_key, 0) + _rowcount(cur)


def _delete_diag_people(conn: Connection, org_id: str, stats: dict[str, int]) -> None:
    person_ids: list[str] = []
    with conn.cursor() as cur:
        for pattern in _PEOPLE_EMAIL_PATTERNS:
            cur.execute(
                """
                SELECT id::text FROM slmct.people
                WHERE organisation_id = %s AND work_email ILIKE %s
                """,
                (org_id, pattern),
            )
            person_ids.extend(str(row["id"]) for row in cur.fetchall())
        for pattern in _PEOPLE_NAME_PATTERNS:
            cur.execute(
                """
                SELECT id::text FROM slmct.people
                WHERE organisation_id = %s AND full_name ILIKE %s
                """,
                (org_id, pattern),
            )
            person_ids.extend(str(row["id"]) for row in cur.fetchall())

    person_ids = sorted(set(person_ids))
    if not person_ids:
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text FROM slmct.app_users
            WHERE person_id = ANY(%s::uuid[])
            """,
            (person_ids,),
        )
        user_ids = [str(row["id"]) for row in cur.fetchall()]
        if user_ids:
            cur.execute(
                "DELETE FROM slmct.user_roles WHERE user_id = ANY(%s::uuid[])",
                (user_ids,),
            )
            stats["user_roles"] = stats.get("user_roles", 0) + _rowcount(cur)
            cur.execute(
                "DELETE FROM slmct.app_users WHERE id = ANY(%s::uuid[])",
                (user_ids,),
            )
            stats["app_users"] = stats.get("app_users", 0) + _rowcount(cur)

        cur.execute(
            "DELETE FROM slmct.licences WHERE assigned_to_person_id = ANY(%s::uuid[])",
            (person_ids,),
        )
        stats["licences"] = stats.get("licences", 0) + _rowcount(cur)
        cur.execute(
            "DELETE FROM slmct.audit_logs WHERE entity_id::text = ANY(%s)",
            (person_ids,),
        )
        stats["audit_logs"] = stats.get("audit_logs", 0) + _rowcount(cur)
        cur.execute(
            "DELETE FROM slmct.people WHERE id = ANY(%s::uuid[])",
            (person_ids,),
        )
        stats["people"] = stats.get("people", 0) + _rowcount(cur)


def _cleanup_main_org_artifacts(conn: Connection, stats: dict[str, int]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text FROM slmct.organisations
            WHERE code = 'derisk360_group' AND is_active = true
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                """
                SELECT id::text FROM slmct.organisations
                WHERE is_active = true
                ORDER BY created_at
                LIMIT 1
                """
            )
            row = cur.fetchone()
    if not row:
        return

    org_id = str(row["id"])
    workflow_ids = _collect_diag_workflow_ids(conn, org_id)
    _delete_workflow_graph(conn, workflow_ids, stats)

    with conn.cursor() as cur:
        for pattern in _SUBSCRIPTION_NAME_PATTERNS:
            cur.execute(
                """
                DELETE FROM slmct.licences
                WHERE subscription_id IN (
                    SELECT id FROM slmct.subscriptions
                    WHERE organisation_id = %s AND name ILIKE %s
                )
                """,
                (org_id, pattern),
            )
            stats["licences"] = stats.get("licences", 0) + _rowcount(cur)
            cur.execute(
                "DELETE FROM slmct.subscriptions WHERE organisation_id = %s AND name ILIKE %s",
                (org_id, pattern),
            )
            stats["subscriptions"] = stats.get("subscriptions", 0) + _rowcount(cur)

    _delete_pattern_rows(
        conn,
        table="slmct.licences",
        column="licence_name",
        patterns=_LICENCE_NAME_PATTERNS,
        org_id=org_id,
        stats_key="licences",
        stats=stats,
    )
    _delete_pattern_rows(
        conn,
        table="slmct.vendors",
        column="name",
        patterns=_VENDOR_NAME_PATTERNS,
        org_id=org_id,
        stats_key="vendors",
        stats=stats,
    )
    _delete_pattern_rows(
        conn,
        table="slmct.contracts",
        column="title",
        patterns=_CONTRACT_TITLE_PATTERNS,
        org_id=org_id,
        stats_key="contracts",
        stats=stats,
    )
    _delete_pattern_rows(
        conn,
        table="slmct.payments",
        column="name",
        patterns=_PAYMENT_NAME_PATTERNS,
        org_id=org_id,
        stats_key="payments",
        stats=stats,
    )
    _delete_pattern_rows(
        conn,
        table="slmct.budgets",
        column="department",
        patterns=_BUDGET_DEPARTMENT_PATTERNS,
        org_id=org_id,
        stats_key="budgets",
        stats=stats,
    )
    _delete_diag_people(conn, org_id, stats)

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM slmct.operator_plans
            WHERE organisation_id = %s
              AND (
                instruction ILIKE '%%OpEval%%'
                OR instruction ILIKE '%%diagnostic%%'
              )
            """,
            (org_id,),
        )
        stats["operator_plans"] = stats.get("operator_plans", 0) + _rowcount(cur)


def _cleanup_orphan_email_logs(conn: Connection, stats: dict[str, int]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM slmct.email_logs el
            WHERE el.workflow_request_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM slmct.workflow_requests wr
                WHERE wr.id = el.workflow_request_id
              )
            """
        )
        stats["orphan_email_logs"] = _rowcount(cur)


def cleanup_all_diagnostic_artifacts(conn: Connection) -> dict[str, Any]:
    """
    Remove diagnostic / eval / autofix test data left in the database.
    Safe to run after judge or autofix jobs; idempotent.
    """
    stats: dict[str, int] = {}
    errors: list[str] = []

    try:
        _cleanup_eval_fixture_orgs(conn, stats)
    except Exception as exc:
        errors.append(f"eval orgs: {exc}")

    try:
        _cleanup_orphan_email_logs(conn, stats)
    except Exception as exc:
        errors.append(f"orphan email logs: {exc}")

    try:
        _cleanup_main_org_artifacts(conn, stats)
    except Exception as exc:
        errors.append(f"main org artifacts: {exc}")
        log.warning("diagnostics cleanup: main org — %s", exc)

    try:
        conn.commit()
    except Exception as exc:
        conn.rollback()
        errors.append(f"commit: {exc}")

    total_deleted = sum(
        v for k, v in stats.items()
        if isinstance(v, int) and not k.endswith("_error") and k not in {"copilot_eval_org", "operator_eval_org"}
    )
    log.info("diagnostics cleanup complete — %s rows affected (%s)", total_deleted, stats)

    return {
        "status": "ok" if not errors else "partial",
        "deleted": stats,
        "total_rows": total_deleted,
        "errors": errors,
    }
