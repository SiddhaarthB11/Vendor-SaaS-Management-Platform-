"""Quick smoke test for Phase 2a write tools."""

from __future__ import annotations

from app.db import get_connection
from app.tools import execute_tool, get_tool_by_name, get_write_toolset


def main() -> None:
    gen = get_connection()
    conn = next(gen)
    try:
        _run(conn)
    finally:
        gen.close()


def _run(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM slmct.organisations ORDER BY created_at LIMIT 1")
        org_id = cur.fetchone()["id"]

    finance_tools = [t.name for t in get_write_toolset(["finance"])]
    print("finance_write_tools:", finance_tools)

    tool = get_tool_by_name("create_budget")
    actor = {"roles": ["finance"], "email": "smoke-test@example.com"}
    result = execute_tool(
        tool,
        conn,
        org_id,
        actor,
        fiscal_year=2099,
        department="Phase2aSmokeTest",
        allocated_amount=12345,
        currency_code="AED",
    )
    if "error" in result:
        raise SystemExit(f"create_budget failed: {result['error']}")

    budget_id = result["budget"]["id"]
    print("created_budget:", budget_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT metadata
            FROM slmct.audit_logs
            WHERE entity_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (budget_id,),
        )
        audit = cur.fetchone()
    print("audit_metadata:", audit["metadata"] if audit else None)

    denied = execute_tool(
        get_tool_by_name("create_budget"),
        conn,
        org_id,
        {"roles": ["employee"]},
        fiscal_year=2099,
        department="Denied",
        allocated_amount=1,
    )
    print("employee_denied:", denied)

    with conn.cursor() as cur:
        cur.execute("DELETE FROM slmct.budgets WHERE id = %s", (budget_id,))
        conn.commit()
    print("cleanup_ok")


if __name__ == "__main__":
    main()
