"""Episode-memory validation for deterministic canonical delta commitment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from event_trader.contracts._validators import validate_timestamp
from event_trader.episode_memory.contracts import (
    EpisodeMemoryContractError,
    EpisodeMemoryDelta,
    EpisodeMemoryDeltaCandidate,
    EpisodeMemoryDeltaSourceRefs,
    EpisodeMemoryValidationReceipt,
    episode_memory_record_hash,
)


@dataclass(frozen=True, slots=True)
class EpisodeMemoryValidationResult:
    """Result of delta candidate validation."""

    accepted: bool
    delta: EpisodeMemoryDelta | None
    receipt: EpisodeMemoryValidationReceipt
    rejection_reasons: tuple[str, ...]


class EpisodeMemoryDeltaValidator:
    """Deterministic validation for EpisodeMemoryDelta candidates."""

    def validate_candidate(
        self,
        candidate: EpisodeMemoryDeltaCandidate,
        *,
        delta_id: str,
        recorded_at: datetime,
        validator_receipt_id: str,
    ) -> EpisodeMemoryValidationResult:
        if not isinstance(candidate, EpisodeMemoryDeltaCandidate):
            raise TypeError("candidate must be an EpisodeMemoryDeltaCandidate.")

        validated_delta_id = _validate_non_blank_text(delta_id, "delta_id")
        validated_receipt_id = _validate_non_blank_text(
            validator_receipt_id,
            "validator_receipt_id",
        )
        reasons: list[str] = []

        try:
            normalized_recorded_at = validate_timestamp(
                recorded_at,
                field_name="recorded_at",
                error_type=EpisodeMemoryContractError,
            )
        except EpisodeMemoryContractError as exc:
            reasons.append(str(exc))
            normalized_recorded_at = _utc_now()

        if candidate.source_visible_through > normalized_recorded_at:
            reasons.append(
                "candidate.source_visible_through cannot be after recorded_at."
            )
        if not candidate.source_refs.has_any_reference:
            reasons.append("candidate source_refs must include at least one source reference.")

        if not _has_primary_fact_source_refs(candidate.source_refs):
            reasons.append(
                "candidate source_refs must include primary Layer 1 fact refs: "
                "evidence, market, PM, critic, risk, execution, or portfolio refs."
            )

        reasons.extend(_validate_status_basis(candidate.memory_status, candidate.validation_basis))
        reasons.extend(_validate_supersedes(candidate))

        delta: EpisodeMemoryDelta | None = None
        if not reasons:
            try:
                delta = EpisodeMemoryDelta(
                    delta_id=validated_delta_id,
                    episode_id=candidate.episode_id,
                    target_key=candidate.target_key,
                    delta_kind=candidate.delta_kind,
                    memory_status=candidate.memory_status,
                    validation_basis=candidate.validation_basis,
                    recorded_at=normalized_recorded_at,
                    source_visible_through=candidate.source_visible_through,
                    usable_from=candidate.usable_from,
                    source_refs=candidate.source_refs,
                    summary_md=candidate.summary_md,
                    thesis_delta=candidate.thesis_delta,
                    pm_management_delta=candidate.pm_management_delta,
                    risk_delta=candidate.risk_delta,
                    invalidation_delta=candidate.invalidation_delta,
                    error_attributions=candidate.error_attributions,
                    confidence=candidate.confidence,
                    supersedes_delta_ids=candidate.supersedes_delta_ids,
                )
            except EpisodeMemoryContractError as exc:
                reasons.append(str(exc))
            else:
                expected_hash = episode_memory_record_hash(
                    {
                        key: value
                        for key, value in delta.to_json_payload().items()
                        if key != "content_hash"
                    }
                )
                if delta.content_hash != expected_hash:
                    reasons.append(
                        "computed content_hash does not match deterministic payload hash."
                    )

        if reasons:
            receipt = EpisodeMemoryValidationReceipt(
                receipt_id=validated_receipt_id,
                delta_id=validated_delta_id,
                episode_id=candidate.episode_id,
                target_key=candidate.target_key,
                status="rejected",
                recorded_at=normalized_recorded_at,
                source_visible_through=candidate.source_visible_through,
                usable_from=candidate.usable_from,
                failure_reason="; ".join(reasons),
            )
            return EpisodeMemoryValidationResult(
                accepted=False,
                delta=None,
                receipt=receipt,
                rejection_reasons=tuple(reasons),
            )

        assert delta is not None
        receipt = EpisodeMemoryValidationReceipt(
            receipt_id=validated_receipt_id,
            delta_id=validated_delta_id,
            episode_id=candidate.episode_id,
            target_key=candidate.target_key,
            status="accepted",
            recorded_at=normalized_recorded_at,
            source_visible_through=candidate.source_visible_through,
            usable_from=candidate.usable_from,
            failure_reason=None,
        )
        return EpisodeMemoryValidationResult(
            accepted=True,
            delta=delta,
            receipt=receipt,
            rejection_reasons=(),
        )


def _validate_non_blank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise EpisodeMemoryContractError(f"{field_name} must be a non-blank string.")
    normalized = value.strip()
    if not normalized:
        raise EpisodeMemoryContractError(f"{field_name} must be a non-blank string.")
    return normalized


def _validate_status_basis(
    memory_status: str,
    validation_basis: str,
) -> tuple[str, ...]:
    if memory_status == "provisional":
        return ()

    if memory_status == "validated":
        if validation_basis == "current_evidence":
            return ("validated memory_status requires later_evidence, not current_evidence.",)
        if validation_basis not in {
            "later_evidence",
            "market_return",
            "execution_feedback",
            "portfolio_feedback",
            "close_review",
        }:
            return (
                (
                    "validated memory_status requires later_evidence, market_return, "
                    "execution_feedback, portfolio_feedback, or close_review validation_basis."
                ),
            )
        return ()

    if memory_status == "invalidated":
        if validation_basis not in {
            "later_evidence",
            "market_return",
            "execution_feedback",
            "portfolio_feedback",
            "close_review",
        }:
            return (
                (
                    "invalidated memory_status requires later_evidence, market_return, "
                    "execution_feedback, portfolio_feedback, or close_review validation_basis."
                ),
            )
        return ()

    if memory_status == "finalized":
        if validation_basis not in {
            "market_return",
            "execution_feedback",
            "portfolio_feedback",
            "close_review",
        }:
            return (
                (
                    "finalized memory_status requires market_return, execution_feedback, "
                    "portfolio_feedback, or close_review validation_basis."
                ),
            )
        return ()

    return (f"unsupported memory_status for deterministic validation: {memory_status!r}.",)


def _validate_supersedes(candidate: EpisodeMemoryDeltaCandidate) -> tuple[str, ...]:
    if len(candidate.supersedes_delta_ids) != len(set(candidate.supersedes_delta_ids)):
        return ("supersedes_delta_ids must not contain duplicates.",)
    return ()


def _has_primary_fact_source_refs(source_refs: EpisodeMemoryDeltaSourceRefs) -> bool:
    return bool(
        source_refs.evidence_event_ids
        or source_refs.market_bar_refs
        or source_refs.pm_review_request_ids
        or source_refs.pm_decision_ids
        or source_refs.execution_record_ids
        or source_refs.portfolio_record_ids
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "EpisodeMemoryDeltaValidator",
    "EpisodeMemoryValidationResult",
]
