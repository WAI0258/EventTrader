"""Provider-neutral bounded supervision for Reflection agent attempts."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Literal

from event_trader.integrations.boxed_json import (
    BoxedJsonPayloadError,
    load_first_boxed_json_object,
)
from event_trader.reasoning.runtime import AgentRunReceipt
from event_trader.reflection.agent_contract import REQUIRED_REFLECTION_TOOL_NAMES
from event_trader.reflection.contracts import (
    MARKET_CONTEXT_USAGE_QUALITY_LABELS,
    ReflectionContractError,
)
from event_trader.reflection.learning_contracts import ReflectionLearningContractError
from event_trader.reflection.output import ReflectionOutputContractError

REFLECTION_EVALUATION_MAX_ATTEMPTS = 3

_REFLECTION_READ_TOOL_NAMES = frozenset(REQUIRED_REFLECTION_TOOL_NAMES)
_TRADE_VERDICTS = ("win", "loss", "scratch", "missed", "not_taken", "open")
_CLOSED_TRADE_VERDICTS = tuple(verdict for verdict in _TRADE_VERDICTS if verdict != "open")
_MEMORY_STATUSES = ("provisional", "validated", "invalidated", "finalized")
_VALIDATION_BASES = (
    "current_evidence",
    "later_evidence",
    "market_return",
    "execution_feedback",
    "portfolio_feedback",
    "close_review",
)
_SOURCE_REF_KEYS = (
    "evidence_event_ids",
    "market_bar_refs",
    "review_paths",
    "pm_review_request_ids",
    "pm_decision_ids",
    "execution_record_ids",
    "portfolio_record_ids",
)
_REFLECTION_PAYLOAD_START_FIELDS = (
    "assessed_horizons",
    "thesis_assessment",
    "trade_assessment",
    "review_decision",
    "decision_rationale",
    "learning_decision",
    "episode_id",
    "target_key",
)
type ReflectionRepairPath = tuple[str | int, ...]
type ReflectionRepairMode = Literal["full_retry", "merge_paths"]


@dataclass(frozen=True, slots=True)
class ReflectionRepairPlan:
    mode: ReflectionRepairMode
    paths: tuple[ReflectionRepairPath, ...] = ()

    def __post_init__(self) -> None:
        if self.mode == "full_retry" and self.paths:
            raise ValueError("full_retry repair plans must not define paths.")
        if self.mode == "merge_paths" and not self.paths:
            raise ValueError("merge_paths repair plans require at least one path.")
        if any(not path for path in self.paths):
            raise ValueError("Reflection repair paths must not target the root payload.")


_FULL_RETRY_PLAN = ReflectionRepairPlan(mode="full_retry")


@dataclass(frozen=True, slots=True)
class ReflectionContractFailure:
    surface: str
    error_code: str
    message: str
    recoverable: bool
    suggested_action: str
    details: Mapping[str, object]
    repair_plan: ReflectionRepairPlan


class ReflectionContractRepairExhausted(RuntimeError):
    """Raised after a Reflection attempt and all bounded repairs fail."""

    def __init__(
        self,
        failure: ReflectionContractFailure,
        *,
        attempt_count: int,
        max_attempts: int,
    ) -> None:
        self.failure = failure
        self.attempt_count = attempt_count
        self.max_attempts = max_attempts
        super().__init__(
            format_reflection_contract_repair_failure(
                failure,
                attempt_count=attempt_count,
                max_attempts=max_attempts,
            )
        )


def format_reflection_contract_repair_failure(
    failure: ReflectionContractFailure,
    *,
    attempt_count: int,
    max_attempts: int,
) -> str:
    attempted = failure.details.get("attempted_tools")
    attempted_text = ""
    if isinstance(attempted, tuple):
        attempted_text = f" attempted_tools={','.join(attempted) or '<none>'}"
    return (
        "Reflection contract repair required: "
        f"surface={failure.surface} "
        f"error_code={failure.error_code} "
        f"attempt={attempt_count}/{max_attempts}"
        f"{attempted_text}: {failure.message}"
    )


async def run_reflection_agent_with_contract_repair[ResultT](
    *,
    task_description: str,
    run_attempt: Callable[[str], Awaitable[AgentRunReceipt]],
    evaluate_run_result: Callable[[AgentRunReceipt], ResultT],
    classify_failure: Callable[[Exception], ReflectionContractFailure | None],
    append_repair_feedback: Callable[[str, ReflectionContractFailure], str],
) -> ResultT:
    """Run Reflection once plus at most two project-supervised repairs."""

    original_task_description = task_description.rstrip()
    current_task_description = original_task_description
    repair_reference: dict[str, object] | None = None
    pending_repair_plan = _FULL_RETRY_PLAN
    for attempt_count in range(1, REFLECTION_EVALUATION_MAX_ATTEMPTS + 1):
        attempt = await run_attempt(current_task_description)
        attempt_payload = _load_repair_payload(attempt)
        evaluation_attempt = attempt
        evaluation_payload = attempt_payload
        if (
            repair_reference is not None
            and pending_repair_plan.mode == "merge_paths"
            and attempt_payload is not None
        ):
            evaluation_payload = _merge_repair_paths(
                reference=repair_reference,
                repair_candidate=attempt_payload,
                paths=pending_repair_plan.paths,
            )
            evaluation_attempt = _with_repair_payload(
                attempt,
                payload=evaluation_payload,
                plan=pending_repair_plan,
            )
        failure: ReflectionContractFailure | None = None
        try:
            result = evaluate_run_result(evaluation_attempt)
            failure = reflection_read_audit_failure(evaluation_attempt)
        except Exception as exc:
            failure = classify_failure(exc)
            if failure is None:
                raise
        if failure is None:
            return result
        repair_plan = _resolve_repair_plan(
            failure=failure,
            rejected_payload=evaluation_payload,
        )
        failure = _attach_repair_context(
            failure,
            rejected_payload=evaluation_payload,
            repair_plan=repair_plan,
        )
        if repair_plan.mode == "merge_paths" and evaluation_payload is not None:
            repair_reference = evaluation_payload
        else:
            repair_reference = None
        pending_repair_plan = repair_plan
        if not failure.recoverable or attempt_count >= REFLECTION_EVALUATION_MAX_ATTEMPTS:
            raise ReflectionContractRepairExhausted(
                failure,
                attempt_count=attempt_count,
                max_attempts=REFLECTION_EVALUATION_MAX_ATTEMPTS,
            )
        current_task_description = append_repair_feedback(
            original_task_description,
            failure,
        )
    raise AssertionError("Reflection contract repair loop ended without a result.")


def classify_reflection_contract_failure(
    exc: Exception,
) -> ReflectionContractFailure | None:
    if isinstance(exc, ReflectionOutputContractError):
        return _reflection_output_contract_failure(exc)
    if isinstance(exc, ReflectionContractError):
        return _reflection_evaluation_contract_failure(exc)
    if isinstance(exc, ReflectionLearningContractError):
        return _reflection_learning_contract_failure(exc)
    return None


def append_reflection_contract_repair_feedback(
    task_description: str,
    failure: ReflectionContractFailure,
    *,
    reads_already_satisfied: bool = False,
) -> str:
    details_json = json.dumps(failure.details, ensure_ascii=False, sort_keys=True)
    repair_boundary = ""
    if failure.repair_plan.mode == "merge_paths":
        formatted_paths = ", ".join(
            f"`{_format_repair_path(path)}`" for path in failure.repair_plan.paths
        )
        repair_boundary = (
            "Project-owned repair boundary:\n"
            "- Use details.rejected_payload as the frozen prior draft.\n"
            f"- Repair only these JSON paths: {formatted_paths}.\n"
            "- Return the complete payload, not a patch.\n"
            "- The project will copy only those exact paths into the frozen draft. "
            "Every other candidate change will be discarded.\n\n"
        )
    read_boundary = (
        "Reflection read receipts are frozen and already satisfy the read contract. "
        "Do not call or request any read tool during this decision-only repair.\n\n"
        if reads_already_satisfied
        else (
            "Before final boxed JSON, call at least one successful reflection read "
            "tool. The compact brief is orientation only and is not sufficient for a "
            "final decision. Any one of the required reflection read tools satisfies "
            "the minimum read contract.\n\n"
        )
    )
    return (
        f"{task_description.rstrip()}\n\n"
        "Previous attempt failed deterministic event-trader contract:\n"
        f"- surface: {failure.surface}\n"
        f"- error_code: {failure.error_code}\n"
        f"- message: {failure.message}\n"
        f"- recoverable: {str(failure.recoverable).lower()}\n"
        f"- suggested_action: {failure.suggested_action}\n"
        f"- details: {details_json}\n\n"
        f"{repair_boundary}"
        f"{read_boundary}"
        "Return one corrected JSON object wrapped in \\boxed{...} for the same "
        "context. Do not invent new evidence or change the trading judgment unless "
        'the deterministic error requires it. For thesis_assessment, "verdict" '
        'must be "validated", "mixed", "invalidated", or "unclear".\n'
    )


def _load_repair_payload(receipt: AgentRunReceipt) -> dict[str, object] | None:
    try:
        payload = load_first_boxed_json_object(
            final_boxed_answer=receipt.contract_output_text,
            final_summary=receipt.final_summary,
            fallback_payload_texts=receipt.fallback_payload_texts,
            object_start_fields=_REFLECTION_PAYLOAD_START_FIELDS,
            missing_error="Reflection repair payload is missing.",
            non_json_error="Reflection repair payload is not JSON.",
            non_object_error="Reflection repair payload is not an object.",
        )
    except BoxedJsonPayloadError:
        return None
    return dict(payload)


def _resolve_repair_plan(
    *,
    failure: ReflectionContractFailure,
    rejected_payload: Mapping[str, object] | None,
) -> ReflectionRepairPlan:
    if failure.error_code != "learning_decision_contract_violation":
        return failure.repair_plan
    if rejected_payload is None or not isinstance(
        rejected_payload.get("learning_decision"),
        Mapping,
    ):
        return _FULL_RETRY_PLAN
    return ReflectionRepairPlan(
        mode="merge_paths",
        paths=(("learning_decision",),),
    )


def _merge_repair_paths(
    *,
    reference: Mapping[str, object],
    repair_candidate: Mapping[str, object],
    paths: tuple[ReflectionRepairPath, ...],
) -> dict[str, object]:
    merged = deepcopy(dict(reference))
    for path in paths:
        found, candidate_value = _read_repair_path(repair_candidate, path)
        if found:
            _write_repair_path(merged, path, deepcopy(candidate_value))
    return merged


def _read_repair_path(
    payload: Mapping[str, object],
    path: ReflectionRepairPath,
) -> tuple[bool, object]:
    current: object = payload
    for segment in path:
        if isinstance(segment, str) and isinstance(current, Mapping):
            if segment not in current:
                return False, None
            current = current[segment]
            continue
        if isinstance(segment, int) and isinstance(current, list):
            if segment < 0 or segment >= len(current):
                return False, None
            current = current[segment]
            continue
        return False, None
    return True, current


def _write_repair_path(
    payload: dict[str, object],
    path: ReflectionRepairPath,
    value: object,
) -> None:
    current: object = payload
    for segment in path[:-1]:
        if isinstance(segment, str) and isinstance(current, dict):
            if segment not in current:
                return
            current = current[segment]
            continue
        if isinstance(segment, int) and isinstance(current, list):
            if segment < 0 or segment >= len(current):
                return
            current = current[segment]
            continue
        return
    final_segment = path[-1]
    if isinstance(final_segment, str) and isinstance(current, dict):
        current[final_segment] = value
    elif (
        isinstance(final_segment, int)
        and isinstance(current, list)
        and 0 <= final_segment < len(current)
    ):
        current[final_segment] = value


def _format_repair_path(path: ReflectionRepairPath) -> str:
    return "/" + "/".join(str(segment).replace("~", "~0").replace("/", "~1") for segment in path)


def _with_repair_payload(
    receipt: AgentRunReceipt,
    *,
    payload: Mapping[str, object],
    plan: ReflectionRepairPlan,
) -> AgentRunReceipt:
    return replace(
        receipt,
        final_summary="",
        contract_output_text=(
            f"\\boxed{{{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}}}"
        ),
        fallback_payload_texts=(),
        diagnostics={
            **receipt.diagnostics,
            "contract_repair_merged": True,
            "contract_repair_paths": tuple(_format_repair_path(path) for path in plan.paths),
        },
    )


def _attach_repair_context(
    failure: ReflectionContractFailure,
    *,
    rejected_payload: Mapping[str, object] | None,
    repair_plan: ReflectionRepairPlan,
) -> ReflectionContractFailure:
    details: dict[str, object] = {
        **failure.details,
        "repair_mode": repair_plan.mode,
        "repair_paths": tuple(_format_repair_path(path) for path in repair_plan.paths),
    }
    if repair_plan.mode == "merge_paths" and rejected_payload is not None:
        details["rejected_payload"] = dict(rejected_payload)
    return replace(
        failure,
        details=details,
        repair_plan=repair_plan,
    )


def is_unfinished_terminal_final_output_failure(
    failure: ReflectionContractFailure,
) -> bool:
    return failure.surface == "final_output" and failure.error_code in {
        "boxed_non_json",
        "missing_boxed_json",
    }


def _reflection_output_contract_failure(
    exc: ReflectionOutputContractError,
) -> ReflectionContractFailure:
    message = str(exc).strip()
    repair_paths = tuple(dict.fromkeys(exc.repair_paths))
    repair_plan = (
        ReflectionRepairPlan(mode="merge_paths", paths=repair_paths)
        if repair_paths
        else _FULL_RETRY_PLAN
    )
    error_code = "reflection_payload_schema_violation"
    if "did not return a boxed JSON payload" in message:
        error_code = "missing_boxed_json"
    elif "returned non-JSON boxed output" in message:
        error_code = "boxed_non_json"
    elif "must return one JSON object" in message:
        error_code = "boxed_non_object"
    elif "missing field(s)" in message:
        error_code = "reflection_payload_missing_fields"
    elif "unexpected field(s)" in message:
        error_code = "reflection_payload_unexpected_fields"
    specific_repair_guidance = _reflection_contract_repair_suggestion(message)
    if error_code == "reflection_payload_unexpected_fields" and (
        "unexpected field(s): return_pct" in message
    ):
        suggested_action = (
            "Return exactly one corrected JSON object wrapped in \\boxed{...}. "
            "Remove the top-level return_pct field. If a return is relevant, put "
            "it inside trade_assessment.return_pct. The top-level object must "
            "contain only the requested reflection fields."
        )
    elif specific_repair_guidance is not None:
        suggested_action = (
            "Return exactly one corrected JSON object wrapped in \\boxed{...}. "
            f"{specific_repair_guidance}"
        )
    else:
        suggested_action = "Return exactly one corrected JSON object wrapped in \\boxed{...}."
    return ReflectionContractFailure(
        surface="final_output",
        error_code=error_code,
        message=message,
        recoverable=True,
        suggested_action=suggested_action,
        details={"raw_error": message},
        repair_plan=repair_plan,
    )


def _reflection_learning_contract_failure(
    exc: ReflectionLearningContractError,
) -> ReflectionContractFailure:
    message = str(exc).strip()
    suggested_action = (
        "Return learning_decision with a non-empty actions array. "
        "Use no_learning_write only with skip_review. For write_review, emit "
        "emit_episode_memory_candidate with candidate_kind='delta' or 'no_update'. "
        "Each action must include source_episode_id, source_review_path, effective_from, "
        "error_attributions, rationale, and payload."
    )
    specific_repair_guidance = _reflection_contract_repair_suggestion(message)
    if specific_repair_guidance is not None:
        suggested_action = f"{suggested_action} {specific_repair_guidance}"
    return ReflectionContractFailure(
        surface="final_output",
        error_code="learning_decision_contract_violation",
        message=message,
        recoverable=True,
        suggested_action=suggested_action,
        details={"raw_error": message},
        repair_plan=_FULL_RETRY_PLAN,
    )


def _reflection_evaluation_contract_failure(
    exc: ReflectionContractError,
) -> ReflectionContractFailure:
    message = str(exc).strip()
    suggested_action = (
        "Return corrected evaluation fields that satisfy the typed reflection "
        "contract. exposure_assessment must be an object with summary_md only; "
        "do not return trade-action fields."
    )
    specific_repair_guidance = _reflection_contract_repair_suggestion(message)
    if specific_repair_guidance is not None:
        suggested_action = f"{suggested_action} {specific_repair_guidance}"
    return ReflectionContractFailure(
        surface="final_output",
        error_code="reflection_evaluation_contract_violation",
        message=message,
        recoverable=True,
        suggested_action=suggested_action,
        details={"raw_error": message},
        repair_plan=_FULL_RETRY_PLAN,
    )


def _reflection_contract_repair_suggestion(message: str) -> str | None:
    if "trade_assessment.return_pct must equal" in message:
        return (
            "Set trade_assessment.return_pct to the expected percentage reported in "
            "the contract error. The source strategy_return is a decimal return and "
            "must be multiplied by 100 exactly once."
        )
    if "assessed_horizons[" in message and ".return_pct must equal" in message:
        return (
            "Set only the rejected assessed_horizons return_pct to the expected "
            "percentage reported in the contract error. The matching horizon baseline "
            "strategy_return is a decimal return and must be multiplied by 100 exactly "
            "once."
        )
    if "assessed_horizons[" in message and "pending_market_data" in message:
        return (
            "Remove the assessed_horizons item whose deterministic horizon baseline is "
            "still pending_market_data; do not invent a return for an unfinished horizon."
        )
    if "open-position learning_decision.actions" in message and (
        "validation_basis must not be 'close_review'" in message
    ):
        return (
            "Do not use validation_basis='close_review' for an open position. Choose a "
            "basis supported by the visible facts, or emit a justified no_update "
            "candidate when no durable delta is warranted."
        )
    if "trade_assessment.verdict" in message:
        return (
            "trade_assessment.verdict must be one of "
            f"{_format_quoted_enum(_TRADE_VERDICTS)}. For close review, use only "
            f"{_format_quoted_enum(_CLOSED_TRADE_VERDICTS)}; for open-position "
            'review, use only "open".'
        )
    if "market_context_error_labels" in message:
        return (
            '"market_context_error_labels" must be [] or an array of distinct '
            "strings chosen only from "
            f"{_format_quoted_enum(MARKET_CONTEXT_USAGE_QUALITY_LABELS)}. Do not use "
            'learning-decision error_attributions such as "interpretation_error".'
        )
    if "memory_status" in message:
        return f'"memory_status" must be one of {_format_quoted_enum(_MEMORY_STATUSES)}.'
    if "validation_basis" in message:
        return f'"validation_basis" must be one of {_format_quoted_enum(_VALIDATION_BASES)}.'
    if "source_visible_through" in message or "usable_from" in message:
        return (
            '"source_visible_through" and "usable_from" must be ISO datetimes '
            '(for example "2026-03-12T02:30:00+00:00").'
        )
    if "source_refs" in message:
        return (
            '"source_refs" must be a JSON object, not an array. Allowed keys are '
            f"{_format_quoted_enum(_SOURCE_REF_KEYS)}; each present key must map to "
            "an array of non-blank strings. Omit unused keys or set them to []."
        )
    if "confidence" in message:
        return '"confidence" must be a number between 0 and 1, not a label like "medium".'
    return None


def _format_quoted_enum(values: tuple[str, ...]) -> str:
    if len(values) == 1:
        return f'"{values[0]}"'
    head = ", ".join(f'"{value}"' for value in values[:-1])
    return f'{head}, or "{values[-1]}"'


def reflection_read_audit_failure(
    attempt: AgentRunReceipt,
) -> ReflectionContractFailure | None:
    successful_tool_names = set(
        _diagnostic_string_tuple(attempt.diagnostics, "successful_tool_names")
    )
    if successful_tool_names & _REFLECTION_READ_TOOL_NAMES:
        return None
    attempted_tool_names = _diagnostic_string_tuple(
        attempt.diagnostics,
        "attempted_tool_names",
    )
    failure_details = _diagnostic_string_tuple(
        attempt.diagnostics,
        "tool_failure_details",
    )
    return ReflectionContractFailure(
        surface="read_audit",
        error_code="reflection_missing_read_tool",
        message=(
            "Every reflection decision requires at least one successful reflection "
            "read tool call before the final boxed JSON."
        ),
        recoverable=True,
        suggested_action=(
            "Before final boxed JSON, call at least one successful reflection read tool."
        ),
        details={
            "required_tools": tuple(sorted(_REFLECTION_READ_TOOL_NAMES)),
            "attempted_tools": attempted_tool_names,
            "failure_details": failure_details,
        },
        repair_plan=_FULL_RETRY_PLAN,
    )


def _diagnostic_string_tuple(
    diagnostics: Mapping[str, object],
    field_name: str,
) -> tuple[str, ...]:
    raw_value = diagnostics.get(field_name, ())
    if not isinstance(raw_value, (list, tuple)):
        return ()
    return tuple(value for value in raw_value if isinstance(value, str) and value)


__all__ = [
    "REFLECTION_EVALUATION_MAX_ATTEMPTS",
    "ReflectionContractFailure",
    "ReflectionContractRepairExhausted",
    "append_reflection_contract_repair_feedback",
    "classify_reflection_contract_failure",
    "format_reflection_contract_repair_failure",
    "is_unfinished_terminal_final_output_failure",
    "reflection_read_audit_failure",
    "run_reflection_agent_with_contract_repair",
]
