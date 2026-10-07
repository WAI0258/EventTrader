"""Project-owned PMReview agent business orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256

from event_trader.contracts import EvidenceLedgerRecord
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.view_state_change import MarketDataBar
from event_trader.episode_memory.projection import PMEpisodeMemoryView
from event_trader.learning_cards import FileBackedLearningCardStore, select_learning_cards
from event_trader.pm_review.context import (
    PMReviewContextError,
    load_candidate_review_anchor_for_request,
    read_visible_evidence,
    read_visible_market_bars,
    read_visible_pm_history,
)
from event_trader.pm_review.contract_repair import (
    PMReviewContractRepairExhausted,
    run_pm_review_contract_repair,
)
from event_trader.pm_review.contracts import (
    CandidateReviewAnchor,
    PMAnalysisSnapshot,
    PMPortfolioRiskSnapshot,
    PMReviewContractError,
    PMReviewEpisodeMemoryReadReceipt,
    PMReviewExecutionDirectionMode,
    PMReviewInput,
    PMReviewRequest,
    PMReviewToolReadReceipt,
    analysis_snapshot_from_assessment,
)
from event_trader.pm_review.errors import (
    PMReviewAgentRuntimeError,
    PMReviewStructuralRuntimeError,
)
from event_trader.pm_review.position_review import (
    PMPositionReviewError,
    build_pm_position_review_triggers,
)
from event_trader.pm_review.prompt import (
    PMReviewOutputProtocol,
    render_pm_review_task_prompt,
    validate_pm_review_prompt_size,
)
from event_trader.pm_review.result import PMReviewRunResult
from event_trader.pm_review.store import (
    PMPositionReviewTriggerStore,
    PMReviewEpisodeMemoryReadReceiptStore,
    PMReviewStoreError,
)
from event_trader.pm_review.tools import (
    REQUIRED_PM_REVIEW_TOOL_NAMES,
    PMReviewAgentToolbox,
    PMReviewToolSession,
    read_pm_episode_memory,
)
from event_trader.portfolio.active_exposure import ActiveExposure
from event_trader.portfolio.contracts import PMDecision
from event_trader.portfolio.pm_decision_adapter import (
    PMDecisionAdapterError,
    append_pm_decision_from_draft,
)
from event_trader.portfolio.store import PortfolioStoreError
from event_trader.reasoning.runtime import (
    AgentRunReceipt,
    AgentRuntime,
    AgentRuntimeContractError,
    AgentTask,
    run_agent_runtime_once_sync,
)
from event_trader.storage import WorkspaceLayout

_PM_PRICE_SURFACE_NOTE = (
    "Use `price_level_roles` and `market_setup_dashboard_md` as the only "
    "authoritative price surfaces. Treat any price numbers inside this prose as "
    "non-authoritative narrative if they disagree."
)

type PMReviewTaskRunner = AgentRuntime
type PMReviewRunner = Callable[[PMReviewRequest], PMReviewRunResult]


@dataclass(frozen=True, slots=True)
class PreparedSourceEpisodeMemory:
    view: PMEpisodeMemoryView
    detailed_receipt: PMReviewEpisodeMemoryReadReceipt
    tool_read_receipt: PMReviewToolReadReceipt


def build_pm_review_runner(
    *,
    layout: WorkspaceLayout,
    task_runner: PMReviewTaskRunner,
    actual_exposure: ActiveExposure,
    evidence_records: tuple[EvidenceLedgerRecord, ...] = (),
    market_bars: tuple[MarketDataBar, ...] = (),
    pm_review_requests: tuple[PMReviewRequest, ...] = (),
    pm_decisions: tuple[PMDecision, ...] = (),
    portfolio_risk_snapshot: PMPortfolioRiskSnapshot | None = None,
    assessment: AnalysisAssessment | None = None,
    available_tool_names: tuple[str, ...] | None = None,
    output_protocol: PMReviewOutputProtocol,
    prompt_max_chars: int | None = None,
    decision_available_at: datetime,
) -> PMReviewRunner:
    """Build a single-request PMReview business runner over one agent runtime."""

    _validate_layout(layout)
    if not callable(getattr(task_runner, "run_once", None)):
        raise PMReviewAgentRuntimeError("task_runner must implement AgentRuntime.run_once.")
    _preflight_pm_review_tools(available_tool_names)

    def run(request: PMReviewRequest) -> PMReviewRunResult:
        return run_pm_review_request(
            layout=layout,
            request=request,
            task_runner=task_runner,
            actual_exposure=actual_exposure,
            evidence_records=evidence_records,
            market_bars=market_bars,
            pm_review_requests=pm_review_requests,
            pm_decisions=pm_decisions,
            portfolio_risk_snapshot=portfolio_risk_snapshot,
            assessment=assessment,
            available_tool_names=available_tool_names,
            output_protocol=output_protocol,
            prompt_max_chars=prompt_max_chars,
            decision_available_at=decision_available_at,
        )

    return run


def run_pm_review_request(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest,
    task_runner: AgentRuntime,
    actual_exposure: ActiveExposure,
    evidence_records: tuple[EvidenceLedgerRecord, ...] = (),
    market_bars: tuple[MarketDataBar, ...] = (),
    pm_review_requests: tuple[PMReviewRequest, ...] = (),
    pm_decisions: tuple[PMDecision, ...] = (),
    portfolio_risk_snapshot: PMPortfolioRiskSnapshot | None = None,
    assessment: AnalysisAssessment | None = None,
    available_tool_names: tuple[str, ...] | None = None,
    execution_direction_mode: PMReviewExecutionDirectionMode = "long_short",
    output_protocol: PMReviewOutputProtocol,
    prompt_max_chars: int | None = None,
    decision_available_at: datetime,
) -> PMReviewRunResult:
    """Run one PMReview request through project-owned quality and commit gates."""

    _validate_layout(layout)
    if not isinstance(request, PMReviewRequest):
        raise PMReviewAgentRuntimeError("request must be a PMReviewRequest instance.")
    if not isinstance(actual_exposure, ActiveExposure):
        raise PMReviewAgentRuntimeError("actual_exposure must be an ActiveExposure instance.")
    if actual_exposure.target_key != request.target_key:
        raise PMReviewAgentRuntimeError("actual_exposure target_key must match request.")
    if not isinstance(decision_available_at, datetime) or decision_available_at.tzinfo is None:
        raise PMReviewAgentRuntimeError("decision_available_at must be a timezone-aware datetime.")
    if decision_available_at < request.business_at:
        raise PMReviewAgentRuntimeError(
            "decision_available_at must be at or after request.business_at."
        )
    if assessment is not None:
        if not isinstance(assessment, AnalysisAssessment):
            raise PMReviewAgentRuntimeError(
                "assessment must be an AnalysisAssessment instance when supplied."
            )
        if assessment.target_key != request.target_key:
            raise PMReviewAgentRuntimeError("assessment target_key must match request.")
    _preflight_pm_review_tools(available_tool_names)
    try:
        candidate_anchor = load_candidate_review_anchor_for_request(
            layout=layout,
            request=request,
        )
    except (PMReviewContextError, PMReviewStoreError) as exc:
        raise PMReviewStructuralRuntimeError(str(exc)) from exc
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
        portfolio_risk_snapshot=portfolio_risk_snapshot,
        candidate_anchor=candidate_anchor,
        execution_direction_mode=execution_direction_mode,
        source_episode_memory_view=(
            prepared_source_episode_memory.view
            if prepared_source_episode_memory is not None
            else None
        ),
        source_episode_memory_read_receipt=(
            prepared_source_episode_memory.detailed_receipt
            if prepared_source_episode_memory is not None
            else None
        ),
    )
    toolbox = PMReviewAgentToolbox(session=session)
    task_description = render_pm_review_task_prompt(
        pm_review_input,
        output_protocol=output_protocol,
    )
    validate_pm_review_prompt_size(
        pm_review_input=pm_review_input,
        task_description=task_description,
        max_prompt_chars=prompt_max_chars,
    )
    task = AgentTask(
        role="pm_review",
        task_id=_pm_review_task_id(request),
        task_description=task_description,
        required_tool_names=REQUIRED_PM_REVIEW_TOOL_NAMES,
    )

    def run_contract_attempt(task_description: str, attempt_count: int) -> AgentRunReceipt:
        attempt_task_id = task.task_id
        if attempt_count > 1:
            prompt_hash = sha256(task_description.encode("utf-8")).hexdigest()[:12]
            attempt_task_id = f"{task.task_id}:repair:{attempt_count - 1}:{prompt_hash}"
        try:
            return run_agent_runtime_once_sync(
                runtime=task_runner,
                task=AgentTask(
                    role=task.role,
                    task_id=attempt_task_id,
                    task_description=task_description,
                    required_tool_names=task.required_tool_names,
                ),
                tools=toolbox,
            )
        except AgentRuntimeContractError as exc:
            raise PMReviewAgentRuntimeError(str(exc)) from exc

    try:
        repair_result = run_pm_review_contract_repair(
            task_description=task.task_description,
            request=request,
            pm_review_input=pm_review_input,
            decision_available_at=decision_available_at,
            output_protocol=output_protocol,
            run_attempt=run_contract_attempt,
        )
    except PMReviewContractRepairExhausted as exc:
        raise PMReviewAgentRuntimeError(str(exc.failure)) from exc
    decision_draft = repair_result.decision_draft
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
        raise PMReviewStructuralRuntimeError(str(exc)) from exc
    except PMDecisionAdapterError as exc:
        raise PMReviewAgentRuntimeError(str(exc)) from exc
    try:
        position_review_triggers = build_pm_position_review_triggers(
            pm_review_input=pm_review_input,
            requested_state=pm_decision.requested_state,
        )
        if position_review_triggers is not None:
            PMPositionReviewTriggerStore(layout).append(position_review_triggers)
    except (PMPositionReviewError, PMReviewStoreError) as exc:
        raise PMReviewStructuralRuntimeError(str(exc)) from exc
    return PMReviewRunResult(
        pm_review_input=pm_review_input,
        decision_draft=decision_draft,
        pm_decision=pm_decision,
        position_review_triggers=position_review_triggers,
        agent_run_receipt=repair_result.accepted_attempt,
        read_receipts=_merge_read_receipts(
            (
                ()
                if prepared_source_episode_memory is None
                else (prepared_source_episode_memory.tool_read_receipt,)
            ),
            toolbox.read_receipts,
        ),
    )


def build_pm_review_input(
    *,
    request: PMReviewRequest,
    actual_exposure: ActiveExposure,
    assessment: AnalysisAssessment | None = None,
    evidence_records: tuple[EvidenceLedgerRecord, ...] = (),
    market_bars: tuple[MarketDataBar, ...] = (),
    pm_review_requests: tuple[PMReviewRequest, ...] = (),
    portfolio_risk_snapshot: PMPortfolioRiskSnapshot | None = None,
    candidate_anchor: CandidateReviewAnchor | None = None,
    source_episode_memory_view: PMEpisodeMemoryView | None = None,
    source_episode_memory_read_receipt: PMReviewEpisodeMemoryReadReceipt | None = None,
    execution_direction_mode: PMReviewExecutionDirectionMode = "long_short",
) -> PMReviewInput:
    """Assemble the repo-owned deterministic PM input surface."""

    if not isinstance(request, PMReviewRequest):
        raise PMReviewAgentRuntimeError("request must be a PMReviewRequest instance.")
    if not isinstance(actual_exposure, ActiveExposure):
        raise PMReviewAgentRuntimeError("actual_exposure must be an ActiveExposure instance.")
    if actual_exposure.target_key != request.target_key:
        raise PMReviewAgentRuntimeError("actual_exposure target_key must match request.")
    if assessment is not None:
        if not isinstance(assessment, AnalysisAssessment):
            raise PMReviewAgentRuntimeError(
                "assessment must be an AnalysisAssessment instance when supplied."
            )
        if assessment.target_key != request.target_key:
            raise PMReviewAgentRuntimeError("assessment target_key must match request.")
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
                None if assessment is None else _pm_analysis_snapshot_from_assessment(assessment)
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
        raise PMReviewStructuralRuntimeError(str(exc)) from exc


def _pm_analysis_snapshot_from_assessment(
    assessment: AnalysisAssessment,
) -> PMAnalysisSnapshot:
    snapshot = analysis_snapshot_from_assessment(assessment)
    return replace(
        snapshot,
        if_flat_implication_md=_pm_price_surface_guardrail_md(snapshot.if_flat_implication_md),
        if_already_long_implication_md=_pm_price_surface_guardrail_md(
            snapshot.if_already_long_implication_md
        ),
        if_already_short_implication_md=(
            None
            if snapshot.if_already_short_implication_md is None
            else _pm_price_surface_guardrail_md(snapshot.if_already_short_implication_md)
        ),
    )


def _pm_price_surface_guardrail_md(content_md: str) -> str:
    return f"{_PM_PRICE_SURFACE_NOTE}\n\n{content_md}"


def _preflight_pm_review_tools(available_tool_names: tuple[str, ...] | None) -> None:
    if available_tool_names is None:
        return
    available = set(available_tool_names)
    missing = tuple(
        tool_name for tool_name in REQUIRED_PM_REVIEW_TOOL_NAMES if tool_name not in available
    )
    if missing:
        raise PMReviewAgentRuntimeError(
            f"PMReview tool preflight failed; missing tool(s): {', '.join(missing)}."
        )
    forbidden = tuple(
        tool_name for tool_name in available if tool_name not in REQUIRED_PM_REVIEW_TOOL_NAMES
    )
    if forbidden:
        raise PMReviewAgentRuntimeError(
            "PMReview runner must expose only PMReview tools; forbidden tool(s): "
            f"{', '.join(sorted(forbidden))}."
        )


def _validate_layout(layout: WorkspaceLayout) -> None:
    if not isinstance(layout, WorkspaceLayout):
        raise PMReviewAgentRuntimeError("layout must be a WorkspaceLayout instance.")


def _pm_review_task_id(request: PMReviewRequest) -> str:
    return f"pm-review:{request.target_key}:{request.request_id}:{request.business_at.isoformat()}"


def _pm_review_session_id(request: PMReviewRequest) -> str:
    return f"pm-review-tool-session:{request.request_id}"


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
        failure_reason = None if receipt is None else receipt.failure_reason
        raise PMReviewStructuralRuntimeError(
            "required runtime-owned PM episode memory pre-read failed"
            + (f": {failure_reason}" if failure_reason else ".")
        ) from exc
    receipt = _load_episode_memory_read_receipt_for_request(
        layout=session.layout,
        request=request,
    )
    if receipt is None:
        raise PMReviewStructuralRuntimeError(
            "required runtime-owned PM episode memory pre-read receipt is missing."
        )
    if receipt.status not in {"read", "empty"}:
        raise PMReviewStructuralRuntimeError(
            "required runtime-owned PM episode memory pre-read did not finish successfully."
        )
    if len(result.records) != 1 or not isinstance(result.records[0], PMEpisodeMemoryView):
        raise PMReviewStructuralRuntimeError(
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
        raise PMReviewStructuralRuntimeError(
            "required runtime-owned PM episode memory pre-read is missing."
        )
    if prepared_source_episode_memory.detailed_receipt.status not in {"read", "empty"}:
        raise PMReviewStructuralRuntimeError(
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


__all__ = [
    "PMReviewRunner",
    "PMReviewTaskRunner",
    "build_pm_review_input",
    "build_pm_review_runner",
    "run_pm_review_request",
]
