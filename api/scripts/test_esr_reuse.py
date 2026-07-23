"""Quick smoke test: two ESR completions share one subscription."""
import os
import sys

import httpx
import psycopg
from psycopg.rows import dict_row

BASE = "http://localhost:8000"
PRODUCT = "[TEST-REUSE] Claude Pro Seat Test"


def main() -> int:
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    cur = conn.cursor()
    cur.execute("SELECT id FROM slmct.organisations WHERE code = 'derisk360_group' LIMIT 1")
    org_id = str(cur.fetchone()["id"])

    def user(email: str):
        cur.execute("SELECT id, email FROM slmct.app_users WHERE email = %s LIMIT 1", (email,))
        return cur.fetchone()

    e3, e4 = user("deriskemployee3@outlook.com"), user("deriskemployee4@outlook.com")
    it, fin, lm = user("deriskit@outlook.com"), user("deriskfinance@outlook.com"), user("deriskline@outlook.com")
    if not all([e3, e4, it, fin, lm]):
        print("SKIP: missing seed users")
        return 0

    wf_ids: list[str] = []

    def run_esr(emp: dict) -> str:
        r = httpx.post(
            f"{BASE}/workflow-requests",
            json={
                "workflow_type": "employee_software_request",
                "organisation_id": org_id,
                "requested_module": "subscriptions",
                "actor_email": emp["email"],
                "actor_user_id": str(emp["id"]),
                "actor_roles": ["employee"],
                "payload": {
                    "name": PRODUCT,
                    "department": "Software Engineering",
                    "justification": "Reuse test",
                    "amount": 25,
                    "currency_code": "AED",
                    "billing_cycle": "monthly",
                },
            },
            timeout=30,
        )
        r.raise_for_status()
        wf_id = str(r.json()["id"])
        wf_ids.append(wf_id)
        httpx.post(
            f"{BASE}/workflow-requests/{wf_id}/line-manager-approve",
            json={"actor_email": lm["email"], "actor_user_id": str(lm["id"]), "actor_roles": ["line_manager"]},
            timeout=30,
        ).raise_for_status()
        httpx.post(
            f"{BASE}/workflow-requests/{wf_id}/validate-budget",
            json={"actor_email": fin["email"], "actor_user_id": str(fin["id"]), "actor_roles": ["finance"], "approved": True},
            timeout=30,
        ).raise_for_status()
        httpx.post(
            f"{BASE}/workflow-requests/{wf_id}/complete",
            json={
                "actor_email": it["email"],
                "actor_user_id": str(it["id"]),
                "actor_roles": ["it_admin"],
                "activation_method": "company_account",
                "username": "test",
                "password": "Test@123",
                "assigned_employee_email": emp["email"],
                "assigned_employee_name": emp["email"],
            },
            timeout=30,
        ).raise_for_status()
        return wf_id

    run_esr(e3)
    run_esr(e4)

    cur.execute(
        """
        SELECT COUNT(*) AS c FROM slmct.subscriptions
        WHERE organisation_id = %s AND lower(name) = lower(%s) AND status = 'active'
        """,
        (org_id, PRODUCT),
    )
    sub_count = cur.fetchone()["c"]
    cur.execute(
        """
        SELECT COUNT(*) AS c FROM slmct.licences l
        JOIN slmct.subscriptions s ON s.id = l.subscription_id
        WHERE s.organisation_id = %s AND lower(s.name) = lower(%s) AND l.status = 'assigned'
        """,
        (org_id, PRODUCT),
    )
    lic_count = cur.fetchone()["c"]
    print(f"active_subscriptions={sub_count} assigned_licences={lic_count}")

    for wf_id in wf_ids:
        cur.execute("DELETE FROM slmct.email_logs WHERE workflow_request_id = %s", (wf_id,))
        cur.execute("DELETE FROM slmct.workflow_status_history WHERE workflow_request_id = %s", (wf_id,))
        cur.execute("DELETE FROM slmct.payments WHERE reference LIKE %s", (f"WF-{wf_id[:8].upper()}%",))
    cur.execute(
        """
        DELETE FROM slmct.licences l USING slmct.subscriptions s
        WHERE l.subscription_id = s.id AND s.organisation_id = %s AND lower(s.name) = lower(%s)
        """,
        (org_id, PRODUCT),
    )
    cur.execute(
        "DELETE FROM slmct.subscriptions WHERE organisation_id = %s AND lower(name) = lower(%s)",
        (org_id, PRODUCT),
    )
    for wf_id in wf_ids:
        cur.execute("DELETE FROM slmct.workflow_requests WHERE id = %s", (wf_id,))
    conn.commit()
    conn.close()

    ok = sub_count == 1 and lic_count == 2
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
