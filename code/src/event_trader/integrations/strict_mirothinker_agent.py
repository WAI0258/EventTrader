"""Strict project-owned runtime contract for MiroThinker-backed agents."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from event_trader.integrations.mirothinker_task_log import mirothinker_portable_run_id

ErrorFactory = Callable[[str], Exception]


@dataclass(frozen=True, slots=True)
class StrictMiroThinkerAgentContract:
    """Minimum runtime contract event-trader requires before trusting an agent run."""

    agent_name: str
    required_tools: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.agent_name, str) or not self.agent_name.strip():
            raise ValueError("agent_name must be a non-blank string.")
        normalized_tools: list[tuple[str, str]] = []
        for index, item in enumerate(self.required_tools):
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0].strip()
                or not isinstance(item[1], str)
                or not item[1].strip()
            ):
                raise ValueError(
                    f"required_tools item {index} must be a "
                    "(server_name, tool_name) tuple of non-blank strings."
                )
            normalized_tools.append((item[0].strip(), item[1].strip()))
        object.__setattr__(self, "agent_name", self.agent_name.strip())
        object.__setattr__(self, "required_tools", tuple(normalized_tools))


@dataclass(frozen=True, slots=True)
class StrictMiroThinkerAgentRunResult:
    """Vendor run output after passing strict event-trader runtime checks."""

    final_summary: str
    final_boxed_answer: str
    log_file_path: str
    failure_experience_summary: object


def mirothinker_structured_output_fallback_texts_from_log(
    log_file_path: str | None,
) -> tuple[str, ...]:
    """Return project-owned structured-output candidates from a MiroThinker log.

    MiroThinker's vendor final-answer summarizer is optimized for short answers and
    can corrupt event-trader JSON contracts into values such as ``yes`` or
    ``strong_long``.  The authoritative candidate is often still present in the
    raw final-answer step or assistant message history, so event-trader extracts
    those texts once here and lets each business contract validate the payload.
    """

    if not isinstance(log_file_path, str) or not log_file_path.strip():
        return ()
    try:
        payload = json.loads(Path(log_file_path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict):
        return ()

    candidates: list[str] = []
    step_logs = payload.get("step_logs")
    if isinstance(step_logs, list):
        for step in reversed(step_logs):
            if not isinstance(step, dict):
                continue
            step_name = step.get("step_name")
            message = step.get("message")
            if not isinstance(step_name, str) or not isinstance(message, str):
                continue
            normalized_step_name = step_name.casefold()
            if "final answer" not in normalized_step_name:
                continue
            if "final boxed answer" in normalized_step_name:
                continue
            _append_structured_candidate(candidates, _strip_final_answer_marker(message))

    message_history = _main_agent_message_history(payload)
    for message in reversed(message_history):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for text in _message_content_texts(message.get("content")):
            _append_structured_candidate(candidates, text)
    return tuple(candidates)


def _main_agent_message_history(payload: dict[str, object]) -> tuple[object, ...]:
    history_container = payload.get("main_agent_message_history")
    if not isinstance(history_container, dict):
        return ()
    message_history = history_container.get("message_history")
    if not isinstance(message_history, list):
        return ()
    return tuple(message_history)


def _message_content_texts(content: object) -> tuple[str, ...]:
    if isinstance(content, str):
        return (content,)
    if not isinstance(content, list):
        return ()
    texts: list[str] = []
    for item in content:
        if isinstance(item, str):
            texts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            texts.append(text)
    return tuple(texts)


def _append_structured_candidate(candidates: list[str], text: str) -> None:
    normalized = text.strip()
    if not _looks_like_structured_output_candidate(normalized):
        return
    if normalized in candidates:
        return
    candidates.append(normalized)


def _strip_final_answer_marker(message: str) -> str:
    marker = "Final answer content:"
    marker_index = message.find(marker)
    if marker_index == -1:
        return message.strip()
    return message[marker_index + len(marker) :].strip()


def _looks_like_structured_output_candidate(text: str) -> bool:
    if not text:
        return False
    normalized = text.lstrip()
    if normalized.startswith(("{", "\\{", "<boxed>", "\\boxed{", "\\\\boxed{")):
        return True
    return normalized.startswith(
        (
            '"target_key"',
            '"event_ids"',
            '"pm_review_request_id"',
            '"assessed_horizons"',
            '"thesis_assessment"',
            '"episode_id"',
        )
    )


async def run_strict_mirothinker_agent(
    *,
    contract: StrictMiroThinkerAgentContract,
    tool_manager: Any,
    execute_task_pipeline: Any,
    cfg: Any,
    task_id: str,
    task_description: str,
    task_file_name: str,
    sub_agent_tool_managers: dict[str, Any],
    output_formatter: Any,
    log_dir: Path,
    error_factory: ErrorFactory,
    allow_context_window_failure: bool = False,
) -> StrictMiroThinkerAgentRunResult:
    """Run one MiroThinker task under event-trader's strict runtime contract."""
    if not isinstance(contract, StrictMiroThinkerAgentContract):
        raise error_factory("contract must be a StrictMiroThinkerAgentContract.")
    if not callable(execute_task_pipeline):
        raise error_factory("execute_task_pipeline must be callable.")
    if not isinstance(log_dir, Path):
        raise error_factory("log_dir must be a Path.")

    await preflight_required_mcp_tools(
        tool_manager=tool_manager,
        required_tools=contract.required_tools,
        error_factory=error_factory,
        agent_name=contract.agent_name,
    )
    try:
        final_summary, final_boxed_answer, log_file_path, failure_experience_summary = (
            await execute_task_pipeline(
                cfg=cfg,
                task_id=task_id,
                task_description=task_description,
                task_file_name=task_file_name,
                main_agent_tool_manager=tool_manager,
                sub_agent_tool_managers=sub_agent_tool_managers,
                output_formatter=output_formatter,
                log_dir=str(log_dir),
            )
        )
    except BaseException as exc:
        _mark_latest_task_log_failed(task_id=task_id, log_dir=log_dir, exc=exc)
        raise
    raise_on_mirothinker_limit_failure(
        log_file_path=log_file_path,
        error_factory=error_factory,
        agent_name=contract.agent_name,
        allow_context_window_failure=allow_context_window_failure,
    )
    _assert_task_log_terminal_success(
        log_file_path=Path(log_file_path),
        final_boxed_answer=final_boxed_answer,
        error_factory=error_factory,
        agent_name=contract.agent_name,
    )
    return StrictMiroThinkerAgentRunResult(
        final_summary=final_summary,
        final_boxed_answer=final_boxed_answer,
        log_file_path=log_file_path,
        failure_experience_summary=failure_experience_summary,
    )


