"""Thin cadence orchestration for cross-review learning accumulation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from event_trader.storage import WorkspaceLayout

from .cadence_aggregation import (
    CadenceAggregationReceipt,
    aggregate_completed_target_reflections,
)
from .cadence_reviews import (
    CadenceReviewWindowKind,
    CadenceReviewWindowReceipt,
    decide_cadence_review_window,
    select_complete_cadence_window,
)
from .pattern_review_decisions import (
    CadencePatternReviewDecision,
    decide_cadence_pattern_review,
)


class ReflectionCadenceOrchestrationError(ValueError):
    """Raised when cadence orchestration receipts drift out of sync."""


@dataclass(frozen=True, slots=True)
class CadencePatternReviewReceipt:
    """Thin composed receipt for one cadence pattern-review orchestration run."""

    cadence_window: CadenceReviewWindowReceipt
    aggregation: CadenceAggregationReceipt
    decision: CadencePatternReviewDecision

    def __post_init__(self) -> None:
        if not isinstance(self.cadence_window, CadenceReviewWindowReceipt):
            raise ReflectionCadenceOrchestrationError(
                "cadence_window must be a CadenceReviewWindowReceipt instance."
            )
        if not isinstance(self.aggregation, CadenceAggregationReceipt):
            raise ReflectionCadenceOrchestrationError(
                "aggregation must be a CadenceAggregationReceipt instance."
            )
        if not isinstance(self.decision, CadencePatternReviewDecision):
            raise ReflectionCadenceOrchestrationError(
                "decision must be a CadencePatternReviewDecision instance."
            )
        if self.cadence_window.window_start != self.aggregation.window_start:
            raise ReflectionCadenceOrchestrationError(
                "cadence_window.window_start must match aggregation.window_start."
            )
        if self.cadence_window.window_end != self.aggregation.window_end:
            raise ReflectionCadenceOrchestrationError(
                "cadence_window.window_end must match aggregation.window_end."
            )


def run_cadence_pattern_review(
    *,
    layout: WorkspaceLayout,
    window_kind: CadenceReviewWindowKind,
    checked_at: datetime,
    min_required_reviews: int,
) -> CadencePatternReviewReceipt:
    """Run one thin cadence review and return audit metadata only."""

    if not isinstance(layout, WorkspaceLayout):
        raise ReflectionCadenceOrchestrationError(
            "layout must be a WorkspaceLayout instance."
        )

    window_start, window_end = select_complete_cadence_window(
        window_kind=window_kind,
        checked_at=checked_at,
    )
    aggregation = aggregate_completed_target_reflections(
        layout=layout,
        window_start=window_start,
        window_end=window_end,
    )
    cadence_window = decide_cadence_review_window(
        window_kind=window_kind,
        checked_at=checked_at,
        review_count=len(aggregation.completed_reflections),
        min_required_reviews=min_required_reviews,
    )
    decision = decide_cadence_pattern_review(
        cadence_window=cadence_window,
        aggregation=aggregation,
    )
    return CadencePatternReviewReceipt(
        cadence_window=cadence_window,
        aggregation=aggregation,
        decision=decision,
    )


__all__ = [
    "CadencePatternReviewReceipt",
    "ReflectionCadenceOrchestrationError",
    "run_cadence_pattern_review",
]
