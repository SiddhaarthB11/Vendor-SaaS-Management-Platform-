"""Remove all operational/demo data while preserving login users and auth metadata."""

import psycopg

from app.settings import get_settings


def main() -> None:
    settings = get_settings()
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM slmct.tool_requests;
                DELETE FROM slmct.workflow_requests;
                DELETE FROM slmct.audit_logs;
                DELETE FROM slmct.licences;
                DELETE FROM slmct.payments;
                DELETE FROM slmct.contracts;
                DELETE FROM slmct.subscriptions;
                DELETE FROM slmct.budgets;
                DELETE FROM slmct.vendors;
                DELETE FROM slmct.people p
                WHERE NOT EXISTS (
                  SELECT 1 FROM slmct.app_users u WHERE u.person_id = p.id
                );
                """
            )
            cur.execute("SELECT COUNT(*) FROM slmct.app_users")
            user_count = cur.fetchone()[0]
        conn.commit()

    print(f"Cleared operational data. Preserved {user_count} login user(s).")


if __name__ == "__main__":
    main()
