"""PM review runtime policy, dispatch, and execution helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from event_trader.analysis_assessment_store import AnalysisAssessmentStore
from event_trader.composition_error import CompositionError
from event_trader.config import (
    KernelConfig,
    resolve_validation_execution_direction_mode,
)
from event_trader.contracts import EvidenceLedgerRecord
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.view_state_change import MarketDataBar
from event_trader.evidence_ledger import FileBackedEvidenceLedger, read_evidence_window
from event_trader.execution import (
    ExecutionCostModel,
    ExecutionRecord,
    ExecutionRecordStore,
    PaperExecutionEngine,
)
from event_trader.integrations.mirothinker_pm_review import (
    REQUIRED_PM_REVIEW_TOOL_NAMES,
    MiroThinkerPMReviewResult,
    MiroThinkerPMReviewRuntimeConfig,
    MiroThinkerPMReviewStructuralRuntimeError,
    PMReviewTaskRunner,
    build_production_mirothinker_pm_review_task_runner,
    resolve_current_position_entry_execution,
    run_pm_review_request,
)
from event_trader.market.adjustments import MarketDataAdjustmentPolicy
from event_trader.market.provider import MarketBarsProvider
from event_trader.market.target_series import read_visible_target_bars
from event_trader.migrations import (
    PMReviewWorkspaceMigrationError,
    validate_pm_review_workspace_ready,
)
from event_trader.migrations.cutover_baseline import DEFAULT_CUTOVER_MIGRATION_ID
from event_trader.pm_review.contracts import (
    PMReviewDispatchConsideration,
    PMReviewFailureRecord,
    PMReviewRequest,
    PMReviewToolReadReceipt,
    derive_pm_review_dispatch_consideration_id,
    derive_pm_review_failure_id,
    is_terminal_pm_review_policy_skip_reason,
)
from event_trader.pm_review.position_review import (
    PMPositionReviewTriggerHit,
    build_triggered_pm_review_request,
    evaluate_pm_position_review_triggers,
)
from event_trader.pm_review.position_review_gate import (
    PMPositionReviewGateConfig,
    PMPositionReviewGateDecision,
    build_pm_position_review_gate_record,
    derive_pm_position_review_gate_key,
    evaluate_pm_position_review_gate,
)
from event_trader.pm_review.store import (
    PMPositionReviewGateStore,
    PMPositionReviewTriggerStore,
    PMReviewDispatchConsiderationStore,
    PMReviewFailureStore,
    PMReviewRequestStore,
    PMReviewToolReadReceiptStore,
)
from event_trader.portfolio import (
    PMDecision,
    PMDecisionStore,
    PortfolioState,
    PortfolioStateStore,
)
from event_trader.portfolio.active_exposure import (
    ActiveExposure,
    resolve_active_exposure,
    validate_active_exposure_runtime,
)
from event_trader.portfolio.pm_execution_flow import (
    PMExecutionFlowResult,
    execute_pm_decision,
)
from event_trader.runtime.pm_review_resolver import RuntimePMReviewResolution
from event_trader.runtime.pm_review_worker import RuntimePMReviewExecutionResult
from event_trader.storage import WorkspaceLayout
from event_trader.validation.market_mapping import resolve_market_mapping

type RuntimeEmitter = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class PMReviewOrchestrationResult:
    """Result of one PMReview request processing helper."""

    request: PMReviewRequest
    active_exposure: ActiveExposure
    analysis_assessment: AnalysisAssessment | None
    pm_review_result: MiroThinkerPMReviewResult


@dataclass(frozen=True, slots=True)
class PMReviewExecutionOrchestrationResult:
    """Result of one PMReview request processing plus execution helper."""

    pm_review_orchestration: PMReviewOrchestrationResult
    execution_flow_result: PMExecutionFlowResult


@dataclass(frozen=True, slots=True)
class PMReviewExecutionInputs:
    """Explicit PMReview execution dependencies for dispatcher callers."""

    execution_engine: PaperExecutionEngine


@dataclass(frozen=True, slots=True)
class PMReviewRuntimePolicy:
    """Explicit runtime policy for analysis-completion PMReview dispatch."""

    auto_dispatch_after_analysis: bool = False
    execute_pm_decisions: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.auto_dispatch_after_analysis, bool):
            raise CompositionError("auto_dispatch_after_analysis must be a boolean.")
        if not isinstance(self.execute_pm_decisions, bool):
            raise CompositionError("execute_pm_decisions must be a boolean.")
        if self.execute_pm_decisions and not self.auto_dispatch_after_analysis:
            raise CompositionError(
                "PMReview execution requires auto_dispatch_after_analysis=true."
            )


@dataclass(frozen=True, slots=True)
class MaterializedPMReviewRequest:
    """Persisted PMReview request plus its canonical record reference."""

    request: PMReviewRequest
    request_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.request, PMReviewRequest):
            raise CompositionError("request must be a PMReviewRequest instance.")
        if not isinstance(self.request_ref, str) or not self.request_ref.strip():
            raise CompositionError("request_ref must be a non-blank string.")


@dataclass(frozen=True, slots=True)
class _TriggerDrivenPMReviewCandidate:
    """One bar-scoped trigger candidate shared by deadline and materialization."""

    trigger_hit: PMPositionReviewTriggerHit
    source_event_ids: tuple[str, ...]
    new_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PMReviewRuntimePreflightReceipt:
    """Preflight result for explicit PMReview runtime enablement."""

    enabled: bool
    execute_approved: bool
    workspace_ready_checked: bool
    runner_checked: bool
    target_keys: tuple[str, ...]


PMReviewLifecycleState = Literal[
    "pending_no_decision",
    "complete_hold_noop",
    "decision_no_execution_requested",
    "decision_without_execution",
    "complete_execution_rejected",
    "complete_executed",
    "after_run_until",
]


@dataclass(frozen=True, slots=True)
class PMReviewLifecycleClassification:
    request_id: str
    state: PMReviewLifecycleState
    execution_record_id: str | None
    pm_decision_id: str | None = None
    failure_id: str | None = None
    failure_retry_allowed: bool | None = None


@dataclass(frozen=True, slots=True)
class PMReviewFailureResult:
    request: PMReviewRequest
    active_exposure: ActiveExposure | None
    analysis_assessment: AnalysisAssessment | None
    failure: PMReviewFailureRecord
    classification_before: PMReviewLifecycleClassification


@dataclass(frozen=True, slots=True)
class PMReviewLifecycleRepairResult:
    request: PMReviewRequest
    active_exposure: ActiveExposure
    analysis_assessment: AnalysisAssessment | None
    read_receipts: tuple[PMReviewToolReadReceipt, ...]
    classification_before: PMReviewLifecycleClassification
    classification_after: PMReviewLifecycleClassification
    execution_flow_result: PMExecutionFlowResult | None = None


@dataclass(frozen=True, slots=True)
class PMReviewDispatchSkip:
    request_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class PMReviewDispatchResult:
    processed: tuple[
        PMReviewOrchestrationResult
        | PMReviewExecutionOrchestrationResult
        | PMReviewFailureResult
        | PMReviewLifecycleRepairResult,
        ...,
    ]
    skipped: tuple[PMReviewDispatchSkip, ...]


def process_pending_pm_review_request(
    *,
    layout: WorkspaceLayout,
    task_runner: PMReviewTaskRunner,
    request: PMReviewRequest | None = None,
    target_key: str | None = None,
    request_id: str | None = None,
    evidence_records: tuple[EvidenceLedgerRecord, ...] | None = None,
    market_bars: tuple[MarketDataBar, ...] = (),
    config: KernelConfig | None = None,
    market_data_provider: MarketBarsProvider | None = None,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
    available_tool_names: tuple[str, ...] = REQUIRED_PM_REVIEW_TOOL_NAMES,
    execution_direction_mode: Literal["long_only", "long_short"] = "long_short",
    decision_available_at: datetime,
    emit: RuntimeEmitter | None = None,
) -> PMReviewOrchestrationResult:
    """Process one persisted PMReviewRequest into an active PMDecision."""

    if not isinstance(layout, WorkspaceLayout):
        raise CompositionError("layout must be a WorkspaceLayout instance.")
    if not callable(task_runner):
        raise CompositionError("task_runner must be callable.")
    active_request = _resolve_pm_review_request(
        layout=layout,
        request=request,
        target_key=target_key,
        request_id=request_id,
    )
    _validate_decision_available_at(
        request=active_request,
        decision_available_at=decision_available_at,
    )
    assessment = _load_linked_analysis_assessment(
        layout=layout,
        request=active_request,
    )
    active_exposure = resolve_active_exposure(
        layout=layout,
        target_key=active_request.target_key,
        migration_id=migration_id,
    )
    visible_input_evidence = (
        evidence_records
        if evidence_records is not None
        else tuple(
            read_evidence(
                list(active_request.source_event_ids),
                ledger=FileBackedEvidenceLedger(layout),
            )
        )
    )
    pm_review_requests = tuple(
        persisted.record
        for persisted in PMReviewRequestStore(layout).read_records(
            target_key=active_request.target_key
        )
    )
    pm_decisions = tuple(
        persisted.record
        for persisted in PMDecisionStore(layout).read_records(
            target_key=active_request.target_key
        )
    )
    execution_records = tuple(
        persisted.record
        for persisted in ExecutionRecordStore(layout).read_records(
            target_key=active_request.target_key
        )
    )
    portfolio_risk_market_bars = build_pm_review_portfolio_risk_market_bars_for_request(
        layout=layout,
        request=active_request,
        active_exposure=active_exposure,
        pm_decisions=pm_decisions,
        execution_records=execution_records,
        config=config,
        market_data_provider=market_data_provider,
        emit=emit,
    )
    pm_review_result = run_pm_review_request(
        layout=layout,
        request=active_request,
        task_runner=task_runner,
        actual_exposure=active_exposure,
        evidence_records=tuple(visible_input_evidence),
        market_bars=tuple(market_bars),
        pm_review_requests=pm_review_requests,
        pm_decisions=pm_decisions,
        execution_records=execution_records,
        portfolio_risk_market_bars=portfolio_risk_market_bars,
        assessment=assessment,
        available_tool_names=available_tool_names,
        execution_direction_mode=execution_direction_mode,
        prompt_max_chars=_pm_review_prompt_max_chars(config),
        decision_available_at=decision_available_at,
    )
    return PMReviewOrchestrationResult(
        request=active_request,
        active_exposure=active_exposure,
        analysis_assessment=assessment,
        pm_review_result=pm_review_result,
    )


def process_pending_pm_review_request_and_execute(
    *,
    layout: WorkspaceLayout,
    task_runner: PMReviewTaskRunner,
    execution_engine: PaperExecutionEngine | None = None,
    request: PMReviewRequest | None = None,
    target_key: str | None = None,
    request_id: str | None = None,
    evidence_records: tuple[EvidenceLedgerRecord, ...] | None = None,
    market_bars: tuple[MarketDataBar, ...] = (),
    config: KernelConfig | None = None,
    market_data_provider: MarketBarsProvider | None = None,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
    available_tool_names: tuple[str, ...] = REQUIRED_PM_REVIEW_TOOL_NAMES,
    execution_direction_mode: Literal["long_only", "long_short"] = "long_short",
    decision_available_at: datetime,
    emit: RuntimeEmitter | None = None,
) -> PMReviewExecutionOrchestrationResult:
    """Process one PMReviewRequest and explicitly continue into PM execution."""

    _validate_pm_review_execution_inputs(execution_engine=execution_engine)
    if execution_engine is None:
        raise CompositionError("execution_engine is required for PMReview execution.")
    pm_review_orchestration = process_pending_pm_review_request(
        layout=layout,
        task_runner=task_runner,
        request=request,
        target_key=target_key,
        request_id=request_id,
        evidence_records=evidence_records,
        market_bars=market_bars,
        config=config,
        market_data_provider=market_data_provider,
        migration_id=migration_id,
        available_tool_names=available_tool_names,
        execution_direction_mode=execution_direction_mode,
        decision_available_at=decision_available_at,
        emit=emit,
    )
    pm_decision = pm_review_orchestration.pm_review_result.pm_decision
    if not pm_decision.execution_required:
        return PMReviewExecutionOrchestrationResult(
            pm_review_orchestration=pm_review_orchestration,
            execution_flow_result=_empty_pm_execution_flow_result(),
        )
    execution_result = execute_pm_decision(
        layout=layout,
        decision=pm_decision,
        execution_engine=execution_engine,
    )
    return PMReviewExecutionOrchestrationResult(
        pm_review_orchestration=pm_review_orchestration,
        execution_flow_result=execution_result,
    )


def build_pm_review_execution_inputs(
    *,
    layout: WorkspaceLayout,
    config: KernelConfig,
    market_data_provider: MarketBarsProvider | None = None,
    execution_engine: PaperExecutionEngine | None = None,
) -> PMReviewExecutionInputs:
    """Build explicit PMReview dispatcher execution inputs from runtime config."""

    if not isinstance(layout, WorkspaceLayout):
        raise CompositionError("layout must be a WorkspaceLayout instance.")
    if not isinstance(config, KernelConfig):
        raise CompositionError("config must be a KernelConfig instance.")
    active_execution_engine = execution_engine
    if active_execution_engine is None:
        active_execution_engine = _build_paper_execution_engine(
            config=config,
            market_data_provider=market_data_provider,
        )
    elif not isinstance(active_execution_engine, PaperExecutionEngine):
        raise CompositionError(
            "execution_engine must be a PaperExecutionEngine instance."
        )
    if active_execution_engine is None:
        raise CompositionError(
            "PMReview execution requires validation market data config or an "
            "explicit PaperExecutionEngine."
        )
    return PMReviewExecutionInputs(execution_engine=active_execution_engine)


def build_pm_review_task_runner(*, config: KernelConfig) -> PMReviewTaskRunner:
    """Build the production PMReview task runner from existing MiroThinker config."""

    if not isinstance(config, KernelConfig):
        raise CompositionError("config must be a KernelConfig instance.")
    pm_review_agent = config.pm_review_agent
    if pm_review_agent is None:
        raise CompositionError(
            "production PMReview runner requires [pm_review_agent] "
            "MiroThinker config."
        )
    try:
        return build_production_mirothinker_pm_review_task_runner(
            config=MiroThinkerPMReviewRuntimeConfig(
                vendor_root=pm_review_agent.vendor_root,
                log_dir=pm_review_agent.log_dir,
                llm_provider=pm_review_agent.llm_provider,
                llm_model_name=pm_review_agent.llm_model_name,
                llm_api_key=pm_review_agent.llm_api_key,
                llm_base_url=pm_review_agent.llm_base_url,
                llm_max_context_length=pm_review_agent.llm_max_context_length,
                llm_reasoning_effort=pm_review_agent.llm_reasoning_effort,
                wall_clock_timeout_seconds=pm_review_agent.wall_clock_timeout_seconds,
            )
        )
    except Exception as exc:
        raise CompositionError(
            f"failed to build production PMReview runner: {exc}"
        ) from exc


def _pm_review_prompt_max_chars(config: KernelConfig | None) -> int | None:
    if config is None or config.pm_review_agent is None:
        return None
    return config.pm_review_agent.llm_max_context_length


def build_runtime_pm_review_executor(
    *,
    layout: WorkspaceLayout,
    task_runner: PMReviewTaskRunner,
    config: KernelConfig | None = None,
    market_data_provider: MarketBarsProvider | None = None,
    execution_engine: PaperExecutionEngine | None = None,
    execute_pm_decisions: bool = False,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
    available_tool_names: tuple[str, ...] = REQUIRED_PM_REVIEW_TOOL_NAMES,
    emit: RuntimeEmitter | None = None,
    now: Callable[[], datetime],
) -> Callable[[RuntimePMReviewResolution], RuntimePMReviewExecutionResult]:
    """Build the runtime-owned PMReview executor from existing PMReview logic."""

    if not isinstance(layout, WorkspaceLayout):
        raise CompositionError("layout must be a WorkspaceLayout instance.")
    if not callable(task_runner):
        raise CompositionError("task_runner must be callable.")
    if config is not None and not isinstance(config, KernelConfig):
        raise CompositionError("config must be a KernelConfig instance when supplied.")
    if execution_engine is not None and not isinstance(execution_engine, PaperExecutionEngine):
        raise CompositionError(
            "execution_engine must be a PaperExecutionEngine instance when supplied."
        )
    if not isinstance(execute_pm_decisions, bool):
        raise CompositionError("execute_pm_decisions must be a boolean.")
    if not callable(now):
        raise CompositionError("now must be callable.")
    active_execution_engine = execution_engine
    if execute_pm_decisions:
        validate_active_exposure_runtime(
            layout=layout,
            migration_id=migration_id,
        )
        if active_execution_engine is None:
            if config is None:
                raise CompositionError(
                    "runtime PMReview execution requires config or an explicit "
                    "execution_engine when execute_pm_decisions=true."
                )
            active_execution_engine = build_pm_review_execution_inputs(
                layout=layout,
                config=config,
                market_data_provider=market_data_provider,
            ).execution_engine

    def _execute(resolution: RuntimePMReviewResolution) -> RuntimePMReviewExecutionResult:
        if not isinstance(resolution, RuntimePMReviewResolution):
            raise CompositionError(
                "resolution must be a RuntimePMReviewResolution instance."
            )
        request = resolution.request
        decision_available_at = max(now(), request.business_at)
        market_bars: tuple[MarketDataBar, ...] = ()
        if config is not None:
            market_bars = build_pm_review_visible_market_bars_for_requests(
                layout=layout,
                target_key=request.target_key,
                run_until=request.max_visible_market_time,
                config=config,
                market_data_provider=market_data_provider,
                emit=emit,
            )
        try:
            execution_direction_mode = (
                "long_short"
                if config is None
                else resolve_validation_execution_direction_mode(
                    config,
                    target_key=request.target_key,
                )
            )
            if active_execution_engine is None:
                orchestration = process_pending_pm_review_request(
                    layout=layout,
                    task_runner=task_runner,
                    request=request,
                    market_bars=market_bars,
                    config=config,
                    market_data_provider=market_data_provider,
                    migration_id=migration_id,
                    available_tool_names=available_tool_names,
                    execution_direction_mode=execution_direction_mode,
                    decision_available_at=decision_available_at,
                    emit=emit,
                )
                pm_decision = orchestration.pm_review_result.pm_decision
            else:
                execution_orchestration = process_pending_pm_review_request_and_execute(
                    layout=layout,
                    task_runner=task_runner,
                    execution_engine=active_execution_engine,
                    request=request,
                    market_bars=market_bars,
                    config=config,
                    market_data_provider=market_data_provider,
                    migration_id=migration_id,
                    available_tool_names=available_tool_names,
                    execution_direction_mode=execution_direction_mode,
                    decision_available_at=decision_available_at,
                    emit=emit,
                )
                pm_decision = (
                    execution_orchestration.pm_review_orchestration
                    .pm_review_result
                    .pm_decision
                )
        except Exception as exc:
            classification_before = _classify_pm_review_request(
                request=request,
                pm_decisions=tuple(
                    persisted.record
                    for persisted in PMDecisionStore(layout).read_records(
                        target_key=request.target_key
                    )
                ),
                execution_records=tuple(
                    persisted.record
                    for persisted in ExecutionRecordStore(layout).read_records(
                        target_key=request.target_key
                    )
                ),
                failure_records=tuple(
                    persisted.record
                    for persisted in PMReviewFailureStore(layout).read_records(
                        target_key=request.target_key
                    )
                ),
                execute_approved=execute_pm_decisions,
            )
            failure_result = _record_pm_review_failure_result(
                layout=layout,
                request=request,
                classification_before=classification_before,
                stage="pm_review_run",
                error=exc,
                migration_id=migration_id,
                failed_at=decision_available_at,
            )
            return RuntimePMReviewExecutionResult(
                status="failed",
                failure_record_ref=_pm_review_failure_record_ref(
                    layout=layout,
                    failure=failure_result.failure,
                ),
                failure_reason="pm_review_run_failed",
                failure_error=str(exc).strip() or repr(exc),
            )
        return RuntimePMReviewExecutionResult(
            status="completed",
            pm_decision_ref=_pm_decision_record_ref(
                layout=layout,
                decision=pm_decision,
            ),
        )

    return _execute


def dispatch_due_pm_reviews_for_target(
    *,
    layout: WorkspaceLayout,
    config: KernelConfig,
    target_key: str,
    run_until: datetime,
    policy: PMReviewRuntimePolicy,
    task_runner: PMReviewTaskRunner,
    market_data_provider: MarketBarsProvider | None,
    emit: RuntimeEmitter,
    runtime_owned_analysis_pm_review: bool,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
) -> None:
    """Dispatch all currently due PMReview work for one target."""
    try:
        _materialize_trigger_driven_pm_review_requests(
            layout=layout,
            config=config,
            run_until=run_until,
            market_data_provider=market_data_provider,
            emit=emit,
            target_keys=(target_key,),
        )
        active_market_bars = build_pm_review_visible_market_bars_for_requests(
            layout=layout,
            target_key=target_key,
            run_until=run_until,
            config=config,
            market_data_provider=market_data_provider,
            emit=emit,
        )
        active_execution_inputs = None
        if policy.execute_pm_decisions:
            validate_active_exposure_runtime(
                layout=layout,
                migration_id=migration_id,
            )
            active_execution_inputs = build_pm_review_execution_inputs(
                layout=layout,
                config=config,
                market_data_provider=market_data_provider,
            )
    except Exception as exc:
        setup_failure_result = _record_pm_review_setup_failure_dispatch_result(
            layout=layout,
            target_key=target_key,
            run_until=run_until,
            execute_approved=policy.execute_pm_decisions,
            error=exc,
            migration_id=migration_id,
        )
        if setup_failure_result is None:
            raise
        _emit_pm_review_dispatch_summary(
            emit=emit,
            target_key=target_key,
            run_until=run_until,
            dispatch_result=setup_failure_result,
        )
        return
    dispatch_result = dispatch_pending_pm_review_requests(
        layout=layout,
        target_key=target_key,
        run_until=run_until,
        task_runner=task_runner,
        runtime_owned_analysis_pm_review=runtime_owned_analysis_pm_review,
        execute_approved=policy.execute_pm_decisions,
        execution_engine=(
            None
            if active_execution_inputs is None
            else active_execution_inputs.execution_engine
        ),
        execution_direction_mode=resolve_validation_execution_direction_mode(
            config,
            target_key=target_key,
        ),
        market_bars=active_market_bars,
        config=config,
        market_data_provider=market_data_provider,
        migration_id=migration_id,
        emit=emit,
    )
    _emit_pm_review_dispatch_summary(
        emit=emit,
        target_key=target_key,
        run_until=run_until,
        dispatch_result=dispatch_result,
    )


def build_pm_review_visible_market_bars_for_requests(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_until: datetime,
    config: KernelConfig | None,
    market_data_provider: MarketBarsProvider | None = None,
    emit: RuntimeEmitter | None = None,
) -> tuple[MarketDataBar, ...]:
    """Read candidate PMReview market bars bounded by pending request visibility."""

    if not isinstance(layout, WorkspaceLayout):
        raise CompositionError("layout must be a WorkspaceLayout instance.")
    if not isinstance(run_until, datetime) or run_until.tzinfo is None:
        raise CompositionError("run_until must be a timezone-aware datetime.")
    requests = tuple(
        persisted.record
        for persisted in PMReviewRequestStore(layout).read_records(
            target_key=target_key
        )
        if persisted.record.business_at <= run_until
    )
    if not requests:
        return ()
    if config is None:
        _emit_pm_review_market_bars_message(
            emit=emit,
            target_key=target_key,
            message="market config unavailable",
        )
        return ()
    if not isinstance(config, KernelConfig):
        raise CompositionError("config must be a KernelConfig instance when supplied.")
    if market_data_provider is None:
        _emit_pm_review_market_bars_message(
            emit=emit,
            target_key=target_key,
            message="market data provider unavailable",
        )
        return ()
    visibility_bound = min(
        max(request.max_visible_market_time for request in requests),
        run_until,
    )
    lookback_hours = config.execution.max_next_bar_wait_hours
    requested_start = visibility_bound - timedelta(hours=lookback_hours)
    try:
        mapping = resolve_market_mapping(config, target_key)
        visible = read_visible_target_bars(
            market_data=market_data_provider,
            market_mapping=mapping,
            start_at=requested_start,
            end_at=visibility_bound,
            adjustment_policy=_pm_review_visible_adjustment_policy(
                config=config,
                target_key=target_key,
                market_symbol=mapping.market_symbol,
            ),
        )
    except Exception as exc:
        if _is_no_prefetched_bars_in_requested_window(exc):
            _emit_pm_review_market_bars_message(
                emit=emit,
                target_key=target_key,
                message=(
                    "no_market_observation "
                    f"start={requested_start.isoformat()} "
                    f"end={visibility_bound.isoformat()}"
                ),
            )
            return ()
        _emit_pm_review_market_bars_message(
            emit=emit,
            target_key=target_key,
            message=f"market data read failed: {exc}",
        )
        return ()
    _emit_pm_review_market_bars_message(
        emit=emit,
        target_key=target_key,
        message=(
            f"visible_bars={len(visible)} "
            f"visibility_bound={visibility_bound.isoformat()}"
        ),
    )
    return visible


def build_pm_review_portfolio_risk_market_bars_for_request(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest,
    active_exposure: ActiveExposure,
    pm_decisions: tuple[PMDecision, ...],
    execution_records: tuple[ExecutionRecord, ...],
    config: KernelConfig | None,
    market_data_provider: MarketBarsProvider | None = None,
    emit: RuntimeEmitter | None = None,
) -> tuple[MarketDataBar, ...]:
    if active_exposure.state == "flat":
        return ()
    if config is None:
        _emit_pm_review_market_bars_message(
            emit=emit,
            target_key=request.target_key,
            message="portfolio risk snapshot skipped: market config unavailable",
        )
        return ()
    if not isinstance(config, KernelConfig):
        raise CompositionError("config must be a KernelConfig instance when supplied.")
    if market_data_provider is None:
        _emit_pm_review_market_bars_message(
            emit=emit,
            target_key=request.target_key,
            message="portfolio risk snapshot skipped: market data provider unavailable",
        )
        return ()
    entry_execution = resolve_current_position_entry_execution(
        actual_exposure=active_exposure,
        pm_decisions=pm_decisions,
        execution_records=execution_records,
        business_at=request.business_at,
    )
    if entry_execution is None or entry_execution.executed_at is None:
        _emit_pm_review_market_bars_message(
            emit=emit,
            target_key=request.target_key,
            message="portfolio risk snapshot skipped: entry execution unavailable",
        )
        return ()
    try:
        mapping = resolve_market_mapping(config, request.target_key)
        visible = read_visible_target_bars(
            market_data=market_data_provider,
            market_mapping=mapping,
            start_at=entry_execution.executed_at,
            end_at=request.max_visible_market_time,
            adjustment_policy=_pm_review_visible_adjustment_policy(
                config=config,
                target_key=request.target_key,
                market_symbol=mapping.market_symbol,
            ),
        )
    except Exception as exc:
        if _is_no_prefetched_bars_in_requested_window(exc):
            _emit_pm_review_market_bars_message(
                emit=emit,
                target_key=request.target_key,
                message=(
                    "portfolio risk snapshot skipped: no_market_observation "
                    f"start={entry_execution.executed_at.isoformat()} "
                    f"end={request.max_visible_market_time.isoformat()}"
                ),
            )
            return ()
        _emit_pm_review_market_bars_message(
            emit=emit,
            target_key=request.target_key,
            message=f"portfolio risk snapshot market data read failed: {exc}",
        )
        return ()
    risk_bars = tuple(
        sorted(
            (
                bar
                for bar in visible
                if entry_execution.executed_at <= bar.end_at <= request.max_visible_market_time
            ),
            key=lambda bar: (bar.start_at, bar.end_at),
        )
    )
    _emit_pm_review_market_bars_message(
        emit=emit,
        target_key=request.target_key,
        message=(
            "portfolio risk snapshot "
            f"bars={len(risk_bars)} "
            f"entry_at={entry_execution.executed_at.isoformat()} "
            f"visibility_bound={request.max_visible_market_time.isoformat()}"
        ),
    )
    return risk_bars


def pm_review_runtime_policy_from_config(config: KernelConfig) -> PMReviewRuntimePolicy:
    """Translate typed config into the PMReview runtime policy."""

    if not isinstance(config, KernelConfig):
        raise CompositionError("config must be a KernelConfig instance.")
    runtime = config.pm_review_runtime
    return PMReviewRuntimePolicy(
        auto_dispatch_after_analysis=runtime.auto_dispatch_after_analysis,
        execute_pm_decisions=runtime.execute_pm_decisions,
    )


def validate_pm_review_runtime_preflight(
    *,
    config: KernelConfig,
    layout: WorkspaceLayout,
    target_keys: tuple[str, ...],
    task_runner: PMReviewTaskRunner | None = None,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
) -> PMReviewRuntimePreflightReceipt:
    """Fail fast before replay/live uses enabled PMReview runtime routing."""

    if not isinstance(config, KernelConfig):
        raise CompositionError("config must be a KernelConfig instance.")
    if not isinstance(layout, WorkspaceLayout):
        raise CompositionError("layout must be a WorkspaceLayout instance.")
    normalized_targets = tuple(target_keys)
    policy = pm_review_runtime_policy_from_config(config)
    workspace_checked = False
    runner_checked = False
    if not policy.auto_dispatch_after_analysis:
        return PMReviewRuntimePreflightReceipt(
            enabled=False,
            execute_approved=False,
            workspace_ready_checked=False,
            runner_checked=False,
            target_keys=normalized_targets,
        )
    if config.pm_review_runtime.require_workspace_ready:
        if not normalized_targets:
            raise CompositionError(
                "PMReview runtime preflight requires target_keys for workspace "
                "schema/baseline validation."
            )
        try:
            validate_pm_review_workspace_ready(
                layout=layout,
                target_keys=normalized_targets,
                migration_id=migration_id,
            )
        except PMReviewWorkspaceMigrationError as exc:
            raise CompositionError(
                "PMReview runtime workspace is not ready: " + str(exc)
            ) from exc
        workspace_checked = True
    if policy.auto_dispatch_after_analysis:
        if task_runner is None:
            build_pm_review_task_runner(config=config)
            runner_checked = True
        elif not callable(task_runner):
            raise CompositionError(
                "PMReview runtime preflight requires a callable runner."
            )
        else:
            runner_checked = True
    return PMReviewRuntimePreflightReceipt(
        enabled=True,
        execute_approved=policy.execute_pm_decisions,
        workspace_ready_checked=workspace_checked,
        runner_checked=runner_checked,
        target_keys=normalized_targets,
    )


def dispatch_pending_pm_review_requests(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_until: datetime,
    task_runner: PMReviewTaskRunner,
    runtime_owned_analysis_pm_review: bool = False,
    execute_approved: bool = False,
    execution_engine: PaperExecutionEngine | None = None,
    evidence_records: tuple[EvidenceLedgerRecord, ...] | None = None,
    market_bars: tuple[MarketDataBar, ...] = (),
    config: KernelConfig | None = None,
    market_data_provider: MarketBarsProvider | None = None,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
    available_tool_names: tuple[str, ...] = REQUIRED_PM_REVIEW_TOOL_NAMES,
    execution_direction_mode: Literal["long_only", "long_short"] = "long_short",
    emit: RuntimeEmitter | None = None,
) -> PMReviewDispatchResult:
    """Explicitly process pending PMReviewRequest records up to run_until."""

    if not isinstance(layout, WorkspaceLayout):
        raise CompositionError("layout must be a WorkspaceLayout instance.")
    if not isinstance(run_until, datetime) or run_until.tzinfo is None:
        raise CompositionError("run_until must be a timezone-aware datetime.")
    if not callable(task_runner):
        raise CompositionError("task_runner must be callable.")
    request_records = tuple(
        persisted.record
        for persisted in PMReviewRequestStore(layout).read_records(target_key=target_key)
    )
    existing_pm_decisions = tuple(
        persisted.record
        for persisted in PMDecisionStore(layout).read_records(target_key=target_key)
    )
    existing_executions = tuple(
        persisted.record
        for persisted in ExecutionRecordStore(layout).read_records(target_key=target_key)
    )
    existing_failures = tuple(
        persisted.record
        for persisted in PMReviewFailureStore(layout).read_records(target_key=target_key)
    )
    processed: list[
        PMReviewOrchestrationResult
        | PMReviewExecutionOrchestrationResult
        | PMReviewFailureResult
        | PMReviewLifecycleRepairResult
    ] = []
    skipped: list[PMReviewDispatchSkip] = []
    for request in sorted(
        request_records,
        key=lambda item: (item.business_at, item.request_id),
    ):
        if request.business_at > run_until:
            skipped.append(
                PMReviewDispatchSkip(
                    request_id=request.request_id,
                    reason="after_run_until",
                )
            )
            continue
        if runtime_owned_analysis_pm_review and request.source == "analysis_event":
            skipped.append(
                PMReviewDispatchSkip(
                    request_id=request.request_id,
                    reason="runtime_pm_review_owned_by_worker",
                )
            )
            continue
        classification = _classify_pm_review_request(
            request=request,
            pm_decisions=existing_pm_decisions,
            execution_records=existing_executions,
            failure_records=existing_failures,
            execute_approved=execute_approved,
        )
        if classification.state == "pending_no_decision":
            if classification.failure_retry_allowed is False:
                skipped.append(
                    PMReviewDispatchSkip(
                        request_id=request.request_id,
                        reason="pm_review_failure_retry_blocked",
                    )
                )
                continue
            auto_dispatch_skip = _resolve_pending_pm_review_auto_dispatch_skip(
                layout=layout,
                request=request,
                migration_id=migration_id,
            )
            if auto_dispatch_skip is not None:
                skipped.append(auto_dispatch_skip)
                continue
            try:
                pm_review_only_result = process_pending_pm_review_request(
                    layout=layout,
                    request=request,
                    task_runner=task_runner,
                    evidence_records=evidence_records,
                    market_bars=market_bars,
                    config=config,
                    market_data_provider=market_data_provider,
                    migration_id=migration_id,
                    available_tool_names=available_tool_names,
                    execution_direction_mode=execution_direction_mode,
                    decision_available_at=run_until,
                    emit=emit,
                )
            except Exception as exc:
                failure_result = _record_pm_review_failure_result(
                    layout=layout,
                    request=request,
                    classification_before=classification,
                    stage=(
                        "pm_review_run_and_execute"
                        if execute_approved
                        else "pm_review_run"
                    ),
                    error=exc,
                    migration_id=migration_id,
                    failed_at=run_until,
                )
                processed.append(failure_result)
                existing_failures = existing_failures + (failure_result.failure,)
                continue
            existing_pm_decisions = existing_pm_decisions + (
                pm_review_only_result.pm_review_result.pm_decision,
            )
            if execute_approved:
                pm_decision = pm_review_only_result.pm_review_result.pm_decision
                if not pm_decision.execution_required:
                    result = PMReviewExecutionOrchestrationResult(
                        pm_review_orchestration=pm_review_only_result,
                        execution_flow_result=_empty_pm_execution_flow_result(),
                    )
                else:
                    result = PMReviewExecutionOrchestrationResult(
                        pm_review_orchestration=pm_review_only_result,
                        execution_flow_result=_execute_existing_pm_review_decision(
                            layout=layout,
                            pm_decision=pm_decision,
                            execution_engine=execution_engine,
                        ),
                    )
                processed.append(result)
                if result.execution_flow_result.execution_record is not None:
                    existing_executions = existing_executions + (
                        result.execution_flow_result.execution_record,
                    )
            else:
                processed.append(pm_review_only_result)
            continue
        if classification.state == "decision_without_execution" and execute_approved:
            try:
                repair = _continue_pm_review_execution_from_existing_decision(
                    layout=layout,
                    request=request,
                    classification_before=classification,
                    pm_decisions=existing_pm_decisions,
                    execution_records=existing_executions,
                    execution_engine=execution_engine,
                    migration_id=migration_id,
                )
            except Exception as exc:
                failure_result = _record_pm_review_failure_result(
                    layout=layout,
                    request=request,
                    classification_before=classification,
                    stage="pm_review_run_and_execute",
                    error=exc,
                    migration_id=migration_id,
                    failed_at=run_until,
                )
                processed.append(failure_result)
                existing_failures = existing_failures + (failure_result.failure,)
                continue
            processed.append(repair)
            if (
                repair.execution_flow_result is not None
                and repair.execution_flow_result.execution_record is not None
            ):
                existing_executions = existing_executions + (
                    repair.execution_flow_result.execution_record,
                )
            continue
        skipped.append(
            PMReviewDispatchSkip(
                request_id=request.request_id,
                reason=_skip_reason_for_pm_review_lifecycle(classification),
            )
        )
    dispatch_result = PMReviewDispatchResult(
        processed=tuple(processed),
        skipped=tuple(skipped),
    )
    _persist_pm_review_dispatch_considerations(
        layout=layout,
        run_until=run_until,
        request_lookup={request.request_id: request for request in request_records},
        dispatch_result=dispatch_result,
    )
    return dispatch_result


def resolve_pm_review_runtime_policy(
    *,
    policy: PMReviewRuntimePolicy | None,
    enabled: bool,
    execute_approved: bool,
    config: KernelConfig | None = None,
) -> PMReviewRuntimePolicy:
    if policy is not None:
        if not isinstance(policy, PMReviewRuntimePolicy):
            raise CompositionError("policy must be a PMReviewRuntimePolicy instance.")
        if enabled or execute_approved:
            raise CompositionError(
                "PMReview dispatch accepts either policy or legacy enabled/"
                "execute_approved flags, not both."
            )
        return policy
    if enabled or execute_approved:
        return PMReviewRuntimePolicy(
            auto_dispatch_after_analysis=enabled,
            execute_pm_decisions=execute_approved,
        )
    if config is not None:
        if not isinstance(config, KernelConfig):
            raise CompositionError("config must be a KernelConfig instance.")
        return pm_review_runtime_policy_from_config(config)
    return PMReviewRuntimePolicy(
        auto_dispatch_after_analysis=False,
        execute_pm_decisions=False,
    )


def _validate_decision_available_at(
    *,
    request: PMReviewRequest,
    decision_available_at: datetime,
) -> None:
    if not isinstance(decision_available_at, datetime) or decision_available_at.tzinfo is None:
        raise CompositionError(
            "decision_available_at must be a timezone-aware datetime."
        )
    if decision_available_at < request.business_at:
        raise CompositionError(
            "decision_available_at must be at or after PMReviewRequest.business_at."
        )


def pm_review_preflight_target_keys_from_config(
    config: KernelConfig,
) -> tuple[str, ...]:
    live_config = config.live
    if live_config is None:
        return ()
    target_keys: list[str] = []
    if live_config.web_search is not None:
        target_keys.extend(target.target_key for target in live_config.web_search.targets)
    if live_config.market_news is not None:
        target_keys.extend(
            target.target_key for target in live_config.market_news.targets
        )
    if live_config.market_data is not None:
        target_keys.extend(
            target.target_key for target in live_config.market_data.targets
        )
    return tuple(dict.fromkeys(target_keys))


def _resolve_pending_pm_review_auto_dispatch_skip(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest,
    migration_id: str,
) -> PMReviewDispatchSkip | None:
    if request.source != "analysis_event" or request.current_exposure_required is not True:
        return None
    if request.candidate_review_allowed:
        return None
    active_exposure = resolve_active_exposure(
        layout=layout,
        target_key=request.target_key,
        migration_id=migration_id,
    )
    if active_exposure.state != "flat":
        return None
    return PMReviewDispatchSkip(
        request_id=request.request_id,
        reason="analysis_event_current_exposure_flat",
    )


def _persist_pm_review_dispatch_considerations(
    *,
    layout: WorkspaceLayout,
    run_until: datetime,
    request_lookup: dict[str, PMReviewRequest],
    dispatch_result: PMReviewDispatchResult,
) -> None:
    store = PMReviewDispatchConsiderationStore(layout)
    for consideration in _build_pm_review_dispatch_considerations(
        run_until=run_until,
        request_lookup=request_lookup,
        dispatch_result=dispatch_result,
    ):
        store.append(consideration)


def _build_pm_review_dispatch_considerations(
    *,
    run_until: datetime,
    request_lookup: dict[str, PMReviewRequest],
    dispatch_result: PMReviewDispatchResult,
) -> tuple[PMReviewDispatchConsideration, ...]:
    rows: list[PMReviewDispatchConsideration] = []
    for skipped in dispatch_result.skipped:
        request = _dispatch_request_for_outcome(skipped, request_lookup)
        rows.append(
            PMReviewDispatchConsideration(
                consideration_id=derive_pm_review_dispatch_consideration_id(
                    request_id=request.request_id,
                    dispatch_run_until=run_until,
                    outcome="skipped",
                    skip_reason=skipped.reason,
                ),
                request_id=request.request_id,
                target_key=request.target_key,
                business_at=request.business_at,
                dispatch_run_until=run_until,
                outcome="skipped",
                skip_reason=skipped.reason,
            )
        )
    for processed in dispatch_result.processed:
        request = _dispatch_processed_request(processed)
        rows.append(
            PMReviewDispatchConsideration(
                consideration_id=derive_pm_review_dispatch_consideration_id(
                    request_id=request.request_id,
                    dispatch_run_until=run_until,
                    outcome="processed",
                    skip_reason=None,
                ),
                request_id=request.request_id,
                target_key=request.target_key,
                business_at=request.business_at,
                dispatch_run_until=run_until,
                outcome="processed",
                skip_reason=None,
            )
        )
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                item.business_at,
                item.request_id,
                item.outcome,
                "" if item.skip_reason is None else item.skip_reason,
            ),
        )
    )


def _dispatch_request_for_outcome(
    skipped: PMReviewDispatchSkip,
    request_lookup: dict[str, PMReviewRequest],
) -> PMReviewRequest:
    try:
        return request_lookup[skipped.request_id]
    except KeyError as exc:
        raise CompositionError(
            "PMReview dispatch skipped request missing request payload: "
            f"{skipped.request_id}"
        ) from exc


def _dispatch_processed_request(
    item: (
        PMReviewOrchestrationResult
        | PMReviewExecutionOrchestrationResult
        | PMReviewFailureResult
        | PMReviewLifecycleRepairResult
    ),
) -> PMReviewRequest:
    if isinstance(item, PMReviewExecutionOrchestrationResult):
        return item.pm_review_orchestration.request
    if isinstance(item, PMReviewOrchestrationResult):
        return item.request
    if isinstance(item, PMReviewFailureResult):
        return item.request
    if isinstance(item, PMReviewLifecycleRepairResult):
        return item.request
    if hasattr(item, "pm_review_orchestration"):
        return item.pm_review_orchestration.request
    if hasattr(item, "request"):
        return item.request
    raise CompositionError("unsupported PMReview dispatch processed result.")


def _resolve_pm_review_request(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest | None,
    target_key: str | None,
    request_id: str | None,
) -> PMReviewRequest:
    if request is not None:
        if not isinstance(request, PMReviewRequest):
            raise CompositionError("request must be a PMReviewRequest instance.")
        return request
    if not isinstance(target_key, str) or not target_key.strip():
        raise CompositionError("target_key is required when request is not supplied.")
    if not isinstance(request_id, str) or not request_id.strip():
        raise CompositionError("request_id is required when request is not supplied.")
    matches = tuple(
        persisted.record
        for persisted in PMReviewRequestStore(layout).read_records(target_key=target_key)
        if persisted.record.request_id == request_id
    )
    if not matches:
        raise CompositionError(f"PMReviewRequest not found: {request_id}")
    if len(matches) > 1:
        raise CompositionError(f"PMReviewRequest id is ambiguous: {request_id}")
    return matches[0]


def _record_pm_review_failure_result(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest,
    classification_before: PMReviewLifecycleClassification,
    stage: Literal["pm_review_run", "pm_review_run_and_execute"],
    error: Exception,
    migration_id: str,
    failed_at: datetime,
) -> PMReviewFailureResult:
    error_message = str(error).strip() or repr(error)
    if not isinstance(failed_at, datetime) or failed_at.tzinfo is None:
        raise CompositionError("failed_at must be a timezone-aware datetime.")
    failure_store = PMReviewFailureStore(layout)
    existing_failure_ids = {
        persisted.record.failure_id
        for persisted in failure_store.read_records(target_key=request.target_key)
    }
    failure_id = derive_pm_review_failure_id(
        pm_review_request_id=request.request_id,
        stage=stage,
        error_type=type(error).__name__,
        error_message=error_message,
        failed_at=failed_at,
    )
    while failure_id in existing_failure_ids:
        failed_at = failed_at + timedelta(microseconds=1)
        failure_id = derive_pm_review_failure_id(
            pm_review_request_id=request.request_id,
            stage=stage,
            error_type=type(error).__name__,
            error_message=error_message,
            failed_at=failed_at,
        )
    failure = PMReviewFailureRecord(
        failure_id=failure_id,
        pm_review_request_id=request.request_id,
        target_key=request.target_key,
        business_at=request.business_at,
        failed_at=failed_at,
        stage=stage,
        error_type=type(error).__name__,
        error_message=error_message,
        retry_allowed=_pm_review_failure_retry_allowed(error),
    )
    failure_store.append(failure)
    try:
        active_exposure = resolve_active_exposure(
            layout=layout,
            target_key=request.target_key,
            migration_id=migration_id,
        )
    except Exception:
        active_exposure = None
    try:
        assessment = _load_linked_analysis_assessment(layout=layout, request=request)
    except Exception:
        assessment = None
    return PMReviewFailureResult(
        request=request,
        active_exposure=active_exposure,
        analysis_assessment=assessment,
        failure=failure,
        classification_before=classification_before,
    )


def _pm_review_failure_record_ref(
    *,
    layout: WorkspaceLayout,
    failure: PMReviewFailureRecord,
) -> str:
    if not isinstance(layout, WorkspaceLayout):
        raise CompositionError("layout must be a WorkspaceLayout instance.")
    if not isinstance(failure, PMReviewFailureRecord):
        raise CompositionError("failure must be a PMReviewFailureRecord instance.")
    path = PMReviewFailureStore(layout).path_for(failure.target_key, failure.business_at)
    return f"{path.resolve(strict=False)}#{failure.failure_id}"


def _pm_decision_record_ref(
    *,
    layout: WorkspaceLayout,
    decision: PMDecision,
) -> str:
    if not isinstance(layout, WorkspaceLayout):
        raise CompositionError("layout must be a WorkspaceLayout instance.")
    if not isinstance(decision, PMDecision):
        raise CompositionError("decision must be a PMDecision instance.")
    path = PMDecisionStore(layout).path_for(decision.target_key, decision.business_at)
    return f"{path.resolve(strict=False)}#{decision.decision_id}"


def _pm_review_failure_retry_allowed(error: Exception) -> bool:
    if isinstance(error, MiroThinkerPMReviewStructuralRuntimeError):
        return False
    return True


def _record_pm_review_setup_failure_dispatch_result(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_until: datetime,
    execute_approved: bool,
    error: Exception,
    migration_id: str,
) -> PMReviewDispatchResult | None:
    requests = _due_pm_review_requests_without_pm_decision(
        layout=layout,
        target_key=target_key,
        run_until=run_until,
        migration_id=migration_id,
    )
    if not requests:
        return None
    failures = tuple(
        persisted.record
        for persisted in PMReviewFailureStore(layout).read_records(target_key=target_key)
    )
    processed: list[PMReviewFailureResult] = []
    for request in requests:
        classification = _classify_pm_review_request(
            request=request,
            pm_decisions=(),
            execution_records=(),
            failure_records=failures,
            execute_approved=execute_approved,
        )
        failure_result = _record_pm_review_failure_result(
            layout=layout,
            request=request,
            classification_before=classification,
            stage=(
                "pm_review_run_and_execute"
                if execute_approved
                else "pm_review_run"
            ),
            error=error,
            migration_id=migration_id,
            failed_at=run_until,
        )
        processed.append(failure_result)
        failures = failures + (failure_result.failure,)
    return PMReviewDispatchResult(processed=tuple(processed), skipped=())


def _due_pm_review_requests_without_pm_decision(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_until: datetime,
    migration_id: str,
) -> tuple[PMReviewRequest, ...]:
    requests = tuple(
        persisted.record
        for persisted in PMReviewRequestStore(layout).read_records(target_key=target_key)
        if persisted.record.business_at <= run_until
    )
    if not requests:
        return ()
    request_ids_with_decision = {
        persisted.record.pm_review_request_id
        for persisted in PMDecisionStore(layout).read_records(target_key=target_key)
        if persisted.record.pm_review_request_id is not None
    }
    return tuple(
        request
        for request in sorted(requests, key=lambda item: (item.business_at, item.request_id))
        if request.request_id not in request_ids_with_decision
        and not is_terminal_pm_review_policy_skip_reason(
            skip.reason
            if (
                skip := _resolve_pending_pm_review_auto_dispatch_skip(
                    layout=layout,
                    request=request,
                    migration_id=migration_id,
                )
            )
            is not None
            else None
        )
    )


def _materialize_trigger_driven_pm_review_requests(
    *,
    layout: WorkspaceLayout,
    config: KernelConfig,
    run_until: datetime,
    market_data_provider: MarketBarsProvider | None,
    emit: RuntimeEmitter,
    target_keys: tuple[str, ...] | None = None,
) -> tuple[MaterializedPMReviewRequest, ...]:
    if not isinstance(layout, WorkspaceLayout):
        raise CompositionError("layout must be a WorkspaceLayout instance.")
    if not isinstance(config, KernelConfig):
        raise CompositionError("config must be a KernelConfig instance.")
    if not isinstance(run_until, datetime) or run_until.tzinfo is None:
        raise CompositionError("run_until must be a timezone-aware datetime.")
    if market_data_provider is None:
        return ()
    request_store = PMReviewRequestStore(layout)
    decision_store = PMDecisionStore(layout)
    trigger_store = PMPositionReviewTriggerStore(layout)
    gate_store = PMPositionReviewGateStore(layout)
    gate_config = PMPositionReviewGateConfig(
        enabled=config.pm_review_runtime.position_review_gate_enabled,
        review_cooldown_minutes=(
            config.pm_review_runtime.position_review_cooldown_minutes
        ),
        rearm_buffer_bps=config.pm_review_runtime.position_review_rearm_buffer_bps,
    )
    created_requests: list[MaterializedPMReviewRequest] = []
    portfolio_states = PortfolioStateStore(layout).read_all()
    target_filter = None if target_keys is None else set(target_keys)
    for target_key in sorted(portfolio_states):
        if target_filter is not None and target_key not in target_filter:
            continue
        portfolio_state = portfolio_states[target_key]
        if portfolio_state.state == "flat":
            continue
        trigger_source_decision = _active_pm_position_review_source_decision(
            layout=layout,
            portfolio_state=portfolio_state,
        )
        if (
            trigger_source_decision is None
            or trigger_source_decision.pm_review_request_id is None
        ):
            continue
        persisted_triggers = trigger_store.latest_for_request(
            target_key=target_key,
            pm_review_request_id=trigger_source_decision.pm_review_request_id,
        )
        if persisted_triggers is None:
            continue
        prior_request = _resolve_pm_review_request(
            layout=layout,
            request=None,
            target_key=target_key,
            request_id=trigger_source_decision.pm_review_request_id,
        )
        assessment = _load_linked_analysis_assessment(
            layout=layout,
            request=prior_request,
        )
        if assessment is None:
            continue
        existing_requests = tuple(
            persisted.record
            for persisted in request_store.read_records(target_key=target_key)
        )
        existing_decisions = tuple(
            persisted.record
            for persisted in decision_store.read_records(target_key=target_key)
        )
        if _pending_trigger_driven_pm_review_exists(
            requests=existing_requests,
            pm_decisions=existing_decisions,
            source_decision=trigger_source_decision,
        ):
            continue
        trigger_effective_at = max(
            persisted_triggers.record.business_at,
            portfolio_state.updated_at,
        )
        try:
            market_bars = _read_market_bars_for_position_review_triggers(
                config=config,
                target_key=target_key,
                start_at=trigger_effective_at,
                end_at=run_until,
                provider=market_data_provider,
            )
        except CompositionError as exc:
            emit(
                "pm trigger review request: skipped "
                f"target_key={target_key} "
                f"reason=market_data_read_failed "
                f"detail={exc}"
            )
            continue
        prior_gate_records = tuple(
            persisted.record for persisted in gate_store.read_records(target_key=target_key)
        )
        for candidate in _iter_trigger_driven_pm_review_candidates(
            layout=layout,
            prior_request=prior_request,
            source_decision=trigger_source_decision,
            persisted_triggers=persisted_triggers.record,
            price_level_roles=assessment.price_level_roles,
            market_bars=market_bars,
            trigger_effective_at=trigger_effective_at,
        ):
            if _trigger_driven_pm_review_candidate_already_audited(
                prior_gate_records=prior_gate_records,
                source_decision=trigger_source_decision,
                trigger_hit=candidate.trigger_hit,
            ):
                continue
            gate_decision = _evaluate_trigger_driven_pm_review_gate(
                gate_config=gate_config,
                prior_gate_records=prior_gate_records,
                source_decision=trigger_source_decision,
                trigger_hit=candidate.trigger_hit,
                market_bars=market_bars,
                price_level_roles=assessment.price_level_roles,
            )
            if gate_decision is not None and gate_decision.outcome == "skipped":
                record = build_pm_position_review_gate_record(
                    decision=gate_decision,
                    source_decision=trigger_source_decision,
                    trigger_hit=candidate.trigger_hit,
                    request_id=None,
                    rearm_buffer_bps=gate_config.rearm_buffer_bps,
                )
                gate_store.append(record)
                prior_gate_records = (*prior_gate_records, record)
                emit(
                    "pm trigger review request: skipped "
                    f"target_key={target_key} "
                    f"reason={gate_decision.reason} "
                    f"gate_key={gate_decision.gate_key} "
                    f"level_ids={list(candidate.trigger_hit.touched_level_ids)!r}"
                )
                continue
            triggered_request = build_triggered_pm_review_request(
                prior_request=prior_request,
                source_decision=trigger_source_decision,
                trigger_hit=candidate.trigger_hit,
                source_event_ids=candidate.source_event_ids,
            )
            request_path = request_store.append(triggered_request)
            if gate_decision is not None:
                record = build_pm_position_review_gate_record(
                    decision=gate_decision,
                    source_decision=trigger_source_decision,
                    trigger_hit=candidate.trigger_hit,
                    request_id=triggered_request.request_id,
                    rearm_buffer_bps=gate_config.rearm_buffer_bps,
                )
                gate_store.append(record)
                prior_gate_records = (*prior_gate_records, record)
            created_requests.append(
                MaterializedPMReviewRequest(
                    request=triggered_request,
                    request_ref=_pm_review_request_ref(
                        request_path=request_path,
                        request_id=triggered_request.request_id,
                    ),
                )
            )
            emit(
                "pm trigger review request: created "
                f"target_key={triggered_request.target_key} "
                f"request_id={triggered_request.request_id} "
                f"source_episode_id={triggered_request.source_episode_id} "
                f"reason={candidate.trigger_hit.trigger_kind} "
                f"level_ids={list(candidate.trigger_hit.touched_level_ids)!r}"
            )
            break
    return tuple(created_requests)


def materialize_trigger_driven_pm_review_requests(
    *,
    layout: WorkspaceLayout,
    config: KernelConfig,
    run_until: datetime,
    market_data_provider: MarketBarsProvider | None,
    emit: RuntimeEmitter,
    target_keys: tuple[str, ...] | None = None,
) -> tuple[MaterializedPMReviewRequest, ...]:
    """Materialize open-position PMReview requests due by one runtime time."""

    return _materialize_trigger_driven_pm_review_requests(
        layout=layout,
        config=config,
        run_until=run_until,
        market_data_provider=market_data_provider,
        emit=emit,
        target_keys=target_keys,
    )


def due_open_position_pm_review_requests_without_decision(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_until: datetime,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
) -> tuple[MaterializedPMReviewRequest, ...]:
    """Return existing due open-position PMReview requests still needing runtime work."""

    if not isinstance(layout, WorkspaceLayout):
        raise CompositionError("layout must be a WorkspaceLayout instance.")
    if not isinstance(run_until, datetime) or run_until.tzinfo is None:
        raise CompositionError("run_until must be a timezone-aware datetime.")
    decided_request_ids = {
        persisted.record.pm_review_request_id
        for persisted in PMDecisionStore(layout).read_records(target_key=target_key)
        if persisted.record.pm_review_request_id is not None
    }
    latest_failures_by_request_id = _latest_pm_review_failures_by_request_id(
        layout=layout,
        target_key=target_key,
    )
    due: list[MaterializedPMReviewRequest] = []
    for persisted in PMReviewRequestStore(layout).read_records(target_key=target_key):
        request = persisted.record
        if request.source != "open_position_material_update":
            continue
        if request.business_at > run_until:
            continue
        if request.request_id in decided_request_ids:
            continue
        latest_failure = latest_failures_by_request_id.get(request.request_id)
        if latest_failure is not None and latest_failure.retry_allowed is False:
            continue
        auto_dispatch_skip = _resolve_pending_pm_review_auto_dispatch_skip(
            layout=layout,
            request=request,
            migration_id=migration_id,
        )
        if is_terminal_pm_review_policy_skip_reason(
            None if auto_dispatch_skip is None else auto_dispatch_skip.reason
        ):
            continue
        due.append(
            MaterializedPMReviewRequest(
                request=request,
                request_ref=_pm_review_request_ref(
                    request_path=persisted.path,
                    request_id=request.request_id,
                ),
            )
        )
    return tuple(sorted(due, key=lambda item: (item.request.business_at, item.request.request_id)))


def _pm_review_request_ref(*, request_path: Path, request_id: str) -> str:
    return f"{request_path.resolve(strict=False)}#{request_id}"


def next_pm_position_review_trigger_at_before(
    *,
    layout: WorkspaceLayout,
    config: KernelConfig,
    boundary: datetime,
    market_data_provider: MarketBarsProvider | None,
    emit: RuntimeEmitter | None = None,
    target_keys: tuple[str, ...] | None = None,
) -> datetime | None:
    """Return the next open-position PM trigger time due at or before boundary."""

    if not isinstance(layout, WorkspaceLayout):
        raise CompositionError("layout must be a WorkspaceLayout instance.")
    if not isinstance(config, KernelConfig):
        raise CompositionError("config must be a KernelConfig instance.")
    if not isinstance(boundary, datetime) or boundary.tzinfo is None:
        raise CompositionError("boundary must be a timezone-aware datetime.")
    if market_data_provider is None:
        return None
    request_store = PMReviewRequestStore(layout)
    decision_store = PMDecisionStore(layout)
    trigger_store = PMPositionReviewTriggerStore(layout)
    gate_store = PMPositionReviewGateStore(layout)
    latest_failures_by_target_request_id: dict[str, PMReviewFailureRecord] = {}
    failure_cache_targets_loaded: set[str] = set()
    gate_config = PMPositionReviewGateConfig(
        enabled=config.pm_review_runtime.position_review_gate_enabled,
        review_cooldown_minutes=(
            config.pm_review_runtime.position_review_cooldown_minutes
        ),
        rearm_buffer_bps=config.pm_review_runtime.position_review_rearm_buffer_bps,
    )
    target_filter = None if target_keys is None else set(target_keys)
    candidate_times: list[datetime] = []
    portfolio_states = PortfolioStateStore(layout).read_all()
    for target_key in sorted(portfolio_states):
        if target_filter is not None and target_key not in target_filter:
            continue
        if target_key not in failure_cache_targets_loaded:
            latest_failures_by_target_request_id.update(
                _latest_pm_review_failures_by_request_id(
                    layout=layout,
                    target_key=target_key,
                )
            )
            failure_cache_targets_loaded.add(target_key)
        pending_existing = due_open_position_pm_review_requests_without_decision(
            layout=layout,
            target_key=target_key,
            run_until=boundary,
        )
        if pending_existing:
            pending_without_failure = tuple(
                item
                for item in pending_existing
                if latest_failures_by_target_request_id.get(item.request.request_id) is None
            )
            if pending_without_failure:
                candidate_times.append(pending_without_failure[0].request.business_at)
            continue
        portfolio_state = portfolio_states[target_key]
        if portfolio_state.state == "flat":
            continue
        trigger_source_decision = _active_pm_position_review_source_decision(
            layout=layout,
            portfolio_state=portfolio_state,
        )
        if (
            trigger_source_decision is None
            or trigger_source_decision.pm_review_request_id is None
        ):
            continue
        persisted_triggers = trigger_store.latest_for_request(
            target_key=target_key,
            pm_review_request_id=trigger_source_decision.pm_review_request_id,
        )
        if persisted_triggers is None:
            continue
        prior_request = _resolve_pm_review_request(
            layout=layout,
            request=None,
            target_key=target_key,
            request_id=trigger_source_decision.pm_review_request_id,
        )
        assessment = _load_linked_analysis_assessment(
            layout=layout,
            request=prior_request,
        )
        if assessment is None:
            continue
        existing_requests = tuple(
            persisted.record
            for persisted in request_store.read_records(target_key=target_key)
        )
        existing_decisions = tuple(
            persisted.record
            for persisted in decision_store.read_records(target_key=target_key)
        )
        if _pending_trigger_driven_pm_review_exists(
            requests=existing_requests,
            pm_decisions=existing_decisions,
            source_decision=trigger_source_decision,
        ):
            continue
        trigger_effective_at = max(
            persisted_triggers.record.business_at,
            portfolio_state.updated_at,
        )
        try:
            market_bars = _read_market_bars_for_position_review_triggers(
                config=config,
                target_key=target_key,
                start_at=trigger_effective_at,
                end_at=boundary,
                provider=market_data_provider,
            )
        except CompositionError as exc:
            if emit is not None:
                emit(
                    "pm trigger review deadline: skipped "
                    f"target_key={target_key} "
                    f"reason=market_data_read_failed "
                f"detail={exc}"
            )
            continue
        prior_gate_records = tuple(
            persisted.record for persisted in gate_store.read_records(target_key=target_key)
        )
        for candidate in _iter_trigger_driven_pm_review_candidates(
            layout=layout,
            prior_request=prior_request,
            source_decision=trigger_source_decision,
            persisted_triggers=persisted_triggers.record,
            price_level_roles=assessment.price_level_roles,
            market_bars=market_bars,
            trigger_effective_at=trigger_effective_at,
        ):
            if _trigger_driven_pm_review_candidate_already_audited(
                prior_gate_records=prior_gate_records,
                source_decision=trigger_source_decision,
                trigger_hit=candidate.trigger_hit,
            ):
                continue
            gate_decision = _evaluate_trigger_driven_pm_review_gate(
                gate_config=gate_config,
                prior_gate_records=prior_gate_records,
                source_decision=trigger_source_decision,
                trigger_hit=candidate.trigger_hit,
                market_bars=market_bars,
                price_level_roles=assessment.price_level_roles,
            )
            if gate_decision is not None and gate_decision.outcome == "skipped":
                continue
            candidate_times.append(candidate.trigger_hit.business_at)
            break
    if not candidate_times:
        return None
    return min(candidate_times)


def _iter_trigger_driven_pm_review_candidates(
    *,
    layout: WorkspaceLayout,
    prior_request: PMReviewRequest,
    source_decision: PMDecision,
    persisted_triggers,
    price_level_roles: tuple,
    market_bars: tuple[MarketDataBar, ...],
    trigger_effective_at: datetime,
) -> tuple[_TriggerDrivenPMReviewCandidate, ...]:
    candidates: list[_TriggerDrivenPMReviewCandidate] = []
    for market_bar in market_bars:
        trigger_hit = evaluate_pm_position_review_triggers(
            triggers=persisted_triggers,
            price_level_roles=price_level_roles,
            market_bars=(market_bar,),
            evaluation_start_at=trigger_effective_at,
        )
        if trigger_hit is None:
            continue
        source_event_ids, new_event_ids = _trigger_driven_pm_review_source_events(
            layout=layout,
            prior_request=prior_request,
            source_decision=source_decision,
            trigger_hit=trigger_hit,
        )
        if (
            trigger_hit.trigger_kind == "review"
            and persisted_triggers.path_context_required
            and not new_event_ids
        ):
            continue
        candidates.append(
            _TriggerDrivenPMReviewCandidate(
                trigger_hit=trigger_hit,
                source_event_ids=source_event_ids,
                new_event_ids=new_event_ids,
            )
        )
    return tuple(candidates)


def _evaluate_trigger_driven_pm_review_gate(
    *,
    gate_config: PMPositionReviewGateConfig,
    prior_gate_records: tuple,
    source_decision: PMDecision,
    trigger_hit: PMPositionReviewTriggerHit,
    market_bars: tuple[MarketDataBar, ...],
    price_level_roles: tuple,
) -> PMPositionReviewGateDecision | None:
    if not gate_config.enabled:
        return None
    return evaluate_pm_position_review_gate(
        config=gate_config,
        source_decision=source_decision,
        trigger_hit=trigger_hit,
        prior_gate_records=prior_gate_records,
        market_bars=market_bars,
        price_level_roles=price_level_roles,
    )


def _trigger_driven_pm_review_candidate_already_audited(
    *,
    prior_gate_records: tuple,
    source_decision: PMDecision,
    trigger_hit: PMPositionReviewTriggerHit,
) -> bool:
    gate_key = derive_pm_position_review_gate_key(
        target_key=trigger_hit.target_key,
        source_episode_id=source_decision.decision_episode_id,
        requested_state=source_decision.requested_state,
        trigger_kind=trigger_hit.trigger_kind,
        touched_level_ids=trigger_hit.touched_level_ids,
    )
    return any(
        record.gate_key == gate_key
        and record.business_at == trigger_hit.business_at
        and record.touched_market_bar_ids == trigger_hit.touched_market_bar_ids
        for record in prior_gate_records
    )


def _active_pm_position_review_source_decision(
    *,
    layout: WorkspaceLayout,
    portfolio_state: PortfolioState,
) -> PMDecision | None:
    decisions = tuple(
        persisted.record
        for persisted in PMDecisionStore(layout).read_records(
            target_key=portfolio_state.target_key
        )
    )
    aligned = tuple(
        decision
        for decision in decisions
        if decision.requested_state == portfolio_state.state
        and decision.business_at >= portfolio_state.updated_at
    )
    if aligned:
        return sorted(aligned, key=lambda item: (item.business_at, item.decision_id))[-1]
    return next(
        (
            decision
            for decision in decisions
            if decision.decision_id == portfolio_state.source_pm_decision_id
        ),
        None,
    )


def _pending_trigger_driven_pm_review_exists(
    *,
    requests: tuple[PMReviewRequest, ...],
    pm_decisions: tuple[PMDecision, ...],
    source_decision: PMDecision,
) -> bool:
    decided_request_ids = {
        decision.pm_review_request_id
        for decision in pm_decisions
        if decision.pm_review_request_id is not None
    }
    return any(
        request.source == "open_position_material_update"
        and request.source_episode_id == source_decision.decision_episode_id
        and request.request_id not in decided_request_ids
        for request in requests
    )


def _read_market_bars_for_position_review_triggers(
    *,
    config: KernelConfig,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
    provider: MarketBarsProvider,
) -> tuple[MarketDataBar, ...]:
    if start_at >= end_at:
        return ()
    try:
        mapping = resolve_market_mapping(config, target_key)
        visible = read_visible_target_bars(
            market_data=provider,
            market_mapping=mapping,
            start_at=start_at,
            end_at=end_at,
            adjustment_policy=_pm_review_visible_adjustment_policy(
                config=config,
                target_key=target_key,
                market_symbol=mapping.market_symbol,
            ),
        )
    except Exception as exc:
        if _is_no_prefetched_bars_in_requested_window(exc):
            return ()
        raise CompositionError(
            f"failed to read market bars for PM position review triggers: {exc}"
        ) from exc
    return tuple(
        sorted(
            (
                bar
                for bar in visible
                if start_at < bar.end_at <= end_at
            ),
            key=lambda bar: (bar.start_at, bar.end_at),
        )
    )


def _pm_review_visible_adjustment_policy(
    *,
    config: KernelConfig,
    target_key: str,
    market_symbol: str,
) -> MarketDataAdjustmentPolicy | None:
    market_context = config.market_context
    if market_context is None:
        return None
    profile_config = market_context.target_profiles.get(target_key)
    if profile_config is None:
        return None
    for subscription in profile_config.market_data_subscriptions:
        if subscription.symbol == market_symbol:
            policy = subscription.adjustment_policy
            if policy == "forward_adjusted_realized":
                raise CompositionError(
                    "PMReview visible market bars require visible adjustment policy; "
                    f"target_key={target_key!r} market_symbol={market_symbol!r} "
                    "configured forward_adjusted_realized."
                )
            return policy
    return None


def _is_no_prefetched_bars_in_requested_window(exc: Exception) -> bool:
    message = str(exc)
    return "no prefetched bars" in message and "requested window" in message


def _trigger_driven_pm_review_source_events(
    *,
    layout: WorkspaceLayout,
    prior_request: PMReviewRequest,
    source_decision: PMDecision,
    trigger_hit: PMPositionReviewTriggerHit,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    new_event_ids = tuple(
        record.event_id
        for record in read_evidence_window(
            start_at=prior_request.business_at,
            end_at=trigger_hit.business_at,
            ledger=FileBackedEvidenceLedger(layout),
            target_keys=[prior_request.target_key],
            exclude_event_ids=[],
        )
    )
    combined_ids = new_event_ids if new_event_ids else source_decision.source_event_ids
    return _stable_unique_strings(combined_ids), _stable_unique_strings(new_event_ids)


def _stable_unique_strings(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _classify_pm_review_request(
    *,
    request: PMReviewRequest,
    pm_decisions: tuple[PMDecision, ...],
    execution_records: tuple[ExecutionRecord, ...],
    failure_records: tuple[PMReviewFailureRecord, ...] = (),
    execute_approved: bool,
) -> PMReviewLifecycleClassification:
    pm_decision = _unique_pm_decision_for_request(
        request=request,
        pm_decisions=pm_decisions,
        required=False,
    )
    if pm_decision is not None:
        execution_record = _terminal_execution_record_for_pm_decision(
            decision=pm_decision,
            execution_records=execution_records,
        )
        if execution_record is not None:
            state: PMReviewLifecycleState = (
                "complete_executed"
                if execution_record.status == "executed"
                else "complete_execution_rejected"
            )
            return PMReviewLifecycleClassification(
                request_id=request.request_id,
                state=state,
                execution_record_id=execution_record.execution_record_id,
                pm_decision_id=pm_decision.decision_id,
            )
        if not pm_decision.execution_required:
            return PMReviewLifecycleClassification(
                request_id=request.request_id,
                state="complete_hold_noop",
                execution_record_id=None,
                pm_decision_id=pm_decision.decision_id,
            )
        return PMReviewLifecycleClassification(
            request_id=request.request_id,
            state=(
                "decision_without_execution"
                if execute_approved
                else "decision_no_execution_requested"
            ),
            execution_record_id=None,
            pm_decision_id=pm_decision.decision_id,
        )
    latest_failure = _latest_pm_review_failure_for_request(
        request=request,
        failure_records=failure_records,
    )
    return PMReviewLifecycleClassification(
        request_id=request.request_id,
        state="pending_no_decision",
        execution_record_id=None,
        failure_id=None if latest_failure is None else latest_failure.failure_id,
        failure_retry_allowed=(
            None if latest_failure is None else latest_failure.retry_allowed
        ),
    )


def _continue_pm_review_execution_from_existing_decision(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest,
    classification_before: PMReviewLifecycleClassification,
    pm_decisions: tuple[PMDecision, ...],
    execution_records: tuple[ExecutionRecord, ...],
    execution_engine: PaperExecutionEngine | None,
    migration_id: str,
) -> PMReviewLifecycleRepairResult:
    pm_decision = _unique_pm_decision_for_request(
        request=request,
        pm_decisions=pm_decisions,
        required=True,
    )
    if pm_decision is None:
        raise CompositionError("existing PMReview decision is required for execution repair.")
    active_exposure = resolve_active_exposure(
        layout=layout,
        target_key=request.target_key,
        migration_id=migration_id,
    )
    assessment = _load_linked_analysis_assessment(
        layout=layout,
        request=request,
    )
    read_receipts = _read_pm_review_tool_receipts_for_request(
        layout=layout,
        request=request,
    )
    execution_result = _execute_existing_pm_review_decision(
        layout=layout,
        pm_decision=pm_decision,
        execution_engine=execution_engine,
    )
    updated_executions = execution_records
    if execution_result.execution_record is not None:
        updated_executions = updated_executions + (execution_result.execution_record,)
    classification_after = _classify_pm_review_request(
        request=request,
        pm_decisions=pm_decisions,
        execution_records=updated_executions,
        execute_approved=True,
    )
    return PMReviewLifecycleRepairResult(
        request=request,
        active_exposure=active_exposure,
        analysis_assessment=assessment,
        read_receipts=read_receipts,
        classification_before=classification_before,
        classification_after=classification_after,
        execution_flow_result=execution_result,
    )


def _execute_existing_pm_review_decision(
    *,
    layout: WorkspaceLayout,
    pm_decision: PMDecision,
    execution_engine: PaperExecutionEngine | None,
) -> PMExecutionFlowResult:
    _validate_pm_review_execution_inputs(execution_engine=execution_engine)
    if execution_engine is None:
        raise CompositionError("execution_engine is required for PMReview execution.")
    return execute_pm_decision(
        layout=layout,
        decision=pm_decision,
        execution_engine=execution_engine,
    )


def _latest_pm_review_failure_for_request(
    *,
    request: PMReviewRequest,
    failure_records: tuple[PMReviewFailureRecord, ...],
) -> PMReviewFailureRecord | None:
    matches = tuple(
        failure
        for failure in failure_records
        if failure.pm_review_request_id == request.request_id
    )
    if not matches:
        return None
    return sorted(matches, key=lambda failure: (failure.failed_at, failure.failure_id))[
        -1
    ]


def _latest_pm_review_failures_by_request_id(
    *,
    layout: WorkspaceLayout,
    target_key: str,
) -> dict[str, PMReviewFailureRecord]:
    latest_by_request_id: dict[str, PMReviewFailureRecord] = {}
    for persisted in PMReviewFailureStore(layout).read_records(target_key=target_key):
        failure = persisted.record
        current = latest_by_request_id.get(failure.pm_review_request_id)
        if current is None or (failure.failed_at, failure.failure_id) > (
            current.failed_at,
            current.failure_id,
        ):
            latest_by_request_id[failure.pm_review_request_id] = failure
    return latest_by_request_id


def _unique_pm_decision_for_request(
    *,
    request: PMReviewRequest,
    pm_decisions: tuple[PMDecision, ...],
    required: bool,
) -> PMDecision | None:
    matches = tuple(
        decision
        for decision in pm_decisions
        if decision.pm_review_request_id == request.request_id
    )
    if not matches:
        if required:
            raise CompositionError(
                "PMReviewRequest lifecycle expected a PMDecision: "
                f"{request.request_id}"
            )
        return None
    if len(matches) > 1:
        raise CompositionError(
            "PMReviewRequest lifecycle has multiple PMDecision records: "
            f"{request.request_id}"
        )
    return matches[0]


def _terminal_execution_record_for_pm_decision(
    *,
    decision: PMDecision,
    execution_records: tuple[ExecutionRecord, ...],
) -> ExecutionRecord | None:
    matches = tuple(
        record
        for record in execution_records
        if record.pm_decision_id == decision.decision_id
        and record.status in {"executed", "rejected"}
    )
    if not matches:
        return None
    return sorted(matches, key=lambda item: (item.business_at, item.execution_record_id))[
        -1
    ]


def _read_pm_review_tool_receipts_for_request(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest,
) -> tuple[PMReviewToolReadReceipt, ...]:
    return tuple(
        persisted.record
        for persisted in PMReviewToolReadReceiptStore(layout).read_records(
            target_key=request.target_key
        )
        if persisted.record.pm_review_request_id == request.request_id
    )


def _skip_reason_for_pm_review_lifecycle(
    classification: PMReviewLifecycleClassification,
) -> str:
    if classification.state == "complete_hold_noop":
        return "complete_hold_noop"
    if classification.state == "complete_executed":
        return "complete_executed"
    if classification.state == "complete_execution_rejected":
        return "complete_execution_rejected"
    if classification.state == "decision_no_execution_requested":
        return "already_has_pm_decision_execute_false"
    if classification.state == "decision_without_execution":
        return "incomplete_pm_decision_without_execution"
    if classification.state == "after_run_until":
        return "after_run_until"
    return classification.state


def _emit_pm_review_dispatch_summary(
    *,
    emit: RuntimeEmitter | None,
    target_key: str,
    run_until: datetime | None,
    dispatch_result: PMReviewDispatchResult,
) -> None:
    if emit is None:
        return
    lifecycle_counts = _pm_review_dispatch_lifecycle_counts(dispatch_result)
    skipped = ", ".join(
        f"{item.request_id}:{item.reason}" for item in dispatch_result.skipped
    )
    run_until_text = "none" if run_until is None else run_until.isoformat()
    suffix = "" if not skipped else f" skipped={skipped}"
    emit(
        "pm review dispatch: "
        f"target_key={target_key} "
        f"run_until={run_until_text} "
        f"processed={len(dispatch_result.processed)} "
        f"skipped={len(dispatch_result.skipped)}"
        f"pm_decisions={lifecycle_counts['pm_decisions']} "
        f"executions={lifecycle_counts['executions']} "
        f"execution_rejected={lifecycle_counts['execution_rejected']} "
        f"failures={lifecycle_counts['failures']} "
        f"sidecars={lifecycle_counts['sidecars']}"
        f"{suffix}"
    )


def _pm_review_dispatch_lifecycle_counts(
    dispatch_result: PMReviewDispatchResult,
) -> dict[str, int]:
    counts = {
        "pm_decisions": 0,
        "executions": 0,
        "execution_rejected": 0,
        "failures": 0,
        "sidecars": 0,
    }
    for item in dispatch_result.processed:
        pm_decision: PMDecision | None = None
        execution_flow_result: PMExecutionFlowResult | None = None
        if isinstance(item, PMReviewExecutionOrchestrationResult):
            pm_decision = item.pm_review_orchestration.pm_review_result.pm_decision
            execution_flow_result = item.execution_flow_result
        elif isinstance(item, PMReviewOrchestrationResult):
            pm_decision = item.pm_review_result.pm_decision
        elif isinstance(item, PMReviewLifecycleRepairResult):
            execution_flow_result = item.execution_flow_result
            if execution_flow_result is not None:
                pm_decision = execution_flow_result.pm_decision
        elif isinstance(item, PMReviewFailureResult):
            counts["failures"] += 1
        if pm_decision is not None:
            counts["pm_decisions"] += 1
        if execution_flow_result is None:
            continue
        record = execution_flow_result.execution_record
        if record is not None:
            if record.status == "executed":
                counts["executions"] += 1
            else:
                counts["execution_rejected"] += 1
        if execution_flow_result.view_state_change is not None:
            counts["sidecars"] += 1
    return counts


def _emit_pm_review_market_bars_message(
    *,
    emit: RuntimeEmitter | None,
    target_key: str,
    message: str,
) -> None:
    if emit is None:
        return
    emit(f"pm review market bars: target_key={target_key} {message}")


def _validate_pm_review_execution_inputs(
    *,
    execution_engine: PaperExecutionEngine | None,
) -> None:
    if not isinstance(execution_engine, PaperExecutionEngine):
        raise CompositionError(
            "execution_engine must be supplied for PMReview execution."
        )


def _empty_pm_execution_flow_result() -> PMExecutionFlowResult:
    return PMExecutionFlowResult(
        pm_decision=None,
        execution_intent=None,
        execution_record=None,
        portfolio_state=None,
        view_state_change=None,
    )


def _load_linked_analysis_assessment(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest,
) -> AnalysisAssessment | None:
    if request.source_assessment_id is None:
        return None
    matches = tuple(
        persisted.record
        for persisted in AnalysisAssessmentStore(layout).read_records(
            target_key=request.target_key
        )
        if persisted.record.assessment_id == request.source_assessment_id
    )
    if not matches:
        raise CompositionError(
            "linked AnalysisAssessment not found for PMReviewRequest: "
            f"{request.source_assessment_id}"
        )
    if len(matches) > 1:
        raise CompositionError(
            "linked AnalysisAssessment id is ambiguous: "
            f"{request.source_assessment_id}"
        )
    return matches[0]


def _build_paper_execution_engine(
    *,
    config: KernelConfig,
    market_data_provider: MarketBarsProvider | None,
) -> PaperExecutionEngine | None:
    if config.validation is None:
        return None
    if market_data_provider is None:
        return None
    provider_name = (
        "market_data_provider"
        if config.validation is None
        else config.validation.market_data.provider
    )
    default_cost_model = ExecutionCostModel(
        buy_cost_bps=config.execution.buy_cost_bps,
        sell_cost_bps=config.execution.sell_cost_bps,
        price_basis=config.execution.price_basis,
    )
    cost_models_by_target = {
        target_key: ExecutionCostModel(
            buy_cost_bps=override.buy_cost_bps,
            sell_cost_bps=override.sell_cost_bps,
            price_basis=config.execution.price_basis,
        )
        for target_key, override in config.execution.target_cost_overrides.items()
    }
    return PaperExecutionEngine(
        resolve_market_mapping=lambda target_key: resolve_market_mapping(
            config,
            target_key,
        ),
        market_data_provider=market_data_provider,
        cost_model=default_cost_model,
        resolve_cost_model=lambda target_key: cost_models_by_target.get(
            target_key,
            default_cost_model,
        ),
        missing_bar_policy=config.execution.missing_bar_policy,
        max_next_bar_wait=timedelta(hours=config.execution.max_next_bar_wait_hours),
        provider_name=provider_name,
    )


def read_evidence(
    event_ids: list[str],
    *,
    ledger: FileBackedEvidenceLedger,
) -> tuple[EvidenceLedgerRecord, ...]:
    return tuple(ledger.read_many(event_ids))


__all__ = [
    "PMReviewDispatchResult",
    "PMReviewExecutionInputs",
    "PMReviewExecutionOrchestrationResult",
    "PMReviewFailureResult",
    "PMReviewLifecycleClassification",
    "PMReviewLifecycleRepairResult",
    "PMReviewLifecycleState",
    "MaterializedPMReviewRequest",
    "PMReviewOrchestrationResult",
    "PMReviewRuntimePolicy",
    "PMReviewRuntimePreflightReceipt",
    "build_pm_review_execution_inputs",
    "build_pm_review_task_runner",
    "build_pm_review_visible_market_bars_for_requests",
    "build_runtime_pm_review_executor",
    "dispatch_due_pm_reviews_for_target",
    "dispatch_pending_pm_review_requests",
    "due_open_position_pm_review_requests_without_decision",
    "materialize_trigger_driven_pm_review_requests",
    "next_pm_position_review_trigger_at_before",
    "pm_review_preflight_target_keys_from_config",
    "pm_review_runtime_policy_from_config",
    "process_pending_pm_review_request",
    "process_pending_pm_review_request_and_execute",
    "resolve_pm_review_runtime_policy",
    "validate_pm_review_runtime_preflight",
]
