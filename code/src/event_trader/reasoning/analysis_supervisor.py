"""Project-owned Analysis supervision, repair, commit, and persistence chain."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

from event_trader.analysis import AnalysisContext
from event_trader.analysis_assessment_store import AnalysisAssessmentStore
from event_trader.config import load_kernel_config
from event_trader.context_assembly import (
    CURRENT_MEMORY_READ_POLICY,
    ContextAssemblyError,
    ContextPacket,
    ContextPacketRuntimeScope,
    ContextPacketStoreError,
    FileBackedContextPacketStore,
    ResearchClaimUsageReceipt,
    VisibilityAudit,
    build_analysis_context_packet,
    hash_context_packet,
    serialize_context_packet,
    validate_context_packet_visibility,
)
from event_trader.contracts import (
    AnalysisAssessment,
    AnalysisResult,
    EvidenceLedgerRecord,
    LLMUsageReceipt,
    PageReadResult,
)
from event_trader.contracts.analysis_assessment_schema import (
    analysis_final_payload_requirements_markdown,
)
from event_trader.contracts.analysis_direction_policy import (
    AnalysisDirectionPolicyNormalizationError,
    normalize_analysis_assessment_for_execution_direction_mode,
)
from event_trader.contracts.evidence_review import (
    classify_evidence_review_dimensions,
    summarize_evidence_review_dimensions,
)
from event_trader.contracts.execution_direction_policy import (
    ExecutionDirectionMode,
    validate_execution_direction_mode,
)
from event_trader.contracts.pm_review_reason import (
    PMReviewReason,
    analysis_pm_annotation_only_reasons,
    analysis_pm_escalation_reasons,
)
from event_trader.decision_memory import (
    DecisionEpisodeRecord,
    DecisionEpisodeStoreError,
    FileBackedDecisionEpisodeStore,
)
from event_trader.integrations.analysis_citations import (
    AnalysisCitation,
    extract_analysis_citations,
    serialize_analysis_citations,
)
from event_trader.integrations.analysis_commit_audit import (
    AnalysisCommitProgress as _AnalysisCommitProgress,
)
from event_trader.integrations.analysis_commit_audit import (
    analysis_commit_journal_path as _analysis_commit_journal_path,
)
from event_trader.integrations.analysis_commit_audit import (
    analysis_commit_state_payload as _analysis_commit_state_payload,
)
from event_trader.integrations.analysis_structured_finalizer import (
    AnalysisStructuredOutputMode,
    normalize_analysis_structured_output_mode,
)
from event_trader.integrations.bounded_context import (
    DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
    bounded_text_payload,
)
from event_trader.integrations.markdown_context import (
    MarkdownContextError,
    build_markdown_page_map,
)
from event_trader.pm_review.contracts import (
    CandidateReviewAnchor,
    PMReviewContractError,
    PMReviewRequest,
)
from event_trader.pm_review.store import (
    CandidateReviewAnchorStore,
    PMReviewRequestStore,
    PMReviewStoreError,
)
from event_trader.portfolio.active_exposure import (
    ActiveExposureResolverError,
    resolve_active_exposure,
)
from event_trader.portfolio.store import PortfolioStateStore, PortfolioStoreError
from event_trader.reasoning.analysis_agent import (
    ANALYSIS_AGENT_CONTRACT,
    AnalysisAgentAttemptRequest,
    AnalysisAgentExecutor,
    AnalysisBusinessTask,
    execute_analysis_agent_attempt,
)
from event_trader.reasoning.analysis_commit_artifacts import (
    AnalysisCommitArtifactPersistenceError,
    AnalysisCommitArtifactPreflightError,
    AnalysisOutcomeArtifactValidationError,
    PersistedAnalysisCommitArtifacts,
    analysis_commit_requires_thesis_revision,
    persist_analysis_commit_artifacts,
    preflight_analysis_assessment_persistence,
    validate_persisted_analysis_outcome_dependencies,
)
from event_trader.reasoning.analysis_commit_log import (
    AnalysisCommitLogError,
    append_analysis_log_entry,
)
from event_trader.reasoning.analysis_contract_repair import (
    ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS as _ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
)
from event_trader.reasoning.analysis_contract_repair import (
    AnalysisContractFailure as _AnalysisContractFailure,
)
from event_trader.reasoning.analysis_contract_repair import (
    AnalysisContractRepairRequired as _AnalysisContractRepairRequired,
)
from event_trader.reasoning.analysis_contract_repair import (
    AnalysisWriteFailureBudgetExceeded as _AnalysisWriteFailureBudgetExceeded,
)
from event_trader.reasoning.analysis_memory_commit import (
    AnalysisMemoryCommitError,
    commit_analysis_memory_writes,
)
from event_trader.reasoning.analysis_output import (
    AnalysisOutputError,
    AnalysisOutputFinalizationConfig,
    derive_analysis_assessment_storage_id,
    finalize_analysis_output_payload,
    normalize_analysis_result,
    with_runtime_analysis_assessment_id,
)
from event_trader.reasoning.analysis_read_audit import (
    ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS,
    ANALYSIS_MEMORY_READ_OPERATIONS,
    analysis_read_audit,
    build_analysis_read_audit_requirements,
    enforce_analysis_read_audit,
)
from event_trader.reasoning.analysis_tools import AnalysisToolContext
from event_trader.reasoning.analysis_write_citations import (
    AnalysisWriteCitationError,
    canonicalize_analysis_write_citations,
    collect_analysis_context_citations,
    validate_analysis_write_citations,
)
from event_trader.reasoning.analysis_write_receipts import (
    ANALYSIS_READ_RECEIPT_OPERATIONS,
    AnalysisWriteReceiptContractError,
    admit_analysis_write_receipt,
    collapse_analysis_write_receipts,
    finalize_analysis_write_outcome,
    is_analysis_material_write_receipt,
    protected_rewrite_contract_repair_required,
    serialize_analysis_write_receipt_summary,
    validate_analysis_commit_receipts,
    validate_analysis_watchlist_maintenance,
)
from event_trader.reasoning.analysis_write_support import (
    AnalysisWriteSupportError,
    analysis_unit_formation_lane,
    collect_analysis_support_refs,
    validate_analysis_write_support,
)
from event_trader.research_memory.active_price_basis_alignment import (
    ActivePriceBasisAlignmentError,
    align_analysis_assessment_to_active_price_basis,
)
from event_trader.research_memory.analysis_commit import AnalysisCommitContractError
from event_trader.storage import (
    WorkspaceLayout,
    build_workspace_layout,
    initialize_workspace_layout,
)
from event_trader.thesis_revision.contracts import ThesisRevision

_ANALYSIS_OUTCOME_DIR_NAME = "analysis_outcomes"
_ANALYSIS_STAGING_DIR_NAME = "analysis_staging"
_PM_REVIEW_CANDIDATE_ANCHOR_TTL = timedelta(days=7)
_ANALYSIS_PM_ESCALATION_REASONS = frozenset(analysis_pm_escalation_reasons())
_ANALYSIS_PM_ANNOTATION_ONLY_REASONS = frozenset(analysis_pm_annotation_only_reasons())
_OPEN_POSITION_ACTIONABLE_ROLES = frozenset(
    {
        "add",
        "hold_boundary",
        "de_risk_or_take_profit",
        "exit",
        "invalidates",
    }
)
_ANALYSIS_CURRENT_EXPOSURE_ESCALATION_ROLES = frozenset(
    {
        "de_risk_or_take_profit",
        "exit",
        "invalidates",
    }
)


class AnalysisSupervisorRuntimeError(RuntimeError):
    """Raised when the project-owned Analysis control chain cannot complete."""

    context_packet_id: str | None
    context_packet_hash: str | None
    context_packet_path: str | None


@dataclass(frozen=True, slots=True)
class AnalysisSupervisorConfig:
    """Provider-neutral inputs required by the deterministic Analysis supervisor."""

    config_path: Path
    runtime_mode: Literal["live", "replay"]
    runtime_scope: ContextPacketRuntimeScope
    run_id: str
    memory_read_policy: str
    structured_output_mode: AnalysisStructuredOutputMode
    structured_output_probe: bool
    execution_direction_mode: ExecutionDirectionMode
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    market_data_store_root: Path | None
    agent_label: str
    market_data_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if self.runtime_mode not in {"live", "replay"}:
            raise AnalysisSupervisorRuntimeError("runtime_mode must be 'live' or 'replay'.")
        if self.runtime_scope not in {"live", "replay"}:
            raise AnalysisSupervisorRuntimeError("runtime_scope must be 'live' or 'replay'.")
        if self.runtime_scope == "live" and self.run_id:
            raise AnalysisSupervisorRuntimeError("live Analysis supervisor must not carry run_id.")
        if self.runtime_scope == "replay" and not self.run_id:
            raise AnalysisSupervisorRuntimeError("replay Analysis supervisor requires run_id.")
        if not self.memory_read_policy:
            raise AnalysisSupervisorRuntimeError("memory_read_policy must be non-blank.")
        object.__setattr__(
            self,
            "structured_output_mode",
            normalize_analysis_structured_output_mode(self.structured_output_mode),
        )
        object.__setattr__(
            self,
            "execution_direction_mode",
            validate_execution_direction_mode(
                self.execution_direction_mode,
                error_type=AnalysisSupervisorRuntimeError,
            ),
        )
        object.__setattr__(
            self,
            "config_path",
            self.config_path.expanduser().resolve(strict=False),
        )
        if self.market_data_store_root is not None:
            object.__setattr__(
                self,
                "market_data_store_root",
                self.market_data_store_root.expanduser().resolve(strict=False),
            )
        if self.market_data_snapshot_id is not None:
            object.__setattr__(self, "market_data_snapshot_id", self.market_data_snapshot_id.strip())
        if not self.agent_label.strip():
            raise AnalysisSupervisorRuntimeError("agent_label must be non-blank.")


@dataclass(frozen=True, slots=True)
class _LearningCardUsage:
    visible_learning_card_ids: tuple[str, ...] = ()
    included_learning_card_ids: tuple[str, ...] = ()
    used_learning_card_ids: tuple[str, ...] = ()
    excluded_future_count: int = 0
    excluded_legacy_count: int = 0
    parse_warning_count: int = 0


class _AnalysisCommitArtifactPersistenceError(AnalysisSupervisorRuntimeError):
    """Raised when append-only artifact persistence fails mid-commit."""

    def __init__(
        self,
        message: str,
        *,
        failure_stage: str,
        partial_artifacts: PersistedAnalysisCommitArtifacts,
    ) -> None:
        self.failure_stage = failure_stage
        self.partial_artifacts = partial_artifacts
        super().__init__(message)


async def run_analysis_supervisor(
    *,
    config: AnalysisSupervisorConfig,
    layout: WorkspaceLayout,
    context: AnalysisContext,
    agent_executor: AnalysisAgentExecutor,
) -> AnalysisResult:
    workspace_root = layout.root
    kernel_config = load_kernel_config(config.config_path)
    task_id = _build_task_id(context)
    stage_root = _analysis_stage_root(workspace_root=workspace_root, task_id=task_id)
    receipt_path = _analysis_tool_activity_path(stage_root=stage_root)
    business_at = context.decision_visibility.business_at
    included_lesson_ids: tuple[str, ...] = ()
    context_packet = build_analysis_context_packet(
        layout=layout,
        target_key=context.request.target_key,
        business_at=business_at,
        event_types=_active_label_values(context, prefix="event_type:"),
        source_kinds=_active_label_values(context, prefix="source_kind:"),
        evidence_records=context.evidence_records,
        target_pages=context.memory_context.target_pages,
        operator_context=context.operator_context,
        market_context=context.market_context,
        memory_read_policy=config.memory_read_policy,
        runtime_scope=config.runtime_scope,
        run_id=config.run_id,
        unit_formation_lane=context.unit_formation_lane,
    )
    context_visibility_audit = _enforce_analysis_context_packet_visibility(
        layout=layout,
        packet=context_packet,
        required_memory_read_policy=config.memory_read_policy,
        required_runtime_scope=config.runtime_scope,
        required_run_id=(config.run_id if config.runtime_scope == "replay" else None),
    )

    committed = False
    try:
        business_brief = _build_analysis_business_brief(
            context,
            context_packet=context_packet,
            runtime_mode=config.runtime_mode,
            execution_direction_mode=config.execution_direction_mode,
        )
        analysis_assessment_id = derive_analysis_assessment_storage_id(
            target_key=context.request.target_key,
            analysis_unit_id=(
                None
                if context.unit_formation_lane is None
                else context.unit_formation_lane.analysis_unit_id
            ),
            decision_episode_id=context.request.decision_episode_id,
            event_ids=tuple(context.request.event_ids),
        )
        previous_assessment = _latest_committed_assessment_before(
            layout=layout,
            target_key=context.request.target_key,
            business_at=business_at,
        )
        repair_failure: _AnalysisContractFailure | None = None
        repair_ownership = _analysis_repair_ownership(agent_executor)
        result: AnalysisResult | None = None
        write_receipts: tuple[dict[str, object], ...] = ()
        activity_receipts: tuple[dict[str, object], ...] = ()
        attempt_llm_usages: list[LLMUsageReceipt] = []
        for attempt_index in range(_ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS):
            _reset_analysis_artifacts(receipt_path=receipt_path, stage_root=stage_root)
            _prepare_analysis_staging_workspace(
                canonical_layout=layout,
                stage_root=stage_root,
            )
            tool_context = AnalysisToolContext(
                canonical_layout=layout,
                stage_layout=build_workspace_layout(stage_root),
                receipt_path=receipt_path,
                active_citations=_active_citations(context),
                context_citations=_context_citations(context),
                target_key=context.request.target_key,
                business_at=business_at,
                runtime_scope=config.runtime_scope,
                execution_direction_mode=config.execution_direction_mode,
                market_config=kernel_config,
                market_data_store_root=config.market_data_store_root,
                market_data_snapshot_id=config.market_data_snapshot_id,
                context_packet=context_packet,
                included_lesson_ids=included_lesson_ids,
                operator_context=context.operator_context,
            )
            try:
                run_result = await execute_analysis_agent_attempt(
                    executor=agent_executor,
                    request=AnalysisAgentAttemptRequest(
                        contract=ANALYSIS_AGENT_CONTRACT,
                        task_id=task_id,
                        attempt_index=attempt_index,
                        analysis_assessment_id=analysis_assessment_id,
                        previous_assessment=previous_assessment,
                        business_task=AnalysisBusinessTask(
                            business_brief=business_brief,
                            final_payload_contract=analysis_final_payload_requirements_markdown(
                                execution_direction_mode=config.execution_direction_mode
                            ),
                            repair_failure=(
                                repair_failure
                                if repair_ownership == "supervisor_attempt"
                                else None
                            ),
                        ),
                        requires_watchlist_maintenance=(
                            context.request.requires_watchlist_maintenance
                        ),
                        market_context=context.market_context,
                        tool_context=tool_context,
                        read_audit_requirements=build_analysis_read_audit_requirements(
                            context=context,
                            context_packet=context_packet,
                        ),
                    ),
                )
                attempt_llm_usages.append(run_result.llm_usage)
            except _AnalysisContractRepairRequired as exc:
                if repair_ownership != "supervisor_attempt":
                    raise AnalysisSupervisorRuntimeError(
                        "stage-local Analysis executor leaked a recoverable contract "
                        f"failure to the supervisor: {exc}"
                    ) from None
                if attempt_index + 1 >= _ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS:
                    raise AnalysisSupervisorRuntimeError(str(exc)) from None
                repair_failure = exc.failure
                continue
            except _AnalysisWriteFailureBudgetExceeded as exc:
                raise AnalysisSupervisorRuntimeError(str(exc)) from None

            try:
                payload = dict(run_result.payload)
                payload = finalize_analysis_output_payload(
                    payload,
                    attempt_index=attempt_index,
                    config=AnalysisOutputFinalizationConfig(
                        structured_output_mode=config.structured_output_mode,
                        structured_output_probe=config.structured_output_probe,
                        execution_direction_mode=config.execution_direction_mode,
                        provider=config.llm_provider,
                        model=config.llm_model_name,
                        api_key=config.llm_api_key,
                        base_url=config.llm_base_url,
                    ),
                    context=context,
                    included_lesson_ids=included_lesson_ids,
                )
                result = normalize_analysis_result(
                    payload,
                    context=context,
                    attempt_index=attempt_index,
                    included_lesson_ids=included_lesson_ids,
                    execution_direction_mode=config.execution_direction_mode,
                )
                try:
                    result = replace(
                        result,
                        analysis_assessment=align_analysis_assessment_to_active_price_basis(
                            result.analysis_assessment,
                            layout=layout,
                            validation_config=kernel_config.validation,
                            market_context_config=kernel_config.market_context,
                            market_context=context.market_context,
                        ),
                    )
                except ActivePriceBasisAlignmentError as exc:
                    raise AnalysisSupervisorRuntimeError(str(exc)) from exc
                if result.analysis_assessment is not None:
                    try:
                        result = replace(
                            result,
                            analysis_assessment=normalize_analysis_assessment_for_execution_direction_mode(
                                result.analysis_assessment,
                                execution_direction_mode=config.execution_direction_mode,
                                error_type=AnalysisDirectionPolicyNormalizationError,
                            ),
                        )
                    except AnalysisDirectionPolicyNormalizationError as exc:
                        raise AnalysisSupervisorRuntimeError(str(exc)) from exc
                result = with_runtime_analysis_assessment_id(result, context=context)
                write_receipts = _load_analysis_write_receipts(receipt_path)
                activity_receipts = write_receipts
                enforce_analysis_read_audit(
                    receipts=write_receipts,
                    context=context,
                    context_packet=context_packet,
                )
                try:
                    result, write_receipts = _validate_analysis_write_contract(
                        result=result,
                        context=context,
                        write_receipts=write_receipts,
                        context_packet=context_packet,
                        attempt_index=attempt_index,
                    )
                except _AnalysisWriteFailureBudgetExceeded as exc:
                    raise AnalysisSupervisorRuntimeError(str(exc)) from None
                break
            except _AnalysisContractRepairRequired as exc:
                if repair_ownership != "supervisor_attempt":
                    raise AnalysisSupervisorRuntimeError(
                        "stage-local Analysis executor leaked a recoverable contract "
                        f"failure to the final supervisor defense: {exc}"
                    ) from None
                if attempt_index + 1 >= _ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS:
                    raise AnalysisSupervisorRuntimeError(str(exc)) from None
                repair_failure = exc.failure
            except AnalysisOutputError as exc:
                raise AnalysisSupervisorRuntimeError(str(exc)) from exc
            except AnalysisSupervisorRuntimeError:
                raise
        if result is None:
            raise AnalysisSupervisorRuntimeError(
                "analysis read-audit retry loop ended without a result."
            )
        committed_at = datetime.now(UTC)
        required_memory_read_policy = (
            config.memory_read_policy
            if config.memory_read_policy != CURRENT_MEMORY_READ_POLICY
            else None
        )
        _commit_analysis_result(
            layout=layout,
            context=context,
            result=result,
            context_packet=context_packet,
            context_visibility_audit=context_visibility_audit,
            write_receipts=write_receipts,
            activity_receipts=activity_receipts,
            task_id=task_id,
            committed_at=committed_at,
            required_memory_read_policy=required_memory_read_policy,
            llm_usage=_combine_llm_usage(
                agent_role="analysis",
                provider=config.llm_provider,
                model=config.llm_model_name,
                usages=tuple(attempt_llm_usages),
            ),
        )
        committed = True
        return result
    finally:
        _remove_analysis_staging_workspace(stage_root, committed=committed)


def _analysis_repair_ownership(agent_executor: AnalysisAgentExecutor) -> str:
    ownership = getattr(agent_executor, "repair_ownership", None)
    if ownership not in {"supervisor_attempt", "stage_local"}:
        raise AnalysisSupervisorRuntimeError(
            "Analysis executor must declare repair_ownership as 'supervisor_attempt' "
            "or 'stage_local'."
        )
    return ownership


def _json_compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _shorten_error_text(value: str, *, max_length: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 3]}..."


def _build_task_id(context: AnalysisContext) -> str:
    timestamp = context.request.event_ids[0]
    return f"event-trader-analysis-{context.request.target_key}-{timestamp}"


def _active_label_values(context: AnalysisContext, *, prefix: str) -> tuple[str, ...]:
    values: set[str] = set()
    for record in context.evidence_records:
        for label in record.labels:
            if label.startswith(prefix):
                value = label.removeprefix(prefix).strip()
                if value:
                    values.add(value)
    return tuple(sorted(values))


def _view_state_discipline_prompt() -> str:
    return (
        "Exposure-blind assessment discipline:\n"
        "- Analysis does not know or infer actual current exposure.\n"
        "- Do not propose target weights, entries, exits, opens, closes, "
        "reverses, resizes, execution timing, or risk approval.\n"
        "- Produce market/thesis claims and an AnalysisAssessment only. The "
        "assessment may describe as-if-flat setup quality and how the same "
        "evidence would matter if already long or already short.\n"
        "- Material numeric levels belong in price_level_roles with stance-aware "
        "roles; they are not orders or direct portfolio instructions.\n"
        "- `pm_candidate_review_required` and "
        "`pm_current_exposure_review_required` are strong PM escalation signals, "
        "not the only runtime entry points. Runtime may also promote a non-low "
        "non-flat as-if-flat assessment into a flat candidate PM review, or "
        "promote a non-flat live position with actionable open-position level "
        "roles into a current-exposure PM review.\n"
        "- pm_review_reasons may record explanatory annotations, but they are "
        "not the trigger surface for downstream PM judgment.\n"
        "- Standalone annotation-only reasons `material_level_used`, "
        "`level_role_conflict`, and `watch_trigger_touched` must not by "
        "themselves summon PM.\n"
        "- Use PM escalation reasons only when downstream PM judgment is "
        "actually needed: `flat_candidate_setup`, `material_setup`, "
        "`current_exposure_pressure`, `invalidation_touched`, and "
        "`risk_reward_compression`.\n"
        "- PMReview, not Analysis, decides action proposals.\n\n"
    )


def _analysis_role_prompt() -> str:
    return (
        "Role:\n"
        "You are the event-driven equity research analyst for event-trader.\n"
        "Your job is to decide whether escalated evidence changes durable research "
        "memory and, when it does, update that memory directly with the provided "
        "tools.\n\n"
    )


def _analysis_operating_boundary_prompt() -> str:
    return (
        "Operating boundary:\n"
        "- You are not the portfolio manager.\n"
        "- You are not the execution engine.\n"
        "- Do not produce a memo, patch plan, or commentary-only answer.\n"
        "- Analysis is exposure-blind: form market and thesis judgment without "
        "assuming actual live position state.\n\n"
    )


def _fixed_target_page_write_contract_prompt(target_key: str) -> str:
    return (
        "Fixed target page write contract:\n"
        "- targets/<target>/watchlist.md skeleton: `# Watchlist`, "
        "`## Immediate Watch Items`, `## Questions To Resolve`, "
        "`## Triggers To Escalate`.\n"
        "- targets/<target>/timeline.md skeleton: `# Timeline`, "
        "`## Recent Developments`, `## Thesis Shifts`, `## Open Threads`.\n"
        "- targets/<target>/thesis.md skeleton: `# Thesis`, "
        "`## Market Setup Dashboard`, `## Why Now`, `## Key Evidence`, "
        "`## Invalidation`.\n"
        "- `thesis.md / Market Setup Dashboard`, `thesis.md / Invalidation`, "
        "`watchlist.md / Immediate Watch Items`, "
        "`watchlist.md / Triggers To Escalate`, and "
        "`risks.md / Failure Conditions` are projected deterministically from "
        "the committed AnalysisAssessment; keep analysis-owned narrative work "
        "in the other canonical sections.\n"
        "- targets/<target>/risks.md skeleton: `# Risks`, "
        "`## Primary Risks`, `## Contradictory Evidence`, "
        "`## Failure Conditions`.\n"
        "- The fixed skeleton is deterministic; existing unique nested headings are "
        "analysis-owned working notes and may be updated directly.\n"
        "- When requires_watchlist_maintenance is true and a durable update is "
        "needed, include one material write to "
        f"`targets/{target_key}/watchlist.md` in the same pass; "
        "the final contract enforces watchlist maintenance for durable updates.\n\n"
    )


def _build_analysis_business_brief(
    context: AnalysisContext,
    *,
    context_packet: ContextPacket | None = None,
    runtime_mode: str = "unknown",
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> str:
    analysis_workbench_context = _render_analysis_workbench(context_packet)
    learning_card_context = _render_selected_learning_cards(context_packet)
    claim_card_context = _render_selected_claim_cards(context_packet)
    has_unit_formation_lane = context.unit_formation_lane is not None or (
        context_packet is not None
        and context_packet.analysis_workbench is not None
        and context_packet.analysis_workbench.unit_formation_lane is not None
    )
    support_payload_guidance = (
        "- absence_based_support/maturity_based_support only for absence/maturity "
        "reasoning; omit for positive evidence. Shape: "
        '{"used": true, "reason_codes": ["no_news_followup"], '
        '"domains": ["news_web_search"], "support_refs": []}. '
        'Maturity: reason_codes ["market_bar_reaction_mature"], domains '
        '["market_bars"]. Do not use a rationale field; extra fields do not '
        "replace required fields.\n"
        if has_unit_formation_lane
        else ""
    )
    return (
        f"{_analysis_role_prompt()}"
        f"{_analysis_operating_boundary_prompt()}"
        "Minimum request:\n"
        f"- target_key: {json.dumps(context.request.target_key)}\n"
        f"- event_ids: {json.dumps(list(context.request.event_ids))}\n"
        f"- why_escalated: {json.dumps(context.request.why_escalated)}\n"
        f"- requires_watchlist_maintenance: "
        f"{json.dumps(context.request.requires_watchlist_maintenance)}\n\n"
        "Runtime frame:\n"
        "Analysis Workbench is the deterministic desk packet: bounded evidence, "
        "market facts, memory context, and hashes. Market Lane is factual "
        "context, not a recommendation. Analysis is exposure-blind: do not "
        "infer actual current position or target weight. Additional grounded "
        "context is required when lanes are insufficient, truncated, stale, "
        "contradictory, or inadequate for citations and durable writes.\n\n"
        f"{analysis_workbench_context}"
        "Runtime brief:\n"
        "Deterministic index of active evidence and target memory "
        "surfaces available to the selected Analysis implementation:\n"
        f"{_render_context_brief(context, runtime_mode=runtime_mode)}\n\n"
        f"{learning_card_context}"
        f"{claim_card_context}"
        "Grounding Coverage Rules:\n"
        "- Active evidence grounding is satisfied by compiled Workbench evidence "
        "excerpts or explicit read_evidence results.\n"
        "- Target memory grounding is satisfied by Memory Impact Lane cards with "
        "compiler read receipts or explicit target ResearchMemory reads.\n"
        "- Recap-sensitive market grounding is satisfied by compiled Market Lane "
        "facts or explicit bounded market reads; non-recap events do not require "
        "market grounding.\n\n"
        "Hard rules:\n"
        "- The runtime brief's active evidence and target memory entries are "
        "metadata only; the Analysis Workbench may include bounded compiled "
        "evidence excerpts that satisfy baseline active-evidence grounding.\n"
        "- Satisfy grounding coverage before final output.\n"
        "- Workbench Evidence Lane excerpts can satisfy active evidence grounding "
        "when a compiled excerpt is available for each active event_id.\n"
        "- Memory Impact Lane cards with compiler read receipts satisfy baseline "
        "target ResearchMemory grounding.\n"
        "- Obtain additional target ResearchMemory context when Memory Impact Lane "
        "cards are missing, empty, materially truncated, contradictory, or "
        "inadequate for exact quotation, contextual citation, or write support.\n"
        "- Obtain additional active evidence context when a compiled excerpt is "
        "missing, materially truncated, contradictory, ambiguous, or inadequate "
        "for exact quotation, claims, or writes.\n"
        "- Keep admitted evidence facts, market context facts, subjective "
        "interpretation, ResearchMemory implications, and PMReview reasons "
        "separate in your reasoning and writes.\n"
        "- If market_context_terminal is available, obtain additional bounded "
        "market context when decision-time facts matter, when Market Lane is "
        "insufficient, stale, contradictory, or when detailed market evidence is needed. Do "
        "not infer unavailable indicators or treat option/cross-asset context "
        "as mechanical trade signals.\n"
        "- Market Lane recap_reconciliation is low-commitment factual coverage; "
        "it is not a priced-in/no-update policy and does not mechanically "
        "recommend a direct position action.\n"
        "- Do not write raw market-context payloads into ResearchMemory; only write "
        "market facts that change durable cognition.\n"
        "- The active-section surfaces "
        "`thesis.md / Market Setup Dashboard`, `thesis.md / Invalidation`, "
        "`watchlist.md / Immediate Watch Items`, "
        "`watchlist.md / Triggers To Escalate`, and "
        "`risks.md / Failure Conditions` are commit-time deterministic "
        "projections from structured assessment truth, not analysis-authored "
        "narrative targets.\n"
        "- Do not treat missing body text in the brief as evidence of absence; "
        "the implementation must obtain the necessary source or wiki text.\n"
        "- Research claim cards are ClaimRegistry projections, not admitted "
        "evidence facts; verify before writing.\n"
        "- Grounding coverage audit accepts compiled Workbench evidence excerpts "
        "or explicit evidence reads for active evidence, and accepts compiler-"
        "receipted Memory Impact Lane cards or explicit target ResearchMemory "
        "reads for memory grounding. For recap-sensitive events, it accepts "
        "compiled Market Lane facts or explicit bounded market reads for market "
        "grounding.\n"
        "- The final JSON event_ids field must contain only the active request "
        "event_ids in the same order. Do not add contextual event_ids you read "
        "from evidence or research memory.\n"
        "- If no durable cognition changed, do not stage any ResearchMemory write.\n"
        "- If durable cognition changed, stage only the necessary ResearchMemory writes.\n"
        "- Use `research-claim` blocks for material thesis/risk/invalidation "
        "writes; omissions are receipted as claim_coverage_status=missing.\n"
        "- If requires_watchlist_maintenance is true and you update durable "
        "cognition, maintain "
        f"`targets/{context.request.target_key}/watchlist.md` with concrete next "
        "attention triggers, invalidation checks, or monitoring conditions.\n"
        "- Use readable evidence citations only in the exact form "
        '"`event_id` | `source_ref`". Bare event_ids or parenthesized '
        "event references are invalid mechanical format errors.\n"
        "- Material wiki writes must cite at least one active request evidence "
        "pair. They may also cite contextual evidence pairs, but only when those "
        "pairs came from evidence or research-memory content you loaded this pass.\n"
        "- Prefer a section-scoped revision when a section rewrite is enough; its "
        "content must not duplicate the page title, section heading, or required "
        "top-level headings.\n"
        f"{support_payload_guidance}"
        "- On fixed target pages, section revisions may target canonical or unique "
        "nested page-map headings.\n"
        "- Use a full-page revision only when current structure is materially wrong "
        "or incoherent; thesis.md, risks.md, watchlist.md, and timeline.md must then "
        "contain the complete fixed skeleton in canonical order.\n"
        "- Create a subject page only when it has become a durable research object.\n"
        "- Do not update index.md from analysis. index.md is deterministic "
        "navigation, not durable research content.\n"
        "- Do not write log.md from analysis; runtime receipts already capture "
        "execution traces and no analysis log write surface exists.\n\n"
        f"{_fixed_target_page_write_contract_prompt(context.request.target_key)}"
        "Source-classification rule:\n"
        "- Evidence labels include exactly one event_type:* label and exactly one "
        "source_kind:* label.\n"
        "- event_type and source_kind describe the material objectively; they are "
        "not importance ratings.\n"
        "- event_type and source_kind do not mechanically imply PM action. "
        "Judge the whole active evidence set, ResearchMemory, Market Lane, and "
        "additional grounded context together.\n\n"
        f"{_view_state_discipline_prompt()}"
        "Analysis-origin PMReviewRequest materialization treats "
        "AnalysisAssessment.pm_candidate_review_required and "
        "AnalysisAssessment.pm_current_exposure_review_required as explicit strong "
        "PM escalation signals. Runtime may also promote a non-low non-flat "
        "as-if-flat assessment into flat candidate PM review, or a non-flat live "
        "position matching actionable role_if_already_long/short levels into "
        "current-exposure PM review; pm_review_reasons may still be recorded for "
        "explanation.\n\n"
        "Never return view_state_change, decision_audit, target_weight, "
        "requested_state, requested_target_weight, or action fields. PMReview "
        "is the first exposure-aware action surface.\n"
    )


def _render_analysis_workbench(context_packet: ContextPacket | None) -> str:
    if context_packet is None or context_packet.analysis_workbench is None:
        return ""
    payload = {
        "debug_identity": {
            "enclosing_context_packet_id": context_packet.packet_id,
            "enclosing_context_packet_hash": context_packet.packet_hash,
            "runtime_scope": context_packet.runtime_scope,
            "run_id": context_packet.run_id or None,
            "identity_source": "enclosing_context_packet",
        },
        "workbench": context_packet.analysis_workbench.to_json_payload(),
        "slice_a_grounding_note": (
            "Workbench evidence excerpts can satisfy active evidence grounding. "
            "Memory Impact Lane cards with compiler read receipts can satisfy "
            "baseline target-memory grounding. Market Lane is low-commitment "
            "factual context for recap-sensitive market grounding, not a "
            "view-state recommendation."
        ),
        "operator_context_note": (
            "Operator Context Lane is human-authored operator context. It may "
            "shape interpretation, but it is not admitted evidence truth, not a "
            "trade instruction, and does not bypass evidence, memory, market "
            "context, validation, risk, or execution. If it is missing or empty, "
            "continue the ordinary workflow. Obtain the relevant operator.md "
            "section only when a bounded excerpt is insufficient."
        ),
        "market_setup_note": (
            "Market Setup Dashboard is the canonical analysis-owned setup "
            "memory. The market_setup_lane identifies this exposure-blind "
            "surface and must not be treated as actual portfolio exposure."
        ),
    }
    return f"Analysis Workbench:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"


def _render_selected_claim_cards(context_packet: ContextPacket | None) -> str:
    if context_packet is None or not context_packet.claim_cards:
        return ""
    payload = {
        "usage_receipts": [
            receipt.to_json_payload()
            for receipt in context_packet.receipts
            if isinstance(receipt, ResearchClaimUsageReceipt)
        ],
        "selected_cards": [
            {
                "claim_id": card.claim_id,
                "status": card.status,
                "claim_text": card.claim_text,
                "page_path": card.page_path,
                "section_name": card.section_name,
                "supporting_event_ids": list(card.supporting_event_ids),
                "contradicting_event_ids": list(card.contradicting_event_ids),
                "visible_from": card.visible_from.isoformat(),
                "updated_at": card.updated_at.isoformat(),
                "content_hash": card.content_hash,
            }
            for card in context_packet.claim_cards
        ],
    }
    return (
        "Research Claim Cards:\n"
        "These selected cards are compact ClaimRegistry projections. They are "
        "not full ResearchMemory page bodies and do not replace admitted evidence "
        "or explicit wiki reads.\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
    )


def _render_context_brief(
    context: AnalysisContext,
    *,
    runtime_mode: str = "unknown",
) -> str:
    expected_page_path = f"targets/{context.request.target_key}/index.md"
    target_index_pages = [
        page for page in context.memory_context.target_pages if page.page_path == expected_page_path
    ]
    if len(target_index_pages) != 1:
        raise AnalysisSupervisorRuntimeError(
            "analysis runtime brief must include exactly one target index.md page."
        )
    if context.memory_context.shared_pages:
        raise AnalysisSupervisorRuntimeError(
            "analysis runtime brief must not include shared wiki pages."
        )
    payload = {
        "active_evidence": [_serialize_record_brief(record) for record in context.evidence_records],
        "market_context_terminal": _serialize_market_context_terminal_header(
            context,
            runtime_mode=runtime_mode,
        ),
        "target_index_page": _serialize_page_brief(target_index_pages[0]),
        "target_context_pages": [
            _serialize_page_inventory(page)
            for page in context.memory_context.target_pages
            if page.page_path != expected_page_path
        ],
        "additional_context_policy": {
            "evidence": "Required for missing, truncated, ambiguous, or conflicting excerpts.",
            "target_memory": "Required when bounded memory context is insufficient.",
            "market_context": "Required when visible decision-time market facts are insufficient.",
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _serialize_market_context_terminal_header(
    context: AnalysisContext,
    *,
    runtime_mode: str,
) -> dict[str, object]:
    business_at = _context_business_at(context)
    base: dict[str, object] = {
        "target_key": context.request.target_key,
        "business_at": business_at.isoformat(),
        "runtime_mode": runtime_mode,
        "available_tools": [
            "read_market_overview",
            "read_price_volume_window",
            "read_technical_panel",
            "read_option_activity",
            "read_cross_asset_context",
            "read_cross_asset_window",
        ],
        "constraints": {
            "as_of_visibility_enforced": True,
            "bounded_results": True,
            "raw_dump_allowed": False,
            "replay_without_prefetched_store_returns_unavailable": True,
        },
        "usage_rule": ("Use market tools for market facts; do not copy raw payloads."),
    }
    if context.market_context is None:
        return {
            **base,
            "available": False,
            "tradable_proxy_symbol": None,
            "bar_granularity": None,
            "enabled_components": [],
            "market_context_hash": None,
        }
    return {
        **base,
        "available": True,
        "tradable_proxy_symbol": context.market_context.tradable_proxy_symbol,
        "bar_granularity": context.market_context.bar_granularity,
        "enabled_components": list(context.market_context.components),
        "market_context_hash": context.market_context.payload_hash(),
        "component_status": dict(sorted(context.market_context.availability.items())),
    }


def _context_business_at(context: AnalysisContext) -> datetime:
    return context.decision_visibility.business_at


def _serialize_record_brief(record: EvidenceLedgerRecord) -> dict[str, object]:
    return {
        "event_id": record.event_id,
        "target_key": record.target_key,
        "source_ref": record.source_ref,
        "title": record.title,
        "content_sha256": sha256(record.content.encode("utf-8")).hexdigest(),
        "content_char_count": len(record.content),
        "content_available_via": "read_evidence",
        "labels": list(record.labels),
        "ts_source": record.ts_source.isoformat(),
        "ts_event": record.ts_event.isoformat(),
        "ts_init": record.ts_init.isoformat(),
    }


def _serialize_page_brief(page: PageReadResult) -> dict[str, object]:
    return {
        "page_path": page.page_path,
        "content_sha256": sha256(page.content_md.encode("utf-8")).hexdigest(),
        "content_char_count": len(page.content_md),
        "content_available_via": "read_page/read_section/read_around_citation",
        "page_map": _serialize_page_map(page.content_md),
    }


def _serialize_page_inventory(page: PageReadResult) -> dict[str, object]:
    return {
        "page_path": page.page_path,
        "content_sha256": sha256(page.content_md.encode("utf-8")).hexdigest(),
        "content_char_count": len(page.content_md),
        "content_available_via": "read_page/read_section/read_around_citation",
    }


def _serialize_page_map(content_md: str) -> list[dict[str, object]]:
    try:
        sections = build_markdown_page_map(
            content_md,
            excerpt_char_limit=DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
        )
    except MarkdownContextError as exc:
        raise AnalysisSupervisorRuntimeError(
            f"Failed to build analysis brief page map: {exc}"
        ) from exc
    return [
        {
            "heading": section["heading"],
            "level": section["level"],
            "content_char_count": section["content_char_count"],
            "content_truncated": section["content_truncated"],
        }
        for section in sections
    ]


def _serialize_active_citations(context: AnalysisContext) -> str:
    return serialize_analysis_citations(_active_citations(context))


def _active_citations(context: AnalysisContext) -> tuple[AnalysisCitation, ...]:
    return tuple(
        AnalysisCitation(event_id=record.event_id, source_ref=record.source_ref)
        for record in context.evidence_records
    )


def _latest_committed_assessment_before(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    business_at: datetime,
) -> AnalysisAssessment | None:
    """Return the latest committed target Assessment visible at this event boundary."""

    candidates = tuple(
        persisted.record
        for persisted in AnalysisAssessmentStore(layout).read_records(target_key=target_key)
        if persisted.record.business_at < business_at
    )
    if not candidates:
        return None
    return max(candidates, key=lambda assessment: assessment.business_at)


def _serialize_context_citations(context: AnalysisContext) -> str:
    payload = [
        {"event_id": citation.event_id, "source_ref": citation.source_ref}
        for citation in _context_citations(context)
    ]
    return json.dumps(payload, ensure_ascii=False)


def _context_citations(context: AnalysisContext) -> tuple[AnalysisCitation, ...]:
    citations: list[AnalysisCitation] = []
    seen: set[str] = set()
    active_event_ids = {record.event_id for record in context.evidence_records}
    for page in (
        *context.memory_context.target_pages,
        *context.memory_context.shared_pages,
    ):
        visible_content = bounded_text_payload(
            page.content_md,
            limit=DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
        )["excerpt"]
        if not isinstance(visible_content, str):
            raise AnalysisSupervisorRuntimeError("bounded analysis page excerpt must be a string.")
        for citation in extract_analysis_citations(visible_content):
            event_id = citation.event_id
            if event_id in active_event_ids or event_id in seen:
                continue
            seen.add(event_id)
            citations.append(citation)
    return tuple(citations)


def _render_selected_learning_cards(context_packet: ContextPacket | None) -> str:
    if context_packet is None or not context_packet.learning_cards:
        return ""
    payload = {
        "usage_receipts": [
            receipt
            for receipt in context_packet.receipts
            if isinstance(receipt, dict) and receipt.get("receipt_type") == "learning_card_usage"
        ],
        "selected_cards": [
            {
                "card_id": card.card_id,
                "scope_key": card.scope_key,
                "consumer_role": card.consumer_role,
                "title": card.title,
                "summary_md": card.summary_md,
                "body_md": card.body_md,
                "use_when": list(card.use_when),
                "avoid_when": list(card.avoid_when),
                "source_delta_ids": list(card.source_delta_ids),
                "source_delta_hashes": list(card.source_delta_hashes),
                "source_review_paths": list(card.source_review_paths),
                "source_episode_ids": list(card.source_episode_ids),
                "target_key": card.target_key,
                "confidence": card.confidence,
                "usable_from": card.usable_from.isoformat(),
                "content_hash": card.content_hash,
            }
            for card in context_packet.learning_cards
        ],
    }
    return (
        "Learning Cards:\n"
        "These selected structured cards are role-filtered analysis learning. "
        "They are not admitted evidence, not PM exposure context, and not trade "
        "action commands. Use them as weak method guidance only.\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
    )


def _analysis_stage_root(*, workspace_root: Path, task_id: str) -> Path:
    return (workspace_root / "runtime" / _ANALYSIS_STAGING_DIR_NAME / task_id).resolve(strict=False)


def _analysis_receipt_path(*, workspace_root: Path, task_id: str) -> Path:
    return _analysis_tool_activity_path(
        stage_root=_analysis_stage_root(workspace_root=workspace_root, task_id=task_id)
    )


def _analysis_tool_activity_path(*, stage_root: Path) -> Path:
    return (stage_root / "tool_activity.jsonl").resolve(strict=False)


def _analysis_outcome_path(
    *,
    workspace_root: Path,
    target_key: str,
    business_at: datetime,
) -> Path:
    outcome_dir = (workspace_root / "runtime" / _ANALYSIS_OUTCOME_DIR_NAME / target_key).resolve(
        strict=False
    )
    outcome_dir.mkdir(parents=True, exist_ok=True)
    return (outcome_dir / f"{business_at.strftime('%Y-%m')}.jsonl").resolve(strict=False)


def _reset_analysis_artifacts(*, receipt_path: Path, stage_root: Path) -> None:
    try:
        if receipt_path.exists():
            receipt_path.unlink()
        if stage_root.exists():
            shutil.rmtree(stage_root)
    except OSError as exc:
        raise AnalysisSupervisorRuntimeError("Failed to reset analysis staging artifacts.") from exc


def _prepare_analysis_staging_workspace(
    *,
    canonical_layout: WorkspaceLayout,
    stage_root: Path,
) -> WorkspaceLayout:
    initialize_workspace_layout(stage_root)
    stage_layout = build_workspace_layout(stage_root)
    try:
        shutil.rmtree(stage_layout.research_memory_root)
        shutil.copytree(
            canonical_layout.research_memory_root,
            stage_layout.research_memory_root,
        )
        for operator_page in stage_layout.research_memory_root.glob(
            "targets/*/operator.md"
        ):
            operator_page.unlink()
    except OSError as exc:
        raise AnalysisSupervisorRuntimeError(
            "Failed to prepare analysis staging research memory."
        ) from exc
    return stage_layout


def _remove_analysis_staging_workspace(stage_root: Path, *, committed: bool) -> None:
    try:
        if stage_root.exists():
            shutil.rmtree(stage_root)
    except OSError as exc:
        if committed:
            return
        raise AnalysisSupervisorRuntimeError(
            "Failed to remove analysis staging workspace."
        ) from exc


def _load_analysis_write_receipts(receipt_path: Path) -> tuple[dict[str, object], ...]:
    if not receipt_path.exists():
        return ()
    if not receipt_path.is_file():
        raise AnalysisSupervisorRuntimeError(
            f"Analysis receipt path must be a file: {receipt_path}"
        )
    receipts: list[dict[str, object]] = []
    with receipt_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            normalized = line.strip()
            if not normalized:
                continue
            try:
                payload = json.loads(normalized)
            except json.JSONDecodeError as exc:
                raise AnalysisSupervisorRuntimeError(
                    "Analysis write receipt file contains invalid JSON."
                ) from exc
            if not isinstance(payload, dict):
                raise AnalysisSupervisorRuntimeError(
                    "Analysis write receipt entries must be JSON objects."
                )
            receipts.append(payload)
    return tuple(receipts)


def _enforce_analysis_context_packet_visibility(
    layout: WorkspaceLayout,
    packet: ContextPacket,
    *,
    required_memory_read_policy: str | None,
    required_runtime_scope: ContextPacketRuntimeScope,
    required_run_id: str | None,
    precomputed_visibility_audit: VisibilityAudit | None = None,
) -> VisibilityAudit:
    audit = precomputed_visibility_audit or validate_context_packet_visibility(
        packet,
        required_memory_read_policy=required_memory_read_policy,
        required_runtime_scope=required_runtime_scope,
        required_run_id=required_run_id,
    )
    future_visible_violations = tuple(
        violation
        for violation in audit.violations
        if violation.get("violation_type") == "future_visible_context"
    )
    if future_visible_violations:
        context_packet_path = _append_analysis_context_packet(
            layout=layout,
            context_packet=packet,
            required_memory_read_policy=required_memory_read_policy,
            precomputed_visibility_audit=audit,
        )
        error = AnalysisSupervisorRuntimeError(
            "analysis context packet contains future_visible_context: "
            f"{_json_compact(future_visible_violations)}"
        )
        error.context_packet_id = packet.packet_id
        error.context_packet_hash = packet.packet_hash
        error.context_packet_path = str(context_packet_path)
        raise error
    return audit


def _preflight_analysis_context_packet_commit(
    *,
    layout: WorkspaceLayout,
    context_packet: ContextPacket,
    required_memory_read_policy: str | None,
    precomputed_visibility_audit: VisibilityAudit | None = None,
) -> None:
    """Fail before canonical memory writes if context packet persistence is unsafe."""
    if context_packet.packet_hash != hash_context_packet(context_packet):
        raise AnalysisSupervisorRuntimeError(
            "analysis context packet hash does not match payload before commit."
        )
    _enforce_analysis_context_packet_visibility(
        layout=layout,
        packet=context_packet,
        required_memory_read_policy=required_memory_read_policy,
        required_runtime_scope=context_packet.runtime_scope,
        required_run_id=(
            context_packet.run_id if context_packet.runtime_scope == "replay" else None
        ),
        precomputed_visibility_audit=precomputed_visibility_audit,
    )
    try:
        FileBackedContextPacketStore(
            layout,
            runtime_scope=context_packet.runtime_scope,
            run_id=context_packet.run_id,
        ).read_packets(
            stage=context_packet.stage,
            target_key=context_packet.target_key,
            year_month=context_packet.business_at.strftime("%Y-%m"),
        )
    except (ContextAssemblyError, ContextPacketStoreError) as exc:
        raise AnalysisSupervisorRuntimeError(
            "analysis context packet store is not readable before commit."
        ) from exc


def _append_analysis_context_packet(
    *,
    layout: WorkspaceLayout,
    context_packet: ContextPacket,
    required_memory_read_policy: str | None,
    precomputed_visibility_audit: VisibilityAudit | None = None,
) -> Path:
    try:
        return FileBackedContextPacketStore(
            layout,
            runtime_scope=context_packet.runtime_scope,
            run_id=context_packet.run_id,
        ).append_packet(
            context_packet,
            required_memory_read_policy=required_memory_read_policy,
            precomputed_visibility_audit=precomputed_visibility_audit,
        )
    except ContextPacketStoreError as exc:
        raise AnalysisSupervisorRuntimeError(
            "Failed to append analysis context packet before memory writes."
        ) from exc


def _append_analysis_commit_journal(
    *,
    layout: WorkspaceLayout,
    context: AnalysisContext,
    result: AnalysisResult,
    context_packet: ContextPacket,
    task_id: str,
    committed_at: datetime,
    record_type: str,
    payload: dict[str, object] | None = None,
) -> None:
    business_at = context.decision_visibility.business_at
    journal_path = _analysis_commit_journal_path(
        layout=layout,
        target_key=result.target_key,
        business_at=business_at,
    )
    record = {
        "record_type": record_type,
        "record_id": (f"analysis-commit:{task_id}:{record_type}:{committed_at.isoformat()}"),
        "task_id": task_id,
        "target_key": result.target_key,
        "event_ids": list(result.event_ids),
        "business_at": business_at.isoformat(),
        "recorded_at": datetime.now(UTC).isoformat(),
        "committed_at": committed_at.isoformat(),
        "outcome": result.outcome,
        "context_packet_id": context_packet.packet_id,
        "context_packet_hash": context_packet.packet_hash,
        "decision_episode_id": context.request.decision_episode_id,
        "payload": dict(payload or {}),
    }
    try:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with journal_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False))
            handle.write("\n")
    except OSError as exc:
        raise AnalysisSupervisorRuntimeError(
            "Failed to append analysis commit journal record."
        ) from exc


def _commit_analysis_result(
    *,
    layout: WorkspaceLayout,
    context: AnalysisContext,
    result: AnalysisResult,
    context_packet: ContextPacket,
    context_visibility_audit: VisibilityAudit | None = None,
    write_receipts: tuple[dict[str, object], ...],
    activity_receipts: tuple[dict[str, object], ...],
    task_id: str,
    committed_at: datetime,
    required_memory_read_policy: str | None,
    llm_usage: LLMUsageReceipt | None,
) -> None:
    business_at = context.decision_visibility.business_at
    progress = _AnalysisCommitProgress(
        analysis_assessment_id=(
            None if result.analysis_assessment is None else result.analysis_assessment.assessment_id
        )
    )
    failure_stage = "preflight"
    commit_started = False
    try:
        _preflight_analysis_context_packet_commit(
            layout=layout,
            context_packet=context_packet,
            required_memory_read_policy=required_memory_read_policy,
            precomputed_visibility_audit=context_visibility_audit,
        )
        try:
            preflight_analysis_assessment_persistence(
                layout=layout,
                assessment=result.analysis_assessment,
            )
        except AnalysisCommitArtifactPreflightError as exc:
            raise AnalysisSupervisorRuntimeError(str(exc)) from exc
        _append_analysis_commit_journal(
            layout=layout,
            context=context,
            result=result,
            context_packet=context_packet,
            task_id=task_id,
            committed_at=committed_at,
            record_type="commit_started",
            payload=_analysis_commit_state_payload(progress=progress, status="incomplete"),
        )
        commit_started = True
        failure_stage = "context_packet_persist"
        context_packet_path = _append_analysis_context_packet(
            layout=layout,
            context_packet=context_packet,
            required_memory_read_policy=required_memory_read_policy,
        )
        progress = replace(progress, context_packet_path=context_packet_path)
        _append_analysis_commit_journal(
            layout=layout,
            context=context,
            result=result,
            context_packet=context_packet,
            task_id=task_id,
            committed_at=committed_at,
            record_type="context_packet_committed",
            payload=_analysis_commit_state_payload(progress=progress, status="incomplete"),
        )
        failure_stage = "memory_writes_commit"
        try:
            committed_memory_writes = commit_analysis_memory_writes(
                layout=layout,
                write_receipts=write_receipts,
                analysis_assessment=result.analysis_assessment,
                target_key=context_packet.target_key,
                actor_id=task_id,
                business_at=business_at,
                committed_at=committed_at,
                event_ids=tuple(context.request.event_ids),
                context_packet_id=context_packet.packet_id,
                context_packet_hash=context_packet.packet_hash,
            )
        except (
            AnalysisCommitContractError,
            AnalysisMemoryCommitError,
            AnalysisWriteReceiptContractError,
        ) as exc:
            raise AnalysisSupervisorRuntimeError(str(exc)) from exc
        progress = replace(
            progress,
            research_memory_writes_committed=True,
            research_memory_write_receipt_ids=committed_memory_writes.receipt_ids,
            thesis_revision_required=analysis_commit_requires_thesis_revision(
                committed_memory_writes
            ),
        )
        _append_analysis_commit_journal(
            layout=layout,
            context=context,
            result=result,
            context_packet=context_packet,
            task_id=task_id,
            committed_at=committed_at,
            record_type="memory_writes_committed",
            payload=_analysis_commit_state_payload(progress=progress, status="incomplete"),
        )
        failure_stage = "analysis_artifact_persist"
        try:
            persisted_artifacts = persist_analysis_commit_artifacts(
                layout=layout,
                target_key=context.request.target_key,
                business_at=business_at,
                committed_at=committed_at,
                analysis_assessment=result.analysis_assessment,
                context_packet_id=context_packet.packet_id,
                context_packet_hash=context_packet.packet_hash,
                source_event_ids=tuple(context.request.event_ids),
                committed_memory_writes=committed_memory_writes,
            )
        except AnalysisCommitArtifactPersistenceError as exc:
            progress = _progress_with_persisted_artifacts(
                progress=progress,
                artifacts=exc.partial_artifacts,
            )
            failure_stage = exc.failure_stage
            raise _AnalysisCommitArtifactPersistenceError(
                str(exc),
                failure_stage=exc.failure_stage,
                partial_artifacts=exc.partial_artifacts,
            ) from exc
        progress = _progress_with_persisted_artifacts(
            progress=progress,
            artifacts=persisted_artifacts,
        )
        if persisted_artifacts.analysis_assessment_path is not None:
            _append_analysis_commit_journal(
                layout=layout,
                context=context,
                result=result,
                context_packet=context_packet,
                task_id=task_id,
                committed_at=committed_at,
                record_type="analysis_assessment_committed",
                payload=_analysis_commit_state_payload(
                    progress=progress,
                    status="incomplete",
                ),
            )
        if (
            persisted_artifacts.thesis_revision is not None
            and persisted_artifacts.thesis_revision_path is not None
        ):
            _append_analysis_commit_journal(
                layout=layout,
                context=context,
                result=result,
                context_packet=context_packet,
                task_id=task_id,
                committed_at=committed_at,
                record_type="thesis_revision_committed",
                payload=_analysis_commit_state_payload(
                    progress=progress,
                    status="incomplete",
                ),
            )
        failure_stage = "outcome_append"
        outcome_path, outcome_record_id = _append_analysis_outcome_receipt(
            layout=layout,
            context=context,
            result=result,
            write_receipts=activity_receipts,
            research_memory_write_receipt_ids=committed_memory_writes.receipt_ids,
            task_id=task_id,
            committed_at=committed_at,
            learning_card_usage=_LearningCardUsage(
                used_learning_card_ids=result.used_lesson_ids,
            ),
            context_packet=context_packet,
            required_memory_read_policy=required_memory_read_policy,
            llm_usage=llm_usage,
            analysis_assessment_path=persisted_artifacts.analysis_assessment_path,
            thesis_revision=persisted_artifacts.thesis_revision,
            thesis_revision_path=persisted_artifacts.thesis_revision_path,
        )
        progress = replace(
            progress,
            analysis_outcome_record_id=outcome_record_id,
            analysis_outcome_path=outcome_path,
        )
        _append_analysis_commit_journal(
            layout=layout,
            context=context,
            result=result,
            context_packet=context_packet,
            task_id=task_id,
            committed_at=committed_at,
            record_type="outcome_committed",
            payload=_analysis_commit_state_payload(progress=progress, status="incomplete"),
        )
        failure_stage = "analysis_log_append"
        try:
            append_analysis_log_entry(
                layout=layout,
                result=result,
                write_receipts=write_receipts,
                task_id=task_id,
                business_at=business_at,
                committed_at=committed_at,
                why_escalated=context.request.why_escalated,
            )
        except AnalysisCommitLogError as exc:
            raise AnalysisSupervisorRuntimeError(str(exc)) from exc
        _append_analysis_commit_journal(
            layout=layout,
            context=context,
            result=result,
            context_packet=context_packet,
            task_id=task_id,
            committed_at=committed_at,
            record_type="commit_completed",
            payload=_analysis_commit_state_payload(progress=progress, status="complete"),
        )
    except Exception as exc:
        if commit_started:
            _append_analysis_commit_journal(
                layout=layout,
                context=context,
                result=result,
                context_packet=context_packet,
                task_id=task_id,
                committed_at=committed_at,
                record_type="commit_failed",
                payload={
                    **_analysis_commit_state_payload(progress=progress, status="failed"),
                    "failure_stage": failure_stage,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
        raise


def _progress_with_persisted_artifacts(
    *,
    progress: _AnalysisCommitProgress,
    artifacts: PersistedAnalysisCommitArtifacts,
) -> _AnalysisCommitProgress:
    return replace(
        progress,
        thesis_revision_required=artifacts.thesis_revision_required,
        analysis_assessment_id=artifacts.analysis_assessment_id,
        analysis_assessment_path=artifacts.analysis_assessment_path,
        thesis_revision_id=(
            None if artifacts.thesis_revision is None else artifacts.thesis_revision.revision_id
        ),
        thesis_revision_path=artifacts.thesis_revision_path,
    )


def _combine_llm_usage(
    *,
    agent_role: Literal["analysis", "reflection"],
    provider: str,
    model: str,
    usages: tuple[LLMUsageReceipt, ...],
) -> LLMUsageReceipt:
    provider_reported = tuple(
        usage for usage in usages if usage.usage_source == "provider_reported"
    )
    if not provider_reported:
        return LLMUsageReceipt.unavailable(
            agent_role=agent_role,
            provider=provider,
            model=model,
        )
    input_tokens = sum(usage.input_tokens for usage in provider_reported)
    output_tokens = sum(usage.output_tokens for usage in provider_reported)
    return LLMUsageReceipt(
        agent_role=agent_role,
        usage_source="provider_reported",
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


@dataclass(frozen=True, slots=True)
class _AnalysisPMReviewRouting:
    current_exposure_required: bool
    candidate_review_allowed: bool
    active_state: str | None = None


def _materialize_pm_review_request_from_assessment(
    assessment: AnalysisAssessment | None,
    *,
    context: AnalysisContext,
    routing: _AnalysisPMReviewRouting,
    candidate_anchor: CandidateReviewAnchor | None = None,
) -> PMReviewRequest | None:
    if assessment is None:
        return None
    if not (routing.candidate_review_allowed or routing.current_exposure_required):
        return None
    review_reasons = _resolve_analysis_pm_review_request_reasons(
        assessment=assessment,
        routing=routing,
    )
    max_visible_event_time = _max_visible_event_time_for_assessment(
        assessment=assessment,
        context=context,
    )
    max_visible_market_time = _max_visible_market_time(context, max_visible_event_time)
    business_at = max(
        assessment.business_at,
        max_visible_event_time,
        max_visible_market_time,
    )
    try:
        return PMReviewRequest(
            request_id=_derive_pm_review_request_id(assessment),
            target_key=assessment.target_key,
            business_at=business_at,
            source="analysis_event",
            source_assessment_id=assessment.assessment_id,
            source_episode_id=None,
            source_event_ids=assessment.source_event_ids,
            review_reasons=review_reasons,
            current_exposure_required=routing.current_exposure_required,
            candidate_review_allowed=routing.candidate_review_allowed,
            max_visible_event_time=max_visible_event_time,
            max_visible_market_time=max_visible_market_time,
            required_price_level_ids=_stable_unique_tuple(
                tuple(level.level_id for level in assessment.price_level_roles)
            ),
            required_claim_ids=_stable_unique_tuple(
                (
                    *assessment.key_claim_ids,
                    *assessment.contested_prior_claim_ids,
                )
            ),
            candidate_anchor_id=(
                None
                if not routing.candidate_review_allowed or candidate_anchor is None
                else candidate_anchor.anchor_id
            ),
        )
    except PMReviewContractError as exc:
        raise AnalysisSupervisorRuntimeError(
            "Failed to materialize PM review request from analysis assessment."
        ) from exc


def _materialize_candidate_review_anchor_from_assessment(
    assessment: AnalysisAssessment | None,
    *,
    candidate_review_allowed: bool,
) -> CandidateReviewAnchor | None:
    if assessment is None:
        return None
    if candidate_review_allowed is not True:
        return None
    try:
        return CandidateReviewAnchor(
            anchor_id=_derive_candidate_review_anchor_id(assessment),
            target_key=assessment.target_key,
            created_at=assessment.business_at,
            source="analysis_assessment",
            source_assessment_id=assessment.assessment_id,
            source_event_ids=assessment.source_event_ids,
            watch_trigger_ids=(),
            expires_at=assessment.business_at + _PM_REVIEW_CANDIDATE_ANCHOR_TTL,
        )
    except PMReviewContractError as exc:
        raise AnalysisSupervisorRuntimeError(
            "Failed to materialize candidate review anchor from analysis assessment."
        ) from exc


def _analysis_pm_review_reasons(
    assessment: AnalysisAssessment,
) -> tuple[PMReviewReason, ...]:
    return tuple(
        cast(PMReviewReason, reason) for reason in assessment.pm_review_reasons if reason != "none"
    )


def _resolve_analysis_pm_review_request_reasons(
    *,
    assessment: AnalysisAssessment,
    routing: _AnalysisPMReviewRouting,
) -> tuple[PMReviewReason, ...]:
    explicit = _analysis_pm_review_reasons(assessment)
    if routing.current_exposure_required:
        return _resolve_current_exposure_review_reasons(
            assessment=assessment,
            explicit_reasons=explicit,
            active_state=routing.active_state,
        )
    if routing.candidate_review_allowed:
        return _resolve_candidate_review_reasons(explicit)
    return explicit


def _resolve_current_exposure_review_reasons(
    *,
    assessment: AnalysisAssessment,
    explicit_reasons: tuple[PMReviewReason, ...],
    active_state: str | None,
) -> tuple[PMReviewReason, ...]:
    reasons: list[PMReviewReason] = []
    preserved_annotations = [
        reason for reason in explicit_reasons if reason in _ANALYSIS_PM_ANNOTATION_ONLY_REASONS
    ]
    preserved_current_exposure_reasons = [
        reason
        for reason in explicit_reasons
        if reason != "flat_candidate_setup" and reason in _ANALYSIS_PM_ESCALATION_REASONS
    ]
    inferred_reasons = _infer_current_exposure_review_reasons(
        assessment=assessment,
        active_state=active_state,
    )
    reasons.extend(preserved_current_exposure_reasons)
    if not any(
        reason
        in {
            "current_exposure_pressure",
            "invalidation_touched",
            "risk_reward_compression",
            "material_setup",
        }
        for reason in reasons
    ):
        reasons.extend(inferred_reasons)
    reasons.extend(preserved_annotations)
    return cast(tuple[PMReviewReason, ...], _stable_unique_tuple(tuple(reasons)))


def _resolve_candidate_review_reasons(
    explicit_reasons: tuple[PMReviewReason, ...],
) -> tuple[PMReviewReason, ...]:
    reasons: list[PMReviewReason] = list(explicit_reasons)
    if not any(reason in _ANALYSIS_PM_ESCALATION_REASONS for reason in reasons):
        reasons.insert(0, "flat_candidate_setup")
    return cast(tuple[PMReviewReason, ...], _stable_unique_tuple(tuple(reasons)))


def _infer_current_exposure_review_reasons(
    *,
    assessment: AnalysisAssessment,
    active_state: str | None,
) -> tuple[PMReviewReason, ...]:
    active_roles = _active_side_material_roles(
        assessment=assessment,
        active_state=active_state,
    )
    if "invalidates" in active_roles:
        return (
            "current_exposure_pressure",
            "invalidation_touched",
            "material_level_used",
        )
    if "de_risk_or_take_profit" in active_roles:
        return (
            "current_exposure_pressure",
            "risk_reward_compression",
            "material_level_used",
        )
    return (
        "current_exposure_pressure",
        "material_level_used",
    )


def _resolve_analysis_pm_review_routing(
    *,
    layout: WorkspaceLayout,
    assessment: AnalysisAssessment | None,
) -> _AnalysisPMReviewRouting:
    if assessment is None:
        return _AnalysisPMReviewRouting(
            current_exposure_required=False,
            candidate_review_allowed=False,
            active_state=None,
        )
    active_state = _resolve_analysis_active_state(
        layout=layout,
        target_key=assessment.target_key,
    )
    if assessment.pm_current_exposure_review_required and active_state not in {None, "flat"}:
        return _AnalysisPMReviewRouting(
            current_exposure_required=True,
            candidate_review_allowed=False,
            active_state=active_state,
        )
    if _assessment_has_analysis_current_exposure_escalation_role(
        assessment=assessment,
        active_state=active_state,
    ):
        return _AnalysisPMReviewRouting(
            current_exposure_required=True,
            candidate_review_allowed=False,
            active_state=active_state,
        )
    if assessment.pm_candidate_review_required:
        return _AnalysisPMReviewRouting(
            current_exposure_required=False,
            candidate_review_allowed=True,
            active_state=active_state,
        )
    if _assessment_supports_inferred_flat_candidate_review(
        assessment=assessment,
        active_state=active_state,
    ):
        return _AnalysisPMReviewRouting(
            current_exposure_required=False,
            candidate_review_allowed=True,
            active_state=active_state,
        )
    return _AnalysisPMReviewRouting(
        current_exposure_required=False,
        candidate_review_allowed=False,
        active_state=active_state,
    )


def _resolve_analysis_active_state(
    *,
    layout: WorkspaceLayout,
    target_key: str,
) -> str | None:
    try:
        return resolve_active_exposure(
            layout=layout,
            target_key=target_key,
        ).state
    except ActiveExposureResolverError:
        try:
            portfolio_state = PortfolioStateStore(layout).read(target_key=target_key)
        except PortfolioStoreError:
            return None
        return None if portfolio_state is None else portfolio_state.state


def _assessment_has_analysis_current_exposure_escalation_role(
    *,
    assessment: AnalysisAssessment,
    active_state: str | None,
) -> bool:
    return bool(
        _active_side_material_roles_for_role_set(
            assessment=assessment,
            active_state=active_state,
            allowed_roles=_ANALYSIS_CURRENT_EXPOSURE_ESCALATION_ROLES,
        )
    )


def _assessment_supports_inferred_flat_candidate_review(
    *,
    assessment: AnalysisAssessment,
    active_state: str | None,
) -> bool:
    if active_state not in {None, "flat"}:
        return False
    if assessment.confidence == "low":
        return False
    return assessment.as_if_flat_state in {
        "weak_long",
        "strong_long",
        "weak_short",
        "strong_short",
    }


def _active_side_material_roles(
    *,
    assessment: AnalysisAssessment,
    active_state: str | None,
) -> tuple[str, ...]:
    return _active_side_material_roles_for_role_set(
        assessment=assessment,
        active_state=active_state,
        allowed_roles=_OPEN_POSITION_ACTIONABLE_ROLES,
    )


def _active_side_material_roles_for_role_set(
    *,
    assessment: AnalysisAssessment,
    active_state: str | None,
    allowed_roles: frozenset[str],
) -> tuple[str, ...]:
    if active_state in {"weak_long", "strong_long"}:
        return _stable_unique_tuple(
            tuple(
                level.role_if_already_long
                for level in assessment.price_level_roles
                if level.role_if_already_long in allowed_roles
            )
        )
    if active_state in {"weak_short", "strong_short"}:
        return _stable_unique_tuple(
            tuple(
                level.role_if_already_short
                for level in assessment.price_level_roles
                if level.role_if_already_short in allowed_roles
            )
        )
    return ()


def _persist_candidate_review_anchor(
    *,
    layout: WorkspaceLayout,
    anchor: CandidateReviewAnchor | None,
) -> Path | None:
    if anchor is None:
        return None
    try:
        return CandidateReviewAnchorStore(layout).append(anchor)
    except PMReviewStoreError as exc:
        raise AnalysisSupervisorRuntimeError(
            "Failed to append candidate review anchor record."
        ) from exc


def _persist_pm_review_request(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest | None,
) -> Path | None:
    if request is None:
        return None
    try:
        return PMReviewRequestStore(layout).append(request)
    except PMReviewStoreError as exc:
        raise AnalysisSupervisorRuntimeError("Failed to append PM review request record.") from exc


def _derive_pm_review_request_id(assessment: AnalysisAssessment) -> str:
    digest = sha256(f"analysis-assessment|{assessment.assessment_id}".encode()).hexdigest()
    return f"pm-review-request:{digest[:24]}"


def _derive_candidate_review_anchor_id(assessment: AnalysisAssessment) -> str:
    digest = sha256(f"analysis-candidate-anchor|{assessment.assessment_id}".encode()).hexdigest()
    return f"candidate-review-anchor:{digest[:24]}"


def _max_visible_event_time_for_assessment(
    *,
    assessment: AnalysisAssessment,
    context: AnalysisContext,
) -> datetime:
    source_ids = set(assessment.source_event_ids)
    visible_times = tuple(
        record.ts_event for record in context.evidence_records if record.event_id in source_ids
    )
    if not visible_times:
        raise AnalysisSupervisorRuntimeError(
            "Analysis assessment PM review request has no visible source events."
        )
    return max(visible_times)


def _max_visible_market_time(
    context: AnalysisContext,
    fallback_event_time: datetime,
) -> datetime:
    if context.market_context is None:
        return fallback_event_time
    return context.market_context.as_of_at


def _stable_unique_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return tuple(normalized)


def _append_analysis_outcome_receipt(
    *,
    layout: WorkspaceLayout,
    context: AnalysisContext,
    result: AnalysisResult,
    write_receipts: tuple[dict[str, object], ...],
    task_id: str,
    committed_at: datetime,
    research_memory_write_receipt_ids: tuple[str, ...] = (),
    learning_card_usage: _LearningCardUsage | None = None,
    context_packet: ContextPacket | None = None,
    required_memory_read_policy: str | None = None,
    llm_usage: LLMUsageReceipt | None = None,
    analysis_assessment_path: Path | None = None,
    thesis_revision: ThesisRevision | None = None,
    thesis_revision_path: Path | None = None,
) -> tuple[Path, str]:
    learning_card_usage = learning_card_usage or _LearningCardUsage(
        used_learning_card_ids=result.used_lesson_ids,
    )
    try:
        validate_persisted_analysis_outcome_dependencies(
            layout=layout,
            result=result,
            analysis_assessment_path=analysis_assessment_path,
            thesis_revision=thesis_revision,
            thesis_revision_path=thesis_revision_path,
        )
    except AnalysisOutcomeArtifactValidationError as exc:
        raise AnalysisSupervisorRuntimeError(str(exc)) from exc
    business_at = context.decision_visibility.business_at
    if context_packet is None:
        context_packet = build_analysis_context_packet(
            layout=layout,
            target_key=context.request.target_key,
            business_at=business_at,
            event_types=_active_label_values(context, prefix="event_type:"),
            source_kinds=_active_label_values(context, prefix="source_kind:"),
            evidence_records=context.evidence_records,
            target_pages=context.memory_context.target_pages,
            operator_context=context.operator_context,
            market_context=context.market_context,
            memory_read_policy=(required_memory_read_policy or CURRENT_MEMORY_READ_POLICY),
            unit_formation_lane=context.unit_formation_lane,
        )
    _append_analysis_context_packet(
        layout=layout,
        context_packet=context_packet,
        required_memory_read_policy=required_memory_read_policy,
    )
    context_visibility_audit = validate_context_packet_visibility(
        context_packet,
        required_memory_read_policy=required_memory_read_policy,
        required_runtime_scope=context_packet.runtime_scope,
        required_run_id=(
            context_packet.run_id if context_packet.runtime_scope == "replay" else None
        ),
    )
    outcome_path = _analysis_outcome_path(
        workspace_root=layout.root,
        target_key=result.target_key,
        business_at=business_at,
    )
    outcome_record_id = _analysis_outcome_record_id(task_id)
    evidence_review_dimensions_by_event_id = {
        record.event_id: classify_evidence_review_dimensions(record).to_dict()
        for record in context.evidence_records
    }
    evidence_review_summary = summarize_evidence_review_dimensions(
        context.evidence_records
    ).to_dict()
    pm_review_routing = _resolve_analysis_pm_review_routing(
        layout=layout,
        assessment=result.analysis_assessment,
    )
    candidate_review_anchor = _materialize_candidate_review_anchor_from_assessment(
        result.analysis_assessment,
        candidate_review_allowed=pm_review_routing.candidate_review_allowed,
    )
    candidate_review_anchor_path = _persist_candidate_review_anchor(
        layout=layout,
        anchor=candidate_review_anchor,
    )
    pm_review_request = _materialize_pm_review_request_from_assessment(
        result.analysis_assessment,
        context=context,
        routing=pm_review_routing,
        candidate_anchor=candidate_review_anchor,
    )
    pm_review_request_path = _persist_pm_review_request(
        layout=layout,
        request=pm_review_request,
    )
    grounding_coverage_receipt, _grounding_error = analysis_read_audit(
        receipts=write_receipts,
        context=context,
        context_packet=context_packet,
    )
    market_tool_usage = _serialize_market_tool_usage(write_receipts)
    workbench_payload = (
        {}
        if context_packet.analysis_workbench is None
        else context_packet.analysis_workbench.to_json_payload()
    )
    payload = {
        "record_id": outcome_record_id,
        "task_id": task_id,
        "target_key": result.target_key,
        "event_ids": list(result.event_ids),
        "evidence_review_dimensions_by_event_id": (evidence_review_dimensions_by_event_id),
        "evidence_review_summary": evidence_review_summary,
        "business_at": business_at.isoformat(),
        "committed_at": committed_at.isoformat(),
        "context_packet_id": context_packet.packet_id,
        "context_packet_hash": context_packet.packet_hash,
        "context_packet_stage": context_packet.stage,
        "context_packet_runtime_scope": context_packet.runtime_scope,
        "context_packet_run_id": context_packet.run_id,
        "decision_episode_id": context.request.decision_episode_id,
        "context_visibility_status": context_visibility_audit.status,
        "grounding_coverage_status": grounding_coverage_receipt.status,
        "grounding_coverage_receipt": grounding_coverage_receipt.to_json_payload(),
        "compiled_evidence_grounding_count": len(
            grounding_coverage_receipt.compiled_evidence_event_ids
        ),
        "compiled_memory_grounding_count": len(grounding_coverage_receipt.compiled_memory_cards),
        "compiled_memory_cards": list(grounding_coverage_receipt.compiled_memory_cards),
        "compiler_memory_read_receipt_ids": list(
            grounding_coverage_receipt.compiler_memory_read_receipt_ids
        ),
        "compiled_market_grounding_count": (
            grounding_coverage_receipt.compiled_market_grounding_count
        ),
        "tool_read_evidence_event_ids": list(
            grounding_coverage_receipt.tool_read_evidence_event_ids
        ),
        "tool_read_memory_refs": list(grounding_coverage_receipt.tool_read_memory_refs),
        "tool_read_market_refs": list(grounding_coverage_receipt.tool_read_market_refs),
        "analysis_workbench_char_count": _json_char_count(workbench_payload),
        "context_packet_char_count": len(serialize_context_packet(context_packet)),
        "tool_call_count": _tool_call_count(write_receipts),
        "read_evidence_call_count": _operation_call_count(
            write_receipts,
            {"read_evidence"},
        ),
        "memory_read_call_count": _operation_call_count(
            write_receipts,
            ANALYSIS_MEMORY_READ_OPERATIONS,
        ),
        "market_tool_call_count": _operation_call_count(
            write_receipts,
            ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS,
        ),
        "memory_lane_omission_count": (
            0
            if context_packet.analysis_workbench is None
            else len(context_packet.analysis_workbench.memory_impact_lane.omission_receipts)
        ),
        "outcome": result.outcome,
        "analysis_assessment_id": (
            None if result.analysis_assessment is None else result.analysis_assessment.assessment_id
        ),
        "analysis_assessment_path": (
            None if analysis_assessment_path is None else str(analysis_assessment_path)
        ),
        "analysis_assessment": (
            None
            if result.analysis_assessment is None
            else result.analysis_assessment.to_json_payload()
        ),
        "thesis_revision_id": (None if thesis_revision is None else thesis_revision.revision_id),
        "thesis_revision_path": (
            None if thesis_revision_path is None else str(thesis_revision_path)
        ),
        "pm_review_request_id": (
            None if pm_review_request is None else pm_review_request.request_id
        ),
        "candidate_review_anchor_id": (
            None if candidate_review_anchor is None else candidate_review_anchor.anchor_id
        ),
        "candidate_review_anchor_path": (
            None if candidate_review_anchor_path is None else str(candidate_review_anchor_path)
        ),
        "candidate_review_anchor": (
            None if candidate_review_anchor is None else candidate_review_anchor.to_json_payload()
        ),
        "pm_review_request_path": (
            None if pm_review_request_path is None else str(pm_review_request_path)
        ),
        "pm_review_request": (
            None if pm_review_request is None else pm_review_request.to_json_payload()
        ),
        "visible_lesson_ids": list(learning_card_usage.visible_learning_card_ids),
        "included_lesson_ids": list(learning_card_usage.included_learning_card_ids),
        "used_lesson_ids": list(learning_card_usage.used_learning_card_ids),
        "lesson_diagnostics": {
            "excluded_future_count": learning_card_usage.excluded_future_count,
            "excluded_legacy_count": learning_card_usage.excluded_legacy_count,
            "parse_warning_count": learning_card_usage.parse_warning_count,
        },
        "material_memory_writes": [
            _serialize_analysis_outcome_write(receipt)
            for receipt in write_receipts
            if _is_material_memory_write_receipt(receipt)
        ],
        "analysis_write_support_diagnostics": _analysis_support_diagnostics(write_receipts),
        "staged_material_write_count": _material_memory_write_count(write_receipts),
        "committed_material_write_count": len(research_memory_write_receipt_ids),
        "collapsed_material_write_count": max(
            0,
            _material_memory_write_count(write_receipts) - len(research_memory_write_receipt_ids),
        ),
        "research_memory_write_receipt_ids": list(research_memory_write_receipt_ids),
        "market_context_present": context.market_context is not None,
        "market_context_audit": (
            context.market_context.compact_audit() if context.market_context is not None else None
        ),
        "market_tool_usage": market_tool_usage,
        "llm_usage": None if llm_usage is None else llm_usage.to_json_payload(),
        "why_escalated": context.request.why_escalated,
    }
    if _jsonl_record_exists(outcome_path, record_id=outcome_record_id):
        return outcome_path, outcome_record_id
    try:
        with outcome_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
            handle.write("\n")
    except OSError as exc:
        raise AnalysisSupervisorRuntimeError("Failed to append analysis outcome receipt.") from exc
    if context.request.decision_episode_id:
        try:
            FileBackedDecisionEpisodeStore(layout).append_record(
                DecisionEpisodeRecord(
                    episode_id=context.request.decision_episode_id,
                    target_key=result.target_key,
                    record_type="analysis",
                    business_at=business_at,
                    recorded_at=committed_at,
                    event_ids=tuple(result.event_ids),
                    source_record_id=outcome_record_id,
                    source_path=str(outcome_path),
                    source_line=None,
                    context_packet_id=context_packet.packet_id,
                    context_packet_hash=context_packet.packet_hash,
                    status=result.outcome,
                    payload={
                        "outcome": result.outcome,
                        "analysis_assessment_id": (
                            None
                            if result.analysis_assessment is None
                            else result.analysis_assessment.assessment_id
                        ),
                        "research_memory_write_receipt_ids": list(
                            research_memory_write_receipt_ids
                        ),
                        "task_id": task_id,
                    },
                )
            )
        except DecisionEpisodeStoreError as exc:
            if "duplicate natural key" not in str(exc):
                raise
    return outcome_path, outcome_record_id


def _serialize_analysis_outcome_write(receipt: dict[str, object]) -> dict[str, str]:
    try:
        return serialize_analysis_write_receipt_summary(receipt)
    except AnalysisWriteReceiptContractError as exc:
        raise AnalysisSupervisorRuntimeError(str(exc)) from exc


def _serialize_market_tool_usage(
    receipts: tuple[dict[str, object], ...],
) -> dict[str, object]:
    duplicate_call_count = sum(
        1
        for receipt in receipts
        if receipt.get("operation") in ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS
        and receipt.get("result_mode") == "duplicate"
    )
    calls = tuple(
        _serialize_market_tool_call(receipt)
        for receipt in receipts
        if receipt.get("operation") in ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS
        and receipt.get("result_mode") != "duplicate"
    )
    statuses = tuple(
        status
        for call in calls
        if isinstance((status := call.get("status")), str) and status.strip()
    )
    tools_called = tuple(
        dict.fromkeys(
            operation for call in calls if isinstance((operation := call.get("operation")), str)
        )
    )
    return {
        "tool_call_count": len(calls),
        "duplicate_tool_call_count": duplicate_call_count,
        "tools_called": list(tools_called),
        "statuses": list(statuses),
        "unavailable_count": sum(1 for status in statuses if status == "unavailable"),
        "partial_count": sum(1 for status in statuses if status == "partial"),
        "truncated_count": sum(
            1 for call in calls if isinstance(call.get("truncated"), bool) and call["truncated"]
        ),
        "calls": list(calls),
    }


def _tool_call_count(receipts: tuple[dict[str, object], ...]) -> int:
    return _operation_call_count(receipts, ANALYSIS_READ_RECEIPT_OPERATIONS)


def _operation_call_count(
    receipts: tuple[dict[str, object], ...],
    operations: frozenset[str] | set[str],
) -> int:
    return sum(
        1
        for receipt in receipts
        if receipt.get("operation") in operations and receipt.get("result_mode") != "duplicate"
    )


def _json_char_count(payload: object) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _serialize_market_tool_call(receipt: dict[str, object]) -> dict[str, object]:
    return {
        "operation": _optional_receipt_text(receipt, "operation"),
        "arguments": _compact_market_tool_arguments(receipt.get("arguments")),
        "status": _optional_receipt_text(receipt, "status"),
        "row_count": _optional_int(receipt.get("row_count")),
        "contract_count": _optional_int(receipt.get("contract_count")),
        "payload_sha256": _optional_receipt_text(receipt, "payload_sha256"),
        "truncated": receipt.get("truncated") is True,
    }


def _compact_market_tool_arguments(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        jsonable_item = _compact_market_tool_argument_value(item)
        if jsonable_item is not None:
            compact[key] = jsonable_item
    return compact


def _compact_market_tool_argument_value(value: object) -> object | None:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [
            item
            for item in (_compact_market_tool_argument_value(item) for item in value)
            if item is not None
        ]
    if isinstance(value, dict):
        return _compact_market_tool_arguments(value)
    return str(value)


def _optional_receipt_text(
    receipt: dict[str, object],
    field_name: str,
) -> str | None:
    value = receipt.get(field_name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_material_memory_write_receipt(receipt: dict[str, object]) -> bool:
    try:
        return is_analysis_material_write_receipt(receipt)
    except AnalysisWriteReceiptContractError as exc:
        raise AnalysisSupervisorRuntimeError(str(exc)) from exc


def _material_memory_write_count(receipts: tuple[dict[str, object], ...]) -> int:
    return sum(1 for receipt in receipts if _is_material_memory_write_receipt(receipt))


def _analysis_outcome_record_id(task_id: str) -> str:
    return f"analysis-outcome:{task_id}"


def _jsonl_record_exists(path: Path, *, record_id: str) -> bool:
    if not path.exists():
        return False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict) and payload.get("record_id") == record_id:
                return True
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisSupervisorRuntimeError(
            "Failed to inspect analysis outcome receipt for idempotency."
        ) from exc
    return False


def _analysis_support_diagnostics(
    write_receipts: tuple[dict[str, object], ...],
) -> dict[str, object]:
    material_writes = tuple(
        receipt for receipt in write_receipts if _is_material_memory_write_receipt(receipt)
    )
    absence_support_payloads: list[dict[str, object]] = []
    maturity_support_payloads: list[dict[str, object]] = []

    for receipt in material_writes:
        operation = _require_receipt_text(receipt, "operation")

        absence_payload = receipt.get("absence_based_support")
        if isinstance(absence_payload, dict):
            absence_support_payloads.append(
                {
                    "operation": operation,
                    "used": bool(absence_payload.get("used")),
                    "reason_codes": _normalize_support_string_tuple_relaxed(
                        absence_payload.get("reason_codes"),
                    ),
                    "domains": _normalize_support_string_tuple_relaxed(
                        absence_payload.get("domains"),
                    ),
                    "support_ref_count": len(
                        _normalize_support_string_tuple_relaxed(
                            absence_payload.get("support_refs"),
                            allow_empty=True,
                        )
                    ),
                }
            )

        maturity_payload = receipt.get("maturity_based_support")
        if isinstance(maturity_payload, dict):
            maturity_support_payloads.append(
                {
                    "operation": operation,
                    "used": bool(maturity_payload.get("used")),
                    "reason_codes": _normalize_support_string_tuple_relaxed(
                        maturity_payload.get("reason_codes"),
                    ),
                    "domains": _normalize_support_string_tuple_relaxed(
                        maturity_payload.get("domains"),
                    ),
                    "support_ref_count": len(
                        _normalize_support_string_tuple_relaxed(
                            maturity_payload.get("support_refs"),
                            allow_empty=True,
                        )
                    ),
                }
            )

    return {
        "material_write_count": len(material_writes),
        "absence_support_payload_count": len(absence_support_payloads),
        "maturity_support_payload_count": len(maturity_support_payloads),
        "absence_support_used_count": sum(1 for item in absence_support_payloads if item["used"]),
        "maturity_support_used_count": sum(1 for item in maturity_support_payloads if item["used"]),
        "absence_support_payloads": absence_support_payloads,
        "maturity_support_payloads": maturity_support_payloads,
    }


def _normalize_support_string_tuple_relaxed(
    value: object,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if value is None:
        if allow_empty:
            return ()
        return ()
    if not isinstance(value, (list, tuple)):
        return ()
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        normalized_item = item.strip()
        if not normalized_item:
            continue
        normalized.append(normalized_item)
    if not normalized and not allow_empty:
        return ()
    return tuple(normalized)


def _validate_analysis_write_contract(
    *,
    result: AnalysisResult,
    context: AnalysisContext,
    write_receipts: tuple[dict[str, object], ...],
    context_packet: ContextPacket | None = None,
    attempt_index: int = 0,
) -> tuple[AnalysisResult, tuple[dict[str, object], ...]]:
    active_citations = {record.event_id: record.source_ref for record in context.evidence_records}
    try:
        context_citations = collect_analysis_context_citations(
            context=context,
            receipts=write_receipts,
            active_citations=active_citations,
        )
    except AnalysisWriteCitationError as exc:
        raise AnalysisSupervisorRuntimeError(str(exc)) from exc
    unit_formation_lane = analysis_unit_formation_lane(
        context=context,
        context_packet=context_packet,
    )
    grounded_support_refs = collect_analysis_support_refs(
        write_receipts=write_receipts,
        context=context,
        context_packet=context_packet,
    )
    committable_write_receipts: list[dict[str, object]] = []
    saw_material_write = False
    for receipt in write_receipts:
        try:
            admitted_receipt = admit_analysis_write_receipt(receipt)
        except AnalysisWriteReceiptContractError as exc:
            raise AnalysisSupervisorRuntimeError(str(exc)) from exc
        if admitted_receipt is None:
            continue
        content_md = admitted_receipt.content_md
        try:
            validate_analysis_write_support(
                support_receipt=receipt,
                unit_formation_lane=unit_formation_lane,
                grounded_support_refs=grounded_support_refs,
            )
        except AnalysisWriteSupportError as exc:
            raise AnalysisSupervisorRuntimeError(str(exc)) from exc
        try:
            content_md = canonicalize_analysis_write_citations(
                content_md=content_md,
                active_citations=active_citations,
                context_citations=context_citations,
            )
        except AnalysisWriteCitationError as exc:
            raise AnalysisSupervisorRuntimeError(str(exc)) from exc
        saw_material_write = True
        committable_write_receipts.append(
            {
                **receipt,
                "content_md": content_md,
            }
        )
        try:
            validate_analysis_write_citations(
                content_md=content_md,
                active_citations=active_citations,
                context_citations=context_citations,
            )
        except AnalysisWriteCitationError as exc:
            raise AnalysisSupervisorRuntimeError(str(exc)) from exc
    try:
        validate_analysis_watchlist_maintenance(
            target_key=context.request.target_key,
            required=context.request.requires_watchlist_maintenance,
            write_receipts=tuple(committable_write_receipts),
        )
    except AnalysisWriteReceiptContractError as exc:
        raise AnalysisSupervisorRuntimeError(str(exc)) from exc
    try:
        final_result, effective_write_receipts = finalize_analysis_write_outcome(
            result=result,
            saw_material_write=saw_material_write,
            write_receipts=tuple(committable_write_receipts),
        )
    except AnalysisWriteReceiptContractError as exc:
        raise AnalysisSupervisorRuntimeError(str(exc)) from exc
    if final_result.outcome == "no_update":
        try:
            validate_analysis_commit_receipts(
                target_key=final_result.target_key,
                analysis_assessment=final_result.analysis_assessment,
                write_receipts=(),
            )
        except (
            AnalysisCommitContractError,
            AnalysisWriteReceiptContractError,
        ) as exc:
            raise AnalysisSupervisorRuntimeError(str(exc)) from exc
        return final_result, ()
    protected_rewrite_repair = protected_rewrite_contract_repair_required(
        write_receipts=effective_write_receipts,
        analysis_assessment=final_result.analysis_assessment,
        attempt_index=attempt_index,
    )
    if protected_rewrite_repair is not None:
        raise protected_rewrite_repair
    try:
        final_write_receipts = collapse_analysis_write_receipts(effective_write_receipts)
    except AnalysisWriteReceiptContractError as exc:
        raise AnalysisSupervisorRuntimeError(str(exc)) from exc
    try:
        validate_analysis_commit_receipts(
            target_key=final_result.target_key,
            analysis_assessment=final_result.analysis_assessment,
            write_receipts=final_write_receipts,
        )
    except (AnalysisCommitContractError, AnalysisWriteReceiptContractError) as exc:
        raise AnalysisSupervisorRuntimeError(str(exc)) from exc
    return final_result, final_write_receipts


def _require_receipt_text(receipt: dict[str, object], field_name: str) -> str:
    value = receipt.get(field_name)
    if not isinstance(value, str):
        raise AnalysisSupervisorRuntimeError(
            f"Analysis write receipt field {field_name!r} must be a string."
        )
    normalized = value.strip()
    if not normalized:
        raise AnalysisSupervisorRuntimeError(
            f"Analysis write receipt field {field_name!r} must not be blank."
        )
    return normalized
