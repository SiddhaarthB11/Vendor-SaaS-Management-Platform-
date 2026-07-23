"""Seed recycle bin demo items across three module types."""

import psycopg

from app.settings import get_settings

DEMO_ITEMS = [
    {
        "module": "vendors",
        "name": "Basecamp LLC",
        "status": "inactive",
        "sql": """
            INSERT INTO slmct.vendors (
              organisation_id, name, legal_name, website_url, contact_email, status
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (organisation_id, name) DO UPDATE SET
              status = EXCLUDED.status,
              updated_at = now() - INTERVAL '3 days'
        """,
        "params": lambda org_id: (
            org_id,
            "Basecamp LLC",
            "Basecamp LLC",
            "https://basecamp.com",
            "billing@basecamp.com",
            "inactive",
        ),
    },
    {
        "module": "subscriptions",
        "name": "Legacy CRM Suite",
        "status": "cancelled",
        "sql": """
            INSERT INTO slmct.subscriptions (
              organisation_id, name, category, department, billing_cycle,
              amount, currency_code, status, notes, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now() - INTERVAL '2 days')
        """,
        "params": lambda org_id: (
            org_id,
            "Legacy CRM Suite",
            "CRM",
            "Sales",
            "annual",
            9600,
            "AED",
            "cancelled",
            "Archived after migration to HubSpot.",
        ),
    },
    {
        "module": "contracts",
        "name": "Zoom Enterprise Agreement",
        "status": "terminated",
        "sql": """
            INSERT INTO slmct.contracts (
              organisation_id, title, contract_number, contract_type,
              start_date, end_date, value, currency_code, owner, status, notes, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() - INTERVAL '1 day')
        """,
        "params": lambda org_id: (
            org_id,
            "Zoom Enterprise Agreement",
            "ZOOM-ENT-2023-014",
            "Software Subscription",
            "2023-01-01",
            "2025-12-31",
            18000,
            "AED",
            "IT Admin",
            "terminated",
            "Contract ended early after vendor consolidation.",
        ),
    },
]


def main() -> None:
    settings = get_settings()
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM slmct.organisations WHERE code = 'derisk360_group' LIMIT 1")
            org = cur.fetchone()
            if not org:
                print("No organisation found for recycle bin seeding.")
                return
            org_id = org[0]

            seeded = 0
            for item in DEMO_ITEMS:
                if item["module"] == "contracts":
                    cur.execute(
                        "SELECT COUNT(*) FROM slmct.contracts WHERE organisation_id = %s AND title = %s AND status = %s",
                        (org_id, item["name"], item["status"]),
                    )
                elif item["module"] == "subscriptions":
                    cur.execute(
                        "SELECT COUNT(*) FROM slmct.subscriptions WHERE organisation_id = %s AND name = %s AND status = %s",
                        (org_id, item["name"], item["status"]),
                    )
                else:
                    cur.execute(
                        f"SELECT COUNT(*) FROM slmct.{item['module']} WHERE organisation_id = %s AND name = %s AND status = %s",
                        (org_id, item["name"], item["status"]),
                    )
                if cur.fetchone()[0]:
                    print(f"Skipping {item['module']} demo item (already present).")
                    continue

                cur.execute(item["sql"], item["params"](org_id))
                seeded += 1
                print(f"Seeded archived {item['module']} record: {item['name']}")

        conn.commit()

    print(f"Recycle bin demo seed complete ({seeded} new item(s)).")


if __name__ == "__main__":
    main()
