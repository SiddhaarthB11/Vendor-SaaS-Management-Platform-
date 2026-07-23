"""
Diagnostic test for the employee offboarding workflow.

Tests the full flow end-to-end against the live API:
  1. HR submits an offboarding request for a temporary test employee
  2. IT confirms licences have been checked  (validate-budget step)
  3. IT completes the offboarding            (complete step)
  4. DB state is verified: employee inactive, licence revoked

Run from the project root:
    python api/scripts/diag_offboarding.py

Creates its own temporary employee and cleans up after itself.
"""

from __future__ import annotations

import json
import sys
import traceback
import urllib.error
import urllib.request
import uuid

# ---------------------------------------------------------------------------
# Config — must match infra/.env / seed data
# ---------------------------------------------------------------------------
BASE_URL    = "http://localhost:8000"
HR_EMAIL      = "deriskHR@outlook.com"
HR_PASSWORD   = "admin@123"
IT_EMAIL      = "deriskit@outlook.com"
IT_PASSWORD   = "admin@123"
ADMIN_EMAIL   = "deriskmaster@outlook.com"
ADMIN_PASSWORD = "admin@123"

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[----]"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _request(method: str, path: str, body: dict | None = None, actor: dict | None = None) -> dict:
    """Make an API call. Actor fields are merged into the body (how this API handles auth)."""
    url = f"{BASE_URL}{path}"
    merged: dict = {}
    if actor:
        merged.update(actor)
    if body:
        merged.update(body)
    data = json.dumps(merged).encode() if merged else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw).get("detail", raw.decode())
        except Exception:
            detail = raw.decode()
        raise RuntimeError(f"HTTP {exc.code} {method} {path}: {detail}") from None


def _login(email: str, password: str) -> dict:
    """Login and return an actor dict ready to merge into request bodies."""
    resp = _request("POST", "/login", {"username": email, "password": password})
    if "user" not in resp:
        raise RuntimeError(f"Login failed for {email}: {resp}")
    u = resp["user"]
    return {"actor_user_id": u["id"], "actor_email": u["email"], "actor_roles": u.get("roles", [])}


# ---------------------------------------------------------------------------
# DB helper (runs psql inside the db container)
# ---------------------------------------------------------------------------

def _db_scalar(sql: str) -> str:
    import subprocess
    cmd = ["docker", "exec", "-i", "slmct-db", "psql", "-U", "slmct_user", "-d", "slmct", "-c", sql, "-t", "--csv"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"DB error: {result.stderr.strip()}")
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def step(label: str):
    print(f"\n{INFO} {label}")


def ok(msg: str):
    print(f"   {PASS} {msg}")


def fail(msg: str):
    print(f"   {FAIL} {msg}")
    raise SystemExit(1)


def assert_eq(label: str, got: str, expected: str):
    if got == expected:
        ok(f"{label}: {got!r}")
    else:
        fail(f"{label}: expected {expected!r}, got {got!r}")


# ---------------------------------------------------------------------------
# Test state
# ---------------------------------------------------------------------------

class _State:
    hr:              dict = None
    it:              dict = None
    admin:           dict = None
    org_id:          str  = ""
    employee_id:     str  = ""
    employee_email:  str  = ""
    subscription_id: str  = ""
    licence_id:      str  = ""
    workflow_id:     str  = ""

state = _State()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup():
    if not state.workflow_id and not state.employee_id:
        return
    step("Cleaning up test data")
    try:
        if state.workflow_id:
            _db_scalar(f"DELETE FROM slmct.workflow_status_history WHERE workflow_request_id = '{state.workflow_id}';")
            _db_scalar(f"DELETE FROM slmct.workflow_requests WHERE id = '{state.workflow_id}';")
            ok(f"Deleted workflow {state.workflow_id[:8]}...")
        if state.licence_id:
            _db_scalar(f"DELETE FROM slmct.licences WHERE id = '{state.licence_id}';")
            ok(f"Deleted licence {state.licence_id[:8]}...")
        if state.subscription_id:
            _db_scalar(f"DELETE FROM slmct.subscriptions WHERE id = '{state.subscription_id}';")
            ok(f"Deleted subscription {state.subscription_id[:8]}...")
        if state.employee_id:
            _db_scalar(f"DELETE FROM slmct.people WHERE id = '{state.employee_id}';")
            ok(f"Deleted test employee {state.employee_email}")
    except Exception as exc:
        print(f"   {FAIL} Cleanup error (manual cleanup may be needed): {exc}")


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

