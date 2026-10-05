"""Small shared helpers for runtime analysis scheduling semantics."""

from __future__ import annotations

from datetime import datetime, timedelta


class RuntimeAnalysisHelperError(ValueError):
    """Raised when runtime analysis helper inputs are invalid."""


def stream_routing_deadline_at(
    *,
    business_at: datetime,
    max_delay_bars: int,
    bar_granularity: str,
) -> datetime:
    """Match composition's stream-routing deadline semantics for one emitted unit."""
    if not isinstance(max_delay_bars, int) or isinstance(max_delay_bars, bool):
        raise RuntimeAnalysisHelperError("max_delay_bars must be an integer.")
    if max_delay_bars < 0:
        raise RuntimeAnalysisHelperError("max_delay_bars must be non-negative.")
    if not isinstance(bar_granularity, str) or not bar_granularity.strip():
        raise RuntimeAnalysisHelperError("bar_granularity must be a non-blank string.")
    if len(bar_granularity) < 2:
        raise RuntimeAnalysisHelperError(
            "stream_routing.bar_granularity must match '<N>m', '<N>min', '<N>h', or '<N>d'."
        )
    normalized = bar_granularity.strip().lower()
    count_text = normalized[:-1]
    unit = normalized[-1]
    if normalized.endswith("min"):
        count_text = normalized[:-3]
        unit = "min"
    try:
        count = int(count_text)
    except ValueError as exc:
        raise RuntimeAnalysisHelperError(
            "stream_routing.bar_granularity must match '<N>m', '<N>min', '<N>h', or '<N>d'."
        ) from exc
    if count < 1:
        raise RuntimeAnalysisHelperError("stream_routing.bar_granularity count must be >= 1.")
    if unit in {"m", "min"}:
        delta = timedelta(minutes=count)
    elif unit == "h":
        delta = timedelta(hours=count)
    elif unit == "d":
        delta = timedelta(days=count)
    else:
        raise RuntimeAnalysisHelperError(
            "stream_routing.bar_granularity must match '<N>m', '<N>min', '<N>h', or '<N>d'."
        )
    return business_at + (delta * max_delay_bars)


__all__ = [
    "RuntimeAnalysisHelperError",
    "stream_routing_deadline_at",
]
