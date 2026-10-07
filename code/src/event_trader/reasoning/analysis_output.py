"""Provider-neutral analysis output parsing, finalization, and normalization."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any

from event_trader.analysis import AnalysisContext
from event_trader.contracts import AnalysisResult
from event_trader.contracts.analysis_assessment_schema import (
    analysis_final_payload_contract_markdown,
)
from event_trader.contracts.analysis_final_payload_validation import (
    AnalysisFinalPayloadValidationResult,
    price_level_role_repair_details,
    validate_analysis_final_payload,
)
from event_trader.contracts.execution_direction_policy import (
    ExecutionDirectionMode,
    validate_execution_direction_mode,
)
from event_trader.integrations.analysis_structured_finalizer import (
    AnalysisStructuredFinalizerConfig,
    AnalysisStructuredFinalizerError,
    AnalysisStructuredFinalizerInput,
    AnalysisStructuredOutputMode,
    finalize_analysis_payload,
)
from event_trader.integrations.analysis_structured_provider import (
    AnalysisStructuredProviderAdapter,
    AnalysisStructuredProviderConfig,
)
from event_trader.integrations.boxed_json import (
    BoxedJsonPayloadError,
    load_first_boxed_json_object,
)
from event_trader.reasoning.analysis_contract_repair import (
    ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
    AnalysisContractFailure,
    AnalysisContractRepairRequired,
)
from event_trader.research_memory.analysis_price_basis import (
    canonicalize_analysis_assessment_price_bases,
)
from event_trader.research_memory.analysis_price_semantics_assembler import (
    enrich_assessment_with_analysis_price_semantics,
)


class AnalysisOutputError(RuntimeError):
    """Raised when analysis output cannot enter the repairable contract path."""


@dataclass(frozen=True, slots=True)
class AnalysisOutputFinalizationConfig:
    structured_output_mode: AnalysisStructuredOutputMode
    structured_output_probe: bool
    execution_direction_mode: ExecutionDirectionMode
    provider: str
    model: str
    api_key: str
    base_url: str


def load_analysis_payload(
    *,
    final_boxed_answer: str,
    final_summary: str,
    fallback_payload_texts: Iterable[str] = (),
    attempt_index: int = 0,
    agent_label: str = "analysis agent",
) -> dict[str, Any]:
    """Load one boxed JSON object or request a bounded contract repair."""

    missing_boxed_json_error = (
        f"{agent_label} did not return a boxed JSON payload. Final summary was: {final_summary}"
    )
    non_json_error = f"{agent_label} returned non-JSON boxed output."
    non_object_error = f"{agent_label} must return one JSON object."
    try:
        return load_first_boxed_json_object(
            final_boxed_answer=final_boxed_answer,
            final_summary=final_summary,
            fallback_payload_texts=fallback_payload_texts,
            object_start_fields=(
                "target_key",
                "event_ids",
                "used_lesson_ids",
                "analysis_assessment",
            ),
            missing_error=missing_boxed_json_error,
            non_json_error=non_json_error,
            non_object_error=non_object_error,
        )
    except BoxedJsonPayloadError as exc:
        message = str(exc)
        if message == missing_boxed_json_error:
            failure_code = "missing_boxed_json"
        elif message == non_json_error:
            failure_code = "boxed_non_json"
        elif message == non_object_error:
            failure_code = "boxed_non_object"
        else:
            raise AnalysisOutputError(message) from exc
        raise AnalysisContractRepairRequired(
            failure=AnalysisContractFailure(
                surface="final_output",
                error_code=failure_code,
                message=message,
            ),
            failure_count=attempt_index + 1,
            threshold=ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
        ) from None


def finalize_analysis_output_payload(
    payload: dict[str, Any],
    *,
    attempt_index: int,
    config: AnalysisOutputFinalizationConfig,
    context: AnalysisContext,
    included_lesson_ids: tuple[str, ...],
) -> dict[str, Any]:
    """Apply the configured structured finalizer without weakening its contract."""

    if config.structured_output_mode == "disabled":
        return payload
    execution_direction_mode = validate_execution_direction_mode(
        config.execution_direction_mode,
        error_type=AnalysisOutputError,
    )
    provider_client = (
        AnalysisStructuredProviderAdapter(
            AnalysisStructuredProviderConfig(
                provider=config.provider,
                model=config.model,
                api_key=config.api_key,
                base_url=config.base_url,
            )
        )
        if config.structured_output_mode in {"auto", "native"}
        else None
    )
    try:
        result = finalize_analysis_payload(
            AnalysisStructuredFinalizerInput(
                expected_target_key=context.request.target_key,
                expected_event_ids=tuple(context.request.event_ids),
                included_lesson_ids=included_lesson_ids,
                execution_direction_mode=execution_direction_mode,
                active_instrument_basis=(
                    None
                    if context.market_context is None
                    else context.market_context.tradable_proxy_symbol
                ),
                draft_payload=payload,
                final_summary=json.dumps(payload, ensure_ascii=False),
                final_boxed_answer="",
            ),
            config=AnalysisStructuredFinalizerConfig(
                mode=config.structured_output_mode,
                probe_enabled=config.structured_output_probe,
            ),
            provider=config.provider,
            provider_client=provider_client,
        )
    except AnalysisStructuredFinalizerError as exc:
        schema_contract_md = analysis_final_payload_contract_markdown(
            execution_direction_mode=execution_direction_mode
        )
        raise AnalysisContractRepairRequired(
            failure=AnalysisContractFailure(
                surface="final_output",
                error_code="analysis_structured_finalizer_error",
                message=str(exc),
                suggested_action=schema_contract_md,
                details={"schema_contract_md": schema_contract_md},
            ),
            failure_count=attempt_index + 1,
            threshold=ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
        ) from None
    return result.payload


def normalize_analysis_result(
    payload: dict[str, Any],
    *,
    context: AnalysisContext,
    attempt_index: int = 0,
    included_lesson_ids: tuple[str, ...] = (),
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> AnalysisResult:
    """Validate and normalize the business result produced by one agent attempt."""

    validation = validate_analysis_final_payload(
        payload,
        expected_target_key=context.request.target_key,
        expected_event_ids=tuple(context.request.event_ids),
        included_lesson_ids=included_lesson_ids,
        execution_direction_mode=execution_direction_mode,
        active_instrument_basis=(
            None
            if context.market_context is None
            else context.market_context.tradable_proxy_symbol
        ),
    )
    if not validation.ok:
        _raise_analysis_contract_repair_required(
            validation,
            attempt_index=attempt_index,
        )
    canonical_assessment = canonicalize_analysis_assessment_price_bases(
        validation.analysis_assessment
    )
    assessment = enrich_assessment_with_analysis_price_semantics(
        canonical_assessment,
        market_context=context.market_context,
    )
    return AnalysisResult(
        target_key=validation.target_key or context.request.target_key,
        event_ids=list(validation.event_ids),
        outcome="no_update",
        analysis_assessment=assessment,
        used_lesson_ids=validation.used_lesson_ids,
    )


def with_runtime_analysis_assessment_id(
    result: AnalysisResult,
    *,
    context: AnalysisContext,
) -> AnalysisResult:
    """Replace a model-provided assessment ID with the stable runtime identity."""

    assessment = result.analysis_assessment
    if assessment is None:
        return result
    return replace(
        result,
        analysis_assessment=replace(
            assessment,
            assessment_id=derive_analysis_assessment_storage_id(
                target_key=context.request.target_key,
                analysis_unit_id=(
                    None
                    if context.unit_formation_lane is None
                    else context.unit_formation_lane.analysis_unit_id
                ),
                decision_episode_id=context.request.decision_episode_id,
                event_ids=tuple(context.request.event_ids),
            ),
        ),
    )


def derive_analysis_assessment_storage_id(
    *,
    target_key: str,
    analysis_unit_id: str | None,
    decision_episode_id: str,
    event_ids: tuple[str, ...],
) -> str:
    """Derive the canonical append-only assessment identity from runtime truth."""

    if analysis_unit_id:
        identity_payload: dict[str, object] = {
            "target_key": target_key,
            "analysis_unit_id": analysis_unit_id,
        }
    elif decision_episode_id:
        identity_payload = {
            "target_key": target_key,
            "decision_episode_id": decision_episode_id,
        }
    else:
        identity_payload = {
            "target_key": target_key,
            "event_ids": sorted(event_ids),
        }
    identity = json.dumps(
        identity_payload,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = sha256(identity.encode("utf-8")).hexdigest()
    return f"analysis-assessment:{target_key}:{digest[:24]}"


def _raise_analysis_contract_repair_required(
    validation: AnalysisFinalPayloadValidationResult,
    *,
    attempt_index: int = 0,
) -> None:
    details: dict[str, object] = {}
    if validation.field_path:
        details["field_path"] = validation.field_path
    if validation.schema_contract_md:
        details["schema_contract_md"] = validation.schema_contract_md
    if validation.field_path and "price_level_roles" in validation.field_path:
        details.update(price_level_role_repair_details())
    raise AnalysisContractRepairRequired(
        failure=AnalysisContractFailure(
            surface="final_output",
            error_code=validation.error_code or "analysis_payload_contract_violation",
            message=validation.message,
            suggested_action=validation.suggested_action or "",
            details=details,
        ),
        failure_count=attempt_index + 1,
        threshold=ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
    )


__all__ = [
    "AnalysisOutputError",
    "AnalysisOutputFinalizationConfig",
    "derive_analysis_assessment_storage_id",
    "finalize_analysis_output_payload",
    "load_analysis_payload",
    "normalize_analysis_result",
    "with_runtime_analysis_assessment_id",
]
