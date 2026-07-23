"""Role-based permissions for subscription, licence, and tool request submissions."""

from __future__ import annotations

SOFTWARE_SUBMISSION_PERMISSIONS: dict[str, dict[str, set[str]]] = {
    "employee": {
        "subscriptions": {"employee_software_request"},
        "licences": set(),
    },
    "it_admin": {
        "subscriptions": {
            "new_subscription_request",
            "renewal_request",
        },
        "licences": {
            "license_assignment_request",
        },
    },
    "hr_admin": {
        "subscriptions": set(),
        "licences": {"license_assignment_request", "hr_onboarding_request"},
    },
}

TOOL_REQUEST_SUBMIT_ROLES = {"employee", "line_manager", "it_admin", "hr_admin", "master_admin"}
TOOL_REQUEST_INBOX_ROLES = {"it_admin", "master_admin"}


def _normalize_roles(actor_roles: list[str] | None) -> list[str]:
    if not actor_roles:
        return []
    return [str(role).strip() for role in actor_roles if str(role).strip()]


def validate_software_workflow_submission(
    actor_roles: list[str] | None,
    requested_module: str,
    workflow_type: str,
) -> None:
    if requested_module not in {"subscriptions", "licences"}:
        return

    roles = _normalize_roles(actor_roles)
    if "master_admin" in roles:
        return

    allowed: set[str] = set()
    for role in roles:
        module_permissions = SOFTWARE_SUBMISSION_PERMISSIONS.get(role, {})
        allowed.update(module_permissions.get(requested_module, set()))

    if not allowed:
        raise PermissionError(
            "You do not have permission to perform this action. "
            "Your role is not allowed to submit this request type."
        )

    if workflow_type not in allowed:
        raise PermissionError(
            f"You do not have permission to submit workflow type '{workflow_type}' on module '{requested_module}'."
        )


def validate_direct_software_create(actor_roles: list[str] | None, module: str) -> None:
    if module not in {"subscriptions", "licences"}:
        return
    roles = _normalize_roles(actor_roles)
    if "master_admin" not in roles:
        raise PermissionError(
            "You do not have permission to perform this action. "
            "Direct subscription and licence creation is restricted to Master Admin. Submit a workflow request instead."
        )


def validate_tool_request_submission(actor_roles: list[str] | None) -> None:
    roles = set(_normalize_roles(actor_roles))
    if not roles.intersection(TOOL_REQUEST_SUBMIT_ROLES):
        raise PermissionError(
            "You do not have permission to perform this action. "
            "Only employees and authorized staff may submit software requests."
        )


def validate_tool_request_inbox_action(actor_roles: list[str] | None) -> None:
    roles = set(_normalize_roles(actor_roles))
    if not roles.intersection(TOOL_REQUEST_INBOX_ROLES):
        raise PermissionError(
            "You do not have permission to perform this action. "
            "Only IT Admin and Master Admin may manage the tool request inbox."
        )


def validate_workflow_reject(actor_roles: list[str] | None, current_status: str) -> None:
    roles = set(_normalize_roles(actor_roles))
    status = str(current_status or "").lower()
    if "master_admin" in roles:
        return
    if "finance" in roles and status in {"line_manager_approved", "master_approved", "finance_approved", "submitted", "reopened"}:
        return
    if "line_manager" in roles and status in {"submitted", "reopened"}:
        return
    raise PermissionError("You do not have permission to reject this workflow at the current stage.")


def validate_workflow_reopen(actor_roles: list[str] | None) -> None:
    roles = set(_normalize_roles(actor_roles))
    if roles.intersection({"master_admin", "finance", "it_admin"}):
        return
    raise PermissionError("You do not have permission to reopen this workflow.")


def validate_workflow_request_info(actor_roles: list[str] | None, current_status: str) -> None:
    roles = set(_normalize_roles(actor_roles))
    status = str(current_status or "").lower()
    if "master_admin" in roles:
        return
    if "finance" in roles and status in {"line_manager_approved", "master_approved", "submitted", "reopened"}:
        return
    if "line_manager" in roles and status in {"submitted", "reopened"}:
        return
    if "it_admin" in roles and status in {"finance_approved", "submitted", "reopened"}:
        return
    raise PermissionError("You do not have permission to request more information at this stage.")


def validate_workflow_resubmit(actor_roles: list[str] | None, actor_email: str | None, request_row: dict) -> None:
    roles = set(_normalize_roles(actor_roles))
    status = str(request_row.get("status") or "").lower()
    if status != "info_requested":
        raise PermissionError("Only workflows awaiting more information can be resubmitted.")
    requester_email = str(request_row.get("requested_by_email") or "").strip().lower()
    actor = str(actor_email or "").strip().lower()
    if "master_admin" in roles:
        return
    if actor and requester_email and actor == requester_email:
        return
    raise PermissionError("You do not have permission to resubmit this workflow.")


def validate_workflow_procurement_complete(actor_roles: list[str] | None) -> None:
    roles = set(_normalize_roles(actor_roles))
    if roles.intersection({"master_admin", "it_admin"}):
        return
    raise PermissionError(
        "You do not have permission to perform this action. "
        "Only IT Admin or Master Admin can complete procurement and activation."
    )
