"""Deterministic Phase 8 counterfactual baselines."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite

from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    PositionSegment,
    ViewStateChange,
    ViewEpisode,
)
from event_trader.execution.contracts import (
    ExecutionRecord,
    MarketDataProvenanceReceipt,
    execution_contract_hash,
)
from event_trader.market.adjustments import (
    MarketAdjustmentSidecar,
    adjust_price_for_realized_policy,
    apply_adjustment_policy_to_series,
)
from event_trader.validation.execution_linkage import execution_record_by_state_change
from event_trader.validation.returns import ValidationReturnsResult

from .contracts import (
    CounterfactualBaselineResult,
    CounterfactualBaselineType,
    CounterfactualContractError,
    CounterfactualEvaluationReport,
)
from .metrics import (
    actual_return_from_validation,
    build_decision_quality_attribution,
    build_episode_metrics,
)

_TAKE_PROFIT_RETURN = 0.05
_STOP_LOSS_RETURN = -0.03


@dataclass(frozen=True, slots=True)
class _EntryPrice:
    bar: MarketDataBar
    price: float


@dataclass(frozen=True, slots=True)
class _ExecutionPrice:
    timestamp: datetime
    price: float


def build_counterfactual_evaluation_report(
    *,
    episode: ViewEpisode,
    segments: Sequence[PositionSegment],
    market_data: MarketDataSeries,
    market_provenance: MarketDataProvenanceReceipt,
    actual_return: float | None = None,
    validation_returns: ValidationReturnsResult | None = None,
    execution_records: Sequence[ExecutionRecord] = (),
    state_changes: Sequence[ViewStateChange] = (),
    adjustment_sidecar: MarketAdjustmentSidecar | None = None,
    evaluation_end_at: datetime | None = None,
    created_at: datetime | None = None,
) -> CounterfactualEvaluationReport:
    """Build the permanent deterministic counterfactual report for one episode."""
    _validate_inputs(
        episode=episode,
        segments=segments,
        actual_return=actual_return,
        market_data=market_data,
        market_provenance=market_provenance,
        adjustment_sidecar=adjustment_sidecar,
    )
    if episode.closed_at is None:
        raise CounterfactualContractError("counterfactual evaluation requires a closed episode.")
    end_at = evaluation_end_at or episode.closed_at
    if not isinstance(end_at, datetime):
        raise CounterfactualContractError("evaluation_end_at must be a datetime when provided.")
    if end_at < episode.opened_at:
        raise CounterfactualContractError("evaluation_end_at must not be before episode.opened_at.")
    resolved_actual_return = (
        actual_return_from_validation(episode=episode, validation_returns=validation_returns)
        if validation_returns is not None
        else actual_return
    )
    if resolved_actual_return is None:
        raise CounterfactualContractError(
            "actual_return is required when validation_returns is not provided."
        )

    computed_at = created_at or datetime.now(UTC)
    ordered_segments = tuple(sorted(segments, key=lambda item: item.opened_at))
    opening_segment = ordered_segments[0]
    series = (
        apply_adjustment_policy_to_series(series=market_data, sidecar=adjustment_sidecar).series
        if adjustment_sidecar is not None
        else market_data
    )
    bars = series.bars
    provenance_hash = execution_contract_hash(market_provenance.to_json_payload())
    execution_by_state_change = _latest_execution_records_by_state_change(
        execution_records,
        state_changes=state_changes,
    )
    decision_episode_id = _decision_episode_id(execution_records)
    pm_decision_id = _pm_decision_id(execution_records)

    baseline_results = (
        _available(
            baseline_type="no_trade",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            baseline_return=0.0,
            actual_return=resolved_actual_return,
            entry_bar=None,
            exit_bar=None,
            provenance_hash=provenance_hash,
            market_data_hash=market_provenance.market_data_hash,
            assumptions={"baseline": "no position is opened"},
            result_metrics={"baseline_return": 0.0},
            computed_at=computed_at,
        ),
        _buy_and_hold_proxy(
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            bars=bars,
            actual_return=resolved_actual_return,
            end_at=end_at,
            provenance_hash=provenance_hash,
            market_data_hash=market_provenance.market_data_hash,
            computed_at=computed_at,
        ),
        _delayed_entry(
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            opening_segment=opening_segment,
            bars=bars,
            actual_return=resolved_actual_return,
            end_at=end_at,
            execution_by_state_change=execution_by_state_change,
            adjustment_sidecar=adjustment_sidecar,
            provenance_hash=provenance_hash,
            market_data_hash=market_provenance.market_data_hash,
            computed_at=computed_at,
        ),
        _full_hold(
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            opening_segment=opening_segment,
            bars=bars,
            actual_return=resolved_actual_return,
            end_at=end_at,
            execution_by_state_change=execution_by_state_change,
            adjustment_sidecar=adjustment_sidecar,
            provenance_hash=provenance_hash,
            market_data_hash=market_provenance.market_data_hash,
            computed_at=computed_at,
        ),
        _random_exit(
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            opening_segment=opening_segment,
            bars=bars,
            actual_return=resolved_actual_return,
            end_at=end_at,
            execution_by_state_change=execution_by_state_change,
            adjustment_sidecar=adjustment_sidecar,
            provenance_hash=provenance_hash,
            market_data_hash=market_provenance.market_data_hash,
            computed_at=computed_at,
        ),
        _rule_based_exit(
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            opening_segment=opening_segment,
            bars=bars,
            actual_return=resolved_actual_return,
            end_at=end_at,
            execution_by_state_change=execution_by_state_change,
            adjustment_sidecar=adjustment_sidecar,
            provenance_hash=provenance_hash,
            market_data_hash=market_provenance.market_data_hash,
            computed_at=computed_at,
        ),
    )

    report = CounterfactualEvaluationReport(
        episode_id=episode.episode_id,
        target_key=episode.target_key,
        actual_return=resolved_actual_return,
        market_provenance=market_provenance,
        baseline_results=baseline_results,
        created_at=computed_at,
    )
    if validation_returns is None:
        return report
    episode_metrics = build_episode_metrics(
        episode=episode,
        segments=ordered_segments,
        market_data=series,
        validation_returns=validation_returns,
        report=report,
        evaluation_end_at=end_at,
    )
    attribution = build_decision_quality_attribution(
        episode=episode,
        report=report,
        execution_records=execution_records,
    )
    return CounterfactualEvaluationReport(
        episode_id=report.episode_id,
        target_key=report.target_key,
        actual_return=report.actual_return,
        market_provenance=report.market_provenance,
        baseline_results=report.baseline_results,
        created_at=report.created_at,
        episode_metrics=episode_metrics,
        decision_quality_attribution=attribution,
    )


def _buy_and_hold_proxy(
    *,
    episode: ViewEpisode,
    decision_episode_id: str | None,
    pm_decision_id: str | None,
    bars: tuple[MarketDataBar, ...],
    actual_return: float,
    end_at: datetime,
    provenance_hash: str,
    market_data_hash: str | None,
    computed_at: datetime,
) -> CounterfactualBaselineResult:
    entry = _first_bar_starting_at_or_after(bars=bars, timestamp=episode.opened_at)
    if entry is None:
        return _unavailable(
            baseline_type="buy_and_hold_proxy",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            reason="missing_entry_bar",
            provenance_hash=provenance_hash,
            market_data_hash=market_data_hash,
            assumptions={"entry": "first tradable bar at or after episode open"},
            computed_at=computed_at,
        )
    exit_bar = _terminal_bar_at_or_before(bars=bars, timestamp=end_at)
    if exit_bar is None or exit_bar.start_at < entry.start_at:
        return _unavailable(
            baseline_type="buy_and_hold_proxy",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            reason="missing_exit_bar",
            entry_bar=entry,
            provenance_hash=provenance_hash,
            market_data_hash=market_data_hash,
            assumptions={"exit": "evaluation horizon or episode close bar"},
            computed_at=computed_at,
        )
    baseline_return = exit_bar.close_price / entry.open_price - 1.0
    return _available(
        baseline_type="buy_and_hold_proxy",
        episode=episode,
        decision_episode_id=decision_episode_id,
        pm_decision_id=pm_decision_id,
        baseline_return=baseline_return,
        actual_return=actual_return,
        entry_bar=entry,
        exit_bar=exit_bar,
        provenance_hash=provenance_hash,
        market_data_hash=market_data_hash,
        assumptions={
            "entry": "episode open first tradable bar",
            "exit": "evaluation horizon or episode close terminal bar",
            "weight": "underlying proxy return, unweighted",
        },
        result_metrics=_result_metrics(
            entry=entry,
            exit_bar=exit_bar,
            baseline_return=baseline_return,
        ),
        computed_at=computed_at,
    )


def _delayed_entry(
    *,
    episode: ViewEpisode,
    decision_episode_id: str | None,
    pm_decision_id: str | None,
    opening_segment: PositionSegment,
    bars: tuple[MarketDataBar, ...],
    actual_return: float,
    end_at: datetime,
    execution_by_state_change: dict[str, ExecutionRecord],
    adjustment_sidecar: MarketAdjustmentSidecar | None,
    provenance_hash: str,
    market_data_hash: str | None,
    computed_at: datetime,
) -> CounterfactualBaselineResult:
    actual_entry = _actual_entry_price(
        segment=opening_segment,
        bars=bars,
        execution_by_state_change=execution_by_state_change,
        adjustment_sidecar=adjustment_sidecar,
    )
    if actual_entry is None:
        return _unavailable_for_entry(
            "delayed_entry",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            provenance_hash=provenance_hash,
            market_data_hash=market_data_hash,
            computed_at=computed_at,
        )
    entry_index = _bar_index(bars=bars, bar=actual_entry.bar)
    delayed_index = entry_index + 1
    if delayed_index >= len(bars):
        return _unavailable(
            baseline_type="delayed_entry",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            reason="missing_delayed_entry_bar",
            entry_bar=actual_entry.bar,
            provenance_hash=provenance_hash,
            market_data_hash=market_data_hash,
            assumptions={"entry": "one tradable bar after actual entry"},
            computed_at=computed_at,
        )
    delayed_entry_bar = bars[delayed_index]
    exit_bar = _terminal_bar_at_or_before(bars=bars, timestamp=end_at)
    if exit_bar is None or exit_bar.start_at < delayed_entry_bar.start_at:
        return _unavailable(
            baseline_type="delayed_entry",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            reason="missing_exit_bar",
            entry_bar=delayed_entry_bar,
            provenance_hash=provenance_hash,
            market_data_hash=market_data_hash,
            assumptions={"exit": "same evaluation close/horizon as report"},
            computed_at=computed_at,
        )
    baseline_return = (
        opening_segment.target_weight
        * (exit_bar.close_price / delayed_entry_bar.open_price - 1.0)
    )
    return _available(
        baseline_type="delayed_entry",
        episode=episode,
        decision_episode_id=decision_episode_id,
        pm_decision_id=pm_decision_id,
        baseline_return=baseline_return,
        actual_return=actual_return,
        entry_bar=delayed_entry_bar,
        exit_bar=exit_bar,
        provenance_hash=provenance_hash,
        market_data_hash=market_data_hash,
        assumptions={
            "entry": "one tradable bar after actual entry",
            "target_weight": opening_segment.target_weight,
        },
        result_metrics=_result_metrics(
            entry=delayed_entry_bar,
            exit_bar=exit_bar,
            baseline_return=baseline_return,
        ),
        computed_at=computed_at,
    )


def _full_hold(
    *,
    episode: ViewEpisode,
    decision_episode_id: str | None,
    pm_decision_id: str | None,
    opening_segment: PositionSegment,
    bars: tuple[MarketDataBar, ...],
    actual_return: float,
    end_at: datetime,
    execution_by_state_change: dict[str, ExecutionRecord],
    adjustment_sidecar: MarketAdjustmentSidecar | None,
    provenance_hash: str,
    market_data_hash: str | None,
    computed_at: datetime,
) -> CounterfactualBaselineResult:
    entry = _actual_entry_price(
        segment=opening_segment,
        bars=bars,
        execution_by_state_change=execution_by_state_change,
        adjustment_sidecar=adjustment_sidecar,
    )
    if entry is None:
        return _unavailable_for_entry(
            "full_hold",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            provenance_hash=provenance_hash,
            market_data_hash=market_data_hash,
            computed_at=computed_at,
        )
    exit_bar = _terminal_bar_at_or_before(bars=bars, timestamp=end_at)
    if exit_bar is None or exit_bar.start_at < entry.bar.start_at:
        return _unavailable(
            baseline_type="full_hold",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            reason="missing_exit_bar",
            entry_bar=entry.bar,
            provenance_hash=provenance_hash,
            market_data_hash=market_data_hash,
            assumptions={"exit": "evaluation horizon or close, ignoring interim state changes"},
            computed_at=computed_at,
        )
    baseline_return = opening_segment.target_weight * (exit_bar.close_price / entry.price - 1.0)
    return _available(
        baseline_type="full_hold",
        episode=episode,
        decision_episode_id=decision_episode_id,
        pm_decision_id=pm_decision_id,
        baseline_return=baseline_return,
        actual_return=actual_return,
        entry_bar=entry.bar,
        exit_bar=exit_bar,
        provenance_hash=provenance_hash,
        market_data_hash=market_data_hash,
        assumptions={
            "entry": "actual entry",
            "exit": "evaluation horizon or close, ignoring interim state changes",
            "target_weight": opening_segment.target_weight,
        },
        result_metrics=_result_metrics(
            entry=entry.bar,
            exit_bar=exit_bar,
            baseline_return=baseline_return,
        ),
        computed_at=computed_at,
    )


def _random_exit(
    *,
    episode: ViewEpisode,
    decision_episode_id: str | None,
    pm_decision_id: str | None,
    opening_segment: PositionSegment,
    bars: tuple[MarketDataBar, ...],
    actual_return: float,
    end_at: datetime,
    execution_by_state_change: dict[str, ExecutionRecord],
    adjustment_sidecar: MarketAdjustmentSidecar | None,
    provenance_hash: str,
    market_data_hash: str | None,
    computed_at: datetime,
) -> CounterfactualBaselineResult:
    entry = _actual_entry_price(
        segment=opening_segment,
        bars=bars,
        execution_by_state_change=execution_by_state_change,
        adjustment_sidecar=adjustment_sidecar,
    )
    if entry is None:
        return _unavailable_for_entry(
            "random_exit",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            provenance_hash=provenance_hash,
            market_data_hash=market_data_hash,
            computed_at=computed_at,
        )
    terminal = _terminal_bar_at_or_before(bars=bars, timestamp=end_at)
    if terminal is None:
        return _unavailable(
            baseline_type="random_exit",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            reason="missing_exit_bar",
            entry_bar=entry.bar,
            provenance_hash=provenance_hash,
            market_data_hash=market_data_hash,
            assumptions={"exit": "seeded single random exit"},
            computed_at=computed_at,
        )
    entry_index = _bar_index(bars=bars, bar=entry.bar)
    terminal_index = _bar_index(bars=bars, bar=terminal)
    candidates = bars[entry_index + 1 : terminal_index + 1]
    seed = _baseline_seed(
        episode_id=episode.episode_id,
        provenance_hash=provenance_hash,
        baseline_type="random_exit",
    )
    if not candidates:
        return _unavailable(
            baseline_type="random_exit",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            reason="missing_random_exit_candidate_bar",
            entry_bar=entry.bar,
            provenance_hash=provenance_hash,
            market_data_hash=market_data_hash,
            assumptions={"seed": seed, "candidate_rule": "after actual entry through terminal"},
            computed_at=computed_at,
        )
    exit_bar = candidates[int(seed, 16) % len(candidates)]
    baseline_return = opening_segment.target_weight * (exit_bar.close_price / entry.price - 1.0)
    return _available(
        baseline_type="random_exit",
        episode=episode,
        decision_episode_id=decision_episode_id,
        pm_decision_id=pm_decision_id,
        baseline_return=baseline_return,
        actual_return=actual_return,
        entry_bar=entry.bar,
        exit_bar=exit_bar,
        provenance_hash=provenance_hash,
        market_data_hash=market_data_hash,
        assumptions={
            "seed": seed,
            "candidate_count": len(candidates),
            "candidate_rule": "bars after actual entry through evaluation terminal",
            "target_weight": opening_segment.target_weight,
        },
        result_metrics=_result_metrics(
            entry=entry.bar,
            exit_bar=exit_bar,
            baseline_return=baseline_return,
        ),
        computed_at=computed_at,
    )


def _rule_based_exit(
    *,
    episode: ViewEpisode,
    decision_episode_id: str | None,
    pm_decision_id: str | None,
    opening_segment: PositionSegment,
    bars: tuple[MarketDataBar, ...],
    actual_return: float,
    end_at: datetime,
    execution_by_state_change: dict[str, ExecutionRecord],
    adjustment_sidecar: MarketAdjustmentSidecar | None,
    provenance_hash: str,
    market_data_hash: str | None,
    computed_at: datetime,
) -> CounterfactualBaselineResult:
    entry = _actual_entry_price(
        segment=opening_segment,
        bars=bars,
        execution_by_state_change=execution_by_state_change,
        adjustment_sidecar=adjustment_sidecar,
    )
    if entry is None:
        return _unavailable_for_entry(
            "rule_based_exit",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            provenance_hash=provenance_hash,
            market_data_hash=market_data_hash,
            computed_at=computed_at,
        )
    terminal = _terminal_bar_at_or_before(bars=bars, timestamp=end_at)
    if terminal is None:
        return _unavailable(
            baseline_type="rule_based_exit",
            episode=episode,
            decision_episode_id=decision_episode_id,
            pm_decision_id=pm_decision_id,
            reason="missing_exit_bar",
            entry_bar=entry.bar,
            provenance_hash=provenance_hash,
            market_data_hash=market_data_hash,
            assumptions={"take_profit": _TAKE_PROFIT_RETURN, "stop_loss": _STOP_LOSS_RETURN},
            computed_at=computed_at,
        )
    entry_index = _bar_index(bars=bars, bar=entry.bar)
    terminal_index = _bar_index(bars=bars, bar=terminal)
    selected_bar = terminal
    trigger = "terminal"
    exit_price = terminal.close_price
    for bar in bars[entry_index + 1 : terminal_index + 1]:
        triggered = _rule_trigger(
            bar=bar,
            entry_price=entry.price,
            target_weight=opening_segment.target_weight,
        )
        if triggered is None:
            continue
        selected_bar = bar
        trigger, exit_price = triggered
        break
    baseline_return = opening_segment.target_weight * (exit_price / entry.price - 1.0)
    return _available(
        baseline_type="rule_based_exit",
        episode=episode,
        decision_episode_id=decision_episode_id,
        pm_decision_id=pm_decision_id,
        baseline_return=baseline_return,
        actual_return=actual_return,
        entry_bar=entry.bar,
        exit_bar=selected_bar,
        provenance_hash=provenance_hash,
        market_data_hash=market_data_hash,
        assumptions={
            "take_profit_return": _TAKE_PROFIT_RETURN,
            "stop_loss_return": _STOP_LOSS_RETURN,
            "same_bar_priority": "stop_loss",
            "trigger": trigger,
            "target_weight": opening_segment.target_weight,
        },
        result_metrics={
            **_result_metrics(
                entry=entry.bar,
                exit_bar=selected_bar,
                baseline_return=baseline_return,
            ),
            "trigger": trigger,
        },
        computed_at=computed_at,
    )


def _rule_trigger(
    *,
    bar: MarketDataBar,
    entry_price: float,
    target_weight: float,
) -> tuple[str, float] | None:
    if target_weight > 0:
        stop_price = entry_price * (1.0 + _STOP_LOSS_RETURN)
        take_price = entry_price * (1.0 + _TAKE_PROFIT_RETURN)
        if bar.low_price <= stop_price:
            return ("stop_loss", stop_price)
        if bar.high_price >= take_price:
            return ("take_profit", take_price)
        return None
    stop_price = entry_price * (1.0 - _STOP_LOSS_RETURN)
    take_price = entry_price * (1.0 - _TAKE_PROFIT_RETURN)
    if bar.high_price >= stop_price:
        return ("stop_loss", stop_price)
    if bar.low_price <= take_price:
        return ("take_profit", take_price)
    return None


def _actual_entry_price(
    *,
    segment: PositionSegment,
    bars: tuple[MarketDataBar, ...],
    execution_by_state_change: dict[str, ExecutionRecord],
    adjustment_sidecar: MarketAdjustmentSidecar | None,
) -> _EntryPrice | None:
    execution_price = _execution_price_for_state_change(
        state_change_id=segment.opened_by_state_change_id,
        execution_by_state_change=execution_by_state_change,
        adjustment_sidecar=adjustment_sidecar,
    )
    timestamp = execution_price.timestamp if execution_price is not None else segment.opened_at
    entry_bar = _first_bar_starting_at_or_after(bars=bars, timestamp=timestamp)
    if entry_bar is None:
        return None
    return _EntryPrice(
        bar=entry_bar,
        price=execution_price.price if execution_price is not None else entry_bar.open_price,
    )


def _available(
    *,
    baseline_type: CounterfactualBaselineType,
    episode: ViewEpisode,
    decision_episode_id: str | None,
    pm_decision_id: str | None,
    baseline_return: float,
    actual_return: float,
    entry_bar: MarketDataBar | None,
    exit_bar: MarketDataBar | None,
    provenance_hash: str,
    market_data_hash: str | None,
    assumptions: dict[str, object],
    result_metrics: dict[str, object],
    computed_at: datetime,
) -> CounterfactualBaselineResult:
    delta = baseline_return - actual_return
    return CounterfactualBaselineResult(
        counterfactual_id=_counterfactual_id(
            episode_id=episode.episode_id,
            provenance_hash=provenance_hash,
            baseline_type=baseline_type,
        ),
        episode_id=episode.episode_id,
        decision_episode_id=decision_episode_id,
        pm_decision_id=pm_decision_id,
        baseline_type=baseline_type,
        baseline_return=baseline_return,
        delta_vs_actual=delta,
        entry_bar_start_at=None if entry_bar is None else entry_bar.start_at,
        exit_bar_start_at=None if exit_bar is None else exit_bar.start_at,
        status="available",
        reason=None,
        provenance_hash=provenance_hash,
        assumptions=assumptions,
        market_data_hash=market_data_hash,
        result_metrics=result_metrics,
        comparison_to_actual={
            "actual_return": actual_return,
            "delta_vs_actual": delta,
            "outperformed_actual": baseline_return > actual_return,
        },
        computed_at=computed_at,
    )


def _unavailable(
    *,
    baseline_type: CounterfactualBaselineType,
    episode: ViewEpisode,
    decision_episode_id: str | None,
    pm_decision_id: str | None,
    reason: str,
    provenance_hash: str,
    market_data_hash: str | None,
    assumptions: dict[str, object],
    computed_at: datetime,
    entry_bar: MarketDataBar | None = None,
    exit_bar: MarketDataBar | None = None,
) -> CounterfactualBaselineResult:
    return CounterfactualBaselineResult(
        counterfactual_id=_counterfactual_id(
            episode_id=episode.episode_id,
            provenance_hash=provenance_hash,
            baseline_type=baseline_type,
        ),
        episode_id=episode.episode_id,
        decision_episode_id=decision_episode_id,
        pm_decision_id=pm_decision_id,
        baseline_type=baseline_type,
        baseline_return=None,
        delta_vs_actual=None,
        entry_bar_start_at=None if entry_bar is None else entry_bar.start_at,
        exit_bar_start_at=None if exit_bar is None else exit_bar.start_at,
        status="unavailable",
        reason=reason,
        provenance_hash=provenance_hash,
        assumptions=assumptions,
        market_data_hash=market_data_hash,
        result_metrics={"status": "unavailable", "reason": reason},
        comparison_to_actual={"status": "unavailable", "reason": reason},
        computed_at=computed_at,
    )


def _unavailable_for_entry(
    baseline_type: CounterfactualBaselineType,
    *,
    episode: ViewEpisode,
    decision_episode_id: str | None,
    pm_decision_id: str | None,
    provenance_hash: str,
    market_data_hash: str | None,
    computed_at: datetime,
) -> CounterfactualBaselineResult:
    return _unavailable(
        baseline_type=baseline_type,
        episode=episode,
        decision_episode_id=decision_episode_id,
        pm_decision_id=pm_decision_id,
        reason="missing_entry_bar",
        provenance_hash=provenance_hash,
        market_data_hash=market_data_hash,
        assumptions={"entry": "actual entry bar must be available"},
        computed_at=computed_at,
    )


def _result_metrics(
    *,
    entry: MarketDataBar,
    exit_bar: MarketDataBar,
    baseline_return: float,
) -> dict[str, object]:
    return {
        "baseline_return": baseline_return,
        "entry_price": entry.open_price,
        "exit_price": exit_bar.close_price,
        "bar_count": max(1, int((exit_bar.end_at - entry.start_at).total_seconds() // 60)),
    }


def _first_bar_starting_at_or_after(
    *,
    bars: tuple[MarketDataBar, ...],
    timestamp: datetime,
) -> MarketDataBar | None:
    for bar in bars:
        if bar.start_at >= timestamp:
            return bar
    return None


def _terminal_bar_at_or_before(
    *,
    bars: tuple[MarketDataBar, ...],
    timestamp: datetime,
) -> MarketDataBar | None:
    latest_prior: MarketDataBar | None = None
    for bar in bars:
        if bar.start_at <= timestamp < bar.end_at:
            return bar
        if bar.end_at <= timestamp:
            latest_prior = bar
            continue
        if bar.start_at > timestamp:
            break
    return latest_prior


def _bar_index(*, bars: tuple[MarketDataBar, ...], bar: MarketDataBar) -> int:
    for index, value in enumerate(bars):
        if value == bar:
            return index
    raise CounterfactualContractError("internal error: bar was not found in series.")


def _latest_execution_records_by_state_change(
    execution_records: Sequence[ExecutionRecord],
    *,
    state_changes: Sequence[ViewStateChange] = (),
) -> dict[str, ExecutionRecord]:
    normalized_state_changes = tuple(state_changes)
    if normalized_state_changes:
        latest: dict[str, ExecutionRecord] = {}
        for target_key in sorted({state_change.target_key for state_change in normalized_state_changes}):
            latest.update(
                execution_record_by_state_change(
                    target_key=target_key,
                    state_changes=normalized_state_changes,
                    execution_records=execution_records,
                )
            )
        return latest
    if execution_records:
        raise CounterfactualContractError(
            "execution_records require sidecar state_changes for linkage."
        )
    return {}


def _execution_price_for_state_change(
    *,
    state_change_id: str,
    execution_by_state_change: dict[str, ExecutionRecord],
    adjustment_sidecar: MarketAdjustmentSidecar | None,
) -> _ExecutionPrice | None:
    record = execution_by_state_change.get(state_change_id)
    if record is None or record.status != "executed":
        return None
    if record.executed_at is None or record.adjusted_price is None:
        raise CounterfactualContractError(
            "executed record is missing executed_at or adjusted_price."
        )
    price = record.adjusted_price
    if adjustment_sidecar is not None:
        price = adjust_price_for_realized_policy(
            raw_price=price,
            timestamp=record.executed_at,
            sidecar=adjustment_sidecar,
        )
    return _ExecutionPrice(timestamp=record.executed_at, price=price)


def _decision_episode_id(execution_records: Sequence[ExecutionRecord]) -> str | None:
    for record in execution_records:
        if record.status == "executed":
            return record.decision_episode_id
    return None


def _pm_decision_id(execution_records: Sequence[ExecutionRecord]) -> str | None:
    for record in execution_records:
        if record.status == "executed":
            return record.pm_decision_id
    return None


def _counterfactual_id(
    *,
    episode_id: str,
    provenance_hash: str,
    baseline_type: CounterfactualBaselineType,
) -> str:
    digest = sha256(f"{episode_id}|{provenance_hash}|{baseline_type}".encode())
    return f"counterfactual:{baseline_type}:{digest.hexdigest()}"


def _baseline_seed(
    *,
    episode_id: str,
    provenance_hash: str,
    baseline_type: CounterfactualBaselineType,
) -> str:
    return sha256(f"{episode_id}{provenance_hash}{baseline_type}".encode()).hexdigest()


def _validate_inputs(
    *,
    episode: ViewEpisode,
    segments: Sequence[PositionSegment],
    actual_return: float | None,
    market_data: MarketDataSeries,
    market_provenance: MarketDataProvenanceReceipt,
    adjustment_sidecar: MarketAdjustmentSidecar | None,
) -> None:
    if not isinstance(episode, ViewEpisode):
        raise CounterfactualContractError("episode must be a ViewEpisode instance.")
    if not isinstance(market_data, MarketDataSeries):
        raise CounterfactualContractError("market_data must be a MarketDataSeries instance.")
    if not isinstance(market_provenance, MarketDataProvenanceReceipt):
        raise CounterfactualContractError(
            "market_provenance must be a MarketDataProvenanceReceipt."
        )
    if adjustment_sidecar is not None:
        if not isinstance(adjustment_sidecar, MarketAdjustmentSidecar):
            raise CounterfactualContractError(
                "adjustment_sidecar must be a MarketAdjustmentSidecar when provided."
            )
        if adjustment_sidecar.policy != "forward_adjusted_realized":
            raise CounterfactualContractError(
                "adjustment_sidecar must use forward_adjusted_realized policy."
            )
    if market_provenance.target_key != episode.target_key:
        raise CounterfactualContractError(
            "market_provenance.target_key must match episode target_key."
        )
    if actual_return is not None:
        if isinstance(actual_return, bool) or not isinstance(actual_return, int | float):
            raise CounterfactualContractError("actual_return must be numeric.")
        if not isfinite(float(actual_return)):
            raise CounterfactualContractError("actual_return must be finite.")
    if isinstance(segments, PositionSegment):
        raise CounterfactualContractError("segments must be a sequence of PositionSegment values.")
    normalized_segments = tuple(segments)
    if not normalized_segments:
        raise CounterfactualContractError("segments must not be empty.")
    for segment in normalized_segments:
        if not isinstance(segment, PositionSegment):
            raise CounterfactualContractError(
                "segments must contain only PositionSegment values."
            )
        if segment.episode_id != episode.episode_id:
            raise CounterfactualContractError("segment episode_id must match episode.")
        if segment.target_key != episode.target_key:
            raise CounterfactualContractError("segment target_key must match episode.")


__all__ = ["build_counterfactual_evaluation_report"]
