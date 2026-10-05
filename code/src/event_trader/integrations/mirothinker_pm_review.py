"""MiroThinker-style PMReview runner integration without execution routing."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from event_trader.contracts import EvidenceLedgerRecord
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.price_level_role import PriceLevelRole
from event_trader.contracts.view_state_change import MarketDataBar
from event_trader.episode_memory.projection import PMEpisodeMemoryView
from event_trader.execution import ExecutionRecord
from event_trader.integrations.boxed_json import (
    BoxedJsonPayloadError,
    load_first_boxed_json_object,
)
from event_trader.integrations.mirothinker_llm_config import (
    build_mirothinker_llm_config,
    normalize_mirothinker_reasoning_effort,
)
from event_trader.integrations.mirothinker_runtime_paths import (
    MiroThinkerRuntimePathError,
    prepare_mirothinker_runtime,
)
from event_trader.integrations.mirothinker_task_log import mirothinker_portable_run_id
from event_trader.integrations.strict_mirothinker_agent import (
    StrictMiroThinkerAgentContract,
    mirothinker_structured_output_fallback_texts_from_log,
    run_strict_mirothinker_agent,
)
from event_trader.learning_cards import (
    FileBackedLearningCardStore,
    LearningCard,
    select_learning_cards,
)
from event_trader.pm_review.context import (
    PMReviewContextError,
    load_candidate_review_anchor_for_request,
    read_visible_evidence,
    read_visible_market_bars,
    read_visible_pm_history,
)
from event_trader.pm_review.contracts import (
    CandidateReviewAnchor,
    PMAnalysisSnapshot,
    PMDecisionDraft,
    PMPortfolioRiskSnapshot,
    PMPositionReviewTriggers,
    PMReviewContractError,
    PMReviewEpisodeMemoryReadReceipt,
    PMReviewExecutionDirectionMode,
    PMReviewInput,
    PMReviewRequest,
    PMReviewToolReadReceipt,
    analysis_snapshot_from_assessment,
    parse_pm_decision_draft,
)
from event_trader.pm_review.position_review import (
    PMPositionReviewError,
    build_pm_position_review_triggers,
)
from event_trader.pm_review.prompt import PM_REVIEW_OUTPUT_SCHEMA_PROMPT
from event_trader.pm_review.store import (
    PMPositionReviewTriggerStore,
    PMReviewEpisodeMemoryReadReceiptStore,
    PMReviewStoreError,
)
from event_trader.pm_review.tools import (
    PMReviewToolResult,
    PMReviewToolSession,
    read_pm_episode_memory,
    read_pm_review_active_exposure,
    read_pm_review_evidence,
    read_pm_review_history,
    read_pm_review_learning_cards,
    read_pm_review_market_bars,
    read_pm_review_request,
)
from event_trader.portfolio.active_exposure import ActiveExposure
from event_trader.portfolio.contracts import PMDecision
from event_trader.portfolio.pm_decision_adapter import (
    PMDecisionAdapterError,
    append_pm_decision_from_draft,
    build_pm_decision_from_draft,
)
from event_trader.portfolio.store import PortfolioStoreError
from event_trader.storage import WorkspaceLayout

PM_REVIEW_MIROTHINKER_SERVER_NAME = "event_trader_pm_review"
REQUIRED_PM_REVIEW_TOOL_NAMES: tuple[str, ...] = (
    "read_pm_review_request",
    "read_pm_review_evidence",
    "read_pm_review_market_bars",
    "read_pm_review_history",
    "read_pm_episode_memory",
    "read_pm_review_learning_cards",
    "read_pm_review_active_exposure",
)
REQUIRED_PM_REVIEW_MCP_TOOLS: tuple[tuple[str, str], ...] = tuple(
    (PM_REVIEW_MIROTHINKER_SERVER_NAME, tool_name)
    for tool_name in REQUIRED_PM_REVIEW_TOOL_NAMES
)
_PM_REVIEW_CONTRACT_REPAIR_MAX_REPAIRS = 3
_PM_REVIEW_PROMPT_FIELD_REPORT_LIMIT = 8
_PM_PRICE_SURFACE_NOTE = (
    "Use `price_level_roles` and `market_setup_dashboard_md` as the only "
    "authoritative price surfaces. Treat any price numbers inside this prose as "
    "non-authoritative narrative if they disagree."
)
_PM_REVIEW_FOCUS_LEVEL_ROLES = frozenset(
    {
        "add",
        "hold_boundary",
        "de_risk_or_take_profit",
        "exit",
        "reverse_or_cover",
        "invalidates",
    }
)


class MiroThinkerPMReviewRuntimeError(RuntimeError):
    """Raised when a PMReview MiroThinker-style run cannot be trusted."""


class MiroThinkerPMReviewStructuralRuntimeError(MiroThinkerPMReviewRuntimeError):
    """Raised when a required runtime-owned PMReview input cannot be materialized."""


@dataclass(frozen=True, slots=True)
class MiroThinkerPMReviewRuntimeConfig:
    """Explicit production runtime inputs for one PMReview MiroThinker task."""

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
            normalize_mirothinker_reasoning_effort(
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
class PMReviewMiroThinkerRunResult:
    """Raw PMReview agent output before parsing and deterministic materialization."""

    final_summary: str
    final_boxed_answer: str
    log_file_path: str = ""
    failure_experience_summary: object = None

    def __post_init__(self) -> None:
        if not isinstance(self.final_summary, str):
            raise MiroThinkerPMReviewRuntimeError("final_summary must be a string.")
        if not isinstance(self.final_boxed_answer, str) or not self.final_boxed_answer.strip():
            raise MiroThinkerPMReviewRuntimeError(
                "PMReview agent returned no boxed final answer."
            )
        if not isinstance(self.log_file_path, str):
            raise MiroThinkerPMReviewRuntimeError("log_file_path must be a string.")


@dataclass(frozen=True, slots=True)
class PMReviewMiroThinkerTask:
    """Inputs exposed to a PMReview MiroThinker task runner."""

    task_id: str
    task_description: str
    session: PMReviewToolSession
    tools: PMReviewAgentToolbox
    required_tool_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedSourceEpisodeMemory:
    view: PMEpisodeMemoryView
    detailed_receipt: PMReviewEpisodeMemoryReadReceipt
    tool_read_receipt: PMReviewToolReadReceipt


@dataclass(frozen=True, slots=True)
class MiroThinkerPMReviewResult:
    """Persisted output from one PMReview MiroThinker-style run."""

    pm_review_input: PMReviewInput
    decision_draft: PMDecisionDraft
    pm_decision: PMDecision
    position_review_triggers: PMPositionReviewTriggers | None
    raw_run_result: PMReviewMiroThinkerRunResult
    read_receipts: tuple[PMReviewToolReadReceipt, ...]


@dataclass(slots=True)
class PMReviewAgentToolbox:
    """Request-bound PMReview tools exposed to a future ReAct agent."""

    session: PMReviewToolSession
    _read_receipts: list[PMReviewToolReadReceipt] = field(default_factory=list)

    @property
    def available_tool_names(self) -> tuple[str, ...]:
        return REQUIRED_PM_REVIEW_TOOL_NAMES

    @property
    def read_receipts(self) -> tuple[PMReviewToolReadReceipt, ...]:
        return tuple(self._read_receipts)

    def read_pm_review_request(self) -> PMReviewToolResult[PMReviewRequest]:
        return self._record(
            read_pm_review_request(
                session=self.session,
                session_id=self.session.session_id,
                pm_review_request_id=self.session.request.request_id,
            )
        )

    def read_pm_review_evidence(
        self,
        *,
        event_ids: tuple[str, ...] = (),
    ) -> PMReviewToolResult[EvidenceLedgerRecord]:
        return self._record(
            read_pm_review_evidence(
                session=self.session,
                session_id=self.session.session_id,
                pm_review_request_id=self.session.request.request_id,
                event_ids=event_ids,
            )
        )

    def read_pm_review_market_bars(self) -> PMReviewToolResult[MarketDataBar]:
        return self._record(
            read_pm_review_market_bars(
                session=self.session,
                session_id=self.session.session_id,
                pm_review_request_id=self.session.request.request_id,
            )
        )

    def read_pm_review_history(self):
        return self._record(
            read_pm_review_history(
                session=self.session,
                session_id=self.session.session_id,
                pm_review_request_id=self.session.request.request_id,
            )
        )

    def read_pm_episode_memory(self):
        return self._record(
            read_pm_episode_memory(
                session=self.session,
                session_id=self.session.session_id,
                pm_review_request_id=self.session.request.request_id,
            )
        )

    def read_pm_review_learning_cards(self) -> PMReviewToolResult[LearningCard]:
        return self._record(
            read_pm_review_learning_cards(
                session=self.session,
                session_id=self.session.session_id,
                pm_review_request_id=self.session.request.request_id,
            )
        )

    def read_pm_review_active_exposure(self) -> PMReviewToolResult[ActiveExposure]:
        return self._record(
            read_pm_review_active_exposure(
                session=self.session,
                session_id=self.session.session_id,
                pm_review_request_id=self.session.request.request_id,
            )
        )

    def _record(self, result):
        self._read_receipts.append(result.receipt)
        return result


type PMReviewTaskRunner = Callable[
    [PMReviewMiroThinkerTask],
    PMReviewMiroThinkerRunResult,
]
type PMReviewRunner = Callable[[PMReviewRequest], MiroThinkerPMReviewResult]


def build_production_mirothinker_pm_review_task_runner(
    *,
    config: MiroThinkerPMReviewRuntimeConfig,
) -> PMReviewTaskRunner:
    """Build the production PMReview task runner using vendored MiroThinker."""

    if not isinstance(config, MiroThinkerPMReviewRuntimeConfig):
        raise MiroThinkerPMReviewRuntimeError(
            "config must be a MiroThinkerPMReviewRuntimeConfig instance."
        )

    def run(task: PMReviewMiroThinkerTask) -> PMReviewMiroThinkerRunResult:
        if not isinstance(task, PMReviewMiroThinkerTask):
            raise MiroThinkerPMReviewRuntimeError(
                "task must be a PMReviewMiroThinkerTask instance."
            )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise MiroThinkerPMReviewRuntimeError(
                "production PMReview MiroThinker runner supports only synchronous callers."
            )
        return asyncio.run(_run_production_pm_review_task(config=config, task=task))

    return run


def build_mirothinker_pm_review_runner(
    *,
    layout: WorkspaceLayout,
    task_runner: PMReviewTaskRunner,
    actual_exposure: ActiveExposure,
    evidence_records: tuple[EvidenceLedgerRecord, ...] = (),
    market_bars: tuple[MarketDataBar, ...] = (),
    pm_review_requests: tuple[PMReviewRequest, ...] = (),
    pm_decisions: tuple[PMDecision, ...] = (),
    execution_records: tuple[ExecutionRecord, ...] = (),
    portfolio_risk_market_bars: tuple[MarketDataBar, ...] = (),
    assessment: AnalysisAssessment | None = None,
    available_tool_names: tuple[str, ...] | None = None,
    prompt_max_chars: int | None = None,
    decision_available_at: datetime,
) -> PMReviewRunner:
    """Build a single-request PMReview runner backed by a supplied task runner."""

    _validate_layout(layout)
    if not callable(task_runner):
        raise MiroThinkerPMReviewRuntimeError("task_runner must be callable.")
    _preflight_pm_review_tools(available_tool_names)

    def run(request: PMReviewRequest) -> MiroThinkerPMReviewResult:
        return run_pm_review_request(
            layout=layout,
            request=request,
            task_runner=task_runner,
            actual_exposure=actual_exposure,
            evidence_records=evidence_records,
            market_bars=market_bars,
            pm_review_requests=pm_review_requests,
            pm_decisions=pm_decisions,
            execution_records=execution_records,
            portfolio_risk_market_bars=portfolio_risk_market_bars,
            assessment=assessment,
            available_tool_names=available_tool_names,
            prompt_max_chars=prompt_max_chars,
            decision_available_at=decision_available_at,
        )

    return run


async def _run_production_pm_review_task(
    *,
    config: MiroThinkerPMReviewRuntimeConfig,
    task: PMReviewMiroThinkerTask,
) -> PMReviewMiroThinkerRunResult:
    execute_task_pipeline, OutputFormatter, OmegaConf = _load_vendor_runtime(
        config.vendor_root
    )
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
                    "tools": list(REQUIRED_PM_REVIEW_TOOL_NAMES),
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
    tool_manager = _PMReviewToolboxToolManager(task.tools)
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
    return PMReviewMiroThinkerRunResult(
        final_summary=run_result.final_summary,
        final_boxed_answer=run_result.final_boxed_answer,
        log_file_path=run_result.log_file_path,
        failure_experience_summary=run_result.failure_experience_summary,
    )


class _PMReviewToolboxToolManager:
    """MiroThinker ToolManager adapter over one request-bound PMReview toolbox."""

    def __init__(self, toolbox: PMReviewAgentToolbox) -> None:
        if not isinstance(toolbox, PMReviewAgentToolbox):
            raise MiroThinkerPMReviewRuntimeError(
                "toolbox must be a PMReviewAgentToolbox instance."
            )
        self._toolbox = toolbox
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
        if server_name != PM_REVIEW_MIROTHINKER_SERVER_NAME:
            return {"error": f"PMReview tool server is not available: {server_name}"}
        if tool_name not in REQUIRED_PM_REVIEW_TOOL_NAMES:
            return {"error": f"PMReview tool is not available: {tool_name}"}
        args = dict(arguments or {})
        try:
            result = _call_pm_review_tool(
                toolbox=self._toolbox,
                tool_name=tool_name,
                arguments=args,
            )
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


def run_pm_review_request(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest,
    task_runner: PMReviewTaskRunner,
    actual_exposure: ActiveExposure,
    evidence_records: tuple[EvidenceLedgerRecord, ...] = (),
    market_bars: tuple[MarketDataBar, ...] = (),
    pm_review_requests: tuple[PMReviewRequest, ...] = (),
    pm_decisions: tuple[PMDecision, ...] = (),
    execution_records: tuple[ExecutionRecord, ...] = (),
    portfolio_risk_market_bars: tuple[MarketDataBar, ...] = (),
    assessment: AnalysisAssessment | None = None,
    available_tool_names: tuple[str, ...] | None = None,
    execution_direction_mode: PMReviewExecutionDirectionMode = "long_short",
    prompt_max_chars: int | None = None,
    decision_available_at: datetime,
) -> MiroThinkerPMReviewResult:
    """Run one PMReview request through a MiroThinker-style task runner.

    The supplied task_runner is the narrow hook for real MiroThinker execution.
    Tests pass a fake runner; production wiring can pass a callable that runs the
    vendor task loop against the same PMReviewToolbox.
    """

    _validate_layout(layout)
    if not isinstance(request, PMReviewRequest):
        raise MiroThinkerPMReviewRuntimeError(
            "request must be a PMReviewRequest instance."
        )
    if not isinstance(actual_exposure, ActiveExposure):
        raise MiroThinkerPMReviewRuntimeError(
            "actual_exposure must be an ActiveExposure instance."
        )
    if actual_exposure.target_key != request.target_key:
        raise MiroThinkerPMReviewRuntimeError(
            "actual_exposure target_key must match request."
        )
    if not isinstance(decision_available_at, datetime) or decision_available_at.tzinfo is None:
        raise MiroThinkerPMReviewRuntimeError(
            "decision_available_at must be a timezone-aware datetime."
        )
    if decision_available_at < request.business_at:
        raise MiroThinkerPMReviewRuntimeError(
            "decision_available_at must be at or after request.business_at."
        )
    if assessment is not None:
        if not isinstance(assessment, AnalysisAssessment):
            raise MiroThinkerPMReviewRuntimeError(
                "assessment must be an AnalysisAssessment instance when supplied."
            )
        if assessment.target_key != request.target_key:
            raise MiroThinkerPMReviewRuntimeError(
                "assessment target_key must match request."
            )
    _preflight_pm_review_tools(available_tool_names)
    try:
        candidate_anchor = load_candidate_review_anchor_for_request(
            layout=layout,
            request=request,
        )
    except (PMReviewContextError, PMReviewStoreError) as exc:
        raise MiroThinkerPMReviewStructuralRuntimeError(str(exc)) from exc
    session = PMReviewToolSession(
        session_id=_pm_review_session_id(request),
        layout=layout,
        request=request,
        active_exposure=actual_exposure,
        evidence_records=evidence_records,
        market_bars=market_bars,
        pm_review_requests=pm_review_requests,
        pm_decisions=pm_decisions,
        learning_cards=select_learning_cards(
            store=FileBackedLearningCardStore(layout),
            consumer_role="pm_review",
            target_key=request.target_key,
            business_at=request.business_at,
        ),
        candidate_anchor=candidate_anchor,
    )
    prepared_source_episode_memory = _prepare_source_episode_memory(session=session)
    pm_review_input = build_pm_review_input(
        request=request,
        actual_exposure=actual_exposure,
        assessment=assessment,
        evidence_records=evidence_records,
        market_bars=market_bars,
        pm_review_requests=pm_review_requests,
        pm_decisions=pm_decisions,
        execution_records=execution_records,
        portfolio_risk_market_bars=portfolio_risk_market_bars,
        candidate_anchor=candidate_anchor,
        execution_direction_mode=execution_direction_mode,
        source_episode_memory_view=prepared_source_episode_memory.view
        if prepared_source_episode_memory is not None
        else None,
        source_episode_memory_read_receipt=prepared_source_episode_memory.detailed_receipt
        if prepared_source_episode_memory is not None
        else None,
    )
    toolbox = PMReviewAgentToolbox(session=session)
    task_description = _render_pm_review_task_prompt(pm_review_input)
    _validate_pm_review_prompt_size(
        pm_review_input=pm_review_input,
        task_description=task_description,
        max_prompt_chars=prompt_max_chars,
    )
    task = PMReviewMiroThinkerTask(
        task_id=mirothinker_portable_run_id(_pm_review_task_id(request)),
        task_description=task_description,
        session=session,
        tools=toolbox,
        required_tool_names=REQUIRED_PM_REVIEW_TOOL_NAMES,
    )
    raw_result, decision_draft = _run_pm_review_task_with_contract_repair(
        task=task,
        task_runner=task_runner,
        pm_review_input=pm_review_input,
        decision_draft_validator=lambda draft: _validate_pm_decision_draft_materializes(
            request=request,
            pm_review_input=pm_review_input,
            decision_draft=draft,
            decision_available_at=decision_available_at,
        ),
    )
    _require_runtime_source_episode_memory_pre_read(
        request=request,
        prepared_source_episode_memory=prepared_source_episode_memory,
    )
    try:
        pm_decision = append_pm_decision_from_draft(
            layout=layout,
            request=request,
            pm_review_input=pm_review_input,
            decision_draft=decision_draft,
            decision_available_at=decision_available_at,
        )
    except PortfolioStoreError as exc:
        raise MiroThinkerPMReviewStructuralRuntimeError(str(exc)) from exc
    except PMDecisionAdapterError as exc:
        raise MiroThinkerPMReviewRuntimeError(str(exc)) from exc
    try:
        position_review_triggers = build_pm_position_review_triggers(
            pm_review_input=pm_review_input,
            requested_state=pm_decision.requested_state,
        )
        if position_review_triggers is not None:
            PMPositionReviewTriggerStore(layout).append(position_review_triggers)
    except (PMPositionReviewError, PMReviewStoreError) as exc:
        raise MiroThinkerPMReviewStructuralRuntimeError(str(exc)) from exc
    return MiroThinkerPMReviewResult(
        pm_review_input=pm_review_input,
        decision_draft=decision_draft,
        pm_decision=pm_decision,
        position_review_triggers=position_review_triggers,
        raw_run_result=raw_result,
        read_receipts=_merge_read_receipts(
            (
                ()
                if prepared_source_episode_memory is None
                else (prepared_source_episode_memory.tool_read_receipt,)
            ),
            toolbox.read_receipts,
        ),
    )


def _run_pm_review_task_with_contract_repair(
    *,
    task: PMReviewMiroThinkerTask,
    task_runner: PMReviewTaskRunner,
    pm_review_input: PMReviewInput | None = None,
    decision_draft_validator: Callable[[PMDecisionDraft], None] | None = None,
) -> tuple[PMReviewMiroThinkerRunResult, PMDecisionDraft]:
    current_task = task
    last_error: MiroThinkerPMReviewRuntimeError | None = None
    max_attempts = _PM_REVIEW_CONTRACT_REPAIR_MAX_REPAIRS + 1
    for attempt_index in range(max_attempts):
        raw_result = task_runner(current_task)
        if not isinstance(raw_result, PMReviewMiroThinkerRunResult):
            raise MiroThinkerPMReviewRuntimeError(
                "task_runner must return PMReviewMiroThinkerRunResult."
            )
        try:
            decision_draft = parse_pm_decision_draft_output(
                final_boxed_answer=raw_result.final_boxed_answer,
                final_summary=raw_result.final_summary,
                fallback_payload_texts=mirothinker_structured_output_fallback_texts_from_log(
                    raw_result.log_file_path
                ),
                pm_review_input=pm_review_input,
            )
            if decision_draft_validator is not None:
                decision_draft_validator(decision_draft)
        except MiroThinkerPMReviewRuntimeError as exc:
            last_error = exc
            if attempt_index >= max_attempts - 1:
                break
            current_task = replace(
                current_task,
                task_description=_append_pm_review_contract_repair_feedback(
                    task_description=current_task.task_description,
                    error=exc,
                    attempt_index=attempt_index,
                    max_attempts=max_attempts,
                ),
            )
            continue
        return raw_result, decision_draft
    raise MiroThinkerPMReviewRuntimeError(
        "PMReview contract repair failed "
        f"[{max_attempts}/{max_attempts}]: {last_error}"
    ) from last_error


def _validate_pm_decision_draft_materializes(
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
    except PMDecisionAdapterError as exc:
        raise MiroThinkerPMReviewRuntimeError(str(exc)) from exc


def _append_pm_review_contract_repair_feedback(
    *,
    task_description: str,
    error: MiroThinkerPMReviewRuntimeError,
    attempt_index: int,
    max_attempts: int,
) -> str:
    return (
        f"{task_description.rstrip()}\n\n"
        "Previous PMReview attempt failed the deterministic output contract:\n"
        f"- attempt: {attempt_index + 1}/{max_attempts}\n"
        f"- error: {_shorten_error_text(str(error), max_length=500)}\n\n"
        "Repeat the same PMReview task and return exactly one corrected JSON object "
        "wrapped in \\boxed{...}. Do not return a tuple, CSV, markdown list, prose, "
        "or any text outside the boxed JSON. Keep the same PMReviewInput identity "
        "fields and do not invent new evidence.\n\n"
        f"{PM_REVIEW_OUTPUT_SCHEMA_PROMPT}"
    )


def _shorten_error_text(value: str, *, max_length: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3]}..."


def build_pm_review_task_prompt(
    *,
    request: PMReviewRequest,
    actual_exposure: ActiveExposure,
    assessment: AnalysisAssessment | None = None,
    evidence_records: tuple[EvidenceLedgerRecord, ...] = (),
    market_bars: tuple[MarketDataBar, ...] = (),
    pm_review_requests: tuple[PMReviewRequest, ...] = (),
    pm_decisions: tuple[PMDecision, ...] = (),
    execution_records: tuple[ExecutionRecord, ...] = (),
    portfolio_risk_market_bars: tuple[MarketDataBar, ...] = (),
    candidate_anchor: CandidateReviewAnchor | None = None,
    source_episode_memory_view: PMEpisodeMemoryView | None = None,
    source_episode_memory_read_receipt: PMReviewEpisodeMemoryReadReceipt | None = None,
    execution_direction_mode: PMReviewExecutionDirectionMode = "long_short",
) -> str:
    """Build the public PMReview prompt around PMReviewInput/PMDecisionDraft."""

    pm_review_input = build_pm_review_input(
        request=request,
        actual_exposure=actual_exposure,
        assessment=assessment,
        evidence_records=evidence_records,
        market_bars=market_bars,
        pm_review_requests=pm_review_requests,
        pm_decisions=pm_decisions,
        execution_records=execution_records,
        portfolio_risk_market_bars=portfolio_risk_market_bars,
        candidate_anchor=candidate_anchor,
        execution_direction_mode=execution_direction_mode,
        source_episode_memory_view=source_episode_memory_view,
        source_episode_memory_read_receipt=source_episode_memory_read_receipt,
    )
    return _render_pm_review_task_prompt(pm_review_input)


def _render_pm_review_task_prompt(pm_review_input: PMReviewInput) -> str:
    return "\n".join(
        (
            _pm_review_role_prompt(),
            "",
            _pm_review_thin_operating_boundary_prompt(),
            "",
            "Use PMReviewInput as the deterministic source of truth for this PM judgment.",
            "Do not invent hidden state, execution objects, requested_target_weight, "
            "or self-review fields.",
            "",
            "Citations must come only from PMReviewInput.visible_evidence and "
            "PMReviewInput.visible_market_bars.",
            "",
            _pm_review_decision_focus_prompt(pm_review_input),
            "",
            "PMReviewInput:",
            json.dumps(
                pm_review_input.to_json_payload(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            "",
            PM_REVIEW_OUTPUT_SCHEMA_PROMPT,
        )
    )


def _validate_pm_review_prompt_size(
    *,
    pm_review_input: PMReviewInput,
    task_description: str,
    max_prompt_chars: int | None,
) -> None:
    if max_prompt_chars is None:
        return
    if (
        not isinstance(max_prompt_chars, int)
        or isinstance(max_prompt_chars, bool)
        or max_prompt_chars <= 0
    ):
        raise MiroThinkerPMReviewRuntimeError(
            "max_prompt_chars must be a positive integer or None."
        )
    prompt_chars = len(task_description)
    if prompt_chars <= max_prompt_chars:
        return
    field_sizes = _pm_review_input_field_char_sizes(pm_review_input)
    largest_fields = ", ".join(
        f"{name}={size}"
        for name, size in sorted(
            field_sizes.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:_PM_REVIEW_PROMPT_FIELD_REPORT_LIMIT]
    )
    raise MiroThinkerPMReviewRuntimeError(
        "PMReview prompt exceeds local prompt budget before agent execution: "
        f"prompt_chars={prompt_chars} "
        f"max_prompt_chars={max_prompt_chars} "
        f"visible_pm_history_count={len(pm_review_input.visible_pm_history)} "
        f"visible_evidence_count={len(pm_review_input.visible_evidence)} "
        f"visible_market_bar_count={len(pm_review_input.visible_market_bars)} "
        f"largest_fields={largest_fields}."
    )


def _pm_review_input_field_char_sizes(
    pm_review_input: PMReviewInput,
) -> dict[str, int]:
    payload = pm_review_input.to_json_payload()
    return {
        key: len(json.dumps(value, ensure_ascii=False, sort_keys=True))
        for key, value in payload.items()
    }


def _pm_review_decision_focus_prompt(pm_review_input: PMReviewInput) -> str:
    lines = [
        "Decision focus:",
        "- Weigh review_reasons, active exposure, same-side level roles, analysis "
        "implications, and visible market structure together.",
        "- No single signal automatically wins. In particular, de_risk_or_take_profit "
        "is a review signal, not a mandatory action by itself.",
        "- Same-side add and hold_boundary levels justify re-evaluation, not automatic "
        "strengthening.",
        (
            "- Current exposure under review: "
            f"{pm_review_input.actual_current_state} "
            f"({pm_review_input.actual_target_weight_before:+.1f})."
        ),
        "- Review reasons: " + ", ".join(pm_review_input.review_reasons) + ".",
    ]
    focus_levels = _active_side_focus_level_summaries(pm_review_input)
    if focus_levels:
        lines.append("- Same-side actionable levels to weigh:")
        lines.extend(f"  - {summary}" for summary in focus_levels)
    elif (
        pm_review_input.current_exposure_required
        and pm_review_input.analysis_snapshot is not None
    ):
        lines.append(
            "- Same-side actionable levels to weigh: none identified in the analysis snapshot."
        )
    if pm_review_input.portfolio_risk_snapshot is not None:
        lines.append(
            "- Portfolio risk snapshot is present: weigh entry-to-current path damage, "
            "retracement from best mark, and current directional return alongside the thesis."
        )
    same_direction_strengthening_focus = _same_direction_strengthening_focus_lines(
        pm_review_input
    )
    if same_direction_strengthening_focus:
        lines.extend(same_direction_strengthening_focus)
    return "\n".join(lines)


def _same_direction_strengthening_focus_lines(
    pm_review_input: PMReviewInput,
) -> tuple[str, ...]:
    state = pm_review_input.actual_current_state
    if state not in {"weak_long", "weak_short"}:
        return ()
    snapshot = pm_review_input.analysis_snapshot
    if snapshot is None:
        return ()
    role_name = (
        "role_if_already_long"
        if state == "weak_long"
        else "role_if_already_short"
    )
    if not any(getattr(level, role_name) == "add" for level in snapshot.price_level_roles):
        return ()
    stronger_state = "strong_long" if state == "weak_long" else "strong_short"
    return (
        "- Same-side add level is present while current exposure is "
        f"{state}: treat that as a review signal, not an automatic reason to "
        f"upgrade to {stronger_state}.",
        "- Upgrade from "
        f"{state} to {stronger_state} only when visible market structure shows "
        "improved edge, not merely continued thesis, positive PnL, or same-side "
        "continuation.",
        "- Keeping "
        f"{state} is valid; if you do not upgrade, explain why the edge did not "
        "improve enough using concrete evidence such as unconfirmed breakout or "
        "breakdown acceptance, volume/price divergence, weaker follow-through, "
        "or compressed reward relative to current path risk.",
    )


def _active_side_focus_level_summaries(
    pm_review_input: PMReviewInput,
) -> tuple[str, ...]:
    snapshot = pm_review_input.analysis_snapshot
    if snapshot is None:
        return ()
    if pm_review_input.actual_current_state in {"weak_long", "strong_long"}:
        role_name = "role_if_already_long"
    elif pm_review_input.actual_current_state in {"weak_short", "strong_short"}:
        role_name = "role_if_already_short"
    else:
        return ()
    summaries: list[str] = []
    required_level_ids = set(pm_review_input.required_price_level_ids)
    for level in snapshot.price_level_roles:
        role = getattr(level, role_name)
        if role not in _PM_REVIEW_FOCUS_LEVEL_ROLES:
            continue
        tag = "required" if level.level_id in required_level_ids else "context"
        summary = (
            f"{level.level_id}: {role} at {_format_focus_level(level)} [{tag}]"
        )
        if level.path_context_required:
            summary += " path_context_required"
        summaries.append(summary)
    return tuple(summaries)


def _format_focus_level(level: PriceLevelRole) -> str:
    if level.value is not None:
        return f"{level.value:.2f}"
    lower = "?" if level.lower is None else f"{level.lower:.2f}"
    upper = "?" if level.upper is None else f"{level.upper:.2f}"
    return f"[{lower}, {upper}]"


def build_pm_review_input(
    *,
    request: PMReviewRequest,
    actual_exposure: ActiveExposure,
    assessment: AnalysisAssessment | None = None,
    evidence_records: tuple[EvidenceLedgerRecord, ...] = (),
    market_bars: tuple[MarketDataBar, ...] = (),
    pm_review_requests: tuple[PMReviewRequest, ...] = (),
    pm_decisions: tuple[PMDecision, ...] = (),
    execution_records: tuple[ExecutionRecord, ...] = (),
    portfolio_risk_market_bars: tuple[MarketDataBar, ...] = (),
    candidate_anchor: CandidateReviewAnchor | None = None,
    source_episode_memory_view: PMEpisodeMemoryView | None = None,
    source_episode_memory_read_receipt: PMReviewEpisodeMemoryReadReceipt | None = None,
    execution_direction_mode: PMReviewExecutionDirectionMode = "long_short",
) -> PMReviewInput:
    """Assemble the repo-owned deterministic PM input surface."""

    if not isinstance(request, PMReviewRequest):
        raise MiroThinkerPMReviewRuntimeError(
            "request must be a PMReviewRequest instance."
        )
    if not isinstance(actual_exposure, ActiveExposure):
        raise MiroThinkerPMReviewRuntimeError(
            "actual_exposure must be an ActiveExposure instance."
        )
    if actual_exposure.target_key != request.target_key:
        raise MiroThinkerPMReviewRuntimeError(
            "actual_exposure target_key must match request."
        )
    if assessment is not None:
        if not isinstance(assessment, AnalysisAssessment):
            raise MiroThinkerPMReviewRuntimeError(
                "assessment must be an AnalysisAssessment instance when supplied."
            )
        if assessment.target_key != request.target_key:
            raise MiroThinkerPMReviewRuntimeError(
                "assessment target_key must match request."
            )
    portfolio_risk_snapshot = build_pm_portfolio_risk_snapshot(
        request=request,
        actual_exposure=actual_exposure,
        pm_decisions=pm_decisions,
        execution_records=execution_records,
        market_bars=portfolio_risk_market_bars,
    )
    try:
        return PMReviewInput(
            pm_review_request_id=request.request_id,
            target_key=request.target_key,
            business_at=request.business_at,
            source=request.source,
            source_assessment_id=request.source_assessment_id,
            source_episode_id=request.source_episode_id,
            source_event_ids=request.source_event_ids,
            review_reasons=tuple(request.review_reasons),
            current_exposure_required=request.current_exposure_required,
            candidate_review_allowed=request.candidate_review_allowed,
            execution_direction_mode=execution_direction_mode,
            decision_visibility=request.decision_visibility,
            required_price_level_ids=request.required_price_level_ids,
            required_claim_ids=request.required_claim_ids,
            actual_current_state=actual_exposure.state,
            actual_target_weight_before=actual_exposure.target_weight,
            analysis_snapshot=(
                None
                if assessment is None
                else _pm_analysis_snapshot_from_assessment(assessment)
            ),
            portfolio_risk_snapshot=portfolio_risk_snapshot,
            visible_evidence=read_visible_evidence(
                request=request,
                records=evidence_records,
            ).records,
            visible_market_bars=read_visible_market_bars(
                request=request,
                bars=market_bars,
            ).records,
            visible_pm_history=read_visible_pm_history(
                request=request,
                requests=pm_review_requests,
            ).records,
            source_episode_memory_view=source_episode_memory_view,
            source_episode_memory_read_receipt=source_episode_memory_read_receipt,
            candidate_anchor=candidate_anchor,
        )
    except PMReviewContractError as exc:
        raise MiroThinkerPMReviewStructuralRuntimeError(str(exc)) from exc


def build_pm_portfolio_risk_snapshot(
    *,
    request: PMReviewRequest,
    actual_exposure: ActiveExposure,
    pm_decisions: tuple[PMDecision, ...] = (),
    execution_records: tuple[ExecutionRecord, ...] = (),
    market_bars: tuple[MarketDataBar, ...] = (),
) -> PMPortfolioRiskSnapshot | None:
    if actual_exposure.state == "flat":
        return None
    entry_execution = resolve_current_position_entry_execution(
        actual_exposure=actual_exposure,
        pm_decisions=pm_decisions,
        execution_records=execution_records,
        business_at=request.business_at,
    )
    if entry_execution is None or entry_execution.executed_at is None:
        return None
    if entry_execution.adjusted_price is None:
        return None
    direction = _view_state_direction(actual_exposure.state)
    if direction is None:
        return None
    relevant_bars = tuple(
        bar
        for bar in sorted(market_bars, key=lambda item: (item.start_at, item.end_at))
        if entry_execution.executed_at <= bar.end_at <= request.max_visible_market_time
    )
    if not relevant_bars:
        return None
    entry_reference_price = entry_execution.adjusted_price
    current_bar = relevant_bars[-1]
    current_return = _directional_return_pct(
        direction=direction,
        entry_reference_price=entry_reference_price,
        current_mark_price=current_bar.close_price,
    )
    best_return = max(
        _directional_return_pct(
            direction=direction,
            entry_reference_price=entry_reference_price,
            current_mark_price=bar.close_price,
        )
        for bar in relevant_bars
    )
    return PMPortfolioRiskSnapshot(
        position_opened_at=entry_execution.executed_at,
        entry_reference_price=entry_reference_price,
        current_mark_price=current_bar.close_price,
        mark_as_of=current_bar.end_at,
        unrealized_directional_return_pct=current_return,
        drawdown_from_best_mark_pct=max(best_return - current_return, 0.0),
    )


def resolve_current_position_entry_execution(
    *,
    actual_exposure: ActiveExposure,
    pm_decisions: tuple[PMDecision, ...] = (),
    execution_records: tuple[ExecutionRecord, ...] = (),
    business_at: datetime,
) -> ExecutionRecord | None:
    current_direction = _view_state_direction(actual_exposure.state)
    if current_direction is None:
        return None
    pm_decisions_by_id = {decision.decision_id: decision for decision in pm_decisions}
    current_state = "flat"
    entry_execution: ExecutionRecord | None = None
    executed_records = tuple(
        sorted(
            (
                record
                for record in execution_records
                if record.target_key == actual_exposure.target_key
                and record.status == "executed"
                and record.executed_at is not None
                and record.executed_at <= business_at
                and record.pm_decision_id in pm_decisions_by_id
            ),
            key=lambda item: (item.executed_at, item.execution_record_id),
        )
    )
    for record in executed_records:
        decision = pm_decisions_by_id[record.pm_decision_id]
        next_direction = _view_state_direction(decision.requested_state)
        if (
            next_direction == current_direction
            and _view_state_direction(current_state) != current_direction
        ):
            entry_execution = record
        current_state = decision.requested_state
    if _view_state_direction(current_state) != current_direction:
        return None
    return entry_execution


def _view_state_direction(state: str) -> str | None:
    if state in {"weak_long", "strong_long"}:
        return "long"
    if state in {"weak_short", "strong_short"}:
        return "short"
    return None


def _directional_return_pct(
    *,
    direction: str,
    entry_reference_price: float,
    current_mark_price: float,
) -> float:
    if direction == "long":
        return (current_mark_price / entry_reference_price) - 1.0
    if direction == "short":
        return (entry_reference_price / current_mark_price) - 1.0
    raise MiroThinkerPMReviewRuntimeError(
        f"unsupported portfolio risk direction: {direction}"
    )


def parse_pm_decision_draft_output(
    *,
    final_boxed_answer: str,
    final_summary: str,
    fallback_payload_texts: Iterable[str] = (),
    pm_review_input: PMReviewInput | None = None,
) -> PMDecisionDraft:
    """Parse one boxed PMReview decision draft against the thin PM contract."""

    try:
        payload = load_first_boxed_json_object(
            final_boxed_answer=final_boxed_answer,
            final_summary=final_summary,
            fallback_payload_texts=fallback_payload_texts,
            object_start_fields=(
                "pm_review_request_id",
                "target_key",
                "business_at",
            ),
            missing_error="PMReview agent did not return a boxed JSON payload.",
            non_json_error="PMReview agent returned non-JSON boxed output.",
            non_object_error="PMReview agent returned a boxed JSON value that was not an object.",
        )
        draft = parse_pm_decision_draft(payload)
    except (BoxedJsonPayloadError, PMReviewContractError) as exc:
        raise MiroThinkerPMReviewRuntimeError(str(exc)) from exc
    if pm_review_input is None:
        return draft
    if draft.pm_review_request_id != pm_review_input.pm_review_request_id:
        raise MiroThinkerPMReviewRuntimeError(
            "PMDecisionDraft.pm_review_request_id must match PMReviewInput."
        )
    if draft.target_key != pm_review_input.target_key:
        raise MiroThinkerPMReviewRuntimeError(
            "PMDecisionDraft.target_key must match PMReviewInput."
        )
    if draft.business_at != pm_review_input.business_at:
        raise MiroThinkerPMReviewRuntimeError(
            "PMDecisionDraft.business_at must match PMReviewInput.business_at."
        )
    return draft


def _pm_review_role_prompt() -> str:
    return (
        "Role:\n"
        "You are the discretionary portfolio manager for event-trader."
    )
def _pm_review_thin_operating_boundary_prompt() -> str:
    return (
        "Operating boundary:\n"
        "Use PMReview to make the first exposure-aware judgment. Your output surface "
        "is only a thin PMDecisionDraft, and the system materializes downstream "
        "execution truth later."
    )


def _pm_analysis_snapshot_from_assessment(
    assessment: AnalysisAssessment,
) -> PMAnalysisSnapshot:
    snapshot = analysis_snapshot_from_assessment(assessment)
    return replace(
        snapshot,
        if_flat_implication_md=_pm_price_surface_guardrail_md(
            snapshot.if_flat_implication_md
        ),
        if_already_long_implication_md=_pm_price_surface_guardrail_md(
            snapshot.if_already_long_implication_md
        ),
        if_already_short_implication_md=(
            None
            if snapshot.if_already_short_implication_md is None
            else _pm_price_surface_guardrail_md(
                snapshot.if_already_short_implication_md
            )
        ),
    )


def _pm_price_surface_guardrail_md(content_md: str) -> str:
    return f"{_PM_PRICE_SURFACE_NOTE}\n\n{content_md}"


def _preflight_pm_review_tools(available_tool_names: tuple[str, ...] | None) -> None:
    if available_tool_names is None:
        return
    available = set(available_tool_names)
    missing = tuple(
        tool_name
        for tool_name in REQUIRED_PM_REVIEW_TOOL_NAMES
        if tool_name not in available
    )
    if missing:
        raise MiroThinkerPMReviewRuntimeError(
            "PMReview tool preflight failed; missing tool(s): "
            f"{', '.join(missing)}."
        )
    forbidden = tuple(
        tool_name
        for tool_name in available
        if tool_name not in REQUIRED_PM_REVIEW_TOOL_NAMES
    )
    if forbidden:
        raise MiroThinkerPMReviewRuntimeError(
            "PMReview runner must expose only PMReview tools; forbidden tool(s): "
            f"{', '.join(sorted(forbidden))}."
        )


def _validate_layout(layout: WorkspaceLayout) -> None:
    if not isinstance(layout, WorkspaceLayout):
        raise MiroThinkerPMReviewRuntimeError(
            "layout must be a WorkspaceLayout instance."
        )


def _pm_review_task_id(request: PMReviewRequest) -> str:
    return (
        "pm-review:"
        f"{request.target_key}:{request.request_id}:{request.business_at.isoformat()}"
    )


def _pm_review_session_id(request: PMReviewRequest) -> str:
    return f"pm-review-tool-session:{request.request_id}"


def _call_pm_review_tool(
    *,
    toolbox: PMReviewAgentToolbox,
    tool_name: str,
    arguments: Mapping[str, object],
) -> object:
    if tool_name == "read_pm_review_request":
        return toolbox.read_pm_review_request()
    if tool_name == "read_pm_review_evidence":
        raw_event_ids = arguments.get("event_ids", ())
        event_ids = tuple(raw_event_ids) if isinstance(raw_event_ids, list | tuple) else ()
        return toolbox.read_pm_review_evidence(event_ids=event_ids)
    if tool_name == "read_pm_review_market_bars":
        return toolbox.read_pm_review_market_bars()
    if tool_name == "read_pm_review_history":
        return toolbox.read_pm_review_history()
    if tool_name == "read_pm_episode_memory":
        return toolbox.read_pm_episode_memory()
    if tool_name == "read_pm_review_learning_cards":
        return toolbox.read_pm_review_learning_cards()
    if tool_name == "read_pm_review_active_exposure":
        return toolbox.read_pm_review_active_exposure()
    raise MiroThinkerPMReviewRuntimeError(f"unsupported PMReview tool: {tool_name}")


def _pm_review_tool_server_definition() -> dict[str, object]:
    return {
        "name": PM_REVIEW_MIROTHINKER_SERVER_NAME,
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
                    "Read structured pm_review learning cards selected by scope and "
                    "business time."
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


def _prepare_source_episode_memory(
    *,
    session: PMReviewToolSession,
) -> PreparedSourceEpisodeMemory | None:
    request = session.request
    if request.source_episode_id is None:
        return None
    try:
        result = read_pm_episode_memory(
            session=session,
            session_id=session.session_id,
            pm_review_request_id=request.request_id,
        )
    except Exception as exc:
        receipt = _load_episode_memory_read_receipt_for_request(
            layout=session.layout,
            request=request,
        )
        failure_reason = (
            None
            if receipt is None
            else receipt.failure_reason
        )
        raise MiroThinkerPMReviewStructuralRuntimeError(
            "required runtime-owned PM episode memory pre-read failed"
            + (f": {failure_reason}" if failure_reason else ".")
        ) from exc
    receipt = _load_episode_memory_read_receipt_for_request(
        layout=session.layout,
        request=request,
    )
    if receipt is None:
        raise MiroThinkerPMReviewStructuralRuntimeError(
            "required runtime-owned PM episode memory pre-read receipt is missing."
        )
    if receipt.status not in {"read", "empty"}:
        raise MiroThinkerPMReviewStructuralRuntimeError(
            "required runtime-owned PM episode memory pre-read did not finish successfully."
        )
    if len(result.records) != 1 or not isinstance(result.records[0], PMEpisodeMemoryView):
        raise MiroThinkerPMReviewStructuralRuntimeError(
            "required runtime-owned PM episode memory pre-read did not return a "
            "PMEpisodeMemoryView."
        )
    return PreparedSourceEpisodeMemory(
        view=result.records[0],
        detailed_receipt=receipt,
        tool_read_receipt=result.receipt,
    )


def _require_runtime_source_episode_memory_pre_read(
    *,
    request: PMReviewRequest,
    prepared_source_episode_memory: PreparedSourceEpisodeMemory | None,
) -> None:
    if request.source_episode_id is None:
        return
    if prepared_source_episode_memory is None:
        raise MiroThinkerPMReviewStructuralRuntimeError(
            "required runtime-owned PM episode memory pre-read is missing."
        )
    if prepared_source_episode_memory.detailed_receipt.status not in {"read", "empty"}:
        raise MiroThinkerPMReviewStructuralRuntimeError(
            "required runtime-owned PM episode memory pre-read must be read or empty "
            "before PMDecision persistence."
        )


def _load_episode_memory_read_receipt_for_request(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest,
) -> PMReviewEpisodeMemoryReadReceipt | None:
    matches = tuple(
        persisted.record
        for persisted in PMReviewEpisodeMemoryReadReceiptStore(layout).read_records(
            target_key=request.target_key
        )
        if persisted.record.request_id == request.request_id
    )
    if not matches:
        return None
    return sorted(matches, key=lambda item: (item.business_at, item.request_id))[-1]


def _merge_read_receipts(
    *groups: tuple[PMReviewToolReadReceipt, ...],
) -> tuple[PMReviewToolReadReceipt, ...]:
    merged: list[PMReviewToolReadReceipt] = []
    seen: set[str] = set()
    for group in groups:
        for receipt in group:
            if receipt.read_id in seen:
                continue
            seen.add(receipt.read_id)
            merged.append(receipt)
    return tuple(merged)


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
    "MiroThinkerPMReviewResult",
    "MiroThinkerPMReviewRuntimeError",
    "PMReviewAgentToolbox",
    "PMReviewMiroThinkerRunResult",
    "PMReviewMiroThinkerTask",
    "PMReviewTaskRunner",
    "REQUIRED_PM_REVIEW_MCP_TOOLS",
    "REQUIRED_PM_REVIEW_TOOL_NAMES",
    "build_mirothinker_pm_review_runner",
    "build_pm_review_input",
    "build_pm_review_task_prompt",
    "build_production_mirothinker_pm_review_task_runner",
    "parse_pm_decision_draft_output",
    "run_pm_review_request",
]
