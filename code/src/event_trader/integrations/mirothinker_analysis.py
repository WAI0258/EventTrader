"""Thin MiroThinker-backed runtime binding for one analysis execution pass."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

from event_trader.analysis import AnalysisContext
from event_trader.analysis_assessment_store import (
    AnalysisAssessmentStore,
    AnalysisAssessmentStoreError,
)
from event_trader.ceau.contracts import UnitFormationLane
from event_trader.context_assembly import (
    CURRENT_MEMORY_READ_POLICY,
    ContextAssemblyError,
    VisibilityAudit,
    ContextPacket,
    ContextPacketRuntimeScope,
    ContextPacketStoreError,
    FileBackedContextPacketStore,
    GroundingCoverageReceipt,
    ResearchClaimUsageReceipt,
    build_analysis_context_packet,
    hash_context_packet,
    serialize_context_packet,
    validate_context_packet_visibility,
)
from event_trader.config import load_kernel_config
from event_trader.contracts import (
    AnalysisAssessment,
    AnalysisResult,
    EvidenceLedgerRecord,
    LLMUsageReceipt,
    PageReadResult,
)
from event_trader.contracts.analysis_assessment_schema import (
    analysis_final_payload_contract_markdown,
)
from event_trader.contracts.analysis_direction_policy import (
    AnalysisDirectionPolicyNormalizationError,
    normalize_analysis_assessment_for_execution_direction_mode,
)
from event_trader.contracts.pm_review_reason import (
    PMReviewReason,
    analysis_pm_annotation_only_reasons,
    analysis_pm_escalation_reasons,
)
from event_trader.contracts.analysis_final_payload_validation import (
    AnalysisFinalPayloadValidationResult,
    price_level_role_repair_details,
    validate_analysis_final_payload,
)
from event_trader.contracts.execution_direction_policy import (
    ExecutionDirectionMode,
    validate_execution_direction_mode,
)
from event_trader.contracts.evidence_review import (
    classify_evidence_review_dimensions,
    summarize_evidence_review_dimensions,
)
from event_trader.decision_memory import (
    DecisionEpisodeRecord,
    DecisionEpisodeStoreError,
    FileBackedDecisionEpisodeStore,
)
from event_trader.integrations.analysis_citations import (
    AnalysisCitation,
    canonicalize_analysis_citations,
    extract_analysis_citations,
    extract_noncanonical_analysis_event_ids,
    serialize_analysis_citations,
)
from event_trader.integrations.analysis_commit_audit import (
    ANALYSIS_COMMIT_DIR_NAME as _ANALYSIS_COMMIT_DIR_NAME,
    AnalysisCommitProgress as _AnalysisCommitProgress,
    analysis_commit_journal_path as _analysis_commit_journal_path,
    analysis_commit_state_payload as _analysis_commit_state_payload,
)
from event_trader.integrations.bounded_context import (
    DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
    bounded_text_payload,
)
from event_trader.integrations.boxed_json import (
    BoxedJsonPayloadError,
    load_first_boxed_json_object,
)
from event_trader.integrations.analysis_structured_finalizer import (
    AnalysisStructuredFinalizerConfig,
    AnalysisStructuredFinalizerError,
    AnalysisStructuredFinalizerInput,
    AnalysisStructuredOutputMode,
    finalize_analysis_payload,
    normalize_analysis_structured_output_mode,
)
from event_trader.integrations.analysis_structured_provider import (
    AnalysisStructuredProviderAdapter,
    AnalysisStructuredProviderConfig,
)
from event_trader.integrations.markdown_context import (
    MarkdownContextError,
    build_markdown_page_map,
    find_markdown_section,
)
from event_trader.integrations.mirothinker_llm_config import (
    normalize_mirothinker_reasoning_effort,
)
from event_trader.integrations.mirothinker_runtime_paths import (
    MiroThinkerRuntimePathError,
    build_mirothinker_child_pythonpath,
    prepare_mirothinker_runtime,
)
from event_trader.integrations.strict_mirothinker_agent import (
    StrictMiroThinkerAgentContract,
    StrictMiroThinkerAgentRunResult,
    mirothinker_structured_output_fallback_texts_from_log,
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
from event_trader.research_memory import FileBackedIndexLogWriter
from event_trader.research_memory.analysis_price_basis import (
    canonicalize_analysis_assessment_price_bases,
)
from event_trader.research_memory.active_price_basis_alignment import (
    ActivePriceBasisAlignmentError,
    align_analysis_assessment_to_active_price_basis,
)
from event_trader.research_memory.analysis_price_semantics_assembler import (
    AnalysisPriceSemanticsAssemblyError,
    enrich_assessment_with_analysis_price_semantics,
)
from event_trader.research_memory.active_price_projection import (
    project_active_price_sections,
)
from event_trader.research_memory.write_receipts import (
    ReceiptedResearchMemoryPageWriter,
    ResearchMemoryWriteAttribution,
    ResearchMemoryWriteReceipt,
)
from event_trader.portfolio.active_exposure import (
    ActiveExposureResolverError,
    resolve_active_exposure,
)
from event_trader.portfolio.store import PortfolioStateStore, PortfolioStoreError
from event_trader.storage import (
    WorkspaceLayout,
    build_workspace_layout,
    initialize_workspace_layout,
)
from event_trader.thesis_revision.builder import ThesisRevisionBuildError, ThesisRevisionBuilder
from event_trader.thesis_revision.canonical_bundle import is_canonical_thesis_bundle_write
from event_trader.thesis_revision.contracts import ThesisRevision
from event_trader.thesis_revision.store import (
    ThesisRevisionStore,
    ThesisRevisionStoreError,
)

type AnalysisCallback = Callable[[AnalysisContext], AnalysisResult]
_ANALYSIS_OUTCOME_DIR_NAME = "analysis_outcomes"
_ANALYSIS_STAGING_DIR_NAME = "analysis_staging"
_ANALYSIS_RECEIPT_PATH_ENV = "EVENT_TRADER_ANALYSIS_RECEIPT_PATH"
_ANALYSIS_STAGE_ROOT_ENV = "EVENT_TRADER_ANALYSIS_STAGE_ROOT"
_ANALYSIS_CITATIONS_ENV = "EVENT_TRADER_ANALYSIS_CITATIONS"
_ANALYSIS_CONTEXT_CITATIONS_ENV = "EVENT_TRADER_ANALYSIS_CONTEXT_CITATIONS"
_ANALYSIS_TARGET_KEY_ENV = "EVENT_TRADER_ANALYSIS_TARGET_KEY"
_ANALYSIS_BUSINESS_AT_ENV = "EVENT_TRADER_ANALYSIS_BUSINESS_AT"
_ANALYSIS_RUNTIME_SCOPE_ENV = "EVENT_TRADER_ANALYSIS_RUNTIME_SCOPE"
_ANALYSIS_EXECUTION_DIRECTION_MODE_ENV = "EVENT_TRADER_ANALYSIS_EXECUTION_DIRECTION_MODE"
_ANALYSIS_REQUIRED_WATCHLIST_PATH_ENV = "EVENT_TRADER_ANALYSIS_REQUIRED_WATCHLIST_PATH"
_ANALYSIS_CONFIG_PATH_ENV = "EVENT_TRADER_CONFIG_PATH"
_ANALYSIS_MARKET_DATA_STORE_ROOT_ENV = "EVENT_TRADER_MARKET_DATA_STORE_ROOT"
_ANALYSIS_CONTEXT_PACKET_ENV = "EVENT_TRADER_ANALYSIS_CONTEXT_PACKET"
_ANALYSIS_INCLUDED_LESSON_IDS_ENV = "EVENT_TRADER_ANALYSIS_INCLUDED_LESSON_IDS"
_ANALYSIS_MCP_SERVER_NAME = "event_trader_analysis"

_ANALYSIS_REQUIRED_MCP_TOOLS = (
    (_ANALYSIS_MCP_SERVER_NAME, "validate_analysis_final_payload"),
    (_ANALYSIS_MCP_SERVER_NAME, "read_evidence"),
    (_ANALYSIS_MCP_SERVER_NAME, "read_page"),
    (_ANALYSIS_MCP_SERVER_NAME, "read_section"),
    (_ANALYSIS_MCP_SERVER_NAME, "read_around_citation"),
    (_ANALYSIS_MCP_SERVER_NAME, "list_pages"),
    (_ANALYSIS_MCP_SERVER_NAME, "search_wiki"),
    (_ANALYSIS_MCP_SERVER_NAME, "update_page_section"),
    (_ANALYSIS_MCP_SERVER_NAME, "rewrite_page"),
    (_ANALYSIS_MCP_SERVER_NAME, "create_page"),
    (_ANALYSIS_MCP_SERVER_NAME, "read_market_overview"),
    (_ANALYSIS_MCP_SERVER_NAME, "read_price_volume_window"),
    (_ANALYSIS_MCP_SERVER_NAME, "read_technical_panel"),
    (_ANALYSIS_MCP_SERVER_NAME, "read_option_activity"),
    (_ANALYSIS_MCP_SERVER_NAME, "read_cross_asset_context"),
    (_ANALYSIS_MCP_SERVER_NAME, "read_cross_asset_window"),
)
_ANALYSIS_READ_RECEIPT_OPERATIONS = frozenset(
    {
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
    }
)
_ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS = frozenset(
    {
        "read_market_overview",
        "read_price_volume_window",
        "read_technical_panel",
        "read_option_activity",
        "read_cross_asset_context",
        "read_cross_asset_window",
    }
)
_ANALYSIS_MEMORY_READ_OPERATIONS = frozenset(
    {
        "read_page",
        "read_section",
        "read_around_citation",
        "list_pages",
        "search_wiki",
    }
)


@dataclass(frozen=True, slots=True)
class _LearningCardUsage:
    visible_learning_card_ids: tuple[str, ...] = ()
    included_learning_card_ids: tuple[str, ...] = ()
    used_learning_card_ids: tuple[str, ...] = ()
    excluded_future_count: int = 0
    excluded_legacy_count: int = 0
    parse_warning_count: int = 0


@dataclass(frozen=True, slots=True)
class _CommittedResearchMemoryWrites:
    receipt_ids: tuple[str, ...]
    receipts: tuple[ResearchMemoryWriteReceipt, ...]


@dataclass(frozen=True, slots=True)
class _PersistedAnalysisCommitArtifacts:
    thesis_revision_required: bool
    analysis_assessment_id: str | None
    analysis_assessment_path: Path | None
    thesis_revision: ThesisRevision | None
    thesis_revision_path: Path | None


_ABSENCE_SUPPORT_REASON_CODES = frozenset(
    {
        "no_news_followup",
        "no_official_followup",
        "no_market_confirmation",
        "no_operator_followup",
    }
)
_MATURITY_SUPPORT_REASON_CODES = frozenset(
    {
        "market_bar_reaction_mature",
        "official_filing_window_complete",
        "operator_admission_visible",
    }
)
_SUPPORT_REASON_PURPOSE: dict[str, str] = {
    "no_news_followup": "absence_inference",
    "no_official_followup": "absence_inference",
    "no_market_confirmation": "absence_inference",
    "no_operator_followup": "absence_inference",
    "market_bar_reaction_mature": "evidence_maturity",
    "official_filing_window_complete": "evidence_maturity",
    "operator_admission_visible": "evidence_maturity",
}
_COMPLETENESS_DOMAINS = frozenset(
    {
        "market_bars",
        "news_web_search",
        "official_filings",
        "operator_admission",
    }
)
_ANALYSIS_AGENT_CONTRACT = StrictMiroThinkerAgentContract(
    agent_name="MiroThinker analysis agent",
    required_tools=_ANALYSIS_REQUIRED_MCP_TOOLS,
)
_ANALYSIS_WRITE_FAILURE_BUDGET = 10
_ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS = 3
_ANALYSIS_SAME_WRITE_ERROR_ABORT_THRESHOLD = 2
_ANALYSIS_WRITE_SUPPORT_UNGROUNDED_PREFIX = (
    "analysis write support for absence/maturity claims not grounded or not "
    "advanced in lane: "
)
_ANALYSIS_WRITE_TOOL_NAMES = frozenset(
    {
        "update_page_section",
        "rewrite_page",
        "create_page",
    }
)


class MiroThinkerAnalysisRuntimeError(RuntimeError):
    """Raised when the committed MiroThinker analysis runtime cannot execute."""


class _AnalysisCommitArtifactPersistenceError(MiroThinkerAnalysisRuntimeError):
    """Raised when append-only artifact persistence fails mid-commit."""

    def __init__(
        self,
        message: str,
        *,
        failure_stage: str,
        partial_artifacts: _PersistedAnalysisCommitArtifacts,
    ) -> None:
        self.failure_stage = failure_stage
        self.partial_artifacts = partial_artifacts
        super().__init__(message)


class _AnalysisWriteFailureBudgetExceeded(BaseException):
    """Internal stop signal that must bypass MiroFlow rollback handling."""


@dataclass(frozen=True, slots=True)
class _AnalysisContractFailure:
    surface: Literal["write_tool", "read_audit", "final_output"]
    error_code: str
    message: str
    recoverable: bool = True
    suggested_action: str = ""
    details: Mapping[str, object] | None = None
    arguments: object | None = None


class _AnalysisContractRepairRequired(BaseException):
    """Internal stop signal for bounded no-HITL contract repair."""

    def __init__(
        self,
        *,
        failure: _AnalysisContractFailure,
        failure_count: int,
        threshold: int,
    ) -> None:
        self.failure = failure
        self.failure_count = failure_count
        self.threshold = threshold
        super().__init__(
            "Analysis contract repair required "
            f"(surface={failure.surface}, error_code={failure.error_code}) "
            f"[{failure_count}/{threshold}]: "
            f"{_shorten_error_text(failure.message, max_length=180)}"
        )


def _analysis_contract_repair_supervision_payload(
    exc: _AnalysisContractRepairRequired,
) -> dict[str, object]:
    failure = exc.failure
    return {
        "status": "analysis_contract_repair_required",
        "error": str(exc),
        "failure_count": exc.failure_count,
        "threshold": exc.threshold,
        "failure": {
            "surface": failure.surface,
            "error_code": failure.error_code,
            "message": failure.message,
            "recoverable": failure.recoverable,
            "suggested_action": failure.suggested_action,
            "details": failure.details,
            "arguments": failure.arguments,
        },
    }


def _analysis_contract_repair_from_supervision_payload(
    payload: Mapping[str, object],
) -> _AnalysisContractRepairRequired:
    failure_payload = payload.get("failure")
    if not isinstance(failure_payload, Mapping):
        raise MiroThinkerAnalysisRuntimeError(
            "Analysis worker contract-repair payload is missing failure details."
        )
    surface = _require_supervision_failure_text(failure_payload, "surface")
    if surface not in {"write_tool", "read_audit", "final_output"}:
        raise MiroThinkerAnalysisRuntimeError(
            f"Analysis worker contract-repair payload has invalid surface: {surface}"
        )
    details = failure_payload.get("details")
    return _AnalysisContractRepairRequired(
        failure=_AnalysisContractFailure(
            surface=cast(Literal["write_tool", "read_audit", "final_output"], surface),
            error_code=_require_supervision_failure_text(
                failure_payload,
                "error_code",
            ),
            message=_require_supervision_failure_text(failure_payload, "message"),
            recoverable=_optional_supervision_failure_bool(
                failure_payload,
                "recoverable",
                default=True,
            ),
            suggested_action=_optional_supervision_failure_text(
                failure_payload,
                "suggested_action",
            ),
            details=details if isinstance(details, Mapping) else None,
            arguments=failure_payload.get("arguments"),
        ),
        failure_count=_require_supervision_int(payload, "failure_count"),
        threshold=_require_supervision_int(payload, "threshold"),
    )


def _require_supervision_failure_text(
    payload: Mapping[str, object],
    field_name: str,
) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise MiroThinkerAnalysisRuntimeError(
            f"Analysis worker contract-repair payload field {field_name} must be text."
        )
    return value.strip()


def _optional_supervision_failure_text(
    payload: Mapping[str, object],
    field_name: str,
) -> str:
    value = payload.get(field_name)
    return value.strip() if isinstance(value, str) else ""


def _optional_supervision_failure_bool(
    payload: Mapping[str, object],
    field_name: str,
    *,
    default: bool,
) -> bool:
    value = payload.get(field_name)
    return value if isinstance(value, bool) else default


def _require_supervision_int(
    payload: Mapping[str, object],
    field_name: str,
) -> int:
    value = payload.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MiroThinkerAnalysisRuntimeError(
            f"Analysis worker payload field {field_name} must be an integer."
        )
    return value


@dataclass(frozen=True, slots=True)
class _AnalysisWriteToolFailure:
    tool_name: str
    error_code: str
    message: str
    recoverable: bool
    arguments: object
    payload: Mapping[str, object] | None


class _PersistentStdioMcpToolManager:
    """Tool manager that keeps one MCP stdio session alive per analysis attempt."""

    def __init__(
        self,
        server_configs: list[dict[str, object]],
        tool_blacklist: object = None,
    ):
        self.server_configs = server_configs
        self.server_dict = {
            config["name"]: config["params"]
            for config in server_configs
            if isinstance(config.get("name"), str)
        }
        self.tool_blacklist: set[tuple[str, str]] = (
            set(cast(set[tuple[str, str]], tool_blacklist)) if tool_blacklist else set()
        )
        self.task_log: Any | None = None
        self._exit_stack = AsyncExitStack()
        self._sessions: dict[str, object] = {}
        self._tool_definitions_cache: list[dict[str, object]] | None = None
        self._closed = False

    def set_task_log(self, task_log: object) -> None:
        self.task_log = task_log
        self._log(
            "info",
            "ToolManager | Initialization",
            f"Persistent ToolManager initialized, loaded servers: {list(self.server_dict.keys())}",
        )

    def _log(
        self,
        level: str,
        step_name: str,
        message: str,
        metadata: object | None = None,
    ) -> None:
        if self.task_log is None:
            return
        log_step = getattr(self.task_log, "log_step", None)
        if callable(log_step):
            log_step(level, step_name, message, metadata)

    async def aclose(self) -> None:
        await self._exit_stack.aclose()
        self._sessions.clear()
        self._tool_definitions_cache = None
        self._closed = True

    async def preflight_required_tools(
        self,
        *,
        required_tools: tuple[tuple[str, str], ...],
        error_factory: Callable[[str], Exception],
        agent_name: str,
    ) -> None:
        tool_definitions = await self.get_all_tool_definitions()
        available_by_server: dict[str, set[str]] = {}
        for server in tool_definitions:
            server_name = server.get("name")
            tools = server.get("tools")
            if not isinstance(server_name, str) or not isinstance(tools, list):
                continue
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                tool_error = tool.get("error")
                if isinstance(tool_error, str) and tool_error.strip():
                    available_by_server.setdefault(server_name, set()).add(
                        f"<error: {tool_error.strip()}>"
                    )
                    continue
                tool_name = tool.get("name")
                if isinstance(tool_name, str) and tool_name.strip():
                    available_by_server.setdefault(server_name, set()).add(tool_name)

        expected_by_server: dict[str, set[str]] = {}
        for server_name, tool_name in required_tools:
            expected_by_server.setdefault(server_name, set()).add(tool_name)
        for server_name, expected_tool_names in expected_by_server.items():
            available_tool_names = available_by_server.get(server_name, set())
            missing_tool_names = sorted(expected_tool_names - available_tool_names)
            if missing_tool_names:
                available = ", ".join(sorted(available_tool_names)) or "<none>"
                raise error_factory(
                    f"{agent_name} MCP preflight failed for {server_name}; expected tools "
                    f"{', '.join(missing_tool_names)} were not listed. "
                    f"available_tools={available}"
                )

    async def get_all_tool_definitions(self) -> list[dict[str, object]]:
        if self._tool_definitions_cache is not None:
            return self._tool_definitions_cache
        all_servers_for_prompt: list[dict[str, object]] = []
        for config in self.server_configs:
            server_name = config["name"]
            if not isinstance(server_name, str):
                continue
            one_server_for_prompt: dict[str, object] = {"name": server_name, "tools": []}
            try:
                session = await self._session_for(server_name)
                tools_response = await session.list_tools()
                prompt_tools: list[dict[str, object]] = []
                for tool in tools_response.tools:
                    if (server_name, tool.name) in self.tool_blacklist:
                        continue
                    prompt_tools.append(
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "schema": tool.inputSchema,
                        }
                    )
                one_server_for_prompt["tools"] = prompt_tools
                self._log(
                    "info",
                    "ToolManager | Tool Definitions Success",
                    "Successfully obtained "
                    f"{len(prompt_tools)} tool definitions from server '{server_name}'.",
                )
            except Exception as exc:
                self._log(
                    "error",
                    "ToolManager | Connection Error",
                    f"Error: Unable to connect or get tools from server '{server_name}': {exc}",
                )
                one_server_for_prompt["tools"] = [
                    {"error": f"Unable to fetch tools: {exc}"}
                ]
            all_servers_for_prompt.append(one_server_for_prompt)
        self._tool_definitions_cache = all_servers_for_prompt
        return all_servers_for_prompt

    async def execute_tool_call(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        if server_name not in self.server_dict:
            self._log(
                "error",
                "ToolManager | Server Not Found",
                f"Error: Attempting to call server '{server_name}' not found",
            )
            return {
                "server_name": server_name,
                "tool_name": tool_name,
                "error": f"Server '{server_name}' not found.",
            }
        self._log(
            "info",
            "ToolManager | Tool Call Start",
            f"Calling tool '{tool_name}' on persistent server '{server_name}'",
            metadata={"arguments": arguments},
        )
        try:
            session = await self._session_for(server_name)
            tool_result = await session.call_tool(tool_name, arguments=arguments)
            result_content = (
                tool_result.content[-1].text if tool_result.content else ""
            )
            self._log(
                "info",
                "ToolManager | Tool Call Success",
                f"Tool '{tool_name}' (server: '{server_name}') called successfully.",
            )
            return {
                "server_name": server_name,
                "tool_name": tool_name,
                "result": result_content,
            }
        except Exception as exc:
            self._log(
                "error",
                "ToolManager | Tool Call Failed",
                f"Error: Failed to call tool '{tool_name}' (server: '{server_name}'): {exc}",
            )
            return {
                "server_name": server_name,
                "tool_name": tool_name,
                "error": f"Tool call failed: {exc}",
            }

    async def _session_for(self, server_name: str) -> Any:
        if self._closed:
            raise RuntimeError("persistent MCP tool manager is closed.")
        existing_session = self._sessions.get(server_name)
        if existing_session is not None:
            return existing_session
        server_params = self.server_dict.get(server_name)
        if server_params is None:
            raise RuntimeError(f"Server '{server_name}' not found.")
        try:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client
        except ModuleNotFoundError as exc:
            raise MiroThinkerAnalysisRuntimeError(
                "MCP client runtime is not available."
            ) from exc
        read, write = await self._exit_stack.enter_async_context(
            stdio_client(cast(Any, server_params))
        )
        new_session: Any = await self._exit_stack.enter_async_context(
            ClientSession(read, write, sampling_callback=None)
        )
        await new_session.initialize()
        self._sessions[server_name] = new_session
        return new_session


@dataclass(frozen=True, slots=True)
class MiroThinkerAnalysisRuntimeConfig:
    """Explicit runtime inputs required to execute one MiroThinker analysis pass."""

    vendor_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_max_context_length: int
    runtime_mode: str
    config_path: Path
    market_data_store_root: Path | None = None
    llm_reasoning_effort: str | None = None
    memory_read_policy: str = CURRENT_MEMORY_READ_POLICY
    runtime_scope: ContextPacketRuntimeScope = "live"
    run_id: str = ""
    wall_clock_timeout_seconds: int = 0
    structured_output_mode: AnalysisStructuredOutputMode = "disabled"
    structured_output_probe: bool = True
    execution_direction_mode: ExecutionDirectionMode = "long_short"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "vendor_root",
            _validate_existing_dir(self.vendor_root, field_name="vendor_root"),
        )
        object.__setattr__(
            self,
            "log_dir",
            _validate_path(self.log_dir, field_name="log_dir"),
        )
        object.__setattr__(
            self,
            "llm_provider",
            _validate_non_blank_text(self.llm_provider, field_name="llm_provider"),
        )
        object.__setattr__(
            self,
            "llm_model_name",
            _validate_non_blank_text(
                self.llm_model_name,
                field_name="llm_model_name",
            ),
        )
        object.__setattr__(
            self,
            "llm_api_key",
            _validate_non_blank_text(self.llm_api_key, field_name="llm_api_key"),
        )
        object.__setattr__(
            self,
            "llm_base_url",
            _validate_non_blank_text(self.llm_base_url, field_name="llm_base_url"),
        )
        if (
            not isinstance(self.llm_max_context_length, int)
            or isinstance(self.llm_max_context_length, bool)
            or self.llm_max_context_length <= 0
        ):
            raise MiroThinkerAnalysisRuntimeError(
                "llm_max_context_length must be a positive integer."
            )
        object.__setattr__(
            self,
            "llm_reasoning_effort",
            normalize_mirothinker_reasoning_effort(
                self.llm_reasoning_effort,
                field_name="llm_reasoning_effort",
                error_type=MiroThinkerAnalysisRuntimeError,
            ),
        )
        if self.runtime_mode not in {"live", "replay"}:
            raise MiroThinkerAnalysisRuntimeError(
                "runtime_mode must be 'live' or 'replay'."
            )
        if self.runtime_scope not in {"live", "replay"}:
            raise MiroThinkerAnalysisRuntimeError(
                "runtime_scope must be 'live' or 'replay'."
            )
        if self.runtime_scope == "live" and self.run_id:
            raise MiroThinkerAnalysisRuntimeError(
                "live analysis runtime must not carry run_id."
            )
        if self.runtime_scope == "replay":
            if not self.run_id:
                raise MiroThinkerAnalysisRuntimeError(
                    "replay analysis runtime requires run_id."
                )
        if not isinstance(self.memory_read_policy, str) or not self.memory_read_policy:
            raise MiroThinkerAnalysisRuntimeError(
                "memory_read_policy must be a non-empty string."
            )
        if (
            not isinstance(self.wall_clock_timeout_seconds, int)
            or isinstance(self.wall_clock_timeout_seconds, bool)
            or self.wall_clock_timeout_seconds < 0
        ):
            raise MiroThinkerAnalysisRuntimeError(
                "wall_clock_timeout_seconds must be a non-negative integer."
            )
        object.__setattr__(
            self,
            "structured_output_mode",
            normalize_analysis_structured_output_mode(self.structured_output_mode),
        )
        if not isinstance(self.structured_output_probe, bool):
            raise MiroThinkerAnalysisRuntimeError(
                "structured_output_probe must be a boolean."
            )
        object.__setattr__(
            self,
            "execution_direction_mode",
            validate_execution_direction_mode(
                self.execution_direction_mode,
                error_type=MiroThinkerAnalysisRuntimeError,
            ),
        )
        object.__setattr__(
            self,
            "config_path",
            _validate_path(self.config_path, field_name="config_path"),
        )
        if self.market_data_store_root is not None:
            object.__setattr__(
                self,
                "market_data_store_root",
                _validate_path(
                    self.market_data_store_root,
                    field_name="market_data_store_root",
                ),
            )


def build_mirothinker_analysis_runner(
    *,
    config: MiroThinkerAnalysisRuntimeConfig,
    layout: WorkspaceLayout,
) -> AnalysisCallback:
    """Build the committed analysis callback using vendored MiroThinker."""
    if not isinstance(layout, WorkspaceLayout):
        raise MiroThinkerAnalysisRuntimeError(
            "layout must be a WorkspaceLayout instance."
        )
    vendor_root = config.vendor_root.resolve(strict=False)
    workspace_root = layout.root.resolve(strict=False)
    # Fail before replay/live can persist AnalysisFailed artifacts for a
    # production runner that is not actually executable.
    _load_vendor_runtime(vendor_root)

    def analyze(context: AnalysisContext) -> AnalysisResult:
        if not isinstance(context, AnalysisContext):
            raise MiroThinkerAnalysisRuntimeError(
                "context must be an AnalysisContext instance."
            )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise MiroThinkerAnalysisRuntimeError(
                "build_mirothinker_analysis_runner currently supports only "
                "synchronous callers; do not call it from an active event loop."
            )

        return asyncio.run(
            _run_analysis_once(
                config=config,
                vendor_root=vendor_root,
                workspace_root=workspace_root,
                layout=layout,
                context=context,
            )
        )

    return analyze


async def _run_analysis_once(
    *,
    config: MiroThinkerAnalysisRuntimeConfig,
    vendor_root: Path,
    workspace_root: Path,
    layout: WorkspaceLayout,
    context: AnalysisContext,
) -> AnalysisResult:
    (
        _execute_task_pipeline,
        _OutputFormatter,
        _ToolManager,
        StdioServerParameters,
        _OmegaConf,
    ) = _load_vendor_runtime(vendor_root)

    config.log_dir.mkdir(parents=True, exist_ok=True)
    _prepare_vendor_environment(config)
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

    analysis_server_config = {
        "name": _ANALYSIS_MCP_SERVER_NAME,
        "params": StdioServerParameters(
            command=sys.executable,
            args=["-m", "event_trader.integrations.analysis_mcp_server"],
            env={
                "EVENT_TRADER_WORKSPACE_ROOT": str(workspace_root),
                _ANALYSIS_CONFIG_PATH_ENV: str(config.config_path),
                _ANALYSIS_STAGE_ROOT_ENV: str(stage_root),
                _ANALYSIS_RECEIPT_PATH_ENV: str(receipt_path),
                _ANALYSIS_CITATIONS_ENV: _serialize_active_citations(context),
                _ANALYSIS_CONTEXT_CITATIONS_ENV: _serialize_context_citations(context),
                _ANALYSIS_CONTEXT_PACKET_ENV: serialize_context_packet(context_packet),
                _ANALYSIS_TARGET_KEY_ENV: context.request.target_key,
                _ANALYSIS_BUSINESS_AT_ENV: business_at.isoformat(),
                _ANALYSIS_RUNTIME_SCOPE_ENV: config.runtime_scope,
                _ANALYSIS_EXECUTION_DIRECTION_MODE_ENV: (
                    config.execution_direction_mode
                ),
                _ANALYSIS_REQUIRED_WATCHLIST_PATH_ENV: (
                    f"targets/{context.request.target_key}/watchlist.md"
                    if context.request.requires_watchlist_maintenance
                    else ""
                ),
                **(
                    {
                        _ANALYSIS_MARKET_DATA_STORE_ROOT_ENV: str(
                            config.market_data_store_root
                        )
                    }
                    if config.market_data_store_root is not None
                    else {}
                ),
                "PYTHONPATH": _build_child_pythonpath(vendor_root),
            },
        ),
    }
    committed = False
    try:
        base_task_description = _build_analysis_task_prompt(
            context,
            context_packet=context_packet,
            runtime_mode=config.runtime_mode,
            execution_direction_mode=config.execution_direction_mode,
        )
        task_description = base_task_description
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
            try:
                run_result = await _run_supervised_strict_analysis_agent(
                    config=config,
                    vendor_root=vendor_root,
                    workspace_root=workspace_root,
                    stage_root=stage_root,
                    task_id=task_id,
                    task_description=task_description,
                    analysis_server_env=analysis_server_config["params"].env,
                )
                attempt_llm_usages.append(
                    _llm_usage_from_mirothinker_task_log(
                        log_file_path=run_result.log_file_path,
                        agent_role="analysis",
                        provider=config.llm_provider,
                        model=config.llm_model_name,
                    )
                )
            except _AnalysisContractRepairRequired as exc:
                if attempt_index + 1 >= _ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS:
                    raise MiroThinkerAnalysisRuntimeError(str(exc)) from None
                task_description = _append_analysis_contract_repair_feedback(
                    base_task_description,
                    exc,
                    execution_direction_mode=config.execution_direction_mode,
                )
                continue
            except _AnalysisWriteFailureBudgetExceeded as exc:
                raise MiroThinkerAnalysisRuntimeError(str(exc)) from None

            try:
                payload = _load_analysis_payload(
                    final_boxed_answer=run_result.final_boxed_answer,
                    final_summary=run_result.final_summary,
                    fallback_payload_texts=mirothinker_structured_output_fallback_texts_from_log(
                        run_result.log_file_path
                    ),
                    attempt_index=attempt_index,
                )
                payload = _finalize_analysis_payload(
                    payload,
                    attempt_index=attempt_index,
                    config=config,
                    context=context,
                    run_result=run_result,
                    included_lesson_ids=included_lesson_ids,
                )
                result = _normalize_analysis_result(
                    payload,
                    context=context,
                    attempt_index=attempt_index,
                    runtime_mode=config.runtime_mode,
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
                    raise MiroThinkerAnalysisRuntimeError(str(exc)) from exc
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
                        raise MiroThinkerAnalysisRuntimeError(str(exc)) from exc
                result = _with_runtime_analysis_assessment_id(result, context=context)
                write_receipts = _load_analysis_write_receipts(receipt_path)
                activity_receipts = write_receipts
                _assert_analysis_read_audit(
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
                    raise MiroThinkerAnalysisRuntimeError(str(exc)) from None
                except MiroThinkerAnalysisRuntimeError as exc:
                    repair_exc = _analysis_write_contract_repair_required(
                        exc,
                        context=context,
                        context_packet=context_packet,
                        attempt_index=attempt_index,
                    )
                    if repair_exc is None:
                        raise
                    if attempt_index + 1 >= _ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS:
                        raise MiroThinkerAnalysisRuntimeError(str(repair_exc)) from None
                    task_description = _append_analysis_contract_repair_feedback(
                        base_task_description,
                        repair_exc,
                        execution_direction_mode=config.execution_direction_mode,
                    )
                    continue
                break
            except _AnalysisContractRepairRequired as exc:
                if attempt_index + 1 >= _ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS:
                    raise MiroThinkerAnalysisRuntimeError(str(exc)) from None
                task_description = _append_analysis_contract_repair_feedback(
                    base_task_description,
                    exc,
                    execution_direction_mode=config.execution_direction_mode,
                )
            except MiroThinkerAnalysisRuntimeError:
                raise
        if result is None:
            raise MiroThinkerAnalysisRuntimeError(
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


def _assert_analysis_read_audit(
    *,
    receipts: tuple[dict[str, object], ...],
    context: AnalysisContext,
    context_packet: ContextPacket | None,
) -> None:
    _receipt, error_message = _analysis_read_audit_error(
        receipts=receipts,
        context=context,
        context_packet=context_packet,
    )
    if error_message is not None:
        raise _AnalysisContractRepairRequired(
            failure=_AnalysisContractFailure(
                surface="read_audit",
                error_code="analysis_read_audit_contract_violation",
                message=error_message,
                suggested_action=(
                    "Satisfy grounding coverage before final boxed JSON. Use "
                    "read_evidence for active event_ids not covered by compiled "
                    "Workbench excerpts, and rely on compiler-receipted Memory "
                    "Impact Lane cards or inspect target research memory with "
                    "explicit read tools."
                ),
            ),
            failure_count=1,
            threshold=_ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
        )


def _analysis_read_audit_error(
    *,
    receipts: tuple[dict[str, object], ...],
    context: AnalysisContext,
    context_packet: ContextPacket | None = None,
) -> tuple[GroundingCoverageReceipt, str | None]:
    active_event_ids = set(context.request.event_ids)
    compiled_event_ids = _compiled_workbench_evidence_event_ids(
        context_packet=context_packet,
        active_event_ids=active_event_ids,
    )
    compiled_memory_cards, compiler_memory_receipt_ids = _compiled_memory_grounding(
        context_packet=context_packet,
    )
    market_grounding_required = _market_grounding_required(context)
    compiled_market_count = _compiled_market_grounding_count(
        context_packet=context_packet,
        market_grounding_required=market_grounding_required,
    )
    read_event_ids: set[str] = set()
    memory_refs: set[str] = set()
    market_refs: set[str] = set()
    for receipt in receipts:
        operation = receipt.get("operation")
        if not isinstance(operation, str):
            continue
        if operation == "read_evidence":
            read_event_ids.update(_read_evidence_receipt_event_ids(receipt))
            continue
        if operation in _ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS:
            if receipt.get("result_mode") != "duplicate":
                market_refs.add(operation)
            continue
        if operation not in _ANALYSIS_MEMORY_READ_OPERATIONS:
            continue
        memory_ref = _target_memory_read_ref(
            receipt,
            target_key=context.request.target_key,
        )
        if memory_ref is not None:
            memory_refs.add(memory_ref)
    grounded_event_ids = compiled_event_ids | read_event_ids
    missing_event_ids = sorted(active_event_ids - grounded_event_ids)
    failed_reasons: list[str] = []
    if missing_event_ids:
        failed_reasons.append(f"missing_evidence_event_ids={missing_event_ids}")
    if not compiled_memory_cards and not memory_refs:
        failed_reasons.append(f"missing_target_memory_grounding={context.request.target_key!r}")
    if market_grounding_required and not compiled_market_count and not market_refs:
        failed_reasons.append(f"missing_market_grounding={context.request.target_key!r}")
    if missing_event_ids:
        evidence_grounding: Literal["compiled", "tool_read", "missing"] = "missing"
    elif compiled_event_ids:
        evidence_grounding = "compiled"
    else:
        evidence_grounding = "tool_read"
    coverage_receipt = GroundingCoverageReceipt(
        evidence_grounding=evidence_grounding,
        memory_grounding=(
            "compiled"
            if compiled_memory_cards
            else ("tool_read" if memory_refs else "missing")
        ),
        market_grounding=_market_grounding_status_for_receipt(
            required=market_grounding_required,
            compiled_market_count=compiled_market_count,
            market_refs=market_refs,
        ),
        compiled_evidence_event_ids=tuple(
            event_id for event_id in context.request.event_ids if event_id in compiled_event_ids
        ),
        tool_read_evidence_event_ids=tuple(
            event_id for event_id in context.request.event_ids if event_id in read_event_ids
        ),
        compiled_memory_cards=compiled_memory_cards,
        compiler_memory_read_receipt_ids=compiler_memory_receipt_ids,
        tool_read_memory_refs=tuple(sorted(memory_refs)),
        compiled_market_grounding_count=compiled_market_count,
        tool_read_market_refs=tuple(sorted(market_refs)),
        failed_reasons=tuple(failed_reasons),
    )
    if missing_event_ids:
        return coverage_receipt, (
            "Analysis grounding coverage failed: active evidence must be grounded by "
            "compiled Workbench evidence excerpts or read_evidence(event_ids). "
            f"missing_event_ids={missing_event_ids}."
        )
    if not compiled_memory_cards and not memory_refs:
        return coverage_receipt, (
            "Analysis grounding coverage failed: target memory must be grounded by "
            "Memory Impact Lane cards with compiler read receipts or explicit target "
            "ResearchMemory reads. "
            f"target_key={context.request.target_key!r}."
        )
    if market_grounding_required and not compiled_market_count and not market_refs:
        return coverage_receipt, (
            "Analysis grounding coverage failed: recap-sensitive active evidence "
            "requires compiled Market Lane grounding or explicit market tool reads. "
            f"target_key={context.request.target_key!r}."
        )
    return coverage_receipt, None


def _compiled_workbench_evidence_event_ids(
    *,
    context_packet: ContextPacket | None,
    active_event_ids: set[str],
) -> set[str]:
    if context_packet is None or context_packet.analysis_workbench is None:
        return set()
    return {
        card.event_id
        for card in context_packet.analysis_workbench.evidence_lane.active_records
        if card.event_id in active_event_ids
        and card.source_excerpt is not None
        and card.grounding_status
        in {"compiled_excerpt_available", "compiled_excerpt_truncated"}
    }


def _compiled_memory_grounding(
    *,
    context_packet: ContextPacket | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if context_packet is None or context_packet.analysis_workbench is None:
        return (), ()
    available_cards = context_packet.analysis_workbench.memory_impact_lane.available_cards
    if not available_cards:
        return (), ()
    receipts_by_id: dict[str, dict[str, object]] = {}
    for receipt in context_packet.receipts:
        if not isinstance(receipt, dict):
            continue
        if receipt.get("receipt_type") != "workbench_compiler_memory_read":
            continue
        receipt_id = receipt.get("receipt_id")
        if isinstance(receipt_id, str) and receipt_id.strip():
            receipts_by_id[receipt_id.strip()] = receipt
    compiled_card_ids: list[str] = []
    receipt_ids: list[str] = []
    for card in available_cards:
        matched_receipt = receipts_by_id.get(card.compiler_read_receipt_id)
        if matched_receipt is None:
            return (), ()
        if not _compiler_receipt_matches_card(matched_receipt, card=card):
            return (), ()
        compiled_card_ids.append(card.card_id)
        receipt_ids.append(card.compiler_read_receipt_id)
    return tuple(compiled_card_ids), tuple(receipt_ids)


def _market_grounding_required(context: AnalysisContext) -> bool:
    return any(
        _market_grounding_required_for_record(record)
        for record in context.evidence_records
        if record.event_id in set(context.request.event_ids)
    )


def _market_grounding_required_for_record(record: EvidenceLedgerRecord) -> bool:
    dimensions = classify_evidence_review_dimensions(record)
    return (
        dimensions.recap_risk in {"medium", "high"}
        or dimensions.evidence_role == "price_recap"
        or "event_type:price_action" in record.labels
    )


def _compiled_market_grounding_count(
    *,
    context_packet: ContextPacket | None,
    market_grounding_required: bool,
) -> int:
    if not market_grounding_required:
        return 0
    if context_packet is None or context_packet.analysis_workbench is None:
        return 0
    return 1


def _market_grounding_status_for_receipt(
    *,
    required: bool,
    compiled_market_count: int,
    market_refs: set[str],
) -> Literal["compiled", "tool_read", "not_required", "missing"]:
    if not required:
        return "not_required"
    if compiled_market_count:
        return "compiled"
    if market_refs:
        return "tool_read"
    return "missing"


def _compiler_receipt_matches_card(
    receipt: dict[str, object],
    *,
    card: Any,
) -> bool:
    return (
        receipt.get("page_path") == card.page_path
        and receipt.get("section_name") == card.section_name
        and receipt.get("content_sha256") == card.content_hash
        and receipt.get("excerpt_sha256") == card.excerpt_hash
        and receipt.get("surface_type") == "research_memory_section"
    )


def _target_memory_read_ref(
    receipt: dict[str, object],
    *,
    target_key: str,
) -> str | None:
    target_prefix = f"targets/{target_key}/"
    accepted_scopes = {
        f"target:{target_key}",
        f"target/{target_key}",
        f"targets/{target_key}",
    }
    page_path = receipt.get("page_path")
    if isinstance(page_path, str):
        normalized_page_path = page_path.replace("\\", "/").strip()
        if normalized_page_path.startswith(target_prefix):
            section_name = receipt.get("section_name")
            if isinstance(section_name, str) and section_name.strip():
                return f"{normalized_page_path}:{section_name.strip()}"
            return normalized_page_path
    scope = receipt.get("scope")
    if isinstance(scope, str) and scope.strip() in accepted_scopes:
        operation = receipt.get("operation")
        return f"{operation}:{scope.strip()}" if isinstance(operation, str) else scope.strip()
    return None


def _read_evidence_receipt_event_ids(receipt: dict[str, object]) -> set[str]:
    records = receipt.get("records")
    if not isinstance(records, list):
        return set()
    event_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        event_id = record.get("event_id")
        if isinstance(event_id, str) and event_id.strip():
            event_ids.add(event_id.strip())
    return event_ids


def _append_analysis_contract_repair_feedback(
    task_description: str,
    exc: _AnalysisContractRepairRequired,
    *,
    execution_direction_mode: ExecutionDirectionMode,
) -> str:
    failure = exc.failure
    details = failure.details or {}
    schema_contract_md = (
        details.get("schema_contract_md")
        if isinstance(details.get("schema_contract_md"), str)
        else analysis_final_payload_contract_markdown(
            execution_direction_mode=execution_direction_mode
        )
    )
    write_tool_info = ""
    if failure.surface == "write_tool":
        tool_name = details.get("tool_name")
        error_code = details.get("error_code") or failure.error_code
        failed_arguments = details.get("arguments")
        write_tool_info = (
            f"- tool: {tool_name}\n"
            f"- error_code: {error_code}\n"
            f"- failed_arguments: "
            f"{_shorten_error_text(_json_compact(failed_arguments), max_length=600)}\n"
            f"- allowed_top_sections: {_json_compact(details.get('allowed_top_sections', []))}\n"
            f"- available_headings: {_json_compact(details.get('available_headings', []))}\n"
            f"- suggested_action: {details.get('suggested_action', '')}\n"
        )
    write_tool_section = f"{write_tool_info}\n" if write_tool_info else ""
    return (
        f"{task_description.rstrip()}\n\n"
        "Previous analysis attempt failed a deterministic event-trader "
        "analysis contract and should be repaired without human input:\n"
        f"- surface: {failure.surface}\n"
        f"- error_code: {failure.error_code}\n"
        f"- error: {_shorten_error_text(failure.message, max_length=500)}\n"
        f"- suggested_action: {failure.suggested_action}\n"
        f"{write_tool_section}"
        f"- details: {_json_compact(details)}\n\n"
        "Repeat the same analysis task. Before returning final output, use required "
        "read tools again as needed, then return exactly one corrected JSON object wrapped "
        "in \\boxed{...}.\n"
        f"{schema_contract_md}\n"
        "Do not include outcome, view_state_change, decision_audit, target "
        "weights, action fields, or any runtime-derived fields.\n"
        "event_ids must exactly equal the active request event_id sequence in order.\n"
        "used_lesson_ids must contain only IDs from included visible learning cards, "
        "or [] when no lesson shaped the decision.\n"
        "Analysis is exposure-blind: do not infer actual current position, "
        "do not propose target_weight, and do not open, close, reverse, or resize.\n"
        "If write recovery is needed, keep the required read/write repair instructions in mind and "
        "do not ask for HITL. Do not invent evidence and do not cite unloaded event_ids.\n"
    )


def _analysis_write_contract_repair_required(
    exc: MiroThinkerAnalysisRuntimeError,
    *,
    context: AnalysisContext,
    context_packet: ContextPacket | None,
    attempt_index: int,
) -> _AnalysisContractRepairRequired | None:
    message = str(exc)
    if message.startswith(
        (
            "market_setup_dashboard_empty",
            "market_setup_dashboard_state_label_forbidden",
            "market_setup_dashboard_incomplete",
            "market_setup_dashboard_market_reference_empty",
            "market_setup_dashboard_market_reference_semantics_missing",
            "market_setup_dashboard_setup_frame_empty",
            "market_setup_dashboard_setup_frame_incomplete",
            "market_setup_dashboard_exposure_language_forbidden",
            "market_setup_dashboard_legacy_section_forbidden",
            "market_setup_dashboard_missing",
            "market_setup_dashboard_invalid_markdown",
        )
    ):
        return _AnalysisContractRepairRequired(
            failure=_AnalysisContractFailure(
                surface="final_output",
                error_code="market_setup_dashboard_contract_error",
                message=message,
                suggested_action=(
                    "Repair the thesis.md Market Setup Dashboard write. The "
                    "section body must be exposure-blind market setup memory. "
                    "Include `### Market Reference`, `### Setup Frame`, "
                    "`### Watch Triggers`, and `### Evidence Gaps`; do not "
                    "claim actual current exposure, target weight, entry, PnL, "
                    "MFE, MAE, or open/close/reverse/resize action."
                ),
                details={
                    "target_key": context.request.target_key,
                },
            ),
            failure_count=attempt_index + 1,
            threshold=_ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
        )
    if not message.startswith(_ANALYSIS_WRITE_SUPPORT_UNGROUNDED_PREFIX):
        return None
    unit_formation_lane = _analysis_unit_formation_lane(
        context=context,
        context_packet=context_packet,
    )
    details: dict[str, object] = {
        "missing_claims": tuple(
            claim.strip()
            for claim in message[len(_ANALYSIS_WRITE_SUPPORT_UNGROUNDED_PREFIX) :].split(",")
            if claim.strip()
        ),
    }
    if unit_formation_lane is not None:
        details.update(
            {
                "analysis_unit_id": unit_formation_lane.analysis_unit_id,
                "event_time_completeness_claim": (
                    unit_formation_lane.event_time_completeness_claim
                ),
                "advanced_completeness_requirements": (
                    unit_formation_lane.advanced_completeness_requirements
                ),
                "missing_completeness_requirements": (
                    unit_formation_lane.missing_completeness_requirements
                ),
            }
        )
    return _AnalysisContractRepairRequired(
        failure=_AnalysisContractFailure(
            surface="final_output",
            error_code="write_support_not_grounded_or_advanced",
            message=message,
            suggested_action=(
                "Repair the analysis write payloads. Use absence_based_support or "
                "maturity_based_support only when the matching UnitFormationLane "
                "requirement is advanced, or when support_refs point to explicit "
                "grounded read/tool receipts. For positive evidence or price-action "
                "recap updates, omit absence_based_support/maturity_based_support "
                "instead of claiming missing follow-up or market maturity."
            ),
            details=details,
        ),
        failure_count=attempt_index + 1,
        threshold=_ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
    )


def _json_compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load_vendor_runtime(vendor_root: Path) -> tuple[Any, Any, Any, Any, Any]:
    _append_vendor_import_roots(vendor_root)
    try:
        pipeline_module = importlib.import_module("src.core.pipeline")
        output_formatter_module = importlib.import_module("src.io.output_formatter")
        omegaconf_module = importlib.import_module("omegaconf")
        manager_module = importlib.import_module("miroflow_tools.manager")
        mcp_module = importlib.import_module("mcp")
    except ModuleNotFoundError as exc:
        missing_dependency = exc.name or str(exc)
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis runtime dependencies are not installed. "
            "Install the vendored runtime dependencies before using this adapter. "
            f"Missing module: {missing_dependency}."
        ) from exc

    return (
        pipeline_module.execute_task_pipeline,
        output_formatter_module.OutputFormatter,
        manager_module.ToolManager,
        mcp_module.StdioServerParameters,
        omegaconf_module.OmegaConf,
    )


def _build_analysis_tool_manager(
    *,
    tool_manager_cls: object,
    server_config: dict[str, object],
) -> Any:
    if _is_vendor_miroflow_tool_manager(tool_manager_cls):
        return _PersistentStdioMcpToolManager([server_config])
    if not callable(tool_manager_cls):
        raise MiroThinkerAnalysisRuntimeError("ToolManager must be callable.")
    return tool_manager_cls([server_config])


def _is_vendor_miroflow_tool_manager(tool_manager_cls: object) -> bool:
    return (
        getattr(tool_manager_cls, "__module__", "") == "miroflow_tools.manager"
        and getattr(tool_manager_cls, "__name__", "") == "ToolManager"
    )


async def _close_analysis_tool_manager(tool_manager: object) -> None:
    close_tool_manager = getattr(tool_manager, "aclose", None)
    if callable(close_tool_manager):
        await close_tool_manager()


async def _run_supervised_strict_analysis_agent(
    *,
    config: MiroThinkerAnalysisRuntimeConfig,
    vendor_root: Path,
    workspace_root: Path,
    stage_root: Path,
    task_id: str,
    task_description: str,
    analysis_server_env: Mapping[str, str],
) -> StrictMiroThinkerAgentRunResult:
    supervision_root = stage_root / "supervision"
    supervision_root.mkdir(parents=True, exist_ok=True)
    input_path = supervision_root / "worker_input.json"
    output_path = supervision_root / "worker_output.json"
    payload = {
        "vendor_root": str(vendor_root),
        "workspace_root": str(workspace_root),
        "log_dir": str(config.log_dir),
        "llm_provider": config.llm_provider,
        "llm_model_name": config.llm_model_name,
        "llm_base_url": config.llm_base_url,
        "llm_api_key_env": "EVENT_TRADER_ANALYSIS_WORKER_LLM_API_KEY",
        "llm_max_context_length": config.llm_max_context_length,
        "llm_reasoning_effort": config.llm_reasoning_effort,
        "task_id": task_id,
        "task_description": task_description,
        "analysis_server_env": dict(analysis_server_env),
    }
    _atomic_write_json(input_path, payload)
    if output_path.exists():
        output_path.unlink()

    child_env = os.environ.copy()
    child_env["EVENT_TRADER_ANALYSIS_WORKER_LLM_API_KEY"] = config.llm_api_key
    cwd = Path.cwd()
    child_env["PYTHONPATH"] = _merge_pythonpath(
        os.environ.get("PYTHONPATH"),
        (str(cwd / "src"), str(cwd)),
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "event_trader.integrations.mirothinker_analysis_worker",
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        cwd=str(cwd),
        env=child_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if config.wall_clock_timeout_seconds == 0:
            stdout, stderr = await process.communicate()
        else:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=config.wall_clock_timeout_seconds,
            )
    except TimeoutError as exc:
        await _terminate_process_tree(process)
        stdout, stderr = await process.communicate()
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis vendor worker timed out after "
            f"{config.wall_clock_timeout_seconds} seconds "
            f"(task_id={task_id}, pid={process.pid}). "
            f"stdout={_decode_process_stream(stdout)} "
            f"stderr={_decode_process_stream(stderr)}"
        ) from exc

    result_payload = _load_supervision_output(output_path)
    worker_status = result_payload.get("status")
    if worker_status == "analysis_contract_repair_required":
        raise _analysis_contract_repair_from_supervision_payload(result_payload)
    if worker_status == "analysis_write_failure_budget_exceeded":
        raise _AnalysisWriteFailureBudgetExceeded(
            _optional_supervision_failure_text(result_payload, "error")
            or "Analysis write failure budget exceeded in supervised worker."
        )
    if process.returncode != 0 or result_payload.get("status") != "success":
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis vendor worker failed "
            f"(task_id={task_id}, returncode={process.returncode}, "
            f"status={result_payload.get('status')!r}, "
            f"error={result_payload.get('error')!r}, "
            f"stdout={_decode_process_stream(stdout)}, "
            f"stderr={_decode_process_stream(stderr)})."
        )
    run_result = result_payload.get("run_result")
    if not isinstance(run_result, dict):
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis vendor worker returned malformed run_result."
        )
    final_summary = run_result.get("final_summary")
    final_boxed_answer = run_result.get("final_boxed_answer")
    log_file_path = run_result.get("log_file_path")
    if not isinstance(final_summary, str):
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis vendor worker final_summary must be a string."
        )
    if not isinstance(final_boxed_answer, str):
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis vendor worker final_boxed_answer must be a string."
        )
    if not isinstance(log_file_path, str) or not log_file_path.strip():
        raise MiroThinkerAnalysisRuntimeError(
            "MiroThinker analysis vendor worker log_file_path must be non-blank."
        )
    return StrictMiroThinkerAgentRunResult(
        final_summary=final_summary,
        final_boxed_answer=final_boxed_answer,
        log_file_path=log_file_path,
        failure_experience_summary=run_result.get("failure_experience_summary"),
    )


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        suffix=".tmp",
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _load_supervision_output(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "status": "missing_output",
            "error": f"worker output file was not written: {path}",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "malformed_output",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        return {
            "status": "malformed_output",
            "error": "worker output payload must be a JSON object.",
        }
    return payload


def _decode_process_stream(value: bytes | None, *, max_chars: int = 4_000) -> str:
    if not value:
        return ""
    decoded = value.decode("utf-8", errors="replace").strip()
    if len(decoded) <= max_chars:
        return decoded
    return f"{decoded[:max_chars]}..."


def _merge_pythonpath(existing: str | None, required: Sequence[str]) -> str:
    parts = [item for item in required if item]
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: int = 10,
) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        taskkill = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await taskkill.wait()
    else:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()


def _install_analysis_write_failure_budget_guard(
    *,
    tool_manager: Any,
    consecutive_failure_budget: int = _ANALYSIS_WRITE_FAILURE_BUDGET,
    same_error_abort_threshold: int = _ANALYSIS_SAME_WRITE_ERROR_ABORT_THRESHOLD,
) -> None:
    original_execute_tool_call = getattr(tool_manager, "execute_tool_call", None)
    if not callable(original_execute_tool_call):
        raise MiroThinkerAnalysisRuntimeError(
            "Could not install analysis write failure budget guard; "
            "ToolManager execute_tool_call is unavailable."
        )

    failure_count = 0
    same_failure_count = 0
    same_failure_key: tuple[str, str, str, str] | None = None
    last_fail_tool = ""
    last_fail_error = ""

    def reset_failure_state() -> None:
        nonlocal failure_count, same_failure_count, same_failure_key
        failure_count = 0
        same_failure_count = 0
        same_failure_key = None

    async def guarded_execute_tool_call(
        *args: object,
        **kwargs: object,
    ) -> Any:
        nonlocal failure_count, same_failure_count, same_failure_key
        nonlocal last_fail_tool, last_fail_error
        server_name, tool_name, arguments = _normalize_tool_call_args(args, kwargs)
        tool_result = await original_execute_tool_call(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
        )
        if (
            server_name != _ANALYSIS_MCP_SERVER_NAME
            or tool_name not in _ANALYSIS_WRITE_TOOL_NAMES
        ):
            return tool_result

        failure = _analysis_write_tool_failure(
            tool_name=tool_name,
            arguments=arguments,
            tool_result=tool_result,
        )
        if failure is None:
            reset_failure_state()
            return tool_result

        if not failure.recoverable:
            raise _AnalysisWriteFailureBudgetExceeded(
                "Non-recoverable analysis write tool failure: "
                f"tool={failure.tool_name} error_code={failure.error_code}: "
                f"{_shorten_error_text(failure.message, max_length=180)}"
            )

        failure_count += 1
        failure_key = _analysis_write_failure_key(failure)
        if same_failure_key == failure_key:
            same_failure_count += 1
        else:
            same_failure_key = failure_key
            same_failure_count = 1
        last_fail_tool = tool_name
        last_fail_error = _shorten_error_text(failure.message, max_length=180)
        if (
            failure.payload is not None
            and same_failure_count >= same_error_abort_threshold
        ):
            raise _AnalysisContractRepairRequired(
                failure=_analysis_contract_failure_for_write_tool(failure),
                failure_count=same_failure_count,
                threshold=same_error_abort_threshold,
            )
        if failure_count >= consecutive_failure_budget:
            raise _AnalysisWriteFailureBudgetExceeded(
                "Analysis write failure budget exceeded: "
                f"{failure_count} consecutive analysis write tool failures "
                f"(threshold={consecutive_failure_budget}). Last failure was "
                f"tool={last_fail_tool}: {last_fail_error}"
            )
        return tool_result

    tool_manager.execute_tool_call = guarded_execute_tool_call
    tool_manager.reset_analysis_write_failure_budget_guard = reset_failure_state


def _reset_analysis_write_failure_budget_guard(*, tool_manager: Any) -> None:
    reset = getattr(tool_manager, "reset_analysis_write_failure_budget_guard", None)
    if callable(reset):
        reset()


def _normalize_tool_call_args(
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> tuple[str, str, object]:
    if args:
        if len(args) != 3:
            raise TypeError("execute_tool_call expects server_name, tool_name, arguments.")
        server_name, tool_name, arguments = args
    else:
        server_name = kwargs.get("server_name")
        tool_name = kwargs.get("tool_name")
        arguments = kwargs.get("arguments")
    if not isinstance(server_name, str) or not isinstance(tool_name, str):
        raise TypeError("execute_tool_call expects server_name and tool_name as strings.")
    return server_name, tool_name, arguments


def _analysis_write_tool_failure(
    *,
    tool_name: str,
    arguments: object,
    tool_result: object,
) -> _AnalysisWriteToolFailure | None:
    payload = _analysis_write_tool_error_payload(tool_result)
    if payload is not None:
        message = _require_mapping_text(payload, "error") or "analysis write tool failed"
        error_code = _require_mapping_text(payload, "error_code") or "write_tool_error"
        recoverable_value = payload.get("recoverable")
        recoverable = recoverable_value if isinstance(recoverable_value, bool) else True
        return _AnalysisWriteToolFailure(
            tool_name=tool_name,
            error_code=error_code,
            message=message,
            recoverable=recoverable,
            arguments=arguments,
            payload=payload,
        )

    failure_text = _analysis_write_tool_error_text(tool_result)
    if failure_text is None:
        return None
    return _AnalysisWriteToolFailure(
        tool_name=tool_name,
        error_code="write_tool_error",
        message=failure_text,
        recoverable=True,
        arguments=arguments,
        payload=None,
    )


def _analysis_contract_failure_for_write_tool(
    failure: _AnalysisWriteToolFailure,
) -> _AnalysisContractFailure:
    details: dict[str, object] = {
        "tool_name": failure.tool_name,
        "arguments": failure.arguments,
        "error_code": failure.error_code,
        "error": failure.message,
        "recoverable": failure.recoverable,
    }
    if failure.payload is not None:
        details.update(failure.payload)
    return _AnalysisContractFailure(
        surface="write_tool",
        error_code=failure.error_code,
        message=failure.message,
        recoverable=failure.recoverable,
        suggested_action=(
            _require_mapping_text(failure.payload, "suggested_action")
            if failure.payload is not None
            else "Retry only if the write-tool error is a mechanical contract issue."
        )
        or "",
        details=details,
        arguments=failure.arguments,
    )


def _analysis_write_failure_key(
    failure: _AnalysisWriteToolFailure,
) -> tuple[str, str, str, str]:
    payload = failure.payload or {}
    page_path = payload.get("page_path")
    section_name = payload.get("section_name")
    return (
        failure.tool_name,
        failure.error_code,
        page_path if isinstance(page_path, str) else "",
        section_name if isinstance(section_name, str) else "",
    )


def _analysis_write_tool_error_payload(
    tool_result: object,
) -> Mapping[str, object] | None:
    raw_result: object
    if isinstance(tool_result, Mapping):
        raw_error = tool_result.get("error")
        if isinstance(raw_error, str) and raw_error.strip():
            return {
                "error": raw_error.strip(),
                "error_code": "write_tool_error",
                "recoverable": True,
            }
        raw_result = tool_result.get("result")
    else:
        raw_result = tool_result

    if isinstance(raw_result, Mapping):
        raw_error = raw_result.get("error")
        if isinstance(raw_error, str) and raw_error.strip():
            return raw_result
        return None
    if not isinstance(raw_result, str):
        return None
    normalized = raw_result.strip()
    if not normalized:
        return None
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    raw_error = payload.get("error")
    if isinstance(raw_error, str) and raw_error.strip():
        return payload
    return None


def _require_mapping_text(payload: Mapping[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _analysis_write_tool_error_text(tool_result: object) -> str | None:
    if isinstance(tool_result, Mapping):
        error = tool_result.get("error")
        if isinstance(error, str) and error.strip():
            return f"Tool call failed: {error.strip()}"
        raw_result = tool_result.get("result")
        return _analysis_write_result_error_text(raw_result)
    return _analysis_write_result_error_text(tool_result)


def _analysis_write_result_error_text(result: object) -> str | None:
    if isinstance(result, str):
        normalized = result.strip()
        if normalized.startswith("Error executing tool "):
            return normalized
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
        return None
    if isinstance(result, Mapping):
        error = result.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
    return None


def _shorten_error_text(value: str, *, max_length: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 3]}..."


def _append_vendor_import_roots(vendor_root: Path) -> None:
    try:
        prepare_mirothinker_runtime(vendor_root)
    except MiroThinkerRuntimePathError as exc:
        raise MiroThinkerAnalysisRuntimeError(str(exc)) from exc


def _prepare_vendor_environment(config: MiroThinkerAnalysisRuntimeConfig) -> None:
    env_updates = {
        "OPENAI_API_KEY": config.llm_api_key,
        "OPENAI_BASE_URL": config.llm_base_url,
    }
    for key, value in env_updates.items():
        os.environ[key] = value


def _build_child_pythonpath(vendor_root: Path) -> str:
    project_src = Path(__file__).resolve(strict=False).parents[2]
    return build_mirothinker_child_pythonpath(
        vendor_root=vendor_root,
        project_src=project_src,
    )


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


def _build_analysis_task_prompt(
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
        "infer actual current position or target weight. Call tools when lanes "
        "are insufficient, truncated, stale, contradictory, or needed for "
        "citations/writes.\n\n"
        f"{analysis_workbench_context}"
        "Runtime brief:\n"
        "Deterministic index of active evidence and target memory "
        "surfaces. Use tools to read the specific source or wiki text needed for "
        "decision:\n"
        f"{_render_context_brief(context, runtime_mode=runtime_mode)}\n\n"
        f"{learning_card_context}"
        f"{claim_card_context}"
        "Grounding Coverage Rules:\n"
        "- Active evidence grounding is satisfied by compiled Workbench evidence "
        "excerpts or explicit read_evidence results.\n"
        "- Target memory grounding is satisfied by Memory Impact Lane cards with "
        "compiler read receipts or explicit target ResearchMemory read tools.\n"
        "- Recap-sensitive market grounding is satisfied by compiled Market Lane "
        "facts or explicit market tool reads; non-recap events do not require "
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
        "- Use target ResearchMemory read tools when Memory Impact Lane cards are "
        "missing, empty, truncated with material omitted detail, contradictory, "
        "or needed for exact quotation, contextual citation, or write support.\n"
        "- Use read_evidence(event_ids) when a compiled source excerpt is missing, "
        "when a truncated excerpt omits text that matters, when source wording is "
        "contradictory or ambiguous, or when exact quotation/source wording is "
        "needed for a claim or write.\n"
        "- Keep admitted evidence facts, market context facts, subjective "
        "interpretation, ResearchMemory implications, and PMReview reasons "
        "separate in your reasoning and writes.\n"
        "- If market_context_terminal is available, call market tools when "
        "decision-time market facts matter, when Market Lane is insufficient, "
        "stale, contradictory, or when detailed market evidence is needed. Do "
        "not infer unavailable indicators or treat option/cross-asset context "
        "as mechanical trade signals.\n"
        "- Market Lane recap_reconciliation is low-commitment factual coverage; "
        "it is not a priced-in/no-update policy and does not mechanically "
        "recommend a direct position action.\n"
        "- Do not write raw market tool payloads into ResearchMemory; only write "
        "market facts that change durable cognition.\n"
        "- The active-section surfaces "
        "`thesis.md / Market Setup Dashboard`, `thesis.md / Invalidation`, "
        "`watchlist.md / Immediate Watch Items`, "
        "`watchlist.md / Triggers To Escalate`, and "
        "`risks.md / Failure Conditions` are commit-time deterministic "
        "projections from structured assessment truth, not analysis-authored "
        "narrative targets.\n"
        "- Inspect target research memory with read_page, read_section, "
        "read_around_citation, list_pages, or search_wiki scoped to the active "
        "target before deciding.\n"
        "- Do not treat missing body text in the brief as evidence of absence; "
        "use read tools for source and wiki text.\n"
        "- Research claim cards are ClaimRegistry projections, not admitted "
        "evidence facts; verify before writing.\n"
        "- Grounding coverage audit accepts compiled Workbench evidence excerpts "
        "or explicit evidence reads for active evidence, and accepts compiler-"
        "receipted Memory Impact Lane cards or explicit target ResearchMemory "
        "reads for memory grounding. For recap-sensitive events, it accepts "
        "compiled Market Lane facts or explicit market tools for market "
        "grounding.\n"
        "- Use list_pages/search_wiki to discover relevant pages, read_page to "
        "inspect bounded page excerpts and page_map, read_section(page_path, "
        "section_name) to load a specific heading, and "
        "read_around_citation(page_path, event_id) to load local context around "
        "a cited event. Prefer read_section/read_around_citation over broad "
        "page reads when a page is truncated.\n"
        f"- list_pages/search_wiki scopes are `shared`, `target:{context.request.target_key}`, "
        f"`target/{context.request.target_key}`, or `targets/{context.request.target_key}`.\n"
        "- The final JSON event_ids field must contain only the active request "
        "event_ids in the same order. Do not add contextual event_ids you read "
        "from evidence or research memory.\n"
        "- Before returning final boxed JSON with a non-null analysis_assessment, "
        "call validate_analysis_final_payload once with the exact payload you intend "
        "to return. If it returns ok=false, repair the JSON and call the validator "
        "again. validate_analysis_final_payload must be your last tool call. After "
        "it returns ok=true, do not call any tool again, do not rewrite the payload, "
        "and return that exact same JSON byte-for-byte in the final boxed answer. "
        "The runtime final parser remains authoritative.\n"
        "- If no durable cognition changed, do not call any write tool.\n"
        "- If durable cognition changed, update the wiki directly using the "
        "write tools.\n"
        "- Use `research-claim` blocks for material thesis/risk/invalidation "
        "writes; omissions are receipted as claim_coverage_status=missing.\n"
        "- If requires_watchlist_maintenance is true and you update durable "
        "cognition, maintain "
        f"`targets/{context.request.target_key}/watchlist.md` with concrete next "
        "attention triggers, invalidation checks, or monitoring conditions.\n"
        '- Use readable evidence citations only in the exact form '
        '"`event_id` | `source_ref`". Bare event_ids or parenthesized '
        "event references are invalid mechanical format errors.\n"
        "- Material wiki writes must cite at least one active request evidence "
        "pair. They may also cite contextual evidence pairs, but only when those "
        "pairs came from evidence or research-memory content you loaded this pass.\n"
        "- Use update_page_section when a section rewrite is enough.\n"
        "- update_page_section rewrites only the body inside the named section; "
        "new_content_md must not include the page title, section heading, or "
        "required top-level headings.\n"
        f"{support_payload_guidance}"
        "- On fixed target pages, update_page_section may target canonical or "
        "unique nested page_map headings.\n"
        "- Use rewrite_page only when the current page structure is materially "
        "wrong or incoherent.\n"
        "- rewrite_page for thesis.md, risks.md, watchlist.md, and timeline.md "
        "must include the full fixed page skeleton with each required top-level "
        "section exactly once and in canonical order.\n"
        "- Use create_page only when a subject has become a durable research object.\n"
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
        "tool reads together.\n\n"
        f"{_view_state_discipline_prompt()}"
        f"{analysis_final_payload_contract_markdown(execution_direction_mode=execution_direction_mode)}\n\n"
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
            "continue the ordinary workflow. Use read_section on "
            "targets/<target>/operator.md only when a bounded excerpt is "
            "insufficient."
        ),
        "market_setup_note": (
            "Market Setup Dashboard is the canonical analysis-owned setup "
            "memory. The market_setup_lane identifies this exposure-blind "
            "surface and must not be treated as actual portfolio exposure."
        ),
    }
    return (
        "Analysis Workbench:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
    )


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
        page
        for page in context.memory_context.target_pages
        if page.page_path == expected_page_path
    ]
    if len(target_index_pages) != 1:
        raise MiroThinkerAnalysisRuntimeError(
            "analysis runtime brief must include exactly one target index.md page."
        )
    if context.memory_context.shared_pages:
        raise MiroThinkerAnalysisRuntimeError(
            "analysis runtime brief must not include shared wiki pages."
        )
    payload = {
        "active_evidence": [
            _serialize_record_brief(record) for record in context.evidence_records
        ],
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
        "deeper_read_tools": {
            "evidence": (
                "Use read_evidence(event_ids) for missing excerpts, omitted text, "
                "ambiguity, conflict, exact wording, or quotation support."
            ),
            "target_memory": (
                "Inspect target memory with read_page, read_section, "
                "read_around_citation, list_pages, or search_wiki for wiki body text."
            ),
            "market_context": (
                "Call bounded market terminal tools for relevant market facts."
            ),
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
        "usage_rule": (
            "Use market tools for market facts; do not copy raw payloads."
        ),
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
        raise MiroThinkerAnalysisRuntimeError(
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
    return serialize_analysis_citations(
        AnalysisCitation(event_id=record.event_id, source_ref=record.source_ref)
        for record in context.evidence_records
    )


def _serialize_context_citations(context: AnalysisContext) -> str:
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
            raise MiroThinkerAnalysisRuntimeError(
                "bounded analysis page excerpt must be a string."
            )
        for citation in extract_analysis_citations(visible_content):
            event_id = citation.event_id
            if event_id in active_event_ids or event_id in seen:
                continue
            seen.add(event_id)
            citations.append(citation)
    payload = [
        {"event_id": citation.event_id, "source_ref": citation.source_ref}
        for citation in citations
    ]
    return json.dumps(payload, ensure_ascii=False)


def _load_analysis_payload(
    *,
    final_boxed_answer: str,
    final_summary: str,
    fallback_payload_texts: Iterable[str] = (),
    attempt_index: int = 0,
    task_id: str | None = None,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    del task_id, log_dir
    missing_boxed_json_error = (
        "MiroThinker analysis agent did not return a boxed JSON payload. "
        f"Final summary was: {final_summary}"
    )
    non_json_error = "MiroThinker analysis agent returned non-JSON boxed output."
    non_object_error = "MiroThinker analysis agent must return one JSON object."
    try:
        return load_first_boxed_json_object(
            final_boxed_answer=final_boxed_answer,
            final_summary=final_summary,
            fallback_payload_texts=fallback_payload_texts,
            object_start_fields=(
                "target_key",
                "event_ids",
                "used_lesson_ids",
                "analysis_assessment",
            ),
            missing_error=missing_boxed_json_error,
            non_json_error=non_json_error,
            non_object_error=non_object_error,
        )
    except BoxedJsonPayloadError as exc:
        message = str(exc)
        if message == missing_boxed_json_error:
            failure_code = "missing_boxed_json"
        elif message == non_json_error:
            failure_code = "boxed_non_json"
        elif message == non_object_error:
            failure_code = "boxed_non_object"
        else:
            raise MiroThinkerAnalysisRuntimeError(message) from exc
        raise _AnalysisContractRepairRequired(
            failure=_AnalysisContractFailure(
                surface="final_output",
                error_code=failure_code,
                message=message,
            ),
            failure_count=attempt_index + 1,
            threshold=_ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
        ) from None


def _finalize_analysis_payload(
    payload: dict[str, Any],
    *,
    attempt_index: int = 0,
    config: MiroThinkerAnalysisRuntimeConfig,
    context: AnalysisContext,
    run_result: StrictMiroThinkerAgentRunResult,
    included_lesson_ids: tuple[str, ...],
) -> dict[str, Any]:
    if config.structured_output_mode == "disabled":
        return payload
    execution_direction_mode = validate_execution_direction_mode(
        getattr(config, "execution_direction_mode", "long_short"),
        error_type=MiroThinkerAnalysisRuntimeError,
    )
    provider_client = (
        AnalysisStructuredProviderAdapter(
            AnalysisStructuredProviderConfig(
                provider=config.llm_provider,
                model=config.llm_model_name,
                api_key=config.llm_api_key,
                base_url=config.llm_base_url,
            )
        )
        if config.structured_output_mode in {"auto", "native"}
        else None
    )
    try:
        result = finalize_analysis_payload(
            AnalysisStructuredFinalizerInput(
                expected_target_key=context.request.target_key,
                expected_event_ids=tuple(context.request.event_ids),
                included_lesson_ids=included_lesson_ids,
                execution_direction_mode=execution_direction_mode,
                draft_payload=payload,
                final_summary=run_result.final_summary,
                final_boxed_answer=run_result.final_boxed_answer,
            ),
            config=AnalysisStructuredFinalizerConfig(
                mode=config.structured_output_mode,
                probe_enabled=config.structured_output_probe,
            ),
            provider=config.llm_provider,
            provider_client=provider_client,
        )
    except AnalysisStructuredFinalizerError as exc:
        raise _AnalysisContractRepairRequired(
            failure=_AnalysisContractFailure(
                surface="final_output",
                error_code="analysis_structured_finalizer_error",
                message=str(exc),
                suggested_action=analysis_final_payload_contract_markdown(
                    execution_direction_mode=execution_direction_mode
                ),
                details={
                    "schema_contract_md": analysis_final_payload_contract_markdown(
                        execution_direction_mode=execution_direction_mode
                    )
                },
            ),
            failure_count=attempt_index + 1,
            threshold=_ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
        ) from None
    return result.payload


def _render_selected_learning_cards(context_packet: ContextPacket | None) -> str:
    if context_packet is None or not context_packet.learning_cards:
        return ""
    payload = {
        "usage_receipts": [
            receipt
            for receipt in context_packet.receipts
            if isinstance(receipt, dict)
            and receipt.get("receipt_type") == "learning_card_usage"
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


def _normalize_analysis_result(
    payload: dict[str, Any],
    *,
    context: AnalysisContext,
    attempt_index: int = 0,
    runtime_mode: str,
    included_lesson_ids: tuple[str, ...] = (),
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> AnalysisResult:
    del runtime_mode
    validation = validate_analysis_final_payload(
        payload,
        expected_target_key=context.request.target_key,
        expected_event_ids=tuple(context.request.event_ids),
        included_lesson_ids=included_lesson_ids,
        execution_direction_mode=execution_direction_mode,
    )
    if not validation.ok:
        _raise_analysis_contract_repair_required(
            validation,
            attempt_index=attempt_index,
        )
    try:
        canonical_assessment = canonicalize_analysis_assessment_price_bases(
            validation.analysis_assessment
        )
        assessment = enrich_assessment_with_analysis_price_semantics(
            canonical_assessment,
            market_context=context.market_context,
        )
    except AnalysisPriceSemanticsAssemblyError as exc:
        raise MiroThinkerAnalysisRuntimeError(str(exc)) from exc
    return AnalysisResult(
        target_key=validation.target_key or context.request.target_key,
        event_ids=list(validation.event_ids),
        outcome="no_update",
        analysis_assessment=assessment,
        used_lesson_ids=validation.used_lesson_ids,
    )


def _with_runtime_analysis_assessment_id(
    result: AnalysisResult,
    *,
    context: AnalysisContext,
) -> AnalysisResult:
    assessment = result.analysis_assessment
    if assessment is None:
        return result
    return replace(
        result,
        analysis_assessment=replace(
            assessment,
            assessment_id=_derive_analysis_assessment_storage_id(
                target_key=context.request.target_key,
                analysis_unit_id=(
                    None
                    if context.unit_formation_lane is None
                    else context.unit_formation_lane.analysis_unit_id
                ),
                decision_episode_id=context.request.decision_episode_id,
                event_ids=tuple(context.request.event_ids),
            ),
        ),
    )


def _derive_analysis_assessment_storage_id(
    *,
    target_key: str,
    analysis_unit_id: str | None,
    decision_episode_id: str,
    event_ids: tuple[str, ...],
) -> str:
    if analysis_unit_id:
        identity_payload: dict[str, object] = {
            "target_key": target_key,
            "analysis_unit_id": analysis_unit_id,
        }
    elif decision_episode_id:
        identity_payload = {
            "target_key": target_key,
            "decision_episode_id": decision_episode_id,
        }
    else:
        identity_payload = {
            "target_key": target_key,
            "event_ids": sorted(event_ids),
        }
    identity = json.dumps(
        identity_payload,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = sha256(identity.encode("utf-8")).hexdigest()
    return f"analysis-assessment:{target_key}:{digest[:24]}"

def _raise_analysis_contract_repair_required(
    validation: AnalysisFinalPayloadValidationResult,
    *,
    attempt_index: int = 0,
) -> None:
    details: dict[str, object] = {}
    if validation.field_path:
        details["field_path"] = validation.field_path
    if validation.schema_contract_md:
        details["schema_contract_md"] = validation.schema_contract_md
    if validation.field_path and "price_level_roles" in validation.field_path:
        details.update(price_level_role_repair_details())
    raise _AnalysisContractRepairRequired(
        failure=_AnalysisContractFailure(
            surface="final_output",
            error_code=validation.error_code or "analysis_payload_contract_violation",
            message=validation.message,
            suggested_action=validation.suggested_action or "",
            details=details,
        ),
        failure_count=attempt_index + 1,
        threshold=_ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
    )


def _analysis_stage_root(*, workspace_root: Path, task_id: str) -> Path:
    return (
        workspace_root / "runtime" / _ANALYSIS_STAGING_DIR_NAME / task_id
    ).resolve(strict=False)


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
    outcome_dir = (
        workspace_root
        / "runtime"
        / _ANALYSIS_OUTCOME_DIR_NAME
        / target_key
    ).resolve(strict=False)
    outcome_dir.mkdir(parents=True, exist_ok=True)
    return (outcome_dir / f"{business_at.strftime('%Y-%m')}.jsonl").resolve(strict=False)


def _reset_analysis_artifacts(*, receipt_path: Path, stage_root: Path) -> None:
    try:
        if receipt_path.exists():
            receipt_path.unlink()
        if stage_root.exists():
            shutil.rmtree(stage_root)
    except OSError as exc:
        raise MiroThinkerAnalysisRuntimeError(
            "Failed to reset analysis staging artifacts."
        ) from exc


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
    except OSError as exc:
        raise MiroThinkerAnalysisRuntimeError(
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
        raise MiroThinkerAnalysisRuntimeError(
            "Failed to remove analysis staging workspace."
        ) from exc


def _load_analysis_write_receipts(receipt_path: Path) -> tuple[dict[str, object], ...]:
    if not receipt_path.exists():
        return ()
    if not receipt_path.is_file():
        raise MiroThinkerAnalysisRuntimeError(
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
                raise MiroThinkerAnalysisRuntimeError(
                    "Analysis write receipt file contains invalid JSON."
                ) from exc
            if not isinstance(payload, dict):
                raise MiroThinkerAnalysisRuntimeError(
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
        error = MiroThinkerAnalysisRuntimeError(
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
        raise MiroThinkerAnalysisRuntimeError(
            "analysis context packet hash does not match payload before commit."
        )
    _enforce_analysis_context_packet_visibility(
        layout=layout,
        packet=context_packet,
        required_memory_read_policy=required_memory_read_policy,
        required_runtime_scope=context_packet.runtime_scope,
        required_run_id=(
            context_packet.run_id
            if context_packet.runtime_scope == "replay"
            else None
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
        raise MiroThinkerAnalysisRuntimeError(
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
        raise MiroThinkerAnalysisRuntimeError(
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
        "record_id": (
            f"analysis-commit:{task_id}:{record_type}:{committed_at.isoformat()}"
        ),
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
        raise MiroThinkerAnalysisRuntimeError(
            "Failed to append analysis commit journal record."
        ) from exc


def _commit_analysis_write_receipts(
    *,
    layout: WorkspaceLayout,
    write_receipts: tuple[dict[str, object], ...],
    analysis_assessment: AnalysisAssessment | None,
    context_packet: ContextPacket,
    task_id: str,
    business_at: datetime,
    committed_at: datetime,
    event_ids: tuple[str, ...],
) -> _CommittedResearchMemoryWrites:
    write_receipts = _collapse_committable_write_receipts(write_receipts)
    projected_sections = project_active_price_sections(analysis_assessment)
    projected_section_ids = {section.section_id for section in projected_sections}
    protected_rewrite_sections_by_page = _protected_rewrite_sections_by_page(
        analysis_assessment
    )
    page_writes = ReceiptedResearchMemoryPageWriter(
        layout=layout,
        attribution=ResearchMemoryWriteAttribution(
            actor_type="analysis",
            actor_id=task_id,
            business_at=business_at,
            committed_at=committed_at,
            event_ids=event_ids,
            context_packet_id=context_packet.packet_id,
            context_packet_hash=context_packet.packet_hash,
        ),
    )
    for receipt in write_receipts:
        operation = _require_receipt_text(receipt, "operation")
        content_md = _require_receipt_text(receipt, "content_md")
        if operation == "update_index":
            raise MiroThinkerAnalysisRuntimeError(
                "Analysis attempted update_index, but analysis must not write "
                "index.md."
            )
        if operation == "update_page_section":
            page_path = _require_receipt_text(receipt, "page_path")
            section_name = _require_receipt_text(receipt, "section_name")
            if f"{page_path}:{section_name}" in projected_section_ids:
                continue
            page_writes.update_page_section(
                page_path,
                section_name,
                content_md,
            )
            continue
        if operation == "rewrite_page":
            page_path = _require_receipt_text(receipt, "page_path")
            protected_sections = protected_rewrite_sections_by_page.get(page_path)
            if protected_sections:
                protected_section_names = ", ".join(
                    f"`{section_name}`" for section_name in protected_sections
                )
                raise MiroThinkerAnalysisRuntimeError(
                    "Analysis attempted rewrite_page for "
                    f"{page_path}, but deterministic active sections must stay "
                    f"section-owned: {protected_section_names}."
                )
            page_writes.rewrite_page(
                page_path,
                content_md,
            )
            continue
        if operation == "create_page":
            page_path = _require_receipt_text(receipt, "page_path")
            page_writes.create_page(
                page_path,
                content_md,
            )
            continue
        raise MiroThinkerAnalysisRuntimeError(
            f"Unknown analysis write receipt operation: {operation}"
        )
    for section in projected_sections:
        page_writes.update_page_section(
            section.page_path,
            section.section_name,
            section.content_md,
        )
    return _CommittedResearchMemoryWrites(
        receipt_ids=page_writes.receipt_ids,
        receipts=page_writes.receipts,
    )


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
            None
            if result.analysis_assessment is None
            else result.analysis_assessment.assessment_id
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
        _preflight_analysis_assessment_persistence(
            layout=layout,
            assessment=result.analysis_assessment,
        )
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
        committed_memory_writes = _commit_analysis_write_receipts(
            layout=layout,
            write_receipts=write_receipts,
            analysis_assessment=result.analysis_assessment,
            context_packet=context_packet,
            task_id=task_id,
            business_at=business_at,
            committed_at=committed_at,
            event_ids=tuple(context.request.event_ids),
        )
        progress = replace(
            progress,
            research_memory_writes_committed=True,
            research_memory_write_receipt_ids=committed_memory_writes.receipt_ids,
            thesis_revision_required=_requires_thesis_revision(
                committed_memory_writes.receipts
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
            persisted_artifacts = _persist_analysis_commit_artifacts(
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
        except _AnalysisCommitArtifactPersistenceError as exc:
            progress = _progress_with_persisted_artifacts(
                progress=progress,
                artifacts=exc.partial_artifacts,
            )
            failure_stage = exc.failure_stage
            raise
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
        _append_analysis_log_entry(
            layout=layout,
            context=context,
            result=result,
            write_receipts=write_receipts,
            task_id=task_id,
            committed_at=committed_at,
        )
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
    artifacts: _PersistedAnalysisCommitArtifacts,
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


_MIROTHINKER_USAGE_RE = re.compile(
    r"Total Input:\s*(?P<input>\d+),\s*"
    r"(?:(?:Cache Input|Cache Creation):\s*\d+,\s*)?"
    r"(?:Cache Read:\s*\d+,\s*)?"
    r"Output:\s*(?P<output>\d+)"
)


def _llm_usage_from_mirothinker_task_log(
    *,
    log_file_path: str,
    agent_role: Literal["analysis", "reflection"],
    provider: str,
    model: str,
) -> LLMUsageReceipt:
    path = Path(log_file_path)
    if not path.is_file():
        return LLMUsageReceipt.unavailable(
            agent_role=agent_role,
            provider=provider,
            model=model,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return LLMUsageReceipt.unavailable(
            agent_role=agent_role,
            provider=provider,
            model=model,
        )
    if not isinstance(payload, dict):
        return LLMUsageReceipt.unavailable(
            agent_role=agent_role,
            provider=provider,
            model=model,
        )
    for item in payload.get("step_logs", ()):
        if not isinstance(item, dict):
            continue
        step_name = item.get("step_name")
        message = item.get("message")
        if not isinstance(step_name, str) or "Usage Calculation" not in step_name:
            continue
        if not isinstance(message, str):
            continue
        match = _MIROTHINKER_USAGE_RE.search(message)
        if match is None:
            continue
        input_tokens = int(match.group("input"))
        output_tokens = int(match.group("output"))
        return LLMUsageReceipt(
            agent_role=agent_role,
            usage_source="provider_reported",
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )
    return LLMUsageReceipt.unavailable(
        agent_role=agent_role,
        provider=provider,
        model=model,
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


def _persist_analysis_assessment(
    *,
    layout: WorkspaceLayout,
    assessment: AnalysisAssessment | None,
) -> Path | None:
    if assessment is None:
        return None
    try:
        return AnalysisAssessmentStore(layout).append(assessment)
    except AnalysisAssessmentStoreError as exc:
        raise MiroThinkerAnalysisRuntimeError(
            str(exc)
        ) from exc


def _preflight_analysis_assessment_persistence(
    *,
    layout: WorkspaceLayout,
    assessment: AnalysisAssessment | None,
) -> None:
    if assessment is None:
        return
    try:
        AnalysisAssessmentStore(layout).validate_appendable(assessment)
    except AnalysisAssessmentStoreError as exc:
        raise MiroThinkerAnalysisRuntimeError(
            f"Analysis assessment persistence preflight failed: {exc}"
        ) from exc

def _persist_analysis_commit_artifacts(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    business_at: datetime,
    committed_at: datetime,
    analysis_assessment: AnalysisAssessment | None,
    context_packet_id: str,
    context_packet_hash: str,
    source_event_ids: tuple[str, ...],
    committed_memory_writes: _CommittedResearchMemoryWrites,
) -> _PersistedAnalysisCommitArtifacts:
    thesis_revision_required = _requires_thesis_revision(committed_memory_writes.receipts)
    artifacts = _PersistedAnalysisCommitArtifacts(
        thesis_revision_required=thesis_revision_required,
        analysis_assessment_id=(
            None
            if analysis_assessment is None
            else analysis_assessment.assessment_id
        ),
        analysis_assessment_path=None,
        thesis_revision=None,
        thesis_revision_path=None,
    )
    if not thesis_revision_required:
        try:
            return replace(
                artifacts,
                analysis_assessment_path=_persist_analysis_assessment(
                    layout=layout,
                    assessment=analysis_assessment,
                ),
            )
        except MiroThinkerAnalysisRuntimeError as exc:
            raise _AnalysisCommitArtifactPersistenceError(
                str(exc),
                failure_stage="analysis_assessment_persist",
                partial_artifacts=artifacts,
            ) from exc
    if analysis_assessment is None:
        raise _AnalysisCommitArtifactPersistenceError(
            "Canonical thesis bundle writes require an AnalysisAssessment before commit.",
            failure_stage="analysis_assessment_required",
            partial_artifacts=artifacts,
        )
    builder = ThesisRevisionBuilder(layout=layout)
    try:
        thesis_revision = builder.build(
            target_key=target_key,
            business_at=business_at,
            committed_at=committed_at,
            source="analysis_commit",
            analysis_assessment=analysis_assessment,
            context_packet_id=context_packet_id,
            context_packet_hash=context_packet_hash,
            source_event_ids=source_event_ids,
            research_memory_write_receipt_ids=committed_memory_writes.receipt_ids,
            committed_write_receipts=committed_memory_writes.receipts,
        )
    except ThesisRevisionBuildError as exc:
        raise _AnalysisCommitArtifactPersistenceError(
            "Failed to build thesis revision from canonical thesis bundle.",
            failure_stage="thesis_revision_build",
            partial_artifacts=artifacts,
        ) from exc
    artifacts = replace(artifacts, thesis_revision=thesis_revision)
    try:
        artifacts = replace(
            artifacts,
            analysis_assessment_path=_persist_analysis_assessment(
                layout=layout,
                assessment=analysis_assessment,
            ),
        )
    except MiroThinkerAnalysisRuntimeError as exc:
        raise _AnalysisCommitArtifactPersistenceError(
            str(exc),
            failure_stage="analysis_assessment_persist",
            partial_artifacts=artifacts,
        ) from exc
    try:
        thesis_revision_path = _persist_thesis_revision(
            layout=layout,
            revision=thesis_revision,
        )
    except MiroThinkerAnalysisRuntimeError as exc:
        raise _AnalysisCommitArtifactPersistenceError(
            "Failed to append thesis revision record.",
            failure_stage="thesis_revision_persist",
            partial_artifacts=artifacts,
        ) from exc
    return replace(
        artifacts,
        thesis_revision_path=thesis_revision_path,
    )


def _requires_thesis_revision(
    receipts: tuple[ResearchMemoryWriteReceipt, ...],
) -> bool:
    return any(
        is_canonical_thesis_bundle_write(
            receipt.page_path,
            receipt.section_name,
            receipt.operation,
        )
        for receipt in receipts
    )


def _persist_thesis_revision(
    *,
    layout: WorkspaceLayout,
    revision: ThesisRevision,
) -> Path:
    try:
        return ThesisRevisionStore(layout).append(revision)
    except ThesisRevisionStoreError as exc:
        raise MiroThinkerAnalysisRuntimeError(
            "Failed to append thesis revision record."
        ) from exc


def _validate_persisted_analysis_outcome_dependencies(
    *,
    layout: WorkspaceLayout,
    result: AnalysisResult,
    analysis_assessment_path: Path | None,
    thesis_revision: ThesisRevision | None,
    thesis_revision_path: Path | None,
) -> None:
    if result.analysis_assessment is not None:
        if analysis_assessment_path is None:
            raise MiroThinkerAnalysisRuntimeError(
                "Analysis outcome requires a persisted AnalysisAssessment path."
            )
        if not analysis_assessment_path.exists():
            raise MiroThinkerAnalysisRuntimeError(
                "Analysis outcome cannot reference a missing AnalysisAssessment path."
            )
    elif analysis_assessment_path is not None:
        raise MiroThinkerAnalysisRuntimeError(
            "Analysis outcome must not reference an AnalysisAssessment path when the "
            "result has no AnalysisAssessment."
        )
    if thesis_revision is None:
        if thesis_revision_path is not None:
            raise MiroThinkerAnalysisRuntimeError(
                "Analysis outcome must not reference a thesis revision path without a "
                "ThesisRevision."
            )
        return
    if thesis_revision_path is None:
        raise MiroThinkerAnalysisRuntimeError(
            "Analysis outcome requires a persisted ThesisRevision path."
        )
    if not thesis_revision_path.exists():
        raise MiroThinkerAnalysisRuntimeError(
            "Analysis outcome cannot reference a missing ThesisRevision path."
        )
    persisted_revisions = ThesisRevisionStore(layout).read_records(
        target_key=thesis_revision.target_key,
        year_month=thesis_revision.business_at.strftime("%Y-%m"),
    )
    normalized_revision_path = thesis_revision_path.resolve(strict=False)
    if not any(
        persisted.record.revision_id == thesis_revision.revision_id
        and persisted.path.resolve(strict=False) == normalized_revision_path
        for persisted in persisted_revisions
    ):
        raise MiroThinkerAnalysisRuntimeError(
            "Analysis outcome cannot reference a ThesisRevision that is not persisted."
        )


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
    if not (
        routing.candidate_review_allowed or routing.current_exposure_required
    ):
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
        raise MiroThinkerAnalysisRuntimeError(
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
        raise MiroThinkerAnalysisRuntimeError(
            "Failed to materialize candidate review anchor from analysis assessment."
        ) from exc


def _analysis_pm_review_reasons(
    assessment: AnalysisAssessment,
) -> tuple[PMReviewReason, ...]:
    return tuple(
        cast(PMReviewReason, reason)
        for reason in assessment.pm_review_reasons
        if reason != "none"
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
        reason
        for reason in explicit_reasons
        if reason in _ANALYSIS_PM_ANNOTATION_ONLY_REASONS
    ]
    preserved_current_exposure_reasons = [
        reason
        for reason in explicit_reasons
        if reason != "flat_candidate_setup"
        and reason in _ANALYSIS_PM_ESCALATION_REASONS
    ]
    inferred_reasons = _infer_current_exposure_review_reasons(
        assessment=assessment,
        active_state=active_state,
    )
    reasons.extend(preserved_current_exposure_reasons)
    if not any(
        reason in {
            "current_exposure_pressure",
            "invalidation_touched",
            "risk_reward_compression",
            "material_setup",
        }
        for reason in reasons
    ):
        reasons.extend(inferred_reasons)
    reasons.extend(preserved_annotations)
    return _stable_unique_tuple(tuple(reasons))


def _resolve_candidate_review_reasons(
    explicit_reasons: tuple[PMReviewReason, ...],
) -> tuple[PMReviewReason, ...]:
    reasons: list[PMReviewReason] = list(explicit_reasons)
    if not any(reason in _ANALYSIS_PM_ESCALATION_REASONS for reason in reasons):
        reasons.insert(0, "flat_candidate_setup")
    return _stable_unique_tuple(tuple(reasons))


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
    if (
        assessment.pm_current_exposure_review_required
        and active_state not in {None, "flat"}
    ):
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
        raise MiroThinkerAnalysisRuntimeError(
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
        raise MiroThinkerAnalysisRuntimeError(
            "Failed to append PM review request record."
        ) from exc


def _derive_pm_review_request_id(assessment: AnalysisAssessment) -> str:
    digest = sha256(
        f"analysis-assessment|{assessment.assessment_id}".encode("utf-8")
    ).hexdigest()
    return f"pm-review-request:{digest[:24]}"


def _derive_candidate_review_anchor_id(assessment: AnalysisAssessment) -> str:
    digest = sha256(
        f"analysis-candidate-anchor|{assessment.assessment_id}".encode("utf-8")
    ).hexdigest()
    return f"candidate-review-anchor:{digest[:24]}"


def _max_visible_event_time_for_assessment(
    *,
    assessment: AnalysisAssessment,
    context: AnalysisContext,
) -> datetime:
    source_ids = set(assessment.source_event_ids)
    visible_times = tuple(
        record.ts_event
        for record in context.evidence_records
        if record.event_id in source_ids
    )
    if not visible_times:
        raise MiroThinkerAnalysisRuntimeError(
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
    _validate_persisted_analysis_outcome_dependencies(
        layout=layout,
        result=result,
        analysis_assessment_path=analysis_assessment_path,
        thesis_revision=thesis_revision,
        thesis_revision_path=thesis_revision_path,
    )
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
    grounding_coverage_receipt, _grounding_error = _analysis_read_audit_error(
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
        "evidence_review_dimensions_by_event_id": (
            evidence_review_dimensions_by_event_id
        ),
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
        "compiled_memory_grounding_count": len(
            grounding_coverage_receipt.compiled_memory_cards
        ),
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
            _ANALYSIS_MEMORY_READ_OPERATIONS,
        ),
        "market_tool_call_count": _operation_call_count(
            write_receipts,
            _ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS,
        ),
        "memory_lane_omission_count": (
            0
            if context_packet.analysis_workbench is None
            else len(context_packet.analysis_workbench.memory_impact_lane.omission_receipts)
        ),
        "outcome": result.outcome,
        "analysis_assessment_id": (
            None
            if result.analysis_assessment is None
            else result.analysis_assessment.assessment_id
        ),
        "analysis_assessment_path": (
            None if analysis_assessment_path is None else str(analysis_assessment_path)
        ),
        "analysis_assessment": (
            None
            if result.analysis_assessment is None
            else result.analysis_assessment.to_json_payload()
        ),
        "thesis_revision_id": (
            None if thesis_revision is None else thesis_revision.revision_id
        ),
        "thesis_revision_path": (
            None if thesis_revision_path is None else str(thesis_revision_path)
        ),
        "pm_review_request_id": (
            None if pm_review_request is None else pm_review_request.request_id
        ),
        "candidate_review_anchor_id": (
            None
            if candidate_review_anchor is None
            else candidate_review_anchor.anchor_id
        ),
        "candidate_review_anchor_path": (
            None
            if candidate_review_anchor_path is None
            else str(candidate_review_anchor_path)
        ),
        "candidate_review_anchor": (
            None
            if candidate_review_anchor is None
            else candidate_review_anchor.to_json_payload()
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
            _material_memory_write_count(write_receipts)
            - len(research_memory_write_receipt_ids),
        ),
        "research_memory_write_receipt_ids": list(research_memory_write_receipt_ids),
        "market_context_present": context.market_context is not None,
        "market_context_audit": (
            context.market_context.compact_audit()
            if context.market_context is not None
            else None
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
        raise MiroThinkerAnalysisRuntimeError(
            "Failed to append analysis outcome receipt."
        ) from exc
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
    content_md = _require_receipt_text(receipt, "content_md")
    payload = {
        "operation": _require_receipt_text(receipt, "operation"),
        "content_sha256": sha256(content_md.encode("utf-8")).hexdigest(),
        "content_preview": _content_preview(content_md),
    }
    page_path = receipt.get("page_path")
    if isinstance(page_path, str) and page_path.strip():
        payload["page_path"] = page_path.strip()
    section_name = receipt.get("section_name")
    if isinstance(section_name, str) and section_name.strip():
        payload["section_name"] = section_name.strip()
    scope_key = receipt.get("scope_key")
    if isinstance(scope_key, str) and scope_key.strip():
        payload["scope_key"] = scope_key.strip()
    return payload


def _serialize_market_tool_usage(
    receipts: tuple[dict[str, object], ...],
) -> dict[str, object]:
    duplicate_call_count = sum(
        1
        for receipt in receipts
        if receipt.get("operation") in _ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS
        and receipt.get("result_mode") == "duplicate"
    )
    calls = tuple(
        _serialize_market_tool_call(receipt)
        for receipt in receipts
        if receipt.get("operation") in _ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS
        and receipt.get("result_mode") != "duplicate"
    )
    statuses = tuple(
        status
        for call in calls
        if isinstance((status := call.get("status")), str) and status.strip()
    )
    tools_called = tuple(
        dict.fromkeys(
            operation
            for call in calls
            if isinstance((operation := call.get("operation")), str)
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
    return _operation_call_count(receipts, _ANALYSIS_READ_RECEIPT_OPERATIONS)


def _operation_call_count(
    receipts: tuple[dict[str, object], ...],
    operations: frozenset[str] | set[str],
) -> int:
    return sum(
        1
        for receipt in receipts
        if receipt.get("operation") in operations
        and receipt.get("result_mode") != "duplicate"
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


def _append_analysis_log_entry(
    *,
    layout: WorkspaceLayout,
    context: AnalysisContext,
    result: AnalysisResult,
    write_receipts: tuple[dict[str, object], ...],
    task_id: str,
    committed_at: datetime,
) -> None:
    if result.outcome == "no_update":
        return

    business_at = context.decision_visibility.business_at
    log_path = layout.targets_root / result.target_key / "log.md"
    log_record_id = _analysis_log_record_id(task_id)
    try:
        existing_log = log_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MiroThinkerAnalysisRuntimeError(
            "Failed to read target analysis log page."
        ) from exc
    if log_record_id in existing_log:
        return

    entry_md = _render_analysis_log_entry(
        context=context,
        result=result,
        write_receipts=write_receipts,
        task_id=task_id,
        business_at=business_at,
        committed_at=committed_at,
        log_record_id=log_record_id,
    )
    try:
        FileBackedIndexLogWriter(layout).append_log_entry(
            f"target:{result.target_key}",
            entry_md,
        )
    except Exception as exc:
        raise MiroThinkerAnalysisRuntimeError(
            "Failed to append deterministic analysis log entry."
        ) from exc


def _render_analysis_log_entry(
    *,
    context: AnalysisContext,
    result: AnalysisResult,
    write_receipts: tuple[dict[str, object], ...],
    task_id: str,
    business_at: datetime,
    committed_at: datetime,
    log_record_id: str,
) -> str:
    lines = [
        f"## {business_at.isoformat()} analysis {result.outcome}",
        "",
        f"- Record: `{log_record_id}`",
        f"- Task: `{task_id}`",
        f"- Committed At: `{committed_at.isoformat()}`",
        f"- Event IDs: {', '.join(f'`{event_id}`' for event_id in result.event_ids)}",
        f"- Why Escalated: {context.request.why_escalated}",
    ]
    material_writes = [
        _serialize_analysis_outcome_write(receipt)
        for receipt in write_receipts
        if _is_material_memory_write_receipt(receipt)
    ]
    if material_writes:
        lines.append("- Material Writes:")
        for write in material_writes:
            location = write.get("page_path") or write.get("scope_key") or "unknown"
            section_name = write.get("section_name")
            section_suffix = f" section `{section_name}`" if section_name else ""
            lines.append(
                f"  - `{write['operation']}` `{location}`{section_suffix} "
                f"sha256 `{write['content_sha256']}`"
            )
    return "\n".join(lines) + "\n"


def _is_material_memory_write_receipt(receipt: dict[str, object]) -> bool:
    return _require_receipt_text(receipt, "operation") in {
        "update_page_section",
        "rewrite_page",
        "create_page",
    }


def _material_memory_write_count(receipts: tuple[dict[str, object], ...]) -> int:
    return sum(1 for receipt in receipts if _is_material_memory_write_receipt(receipt))


def _content_preview(content_md: str, *, max_length: int = 240) -> str:
    collapsed = " ".join(content_md.split())
    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[: max_length - 3].rstrip() + "..."


def _analysis_outcome_record_id(task_id: str) -> str:
    return f"analysis-outcome:{task_id}"


def _analysis_log_record_id(task_id: str) -> str:
    return f"analysis-log:{task_id}"


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
        raise MiroThinkerAnalysisRuntimeError(
            "Failed to inspect analysis outcome receipt for idempotency."
        ) from exc
    return False


def _analysis_support_diagnostics(
    write_receipts: tuple[dict[str, object], ...],
) -> dict[str, object]:
    material_writes = tuple(
        receipt
        for receipt in write_receipts
        if _is_material_memory_write_receipt(receipt)
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
        "absence_support_used_count": sum(
            1 for item in absence_support_payloads if item["used"]
        ),
        "maturity_support_used_count": sum(
            1 for item in maturity_support_payloads if item["used"]
        ),
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


def _normalize_support_string_tuple(
    value: object,
    *,
    field_name: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if value is None:
        if allow_empty:
            return ()
        raise MiroThinkerAnalysisRuntimeError(
            f"{field_name} must be a list or tuple."
        )
    if not isinstance(value, (list, tuple)):
        raise MiroThinkerAnalysisRuntimeError(
            f"{field_name} must be a list or tuple."
        )
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise MiroThinkerAnalysisRuntimeError(
                f"{field_name}[{index}] must be a string."
            )
        normalized_item = item.strip()
        if not normalized_item:
            raise MiroThinkerAnalysisRuntimeError(
                f"{field_name}[{index}] must not be blank."
            )
        normalized.append(normalized_item)
    return tuple(normalized)


def _parse_analysis_support_payload(
    payload: object,
    *,
    payload_name: str,
    support_reason_codes: frozenset[str],
) -> tuple[bool, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if not isinstance(payload, dict):
        raise MiroThinkerAnalysisRuntimeError(f"{payload_name} must be an object.")

    used = payload.get("used")
    if not isinstance(used, bool):
        raise MiroThinkerAnalysisRuntimeError(
            f"{payload_name}.used must be a boolean."
        )

    reason_codes = _normalize_support_string_tuple(
        payload.get("reason_codes"),
        field_name=f"{payload_name}.reason_codes",
    )
    if not reason_codes:
        raise MiroThinkerAnalysisRuntimeError(
            f"{payload_name}.reason_codes must not be empty."
        )
    unsupported_reason_codes = [
        reason_code for reason_code in reason_codes if reason_code not in support_reason_codes
    ]
    if unsupported_reason_codes:
        raise MiroThinkerAnalysisRuntimeError(
            f"{payload_name}.reason_codes contains unsupported values: "
            f"{', '.join(unsupported_reason_codes)}."
        )

    domains = _normalize_support_string_tuple(
        payload.get("domains"),
        field_name=f"{payload_name}.domains",
    )
    if not domains:
        raise MiroThinkerAnalysisRuntimeError(
            f"{payload_name}.domains must not be empty."
        )
    unsupported_domains = [
        domain for domain in domains if domain not in _COMPLETENESS_DOMAINS
    ]
    if unsupported_domains:
        raise MiroThinkerAnalysisRuntimeError(
            f"{payload_name}.domains contains unsupported values: "
            f"{', '.join(unsupported_domains)}."
        )

    support_refs = _normalize_support_string_tuple(
        payload.get("support_refs"),
        field_name=f"{payload_name}.support_refs",
        allow_empty=True,
    )

    return used, reason_codes, domains, support_refs


def _support_refs_grounded(
    *,
    support_refs: tuple[str, ...],
    grounded_support_refs: set[str],
) -> bool:
    if not support_refs:
        return False
    return any(ref in grounded_support_refs for ref in support_refs)


def _requirement_is_advanced(
    *,
    unit_formation_lane: UnitFormationLane,
    purpose: str,
    reason_code: str,
    domain: str,
) -> bool:
    if reason_code not in unit_formation_lane.advanced_completeness_requirements:
        return False
    return any(
        req.purpose == purpose
        and req.domain == domain
        and reason_code in req.required_for
        for req in unit_formation_lane.completeness_requirements
    )


def _collect_analysis_support_refs(
    *,
    write_receipts: tuple[dict[str, object], ...],
    context: AnalysisContext,
    context_packet: ContextPacket | None,
) -> set[str]:
    grounded_refs: set[str] = set()
    for receipt in write_receipts:
        operation = receipt.get("operation")
        if not isinstance(operation, str):
            continue
        if operation == "read_evidence":
            grounded_refs.update(_read_evidence_receipt_event_ids(receipt))
            continue
        if operation == "search_wiki":
            matches = receipt.get("matches")
            if isinstance(matches, list):
                for match in matches:
                    if not isinstance(match, dict):
                        continue
                    for key in ("page_path", "scope"):
                        value = match.get(key)
                        if isinstance(value, str):
                            normalized = value.strip()
                            if normalized:
                                grounded_refs.add(normalized)
            scope = receipt.get("scope")
            if isinstance(scope, str):
                normalized_scope = scope.strip()
                if normalized_scope:
                    grounded_refs.add(normalized_scope)
            continue
        if operation in _ANALYSIS_MEMORY_READ_OPERATIONS:
            memory_ref = _target_memory_read_ref(
                receipt,
                target_key=context.request.target_key,
            )
            if memory_ref is not None:
                grounded_refs.add(memory_ref)
            page_path = receipt.get("page_path")
            if isinstance(page_path, str):
                grounded_refs.add(page_path.replace("\\", "/").strip())
            section_name = receipt.get("section_name")
            if isinstance(section_name, str):
                grounded_refs.add(section_name.strip())
            event_id = receipt.get("event_id")
            if isinstance(event_id, str):
                grounded_refs.add(event_id.strip())
    if context_packet is None or context_packet.analysis_workbench is None:
        return grounded_refs

    workbench = context_packet.analysis_workbench
    grounded_refs.update(card.event_id for card in workbench.evidence_lane.active_records)
    for card in workbench.memory_impact_lane.cards:
        grounded_refs.add(card.card_id)
        grounded_refs.add(card.page_path)
        grounded_refs.add(f"{card.page_path}:{card.section_name}")
        if card.compiler_read_receipt_id:
            grounded_refs.add(card.compiler_read_receipt_id)
    for card in workbench.operator_context_lane.cards:
        grounded_refs.add(card.card_id)
        grounded_refs.add(f"{card.card_id}:{card.section_name}")
    for receipt in context_packet.receipts:
        if isinstance(receipt, dict):
            receipt_id = receipt.get("receipt_id")
        else:
            receipt_id = getattr(receipt, "receipt_id", None)
        if isinstance(receipt_id, str):
            normalized = receipt_id.strip()
            if normalized:
                grounded_refs.add(normalized)
    return grounded_refs


def _validate_analysis_write_support_payloads(
    *,
    support_receipt: dict[str, object],
    unit_formation_lane: UnitFormationLane | None,
    grounded_support_refs: set[str],
) -> None:
    supported_by_lane: set[tuple[str, str]] = set()
    supported_by_ref: set[tuple[str, str]] = set()
    claims: list[tuple[str, str, str]] = []
    for payload_name, support_reason_codes in (
        ("absence_based_support", _ABSENCE_SUPPORT_REASON_CODES),
        ("maturity_based_support", _MATURITY_SUPPORT_REASON_CODES),
    ):
        payload = support_receipt.get(payload_name)
        if payload is None:
            continue
        used, reasons, domains, support_refs = _parse_analysis_support_payload(
            payload,
            payload_name=payload_name,
            support_reason_codes=support_reason_codes,
        )
        if not used:
            continue
        ref_supported = _support_refs_grounded(
            support_refs=support_refs,
            grounded_support_refs=grounded_support_refs,
        )
        for reason_code in reasons:
            purpose = _SUPPORT_REASON_PURPOSE.get(reason_code)
            if purpose is None:
                raise MiroThinkerAnalysisRuntimeError(
                    f"{payload_name}.reason_codes contains unsupported values: {reason_code}"
                )
            for domain in domains:
                claim = (reason_code, domain)
                if (
                    unit_formation_lane is not None
                    and _requirement_is_advanced(
                        unit_formation_lane=unit_formation_lane,
                        purpose=purpose,
                        reason_code=reason_code,
                        domain=domain,
                    )
                ):
                    supported_by_lane.add(claim)
                if ref_supported:
                    supported_by_ref.add(claim)
                claims.append((payload_name, reason_code, domain))
    missing_claims = [
        f"{claim_payload_name}:{reason_code}:{domain}"
        for claim_payload_name, reason_code, domain in claims
        if (reason_code, domain) not in supported_by_lane
        and (reason_code, domain) not in supported_by_ref
    ]
    if missing_claims:
        raise MiroThinkerAnalysisRuntimeError(
            "analysis write support for absence/maturity claims not grounded or not "
            "advanced in lane: " + ", ".join(missing_claims)
        )


def _analysis_unit_formation_lane(
    *,
    context: AnalysisContext,
    context_packet: ContextPacket | None,
) -> UnitFormationLane | None:
    if context.unit_formation_lane is not None:
        return context.unit_formation_lane
    if context_packet is not None and context_packet.analysis_workbench is not None:
        return context_packet.analysis_workbench.unit_formation_lane
    return None


def _validate_analysis_write_contract(
    *,
    result: AnalysisResult,
    context: AnalysisContext,
    write_receipts: tuple[dict[str, object], ...],
    context_packet: ContextPacket | None = None,
    attempt_index: int = 0,
) -> tuple[AnalysisResult, tuple[dict[str, object], ...]]:
    active_citations = {
        record.event_id: record.source_ref for record in context.evidence_records
    }
    context_citations = _collect_context_citations(
        context=context,
        receipts=write_receipts,
        active_citations=active_citations,
    )
    material_write_ops = {
        "update_page_section",
        "rewrite_page",
        "create_page",
    }
    unit_formation_lane = _analysis_unit_formation_lane(
        context=context,
        context_packet=context_packet,
    )
    grounded_support_refs = _collect_analysis_support_refs(
        write_receipts=write_receipts,
        context=context,
        context_packet=context_packet,
    )
    allowed_operations = material_write_ops
    committable_write_receipts: list[dict[str, object]] = []
    saw_material_write = False
    saw_watchlist_write = False
    required_watchlist_path = f"targets/{context.request.target_key}/watchlist.md"
    for receipt in write_receipts:
        operation = _require_receipt_text(receipt, "operation")
        if operation in _ANALYSIS_READ_RECEIPT_OPERATIONS:
            continue
        _require_receipt_text(receipt, "written_path")
        content_md = _require_receipt_text(receipt, "content_md")
        if operation == "append_log_entry":
            raise MiroThinkerAnalysisRuntimeError(
                "Analysis attempted append_log_entry, but analysis must not write "
                "research-memory log.md."
            )
        if operation == "update_index":
            raise MiroThinkerAnalysisRuntimeError(
                "Analysis attempted update_index, but analysis must not write "
                "index.md."
            )
        if operation not in allowed_operations:
            raise MiroThinkerAnalysisRuntimeError(
                f"Unknown analysis write receipt operation: {operation}"
            )
        if operation in material_write_ops:
            _validate_analysis_write_support_payloads(
                support_receipt=receipt,
                unit_formation_lane=unit_formation_lane,
                grounded_support_refs=grounded_support_refs,
            )
            content_md = _canonicalize_citations(
                content_md=content_md,
                active_citations=active_citations,
                context_citations=context_citations,
            )
            saw_material_write = True
            committable_write_receipts.append(
                {
                    **receipt,
                    "content_md": content_md,
                }
            )
            if _receipt_page_path(receipt) == required_watchlist_path:
                saw_watchlist_write = True
            _validate_citations(
                content_md=content_md,
                active_citations=active_citations,
                context_citations=context_citations,
            )
    if not saw_material_write:
        if committable_write_receipts:
            raise MiroThinkerAnalysisRuntimeError(
                "analysis_write_receipts_contained_no_material_memory_update"
            )
        return (
            AnalysisResult(
                target_key=result.target_key,
                event_ids=result.event_ids,
                outcome="no_update",
                analysis_assessment=result.analysis_assessment,
                used_lesson_ids=result.used_lesson_ids,
            ),
            (),
        )
    if context.request.requires_watchlist_maintenance and not saw_watchlist_write:
        raise MiroThinkerAnalysisRuntimeError(
            f"missing_watchlist_maintenance: {required_watchlist_path}"
        )
    protected_rewrite_repair = _protected_rewrite_contract_repair_required(
        write_receipts=tuple(committable_write_receipts),
        analysis_assessment=result.analysis_assessment,
        attempt_index=attempt_index,
    )
    if protected_rewrite_repair is not None:
        raise protected_rewrite_repair
    return (
        AnalysisResult(
            target_key=result.target_key,
            event_ids=result.event_ids,
            outcome="memory_updated",
            analysis_assessment=result.analysis_assessment,
            used_lesson_ids=result.used_lesson_ids,
        ),
        _collapse_committable_write_receipts(tuple(committable_write_receipts)),
    )


def _protected_rewrite_contract_repair_required(
    *,
    write_receipts: tuple[dict[str, object], ...],
    analysis_assessment: AnalysisAssessment | None,
    attempt_index: int,
) -> _AnalysisContractRepairRequired | None:
    protected_sections_by_page = _protected_rewrite_sections_by_page(
        analysis_assessment
    )
    if not protected_sections_by_page:
        return None
    for receipt in write_receipts:
        operation = _require_receipt_text(receipt, "operation")
        if operation != "rewrite_page":
            continue
        page_path = _receipt_page_path(receipt)
        protected_sections = protected_sections_by_page.get(page_path)
        if not protected_sections:
            continue
        protected_section_names = ", ".join(
            f"`{section_name}`" for section_name in protected_sections
        )
        message = (
            "Analysis attempted rewrite_page for "
            f"{page_path}, but deterministic active sections must stay "
            f"section-owned: {protected_section_names}."
        )
        return _AnalysisContractRepairRequired(
            failure=_AnalysisContractFailure(
                surface="write_tool",
                error_code="protected_section_rewrite_forbidden",
                message=message,
                recoverable=True,
                suggested_action=(
                    "Do not use rewrite_page for this page. Use update_page_section "
                    "for the relevant canonical top-level section, then validate the "
                    "final payload again."
                ),
                details={
                    "tool_name": "rewrite_page",
                    "error_code": "protected_section_rewrite_forbidden",
                    "arguments": {
                        "page_path": page_path,
                        "protected_sections": list(protected_sections),
                    },
                    "page_path": page_path,
                    "protected_sections": list(protected_sections),
                    "allowed_top_sections": list(protected_sections),
                    "suggested_action": (
                        "Use update_page_section for canonical top-level sections; "
                        "do not rewrite the full fixed target page."
                    ),
                },
            ),
            failure_count=attempt_index + 1,
            threshold=_ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
        )
    return None


def _protected_rewrite_sections_by_page(
    analysis_assessment: AnalysisAssessment | None,
) -> dict[str, tuple[str, ...]]:
    protected_rewrite_sections_by_page_lists: dict[str, list[str]] = {}
    for section in project_active_price_sections(analysis_assessment):
        protected_rewrite_sections_by_page_lists.setdefault(section.page_path, [])
        protected_rewrite_sections_by_page_lists[section.page_path].append(
            section.section_name
        )
    return {
        page_path: tuple(section_names)
        for page_path, section_names in protected_rewrite_sections_by_page_lists.items()
    }


def _collapse_committable_write_receipts(
    write_receipts: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    collapsed: list[dict[str, object]] = []
    for receipt in write_receipts:
        operation = _require_receipt_text(receipt, "operation")
        if operation not in _ANALYSIS_WRITE_TOOL_NAMES:
            collapsed.append(receipt)
            continue
        page_path = _receipt_page_path(receipt)
        if operation == "update_page_section":
            section_name = _require_receipt_text(receipt, "section_name")
            collapsed = [
                existing
                for existing in collapsed
                if not (
                    _optional_receipt_text(existing, "operation")
                    == "update_page_section"
                    and _receipt_page_path(existing) == page_path
                    and _optional_receipt_text(existing, "section_name")
                    == section_name
                )
            ]
            collapsed.append(receipt)
            continue
        if operation in {"rewrite_page", "create_page"}:
            collapsed = [
                existing
                for existing in collapsed
                if _receipt_page_path(existing) != page_path
            ]
            collapsed.append(receipt)
            continue
    return tuple(collapsed)


def _receipt_page_path(receipt: dict[str, object]) -> str:
    page_path = receipt.get("page_path")
    if isinstance(page_path, str) and page_path.strip():
        return page_path.strip().replace("\\", "/")
    return _require_receipt_text(receipt, "written_path").replace("\\", "/")


def _validate_citations(
    *,
    content_md: str,
    active_citations: dict[str, str],
    context_citations: dict[str, str],
) -> None:
    noncanonical_event_ids = set(extract_noncanonical_analysis_event_ids(content_md))
    if noncanonical_event_ids:
        raise MiroThinkerAnalysisRuntimeError(
            "Analysis write content contained noncanonical event_id references: "
            f"{', '.join(sorted(noncanonical_event_ids))}. "
            "Use canonical markdown citations only: `event_id` | `source_ref`."
        )
    citations = _extract_citation_pairs(content_md)
    if not citations:
        raise MiroThinkerAnalysisRuntimeError(
            "Analysis write content must include readable citations in the form "
            "`event_id` | `source_ref`."
        )
    unknown_event_ids = {
        event_id
        for event_id, _source_ref in citations
        if event_id not in active_citations and event_id not in context_citations
    }
    if unknown_event_ids:
        raise MiroThinkerAnalysisRuntimeError(
            "Analysis write content cited event_ids that were neither active nor "
            f"loaded as analysis context: {', '.join(sorted(unknown_event_ids))}"
        )
    if not any(event_id in active_citations for event_id, _source_ref in citations):
        raise MiroThinkerAnalysisRuntimeError(
            "Analysis write content must cite at least one active request event_id / "
            "source_ref pairs."
        )


def _collect_context_citations(
    *,
    context: AnalysisContext,
    receipts: tuple[dict[str, object], ...],
    active_citations: dict[str, str],
) -> dict[str, str]:
    citations: dict[str, str] = {}
    for page in (
        *context.memory_context.target_pages,
        *context.memory_context.shared_pages,
    ):
        visible_content = bounded_text_payload(
            page.content_md,
            limit=DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
        )["excerpt"]
        if not isinstance(visible_content, str):
            raise MiroThinkerAnalysisRuntimeError(
                "bounded analysis page excerpt must be a string."
            )
        _add_context_citations(
            citations,
            _extract_citation_pairs(visible_content),
            active_citations=active_citations,
        )
    for receipt in receipts:
        if receipt.get("result_mode") == "duplicate":
            continue
        operation = _require_receipt_text(receipt, "operation")
        if operation in {"read_page", "read_section", "read_around_citation"}:
            _add_context_citations(
                citations,
                _extract_citation_pairs(_require_receipt_string(receipt, "content_md")),
                active_citations=active_citations,
            )
            continue
        if operation == "search_wiki":
            _add_context_citations(
                citations,
                _extract_search_wiki_citations(receipt),
                active_citations=active_citations,
            )
            continue
        if operation == "read_evidence":
            _add_context_citations(
                citations,
                _extract_read_evidence_citations(receipt),
                active_citations=active_citations,
            )
            continue
    return citations


def _extract_search_wiki_citations(
    receipt: dict[str, object],
) -> set[tuple[str, str]]:
    matches = receipt.get("matches")
    if not isinstance(matches, list):
        raise MiroThinkerAnalysisRuntimeError(
            "Analysis search_wiki receipt field 'matches' must be a list."
        )
    citations: set[tuple[str, str]] = set()
    for index, match in enumerate(matches):
        if not isinstance(match, dict):
            raise MiroThinkerAnalysisRuntimeError(
                f"Analysis search_wiki receipt match {index} must be an object."
            )
        snippet = match.get("snippet")
        if not isinstance(snippet, str):
            raise MiroThinkerAnalysisRuntimeError(
                f"Analysis search_wiki receipt match {index}.snippet must be a string."
            )
        citations.update(_extract_citation_pairs(snippet))
    return citations


def _extract_read_evidence_citations(
    receipt: dict[str, object],
) -> set[tuple[str, str]]:
    records = receipt.get("records")
    if not isinstance(records, list):
        raise MiroThinkerAnalysisRuntimeError(
            "Analysis read_evidence receipt field 'records' must be a list."
        )
    citations: set[tuple[str, str]] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise MiroThinkerAnalysisRuntimeError(
                f"Analysis read_evidence receipt record {index} must be an object."
            )
        event_id = record.get("event_id")
        source_ref = record.get("source_ref")
        if not isinstance(event_id, str) or not event_id.strip():
            raise MiroThinkerAnalysisRuntimeError(
                f"Analysis read_evidence receipt record {index}.event_id must be a "
                "non-blank string."
            )
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise MiroThinkerAnalysisRuntimeError(
                f"Analysis read_evidence receipt record {index}.source_ref must be a "
                "non-blank string."
            )
        citations.add((event_id.strip(), source_ref.strip()))
    return citations


def _extract_citation_pairs(content_md: str) -> set[tuple[str, str]]:
    return {
        (citation.event_id, citation.source_ref)
        for citation in extract_analysis_citations(content_md)
    }


def _add_context_citations(
    citations: dict[str, str],
    pairs: set[tuple[str, str]],
    *,
    active_citations: dict[str, str],
) -> None:
    for event_id, source_ref in pairs:
        if event_id in active_citations:
            continue
        existing_source_ref = citations.get(event_id)
        if existing_source_ref is None:
            citations[event_id] = source_ref
            continue
        if existing_source_ref != source_ref:
            raise MiroThinkerAnalysisRuntimeError(
                "Analysis context contains ambiguous source_ref values for "
                f"event_id {event_id}: {existing_source_ref!r} and {source_ref!r}."
            )


def _canonicalize_citations(
    *,
    content_md: str,
    active_citations: dict[str, str],
    context_citations: dict[str, str],
) -> str:
    return canonicalize_analysis_citations(
        content_md,
        {
            **context_citations,
            **active_citations,
        },
    )


def _require_receipt_text(receipt: dict[str, object], field_name: str) -> str:
    value = receipt.get(field_name)
    if not isinstance(value, str):
        raise MiroThinkerAnalysisRuntimeError(
            f"Analysis write receipt field {field_name!r} must be a string."
        )
    normalized = value.strip()
    if not normalized:
        raise MiroThinkerAnalysisRuntimeError(
            f"Analysis write receipt field {field_name!r} must not be blank."
        )
    return normalized


def _require_receipt_string(receipt: dict[str, object], field_name: str) -> str:
    value = receipt.get(field_name)
    if not isinstance(value, str):
        raise MiroThinkerAnalysisRuntimeError(
            f"Analysis receipt field {field_name!r} must be a string."
        )
    return value


def _validate_existing_dir(path: Path, *, field_name: str) -> Path:
    validated = _validate_path(path, field_name=field_name)
    if not validated.exists():
        raise MiroThinkerAnalysisRuntimeError(
            f"{field_name} does not exist: {validated}"
        )
    if not validated.is_dir():
        raise MiroThinkerAnalysisRuntimeError(
            f"{field_name} must be a directory: {validated}"
        )
    return validated


def _validate_path(path: Path, *, field_name: str) -> Path:
    if not isinstance(path, Path):
        raise MiroThinkerAnalysisRuntimeError(f"{field_name} must be a pathlib.Path.")
    return path.resolve(strict=False)


def _validate_non_blank_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MiroThinkerAnalysisRuntimeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MiroThinkerAnalysisRuntimeError(f"{field_name} must not be blank.")
    return normalized


def _require_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MiroThinkerAnalysisRuntimeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MiroThinkerAnalysisRuntimeError(f"{field_name} must not be blank.")
    return normalized


__all__ = [
    "MiroThinkerAnalysisRuntimeConfig",
    "MiroThinkerAnalysisRuntimeError",
    "build_mirothinker_analysis_runner",
]
