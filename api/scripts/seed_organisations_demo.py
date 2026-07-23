"""Seed distinct demo data for each Derisk360 organisation (idempotent)."""

from __future__ import annotations

from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from app.settings import get_settings

SEED_MARKER = "org-demo-seed-v1"
TODAY = date.today()
FISCAL_YEAR = TODAY.year


def _get_org_ids(cur) -> dict[str, str]:
    cur.execute(
        """
        SELECT code, id
        FROM slmct.organisations
        WHERE code IN (
          'derisk360_group',
          'derisk360_admin_services',
          'derisk360_it_services',
          'derisk360_operations'
        )
          AND is_active = true
        """
    )
    rows = cur.fetchall()
    org_ids = {row["code"]: str(row["id"]) for row in rows}
    missing = {
        code
        for code in (
            "derisk360_group",
            "derisk360_admin_services",
            "derisk360_it_services",
            "derisk360_operations",
        )
        if code not in org_ids
    }
    if missing:
        raise RuntimeError(f"Missing active organisations: {', '.join(sorted(missing))}")
    return org_ids


def _ensure_vendor(cur, org_id: str, name: str, legal_name: str, website: str, email: str) -> str:
    cur.execute(
        """
        SELECT id FROM slmct.vendors
        WHERE organisation_id = %s AND lower(name) = lower(%s)
        LIMIT 1
        """,
        (org_id, name),
    )
    row = cur.fetchone()
    if row:
        return str(row["id"])
    cur.execute(
        """
        INSERT INTO slmct.vendors (
          organisation_id, name, legal_name, website_url, contact_email, status
        )
        VALUES (%s, %s, %s, %s, %s, 'active')
        RETURNING id
        """,
        (org_id, name, legal_name, website, email),
    )
    return str(cur.fetchone()["id"])


def _ensure_employee(
    cur,
    org_id: str,
    employee_number: str,
    full_name: str,
    email: str,
    department: str,
    job_title: str,
) -> str:
    cur.execute(
        """
        SELECT id FROM slmct.people
        WHERE organisation_id = %s AND employee_number = %s
        LIMIT 1
        """,
        (org_id, employee_number),
    )
    row = cur.fetchone()
    if row:
        return str(row["id"])
    cur.execute(
        """
        INSERT INTO slmct.people (
          organisation_id, employee_number, full_name, work_email, department, job_title, status
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'active'::slmct.person_status)
        RETURNING id
        """,
        (org_id, employee_number, full_name, email, department, job_title),
    )
    return str(cur.fetchone()["id"])


def _ensure_budget(cur, org_id: str, department: str, amount: float, currency: str = "AED") -> str:
    cur.execute(
        """
        SELECT id FROM slmct.budgets
        WHERE organisation_id = %s
          AND fiscal_year = %s
          AND department = %s
        LIMIT 1
        """,
        (org_id, FISCAL_YEAR, department),
    )
    row = cur.fetchone()
    if row:
        return str(row["id"])
    cur.execute(
        """
        INSERT INTO slmct.budgets (
          organisation_id, fiscal_year, department, allocated_amount, currency_code, status, notes
        )
        VALUES (%s, %s, %s, %s, %s, 'approved', %s)
        RETURNING id
        """,
        (org_id, FISCAL_YEAR, department, amount, currency, SEED_MARKER),
    )
    return str(cur.fetchone()["id"])


def _ensure_subscription(
    cur,
    org_id: str,
    name: str,
    vendor_id: str,
    category: str,
    department: str,
    owner_id: str | None,
    start: date,
    renewal: date,
    cycle: str,
    amount: float,
    currency: str = "AED",
) -> str:
    cur.execute(
        """
        SELECT id FROM slmct.subscriptions
        WHERE organisation_id = %s AND name = %s
        LIMIT 1
        """,
        (org_id, name),
    )
    row = cur.fetchone()
    if row:
        return str(row["id"])
    cur.execute(
        """
        INSERT INTO slmct.subscriptions (
          organisation_id, vendor_id, name, category, owner_person_id,
          start_date, renewal_date, billing_cycle, amount, currency_code, status, department, notes
        )
        VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active'::slmct.subscription_status, %s, %s
        )
        RETURNING id
        """,
        (
            org_id,
            vendor_id,
            name,
            category,
            owner_id,
            start,
            renewal,
            cycle,
            amount,
            currency,
            department,
            SEED_MARKER,
        ),
    )
    return str(cur.fetchone()["id"])


