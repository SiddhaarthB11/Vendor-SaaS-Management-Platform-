"""Workflow role mailboxes loaded from environment variables."""

from __future__ import annotations

from app.test_users import workflow_role_mailboxes


def configured_role_mailboxes() -> list[dict]:
    return workflow_role_mailboxes()
