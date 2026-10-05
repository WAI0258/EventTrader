"""Apply typed reflection learning decisions through authorized writers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

from event_trader.decision_memory import (
    DecisionEpisodeRecord,
    FileBackedDecisionEpisodeStore,
    find_episode_ids_for_state_change,
)
from event_trader.episode_memory import (
    EpisodeMemoryContractError,
    EpisodeMemoryDelta,
    EpisodeMemoryDeltaCandidate,
    EpisodeMemoryDeltaSourceRefs,
    EpisodeMemoryPromotionDecisionIntent,
    EpisodeMemoryPromotionWriteReceipt,
    EpisodeMemoryDeltaValidator,
    EpisodeMemoryNoUpdate,
    EpisodeMemoryNoUpdateCandidate,
    EpisodeMemoryStatus,
    EpisodeMemoryStoreError,
    EpisodeMemoryValidationBasis,
    EpisodeMemoryWriteReceipt,
    FileBackedEpisodeMemoryStore,
    LearningCardPromotionIntent,
    NoPromoteIntent,
    episode_memory_record_hash,
    validate_promotion_intent_batch,
)
from event_trader.learning_cards import (
    FileBackedLearningCardStore,
    PromotionDecisionValidator,
    PromotionWriter,
)
from event_trader.storage import WorkspaceLayout

from .contracts import (
    TargetCloseReflectionEvaluationResult,
    TargetReflectionEvaluationResult,
    OpenPositionEvaluationResult,
)
from .learning_contracts import (
    ReflectionLearningAction,
    ReflectionLearningActionReceipt,
    ReflectionLearningReceipt,
)
from .review_writer import (
    OpenPositionMaterialUpdateReviewWriteReceipt,
    TargetCloseReviewWriteReceipt,
    TargetReviewWriteReceipt,
    OpenPositionHorizonReviewWriteReceipt,
)
from .view_contracts import (
    CloseEpisodeReflectionContext,
    EpisodeReflectionContext,
    OpenPositionReflectionContext,
)

type ReflectionLearningContext = (
    EpisodeReflectionContext
    | CloseEpisodeReflectionContext
    | OpenPositionReflectionContext
)
type ReflectionLearningEvaluation = (
    TargetReflectionEvaluationResult
    | TargetCloseReflectionEvaluationResult
    | OpenPositionEvaluationResult
)
type ReflectionLearningReviewReceipt = (
    TargetReviewWriteReceipt
    | TargetCloseReviewWriteReceipt
    | OpenPositionHorizonReviewWriteReceipt
    | OpenPositionMaterialUpdateReviewWriteReceipt
)

class ReflectionLearningLifecycleError(ValueError):
    """Raised when typed reflection learning cannot be applied."""


@dataclass(frozen=True, slots=True)
class _PromotionWriteResult:
    promotion_decision_id: str
    receipt: EpisodeMemoryPromotionWriteReceipt | None = None


def apply_reflection_learning(
    *,
    context: ReflectionLearningContext,
    evaluation: ReflectionLearningEvaluation,
    review_receipt: ReflectionLearningReviewReceipt,
    layout: WorkspaceLayout,
) -> ReflectionLearningReceipt:
    """Apply one reflection learning decision and append episode-memory receipts."""

    _validate_lifecycle_inputs(
        context=context,
        evaluation=evaluation,
        review_receipt=review_receipt,
        layout=layout,
    )
    decision = evaluation.learning_decision
    decision.validate_for_review_decision(evaluation.review_decision)
    action_receipts = tuple(
        _apply_action(
            context=context,
            evaluation=evaluation,
            review_receipt=review_receipt,
            layout=layout,
            action=action,
        )
        for action in decision.actions
    )
    record_path = _append_learning_episode_record(
        context=context,
        evaluation=evaluation,
        review_receipt=review_receipt,
        layout=layout,
        action_receipts=action_receipts,
    )
    return ReflectionLearningReceipt(
        source_episode_id=evaluation.episode_id,
        source_review_path=review_receipt.review_page_path,
        primary_outcome=decision.primary_outcome,
        action_receipts=action_receipts,
        decision_episode_record_path=record_path,
    )


def _apply_action(
    *,
    context: ReflectionLearningContext,
    evaluation: ReflectionLearningEvaluation,
    review_receipt: ReflectionLearningReviewReceipt,
    layout: WorkspaceLayout,
    action: ReflectionLearningAction,
) -> ReflectionLearningActionReceipt:
    try:
        _validate_action_identity(
            context=context,
            evaluation=evaluation,
            review_receipt=review_receipt,
            action=action,
        )
        if action.outcome == "no_learning_write":
            return ReflectionLearningActionReceipt(
                action_id=action.action_id,
                outcome=action.outcome,
                status="skipped",
                episode_id=evaluation.episode_id,
                source_review_path=review_receipt.review_page_path,
                skip_reason=action.rationale,
            )
        if action.outcome == "emit_episode_memory_candidate":
            return _write_episode_memory_candidate(
                context=context,
                evaluation=evaluation,
                review_receipt=review_receipt,
                layout=layout,
                action=action,
            )
    except (
        ReflectionLearningLifecycleError,
        EpisodeMemoryContractError,
        EpisodeMemoryStoreError,
        OSError,
    ) as exc:
        return ReflectionLearningActionReceipt(
            action_id=action.action_id,
            outcome=action.outcome,
            status="failed",
            episode_id=evaluation.episode_id,
            source_review_path=review_receipt.review_page_path,
            failure_reason=str(exc),
        )
    raise ReflectionLearningLifecycleError(
        f"unsupported reflection learning outcome: {action.outcome}"
    )


def _write_episode_memory_candidate(
    *,
    context: ReflectionLearningContext,
    evaluation: ReflectionLearningEvaluation,
    review_receipt: ReflectionLearningReviewReceipt,
    layout: WorkspaceLayout,
    action: ReflectionLearningAction,
) -> ReflectionLearningActionReceipt:
    candidate_kind = _payload_text(action, "candidate_kind")
    if candidate_kind == "delta":
        return _write_episode_memory_delta_candidate(
            context=context,
            evaluation=evaluation,
            review_receipt=review_receipt,
            layout=layout,
            action=action,
        )
    if candidate_kind == "no_update":
        return _write_episode_memory_no_update(
            context=context,
            evaluation=evaluation,
            review_receipt=review_receipt,
            layout=layout,
            action=action,
        )
    raise ReflectionLearningLifecycleError(
        "episode memory candidate_kind must be 'delta' or 'no_update'."
    )


def _write_episode_memory_delta_candidate(
    *,
    context: ReflectionLearningContext,
    evaluation: ReflectionLearningEvaluation,
    review_receipt: ReflectionLearningReviewReceipt,
    layout: WorkspaceLayout,
    action: ReflectionLearningAction,
) -> ReflectionLearningActionReceipt:
    recorded_at = datetime.now(UTC)
    _validate_promotion_payload_scope(context=context, action=action)
    delta_id = _stable_id(
        "reflection-delta",
        action.action_id,
        evaluation.episode_id,
        review_receipt.review_page_path,
    )
    validator_receipt_id = _stable_id("episode-memory-validator", delta_id)
    candidate = EpisodeMemoryDeltaCandidate(
        episode_id=evaluation.episode_id,
        target_key=evaluation.target_key,
        delta_kind=_payload_text(action, "delta_kind"),
        memory_status=cast(
            EpisodeMemoryStatus,
            _payload_text(action, "memory_status"),
        ),
        validation_basis=cast(
            EpisodeMemoryValidationBasis,
            _payload_text(action, "validation_basis"),
        ),
        source_visible_through=_payload_datetime(action, "source_visible_through"),
        usable_from=_payload_datetime(action, "usable_from"),
        source_refs=_candidate_source_refs(
            action=action,
            context=context,
            review_receipt=review_receipt,
        ),
        summary_md=_payload_text(action, "summary_md"),
        thesis_delta=_payload_text(action, "thesis_delta"),
        pm_management_delta=_payload_text(action, "pm_management_delta"),
        risk_delta=_payload_text(action, "risk_delta"),
        invalidation_delta=_payload_text(action, "invalidation_delta"),
        error_attributions=_payload_text_tuple(action, "error_attributions"),
        confidence=_payload_float(action, "confidence"),
        supersedes_delta_ids=_payload_text_tuple(action, "supersedes_delta_ids"),
    )
    validation = EpisodeMemoryDeltaValidator().validate_candidate(
        candidate,
        delta_id=delta_id,
        recorded_at=recorded_at,
        validator_receipt_id=validator_receipt_id,
    )
    store = FileBackedEpisodeMemoryStore(layout)
    delta_path = store.delta_path(
        target_key=evaluation.target_key,
        episode_id=evaluation.episode_id,
    )
    existing_delta_path = _existing_matching_delta_path(
        store=store,
        candidate=candidate,
        delta_id=delta_id,
    )
    if existing_delta_path is not None:
        promotion = _write_required_close_promotion_batch(
            delta=existing_delta_path[1],
            action=action,
            layout=layout,
        )
        return ReflectionLearningActionReceipt(
            action_id=action.action_id,
            outcome=action.outcome,
            status="written",
            episode_id=evaluation.episode_id,
            artifact_path=existing_delta_path[0],
            source_review_path=review_receipt.review_page_path,
            promotion_receipt_id=(
                promotion.receipt.receipt_id
                if promotion is not None and promotion.receipt is not None
                else None
            ),
            promotion_decision_id=(
                promotion.promotion_decision_id if promotion is not None else None
            ),
            promotion_card_id=(
                _promotion_card_id(promotion.receipt)
                if promotion is not None and promotion.receipt is not None
                else None
            ),
            promotion_artifact_path=(
                _promotion_artifact_path(promotion.receipt)
                if promotion is not None and promotion.receipt is not None
                else None
            ),
            promotion_status=(
                promotion.receipt.write_status
                if promotion is not None and promotion.receipt is not None
                else None
            ),
        )
    if not validation.accepted or validation.delta is None:
        failure_reason = validation.receipt.failure_reason or "episode memory validation failed."
        candidate_hash = episode_memory_record_hash(candidate.to_json_payload())
        _append_write_receipt_idempotent(
            store=store,
            receipt=EpisodeMemoryWriteReceipt(
                receipt_id=_stable_id("episode-memory-write", delta_id, "rejected"),
                delta_id=delta_id,
                episode_id=evaluation.episode_id,
                target_key=evaluation.target_key,
                artifact_path=delta_path.as_posix(),
                content_hash=candidate_hash,
                source_visible_through=candidate.source_visible_through,
                usable_from=candidate.usable_from,
                validator_receipt_id=validator_receipt_id,
                status="rejected",
                recorded_at=recorded_at,
                failure_reason=failure_reason,
            ),
        )
        return ReflectionLearningActionReceipt(
            action_id=action.action_id,
            outcome=action.outcome,
            status="failed",
            episode_id=evaluation.episode_id,
            artifact_path=delta_path,
            source_review_path=review_receipt.review_page_path,
            failure_reason=failure_reason,
        )

    written_path = store.append_delta(validation.delta)
    _append_write_receipt_idempotent(
        store=store,
        receipt=EpisodeMemoryWriteReceipt(
            receipt_id=_stable_id("episode-memory-write", validation.delta.delta_id, "stored"),
            delta_id=validation.delta.delta_id,
            episode_id=validation.delta.episode_id,
            target_key=validation.delta.target_key,
            artifact_path=written_path.as_posix(),
            content_hash=validation.delta.content_hash,
            source_visible_through=validation.delta.source_visible_through,
            usable_from=validation.delta.usable_from,
            validator_receipt_id=validator_receipt_id,
            status="stored",
            recorded_at=recorded_at,
            failure_reason=None,
        ),
    )
    promotion = _write_required_close_promotion_batch(
        delta=validation.delta,
        action=action,
        layout=layout,
    )
    return ReflectionLearningActionReceipt(
        action_id=action.action_id,
        outcome=action.outcome,
        status="written",
        episode_id=evaluation.episode_id,
        artifact_path=written_path,
        source_review_path=review_receipt.review_page_path,
        promotion_receipt_id=(
            promotion.receipt.receipt_id
            if promotion is not None and promotion.receipt is not None
            else None
        ),
        promotion_decision_id=(
            promotion.promotion_decision_id if promotion is not None else None
        ),
        promotion_card_id=(
            _promotion_card_id(promotion.receipt)
            if promotion is not None and promotion.receipt is not None
            else None
        ),
        promotion_artifact_path=(
            _promotion_artifact_path(promotion.receipt)
            if promotion is not None and promotion.receipt is not None
            else None
        ),
        promotion_status=(
            promotion.receipt.write_status
            if promotion is not None and promotion.receipt is not None
            else None
        ),
    )


def _write_episode_memory_no_update(
    *,
    context: ReflectionLearningContext,
    evaluation: ReflectionLearningEvaluation,
    review_receipt: ReflectionLearningReviewReceipt,
    layout: WorkspaceLayout,
    action: ReflectionLearningAction,
) -> ReflectionLearningActionReceipt:
    candidate_id = _payload_optional_text(action, "candidate_id") or _stable_id(
        "episode-memory-no-update-candidate",
        action.action_id,
        evaluation.episode_id,
        review_receipt.review_page_path,
    )
    receipt_id = _payload_optional_text(action, "receipt_id") or _stable_id(
        "episode-memory-no-update",
        action.action_id,
        evaluation.episode_id,
        review_receipt.review_page_path,
    )
    candidate = EpisodeMemoryNoUpdateCandidate(
        candidate_id=candidate_id,
        episode_id=evaluation.episode_id,
        target_key=evaluation.target_key,
        review_path=review_receipt.review_page_path,
        business_at=_learning_business_at(context),
        source_visible_through=_payload_datetime(action, "source_visible_through"),
        usable_from=_payload_datetime(action, "usable_from"),
        reason=_payload_optional_text(action, "reason_md")
        or _payload_text(action, "reason"),
        reason_code=_payload_text(action, "reason_code"),
        source_refs=_candidate_source_refs(
            action=action,
            context=context,
            review_receipt=review_receipt,
        ),
        reviewed_delta_ids=_payload_text_tuple(action, "reviewed_delta_ids"),
    )
    no_update = EpisodeMemoryNoUpdate(
        receipt_id=receipt_id,
        episode_id=candidate.episode_id,
        target_key=candidate.target_key,
        review_path=candidate.review_path,
        business_at=candidate.business_at,
        recorded_at=datetime.now(UTC),
        source_visible_through=candidate.source_visible_through,
        usable_from=candidate.usable_from,
        reason=candidate.reason,
        reason_code=candidate.reason_code,
        source_refs=candidate.source_refs,
        reviewed_delta_ids=candidate.reviewed_delta_ids,
    )
    store = FileBackedEpisodeMemoryStore(layout)
    existing_no_update_path = _existing_matching_no_update_path(
        store=store,
        candidate=candidate,
        receipt_id=receipt_id,
    )
    written_path = existing_no_update_path or store.append_no_update(no_update)
    return ReflectionLearningActionReceipt(
        action_id=action.action_id,
        outcome=action.outcome,
        status="written",
        episode_id=evaluation.episode_id,
        artifact_path=written_path,
        source_review_path=review_receipt.review_page_path,
    )


def _existing_matching_delta_path(
    *,
    store: FileBackedEpisodeMemoryStore,
    candidate: EpisodeMemoryDeltaCandidate,
    delta_id: str,
) -> tuple[Path, EpisodeMemoryDelta] | None:
    for persisted in store.read_deltas(
        target_key=candidate.target_key,
        episode_id=candidate.episode_id,
    ):
        if persisted.record.delta_id != delta_id:
            continue
        if _delta_matches_candidate(persisted.record, candidate):
            return persisted.path, persisted.record
        raise ReflectionLearningLifecycleError(
            "deterministic episode memory delta_id already exists with different payload."
        )
    return None


def _validate_promotion_payload_scope(
    *,
    context: ReflectionLearningContext,
    action: ReflectionLearningAction,
) -> None:
    promotion_payload = _payload_optional_mapping(action, "promotion")
    if promotion_payload is None:
        return
    if not isinstance(context, CloseEpisodeReflectionContext):
        raise ReflectionLearningLifecycleError(
            "promotion payload is allowed only for close/finalized reflection."
        )
    if (
        _payload_text(action, "memory_status") != "finalized"
        or _payload_text(action, "validation_basis") != "close_review"
    ):
        raise ReflectionLearningLifecycleError(
            "promotion payload requires a finalized close_review EpisodeMemoryDelta candidate."
        )


def _write_required_close_promotion_batch(
    *,
    delta: EpisodeMemoryDelta,
    action: ReflectionLearningAction,
    layout: WorkspaceLayout,
) -> _PromotionWriteResult | None:
    promotion_payload = _payload_optional_mapping(action, "promotion")
    is_close_finalization = (
        delta.memory_status == "finalized" and delta.validation_basis == "close_review"
    )
    if not is_close_finalization:
        if promotion_payload is not None:
            raise ReflectionLearningLifecycleError(
                "promotion requires a finalized close_review EpisodeMemoryDelta."
            )
        return None
    store = FileBackedEpisodeMemoryStore(layout)
    if promotion_payload is None:
        intent = _no_promote_intent_from_delta(delta=delta, action=action)
        _append_promotion_batch(store=store, intents=(intent,))
        return _PromotionWriteResult(promotion_decision_id=intent.decision_id)
    intent = _promotion_intent_from_delta(
        delta=delta,
        action=action,
        payload=promotion_payload,
    )
    _append_promotion_batch(store=store, intents=(intent,))
    validation = PromotionDecisionValidator(store).validate_intent(
        intent,
        recorded_at=intent.recorded_at,
    )
    if validation.validated is None:
        failure = validation.receipt.failure_reason or "promotion validation failed."
        raise ReflectionLearningLifecycleError(f"promotion validation failed: {failure}")
    receipt = PromotionWriter(
        FileBackedLearningCardStore(layout),
        store,
    ).write(
        validation.validated,
        recorded_at=intent.recorded_at,
    )
    if receipt.write_status != "written":
        failure = receipt.failure_reason or "promotion write failed."
        raise ReflectionLearningLifecycleError(f"promotion write failed: {failure}")
    return _PromotionWriteResult(
        promotion_decision_id=intent.decision_id,
        receipt=receipt,
    )


def _append_promotion_batch(
    *,
    store: FileBackedEpisodeMemoryStore,
    intents: tuple[EpisodeMemoryPromotionDecisionIntent, ...],
) -> None:
    validate_promotion_intent_batch(intents)
    target_key = intents[0].target_key
    episode_id = intents[0].episode_id
    batch_id = intents[0].promotion_batch_id
    combined_by_decision_id: dict[str, EpisodeMemoryPromotionDecisionIntent] = {
        persisted.record.decision_id: persisted.record
        for persisted in store.read_promotion_intents(
            target_key=target_key,
            episode_id=episode_id,
        )
        if persisted.record.promotion_batch_id == batch_id
    }
    for intent in intents:
        existing = combined_by_decision_id.get(intent.decision_id)
        if existing is not None and existing.content_hash != intent.content_hash:
            raise ReflectionLearningLifecycleError(
                "promotion batch decision_id already exists with different payload."
            )
        combined_by_decision_id[intent.decision_id] = intent
    validate_promotion_intent_batch(tuple(combined_by_decision_id.values()))
    for intent in intents:
        store.append_promotion_intent(intent)


def _no_promote_intent_from_delta(
    *,
    delta: EpisodeMemoryDelta,
    action: ReflectionLearningAction,
) -> NoPromoteIntent:
    promotion_batch_id = _stable_id(
        "episode-memory-promotion-batch",
        action.action_id,
        delta.delta_id,
    )
    return NoPromoteIntent(
        decision_id=_stable_id("promotion-decision", promotion_batch_id, "no-promote"),
        promotion_batch_id=promotion_batch_id,
        episode_id=delta.episode_id,
        target_key=delta.target_key,
        business_at=delta.usable_from,
        recorded_at=delta.recorded_at,
        source_visible_through=delta.source_visible_through,
        usable_from=delta.usable_from,
        source_delta_ids=(delta.delta_id,),
        source_delta_hashes=(delta.content_hash,),
        promotion_intent="no_promote",
        decision_confidence=delta.confidence,
        content_hash="",
        no_promote_reason=(
            "Close/finalized reflection found no durable downstream promotion worth writing."
        ),
        no_promote_reason_code="no_positive_promotion_payload",
    )


def _promotion_intent_from_delta(
    *,
    delta: EpisodeMemoryDelta,
    action: ReflectionLearningAction,
    payload: Mapping[str, object],
) -> LearningCardPromotionIntent:
    consumer_role = _mapping_text(payload, "consumer_role")
    scope_key = _mapping_text(payload, "scope_key")
    usable_from = _mapping_optional_datetime(payload, "usable_from") or delta.usable_from
    if usable_from < delta.usable_from:
        raise ReflectionLearningLifecycleError(
            "promotion usable_from must be >= source delta usable_from."
        )
    promotion_batch_id = _mapping_optional_text(payload, "promotion_batch_id") or _stable_id(
        "episode-memory-promotion-batch",
        action.action_id,
        delta.delta_id,
    )
    decision_id = _mapping_optional_text(
        payload,
        "promotion_decision_id",
    ) or _stable_id("promotion-decision", promotion_batch_id, consumer_role, scope_key)
    confidence = _mapping_optional_float(payload, "confidence")
    return LearningCardPromotionIntent(
        decision_id=decision_id,
        promotion_batch_id=promotion_batch_id,
        episode_id=delta.episode_id,
        target_key=delta.target_key,
        business_at=delta.usable_from,
        recorded_at=_mapping_optional_datetime(payload, "created_at") or delta.recorded_at,
        source_visible_through=delta.source_visible_through,
        source_delta_ids=(delta.delta_id,),
        source_delta_hashes=(delta.content_hash,),
        promotion_intent="propose_learning_card",
        decision_confidence=delta.confidence if confidence is None else confidence,
        content_hash="",
        scope_key=scope_key,
        consumer_role=consumer_role,
        title=_mapping_text(payload, "title"),
        summary_md=_mapping_text(payload, "summary_md"),
        body_md=_mapping_text(payload, "body_md"),
        use_when="\n".join(_mapping_text_tuple(payload, "use_when")),
        avoid_when="\n".join(_mapping_text_tuple(payload, "avoid_when")),
        usable_from=usable_from,
        supersedes_card_ids=_mapping_optional_text_tuple(
            payload,
            "supersedes_card_ids",
        ),
        tags=_mapping_optional_text_tuple(payload, "tags"),
    )


def _promotion_card_id(receipt: EpisodeMemoryPromotionWriteReceipt) -> str | None:
    for artifact_ref in receipt.artifact_refs:
        if artifact_ref.startswith("learning-card://"):
            return artifact_ref.partition("://")[2] or None
    return None


def _promotion_artifact_path(
    receipt: EpisodeMemoryPromotionWriteReceipt,
) -> Path | None:
    for artifact_ref in receipt.artifact_refs:
        if "://" not in artifact_ref:
            return Path(artifact_ref)
    return None


def _delta_matches_candidate(
    delta: EpisodeMemoryDelta,
    candidate: EpisodeMemoryDeltaCandidate,
) -> bool:
    delta_payload = delta.to_json_payload()
    for key, value in candidate.to_json_payload().items():
        if delta_payload.get(key) != value:
            return False
    return True


def _existing_matching_no_update_path(
    *,
    store: FileBackedEpisodeMemoryStore,
    candidate: EpisodeMemoryNoUpdateCandidate,
    receipt_id: str,
) -> Path | None:
    for persisted in store.read_no_updates(
        target_key=candidate.target_key,
        episode_id=candidate.episode_id,
    ):
        if persisted.record.receipt_id != receipt_id:
            continue
        if _no_update_matches_candidate(persisted.record, candidate):
            return persisted.path
        raise ReflectionLearningLifecycleError(
            "deterministic episode memory no-update receipt_id already exists with "
            "different payload."
        )
    return None


def _no_update_matches_candidate(
    no_update: EpisodeMemoryNoUpdate,
    candidate: EpisodeMemoryNoUpdateCandidate,
) -> bool:
    no_update_payload = no_update.to_json_payload()
    candidate_payload = candidate.to_json_payload()
    candidate_payload.pop("candidate_id", None)
    for key, value in candidate_payload.items():
        if no_update_payload.get(key) != value:
            return False
    return True


def _append_write_receipt_idempotent(
    *,
    store: FileBackedEpisodeMemoryStore,
    receipt: EpisodeMemoryWriteReceipt,
) -> Path:
    for persisted in store.read_write_receipts(
        target_key=receipt.target_key,
        episode_id=receipt.episode_id,
    ):
        if persisted.record.receipt_id != receipt.receipt_id:
            continue
        if _write_receipt_matches_retry(persisted.record, receipt):
            return persisted.path
        raise ReflectionLearningLifecycleError(
            "deterministic episode memory write receipt_id already exists with "
            "different payload."
        )
    return store.append_write_receipt(receipt)


def _write_receipt_matches_retry(
    left: EpisodeMemoryWriteReceipt,
    right: EpisodeMemoryWriteReceipt,
) -> bool:
    left_payload = left.to_json_payload()
    right_payload = right.to_json_payload()
    left_payload.pop("recorded_at", None)
    right_payload.pop("recorded_at", None)
    return left_payload == right_payload


def _candidate_source_refs(
    *,
    action: ReflectionLearningAction,
    context: ReflectionLearningContext,
    review_receipt: ReflectionLearningReviewReceipt,
) -> EpisodeMemoryDeltaSourceRefs:
    payload_source_refs = action.payload.get("source_refs")
    if payload_source_refs is None:
        source_refs: Mapping[str, object] = {}
    elif isinstance(payload_source_refs, Mapping):
        source_refs = payload_source_refs
    else:
        raise ReflectionLearningLifecycleError("action payload source_refs must be an object.")
    return EpisodeMemoryDeltaSourceRefs(
        evidence_event_ids=_merge_text(
            _source_ref_values(source_refs, "evidence_event_ids"),
            tuple(record.event_id for record in context.original_evidence),
        ),
        market_bar_refs=_source_ref_values(source_refs, "market_bar_refs"),
        review_paths=_merge_text(
            _source_ref_values(source_refs, "review_paths"),
            (review_receipt.review_page_path,),
        ),
        pm_review_request_ids=_source_ref_values(source_refs, "pm_review_request_ids"),
        pm_decision_ids=_source_ref_values(source_refs, "pm_decision_ids"),
        execution_record_ids=_source_ref_values(source_refs, "execution_record_ids"),
        portfolio_record_ids=_source_ref_values(source_refs, "portfolio_record_ids"),
    )


def _source_ref_values(source_refs: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    value = source_refs.get(field_name)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ReflectionLearningLifecycleError(
            f"action payload source_refs.{field_name} must be an array when set."
        )
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ReflectionLearningLifecycleError(
                f"action payload source_refs.{field_name} must contain only non-blank strings."
            )
        normalized.append(item.strip())
    return tuple(normalized)


def _merge_text(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*left, *right)))


def _append_learning_episode_record(
    *,
    context: ReflectionLearningContext,
    evaluation: ReflectionLearningEvaluation,
    review_receipt: ReflectionLearningReviewReceipt,
    layout: WorkspaceLayout,
    action_receipts: tuple[ReflectionLearningActionReceipt, ...],
) -> Path | None:
    if evaluation.review_decision == "skip_review" and not review_receipt.wrote_review_episode:
        return None
    state_change_ids = _learning_state_change_ids(context)
    if not state_change_ids:
        return None
    store = FileBackedDecisionEpisodeStore(layout)
    persisted_records = store.read_records(target_key=evaluation.target_key)
    episode_ids = find_episode_ids_for_state_change(
        persisted_records,
        state_change_ids=state_change_ids,
    )
    if not episode_ids:
        return None
    recorded_at = datetime.now(UTC)
    event_ids = tuple(record.event_id for record in context.original_evidence)
    source_record_id = (
        "reflection-learning:"
        f"{evaluation.episode_id}:{review_receipt.review_page_path}"
    )
    existing_keys = {persisted.record.natural_key for persisted in persisted_records}
    record_path: Path | None = None
    for decision_episode_id in episode_ids:
        natural_key = (decision_episode_id, "reflection_learning", source_record_id)
        if natural_key in existing_keys:
            continue
        record_path = store.append_record(
            DecisionEpisodeRecord(
                episode_id=decision_episode_id,
                target_key=evaluation.target_key,
                record_type="reflection_learning",
                business_at=_learning_business_at(context),
                recorded_at=recorded_at,
                event_ids=event_ids,
                source_record_id=source_record_id,
                source_path=review_receipt.review_page_path,
                source_line=None,
                context_packet_id=None,
                context_packet_hash=None,
                status="reflected",
                payload={
                    "source_episode_id": evaluation.episode_id,
                    "source_review_path": review_receipt.review_page_path,
                    "review_decision": evaluation.review_decision,
                    "primary_outcome": evaluation.learning_decision.primary_outcome,
                    "action_receipts": [
                        receipt.to_json_payload() for receipt in action_receipts
                    ],
                    "linked_state_change_ids": list(state_change_ids),
                },
            )
        )
    return record_path


def _learning_state_change_ids(context: ReflectionLearningContext) -> tuple[str, ...]:
    if isinstance(context, OpenPositionReflectionContext):
        return (context.open_segment.opened_by_state_change_id,)
    return tuple(state_change.state_change_id for state_change in context.state_changes)


def _validate_lifecycle_inputs(
    *,
    context: ReflectionLearningContext,
    evaluation: ReflectionLearningEvaluation,
    review_receipt: ReflectionLearningReviewReceipt,
    layout: WorkspaceLayout,
) -> None:
    if not isinstance(
        context,
        (
            EpisodeReflectionContext,
            CloseEpisodeReflectionContext,
            OpenPositionReflectionContext,
        ),
    ):
        raise ReflectionLearningLifecycleError("context is not a reflection context.")
    if not isinstance(
        evaluation,
        (
            TargetReflectionEvaluationResult,
            TargetCloseReflectionEvaluationResult,
            OpenPositionEvaluationResult,
        ),
    ):
        raise ReflectionLearningLifecycleError("evaluation is not a target evaluation.")
    if not isinstance(
        review_receipt,
        (
            TargetReviewWriteReceipt,
            TargetCloseReviewWriteReceipt,
            OpenPositionHorizonReviewWriteReceipt,
            OpenPositionMaterialUpdateReviewWriteReceipt,
        ),
    ):
        raise ReflectionLearningLifecycleError("review_receipt is not supported.")
    if not isinstance(layout, WorkspaceLayout):
        raise ReflectionLearningLifecycleError("layout must be a WorkspaceLayout.")
    if evaluation.episode_id != context.episode.episode_id:
        raise ReflectionLearningLifecycleError(
            "evaluation.episode_id must match context episode_id."
        )
    if evaluation.target_key != context.episode.target_key:
        raise ReflectionLearningLifecycleError(
            "evaluation.target_key must match context episode target_key."
        )
    if review_receipt.episode_id != evaluation.episode_id:
        raise ReflectionLearningLifecycleError(
            "review_receipt.episode_id must match evaluation.episode_id."
        )
    if review_receipt.target_key != evaluation.target_key:
        raise ReflectionLearningLifecycleError(
            "review_receipt.target_key must match evaluation.target_key."
        )


def _validate_action_identity(
    *,
    context: ReflectionLearningContext,
    evaluation: ReflectionLearningEvaluation,
    review_receipt: ReflectionLearningReviewReceipt,
    action: ReflectionLearningAction,
) -> None:
    if action.source_episode_id != evaluation.episode_id:
        raise ReflectionLearningLifecycleError(
            "learning action source_episode_id must match evaluation episode_id."
        )
    if action.source_episode_id != context.episode.episode_id:
        raise ReflectionLearningLifecycleError(
            "learning action source_episode_id must match context episode_id."
        )
    if action.source_review_path != review_receipt.review_page_path:
        raise ReflectionLearningLifecycleError(
            "learning action source_review_path must match review_receipt review path."
        )
    if action.outcome != "no_learning_write":
        if evaluation.review_decision != "write_review" or not review_receipt.wrote_review_episode:
            raise ReflectionLearningLifecycleError(
                "write learning actions require review_decision='write_review' and a "
                "completed review artifact."
            )


def _learning_business_at(context: ReflectionLearningContext) -> datetime:
    if isinstance(context, OpenPositionReflectionContext):
        return context.terminal_mark.replay_end_at
    return context.episode.closed_at or context.episode.opened_at


def _payload_text(action: ReflectionLearningAction, field_name: str) -> str:
    value = action.payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ReflectionLearningLifecycleError(
            f"action payload {field_name} must be non-blank text."
        )
    return value.strip()


def _payload_optional_text(
    action: ReflectionLearningAction,
    field_name: str,
) -> str | None:
    value = action.payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReflectionLearningLifecycleError(
            f"action payload {field_name} must be text when set."
        )
    normalized = value.strip()
    return normalized or None


def _payload_optional_mapping(
    action: ReflectionLearningAction,
    field_name: str,
) -> Mapping[str, object] | None:
    value = action.payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ReflectionLearningLifecycleError(
            f"action payload {field_name} must be an object when set."
        )
    return dict(value)


def _payload_datetime(action: ReflectionLearningAction, field_name: str) -> datetime:
    value = action.payload.get(field_name)
    if not isinstance(value, str):
        raise ReflectionLearningLifecycleError(
            f"action payload {field_name} must be an ISO datetime string."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReflectionLearningLifecycleError(
            f"action payload {field_name} must be an ISO datetime string."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReflectionLearningLifecycleError(
            f"action payload {field_name} must be timezone-aware."
        )
    return parsed.astimezone(UTC)


def _payload_text_tuple(
    action: ReflectionLearningAction,
    field_name: str,
) -> tuple[str, ...]:
    value = action.payload.get(field_name)
    if not isinstance(value, list):
        raise ReflectionLearningLifecycleError(
            f"action payload {field_name} must be an array."
        )
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ReflectionLearningLifecycleError(
                f"action payload {field_name} must contain only non-blank strings."
            )
        normalized.append(item.strip())
    return tuple(normalized)


def _payload_float(action: ReflectionLearningAction, field_name: str) -> float:
    value = action.payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReflectionLearningLifecycleError(
            f"action payload {field_name} must be a number."
        )
    return float(value)


def _mapping_text(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ReflectionLearningLifecycleError(
            f"promotion payload {field_name} must be non-blank text."
        )
    return value.strip()


def _mapping_optional_text(
    payload: Mapping[str, object],
    field_name: str,
) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReflectionLearningLifecycleError(
            f"promotion payload {field_name} must be text when set."
        )
    normalized = value.strip()
    return normalized or None


def _mapping_datetime(payload: Mapping[str, object], field_name: str) -> datetime:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise ReflectionLearningLifecycleError(
            f"promotion payload {field_name} must be an ISO datetime string."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReflectionLearningLifecycleError(
            f"promotion payload {field_name} must be an ISO datetime string."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReflectionLearningLifecycleError(
            f"promotion payload {field_name} must be timezone-aware."
        )
    return parsed.astimezone(UTC)


def _mapping_optional_datetime(
    payload: Mapping[str, object],
    field_name: str,
) -> datetime | None:
    if payload.get(field_name) is None:
        return None
    return _mapping_datetime(payload, field_name)


def _mapping_text_tuple(
    payload: Mapping[str, object],
    field_name: str,
) -> tuple[str, ...]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise ReflectionLearningLifecycleError(
            f"promotion payload {field_name} must be an array."
        )
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ReflectionLearningLifecycleError(
                f"promotion payload {field_name} must contain only non-blank strings."
            )
        normalized.append(item.strip())
    return tuple(normalized)


def _mapping_optional_text_tuple(
    payload: Mapping[str, object],
    field_name: str,
) -> tuple[str, ...]:
    if payload.get(field_name) is None:
        return ()
    return _mapping_text_tuple(payload, field_name)


def _mapping_optional_float(
    payload: Mapping[str, object],
    field_name: str,
) -> float | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReflectionLearningLifecycleError(
            f"promotion payload {field_name} must be a number when set."
        )
    return float(value)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


__all__ = [
    "ReflectionLearningLifecycleError",
    "apply_reflection_learning",
]

