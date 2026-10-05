"""Deterministic performance math for position monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from event_trader.contracts.view_state_change import (
    MarketDataSeries,
    ViewState,
    canonical_view_state_target_weight,
)
from event_trader.market.adjustments import (
    MarketAdjustmentSidecar,
    adjust_price_for_realized_policy,
    apply_adjustment_policy_to_series,
)
from event_trader.position_monitoring.contracts import canonical_state_for_weight


class PositionPerformanceError(ValueError):
    """Raised when position performance math cannot be computed."""


@dataclass(frozen=True, slots=True)
class TargetWeightEvent:
    effective_at: datetime
    target_weight: float
    state: ViewState
    execution_price: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.effective_at, datetime):
            raise PositionPerformanceError("effective_at must be a datetime.")
        if isinstance(self.target_weight, bool) or not isinstance(
            self.target_weight,
            int | float,
        ):
            raise PositionPerformanceError("target_weight must be numeric.")
        target_weight = float(self.target_weight)
        if not isfinite(target_weight):
            raise PositionPerformanceError("target_weight must be finite.")
        canonical_state = canonical_state_for_weight(target_weight)
        if canonical_state is None or canonical_state != self.state:
            raise PositionPerformanceError("target_weight must match the canonical state weight.")
        object.__setattr__(self, "target_weight", target_weight)
        if self.execution_price is not None:
            if isinstance(self.execution_price, bool) or not isinstance(
                self.execution_price,
                int | float,
            ):
                raise PositionPerformanceError("execution_price must be numeric.")
            execution_price = float(self.execution_price)
            if not isfinite(execution_price) or execution_price <= 0.0:
                raise PositionPerformanceError(
                    "execution_price must be finite and greater than zero."
                )
            object.__setattr__(self, "execution_price", execution_price)


@dataclass(frozen=True, slots=True)
class StrategyLinePoint:
    time: datetime
    value: float
    target_weight: float
    state: ViewState

    def __post_init__(self) -> None:
        if not isinstance(self.time, datetime):
            raise PositionPerformanceError("time must be a datetime.")
        if isinstance(self.value, bool) or not isinstance(self.value, int | float):
            raise PositionPerformanceError("value must be numeric.")
        value = float(self.value)
        if not isfinite(value) or value <= 0.0:
            raise PositionPerformanceError("value must be finite and greater than zero.")
        object.__setattr__(self, "value", value)
        if isinstance(self.target_weight, bool) or not isinstance(
            self.target_weight,
            int | float,
        ):
            raise PositionPerformanceError("target_weight must be numeric.")
        target_weight = float(self.target_weight)
        if not isfinite(target_weight):
            raise PositionPerformanceError("target_weight must be finite.")
        canonical_state = canonical_state_for_weight(target_weight)
        if canonical_state is None or canonical_state != self.state:
            raise PositionPerformanceError("target_weight must match the canonical state weight.")
        object.__setattr__(self, "target_weight", target_weight)


def calculate_cumulative_equity_series(
    *,
    market_data: MarketDataSeries,
    events: tuple[TargetWeightEvent, ...],
    base_value: float,
    default_state: ViewState = "flat",
    adjustment_sidecar: MarketAdjustmentSidecar | None = None,
) -> tuple[StrategyLinePoint, ...]:
    """Calculate cumulative equity from the first bar start through each bar boundary."""

    if not isinstance(market_data, MarketDataSeries):
        raise PositionPerformanceError("market_data must be a MarketDataSeries.")
    if isinstance(base_value, bool) or not isinstance(base_value, int | float):
        raise PositionPerformanceError("base_value must be numeric.")
    starting_value = float(base_value)
    if not isfinite(starting_value) or starting_value <= 0.0:
        raise PositionPerformanceError("base_value must be finite and greater than zero.")
    default_weight = canonical_view_state_target_weight(default_state)
    ordered_events = _normalize_events(events)
    series = (
        apply_adjustment_policy_to_series(series=market_data, sidecar=adjustment_sidecar).series
        if adjustment_sidecar is not None
        else market_data
    )
    bars = series.bars

    active_weight = default_weight
    active_state = default_state
    event_index = 0
    while (
        event_index < len(ordered_events)
        and ordered_events[event_index].effective_at < bars[0].start_at
    ):
        event = ordered_events[event_index]
        active_weight = event.target_weight
        active_state = event.state
        event_index += 1

    current_value = starting_value
    previous_close: float | None = None
    points: list[StrategyLinePoint] = [
        StrategyLinePoint(
            time=bars[0].start_at,
            value=current_value,
            target_weight=active_weight,
            state=active_state,
        )
    ]

    for bar in bars:
        intra_bar_price = bar.open_price
        if previous_close is not None:
            current_value *= 1.0 + active_weight * (bar.open_price / previous_close - 1.0)

        while (
            event_index < len(ordered_events)
            and ordered_events[event_index].effective_at <= bar.start_at
        ):
            event = ordered_events[event_index]
            event_price = (
                intra_bar_price
                if event.execution_price is None
                else (
                    adjust_price_for_realized_policy(
                        raw_price=event.execution_price,
                        timestamp=event.effective_at,
                        sidecar=adjustment_sidecar,
                    )
                    if adjustment_sidecar is not None
                    else event.execution_price
                )
            )
            current_value *= 1.0 + active_weight * (event_price / intra_bar_price - 1.0)
            intra_bar_price = event_price
            active_weight = event.target_weight
            active_state = event.state
            event_index += 1

        current_value *= 1.0 + active_weight * (bar.close_price / intra_bar_price - 1.0)
        previous_close = bar.close_price
        points.append(
            StrategyLinePoint(
                time=bar.end_at,
                value=current_value,
                target_weight=active_weight,
                state=active_state,
            )
        )

    return tuple(points)


def _normalize_events(events: tuple[TargetWeightEvent, ...]) -> tuple[TargetWeightEvent, ...]:
    if not isinstance(events, tuple):
        raise PositionPerformanceError("events must be a tuple.")
    by_effective_at: dict[datetime, TargetWeightEvent] = {}
    for event in events:
        if not isinstance(event, TargetWeightEvent):
            raise PositionPerformanceError("events must contain TargetWeightEvent values.")
        by_effective_at[event.effective_at] = event
    return tuple(
        event for _, event in sorted(by_effective_at.items(), key=lambda item: item[0])
    )
__all__ = [
    "PositionPerformanceError",
    "StrategyLinePoint",
    "TargetWeightEvent",
    "calculate_cumulative_equity_series",
]
