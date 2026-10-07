"""Thin MiroThinker-backed runtime bindings for reflection evaluation."""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from event_trader.integrations.mirothinker_llm_config import (
    build_mirothinker_llm_config,
)
from event_trader.integrations.mirothinker_runtime_paths import (
    MiroThinkerRuntimePathError,
    build_mirothinker_child_pythonpath,
    prepare_mirothinker_runtime,
)
from event_trader.integrations.mirothinker_task_log import mirothinker_portable_run_id
from event_trader.integrations.strict_mirothinker_agent import (
    StrictMiroThinkerAgentContract,
    mirothinker_structured_output_fallback_texts_from_log,
    run_strict_mirothinker_agent,
)
from event_trader.reasoning.runtime import (
    AgentRunReceipt,
    AgentRuntime,
    AgentTask,
    AgentToolGateway,
)
from event_trader.reflection.agent_contract import (
    REFLECTION_AGENT_ROLE,
    REFLECTION_TOOL_SERVER_NAME,
    REQUIRED_REFLECTION_TOOL_NAMES,
)
from event_trader.reflection.supervisor import (
    OpenPositionReflectionCallback,
    ReflectionAttemptBinding,
    ReflectionAttemptFactory,
    ReflectionAttemptScope,
    ReflectionCallback,
    ReflectionRuntimeError,
    TargetCloseReflectionCallback,
    TargetReflectionCallback,
)
from event_trader.reflection.supervisor import (
    build_open_position_reflection_runner as build_project_open_position_runner,
)
from event_trader.reflection.supervisor import (
    build_reflection_runner as build_project_reflection_runner,
)
from event_trader.reflection.supervisor import (
    build_target_close_reflection_runner as build_project_target_close_runner,
)
from event_trader.reflection.supervisor import (
    build_target_reflection_runner as build_project_target_runner,
)
from event_trader.storage import WorkspaceLayout

_REFLECTION_REQUIRED_MCP_TOOLS = tuple(
    (REFLECTION_TOOL_SERVER_NAME, tool_name) for tool_name in REQUIRED_REFLECTION_TOOL_NAMES
)
_REFLECTION_READ_TOOL_NAMES = frozenset(REQUIRED_REFLECTION_TOOL_NAMES)
_REFLECTION_AGENT_CONTRACT = StrictMiroThinkerAgentContract(
    agent_name="MiroThinker reflection agent",
    required_tools=_REFLECTION_REQUIRED_MCP_TOOLS,
)


class MiroThinkerReflectionRuntimeError(RuntimeError):
    """Raised when the committed reflection runtime cannot execute."""


@dataclass(frozen=True, slots=True)
class _ReflectionToolUseTrace:
    attempted_tool_names: tuple[str, ...]
    successful_tool_names: tuple[str, ...]
    failure_details: tuple[str, ...] = ()


class _ReflectionToolTraceRecorder:
    def __init__(self) -> None:
        self._attempted_tool_names: list[str] = []
        self._successful_tool_names: list[str] = []
        self._failure_details: list[str] = []
        self._tool_manager: Any | None = None
        self._original_execute_tool_call: Any | None = None

    def install(self, tool_manager: Any) -> None:
        original_execute_tool_call = getattr(tool_manager, "execute_tool_call", None)
        if not callable(original_execute_tool_call):
            raise MiroThinkerReflectionRuntimeError(
                "Could not install reflection read audit recorder; "
                "ToolManager execute_tool_call is unavailable."
            )
        self._tool_manager = tool_manager
        self._original_execute_tool_call = original_execute_tool_call

        async def recorded_execute_tool_call(
            *args: object,
            **kwargs: object,
        ) -> Any:
            server_name, tool_name, _arguments = _normalize_reflection_tool_call_args(
                args,
                kwargs,
            )
            if server_name == REFLECTION_TOOL_SERVER_NAME:
                self._attempted_tool_names.append(tool_name)
            try:
                tool_result = await original_execute_tool_call(*args, **kwargs)
            except Exception as exc:
                if server_name == REFLECTION_TOOL_SERVER_NAME:
                    self._failure_details.append(f"{tool_name}: {type(exc).__name__}: {exc}")
                raise
            failure_text = _reflection_tool_result_failure_text(tool_result)
            if server_name == REFLECTION_TOOL_SERVER_NAME and failure_text is not None:
                self._failure_details.append(f"{tool_name}: {failure_text}")
            elif server_name == REFLECTION_TOOL_SERVER_NAME:
                self._successful_tool_names.append(tool_name)
            return tool_result

        tool_manager.execute_tool_call = recorded_execute_tool_call

    def uninstall(self) -> None:
        if self._tool_manager is not None and self._original_execute_tool_call is not None:
            self._tool_manager.execute_tool_call = self._original_execute_tool_call
        self._tool_manager = None
        self._original_execute_tool_call = None

    def snapshot(self) -> _ReflectionToolUseTrace:
        return _ReflectionToolUseTrace(
            attempted_tool_names=tuple(self._attempted_tool_names),
            successful_tool_names=_unique_strings(self._successful_tool_names),
            failure_details=tuple(self._failure_details),
        )


