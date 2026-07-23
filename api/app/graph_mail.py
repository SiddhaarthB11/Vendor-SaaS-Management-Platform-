"""Send workflow email through Microsoft Graph (recommended for Outlook / M365)."""

from __future__ import annotations

import json
import os
import urllib.parse

from app.employee_sync import EmployeeSyncConfig, _get_graph_access_token, _http_json, GRAPH_BASE


def graph_mail_configured() -> bool:
    return EmployeeSyncConfig.from_env().graph_configured


def graph_sender_address() -> str:
    return (
        os.environ.get("GRAPH_MAIL_SENDER", "").strip()
        or os.environ.get("SMTP_FROM", "").strip()
        or os.environ.get("SMTP_USER", "").strip()
    )


def send_graph_email(
    *,
    to_emails: list[str],
    subject: str,
    body: str,
    cc_emails: list[str] | None = None,
    from_email: str | None = None,
) -> tuple[str, str | None]:
    config = EmployeeSyncConfig.from_env()
    if not config.graph_configured:
        raise RuntimeError(
            "Microsoft Graph is not configured. Set MS_GRAPH_TENANT_ID, MS_GRAPH_CLIENT_ID, "
            "and MS_GRAPH_CLIENT_SECRET in infra/.env."
        )

    sender = (from_email or graph_sender_address()).strip()
    if not sender:
        raise RuntimeError(
            "No Graph sender mailbox configured. Set GRAPH_MAIL_SENDER or SMTP_FROM to a mailbox in your tenant."
        )

    recipients = [email.strip() for email in to_emails if str(email or "").strip()]
    if not recipients:
        raise ValueError("No email recipients supplied")

    cc_list = [email.strip() for email in (cc_emails or []) if str(email or "").strip()]
    token = _get_graph_access_token(config)
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "from": {"emailAddress": {"address": sender}},
            "toRecipients": [{"emailAddress": {"address": email}} for email in recipients],
        },
        "saveToSentItems": True,
    }
    if cc_list:
        payload["message"]["ccRecipients"] = [{"emailAddress": {"address": email}} for email in cc_list]

    sender_path = urllib.parse.quote(sender)
    url = f"{GRAPH_BASE}/users/{sender_path}/sendMail"
    _http_json(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload).encode("utf-8"),
    )
    return "SENT", "graph-" + os.urandom(8).hex()