def _ensure_payment(
    cur,
    org_id: str,
    subscription_id: str,
    vendor_id: str,
    budget_id: str,
    reference: str,
    amount: float,
    payment_date: date,
    currency: str = "AED",
) -> None:
    cur.execute(
        """
        SELECT id FROM slmct.payments
        WHERE organisation_id = %s AND reference = %s
        LIMIT 1
        """,
        (org_id, reference),
    )
    if cur.fetchone():
        return
    cur.execute(
        """
        INSERT INTO slmct.payments (
          organisation_id, subscription_id, vendor_id, budget_id,
          payment_date, amount, currency_code, status, reference, notes
        )
        VALUES (
          %s, %s, %s, %s, %s, %s, %s, 'paid'::slmct.payment_status, %s, %s
        )
        """,
        (org_id, subscription_id, vendor_id, budget_id, payment_date, amount, currency, reference, SEED_MARKER),
    )


def _ensure_licence(
    cur,
    org_id: str,
    subscription_id: str,
    licence_name: str,
    seat_reference: str,
    person_id: str | None,
    assigned_at: date | None,
    expires_at: date | None,
    status: str,
) -> None:
    cur.execute(
        """
        SELECT id FROM slmct.licences
        WHERE organisation_id = %s AND seat_reference = %s
        LIMIT 1
        """,
        (org_id, seat_reference),
    )
    if cur.fetchone():
        return
    cur.execute(
        """
        INSERT INTO slmct.licences (
          organisation_id, subscription_id, assigned_to_person_id,
          licence_name, seat_reference, assigned_at, expires_at, status, notes
        )
        VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s::slmct.licence_status, %s
        )
        """,
        (
            org_id,
            subscription_id,
            person_id,
            licence_name,
            seat_reference,
            assigned_at,
            expires_at,
            status,
            SEED_MARKER,
        ),
    )


def _ensure_workflow(cur, org_id: str, module: str, action: str, payload: dict, notes: str) -> None:
    cur.execute(
        """
        SELECT id FROM slmct.workflow_requests
        WHERE organisation_id = %s
          AND requested_module = %s
          AND notes = %s
        LIMIT 1
        """,
        (org_id, module, notes),
    )
    if cur.fetchone():
        return
    cur.execute(
        """
        INSERT INTO slmct.workflow_requests (
          organisation_id, workflow_type, requested_module, requested_action, payload,
          requested_by_email, status, notes
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'submitted', %s)
        """,
        (
            org_id,
            "it_admin",
            module,
            action,
            Json(payload),
            "deriskmaster@outlook.com",
            notes,
        ),
    )


def seed_group(cur, org_id: str) -> None:
    print("  Seeding Derisk360 Group (parent / global rollup)...")
    owner = _ensure_employee(
        cur,
        org_id,
        "GRP-001",
        "Nadia Al Mansoori",
        "nadia.almansoori@derisk360.com",
        "Executive",
        "Group CFO",
    )
    _ensure_employee(cur, org_id, "GRP-002", "Omar Hassan", "omar.hassan@derisk360.com", "Finance & Accounts", "Finance Director")
    _ensure_employee(cur, org_id, "GRP-003", "Layla Farouk", "layla.farouk@derisk360.com", "Human Resources", "Group HR Lead")

    ms = _ensure_vendor(cur, org_id, "Microsoft", "Microsoft Corporation", "https://microsoft.com", "licensing@microsoft.com")
    sf = _ensure_vendor(cur, org_id, "Salesforce", "Salesforce, Inc.", "https://salesforce.com", "billing@salesforce.com")

    b_exec = _ensure_budget(cur, org_id, "Executive", 520000)
    b_fin = _ensure_budget(cur, org_id, "Finance & Accounts", 380000)
    b_hr = _ensure_budget(cur, org_id, "Human Resources", 210000)

    sub_m365 = _ensure_subscription(
        cur,
        org_id,
        "Microsoft 365 E5 (Group)",
        ms,
        "Productivity",
        "Executive",
        owner,
        TODAY - timedelta(days=200),
        TODAY + timedelta(days=165),
        "annual",
        96000,
    )
    sub_sf = _ensure_subscription(
        cur,
        org_id,
        "Salesforce Enterprise (Group CRM)",
        sf,
        "CRM",
        "Finance & Accounts",
        owner,
        TODAY - timedelta(days=120),
        TODAY + timedelta(days=245),
        "annual",
        145000,
    )

    _ensure_payment(cur, org_id, sub_m365, ms, b_exec, "GRP-M365-PMT", 96000, TODAY - timedelta(days=190))
    _ensure_payment(cur, org_id, sub_sf, sf, b_fin, "GRP-SF-PMT", 145000, TODAY - timedelta(days=110))

    _ensure_licence(cur, org_id, sub_m365, "M365 E5 Seat", "GRP-M365-01", owner, TODAY - timedelta(days=200), TODAY + timedelta(days=165), "assigned")
    _ensure_licence(cur, org_id, sub_sf, "Salesforce Enterprise Seat", "GRP-SF-01", owner, TODAY - timedelta(days=120), TODAY + timedelta(days=245), "assigned")

    _ensure_workflow(
        cur,
        org_id,
        "budgets",
        "create",
        {"department": "Executive", "allocated_amount": 520000, "fiscal_year": FISCAL_YEAR, "organisation_id": org_id},
        f"{SEED_MARKER}:group-budget-review",
    )


