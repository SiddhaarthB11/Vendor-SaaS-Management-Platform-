"""Sync employee records from a configured Microsoft 365 directory."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from psycopg import Connection

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


@dataclass(frozen=True)
class EmployeeSyncConfig:
    org_code: str
    tenant_id: str
    client_id: str
    client_secret: str
    email_domain: str

    @property
    def graph_configured(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret)

    @classmethod
    def from_env(cls) -> "EmployeeSyncConfig":
        return cls(
            org_code=os.environ.get("EMPLOYEE_SYNC_ORG_CODE", "derisk360_group").strip() or "derisk360_group",
            tenant_id=os.environ.get("MS_GRAPH_TENANT_ID", "").strip(),
            client_id=os.environ.get("MS_GRAPH_CLIENT_ID", "").strip(),
            client_secret=os.environ.get("MS_GRAPH_CLIENT_SECRET", "").strip(),
            email_domain=os.environ.get("EMPLOYEE_SYNC_EMAIL_DOMAIN", "").strip().lower(),
        )


def get_employee_sync_status() -> dict:
    config = EmployeeSyncConfig.from_env()
    return {
        "orgCode": config.org_code,
        "source": "microsoft_graph" if config.graph_configured else "app_directory",
        "graphConfigured": config.graph_configured,
        "emailDomainFilter": config.email_domain or None,
        "requiredPermissions": [
            "User.Read.All (application)",
        ],
    }


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    data: bytes | None = None,
) -> dict:
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc


def _get_graph_access_token(config: EmployeeSyncConfig) -> str:
    payload = urllib.parse.urlencode(
        {
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "scope": GRAPH_SCOPE,
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")
    token_url = f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/token"
    result = _http_json(
        token_url,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    token = str(result.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Microsoft Graph token request did not return an access token.")
    return token


def _normalize_email(raw: str | None) -> str | None:
    value = str(raw or "").strip().lower()
    if not value or "#ext#" in value:
        return None
    if not re.match(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$", value):
        return None
    return value


def _matches_domain(email: str, domain: str) -> bool:
    if not domain:
        return True
    return email.endswith(f"@{domain}")


def _fetch_graph_users(config: EmployeeSyncConfig) -> list[dict]:
    token = _get_graph_access_token(config)
    headers = {"Authorization": f"Bearer {token}"}
    query = urllib.parse.urlencode(
        {
            "$select": "id,displayName,mail,userPrincipalName,jobTitle,department,employeeId,accountEnabled,userType",
            "$top": "999",
            "$filter": "accountEnabled eq true and userType eq 'Member'",
        }
    )
    url = f"{GRAPH_BASE}/users?{query}"
    users: list[dict] = []

    while url:
        payload = _http_json(url, headers=headers)
        users.extend(payload.get("value") or [])
        url = payload.get("@odata.nextLink")

    directory: list[dict] = []
    for user in users:
        email = _normalize_email(user.get("mail")) or _normalize_email(user.get("userPrincipalName"))
        if not email or not _matches_domain(email, config.email_domain):
            continue
        full_name = str(user.get("displayName") or email.split("@", 1)[0]).strip()
        employee_number = str(user.get("employeeId") or "").strip() or f"MS-{str(user.get('id') or '')[:8].upper()}"
        directory.append(
            {
                "employee_number": employee_number,
                "full_name": full_name,
                "work_email": email,
                "department": str(user.get("department") or "").strip() or None,
                "job_title": str(user.get("jobTitle") or "").strip() or None,
                "status": "active" if user.get("accountEnabled", True) else "inactive",
            }
        )
    return directory


def _fetch_app_directory_users(conn: Connection, org_id) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COALESCE(NULLIF(p.employee_number, ''), CONCAT('APP-', UPPER(SUBSTRING(p.id::text, 1, 8)))) AS employee_number,
              p.full_name,
              lower(p.work_email::text) AS work_email,
              p.department,
              p.job_title,
              p.status::text AS status
            FROM slmct.people p
            JOIN slmct.app_users au ON au.person_id = p.id
            WHERE p.organisation_id = %s
              AND p.work_email IS NOT NULL
              AND au.status = 'active'
            ORDER BY p.full_name
            """,
            (org_id,),
        )
        rows = cur.fetchall()

    directory: list[dict] = []
    for row in rows:
        email = _normalize_email(row.get("work_email") if isinstance(row, dict) else row[2])
        if not email:
            continue
        directory.append(
            {
                "employee_number": str(row.get("employee_number") if isinstance(row, dict) else row[0]),
                "full_name": str(row.get("full_name") if isinstance(row, dict) else row[1]),
                "work_email": email,
                "department": row.get("department") if isinstance(row, dict) else row[3],
                "job_title": row.get("job_title") if isinstance(row, dict) else row[4],
                "status": str(row.get("status") if isinstance(row, dict) else row[5] or "active"),
            }
        )
    return directory


