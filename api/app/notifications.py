import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import Header
from email.mime.text import MIMEText
from uuid import UUID

from psycopg import Connection


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def _slack_dm(email: str, text: str) -> None:
    """Send a Slack DM to a user identified by their email address. Silent on failure."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return
    try:
        import json as _json
        import urllib.request as _urllib

        # Look up Slack user ID by email
        req = _urllib.Request(
            f"https://slack.com/api/users.lookupByEmail?email={email}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with _urllib.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
        if not data.get("ok"):
            return
        user_id = data["user"]["id"]

        # Open a DM channel
        body = _json.dumps({"users": user_id}).encode()
        req2 = _urllib.Request(
            "https://slack.com/api/conversations.open",
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with _urllib.urlopen(req2, timeout=8) as resp2:
            data2 = _json.loads(resp2.read())
        if not data2.get("ok"):
            return
        channel_id = data2["channel"]["id"]

        # Post the message
        body3 = _json.dumps({"channel": channel_id, "text": text}).encode()
        req3 = _urllib.Request(
            "https://slack.com/api/chat.postMessage",
            data=body3,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        _urllib.urlopen(req3, timeout=8)
    except Exception:
        pass


def _strip_html(html: str) -> str:
    """Convert HTML email body to plain text for Slack."""
    import re
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _slack_post(text: str, webhook_url: str | None = None) -> None:
    """Post a plain-text message to a Slack webhook. Silent on failure."""
    url = webhook_url or os.environ.get("SLACK_WEBHOOK_SOFTWARE_REQUESTS", "")
    if not url:
        return
    try:
        import json as _json
        import urllib.request as _urllib
        data = _json.dumps({"text": text}).encode()
        req = _urllib.Request(url, data=data, headers={"Content-Type": "application/json"})
        _urllib.urlopen(req, timeout=8)
    except Exception:
        pass


def _slack_software_request(event: str, request_row: dict) -> None:
    """Send a Slack notification for employee software request workflow events."""
    payload = dict(request_row.get("payload") or {})
    software = str(payload.get("name") or payload.get("tool_name") or payload.get("requested_tool") or payload.get("offboarded_employee_display_name") or "request")
    requester = str(request_row.get("requested_by_email") or payload.get("requested_by_email") or "an employee")
    short_id = str(request_row.get("id") or "")[:8].upper()

    wf_type = str(request_row.get("workflow_type") or "")
    wf_label = wf_type.replace("_", " ").title()
    messages = {
        "submitted": (
            f":new: *{wf_label} Submitted* #{short_id}\n"
            f"*Requested by:* {requester}\n"
            f"*Item:* {software}\n"
            f"Awaiting approval."
        ),
        "line_manager_approved": (
            f":white_check_mark: *Line Manager Approved* #{short_id}\n"
            f"*Item:* {software} (requested by {requester})\n"
            f"Awaiting finance budget validation."
        ),
        "finance_approved": (
            f":moneybag: *Finance Approved* #{short_id}\n"
            f"*Item:* {software} (requested by {requester})\n"
            f"IT — please complete procurement."
        ),
        "completed": (
            f":rocket: *{wf_label} Completed* #{short_id}\n"
            f"*Requested by:* {requester}\n"
            f"*Item:* {software} — done."
        ),
        "rejected": (
            f":x: *{wf_label} Rejected* #{short_id}\n"
            f"*Requested by:* {requester}\n"
            f"*Item:* {software}"
        ),
    }

    text = messages.get(event)
    if text:
        _slack_post(text)


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("true", "1", "yes", "on")


def _row_id(row) -> UUID | None:
    if not row:
        return None
    if isinstance(row, dict):
        return row.get("id")
    return row[0]


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    use_ssl: bool
    use_starttls: bool
    user: str
    password: str
    from_address: str

    @property
    def is_configured(self) -> bool:
        return bool(self.host)

    @property
    def delivery_mode(self) -> str:
        if not self.is_configured:
            return "mock"
        if self.host.lower() in {"mailhog", "localhost", "127.0.0.1"} and self.port == 1025:
            return "mailhog"
        if "gmail.com" in self.host.lower():
            return "gmail"
        if "office365" in self.host.lower() or "outlook" in self.host.lower():
            return "outlook"
        return "smtp"

    @property
    def smtp_ready(self) -> bool:
        return bool(self.host and self.user and self.password)

    @classmethod
    def from_env(cls) -> "SmtpConfig":
        provider = os.environ.get("SMTP_PROVIDER", "").strip().lower()
        host = os.environ.get("SMTP_HOST", "").strip()
        port_raw = os.environ.get("SMTP_PORT", "").strip()
        user = os.environ.get("SMTP_USER", "").strip()
        password = os.environ.get("SMTP_PASS") or os.environ.get("SMTP_PASSWORD") or ""
        from_address = os.environ.get("SMTP_FROM", "").strip()

        if provider in {"outlook", "office365", "microsoft365", "m365"}:
            host = host or "smtp.office365.com"
            port = int(port_raw or "587")
            use_ssl = False
            use_starttls = True
        elif provider in {"outlook-personal", "hotmail", "live"}:
            host = host or "smtp-mail.outlook.com"
            port = int(port_raw or "587")
            use_ssl = False
            use_starttls = True
        elif provider in {"gmail", "google"}:
            host = host or "smtp.gmail.com"
            port = int(port_raw or "587")
            use_ssl = False
            use_starttls = True
        else:
            port = int(port_raw or "587")
            use_ssl = _parse_bool(os.environ.get("SMTP_SECURE"), default=(port == 465))
            starttls_env = os.environ.get("SMTP_STARTTLS")
            if starttls_env is not None and starttls_env.strip() != "":
                use_starttls = _parse_bool(starttls_env, default=False)
            else:
                # Dev SMTP sinks such as Mailhog on 1025 do not support STARTTLS.
                use_starttls = port in (587, 25) and not use_ssl

        outlook_providers = {"outlook", "office365", "microsoft365", "m365", "outlook-personal", "hotmail", "live"}
        gmail_providers = {"gmail", "google"}
        # Determine the 'from_address' based on environment variables and provider.
        # Prioritize SMTP_FROM if it's a valid-looking email.
        # Otherwise, try SMTP_USER if it's a valid-looking email.
        # Fallback to a default if neither is suitable.

        candidate_from_address = os.environ.get("SMTP_FROM", "").strip()
        candidate_user_email = user if "@" in user else ""  # Only consider user as email if it contains '@'

        if candidate_from_address and "@" in candidate_from_address and \
           not (candidate_from_address.endswith("@derisk360.local") or candidate_from_address.endswith("@demo.derisk360.com")):
            from_address = candidate_from_address
        elif candidate_user_email:
            from_address = candidate_user_email
        else:
            from_address = "no-reply@derisk360.com"

        return cls(
            host=host,
            port=port,
            use_ssl=use_ssl,
            use_starttls=use_starttls,
            user=user,
            password=password,
            from_address=from_address,
        )


def get_smtp_status() -> dict:
    from app.graph_mail import graph_mail_configured, graph_sender_address

    config = SmtpConfig.from_env()
    graph_ready = graph_mail_configured()
    graph_sender = graph_sender_address() if graph_ready else None
    delivery_enabled = _parse_bool(os.environ.get("EMAIL_DELIVERY_ENABLED"), default=False)
    delivery_method = os.environ.get("EMAIL_DELIVERY_METHOD", "auto").strip().lower() or "auto"
    if delivery_method not in {"auto", "graph", "smtp"}:
        delivery_method = "auto"
    preferred = delivery_method
    if delivery_method == "auto":
        preferred = "graph" if graph_ready and graph_sender else "smtp"
    return {
        "configured": config.is_configured or graph_ready,
        "deliveryMode": config.delivery_mode,
        "host": config.host or None,
        "port": config.port,
        "useSsl": config.use_ssl,
        "useStarttls": config.use_starttls,
        "fromAddress": graph_sender or config.from_address,
        "authConfigured": bool(config.user and config.password),
        "provider": os.environ.get("SMTP_PROVIDER", "").strip() or None,
        "mailhogUiUrl": "http://localhost:8025" if config.delivery_mode == "mailhog" else None,
        "gmailConfigured": config.delivery_mode == "gmail" and config.smtp_ready,
        "outlookConfigured": config.delivery_mode == "outlook" and config.smtp_ready,
        "smtpReady": config.smtp_ready,
        "emailDeliveryEnabled": delivery_enabled,
        "graphConfigured": graph_ready,
        "graphSender": graph_sender,
        "preferredDeliveryMethod": preferred if delivery_enabled else "preview",
        "setupNote": (
            "Gmail requires an App Password when 2-Step Verification is enabled. "
            "Create one at https://myaccount.google.com/apppasswords"
            if config.delivery_mode == "gmail"
            else "Configure SMTP_USER and SMTP_PASS in infra/.env, then restart the API container."
        ),
    }


def _send_via_smtp(
    config: SmtpConfig,
    to_emails: list[str],
    cc_email: str | None,
    subject: str,
    body: str,
) -> tuple[str, str | None]:
    recipients = [email for email in to_emails if email]
    if cc_email:
        recipients.append(cc_email)
    if not recipients:
        raise ValueError("No email recipients supplied")

    mime_subtype = "html" if body.strip().startswith("<") else "plain"
    msg = MIMEText(body, mime_subtype, "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = config.from_address
    msg["To"] = ", ".join(to_emails)
    if cc_email:
        msg["Cc"] = cc_email

    server = None
    try:
        if config.use_ssl:
            server = smtplib.SMTP_SSL(config.host, config.port, timeout=15)
        else:
            server = smtplib.SMTP(config.host, config.port, timeout=15)
            server.ehlo()
            if config.use_starttls:
                server.starttls()
                server.ehlo()

        if config.user and config.password:
            server.login(config.user, config.password)

        server.sendmail(config.from_address, recipients, msg.as_string())
        return "SENT", "smtp-msg-" + os.urandom(8).hex()
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def send_workflow_email(
    subject: str,
    body: str,
    conn: Connection,
    to_email: str = "",
    to_emails: list[str] | None = None,
    workflow_request_id: UUID = None,
    tool_request_id: UUID = None,
    event_type: str = "general",
    cc_email: str = None,
    workflow_stage: str | None = None,
) -> dict:
    recipients = [email.strip() for email in (to_emails or []) if str(email or "").strip()]
    if not recipients and to_email:
        recipients = [to_email.strip()]
    if not recipients:
        return {"status": "FAILED", "error": "No email recipients supplied", "log_id": None}

    to_display = ", ".join(recipients)
    config = SmtpConfig.from_env()
    from_email = config.from_address or config.user or "no-reply@slmct.local"

    print("\n========================================================")
    print(f"EMAIL TRIGGERED FOR: {to_display}")
    print(f"EVENT TYPE: {event_type}")
    print(f"SUBJECT: {subject}")
    print(f"SMTP MODE: {config.delivery_mode}")
    print(f"BODY:\n{body}")
    print("========================================================\n")

    from app.graph_mail import graph_mail_configured

    status = "PENDING"
    provider_message_id = None
    error_message = None
    is_mock = not config.is_configured and not graph_mail_configured()
    preview_only = not _parse_bool(os.environ.get("EMAIL_DELIVERY_ENABLED"), default=False)

    if preview_only:
        status = "PREVIEW"
        provider_message_id = "preview-" + os.urandom(4).hex()
        print("EMAIL_DELIVERY_ENABLED=false. Email preview stored; SMTP delivery skipped.")
    elif is_mock and not graph_mail_configured():
        status = "MOCK_MODE"
        provider_message_id = "mock-msg-" + os.urandom(4).hex()
        print("SMTP/Graph not configured. Email was logged but not delivered.")

    log_id = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO slmct.email_logs (
                    workflow_request_id, tool_request_id, event_type, from_email, to_email, cc_email,
                    subject, body, status, provider_message_id, error_message, workflow_stage
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    workflow_request_id,
                    tool_request_id,
                    event_type,
                    from_email,
                    to_display,
                    cc_email,
                    subject,
                    body,
                    status,
                    provider_message_id,
                    error_message,
                    workflow_stage,
                ),
            )
            log_id = _row_id(cur.fetchone())
            conn.commit()
    except Exception as db_err:
        conn.rollback()
        print(f"Database logging failed: {db_err}")
        return {"status": "FAILED", "error": f"Email log insert failed: {db_err}", "log_id": None}

    if is_mock or preview_only:
        return {"status": status, "log_id": str(log_id) if log_id else None}

    from app.graph_mail import graph_mail_configured, graph_sender_address, send_graph_email

    delivery_method = os.environ.get("EMAIL_DELIVERY_METHOD", "auto").strip().lower() or "auto"
    use_graph = delivery_method == "graph" or (delivery_method == "auto" and graph_mail_configured() and graph_sender_address())
    use_smtp = delivery_method == "smtp" or (
        delivery_method == "auto" and not use_graph and config.is_configured
    )

    if use_graph:
        try:
            cc_list = [part.strip() for part in str(cc_email or "").split(",") if part.strip()]
            status, provider_message_id = send_graph_email(
                to_emails=recipients,
                subject=subject,
                body=body,
                cc_emails=cc_list,
                from_email=from_email,
            )
            error_message = None
            print(f"Successfully delivered email via Microsoft Graph to {to_display}")
        except Exception as graph_err:
            status = "FAILED"
            provider_message_id = None
            error_message = str(graph_err)
            print(f"Error sending email via Microsoft Graph to {to_display}: {graph_err}")
    elif use_smtp and config.delivery_mode in {"outlook", "smtp", "gmail"} and (not config.user or not config.password):
        status = "FAILED"
        error_message = (
            "SMTP credentials are not set. Add SMTP_USER and SMTP_PASS to infra/.env, then restart the API."
        )
        print(error_message)
    elif use_smtp:
        try:
            status, provider_message_id = _send_via_smtp(config, recipients, cc_email, subject, body)
            error_message = None
            print(f"Successfully delivered email via SMTP to {to_display}")
        except Exception as smtp_err:
            status = "FAILED"
            provider_message_id = None
            error_message = str(smtp_err)
            print(f"Error sending email via SMTP to {to_display}: {smtp_err}")
    else:
        status = "FAILED"
        error_message = (
            "No live email delivery method is configured. Set Microsoft Graph (MS_GRAPH_*) "
            "or SMTP credentials in infra/.env."
        )
        print(error_message)

    if log_id:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE slmct.email_logs
                    SET status = %s,
                        provider_message_id = %s,
                        error_message = %s,
                        sent_at = %s
                    WHERE id = %s
                    """,
                    (
                        status,
                        provider_message_id,
                        error_message,
                        datetime.now(timezone.utc) if status == "SENT" else None,
                        log_id,
                    ),
                )
                conn.commit()
        except Exception as db_up_err:
            conn.rollback()
            print(f"Failed to update email log in database: {db_up_err}")

    return {
        "status": status,
        "error": error_message,
        "log_id": str(log_id) if log_id else None,
    }


