"""Project-owned bounded cognition workflow for one Analysis attempt."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import ClassVar
from uuid import uuid4

from event_trader.analysis import FileBackedResearchMemoryReader
from event_trader.contracts import LLMUsageReceipt
from event_trader.contracts.analysis_assessment import (
    AnalysisAssessment,
    AnalysisAssessmentContractError,
    parse_analysis_assessment,
)
from event_trader.contracts.analysis_assessment_schema import (
    AnalysisOwnerDraftConstraints,
    analysis_assessment_draft_contract_markdown,
    analysis_assessment_draft_payload_json_schema,
    analysis_assessment_precommit_defaults,
    analysis_assessment_runtime_owned_fields,
    analysis_decision_payload_json_schema,
    analysis_decision_requirements_markdown,
    assemble_price_level_actions,
)
from event_trader.contracts.execution_direction_policy import (
    ExecutionDirectionMode,
    validate_execution_direction_mode,
)
from event_trader.contracts.price_level_role import PriceLevelRole
from event_trader.contracts.research_memory import resolve_page_ref
from event_trader.integrations.analysis_citations import extract_analysis_citations
from event_trader.integrations.markdown_context import (
    MarkdownContextError,
    build_citation_neighborhood,
    build_markdown_page_map,
)
from event_trader.reasoning.analysis_agent import (
    ANALYSIS_AGENT_CONTRACT,
    AnalysisAgentAttemptRequest,
    AnalysisAgentAttemptResult,
    AnalysisAgentExecutor,
    AnalysisRepairOwnership,
)
from event_trader.reasoning.analysis_contract_repair import (
    ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
    AnalysisContractFailure,
    AnalysisWriteFailureBudgetExceeded,
    AnalysisWriteToolFailure,
    parse_analysis_write_tool_failure,
)
from event_trader.reasoning.analysis_read_audit import (
    analysis_read_audit_for_requirements,
)
from event_trader.reasoning.analysis_tools import (
    ANALYSIS_TOOL_NAMES,
    AnalysisToolContext,
    AnalysisToolGateway,
)
from event_trader.reasoning.analysis_write_receipts import (
    analysis_revision_plan_contract_failure,
)
from event_trader.reasoning.anthropic_structured import (
    AnthropicStructuredInference,
    AnthropicStructuredInferenceConfig,
    AnthropicStructuredInferenceError,
    structured_inference_session,
)
from event_trader.reasoning.effort import normalize_agent_reasoning_effort
from event_trader.reasoning.structured_inference import (
    StructuredInference,
    StructuredInferenceResult,
)
from event_trader.reasoning.token_budget import (
    StructuredPromptBudgetError,
    assert_structured_prompt_budget,
    validate_structured_token_budget,
)
from event_trader.research_memory.active_price_basis_alignment import (
    ActivePriceBasisAlignmentError,
    align_analysis_assessment_to_active_price_basis,
)
from event_trader.research_memory.analysis_price_semantics_assembler import (
    enrich_assessment_with_analysis_price_semantics,
)
from event_trader.thesis_revision.canonical_bundle import canonical_thesis_page_sections

_READ_PLAN_TOOL = "analysis_read_plan"
_EVIDENCE_EXPERT_TOOL = "analysis_evidence_causality_view"
_MARKET_EXPERT_TOOL = "analysis_market_price_view"
_THESIS_EXPERT_TOOL = "analysis_thesis_memory_view"
_DECISION_TOOL = "analysis_decision"
_ASSESSMENT_TOOL = "analysis_assessment_synthesis"
_REVISION_PLAN_TOOL = "analysis_memory_revision_plan"
_REVISION_WRITER_TOOL = "analysis_memory_revision_writer"

_MAX_READ_CALLS = 16
_MAX_WRITE_CALLS = 12
_MAX_CONCURRENT_REVISION_WRITERS = 3
_TEMPERATURE = 0.1
_TRANSPORT_MAX_ATTEMPTS = 5
_TRANSPORT_RETRY_SECONDS = 10
_STRUCTURED_PROTOCOL_MAX_ATTEMPTS = 3

_EXPERT_RECOMMENDATIONS = (
    "emit_assessment",
    "no_material_assessment",
    "uncertain",
)
_ASSESSMENT_DECISIONS = ("emit_assessment", "no_material_assessment")

_READ_TOOL_NAMES = (
    "read_evidence",
    "read_page",
    "read_section",
    "read_around_citation",
    "list_pages",
    "search_wiki",
    "read_market_overview",
    "read_price_volume_window",
    "read_technical_panel",
    "read_option_activity",
    "read_cross_asset_context",
    "read_cross_asset_window",
)
_MARKET_READ_TOOL_NAMES = frozenset(
    {
        "read_market_overview",
        "read_price_volume_window",
        "read_technical_panel",
        "read_option_activity",
        "read_cross_asset_context",
        "read_cross_asset_window",
    }
)
_WRITE_TOOL_NAMES = (
    "update_page_section",
    "rewrite_page",
    "create_page",
)
class AnalysisWorkflowError(RuntimeError):
    """Raised when one project-owned Analysis cognition attempt is untrustworthy."""


@dataclass(frozen=True, slots=True)
class _MemoryTopology:
    """Actual per-attempt ResearchMemory paths and headings available to the workflow."""

    readable_pages: tuple[str, ...]
    readable_section_targets: tuple[tuple[str, str], ...]
    readable_citation_targets: tuple[tuple[str, str], ...]
    writable_pages: tuple[str, ...]
    writable_section_targets: tuple[tuple[str, str], ...]

    def readable_payload(self) -> dict[str, object]:
        return {
            "pages": list(self.readable_pages),
            "section_targets": [
                {"page_path": page_path, "section_name": section_name}
                for page_path, section_name in self.readable_section_targets
            ],
            "citation_targets": [
                {"page_path": page_path, "event_id": event_id}
                for page_path, event_id in self.readable_citation_targets
            ],
        }

    def writable_payload(self) -> dict[str, object]:
        return {
            "pages": list(self.writable_pages),
            "section_targets": [
                {"page_path": page_path, "section_name": section_name}
                for page_path, section_name in self.writable_section_targets
            ],
        }


@dataclass(slots=True)
class _RepairViolation:
    key: str
    violation: dict[str, object]
    attempts: list[int]

    def trace_payload(self) -> dict[str, object]:
        return {
            "key": self.key,
            "violation": dict(self.violation),
            "attempts": list(self.attempts),
        }


@dataclass(slots=True)
class _RepairViolationLedger:
    """Bounded, auditable validation history for one coherent draft repair loop."""

    _entries_by_key: dict[str, _RepairViolation]

    @classmethod
    def empty(cls) -> _RepairViolationLedger:
        return cls(_entries_by_key={})

    def record(
        self,
        *,
        attempt: int,
        violation: Mapping[str, object],
    ) -> None:
        normalized = _json_text(dict(violation))
        key = sha256(normalized.encode("utf-8")).hexdigest()
        entry = self._entries_by_key.get(key)
        if entry is None:
            entry = _RepairViolation(
                key=key,
                violation=dict(violation),
                attempts=[],
            )
            self._entries_by_key[key] = entry
        entry.attempts.append(attempt)

    def snapshot(self) -> list[dict[str, object]]:
        return [entry.trace_payload() for entry in self._entries_by_key.values()]


@dataclass(frozen=True, slots=True)
class AnalysisWorkflowConfig:
    """Provider and budget settings for the bounded Analysis workflow."""

    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_reasoning_effort: str | None
    llm_max_context_length: int
    llm_max_output_tokens: int
    wall_clock_timeout_seconds: int
    execution_direction_mode: ExecutionDirectionMode

    def __post_init__(self) -> None:
        object.__setattr__(self, "log_dir", _validate_path(self.log_dir, "log_dir"))
        for field_name in (
            "llm_provider",
            "llm_model_name",
            "llm_api_key",
            "llm_base_url",
        ):
            value = _non_blank(getattr(self, field_name), field_name)
            object.__setattr__(
                self,
                field_name,
                value.lower() if field_name == "llm_provider" else value,
            )
        effort = normalize_agent_reasoning_effort(
            self.llm_reasoning_effort,
            field_name="llm_reasoning_effort",
            error_type=AnalysisWorkflowError,
        )
        if effort is not None:
            raise AnalysisWorkflowError(
                "Analysis workflow does not support explicit llm_reasoning_effort "
                "without a provider-specific reasoning budget."
            )
        object.__setattr__(self, "llm_reasoning_effort", None)
        if self.llm_provider != "anthropic":
            raise AnalysisWorkflowError(
                "Analysis workflow currently requires llm_provider='anthropic'."
            )
        try:
            validate_structured_token_budget(
                max_context_tokens=self.llm_max_context_length,
                max_output_tokens=self.llm_max_output_tokens,
                field_prefix="AnalysisWorkflowConfig",
            )
        except StructuredPromptBudgetError as exc:
            raise AnalysisWorkflowError(str(exc)) from exc
        if (
            not isinstance(self.wall_clock_timeout_seconds, int)
            or isinstance(self.wall_clock_timeout_seconds, bool)
            or self.wall_clock_timeout_seconds < 0
        ):
            raise AnalysisWorkflowError(
                "wall_clock_timeout_seconds must be a non-negative integer."
            )
        object.__setattr__(
            self,
            "execution_direction_mode",
            validate_execution_direction_mode(
                self.execution_direction_mode,
                error_type=AnalysisWorkflowError,
            ),
        )


@dataclass(frozen=True, slots=True)
class AnalysisWorkflowExecutor:
    """Execute the fixed Analysis cognitive stages inside one supervisor attempt."""

    repair_ownership: ClassVar[AnalysisRepairOwnership] = "stage_local"
    config: AnalysisWorkflowConfig
    provider: StructuredInference

    async def execute(
        self,
        request: AnalysisAgentAttemptRequest,
    ) -> AnalysisAgentAttemptResult:
        if request.contract != ANALYSIS_AGENT_CONTRACT:
            raise AnalysisWorkflowError(
                "Analysis workflow requires the canonical ANALYSIS_AGENT_CONTRACT."
            )
        trace: dict[str, object] = {
            "implementation": "event_trader",
            "workflow": "bounded_analysis_decision_assessment_v1",
            "task_id": request.task_id,
            "provider": self.config.llm_provider,
            "model": self.config.llm_model_name,
            "status": "running",
        }
        try:
            async with structured_inference_session(self.provider) as attempt_provider:
                run = _execute_workflow_attempt(
                    config=self.config,
                    provider=attempt_provider,
                    request=request,
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
        except AnalysisWriteFailureBudgetExceeded as exc:
            trace["status"] = "failed"
            trace["error_type"] = type(exc).__name__
            trace["error"] = str(exc)
            _record_trace_usage_totals(trace)
            _write_trace(config=self.config, request=request, trace=trace)
            raise
        except TimeoutError as exc:
            message = (
                "Analysis workflow timed out after "
                f"{self.config.wall_clock_timeout_seconds} seconds."
            )
            trace.update(status="failed", error_type="TimeoutError", error=message)
            _record_trace_usage_totals(trace)
            _write_trace(config=self.config, request=request, trace=trace)
            raise AnalysisWorkflowError(message) from exc
        except Exception as exc:
            message = str(exc).strip() or repr(exc)
            trace.update(
                status="failed",
                error_type=type(exc).__name__,
                error=message,
            )
            _record_trace_usage_totals(trace)
            _write_trace(config=self.config, request=request, trace=trace)
            if isinstance(exc, AnalysisWorkflowError):
                raise
            raise AnalysisWorkflowError(message) from exc
        trace["status"] = "succeeded"
        _record_trace_usage_totals(trace)
        _write_trace(config=self.config, request=request, trace=trace)
        return AnalysisAgentAttemptResult(
            payload=result.payload,
            llm_usage=result.llm_usage,
        )


@dataclass(frozen=True, slots=True)
class _WorkflowResult:
    payload: Mapping[str, object]
    llm_usage: LLMUsageReceipt


@dataclass(frozen=True, slots=True)
class _RevisionWriterInitialSpec:
    index: int
    intent: Mapping[str, object]
    prompt: str
    schema: Mapping[str, object]
    session_id: str


async def _execute_workflow_attempt(
    *,
    config: AnalysisWorkflowConfig,
    provider: StructuredInference,
    request: AnalysisAgentAttemptRequest,
    trace: dict[str, object],
) -> _WorkflowResult:
    gateway = AnalysisToolGateway(context=request.tool_context)
    _validate_gateway_contract(request=request)
    inference_results: list[StructuredInferenceResult] = []
    stage_receipts: list[dict[str, object]] = []
    trace["structured_stage_receipts"] = stage_receipts
    memory_topology = _memory_topology(gateway=gateway)
    trace["memory_topology"] = {
        "readable": memory_topology.readable_payload(),
        "writable": memory_topology.writable_payload(),
    }

    owner_task = _workflow_business_task_prompt(request)
    requirements = request.read_audit_requirements
    if requirements is None:
        raise AnalysisWorkflowError(
            "event-trader Analysis workflow requires deterministic read-audit requirements."
        )
    read_plan_attempts: list[dict[str, object]] = []
    trace["read_plan_attempts"] = read_plan_attempts
    all_read_calls: list[tuple[str, dict[str, object]]] = []
    read_results: list[dict[str, object]] = []
    prior_read_plan: Mapping[str, object] | None = None
    read_audit_failure: AnalysisContractFailure | None = None
    for read_plan_attempt_index in range(ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS):
        remaining_call_budget = _MAX_READ_CALLS - len(all_read_calls)
        if remaining_call_budget <= 0:
            raise AnalysisWorkflowError("Analysis read-plan call budget exhausted.")
        base_read_plan_prompt = _read_plan_prompt(
            owner_task,
            memory_topology=memory_topology,
            max_read_calls=remaining_call_budget,
            target_key=request.tool_context.target_key,
            tradable_proxy_symbol=(
                None
                if request.market_context is None
                else request.market_context.tradable_proxy_symbol
            ),
        )
        active_read_plan_prompt = (
            base_read_plan_prompt
            if read_audit_failure is None
            else _read_plan_repair_prompt(
                base_read_plan_prompt=base_read_plan_prompt,
                prior_read_plan=prior_read_plan,
                prior_read_results=read_results,
                failure=read_audit_failure,
                max_read_calls=remaining_call_budget,
            )
        )
        _assert_prompt_budget(config, active_read_plan_prompt, stage="read_plan")
        read_plan, read_plan_receipt = await _generate_structured_stage(
            trace_receipts=stage_receipts,
            stage="read_plan",
            business_repair_attempt=read_plan_attempt_index + 1,
            provider=provider,
            prompt=active_read_plan_prompt,
            session_id=f"{request.task_id}:read-plan",
            tool_name=_READ_PLAN_TOOL,
            tool_description=(
                "Plan only the bounded project-owned Analysis reads needed for this attempt."
            ),
            input_schema=_read_plan_schema(
                memory_topology=memory_topology,
                max_read_calls=remaining_call_budget,
                target_key=request.tool_context.target_key,
            ),
        )
        inference_results.append(read_plan)
        planned_calls = _parse_calls(
            read_plan.payload,
            field_name="read_calls",
            allowed_names=_READ_TOOL_NAMES,
            max_calls=remaining_call_budget,
            runtime_market_as_of_at=request.tool_context.business_at,
        )
        _validate_read_memory_topology(
            calls=planned_calls,
            memory_topology=memory_topology,
        )
        attempt = {
            "attempt": read_plan_attempt_index + 1,
            "read_plan": dict(read_plan.payload),
            "executed_read_calls": [],
        }
        read_plan_attempts.append(attempt)
        try:
            executed_results = _execute_read_calls(
                gateway=gateway,
                calls=planned_calls,
                trace_attempt=attempt,
            )
        except Exception as exc:
            attempt["status"] = "tool_execution_failed"
            attempt["failure"] = {
                "error_code": "analysis_read_tool_execution_failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            raise
        all_read_calls.extend(planned_calls)
        read_results.extend(executed_results)
        coverage_receipt, audit_error = analysis_read_audit_for_requirements(
            receipts=_load_workflow_activity_receipts(request.tool_context.receipt_path),
            requirements=requirements,
        )
        attempt["coverage"] = _coverage_receipt_payload(coverage_receipt)
        if audit_error is None:
            _mark_stage_business_status(read_plan_receipt, "accepted")
            attempt["status"] = "accepted"
            prior_read_plan = dict(read_plan.payload)
            break
        _mark_stage_business_status(read_plan_receipt, "rejected")
        read_audit_failure = AnalysisContractFailure(
            surface="read_audit",
            error_code="analysis_read_audit_contract_violation",
            message=audit_error,
            suggested_action=(
                "Plan only the missing project-owned evidence, target memory, or "
                "market reads identified by the deterministic grounding coverage."
            ),
            details={
                "failed_reasons": coverage_receipt.failed_reasons,
                "missing_evidence_event_ids": tuple(
                    event_id
                    for event_id in requirements.active_event_ids
                    if event_id
                    not in (
                        set(coverage_receipt.compiled_evidence_event_ids)
                        | set(coverage_receipt.tool_read_evidence_event_ids)
                    )
                ),
            },
        )
        attempt["status"] = "rejected"
        attempt["failure"] = {
            "error_code": read_audit_failure.error_code,
            "message": read_audit_failure.message,
            "details": read_audit_failure.details,
        }
        prior_read_plan = dict(read_plan.payload)
        if read_plan_attempt_index + 1 >= ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS:
            raise AnalysisWorkflowError(
                "Analysis read-plan coverage repair exhausted after "
                f"{ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS} attempts: {audit_error}"
            )
    else:
        raise AssertionError("Analysis read-plan repair loop ended without coverage.")
    read_calls = tuple(all_read_calls)
    trace["read_plan"] = dict(prior_read_plan or {})
    trace["read_results"] = read_results

    expert_context = _json_text(read_results, pretty=True)
    evidence_prompt = _expert_prompt(
        owner_task=owner_task,
        read_results=expert_context,
        role="Evidence/Causality",
        instructions=(
            "Separate admitted facts from inference; reconstruct causal transmission; "
            "surface contradictions, source-quality limits, missing evidence, and the "
            "specific durable-memory implications. Do not draft final JSON or writes."
        ),
    )
    thesis_prompt = _expert_prompt(
        owner_task=owner_task,
        read_results=expert_context,
        role="Thesis/Memory",
        instructions=(
            "Compare the active evidence with existing ResearchMemory; identify what "
            "must change, what must remain, contested claims, invalidation logic, "
            "watch items, and the strongest no-update case. Remain exposure-blind. "
            "Do not draft final JSON or writes."
        ),
    )
    market_required = _market_expert_required(request=request, read_calls=read_calls)
    market_prompt = _expert_prompt(
        owner_task=owner_task,
        read_results=expert_context,
        role="Market/Price",
        instructions=(
            "Interpret only visible decision-time market facts: price/volume structure, "
            "confirmation, non-confirmation, price-basis mismatches, levels, invalidation "
            "and data gaps. Market facts are not mechanical trade signals and you are "
            "exposure-blind. Do not draft final JSON or writes."
        ),
    )
    _assert_prompt_budget(config, evidence_prompt, stage="evidence_expert")
    _assert_prompt_budget(config, thesis_prompt, stage="thesis_expert")
    if market_required:
        _assert_prompt_budget(config, market_prompt, stage="market_expert")

    expert_coroutines = [
        _generate_structured_stage(
            trace_receipts=stage_receipts,
            stage="evidence_expert",
            provider=provider,
            prompt=evidence_prompt,
            session_id=f"{request.task_id}:evidence-expert",
            tool_name=_EVIDENCE_EXPERT_TOOL,
            tool_description="Return the bounded Evidence/Causality expert view.",
            input_schema=_expert_schema(),
        ),
        _generate_structured_stage(
            trace_receipts=stage_receipts,
            stage="thesis_expert",
            provider=provider,
            prompt=thesis_prompt,
            session_id=f"{request.task_id}:thesis-expert",
            tool_name=_THESIS_EXPERT_TOOL,
            tool_description="Return the bounded Thesis/Memory expert view.",
            input_schema=_expert_schema(),
        ),
    ]
    if market_required:
        expert_coroutines.append(
            _generate_structured_stage(
                trace_receipts=stage_receipts,
                stage="market_expert",
                provider=provider,
                prompt=market_prompt,
                session_id=f"{request.task_id}:market-expert",
                tool_name=_MARKET_EXPERT_TOOL,
                tool_description="Return the bounded Market/Price expert view.",
                input_schema=_expert_schema(),
            )
        )
    expert_stage_results = await asyncio.gather(*expert_coroutines)
    expert_results = [result for result, _ in expert_stage_results]
    for _, stage_receipt in expert_stage_results:
        _mark_stage_business_status(stage_receipt, "accepted")
    inference_results.extend(expert_results)
    evidence_view = dict(expert_results[0].payload)
    thesis_view = dict(expert_results[1].payload)
    market_view: Mapping[str, object] | None = (
        dict(expert_results[2].payload) if market_required else None
    )
    trace["market_expert_required"] = market_required
    trace["expert_views"] = {
        "evidence_causality": evidence_view,
        "thesis_memory": thesis_view,
        "market_price": market_view,
    }
    available_expert_views: dict[str, Mapping[str, object]] = {
        "evidence_causality": evidence_view,
        "thesis_memory": thesis_view,
    }
    if market_view is not None:
        available_expert_views["market_price"] = market_view

    decision_prompt = _decision_prompt(
        owner_task=owner_task,
        read_results=read_results,
        evidence_view=evidence_view,
        thesis_view=thesis_view,
        market_view=market_view,
        decision_contract=analysis_decision_requirements_markdown(),
    )
    decision_schema = _decision_schema(request=request)
    trace["decision_frozen_context"] = {
        "business_task": owner_task,
        "decision_prompt": decision_prompt,
    }
    expert_recommendations = _validated_expert_recommendations(available_expert_views)
    trace["expert_recommendations"] = dict(expert_recommendations)
    decision_attempts: list[dict[str, object]] = []
    trace["decision_attempts"] = decision_attempts
    decision_draft: dict[str, object] | None = None
    for decision_attempt_index in range(2):
        active_decision_prompt = decision_prompt
        if decision_draft is not None:
            contested_roles = _contested_no_material_roles(
                decision_draft=decision_draft,
                expert_recommendations=expert_recommendations,
            )
            if not contested_roles:
                break
            active_decision_prompt = _decision_reconsideration_prompt(
                decision_prompt,
                initial_decision_draft=decision_draft,
                contested_expert_roles=contested_roles,
            )
        _assert_prompt_budget(config, active_decision_prompt, stage="analysis_decision")
        decision_result, decision_stage_receipt = await _generate_structured_stage(
            trace_receipts=stage_receipts,
            stage="analysis_decision",
            business_repair_attempt=decision_attempt_index + 1,
            provider=provider,
            prompt=active_decision_prompt,
            session_id=f"{request.task_id}:decision",
            tool_name=_DECISION_TOOL,
            tool_description="Return the canonical Analysis materiality decision.",
            input_schema=decision_schema,
        )
        inference_results.append(decision_result)
        decision_draft = dict(decision_result.payload)
        assessment_decision = _validated_assessment_decision(
            decision_draft=decision_draft,
        )
        decision_attempt: dict[str, object] = {
            "attempt": decision_attempt_index + 1,
            "decision_draft": dict(decision_draft),
            "decision_validation": {"ok": True},
        }
        decision_attempts.append(decision_attempt)
        trace["decision_attempt_count"] = len(decision_attempts)
        contested_roles = _contested_no_material_roles(
            decision_draft=decision_draft,
            expert_recommendations=expert_recommendations,
        )
        if decision_attempt_index == 0 and contested_roles:
            _mark_stage_business_status(decision_stage_receipt, "accepted")
            challenge = {
                "reason": "contested_no_material_assessment",
                "expert_roles": contested_roles,
            }
            decision_attempt.update(status="challenged", challenge=challenge)
            trace["decision_reconsideration"] = challenge
            continue
        _mark_stage_business_status(decision_stage_receipt, "accepted")
        decision_attempt["status"] = "accepted"
        trace["accepted_assessment_decision"] = assessment_decision
        break
    else:
        raise AnalysisWorkflowError(
            "Analysis decision reconsideration ended without an accepted decision."
        )
    if not decision_attempts or decision_attempts[-1].get("status") != "accepted":
        raise AnalysisWorkflowError(
            "Analysis decision reconsideration ended without an accepted decision."
        )
    if decision_draft is None:
        raise AnalysisWorkflowError("Analysis decision stage ended without a decision draft.")
    accepted_assessment_decision = _validated_assessment_decision(
        decision_draft=decision_draft,
    )
    trace["accepted_assessment_decision"] = accepted_assessment_decision
    trace["rejected_expert_roles"] = _rejected_expert_roles(
        assessment_decision=accepted_assessment_decision,
        expert_recommendations=expert_recommendations,
    )
    canonical_payload: dict[str, object] | None = None
    if accepted_assessment_decision == "no_material_assessment":
        canonical_payload = _assemble_canonical_analysis_payload(
            decision_draft=decision_draft,
            assessment_draft=None,
            request=request,
        )
        validation_result = gateway.call_tool(
            "validate_analysis_final_payload", {"payload": canonical_payload}
        )
        trace["final_validation"] = _decode_tool_result(validation_result)
        validation_failure = _canonical_validation_failure(validation_result)
        if validation_failure is not None:
            raise AnalysisWorkflowError(
                "Accepted Analysis no-material decision failed final validation "
                f"(error_code={validation_failure.error_code}): {validation_failure.message}"
            )
        trace["memory_revision_plan"] = {
            "revision_intents": [],
            "reason": (
                "Validated decision found no material AnalysisAssessment; "
                "no durable cognition revision allowed."
            ),
        }
        trace["write_results"] = []
        return _workflow_result(
            config=config,
            payload=canonical_payload,
            inference_results=tuple(inference_results),
            trace=trace,
        )

    visible_source_event_ids = _visible_source_event_ids(request=request)
    baseline_price_level_roles = (
        ()
        if request.previous_assessment is None
        else request.previous_assessment.price_level_roles
    )
    assessment_prompt = _assessment_prompt(
        owner_task=owner_task,
        read_results=read_results,
        evidence_view=evidence_view,
        thesis_view=thesis_view,
        market_view=market_view,
        decision_draft=decision_draft,
        baseline_price_level_roles=baseline_price_level_roles,
        visible_source_event_ids=visible_source_event_ids,
        assessment_contract=analysis_assessment_draft_contract_markdown(
            execution_direction_mode=config.execution_direction_mode
        ),
    )
    assessment_schema = _assessment_schema(
        request=request,
        execution_direction_mode=config.execution_direction_mode,
        visible_source_event_ids=visible_source_event_ids,
    )
    trace["assessment_frozen_context"] = {
        "business_task": owner_task,
        "assessment_prompt": assessment_prompt,
        "accepted_decision": dict(decision_draft),
        "baseline_price_level_ids": [
            level.level_id for level in baseline_price_level_roles
        ],
        "visible_source_event_ids": list(visible_source_event_ids),
    }
    assessment_attempts: list[dict[str, object]] = []
    trace["assessment_attempts"] = assessment_attempts
    assessment_failure_ledger = _RepairViolationLedger.empty()
    trace["assessment_failure_ledger"] = assessment_failure_ledger.snapshot()
    assessment_draft: dict[str, object] | None = None
    for assessment_attempt_index in range(ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS):
        active_assessment_prompt = (
            assessment_prompt
            if assessment_draft is None
            else _assessment_repair_prompt(
                assessment_prompt,
                rejected_assessment_draft=assessment_draft,
                failure_ledger=assessment_failure_ledger.snapshot(),
            )
        )
        _assert_prompt_budget(config, active_assessment_prompt, stage="analysis_assessment")
        assessment_result, assessment_stage_receipt = await _generate_structured_stage(
            trace_receipts=stage_receipts,
            stage="analysis_assessment",
            business_repair_attempt=assessment_attempt_index + 1,
            provider=provider,
            prompt=active_assessment_prompt,
            session_id=f"{request.task_id}:assessment",
            tool_name=_ASSESSMENT_TOOL,
            tool_description="Return the complete model-owned AnalysisAssessment draft.",
            input_schema=assessment_schema,
        )
        inference_results.append(assessment_result)
        assessment_draft = dict(assessment_result.payload)
        candidate_payload = _assemble_canonical_analysis_payload(
            decision_draft=decision_draft,
            assessment_draft=assessment_draft,
            request=request,
        )
        validation_result = gateway.call_tool(
            "validate_analysis_final_payload", {"payload": candidate_payload}
        )
        decoded_validation = _decode_tool_result(validation_result)
        trace["final_validation"] = decoded_validation
        assessment_failure = _canonical_validation_failure(validation_result)
        assessment_attempt: dict[str, object] = {
            "attempt": assessment_attempt_index + 1,
            "assessment_draft": dict(assessment_draft),
            "validation": decoded_validation,
        }
        assessment_attempts.append(assessment_attempt)
        trace["assessment_attempt_count"] = len(assessment_attempts)
        if assessment_failure is None:
            _mark_stage_business_status(assessment_stage_receipt, "accepted")
            assessment_attempt["status"] = "accepted"
            assessment_attempt["failure_ledger"] = assessment_failure_ledger.snapshot()
            canonical_payload = candidate_payload
            break
        rejection = _contract_failure_payload(assessment_failure)
        _mark_stage_business_status(assessment_stage_receipt, "rejected")
        assessment_failure_ledger.record(
            attempt=assessment_attempt_index + 1,
            violation=rejection,
        )
        ledger_snapshot = assessment_failure_ledger.snapshot()
        trace["assessment_failure_ledger"] = ledger_snapshot
        assessment_attempt.update(
            status="rejected",
            rejection=rejection,
            failure_ledger=ledger_snapshot,
        )
        if assessment_attempt_index + 1 >= ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS:
            trace["assessment_repair_exhausted"] = rejection
            raise AnalysisWorkflowError(
                "Analysis assessment contract repair exhausted after "
                f"{ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS} attempts "
                f"(error_code={rejection['error_code']}, "
                f"field_path={rejection['field_path']}): {rejection['message']}"
            )
    if canonical_payload is None:
        raise AnalysisWorkflowError("Analysis assessment loop ended without an accepted payload.")

    revision_plan_prompt = _revision_plan_prompt(
        owner_task=owner_task,
        final_payload=canonical_payload,
        thesis_view=thesis_view,
        memory_topology=memory_topology,
        requires_watchlist_maintenance=request.requires_watchlist_maintenance,
    )
    revision_plan_assessment = _revision_plan_analysis_assessment(
        canonical_payload=canonical_payload,
        request=request,
        config=config,
    )
    revision_plan_attempts: list[dict[str, object]] = []
    trace["revision_plan_attempts"] = revision_plan_attempts
    revision_plan_failure_ledger = _RepairViolationLedger.empty()
    trace["revision_plan_failure_ledger"] = revision_plan_failure_ledger.snapshot()
    revision_plan_draft: dict[str, object] | None = None
    revision_intents: tuple[dict[str, object], ...] | None = None
    for revision_plan_attempt_index in range(ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS):
        active_revision_plan_prompt = (
            revision_plan_prompt
            if revision_plan_draft is None
            else _revision_plan_repair_prompt(
                revision_plan_prompt,
                rejected_revision_plan=revision_plan_draft,
                failure_ledger=revision_plan_failure_ledger.snapshot(),
            )
        )
        _assert_prompt_budget(
            config,
            active_revision_plan_prompt,
            stage="memory_revision_plan",
        )
        revision_plan, revision_plan_receipt = await _generate_structured_stage(
            trace_receipts=stage_receipts,
            stage="memory_revision_plan",
            business_repair_attempt=revision_plan_attempt_index + 1,
            provider=provider,
            prompt=active_revision_plan_prompt,
            session_id=f"{request.task_id}:memory-revision-plan",
            tool_name=_REVISION_PLAN_TOOL,
            tool_description=(
                "Return the bounded plan of ResearchMemory revisions implied by a "
                "validated Analysis assessment."
            ),
            input_schema=_revision_plan_schema(memory_topology=memory_topology),
        )
        inference_results.append(revision_plan)
        revision_plan_draft = dict(revision_plan.payload)
        candidate_intents = _parse_revision_intents(revision_plan.payload)
        _validate_revision_memory_topology(
            intents=candidate_intents,
            memory_topology=memory_topology,
        )
        revision_plan_failure = analysis_revision_plan_contract_failure(
            target_key=request.tool_context.target_key,
            requires_watchlist_maintenance=request.requires_watchlist_maintenance,
            analysis_assessment=revision_plan_assessment,
            revision_intents=candidate_intents,
        )
        revision_plan_attempt: dict[str, object] = {
            "attempt": revision_plan_attempt_index + 1,
            "revision_plan": revision_plan_draft,
        }
        revision_plan_attempts.append(revision_plan_attempt)
        trace["revision_plan_attempt_count"] = len(revision_plan_attempts)
        if revision_plan_failure is None:
            _mark_stage_business_status(revision_plan_receipt, "accepted")
            revision_plan_attempt["status"] = "accepted"
            revision_plan_attempt["failure_ledger"] = (
                revision_plan_failure_ledger.snapshot()
            )
            revision_intents = candidate_intents
            trace["memory_revision_plan"] = revision_plan_draft
            break
        rejection = _revision_plan_contract_failure_payload(revision_plan_failure)
        _mark_stage_business_status(revision_plan_receipt, "rejected")
        revision_plan_failure_ledger.record(
            attempt=revision_plan_attempt_index + 1,
            violation=rejection,
        )
        ledger_snapshot = revision_plan_failure_ledger.snapshot()
        trace["revision_plan_failure_ledger"] = ledger_snapshot
        revision_plan_attempt.update(
            status="rejected",
            rejection=rejection,
            failure_ledger=ledger_snapshot,
        )
        if revision_plan_attempt_index + 1 >= ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS:
            exhausted = rejection
            trace["revision_plan_repair_exhausted"] = exhausted
            raise AnalysisWorkflowError(
                "Analysis revision plan contract repair exhausted after "
                f"{ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS} attempts "
                f"(error_code={exhausted['error_code']}): {exhausted['message']}"
            )
    if revision_intents is None:
        raise AnalysisWorkflowError(
            "Analysis revision plan loop ended without an accepted plan."
        )

    initial_writer_specs: list[_RevisionWriterInitialSpec] = []
    for index, intent in enumerate(revision_intents):
        revision_prompt = _revision_writer_prompt(
            owner_task=owner_task,
            final_payload=canonical_payload,
            read_results=read_results,
            evidence_view=evidence_view,
            thesis_view=thesis_view,
            market_view=market_view,
            intent=intent,
        )
        _assert_prompt_budget(
            config,
            revision_prompt,
            stage="memory_revision_writer",
        )
        initial_writer_specs.append(
            _RevisionWriterInitialSpec(
                index=index,
                intent=intent,
                prompt=revision_prompt,
                schema=_revision_writer_schema(intent),
                session_id=f"{request.task_id}:memory-revision:{index}",
            )
        )

    initial_writer_results = await _generate_initial_revision_writer_drafts(
        provider=provider,
        specs=tuple(initial_writer_specs),
        trace_receipts=stage_receipts,
    )
    inference_results.extend(result for result, _ in initial_writer_results)

    write_results: list[dict[str, object]] = []
    revision_writer_attempts: list[dict[str, object]] = []
    trace["revision_writer_attempts"] = revision_writer_attempts
    for spec, (initial_revision, initial_receipt) in zip(
        initial_writer_specs,
        initial_writer_results,
        strict=True,
    ):
        index = spec.index
        intent = dict(spec.intent)
        revision_prompt = spec.prompt
        revision_schema = spec.schema
        writer_draft: dict[str, object] = dict(initial_revision.payload)
        writer_failure_ledger = _RepairViolationLedger.empty()
        for writer_attempt_index in range(ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS):
            if writer_attempt_index == 0:
                revision = initial_revision
                revision_stage_receipt = initial_receipt
            else:
                active_revision_prompt = _revision_writer_repair_prompt(
                    revision_prompt,
                    rejected_writer_payload=writer_draft,
                    failure_ledger=writer_failure_ledger.snapshot(),
                )
                _assert_prompt_budget(
                    config,
                    active_revision_prompt,
                    stage="memory_revision_writer",
                )
                revision, revision_stage_receipt = await _generate_structured_stage(
                    trace_receipts=stage_receipts,
                    stage="memory_revision_writer",
                    business_repair_attempt=writer_attempt_index + 1,
                    provider=provider,
                    prompt=active_revision_prompt,
                    session_id=spec.session_id,
                    tool_name=_REVISION_WRITER_TOOL,
                    tool_description=(
                        "Return the body for exactly one project-selected ResearchMemory "
                        "revision."
                    ),
                    input_schema=revision_schema,
                )
                inference_results.append(revision)
            writer_draft = dict(revision.payload)
            write_call = _revision_write_call(intent=intent, payload=revision.payload)
            write_result, writer_repair_failure = _execute_write_call(
                gateway=gateway,
                call=write_call,
            )
            writer_attempt: dict[str, object] = {
                "intent_index": index,
                "attempt": writer_attempt_index + 1,
                "operation": intent["operation"],
                "page_path": intent["page_path"],
                "section_name": intent.get("section_name"),
                "writer_payload": writer_draft,
            }
            revision_writer_attempts.append(writer_attempt)
            if writer_repair_failure is None:
                _mark_stage_business_status(revision_stage_receipt, "accepted")
                writer_attempt["status"] = "accepted"
                writer_attempt["failure_ledger"] = writer_failure_ledger.snapshot()
                if write_result is None:
                    raise AnalysisWorkflowError(
                        "Analysis revision writer succeeded without a write result."
                    )
                write_results.append(write_result)
                break
            rejection = _revision_writer_failure_payload(writer_repair_failure)
            _mark_stage_business_status(revision_stage_receipt, "rejected")
            writer_failure_ledger.record(
                attempt=writer_attempt_index + 1,
                violation=rejection,
            )
            ledger_snapshot = writer_failure_ledger.snapshot()
            writer_attempt.update(
                status="rejected",
                rejection=rejection,
                failure_ledger=ledger_snapshot,
            )
            if writer_attempt_index + 1 >= ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS:
                exhausted = {"intent_index": index, **rejection}
                exhausted["failure_ledger"] = ledger_snapshot
                trace["revision_writer_repair_exhausted"] = exhausted
                raise AnalysisWorkflowError(
                    "Analysis revision writer contract repair exhausted after "
                    f"{ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS} attempts "
                    f"(tool_name={exhausted['tool_name']}, "
                    f"error_code={exhausted['error_code']}): {exhausted['message']}"
                )
    trace["write_results"] = write_results

    return _workflow_result(
        config=config,
        payload=canonical_payload,
        inference_results=tuple(inference_results),
        trace=trace,
    )


def build_analysis_workflow_executor(
    *,
    config: AnalysisWorkflowConfig,
) -> AnalysisAgentExecutor:
    """Build the production provider binding for the project-owned workflow."""

    if not isinstance(config, AnalysisWorkflowConfig):
        raise AnalysisWorkflowError("config must be an AnalysisWorkflowConfig instance.")
    return AnalysisWorkflowExecutor(
        config=config,
        provider=AnthropicStructuredInference(
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
            )
        ),
    )


def _validate_gateway_contract(
    *,
    request: AnalysisAgentAttemptRequest,
) -> None:
    available = set(ANALYSIS_TOOL_NAMES)
    missing = tuple(name for name in request.contract.required_tool_names if name not in available)
    if missing:
        raise AnalysisWorkflowError(
            "Analysis workflow canonical tool preflight failed; missing tool(s): "
            + ", ".join(missing)
        )


def _memory_topology(*, gateway: AnalysisToolGateway) -> _MemoryTopology:
    context = gateway.context
    citations = (*context.active_citations, *context.context_citations)
    readable_citation_targets: list[tuple[str, str]] = []
    readable_pages, readable_sections = _page_topology(
        reader=context.reader,
        target_key=context.target_key,
        citations=citations,
        citation_targets=readable_citation_targets,
    )
    operator_page = context.operator_context
    operator_sections = _page_sections(
        page_path=operator_page.page_path,
        content_md=operator_page.content_md,
        writable=False,
    )
    readable_pages = tuple(sorted({*readable_pages, operator_page.page_path}))
    readable_sections = tuple(sorted({*readable_sections, *operator_sections}))
    readable_citation_targets.extend(
        _citation_targets_for_page(
            page_path=operator_page.page_path,
            content_md=operator_page.content_md,
            citations=citations,
        )
    )

    writable_reader = FileBackedResearchMemoryReader(context.stage_layout)
    writable_pages, writable_sections = _page_topology(
        reader=writable_reader,
        target_key=context.target_key,
        writable=True,
    )
    return _MemoryTopology(
        readable_pages=readable_pages,
        readable_section_targets=readable_sections,
        readable_citation_targets=tuple(sorted(set(readable_citation_targets))),
        writable_pages=writable_pages,
        writable_section_targets=writable_sections,
    )


def _citation_targets_for_page(
    *,
    page_path: str,
    content_md: str,
    citations: tuple[object, ...],
) -> tuple[tuple[str, str], ...]:
    targets: list[tuple[str, str]] = []
    for citation in citations:
        event_id = getattr(citation, "event_id", None)
        source_ref = getattr(citation, "source_ref", None)
        if not isinstance(event_id, str) or not isinstance(source_ref, str):
            continue
        if build_citation_neighborhood(
            content_md,
            event_id=event_id,
            source_ref=source_ref,
        ) is not None:
            targets.append((page_path, event_id))
    return tuple(targets)


def _page_topology(
    *,
    reader: object,
    target_key: str,
    writable: bool = False,
    citations: tuple[object, ...] = (),
    citation_targets: list[tuple[str, str]] | None = None,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    list_pages = getattr(reader, "list_pages", None)
    read_page = getattr(reader, "read_page", None)
    if not callable(list_pages) or not callable(read_page):
        raise AnalysisWorkflowError("ResearchMemory reader must expose list_pages and read_page.")
    page_paths: list[str] = []
    section_targets: list[tuple[str, str]] = []
    for page_ref in list_pages(f"target:{target_key}"):
        page_path = getattr(page_ref, "page_path", None)
        if not isinstance(page_path, str):
            raise AnalysisWorkflowError(
                "ResearchMemory page listing returned an invalid page path."
            )
        resolved_ref = resolve_page_ref(page_path)
        if resolved_ref.target_key != target_key:
            continue
        if writable and resolved_ref.page_kind in {"operator", "index", "log"}:
            continue
        page = read_page(page_path)
        content_md = getattr(page, "content_md", None)
        if not isinstance(content_md, str):
            raise AnalysisWorkflowError(
                f"ResearchMemory page {page_path!r} returned non-text content."
            )
        page_paths.append(page_path)
        if citation_targets is not None and citations:
            citation_targets.extend(
                _citation_targets_for_page(
                    page_path=page_path,
                    content_md=content_md,
                    citations=citations,
                )
            )
        section_targets.extend(
            _page_sections(
                page_path=page_path,
                content_md=content_md,
                writable=writable,
            )
        )
    return tuple(sorted(set(page_paths))), tuple(sorted(set(section_targets)))


def _page_sections(
    *,
    page_path: str,
    content_md: str,
    writable: bool,
) -> tuple[tuple[str, str], ...]:
    try:
        sections = build_markdown_page_map(content_md)
    except MarkdownContextError as exc:
        raise AnalysisWorkflowError(
            f"Could not build ResearchMemory topology for {page_path!r}: {exc}"
        ) from exc
    page_ref = resolve_page_ref(page_path)
    targets: list[tuple[str, str]] = []
    for section in sections:
        section_name = section.get("heading")
        heading_level = section.get("level")
        if not isinstance(section_name, str) or not isinstance(heading_level, int):
            raise AnalysisWorkflowError(
                f"ResearchMemory topology for {page_path!r} contains an invalid heading."
            )
        if not writable:
            targets.append((page_path, section_name))
            continue
        if heading_level == 1:
            continue
        if (
            page_ref.page_kind in {"thesis", "timeline", "risks", "watchlist"}
            and heading_level == 2
            and section_name
            not in canonical_thesis_page_sections(page_path.rsplit("/", maxsplit=1)[-1])
        ):
            continue
        targets.append((page_path, section_name))
    return tuple(targets)


def _validate_read_memory_topology(
    *,
    calls: tuple[tuple[str, dict[str, object]], ...],
    memory_topology: _MemoryTopology,
) -> None:
    for tool_name, arguments in calls:
        page_path = arguments.get("page_path")
        if tool_name in {"read_page", "read_around_citation"}:
            if page_path not in memory_topology.readable_pages:
                _raise_memory_topology_invariant(
                    error_code="analysis_memory_page_not_readable",
                    message=f"{tool_name} selected a page outside the readable topology.",
                    arguments=arguments,
                )
            if tool_name == "read_around_citation" and (
                page_path,
                arguments.get("event_id"),
            ) not in memory_topology.readable_citation_targets:
                _raise_memory_topology_invariant(
                    error_code="analysis_memory_citation_target_not_readable",
                    message=(
                        "read_around_citation selected a page/event pair outside the "
                        "readable citation topology."
                    ),
                    arguments=arguments,
                )
            continue
        if tool_name != "read_section":
            continue
        section_name = arguments.get("section_name")
        if (page_path, section_name) not in memory_topology.readable_section_targets:
            _raise_memory_topology_invariant(
                error_code="analysis_memory_section_not_readable",
                message="read_section selected a page/section pair outside the readable topology.",
                arguments=arguments,
            )


def _validate_revision_memory_topology(
    *,
    intents: tuple[dict[str, object], ...],
    memory_topology: _MemoryTopology,
) -> None:
    for intent in intents:
        operation = intent["operation"]
        page_path = intent["page_path"]
        if operation == "rewrite_page" and page_path not in memory_topology.writable_pages:
            _raise_memory_topology_invariant(
                error_code="analysis_memory_page_not_writable",
                message="rewrite_page selected a page outside the writable topology.",
                arguments=intent,
            )
        if operation == "update_page_section" and (
            page_path,
            intent.get("section_name"),
        ) not in memory_topology.writable_section_targets:
            _raise_memory_topology_invariant(
                error_code="analysis_memory_section_not_writable",
                message=(
                    "update_page_section selected a page/section pair outside the "
                    "writable topology."
                ),
                arguments=intent,
            )


def _raise_memory_topology_invariant(
    *,
    error_code: str,
    message: str,
    arguments: Mapping[str, object],
) -> None:
    arguments_text = _json_text(dict(arguments))
    if len(arguments_text) > 600:
        arguments_text = f"{arguments_text[:597]}..."
    raise AnalysisWorkflowError(
        "Analysis memory topology invariant failed "
        f"(error_code={error_code}): {message} arguments={arguments_text}"
    )


def _execute_read_calls(
    *,
    gateway: AnalysisToolGateway,
    calls: tuple[tuple[str, dict[str, object]], ...],
    trace_attempt: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for tool_name, arguments in calls:
        raw_result = gateway.call_tool(tool_name, dict(arguments))
        if trace_attempt is not None:
            executed = trace_attempt.setdefault("executed_read_calls", [])
            if isinstance(executed, list):
                executed.append({"tool_name": tool_name, "arguments": arguments})
        results.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "result": _decode_tool_result(raw_result),
            }
        )
    return results


def _load_workflow_activity_receipts(receipt_path: Path) -> tuple[dict[str, object], ...]:
    if not receipt_path.exists():
        return ()
    receipts: list[dict[str, object]] = []
    for line in receipt_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            receipt = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnalysisWorkflowError(
                "Analysis activity receipt log contains invalid JSON."
            ) from exc
        if not isinstance(receipt, dict):
            raise AnalysisWorkflowError("Analysis activity receipt must be an object.")
        receipts.append(receipt)
    return tuple(receipts)


def _visible_source_event_ids(*, request: AnalysisAgentAttemptRequest) -> tuple[str, ...]:
    """Return only admitted evidence IDs that this frozen attempt can support."""

    citations = (
        *request.tool_context.active_citations,
        *request.tool_context.context_citations,
    )
    event_ids = {citation.event_id for citation in citations}
    for receipt in _load_workflow_activity_receipts(request.tool_context.receipt_path):
        if receipt.get("result_mode") == "duplicate":
            continue
        operation = receipt.get("operation")
        if operation == "read_evidence":
            records = receipt.get("records")
            if not isinstance(records, list):
                raise AnalysisWorkflowError("read_evidence receipt records must be a list.")
            for record in records:
                if isinstance(record, Mapping) and isinstance(record.get("event_id"), str):
                    event_ids.add(record["event_id"])
            continue
        if operation in {"read_page", "read_section", "read_around_citation"}:
            content_md = receipt.get("content_md")
            if not isinstance(content_md, str):
                raise AnalysisWorkflowError(f"{operation} receipt content_md must be a string.")
            event_ids.update(
                citation.event_id for citation in extract_analysis_citations(content_md)
            )
            continue
        if operation == "search_wiki":
            matches = receipt.get("matches")
            if not isinstance(matches, list):
                raise AnalysisWorkflowError("search_wiki receipt matches must be a list.")
            for match in matches:
                if isinstance(match, Mapping) and isinstance(match.get("snippet"), str):
                    event_ids.update(
                        citation.event_id
                        for citation in extract_analysis_citations(match["snippet"])
                    )
    return tuple(sorted(event_ids))


def _price_level_baseline_summary(
    levels: tuple[PriceLevelRole, ...],
) -> list[dict[str, object]]:
    """Expose decision-relevant level facts without asking the model to re-own provenance."""

    return [
        {
            "baseline_level_id": level.level_id,
            "level": (
                {"kind": "scalar", "price": level.value}
                if level.value is not None
                else {"kind": "zone", "lower": level.lower, "upper": level.upper}
            ),
            "instrument_basis": level.instrument_basis,
            "source_type": level.source_type,
            "role_if_flat": level.role_if_flat,
            "role_if_already_long": level.role_if_already_long,
            "role_if_already_short": level.role_if_already_short,
            "path_context_required": level.path_context_required,
            "refresh_triggers": list(level.refresh_triggers),
            "invalidation_triggers": list(level.invalidation_triggers),
            "confidence": level.confidence,
            "rationale_md": level.rationale_md,
        }
        for level in levels
    ]


def _coverage_receipt_payload(receipt: object) -> dict[str, object]:
    fields = (
        "evidence_grounding",
        "memory_grounding",
        "market_grounding",
        "compiled_evidence_event_ids",
        "tool_read_evidence_event_ids",
        "compiled_memory_cards",
        "compiler_memory_read_receipt_ids",
        "tool_read_memory_refs",
        "compiled_market_grounding_count",
        "tool_read_market_refs",
        "failed_reasons",
    )
    return {
        field_name: getattr(receipt, field_name)
        for field_name in fields
        if hasattr(receipt, field_name)
    }


def _execute_write_call(
    *,
    gateway: AnalysisToolGateway,
    call: tuple[str, dict[str, object]],
) -> tuple[dict[str, object] | None, AnalysisWriteToolFailure | None]:
    tool_name, arguments = call
    raw_result = gateway.call_tool(tool_name, dict(arguments))
    failure = parse_analysis_write_tool_failure(
        tool_name=tool_name,
        arguments=arguments,
        tool_result=raw_result,
    )
    if failure is not None:
        if not failure.recoverable:
            raise AnalysisWriteFailureBudgetExceeded(
                "Non-recoverable analysis write tool failure: "
                f"tool={failure.tool_name} error_code={failure.error_code}: "
                f"{failure.message}"
            )
        return None, failure
    return (
        {
            "tool_name": tool_name,
            "arguments": arguments,
            "result": _decode_tool_result(raw_result),
        },
        None,
    )


def _canonical_validation_failure(
    validation_result: str,
) -> AnalysisContractFailure | None:
    decoded = _decode_tool_result(validation_result)
    if not isinstance(decoded, Mapping):
        raise AnalysisWorkflowError("validate_analysis_final_payload returned a non-object result.")
    if decoded.get("ok") is True:
        return None
    error_code = decoded.get("error_code")
    message = decoded.get("message")
    field_path = decoded.get("field_path")
    return AnalysisContractFailure(
        surface="final_output",
        error_code=(
            error_code
            if isinstance(error_code, str) and error_code.strip()
            else "analysis_final_payload_invalid"
        ),
        message=(
            message
            if isinstance(message, str) and message.strip()
            else "Analysis final payload failed deterministic validation."
        ),
        details={
            "field_path": field_path if isinstance(field_path, str) else "",
        },
    )


def _validated_assessment_decision(*, decision_draft: Mapping[str, object]) -> str:
    decision = decision_draft.get("assessment_decision")
    if decision not in _ASSESSMENT_DECISIONS:
        raise AnalysisWorkflowError(
            "Analysis structured provider returned an invalid assessment_decision."
        )
    return str(decision)


def _validated_expert_recommendations(
    expert_views: Mapping[str, Mapping[str, object]],
) -> dict[str, str]:
    recommendations: dict[str, str] = {}
    for role, view in expert_views.items():
        recommendation = view.get("assessment_recommendation")
        if recommendation not in _EXPERT_RECOMMENDATIONS:
            raise AnalysisWorkflowError(
                f"Analysis structured provider returned an invalid recommendation "
                f"for expert {role!r}."
            )
        recommendations[role] = str(recommendation)
    return recommendations


def _contested_no_material_roles(
    *,
    decision_draft: Mapping[str, object],
    expert_recommendations: Mapping[str, str],
) -> list[str]:
    if _validated_assessment_decision(decision_draft=decision_draft) != (
        "no_material_assessment"
    ):
        return []
    contested_roles = sorted(
        role
        for role, recommendation in expert_recommendations.items()
        if recommendation == "emit_assessment"
    )
    return contested_roles if len(contested_roles) >= 2 else []


def _rejected_expert_roles(
    *,
    assessment_decision: str,
    expert_recommendations: Mapping[str, str],
) -> list[str]:
    opposite_recommendation = (
        "no_material_assessment"
        if assessment_decision == "emit_assessment"
        else "emit_assessment"
    )
    return sorted(
        role
        for role, recommendation in expert_recommendations.items()
        if recommendation == opposite_recommendation
    )


def _market_expert_required(
    *,
    request: AnalysisAgentAttemptRequest,
    read_calls: tuple[tuple[str, dict[str, object]], ...],
) -> bool:
    if any(tool_name in _MARKET_READ_TOOL_NAMES for tool_name, _ in read_calls):
        return True
    packet = request.tool_context.context_packet
    if packet is None or packet.analysis_workbench is None:
        return False
    workbench = packet.analysis_workbench
    return (
        workbench.market_lane.available
        or workbench.market_lane.recap_reconciliation.required
        or workbench.market_path_lane.available
    )


def _workflow_business_task_prompt(request: AnalysisAgentAttemptRequest) -> str:
    business_brief = request.business_task.business_brief.rstrip()
    if request.business_task.repair_failure is not None:
        raise AnalysisWorkflowError(
            "stage-local Analysis workflow must not receive a supervisor repair failure."
        )
    return business_brief


def _read_plan_prompt(
    owner_task: str,
    *,
    memory_topology: _MemoryTopology,
    max_read_calls: int,
    target_key: str,
    tradable_proxy_symbol: str | None,
) -> str:
    market_identity = (
        f"- Business target_key for every market tool call: {target_key}. The tradable "
        f"proxy symbol {tradable_proxy_symbol} is a distinct instrument identity; never "
        "pass it as target_key. Market terminals resolve the configured proxy internally.\n"
        if tradable_proxy_symbol is not None
        else f"- Business target_key for every market tool call: {target_key}.\n"
    )
    return (
        f"{owner_task.rstrip()}\n\n"
        "Bounded Analysis read-planning stage:\n"
        "- Plan reads only; do not decide, write ResearchMemory, or draft final JSON.\n"
        "- The Analysis Workbench is already visible. Do not repeat reads satisfied by "
        "complete compiler receipts unless exact omitted detail, contradiction, citation "
        "context, or write support is needed.\n"
        "- Read active evidence when compiled excerpts are missing/truncated/ambiguous.\n"
        "- Inspect target ResearchMemory enough to compare and safely write.\n"
        "- The following is the project-derived readable ResearchMemory topology. Use an "
        "exact listed page/section pair for read_section; never infer a pairing from "
        "section names alone:\n"
        f"{_json_text(memory_topology.readable_payload(), pretty=True)}\n"
        "- Use market reads only for decision-time facts not adequately visible in the "
        "Market Lane. Never request future data.\n"
        f"{market_identity}"
        f"- This attempt has {max_read_calls} remaining read-call slots. Return at most "
        f"{max_read_calls} ordered canonical read calls."
    )


def _read_plan_repair_prompt(
    *,
    base_read_plan_prompt: str,
    prior_read_plan: Mapping[str, object] | None,
    prior_read_results: list[dict[str, object]],
    failure: AnalysisContractFailure,
    max_read_calls: int,
) -> str:
    """Request only the deterministic grounding gap; no downstream stage reruns."""

    return (
        f"{base_read_plan_prompt.rstrip()}\n\n"
        "Deterministic read-plan coverage repair:\n"
        f"- error_code: {failure.error_code}\n"
        f"- error: {failure.message}\n"
        f"- details: {_json_text(dict(failure.details or {}), pretty=True)}\n"
        f"- Previous read plan: {_json_text(dict(prior_read_plan or {}), pretty=True)}\n"
        f"- Project-executed read results so far: {_json_text(prior_read_results, pretty=True)}\n"
        f"- Remaining read-call slots in this attempt: {max_read_calls}.\n"
        "Return only additional canonical read calls that close the stated grounding "
        "gap. Do not repeat completed reads, plan experts, make a decision, or write."
    )


def _expert_prompt(
    *,
    owner_task: str,
    read_results: str,
    role: str,
    instructions: str,
) -> str:
    return (
        f"{owner_task.rstrip()}\n\n"
        f"Project-executed canonical read results:\n{read_results}\n\n"
        f"You are the temporary stateless {role} expert. {instructions}\n"
        "Return only the required structured expert view. Set "
        "assessment_recommendation to emit_assessment when the visible evidence is "
        "sufficient for a material AnalysisAssessment, no_material_assessment when it "
        "is not, or uncertain when this expert view cannot decide independently; give "
        "a non-blank rationale. Another single Analysis "
        "owner will see the original task, every read result, and every expert view; "
        "your output is advisory and has no write or commit authority."
    )


def _decision_prompt(
    *,
    owner_task: str,
    read_results: list[dict[str, object]],
    evidence_view: Mapping[str, object],
    thesis_view: Mapping[str, object],
    market_view: Mapping[str, object] | None,
    decision_contract: str,
) -> str:
    owner_context = {
        "project_executed_read_results": read_results,
        "advisory_expert_views": {
            "evidence_causality": dict(evidence_view),
            "thesis_memory": dict(thesis_view),
            "market_price": (
                dict(market_view)
                if market_view is not None
                else {"status": "not_required_by_deterministic_workflow"}
            ),
        },
    }
    return (
        f"{owner_task.rstrip()}\n\n"
        "Bounded Analysis decision context:\n"
        f"{_json_text(owner_context, pretty=True)}\n\n"
        "Decision owner instructions:\n"
        "- You are the single Analysis business owner. The full original task and raw "
        "ContextPacket above remain authoritative; expert views are advisory.\n"
        "- Do not call any read, write, or validation tool from this owner stage. Return "
        "exactly one analysis_decision object matching the supplied schema.\n"
        "- The analysis_decision tool input root contains exactly the model-owned fields "
        "used_lesson_ids, assessment_decision, and assessment_decision_rationale_md. "
        "Return that object directly, never a serialized JSON string.\n"
        "- Make the Assessment materiality decision explicitly. Experts do not vote and "
        "cannot compel the result. The decision rationale must directly reconcile any "
        "definite advisory recommendation that the owner rejects.\n"
        "- Resolve contradictions yourself. Keep evidence facts, market facts, "
        "interpretation, memory implications and PMReview reasons distinct.\n"
        "- Remain exposure-blind. Do not output actions, target weights, actual exposure, "
        "view_state_change, or decision_audit.\n"
        "- Do not draft an AnalysisAssessment or select ResearchMemory writes. After an "
        "Assessment passes deterministic validation, a separate bounded revision workflow will "
        "derive and validate the necessary ResearchMemory revisions.\n"
        "- Any rejected decision must be regenerated coherently from this same frozen "
        "context using the exact deterministic validation feedback.\n\n"
        "The following decision contract is authoritative for this stage:\n"
        f"{decision_contract.rstrip()}"
    )


def _decision_reconsideration_prompt(
    decision_prompt: str,
    *,
    initial_decision_draft: Mapping[str, object],
    contested_expert_roles: list[str],
) -> str:
    return (
        f"{decision_prompt.rstrip()}\n\n"
        "One-time contested no-material reconsideration:\n"
        "Initial schema-valid decision:\n"
        f"{_json_text(dict(initial_decision_draft), pretty=True)}\n\n"
        "Advisory experts recommending emit_assessment:\n"
        f"{_json_text(contested_expert_roles, pretty=True)}\n\n"
        "Reconsideration instructions:\n"
        "- This is a single deliberation checkpoint, not contract repair and not an "
        "expert vote.\n"
        "- Return one fresh, complete analysis_decision from the frozen context.\n"
        "- You may retain no_material_assessment or switch to emit_assessment. In either "
        "case, the rationale must directly reconcile the contrary expert advice.\n"
        "- Do not return a patch or fields outside the decision schema."
    )


def _assessment_prompt(
    *,
    owner_task: str,
    read_results: list[dict[str, object]],
    evidence_view: Mapping[str, object],
    thesis_view: Mapping[str, object],
    market_view: Mapping[str, object] | None,
    decision_draft: Mapping[str, object],
    baseline_price_level_roles: tuple[PriceLevelRole, ...],
    visible_source_event_ids: tuple[str, ...],
    assessment_contract: str,
) -> str:
    assessment_context = {
        "project_executed_read_results": read_results,
        "advisory_expert_views": {
            "evidence_causality": dict(evidence_view),
            "thesis_memory": dict(thesis_view),
            "market_price": (
                dict(market_view)
                if market_view is not None
                else {"status": "not_required_by_deterministic_workflow"}
            ),
        },
        "accepted_decision": dict(decision_draft),
        "runtime_price_level_baseline": _price_level_baseline_summary(
            baseline_price_level_roles
        ),
        "visible_admitted_source_event_ids": list(visible_source_event_ids),
    }
    return (
        f"{owner_task.rstrip()}\n\n"
        "Bounded Analysis assessment-synthesis context:\n"
        f"{_json_text(assessment_context, pretty=True)}\n\n"
        "Assessment synthesizer instructions:\n"
        "- The accepted materiality decision above is binding. Synthesize exactly one full "
        "AnalysisAssessment cognition draft from the same frozen task, reads, and expert views.\n"
        "- Do not call read, write, or validation tools. Do not emit runtime-owned fields, "
        "actions, target weights, actual exposure, view_state_change, or decision_audit.\n"
        "- Do not select or return ResearchMemory writes. A later deterministic workflow "
        "validates and derives revisions only after this Assessment is accepted.\n"
        "- For each runtime baseline price level, return exactly one retain, retire, or "
        "replace action. Retain means the complete canonical level and its provenance "
        "are preserved by runtime; do not recreate it. New or replacement levels may "
        "cite only visible_admitted_source_event_ids.\n"
        "- Resolve contradictions yourself and preserve the business boundary between facts, "
        "interpretation, memory implications, and PMReview reasons.\n"
        "- Return exactly one analysis_assessment_synthesis object matching the supplied "
        "schema, never a serialized JSON string.\n\n"
        "The following Assessment draft contract is authoritative for this stage:\n"
        f"{assessment_contract.rstrip()}"
    )


def _assessment_repair_prompt(
    assessment_prompt: str,
    *,
    rejected_assessment_draft: Mapping[str, object],
    failure_ledger: list[dict[str, object]],
) -> str:
    return (
        f"{assessment_prompt.rstrip()}\n\n"
        "Current rejected Assessment draft:\n"
        f"{_json_text(dict(rejected_assessment_draft), pretty=True)}\n\n"
        "Cumulative deterministic Assessment validation ledger:\n"
        f"{_json_text(failure_ledger, pretty=True)}\n\n"
        "Repair instructions:\n"
        "- Return one complete, coherent replacement analysis_assessment_synthesis, never "
        "a JSON patch.\n"
        "- Start from the rejected Assessment draft, correct every unresolved ledger "
        "violation, and do not reintroduce a previously observed violation.\n"
        "- The accepted decision, frozen context, and Assessment schema remain authoritative."
    )


def _contract_failure_payload(
    failure: AnalysisContractFailure,
) -> dict[str, object]:
    details = failure.details or {}
    field_path = details.get("field_path")
    return {
        "error_code": failure.error_code,
        "field_path": field_path if isinstance(field_path, str) else "",
        "message": failure.message,
    }


def _revision_plan_prompt(
    *,
    owner_task: str,
    final_payload: Mapping[str, object],
    thesis_view: Mapping[str, object],
    memory_topology: _MemoryTopology,
    requires_watchlist_maintenance: bool,
) -> str:
    context = {
        "validated_analysis_payload": dict(final_payload),
        "thesis_memory_advisory_view": dict(thesis_view),
        "project_derived_writable_memory_topology": memory_topology.writable_payload(),
        "requires_watchlist_maintenance": requires_watchlist_maintenance,
    }
    return (
        f"{owner_task.rstrip()}\n\n"
        "Validated Analysis revision-planning context:\n"
        f"{_json_text(context, pretty=True)}\n\n"
        "ResearchMemory revision-planning instructions:\n"
        "- The Analysis payload above has already passed deterministic final validation. "
        "Plan only durable ResearchMemory changes implied by it.\n"
        "- If analysis_assessment is null, revision_intents must be empty.\n"
        "- When requires_watchlist_maintenance is true and any durable revision is "
        "planned, at least one intent must target the active target watchlist page.\n"
        "- Return at most one intent per exact operation/page/section target. An empty "
        "plan is required when no durable cognition changed.\n"
        "- Preserve fixed-page skeletons. Do not target runtime-owned deterministic "
        "projection sections, including Market Setup Dashboard.\n"
        "- For update_page_section, use only an exact listed page/section pair from the "
        "project-derived writable topology. Never infer a pairing from section names alone.\n"
        "- Select update_page_section whenever a section-scoped revision is sufficient. "
        "Use rewrite_page only when the page structure is materially wrong or incoherent; "
        "use create_page only for a new durable research object.\n"
        "- Do not draft markdown in this stage and do not produce an Assessment, PM "
        "decision, exposure action, or tool call."
    )


def _revision_plan_repair_prompt(
    revision_plan_prompt: str,
    *,
    rejected_revision_plan: Mapping[str, object],
    failure_ledger: list[dict[str, object]],
) -> str:
    return (
        f"{revision_plan_prompt.rstrip()}\n\n"
        "Current rejected revision plan:\n"
        f"{_json_text(dict(rejected_revision_plan), pretty=True)}\n\n"
        "Cumulative deterministic revision plan failure ledger:\n"
        f"{_json_text(failure_ledger, pretty=True)}\n\n"
        "Repair instructions:\n"
        "- Return one complete, coherent replacement revision plan, never a patch.\n"
        "- Keep the accepted owner Assessment and frozen context unchanged.\n"
        "- Correct every unresolved ledger violation without reintroducing a previous "
        "violation. No writer has executed, so do not compensate for partial writes."
    )


def _revision_plan_contract_failure_payload(
    failure: AnalysisContractFailure,
) -> dict[str, object]:
    return {
        "error_code": failure.error_code,
        "message": failure.message,
        "suggested_action": failure.suggested_action,
        "details": dict(failure.details or {}),
    }


def _revision_writer_prompt(
    *,
    owner_task: str,
    final_payload: Mapping[str, object],
    read_results: list[dict[str, object]],
    evidence_view: Mapping[str, object],
    thesis_view: Mapping[str, object],
    market_view: Mapping[str, object] | None,
    intent: Mapping[str, object],
) -> str:
    context = {
        "validated_analysis_payload": dict(final_payload),
        "project_executed_read_results": read_results,
        "advisory_expert_views": {
            "evidence_causality": dict(evidence_view),
            "thesis_memory": dict(thesis_view),
            "market_price": (
                dict(market_view)
                if market_view is not None
                else {"status": "not_required_by_deterministic_workflow"}
            ),
        },
        "project_selected_revision_intent": dict(intent),
    }
    operation = intent["operation"]
    page_path = intent["page_path"]
    section_name = intent.get("section_name")
    surface = (
        f"section {section_name!r} of {page_path!r}"
        if operation == "update_page_section"
        else f"page {page_path!r}"
    )
    return (
        f"{owner_task.rstrip()}\n\n"
        "Validated Analysis revision-writing context:\n"
        f"{_json_text(context, pretty=True)}\n\n"
        "ResearchMemory revision-writing instructions:\n"
        f"- Produce content only for the project-selected {surface}.\n"
        "- Do not change the target, operation, page structure, or any other page.\n"
        "- Preserve all evidence, grounding, citation, claim, fixed-page, and "
        "Market Setup Dashboard restrictions in the original task.\n"
        "- For update_page_section, return body-only markdown: no page title, no selected "
        "section heading, and no other required top-level heading.\n"
        "- Do not return an Analysis payload, PM decision, exposure action, tool call, or "
        "a serialized JSON string."
    )


def _revision_writer_repair_prompt(
    revision_prompt: str,
    *,
    rejected_writer_payload: Mapping[str, object],
    failure_ledger: list[dict[str, object]],
) -> str:
    return (
        f"{revision_prompt.rstrip()}\n\n"
        "Current rejected revision writer payload:\n"
        f"{_json_text(dict(rejected_writer_payload), pretty=True)}\n\n"
        "Cumulative deterministic revision writer failure ledger:\n"
        f"{_json_text(failure_ledger, pretty=True)}\n\n"
        "Repair instructions:\n"
        "- Return one complete, coherent replacement writer payload, never a patch.\n"
        "- Preserve the project-selected revision target and correct every unresolved "
        "ledger violation without reintroducing a previously observed violation."
    )


def _revision_writer_failure_payload(
    failure: AnalysisWriteToolFailure,
) -> dict[str, object]:
    failure_payload = failure.payload or {}
    suggested_action = failure_payload.get("suggested_action")
    feedback: dict[str, object] = {
        "tool_name": failure.tool_name,
        "error_code": failure.error_code,
        "message": failure.message,
        "suggested_action": (
            suggested_action if isinstance(suggested_action, str) else ""
        ),
    }
    for field_name in (
        "page_path",
        "section_name",
        "allowed_top_sections",
        "available_headings",
    ):
        if field_name in failure_payload:
            feedback[field_name] = failure_payload[field_name]
    return feedback


def _read_plan_schema(
    *,
    memory_topology: _MemoryTopology,
    max_read_calls: int,
    target_key: str,
) -> dict[str, object]:
    if max_read_calls < 1 or max_read_calls > _MAX_READ_CALLS:
        raise AnalysisWorkflowError("read-plan max_read_calls is outside the total budget.")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["read_calls", "planning_notes"],
        "properties": {
            "read_calls": {
                "type": "array",
                "maxItems": max_read_calls,
                "items": {
                    "oneOf": [
                        _read_call_schema(
                            name,
                            memory_topology=memory_topology,
                            target_key=target_key,
                        )
                        for name in _READ_TOOL_NAMES
                    ]
                },
            },
            "planning_notes": {"type": "array", "items": _bounded_string(), "maxItems": 8},
        },
    }


def _read_call_schema(
    tool_name: str,
    *,
    memory_topology: _MemoryTopology,
    target_key: str,
) -> dict[str, object]:
    argument_schemas: dict[str, dict[str, object]] = {
        "read_evidence": _object_schema(
            required=("event_ids",),
            properties={"event_ids": _string_array(max_items=32)},
        ),
        "read_page": _object_schema(
            required=("page_path",),
            properties={"page_path": _literal_string_union(memory_topology.readable_pages)},
        ),
        "read_section": _memory_section_target_schema(
            memory_topology.readable_section_targets,
        ),
        "read_around_citation": _memory_citation_target_schema(
            memory_topology.readable_citation_targets,
        ),
        "list_pages": _object_schema(required=("scope",), properties={"scope": _bounded_string()}),
        "search_wiki": _object_schema(
            required=("query", "scope"),
            properties={
                "query": _bounded_string(),
                "scope": _bounded_string(),
            },
        ),
        "read_market_overview": _market_base_schema(target_key=target_key),
        "read_technical_panel": _market_base_schema(target_key=target_key),
        "read_price_volume_window": _object_schema(
            required=("target_key", "lookback_hours"),
            properties={
                **_market_base_properties(target_key=target_key),
                "lookback_hours": {"type": "number", "exclusiveMinimum": 0},
                "granularity": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 400},
                "include_bars": {"type": "boolean"},
            },
        ),
        "read_option_activity": _object_schema(
            required=("target_key",),
            properties={
                **_market_base_properties(target_key=target_key),
                "max_contracts": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        ),
        "read_cross_asset_context": _object_schema(
            required=("target_key",),
            properties={
                **_market_base_properties(target_key=target_key),
                "include_proxy_details": {"type": "boolean"},
            },
        ),
        "read_cross_asset_window": _object_schema(
            required=("target_key", "lookback_hours"),
            properties={
                **_market_base_properties(target_key=target_key),
                "lookback_hours": {"type": "number", "exclusiveMinimum": 0},
                "max_rows_per_symbol": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 400,
                },
            },
        ),
    }
    return _call_schema(tool_name, argument_schemas[tool_name])


def _expert_schema() -> dict[str, object]:
    fields = (
        "material_facts",
        "causal_or_market_structure",
        "contradictions_and_uncertainties",
        "durable_memory_implications",
        "invalidation_and_missing_evidence",
    )
    return _object_schema(
        required=(
            "assessment_recommendation",
            "assessment_recommendation_rationale_md",
            *fields,
        ),
        properties={
            "assessment_recommendation": {
                "type": "string",
                "enum": list(_EXPERT_RECOMMENDATIONS),
            },
            "assessment_recommendation_rationale_md": {
                "type": "string",
                "minLength": 1,
                "pattern": r"^\S(?:[\s\S]*\S)?$",
            },
            **{name: _string_array(max_items=12) for name in fields},
        },
    )


def _decision_schema(*, request: AnalysisAgentAttemptRequest) -> dict[str, object]:
    context = request.tool_context
    return analysis_decision_payload_json_schema(
        constraints=AnalysisOwnerDraftConstraints(
            target_key=context.target_key,
            active_event_ids=tuple(citation.event_id for citation in context.active_citations),
            included_lesson_ids=context.included_lesson_ids,
            active_instrument_basis=_active_instrument_basis(context),
        ),
    )


def _assessment_schema(
    *,
    request: AnalysisAgentAttemptRequest,
    execution_direction_mode: ExecutionDirectionMode,
    visible_source_event_ids: tuple[str, ...],
) -> dict[str, object]:
    context = request.tool_context
    return analysis_assessment_draft_payload_json_schema(
        execution_direction_mode=execution_direction_mode,
        constraints=AnalysisOwnerDraftConstraints(
            target_key=context.target_key,
            active_event_ids=tuple(citation.event_id for citation in context.active_citations),
            included_lesson_ids=context.included_lesson_ids,
            active_instrument_basis=_active_instrument_basis(context),
            visible_event_ids=visible_source_event_ids,
            baseline_price_level_ids=(
                ()
                if request.previous_assessment is None
                else tuple(
                    level.level_id
                    for level in request.previous_assessment.price_level_roles
                )
            ),
        ),
    )


def _active_instrument_basis(context: AnalysisToolContext) -> str | None:
    packet = context.context_packet
    workbench = None if packet is None else packet.analysis_workbench
    market_lane = None if workbench is None else workbench.market_lane
    basis = None if market_lane is None else market_lane.tradable_proxy_symbol
    return basis.strip() if isinstance(basis, str) and basis.strip() else None


def _assemble_canonical_analysis_payload(
    *,
    decision_draft: Mapping[str, object],
    assessment_draft: Mapping[str, object] | None,
    request: AnalysisAgentAttemptRequest,
) -> dict[str, object]:
    """Assemble the final payload from split model cognition and runtime truth."""

    context = request.tool_context
    if context.business_at is None:
        raise AnalysisWorkflowError(
            "Analysis runtime assembly requires a decision-time business_at."
        )
    unexpected_decision_fields = set(decision_draft).difference(
        {
            "used_lesson_ids",
            "assessment_decision",
            "assessment_decision_rationale_md",
        }
    )
    if unexpected_decision_fields:
        raise AnalysisWorkflowError(
            "Analysis decision emitted field(s) outside its model-owned contract: "
            f"{sorted(unexpected_decision_fields)!r}."
        )
    canonical_payload: dict[str, object] = {
        "target_key": context.target_key,
        "event_ids": [citation.event_id for citation in context.active_citations],
        "used_lesson_ids": decision_draft.get("used_lesson_ids"),
        "analysis_assessment": assessment_draft,
    }
    assessment = canonical_payload["analysis_assessment"]
    if not isinstance(assessment, Mapping):
        return canonical_payload
    canonical_assessment = dict(assessment)
    for field_name in analysis_assessment_runtime_owned_fields():
        if field_name in canonical_assessment:
            raise AnalysisWorkflowError(
                f"Analysis Assessment synthesizer must not emit runtime-owned field {field_name!r}."
            )
    supporting_event_ids = canonical_assessment.pop("supporting_event_ids", None)
    if not isinstance(supporting_event_ids, list) or not all(
        isinstance(event_id, str) for event_id in supporting_event_ids
    ):
        raise AnalysisWorkflowError(
            "Analysis Assessment draft supporting_event_ids must be a list of strings."
        )
    visible_source_event_ids = _visible_source_event_ids(request=request)
    invisible_supporting_event_ids = set(supporting_event_ids).difference(
        visible_source_event_ids
    )
    if invisible_supporting_event_ids:
        raise AnalysisWorkflowError(
            "Analysis Assessment draft supporting_event_ids are not admitted by this "
            f"Analysis pass: {sorted(invisible_supporting_event_ids)!r}."
        )
    price_level_actions = canonical_assessment.pop("price_level_actions", None)
    active_event_ids = [citation.event_id for citation in context.active_citations]
    canonical_assessment["source_event_ids"] = list(
        dict.fromkeys((*active_event_ids, *supporting_event_ids))
    )
    try:
        canonical_assessment["price_level_roles"] = assemble_price_level_actions(
            price_level_actions,
            target_key=context.target_key,
            baseline_price_level_roles=(
                ()
                if request.previous_assessment is None
                else request.previous_assessment.price_level_roles
            ),
            allowed_source_event_ids=visible_source_event_ids,
        )
    except ValueError as exc:
        raise AnalysisWorkflowError(str(exc)) from exc
    canonical_assessment.update(
        {
            "assessment_id": request.analysis_assessment_id,
            "target_key": context.target_key,
            "business_at": context.business_at.isoformat(),
            **analysis_assessment_precommit_defaults(),
        }
    )
    canonical_payload["analysis_assessment"] = canonical_assessment
    return canonical_payload




def _revision_plan_analysis_assessment(
    *,
    canonical_payload: Mapping[str, object],
    request: AnalysisAgentAttemptRequest,
    config: AnalysisWorkflowConfig,
) -> AnalysisAssessment | None:
    """Assemble the same runtime-owned Assessment truth used by final defenses."""

    assessment_payload = canonical_payload.get("analysis_assessment")
    if assessment_payload is None:
        return None
    if not isinstance(assessment_payload, Mapping):
        raise AnalysisWorkflowError(
            "Validated Analysis owner payload contained a non-object assessment."
        )
    try:
        assessment = parse_analysis_assessment(
            assessment_payload,
            execution_direction_mode=config.execution_direction_mode,
        )
    except AnalysisAssessmentContractError as exc:
        raise AnalysisWorkflowError(
            "Validated Analysis owner assessment could not be parsed for revision "
            f"planning: {exc}"
        ) from exc
    market_config = request.tool_context.market_config
    if market_config is None:
        enriched_assessment = enrich_assessment_with_analysis_price_semantics(
            assessment,
            market_context=request.market_context,
        )
        if enriched_assessment is None:
            raise AnalysisWorkflowError(
                "Runtime AnalysisAssessment enrichment unexpectedly removed a non-null "
                "validated assessment."
            )
        return enriched_assessment
    try:
        return align_analysis_assessment_to_active_price_basis(
            assessment,
            layout=request.tool_context.canonical_layout,
            validation_config=market_config.validation,
            market_context_config=market_config.market_context,
            market_context=request.market_context,
        )
    except ActivePriceBasisAlignmentError as exc:
        raise AnalysisWorkflowError(str(exc)) from exc


def _revision_plan_schema(*, memory_topology: _MemoryTopology) -> dict[str, object]:
    return _object_schema(
        required=("revision_intents",),
        properties={
            "revision_intents": {
                "type": "array",
                "maxItems": _MAX_WRITE_CALLS,
                "items": {
                    "oneOf": [
                        _revision_intent_schema(
                            "update_page_section",
                            memory_topology=memory_topology,
                        ),
                        _revision_intent_schema(
                            "rewrite_page",
                            memory_topology=memory_topology,
                        ),
                        _revision_intent_schema("create_page"),
                    ]
                },
            }
        },
    )


def _revision_intent_schema(
    operation: str,
    *,
    memory_topology: _MemoryTopology | None = None,
) -> dict[str, object]:
    properties: dict[str, object] = {
        "operation": {"const": operation},
        "page_path": _bounded_string(),
        "reason": _bounded_string(max_length=2_000),
    }
    required = ("operation", "page_path", "reason")
    if operation == "update_page_section":
        if memory_topology is None:
            raise AnalysisWorkflowError(
                "update_page_section revision schema requires a memory topology."
            )
        return _memory_revision_section_target_schema(
            memory_topology.writable_section_targets
        )
    if operation == "rewrite_page":
        if memory_topology is None:
            raise AnalysisWorkflowError("rewrite_page revision schema requires a memory topology.")
        properties["page_path"] = _literal_string_union(memory_topology.writable_pages)
    return _object_schema(required=required, properties=properties)


def _literal_string_union(values: tuple[str, ...]) -> dict[str, object]:
    if not values:
        raise AnalysisWorkflowError("ResearchMemory topology must contain at least one page.")
    return {"type": "string", "enum": list(values)}


def _memory_section_target_schema(
    targets: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    if not targets:
        raise AnalysisWorkflowError("Readable ResearchMemory topology has no sections.")
    return {
        "oneOf": [
            _object_schema(
                required=("page_path", "section_name"),
                properties={
                    "page_path": {"const": page_path},
                    "section_name": {"const": section_name},
                },
            )
            for page_path, section_name in targets
        ]
    }


def _memory_citation_target_schema(
    targets: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    if not targets:
        # Keep the tool schema constructible even when this attempt has no
        # citation pairs.  The empty enum makes this operation impossible,
        # while other read tools remain available for the attempt.
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["page_path", "event_id"],
            "properties": {
                "page_path": {"type": "string", "enum": []},
                "event_id": _bounded_string(),
            },
        }
    return {
        "oneOf": [
            _object_schema(
                required=("page_path", "event_id"),
                properties={
                    "page_path": {"const": page_path},
                    "event_id": {"const": event_id},
                },
            )
            for page_path, event_id in targets
        ]
    }


def _memory_revision_section_target_schema(
    targets: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    if not targets:
        raise AnalysisWorkflowError("Writable ResearchMemory topology has no sections.")
    return {
        "oneOf": [
            _object_schema(
                required=("operation", "page_path", "section_name", "reason"),
                properties={
                    "operation": {"const": "update_page_section"},
                    "page_path": {"const": page_path},
                    "section_name": {"const": section_name},
                    "reason": _bounded_string(max_length=2_000),
                },
            )
            for page_path, section_name in targets
        ]
    }


def _revision_writer_schema(intent: Mapping[str, object]) -> dict[str, object]:
    operation = intent.get("operation")
    if operation not in _WRITE_TOOL_NAMES:
        raise AnalysisWorkflowError(f"Unknown ResearchMemory revision operation {operation!r}.")
    content_field = "new_content_md" if operation != "create_page" else "initial_content_md"
    max_length = 30_000 if operation == "update_page_section" else 50_000
    support_fields = {
        "absence_based_support": _support_schema(),
        "maturity_based_support": _support_schema(),
    }
    return _object_schema(
        required=(content_field,),
        properties={
            content_field: _bounded_string(max_length=max_length),
            **support_fields,
        },
    )


def _parse_revision_intents(
    payload: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    raw_intents = payload.get("revision_intents")
    if not isinstance(raw_intents, list):
        raise AnalysisWorkflowError("revision_intents must be a list.")
    if len(raw_intents) > _MAX_WRITE_CALLS:
        raise AnalysisWorkflowError("revision_intents exceeds its bounded write budget.")
    intents: list[dict[str, object]] = []
    identities: set[str] = set()
    for index, raw_intent in enumerate(raw_intents):
        if not isinstance(raw_intent, Mapping):
            raise AnalysisWorkflowError(f"revision_intents[{index}] must be an object.")
        intent = {str(key): value for key, value in raw_intent.items()}
        operation = intent.get("operation")
        page_path = intent.get("page_path")
        reason = intent.get("reason")
        if operation not in _WRITE_TOOL_NAMES:
            raise AnalysisWorkflowError(
                f"revision_intents[{index}] selected forbidden operation {operation!r}."
            )
        if not isinstance(page_path, str) or not page_path.strip():
            raise AnalysisWorkflowError(f"revision_intents[{index}].page_path must be non-blank.")
        if not isinstance(reason, str) or not reason.strip():
            raise AnalysisWorkflowError(f"revision_intents[{index}].reason must be non-blank.")
        section_name = intent.get("section_name")
        if operation == "update_page_section":
            if not isinstance(section_name, str) or not section_name.strip():
                raise AnalysisWorkflowError(
                    f"revision_intents[{index}].section_name must be non-blank for "
                    "update_page_section."
                )
        elif section_name is not None:
            raise AnalysisWorkflowError(
                f"revision_intents[{index}].section_name is only valid for "
                "update_page_section."
            )
        identity = _json_text([operation, page_path, section_name])
        if identity in identities:
            raise AnalysisWorkflowError(
                "revision_intents contains an exact duplicate operation/page/section target."
            )
        identities.add(identity)
        intents.append(intent)
    return tuple(intents)


def _revision_write_call(
    *,
    intent: Mapping[str, object],
    payload: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    operation = intent["operation"]
    if not isinstance(operation, str):
        raise AnalysisWorkflowError("ResearchMemory revision operation must be a string.")
    arguments: dict[str, object] = {"page_path": intent["page_path"]}
    if operation == "update_page_section":
        arguments["section_name"] = intent["section_name"]
        content_field = "new_content_md"
    elif operation == "rewrite_page":
        content_field = "new_content_md"
    elif operation == "create_page":
        content_field = "initial_content_md"
    else:
        raise AnalysisWorkflowError(f"Unknown ResearchMemory revision operation {operation!r}.")
    content = payload.get(content_field)
    if not isinstance(content, str) or not content.strip():
        raise AnalysisWorkflowError(
            f"ResearchMemory revision writer must return non-blank {content_field}."
        )
    arguments[content_field] = content
    for support_field in ("absence_based_support", "maturity_based_support"):
        support = payload.get(support_field)
        if support is not None:
            arguments[support_field] = support
    return operation, arguments


def _support_schema() -> dict[str, object]:
    support = _object_schema(
        required=("used", "reason_codes", "domains", "support_refs"),
        properties={
            "used": {"const": True},
            "reason_codes": _string_array(max_items=12),
            "domains": _string_array(max_items=12),
            "support_refs": _string_array(max_items=32),
        },
    )
    return {"anyOf": [support, {"type": "null"}]}


def _call_schema(tool_name: str, arguments: Mapping[str, object]) -> dict[str, object]:
    return _object_schema(
        required=("tool_name", "arguments"),
        properties={
            "tool_name": {"const": tool_name},
            "arguments": dict(arguments),
        },
    )


def _object_schema(
    *,
    required: tuple[str, ...],
    properties: Mapping[str, object],
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": dict(properties),
    }


def _market_base_schema(*, target_key: str) -> dict[str, object]:
    return _object_schema(
        required=("target_key",),
        properties=_market_base_properties(target_key=target_key),
    )


def _market_base_properties(*, target_key: str) -> dict[str, object]:
    return {
        "target_key": {"const": target_key},
    }


def _bounded_string(*, max_length: int = 2_000) -> dict[str, object]:
    return {"type": "string", "minLength": 1, "maxLength": max_length}


def _string_array(*, max_items: int) -> dict[str, object]:
    return {
        "type": "array",
        "items": _bounded_string(),
        "maxItems": max_items,
        "uniqueItems": True,
    }


def _parse_calls(
    payload: Mapping[str, object],
    *,
    field_name: str,
    allowed_names: tuple[str, ...],
    max_calls: int,
    runtime_market_as_of_at: datetime | None = None,
) -> tuple[tuple[str, dict[str, object]], ...]:
    raw_calls = payload.get(field_name)
    if not isinstance(raw_calls, list):
        raise AnalysisWorkflowError(f"{field_name} must be a list.")
    if len(raw_calls) > max_calls:
        raise AnalysisWorkflowError(f"{field_name} exceeds its bounded call budget.")
    calls: list[tuple[str, dict[str, object]]] = []
    identities: set[str] = set()
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, Mapping):
            raise AnalysisWorkflowError(f"{field_name}[{index}] must be an object.")
        tool_name = raw_call.get("tool_name")
        arguments = raw_call.get("arguments")
        if not isinstance(tool_name, str) or tool_name not in allowed_names:
            raise AnalysisWorkflowError(
                f"{field_name}[{index}] selected forbidden tool {tool_name!r}."
            )
        if not isinstance(arguments, Mapping):
            raise AnalysisWorkflowError(f"{field_name}[{index}].arguments must be an object.")
        normalized_arguments = {str(key): value for key, value in arguments.items()}
        if tool_name in _MARKET_READ_TOOL_NAMES:
            if runtime_market_as_of_at is None:
                raise AnalysisWorkflowError(
                    "Market reads require a runtime-owned as_of_at timestamp."
                )
            normalized_arguments.pop("as_of_at", None)
            normalized_arguments["as_of_at"] = runtime_market_as_of_at.isoformat()
        identity = _json_text([tool_name, normalized_arguments])
        if identity in identities:
            raise AnalysisWorkflowError(
                f"{field_name} contains an exact duplicate call for {tool_name}."
            )
        identities.add(identity)
        calls.append((tool_name, normalized_arguments))
    selected = set(allowed_names)
    if any(tool_name not in selected for tool_name, _ in calls):
        raise AnalysisWorkflowError(f"{field_name} contains a forbidden tool.")
    return tuple(calls)


def _decode_tool_result(raw_result: str) -> object:
    try:
        return json.loads(raw_result)
    except json.JSONDecodeError:
        return raw_result


async def _generate_initial_revision_writer_drafts(
    *,
    provider: StructuredInference,
    specs: tuple[_RevisionWriterInitialSpec, ...],
    trace_receipts: list[dict[str, object]],
) -> tuple[tuple[StructuredInferenceResult, dict[str, object]], ...]:
    """Generate independent writer drafts before allowing any memory write."""

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REVISION_WRITERS)
    local_receipts: list[list[dict[str, object]]] = [[] for _ in specs]

    async def generate_one(
        spec: _RevisionWriterInitialSpec,
    ) -> tuple[StructuredInferenceResult, dict[str, object]]:
        async with semaphore:
            result, receipt = await _generate_structured_stage(
                trace_receipts=local_receipts[spec.index],
                stage="memory_revision_writer",
                business_repair_attempt=1,
                provider=provider,
                prompt=spec.prompt,
                session_id=spec.session_id,
                tool_name=_REVISION_WRITER_TOOL,
                tool_description=(
                    "Return the body for exactly one project-selected ResearchMemory "
                    "revision."
                ),
                input_schema=spec.schema,
            )
            return result, receipt

    tasks = [asyncio.create_task(generate_one(spec)) for spec in specs]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for receipts in local_receipts:
            trace_receipts.extend(receipts)
        raise

    for receipts in local_receipts:
        trace_receipts.extend(receipts)
    return tuple(results)


async def _generate_structured_stage(
    *,
    trace_receipts: list[dict[str, object]],
    stage: str,
    provider: StructuredInference,
    prompt: str,
    session_id: str,
    tool_name: str,
    tool_description: str,
    input_schema: Mapping[str, object],
    business_repair_attempt: int | None = None,
) -> tuple[StructuredInferenceResult, dict[str, object]]:
    """Run one provider call and retain only bounded operational diagnostics."""

    receipt: dict[str, object] = {
        "stage": stage,
        "tool_name": tool_name,
        "business_repair_attempt": business_repair_attempt,
        "status": "running",
    }
    trace_receipts.append(receipt)
    started_at = monotonic()
    try:
        result = await provider.generate_structured(
            prompt=prompt,
            session_id=session_id,
            tool_name=tool_name,
            tool_description=tool_description,
            input_schema=input_schema,
        )
    except asyncio.CancelledError:
        receipt.update(
            status="terminal",
            elapsed_ms=_elapsed_milliseconds(started_at),
            error_type="CancelledError",
        )
        raise
    except Exception as exc:
        terminal_metadata = (
            exc.terminal_metadata
            if isinstance(exc, AnthropicStructuredInferenceError)
            else {}
        )
        receipt.update(
            status="terminal",
            elapsed_ms=_elapsed_milliseconds(started_at),
            error_type=type(exc).__name__,
            **_structured_stage_metadata(terminal_metadata),
        )
        raise
    receipt.update(
        status="returned",
        elapsed_ms=_elapsed_milliseconds(started_at),
        **_structured_stage_metadata(result.metadata),
    )
    return result, receipt


def _mark_stage_business_status(receipt: dict[str, object], status: str) -> None:
    if status not in {"accepted", "rejected"}:
        raise AnalysisWorkflowError(f"unsupported structured-stage status {status!r}.")
    receipt["business_status"] = status


def _elapsed_milliseconds(started_at: float) -> int:
    return max(0, round((monotonic() - started_at) * 1_000))


def _structured_stage_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    """Persist call diagnostics without duplicating provider responses or prompts."""

    receipt: dict[str, object] = {}
    for field_name in (
        "transport_attempt_count",
        "total_transport_attempt_count",
        "protocol_attempt_count",
    ):
        value = metadata.get(field_name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            receipt[field_name] = value
    totals = metadata.get("usage_totals")
    if isinstance(totals, Mapping):
        normalized_totals: dict[str, object] = {}
        for field_name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "response_count",
        ):
            value = totals.get(field_name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                normalized_totals[field_name] = value
        for field_name in (
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "observed_input_tokens",
            "observed_total_tokens",
        ):
            value = totals.get(field_name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                normalized_totals[field_name] = value
        cache_complete = totals.get("cache_complete")
        if isinstance(cache_complete, bool):
            normalized_totals["cache_complete"] = cache_complete
        complete = totals.get("complete")
        if isinstance(complete, bool):
            normalized_totals["complete"] = complete
        if normalized_totals:
            receipt["usage_totals"] = normalized_totals
    rejected = metadata.get("rejected_response_summaries")
    if isinstance(rejected, list):
        receipt["rejected_response_count"] = len(rejected)
    rejected_count = metadata.get("rejected_response_count")
    if isinstance(rejected_count, int) and not isinstance(rejected_count, bool):
        receipt["rejected_response_count"] = rejected_count
    schema_ledger = metadata.get("schema_repair_ledger")
    if isinstance(schema_ledger, list):
        receipt["schema_repair_violation_count"] = len(schema_ledger)
    schema_repair_count = metadata.get("schema_repair_violation_count")
    if isinstance(schema_repair_count, int) and not isinstance(schema_repair_count, bool):
        receipt["schema_repair_violation_count"] = schema_repair_count
    return receipt


def _workflow_result(
    *,
    config: AnalysisWorkflowConfig,
    payload: Mapping[str, object],
    inference_results: tuple[StructuredInferenceResult, ...],
    trace: dict[str, object],
) -> _WorkflowResult:
    _record_trace_usage_totals(trace)
    return _WorkflowResult(
        payload=payload,
        llm_usage=_combine_usage(config=config, results=inference_results),
    )


def _record_trace_usage_totals(trace: dict[str, object]) -> None:
    raw_receipts = trace.get("structured_stage_receipts")
    if not isinstance(raw_receipts, list):
        trace["structured_usage_totals"] = _empty_structured_usage_totals()
        return
    receipts = [receipt for receipt in raw_receipts if isinstance(receipt, Mapping)]
    trace["structured_usage_totals"] = _structured_usage_totals_from_receipts(receipts)


def _empty_structured_usage_totals() -> dict[str, object]:
    return {
        "complete": True,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "response_count": 0,
        "receipt_count": 0,
        "incomplete_receipt_count": 0,
    }


def _structured_usage_totals_from_receipts(
    receipts: list[Mapping[str, object]],
) -> dict[str, object]:
    input_tokens = 0
    output_tokens = 0
    response_count = 0
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0
    observed_input_tokens = 0
    observed_total_tokens = 0
    cache_receipt_count = 0
    cache_incomplete_receipt_count = 0
    incomplete_receipt_count = 0
    for receipt in receipts:
        totals = receipt.get("usage_totals")
        if not isinstance(totals, Mapping):
            incomplete_receipt_count += 1
            continue
        stage_input = totals.get("input_tokens")
        stage_output = totals.get("output_tokens")
        stage_responses = totals.get("response_count")
        if (
            not isinstance(stage_input, int)
            or isinstance(stage_input, bool)
            or stage_input < 0
            or not isinstance(stage_output, int)
            or isinstance(stage_output, bool)
            or stage_output < 0
            or not isinstance(stage_responses, int)
            or isinstance(stage_responses, bool)
            or stage_responses < 0
        ):
            incomplete_receipt_count += 1
            continue
        input_tokens += stage_input
        output_tokens += stage_output
        response_count += stage_responses
        stage_cache_creation = totals.get("cache_creation_input_tokens")
        stage_cache_read = totals.get("cache_read_input_tokens")
        stage_cache_complete = totals.get("cache_complete")
        stage_observed_input = totals.get("observed_input_tokens")
        stage_observed_total = totals.get("observed_total_tokens")
        if (
            isinstance(stage_cache_creation, int)
            and not isinstance(stage_cache_creation, bool)
            and stage_cache_creation >= 0
            and isinstance(stage_cache_read, int)
            and not isinstance(stage_cache_read, bool)
            and stage_cache_read >= 0
        ):
            cache_receipt_count += 1
            cache_creation_input_tokens += stage_cache_creation
            cache_read_input_tokens += stage_cache_read
            if (
                isinstance(stage_observed_input, int)
                and not isinstance(stage_observed_input, bool)
                and stage_observed_input >= 0
                and isinstance(stage_observed_total, int)
                and not isinstance(stage_observed_total, bool)
                and stage_observed_total >= 0
            ):
                observed_input_tokens += stage_observed_input
                observed_total_tokens += stage_observed_total
            if stage_cache_complete is not True:
                cache_incomplete_receipt_count += 1
        if totals.get("complete") is not True:
            incomplete_receipt_count += 1
    result: dict[str, object] = {
        "complete": incomplete_receipt_count == 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "response_count": response_count,
        "receipt_count": len(receipts),
        "incomplete_receipt_count": incomplete_receipt_count,
    }
    if cache_receipt_count:
        result.update(
            {
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
                "observed_input_tokens": observed_input_tokens,
                "observed_total_tokens": observed_total_tokens,
                "total_tokens": observed_total_tokens,
                "cache_complete": cache_incomplete_receipt_count == 0,
                "cache_receipt_count": cache_receipt_count,
                "cache_incomplete_receipt_count": cache_incomplete_receipt_count,
            }
        )
    return result


def _combine_usage(
    *,
    config: AnalysisWorkflowConfig,
    results: tuple[StructuredInferenceResult, ...],
) -> LLMUsageReceipt:
    input_tokens = 0
    output_tokens = 0
    stage_totals: list[Mapping[str, object]] = []
    for result in results:
        totals = result.metadata.get("usage_totals")
        if not isinstance(totals, Mapping) or totals.get("complete") is not True:
            return LLMUsageReceipt.unavailable(
                agent_role="analysis",
                provider=config.llm_provider,
                model=config.llm_model_name,
            )
        stage_totals.append(totals)
        stage_input = totals.get("input_tokens")
        stage_output = totals.get("output_tokens")
        if (
            not isinstance(stage_input, int)
            or isinstance(stage_input, bool)
            or stage_input < 0
            or not isinstance(stage_output, int)
            or isinstance(stage_output, bool)
            or stage_output < 0
        ):
            return LLMUsageReceipt.unavailable(
                agent_role="analysis",
                provider=config.llm_provider,
                model=config.llm_model_name,
            )
        output_tokens += stage_output

    cache_metadata_keys = {
        "cache_complete",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "observed_input_tokens",
        "observed_total_tokens",
    }
    has_cache_metadata = any(
        any(key in totals for key in cache_metadata_keys) for totals in stage_totals
    )
    if has_cache_metadata:
        for totals in stage_totals:
            observed_input = totals.get("observed_input_tokens")
            if (
                totals.get("cache_complete") is not True
                or not isinstance(observed_input, int)
                or isinstance(observed_input, bool)
                or observed_input < 0
            ):
                return LLMUsageReceipt.unavailable(
                    agent_role="analysis",
                    provider=config.llm_provider,
                    model=config.llm_model_name,
                )
            input_tokens += observed_input
    else:
        input_tokens = sum(
            totals["input_tokens"]
            for totals in stage_totals
            if isinstance(totals["input_tokens"], int)
        )
    return LLMUsageReceipt(
        agent_role="analysis",
        usage_source="provider_reported",
        provider=config.llm_provider,
        model=config.llm_model_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _assert_prompt_budget(
    config: AnalysisWorkflowConfig,
    prompt: str,
    *,
    stage: str,
) -> None:
    assert_structured_prompt_budget(
        prompt,
        max_context_tokens=config.llm_max_context_length,
        max_output_tokens=config.llm_max_output_tokens,
        stage=f"Analysis workflow {stage}",
        error_type=AnalysisWorkflowError,
    )


def _write_trace(
    *,
    config: AnalysisWorkflowConfig,
    request: AnalysisAgentAttemptRequest,
    trace: Mapping[str, object],
) -> Path:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    digest = sha256(request.task_id.encode("utf-8")).hexdigest()[:24]
    path = config.log_dir / f"event-trader-analysis-{digest}-{uuid4().hex}.json"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=config.log_dir,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(trace, handle, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            handle.write("\n")
        os.replace(temporary_path, path)
    except Exception as exc:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        raise AnalysisWorkflowError(
            f"failed to persist Analysis workflow diagnostic log: {exc}"
        ) from exc
    return path


def _json_text(value: object, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2 if pretty else None,
        sort_keys=True,
        separators=None if pretty else (",", ":"),
        default=str,
    )


def _validate_path(value: Path, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise AnalysisWorkflowError(f"{field_name} must be a pathlib.Path.")
    return value.expanduser().resolve(strict=False)


def _non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisWorkflowError(f"{field_name} must be non-blank.")
    return value.strip()


__all__ = [
    "AnalysisWorkflowConfig",
    "AnalysisWorkflowError",
    "AnalysisWorkflowExecutor",
    "build_analysis_workflow_executor",
]
