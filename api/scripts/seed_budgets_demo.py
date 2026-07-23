"""Seed department budgets for the Budgets page demo."""

from datetime import date

import psycopg

from app.settings import get_settings

DEMO_BUDGETS = [
    ("IT", 250000, "approved", "Core infrastructure, SaaS tooling, and IT operations."),
    ("Software Engineering", 180000, "approved", "Developer tools, cloud services, and engineering subscriptions."),
    ("Marketing", 95000, "approved", "Campaign platforms, design tools, and martech stack."),
    ("Finance & Accounts", 120000, "locked", "Finance systems, reporting tools, and compliance software."),
    ("Product", 110000, "approved", "Product discovery, analytics, and collaboration tools."),
    ("Sales", 85000, "draft", "CRM, sales enablement, and customer engagement platforms."),
    ("Human Resources", 65000, "approved", "HRIS, recruitment, and employee experience tools."),
    ("Operations", 75000, "approved", "Operations tooling, workflow automation, and vendor management."),
]


def main() -> None:
    settings = get_settings()
    fiscal_year = date.today().year

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM slmct.organisations WHERE code = 'derisk360_group' LIMIT 1")
            org = cur.fetchone()
            if not org:
                print("No organisation found for budget seeding.")
                return
            org_id = org[0]

            seeded = 0
            for department, amount, status, notes in DEMO_BUDGETS:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM slmct.budgets
                    WHERE organisation_id = %s AND fiscal_year = %s AND department = %s
                    """,
                    (org_id, fiscal_year, department),
                )
                if cur.fetchone()[0]:
                    print(f"Skipping budget: {department} ({fiscal_year})")
                    continue

                cur.execute(
                    """
                    INSERT INTO slmct.budgets (
                      organisation_id, fiscal_year, department, allocated_amount,
                      currency_code, status, notes
                    )
                    VALUES (%s, %s, %s, %s, 'AED', %s::slmct.budget_status, %s)
                    """,
                    (org_id, fiscal_year, department, amount, status, notes),
                )
                seeded += 1
                print(f"Seeded budget: {department} — AED {amount:,.0f} ({status})")

        conn.commit()

    print(f"Budget demo seed complete ({seeded} new budget(s) for FY {fiscal_year}).")


if __name__ == "__main__":
    main()
