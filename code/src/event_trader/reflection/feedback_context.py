"""Reflection-owned builders for validation feedback facts.

This module keeps orchestration near reflection while preserving ownership:
validation computes portfolio feedback facts, market owns decision-time market
facts, and reflection receives both for later interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from event_trader.contracts.view_state_change import (
    MarketDataSeries,
    MarketMapping,
    ViewStateChange,
)
from event_trader.execution.contracts import ExecutionRecord
from event_trader.market.adjustments import MarketAdjustmentSidecar
from event_trader.validation.portfolio_feedback import (
    PortfolioFeedbackError,
    PortfolioFeedbackSnapshot,
    build_default_portfolio_feedback_config,
    build_portfolio_feedback_run_id,
    build_portfolio_feedback_snapshot,
)
from event_trader.validation.returns import (
    OpenEpisodeTerminalMark,
    ValidationReturnsResult,
)

from .market_context_usage import (
    MarketContextReceiptReader,
    MarketContextUsageFacts,
    build_market_context_usage_facts,
)


class ReflectionFeedbackContextError(ValueError):
    """Raised when reflection feedback facts cannot be built."""


@dataclass(frozen=True, slots=True)
class ReflectionFeedbackContextFacts:
    """Validation and usage facts passed into reflection context."""

    portfolio_feedback: PortfolioFeedbackSnapshot
    market_context_usage: MarketContextUsageFacts


def build_reflection_feedback_context_facts(
    *,
    target_key: str,
    episode_id: str,
    state_changes: tuple[ViewStateChange, ...],
    market_mapping: MarketMapping,
    market_data: MarketDataSeries,
    window_start: datetime,
    window_end: datetime,
    read_analysis_outcomes: MarketContextReceiptReader,
    read_checker_receipts: MarketContextReceiptReader,
    market_returns: ValidationReturnsResult | None = None,
    terminal_mark: OpenEpisodeTerminalMark | None = None,
    execution_records: tuple[ExecutionRecord, ...] = (),
    require_execution_records: bool = False,
    adjustment_sidecar: MarketAdjustmentSidecar | None = None,
) -> ReflectionFeedbackContextFacts:
    """Build the deterministic feedback bundle reflection consumes."""
    try:
        portfolio_feedback = build_portfolio_feedback_snapshot(
            target_key=target_key,
            run_id=build_portfolio_feedback_run_id(
                prefix="reflection",
                episode_id=episode_id,
                window_end=window_end,
            ),
            state_changes=state_changes,
            market_data=market_data,
            window_start=window_start,
            window_end=window_end,
            config=build_default_portfolio_feedback_config(
                market_mapping=market_mapping,
            ),
            execution_records=execution_records,
            require_execution_records=require_execution_records,
            adjustment_sidecar=adjustment_sidecar,
        )
    except PortfolioFeedbackError as exc:
        raise ReflectionFeedbackContextError(str(exc)) from exc

    return ReflectionFeedbackContextFacts(
        portfolio_feedback=portfolio_feedback,
        market_context_usage=build_market_context_usage_facts(
            target_key=target_key,
            state_changes=state_changes,
            read_analysis_outcomes=read_analysis_outcomes,
            read_checker_receipts=read_checker_receipts,
            market_returns=market_returns,
            terminal_mark=terminal_mark,
            portfolio_feedback=portfolio_feedback,
        ),
    )


__all__ = [
    "ReflectionFeedbackContextError",
    "ReflectionFeedbackContextFacts",
    "build_reflection_feedback_context_facts",
]
