"""Autonomous gap-filling for Operator plans — resolves prices/URLs without user input."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Callable
from uuid import UUID

AUTOFILL_INTENT_RE = re.compile(
    r"\b(find|look\s*up|lookup|autofill|search\s+for|figure\s+out|fill\s+out|required\s+details|get\s+the)\b",
    re.IGNORECASE,
)
PLACEHOLDER_RE = re.compile(r"^\$question:", re.IGNORECASE)
LOOKUP_ANSWER_RE = re.compile(
    r"^(find(\s+it|\s+the\s+price|\s+the\s+url|\s+them)?|look\s*it\s*up|lookup|autofill|search|n/?a|unknown|same)$",
    re.IGNORECASE,
)
PRICE_QUESTION_RE = re.compile(r"\b(price|amount|cost)\b", re.IGNORECASE)
BILLING_QUESTION_RE = re.compile(r"\bbilling\b", re.IGNORECASE)
URL_QUESTION_RE = re.compile(r"\b(url|pricing\s+page|scrape)\b", re.IGNORECASE)
CURRENCY_QUESTION_RE = re.compile(r"\bcurrency\b", re.IGNORECASE)
RENEWAL_QUESTION_RE = re.compile(r"\brenewal\b", re.IGNORECASE)
STEP_REF_RE = re.compile(r"^\$step:(\d+):([\w.]+)$", re.IGNORECASE)

PricingResolver = Callable[[str, str, str | None], dict[str, Any] | None]


def is_step_ref(value: Any) -> bool:
    """True when value is a write-plan step reference like $step:0:vendor.id."""
    if value is None:
        return False
    return bool(STEP_REF_RE.match(str(value).strip()))


def wants_autofill(text: str) -> bool:
    return bool(AUTOFILL_INTENT_RE.search(text or ""))


def is_lookup_answer(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    if PLACEHOLDER_RE.match(text):
        return True
    if LOOKUP_ANSWER_RE.match(text):
        return True
    return False


def is_placeholder_value(value: Any) -> bool:
    if value is None:
        return False
    return bool(PLACEHOLDER_RE.match(str(value).strip()))


def is_missing_numeric(value: Any) -> bool:
    if value is None or value == "":
        return True
    if is_lookup_answer(value):
        return True
    if isinstance(value, (int, float)):
        return False
    if isinstance(value, str):
        try:
            float(value.replace(",", "").strip())
            return False
        except ValueError:
            return True
    return True


def parse_structured_details(instruction: str) -> dict[str, str]:
    """Extract concrete structured details from merged operator instruction."""
    details: dict[str, str] = {}
    for line in (instruction or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Answer to"):
            continue
        if ": " not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key not in {
            "vendor",
            "amount",
            "department",
            "billing_cycle",
            "currency_code",
            "fiscal_year",
            "scrape_url",
            "renewal_date",
        }:
            continue
        if value and not is_lookup_answer(value):
            details[key] = value
    return details


def _vendor_name_for_id(conn, vendor_id: str, org_id: UUID) -> str | None:
    if is_step_ref(vendor_id):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name FROM slmct.vendors WHERE id = %s AND organisation_id = %s LIMIT 1",
            (str(vendor_id), org_id),
        )
        row = cur.fetchone()
    return str(row["name"]) if row and row.get("name") else None


def resolve_subscription_pricing(
    conn,
    *,
    subscription_name: str,
    vendor_name: str,
    scrape_url: str | None = None,
) -> dict[str, Any] | None:
    """Look up monthly price, currency, and pricing URL for a subscription/product."""
    from app.price_scraper import (
        _FALLBACK_FX,
        _get_price_for_item,
        _load_fx,
        _to_aed,
        fetch_price_from_url,
        find_pricing_url,
    )

    name = (subscription_name or "").strip()
    vendor = (vendor_name or "").strip()
    url = (scrape_url or "").strip() or None
    if not name:
        return None

    original_price: float | None = None
    original_currency = "USD"
    resolved_url = url

    try:
        if url:
            try:
                fetched = fetch_price_from_url(url, lambda: conn, subscription_name=name)
                original_price = float(fetched.get("original_price") or fetched.get("price") or 0)
                original_currency = str(fetched.get("original_currency") or fetched.get("currency_code") or "USD")
                resolved_url = url
            except (ValueError, TypeError):
                pass

        if original_price is None:
            result = _get_price_for_item(name, vendor, url)
            if result:
                original_price, original_currency = float(result[0]), str(result[1] or "USD")

        if not resolved_url:
            resolved_url = find_pricing_url(name, vendor)

        if original_price is None:
            return None

        try:
            fx = _load_fx(conn)
        except Exception:
            fx = dict(_FALLBACK_FX)

        aed_price = float(_to_aed(original_price, original_currency, fx))
        return {
            "price": original_price,
            "amount": original_price,
            "currency_code": original_currency.upper(),
            "scrape_url": resolved_url,
            "price_aed": aed_price,
        }
    except Exception:
        return None


def _question_is_autofillable(question: str) -> bool:
    text = question or ""
    return bool(
        PRICE_QUESTION_RE.search(text)
        or BILLING_QUESTION_RE.search(text)
        or URL_QUESTION_RE.search(text)
        or CURRENCY_QUESTION_RE.search(text)
        or RENEWAL_QUESTION_RE.search(text)
    )


def _extract_plan_context(plan: dict[str, Any], details: dict[str, str], conn, org_id: UUID) -> dict[str, Any]:
    product_name = ""
    vendor_name = details.get("vendor", "")
    vendor_id = ""
    scrape_url = details.get("scrape_url", "")

    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        params = step.get("params") or {}
        if not isinstance(params, dict):
            continue
        if params.get("name") and not is_lookup_answer(params.get("name")):
            product_name = str(params["name"])
        if params.get("vendor_id") and not is_step_ref(params.get("vendor_id")) and not is_placeholder_value(params.get("vendor_id")):
            vendor_id = str(params["vendor_id"])
        if params.get("scrape_url") and not is_lookup_answer(params.get("scrape_url")):
            scrape_url = str(params["scrape_url"])

    if vendor_id and not vendor_name:
        vendor_name = _vendor_name_for_id(conn, vendor_id, org_id) or ""

    if not product_name:
        product_name = details.get("name", "")

    return {
        "product_name": product_name.strip(),
        "vendor_name": vendor_name.strip(),
        "scrape_url": scrape_url.strip() or None,
    }


def _apply_pricing_to_params(
    params: dict[str, Any],
    pricing: dict[str, Any],
    details: dict[str, str],
    *,
    tool_name: str = "",
) -> None:
    price = pricing.get("price")
    currency = details.get("currency_code") or pricing.get("currency_code") or "USD"
    scrape_url = pricing.get("scrape_url")
    tool = str(tool_name or "").strip()

    if "price" in params and is_missing_numeric(params.get("price")) and price is not None:
        params["price"] = price
    if "amount" in params and is_missing_numeric(params.get("amount")) and price is not None:
        params["amount"] = price
    if "currency_code" in params and (not params.get("currency_code") or is_lookup_answer(params.get("currency_code"))):
        params["currency_code"] = currency
    elif "currency_code" not in params and price is not None:
        params["currency_code"] = currency
    if scrape_url and (not params.get("scrape_url") or is_lookup_answer(params.get("scrape_url"))):
        params["scrape_url"] = scrape_url
    if tool == "create_subscription":
        if "billing_cycle" in params and (not params.get("billing_cycle") or is_lookup_answer(params.get("billing_cycle"))):
            params["billing_cycle"] = details.get("billing_cycle") or "monthly"
        elif "billing_cycle" not in params and price is not None:
            params["billing_cycle"] = details.get("billing_cycle") or "monthly"
        if "renewal_date" in params and (not params.get("renewal_date") or is_lookup_answer(params.get("renewal_date"))):
            params["renewal_date"] = details.get("renewal_date") or (date.today() + timedelta(days=365)).isoformat()


def autofill_plan_gaps(
    conn,
    plan: dict[str, Any],
    instruction: str,
    organisation_id: UUID,
    *,
    pricing_resolver: PricingResolver | None = None,
) -> dict[str, Any]:
    """
    Fill missing prices/URLs/billing in a draft plan when the user asked for lookup/autofill.
    Removes questions that were resolved autonomously.
    """
    from app.operator_entity_resolver import instruction_wants_budget_mutation

    if instruction_wants_budget_mutation(instruction):
        return plan

    result = dict(plan)
    steps = [dict(step) if isinstance(step, dict) else step for step in (result.get("steps") or [])]
    warnings = list(result.get("warnings") or [])
    questions = list(result.get("questions") or [])
    meta = dict(result.get("_meta") or {})

    details = parse_structured_details(instruction)
    autofill_requested = wants_autofill(instruction) or any(
        is_placeholder_value(value)
        for step in steps
        if isinstance(step, dict)
        for value in (step.get("params") or {}).values()
    )

    needs_pricing = autofill_requested or any(
        isinstance(step, dict)
        and step.get("tool") in {"create_vendor_catalogue_item", "create_subscription"}
        and (
            is_missing_numeric((step.get("params") or {}).get("price"))
            or is_missing_numeric((step.get("params") or {}).get("amount"))
            or any(is_placeholder_value(v) for v in ((step.get("params") or {}).values()))
        )
        for step in steps
    )
    if not needs_pricing:
        result["steps"] = steps
        return result

    context = _extract_plan_context({"steps": steps}, details, conn, organisation_id)
    product_name = context["product_name"]
    vendor_name = context["vendor_name"]
    scrape_url = context["scrape_url"]

    pricing: dict[str, Any] | None = None
    if product_name and (autofill_requested or any(is_missing_numeric((s.get("params") or {}).get("price")) or is_missing_numeric((s.get("params") or {}).get("amount")) for s in steps if isinstance(s, dict))):
        if pricing_resolver:
            pricing = pricing_resolver(product_name, vendor_name, scrape_url)
        else:
            pricing = resolve_subscription_pricing(
                conn,
                subscription_name=product_name,
                vendor_name=vendor_name,
                scrape_url=scrape_url,
            )

    if pricing:
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("tool") not in {"create_vendor_catalogue_item", "create_subscription"}:
                continue
            params = dict(step.get("params") or {})
            _apply_pricing_to_params(params, pricing, details, tool_name=str(step.get("tool") or ""))
            step["params"] = params

        for key, value in details.items():
            if key in {"department"} and value:
                for step in steps:
                    params = step.get("params") if isinstance(step, dict) else None
                    if isinstance(params, dict) and key in params and not params.get(key):
                        params[key] = value

        resolved = []
        if pricing.get("price") is not None:
            resolved.append(f"price {pricing['price']} {pricing.get('currency_code', 'USD')}/month")
        if pricing.get("scrape_url"):
            resolved.append(f"pricing URL {pricing['scrape_url']}")
        if resolved:
            warnings.append("Auto-resolved via lookup: " + "; ".join(resolved) + ".")
        meta["autofilled"] = True
        meta["autofill_pricing"] = {
            k: pricing[k]
            for k in ("price", "currency_code", "scrape_url")
            if k in pricing
        }

        if autofill_requested:
            questions = [q for q in questions if not _question_is_autofillable(q)]
    elif autofill_requested and questions:
        warnings.append(
            "Could not auto-resolve pricing from public sources — please provide monthly price or a direct pricing page URL."
        )

    result["steps"] = steps
    result["questions"] = questions
    result["warnings"] = warnings
    result["_meta"] = meta
    return result