def _ensure_email_sent(result: dict) -> None:
    if result.get("status") == "FAILED":
        raise RuntimeError(result.get("error") or "Email delivery failed")


def dispatch_outbound_emails(
    messages,
    conn: Connection,
    workflow_request_id: UUID | None = None,
    tool_request_id: UUID | None = None,
    workflow_stage: str | None = None,
) -> None:
    failures: list[str] = []
    for message in messages:
        cc = ", ".join(message.cc_emails) if getattr(message, "cc_emails", None) else None
        result = send_workflow_email(
            subject=message.subject,
            body=message.body,
            conn=conn,
            to_emails=message.to_emails,
            cc_email=cc or None,
            workflow_request_id=workflow_request_id,
            tool_request_id=tool_request_id,
            event_type=message.event_type,
            workflow_stage=workflow_stage,
        )
        if result.get("status") == "FAILED" and not result.get("log_id"):
            # Only raise if the email couldn't even be logged to the database.
            # SMTP/Graph delivery failures are already recorded in email_logs with FAILED
            # status and visible in Email History — no need to surface them as a UI warning.
            failures.append(str(result.get("error") or message.event_type))
        # Mirror every outbound email to Slack channel + DM each recipient
        to_line = ", ".join(message.to_emails) if message.to_emails else ""
        plain_body = _strip_html(message.body)
        slack_text = f"*To:* {to_line}\n*Subject:* {message.subject}\n\n{plain_body}"
        _slack_post(slack_text)
        for recipient_email in (message.to_emails or []):
            _slack_dm(recipient_email, slack_text)
    if failures:
        raise RuntimeError(failures[0])