def _normalize_reflection_tool_call_args(
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> tuple[str, str, object]:
    if args:
        if len(args) != 3:
            raise TypeError("execute_tool_call expects server_name, tool_name, arguments.")
        server_name, tool_name, arguments = args
    else:
        server_name = kwargs.get("server_name")
        tool_name = kwargs.get("tool_name")
        arguments = kwargs.get("arguments")
    if not isinstance(server_name, str) or not isinstance(tool_name, str):
        raise TypeError("execute_tool_call expects server_name and tool_name as strings.")
    return server_name, tool_name, arguments


def _reflection_tool_result_failure_text(tool_result: object) -> str | None:
    if isinstance(tool_result, Mapping):
        error = tool_result.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()

        success = tool_result.get("success")
        if success is False or (
            isinstance(success, str) and success.strip().lower() in {"false", "0"}
        ):
            return "tool returned success=false"

        status = tool_result.get("status")
        if isinstance(status, str) and status.strip().lower() in {"error", "failed"}:
            return f"tool returned status={status}"

        raw_result = tool_result.get("result")
        if raw_result is not None:
            return _reflection_tool_payload_failure_text(raw_result)
    return _reflection_tool_payload_failure_text(tool_result)


def _reflection_tool_payload_failure_text(raw_payload: object) -> str | None:
    payload = raw_payload
    if isinstance(raw_payload, str):
        normalized = raw_payload.strip()
        if not normalized:
            return None
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, Mapping):
        return None

    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()

    success = payload.get("success")
    if success is False or (isinstance(success, str) and success.strip().lower() in {"false", "0"}):
        return "success=false"

    status = payload.get("status")
    if isinstance(status, str) and status.strip().lower() in {"error", "failed"}:
        return f"status={status}"
    return None


def _unique_strings(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return tuple(unique)


@dataclass(frozen=True, slots=True)
class MiroThinkerReflectionRuntimeConfig:
    """Explicit runtime inputs required to execute one reflection pass."""

    vendor_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_max_context_length: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "vendor_root",
            _validate_existing_dir(self.vendor_root, field_name="vendor_root"),
        )
        object.__setattr__(
            self,
            "log_dir",
            _validate_path(self.log_dir, field_name="log_dir"),
        )
        object.__setattr__(
            self,
            "llm_provider",
            _validate_non_blank_text(self.llm_provider, field_name="llm_provider"),
        )
        object.__setattr__(
            self,
            "llm_model_name",
            _validate_non_blank_text(
                self.llm_model_name,
                field_name="llm_model_name",
            ),
        )
        object.__setattr__(
            self,
            "llm_api_key",
            _validate_non_blank_text(self.llm_api_key, field_name="llm_api_key"),
        )
        object.__setattr__(
            self,
            "llm_base_url",
            _validate_non_blank_text(self.llm_base_url, field_name="llm_base_url"),
        )
        if (
            not isinstance(self.llm_max_context_length, int)
            or isinstance(self.llm_max_context_length, bool)
            or self.llm_max_context_length <= 0
        ):
            raise MiroThinkerReflectionRuntimeError(
                "llm_max_context_length must be a positive integer."
            )


