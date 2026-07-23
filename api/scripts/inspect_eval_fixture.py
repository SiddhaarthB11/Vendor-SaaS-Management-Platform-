import psycopg
from psycopg.rows import dict_row

from app.copilot_eval import _cleanup_eval_org, _seed_eval_org
from app.settings import get_settings

conn = psycopg.connect(get_settings().database_url, row_factory=dict_row)
try:
    _cleanup_eval_org(conn)
    fx = _seed_eval_org(conn)
    org_id = fx["org_id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, department, status FROM slmct.subscriptions WHERE organisation_id = %s ORDER BY name",
            (org_id,),
        )
        rows = cur.fetchall()
        print("subs", len(rows))
        print("sample", rows[0] if rows else None)
        cur.execute(
            """
            SELECT COUNT(*) AS c FROM slmct.subscriptions
            WHERE organisation_id = %s AND department = 'Engineering' AND status = 'active'
            """,
            (org_id,),
        )
        print("eng_active", cur.fetchone())
        cur.execute("SELECT COUNT(*) AS c FROM slmct.licences WHERE organisation_id = %s", (org_id,))
        print("licences", cur.fetchone())
    _cleanup_eval_org(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM slmct.organisations WHERE code = 'copilot_eval_org'")
        print("org_left", cur.fetchone())
finally:
    conn.close()
