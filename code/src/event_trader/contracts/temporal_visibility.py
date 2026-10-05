"""Shared decision-time visibility boundary contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._validators import validate_timestamp


class DecisionVisibilityBoundaryError(ValueError):
    """Raised when a decision-time visibility boundary is malformed."""


@dataclass(frozen=True, slots=True)
class DecisionVisibilityBoundary:
    """Business-time boundary for decision-visible event and market surfaces."""

    business_at: datetime
    max_visible_event_time: datetime
    max_visible_market_time: datetime

    def __post_init__(self) -> None:
        business_at = validate_timestamp(
            self.business_at,
            field_name="business_at",
            error_type=DecisionVisibilityBoundaryError,
        )
        max_visible_event_time = validate_timestamp(
            self.max_visible_event_time,
            field_name="max_visible_event_time",
            error_type=DecisionVisibilityBoundaryError,
        )
        max_visible_market_time = validate_timestamp(
            self.max_visible_market_time,
            field_name="max_visible_market_time",
            error_type=DecisionVisibilityBoundaryError,
        )
        if max_visible_event_time > business_at:
            raise DecisionVisibilityBoundaryError(
                "max_visible_event_time must not be after business_at."
            )
        if max_visible_market_time > business_at:
            raise DecisionVisibilityBoundaryError(
                "max_visible_market_time must not be after business_at."
            )
        object.__setattr__(self, "business_at", business_at)
        object.__setattr__(self, "max_visible_event_time", max_visible_event_time)
        object.__setattr__(self, "max_visible_market_time", max_visible_market_time)

    @classmethod
    def at_business_time(cls, business_at: datetime) -> "DecisionVisibilityBoundary":
        """Build the analysis-style boundary where all visibility collapses to business_at."""

        return cls(
            business_at=business_at,
            max_visible_event_time=business_at,
            max_visible_market_time=business_at,
        )


__all__ = [
    "DecisionVisibilityBoundary",
    "DecisionVisibilityBoundaryError",
]
