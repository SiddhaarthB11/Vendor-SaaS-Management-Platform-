"""Sync Derisk360 employee records from Microsoft 365 or the app directory."""

from app.employee_sync import get_employee_sync_status, sync_employees
from app.settings import get_settings
import psycopg


def main() -> None:
    status = get_employee_sync_status()
    print(f"Employee sync source: {status['source']}")

    settings = get_settings()
    with psycopg.connect(settings.database_url) as conn:
        result = sync_employees(conn)

    print(result["message"])
    print(
        f"Processed={result['totalProcessed']} created={result['created']} updated={result['updated']}"
    )


if __name__ == "__main__":
    main()
