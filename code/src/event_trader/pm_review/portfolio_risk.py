"""Deterministic current-segment portfolio risk for PMReview."""

from __future__ import annotations

from collections.abc import Sequence

from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketMapping,
    PositionSegment,
    ViewEpisode,
    ViewStateChange,
)
from event_trader.execution import ExecutionRecord
from event_trader.market.adjustments import MarketDataAdjustmentPolicy
from event_trader.pm_review.contracts import (
    PMPortfolioRiskEntrySource,
    PMPortfolioRiskSnapshot,
    PMReviewRequest,
)
from event_trader.portfolio.active_exposure import ActiveExposure
from event_trader.validation.execution_linkage import execution_record_by_state_change
from event_trader.validation.returns import ValidationReturnsError, calculate_mark_return


class PMPortfolioRiskError(ValueError):
    """Raised when current-segment risk cannot be built without mixing bases."""


def build_pm_portfolio_risk_snapshot(
    *,
    request: PMReviewRequest,
    actual_exposure: ActiveExposure,
    open_episode: ViewEpisode | None,
    open_segment: PositionSegment | None,
    opening_state_change: ViewStateChange | None,
    market_mapping: MarketMapping,
    market_bars: Sequence[MarketDataBar],
    execution_records: Sequence[ExecutionRecord] = (),
    adjustment_policy: MarketDataAdjustmentPolicy | None = None,
) -> PMPortfolioRiskSnapshot | None:
    """Build risk from the current validation segment on one active price basis."""

    if actual_exposure.state == "flat":
        return None
    _validate_current_segment(
        request=request,
        actual_exposure=actual_exposure,
        open_episode=open_episode,
        open_segment=open_segment,
        opening_state_change=opening_state_change,
        market_mapping=market_mapping,
    )
    if open_episode is None or open_segment is None or opening_state_change is None:
        raise PMPortfolioRiskError(
            "non-flat portfolio risk requires an open episode, segment, and opening state-change."
        )
    if adjustment_policy not in {None, "forward_adjusted_visible"}:
        raise PMPortfolioRiskError(
            "portfolio risk accepts only raw or forward_adjusted_visible market bars."
        )

    visible_bars = tuple(
        sorted(
            (
                bar
                for bar in market_bars
                if bar.start_at >= open_segment.opened_at
                and bar.end_at <= request.max_visible_market_time
            ),
            key=lambda bar: (bar.start_at, bar.end_at),
        )
    )
    if not visible_bars:
        raise PMPortfolioRiskError(
            "current segment has no active-basis market bars inside the visibility boundary."
        )

    entry_at = visible_bars[0].start_at
    entry_price = visible_bars[0].open_price
    entry_source: PMPortfolioRiskEntrySource = "active_basis_market_bar"
    entry_execution = _same_basis_opening_execution(
        target_key=request.target_key,
        opening_state_change=opening_state_change,
        market_mapping=market_mapping,
        execution_records=execution_records,
        adjustment_policy=adjustment_policy,
    )
    if entry_execution is not None:
        if entry_execution.executed_at is None or entry_execution.adjusted_price is None:
            raise PMPortfolioRiskError(
                "linked same-basis execution is missing its execution time or adjusted price."
            )
        if entry_execution.executed_at != open_segment.opened_at:
            raise PMPortfolioRiskError(
                "linked same-basis execution must align exactly with the current segment opening."
            )
        if entry_execution.executed_at > request.max_visible_market_time:
            raise PMPortfolioRiskError(
                "linked same-basis execution exceeds the market visibility boundary."
            )
        if entry_execution.target_weight != open_segment.target_weight:
            raise PMPortfolioRiskError(
                "linked same-basis execution target_weight must match the current segment."
            )
        entry_at = entry_execution.executed_at
        entry_price = entry_execution.adjusted_price
        entry_source = "same_basis_execution"

    path_bars = tuple(bar for bar in visible_bars if bar.end_at >= entry_at)
    if not path_bars:
        raise PMPortfolioRiskError(
            "current segment has no visible mark at or after its entry reference."
        )
    current_bar = path_bars[-1]
    try:
        current_return = calculate_mark_return(
            entry_price=entry_price,
            mark_price=current_bar.close_price,
            target_weight=open_segment.target_weight,
        )
        path_strategy_returns = tuple(
            calculate_mark_return(
                entry_price=entry_price,
                mark_price=bar.close_price,
                target_weight=open_segment.target_weight,
            ).strategy_return
            for bar in path_bars
        )
    except ValidationReturnsError as exc:
        raise PMPortfolioRiskError(str(exc)) from exc
    best_strategy_return = max((0.0, *path_strategy_returns))
    return PMPortfolioRiskSnapshot(
        episode_id=open_episode.episode_id,
        segment_id=open_segment.segment_id,
        instrument_basis=market_mapping.market_symbol,
        segment_opened_at=open_segment.opened_at,
        entry_reference_at=entry_at,
        entry_reference_price=entry_price,
        entry_source=entry_source,
        target_weight=open_segment.target_weight,
        current_mark_price=current_bar.close_price,
        mark_as_of=current_bar.end_at,
        underlying_return=current_return.underlying_return,
        strategy_return=current_return.strategy_return,
        drawdown_from_best_strategy_return=max(
            best_strategy_return - current_return.strategy_return,
            0.0,
        ),
    )


