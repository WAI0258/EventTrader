"""Minimal helpers for reading deterministic facts from MiroThinker task logs."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast


class MiroThinkerTaskLogError(ValueError):
    """Raised when a MiroThinker task log cannot be read deterministically."""


class _MiroThinkerFinalSummaryAfterToolCall(ValueError):
    """Raised when the vendor final-summary prompt appears where a tool result would be."""


@dataclass(frozen=True, slots=True)
class MiroThinkerCollectionToolTrace:
    """Search/scrape tool outcomes captured from one MiroThinker task log."""

    search_attempt_count: int
    search_success_count: int
    search_failure_count: int
    scrape_attempt_count: int
    scrape_success_count: int
    scrape_failure_count: int
    failure_details: tuple[str, ...]
    source_refs: tuple[str, ...] = ()
    inspected_source_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MiroThinkerToolUseTrace:
    """Tool call names and outcomes captured from one MiroThinker task log."""

    attempted_tool_names: tuple[str, ...]
    successful_tool_names: tuple[str, ...]
    failure_details: tuple[str, ...] = ()


def mirothinker_portable_run_id(logical_task_id: str) -> str:
    """Derive the vendor run id used where MiroThinker persists task logs."""
    if not isinstance(logical_task_id, str):
        raise MiroThinkerTaskLogError("MiroThinker logical_task_id must be a string.")
    normalized = logical_task_id.strip()
    if not normalized:
        raise MiroThinkerTaskLogError("MiroThinker logical_task_id must not be blank.")
    run_id = re.sub(r"[^A-Za-z0-9._-]+", "_", normalized).strip("._")
    run_id = re.sub(r"_+", "_", run_id)
    if not run_id:
        raise MiroThinkerTaskLogError(
            "MiroThinker logical_task_id must contain at least one portable run-id character."
        )
    return run_id


def load_latest_task_log_assistant_texts(
    *,
    task_id: str,
    log_dir: Path,
    not_before_mtime_ns: int | None = None,
) -> tuple[str, ...]:
    """Load assistant message text candidates from the latest task log for one task."""
    log_path = _select_latest_task_log_file(
        task_id=task_id,
        log_dir=log_dir,
        not_before_mtime_ns=not_before_mtime_ns,
    )
    if log_path is None:
        return ()
    payload = _load_task_log_payload(log_path)
    return extract_assistant_text_candidates(payload)


def load_task_log_assistant_texts(log_path: Path) -> tuple[str, ...]:
    """Load assistant message text candidates from one exact task log path."""
    payload = _load_task_log_payload(log_path)
    return extract_assistant_text_candidates(payload)


def load_latest_task_log_collection_tool_trace(
    *,
    task_id: str,
    log_dir: Path,
    not_before_mtime_ns: int | None = None,
) -> MiroThinkerCollectionToolTrace:
    """Load search/scrape tool outcomes from the latest task log for one task."""
    log_path = _select_latest_task_log_file(
        task_id=task_id,
        log_dir=log_dir,
        not_before_mtime_ns=not_before_mtime_ns,
    )
    if log_path is None:
        raise MiroThinkerTaskLogError(
            f"Missing MiroThinker task log for tool trace: task_id={task_id}"
        )
    payload = _load_task_log_payload(log_path)
    return extract_collection_tool_trace(payload)


def load_latest_task_log_tool_use_trace(
    *,
    task_id: str,
    log_dir: Path,
    not_before_mtime_ns: int | None = None,
) -> MiroThinkerToolUseTrace:
    """Load generic tool-call outcomes from the latest task log for one task."""
    log_path = _select_latest_task_log_file(
        task_id=task_id,
        log_dir=log_dir,
        not_before_mtime_ns=not_before_mtime_ns,
    )
    if log_path is None:
        raise MiroThinkerTaskLogError(
            f"Missing MiroThinker task log for tool-use trace: task_id={task_id}"
        )
    payload = _load_task_log_payload(log_path)
    return extract_tool_use_trace(payload)


def load_task_log_tool_use_trace(log_path: Path) -> MiroThinkerToolUseTrace:
    """Load generic tool-call outcomes from one exact task log path."""
    payload = _load_task_log_payload(log_path)
    return extract_tool_use_trace(payload)


def extract_assistant_text_candidates(task_log_payload: object) -> tuple[str, ...]:
    """Extract assistant-authored text candidates from one decoded task log payload."""
    if not isinstance(task_log_payload, dict):
        raise MiroThinkerTaskLogError("MiroThinker task log payload must be a JSON object.")
    history = task_log_payload.get("main_agent_message_history")
    if not isinstance(history, dict):
        return ()
    messages = history.get("message_history")
    if not isinstance(messages, list):
        return ()

    candidates: list[str] = []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            if content.strip():
                candidates.append(content)
            continue
        if not isinstance(content, list):
            continue
        for item in reversed(content):
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                candidates.append(text)
    return tuple(candidates)


def extract_tool_use_trace(task_log_payload: object) -> MiroThinkerToolUseTrace:
    """Extract generic tool call success facts from one decoded task log payload."""
    messages = _load_optional_message_history(task_log_payload)
    attempted_tool_names: list[str] = []
    successful_tool_names: list[str] = []
    failure_details: list[str] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = _message_text(message.get("content"))
        tool_name = _extract_tool_name(text)
        if tool_name is None:
            continue
        attempted_tool_names.append(tool_name)
        try:
            result_text = _tool_result_after(
                messages,
                tool_index=index,
                tool_name=tool_name,
            )
        except _MiroThinkerFinalSummaryAfterToolCall:
            continue
        except MiroThinkerTaskLogError as exc:
            failure_details.append(f"{tool_name}: {_normalize_failure_text(str(exc))}")
            continue
        try:
            success, failure_detail, _ = _parse_tool_result_success(
                result_text,
                tool_name=tool_name,
            )
        except MiroThinkerTaskLogError as exc:
            success = False
            failure_detail = f"{tool_name}: {_normalize_failure_text(str(exc))}"
        if success:
            successful_tool_names.append(tool_name)
        elif failure_detail is not None:
            failure_details.append(failure_detail)
    _collect_step_log_tool_use_trace(
        task_log_payload,
        attempted_tool_names=attempted_tool_names,
        successful_tool_names=successful_tool_names,
    )
    return MiroThinkerToolUseTrace(
        attempted_tool_names=tuple(attempted_tool_names),
        successful_tool_names=_unique_texts(successful_tool_names),
        failure_details=tuple(failure_details[:5]),
    )


def extract_collection_tool_trace(
    task_log_payload: object,
) -> MiroThinkerCollectionToolTrace:
    """Extract search/scrape tool success facts from one decoded task log payload."""
    messages = _load_message_history(task_log_payload)
    search_attempt_count = 0
    search_success_count = 0
    search_failure_count = 0
    scrape_attempt_count = 0
    scrape_success_count = 0
    scrape_failure_count = 0
    failure_details: list[str] = []
    source_refs: list[str] = []
    inspected_source_refs: list[str] = []

    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = _message_text(message.get("content"))
        tool_name = _extract_tool_name(text)
        if tool_name is None:
            continue
        if tool_name in {
            "append_historical_web_search_candidate",
            "append_live_web_search_candidate",
        }:
            continue
        success: bool
        failure_detail: str | None
        successful_payloads: tuple[dict[str, object], ...]
        try:
            result_text = _tool_result_after(
                messages,
                tool_index=index,
                tool_name=tool_name,
            )
        except _MiroThinkerFinalSummaryAfterToolCall:
            continue
        except MiroThinkerTaskLogError as exc:
            if index != len(messages) - 1:
                raise
            success = False
            failure_detail = f"{tool_name}: {_normalize_failure_text(str(exc))}"
            successful_payloads = ()
        else:
            success, failure_detail, successful_payloads = _parse_tool_result_success(
                result_text,
                tool_name=tool_name,
            )
        if successful_payloads:
            tool_source_refs = _extract_source_refs_from_successful_tool_payloads(
                successful_payloads
            )
            source_refs.extend(tool_source_refs)
            if _tool_result_contains_inspected_source_material(tool_name):
                inspected_source_refs.extend(
                    _extract_inspected_source_refs_from_successful_tool_payloads(
                        successful_payloads
                    )
                )
        if tool_name != "scrape_and_extract_info":
            search_attempt_count += 1
            if success:
                search_success_count += 1
            else:
                search_failure_count += 1
        else:
            scrape_attempt_count += 1
            if success:
                scrape_success_count += 1
            else:
                scrape_failure_count += 1
        if failure_detail is not None:
            failure_details.append(failure_detail)

    return MiroThinkerCollectionToolTrace(
        search_attempt_count=search_attempt_count,
        search_success_count=search_success_count,
        search_failure_count=search_failure_count,
        scrape_attempt_count=scrape_attempt_count,
        scrape_success_count=scrape_success_count,
        scrape_failure_count=scrape_failure_count,
        failure_details=tuple(failure_details[:5]),
        source_refs=_unique_texts(source_refs),
        inspected_source_refs=_unique_texts(inspected_source_refs),
    )


def extract_collection_tool_trace_from_tool_result(
    *,
    tool_name: str,
    result_text: str,
) -> MiroThinkerCollectionToolTrace:
    """Extract one search/scrape tool outcome from a raw MiroThinker tool result."""
    try:
        success, failure_detail, successful_payloads = _parse_tool_result_success(
            result_text,
            tool_name=tool_name,
        )
    except MiroThinkerTaskLogError as exc:
        success = False
        failure_detail = f"{tool_name}: {_normalize_failure_text(str(exc))}"
        successful_payloads = ()

    source_refs = _extract_source_refs_from_successful_tool_payloads(successful_payloads)
    inspected_source_refs: tuple[str, ...] = ()
    if _tool_result_contains_inspected_source_material(tool_name):
        inspected_source_refs = _extract_inspected_source_refs_from_successful_tool_payloads(
            successful_payloads
        )

    if tool_name != "scrape_and_extract_info":
        return MiroThinkerCollectionToolTrace(
            search_attempt_count=1,
            search_success_count=1 if success else 0,
            search_failure_count=0 if success else 1,
            scrape_attempt_count=0,
            scrape_success_count=0,
            scrape_failure_count=0,
            failure_details=() if failure_detail is None else (failure_detail,),
            source_refs=source_refs,
            inspected_source_refs=inspected_source_refs,
        )
    return MiroThinkerCollectionToolTrace(
        search_attempt_count=0,
        search_success_count=0,
        search_failure_count=0,
        scrape_attempt_count=1,
        scrape_success_count=1 if success else 0,
        scrape_failure_count=0 if success else 1,
        failure_details=() if failure_detail is None else (failure_detail,),
        source_refs=source_refs,
        inspected_source_refs=inspected_source_refs,
    )


def _select_latest_task_log_file(
    *,
    task_id: str,
    log_dir: Path,
    not_before_mtime_ns: int | None,
) -> Path | None:
    if not log_dir.exists() or not log_dir.is_dir():
        return None
    run_id = mirothinker_portable_run_id(task_id)
    matching_files: list[tuple[int, Path]] = []
    for path in log_dir.glob(f"task_{run_id}_*.json"):
        mtime_ns = path.stat().st_mtime_ns
        if not_before_mtime_ns is not None and mtime_ns < not_before_mtime_ns:
            continue
        matching_files.append((mtime_ns, path))
    matching_files = sorted(matching_files, reverse=True)
    if not matching_files:
        return None
    return matching_files[0][1]


def _load_task_log_payload(log_path: Path) -> object:
    try:
        return json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MiroThinkerTaskLogError(
            f"Unable to read MiroThinker task log payload: {log_path}"
        ) from exc


def _load_message_history(task_log_payload: object) -> list[object]:
    if not isinstance(task_log_payload, dict):
        raise MiroThinkerTaskLogError("MiroThinker task log payload must be a JSON object.")
    history = task_log_payload.get("main_agent_message_history")
    if not isinstance(history, dict):
        raise MiroThinkerTaskLogError(
            "MiroThinker task log payload is missing main_agent_message_history."
        )
    messages = history.get("message_history")
    if not isinstance(messages, list):
        raise MiroThinkerTaskLogError("MiroThinker task log payload is missing message_history.")
    return messages


def _load_optional_message_history(task_log_payload: object) -> list[object]:
    if not isinstance(task_log_payload, dict):
        raise MiroThinkerTaskLogError("MiroThinker task log payload must be a JSON object.")
    history = task_log_payload.get("main_agent_message_history")
    if not isinstance(history, dict):
        return []
    messages = history.get("message_history")
    if not isinstance(messages, list):
        return []
    return messages


def _collect_step_log_tool_use_trace(
    task_log_payload: object,
    *,
    attempted_tool_names: list[str],
    successful_tool_names: list[str],
) -> None:
    if not isinstance(task_log_payload, dict):
        raise MiroThinkerTaskLogError("MiroThinker task log payload must be a JSON object.")
    step_logs = task_log_payload.get("step_logs")
    if not isinstance(step_logs, list):
        return
    for step in step_logs:
        if not isinstance(step, dict):
            continue
        message = step.get("message")
        if not isinstance(message, str):
            continue
        attempted = _extract_step_log_attempted_tool_name(message)
        if attempted is not None:
            attempted_tool_names.append(attempted)
        successful = _extract_step_log_successful_tool_name(message)
        if successful is not None:
            successful_tool_names.append(successful)


def _extract_step_log_attempted_tool_name(message: str) -> str | None:
    match = re.search(r"call tool '([^']+)'", message)
    if match is None:
        return None
    tool_name = match.group(1).strip()
    return tool_name or None


def _extract_step_log_successful_tool_name(message: str) -> str | None:
    match = re.search(r"Tool '([^']+)' .* called successfully\.", message)
    if match is None:
        match = re.search(r"Tool ([A-Za-z0-9_]+) completed in \d+ms", message)
    if match is None:
        return None
    tool_name = match.group(1).strip()
    return tool_name or None


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _extract_tool_name(text: str) -> str | None:
    match = re.search(r"<tool_name>(.*?)</tool_name>", text, flags=re.DOTALL)
    if match is None:
        return None
    tool_name = match.group(1).strip()
    if not tool_name:
        raise MiroThinkerTaskLogError("MiroThinker tool call has a blank tool_name.")
    return tool_name


def _tool_result_after(
    messages: list[object],
    *,
    tool_index: int,
    tool_name: str,
) -> str:
    result_index = tool_index + 1
    if result_index >= len(messages):
        raise MiroThinkerTaskLogError(
            f"MiroThinker tool call has no following result: tool_name={tool_name}"
        )
    result_message = messages[result_index]
    if not isinstance(result_message, dict) or result_message.get("role") != "user":
        raise MiroThinkerTaskLogError(
            f"MiroThinker tool call result is not a user message: tool_name={tool_name}"
        )
    result_text = _message_text(result_message.get("content")).strip()
    if not result_text:
        raise MiroThinkerTaskLogError(
            f"MiroThinker tool call result is blank: tool_name={tool_name}"
        )
    if _is_mirothinker_final_summary_prompt(result_text):
        raise _MiroThinkerFinalSummaryAfterToolCall
    return result_text


def _is_mirothinker_final_summary_prompt(text: str) -> bool:
    return text.startswith("Summarize the above conversation, and output the FINAL ANSWER")


def _parse_tool_result_success(
    result_text: str,
    *,
    tool_name: str,
) -> tuple[bool, str | None, tuple[dict[str, object], ...]]:
    tool_error_prefix = f"Error executing tool {tool_name}:"
    if result_text.startswith(tool_error_prefix):
        return False, f"{tool_name}: {_normalize_failure_text(result_text)}", ()
    plain_text_failure = _known_plain_text_tool_failure(result_text)
    if plain_text_failure is not None:
        return False, f"{tool_name}: {plain_text_failure}", ()
    payloads = _decode_tool_result_payloads(result_text, tool_name=tool_name)
    successful_payloads: list[dict[str, object]] = []
    for payload in payloads:
        success, failure_detail = _parse_tool_result_payload_success(
            payload,
            tool_name=tool_name,
        )
        if not success:
            return False, failure_detail, tuple(successful_payloads)
        successful_payloads.append(payload)
    return True, None, tuple(successful_payloads)


def _decode_tool_result_payloads(
    result_text: str,
    *,
    tool_name: str,
) -> tuple[dict[str, object], ...]:
    decoder = json.JSONDecoder()
    payloads: list[dict[str, object]] = []
    index = 0
    while index < len(result_text):
        while index < len(result_text) and result_text[index].isspace():
            index += 1
        if index >= len(result_text):
            break
        try:
            payload, end = decoder.raw_decode(result_text, index)
        except json.JSONDecodeError as exc:
            raise MiroThinkerTaskLogError(
                f"MiroThinker tool result is not JSON: tool_name={tool_name}"
            ) from exc
        if not isinstance(payload, dict):
            raise MiroThinkerTaskLogError(
                f"MiroThinker tool result must be a JSON object: tool_name={tool_name}"
            )
        payloads.append(cast(dict[str, object], payload))
        index = end
    if not payloads:
        raise MiroThinkerTaskLogError(
            f"MiroThinker tool result is not JSON: tool_name={tool_name}"
        )
    return tuple(payloads)


def _known_plain_text_tool_failure(result_text: str) -> str | None:
    normalized = _normalize_failure_text(result_text)
    if re.match(r"^Tool call to .+ failed\. Error:", normalized):
        return normalized
    return None


def _parse_tool_result_payload_success(
    payload: dict[str, object],
    *,
    tool_name: str,
) -> tuple[bool, str | None]:
    success = payload.get("success")
    if success is None and tool_name == "google_search":
        if "organic" in payload or "searchParameters" in payload:
            return True, None
    if success is None:
        if "error" not in payload:
            return True, None
        success = False
    if not isinstance(success, bool):
        raise MiroThinkerTaskLogError(
            f"MiroThinker tool result success must be boolean: tool_name={tool_name}"
        )
    if success:
        return True, None
    return False, _summarize_tool_failure(tool_name=tool_name, payload=payload)


def _summarize_tool_failure(*, tool_name: str, payload: dict[str, object]) -> str:
    raw_error = payload.get("error")
    if not isinstance(raw_error, str) or not raw_error.strip():
        return f"{tool_name}: success=false"
    return f"{tool_name}: {_normalize_failure_text(raw_error)}"


def _normalize_failure_text(value: str) -> str:
    normalized = " ".join(value.strip().split())
    return normalized[:240]


def _extract_source_refs_from_successful_tool_payloads(
    payloads: tuple[dict[str, object], ...],
) -> tuple[str, ...]:
    refs: list[str] = []
    for payload in payloads:
        _collect_source_ref_texts(payload, refs)
    return _unique_texts(refs)


def _tool_result_contains_inspected_source_material(tool_name: str) -> bool:
    normalized = tool_name.strip().lower()
    return normalized in {"scrape_and_extract_info", "jina_scrape_llm_summary"}


def _extract_inspected_source_refs_from_successful_tool_payloads(
    payloads: tuple[dict[str, object], ...],
) -> tuple[str, ...]:
    refs: list[str] = []
    for payload in payloads:
        url = payload.get("url")
        if isinstance(url, str):
            refs.extend(_extract_http_urls(url))
    return _unique_texts(refs)


def _collect_source_ref_texts(value: object, refs: list[str]) -> None:
    if isinstance(value, str):
        refs.extend(_extract_http_urls(value))
        return
    if isinstance(value, list):
        for item in value:
            _collect_source_ref_texts(item, refs)
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_source_ref_texts(item, refs)


def _extract_http_urls(value: str) -> tuple[str, ...]:
    return tuple(
        match.rstrip(".,;:)]}'\"") for match in re.findall(r"https?://[^\s\"'<>\])}]+", value)
    )


def _unique_texts(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return tuple(unique_values)


__all__ = [
    "MiroThinkerTaskLogError",
    "MiroThinkerCollectionToolTrace",
    "MiroThinkerToolUseTrace",
    "extract_assistant_text_candidates",
    "extract_collection_tool_trace",
    "extract_collection_tool_trace_from_tool_result",
    "extract_tool_use_trace",
    "load_latest_task_log_assistant_texts",
    "load_latest_task_log_collection_tool_trace",
    "load_latest_task_log_tool_use_trace",
    "load_task_log_assistant_texts",
    "load_task_log_tool_use_trace",
]
