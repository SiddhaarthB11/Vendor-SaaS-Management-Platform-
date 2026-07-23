"""Single gateway for all LLM calls — routing, prompts, logging."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Json

from app.prompts import get_prompt
from app.settings import get_settings

log = logging.getLogger(__name__)

# Model identifiers — the only place in the codebase that names models.
_GEMINI_FLASH_25 = "gemini-2.5-flash"
_GEMINI_PRO_25 = "gemini-2.5-pro"

DEFAULT_CANDIDATES = [_GEMINI_FLASH_25, _GEMINI_PRO_25]

TASK_CANDIDATES: dict[str, list[str]] = {
    "copilot": DEFAULT_CANDIDATES,
    "contract_extract": [_GEMINI_FLASH_25],
    "tool_summary": [_GEMINI_FLASH_25],
    "vendor_autofill": [_GEMINI_FLASH_25],
    "vendor_purchase_url": [_GEMINI_FLASH_25],
    "workflow_review": DEFAULT_CANDIDATES,
    "email_message": [_GEMINI_FLASH_25, _GEMINI_PRO_25],
    "diag_ai_analysis": [_GEMINI_FLASH_25],
    "operator_plan": DEFAULT_CANDIDATES,
    "price_from_html": [_GEMINI_FLASH_25],
    "price_by_search": [_GEMINI_FLASH_25],
    "find_pricing_url": [_GEMINI_FLASH_25],
    "find_password_change_url": [_GEMINI_FLASH_25],
    "default": DEFAULT_CANDIDATES,
}

NOTDIAMOND_MODEL_SELECT_URL = "https://api.notdiamond.ai/v2/modelRouter/modelSelect"
# Tasks that may call Not Diamond modelSelect when NOTDIAMOND_ROUTING_ENABLED (not prompt optimize).
ND_ROUTED_TASKS = frozenset({"copilot", "operator_plan"})


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str = "gemini"
    latency_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    raw_response: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    function_calls: list[dict[str, Any]] = field(default_factory=list)


def extract_function_calls(response: Any) -> list[dict[str, Any]]:
    """Parse Gemini function_call parts from a generate_content response."""
    calls: list[dict[str, Any]] = []
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if not fc:
                continue
            name = str(getattr(fc, "name", "") or "").strip()
            if not name:
                continue
            raw_args = getattr(fc, "args", None)
            if raw_args is None:
                args: dict[str, Any] = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = dict(raw_args)
                except Exception:
                    args = {}
            calls.append({"name": name, "args": args})
    return calls


def response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text).strip()
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        chunks = [getattr(part, "text", "") for part in parts if getattr(part, "text", "")]
        if chunks:
            return "".join(chunks).strip()
    return ""


def _usage_from_response(response: Any) -> tuple[int | None, int | None, int | None]:
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return None, None, None
    input_tokens = getattr(usage, "prompt_token_count", None)
    output_tokens = getattr(usage, "candidates_token_count", None)
    total_tokens = getattr(usage, "total_token_count", None)
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = int(input_tokens) + int(output_tokens)
    return input_tokens, output_tokens, total_tokens


def _messages_for_routing(contents: Any, system_instruction: str | None) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction[:4000]})
    if isinstance(contents, str):
        messages.append({"role": "user", "content": contents[:4000]})
    elif isinstance(contents, list):
        for item in contents:
            role = getattr(item, "role", None) or "user"
            parts = getattr(getattr(item, "parts", None), "__iter__", lambda: [])()
            text_chunks = []
            try:
                for part in item.parts or []:
                    txt = getattr(part, "text", None)
                    if txt:
                        text_chunks.append(str(txt))
            except Exception:
                text_chunks.append(str(item))
            if text_chunks:
                messages.append({"role": "model" if role == "model" else "user", "content": "\n".join(text_chunks)[:4000]})
    else:
        messages.append({"role": "user", "content": str(contents)[:4000]})
    return messages


def _parse_nd_routed_model(data: dict[str, Any], candidates: list[str]) -> str | None:
    model = str(data.get("model") or "").strip()
    if model in candidates:
        return model
    providers = data.get("providers") or []
    if providers and isinstance(providers, list):
        first = providers[0]
        if isinstance(first, dict):
            model = str(first.get("model") or "").strip()
            if model in candidates:
                return model
    return None


def _route_model_via_not_diamond(task: str, contents: Any, system_instruction: str | None, candidates: list[str]) -> str | None:
    settings = get_settings()
    if not settings.notdiamond_api_key or not settings.notdiamond_routing_enabled:
        return None
    messages = _messages_for_routing(contents, system_instruction)
    llm_providers = [{"provider": "google", "model": model} for model in candidates]

    try:
        from notdiamond import NotDiamond

        client = NotDiamond(api_key=settings.notdiamond_api_key)
        result = client.model_router.select_model(
            messages=messages,
            llm_providers=llm_providers,
            tradeoff="cost",
        )
        providers = getattr(result, "providers", None) or []
        if providers:
            model = str(getattr(providers[0], "model", "") or "").strip()
            if model in candidates:
                return model
    except Exception as exc:
        log.warning("Not Diamond SDK routing error for task=%s: %s", task, exc)

    payload = {"messages": messages, "llm_providers": llm_providers, "tradeoff": "cost"}
    try:
        response = httpx.post(
            NOTDIAMOND_MODEL_SELECT_URL,
            headers={"Authorization": f"Bearer {settings.notdiamond_api_key}"},
            json=payload,
            timeout=20,
        )
        if response.status_code != 200:
            log.warning("Not Diamond routing failed for task=%s status=%s", task, response.status_code)
            return None
        routed = _parse_nd_routed_model(response.json(), candidates)
        if routed:
            return routed
    except Exception as exc:
        log.warning("Not Diamond routing error for task=%s: %s", task, exc)
    return None


def _candidate_models(task: str, explicit_model: str | None) -> list[str]:
    if explicit_model:
        return [explicit_model]
    return list(TASK_CANDIDATES.get(task) or TASK_CANDIDATES["default"])


def _build_gemini_config(
    *,
    system_instruction: str | None,
    temperature: float | None,
    response_mime_type: str | None,
    max_output_tokens: int | None,
    thinking_budget: int | None,
    google_search: bool,
    tools: list | None,
):
    from google.genai import types as gtypes

    kwargs: dict[str, Any] = {}
    if system_instruction:
        kwargs["system_instruction"] = system_instruction
    if temperature is not None:
        kwargs["temperature"] = temperature
    if response_mime_type:
        kwargs["response_mime_type"] = response_mime_type
    if max_output_tokens is not None:
        kwargs["max_output_tokens"] = max_output_tokens
    if thinking_budget is not None:
        kwargs["thinking_config"] = gtypes.ThinkingConfig(thinking_budget=thinking_budget)
    if google_search:
        kwargs["tools"] = [gtypes.Tool(google_search=gtypes.GoogleSearch())]
    elif tools:
        kwargs["tools"] = tools
    return gtypes.GenerateContentConfig(**kwargs)


def _log_llm_call(
    conn: Connection | None,
    *,
    task: str,
    model: str,
    prompt_variant: str | None,
    latency_ms: int,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    metadata: dict[str, Any] | None,
) -> None:
    meta = metadata or {}
    own_conn = False
    if conn is None:
        try:
            conn = psycopg.connect(get_settings().database_url, row_factory=dict_row)
            own_conn = True
        except Exception as exc:
            log.warning("llm_calls log skipped — no DB connection: %s", exc)
            return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO slmct.llm_calls (
                  task, model, prompt_variant, input_tokens, output_tokens, total_tokens, latency_ms, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    task,
                    model,
                    prompt_variant,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    latency_ms,
                    Json(meta),
                ),
            )
        conn.commit()
    except Exception as exc:
        if own_conn:
            conn.rollback()
        log.warning("llm_calls insert failed: %s", exc)
    finally:
        if own_conn and conn is not None:
            conn.close()


def get_copilot_candidate_models() -> list[str]:
    """Return copilot model candidates for eval/optimization (single source of truth)."""
    return list(TASK_CANDIDATES["copilot"])


def get_primary_copilot_model() -> str:
    """Strong default model for copilot baseline eval (Phase 1d comparison)."""
    return get_copilot_candidate_models()[0]


def notdiamond_configured() -> bool:
    settings = get_settings()
    return bool(settings.notdiamond_api_key)


def notdiamond_routing_active() -> bool:
    settings = get_settings()
    return bool(settings.notdiamond_api_key and settings.notdiamond_routing_enabled)


def notdiamond_optimize_allowed() -> bool:
    settings = get_settings()
    return bool(settings.notdiamond_api_key and settings.notdiamond_optimize_enabled)


def complete(
    task: str,
    *,
    contents: Any,
    system_instruction: str | None = None,
    model: str | None = None,
    prompt_variant: str | None = None,
    temperature: float | None = None,
    response_mime_type: str | None = None,
    max_output_tokens: int | None = None,
    thinking_budget: int | None = None,
    google_search: bool = False,
    tools: list | None = None,
    conn: Connection | None = None,
    metadata: dict[str, Any] | None = None,
) -> LLMResult:
    """Run a Gemini completion for the given task with logging and model fallback."""
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")

    if system_instruction is None:
        variant = prompt_variant or "default"
        loaded = get_prompt(task, model, variant=variant if variant != "default" else None)
        if loaded:
            system_instruction = loaded

    candidates = _candidate_models(task, model)
    routed: str | None = None
    if task in ND_ROUTED_TASKS and model is None:
        routed = _route_model_via_not_diamond(task, contents, system_instruction, candidates)
    models_to_try = [routed] + [m for m in candidates if m != routed] if routed else candidates

    import httpx as _httpx
    from google import genai

    client = genai.Client(
        api_key=settings.gemini_api_key,
        http_options={"httpx_client": _httpx.Client(verify=False)},
    )
    config = _build_gemini_config(
        system_instruction=system_instruction,
        temperature=temperature,
        response_mime_type=response_mime_type,
        max_output_tokens=max_output_tokens,
        thinking_budget=thinking_budget,
        google_search=google_search,
        tools=tools,
    )

    started = time.perf_counter()
    last_error: Exception | None = None
    model_errors: list[str] = []
    response = None
    used_model = models_to_try[0]

    for model_name in models_to_try:
        try:
            response = client.models.generate_content(model=model_name, contents=contents, config=config)
            used_model = model_name
            break
        except Exception as exc:
            last_error = exc
            model_errors.append(f"{model_name}: {exc}")
            log.warning("Gemini model %s failed for task=%s: %s", model_name, task, exc)
            continue

    if response is None:
        detail = "; ".join(model_errors) if model_errors else "unknown error"
        raise RuntimeError(f"No Gemini model available ({detail})") from last_error

    latency_ms = int((time.perf_counter() - started) * 1000)
    input_tokens, output_tokens, total_tokens = _usage_from_response(response)
    text = response_text(response)
    function_calls = extract_function_calls(response)

    log_meta = dict(metadata or {})
    if routed:
        log_meta["routed_by"] = "notdiamond"
    if function_calls:
        log_meta["function_calls"] = [fc["name"] for fc in function_calls]
    _log_llm_call(
        conn,
        task=task,
        model=used_model,
        prompt_variant=prompt_variant,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        metadata=log_meta,
    )

    return LLMResult(
        text=text,
        model=used_model,
        provider="gemini",
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        raw_response=response,
        metadata=log_meta,
        function_calls=function_calls,
    )
