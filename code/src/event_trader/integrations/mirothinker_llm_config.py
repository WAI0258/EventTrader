"""Shared vendor-LLM config shape for committed MiroThinker runtimes."""

from __future__ import annotations

from typing import Any

from event_trader.reasoning.effort import normalize_agent_reasoning_effort

_DEFAULT_MAX_CONTEXT_LENGTH = 65536


def build_mirothinker_llm_config(
    *,
    provider: str,
    model_name: str,
    api_key: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    max_context_length: int = _DEFAULT_MAX_CONTEXT_LENGTH,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Return the full llm config shape expected by vendored MiroThinker."""

    llm_config = {
        "provider": provider,
        "model_name": model_name,
        "async_client": False,
        "temperature": temperature,
        "top_p": 1.0,
        "min_p": 0.0,
        "top_k": -1,
        "max_context_length": max_context_length,
        "max_tokens": max_tokens,
        "api_key": api_key,
        "base_url": base_url,
        "repetition_penalty": 1.0,
    }
    normalized_reasoning_effort = normalize_agent_reasoning_effort(
        reasoning_effort,
        field_name="reasoning_effort",
    )
    if normalized_reasoning_effort is not None:
        llm_config["reasoning_effort"] = normalized_reasoning_effort
    return llm_config


__all__ = ["build_mirothinker_llm_config"]