@dataclass(frozen=True, slots=True)
class _MiroThinkerReflectionToolGateway:
    tool_manager: Any

    @property
    def server_name(self) -> str:
        return REFLECTION_TOOL_SERVER_NAME

    @property
    def available_tool_names(self) -> tuple[str, ...]:
        return REQUIRED_REFLECTION_TOOL_NAMES

    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> object:
        if tool_name not in self.available_tool_names:
            raise MiroThinkerReflectionRuntimeError(
                f"Reflection tool is not available: {tool_name}"
            )
        execute_tool_call = getattr(self.tool_manager, "execute_tool_call", None)
        if not callable(execute_tool_call):
            raise MiroThinkerReflectionRuntimeError(
                "Reflection tool gateway requires ToolManager.execute_tool_call."
            )
        return await execute_tool_call(
            self.server_name,
            tool_name,
            dict(arguments),
        )


@dataclass(frozen=True, slots=True)
class _MiroThinkerReflectionAgentRuntime:
    tools: _MiroThinkerReflectionToolGateway
    execute_task_pipeline: Any
    cfg: Any
    sub_agent_tool_managers: dict[str, Any]
    output_formatter: Any
    log_dir: Path

    async def run_once(
        self,
        task: AgentTask,
        tools: AgentToolGateway,
    ) -> AgentRunReceipt:
        if task.role != REFLECTION_AGENT_ROLE:
            raise MiroThinkerReflectionRuntimeError(
                f"MiroThinker reflection runtime requires role={REFLECTION_AGENT_ROLE!r}."
            )
        if task.required_tool_names != REQUIRED_REFLECTION_TOOL_NAMES:
            raise MiroThinkerReflectionRuntimeError(
                "MiroThinker reflection runtime requires the canonical reflection tool contract."
            )
        if tools is not self.tools:
            raise MiroThinkerReflectionRuntimeError(
                "MiroThinker reflection runtime received an unexpected tool gateway."
            )
        tool_trace_recorder = _ReflectionToolTraceRecorder()
        tool_trace_recorder.install(self.tools.tool_manager)
        try:
            run_result = await run_strict_mirothinker_agent(
                contract=_REFLECTION_AGENT_CONTRACT,
                tool_manager=self.tools.tool_manager,
                execute_task_pipeline=self.execute_task_pipeline,
                cfg=self.cfg,
                task_id=task.task_id,
                task_description=task.task_description,
                task_file_name="",
                sub_agent_tool_managers=self.sub_agent_tool_managers,
                output_formatter=self.output_formatter,
                log_dir=self.log_dir,
                error_factory=MiroThinkerReflectionRuntimeError,
                allow_context_window_failure=True,
            )
        finally:
            tool_trace_recorder.uninstall()
        tool_trace = tool_trace_recorder.snapshot()
        return AgentRunReceipt(
            final_summary=run_result.final_summary,
            contract_output_text=run_result.final_boxed_answer,
            diagnostic_log_ref=run_result.log_file_path,
            diagnostics={
                "failure_experience_summary": run_result.failure_experience_summary,
                "attempted_tool_names": tool_trace.attempted_tool_names,
                "successful_tool_names": tool_trace.successful_tool_names,
                "tool_failure_details": tool_trace.failure_details,
            },
            fallback_payload_texts=(
                mirothinker_structured_output_fallback_texts_from_log(run_result.log_file_path)
            ),
        )


def _build_mirothinker_reflection_attempt_runtime(
    *,
    tool_manager: Any,
    execute_task_pipeline: Any,
    cfg: Any,
    sub_agent_tool_managers: dict[str, Any],
    output_formatter: Any,
    log_dir: Path,
) -> tuple[AgentRuntime, AgentToolGateway]:
    tools = _MiroThinkerReflectionToolGateway(tool_manager=tool_manager)
    runtime = _MiroThinkerReflectionAgentRuntime(
        tools=tools,
        execute_task_pipeline=execute_task_pipeline,
        cfg=cfg,
        sub_agent_tool_managers=sub_agent_tool_managers,
        output_formatter=output_formatter,
        log_dir=log_dir,
    )
    return runtime, tools


def build_mirothinker_reflection_runner(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    layout: WorkspaceLayout,
) -> ReflectionCallback:
    """Build the committed shared reflection evaluator using vendored MiroThinker."""
    attempt_factory = _build_mirothinker_reflection_attempt_factory(
        config=config,
        layout=layout,
    )
    return _translate_project_runtime_errors(
        build_project_reflection_runner(attempt_factory=attempt_factory)
    )