async def preflight_required_mcp_tools(
    *,
    tool_manager: Any,
    required_tools: tuple[tuple[str, str], ...],
    error_factory: ErrorFactory,
    agent_name: str,
) -> None:
    """Fail before the first LLM call when a required MCP tool is unavailable."""
    if not required_tools:
        return
    native_preflight = getattr(tool_manager, "preflight_required_tools", None)
    if callable(native_preflight):
        await native_preflight(
            required_tools=required_tools,
            error_factory=error_factory,
            agent_name=agent_name,
        )
        return
    server_configs = getattr(tool_manager, "server_dict", None)
    if not isinstance(server_configs, dict):
        raise error_factory(
            f"{agent_name} MCP preflight could not read configured servers."
        )
    expected_tools_by_server: dict[str, set[str]] = {}
    for server_name, expected_tool_name in required_tools:
        expected_tools_by_server.setdefault(server_name, set()).add(expected_tool_name)
    for server_name, expected_tool_names in expected_tools_by_server.items():
        server_params = server_configs.get(server_name)
        if server_params is None:
            raise error_factory(
                f"{agent_name} MCP preflight failed for {server_name}; "
                "server was not installed."
            )
        await _preflight_one_mcp_server(
            server_name=server_name,
            expected_tool_names=tuple(sorted(expected_tool_names)),
            server_params=server_params,
            error_factory=error_factory,
            agent_name=agent_name,
        )


async def _preflight_one_mcp_server(
    *,
    server_name: str,
    expected_tool_names: tuple[str, ...],
    server_params: Any,
    error_factory: ErrorFactory,
    agent_name: str,
) -> None:
    try:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client
    except ModuleNotFoundError as exc:
        raise error_factory(
            f"{agent_name} MCP preflight failed for {server_name}; "
            "MCP client runtime is not available."
        ) from exc

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write, sampling_callback=None) as session:
                await session.initialize()
                tools_response = await session.list_tools()
                available_tool_names = {tool.name for tool in tools_response.tools}
    except Exception as exc:
        raise error_factory(
            f"{agent_name} MCP preflight failed for {server_name}; "
            f"expected tools {', '.join(expected_tool_names)}; "
            f"server error: {type(exc).__name__}: {exc}"
        ) from exc

    missing_tool_names = sorted(set(expected_tool_names) - available_tool_names)
    if missing_tool_names:
        available = ", ".join(sorted(available_tool_names)) or "<none>"
        raise error_factory(
            f"{agent_name} MCP preflight failed for {server_name}; expected tools "
            f"{', '.join(missing_tool_names)} were not listed. "
            f"available_tools={available}"
        )


