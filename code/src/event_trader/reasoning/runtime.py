"""Minimal provider-neutral port for one agent execution attempt."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol


class AgentRuntimeContractError(RuntimeError):
    """Raised when an agent runtime violates the project-owned execution port."""


@dataclass(frozen=True, slots=True)
class AgentTask:
    """Provider-neutral inputs for exactly one agent attempt."""

    role: str
    task_id: str
    task_description: str
    required_tool_names: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("role", "task_id", "task_description"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise AgentRuntimeContractError(f"agent task {field_name} must be non-blank.")
        if not isinstance(self.required_tool_names, tuple) or any(
            not isinstance(tool_name, str) or not tool_name.strip()
            for tool_name in self.required_tool_names
        ):
            raise AgentRuntimeContractError(
                "agent task required_tool_names must be a tuple of non-blank strings."
            )
        if len(set(self.required_tool_names)) != len(self.required_tool_names):
            raise AgentRuntimeContractError(
                "agent task required_tool_names must not contain duplicates."
            )


@dataclass(frozen=True, slots=True)
class AgentRunReceipt:
    """Provider-neutral output consumed by project-owned contract validation."""

    final_summary: str
    contract_output_text: str
    diagnostic_log_ref: str = ""
    diagnostics: Mapping[str, object] = field(default_factory=dict)
    fallback_payload_texts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.final_summary, str):
            raise AgentRuntimeContractError("agent runtime final_summary must be a string.")
        if not isinstance(self.contract_output_text, str):
            raise AgentRuntimeContractError("agent runtime contract_output_text must be a string.")
        if not isinstance(self.diagnostic_log_ref, str):
            raise AgentRuntimeContractError("agent runtime diagnostic_log_ref must be a string.")
        if not isinstance(self.diagnostics, Mapping):
            raise AgentRuntimeContractError("agent runtime diagnostics must be a mapping.")
        if not isinstance(self.fallback_payload_texts, tuple) or any(
            not isinstance(value, str) for value in self.fallback_payload_texts
        ):
            raise AgentRuntimeContractError(
                "agent runtime fallback_payload_texts must be a tuple of strings."
            )
        object.__setattr__(self, "diagnostics", MappingProxyType(dict(self.diagnostics)))


class AgentToolGateway(Protocol):
    """Project-owned tool surface exposed to one provider adapter."""

    @property
    def server_name(self) -> str: ...

    @property
    def available_tool_names(self) -> tuple[str, ...]: ...

    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> object: ...


class AgentRuntime(Protocol):
    """Replaceable provider adapter for one bounded inference attempt."""

    async def run_once(
        self,
        task: AgentTask,
        tools: AgentToolGateway,
    ) -> AgentRunReceipt: ...


def run_agent_runtime_once_sync(
    *,
    runtime: AgentRuntime,
    task: AgentTask,
    tools: AgentToolGateway,
) -> AgentRunReceipt:
    """Bridge the async runtime port into an existing synchronous supervisor."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise AgentRuntimeContractError(
            "synchronous agent runtime bridge cannot run inside an active event loop."
        )
    return asyncio.run(execute_agent_runtime_once(runtime=runtime, task=task, tools=tools))


async def execute_agent_runtime_once(
    *,
    runtime: AgentRuntime,
    task: AgentTask,
    tools: AgentToolGateway,
) -> AgentRunReceipt:
    """Preflight one adapter call and reject noncanonical runtime output."""

    missing_tools = tuple(
        tool_name
        for tool_name in task.required_tool_names
        if tool_name not in tools.available_tool_names
    )
    if missing_tools:
        raise AgentRuntimeContractError(
            f"agent runtime tool preflight failed; missing tool(s): {', '.join(missing_tools)}."
        )
    result = await runtime.run_once(task, tools)
    if not isinstance(result, AgentRunReceipt):
        raise AgentRuntimeContractError("agent runtime must return an AgentRunReceipt.")
    return result


__all__ = [
    "AgentRunReceipt",
    "AgentRuntime",
    "AgentRuntimeContractError",
    "AgentTask",
    "AgentToolGateway",
    "execute_agent_runtime_once",
    "run_agent_runtime_once_sync",
]