def build_mirothinker_target_reflection_runner(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    layout: WorkspaceLayout,
) -> TargetReflectionCallback:
    """Build the committed target reflection evaluator using vendored MiroThinker."""
    attempt_factory = _build_mirothinker_reflection_attempt_factory(
        config=config,
        layout=layout,
    )
    return _translate_project_runtime_errors(
        build_project_target_runner(attempt_factory=attempt_factory)
    )


def build_mirothinker_target_close_reflection_runner(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    layout: WorkspaceLayout,
) -> TargetCloseReflectionCallback:
    """Build the committed target close reflection evaluator."""
    attempt_factory = _build_mirothinker_reflection_attempt_factory(
        config=config,
        layout=layout,
    )
    return _translate_project_runtime_errors(
        build_project_target_close_runner(attempt_factory=attempt_factory)
    )


def build_mirothinker_open_position_reflection_runner(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    layout: WorkspaceLayout,
) -> OpenPositionReflectionCallback:
    """Build the committed open-position reflection evaluator."""
    attempt_factory = _build_mirothinker_reflection_attempt_factory(
        config=config,
        layout=layout,
    )
    return _translate_project_runtime_errors(
        build_project_open_position_runner(attempt_factory=attempt_factory)
    )


def _build_mirothinker_reflection_attempt_factory(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    layout: WorkspaceLayout,
) -> ReflectionAttemptFactory:
    if not isinstance(layout, WorkspaceLayout):
        raise MiroThinkerReflectionRuntimeError("layout must be a WorkspaceLayout instance.")
    vendor_root = config.vendor_root.resolve(strict=False)
    workspace_root = layout.root.resolve(strict=False)

    def build(scope: ReflectionAttemptScope) -> ReflectionAttemptBinding:
        execute_task_pipeline, create_pipeline_components, omega_conf = _load_vendor_runtime(
            vendor_root
        )
        agent_cfg = _build_agent_cfg(config, omega_conf=omega_conf)
        _prepare_vendor_environment(config)
        config.log_dir.mkdir(parents=True, exist_ok=True)
        main_agent_tool_manager, sub_agent_tool_managers, output_formatter = (
            create_pipeline_components(agent_cfg)
        )
        _install_reflection_mcp_server(
            tool_manager=main_agent_tool_manager,
            vendor_root=vendor_root,
            workspace_root=workspace_root,
            target_key=scope.target_key,
            window_start=scope.window_start,
            window_end=scope.window_end,
        )
        runtime, tools = _build_mirothinker_reflection_attempt_runtime(
            tool_manager=main_agent_tool_manager,
            execute_task_pipeline=execute_task_pipeline,
            cfg=agent_cfg,
            sub_agent_tool_managers=sub_agent_tool_managers,
            output_formatter=output_formatter,
            log_dir=config.log_dir,
        )
        return ReflectionAttemptBinding(
            task_id=mirothinker_portable_run_id(scope.logical_task_id),
            runtime=runtime,
            tools=tools,
        )

    return build


def _translate_project_runtime_errors[ContextT, ResultT](
    callback: Callable[[ContextT], ResultT],
) -> Callable[[ContextT], ResultT]:
    def wrapped(context: ContextT) -> ResultT:
        try:
            return callback(context)
        except ReflectionRuntimeError as exc:
            raise MiroThinkerReflectionRuntimeError(str(exc)) from exc

    return wrapped


def _build_agent_cfg(
    config: MiroThinkerReflectionRuntimeConfig,
    *,
    omega_conf: Any,
) -> Any:
    return omega_conf.create(
        {
            "project_name": "event-trader",
            "debug_dir": str(config.log_dir),
            "llm": build_mirothinker_llm_config(
                provider=config.llm_provider,
                model_name=config.llm_model_name,
                api_key=config.llm_api_key,
                base_url=config.llm_base_url,
                temperature=0.1,
                max_tokens=4096,
                max_context_length=config.llm_max_context_length,
            ),
            "agent": {
                "main_agent": {
                    "tools": [],
                    "tool_blacklist": [],
                    "max_turns": 120,
                },
                "sub_agents": {},
                "keep_tool_result": 5,
                "context_compress_limit": 0,
            },
            "benchmark": {},
        }
    )


