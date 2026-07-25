"""Workflow governance: typed business processes, approvers, and email routing."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import Connection

WORKFLOW_TYPES = {
    "employee_software_request": {
        "label": "Employee Software Request",
        "stages": [
            "submitted",
            "line_manager_approved",
            "finance_approved",
            "completed",
        ],
        "requires_line_manager": True,
    },
    "new_subscription_request": {
        "label": "New Subscription Request",
        "stages": ["submitted", "finance_approved", "completed"],
        "requires_line_manager": False,
    },
    "license_assignment_request": {
        "label": "License Assignment Request",
        "stages": ["submitted", "finance_approved", "completed"],
        "requires_line_manager": False,
    },
    "renewal_request": {
        "label": "Renewal Request",
        "stages": ["submitted", "finance_approved", "completed"],
        "requires_line_manager": False,
    },
    "hr_onboarding_request": {
        "label": "HR Onboarding — Licence Request",
        "stages": ["submitted", "finance_approved", "completed"],
        "requires_line_manager": False,
    },
    "generic_procurement": {
        "label": "Procurement Workflow",
        "stages": ["submitted", "master_approved", "finance_approved", "completed"],
        "requires_line_manager": False,
    },
    "employee_offboarding": {
        "label": "Employee Offboarding",
        "stages": ["submitted", "it_confirmed", "completed"],
        "requires_line_manager": False,
    },
}

STAGE_LABELS = {
    "draft": "Draft",
    "submitted": "Submitted",
    "line_manager_approved": "Waiting for Budget Approval",
    "master_approved": "Waiting for Budget Approval",
    "finance_approved": "Awaiting Procurement",
    "finance_closed": "Finance Closed",
    "rejected": "Rejected",
    "completed": "Completed",
    "cancelled": "Cancelled",
    "reopened": "Submitted",
    "info_requested": "More Information Requested",
}


def display_workflow_status(request_row: dict[str, Any]) -> str:
    status = str(request_row.get("status") or "").lower()
    workflow_type = str(request_row.get("workflow_type") or "generic_procurement")
    if status == "info_requested":
        pre = str(request_row.get("pre_info_request_status") or "").lower()
        needs_lm = requires_line_manager(workflow_type)
        if pre in ("submitted", "reopened"):
            return "More Info Needed · Waiting for LM Approval" if needs_lm else "More Info Needed · Waiting for Budget Approval"
        if pre in ("line_manager_approved", "master_approved"):
            return "More Info Needed · Waiting for Budget Approval"
        if pre == "finance_approved":
            return "More Info Needed · Awaiting Procurement"
        return "More Information Requested"
    if status in {"submitted", "reopened"} and requires_line_manager(workflow_type):
        return "Waiting for Line Manager Approval"
    if status in {"submitted", "reopened", "line_manager_approved", "master_approved"}:
        return "Waiting for Budget Approval"
    if status == "finance_approved":
        if workflow_type == "license_assignment_request":
            return "Awaiting IT Assignment"
        return "Awaiting Procurement"
    return stage_label(status)

# Canonical workflow-generated email templates (spec EMAIL 1–7)
EMAIL_TEMPLATE_LABELS = {
    "request_submitted": "Email 1 — Request Submitted",
    "workflow_started": "Email 2 — Request Received",
    "request_approved": "Email 3 — Approved",
    "request_rejected": "Email 4 — Rejected",
    "stage_forwarded": "Email 5 — Forwarded To Next Stage",
    "action_required": "Email 6 — Action Required",
    "procurement_required": "Email 6b — Procurement Required",
    "request_completed": "Email 7 — Completed",
    "procurement_completed": "Email 7b — Procurement Recorded",
    "info_requested": "More Information Requested",
}


def resolve_workflow_type(requested_module: str, payload: dict[str, Any], explicit: str | None = None) -> str:
    if explicit and explicit in WORKFLOW_TYPES:
        return explicit
    if payload.get("workflow_type") in WORKFLOW_TYPES:
        return str(payload["workflow_type"])

    if requested_module == "renewals":
        return "renewal_request"
    if requested_module == "subscriptions":
        return "new_subscription_request"
    if requested_module == "licences":
        return "license_assignment_request"
    if requested_module == "employees" or payload.get("tool_requested") or payload.get("business_justification"):
        return "employee_software_request"
    return "generic_procurement"


def stage_label(status: str | None) -> str:
    return STAGE_LABELS.get(str(status or "").strip(), str(status or "-"))


def requires_line_manager(workflow_type: str) -> bool:
    return bool(WORKFLOW_TYPES.get(workflow_type, {}).get("requires_line_manager"))


def finance_validation_statuses(workflow_type: str) -> tuple[str, ...]:
    if requires_line_manager(workflow_type):
        return ("line_manager_approved", "master_approved")
    if workflow_type == "employee_offboarding":
        return ("submitted", "reopened")
    if workflow_type == "new_subscription_request":
        return ("submitted", "reopened", "master_approved")
    if workflow_type == "renewal_request":
        return ("submitted", "reopened", "master_approved")
    if workflow_type == "license_assignment_request":
        return ("submitted", "reopened", "master_approved")
    if workflow_type == "hr_onboarding_request":
        return ("submitted", "reopened", "master_approved")
    return ("submitted", "reopened", "master_approved")


def get_current_approver(request_row: dict[str, Any], conn: Connection) -> dict[str, str | None]:
    status = str(request_row.get("status") or "").lower()
    workflow_type = str(request_row.get("workflow_type") or "generic_procurement")

    if status in {"completed", "rejected", "cancelled", "info_requested"}:
        if status == "info_requested":
            return {
                "role": "requester",
                "name": "Requester",
                "email": _resolve_requester_email(request_row, conn),
                "stage": display_workflow_status(request_row),
            }
        return {
            "role": "-",
            "name": "-",
            "email": None,
            "stage": stage_label(status),
        }

    if status in {"submitted", "reopened"}:
        if requires_line_manager(workflow_type):
            emails = _line_manager_emails(conn, request_row)
            return {
                "role": "line_manager",
                "name": "Line Manager",
                "email": emails[0] if emails else None,
                "stage": stage_label(status),
            }
        if workflow_type == "employee_offboarding":
            emails = _role_emails(conn, "it_admin")
            return {
                "role": "it_admin",
                "name": "IT Administrator",
                "email": emails[0] if emails else None,
                "stage": "Awaiting IT Confirmation",
            }
        if workflow_type == "generic_procurement":
            master = _role_emails(conn, "master_admin")
            return {
                "role": "master_admin",
                "name": "Master Admin",
                "email": master[0] if master else None,
                "stage": stage_label(status),
            }
        emails = _role_emails(conn, "finance")
        return {
            "role": "finance",
            "name": "Finance Manager",
            "email": emails[0] if emails else None,
            "stage": stage_label(status),
        }

    if status == "line_manager_approved":
        emails = _role_emails(conn, "finance")
        return {
            "role": "finance",
            "name": "Finance Manager",
            "email": emails[0] if emails else None,
            "stage": stage_label(status),
        }

    if status == "master_approved":
        emails = _role_emails(conn, "finance")
        return {
            "role": "finance",
            "name": "Finance Manager",
            "email": emails[0] if emails else None,
            "stage": stage_label(status),
        }

    if status == "finance_approved":
        emails = _procurement_emails(conn)
        return {
            "role": "it_admin",
            "name": "IT Administrator",
            "email": emails[0] if emails else None,
            "stage": "Awaiting Procurement",
        }

    return {"role": "-", "name": "-", "email": None, "stage": stage_label(status)}


def record_status_history(
    conn: Connection,
    workflow_request_id: UUID | str,
    from_status: str | None,
    to_status: str,
    actor_user_id: UUID | str | None = None,
    actor_email: str | None = None,
    notes: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO slmct.workflow_status_history (
              workflow_request_id, from_status, to_status, actor_user_id, actor_email, notes
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (workflow_request_id, from_status, to_status, actor_user_id, actor_email, notes),
        )


def _role_emails(conn: Connection, role_code: str) -> list[str]:
    from app.email_templates import _get_emails_by_role

    return _get_emails_by_role(conn, role_code)


def _resolve_requester_email(request_row: dict[str, Any], conn: Connection) -> str:
    from app.email_templates import _resolve_workflow_requester
    import re  # `re` is used for email validation

    # Prioritize the email stored directly in the workflow request record
    email_from_record = str(request_row.get("requested_by_email") or "").strip()
    if email_from_record and re.match(r"[^@]+@[^@]+\.[^@]+", email_from_record):
        return email_from_record

    # Fallback to the dedicated resolver from email_templates
    requester_email = str(_resolve_workflow_requester(request_row, conn) or "").strip()
    if requester_email and re.match(r"[^@]+@[^@]+\.[^@]+", requester_email):
        return requester_email

    # Fallback to master admin email
    master_admin_emails = _role_emails(conn, "master_admin")
    if master_admin_emails:
        return master_admin_emails[0]

    # Final fallback: generic system email
    return "system-notifications@derisk360.com"


def _resolve_requester_name(request_row: dict[str, Any], requester_email: str) -> str:
    payload = request_row.get("payload") or {}
    name = str(payload.get("requester_name") or "").strip()
    return name or requester_email or "-"


def _line_manager_emails(conn: Connection, request_row: dict[str, Any]) -> list[str]:
    from app.email_templates import _resolve_app_user_email

    payload = request_row.get("payload") or {}
    explicit = str(payload.get("line_manager_email") or "").strip()
    if explicit:
        resolved = _resolve_app_user_email(conn, explicit)
        if resolved:
            return [resolved]
    managers = _role_emails(conn, "line_manager")
    if managers:
        return managers
    return _role_emails(conn, "finance")


def _procurement_emails(conn: Connection) -> list[str]:
    """IT admins who complete procurement after finance approval."""
    emails = _role_emails(conn, "it_admin")
    if emails:
        return emails
    from app.test_users import configured_test_users

    for entry in configured_test_users():
        if entry.get("role") == "it_admin" and entry.get("email"):
            return [str(entry["email"])]
    return []


def _approver_emails_for_status(request_row: dict[str, Any], conn: Connection) -> list[str]:
    approver = get_current_approver(request_row, conn)
    role = str(approver.get("role") or "")
    if role and role != "-":
        role_emails = _role_emails(conn, role)
        if role_emails:
            return role_emails
    email = approver.get("email")
    if email:
        return [email]
    return []


def _workflow_email_context(request_row: dict[str, Any], conn: Connection) -> dict[str, str]:
    payload = request_row.get("payload") or {}
    requester_email = _resolve_requester_email(request_row, conn)
    requester_name = _resolve_requester_name(request_row, requester_email)
    workflow_type = str(request_row.get("workflow_type") or "generic_procurement")
    status = str(request_row.get("status") or "submitted")
    approver = get_current_approver(request_row, conn)
    raw_workflow_id = str(request_row.get("id") or "")
    workflow_id = raw_workflow_id.upper() if raw_workflow_id else "Pending"

    return {
        "workflow_id": workflow_id,
        "requester": requester_name,
        "requester_email": requester_email or "-",
        "request_type": WORKFLOW_TYPES.get(workflow_type, {}).get("label", workflow_type),
        "current_stage": approver.get("stage") or stage_label(status),
        "approver": str(approver.get("name") or "-"),
        "approver_email": str(approver.get("email") or "-"),
        "next_approver": str(approver.get("name") or "-"),
        "next_approver_email": str(approver.get("email") or "-"),
        "owner": str(approver.get("name") or "-"),
        "owner_email": str(approver.get("email") or "-"),
        "stage": approver.get("stage") or stage_label(status),
        "rejection_reason": str(request_row.get("rejection_reason") or "No reason provided."),
    }


def _render_template(template_key: str, ctx: dict[str, str]) -> tuple[str, str]:
    if template_key == "request_submitted":
        return (
            "New Request Submitted",
            (
                "A new request has been submitted.\n\n"
                f"Requester:\n{ctx['requester']}\n\n"
                f"Requester Email:\n{ctx['requester_email']}\n\n"
                f"Request Type:\n{ctx['request_type']}\n\n"
                f"Workflow ID:\n{ctx['workflow_id']}\n\n"
                "Please review and take action."
            ),
        )
    if template_key == "workflow_started":
        return (
            f"Request Submitted – Workflow Started #{ctx['workflow_id']}",
            (
                "Your request has been successfully submitted.\n\n"
                f"Workflow ID:\n{ctx['workflow_id']}\n\n"
                f"Current Stage:\n{ctx['current_stage']}\n\n"
                f"Current Approver:\n{ctx['approver']}\n\n"
                f"Approver Email:\n{ctx['approver_email']}"
            ),
        )
    if template_key == "request_approved":
        return (
            "Request Approved",
            (
                "Your request has been approved.\n\n"
                f"Workflow ID:\n{ctx['workflow_id']}\n\n"
                f"Current Stage:\n{ctx['current_stage']}\n\n"
                f"Next Approver:\n{ctx['next_approver']}\n\n"
                f"Next Approver Email:\n{ctx['next_approver_email']}"
            ),
        )
    if template_key == "request_rejected":
        return (
            "Request Rejected",
            (
                "Your request has been rejected.\n\n"
                f"Workflow ID:\n{ctx['workflow_id']}\n\n"
                f"Reason:\n{ctx['rejection_reason']}"
            ),
        )
    if template_key == "stage_forwarded":
        return (
            "Request Moved To Next Stage",
            (
                "Your request has progressed.\n\n"
                f"Current Stage:\n{ctx['stage']}\n\n"
                f"Current Owner:\n{ctx['owner']}\n\n"
                f"Owner Email:\n{ctx['owner_email']}"
            ),
        )
    if template_key == "action_required":
        return (
            f"Action Required – Workflow #{ctx['workflow_id']}",
            (
                "A request is awaiting your review.\n\n"
                f"Requester:\n{ctx['requester']}\n\n"
                f"Requester Email:\n{ctx['requester_email']}\n\n"
                f"Workflow ID:\n{ctx['workflow_id']}"
            ),
        )
    if template_key == "procurement_required":
        return (
            "Procurement Required",
            (
                "Budget has been approved. This request is now awaiting IT procurement.\n\n"
                f"Requester:\n{ctx['requester']}\n\n"
                f"Requester Email:\n{ctx['requester_email']}\n\n"
                f"Request Type:\n{ctx['request_type']}\n\n"
                f"Workflow ID:\n{ctx['workflow_id']}\n\n"
                "Please complete purchase and activation in Derisk360 Workflows."
            ),
        )
    if template_key == "request_completed":
        return (
            "Request Completed",
            (
                "Your request has been completed.\n\n"
                f"Workflow ID:\n{ctx['workflow_id']}\n\n"
                "Subscription/License is now active."
            ),
        )
    if template_key == "procurement_completed":
        return (
            "Procurement Completed",
            (
                "A workflow request has been procured and activated.\n\n"
                f"Workflow ID:\n{ctx['workflow_id']}\n\n"
                f"Requester:\n{ctx['requester']}\n\n"
                f"Request Type:\n{ctx['request_type']}\n\n"
                "The subscription or licence record is now active."
            ),
        )
    if template_key == "info_requested":
        return (
            "More Information Required",
            (
                "More information is required for your workflow request.\n\n"
                f"Workflow ID:\n{ctx['workflow_id']}\n\n"
                f"Request type:\n{ctx['request_type']}\n\n"
                f"Current stage:\n{ctx['current_stage']}"
            ),
        )
    raise ValueError(f"Unknown email template: {template_key}")


def _build_template_email(
    template_key: str,
    request_row: dict[str, Any],
    conn: Connection,
    *,
    to_emails: list[str],
    cc_emails: list[str] | None = None,
) -> dict[str, Any]:
    from app.email_templates import OutboundEmail

    ctx = _workflow_email_context(request_row, conn)
    subject, body = _render_template(template_key, ctx)
    approver = get_current_approver(request_row, conn)
    stage = str(approver.get("stage") or stage_label(str(request_row.get("status") or "")))

    message = OutboundEmail(
        to_emails=to_emails,
        cc_emails=tuple(cc_emails or []),
        subject=subject,
        body=body,
        event_type=template_key,
    )
    return {
        "to": ", ".join(message.to_emails),
        "toEmails": message.to_emails,
        "cc": ", ".join(message.cc_emails),
        "ccEmails": message.cc_emails,
        "subject": message.subject,
        "body": message.body,
        "eventType": message.event_type,
        "templateLabel": EMAIL_TEMPLATE_LABELS.get(template_key, template_key),
        "stage": stage,
        "recipientRole": "approver"
        if template_key in {"request_submitted", "action_required", "procurement_required", "procurement_completed"}
        else "requester",
    }


def _rich_emails_to_dicts(
    rich_emails: list,
    stage: str,
    requester_email: str,
    template_label: str,
    event_type: str,
) -> list[dict[str, Any]]:
    """Convert OutboundEmail objects from the rich builders to the dict format used by _emails_for_event."""
    result = []
    for e in rich_emails:
        to_list = list(e.to_emails or [])
        cc_list = list(e.cc_emails or [])
        is_approver = requester_email not in to_list
        result.append({
            "to": ", ".join(to_list),
            "toEmails": to_list,
            "cc": ", ".join(cc_list),
            "ccEmails": cc_list,
            "subject": e.subject,
            "body": e.body,
            "eventType": e.event_type or event_type,
            "templateLabel": EMAIL_TEMPLATE_LABELS.get(e.event_type or event_type, template_label),
            "stage": stage,
            "recipientRole": "approver" if is_approver else "requester",
        })
    return result


def _emails_for_event(event: str, request_row: dict[str, Any], conn: Connection) -> list[dict[str, Any]]:
    requester_email = _resolve_requester_email(request_row, conn)
    if not requester_email:
        # Fallback to master admin email if requester email is not configured,
        # to ensure the confirmation email is always generated and sent to a responsible party.
        master_admin_emails = _role_emails(conn, "master_admin")
        if master_admin_emails:
            requester_email = master_admin_emails[0]
        else:
            # If no master admin, use a generic placeholder to prevent ValueError
            # and ensure the email generation proceeds. This email will likely not be sent.
            requester_email = "system-notifications@derisk360.com"

    status = str(request_row.get("status") or "").lower()
    messages: list[dict[str, Any]] = []

    if event in {"submitted", "workflow_created", "reopened"}:
        # 1. Employee confirmation email (Email 2 — Request Received)
        messages.append(_build_template_email("workflow_started", request_row, conn, to_emails=[requester_email]))

        # 2. Action-required email for the current approver (Email 6 — Action Required)
        approver = get_current_approver(request_row, conn)
        approver_email = approver.get("email")
        approver_role = approver.get("role")

        # Only send action required if there's an approver and it's not the requester themselves
        if approver_email and approver_role and approver_role != "requester":
            messages.append(_build_template_email("action_required", request_row, conn, to_emails=[approver_email]))

        return messages

    if event == "line_manager_approved":
        from app.email_templates import workflow_line_manager_approved_emails
        rich = workflow_line_manager_approved_emails(request_row, conn)
        return _rich_emails_to_dicts(rich, "line_manager_approved", requester_email, "Request Approved – Line Manager", "line_manager_approved")
    if event in {"master_approved", "approved"}:
        from app.email_templates import workflow_master_approved_emails
        rich = workflow_master_approved_emails(request_row, conn)
        return _rich_emails_to_dicts(rich, "master_approved", requester_email, "Request Approved", "master_approved")

    if event in {"finance_approved"}:
        workflow_type = str(request_row.get("workflow_type") or "")
        if workflow_type == "employee_offboarding":
            from app.email_templates import OutboundEmail
            it_emails = _role_emails(conn, "it_admin")
            recipients = list(dict.fromkeys([requester_email] + it_emails)) if requester_email else it_emails
            employee_name = str(request_row.get("payload", {}).get("offboarded_employee_display_name") or "the employee")
            wf_id = str(request_row.get("id") or "")[:8].upper()
            body = (
                f"IT has confirmed offboarding for {employee_name}.\n\n"
                f"Workflow ID: {wf_id}\n\n"
                "The following actions have been completed:\n"
                "• Employee account deactivated\n"
                "• All licences revoked\n"
                "• Subscription ownership unlinked\n"
                "• Portal login disabled\n\n"
                "The workflow will now proceed to completion."
            )
            return [{
                "to": ", ".join(recipients),
                "toEmails": recipients,
                "cc": "", "ccEmails": [],
                "subject": f"[Derisk360] IT Confirmed Offboarding – {employee_name} #{wf_id}",
                "body": body,
                "eventType": "it_confirmed_offboarding",
                "templateLabel": "IT Confirmed Offboarding",
                "stage": "it_confirmed",
                "recipientRole": "requester",
            }]
        from app.email_templates import workflow_finance_approved_emails
        rich = workflow_finance_approved_emails(request_row, conn)
        return _rich_emails_to_dicts(rich, "finance_approved", requester_email, "Finance Approved", "finance_approved")

    if event in {"rejected", "manager_rejected", "finance_rejected"}:
        from app.email_templates import workflow_rejected_emails
        rich = workflow_rejected_emails(request_row, conn)
        return _rich_emails_to_dicts(rich, "rejected", requester_email, "Request Rejected", "workflow_rejected")

    if event in {"completed", "procurement_complete", "license_assigned"} and str(request_row.get("workflow_type") or "") == "employee_offboarding":
        it_emails = _role_emails(conn, "it_admin")
        hr_emails = _role_emails(conn, "hr_admin")
        master_emails = _role_emails(conn, "master_admin")
        recipients = list(dict.fromkeys(([requester_email] if requester_email else []) + it_emails + hr_emails + master_emails))
        payload = request_row.get("payload") or {}
        if isinstance(payload, str):
            import json as _json
            try: payload = _json.loads(payload)
            except Exception: payload = {}
        employee_name = str(payload.get("offboarded_employee_display_name") or payload.get("offboarded_employee_name") or "the employee")
        employee_email = str(payload.get("offboarded_employee_email") or "—")
        wf_id = str(request_row.get("id") or "")[:8].upper()
        body = (
            f"The offboarding of {employee_name} ({employee_email}) has been fully completed.\n\n"
            f"Workflow ID: {wf_id}\n\n"
            "Summary of actions completed:\n"
            "• Employee record deactivated\n"
            "• All software licences revoked\n"
            "• Subscription ownership unlinked\n"
            "• Portal login and roles disabled\n\n"
            "No further action is required. This workflow is now closed."
        )
        return [{
            "to": ", ".join(recipients),
            "toEmails": recipients,
            "cc": "", "ccEmails": [],
            "subject": f"[Derisk360] Offboarding Completed – {employee_name} #{wf_id}",
            "body": body,
            "eventType": "offboarding_completed",
            "templateLabel": "Offboarding Completed",
            "stage": "completed",
            "recipientRole": "requester",
        }]

    if event in {"completed", "procurement_complete", "license_assigned"}:
        # Use the rich completion email builder for the requester — it includes
        # subscription name, activation date, credentials, and access details.
        from app.email_templates import OutboundEmail, workflow_completed_emails
        rich_emails = workflow_completed_emails(request_row, conn)
        # Include: bulk completion email (contains requester) + employee welcome email (separate recipient)
        outgoing = [e for e in rich_emails if requester_email in (e.to_emails or []) or e.event_type == "workflow_completed_employee"]
        if not outgoing and rich_emails:
            # Fallback: re-address first rich email to requester if not already in list
            first = rich_emails[0]
            outgoing = [OutboundEmail(
                to_emails=[requester_email],
                subject=first.subject,
                body=first.body,
                event_type="request_completed",
            )]
        for e in outgoing:
            messages.append({
                "to": ", ".join(e.to_emails),
                "toEmails": list(e.to_emails),
                "cc": "",
                "ccEmails": [],
                "subject": e.subject,
                "body": e.body,
                "eventType": e.event_type or "request_completed",
                "templateLabel": EMAIL_TEMPLATE_LABELS.get("request_completed", "Request Completed"),
                "stage": "completed",
                "recipientRole": "requester" if requester_email in (e.to_emails or []) else "employee",
            })
        return messages

    if event in {"info_requested", "request_more_information"}:
        info_message = str(request_row.get("info_request_message") or "Please provide additional details.")
        ctx = _workflow_email_context(request_row, conn)
        messages.append(
            _build_template_email(
                "info_requested",
                request_row,
                conn,
                to_emails=[requester_email],
            )
        )
        messages[-1]["body"] = (
            "More information is required for your workflow request.\n\n"
            f"Workflow ID:\n{ctx['workflow_id']}\n\n"
            f"Request type:\n{ctx['request_type']}\n\n"
            f"Message from reviewer:\n{info_message}\n\n"
            "Please update your request details and resubmit when ready."
        )
        messages[-1]["subject"] = "More information required for your request"
        return messages

    raise ValueError(f"Unsupported workflow email event: {event}")


def build_workflow_emails(
    event: str,
    request_row: dict[str, Any],
    conn: Connection,
) -> list[dict[str, Any]]:
    """Build workflow-generated emails for a lifecycle event."""
    return _emails_for_event(event, request_row, conn)


def preview_workflow_emails(
    request_row: dict[str, Any],
    event: str,
    conn: Connection,
) -> list[dict[str, Any]]:
    return build_workflow_emails(event, request_row, conn)
