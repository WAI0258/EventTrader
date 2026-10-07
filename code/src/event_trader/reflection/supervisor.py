"""Provider-neutral supervision for the four Reflection agent workflows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol, cast

from event_trader.reasoning.runtime import (
    AgentRunReceipt,
    AgentRuntime,
    AgentRuntimeContractError,
    AgentTask,
    AgentToolGateway,
    execute_agent_runtime_once,
)
from event_trader.reflection.agent import (
    ReflectionContractFailure,
    ReflectionContractRepairExhausted,
    append_reflection_contract_repair_feedback,
    classify_reflection_contract_failure,
    format_reflection_contract_repair_failure,
    is_unfinished_terminal_final_output_failure,
    run_reflection_agent_with_contract_repair,
)
from event_trader.reflection.agent_contract import (
    REFLECTION_AGENT_ROLE,
    REQUIRED_REFLECTION_TOOL_NAMES,
    ReflectionTaskKind,
)
from event_trader.reflection.context import ReflectionLedgerContext
from event_trader.reflection.contracts import (
    OpenPositionEvaluationResult,
    ReflectionEvaluationResult,
    TargetCloseReflectionEvaluationResult,
    TargetReflectionEvaluationResult,
)
from event_trader.reflection.output import (
    build_skipped_open_position_contract_evaluation,
    parse_open_position_reflection_output,
    parse_shared_reflection_output,
    parse_target_close_reflection_output,
    parse_target_reflection_output,
    require_reflection_datetime,
)
from event_trader.reflection.prompt import (
    build_open_position_reflection_prompt,
    build_shared_reflection_prompt,
    build_target_close_reflection_prompt,
    build_target_reflection_prompt,
    target_reflection_window_end,
)
from event_trader.reflection.view_contracts import (
    CloseEpisodeReflectionContext,
    EpisodeReflectionContext,
    OpenPositionReflectionContext,
)

type ReflectionCallback = Callable[
    [ReflectionLedgerContext],
    ReflectionEvaluationResult,
]
type TargetReflectionCallback = Callable[
    [EpisodeReflectionContext],
    TargetReflectionEvaluationResult,
]
type TargetCloseReflectionCallback = Callable[
    [CloseEpisodeReflectionContext],
    TargetCloseReflectionEvaluationResult,
]
type OpenPositionReflectionCallback = Callable[
    [OpenPositionReflectionContext],
    OpenPositionEvaluationResult,
]


class ReflectionRuntimeError(RuntimeError):
    """Raised when project-owned Reflection supervision cannot finish safely."""


class ReflectionContractRepairRuntimeError(ReflectionRuntimeError):
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


@dataclass(frozen=True, slots=True)
class ReflectionAttemptScope:
    """Business visibility and logical identity for one Reflection attempt runtime."""

    task_kind: ReflectionTaskKind
    logical_task_id: str
    target_key: str | None
    window_start: datetime
    window_end: datetime


@dataclass(frozen=True, slots=True)
class ReflectionAttemptBinding:
    """Provider implementation bound to one Reflection visibility scope."""

    task_id: str
    runtime: AgentRuntime
    tools: AgentToolGateway
    repair_ownership: Literal["supervisor_attempt", "workflow_decision"] = (
        "supervisor_attempt"
    )
    decision_repair_runner: ReflectionDecisionRepairRunner | None = None

    def __post_init__(self) -> None:
        if (self.repair_ownership == "workflow_decision") != (
            self.decision_repair_runner is not None
        ):
            raise ValueError(
                "workflow_decision repair ownership requires exactly one "
                "decision_repair_runner."
            )


class ReflectionDecisionRepairRunner(Protocol):
    def __call__(
        self,
        *,
        task: AgentTask,
        tools: AgentToolGateway,
        evaluate_run_result: Callable[[AgentRunReceipt], object],
    ) -> Awaitable[object]: ...


type ReflectionAttemptFactory = Callable[
    [ReflectionAttemptScope],
    ReflectionAttemptBinding,
]


def build_reflection_runner(
    *,
    attempt_factory: ReflectionAttemptFactory,
) -> ReflectionCallback:
    """Build the shared-ledger Reflection evaluator."""

    def evaluate(context: ReflectionLedgerContext) -> ReflectionEvaluationResult:
        if not isinstance(context, ReflectionLedgerContext):
            raise ReflectionRuntimeError("context must be a ReflectionLedgerContext instance.")
        _assert_not_in_event_loop()
        return asyncio.run(_run_shared_reflection(context, attempt_factory))

    return evaluate


def build_target_reflection_runner(
    *,
    attempt_factory: ReflectionAttemptFactory,
) -> TargetReflectionCallback:
    """Build the target-episode Reflection evaluator."""

    def evaluate(
        context: EpisodeReflectionContext,
    ) -> TargetReflectionEvaluationResult:
        if not isinstance(context, EpisodeReflectionContext):
            raise ReflectionRuntimeError("context must be an EpisodeReflectionContext instance.")
        _assert_not_in_event_loop()
        return asyncio.run(_run_target_reflection(context, attempt_factory))

    return evaluate


def build_target_close_reflection_runner(
    *,
    attempt_factory: ReflectionAttemptFactory,
) -> TargetCloseReflectionCallback:
    """Build the target-close Reflection evaluator."""

    def evaluate(
        context: CloseEpisodeReflectionContext,
    ) -> TargetCloseReflectionEvaluationResult:
        if not isinstance(context, CloseEpisodeReflectionContext):
            raise ReflectionRuntimeError(
                "context must be a CloseEpisodeReflectionContext instance."
            )
        _assert_not_in_event_loop()
        return asyncio.run(_run_target_close_reflection(context, attempt_factory))

    return evaluate


def build_open_position_reflection_runner(
    *,
    attempt_factory: ReflectionAttemptFactory,
) -> OpenPositionReflectionCallback:
    """Build the open-position Reflection evaluator with conservative fallback."""

    def evaluate(
        context: OpenPositionReflectionContext,
    ) -> OpenPositionEvaluationResult:
        if not isinstance(context, OpenPositionReflectionContext):
            raise ReflectionRuntimeError(
                "context must be a OpenPositionReflectionContext instance."
            )
        _assert_not_in_event_loop()
        try:
            return asyncio.run(_run_open_position_reflection(context, attempt_factory))
        except ReflectionContractRepairRuntimeError as exc:
            if is_unfinished_terminal_final_output_failure(exc.failure):
                return build_skipped_open_position_contract_evaluation(
                    context=context,
                    failure_reason=str(exc),
                )
            raise

    return evaluate


async def run_reflection_agent_supervisor[ResultT](
    *,
    scope: ReflectionAttemptScope,
    task_description: str,
    attempt_factory: ReflectionAttemptFactory,
    evaluate_run_result: Callable[[AgentRunReceipt], ResultT],
) -> ResultT:
    """Run one Reflection decision plus bounded project-owned contract repairs."""
    binding = attempt_factory(scope)
    task = AgentTask(
        role=REFLECTION_AGENT_ROLE,
        task_id=binding.task_id,
        task_description=task_description,
        required_tool_names=REQUIRED_REFLECTION_TOOL_NAMES,
    )

    if binding.repair_ownership == "workflow_decision":
        assert binding.decision_repair_runner is not None
        try:
            return cast(
                ResultT,
                await binding.decision_repair_runner(
                    task=task,
                    tools=binding.tools,
                    evaluate_run_result=cast(
                        Callable[[AgentRunReceipt], object], evaluate_run_result
                    ),
                ),
            )
        except ReflectionContractRepairExhausted as exc:
            raise ReflectionContractRepairRuntimeError(
                exc.failure,
                attempt_count=exc.attempt_count,
                max_attempts=exc.max_attempts,
            ) from exc

    async def _run_attempt(current_task_description: str) -> AgentRunReceipt:
        try:
            return await execute_agent_runtime_once(
                runtime=binding.runtime,
                task=replace(task, task_description=current_task_description),
                tools=binding.tools,
            )
        except AgentRuntimeContractError as exc:
            raise ReflectionRuntimeError(str(exc)) from exc

    try:
        return await run_reflection_agent_with_contract_repair(
            task_description=task_description,
            run_attempt=_run_attempt,
            evaluate_run_result=evaluate_run_result,
            classify_failure=classify_reflection_contract_failure,
            append_repair_feedback=append_reflection_contract_repair_feedback,
        )
    except ReflectionContractRepairExhausted as exc:
        raise ReflectionContractRepairRuntimeError(
            exc.failure,
            attempt_count=exc.attempt_count,
            max_attempts=exc.max_attempts,
        ) from exc


async def _run_shared_reflection(
    context: ReflectionLedgerContext,
    attempt_factory: ReflectionAttemptFactory,
) -> ReflectionEvaluationResult:
    scope = ReflectionAttemptScope(
        task_kind="shared",
        logical_task_id=(
            f"event-trader-reflection-{context.anchor_id.replace('/', '_').replace('#', '_')}"
        ),
        target_key=context.anchor.target_key,
        window_start=context.receipt.later_evidence_window_start,
        window_end=context.receipt.later_evidence_window_end,
    )
    return await run_reflection_agent_supervisor(
        scope=scope,
        task_description=build_shared_reflection_prompt(context=context),
        attempt_factory=attempt_factory,
        evaluate_run_result=lambda receipt: parse_shared_reflection_output(
            receipt,
            context=context,
        ),
    )


async def _run_target_reflection(
    context: EpisodeReflectionContext,
    attempt_factory: ReflectionAttemptFactory,
) -> TargetReflectionEvaluationResult:
    scope = ReflectionAttemptScope(
        task_kind="target",
        logical_task_id=f"event-trader-target-reflection-{context.episode.episode_id}",
        target_key=context.episode.target_key,
        window_start=context.episode.opened_at,
        window_end=target_reflection_window_end(context),
    )
    return await run_reflection_agent_supervisor(
        scope=scope,
        task_description=build_target_reflection_prompt(context=context),
        attempt_factory=attempt_factory,
        evaluate_run_result=lambda receipt: parse_target_reflection_output(
            receipt,
            context=context,
        ),
    )


async def _run_open_position_reflection(
    context: OpenPositionReflectionContext,
    attempt_factory: ReflectionAttemptFactory,
) -> OpenPositionEvaluationResult:
    scope = ReflectionAttemptScope(
        task_kind="open_position",
        logical_task_id=(f"event-trader-terminal-reflection-{context.episode.episode_id}"),
        target_key=context.episode.target_key,
        window_start=context.episode.opened_at,
        window_end=context.terminal_mark.replay_end_at,
    )
    return await run_reflection_agent_supervisor(
        scope=scope,
        task_description=build_open_position_reflection_prompt(context=context),
        attempt_factory=attempt_factory,
        evaluate_run_result=lambda receipt: parse_open_position_reflection_output(
            receipt,
            context=context,
        ),
    )


async def _run_target_close_reflection(
    context: CloseEpisodeReflectionContext,
    attempt_factory: ReflectionAttemptFactory,
) -> TargetCloseReflectionEvaluationResult:
    scope = ReflectionAttemptScope(
        task_kind="target_close",
        logical_task_id=f"event-trader-close-reflection-{context.episode.episode_id}",
        target_key=context.episode.target_key,
        window_start=context.episode.opened_at,
        window_end=require_reflection_datetime(
            context.episode.closed_at,
            field_name="episode.closed_at",
        ),
    )
    return await run_reflection_agent_supervisor(
        scope=scope,
        task_description=build_target_close_reflection_prompt(context=context),
        attempt_factory=attempt_factory,
        evaluate_run_result=lambda receipt: parse_target_close_reflection_output(
            receipt,
            context=context,
        ),
    )


def _assert_not_in_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise ReflectionRuntimeError(
        "Reflection runtime currently supports only synchronous callers; do not "
        "call it from an active event loop."
    )


__all__ = [
    "OpenPositionReflectionCallback",
    "ReflectionAttemptBinding",
    "ReflectionAttemptFactory",
    "ReflectionAttemptScope",
    "ReflectionCallback",
    "ReflectionContractRepairRuntimeError",
    "ReflectionRuntimeError",
    "TargetCloseReflectionCallback",
    "TargetReflectionCallback",
    "build_open_position_reflection_runner",
    "build_reflection_runner",
    "build_target_close_reflection_runner",
    "build_target_reflection_runner",
    "run_reflection_agent_supervisor",
]
