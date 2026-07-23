"""Workflow test users loaded from environment variables — no hardcoded credentials."""

from __future__ import annotations

import os

from app.security import hash_password


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _password_hash(env_key: str) -> str | None:
    password = _env(env_key)
    if not password:
        return None
    return hash_password(password)


TEST_USER_DEFINITIONS = [
    {
        "key": "employee_1",
        "login": "deriskemployee1",
        "role": "employee",
        "label": "Employee One",
        "employee_number": "EMP-0001",
        "department": "Software Engineering",
        "job_title": "Software Engineer",
        "email_env": "APP_USER_EMAIL_EMPLOYEE_1",
        "password_env": "APP_USER_PASSWORD_EMPLOYEE_1",
        "receives": "Own request status updates at every workflow stage",
    },
    {
        "key": "employee_2",
        "login": "deriskemployee2",
        "role": "employee",
        "label": "Employee Two",
        "employee_number": "EMP-0002",
        "department": "Marketing",
        "job_title": "Marketing Specialist",
        "email_env": "APP_USER_EMAIL_EMPLOYEE_2",
        "password_env": "APP_USER_PASSWORD_EMPLOYEE_2",
        "receives": "Own request status updates at every workflow stage",
    },
    {
        "key": "finance",
        "login": "deriskfinance",
        "role": "finance",
        "label": "Finance Manager",
        "employee_number": "FIN-0001",
        "department": "Finance & Accounts",
        "job_title": "Finance Manager",
        "email_env": "APP_USER_EMAIL_FINANCE",
        "password_env": "APP_USER_PASSWORD_FINANCE",
        "receives": "Budget validation, procurement, workflow completion",
    },
    {
        "key": "master",
        "login": "deriskmaster",
        "role": "master_admin",
        "label": "Master Admin",
        "employee_number": "ADM-0001",
        "department": "Platform",
        "job_title": "Master Admin",
        "email_env": "APP_USER_EMAIL_ADMIN",
        "password_env": "APP_USER_PASSWORD_ADMIN",
        "receives": "Workflow approvals, completions, overrides",
    },
    {
        "key": "it",
        "login": "deriskit",
        "role": "it_admin",
        "label": "IT Administrator",
        "employee_number": "IT-0001",
        "department": "IT",
        "job_title": "IT Administrator",
        "email_env": "APP_USER_EMAIL_IT",
        "password_env": "APP_USER_PASSWORD_IT",
        "receives": "Tool requests, procurement completion, license assignment",
    },
    {
        "key": "line_manager",
        "login": "deriskline",
        "role": "line_manager",
        "label": "Line Manager",
        "employee_number": "MGR-0001",
        "department": "Operations",
        "job_title": "Line Manager",
        "email_env": "APP_USER_EMAIL_MANAGER",
        "password_env": "APP_USER_PASSWORD_MANAGER",
        "receives": "Employee software request approvals",
    },
]


def configured_test_users() -> list[dict]:
    users: list[dict] = []
    for definition in TEST_USER_DEFINITIONS:
        email = _env(definition["email_env"])
        if not email:
            continue
        password_hash = _password_hash(definition["password_env"])
        users.append(
            {
                **definition,
                "email": email,
                "password_hash": password_hash,
                "password_configured": password_hash is not None,
            }
        )
    return users


def workflow_role_mailboxes() -> list[dict]:
    return [
        {
            "login": user["login"],
            "role": user["role"],
            "label": user["label"],
            "email": user["email"],
            "receives": user["receives"],
        }
        for user in configured_test_users()
    ]
