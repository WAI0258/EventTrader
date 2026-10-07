"""Shared reasoning-effort configuration for agent implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

_ALLOWED_REASONING_EFFORTS: Final[tuple[str, ...]] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


def normalize_agent_reasoning_effort(
    value: object,
    *,
    field_name: str,
    error_type: Callable[[str], Exception] = ValueError,
) -> str | None:
    """Normalize the shared reasoning-effort vocabulary used by agent configs."""

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


__all__ = ["normalize_agent_reasoning_effort"]
