"""Exposure-blind analysis assessment contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from event_trader.contracts._validators import (
    normalize_content,
    validate_event_id,
    validate_target_key,
    validate_timestamp,
)
from event_trader.contracts.analysis_price_semantics import (
    AnalysisPriceSemantics,
    AnalysisPriceSemanticsContractError,
    parse_analysis_price_semantics,
)
from event_trader.contracts.pm_review_reason import (
    PMReviewReason,
    analysis_pm_escalation_reasons,
    validate_pm_review_reasons,
)
from event_trader.contracts.analysis_assessment_schema import (
    analysis_assessment_optional_fields,
    allowed_confidence_values,
    analysis_assessment_required_fields,
    invalid_legacy_price_level_role_shape,
    price_level_role_required_fields,
)
from event_trader.contracts.execution_direction_policy import (
    ExecutionDirectionMode,
    supported_analysis_view_states,
)
from event_trader.contracts.price_level_role import (
    PriceLevelRole,
    PriceLevelRoleContractError,
    parse_price_level_role,
)
from event_trader.contracts.view_state_change import ViewState

Confidence = Literal["low", "medium", "high"]
AnalysisOutcome = Literal[
    "no_update",
    "memory_updated",
    "assessment_emitted",
    "memory_and_assessment_updated",
]

_CONFIDENCE_VALUES = frozenset(allowed_confidence_values())
_VIEW_STATES = frozenset(supported_analysis_view_states())
_PM_REVIEW_ESCALATION_REASONS = frozenset(analysis_pm_escalation_reasons())
_LEGACY_PM_REVIEW_CANDIDATE_TRIGGER_REASONS = frozenset(
    {
        "flat_candidate_setup",
        "material_setup",
    }
)
_LEGACY_PM_REVIEW_CURRENT_EXPOSURE_TRIGGER_REASONS = frozenset(
    {
        "current_exposure_pressure",
        "invalidation_touched",
        "risk_reward_compression",
    }
)


class AnalysisAssessmentContractError(ValueError):
    """Raised when an analysis assessment is malformed."""


@dataclass(frozen=True, slots=True)
class AnalysisAssessment:
    assessment_id: str
    target_key: str
    business_at: datetime
    source_event_ids: tuple[str, ...]
    memory_write_receipt_ids: tuple[str, ...]

    as_if_flat_state: ViewState
    as_if_flat_rationale_md: str

    if_flat_implication_md: str
    if_already_long_implication_md: str
    if_already_short_implication_md: str | None

    price_level_roles: tuple[PriceLevelRole, ...]
    analysis_price_semantics: AnalysisPriceSemantics | None
    market_setup_dashboard_md: str
    key_claim_ids: tuple[str, ...]
    contested_prior_claim_ids: tuple[str, ...]
    missing_evidence_md: str

    pm_candidate_review_required: bool
    pm_current_exposure_review_required: bool
    pm_review_reasons: tuple[PMReviewReason, ...]
    confidence: Confidence

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assessment_id",
            _validate_non_blank(self.assessment_id, "assessment_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=AnalysisAssessmentContractError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=AnalysisAssessmentContractError,
            ),
        )
        object.__setattr__(
            self,
            "source_event_ids",
            _validate_event_ids(self.source_event_ids, allow_empty=False),
        )
        object.__setattr__(
            self,
            "memory_write_receipt_ids",
            _validate_string_tuple(
                self.memory_write_receipt_ids,
                "memory_write_receipt_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "as_if_flat_state",
            _validate_view_state(self.as_if_flat_state, "as_if_flat_state"),
        )
        for field_name in (
            "as_if_flat_rationale_md",
            "if_flat_implication_md",
            "if_already_long_implication_md",
            "market_setup_dashboard_md",
            "missing_evidence_md",
        ):
            object.__setattr__(
                self,
                field_name,
                normalize_content(
                    getattr(self, field_name),
                    field_name=field_name,
                    error_type=AnalysisAssessmentContractError,
                ),
            )
        object.__setattr__(
            self,
            "if_already_short_implication_md",
            _validate_optional_markdown(
                self.if_already_short_implication_md,
                "if_already_short_implication_md",
            ),
        )
        object.__setattr__(
            self,
            "price_level_roles",
            _validate_price_level_roles(self.price_level_roles, self.target_key),
        )
        object.__setattr__(
            self,
            "analysis_price_semantics",
            _validate_analysis_price_semantics(
                self.analysis_price_semantics,
                target_key=self.target_key,
                price_level_roles=self.price_level_roles,
            ),
        )
        object.__setattr__(
            self,
            "key_claim_ids",
            _validate_string_tuple(self.key_claim_ids, "key_claim_ids", allow_empty=True),
        )
        object.__setattr__(
            self,
            "contested_prior_claim_ids",
            _validate_string_tuple(
                self.contested_prior_claim_ids,
                "contested_prior_claim_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "pm_candidate_review_required",
            _validate_bool(
                self.pm_candidate_review_required,
                "pm_candidate_review_required",
            ),
        )
        object.__setattr__(
            self,
            "pm_current_exposure_review_required",
            _validate_bool(
                self.pm_current_exposure_review_required,
                "pm_current_exposure_review_required",
            ),
        )
        object.__setattr__(
            self,
            "pm_review_reasons",
            validate_pm_review_reasons(
                self.pm_review_reasons,
                field_name="pm_review_reasons",
                allow_none=True,
                allow_empty=True,
                error_type=AnalysisAssessmentContractError,
            ),
        )
        object.__setattr__(self, "confidence", _validate_confidence(self.confidence))
        _validate_pm_trigger_consistency(self)

    def to_json_payload(self) -> dict[str, object]:
        return {
            "assessment_id": self.assessment_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "source_event_ids": list(self.source_event_ids),
            "memory_write_receipt_ids": list(self.memory_write_receipt_ids),
            "as_if_flat_state": self.as_if_flat_state,
            "as_if_flat_rationale_md": self.as_if_flat_rationale_md,
            "if_flat_implication_md": self.if_flat_implication_md,
            "if_already_long_implication_md": self.if_already_long_implication_md,
            "price_level_roles": [
                level_role.to_json_payload() for level_role in self.price_level_roles
            ],
            **(
                {}
                if self.analysis_price_semantics is None
                else {
                    "analysis_price_semantics": self.analysis_price_semantics.to_json_payload()
                }
            ),
            "market_setup_dashboard_md": self.market_setup_dashboard_md,
            "key_claim_ids": list(self.key_claim_ids),
            "contested_prior_claim_ids": list(self.contested_prior_claim_ids),
            "missing_evidence_md": self.missing_evidence_md,
            "pm_candidate_review_required": self.pm_candidate_review_required,
            "pm_current_exposure_review_required": (
                self.pm_current_exposure_review_required
            ),
            "pm_review_reasons": list(self.pm_review_reasons),
            "confidence": self.confidence,
            **(
                {}
                if self.if_already_short_implication_md is None
                else {
                    "if_already_short_implication_md": (
                        self.if_already_short_implication_md
                    )
                }
            ),
        }


def parse_analysis_assessment(
    payload: Mapping[str, object],
    *,
    allow_legacy_pm_trigger_fields: bool = False,
    execution_direction_mode: ExecutionDirectionMode | None = None,
) -> AnalysisAssessment:
    normalized_payload = _normalize_analysis_assessment_payload(
        payload,
        allow_legacy_pm_trigger_fields=allow_legacy_pm_trigger_fields,
    )
    required_fields = (
        analysis_assessment_required_fields(execution_direction_mode)
        if execution_direction_mode is not None
        else tuple(
            field_name
            for field_name in analysis_assessment_required_fields("long_short")
            if field_name != "if_already_short_implication_md"
        )
    )
    optional_fields = (
        analysis_assessment_optional_fields(execution_direction_mode)
        if execution_direction_mode is not None
        else analysis_assessment_optional_fields("long_only")
    )
    _require_exact_fields(
        normalized_payload,
        required=set(required_fields),
        optional=set(optional_fields),
        surface="analysis_assessment",
    )
    price_level_roles_payload = normalized_payload.get("price_level_roles")
    if not isinstance(price_level_roles_payload, list):
        raise AnalysisAssessmentContractError("price_level_roles must be a list.")
    price_level_roles: list[PriceLevelRole] = []
    for index, item in enumerate(price_level_roles_payload):
        if not isinstance(item, Mapping):
            raise AnalysisAssessmentContractError(
                f"analysis_assessment.price_level_roles[{index}] must be a JSON object."
            )
        try:
            price_level_roles.append(parse_price_level_role(item))
        except PriceLevelRoleContractError as exc:
            raise AnalysisAssessmentContractError(
                _format_price_level_role_contract_error(
                    index=index,
                    payload=item,
                    error=exc,
                )
            ) from exc
    has_analysis_price_semantics = "analysis_price_semantics" in normalized_payload
    analysis_price_semantics_payload = normalized_payload.get("analysis_price_semantics")
    analysis_price_semantics: AnalysisPriceSemantics | None
    if not has_analysis_price_semantics:
        analysis_price_semantics = None
    else:
        if not isinstance(analysis_price_semantics_payload, Mapping):
            raise AnalysisAssessmentContractError(
                "analysis_assessment.analysis_price_semantics must be a JSON object."
            )
        try:
            analysis_price_semantics = parse_analysis_price_semantics(
                analysis_price_semantics_payload
            )
        except AnalysisPriceSemanticsContractError as exc:
            raise AnalysisAssessmentContractError(
                f"analysis_price_semantics {exc}"
            ) from exc
    return AnalysisAssessment(
        assessment_id=_require_text(normalized_payload, "assessment_id"),
        target_key=_require_text(normalized_payload, "target_key"),
        business_at=_parse_timestamp(normalized_payload.get("business_at"), "business_at"),
        source_event_ids=tuple(_require_string_list(normalized_payload, "source_event_ids")),
        memory_write_receipt_ids=tuple(
            _require_string_list(normalized_payload, "memory_write_receipt_ids")
        ),
        as_if_flat_state=cast(ViewState, _require_text(normalized_payload, "as_if_flat_state")),
        as_if_flat_rationale_md=_require_text(normalized_payload, "as_if_flat_rationale_md"),
        if_flat_implication_md=_require_text(normalized_payload, "if_flat_implication_md"),
        if_already_long_implication_md=_require_text(
            normalized_payload,
            "if_already_long_implication_md",
        ),
        if_already_short_implication_md=(
            _require_text(normalized_payload, "if_already_short_implication_md")
            if execution_direction_mode == "long_short"
            else _require_optional_markdown(
                normalized_payload,
                "if_already_short_implication_md",
            )
        ),
        price_level_roles=tuple(price_level_roles),
        analysis_price_semantics=analysis_price_semantics,
        market_setup_dashboard_md=_require_text(normalized_payload, "market_setup_dashboard_md"),
        key_claim_ids=tuple(_require_string_list(normalized_payload, "key_claim_ids")),
        contested_prior_claim_ids=tuple(
            _require_string_list(normalized_payload, "contested_prior_claim_ids")
        ),
        missing_evidence_md=_require_text(normalized_payload, "missing_evidence_md"),
        pm_candidate_review_required=_require_bool(
            normalized_payload,
            "pm_candidate_review_required",
        ),
        pm_current_exposure_review_required=_require_bool(
            normalized_payload,
            "pm_current_exposure_review_required",
        ),
        pm_review_reasons=tuple(
            cast(PMReviewReason, value)
            for value in _require_string_list(normalized_payload, "pm_review_reasons")
        ),
        confidence=cast(Confidence, _require_text(normalized_payload, "confidence")),
    )


def _normalize_analysis_assessment_payload(
    payload: Mapping[str, object],
    *,
    allow_legacy_pm_trigger_fields: bool,
) -> Mapping[str, object]:
    if not allow_legacy_pm_trigger_fields:
        return payload
    has_candidate_flag = "pm_candidate_review_required" in payload
    has_current_exposure_flag = "pm_current_exposure_review_required" in payload
    if has_candidate_flag and has_current_exposure_flag:
        return payload
    normalized_payload = dict(payload)
    review_reasons = tuple(
        value
        for value in _require_string_list(payload, "pm_review_reasons")
        if value != "none"
    )
    if not has_candidate_flag:
        normalized_payload["pm_candidate_review_required"] = (
            _legacy_entry_role_requires_candidate_trigger(payload)
            or any(
                reason in _LEGACY_PM_REVIEW_CANDIDATE_TRIGGER_REASONS
                for reason in review_reasons
            )
        )
    if not has_current_exposure_flag:
        normalized_payload["pm_current_exposure_review_required"] = any(
            reason in _LEGACY_PM_REVIEW_CURRENT_EXPOSURE_TRIGGER_REASONS
            for reason in review_reasons
        )
    return normalized_payload


def _legacy_entry_role_requires_candidate_trigger(payload: Mapping[str, object]) -> bool:
    price_level_roles = payload.get("price_level_roles")
    if not isinstance(price_level_roles, list):
        return False
    return any(
        isinstance(level, Mapping) and level.get("role_if_flat") == "entry"
        for level in price_level_roles
    )


def _validate_non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisAssessmentContractError(f"{field_name} must be a non-blank string.")
    normalized = value.strip()
    if normalized != value:
        raise AnalysisAssessmentContractError(
            f"{field_name} must not include leading or trailing whitespace."
        )
    return normalized


def _format_price_level_role_contract_error(
    *,
    index: int,
    payload: Mapping[str, object],
    error: PriceLevelRoleContractError,
) -> str:
    compact_payload = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return (
        f"analysis_assessment.price_level_roles[{index}] violates the "
        f"PriceLevelRole contract: {error}. "
        "Incomplete legacy objects such as "
        f"{invalid_legacy_price_level_role_shape()} are invalid and must be replaced with "
        "the full PriceLevelRole contract. "
        f"Required fields: {', '.join(price_level_role_required_fields())}. "
        f"Actual object: {compact_payload}"
    )


def _validate_event_ids(values: tuple[str, ...], *, allow_empty: bool) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized and not allow_empty:
        raise AnalysisAssessmentContractError("source_event_ids must not be empty.")
    seen: set[str] = set()
    for value in normalized:
        validated = validate_event_id(value, error_type=AnalysisAssessmentContractError)
        if validated in seen:
            raise AnalysisAssessmentContractError("source_event_ids must not contain duplicates.")
        seen.add(validated)
    return normalized


def _validate_string_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized and not allow_empty:
        raise AnalysisAssessmentContractError(f"{field_name} must not be empty.")
    seen: set[str] = set()
    for value in normalized:
        text = _validate_non_blank(value, field_name)
        if text in seen:
            raise AnalysisAssessmentContractError(f"{field_name} must not contain duplicates.")
        seen.add(text)
    return normalized


def _validate_price_level_roles(
    values: tuple[PriceLevelRole, ...],
    target_key: str,
) -> tuple[PriceLevelRole, ...]:
    normalized = tuple(values)
    seen: set[str] = set()
    for value in normalized:
        if not isinstance(value, PriceLevelRole):
            raise AnalysisAssessmentContractError(
                "price_level_roles must contain only PriceLevelRole instances."
            )
        if value.target_key != target_key:
            raise AnalysisAssessmentContractError(
                "price_level_roles target_key must match assessment target_key."
            )
        if value.level_id in seen:
            raise AnalysisAssessmentContractError(
                "price_level_roles must not contain duplicate level_id values."
            )
        seen.add(value.level_id)
    return normalized


def _validate_view_state(value: object, field_name: str) -> ViewState:
    if value not in _VIEW_STATES:
        raise AnalysisAssessmentContractError(f"{field_name} is not a supported view state.")
    return cast(ViewState, value)


def _validate_analysis_price_semantics(
    value: AnalysisPriceSemantics | None,
    *,
    target_key: str,
    price_level_roles: tuple[PriceLevelRole, ...],
) -> AnalysisPriceSemantics | None:
    if value is None:
        return None
    if not isinstance(value, AnalysisPriceSemantics):
        raise AnalysisAssessmentContractError(
            "analysis_price_semantics must be an AnalysisPriceSemantics instance."
        )
    if value.target_key != target_key:
        raise AnalysisAssessmentContractError(
            "analysis_price_semantics.target_key must match assessment target_key."
        )
    allowed_level_ids = {level.level_id for level in price_level_roles}
    unknown_level_ids = tuple(
        level_id
        for level_id in value.active_price_level_ids
        if level_id not in allowed_level_ids
    )
    if unknown_level_ids:
        raise AnalysisAssessmentContractError(
            "analysis_price_semantics.active_price_level_ids must be a subset of "
            "price_level_roles.level_id values."
        )
    return value


def _validate_confidence(value: object) -> Confidence:
    if value not in _CONFIDENCE_VALUES:
        raise AnalysisAssessmentContractError("confidence must be low, medium, or high.")
    return cast(Confidence, value)


def _validate_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise AnalysisAssessmentContractError(f"{field_name} must be a boolean.")
    return value


def _validate_pm_trigger_consistency(assessment: AnalysisAssessment) -> None:
    if any(level.role_if_flat == "entry" for level in assessment.price_level_roles):
        if assessment.pm_candidate_review_required is not True:
            raise AnalysisAssessmentContractError(
                "pm_candidate_review_required must be true when any "
                "price_level_roles role_if_flat is 'entry'."
            )
    if any(reason in _PM_REVIEW_ESCALATION_REASONS for reason in assessment.pm_review_reasons):
        if not (
            assessment.pm_candidate_review_required
            or assessment.pm_current_exposure_review_required
        ):
            raise AnalysisAssessmentContractError(
                "pm_review_reasons escalation entries require "
                "pm_candidate_review_required or "
                "pm_current_exposure_review_required to be true."
            )


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_non_blank(payload.get(field_name), field_name)


def _require_optional_markdown(
    payload: Mapping[str, object],
    field_name: str,
) -> str | None:
    return _validate_optional_markdown(payload.get(field_name), field_name)


def _require_string_list(payload: Mapping[str, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AnalysisAssessmentContractError(f"{field_name} must be a list of strings.")
    return value


def _require_bool(payload: Mapping[str, object], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise AnalysisAssessmentContractError(f"{field_name} must be a boolean.")
    return value


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise AnalysisAssessmentContractError(f"{field_name} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AnalysisAssessmentContractError(f"{field_name} must be an ISO timestamp.") from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=AnalysisAssessmentContractError,
    )


def _validate_optional_markdown(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnalysisAssessmentContractError(f"{field_name} must be a string when set.")
    return normalize_content(
        value,
        field_name=field_name,
        error_type=AnalysisAssessmentContractError,
    )


def _require_exact_fields(
    payload: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] | None = None,
    surface: str,
) -> None:
    allowed_optional = optional or set()
    actual = set(str(key) for key in payload)
    missing = sorted(required - actual)
    unexpected = sorted(actual - required - allowed_optional)
    if missing or unexpected:
        problems: list[str] = []
        if missing:
            problems.append(f"missing fields: {', '.join(missing)}")
        if unexpected:
            problems.append(f"unexpected fields: {', '.join(unexpected)}")
        raise AnalysisAssessmentContractError(f"{surface} " + "; ".join(problems))


__all__ = [
    "AnalysisAssessment",
    "AnalysisAssessmentContractError",
    "AnalysisOutcome",
    "Confidence",
    "parse_analysis_assessment",
]
