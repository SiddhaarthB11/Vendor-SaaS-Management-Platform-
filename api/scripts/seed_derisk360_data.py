"""Seed operational data for the Derisk360 Group organisation.

Inserts vendors, subscriptions, budgets, contracts, payments, and licences
that reflect the current real state of the app. Each record is only inserted
if it does not already exist (checked by name/title within the org), so this
script is safe to run on every startup without creating duplicates.

Run automatically from api-entrypoint.sh after login users are seeded.
"""

import psycopg
import os

DATABASE_URL = os.environ["DATABASE_URL"]


def _exists(cur, table: str, where: str, params: tuple) -> bool:
    cur.execute(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1", params)
    return cur.fetchone() is not None


def main() -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # ── Resolve Derisk360 Group org ID ───────────────────────────────
            cur.execute(
                "SELECT id FROM slmct.organisations WHERE code = 'derisk360_group' LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                print("derisk360_group org not found — skipping data seed.")
                return
            org_id = str(row[0])

            # ── Vendors ──────────────────────────────────────────────────────
            vendors_data = [
                ("Adobe",     "Adobe Systems Inc.",    "https://adobe.com",         "licensing@adobe.com"),
                ("Microsoft", "Microsoft Corporation", "https://microsoft.com",      "licensing@microsoft.com"),
                ("Figma",     "Figma, Inc.",           "https://www.figma.com",      ""),
                ("Anthropic", "Anthropic PBC",         "https://www.anthropic.com",  "sales@anthropic.com"),
            ]
            vendor_ids: dict[str, str] = {}
            for name, legal, website, contact_email in vendors_data:
                if not _exists(cur, "slmct.vendors",
                               "organisation_id = %s AND name = %s AND status != 'inactive'",
                               (org_id, name)):
                    cur.execute(
                        """
                        INSERT INTO slmct.vendors
                          (organisation_id, name, legal_name, website_url, contact_email, status)
                        VALUES (%s, %s, %s, %s, %s, 'active')
                        """,
                        (org_id, name, legal, website, contact_email or None),
                    )
                cur.execute(
                    "SELECT id FROM slmct.vendors WHERE organisation_id = %s AND name = %s AND status != 'inactive' LIMIT 1",
                    (org_id, name),
                )
                r = cur.fetchone()
                if r:
                    vendor_ids[name] = str(r[0])

            # ── Subscriptions ────────────────────────────────────────────────
            subscriptions_data = [
                # (name, vendor_name, category, department, billing_cycle, amount, currency, status, start_date, renewal_date, notes)
                ("Adobe Premier Pro",  "Adobe",     "Design",        "Sales",                "monthly", 10000.00, "INR", "active", "2026-06-24", "2026-07-25", "Will be used to make better designs."),
                ("Microsoft Teams",    "Microsoft", "Collaboration", "Software Engineering", "monthly", 10000.00, "AED", "active", "2026-06-24", "2026-07-25", ""),
                ("Adobe Acrobat Pro",  "Adobe",     None,            "Sales",                "annual",    880.96, "AED", "active", None,         "2027-06-27", ""),
                ("Slack Pro",          None,        None,            "Software Engineering", "monthly",   150.00, "AED", "active", None,         None,         ""),
                ("Figma",              None,        None,            "Sales",                "monthly", 10000.00, "AED", "active", "2026-06-23", "2026-07-25", "Will be used to make better designs"),
            ]
            sub_ids: dict[str, str] = {}
            for name, vendor_name, category, dept, billing, amount, currency, status, start, renewal, notes in subscriptions_data:
                vid = vendor_ids.get(vendor_name) if vendor_name else None
                if not _exists(cur, "slmct.subscriptions",
                               "organisation_id = %s AND name = %s AND status != 'cancelled'",
                               (org_id, name)):
                    cur.execute(
                        """
                        INSERT INTO slmct.subscriptions
                          (organisation_id, vendor_id, name, category, department, billing_cycle,
                           amount, currency_code, status, start_date, renewal_date, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (org_id, vid, name, category, dept, billing, amount, currency, status,
                         start or None, renewal or None, notes or None),
                    )
                cur.execute(
                    "SELECT id FROM slmct.subscriptions WHERE organisation_id = %s AND name = %s AND status != 'cancelled' LIMIT 1",
                    (org_id, name),
                )
                r = cur.fetchone()
                if r:
                    sub_ids[name] = str(r[0])

            # ── Budgets ──────────────────────────────────────────────────────
            budgets_data = [
                # (fiscal_year, department, allocated_amount, currency, status, notes)
                (2026, "Executive",            200000.00,  "AED", "approved", "Executive discretionary and strategic programme budget."),
                (2026, "Software Engineering", 460000.00,  "AED", "approved", "Developer tools, cloud services, and engineering subscriptions."),
                (2026, "IT",                   295000.00,  "AED", "approved", "Core infrastructure, SaaS tooling, and IT operations."),
                (2026, "Security",             185000.00,  "AED", "approved", "Security tooling, compliance, and monitoring platforms."),
                (2026, "Finance & Accounts",   125000.00,  "AED", "approved", "Finance systems, reporting tools, and compliance software."),
                (2026, "Sales",                112000.00,  "AED", "approved", "CRM, sales enablement, and customer engagement platforms."),
                (2026, "Product",              110000.00,  "AED", "approved", "Product discovery, analytics, and collaboration tools."),
                (2026, "Operations",            98000.00,  "AED", "approved", "Operations tooling, workflow automation, and vendor management."),
                (2026, "Marketing",             95000.00,  "AED", "approved", "Campaign platforms, design tools, and martech stack."),
                (2026, "Human Resources",       88000.00,  "AED", "approved", "HRIS, recruitment, and employee experience tools."),
                (2026, "Support",               86000.00,  "AED", "approved", "Customer support and helpdesk tooling."),
                (2026, "Legal",                 20000.00,  "AED", "approved", "Legal research, contract management, and compliance tools."),
            ]
            for fy, dept, amount, currency, status, notes in budgets_data:
                cur.execute(
                    """
                    INSERT INTO slmct.budgets
                      (organisation_id, fiscal_year, department, allocated_amount, currency_code, status, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (organisation_id, department, fiscal_year) DO NOTHING
                    """,
                    (org_id, fy, dept, amount, currency, status, notes),
                )

            # ── Contracts ────────────────────────────────────────────────────
            contracts_data = [
                # (title, vendor_name, sub_name, contract_type, start, end, value, currency, auto_renew, notice_days, status)
                (
                    "Adobe Creative Cloud Order Form",
                    "Adobe", "Adobe Premier Pro",
                    "Order Form", "2026-01-15", "2027-01-14",
                    42000.00, "AED", True, 45, "draft",
                ),
                (
                    "Slack Enterprise Master Agreement",
                    None, "Slack Pro",
                    "MSA", "2025-04-01", "2026-03-31",
                    12000.00, "AED", True, 30, "active",
                ),
                (
                    "SOFTWARE AS A SERVICE SUBSCRIPTION AGREEMENT",
                    "Microsoft", "Microsoft Teams",
                    "SaaS", "2026-07-01", "2027-06-30",
                    13200.00, "USD", True, 30, "draft",
                ),
            ]
            for title, vendor_name, sub_name, ctype, start, end, value, currency, auto_renew, notice, status in contracts_data:
                vid = vendor_ids.get(vendor_name) if vendor_name else None
                sid = sub_ids.get(sub_name) if sub_name else None
                if not _exists(cur, "slmct.contracts",
                               "organisation_id = %s AND title = %s AND status != 'terminated'",
                               (org_id, title)):
                    cur.execute(
                        """
                        INSERT INTO slmct.contracts
                          (organisation_id, vendor_id, subscription_id, title, contract_type,
                           start_date, end_date, value, currency_code, auto_renew, notice_period_days, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (org_id, vid, sid, title, ctype, start, end, value, currency, auto_renew, notice, status),
                    )

            # ── Payments ─────────────────────────────────────────────────────
            payments_data = [
                # (name, vendor_name, sub_name, amount, currency, status, payment_date, due_date, reference, notes)
                ("Microsoft 365 Business Premium", None, None,
                 48000.00, "AED", "paid", "2026-05-25", "2026-05-23", "MSFT-EA-2026-001",
                 "Annual Microsoft 365 enterprise renewal."),
                ("Adobe Creative Cloud Teams", "Adobe", None,
                 42000.00, "AED", "paid", "2026-06-26", "2026-06-29", "ADB-OF-2026-014",
                 "Adobe order form payment."),
                ("Slack Enterprise Grid", None, "Slack Pro",
                 12000.00, "AED", "paid", None, "2026-07-06", "SLK-INV-2026-Q2",
                 "Slack renewal invoice."),
            ]
            for name, vendor_name, sub_name, amount, currency, status, pay_date, due_date, ref, notes in payments_data:
                vid = vendor_ids.get(vendor_name) if vendor_name else None
                sid = sub_ids.get(sub_name) if sub_name else None
                if not _exists(cur, "slmct.payments",
                               "organisation_id = %s AND reference = %s AND status != 'cancelled'",
                               (org_id, ref)):
                    cur.execute(
                        """
                        INSERT INTO slmct.payments
                          (organisation_id, vendor_id, subscription_id, name, amount, currency_code,
                           status, payment_date, due_date, reference, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (org_id, vid, sid, name, amount, currency, status,
                         pay_date or None, due_date or None, ref or None, notes or None),
                    )

            # ── Licences ─────────────────────────────────────────────────────
            cur.execute(
                "SELECT id FROM slmct.people WHERE organisation_id = %s AND employee_number = 'EMP-0001' LIMIT 1",
                (org_id,),
            )
            emp1 = cur.fetchone()
            emp1_id = str(emp1[0]) if emp1 else None

            licences_data = [
                # (licence_name, sub_name, assigned_to_id, status, assigned_at, expires_at)
                ("Slack Enterprise Grid Seat", "Slack Pro",         emp1_id, "assigned",  "2026-06-24", None),
                ("Microsoft Teams Seat",       "Microsoft Teams",   emp1_id, "assigned",  "2026-06-27", None),
                ("Adobe Acrobat Pro Seat",     "Adobe Acrobat Pro", emp1_id, "assigned",  "2026-06-27", None),
                ("Figma Seat",                 "Figma",             None,    "available", None,         "2027-07-07"),
            ]
            for lic_name, sub_name, person_id, status, assigned_at, expires_at in licences_data:
                sid = sub_ids.get(sub_name)
                if not sid:
                    continue
                if not _exists(cur, "slmct.licences",
                               "organisation_id = %s AND licence_name = %s AND status != 'revoked'",
                               (org_id, lic_name)):
                    cur.execute(
                        """
                        INSERT INTO slmct.licences
                          (organisation_id, subscription_id, assigned_to_person_id, licence_name,
                           status, assigned_at, expires_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (org_id, sid, person_id or None, lic_name,
                         status, assigned_at or None, expires_at or None),
                    )

        conn.commit()
        print("Derisk360 Group operational data seeded successfully.")


if __name__ == "__main__":
    main()