def raise_on_mirothinker_limit_failure(
    *,
    log_file_path: str,
    error_factory: ErrorFactory,
    agent_name: str,
    allow_context_window_failure: bool = False,
) -> None:
    if not isinstance(log_file_path, str) or not log_file_path.strip():
        return
    path = Path(log_file_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    limit_signal = _task_log_limit_signal(payload)
    if limit_signal is None:
        return
    if limit_signal == "context_window":
        if allow_context_window_failure:
            return
        raise error_factory(
            f"{agent_name} hit an LLM context-window limit during execution; this "
            "is an event-trader agent contract failure."
        )
    return


def _task_log_limit_signal(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    error_text = payload.get("error")
    if isinstance(error_text, str) and error_text.strip():
        normalized_error = error_text.casefold()
        if "context window exceeds limit" in normalized_error:
            return "context_window"
    for step in payload.get("step_logs", ()):
        if not isinstance(step, dict):
            continue
        step_name = step.get("step_name")
        message = step.get("message")
        if not isinstance(step_name, str) or not isinstance(message, str):
            continue
        normalized_step = step_name.casefold()
        normalized_message = message.casefold()
        if (
            "call failed" in normalized_step
            and "context window exceeds limit" in normalized_message
        ):
            return "context_window"
    return None


def _assert_task_log_terminal_success(
    *,
    log_file_path: Path,
    final_boxed_answer: str,
    error_factory: ErrorFactory,
    agent_name: str,
) -> None:
    try:
        payload = json.loads(log_file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise error_factory(
            f"{agent_name} MiroThinker task log could not be read: {log_file_path}."
        ) from exc
    if not isinstance(payload, dict):
        raise error_factory(
            f"{agent_name} MiroThinker task log must be a JSON object: {log_file_path}."
        )
    status = payload.get("status")
    if status != "success":
        raise error_factory(
            f"{agent_name} MiroThinker task log ended with status={status!r}; "
            f"log_file_path={log_file_path}; error={payload.get('error')!r}."
        )
    if not isinstance(final_boxed_answer, str) or not final_boxed_answer.strip():
        raise error_factory(
            f"{agent_name} MiroThinker task log ended without a final boxed answer; "
            f"log_file_path={log_file_path}."
        )


def _mark_latest_task_log_failed(
    *,
    task_id: str,
    log_dir: Path,
    exc: BaseException,
) -> None:
    try:
        log_path = _select_latest_task_log_file(task_id=task_id, log_dir=log_dir)
        if log_path is None:
            return
        payload = json.loads(log_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        payload["status"] = "failed"
        payload["error"] = f"{type(exc).__name__}: {exc}"
        step_logs = payload.setdefault("step_logs", [])
        if isinstance(step_logs, list):
            step_logs.append(
                {
                    "step_name": "strict_runtime_exception",
                    "message": (
                        "Project strict runtime caught an exception that bypassed "
                        f"vendor Exception handling: {type(exc).__name__}: {exc}"
                    ),
                    "timestamp": _utc_plus_8_timestamp(),
                    "info_level": "error",
                    "metadata": {"exception_type": type(exc).__name__},
                }
            )
        log_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        return


def _select_latest_task_log_file(*, task_id: str, log_dir: Path) -> Path | None:
    if not log_dir.exists() or not log_dir.is_dir():
        return None
    run_id = mirothinker_portable_run_id(task_id)
    matches = sorted(
        log_dir.glob(f"task_{run_id}_*.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    return matches[0] if matches else None


def _utc_plus_8_timestamp() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


__all__ = [
    "StrictMiroThinkerAgentContract",
    "StrictMiroThinkerAgentRunResult",
    "preflight_required_mcp_tools",
    "raise_on_mirothinker_limit_failure",
    "run_strict_mirothinker_agent",
]
