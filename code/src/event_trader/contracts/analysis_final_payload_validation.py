"""Read-only validation for MiroThinker analysis final payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from event_trader.contracts.analysis_assessment import (
    AnalysisAssessment,
    AnalysisAssessmentContractError,
    parse_analysis_assessment,
)
from event_trader.contracts.analysis_assessment_schema import (
    analysis_assessment_contract_markdown,
    analysis_assessment_optional_fields,
    analysis_assessment_required_fields,
    analysis_final_payload_contract_markdown,
    analysis_final_payload_required_fields,
    analysis_price_semantics_contract_markdown,
    forbidden_analysis_final_payload_fields,
    invalid_legacy_price_level_role_shape,
    price_level_role_contract_markdown,
    price_level_role_required_fields,
)
from event_trader.contracts.execution_direction_policy import (
    ExecutionDirectionMode,
    analysis_assessment_direction_policy_violations,
)
from event_trader.contracts.instrument_basis import canonicalize_instrument_basis
from event_trader.contracts.price_level_role import (
    is_semantically_active_price_level,
)


@dataclass(frozen=True, slots=True)
class AnalysisFinalPayloadValidationResult:
    ok: bool
    error_code: str | None
    message: str
    field_path: str | None = None
    suggested_action: str | None = None
    schema_contract_md: str | None = None
    target_key: str | None = None
    event_ids: tuple[str, ...] = ()
    used_lesson_ids: tuple[str, ...] = ()
    analysis_assessment: AnalysisAssessment | None = None

    def to_json_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "error_code": self.error_code,
            "message": self.message,
            "field_path": self.field_path,
            "suggested_action": self.suggested_action,
            "schema_contract_md": self.schema_contract_md,
        }


def validate_analysis_final_payload(
    payload: object,
    *,
    expected_target_key: str,
    expected_event_ids: tuple[str, ...],
    included_lesson_ids: tuple[str, ...] = (),
    execution_direction_mode: ExecutionDirectionMode = "long_short",
    active_instrument_basis: str | None = None,
) -> AnalysisFinalPayloadValidationResult:
    if not isinstance(payload, Mapping):
        return _invalid(
            error_code="analysis_payload_type_violation",
            message="analysis final payload must be a JSON object.",
            field_path=None,
        )
    legacy_fields = sorted(set(forbidden_analysis_final_payload_fields()) & set(payload))
    if legacy_fields:
        return _invalid(
            error_code="analysis_payload_legacy_trading_fields",
            message=(
                "analysis output must be exposure-blind and must not include "
                "legacy trading/view-state field(s): "
                f"{', '.join(legacy_fields)}."
            ),
            field_path=legacy_fields[0],
        )
    required_fields = set(analysis_final_payload_required_fields())
    actual_fields = set(str(key) for key in payload)
    missing_fields = sorted(required_fields - actual_fields)
    unexpected_fields = sorted(actual_fields - required_fields)
    if missing_fields:
        return _invalid(
            error_code="analysis_payload_missing_fields",
            message=(
                "analysis payload missing required top-level field(s): "
                f"{', '.join(missing_fields)}."
            ),
            field_path=missing_fields[0],
        )
    if unexpected_fields:
        return _invalid(
            error_code="analysis_payload_unexpected_fields",
            message=(
                "analysis payload contained unexpected top-level field(s): "
                f"{', '.join(unexpected_fields)}."
            ),
            field_path=unexpected_fields[0],
        )

    target_key_result = _normalize_target_key(
        payload.get("target_key"),
        expected_target_key=expected_target_key,
    )
    if isinstance(target_key_result, AnalysisFinalPayloadValidationResult):
        return target_key_result
    target_key = target_key_result

    event_ids_result = _normalize_event_ids(
        payload.get("event_ids"),
        expected_event_ids=expected_event_ids,
    )
    if isinstance(event_ids_result, AnalysisFinalPayloadValidationResult):
        return event_ids_result
    event_ids = event_ids_result

    used_lesson_ids_result = _normalize_used_lesson_ids(
        payload.get("used_lesson_ids"),
        included_lesson_ids=included_lesson_ids,
    )
    if isinstance(used_lesson_ids_result, AnalysisFinalPayloadValidationResult):
        return used_lesson_ids_result
    used_lesson_ids = used_lesson_ids_result

    assessment_result = _normalize_analysis_assessment(
        payload.get("analysis_assessment"),
        expected_target_key=expected_target_key,
        expected_event_ids=expected_event_ids,
        execution_direction_mode=execution_direction_mode,
    )
    if isinstance(assessment_result, AnalysisFinalPayloadValidationResult):
        return assessment_result
    basis_integrity_result = _validate_analysis_price_basis_integrity(
        assessment_result,
        active_instrument_basis=active_instrument_basis,
        execution_direction_mode=execution_direction_mode,
    )
    if basis_integrity_result is not None:
        return basis_integrity_result

    return AnalysisFinalPayloadValidationResult(
        ok=True,
        error_code=None,
        message="analysis final payload is valid.",
        target_key=target_key,
        event_ids=event_ids,
        used_lesson_ids=used_lesson_ids,
        analysis_assessment=assessment_result,
    )


def _normalize_target_key(
    value: object,
    *,
    expected_target_key: str,
) -> str | AnalysisFinalPayloadValidationResult:
    if not isinstance(value, str) or not value.strip():
        return _invalid(
            error_code="target_key_contract_violation",
            message="target_key must be a non-blank string.",
            field_path="target_key",
        )
    target_key = value.strip()
    if target_key != expected_target_key:
        return _invalid(
            error_code="target_key_contract_violation",
            message=(
                "MiroThinker analysis agent must return the active target_key. "
                f"expected={expected_target_key!r}, actual={target_key!r}."
            ),
            field_path="target_key",
        )
    return target_key


def _normalize_event_ids(
    value: object,
    *,
    expected_event_ids: tuple[str, ...],
) -> tuple[str, ...] | AnalysisFinalPayloadValidationResult:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return _invalid(
            error_code="event_ids_contract_violation",
            message="event_ids must be a JSON array of strings.",
            field_path="event_ids",
        )
    event_ids = tuple(value)
    if event_ids != expected_event_ids:
        return _invalid(
            error_code="event_ids_contract_violation",
            message=(
                "event_ids must match active request event_ids in order. "
                f"expected={list(expected_event_ids)!r}, actual={list(event_ids)!r}."
            ),
            field_path="event_ids",
        )
    return event_ids


def _normalize_used_lesson_ids(
    value: object,
    *,
    included_lesson_ids: tuple[str, ...],
) -> tuple[str, ...] | AnalysisFinalPayloadValidationResult:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return _invalid(
            error_code="used_lesson_ids_contract_violation",
            message="used_lesson_ids must be a JSON array of strings.",
            field_path="used_lesson_ids",
        )
    normalized = tuple(item.strip() for item in value)
    if not all(normalized) or normalized != tuple(value):
        return _invalid(
            error_code="used_lesson_ids_contract_violation",
            message="used_lesson_ids entries must be non-blank strings.",
            field_path="used_lesson_ids",
        )
    if len(set(normalized)) != len(normalized):
        return _invalid(
            error_code="used_lesson_ids_contract_violation",
            message="used_lesson_ids must not contain duplicate lesson IDs.",
            field_path="used_lesson_ids",
        )
    unknown_lesson_ids = tuple(
        lesson_id for lesson_id in normalized if lesson_id not in included_lesson_ids
    )
    if unknown_lesson_ids:
        return _invalid(
            error_code="used_lesson_ids_not_included",
            message=(
                "used_lesson_ids must reference only lesson IDs included in "
                f"the prompt context. unknown_lesson_ids={list(unknown_lesson_ids)!r}"
            ),
            field_path="used_lesson_ids",
        )
    return normalized


def _normalize_analysis_assessment(
    value: object,
    *,
    expected_target_key: str,
    expected_event_ids: tuple[str, ...],
    execution_direction_mode: ExecutionDirectionMode,
) -> AnalysisAssessment | None | AnalysisFinalPayloadValidationResult:
    schema_contract_md = analysis_final_payload_contract_markdown(
        execution_direction_mode=execution_direction_mode
    )
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return _invalid(
            error_code="analysis_assessment_type_violation",
            message="analysis_assessment must be null or a JSON object.",
            field_path="analysis_assessment",
            schema_contract_md=schema_contract_md,
        )
    exposure_field = _find_exposure_field_path(value)
    if exposure_field is not None:
        return _invalid(
            error_code="analysis_assessment_exposure_field_violation",
            message=(
                "analysis_assessment must be exposure-blind and must not include "
                f"field {exposure_field!r}."
            ),
            field_path=exposure_field,
            schema_contract_md=schema_contract_md,
        )
    if "analysis_price_semantics" in value:
        return _invalid(
            error_code="analysis_assessment_runtime_owned_field_violation",
            message=(
                "analysis_assessment must not include analysis_price_semantics. "
                "That field is runtime-owned deterministic truth derived from "
                "price_level_roles plus market context."
            ),
            field_path="analysis_assessment.analysis_price_semantics",
            suggested_action=(
                "Remove analysis_assessment.analysis_price_semantics from the final "
                "payload. Emit only price_level_roles; runtime will derive "
                "analysis_price_semantics after validation."
            ),
            schema_contract_md=schema_contract_md,
        )
    try:
        assessment = parse_analysis_assessment(
            value,
            execution_direction_mode=execution_direction_mode,
        )
    except AnalysisAssessmentContractError as exc:
        message = analysis_assessment_contract_repair_message(exc)
        return _invalid(
            error_code="analysis_assessment_contract_violation",
            message=message,
            field_path=analysis_assessment_contract_field_path(message),
            suggested_action=analysis_assessment_contract_repair_suggestion(
                message,
                execution_direction_mode=execution_direction_mode,
            ),
            schema_contract_md=analysis_assessment_contract_schema_excerpt(
                message,
                execution_direction_mode=execution_direction_mode,
            ),
        )
    if assessment.target_key != expected_target_key:
        return _invalid(
            error_code="analysis_assessment_target_violation",
            message=(
                "analysis_assessment.target_key must match active target_key. "
                f"expected={expected_target_key!r}, actual={assessment.target_key!r}."
            ),
            field_path="analysis_assessment.target_key",
            schema_contract_md=schema_contract_md,
        )
    if not set(expected_event_ids).issubset(set(assessment.source_event_ids)):
        return _invalid(
            error_code="analysis_assessment_source_event_ids_violation",
            message=(
                "analysis_assessment.source_event_ids must cover every active "
                "request event_id."
            ),
            field_path="analysis_assessment.source_event_ids",
            schema_contract_md=schema_contract_md,
        )
    direction_violations = analysis_assessment_direction_policy_violations(
        as_if_flat_state=assessment.as_if_flat_state,
        role_if_already_short_values=tuple(
            level.role_if_already_short for level in assessment.price_level_roles
        ),
        execution_direction_mode=execution_direction_mode,
        field_prefix="analysis_assessment",
    )
    if direction_violations:
        violation = direction_violations[0]
        return _invalid(
            error_code="analysis_assessment_execution_direction_mode_violation",
            message=violation.message,
            field_path=violation.field_path,
            suggested_action=(
                "Repair analysis_assessment so its directional fields match the active "
                "target execution_direction_mode.\n\n"
                f"{analysis_assessment_contract_markdown(execution_direction_mode=execution_direction_mode)}"
            ),
            schema_contract_md=schema_contract_md,
        )
    return assessment


def _validate_analysis_price_basis_integrity(
    assessment: AnalysisAssessment | None,
    *,
    active_instrument_basis: str | None,
    execution_direction_mode: ExecutionDirectionMode,
) -> AnalysisFinalPayloadValidationResult | None:
    if assessment is None:
        return None
    expected_basis = (
        None
        if active_instrument_basis is None
        else canonicalize_instrument_basis(active_instrument_basis) or None
    )
    for index, level in enumerate(assessment.price_level_roles):
        is_actionable = is_semantically_active_price_level(level)
        if level.source_type == "source_quoted" and is_actionable:
            field_path = f"analysis_assessment.price_level_roles[{index}].source_type"
            return _invalid(
                error_code="analysis_source_quoted_actionability_violation",
                message=(
                    f"{field_path} cannot be source_quoted when the level is "
                    f"actionable. level_id={level.level_id!r}. Source-quoted prices "
                    "are evidence context, not verified active-instrument levels."
                ),
                field_path=field_path,
                suggested_action=(
                    "Keep the quoted level informational by setting every role to "
                    "not_relevant, clearing refresh_triggers and "
                    "invalidation_triggers, and setting path_context_required=false. "
                    "If the quote identifies a tradable setup, emit a separate "
                    "active-basis level supported by visible target market bars; do "
                    "not relabel the quoted numeric value as the active instrument.\n\n"
                    f"{price_level_role_contract_markdown(execution_direction_mode=execution_direction_mode)}"
                ),
                schema_contract_md=price_level_role_contract_markdown(
                    execution_direction_mode=execution_direction_mode
                ),
            )
        if expected_basis is None:
            continue
        actual_basis = canonicalize_instrument_basis(level.instrument_basis)
        basis_must_match = level.source_type == "market_bar_derived" or is_actionable
        if basis_must_match and actual_basis != expected_basis:
            field_path = f"analysis_assessment.price_level_roles[{index}].instrument_basis"
            return _invalid(
                error_code="analysis_price_basis_integrity_violation",
                message=(
                    f"{field_path} must match the active tradable instrument basis "
                    f"for an actionable or market-bar-derived level. "
                    f"expected={expected_basis!r}, actual={actual_basis!r}, "
                    f"source_type={level.source_type!r}, level_id={level.level_id!r}."
                ),
                field_path=field_path,
                suggested_action=(
                    "Emit an actionable level whose instrument_basis and numeric "
                    "value both belong to the active tradable instrument. Do not "
                    "relabel a value from another instrument.\n\n"
                    f"{price_level_role_contract_markdown(execution_direction_mode=execution_direction_mode)}"
                ),
                schema_contract_md=price_level_role_contract_markdown(
                    execution_direction_mode=execution_direction_mode
                ),
            )
    return None


def analysis_assessment_contract_repair_message(
    exc: AnalysisAssessmentContractError,
) -> str:
    message = str(exc)
    if message.startswith("analysis_assessment."):
        return message
    for field_name in (
        *analysis_assessment_required_fields(),
        *analysis_assessment_optional_fields(),
    ):
        if message.startswith(f"{field_name} "):
            return f"analysis_assessment.{message}"
    return f"analysis_assessment contract violation: {message}"


def analysis_assessment_contract_repair_suggestion(
    message: str,
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> str:
    if "must not include analysis_price_semantics" in message:
        return (
            "Remove analysis_assessment.analysis_price_semantics entirely. Emit only "
            "price_level_roles; runtime derives deterministic price semantics after "
            "validation.\n\n"
            f"{analysis_assessment_contract_markdown(execution_direction_mode=execution_direction_mode)}"
        )
    if "analysis_price_semantics" in message:
        return (
            "Repair analysis_assessment.analysis_price_semantics. When present, it "
            "must be a full AnalysisPriceSemantics object with exact fields, finite "
            "numeric prices, and active_price_level_ids drawn from the parent "
            "price_level_roles.\n\n"
            f"{analysis_price_semantics_contract_markdown()}"
        )
    if "price_level_roles" in message:
        if " must be one of:" in message:
            return (
                "Repair only the reported PriceLevelRole field using one of the "
                "listed allowed values; preserve the remaining validated fields.\n\n"
                f"{price_level_role_contract_markdown(execution_direction_mode=execution_direction_mode)}"
            )
        return (
            "Repair analysis_assessment.price_level_roles. Each item must be a full "
            "PriceLevelRole object; do not emit incomplete legacy objects like "
            "{price, role, stance}.\n\n"
            f"{price_level_role_contract_markdown(execution_direction_mode=execution_direction_mode)}"
        )
    return (
        "Repair analysis_assessment so it exactly satisfies the strict contract.\n\n"
        f"{analysis_final_payload_contract_markdown(execution_direction_mode=execution_direction_mode)}"
    )


def analysis_assessment_contract_schema_excerpt(
    message: str,
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> str:
    if "must not include analysis_price_semantics" in message:
        return analysis_assessment_contract_markdown(
            execution_direction_mode=execution_direction_mode
        )
    if "analysis_price_semantics" in message:
        return analysis_price_semantics_contract_markdown()
    if "price_level_roles" in message:
        return price_level_role_contract_markdown(
            execution_direction_mode=execution_direction_mode
        )
    return analysis_final_payload_contract_markdown(
        execution_direction_mode=execution_direction_mode
    )


def analysis_assessment_contract_field_path(message: str) -> str | None:
    match = re.match(
        r"^(analysis_assessment(?:\.[A-Za-z0-9_]+(?:\[\d+\])?)*)"
        r"(?=\s|\.|:|$)",
        message,
    )
    if match is None:
        return None
    return match.group(1)


def price_level_role_repair_details() -> dict[str, object]:
    return {
        "invalid_legacy_shape": invalid_legacy_price_level_role_shape(),
        "required_price_level_role_fields": price_level_role_required_fields(),
        "price_level_role_contract": price_level_role_contract_markdown(),
    }


def _find_exposure_field_path(value: object, *, prefix: str = "analysis_assessment") -> str | None:
    forbidden_terms = {
        "portfolio_state",
        "actual_target_weight",
        "actual_current_state",
        "current_target_weight",
        "entry_price",
        "pnl",
        "mfe",
        "mae",
        "pm_decision",
        "execution_record",
    }
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}"
            if key_text.casefold() in forbidden_terms:
                return path
            nested_path = _find_exposure_field_path(nested, prefix=path)
            if nested_path is not None:
                return nested_path
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            nested_path = _find_exposure_field_path(nested, prefix=f"{prefix}[{index}]")
            if nested_path is not None:
                return nested_path
    return None


def _invalid(
    *,
    error_code: str,
    message: str,
    field_path: str | None,
    suggested_action: str | None = None,
    schema_contract_md: str | None = None,
) -> AnalysisFinalPayloadValidationResult:
    return AnalysisFinalPayloadValidationResult(
        ok=False,
        error_code=error_code,
        message=message,
        field_path=field_path,
        suggested_action=(
            suggested_action
            if suggested_action is not None
            else "Repair the final JSON payload and call validate_analysis_final_payload again."
        ),
        schema_contract_md=schema_contract_md or analysis_final_payload_contract_markdown(),
    )


__all__ = [
    "AnalysisFinalPayloadValidationResult",
    "analysis_assessment_contract_field_path",
    "analysis_assessment_contract_repair_message",
    "analysis_assessment_contract_repair_suggestion",
    "analysis_assessment_contract_schema_excerpt",
    "price_level_role_repair_details",
    "validate_analysis_final_payload",
]
