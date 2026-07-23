"""Quick smoke test for copilot eval seed/teardown (no LLM calls)."""

import psycopg
from psycopg.rows import dict_row

from app.copilot_eval import _build_questions, _cleanup_eval_org, _seed_eval_org, build_copilot_state
from app.settings import get_settings


def main() -> None:
    conn = psycopg.connect(get_settings().database_url, row_factory=dict_row)
    try:
        _cleanup_eval_org(conn)
        fixture = _seed_eval_org(conn)
        org_id = fixture["org_id"]
        state = build_copilot_state(conn, org_id)
        questions = _build_questions()
        print(
            "seed_ok",
            {
                "subscriptions": len(state["subscriptions"]),
                "licences": len(state["licences"]),
                "budgets": len(state["budgets"]),
                "payments": len(state["payments"]),
                "workflows": len(state["workflows"]),
                "audit_logs": len(state["audit_logs"]),
                "questions": len(questions),
                "holdout": sum(1 for q in questions if q.holdout),
            },
        )
        _cleanup_eval_org(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM slmct.organisations WHERE code = 'copilot_eval_org'")
            leftover = cur.fetchone()
        print("teardown_ok", leftover is None)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
