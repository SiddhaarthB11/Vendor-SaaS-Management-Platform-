"""Seed three demo payments linked to subscriptions, vendors, and budgets."""

from datetime import date, timedelta

import psycopg

from app.settings import get_settings

TODAY = date.today()

DEMO_PAYMENTS = [
    {
        "subscription_name": "Microsoft 365 Business Premium",
        "vendor_name": "Microsoft",
        "budget_department": "IT",
        "due_date": TODAY - timedelta(days=30),
        "payment_date": TODAY - timedelta(days=28),
        "amount": 48000,
        "status": "paid",
        "reference": "MSFT-EA-2026-001",
        "notes": "Annual Microsoft 365 enterprise renewal — cleared by Finance.",
    },
    {
        "subscription_name": "Slack Enterprise Grid",
        "vendor_name": "Slack",
        "budget_department": "IT",
        "due_date": TODAY + timedelta(days=14),
        "payment_date": None,
        "amount": 12000,
        "status": "planned",
        "reference": "SLK-INV-2026-Q2",
        "notes": "Upcoming Slack renewal invoice — awaiting approval to release.",
    },
    {
        "subscription_name": "Adobe Creative Cloud Teams",
        "vendor_name": "Adobe",
        "budget_department": "Marketing",
        "due_date": TODAY + timedelta(days=7),
        "payment_date": None,
        "amount": 42000,
        "status": "pending",
        "reference": "ADB-OF-2026-014",
        "notes": "Adobe order form payment submitted — pending bank clearance.",
    },
]


def main() -> None:
    settings = get_settings()
    fiscal_year = TODAY.year

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM slmct.organisations WHERE code = 'derisk360_group' LIMIT 1")
            org = cur.fetchone()
            if not org:
                print("No organisation found for payment seeding.")
                return
            org_id = org[0]

            seeded = 0
            for item in DEMO_PAYMENTS:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM slmct.payments
                    WHERE organisation_id = %s AND reference = %s
                    """,
                    (org_id, item["reference"]),
                )
                if cur.fetchone()[0]:
                    print(f"Skipping payment: {item['reference']}")
                    continue

                cur.execute(
                    """
                    SELECT id FROM slmct.subscriptions
                    WHERE organisation_id = %s AND name = %s AND status = 'active'
                    LIMIT 1
                    """,
                    (org_id, item["subscription_name"]),
                )
                sub = cur.fetchone()
                if not sub:
                    print(f"Skipping {item['reference']}: subscription not found.")
                    continue

                cur.execute(
                    """
                    SELECT id FROM slmct.vendors
                    WHERE organisation_id = %s AND name = %s
                    LIMIT 1
                    """,
                    (org_id, item["vendor_name"]),
                )
                vendor = cur.fetchone()

                cur.execute(
                    """
                    SELECT id FROM slmct.budgets
                    WHERE organisation_id = %s AND department = %s AND fiscal_year = %s
                    LIMIT 1
                    """,
                    (org_id, item["budget_department"], fiscal_year),
                )
                budget = cur.fetchone()

                cur.execute(
                    """
                    INSERT INTO slmct.payments (
                      organisation_id, subscription_id, vendor_id, budget_id, name,
                      due_date, payment_date, amount, currency_code, status, reference, notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'AED', %s::slmct.payment_status, %s, %s)
                    """,
                    (
                        org_id,
                        sub[0],
                        vendor[0] if vendor else None,
                        budget[0] if budget else None,
                        item["subscription_name"],
                        item["due_date"],
                        item["payment_date"],
                        item["amount"],
                        item["status"],
                        item["reference"],
                        item["notes"],
                    ),
                )
                seeded += 1
                print(f"Seeded payment: {item['subscription_name']} ({item['status']}, AED {item['amount']:,.0f})")

        conn.commit()

    print(f"Payment demo seed complete ({seeded} new payment(s)).")


if __name__ == "__main__":
    main()