def seed_admin_services(cur, org_id: str) -> None:
    print("  Seeding Derisk360 Admin Services...")
    fin = _ensure_employee(cur, org_id, "AS-FIN-001", "Priya Nair", "priya.nair@admin.derisk360.com", "Finance & Accounts", "Finance Manager")
    hr = _ensure_employee(cur, org_id, "AS-HR-001", "James Okonkwo", "james.okonkwo@admin.derisk360.com", "Human Resources", "HR Business Partner")
    legal = _ensure_employee(cur, org_id, "AS-LEG-001", "Sara Al Ketbi", "sara.alketbi@admin.derisk360.com", "Legal", "Legal Counsel")

    ms = _ensure_vendor(cur, org_id, "Microsoft", "Microsoft Corporation", "https://microsoft.com", "licensing@microsoft.com")
    docusign = _ensure_vendor(cur, org_id, "DocuSign", "DocuSign, Inc.", "https://docusign.com", "billing@docusign.com")
    quickbooks = _ensure_vendor(cur, org_id, "Intuit QuickBooks", "Intuit Inc.", "https://quickbooks.intuit.com", "billing@intuit.com")

    b_fin = _ensure_budget(cur, org_id, "Finance & Accounts", 125000)
    b_hr = _ensure_budget(cur, org_id, "Human Resources", 88000)
    b_legal = _ensure_budget(cur, org_id, "Legal", 72000)

    sub_m365 = _ensure_subscription(
        cur, org_id, "Microsoft 365 Business", ms, "Productivity", "Finance & Accounts", fin,
        TODAY - timedelta(days=150), TODAY + timedelta(days=215), "annual", 18000,
    )
    sub_ds = _ensure_subscription(
        cur, org_id, "DocuSign Business Pro", docusign, "Legal Tech", "Legal", legal,
        TODAY - timedelta(days=60), TODAY + timedelta(days=305), "annual", 9600,
    )
    sub_qb = _ensure_subscription(
        cur, org_id, "QuickBooks Online Advanced", quickbooks, "Accounting", "Finance & Accounts", fin,
        TODAY - timedelta(days=30), TODAY + timedelta(days=335), "annual", 7200,
    )

    _ensure_payment(cur, org_id, sub_m365, ms, b_fin, "AS-M365-PMT", 18000, TODAY - timedelta(days=140))
    _ensure_payment(cur, org_id, sub_ds, docusign, b_legal, "AS-DS-PMT", 9600, TODAY - timedelta(days=55))
    _ensure_payment(cur, org_id, sub_qb, quickbooks, b_fin, "AS-QB-PMT", 7200, TODAY - timedelta(days=25))

    _ensure_licence(cur, org_id, sub_m365, "M365 Business Seat", "AS-M365-01", fin, TODAY - timedelta(days=150), TODAY + timedelta(days=215), "assigned")
    _ensure_licence(cur, org_id, sub_ds, "DocuSign Seat", "AS-DS-01", legal, TODAY - timedelta(days=60), TODAY + timedelta(days=305), "assigned")
    _ensure_licence(cur, org_id, sub_qb, "QuickBooks Seat", "AS-QB-01", hr, TODAY - timedelta(days=30), TODAY + timedelta(days=335), "assigned")

    _ensure_workflow(
        cur,
        org_id,
        "subscriptions",
        "create",
        {"name": "DocuSign Business Pro", "department": "Legal", "organisation_id": org_id, "amount": 9600},
        f"{SEED_MARKER}:admin-subscription-request",
    )