def _dispatch_governance_emails(
    event: str,
    request_row: dict,
    conn: Connection,
    actor_email: str | None = None,
) -> None:
    from app.workflow_governance import build_workflow_emails, get_current_approver

    previews = build_workflow_emails(event, request_row, conn)
    from app.email_templates import OutboundEmail, workflow_actor_confirmation_emails

    approver = get_current_approver(request_row, conn)
    workflow_stage = str(approver.get("stage") or request_row.get("status") or event)

    messages = [
        OutboundEmail(
            to_emails=preview["toEmails"],
            cc_emails=tuple(preview.get("ccEmails") or []),
            subject=preview["subject"],
            body=preview["body"],
            event_type=preview["eventType"],
        )
        for preview in previews
    ]

    # Append actor confirmation if we know who performed the action
    if actor_email:
        confirmation = workflow_actor_confirmation_emails(request_row, conn, event, actor_email)
        messages.extend(confirmation)

    dispatch_outbound_emails(
        messages,
        conn,
        workflow_request_id=request_row["id"],
        workflow_stage=workflow_stage,
    )


def notify_tool_request_created(tool_request: dict, conn: Connection):
    from app.email_templates import tool_request_created_emails

    dispatch_outbound_emails(
        tool_request_created_emails(tool_request, conn),
        conn,
        tool_request_id=tool_request["id"],
    )


