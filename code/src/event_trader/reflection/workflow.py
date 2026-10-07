"""Bounded project-controlled workflow for Reflection attempts."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from event_trader.reasoning.anthropic_structured import (
    AnthropicStructuredInference,
    AnthropicStructuredInferenceConfig,
    structured_inference_session,
)
from event_trader.reasoning.runtime import (
    AgentRunReceipt,
    AgentTask,
    AgentToolGateway,
)
from event_trader.reasoning.structured_inference import (
    StructuredInference,
    StructuredInferenceError,
    StructuredInferenceResult,
)
from event_trader.reasoning.token_budget import (
    StructuredPromptBudgetError,
    assert_structured_prompt_budget,
    validate_structured_token_budget,
)
from event_trader.reflection.agent import (
    ReflectionContractFailure,
    ReflectionContractRepairExhausted,
    append_reflection_contract_repair_feedback,
    classify_reflection_contract_failure,
    reflection_read_audit_failure,
    run_reflection_agent_with_contract_repair,
)
from event_trader.reflection.agent_contract import (
    REFLECTION_AGENT_ROLE,
    REFLECTION_TOOL_SERVER_NAME,
    REQUIRED_REFLECTION_TOOL_NAMES,
    ReflectionTaskKind,
)
from event_trader.reflection.output import (
    reflection_evaluation_schema,
    reflection_learning_decision_schema,
)
from event_trader.reflection.supervisor import (
    ReflectionAttemptBinding,
    ReflectionAttemptFactory,
    ReflectionAttemptScope,
)
from event_trader.reflection.tools import (
    REFLECTION_TOOL_SPECS,
    ReflectionAgentToolGateway,
    ReflectionToolGateway,
    ReflectionToolSpec,
)
from event_trader.storage import WorkspaceLayout, validate_workspace_layout

_READ_PLAN_TOOL_NAME = "reflection_read_plan"
_MAX_READ_ROUNDS = 2
_MAX_TOOL_CALLS_PER_ROUND = len(REFLECTION_TOOL_SPECS)
_READ_PLAN_CONTRACT_MAX_ATTEMPTS = 3
_TEMPERATURE = 0.1
_TRANSPORT_MAX_ATTEMPTS = 5
_TRANSPORT_RETRY_SECONDS = 10
_STRUCTURED_PROTOCOL_MAX_ATTEMPTS = 3


class WorkflowReflectionRuntimeError(RuntimeError):
    """Raised when one bounded Reflection workflow attempt cannot be trusted."""


@dataclass(frozen=True, slots=True)
class WorkflowReflectionRuntimeConfig:
    """Explicit provider and diagnostic settings for the Reflection workflow."""

    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_max_context_length: int
    llm_max_output_tokens: int
    wall_clock_timeout_seconds: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "log_dir", _validate_path(self.log_dir, "log_dir"))
        object.__setattr__(
            self,
            "llm_provider",
            _validate_non_blank_text(self.llm_provider, "llm_provider").lower(),
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
        if self.llm_provider != "anthropic":
            raise WorkflowReflectionRuntimeError(
                "workflow Reflection currently requires llm_provider='anthropic'."
            )
        try:
            validate_structured_token_budget(
                max_context_tokens=self.llm_max_context_length,
                max_output_tokens=self.llm_max_output_tokens,
                field_prefix="WorkflowReflectionRuntimeConfig",
            )
        except StructuredPromptBudgetError as exc:
            raise WorkflowReflectionRuntimeError(str(exc)) from exc
        if (
            not isinstance(self.wall_clock_timeout_seconds, int)
            or isinstance(self.wall_clock_timeout_seconds, bool)
            or self.wall_clock_timeout_seconds < 0
        ):
            raise WorkflowReflectionRuntimeError(
                "wall_clock_timeout_seconds must be a non-negative integer."
            )


class _AnthropicWorkflowReflectionProvider:
    def __init__(
        self,
        config: WorkflowReflectionRuntimeConfig,
        *,
        client: object | None = None,
        provider: StructuredInference | None = None,
    ) -> None:
        self._provider = provider or AnthropicStructuredInference(
            AnthropicStructuredInferenceConfig(
                model_name=config.llm_model_name,
                api_key=config.llm_api_key,
                base_url=config.llm_base_url,
                max_output_tokens=config.llm_max_output_tokens,
                temperature=_TEMPERATURE,
                transport_max_attempts=_TRANSPORT_MAX_ATTEMPTS,
                transport_retry_seconds=_TRANSPORT_RETRY_SECONDS,
                protocol_max_attempts=_STRUCTURED_PROTOCOL_MAX_ATTEMPTS,
                enable_tool_schema_cache=True,
            ),
            client=client,
        )
        self._config = config

    @asynccontextmanager
    async def session(self) -> Any:
        session = getattr(self._provider, "session", None)
        if not callable(session):
            yield self
            return
        async with session() as attempt_provider:
            yield _AnthropicWorkflowReflectionProvider(
                self._config,
                provider=attempt_provider,
            )

    async def generate_structured(
        self,
        *,
        prompt: str,
        session_id: str,
        tool_name: str,
        tool_description: str,
        input_schema: Mapping[str, object],
    ) -> StructuredInferenceResult:
        try:
            return await self._provider.generate_structured(
                prompt=prompt,
                session_id=session_id,
                tool_name=tool_name,
                tool_description=tool_description,
                input_schema=input_schema,
            )
        except StructuredInferenceError as exc:
            raise WorkflowReflectionRuntimeError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class _PlannedToolCall:
    tool_name: str
    arguments: Mapping[str, object]

    @property
    def fingerprint(self) -> str:
        arguments_json = json.dumps(
            dict(self.arguments),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"{self.tool_name}:{arguments_json}"


@dataclass(frozen=True, slots=True)
class _WorkflowReflectionAgentRuntime:
    config: WorkflowReflectionRuntimeConfig
    task_kind: ReflectionTaskKind
    provider: StructuredInference
    target_key: str | None = None

    def __post_init__(self) -> None:
        if self.task_kind != "shared" and (
            not isinstance(self.target_key, str) or not self.target_key.strip()
        ):
            raise WorkflowReflectionRuntimeError(
                f"workflow Reflection task_kind={self.task_kind!r} requires a target_key."
            )

    async def run_once(
        self,
        task: AgentTask,
        tools: AgentToolGateway,
    ) -> AgentRunReceipt:
        _validate_runtime_contract(task=task, tools=tools)
        trace = _new_workflow_trace(
            config=self.config,
            task_kind=self.task_kind,
            task=task,
        )
        try:
            async with structured_inference_session(self.provider) as attempt_provider:
                run = _execute_workflow_attempt(
                    config=self.config,
                    task_kind=self.task_kind,
                    provider=attempt_provider,
                    task=task,
                    target_key=self.target_key,
                    tools=tools,
                    trace=trace,
                )
                result = (
                    await asyncio.wait_for(
                        run,
                        timeout=self.config.wall_clock_timeout_seconds,
                    )
                    if self.config.wall_clock_timeout_seconds
                    else await run
                )
        except TimeoutError as exc:
            trace["status"] = "failed"
            trace["error"] = (
                "workflow Reflection timed out after "
                f"{self.config.wall_clock_timeout_seconds} seconds."
            )
            _write_workflow_trace(log_dir=self.config.log_dir, task=task, trace=trace)
            raise WorkflowReflectionRuntimeError(str(trace["error"])) from exc
        except Exception as exc:
            trace["status"] = "failed"
            trace["error_type"] = type(exc).__name__
            trace["error"] = str(exc).strip() or repr(exc)
            _write_workflow_trace(log_dir=self.config.log_dir, task=task, trace=trace)
            if isinstance(exc, WorkflowReflectionRuntimeError):
                raise
            raise WorkflowReflectionRuntimeError(str(trace["error"])) from exc
        trace["status"] = "succeeded"
        log_path = _write_workflow_trace(
            log_dir=self.config.log_dir,
            task=task,
            trace=trace,
        )
        return AgentRunReceipt(
            final_summary=result.final_summary,
            contract_output_text=result.contract_output_text,
            diagnostic_log_ref=str(log_path),
            diagnostics=result.diagnostics,
        )

    async def run_with_decision_repair(
        self,
        *,
        task: AgentTask,
        tools: AgentToolGateway,
        evaluate_run_result: Callable[[AgentRunReceipt], object],
    ) -> object:
        try:
            async with structured_inference_session(self.provider) as attempt_provider:
                run = self._run_with_decision_repair_unbounded(
                    task=task,
                    tools=tools,
                    provider=attempt_provider,
                    evaluate_run_result=evaluate_run_result,
                )
                return (
                    await asyncio.wait_for(
                        run,
                        timeout=self.config.wall_clock_timeout_seconds,
                    )
                    if self.config.wall_clock_timeout_seconds
                    else await run
                )
        except TimeoutError as exc:
            raise WorkflowReflectionRuntimeError(
                "workflow Reflection timed out after "
                f"{self.config.wall_clock_timeout_seconds} seconds."
            ) from exc

    async def _run_with_decision_repair_unbounded(
        self,
        *,
        task: AgentTask,
        tools: AgentToolGateway,
        provider: StructuredInference,
        evaluate_run_result: Callable[[AgentRunReceipt], object],
    ) -> object:
        _validate_runtime_contract(task=task, tools=tools)
        trace = _new_workflow_trace(config=self.config, task_kind=self.task_kind, task=task)
        try:
            frozen_reads = await _execute_reflection_reads(
                config=self.config,
                task_kind=self.task_kind,
                provider=provider,
                task=task,
                tools=tools,
                trace=trace,
            )
            frozen_read_receipt = AgentRunReceipt(
                final_summary="",
                contract_output_text="",
                diagnostics={
                    "attempted_tool_names": frozen_reads.attempted_tool_names,
                    "successful_tool_names": frozen_reads.successful_tool_names,
                    "tool_failure_details": frozen_reads.failure_details,
                },
            )
            read_failure = reflection_read_audit_failure(frozen_read_receipt)
            if read_failure is not None:
                raise ReflectionContractRepairExhausted(
                    read_failure,
                    attempt_count=1,
                    max_attempts=1,
                )

            async def run_decision(current_task_description: str) -> AgentRunReceipt:
                decision_task = AgentTask(
                    role=task.role,
                    task_id=task.task_id,
                    task_description=current_task_description,
                    required_tool_names=task.required_tool_names,
                )
                result = await _execute_reflection_decision(
                    config=self.config,
                    task_kind=self.task_kind,
                    provider=provider,
                    task=decision_task,
                    target_key=self.target_key,
                    frozen_reads=frozen_reads,
                    trace=trace,
                )
                return AgentRunReceipt(
                    final_summary=result.final_summary,
                    contract_output_text=result.contract_output_text,
                    diagnostics=result.diagnostics,
                )

            failure_ledger: list[ReflectionContractFailure] = []

            def append_cumulative_feedback(
                original_task_description: str,
                failure: ReflectionContractFailure,
            ) -> str:
                failure_ledger.append(failure)
                entries = "\n\n".join(
                    append_reflection_contract_repair_feedback(
                        "",
                        prior,
                        reads_already_satisfied=True,
                    ).strip()
                    for prior in failure_ledger
                )
                return (
                    f"{original_task_description.rstrip()}\n\n"
                    "Cumulative deterministic event-trader Reflection decision "
                    "repair ledger:\n"
                    f"{entries}"
                )

            def evaluate_decision(receipt: AgentRunReceipt) -> object:
                try:
                    evaluated = evaluate_run_result(receipt)
                except Exception as exc:
                    failure = classify_reflection_contract_failure(exc)
                    if failure is not None:
                        _finish_decision_trace_receipt(
                            trace,
                            status="rejected",
                            failure=failure,
                        )
                    raise
                _finish_decision_trace_receipt(trace, status="accepted")
                return evaluated

            result = await run_reflection_agent_with_contract_repair(
                task_description=task.task_description,
                run_attempt=run_decision,
                evaluate_run_result=evaluate_decision,
                classify_failure=classify_reflection_contract_failure,
                append_repair_feedback=append_cumulative_feedback,
            )
        except Exception as exc:
            trace["status"] = "failed"
            trace["error_type"] = type(exc).__name__
            trace["error"] = str(exc).strip() or repr(exc)
            _write_workflow_trace(log_dir=self.config.log_dir, task=task, trace=trace)
            raise
        trace["status"] = "succeeded"
        _write_workflow_trace(log_dir=self.config.log_dir, task=task, trace=trace)
        return result


@dataclass(frozen=True, slots=True)
class _WorkflowAttemptResult:
    final_summary: str
    contract_output_text: str
    diagnostics: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _FrozenReflectionReads:
    tool_results: tuple[dict[str, object], ...]
    attempted_tool_names: tuple[str, ...]
    successful_tool_names: tuple[str, ...]
    failure_details: tuple[str, ...]
    read_plan_metadata: tuple[dict[str, object], ...]


def _new_workflow_trace(
    *,
    config: WorkflowReflectionRuntimeConfig,
    task_kind: ReflectionTaskKind,
    task: AgentTask,
) -> dict[str, object]:
    return {
        "implementation": "event_trader",
        "task_kind": task_kind,
        "task_id": task.task_id,
        "role": task.role,
        "task_description": task.task_description,
        "required_tool_names": list(task.required_tool_names),
        "provider": config.llm_provider,
        "model": config.llm_model_name,
        "status": "running",
    }


def _finish_decision_trace_receipt(
    trace: dict[str, object],
    *,
    status: str,
    failure: ReflectionContractFailure | None = None,
) -> None:
    decision_attempts = trace.get("decision_attempts")
    if not isinstance(decision_attempts, list) or not decision_attempts:
        return
    current = decision_attempts[-1]
    if not isinstance(current, dict):
        return
    current["status"] = status
    if failure is not None:
        current["failure"] = {
            "surface": failure.surface,
            "error_code": failure.error_code,
            "message": failure.message,
            "recoverable": failure.recoverable,
            "repair_paths": [list(path) for path in failure.repair_plan.paths],
        }


async def _execute_workflow_attempt(
    *,
    config: WorkflowReflectionRuntimeConfig,
    task_kind: ReflectionTaskKind,
    provider: StructuredInference,
    task: AgentTask,
    target_key: str | None,
    tools: AgentToolGateway,
    trace: dict[str, object],
) -> _WorkflowAttemptResult:
    frozen_reads = await _execute_reflection_reads(
        config=config,
        task_kind=task_kind,
        provider=provider,
        task=task,
        tools=tools,
        trace=trace,
    )
    return await _execute_reflection_decision(
        config=config,
        task_kind=task_kind,
        provider=provider,
        task=task,
        target_key=target_key,
        frozen_reads=frozen_reads,
        trace=trace,
    )


async def _execute_reflection_reads(
    *,
    config: WorkflowReflectionRuntimeConfig,
    task_kind: ReflectionTaskKind,
    provider: StructuredInference,
    task: AgentTask,
    tools: AgentToolGateway,
    trace: dict[str, object],
) -> _FrozenReflectionReads:
    tool_results: list[dict[str, object]] = []
    attempted_tool_names: list[str] = []
    successful_tool_names: list[str] = []
    failure_details: list[str] = []
    prior_call_fingerprints: set[str] = set()
    read_plan_metadata: list[dict[str, object]] = []
    trace["read_plans"] = read_plan_metadata
    trace["tool_results"] = tool_results
    trace["attempted_tool_names"] = attempted_tool_names
    trace["successful_tool_names"] = successful_tool_names
    trace["tool_failure_details"] = failure_details
    trace["read_execution_mode"] = "planned"

    async def execute_calls(
        planned_calls: tuple[_PlannedToolCall, ...],
        *,
        round_number: int,
    ) -> None:
        for planned_call in planned_calls:
            prior_call_fingerprints.add(planned_call.fingerprint)
            attempted_tool_names.append(planned_call.tool_name)
            recorded_result: dict[str, object] = {
                "round": round_number,
                "tool_name": planned_call.tool_name,
                "arguments": dict(planned_call.arguments),
            }
            try:
                raw_result = await tools.call_tool(
                    planned_call.tool_name,
                    planned_call.arguments,
                )
            except Exception as exc:
                failure = (
                    f"{planned_call.tool_name}: {type(exc).__name__}: "
                    f"{str(exc).strip() or repr(exc)}"
                )
                failure_details.append(failure)
                recorded_result.update(
                    {
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc).strip() or repr(exc),
                    }
                )
            else:
                successful_tool_names.append(planned_call.tool_name)
                recorded_result.update(
                    {
                        "status": "succeeded",
                        "result_sha256": _tool_result_sha256(raw_result),
                        "result": _normalize_tool_result(raw_result),
                    }
                )
            tool_results.append(recorded_result)
    for round_number in range(1, _MAX_READ_ROUNDS + 1):
        plan_prompt = _build_read_plan_prompt(
            task=task,
            task_kind=task_kind,
            round_number=round_number,
            prior_tool_results=tool_results,
        )
        _assert_prompt_budget(
            prompt=plan_prompt,
            config=config,
            stage=f"read-plan round {round_number}",
        )
        planned_calls, plan_metadata = await _generate_read_plan(
            provider=provider,
            prompt=plan_prompt,
            session_id=task.task_id,
            allowed_tool_names=task.required_tool_names,
            require_read=round_number == 1,
            prior_call_fingerprints=prior_call_fingerprints,
        )
        read_plan_metadata.append(
            {
                "round": round_number,
                **plan_metadata,
            }
        )
        await execute_calls(planned_calls, round_number=round_number)

    return _FrozenReflectionReads(
        tool_results=tuple(tool_results),
        attempted_tool_names=tuple(attempted_tool_names),
        successful_tool_names=tuple(successful_tool_names),
        failure_details=tuple(failure_details),
        read_plan_metadata=tuple(read_plan_metadata),
    )


async def _execute_reflection_decision(
    *,
    config: WorkflowReflectionRuntimeConfig,
    task_kind: ReflectionTaskKind,
    provider: StructuredInference,
    task: AgentTask,
    target_key: str | None,
    frozen_reads: _FrozenReflectionReads,
    trace: dict[str, object],
) -> _WorkflowAttemptResult:
    tool_results = list(frozen_reads.tool_results)
    decision_attempts = trace.setdefault("decision_attempts", [])
    if not isinstance(decision_attempts, list):
        raise WorkflowReflectionRuntimeError("Reflection decision trace ledger is invalid.")
    decision_attempt_number = len(decision_attempts) + 1
    decision_receipt: dict[str, object] = {
        "attempt": decision_attempt_number,
        "status": "running",
    }
    decision_attempts.append(decision_receipt)

    owner_context_prompt = _build_reflection_owner_context_prompt(
        task_description=task.task_description,
        task_kind=task_kind,
        tool_results=tool_results,
    )
    evaluation_prompt = _build_reflection_evaluation_prompt(
        owner_context_prompt=owner_context_prompt,
        task_kind=task_kind,
    )
    _assert_prompt_budget(prompt=evaluation_prompt, config=config, stage="final evaluation")
    evaluation_tool_name = (
        "reflection_shared_final" if task_kind == "shared" else f"reflection_{task_kind}_evaluation"
    )
    evaluation_result = await provider.generate_structured(
        prompt=evaluation_prompt,
        session_id=task.task_id,
        tool_name=evaluation_tool_name,
        tool_description=(
            "Return the complete shared Reflection business result."
            if task_kind == "shared"
            else "Return the complete Reflection evaluation before learning synthesis."
        ),
        input_schema=reflection_evaluation_schema(task_kind),
    )
    initial_evaluation_payload = dict(evaluation_result.payload)
    initial_payload_text = json.dumps(
        initial_evaluation_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    trace.update(
        {
            "initial_evaluation_provider_metadata": dict(evaluation_result.metadata),
            "initial_evaluation_payload": initial_evaluation_payload,
        }
    )
    # The owner evaluation is the single business-owned draft. Deterministic
    # Reflection contracts below remain responsible for validation and bounded
    # repair; a second LLM audit/revision stage has no independent fact source.
    evaluation_payload = initial_evaluation_payload
    accepted_evaluation_metadata = dict(evaluation_result.metadata)
    evaluation_payload_text = json.dumps(
        evaluation_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    evaluation_sha256 = sha256(evaluation_payload_text.encode("utf-8")).hexdigest()
    trace.update(
        {
            "evaluation_provider_metadata": accepted_evaluation_metadata,
            "evaluation_payload": evaluation_payload,
            "evaluation_payload_sha256": evaluation_sha256,
        }
    )
    learning_result: StructuredInferenceResult | None = None
    final_payload = evaluation_payload
    if task_kind != "shared":
        learning_prompt = _build_reflection_learning_prompt(
            owner_context_prompt=owner_context_prompt,
            frozen_evaluation=evaluation_payload,
            frozen_evaluation_sha256=evaluation_sha256,
        )
        _assert_prompt_budget(
            prompt=learning_prompt,
            config=config,
            stage="learning decision",
        )
        learning_result = await provider.generate_structured(
            prompt=learning_prompt,
            session_id=task.task_id,
            tool_name=f"reflection_{task_kind}_learning_decision",
            tool_description=(
                "Return one complete learning decision derived from the frozen "
                "Reflection evaluation."
            ),
            input_schema=reflection_learning_decision_schema(target_key=target_key),
        )
        learning_payload = dict(learning_result.payload)
        final_payload = {
            **evaluation_payload,
            "learning_decision": learning_payload["learning_decision"],
        }
    payload_text = json.dumps(
        final_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    final_provider_metadata: dict[str, object] = {
        "evaluation": accepted_evaluation_metadata,
    }
    if learning_result is not None:
        final_provider_metadata["learning_decision"] = dict(learning_result.metadata)
    decision_receipt.update(
        {
            "status": "generated",
            "initial_payload_sha256": sha256(
                initial_payload_text.encode("utf-8")
            ).hexdigest(),
            "final_payload_sha256": sha256(payload_text.encode("utf-8")).hexdigest(),
            "provider_metadata": final_provider_metadata,
        }
    )
    trace.update(
        {
            "learning_provider_metadata": (
                None if learning_result is None else dict(learning_result.metadata)
            ),
            "final_provider_metadata": final_provider_metadata,
            "final_payload": final_payload,
        }
    )
    return _WorkflowAttemptResult(
        final_summary=payload_text,
        contract_output_text=f"\\boxed{{{payload_text}}}",
        diagnostics={
            "implementation": "event_trader",
            "task_kind": task_kind,
            "provider": config.llm_provider,
            "model": config.llm_model_name,
            "attempted_tool_names": frozen_reads.attempted_tool_names,
            "successful_tool_names": frozen_reads.successful_tool_names,
            "tool_failure_details": frozen_reads.failure_details,
            "read_execution_mode": "planned",
            "read_calls": tuple(
                {
                    "round": result["round"],
                    "tool_name": result["tool_name"],
                    "arguments": result["arguments"],
                    "status": result["status"],
                    **(
                        {"result_sha256": result["result_sha256"]}
                        if "result_sha256" in result
                        else {}
                    ),
                }
                for result in tool_results
            ),
            "read_plan_provider_metadata": frozen_reads.read_plan_metadata,
            "evaluation_provider_metadata": accepted_evaluation_metadata,
            "evaluation_payload_sha256": evaluation_sha256,
            "learning_provider_metadata": (
                None if learning_result is None else dict(learning_result.metadata)
            ),
            "final_provider_metadata": final_provider_metadata,
        },
    )


def build_workflow_reflection_attempt_factory(
    *,
    config: WorkflowReflectionRuntimeConfig,
    layout: WorkspaceLayout,
) -> ReflectionAttemptFactory:
    """Bind the bounded workflow implementation to project-owned scopes."""

    if not isinstance(config, WorkflowReflectionRuntimeConfig):
        raise WorkflowReflectionRuntimeError(
            "config must be a WorkflowReflectionRuntimeConfig instance."
        )
    if not isinstance(layout, WorkspaceLayout):
        raise WorkflowReflectionRuntimeError("layout must be a WorkspaceLayout instance.")
    validate_workspace_layout(layout)
    provider = _AnthropicWorkflowReflectionProvider(config)

    def build(scope: ReflectionAttemptScope) -> ReflectionAttemptBinding:
        runtime = _WorkflowReflectionAgentRuntime(
            config=config,
            task_kind=scope.task_kind,
            provider=provider,
            target_key=scope.target_key,
        )
        return ReflectionAttemptBinding(
            task_id=scope.logical_task_id,
            runtime=runtime,
            tools=ReflectionAgentToolGateway(
                ReflectionToolGateway(
                    layout=layout,
                    configured_target_key=scope.target_key,
                    fixed_window=(scope.window_start, scope.window_end),
                )
            ),
            repair_ownership="workflow_decision",
            decision_repair_runner=runtime.run_with_decision_repair,
        )

    return build


def _validate_runtime_contract(*, task: AgentTask, tools: AgentToolGateway) -> None:
    if task.role != REFLECTION_AGENT_ROLE:
        raise WorkflowReflectionRuntimeError(
            f"workflow Reflection runtime requires role={REFLECTION_AGENT_ROLE!r}."
        )
    if task.required_tool_names != REQUIRED_REFLECTION_TOOL_NAMES:
        raise WorkflowReflectionRuntimeError(
            "workflow Reflection runtime requires the canonical Reflection tool contract."
        )
    if tools.server_name != REFLECTION_TOOL_SERVER_NAME:
        raise WorkflowReflectionRuntimeError(
            "workflow Reflection runtime received a noncanonical tool gateway."
        )


def _build_read_plan_prompt(
    *,
    task: AgentTask,
    task_kind: ReflectionTaskKind,
    round_number: int,
    prior_tool_results: list[dict[str, object]],
) -> str:
    catalog = "\n".join(
        f"- {spec.name}: {spec.description}"
        for spec in _allowed_tool_specs(task.required_tool_names)
    )
    prior_reads = json.dumps(
        prior_tool_results,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if round_number == 1:
        round_instruction = (
            "This is the mandatory orientation round. Select at least one concrete "
            "read and no more than the schema permits."
        )
    else:
        round_instruction = (
            "This is the only gap-fill round. Use prior results to select only material "
            "follow-up reads. Return an empty tool_calls array when no gap remains."
        )
    return (
        f"{task.task_description.rstrip()}\n\n"
        "Bounded Reflection read planning:\n"
        f"- task_kind: {task_kind}\n"
        f"- read_round: {round_number}/{_MAX_READ_ROUNDS}\n"
        f"- {round_instruction}\n"
        "- Do not draft or return the final Reflection judgment in this step.\n"
        "- Use only exact ids, target keys, page paths, section names, and time bounds "
        "visible in the task or prior reads.\n"
        "- Calls in one round cannot depend on results from another call in that same "
        "round. Put dependent follow-up work in the gap-fill round.\n"
        "- Do not repeat an identical tool call. The same tool may be used with different "
        "arguments when it reads genuinely different material.\n\n"
        f"Available project-owned reads:\n{catalog}\n\n"
        f"Prior project-owned read results:\n{prior_reads}\n\n"
        "Read-plan response protocol:\n"
        f"- Return exactly one `{_READ_PLAN_TOOL_NAME}` structured tool call.\n"
        "- This step plans reads; it does not execute them. Do not directly call any "
        "tool listed under Available project-owned reads.\n"
        f"- Put every intended business read inside `{_READ_PLAN_TOOL_NAME}.tool_calls`."
    )


def _build_reflection_owner_context_prompt(
    *,
    task_description: str,
    task_kind: ReflectionTaskKind,
    tool_results: list[dict[str, object]],
) -> str:
    serialized_tool_results = json.dumps(
        tool_results,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return (
        f"{task_description.rstrip()}\n\n"
        "Project-owned Reflection reads:\n"
        f"{serialized_tool_results}\n\n"
        "Final Reflection ownership rules:\n"
        f"- Complete the original {task_kind} Reflection task as the single final "
        "business owner.\n"
        "- The original compact brief and successful project-owned reads are the only "
        "allowed sources. Failed reads are not evidence.\n"
        "- Preserve episode identity, target identity, visibility bounds, outcome facts, "
        "and review kind exactly.\n"
        "- Resolve contradictions explicitly; do not average them away or invent missing "
        "facts.\n"
        "- Write coherent professional English; do not emit fragments or prose that "
        "contradicts the structured facts.\n"
        "- The project parser, learning contract, read audit, and bounded repair remain "
        "final."
    )


def _build_reflection_evaluation_prompt(
    *,
    owner_context_prompt: str,
    task_kind: ReflectionTaskKind,
) -> str:
    if task_kind == "shared":
        stage_boundary = (
            "- This shared task has no learning-decision stage. Return the complete "
            "shared Reflection result through the required structured tool.\n"
        )
    else:
        stage_boundary = (
            "- Return every required Reflection evaluation field, but do not return "
            "learning_decision in this stage.\n"
            "- The accepted evaluation will be frozen before learning synthesis.\n"
            "- This stage boundary overrides only the original combined output-shape "
            "instruction; all original business rules remain binding.\n"
        )
    return (
        f"{owner_context_prompt.rstrip()}\n\n"
        "Reflection evaluation stage:\n"
        f"{stage_boundary}"
        "- Return native structured objects, never JSON serialized inside strings.\n"
        "- Call exactly the required structured tool and do not call a read tool.\n"
    )


def _build_reflection_learning_prompt(
    *,
    owner_context_prompt: str,
    frozen_evaluation: Mapping[str, object],
    frozen_evaluation_sha256: str,
) -> str:
    serialized_evaluation = json.dumps(
        dict(frozen_evaluation),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return (
        f"{owner_context_prompt.rstrip()}\n\n"
        "Frozen Reflection evaluation:\n"
        f"- sha256: {frozen_evaluation_sha256}\n"
        f"{serialized_evaluation}\n\n"
        "Learning-decision stage:\n"
        "- Treat the frozen evaluation as final. Do not reassess, rewrite, or return any "
        "evaluation field.\n"
        "- Produce one complete learning_decision consistent with the frozen review "
        "decision, rationale, verdicts, and visible evidence.\n"
        "- Return exactly one wrapper object with one field named learning_decision.\n"
        "- learning_decision and every nested payload must be native JSON objects, not "
        "JSON serialized strings.\n"
        "- This stage boundary overrides only the original combined output-shape "
        "instruction; all original learning and business rules remain binding.\n"
        "- Call exactly the required structured tool and do not call a read tool.\n"
    )


def _read_plan_schema(
    *,
    allowed_tool_names: tuple[str, ...],
    require_read: bool,
) -> dict[str, object]:
    call_variants = [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["tool_name", "arguments"],
            "properties": {
                "tool_name": {"type": "string", "enum": [spec.name]},
                "arguments": dict(spec.input_schema),
            },
        }
        for spec in _allowed_tool_specs(allowed_tool_names)
    ]
    calls_schema: dict[str, object] = {
        "type": "array",
        "items": {"anyOf": call_variants},
        "maxItems": _MAX_TOOL_CALLS_PER_ROUND,
    }
    if require_read:
        calls_schema["minItems"] = 1
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["tool_calls"],
        "properties": {"tool_calls": calls_schema},
    }


async def _generate_read_plan(
    *,
    provider: StructuredInference,
    prompt: str,
    session_id: str,
    allowed_tool_names: tuple[str, ...],
    require_read: bool,
    prior_call_fingerprints: set[str],
) -> tuple[tuple[_PlannedToolCall, ...], dict[str, object]]:
    current_prompt = prompt
    provider_attempts: list[dict[str, object]] = []
    rejected_plans: list[dict[str, object]] = []
    for contract_attempt in range(1, _READ_PLAN_CONTRACT_MAX_ATTEMPTS + 1):
        plan_result = await provider.generate_structured(
            prompt=current_prompt,
            session_id=session_id,
            tool_name=_READ_PLAN_TOOL_NAME,
            tool_description=(
                "Return a bounded Reflection read plan. Put intended business reads "
                "inside tool_calls; do not call those business tools directly."
            ),
            input_schema=_read_plan_schema(
                allowed_tool_names=allowed_tool_names,
                require_read=require_read,
            ),
        )
        provider_attempts.append(dict(plan_result.metadata))
        try:
            planned_calls = _parse_read_plan(
                plan_result.payload,
                allowed_tool_names=allowed_tool_names,
                require_read=require_read,
                prior_call_fingerprints=prior_call_fingerprints,
            )
        except WorkflowReflectionRuntimeError as exc:
            rejected_plans.append(
                {
                    "contract_attempt": contract_attempt,
                    "error": str(exc),
                    "payload": dict(plan_result.payload),
                }
            )
            if contract_attempt >= _READ_PLAN_CONTRACT_MAX_ATTEMPTS:
                raise WorkflowReflectionRuntimeError(
                    "workflow Reflection read-plan contract repair exhausted after "
                    f"{contract_attempt} attempts: {exc}"
                ) from exc
            current_prompt = (
                f"{prompt.rstrip()}\n\n"
                "Previous read plan failed deterministic event-trader workflow "
                "contract:\n"
                f"- {exc}\n"
                "Return only a corrected tool_calls plan for the same Reflection "
                "context. Do not make the final business judgment."
            )
            continue
        return planned_calls, {
            "contract_attempt_count": contract_attempt,
            "payload": dict(plan_result.payload),
            "provider_attempts": provider_attempts,
            "rejected_plans": rejected_plans,
        }
    raise AssertionError("Reflection read-plan contract repair loop ended unexpectedly")


def _parse_read_plan(
    payload: Mapping[str, object],
    *,
    allowed_tool_names: tuple[str, ...],
    require_read: bool,
    prior_call_fingerprints: set[str],
) -> tuple[_PlannedToolCall, ...]:
    if set(payload) != {"tool_calls"}:
        raise WorkflowReflectionRuntimeError(
            "workflow Reflection read plan must contain exactly tool_calls."
        )
    raw_calls = payload.get("tool_calls")
    if not isinstance(raw_calls, list):
        raise WorkflowReflectionRuntimeError("workflow Reflection tool_calls must be an array.")
    if require_read and not raw_calls:
        raise WorkflowReflectionRuntimeError(
            "workflow Reflection first read plan must contain at least one tool call."
        )
    if len(raw_calls) > _MAX_TOOL_CALLS_PER_ROUND:
        raise WorkflowReflectionRuntimeError(
            "workflow Reflection read plan exceeded the per-round tool-call budget."
        )
    planned_calls: list[_PlannedToolCall] = []
    seen_fingerprints = set(prior_call_fingerprints)
    allowed = set(allowed_tool_names)
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, Mapping) or set(raw_call) != {
            "tool_name",
            "arguments",
        }:
            raise WorkflowReflectionRuntimeError(
                f"workflow Reflection tool_calls[{index}] has the wrong shape."
            )
        tool_name = raw_call.get("tool_name")
        arguments = raw_call.get("arguments")
        if not isinstance(tool_name, str) or tool_name not in allowed:
            raise WorkflowReflectionRuntimeError(
                f"workflow Reflection tool_calls[{index}] selected a forbidden tool."
            )
        if not isinstance(arguments, Mapping):
            raise WorkflowReflectionRuntimeError(
                f"workflow Reflection tool_calls[{index}].arguments must be an object."
            )
        planned_call = _PlannedToolCall(
            tool_name=tool_name,
            arguments=dict(arguments),
        )
        if planned_call.fingerprint in seen_fingerprints:
            raise WorkflowReflectionRuntimeError(
                "workflow Reflection read plan repeated an identical tool call."
            )
        seen_fingerprints.add(planned_call.fingerprint)
        planned_calls.append(planned_call)
    return tuple(planned_calls)


def _allowed_tool_specs(
    allowed_tool_names: tuple[str, ...],
) -> tuple[ReflectionToolSpec, ...]:
    specs_by_name = {spec.name: spec for spec in REFLECTION_TOOL_SPECS}
    try:
        return tuple(specs_by_name[name] for name in allowed_tool_names)
    except KeyError as exc:
        raise WorkflowReflectionRuntimeError(
            f"workflow Reflection has no schema for required tool {exc.args[0]!r}."
        ) from exc


def _assert_prompt_budget(
    *,
    prompt: str,
    config: WorkflowReflectionRuntimeConfig,
    stage: str,
) -> None:
    assert_structured_prompt_budget(
        prompt,
        max_context_tokens=config.llm_max_context_length,
        max_output_tokens=config.llm_max_output_tokens,
        stage=f"workflow Reflection {stage}",
        error_type=WorkflowReflectionRuntimeError,
    )


def _normalize_tool_result(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return _to_jsonable(value)


def _tool_result_sha256(value: object) -> str:
    if isinstance(value, str):
        serialized = value
    else:
        serialized = json.dumps(
            _normalize_tool_result(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return sha256(serialized.encode("utf-8")).hexdigest()


def _to_jsonable(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_to_jsonable(item) for item in value]
    if hasattr(value, "to_json_payload"):
        return _to_jsonable(value.to_json_payload())
    if is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(asdict(cast(Any, value)))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _to_jsonable(model_dump())
    return str(value)


def _write_workflow_trace(
    *,
    log_dir: Path,
    task: AgentTask,
    trace: Mapping[str, object],
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    task_digest = sha256(task.task_id.encode("utf-8")).hexdigest()[:24]
    path = log_dir / f"workflow-reflection-{task_digest}-{uuid4().hex}.json"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=log_dir,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(
                _to_jsonable(trace),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
        os.replace(temporary_path, path)
    except Exception as exc:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        raise WorkflowReflectionRuntimeError(
            f"failed to persist workflow Reflection diagnostic log: {exc}"
        ) from exc
    return path


def _validate_path(value: Path, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise WorkflowReflectionRuntimeError(f"{field_name} must be a Path.")
    return value.expanduser().resolve(strict=False)


def _validate_non_blank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowReflectionRuntimeError(f"{field_name} must be non-blank.")
    return value.strip()


__all__ = [
    "WorkflowReflectionRuntimeConfig",
    "WorkflowReflectionRuntimeError",
    "build_workflow_reflection_attempt_factory",
]
