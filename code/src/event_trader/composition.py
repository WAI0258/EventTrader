"""Project-owned composition root for runtime assembly."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from nautilus_trader.common.component import TestClock

from event_trader.analysis_request_handler import AnalysisCallback
from event_trader.ceau import CEAUUnitEmittedRecord, FileBackedCEAUStore
from event_trader.checker.context_pack import CheckerContextPack
from event_trader.checker.validator import RawCheckerDecision
from event_trader.composition_error import CompositionError
from event_trader.config import KernelConfig, load_kernel_config
from event_trader.context_assembly import (
    CURRENT_MEMORY_READ_POLICY,
    HISTORICAL_THESIS_SNAPSHOT_MEMORY_READ_POLICY,
)
from event_trader.contracts.ports import OutcomeContextPort, TradeContextPort
from event_trader.contracts.view_state_change import MarketMapping
from event_trader.evidence_ledger import FileBackedEvidenceLedger
from event_trader.integrations import (
    MiroThinkerReflectionRuntimeConfig,
    build_mirothinker_open_position_reflection_runner,
    build_mirothinker_reflection_runner,
    build_mirothinker_target_close_reflection_runner,
    build_mirothinker_target_reflection_runner,
)
from event_trader.integrations.capabilities import (
    CapabilityStatus,
    discover_capability_status,
)
from event_trader.integrations.mirothinker_pm_review import PMReviewTaskRunner
from event_trader.kernel import EventTraderKernel
from event_trader.market.provider import (
    MarketBarsProvider,
    build_default_market_bars_provider,
)
from event_trader.migrations.cutover_baseline import DEFAULT_CUTOVER_MIGRATION_ID
from event_trader.pm_review import runtime as pm_review_runtime
from event_trader.reflection import runtime as reflection_runtime
from event_trader.reflection.outcome_context import (
    FileBackedReflectionOutcomeContextPort,
)
from event_trader.reflection.review_loop import (
    ReflectionReviewEvaluator,
    ReflectionReviewLoop,
    TargetCloseReflectionReviewEvaluator,
    TargetOpenPositionHorizonReflectionEvaluator,
    TargetReflectionReviewEvaluator,
)
from event_trader.reflection.trigger_store import FileBackedReflectionObligationStore
from event_trader.replay.runner import ReplayRunner
from event_trader.runtime import research_pipeline
from event_trader.runtime.admission import (
    FileBackedAdmissionQuarantineStore,
    RuntimeEvidenceAdmissionAdapter,
)
from event_trader.runtime.admitted_path import NautilusAdmittedEvidencePath
from event_trader.runtime.analysis_outbox import FileBackedAnalysisCompletionOutbox
from event_trader.runtime.analysis_queue import FileBackedAnalysisWorkQueue
from event_trader.runtime.analysis_resolver import RuntimeAnalysisRequestResolver
from event_trader.runtime.bootstrap import (
    BoundedReplayRuntime,
    LiveRuntimeHost,
    _OwnedRuntimeResources,
    _ResidentKernelRunner,
    _RuntimeGraphAdmissionReleasePath,
)
from event_trader.runtime.contracts import PMReviewRequested, base_message_kwargs
from event_trader.runtime.graph import (
    EventTraderRuntimeGraph,
    PMReviewRuntimeWiring,
    ReflectionRuntimeWiring,
)
from event_trader.runtime.nautilus import (
    EventTraderNautilusNode,
    build_event_trader_nautilus_node,
)
from event_trader.runtime.outbox import FileBackedCheckerCompletionOutbox
from event_trader.runtime.pm_review_outbox import FileBackedPMReviewCompletionOutbox
from event_trader.runtime.pm_review_queue import FileBackedPMReviewWorkQueue
from event_trader.runtime.pm_review_resolver import RuntimePMReviewRequestResolver
from event_trader.runtime.queue import FileBackedCheckerWorkQueue
from event_trader.runtime.reflection_outbox import FileBackedReflectionCompletionOutbox
from event_trader.runtime.reflection_queue import FileBackedReflectionWorkQueue
from event_trader.runtime.reflection_scheduler import ReflectionCycleScheduler
from event_trader.runtime.source_publisher import RuntimeRawSourcePublisher
from event_trader.validation.market_mapping import resolve_market_mapping
from event_trader.workspace import BootstrapWorkspace, bootstrap_workspace

type CompositionEmitter = Callable[[str], None]
type PipelineStageObserver = Callable[[str, str], None]


@dataclass(frozen=True, slots=True)
class ComposedKernel:
    """Typed result of assembling the committed runtime seams."""

    config: KernelConfig
    workspace: BootstrapWorkspace
    capabilities: tuple[CapabilityStatus, ...]
    live_host: LiveRuntimeHost | None = None
    replay_runtime: BoundedReplayRuntime | None = None
    nautilus_node: EventTraderNautilusNode | None = None
    runtime_graph: EventTraderRuntimeGraph | None = None
    raw_source_publisher: RuntimeRawSourcePublisher | None = None
    market_data_provider: MarketBarsProvider | None = None
    _owned_resources: _OwnedRuntimeResources = field(
        default_factory=_OwnedRuntimeResources,
        repr=False,
        compare=False,
    )

    def require_replay_runtime(self) -> BoundedReplayRuntime:
        """Return the bounded replay execution surface or fail explicitly."""
        if self.config.mode != "replay" or self.replay_runtime is None:
            raise CompositionError(
                "Replay execution surface is available only when config.mode is "
                "'replay'."
            )
        return self.replay_runtime

    def require_nautilus_node(self) -> EventTraderNautilusNode:
        """Return the Nautilus runtime node or fail explicitly."""
        if self.nautilus_node is None:
            raise CompositionError(
                "Nautilus runtime node is available only for live/replay kernels."
            )
        return self.nautilus_node

    def require_runtime_graph(self) -> EventTraderRuntimeGraph:
        """Return the composed runtime graph or fail explicitly."""
        if self.runtime_graph is None:
            raise CompositionError(
                "Runtime graph is available only for live/replay kernels."
            )
        return self.runtime_graph

    def require_raw_source_publisher(self) -> RuntimeRawSourcePublisher:
        """Return the runtime-owned raw source publisher or fail explicitly."""
        if self.raw_source_publisher is None:
            raise CompositionError(
                "Raw source publisher is available only for live/replay kernels."
            )
        return self.raw_source_publisher

    def require_live_host(self) -> LiveRuntimeHost:
        """Return the committed long-running live host or fail explicitly."""
        if self.config.mode != "live" or self.live_host is None:
            raise CompositionError(
                "Live host is available only when config.mode is 'live'; replay "
                "remains a bounded execution surface."
            )
        return self.live_host

    def close(self) -> None:
        """Close runtime-owned resources exactly once."""
        self._owned_resources.close()


def _resolve_active_market_data_provider(
    *,
    config: KernelConfig,
    market_data_provider_override: MarketBarsProvider | None,
    owned_resources: _OwnedRuntimeResources,
) -> MarketBarsProvider | None:
    if market_data_provider_override is not None:
        return market_data_provider_override
    provider = build_default_market_bars_provider(config)
    if provider is None:
        return None
    close = getattr(provider, "close", None)
    if callable(close):
        owned_resources.register(close)
    return provider


def compose_kernel(
    config_path: str | Path,
    *,
    emit: CompositionEmitter | None = None,
    pipeline_stage_observer: PipelineStageObserver | None = None,
    outcome_context: OutcomeContextPort | None = None,
    trade_context: TradeContextPort | None = None,
    evaluate_review: ReflectionReviewEvaluator | None = None,
    evaluate_target_review: TargetReflectionReviewEvaluator | None = None,
    evaluate_target_close_review: TargetCloseReflectionReviewEvaluator | None = None,
    evaluate_target_open_position_review: (
        TargetOpenPositionHorizonReflectionEvaluator | None
    ) = None,
    replay_market_mapping: MarketMapping | None = None,
    market_data_provider_override: MarketBarsProvider | None = None,
    market_data_store_root: Path | None = None,
    replay_run_id: str | None = None,
    auto_dispatch_pm_review_after_analysis: bool = False,
    execute_approved_pm_review_after_analysis: bool = False,
    pm_review_runtime_policy: pm_review_runtime.PMReviewRuntimePolicy | None = None,
    pm_review_task_runner: PMReviewTaskRunner | None = None,
    pm_review_preflight_target_keys: tuple[str, ...] = (),
    checker_policy: Callable[[CheckerContextPack], RawCheckerDecision] | None = None,
    analysis_callback: AnalysisCallback | None = None,
) -> ComposedKernel:
    """Assemble config, workspace, capability metadata, and kernel in one place."""

    if emit is None:
        raise CompositionError("compose_kernel requires an explicit emit callback.")
    lifecycle_emit = emit
    owned_resources = _OwnedRuntimeResources()
    try:
        config = load_kernel_config(config_path)
        workspace = bootstrap_workspace(config.workspace_root)
        capabilities = discover_capability_status()
        active_market_data_provider = _resolve_active_market_data_provider(
            config=config,
            market_data_provider_override=market_data_provider_override,
            owned_resources=owned_resources,
        )
        admitted_evidence_path = None
        nautilus_node = None
        runtime_graph = None
        raw_source_publisher = None
        replay_runner = None
        active_layout = workspace.layout
        analysis_memory_read_policy = CURRENT_MEMORY_READ_POLICY
        active_replay_run_id = replay_run_id or "default"
        active_pm_review_runtime_policy = (
            pm_review_runtime.resolve_pm_review_runtime_policy(
                policy=pm_review_runtime_policy,
                enabled=auto_dispatch_pm_review_after_analysis,
                execute_approved=execute_approved_pm_review_after_analysis,
                config=config,
            )
        )
        pm_review_runtime_requires_layout = (
            active_pm_review_runtime_policy.auto_dispatch_after_analysis
        )
        if pm_review_runtime_requires_layout and active_layout is None:
            raise CompositionError("PMReview runtime requires a workspace layout.")
        if pm_review_runtime_requires_layout and active_layout is not None:
            active_pm_review_preflight_targets = (
                pm_review_preflight_target_keys
                or pm_review_runtime.pm_review_preflight_target_keys_from_config(
                    config
                )
            )
            pm_review_runtime.validate_pm_review_runtime_preflight(
                config=config,
                layout=active_layout,
                target_keys=active_pm_review_preflight_targets,
                task_runner=pm_review_task_runner,
            )
        active_pm_review_task_runner = pm_review_task_runner
        if (
            active_pm_review_runtime_policy.auto_dispatch_after_analysis
            and active_pm_review_task_runner is None
        ):
            active_pm_review_task_runner = pm_review_runtime.build_pm_review_task_runner(
                config=config
            )
        if (
            active_pm_review_runtime_policy.auto_dispatch_after_analysis
            and not callable(active_pm_review_task_runner)
        ):
            raise CompositionError(
                "automatic PMReview dispatch requires a PMReview task runner."
            )

        def _noop_time_advance_finalizer() -> None:
            return None

        def _noop_time_advance(
            _observed_at: datetime,
        ) -> research_pipeline.RuntimeTimeObservation:
            return research_pipeline.RuntimeTimeObservation()

        runtime_time_finalizer: Callable[[], None] = (
            _noop_time_advance_finalizer
        )
        runtime_time_advancer: Callable[
            [datetime],
            research_pipeline.RuntimeTimeObservation,
        ] = (
            _noop_time_advance
        )
        runtime_time_hooks = research_pipeline.RuntimeTimeAdvanceHooks(
            finalize=runtime_time_finalizer,
            advance_time_to=runtime_time_advancer,
        )
        runtime_graph_reflection = None
        if workspace.layout is not None:
            if config.mode == "replay":
                analysis_memory_read_policy = (
                    HISTORICAL_THESIS_SNAPSHOT_MEMORY_READ_POLICY
                )
            if config.mode in {"live", "replay"}:
                nautilus_node = build_event_trader_nautilus_node(
                    clock=TestClock() if config.mode == "replay" else None,
                )

                def _close_runtime_side_stream() -> None:
                    if runtime_graph is not None:
                        runtime_graph.close()
                        return
                    nautilus_node.stop()

                owned_resources.register(_close_runtime_side_stream)
                lifecycle_emit(
                    "nautilus runtime: TradingNode initialized "
                    f"message_bus={type(nautilus_node.msgbus).__name__}"
                )
                if active_layout is None:
                    raise CompositionError(
                        "Workspace layout is required for primary flow."
                    )
                if config.analysis_agent is None:
                    raise CompositionError(
                        "compose_kernel primary research runtime requires "
                        "[analysis_agent] config when mode is 'live' or 'replay'."
                    )
                if config.checker_agent is None:
                    raise CompositionError(
                        "compose_kernel primary research runtime requires "
                        "[checker_agent] config when mode is 'live' or 'replay'."
                    )
                checker_work_queue = FileBackedCheckerWorkQueue(
                    root=active_layout.runtime_root / "checker_work_queue",
                    now=nautilus_node.clock.utc_now,
                )
                checker_outbox = FileBackedCheckerCompletionOutbox(
                    root=active_layout.runtime_root / "checker_outbox",
                )
                analysis_work_queue = FileBackedAnalysisWorkQueue(
                    root=active_layout.runtime_root / "analysis_work_queue",
                    now=nautilus_node.clock.utc_now,
                )
                analysis_outbox = FileBackedAnalysisCompletionOutbox(
                    root=active_layout.runtime_root / "analysis_outbox",
                )
                ledger = FileBackedEvidenceLedger(workspace.layout)
                admission_adapter = RuntimeEvidenceAdmissionAdapter(
                    ledger=ledger,
                    load_news_request=lambda message: (
                        research_pipeline.load_archived_news_admission_request(
                            layout=active_layout,
                            message=message,
                        )
                    ),
                    load_web_request=lambda message: (
                        research_pipeline.load_archived_web_admission_request(
                            layout=active_layout,
                            message=message,
                        )
                    ),
                    quarantine_store=FileBackedAdmissionQuarantineStore(
                        root=active_layout.runtime_root / "admission_quarantine",
                    ),
                )
                admitted_evidence_path = NautilusAdmittedEvidencePath(
                    ledger=ledger,
                    msgbus=nautilus_node.msgbus,
                    require_subscribers=True,
                )

                def _publish_runtime_analysis_requested(
                    emitted_record: CEAUUnitEmittedRecord,
                ) -> None:
                    message = research_pipeline.runtime_analysis_requested_message(
                        emitted_record=emitted_record,
                    )
                    nautilus_node.msgbus.publish(message.topic, message, False)

                primary_runtime = research_pipeline.install_primary_research_runtime(
                    layout=active_layout,
                    runtime_mode=config.mode,
                    now=nautilus_node.clock.utc_now,
                    emit=lifecycle_emit,
                    pipeline_stage_observer=pipeline_stage_observer,
                    analysis_config=config.analysis_agent,
                    checker_config=config.checker_agent,
                    replay_market_mapping=replay_market_mapping,
                    market_data_provider=active_market_data_provider,
                    config_path=Path(config_path),
                    market_data_store_root=market_data_store_root,
                    config=config,
                    memory_read_policy=analysis_memory_read_policy,
                    replay_run_id=active_replay_run_id,
                    pm_review_runtime_policy=active_pm_review_runtime_policy,
                    pm_review_task_runner=active_pm_review_task_runner,
                    publish_runtime_analysis_requested=_publish_runtime_analysis_requested,
                    runtime_owned_analysis_pm_review=(
                        active_pm_review_runtime_policy.auto_dispatch_after_analysis
                    ),
                    checker_policy=checker_policy,
                    analysis_callback=analysis_callback,
                )
                analysis_resolver = RuntimeAnalysisRequestResolver(
                    ledger=ledger,
                    ceau_store=FileBackedCEAUStore(active_layout),
                    stream_routing_config=config.stream_routing,
                )
                should_install_reflection = config.mode in {"live", "replay"}
                if should_install_reflection:
                    if config.mode == "live" and outcome_context is None:
                        outcome_context = FileBackedReflectionOutcomeContextPort(
                            workspace.layout
                        )
                    shared_reflection_evaluator = evaluate_review
                    target_reflection_evaluator = evaluate_target_review
                    target_close_reflection_evaluator = evaluate_target_close_review
                    target_open_position_reflection_evaluator = (
                        evaluate_target_open_position_review
                    )
                    if config.reflection_agent is not None:
                        reflection_runtime_config = MiroThinkerReflectionRuntimeConfig(
                            vendor_root=config.reflection_agent.vendor_root,
                            log_dir=config.reflection_agent.log_dir,
                            llm_provider=config.reflection_agent.llm_provider,
                            llm_model_name=config.reflection_agent.llm_model_name,
                            llm_api_key=config.reflection_agent.llm_api_key,
                            llm_base_url=config.reflection_agent.llm_base_url,
                            llm_max_context_length=(
                                config.reflection_agent.llm_max_context_length
                            ),
                        )
                        if shared_reflection_evaluator is None:
                            shared_reflection_evaluator = (
                                build_mirothinker_reflection_runner(
                                    config=reflection_runtime_config,
                                    layout=workspace.layout,
                                )
                            )
                        if target_reflection_evaluator is None:
                            target_reflection_evaluator = (
                                build_mirothinker_target_reflection_runner(
                                    config=reflection_runtime_config,
                                    layout=workspace.layout,
                                )
                            )
                        if target_close_reflection_evaluator is None:
                            target_close_reflection_evaluator = (
                                build_mirothinker_target_close_reflection_runner(
                                    config=reflection_runtime_config,
                                    layout=workspace.layout,
                                )
                            )
                        if target_open_position_reflection_evaluator is None:
                            target_open_position_reflection_evaluator = (
                                build_mirothinker_open_position_reflection_runner(
                                    config=reflection_runtime_config,
                                    layout=workspace.layout,
                                )
                            )
                    missing_reflection_dependencies = (
                        reflection_runtime.missing_reflection_dependencies(
                            outcome_context=outcome_context,
                            evaluate_review=shared_reflection_evaluator,
                            evaluate_target_review=target_reflection_evaluator,
                            evaluate_target_close_review=(
                                target_close_reflection_evaluator
                            ),
                            evaluate_target_open_position_review=(
                                target_open_position_reflection_evaluator
                            ),
                        )
                    )
                    if missing_reflection_dependencies:
                        missing = ", ".join(missing_reflection_dependencies)
                        raise CompositionError(
                            "compose_kernel reflection runtime requires "
                            f"dependencies: {missing}."
                        )
                    reflection_market_mapping_resolver = None
                    if config.validation is not None:

                        def _resolve_reflection_market_mapping(target_key: str):
                            return resolve_market_mapping(config, target_key)

                        reflection_market_mapping_resolver = (
                            _resolve_reflection_market_mapping
                        )
                    reflection_loop_kwargs = {
                        "config": config,
                        "layout": workspace.layout,
                        "ledger": FileBackedEvidenceLedger(workspace.layout),
                        "outcome_context": outcome_context,
                        "trade_context": trade_context,
                        "evaluate_review": shared_reflection_evaluator,
                        "evaluate_target_close_review": (
                            target_close_reflection_evaluator
                        ),
                        "evaluate_target_open_position_review": (
                            target_open_position_reflection_evaluator
                        ),
                        "market_data": active_market_data_provider,
                        "resolve_market_mapping": (
                            reflection_market_mapping_resolver
                        ),
                        "emit": lifecycle_emit,
                        "now": nautilus_node.clock.utc_now,
                    }
                    if target_reflection_evaluator is not None:
                        reflection_loop_kwargs["evaluate_target_review"] = (
                            target_reflection_evaluator
                        )
                    reflection_loop = ReflectionReviewLoop(**reflection_loop_kwargs)
                    reflection_work_queue = FileBackedReflectionWorkQueue(
                        root=active_layout.runtime_root / "reflection_work_queue",
                        now=nautilus_node.clock.utc_now,
                    )
                    reflection_outbox = FileBackedReflectionCompletionOutbox(
                        root=active_layout.runtime_root / "reflection_outbox",
                    )
                    runtime_graph_reflection = ReflectionRuntimeWiring(
                        queue=reflection_work_queue,
                        outbox=reflection_outbox,
                        execute_cycle=lambda cycle_number, checked_at: (
                            reflection_loop.run_heartbeat_at(
                                heartbeat_number=cycle_number,
                                checked_at=checked_at,
                            )
                        ),
                    )
                pm_review_runtime_wiring = None

                if active_pm_review_runtime_policy.auto_dispatch_after_analysis:
                    pm_review_work_queue = FileBackedPMReviewWorkQueue(
                        root=active_layout.runtime_root / "pm_review_work_queue",
                        now=nautilus_node.clock.utc_now,
                    )
                    pm_review_outbox = FileBackedPMReviewCompletionOutbox(
                        root=active_layout.runtime_root / "pm_review_outbox",
                    )
                    pm_review_resolver = RuntimePMReviewRequestResolver(
                        layout=active_layout,
                        migration_id=DEFAULT_CUTOVER_MIGRATION_ID,
                    )
                    pm_review_runtime_wiring = PMReviewRuntimeWiring(
                        queue=pm_review_work_queue,
                        outbox=pm_review_outbox,
                        resolver=pm_review_resolver,
                        execute=pm_review_runtime.build_runtime_pm_review_executor(
                            layout=active_layout,
                            task_runner=active_pm_review_task_runner,
                            config=config,
                            market_data_provider=active_market_data_provider,
                            execute_pm_decisions=(
                                active_pm_review_runtime_policy.execute_pm_decisions
                            ),
                            migration_id=DEFAULT_CUTOVER_MIGRATION_ID,
                            emit=lifecycle_emit,
                            now=nautilus_node.clock.utc_now,
                        ),
                    )

                runtime_graph = EventTraderRuntimeGraph(
                    msgbus=nautilus_node.msgbus,
                    checker_queue=checker_work_queue,
                    checker_outbox=checker_outbox,
                    checker=primary_runtime.checker_worker_runner,
                    analysis_queue=analysis_work_queue,
                    analysis_outbox=analysis_outbox,
                    analysis_resolver=analysis_resolver,
                    analysis=research_pipeline.build_runtime_analysis_executor(
                        ceau_store=FileBackedCEAUStore(active_layout),
                        dispatch_analysis=primary_runtime.dispatch_analysis,
                        now=nautilus_node.clock.utc_now,
                    ),
                    pm_review=pm_review_runtime_wiring,
                    reflection=runtime_graph_reflection,
                    reflection_obligation_store=FileBackedReflectionObligationStore(
                        active_layout
                    ),
                    runtime_workers=config.runtime_workers,
                    admission_port=admission_adapter,
                    ceau_port=primary_runtime.ceau_port,
                    nautilus_node=nautilus_node,
                    now=nautilus_node.clock.utc_now,
                )
                runtime_graph.start()
                raw_source_publisher = RuntimeRawSourcePublisher(
                    msgbus=nautilus_node.msgbus,
                    drain_runtime_graph=runtime_graph.drain_all,
                )
                admitted_evidence_path = _RuntimeGraphAdmissionReleasePath(
                    downstream=admitted_evidence_path,
                    runtime_graph=runtime_graph,
                    observe_admitted_event=primary_runtime.observe_admitted_event,
                )
                primary_hooks = primary_runtime.hooks

                def _advance_runtime_graph_time(
                    observed_at: datetime,
                ) -> research_pipeline.RuntimeTimeObservation:
                    observation = primary_hooks.advance_time_to(observed_at)
                    for materialized in observation.pm_review_requests:
                        request = materialized.request
                        recorded_at = nautilus_node.clock.utc_now()
                        requested = PMReviewRequested(
                            **base_message_kwargs(
                                message_id=(
                                    f"pm-review-requested:{request.request_id}"
                                ),
                                event_time=request.business_at,
                                recorded_at=recorded_at,
                                correlation_id=request.request_id,
                                causation_id=None,
                                idempotency_key=(
                                    "pm_review_requested:"
                                    f"{request.target_key}:{request.request_id}:"
                                    "position_trigger"
                                ),
                            ),
                            target_key=request.target_key,
                            pm_review_request_id=request.request_id,
                            pm_review_request_ref=materialized.request_ref,
                        )
                        nautilus_node.msgbus.publish(requested.topic, requested, False)
                    runtime_graph.drain_all()
                    return observation

                runtime_time_hooks = research_pipeline.RuntimeTimeAdvanceHooks(
                    finalize=lambda: (
                        research_pipeline.finalize_runtime_graph_pipeline(
                            primary_hooks=primary_hooks,
                            runtime_graph=runtime_graph,
                        )
                    ),
                    advance_time_to=_advance_runtime_graph_time,
                    next_deferred_time_before=(
                        primary_hooks.next_deferred_time_before
                    ),
                )
                runtime_time_finalizer = runtime_time_hooks.finalize
                runtime_time_advancer = runtime_time_hooks.advance_time_to
                lifecycle_emit(
                    "nautilus runtime graph: admitted evidence now enters the "
                    "owned checker/CEAU/analysis path directly"
                )
                lifecycle_emit(
                    "nautilus runtime graph: AnalysisRequested now drains through "
                    "a durable analysis queue/outbox and executes real analysis "
                    "work in the runtime worker"
                )
                if pm_review_runtime_wiring is None:
                    lifecycle_emit(
                        "nautilus runtime graph: replay/live CEAU close/final-drain "
                        "now flushes runtime-owned analysis work through the graph"
                    )
                else:
                    lifecycle_emit(
                        "nautilus runtime graph: PMReview now fans out from "
                        "AnalysisOutcome through a durable PMReview queue/outbox "
                        "and executes real PMReview work in the runtime worker"
                    )
                    lifecycle_emit(
                        "nautilus runtime graph: replay/live CEAU close/final-drain "
                        "also flushes runtime-owned analysis-event PMReview work "
                        "through the graph"
                    )
            if config.mode == "replay":
                replay_runner = ReplayRunner(
                    downstream_path=admitted_evidence_path,
                )
            if config.mode == "live":
                if config.live is None:
                    raise CompositionError(
                        "compose_kernel requires [live] config when mode is 'live'."
                    )
                lifecycle_emit(
                    "live runtime: source acquisition stays outside the kernel; "
                    "admitted evidence must enter through the composed downstream path"
                )
        live_host = None
        if config.mode == "live" and admitted_evidence_path is not None:
            if runtime_graph is None or not runtime_graph.has_reflection_runtime:
                raise CompositionError(
                    "Live host requires reflection observability to be installed."
                )
            if nautilus_node is None:
                raise CompositionError(
                    "Live host requires a Nautilus node for reflection scheduling."
                )
            kernel = EventTraderKernel(
                config=config,
                workspace=workspace,
                emit=lifecycle_emit,
                sleep=time.sleep,
            )
            resident_runner = _ResidentKernelRunner(kernel=kernel)
            reflection_scheduler = ReflectionCycleScheduler(
                clock=nautilus_node.clock,
                interval=timedelta(hours=config.reflection_interval_hours),
                enqueue_reflection_cycle=runtime_graph.enqueue_reflection_cycle,
                drain_reflection_work=runtime_graph.drain_reflection_work,
                pump_reflection_completions=runtime_graph.pump_reflection_completions,
            )
            owned_resources.register(reflection_scheduler.close)
            live_host_runner = reflection_runtime.LiveReflectionHostRunner(
                kernel=kernel,
                resident_runner=resident_runner,
                reflection_scheduler=reflection_scheduler,
            )
            live_host = LiveRuntimeHost(
                _run=live_host_runner.run_blocking,
                _start=live_host_runner.start,
                _join=live_host_runner.join,
                _stop=lambda reason: live_host_runner.stop(reason=reason),
                _release_admitted=admitted_evidence_path.release_admitted,
                _state=lambda: kernel.state,
                _heartbeat_count=lambda: kernel.heartbeat_count,
                _transition_history=lambda: kernel.transition_history,
                _last_reflection_receipt=lambda: runtime_graph.last_reflection_receipt,
                _advance_runtime_time=runtime_time_hooks.advance_time_to,
            )
        replay_runtime = None
        if replay_runner is not None:
            if runtime_graph is None or not runtime_graph.has_reflection_runtime:
                raise CompositionError(
                    "Replay runtime requires an explicit reflection execution seam."
                )
            replay_runtime = BoundedReplayRuntime(
                _runner=replay_runner,
                _drain_reflection_cycle=runtime_graph.run_reflection_cycle,
                _last_reflection_receipt=lambda: runtime_graph.last_reflection_receipt,
                _finalize_pipeline=runtime_time_finalizer,
                _observe_replay_time=runtime_time_advancer,
                _next_replay_deferred_time_before=(
                    runtime_time_hooks.next_deferred_time_before
                ),
                _resume_runtime_backlog=lambda _replay_at: runtime_graph.drain_all(),
                _advance_replay_clock=lambda replay_at: _set_test_clock_time(
                    nautilus_node.clock,
                    replay_at,
                ),
            )
        return ComposedKernel(
            config=config,
            workspace=workspace,
            capabilities=capabilities,
            live_host=live_host,
            replay_runtime=replay_runtime,
            nautilus_node=nautilus_node,
            runtime_graph=runtime_graph,
            raw_source_publisher=raw_source_publisher,
            market_data_provider=active_market_data_provider,
            _owned_resources=owned_resources,
        )
    except Exception:
        try:
            owned_resources.close()
        except Exception:
            pass
        raise


def _set_test_clock_time(clock: TestClock, replay_at: datetime) -> None:
    if not isinstance(clock, TestClock):
        raise CompositionError("replay runtime clock must be a Nautilus TestClock.")
    if not isinstance(replay_at, datetime) or replay_at.tzinfo is None:
        raise CompositionError("replay_at must be a timezone-aware datetime.")
    clock.set_time(int(replay_at.timestamp() * 1_000_000_000))
