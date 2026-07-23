"""Copilot handlers — legacy state mode and tool-calling mode."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from google.genai import types as gtypes
from psycopg import Connection

from app.llm import complete, extract_function_calls
from app.prompts import get_prompt
from app.settings import get_settings
from app.tools import Tool, execute_tool, get_tool_by_name, get_toolset, toolset_to_gemini_tools

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 6


@dataclass
class CopilotInput:
    messages: list[dict]
    state: dict | None = None
    organisation_id: str | None = None
    actor_roles: list[str] | None = None
    actor_user_id: str | None = None
    model: str | None = None
    prompt_variant: str | None = None
    copilot_mode: str | None = None


def resolve_copilot_mode(req: CopilotInput) -> str:
    explicit = str(req.copilot_mode or "").strip().lower()
    if explicit in ("tools", "legacy"):
        return explicit
    mode = str(get_settings().copilot_mode or "tools").strip().lower()
    return mode if mode in ("tools", "legacy") else "tools"


def _build_contents(messages: list[dict]) -> list[gtypes.Content]:
    contents: list[gtypes.Content] = []
    for msg in messages:
        content = str(msg.get("content", "")).strip()
        if not content or msg.get("id") == "welcome":
            continue
        role = "model" if msg.get("role") in ("assistant", "model") else "user"
        contents.append(gtypes.Content(role=role, parts=[gtypes.Part.from_text(text=content)]))
    if not contents:
        contents.append(
            gtypes.Content(role="user", parts=[gtypes.Part.from_text(text="Summarize the current software estate.")])
        )
    return contents


def run_copilot_legacy(req: CopilotInput, conn: Connection | None = None) -> dict[str, Any]:
    if not req.state:
        raise ValueError("Legacy copilot mode requires a state payload.")

    variant = req.prompt_variant if req.prompt_variant and req.prompt_variant != "default" else "legacy"
    copilot_template = get_prompt("copilot", req.model, variant=variant)
    if "<state>" not in copilot_template:
        copilot_template = get_prompt("copilot", req.model, variant="legacy")
    system_prompt = copilot_template.replace("<state>", json.dumps(req.state, indent=2))
    contents = _build_contents(req.messages)

    result = complete(
        "copilot",
        contents=contents,
        system_instruction=system_prompt,
        model=req.model,
        prompt_variant=req.prompt_variant or "legacy",
        temperature=0.2,
        conn=conn,
        metadata={"mode": "legacy"},
    )
    response_text = (result.text or "").strip()
    if not response_text:
        raise RuntimeError("Gemini returned an empty response")

    return {
        "responseText": response_text,
        "provider": "gemini",
        "model": result.model,
        "prompt_variant": req.prompt_variant or "legacy",
        "mode": "legacy",
        "toolCalls": [],
        "iterations": 1,
    }


def _execute_tool_call(
    fc: dict[str, Any],
    *,
    conn: Connection,
    organisation_id: str,
    actor: dict[str, Any],
    toolset_by_name: dict[str, Tool],
    tool_errors: dict[str, int],
) -> dict[str, Any]:
    name = str(fc.get("name") or "").strip()
    args = fc.get("args") or {}
    if not isinstance(args, dict):
        try:
            args = dict(args)
        except Exception:
            args = {}

    tool = toolset_by_name.get(name) or get_tool_by_name(name)
    if tool is None:
        payload = {"error": f"Tool '{name}' is not available for your role."}
        tool_errors[name] = tool_errors.get(name, 0) + 1
        return payload

    try:
        payload = execute_tool(tool, conn, organisation_id, actor, **args)
        if isinstance(payload, dict) and payload.get("error"):
            tool_errors[name] = tool_errors.get(name, 0) + 1
        return payload
    except Exception as exc:
        tool_errors[name] = tool_errors.get(name, 0) + 1
        log.warning("copilot tool error %s: %s", name, exc)
        return {"error": str(exc)}


def _model_turn_content(response: Any) -> gtypes.Content | None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    content = getattr(candidates[0], "content", None)
    if content is None:
        return None
    return content


def _should_surface_tool_errors(tool_errors: dict[str, int]) -> list[str]:
    surfaced: list[str] = []
    for name, count in tool_errors.items():
        if count >= 2:
            surfaced.append(name)
    return surfaced


def run_copilot_tools(req: CopilotInput, conn: Connection) -> dict[str, Any]:
    if not req.organisation_id:
        raise ValueError("Tool-calling copilot mode requires organisation_id.")
    if not req.actor_roles:
        raise ValueError("Tool-calling copilot mode requires actor_roles.")

    organisation_id = str(req.organisation_id)
    actor = {"roles": list(req.actor_roles or []), "user_id": req.actor_user_id}
    toolset = get_toolset(req.actor_roles)
    toolset_by_name = {tool.name: tool for tool in toolset}
    gemini_tools = toolset_to_gemini_tools(toolset)

    system_prompt = get_prompt(
        "copilot",
        req.model,
        variant=req.prompt_variant if req.prompt_variant and req.prompt_variant != "default" else None,
    )
    contents = _build_contents(req.messages)

    tool_calls_used: list[str] = []
    tool_errors: dict[str, int] = {}
    used_model = req.model or ""
    iterations = 0
    last_result = None

    for iteration in range(MAX_TOOL_ITERATIONS):
        iterations = iteration + 1
        result = complete(
            "copilot",
            contents=contents,
            system_instruction=system_prompt,
            model=req.model,
            prompt_variant=req.prompt_variant,
            temperature=0.2,
            tools=gemini_tools if gemini_tools else None,
            conn=conn,
            metadata={"mode": "tools", "iteration": iteration, "organisation_id": organisation_id},
        )
        last_result = result
        used_model = result.model
        function_calls = result.function_calls or extract_function_calls(result.raw_response)

        if not function_calls:
            response_text = (result.text or "").strip()
            if not response_text and iteration < MAX_TOOL_ITERATIONS:
                contents.append(
                    gtypes.Content(
                        role="user",
                        parts=[gtypes.Part(text="Please provide a concise answer based on the tool results above.")],
                    )
                )
                continue
            if not response_text:
                raise RuntimeError("Gemini returned an empty response")
            surfaced = _should_surface_tool_errors(tool_errors)
            if surfaced:
                response_text += (
                    "\n\n>Note: Some data lookups failed after retry ("
                    + ", ".join(surfaced)
                    + "). Results above may be incomplete."
                )
            return {
                "responseText": response_text,
                "provider": "gemini",
                "model": used_model,
                "prompt_variant": req.prompt_variant or "default",
                "mode": "tools",
                "toolCalls": tool_calls_used,
                "iterations": iterations,
            }

        model_turn = _model_turn_content(result.raw_response)
        if model_turn is not None:
            contents.append(model_turn)

        response_parts: list[gtypes.Part] = []
        for fc in function_calls:
            name = str(fc.get("name") or "")
            tool_calls_used.append(name)
            payload = _execute_tool_call(
                fc,
                conn=conn,
                organisation_id=organisation_id,
                actor=actor,
                toolset_by_name=toolset_by_name,
                tool_errors=tool_errors,
            )
            response_parts.append(
                gtypes.Part.from_function_response(name=name, response={"result": payload})
            )

        contents.append(gtypes.Content(role="user", parts=response_parts))

        if _should_surface_tool_errors(tool_errors):
            break

    # Max iterations or repeated tool errors — force a final answer without tools.
    contents.append(
        gtypes.Content(
            role="user",
            parts=[
                gtypes.Part.from_text(
                    text=(
                        "Provide your final answer now using the tool results above. "
                        "Do not request more tool calls. If data is missing, say so plainly."
                    )
                )
            ],
        )
    )
    final = complete(
        "copilot",
        contents=contents,
        system_instruction=system_prompt,
        model=req.model or used_model,
        prompt_variant=req.prompt_variant,
        temperature=0.2,
        conn=conn,
        metadata={"mode": "tools", "iteration": "final", "organisation_id": organisation_id},
    )
    response_text = (final.text or "").strip()
    if not response_text:
        raise RuntimeError("Gemini returned an empty response after tool loop")
    surfaced = _should_surface_tool_errors(tool_errors)
    if surfaced:
        response_text += (
            "\n\n>Note: Some data lookups failed after retry ("
            + ", ".join(surfaced)
            + "). Results above may be incomplete."
        )
    return {
        "responseText": response_text,
        "provider": "gemini",
        "model": final.model,
        "prompt_variant": req.prompt_variant or "default",
        "mode": "tools",
        "toolCalls": tool_calls_used,
        "iterations": iterations + 1,
    }


def run_copilot(req: CopilotInput, conn: Connection | None = None) -> dict[str, Any]:
    mode = resolve_copilot_mode(req)
    if mode == "legacy":
        return run_copilot_legacy(req, conn=conn)
    if conn is None:
        raise ValueError("Tool-calling copilot mode requires a database connection.")
    return run_copilot_tools(req, conn)
