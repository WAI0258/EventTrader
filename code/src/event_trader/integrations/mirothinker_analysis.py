"""Thin MiroThinker-backed runtime binding for one analysis execution pass."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

from event_trader.analysis import AnalysisContext
from event_trader.context_assembly import (
    CURRENT_MEMORY_READ_POLICY,
    ContextPacketRuntimeScope,
)
from event_trader.contracts import (
    AnalysisResult,
    LLMUsageReceipt,
)
from event_trader.contracts.execution_direction_policy import (
    ExecutionDirectionMode,
    validate_execution_direction_mode,
)
from event_trader.integrations.analysis_mcp_transport import (
    ANALYSIS_MCP_SERVER_NAME,
    analysis_tool_context_to_env,
)
from event_trader.integrations.analysis_structured_finalizer import (
    AnalysisStructuredOutputMode,
    normalize_analysis_structured_output_mode,
)
from event_trader.integrations.mirothinker_runtime_paths import (
    MiroThinkerRuntimePathError,
    build_mirothinker_child_pythonpath,
    prepare_mirothinker_runtime,
)
from event_trader.integrations.strict_mirothinker_agent import (
    StrictMiroThinkerAgentRunResult,
    mirothinker_structured_output_fallback_texts_from_log,
)
from event_trader.reasoning.analysis_agent import (
    AnalysisAgentAttemptRequest,
    AnalysisAgentAttemptResult,
    AnalysisRepairOwnership,
)
from event_trader.reasoning.analysis_contract_repair import (
    AnalysisContractRepairPayloadError,
    analysis_contract_failure_for_write_tool,
    analysis_contract_repair_from_supervision_payload,
    analysis_write_failure_key,
    parse_analysis_write_tool_failure,
    render_analysis_contract_failure,
)
from event_trader.reasoning.analysis_contract_repair import (
    AnalysisContractRepairRequired as _AnalysisContractRepairRequired,
)
from event_trader.reasoning.analysis_contract_repair import (
    AnalysisWriteFailureBudgetExceeded as _AnalysisWriteFailureBudgetExceeded,
)
from event_trader.reasoning.analysis_output import load_analysis_payload
from event_trader.reasoning.analysis_supervisor import (
    AnalysisSupervisorConfig,
    AnalysisSupervisorRuntimeError,
    run_analysis_supervisor,
)
from event_trader.reasoning.analysis_write_receipts import (
    ANALYSIS_MATERIAL_WRITE_OPERATIONS,
)
from event_trader.reasoning.effort import normalize_agent_reasoning_effort
from event_trader.storage import (
    WorkspaceLayout,
)

type AnalysisCallback = Callable[[AnalysisContext], AnalysisResult]


_ANALYSIS_WRITE_FAILURE_BUDGET = 10
_ANALYSIS_SAME_WRITE_ERROR_ABORT_THRESHOLD = 2


class MiroThinkerAnalysisRuntimeError(RuntimeError):
    """Raised when the committed MiroThinker analysis runtime cannot execute."""


def _analysis_contract_repair_from_supervision_payload(
    payload: Mapping[str, object],
) -> _AnalysisContractRepairRequired:
    try:
        return analysis_contract_repair_from_supervision_payload(payload)
    except AnalysisContractRepairPayloadError as exc:
        raise MiroThinkerAnalysisRuntimeError(str(exc)) from exc


class _PersistentStdioMcpToolManager:
    """Tool manager that keeps one MCP stdio session alive per analysis attempt."""

    def __init__(
        self,
        server_configs: list[dict[str, object]],
        tool_blacklist: object = None,
    ):
        self.server_configs = server_configs
        self.server_dict = {
            config["name"]: config["params"]
            for config in server_configs
            if isinstance(config.get("name"), str)
        }
        self.tool_blacklist: set[tuple[str, str]] = (
            set(cast(set[tuple[str, str]], tool_blacklist)) if tool_blacklist else set()
        )
        self.task_log: Any | None = None
        self._exit_stack = AsyncExitStack()
        self._sessions: dict[str, object] = {}
        self._tool_definitions_cache: list[dict[str, object]] | None = None
        self._closed = False

    def set_task_log(self, task_log: object) -> None:
        self.task_log = task_log
        self._log(
            "info",
            "ToolManager | Initialization",
            f"Persistent ToolManager initialized, loaded servers: {list(self.server_dict.keys())}",
        )

    def _log(
        self,
        level: str,
        step_name: str,
        message: str,
        metadata: object | None = None,
    ) -> None:
        if self.task_log is None:
            return
        log_step = getattr(self.task_log, "log_step", None)
        if callable(log_step):
            log_step(level, step_name, message, metadata)

    async def aclose(self) -> None:
        await self._exit_stack.aclose()
        self._sessions.clear()
        self._tool_definitions_cache = None
        self._closed = True

    async def preflight_required_tools(
        self,
        *,
        required_tools: tuple[tuple[str, str], ...],
        error_factory: Callable[[str], Exception],
        agent_name: str,
    ) -> None:
        tool_definitions = await self.get_all_tool_definitions()
        available_by_server: dict[str, set[str]] = {}
        for server in tool_definitions:
            server_name = server.get("name")
            tools = server.get("tools")
            if not isinstance(server_name, str) or not isinstance(tools, list):
                continue
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                tool_error = tool.get("error")
                if isinstance(tool_error, str) and tool_error.strip():
                    available_by_server.setdefault(server_name, set()).add(
                        f"<error: {tool_error.strip()}>"
                    )
                    continue
                tool_name = tool.get("name")
                if isinstance(tool_name, str) and tool_name.strip():
                    available_by_server.setdefault(server_name, set()).add(tool_name)

        expected_by_server: dict[str, set[str]] = {}
        for server_name, tool_name in required_tools:
            expected_by_server.setdefault(server_name, set()).add(tool_name)
        for server_name, expected_tool_names in expected_by_server.items():
            available_tool_names = available_by_server.get(server_name, set())
            missing_tool_names = sorted(expected_tool_names - available_tool_names)
            if missing_tool_names:
                available = ", ".join(sorted(available_tool_names)) or "<none>"
                raise error_factory(
                    f"{agent_name} MCP preflight failed for {server_name}; expected tools "
                    f"{', '.join(missing_tool_names)} were not listed. "
                    f"available_tools={available}"
                )

    async def get_all_tool_definitions(self) -> list[dict[str, object]]:
        if self._tool_definitions_cache is not None:
            return self._tool_definitions_cache
        all_servers_for_prompt: list[dict[str, object]] = []
        for config in self.server_configs:
            server_name = config["name"]
            if not isinstance(server_name, str):
                continue
            one_server_for_prompt: dict[str, object] = {"name": server_name, "tools": []}
            try:
                session = await self._session_for(server_name)
                tools_response = await session.list_tools()
                prompt_tools: list[dict[str, object]] = []
                for tool in tools_response.tools:
                    if (server_name, tool.name) in self.tool_blacklist:
                        continue
                    prompt_tools.append(
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "schema": tool.inputSchema,
                        }
                    )
                one_server_for_prompt["tools"] = prompt_tools
                self._log(
                    "info",
                    "ToolManager | Tool Definitions Success",
                    "Successfully obtained "
                    f"{len(prompt_tools)} tool definitions from server '{server_name}'.",
                )
            except Exception as exc:
                self._log(
                    "error",
                    "ToolManager | Connection Error",
                    f"Error: Unable to connect or get tools from server '{server_name}': {exc}",
                )
                one_server_for_prompt["tools"] = [{"error": f"Unable to fetch tools: {exc}"}]
            all_servers_for_prompt.append(one_server_for_prompt)
        self._tool_definitions_cache = all_servers_for_prompt
        return all_servers_for_prompt

    async def execute_tool_call(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        if server_name not in self.server_dict:
            self._log(
                "error",
                "ToolManager | Server Not Found",
                f"Error: Attempting to call server '{server_name}' not found",
            )
            return {
                "server_name": server_name,
                "tool_name": tool_name,
                "error": f"Server '{server_name}' not found.",
            }
        self._log(
            "info",
            "ToolManager | Tool Call Start",
            f"Calling tool '{tool_name}' on persistent server '{server_name}'",
            metadata={"arguments": arguments},
        )
        try:
            session = await self._session_for(server_name)
            tool_result = await session.call_tool(tool_name, arguments=arguments)
            result_content = tool_result.content[-1].text if tool_result.content else ""
            self._log(
                "info",
                "ToolManager | Tool Call Success",
                f"Tool '{tool_name}' (server: '{server_name}') called successfully.",
            )
            return {
                "server_name": server_name,
                "tool_name": tool_name,
                "result": result_content,
            }
        except Exception as exc:
            self._log(
                "error",
                "ToolManager | Tool Call Failed",
                f"Error: Failed to call tool '{tool_name}' (server: '{server_name}'): {exc}",
            )
            return {
                "server_name": server_name,
                "tool_name": tool_name,
                "error": f"Tool call failed: {exc}",
            }

    async def _session_for(self, server_name: str) -> Any:
        if self._closed:
            raise RuntimeError("persistent MCP tool manager is closed.")
        existing_session = self._sessions.get(server_name)
        if existing_session is not None:
            return existing_session
        server_params = self.server_dict.get(server_name)
        if server_params is None:
            raise RuntimeError(f"Server '{server_name}' not found.")
        try:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client
        except ModuleNotFoundError as exc:
            raise MiroThinkerAnalysisRuntimeError("MCP client runtime is not available.") from exc
        read, write = await self._exit_stack.enter_async_context(
            stdio_client(cast(Any, server_params))
        )
        new_session: Any = await self._exit_stack.enter_async_context(
            ClientSession(read, write, sampling_callback=None)
        )
        await new_session.initialize()
        self._sessions[server_name] = new_session
        return new_session


@dataclass(frozen=True, slots=True)
class MiroThinkerAnalysisRuntimeConfig:
    """Explicit runtime inputs required to execute one MiroThinker analysis pass."""

    vendor_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_max_context_length: int
    runtime_mode: str
    config_path: Path
    market_data_store_root: Path | None = None
    market_data_snapshot_id: str | None = None
    llm_reasoning_effort: str | None = None
    memory_read_policy: str = CURRENT_MEMORY_READ_POLICY
    runtime_scope: ContextPacketRuntimeScope = "live"
    run_id: str = ""
    wall_clock_timeout_seconds: int = 0
    structured_output_mode: AnalysisStructuredOutputMode = "disabled"
    structured_output_probe: bool = True
    execution_direction_mode: ExecutionDirectionMode = "long_short"

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
            raise MiroThinkerAnalysisRuntimeError(
                "llm_max_context_length must be a positive integer."
            )
        object.__setattr__(
            self,
            "llm_reasoning_effort",
            normalize_agent_reasoning_effort(
                self.llm_reasoning_effort,
                field_name="llm_reasoning_effort",
                error_type=MiroThinkerAnalysisRuntimeError,
            ),
        )
        if self.runtime_mode not in {"live", "replay"}:
            raise MiroThinkerAnalysisRuntimeError("runtime_mode must be 'live' or 'replay'.")
        if self.runtime_scope not in {"live", "replay"}:
            raise MiroThinkerAnalysisRuntimeError("runtime_scope must be 'live' or 'replay'.")
        if self.runtime_scope == "live" and self.run_id:
            raise MiroThinkerAnalysisRuntimeError("live analysis runtime must not carry run_id.")
        if self.runtime_scope == "replay":
            if not self.run_id:
                raise MiroThinkerAnalysisRuntimeError("replay analysis runtime requires run_id.")
        if not isinstance(self.memory_read_policy, str) or not self.memory_read_policy:
            raise MiroThinkerAnalysisRuntimeError("memory_read_policy must be a non-empty string.")
        if (
            not isinstance(self.wall_clock_timeout_seconds, int)
            or isinstance(self.wall_clock_timeout_seconds, bool)
            or self.wall_clock_timeout_seconds < 0
        ):
            raise MiroThinkerAnalysisRuntimeError(
                "wall_clock_timeout_seconds must be a non-negative integer."
            )
        object.__setattr__(
            self,
            "structured_output_mode",
            normalize_analysis_structured_output_mode(self.structured_output_mode),
        )
        if not isinstance(self.structured_output_probe, bool):
            raise MiroThinkerAnalysisRuntimeError("structured_output_probe must be a boolean.")
        object.__setattr__(
            self,
            "execution_direction_mode",
            validate_execution_direction_mode(
                self.execution_direction_mode,
                error_type=MiroThinkerAnalysisRuntimeError,
            ),
        )
        object.__setattr__(
            self,
            "config_path",
            _validate_path(self.config_path, field_name="config_path"),
        )
        if self.market_data_store_root is not None:
            object.__setattr__(
                self,
                "market_data_store_root",
                _validate_path(
                    self.market_data_store_root,
                    field_name="market_data_store_root",
                ),
            )


@dataclass(frozen=True, slots=True)
class _MiroThinkerAnalysisAgentExecutor:
    """Translate one project-owned attempt into the strict MiroThinker worker."""

    config: MiroThinkerAnalysisRuntimeConfig
    vendor_root: Path
    workspace_root: Path
    repair_ownership: ClassVar[AnalysisRepairOwnership] = "supervisor_attempt"

    async def execute(
        self,
        request: AnalysisAgentAttemptRequest,
    ) -> AnalysisAgentAttemptResult:
        analysis_server_env = analysis_tool_context_to_env(
            request.tool_context,
            config_path=self.config.config_path,
            pythonpath=_build_child_pythonpath(self.vendor_root),
        )
        run_result = await _run_supervised_strict_analysis_agent(
            config=self.config,
            vendor_root=self.vendor_root,
            workspace_root=self.workspace_root,
            stage_root=request.tool_context.stage_layout.root,
            task_id=request.task_id,
            task_description=_mirothinker_task_description(request),
            analysis_server_env=analysis_server_env,
        )
        payload = load_analysis_payload(
            final_boxed_answer=run_result.final_boxed_answer,
            final_summary=run_result.final_summary,
            fallback_payload_texts=(
                mirothinker_structured_output_fallback_texts_from_log(
                    run_result.log_file_path
                )
            ),
            attempt_index=request.attempt_index,
            agent_label="MiroThinker Analysis agent",
        )
        return AnalysisAgentAttemptResult(
            payload=payload,
            llm_usage=_llm_usage_from_mirothinker_task_log(
                log_file_path=run_result.log_file_path,
                agent_role="analysis",
                provider=self.config.llm_provider,
                model=self.config.llm_model_name,
            ),
        )


def _mirothinker_task_description(request: AnalysisAgentAttemptRequest) -> str:
    target_key = request.tool_context.target_key
    task = request.business_task
    execution_protocol = (
        "MiroThinker Analysis execution protocol:\n"
        "- You own a free tool-using Analysis attempt. Use the project tools to obtain "
        "context that the business brief identifies as missing, truncated, stale, "
        "contradictory, or necessary for citations and durable writes.\n"
        "- Use read_evidence for active source text; use read_page, read_section, "
        "read_around_citation, list_pages, and search_wiki for target ResearchMemory; "
        "use the bounded market tools only for visible decision-time facts.\n"
        f"- Wiki discovery scopes are `shared`, `target:{target_key}`, `target/{target_key}`, "
        f"or `targets/{target_key}`. Prefer section or citation reads over broad page "
        "reads when a page is truncated.\n"
        "- If durable cognition did not change, do not call a write tool. If it changed, "
        "write only to the staging wiki with update_page_section, rewrite_page, or "
        "create_page as appropriate. Never write deterministic projection sections.\n"
        "- update_page_section supplies only the section body. Use rewrite_page only "
        "when page structure is materially wrong, and preserve every required fixed-page "
        "section exactly once in canonical order. Use create_page only for a durable "
        "research object.\n"
        "- Before returning a non-null analysis_assessment, call "
        "validate_analysis_final_payload with the exact intended payload. Repair any "
        "ok=false result and validate again. The successful validator call must be the "
        "last tool call; afterwards return the same JSON byte-for-byte.\n"
        "- Return exactly one final Analysis JSON object wrapped in \\boxed{...}. The "
        "project parser and downstream Contract remain authoritative.\n"
    )
    parts = [
        task.business_brief.rstrip(),
        task.final_payload_contract.rstrip(),
        execution_protocol.rstrip(),
    ]
    if task.repair_failure is not None:
        parts.extend(
            (
                "Deterministic contract repair feedback for this attempt:\n"
                f"{render_analysis_contract_failure(task.repair_failure)}",
                "Repeat the same business task, use the required read/write tools again "
                "as needed, and return one corrected JSON object wrapped in \\boxed{...}.",
            )
        )
    return "\n\n".join(parts) + "\n"


def build_mirothinker_analysis_runner(
    *,
    config: MiroThinkerAnalysisRuntimeConfig,
    layout: WorkspaceLayout,
) -> AnalysisCallback:
    """Build the committed analysis callback using vendored MiroThinker."""
    if not isinstance(layout, WorkspaceLayout):
        raise MiroThinkerAnalysisRuntimeError("layout must be a WorkspaceLayout instance.")
    vendor_root = config.vendor_root.resolve(strict=False)
    workspace_root = layout.root.resolve(strict=False)
    # Fail before replay/live can persist AnalysisFailed artifacts for a
    # production runner that is not actually executable.
    _load_vendor_runtime(vendor_root)
    agent_executor = _MiroThinkerAnalysisAgentExecutor(
        config=config,
        vendor_root=vendor_root,
        workspace_root=workspace_root,
    )
    supervisor_config = AnalysisSupervisorConfig(
        config_path=config.config_path,
        runtime_mode=cast(Literal["live", "replay"], config.runtime_mode),
        runtime_scope=config.runtime_scope,
        run_id=config.run_id,
        memory_read_policy=config.memory_read_policy,
        structured_output_mode=config.structured_output_mode,
        structured_output_probe=config.structured_output_probe,
        execution_direction_mode=config.execution_direction_mode,
        llm_provider=config.llm_provider,
        llm_model_name=config.llm_model_name,
        llm_api_key=config.llm_api_key,
        llm_base_url=config.llm_base_url,
        market_data_store_root=config.market_data_store_root,
        market_data_snapshot_id=config.market_data_snapshot_id,
        agent_label="MiroThinker analysis agent",
    )

    def analyze(context: AnalysisContext) -> AnalysisResult:
        if not isinstance(context, AnalysisContext):
            raise MiroThinkerAnalysisRuntimeError("context must be an AnalysisContext instance.")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise MiroThinkerAnalysisRuntimeError(
                "build_mirothinker_analysis_runner currently supports only "
                "synchronous callers; do not call it from an active event loop."
            )

        config.log_dir.mkdir(parents=True, exist_ok=True)
        _prepare_vendor_environment(config)
        try:
            return asyncio.run(
                run_analysis_supervisor(
                    config=supervisor_config,
                    layout=layout,
                    context=context,
                    agent_executor=agent_executor,
                )
            )
        except AnalysisSupervisorRuntimeError as exc:
            raise MiroThinkerAnalysisRuntimeError(str(exc)) from exc

    return analyze


def _json_compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load_vendor_runtime(vendor_root: Path) -> tuple[Any, Any, Any, Any, Any]:
    _append_vendor_import_roots(vendor_root)
    try:
        pipeline_module = importlib.import_module("src.core.pipeline")
        output_formatter_module = importlib.import_module("src.io.output_formatter")
        omegaconf_module = importlib.import_module("omegaconf")
        manager_module = importlib.import_module("miroflow_tools.manager")
        mcp_module = importlib.import_module("mcp")
    except ModuleNotFoundError as exc:
        missing_dependency = exc.name or str(exc)
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis runtime dependencies are not installed. "
            "Install the vendored runtime dependencies before using this adapter. "
            f"Missing module: {missing_dependency}."
        ) from exc

    return (
        pipeline_module.execute_task_pipeline,
        output_formatter_module.OutputFormatter,
        manager_module.ToolManager,
        mcp_module.StdioServerParameters,
        omegaconf_module.OmegaConf,
    )


def _build_analysis_tool_manager(
    *,
    tool_manager_cls: object,
    server_config: dict[str, object],
) -> Any:
    if _is_vendor_miroflow_tool_manager(tool_manager_cls):
        return _PersistentStdioMcpToolManager([server_config])
    if not callable(tool_manager_cls):
        raise MiroThinkerAnalysisRuntimeError("ToolManager must be callable.")
    return tool_manager_cls([server_config])


def _is_vendor_miroflow_tool_manager(tool_manager_cls: object) -> bool:
    return (
        getattr(tool_manager_cls, "__module__", "") == "miroflow_tools.manager"
        and getattr(tool_manager_cls, "__name__", "") == "ToolManager"
    )


async def _close_analysis_tool_manager(tool_manager: object) -> None:
    close_tool_manager = getattr(tool_manager, "aclose", None)
    if callable(close_tool_manager):
        await close_tool_manager()


async def _run_supervised_strict_analysis_agent(
    *,
    config: MiroThinkerAnalysisRuntimeConfig,
    vendor_root: Path,
    workspace_root: Path,
    stage_root: Path,
    task_id: str,
    task_description: str,
    analysis_server_env: Mapping[str, str],
) -> StrictMiroThinkerAgentRunResult:
    supervision_root = stage_root / "supervision"
    supervision_root.mkdir(parents=True, exist_ok=True)
    input_path = supervision_root / "worker_input.json"
    output_path = supervision_root / "worker_output.json"
    payload = {
        "vendor_root": str(vendor_root),
        "workspace_root": str(workspace_root),
        "log_dir": str(config.log_dir),
        "llm_provider": config.llm_provider,
        "llm_model_name": config.llm_model_name,
        "llm_base_url": config.llm_base_url,
        "llm_api_key_env": "EVENT_TRADER_ANALYSIS_WORKER_LLM_API_KEY",
        "llm_max_context_length": config.llm_max_context_length,
        "llm_reasoning_effort": config.llm_reasoning_effort,
        "task_id": task_id,
        "task_description": task_description,
        "analysis_server_env": dict(analysis_server_env),
    }
    _atomic_write_json(input_path, payload)
    if output_path.exists():
        output_path.unlink()

    child_env = os.environ.copy()
    child_env["EVENT_TRADER_ANALYSIS_WORKER_LLM_API_KEY"] = config.llm_api_key
    cwd = Path.cwd()
    child_env["PYTHONPATH"] = _merge_pythonpath(
        os.environ.get("PYTHONPATH"),
        (str(cwd / "src"), str(cwd)),
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "event_trader.integrations.mirothinker_analysis_worker",
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        cwd=str(cwd),
        env=child_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if config.wall_clock_timeout_seconds == 0:
            stdout, stderr = await process.communicate()
        else:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=config.wall_clock_timeout_seconds,
            )
    except TimeoutError as exc:
        await _terminate_process_tree(process)
        stdout, stderr = await process.communicate()
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis vendor worker timed out after "
            f"{config.wall_clock_timeout_seconds} seconds "
            f"(task_id={task_id}, pid={process.pid}). "
            f"stdout={_decode_process_stream(stdout)} "
            f"stderr={_decode_process_stream(stderr)}"
        ) from exc

    result_payload = _load_supervision_output(output_path)
    worker_status = result_payload.get("status")
    if worker_status == "analysis_contract_repair_required":
        raise _analysis_contract_repair_from_supervision_payload(result_payload)
    if worker_status == "analysis_write_failure_budget_exceeded":
        worker_error = result_payload.get("error")
        raise _AnalysisWriteFailureBudgetExceeded(
            (worker_error.strip() if isinstance(worker_error, str) else "")
            or "Analysis write failure budget exceeded in supervised worker."
        )
    if process.returncode != 0 or result_payload.get("status") != "success":
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis vendor worker failed "
            f"(task_id={task_id}, returncode={process.returncode}, "
            f"status={result_payload.get('status')!r}, "
            f"error={result_payload.get('error')!r}, "
            f"stdout={_decode_process_stream(stdout)}, "
            f"stderr={_decode_process_stream(stderr)})."
        )
    run_result = result_payload.get("run_result")
    if not isinstance(run_result, dict):
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis vendor worker returned malformed run_result."
        )
    final_summary = run_result.get("final_summary")
    final_boxed_answer = run_result.get("final_boxed_answer")
    log_file_path = run_result.get("log_file_path")
    if not isinstance(final_summary, str):
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis vendor worker final_summary must be a string."
        )
    if not isinstance(final_boxed_answer, str):
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis vendor worker final_boxed_answer must be a string."
        )
    if not isinstance(log_file_path, str) or not log_file_path.strip():
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis vendor worker log_file_path must be non-blank."
        )
    return StrictMiroThinkerAgentRunResult(
        final_summary=final_summary,
        final_boxed_answer=final_boxed_answer,
        log_file_path=log_file_path,
        failure_experience_summary=run_result.get("failure_experience_summary"),
    )


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        suffix=".tmp",
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _load_supervision_output(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "status": "missing_output",
            "error": f"worker output file was not written: {path}",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "malformed_output",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        return {
            "status": "malformed_output",
            "error": "worker output payload must be a JSON object.",
        }
    return payload


def _decode_process_stream(value: bytes | None, *, max_chars: int = 4_000) -> str:
    if not value:
        return ""
    decoded = value.decode("utf-8", errors="replace").strip()
    if len(decoded) <= max_chars:
        return decoded
    return f"{decoded[:max_chars]}..."


def _merge_pythonpath(existing: str | None, required: Sequence[str]) -> str:
    parts = [item for item in required if item]
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: int = 10,
) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        taskkill = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await taskkill.wait()
    else:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()


def _install_analysis_write_failure_budget_guard(
    *,
    tool_manager: Any,
    consecutive_failure_budget: int = _ANALYSIS_WRITE_FAILURE_BUDGET,
    same_error_abort_threshold: int = _ANALYSIS_SAME_WRITE_ERROR_ABORT_THRESHOLD,
) -> None:
    original_execute_tool_call = getattr(tool_manager, "execute_tool_call", None)
    if not callable(original_execute_tool_call):
        raise MiroThinkerAnalysisRuntimeError(
            "Could not install analysis write failure budget guard; "
            "ToolManager execute_tool_call is unavailable."
        )

    failure_count = 0
    same_failure_count = 0
    same_failure_key: tuple[str, str, str, str] | None = None
    last_fail_tool = ""
    last_fail_error = ""

    def reset_failure_state() -> None:
        nonlocal failure_count, same_failure_count, same_failure_key
        failure_count = 0
        same_failure_count = 0
        same_failure_key = None

    async def guarded_execute_tool_call(
        *args: object,
        **kwargs: object,
    ) -> Any:
        nonlocal failure_count, same_failure_count, same_failure_key
        nonlocal last_fail_tool, last_fail_error
        server_name, tool_name, arguments = _normalize_tool_call_args(args, kwargs)
        tool_result = await original_execute_tool_call(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
        )
        if (
            server_name != ANALYSIS_MCP_SERVER_NAME
            or tool_name not in ANALYSIS_MATERIAL_WRITE_OPERATIONS
        ):
            return tool_result

        failure = parse_analysis_write_tool_failure(
            tool_name=tool_name,
            arguments=arguments,
            tool_result=tool_result,
        )
        if failure is None:
            reset_failure_state()
            return tool_result

        if not failure.recoverable:
            raise _AnalysisWriteFailureBudgetExceeded(
                "Non-recoverable analysis write tool failure: "
                f"tool={failure.tool_name} error_code={failure.error_code}: "
                f"{_shorten_error_text(failure.message, max_length=180)}"
            )

        failure_count += 1
        failure_key = analysis_write_failure_key(failure)
        if same_failure_key == failure_key:
            same_failure_count += 1
        else:
            same_failure_key = failure_key
            same_failure_count = 1
        last_fail_tool = tool_name
        last_fail_error = _shorten_error_text(failure.message, max_length=180)
        if failure.payload is not None and same_failure_count >= same_error_abort_threshold:
            raise _AnalysisContractRepairRequired(
                failure=analysis_contract_failure_for_write_tool(failure),
                failure_count=same_failure_count,
                threshold=same_error_abort_threshold,
            )
        if failure_count >= consecutive_failure_budget:
            raise _AnalysisWriteFailureBudgetExceeded(
                "Analysis write failure budget exceeded: "
                f"{failure_count} consecutive analysis write tool failures "
                f"(threshold={consecutive_failure_budget}). Last failure was "
                f"tool={last_fail_tool}: {last_fail_error}"
            )
        return tool_result

    tool_manager.execute_tool_call = guarded_execute_tool_call
    tool_manager.reset_analysis_write_failure_budget_guard = reset_failure_state


def _reset_analysis_write_failure_budget_guard(*, tool_manager: Any) -> None:
    reset = getattr(tool_manager, "reset_analysis_write_failure_budget_guard", None)
    if callable(reset):
        reset()


def _normalize_tool_call_args(
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


def _shorten_error_text(value: str, *, max_length: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 3]}..."


def _append_vendor_import_roots(vendor_root: Path) -> None:
    try:
        prepare_mirothinker_runtime(vendor_root)
    except MiroThinkerRuntimePathError as exc:
        raise MiroThinkerAnalysisRuntimeError(str(exc)) from exc


def _prepare_vendor_environment(config: MiroThinkerAnalysisRuntimeConfig) -> None:
    env_updates = {
        "OPENAI_API_KEY": config.llm_api_key,
        "OPENAI_BASE_URL": config.llm_base_url,
    }
    for key, value in env_updates.items():
        os.environ[key] = value


def _build_child_pythonpath(vendor_root: Path) -> str:
    project_src = Path(__file__).resolve(strict=False).parents[2]
    return build_mirothinker_child_pythonpath(
        vendor_root=vendor_root,
        project_src=project_src,
    )


_MIROTHINKER_USAGE_RE = re.compile(
    r"Total Input:\s*(?P<input>\d+),\s*"
    r"(?:(?:Cache Input|Cache Creation):\s*\d+,\s*)?"
    r"(?:Cache Read:\s*\d+,\s*)?"
    r"Output:\s*(?P<output>\d+)"
)


def _llm_usage_from_mirothinker_task_log(
    *,
    log_file_path: str,
    agent_role: Literal["analysis", "reflection"],
    provider: str,
    model: str,
) -> LLMUsageReceipt:
    path = Path(log_file_path)
    if not path.is_file():
        return LLMUsageReceipt.unavailable(
            agent_role=agent_role,
            provider=provider,
            model=model,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return LLMUsageReceipt.unavailable(
            agent_role=agent_role,
            provider=provider,
            model=model,
        )
    if not isinstance(payload, dict):
        return LLMUsageReceipt.unavailable(
            agent_role=agent_role,
            provider=provider,
            model=model,
        )
    for item in payload.get("step_logs", ()):
        if not isinstance(item, dict):
            continue
        step_name = item.get("step_name")
        message = item.get("message")
        if not isinstance(step_name, str) or "Usage Calculation" not in step_name:
            continue
        if not isinstance(message, str):
            continue
        match = _MIROTHINKER_USAGE_RE.search(message)
        if match is None:
            continue
        input_tokens = int(match.group("input"))
        output_tokens = int(match.group("output"))
        return LLMUsageReceipt(
            agent_role=agent_role,
            usage_source="provider_reported",
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )
    return LLMUsageReceipt.unavailable(
        agent_role=agent_role,
        provider=provider,
        model=model,
    )


def _validate_existing_dir(path: Path, *, field_name: str) -> Path:
    validated = _validate_path(path, field_name=field_name)
    if not validated.exists():
        raise MiroThinkerAnalysisRuntimeError(f"{field_name} does not exist: {validated}")
    if not validated.is_dir():
        raise MiroThinkerAnalysisRuntimeError(f"{field_name} must be a directory: {validated}")
    return validated


def _validate_path(path: Path, *, field_name: str) -> Path:
    if not isinstance(path, Path):
        raise MiroThinkerAnalysisRuntimeError(f"{field_name} must be a pathlib.Path.")
    return path.resolve(strict=False)


def _validate_non_blank_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MiroThinkerAnalysisRuntimeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MiroThinkerAnalysisRuntimeError(f"{field_name} must not be blank.")
    return normalized


def _require_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MiroThinkerAnalysisRuntimeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MiroThinkerAnalysisRuntimeError(f"{field_name} must not be blank.")
    return normalized


__all__ = [
    "MiroThinkerAnalysisRuntimeConfig",
    "MiroThinkerAnalysisRuntimeError",
    "build_mirothinker_analysis_runner",
]
