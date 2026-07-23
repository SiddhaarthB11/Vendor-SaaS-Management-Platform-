"""Seed login users for Derisk360 SLMCT.

All emails and passwords are loaded from environment variables.
See infra/.env for APP_USER_EMAIL_* and APP_USER_PASSWORD_* settings.
"""

import psycopg

from app.test_users import configured_test_users
from app.settings import get_settings


def main() -> None:
    settings = get_settings()
    users = configured_test_users()
    if not users:
        print(
            "No workflow test users configured. Set APP_USER_EMAIL_* and APP_USER_PASSWORD_* "
            "in infra/.env, then restart the API."
        )
        return

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM slmct.organisations WHERE code = 'derisk360_group' LIMIT 1")
            org = cur.fetchone()
            if not org:
                print("No derisk360_group organisation found; skipping login user seed.")
                return
            org_id = org[0]

            for entry in users:
                email = entry["email"]
                if not entry["password_hash"]:
                    print(
                        f"Skipping {entry['login']} ({email}) — set {entry['password_env']} in infra/.env."
                    )
                    continue

                cur.execute(
                    """
                    INSERT INTO slmct.people (
                      organisation_id, employee_number, full_name, work_email, department, job_title, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'active')
                    ON CONFLICT (employee_number) DO UPDATE SET
                      full_name = EXCLUDED.full_name,
                      work_email = EXCLUDED.work_email,
                      department = EXCLUDED.department,
                      job_title = EXCLUDED.job_title,
                      status = 'active',
                      updated_at = now()
                    RETURNING id
                    """,
                    (
                        org_id,
                        entry["employee_number"],
                        entry["label"],
                        email,
                        entry["department"],
                        entry["job_title"],
                    ),
                )
                person_id = cur.fetchone()[0]

                cur.execute(
                    """
                    UPDATE slmct.app_users
                    SET email = %s,
                        password_hash = %s,
                        status = 'active',
                        must_change_password = false,
                        temp_password_expires_at = NULL,
                        updated_at = now()
                    WHERE person_id = %s
                    RETURNING id
                    """,
                    (email, entry["password_hash"], person_id),
                )
                updated = cur.fetchone()
                if updated:
                    user_id = updated[0]
                else:
                    cur.execute(
                        """
                        INSERT INTO slmct.app_users (person_id, email, password_hash, status, mfa_enabled, must_change_password, temp_password_expires_at)
                        VALUES (%s, %s, %s, 'active', false, false, NULL)
                        ON CONFLICT (email) DO UPDATE SET
                          person_id = EXCLUDED.person_id,
                          password_hash = EXCLUDED.password_hash,
                          status = 'active',
                          must_change_password = false,
                          temp_password_expires_at = NULL,
                          updated_at = now()
                        RETURNING id
                        """,
                        (person_id, email, entry["password_hash"]),
                    )
                    user_id = cur.fetchone()[0]

                cur.execute(
                    """
                    INSERT INTO slmct.user_roles (user_id, role_id, organisation_id)
                    SELECT %s, roles.id, NULL
                    FROM slmct.roles roles
                    WHERE roles.code = %s
                    ON CONFLICT DO NOTHING
                    """,
                    (user_id, entry["role"]),
                )

                print(f"Seeded app user: login={entry['login']} role={entry['role']} email={email}")
        conn.commit()

    print("Workflow test users ready. Passwords are loaded from environment variables only.")


if __name__ == "__main__":
    main()
