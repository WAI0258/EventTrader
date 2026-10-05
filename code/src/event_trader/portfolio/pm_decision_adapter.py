"""Adapter from PM judgment outputs to persisted PMDecision records."""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256

from event_trader.contracts.pm_review_reason import PMReviewReason
from event_trader.contracts.view_state_change import canonical_view_state_target_weight
from event_trader.pm_review.contracts import (
    PMDecisionDraft,
    PMReviewInput,
    PMReviewRequest,
    requested_state_allowed_for_execution_direction_mode,
)
from event_trader.portfolio.contracts import PMDecision
from event_trader.portfolio.store import PMDecisionStore
from event_trader.storage import WorkspaceLayout


class PMDecisionAdapterError(ValueError):
    """Raised when PM judgment cannot become a PMDecision."""


_EXPLICIT_HOLD_REVIEW_REASONS: frozenset[PMReviewReason] = frozenset(
    {
        "current_exposure_pressure",
        "risk_reward_compression",
        "invalidation_touched",
    }
)


def build_pm_decision_from_draft(
    *,
    request: PMReviewRequest,
    pm_review_input: PMReviewInput,
    decision_draft: PMDecisionDraft,
    decision_available_at: datetime,
) -> PMDecision:
    """Build the active PMDecision from a thin PMDecisionDraft."""

    _validate_request_and_draft(
        request=request,
        pm_review_input=pm_review_input,
        decision_draft=decision_draft,
    )
    _validate_decision_available_at(
        request=request,
        decision_available_at=decision_available_at,
    )
    source_event_ids = (
        decision_draft.cited_event_ids
        if decision_draft.cited_event_ids
        else request.source_event_ids
    )
    requested_target_weight = canonical_view_state_target_weight(decision_draft.requested_state)
    return PMDecision(
        decision_id=_derive_pm_review_decision_id(
            request=request,
            pm_review_input=pm_review_input,
            decision_draft=decision_draft,
        ),
        decision_episode_id=_derive_decision_episode_id(
            request=request,
            pm_review_input=pm_review_input,
            decision_draft=decision_draft,
        ),
        pm_review_request_id=request.request_id,
        target_key=request.target_key,
        business_at=request.business_at,
        decision_available_at=decision_available_at,
        source_event_ids=source_event_ids,
        actual_state_before_decision=pm_review_input.actual_current_state,
        actual_target_weight_before_decision=pm_review_input.actual_target_weight_before,
        execution_required=(
            requested_target_weight != pm_review_input.actual_target_weight_before
        ),
        requested_state=decision_draft.requested_state,
        requested_target_weight=requested_target_weight,
        rationale_md=decision_draft.rationale_md,
        fallback_state_if_clamped=decision_draft.fallback_state_if_clamped,
        fallback_rationale_md=decision_draft.fallback_rationale_md,
    )


def append_pm_decision_from_draft(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest,
    pm_review_input: PMReviewInput,
    decision_draft: PMDecisionDraft,
    decision_available_at: datetime,
) -> PMDecision:
    """Append the active PMDecision derived from a PMDecisionDraft."""

    decision = build_pm_decision_from_draft(
        request=request,
        pm_review_input=pm_review_input,
        decision_draft=decision_draft,
        decision_available_at=decision_available_at,
    )
    PMDecisionStore(layout).append(decision)
    return decision