def _resolve_org_id(conn: Connection, org_code: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM slmct.organisations
            WHERE code = %s AND is_active = true
            LIMIT 1
            """,
            (org_code,),
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Organisation '{org_code}' was not found.")
    return row["id"] if isinstance(row, dict) else row[0]


def _upsert_employee(conn: Connection, org_id, entry: dict) -> str:
    employee_number = str(entry.get("employee_number") or "").strip() or None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, lower(work_email::text) AS work_email
            FROM slmct.people
            WHERE lower(work_email::text) = lower(%s)
               OR (%s IS NOT NULL AND employee_number = %s)
            LIMIT 1
            """,
            (entry["work_email"], employee_number, employee_number),
        )
        existing = cur.fetchone()

        if existing:
            person_id = existing["id"] if isinstance(existing, dict) else existing[0]
            existing_email = existing["work_email"] if isinstance(existing, dict) else existing[1]
            if str(existing_email).lower() != str(entry["work_email"]).lower():
                employee_number = f"{employee_number}-{str(entry['work_email']).split('@', 1)[0][:8].upper()}"
            cur.execute(
                """
                UPDATE slmct.people
                SET organisation_id = %s,
                    full_name = %s,
                    work_email = %s,
                    department = COALESCE(%s, department),
                    job_title = COALESCE(%s, job_title),
                    status = %s::slmct.person_status,
                    employee_number = COALESCE(%s, employee_number),
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    org_id,
                    entry["full_name"],
                    entry["work_email"],
                    entry.get("department"),
                    entry.get("job_title"),
                    entry.get("status", "active"),
                    employee_number,
                    person_id,
                ),
            )
            return "updated"

        cur.execute(
            """
            INSERT INTO slmct.people (
              organisation_id, employee_number, full_name, work_email, department, job_title, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::slmct.person_status)
            """,
            (
                org_id,
                employee_number,
                entry["full_name"],
                entry["work_email"],
                entry.get("department"),
                entry.get("job_title"),
                entry.get("status", "active"),
            ),
        )
        return "created"


def sync_employees(conn: Connection) -> dict:
    config = EmployeeSyncConfig.from_env()
    org_id = _resolve_org_id(conn, config.org_code)

    if config.graph_configured:
        directory = _fetch_graph_users(config)
        source = "microsoft_graph"
    else:
        directory = _fetch_app_directory_users(conn, org_id)
        source = "app_directory"

    created = 0
    updated = 0
    for entry in directory:
        action = _upsert_employee(conn, org_id, entry)
        if action == "created":
            created += 1
        else:
            updated += 1

    conn.commit()
    return {
        "source": source,
        "organisationCode": config.org_code,
        "totalProcessed": len(directory),
        "created": created,
        "updated": updated,
        "graphConfigured": config.graph_configured,
        "message": (
            f"Synced {len(directory)} employee record(s) from {'your Microsoft 365 tenant' if config.graph_configured else 'configured app users'}."
        ),
    }
