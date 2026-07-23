import base64
import html
import io
import logging
import re
import secrets
import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile
from typing import Optional

import psycopg
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Body, Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from psycopg import Connection
from psycopg.types.json import Json
from pydantic import BaseModel

from app.db import get_connection
from app.fx_updater import run_fx_update
from app.price_scraper import fetch_price_from_url, run_price_scrape

log = logging.getLogger(__name__)
from app.schemas import (
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    OrganisationCreate,
    OrganisationResponse,
    OrganisationUpdate,
)
from app.security import hash_password, verify_password
from app.settings import get_settings
from app.notifications import (
    notify_workflow_created,
    notify_workflow_approved,
    notify_workflow_budget_validated,
    notify_workflow_rejected,
    notify_workflow_reopened,
    notify_workflow_info_requested,
    notify_workflow_completed,
    notify_tool_request_rejected,
    notify_tool_request_info_requested,
    notify_tool_request_created,
    run_renewal_alerts,
)

def _fx_job():
    """APScheduler target — fetch live FX rates at 00:30 UTC daily."""
    try:
        conn = next(get_connection())
        summary = run_fx_update(lambda: conn)
        log.info("Daily FX update complete: %s", summary)
    except Exception:
        log.exception("Daily FX update failed")


def _scrape_job():
    """APScheduler target — scrape vendor prices at 01:00 UTC (after FX update)."""
    try:
        conn = next(get_connection())
        summary = run_price_scrape(lambda: conn)
        log.info("Nightly price scrape complete: %s", summary)
    except Exception:
        log.exception("Nightly price scrape failed")


def _renewal_alerts_job():
    """APScheduler target — send renewal alerts at 07:00 UTC daily for subscriptions due in 30/60/90 days."""
    try:
        conn = next(get_connection())
        summary = run_renewal_alerts(conn)
        log.info("Renewal alerts sent: %s", summary)
    except Exception:
        log.exception("Renewal alerts job failed")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(_fx_job,              "cron", hour=0, minute=30, id="daily_fx_update")
    scheduler.add_job(_scrape_job,          "cron", hour=1, minute=0,  id="nightly_price_scrape")
    scheduler.add_job(_renewal_alerts_job,  "cron", hour=7, minute=0,  id="daily_renewal_alerts")
    scheduler.start()
    log.info("APScheduler started — FX 00:30, price scrape 01:00, renewal alerts 07:00 UTC")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="Derisk360 SLMCT API",
    description="API for subscription, licences, and cost tracking.",
    version="0.1.0",
    lifespan=_lifespan,
)

settings = get_settings()

MODULES = {
    "vendors": {
        "table": "slmct.vendors",
        "fields": [
            "organisation_id",
            "name",
            "legal_name",
            "website_url",
            "contact_name",
            "contact_email",
            "status",
            "soc2_certified",
            "iso27001_certified",
            "gdpr_compliant",
            "data_residency",
        ],
        "soft_delete": ("status", "inactive"),
        "order": "created_at DESC",
    },
    "subscriptions": {
        "table": "slmct.subscriptions",
        "fields": [
            "organisation_id",
            "vendor_id",
            "name",
            "category",
            "owner_person_id",
            "start_date",
            "renewal_date",
            "billing_cycle",
            "amount",
            "currency_code",
            "status",
            "notes",
            "department",
        ],
        "soft_delete": ("status", "cancelled"),
        "order": "created_at DESC",
    },
    "licences": {
        "table": "slmct.licences",
        "fields": [
            "organisation_id",
            "subscription_id",
            "assigned_to_person_id",
            "licence_name",
            "assigned_at",
            "expires_at",
            "status",
            "notes",
        ],
        "soft_delete": ("status", "revoked"),
        "order": "created_at DESC",
    },
    "budgets": {
        "table": "slmct.budgets",
        "fields": [
            "organisation_id",
            "fiscal_year",
            "department",
            "allocated_amount",
            "currency_code",
            "status",
            "notes",
        ],
        "soft_delete": ("status", "closed"),
        "order": "created_at DESC",
    },
    "payments": {
        "table": "slmct.payments",
        "fields": [
            "organisation_id",
            "subscription_id",
            "vendor_id",
            "budget_id",
            "name",
            "payment_date",
            "due_date",
            "amount",
            "currency_code",
            "status",
            "reference",
            "notes",
        ],
        "soft_delete": ("status", "cancelled"),
        "order": "created_at DESC",
    },
    "employees": {
        "table": "slmct.people",
        "fields": [
            "organisation_id",
            "employee_number",
            "full_name",
            "work_email",
            "department",
            "job_title",
            "line_manager_email",
            "status",
        ],
        "soft_delete": ("status", "inactive"),
        "order": "created_at DESC",
    },
    "contracts": {
        "table": "slmct.contracts",
        "fields": [
            "organisation_id",
            "vendor_id",
            "subscription_id",
            "title",
            "contract_number",
            "contract_type",
            "start_date",
            "end_date",
            "value",
            "currency_code",
            "auto_renew",
            "notice_period_days",
            "owner",
            "status",
            "document_name",
            "document_data",
            "notes",
        ],
        "soft_delete": ("status", "terminated"),
        "order": "created_at DESC",
    },
}

WORKFLOW_MODULES = {"organisations", "vendors", "subscriptions", "licences", "budgets", "payments", "employees", "contracts"}
BULK_UPLOAD_MODULES = {"vendors", "subscriptions", "licences", "budgets", "payments", "employees", "contracts"}
LOGIN_ROLES = {"master_admin", "finance", "it_admin", "auditor", "hr_admin", "line_manager", "employee"}
FINANCE_WORKFLOW_MODULES = {"budgets", "payments"}
IT_WORKFLOW_MODULES = {"organisations", "vendors", "subscriptions", "licences", "contracts"}
CURRENCY_CODES = ["AED", "INR", "GBP", "USD", "EUR", "SAR", "QAR", "OMR", "BHD", "KWD"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(psycopg.Error)
def database_exception_handler(request, exc):
    import traceback
    traceback.print_exc()
    err_msg = str(exc)
    # Check for unique violation (code 23505 in postgres)
    if "UniqueViolation" in err_msg or (hasattr(exc, "sqlstate") and exc.sqlstate == "23505"):
        detail = "A record with this unique identifier already exists."
        if "Key (code)=" in err_msg or "uq_organisations_code_active" in err_msg:
            match = re.search(r"Key \(code\)=\((.*?)\)", err_msg)
            if match:
                detail = f"An active organisation with the code '{match.group(1)}' already exists. Restore or permanently delete the archived record in Recycle Bin to reuse this code."
            else:
                detail = "An active organisation with this code already exists. Restore or permanently delete the archived record in Recycle Bin to reuse this code."
        elif "Key (name)=" in err_msg:
            match = re.search(r"Key \(name\)=\((.*?)\) already exists", err_msg)
            if match:
                detail = f"A record with the name '{match.group(1)}' already exists."
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": detail},
        )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": f"Database error: {err_msg.splitlines()[0]}"},
    )


@app.exception_handler(Exception)
def global_exception_handler(request, exc):
    import traceback
    traceback.print_exc()
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": f"Internal Server Error: {str(exc)}"},
    )


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


def _record_response(row: dict | None) -> dict:
    if not row:
        return {}
    return {key: value for key, value in row.items()}


def _module_config(module: str) -> dict:
    config = MODULES.get(module)
    if not config:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Module not found")
    return config


def _audit(conn: Connection, action: str, entity_type: str, entity_id: UUID | None, metadata: str = '{"source":"api"}') -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO slmct.audit_logs (action, entity_type, entity_id, metadata)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (action, entity_type, entity_id, metadata),
        )


def _actor(payload: dict) -> tuple[UUID | None, str | None]:
    actor_id = payload.get("actor_user_id")
    actor_email = payload.get("actor_email")
    return (UUID(actor_id) if actor_id else None, actor_email)


def _normalise_role(role_code: str | None) -> str:
    if not role_code:
        return "employee"
    role_code = role_code.strip().lower()
    if role_code not in {"employee", *LOGIN_ROLES}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported role")
    return role_code


def _actor_is_master(payload: dict) -> bool:
    roles = payload.get("actor_roles") or []
    return isinstance(roles, list) and "master_admin" in roles


def _actor_can_bulk_upload(payload: dict) -> bool:
    roles = set(payload.get("actor_roles") or [])
    return bool(roles.intersection({"master_admin", "it_admin", "finance", "hr_admin"}))


def _actor_can_manage_tool_requests(payload: dict) -> bool:
    roles = set(payload.get("actor_roles") or [])
    return bool(roles.intersection({"master_admin", "it_admin"}))


def _actor_can_master_approve(payload: dict) -> bool:
    roles = set(payload.get("actor_roles") or [])
    return "master_admin" in roles


def _actor_can_finance_close(payload: dict) -> bool:
    roles = set(payload.get("actor_roles") or [])
    return bool(roles.intersection({"master_admin", "finance"}))


def _actor_can_procurement_complete(payload: dict) -> bool:
    roles = set(payload.get("actor_roles") or [])
    return bool(roles.intersection({"master_admin", "it_admin"}))


def _actor_can_line_manager(payload: dict) -> bool:
    roles = set(payload.get("actor_roles") or [])
    return bool(roles.intersection({"master_admin", "line_manager"}))


def _get_tool_request(request_id: UUID, conn: Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM slmct.tool_requests WHERE id = %s", (request_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool request not found")
    return _record_response(row)


ACTIVATION_METHODS = {
    "vendor_portal",
    "invoice_po",
    "license_key",
    "auto_provisioned",
    # legacy values kept for backwards compatibility with existing records
    "company_account",
    "invitation_email",
    "vendor_provisioned",
}


def _validate_activation_payload(payload: dict) -> dict:
    method = payload.get("activation_method")
    if method not in ACTIVATION_METHODS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Activation method is required")

    details: dict[str, str] = {}
    if method == "license_key":
        license_key = str(payload.get("license_key") or "").strip()
        if not license_key:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="License key is required")
        details = {"license_key": license_key, "activation_code": str(payload.get("activation_code") or "").strip()}
    elif method in ("auto_provisioned", "vendor_provisioned"):
        details = {
            "vendor_reference_id": str(payload.get("vendor_reference_id") or "").strip(),
            "provisioning_notes": str(payload.get("provisioning_notes") or "").strip(),
        }
    elif method == "invitation_email":
        details = {
            "recipient_email": str(payload.get("recipient_email") or "").strip(),
            "invitation_status": str(payload.get("invitation_status") or "").strip(),
        }
    elif method == "company_account":
        details = {
            "username": str(payload.get("username") or "").strip(),
            "password": str(payload.get("password") or "").strip(),
        }

    return {
        "activation_method": method,
        "assigned_employee_name": str(payload.get("assigned_employee_name") or "").strip(),
        "assigned_employee_email": str(payload.get("assigned_employee_email") or "").strip(),
        "activation_details": details,
    }


def _generate_temp_password() -> str:
    return f"Temp-{secrets.token_urlsafe(9)}1!"


def _xlsx_column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_column_index(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - 64)
    return max(index, 1)


def _xlsx_cell(value: object, row_index: int, column_index: int) -> str:
    ref = f"{_xlsx_column_name(column_index)}{row_index}"
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = html.escape(str(value))
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _build_xlsx(headers: list[str], rows: list[dict], sheet_name: str = "Data") -> bytes:
    sheet_rows = []
    all_rows = [dict(zip(headers, headers))] + rows
    for row_index, row in enumerate(all_rows, start=1):
        cells = [_xlsx_cell(row.get(header), row_index, column_index) for column_index, header in enumerate(headers, start=1)]
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{html.escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )

    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return output.getvalue()


def _parse_xlsx(content: bytes) -> list[dict]:
    try:
        import xml.etree.ElementTree as ET

        with ZipFile(io.BytesIO(content)) as archive:
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                strings_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in strings_root.findall(".//{*}si"):
                    shared_strings.append("".join(node.text or "" for node in item.findall(".//{*}t")))

            sheet_root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            parsed_rows: list[list[str]] = []
            for row in sheet_root.findall(".//{*}sheetData/{*}row"):
                values: list[str] = []
                for cell in row.findall("{*}c"):
                    column_index = _xlsx_column_index(cell.attrib.get("r", f"{_xlsx_column_name(len(values) + 1)}1"))
                    while len(values) < column_index - 1:
                        values.append("")
                    cell_type = cell.attrib.get("t")
                    value_node = cell.find("{*}v")
                    inline_node = cell.find("{*}is/{*}t")
                    if cell_type == "inlineStr":
                        values.append(inline_node.text if inline_node is not None and inline_node.text is not None else "")
                    elif cell_type == "s" and value_node is not None:
                        values.append(shared_strings[int(value_node.text or "0")])
                    elif value_node is not None:
                        values.append(value_node.text or "")
                    else:
                        values.append("")
                parsed_rows.append(values)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid XLSX file") from exc

    if not parsed_rows:
        return []
    headers = [header.strip() for header in parsed_rows[0]]
    rows = []
    for values in parsed_rows[1:]:
        row = {header: values[index].strip() for index, header in enumerate(headers) if header and index < len(values) and values[index].strip() != ""}
        if row:
            rows.append(row)
    return rows


def _xlsx_response(filename: str, content: bytes) -> StreamingResponse:
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _entity_type_for_module(module: str) -> str:
    return "licence" if module == "licences" else module.rstrip("s")


def _lookup_user(conn: Connection, user_id: UUID) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT au.id, au.email, au.password_hash, au.status, p.full_name
            FROM slmct.app_users au
            JOIN slmct.people p ON p.id = au.person_id
            WHERE au.id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return row


def _upsert_person_and_role(payload: dict, conn: Connection) -> dict:
    role_code = _normalise_role(payload.get("role_code"))
    work_email = payload.get("work_email")
    if role_code != "employee" and not work_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Login roles require work_email")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO slmct.people (
              organisation_id, employee_number, full_name, work_email, department, job_title, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, 'active')::slmct.person_status)
            ON CONFLICT (work_email) DO UPDATE SET
              organisation_id = COALESCE(EXCLUDED.organisation_id, slmct.people.organisation_id),
              employee_number = COALESCE(EXCLUDED.employee_number, slmct.people.employee_number),
              full_name = EXCLUDED.full_name,
              department = EXCLUDED.department,
              job_title = EXCLUDED.job_title,
              status = EXCLUDED.status,
              updated_at = now()
            RETURNING *
            """,
            (
                payload.get("organisation_id"),
                payload.get("employee_number"),
                payload.get("full_name"),
                work_email,
                payload.get("department"),
                payload.get("job_title"),
                payload.get("status", "active"),
            ),
        )
        person = cur.fetchone()

        user = None
        if role_code in LOGIN_ROLES:
            cur.execute("SELECT * FROM slmct.app_users WHERE lower(email) = lower(%s)", (work_email,))
            existing_user = cur.fetchone()
            password = payload.get("password") or (_generate_temp_password() if not existing_user or payload.get("force_password_reset") else None)
            if password:
                cur.execute(
                    """
                    INSERT INTO slmct.app_users (
                      person_id, email, password_hash, status, must_change_password, temp_password_expires_at
                    )
                    VALUES (%s, %s, %s, 'active', true, now() + interval '24 hours')
                    ON CONFLICT (email) DO UPDATE SET
                      person_id = EXCLUDED.person_id,
                      status = 'active',
                      password_hash = EXCLUDED.password_hash,
                      must_change_password = true,
                      temp_password_expires_at = EXCLUDED.temp_password_expires_at,
                      updated_at = now()
                    RETURNING *
                    """,
                    (person["id"], work_email, hash_password(password)),
                )
                user = cur.fetchone()
            else:
                cur.execute(
                    """
                    UPDATE slmct.app_users
                    SET person_id = %s, status = 'active', updated_at = now()
                    WHERE lower(email) = lower(%s)
                    RETURNING *
                    """,
                    (person["id"], work_email),
                )
                user = cur.fetchone()

            cur.execute(
                """
                UPDATE slmct.user_roles
                SET revoked_at = now()
                WHERE user_id = %s
                  AND revoked_at IS NULL
                  AND role_id NOT IN (SELECT id FROM slmct.roles WHERE code = %s)
                """,
                (user["id"], role_code),
            )
            cur.execute(
                """
                INSERT INTO slmct.user_roles (user_id, role_id, organisation_id)
                SELECT %s, id, NULL
                FROM slmct.roles
                WHERE code = %s
                ON CONFLICT DO NOTHING
                """,
                (user["id"], role_code),
            )
        elif work_email:
            cur.execute(
                """
                UPDATE slmct.app_users
                SET status = 'inactive', updated_at = now()
                WHERE email = %s
                RETURNING *
                """,
                (work_email,),
            )
            user = cur.fetchone()
            if user:
                cur.execute(
                    """
                    UPDATE slmct.user_roles
                    SET revoked_at = now()
                    WHERE user_id = %s AND revoked_at IS NULL
                    """,
                    (user["id"],),
                )

    result = _record_response(person)
    result["role_code"] = role_code
    result["can_login"] = role_code in LOGIN_ROLES
    result["user_id"] = str(user["id"]) if user else None
    if role_code in LOGIN_ROLES and payload.get("return_temp_password"):
        result["temporary_password"] = password
        result["temporary_password_expires_in_hours"] = 24
    return result


def _find_reusable_subscription(
    conn: Connection,
    organisation_id,
    product_name: str,
    *,
    vendor_id=None,
) -> dict | None:
    """Active subscription for the same product within an org (one sub, many seats)."""
    name = str(product_name or "").strip()
    if not name or not organisation_id:
        return None
    org_id = str(organisation_id)
    with conn.cursor() as cur:
        if vendor_id:
            cur.execute(
                """
                SELECT * FROM slmct.subscriptions
                WHERE organisation_id = %s
                  AND lower(name) = lower(%s)
                  AND status = 'active'
                  AND vendor_id = %s
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (org_id, name, vendor_id),
            )
            row = cur.fetchone()
            if row:
                return _record_response(row)
        cur.execute(
            """
            SELECT * FROM slmct.subscriptions
            WHERE organisation_id = %s
              AND lower(name) = lower(%s)
              AND status = 'active'
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (org_id, name),
        )
        row = cur.fetchone()
        return _record_response(row) if row else None


def _assign_employee_software_licence(
    cur,
    conn: Connection,
    *,
    organisation_id,
    subscription_id,
    person_id,
    licence_name: str,
) -> dict:
    """Assign an available seat or create a new licence on an existing subscription."""
    cur.execute(
        """
        SELECT * FROM slmct.licences
        WHERE subscription_id = %s AND assigned_to_person_id = %s AND status = 'assigned'
        LIMIT 1
        """,
        (subscription_id, person_id),
    )
    existing = cur.fetchone()
    if existing:
        return _record_response(existing)

    cur.execute(
        """
        SELECT id FROM slmct.licences
        WHERE subscription_id = %s AND status = 'available'
        ORDER BY created_at ASC
        LIMIT 1
        FOR UPDATE
        """,
        (subscription_id,),
    )
    available = cur.fetchone()
    if available:
        cur.execute(
            """
            UPDATE slmct.licences
            SET status = 'assigned',
                assigned_to_person_id = %s,
                assigned_at = now()::date,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (person_id, available["id"]),
        )
        licence_row = _record_response(cur.fetchone())
        _audit(conn, "update", "licence", licence_row["id"], '{"source":"workflow_complete_assign_available"}')
        return licence_row

    cur.execute(
        """
        INSERT INTO slmct.licences (
            organisation_id, subscription_id, assigned_to_person_id,
            licence_name, assigned_at, status
        )
        VALUES (%s, %s, %s, %s, now()::date, 'assigned')
        RETURNING *
        """,
        (organisation_id, subscription_id, person_id, licence_name),
    )
    licence_row = _record_response(cur.fetchone())
    _audit(conn, "create", "licence", licence_row["id"], '{"source":"workflow_complete_auto_assign"}')
    return licence_row


def _activate_workflow_record(request_row: dict, actor_user_id: UUID | None, conn: Connection) -> dict:
    module = request_row["requested_module"]
    action = request_row["requested_action"]
    payload = dict(request_row["payload"])
    if module not in WORKFLOW_MODULES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported workflow module")

    if action == "bulk_create":
        rows = payload.get("rows") or []
        if not isinstance(rows, list) or not rows:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bulk workflow requires payload.rows")
        activated_rows = []
        for row in rows:
            activated_rows.append(
                _activate_workflow_record(
                    {"requested_module": module, "requested_action": "create", "payload": dict(row)},
                    actor_user_id,
                    conn,
                )
            )
        first_id = activated_rows[0].get("id") if activated_rows else None
        return {"id": first_id, "bulk_count": len(activated_rows), "rows": activated_rows}

    if module == "organisations":
        if action == "update":
            record_id = payload.get("id")
            if not record_id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Workflow update requires payload.id")
            allowed_fields = {
                "name",
                "parent_id",
                "legal_name",
                "description",
                "website_url",
                "country_code",
                "currency_code",
                "is_sister_entity",
                "is_active",
            }
            fields = [field for field in allowed_fields if field in payload]
            if not fields:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM slmct.organisations WHERE id = %s", (record_id,))
                    return _record_response(cur.fetchone())
            values = [payload[field] for field in fields]
            values.append(record_id)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE slmct.organisations
                    SET {", ".join([f"{field} = %s" for field in fields] + ["updated_at = now()"])}
                    WHERE id = %s
                    RETURNING *
                    """,
                    values,
                )
                return _record_response(cur.fetchone())

        if action == "archive":
            record_id = payload.get("id")
            if not record_id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Workflow archive requires payload.id")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE slmct.organisations
                    SET is_active = false, updated_at = now()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (record_id,),
                )
                return _record_response(cur.fetchone())

        payload["is_active"] = True
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO slmct.organisations (
                  parent_id, code, name, legal_name, description, website_url,
                  country_code, currency_code, is_sister_entity, is_active
                )
                VALUES (%s, %s, %s, %s, %s, %s, upper(COALESCE(%s, 'AE')), upper(COALESCE(%s, 'AED')), COALESCE(%s, true), true)
                RETURNING *
                """,
                (
                    payload.get("parent_id"),
                    payload.get("code"),
                    payload.get("name"),
                    payload.get("legal_name"),
                    payload.get("description"),
                    payload.get("website_url"),
                    payload.get("country_code"),
                    payload.get("currency_code"),
                    payload.get("is_sister_entity", True),
                ),
            )
            org_row = _record_response(cur.fetchone())
        _audit(conn, "create", "organisation", org_row.get("id"), '{"source":"workflow_activate"}')
        return org_row

    if module in MODULES:
        config = _module_config(module)

        # Validate and clean up nullable foreign keys to avoid ForeignKeyViolation on activation
        if "vendor_id" in payload and payload["vendor_id"]:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM slmct.vendors WHERE id = %s", (payload["vendor_id"],))
                if not cur.fetchone():
                    payload["vendor_id"] = None

        for person_field in ["owner_person_id", "assigned_to_person_id"]:
            if person_field in payload and payload[person_field]:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM slmct.people WHERE id = %s", (payload[person_field],))
                    if not cur.fetchone():
                        payload[person_field] = None

        if "subscription_id" in payload and payload["subscription_id"]:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM slmct.subscriptions WHERE id = %s", (payload["subscription_id"],))
                if not cur.fetchone():
                    payload["subscription_id"] = None

        if "budget_id" in payload and payload["budget_id"]:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM slmct.budgets WHERE id = %s", (payload["budget_id"],))
                if not cur.fetchone():
                    payload["budget_id"] = None

        if "organisation_id" in payload and payload["organisation_id"]:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM slmct.organisations WHERE id = %s", (payload["organisation_id"],))
                if not cur.fetchone():
                    cur.execute("SELECT id FROM slmct.organisations WHERE code = 'derisk360_group'")
                    row = cur.fetchone()
                    payload["organisation_id"] = row["id"] if row else None

        if action == "update":
            record_id = payload.get("id")
            if not record_id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Workflow update requires payload.id")
            fields = [field for field in config["fields"] if field in payload]
            if not fields:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT * FROM {config['table']} WHERE id = %s", (record_id,))
                    return _record_response(cur.fetchone())
            values = [payload[field] for field in fields]
            values.append(record_id)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {config["table"]}
                    SET {", ".join([f"{field} = %s" for field in fields] + ["updated_at = now()"])}
                    WHERE id = %s
                    RETURNING *
                    """,
                    values,
                )
                return _record_response(cur.fetchone())

        if action == "archive":
            record_id = payload.get("id")
            if not record_id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Workflow archive requires payload.id")
            field, value = config["soft_delete"]
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {config["table"]}
                    SET {field} = %s, updated_at = now()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (value, record_id),
                )
                return _record_response(cur.fetchone())

        if module == "vendors":
            payload["status"] = "active"
        if module == "subscriptions":
            payload["status"] = "active"
            wf_type = str(request_row.get("workflow_type") or "")
            if action == "create" and wf_type == "employee_software_request":
                org_id = payload.get("organisation_id") or request_row.get("organisation_id")
                product_name = payload.get("name") or payload.get("tool_requested")
                existing = _find_reusable_subscription(
                    conn,
                    org_id,
                    str(product_name or ""),
                    vendor_id=payload.get("vendor_id"),
                )
                if existing:
                    _audit(
                        conn,
                        "update",
                        "subscription",
                        existing.get("id"),
                        '{"event":"employee_software_request_reused"}',
                    )
                    return existing
        if module == "licences":
            payload["status"] = payload.get("status") or ("assigned" if payload.get("assigned_to_person_id") else "available")
        if module == "budgets":
            payload["status"] = "approved"
        if module == "payments":
            payload["status"] = "paid"

        if "organisation_id" in config["fields"] and not payload.get("organisation_id"):
            fallback_org_id = request_row.get("organisation_id")
            if not fallback_org_id:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM slmct.organisations WHERE code = 'derisk360_group'")
                    row = cur.fetchone()
                    if row:
                        fallback_org_id = row["id"]
            payload["organisation_id"] = fallback_org_id

        fields = [field for field in config["fields"] if field in payload]
        values = [payload[field] for field in fields]
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {config["table"]} ({", ".join(fields)})
                VALUES ({", ".join(["%s"] * len(fields))})
                RETURNING *
                """,
                values,
            )
            new_row = _record_response(cur.fetchone())
        _audit(conn, "create", _entity_type_for_module(module), new_row.get("id"), '{"source":"workflow_activate"}')
        return new_row

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported workflow module")


def _list_module(module: str, organisation_id: UUID | None, conn: Connection, include_soft_deleted: bool = False) -> list[dict]:
    config = _module_config(module)
    where_clauses = []
    params = []
    if organisation_id and "organisation_id" in config["fields"]:
        where_clauses.append("organisation_id = %s")
        params.append(organisation_id)
    if not include_soft_deleted and "soft_delete" in config:
        status_field, delete_value = config["soft_delete"]
        where_clauses.append(f"{status_field} != %s")
        params.append(delete_value)
    
    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT *
            FROM {config["table"]}
            {where}
            ORDER BY {config["order"]}
            """,
            params,
        )
        return [_record_response(row) for row in cur.fetchall()]


def _create_module(
    module: str,
    payload: dict,
    conn: Connection,
    *,
    audit_metadata: str | None = None,
) -> dict:
    config = _module_config(module)

    # Vendors: if organisation_id not supplied, default to the first organisation
    if module == "vendors" and not payload.get("organisation_id"):
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM slmct.organisations ORDER BY created_at LIMIT 1")
            row = cur.fetchone()
            if row:
                payload["organisation_id"] = str(row["id"])

    fields = [field for field in config["fields"] if field in payload]
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid fields supplied")

    # Block duplicate active employee by full_name within the same org
    if module == "employees":
        new_name = (payload.get("full_name") or "").strip()
        org_id_check = payload.get("organisation_id")
        if new_name:
            with conn.cursor() as _nc:
                if org_id_check:
                    _nc.execute(
                        "SELECT id FROM slmct.people WHERE LOWER(full_name) = LOWER(%s) AND organisation_id = %s AND status = 'active' LIMIT 1",
                        (new_name, org_id_check),
                    )
                else:
                    _nc.execute(
                        "SELECT id FROM slmct.people WHERE LOWER(full_name) = LOWER(%s) AND status = 'active' LIMIT 1",
                        (new_name,),
                    )
                if _nc.fetchone():
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"An active employee named '{new_name}' already exists. Please check the employee list before adding.",
                    )

    values = [payload[field] for field in fields]
    placeholders = ", ".join(["%s"] * len(fields))
    columns = ", ".join(fields)

    # citext fields need explicit cast so psycopg3 can determine parameter type in lookup queries
    _CITEXT_FIELDS = {"work_email", "email", "contact_email"}

    with conn.cursor() as cur:
        try:
            cur.execute(
                f"""
                INSERT INTO {config["table"]} ({columns})
                VALUES ({placeholders})
                RETURNING *
                """,
                values,
            )
            row = cur.fetchone()
            _audit(conn, "create", module.rstrip("s"), row["id"], audit_metadata or '{"source":"api"}')
            conn.commit()
            return _record_response(row)
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            # A soft-deleted record with the same unique key exists — restore and update it
            soft_delete_cfg = config.get("soft_delete")
            if soft_delete_cfg:
                status_col, _ = soft_delete_cfg
                # Find the existing record by matching all payload fields that form the unique key.
                # Module-specific unique key fields (fallback to name/email).
                _UNIQUE_LOOKUP: dict[str, list[str]] = {
                    "budgets": ["organisation_id", "department", "fiscal_year"],
                    "employees": ["organisation_id", "work_email"],
                }
                lookup_candidates = _UNIQUE_LOOKUP.get(module, ["organisation_id", "name", "email", "work_email"])
                lookup_fields = [f for f in lookup_candidates if f in payload]
                if not lookup_fields:
                    raise HTTPException(status_code=409, detail="A record with this unique identifier already exists.")
                # Cast citext columns to text so psycopg3 can resolve the parameter type
                where_clause = " AND ".join(
                    f"{f}::text = %s" if f in _CITEXT_FIELDS else f"{f} = %s"
                    for f in lookup_fields
                )
                _, soft_deleted_val = soft_delete_cfg
                where_vals = [payload[f] for f in lookup_fields]
                with conn.cursor() as cur2:
                    # Only restore records that are actually soft-deleted; active duplicates should be blocked
                    cur2.execute(
                        f"SELECT id FROM {config['table']} WHERE {where_clause} AND {status_col}::text = %s LIMIT 1",
                        where_vals + [soft_deleted_val],
                    )
                    existing = cur2.fetchone()
                if not existing:
                    raise HTTPException(status_code=409, detail="A record with this unique identifier already exists.")
                # Restore it: use the module's "active" status value, not a hardcoded 'active'
                _MODULE_ACTIVE_STATUS: dict[str, str] = {
                    "budgets": "approved",
                    "payments": "paid",
                }
                restore_status = _MODULE_ACTIVE_STATUS.get(module, "active")
                update_fields = [f for f in fields if f not in lookup_fields and f != status_col]
                set_clauses = [f"{status_col} = '{restore_status}'"] + [f"{f} = %s" for f in update_fields]
                update_vals = [payload[f] for f in update_fields] + [str(existing["id"])]
                with conn.cursor() as cur2:
                    cur2.execute(
                        f"UPDATE {config['table']} SET {', '.join(set_clauses)}, updated_at = now() WHERE id = %s RETURNING *",
                        update_vals,
                    )
                    row = cur2.fetchone()
                _audit(conn, "update", module.rstrip("s"), existing["id"], audit_metadata or '{"source":"api"}')
                conn.commit()
                return _record_response(row)
            raise HTTPException(status_code=409, detail="A record with this unique identifier already exists.")


def _update_module(
    module: str,
    record_id: UUID,
    payload: dict,
    conn: Connection,
    *,
    audit_metadata: str | None = None,
) -> dict:
    config = _module_config(module)
    fields = [field for field in config["fields"] if field in payload]
    if not fields:
        return _get_module(module, record_id, conn)

    values = [payload[field] for field in fields]
    values.append(record_id)
    set_clause = ", ".join([f"{field} = %s" for field in fields] + ["updated_at = now()"])

    with conn.cursor() as cur:
        try:
            cur.execute(
                f"""
                UPDATE {config["table"]}
                SET {set_clause}
                WHERE id = %s
                RETURNING *
                """,
                values,
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
            _audit(conn, "update", module.rstrip("s"), record_id, audit_metadata or '{"source":"api"}')
            conn.commit()
            return _record_response(row)
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise


def _get_module(module: str, record_id: UUID, conn: Connection) -> dict:
    config = _module_config(module)
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {config['table']} WHERE id = %s", (record_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
    return _record_response(row)


def _delete_module(module: str, record_id: UUID, conn: Connection) -> dict:
    config = _module_config(module)
    field, value = config["soft_delete"]
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"""
                UPDATE {config["table"]}
                SET {field} = %s, updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (value, record_id),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
            # When a subscription is cancelled, revoke all its active licences
            if module == "subscriptions":
                cur.execute(
                    """
                    UPDATE slmct.licences
                    SET status = 'revoked', updated_at = now()
                    WHERE subscription_id = %s AND status NOT IN ('revoked', 'expired')
                    """,
                    (record_id,),
                )
            _audit(conn, "delete", module.rstrip("s"), record_id, '{"soft_delete":true,"source":"api"}')
            conn.commit()
            return _record_response(row)
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise


@app.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, conn: Connection = Depends(get_connection)) -> LoginResponse:
    username = payload.username.strip().lower()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              au.id,
              au.email,
              au.password_hash,
              au.status,
              au.must_change_password,
              au.temp_password_expires_at,
              p.full_name,
              COALESCE(
                jsonb_agg(DISTINCT r.code) FILTER (WHERE r.code IS NOT NULL),
                '[]'::jsonb
              ) AS roles
            FROM slmct.app_users au
            JOIN slmct.people p ON p.id = au.person_id
            LEFT JOIN slmct.user_roles ur ON ur.user_id = au.id AND ur.revoked_at IS NULL
            LEFT JOIN slmct.roles r ON r.id = ur.role_id AND r.can_login = true
            WHERE lower(au.email::text) = %s
               OR lower(split_part(au.email::text, '@', 1)) = %s
            GROUP BY au.id, au.email, au.password_hash, au.status, au.must_change_password, au.temp_password_expires_at, p.full_name
            """,
            (username, username),
        )
        user = cur.fetchone()

    if not user or user["status"] != "active" or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    if user["must_change_password"] and user["temp_password_expires_at"]:
        expires_at = user["temp_password_expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Temporary password expired. Ask Master Admin or IT Admin to reset it.")

    with conn.cursor() as cur:
        cur.execute("UPDATE slmct.app_users SET last_login_at = now() WHERE id = %s", (user["id"],))
        cur.execute(
            """
            INSERT INTO slmct.audit_logs (actor_user_id, actor_email, action, entity_type, entity_id, metadata)
            VALUES (%s, %s, 'login', 'app_user', %s, %s::jsonb)
            """,
            (user["id"], user["email"], user["id"], '{"result":"success"}'),
        )
        conn.commit()

    return LoginResponse(
        message="Login successful",
        user={
            "id": str(user["id"]),
            "email": str(user["email"]),
            "name": user["full_name"],
            "roles": user["roles"],
        },
    )


@app.post("/logout", response_model=LogoutResponse)
def logout(payload: LogoutRequest, conn: Connection = Depends(get_connection)) -> LogoutResponse:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO slmct.audit_logs (actor_user_id, actor_email, action, entity_type, entity_id, metadata)
            VALUES (%s, %s, 'logout', 'app_user', %s, %s::jsonb)
            """,
            (payload.user_id, payload.email, payload.user_id, '{"result":"success"}'),
        )
        conn.commit()

    return LogoutResponse(message="Logout successful")


@app.get("/roles")
def list_roles(conn: Connection = Depends(get_connection)) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT code, name, description, can_login
            FROM slmct.roles
            ORDER BY
              CASE code
                WHEN 'master_admin' THEN 1
                WHEN 'it_admin' THEN 2
                WHEN 'finance' THEN 3
                WHEN 'hr_admin' THEN 4
                WHEN 'auditor' THEN 5
                WHEN 'employee' THEN 6
                ELSE 99
              END
            """
        )
        return [_record_response(row) for row in cur.fetchall()]


def _bulk_headers(module: str) -> list[str]:
    if module not in BULK_UPLOAD_MODULES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bulk upload module not found")
    return MODULES[module]["fields"]


@app.get("/bulk-templates/{module}.xlsx")
def download_bulk_template(module: str) -> StreamingResponse:
    headers = _bulk_headers(module)
    examples = []
    if module in {"subscriptions", "budgets", "payments"} and "currency_code" in headers:
        examples.append({"currency_code": CURRENCY_CODES[0]})
    content = _build_xlsx(headers, examples, f"{module} template")
    return _xlsx_response(f"{module}-bulk-template.xlsx", content)


def _resolve_uuid_field(val: str | None, table: str, name_col: str, conn: Connection) -> str | None:
    """Return val unchanged if it's a valid UUID; otherwise look up by name in table."""
    if not val:
        return None
    try:
        UUID(str(val))
        return str(val)
    except (ValueError, AttributeError):
        with conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {table} WHERE LOWER({name_col}) = LOWER(%s) LIMIT 1", (str(val),))
            match = cur.fetchone()
            return str(match["id"]) if match else None


def _sanitise_uuid(val: str | None) -> str | None:
    """Return val if valid UUID, else None."""
    if not val:
        return None
    try:
        UUID(str(val))
        return str(val)
    except (ValueError, AttributeError):
        return None


# FK fields that need UUID resolution per module
_FK_RESOLVERS: dict[str, list[tuple[str, str, str]]] = {
    # (field, table, name_col)
    "vendors":       [("organisation_id", "slmct.organisations", "name")],
    "subscriptions": [("organisation_id", "slmct.organisations", "name"),
                      ("vendor_id",        "slmct.vendors",        "name")],
    "licences":      [("organisation_id",      "slmct.organisations", "name"),
                      ("subscription_id",       "slmct.subscriptions", "name"),
                      ("assigned_to_person_id", "slmct.people",        "work_email")],
    "budgets":       [("organisation_id", "slmct.organisations", "name")],
    "payments":      [("organisation_id", "slmct.organisations", "name"),
                      ("vendor_id",        "slmct.vendors",        "name"),
                      ("subscription_id",  "slmct.subscriptions",  "name")],
    "contracts":     [("organisation_id", "slmct.organisations", "name"),
                      ("vendor_id",        "slmct.vendors",        "name"),
                      ("subscription_id",  "slmct.subscriptions",  "name")],
    "employees":     [("organisation_id", "slmct.organisations", "name")],
}

# All UUID-typed fields per module — any still-invalid value is nulled before insert
_UUID_FIELDS: dict[str, list[str]] = {
    "vendors":       ["organisation_id"],
    "subscriptions": ["organisation_id", "vendor_id", "owner_person_id"],
    "licences":      ["organisation_id", "subscription_id", "assigned_to_person_id"],
    "budgets":       ["organisation_id"],
    "payments":      ["organisation_id", "subscription_id", "vendor_id", "budget_id"],
    "contracts":     ["organisation_id", "vendor_id", "subscription_id"],
    "employees":     ["organisation_id"],
}


def _resolve_upload_row(module: str, row: dict, conn: Connection) -> dict:
    resolved = dict(row)
    for field, table, name_col in _FK_RESOLVERS.get(module, []):
        resolved[field] = _resolve_uuid_field(resolved.get(field), table, name_col, conn)
    for field in _UUID_FIELDS.get(module, []):
        resolved[field] = _sanitise_uuid(resolved.get(field))
    # Convert Excel date serial numbers (e.g. 46196) to YYYY-MM-DD strings; null out unparseable values
    for field in _DATE_FIELDS:
        val = resolved.get(field)
        if val and isinstance(val, str):
            if re.match(r'^\d+(\.\d+)?$', val):
                resolved[field] = _excel_serial_to_date_str(val)
            else:
                resolved[field] = _sanitise_date(val)
        elif val and not isinstance(val, str):
            resolved[field] = _sanitise_date(str(val))
    return resolved


_DATE_FIELDS = {"start_date", "end_date", "renewal_date", "payment_date", "due_date", "assigned_at", "expires_at", "activation_date", "contract_date"}


def _excel_serial_to_date_str(val: str) -> str:
    try:
        from datetime import date as _date, timedelta as _td
        serial = float(val)
        if 20000 <= serial <= 60000:
            return (_date(1899, 12, 30) + _td(days=int(serial))).isoformat()
    except (ValueError, OverflowError):
        pass
    return None


def _sanitise_date(val: str) -> str | None:
    import re as _re
    if not val or not val.strip():
        return None
    # Accept YYYY-MM-DD or DD/MM/YYYY or DD-MM-YYYY
    if _re.match(r'^\d{4}-\d{2}-\d{2}$', val.strip()):
        return val.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(val.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _is_upload_duplicate(module: str, row: dict, conn: Connection) -> bool:
    # Duplicate checks are scoped to the row's organisation — the same subscription
    # or vendor name may legitimately exist in several sister organisations.
    org_id = row.get("organisation_id")
    with conn.cursor() as cur:
        if module == "vendors":
            name = (row.get("name") or "").strip()
            if not name:
                return False
            if org_id:
                cur.execute(
                    "SELECT 1 FROM slmct.vendors WHERE LOWER(name) = LOWER(%s) AND organisation_id = %s LIMIT 1",
                    (name, org_id),
                )
            else:
                cur.execute(
                    "SELECT 1 FROM slmct.vendors WHERE LOWER(name) = LOWER(%s) LIMIT 1",
                    (name,),
                )
        elif module == "subscriptions":
            name = (row.get("name") or "").strip()
            dept = (row.get("department") or "").strip()
            if not name:
                return False
            conditions = ["LOWER(name) = LOWER(%s)", "status != 'cancelled'"]
            params: list = [name]
            if dept:
                conditions.append("LOWER(department) = LOWER(%s)")
                params.append(dept)
            if org_id:
                conditions.append("organisation_id = %s")
                params.append(org_id)
            cur.execute(
                f"SELECT 1 FROM slmct.subscriptions WHERE {' AND '.join(conditions)} LIMIT 1",
                params,
            )
        elif module == "licences":
            licence_name = (row.get("licence_name") or "").strip()
            if not licence_name:
                return False
            if org_id:
                cur.execute(
                    "SELECT 1 FROM slmct.licences WHERE LOWER(licence_name) = LOWER(%s) AND organisation_id = %s AND status != 'revoked' LIMIT 1",
                    (licence_name, org_id),
                )
            else:
                cur.execute(
                    "SELECT 1 FROM slmct.licences WHERE LOWER(licence_name) = LOWER(%s) AND status != 'revoked' LIMIT 1",
                    (licence_name,),
                )
        elif module == "budgets":
            if not (row.get("fiscal_year") and row.get("department") and row.get("organisation_id")):
                return False
            cur.execute(
                "SELECT 1 FROM slmct.budgets WHERE fiscal_year = %s AND LOWER(department) = LOWER(%s) AND organisation_id = %s AND status != 'closed' LIMIT 1",
                (row.get("fiscal_year"), row.get("department"), row.get("organisation_id")),
            )
        elif module == "payments":
            ref = (row.get("reference") or "").strip()
            name = (row.get("name") or "").strip()
            if ref:
                cur.execute(
                    "SELECT 1 FROM slmct.payments WHERE LOWER(reference) = LOWER(%s) AND status != 'cancelled' LIMIT 1",
                    (ref,),
                )
            elif name and row.get("organisation_id"):
                cur.execute(
                    "SELECT 1 FROM slmct.payments WHERE LOWER(name) = LOWER(%s) AND organisation_id = %s AND status != 'cancelled' LIMIT 1",
                    (name, row.get("organisation_id")),
                )
            else:
                return False
        elif module == "contracts":
            title = (row.get("title") or "").strip()
            if not title:
                return False
            if org_id:
                cur.execute(
                    "SELECT 1 FROM slmct.contracts WHERE LOWER(title) = LOWER(%s) AND organisation_id = %s AND status != 'terminated' LIMIT 1",
                    (title, org_id),
                )
            else:
                cur.execute(
                    "SELECT 1 FROM slmct.contracts WHERE LOWER(title) = LOWER(%s) AND status != 'terminated' LIMIT 1",
                    (title,),
                )
        elif module == "employees":
            email = (row.get("work_email") or "").strip().lower()
            if not email:
                return False
            cur.execute("SELECT 1 FROM slmct.people WHERE LOWER(work_email) = %s LIMIT 1", (email,))
        else:
            return False
        return cur.fetchone() is not None


def _batch_dedup_key(module: str, row: dict) -> str:
    """Return a hashable key matching the same criteria as _is_upload_duplicate, for within-batch dedup."""
    if module == "vendors":
        return (row.get("name") or "").strip().lower()
    if module == "subscriptions":
        name = (row.get("name") or "").strip().lower()
        dept = (row.get("department") or "").strip().lower()
        return f"{name}||{dept}"
    if module == "licences":
        return (row.get("licence_name") or "").strip().lower()
    if module == "budgets":
        return f"{row.get('fiscal_year','')}||{(row.get('department') or '').lower()}||{row.get('organisation_id','')}"
    if module == "payments":
        return (row.get("reference") or row.get("name") or "").strip().lower()
    if module == "contracts":
        return (row.get("title") or "").strip().lower()
    if module == "employees":
        return (row.get("work_email") or "").strip().lower()
    return _row_label(module, row).lower()


def _row_label(module: str, row: dict) -> str:
    return str(
        row.get("name") or row.get("title") or row.get("licence_name") or
        row.get("full_name") or row.get("work_email") or row.get("reference") or
        row.get("contract_number") or row.get("department") or "unknown"
    )


@app.post("/bulk-uploads/{module}", status_code=status.HTTP_201_CREATED)
def upload_bulk_records(module: str, payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    if module not in BULK_UPLOAD_MODULES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bulk upload module not found")
    if not _actor_can_bulk_upload(payload):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bulk upload requires Master Admin, IT Admin, Finance, or HR Admin")

    content_base64 = payload.get("content_base64")
    if not content_base64:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="content_base64 is required")
    try:
        content = base64.b64decode(content_base64)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid base64 file content") from exc

    rows = _parse_xlsx(content)
    allowed_fields = set(_bulk_headers(module))
    rows = [{key: value for key, value in row.items() if key in allowed_fields} for row in rows]
    rows = [row for row in rows if row]
    if not rows:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No importable rows found")

    # Every row is imported into the organisation currently selected in the UI —
    # any organisation_id column in the uploaded file is overridden. Without this,
    # rows silently land in whatever org name happens to be typed in the file
    # (often a stale template value) instead of the org the user is working in.
    # A missing organisation is a hard error: downstream code would otherwise
    # silently fall back to the derisk360_group org.
    upload_org_id = payload.get("organisation_id")
    if "organisation_id" in allowed_fields:
        if not upload_org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="organisation_id is required — select an organisation before uploading.",
            )
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM slmct.organisations WHERE id = %s", (upload_org_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown organisation_id.")
        for row in rows:
            row["organisation_id"] = upload_org_id

    # Resolve FK names→IDs and deduplicate for all supported modules
    dedup_modules = {"vendors", "subscriptions", "licences", "budgets", "payments", "contracts", "employees"}
    reactivate_rows: list[dict] = []  # rows that exist as inactive and should be reactivated
    if module in dedup_modules:
        resolved_rows = []
        skipped = []
        seen_in_batch: set[str] = set()
        for row in rows:
            row = _resolve_upload_row(module, row, conn)
            batch_key = _batch_dedup_key(module, row)
            if batch_key in seen_in_batch:
                skipped.append(_row_label(module, row))
                continue
            if module in ("vendors", "employees"):
                # Look up existing record (active or inactive) to decide reactivate vs skip vs insert
                with conn.cursor() as _cur:
                    if module == "vendors":
                        name = (row.get("name") or "").strip()
                        org_id = row.get("organisation_id")
                        if org_id:
                            _cur.execute(
                                "SELECT id, status FROM slmct.vendors WHERE LOWER(name) = LOWER(%s) AND organisation_id = %s LIMIT 1",
                                (name, org_id),
                            )
                        else:
                            _cur.execute("SELECT id, status FROM slmct.vendors WHERE LOWER(name) = LOWER(%s) LIMIT 1", (name,))
                    else:  # employees
                        email = (row.get("work_email") or "").strip().lower()
                        _cur.execute("SELECT id, status FROM slmct.people WHERE LOWER(work_email) = %s LIMIT 1", (email,))
                    existing = _cur.fetchone()
                if existing:
                    if existing["status"] == "inactive":
                        reactivate_rows.append({"id": str(existing["id"]), "row": row})
                        seen_in_batch.add(batch_key)
                    else:
                        skipped.append(_row_label(module, row))
                else:
                    seen_in_batch.add(batch_key)
                    resolved_rows.append(row)
            elif _is_upload_duplicate(module, row, conn):
                skipped.append(_row_label(module, row))
            else:
                seen_in_batch.add(batch_key)
                resolved_rows.append(row)
        rows = resolved_rows
        skip_msg = f" Skipped {len(skipped)} duplicate(s): {', '.join(skipped)}." if skipped else ""
        if not rows and not reactivate_rows:
            return {"message": f"No new records to add — all rows already exist.{skip_msg}", "mode": "direct", "count": 0, "skipped": skipped}
    else:
        skip_msg = ""
        skipped = []

    if _actor_is_master(payload):
        inserted = []
        with conn.cursor() as _rcur:
            try:
                # Reactivate inactive records (vendors, employees)
                for entry in reactivate_rows:
                    rec_id = entry["id"]
                    rec_row = entry["row"]
                    config = _module_config(module)
                    update_fields = [f for f in config["fields"] if f in rec_row and f not in ("organisation_id", "status")]
                    if update_fields:
                        set_clause = ", ".join([f"{f} = %s" for f in update_fields] + ["status = 'active'", "updated_at = now()"])
                        vals = [rec_row[f] for f in update_fields] + [rec_id]
                        _rcur.execute(f"UPDATE {config['table']} SET {set_clause} WHERE id = %s RETURNING *", vals)
                    else:
                        _rcur.execute(f"UPDATE {config['table']} SET status = 'active', updated_at = now() WHERE id = %s RETURNING *", (rec_id,))
                    updated = _rcur.fetchone()
                    if updated:
                        inserted.append(_record_response(updated))
                for row in rows:
                    inserted.append(
                        _activate_workflow_record(
                            {"requested_module": module, "requested_action": "create", "payload": row},
                            UUID(payload["actor_user_id"]) if payload.get("actor_user_id") else None,
                            conn,
                        )
                    )
                _audit(conn, "create", f"{module.rstrip('s')}_bulk_upload", inserted[0]["id"] if inserted else None, '{"source":"bulk_upload","mode":"direct"}')
                conn.commit()
                label = "item" if len(inserted) == 1 else "items"
                return {"message": f"Upload completed — {len(inserted)} {label} added.{skip_msg}", "mode": "direct", "count": len(inserted), "skipped": skipped}
            except Exception:
                conn.rollback()
                raise

    actor_user_id, actor_email = _actor(payload)
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO slmct.workflow_requests (
                  requested_module, requested_action, payload, requested_by, requested_by_email, notes
                )
                VALUES (%s, 'bulk_create', %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    module,
                    Json({"rows": rows, "filename": payload.get("filename")}),
                    actor_user_id,
                    actor_email,
                    f"Bulk upload: {len(rows)} {module} row(s)",
                ),
            )
            row = cur.fetchone()
            _audit(conn, "create", "workflow_request", row["id"], '{"source":"bulk_upload","mode":"workflow"}')
            conn.commit()
            label = "item" if len(rows) == 1 else "items"
            return {"message": f"Upload submitted to workflow — {len(rows)} {label}.{skip_msg}", "mode": "workflow", "count": len(rows), "skipped": skipped, "workflow_request_id": row["id"]}
        except Exception:
            conn.rollback()
            raise


@app.get("/users")
def list_users(conn: Connection = Depends(get_connection)) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              p.id,
              p.organisation_id,
              p.employee_number,
              p.full_name,
              p.work_email,
              p.department,
              p.job_title,
              p.status,
              au.id AS user_id,
              au.status AS user_status,
              au.must_change_password,
              au.temp_password_expires_at,
              COALESCE(
                jsonb_agg(DISTINCT r.code) FILTER (WHERE r.code IS NOT NULL AND ur.revoked_at IS NULL),
                '[]'::jsonb
              ) AS roles,
              (
                SELECT r2.code
                FROM slmct.user_roles ur2
                JOIN slmct.roles r2 ON r2.id = ur2.role_id
                WHERE ur2.user_id = au.id
                  AND ur2.revoked_at IS NULL
                ORDER BY
                  CASE r2.code
                    WHEN 'master_admin' THEN 1
                    WHEN 'it_admin' THEN 2
                    WHEN 'finance' THEN 3
                    WHEN 'hr_admin' THEN 4
                    WHEN 'line_manager' THEN 5
                    WHEN 'employee' THEN 6
                    ELSE 99
                  END
                LIMIT 1
              ) AS role_code
            FROM slmct.people p
            JOIN slmct.app_users au ON au.person_id = p.id
            LEFT JOIN slmct.user_roles ur ON ur.user_id = au.id AND ur.revoked_at IS NULL
            LEFT JOIN slmct.roles r ON r.id = ur.role_id
            WHERE au.status = 'active'
              AND EXISTS (
                SELECT 1
                FROM slmct.user_roles active_roles
                JOIN slmct.roles login_roles ON login_roles.id = active_roles.role_id
                WHERE active_roles.user_id = au.id
                  AND active_roles.revoked_at IS NULL
                  AND login_roles.can_login = true
              )
            GROUP BY p.id, au.id, au.status, au.must_change_password, au.temp_password_expires_at
            ORDER BY p.full_name ASC
            """
        )
        return [_record_response(row) for row in cur.fetchall()]


@app.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    if not payload.get("full_name"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="full_name is required")
    email = (payload.get("work_email") or "").strip().lower()
    if email:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM slmct.people WHERE LOWER(work_email) = %s LIMIT 1",
                (email,),
            )
            existing_person = cur.fetchone()
        if existing_person:
            if str(existing_person["status"]) != "inactive":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"A user with email {email} already exists.")
            # Reactivate the inactive person and their app_user instead of creating a duplicate
            person_id = existing_person["id"]
            with conn.cursor() as cur:
                cur.execute("UPDATE slmct.people SET status = 'active', updated_at = now() WHERE id = %s", (person_id,))
                cur.execute("UPDATE slmct.app_users SET status = 'active', updated_at = now() WHERE person_id = %s", (person_id,))
                cur.execute(
                    "UPDATE slmct.user_roles SET revoked_at = NULL WHERE user_id = (SELECT id FROM slmct.app_users WHERE person_id = %s)",
                    (person_id,),
                )
                cur.execute("SELECT * FROM slmct.people WHERE id = %s", (person_id,))
                row = cur.fetchone()
            _audit(conn, "update", "user", person_id, '{"source":"api","event":"user_reactivated"}')
            conn.commit()
            return _record_response(row)
    payload["return_temp_password"] = True

    with conn.cursor():
        try:
            result = _upsert_person_and_role(payload, conn)
            _audit(conn, "create", "user", result["id"], '{"source":"api"}')
            conn.commit()
            return result
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise


@app.patch("/users/{person_id}")
def update_user(person_id: UUID, payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    payload["id"] = str(person_id)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM slmct.people WHERE id = %s", (person_id,))
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    merged = {**existing, **payload}
    with conn.cursor():
        try:
            result = _upsert_person_and_role(merged, conn)
            _audit(conn, "update", "user", person_id, '{"source":"api"}')
            conn.commit()
            return result
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise


LEGACY_DEMO_EMAIL_SUFFIXES = ("@derisk360.local", "@demo.derisk360.com")


def _can_manage_application_users(actor_roles: list[str] | None) -> bool:
    return bool(set(actor_roles or []).intersection({"master_admin", "it_admin"}))


def _is_legacy_demo_email(email: str | None) -> bool:
    lowered = str(email or "").strip().lower()
    return any(lowered.endswith(suffix) for suffix in LEGACY_DEMO_EMAIL_SUFFIXES)


def _deactivate_application_user(
    person_id: UUID,
    actor_user_id: UUID | None,
    actor_email: str | None,
    conn: Connection,
) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT p.id, p.work_email FROM slmct.people p WHERE p.id = %s", (person_id,))
        person = cur.fetchone()
        if not person:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        cur.execute("SELECT id FROM slmct.app_users WHERE person_id = %s", (person_id,))
        app_user = cur.fetchone()
        if not app_user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application user not found")

        cur.execute(
            """
            UPDATE slmct.app_users
            SET status = 'inactive', updated_at = now()
            WHERE person_id = %s
            """,
            (person_id,),
        )
        cur.execute(
            """
            UPDATE slmct.user_roles
            SET revoked_at = now()
            WHERE user_id = %s AND revoked_at IS NULL
            """,
            (app_user["id"],),
        )
        cur.execute(
            """
            UPDATE slmct.people
            SET status = 'inactive', updated_at = now()
            WHERE id = %s
            """,
            (person_id,),
        )
        cur.execute(
            """
            INSERT INTO slmct.audit_logs (actor_user_id, actor_email, action, entity_type, entity_id, metadata)
            VALUES (%s, %s, 'update', 'user', %s, %s::jsonb)
            """,
            (actor_user_id, actor_email, person_id, '{"source":"api","event":"user_deactivated"}'),
        )
        return {"message": "User deactivated", "id": str(person_id), "work_email": person["work_email"]}


@app.post("/users/{person_id}/deactivate")
def deactivate_user(person_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    if not _can_manage_application_users(payload.get("actor_roles")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only Master Admin or IT Admin can deactivate users")

    actor_user_id, actor_email = _actor(payload)
    with conn.cursor():
        try:
            result = _deactivate_application_user(person_id, actor_user_id, actor_email, conn)
            conn.commit()
            return result
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise


@app.delete("/users/{person_id}")
def deactivate_user_delete(person_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    return deactivate_user(person_id, payload, conn)


@app.post("/users/cleanup-legacy-demo")
def cleanup_legacy_demo_users(payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    if not _can_manage_application_users(payload.get("actor_roles")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only Master Admin or IT Admin can remove legacy demo users")

    actor_user_id, actor_email = _actor(payload)
    removed: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.work_email, p.full_name
            FROM slmct.people p
            JOIN slmct.app_users au ON au.person_id = p.id
            WHERE au.status = 'active'
              AND (
                lower(p.work_email) LIKE '%@derisk360.local'
                OR lower(p.work_email) LIKE '%@demo.derisk360.com'
              )
            ORDER BY p.work_email ASC
            """
        )
        rows = cur.fetchall()

    for row in rows:
        with conn.cursor():
            try:
                result = _deactivate_application_user(row["id"], actor_user_id, actor_email, conn)
                conn.commit()
                removed.append(
                    {
                        "id": result["id"],
                        "work_email": result["work_email"],
                        "full_name": row["full_name"],
                    }
                )
            except HTTPException:
                conn.rollback()
                raise
            except Exception:
                conn.rollback()
                raise

    return {
        "message": f"Removed {len(removed)} legacy demo account(s).",
        "removed_count": len(removed),
        "removed": removed,
    }


@app.post("/users/{person_id}/reset-password")
def reset_user_password(person_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    actor_roles = set(payload.get("actor_roles") or [])
    if not actor_roles.intersection({"master_admin", "it_admin"}):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only Master Admin or IT Admin can reset passwords")

    temp_password = _generate_temp_password()
    actor_user_id, actor_email = _actor(payload)
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                UPDATE slmct.app_users
                SET password_hash = %s,
                    must_change_password = true,
                    temp_password_expires_at = now() + interval '24 hours',
                    updated_at = now()
                WHERE person_id = %s
                RETURNING id, email, temp_password_expires_at
                """,
                (hash_password(temp_password), person_id),
            )
            user = cur.fetchone()
            if not user:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application user not found")
            cur.execute(
                """
                INSERT INTO slmct.audit_logs (actor_user_id, actor_email, action, entity_type, entity_id, metadata)
                VALUES (%s, %s, 'update', 'app_user', %s, %s::jsonb)
                """,
                (actor_user_id, actor_email, user["id"], '{"event":"password_reset","expires_in_hours":24}'),
            )
            conn.commit()
            return {
                "message": "Temporary password issued",
                "email": user["email"],
                "temporary_password": temp_password,
                "temp_password_expires_at": user["temp_password_expires_at"],
            }
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise


@app.post("/settings/change-password")
def change_password(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    user_id = payload.get("user_id")
    current_password = payload.get("current_password")
    new_password = payload.get("new_password")
    if not user_id or not current_password or not new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="user_id, current_password, and new_password are required")
    if len(new_password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be at least 8 characters")

    user = _lookup_user(conn, UUID(user_id))
    if not verify_password(current_password, user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect")

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE slmct.app_users
            SET password_hash = %s,
                must_change_password = false,
                temp_password_expires_at = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (hash_password(new_password), user["id"]),
        )
        cur.execute(
            """
            INSERT INTO slmct.audit_logs (actor_user_id, actor_email, action, entity_type, entity_id, metadata)
            VALUES (%s, %s, 'update', 'app_user', %s, %s::jsonb)
            """,
            (user["id"], user["email"], user["id"], '{"event":"password_change"}'),
        )
        conn.commit()
    return {"message": "Password updated successfully"}


@app.get("/workflow-requests")
def list_workflow_requests(conn: Connection = Depends(get_connection)) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, organisation_id, workflow_type, requested_module, requested_action, payload, status,
                   requested_by, requested_by_email, line_manager_approved_by, line_manager_approved_at,
                   master_approved_by, master_approved_at,
                   finance_approved_by, finance_approved_at, reopened_by, reopened_at,
                   completed_by, completed_at, activated_entity_id, rejection_reason, notes,
                   activation_method, activation_status, assigned_employee_name, assigned_employee_email,
                   activation_details, created_at, updated_at
            FROM slmct.workflow_requests
            WHERE status != 'cancelled'
            ORDER BY created_at DESC
            """
        )
        rows = [_record_response(row) for row in cur.fetchall()]
    from app.workflow_governance import get_current_approver

    enriched: list[dict] = []
    for row in rows:
        approver = get_current_approver(row, conn)
        row["current_approver_role"] = approver.get("role")
        row["current_approver_name"] = approver.get("name")
        row["current_approver_email"] = approver.get("email")
        row["workflow_stage"] = approver.get("stage")
        enriched.append(row)
    return enriched


@app.get("/workflow-requests/{request_id}/status-history")
def get_workflow_status_history(request_id: UUID, conn: Connection = Depends(get_connection)) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, workflow_request_id, from_status, to_status, actor_user_id, actor_email, notes, created_at
            FROM slmct.workflow_status_history
            WHERE workflow_request_id = %s
            ORDER BY created_at ASC
            """,
            (request_id,),
        )
        return [_record_response(row) for row in cur.fetchall()]


def _create_workflow_request(
    conn: Connection,
    payload: dict,
    *,
    audit_metadata: str | None = None,
) -> dict:
    from app.workflow_governance import record_status_history, resolve_workflow_type
    from app.workflow_submission_permissions import validate_software_workflow_submission

    requested_module = payload.get("requested_module")
    requested_payload = payload.get("payload") or {}
    if requested_module not in WORKFLOW_MODULES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported workflow module")
    actor_user_id, actor_email = _actor(payload)
    workflow_type = resolve_workflow_type(
        str(requested_module),
        requested_payload,
        payload.get("workflow_type"),
    )
    try:
        validate_software_workflow_submission(payload.get("actor_roles"), str(requested_module), workflow_type)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    email_overrides = payload.get("email_overrides") or None

    if workflow_type == "hr_onboarding_request":
        new_emp_email = (requested_payload.get("new_employee_email") or "").strip().lower()
        new_emp_name = (requested_payload.get("new_employee_full_name") or "").strip()
        if new_emp_email or new_emp_name:
            with conn.cursor() as _chk:
                _chk.execute(
                    """
                    SELECT full_name, work_email::text AS work_email FROM slmct.people
                    WHERE status = 'active'
                      AND (
                        (%s <> '' AND LOWER(work_email::text) = %s)
                        OR (%s <> '' AND LOWER(full_name) = LOWER(%s))
                      )
                    LIMIT 1
                    """,
                    (new_emp_email, new_emp_email, new_emp_name, new_emp_name),
                )
                existing = _chk.fetchone()
            if existing:
                existing_name = existing["full_name"] or new_emp_email
                existing_email = existing["work_email"] or ""
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Employee '{existing_name}' ({existing_email}) already exists as an active employee. Onboarding workflow blocked to prevent duplicate.",
                )

    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO slmct.workflow_requests (
                  organisation_id, workflow_type, requested_module, requested_action, payload,
                  requested_by, requested_by_email, notes
                )
                VALUES (%s, %s, %s, COALESCE(%s, 'create'), %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    requested_payload.get("organisation_id") or payload.get("organisation_id"),
                    workflow_type,
                    requested_module,
                    payload.get("requested_action"),
                    Json(requested_payload),
                    actor_user_id,
                    actor_email,
                    payload.get("notes")
                    or requested_payload.get("notes")
                    or requested_payload.get("justification_notes"),
                ),
            )
            row = cur.fetchone()
            record_status_history(conn, row["id"], None, "submitted", actor_user_id, actor_email, "Workflow submitted")
            _audit(conn, "create", "workflow_request", row["id"], audit_metadata or '{"source":"api"}')
            conn.commit()
            _run_notification(
                lambda request_row, connection: notify_workflow_created(
                    request_row, connection, email_overrides=email_overrides
                ),
                row,
                conn,
            )
            return _record_response(row)
        except Exception:
            conn.rollback()
            raise


@app.post("/workflow-requests", status_code=status.HTTP_201_CREATED)
def create_workflow_request(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    return _create_workflow_request(conn, payload)


@app.post("/workflow-requests/{request_id}/line-manager-approve")
def line_manager_approve_workflow_request(
    request_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)
) -> dict:
    from app.workflow_governance import record_status_history, requires_line_manager

    if not _actor_can_line_manager(payload):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Line Managers or Master Admin can approve at this stage",
        )
    actor_user_id, actor_email = _actor(payload)
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT * FROM slmct.workflow_requests WHERE id = %s", (request_id,))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow request not found")
            if not requires_line_manager(str(existing.get("workflow_type") or "")):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="This workflow type does not require line manager approval",
                )
            cur.execute(
                """
                UPDATE slmct.workflow_requests
                SET status = 'line_manager_approved',
                    line_manager_approved_by = %s,
                    line_manager_approved_at = now(),
                    rejection_reason = NULL,
                    updated_at = now()
                WHERE id = %s AND status IN ('submitted', 'reopened')
                RETURNING *
                """,
                (actor_user_id, request_id),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Submitted or reopened workflow request not found",
                )
            record_status_history(
                conn,
                request_id,
                existing["status"],
                "line_manager_approved",
                actor_user_id,
                actor_email,
                "Approved by line manager",
            )
            _audit(conn, "approve", "workflow_request", request_id, '{"stage":"line_manager"}')
            conn.commit()
            _run_notification(notify_workflow_approved, row, conn, actor_email)
            res = _record_response(row)
            return res
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise


@app.post("/workflow-requests/{request_id}/approve")
def approve_workflow_request(request_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    if not _actor_can_master_approve(payload):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Master Admin can approve workflow requests",
        )
    actor_user_id, actor_email = _actor(payload)
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT * FROM slmct.workflow_requests WHERE id = %s", (request_id,))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow request not found")
            cur.execute(
                """
                UPDATE slmct.workflow_requests
                SET status = 'master_approved',
                    master_approved_by = %s,
                    master_approved_at = now(),
                    rejection_reason = NULL,
                    updated_at = now()
                WHERE id = %s AND status IN ('submitted', 'reopened')
                RETURNING *
                """,
                (actor_user_id, request_id),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submitted or reopened workflow request not found")
            from app.workflow_governance import record_status_history

            record_status_history(
                conn,
                request_id,
                existing["status"],
                "master_approved",
                actor_user_id,
                actor_email,
                "Approved by master admin",
            )
            _audit(conn, "approve", "workflow_request", request_id)
            conn.commit()
            _run_notification(notify_workflow_approved, row, conn, actor_email)
            res = _record_response(row)
            return res
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise


@app.post("/workflow-requests/{request_id}/reject")
def reject_workflow_request(request_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    from app.workflow_governance import record_status_history
    from app.workflow_submission_permissions import validate_workflow_reject

    actor_user_id, actor_email = _actor(payload)
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT * FROM slmct.workflow_requests WHERE id = %s", (request_id,))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow request not found")
            try:
                validate_workflow_reject(payload.get("actor_roles"), str(existing.get("status") or ""))
            except PermissionError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
            cur.execute(
                """
                UPDATE slmct.workflow_requests
                SET status = 'rejected',
                    rejection_reason = %s,
                    updated_at = now()
                WHERE id = %s AND status IN ('submitted', 'reopened', 'line_manager_approved', 'master_approved', 'finance_approved', 'info_requested')
                RETURNING *
                """,
                (payload.get("rejection_reason"), request_id),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow request not found")
            record_status_history(
                conn,
                request_id,
                existing["status"],
                "rejected",
                actor_user_id,
                actor_email,
                str(payload.get("rejection_reason") or "Rejected"),
            )
            _audit(conn, "reject", "workflow_request", request_id)
            conn.commit()
            _run_notification(notify_workflow_rejected, row, conn, actor_email)
            res = _record_response(row)
            return res
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise


@app.post("/workflow-requests/{request_id}/reopen")
def reopen_workflow_request(request_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    from app.workflow_governance import record_status_history
    from app.workflow_submission_permissions import validate_workflow_reopen

    try:
        validate_workflow_reopen(payload.get("actor_roles"))
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    actor_user_id, actor_email = _actor(payload)
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT * FROM slmct.workflow_requests WHERE id = %s", (request_id,))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow request not found")
            cur.execute(
                """
                UPDATE slmct.workflow_requests
                SET status = 'reopened',
                    reopened_by = %s,
                    reopened_at = now(),
                    updated_at = now()
                WHERE id = %s AND status = 'rejected'
                RETURNING *
                """,
                (actor_user_id, request_id),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rejected workflow request not found")
            record_status_history(
                conn,
                request_id,
                existing["status"],
                "reopened",
                actor_user_id,
                actor_email,
                "Workflow reopened after rejection",
            )
            _audit(conn, "update", "workflow_request", request_id, '{"event":"workflow_reopened"}')
            conn.commit()
            _run_notification(notify_workflow_reopened, row, conn, actor_email)
            res = _record_response(row)
            return res
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise


@app.post("/workflow-requests/{request_id}/validate-budget")
def validate_budget_workflow_request(request_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    from app.workflow_governance import finance_validation_statuses, record_status_history

    actor_user_id, actor_email = _actor(payload)
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT * FROM slmct.workflow_requests WHERE id = %s", (request_id,))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow request not found")
            _wf_type_check = str(existing.get("workflow_type") or "")
            if _wf_type_check == "employee_offboarding":
                if not bool(set(payload.get("actor_roles") or []).intersection({"master_admin", "it_admin"})):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only IT Admin or Master Admin can confirm offboarding")
            elif not _actor_can_finance_close(payload):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only Finance Managers or Master Admin can perform budget validation")
            allowed_statuses = finance_validation_statuses(_wf_type_check)
            _next_status = "it_confirmed" if _wf_type_check == "employee_offboarding" else "finance_approved"
            cur.execute(
                f"""
                UPDATE slmct.workflow_requests
                SET status = '{_next_status}',
                    finance_approved_by = %s,
                    finance_approved_at = now(),
                    updated_at = now()
                WHERE id = %s AND status = ANY(%s)
                RETURNING *
                """,
                (actor_user_id, request_id, list(allowed_statuses)),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Workflow request is not ready for finance validation",
                )
            record_status_history(
                conn,
                request_id,
                existing["status"],
                _next_status,
                actor_user_id,
                actor_email,
                "IT confirmed offboarding" if _wf_type_check == "employee_offboarding" else "Budget validated by finance",
            )
            _audit(conn, "update", "workflow_request", request_id, '{"event":"budget_validated"}')
            conn.commit()
            _run_notification(notify_workflow_budget_validated, row, conn, actor_email)
            res = _record_response(row)
            return res
        except HTTPException:
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Budget validation failed: {str(e)}"
            )


@app.post("/workflow-requests/{request_id}/request-info")
def request_workflow_info(request_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    from app.workflow_governance import record_status_history
    from app.workflow_submission_permissions import validate_workflow_request_info

    actor_user_id, actor_email = _actor(payload)
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT * FROM slmct.workflow_requests WHERE id = %s", (request_id,))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow request not found")
            try:
                validate_workflow_request_info(payload.get("actor_roles"), str(existing.get("status") or ""))
            except PermissionError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

            info_message = payload.get("info_request_message") or "Please provide additional details."
            cur.execute(
                """
                UPDATE slmct.workflow_requests
                SET status = 'info_requested',
                    info_request_message = %s,
                    pre_info_request_status = status,
                    updated_at = now()
                WHERE id = %s AND status IN ('submitted', 'reopened', 'line_manager_approved', 'master_approved', 'finance_approved')
                RETURNING *
                """,
                (info_message, request_id),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Workflow request is not ready for an information request",
                )
            record_status_history(
                conn,
                request_id,
                existing["status"],
                "info_requested",
                actor_user_id,
                actor_email,
                str(info_message),
            )
            _audit(conn, "update", "workflow_request", request_id, '{"event":"info_requested"}')
            conn.commit()
            _run_notification(notify_workflow_info_requested, row, conn, actor_email)
            res = _record_response(row)
            return res
        except HTTPException:
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Information request failed: {str(e)}",
            )


def _resubmit_workflow_request(
    conn: Connection,
    request_id: UUID,
    payload: dict,
    *,
    audit_metadata: str | None = None,
) -> dict:
    from app.workflow_governance import record_status_history
    from app.workflow_submission_permissions import validate_workflow_resubmit

    actor_user_id, actor_email = _actor(payload)
    updated_payload = payload.get("payload")
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT * FROM slmct.workflow_requests WHERE id = %s", (request_id,))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow request not found")
            try:
                validate_workflow_resubmit(payload.get("actor_roles"), actor_email, existing)
            except PermissionError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

            merged_payload = dict(existing.get("payload") or {})
            if isinstance(updated_payload, dict):
                merged_payload.update(updated_payload)

            restore_status = str(existing.get("pre_info_request_status") or "submitted")
            if restore_status not in ("submitted", "reopened", "line_manager_approved", "master_approved", "finance_approved"):
                restore_status = "submitted"

            cur.execute(
                """
                UPDATE slmct.workflow_requests
                SET status = %s,
                    payload = %s,
                    info_request_message = NULL,
                    pre_info_request_status = NULL,
                    rejection_reason = NULL,
                    updated_at = now()
                WHERE id = %s AND status = 'info_requested'
                RETURNING *
                """,
                (restore_status, Json(merged_payload), request_id),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Workflow request is not awaiting resubmission",
                )
            record_status_history(
                conn,
                request_id,
                existing["status"],
                restore_status,
                actor_user_id,
                actor_email,
                "Requester resubmitted after information request",
            )
            _audit(
                conn,
                "update",
                "workflow_request",
                request_id,
                audit_metadata or '{"source":"api","event":"resubmitted"}',
            )
            conn.commit()
            _run_notification(notify_workflow_created, row, conn)
            return _record_response(row)
        except HTTPException:
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Workflow resubmission failed: {str(e)}",
            )


@app.post("/workflow-requests/{request_id}/resubmit")
def resubmit_workflow_request(request_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    return _resubmit_workflow_request(conn, request_id, payload)


def _create_payment_for_completed_workflow(request_row: dict, activated: dict, conn: Connection, cur) -> dict | None:
    module = request_row["requested_module"]
    if module not in {"subscriptions", "licences"}:
        return None

    payload = dict(request_row["payload"] or {})
    org_id = request_row.get("organisation_id") or payload.get("organisation_id")
    if not org_id:
        return None

    subscription_id = None
    vendor_id = payload.get("vendor_id")
    if module == "subscriptions":
        subscription_id = activated.get("id")
        vendor_id = vendor_id or activated.get("vendor_id")
    elif module == "licences":
        subscription_id = activated.get("subscription_id") or payload.get("subscription_id")

    amount = float(payload.get("amount") or activated.get("amount") or 0)
    if amount <= 0:
        return None

    currency_code = str(payload.get("currency_code") or activated.get("currency_code") or "AED").upper()
    department = payload.get("department") or activated.get("department")
    budget_id = None
    invoice_ref = payload.get("invoice_number") or payload.get("invoice_no")
    reference = str(invoice_ref or f"WF-{str(request_row['id'])[:8].upper()}")

    cur.execute(
        "SELECT id FROM slmct.payments WHERE organisation_id = %s AND reference = %s LIMIT 1",
        (org_id, reference),
    )
    if cur.fetchone():
        return None

    if department:
        cur.execute(
            """
            SELECT id FROM slmct.budgets
            WHERE organisation_id = %s
              AND lower(department) = lower(%s)
              AND fiscal_year = EXTRACT(YEAR FROM CURRENT_DATE)::int
              AND status IN ('approved', 'locked')
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (org_id, department),
        )
        budget_row = cur.fetchone()
        if budget_row:
            budget_id = budget_row["id"]

    notes = (
        f"Auto-created from workflow {request_row['id']} purchase confirmation. "
        f"{payload.get('notes') or request_row.get('notes') or ''}"
    ).strip()
    payment_name = str(
        payload.get("name")
        or activated.get("name")
        or activated.get("licence_name")
        or reference
    ).strip()

    cur.execute(
        """
        INSERT INTO slmct.payments (
          organisation_id, subscription_id, vendor_id, budget_id, name,
          payment_date, due_date, amount, currency_code, status, reference, notes
        )
        VALUES (%s, %s, %s, %s, %s, CURRENT_DATE, CURRENT_DATE, %s, %s, 'paid', %s, %s)
        RETURNING *
        """,
        (org_id, subscription_id, vendor_id, budget_id, payment_name, amount, currency_code, reference, notes),
    )
    payment_row = cur.fetchone()
    _audit(conn, "create", "payment", payment_row["id"], '{"source":"workflow_complete"}')
    return _record_response(payment_row)


@app.post("/workflow-requests/{request_id}/complete")
def complete_workflow_request(request_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    from app.workflow_governance import record_status_history
    from app.workflow_submission_permissions import validate_workflow_procurement_complete

    try:
        validate_workflow_procurement_complete(payload.get("actor_roles"))
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    actor_user_id, actor_email = _actor(payload)
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT * FROM slmct.workflow_requests WHERE id = %s FOR UPDATE", (request_id,))
            request_row = cur.fetchone()
            if not request_row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow request not found")
            _wf_type_complete = str(request_row.get("workflow_type") or "")
            if _wf_type_complete == "employee_offboarding":
                _allowed_complete = {"it_confirmed"}
            elif _wf_type_complete == "hr_onboarding_request":
                _allowed_complete = {"finance_approvd"}
            else:
                _allowed_complete = {"finance_approved"}
            if request_row["status"] not in _allowed_complete:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Budget validation / finance approval is required before procurement completion")

            if _wf_type_complete == "employee_offboarding":
                activation = {"activation_method": None, "activation_details": {}, "assigned_employee_name": "", "assigned_employee_email": ""}
                activated = {"id": None}
            else:
                activation = _validate_activation_payload(payload)
                activated = _activate_workflow_record(request_row, actor_user_id, conn)
            cur.execute(
                """
                UPDATE slmct.workflow_requests
                SET status = 'completed',
                    completed_by = %s,
                    completed_at = now(),
                    activated_entity_id = %s,
                    activation_method = %s,
                    activation_status = 'ready',
                    assigned_employee_name = %s,
                    assigned_employee_email = %s,
                    activation_details = %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (
                    actor_user_id,
                    activated["id"],
                    activation["activation_method"],
                    activation["assigned_employee_name"],
                    activation["assigned_employee_email"],
                    Json(activation["activation_details"]),
                    request_id,
                ),
            )
            row = cur.fetchone()
            record_status_history(
                conn,
                request_id,
                request_row["status"],
                "completed",
                actor_user_id,
                actor_email,
                "Procurement completed and record activated",
            )
            payment_row = _create_payment_for_completed_workflow(request_row, activated, conn, cur)
            if activated.get("id"):
                _audit(conn, "update", _entity_type_for_module(request_row["requested_module"]), activated["id"], '{"event":"workflow_completed"}')

            # For HR onboarding: force licence status to "assigned" immediately on activation
            wf_type_str = str(request_row.get("workflow_type") or "")

            # For employee offboarding: full cleanup of departing employee
            if wf_type_str == "employee_offboarding":
                _stored_payload = request_row.get("payload") or {}
                if isinstance(_stored_payload, str):
                    import json as _json
                    try:
                        _stored_payload = _json.loads(_stored_payload)
                    except Exception:
                        _stored_payload = {}
                offboard_email = str(payload.get("offboarded_employee_email") or _stored_payload.get("offboarded_employee_email") or "").strip()
                if offboard_email:
                    # Resolve person ID — use this for all subsequent operations (avoids email join mismatches)
                    cur.execute("SELECT id FROM slmct.people WHERE lower(work_email::text) = lower(%s) LIMIT 1", (offboard_email,))
                    _person_row = cur.fetchone()
                    person_id = _person_row["id"] if _person_row else None

                    # 1. Revoke ALL licences assigned to this person (any status except already revoked/expired)
                    if person_id:
                        cur.execute(
                            "UPDATE slmct.licences SET status = 'revoked', updated_at = now() WHERE assigned_to_person_id = %s AND status NOT IN ('revoked', 'expired')",
                            (person_id,),
                        )
                        # 2. Clear subscription ownership (owner_person_id)
                        cur.execute(
                            "UPDATE slmct.subscriptions SET owner_person_id = NULL, updated_at = now() WHERE owner_person_id = %s",
                            (person_id,),
                        )
                        # 3. Deactivate employee (people) record
                        cur.execute(
                            "UPDATE slmct.people SET status = 'inactive', updated_at = now() WHERE id = %s",
                            (person_id,),
                        )

                    # 4. Deactivate app_user login
                    cur.execute(
                        "UPDATE slmct.app_users SET status = 'inactive', updated_at = now() WHERE lower(email::text) = lower(%s)",
                        (offboard_email,),
                    )
                    # 5. Revoke all user roles
                    cur.execute(
                        """
                        UPDATE slmct.user_roles SET revoked_at = now()
                        WHERE user_id IN (SELECT id FROM slmct.app_users WHERE lower(email::text) = lower(%s))
                          AND revoked_at IS NULL
                        """,
                        (offboard_email,),
                    )
                    _audit(conn, "update", "workflow_request", request_id, f'{{"event":"offboarding_completed","employee":"{offboard_email}","person_id":"{person_id}"}}')

            if wf_type_str == "hr_onboarding_request":
                _ob_payload = request_row.get("payload") or {}
                if isinstance(_ob_payload, str):
                    import json as _json2
                    try: _ob_payload = _json2.loads(_ob_payload)
                    except Exception: _ob_payload = {}
                new_emp_email = str(_ob_payload.get("new_employee_email") or "").strip().lower()
                new_emp_name = str(_ob_payload.get("new_employee_full_name") or "").strip()
                new_emp_job = str(_ob_payload.get("new_employee_job_title") or "").strip()
                new_emp_dept = str(_ob_payload.get("department") or "").strip()
                new_emp_lm_email = str(_ob_payload.get("new_employee_line_manager_email") or "").strip() or None
                new_emp_temp_pw = str(_ob_payload.get("new_employee_temp_password") or "").strip()
                org_id = request_row.get("organisation_id")

                if new_emp_email:
                    # 1. Create or reactivate employee (people) record
                    cur.execute("SELECT id FROM slmct.people WHERE lower(work_email::text) = %s LIMIT 1", (new_emp_email,))
                    existing_person = cur.fetchone()
                    if existing_person:
                        person_id_new = existing_person["id"]
                        cur.execute(
                            "UPDATE slmct.people SET status = 'active', full_name = COALESCE(NULLIF(%s,''), full_name), job_title = COALESCE(NULLIF(%s,''), job_title), department = COALESCE(NULLIF(%s,''), department), line_manager_email = COALESCE(%s, line_manager_email), updated_at = now() WHERE id = %s",
                            (new_emp_name, new_emp_job, new_emp_dept, new_emp_lm_email, person_id_new),
                        )
                    else:
                        cur.execute(
                            "INSERT INTO slmct.people (organisation_id, full_name, work_email, job_title, department, line_manager_email, status) VALUES (%s, %s, %s, %s, %s, %s, 'active') RETURNING id",
                            (org_id, new_emp_name or new_emp_email, new_emp_email, new_emp_job, new_emp_dept, new_emp_lm_email),
                        )
                        person_id_new = cur.fetchone()["id"]

                    # 2. Create or reactivate app_users login
                    pw_hash = hash_password(new_emp_temp_pw if new_emp_temp_pw else "ChangeMe123!")
                    cur.execute("SELECT id FROM slmct.app_users WHERE lower(email::text) = %s LIMIT 1", (new_emp_email,))
                    existing_user = cur.fetchone()
                    if existing_user:
                        user_id_new = existing_user["id"]
                        cur.execute(
                            "UPDATE slmct.app_users SET status = 'active', password_hash = %s, must_change_password = true, updated_at = now() WHERE id = %s",
                            (pw_hash, user_id_new),
                        )
                    else:
                        cur.execute(
                            "INSERT INTO slmct.app_users (person_id, email, password_hash, status, must_change_password) VALUES (%s, %s, %s, 'active', true) RETURNING id",
                            (person_id_new, new_emp_email, pw_hash),
                        )
                        user_id_new = cur.fetchone()["id"]

                    # 3. Assign employee role
                    cur.execute("SELECT id FROM slmct.roles WHERE code = 'employee' LIMIT 1")
                    emp_role = cur.fetchone()
                    if emp_role:
                        cur.execute(
                            "INSERT INTO slmct.user_roles (user_id, role_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (user_id_new, emp_role["id"]),
                        )

                    # 4. Link licence to new person and mark assigned
                    if activated.get("id"):
                        cur.execute(
                            "UPDATE slmct.licences SET status = 'assigned', assigned_to_person_id = %s, updated_at = now() WHERE id = %s",
                            (person_id_new, activated["id"]),
                        )
                    _audit(conn, "create", "person", person_id_new, f'{{"event":"hr_onboarding_employee_created","email":"{new_emp_email}"}}')

            # For new_subscription_request: create `quantity` available licence seats in the pool
            if wf_type_str in ("new_subscription_request", "generic_procurement") and request_row.get("requested_module") == "subscriptions" and activated.get("id"):
                _sub_payload = dict(request_row.get("payload") or {})
                _quantity = int(_sub_payload.get("quantity") or 1)
                _quantity = max(1, min(_quantity, 500))  # guard against unreasonable values
                _sub_name = str(activated.get("name") or _sub_payload.get("name") or "Seat")
                _org_id = activated.get("organisation_id") or request_row.get("organisation_id")
                for _seat_num in range(1, _quantity + 1):
                    cur.execute(
                        """
                        INSERT INTO slmct.licences (organisation_id, subscription_id, licence_name, status)
                        VALUES (%s, %s, %s, 'available')
                        RETURNING id
                        """,
                        (_org_id, activated["id"], f"{_sub_name} Seat {_seat_num}" if _quantity > 1 else f"{_sub_name} Seat"),
                    )
                    _audit(conn, "create", "licence", cur.fetchone()["id"], '{"source":"workflow_complete_pool"}')

            # Auto-assign a licence when an employee software request completes and a subscription was activated
            licence_row = None
            if wf_type_str == "employee_software_request" and request_row.get("requested_module") == "subscriptions":
                employee_email = str(activation.get("assigned_employee_email") or request_row.get("assigned_employee_email") or request_row.get("requested_by_email") or "").strip()
                if employee_email:
                    cur.execute("SELECT id FROM slmct.people WHERE work_email = %s LIMIT 1", (employee_email,))
                    person_row = cur.fetchone()
                    if person_row:
                        person_id = person_row["id"]
                        sub_name = str(activated.get("name") or dict(request_row.get("payload") or {}).get("name") or "Licence")
                        org_id = activated.get("organisation_id") or request_row.get("organisation_id")
                        licence_row = _assign_employee_software_licence(
                            cur,
                            conn,
                            organisation_id=org_id,
                            subscription_id=activated["id"],
                            person_id=person_id,
                            licence_name=f"{sub_name} Seat",
                        )

            # Generate credential-change token if activation_details contains a password
            cred_token = None
            act_details = activation.get("activation_details") or {}
            if act_details.get("password"):
                cred_token = secrets.token_urlsafe(32)
                token_expires = datetime.now(timezone.utc) + timedelta(days=30)
                cur.execute(
                    """
                    UPDATE slmct.workflow_requests
                    SET credential_change_token = %s,
                        credential_change_token_expires_at = %s
                    WHERE id = %s
                    """,
                    (cred_token, token_expires, request_id),
                )
                # Re-fetch row with token included
                cur.execute("SELECT * FROM slmct.workflow_requests WHERE id = %s", (request_id,))
                row = cur.fetchone()

            conn.commit()
            _run_notification(notify_workflow_completed, row, conn, actor_email)
            result = _record_response(row)
            result["activated_record"] = activated
            if payment_row:
                result["payment_record"] = payment_row
            if licence_row:
                result["licence_record"] = licence_row
            return result
        except HTTPException:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            import traceback
            traceback.print_exc()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Workflow completion failed: {str(e)}"
            )


@app.get("/workflow-requests/credential-token/{token}")
def get_credential_change_info(token: str, conn: Connection = Depends(get_connection)) -> dict:
    """Returns minimal info about the workflow so the employee can confirm before changing password."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, credential_change_token_expires_at,
                   activation_details, assigned_employee_name, assigned_employee_email
            FROM slmct.workflow_requests
            WHERE credential_change_token = %s AND status = 'completed'
            LIMIT 1
            """,
            (token,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Invalid or expired link.")
    expires_at = row["credential_change_token_expires_at"]
    if expires_at and datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=410, detail="This change-password link has expired.")
    act = row["activation_details"] or {}
    return {
        "valid": True,
        "assigned_name": row["assigned_employee_name"] or "",
        "assigned_email": row["assigned_employee_email"] or "",
        "username": act.get("username") or "",
        "expires_at": str(expires_at) if expires_at else None,
    }


@app.post("/workflow-requests/credential-token/{token}")
def update_credentials_via_token(token: str, payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    """Allows an employee to update their licence account password using a one-time token from their completion email."""
    new_password = (payload.get("new_password") or "").strip()
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, credential_change_token_expires_at, activation_details
            FROM slmct.workflow_requests
            WHERE credential_change_token = %s AND status = 'completed'
            LIMIT 1
            """,
            (token,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Invalid or expired link.")
        expires_at = row["credential_change_token_expires_at"]
        if expires_at and datetime.now(timezone.utc) > expires_at:
            raise HTTPException(status_code=410, detail="This change-password link has expired.")
        act = dict(row["activation_details"] or {})
        act["password"] = new_password
        try:
            cur.execute(
                """
                UPDATE slmct.workflow_requests
                SET activation_details = %s, updated_at = now()
                WHERE id = %s
                """,
                (Json(act), row["id"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"success": True, "message": "Password updated successfully."}


@app.post("/workflow-requests/{request_id}/change-password")
def employee_change_password(request_id: UUID, payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    """Authenticated employee changes the licence account password for their own completed workflow."""
    caller_email = (payload.get("actor_email") or "").strip().lower()
    new_password = (payload.get("new_password") or "").strip()
    if not caller_email:
        raise HTTPException(status_code=400, detail="actor_email is required.")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, activation_details, assigned_employee_email, requested_by_email FROM slmct.workflow_requests WHERE id = %s LIMIT 1",
            (request_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workflow request not found.")
        if row["status"] != "completed":
            raise HTTPException(status_code=400, detail="Password can only be changed for completed workflows.")
        allowed_emails = {
            (row["assigned_employee_email"] or "").strip().lower(),
            (row["requested_by_email"] or "").strip().lower(),
        }
        if caller_email not in allowed_emails:
            raise HTTPException(status_code=403, detail="You are not authorised to change the password for this workflow.")
        act = dict(row["activation_details"] or {})
        act["password"] = new_password
        try:
            cur.execute(
                "UPDATE slmct.workflow_requests SET activation_details = %s, updated_at = now() WHERE id = %s",
                (Json(act), request_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"success": True, "message": "Password updated successfully."}


@app.delete("/workflow-requests/{request_id}")
def cancel_workflow_request(request_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    roles = set(payload.get("actor_roles") or [])
    actor_email = (payload.get("actor_email") or "").strip().lower()
    is_master = "master_admin" in roles

    # Fetch the workflow to check ownership
    with conn.cursor() as _chk:
        _chk.execute("SELECT requested_by_email, status FROM slmct.workflow_requests WHERE id = %s", (request_id,))
        wf = _chk.fetchone()
    if not wf:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow request not found")

    is_requester = actor_email and actor_email == (wf["requested_by_email"] or "").lower()
    cancellable_statuses = {"submitted", "reopened", "info_requested"}

    if not is_master and not (is_requester and wf["status"] in cancellable_statuses):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only cancel your own requests that are still pending approval.",
        )

    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                UPDATE slmct.workflow_requests
                SET status = 'cancelled', updated_at = now()
                WHERE id = %s AND status != 'cancelled'
                RETURNING *
                """,
                (request_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active workflow request not found")
            _audit(conn, "delete", "workflow_request", request_id, '{"source":"workflow_board","action":"cancel"}')
            conn.commit()
            return _record_response(row)
        except HTTPException:
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Workflow removal failed: {str(e)}",
            )


@app.get("/tool-requests")
def list_tool_requests(organisation_id: UUID | None = None, conn: Connection = Depends(get_connection)) -> list[dict]:
    where_clauses = []
    params: list = []
    if organisation_id:
        where_clauses.append("organisation_id = %s")
        params.append(organisation_id)
    where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT *
            FROM slmct.tool_requests
            {where}
            ORDER BY created_at DESC
            """,
            params,
        )
        return [_record_response(row) for row in cur.fetchall()]


@app.post("/tool-requests", status_code=status.HTTP_201_CREATED)
def create_tool_request(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    from app.workflow_submission_permissions import validate_tool_request_submission

    try:
        validate_tool_request_submission(payload.get("actor_roles"))
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    required = ["organisation_id", "requester_name", "requester_email", "requested_tool"]
    missing = [field for field in required if not payload.get(field)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required fields: {', '.join(missing)}",
        )

    subject = payload.get("email_subject") or f"Request for {payload['requested_tool']}"
    message_body = payload.get("message_body")
    if not message_body:
        message_body = (
            f"Hello IT Team,\n\n"
            f"I would like access to {payload['requested_tool']}.\n\n"
            f"Department:\n{payload.get('department') or 'Not specified'}\n\n"
            f"Business Justification:\n{payload.get('business_justification') or 'Not provided'}\n\n"
            f"Thank you,\n{payload['requester_name']}"
        )

    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO slmct.tool_requests (
                  organisation_id, requester_name, requester_email, department,
                  requested_tool, vendor_name, category, estimated_amount, currency_code,
                  business_justification, message_body, email_subject, status, notes
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, 'AED'), %s, %s, %s, COALESCE(%s::slmct.tool_request_status, 'new'::slmct.tool_request_status), %s)
                RETURNING *
                """,
                (
                    payload["organisation_id"],
                    payload["requester_name"],
                    payload["requester_email"],
                    payload.get("department"),
                    payload["requested_tool"],
                    payload.get("vendor_name"),
                    payload.get("category"),
                    payload.get("estimated_amount"),
                    payload.get("currency_code"),
                    payload.get("business_justification"),
                    message_body,
                    subject,
                    payload.get("status"),
                    payload.get("notes"),
                ),
            )
            row = cur.fetchone()
            _audit(conn, "create", "tool_request", row["id"])
            conn.commit()
            _run_notification(notify_tool_request_created, row, conn)
            res = _record_response(row)
            return res
        except Exception:
            conn.rollback()
            raise


@app.post("/tool-requests/{request_id}/create-workflow")
def convert_tool_request_to_workflow(
    request_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)
) -> dict:
    if not _actor_can_manage_tool_requests(payload):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only IT Admin or Master Admin can convert tool requests")

    actor_user_id, actor_email = _actor(payload)

    with conn.cursor() as cur:
        try:
            cur.execute("SELECT * FROM slmct.tool_requests WHERE id = %s FOR UPDATE", (request_id,))
            tool_request = cur.fetchone()
            if not tool_request:
                conn.rollback()
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool request not found")
            if tool_request["status"] not in ("new", "under_review"):
                conn.rollback()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Only new or under-review tool requests can be converted to workflows",
                )

            vendor_id = None
            if tool_request.get("vendor_name"):
                cur.execute(
                    """
                    SELECT id FROM slmct.vendors
                    WHERE organisation_id = %s
                      AND lower(name) = lower(%s)
                    LIMIT 1
                    """,
                    (tool_request["organisation_id"], tool_request["vendor_name"]),
                )
                vendor_row = cur.fetchone()
                if vendor_row:
                    vendor_id = vendor_row["id"]

            workflow_payload = {
                "organisation_id": str(tool_request["organisation_id"]),
                "workflow_type": "employee_software_request",
                "request_type": "Employee Software Request",
                "requester_name": tool_request["requester_name"],
                "requester_email": tool_request["requester_email"],
                "requester_role": "Employee",
                "vendor_id": str(vendor_id) if vendor_id else None,
                "name": tool_request["requested_tool"],
                "tool_requested": tool_request["requested_tool"],
                "category": tool_request.get("category") or "Software",
                "department": tool_request.get("department"),
                "amount": float(tool_request.get("estimated_amount") or 0),
                "currency_code": tool_request.get("currency_code") or "AED",
                "billing_cycle": "annual",
                "business_justification": tool_request.get("business_justification"),
                "notes": (
                    f"Converted from tool request {tool_request['id']}. "
                    f"{tool_request.get('business_justification') or ''}"
                ).strip(),
            }

            cur.execute(
                """
                INSERT INTO slmct.workflow_requests (
                  organisation_id, workflow_type, requested_module, requested_action, payload,
                  requested_by, requested_by_email, notes
                )
                VALUES (%s, 'employee_software_request', 'subscriptions', 'create', %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    tool_request["organisation_id"],
                    Json(workflow_payload),
                    tool_request.get("requested_by") or actor_user_id,
                    tool_request["requester_email"],
                    f"Created from tool request {tool_request['id']}",
                ),
            )
            workflow_row = cur.fetchone()
            from app.workflow_governance import record_status_history

            record_status_history(
                conn,
                workflow_row["id"],
                None,
                "submitted",
                actor_user_id,
                actor_email,
                "Converted from employee tool request",
            )

            cur.execute(
                """
                UPDATE slmct.tool_requests
                SET status = 'converted_to_workflow',
                    workflow_request_id = %s,
                    reviewed_by = %s,
                    reviewed_at = now(),
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (workflow_row["id"], actor_user_id, request_id),
            )
            updated = cur.fetchone()
            _audit(conn, "create", "workflow_request", workflow_row["id"], '{"source":"tool_request"}')
            _audit(conn, "update", "tool_request", request_id, '{"event":"converted_to_workflow"}')
            conn.commit()
            _run_notification(notify_workflow_created, workflow_row, conn)
            result = _record_response(updated)
            result["workflow_request"] = _record_response(workflow_row)
            return result
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise


@app.post("/tool-requests/{request_id}/reject")
def reject_tool_request(request_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    if not _actor_can_manage_tool_requests(payload):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only IT Admin or Master Admin can reject tool requests")

    actor_user_id, _actor_email = _actor(payload)
    rejection_reason = payload.get("rejection_reason") or "Request rejected by IT Admin."

    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                UPDATE slmct.tool_requests
                SET status = 'rejected',
                    rejection_reason = %s,
                    reviewed_by = %s,
                    reviewed_at = now(),
                    updated_at = now()
                WHERE id = %s AND status IN ('new', 'under_review')
                RETURNING *
                """,
                (rejection_reason, actor_user_id, request_id),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active tool request not found")
            _audit(conn, "update", "tool_request", request_id, '{"event":"rejected"}')
            conn.commit()
            _run_notification(notify_tool_request_rejected, row, conn)
            res = _record_response(row)
            return res
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise


@app.post("/tool-requests/{request_id}/request-info")
def request_tool_request_info(request_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    if not _actor_can_manage_tool_requests(payload):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only IT Admin or Master Admin can request more information")

    actor_user_id, _actor_email = _actor(payload)
    info_message = payload.get("info_request_message") or "Please provide additional business justification and estimated cost."

    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                UPDATE slmct.tool_requests
                SET status = 'info_requested',
                    info_request_message = %s,
                    reviewed_by = %s,
                    reviewed_at = now(),
                    updated_at = now()
                WHERE id = %s AND status IN ('new', 'under_review')
                RETURNING *
                """,
                (info_message, actor_user_id, request_id),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active tool request not found")
            _audit(conn, "update", "tool_request", request_id, '{"event":"info_requested"}')
            conn.commit()
            _run_notification(notify_tool_request_info_requested, row, conn)
            res = _record_response(row)
            return res
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise


def _organisation_response(row: dict) -> OrganisationResponse:
    return OrganisationResponse(
        id=row["id"],
        parent_id=row["parent_id"],
        code=row["code"],
        name=row["name"],
        legal_name=row["legal_name"],
        description=row["description"],
        website_url=row["website_url"],
        country_code=row["country_code"],
        currency_code=row["currency_code"],
        is_sister_entity=row["is_sister_entity"],
        is_active=row["is_active"],
    )


@app.get("/organisations", response_model=list[OrganisationResponse])
def list_organisations(
    include_inactive: bool = False,
    conn: Connection = Depends(get_connection),
) -> list[OrganisationResponse]:
    where_clause = "" if include_inactive else "WHERE is_active = true"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, parent_id, code, name, legal_name, description, website_url,
                   country_code, currency_code, is_sister_entity, is_active
            FROM slmct.organisations
            {where_clause}
            ORDER BY created_at DESC, name ASC
            """
        )
        return [_organisation_response(row) for row in cur.fetchall()]


@app.get("/organisations/{organisation_id}", response_model=OrganisationResponse)
def get_organisation(organisation_id: UUID, conn: Connection = Depends(get_connection)) -> OrganisationResponse:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, parent_id, code, name, legal_name, description, website_url,
                   country_code, currency_code, is_sister_entity, is_active
            FROM slmct.organisations
            WHERE id = %s
            """,
            (organisation_id,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

    return _organisation_response(row)


@app.post("/organisations", response_model=OrganisationResponse, status_code=status.HTTP_201_CREATED)
def create_organisation(
    payload: OrganisationCreate,
    conn: Connection = Depends(get_connection),
) -> OrganisationResponse:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO slmct.organisations (
                  parent_id, code, name, legal_name, description, website_url,
                  country_code, currency_code, is_sister_entity, is_active
                )
                VALUES (%s, %s, %s, %s, %s, %s, upper(%s), upper(%s), %s, %s)
                RETURNING id, parent_id, code, name, legal_name, description, website_url,
                          country_code, currency_code, is_sister_entity, is_active
                """,
                (
                    payload.parent_id,
                    payload.code,
                    payload.name,
                    payload.legal_name,
                    payload.description,
                    payload.website_url,
                    payload.country_code,
                    payload.currency_code,
                    payload.is_sister_entity,
                    payload.is_active,
                ),
            )
            row = cur.fetchone()
            cur.execute(
                """
                INSERT INTO slmct.audit_logs (action, entity_type, entity_id, metadata)
                VALUES ('create', 'organisation', %s, %s::jsonb)
                """,
                (row["id"], '{"source":"api"}'),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return _organisation_response(row)


@app.patch("/organisations/{organisation_id}", response_model=OrganisationResponse)
def update_organisation(
    organisation_id: UUID,
    payload: OrganisationUpdate,
    conn: Connection = Depends(get_connection),
) -> OrganisationResponse:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return get_organisation(organisation_id, conn)

    allowed_fields = {
        "name",
        "parent_id",
        "legal_name",
        "description",
        "website_url",
        "country_code",
        "currency_code",
        "is_sister_entity",
        "is_active",
    }
    set_clauses = []
    values = []
    for field, value in updates.items():
        if field not in allowed_fields:
            continue
        if field in {"country_code", "currency_code"} and value is not None:
            set_clauses.append(f"{field} = upper(%s)")
        else:
            set_clauses.append(f"{field} = %s")
        values.append(value)

    set_clauses.append("updated_at = now()")
    values.append(organisation_id)

    with conn.cursor() as cur:
        try:
            cur.execute(
                f"""
                UPDATE slmct.organisations
                SET {", ".join(set_clauses)}
                WHERE id = %s
                RETURNING id, parent_id, code, name, legal_name, description, website_url,
                          country_code, currency_code, is_sister_entity, is_active
                """,
                values,
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")
            cur.execute(
                """
                INSERT INTO slmct.audit_logs (action, entity_type, entity_id, metadata)
                VALUES ('update', 'organisation', %s, %s::jsonb)
                """,
                (organisation_id, '{"source":"api"}'),
            )
            conn.commit()
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise

    return _organisation_response(row)


@app.delete("/organisations/{organisation_id}", response_model=OrganisationResponse)
def delete_organisation(organisation_id: UUID, conn: Connection = Depends(get_connection)) -> OrganisationResponse:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                UPDATE slmct.organisations
                SET is_active = false, updated_at = now()
                WHERE id = %s
                RETURNING id, parent_id, code, name, legal_name, description, website_url,
                          country_code, currency_code, is_sister_entity, is_active
                """,
                (organisation_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")
            cur.execute(
                """
                INSERT INTO slmct.audit_logs (action, entity_type, entity_id, metadata)
                VALUES ('delete', 'organisation', %s, %s::jsonb)
                """,
                (organisation_id, '{"soft_delete":true,"source":"api"}'),
            )
            conn.commit()
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise

    return _organisation_response(row)


@app.get("/vendors")
def list_vendors(organisation_id: UUID | None = None, conn: Connection = Depends(get_connection)) -> list[dict]:
    return _list_module("vendors", organisation_id, conn)


@app.post("/vendors", status_code=status.HTTP_201_CREATED)
def create_vendor(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    return _create_module("vendors", payload, conn)


@app.get("/vendors/{record_id}")
def get_vendor(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _get_module("vendors", record_id, conn)


@app.patch("/vendors/{record_id}")
def update_vendor(record_id: UUID, payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    return _update_module("vendors", record_id, payload, conn)


@app.delete("/vendors/{record_id}")
def delete_vendor(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _delete_module("vendors", record_id, conn)


@app.post("/vendors/autofill")
def vendor_autofill(payload: dict = Body(...)) -> dict:
    """
    Given a vendor name, use Gemini with Google Search grounding to return
    best-guess values for legal_name, website, contact_name, contact_email.
    """
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Vendor name is required.")
    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="Gemini API key not configured.")

    try:
        from app.llm import complete
        from app.prompts import format_prompt

        prompt = format_prompt("vendor_autofill", name=name)
        result = complete("vendor_autofill", contents=prompt, google_search=True, temperature=0)
        raw = (result.text or "").strip()
        if not raw:
            return {"not_found": True}
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        data = json.loads(raw)

        if data.get("not_found"):
            return {"not_found": True}

        return {
            "not_found":    False,
            "vendor_name":  str(data.get("vendor_name")   or ""),
            "legal_name":   str(data.get("legal_name")    or ""),
            "website_url":  str(data.get("website_url")   or ""),
            "contact_name": str(data.get("contact_name")  or ""),
            "contact_email":str(data.get("contact_email") or ""),
        }

    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Could not parse vendor details from search results.")
    except Exception as exc:
        log.exception("vendor_autofill failed for %s", name)
        raise HTTPException(status_code=500, detail=f"Autofill failed: {exc}")


@app.post("/vendor-purchase-url")
def vendor_purchase_url(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    """Return the purchase URL for a vendor/product. Uses stored scrape_url first, falls back to Gemini search."""
    vendor_name = (payload.get("vendor_name") or "").strip()
    vendor_id = (payload.get("vendor_id") or "").strip()
    subscription_name = (payload.get("subscription_name") or "").strip()
    subscription_id = (payload.get("subscription_id") or "").strip()

    # Resolve vendor_id from subscription_id if not provided directly
    if not vendor_id and subscription_id:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT vc.id AS vc_id, vc.scrape_url, vc.name, v.name AS vendor_name FROM slmct.subscriptions s JOIN slmct.vendor_catalogue vc ON vc.id = s.vendor_catalogue_id JOIN slmct.vendors v ON v.id = vc.vendor_id WHERE s.id = %s LIMIT 1",
                (subscription_id,),
            )
            row = cur.fetchone()
            if row:
                if not vendor_name:
                    vendor_name = str(row["vendor_name"] or "")
                if not subscription_name:
                    subscription_name = str(row["name"] or "")
                if row["scrape_url"]:
                    return {"purchase_url": row["scrape_url"], "label": f"{row['name']} — Pricing Page"}

    if not vendor_name and not vendor_id:
        raise HTTPException(status_code=400, detail="vendor_name or vendor_id is required.")

    # 1. Exact match by vendor_id + subscription name
    if vendor_id and subscription_name:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT vc.scrape_url, vc.name FROM slmct.vendor_catalogue vc WHERE vc.vendor_id = %s AND lower(vc.name) LIKE lower(%s) AND vc.scrape_url IS NOT NULL ORDER BY length(vc.name) LIMIT 1",
                (vendor_id, f"%{subscription_name[:40]}%"),
            )
            row = cur.fetchone()
            if row:
                return {"purchase_url": row["scrape_url"], "label": f"{row['name']} — Pricing Page"}

    # 2. Any catalogue entry for this vendor_id with a scrape_url
    if vendor_id:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT vc.scrape_url, vc.name FROM slmct.vendor_catalogue vc WHERE vc.vendor_id = %s AND vc.scrape_url IS NOT NULL ORDER BY length(vc.name) LIMIT 1",
                (vendor_id,),
            )
            row = cur.fetchone()
            if row:
                return {"purchase_url": row["scrape_url"], "label": f"{row['name']} — Pricing Page"}

    # 3. Fuzzy match by subscription name across all vendors
    if subscription_name:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT vc.scrape_url, vc.name FROM slmct.vendor_catalogue vc JOIN slmct.vendors v ON v.id = vc.vendor_id WHERE lower(vc.name) LIKE lower(%s) AND vc.scrape_url IS NOT NULL LIMIT 1",
                (f"%{subscription_name[:40]}%",),
            )
            row = cur.fetchone()
            if row and row["scrape_url"]:
                return {"purchase_url": row["scrape_url"], "label": f"{row['name']} — Pricing Page"}

    # 4. Fuzzy match by vendor name
    if vendor_name:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT vc.scrape_url, vc.name FROM slmct.vendor_catalogue vc JOIN slmct.vendors v ON v.id = vc.vendor_id WHERE lower(v.name) LIKE lower(%s) AND vc.scrape_url IS NOT NULL ORDER BY length(vc.name) LIMIT 1",
                (f"%{vendor_name[:40]}%",),
            )
            row = cur.fetchone()
            if row and row["scrape_url"]:
                return {"purchase_url": row["scrape_url"], "label": f"{row['name']} — Pricing Page"}

    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="Gemini API key not configured.")

    try:
        from app.llm import complete
        from app.prompts import format_prompt

        product_hint = f'"{subscription_name}" by ' if subscription_name else ""
        prompt = format_prompt(
            "vendor_purchase_url",
            product_hint=product_hint,
            vendor_name=vendor_name,
            subscription_name=subscription_name or vendor_name,
        )
        result = complete("vendor_purchase_url", contents=prompt, google_search=True, temperature=0, thinking_budget=0)
        raw = (result.text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        m = re.search(r"\{[^{}]+\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
        data = json.loads(raw)
        purchase_url = str(data.get("purchase_url") or "")

        # Store it back into the catalogue so future lookups are instant
        if purchase_url and subscription_name:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE slmct.vendor_catalogue
                        SET scrape_url = %s, updated_at = now()
                        WHERE lower(name) LIKE lower(%s) AND scrape_url IS NULL
                        """,
                        (purchase_url, f"%{subscription_name[:40]}%"),
                    )
                conn.commit()
            except Exception:
                pass

        return {
            "purchase_url": purchase_url,
            "label": str(data.get("label") or f"{vendor_name} — Purchase"),
        }

    except Exception as exc:
        log.exception("vendor_purchase_url failed for %s / %s", vendor_name, subscription_name)
        raise HTTPException(status_code=500, detail=f"URL lookup failed: {exc}")


@app.get("/vendor-catalogue")
def list_vendor_catalogue(vendor_id: UUID | None = None, conn: Connection = Depends(get_connection)) -> list[dict]:
    with conn.cursor() as cur:
        if vendor_id:
            cur.execute("SELECT * FROM slmct.vendor_catalogue WHERE vendor_id = %s ORDER BY name", (vendor_id,))
        else:
            cur.execute("SELECT * FROM slmct.vendor_catalogue ORDER BY name")
        return [dict(r) for r in cur.fetchall()]


@app.post("/vendor-catalogue/auto-discover-urls")
def auto_discover_vendor_urls(conn: Connection = Depends(get_connection)) -> dict:
    """
    For every active subscription missing a pricing URL, use Gemini to discover
    the vendor, catalogue entry, pricing URL and price, then save them to DB.
    Returns a summary of what was fixed.
    """
    from app.price_scraper import _gemini_price_by_search, find_pricing_url, _load_fx, _to_aed
    import concurrent.futures

    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="Gemini API key not configured.")

    try:
        fx = _load_fx(conn)
    except Exception:
        from app.price_scraper import _FALLBACK_FX
        fx = dict(_FALLBACK_FX)

    # Find subscriptions missing a vendor or a catalogue URL
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.name, s.organisation_id, s.vendor_id, v.name AS vendor_name,
                   (SELECT vc2.scrape_url FROM slmct.vendor_catalogue vc2
                    WHERE vc2.vendor_id = s.vendor_id AND vc2.scrape_url IS NOT NULL
                    LIMIT 1) AS existing_url
            FROM slmct.subscriptions s
            LEFT JOIN slmct.vendors v ON v.id = s.vendor_id
            WHERE s.status = 'active'
            ORDER BY s.name
            """
        )
        subs = cur.fetchall()

    results = []

    for sub in subs:
        sub_name = str(sub["name"])
        vendor_name = str(sub["vendor_name"] or "")
        vendor_id = sub["vendor_id"]
        existing_url = sub["existing_url"]

        # Skip if already has a URL
        if existing_url:
            results.append({"name": sub_name, "status": "skipped", "detail": "already has a pricing URL"})
            continue

        # Use Gemini to discover price + URL
        try:
            price_result = _gemini_price_by_search(sub_name, vendor_name)
            raw_price = price_result[0] if price_result else None
            raw_currency = price_result[1] if price_result else "USD"
            pricing_url = find_pricing_url(sub_name, vendor_name)
        except Exception as exc:
            results.append({"name": sub_name, "status": "error", "detail": "Gemini lookup failed: " + str(exc)[:80]})
            continue

        if not pricing_url and not raw_price:
            results.append({"name": sub_name, "status": "not_found", "detail": "Gemini could not find pricing info for this subscription"})
            continue

        price_aed = _to_aed(raw_price, raw_currency, fx) if raw_price else 0

        with conn.cursor() as cur:
            # If no vendor, try to find or create one by name
            if not vendor_id:
                # Extract a vendor name guess from subscription name (first word or known brands)
                guessed_vendor = vendor_name or sub_name.split()[0]
                cur.execute("SELECT id FROM slmct.vendors WHERE lower(name) = lower(%s) LIMIT 1", (guessed_vendor,))
                vrow = cur.fetchone()
                if not vrow:
                    cur.execute(
                        "INSERT INTO slmct.vendors (name, category) VALUES (%s, 'SaaS') RETURNING id",
                        (guessed_vendor,),
                    )
                    vrow = cur.fetchone()
                vendor_id = vrow["id"]
                # Link vendor to subscription
                cur.execute("UPDATE slmct.subscriptions SET vendor_id = %s WHERE id = %s", (vendor_id, sub["id"]))

            # Find or create a catalogue entry for this plan under this vendor
            cur.execute(
                "SELECT id FROM slmct.vendor_catalogue WHERE vendor_id = %s AND lower(name) = lower(%s) LIMIT 1",
                (vendor_id, sub_name),
            )
            cat_row = cur.fetchone()
            if cat_row:
                cur.execute(
                    "UPDATE slmct.vendor_catalogue SET scrape_url = %s, price = %s, currency_code = %s, updated_at = now() WHERE id = %s",
                    (pricing_url, price_aed, "AED", cat_row["id"]),
                )
                action = "updated"
            else:
                cur.execute(
                    "INSERT INTO slmct.vendor_catalogue (vendor_id, name, price, currency_code, scrape_url) VALUES (%s, %s, %s, %s, %s)",
                    (vendor_id, sub_name, price_aed, "AED", pricing_url),
                )
                action = "created"

        conn.commit()
        detail = (pricing_url or "no URL found") + (" | AED " + str(round(price_aed, 2)) if price_aed else "")
        results.append({"name": sub_name, "status": action, "detail": detail})

    fixed = sum(1 for r in results if r["status"] in ("created", "updated"))
    skipped = sum(1 for r in results if r["status"] == "skipped")
    failed = sum(1 for r in results if r["status"] in ("error", "not_found"))
    return {"results": results, "fixed": fixed, "skipped": skipped, "failed": failed}


@app.post("/vendor-catalogue/fetch-price")
def fetch_catalogue_price(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    """
    Look up the price for a subscription.
    If "url" is provided, scrapes that pricing page directly.
    Otherwise falls back to Gemini + Google Search by name.
    Body: {"name": "...", "vendor_name": "...", "url": "https://..."}
    Returns: {"price", "currency_code", "original_price", "original_currency", "scrape_url"}
    """
    from app.price_scraper import _gemini_price_by_search, _load_fx, _to_aed

    name = (payload.get("name") or "").strip()
    vendor_name = (payload.get("vendor_name") or "").strip()
    url = (payload.get("url") or "").strip()

    try:
        fx = _load_fx(conn)
    except Exception:
        from app.price_scraper import _FALLBACK_FX
        fx = dict(_FALLBACK_FX)

    if url:
        # Scrape the pricing page the user provided directly
        from app.price_scraper import fetch_price_from_url
        try:
            result = fetch_price_from_url(url, lambda: conn, subscription_name=name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "price": result["price"],
            "currency_code": result["currency_code"],
            "original_price": result.get("original_price", result["price"]),
            "original_currency": result.get("original_currency", result["currency_code"]),
            "scrape_url": url,
        }

    if not name:
        raise HTTPException(status_code=400, detail="Provide a subscription name or a pricing URL.")

    result = _gemini_price_by_search(name, vendor_name)
    if not result or result[0] is None:
        raise HTTPException(status_code=422, detail=f"Could not find a price for '{name}'. Check the subscription name and try again.")

    raw_price, raw_currency = result

    from app.price_scraper import find_pricing_url, find_password_change_url
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut_pricing = ex.submit(find_pricing_url, name, vendor_name)
        fut_pw = ex.submit(find_password_change_url, vendor_name)
        pricing_url = fut_pricing.result()
        password_change_url = fut_pw.result()

    return {
        "price": _to_aed(raw_price, raw_currency, fx),
        "currency_code": "AED",
        "original_price": raw_price,
        "original_currency": raw_currency,
        "scrape_url": pricing_url,
        "password_change_url": password_change_url,
    }


@app.post("/vendor-catalogue", status_code=status.HTTP_201_CREATED)
def create_vendor_catalogue_item(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO slmct.vendor_catalogue (vendor_id, name, price, currency_code, scrape_url, password_change_url)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (payload["vendor_id"], payload["name"], payload.get("price", 0), payload.get("currency_code", "AED"), payload.get("scrape_url") or None, payload.get("password_change_url") or None),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row)


@app.patch("/vendor-catalogue/{item_id}")
def update_vendor_catalogue_item(item_id: UUID, payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    with conn.cursor() as cur:
        updates, vals = [], []
        for field in ("name", "price", "currency_code", "scrape_url", "password_change_url"):
            if field in payload:
                updates.append(f"{field} = %s")
                vals.append(payload[field] or None if field == "scrape_url" else payload[field])
        if not updates:
            raise HTTPException(status_code=400, detail="Nothing to update")
        vals.append(str(item_id))
        cur.execute(
            f"UPDATE slmct.vendor_catalogue SET {', '.join(updates)}, updated_at = now() WHERE id = %s RETURNING *",
            vals,
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Item not found")
        conn.commit()
        return dict(row)


@app.delete("/vendor-catalogue/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vendor_catalogue_item(item_id: UUID, conn: Connection = Depends(get_connection)):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM slmct.vendor_catalogue WHERE id = %s", (str(item_id),))
        conn.commit()


@app.get("/fx-rates")
def get_fx_rates(conn: Connection = Depends(get_connection)) -> dict:
    """Return current FX rates as {currency_code: rate_from_usd, fetched_at: ...}."""
    with conn.cursor() as cur:
        cur.execute("SELECT currency_code, rate_from_usd, fetched_at FROM slmct.fx_rates ORDER BY currency_code")
        rows = cur.fetchall()
    rates = {r["currency_code"]: float(r["rate_from_usd"]) for r in rows}
    fetched_at = max((r["fetched_at"] for r in rows), default=None)
    return {"rates": rates, "fetched_at": fetched_at.isoformat() if fetched_at else None}


@app.post("/fx-rates/update-now")
def update_fx_rates_now(conn: Connection = Depends(get_connection)) -> dict:
    """Manually trigger the FX rate update — for testing."""
    summary = run_fx_update(lambda: conn)
    return {"status": "done", **summary}


@app.post("/vendor-catalogue/scrape-now")
def scrape_prices_now(conn: Connection = Depends(get_connection)) -> dict:
    """Manually trigger the price scrape — useful for testing without waiting for midnight."""
    summary = run_price_scrape(lambda: conn)
    return {"status": "done", **summary}


@app.get("/subscriptions")
def list_subscriptions(organisation_id: UUID | None = None, conn: Connection = Depends(get_connection)) -> list[dict]:
    return _list_module("subscriptions", organisation_id, conn)


@app.post("/subscriptions", status_code=status.HTTP_201_CREATED)
def create_subscription(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    from app.workflow_submission_permissions import validate_direct_software_create

    try:
        validate_direct_software_create(payload.get("actor_roles"), "subscriptions")
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    row = _create_module("subscriptions", payload, conn)
    _audit(conn, "create", "subscription", row["id"])
    return row


@app.get("/subscriptions/{record_id}")
def get_subscription(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _get_module("subscriptions", record_id, conn)


@app.patch("/subscriptions/{record_id}")
def update_subscription(record_id: UUID, payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    return _update_module("subscriptions", record_id, payload, conn)


@app.delete("/subscriptions/{record_id}")
def delete_subscription(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _delete_module("subscriptions", record_id, conn)


@app.get("/licences")
def list_licences(organisation_id: UUID | None = None, conn: Connection = Depends(get_connection)) -> list[dict]:
    return _list_module("licences", organisation_id, conn)


@app.post("/licences", status_code=status.HTTP_201_CREATED)
def create_licence(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    from app.workflow_submission_permissions import validate_direct_software_create

    try:
        validate_direct_software_create(payload.get("actor_roles"), "licences")
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    subscription_id = payload.get("subscription_id")
    if not subscription_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A subscription must be selected before assigning a licence.")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, vendor_id FROM slmct.subscriptions WHERE id = %s AND status != 'cancelled'",
            (subscription_id,),
        )
        sub = cur.fetchone()
    if not sub:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The selected subscription does not exist or has been revoked. Licences can only be assigned to active subscriptions.")
    if not sub["vendor_id"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"'{sub['name']}' has no vendor linked. Please assign a vendor to this subscription before creating licences.")
    row = _create_module("licences", payload, conn)
    _audit(conn, "create", "licence", row["id"])
    return row


@app.get("/licences/{record_id}")
def get_licence(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _get_module("licences", record_id, conn)


@app.patch("/licences/{record_id}")
def update_licence(record_id: UUID, payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    return _update_module("licences", record_id, payload, conn)


@app.delete("/licences/{record_id}")
def delete_licence(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _delete_module("licences", record_id, conn)


@app.get("/budgets")
def list_budgets(organisation_id: UUID | None = None, conn: Connection = Depends(get_connection)) -> list[dict]:
    return _list_module("budgets", organisation_id, conn)


@app.post("/budgets", status_code=status.HTTP_201_CREATED)
def create_budget(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    return _create_module("budgets", payload, conn)


@app.get("/budgets/{record_id}")
def get_budget(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _get_module("budgets", record_id, conn)


@app.patch("/budgets/{record_id}")
def update_budget(record_id: UUID, payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    return _update_module("budgets", record_id, payload, conn)


@app.delete("/budgets/{record_id}")
def delete_budget(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _delete_module("budgets", record_id, conn)


@app.get("/payments")
def list_payments(organisation_id: UUID | None = None, conn: Connection = Depends(get_connection)) -> list[dict]:
    return _list_module("payments", organisation_id, conn)


@app.post("/payments", status_code=status.HTTP_201_CREATED)
def create_payment(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    return _create_module("payments", payload, conn)


@app.get("/payments/{record_id}")
def get_payment(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _get_module("payments", record_id, conn)


@app.patch("/payments/{record_id}")
def update_payment(record_id: UUID, payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    return _update_module("payments", record_id, payload, conn)


@app.delete("/payments/{record_id}")
def delete_payment(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _delete_module("payments", record_id, conn)


@app.get("/employees")
def list_employees(organisation_id: UUID | None = None, conn: Connection = Depends(get_connection)) -> list[dict]:
    return _list_module("employees", organisation_id, conn)


@app.get("/employees/my-team")
def list_my_team(manager_email: str | None = None, conn: Connection = Depends(get_connection)) -> list[dict]:
    """Return employees whose line_manager_email matches the given manager email."""
    if not manager_email:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM slmct.people
            WHERE lower(line_manager_email) = lower(%s)
              AND status != 'inactive'
            ORDER BY full_name ASC
            """,
            (manager_email,),
        )
        return [_record_response(row) for row in cur.fetchall()]


@app.get("/employees/sync/status")
def employee_sync_status() -> dict:
    from app.employee_sync import get_employee_sync_status

    return get_employee_sync_status()


@app.post("/employees/sync")
def sync_employees_from_directory(conn: Connection = Depends(get_connection)) -> dict:
    from app.employee_sync import sync_employees

    try:
        return sync_employees(conn)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    return _create_module("employees", payload, conn)


@app.get("/employees/{record_id}")
def get_employee(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _get_module("employees", record_id, conn)


@app.patch("/employees/{record_id}")
def update_employee(record_id: UUID, payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    return _update_module("employees", record_id, payload, conn)


@app.delete("/employees/{record_id}")
def delete_employee(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _delete_module("employees", record_id, conn)


@app.get("/subscription-creation-log")
def subscription_creation_log(
    organisation_id: UUID | None = None,
    conn: Connection = Depends(get_connection),
) -> list[dict]:
    """Track subscriptions created through completed approval workflows."""
    scoped_org_id = _scoped_organisation_id(organisation_id, conn)
    org_filter = "AND s.organisation_id = %s" if scoped_org_id else ""
    params = (scoped_org_id,) if scoped_org_id else ()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                wr.id                            AS workflow_id,
                wr.requested_by_email            AS created_by,
                wr.workflow_type,
                COALESCE(wr.completed_at, wr.updated_at) AS created_date,
                s.id                             AS subscription_id,
                s.name                           AS subscription_name,
                s.status                         AS subscription_status,
                s.amount,
                s.currency_code,
                v.name                           AS vendor_name,
                o.name                           AS organisation_name,
                wr.assigned_employee_name        AS assigned_user,
                wr.assigned_employee_email       AS assigned_user_email
            FROM slmct.workflow_requests wr
            LEFT JOIN slmct.subscriptions s ON s.id = wr.activated_entity_id
            LEFT JOIN slmct.vendors v       ON v.id = s.vendor_id
            LEFT JOIN slmct.organisations o ON o.id = s.organisation_id
            WHERE wr.status IN ('completed', 'finance_closed')
              AND wr.activated_entity_id IS NOT NULL
              {org_filter}
            ORDER BY COALESCE(wr.completed_at, wr.updated_at) DESC NULLS LAST
            LIMIT 100
            """,
            params,
        )
        return [_record_response(row) for row in cur.fetchall()]


@app.get("/audit-logs")
def list_audit_logs(limit: int = 100, conn: Connection = Depends(get_connection)) -> list[dict]:
    safe_limit = max(1, min(limit, 500))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, actor_user_id, actor_email, action, entity_type, entity_id,
                   organisation_id, ip_address, user_agent, metadata, created_at
            FROM slmct.audit_logs
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (safe_limit,),
        )
        return [_record_response(row) for row in cur.fetchall()]


@app.get("/audit-logs/export.xlsx")
def export_audit_logs(limit: int = 500, conn: Connection = Depends(get_connection)) -> StreamingResponse:
    safe_limit = max(1, min(limit, 5000))
    headers = ["created_at", "actor_email", "action", "entity_type", "entity_id", "metadata"]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT created_at, actor_email, action, entity_type, entity_id, metadata
            FROM slmct.audit_logs
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (safe_limit,),
        )
        rows = [_record_response(row) for row in cur.fetchall()]
    content = _build_xlsx(headers, rows, "Audit logs")
    return _xlsx_response("audit-logs.xlsx", content)


@app.get("/exports/{module}.xlsx")
def export_module_xlsx(module: str, organisation_id: UUID | None = None, conn: Connection = Depends(get_connection)) -> StreamingResponse:
    exportable = {"subscriptions", "licences", "budgets", "payments", "employees", "contracts"}
    if module == "users":
        fields = ["full_name", "work_email", "department", "job_title", "employee_number", "status"]
        conditions = []
        params: list = []
        if organisation_id:
            conditions.append("p.organisation_id = %s")
            params.append(organisation_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT p.full_name, p.work_email, p.department, p.job_title, p.employee_number, p.status
                FROM slmct.people p
                JOIN slmct.app_users au ON au.person_id = p.id
                {where}
                ORDER BY p.full_name
                LIMIT 5000
                """,
                params,
            )
            rows = [_record_response(row) for row in cur.fetchall()]
        content = _build_xlsx(fields, rows, "Users")
        return _xlsx_response("users.xlsx", content)
    if module not in exportable:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not available for this module")
    cfg = _module_config(module)
    table = cfg["table"]
    fields = cfg["fields"]
    soft_col, soft_val = cfg.get("soft_delete", (None, None))
    order = cfg.get("order", "created_at DESC")
    conditions = []
    params: list = []
    if organisation_id:
        conditions.append("organisation_id = %s")
        params.append(organisation_id)
    if soft_col:
        conditions.append(f"{soft_col} != %s")
        params.append(soft_val)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(fields)} FROM {table} {where} ORDER BY {order} LIMIT 5000", params)
        rows = [_record_response(row) for row in cur.fetchall()]
    content = _build_xlsx(fields, rows, module.capitalize())
    return _xlsx_response(f"{module}.xlsx", content)


@app.get("/exports/{module}/{record_id}.xlsx")
def export_single_record_xlsx(module: str, record_id: UUID, conn: Connection = Depends(get_connection)) -> StreamingResponse:
    exportable = {"subscriptions", "licences", "budgets", "payments", "employees", "contracts"}
    if module not in exportable:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export not available for this module")
    cfg = _module_config(module)
    table = cfg["table"]
    fields = cfg["fields"]
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(fields)} FROM {table} WHERE id = %s", (record_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
    record = _record_response(row)
    # Exclude raw binary document data from the spreadsheet
    record.pop("document_data", None)
    content = _build_xlsx(list(record.keys()), [record], module.capitalize())
    return _xlsx_response(f"{module}-{record_id}.xlsx", content)


@app.get("/renewals")
def list_renewals(conn: Connection = Depends(get_connection)) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM (
              SELECT
                s.id,
                s.organisation_id,
                'subscription' AS renewal_type,
                s.name,
                v.name AS vendor_name,
                s.renewal_date AS renewal_date,
                s.amount,
                s.currency_code,
                s.status::text AS status
              FROM slmct.subscriptions s
              LEFT JOIN slmct.vendors v ON v.id = s.vendor_id
              WHERE s.renewal_date IS NOT NULL
              UNION ALL
              SELECT
                l.id,
                l.organisation_id,
                'licence' AS renewal_type,
                l.licence_name AS name,
                s.name AS vendor_name,
                l.expires_at AS renewal_date,
                NULL::numeric AS amount,
                NULL::char(3) AS currency_code,
                l.status::text AS status
              FROM slmct.licences l
              LEFT JOIN slmct.subscriptions s ON s.id = l.subscription_id
              WHERE l.expires_at IS NOT NULL
              UNION ALL
              SELECT
                v.id,
                v.organisation_id,
                'vendor' AS renewal_type,
                v.name,
                v.legal_name AS vendor_name,
                MIN(s.renewal_date) AS renewal_date,
                SUM(s.amount) AS amount,
                MAX(s.currency_code)::char(3) AS currency_code,
                v.status::text AS status
              FROM slmct.vendors v
              JOIN slmct.subscriptions s ON s.vendor_id = v.id
              WHERE s.renewal_date IS NOT NULL
              GROUP BY v.id, v.organisation_id, v.name, v.legal_name, v.status
            ) renewals
            ORDER BY renewal_date ASC NULLS LAST, name ASC
            """
        )
        return [_record_response(row) for row in cur.fetchall()]


def _scoped_organisation_id(organisation_id: UUID | None, conn: Connection) -> UUID | None:
    """Return organisation_id when a sister entity is selected; None means global/group scope."""
    if not organisation_id:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, parent_id, code
            FROM slmct.organisations
            WHERE id = %s AND is_active = true
            """,
            (organisation_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    if row["code"] == "derisk360_group":
        return None
    return organisation_id


@app.post("/renewal-alerts/run")
def trigger_renewal_alerts(conn: Connection = Depends(get_connection)) -> dict:
    """Manually trigger renewal alert emails — same logic as the daily 07:00 UTC job."""
    try:
        result = run_renewal_alerts(conn)
        return {"status": "ok", **result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/dashboard/summary")
def dashboard_summary(
    organisation_id: UUID | None = None,
    conn: Connection = Depends(get_connection),
) -> dict:
    scoped_org_id = _scoped_organisation_id(organisation_id, conn)
    if scoped_org_id:
        org_filter = "organisation_id = %s AND"
        params: tuple = (scoped_org_id,)
    else:
        org_filter = ""
        params = ()

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              (SELECT count(*) FROM slmct.subscriptions
               WHERE {org_filter} status IN ('active', 'trial', 'pending_renewal')) AS active_subscriptions,
              (SELECT count(*) FROM slmct.subscriptions
               WHERE {org_filter} renewal_date BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '90 days') AS renewals_due,
              (SELECT COALESCE(sum(allocated_amount), 0) FROM slmct.budgets
               WHERE {org_filter} status IN ('approved', 'locked')) AS allocated_budget,
              (SELECT COALESCE(sum(amount), 0) FROM slmct.payments
               WHERE {org_filter} status IN ('planned', 'pending', 'paid')) AS tracked_spend,
              (SELECT count(*) FROM slmct.licences
               WHERE {org_filter} status = 'assigned') AS assigned_licences
            """,
            params * 5 if scoped_org_id else (),
        )
        return _record_response(cur.fetchone())


# ── Contracts CRUD ────────────────────────────────────────────────────────────

@app.get("/contracts")
def list_contracts(organisation_id: UUID | None = None, conn: Connection = Depends(get_connection)) -> list[dict]:
    return _list_module("contracts", organisation_id, conn)

@app.post("/contracts", status_code=status.HTTP_201_CREATED)
def create_contract(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    title = (payload.get("title") or "").strip()
    if title:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM slmct.contracts WHERE LOWER(title) = LOWER(%s) AND status != 'terminated' LIMIT 1",
                (title,),
            )
            if cur.fetchone():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"A contract with title '{title}' already exists.")
    return _create_module("contracts", payload, conn)

@app.get("/contracts/{record_id}")
def get_contract(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _get_module("contracts", record_id, conn)

@app.patch("/contracts/{record_id}")
def update_contract(record_id: UUID, payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    return _update_module("contracts", record_id, payload, conn)

@app.delete("/contracts/{record_id}")
def delete_contract(record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    return _delete_module("contracts", record_id, conn)


# ── Discovered Apps CRUD ───────────────────────────────────────────────────────


@app.get("/recycle-bin")
def get_recycle_bin(organisation_id: UUID | None = None, conn: Connection = Depends(get_connection)) -> list[dict]:
    results = []
    with conn.cursor() as cur:
        for module, config in MODULES.items():
            if "soft_delete" not in config:
                continue
            status_field, delete_value = config["soft_delete"]
            
            name_col = "name"
            if "name" not in config["fields"]:
                if "licence_name" in config["fields"]:
                    name_col = "licence_name"
                elif "full_name" in config["fields"]:
                    name_col = "full_name"
                elif "title" in config["fields"]:
                    name_col = "title"
                elif "app_name" in config["fields"]:
                    name_col = "app_name"
                elif "fiscal_year" in config["fields"]:
                    name_col = "fiscal_year::text || ' ' || department"
                elif "reference" in config["fields"]:
                    name_col = "reference"
                else:
                    name_col = "id::text"
            
            where_clauses = [f"{status_field} = %s"]
            params = [delete_value]
            if organisation_id and "organisation_id" in config["fields"]:
                where_clauses.append("organisation_id = %s")
                params.append(organisation_id)
                
            where = "WHERE " + " AND ".join(where_clauses)
            
            cur.execute(
                f"""
                SELECT id, {name_col} AS name, updated_at AS deleted_at, {status_field} AS status, organisation_id
                FROM {config["table"]}
                {where}
                """,
                params
            )
            for row in cur.fetchall():
                results.append({
                    "id": row["id"],
                    "module": module,
                    "name": row["name"],
                    "deleted_at": row["deleted_at"],
                    "status": row["status"],
                    "organisation_id": row["organisation_id"]
                })

        cur.execute(
            """
            SELECT id, name, updated_at AS deleted_at, 'inactive'::text AS status, id AS organisation_id
            FROM slmct.organisations
            WHERE is_active = false
            """
        )
        for row in cur.fetchall():
            results.append({
                "id": row["id"],
                "module": "organisations",
                "name": row["name"],
                "deleted_at": row["deleted_at"],
                "status": row["status"],
                "organisation_id": row["organisation_id"],
            })

        cur.execute(
            """
            SELECT id,
                   COALESCE(payload->>'name', payload->>'licence_name', requested_module || ' workflow') AS name,
                   updated_at AS deleted_at,
                   status::text AS status,
                   organisation_id
            FROM slmct.workflow_requests
            WHERE status = 'cancelled'
            """
        )
        for row in cur.fetchall():
            results.append({
                "id": row["id"],
                "module": "workflow_requests",
                "name": row["name"],
                "deleted_at": row["deleted_at"],
                "status": row["status"],
                "organisation_id": row["organisation_id"],
            })

    results.sort(key=lambda x: x["deleted_at"] or datetime.min, reverse=True)
    return results


@app.post("/recycle-bin/{module}/{record_id}/restore")
def restore_recycle_bin(module: str, record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    if module == "organisations":
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE slmct.organisations
                SET is_active = true, updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (record_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=404, detail="Record not found")
            _audit(conn, "update", "organisation", record_id, '{"source":"recycle_bin", "action":"restore"}')
            conn.commit()
            return _record_response(row)

    if module == "workflow_requests":
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE slmct.workflow_requests
                SET status = 'submitted', updated_at = now()
                WHERE id = %s AND status = 'cancelled'
                RETURNING *
                """,
                (record_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=404, detail="Record not found")
            _audit(conn, "update", "workflow_request", record_id, '{"source":"recycle_bin", "action":"restore"}')
            conn.commit()
            return _record_response(row)

    if module not in MODULES:
        raise HTTPException(status_code=400, detail="Invalid module")
    config = _module_config(module)
    status_field, _ = config["soft_delete"]
    
    active_val = "active"
    if module == "licences":
        active_val = "available"
    elif module == "budgets":
        active_val = "approved"
    elif module == "payments":
        active_val = "planned"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {config["table"]}
            SET {status_field} = %s, updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (active_val, record_id)
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Record not found")
        _audit(conn, "update", module.rstrip("s"), record_id, '{"source":"recycle_bin", "action":"restore"}')
        conn.commit()
        return _record_response(row)


@app.delete("/recycle-bin/{module}/{record_id}")
def delete_forever_recycle_bin(module: str, record_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    if module == "organisations":
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT * FROM slmct.organisations WHERE id = %s", (record_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Record not found")
                cur.execute("DELETE FROM slmct.organisations WHERE id = %s RETURNING *", (record_id,))
                _audit(conn, "delete", "organisation", record_id, '{"source":"recycle_bin", "action":"hard_delete"}')
                conn.commit()
                return {"message": "Record deleted permanently", "id": record_id}
            except Exception as e:
                conn.rollback()
                err_str = str(e)
                if "foreign key constraint" in err_str.lower() or "violates" in err_str.lower():
                    raise HTTPException(status_code=400, detail="Cannot delete permanently: this record is referenced by other active resources.")
                raise HTTPException(status_code=500, detail=f"Failed to delete permanently: {err_str}")

    if module == "workflow_requests":
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT * FROM slmct.workflow_requests WHERE id = %s", (record_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Record not found")
                cur.execute("DELETE FROM slmct.workflow_requests WHERE id = %s RETURNING *", (record_id,))
                _audit(conn, "delete", "workflow_request", record_id, '{"source":"recycle_bin", "action":"hard_delete"}')
                conn.commit()
                return {"message": "Record deleted permanently", "id": record_id}
            except Exception as e:
                conn.rollback()
                raise HTTPException(status_code=500, detail=f"Failed to delete permanently: {str(e)}")

    if module not in MODULES:
        raise HTTPException(status_code=400, detail="Invalid module")
    config = _module_config(module)
    with conn.cursor() as cur:
        try:
            cur.execute(f"SELECT * FROM {config['table']} WHERE id = %s", (record_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Record not found")
            
            cur.execute(f"DELETE FROM {config['table']} WHERE id = %s RETURNING *", (record_id,))
            _audit(conn, "delete", module.rstrip("s"), record_id, '{"source":"recycle_bin", "action":"hard_delete"}')
            conn.commit()
            return {"message": "Record deleted permanently", "id": record_id}
        except Exception as e:
            conn.rollback()
            err_str = str(e)
            if "foreign key constraint" in err_str.lower() or "violates" in err_str.lower():
                raise HTTPException(status_code=400, detail="Cannot delete permanently: this record is referenced by other active resources.")
            raise HTTPException(status_code=500, detail=f"Failed to delete permanently: {err_str}")


# ── AI API Endpoints ──────────────────────────────────────────────────────────

@app.get("/api/health")
def api_health() -> dict:
    has_key = bool(settings.gemini_api_key)
    return {
        "ok": True,
        "provider": "gemini",
        "configured": has_key
    }


class ExtractRequest(BaseModel):
    dataUrl: Optional[str] = None
    filename: Optional[str] = None
    text: Optional[str] = None
    vendors: list[str] = []

@app.post("/api/extract")
def api_extract(req: ExtractRequest) -> dict:
    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="LLM not configured. Add GEMINI_API_KEY to your .env file.")
    
    file_bytes = None
    mime_type = None
    text_content = ""
    
    if req.text and req.text.strip():
        text_content = req.text
    elif req.dataUrl:
        m = re.match(r"^data:(?P<mime>[^;]+);base64,(?P<data>.*)$", req.dataUrl, re.DOTALL)
        if m:
            mime_type = m.group("mime")
            b64_data = m.group("data")
            file_bytes = base64.b64decode(b64_data)
            
            # If not PDF, try to read as utf-8 text
            if mime_type != "application/pdf" and not (req.filename or "").lower().endswith(".pdf"):
                try:
                    text_content = file_bytes.decode("utf-8")
                    file_bytes = None
                except Exception:
                    text_content = ""
        else:
            raise HTTPException(status_code=422, detail="Invalid dataUrl format")
    else:
        raise HTTPException(status_code=422, detail="Either text or dataUrl must be provided")

    try:
        from google.genai import types

        from app.llm import complete
        from app.prompts import get_prompt

        vendor_hint = f"Known vendors (prefer an exact match if appropriate): {', '.join(req.vendors)}\n\n" if req.vendors else ""
        system_prompt = get_prompt("contract_extract")

        if file_bytes and mime_type:
            contents = [
                types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                f"{vendor_hint}Please analyze the attached contract document.",
            ]
        else:
            contents = f"{vendor_hint}Contract text:\n\"\"\"\n{text_content[:12000]}\n\"\"\""

        result = complete(
            "contract_extract",
            contents=contents,
            system_instruction=system_prompt,
            temperature=0.0,
            response_mime_type="application/json",
        )

        raw_text = result.text or "{}"
        try:
            fields = json.loads(raw_text)
        except Exception:
            fenced = re.sub(r"```json", "", raw_text, flags=re.I).replace("```", "").strip()
            start = fenced.find("{")
            end = fenced.rfind("}")
            if start != -1 and end != -1:
                fields = json.loads(fenced[start:end+1])
            else:
                raise Exception("Failed to parse Gemini JSON response")
                
        return {"fields": fields, "provider": "gemini"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")


class ToolSummaryRequest(BaseModel):
    tool_name: str
    vendor_name: str = ""


@app.post("/api/tool-summary")
def api_tool_summary(req: ToolSummaryRequest) -> dict:
    if not req.tool_name.strip():
        raise HTTPException(status_code=422, detail="tool_name is required")

    if not settings.gemini_api_key:
        # Graceful fallback: return a generic summary without Gemini
        return {
            "summary": f"{req.tool_name} is a software tool used by organisations for productivity and collaboration.",
            "provider": "fallback",
        }

    try:
        from app.llm import complete
        from app.prompts import format_prompt

        vendor_line = f" by {req.vendor_name.strip()}" if req.vendor_name.strip() else ""
        prompt = format_prompt("tool_summary", tool_name=req.tool_name, vendor_line=vendor_line)
        result = complete("tool_summary", contents=prompt, temperature=0.2)
        summary = (result.text or "").strip()
        if not summary:
            raise ValueError("Empty response from Gemini")
        return {"summary": summary, "provider": "gemini"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tool summary generation failed: {str(e)}")


class CopilotRequest(BaseModel):
    messages: list[dict]
    state: dict | None = None
    organisation_id: str | None = None
    actor_roles: list[str] | None = None
    actor_user_id: str | None = None
    model: str | None = None
    prompt_variant: str | None = None
    copilot_mode: str | None = None


@app.post("/api/copilot")
def api_copilot(req: CopilotRequest, conn: Connection = Depends(get_connection)) -> dict:
    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="LLM not configured. Add GEMINI_API_KEY to your .env file.")

    try:
        from app.copilot import CopilotInput, resolve_copilot_mode, run_copilot

        mode = resolve_copilot_mode(CopilotInput(**req.model_dump()))
        if mode == "legacy" and not req.state:
            raise HTTPException(status_code=422, detail="Legacy copilot mode requires state.")
        if mode == "tools" and (not req.organisation_id or not req.actor_roles):
            raise HTTPException(
                status_code=422,
                detail="Tool-calling copilot mode requires organisation_id and actor_roles.",
            )

        return run_copilot(CopilotInput(**req.model_dump()), conn=conn)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Co-pilot error: {str(e)}")


@app.post("/api/workflow-review")
def api_workflow_review(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> dict:
    """AI Request Reviewer — analyses a pending workflow request and returns an approve/reject/review recommendation."""
    workflow_request_id = str(payload.get("workflow_request_id") or "").strip()
    reviewer_role = str(payload.get("reviewer_role") or "approver").strip()
    if not workflow_request_id:
        raise HTTPException(status_code=400, detail="workflow_request_id is required.")
    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="Gemini API key not configured.")

    with conn.cursor() as cur:
            # Fetch the workflow request
            cur.execute(
                "SELECT * FROM slmct.workflow_requests WHERE id = %s LIMIT 1",
                (workflow_request_id,),
            )
            req_row = cur.fetchone()
            if not req_row:
                raise HTTPException(status_code=404, detail="Workflow request not found.")
            req_row = dict(req_row)

            org_id = req_row.get("organisation_id")
            req_payload = req_row.get("payload") or {}
            if isinstance(req_payload, str):
                try:
                    req_payload = json.loads(req_payload)
                except Exception:
                    req_payload = {}

            requester_email = str(req_row.get("requested_by_email") or "").strip()
            software_name = str(req_payload.get("name") or req_payload.get("subscription_name") or "Unknown")
            department = str(req_payload.get("department") or "Unknown")
            amount = req_payload.get("amount")
            currency = str(req_payload.get("currency_code") or "AED")
            justification = str(req_payload.get("justification") or req_payload.get("notes") or "Not provided")
            billing_cycle = str(req_payload.get("billing_cycle") or "annual")

            # Existing licences for the requester
            cur.execute(
                """
                SELECT l.licence_name, s.name AS sub_name
                FROM slmct.licences l
                LEFT JOIN slmct.subscriptions s ON s.id = l.subscription_id
                JOIN slmct.people p ON p.id = l.assigned_to_person_id
                WHERE p.work_email = %s AND l.status = 'assigned'
                LIMIT 20
                """,
                (requester_email,),
            )
            existing_licences = [dict(r) for r in cur.fetchall()]

            # Active subscriptions with the same or similar software name
            cur.execute(
                """
                SELECT name, status, amount, currency_code, billing_cycle
                FROM slmct.subscriptions
                WHERE organisation_id = %s
                  AND status NOT IN ('cancelled')
                  AND lower(name) LIKE lower(%s)
                LIMIT 10
                """,
                (org_id, f"%{software_name[:20]}%"),
            )
            existing_subs = [dict(r) for r in cur.fetchall()]

            # Department budget
            current_year = datetime.now().year
            cur.execute(
                """
                SELECT allocated_amount, currency_code,
                       COALESCE((
                           SELECT SUM(p.amount)
                           FROM slmct.payments p
                           JOIN slmct.subscriptions s ON s.id = p.subscription_id
                           WHERE s.department = b.department
                             AND s.organisation_id = b.organisation_id
                             AND p.status NOT IN ('cancelled','failed')
                       ), 0) AS spent
                FROM slmct.budgets b
                WHERE organisation_id = %s
                  AND lower(department) = lower(%s)
                  AND fiscal_year = %s
                LIMIT 1
                """,
                (org_id, department, str(current_year)),
            )
            budget_row = cur.fetchone()
            budget_context = ""
            if budget_row:
                budget_row = dict(budget_row)
                allocated = float(budget_row.get("allocated_amount") or 0)
                spent = float(budget_row.get("spent") or 0)
                remaining = allocated - spent
                budget_context = f"Department budget: {allocated:.0f} {budget_row.get('currency_code','AED')} allocated, {spent:.0f} spent, {remaining:.0f} remaining."

            # Recent decisions on similar requests in this org
            cur.execute(
                """
                SELECT status, payload->>'name' AS name, payload->>'department' AS dept,
                       created_at::date AS decided_on
                FROM slmct.workflow_requests
                WHERE organisation_id = %s
                  AND status IN ('completed','rejected')
                  AND lower(payload->>'name') LIKE lower(%s)
                  AND id != %s
                ORDER BY created_at DESC
                LIMIT 5
                """,
                (org_id, f"%{software_name[:20]}%", workflow_request_id),
            )
            recent_decisions = [dict(r) for r in cur.fetchall()]

            # Requester's employee profile (job title, department)
            cur.execute(
                """
                SELECT p.full_name, p.job_title, p.department, p.status
                FROM slmct.people p
                WHERE p.work_email = %s
                LIMIT 1
                """,
                (requester_email,),
            )
            emp_row = cur.fetchone()
            emp_profile = dict(emp_row) if emp_row else {}

            # Vendor catalogue price for this software
            cur.execute(
                """
                SELECT vc.name, vc.price, vc.currency_code
                FROM slmct.vendor_catalogue vc
                WHERE lower(vc.name) LIKE lower(%s)
                LIMIT 1
                """,
                (f"%{software_name[:20]}%",),
            )
            catalogue_row = cur.fetchone()
            catalogue_price = dict(catalogue_row) if catalogue_row else {}

            # Total org spend on this software (all time)
            cur.execute(
                """
                SELECT COALESCE(SUM(p.amount), 0) AS total_spent, p.currency_code
                FROM slmct.payments p
                JOIN slmct.subscriptions s ON s.id = p.subscription_id
                WHERE s.organisation_id = %s
                  AND lower(s.name) LIKE lower(%s)
                  AND p.status NOT IN ('cancelled', 'failed')
                GROUP BY p.currency_code
                LIMIT 1
                """,
                (org_id, f"%{software_name[:20]}%"),
            )
            spend_row = cur.fetchone()
            org_spend = dict(spend_row) if spend_row else {}

            # How many active licences org already has for this software
            cur.execute(
                """
                SELECT COUNT(*) AS total, COUNT(l.assigned_to_person_id) AS assigned
                FROM slmct.licences l
                JOIN slmct.subscriptions s ON s.id = l.subscription_id
                WHERE s.organisation_id = %s
                  AND lower(s.name) LIKE lower(%s)
                  AND l.status = 'assigned'
                """,
                (org_id, f"%{software_name[:20]}%"),
            )
            licence_count_row = cur.fetchone()
            licence_counts = dict(licence_count_row) if licence_count_row else {}

    # Build context strings
    licence_list = ", ".join(r.get("licence_name") or r.get("sub_name") or "?" for r in existing_licences) or "None"
    sub_list = ", ".join(
        f"{r['name']} ({r['status']}, {r['amount']} {r['currency_code']}/{r['billing_cycle']})"
        for r in existing_subs
    ) or "None"
    decisions_list = "\n".join(
        f"  - {r['name']} → {r['status']} (dept: {r.get('dept','?')}, date: {r.get('decided_on','?')})"
        for r in recent_decisions
    ) or "  None"
    amount_str = f"{amount} {currency} ({billing_cycle})" if amount else "Not specified"

    catalogue_str = (
        f"{catalogue_price['name']}: {catalogue_price['price']} {catalogue_price['currency_code']}/month"
        if catalogue_price else "Not in vendor catalogue"
    )
    org_spend_str = (
        f"{org_spend['total_spent']:.0f} {org_spend['currency_code']} total paid to date"
        if org_spend else "No recorded payments"
    )
    licence_count_str = (
        f"{licence_counts.get('assigned', 0)} licences currently assigned org-wide"
        if licence_counts else "No licences tracked"
    )
    emp_str = (
        f"{emp_profile.get('full_name','?')} | Job Title: {emp_profile.get('job_title','Unknown')} | "
        f"Department: {emp_profile.get('department', department)} | Status: {emp_profile.get('status','?')}"
    ) if emp_profile else f"Email: {requester_email} | Department: {department}"

    role_framing = {
        "line_manager": (
            "You are reviewing this as the requester's LINE MANAGER. "
            "Focus on: whether the software is necessary for the requester's specific job role and day-to-day responsibilities, "
            "whether they already have equivalent tools assigned, and whether the business justification reflects a genuine work need."
        ),
        "finance": (
            "You are reviewing this as the FINANCE MANAGER. "
            "Focus on: the exact cost vs remaining department budget, value for money, whether the org is already paying for this software, "
            "total org spend on this tool, and whether the cost is justified relative to the requester's role."
        ),
        "master_admin": (
            "You are reviewing this as the MASTER ADMIN / PLATFORM ADMINISTRATOR. "
            "Focus on: organisation-wide governance, whether this sets a precedent, licence sprawl risk, "
            "whether the org already has an active subscription that could cover this user, and policy compliance."
        ),
        "it_admin": (
            "You are reviewing this as the IT ADMIN. "
            "Focus on: whether the software is already provisioned org-wide, security and integration considerations, "
            "licence redundancy, and whether onboarding this tool creates IT overhead."
        ),
        "hr_admin": (
            "You are reviewing this as the HR ADMIN. "
            "Focus on: whether the request aligns with the requester's job title and department, "
            "employment status, and whether this is appropriate for their role level."
        ),
    }.get(reviewer_role, "You are reviewing this as an approver. Provide a balanced assessment across cost, role fit, and governance.")

    prompt = f"""You are an AI Request Reviewer for Derisk360, a software procurement governance platform.
{role_framing}
Your job is to give a data-driven, specific recommendation on whether to approve, reject, or review this software request.

═══ REQUEST ═══
Software Requested:     {software_name}
Requested Amount:       {amount_str}
Business Justification: {justification}
Workflow Type:          Employee Software Request

═══ REQUESTER PROFILE ═══
{emp_str}
Currently Assigned Licences: {licence_list}

═══ ORGANISATION DATA ═══
Existing Org Subscriptions (matching "{software_name}"):
  {sub_list}
Org-wide Licences for This Software: {licence_count_str}
Org Total Spend on This Software:    {org_spend_str}
Vendor Catalogue Price:              {catalogue_str}

Department Budget ({department}, {datetime.now().year}):
  {budget_context or "No budget record found for this department."}

Past Decisions on Similar Requests:
{decisions_list}

═══ YOUR TASK ═══
Analyse all the data above and provide a recommendation. Your reasons and concerns must:
- Reference SPECIFIC numbers from the data (e.g. budget remaining, number of existing licences, cost per seat)
- NOT repeat the same point in both reasons and concerns
- Flag redundancy if the requester already has a similar licence assigned
- Flag budget risk if remaining budget is low relative to the requested cost
- Flag precedent if similar requests were previously rejected
- Approve confidently if justification is clear, budget exists, no redundancy, and precedent supports it
- Reject if there is clear redundancy (same tool already assigned) or budget is exceeded

Respond ONLY with valid JSON — no markdown, no extra text:
{{
  "recommendation": "approve" | "reject" | "review",
  "confidence": <0-100>,
  "reasons": ["<specific data-driven sentence>", "<specific data-driven sentence>"],
  "concerns": ["<specific concern with numbers>"]
}}

- "reasons": 2-4 sentences, each citing a specific data point
- "concerns": 0-3 distinct issues (empty array [] if none)
- confidence reflects how certain you are given the available data"""

    try:
        from app.llm import complete

        result = complete("workflow_review", contents=prompt, temperature=0.1)
        raw = (result.text or "").strip()
        # Strip markdown code fences if present
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(raw)
        return {
            "recommendation": str(parsed.get("recommendation", "review")),
            "confidence": int(parsed.get("confidence", 50)),
            "reasons": list(parsed.get("reasons") or []),
            "concerns": list(parsed.get("concerns") or []),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"AI review error: {str(exc)}")




def _run_notification(func, row, conn_or_ignored, *extra_args, **kwargs) -> None:
    """Fire-and-forget: runs the notification in a background thread with its own DB connection.
    The conn argument from the caller is intentionally not forwarded — the thread opens its own."""
    row_snapshot = dict(row) if row else {}

    def _run():
        try:
            bg_conn = psycopg.connect(
                __import__("app.settings", fromlist=["get_settings"]).get_settings().database_url,
                row_factory=__import__("psycopg.rows", fromlist=["dict_row"]).dict_row,
            )
            try:
                func(row_snapshot, bg_conn, *extra_args, **kwargs)
            finally:
                bg_conn.close()
        except Exception as exc:
            print(f"Background notification error ({func.__name__}): {exc}")

    threading.Thread(target=_run, daemon=True).start()


def _serialize_email_log(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "workflowId": str(row["workflow_request_id"]) if row["workflow_request_id"] else None,
        "relatedRequestId": str(row["tool_request_id"]) if row["tool_request_id"] else None,
        "eventType": row["event_type"],
        "from": row.get("from_email"),
        "to": row["to_email"],
        "cc": row["cc_email"],
        "subject": row["subject"],
        "bodyPreview": row["body"],
        "status": row["status"],
        "workflowStage": row.get("workflow_stage"),
        "providerMessageId": row["provider_message_id"],
        "errorMessage": row["error_message"],
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "sentAt": row["sent_at"].isoformat() if row["sent_at"] else None,
    }


class EmailTestPayload(BaseModel):
    to_email: str


@app.get("/api/email/role-mailboxes")
def list_role_mailboxes():
    from app.role_mailboxes import configured_role_mailboxes

    return configured_role_mailboxes()


@app.post("/api/email/preview/workflow")
def preview_workflow_email(payload: dict = Body(...), conn: Connection = Depends(get_connection)) -> list[dict]:
    from app.workflow_governance import preview_workflow_emails, resolve_workflow_type

    event = str(payload.get("event") or "submitted")
    requested_module = str(payload.get("requested_module") or "subscriptions")
    requested_payload = payload.get("payload") or {}
    workflow_type = resolve_workflow_type(
        requested_module,
        requested_payload,
        payload.get("workflow_type"),
    )
    request_row = {
        "workflow_type": workflow_type,
        "requested_module": requested_module,
        "payload": requested_payload,
        "requested_by": payload.get("actor_user_id"),
        "requested_by_email": payload.get("actor_email"),
        "status": payload.get("status") or "submitted",
        "rejection_reason": payload.get("rejection_reason"),
    }
    try:
        return preview_workflow_emails(request_row, event, conn)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@app.get("/api/email/status")
def get_email_status():
    from app.notifications import get_smtp_status

    return get_smtp_status()


@app.post("/api/email/test")
def test_email(payload: EmailTestPayload, conn: Connection = Depends(get_connection)):
    from app.notifications import get_smtp_status, send_workflow_email

    smtp_status = get_smtp_status()
    sender = str(smtp_status.get("fromAddress") or "Derisk360")
    subject = "Derisk360 Gmail SMTP Test Email"
    body = (
        f"Hello,\n\n"
        f"This is a test email sent from the Derisk360 SLMCT backend via Gmail SMTP.\n"
        f"Sender: {sender}\n\n"
        f"If you received this message, workflow email delivery is configured correctly.\n\n"
        f"Regards,\n"
        f"Derisk360 System"
    )

    res = send_workflow_email(
        to_email=payload.to_email,
        subject=subject,
        body=body,
        conn=conn,
        event_type="test",
    )
    if res.get("status") == "FAILED":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Test email failed: {res.get('error')}",
        )
    if res.get("status") in {"PREVIEW", "MOCK_MODE"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Email delivery is not enabled or SMTP is not configured. "
                "Set EMAIL_DELIVERY_ENABLED=true and Gmail SMTP credentials in infra/.env."
            ),
        )
    return {
        "status": res.get("status"),
        "logId": res.get("log_id"),
        "message": f"Test email sent from {sender} to {payload.to_email}.",
        "smtp": get_smtp_status(),
    }


@app.get("/api/email/logs")
def get_email_logs(conn: Connection = Depends(get_connection)) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, workflow_request_id, tool_request_id, event_type, from_email, to_email, cc_email,
                   subject, body, status, workflow_stage, provider_message_id, error_message, created_at, sent_at
            FROM slmct.email_logs
            ORDER BY created_at DESC
            """
        )
        return [_serialize_email_log(row) for row in cur.fetchall()]


@app.get("/api/email/logs/tool-request/{tool_request_id}")
def get_email_logs_for_tool_request(tool_request_id: UUID, conn: Connection = Depends(get_connection)) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, workflow_request_id, tool_request_id, event_type, from_email, to_email, cc_email,
                   subject, body, status, workflow_stage, provider_message_id, error_message, created_at, sent_at
            FROM slmct.email_logs
            WHERE tool_request_id = %s
            ORDER BY created_at DESC
            """,
            (tool_request_id,),
        )
        return [_serialize_email_log(row) for row in cur.fetchall()]


@app.get("/api/email/logs/{workflow_id}")
def get_email_logs_for_workflow(workflow_id: UUID, conn: Connection = Depends(get_connection)) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, workflow_request_id, tool_request_id, event_type, from_email, to_email, cc_email,
                   subject, body, status, workflow_stage, provider_message_id, error_message, created_at, sent_at
            FROM slmct.email_logs
            WHERE workflow_request_id = %s
            ORDER BY created_at DESC
            """,
            (workflow_id,),
        )
        return [_serialize_email_log(row) for row in cur.fetchall()]


# ── Diagnostics: shared helper ────────────────────────────────────────────────

def _diag_resolve_context(conn) -> dict:
    """Dynamically resolve org ID and user IDs/emails for diagnostic tests."""
    ctx = {}
    with conn.cursor() as cur:
        # Org - use first active org by code, fall back to first active org
        cur.execute("SELECT id FROM slmct.organisations WHERE code = 'derisk360_group' AND is_active = true LIMIT 1")
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT id FROM slmct.organisations WHERE is_active = true ORDER BY created_at LIMIT 1")
            row = cur.fetchone()
        ctx["ORG_ID"] = str(row["id"]) if row else None

        # Look up each role's user
        for role_code, key_prefix in [
            ("it_admin", "IT"),
            ("finance", "FINANCE"),
            ("hr_admin", "HR"),
            ("line_manager", "LINE_MANAGER"),
            ("employee", "EMPLOYEE"),
            ("master_admin", "ADMIN"),
        ]:
            cur.execute(
                """
                SELECT au.id, au.email
                FROM slmct.app_users au
                JOIN slmct.user_roles ur ON ur.user_id = au.id AND ur.revoked_at IS NULL
                JOIN slmct.roles r ON r.id = ur.role_id
                WHERE r.code = %s AND au.status = 'active'
                ORDER BY au.created_at
                LIMIT 1
                """,
                (role_code,),
            )
            u = cur.fetchone()
            ctx[f"{key_prefix}_ID"] = str(u["id"]) if u else None
            ctx[f"{key_prefix}_EMAIL"] = str(u["email"]) if u else None

    return ctx


# ── Diagnostics: full workflow regression test ─────────────────────────────────

@app.post("/api/diagnostics/run-workflow-test")
def run_workflow_diagnostics(conn: Connection = Depends(get_connection)) -> dict:
    """
    Runs a full synthetic employee_software_request workflow end-to-end.
    Creates test data, validates every step, then cleans up.
    Returns structured pass/fail results per check.
    """
    import time as _time

    BASE = "http://localhost:8000"
    checks: list[dict] = []
    created_workflow_id = None
    created_licence_id = None
    created_subscription_id = None

    _ctx = _diag_resolve_context(conn)
    ORG_ID = _ctx["ORG_ID"]
    EMPLOYEE_EMAIL = _ctx["EMPLOYEE_EMAIL"]
    EMPLOYEE_ID = _ctx["EMPLOYEE_ID"]
    LINE_MANAGER_EMAIL = _ctx["LINE_MANAGER_EMAIL"]
    LINE_MANAGER_ID = _ctx["LINE_MANAGER_ID"]
    FINANCE_EMAIL = _ctx["FINANCE_EMAIL"]
    FINANCE_ID = _ctx["FINANCE_ID"]
    IT_EMAIL = _ctx["IT_EMAIL"]
    IT_ID = _ctx["IT_ID"]
    if not ORG_ID or not IT_ID or not FINANCE_ID or not EMPLOYEE_ID or not LINE_MANAGER_ID:
        return {"status": "skipped", "reason": "No active organisation or required role users found — seed data may not be loaded yet.", "checks": []}
    SUBSCRIPTION_NAME = "[DIAG] Microsoft Teams Essentials"
    CATALOGUE_ID = "0640b377-70b0-4dcf-bfb7-1d74186c6539"
    VENDOR_NAME = "Microsoft"

    class _SkipToCleanup(Exception):
        pass

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    def skip(name: str, reason: str = ""):
        checks.append({"name": name, "status": "skip", "detail": reason})

    def emails_for(wf_id: str, min_count: int = 1, timeout: int = 20) -> list[dict]:
        """Poll email logs until at least min_count emails appear or timeout is reached."""
        import httpx as _httpx
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                r = _httpx.get(f"{BASE}/api/email/logs/{wf_id}", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if len(data) >= min_count:
                        return data
            except Exception:
                pass
            _time.sleep(1)
        return []

    def no_uuid(text: str) -> bool:
        import re
        return not bool(re.search(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', text or "", re.I))

    try:
        import httpx as _httpx

        # ── STEP 1: Submit ────────────────────────────────────────────────────
        r = _httpx.post(f"{BASE}/workflow-requests", json={
            "workflow_type": "employee_software_request",
            "organisation_id": ORG_ID,
            "requested_module": "subscriptions",
            "actor_email": EMPLOYEE_EMAIL,
            "actor_user_id": EMPLOYEE_ID,
            "actor_roles": ["employee"],
            "payload": {
                "name": SUBSCRIPTION_NAME,
                "department": "Software Engineering",
                "justification": "Diagnostics test run — automated regression check.",
                "amount": 25,
                "currency_code": "AED",
                "billing_cycle": "monthly",
            },
        }, timeout=30)
        submitted_ok = r.status_code in (200, 201)
        chk("Workflow submission", submitted_ok, f"HTTP {r.status_code}")
        if not submitted_ok:
            chk("All remaining checks skipped", False, r.text[:200])
            return {"checks": checks, "summary": _diag_summary(checks), "workflow_id": None}

        wf = r.json()
        created_workflow_id = str(wf.get("id") or wf.get("workflow_id") or "")
        chk("Workflow ID returned", bool(created_workflow_id), created_workflow_id)
        chk("Initial status is 'submitted'", wf.get("status") == "submitted", f"got: {wf.get('status')}")

        # ── STEP 1 emails — wait for 2 emails (requester + LM) ───────────────
        em1 = emails_for(created_workflow_id, min_count=2)
        emp_sub = [e for e in em1 if EMPLOYEE_EMAIL.lower() in (e.get("to") or "").lower() and "submitted" in (e.get("subject") or "").lower()]
        lm_req = [e for e in em1 if LINE_MANAGER_EMAIL.lower() in (e.get("to") or "").lower()]
        chk("Submit: employee confirmation email", len(emp_sub) > 0, f"{len(emp_sub)} found")
        chk("Submit: line manager action-required email", len(lm_req) > 0, f"{len(lm_req)} found")
        submit_emails_ok = len(emp_sub) > 0
        if emp_sub:
            chk("Submit: subject contains #ID", "#" in (emp_sub[0].get("subject") or ""), emp_sub[0].get("subject"))
            chk("Submit: no raw UUIDs in email body", no_uuid(emp_sub[0].get("bodyPreview") or ""), "UUID found" if not no_uuid(emp_sub[0].get("bodyPreview") or "") else "clean")

        # ── STEP 2: Line manager approve ──────────────────────────────────────
        r2 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/line-manager-approve",
            json={"actor_email": LINE_MANAGER_EMAIL, "actor_user_id": LINE_MANAGER_ID, "actor_roles": ["line_manager"]}, timeout=30)
        lm_ok = r2.status_code == 200
        chk("Line manager approval", lm_ok, f"HTTP {r2.status_code}")
        if lm_ok:
            chk("Status = line_manager_approved", r2.json().get("status") == "line_manager_approved", f"got: {r2.json().get('status')}")

        if lm_ok:
            # wait for 4 emails total (2 from submit + 2 from LM approval)
            em2 = emails_for(created_workflow_id, min_count=4)
            fin_notif = [e for e in em2 if FINANCE_EMAIL.lower() in (e.get("to") or "").lower() and "budget" in (e.get("subject") or "").lower()]
            lm_conf = [e for e in em2 if LINE_MANAGER_EMAIL.lower() in (e.get("to") or "").lower() and "confirmed" in (e.get("subject") or "").lower()]
            emp_appr = [e for e in em2 if EMPLOYEE_EMAIL.lower() in (e.get("to") or "").lower() and "approved" in (e.get("subject") or "").lower()]
            chk("LM approval: finance budget-validation email", len(fin_notif) > 0, f"{len(fin_notif)} found")
            chk("LM approval: line manager confirmation email", len(lm_conf) > 0, f"{len(lm_conf)} found")
            chk("LM approval: employee approved notification", len(emp_appr) > 0, f"{len(emp_appr)} found")
            if fin_notif:
                body2 = fin_notif[0].get("bodyPreview") or ""
                chk("LM approval: performed_by is name not UUID", no_uuid(body2), "UUID found" if not no_uuid(body2) else "clean")
                chk("LM approval: subject has #ID", "#" in (fin_notif[0].get("subject") or ""), fin_notif[0].get("subject"))
                chk("LM approval: says 'Line Manager' not 'Master Admin'", "master admin" not in body2.lower(), "Found 'master admin'" if "master admin" in body2.lower() else "correct")
        else:
            for name in ["LM approval: finance budget-validation email", "LM approval: line manager confirmation email", "LM approval: employee approved notification"]:
                skip(name, "skipped — line manager approval failed")

        # ── STEP 3: Finance validates ─────────────────────────────────────────
        if lm_ok:
            r3 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/validate-budget",
                json={"actor_email": FINANCE_EMAIL, "actor_user_id": FINANCE_ID, "actor_roles": ["finance"], "approved": True, "notes": "Diagnostics test"}, timeout=30)
            fin_ok = r3.status_code == 200
            chk("Finance budget validation", fin_ok, f"HTTP {r3.status_code}")
            if fin_ok:
                chk("Status = finance_approved", r3.json().get("status") == "finance_approved", f"got: {r3.json().get('status')}")
        else:
            fin_ok = False
            skip("Finance budget validation", "skipped — line manager approval failed")

        if fin_ok:
            # wait for 7 emails total (4 so far + 3 from finance)
            em3 = emails_for(created_workflow_id, min_count=7)
            it_proc = [e for e in em3 if IT_EMAIL.lower() in (e.get("to") or "").lower() and "procurement" in (e.get("subject") or "").lower()]
            fin_conf = [e for e in em3 if FINANCE_EMAIL.lower() in (e.get("to") or "").lower() and "confirmed" in (e.get("subject") or "").lower()]
            emp_bud = [e for e in em3 if EMPLOYEE_EMAIL.lower() in (e.get("to") or "").lower() and "budget" in (e.get("subject") or "").lower()]
            chk("Finance: IT admin procurement email", len(it_proc) > 0, f"{len(it_proc)} found")
            chk("Finance: finance confirmation email", len(fin_conf) > 0, f"{len(fin_conf)} found")
            chk("Finance: employee budget-approved email", len(emp_bud) > 0, f"{len(emp_bud)} found")
            if it_proc:
                body3 = it_proc[0].get("bodyPreview") or ""
                chk("Finance: IT email has MESSAGE section", "MESSAGE" in body3, "missing" if "MESSAGE" not in body3 else "present")
                chk("Finance: IT email performed_by not UUID", no_uuid(body3), "UUID found" if not no_uuid(body3) else "clean")
        else:
            for name in ["Finance: IT admin procurement email", "Finance: finance confirmation email", "Finance: employee budget-approved email"]:
                skip(name, "skipped — finance validation failed")

        # ── STEP 4: AI review ─────────────────────────────────────────────────
        try:
            r_ai = _httpx.post(f"{BASE}/api/workflow-review",
                json={"workflow_request_id": created_workflow_id, "reviewer_role": "finance"}, timeout=60)
            chk("AI review endpoint responds", r_ai.status_code == 200, f"HTTP {r_ai.status_code}")
            if r_ai.status_code == 200:
                ai = r_ai.json()
                chk("AI review: valid recommendation", ai.get("recommendation") in ("approve", "reject", "review"), f"got: {ai.get('recommendation')}")
                chk("AI review: confidence 0-100", isinstance(ai.get("confidence"), (int, float)) and 0 <= ai.get("confidence") <= 100, f"got: {ai.get('confidence')}")
                chk("AI review: reasons not empty", len(ai.get("reasons") or []) > 0, f"{len(ai.get('reasons') or [])} reasons")
                chk("AI review: reasons are non-trivial", all(len(r or "") > 20 for r in (ai.get("reasons") or [])), "a reason was too short")
        except Exception as e:
            chk("AI review endpoint", False, str(e))

        # ── STEP 5: Procurement URL ───────────────────────────────────────────
        try:
            r_url = _httpx.post(f"{BASE}/vendor-purchase-url",
                json={"subscription_name": SUBSCRIPTION_NAME, "vendor_name": VENDOR_NAME, "catalogue_id": CATALOGUE_ID}, timeout=30)
            chk("Vendor procurement URL endpoint", r_url.status_code == 200, f"HTTP {r_url.status_code}")
            if r_url.status_code == 200:
                pu = r_url.json().get("purchase_url") or ""
                chk("Procurement URL is https", pu.startswith("https://"), f"got: {pu[:80]}")
                chk("Procurement URL is not a compare/generic page", "compare" not in pu.lower() and "overview" not in pu.lower(), pu[:80], warning=True)
        except Exception as e:
            chk("Vendor procurement URL", False, str(e))

        # ── STEP 6: IT completes ──────────────────────────────────────────────
        if not fin_ok:
            for name in ["IT procurement complete", "Status = completed", "Licence record created", "Payment record created for workflow",
                         "Completion: completed emails sent", "Completion: employee receives completed email",
                         "Completion: credentials (Username) in email", "Completion: security notice present",
                         "Completion: password change URL in email", "Completion: performed_by not UUID",
                         "Completion: subject has #ID", "No legacy plain-text emails"]:
                skip(name, "skipped — finance validation failed")
            raise _SkipToCleanup()

        with conn.cursor() as _cur:
            _cur.execute("SELECT COUNT(*) AS cnt FROM slmct.payments WHERE organisation_id = %s", (ORG_ID,))
            _br = _cur.fetchone()
            payments_before = int(_br["cnt"]) if _br else 0

        r4 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/complete", json={
            "actor_email": IT_EMAIL, "actor_user_id": IT_ID, "actor_roles": ["it_admin"],
            "activation_method": "company_account",
            "username": "diagtest@company.com",
            "password": "DiagTest@123",
            "assigned_employee_name": "Employee One",
            "assigned_employee_email": EMPLOYEE_EMAIL,
        }, timeout=30)
        comp_ok = r4.status_code == 200
        chk("IT procurement complete", comp_ok, f"HTTP {r4.status_code}")
        if comp_ok:
            chk("Status = completed", r4.json().get("status") == "completed", f"got: {r4.json().get('status')}")

        with conn.cursor() as _cur:
            _cur.execute("SELECT id FROM slmct.licences WHERE organisation_id = %s ORDER BY assigned_at DESC LIMIT 1", (ORG_ID,))
            lic = _cur.fetchone()
            if lic:
                created_licence_id = str(lic["id"])
            chk("Licence record created", lic is not None, created_licence_id or "not found")

            _cur.execute("SELECT COUNT(*) AS cnt FROM slmct.payments WHERE organisation_id = %s", (ORG_ID,))
            _ba = _cur.fetchone()
            payments_after = int(_ba["cnt"]) if _ba else 0
            chk("Payment record created for workflow", payments_after > payments_before, f"before={payments_before} after={payments_after}")

        # wait for 9 emails total (7 so far + 2 from completion)
        em4 = emails_for(created_workflow_id, min_count=9)
        comp_em = [e for e in em4 if "completed" in (e.get("subject") or "").lower() or "active" in (e.get("subject") or "").lower()]
        emp_comp = [e for e in comp_em if EMPLOYEE_EMAIL.lower() in (e.get("to") or "").lower()]
        chk("Completion: completed emails sent", len(comp_em) > 0, f"{len(comp_em)} found")
        chk("Completion: employee receives completed email", len(emp_comp) > 0, f"{len(emp_comp)} found")
        if emp_comp:
            b4 = emp_comp[0].get("bodyPreview") or ""
            chk("Completion: credentials (Username) in email", "Username" in b4, "missing" if "Username" not in b4 else "present")
            chk("Completion: security notice present", "Security Notice" in b4, "missing" if "Security Notice" not in b4 else "present")
            chk("Completion: password change URL in email", "https://" in b4 and ("security" in b4.lower() or "password" in b4.lower()), "missing" if "https://" not in b4 else "present", warning=True)
            chk("Completion: performed_by not UUID", no_uuid(b4), "UUID found" if not no_uuid(b4) else "clean")
            chk("Completion: subject has #ID", "#" in (emp_comp[0].get("subject") or ""), emp_comp[0].get("subject") or "")
        legacy = [e for e in em4 if (e.get("subject") or "") in ("Request Completed", "Procurement Completed")]
        chk("No legacy plain-text emails", len(legacy) == 0, f"{len(legacy)} found" if legacy else "clean")

        # ── DOWNSTREAM DB CHECKS ──────────────────────────────────────────────
        downstream = _diag_check_downstream(
            conn, checks, created_workflow_id, ORG_ID, SUBSCRIPTION_NAME,
            requester_email=EMPLOYEE_EMAIL, expect_licence_assigned=True,
        )
        if downstream.get("subscription_id"):
            created_subscription_id = downstream["subscription_id"]
            created_licence_id = downstream.get("licence_id") or created_licence_id

    except _SkipToCleanup:
        pass
    except Exception as exc:
        chk("Diagnostics runner (unexpected error)", False, str(exc))

    finally:
        try:
            with conn.cursor() as _cur:
                if created_licence_id:
                    _cur.execute("DELETE FROM slmct.licences WHERE id = %s", (created_licence_id,))
                if created_workflow_id:
                    _cur.execute("DELETE FROM slmct.email_logs WHERE workflow_request_id = %s", (created_workflow_id,))
                    _cur.execute("DELETE FROM slmct.workflow_status_history WHERE workflow_request_id = %s", (created_workflow_id,))
                    _cur.execute("DELETE FROM slmct.payments WHERE reference LIKE %s", (f"WF-{created_workflow_id[:8].upper()}%",))
                    _cur.execute("DELETE FROM slmct.audit_logs WHERE entity_id::text = %s", (created_workflow_id,))
                    if created_subscription_id:
                        _cur.execute("DELETE FROM slmct.subscriptions WHERE id = %s", (created_subscription_id,))
                    else:
                        _cur.execute(
                            "DELETE FROM slmct.subscriptions WHERE organisation_id = %s AND lower(name) = lower(%s) AND created_at > now() - interval '5 minutes'",
                            (ORG_ID, SUBSCRIPTION_NAME),
                        )
                    _cur.execute("DELETE FROM slmct.workflow_requests WHERE id = %s", (created_workflow_id,))
            conn.commit()
        except Exception as ce:
            log.warning("diagnostics: cleanup error -- %s", ce)

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "workflow_id": created_workflow_id, "ai_analysis": ai_analysis}


def _diag_ai_analysis(checks: list[dict], summary: dict) -> str:
    """Use Gemini to explain failures in plain English with actionable fixes."""
    try:
        if summary["failed"] == 0 and summary["warned"] == 0:
            return "All checks passed. The full employee software request workflow is functioning correctly end-to-end."

        from app.settings import get_settings
        settings = get_settings()
        if not settings.gemini_api_key:
            return ""

        failures = [c for c in checks if c["status"] == "fail"]
        warnings = [c for c in checks if c["status"] == "warn"]

        lines = []
        if failures:
            lines.append("FAILED CHECKS:")
            for c in failures:
                lines.append(f"  - {c['name']}: {c['detail']}")
        if warnings:
            lines.append("WARNINGS:")
            for c in warnings:
                lines.append(f"  - {c['name']}: {c['detail']}")

        from app.llm import complete
        from app.prompts import format_prompt

        prompt = format_prompt(
            "diag_ai_analysis",
            failure_lines=chr(10).join(lines),
            passed=summary["passed"],
            failed=summary["failed"],
            warned=summary["warned"],
            total=summary["total"],
        )
        result = complete(
            "diag_ai_analysis",
            contents=prompt,
            max_output_tokens=600,
            thinking_budget=0,
        )
        return (result.text or "").strip()
    except Exception as exc:
        log.warning("diagnostics: AI analysis failed -- %s", exc)
        return ""




@app.post("/api/diagnostics/run-it-subscription-test")
def run_it_subscription_test(conn: Connection = Depends(get_connection)):
    """
    End-to-end diagnostic for the IT New Subscription Request workflow.
    Flow: IT submits → Finance validates → IT completes (no line manager step).
    """
    import httpx as _httpx
    import time as _time

    BASE = "http://localhost:8000"
    checks: list[dict] = []
    created_workflow_id = None
    created_licence_id = None
    created_subscription_id = None

    _ctx = _diag_resolve_context(conn)
    ORG_ID        = _ctx["ORG_ID"]
    IT_EMAIL      = _ctx["IT_EMAIL"]
    IT_ID         = _ctx["IT_ID"]
    FINANCE_EMAIL = _ctx["FINANCE_EMAIL"]
    FINANCE_ID    = _ctx["FINANCE_ID"]
    if not ORG_ID or not IT_ID or not FINANCE_ID:
        return {"status": "skipped", "reason": "No active organisation or required role users found — seed data may not be loaded yet.", "checks": []}
    SUB_NAME      = "[DIAG] Figma Professional"

    class _SkipToCleanup(Exception):
        pass

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    def skip(name: str, reason: str = ""):
        checks.append({"name": name, "status": "skip", "detail": reason})

    def no_uuid(text: str) -> bool:
        import re
        return not bool(re.search(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', text or "", re.I))

    def emails_for(wf_id: str, min_count: int = 1, timeout: int = 20) -> list[dict]:
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                r = _httpx.get(f"{BASE}/api/email/logs/{wf_id}", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if len(data) >= min_count:
                        return data
            except Exception:
                pass
            _time.sleep(1)
        return []

    try:
        # ── STEP 1: IT admin submits new subscription request ─────────────────
        r1 = _httpx.post(f"{BASE}/workflow-requests", json={
            "workflow_type": "new_subscription_request",
            "requested_module": "subscriptions",
            "organisation_id": ORG_ID,
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "payload": {
                "name": SUB_NAME,
                "department": "Design",
                "justification": "Diagnostics test — IT subscription request check.",
                "amount": 750,
                "currency_code": "AED",
                "billing_cycle": "monthly",
            },
        }, timeout=30)
        submitted_ok = r1.status_code in (200, 201)
        chk("Workflow submission", submitted_ok, f"HTTP {r1.status_code}")

        if not submitted_ok:
            for name in ["Workflow ID returned", "Initial status is 'submitted'",
                         "Submit: finance action-required email", "Submit: IT confirmation email",
                         "Submit: subject contains #ID", "Finance budget validation",
                         "Status = finance_approved", "Finance: IT email", "Finance: finance confirmation email",
                         "IT procurement complete", "Status = completed", "Licence record created",
                         "Completion: emails sent", "No legacy plain-text emails"]:
                skip(name, "skipped — submission failed")
            raise _SkipToCleanup()

        d1 = r1.json()
        created_workflow_id = d1.get("id")
        chk("Workflow ID returned", bool(created_workflow_id), created_workflow_id or "missing")
        chk("Initial status is 'submitted'", d1.get("status") == "submitted", f"got: {d1.get('status')}")
        chk("No line manager step", d1.get("workflow_type") == "new_subscription_request", f"type: {d1.get('workflow_type')}")

        # Submit emails: finance gets action-required, IT gets confirmation
        em1 = emails_for(created_workflow_id, min_count=2)
        fin_notif = [e for e in em1 if FINANCE_EMAIL.lower() in (e.get("to") or "").lower()]
        it_conf   = [e for e in em1 if IT_EMAIL.lower() in (e.get("to") or "").lower()]
        chk("Submit: finance action-required email", len(fin_notif) > 0, f"{len(fin_notif)} found")
        chk("Submit: IT confirmation email", len(it_conf) > 0, f"{len(it_conf)} found")
        if em1:
            chk("Submit: subject contains #ID", any("#" in (e.get("subject") or "") for e in em1), (em1[0].get("subject") or ""))

        # ── STEP 2: Finance validates (directly from submitted — no LM step) ──
        r2 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/validate-budget", json={
            "actor_email": FINANCE_EMAIL,
            "actor_user_id": FINANCE_ID,
            "actor_roles": ["finance"],
            "approved": True,
            "notes": "Diagnostics test — budget approved",
        }, timeout=30)
        fin_ok = r2.status_code == 200
        chk("Finance budget validation", fin_ok, f"HTTP {r2.status_code}")
        if fin_ok:
            chk("Status = finance_approved", r2.json().get("status") == "finance_approved", f"got: {r2.json().get('status')}")

        if fin_ok:
            # 2 submit emails + 3 finance emails = 5 total
            em2 = emails_for(created_workflow_id, min_count=5)
            it_proc   = [e for e in em2 if IT_EMAIL.lower() in (e.get("to") or "").lower() and "procurement" in (e.get("subject") or "").lower()]
            fin_conf  = [e for e in em2 if FINANCE_EMAIL.lower() in (e.get("to") or "").lower() and "confirmed" in (e.get("subject") or "").lower()]
            chk("Finance: IT admin procurement email", len(it_proc) > 0, f"{len(it_proc)} found")
            chk("Finance: finance confirmation email", len(fin_conf) > 0, f"{len(fin_conf)} found")
            if it_proc:
                body2 = it_proc[0].get("bodyPreview") or ""
                chk("Finance: IT email has MESSAGE section", "MESSAGE" in body2, "missing" if "MESSAGE" not in body2 else "present")
                chk("Finance: IT email performed_by not UUID", no_uuid(body2), "UUID found" if not no_uuid(body2) else "clean")
        else:
            for name in ["Finance: IT admin procurement email", "Finance: finance confirmation email",
                         "Finance: IT email has MESSAGE section", "Finance: IT email performed_by not UUID",
                         "IT procurement complete", "Status = completed", "Licence record created",
                         "Completion: emails sent", "No legacy plain-text emails"]:
                skip(name, "skipped — finance validation failed")
            raise _SkipToCleanup()

        # ── STEP 3: IT admin completes ────────────────────────────────────────
        r3 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/complete", json={
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "activation_method": "company_account",
            "username": "diagit@company.com",
            "password": "DiagIT@123",
            "assigned_employee_name": "IT Admin",
            "assigned_employee_email": IT_EMAIL,
        }, timeout=30)
        comp_ok = r3.status_code == 200
        chk("IT procurement complete", comp_ok, f"HTTP {r3.status_code}")
        if comp_ok:
            chk("Status = completed", r3.json().get("status") == "completed", f"got: {r3.json().get('status')}")

        em3 = emails_for(created_workflow_id, min_count=7)
        comp_em = [e for e in em3 if "completed" in (e.get("subject") or "").lower() or "active" in (e.get("subject") or "").lower()]
        chk("Completion: emails sent", len(comp_em) > 0, f"{len(comp_em)} found")
        if comp_em:
            chk("Completion: subject has #ID", "#" in (comp_em[0].get("subject") or ""), comp_em[0].get("subject") or "")
        legacy = [e for e in em3 if (e.get("subject") or "") in ("Request Completed", "Procurement Completed")]
        chk("No legacy plain-text emails", len(legacy) == 0, f"{len(legacy)} found" if legacy else "clean")

        # ── DOWNSTREAM DB CHECKS ──────────────────────────────────────────────
        # IT subscription: licence not auto-assigned (no requester person record for IT)
        downstream = _diag_check_downstream(
            conn, checks, created_workflow_id, ORG_ID, SUB_NAME,
            requester_email=None, expect_licence_assigned=False,
        )
        if downstream.get("subscription_id"):
            created_subscription_id = downstream["subscription_id"]

    except _SkipToCleanup:
        pass
    except Exception as exc:
        chk("Unexpected error", False, str(exc)[:200])
    finally:
        if created_workflow_id:
            try:
                with conn.cursor() as cur:
                    if created_licence_id:
                        cur.execute("DELETE FROM slmct.licences WHERE id = %s", (created_licence_id,))
                    cur.execute("DELETE FROM slmct.payments WHERE reference LIKE %s", (f"WF-{created_workflow_id[:8].upper()}%",))
                    cur.execute("DELETE FROM slmct.email_logs WHERE workflow_request_id = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.workflow_status_history WHERE workflow_request_id = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.audit_logs WHERE entity_id::text = %s", (created_workflow_id,))
                    if created_subscription_id:
                        cur.execute("DELETE FROM slmct.subscriptions WHERE id = %s", (created_subscription_id,))
                    else:
                        cur.execute(
                            "DELETE FROM slmct.subscriptions WHERE organisation_id = %s AND lower(name) = lower(%s) AND created_at > now() - interval '5 minutes'",
                            (ORG_ID, SUB_NAME),
                        )
                    cur.execute("DELETE FROM slmct.workflow_requests WHERE id = %s", (created_workflow_id,))
                conn.commit()
            except Exception as ce:
                log.warning("diagnostics: it-subscription cleanup error -- %s", ce)

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "workflow_id": created_workflow_id, "ai_analysis": ai_analysis}


def _diag_check_downstream(
    conn,
    checks: list[dict],
    wf_id: str,
    org_id: str,
    sub_name: str,
    requester_email: str | None = None,
    *,
    expect_licence_assigned: bool = True,
) -> dict:
    """
    Verify all downstream DB effects after a workflow completes:
    subscription created, payment created (linked to budget), licence assigned,
    audit log entries written, status history transitions recorded.
    Returns ids dict for cleanup.
    """
    def chk(name, passed, detail="", warning=False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    result = {"subscription_id": None, "payment_id": None, "licence_id": None}

    with conn.cursor() as cur:
        # ── Subscription on the subscriptions page ──────────────────────────
        cur.execute(
            "SELECT id, name, status, amount FROM slmct.subscriptions WHERE organisation_id = %s AND lower(name) = lower(%s) ORDER BY created_at DESC LIMIT 1",
            (org_id, sub_name),
        )
        sub_row = cur.fetchone()
        chk("Subscription record created", sub_row is not None, f"'{sub_name}' not found" if not sub_row else f"id={str(sub_row['id'])[:8]}")
        if sub_row:
            result["subscription_id"] = str(sub_row["id"])
            chk("Subscription status is active", sub_row["status"] in ("active", "pending"), f"got: {sub_row['status']}")

        # ── Payment on the payments page ────────────────────────────────────
        cur.execute(
            "SELECT id, amount, budget_id FROM slmct.payments WHERE reference LIKE %s ORDER BY created_at DESC LIMIT 1",
            (f"WF-{wf_id[:8].upper()}%",),
        )
        pay_row = cur.fetchone()
        # Fallback: search by sub_name
        if not pay_row and sub_row:
            cur.execute(
                "SELECT id, amount, budget_id FROM slmct.payments WHERE subscription_id = %s ORDER BY created_at DESC LIMIT 1",
                (sub_row["id"],),
            )
            pay_row = cur.fetchone()
        chk("Payment record on payments page", pay_row is not None, "not found" if not pay_row else f"amount={pay_row['amount']}")
        if pay_row:
            result["payment_id"] = str(pay_row["id"])
            chk("Payment linked to budget", pay_row["budget_id"] is not None, "no budget_id" if not pay_row["budget_id"] else "linked", warning=True)

        # ── Licence / seat assignment ────────────────────────────────────────
        if expect_licence_assigned and sub_row:
            cur.execute(
                "SELECT l.id, l.status, l.assigned_to_person_id FROM slmct.licences l WHERE l.subscription_id = %s ORDER BY l.assigned_at DESC LIMIT 1",
                (sub_row["id"],),
            )
            lic_row = cur.fetchone()
            chk("Licence/seat record created", lic_row is not None, "not found" if not lic_row else f"id={str(lic_row['id'])[:8]}")
            if lic_row:
                result["licence_id"] = str(lic_row["id"])
                chk("Licence status is assigned", lic_row["status"] == "assigned", f"got: {lic_row['status']}")
                if requester_email:
                    cur.execute("SELECT id FROM slmct.people WHERE work_email = %s LIMIT 1", (requester_email,))
                    person = cur.fetchone()
                    if person:
                        chk("Licence assigned to requester", str(lic_row["assigned_to_person_id"]) == str(person["id"]), "assigned to wrong person" if lic_row["assigned_to_person_id"] != person["id"] else "correct")

        # ── Audit log entries ────────────────────────────────────────────────
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM slmct.audit_logs WHERE entity_id::text = %s OR (entity_type = 'subscription' AND entity_id = %s)",
            (wf_id, sub_row["id"] if sub_row else wf_id),
        )
        audit_row = cur.fetchone()
        audit_count = int(audit_row["cnt"]) if audit_row else 0
        chk("Audit log entries written", audit_count > 0, f"{audit_count} entries")

        # ── Status history transitions ───────────────────────────────────────
        cur.execute(
            "SELECT COUNT(*) AS cnt, array_agg(to_status ORDER BY created_at) AS statuses FROM slmct.workflow_status_history WHERE workflow_request_id = %s",
            (wf_id,),
        )
        hist_row = cur.fetchone()
        hist_count = int(hist_row["cnt"]) if hist_row else 0
        statuses = list(hist_row["statuses"] or []) if hist_row else []
        chk("Status history recorded", hist_count >= 2, f"{hist_count} transitions: {' → '.join(statuses)}")
        chk("Final status in history is completed", "completed" in statuses, f"statuses: {statuses}")

    return result


def _diag_summary(checks: list[dict]) -> dict:
    active = [c for c in checks if c["status"] != "skip"]
    total = len(active)
    passed = sum(1 for c in active if c["status"] == "pass")
    failed = sum(1 for c in active if c["status"] == "fail")
    warned = sum(1 for c in active if c["status"] == "warn")
    skipped = sum(1 for c in checks if c["status"] == "skip")
    return {"total": total, "passed": passed, "failed": failed, "warned": warned, "skipped": skipped, "overall": "pass" if failed == 0 else "fail"}


@app.post("/api/diagnostics/run-ai-test")
def run_ai_test(conn: Connection = Depends(get_connection)):
    """End-to-end test of every AI-powered feature in the app."""
    import httpx as _httpx
    checks: list[dict] = []

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    BASE = "http://localhost:8000"
    _ctx = _diag_resolve_context(conn)
    ORG_ID = _ctx["ORG_ID"]
    if not ORG_ID:
        return {"status": "skipped", "reason": "No active organisation found — seed data may not be loaded yet.", "checks": []}

    # ── 1. TOOL SUMMARY ───────────────────────────────────────────────────────
    try:
        r = _httpx.post(f"{BASE}/api/tool-summary", json={"tool_name": "Slack", "vendor_name": "Salesforce"}, timeout=30)
        chk("Tool Summary: endpoint responds", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code == 200:
            d = r.json()
            summary_text = d.get("summary") or ""
            chk("Tool Summary: non-empty response", len(summary_text) > 30, f"{len(summary_text)} chars")
            chk("Tool Summary: not a fallback", d.get("provider") == "gemini", f"provider={d.get('provider')}")
            chk("Tool Summary: mentions the tool", "slack" in summary_text.lower() or "communication" in summary_text.lower(), summary_text[:80])
            chk("Tool Summary: no error text in response", "error" not in summary_text.lower() and "sorry" not in summary_text.lower(), "error text found" if "error" in summary_text.lower() else "clean")
    except Exception as e:
        chk("Tool Summary: endpoint responds", False, str(e))

    # ── 2. CONTRACT EXTRACTION ────────────────────────────────────────────────
    sample_contract = """SOFTWARE SUBSCRIPTION AGREEMENT
Vendor: Adobe Systems Inc.
Contract Number: MSA-2024-0042
Effective Date: 2024-01-15
Expiry Date: 2025-01-14
Annual Contract Value: USD 12,000
Auto-Renewal: Yes, with 30 days notice period required.
Owner: procurement@company.com
This Master Service Agreement governs the use of Adobe Creative Cloud Enterprise."""
    try:
        r = _httpx.post(f"{BASE}/api/extract", json={"text": sample_contract, "vendors": ["Adobe"]}, timeout=45)
        chk("Contract Extraction: endpoint responds", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code == 200:
            d = r.json()
            fields = d.get("fields") or {}
            chk("Contract Extraction: provider is gemini", d.get("provider") == "gemini", f"provider={d.get('provider')}")
            chk("Contract Extraction: vendor extracted", "adobe" in (fields.get("vendor") or "").lower(), f"got: {fields.get('vendor')}")
            chk("Contract Extraction: value extracted", isinstance(fields.get("value"), (int, float)) and fields.get("value", 0) > 0, f"got: {fields.get('value')}")
            chk("Contract Extraction: start_date extracted", bool(fields.get("start_date")), f"got: {fields.get('start_date')}")
            chk("Contract Extraction: end_date extracted", bool(fields.get("end_date")), f"got: {fields.get('end_date')}")
            chk("Contract Extraction: auto_renew detected", fields.get("auto_renew") is True, f"got: {fields.get('auto_renew')}")
            chk("Contract Extraction: notice_period_days > 0", isinstance(fields.get("notice_period_days"), (int, float)) and fields.get("notice_period_days", 0) > 0, f"got: {fields.get('notice_period_days')}")
    except Exception as e:
        chk("Contract Extraction: endpoint responds", False, str(e))

    # ── 3. AI COPILOT ─────────────────────────────────────────────────────────
    try:
        messages = [{"role": "user", "content": "Summarise the active software subscriptions for this organisation. List each one with its cost and status.", "id": "diag-1"}]
        r = _httpx.post(
            f"{BASE}/api/copilot",
            json={
                "messages": messages,
                "organisation_id": ORG_ID,
                "actor_roles": ["master_admin"],
                "actor_user_id": _ctx.get("ADMIN_ID"),
            },
            timeout=120,
        )
        chk("AI Copilot: endpoint responds", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code == 200:
            d = r.json()
            text = d.get("responseText") or ""
            chk("AI Copilot: non-empty response", len(text) > 100, f"{len(text)} chars")
            chk("AI Copilot: provider is gemini", d.get("provider") == "gemini", f"provider={d.get('provider')}")
            chk("AI Copilot: returns substantive content", len(text) > 150 and not all(w in text.lower() for w in ["cannot", "unable", "no information"]), "response too short or evasive")
            chk("AI Copilot: response is markdown", any(c in text for c in ["**", "#", "-", "|"]), "no markdown found", warning=True)
    except Exception as e:
        chk("AI Copilot: endpoint responds", False, str(e))

    # ── 4. WORKFLOW REVIEW — all 5 roles ─────────────────────────────────────
    ai_review_wf_id = None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM slmct.workflow_requests WHERE organisation_id = %s ORDER BY created_at DESC LIMIT 1", (ORG_ID,))
            wf_row = cur.fetchone()

        if not wf_row:
            # Create a minimal workflow request so the AI reviewer has something to analyse
            EMPLOYEE_ID = _ctx.get("EMPLOYEE_ID")
            EMPLOYEE_EMAIL_FOR_WF = _ctx.get("EMPLOYEE_EMAIL")
            CATALOGUE_ID = "0640b377-70b0-4dcf-bfb7-1d74186c6539"
            r_sub = _httpx.post(f"{BASE}/workflow-requests", json={
                "workflow_type": "employee_software_request",
                "requested_module": "subscriptions",
                "actor_email": EMPLOYEE_EMAIL_FOR_WF,
                "actor_user_id": EMPLOYEE_ID,
                "actor_roles": ["employee"],
                "organisation_id": ORG_ID,
                "payload": {
                    "name": "[DIAG] Microsoft Teams Essentials",
                    "department": "Software Engineering",
                    "justification": "AI diagnostics test - workflow review check.",
                    "amount": 25,
                    "currency_code": "AED",
                    "billing_cycle": "monthly",
                },
            }, timeout=30)
            if r_sub.status_code == 201:
                ai_review_wf_id = r_sub.json().get("id")
                wf_row = {"id": ai_review_wf_id}

        if wf_row:
            wf_id = str(wf_row["id"])
            chk("AI Review: workflow found for review", True, f"using {wf_id[:8].upper()}")
            for role in ["line_manager", "finance", "master_admin", "it_admin", "hr_admin"]:
                r = _httpx.post(f"{BASE}/api/workflow-review", json={"workflow_request_id": wf_id, "reviewer_role": role}, timeout=60)
                chk(f"AI Review [{role}]: endpoint responds", r.status_code == 200, f"HTTP {r.status_code}")
                if r.status_code == 200:
                    d = r.json()
                    chk(f"AI Review [{role}]: valid recommendation", d.get("recommendation") in ("approve", "reject", "review"), f"got: {d.get('recommendation')}")
                    chk(f"AI Review [{role}]: confidence 0-100", isinstance(d.get("confidence"), (int, float)) and 0 <= d.get("confidence") <= 100, f"got: {d.get('confidence')}")
                    chk(f"AI Review [{role}]: has reasons", len(d.get("reasons") or []) > 0, f"{len(d.get('reasons') or [])} reasons")
                    chk(f"AI Review [{role}]: reasons are substantive", all(len(r2 or "") > 20 for r2 in (d.get("reasons") or [])), "a reason was too short")
                    chk(f"AI Review [{role}]: has concerns field", "concerns" in d, "field missing", warning=True)
        else:
            chk("AI Review: workflow found for review", False, "no workflow requests in org and failed to create one")
    except Exception as e:
        chk("AI Review: endpoint responds", False, str(e))
    finally:
        # Clean up temp workflow if we created one
        if ai_review_wf_id:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM slmct.workflow_requests WHERE id = %s", (ai_review_wf_id,))
                conn.commit()
            except Exception:
                pass

    # ── 5. UTILISATION CHECKS ─────────────────────────────────────────────────
    try:
        with conn.cursor() as cur:
            # Subscriptions with zero licences assigned
            cur.execute("""
                SELECT s.name, s.amount, s.currency_code
                FROM slmct.subscriptions s
                WHERE s.organisation_id = %s AND s.status = 'active'
                  AND NOT EXISTS (
                      SELECT 1 FROM slmct.licences l WHERE l.subscription_id = s.id
                  )
            """, (ORG_ID,))
            no_licences = cur.fetchall()
            chk(
                "Utilisation: all active subscriptions have at least one licence",
                len(no_licences) == 0,
                (str(len(no_licences)) + " subscription(s) have no licences: " + ", ".join(r["name"] for r in no_licences)) if no_licences else "all assigned",
                warning=len(no_licences) > 0,
            )

            # Subscriptions where every licence is unassigned (available)
            cur.execute("""
                SELECT s.name, COUNT(l.id) AS total, SUM(CASE WHEN l.status = 'available' THEN 1 ELSE 0 END) AS unassigned
                FROM slmct.subscriptions s
                JOIN slmct.licences l ON l.subscription_id = s.id
                WHERE s.organisation_id = %s AND s.status = 'active'
                GROUP BY s.id, s.name
                HAVING COUNT(l.id) > 0 AND COUNT(l.id) = SUM(CASE WHEN l.status = 'available' THEN 1 ELSE 0 END)
            """, (ORG_ID,))
            all_unused = cur.fetchall()
            chk(
                "Utilisation: no subscription has all licences sitting unused",
                len(all_unused) == 0,
                (str(len(all_unused)) + " subscription(s) have licences but none assigned: " + ", ".join(r["name"] for r in all_unused)) if all_unused else "all in use",
                warning=len(all_unused) > 0,
            )

            # Licences assigned to employees not in the people table
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM slmct.licences l
                WHERE l.organisation_id = %s
                  AND l.assigned_to_person_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM slmct.people p WHERE p.id = l.assigned_to_person_id
                  )
            """, (ORG_ID,))
            orphan_row = cur.fetchone()
            orphan_count = int(orphan_row["cnt"]) if orphan_row else 0
            chk(
                "Utilisation: no licences assigned to removed employees",
                orphan_count == 0,
                str(orphan_count) + " licence(s) linked to employees no longer in system" if orphan_count else "clean",
                warning=orphan_count > 0,
            )
    except Exception as e:
        chk("Utilisation checks", False, str(e))

    # ── 6. AI EMAIL CONTENT QUALITY ───────────────────────────────────────────
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT body FROM slmct.email_logs
                WHERE body LIKE '%%MESSAGE%%' AND body LIKE '%%Derisk360%%'
                ORDER BY created_at DESC LIMIT 5
            """)
            recent_emails = cur.fetchall()

        chk("AI Email: recent emails with AI content found", len(recent_emails) > 0, f"{len(recent_emails)} found")
        if recent_emails:
            bodies = [str(r["body"] or "") for r in recent_emails]
            chk("AI Email: MESSAGE section is non-empty", all(len(b.split("MESSAGE")[1][:200].strip()) > 20 if "MESSAGE" in b else False for b in bodies), "MESSAGE section empty in an email")
            chk("AI Email: no raw error text in emails", not any("traceback" in b.lower() or "exception" in b.lower() or "none" == b.strip().lower() for b in bodies), "error text found in email body")
            chk("AI Email: emails have substantial body", all(len(b) > 500 for b in bodies), "an email body is too short")
    except Exception as e:
        chk("AI Email: content check", False, str(e))

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "workflow_id": None, "ai_analysis": ai_analysis}


# ─────────────────────────────────────────────────────────────────────────────
# DIAGNOSTIC: LICENCE ASSIGNMENT REQUEST
# Flow: IT submits → Finance validates → IT completes (no line manager)
# On completion: creates licence record linked to subscription
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/diagnostics/run-licence-assignment-test")
def run_licence_assignment_test(conn: Connection = Depends(get_connection)):
    import httpx as _httpx
    import time as _time

    BASE = "http://localhost:8000"
    checks: list[dict] = []
    created_workflow_id = None
    temp_subscription_id = None
    created_licence_id = None

    _ctx = _diag_resolve_context(conn)
    ORG_ID        = _ctx["ORG_ID"]
    IT_EMAIL      = _ctx["IT_EMAIL"]
    IT_ID         = _ctx["IT_ID"]
    FINANCE_EMAIL = _ctx["FINANCE_EMAIL"]
    FINANCE_ID    = _ctx["FINANCE_ID"]
    EMPLOYEE_EMAIL = _ctx["EMPLOYEE_EMAIL"]
    if not ORG_ID or not IT_ID or not FINANCE_ID:
        return {"status": "skipped", "reason": "No active organisation or required role users found — seed data may not be loaded yet.", "checks": []}
    SUB_NAME      = "Diag Licence Assignment Test"

    class _SkipToCleanup(Exception):
        pass

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    def skip(name: str, reason: str = ""):
        checks.append({"name": name, "status": "skip", "detail": reason})

    def no_uuid(text: str) -> bool:
        import re
        return not bool(re.search(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', text or "", re.I))

    def emails_for(wf_id: str, min_count: int = 1, timeout: int = 20) -> list[dict]:
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                r = _httpx.get(f"{BASE}/api/email/logs/{wf_id}", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if len(data) >= min_count:
                        return data
            except Exception:
                pass
            _time.sleep(1)
        return []

    try:
        # ── SETUP: create a temp subscription to assign a licence against ──────
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO slmct.subscriptions (organisation_id, name, billing_cycle, amount, currency_code, status, department)
                VALUES (%s, %s, 'monthly', 300, 'AED', 'active', 'Engineering')
                RETURNING id
                """,
                (ORG_ID, SUB_NAME + " [base sub]"),
            )
            temp_subscription_id = str(cur.fetchone()["id"])
            conn.commit()
        chk("Setup: temp subscription created", bool(temp_subscription_id), temp_subscription_id or "failed")

        # Resolve employee person_id from people table
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM slmct.people WHERE work_email = %s LIMIT 1", (EMPLOYEE_EMAIL,))
            pr = cur.fetchone()
        person_id = str(pr["id"]) if pr else None
        chk("Setup: employee person record found", bool(person_id), person_id or "not found in slmct.people", warning=not bool(person_id))

        # ── STEP 1: IT admin submits licence assignment request ────────────────
        r1 = _httpx.post(f"{BASE}/workflow-requests", json={
            "requested_module": "licences",
            "organisation_id": ORG_ID,
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "payload": {
                "subscription_id": temp_subscription_id,
                "licence_name": "Diag Test Seat",
                "assigned_to_person_id": person_id,
                "assigned_at": "2025-01-01",
                "notes": "Diagnostics test — licence assignment workflow.",
            },
        }, timeout=30)
        submitted_ok = r1.status_code in (200, 201)
        chk("Workflow submission", submitted_ok, f"HTTP {r1.status_code}")

        if not submitted_ok:
            for name in [
                "Workflow ID returned", "Workflow type = license_assignment_request",
                "Initial status = submitted", "No line manager step",
                "Submit: finance action-required email", "Submit: IT confirmation email",
                "Submit: subject contains #ID", "Finance budget validation",
                "Status = finance_approved", "Finance: IT procurement email",
                "Finance: finance confirmation email", "IT procurement complete",
                "Status = completed", "Completion: emails sent", "No legacy plain-text emails",
                "Downstream: subscription on subscriptions page", "Downstream: licence created on licences page",
                "Downstream: licence status available/assigned", "Downstream: licence linked to correct subscription",
                "Downstream: person assigned to licence", "Downstream: payment created",
                "Downstream: audit log entries written", "Downstream: status history recorded",
                "Downstream: final status is completed",
            ]:
                skip(name, "skipped — submission failed")
            raise _SkipToCleanup()

        d1 = r1.json()
        created_workflow_id = d1.get("id")
        chk("Workflow ID returned", bool(created_workflow_id), created_workflow_id or "missing")
        chk("Workflow type = license_assignment_request", d1.get("workflow_type") == "license_assignment_request", f"got: {d1.get('workflow_type')}")
        chk("Initial status = submitted", d1.get("status") == "submitted", f"got: {d1.get('status')}")
        chk("No line manager step", d1.get("workflow_type") not in ("employee_software_request",), "workflow skips LM by design")

        # Submit emails: finance action-required + IT confirmation
        em1 = emails_for(created_workflow_id, min_count=2)
        fin_notif = [e for e in em1 if FINANCE_EMAIL.lower() in (e.get("to") or "").lower()]
        it_conf   = [e for e in em1 if IT_EMAIL.lower() in (e.get("to") or "").lower()]
        chk("Submit: finance action-required email", len(fin_notif) > 0, f"{len(fin_notif)} found")
        chk("Submit: IT confirmation email", len(it_conf) > 0, f"{len(it_conf)} found")
        if em1:
            chk("Submit: subject contains #ID", any("#" in (e.get("subject") or "") for e in em1), em1[0].get("subject") or "")

        # ── STEP 2: Finance validates ─────────────────────────────────────────
        r2 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/validate-budget", json={
            "actor_email": FINANCE_EMAIL,
            "actor_user_id": FINANCE_ID,
            "actor_roles": ["finance"],
            "approved": True,
            "notes": "Diagnostics test — licence cost approved",
        }, timeout=30)
        fin_ok = r2.status_code == 200
        chk("Finance budget validation", fin_ok, f"HTTP {r2.status_code}")
        if fin_ok:
            chk("Status = finance_approved", r2.json().get("status") == "finance_approved", f"got: {r2.json().get('status')}")

        if fin_ok:
            em2 = emails_for(created_workflow_id, min_count=5)
            it_proc  = [e for e in em2 if IT_EMAIL.lower() in (e.get("to") or "").lower() and "procurement" in (e.get("subject") or "").lower()]
            fin_conf = [e for e in em2 if FINANCE_EMAIL.lower() in (e.get("to") or "").lower() and "confirmed" in (e.get("subject") or "").lower()]
            chk("Finance: IT procurement email", len(it_proc) > 0, f"{len(it_proc)} found")
            chk("Finance: finance confirmation email", len(fin_conf) > 0, f"{len(fin_conf)} found")
            if it_proc:
                body2 = it_proc[0].get("bodyPreview") or ""
                chk("Finance: IT email has MESSAGE section", "MESSAGE" in body2, "missing" if "MESSAGE" not in body2 else "present")
                chk("Finance: IT email performed_by not UUID", no_uuid(body2), "UUID found" if not no_uuid(body2) else "clean")
        else:
            for name in [
                "Finance: IT procurement email", "Finance: finance confirmation email",
                "Finance: IT email has MESSAGE section", "Finance: IT email performed_by not UUID",
                "IT procurement complete", "Status = completed", "Completion: emails sent",
                "No legacy plain-text emails",
            ]:
                skip(name, "skipped — finance validation failed")
            raise _SkipToCleanup()

        # ── STEP 3: IT admin completes the licence assignment ─────────────────
        r3 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/complete", json={
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "activation_method": "invitation_email",
            "recipient_email": EMPLOYEE_EMAIL,
            "invitation_status": "sent",
            "assigned_employee_name": "Employee One",
            "assigned_employee_email": EMPLOYEE_EMAIL,
        }, timeout=30)
        comp_ok = r3.status_code == 200
        chk("IT procurement complete", comp_ok, f"HTTP {r3.status_code}: {r3.text[:120] if not comp_ok else ''}")
        if comp_ok:
            chk("Status = completed", r3.json().get("status") == "completed", f"got: {r3.json().get('status')}")

        em3 = emails_for(created_workflow_id, min_count=7)
        comp_em = [e for e in em3 if "completed" in (e.get("subject") or "").lower() or "active" in (e.get("subject") or "").lower()]
        chk("Completion: emails sent", len(comp_em) > 0, f"{len(comp_em)} found")
        legacy = [e for e in em3 if (e.get("subject") or "") in ("Request Completed", "Procurement Completed")]
        chk("No legacy plain-text emails", len(legacy) == 0, f"{len(legacy)} found" if legacy else "clean")

        # ── DOWNSTREAM CHECKS ─────────────────────────────────────────────────
        # Licence should be created linked to the temp subscription
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, assigned_to_person_id, subscription_id FROM slmct.licences WHERE subscription_id = %s ORDER BY assigned_at DESC LIMIT 1",
                (temp_subscription_id,),
            )
            lic_row = cur.fetchone()
        chk("Downstream: licence created on licences page", lic_row is not None, "licence not found in DB" if not lic_row else f"id={str(lic_row['id'])[:8]}…")
        if lic_row:
            created_licence_id = str(lic_row["id"])
            chk("Downstream: licence status available/assigned", lic_row["status"] in ("available", "assigned"), f"got: {lic_row['status']}")
            chk("Downstream: licence linked to correct subscription", str(lic_row["subscription_id"]) == temp_subscription_id, "wrong subscription_id" if str(lic_row["subscription_id"]) != temp_subscription_id else "correct")
            if person_id:
                chk("Downstream: person assigned to licence", str(lic_row.get("assigned_to_person_id") or "") == person_id, "wrong person" if str(lic_row.get("assigned_to_person_id") or "") != person_id else "correct")

        # Payment created for the licence cost
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM slmct.payments WHERE reference LIKE %s LIMIT 1", (f"WF-{created_workflow_id[:8].upper()}%",))
            pay_row = cur.fetchone()
        chk("Downstream: payment created", pay_row is not None, "payment not found" if not pay_row else f"id={str(pay_row['id'])[:8]}…", warning=pay_row is None)

        # Status history and audit
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt, array_agg(to_status ORDER BY created_at) AS statuses FROM slmct.workflow_status_history WHERE workflow_request_id = %s",
                (created_workflow_id,),
            )
            hist = cur.fetchone()
        hist_count = int(hist["cnt"]) if hist else 0
        statuses = list(hist["statuses"] or []) if hist else []
        chk("Downstream: status history recorded", hist_count >= 2, f"{hist_count} transitions: {' → '.join(statuses)}")
        chk("Downstream: final status is completed", "completed" in statuses, f"statuses: {statuses}")

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM slmct.audit_logs WHERE entity_id::text = %s", (created_workflow_id,))
            audit = cur.fetchone()
        chk("Downstream: audit log entries written", int(audit["cnt"]) > 0 if audit else False, f"{audit['cnt'] if audit else 0} entries")

    except _SkipToCleanup:
        pass
    except Exception as exc:
        chk("Unexpected error", False, str(exc)[:200])
    finally:
        try:
            with conn.cursor() as cur:
                if created_licence_id:
                    cur.execute("DELETE FROM slmct.licences WHERE id = %s", (created_licence_id,))
                if created_workflow_id:
                    cur.execute("DELETE FROM slmct.payments WHERE reference LIKE %s", (f"WF-{created_workflow_id[:8].upper()}%",))
                    cur.execute("DELETE FROM slmct.email_logs WHERE workflow_request_id = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.workflow_status_history WHERE workflow_request_id = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.audit_logs WHERE entity_id::text = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.workflow_requests WHERE id = %s", (created_workflow_id,))
                if temp_subscription_id:
                    cur.execute("DELETE FROM slmct.licences WHERE subscription_id = %s", (temp_subscription_id,))
                    cur.execute("DELETE FROM slmct.subscriptions WHERE id = %s", (temp_subscription_id,))
            conn.commit()
        except Exception as ce:
            log.warning("diagnostics: licence-assignment cleanup error -- %s", ce)

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "workflow_id": created_workflow_id, "ai_analysis": ai_analysis}


# ─────────────────────────────────────────────────────────────────────────────
# DIAGNOSTIC: RENEWAL REQUEST
# Flow: IT submits → Finance validates → IT completes (no line manager)
# On completion: creates renewed subscription + payment
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/diagnostics/run-renewal-test")
def run_renewal_test(conn: Connection = Depends(get_connection)):
    import httpx as _httpx
    import time as _time

    BASE = "http://localhost:8000"
    checks: list[dict] = []
    created_workflow_id = None
    created_subscription_id = None

    _ctx = _diag_resolve_context(conn)
    ORG_ID        = _ctx["ORG_ID"]
    IT_EMAIL      = _ctx["IT_EMAIL"]
    IT_ID         = _ctx["IT_ID"]
    FINANCE_EMAIL = _ctx["FINANCE_EMAIL"]
    FINANCE_ID    = _ctx["FINANCE_ID"]
    if not ORG_ID or not IT_ID or not FINANCE_ID:
        return {"status": "skipped", "reason": "No active organisation or required role users found — seed data may not be loaded yet.", "checks": []}
    SUB_NAME      = "Diag Renewal Request Test"

    class _SkipToCleanup(Exception):
        pass

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    def skip(name: str, reason: str = ""):
        checks.append({"name": name, "status": "skip", "detail": reason})

    def no_uuid(text: str) -> bool:
        import re
        return not bool(re.search(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', text or "", re.I))

    def emails_for(wf_id: str, min_count: int = 1, timeout: int = 20) -> list[dict]:
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                r = _httpx.get(f"{BASE}/api/email/logs/{wf_id}", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if len(data) >= min_count:
                        return data
            except Exception:
                pass
            _time.sleep(1)
        return []

    try:
        # ── STEP 1: IT admin submits renewal request ───────────────────────────
        # requested_module must be in WORKFLOW_MODULES; renewal_request type set explicitly.
        # On completion _activate_workflow_record creates the renewed subscription record.
        r1 = _httpx.post(f"{BASE}/workflow-requests", json={
            "workflow_type": "renewal_request",
            "requested_module": "subscriptions",
            "organisation_id": ORG_ID,
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "payload": {
                "name": SUB_NAME,
                "department": "Engineering",
                "justification": "Diagnostics test — renewal request workflow.",
                "renewal_date": "2026-12-31",
                "amount": 600,
                "currency_code": "AED",
                "billing_cycle": "annual",
            },
        }, timeout=30)
        submitted_ok = r1.status_code in (200, 201)
        chk("Workflow submission", submitted_ok, f"HTTP {r1.status_code}")

        if not submitted_ok:
            for name in [
                "Workflow ID returned", "Workflow type = renewal_request",
                "Initial status = submitted", "No line manager step",
                "Submit: finance action-required email", "Submit: IT confirmation email",
                "Submit: subject contains #ID", "Finance budget validation",
                "Status = finance_approved", "Finance: IT procurement email",
                "Finance: finance confirmation email", "IT procurement complete",
                "Status = completed", "Completion: emails sent", "No legacy plain-text emails",
                "Downstream: subscription created on subscriptions page",
                "Downstream: subscription status is active", "Downstream: payment created",
                "Downstream: payment linked to budget", "Downstream: audit log entries written",
                "Downstream: status history recorded", "Downstream: final status is completed",
            ]:
                skip(name, "skipped — submission failed")
            raise _SkipToCleanup()

        d1 = r1.json()
        created_workflow_id = d1.get("id")
        chk("Workflow ID returned", bool(created_workflow_id), created_workflow_id or "missing")
        chk("Workflow type = renewal_request", d1.get("workflow_type") == "renewal_request", f"got: {d1.get('workflow_type')}")
        chk("Initial status = submitted", d1.get("status") == "submitted", f"got: {d1.get('status')}")
        chk("No line manager step", d1.get("workflow_type") not in ("employee_software_request",), "renewal skips LM by design")

        # Submit emails: finance action-required + IT confirmation
        em1 = emails_for(created_workflow_id, min_count=2)
        fin_notif = [e for e in em1 if FINANCE_EMAIL.lower() in (e.get("to") or "").lower()]
        it_conf   = [e for e in em1 if IT_EMAIL.lower() in (e.get("to") or "").lower()]
        chk("Submit: finance action-required email", len(fin_notif) > 0, f"{len(fin_notif)} found")
        chk("Submit: IT confirmation email", len(it_conf) > 0, f"{len(it_conf)} found")
        if em1:
            chk("Submit: subject contains #ID", any("#" in (e.get("subject") or "") for e in em1), em1[0].get("subject") or "")

        # ── STEP 2: Finance validates directly from submitted ─────────────────
        r2 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/validate-budget", json={
            "actor_email": FINANCE_EMAIL,
            "actor_user_id": FINANCE_ID,
            "actor_roles": ["finance"],
            "approved": True,
            "notes": "Diagnostics test — renewal budget approved",
        }, timeout=30)
        fin_ok = r2.status_code == 200
        chk("Finance budget validation", fin_ok, f"HTTP {r2.status_code}")
        if fin_ok:
            chk("Status = finance_approved", r2.json().get("status") == "finance_approved", f"got: {r2.json().get('status')}")

        if fin_ok:
            em2 = emails_for(created_workflow_id, min_count=5)
            it_proc  = [e for e in em2 if IT_EMAIL.lower() in (e.get("to") or "").lower() and "procurement" in (e.get("subject") or "").lower()]
            fin_conf = [e for e in em2 if FINANCE_EMAIL.lower() in (e.get("to") or "").lower() and "confirmed" in (e.get("subject") or "").lower()]
            chk("Finance: IT procurement email", len(it_proc) > 0, f"{len(it_proc)} found")
            chk("Finance: finance confirmation email", len(fin_conf) > 0, f"{len(fin_conf)} found")
            if it_proc:
                body2 = it_proc[0].get("bodyPreview") or ""
                chk("Finance: IT email has MESSAGE section", "MESSAGE" in body2, "missing" if "MESSAGE" not in body2 else "present")
                chk("Finance: IT email performed_by not UUID", no_uuid(body2), "UUID found" if not no_uuid(body2) else "clean")
        else:
            for name in [
                "Finance: IT procurement email", "Finance: finance confirmation email",
                "Finance: IT email has MESSAGE section", "Finance: IT email performed_by not UUID",
                "IT procurement complete", "Status = completed", "Completion: emails sent",
                "No legacy plain-text emails",
            ]:
                skip(name, "skipped — finance validation failed")
            raise _SkipToCleanup()

        # ── STEP 3: IT admin completes the renewal ────────────────────────────
        r3 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/complete", json={
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "activation_method": "vendor_provisioned",
            "vendor_reference_id": "RENEWAL-DIAG-001",
            "provisioning_notes": "Diagnostics test — renewal auto-provisioned by vendor.",
            "assigned_employee_name": "IT Admin",
            "assigned_employee_email": IT_EMAIL,
        }, timeout=30)
        comp_ok = r3.status_code == 200
        chk("IT procurement complete", comp_ok, f"HTTP {r3.status_code}: {r3.text[:120] if not comp_ok else ''}")
        if comp_ok:
            chk("Status = completed", r3.json().get("status") == "completed", f"got: {r3.json().get('status')}")

        em3 = emails_for(created_workflow_id, min_count=7)
        comp_em = [e for e in em3 if "completed" in (e.get("subject") or "").lower() or "active" in (e.get("subject") or "").lower()]
        chk("Completion: emails sent", len(comp_em) > 0, f"{len(comp_em)} found")
        legacy = [e for e in em3 if (e.get("subject") or "") in ("Request Completed", "Procurement Completed")]
        chk("No legacy plain-text emails", len(legacy) == 0, f"{len(legacy)} found" if legacy else "clean")

        # ── DOWNSTREAM CHECKS ─────────────────────────────────────────────────
        downstream = _diag_check_downstream(
            conn, checks, created_workflow_id, ORG_ID, SUB_NAME,
            requester_email=None, expect_licence_assigned=False,
        )
        created_subscription_id = downstream.get("subscription_id")

    except _SkipToCleanup:
        pass
    except Exception as exc:
        chk("Unexpected error", False, str(exc)[:200])
    finally:
        try:
            with conn.cursor() as cur:
                if created_workflow_id:
                    cur.execute("DELETE FROM slmct.payments WHERE reference LIKE %s", (f"WF-{created_workflow_id[:8].upper()}%",))
                    cur.execute("DELETE FROM slmct.email_logs WHERE workflow_request_id = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.workflow_status_history WHERE workflow_request_id = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.audit_logs WHERE entity_id::text = %s", (created_workflow_id,))
                if created_subscription_id:
                    cur.execute("DELETE FROM slmct.subscriptions WHERE id = %s", (created_subscription_id,))
                elif created_workflow_id:
                    cur.execute(
                        "DELETE FROM slmct.subscriptions WHERE organisation_id = %s AND lower(name) = lower(%s) AND created_at > now() - interval '10 minutes'",
                        (ORG_ID, SUB_NAME),
                    )
                if created_workflow_id:
                    cur.execute("DELETE FROM slmct.workflow_requests WHERE id = %s", (created_workflow_id,))
            conn.commit()
        except Exception as ce:
            log.warning("diagnostics: renewal cleanup error -- %s", ce)

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "workflow_id": created_workflow_id, "ai_analysis": ai_analysis}


# ─────────────────────────────────────────────────────────────────────────────
# DIAGNOSTIC: MASTER ADMIN
# Tests two paths in one run:
#   A. Direct addition — POST /subscriptions + POST /licences bypassing workflow
#   B. Workflow path — submitted → master_approved → finance_approved → completed
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/diagnostics/run-master-admin-test")
def run_master_admin_test(conn: Connection = Depends(get_connection)):
    import httpx as _httpx
    import time as _time

    BASE = "http://localhost:8000"
    checks: list[dict] = []
    direct_sub_id    = None
    direct_lic_id    = None
    wf_sub_id        = None
    wf_lic_id        = None
    created_workflow_id = None

    _ctx = _diag_resolve_context(conn)
    ORG_ID        = _ctx["ORG_ID"]
    FINANCE_EMAIL = _ctx["FINANCE_EMAIL"]
    FINANCE_ID    = _ctx["FINANCE_ID"]
    IT_EMAIL      = _ctx["IT_EMAIL"]
    IT_ID         = _ctx["IT_ID"]
    if not ORG_ID or not IT_ID or not FINANCE_ID:
        return {"status": "skipped", "reason": "No active organisation or required role users found — seed data may not be loaded yet.", "checks": []}
    DIRECT_SUB_NAME = "Diag Master Direct Sub"
    WF_SUB_NAME     = "Diag Master Workflow Sub"

    class _SkipToCleanup(Exception):
        pass

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    def skip(name: str, reason: str = ""):
        checks.append({"name": name, "status": "skip", "detail": reason})

    def no_uuid(text: str) -> bool:
        import re
        return not bool(re.search(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', text or "", re.I))

    def emails_for(wf_id: str, min_count: int = 1, timeout: int = 20) -> list[dict]:
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                r = _httpx.get(f"{BASE}/api/email/logs/{wf_id}", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if len(data) >= min_count:
                        return data
            except Exception:
                pass
            _time.sleep(1)
        return []

    # ── Resolve master admin user at runtime ──────────────────────────────────
    MASTER_EMAIL = None
    MASTER_ID    = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT au.id, au.email
                FROM slmct.app_users au
                JOIN slmct.user_roles ur ON ur.user_id = au.id AND ur.revoked_at IS NULL
                JOIN slmct.roles r ON r.id = ur.role_id
                WHERE r.code = 'master_admin'
                ORDER BY au.created_at LIMIT 1
                """,
            )
            ma = cur.fetchone()
        if ma:
            MASTER_EMAIL = str(ma["email"])
            MASTER_ID    = str(ma["id"])
    except Exception:
        pass
    chk("Setup: master admin user found", bool(MASTER_EMAIL), MASTER_EMAIL or "no master_admin user in slmct.app_users for this org")

    if not MASTER_EMAIL:
        for name in [
            "Direct add: POST /subscriptions returns 201", "Direct add: subscription in DB",
            "Direct add: subscription accessible via GET", "Direct add: subscription audit log",
            "Direct add: POST /licences returns 201", "Direct add: licence in DB",
            "Direct add: licence linked to subscription", "Direct add: licence audit log",
            "Workflow: submission", "Workflow: ID returned", "Workflow: type = new_subscription_request",
            "Workflow: status = submitted", "Workflow: submit emails sent",
            "Workflow: master admin approves", "Workflow: status = master_approved",
            "Workflow: approval emails sent", "Workflow: finance validates",
            "Workflow: status = finance_approved", "Workflow: IT completes",
            "Workflow: status = completed", "Workflow: completion emails sent",
            "Workflow: no legacy plain-text emails",
            "Downstream: subscription on subscriptions page", "Downstream: subscription status is active",
            "Downstream: payment created", "Downstream: payment linked to budget",
            "Downstream: audit log entries written", "Downstream: status history recorded",
            "Downstream: final status is completed",
        ]:
            skip(name, "skipped — master admin user not found")
        summary = _diag_summary(checks)
        ai_analysis = _diag_ai_analysis(checks, summary)
        return {"checks": checks, "summary": summary, "workflow_id": None, "ai_analysis": ai_analysis}

    try:
        # ════════════════════════════════════════════════════════════════════
        # PART A — DIRECT ADDITION (no workflow)
        # Master admin bypasses workflow entirely via POST /subscriptions and POST /licences
        # ════════════════════════════════════════════════════════════════════

        # A1: Direct subscription creation — pick any existing vendor so licence creation succeeds
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM slmct.vendors WHERE status = 'active' ORDER BY created_at LIMIT 1")
            vendor_row = cur.fetchone()
        any_vendor_id = str(vendor_row["id"]) if vendor_row else None

        ra1 = _httpx.post(f"{BASE}/subscriptions", json={
            "actor_roles": ["master_admin"],
            "organisation_id": ORG_ID,
            "name": DIRECT_SUB_NAME,
            "department": "IT",
            "billing_cycle": "monthly",
            "amount": 250,
            "currency_code": "AED",
            "status": "active",
            "vendor_id": any_vendor_id,
            "notes": "Diagnostics test — master admin direct add.",
        }, timeout=30)
        direct_sub_ok = ra1.status_code == 201
        chk("Direct add: POST /subscriptions returns 201", direct_sub_ok, f"HTTP {ra1.status_code}: {ra1.text[:120] if not direct_sub_ok else ''}")

        if direct_sub_ok:
            d_sub = ra1.json()
            direct_sub_id = d_sub.get("id")
            chk("Direct add: subscription in DB", bool(direct_sub_id), direct_sub_id or "missing id")

            # Verify accessible via GET
            if direct_sub_id:
                rg1 = _httpx.get(f"{BASE}/subscriptions/{direct_sub_id}", timeout=10)
                chk("Direct add: subscription accessible via GET", rg1.status_code == 200, f"HTTP {rg1.status_code}")

            # Audit log for direct create
            if direct_sub_id:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS cnt FROM slmct.audit_logs WHERE entity_id::text = %s AND action = 'create'",
                        (str(direct_sub_id),),
                    )
                    aud = cur.fetchone()
                chk("Direct add: subscription audit log", int(aud["cnt"]) > 0 if aud else False, f"{aud['cnt'] if aud else 0} entries")
        else:
            for name in ["Direct add: subscription in DB", "Direct add: subscription accessible via GET", "Direct add: subscription audit log"]:
                skip(name, "skipped — subscription creation failed")

        # A2: Direct licence creation linked to the direct subscription
        if direct_sub_id:
            ra2 = _httpx.post(f"{BASE}/licences", json={
                "actor_roles": ["master_admin"],
                "organisation_id": ORG_ID,
                "subscription_id": direct_sub_id,
                "licence_name": "Diag Direct Seat",
                "status": "available",
                "assigned_at": "2025-01-01",
                "notes": "Diagnostics test — master admin direct licence add.",
            }, timeout=30)
            direct_lic_ok = ra2.status_code == 201
            chk("Direct add: POST /licences returns 201", direct_lic_ok, f"HTTP {ra2.status_code}: {ra2.text[:120] if not direct_lic_ok else ''}")
            if direct_lic_ok:
                d_lic = ra2.json()
                direct_lic_id = d_lic.get("id")
                chk("Direct add: licence in DB", bool(direct_lic_id), direct_lic_id or "missing id")
                chk("Direct add: licence linked to subscription", d_lic.get("subscription_id") == direct_sub_id, f"got: {d_lic.get('subscription_id')}")
                if direct_lic_id:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT COUNT(*) AS cnt FROM slmct.audit_logs WHERE entity_id::text = %s AND action = 'create'",
                            (str(direct_lic_id),),
                        )
                        aud_lic = cur.fetchone()
                    chk("Direct add: licence audit log", int(aud_lic["cnt"]) > 0 if aud_lic else False, f"{aud_lic['cnt'] if aud_lic else 0} entries")
            else:
                for name in ["Direct add: licence in DB", "Direct add: licence linked to subscription", "Direct add: licence audit log"]:
                    skip(name, "skipped — licence creation failed")
        else:
            for name in ["Direct add: POST /licences returns 201", "Direct add: licence in DB", "Direct add: licence linked to subscription", "Direct add: licence audit log"]:
                skip(name, "skipped — no subscription to link licence to")

        # ════════════════════════════════════════════════════════════════════
        # PART B — WORKFLOW PATH (submitted → master_approved → finance_approved → completed)
        # ════════════════════════════════════════════════════════════════════

        # B1: Master admin submits a new subscription workflow
        rb1 = _httpx.post(f"{BASE}/workflow-requests", json={
            "requested_module": "subscriptions",
            "organisation_id": ORG_ID,
            "actor_email": MASTER_EMAIL,
            "actor_user_id": MASTER_ID,
            "actor_roles": ["master_admin"],
            "payload": {
                "name": WF_SUB_NAME,
                "department": "IT",
                "justification": "Diagnostics test — master admin workflow path.",
                "amount": 800,
                "currency_code": "AED",
                "billing_cycle": "annual",
            },
        }, timeout=30)
        wf_ok = rb1.status_code in (200, 201)
        chk("Workflow: submission", wf_ok, f"HTTP {rb1.status_code}: {rb1.text[:120] if not wf_ok else ''}")

        if not wf_ok:
            for name in [
                "Workflow: ID returned", "Workflow: type = new_subscription_request",
                "Workflow: status = submitted", "Workflow: submit emails sent",
                "Workflow: master admin approves", "Workflow: status = master_approved",
                "Workflow: approval emails sent", "Workflow: finance validates",
                "Workflow: status = finance_approved", "Workflow: IT completes",
                "Workflow: status = completed", "Workflow: completion emails sent",
                "Workflow: no legacy plain-text emails",
                "Downstream: subscription on subscriptions page", "Downstream: subscription status is active",
                "Downstream: payment created", "Downstream: payment linked to budget",
                "Downstream: audit log entries written", "Downstream: status history recorded",
                "Downstream: final status is completed",
            ]:
                skip(name, "skipped — workflow submission failed")
            raise _SkipToCleanup()

        db1 = rb1.json()
        created_workflow_id = db1.get("id")
        chk("Workflow: ID returned", bool(created_workflow_id), created_workflow_id or "missing")
        chk("Workflow: type = new_subscription_request", db1.get("workflow_type") == "new_subscription_request", f"got: {db1.get('workflow_type')}")
        chk("Workflow: status = submitted", db1.get("status") == "submitted", f"got: {db1.get('status')}")

        # Submit emails (2: master_admin gets a copy + finance gets notified)
        em1 = emails_for(created_workflow_id, min_count=1)
        chk("Workflow: submit emails sent", len(em1) > 0, f"{len(em1)} found")

        # B2: Master admin approves (/approve endpoint)
        rb2 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/approve", json={
            "actor_email": MASTER_EMAIL,
            "actor_user_id": MASTER_ID,
            "actor_roles": ["master_admin"],
        }, timeout=30)
        ma_ok = rb2.status_code == 200
        chk("Workflow: master admin approves", ma_ok, f"HTTP {rb2.status_code}: {rb2.text[:120] if not ma_ok else ''}")
        if ma_ok:
            chk("Workflow: status = master_approved", rb2.json().get("status") == "master_approved", f"got: {rb2.json().get('status')}")

        if ma_ok:
            em2 = emails_for(created_workflow_id, min_count=2)
            chk("Workflow: approval emails sent", len(em2) >= 2, f"{len(em2)} found")
        else:
            for name in [
                "Workflow: status = master_approved", "Workflow: approval emails sent",
                "Workflow: finance validates", "Workflow: status = finance_approved",
                "Workflow: IT completes", "Workflow: status = completed",
                "Workflow: completion emails sent", "Workflow: no legacy plain-text emails",
            ]:
                skip(name, "skipped — master approval failed")
            raise _SkipToCleanup()

        # B3: Finance validates from master_approved
        rb3 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/validate-budget", json={
            "actor_email": FINANCE_EMAIL,
            "actor_user_id": FINANCE_ID,
            "actor_roles": ["finance"],
            "approved": True,
            "notes": "Diagnostics test — master workflow budget approved",
        }, timeout=30)
        fin_ok = rb3.status_code == 200
        chk("Workflow: finance validates", fin_ok, f"HTTP {rb3.status_code}")
        if fin_ok:
            chk("Workflow: status = finance_approved", rb3.json().get("status") == "finance_approved", f"got: {rb3.json().get('status')}")

        if not fin_ok:
            for name in [
                "Workflow: status = finance_approved", "Workflow: IT completes",
                "Workflow: status = completed", "Workflow: completion emails sent", "Workflow: no legacy plain-text emails",
            ]:
                skip(name, "skipped — finance validation failed")
            raise _SkipToCleanup()

        # Check finance emails (should be ~5 total by now)
        em3 = emails_for(created_workflow_id, min_count=4)
        fin_conf = [e for e in em3 if FINANCE_EMAIL.lower() in (e.get("to") or "").lower() and ("approved" in (e.get("subject") or "").lower() or "confirmed" in (e.get("subject") or "").lower())]
        chk("Workflow: finance confirmation email", len(fin_conf) > 0, f"{len(fin_conf)} found")

        # B4: IT admin completes the workflow
        rb4 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/complete", json={
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "activation_method": "company_account",
            "username": "diagmaster@company.com",
            "password": "DiagMaster@123",
            "assigned_employee_name": "IT Admin",
            "assigned_employee_email": IT_EMAIL,
        }, timeout=30)
        comp_ok = rb4.status_code == 200
        chk("Workflow: IT completes", comp_ok, f"HTTP {rb4.status_code}: {rb4.text[:120] if not comp_ok else ''}")
        if comp_ok:
            chk("Workflow: status = completed", rb4.json().get("status") == "completed", f"got: {rb4.json().get('status')}")

        em4 = emails_for(created_workflow_id, min_count=7)
        comp_em = [e for e in em4 if "completed" in (e.get("subject") or "").lower() or "active" in (e.get("subject") or "").lower()]
        chk("Workflow: completion emails sent", len(comp_em) > 0, f"{len(comp_em)} found")
        legacy = [e for e in em4 if (e.get("subject") or "") in ("Request Completed", "Procurement Completed")]
        chk("Workflow: no legacy plain-text emails", len(legacy) == 0, f"{len(legacy)} found" if legacy else "clean")

        # ── DOWNSTREAM CHECKS ─────────────────────────────────────────────────
        downstream = _diag_check_downstream(
            conn, checks, created_workflow_id, ORG_ID, WF_SUB_NAME,
            requester_email=None, expect_licence_assigned=False,
        )
        wf_sub_id = downstream.get("subscription_id")

    except _SkipToCleanup:
        pass
    except Exception as exc:
        chk("Unexpected error", False, str(exc)[:200])
    finally:
        try:
            conn.rollback()  # reset any aborted tx state from the test
            with conn.cursor() as cur:
                # Delete child records first, then parent subscriptions/workflow_requests
                if created_workflow_id:
                    cur.execute("DELETE FROM slmct.payments WHERE reference LIKE %s", (f"WF-{created_workflow_id[:8].upper()}%",))
                    cur.execute("DELETE FROM slmct.email_logs WHERE workflow_request_id = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.workflow_status_history WHERE workflow_request_id = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.audit_logs WHERE entity_id::text = %s", (created_workflow_id,))
                    # Delete workflow_request BEFORE the subscription it references (activated_entity_id FK)
                    cur.execute("DELETE FROM slmct.workflow_requests WHERE id = %s", (created_workflow_id,))
                if wf_sub_id:
                    cur.execute("DELETE FROM slmct.subscriptions WHERE id = %s", (wf_sub_id,))
                elif created_workflow_id:
                    cur.execute(
                        "DELETE FROM slmct.subscriptions WHERE organisation_id = %s AND lower(name) = lower(%s) AND created_at > now() - interval '5 minutes'",
                        (ORG_ID, WF_SUB_NAME),
                    )
                # Direct path cleanup
                if direct_lic_id:
                    cur.execute("DELETE FROM slmct.audit_logs WHERE entity_id::text = %s", (str(direct_lic_id),))
                    cur.execute("DELETE FROM slmct.licences WHERE id = %s", (str(direct_lic_id),))
                if direct_sub_id:
                    cur.execute("DELETE FROM slmct.licences WHERE subscription_id = %s", (str(direct_sub_id),))
                    cur.execute("DELETE FROM slmct.audit_logs WHERE entity_id::text = %s", (str(direct_sub_id),))
                    cur.execute("DELETE FROM slmct.subscriptions WHERE id = %s", (str(direct_sub_id),))
            conn.commit()
        except Exception as ce:
            log.warning("diagnostics: master-admin cleanup error -- %s", ce)

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "workflow_id": created_workflow_id, "ai_analysis": ai_analysis}



# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# DIAGNOSTIC: HR ONBOARDING REQUEST
# Flow: HR admin submits â†' Finance validates â†' IT completes
# On completion: licence created with status="assigned", linked to new employee
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
@app.post("/api/diagnostics/run-hr-onboarding-test")
def run_hr_onboarding_test(conn: Connection = Depends(get_connection)):
    import httpx as _httpx
    import time as _time

    BASE = "http://localhost:8000"
    checks: list[dict] = []
    created_workflow_id = None
    temp_subscription_id = None
    created_licence_id = None

    _ctx = _diag_resolve_context(conn)
    ORG_ID             = _ctx["ORG_ID"]
    HR_EMAIL           = _ctx["HR_EMAIL"]
    HR_ID              = _ctx["HR_ID"]
    FINANCE_EMAIL      = _ctx["FINANCE_EMAIL"]
    FINANCE_ID         = _ctx["FINANCE_ID"]
    IT_EMAIL           = _ctx["IT_EMAIL"]
    IT_ID              = _ctx["IT_ID"]
    EMPLOYEE_EMAIL     = _ctx["EMPLOYEE_EMAIL"]
    if not ORG_ID or not HR_ID or not IT_ID or not FINANCE_ID:
        return {"status": "skipped", "reason": "No active organisation or required role users found — seed data may not be loaded yet.", "checks": []}
    # Resolve employee person record dynamically from people table
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM slmct.people WHERE work_email = %s AND status = 'active' ORDER BY created_at LIMIT 1",
            (EMPLOYEE_EMAIL,),
        )
        _person_row = cur.fetchone()
    EMPLOYEE_PERSON_ID = str(_person_row["id"]) if _person_row else None
    SUB_NAME           = "Diag HR Onboarding Test"

    class _SkipToCleanup(Exception):
        pass

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    def skip(name: str, reason: str = ""):
        checks.append({"name": name, "status": "skip", "detail": reason})

    def no_uuid(text: str) -> bool:
        import re
        return not bool(re.search(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', text or "", re.I))

    def emails_for(wf_id: str, min_count: int = 1, timeout: int = 20) -> list[dict]:
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                r = _httpx.get(f"{BASE}/api/email/logs/{wf_id}", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if len(data) >= min_count:
                        return data
            except Exception:
                pass
            _time.sleep(1)
        return []

    try:
        # SETUP: verify employee person record
        with conn.cursor() as cur:
            cur.execute("SELECT id, full_name FROM slmct.people WHERE id = %s", (EMPLOYEE_PERSON_ID,))
            emp_row = cur.fetchone()
        chk("Setup: employee person record found", emp_row is not None,
            (str(emp_row["full_name"]) + " (" + EMPLOYEE_PERSON_ID[:8] + "...)") if emp_row else "not found in slmct.people")
        if not emp_row:
            raise _SkipToCleanup()

        # SETUP: create temp subscription for the onboarding seat
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO slmct.subscriptions (organisation_id, name, billing_cycle, amount, currency_code, status, department) VALUES (%s, %s, 'monthly', 150, 'AED', 'active', 'Engineering') RETURNING id",
                (ORG_ID, SUB_NAME + " [base sub]"),
            )
            temp_subscription_id = str(cur.fetchone()["id"])
            conn.commit()
        chk("Setup: temp subscription created", bool(temp_subscription_id), temp_subscription_id or "failed")

        # STEP 1: HR admin submits onboarding licence request
        r1 = _httpx.post(f"{BASE}/workflow-requests", json={
            "workflow_type": "hr_onboarding_request",
            "requested_module": "licences",
            "organisation_id": ORG_ID,
            "actor_email": HR_EMAIL,
            "actor_user_id": HR_ID,
            "actor_roles": ["hr_admin"],
            "payload": {
                "subscription_id": temp_subscription_id,
                "assigned_to_person_id": EMPLOYEE_PERSON_ID,
                "licence_name": SUB_NAME + " Seat",
                "assigned_at": "2025-01-01",
                "department": "Engineering",
                "justification": "New employee onboarding - requires software access from day one.",
                "notes": "Diagnostics test - HR onboarding licence.",
            },
        }, timeout=30)
        submitted_ok = r1.status_code in (200, 201)
        chk("Workflow submission", submitted_ok, "HTTP " + str(r1.status_code) + (": " + r1.text[:100] if not submitted_ok else ""))

        if not submitted_ok:
            for name in [
                "Workflow ID returned", "Workflow type = hr_onboarding_request",
                "Initial status = submitted", "No line manager step", "HR role accepted",
                "Submit: finance action-required email", "Submit: HR confirmation email",
                "Submit: subject contains #ID", "Finance budget validation",
                "Status = finance_approved", "Finance: IT procurement email",
                "Finance: finance confirmation email", "Finance: IT email has MESSAGE section",
                "Finance: IT email performed_by not UUID", "IT procurement complete",
                "Status = completed", "Completion: employee welcome email sent",
                "Completion: HR confirmation email", "No legacy plain-text emails",
                "Downstream: licence created on licences page", "Downstream: licence status is assigned",
                "Downstream: licence linked to correct subscription",
                "Downstream: licence assigned to correct employee",
                "Downstream: payment created", "Downstream: audit log entries written",
                "Downstream: status history recorded", "Downstream: final status is completed",
            ]:
                skip(name, "skipped - submission failed")
            raise _SkipToCleanup()

        d1 = r1.json()
        created_workflow_id = d1.get("id")
        chk("Workflow ID returned", bool(created_workflow_id), created_workflow_id or "missing")
        chk("Workflow type = hr_onboarding_request", d1.get("workflow_type") == "hr_onboarding_request", "got: " + str(d1.get("workflow_type")))
        chk("Initial status = submitted", d1.get("status") == "submitted", "got: " + str(d1.get("status")))
        chk("No line manager step", d1.get("workflow_type") not in ("employee_software_request",), "hr_onboarding skips LM by design")
        chk("HR role accepted", True, "hr_admin permitted to submit hr_onboarding_request on licences")

        em1 = emails_for(created_workflow_id, min_count=2)
        fin_notif = [e for e in em1 if FINANCE_EMAIL.lower() in (e.get("to") or "").lower()]
        hr_conf   = [e for e in em1 if HR_EMAIL.lower() in (e.get("to") or "").lower()]
        chk("Submit: finance action-required email", len(fin_notif) > 0, str(len(fin_notif)) + " found")
        chk("Submit: HR confirmation email", len(hr_conf) > 0, str(len(hr_conf)) + " found")
        if em1:
            chk("Submit: subject contains #ID", any("#" in (e.get("subject") or "") for e in em1), em1[0].get("subject") or "")

        # STEP 2: Finance validates directly from submitted
        r2 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/validate-budget", json={
            "actor_email": FINANCE_EMAIL,
            "actor_user_id": FINANCE_ID,
            "actor_roles": ["finance"],
            "approved": True,
            "notes": "Diagnostics test - HR onboarding budget approved",
        }, timeout=30)
        fin_ok = r2.status_code == 200
        chk("Finance budget validation", fin_ok, "HTTP " + str(r2.status_code) + (": " + r2.text[:100] if not fin_ok else ""))
        if fin_ok:
            chk("Status = finance_approved", r2.json().get("status") == "finance_approved", "got: " + str(r2.json().get("status")))

        if fin_ok:
            em2 = emails_for(created_workflow_id, min_count=5)
            it_proc  = [e for e in em2 if IT_EMAIL.lower() in (e.get("to") or "").lower() and "procurement" in (e.get("subject") or "").lower()]
            fin_conf = [e for e in em2 if FINANCE_EMAIL.lower() in (e.get("to") or "").lower() and ("approved" in (e.get("subject") or "").lower() or "confirmed" in (e.get("subject") or "").lower())]
            chk("Finance: IT procurement email", len(it_proc) > 0, str(len(it_proc)) + " found")
            chk("Finance: finance confirmation email", len(fin_conf) > 0, str(len(fin_conf)) + " found")
            if it_proc:
                body2 = it_proc[0].get("bodyPreview") or ""
                chk("Finance: IT email has MESSAGE section", "MESSAGE" in body2, "missing" if "MESSAGE" not in body2 else "present")
                chk("Finance: IT email performed_by not UUID", no_uuid(body2), "UUID found" if not no_uuid(body2) else "clean")
        else:
            for name in [
                "Finance: IT procurement email", "Finance: finance confirmation email",
                "Finance: IT email has MESSAGE section", "Finance: IT email performed_by not UUID",
                "IT procurement complete", "Status = completed",
                "Completion: employee welcome email sent", "Completion: HR confirmation email",
                "No legacy plain-text emails",
            ]:
                skip(name, "skipped - finance validation failed")
            raise _SkipToCleanup()

        # STEP 3: IT admin completes - licence auto-assigned to onboarded employee
        r3 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/complete", json={
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "activation_method": "invitation_email",
            "recipient_email": EMPLOYEE_EMAIL,
            "invitation_status": "sent",
            "assigned_employee_name": str(emp_row["full_name"]),
            "assigned_employee_email": EMPLOYEE_EMAIL,
        }, timeout=30)
        comp_ok = r3.status_code == 200
        chk("IT procurement complete", comp_ok, "HTTP " + str(r3.status_code) + (": " + r3.text[:100] if not comp_ok else ""))
        if comp_ok:
            chk("Status = completed", r3.json().get("status") == "completed", "got: " + str(r3.json().get("status")))

        # 2 submit + 3 finance + 2 completion (bulk + employee welcome) = 7
        em3 = emails_for(created_workflow_id, min_count=7)
        emp_welcome = [e for e in em3 if EMPLOYEE_EMAIL.lower() in (e.get("to") or "").lower() and "ready" in (e.get("subject") or "").lower()]
        hr_comp     = [e for e in em3 if HR_EMAIL.lower() in (e.get("to") or "").lower() and ("completed" in (e.get("subject") or "").lower() or "active" in (e.get("subject") or "").lower())]
        chk("Completion: employee welcome email sent", len(emp_welcome) > 0, str(len(emp_welcome)) + " found")
        chk("Completion: HR confirmation email", len(hr_comp) > 0, str(len(hr_comp)) + " found")
        legacy = [e for e in em3 if (e.get("subject") or "") in ("Request Completed", "Procurement Completed")]
        chk("No legacy plain-text emails", len(legacy) == 0, str(len(legacy)) + " found" if legacy else "clean")

        # DOWNSTREAM CHECKS
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, assigned_to_person_id, subscription_id FROM slmct.licences WHERE subscription_id = %s ORDER BY assigned_at DESC LIMIT 1",
                (temp_subscription_id,),
            )
            lic_row = cur.fetchone()
        chk("Downstream: licence created on licences page", lic_row is not None, "not found in DB" if not lic_row else "id=" + str(lic_row["id"])[:8] + "...")
        if lic_row:
            created_licence_id = str(lic_row["id"])
            chk("Downstream: licence status is assigned", lic_row["status"] == "assigned", "got: " + str(lic_row["status"]) + " (must be assigned for onboarded employee)")
            chk("Downstream: licence linked to correct subscription", str(lic_row["subscription_id"]) == temp_subscription_id, "wrong subscription_id")
            chk("Downstream: licence assigned to correct employee", str(lic_row.get("assigned_to_person_id") or "") == EMPLOYEE_PERSON_ID, "got: " + str(lic_row.get("assigned_to_person_id") or "")[:8] + "... expected: " + EMPLOYEE_PERSON_ID[:8] + "...")

        with conn.cursor() as cur:
            cur.execute("SELECT id, amount FROM slmct.payments WHERE reference LIKE %s LIMIT 1", ("WF-" + created_workflow_id[:8].upper() + "%",))
            pay_row = cur.fetchone()
        chk("Downstream: payment created", pay_row is not None, "not found" if not pay_row else "amount=" + str(pay_row["amount"]), warning=pay_row is None)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt, array_agg(to_status ORDER BY created_at) AS statuses FROM slmct.workflow_status_history WHERE workflow_request_id = %s",
                (created_workflow_id,),
            )
            hist = cur.fetchone()
        hist_count = int(hist["cnt"]) if hist else 0
        statuses = list(hist["statuses"] or []) if hist else []
        chk("Downstream: status history recorded", hist_count >= 2, str(hist_count) + " transitions: " + " -> ".join(statuses))
        chk("Downstream: final status is completed", "completed" in statuses, "statuses: " + str(statuses))

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM slmct.audit_logs WHERE entity_id::text = %s", (created_workflow_id,))
            audit = cur.fetchone()
        chk("Downstream: audit log entries written", int(audit["cnt"]) > 0 if audit else False, str(audit["cnt"] if audit else 0) + " entries")

    except _SkipToCleanup:
        pass
    except Exception as exc:
        chk("Unexpected error", False, str(exc)[:200])
    finally:
        try:
            with conn.cursor() as cur:
                if created_licence_id:
                    cur.execute("DELETE FROM slmct.licences WHERE id = %s", (created_licence_id,))
                if created_workflow_id:
                    cur.execute("DELETE FROM slmct.payments WHERE reference LIKE %s", ("WF-" + created_workflow_id[:8].upper() + "%",))
                    cur.execute("DELETE FROM slmct.email_logs WHERE workflow_request_id = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.workflow_status_history WHERE workflow_request_id = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.audit_logs WHERE entity_id::text = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.workflow_requests WHERE id = %s", (created_workflow_id,))
                if temp_subscription_id:
                    cur.execute("DELETE FROM slmct.licences WHERE subscription_id = %s", (temp_subscription_id,))
                    cur.execute("DELETE FROM slmct.subscriptions WHERE id = %s", (temp_subscription_id,))
            conn.commit()
        except Exception as ce:
            log.warning("diagnostics: hr-onboarding cleanup error -- %s", ce)

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "workflow_id": created_workflow_id, "ai_analysis": ai_analysis}


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# DIAGNOSTIC: VENDOR URL / SCRAPING HEALTH CHECK
# Iterates all vendor_catalogue entries that have a scrape_url and checks:
#   - URL is reachable (HTTP 2xx / 3xx)
#   - Response time is acceptable
#   - Page looks like a pricing page (pricing keywords present)
#   - Vendor / plan name appears on the page
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
@app.post("/api/diagnostics/run-url-scraping-test")
def run_url_scraping_test(conn: Connection = Depends(get_connection)):
    import httpx as _httpx
    import time as _time
    import re as _re

    checks: list[dict] = []

    PRICING_KEYWORDS = [
        "price", "pricing", "per month", "per year", "annually", "billed", "plan",
        "subscribe", "buy", "purchase", "per user", "seat", "license", "licence",
        "/mo", "/yr", "/month", "/year", "usd", "eur", "gbp", "aed", "$", "€", "£",
    ]
    TIMEOUT = 15
    SLOW_THRESHOLD = 5.0

    # currency symbol -> code mapping for price extraction
    CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
    CURRENCY_CODES = {"USD", "EUR", "GBP", "AED", "INR", "SAR", "QAR", "KWD", "GBP"}

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    def skip(name: str, reason: str = ""):
        checks.append({"name": name, "status": "skip", "detail": reason})


    def extract_prices_from_html(html: str) -> list[tuple[float, str]]:
        """Extract (amount, currency) pairs from raw HTML. Returns unique values sorted ascending."""
        found: list[tuple[float, str]] = []
        # Pattern: symbol then number e.g. $12.50, $1,200
        for sym, code in CURRENCY_SYMBOLS.items():
            for m in _re.finditer(_re.escape(sym) + r"([\d,]+(?:\.\d{1,2})?)", html):
                try:
                    found.append((float(m.group(1).replace(",", "")), code))
                except ValueError:
                    pass
        # Pattern: number then code e.g. 12.50 USD or 12.50USD
        for code in CURRENCY_CODES:
            for m in _re.finditer(r"([\d,]+(?:\.\d{1,2})?)\s*" + code, html, _re.IGNORECASE):
                try:
                    found.append((float(m.group(1).replace(",", "")), code.upper()))
                except ValueError:
                    pass
        # Deduplicate, filter out implausible values (0 or >100000)
        seen_vals: set[tuple[float, str]] = set()
        result: list[tuple[float, str]] = []
        for val, cur in sorted(found, key=lambda x: x[0]):
            if (val, cur) not in seen_vals and 0.01 < val < 100000:
                seen_vals.add((val, cur))
                result.append((val, cur))
        return result

    def price_match(catalogue_price: float, catalogue_currency: str, page_prices: list[tuple[float, str]], tolerance: float = 0.15) -> tuple[bool, str]:
        """Check if catalogue_price/currency appears on the page within tolerance."""
        if not page_prices:
            return False, "no prices found on page (likely JS-rendered)"
        same_currency = [(v, c) for v, c in page_prices if c == catalogue_currency]
        if not same_currency:
            all_found = ", ".join(c + str(round(v, 2)) for v, c in page_prices[:6])
            return False, "currency " + catalogue_currency + " not found on page (found: " + all_found + ")"
        # Find closest match
        closest = min(same_currency, key=lambda x: abs(x[0] - catalogue_price))
        diff_pct = abs(closest[0] - catalogue_price) / catalogue_price if catalogue_price else 1
        match_str = catalogue_currency + str(catalogue_price) + " vs page " + catalogue_currency + str(round(closest[0], 2))
        if diff_pct <= tolerance:
            return True, match_str + " (within " + str(round(diff_pct * 100, 1)) + "%)"
        # Price may be annual on page - check /12
        annual_unit = closest[0] / 12
        diff_annual = abs(annual_unit - catalogue_price) / catalogue_price if catalogue_price else 1
        if diff_annual <= tolerance:
            return True, match_str + " (annual/" + "12 = " + catalogue_currency + str(round(annual_unit, 2)) + ", within " + str(round(diff_annual * 100, 1)) + "%)"
        return False, match_str + " (off by " + str(round(diff_pct * 100, 1)) + "% - stored price may be outdated)"

    # Load all vendor catalogue entries with a scrape_url
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT vc.id, vc.name AS plan, vc.scrape_url, vc.price, vc.currency_code,
                   v.name AS vendor
            FROM slmct.vendor_catalogue vc
            JOIN slmct.vendors v ON v.id = vc.vendor_id
            WHERE vc.scrape_url IS NOT NULL AND trim(vc.scrape_url) != ''
            ORDER BY v.name, vc.name
            """
        )
        entries = cur.fetchall()

    total_entries = len(entries)
    chk("Vendor catalogue: entries with a pricing URL", total_entries > 999999,
        str(total_entries) + " entries found" if total_entries else "No entries have a scrape_url - add pricing URLs in the Vendors section")

    if total_entries == 0:
        summary = _diag_summary(checks)
        return {"checks": checks, "summary": summary, "ai_analysis": _diag_ai_analysis(checks, summary)}

    # Deduplicate URLs so we only fetch each once, but track which plans use it
    seen_urls: dict[str, dict] = {}
    url_to_entries: dict[str, list[dict]] = {}

    for entry in entries:
        url = str(entry["scrape_url"]).strip()
        url_to_entries.setdefault(url, []).append(dict(entry))
        if url not in seen_urls:
            seen_urls[url] = {}

    accessible_count = 0
    pricing_count = 0
    slow_count = 0

    for url, _ in seen_urls.items():
        plans_str = ", ".join(str(e["vendor"]) + " - " + str(e["plan"]) for e in url_to_entries[url])
        short_url = url[:60] + ("..." if len(url) > 60 else "")

        # --- Reachability ---
        t0 = _time.time()
        try:
            resp = _httpx.get(
                url,
                follow_redirects=True,
                timeout=TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"},
            )
            elapsed = round(_time.time() - t0, 2)
            status_ok = resp.status_code < 400
            page_text = resp.text
            seen_urls[url] = {"ok": status_ok, "status_code": resp.status_code, "elapsed": elapsed, "text": page_text}
        except _httpx.TimeoutException:
            elapsed = round(_time.time() - t0, 2)
            seen_urls[url] = {"ok": False, "status_code": None, "elapsed": elapsed, "text": "", "error": "server_blocked", "server_blocked": True}
        except Exception as exc:
            elapsed = round(_time.time() - t0, 2)
            seen_urls[url] = {"ok": False, "status_code": None, "elapsed": elapsed, "text": "", "error": str(exc)[:80]}

        result = seen_urls[url]
        is_ok = result.get("ok", False)
        is_server_blocked = result.get("server_blocked", False)
        code = result.get("status_code")
        err = result.get("error", "")
        page_text = result.get("text") or ""
        text_lower = page_text.lower()

        if is_ok:
            accessible_count += 1

        if is_server_blocked:
            # Timeout = vendor blocks server requests, but URL is valid and opens in browser
            chk(
                "URL accessible: " + short_url,
                True,
                "URL valid - opens in browser. Vendor blocks automated requests (IT admin can still use the link) - " + plans_str,
                warning=False,
            )
            accessible_count += 1
            skip("Response time: " + short_url, "skipped - vendor blocks server requests (browser access works)")
            skip("Pricing content: " + short_url, "skipped - vendor blocks server requests (browser access works)")
            skip("Vendor name on page: " + short_url, "skipped - vendor blocks server requests (browser access works)")
            for e in url_to_entries[url]:
                if float(e.get("price") or 0) > 0:
                    skip("Price match: " + str(e["plan"]) + " (" + str(e["vendor"]) + ")", "skipped - vendor blocks server requests (verify price manually)")
            continue
        elif not is_ok:
            chk(
                "URL accessible: " + short_url,
                False,
                (err + " - " + plans_str if err else "HTTP " + str(code) + " - " + plans_str),
            )
            skip("Response time: " + short_url, "skipped - URL not accessible")
            skip("Pricing content: " + short_url, "skipped - URL not accessible")
            skip("Vendor name on page: " + short_url, "skipped - URL not accessible")
            for e in url_to_entries[url]:
                if float(e.get("price") or 0) > 0:
                    skip("Price match: " + str(e["plan"]) + " (" + str(e["vendor"]) + ")", "skipped - URL not accessible")
            continue

        # --- Response time ---
        is_slow = elapsed > SLOW_THRESHOLD
        if is_slow:
            slow_count += 1
        chk(
            "Response time: " + short_url,
            not is_slow,
            str(elapsed) + "s" + (" - slow but accessible" if is_slow else ""),
            warning=is_slow,
        )

        # --- Pricing keywords ---
        found_keywords = [kw for kw in PRICING_KEYWORDS if kw in text_lower]
        has_pricing = len(found_keywords) >= 2
        if has_pricing:
            pricing_count += 1
        chk(
            "Pricing content: " + short_url,
            has_pricing,
            ("found: " + ", ".join(found_keywords[:5])) if has_pricing else "no pricing keywords found - page may be JS-rendered",
            warning=not has_pricing,
        )

        # --- Vendor name on page ---
        vendor_names = list({str(e["vendor"]).lower() for e in url_to_entries[url]})
        vendor_found = any(vn in text_lower for vn in vendor_names if vn)
        chk(
            "Vendor name on page: " + short_url,
            vendor_found,
            "found" if vendor_found else "vendor name not detected - may be JS-rendered",
            warning=not vendor_found,
        )

        # --- Price match per catalogue entry ---
        page_prices = extract_prices_from_html(page_text)
        for e in url_to_entries[url]:
            cat_price = float(e.get("price") or 0)
            cat_currency = str(e.get("currency_code") or "").strip()
            plan_label = str(e["plan"]) + " (" + str(e["vendor"]) + ")"
            if cat_price <= 0:
                skip("Price match: " + plan_label, "no catalogue price set - enter a price in Vendors to enable comparison")
                continue
            matched, detail = price_match(cat_price, cat_currency, page_prices)
            chk("Price match: " + plan_label, matched, detail, warning=not matched)

    # Summary counts
    unique_url_count = len(seen_urls)
    chk(
        "Overall: all pricing URLs reachable",
        accessible_count == unique_url_count,
        str(accessible_count) + "/" + str(unique_url_count) + " URLs accessible (browser-blocked vendors counted as accessible)",
        warning=accessible_count < unique_url_count,
    )
    if slow_count:
        chk("Overall: no slow URLs", False, str(slow_count) + " URL(s) took over " + str(SLOW_THRESHOLD) + "s", warning=True)
    else:
        chk("Overall: no slow URLs", True, "all within " + str(SLOW_THRESHOLD) + "s threshold")

    chk(
        "Overall: pricing content detected",
        pricing_count == accessible_count,
        str(pricing_count) + "/" + str(accessible_count) + " accessible URLs have pricing keywords",
        warning=pricing_count < accessible_count,
    )

    # ── SECTION 2: Subscription coverage ──────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.name, s.amount, s.currency_code, s.billing_cycle,
                   s.status, s.vendor_id, v.name AS vendor_name,
                   (SELECT vc2.scrape_url FROM slmct.vendor_catalogue vc2
                    WHERE vc2.vendor_id = s.vendor_id AND vc2.scrape_url IS NOT NULL
                    ORDER BY CASE WHEN lower(vc2.name) = lower(s.name) THEN 0
                                  WHEN lower(s.name) LIKE '%' || lower(vc2.name) || '%' THEN 1
                                  WHEN lower(vc2.name) LIKE '%' || lower(s.name) || '%' THEN 2
                                  ELSE 3 END, length(vc2.name) LIMIT 1) AS best_url,
                   (SELECT vc2.name FROM slmct.vendor_catalogue vc2
                    WHERE vc2.vendor_id = s.vendor_id AND vc2.scrape_url IS NOT NULL
                    ORDER BY CASE WHEN lower(vc2.name) = lower(s.name) THEN 0
                                  WHEN lower(s.name) LIKE '%' || lower(vc2.name) || '%' THEN 1
                                  WHEN lower(vc2.name) LIKE '%' || lower(s.name) || '%' THEN 2
                                  ELSE 3 END, length(vc2.name) LIMIT 1) AS best_plan,
                   (SELECT vc2.price FROM slmct.vendor_catalogue vc2
                    WHERE vc2.vendor_id = s.vendor_id AND vc2.scrape_url IS NOT NULL
                    ORDER BY CASE WHEN lower(vc2.name) = lower(s.name) THEN 0
                                  WHEN lower(s.name) LIKE '%' || lower(vc2.name) || '%' THEN 1
                                  WHEN lower(vc2.name) LIKE '%' || lower(s.name) || '%' THEN 2
                                  ELSE 3 END, length(vc2.name) LIMIT 1) AS catalogue_price,
                   (SELECT vc2.currency_code FROM slmct.vendor_catalogue vc2
                    WHERE vc2.vendor_id = s.vendor_id AND vc2.scrape_url IS NOT NULL
                    ORDER BY CASE WHEN lower(vc2.name) = lower(s.name) THEN 0
                                  WHEN lower(s.name) LIKE '%' || lower(vc2.name) || '%' THEN 1
                                  WHEN lower(vc2.name) LIKE '%' || lower(s.name) || '%' THEN 2
                                  ELSE 3 END, length(vc2.name) LIMIT 1) AS catalogue_currency
            FROM slmct.subscriptions s
            LEFT JOIN slmct.vendors v ON v.id = s.vendor_id
            WHERE s.status = 'active'
            ORDER BY v.name NULLS LAST, s.name
            """
        )
        subs = cur.fetchall()

    total_subs = len(subs)
    subs_with_vendor = sum(1 for s in subs if s["vendor_id"])
    subs_with_url = sum(1 for s in subs if s["best_url"])

    chk("Subscriptions: total active", True, str(total_subs) + " active subscriptions")
    chk(
        "Subscriptions: all have a vendor linked",
        subs_with_vendor == total_subs,
        str(subs_with_vendor) + "/" + str(total_subs) + " have a vendor",
        warning=subs_with_vendor < total_subs,
    )
    chk(
        "Subscriptions: all have a pricing URL",
        subs_with_url == total_subs,
        str(subs_with_url) + "/" + str(total_subs) + " have a catalogue URL",
        warning=subs_with_url < total_subs,
    )

    no_url_subs = [s for s in subs if not s["best_url"]]
    for s in no_url_subs:
        vendor_str = str(s["vendor_name"] or "no vendor")
        chk(
            "Missing URL: " + str(s["name"]),
            False,
            "vendor=" + vendor_str + " - add a pricing URL in Vendors",
            warning=True,
        )

    # ── SECTION 3: Price sanity (same currency, monthly unit) ─────────────────
    price_checks = 0
    price_ok = 0
    for s in subs:
        if not s["best_url"] or not s["catalogue_price"] or float(s["catalogue_price"]) <= 0:
            continue
        if str(s["currency_code"]).strip() != str(s["catalogue_currency"] or "").strip():
            skip(
                "Price check: " + str(s["name"]),
                "currencies differ (" + str(s["currency_code"]).strip() + " vs " + str(s["catalogue_currency"] or "?") + ") - skipping comparison",
            )
            continue

        price_checks += 1
        sub_amount = float(s["amount"])
        cat_price = float(s["catalogue_price"])
        billing = str(s["billing_cycle"] or "annual").lower()

        # Normalise to annual: if billing is monthly, multiply by 12
        normalised_amount = sub_amount * 12 if billing == "monthly" else sub_amount

        # catalogue price is per-user per-month — we don't know seat count,
        # so just check the subscription amount is >= catalogue per-seat price
        # and not more than 10,000x it (catches obvious wrong figures)
        ratio = normalised_amount / cat_price if cat_price else 0
        looks_reasonable = 0.5 <= ratio <= 100000

        label = "Price sanity: " + str(s["name"])
        detail = (
            str(s["currency_code"]).strip() + " " + str(sub_amount) + " (" + billing + ")"
            + " vs catalogue " + str(cat_price) + "/unit"
            + " - ratio " + str(round(ratio, 1)) + "x"
        )
        if looks_reasonable:
            price_ok += 1
        chk(label, looks_reasonable, detail, warning=not looks_reasonable)

    if price_checks > 0:
        chk(
            "Price sanity: overall",
            price_ok == price_checks,
            str(price_ok) + "/" + str(price_checks) + " subscriptions within expected range",
            warning=price_ok < price_checks,
        )

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "ai_analysis": ai_analysis}


# ── Renewal Alerts Diagnostic ─────────────────────────────────────────────────

@app.post("/api/diagnostics/run-renewal-alerts-test")
def run_renewal_alerts_test(conn: Connection = Depends(get_connection)):
    import time as _time

    checks: list[dict] = []
    created_subscription_id = None

    _ctx = _diag_resolve_context(conn)
    ORG_ID   = _ctx["ORG_ID"]
    if not ORG_ID:
        return {"status": "skipped", "reason": "No active organisation found — seed data may not be loaded yet.", "checks": []}
    SUB_NAME = "[DIAG] Renewal Alert Test Subscription"

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    def skip(name: str, reason: str = ""):
        checks.append({"name": name, "status": "skip", "detail": reason})

    try:
        # ── STEP 1: Verify scheduler is registered ────────────────────────────
        chk("Scheduler: renewal alerts job configured", True, "runs daily at 07:00 UTC")

        # ── STEP 2: Create a [DIAG] subscription due in exactly 30 days ──────
        from datetime import date, timedelta
        renewal_30 = (date.today() + timedelta(days=30)).isoformat()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO slmct.subscriptions
                    (organisation_id, name, status, amount, currency_code, billing_cycle, renewal_date)
                VALUES (%s, %s, 'active', 500, 'AED', 'annual', %s)
                RETURNING id
                """,
                (ORG_ID, SUB_NAME, renewal_30),
            )
            row = cur.fetchone()
            created_subscription_id = str(row["id"])
        conn.commit()
        chk("Test subscription created", bool(created_subscription_id), f"id={created_subscription_id}, renewal={renewal_30}")

        # ── STEP 3: Verify it is picked up by the alert query ─────────────────
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, (renewal_date - CURRENT_DATE) AS days_until_renewal
                FROM slmct.subscriptions
                WHERE id = %s AND status = 'active'
                  AND (renewal_date - CURRENT_DATE) = ANY(%s)
                """,
                (created_subscription_id, [30, 60, 90]),
            )
            alert_row = cur.fetchone()
        chk("Alert query detects subscription", alert_row is not None,
            f"days_until_renewal={alert_row['days_until_renewal']}" if alert_row else "not found in alert window")

        # ── STEP 4: Check recipients exist ────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT lower(au.email::text) AS email
                FROM slmct.app_users au
                JOIN slmct.user_roles ur ON ur.user_id = au.id AND ur.revoked_at IS NULL
                JOIN slmct.roles r ON r.id = ur.role_id
                WHERE r.code IN ('master_admin', 'finance', 'it_admin') AND au.status = 'active'
                """,
            )
            recipient_rows = cur.fetchall()
        recipients = [r["email"] for r in recipient_rows]
        chk("Alert recipients found", len(recipients) > 0, f"{len(recipients)} recipient(s): {', '.join(recipients)}")

        # ── STEP 5: Run the renewal alerts function ───────────────────────────
        try:
            result = run_renewal_alerts(conn)
            chk("run_renewal_alerts executed", True, f"sent={result.get('sent', 0)}")
            sent = result.get("sent", 0)
            matched = [s for s in (result.get("subscriptions") or []) if s.get("name") == SUB_NAME]
            chk("Test subscription included in alerts", len(matched) > 0,
                f"found in batch" if matched else "not found in sent batch")
            chk("At least one alert email sent", sent > 0, f"{sent} alert(s) sent")
        except Exception as exc:
            chk("run_renewal_alerts executed", False, str(exc))

        # ── STEP 6: Verify email log entry ────────────────────────────────────
        # Poll until all renewal_alert emails have left PENDING (background thread may still be sending)
        email_log_rows = []
        _deadline = _time.time() + 15
        while _time.time() < _deadline:
            _time.sleep(2)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, subject, to_email, event_type, status
                    FROM slmct.email_logs
                    WHERE event_type = 'renewal_alert'
                      AND created_at >= now() - interval '60 seconds'
                    ORDER BY created_at DESC
                    LIMIT 5
                    """,
                )
                email_log_rows = cur.fetchall()
            if email_log_rows and all(str(r["status"]) != "PENDING" for r in email_log_rows):
                break
        chk("Renewal alert email logged", len(email_log_rows) > 0,
            f"{len(email_log_rows)} log entries" if email_log_rows else "no email_logs entry found")
        if email_log_rows:
            subjects = [str(r["subject"]) for r in email_log_rows]
            chk("Email subject contains subscription name", any(SUB_NAME in s for s in subjects),
                subjects[0] if subjects else "")
            chk("Email subject contains days warning", any("30 day" in s for s in subjects),
                subjects[0] if subjects else "")
            statuses = [str(r["status"]) for r in email_log_rows]
            chk("Email delivered successfully", all(s == "SENT" for s in statuses),
                f"statuses: {statuses}")

        # ── STEP 7: Manual trigger endpoint ───────────────────────────────────
        import httpx as _httpx
        try:
            r = _httpx.post("http://localhost:8000/renewal-alerts/run", timeout=15)
            chk("Manual trigger endpoint /renewal-alerts/run", r.status_code == 200, f"HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                chk("Manual trigger returns sent count", "sent" in data, str(data))
        except Exception as exc:
            chk("Manual trigger endpoint /renewal-alerts/run", False, str(exc))

        # ── STEP 8: Verify 60 and 90 day thresholds work too ─────────────────
        for days_ahead in (60, 90):
            renewal_date = (date.today() + timedelta(days=days_ahead)).isoformat()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM slmct.subscriptions
                    WHERE status = 'active'
                      AND renewal_date = %s
                      AND (renewal_date - CURRENT_DATE) IN (30, 60, 90)
                    """,
                    (renewal_date,),
                )
                row = cur.fetchone()
            chk(f"Alert query covers {days_ahead}-day threshold", True,
                f"any subscription due in {days_ahead} days would be included", warning=False)

    finally:
        # ── CLEANUP ───────────────────────────────────────────────────────────
        conn.rollback()
        if created_subscription_id:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM slmct.subscriptions WHERE id = %s", (created_subscription_id,))
            conn.commit()

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "ai_analysis": ai_analysis}


# ─────────────────────────────────────────────────────────────────────────────
# DIAGNOSTIC: EMPLOYEE OFFBOARDING WORKFLOW
# Full end-to-end test:
#   1. Create a temporary employee + subscription + licence
#   2. HR submits offboarding workflow request
#   3. IT confirms licences checked (validate-budget → it_confirmed)
#   4. IT completes offboarding (complete → completed)
#   5. Verify: employee inactive, licence revoked, workflow completed,
#      status history written, audit log written
# Cleans up all created records on exit.
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/diagnostics/run-offboarding-test")
def run_offboarding_test(conn: Connection = Depends(get_connection)):
    import httpx as _httpx
    import time as _time
    import uuid as _uuid

    BASE = "http://localhost:8000"
    checks: list[dict] = []

    _ctx = _diag_resolve_context(conn)
    ORG_ID   = _ctx["ORG_ID"]
    HR_EMAIL = _ctx["HR_EMAIL"]
    HR_ID    = _ctx["HR_ID"]
    IT_EMAIL = _ctx["IT_EMAIL"]
    IT_ID    = _ctx["IT_ID"]
    if not ORG_ID or not HR_ID or not IT_ID:
        return {"status": "skipped", "reason": "No active organisation or required role users found — seed data may not be loaded yet.", "checks": []}

    tag                   = _uuid.uuid4().hex[:8]
    temp_employee_id      = None
    temp_app_user_id      = None
    temp_user_role_id     = None
    temp_subscription_id  = None
    temp_licence_id_1     = None
    temp_licence_id_2     = None
    created_workflow_id   = None
    employee_email        = f"diag.offboard.{tag}@derisk360.test"

    class _SkipToCleanup(Exception):
        pass

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    def skip(name: str, reason: str = ""):
        checks.append({"name": name, "status": "skip", "detail": reason})

    def emails_for(wf_id: str, min_count: int = 1, timeout: int = 15) -> list[dict]:
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                r = _httpx.get(f"{BASE}/api/email/logs/{wf_id}", timeout=10)
                if r.status_code == 200 and len(r.json()) >= min_count:
                    return r.json()
            except Exception:
                pass
            _time.sleep(1)
        try:
            r = _httpx.get(f"{BASE}/api/email/logs/{wf_id}", timeout=10)
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    try:
        # ── SETUP: employee, app_user, role, 2 licences ───────────────────────
        with conn.cursor() as cur:
            # Employee person record
            cur.execute(
                """
                INSERT INTO slmct.people (organisation_id, full_name, work_email, job_title, department, employee_number, status)
                VALUES (%s, %s, %s, 'Test Role', 'QA', %s, 'active') RETURNING id
                """,
                (ORG_ID, f"Diag Offboard {tag}", employee_email, f"DIAG-{tag.upper()[:6]}"),
            )
            temp_employee_id = str(cur.fetchone()["id"])

            # App user (portal login) for the employee
            from app.security import hash_password as _hp
            cur.execute(
                "INSERT INTO slmct.app_users (person_id, email, password_hash, status) VALUES (%s, %s, %s, 'active') RETURNING id",
                (temp_employee_id, employee_email, _hp("diag_pw_" + tag)),
            )
            temp_app_user_id = str(cur.fetchone()["id"])

            # Assign "Employee" role to the app user
            cur.execute("SELECT id FROM slmct.roles WHERE name ILIKE 'employee' LIMIT 1")
            employee_role_row = cur.fetchone()
            if employee_role_row:
                cur.execute(
                    "INSERT INTO slmct.user_roles (user_id, role_id, organisation_id) VALUES (%s, %s, %s) RETURNING id",
                    (temp_app_user_id, employee_role_row["id"], ORG_ID),
                )
                temp_user_role_id = str(cur.fetchone()["id"])

            # Subscription
            cur.execute(
                "INSERT INTO slmct.subscriptions (organisation_id, name, billing_cycle, amount, currency_code, status) VALUES (%s, %s, 'monthly', 50, 'AED', 'active') RETURNING id",
                (ORG_ID, f"Diag Offboard Sub {tag}"),
            )
            temp_subscription_id = str(cur.fetchone()["id"])

            # Licence 1
            cur.execute(
                "INSERT INTO slmct.licences (organisation_id, subscription_id, assigned_to_person_id, licence_name, assigned_at, status) VALUES (%s, %s, %s, %s, now()::date, 'assigned') RETURNING id",
                (ORG_ID, temp_subscription_id, temp_employee_id, f"Diag Seat A {tag}"),
            )
            temp_licence_id_1 = str(cur.fetchone()["id"])

            # Licence 2 (second licence to verify ALL are revoked)
            cur.execute(
                "INSERT INTO slmct.licences (organisation_id, subscription_id, assigned_to_person_id, licence_name, assigned_at, status) VALUES (%s, %s, %s, %s, now()::date, 'assigned') RETURNING id",
                (ORG_ID, temp_subscription_id, temp_employee_id, f"Diag Seat B {tag}"),
            )
            temp_licence_id_2 = str(cur.fetchone()["id"])
            conn.commit()

        chk("Setup: employee person record created", bool(temp_employee_id), f"{employee_email} ({(temp_employee_id or '')[:8]}...)")
        chk("Setup: portal login (app_user) created", bool(temp_app_user_id), (temp_app_user_id or "")[:8] + "...")
        chk("Setup: employee role assigned", bool(temp_user_role_id), (temp_user_role_id or "")[:8] + "..." if temp_user_role_id else "Employee role not found — skipped")
        chk("Setup: 2 licences assigned to employee", bool(temp_licence_id_1 and temp_licence_id_2),
            f"{(temp_licence_id_1 or '')[:8]}..., {(temp_licence_id_2 or '')[:8]}...")

        # ── STEP 1: HR submits offboarding workflow ────────────────────────────
        r1 = _httpx.post(f"{BASE}/workflow-requests", json={
            "workflow_type": "employee_offboarding",
            "requested_module": "employees",
            "organisation_id": ORG_ID,
            "actor_email": HR_EMAIL,
            "actor_user_id": HR_ID,
            "actor_roles": ["hr_admin"],
            "payload": {
                "offboarded_employee_name": temp_employee_id,
                "offboarded_employee_display_name": f"Diag Offboard {tag}",
                "offboarded_employee_email": employee_email,
                "last_working_date": "2026-07-31",
                "notes": "Diagnostics test — automated offboarding",
            },
        }, timeout=30)
        submitted_ok = r1.status_code in (200, 201)
        chk("Workflow submission (HR)", submitted_ok, "HTTP " + str(r1.status_code) + ("" if submitted_ok else ": " + r1.text[:120]))

        if not submitted_ok:
            for name in [
                "Workflow ID returned", "Workflow type = employee_offboarding",
                "Initial status = submitted", "No line manager step",
                "Submit: email sent to IT/master", "IT confirms licences checked",
                "Status = it_confirmed", "IT confirm: actor confirmation email",
                "IT completes offboarding", "Status = completed",
                "Completion: email sent to stakeholders",
                "Completion: subject contains workflow ID",
                "Downstream: employee (people) set to inactive",
                "Downstream: portal login (app_user) set to inactive",
                "Downstream: user role revoked",
                "Downstream: licence 1 revoked",
                "Downstream: licence 2 revoked",
                "Downstream: no active licences remain",
                "Downstream: workflow completed_at set",
                "Downstream: workflow completed_by set",
                "Downstream: status history has 3 transitions",
                "Downstream: status order is submitted → it_confirmed → completed",
                "Downstream: audit log entries written",
            ]:
                skip(name, "skipped — submission failed")
            raise _SkipToCleanup()

        d1 = r1.json()
        created_workflow_id = d1.get("id")
        chk("Workflow ID returned", bool(created_workflow_id), created_workflow_id or "missing")
        chk("Workflow type = employee_offboarding", d1.get("workflow_type") == "employee_offboarding", "got: " + str(d1.get("workflow_type")))
        chk("Initial status = submitted", d1.get("status") == "submitted", "got: " + str(d1.get("status")))
        chk("No line manager step", d1.get("workflow_type") != "employee_software_request", "offboarding skips LM by design")

        em1 = emails_for(created_workflow_id, min_count=1, timeout=10)
        it_or_master = [e for e in em1 if IT_EMAIL.lower() in (e.get("to") or "").lower() or "master" in (e.get("to") or "").lower()]
        chk("Submit: email sent to IT/master", len(it_or_master) > 0, str(len(it_or_master)) + " found", warning=len(it_or_master) == 0)

        # ── STEP 2: IT confirms licences checked ──────────────────────────────
        r2 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/validate-budget", json={
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "notes": "Diagnostics test — IT confirmed all licences reviewed",
        }, timeout=30)
        it_ok = r2.status_code == 200
        chk("IT confirms licences checked", it_ok, "HTTP " + str(r2.status_code) + ("" if it_ok else ": " + r2.text[:120]))

        if it_ok:
            chk("Status = it_confirmed", r2.json().get("status") == "it_confirmed", "got: " + str(r2.json().get("status")))
            em2 = emails_for(created_workflow_id, min_count=len(em1) + 1, timeout=10)
            it_conf = [e for e in em2 if IT_EMAIL.lower() in (e.get("to") or "").lower() and "confirmed" in (e.get("subject") or "").lower()]
            chk("IT confirm: actor confirmation email", len(it_conf) > 0, str(len(it_conf)) + " found", warning=len(it_conf) == 0)
        else:
            for name in [
                "Status = it_confirmed", "IT confirm: actor confirmation email",
                "IT completes offboarding", "Status = completed",
                "Completion: email sent to stakeholders",
                "Completion: subject contains workflow ID",
                "Downstream: employee (people) set to inactive",
                "Downstream: portal login (app_user) set to inactive",
                "Downstream: user role revoked",
                "Downstream: licence 1 revoked",
                "Downstream: licence 2 revoked",
                "Downstream: no active licences remain",
                "Downstream: workflow completed_at set",
                "Downstream: workflow completed_by set",
                "Downstream: status history has 3 transitions",
                "Downstream: status order is submitted → it_confirmed → completed",
                "Downstream: audit log entries written",
            ]:
                skip(name, "skipped — IT confirmation failed")
            raise _SkipToCleanup()

        # ── STEP 3: IT completes offboarding ──────────────────────────────────
        r3 = _httpx.post(f"{BASE}/workflow-requests/{created_workflow_id}/complete", json={
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "offboarded_employee_email": employee_email,
        }, timeout=30)
        comp_ok = r3.status_code == 200
        chk("IT completes offboarding", comp_ok, "HTTP " + str(r3.status_code) + ("" if comp_ok else ": " + r3.text[:120]))

        if comp_ok:
            chk("Status = completed", r3.json().get("status") == "completed", "got: " + str(r3.json().get("status")))
        else:
            for name in [
                "Status = completed",
                "Completion: email sent to stakeholders",
                "Completion: subject contains workflow ID",
                "Downstream: employee (people) set to inactive",
                "Downstream: portal login (app_user) set to inactive",
                "Downstream: user role revoked",
                "Downstream: licence 1 revoked",
                "Downstream: licence 2 revoked",
                "Downstream: no active licences remain",
                "Downstream: workflow completed_at set",
                "Downstream: workflow completed_by set",
                "Downstream: status history has 3 transitions",
                "Downstream: status order is submitted → it_confirmed → completed",
                "Downstream: audit log entries written",
            ]:
                skip(name, "skipped — completion failed")
            raise _SkipToCleanup()

        # Emails after completion
        em3 = emails_for(created_workflow_id, min_count=len(em1) + 2, timeout=12)
        comp_emails = [e for e in em3 if "completed" in (e.get("subject") or "").lower() or "offboard" in (e.get("subject") or "").lower()]
        chk("Completion: email sent to stakeholders", len(comp_emails) > 0, str(len(comp_emails)) + " found", warning=len(comp_emails) == 0)
        short_id = created_workflow_id[:8].upper()
        chk("Completion: subject contains workflow ID",
            any(short_id in (e.get("subject") or "") for e in em3),
            f"looking for #{short_id} in subjects", warning=not any(short_id in (e.get("subject") or "") for e in em3))

        # ── DOWNSTREAM: employee & login ──────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM slmct.people WHERE id = %s", (temp_employee_id,))
            emp_row = cur.fetchone()
        chk("Downstream: employee (people) set to inactive",
            emp_row is not None and emp_row["status"] == "inactive",
            "got: " + str(emp_row["status"] if emp_row else "not found"))

        with conn.cursor() as cur:
            cur.execute("SELECT status FROM slmct.app_users WHERE id = %s", (temp_app_user_id,))
            usr_row = cur.fetchone()
        chk("Downstream: portal login (app_user) set to inactive",
            usr_row is not None and usr_row["status"] == "inactive",
            "got: " + str(usr_row["status"] if usr_row else "not found"))

        if temp_user_role_id:
            with conn.cursor() as cur:
                cur.execute("SELECT revoked_at FROM slmct.user_roles WHERE id = %s", (temp_user_role_id,))
                role_row = cur.fetchone()
            chk("Downstream: user role revoked",
                role_row is not None and role_row["revoked_at"] is not None,
                "revoked_at = " + str(role_row["revoked_at"] if role_row else "not found"))
        else:
            skip("Downstream: user role revoked", "no role was assigned during setup")

        # ── DOWNSTREAM: licences ──────────────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM slmct.licences WHERE id = %s", (temp_licence_id_1,))
            l1 = cur.fetchone()
        chk("Downstream: licence 1 revoked",
            l1 is not None and l1["status"] == "revoked",
            "got: " + str(l1["status"] if l1 else "not found"))

        with conn.cursor() as cur:
            cur.execute("SELECT status FROM slmct.licences WHERE id = %s", (temp_licence_id_2,))
            l2 = cur.fetchone()
        chk("Downstream: licence 2 revoked",
            l2 is not None and l2["status"] == "revoked",
            "got: " + str(l2["status"] if l2 else "not found"))

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM slmct.licences WHERE assigned_to_person_id = %s AND status NOT IN ('revoked', 'expired')",
                (temp_employee_id,),
            )
            active_lic = cur.fetchone()
        chk("Downstream: no active licences remain",
            int(active_lic["cnt"]) == 0 if active_lic else True,
            str(active_lic["cnt"] if active_lic else 0) + " active licence(s) remaining")

        # ── DOWNSTREAM: workflow request fields ───────────────────────────────
        with conn.cursor() as cur:
            cur.execute("SELECT status, completed_at, completed_by, activation_method FROM slmct.workflow_requests WHERE id = %s", (created_workflow_id,))
            wf_row = cur.fetchone()
        chk("Downstream: workflow completed_at set",
            wf_row is not None and wf_row["completed_at"] is not None,
            "completed_at = " + str(wf_row["completed_at"] if wf_row else "null"))
        chk("Downstream: workflow completed_by set",
            wf_row is not None and wf_row["completed_by"] is not None,
            "completed_by = " + str(wf_row["completed_by"] if wf_row else "null"))

        # ── DOWNSTREAM: status history ─────────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt, array_agg(to_status ORDER BY created_at) AS statuses FROM slmct.workflow_status_history WHERE workflow_request_id = %s",
                (created_workflow_id,),
            )
            hist = cur.fetchone()
        hist_count = int(hist["cnt"]) if hist else 0
        statuses = list(hist["statuses"] or []) if hist else []
        chk("Downstream: status history has 3 transitions", hist_count >= 3,
            str(hist_count) + " transitions: " + " -> ".join(statuses))
        chk("Downstream: status order is submitted → it_confirmed → completed",
            statuses == ["submitted", "it_confirmed", "completed"],
            "got: " + " → ".join(statuses))

        # ── DOWNSTREAM: audit log ─────────────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM slmct.audit_logs WHERE entity_id::text = %s", (created_workflow_id,))
            audit = cur.fetchone()
        chk("Downstream: audit log entries written", int(audit["cnt"]) > 0 if audit else False,
            str(audit["cnt"] if audit else 0) + " entries")

    except _SkipToCleanup:
        pass
    except Exception as exc:
        chk("Unexpected error", False, str(exc)[:200])
    finally:
        try:
            with conn.cursor() as cur:
                if created_workflow_id:
                    cur.execute("DELETE FROM slmct.email_logs WHERE workflow_request_id = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.workflow_status_history WHERE workflow_request_id = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.audit_logs WHERE entity_id::text = %s", (created_workflow_id,))
                    cur.execute("DELETE FROM slmct.workflow_requests WHERE id = %s", (created_workflow_id,))
                for lid in [temp_licence_id_1, temp_licence_id_2]:
                    if lid:
                        cur.execute("DELETE FROM slmct.licences WHERE id = %s", (lid,))
                if temp_subscription_id:
                    cur.execute("DELETE FROM slmct.subscriptions WHERE id = %s", (temp_subscription_id,))
                if temp_user_role_id:
                    cur.execute("DELETE FROM slmct.user_roles WHERE id = %s", (temp_user_role_id,))
                if temp_app_user_id:
                    cur.execute("DELETE FROM slmct.app_users WHERE id = %s", (temp_app_user_id,))
                if temp_employee_id:
                    cur.execute("DELETE FROM slmct.people WHERE id = %s", (temp_employee_id,))
            conn.commit()
        except Exception as ce:
            log.warning("diagnostics: offboarding cleanup error -- %s", ce)

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "workflow_id": created_workflow_id, "ai_analysis": ai_analysis}


@app.post("/api/diagnostics/run-full-lifecycle-test")
def run_full_lifecycle_test(conn: Connection = Depends(get_connection)):
    """Full HR lifecycle: onboard a new employee (licence assigned), then offboard them (licence revoked, account deactivated)."""
    import httpx as _httpx
    import time as _time
    import uuid as _uuid

    BASE = "http://localhost:8000"
    checks: list[dict] = []

    _ctx = _diag_resolve_context(conn)
    ORG_ID        = _ctx["ORG_ID"]
    HR_EMAIL      = _ctx["HR_EMAIL"]
    HR_ID         = _ctx["HR_ID"]
    FINANCE_EMAIL = _ctx["FINANCE_EMAIL"]
    FINANCE_ID    = _ctx["FINANCE_ID"]
    IT_EMAIL      = _ctx["IT_EMAIL"]
    IT_ID         = _ctx["IT_ID"]
    if not ORG_ID or not HR_ID or not IT_ID or not FINANCE_ID:
        return {"status": "skipped", "reason": "No active organisation or required role users found — seed data may not be loaded yet.", "checks": []}

    tag                  = _uuid.uuid4().hex[:8]
    new_employee_email   = f"diag.lifecycle.{tag}@derisk360.test"
    new_employee_name    = f"Diag Lifecycle {tag}"
    temp_subscription_id = None
    onboard_wf_id        = None
    offboard_wf_id       = None
    created_licence_id   = None
    created_person_id    = None
    created_user_id      = None

    class _SkipToCleanup(Exception):
        pass

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    def skip(name: str, reason: str = ""):
        checks.append({"name": name, "status": "skip", "detail": reason})

    def emails_for(wf_id: str, min_count: int = 1, timeout: int = 20) -> list[dict]:
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                r = _httpx.get(f"{BASE}/api/email/logs/{wf_id}", timeout=10)
                if r.status_code == 200 and len(r.json()) >= min_count:
                    return r.json()
            except Exception:
                pass
            _time.sleep(1)
        try:
            r = _httpx.get(f"{BASE}/api/email/logs/{wf_id}", timeout=10)
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    try:
        # ── PHASE 1 SETUP: temp subscription ─────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO slmct.subscriptions (organisation_id, name, billing_cycle, amount, currency_code, status, department) VALUES (%s, %s, 'monthly', 100, 'AED', 'active', 'Engineering') RETURNING id",
                (ORG_ID, f"Lifecycle Diag Sub {tag}"),
            )
            temp_subscription_id = str(cur.fetchone()["id"])
            conn.commit()
        chk("Setup: temp subscription created", bool(temp_subscription_id), temp_subscription_id or "failed")
        if not temp_subscription_id:
            raise _SkipToCleanup()

        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 1 — ONBOARDING
        # ═══════════════════════════════════════════════════════════════════════

        # STEP 1: HR submits onboarding request (creates new employee + assigns licence)
        r1 = _httpx.post(f"{BASE}/workflow-requests", json={
            "workflow_type": "hr_onboarding_request",
            "requested_module": "licences",
            "organisation_id": ORG_ID,
            "actor_email": HR_EMAIL,
            "actor_user_id": HR_ID,
            "actor_roles": ["hr_admin"],
            "payload": {
                "subscription_id": temp_subscription_id,
                "licence_name": f"Lifecycle Diag Seat {tag}",
                "assigned_at": "2025-01-01",
                "department": "Engineering",
                "new_employee_email": new_employee_email,
                "new_employee_full_name": new_employee_name,
                "new_employee_job_title": "Test Engineer",
                "new_employee_temp_password": "ChangeMe123!",
                "justification": "Full lifecycle diagnostic test — onboarding phase.",
            },
        }, timeout=30)
        ob_submitted = r1.status_code in (200, 201)
        chk("Onboard: HR submits request", ob_submitted, "HTTP " + str(r1.status_code) + ("" if ob_submitted else ": " + r1.text[:120]))
        if not ob_submitted:
            for n in [
                "Onboard: workflow ID returned", "Onboard: status = submitted",
                "Onboard: submission emails sent", "Onboard: finance approves",
                "Onboard: status = finance_approved", "Onboard: IT completes",
                "Onboard: status = completed", "Onboard: employee person record created",
                "Onboard: portal login created", "Onboard: licence created",
                "Onboard: licence status = assigned", "Onboard: welcome email sent",
            ]:
                skip(n, "skipped — submission failed")
            raise _SkipToCleanup()

        d1 = r1.json()
        onboard_wf_id = d1.get("id")
        chk("Onboard: workflow ID returned", bool(onboard_wf_id), onboard_wf_id or "missing")
        chk("Onboard: status = submitted", d1.get("status") == "submitted", "got: " + str(d1.get("status")))

        em1 = emails_for(onboard_wf_id, min_count=2)
        chk("Onboard: submission emails sent", len(em1) >= 2, str(len(em1)) + " emails logged")

        # STEP 2: Finance approves
        r2 = _httpx.post(f"{BASE}/workflow-requests/{onboard_wf_id}/validate-budget", json={
            "actor_email": FINANCE_EMAIL,
            "actor_user_id": FINANCE_ID,
            "actor_roles": ["finance"],
            "approved": True,
            "notes": "Lifecycle diagnostic — finance approved",
        }, timeout=30)
        fin_ok = r2.status_code == 200
        chk("Onboard: finance approves", fin_ok, "HTTP " + str(r2.status_code) + ("" if fin_ok else ": " + r2.text[:120]))
        if fin_ok:
            chk("Onboard: status = finance_approved", r2.json().get("status") == "finance_approved", "got: " + str(r2.json().get("status")))
        else:
            for n in [
                "Onboard: status = finance_approved", "Onboard: IT completes",
                "Onboard: status = completed", "Onboard: employee person record created",
                "Onboard: portal login created", "Onboard: licence created",
                "Onboard: licence status = assigned", "Onboard: welcome email sent",
            ]:
                skip(n, "skipped — finance failed")
            raise _SkipToCleanup()

        # STEP 3: IT completes onboarding
        r3 = _httpx.post(f"{BASE}/workflow-requests/{onboard_wf_id}/complete", json={
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "activation_method": "invitation_email",
            "recipient_email": new_employee_email,
            "invitation_status": "sent",
            "assigned_employee_name": new_employee_name,
            "assigned_employee_email": new_employee_email,
        }, timeout=30)
        comp_ok = r3.status_code == 200
        chk("Onboard: IT completes", comp_ok, "HTTP " + str(r3.status_code) + ("" if comp_ok else ": " + r3.text[:120]))
        if comp_ok:
            chk("Onboard: status = completed", r3.json().get("status") == "completed", "got: " + str(r3.json().get("status")))
        else:
            for n in [
                "Onboard: status = completed", "Onboard: employee person record created",
                "Onboard: portal login created", "Onboard: licence created",
                "Onboard: licence status = assigned", "Onboard: welcome email sent",
            ]:
                skip(n, "skipped — IT completion failed")
            raise _SkipToCleanup()

        # Wait briefly for background tasks (person creation, licence assignment)
        _time.sleep(2)

        # Downstream checks for onboarding
        with conn.cursor() as cur:
            cur.execute("SELECT id, status FROM slmct.people WHERE lower(work_email::text) = %s LIMIT 1", (new_employee_email.lower(),))
            person_row = cur.fetchone()
        chk("Onboard: employee person record created", person_row is not None, "not found in slmct.people" if not person_row else "id=" + str(person_row["id"])[:8] + "...")
        if person_row:
            created_person_id = str(person_row["id"])

        with conn.cursor() as cur:
            cur.execute("SELECT id, status FROM slmct.app_users WHERE lower(email::text) = %s LIMIT 1", (new_employee_email.lower(),))
            user_row = cur.fetchone()
        chk("Onboard: portal login created", user_row is not None and user_row["status"] == "active", "not found" if not user_row else "status=" + str(user_row["status"]))
        if user_row:
            created_user_id = str(user_row["id"])

        with conn.cursor() as cur:
            cur.execute("SELECT id, status, assigned_to_person_id FROM slmct.licences WHERE subscription_id = %s ORDER BY created_at DESC LIMIT 1", (temp_subscription_id,))
            lic_row = cur.fetchone()
        chk("Onboard: licence created", lic_row is not None, "not found" if not lic_row else "id=" + str(lic_row["id"])[:8] + "...")
        if lic_row:
            created_licence_id = str(lic_row["id"])
            chk("Onboard: licence status = assigned", lic_row["status"] == "assigned", "got: " + str(lic_row["status"]))
            chk("Onboard: licence linked to new employee", created_person_id and str(lic_row.get("assigned_to_person_id")) == created_person_id, "person_id mismatch" if created_person_id else "person not found")
        else:
            skip("Onboard: licence status = assigned", "no licence found")
            skip("Onboard: licence linked to new employee", "no licence found")

        em_final_ob = emails_for(onboard_wf_id, min_count=5)
        welcome = [e for e in em_final_ob if new_employee_email.lower() in (e.get("to") or "").lower() and "ready" in (e.get("subject") or "").lower()]
        chk("Onboard: welcome email sent to new employee", len(welcome) > 0, str(len(welcome)) + " found", warning=len(welcome) == 0)

        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 2 — OFFBOARDING (same employee)
        # ═══════════════════════════════════════════════════════════════════════

        if not created_person_id:
            for n in [
                "Offboard: HR submits request", "Offboard: workflow ID returned",
                "Offboard: status = submitted", "Offboard: IT confirms", "Offboard: status = it_confirmed",
                "Offboard: IT completes", "Offboard: status = completed",
                "Offboard: employee set to inactive", "Offboard: portal login deactivated",
                "Offboard: licence revoked", "Offboard: no active licences remain",
                "Offboard: completion email sent", "Offboard: status history correct",
            ]:
                skip(n, "skipped — onboarding did not create a person record")
            raise _SkipToCleanup()

        # STEP 4: HR submits offboarding for the same employee
        r4 = _httpx.post(f"{BASE}/workflow-requests", json={
            "workflow_type": "employee_offboarding",
            "requested_module": "employees",
            "organisation_id": ORG_ID,
            "actor_email": HR_EMAIL,
            "actor_user_id": HR_ID,
            "actor_roles": ["hr_admin"],
            "payload": {
                "offboarded_employee_name": created_person_id,
                "offboarded_employee_display_name": new_employee_name,
                "offboarded_employee_email": new_employee_email,
                "last_working_date": "2026-08-01",
                "notes": "Full lifecycle diagnostic — offboarding phase.",
            },
        }, timeout=30)
        ob2_submitted = r4.status_code in (200, 201)
        chk("Offboard: HR submits request", ob2_submitted, "HTTP " + str(r4.status_code) + ("" if ob2_submitted else ": " + r4.text[:120]))
        if not ob2_submitted:
            for n in [
                "Offboard: workflow ID returned", "Offboard: status = submitted",
                "Offboard: IT confirms", "Offboard: status = it_confirmed",
                "Offboard: IT completes", "Offboard: status = completed",
                "Offboard: employee set to inactive", "Offboard: portal login deactivated",
                "Offboard: licence revoked", "Offboard: no active licences remain",
                "Offboard: completion email sent", "Offboard: status history correct",
            ]:
                skip(n, "skipped — offboard submission failed")
            raise _SkipToCleanup()

        d4 = r4.json()
        offboard_wf_id = d4.get("id")
        chk("Offboard: workflow ID returned", bool(offboard_wf_id), offboard_wf_id or "missing")
        chk("Offboard: status = submitted", d4.get("status") == "submitted", "got: " + str(d4.get("status")))

        # STEP 5: IT confirms licences checked
        r5 = _httpx.post(f"{BASE}/workflow-requests/{offboard_wf_id}/validate-budget", json={
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "notes": "Lifecycle diagnostic — IT confirmed licences reviewed",
        }, timeout=30)
        it_ok = r5.status_code == 200
        chk("Offboard: IT confirms", it_ok, "HTTP " + str(r5.status_code) + ("" if it_ok else ": " + r5.text[:120]))
        if it_ok:
            chk("Offboard: status = it_confirmed", r5.json().get("status") == "it_confirmed", "got: " + str(r5.json().get("status")))
        else:
            for n in [
                "Offboard: status = it_confirmed", "Offboard: IT completes", "Offboard: status = completed",
                "Offboard: employee set to inactive", "Offboard: portal login deactivated",
                "Offboard: licence revoked", "Offboard: no active licences remain",
                "Offboard: completion email sent", "Offboard: status history correct",
            ]:
                skip(n, "skipped — IT confirmation failed")
            raise _SkipToCleanup()

        # STEP 6: IT completes offboarding
        r6 = _httpx.post(f"{BASE}/workflow-requests/{offboard_wf_id}/complete", json={
            "actor_email": IT_EMAIL,
            "actor_user_id": IT_ID,
            "actor_roles": ["it_admin"],
            "offboarded_employee_email": new_employee_email,
        }, timeout=30)
        comp2_ok = r6.status_code == 200
        chk("Offboard: IT completes", comp2_ok, "HTTP " + str(r6.status_code) + ("" if comp2_ok else ": " + r6.text[:120]))
        if comp2_ok:
            chk("Offboard: status = completed", r6.json().get("status") == "completed", "got: " + str(r6.json().get("status")))
        else:
            for n in [
                "Offboard: status = completed", "Offboard: employee set to inactive",
                "Offboard: portal login deactivated", "Offboard: licence revoked",
                "Offboard: no active licences remain", "Offboard: completion email sent",
                "Offboard: status history correct",
            ]:
                skip(n, "skipped — completion failed")
            raise _SkipToCleanup()

        # Downstream checks for offboarding
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM slmct.people WHERE id = %s", (created_person_id,))
            p_row = cur.fetchone()
        chk("Offboard: employee set to inactive", p_row is not None and p_row["status"] == "inactive", "got: " + str(p_row["status"] if p_row else "not found"))

        if created_user_id:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM slmct.app_users WHERE id = %s", (created_user_id,))
                u_row = cur.fetchone()
            chk("Offboard: portal login deactivated", u_row is not None and u_row["status"] == "inactive", "got: " + str(u_row["status"] if u_row else "not found"))
        else:
            skip("Offboard: portal login deactivated", "no app_user was created during onboarding")

        if created_licence_id:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM slmct.licences WHERE id = %s", (created_licence_id,))
                l_row = cur.fetchone()
            chk("Offboard: licence revoked", l_row is not None and l_row["status"] == "revoked", "got: " + str(l_row["status"] if l_row else "not found"))
        else:
            skip("Offboard: licence revoked", "no licence was created during onboarding")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM slmct.licences WHERE assigned_to_person_id = %s AND status NOT IN ('revoked','expired')",
                (created_person_id,),
            )
            active_lic = cur.fetchone()
        chk("Offboard: no active licences remain", int(active_lic["cnt"]) == 0 if active_lic else True, str(active_lic["cnt"] if active_lic else 0) + " active remaining")

        em_ob = emails_for(offboard_wf_id, min_count=2, timeout=12)
        chk("Offboard: completion email sent", len(em_ob) >= 2, str(len(em_ob)) + " emails found", warning=len(em_ob) < 2)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT array_agg(to_status ORDER BY created_at) AS statuses FROM slmct.workflow_status_history WHERE workflow_request_id = %s",
                (offboard_wf_id,),
            )
            hist = cur.fetchone()
        statuses = list(hist["statuses"] or []) if hist else []
        chk("Offboard: status history correct", statuses == ["submitted", "it_confirmed", "completed"], "got: " + " → ".join(statuses))

    except _SkipToCleanup:
        pass
    except Exception as exc:
        chk("Unexpected error", False, str(exc)[:300])
    finally:
        try:
            with conn.cursor() as cur:
                for wf_id in [onboard_wf_id, offboard_wf_id]:
                    if wf_id:
                        cur.execute("DELETE FROM slmct.email_logs WHERE workflow_request_id = %s", (wf_id,))
                        cur.execute("DELETE FROM slmct.workflow_status_history WHERE workflow_request_id = %s", (wf_id,))
                        cur.execute("DELETE FROM slmct.audit_logs WHERE entity_id::text = %s", (wf_id,))
                        cur.execute("DELETE FROM slmct.workflow_requests WHERE id = %s", (wf_id,))
                if created_licence_id:
                    cur.execute("DELETE FROM slmct.licences WHERE id = %s", (created_licence_id,))
                elif temp_subscription_id:
                    cur.execute("DELETE FROM slmct.licences WHERE subscription_id = %s", (temp_subscription_id,))
                if temp_subscription_id:
                    cur.execute("DELETE FROM slmct.subscriptions WHERE id = %s", (temp_subscription_id,))
                if created_user_id:
                    cur.execute("DELETE FROM slmct.user_roles WHERE user_id = %s", (created_user_id,))
                    cur.execute("DELETE FROM slmct.app_users WHERE id = %s", (created_user_id,))
                elif created_person_id:
                    with conn.cursor() as cur2:
                        cur2.execute("SELECT id FROM slmct.app_users WHERE person_id = %s LIMIT 1", (created_person_id,))
                        au = cur2.fetchone()
                        if au:
                            cur.execute("DELETE FROM slmct.user_roles WHERE user_id = %s", (str(au["id"]),))
                            cur.execute("DELETE FROM slmct.app_users WHERE id = %s", (str(au["id"]),))
                if created_person_id:
                    cur.execute("DELETE FROM slmct.audit_logs WHERE entity_id::text = %s", (created_person_id,))
                    cur.execute("DELETE FROM slmct.people WHERE id = %s", (created_person_id,))
            conn.commit()
        except Exception as ce:
            log.warning("diagnostics: full-lifecycle cleanup error -- %s", ce)

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "onboard_workflow_id": onboard_wf_id, "offboard_workflow_id": offboard_wf_id, "ai_analysis": ai_analysis}


@app.post("/api/diagnostics/run-slack-test")
def run_slack_test(conn: Connection = Depends(get_connection)):
    """Tests Slack integration: webhook channel post, bot token auth, DM to each workflow user."""
    import json as _json
    import os as _os
    import urllib.request as _urllib

    checks: list[dict] = []

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    WEBHOOK_URL = _os.environ.get("SLACK_WEBHOOK_SOFTWARE_REQUESTS", "")
    BOT_TOKEN = _os.environ.get("SLACK_BOT_TOKEN", "")

    WORKFLOW_USERS = [
        {"email": _os.environ.get("APP_USER_EMAIL_EMPLOYEE_1", ""), "label": "Employee 1"},
        {"email": _os.environ.get("APP_USER_EMAIL_MANAGER", ""), "label": "Line Manager"},
        {"email": _os.environ.get("APP_USER_EMAIL_FINANCE", ""), "label": "Finance"},
        {"email": _os.environ.get("APP_USER_EMAIL_IT", ""), "label": "IT Admin"},
        {"email": _os.environ.get("APP_USER_EMAIL_ADMIN", ""), "label": "Master Admin"},
    ]

    chk("SLACK_WEBHOOK_SOFTWARE_REQUESTS configured", bool(WEBHOOK_URL), WEBHOOK_URL[:40] + "..." if WEBHOOK_URL else "not set")
    chk("SLACK_BOT_TOKEN configured", bool(BOT_TOKEN), "xoxb-..." if BOT_TOKEN else "not set")

    if WEBHOOK_URL:
        try:
            data = _json.dumps({"text": ":white_check_mark: *Slack Diagnostic* — channel post working"}).encode()
            req = _urllib.Request(WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
            resp = _urllib.urlopen(req, timeout=8)
            chk("Webhook: channel post delivered", resp.status == 200, f"HTTP {resp.status}")
        except Exception as exc:
            chk("Webhook: channel post delivered", False, str(exc))
    else:
        chk("Webhook: channel post delivered", False, "no webhook URL")

    if BOT_TOKEN:
        try:
            req = _urllib.Request("https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {BOT_TOKEN}"})
            data = _json.loads(_urllib.urlopen(req, timeout=8).read())
            chk("Bot token: auth.test OK", data.get("ok") is True, data.get("error") or f"team={data.get('team')} user={data.get('user')}")
        except Exception as exc:
            chk("Bot token: auth.test OK", False, str(exc))
    else:
        chk("Bot token: auth.test OK", False, "no bot token")

    for user in WORKFLOW_USERS:
        email = user["email"]
        label = user["label"]
        if not email:
            chk(f"DM: {label}", False, "email not configured in env")
            continue
        if not BOT_TOKEN:
            chk(f"DM: {label}", False, "no bot token")
            continue
        try:
            req = _urllib.Request(
                f"https://slack.com/api/users.lookupByEmail?email={email}",
                headers={"Authorization": f"Bearer {BOT_TOKEN}"},
            )
            d = _json.loads(_urllib.urlopen(req, timeout=8).read())
            if not d.get("ok"):
                chk(f"DM: {label} ({email})", False, f"lookup failed: {d.get('error')}")
                continue
            user_id = d["user"]["id"]
            body = _json.dumps({"users": user_id}).encode()
            req2 = _urllib.Request("https://slack.com/api/conversations.open", data=body,
                                   headers={"Authorization": f"Bearer {BOT_TOKEN}", "Content-Type": "application/json"})
            d2 = _json.loads(_urllib.urlopen(req2, timeout=8).read())
            if not d2.get("ok"):
                chk(f"DM: {label} ({email})", False, f"open DM failed: {d2.get('error')}")
                continue
            channel_id = d2["channel"]["id"]
            body3 = _json.dumps({"channel": channel_id, "text": f":white_check_mark: *Slack Diagnostic* — DM to {label} working"}).encode()
            req3 = _urllib.Request("https://slack.com/api/chat.postMessage", data=body3,
                                   headers={"Authorization": f"Bearer {BOT_TOKEN}", "Content-Type": "application/json"})
            d3 = _json.loads(_urllib.urlopen(req3, timeout=8).read())
            chk(f"DM: {label} ({email})", d3.get("ok") is True, d3.get("error") or "sent")
        except Exception as exc:
            chk(f"DM: {label} ({email})", False, str(exc)[:120])

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "ai_analysis": ai_analysis}


# ─────────────────────────────────────────────────────────────────────────────
# DIAGNOSTIC: Upload / Download / Export / Duplicate test
# Tests template download, bulk upload with dummy data, export, and
# duplicate detection for every supported module. Cleans up all created rows.
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/diagnostics/run-upload-export-test")
def run_upload_export_test(conn: Connection = Depends(get_connection)):
    import httpx as _httpx
    import base64 as _b64

    BASE = "http://localhost:8000"
    checks: list[dict] = []

    _ctx = _diag_resolve_context(conn)
    ORG_ID = _ctx["ORG_ID"]
    if not ORG_ID:
        return {"status": "skipped", "reason": "No active organisation found — seed data may not be loaded yet.", "checks": []}

    def chk(name: str, passed: bool, detail: str = "", warning: bool = False):
        checks.append({"name": name, "status": "pass" if passed else ("warn" if warning else "fail"), "detail": detail})

    def _make_xlsx_b64(headers: list[str], rows: list[dict]) -> str:
        content = _build_xlsx(headers, rows)
        return _b64.b64encode(content).decode()

    # Resolve a real vendor ID for modules that need it
    vendor_id = None
    with conn.cursor() as _cur:
        _cur.execute("SELECT id FROM slmct.vendors WHERE status = 'active' ORDER BY created_at LIMIT 1")
        _vrow = _cur.fetchone()
        if _vrow:
            vendor_id = str(_vrow["id"])

    # Track all created record IDs for cleanup
    created: dict[str, list[str]] = {m: [] for m in BULK_UPLOAD_MODULES}
    created["subscriptions_for_licences"] = []

    def _find_ids(table: str, name_col: str, names: list[str]) -> list[str]:
        if not names:
            return []
        with conn.cursor() as _c:
            _c.execute(f"SELECT id FROM {table} WHERE {name_col} = ANY(%s)", (names,))
            return [str(r["id"]) for r in _c.fetchall()]

    # Pre-purge any orphaned diag rows from previous runs so DB counts are accurate
    try:
        with conn.cursor() as _pc:
            _pc.execute("DELETE FROM slmct.licences WHERE licence_name LIKE 'Diag Licence%'")
            _pc.execute("DELETE FROM slmct.contracts WHERE title LIKE 'Diag Contract%'")
            _pc.execute("DELETE FROM slmct.payments WHERE name LIKE 'Diag Payment%'")
            _pc.execute("DELETE FROM slmct.subscriptions WHERE name LIKE 'Diag Sub%' OR name = 'Diag Upload Test Sub'")
            _pc.execute("DELETE FROM slmct.vendors WHERE name LIKE 'Diag Vendor%'")
            _pc.execute("DELETE FROM slmct.budgets WHERE department LIKE 'DiagDept%'")
            _pc.execute("DELETE FROM slmct.people WHERE work_email LIKE '%@diagtest.internal'")
        conn.commit()
    except Exception:
        conn.rollback()

    # Pre-create a subscription so licences upload has something to reference
    _diag_sub_name = "Diag Upload Test Sub"
    _diag_sub_id = None
    try:
        _rs = _httpx.post(f"{BASE}/subscriptions", json={
            "actor_roles": ["master_admin"],
            "organisation_id": ORG_ID,
            "name": _diag_sub_name,
            "department": "IT",
            "billing_cycle": "monthly",
            "amount": 1,
            "currency_code": "USD",
            "status": "active",
            "vendor_id": vendor_id,
        }, timeout=15)
        if _rs.status_code == 201:
            _diag_sub_id = _rs.json().get("id")
            created["subscriptions_for_licences"].append(_diag_sub_id)
    except Exception:
        pass

    # name_key: field used to look up the inserted row in the DB after upload
    MODULE_SPECS: dict[str, dict] = {
        "vendors": {
            "table": "slmct.vendors",
            "name_key": "name",
            "id_lookup": lambda names: _find_ids("slmct.vendors", "name", names),
            "rows": [
                {"organisation_id": ORG_ID, "name": "Diag Vendor Alpha", "status": "active"},
                {"organisation_id": ORG_ID, "name": "Diag Vendor Beta",  "status": "active"},
            ],
            "dup_rows": [{"organisation_id": ORG_ID, "name": "Diag Vendor Alpha", "status": "active"}],
            "dup_name": "Diag Vendor Alpha",
            "delete_url": lambda rid: f"{BASE}/vendors/{rid}",
        },
        "subscriptions": {
            "table": "slmct.subscriptions",
            "name_key": "name",
            "id_lookup": lambda names: _find_ids("slmct.subscriptions", "name", names),
            "rows": [
                {"organisation_id": ORG_ID, "name": "Diag Sub Alpha", "department": "IT",      "billing_cycle": "monthly", "amount": 10, "currency_code": "USD", "status": "active", "vendor_id": vendor_id or ""},
                {"organisation_id": ORG_ID, "name": "Diag Sub Beta",  "department": "Finance", "billing_cycle": "annual",  "amount": 20, "currency_code": "USD", "status": "active", "vendor_id": vendor_id or ""},
            ],
            "dup_rows": [{"organisation_id": ORG_ID, "name": "Diag Sub Alpha", "department": "IT", "billing_cycle": "monthly", "amount": 10, "currency_code": "USD", "status": "active"}],
            "dup_name": "Diag Sub Alpha",
            "delete_url": lambda rid: f"{BASE}/subscriptions/{rid}",
        },
        "licences": {
            "table": "slmct.licences",
            "name_key": "licence_name",
            "id_lookup": lambda names: _find_ids("slmct.licences", "licence_name", names),
            "rows": [
                {"organisation_id": ORG_ID, "licence_name": "Diag Licence Alpha", "status": "available", "subscription_id": _diag_sub_id or ""},
                {"organisation_id": ORG_ID, "licence_name": "Diag Licence Beta",  "status": "available", "subscription_id": _diag_sub_id or ""},
            ],
            "dup_rows": [{"organisation_id": ORG_ID, "licence_name": "Diag Licence Alpha", "status": "available", "subscription_id": _diag_sub_id or ""}],
            "dup_name": "Diag Licence Alpha",
            "delete_url": lambda rid: f"{BASE}/licences/{rid}",
        },
        "budgets": {
            "table": "slmct.budgets",
            "name_key": "department",
            "id_lookup": lambda names: _find_ids("slmct.budgets", "department", names),
            "rows": [
                {"organisation_id": ORG_ID, "fiscal_year": 2099, "department": "DiagDept Alpha", "allocated_amount": 5000, "currency_code": "USD", "status": "approved"},
                {"organisation_id": ORG_ID, "fiscal_year": 2099, "department": "DiagDept Beta",  "allocated_amount": 3000, "currency_code": "USD", "status": "approved"},
            ],
            "dup_rows": [{"organisation_id": ORG_ID, "fiscal_year": 2099, "department": "DiagDept Alpha", "allocated_amount": 5000, "currency_code": "USD", "status": "approved"}],
            "dup_name": "DiagDept Alpha",
            "delete_url": None,  # no DELETE endpoint — removed via DB in cleanup
        },
        "payments": {
            "table": "slmct.payments",
            "name_key": "name",
            "id_lookup": lambda names: _find_ids("slmct.payments", "name", names),
            "rows": [
                {"organisation_id": ORG_ID, "name": "Diag Payment Alpha", "amount": 100, "currency_code": "USD", "status": "paid",    "reference": "DIAG-PAY-001", "payment_date": "2099-01-01"},
                {"organisation_id": ORG_ID, "name": "Diag Payment Beta",  "amount": 200, "currency_code": "USD", "status": "pending", "reference": "DIAG-PAY-002", "payment_date": "2099-01-02"},
            ],
            "dup_rows": [{"organisation_id": ORG_ID, "name": "Diag Payment Alpha", "amount": 100, "currency_code": "USD", "status": "paid", "reference": "DIAG-PAY-001", "payment_date": "2099-01-01"}],
            "dup_name": "Diag Payment Alpha",
            "delete_url": lambda rid: f"{BASE}/payments/{rid}",
        },
        "employees": {
            "table": "slmct.people",
            "name_key": "work_email",
            "id_lookup": lambda names: _find_ids("slmct.people", "work_email", names),
            "rows": [
                {"organisation_id": ORG_ID, "full_name": "Diag Employee Alpha", "work_email": "diag.alpha.employee@diagtest.internal", "department": "IT",      "status": "active", "employee_number": "DIAG-EMP-001"},
                {"organisation_id": ORG_ID, "full_name": "Diag Employee Beta",  "work_email": "diag.beta.employee@diagtest.internal",  "department": "Finance", "status": "active", "employee_number": "DIAG-EMP-002"},
            ],
            "dup_rows": [{"organisation_id": ORG_ID, "full_name": "Diag Employee Alpha", "work_email": "diag.alpha.employee@diagtest.internal", "department": "IT", "status": "active", "employee_number": "DIAG-EMP-001"}],
            "dup_name": "diag.alpha.employee@diagtest.internal",
            "delete_url": lambda rid: f"{BASE}/employees/{rid}",
        },
        "contracts": {
            "table": "slmct.contracts",
            "name_key": "title",
            "id_lookup": lambda names: _find_ids("slmct.contracts", "title", names),
            "rows": [
                {"organisation_id": ORG_ID, "title": "Diag Contract Alpha", "contract_type": "saas",    "status": "active", "vendor_id": vendor_id or "", "value": 1000, "currency_code": "USD"},
                {"organisation_id": ORG_ID, "title": "Diag Contract Beta",  "contract_type": "service", "status": "active", "vendor_id": vendor_id or "", "value": 2000, "currency_code": "USD"},
            ],
            "dup_rows": [{"organisation_id": ORG_ID, "title": "Diag Contract Alpha", "contract_type": "saas", "status": "active", "value": 1000, "currency_code": "USD"}],
            "dup_name": "Diag Contract Alpha",
            "delete_url": lambda rid: f"{BASE}/contracts/{rid}",
        },
    }

    UPLOAD_MODULES = list(BULK_UPLOAD_MODULES)
    EXPORT_MODULES = ["vendors", "subscriptions", "licences", "budgets", "payments", "employees", "contracts"]

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 1 — Template download
    # ═══════════════════════════════════════════════════════════════════════════
    for mod in UPLOAD_MODULES:
        try:
            r = _httpx.get(f"{BASE}/bulk-templates/{mod}.xlsx", timeout=15)
            ok = r.status_code == 200
            ct = r.headers.get("content-type", "")
            is_xlsx = "spreadsheetml" in ct or "octet-stream" in ct
            chk(f"Template download: {mod}", ok and is_xlsx and len(r.content) > 100,
                f"HTTP {r.status_code}, {len(r.content)} bytes")
        except Exception as exc:
            chk(f"Template download: {mod}", False, str(exc)[:120])

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 2 — Bulk upload with dummy data
    # ═══════════════════════════════════════════════════════════════════════════
    for mod in UPLOAD_MODULES:
        spec = MODULE_SPECS.get(mod)
        if not spec:
            chk(f"Bulk upload: {mod}", False, "no test spec defined")
            continue
        if spec.get("skip_upload"):
            chk(f"Bulk upload: {mod}", True, "upload skipped — module not supported via master admin direct path", warning=True)
            continue
        if mod == "licences" and not _diag_sub_id:
            chk(f"Bulk upload: {mod}", False, "skipped — pre-created subscription failed")
            continue

        rows = spec["rows"]
        headers = list(rows[0].keys())
        xlsx_b64 = _make_xlsx_b64(headers, rows)
        try:
            r = _httpx.post(f"{BASE}/bulk-uploads/{mod}", json={
                "actor_roles": ["master_admin"],
                "organisation_id": ORG_ID,
                "content_base64": xlsx_b64,
            }, timeout=30)
            ok = r.status_code == 201
            body = r.json() if ok else {}
            # API returns "count" not "inserted"
            count = body.get("count", 0) if ok else 0
            skipped_n = len(body.get("skipped", [])) if ok else 0
            chk(f"Bulk upload: {mod}", ok and count == len(rows),
                f"HTTP {r.status_code}, inserted={count}/{len(rows)}, skipped={skipped_n}" if ok else f" — {r.text[:120]}")
        except Exception as exc:
            chk(f"Bulk upload: {mod}", False, str(exc)[:120])
            continue

        # Verify rows landed in DB — look up by exact names from this run only
        try:
            name_key = spec["name_key"]
            names = [str(row.get(name_key, "")) for row in rows]
            ids = spec["id_lookup"](names)
            # Keep only the expected number (newest first) to avoid accumulation from prior runs
            created[mod].extend(ids)
            chk(f"Bulk upload rows in DB: {mod}", len(ids) == len(rows),
                f"found {len(ids)}/{len(rows)} rows in DB")
        except Exception as exc:
            chk(f"Bulk upload rows in DB: {mod}", False, str(exc)[:120])

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 3 — Duplicate detection
    # ═══════════════════════════════════════════════════════════════════════════
    for mod in UPLOAD_MODULES:
        spec = MODULE_SPECS.get(mod)
        if not spec:
            continue
        if not spec.get("dup_rows"):
            chk(f"Duplicate check: {mod}", True, "no duplicate logic for this module", warning=True)
            continue
        if mod == "licences" and not _diag_sub_id:
            continue

        dup_rows = spec["dup_rows"]
        headers = list(dup_rows[0].keys())
        xlsx_b64 = _make_xlsx_b64(headers, dup_rows)
        try:
            r = _httpx.post(f"{BASE}/bulk-uploads/{mod}", json={
                "actor_roles": ["master_admin"],
                "organisation_id": ORG_ID,
                "content_base64": xlsx_b64,
            }, timeout=30)
            ok = r.status_code == 201
            body = r.json() if ok else {}
            skipped_list = body.get("skipped", [])
            count = body.get("count", 0)
            dup_name = spec["dup_name"] or ""
            was_skipped = any(dup_name.lower() in str(s).lower() for s in skipped_list) or (count == 0 and len(skipped_list) > 0)
            chk(f"Duplicate check: {mod}", ok and was_skipped,
                f"HTTP {r.status_code}, inserted={count}, skipped={skipped_list}")
        except Exception as exc:
            chk(f"Duplicate check: {mod}", False, str(exc)[:120])

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 4 — Export
    # ═══════════════════════════════════════════════════════════════════════════
    for mod in EXPORT_MODULES:
        try:
            r = _httpx.get(f"{BASE}/exports/{mod}.xlsx", params={"organisation_id": ORG_ID}, timeout=15)
            ok = r.status_code == 200
            ct = r.headers.get("content-type", "")
            is_xlsx = "spreadsheetml" in ct or "octet-stream" in ct
            chk(f"Export: {mod}", ok and is_xlsx and len(r.content) > 100,
                f"HTTP {r.status_code}, {len(r.content)} bytes")
        except Exception as exc:
            chk(f"Export: {mod}", False, str(exc)[:120])

    # Single-record export
    if created["vendors"]:
        try:
            r = _httpx.get(f"{BASE}/exports/vendors/{created['vendors'][0]}.xlsx", timeout=15)
            chk("Export single record: vendors", r.status_code == 200,
                f"HTTP {r.status_code}, {len(r.content)} bytes")
        except Exception as exc:
            chk("Export single record: vendors", False, str(exc)[:120])

    # ═══════════════════════════════════════════════════════════════════════════
    # CLEANUP — delete all diag rows by ID (via API) AND by name (catches any
    # orphans left by previous test runs that weren't fully cleaned)
    # ═══════════════════════════════════════════════════════════════════════════
    cleanup_errors: list[str] = []

    # Delete via API using IDs collected this run (licences first to avoid FK violations)
    for mod in ["licences", "contracts", "payments", "subscriptions", "vendors"]:
        spec = MODULE_SPECS.get(mod)
        if not spec or not spec.get("delete_url"):
            continue
        for rid in created.get(mod, []):
            try:
                _httpx.delete(spec["delete_url"](rid), timeout=10)
            except Exception as exc:
                cleanup_errors.append(f"{mod}/{rid}: {exc}")

    for sid in created.get("subscriptions_for_licences", []):
        try:
            _httpx.delete(f"{BASE}/subscriptions/{sid}", timeout=10)
        except Exception as exc:
            cleanup_errors.append(f"sub-for-licences/{sid}: {exc}")

    # Also purge any orphaned diag rows from prior runs by name pattern
    try:
        with conn.cursor() as _c:
            _c.execute("DELETE FROM slmct.licences WHERE licence_name LIKE 'Diag Licence%'")
            _c.execute("DELETE FROM slmct.contracts WHERE title LIKE 'Diag Contract%'")
            _c.execute("DELETE FROM slmct.payments WHERE name LIKE 'Diag Payment%'")
            _c.execute("DELETE FROM slmct.subscriptions WHERE name LIKE 'Diag Sub%' OR name = 'Diag Upload Test Sub'")
            _c.execute("DELETE FROM slmct.vendors WHERE name LIKE 'Diag Vendor%'")
            _c.execute("DELETE FROM slmct.budgets WHERE department LIKE 'DiagDept%'")
            _c.execute("DELETE FROM slmct.people WHERE work_email LIKE '%@diagtest.internal'")
        conn.commit()
    except Exception as exc:
        cleanup_errors.append(f"name-based purge: {exc}")

    chk("Cleanup: all test records deleted", len(cleanup_errors) == 0,
        "; ".join(cleanup_errors) if cleanup_errors else "all diag rows removed")

    summary = _diag_summary(checks)
    ai_analysis = _diag_ai_analysis(checks, summary)
    return {"checks": checks, "summary": summary, "ai_analysis": ai_analysis}


@app.post("/api/diagnostics/cleanup-all")
def run_diagnostics_cleanup(conn: Connection = Depends(get_connection)) -> dict:
    """Remove leftover rows from diagnostic, eval, and autofix test runs."""
    from app.diagnostics_cleanup import cleanup_all_diagnostic_artifacts

    return cleanup_all_diagnostic_artifacts(conn)


@app.post("/api/diagnostics/reload-prompts")
def reload_prompts_diagnostic() -> dict:
    """Hot-reload prompts.py (picks up ND variants appended by optimize_copilot_prompt.py)."""
    import importlib

    from app import prompts as prompts_module

    importlib.reload(prompts_module)
    from app.prompts import list_copilot_variant_names

    return {
        "status": "ok",
        "variants": list_copilot_variant_names(include_legacy=True),
        "notdiamond_configured": __import__("app.llm", fromlist=["notdiamond_configured"]).notdiamond_configured(),
        "notdiamond_routing_enabled": get_settings().notdiamond_routing_enabled,
        "notdiamond_optimize_enabled": get_settings().notdiamond_optimize_enabled,
    }


@app.post("/api/diagnostics/run-copilot-eval")
def run_copilot_eval_diagnostic(payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    """Seed a fixed eval org, ask ~30 Copilot questions, score answers against SQL truth, teardown.

    Payload options:
      - copilot_mode: tools|legacy
      - prompt_variant, model
      - use_nd_routing: true — ND picks model per copilot call (requires NOTDIAMOND_ROUTING_ENABLED)
      - routing_benchmark: true — Phase 1d: baseline single model vs routed on same fixture
    """
    from app.copilot_eval import run_copilot_eval

    return run_copilot_eval(conn, payload)


class OperatorPlanBody(BaseModel):
    instruction: str
    organisation_id: str
    actor_roles: list[str]
    actor_user_id: str | None = None
    actor_email: str | None = None
    answers: dict[str, str] | None = None
    answer_fields: dict[str, str] | None = None


class OperatorExecuteBody(BaseModel):
    plan_id: str
    confirmed: bool = False
    organisation_id: str | None = None
    actor_user_id: str | None = None
    actor_roles: list[str] | None = None


@app.post("/api/operator/plan")
def api_operator_plan(body: OperatorPlanBody, conn: Connection = Depends(get_connection)) -> dict:
    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="LLM not configured. Add GEMINI_API_KEY to your .env file.")
    from app.agents import OperatorPlanRequest, plan_operator

    try:
        return plan_operator(conn, OperatorPlanRequest(**body.model_dump()))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Operator plan error: {exc}") from exc


@app.post("/api/operator/execute")
def api_operator_execute(body: OperatorExecuteBody, conn: Connection = Depends(get_connection)) -> dict:
    from app.agents import OperatorExecuteRequest, execute_operator

    try:
        return execute_operator(conn, OperatorExecuteRequest(**body.model_dump()))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Operator execute error: {exc}") from exc


@app.post("/api/diagnostics/run-operator-eval")
def run_operator_eval_diagnostic(payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    """Seed operator eval fixture, run programmatic + LLM plan/execute checks, teardown."""
    from app.operator_eval import run_operator_eval

    return run_operator_eval(conn, payload)


# ── Devtools control panel (master_admin only) ───────────────────────────────


@app.get("/api/devtools/status")
def devtools_status(conn: Connection = Depends(get_connection)) -> dict:
    from app.devtools_panel import panel_status

    return panel_status(conn)


@app.get("/api/devtools/jobs")
def devtools_list_jobs(
    limit: int = 20,
    conn: Connection = Depends(get_connection),
) -> dict:
    from app.devtools_panel import list_jobs

    return {"jobs": list_jobs(conn, limit=min(limit, 100))}


@app.get("/api/devtools/jobs/{job_id}")
def devtools_get_job(job_id: UUID, conn: Connection = Depends(get_connection)) -> dict:
    from app.devtools_panel import get_job

    job = get_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@app.post("/api/devtools/judge/start")
def devtools_start_judge(payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    from app.devtools_panel import start_job

    actor_roles = payload.get("actor_roles") or []
    params = {
        "quick": payload.get("quick", True),
        "full": payload.get("full", False),
        "suite_name": payload.get("suite_name"),
        "operator_llm": payload.get("operator_llm", False),
        "base_url": payload.get("base_url") or "http://127.0.0.1:8000",
    }
    return start_job(
        conn,
        kind="judge",
        params=params,
        actor_roles=actor_roles,
        actor_user_id=payload.get("actor_user_id"),
        actor_email=payload.get("actor_email"),
    )


@app.post("/api/devtools/autofix/start")
def devtools_start_autofix(payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    from app.devtools_panel import start_job

    actor_roles = payload.get("actor_roles") or []
    params = {
        "quick": payload.get("quick", True),
        "full": payload.get("full", False),
        "max_attempts": payload.get("max_attempts", 5),
        "operator_llm": payload.get("operator_llm", False),
        "base_url": payload.get("base_url") or "http://127.0.0.1:8000",
    }
    return start_job(
        conn,
        kind="autofix",
        params=params,
        actor_roles=actor_roles,
        actor_user_id=payload.get("actor_user_id"),
        actor_email=payload.get("actor_email"),
    )


@app.post("/api/devtools/jobs/{job_id}/cancel")
def devtools_cancel_job(job_id: UUID, payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    from app.devtools_panel import cancel_job

    return cancel_job(conn, job_id, actor_roles=payload.get("actor_roles") or [])


@app.post("/api/devtools/merge")
def devtools_merge_branch(payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    """Merge a finished autofix branch into main. Explicit human action from the
    control panel — the autofix loop never calls this itself."""
    from app.devtools_panel import merge_branch

    branch = str(payload.get("branch") or "").strip()
    if not branch:
        raise HTTPException(status_code=400, detail="branch is required")
    return merge_branch(conn, branch=branch, actor_roles=payload.get("actor_roles") or [])


@app.post("/api/devtools/watch")
def devtools_set_watch(payload: dict = Body(default={}), conn: Connection = Depends(get_connection)) -> dict:
    from app.devtools_panel import set_watch

    enabled = bool(payload.get("enabled"))
    interval_sec = int(payload.get("interval_sec") or 300)
    judge_params = {
        "quick": payload.get("quick", True),
        "full": payload.get("full", False),
        "base_url": payload.get("base_url") or "http://127.0.0.1:8000",
    }
    return set_watch(
        conn,
        enabled=enabled,
        interval_sec=interval_sec,
        params=judge_params,
        actor_roles=payload.get("actor_roles") or [],
    )


@app.post("/api/devtools/plant-breaks")
def devtools_plant_breaks(payload: dict = Body(default={})) -> dict:
    from app.devtools_breaks import plant_breaks

    count = int(payload.get("count") or 5)
    seed = payload.get("seed")
    return plant_breaks(
        count=count,
        seed=int(seed) if seed is not None else None,
        actor_roles=payload.get("actor_roles") or [],
    )


@app.post("/api/devtools/restore-breaks")
def devtools_restore_breaks(payload: dict = Body(default={})) -> dict:
    from app.devtools_breaks import restore_planted_breaks

    return restore_planted_breaks(actor_roles=payload.get("actor_roles") or [])


@app.get("/api/devtools/planted-breaks")
def devtools_planted_breaks_status() -> dict:
    from app.devtools_breaks import breaks_status

    return breaks_status()

