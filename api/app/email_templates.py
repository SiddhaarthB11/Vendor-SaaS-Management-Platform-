import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from psycopg import Connection


@dataclass(frozen=True)
class OutboundEmail:
    to_emails: list[str]
    subject: str
    body: str
    event_type: str
    cc_emails: tuple[str, ...] = ()


def _get_emails_by_role(conn: Connection, role_code: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT lower(au.email::text) AS email
            FROM slmct.app_users au
            JOIN slmct.user_roles ur ON ur.user_id = au.id AND ur.revoked_at IS NULL
            JOIN slmct.roles r ON r.id = ur.role_id
            WHERE r.code = %s
              AND au.status = 'active'
              AND r.can_login = true
            ORDER BY 1
            """,
            (role_code,),
        )
        rows = cur.fetchall()
        emails: list[str] = []
        for row in rows:
            value = row.get("email") if isinstance(row, dict) else row[0]
            if value:
                emails.append(str(value))
        return emails


def _get_user_email_by_id(conn: Connection, user_id: UUID | str | None) -> str | None:
    if not user_id:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT email
            FROM slmct.app_users
            WHERE id = %s AND status = 'active'
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return str(row.get("email") if isinstance(row, dict) else row[0])


def _resolve_app_user_email(conn: Connection, email: str | None) -> str | None:
    candidate = str(email or "").strip()
    if not candidate:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT email
            FROM slmct.app_users
            WHERE lower(email::text) = lower(%s)
              AND status = 'active'
            LIMIT 1
            """,
            (candidate,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return str(row.get("email") if isinstance(row, dict) else row[0])


def _get_user_full_name(conn: Connection, email: str | None) -> str | None:
    """Return a person's full name given their email, or None if not found."""
    candidate = str(email or "").strip()
    if not candidate:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.full_name
            FROM slmct.app_users au
            JOIN slmct.people p ON p.id = au.person_id
            WHERE lower(au.email::text) = lower(%s) AND au.status = 'active'
            LIMIT 1
            """,
            (candidate,),
        )
        row = cur.fetchone()
        if row:
            val = row.get("full_name") if isinstance(row, dict) else row[0]
            if val:
                return str(val)
    return None


import re as _re
_UUID_RE = _re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", _re.I)


def _resolve_actor_email(conn: Connection, actor_id_or_email: str | None) -> str | None:
    """Accept either a UUID (user id) or email string; return the actor's email."""
    candidate = str(actor_id_or_email or "").strip()
    if not candidate:
        return None
    if _UUID_RE.match(candidate):
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM slmct.app_users WHERE id = %s LIMIT 1", (candidate,))
            row = cur.fetchone()
            if row:
                return str(row.get("email") if isinstance(row, dict) else row[0])
        return None
    return candidate


def _resolve_workflow_requester(request_row: dict, conn: Connection) -> str:
    by_id = _get_user_email_by_id(conn, request_row.get("requested_by"))
    if by_id:
        return by_id

    by_email = _resolve_app_user_email(conn, request_row.get("requested_by_email"))
    if by_email:
        return by_email

    raise ValueError(
        "Workflow requester is not linked to an active app user. "
        "Emails are only sent between users configured in the Users page."
    )


def _resolve_tool_request_requester(tool_request: dict, conn: Connection) -> str | None:
    requester_email = str(tool_request.get("requester_email") or "").strip()
    app_user_email = _resolve_app_user_email(conn, requester_email)
    if app_user_email:
        return app_user_email

    requester_name = str(tool_request.get("requester_name") or "").strip()
    if requester_name:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT au.email
                FROM slmct.people p
                JOIN slmct.app_users au ON au.person_id = p.id AND au.status = 'active'
                WHERE lower(p.full_name) = lower(%s)
                LIMIT 1
                """,
                (requester_name,),
            )
            row = cur.fetchone()
            if row:
                return str(row.get("email") if isinstance(row, dict) else row[0])

    return requester_email or None


def _unique_emails(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for group in groups:
        for email in group:
            value = str(email or "").strip()
            key = value.lower()
            if value and key not in seen:
                seen.add(key)
                ordered.append(value)
    return ordered


def _role_emails(conn: Connection, role_code: str) -> list[str]:
    return _get_emails_by_role(conn, role_code)


# ── Rich email body builder ────────────────────────────────────────────────────

_WORKFLOW_TYPE_LABELS: dict[str, str] = {
    "employee_software_request": "Employee Software Request",
    "new_subscription_request": "New Subscription Request",
    "license_assignment_request": "License Assignment Request",
    "renewal_request": "Renewal Request",
    "generic_procurement": "Procurement Workflow",
}


def _wf_type_label(wf_type: str | None) -> str:
    return _WORKFLOW_TYPE_LABELS.get(str(wf_type or ""), "Procurement Workflow")


def _wf_software_name(request_row: dict) -> str:
    payload = request_row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    for key in ("name", "tool_requested", "licence_name", "title"):
        val = payload.get(key)
        if val and str(val).strip():
            return str(val).strip()
    return "Software Request"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")


def _fmt_email_name(name: str | None, email: str | None) -> str:
    if name and email:
        return f"{name} <{email}>"
    return name or email or "—"


def _generate_ai_email_message(
    event: str,
    requester_name: str,
    software: str,
    actor_name: str | None,
    next_step: str | None,
    rejection_reason: str | None = None,
) -> str:
    """Generate a short personalized message for the email using Gemini. Returns empty string on any failure."""
    try:
        from app.settings import get_settings
        settings = get_settings()
        if not settings.gemini_api_key:
            return ""

        event_context = {
            "submitted": f"A new software request has been submitted by {requester_name} for {software}. The request is now awaiting approval.",
            "line_manager_approved": f"{actor_name or 'The line manager'} has approved {requester_name}'s request for {software}. It is now moving to the next approval stage.",
            "master_approved": f"{actor_name or 'The master administrator'} has approved {requester_name}'s request for {software}. It is now with Finance for budget validation.",
            "finance_approved": f"Finance has validated the budget for {requester_name}'s request for {software}. It is now with IT for procurement.",
            "rejected": f"{requester_name}'s request for {software} has been rejected. Reason: {rejection_reason or 'Not specified'}.",
            "completed": f"{requester_name}'s request for {software} has been completed and the subscription is now active.",
            "info_requested": f"Additional information has been requested from {requester_name} regarding their {software} request.",
        }.get(event, f"An update has occurred on {requester_name}'s request for {software}.")

        next_str = f" Next step: {next_step}." if next_step else ""

        from app.llm import complete
        from app.prompts import format_prompt

        prompt = format_prompt(
            "email_message",
            event_context=event_context,
            next_str=next_str,
            requester_first_name=requester_name.split()[0] if requester_name else "there",
        )
        result = complete(
            "email_message",
            contents=prompt,
            temperature=0.4,
            max_output_tokens=300,
            thinking_budget=0,
        )
        return (result.text or "").strip()
    except Exception:
        return ""


def _build_rich_body(
    request_row: dict,
    conn: Connection,
    event_label: str,
    action_detail: str,
    action_by_email: str | None = None,
    next_owner: str | None = None,
    next_owner_email: str | None = None,
    next_action: str | None = None,
    extra_sections: list[tuple[str, list[tuple[str, str]]]] | None = None,
    ai_event: str | None = None,
) -> str:
    SEP = "─" * 60

    wf_id = str(request_row.get("id") or "")[:8].upper()
    requester_email = str(request_row.get("requested_by_email") or "").strip()
    requester_name = _get_user_full_name(conn, requester_email) or requester_email or "Unknown"
    wf_type = _wf_type_label(str(request_row.get("workflow_type") or ""))
    software = _wf_software_name(request_row)
    status = str(request_row.get("status") or "").replace("_", " ").title()
    created_at = request_row.get("created_at")
    submitted_str = str(created_at)[:16].replace("T", " ") if created_at else "—"
    resolved_actor_email = _resolve_actor_email(conn, action_by_email)
    action_by_name = _get_user_full_name(conn, resolved_actor_email)
    timestamp = _now_utc()

    next_owner_full = _get_user_full_name(conn, next_owner_email) or next_owner

    # Generate AI personal message if an event type is provided
    ai_message = ""
    if ai_event:
        rejection_reason = str(request_row.get("rejection_reason") or "")
        ai_message = _generate_ai_email_message(
            event=ai_event,
            requester_name=requester_name,
            software=software,
            actor_name=_get_user_full_name(conn, resolved_actor_email),
            next_step=next_action,
            rejection_reason=rejection_reason or None,
        )

    def row(label: str, value: str) -> str:
        return f"  {label:<22}{value}"

    lines = [
        "",
        SEP,
        "  DERISK360  ·  WORKFLOW NOTIFICATION",
        SEP,
        "",
        row("Workflow ID:", f"#{wf_id}"),
        row("Request Type:", wf_type),
        row("Software:", software),
        row("Requester:", requester_name),
        row("Requester Email:", requester_email or "—"),
        row("Submitted:", submitted_str),
        row("Current Status:", status),
        "",
        *([SEP, "  MESSAGE", SEP, ""] + [f"  {line}" for line in ai_message.splitlines()] + [""] if ai_message else []),
        SEP,
        "  ACTION TAKEN",
        SEP,
        "",
        row("Event:", event_label),
        row("Detail:", action_detail),
        row("Performed by:", _fmt_email_name(action_by_name, resolved_actor_email)),
        row("Date & Time:", timestamp),
        "",
    ]

    if next_owner or next_action:
        lines += [
            SEP,
            "  NEXT STEPS",
            SEP,
            "",
        ]
        if next_owner or next_owner_email:
            lines.append(row("Next Owner:", _fmt_email_name(next_owner_full, next_owner_email)))
        if next_action:
            lines.append(row("Required Action:", next_action))
        lines.append("")

    if extra_sections:
        for section_title, section_rows in extra_sections:
            lines += [
                SEP,
                f"  {section_title.upper()}",
                SEP,
                "",
            ]
            for label, value in section_rows:
                lines.append(row(label + ":", value))
            lines.append("")

    lines += [
        SEP,
        "",
        "  This notification was sent automatically by Derisk360 SLMCT.",
        "  Please do not reply to this email.",
        "  Support: support@derisk360.com",
        "",
        SEP,
        "",
    ]

    return "\n".join(lines)


# ── Email preview helper ───────────────────────────────────────────────────────

def preview_workflow_submitted_emails(
    actor_user_id: UUID | str | None,
    actor_email: str | None,
    conn: Connection,
) -> list[dict]:
    request_row = {
        "requested_by": actor_user_id,
        "requested_by_email": actor_email,
    }
    messages = workflow_submitted_emails(request_row, conn)
    return [
        {
            "to": ", ".join(message.to_emails),
            "toEmails": message.to_emails,
            "subject": message.subject,
            "body": message.body,
            "eventType": message.event_type,
        }
        for message in messages
    ]


# ── Workflow email builders ────────────────────────────────────────────────────

def workflow_submitted_emails(request_row: dict, conn: Connection) -> list[OutboundEmail]:
    from app.workflow_governance import get_current_approver, _approver_emails_for_status
    requester = _resolve_workflow_requester(request_row, conn)
    approver = get_current_approver(request_row, conn)
    approver_role = str(approver.get("role") or "master_admin")
    approver_name = str(approver.get("name") or approver_role.replace("_", " ").title())
    approver_email = str(approver.get("email") or "")
    approver_recipients = [e for e in _approver_emails_for_status(request_row, conn) if e.lower() != (requester or "").lower()]

    software = _wf_software_name(request_row)
    wf_id = str(request_row.get("id") or "")[:8].upper()
    emails: list[OutboundEmail] = []

    # Email 1 — to the correct approver (line manager or master admin): action required
    if approver_recipients:
        approver_body = _build_rich_body(
            request_row,
            conn,
            event_label="New Request — Action Required",
            action_detail="A new software request has been submitted and is awaiting your review.",
            action_by_email=requester,
            next_owner=approver_name,
            next_owner_email=approver_email or None,
            next_action="Review and approve or reject the workflow request in Derisk360 Workflows.",
            ai_event="submitted",
        )
        emails.append(OutboundEmail(
            to_emails=approver_recipients,
            cc_emails=(tuple([requester]) if requester else ()),
            subject=f"[Derisk360] Action Required – New Request – {software} #{wf_id}",
            body=approver_body,
            event_type="workflow_created",
        ))

    # Email 2 — to requester: submission confirmed
    if requester:
        requester_body = _build_rich_body(
            request_row,
            conn,
            event_label="Request Submitted Successfully",
            action_detail="Your software request has been received and is now awaiting approval.",
            action_by_email=requester,
            next_owner=approver_name,
            next_owner_email=approver_email or None,
            next_action="Your request will be reviewed shortly. You will be notified at each stage.",
            ai_event="submitted",
        )
        emails.append(OutboundEmail(
            to_emails=[requester],
            subject=f"[Derisk360] Request Submitted – {software} #{wf_id}",
            body=requester_body,
            event_type="workflow_created_requester",
        ))

    if not emails:
        raise ValueError("No app user recipients found for workflow submission email.")
    return emails


def workflow_line_manager_approved_emails(request_row: dict, conn: Connection) -> list[OutboundEmail]:
    """Used when a line manager approves an employee_software_request (goes straight to finance)."""
    requester = _resolve_workflow_requester(request_row, conn)
    finance_emails = _role_emails(conn, "finance")
    recipients = _unique_emails([requester], finance_emails)
    if not recipients:
        raise ValueError("No app user recipients found for line manager approval email.")

    wf_id = str(request_row.get("id") or "")[:8].upper()
    approver_email = str(request_row.get("line_manager_approved_by") or "").strip() or None
    next_owner_email = finance_emails[0] if finance_emails else None
    body = _build_rich_body(
        request_row,
        conn,
        event_label="Line Manager Approved",
        action_detail="The workflow has been reviewed and approved by your Line Manager. Budget validation is now required.",
        action_by_email=approver_email,
        next_owner="Finance Manager",
        next_owner_email=next_owner_email,
        next_action="Validate the budget allocation and approve or reject in Derisk360 Workflows.",
        ai_event="line_manager_approved",
    )
    return [
        OutboundEmail(
            to_emails=recipients,
            subject=f"[Derisk360] Workflow Approved – Awaiting Budget Validation – {_wf_software_name(request_row)} #{wf_id}",
            body=body,
            event_type="line_manager_approved",
        )
    ]


def workflow_master_approved_emails(request_row: dict, conn: Connection) -> list[OutboundEmail]:
    requester = _resolve_workflow_requester(request_row, conn)
    finance_emails = _role_emails(conn, "finance")
    recipients = _unique_emails([requester], finance_emails)
    if not recipients:
        raise ValueError("No app user recipients found for master approval email.")

    wf_id = str(request_row.get("id") or "")[:8].upper()
    approver_email = str(request_row.get("master_approved_by") or "").strip() or None
    next_owner_email = finance_emails[0] if finance_emails else None
    body = _build_rich_body(
        request_row,
        conn,
        event_label="Master Admin Approved",
        action_detail="The workflow has been reviewed and approved by Master Admin. Budget validation is now required.",
        action_by_email=approver_email,
        next_owner="Finance Team",
        next_owner_email=next_owner_email,
        next_action="Validate the budget allocation and approve or reject in Derisk360 Workflows.",
        ai_event="master_approved",
    )
    return [
        OutboundEmail(
            to_emails=recipients,
            subject=f"[Derisk360] Workflow Approved – Awaiting Budget Validation – {_wf_software_name(request_row)} #{wf_id}",
            body=body,
            event_type="master_approved",
        )
    ]


def workflow_finance_approved_emails(request_row: dict, conn: Connection) -> list[OutboundEmail]:
    requester = _resolve_workflow_requester(request_row, conn)
    it_emails = _role_emails(conn, "it_admin")
    messages: list[OutboundEmail] = []

    wf_id = str(request_row.get("id") or "")[:8].upper()
    approver_email = str(request_row.get("finance_approved_by") or "").strip() or None
    next_owner_email = it_emails[0] if it_emails else None

    if requester:
        body = _build_rich_body(
            request_row,
            conn,
            event_label="Budget Validation Approved",
            action_detail="The budget has been validated by Finance. The request is now queued for IT procurement and activation.",
            action_by_email=approver_email,
            next_owner="IT Administrator",
            next_owner_email=next_owner_email,
            next_action="Procure the software, complete purchase confirmation, and activate the subscription.",
            ai_event="finance_approved",
        )
        messages.append(
            OutboundEmail(
                to_emails=[requester],
                subject=f"[Derisk360] Budget Approved – Awaiting Procurement – {_wf_software_name(request_row)} #{wf_id}",
                body=body,
                event_type="finance_approved_requester",
            )
        )

    if it_emails:
        body_it = _build_rich_body(
            request_row,
            conn,
            event_label="Procurement Required",
            action_detail="Budget has been approved by Finance. The IT team must now purchase and activate the software.",
            action_by_email=approver_email,
            next_owner="IT Administrator",
            next_owner_email=next_owner_email,
            next_action="Complete procurement, purchase confirmation, and activation in Derisk360 Workflows.",
            ai_event="finance_approved",
        )
        messages.append(
            OutboundEmail(
                to_emails=it_emails,
                subject=f"[Derisk360] Action Required – Procurement – {_wf_software_name(request_row)} #{wf_id}",
                body=body_it,
                event_type="finance_approved_it",
            )
        )

    if not messages:
        raise ValueError("No app user recipients found for finance approval email.")
    return messages


def workflow_completed_emails(request_row: dict, conn: Connection) -> list[OutboundEmail]:
    from app.settings import get_settings
    requester = _resolve_workflow_requester(request_row, conn)
    from app.workflow_governance import _line_manager_emails
    master_admins = _role_emails(conn, "master_admin")
    finance_emails = _role_emails(conn, "finance")
    it_admins = _role_emails(conn, "it_admin")
    lm_emails = _line_manager_emails(conn, request_row)
    recipients = _unique_emails([requester], master_admins, finance_emails, it_admins, lm_emails)
    if not recipients:
        raise ValueError("No app user recipients found for workflow completion email.")

    completed_by_email = str(request_row.get("completed_by") or "").strip() or None
    software = _wf_software_name(request_row)

    activation_details = request_row.get("activation_details") or {}
    if isinstance(activation_details, str):
        try:
            activation_details = json.loads(activation_details)
        except Exception:
            activation_details = {}

    assigned_name = str(request_row.get("assigned_employee_name") or "").strip() or "—"
    assigned_email = str(request_row.get("assigned_employee_email") or "").strip() or "—"
    activation_status = str(request_row.get("activation_status") or "active").title()
    completed_at = request_row.get("completed_at")
    activation_date = str(completed_at)[:10] if completed_at else _now_utc()[:10]

    payload = request_row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    renewal_date = str(payload.get("renewal_date") or activation_details.get("renewal_date") or "Not set")
    activation_method = str(request_row.get("activation_method") or activation_details.get("activation_method") or "company_account")
    support_contact = str(activation_details.get("support_contact") or "support@derisk360.com")
    cred_token = str(request_row.get("credential_change_token") or "").strip()

    # Build credential rows and employee-facing rows based on activation method
    cred_rows: list[tuple[str, str]] = []
    emp_cred_rows: list[tuple[str, str]] = []
    change_pw_note = ""

    if activation_method == "company_account":
        username = str(activation_details.get("username") or "—")
        access_password = str(activation_details.get("password") or "—")
        # Look up password change URL from vendor catalogue
        pw_change_url = None
        try:
            with conn.cursor() as _cur:
                _cur.execute(
                    "SELECT password_change_url FROM slmct.vendor_catalogue WHERE lower(name) LIKE lower(%s) AND password_change_url IS NOT NULL LIMIT 1",
                    (f"%{software[:40]}%",),
                )
                _row = _cur.fetchone()
                if _row:
                    pw_change_url = _row["password_change_url"]
        except Exception:
            pass
        pw_note_text = (
            f"This is a system-generated password. The account holder must change it immediately after first login. "
            f"Change password: {pw_change_url}"
            if pw_change_url
            else "This is a system-generated password. The account holder must change it immediately after first login on the vendor's website."
        )
        security_note = ("⚠ Security Notice", pw_note_text)
        cred_rows = [("Username", username), ("Password", access_password), security_note]
        emp_cred_rows = [("Username", username), ("Password", access_password), security_note]
        if cred_token and access_password != "—":
            base_url = get_settings().app_base_url.rstrip("/")
            change_pw_url = f"{base_url}/update-credentials?token={cred_token}"
            change_pw_note = f'\n\n<p style="margin:12px 0"><a href="{change_pw_url}" style="background:#14b8a6;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:600">Change Your Password</a></p><p style="font-size:12px;color:#888">This link is valid for 30 days. Use it to update your licence account password at any time.</p>'

    elif activation_method == "invitation_email":
        recipient_email = str(activation_details.get("recipient_email") or "—")
        invitation_status = str(activation_details.get("invitation_status") or "—")
        cred_rows = [("Invitation Sent To", recipient_email), ("Invitation Status", invitation_status)]
        emp_cred_rows = [("Invitation Sent To", recipient_email), ("Invitation Status", invitation_status)]

    elif activation_method == "license_key":
        license_key = str(activation_details.get("license_key") or "—")
        activation_code = str(activation_details.get("activation_code") or "—")
        cred_rows = [("License Key", license_key), ("Activation Code", activation_code)]
        emp_cred_rows = [("License Key", license_key), ("Activation Code", activation_code)]

    elif activation_method == "vendor_provisioned":
        vendor_ref = str(activation_details.get("vendor_reference_id") or "—")
        prov_notes = str(activation_details.get("provisioning_notes") or "—")
        cred_rows = [("Vendor Reference ID", vendor_ref), ("Provisioning Notes", prov_notes)]
        emp_cred_rows = [("Vendor Reference ID", vendor_ref), ("Provisioning Notes", prov_notes)]

    else:
        cred_rows = []
        emp_cred_rows = []

    method_label = {
        "company_account": "Company Account",
        "invitation_email": "Invitation Email",
        "license_key": "License Key",
        "vendor_provisioned": "Vendor Provisioned",
    }.get(activation_method, activation_method.replace("_", " ").title())

    access_rows: list[tuple[str, str]] = [
        ("Software", software),
        ("Assigned User", assigned_name),
        ("Assigned Email", assigned_email),
        ("Activation Method", method_label),
        ("Activation Status", activation_status),
        ("Activation Date", activation_date),
        ("Renewal Date", renewal_date),
        *cred_rows,
        ("Support Contact", support_contact),
    ]

    body = _build_rich_body(
        request_row,
        conn,
        event_label="Workflow Completed",
        action_detail="The software purchase has been confirmed and the subscription is now active. Licences are available for assignment.",
        action_by_email=completed_by_email,
        extra_sections=[("Software Access Details", access_rows)],
        ai_event="completed",
    )
    if change_pw_note:
        body = body.replace("</body>", f"{change_pw_note}</body>")

    wf_id_short = str(request_row.get("id") or "")[:8].upper()
    emails: list[OutboundEmail] = [
        OutboundEmail(
            to_emails=recipients,
            subject=f"[Derisk360] Workflow Completed – {software} is Now Active #{wf_id_short}",
            body=body,
            event_type="workflow_completed",
        )
    ]

    # Send a dedicated personal email to the assigned employee with their credentials
    if assigned_email and assigned_email != "—" and assigned_email not in recipients:
        employee_rows: list[tuple[str, str]] = [
            ("Software", software),
            ("Activation Method", method_label),
            *emp_cred_rows,
            ("Support Contact", support_contact),
        ]
        emp_body = _build_rich_body(
            request_row,
            conn,
            event_label="Your Software Access is Ready",
            action_detail=f"Your access to {software} has been activated. Use the details below to get started.",
            action_by_email=completed_by_email,
            extra_sections=[("Your Access Details", employee_rows)],
            ai_event="completed",
        )
        if change_pw_note:
            emp_body = emp_body.replace("</body>", f"{change_pw_note}</body>")
        emails.append(
            OutboundEmail(
                to_emails=[assigned_email],
                subject=f"[Derisk360] Your {software} Access is Ready",
                body=emp_body,
                event_type="workflow_completed_employee",
            )
        )

    return emails


def workflow_rejected_emails(request_row: dict, conn: Connection) -> list[OutboundEmail]:
    requester = _resolve_workflow_requester(request_row, conn)
    it_admins = _role_emails(conn, "it_admin")
    reason = str(request_row.get("rejection_reason") or "No reason provided.")
    recipients = _unique_emails([requester], it_admins)
    if not recipients:
        raise ValueError("No app user recipients found for workflow rejection email.")

    rejected_by_email = (
        str(request_row.get("finance_approved_by") or request_row.get("master_approved_by") or "").strip() or None
    )
    body = _build_rich_body(
        request_row,
        conn,
        event_label="Workflow Rejected",
        action_detail=f"The workflow request has been rejected. Reason: {reason}",
        action_by_email=rejected_by_email,
        extra_sections=[("Rejection Details", [("Reason", reason)])],
        ai_event="rejected",
    )
    return [
        OutboundEmail(
            to_emails=recipients,
            subject=f"[Derisk360] Workflow Rejected – {_wf_software_name(request_row)}",
            body=body,
            event_type="workflow_rejected",
        )
    ]


_ACTOR_EVENT_MESSAGES: dict[str, tuple[str, str]] = {
    "line_manager_approved": (
        "Line Manager Approved",
        "You successfully approved this software request as Line Manager. The workflow has moved to Finance for budget validation.",
    ),
    "master_approved": (
        "Master Admin Approved",
        "You successfully approved this workflow. The workflow has moved to Finance for budget validation.",
    ),
    "finance_approved": (
        "Budget Validation Approved",
        "You successfully validated the budget. The workflow has been passed to IT for procurement and activation.",
    ),
    "completed": (
        "Procurement Completed",
        "You successfully completed procurement and activated the subscription. The workflow has been marked as Completed.",
    ),
    "rejected": (
        "Workflow Rejected",
        "You rejected this workflow request. The requester and relevant parties have been notified.",
    ),
    "info_requested": (
        "Information Requested",
        "You requested additional information from the requester. The requester has been notified and the workflow is on hold.",
    ),
    "reopened": (
        "Workflow Reopened",
        "You reopened this rejected workflow. It has been returned to the submission queue for re-approval.",
    ),
}


def workflow_actor_confirmation_emails(
    request_row: dict,
    conn: Connection,
    event: str,
    actor_email: str,
) -> list[OutboundEmail]:
    """Send a confirmation to the approver who just performed an action."""
    if not actor_email:
        return []

    event_label, outcome_description = _ACTOR_EVENT_MESSAGES.get(
        event, ("Action Recorded", "Your action has been recorded in the workflow.")
    )

    from app.workflow_governance import get_current_approver

    approver = get_current_approver(request_row, conn)
    next_owner = approver.get("name") or None
    next_owner_email = approver.get("email") or None
    next_stage = approver.get("stage") or str(request_row.get("status", "")).replace("_", " ").title()

    wf_id = str(request_row.get("id") or "")[:8].upper()
    software = _wf_software_name(request_row)
    requester_email = str(request_row.get("requested_by_email") or "").strip()
    requester_name = _get_user_full_name(conn, requester_email) or requester_email or "Unknown"
    actor_name = _get_user_full_name(conn, actor_email) or actor_email

    SEP = "─" * 60

    def row(label: str, value: str) -> str:
        return f"  {label:<24}{value}"

    lines = [
        "",
        SEP,
        "  DERISK360  ·  APPROVAL CONFIRMATION",
        SEP,
        "",
        f"  {outcome_description}",
        "",
        SEP,
        "  WORKFLOW DETAILS",
        SEP,
        "",
        row("Workflow ID:", f"#{wf_id}"),
        row("Request Type:", _wf_type_label(str(request_row.get("workflow_type") or ""))),
        row("Software:", software),
        row("Requester:", requester_name),
        row("Requester Email:", requester_email or "—"),
        "",
        SEP,
        "  YOUR ACTION",
        SEP,
        "",
        row("Action Taken:", event_label),
        row("Performed by:", _fmt_email_name(actor_name, actor_email)),
        row("Date & Time:", _now_utc()),
        row("Current Status:", str(request_row.get("status", "")).replace("_", " ").title()),
        "",
    ]

    if event not in {"rejected", "completed"}:
        lines += [
            SEP,
            "  NEXT STEPS",
            SEP,
            "",
            row("Next Stage:", next_stage or "—"),
        ]
        if next_owner:
            lines.append(row("Next Owner:", _fmt_email_name(next_owner, next_owner_email)))
        lines.append("")

    if request_row.get("rejection_reason") and event == "rejected":
        lines += [
            SEP,
            "  REJECTION DETAILS",
            SEP,
            "",
            row("Reason:", str(request_row.get("rejection_reason"))),
            "",
        ]

    lines += [
        SEP,
        "",
        "  This email confirms that your action was successfully recorded in Derisk360.",
        "  Please do not reply to this email.",
        "  Support: support@derisk360.com",
        "",
        SEP,
        "",
    ]

    body = "\n".join(lines)
    subject = f"[Derisk360] Action Confirmed – {event_label} – {software} #{wf_id}"

    return [
        OutboundEmail(
            to_emails=[actor_email],
            subject=subject,
            body=body,
            event_type=f"actor_confirmation_{event}",
        )
    ]


def tool_request_created_emails(tool_request: dict, conn: Connection) -> list[OutboundEmail]:
    it_emails = _role_emails(conn, "it_admin")
    admin_emails = _role_emails(conn, "master_admin")
    recipients = _unique_emails(it_emails, admin_emails)
    if not recipients:
        raise ValueError("No IT Admin or Master Admin app users found for tool request email.")

    requester = _resolve_tool_request_requester(tool_request, conn)
    subject = str(tool_request.get("email_subject") or f"Request for {tool_request.get('requested_tool', 'Software')}")
    body = str(
        tool_request.get("message_body")
        or (
            f"Hello IT Team,\n\n"
            f"I would like access to {tool_request.get('requested_tool', 'software')}.\n\n"
            f"Department:\n{tool_request.get('department') or 'Not specified'}\n\n"
            f"Business Justification:\n{tool_request.get('business_justification') or 'Not provided'}\n\n"
            f"Thank you,\n{tool_request.get('requester_name', 'Employee')}"
        )
    )

    messages = [
        OutboundEmail(
            to_emails=recipients,
            subject=subject,
            body=body,
            event_type="tool_request_created",
        )
    ]

    if requester and requester.lower() not in {email.lower() for email in recipients}:
        messages.append(
            OutboundEmail(
                to_emails=[requester],
                subject=f"Tool request received: {tool_request.get('requested_tool', 'Software')}",
                body=(
                    f"Hello {tool_request.get('requester_name', 'there')},\n\n"
                    f"Your tool request for {tool_request.get('requested_tool', 'software')} has been received "
                    f"and sent to the IT team for review.\n"
                ),
                event_type="tool_request_acknowledgement",
            )
        )

    return messages
