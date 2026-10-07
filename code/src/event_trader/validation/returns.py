"""Deterministic return math for validation segments and episodes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite, prod
from typing import Literal

from event_trader.contracts.view_state_change import (
    HorizonBaselineStatus,
    MarketDataBar,
    MarketDataSeries,
    PositionSegment,
    ViewEpisode,
    ViewStateChange,
)
from event_trader.execution.contracts import ExecutionRecord
from event_trader.market.adjustments import (
    MarketAdjustmentSidecar,
    adjust_price_for_realized_policy,
    apply_adjustment_policy_to_series,
)
from event_trader.validation.execution_linkage import execution_record_by_state_change


class ValidationReturnsError(ValueError):
    """Raised when deterministic validation return inputs are incomplete or invalid."""


@dataclass(frozen=True, slots=True)
class SegmentReturn:
    """Closed-segment return computed from first tradable entry/exit bars."""

    segment_id: str
    episode_id: str
    target_key: str
    entry_bar_start_at: datetime
    exit_bar_start_at: datetime
    entry_price: float
    exit_price: float
    target_weight: float
    underlying_return: float
    strategy_return: float


@dataclass(frozen=True, slots=True)
class EpisodeReturn:
    """Closed-episode return including compounded segment strategy return."""

    episode_id: str
    target_key: str
    direction: Literal["long", "short"]
    entry_bar_start_at: datetime
    exit_bar_start_at: datetime
    entry_price: float
    exit_price: float
    underlying_return: float
    strategy_return: float


@dataclass(frozen=True, slots=True)
class HorizonBaseline:
    """Standardized horizon baseline with explicit pending-market-data status."""

    episode_id: str
    target_key: str
    horizon: timedelta
    horizon_end_at: datetime
    status: HorizonBaselineStatus
    entry_bar_start_at: datetime
    exit_bar_start_at: datetime | None
    underlying_return: float | None
    strategy_return: float | None
    bar_count: int | None


@dataclass(frozen=True, slots=True)
class ValidationReturnsResult:
    """Validation return outputs for closed segments and episodes."""

    segment_returns: tuple[SegmentReturn, ...]
    episode_returns: tuple[EpisodeReturn, ...]
    horizon_baselines: tuple[HorizonBaseline, ...]


@dataclass(frozen=True, slots=True)
class OpenEpisodeTerminalMark:
    """Replay-only mark-to-market view for an episode still open at replay end."""

    episode_id: str
    target_key: str
    direction: Literal["long", "short"]
    replay_end_at: datetime
    entry_bar_start_at: datetime
    mark_bar_start_at: datetime
    entry_price: float
    mark_price: float
    underlying_return: float
    strategy_return: float
    target_weight: float


@dataclass(frozen=True, slots=True)
class MarkReturn:
    """Canonical underlying and weighted strategy return for one price mark."""

    underlying_return: float
    strategy_return: float


@dataclass(frozen=True, slots=True)
class _ExecutionPrice:
    timestamp: datetime
    price: float


def _realized_price(
    execution_price: _ExecutionPrice,
    adjustment_sidecar: MarketAdjustmentSidecar | None,
) -> float:
    if adjustment_sidecar is None:
        return execution_price.price
    return adjust_price_for_realized_policy(
        raw_price=execution_price.price,
        timestamp=execution_price.timestamp,
        sidecar=adjustment_sidecar,
    )


def calculate_mark_return(
    *,
    entry_price: float,
    mark_price: float,
    target_weight: float,
) -> MarkReturn:
    """Apply the project-owned validation return convention to one mark."""

    values = {
        "entry_price": entry_price,
        "mark_price": mark_price,
        "target_weight": target_weight,
    }
    normalized: dict[str, float] = {}
    for field_name, value in values.items():
        if isinstance(value, bool):
            raise ValidationReturnsError(f"{field_name} must be a finite number.")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationReturnsError(
                f"{field_name} must be a finite number."
            ) from exc
        if not isfinite(numeric):
            raise ValidationReturnsError(f"{field_name} must be a finite number.")
        normalized[field_name] = numeric
    if normalized["entry_price"] <= 0.0:
        raise ValidationReturnsError("entry_price must be greater than zero.")
    if normalized["mark_price"] <= 0.0:
        raise ValidationReturnsError("mark_price must be greater than zero.")
    underlying_return = normalized["mark_price"] / normalized["entry_price"] - 1.0
    return MarkReturn(
        underlying_return=underlying_return,
        strategy_return=normalized["target_weight"] * underlying_return,
    )


def calculate_returns(
    *,
    closed_segments: Sequence[PositionSegment],
    closed_episodes: Sequence[ViewEpisode],
    market_data: MarketDataSeries,
    horizons: Sequence[timedelta] = (),
    execution_records: Sequence[ExecutionRecord] = (),
    state_changes: Sequence[ViewStateChange] = (),
    require_execution_records: bool = False,
    adjustment_sidecar: MarketAdjustmentSidecar | None = None,
) -> ValidationReturnsResult:
    """Calculate deterministic returns and horizon baselines."""
    series = (
        apply_adjustment_policy_to_series(series=market_data, sidecar=adjustment_sidecar).series
        if adjustment_sidecar is not None
        else market_data
    )
    bars = series.bars
    execution_by_state_change = _latest_execution_records_by_state_change(
        state_changes=state_changes,
        execution_records=execution_records,
    )
    segment_returns: list[SegmentReturn] = []
    segment_returns_by_episode: dict[str, list[SegmentReturn]] = {}
    first_weight_by_episode: dict[str, float] = {}

    for segment in closed_segments:
        if segment.closed_at is None:
            raise ValidationReturnsError(
                "segment "
                f"'{segment.segment_id}' is open; calculate_returns requires closed segments."
            )
        entry_execution = _execution_price_for_state_change(
            state_change_id=segment.opened_by_state_change_id,
            execution_by_state_change=execution_by_state_change,
            require_execution_records=require_execution_records,
        )
        exit_execution = _execution_price_for_state_change(
            state_change_id=segment.closed_by_state_change_id,
            execution_by_state_change=execution_by_state_change,
            require_execution_records=require_execution_records,
        )
        entry = _find_first_bar_starting_at_or_after(
            bars=bars,
            timestamp=entry_execution.timestamp if entry_execution else segment.opened_at,
            missing_message=(
                f"Missing entry bar for closed segment '{segment.segment_id}' at "
                f"{segment.opened_at.isoformat()}."
            ),
        )
        exit_bar = _find_first_bar_starting_at_or_after(
            bars=bars,
            timestamp=exit_execution.timestamp if exit_execution else segment.closed_at,
            missing_message=(
                f"Missing exit bar for closed segment '{segment.segment_id}' at "
                f"{segment.closed_at.isoformat()}."
            ),
        )
        entry_price = (
            _realized_price(entry_execution, adjustment_sidecar)
            if entry_execution
            else entry.open_price
        )
        exit_price = (
            _realized_price(exit_execution, adjustment_sidecar)
            if exit_execution
            else exit_bar.open_price
        )
        mark_return = calculate_mark_return(
            entry_price=entry_price,
            mark_price=exit_price,
            target_weight=segment.target_weight,
        )
        calculated = SegmentReturn(
            segment_id=segment.segment_id,
            episode_id=segment.episode_id,
            target_key=segment.target_key,
            entry_bar_start_at=entry.start_at,
            exit_bar_start_at=exit_bar.start_at,
            entry_price=entry_price,
            exit_price=exit_price,
            target_weight=segment.target_weight,
            underlying_return=mark_return.underlying_return,
            strategy_return=mark_return.strategy_return,
        )
        segment_returns.append(calculated)
        segment_returns_by_episode.setdefault(segment.episode_id, []).append(calculated)
        first_weight_by_episode.setdefault(segment.episode_id, segment.target_weight)

    episode_returns: list[EpisodeReturn] = []
    horizon_baselines: list[HorizonBaseline] = []
    for episode in closed_episodes:
        if episode.closed_at is None:
            raise ValidationReturnsError(
                "episode "
                f"'{episode.episode_id}' is open; calculate_returns requires closed episodes."
            )
        entry_execution = _execution_price_for_state_change(
            state_change_id=episode.opened_by_state_change_id,
            execution_by_state_change=execution_by_state_change,
            require_execution_records=require_execution_records,
        )
        exit_execution = _execution_price_for_state_change(
            state_change_id=episode.closed_by_state_change_id,
            execution_by_state_change=execution_by_state_change,
            require_execution_records=require_execution_records,
        )
        entry = _find_first_bar_starting_at_or_after(
            bars=bars,
            timestamp=entry_execution.timestamp if entry_execution else episode.opened_at,
            missing_message=(
                f"Missing entry bar for closed episode '{episode.episode_id}' at "
                f"{episode.opened_at.isoformat()}."
            ),
        )
        exit_bar = _find_first_bar_starting_at_or_after(
            bars=bars,
            timestamp=exit_execution.timestamp if exit_execution else episode.closed_at,
            missing_message=(
                f"Missing exit bar for closed episode '{episode.episode_id}' at "
                f"{episode.closed_at.isoformat()}."
            ),
        )
        segment_results = segment_returns_by_episode.get(episode.episode_id)
        if not segment_results:
            raise ValidationReturnsError(
                f"Closed episode '{episode.episode_id}' has no closed segments for compounding."
            )
        compounded_strategy_return = (
            prod(
                (1.0 + item.strategy_return for item in segment_results),
                start=1.0,
            )
            - 1.0
        )
        entry_price = (
            _realized_price(entry_execution, adjustment_sidecar)
            if entry_execution
            else entry.open_price
        )
        exit_price = (
            _realized_price(exit_execution, adjustment_sidecar)
            if exit_execution
            else exit_bar.open_price
        )
        underlying_return = exit_price / entry_price - 1.0
        episode_returns.append(
            EpisodeReturn(
                episode_id=episode.episode_id,
                target_key=episode.target_key,
                direction=episode.direction,
                entry_bar_start_at=entry.start_at,
                exit_bar_start_at=exit_bar.start_at,
                entry_price=entry_price,
                exit_price=exit_price,
                underlying_return=underlying_return,
                strategy_return=compounded_strategy_return,
            )
        )

        opening_weight = first_weight_by_episode.get(episode.episode_id)
        if opening_weight is None:
            raise ValidationReturnsError(
                f"Closed episode '{episode.episode_id}' is missing opening segment weight."
            )
        for horizon in horizons:
            horizon_baselines.append(
                _calculate_horizon_baseline(
                    episode=episode,
                    bars=bars,
                    entry=entry,
                    entry_price=entry_price,
                    opening_weight=opening_weight,
                    horizon=horizon,
                )
            )

    return ValidationReturnsResult(
        segment_returns=tuple(segment_returns),
        episode_returns=tuple(episode_returns),
        horizon_baselines=tuple(horizon_baselines),
    )


def calculate_open_episode_terminal_mark(
    *,
    open_segment: PositionSegment,
    open_episode: ViewEpisode,
    market_data: MarketDataSeries,
    replay_end_at: datetime,
    execution_records: Sequence[ExecutionRecord] = (),
    state_changes: Sequence[ViewStateChange] = (),
    require_execution_records: bool = False,
    adjustment_sidecar: MarketAdjustmentSidecar | None = None,
) -> OpenEpisodeTerminalMark:
    """Calculate replay-only terminal mark-to-market for one still-open episode."""
    if open_segment.closed_at is not None:
        raise ValidationReturnsError(
            "open_segment must be open for terminal mark-to-market evaluation."
        )
    if open_episode.closed_at is not None:
        raise ValidationReturnsError(
            "open_episode must be open for terminal mark-to-market evaluation."
        )
    if open_segment.episode_id != open_episode.episode_id:
        raise ValidationReturnsError(
            "open_segment.episode_id must match open_episode.episode_id."
        )
    if open_segment.target_key != open_episode.target_key:
        raise ValidationReturnsError(
            "open_segment.target_key must match open_episode.target_key."
        )
    if open_segment.opened_at < open_episode.opened_at:
        raise ValidationReturnsError(
            "open_segment.opened_at must be greater than or equal to open_episode.opened_at."
        )
    validated_replay_end_at = replay_end_at
    if validated_replay_end_at < open_segment.opened_at:
        raise ValidationReturnsError(
            "replay_end_at must be greater than or equal to open_segment.opened_at."
        )

    series = (
        apply_adjustment_policy_to_series(series=market_data, sidecar=adjustment_sidecar).series
        if adjustment_sidecar is not None
        else market_data
    )
    bars = series.bars
    entry_execution = _execution_price_for_state_change(
        state_change_id=open_segment.opened_by_state_change_id,
        execution_by_state_change=_latest_execution_records_by_state_change(
            state_changes=state_changes,
            execution_records=execution_records,
        ),
        require_execution_records=require_execution_records,
    )
    entry = _find_first_bar_starting_at_or_after(
        bars=bars,
        timestamp=entry_execution.timestamp if entry_execution else open_segment.opened_at,
        missing_message=(
            f"Missing entry bar for open segment '{open_segment.segment_id}' at "
            f"{open_segment.opened_at.isoformat()}."
        ),
    )
    mark_bar = _find_terminal_mark_bar(
        bars=bars,
        timestamp=validated_replay_end_at,
        missing_message=(
            "Missing terminal mark bar for replay terminal mark at "
            f"{validated_replay_end_at.isoformat()}."
        ),
    )
    entry_price = (
        _realized_price(entry_execution, adjustment_sidecar)
        if entry_execution
        else entry.open_price
    )
    mark_return = calculate_mark_return(
        entry_price=entry_price,
        mark_price=mark_bar.close_price,
        target_weight=open_segment.target_weight,
    )
    return OpenEpisodeTerminalMark(
        episode_id=open_episode.episode_id,
        target_key=open_episode.target_key,
        direction=open_episode.direction,
        replay_end_at=validated_replay_end_at,
        entry_bar_start_at=entry.start_at,
        mark_bar_start_at=mark_bar.start_at,
        entry_price=entry_price,
        mark_price=mark_bar.close_price,
        underlying_return=mark_return.underlying_return,
        strategy_return=mark_return.strategy_return,
        target_weight=open_segment.target_weight,
    )


def _find_first_bar_starting_at_or_after(
    *,
    bars: tuple[MarketDataBar, ...],
    timestamp: datetime,
    missing_message: str,
) -> MarketDataBar:
    for bar in bars:
        if bar.start_at >= timestamp:
            return bar
    raise ValidationReturnsError(missing_message)


def _calculate_horizon_baseline(
    *,
    episode: ViewEpisode,
    bars: tuple[MarketDataBar, ...],
    entry: MarketDataBar,
    entry_price: float,
    opening_weight: float,
    horizon: timedelta,
) -> HorizonBaseline:
    if horizon <= timedelta(0):
        raise ValidationReturnsError("horizon values must be greater than zero.")

    horizon_end_at = episode.opened_at + horizon
    entry_index = _find_bar_index(bars=bars, bar=entry)
    covering_bar_index = _find_covering_bar_index(
        bars=bars,
        timestamp=horizon_end_at,
    )
    if covering_bar_index is None:
        return HorizonBaseline(
            episode_id=episode.episode_id,
            target_key=episode.target_key,
            horizon=horizon,
            horizon_end_at=horizon_end_at,
            status="pending_market_data",
            entry_bar_start_at=entry.start_at,
            exit_bar_start_at=None,
            underlying_return=None,
            strategy_return=None,
            bar_count=None,
        )

    exit_bar = bars[covering_bar_index]
    mark_return = calculate_mark_return(
        entry_price=entry_price,
        mark_price=exit_bar.close_price,
        target_weight=opening_weight,
    )
    return HorizonBaseline(
        episode_id=episode.episode_id,
        target_key=episode.target_key,
        horizon=horizon,
        horizon_end_at=horizon_end_at,
        status="final",
        entry_bar_start_at=entry.start_at,
        exit_bar_start_at=exit_bar.start_at,
        underlying_return=mark_return.underlying_return,
        strategy_return=mark_return.strategy_return,
        bar_count=covering_bar_index - entry_index + 1,
    )


def _find_bar_index(*, bars: tuple[MarketDataBar, ...], bar: MarketDataBar) -> int:
    for index, value in enumerate(bars):
        if value == bar:
            return index
    raise ValidationReturnsError("Internal error: could not locate entry bar index.")


def _find_covering_bar_index(
    *,
    bars: tuple[MarketDataBar, ...],
    timestamp: datetime,
) -> int | None:
    for index, bar in enumerate(bars):
        if bar.start_at <= timestamp < bar.end_at:
            return index
    return None


def _find_covering_bar(
    *,
    bars: tuple[MarketDataBar, ...],
    timestamp: datetime,
    missing_message: str,
) -> MarketDataBar:
    index = _find_covering_bar_index(bars=bars, timestamp=timestamp)
    if index is None:
        raise ValidationReturnsError(missing_message)
    return bars[index]


def _find_terminal_mark_bar(
    *,
    bars: tuple[MarketDataBar, ...],
    timestamp: datetime,
    missing_message: str,
) -> MarketDataBar:
    covering_index = _find_covering_bar_index(bars=bars, timestamp=timestamp)
    if covering_index is not None:
        return bars[covering_index]

    latest_prior_bar: MarketDataBar | None = None
    for bar in bars:
        if bar.end_at <= timestamp:
            latest_prior_bar = bar
            continue
        if bar.start_at > timestamp:
            break
    if latest_prior_bar is not None:
        return latest_prior_bar
    raise ValidationReturnsError(missing_message)


def _latest_execution_records_by_state_change(
    *,
    state_changes: Sequence[ViewStateChange] = (),
    execution_records: Sequence[ExecutionRecord],
) -> dict[str, ExecutionRecord]:
    if state_changes:
        target_keys = {state_change.target_key for state_change in state_changes}
        latest: dict[str, ExecutionRecord] = {}
        for target_key in sorted(target_keys):
            latest.update(
                execution_record_by_state_change(
                    target_key=target_key,
                    state_changes=state_changes,
                    execution_records=execution_records,
                )
            )
        return latest
    if execution_records:
        raise ValidationReturnsError(
            "execution_records require sidecar state_changes for linkage."
        )
    return {}


def _execution_price_for_state_change(
    *,
    state_change_id: str | None,
    execution_by_state_change: dict[str, ExecutionRecord],
    require_execution_records: bool,
) -> _ExecutionPrice | None:
    if state_change_id is None:
        return None
    record = execution_by_state_change.get(state_change_id)
    if record is None:
        if require_execution_records:
            raise ValidationReturnsError(
                f"Missing execution record for state_change_id {state_change_id!r}."
            )
        return None
    if record.status != "executed":
        if require_execution_records:
            raise ValidationReturnsError(
                f"Execution record for state_change_id {state_change_id!r} was not executed."
            )
        return None
    if record.executed_at is None or record.adjusted_price is None:
        raise ValidationReturnsError(
            "Executed record is missing executed_at or adjusted_price."
        )
    return _ExecutionPrice(timestamp=record.executed_at, price=record.adjusted_price)


__all__ = [
    "EpisodeReturn",
    "HorizonBaseline",
    "OpenEpisodeTerminalMark",
    "SegmentReturn",
    "ValidationReturnsError",
    "ValidationReturnsResult",
    "calculate_open_episode_terminal_mark",
    "calculate_returns",
]

