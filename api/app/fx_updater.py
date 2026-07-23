"""
Daily FX rate updater.

Fetches live exchange rates from open.er-api.com (free, no key required)
and writes them into slmct.fx_rates.  Runs via APScheduler at 01:00 UTC
(after the price scraper at 00:00 so the scraper always converts with
today's rates from the previous day's fetch at minimum).
"""

import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

_FX_API_URL = "https://open.er-api.com/v6/latest/USD"
_CURRENCIES = ["USD", "AED", "GBP", "EUR", "INR", "SAR", "QAR", "KWD"]

# Fallback hardcoded rates (used only if the API is unreachable)
_FALLBACK = {
    "USD": 1.0, "AED": 3.6725, "GBP": 0.79,
    "EUR": 0.92, "INR": 83.5, "SAR": 3.75,
    "QAR": 3.64, "KWD": 0.307,
}


def fetch_live_rates() -> dict[str, float] | None:
    """Return {currency_code: rate_from_usd} or None on failure."""
    try:
        r = httpx.get(_FX_API_URL, timeout=15, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
        rates = data.get("rates", {})
        result = {"USD": 1.0}
        for code in _CURRENCIES:
            if code in rates:
                result[code] = float(rates[code])
        return result
    except Exception as exc:
        log.warning("fx_updater: API fetch failed — %s", exc)
        return None


def run_fx_update(get_conn) -> dict:
    """
    Fetch rates and write to DB.  get_conn is a zero-arg callable that
    returns a psycopg connection.  Returns a summary dict.
    """
    summary = {"updated": 0, "errors": 0}

    rates = fetch_live_rates()
    if rates is None:
        log.error("fx_updater: could not fetch live rates, keeping existing DB values")
        summary["errors"] += 1
        return summary

    try:
        conn = get_conn()
    except Exception as exc:
        log.error("fx_updater: cannot get DB connection — %s", exc)
        summary["errors"] += 1
        return summary

    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        for code, rate in rates.items():
            cur.execute(
                """
                INSERT INTO slmct.fx_rates (currency_code, rate_from_usd, fetched_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (currency_code) DO UPDATE
                  SET rate_from_usd = EXCLUDED.rate_from_usd,
                      fetched_at    = EXCLUDED.fetched_at
                """,
                (code, rate, now),
            )
            summary["updated"] += 1
    conn.commit()

    log.info("fx_updater: updated %d rates — %s", summary["updated"], {k: round(v, 4) for k, v in rates.items()})
    return summary