def notify_tool_request_rejected(tool_request: dict, conn: Connection):
    requester_email = str(tool_request.get("requester_email") or "").strip()
    if not requester_email:
        return
    from app.email_templates import OutboundEmail

    reason = str(tool_request.get("rejection_reason") or "No reason provided.")
    dispatch_outbound_emails(
        [
            OutboundEmail(
                to_emails=[requester_email],
                subject=f"Software request rejected: {tool_request.get('requested_tool') or 'Request'}",
                body=(
                    "Your software request was rejected.\n\n"
                    f"Tool: {tool_request.get('requested_tool') or '-'}\n\n"
                    f"Reason:\n{reason}"
                ),
                event_type="tool_request_rejected",
            )
        ],
        conn,
        tool_request_id=tool_request["id"],
    )


def notify_tool_request_info_requested(tool_request: dict, conn: Connection):
    requester_email = str(tool_request.get("requester_email") or "").strip()
    if not requester_email:
        return
    from app.email_templates import OutboundEmail

    message = str(tool_request.get("notes") or tool_request.get("info_request_message") or "Please provide more information.")
    dispatch_outbound_emails(
        [
            OutboundEmail(
                to_emails=[requester_email],
                subject=f"More information required: {tool_request.get('requested_tool') or 'Software request'}",
                body=(
                    "Your software request needs additional information before it can proceed.\n\n"
                    f"Tool: {tool_request.get('requested_tool') or '-'}\n\n"
                    f"Message:\n{message}"
                ),
                event_type="tool_request_info_requested",
            )
        ],
        conn,
        tool_request_id=tool_request["id"],
    )


