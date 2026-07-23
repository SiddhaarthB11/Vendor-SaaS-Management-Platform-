"""
Nightly price scraper for vendor catalogue items.

Strategy per item:
  1. If scrape_url exists: fetch HTML -> regex price
  2. If that fails or no URL: Gemini reads the HTML text
  3. If that fails or no URL: Gemini searches the web by subscription name (covers JS-rendered sites like Adobe)
  4. Convert whatever is found to AED via live FX rates

This means EVERY catalogue item gets nightly updates regardless of vendor.
"""

import json
import logging
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

_TARGET_CURRENCY = "AED"

_FALLBACK_FX = {
    "USD": 1.0, "AED": 3.6725, "GBP": 0.79, "EUR": 0.92,
    "INR": 83.5, "SAR": 3.75, "QAR": 3.64, "KWD": 0.307,
}

_PRICE_PATTERNS = [
    ("INR", re.compile(u"₹\\s*([\\d,]+\\.?\\d*)")),
    ("USD", re.compile(r"\$\s*([\d,]+\.?\d*)")),
    ("GBP", re.compile(u"£\\s*([\\d,]+\\.?\\d*)")),
    ("EUR", re.compile(u"€\\s*([\\d,]+\\.?\\d*)")),
]

# Minimal headers - adding Accept triggers JS-shell from Microsoft (no prices)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}


