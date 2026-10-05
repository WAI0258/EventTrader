"""Convert executed replay truth into daily marked performance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, cast

from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    ViewState,
    ViewStateChange,
)
from event_trader.evaluation.baselines import DailyStrategyPoint
from event_trader.execution.contracts import ExecutionRecord
from event_trader.market.adjustments import (
    MarketAdjustmentSidecar,
    adjust_price_for_realized_policy,
    apply_adjustment_policy_to_series,
)
from event_trader.validation.execution_linkage import execution_record_by_state_change


class ReplayPerformanceError(ValueError):
    """Raised when replay artifacts cannot produce strict performance results."""


@dataclass(frozen=True, slots=True)
class _ExecutionEvent:
    executed_at: datetime
    adjusted_price: float
    target_weight: float
    state_change_id: str


def calculate_replay_daily_performance(
    *,
    state_changes: tuple[ViewStateChange, ...],
    execution_records: tuple[ExecutionRecord, ...],
    market_data: MarketDataSeries,
    adjustment_sidecar: MarketAdjustmentSidecar | None = None,
) -> tuple[DailyStrategyPoint, ...]:
    """Calculate daily mark-to-market performance from executed replay records."""
    if not state_changes:
        raise ReplayPerformanceError("state_changes must not be empty.")
    if not isinstance(market_data, MarketDataSeries):
        raise ReplayPerformanceError("market_data must be a MarketDataSeries.")

    ordered_state_changes = tuple(sorted(state_changes, key=lambda item: item.effective_at))
    execution_events = _strict_execution_events(
        state_changes=ordered_state_changes,
        execution_records=execution_records,
        adjustment_sidecar=adjustment_sidecar,
    )
    series = (
        apply_adjustment_policy_to_series(
            series=market_data,
            sidecar=adjustment_sidecar,
        ).series
        if adjustment_sidecar is not None
        else market_data
    )
    daily_marks = tuple(
        mark for mark in _daily_close_marks(series)
        if mark[0] >= execution_events[0].executed_at.date()
    )
    if not daily_marks:
        raise ReplayPerformanceError(
            "market_data must contain at least one daily mark at or after first execution."
        )

    active_weight = 0.0
    previous_price: float | None = None
    underlying_equity = 1.0
    strategy_equity = 1.0
    points: list[DailyStrategyPoint] = []
    execution_index = 0

    for mark_date, mark_at, close_price, open_price in daily_marks:
        day_start_strategy_equity = strategy_equity
        day_start_underlying_equity = underlying_equity
        day_start_price = previous_price if previous_price is not None else open_price

        while (
            execution_index < len(execution_events)
            and execution_events[execution_index].executed_at <= mark_at
        ):
            event = execution_events[execution_index]
            if previous_price is not None:
                interval_return = event.adjusted_price / previous_price - 1.0
                underlying_equity *= 1.0 + interval_return
                strategy_equity *= 1.0 + active_weight * interval_return
            active_weight = event.target_weight
            previous_price = event.adjusted_price
            execution_index += 1

        if previous_price is None:
            previous_price = close_price
        else:
            interval_return = close_price / previous_price - 1.0
            underlying_equity *= 1.0 + interval_return
            strategy_equity *= 1.0 + active_weight * interval_return
            previous_price = close_price

        points.append(
            DailyStrategyPoint(
                date=mark_date,
                state=_state_from_weight(active_weight),
                target_weight=active_weight,
                open_price=day_start_price,
                close_price=close_price,
                underlying_return=underlying_equity / day_start_underlying_equity - 1.0,
                strategy_return=strategy_equity / day_start_strategy_equity - 1.0,
                cumulative_underlying_equity=underlying_equity,
                cumulative_strategy_equity=strategy_equity,
            )
        )

    return tuple(points)


def _strict_execution_events(
    *,
    state_changes: tuple[ViewStateChange, ...],
    execution_records: tuple[ExecutionRecord, ...],
    adjustment_sidecar: MarketAdjustmentSidecar | None,
) -> tuple[_ExecutionEvent, ...]:
    records_by_state_change: dict[str, ExecutionRecord] = {}
    for target_key in sorted({state_change.target_key for state_change in state_changes}):
        records_by_state_change.update(
            execution_record_by_state_change(
                target_key=target_key,
                state_changes=state_changes,
                execution_records=execution_records,
            )
        )

    events: list[_ExecutionEvent] = []
    for state_change in state_changes:
        matched_record = records_by_state_change.get(state_change.state_change_id)
        if matched_record is None:
            raise ReplayPerformanceError(
                "missing execution record for state_change_id="
                f"{state_change.state_change_id!r}."
            )
        if matched_record.status != "executed":
            raise ReplayPerformanceError(
                "execution record is not executed for state_change_id="
                f"{state_change.state_change_id!r}."
            )
        if (
            matched_record.executed_at is None
            or matched_record.adjusted_price is None
            or matched_record.target_weight is None
        ):
            raise ReplayPerformanceError(
                "executed record is missing executed_at, adjusted_price, or target_weight "
                f"for state_change_id={state_change.state_change_id!r}."
            )
        if matched_record.target_weight != state_change.target_weight:
            raise ReplayPerformanceError(
                "execution target_weight does not match state change for "
                f"state_change_id={state_change.state_change_id!r}."
            )
        events.append(
            _ExecutionEvent(
                executed_at=matched_record.executed_at,
                adjusted_price=(
                    adjust_price_for_realized_policy(
                        raw_price=matched_record.adjusted_price,
                        timestamp=matched_record.executed_at,
                        sidecar=adjustment_sidecar,
                    )
                    if adjustment_sidecar is not None
                    else matched_record.adjusted_price
                ),
                target_weight=matched_record.target_weight,
                state_change_id=state_change.state_change_id,
            )
        )
    return tuple(sorted(events, key=lambda item: item.executed_at))


def _daily_close_marks(
    market_data: MarketDataSeries,
) -> tuple[tuple[date, datetime, float, float], ...]:
    by_date: dict[date, list[MarketDataBar]] = {}
    for bar in market_data.bars:
        by_date.setdefault(bar.start_at.date(), []).append(bar)
    marks: list[tuple[date, datetime, float, float]] = []
    for mark_date, bars in sorted(by_date.items(), key=lambda item: item[0]):
        ordered = tuple(sorted(bars, key=lambda item: item.start_at))
        marks.append(
            (
                mark_date,
                ordered[-1].end_at,
                ordered[-1].close_price,
                ordered[0].open_price,
            )
        )
    return tuple(marks)


def _state_from_weight(weight: float) -> ViewState:
    if weight == 0.0:
        return "flat"
    direction: Literal["long", "short"] = "long" if weight > 0.0 else "short"
    conviction = "strong" if abs(weight) >= 1.0 else "weak"
    return cast(ViewState, f"{conviction}_{direction}")
