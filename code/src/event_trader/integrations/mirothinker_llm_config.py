"""Shared vendor-LLM config shape for committed MiroThinker runtimes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_DEFAULT_MAX_CONTEXT_LENGTH = 65536
_ALLOWED_REASONING_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


def normalize_mirothinker_reasoning_effort(
    value: object,
    *,
    field_name: str,
    error_type: Callable[[str], Exception] = ValueError,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise error_type(f"{field_name} must be a string.")
    normalized = value.strip().lower()
    if not normalized:
        raise error_type(f"{field_name} must not be blank.")
    if normalized not in _ALLOWED_REASONING_EFFORTS:
        allowed = ", ".join(_ALLOWED_REASONING_EFFORTS)
        raise error_type(f"{field_name} must be one of: {allowed}.")
    if normalized == "none":
        return None
    return normalized


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
    normalized_reasoning_effort = normalize_mirothinker_reasoning_effort(
        reasoning_effort,
        field_name="reasoning_effort",
    )
    if normalized_reasoning_effort is not None:
        llm_config["reasoning_effort"] = normalized_reasoning_effort
    return llm_config


__all__ = ["build_mirothinker_llm_config", "normalize_mirothinker_reasoning_effort"]