def _load_fx(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT currency_code, rate_from_usd FROM slmct.fx_rates")
            rows = cur.fetchall()
        if rows:
            return {r["currency_code"]: float(r["rate_from_usd"]) for r in rows}
    except Exception as exc:
        log.warning("price_scraper: could not load FX from DB -- %s", exc)
    return dict(_FALLBACK_FX)


def _to_aed(price, from_currency, fx):
    rate_from = fx.get(from_currency, _FALLBACK_FX.get(from_currency, 1.0))
    rate_to = fx.get(_TARGET_CURRENCY, _FALLBACK_FX[_TARGET_CURRENCY])
    return round((price / rate_from) * rate_to, 2)


def _fetch_html(url):
    try:
        with httpx.Client(headers=_HEADERS, follow_redirects=True, timeout=20) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception as exc:
        log.warning("price_scraper: fetch failed for %s -- %s", url, exc)
        return None


def _regex_price(html):
    """Return (price, currency) for the first plausible per-month amount, or None."""
    for currency, pattern in _PRICE_PATTERNS:
        for raw in pattern.findall(html):
            try:
                val = float(raw.replace(",", ""))
            except ValueError:
                continue
            if 0 < val < 10_000:
                return val, currency
    return None


def _parse_gemini_json(text):
    raw = re.sub(r"^```[a-z]*\n?", "", text.strip())
    raw = re.sub(r"\n?```$", "", raw)
    # grab first JSON object
    m = re.search(r"\{[^{}]+\}", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    data = json.loads(raw)
    if data.get("price") and data.get("currency"):
        return float(data["price"]), str(data["currency"]).upper()
    return None


def _gemini_configured() -> bool:
    try:
        from app.settings import get_settings
        return bool(get_settings().gemini_api_key)
    except Exception:
        return False


def _gemini_price_from_html(url, html, subscription_name=""):
    """Ask Gemini to extract price from fetched page text (for JS-rendered pages)."""
    if not _gemini_configured():
        return None
    try:
        from app.llm import complete
        from app.prompts import format_prompt

        plan_hint = f' for the plan named "{subscription_name}"' if subscription_name else " (the most relevant plan)"
        prompt = format_prompt(
            "price_from_html",
            url=url,
            plan_hint=plan_hint,
            subscription_name=subscription_name,
            html=html[:20000],
        )
        result = complete(
            "price_from_html",
            contents=prompt,
            max_output_tokens=200,
            thinking_budget=0,
        )
        return _parse_gemini_json(result.text or "")
    except Exception as exc:
        log.warning("price_scraper: Gemini HTML parse failed -- %s", exc)
    return None


def _gemini_price_by_search(subscription_name, vendor_name=""):
    """
    Use Gemini with Google Search grounding to find the current price.
    Works for any vendor including JS-rendered sites like Adobe.
    This is the ultimate fallback and also the method for items with no URL.
    """
    if not _gemini_configured():
        log.warning("price_scraper: GEMINI_API_KEY not set, cannot search for price")
        return None
    try:
        from app.llm import complete
        from app.prompts import format_prompt

        context = f"{vendor_name} {subscription_name}".strip()
        prompt = format_prompt(
            "price_by_search",
            context=context,
            subscription_name=subscription_name,
        )
        for attempt in range(3):
            try:
                result = complete(
                    "price_by_search",
                    contents=prompt,
                    google_search=True,
                    max_output_tokens=200,
                    thinking_budget=0,
                )
                parsed = _parse_gemini_json(result.text or "")
                if parsed:
                    price, currency = parsed
                    if price is not None and (price <= 0 or price > 100_000):
                        log.warning(
                            "price_scraper: implausible price %s %s for %s — retrying",
                            price,
                            currency,
                            subscription_name,
                        )
                        continue
                    return parsed
            except Exception as exc:
                log.warning("price_scraper: attempt %d failed for %s -- %s", attempt + 1, subscription_name, exc)
    except Exception as exc:
        log.warning("price_scraper: Gemini search failed for %s -- %s", subscription_name, exc)
    return None


def find_pricing_url(subscription_name, vendor_name=""):
    """Find the official pricing page URL via Gemini search grounding."""
    if not _gemini_configured():
        return None
    try:
        from app.llm import complete
        from app.prompts import format_prompt

        context = f"{vendor_name} {subscription_name}".strip()
        prompt = format_prompt("find_pricing_url", context=context)
        result = complete(
            "find_pricing_url",
            contents=prompt,
            google_search=True,
            max_output_tokens=200,
            thinking_budget=0,
        )
        text = (result.text or "").strip()
        # Accept bare URL or extract first https URL from any surrounding text
        if text.startswith("http"):
            return text.strip("`").strip()
        m = re.search(r"https?://[^\s\)\]\"']+", text)
        if m:
            return m.group(0).rstrip(".")

    except Exception as exc:
        log.warning("price_scraper: find_pricing_url failed -- %s", exc)
    return None


def find_password_change_url(vendor_name):
    """Find the official account password change page for a vendor via Gemini search grounding."""
    if not _gemini_configured():
        return None
    try:
        from app.llm import complete
        from app.prompts import format_prompt

        prompt = format_prompt("find_password_change_url", vendor_name=vendor_name)
        result = complete(
            "find_password_change_url",
            contents=prompt,
            google_search=True,
            max_output_tokens=200,
            thinking_budget=0,
        )
        text = (result.text or "").strip()
        if text.startswith("http"):
            return text.strip("`").strip()
        m = re.search(r"https?://[^\s\)\]\"']+", text)
        if m:
            return m.group(0).rstrip(".")
    except Exception as exc:
        log.warning("price_scraper: find_password_change_url failed -- %s", exc)
    return None


def fetch_price_from_url(url, get_conn, subscription_name=""):
    """On-demand fetch for the /fetch-price endpoint (URL provided explicitly)."""
    html = _fetch_html(url)
    if html is None:
        raise ValueError("Could not load that page. Check the URL is a direct product/pricing page.")

    result = (
        _regex_price(html) if not subscription_name else None
    ) or _gemini_price_from_html(url, html, subscription_name) or (
        _gemini_price_by_search(subscription_name) if subscription_name else None
    )
    if result is None:
        raise ValueError(
            "Could not find a subscription price on that page. "
            "Make sure the URL goes directly to a pricing page that shows a monthly price."
        )

    raw_price, raw_currency = result
    try:
        conn = get_conn()
        fx = _load_fx(conn)
    except Exception:
        fx = dict(_FALLBACK_FX)

    return {
        "price": _to_aed(raw_price, raw_currency, fx),
        "currency_code": _TARGET_CURRENCY,
        "original_price": raw_price,
        "original_currency": raw_currency,
    }


def _get_price_for_item(name, vendor_name, url):
    """
    Full price resolution chain for a single catalogue item.
    Returns (price, currency) or None.
    """
    # Step 1: try fetching the URL and regex
    if url:
        html = _fetch_html(url)
        if html:
            result = _regex_price(html)
            if result:
                log.info("price_scraper: %s -- regex hit from URL", name)
                return result
            # Step 2: Gemini reads the HTML (catches JS-rendered pages where URL is known)
            result = _gemini_price_from_html(url, html)
            if result:
                log.info("price_scraper: %s -- Gemini HTML parse hit", name)
                return result

    # Step 3: Gemini searches the web by name (works for everything including Adobe)
    log.info("price_scraper: %s -- falling back to Gemini web search", name)
    return _gemini_price_by_search(name, vendor_name)


def run_price_scrape(get_conn):
    """
    Nightly job. Checks ALL catalogue items -- with or without a scrape_url.
    Uses the full resolution chain so every item gets updated.
    """
    summary = {"checked": 0, "updated": 0, "errors": 0, "skipped": 0}

    try:
        conn = get_conn()
    except Exception as exc:
        log.error("price_scraper: cannot get DB connection -- %s", exc)
        summary["errors"] += 1
        return summary

    fx = _load_fx(conn)
    log.info("price_scraper: FX INR=%.4f AED=%.4f", fx.get("INR", 0), fx.get("AED", 0))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT vc.id, vc.name, vc.price, vc.currency_code, vc.scrape_url,
                   v.name AS vendor_name
            FROM slmct.vendor_catalogue vc
            JOIN slmct.vendors v ON v.id = vc.vendor_id
            ORDER BY vc.name
            """
        )
        rows = cur.fetchall()

    for row in rows:
        item_id      = row["id"]
        name         = row["name"]
        old_price    = float(row["price"])
        old_currency = row["currency_code"]
        url          = row["scrape_url"] or ""
        vendor_name  = row["vendor_name"] or ""
        summary["checked"] += 1

        result = _get_price_for_item(name, vendor_name, url)
        if result is None:
            log.warning("price_scraper: could not get price for %s (%s)", name, vendor_name)
            summary["errors"] += 1
            continue

        raw_price, raw_currency = result
        new_price    = _to_aed(raw_price, raw_currency, fx)
        new_currency = _TARGET_CURRENCY
        now          = datetime.now(timezone.utc)

        price_changed    = abs(new_price - old_price) >= 0.01
        currency_changed = old_currency != new_currency

        if not price_changed and not currency_changed:
            log.info("price_scraper: %s -- no change (%.2f AED)", name, old_price)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE slmct.vendor_catalogue SET last_scraped_at=%s, updated_at=%s WHERE id=%s",
                    (now, now, item_id),
                )
            conn.commit()
            summary["skipped"] += 1
            continue

        log.info("price_scraper: %s -- %.2f %s -> %.2f AED (was %.2f %s)",
                 name, raw_price, raw_currency, new_price, old_price, old_currency)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE slmct.vendor_catalogue "
                "SET price=%s, currency_code=%s, last_scraped_at=%s, updated_at=%s "
                "WHERE id=%s",
                (new_price, new_currency, now, now, item_id),
            )
        conn.commit()
        summary["updated"] += 1

    log.info("price_scraper: done -- %s", summary)
    return summary