def _validate_current_segment(
    *,
    request: PMReviewRequest,
    actual_exposure: ActiveExposure,
    open_episode: ViewEpisode | None,
    open_segment: PositionSegment | None,
    opening_state_change: ViewStateChange | None,
    market_mapping: MarketMapping,
) -> None:
    if market_mapping.target_key != request.target_key:
        raise PMPortfolioRiskError("market mapping target_key must match request.")
    if actual_exposure.target_key != request.target_key:
        raise PMPortfolioRiskError("actual exposure target_key must match request.")
    if open_episode is None or open_segment is None or opening_state_change is None:
        raise PMPortfolioRiskError(
            "non-flat portfolio risk requires an open episode, segment, and opening state-change."
        )
    if open_episode.target_key != request.target_key:
        raise PMPortfolioRiskError("open episode target_key must match request.")
    if open_segment.target_key != request.target_key:
        raise PMPortfolioRiskError("open segment target_key must match request.")
    if open_segment.episode_id != open_episode.episode_id:
        raise PMPortfolioRiskError("open segment must belong to the open episode.")
    if open_segment.opened_by_state_change_id != opening_state_change.state_change_id:
        raise PMPortfolioRiskError(
            "open segment must reference the supplied opening state-change."
        )
    if opening_state_change.target_key != request.target_key:
        raise PMPortfolioRiskError("opening state-change target_key must match request.")
    if open_segment.target_weight != actual_exposure.target_weight:
        raise PMPortfolioRiskError(
            "open segment target_weight must match the active exposure."
        )
    if opening_state_change.source_kind == "basis_handover" and (
        opening_state_change.instrument_basis != market_mapping.market_symbol
    ):
        raise PMPortfolioRiskError(
            "basis_handover successor basis must match the active market mapping."
        )


def _same_basis_opening_execution(
    *,
    target_key: str,
    opening_state_change: ViewStateChange,
    market_mapping: MarketMapping,
    execution_records: Sequence[ExecutionRecord],
    adjustment_policy: MarketDataAdjustmentPolicy | None,
) -> ExecutionRecord | None:
    if opening_state_change.source_kind != "pm_execution_sidecar":
        return None
    if adjustment_policy is not None:
        return None
    linked = execution_record_by_state_change(
        target_key=target_key,
        state_changes=(opening_state_change,),
        execution_records=execution_records,
    ).get(opening_state_change.state_change_id)
    if linked is None or linked.status != "executed" or linked.provenance is None:
        return None
    if linked.provenance.market_symbol != market_mapping.market_symbol:
        return None
    return linked


__all__ = [
    "PMPortfolioRiskError",
    "build_pm_portfolio_risk_snapshot",
]
