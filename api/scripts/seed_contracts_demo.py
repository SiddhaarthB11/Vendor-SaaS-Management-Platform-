"""Seed three active demo contracts with linked vendors and subscriptions."""

import psycopg

from app.settings import get_settings

DEMO_CONTRACTS = [
    {
        "vendor": ("Microsoft", "Microsoft Corporation", "https://microsoft.com", "licensing@microsoft.com"),
        "subscription": ("Microsoft 365 Business Premium", "Productivity", "IT", 48000),
        "contract": {
            "title": "Microsoft 365 Enterprise Agreement",
            "contract_number": "MSFT-EA-2026-001",
            "contract_type": "SaaS",
            "start_date": "2025-01-01",
            "end_date": "2026-12-31",
            "value": 48000,
            "auto_renew": False,
            "notice_period_days": 60,
            "owner": "IT Admin",
            "status": "active",
            "document_name": "m365_enterprise_agreement.pdf",
            "notes": "Annual enterprise agreement covering all business premium seats.",
        },
    },
    {
        "vendor": ("Slack", "Slack Technologies, Inc.", "https://slack.com", "billing@slack.com"),
        "subscription": ("Slack Enterprise Grid", "Collaboration", "Software Engineering", 12000),
        "contract": {
            "title": "Slack Enterprise Master Agreement",
            "contract_number": "SLK-CTR-2026",
            "contract_type": "MSA",
            "start_date": "2025-04-01",
            "end_date": "2026-03-31",
            "value": 12000,
            "auto_renew": True,
            "notice_period_days": 30,
            "owner": "Jane Smith",
            "status": "active",
            "document_name": "slack_enterprise_msa.pdf",
            "notes": "Standard enterprise grid agreement with 30-day renewal notice.",
        },
    },
    {
        "vendor": ("Adobe", "Adobe Systems Inc.", "https://adobe.com", "licensing@adobe.com"),
        "subscription": ("Adobe Creative Cloud Teams", "Design", "Marketing", 42000),
        "contract": {
            "title": "Adobe Creative Cloud Order Form",
            "contract_number": "ADB-OF-2026-014",
            "contract_type": "Order Form",
            "start_date": "2026-01-15",
            "end_date": "2027-01-14",
            "value": 42000,
            "auto_renew": True,
            "notice_period_days": 45,
            "owner": "Sarah Ahmed",
            "status": "draft",
            "document_name": "adobe_cc_order_form.pdf",
            "notes": "Pending legal review before activation.",
        },
    },
]


def main() -> None:
    settings = get_settings()
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM slmct.organisations WHERE code = 'derisk360_group' LIMIT 1")
            org = cur.fetchone()
            if not org:
                print("No organisation found for contract seeding.")
                return
            org_id = org[0]

            seeded = 0
            for item in DEMO_CONTRACTS:
                vendor_name, legal_name, website, email = item["vendor"]
                sub_name, category, department, amount = item["subscription"]
                contract = item["contract"]

                cur.execute(
                    """
                    SELECT COUNT(*) FROM slmct.contracts
                    WHERE organisation_id = %s AND title = %s AND status = %s
                    """,
                    (org_id, contract["title"], contract["status"]),
                )
                if cur.fetchone()[0]:
                    print(f"Skipping contract: {contract['title']}")
                    continue

                cur.execute(
                    """
                    INSERT INTO slmct.vendors (organisation_id, name, legal_name, website_url, contact_email, status)
                    VALUES (%s, %s, %s, %s, %s, 'active')
                    ON CONFLICT (organisation_id, name) DO UPDATE SET
                      legal_name = EXCLUDED.legal_name,
                      website_url = EXCLUDED.website_url,
                      contact_email = EXCLUDED.contact_email,
                      status = 'active',
                      updated_at = now()
                    RETURNING id
                    """,
                    (org_id, vendor_name, legal_name, website, email),
                )
                vendor_id = cur.fetchone()[0]

                cur.execute(
                    """
                    INSERT INTO slmct.subscriptions (
                      organisation_id, vendor_id, name, category, department,
                      billing_cycle, amount, currency_code, status
                    )
                    VALUES (%s, %s, %s, %s, %s, 'annual', %s, 'AED', 'active')
                    RETURNING id
                    """,
                    (org_id, vendor_id, sub_name, category, department, amount),
                )
                subscription_id = cur.fetchone()[0]

                cur.execute(
                    """
                    INSERT INTO slmct.contracts (
                      organisation_id, vendor_id, subscription_id, title, contract_number,
                      contract_type, start_date, end_date, value, currency_code, auto_renew,
                      notice_period_days, owner, status, document_name, notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'AED', %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        org_id,
                        vendor_id,
                        subscription_id,
                        contract["title"],
                        contract["contract_number"],
                        contract["contract_type"],
                        contract["start_date"],
                        contract["end_date"],
                        contract["value"],
                        contract["auto_renew"],
                        contract["notice_period_days"],
                        contract["owner"],
                        contract["status"],
                        contract["document_name"],
                        contract["notes"],
                    ),
                )
                seeded += 1
                print(f"Seeded contract: {contract['title']}")

        conn.commit()

    print(f"Contract demo seed complete ({seeded} new contract(s)).")


if __name__ == "__main__":
    main()