def _validate_request_and_draft(
    *,
    request: PMReviewRequest,
    pm_review_input: PMReviewInput,
    decision_draft: PMDecisionDraft,
) -> None:
    if not isinstance(request, PMReviewRequest):
        raise PMDecisionAdapterError("request must be a PMReviewRequest instance.")
    if not isinstance(pm_review_input, PMReviewInput):
        raise PMDecisionAdapterError("pm_review_input must be a PMReviewInput instance.")
    if not isinstance(decision_draft, PMDecisionDraft):
        raise PMDecisionAdapterError(
            "decision_draft must be a PMDecisionDraft instance."
        )
    if pm_review_input.pm_review_request_id != request.request_id:
        raise PMDecisionAdapterError(
            "pm_review_input.pm_review_request_id must match request.request_id."
        )
    if pm_review_input.target_key != request.target_key:
        raise PMDecisionAdapterError(
            "pm_review_input.target_key must match request.target_key."
        )
    if pm_review_input.business_at != request.business_at:
        raise PMDecisionAdapterError(
            "pm_review_input.business_at must match request.business_at."
        )
    if decision_draft.pm_review_request_id != request.request_id:
        raise PMDecisionAdapterError(
            "decision_draft.pm_review_request_id must match request.request_id."
        )
    if decision_draft.target_key != request.target_key:
        raise PMDecisionAdapterError(
            "decision_draft.target_key must match request.target_key."
        )
    if decision_draft.business_at != request.business_at:
        raise PMDecisionAdapterError(
            "decision_draft.business_at must match request.business_at."
        )
    if decision_draft.requested_state != "flat" and not decision_draft.cited_event_ids:
        raise PMDecisionAdapterError(
            "non-flat PMDecisionDraft requires cited_event_ids for PMDecision."
        )
    if not requested_state_allowed_for_execution_direction_mode(
        decision_draft.requested_state,
        pm_review_input.execution_direction_mode,
    ):
        raise PMDecisionAdapterError(
            "PMDecisionDraft.requested_state is not allowed for target execution_direction_mode "
            f"{pm_review_input.execution_direction_mode!r}: {decision_draft.requested_state!r}."
        )
    if _requires_explicit_hold_reason(
        pm_review_input=pm_review_input,
        decision_draft=decision_draft,
    ):
        raise PMDecisionAdapterError(
            "PMDecisionDraft must provide hold_reason_code when current-exposure "
            "review reasons require an explicit hold justification and requested_state "
            "keeps actual_current_state unchanged."
        )


def _validate_decision_available_at(
    *,
    request: PMReviewRequest,
    decision_available_at: datetime,
) -> None:
    if not isinstance(decision_available_at, datetime) or decision_available_at.tzinfo is None:
        raise PMDecisionAdapterError(
            "decision_available_at must be a timezone-aware datetime."
        )
    if decision_available_at < request.business_at:
        raise PMDecisionAdapterError(
            "decision_available_at must be at or after request.business_at."
        )


def _requires_explicit_hold_reason(
    *,
    pm_review_input: PMReviewInput,
    decision_draft: PMDecisionDraft,
) -> bool:
    if not pm_review_input.current_exposure_required:
        return False
    if decision_draft.requested_state != pm_review_input.actual_current_state:
        return False
    if decision_draft.hold_reason_code is not None:
        return False
    return any(
        reason in _EXPLICIT_HOLD_REVIEW_REASONS
        for reason in pm_review_input.review_reasons
    )


def _derive_decision_episode_id(
    *,
    request: PMReviewRequest,
    pm_review_input: PMReviewInput,
    decision_draft: PMDecisionDraft,
) -> str:
    if request.source == "open_position_material_update" and request.source_episode_id:
        actual_direction = _position_direction(pm_review_input.actual_current_state)
        requested_direction = _position_direction(decision_draft.requested_state)
        if requested_direction is None or requested_direction == actual_direction:
            return request.source_episode_id
    digest = sha256(request.request_id.encode("utf-8")).hexdigest()[:24]
    return f"decision-episode:pm-review:{digest}"


def _position_direction(state: str) -> str | None:
    if state in {"weak_long", "strong_long"}:
        return "long"
    if state in {"weak_short", "strong_short"}:
        return "short"
    return None


def _derive_pm_review_decision_id(
    *,
    request: PMReviewRequest,
    pm_review_input: PMReviewInput,
    decision_draft: PMDecisionDraft,
) -> str:
    requested_target_weight = canonical_view_state_target_weight(decision_draft.requested_state)
    payload = {
        "pm_review_request_id": request.request_id,
        "target_key": request.target_key,
        "business_at": request.business_at.isoformat(),
        "actual_state_before_decision": pm_review_input.actual_current_state,
        "actual_target_weight_before_decision": pm_review_input.actual_target_weight_before,
        "execution_required": requested_target_weight
        != pm_review_input.actual_target_weight_before,
        "requested_state": decision_draft.requested_state,
        "requested_target_weight": requested_target_weight,
        "cited_event_ids": list(decision_draft.cited_event_ids),
        "cited_market_bar_ids": list(decision_draft.cited_market_bar_ids),
        "fallback_state_if_clamped": decision_draft.fallback_state_if_clamped,
        "fallback_rationale_md": decision_draft.fallback_rationale_md,
        "hold_reason_code": decision_draft.hold_reason_code,
    }
    digest = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]
    return f"pm-decision:{digest}"


__all__ = [
    "PMDecisionAdapterError",
    "append_pm_decision_from_draft",
    "build_pm_decision_from_draft",
]
