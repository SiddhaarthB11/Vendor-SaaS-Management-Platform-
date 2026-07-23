"""Not Diamond prompt optimization for copilot (Phase 1c)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.prompts import COPILOT_TOOLS
from app.settings import get_settings

log = logging.getLogger(__name__)

ND_OPTIMIZE_URL = "https://api.notdiamond.ai/v2/promptOptimization/optimize"
ND_RESULTS_URL = "https://api.notdiamond.ai/v2/promptOptimization/results/{run_id}"


@dataclass
class NDPromptOptimizeResult:
    """Optimized prompts from Not Diamond."""

    model_prompts: dict[str, str] = field(default_factory=dict)
    variant_prompts: dict[str, str] = field(default_factory=dict)
    run_id: str | None = None
    error: str | None = None


def _google_target_models(model_slugs: list[str]) -> list[dict[str, str]]:
    return [{"provider": "google", "model": slug} for slug in model_slugs]


def _parse_nd_targets(targets: list[Any], target_model_slugs: list[str]) -> NDPromptOptimizeResult:
    model_prompts: dict[str, str] = {}
    variant_prompts: dict[str, str] = {}
    for target in targets:
        if isinstance(target, dict):
            model_name = str(target.get("model_name") or target.get("model") or "").strip()
            prompt = str(target.get("system_prompt") or "").strip()
        else:
            model_name = str(getattr(target, "model_name", "") or getattr(target, "model", "") or "").strip()
            prompt = str(getattr(target, "system_prompt", "") or "").strip()
        if not model_name or not prompt:
            continue
        if model_name in target_model_slugs:
            model_prompts[model_name] = prompt
        variant_prompts[f"nd_{model_name.replace('.', '_')}"] = prompt
    return NDPromptOptimizeResult(model_prompts=model_prompts, variant_prompts=variant_prompts)


def optimize_copilot_prompts_via_nd(
    train_goldens: list[dict],
    test_goldens: list[dict],
    *,
    target_model_slugs: list[str],
    system_prompt: str | None = None,
    poll_seconds: int = 15,
    max_wait_seconds: int = 1800,
) -> NDPromptOptimizeResult:
    """
    Run Not Diamond prompt optimization and return per-model optimized system prompts.
    Requires NOTDIAMOND_API_KEY and the notdiamond Python package.
    """
    settings = get_settings()
    if not settings.notdiamond_api_key:
        return NDPromptOptimizeResult(error="NOTDIAMOND_API_KEY not configured")
    if not settings.notdiamond_optimize_enabled:
        return NDPromptOptimizeResult(
            error=(
                "NOTDIAMOND_OPTIMIZE_ENABLED is false — ND prompt optimization is disabled. "
                "Set NOTDIAMOND_OPTIMIZE_ENABLED=true in infra/.env only when you intend to run it once."
            )
        )

    if len(train_goldens) < 3:
        return NDPromptOptimizeResult(error="Need at least 3 train goldens for ND prototype_mode")

    try:
        from notdiamond import NotDiamond
    except ImportError:
        return NDPromptOptimizeResult(error="notdiamond package not installed — rebuild API image")

    try:
        client = NotDiamond(api_key=settings.notdiamond_api_key)
        response = client.prompt_optimization.optimize(
            system_prompt=system_prompt or COPILOT_TOOLS,
            template="{question}",
            fields=["question"],
            train_goldens=train_goldens,
            test_goldens=test_goldens or train_goldens[:3],
            target_models=_google_target_models(target_model_slugs),
            evaluation_metric="LLMaaJ:Sem_Sim_1",
            prototype_mode=True,
        )
        run_id = str(response.optimization_run_id or "")
        log.info("ND prompt optimization started: run_id=%s", run_id)
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            results = client.prompt_optimization.get_optimization_results(run_id)
            status = str(getattr(results, "status", "") or "").lower()
            if status in ("completed", "complete", "succeeded", "success"):
                parsed = _parse_nd_targets(getattr(results, "target_models", None) or [], target_model_slugs)
                parsed.run_id = run_id
                return parsed
            if status in ("failed", "error", "cancelled"):
                return NDPromptOptimizeResult(run_id=run_id, error=f"ND job failed: {status}")
            time.sleep(poll_seconds)
        return NDPromptOptimizeResult(run_id=run_id, error=f"ND job timed out after {max_wait_seconds}s")
    except Exception as exc:
        log.warning("ND prompt optimization failed: %s", exc)
        rest = _optimize_via_rest(
            train_goldens,
            test_goldens,
            target_model_slugs=target_model_slugs,
            system_prompt=system_prompt,
            poll_seconds=poll_seconds,
            max_wait_seconds=max_wait_seconds,
        )
        if rest.model_prompts or rest.variant_prompts:
            return rest
        return NDPromptOptimizeResult(error=str(exc))


def _optimize_via_rest(
    train_goldens: list[dict],
    test_goldens: list[dict],
    *,
    target_model_slugs: list[str],
    system_prompt: str | None,
    poll_seconds: int,
    max_wait_seconds: int,
) -> NDPromptOptimizeResult:
    import httpx

    settings = get_settings()
    payload = {
        "system_prompt": system_prompt or COPILOT_TOOLS,
        "template": "{question}",
        "fields": ["question"],
        "train_goldens": train_goldens,
        "test_goldens": test_goldens or train_goldens[:3],
        "target_models": _google_target_models(target_model_slugs),
        "evaluation_metric": "LLMaaJ:Sem_Sim_1",
        "prototype_mode": True,
    }
    try:
        response = httpx.post(
            ND_OPTIMIZE_URL,
            headers={"Authorization": f"Bearer {settings.notdiamond_api_key}"},
            json=payload,
            timeout=60,
        )
        if response.status_code != 200:
            return NDPromptOptimizeResult(error=f"ND REST HTTP {response.status_code}: {response.text[:200]}")
        run_id = str(response.json().get("optimization_run_id") or "")
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            poll = httpx.get(
                ND_RESULTS_URL.format(run_id=run_id),
                headers={"Authorization": f"Bearer {settings.notdiamond_api_key}"},
                timeout=60,
            )
            if poll.status_code != 200:
                time.sleep(poll_seconds)
                continue
            data = poll.json()
            status = str(data.get("status") or "").lower()
            if status in ("completed", "complete", "succeeded", "success"):
                parsed = _parse_nd_targets(data.get("target_models") or [], target_model_slugs)
                parsed.run_id = run_id
                return parsed
            if status in ("failed", "error", "cancelled"):
                return NDPromptOptimizeResult(run_id=run_id, error=f"ND REST job failed: {status}")
            time.sleep(poll_seconds)
        return NDPromptOptimizeResult(run_id=run_id, error="ND REST job timed out")
    except Exception as exc:
        return NDPromptOptimizeResult(error=str(exc))


# Back-compat alias used by older optimize script paths.
def fetch_nd_copilot_variants(
    train_goldens: list[dict],
    test_goldens: list[dict],
    *,
    target_model_slugs: list[str],
    system_prompt: str | None = None,
    poll_seconds: int = 15,
    max_wait_seconds: int = 1800,
) -> dict[str, str]:
    result = optimize_copilot_prompts_via_nd(
        train_goldens,
        test_goldens,
        target_model_slugs=target_model_slugs,
        system_prompt=system_prompt,
        poll_seconds=poll_seconds,
        max_wait_seconds=max_wait_seconds,
    )
    return result.variant_prompts