def notify_workflow_created(request_row: dict, conn: Connection, email_overrides: list[dict] | None = None):
    if email_overrides:
        _dispatch_email_overrides(email_overrides, request_row, conn)
    else:
        _dispatch_governance_emails("submitted", request_row, conn)
    _slack_software_request("submitted", request_row)


def _dispatch_email_overrides(overrides: list[dict], request_row: dict, conn: Connection) -> None:
    from app.email_templates import OutboundEmail
    from app.workflow_governance import get_current_approver

    approver = get_current_approver(request_row, conn)
    workflow_stage = str(approver.get("stage") or request_row.get("status") or "submitted")
    messages = []
    for item in overrides:
        to_raw = str(item.get("to") or "").strip()
        to_emails = [entry.strip() for entry in to_raw.split(",") if entry.strip()]
        if not to_emails:
            continue
        cc_raw = str(item.get("cc") or "").strip()
        cc_emails = [entry.strip() for entry in cc_raw.split(",") if entry.strip()]
        messages.append(
            OutboundEmail(
                to_emails=to_emails,
                cc_emails=tuple(cc_emails),
                subject=str(item.get("subject") or "Workflow notification"),
                body=str(item.get("body") or ""),
                event_type=str(item.get("eventType") or "custom_preview"),
            )
        )
    if messages:
        dispatch_outbound_emails(
            messages,
            conn,
            workflow_request_id=request_row["id"],
            workflow_stage=workflow_stage,
        )