def seed_it_services(cur, org_id: str) -> None:
    print("  Seeding Derisk360 IT Services...")
    dev = _ensure_employee(cur, org_id, "IT-DEV-001", "Arjun Mehta", "arjun.mehta@it.derisk360.com", "Software Engineering", "Lead Engineer")
    admin = _ensure_employee(cur, org_id, "IT-ADM-001", "Fatima Rahman", "fatima.rahman@it.derisk360.com", "IT", "IT Operations Manager")
    sec = _ensure_employee(cur, org_id, "IT-SEC-001", "Daniel Brooks", "daniel.brooks@it.derisk360.com", "Security", "Security Analyst")

    github = _ensure_vendor(cur, org_id, "GitHub", "GitHub, Inc.", "https://github.com", "enterprise@github.com")
    atlassian = _ensure_vendor(cur, org_id, "Atlassian", "Atlassian Pty Ltd", "https://atlassian.com", "billing@atlassian.com")
    sentry = _ensure_vendor(cur, org_id, "Sentry", "Functional Software, Inc.", "https://sentry.io", "billing@sentry.io")
    ms = _ensure_vendor(cur, org_id, "Microsoft", "Microsoft Corporation", "https://microsoft.com", "licensing@microsoft.com")

    b_eng = _ensure_budget(cur, org_id, "Software Engineering", 460000)
    b_it = _ensure_budget(cur, org_id, "IT", 295000)
    b_sec = _ensure_budget(cur, org_id, "Security", 185000)

    sub_gh = _ensure_subscription(
        cur, org_id, "GitHub Enterprise Cloud", github, "Developer Tools", "Software Engineering", dev,
        TODAY - timedelta(days=180), TODAY + timedelta(days=185), "annual", 78000,
    )
    sub_jira = _ensure_subscription(
        cur, org_id, "Jira Software Premium", atlassian, "Project Management", "Software Engineering", dev,
        TODAY - timedelta(days=90), TODAY + timedelta(days=275), "annual", 42000,
    )
    sub_teams = _ensure_subscription(
        cur, org_id, "Microsoft Teams Enterprise", ms, "Collaboration", "IT", admin,
        TODAY - timedelta(days=120), TODAY + timedelta(days=245), "annual", 36000,
    )
    sub_sentry = _ensure_subscription(
        cur, org_id, "Sentry Business", sentry, "Observability", "Security", sec,
        TODAY - timedelta(days=45), TODAY + timedelta(days=320), "annual", 24000,
    )

    _ensure_payment(cur, org_id, sub_gh, github, b_eng, "IT-GH-PMT", 78000, TODAY - timedelta(days=170))
    _ensure_payment(cur, org_id, sub_jira, atlassian, b_eng, "IT-JIRA-PMT", 42000, TODAY - timedelta(days=85))
    _ensure_payment(cur, org_id, sub_teams, ms, b_it, "IT-TEAMS-PMT", 36000, TODAY - timedelta(days=115))
    _ensure_payment(cur, org_id, sub_sentry, sentry, b_sec, "IT-SENTRY-PMT", 24000, TODAY - timedelta(days=40))

    _ensure_licence(cur, org_id, sub_gh, "GitHub Enterprise Seat", "IT-GH-01", dev, TODAY - timedelta(days=180), TODAY + timedelta(days=185), "assigned")
    _ensure_licence(cur, org_id, sub_jira, "Jira Premium Seat", "IT-JIRA-01", admin, TODAY - timedelta(days=90), TODAY + timedelta(days=275), "assigned")
    _ensure_licence(cur, org_id, sub_teams, "Teams Enterprise Seat", "IT-TEAMS-01", admin, TODAY - timedelta(days=120), TODAY + timedelta(days=245), "assigned")
    _ensure_licence(cur, org_id, sub_sentry, "Sentry Business Seat", "IT-SENTRY-01", sec, TODAY - timedelta(days=45), TODAY + timedelta(days=320), "assigned")

    _ensure_workflow(
        cur,
        org_id,
        "licences",
        "create",
        {"licence_name": "GitHub Enterprise Seat", "organisation_id": org_id, "department": "Software Engineering"},
        f"{SEED_MARKER}:it-licence-request",
    )


