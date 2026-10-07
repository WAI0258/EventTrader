"""Provider-neutral execution and grounding contract for search agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

from event_trader.integrations.boxed_json import (
    BoxedJsonPayloadError,
    load_first_boxed_json_object,
)

SEARCH_AGENT_ROLE = "search"
SEARCH_TOOL_SERVER_NAME = "event_trader_search"

LIVE_SEARCH_APPEND_TOOL_NAME = "append_live_web_search_candidate"
HISTORICAL_SEARCH_APPEND_TOOL_NAME = "append_historical_web_search_candidate"
LIVE_SEARCH_TOOL_SERVER_NAME = "event_trader_live_web_search"
HISTORICAL_SEARCH_TOOL_SERVER_NAME = "event_trader_historical_web_search"

LIVE_SEARCH_MAX_ATTEMPTS = 2
HISTORICAL_SEARCH_MAX_ATTEMPTS = 2
SEARCH_ACQUISITION_FAILURE_BUDGET = 10

_TRACKING_QUERY_PARAM_NAMES = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
    }
)
_INSPECTED_ONLY_NOISE_QUERY_PARAM_NAMES = frozenset(
    {
        "domshim",
        "noservercache",
        "noservertelemetry",
        "batchservertelemetry",
        "renderwebcomponents",
        "wcseo",
    }
)
_INSPECTED_ONLY_APIVERSION_QUERY_PARAM_NAME = "apiversion"

type SearchAttemptRunner[T] = Callable[[str, str], Awaitable[T]]


class SearchAcquisitionTrace(Protocol):
    """Project fields required to audit one search acquisition attempt."""

    @property
    def search_success_count(self) -> int: ...

    @property
    def search_failure_count(self) -> int: ...

    @property
    def scrape_success_count(self) -> int: ...

    @property
    def scrape_failure_count(self) -> int: ...

    @property
    def source_refs(self) -> tuple[str, ...]: ...

    @property
    def inspected_source_refs(self) -> tuple[str, ...]: ...


class SearchAttemptRepairRequired(RuntimeError):
    """Signals a deterministic, recoverable search-attempt contract failure."""

    def __init__(self, *, message: str, feedback: str) -> None:
        self.feedback = feedback
        super().__init__(message)


class SearchAgentContractError(RuntimeError):
    """Raised when a search agent violates a deterministic output contract."""


def normalize_search_collection_final_status(payload_text: str) -> str:
    """Require the terminal boxed JSON status without trusting provider formatting."""

    try:
        payload = load_first_boxed_json_object(
            final_boxed_answer=payload_text,
            final_summary="",
            object_start_fields=("status",),
            missing_error=(
                "MiroThinker historical search agent did not return a boxed JSON status payload."
            ),
            non_json_error=(
                "MiroThinker historical search agent returned non-JSON boxed output."
            ),
            non_object_error=(
                "MiroThinker historical search agent must return one JSON object."
            ),
        )
    except BoxedJsonPayloadError as exc:
        raise SearchAgentContractError(str(exc)) from exc
    if set(payload) != {"status"}:
        unexpected_fields = sorted(set(payload) - {"status"})
        missing_fields = sorted({"status"} - set(payload))
        problems: list[str] = []
        if missing_fields:
            problems.append(f"missing field(s): {', '.join(missing_fields)}")
        if unexpected_fields:
            problems.append(f"unexpected field(s): {', '.join(unexpected_fields)}")
        raise SearchAgentContractError(
            "MiroThinker historical search agent returned the wrong final payload "
            f"shape; {'; '.join(problems)}."
        )
    status = payload["status"]
    if status not in {"completed", "no_source_found"}:
        raise SearchAgentContractError(
            "MiroThinker historical search agent final status must be completed or no_source_found."
        )
    return status


def is_retriable_search_collection_final_status_error(error_text: str) -> bool:
    return error_text.startswith(
        "MiroThinker historical search agent did not return a boxed JSON status payload."
    ) or error_text.startswith(
        "MiroThinker historical search agent returned non-JSON boxed output."
    ) or error_text.startswith(
        "MiroThinker historical search agent must return one JSON object."
    ) or error_text.startswith(
        "MiroThinker historical search agent returned the wrong final payload shape;"
    ) or error_text.startswith(
        "MiroThinker historical search agent final status must be completed or no_source_found."
    )


async def execute_search_with_repair[T](
    *,
    base_task_id: str,
    base_task_description: str,
    max_attempts: int,
    run_attempt: SearchAttemptRunner[T],
) -> T:
    """Run the project-owned bounded repair loop around one replaceable attempt."""

    if max_attempts < 1:
        raise ValueError("search max_attempts must be at least one.")
    task_description = base_task_description
    for attempt_index in range(max_attempts):
        task_id = (
            base_task_id
            if attempt_index == 0
            else f"{base_task_id}-retry{attempt_index}"
        )
        try:
            return await run_attempt(task_id, task_description)
        except SearchAttemptRepairRequired as exc:
            if attempt_index + 1 >= max_attempts:
                raise
            task_description = build_search_repair_prompt(
                base_task_description=base_task_description,
                feedback=exc.feedback,
            )
    raise AssertionError("bounded search repair loop exited without a result.")


def build_search_repair_prompt(
    *,
    base_task_description: str,
    feedback: str,
) -> str:
    """Append deterministic audit feedback without changing the original task."""

    return base_task_description + "\n\n" + feedback


def find_ungrounded_source_refs(
    *,
    accepted_source_refs: tuple[str, ...],
    inspected_source_refs: tuple[str, ...],
) -> tuple[str, ...]:
    """Return accepted sources not proven by successful inspected-source output."""

    return tuple(
        source_ref
        for source_ref in accepted_source_refs
        if not any(
            _source_ref_grounding_match(
                accepted_source_ref=source_ref,
                inspected_source_ref=inspected_source_ref,
            )
            for inspected_source_ref in inspected_source_refs
        )
    )


def build_source_grounding_repair_feedback(
    *,
    ungrounded_source_refs: tuple[str, ...],
    inspected_source_refs: tuple[str, ...],
) -> str:
    """Build the stable repair instruction for an ungrounded admission attempt."""

    return (
        "Previous search attempt failed deterministic source audit.\n"
        "The agent submitted accepted source_ref value(s) that were not proven by "
        "successful inspected source output.\n\n"
        "Submitted but unproven source_ref value(s):\n"
        f"{_format_value_list(ungrounded_source_refs)}\n\n"
        "Successful inspected source output proved these source URL(s):\n"
        f"{_format_value_list(inspected_source_refs)}\n\n"
        "For this new attempt, do not submit an unproven source_ref again unless "
        "you first inspect that source_ref with an acquisition tool. If a source "
        "cannot be inspected, submit an actually inspected source, find another "
        "inspectable source, or return no_source_found. Do not treat this audit "
        "feedback as source material."
    )


def search_acquisition_success_count(trace: SearchAcquisitionTrace) -> int:
    return trace.search_success_count + trace.scrape_success_count


def search_acquisition_failure_count(trace: SearchAcquisitionTrace) -> int:
    return trace.search_failure_count + trace.scrape_failure_count


def search_trace_has_source_ref_coverage(trace: SearchAcquisitionTrace) -> bool:
    if trace.inspected_source_refs:
        return True
    return not trace.source_refs


def is_verified_no_source_found(trace: SearchAcquisitionTrace) -> bool:
    """Require a successful, source-covered and failure-free acquisition trace."""

    return (
        search_acquisition_success_count(trace) > 0
        and search_trace_has_source_ref_coverage(trace)
        and search_acquisition_failure_count(trace) <= 0
    )


def build_append_rejected_repair_feedback(*, first_rejection_reason: str) -> str:
    return (
        "Previous search attempt failed deterministic append validation.\n"
        "The append tool rejected every submitted candidate.\n\n"
        "First rejection reason:\n"
        f"- {first_rejection_reason}\n\n"
        "For this new attempt, use the original task and submit only candidates "
        "that satisfy the append contract. If no real inspected source exists, "
        "return no_source_found. Do not treat this validation feedback as source "
        "material."
    )


def build_unverified_no_source_found_repair_feedback(
    *,
    trace: SearchAcquisitionTrace,
) -> str:
    return (
        "Previous search attempt returned no_source_found, but deterministic "
        "audit could not verify that result.\n\n"
        "Audit facts:\n"
        f"- acquisition successes: {search_acquisition_success_count(trace)}\n"
        f"- acquisition failures: {search_acquisition_failure_count(trace)}\n"
        "- inspected source URL(s):\n"
        f"{_format_value_list(trace.inspected_source_refs)}\n"
        "- source URL(s) surfaced by acquisition tools:\n"
        f"{_format_value_list(trace.source_refs)}\n\n"
        "For this new attempt, inspect source material before returning "
        "no_source_found. If source candidates cannot be inspected, find another "
        "inspectable source or return no_source_found only after the acquisition "
        "trace proves the result. Do not treat this audit feedback as source "
        "material."
    )


def build_historical_final_status_repair_feedback(*, error_text: str) -> str:
    return (
        "Previous historical search attempt failed the final output contract.\n"
        "The agent did not finish with the required boxed JSON status payload.\n\n"
        "Deterministic audit failure:\n"
        f"- {error_text}\n\n"
        "For this new attempt, keep using the append tool exactly as instructed, "
        "then end with exactly one tiny JSON object wrapped in \\boxed{...} and no "
        "extra text before or after it.\n"
        '- If one or more acceptable sources were appended, end with `{"status":"completed"}`.\n'
        "- If no acceptable inspected source is available after validation, end "
        'with `{"status":"no_source_found"}`.\n'
        "Do not treat this validation feedback as source material."
    )


def _source_ref_grounding_match(
    *,
    accepted_source_ref: str,
    inspected_source_ref: str,
) -> bool:
    accepted = _parse_source_ref_for_grounding(accepted_source_ref)
    inspected = _parse_source_ref_for_grounding(inspected_source_ref)
    if accepted[:4] != inspected[:4]:
        return False
    return _grounding_query_match(
        accepted_query_items=accepted[4],
        inspected_query_items=inspected[4],
    )


def _parse_source_ref_for_grounding(
    source_ref: str,
) -> tuple[str, str, str, str, tuple[tuple[str, str], ...]]:
    parsed = urlsplit(source_ref.strip())
    query_items = tuple(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_query_param(key)
    )
    return (
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path or "/",
        parsed.fragment,
        query_items,
    )


def _grounding_query_match(
    *,
    accepted_query_items: tuple[tuple[str, str], ...],
    inspected_query_items: tuple[tuple[str, str], ...],
) -> bool:
    accepted_query_param_names = {
        _normalize_query_param_name(key) for key, _ in accepted_query_items
    }
    inspected_has_strong_transport_marker = any(
        _is_inspected_only_noise_query_param(key)
        for key, _ in inspected_query_items
    )
    accepted_index = 0
    accepted_count = len(accepted_query_items)
    for inspected_item in inspected_query_items:
        if (
            accepted_index < accepted_count
            and inspected_item == accepted_query_items[accepted_index]
        ):
            accepted_index += 1
            continue
        if (
            _is_inspected_only_noise_query_param(inspected_item[0])
            and _normalize_query_param_name(inspected_item[0])
            not in accepted_query_param_names
        ):
            continue
        if (
            _is_inspected_only_apiversion_query_param(inspected_item[0])
            and inspected_has_strong_transport_marker
            and _normalize_query_param_name(inspected_item[0])
            not in accepted_query_param_names
        ):
            continue
        return False
    return accepted_index == accepted_count


def _normalize_query_param_name(name: str) -> str:
    return name.strip().lower()


def _is_tracking_query_param(name: str) -> bool:
    normalized = _normalize_query_param_name(name)
    return normalized.startswith("utm_") or normalized in _TRACKING_QUERY_PARAM_NAMES


def _is_inspected_only_noise_query_param(name: str) -> bool:
    return _normalize_query_param_name(name) in _INSPECTED_ONLY_NOISE_QUERY_PARAM_NAMES


def _is_inspected_only_apiversion_query_param(name: str) -> bool:
    return (
        _normalize_query_param_name(name)
        == _INSPECTED_ONLY_APIVERSION_QUERY_PARAM_NAME
    )


def _format_value_list(values: tuple[str, ...]) -> str:
    if not values:
        return "- (none)"
    return "\n".join(f"- {value}" for value in values)


__all__ = [
    "HISTORICAL_SEARCH_APPEND_TOOL_NAME",
    "HISTORICAL_SEARCH_MAX_ATTEMPTS",
    "HISTORICAL_SEARCH_TOOL_SERVER_NAME",
    "LIVE_SEARCH_APPEND_TOOL_NAME",
    "LIVE_SEARCH_MAX_ATTEMPTS",
    "LIVE_SEARCH_TOOL_SERVER_NAME",
    "SEARCH_ACQUISITION_FAILURE_BUDGET",
    "SEARCH_AGENT_ROLE",
    "SEARCH_TOOL_SERVER_NAME",
    "SearchAcquisitionTrace",
    "SearchAgentContractError",
    "SearchAttemptRepairRequired",
    "build_append_rejected_repair_feedback",
    "build_historical_final_status_repair_feedback",
    "build_search_repair_prompt",
    "build_source_grounding_repair_feedback",
    "build_unverified_no_source_found_repair_feedback",
    "execute_search_with_repair",
    "find_ungrounded_source_refs",
    "is_verified_no_source_found",
    "is_retriable_search_collection_final_status_error",
    "normalize_search_collection_final_status",
    "search_acquisition_failure_count",
    "search_acquisition_success_count",
    "search_trace_has_source_ref_coverage",
]
