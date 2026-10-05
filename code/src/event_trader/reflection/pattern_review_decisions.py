"""Thin deterministic cross-review decisions for cadence pattern reviews."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .cadence_aggregation import CadenceAggregationReceipt
from .cadence_reviews import CadenceReviewWindowReceipt


type CadencePatternSignal = Literal[
    "insufficient_signal",
    "recurring_pattern",
    "strong_method_signal",
]


class ReflectionPatternReviewDecisionError(ValueError):
    """Raised when cadence pattern-review decisions cannot be normalized."""


@dataclass(frozen=True, slots=True)
class CadencePatternReviewDecision:
    """Thin audit decision for one cadence review window."""

    pattern_signal: CadencePatternSignal
    decision_summary: str

    def __post_init__(self) -> None:
        if self.pattern_signal not in {
            "insufficient_signal",
            "recurring_pattern",
            "strong_method_signal",
        }:
            raise ReflectionPatternReviewDecisionError(
                "pattern_signal must be one of the supported audit signals."
            )
        if not isinstance(self.decision_summary, str) or not self.decision_summary.strip():
            raise ReflectionPatternReviewDecisionError(
                "decision_summary must be a non-blank string."
            )


def decide_cadence_pattern_review(
    *,
    cadence_window: CadenceReviewWindowReceipt,
    aggregation: CadenceAggregationReceipt,
) -> CadencePatternReviewDecision:
    """Decide which cross-review accumulation surface should advance."""

    if not isinstance(cadence_window, CadenceReviewWindowReceipt):
        raise ReflectionPatternReviewDecisionError(
            "cadence_window must be a CadenceReviewWindowReceipt instance."
        )
    if not isinstance(aggregation, CadenceAggregationReceipt):
        raise ReflectionPatternReviewDecisionError(
            "aggregation must be a CadenceAggregationReceipt instance."
        )
    if cadence_window.window_start != aggregation.window_start:
        raise ReflectionPatternReviewDecisionError(
            "cadence_window.window_start must match aggregation.window_start."
        )
    if cadence_window.window_end != aggregation.window_end:
        raise ReflectionPatternReviewDecisionError(
            "cadence_window.window_end must match aggregation.window_end."
        )

    completed_reflection_count = len(aggregation.completed_reflections)
    if cadence_window.review_count != completed_reflection_count:
        raise ReflectionPatternReviewDecisionError(
            "cadence_window.review_count must match the number of aggregated "
            "completed reflections."
        )
    distinct_target_count = len(
        {reflection.target_key for reflection in aggregation.completed_reflections}
    )

    if cadence_window.skipped or distinct_target_count < 2:
        return CadencePatternReviewDecision(
            pattern_signal="insufficient_signal",
            decision_summary=(
                "Cadence pattern review is deferred because the completed reflections "
                "do not yet show enough diversified cross-review signal."
            ),
        )

    strong_cross_review_signal = (
        cadence_window.window_kind == "monthly"
        and cadence_window.review_count >= max(cadence_window.min_required_reviews + 2, 4)
        and distinct_target_count >= 3
    )
    if strong_cross_review_signal:
        return CadencePatternReviewDecision(
            pattern_signal="strong_method_signal",
            decision_summary=(
                "Cadence pattern review found a stronger cross-review method signal, "
                "but direct shared markdown and prompt-asset writeback is retired."
            ),
        )

    return CadencePatternReviewDecision(
        pattern_signal="recurring_pattern",
        decision_summary=(
            "Cadence pattern review found diversified recurring patterns, so it "
            "records audit metadata without writing shared markdown or prompt assets."
        ),
    )


__all__ = [
    "CadencePatternSignal",
    "CadencePatternReviewDecision",
    "ReflectionPatternReviewDecisionError",
    "decide_cadence_pattern_review",
]
