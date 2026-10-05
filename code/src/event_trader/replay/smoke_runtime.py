"""Deterministic primary-research callbacks for replay smoke runs."""

from __future__ import annotations

from event_trader.analysis import AnalysisContext
from event_trader.checker.context_pack import CheckerContextPack
from event_trader.checker.validator import RawCheckerDecision
from event_trader.contracts import AnalysisResult


def deterministic_replay_smoke_checker_policy(
    pack: CheckerContextPack,
) -> RawCheckerDecision:
    """Escalate deterministically so replay smoke can traverse primary research."""

    return RawCheckerDecision(
        target_key=pack.request.target_key,
        event_ids=list(pack.request.event_ids),
        decision="escalate",
        rationale=(
            "Deterministic replay smoke escalated the admitted evidence so the "
            "existing replay runtime can exercise checker-to-analysis routing "
            "without external LLM dependencies."
        ),
        attention_hint=(
            "Run deterministic replay smoke analysis for this admitted evidence."
        ),
        requires_watchlist_maintenance=False,
        uncertainty=False,
        possible_watchlist_trigger=False,
        no_action_support=None,
    )


def timebatch_passthrough_checker_policy(
    pack: CheckerContextPack,
) -> RawCheckerDecision:
    """Escalate fixed calendar time-batches without calling checker LLM."""

    return RawCheckerDecision(
        target_key=pack.request.target_key,
        event_ids=list(pack.request.event_ids),
        decision="escalate",
        rationale=(
            "Calendar time-batch replay bypassed checker filtering: every fixed-clock "
            "batch is forwarded to production analysis."
        ),
        attention_hint=(
            "Analyze this fixed calendar time-batch as the complete visible evidence "
            "packet for its interval."
        ),
        requires_watchlist_maintenance=False,
        uncertainty=False,
        possible_watchlist_trigger=False,
        no_action_support=None,
    )


def per_event_passthrough_checker_policy(
    pack: CheckerContextPack,
) -> RawCheckerDecision:
    """Escalate every admitted evidence record without checker LLM filtering."""

    return RawCheckerDecision(
        target_key=pack.request.target_key,
        event_ids=list(pack.request.event_ids),
        decision="escalate",
        rationale=(
            "Per-event replay bypassed checker filtering: every admitted evidence "
            "record is forwarded immediately to production analysis."
        ),
        attention_hint=(
            "Analyze this single admitted evidence record without calendar batching "
            "or CEAU maturity waiting."
        ),
        requires_watchlist_maintenance=False,
        uncertainty=False,
        possible_watchlist_trigger=False,
        no_action_support=None,
    )


def deterministic_replay_smoke_analysis_callback(
    context: AnalysisContext,
) -> AnalysisResult:
    """Return a no-update analysis result without leaving the replay seam."""

    return AnalysisResult(
        target_key=context.request.target_key,
        event_ids=list(context.request.event_ids),
        outcome="no_update",
    )


__all__ = [
    "deterministic_replay_smoke_analysis_callback",
    "deterministic_replay_smoke_checker_policy",
    "per_event_passthrough_checker_policy",
    "timebatch_passthrough_checker_policy",
]
