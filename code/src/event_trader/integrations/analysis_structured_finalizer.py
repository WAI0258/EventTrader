"""Optional structured finalizer boundary for analysis final payloads."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from event_trader.contracts.analysis_assessment_schema import (
    analysis_final_payload_contract_markdown,
    analysis_final_payload_json_schema,
    analysis_final_payload_required_fields,
    forbidden_analysis_final_payload_fields,
)
from event_trader.contracts.analysis_final_payload_validation import (
    AnalysisFinalPayloadValidationResult,
    validate_analysis_final_payload,
)
from event_trader.contracts.execution_direction_policy import (
    ExecutionDirectionMode,
)
from event_trader.integrations.boxed_json import (
    BoxedJsonPayloadError,
    load_first_boxed_json_object,
)

AnalysisStructuredOutputMode = Literal[
    "auto",
    "native",
    "validated_prompt",
    "disabled",
]
ProviderNativeMode = Literal[
    "openai_json_schema",
    "anthropic_forced_tool",
    "unavailable",
]

_STRUCTURED_OUTPUT_MODES = ("auto", "native", "validated_prompt", "disabled")


class AnalysisStructuredFinalizerError(RuntimeError):
    """Raised when optional structured analysis finalization cannot validate output."""


@dataclass(frozen=True, slots=True)
class AnalysisStructuredCapabilityResult:
    provider: str
    native_available: bool
    native_mode: ProviderNativeMode
    reason: str
    base_url_host: str = ""
    model: str = ""


@dataclass(frozen=True, slots=True)
class AnalysisStructuredFinalizerConfig:
    mode: AnalysisStructuredOutputMode = "disabled"
    probe_enabled: bool = True


@dataclass(frozen=True, slots=True)
class AnalysisStructuredFinalizerInput:
    expected_target_key: str
    expected_event_ids: tuple[str, ...]
    included_lesson_ids: tuple[str, ...]
    execution_direction_mode: ExecutionDirectionMode
    active_instrument_basis: str | None
    draft_payload: Mapping[str, object] | None
    final_summary: str
    final_boxed_answer: str


@dataclass(frozen=True, slots=True)
class AnalysisStructuredFinalizerResult:
    payload: dict[str, Any]
    mode_used: AnalysisStructuredOutputMode
    native_capability: AnalysisStructuredCapabilityResult | None = None


class AnalysisStructuredProviderClient(Protocol):
    def probe_native_structured_output(
        self,
        *,
        provider: str,
        response_format: Mapping[str, object],
    ) -> AnalysisStructuredCapabilityResult:
        ...

    def finalize_native_analysis_payload(
        self,
        *,
        prompt: str,
        response_format: Mapping[str, object],
    ) -> Mapping[str, object]:
        ...


PromptFinalizer = Callable[[str], str]


def allowed_structured_output_modes() -> tuple[str, ...]:
    return _STRUCTURED_OUTPUT_MODES


def normalize_analysis_structured_output_mode(value: object) -> AnalysisStructuredOutputMode:
    if not isinstance(value, str):
        raise AnalysisStructuredFinalizerError("structured_output_mode must be a string.")
    normalized = value.strip().lower()
    if normalized not in _STRUCTURED_OUTPUT_MODES:
        raise AnalysisStructuredFinalizerError(
            "structured_output_mode must be one of: "
            f"{', '.join(_STRUCTURED_OUTPUT_MODES)}."
        )
    return cast(AnalysisStructuredOutputMode, normalized)


def finalize_analysis_payload(
    finalizer_input: AnalysisStructuredFinalizerInput,
    *,
    config: AnalysisStructuredFinalizerConfig,
    provider: str,
    provider_client: AnalysisStructuredProviderClient | None = None,
    prompt_finalizer: PromptFinalizer | None = None,
) -> AnalysisStructuredFinalizerResult:
    mode = normalize_analysis_structured_output_mode(config.mode)
    if mode == "disabled":
        if finalizer_input.draft_payload is None:
            raise AnalysisStructuredFinalizerError(
                "structured finalizer is disabled and no draft payload was available."
            )
        return AnalysisStructuredFinalizerResult(
            payload=dict(finalizer_input.draft_payload),
            mode_used="disabled",
        )
    if mode == "validated_prompt":
        return AnalysisStructuredFinalizerResult(
            payload=_validated_prompt_payload(
                finalizer_input,
                prompt_finalizer=prompt_finalizer,
            ),
            mode_used="validated_prompt",
        )

    capability = _probe_native_capability(
        provider=provider,
        config=config,
        provider_client=provider_client,
        execution_direction_mode=finalizer_input.execution_direction_mode,
    )
    if capability.native_available:
        payload = _native_payload(
            finalizer_input,
            provider_client=provider_client,
            native_capability=capability,
        )
        return AnalysisStructuredFinalizerResult(
            payload=payload,
            mode_used="native",
            native_capability=capability,
        )
    if mode == "native":
        raise AnalysisStructuredFinalizerError(
            "analysis structured native finalizer unavailable: " + capability.reason
        )
    return AnalysisStructuredFinalizerResult(
        payload=_validated_prompt_payload(
            finalizer_input,
            prompt_finalizer=prompt_finalizer,
        ),
        mode_used="validated_prompt",
        native_capability=capability,
    )


def analysis_final_payload_response_format(
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "analysis_final_payload",
            "strict": True,
            "schema": analysis_final_payload_json_schema(
                execution_direction_mode=execution_direction_mode
            ),
        },
    }


def _probe_native_capability(
    *,
    provider: str,
    config: AnalysisStructuredFinalizerConfig,
    provider_client: AnalysisStructuredProviderClient | None,
    execution_direction_mode: ExecutionDirectionMode,
) -> AnalysisStructuredCapabilityResult:
    normalized_provider = provider.strip().lower() if isinstance(provider, str) else ""
    if not config.probe_enabled:
        return AnalysisStructuredCapabilityResult(
            provider=normalized_provider,
            native_available=False,
            native_mode="unavailable",
            reason="structured output probe disabled",
        )
    if provider_client is None:
        return AnalysisStructuredCapabilityResult(
            provider=normalized_provider,
            native_available=False,
            native_mode="unavailable",
            reason="no structured provider client configured",
        )
    try:
        capability = provider_client.probe_native_structured_output(
            provider=normalized_provider,
            response_format=analysis_final_payload_response_format(
                execution_direction_mode=execution_direction_mode
            ),
        )
    except Exception as exc:
        return AnalysisStructuredCapabilityResult(
            provider=normalized_provider,
            native_available=False,
            native_mode="unavailable",
            reason=f"structured output probe failed: {exc}",
        )
    if capability.native_available:
        if normalized_provider == "anthropic" and capability.native_mode != (
            "anthropic_forced_tool"
        ):
            return AnalysisStructuredCapabilityResult(
                provider=normalized_provider,
                native_available=False,
                native_mode="unavailable",
                reason="Anthropic-compatible native finalizer requires forced tool use.",
            )
        if normalized_provider == "openai" and capability.native_mode != (
            "openai_json_schema"
        ):
            return AnalysisStructuredCapabilityResult(
                provider=normalized_provider,
                native_available=False,
                native_mode="unavailable",
                reason="OpenAI-compatible native finalizer requires strict json_schema.",
            )
    return capability


def _native_payload(
    finalizer_input: AnalysisStructuredFinalizerInput,
    *,
    provider_client: AnalysisStructuredProviderClient | None,
    native_capability: AnalysisStructuredCapabilityResult,
) -> dict[str, Any]:
    if provider_client is None:
        raise AnalysisStructuredFinalizerError("native finalizer requires provider client.")
    try:
        raw_payload = provider_client.finalize_native_analysis_payload(
            prompt=_finalizer_prompt(finalizer_input),
            response_format=analysis_final_payload_response_format(
                execution_direction_mode=finalizer_input.execution_direction_mode
            ),
        )
    except Exception as exc:
        raise AnalysisStructuredFinalizerError(
            f"native structured finalizer failed: {exc}"
        ) from exc
    payload = dict(raw_payload)
    _raise_if_invalid(
        payload,
        finalizer_input=finalizer_input,
        mode_label=f"native:{native_capability.native_mode}",
    )
    return payload


def _validated_prompt_payload(
    finalizer_input: AnalysisStructuredFinalizerInput,
    *,
    prompt_finalizer: PromptFinalizer | None,
) -> dict[str, Any]:
    if prompt_finalizer is None:
        if finalizer_input.draft_payload is None:
            payload = _load_boxed_payload(finalizer_input)
        else:
            payload = dict(finalizer_input.draft_payload)
    else:
        payload = _load_boxed_payload(
            finalizer_input,
            finalizer_text=prompt_finalizer(_finalizer_prompt(finalizer_input)),
        )
    _raise_if_invalid(payload, finalizer_input=finalizer_input, mode_label="validated_prompt")
    return payload


def _load_boxed_payload(
    finalizer_input: AnalysisStructuredFinalizerInput,
    *,
    finalizer_text: str | None = None,
) -> dict[str, Any]:
    try:
        return load_first_boxed_json_object(
            final_boxed_answer=finalizer_text or finalizer_input.final_boxed_answer,
            final_summary=finalizer_input.final_summary,
            object_start_fields=analysis_final_payload_required_fields(),
            missing_error="analysis structured finalizer produced no boxed JSON payload.",
            non_json_error="analysis structured finalizer produced non-JSON output.",
            non_object_error="analysis structured finalizer must produce one JSON object.",
        )
    except BoxedJsonPayloadError as exc:
        raise AnalysisStructuredFinalizerError(str(exc)) from exc


def _raise_if_invalid(
    payload: Mapping[str, object],
    *,
    finalizer_input: AnalysisStructuredFinalizerInput,
    mode_label: str,
) -> None:
    validation = validate_analysis_final_payload(
        payload,
        expected_target_key=finalizer_input.expected_target_key,
        expected_event_ids=finalizer_input.expected_event_ids,
        included_lesson_ids=finalizer_input.included_lesson_ids,
        execution_direction_mode=finalizer_input.execution_direction_mode,
        active_instrument_basis=finalizer_input.active_instrument_basis,
    )
    if not validation.ok:
        raise AnalysisStructuredFinalizerError(
            f"analysis structured finalizer {mode_label} returned invalid payload: "
            f"{_validation_error_text(validation)}"
        )


def _validation_error_text(validation: AnalysisFinalPayloadValidationResult) -> str:
    parts = [validation.error_code or "analysis_payload_contract_violation"]
    if validation.field_path:
        parts.append(f"field_path={validation.field_path}")
    parts.append(validation.message)
    return "; ".join(parts)


def _finalizer_prompt(finalizer_input: AnalysisStructuredFinalizerInput) -> str:
    draft_json = (
        "{}"
        if finalizer_input.draft_payload is None
        else json.dumps(dict(finalizer_input.draft_payload), ensure_ascii=False, indent=2)
    )
    forbidden = ", ".join(forbidden_analysis_final_payload_fields())
    return (
        f"{_analysis_finalizer_role_prompt()}"
        "Return exactly one JSON object wrapped in \\boxed{...} unless native "
        "structured output is being used.\n"
        f"Expected target_key: {finalizer_input.expected_target_key}\n"
        f"Expected event_ids: {json.dumps(list(finalizer_input.expected_event_ids))}\n"
        f"Included lesson IDs: {json.dumps(list(finalizer_input.included_lesson_ids))}\n"
        "Active execution_direction_mode: "
        f"{finalizer_input.execution_direction_mode}\n"
        "Active tradable instrument basis: "
        f"{finalizer_input.active_instrument_basis or 'unavailable'}\n"
        f"Forbidden final payload fields: {forbidden}\n\n"
        f"{analysis_final_payload_contract_markdown(execution_direction_mode=finalizer_input.execution_direction_mode)}\n\n"
        "Draft ReAct payload:\n"
        f"{draft_json}\n\n"
        "ReAct final summary:\n"
        f"{finalizer_input.final_summary}"
    )


def _analysis_finalizer_role_prompt() -> str:
    return (
        "Role:\n"
        "You are the analysis payload finalizer for event-trader.\n"
        "Your only job is to convert the draft ReAct result into the exact "
        "Analysis final JSON contract. Do not add new decisions, new evidence, or "
        "extra fields.\n\n"
    )


__all__ = [
    "AnalysisStructuredCapabilityResult",
    "AnalysisStructuredFinalizerConfig",
    "AnalysisStructuredFinalizerError",
    "AnalysisStructuredFinalizerInput",
    "AnalysisStructuredFinalizerResult",
    "AnalysisStructuredOutputMode",
    "AnalysisStructuredProviderClient",
    "allowed_structured_output_modes",
    "analysis_final_payload_response_format",
    "finalize_analysis_payload",
    "normalize_analysis_structured_output_mode",
]