def _load_vendor_runtime(vendor_root: Path) -> tuple[Any, Any, Any]:
    _append_vendor_import_roots(vendor_root)
    try:
        pipeline_module = importlib.import_module("src.core.pipeline")
        omegaconf_module = importlib.import_module("omegaconf")
    except ModuleNotFoundError as exc:
        raise MiroThinkerReflectionRuntimeError(
            "MiroThinker reflection runtime dependencies are not installed. "
            "Install the vendored runtime dependencies before using this adapter."
        ) from exc

    return (
        pipeline_module.execute_task_pipeline,
        pipeline_module.create_pipeline_components,
        omegaconf_module.OmegaConf,
    )


def _append_vendor_import_roots(vendor_root: Path) -> None:
    try:
        prepare_mirothinker_runtime(vendor_root)
    except MiroThinkerRuntimePathError as exc:
        raise MiroThinkerReflectionRuntimeError(str(exc)) from exc


def _prepare_vendor_environment(config: MiroThinkerReflectionRuntimeConfig) -> None:
    os.environ["OPENAI_API_KEY"] = config.llm_api_key
    os.environ["OPENAI_BASE_URL"] = config.llm_base_url


def _install_reflection_mcp_server(
    *,
    tool_manager: Any,
    vendor_root: Path,
    workspace_root: Path,
    target_key: str | None,
    window_start: datetime,
    window_end: datetime,
) -> None:
    existing_configs = list(getattr(tool_manager, "server_configs", ()))
    base_params = existing_configs[0]["params"] if existing_configs else None
    params_env = dict(getattr(base_params, "env", {}) or {})
    cwd = getattr(base_params, "cwd", None) if base_params is not None else str(workspace_root)
    server_env = dict(params_env)
    server_env.update(
        {
            "EVENT_TRADER_WORKSPACE_ROOT": str(workspace_root),
            "EVENT_TRADER_REFLECTION_TARGET_KEY": target_key or "",
            "EVENT_TRADER_REFLECTION_WINDOW_START_AT": window_start.isoformat(),
            "EVENT_TRADER_REFLECTION_WINDOW_END_AT": window_end.isoformat(),
            "PYTHONPATH": build_mirothinker_child_pythonpath(
                vendor_root=vendor_root,
                project_src=Path(__file__).resolve(strict=False).parents[2],
            ),
        }
    )
    server_params = _make_stdio_server_parameters(
        command=sys.executable,
        args=["-m", "event_trader.integrations.reflection_mcp_server"],
        env=server_env,
        cwd=cwd,
    )
    tool_manager.server_configs = [
        *existing_configs,
        {"name": REFLECTION_TOOL_SERVER_NAME, "params": server_params},
    ]
    tool_manager.server_dict = {
        config["name"]: config["params"] for config in tool_manager.server_configs
    }


def _make_stdio_server_parameters(
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
    cwd: str | None,
) -> Any:
    try:
        from mcp import StdioServerParameters
    except ModuleNotFoundError as exc:
        raise MiroThinkerReflectionRuntimeError(
            "MCP runtime is not installed; cannot configure reflection read tools."
        ) from exc
    return StdioServerParameters(command=command, args=args, env=env, cwd=cwd)


def _validate_existing_dir(path: Path, *, field_name: str) -> Path:
    validated = _validate_path(path, field_name=field_name)
    if not validated.exists():
        raise MiroThinkerReflectionRuntimeError(f"{field_name} does not exist: {validated}")
    if not validated.is_dir():
        raise MiroThinkerReflectionRuntimeError(f"{field_name} must be a directory: {validated}")
    return validated


def _validate_path(path: Path, *, field_name: str) -> Path:
    if not isinstance(path, Path):
        raise MiroThinkerReflectionRuntimeError(f"{field_name} must be a pathlib.Path.")
    return path.resolve(strict=False)


def _validate_non_blank_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MiroThinkerReflectionRuntimeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MiroThinkerReflectionRuntimeError(f"{field_name} must not be blank.")
    return normalized


__all__ = [
    "MiroThinkerReflectionRuntimeConfig",
    "MiroThinkerReflectionRuntimeError",
    "build_mirothinker_reflection_runner",
    "build_mirothinker_target_close_reflection_runner",
    "build_mirothinker_target_reflection_runner",
    "build_mirothinker_open_position_reflection_runner",
]
