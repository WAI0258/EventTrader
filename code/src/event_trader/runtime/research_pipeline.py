"""Primary runtime research pipeline ownership outside the composition root."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from event_trader.analysis import (
    AnalysisContextLoader,
    FileBackedResearchMemoryReader,
    current_target_seed_page_paths,
    historical_target_seed_page_paths,
)
from event_trader.analysis_request_handler import (
    AnalysisCallback,
    build_analysis_request_handler,
)
from event_trader.ceau import (
    CEAUAnalysisDispatchResult,
    CEAUAnalysisExecutionFence,
    CEAUAnalysisExecutionItem,
    CEAUUnitEmittedRecord,
    FileBackedCEAUStore,
    LiveCEAUCoordinator,
    ReplayCEAUCoordinator,
    UnitFormationLane,
)
from event_trader.checker.context_pack import (
    CheckerContextPack,
    CheckerContextPackBuilder,
    CheckerPackConfig,
)
from event_trader.checker.recorder import FileBackedCheckerDecisionRecorder
from event_trader.checker.runtime import build_checker_policy
from event_trader.checker.validator import (
    ConservativeCheckerValidator,
    RawCheckerDecision,
)
from event_trader.composition_error import CompositionError
from event_trader.config import (
    AnalysisAgentConfig,
    CheckerAgentConfig,
    KernelConfig,
    resolve_validation_execution_direction_mode,
)
from event_trader.context_assembly import write_replay_visibility_audit_from_store
from event_trader.contracts import AnalysisRequest, CheckerDecision
from event_trader.contracts.research_memory import initialize_target_research_layout
from event_trader.contracts.runtime import CheckerRequest
from event_trader.contracts.view_state_change import MarketMapping
from event_trader.decision_memory import (
    FileBackedDecisionEpisodeStore,
    derive_decision_episode_id,
    project_decision_episodes,
)
from event_trader.evidence_ledger import (
    FileBackedEvidenceLedger,
    read_evidence,
    read_evidence_window,
)
from event_trader.feeds.models import LiveNewsStreamInput, LiveWebSearchInput
from event_trader.ingest.admission import AdmissionOutputs, AdmissionRequest
from event_trader.market.context_builder import build_market_context_snapshot
from event_trader.market.contracts import MarketContextSnapshot
from event_trader.market.provider import MarketBarsProvider
from event_trader.operator_context import CanonicalOperatorContextReader
from event_trader.pm_review.runtime import (
    MaterializedPMReviewRequest,
    PMReviewRuntimePolicy,
    dispatch_due_pm_reviews_for_target,
    due_open_position_pm_review_requests_without_decision,
    materialize_trigger_driven_pm_review_requests,
    next_pm_position_review_trigger_at_before,
)
from event_trader.portfolio import PMDecisionStore, PortfolioStateStore
from event_trader.projection.current_state import CurrentStateProjectionLoader
from event_trader.projection.report_artifacts import (
    FileBackedCurrentStateReportWriter,
    MarkdownCurrentStateReportRenderer,
)
from event_trader.reasoning.analysis_runtime import build_analysis_runner
from event_trader.reasoning.runtime import AgentRuntime
from event_trader.runtime.admission import RawSourceUnavailable
from event_trader.runtime.analysis_receipts import (
    _analysis_failure_business_at,
    _append_failed_analysis_decision_record,
    _ensure_analysis_outcome_receipt,
)
from event_trader.runtime.analysis_resolver import RuntimeAnalysisResolution
from event_trader.runtime.ceau import RuntimeCEAUEngineAdapter
from event_trader.runtime.checker_worker import CheckerWorkerDecision
from event_trader.runtime.contracts import (
    AnalysisRequested,
    NewsRaw,
    WebResultRaw,
    base_message_kwargs,
)
from event_trader.runtime.graph import EventTraderRuntimeGraph
from event_trader.runtime.queue import CheckerWorkItem
from event_trader.source_archive.market_news import (
    market_news_record_id,
    read_market_news_records,
)
from event_trader.source_archive.web_search import (
    historical_web_search_observation_id,
    read_historical_web_search_records,
)
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision import (
    HistoricalThesisSnapshotReader,
    ThesisRevisionStore,
)

type RuntimeEmitter = Callable[[str], None]
type PipelineStageObserver = Callable[[str, str], None]


@dataclass(frozen=True, slots=True)
class RuntimeTimeObservation:
    """Deferred runtime effects produced by advancing time."""

    pm_review_requests: tuple[MaterializedPMReviewRequest, ...] = ()
    ceau_emitted_count: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeTimeAdvanceHooks:
    """Runtime-owned hooks for durable time-driven side effects."""

    finalize: Callable[[], None]
    advance_time_to: Callable[[datetime], RuntimeTimeObservation]
    next_deferred_time_before: Callable[[datetime], datetime | None] = field(
        default=lambda _boundary: None
    )


@dataclass(frozen=True, slots=True)
class PrimaryResearchRuntimeInstallation:
    """Reusable runtime dependencies for the composed primary research path."""

    hooks: RuntimeTimeAdvanceHooks
    checker_worker_runner: Callable[[CheckerWorkItem], CheckerWorkerDecision]
    ceau_port: RuntimeCEAUEngineAdapter
    observe_admitted_event: Callable[[AdmissionOutputs], None]
    dispatch_analysis: Callable[
        [str, AnalysisRequest, UnitFormationLane | None],
        CEAUAnalysisDispatchResult,
    ]


def finalize_runtime_graph_pipeline(
    *,
    primary_hooks: RuntimeTimeAdvanceHooks,
    runtime_graph: EventTraderRuntimeGraph,
) -> None:
    primary_hooks.finalize()
    runtime_graph.drain_all()


def install_primary_research_runtime(
    *,
    config: KernelConfig,
    layout: WorkspaceLayout,
    runtime_mode: str,
    now: Callable[[], datetime],
    emit: RuntimeEmitter,
    pipeline_stage_observer: PipelineStageObserver | None,
    analysis_config: AnalysisAgentConfig,
    checker_config: CheckerAgentConfig,
    replay_market_mapping: MarketMapping | None,
    market_data_provider: MarketBarsProvider | None,
    config_path: Path,
    market_data_store_root: Path | None,
    market_data_snapshot_id: str | None,
    memory_read_policy: str,
    replay_run_id: str,
    pm_review_runtime_policy: PMReviewRuntimePolicy,
    pm_review_task_runner: AgentRuntime | None,
    publish_runtime_analysis_requested: Callable[[CEAUUnitEmittedRecord], None],
    runtime_owned_analysis_pm_review: bool,
    checker_policy: Callable[[CheckerContextPack], RawCheckerDecision] | None,
    analysis_callback: AnalysisCallback | None,
) -> PrimaryResearchRuntimeInstallation:
    if not callable(now):
        raise CompositionError("primary research runtime now must be callable.")
    ledger = FileBackedEvidenceLedger(layout)
    checker_research_memory_reader = FileBackedResearchMemoryReader(layout)
    operator_context_reader = CanonicalOperatorContextReader(layout)
    thesis_revision_store = ThesisRevisionStore(layout)
    market_context_max_prompt_chars = 6_000
    if config.market_context is not None and config.market_context.enabled:
        market_context_max_prompt_chars = config.market_context.max_prompt_chars

    def _build_runtime_market_context(
        target_key: str,
        as_of_at: datetime,
    ) -> MarketContextSnapshot | None:
        return build_market_context_snapshot(
            config=config,
            target_key=target_key,
            as_of_at=as_of_at,
            market_data_provider=market_data_provider,
        )

    def _build_runtime_research_memory(business_at: datetime):
        if runtime_mode == "replay":
            return HistoricalThesisSnapshotReader(
                layout,
                business_at=business_at,
                store=thesis_revision_store,
            )
        return checker_research_memory_reader

    pack_builder = CheckerContextPackBuilder(
        read_evidence=lambda event_ids: read_evidence(event_ids, ledger=ledger),
        read_page=None,
        config=CheckerPackConfig(
            evidence_excerpt_chars=checker_config.evidence_excerpt_chars,
            section_excerpt_chars=checker_config.section_excerpt_chars,
            recent_timeline_items=checker_config.recent_timeline_items,
            max_pack_chars=checker_config.max_pack_chars,
            force_escalate_on_insufficient_coverage=(
                checker_config.force_escalate_on_insufficient_coverage
            ),
        ),
        build_research_memory=_build_runtime_research_memory,
        read_evidence_window=lambda target_key, end_at, exclude_event_ids: (
            read_evidence_window(
                start_at=datetime.min.replace(tzinfo=UTC),
                end_at=end_at,
                ledger=ledger,
                target_keys=[target_key],
                exclude_event_ids=exclude_event_ids,
            )
        ),
        build_market_context=_build_runtime_market_context,
        market_context_max_prompt_chars=market_context_max_prompt_chars,
    )
    checker: Callable[[CheckerContextPack], RawCheckerDecision]
    if checker_policy is None:
        checker = build_checker_policy(config=checker_config)
    else:
        checker = checker_policy
    checker_validator = ConservativeCheckerValidator(
        force_escalate_on_insufficient_coverage=(
            checker_config.force_escalate_on_insufficient_coverage
        ),
    )
    if config.stream_routing is None or not config.stream_routing.enabled:
        raise CompositionError(
            "Primary research runtime requires [stream_routing] CEAU policy."
        )
    stream_target_key = config.stream_routing.target_key
    ceau_coordinator: ReplayCEAUCoordinator | None = None
    live_ceau_coordinator: LiveCEAUCoordinator | None = None
    ceau_store = FileBackedCEAUStore(layout)
    if runtime_mode == "replay":
        ceau_coordinator = ReplayCEAUCoordinator(
            store=ceau_store,
            stream_routing_config=config.stream_routing,
        )
    else:
        live_ceau_coordinator = LiveCEAUCoordinator(
            store=ceau_store,
            stream_routing_config=config.stream_routing,
        )
    checker_worker_runner = build_runtime_graph_checker_runner(
        layout=layout,
        pack_builder=pack_builder,
        decision_recorder=FileBackedCheckerDecisionRecorder(
            layout,
            memory_read_policy=memory_read_policy,
            runtime_scope=("replay" if runtime_mode == "replay" else "live"),
            run_id=(replay_run_id if runtime_mode == "replay" else ""),
        ),
        checker=checker,
        checker_validator=checker_validator,
        pipeline_stage_observer=pipeline_stage_observer,
    )
    analysis_context_loader = AnalysisContextLoader(
        read_evidence=lambda event_ids: read_evidence(event_ids, ledger=ledger),
        research_memory=None,
        build_research_memory=_build_runtime_research_memory,
        build_market_context=_build_runtime_market_context,
        market_context_max_prompt_chars=market_context_max_prompt_chars,
        target_seed_page_paths=(
            current_target_seed_page_paths
            if runtime_mode != "replay"
            else historical_target_seed_page_paths
        ),
        read_operator_context=operator_context_reader.read,
    )
    analyze = analysis_callback
    if analyze is None:
        analyze = build_analysis_runner(
            analysis_agent=analysis_config,
            layout=layout,
            runtime_mode=runtime_mode,
            config_path=config_path,
            market_data_store_root=market_data_store_root,
            market_data_snapshot_id=market_data_snapshot_id,
            memory_read_policy=memory_read_policy,
            runtime_scope=("replay" if runtime_mode == "replay" else "live"),
            run_id=(replay_run_id if runtime_mode == "replay" else ""),
            execution_direction_mode=resolve_validation_execution_direction_mode(
                config,
                target_key=stream_target_key,
            ),
        )
    analysis_handler = build_analysis_request_handler(
        context_loader=analysis_context_loader,
        analyze=analyze,
    )
    projection_loader = CurrentStateProjectionLoader(
        read_evidence=lambda event_ids: read_evidence(event_ids, ledger=ledger),
        research_memory=checker_research_memory_reader,
        read_decision_episodes=lambda target_key: project_decision_episodes(
            FileBackedDecisionEpisodeStore(layout).read_records(target_key=target_key)
        ),
        read_portfolio_state=lambda target_key: PortfolioStateStore(layout).read(
            target_key=target_key
        ),
        read_latest_pm_decision=lambda target_key: _latest_pm_decision(
            layout=layout,
            target_key=target_key,
        ),
    )
    projection_renderer = MarkdownCurrentStateReportRenderer()
    projection_writer = FileBackedCurrentStateReportWriter(layout)

    def _dispatch_analysis(
        lane: str,
        request,
        unit_formation_lane: UnitFormationLane | None = None,
    ) -> CEAUAnalysisDispatchResult:
        _observe_pipeline_stage(
            pipeline_stage_observer,
            event_id=request.event_ids[0],
            stage="analysis",
        )
        try:
            result = analysis_handler(lane, request, unit_formation_lane)
        except Exception as exc:
            if request.decision_episode_id:
                business_at = _analysis_failure_business_at(
                    layout=layout,
                    context_loader=analysis_context_loader,
                    request=request,
                    unit_formation_lane=unit_formation_lane,
                )
                _append_failed_analysis_decision_record(
                    layout=layout,
                    request=request,
                    business_at=business_at,
                    error=exc,
                    recorded_at=now(),
                    context_packet_id=getattr(exc, "context_packet_id", None),
                    context_packet_hash=getattr(exc, "context_packet_hash", None),
                )
            raise

        outcome_receipt_ref = _ensure_analysis_outcome_receipt(
            layout=layout,
            context_loader=analysis_context_loader,
            request=request,
            unit_formation_lane=unit_formation_lane,
            result=result,
            committed_at=now(),
        )

        def _refresh_projection(target_key: str) -> None:
            rendered_projection = projection_renderer.render(
                projection_loader.load_model(target_key)
            )
            written_projection = projection_writer.write(rendered_projection)
            emit(
                "projection artifact: updated "
                f"target_key={target_key} "
                f"artifact_path={written_projection.receipt.artifact_path}"
            )

        if result.outcome == "memory_updated":
            _refresh_projection(result.target_key)
        emit(
            f"analysis outcome: {result.outcome} "
            f"target_key={result.target_key} "
            f"event_ids={list(result.event_ids)!r}"
        )
        return CEAUAnalysisDispatchResult(
            analysis_result=result,
            outcome_receipt_ref=outcome_receipt_ref,
        )

    def _dispatch_due_target_pm_reviews(
        *,
        target_key: str,
        run_until: datetime,
    ) -> tuple[MaterializedPMReviewRequest, ...]:
        if not pm_review_runtime_policy.auto_dispatch_after_analysis:
            return ()
        if runtime_owned_analysis_pm_review:
            materialized = materialize_trigger_driven_pm_review_requests(
                layout=layout,
                config=config,
                run_until=run_until,
                market_data_provider=market_data_provider,
                emit=emit,
                target_keys=(target_key,),
            )
            due = due_open_position_pm_review_requests_without_decision(
                layout=layout,
                target_key=target_key,
                run_until=run_until,
            )
            by_request_id = {item.request.request_id: item for item in due}
            by_request_id.update(
                {item.request.request_id: item for item in materialized}
            )
            return tuple(
                sorted(
                    by_request_id.values(),
                    key=lambda item: (
                        item.request.business_at,
                        item.request.request_id,
                    ),
                )
            )
        if pm_review_task_runner is None:
            raise CompositionError("automatic PMReview dispatch requires a task runner.")
        dispatch_due_pm_reviews_for_target(
            layout=layout,
            config=config,
            policy=pm_review_runtime_policy,
            task_runner=pm_review_task_runner,
            market_data_provider=market_data_provider,
            emit=emit,
            target_key=target_key,
            run_until=run_until,
            runtime_owned_analysis_pm_review=runtime_owned_analysis_pm_review,
        )
        return ()

    latest_replay_business_at: datetime | None = None
    latest_observed_replay_at: datetime | None = None

    def _observe_admitted_event(outputs: AdmissionOutputs) -> None:
        nonlocal latest_replay_business_at
        if not isinstance(outputs, AdmissionOutputs):
            raise CompositionError(
                "runtime graph admission observer requires AdmissionOutputs."
            )
        initialize_target_research_layout(layout, outputs.runtime_event.target_key)
        if runtime_mode == "replay":
            latest_replay_business_at = outputs.runtime_event.ts_event

    def _dispatch_ceau_emitted_record(emitted_record: CEAUUnitEmittedRecord) -> None:
        publish_runtime_analysis_requested(emitted_record)

    def _finalize_without_replay_views() -> None:
        if runtime_mode == "replay":
            replay_end_time = latest_observed_replay_at or latest_replay_business_at
            if ceau_coordinator is not None and replay_end_time is not None:
                for emitted_record in ceau_coordinator.final_drain(
                    replay_end_time=replay_end_time,
                ):
                    _dispatch_ceau_emitted_record(emitted_record)
            write_replay_visibility_audit_from_store(
                layout=layout,
                run_id=replay_run_id,
            )

    def _next_deferred_time_before(boundary: datetime) -> datetime | None:
        if runtime_mode != "replay" or ceau_coordinator is None:
            ceau_deadline = None
        else:
            pending_deadlines = tuple(
                unit.deadline_at
                for unit in ceau_coordinator.pending_units
                if unit.deadline_at <= boundary
            )
            ceau_deadline = min(pending_deadlines) if pending_deadlines else None
        pm_trigger_at = None
        if runtime_mode == "replay":
            pm_trigger_at = next_pm_position_review_trigger_at_before(
                layout=layout,
                config=config,
                boundary=boundary,
                market_data_provider=market_data_provider,
                emit=emit,
            )
        candidates = tuple(
            candidate
            for candidate in (ceau_deadline, pm_trigger_at)
            if candidate is not None
        )
        if not candidates:
            return None
        return min(candidates)

    def _advance_time_to(observed_at: datetime) -> RuntimeTimeObservation:
        nonlocal latest_observed_replay_at
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise CompositionError("observed_at must be a timezone-aware datetime.")
        if runtime_mode == "replay":
            latest_observed_replay_at = observed_at
        ceau_emitted_count = 0
        if runtime_mode == "replay" and ceau_coordinator is not None:
            ceau_coordinator.observe_replay_time(replay_at=observed_at)
            for emitted_record in ceau_coordinator.pre_close_before_event(
                current_event_time=observed_at,
            ):
                _dispatch_ceau_emitted_record(emitted_record)
                ceau_emitted_count += 1
        if runtime_mode == "live" and live_ceau_coordinator is not None:
            for emitted_record in live_ceau_coordinator.close_due_by_processing_time(
                current_time=observed_at,
            ):
                _dispatch_ceau_emitted_record(emitted_record)
                ceau_emitted_count += 1
        pm_review_requests: list[MaterializedPMReviewRequest] = []
        for target_key in sorted(PortfolioStateStore(layout).read_all()):
            pm_review_requests.extend(
                _dispatch_due_target_pm_reviews(
                    target_key=target_key,
                    run_until=observed_at,
                )
            )
        return RuntimeTimeObservation(
            pm_review_requests=tuple(pm_review_requests),
            ceau_emitted_count=ceau_emitted_count,
        )

    _ = replay_market_mapping
    return PrimaryResearchRuntimeInstallation(
        hooks=RuntimeTimeAdvanceHooks(
            finalize=_finalize_without_replay_views,
            advance_time_to=_advance_time_to,
            next_deferred_time_before=_next_deferred_time_before,
        ),
        checker_worker_runner=checker_worker_runner,
        ceau_port=RuntimeCEAUEngineAdapter(
            ledger=ledger,
            mode=cast("Literal['replay', 'live']", runtime_mode),
            replay_coordinator=ceau_coordinator,
            live_coordinator=live_ceau_coordinator,
        ),
        observe_admitted_event=_observe_admitted_event,
        dispatch_analysis=_dispatch_analysis,
    )


def build_runtime_graph_checker_runner(
    *,
    layout: WorkspaceLayout,
    pack_builder: CheckerContextPackBuilder,
    decision_recorder: FileBackedCheckerDecisionRecorder,
    checker: Callable[[CheckerContextPack], RawCheckerDecision],
    checker_validator: ConservativeCheckerValidator,
    pipeline_stage_observer: PipelineStageObserver | None = None,
) -> Callable[[CheckerWorkItem], CheckerWorkerDecision]:
    def _run(item: CheckerWorkItem) -> CheckerWorkerDecision:
        if not isinstance(item, CheckerWorkItem):
            raise CompositionError(
                "runtime graph checker runner requires a CheckerWorkItem."
            )
        initialize_target_research_layout(layout, item.target_key)
        _observe_pipeline_stage(
            pipeline_stage_observer,
            event_id=item.event_id,
            stage="checker",
        )
        request = CheckerRequest(
            target_key=item.target_key,
            event_ids=[item.event_id],
        )
        pack = pack_builder.build(request)
        raw_decision = checker(pack)
        validation = checker_validator.validate(
            pack=pack,
            raw_decision=raw_decision,
        )
        receipt_path = decision_recorder.append_pack(
            pack=pack,
            decision=validation.decision,
            caused_analysis_request=validation.decision.decision == "escalate",
            validator_action=validation.validator_action,
            raw_policy_output=_raw_checker_policy_output(raw_decision),
            llm_usage=raw_decision.llm_usage,
        )
        return CheckerWorkerDecision(
            decision=validation.decision,
            decision_record_ref=f"{receipt_path.resolve(strict=False)}#{item.event_id}",
            validator_action=validation.validator_action,
            decision_episode_id=_checker_decision_episode_id(
                decision=validation.decision,
                pack=pack,
            ),
        )

    return _run


def build_runtime_analysis_executor(
    *,
    ceau_store: FileBackedCEAUStore,
    dispatch_analysis: Callable[
        [str, AnalysisRequest, UnitFormationLane | None],
        CEAUAnalysisDispatchResult,
    ],
    now: Callable[[], datetime] | None = None,
) -> Callable[[RuntimeAnalysisResolution], CEAUAnalysisDispatchResult]:
    """Build the runtime-owned analysis executor from the existing analysis path."""

    if not isinstance(ceau_store, FileBackedCEAUStore):
        raise CompositionError("ceau_store must be a FileBackedCEAUStore.")
    if not callable(dispatch_analysis):
        raise CompositionError("dispatch_analysis must be callable.")
    if now is not None and not callable(now):
        raise CompositionError("now must be callable when supplied.")

    def _execute(resolution: RuntimeAnalysisResolution) -> CEAUAnalysisDispatchResult:
        if not isinstance(resolution, RuntimeAnalysisResolution):
            raise CompositionError(
                "resolution must be a RuntimeAnalysisResolution instance."
            )
        return CEAUAnalysisExecutionFence(
            store=ceau_store,
            dispatch_analysis=dispatch_analysis,
            now=now,
            raise_on_failure=True,
        ).enqueue_and_drain(
            CEAUAnalysisExecutionItem(
                emitted_record=resolution.emitted_record,
                request=resolution.request,
                unit_formation_lane=resolution.unit_formation_lane,
                business_at=resolution.unit_formation_lane.business_at,
            )
        )

    return _execute


def _checker_decision_episode_id(
    *,
    decision: CheckerDecision,
    pack: CheckerContextPack,
) -> str:
    if decision.decision != "escalate":
        return ""
    return derive_decision_episode_id(
        target_key=decision.target_key,
        first_event_id=decision.event_ids[0],
        checker_business_at=pack.evidence.ts_event,
    )


def _raw_checker_policy_output(raw_decision: RawCheckerDecision) -> dict[str, object]:
    no_action_support = raw_decision.no_action_support
    return {
        "decision": raw_decision.decision,
        "uncertainty": raw_decision.uncertainty,
        "possible_watchlist_trigger": raw_decision.possible_watchlist_trigger,
        "operational_status": raw_decision.operational_status,
        "operational_detail": raw_decision.operational_detail,
        "no_action_support": (
            None
            if no_action_support is None
            else {
                "support_type": no_action_support.support_type,
                "supporting_sections": list(no_action_support.supporting_sections),
                "support_summary": no_action_support.support_summary,
            }
        ),
    }


def _serialize_raw_checker_policy_output(
    raw_decision: RawCheckerDecision,
) -> dict[str, object]:
    support = raw_decision.no_action_support
    return {
        "decision": raw_decision.decision,
        "uncertainty": raw_decision.uncertainty,
        "possible_watchlist_trigger": raw_decision.possible_watchlist_trigger,
        "operational_status": raw_decision.operational_status,
        "operational_detail": raw_decision.operational_detail,
        "no_action_support": (
            None
            if support is None
            else {
                "support_type": support.support_type,
                "supporting_sections": list(support.supporting_sections),
                "support_summary": support.support_summary,
            }
        ),
    }


def runtime_analysis_requested_message(
    *,
    emitted_record: CEAUUnitEmittedRecord,
) -> AnalysisRequested:
    return AnalysisRequested(
        **base_message_kwargs(
            message_id=(
                f"analysis-requested:{emitted_record.analysis_unit_id}:"
                f"{emitted_record.record_id}"
            ),
            event_time=emitted_record.recorded_at,
            recorded_at=emitted_record.recorded_at,
            correlation_id=f"evidence:{emitted_record.primary_event_ids[0]}",
            causation_id=f"ceau-unit-emitted:{emitted_record.record_id}",
            idempotency_key=(
                f"analysis_requested:{emitted_record.analysis_unit_id}:"
                f"{emitted_record.record_id}"
            ),
        ),
        target_key=emitted_record.target_key,
        analysis_unit_id=emitted_record.analysis_unit_id,
        record_id=emitted_record.record_id,
    )


def load_archived_news_admission_request(
    *,
    layout: WorkspaceLayout,
    message: NewsRaw,
) -> AdmissionRequest:
    records = read_market_news_records(
        layout,
        target_key=message.target_key,
        start_at=message.event_time,
        end_at=message.event_time,
    )
    for record in records:
        if market_news_record_id(record) != message.record_id:
            continue
        if record.source_ref != message.source_ref:
            raise RawSourceUnavailable(
                "market_news source_ref does not match raw message."
            )
        return AdmissionRequest(
            target_key=record.target_key,
            ingress_input=LiveNewsStreamInput(
                source_ref=record.source_ref,
                headline=record.headline,
                body=record.content_text,
                published_at=record.created_at,
                captured_at=record.captured_at,
            ),
            labels=list(record.labels),
        )
    raise RawSourceUnavailable(
        f"market_news archive record not found: record_id={message.record_id}"
    )


def load_archived_web_admission_request(
    *,
    layout: WorkspaceLayout,
    message: WebResultRaw,
) -> AdmissionRequest:
    records = read_historical_web_search_records(
        layout,
        target_key=message.target_key,
        start_at=message.event_time,
        end_at=message.event_time,
    )
    for record in records:
        if historical_web_search_observation_id(record) != message.record_id:
            continue
        if record.source_ref != message.source_ref:
            raise RawSourceUnavailable(
                "web_search source_ref does not match raw message."
            )
        return AdmissionRequest(
            target_key=record.target_key,
            ingress_input=LiveWebSearchInput(
                query=record.query,
                source_ref=record.source_ref,
                title=record.title,
                content=record.content,
                discovered_at=record.discovered_at,
                published_at=record.published_at,
            ),
            labels=list(record.labels),
        )
    raise RawSourceUnavailable(
        f"web_search archive record not found: record_id={message.record_id}"
    )


def _observe_pipeline_stage(
    observer: PipelineStageObserver | None,
    *,
    event_id: str,
    stage: str,
) -> None:
    if observer is None:
        return
    observer(event_id, stage)


def _latest_pm_decision(*, layout: WorkspaceLayout, target_key: str):
    decisions = PMDecisionStore(layout).read_records(target_key=target_key)
    if not decisions:
        return None
    return max(
        (item.record for item in decisions),
        key=lambda decision: (
            decision.decision_available_at,
            decision.business_at,
            decision.decision_id,
        ),
    )


__all__ = [
    "PipelineStageObserver",
    "PrimaryResearchRuntimeInstallation",
    "RuntimeTimeAdvanceHooks",
    "RuntimeTimeObservation",
    "build_runtime_graph_checker_runner",
    "build_runtime_analysis_executor",
    "finalize_runtime_graph_pipeline",
    "install_primary_research_runtime",
    "load_archived_news_admission_request",
    "load_archived_web_admission_request",
    "runtime_analysis_requested_message",
]
