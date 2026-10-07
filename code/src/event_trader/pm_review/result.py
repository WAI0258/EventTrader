"""Provider-neutral result of one completed PMReview business run."""

from __future__ import annotations

from dataclasses import dataclass

from event_trader.pm_review.contracts import (
    PMDecisionDraft,
    PMPositionReviewTriggers,
    PMReviewInput,
    PMReviewToolReadReceipt,
)
from event_trader.portfolio.contracts import PMDecision
from event_trader.reasoning.runtime import AgentRunReceipt


@dataclass(frozen=True, slots=True)
class PMReviewRunResult:
    pm_review_input: PMReviewInput
    decision_draft: PMDecisionDraft
    pm_decision: PMDecision
    position_review_triggers: PMPositionReviewTriggers | None
    agent_run_receipt: AgentRunReceipt
    read_receipts: tuple[PMReviewToolReadReceipt, ...]


__all__ = ["PMReviewRunResult"]
