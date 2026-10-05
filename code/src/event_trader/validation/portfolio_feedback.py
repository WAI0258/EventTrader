"""Deterministic portfolio validation facts for reflection input."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite, prod
from statistics import mean
from typing import Literal

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.contracts.view_state_change import (
    MarketDataSeries,
    MarketMapping,
    PositionSegment,
    ViewState,
    ViewStateChange,
)
from event_trader.execution.contracts import ExecutionRecord
from event_trader.market.adjustments import (
    MarketAdjustmentSidecar,
    adjust_price_for_realized_policy,
)
from event_trader.validation.execution_linkage import execution_record_by_state_change
from event_trader.validation.episode_builder import build_episodes
from event_trader.validation.window_performance import (
    ValidationWindowBarReturn,
    ValidationWindowPerformanceError,
    calculate_window_performance,
)

DecisionTimingLabel = Literal[
    "before_drawdown",
    "after_drawdown",
    "before_rally",
    "after_rally",
    "unclear",
]
ExposureChange = Literal["increased", "decreased", "unchanged"]
ReturnWindowStatus = Literal["final", "pending_market_data"]
SegmentStatus = Literal["final", "pending_market_data"]

_CONTINUOUS_SESSION_DAILY_BAR_MINUTES = 365.0 * 24.0 * 60.0
_REGULAR_EXCHANGE_SESSION_DAILY_BAR_MINUTES = 252.0 * 390.0
_EXTENDED_EXCHANGE_SESSION_DAILY_BAR_MINUTES = 252.0 * 960.0


class PortfolioFeedbackError(ValueError):
    """Raised when deterministic portfolio feedback cannot be built."""


@dataclass(frozen=True, slots=True)
class PortfolioFeedbackConfig:
    """Explicit thresholds and horizons for deterministic feedback labels."""

    annual_periods: float
    pre_decision_bars: int = 2
    post_decision_bars: tuple[int, ...] = (1, 2)
    material_positive_return: float = 0.01
    material_negative_return: float = -0.01
    rolling_sharpe_bars: int = 20

    def __post_init__(self) -> None:
        if not isfinite(self.annual_periods) or self.annual_periods <= 0.0:
            raise PortfolioFeedbackError("annual_periods must be finite and greater than zero.")
        if self.pre_decision_bars <= 0:
            raise PortfolioFeedbackError("pre_decision_bars must be greater than zero.")
        if not self.post_decision_bars:
            raise PortfolioFeedbackError("post_decision_bars must not be empty.")
        if any(value <= 0 for value in self.post_decision_bars):
            raise PortfolioFeedbackError("post_decision_bars values must be greater than zero.")
        if self.material_positive_return <= 0.0:
            raise PortfolioFeedbackError("material_positive_return must be greater than zero.")
        if self.material_negative_return >= 0.0:
            raise PortfolioFeedbackError("material_negative_return must be less than zero.")
        if self.rolling_sharpe_bars <= 1:
            raise PortfolioFeedbackError("rolling_sharpe_bars must be greater than one.")
        object.__setattr__(
            self,
            "post_decision_bars",
            tuple(sorted(set(self.post_decision_bars))),
        )


@dataclass(frozen=True, slots=True)
class ReturnWindowFact:
    """A deterministic underlying return window around a state-change."""

    status: ReturnWindowStatus
    bar_count: int
    requested_bar_count: int
    window_start: datetime | None
    window_end: datetime | None
    underlying_return: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "bar_count": self.bar_count,
            "requested_bar_count": self.requested_bar_count,
            "window_start": _format_optional_datetime(self.window_start),
            "window_end": _format_optional_datetime(self.window_end),
            "underlying_return": self.underlying_return,
        }


@dataclass(frozen=True, slots=True)
class PortfolioFacts:
    """Portfolio-level deterministic return and exposure facts."""

    benchmark_return: float
    strategy_return: float
    active_return: float
    strategy_max_drawdown: float
    benchmark_max_drawdown: float
    average_exposure: float
    time_in_market: float
    turnover: float
    bar_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "benchmark_return": self.benchmark_return,
            "strategy_return": self.strategy_return,
            "active_return": self.active_return,
            "strategy_max_drawdown": self.strategy_max_drawdown,
            "benchmark_max_drawdown": self.benchmark_max_drawdown,
            "average_exposure": self.average_exposure,
            "time_in_market": self.time_in_market,
            "turnover": self.turnover,
            "bar_count": self.bar_count,
        }


@dataclass(frozen=True, slots=True)
class SegmentFact:
    """Deterministic exposure segment facts for reflection review."""

    segment_id: str
    state: ViewState
    target_weight: float
    opened_at: datetime
    closed_at: datetime | None
    entry_bar_start_at: datetime | None
    exit_bar_start_at: datetime | None
    entry_price: float | None
    exit_price: float | None
    underlying_return: float | None
    strategy_return: float | None
    exposure_shortfall: float | None
    status: SegmentStatus

    def to_dict(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "state": self.state,
            "target_weight": self.target_weight,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": _format_optional_datetime(self.closed_at),
            "entry_bar_start_at": _format_optional_datetime(self.entry_bar_start_at),
            "exit_bar_start_at": _format_optional_datetime(self.exit_bar_start_at),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "underlying_return": self.underlying_return,
            "strategy_return": self.strategy_return,
            "exposure_shortfall": self.exposure_shortfall,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class DecisionFact:
    """Deterministic state-change timing and exposure-change facts."""

    state_change_id: str
    effective_at: datetime
    previous_state: ViewState
    new_state: ViewState
    previous_target_weight: float
    new_target_weight: float
    source_event_ids: tuple[str, ...]
    pre_decision_return_window: ReturnWindowFact
    post_decision_return_windows: tuple[ReturnWindowFact, ...]
    exposure_change: ExposureChange
    timing_label: DecisionTimingLabel

    def to_dict(self) -> dict[str, object]:
        return {
            "state_change_id": self.state_change_id,
            "effective_at": self.effective_at.isoformat(),
            "previous_state": self.previous_state,
            "new_state": self.new_state,
            "previous_target_weight": self.previous_target_weight,
            "new_target_weight": self.new_target_weight,
            "source_event_ids": list(self.source_event_ids),
            "pre_decision_return_window": self.pre_decision_return_window.to_dict(),
            "post_decision_return_windows": [
                item.to_dict() for item in self.post_decision_return_windows
            ],
            "exposure_change": self.exposure_change,
            "timing_label": self.timing_label,
        }


@dataclass(frozen=True, slots=True)
class ExposurePathFact:
    """One bar-level exposure and return fact reused from window performance."""

    target_key: str
    state: ViewState
    target_weight: float
    interval_start_at: datetime
    interval_end_at: datetime
    mark_mode: str
    entry_price: float
    exit_price: float
    underlying_return: float
    strategy_return: float
    cumulative_benchmark_equity: float
    cumulative_strategy_equity: float

    def to_dict(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "state": self.state,
            "target_weight": self.target_weight,
            "interval_start_at": self.interval_start_at.isoformat(),
            "interval_end_at": self.interval_end_at.isoformat(),
            "mark_mode": self.mark_mode,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "underlying_return": self.underlying_return,
            "strategy_return": self.strategy_return,
            "cumulative_benchmark_equity": self.cumulative_benchmark_equity,
            "cumulative_strategy_equity": self.cumulative_strategy_equity,
        }


@dataclass(frozen=True, slots=True)
class PortfolioFeedbackSnapshot:
    """Business-facing validation facts consumed by reflection."""

    target_key: str
    run_id: str
    window_start: datetime
    window_end: datetime
    config: PortfolioFeedbackConfig
    portfolio_facts: PortfolioFacts
    segment_facts: tuple[SegmentFact, ...]
    decision_facts: tuple[DecisionFact, ...]
    exposure_path: tuple[ExposurePathFact, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "run_id": self.run_id,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "config": {
                "annual_periods": self.config.annual_periods,
                "pre_decision_bars": self.config.pre_decision_bars,
                "post_decision_bars": list(self.config.post_decision_bars),
                "material_positive_return": self.config.material_positive_return,
                "material_negative_return": self.config.material_negative_return,
                "rolling_sharpe_bars": self.config.rolling_sharpe_bars,
            },
            "portfolio_facts": self.portfolio_facts.to_dict(),
            "segment_count": len(self.segment_facts),
            "decision_count": len(self.decision_facts),
            "exposure_path_count": len(self.exposure_path),
        }


def build_portfolio_feedback_snapshot(
    *,
    target_key: str,
    run_id: str,
    state_changes: tuple[ViewStateChange, ...],
    market_data: MarketDataSeries,
    window_start: datetime,
    window_end: datetime,
    config: PortfolioFeedbackConfig,
    execution_records: tuple[ExecutionRecord, ...] = (),
    require_execution_records: bool = False,
    adjustment_sidecar: MarketAdjustmentSidecar | None = None,
) -> PortfolioFeedbackSnapshot:
    """Build deterministic portfolio feedback facts from canonical validation inputs."""
    validated_target_key = validate_target_key(
        target_key,
        error_type=PortfolioFeedbackError,
    )
    validated_run_id = _validate_run_id(run_id)
    validated_window_start = validate_timestamp(
        window_start,
        field_name="window_start",
        error_type=PortfolioFeedbackError,
    )
    validated_window_end = validate_timestamp(
        window_end,
        field_name="window_end",
        error_type=PortfolioFeedbackError,
    )
    if validated_window_end < validated_window_start:
        raise PortfolioFeedbackError("window_end must be greater than or equal to window_start.")
    if not isinstance(config, PortfolioFeedbackConfig):
        raise PortfolioFeedbackError("config must be a PortfolioFeedbackConfig instance.")
    if not isinstance(market_data, MarketDataSeries):
        raise PortfolioFeedbackError("market_data must be a MarketDataSeries instance.")

    target_state_changes = tuple(
        sorted(
            (
                item
                for item in state_changes
                if item.target_key == validated_target_key
                and item.effective_at <= validated_window_end
            ),
            key=lambda item: item.effective_at,
        )
    )
    if not target_state_changes:
        raise PortfolioFeedbackError(
            "portfolio feedback requires canonical state-changes at or before window_end."
        )

    try:
        performance = calculate_window_performance(
            target_key=validated_target_key,
            state_changes=target_state_changes,
            market_data=market_data,
            window_start=validated_window_start,
            window_end=validated_window_end,
            annual_periods=config.annual_periods,
            rolling_sharpe_bars=config.rolling_sharpe_bars,
            execution_records=execution_records,
            require_execution_records=require_execution_records,
            adjustment_sidecar=adjustment_sidecar,
        )
    except ValidationWindowPerformanceError as exc:
        raise PortfolioFeedbackError(str(exc)) from exc
    exposure_path = tuple(_to_exposure_path_fact(item) for item in performance.bar_returns)
    portfolio_facts = PortfolioFacts(
        benchmark_return=performance.summary.total_underlying_return,
        strategy_return=performance.summary.total_strategy_return,
        active_return=(
            performance.summary.total_strategy_return
            - performance.summary.total_underlying_return
        ),
        strategy_max_drawdown=performance.summary.max_drawdown,
        benchmark_max_drawdown=_calculate_max_drawdown(
            tuple(item.cumulative_underlying_equity for item in performance.bar_returns)
        ),
        average_exposure=mean(abs(item.target_weight) for item in performance.bar_returns),
        time_in_market=(
            sum(1 for item in performance.bar_returns if item.target_weight != 0.0)
            / len(performance.bar_returns)
        ),
        turnover=_calculate_turnover(
            state_changes=target_state_changes,
            window_start=validated_window_start,
            window_end=validated_window_end,
        ),
        bar_count=len(performance.bar_returns),
    )
    segment_facts = _build_segment_facts(
        state_changes=target_state_changes,
        bar_returns=performance.bar_returns,
        window_start=validated_window_start,
        window_end=validated_window_end,
        execution_records=execution_records,
        require_execution_records=require_execution_records,
        adjustment_sidecar=adjustment_sidecar,
    )
    decision_facts = _build_decision_facts(
        state_changes=target_state_changes,
        bar_returns=performance.bar_returns,
        window_start=validated_window_start,
        window_end=validated_window_end,
        config=config,
    )
    return PortfolioFeedbackSnapshot(
        target_key=validated_target_key,
        run_id=validated_run_id,
        window_start=validated_window_start,
        window_end=validated_window_end,
        config=config,
        portfolio_facts=portfolio_facts,
        segment_facts=segment_facts,
        decision_facts=decision_facts,
        exposure_path=exposure_path,
    )


def build_default_portfolio_feedback_config(
    *,
    market_mapping: MarketMapping,
    pre_decision_bars: int = 2,
    post_decision_bars: tuple[int, ...] = (1, 2),
    material_positive_return: float = 0.01,
    material_negative_return: float = -0.01,
    rolling_sharpe_bars: int = 20,
) -> PortfolioFeedbackConfig:
    """Build the standard reflection-facing portfolio feedback config."""
    if not isinstance(market_mapping, MarketMapping):
        raise PortfolioFeedbackError("market_mapping must be a MarketMapping instance.")
    return PortfolioFeedbackConfig(
        annual_periods=resolve_annual_periods(
            market_session=market_mapping.market_session,
            bar_granularity=market_mapping.bar_granularity,
            exchange_session_scope=market_mapping.exchange_session_scope,
        ),
        pre_decision_bars=pre_decision_bars,
        post_decision_bars=post_decision_bars,
        material_positive_return=material_positive_return,
        material_negative_return=material_negative_return,
        rolling_sharpe_bars=rolling_sharpe_bars,
    )


def build_portfolio_feedback_run_id(*, prefix: str, episode_id: str, window_end: datetime) -> str:
    """Build a stable clean-path run id for reflection-scoped portfolio feedback."""
    if not isinstance(prefix, str) or not prefix.strip():
        raise PortfolioFeedbackError("prefix must be a non-blank string.")
    if not isinstance(episode_id, str) or not episode_id.strip():
        raise PortfolioFeedbackError("episode_id must be a non-blank string.")
    window_end_utc = validate_timestamp(
        window_end,
        field_name="window_end",
        error_type=PortfolioFeedbackError,
    )
    raw = f"{prefix}-{episode_id}-{window_end_utc.strftime('%Y%m%dT%H%M%SZ')}"
    return _validate_run_id(
        "".join(
            char if char.isalnum() or char in {"-", "_", "."} else "-"
            for char in raw
        )
    )


def _to_exposure_path_fact(item: ValidationWindowBarReturn) -> ExposurePathFact:
    return ExposurePathFact(
        target_key=item.target_key,
        state=item.state,
        target_weight=item.target_weight,
        interval_start_at=item.interval_start_at,
        interval_end_at=item.interval_end_at,
        mark_mode=item.mark_mode,
        entry_price=item.entry_price,
        exit_price=item.exit_price,
        underlying_return=item.underlying_return,
        strategy_return=item.strategy_return,
        cumulative_benchmark_equity=item.cumulative_underlying_equity,
        cumulative_strategy_equity=item.cumulative_strategy_equity,
    )


def _build_segment_facts(
    *,
    state_changes: tuple[ViewStateChange, ...],
    bar_returns: tuple[ValidationWindowBarReturn, ...],
    window_start: datetime,
    window_end: datetime,
    execution_records: tuple[ExecutionRecord, ...],
    require_execution_records: bool,
    adjustment_sidecar: MarketAdjustmentSidecar | None,
) -> tuple[SegmentFact, ...]:
    episode_build = build_episodes(state_changes)
    segments: list[PositionSegment] = list(episode_build.completed_segments)
    if episode_build.open_segment is not None:
        segments.append(episode_build.open_segment)
    state_by_change_id = {item.state_change_id: item.state for item in state_changes}
    execution_by_change_id = _execution_record_by_state_change(
        state_changes=state_changes,
        execution_records=execution_records,
    )
    facts: list[SegmentFact] = []
    for segment in segments:
        open_execution = _resolve_execution_record(
            execution_by_change_id=execution_by_change_id,
            state_change_id=segment.opened_by_state_change_id,
            require_execution_records=require_execution_records,
        )
        close_execution = _resolve_execution_record(
            execution_by_change_id=execution_by_change_id,
            state_change_id=segment.closed_by_state_change_id,
            require_execution_records=require_execution_records,
        )
        opened_at = (
            segment.opened_at if open_execution is None else open_execution.executed_at
        )
        if opened_at is None:
            raise PortfolioFeedbackError("executed open record is missing executed_at.")
        segment_end = (
            segment.closed_at
            if close_execution is None
            else close_execution.executed_at
        ) or window_end
        effective_open = max(opened_at, window_start)
        effective_close = min(segment_end, window_end)
        if effective_close <= effective_open:
            continue
        state = state_by_change_id.get(segment.opened_by_state_change_id)
        if state is None:
            raise PortfolioFeedbackError(
                "segment opened_by_state_change_id does not match input state changes."
            )
        facts.append(
            _build_segment_fact(
                segment=segment,
                state=state,
                target_weight=(
                    segment.target_weight
                    if open_execution is None
                    else open_execution.target_weight
                ),
                effective_open=effective_open,
                effective_close=effective_close,
                bar_returns=bar_returns,
                exit_price_override=None
                if close_execution is None
                else _realized_execution_price(
                    execution_record=close_execution,
                    adjustment_sidecar=adjustment_sidecar,
                ),
            )
        )
    return tuple(facts)


def _build_segment_fact(
    *,
    segment: PositionSegment,
    state: ViewState,
    target_weight: float | None,
    effective_open: datetime,
    effective_close: datetime,
    bar_returns: tuple[ValidationWindowBarReturn, ...],
    exit_price_override: float | None,
) -> SegmentFact:
    selected = tuple(
        item
        for item in bar_returns
        if item.interval_start_at >= effective_open and item.interval_start_at < effective_close
    )
    if not selected:
        entry = _find_first_window_return_starting_at_or_after(bar_returns, effective_open)
        return SegmentFact(
            segment_id=segment.segment_id,
            state=state,
            target_weight=_require_execution_float(
                target_weight,
                "executed open record is missing target_weight.",
            ),
            opened_at=effective_open,
            closed_at=effective_close,
            entry_bar_start_at=None if entry is None else entry.interval_start_at,
            exit_bar_start_at=None,
            entry_price=None if entry is None else entry.entry_price,
            exit_price=None,
            underlying_return=None,
            strategy_return=None,
            exposure_shortfall=None,
            status="pending_market_data",
        )

    exit_price = (
        selected[-1].exit_price if exit_price_override is None else exit_price_override
    )
    underlying_return = exit_price / selected[0].entry_price - 1.0
    active_target_weight = _require_execution_float(
        target_weight,
        "executed open record is missing target_weight.",
    )
    strategy_return = active_target_weight * underlying_return
    exit_bar_start_at = (
        selected[-1].interval_start_at
        if selected[-1].mark_mode == "terminal_close"
        else selected[-1].interval_end_at
    )
    return SegmentFact(
        segment_id=segment.segment_id,
        state=state,
        target_weight=active_target_weight,
        opened_at=effective_open,
        closed_at=effective_close,
        entry_bar_start_at=selected[0].interval_start_at,
        exit_bar_start_at=exit_bar_start_at,
        entry_price=selected[0].entry_price,
        exit_price=exit_price,
        underlying_return=underlying_return,
        strategy_return=strategy_return,
        exposure_shortfall=underlying_return - strategy_return,
        status="final",
    )


def _build_decision_facts(
    *,
    state_changes: tuple[ViewStateChange, ...],
    bar_returns: tuple[ValidationWindowBarReturn, ...],
    window_start: datetime,
    window_end: datetime,
    config: PortfolioFeedbackConfig,
) -> tuple[DecisionFact, ...]:
    facts: list[DecisionFact] = []
    previous_state: ViewState = "flat"
    previous_weight = 0.0
    for state_change in state_changes:
        if state_change.effective_at < window_start:
            previous_state = state_change.state
            previous_weight = state_change.target_weight
            continue
        if state_change.effective_at > window_end:
            break
        pre_window = _return_window_before(
            bar_returns=bar_returns,
            effective_at=state_change.effective_at,
            requested_bar_count=config.pre_decision_bars,
        )
        post_windows = tuple(
            _return_window_after(
                bar_returns=bar_returns,
                effective_at=state_change.effective_at,
                requested_bar_count=bar_count,
            )
            for bar_count in config.post_decision_bars
        )
        exposure_change = _classify_exposure_change(
            previous_weight=previous_weight,
            new_weight=state_change.target_weight,
        )
        facts.append(
            DecisionFact(
                state_change_id=state_change.state_change_id,
                effective_at=state_change.effective_at,
                previous_state=previous_state,
                new_state=state_change.state,
                previous_target_weight=previous_weight,
                new_target_weight=state_change.target_weight,
                source_event_ids=state_change.source_event_ids,
                pre_decision_return_window=pre_window,
                post_decision_return_windows=post_windows,
                exposure_change=exposure_change,
                timing_label=_label_decision_timing(
                    exposure_change=exposure_change,
                    pre_window=pre_window,
                    post_windows=post_windows,
                    config=config,
                ),
            )
        )
        previous_state = state_change.state
        previous_weight = state_change.target_weight
    return tuple(facts)


def _return_window_before(
    *,
    bar_returns: tuple[ValidationWindowBarReturn, ...],
    effective_at: datetime,
    requested_bar_count: int,
) -> ReturnWindowFact:
    eligible = tuple(item for item in bar_returns if item.interval_end_at <= effective_at)
    selected = eligible[-requested_bar_count:]
    return _build_return_window_fact(
        selected=selected,
        requested_bar_count=requested_bar_count,
    )


def _return_window_after(
    *,
    bar_returns: tuple[ValidationWindowBarReturn, ...],
    effective_at: datetime,
    requested_bar_count: int,
) -> ReturnWindowFact:
    selected = tuple(
        item for item in bar_returns if item.interval_start_at >= effective_at
    )[:requested_bar_count]
    return _build_return_window_fact(
        selected=selected,
        requested_bar_count=requested_bar_count,
    )


def _build_return_window_fact(
    *,
    selected: tuple[ValidationWindowBarReturn, ...],
    requested_bar_count: int,
) -> ReturnWindowFact:
    if len(selected) < requested_bar_count:
        return ReturnWindowFact(
            status="pending_market_data",
            bar_count=len(selected),
            requested_bar_count=requested_bar_count,
            window_start=None if not selected else selected[0].interval_start_at,
            window_end=None if not selected else selected[-1].interval_end_at,
            underlying_return=None,
        )
    return ReturnWindowFact(
        status="final",
        bar_count=len(selected),
        requested_bar_count=requested_bar_count,
        window_start=selected[0].interval_start_at,
        window_end=selected[-1].interval_end_at,
        underlying_return=prod(1.0 + item.underlying_return for item in selected) - 1.0,
    )


def _label_decision_timing(
    *,
    exposure_change: ExposureChange,
    pre_window: ReturnWindowFact,
    post_windows: tuple[ReturnWindowFact, ...],
    config: PortfolioFeedbackConfig,
) -> DecisionTimingLabel:
    pre_return = pre_window.underlying_return if pre_window.status == "final" else None
    post_returns = tuple(
        item.underlying_return
        for item in post_windows
        if item.status == "final" and item.underlying_return is not None
    )
    if exposure_change == "decreased":
        if pre_return is not None and pre_return <= config.material_negative_return:
            if not any(item < pre_return for item in post_returns):
                return "after_drawdown"
            return "before_drawdown"
        if any(item <= config.material_negative_return for item in post_returns):
            return "before_drawdown"
    if exposure_change == "increased":
        if pre_return is not None and pre_return >= config.material_positive_return:
            if not any(item > pre_return for item in post_returns):
                return "after_rally"
            return "before_rally"
        if any(item >= config.material_positive_return for item in post_returns):
            return "before_rally"
    return "unclear"


def _classify_exposure_change(
    *,
    previous_weight: float,
    new_weight: float,
) -> ExposureChange:
    previous_abs = abs(previous_weight)
    new_abs = abs(new_weight)
    if new_abs > previous_abs:
        return "increased"
    if new_abs < previous_abs:
        return "decreased"
    return "unchanged"


def _calculate_turnover(
    *,
    state_changes: tuple[ViewStateChange, ...],
    window_start: datetime,
    window_end: datetime,
) -> float:
    previous_weight = 0.0
    for state_change in state_changes:
        if state_change.effective_at < window_start:
            previous_weight = state_change.target_weight
            continue
        break

    turnover = 0.0
    for state_change in state_changes:
        if state_change.effective_at < window_start:
            continue
        if state_change.effective_at > window_end:
            break
        turnover += abs(state_change.target_weight - previous_weight)
        previous_weight = state_change.target_weight
    return turnover


def _calculate_max_drawdown(equity_values: tuple[float, ...]) -> float:
    peak = 1.0
    max_drawdown = 0.0
    for equity in equity_values:
        if equity > peak:
            peak = equity
        drawdown = 1.0 - (equity / peak)
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    return max_drawdown


def _find_first_window_return_starting_at_or_after(
    bar_returns: tuple[ValidationWindowBarReturn, ...],
    timestamp: datetime,
) -> ValidationWindowBarReturn | None:
    for bar_return in bar_returns:
        if bar_return.interval_start_at >= timestamp:
            return bar_return
    return None


def _execution_record_by_state_change(
    *,
    state_changes: tuple[ViewStateChange, ...],
    execution_records: tuple[ExecutionRecord, ...],
) -> dict[str, ExecutionRecord]:
    target_keys = {state_change.target_key for state_change in state_changes}
    records: dict[str, ExecutionRecord] = {}
    for target_key in sorted(target_keys):
        records.update(
            execution_record_by_state_change(
                target_key=target_key,
                state_changes=state_changes,
                execution_records=execution_records,
            )
        )
    return records


def _resolve_execution_record(
    *,
    execution_by_change_id: dict[str, ExecutionRecord],
    state_change_id: str | None,
    require_execution_records: bool,
) -> ExecutionRecord | None:
    if state_change_id is None:
        return None
    record = execution_by_change_id.get(state_change_id)
    if record is None:
        if require_execution_records:
            raise PortfolioFeedbackError(
                f"Missing execution record for state_change_id {state_change_id!r}."
            )
        return None
    if record.status != "executed":
        if require_execution_records:
            raise PortfolioFeedbackError(
                f"Execution record for state_change_id {state_change_id!r} was not executed."
            )
        return None
    if (
        record.executed_at is None
        or record.adjusted_price is None
        or record.target_weight is None
    ):
        raise PortfolioFeedbackError(
            "Executed record is missing executed_at, adjusted_price, or target_weight."
        )
    return record


def _realized_execution_price(
    *,
    execution_record: ExecutionRecord,
    adjustment_sidecar: MarketAdjustmentSidecar | None,
) -> float:
    adjusted_price = _require_execution_float(
        execution_record.adjusted_price,
        "executed record is missing adjusted_price.",
    )
    if adjustment_sidecar is None:
        return adjusted_price
    executed_at = execution_record.executed_at
    if executed_at is None:
        raise PortfolioFeedbackError("executed record is missing executed_at.")
    return adjust_price_for_realized_policy(
        raw_price=adjusted_price,
        timestamp=executed_at,
        sidecar=adjustment_sidecar,
    )


def _require_execution_float(value: float | None, message: str) -> float:
    if value is None:
        raise PortfolioFeedbackError(message)
    return value


def _validate_run_id(value: str) -> str:
    if not isinstance(value, str):
        raise PortfolioFeedbackError("run_id must be a string.")
    normalized = value.strip()
    if not normalized:
        raise PortfolioFeedbackError("run_id must not be blank.")
    if normalized in {".", ".."}:
        raise PortfolioFeedbackError("run_id must be a clean path segment.")
    if normalized != value or any(char in normalized for char in "\\/:*?\"<>|"):
        raise PortfolioFeedbackError("run_id must be a clean path segment.")
    return normalized


def resolve_annual_periods(
    *,
    market_session: str,
    bar_granularity: str,
    exchange_session_scope: str | None,
    error_type: type[Exception] = PortfolioFeedbackError,
) -> float:
    """Resolve deterministic annualization for validation windows from market session scope."""
    bar_minutes = _parse_bar_granularity_minutes(bar_granularity)
    if market_session == "continuous":
        return _CONTINUOUS_SESSION_DAILY_BAR_MINUTES / bar_minutes
    if market_session != "exchange_session":
        raise error_type("market_session must be either 'continuous' or 'exchange_session'.")
    if exchange_session_scope == "regular":
        return _REGULAR_EXCHANGE_SESSION_DAILY_BAR_MINUTES / bar_minutes
    if exchange_session_scope == "extended":
        return _EXTENDED_EXCHANGE_SESSION_DAILY_BAR_MINUTES / bar_minutes
    raise error_type(
        "exchange_session market_session requires exchange_session_scope "
        "of 'regular' or 'extended'."
    )


def _parse_bar_granularity_minutes(raw_value: str) -> float:
    normalized = raw_value.strip().lower()
    if normalized.endswith("min"):
        count_text = normalized[:-3]
        unit = "min"
    elif normalized.endswith("m"):
        count_text = normalized[:-1]
        unit = "min"
    elif normalized.endswith("h"):
        count_text = normalized[:-1]
        unit = "h"
    elif normalized.endswith("d"):
        count_text = normalized[:-1]
        unit = "d"
    else:
        raise PortfolioFeedbackError(
            "bar_granularity must match '<N>m', '<N>min', '<N>h', or '<N>d'."
        )
    if not count_text.isdigit() or count_text.startswith("0"):
        raise PortfolioFeedbackError(
            "bar_granularity must match '<N>m', '<N>min', '<N>h', or '<N>d'."
        )
    count = int(count_text)
    if unit == "min":
        return float(count)
    if unit == "h":
        return float(count * 60)
    return float(count * 24 * 60)


def _format_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


__all__ = [
    "DecisionFact",
    "DecisionTimingLabel",
    "ExposureChange",
    "ExposurePathFact",
    "PortfolioFacts",
    "PortfolioFeedbackConfig",
    "PortfolioFeedbackError",
    "PortfolioFeedbackSnapshot",
    "ReturnWindowFact",
    "SegmentFact",
    "build_default_portfolio_feedback_config",
    "build_portfolio_feedback_run_id",
    "build_portfolio_feedback_snapshot",
]
