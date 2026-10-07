"""MiroThinker adapter for the project-owned PMReview agent runtime port."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from event_trader.integrations.mirothinker_llm_config import build_mirothinker_llm_config
from event_trader.integrations.mirothinker_runtime_paths import (
    MiroThinkerRuntimePathError,
    prepare_mirothinker_runtime,
)
from event_trader.integrations.strict_mirothinker_agent import (
    StrictMiroThinkerAgentContract,
    mirothinker_structured_output_fallback_texts_from_log,
    run_strict_mirothinker_agent,
)
from event_trader.pm_review.tools import (
    PM_REVIEW_TOOL_SERVER_NAME,
)
from event_trader.reasoning.effort import normalize_agent_reasoning_effort
from event_trader.reasoning.runtime import (
    AgentRunReceipt,
    AgentRuntime,
    AgentTask,
    AgentToolGateway,
)


class MiroThinkerPMReviewRuntimeError(RuntimeError):
    """Raised when the MiroThinker PMReview adapter cannot be trusted."""


@dataclass(frozen=True, slots=True)
class MiroThinkerPMReviewRuntimeConfig:
    """Explicit production inputs for one MiroThinker PMReview adapter."""

    vendor_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_max_context_length: int
    llm_reasoning_effort: str | None = None
    wall_clock_timeout_seconds: int = 0
    max_turns: int = 18

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "vendor_root",
            _validate_existing_dir(self.vendor_root, field_name="vendor_root"),
        )
        object.__setattr__(self, "log_dir", _validate_path(self.log_dir, "log_dir"))
        object.__setattr__(
            self,
            "llm_provider",
            _validate_non_blank_text(self.llm_provider, "llm_provider"),
        )
        object.__setattr__(
            self,
            "llm_model_name",
            _validate_non_blank_text(self.llm_model_name, "llm_model_name"),
        )
        object.__setattr__(
            self,
            "llm_api_key",
            _validate_non_blank_text(self.llm_api_key, "llm_api_key"),
        )
        object.__setattr__(
            self,
            "llm_base_url",
            _validate_non_blank_text(self.llm_base_url, "llm_base_url"),
        )
        if (
            not isinstance(self.llm_max_context_length, int)
            or isinstance(self.llm_max_context_length, bool)
            or self.llm_max_context_length <= 0
        ):
            raise MiroThinkerPMReviewRuntimeError(
                "llm_max_context_length must be a positive integer."
            )
        object.__setattr__(
            self,
            "llm_reasoning_effort",
            normalize_agent_reasoning_effort(
                self.llm_reasoning_effort,
                field_name="llm_reasoning_effort",
                error_type=MiroThinkerPMReviewRuntimeError,
            ),
        )
        if (
            not isinstance(self.wall_clock_timeout_seconds, int)
            or isinstance(self.wall_clock_timeout_seconds, bool)
            or self.wall_clock_timeout_seconds < 0
        ):
            raise MiroThinkerPMReviewRuntimeError(
                "wall_clock_timeout_seconds must be a non-negative integer."
            )
        if (
            not isinstance(self.max_turns, int)
            or isinstance(self.max_turns, bool)
            or self.max_turns <= 0
        ):
            raise MiroThinkerPMReviewRuntimeError("max_turns must be a positive integer.")


@dataclass(frozen=True, slots=True)
class _MiroThinkerPMReviewAgentRuntime:
    config: MiroThinkerPMReviewRuntimeConfig

    async def run_once(
        self,
        task: AgentTask,
        tools: AgentToolGateway,
    ) -> AgentRunReceipt:
        if task.role != "pm_review":
            raise MiroThinkerPMReviewRuntimeError(
                "MiroThinker PMReview runtime requires role='pm_review'."
            )
        return await _run_production_pm_review_task(
            config=self.config,
            task=task,
            tools=tools,
        )


def build_production_mirothinker_pm_review_task_runner(
    *,
    config: MiroThinkerPMReviewRuntimeConfig,
) -> AgentRuntime:
    """Build the MiroThinker implementation of the PMReview runtime port."""

    if not isinstance(config, MiroThinkerPMReviewRuntimeConfig):
        raise MiroThinkerPMReviewRuntimeError(
            "config must be a MiroThinkerPMReviewRuntimeConfig instance."
        )
    return _MiroThinkerPMReviewAgentRuntime(config=config)


async def _run_production_pm_review_task(
    *,
    config: MiroThinkerPMReviewRuntimeConfig,
    task: AgentTask,
    tools: AgentToolGateway,
) -> AgentRunReceipt:
    execute_task_pipeline, OutputFormatter, OmegaConf = _load_vendor_runtime(config.vendor_root)
    _prepare_vendor_environment(config)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    agent_cfg = OmegaConf.create(
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
                reasoning_effort=config.llm_reasoning_effort,
            ),
            "agent": {
                "main_agent": {
                    "tools": list(task.required_tool_names),
                    "tool_blacklist": [],
                    "max_turns": config.max_turns,
                },
                "sub_agents": {},
                "keep_tool_result": True,
                "context_compress_limit": 0,
            },
            "benchmark": {},
        }
    )
    tool_manager = _PMReviewToolboxToolManager(tools)
    contract = StrictMiroThinkerAgentContract(
        agent_name="MiroThinker PMReview agent",
        required_tools=(),
    )
    run_coro = run_strict_mirothinker_agent(
        contract=contract,
        tool_manager=tool_manager,
        execute_task_pipeline=execute_task_pipeline,
        cfg=agent_cfg,
        task_id=task.task_id,
        task_description=task.task_description,
        task_file_name="",
        sub_agent_tool_managers={},
        output_formatter=OutputFormatter(),
        log_dir=config.log_dir,
        error_factory=MiroThinkerPMReviewRuntimeError,
    )
    try:
        run_result = (
            await asyncio.wait_for(run_coro, timeout=config.wall_clock_timeout_seconds)
            if config.wall_clock_timeout_seconds
            else await run_coro
        )
    except TimeoutError as exc:
        raise MiroThinkerPMReviewRuntimeError(
            "MiroThinker PMReview runner timed out after "
            f"{config.wall_clock_timeout_seconds} seconds."
        ) from exc
    return AgentRunReceipt(
        final_summary=run_result.final_summary,
        contract_output_text=run_result.final_boxed_answer,
        diagnostic_log_ref=run_result.log_file_path,
        diagnostics={"failure_experience_summary": run_result.failure_experience_summary},
        fallback_payload_texts=mirothinker_structured_output_fallback_texts_from_log(
            run_result.log_file_path
        ),
    )


class _PMReviewToolboxToolManager:
    """MiroThinker ToolManager adapter over the PMReview tool gateway."""

    def __init__(self, tools: AgentToolGateway) -> None:
        self._tools = tools
        self._task_log: object | None = None

    def set_task_log(self, task_log: object) -> None:
        self._task_log = task_log

    async def get_all_tool_definitions(self) -> list[dict[str, object]]:
        return [_pm_review_tool_server_definition()]

    async def execute_tool_call(
        self,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if server_name != self._tools.server_name:
            return {"error": f"PMReview tool server is not available: {server_name}"}
        if tool_name not in self._tools.available_tool_names:
            return {"error": f"PMReview tool is not available: {tool_name}"}
        args = dict(arguments or {})
        try:
            result = await self._tools.call_tool(tool_name, args)
        except Exception as exc:
            return {
                "error": f"PMReview tool call failed: {exc}",
                "server_name": server_name,
                "tool_name": tool_name,
            }
        return {
            "result": json.dumps(_to_jsonable(result), ensure_ascii=False, sort_keys=True),
            "server_name": server_name,
            "tool_name": tool_name,
        }


def _pm_review_tool_server_definition() -> dict[str, object]:
    return {
        "name": PM_REVIEW_TOOL_SERVER_NAME,
        "tools": [
            {
                "name": "read_pm_review_request",
                "description": "Read the PMReviewRequest bound to this PMReview session.",
                "schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "read_pm_review_evidence",
                "description": "Read request-visible evidence only.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "event_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "read_pm_review_market_bars",
                "description": "Read market bars bounded by request visibility.",
                "schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "read_pm_review_history",
                "description": "Read prior PM lifecycle records only.",
                "schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "read_pm_episode_memory",
                "description": "Read PM-facing episode memory as of request business time.",
                "schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "read_pm_review_learning_cards",
                "description": (
                    "Read structured pm_review learning cards selected by scope and business time."
                ),
                "schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "read_pm_review_active_exposure",
                "description": "Read resolved active exposure for this PMReview request.",
                "schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ],
    }


def _to_jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_to_jsonable(item) for item in value]
    if hasattr(value, "to_json_payload"):
        return _to_jsonable(value.to_json_payload())
    if is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(asdict(cast(Any, value)))
    return value


def _load_vendor_runtime(vendor_root: Path) -> tuple[Any, Any, Any]:
    try:
        prepare_mirothinker_runtime(vendor_root)
        pipeline_module = __import__("src.core.pipeline", fromlist=["execute_task_pipeline"])
        output_formatter_module = __import__(
            "src.io.output_formatter",
            fromlist=["OutputFormatter"],
        )
        omegaconf_module = __import__("omegaconf", fromlist=["OmegaConf"])
    except (ImportError, MiroThinkerRuntimePathError) as exc:
        raise MiroThinkerPMReviewRuntimeError(
            "MiroThinker PMReview runtime dependencies are unavailable. "
            "Install/prepare the vendored MiroThinker runtime and Python "
            f"dependencies before using production PMReview. Missing: {exc}."
        ) from exc
    return (
        pipeline_module.execute_task_pipeline,
        output_formatter_module.OutputFormatter,
        omegaconf_module.OmegaConf,
    )


def _prepare_vendor_environment(config: MiroThinkerPMReviewRuntimeConfig) -> None:
    os.environ["OPENAI_API_KEY"] = config.llm_api_key
    os.environ["OPENAI_BASE_URL"] = config.llm_base_url


def _validate_existing_dir(value: Path, *, field_name: str) -> Path:
    path = _validate_path(value, field_name)
    if not path.exists() or not path.is_dir():
        raise MiroThinkerPMReviewRuntimeError(f"{field_name} must be an existing directory.")
    return path


def _validate_path(value: Path, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise MiroThinkerPMReviewRuntimeError(f"{field_name} must be a Path.")
    return value.expanduser().resolve(strict=False)


def _validate_non_blank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MiroThinkerPMReviewRuntimeError(f"{field_name} must be non-blank.")
    return value.strip()


__all__ = [
    "MiroThinkerPMReviewRuntimeConfig",
    "MiroThinkerPMReviewRuntimeError",
    "build_production_mirothinker_pm_review_task_runner",
]