def run():
    print("\n" + "=" * 60)
    print("  SLMCT - Employee Offboarding Diagnostic")
    print("=" * 60)

    # 1. Login
    step("Logging in as HR Admin")
    try:
        state.hr = _login(HR_EMAIL, HR_PASSWORD)
        ok(f"HR actor: {state.hr['actor_email']}")
    except Exception as exc:
        fail(str(exc))

    step("Logging in as IT Admin")
    try:
        state.it = _login(IT_EMAIL, IT_PASSWORD)
        ok(f"IT actor: {state.it['actor_email']}")
    except Exception as exc:
        fail(str(exc))

    step("Logging in as Master Admin")
    try:
        state.admin = _login(ADMIN_EMAIL, ADMIN_PASSWORD)
        ok(f"Admin actor: {state.admin['actor_email']}")
    except Exception as exc:
        fail(str(exc))

    # 2. Resolve org
    step("Resolving organisation")
    try:
        orgs = _request("GET", "/organisations", actor=state.hr)
        org = next((o for o in orgs if not o.get("is_sister_entity")), orgs[0])
        state.org_id = org["id"]
        ok(f"Organisation: {org.get('name')} ({state.org_id[:8]}...)")
    except Exception as exc:
        fail(str(exc))

    # 3. Create temporary test employee
    tag = uuid.uuid4().hex[:6]
    state.employee_email = f"diag.offboard.{tag}@derisk360.test"
    step(f"Creating test employee ({state.employee_email})")
    try:
        emp = _request("POST", "/employees", {
            "full_name": f"Diag Offboard {tag}",
            "work_email": state.employee_email,
            "job_title": "Test Role",
            "department": "QA",
            "employee_number": f"DIAG-{tag.upper()}",
            "status": "active",
            "organisation_id": state.org_id,
        }, actor=state.hr)
        state.employee_id = emp["id"]
        ok(f"Employee created: {emp.get('full_name')} ({state.employee_id[:8]}...)")
    except Exception as exc:
        fail(str(exc))

    # 4. Create subscription + assign licence to the employee (requires master_admin)
    step("Creating test subscription")
    try:
        vendors = _request("GET", "/vendors", actor=state.admin)
        if not vendors:
            fail("No vendors found -- seed demo data first")
        vendor_id = vendors[0]["id"]
        sub = _request("POST", "/subscriptions", {
            "name": f"DiagTest Suite {tag}",
            "vendor_id": vendor_id,
            "organisation_id": state.org_id,
            "status": "active",
            "amount": 100,
            "currency_code": "AED",
            "billing_cycle": "monthly",
        }, actor=state.admin)
        state.subscription_id = sub["id"]
        ok(f"Subscription: {sub['name']} ({state.subscription_id[:8]}...)")
    except Exception as exc:
        fail(str(exc))

    step("Assigning licence to test employee")
    try:
        lic = _request("POST", "/licences", {
            "subscription_id": state.subscription_id,
            "organisation_id": state.org_id,
            "assigned_to_person_id": state.employee_id,
            "licence_name": f"DiagTest Seat {tag}",
            "status": "assigned",
        }, actor=state.admin)
        state.licence_id = lic["id"]
        ok(f"Licence assigned: {lic['licence_name']} ({state.licence_id[:8]}...)")
    except Exception as exc:
        fail(str(exc))

    # 5. HR submits offboarding workflow
    step("HR submits offboarding workflow request")
    try:
        wf = _request("POST", "/workflow-requests", {
            "workflow_type": "employee_offboarding",
            "requested_module": "employees",
            "organisation_id": state.org_id,
            "payload": {
                "offboarded_employee_name": state.employee_id,
                "offboarded_employee_display_name": f"Diag Offboard {tag}",
                "offboarded_employee_email": state.employee_email,
                "last_working_date": "2026-07-31",
                "notes": "Diagnostic test offboarding -- automated",
            },
        }, actor=state.hr)
        state.workflow_id = wf["id"]
        assert_eq("Workflow status after submission", wf["status"], "submitted")
        ok(f"Workflow ID: {state.workflow_id[:8]}...")
    except SystemExit:
        raise
    except Exception as exc:
        fail(str(exc))

    # 6. IT confirms licences checked
    step("IT confirms licences checked (validate-budget)")
    try:
        resp = _request(
            "POST",
            f"/workflow-requests/{state.workflow_id}/validate-budget",
            {"notes": "Diagnostic: IT confirmed all licences reviewed"},
            actor=state.it,
        )
        assert_eq("Workflow status after IT confirm", resp["status"], "it_confirmed")
    except SystemExit:
        raise
    except Exception as exc:
        fail(str(exc))

    # 7. IT completes offboarding
    step("IT completes offboarding")
    try:
        resp = _request(
            "POST",
            f"/workflow-requests/{state.workflow_id}/complete",
            {"offboarded_employee_email": state.employee_email},
            actor=state.it,
        )
        assert_eq("Workflow status after completion", resp["status"], "completed")
    except SystemExit:
        raise
    except Exception as exc:
        fail(str(exc))

    # 8. Verify DB state
    step("Verifying database state")

    people_status = _db_scalar(
        f"SELECT status FROM slmct.people WHERE id = '{state.employee_id}';"
    )
    assert_eq("Employee (people) status", people_status, "inactive")

    licence_status = _db_scalar(
        f"SELECT status FROM slmct.licences WHERE id = '{state.licence_id}';"
    )
    assert_eq("Licence status", licence_status, "revoked")

    wf_status = _db_scalar(
        f"SELECT status FROM slmct.workflow_requests WHERE id = '{state.workflow_id}';"
    )
    assert_eq("Workflow request status", wf_status, "completed")

    print(f"\n{'=' * 60}")
    print(f"  {PASS} All offboarding diagnostic checks passed")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        sys.exit(1)
    except Exception as exc:
        print(f"\n{FAIL} Unexpected error: {exc}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        cleanup()