def seed_operations(cur, org_id: str) -> None:
    print("  Seeding Derisk360 Operations...")
    ops = _ensure_employee(cur, org_id, "OPS-001", "Maria Santos", "maria.santos@ops.derisk360.com", "Operations", "Operations Lead")
    sales = _ensure_employee(cur, org_id, "SAL-001", "Tom Bradley", "tom.bradley@ops.derisk360.com", "Sales", "Account Executive")
    support = _ensure_employee(cur, org_id, "SUP-001", "Aisha Khan", "aisha.khan@ops.derisk360.com", "Support", "Support Manager")

    slack = _ensure_vendor(cur, org_id, "Slack", "Slack Technologies, Inc.", "https://slack.com", "billing@slack.com")
    zoom = _ensure_vendor(cur, org_id, "Zoom", "Zoom Video Communications", "https://zoom.us", "finance@zoom.us")
    miro = _ensure_vendor(cur, org_id, "Miro", "RealtimeBoard, Inc.", "https://miro.com", "billing@miro.com")
    freshdesk = _ensure_vendor(cur, org_id, "Freshdesk", "Freshworks Inc.", "https://freshdesk.com", "billing@freshworks.com")

    b_ops = _ensure_budget(cur, org_id, "Operations", 98000)
    b_sales = _ensure_budget(cur, org_id, "Sales", 112000)
    b_support = _ensure_budget(cur, org_id, "Support", 86000)

    sub_slack = _ensure_subscription(
        cur, org_id, "Slack Business+", slack, "Collaboration", "Operations", ops,
        TODAY - timedelta(days=100), TODAY + timedelta(days=265), "annual", 15600,
    )
    sub_zoom = _ensure_subscription(
        cur, org_id, "Zoom Workplace", zoom, "Collaboration", "Sales", sales,
        TODAY - timedelta(days=40), TODAY + timedelta(days=325), "annual", 8400,
    )
    sub_miro = _ensure_subscription(
        cur, org_id, "Miro Team Plan", miro, "Whiteboarding", "Operations", ops,
        TODAY - timedelta(days=70), TODAY + timedelta(days=295), "annual", 5400,
    )
    sub_fd = _ensure_subscription(
        cur, org_id, "Freshdesk Omnichannel", freshdesk, "Support", "Support", support,
        TODAY - timedelta(days=20), TODAY + timedelta(days=345), "annual", 12000,
    )

    _ensure_payment(cur, org_id, sub_slack, slack, b_ops, "OPS-SLACK-PMT", 15600, TODAY - timedelta(days=95))
    _ensure_payment(cur, org_id, sub_zoom, zoom, b_sales, "OPS-ZOOM-PMT", 8400, TODAY - timedelta(days=35))
    _ensure_payment(cur, org_id, sub_miro, miro, b_ops, "OPS-MIRO-PMT", 5400, TODAY - timedelta(days=65))
    _ensure_payment(cur, org_id, sub_fd, freshdesk, b_support, "OPS-FD-PMT", 12000, TODAY - timedelta(days=15))

    _ensure_licence(cur, org_id, sub_slack, "Slack Business+ Seat", "OPS-SLK-01", ops, TODAY - timedelta(days=100), TODAY + timedelta(days=265), "assigned")
    _ensure_licence(cur, org_id, sub_zoom, "Zoom Workplace Seat", "OPS-ZOOM-01", sales, TODAY - timedelta(days=40), TODAY + timedelta(days=325), "assigned")
    _ensure_licence(cur, org_id, sub_miro, "Miro Team Seat", "OPS-MIRO-01", ops, TODAY - timedelta(days=70), TODAY + timedelta(days=295), "assigned")
    _ensure_licence(cur, org_id, sub_fd, "Freshdesk Agent Seat", "OPS-FD-01", support, TODAY - timedelta(days=20), TODAY + timedelta(days=345), "assigned")

    _ensure_workflow(
        cur,
        org_id,
        "payments",
        "create",
        {"reference": "OPS-FD-PMT", "amount": 12000, "organisation_id": org_id, "department": "Support"},
        f"{SEED_MARKER}:ops-payment-request",
    )


def main() -> None:
    settings = get_settings()
    print("Seeding organisation-scoped demo data...")
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            org_ids = _get_org_ids(cur)
            seed_group(cur, org_ids["derisk360_group"])
            seed_admin_services(cur, org_ids["derisk360_admin_services"])
            seed_it_services(cur, org_ids["derisk360_it_services"])
            seed_operations(cur, org_ids["derisk360_operations"])
        conn.commit()
    print("Organisation demo data seed complete.")


if __name__ == "__main__":
    main()
