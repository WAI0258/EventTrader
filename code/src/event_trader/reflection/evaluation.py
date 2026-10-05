"""Trader-style reflection evaluation outputs with explicit review write decisions."""

from __future__ import annotations

from dataclasses import dataclass
from math import isclose

from .contracts import (
    ReflectionContextPacket,
    ReflectionEvaluationResult,
    ReflectionHorizonAssessment,
    ReflectionReviewDecision,
    ReflectionThesisAssessment,
    ReflectionTradeAssessment,
)


class ReflectionEvaluationError(ValueError):
    """Raised when reflection evaluation inputs are inconsistent or incomplete."""


@dataclass(frozen=True, slots=True)
class ReflectionEvaluationInput:
    """Structured assessment inputs used to evaluate one reflection review anchor."""

    context: ReflectionContextPacket
    horizon_assessments: tuple[ReflectionHorizonAssessment, ...]
    thesis_assessment: ReflectionThesisAssessment
    trade_assessment: ReflectionTradeAssessment | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.context, ReflectionContextPacket):
            raise ReflectionEvaluationError(
                "context must be a ReflectionContextPacket instance."
            )
        if (
            not isinstance(self.horizon_assessments, tuple)
            or not self.horizon_assessments
        ):
            raise ReflectionEvaluationError(
                "horizon_assessments must be a non-empty tuple of "
                "ReflectionHorizonAssessment values."
            )
        for assessment in self.horizon_assessments:
            if not isinstance(assessment, ReflectionHorizonAssessment):
                raise ReflectionEvaluationError(
                    "horizon_assessments must contain only ReflectionHorizonAssessment instances."
                )
        if not isinstance(self.thesis_assessment, ReflectionThesisAssessment):
            raise ReflectionEvaluationError(
                "thesis_assessment must be a ReflectionThesisAssessment instance."
            )
        if self.context.trade_context is None and self.trade_assessment is not None:
            raise ReflectionEvaluationError(
                "trade_assessment requires context.trade_context to be present."
            )
        if self.context.trade_context is not None and self.trade_assessment is None:
            raise ReflectionEvaluationError(
                "trade_assessment is required when context.trade_context is present."
            )
        if self.trade_assessment is not None and not isinstance(
            self.trade_assessment,
            ReflectionTradeAssessment,
        ):
            raise ReflectionEvaluationError(
                "trade_assessment must be a ReflectionTradeAssessment instance when provided."
            )


def evaluate_reflection_review(
    *,
    context: ReflectionContextPacket,
    horizon_assessments: tuple[ReflectionHorizonAssessment, ...],
    thesis_assessment: ReflectionThesisAssessment,
    trade_assessment: ReflectionTradeAssessment | None = None,
) -> ReflectionEvaluationResult:
    """Return a typed trader-style reflection evaluation for one selected anchor.

    The evaluator is intentionally narrow. It does not rewrite thesis pages or
    perform a second analysis pass. Instead it accepts explicit horizon, thesis,
    and optional trade assessments, then decides whether the review signal is
    strong enough to justify writing a reflection-owned review episode.
    """

    evaluation_input = ReflectionEvaluationInput(
        context=context,
        horizon_assessments=horizon_assessments,
        thesis_assessment=thesis_assessment,
        trade_assessment=trade_assessment,
    )
    review_decision, rationale = _decide_review_surface(
        horizon_assessments=evaluation_input.horizon_assessments,
        thesis_assessment=evaluation_input.thesis_assessment,
        trade_assessment=evaluation_input.trade_assessment,
    )
    return ReflectionEvaluationResult(
        anchor=evaluation_input.context.anchor,
        coverage=evaluation_input.context.coverage,
        assessed_horizons=evaluation_input.horizon_assessments,
        thesis_assessment=evaluation_input.thesis_assessment,
        trade_assessment=evaluation_input.trade_assessment,
        review_decision=review_decision,
        decision_rationale=rationale,
    )


def _decide_review_surface(
    *,
    horizon_assessments: tuple[ReflectionHorizonAssessment, ...],
    thesis_assessment: ReflectionThesisAssessment,
    trade_assessment: ReflectionTradeAssessment | None,
) -> tuple[ReflectionReviewDecision, str]:
    concrete_horizon_signal = any(
        _has_concrete_horizon_signal(assessment) for assessment in horizon_assessments
    )
    concrete_thesis_signal = thesis_assessment.verdict != "unclear"
    concrete_trade_signal = (
        trade_assessment is not None
        and trade_assessment.verdict not in {"not_taken", "open"}
    )

    assessed_horizons_hours = [
        assessment.horizon_hours for assessment in horizon_assessments
    ]
    concrete_components: list[str] = []
    if concrete_horizon_signal:
        concrete_components.append("return/path-tradability")
    if concrete_thesis_signal:
        concrete_components.append("thesis validation")
    if concrete_trade_signal:
        concrete_components.append("trade outcome")

    if concrete_components:
        joined_components = ", ".join(concrete_components)
        return (
            "write_review",
            "Assessed horizons "
            f"{assessed_horizons_hours} and found concrete review value in "
            f"{joined_components}.",
        )

    return (
        "skip_review",
        "Assessed horizons "
        f"{assessed_horizons_hours} but found no concrete return, "
        "path/tradability, thesis, or trade signal strong enough to justify a "
        "review episode.",
    )


def _has_concrete_horizon_signal(assessment: ReflectionHorizonAssessment) -> bool:
    return (
        not isclose(assessment.return_pct, 0.0, abs_tol=1e-9)
        or assessment.path_verdict != "unclear"
        or assessment.tradability_verdict != "unclear"
    )


__all__ = [
    "ReflectionEvaluationError",
    "ReflectionEvaluationInput",
    "evaluate_reflection_review",
]
