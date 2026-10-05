"""Config-driven reasoning parameter mapping for LLM transports."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

_ALLOWED_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
}


def normalize_reasoning_effort(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("llm.reasoning_effort must be a string.")
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("llm.reasoning_effort must not be blank.")
    if normalized not in _ALLOWED_REASONING_EFFORTS:
        allowed = ", ".join(sorted(_ALLOWED_REASONING_EFFORTS))
        raise ValueError(f"llm.reasoning_effort must be one of: {allowed}.")
    if normalized == "none":
        return None
    return normalized


def build_openai_reasoning_params(
    *,
    reasoning_effort: str | None,
    base_url: str | None,
    model_name: str,
) -> dict[str, Any]:
    normalized_effort = normalize_reasoning_effort(reasoning_effort)
    if normalized_effort is None:
        return {}
    if is_deepseek_compatible_endpoint(base_url=base_url, model_name=model_name):
        return {
            "reasoning_effort": _deepseek_reasoning_effort(normalized_effort),
            "extra_body": {"thinking": {"type": "enabled"}},
        }
    if normalized_effort == "max":
        raise ValueError(
            "OpenAI native reasoning_effort does not support max; use xhigh."
        )
    return {"reasoning_effort": normalized_effort}


def build_anthropic_reasoning_params(
    *,
    reasoning_effort: str | None,
    base_url: str | None,
    model_name: str,
) -> dict[str, Any]:
    normalized_effort = normalize_reasoning_effort(reasoning_effort)
    if normalized_effort is None:
        return {}
    if is_deepseek_compatible_endpoint(base_url=base_url, model_name=model_name):
        return {"output_config": {"effort": _deepseek_reasoning_effort(normalized_effort)}}
    raise ValueError(
        "Anthropic native extended thinking requires explicit budget_tokens; "
        "llm_reasoning_budget_tokens is not configured."
    )


def is_deepseek_compatible_endpoint(*, base_url: str | None, model_name: str) -> bool:
    if model_name.strip().lower().startswith("deepseek-"):
        return True
    if not base_url:
        return False
    host = urlsplit(base_url.strip()).netloc.lower()
    return host == "api.deepseek.com"


def _deepseek_reasoning_effort(value: str) -> str:
    if value == "minimal":
        raise ValueError(
            "DeepSeek reasoning_effort does not support minimal; use low, medium, "
            "high, xhigh, or max."
        )
    if value in {"low", "medium", "high"}:
        return "high"
    return "max"
