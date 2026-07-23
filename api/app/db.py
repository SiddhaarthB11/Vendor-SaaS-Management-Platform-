from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row

from app.settings import get_settings


def get_connection() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(get_settings().database_url, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()
