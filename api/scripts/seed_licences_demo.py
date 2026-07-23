"""Seed three demo licences linked to active subscriptions."""

import psycopg

from app.settings import get_settings

DEMO_LICENCES = [
    {
        "subscription_name": "Microsoft 365 Business Premium",
        "assignee_name": "IT_admin",
        "licence_name": "Microsoft 365 Business Premium Seat",
        "seat_reference": "M365-SEAT-001",
        "assigned_at": "2025-06-01",
        "expires_at": "2026-05-31",
        "status": "assigned",
        "notes": "Assigned to IT Admin for platform administration.",
    },
    {
        "subscription_name": "Slack Enterprise Grid",
        "assignee_name": "Finance",
        "licence_name": "Slack Enterprise Grid Seat",
        "seat_reference": "SLK-SEAT-001",
        "assigned_at": "2025-09-15",
        "expires_at": "2026-03-31",
        "status": "assigned",
        "notes": "Finance team collaboration seat.",
    },
    {
        "subscription_name": "Adobe Creative Cloud Teams",
        "assignee_name": None,
        "licence_name": "Adobe Creative Cloud Teams Seat",
        "seat_reference": "ADB-SEAT-004",
        "assigned_at": None,
        "expires_at": None,
        "status": "available",
        "notes": "Unassigned seat available for marketing team allocation.",
    },
]


def main() -> None:
    settings = get_settings()
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM slmct.organisations WHERE code = 'derisk360_group' LIMIT 1")
            org = cur.fetchone()
            if not org:
                print("No organisation found for licence seeding.")
                return
            org_id = org[0]

            seeded = 0
            for item in DEMO_LICENCES:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM slmct.licences
                    WHERE organisation_id = %s AND seat_reference = %s
                    """,
                    (org_id, item["seat_reference"]),
                )
                if cur.fetchone()[0]:
                    print(f"Skipping licence: {item['seat_reference']}")
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
                    print(f"Skipping licence {item['seat_reference']}: subscription not found ({item['subscription_name']}).")
                    continue
                subscription_id = sub[0]

                person_id = None
                if item["assignee_name"]:
                    cur.execute(
                        """
                        SELECT id FROM slmct.people
                        WHERE organisation_id = %s AND full_name = %s
                        LIMIT 1
                        """,
                        (org_id, item["assignee_name"]),
                    )
                    person = cur.fetchone()
                    if person:
                        person_id = person[0]

                cur.execute(
                    """
                    INSERT INTO slmct.licences (
                      organisation_id, subscription_id, assigned_to_person_id,
                      licence_name, seat_reference, assigned_at, expires_at, status, notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::slmct.licence_status, %s)
                    """,
                    (
                        org_id,
                        subscription_id,
                        person_id,
                        item["licence_name"],
                        item["seat_reference"],
                        item["assigned_at"],
                        item["expires_at"],
                        item["status"],
                        item["notes"],
                    ),
                )
                seeded += 1
                print(f"Seeded licence: {item['licence_name']} ({item['seat_reference']})")

        conn.commit()

    print(f"Licence demo seed complete ({seeded} new licence(s)).")


if __name__ == "__main__":
    main()
