import psycopg
from psycopg.rows import dict_row
from datetime import date, timedelta
from app.settings import get_settings

def main() -> None:
    settings = get_settings()
    
    print(f"Connecting to database to seed demo data...")
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # 1. Get organisation ID for 'derisk360_group'
            cur.execute("SELECT id FROM slmct.organisations WHERE code = 'derisk360_group'")
            org_row = cur.fetchone()
            if not org_row:
                print("Error: organisations table does not have 'derisk360_group' seeded.")
                return
            org_id = org_row["id"]
            print(f"Using Organisation ID: {org_id}")

            # Clean existing records to avoid duplicates and have a clean seed state
            print("Cleaning up old test data...")
            cur.execute("DELETE FROM slmct.contracts WHERE organisation_id = %s", (org_id,))
            cur.execute("DELETE FROM slmct.licences WHERE organisation_id = %s", (org_id,))
            cur.execute("DELETE FROM slmct.payments WHERE organisation_id = %s", (org_id,))
            cur.execute("DELETE FROM slmct.subscriptions WHERE organisation_id = %s", (org_id,))
            cur.execute("DELETE FROM slmct.budgets WHERE organisation_id = %s", (org_id,))
            cur.execute("DELETE FROM slmct.vendors WHERE organisation_id = %s AND name != 'Microsoft'", (org_id,))
            cur.execute("DELETE FROM slmct.people WHERE organisation_id = %s AND employee_number != 'ADM-0001'", (org_id,))

            # Get master admin person to map as subscription owner
            cur.execute("SELECT id FROM slmct.people WHERE employee_number = 'ADM-0001'")
            admin_person_id = cur.fetchone()["id"]

            # 2. Seed Vendors
            print("Seeding vendors...")
            vendors = [
                ("Slack Technologies, Inc.", "Slack", "https://slack.com", "billing@slack.com"),
                ("Zoom Video Communications", "Zoom", "https://zoom.us", "finance@zoom.us"),
                ("Adobe Systems Inc.", "Adobe", "https://adobe.com", "licensing@adobe.com"),
                ("Canva Pty Ltd", "Canva", "https://canva.com", "billing@canva.com"),
            ]
            vendor_ids = {}
            for legal_name, name, website, email in vendors:
                cur.execute(
                    """
                    INSERT INTO slmct.vendors (organisation_id, name, legal_name, website_url, contact_email, status)
                    VALUES (%s, %s, %s, %s, %s, 'active')
                    RETURNING id
                    """,
                    (org_id, name, legal_name, website, email)
                )
                vendor_ids[name] = cur.fetchone()["id"]
            
            # Fetch Microsoft vendor (already seeded or created)
            cur.execute("SELECT id FROM slmct.vendors WHERE organisation_id = %s AND name = 'Microsoft'", (org_id,))
            ms_row = cur.fetchone()
            if ms_row:
                vendor_ids["Microsoft"] = ms_row["id"]
            else:
                cur.execute(
                    """
                    INSERT INTO slmct.vendors (organisation_id, name, legal_name, website_url, contact_email, status)
                    VALUES (%s, 'Microsoft', 'Microsoft Corporation', 'https://microsoft.com', 'licensing@microsoft.com', 'active')
                    RETURNING id
                    """
                )
                vendor_ids["Microsoft"] = cur.fetchone()["id"]

            # 3. Seed People (Employees)
            print("Seeding employees...")
            employees = [
                ("John Doe", "EMP-1002", "john.doe@derisk360.local", "Engineering", "Software Engineer", "left_org"),
                ("Jane Smith", "EMP-1003", "jane.smith@derisk360.local", "Product", "Product Manager", "active"),
                ("Alice Webb", "EMP-1004", "alice.webb@derisk360.local", "Marketing", "Marketing Specialist", "inactive"),
                ("Bob Miller", "EMP-1005", "bob.miller@derisk360.local", "Sales", "Sales Director", "active"),
            ]
            employee_ids = {}
            for name, emp_num, email, dept, title, status in employees:
                cur.execute(
                    """
                    INSERT INTO slmct.people (organisation_id, employee_number, full_name, work_email, department, job_title, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::slmct.person_status)
                    RETURNING id
                    """,
                    (org_id, emp_num, name, email, dept, title, status)
                )
                employee_ids[name] = cur.fetchone()["id"]

            # 4. Seed Budgets
            print("Seeding budgets...")
            budget_departments = [
                ("IT", 100000.00),
                ("Software Engineering", 250000.00),
                ("Human Resources", 80000.00),
                ("Finance & Accounts", 150000.00),
                ("Marketing", 60000.00),
                ("Sales", 120000.00),
                ("Product", 90000.00),
                ("Operations", 70000.00)
            ]
            budget_ids = {}
            for dept, amt in budget_departments:
                cur.execute(
                    """
                    INSERT INTO slmct.budgets (organisation_id, fiscal_year, department, allocated_amount, currency_code, status)
                    VALUES (%s, %s, %s, %s, 'AED', 'approved')
                    RETURNING id, department
                    """,
                    (org_id, date.today().year, dept, amt)
                )
                res = cur.fetchone()
                budget_ids[res["department"]] = res["id"]
            budget_id = budget_ids["IT"]

            # 5. Seed Subscriptions
            print("Seeding subscriptions...")
            today = date.today()
            subscriptions = [
                ("Microsoft 365 Business", "Microsoft", "Productivity", today - timedelta(days=180), today + timedelta(days=180), "annual", 24000.00, "AED", "active", "Finance & Accounts"),
                ("Slack Enterprise Grid", "Slack", "Collaboration", today - timedelta(days=90), today + timedelta(days=275), "annual", 12000.00, "AED", "active", "Software Engineering"),
                ("Zoom Pro Suite", "Zoom", "Collaboration", today - timedelta(days=30), today + timedelta(days=5), "monthly", 800.00, "AED", "active", "Human Resources"),
                ("Adobe Creative Cloud", "Adobe", "Design", today - timedelta(days=15), today + timedelta(days=15), "monthly", 300.00, "AED", "active", "Software Engineering"),
            ]
            sub_ids = {}
            for name, v_name, cat, start, renewal, cycle, amount, currency, status, dept in subscriptions:
                cur.execute(
                    """
                    INSERT INTO slmct.subscriptions (organisation_id, vendor_id, name, category, owner_person_id, start_date, renewal_date, billing_cycle, amount, currency_code, status, department)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::slmct.subscription_status, %s)
                    RETURNING id
                    """,
                    (org_id, vendor_ids[v_name], name, cat, admin_person_id, start, renewal, cycle, amount, currency, status, dept)
                )
                sub_ids[name] = cur.fetchone()["id"]

            # 6. Seed Payments (designed to trigger a budget utilisation warning on IT department)
            print("Seeding payments...")
            payments = [
                (sub_ids["Microsoft 365 Business"], vendor_ids["Microsoft"], budget_id, today - timedelta(days=170), 24000.00, "AED", "paid", "MS-365-PMT"),
                (sub_ids["Slack Enterprise Grid"], vendor_ids["Slack"], budget_id, today - timedelta(days=80), 12000.00, "AED", "paid", "SLK-GRID-PMT"),
                (sub_ids["Zoom Pro Suite"], vendor_ids["Zoom"], budget_id, today - timedelta(days=20), 800.00, "AED", "paid", "ZOM-PRO-PMT1"),
                (sub_ids["Zoom Pro Suite"], vendor_ids["Zoom"], budget_id, today + timedelta(days=5), 800.00, "AED", "planned", "ZOM-PRO-PMT2"),
            ]
            for sub_id, v_id, b_id, p_date, amount, currency, status, ref in payments:
                cur.execute(
                    """
                    INSERT INTO slmct.payments (organisation_id, subscription_id, vendor_id, budget_id, payment_date, amount, currency_code, status, reference)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::slmct.payment_status, %s)
                    """,
                    (org_id, sub_id, v_id, b_id, p_date, amount, currency, status, ref)
                )

            # 7. Seed Licences (designed to trigger different optimization cards)
            print("Seeding licenses...")
            licences = [
                # Slack licenses
                (sub_ids["Slack Enterprise Grid"], employee_ids["Jane Smith"], "Slack Enterprise Licence", "SLK-SEAT-01", today - timedelta(days=80), today + timedelta(days=275), "assigned"),
                (sub_ids["Slack Enterprise Grid"], None, "Slack Enterprise Licence", "SLK-SEAT-02", None, None, "available"), # Idle Seat!
                (sub_ids["Slack Enterprise Grid"], employee_ids["John Doe"], "Slack Enterprise Licence", "SLK-SEAT-03", today - timedelta(days=80), today + timedelta(days=275), "assigned"), # Inactive employee (John Doe left_org)!
                (sub_ids["Slack Enterprise Grid"], employee_ids["Alice Webb"], "Slack Enterprise Licence", "SLK-SEAT-04", today - timedelta(days=80), today + timedelta(days=275), "assigned"), # Inactive employee (Alice Webb inactive)!
                
                # Zoom licenses
                (sub_ids["Zoom Pro Suite"], employee_ids["Bob Miller"], "Zoom Pro Licence", "ZOM-SEAT-01", today - timedelta(days=20), today + timedelta(days=5), "assigned"), # Expiring soon!
                (sub_ids["Zoom Pro Suite"], None, "Zoom Pro Licence", "ZOM-SEAT-02", None, None, "available"), # Idle Seat!
            ]
            for sub_id, person_id, name, ref, assigned, expires, status in licences:
                cur.execute(
                    """
                    INSERT INTO slmct.licences (organisation_id, subscription_id, assigned_to_person_id, licence_name, seat_reference, assigned_at, expires_at, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::slmct.licence_status)
                    """,
                    (org_id, sub_id, person_id, name, ref, assigned, expires, status)
                )

            # 8. Seed Contracts
            print("Seeding contracts...")
            contracts = [
                (vendor_ids["Slack"], sub_ids["Slack Enterprise Grid"], "Slack Enterprise Master Agreement", "SLK-CTR-2026", "software_license", today - timedelta(days=90), today + timedelta(days=275), 12000.00, "AED", True, 30, "Jane Smith", "active", "slack_contract.pdf", "standard agreement"),
                (vendor_ids["Microsoft"], sub_ids["Microsoft 365 Business"], "Microsoft Enterprise Subscription Contract", "MSFT-EA-993", "enterprise_agreement", today - timedelta(days=180), today + timedelta(days=180), 24000.00, "AED", False, 60, "Siddhaarth Admin", "active", "m365_ea_contract.pdf", "renew manual"),
                (vendor_ids["Zoom"], sub_ids["Zoom Pro Suite"], "Zoom Pro Services Agreement", "ZOM-SA-12", "service_contract", today - timedelta(days=30), today + timedelta(days=5), 9600.00, "AED", True, 15, "Bob Miller", "draft", "zoom_agreement.pdf", "pending review"),
            ]
            for v_id, sub_id, title, c_num, c_type, start, end, val, curr, renew, notice, owner, status, doc, notes in contracts:
                cur.execute(
                    """
                    INSERT INTO slmct.contracts (organisation_id, vendor_id, subscription_id, title, contract_number, contract_type, start_date, end_date, value, currency_code, auto_renew, notice_period_days, owner, status, document_name, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (org_id, v_id, sub_id, title, c_num, c_type, start, end, val, curr, renew, notice, owner, status, doc, notes)
                )

            conn.commit()
            print("Demo data seeded successfully!")

if __name__ == "__main__":
    main()
