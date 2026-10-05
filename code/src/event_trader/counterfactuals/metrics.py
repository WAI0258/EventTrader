"""Deterministic episode metrics and decision-quality attribution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    PositionSegment,
    ViewEpisode,
)
from event_trader.execution.contracts import ExecutionRecord
from event_trader.validation.returns import ValidationReturnsResult

from .contracts import (
    CounterfactualContractError,
    CounterfactualEpisodeMetrics,
    CounterfactualEvaluationReport,
    DecisionQualityAttribution,
)


@dataclass(frozen=True, slots=True)
class _PathPoint:
    bar: MarketDataBar
    strategy_return: float


def actual_return_from_validation(
    *,
    episode: ViewEpisode,
    validation_returns: ValidationReturnsResult,
) -> float:
    """Read actual episode return from the validation return surface."""
    episode_return = _episode_return(episode=episode, validation_returns=validation_returns)
    return episode_return.strategy_return


def build_episode_metrics(
    *,
    episode: ViewEpisode,
    segments: Sequence[PositionSegment],
    market_data: MarketDataSeries,
    validation_returns: ValidationReturnsResult,
    report: CounterfactualEvaluationReport,
    evaluation_end_at: datetime,
) -> CounterfactualEpisodeMetrics:
    """Calculate deterministic actual-outcome metrics from validation returns."""
    episode_return = _episode_return(episode=episode, validation_returns=validation_returns)
    if episode.closed_at is None:
        raise CounterfactualContractError("episode metrics require a closed episode.")
    ordered_segments = tuple(sorted(segments, key=lambda item: item.opened_at))
    if not ordered_segments:
        raise CounterfactualContractError("episode metrics require at least one segment.")
    opening_weight = ordered_segments[0].target_weight
    bars = _bars_between(
        market_data.bars,
        start_at=episode_return.entry_bar_start_at,
        end_at=episode_return.exit_bar_start_at,
    )
    path = tuple(
        _PathPoint(
            bar=bar,
            strategy_return=opening_weight * (bar.close_price / episode_return.entry_price - 1.0),
        )
        for bar in bars
    )
    max_gain = max(path, key=lambda item: item.strategy_return, default=None)
    max_loss = min(path, key=lambda item: item.strategy_return, default=None)
    return CounterfactualEpisodeMetrics(
        episode_id=episode.episode_id,
        target_key=episode.target_key,
        return_pct=episode_return.strategy_return,
        max_drawdown_pct=None if max_loss is None else max_loss.strategy_return,
        time_to_max_gain_minutes=(
            None
            if max_gain is None
            else _minutes_between(episode.opened_at, max_gain.bar.end_at)
        ),
        time_to_max_loss_minutes=(
            None
            if max_loss is None
            else _minutes_between(episode.opened_at, max_loss.bar.end_at)
        ),
        holding_period_minutes=_minutes_between(episode.opened_at, episode.closed_at),
        bar_count=len(bars),
        hit_by_horizon=evaluation_end_at >= episode.closed_at,
        return_vs_baseline={
            result.baseline_type: (
                None
                if result.baseline_return is None
                else episode_return.strategy_return - result.baseline_return
            )
            for result in report.baseline_results
        },
    )


def build_decision_quality_attribution(
    *,
    episode: ViewEpisode,
    report: CounterfactualEvaluationReport,
    execution_records: Sequence[ExecutionRecord],
) -> DecisionQualityAttribution:
    """Classify deterministic attribution availability without LLM scoring."""
    baselines = {result.baseline_type: result for result in report.baseline_results}
    entry_baselines = (
        baselines["no_trade"],
        baselines["delayed_entry"],
        baselines["buy_and_hold_proxy"],
    )
    exit_baselines = (
        baselines["full_hold"],
        baselines["random_exit"],
        baselines["rule_based_exit"],
    )
    entry_available = all(item.baseline_return is not None for item in entry_baselines)
    exit_available = all(item.baseline_return is not None for item in exit_baselines)
    matching_executions = tuple(
        record
        for record in execution_records
        if record.target_key == episode.target_key and record.status == "executed"
    )
    return DecisionQualityAttribution(
        episode_id=episode.episode_id,
        target_key=episode.target_key,
        entry_quality="available" if entry_available else "unavailable",
        exit_quality="available" if exit_available else "unavailable",
        execution_quality="available" if matching_executions else "unavailable",
        reasons={
            "entry_quality": _comparison_summary(report.actual_return, entry_baselines),
            "exit_quality": _comparison_summary(report.actual_return, exit_baselines),
            "execution_quality": {
                "executed_record_count": len(matching_executions),
            },
        },
    )


def _episode_return(*, episode: ViewEpisode, validation_returns: ValidationReturnsResult):
    if not isinstance(validation_returns, ValidationReturnsResult):
        raise CounterfactualContractError(
            "validation_returns must be a ValidationReturnsResult."
        )
    if len(validation_returns.episode_returns) != 1:
        raise CounterfactualContractError(
            "validation_returns must contain exactly one episode return."
        )
    episode_return = validation_returns.episode_returns[0]
    if episode_return.episode_id != episode.episode_id:
        raise CounterfactualContractError(
            "validation return episode_id must match episode."
        )
    if episode_return.target_key != episode.target_key:
        raise CounterfactualContractError(
            "validation return target_key must match episode."
        )
    return episode_return


def _bars_between(
    bars: tuple[MarketDataBar, ...],
    *,
    start_at: datetime,
    end_at: datetime,
) -> tuple[MarketDataBar, ...]:
    return tuple(bar for bar in bars if start_at <= bar.start_at <= end_at)


def _minutes_between(start_at: datetime, end_at: datetime) -> float:
    return (end_at - start_at).total_seconds() / 60.0


def _comparison_summary(
    actual_return: float,
    baselines,
) -> dict[str, object]:
    available = {
        item.baseline_type: item.baseline_return
        for item in baselines
        if item.baseline_return is not None
    }
    if len(available) != len(tuple(baselines)):
        return {"status": "unavailable", "available_baselines": available}
    best_baseline = max(available.items(), key=lambda item: item[1])
    return {
        "status": "available",
        "actual_return": actual_return,
        "best_baseline_type": best_baseline[0],
        "best_baseline_return": best_baseline[1],
        "actual_minus_best_baseline": actual_return - best_baseline[1],
    }


__all__ = [
    "actual_return_from_validation",
    "build_decision_quality_attribution",
    "build_episode_metrics",
]