def notify_workflow_approved(request_row: dict, conn: Connection, actor_email: str | None = None):
    workflow_type = str(request_row.get("workflow_type") or "")
    status = str(request_row.get("status") or "")
    from app.workflow_governance import requires_line_manager

    if requires_line_manager(workflow_type) and status == "line_manager_approved":
        _dispatch_governance_emails("line_manager_approved", request_row, conn, actor_email)
        _slack_software_request("line_manager_approved", request_row)
    elif status == "master_approved":
        _dispatch_governance_emails("master_approved", request_row, conn, actor_email)
    else:
        _dispatch_governance_emails("approved", request_row, conn, actor_email)


def notify_workflow_budget_validated(request_row: dict, conn: Connection, actor_email: str | None = None):
    _dispatch_governance_emails("finance_approved", request_row, conn, actor_email)
    _slack_software_request("finance_approved", request_row)


def notify_workflow_rejected(request_row: dict, conn: Connection, actor_email: str | None = None):
    _dispatch_governance_emails("rejected", request_row, conn, actor_email)
    _slack_software_request("rejected", request_row)


def notify_workflow_reopened(request_row: dict, conn: Connection, actor_email: str | None = None):
    notify_workflow_created(request_row, conn)


def notify_workflow_info_requested(request_row: dict, conn: Connection, actor_email: str | None = None):
    _dispatch_governance_emails("info_requested", request_row, conn, actor_email)


def notify_workflow_completed(request_row: dict, conn: Connection, actor_email: str | None = None):
    _dispatch_governance_emails("completed", request_row, conn, actor_email)
    _slack_software_request("completed", request_row)


