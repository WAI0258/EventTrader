"""Provider-neutral bounded repair for PMReview contract output."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from event_trader.integrations.boxed_json import (
    BoxedJsonPayloadError,
    load_first_boxed_json_object,
)
from event_trader.pm_review.contracts import (
    PMDecisionDraft,
    PMReviewContractError,
    PMReviewInput,
    PMReviewRequest,
    parse_pm_decision_draft,
)
from event_trader.pm_review.prompt import (
    PM_REVIEW_OUTPUT_SCHEMA_PROMPT,
    PMReviewOutputProtocol,
    render_pm_review_contract_repair_directive,
)
from event_trader.portfolio.contracts import PortfolioContractError
from event_trader.portfolio.pm_decision_adapter import (
    PMDecisionAdapterError,
    PMDecisionHoldReasonRequiredError,
    build_pm_decision_from_draft,
)
from event_trader.reasoning.runtime import AgentRunReceipt

PM_REVIEW_CONTRACT_REPAIR_MAX_REPAIRS = 3
_PM_REVIEW_CONTRACT_MAX_ATTEMPTS = PM_REVIEW_CONTRACT_REPAIR_MAX_REPAIRS + 1


class PMReviewContractOutputError(ValueError):
    """Raised when one PMReview attempt violates the project-owned output contract."""

    def __init__(
        self,
        message: str,
        *,
        repair_fields: tuple[str, ...] = (),
    ) -> None:
        self.repair_fields = repair_fields
        super().__init__(message)


class PMReviewContractRepairExhausted(ValueError):
    """Raised after the initial PMReview attempt and all repairs fail."""

    def __init__(self, *, failure: PMReviewContractOutputError, attempt_count: int) -> None:
        self.failure = failure
        self.attempt_count = attempt_count
        self.max_repairs = PM_REVIEW_CONTRACT_REPAIR_MAX_REPAIRS
        super().__init__(
            "PMReview contract repair exhausted after "
            f"{attempt_count} attempts: {failure}"
        )


@dataclass(frozen=True, slots=True)
class PMReviewContractRepairResult:
    decision_draft: PMDecisionDraft
    accepted_attempt: AgentRunReceipt
    attempt_count: int


def run_pm_review_contract_repair(
    *,
    task_description: str,
    request: PMReviewRequest,
    pm_review_input: PMReviewInput,
    decision_available_at: datetime,
    output_protocol: PMReviewOutputProtocol,
    run_attempt: Callable[[str, int], AgentRunReceipt],
) -> PMReviewContractRepairResult:
    """Run one initial PMReview attempt and at most three deterministic repairs."""

    original_task_description = task_description.rstrip()
    current_task_description = original_task_description
    pending_repair_payload: dict[str, object] | None = None
    pending_repair_fields: tuple[str, ...] = ()
    for attempt_count in range(1, _PM_REVIEW_CONTRACT_MAX_ATTEMPTS + 1):
        attempt = run_attempt(current_task_description, attempt_count)
        attempt_payload: dict[str, object] | None = None
        try:
            attempt_payload = _load_pm_decision_draft_payload(
                final_boxed_answer=attempt.contract_output_text,
                final_summary=attempt.final_summary,
                fallback_payload_texts=attempt.fallback_payload_texts,
            )
            if pending_repair_payload is not None and pending_repair_fields:
                attempt_payload = _merge_pm_review_repair_fields(
                    prior_payload=pending_repair_payload,
                    repair_candidate=attempt_payload,
                    field_names=pending_repair_fields,
                )
                attempt = _with_pm_review_repair_payload(
                    attempt,
                    payload=attempt_payload,
                    field_names=pending_repair_fields,
                )
            decision_draft = _parse_pm_decision_draft_payload(
                attempt_payload,
                pm_review_input=pm_review_input,
            )
            _validate_pm_decision_draft_materialization(
                request=request,
                pm_review_input=pm_review_input,
                decision_draft=decision_draft,
                decision_available_at=decision_available_at,
            )
        except PMReviewContractOutputError as caught:
            exc = caught
            if pending_repair_fields and not exc.repair_fields:
                exc = PMReviewContractOutputError(
                    str(exc),
                    repair_fields=pending_repair_fields,
                )
            if attempt_count >= _PM_REVIEW_CONTRACT_MAX_ATTEMPTS:
                raise PMReviewContractRepairExhausted(
                    failure=exc,
                    attempt_count=attempt_count,
                ) from exc
            if exc.repair_fields:
                if attempt_payload is not None:
                    pending_repair_payload = dict(attempt_payload)
                pending_repair_fields = exc.repair_fields
            else:
                pending_repair_payload = None
                pending_repair_fields = ()
            current_task_description = _append_pm_review_contract_repair_feedback(
                original_task_description,
                failure=exc,
                repair_count=attempt_count,
                output_protocol=output_protocol,
            )
            continue
        return PMReviewContractRepairResult(
            decision_draft=decision_draft,
            accepted_attempt=attempt,
            attempt_count=attempt_count,
        )
    raise AssertionError("PMReview contract repair loop ended without a result.")


def parse_pm_decision_draft_output(
    *,
    final_boxed_answer: str,
    final_summary: str,
    fallback_payload_texts: tuple[str, ...] = (),
    pm_review_input: PMReviewInput | None = None,
) -> PMDecisionDraft:
    """Parse boxed PMReview output against the project-owned thin PM contract."""

    payload = _load_pm_decision_draft_payload(
        final_boxed_answer=final_boxed_answer,
        final_summary=final_summary,
        fallback_payload_texts=fallback_payload_texts,
    )
    return _parse_pm_decision_draft_payload(
        payload,
        pm_review_input=pm_review_input,
    )


def _load_pm_decision_draft_payload(
    *,
    final_boxed_answer: str,
    final_summary: str,
    fallback_payload_texts: tuple[str, ...],
) -> dict[str, object]:
    try:
        return load_first_boxed_json_object(
            final_boxed_answer=final_boxed_answer,
            final_summary=final_summary,
            fallback_payload_texts=fallback_payload_texts,
            object_start_fields=("pm_review_request_id", "target_key", "business_at"),
            missing_error="PMReview agent did not return a boxed JSON payload.",
            non_json_error="PMReview agent returned non-JSON boxed output.",
            non_object_error="PMReview agent returned a boxed JSON value that was not an object.",
        )
    except BoxedJsonPayloadError as exc:
        raise PMReviewContractOutputError(str(exc)) from exc


def _parse_pm_decision_draft_payload(
    payload: Mapping[str, object],
    *,
    pm_review_input: PMReviewInput | None,
) -> PMDecisionDraft:
    try:
        draft = parse_pm_decision_draft(payload)
    except PMReviewContractError as exc:
        raise PMReviewContractOutputError(str(exc)) from exc
    if pm_review_input is None:
        return draft
    if draft.pm_review_request_id != pm_review_input.pm_review_request_id:
        raise PMReviewContractOutputError(
            "PMDecisionDraft.pm_review_request_id must match PMReviewInput."
        )
    if draft.target_key != pm_review_input.target_key:
        raise PMReviewContractOutputError("PMDecisionDraft.target_key must match PMReviewInput.")
    if draft.business_at != pm_review_input.business_at:
        raise PMReviewContractOutputError("PMDecisionDraft.business_at must match PMReviewInput.")
    return draft


def _validate_pm_decision_draft_materialization(
    *,
    request: PMReviewRequest,
    pm_review_input: PMReviewInput,
    decision_draft: PMDecisionDraft,
    decision_available_at: datetime,
) -> None:
    try:
        build_pm_decision_from_draft(
            request=request,
            pm_review_input=pm_review_input,
            decision_draft=decision_draft,
            decision_available_at=decision_available_at,
        )
    except PMDecisionHoldReasonRequiredError as exc:
        raise PMReviewContractOutputError(
            str(exc),
            repair_fields=("hold_reason_code",),
        ) from exc
    except PMDecisionAdapterError as exc:
        raise PMReviewContractOutputError(
            str(exc),
            repair_fields=exc.repair_fields,
        ) from exc
    except PortfolioContractError as exc:
        raise PMReviewContractOutputError(str(exc)) from exc


def _append_pm_review_contract_repair_feedback(
    task_description: str,
    *,
    failure: PMReviewContractOutputError,
    repair_count: int,
    output_protocol: PMReviewOutputProtocol,
) -> str:
    repair_boundary = ""
    if output_protocol == "boxed_json":
        output_contract_prompt = PM_REVIEW_OUTPUT_SCHEMA_PROMPT
        correction_instruction = (
            "Repeat the same PMReview task and return exactly one corrected JSON object "
            "wrapped in \\boxed{...}. Do not return a tuple, CSV, markdown list, prose, "
            "or any text outside the boxed JSON. Keep the same PMReviewInput identity "
            "fields and do not invent new evidence.\n\n"
        )
    elif output_protocol == "structured_tool":
        output_contract_prompt = ""
        correction_instruction = (
            "Repeat the same PMReview task and return the corrected PMDecisionDraft "
            "through the required structured tool. Keep the same PMReviewInput identity "
            "fields and do not invent new evidence.\n\n"
        )
    else:
        raise PMReviewContractOutputError(
            f"unsupported PMReview output protocol: {output_protocol!r}."
        )
    if failure.repair_fields:
        fields = ", ".join(f"`{field_name}`" for field_name in failure.repair_fields)
        field_requirement = ""
        if "hold_reason_code" in failure.repair_fields:
            field_requirement = (
                "- Set hold_reason_code to a non-null lower_snake_case string; "
                "returning null repeats the same contract violation.\n"
            )
        if "cited_market_bar_ids" in failure.repair_fields:
            field_requirement += (
                "- Set cited_market_bar_ids only to exact bar_id values displayed in "
                "PMReviewInput.visible_market_bars; do not construct ids from timestamps.\n"
            )
        payload_instruction = (
            "- Return exactly one JSON object containing only the listed field(s).\n"
            if output_protocol == "boxed_json"
            else "- Put only the listed field(s) in the required structured tool input.\n"
        )
        repair_boundary = (
            f"{render_pm_review_contract_repair_directive(repair_fields=failure.repair_fields)}\n\n"
            "Targeted business repair boundary:\n"
            f"- Correct only these field(s): {fields}.\n"
            f"{field_requirement}"
            f"{payload_instruction}"
            "- The project will merge only those fields into the prior accepted payload "
            "and will re-run the complete PMReview materialization contract.\n\n"
        )
        output_contract_prompt = ""
        if output_protocol == "boxed_json":
            correction_instruction = (
                "Return exactly one targeted repair JSON object wrapped in \\boxed{...}. "
                "Do not return a tuple, CSV, markdown list, prose, or any text outside "
                "the boxed JSON.\n\n"
            )
        else:
            correction_instruction = (
                "Return exactly the targeted repair through the required structured tool. "
                "Do not answer with a text block.\n\n"
            )
    return (
        f"{task_description.rstrip()}\n\n"
        "Previous PMReview attempt failed the deterministic output contract:\n"
        f"- attempt: {repair_count}/{_PM_REVIEW_CONTRACT_MAX_ATTEMPTS}\n"
        f"- error: {_shorten_error_text(str(failure), max_length=500)}\n\n"
        f"{correction_instruction}"
        f"{repair_boundary}"
        f"{output_contract_prompt}"
    )


def _merge_pm_review_repair_fields(
    *,
    prior_payload: Mapping[str, object],
    repair_candidate: Mapping[str, object],
    field_names: tuple[str, ...],
) -> dict[str, object]:
    merged = dict(prior_payload)
    for field_name in field_names:
        if field_name in repair_candidate:
            merged[field_name] = repair_candidate[field_name]
    return merged


def _with_pm_review_repair_payload(
    receipt: AgentRunReceipt,
    *,
    payload: Mapping[str, object],
    field_names: tuple[str, ...],
) -> AgentRunReceipt:
    payload_text = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return replace(
        receipt,
        final_summary="",
        contract_output_text=f"\\boxed{{{payload_text}}}",
        fallback_payload_texts=(),
        diagnostics={
            **receipt.diagnostics,
            "business_repair_merged": True,
            "business_repair_fields": field_names,
        },
    )


def _shorten_error_text(value: str, *, max_length: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3]}..."


__all__ = [
    "PM_REVIEW_CONTRACT_REPAIR_MAX_REPAIRS",
    "PMReviewContractOutputError",
    "PMReviewContractRepairExhausted",
    "PMReviewContractRepairResult",
    "parse_pm_decision_draft_output",
    "run_pm_review_contract_repair",
]