def run_renewal_alerts(conn: Connection) -> dict:
    """
    Scan all active subscriptions with renewal dates in 30, 60, or 90 days
    and send alert emails to master_admin and finance recipients.
    Returns a summary dict with counts.
    """
    from app.email_templates import OutboundEmail, _role_emails

    THRESHOLDS = [30, 60, 90]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                s.id, s.name, s.renewal_date, s.amount, s.currency_code,
                s.billing_cycle, s.department,
                v.name AS vendor_name,
                p.full_name AS owner_name,
                p.work_email AS owner_email,
                (s.renewal_date - CURRENT_DATE) AS days_until_renewal
            FROM slmct.subscriptions s
            LEFT JOIN slmct.vendors v ON v.id = s.vendor_id
            LEFT JOIN slmct.people p ON p.id = s.owner_person_id
            WHERE s.status = 'active'
              AND s.renewal_date IS NOT NULL
              AND (s.renewal_date - CURRENT_DATE) = ANY(%s)
            ORDER BY s.renewal_date ASC
            """,
            (THRESHOLDS,),
        )
        rows = cur.fetchall()

    if not rows:
        return {"sent": 0, "subscriptions": []}

    master_admins = _role_emails(conn, "master_admin")
    finance_emails = _role_emails(conn, "finance")
    it_emails = _role_emails(conn, "it_admin")
    recipients = list(dict.fromkeys(master_admins + finance_emails + it_emails))
    if not recipients:
        return {"sent": 0, "subscriptions": [], "error": "No recipients found"}

    sent = 0
    summaries = []
    for row in rows:
        days = int(row["days_until_renewal"])
        name = str(row["name"])
        renewal_date = str(row["renewal_date"])
        vendor = str(row["vendor_name"] or "Unknown vendor")
        amount = float(row["amount"] or 0)
        currency = str(row["currency_code"] or "AED").strip()
        billing = str(row["billing_cycle"] or "annual")
        department = str(row["department"] or "-")
        owner_email = str(row["owner_email"] or "").strip()
        owner_name = str(row["owner_name"] or "").strip()

        # Per-subscription recipients: shared list + owner if set and not already included
        sub_recipients = list(recipients)
        if owner_email and owner_email not in sub_recipients:
            sub_recipients.append(owner_email)

        subject = f"[Derisk360] Renewal Alert: {name} renews in {days} day{'s' if days != 1 else ''}"

        body = f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;padding:32px">
<div style="max-width:600px;margin:0 auto;background:#1e293b;border-radius:12px;padding:32px">
  <h2 style="color:#14b8a6;margin-top:0">&#128197; Subscription Renewal Alert</h2>
  <p style="color:#94a3b8">This is an automated reminder that the following subscription is due for renewal.</p>
  <table style="width:100%;border-collapse:collapse;margin:20px 0">
    <tr><td style="padding:10px;border-bottom:1px solid #334155;color:#94a3b8;width:40%">Subscription</td><td style="padding:10px;border-bottom:1px solid #334155;font-weight:600">{name}</td></tr>
    <tr><td style="padding:10px;border-bottom:1px solid #334155;color:#94a3b8">Vendor</td><td style="padding:10px;border-bottom:1px solid #334155">{vendor}</td></tr>
    <tr><td style="padding:10px;border-bottom:1px solid #334155;color:#94a3b8">Renewal Date</td><td style="padding:10px;border-bottom:1px solid #334155;color:#f59e0b;font-weight:600">{renewal_date} ({days} days)</td></tr>
    <tr><td style="padding:10px;border-bottom:1px solid #334155;color:#94a3b8">Amount</td><td style="padding:10px;border-bottom:1px solid #334155">{amount:,.2f} {currency} / {billing}</td></tr>
    <tr><td style="padding:10px;border-bottom:1px solid #334155;color:#94a3b8">Department</td><td style="padding:10px;border-bottom:1px solid #334155">{department}</td></tr>
    {f'<tr><td style="padding:10px;border-bottom:1px solid #334155;color:#94a3b8">Subscription Owner</td><td style="padding:10px;border-bottom:1px solid #334155">{owner_name} &lt;{owner_email}&gt;</td></tr>' if owner_email else ''}
  </table>
  <p style="color:#94a3b8;font-size:14px">Action required: review this subscription and submit a renewal request or cancel before the renewal date to avoid unexpected charges.</p>
  <p style="color:#475569;font-size:12px;margin-top:32px">Derisk360 SLMCT Platform — automated renewal alert</p>
</div>
</body></html>"""

        email = OutboundEmail(
            to_emails=sub_recipients,
            subject=subject,
            body=body,
            event_type="renewal_alert",
        )
        dispatch_outbound_emails([email], conn)
        sent += 1
        summaries.append({"name": name, "days_until_renewal": days, "renewal_date": renewal_date, "vendor": vendor})

    return {"sent": sent, "subscriptions": summaries}
