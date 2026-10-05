"""Thin MiroThinker-backed runtime bindings for reflection evaluation."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, TypedDict, cast

from event_trader.contracts import EvidenceLedgerRecord, PageReadResult, ViewStateChange
from event_trader.counterfactuals import (
    CounterfactualBaselineResult,
    CounterfactualEvaluationReport,
)
from event_trader.integrations.bounded_context import bounded_text_payload
from event_trader.integrations.boxed_json import (
    BoxedJsonPayloadError,
    load_first_boxed_json_object,
)
from event_trader.integrations.markdown_context import (
    MarkdownContextError,
    build_markdown_page_map,
)
from event_trader.integrations.mirothinker_llm_config import (
    build_mirothinker_llm_config,
)
from event_trader.integrations.mirothinker_runtime_paths import (
    MiroThinkerRuntimePathError,
    build_mirothinker_child_pythonpath,
    prepare_mirothinker_runtime,
)
from event_trader.integrations.mirothinker_task_log import mirothinker_portable_run_id
from event_trader.integrations.strict_mirothinker_agent import (
    StrictMiroThinkerAgentContract,
    StrictMiroThinkerAgentRunResult,
    mirothinker_structured_output_fallback_texts_from_log,
    run_strict_mirothinker_agent,
)
from event_trader.reflection.anchors import ReflectionLogEntry
from event_trader.reflection.context import ReflectionLedgerContext
from event_trader.reflection.contracts import (
    MARKET_CONTEXT_USAGE_QUALITY_LABELS,
    MarketContextUsageQualityLabel,
    OpenPositionEvaluationResult,
    ReflectionContractError,
    ReflectionEvaluationResult,
    ReflectionExposureAssessment,
    ReflectionHorizonAssessment,
    ReflectionPathVerdict,
    ReflectionReviewDecision,
    ReflectionThesisAssessment,
    ReflectionThesisVerdict,
    ReflectionTradabilityVerdict,
    ReflectionTradeAssessment,
    ReflectionTradeVerdict,
    ReflectionWatchlistAssessment,
    ReflectionWatchlistVerdict,
    TargetCloseReflectionEvaluationResult,
    TargetReflectionEvaluationResult,
)
from event_trader.reflection.learning_contracts import (
    ReflectionLearningAction,
    ReflectionLearningContractError,
    ReflectionLearningDecision,
    parse_learning_decision_payload,
)
from event_trader.reflection.market_context_usage import MarketContextUsageFacts
from event_trader.reflection.view_contracts import (
    CloseEpisodeReflectionContext,
    EpisodeReflectionContext,
    OpenPositionReflectionContext,
)
from event_trader.storage import WorkspaceLayout
from event_trader.validation.portfolio_feedback import PortfolioFeedbackSnapshot
from event_trader.validation.returns import HorizonBaseline, ValidationReturnsResult

type ReflectionCallback = Callable[[ReflectionLedgerContext], ReflectionEvaluationResult]
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


class _UsageQualityPayload(TypedDict):
    usage_quality_label: MarketContextUsageQualityLabel | None
    usage_quality_summary: str | None
    market_context_error_labels: tuple[MarketContextUsageQualityLabel, ...]

_REFLECTION_PAGE_EXCERPT_CHAR_LIMIT = 4_000
_REFLECTION_EVIDENCE_EXCERPT_CHAR_LIMIT = 1_200
_REFLECTION_STATE_RATIONALE_EXCERPT_CHAR_LIMIT = 1_200
_REFLECTION_PAGE_MAP_SECTION_EXCERPT_CHAR_LIMIT = 280
_REFLECTION_PAGE_MAP_SECTION_LIMIT = 10
_REFLECTION_BRIEF_EVIDENCE_SAMPLE_LIMIT = 3
_REFLECTION_BRIEF_STATE_CHANGE_SAMPLE_LIMIT = 5
_REFLECTION_DECISION_EPISODE_SINCE_FULL_THRESHOLD = 32
_REFLECTION_DECISION_EPISODE_SINCE_HARD_CAP = 48
_REFLECTION_DECISION_EPISODE_RECENT_TAIL_COUNT = 12
_REFLECTION_DECISION_EPISODE_ABSOLUTE_ITEM_FUSE = 80
_REFLECTION_EVALUATION_MAX_ATTEMPTS = 3


def _format_quoted_enum(values: tuple[str, ...]) -> str:
    if len(values) == 1:
        return f'"{values[0]}"'
    head = ", ".join(f'"{value}"' for value in values[:-1])
    return f'{head}, or "{values[-1]}"'


_REFLECTION_TRADE_VERDICTS: tuple[ReflectionTradeVerdict, ...] = (
    "win",
    "loss",
    "scratch",
    "missed",
    "not_taken",
    "open",
)
_CLOSED_REFLECTION_TRADE_VERDICTS = tuple(
    verdict for verdict in _REFLECTION_TRADE_VERDICTS if verdict != "open"
)
_REFLECTION_MEMORY_STATUSES = (
    "provisional",
    "validated",
    "invalidated",
    "finalized",
)
_REFLECTION_VALIDATION_BASES = (
    "current_evidence",
    "later_evidence",
    "market_return",
    "execution_feedback",
    "portfolio_feedback",
    "close_review",
)
_REFLECTION_SOURCE_REF_KEYS = (
    "evidence_event_ids",
    "market_bar_refs",
    "review_paths",
    "pm_review_request_ids",
    "pm_decision_ids",
    "execution_record_ids",
    "portfolio_record_ids",
)
_THESIS_ASSESSMENT_VERDICT_INSTRUCTION = (
    '"verdict" must be "validated", "mixed", "invalidated", or "unclear"'
)
_ALL_TRADE_ASSESSMENT_VERDICT_INSTRUCTION = (
    "trade_assessment.verdict must be one of "
    + _format_quoted_enum(_REFLECTION_TRADE_VERDICTS)
    + "."
)
_CLOSE_TRADE_ASSESSMENT_VERDICT_INSTRUCTION = (
    "trade_assessment.verdict must be one of "
    + _format_quoted_enum(_CLOSED_REFLECTION_TRADE_VERDICTS)
    + '. Close review must never use "open".'
)
_MARKET_CONTEXT_USAGE_QUALITY_FIELD_NAMES = (
    "usage_quality_label",
    "usage_quality_summary",
    "market_context_error_labels",
)
_MARKET_CONTEXT_USAGE_QUALITY_LABEL_INSTRUCTION = (
    '"usage_quality_label" must be one of '
    + ", ".join(f'"{label}"' for label in MARKET_CONTEXT_USAGE_QUALITY_LABELS)
)
_MARKET_CONTEXT_ERROR_LABELS_INSTRUCTION = (
    '"market_context_error_labels" must be [] or an array of distinct strings '
    "chosen only from "
    + _format_quoted_enum(MARKET_CONTEXT_USAGE_QUALITY_LABELS)
    + '. Do not use learning-decision error_attributions such as '
    '"interpretation_error".'
)
_LEARNING_MEMORY_STATUS_INSTRUCTION = (
    '"memory_status" must be one of '
    + _format_quoted_enum(_REFLECTION_MEMORY_STATUSES)
    + "."
)
_LEARNING_VALIDATION_BASIS_INSTRUCTION = (
    '"validation_basis" must be one of '
    + _format_quoted_enum(_REFLECTION_VALIDATION_BASES)
    + "."
)
_LEARNING_TIMESTAMP_INSTRUCTION = (
    '"source_visible_through" and "usable_from" must be ISO datetimes '
    '(for example "2026-03-12T02:30:00+00:00").'
)
_LEARNING_SOURCE_REFS_INSTRUCTION = (
    '"source_refs" must be a JSON object, not an array. Allowed keys are '
    + _format_quoted_enum(_REFLECTION_SOURCE_REF_KEYS)
    + "; each present key must map to an array of non-blank strings. "
    "Omit unused keys or set them to []."
)
_LEARNING_CONFIDENCE_INSTRUCTION = (
    '"confidence" must be a number between 0 and 1, not a label like "medium".'
)
_MARKET_CONTEXT_USAGE_QUALITY_PROMPT = (
    "Market-context usage evaluation:\n"
    "- If market_context_usage is present, evaluate market-context usage only from "
    "market_context_usage.decision_usages, "
    "market_context_usage.post_decision_outcome_summary, portfolio_feedback, and "
    "existing episode/terminal outcome facts.\n"
    "- Do not infer unavailable market data. Do not use or ask for raw "
    "decision-time market audit payloads.\n"
    "- Do not claim market context caused returns unless supported by validation "
    "facts.\n"
    "- Distinguish ignored context from context that existed but was not relevant.\n"
    "- Ask: Was market context visible at decision time? Which components were "
    "available, partial, unavailable, or missing? Did the decision text actually "
    "reference available context? Did ignored context plausibly matter after "
    "outcomes were known? Did the decision overtrust partial/unavailable context? "
    "Was context reasonable but exposure/positioning wrong? Was context itself "
    "misleading in hindsight?\n"
    "- Use \"inconclusive\" when evidence is insufficient.\n"
    "- Use \"not_applicable\" when market_context_usage is absent.\n\n"
)
_LEARNING_DECISION_PROMPT = (
    "Learning-decision rules:\n"
    "- Always emit learning_decision. There is no fallback writeback heuristic.\n"
    "- Reflection learning writes only typed EpisodeMemory candidates. Shared lessons, "
    "prompt assets, research claims, learning cards, and risk-policy candidates are "
    "controlled by deterministic promotion paths, not this output contract.\n"
    "- Reflection emits only EpisodeMemory delta/no-update candidates. Promotion intent "
    "is not a direct artifact write.\n"
    "- Close/finalized review must end with a promotion intent batch after the finalized "
    "close_review delta is written.\n"
    "- If a close/finalized review warrants downstream promotion, include one positive "
    "promotion object inside the delta payload. If not, omit promotion and the lifecycle "
    "will deterministically write a no_promote intent.\n"
    "- Do not use removed shared-lesson or prompt-asset write surfaces.\n"
    "- If review_decision is skip_review, learning_decision.primary_outcome must be "
    "no_learning_write and actions must contain no write action.\n"
    "- If review_decision is write_review, learning_decision.primary_outcome must be "
    "emit_episode_memory_candidate.\n"
    "- If no durable delta is warranted for a written review, emit "
    "candidate_kind='no_update'.\n"
    "- actions must be non-empty. no_learning_write still requires one non-writing "
    "action object that records the rationale.\n"
    "- When the brief contains expected_source_review_path, use "
    "expected_source_review_path exactly as each action's source_review_path; do "
    "not construct review paths yourself.\n"
    "- effective_from must be an ISO timestamp at or after the reviewed outcome time.\n"
    "- error_attributions must use only: attention_error, interpretation_error, "
    "timing_error, sizing_error, risk_management_error, execution_error, exit_error, "
    "market_regime_error, unavoidable_noise.\n\n"
    "learning_decision shape:\n"
    '{"primary_outcome": string, "actions": ['
    '{"action_id": string, "outcome": string, "rationale": string, '
    '"source_episode_id": string, "source_review_path": string, '
    '"effective_from": string, "error_attributions": [string], "payload": object}'
    "]}\n"
    "Allowed outcomes: no_learning_write, emit_episode_memory_candidate.\n"
    "EpisodeMemory payload requirements:\n"
    "- candidate_kind='delta' requires delta_kind, memory_status, validation_basis, "
    "source_visible_through, usable_from, source_refs, summary_md, thesis_delta, "
    "pm_management_delta, risk_delta, invalidation_delta, error_attributions, "
    "confidence, and supersedes_delta_ids.\n"
    "- candidate_kind='no_update' requires reason or reason_md, reason_code, "
    "source_visible_through, usable_from, source_refs, and reviewed_delta_ids.\n"
    f"- {_LEARNING_MEMORY_STATUS_INSTRUCTION}\n"
    f"- {_LEARNING_VALIDATION_BASIS_INSTRUCTION}\n"
    f"- {_LEARNING_TIMESTAMP_INSTRUCTION}\n"
    f"- {_LEARNING_SOURCE_REFS_INSTRUCTION}\n"
    f"- {_LEARNING_CONFIDENCE_INSTRUCTION}\n"
    "- Promotion is allowed only for a delta candidate with "
    'memory_status="finalized" and validation_basis="close_review".\n\n'
)


def _reflection_role_prompt(*, mission: str) -> str:
    return (
        "Role:\n"
        "You are the post-trade reviewer for event-trader.\n"
        f"{mission}\n\n"
    )


def _reflection_operating_boundary_prompt() -> str:
    return (
        "Operating boundary:\n"
        "- Reflection is a review and learning surface, not an execution surface.\n"
        "- Use only deterministic reflection context plus reflection read tools.\n"
        "- If the bounded context is insufficient for a defensible review, choose "
        "skip_review rather than inventing certainty.\n\n"
    )
_REFLECTION_MCP_SERVER_NAME = "event_trader_reflection"
_REFLECTION_REQUIRED_MCP_TOOLS = (
    (_REFLECTION_MCP_SERVER_NAME, "list_evidence_window"),
    (_REFLECTION_MCP_SERVER_NAME, "read_evidence"),
    (_REFLECTION_MCP_SERVER_NAME, "list_state_changes"),
    (_REFLECTION_MCP_SERVER_NAME, "list_decision_episodes"),
    (_REFLECTION_MCP_SERVER_NAME, "read_decision_episode"),
    (_REFLECTION_MCP_SERVER_NAME, "read_target_page"),
    (_REFLECTION_MCP_SERVER_NAME, "read_target_section"),
    (_REFLECTION_MCP_SERVER_NAME, "search_target_memory"),
)
_REFLECTION_READ_TOOL_NAMES = frozenset(
    tool_name for _, tool_name in _REFLECTION_REQUIRED_MCP_TOOLS
)
_REFLECTION_AGENT_CONTRACT = StrictMiroThinkerAgentContract(
    agent_name="MiroThinker reflection agent",
    required_tools=_REFLECTION_REQUIRED_MCP_TOOLS,
)


class MiroThinkerReflectionRuntimeError(RuntimeError):
    """Raised when the committed reflection runtime cannot execute."""


class _ReflectionContractRepairRuntimeError(MiroThinkerReflectionRuntimeError):
    def __init__(
        self,
        failure: _ReflectionContractFailure,
        *,
        attempt_count: int,
        max_attempts: int,
    ) -> None:
        self.failure = failure
        self.attempt_count = attempt_count
        self.max_attempts = max_attempts
        super().__init__(
            str(
                _ReflectionContractRepairRequired(
                    failure,
                    attempt_count=attempt_count,
                    max_attempts=max_attempts,
                )
            )
        )


@dataclass(frozen=True, slots=True)
class _ReflectionContractFailure:
    surface: str
    error_code: str
    message: str
    recoverable: bool
    suggested_action: str
    details: Mapping[str, object]


class _ReflectionContractRepairRequired(Exception):
    def __init__(
        self,
        failure: _ReflectionContractFailure,
        *,
        attempt_count: int,
        max_attempts: int,
    ) -> None:
        self.failure = failure
        self.attempt_count = attempt_count
        self.max_attempts = max_attempts
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        attempted = self.failure.details.get("attempted_tools")
        attempted_text = ""
        if isinstance(attempted, tuple):
            attempted_text = f" attempted_tools={','.join(attempted) or '<none>'}"
        return (
            "Reflection contract repair required: "
            f"surface={self.failure.surface} "
            f"error_code={self.failure.error_code} "
            f"attempt={self.attempt_count}/{self.max_attempts}"
            f"{attempted_text}: {self.failure.message}"
        )


@dataclass(frozen=True, slots=True)
class _ReflectionToolUseTrace:
    attempted_tool_names: tuple[str, ...]
    successful_tool_names: tuple[str, ...]
    failure_details: tuple[str, ...] = ()


class _ReflectionToolTraceRecorder:
    def __init__(self) -> None:
        self._attempted_tool_names: list[str] = []
        self._successful_tool_names: list[str] = []
        self._failure_details: list[str] = []
        self._tool_manager: Any | None = None
        self._original_execute_tool_call: Any | None = None

    def install(self, tool_manager: Any) -> None:
        original_execute_tool_call = getattr(tool_manager, "execute_tool_call", None)
        if not callable(original_execute_tool_call):
            raise MiroThinkerReflectionRuntimeError(
                "Could not install reflection read audit recorder; "
                "ToolManager execute_tool_call is unavailable."
            )
        self._tool_manager = tool_manager
        self._original_execute_tool_call = original_execute_tool_call

        async def recorded_execute_tool_call(
            *args: object,
            **kwargs: object,
        ) -> Any:
            server_name, tool_name, _arguments = _normalize_reflection_tool_call_args(
                args,
                kwargs,
            )
            if server_name == _REFLECTION_MCP_SERVER_NAME:
                self._attempted_tool_names.append(tool_name)
            try:
                tool_result = await original_execute_tool_call(*args, **kwargs)
            except Exception as exc:
                if server_name == _REFLECTION_MCP_SERVER_NAME:
                    self._failure_details.append(
                        f"{tool_name}: {type(exc).__name__}: {exc}"
                    )
                raise
            failure_text = _reflection_tool_result_failure_text(tool_result)
            if server_name == _REFLECTION_MCP_SERVER_NAME and failure_text is not None:
                self._failure_details.append(f"{tool_name}: {failure_text}")
            elif server_name == _REFLECTION_MCP_SERVER_NAME:
                self._successful_tool_names.append(tool_name)
            return tool_result

        tool_manager.execute_tool_call = recorded_execute_tool_call

    def uninstall(self) -> None:
        if self._tool_manager is not None and self._original_execute_tool_call is not None:
            self._tool_manager.execute_tool_call = self._original_execute_tool_call
        self._tool_manager = None
        self._original_execute_tool_call = None

    def snapshot(self) -> _ReflectionToolUseTrace:
        return _ReflectionToolUseTrace(
            attempted_tool_names=tuple(self._attempted_tool_names),
            successful_tool_names=_unique_strings(self._successful_tool_names),
            failure_details=tuple(self._failure_details),
        )


def _normalize_reflection_tool_call_args(
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


def _reflection_tool_result_failure_text(tool_result: object) -> str | None:
    if isinstance(tool_result, Mapping):
        error = tool_result.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()

        success = tool_result.get("success")
        if success is False or (
            isinstance(success, str) and success.strip().lower() in {"false", "0"}
        ):
            return "tool returned success=false"

        status = tool_result.get("status")
        if isinstance(status, str) and status.strip().lower() in {"error", "failed"}:
            return f"tool returned status={status}"

        raw_result = tool_result.get("result")
        if raw_result is not None:
            return _reflection_tool_payload_failure_text(raw_result)
    return _reflection_tool_payload_failure_text(tool_result)


def _reflection_tool_payload_failure_text(raw_payload: object) -> str | None:
    payload = raw_payload
    if isinstance(raw_payload, str):
        normalized = raw_payload.strip()
        if not normalized:
            return None
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, Mapping):
        return None

    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()

    success = payload.get("success")
    if success is False or (
        isinstance(success, str) and success.strip().lower() in {"false", "0"}
    ):
        return "success=false"

    status = payload.get("status")
    if isinstance(status, str) and status.strip().lower() in {"error", "failed"}:
        return f"status={status}"
    return None


def _unique_strings(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return tuple(unique)


@dataclass(frozen=True, slots=True)
class MiroThinkerReflectionRuntimeConfig:
    """Explicit runtime inputs required to execute one reflection pass."""

    vendor_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_max_context_length: int

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
            raise MiroThinkerReflectionRuntimeError(
                "llm_max_context_length must be a positive integer."
            )


def build_mirothinker_reflection_runner(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    layout: WorkspaceLayout,
) -> ReflectionCallback:
    """Build the committed shared reflection evaluator using vendored MiroThinker."""
    if not isinstance(layout, WorkspaceLayout):
        raise MiroThinkerReflectionRuntimeError(
            "layout must be a WorkspaceLayout instance."
        )
    vendor_root = config.vendor_root.resolve(strict=False)
    workspace_root = layout.root.resolve(strict=False)

    def evaluate(context: ReflectionLedgerContext) -> ReflectionEvaluationResult:
        if not isinstance(context, ReflectionLedgerContext):
            raise MiroThinkerReflectionRuntimeError(
                "context must be a ReflectionLedgerContext instance."
            )
        _assert_not_in_event_loop()
        return asyncio.run(
            _run_shared_reflection_once(
                config=config,
                vendor_root=vendor_root,
                workspace_root=workspace_root,
                context=context,
            )
        )

    return evaluate


def build_mirothinker_target_reflection_runner(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    layout: WorkspaceLayout,
) -> TargetReflectionCallback:
    """Build the committed target reflection evaluator using vendored MiroThinker."""
    if not isinstance(layout, WorkspaceLayout):
        raise MiroThinkerReflectionRuntimeError(
            "layout must be a WorkspaceLayout instance."
        )
    vendor_root = config.vendor_root.resolve(strict=False)
    workspace_root = layout.root.resolve(strict=False)

    def evaluate(
        context: EpisodeReflectionContext,
    ) -> TargetReflectionEvaluationResult:
        if not isinstance(context, EpisodeReflectionContext):
            raise MiroThinkerReflectionRuntimeError(
                "context must be an EpisodeReflectionContext instance."
            )
        _assert_not_in_event_loop()
        return asyncio.run(
            _run_target_reflection_once(
                config=config,
                vendor_root=vendor_root,
                workspace_root=workspace_root,
                context=context,
            )
        )

    return evaluate


def build_mirothinker_target_close_reflection_runner(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    layout: WorkspaceLayout,
) -> TargetCloseReflectionCallback:
    """Build the committed target close reflection evaluator."""
    if not isinstance(layout, WorkspaceLayout):
        raise MiroThinkerReflectionRuntimeError(
            "layout must be a WorkspaceLayout instance."
        )
    vendor_root = config.vendor_root.resolve(strict=False)
    workspace_root = layout.root.resolve(strict=False)

    def evaluate(
        context: CloseEpisodeReflectionContext,
    ) -> TargetCloseReflectionEvaluationResult:
        if not isinstance(context, CloseEpisodeReflectionContext):
            raise MiroThinkerReflectionRuntimeError(
                "context must be a CloseEpisodeReflectionContext instance."
            )
        _assert_not_in_event_loop()
        return asyncio.run(
            _run_target_close_reflection_once(
                config=config,
                vendor_root=vendor_root,
                workspace_root=workspace_root,
                context=context,
            )
        )

    return evaluate


def build_mirothinker_open_position_reflection_runner(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    layout: WorkspaceLayout,
) -> OpenPositionReflectionCallback:
    """Build the committed open-position reflection evaluator."""
    if not isinstance(layout, WorkspaceLayout):
        raise MiroThinkerReflectionRuntimeError(
            "layout must be a WorkspaceLayout instance."
        )
    vendor_root = config.vendor_root.resolve(strict=False)
    workspace_root = layout.root.resolve(strict=False)

    def evaluate(
        context: OpenPositionReflectionContext,
    ) -> OpenPositionEvaluationResult:
        if not isinstance(context, OpenPositionReflectionContext):
            raise MiroThinkerReflectionRuntimeError(
                "context must be a OpenPositionReflectionContext instance."
            )
        _assert_not_in_event_loop()
        try:
            return asyncio.run(
                _run_open_position_reflection_once(
                    config=config,
                    vendor_root=vendor_root,
                    workspace_root=workspace_root,
                    context=context,
                )
            )
        except MiroThinkerReflectionRuntimeError as exc:
            if _is_unfinished_terminal_final_output_failure(exc):
                return _skipped_open_position_contract_evaluation(
                    context=context,
                    failure_reason=str(exc),
                )
            raise

    return evaluate


def _is_unfinished_terminal_final_output_failure(
    exc: MiroThinkerReflectionRuntimeError,
) -> bool:
    return (
        isinstance(exc, _ReflectionContractRepairRuntimeError)
        and exc.failure.surface == "final_output"
        and exc.failure.error_code in {"boxed_non_json", "missing_boxed_json"}
    )


def _skipped_open_position_contract_evaluation(
    *,
    context: OpenPositionReflectionContext,
    failure_reason: str,
) -> OpenPositionEvaluationResult:
    review_page_path = _open_position_review_page_path_for_context(context)
    rationale = (
        "Terminal open-position reflection skipped because MiroThinker did not "
        "return a usable final boxed JSON payload after bounded contract repair. "
        f"No review or learning artifact should be written. Failure: {failure_reason}"
    )
    return OpenPositionEvaluationResult(
        episode_id=context.episode.episode_id,
        target_key=context.episode.target_key,
        thesis_assessment=ReflectionThesisAssessment(
            verdict="unclear",
            summary_md=(
                "Reflection evaluation did not complete because the agent failed the "
                "final output contract."
            ),
        ),
        watchlist_assessment=ReflectionWatchlistAssessment(
            verdict="unclear",
            summary_md=(
                "Watchlist quality was not evaluated because the agent failed the "
                "final output contract."
            ),
        ),
        trade_assessment=ReflectionTradeAssessment(
            verdict="open",
            summary_md=(
                "Open-position trade quality was not evaluated because the agent "
                "failed the final output contract."
            ),
            return_pct=context.terminal_mark.strategy_return * 100.0,
        ),
        exposure_assessment=ReflectionExposureAssessment(
            summary_md=(
                "Open exposure was not assessed because the reflection agent "
                "failed the final output contract."
            ),
        ),
        review_decision="skip_review",
        decision_rationale=rationale,
        learning_decision=ReflectionLearningDecision(
            primary_outcome="no_learning_write",
            actions=(
                ReflectionLearningAction(
                    action_id=(
                        "learning:"
                        f"{context.episode.episode_id}:terminal_final_output_skip"
                    ),
                    outcome="no_learning_write",
                    rationale=rationale,
                    source_episode_id=context.episode.episode_id,
                    source_review_path=review_page_path,
                    effective_from=context.terminal_mark.replay_end_at,
                    error_attributions=(),
                    payload={},
                ),
            ),
        ),
        usage_quality_label="inconclusive",
        usage_quality_summary=(
            "Market-context usage was not evaluated because the reflection agent "
            "failed the final output contract."
        ),
        market_context_error_labels=(),
    )


def _open_position_review_page_path_for_context(
    context: OpenPositionReflectionContext,
) -> str:
    if context.review_kind == "open_position_material_update":
        if context.open_position_material_update_sequence is None:
            raise MiroThinkerReflectionRuntimeError(
                "open material-update reflection context is missing "
                "material-update metadata."
            )
        return (
            f"targets/{context.episode.target_key}/reviews/"
            f"{_format_review_path_timestamp(context.episode.opened_at)}_"
            f"{_format_review_path_timestamp(context.terminal_mark.replay_end_at)}_"
            f"open_position_material_update_n"
            f"{context.open_position_material_update_sequence}.md"
        )
    return (
        f"targets/{context.episode.target_key}/reviews/"
        f"{_format_review_path_timestamp(context.episode.opened_at)}_"
        f"{_format_review_path_timestamp(context.terminal_mark.replay_end_at)}_"
        f"open_position_horizon_{_format_review_path_hour_label(context.review_horizon_hours)}.md"
    )


def _format_review_path_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%y%m%dT%H%M%S")


def _format_review_path_hour_label(hour: float) -> str:
    if float(hour).is_integer():
        return f"{int(hour)}h"
    return f"{format(hour, 'g')}h"


async def _run_shared_reflection_once(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    vendor_root: Path,
    workspace_root: Path,
    context: ReflectionLedgerContext,
) -> ReflectionEvaluationResult:
    execute_task_pipeline, create_pipeline_components, OmegaConf = _load_vendor_runtime(
        vendor_root
    )
    agent_cfg = _build_agent_cfg(config)
    _prepare_vendor_environment(config)
    config.log_dir.mkdir(parents=True, exist_ok=True)

    main_agent_tool_manager, sub_agent_tool_managers, output_formatter = (
        create_pipeline_components(agent_cfg)
    )
    task_id = mirothinker_portable_run_id(
        f"event-trader-reflection-{context.anchor_id.replace('/', '_').replace('#', '_')}"
    )
    _install_reflection_mcp_server(
        tool_manager=main_agent_tool_manager,
        vendor_root=vendor_root,
        workspace_root=workspace_root,
        target_key=context.anchor.target_key,
        window_start=context.receipt.later_evidence_window_start,
        window_end=context.receipt.later_evidence_window_end,
    )
    def _evaluate_run_result(
        run_result: StrictMiroThinkerAgentRunResult,
    ) -> ReflectionEvaluationResult:
        payload = _load_reflection_payload(
            final_boxed_answer=run_result.final_boxed_answer,
            final_summary=run_result.final_summary,
            task_id=task_id,
            log_dir=config.log_dir,
            log_file_path=run_result.log_file_path,
        )
        normalized = _normalize_reflection_payload_object(payload)
        usage_quality = _normalize_usage_quality_payload(normalized)
        return ReflectionEvaluationResult(
            anchor=context.anchor,
            coverage=context.coverage,
            assessed_horizons=_normalize_horizon_assessments(
                normalized["assessed_horizons"]
            ),
            thesis_assessment=_normalize_thesis_assessment(
                normalized["thesis_assessment"]
            ),
            trade_assessment=_normalize_trade_assessment(
                normalized.get("trade_assessment")
            ),
            review_decision=_require_review_decision(
                normalized["review_decision"],
                field_name="review_decision",
            ),
            decision_rationale=_require_string(
                normalized["decision_rationale"],
                field_name="decision_rationale",
            ),
            usage_quality_label=usage_quality["usage_quality_label"],
            usage_quality_summary=usage_quality["usage_quality_summary"],
            market_context_error_labels=usage_quality["market_context_error_labels"],
        )

    return await _run_reflection_agent_with_evaluation_retry(
        tool_manager=main_agent_tool_manager,
        execute_task_pipeline=execute_task_pipeline,
        cfg=agent_cfg,
        task_id=task_id,
        task_description=_build_shared_reflection_prompt(
            context=context,
        ),
        sub_agent_tool_managers=sub_agent_tool_managers,
        output_formatter=output_formatter,
        log_dir=config.log_dir,
        evaluate_run_result=_evaluate_run_result,
    )


async def _run_target_reflection_once(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    vendor_root: Path,
    workspace_root: Path,
    context: EpisodeReflectionContext,
) -> TargetReflectionEvaluationResult:
    execute_task_pipeline, create_pipeline_components, OmegaConf = _load_vendor_runtime(
        vendor_root
    )
    agent_cfg = _build_agent_cfg(config)
    _prepare_vendor_environment(config)
    config.log_dir.mkdir(parents=True, exist_ok=True)

    main_agent_tool_manager, sub_agent_tool_managers, output_formatter = (
        create_pipeline_components(agent_cfg)
    )
    task_id = mirothinker_portable_run_id(
        f"event-trader-target-reflection-{context.episode.episode_id}"
    )
    _install_reflection_mcp_server(
        tool_manager=main_agent_tool_manager,
        vendor_root=vendor_root,
        workspace_root=workspace_root,
        target_key=context.episode.target_key,
        window_start=context.episode.opened_at,
        window_end=_target_reflection_window_end(context),
    )
    def _evaluate_run_result(
        run_result: StrictMiroThinkerAgentRunResult,
    ) -> TargetReflectionEvaluationResult:
        payload = _load_reflection_payload(
            final_boxed_answer=run_result.final_boxed_answer,
            final_summary=run_result.final_summary,
            task_id=task_id,
            log_dir=config.log_dir,
            log_file_path=run_result.log_file_path,
            required_fields=(
                "episode_id",
                "target_key",
                "watchlist_assessment",
                "learning_decision",
            ),
        )
        payload = _normalize_reflection_payload_object(
            payload,
            required_fields=(
                "episode_id",
                "target_key",
                "watchlist_assessment",
                "learning_decision",
            ),
        )
        episode_id = _require_string(payload["episode_id"], field_name="episode_id")
        if episode_id != context.episode.episode_id:
            raise MiroThinkerReflectionRuntimeError(
                "Reflection agent must return the active episode_id."
            )
        target_key = _require_string(payload["target_key"], field_name="target_key")
        if target_key != context.episode.target_key:
            raise MiroThinkerReflectionRuntimeError(
                "Reflection agent must return the active target_key."
            )
        review_decision = _require_review_decision(
            payload["review_decision"],
            field_name="review_decision",
        )
        usage_quality = _normalize_usage_quality_payload(payload)
        return TargetReflectionEvaluationResult(
            episode_id=episode_id,
            target_key=target_key,
            coverage=context.coverage,
            assessed_horizons=_normalize_horizon_assessments(
                payload["assessed_horizons"]
            ),
            thesis_assessment=_normalize_thesis_assessment(
                payload["thesis_assessment"]
            ),
            watchlist_assessment=_normalize_watchlist_assessment(
                payload["watchlist_assessment"]
            ),
            trade_assessment=_normalize_trade_assessment(payload.get("trade_assessment")),
            review_decision=review_decision,
            decision_rationale=_require_string(
                payload["decision_rationale"],
                field_name="decision_rationale",
            ),
            learning_decision=parse_learning_decision_payload(
                payload["learning_decision"],
                review_decision=review_decision,
            ),
            usage_quality_label=usage_quality["usage_quality_label"],
            usage_quality_summary=usage_quality["usage_quality_summary"],
            market_context_error_labels=usage_quality["market_context_error_labels"],
        )

    return await _run_reflection_agent_with_evaluation_retry(
        tool_manager=main_agent_tool_manager,
        execute_task_pipeline=execute_task_pipeline,
        cfg=agent_cfg,
        task_id=task_id,
        task_description=_build_target_reflection_prompt(
            context=context,
        ),
        sub_agent_tool_managers=sub_agent_tool_managers,
        output_formatter=output_formatter,
        log_dir=config.log_dir,
        evaluate_run_result=_evaluate_run_result,
    )


async def _run_open_position_reflection_once(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    vendor_root: Path,
    workspace_root: Path,
    context: OpenPositionReflectionContext,
) -> OpenPositionEvaluationResult:
    execute_task_pipeline, create_pipeline_components, OmegaConf = _load_vendor_runtime(
        vendor_root
    )
    agent_cfg = _build_agent_cfg(config)
    _prepare_vendor_environment(config)
    config.log_dir.mkdir(parents=True, exist_ok=True)

    main_agent_tool_manager, sub_agent_tool_managers, output_formatter = (
        create_pipeline_components(agent_cfg)
    )
    task_id = mirothinker_portable_run_id(
        f"event-trader-terminal-reflection-{context.episode.episode_id}"
    )
    _install_reflection_mcp_server(
        tool_manager=main_agent_tool_manager,
        vendor_root=vendor_root,
        workspace_root=workspace_root,
        target_key=context.episode.target_key,
        window_start=context.episode.opened_at,
        window_end=context.terminal_mark.replay_end_at,
    )
    def _evaluate_run_result(
        run_result: StrictMiroThinkerAgentRunResult,
    ) -> OpenPositionEvaluationResult:
        payload = _load_reflection_payload(
            final_boxed_answer=run_result.final_boxed_answer,
            final_summary=run_result.final_summary,
            task_id=task_id,
            log_dir=config.log_dir,
            log_file_path=run_result.log_file_path,
            required_fields=(
                "episode_id",
                "target_key",
                "watchlist_assessment",
                "exposure_assessment",
                "learning_decision",
            ),
            excluded_fields=("assessed_horizons",),
        )
        payload = _normalize_reflection_payload_object(
            payload,
            required_fields=(
                "episode_id",
                "target_key",
                "watchlist_assessment",
                "exposure_assessment",
                "learning_decision",
            ),
            excluded_fields=("assessed_horizons",),
        )
        episode_id = _require_string(payload["episode_id"], field_name="episode_id")
        if episode_id != context.episode.episode_id:
            raise MiroThinkerReflectionRuntimeError(
                "Reflection agent must return the active episode_id."
            )
        target_key = _require_string(payload["target_key"], field_name="target_key")
        if target_key != context.episode.target_key:
            raise MiroThinkerReflectionRuntimeError(
                "Reflection agent must return the active target_key."
            )
        trade_assessment = _normalize_trade_assessment(payload.get("trade_assessment"))
        if trade_assessment is None:
            raise MiroThinkerReflectionRuntimeError(
                "open-position reflection requires trade_assessment."
            )
        review_decision = _require_review_decision(
            payload["review_decision"],
            field_name="review_decision",
        )
        usage_quality = _normalize_usage_quality_payload(payload)
        return OpenPositionEvaluationResult(
            episode_id=episode_id,
            target_key=target_key,
            thesis_assessment=_normalize_thesis_assessment(
                payload["thesis_assessment"]
            ),
            watchlist_assessment=_normalize_watchlist_assessment(
                payload["watchlist_assessment"]
            ),
            trade_assessment=trade_assessment,
            exposure_assessment=_normalize_exposure_assessment(
                payload["exposure_assessment"]
            ),
            review_decision=review_decision,
            decision_rationale=_require_string(
                payload["decision_rationale"],
                field_name="decision_rationale",
            ),
            learning_decision=parse_learning_decision_payload(
                payload["learning_decision"],
                review_decision=review_decision,
            ),
            usage_quality_label=usage_quality["usage_quality_label"],
            usage_quality_summary=usage_quality["usage_quality_summary"],
            market_context_error_labels=usage_quality["market_context_error_labels"],
        )

    return await _run_reflection_agent_with_evaluation_retry(
        tool_manager=main_agent_tool_manager,
        execute_task_pipeline=execute_task_pipeline,
        cfg=agent_cfg,
        task_id=task_id,
        task_description=_build_open_position_reflection_prompt(
            context=context,
        ),
        sub_agent_tool_managers=sub_agent_tool_managers,
        output_formatter=output_formatter,
        log_dir=config.log_dir,
        evaluate_run_result=_evaluate_run_result,
    )


async def _run_target_close_reflection_once(
    *,
    config: MiroThinkerReflectionRuntimeConfig,
    vendor_root: Path,
    workspace_root: Path,
    context: CloseEpisodeReflectionContext,
) -> TargetCloseReflectionEvaluationResult:
    execute_task_pipeline, create_pipeline_components, OmegaConf = _load_vendor_runtime(
        vendor_root
    )
    agent_cfg = _build_agent_cfg(config)
    _prepare_vendor_environment(config)
    config.log_dir.mkdir(parents=True, exist_ok=True)

    main_agent_tool_manager, sub_agent_tool_managers, output_formatter = (
        create_pipeline_components(agent_cfg)
    )
    task_id = mirothinker_portable_run_id(
        f"event-trader-close-reflection-{context.episode.episode_id}"
    )
    _install_reflection_mcp_server(
        tool_manager=main_agent_tool_manager,
        vendor_root=vendor_root,
        workspace_root=workspace_root,
        target_key=context.episode.target_key,
        window_start=context.episode.opened_at,
        window_end=_require_datetime(
            context.episode.closed_at,
            field_name="episode.closed_at",
        ),
    )
    def _evaluate_run_result(
        run_result: StrictMiroThinkerAgentRunResult,
    ) -> TargetCloseReflectionEvaluationResult:
        payload = _load_reflection_payload(
            final_boxed_answer=run_result.final_boxed_answer,
            final_summary=run_result.final_summary,
            task_id=task_id,
            log_dir=config.log_dir,
            log_file_path=run_result.log_file_path,
            required_fields=(
                "episode_id",
                "target_key",
                "watchlist_assessment",
                "learning_decision",
            ),
            excluded_fields=("assessed_horizons",),
        )
        payload = _normalize_reflection_payload_object(
            payload,
            required_fields=(
                "episode_id",
                "target_key",
                "watchlist_assessment",
                "learning_decision",
            ),
            excluded_fields=("assessed_horizons",),
        )
        episode_id = _require_string(payload["episode_id"], field_name="episode_id")
        if episode_id != context.episode.episode_id:
            raise MiroThinkerReflectionRuntimeError(
                "Reflection agent must return the active episode_id."
            )
        target_key = _require_string(payload["target_key"], field_name="target_key")
        if target_key != context.episode.target_key:
            raise MiroThinkerReflectionRuntimeError(
                "Reflection agent must return the active target_key."
            )
        trade_assessment = _normalize_trade_assessment(payload.get("trade_assessment"))
        if trade_assessment is None:
            raise MiroThinkerReflectionRuntimeError(
                "target close reflection requires trade_assessment."
            )
        review_decision = _require_review_decision(
            payload["review_decision"],
            field_name="review_decision",
        )
        usage_quality = _normalize_usage_quality_payload(payload)
        return TargetCloseReflectionEvaluationResult(
            episode_id=episode_id,
            target_key=target_key,
            thesis_assessment=_normalize_thesis_assessment(
                payload["thesis_assessment"]
            ),
            watchlist_assessment=_normalize_watchlist_assessment(
                payload["watchlist_assessment"]
            ),
            trade_assessment=trade_assessment,
            review_decision=review_decision,
            decision_rationale=_require_string(
                payload["decision_rationale"],
                field_name="decision_rationale",
            ),
            learning_decision=parse_learning_decision_payload(
                payload["learning_decision"],
                review_decision=review_decision,
            ),
            usage_quality_label=usage_quality["usage_quality_label"],
            usage_quality_summary=usage_quality["usage_quality_summary"],
            market_context_error_labels=usage_quality["market_context_error_labels"],
        )

    return await _run_reflection_agent_with_evaluation_retry(
        tool_manager=main_agent_tool_manager,
        execute_task_pipeline=execute_task_pipeline,
        cfg=agent_cfg,
        task_id=task_id,
        task_description=_build_target_close_reflection_prompt(
            context=context,
        ),
        sub_agent_tool_managers=sub_agent_tool_managers,
        output_formatter=output_formatter,
        log_dir=config.log_dir,
        evaluate_run_result=_evaluate_run_result,
    )


async def _run_reflection_agent_with_evaluation_retry[T](
    *,
    tool_manager: Any,
    execute_task_pipeline: Any,
    cfg: Any,
    task_id: str,
    task_description: str,
    sub_agent_tool_managers: dict[str, Any],
    output_formatter: Any,
    log_dir: Path,
    evaluate_run_result: Callable[[StrictMiroThinkerAgentRunResult], T],
) -> T:
    current_task_description = task_description
    for attempt_index in range(_REFLECTION_EVALUATION_MAX_ATTEMPTS):
        attempt_count = attempt_index + 1
        tool_trace_recorder = _ReflectionToolTraceRecorder()
        tool_trace_recorder.install(tool_manager)
        try:
            run_result = await run_strict_mirothinker_agent(
                contract=_REFLECTION_AGENT_CONTRACT,
                tool_manager=tool_manager,
                execute_task_pipeline=execute_task_pipeline,
                cfg=cfg,
                task_id=task_id,
                task_description=current_task_description,
                task_file_name="",
                sub_agent_tool_managers=sub_agent_tool_managers,
                output_formatter=output_formatter,
                log_dir=log_dir,
                error_factory=MiroThinkerReflectionRuntimeError,
                allow_context_window_failure=True,
            )
        finally:
            tool_trace_recorder.uninstall()
        try:
            result = evaluate_run_result(run_result)
            _assert_reflection_read_audit(
                result=result,
                tool_trace=tool_trace_recorder.snapshot(),
            )
            return result
        except _ReflectionContractRepairRequired as exc:
            if (
                not exc.failure.recoverable
                or attempt_count >= _REFLECTION_EVALUATION_MAX_ATTEMPTS
            ):
                raise _ReflectionContractRepairRuntimeError(
                    exc.failure,
                    attempt_count=attempt_count,
                    max_attempts=_REFLECTION_EVALUATION_MAX_ATTEMPTS,
                ) from exc
            current_task_description = _append_reflection_contract_repair_feedback(
                task_description,
                exc.failure,
            )
        except MiroThinkerReflectionRuntimeError as exc:
            failure = _reflection_final_output_contract_failure(exc)
            if attempt_count >= _REFLECTION_EVALUATION_MAX_ATTEMPTS:
                raise _ReflectionContractRepairRuntimeError(
                    failure,
                    attempt_count=attempt_count,
                    max_attempts=_REFLECTION_EVALUATION_MAX_ATTEMPTS,
                ) from exc
            current_task_description = _append_reflection_contract_repair_feedback(
                task_description,
                failure,
            )
        except ReflectionContractError as exc:
            failure = _reflection_evaluation_contract_failure(exc)
            if attempt_count >= _REFLECTION_EVALUATION_MAX_ATTEMPTS:
                raise _ReflectionContractRepairRuntimeError(
                    failure,
                    attempt_count=attempt_count,
                    max_attempts=_REFLECTION_EVALUATION_MAX_ATTEMPTS,
                ) from exc
            current_task_description = _append_reflection_contract_repair_feedback(
                task_description,
                failure,
            )
        except ReflectionLearningContractError as exc:
            failure = _reflection_learning_contract_failure(exc)
            if attempt_count >= _REFLECTION_EVALUATION_MAX_ATTEMPTS:
                raise _ReflectionContractRepairRuntimeError(
                    failure,
                    attempt_count=attempt_count,
                    max_attempts=_REFLECTION_EVALUATION_MAX_ATTEMPTS,
                ) from exc
            current_task_description = _append_reflection_contract_repair_feedback(
                task_description,
                failure,
            )
    raise MiroThinkerReflectionRuntimeError(
        "reflection evaluation retry loop ended without a result."
    )


def _assert_reflection_read_audit(
    *,
    result: object,
    tool_trace: _ReflectionToolUseTrace | None = None,
) -> None:
    failure = _reflection_read_audit_failure(
        result=result,
        tool_trace=tool_trace,
    )
    if failure is not None:
        raise _ReflectionContractRepairRequired(
            failure,
            attempt_count=1,
            max_attempts=_REFLECTION_EVALUATION_MAX_ATTEMPTS,
        )


def _reflection_read_audit_failure(
    *,
    result: object,
    tool_trace: _ReflectionToolUseTrace | None = None,
) -> _ReflectionContractFailure | None:
    _ = result
    if tool_trace is None:
        attempted_tools: tuple[str, ...] = ()
        failure_details: tuple[str, ...] = ()
        successful_tool_names: set[str] = set()
    else:
        attempted_tools = tuple(tool_trace.attempted_tool_names)
        failure_details = tuple(tool_trace.failure_details)
        successful_tool_names = set(tool_trace.successful_tool_names)
    if successful_tool_names & _REFLECTION_READ_TOOL_NAMES:
        return None
    return _ReflectionContractFailure(
        surface="read_audit",
        error_code="reflection_missing_read_tool",
        message=(
            "Every reflection decision requires at least one successful reflection "
            "read tool call before the final boxed JSON."
        ),
        recoverable=True,
        suggested_action=(
            "Before final boxed JSON, call at least one successful reflection read tool."
        ),
        details={
            "required_tools": tuple(sorted(_REFLECTION_READ_TOOL_NAMES)),
            "attempted_tools": attempted_tools,
            "failure_details": failure_details,
        },
    )


def _reflection_final_output_contract_failure(
    exc: MiroThinkerReflectionRuntimeError,
) -> _ReflectionContractFailure:
    message = str(exc).strip()
    error_code = "reflection_payload_schema_violation"
    if "did not return a boxed JSON payload" in message:
        error_code = "missing_boxed_json"
    elif "returned non-JSON boxed output" in message:
        error_code = "boxed_non_json"
    elif "must return one JSON object" in message:
        error_code = "boxed_non_object"
    elif "missing field(s)" in message:
        error_code = "reflection_payload_missing_fields"
    elif "unexpected field(s)" in message:
        error_code = "reflection_payload_unexpected_fields"
    specific_repair_guidance = _reflection_contract_repair_suggestion(message)
    if error_code == "reflection_payload_unexpected_fields" and (
        "unexpected field(s): return_pct" in message
    ):
        suggested_action = (
            "Return exactly one corrected JSON object wrapped in \\boxed{...}. "
            "Remove the top-level return_pct field. If a return is relevant, put "
            "it inside trade_assessment.return_pct. The top-level object must "
            "contain only the requested reflection fields."
        )
    elif specific_repair_guidance is not None:
        suggested_action = (
            "Return exactly one corrected JSON object wrapped in \\boxed{...}. "
            f"{specific_repair_guidance}"
        )
    else:
        suggested_action = (
            "Return exactly one corrected JSON object wrapped in \\boxed{...}."
        )
    return _ReflectionContractFailure(
        surface="final_output",
        error_code=error_code,
        message=message,
        recoverable=True,
        suggested_action=suggested_action,
        details={"raw_error": message},
    )


def _reflection_learning_contract_failure(
    exc: ReflectionLearningContractError,
) -> _ReflectionContractFailure:
    message = str(exc).strip()
    suggested_action = (
        "Return learning_decision with a non-empty actions array. "
        "Use no_learning_write only with skip_review. For write_review, emit "
        "emit_episode_memory_candidate with candidate_kind='delta' or 'no_update'. "
        "Each action must include source_episode_id, source_review_path, effective_from, "
        "error_attributions, rationale, and payload."
    )
    specific_repair_guidance = _reflection_contract_repair_suggestion(message)
    if specific_repair_guidance is not None:
        suggested_action = f"{suggested_action} {specific_repair_guidance}"
    return _ReflectionContractFailure(
        surface="final_output",
        error_code="learning_decision_contract_violation",
        message=message,
        recoverable=True,
        suggested_action=suggested_action,
        details={"raw_error": message},
    )


def _reflection_evaluation_contract_failure(
    exc: ReflectionContractError,
) -> _ReflectionContractFailure:
    message = str(exc).strip()
    suggested_action = (
        "Return corrected evaluation fields that satisfy the typed reflection "
        "contract. exposure_assessment must be an object with summary_md only; "
        "do not return trade-action fields."
    )
    specific_repair_guidance = _reflection_contract_repair_suggestion(message)
    if specific_repair_guidance is not None:
        suggested_action = f"{suggested_action} {specific_repair_guidance}"
    return _ReflectionContractFailure(
        surface="final_output",
        error_code="reflection_evaluation_contract_violation",
        message=message,
        recoverable=True,
        suggested_action=suggested_action,
        details={"raw_error": message},
    )


def _append_reflection_contract_repair_feedback(
    task_description: str,
    failure: _ReflectionContractFailure,
) -> str:
    details_json = json.dumps(failure.details, ensure_ascii=False, sort_keys=True)
    return (
        f"{task_description.rstrip()}\n\n"
        "Previous attempt failed deterministic event-trader contract:\n"
        f"- surface: {failure.surface}\n"
        f"- error_code: {failure.error_code}\n"
        f"- message: {failure.message}\n"
        f"- recoverable: {str(failure.recoverable).lower()}\n"
        f"- suggested_action: {failure.suggested_action}\n"
        f"- details: {details_json}\n\n"
        "Before final boxed JSON, call at least one successful reflection read tool. "
        "The compact brief is orientation only and is not sufficient for a final "
        "decision. Any one of the required reflection read tools satisfies the "
        "minimum read contract.\n\n"
        "Return one corrected JSON object wrapped in \\boxed{...} for the same "
        "context. Do not invent new evidence or change the trading judgment unless "
        "the deterministic error requires it. "
        f"For thesis_assessment, {_THESIS_ASSESSMENT_VERDICT_INSTRUCTION}.\n"
    )


def _reflection_contract_repair_suggestion(message: str) -> str | None:
    if "trade_assessment.verdict" in message:
        return (
            f"{_ALL_TRADE_ASSESSMENT_VERDICT_INSTRUCTION} "
            f'For close review, use only {_format_quoted_enum(_CLOSED_REFLECTION_TRADE_VERDICTS)}; '
            'for open-position review, use only "open".'
        )
    if "market_context_error_labels" in message:
        return _MARKET_CONTEXT_ERROR_LABELS_INSTRUCTION
    if "memory_status" in message:
        return _LEARNING_MEMORY_STATUS_INSTRUCTION
    if "validation_basis" in message:
        return _LEARNING_VALIDATION_BASIS_INSTRUCTION
    if "source_visible_through" in message or "usable_from" in message:
        return _LEARNING_TIMESTAMP_INSTRUCTION
    if "source_refs" in message:
        return _LEARNING_SOURCE_REFS_INSTRUCTION
    if "confidence" in message:
        return _LEARNING_CONFIDENCE_INSTRUCTION
    return None


def _build_agent_cfg(config: MiroThinkerReflectionRuntimeConfig):
    _, _, OmegaConf = _load_vendor_runtime(config.vendor_root.resolve(strict=False))
    return OmegaConf.create(
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
            ),
            "agent": {
                "main_agent": {
                    "tools": [],
                    "tool_blacklist": [],
                    "max_turns": 120,
                },
                "sub_agents": {},
                "keep_tool_result": 5,
                "context_compress_limit": 0,
            },
            "benchmark": {},
        }
    )


def _load_vendor_runtime(vendor_root: Path) -> tuple[Any, Any, Any]:
    _append_vendor_import_roots(vendor_root)
    try:
        pipeline_module = importlib.import_module("src.core.pipeline")
        omegaconf_module = importlib.import_module("omegaconf")
    except ModuleNotFoundError as exc:
        raise MiroThinkerReflectionRuntimeError(
            "MiroThinker reflection runtime dependencies are not installed. "
            "Install the vendored runtime dependencies before using this adapter."
        ) from exc

    return (
        pipeline_module.execute_task_pipeline,
        pipeline_module.create_pipeline_components,
        omegaconf_module.OmegaConf,
    )


def _append_vendor_import_roots(vendor_root: Path) -> None:
    try:
        prepare_mirothinker_runtime(vendor_root)
    except MiroThinkerRuntimePathError as exc:
        raise MiroThinkerReflectionRuntimeError(str(exc)) from exc


def _prepare_vendor_environment(config: MiroThinkerReflectionRuntimeConfig) -> None:
    os.environ["OPENAI_API_KEY"] = config.llm_api_key
    os.environ["OPENAI_BASE_URL"] = config.llm_base_url


def _assert_not_in_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise MiroThinkerReflectionRuntimeError(
        "Reflection runtime currently supports only synchronous callers; do not "
        "call it from an active event loop."
    )


def _build_shared_reflection_prompt(
    *,
    context: ReflectionLedgerContext,
) -> str:
    serialized_context = json.dumps(
        _build_shared_reflection_brief(context),
        ensure_ascii=False,
        indent=2,
    )
    return (
        _reflection_role_prompt(
            mission=(
                "Review this shared reflection anchor like a disciplined human "
                "trader. Decide whether the anchor plus later evidence and "
                "outcome context produce reusable review value worth writing "
                "forward."
            )
        )
        + _reflection_operating_boundary_prompt()
        +
        "Hard rules:\n"
        "- Use only the provided context and reflection read tools.\n"
        "- Before any final boxed JSON, call at least one reflection read tool to "
        "inspect evidence, state changes, or target memory.\n"
        "- The compact brief is orientation only and is not sufficient for a final "
        "decision.\n"
        "- Bounded text snippets and page maps are read hints, not full context.\n"
        "- Do not invent evidence, trades, or market outcomes.\n"
        "- Review from a human trader perspective: what happened, whether the "
        "path was favorable or adverse, whether it was tradable, and whether the "
        "prior thesis held up.\n"
        "- If the bounded context is insufficient or there is not enough concrete "
        "review value, choose skip_review.\n\n"
        "Return exactly one JSON object wrapped in \\boxed{...}. Do not include "
        "text before or after the boxed JSON.\n\n"
        "The JSON object must contain exactly these fields:\n"
        '- "assessed_horizons": array of objects with '
        '"horizon_hours", "return_pct", "path_verdict", '
        '"tradability_verdict", "summary_md"\n'
        '- "thesis_assessment": object with "verdict", "summary_md"; '
        f"{_THESIS_ASSESSMENT_VERDICT_INSTRUCTION}\n"
        '- "trade_assessment": object or null with "verdict", "summary_md", '
        '"return_pct"\n'
        '- "review_decision": "write_review" | "skip_review"\n'
        '- "decision_rationale": string\n\n'
        f"Reflection context:\n{serialized_context}\n"
    )


def _build_target_reflection_prompt(
    *,
    context: EpisodeReflectionContext,
) -> str:
    serialized_context = json.dumps(
        _build_target_reflection_brief(context),
        ensure_ascii=False,
        indent=2,
    )
    return (
        _reflection_role_prompt(
            mission=(
                "Review this closed target episode like a disciplined human "
                "trader. Judge whether the thesis held up, whether the path was "
                "tradable, and whether the result is worth writing as a durable "
                "target review."
            )
        )
        + _reflection_operating_boundary_prompt()
        +
        "Hard rules:\n"
        "- Use only the provided context and reflection read tools.\n"
        "- Before any final boxed JSON, call at least one reflection read tool to "
        "inspect evidence, state changes, or target memory.\n"
        "- The compact brief is orientation only and is not sufficient for a final "
        "decision.\n"
        "- Bounded text snippets and page maps are read hints, not full context.\n"
        "- The decision_episodes brief is selected review navigation, not the "
        "full audit trail; use list_decision_episodes/read_decision_episode "
        "when omitted decision context matters.\n"
        "- Treat portfolio feedback as deterministic validation facts. Use it to "
        "judge exposure and timing, but do not treat it as an already-written "
        "lesson or trading rule.\n"
        "- Treat counterfactual_evaluation, when present, as a post-horizon "
        "evaluation artifact for error attribution only. It was not visible at "
        "decision time and must not rewrite the decision-time context.\n"
        "- Do not invent evidence, market data, or trade actions.\n"
        "- Review from a human trader perspective, not a schema-filling perspective.\n"
        "- If the bounded context is insufficient or the episode does not produce "
        "concrete review value yet, choose skip_review.\n\n"
        f"{_MARKET_CONTEXT_USAGE_QUALITY_PROMPT}"
        f"{_LEARNING_DECISION_PROMPT}"
        "Return exactly one JSON object wrapped in \\boxed{...}. Do not include "
        "text before or after the boxed JSON.\n\n"
        "The JSON object must contain exactly these fields:\n"
        '- "episode_id": string\n'
        '- "target_key": string\n'
        '- "assessed_horizons": array of objects with '
        '"horizon_hours", "return_pct", "path_verdict", '
        '"tradability_verdict", "summary_md"\n'
        '- "thesis_assessment": object with "verdict", "summary_md"; '
        f"{_THESIS_ASSESSMENT_VERDICT_INSTRUCTION}\n"
        '- "watchlist_assessment": object with "verdict", "summary_md"; '
        '"verdict" must be "useful", "mixed", "stale", "missing", or "unclear"\n'
        '- "trade_assessment": object or null with "verdict", "summary_md", '
        '"return_pct"\n'
        f'- "usage_quality_label": string; '
        f"{_MARKET_CONTEXT_USAGE_QUALITY_LABEL_INSTRUCTION}\n"
        '- "usage_quality_summary": string; compact usage-quality rationale; '
        'may be "" only when label is "not_applicable"\n'
        '- "market_context_error_labels": array of strings from the same fixed '
        "label set; use [] when no market-context usage error is supported\n"
        '- "review_decision": "write_review" | "skip_review"\n'
        '- "decision_rationale": string\n'
        '- "learning_decision": object following the learning-decision rules above\n\n'
        f"Episode reflection context:\n{serialized_context}\n"
    )


def _build_open_position_reflection_prompt(
    *,
    context: OpenPositionReflectionContext,
) -> str:
    serialized_context = json.dumps(
        _build_open_position_reflection_brief(context),
        ensure_ascii=False,
        indent=2,
    )
    return (
        _reflection_role_prompt(
            mission=(
                "Review this open-position mark like a disciplined human trader. "
                "The episode is still open; do not treat this as a closed trade. "
                "Judge whether the thesis, open trade, and watchlist still make "
                "sense as of the replay/live cut-off."
            )
        )
        + _reflection_operating_boundary_prompt()
        +
        "Hard rules:\n"
        "- Use only the provided context and reflection read tools.\n"
        "- Before any final boxed JSON, call at least one reflection read tool to "
        "inspect evidence, state changes, or target memory.\n"
        "- The compact brief is orientation only and is not sufficient for a final "
        "decision.\n"
        "- Bounded text snippets and page maps are read hints, not full context.\n"
        "- The decision_episodes brief is selected review navigation, not the "
        "full audit trail; use list_decision_episodes/read_decision_episode "
        "when omitted decision context matters.\n"
        "- Treat portfolio feedback as deterministic validation facts. Use it to "
        "judge exposure and timing, but do not treat it as an already-written "
        "lesson or trading rule.\n"
        "- Always separate profit from risk: profit alone is not a sell reason, "
        "but profitable open exposure lowers the tolerance for independent "
        "reversal, invalidation, margin, liquidity, positioning, or market-context "
        "divergence risk.\n"
        "- exposure_assessment is reflection-only audit text. It does not "
        "execute, recommend, or encode a trade action. Do not return trade "
        "actions or sizing labels.\n"
        "- This is an open-position review. If no durable delta is warranted for "
        "a written review, emit an EpisodeMemory no_update candidate.\n"
        "- Do not invent evidence, market data, exits, or trade actions.\n"
        "- Review from a human trader perspective, not a schema-filling perspective.\n"
        "- trade_assessment.verdict must be \"open\".\n"
        "- If the bounded context is insufficient or the terminal mark does not "
        "produce concrete review value yet, choose skip_review.\n\n"
        f"{_MARKET_CONTEXT_USAGE_QUALITY_PROMPT}"
        f"{_LEARNING_DECISION_PROMPT}"
        "Return exactly one JSON object wrapped in \\boxed{...}. Do not include "
        "text before or after the boxed JSON.\n\n"
        "The JSON object must contain exactly these fields:\n"
        '- "episode_id": string\n'
        '- "target_key": string\n'
        '- "thesis_assessment": object with "verdict", "summary_md"; '
        f"{_THESIS_ASSESSMENT_VERDICT_INSTRUCTION}\n"
        '- "watchlist_assessment": object with "verdict", "summary_md"; '
        '"verdict" must be "useful", "mixed", "stale", "missing", or "unclear"\n'
        '- "trade_assessment": object with "verdict", "summary_md", "return_pct"\n'
        '- Do not include a top-level "return_pct"; return_pct belongs only '
        'inside "trade_assessment".\n'
        '- "exposure_assessment": object with "summary_md"; describe open '
        "exposure quality and risk, without trade-action fields\n"
        f'- "usage_quality_label": string; '
        f"{_MARKET_CONTEXT_USAGE_QUALITY_LABEL_INSTRUCTION}\n"
        '- "usage_quality_summary": string; compact usage-quality rationale; '
        'may be "" only when label is "not_applicable"\n'
        '- "market_context_error_labels": array of strings from the same fixed '
        "label set; use [] when no market-context usage error is supported\n"
        '- "review_decision": "write_review" | "skip_review"\n'
        '- "decision_rationale": string\n'
        '- "learning_decision": object following the learning-decision rules above\n\n'
        f"Terminal open-position reflection context:\n{serialized_context}\n"
    )


def _build_target_close_reflection_prompt(
    *,
    context: CloseEpisodeReflectionContext,
) -> str:
    serialized_context = json.dumps(
        _build_target_close_reflection_brief(context),
        ensure_ascii=False,
        indent=2,
    )
    return (
        _reflection_role_prompt(
            mission=(
                "Review this just-closed target episode like a disciplined human "
                "trader. This is a close/reversal review, not a horizon "
                "follow-up. Judge the trade that actually ended, whether the "
                "thesis held up through close, and whether the watchlist helped "
                "attention before the close."
            )
        )
        + _reflection_operating_boundary_prompt()
        +
        "Hard rules:\n"
        "- Use only the provided context and reflection read tools.\n"
        "- Before any final boxed JSON, call at least one reflection read tool to "
        "inspect evidence, state changes, or target memory.\n"
        "- The compact brief is orientation only and is not sufficient for a final "
        "decision.\n"
        "- Bounded text snippets and page maps are read hints, not full context.\n"
        "- The decision_episodes brief is selected review navigation, not the "
        "full audit trail; use list_decision_episodes/read_decision_episode "
        "when omitted decision context matters.\n"
        "- Treat portfolio feedback as deterministic validation facts. Use it to "
        "judge exposure and timing, but do not treat it as an already-written "
        "lesson or trading rule.\n"
        "- Do not invent evidence, market data, exits, or trade actions.\n"
        "- Do not request or assess 24h/72h/168h horizons in this close review.\n"
        f"- {_CLOSE_TRADE_ASSESSMENT_VERDICT_INSTRUCTION}\n"
        "- If the bounded context is insufficient or the close does not produce "
        "concrete review value, choose skip_review.\n\n"
        f"{_MARKET_CONTEXT_USAGE_QUALITY_PROMPT}"
        f"{_LEARNING_DECISION_PROMPT}"
        "Return exactly one JSON object wrapped in \\boxed{...}. Do not include "
        "text before or after the boxed JSON.\n\n"
        "The JSON object must contain exactly these fields:\n"
        '- "episode_id": string\n'
        '- "target_key": string\n'
        '- "thesis_assessment": object with "verdict", "summary_md"; '
        f"{_THESIS_ASSESSMENT_VERDICT_INSTRUCTION}\n"
        '- "watchlist_assessment": object with "verdict", "summary_md"; '
        '"verdict" must be "useful", "mixed", "stale", "missing", or "unclear"\n'
        '- "trade_assessment": object with "verdict", "summary_md", "return_pct"; '
        f"{_CLOSE_TRADE_ASSESSMENT_VERDICT_INSTRUCTION}\n"
        f'- "usage_quality_label": string; '
        f"{_MARKET_CONTEXT_USAGE_QUALITY_LABEL_INSTRUCTION}\n"
        '- "usage_quality_summary": string; compact usage-quality rationale; '
        'may be "" only when label is "not_applicable"\n'
        f"- {_MARKET_CONTEXT_ERROR_LABELS_INSTRUCTION}\n"
        '- "review_decision": "write_review" | "skip_review"\n'
        '- "decision_rationale": string\n'
        '- "learning_decision": object following the learning-decision rules above\n\n'
        f"Close episode reflection context:\n{serialized_context}\n"
    )


def _build_shared_reflection_brief(
    context: ReflectionLedgerContext,
) -> dict[str, object]:
    return {
        "task_kind": "shared_anchor_reflection",
        "anchor": {
            "anchor_id": context.anchor_id,
            "scope_key": context.anchor.scope_key,
            "target_key": context.anchor.target_key,
            "log_page_path": context.anchor.log_page_path,
            "entry_key": context.anchor.entry_key,
            "logged_at": context.anchor.logged_at.isoformat(),
        },
        "coverage": {
            "lookback_hours": context.coverage.lookback_hours,
            "horizons_hours": list(context.coverage.horizons_hours),
        },
        "anchor_entry": {
            "heading_text": context.anchor_entry.heading_text,
            "line_start": context.anchor_entry.line_start,
            "line_end": context.anchor_entry.line_end,
            "entry": _serialize_brief_markdown(context.anchor_entry.entry_md),
            "body": _serialize_brief_markdown(context.anchor_entry.body_md),
            "cited_event_ids": list(context.anchor_entry.cited_event_ids),
            "cited_source_refs": list(context.anchor_entry.cited_source_refs),
        },
        "original_evidence": _serialize_evidence_set_brief(context.original_evidence),
        "later_evidence": _serialize_evidence_set_brief(context.later_evidence),
        "outcome_context": (
            None
            if context.outcome_context is None
            else {
                "observed_at": context.outcome_context.observed_at.isoformat(),
                "summary": _serialize_brief_markdown(context.outcome_context.summary_md),
            }
        ),
        "trade_context": (
            None
            if context.trade_context is None
            else {
                "observed_at": context.trade_context.observed_at.isoformat(),
                "summary": _serialize_brief_markdown(context.trade_context.summary_md),
            }
        ),
        "tool_reads": _serialize_reflection_tool_read_brief(),
    }


def _build_target_reflection_brief(
    context: EpisodeReflectionContext,
) -> dict[str, object]:
    return {
        "task_kind": "closed_target_episode_reflection",
        "episode": {
            "episode_id": context.episode.episode_id,
            "target_key": context.episode.target_key,
            "direction": context.episode.direction,
            "opened_at": context.episode.opened_at.isoformat(),
            "closed_at": (
                None
                if context.episode.closed_at is None
                else context.episode.closed_at.isoformat()
            ),
            "close_reason": context.episode.close_reason,
        },
        "coverage": {
            "lookback_hours": context.coverage.lookback_hours,
            "horizons_hours": list(context.coverage.horizons_hours),
        },
        "expected_source_review_path": _target_review_page_path(
            target_key=context.episode.target_key,
            opened_at=context.episode.opened_at,
            closed_at=_require_datetime(
                context.episode.closed_at,
                field_name="episode.closed_at",
            ),
        ),
        "state_changes": _serialize_state_change_set_brief(context.state_changes),
        "decision_episodes": _serialize_decision_episode_summaries(
            context.decision_episodes
        ),
        "original_evidence": _serialize_evidence_set_brief(context.original_evidence),
        "later_evidence": _serialize_evidence_set_brief(context.later_evidence),
        "market_mapping": {
            "market_symbol": context.market_mapping.market_symbol,
            "market_session": context.market_mapping.market_session,
            "exchange_session_scope": context.market_mapping.exchange_session_scope,
            "exchange": context.market_mapping.exchange,
            "bar_granularity": context.market_mapping.bar_granularity,
        },
        "market_returns": _serialize_market_returns(context.market_returns),
        "portfolio_feedback": _serialize_portfolio_feedback_brief(
            context.portfolio_feedback
        ),
        "market_context_usage": _serialize_market_context_usage(
            context.market_context_usage
        ),
        "counterfactual_evaluation": _serialize_counterfactual_summary(
            context.counterfactual_report
        ),
        "target_log_context": (
            None
            if context.target_log_context is None
            else _serialize_page_brief(context.target_log_context)
        ),
        "watchlist_context": (
            None
            if context.watchlist_context is None
            else _serialize_page_brief(context.watchlist_context)
        ),
        "tool_reads": _serialize_reflection_tool_read_brief(),
    }


def _build_open_position_reflection_brief(
    context: OpenPositionReflectionContext,
) -> dict[str, object]:
    return {
        "task_kind": "open_position_reflection",
        "episode": {
            "episode_id": context.episode.episode_id,
            "target_key": context.episode.target_key,
            "direction": context.episode.direction,
            "opened_at": context.episode.opened_at.isoformat(),
            "closed_at": None,
        },
        "open_segment": {
            "segment_id": context.open_segment.segment_id,
            "target_weight": context.open_segment.target_weight,
            "opened_at": context.open_segment.opened_at.isoformat(),
            "opened_by_state_change_id": context.open_segment.opened_by_state_change_id,
        },
        "state_changes": _serialize_state_change_set_brief(context.state_changes),
        "decision_episodes": _serialize_decision_episode_summaries(
            context.decision_episodes,
            previous_memory_update_at=context.previous_open_memory_update_at,
        ),
        "original_evidence": _serialize_evidence_set_brief(context.original_evidence),
        "later_evidence": _serialize_evidence_set_brief(context.later_evidence),
        "market_mapping": {
            "market_symbol": context.market_mapping.market_symbol,
            "market_session": context.market_mapping.market_session,
            "exchange_session_scope": context.market_mapping.exchange_session_scope,
            "exchange": context.market_mapping.exchange,
            "bar_granularity": context.market_mapping.bar_granularity,
        },
        "terminal_mark": {
            "episode_id": context.terminal_mark.episode_id,
            "replay_end_at": context.terminal_mark.replay_end_at.isoformat(),
            "entry_bar_start_at": context.terminal_mark.entry_bar_start_at.isoformat(),
            "mark_bar_start_at": context.terminal_mark.mark_bar_start_at.isoformat(),
            "entry_price": context.terminal_mark.entry_price,
            "mark_price": context.terminal_mark.mark_price,
            "underlying_return": context.terminal_mark.underlying_return,
            "strategy_return": context.terminal_mark.strategy_return,
            "target_weight": context.terminal_mark.target_weight,
        },
        "review_kind": context.review_kind,
        "previous_open_memory_update_at": (
            None
            if context.previous_open_memory_update_at is None
            else context.previous_open_memory_update_at.isoformat()
        ),
        "open_position_material_update_sequence": (
            context.open_position_material_update_sequence
        ),
        "expected_source_review_path": _open_position_review_page_path_for_context(
            context
        ),
        "expected_source_review_path_note": (
            "Use the review artifact path produced by the open-position review writer "
            "for source_review_path. For open_position_material_update, assess both "
            "changes since the previous open memory update and the full "
            "position-to-date thesis. For write_review with no durable delta, emit "
            "emit_episode_memory_candidate with candidate_kind='no_update'."
        ),
        "portfolio_feedback": _serialize_portfolio_feedback_brief(
            context.portfolio_feedback
        ),
        "market_context_usage": _serialize_market_context_usage(
            context.market_context_usage
        ),
        "watchlist_context": _serialize_page_brief(context.watchlist_context),
        "target_log_context": (
            None
            if context.target_log_context is None
            else _serialize_page_brief(context.target_log_context)
        ),
        "tool_reads": _serialize_reflection_tool_read_brief(),
    }


def _build_target_close_reflection_brief(
    context: CloseEpisodeReflectionContext,
) -> dict[str, object]:
    return {
        "task_kind": "close_episode_reflection",
        "episode": {
            "episode_id": context.episode.episode_id,
            "target_key": context.episode.target_key,
            "direction": context.episode.direction,
            "opened_at": context.episode.opened_at.isoformat(),
            "closed_at": (
                None
                if context.episode.closed_at is None
                else context.episode.closed_at.isoformat()
            ),
            "close_reason": context.episode.close_reason,
        },
        "expected_source_review_path": _target_close_review_page_path(
            target_key=context.episode.target_key,
            opened_at=context.episode.opened_at,
            closed_at=_require_datetime(
                context.episode.closed_at,
                field_name="episode.closed_at",
            ),
        ),
        "state_changes": _serialize_state_change_set_brief(context.state_changes),
        "decision_episodes": _serialize_decision_episode_summaries(
            context.decision_episodes
        ),
        "original_evidence": _serialize_evidence_set_brief(context.original_evidence),
        "later_evidence": _serialize_evidence_set_brief(context.later_evidence),
        "market_mapping": {
            "market_symbol": context.market_mapping.market_symbol,
            "market_session": context.market_mapping.market_session,
            "exchange_session_scope": context.market_mapping.exchange_session_scope,
            "exchange": context.market_mapping.exchange,
            "bar_granularity": context.market_mapping.bar_granularity,
        },
        "market_returns": _serialize_market_returns(context.market_returns),
        "portfolio_feedback": _serialize_portfolio_feedback_brief(
            context.portfolio_feedback
        ),
        "market_context_usage": _serialize_market_context_usage(
            context.market_context_usage
        ),
        "watchlist_context": _serialize_page_brief(context.watchlist_context),
        "target_log_context": (
            None
            if context.target_log_context is None
            else _serialize_page_brief(context.target_log_context)
        ),
        "tool_reads": _serialize_reflection_tool_read_brief(),
    }


def _serialize_evidence_set_brief(
    records: tuple[EvidenceLedgerRecord, ...],
) -> dict[str, object]:
    if not records:
        return {"count": 0, "event_time_range": None, "sample_latest": []}
    ordered = tuple(
        sorted(
            records,
            key=lambda record: (record.ts_event, record.ts_init, record.event_id),
        )
    )
    sample = ordered[-_REFLECTION_BRIEF_EVIDENCE_SAMPLE_LIMIT :]
    return {
        "count": len(ordered),
        "event_time_range": {
            "start": ordered[0].ts_event.isoformat(),
            "end": ordered[-1].ts_event.isoformat(),
        },
        "sample_latest": [_serialize_evidence_record_brief(record) for record in sample],
    }


def _serialize_state_change_set_brief(
    state_changes: tuple[ViewStateChange, ...],
) -> dict[str, object]:
    if not state_changes:
        return {"count": 0, "effective_time_range": None, "sample_latest": []}
    ordered = tuple(
        sorted(
            state_changes,
            key=lambda state_change: (
                state_change.effective_at,
                state_change.state_change_id,
            ),
        )
    )
    sample = ordered[-_REFLECTION_BRIEF_STATE_CHANGE_SAMPLE_LIMIT :]
    return {
        "count": len(ordered),
        "effective_time_range": {
            "start": ordered[0].effective_at.isoformat(),
            "end": ordered[-1].effective_at.isoformat(),
        },
        "sample_latest": [_serialize_state_change(state_change) for state_change in sample],
    }


def _target_review_page_path(
    *,
    target_key: str,
    opened_at: datetime,
    closed_at: datetime,
) -> str:
    return (
        f"targets/{target_key}/reviews/"
        f"{_review_timestamp(opened_at)}_{_review_timestamp(closed_at)}.md"
    )


def _target_close_review_page_path(
    *,
    target_key: str,
    opened_at: datetime,
    closed_at: datetime,
) -> str:
    return (
        f"targets/{target_key}/reviews/"
        f"{_review_timestamp(opened_at)}_{_review_timestamp(closed_at)}_close.md"
    )


def _review_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%y%m%dT%H%M%S")


def _format_hour_label(hour: float) -> str:
    return f"{_format_hour_value(hour)}h"


def _format_hour_value(hour: float) -> str:
    if float(hour).is_integer():
        return str(int(hour))
    return format(hour, "g")


def _serialize_decision_episode_summaries(
    episodes,
    *,
    previous_memory_update_at: datetime | None = None,
) -> dict[str, object]:
    if not episodes:
        return {
            "count": 0,
            "included_count": 0,
            "omitted_count": 0,
            "selection_policy": "adaptive_review_window",
            "selection_counts": {},
            "omitted_by_status": {},
            "limits": _decision_episode_selection_limits(),
            "window": _decision_episode_selection_window(previous_memory_update_at),
            "items": [],
        }

    ordered = tuple(
        sorted(
            episodes,
            key=lambda episode: (
                episode.opened_at,
                episode.target_key,
                episode.episode_id,
            ),
        )
    )
    selected_reasons = _select_decision_episode_reasons(
        ordered,
        previous_memory_update_at=previous_memory_update_at,
    )
    selected_ids = set(selected_reasons)
    omitted = tuple(
        episode for episode in ordered if episode.episode_id not in selected_ids
    )
    selection_counts: dict[str, int] = {}
    for reasons in selected_reasons.values():
        for reason in reasons:
            selection_counts[reason] = selection_counts.get(reason, 0) + 1

    return {
        "count": len(ordered),
        "included_count": len(selected_reasons),
        "omitted_count": len(omitted),
        "selection_policy": "adaptive_review_window",
        "selection_counts": dict(sorted(selection_counts.items())),
        "omitted_by_status": _decision_episode_status_counts(omitted),
        "limits": _decision_episode_selection_limits(),
        "window": _decision_episode_selection_window(previous_memory_update_at),
        "items": [
            _serialize_decision_episode_summary_item(
                episode,
                selection_reasons=selected_reasons[episode.episode_id],
            )
            for episode in ordered
            if episode.episode_id in selected_ids
        ],
    }


def _decision_episode_selection_limits() -> dict[str, object]:
    return {
        "since_previous_memory_update_full_threshold": (
            _REFLECTION_DECISION_EPISODE_SINCE_FULL_THRESHOLD
        ),
        "since_previous_memory_update_hard_cap": (
            _REFLECTION_DECISION_EPISODE_SINCE_HARD_CAP
        ),
        "recent_tail_count": _REFLECTION_DECISION_EPISODE_RECENT_TAIL_COUNT,
        "absolute_item_fuse": _REFLECTION_DECISION_EPISODE_ABSOLUTE_ITEM_FUSE,
    }


def _decision_episode_selection_window(
    previous_memory_update_at: datetime | None,
) -> dict[str, object]:
    return {
        "previous_memory_update_at": (
            None
            if previous_memory_update_at is None
            else previous_memory_update_at.isoformat()
        ),
        "since_previous_memory_update_enabled": previous_memory_update_at is not None,
    }


def _select_decision_episode_reasons(
    episodes: tuple[object, ...],
    *,
    previous_memory_update_at: datetime | None,
) -> dict[str, tuple[str, ...]]:
    reasons_by_id: dict[str, set[str]] = {}

    def add(episode: object, reason: str) -> None:
        reasons_by_id.setdefault(episode.episode_id, set()).add(reason)

    if episodes:
        add(episodes[0], "opening")

    for episode in episodes:
        if _is_decision_episode_anchor(episode):
            add(episode, "anchor")

    if previous_memory_update_at is None:
        if len(episodes) <= _REFLECTION_DECISION_EPISODE_ABSOLUTE_ITEM_FUSE:
            selected_initial = episodes
        else:
            selected_initial = tuple(
                sorted(
                    episodes,
                    key=_decision_episode_selection_priority,
                    reverse=True,
                )[:_REFLECTION_DECISION_EPISODE_ABSOLUTE_ITEM_FUSE]
            )
        for episode in selected_initial:
            add(episode, "initial_window")
    else:
        since_previous = tuple(
            episode
            for episode in episodes
            if _decision_episode_touched_after(episode, previous_memory_update_at)
        )
        if len(since_previous) <= _REFLECTION_DECISION_EPISODE_SINCE_FULL_THRESHOLD:
            selected_since = since_previous
        else:
            selected_since = tuple(
                sorted(
                    since_previous,
                    key=_decision_episode_selection_priority,
                    reverse=True,
                )[:_REFLECTION_DECISION_EPISODE_SINCE_HARD_CAP]
            )
        for episode in selected_since:
            add(episode, "since_previous_review")

    recent_tail = tuple(
        episode
        for episode in reversed(episodes)
        if episode.episode_id not in reasons_by_id
    )[:_REFLECTION_DECISION_EPISODE_RECENT_TAIL_COUNT]
    for episode in recent_tail:
        add(episode, "recent_tail")

    if len(reasons_by_id) > _REFLECTION_DECISION_EPISODE_ABSOLUTE_ITEM_FUSE:
        anchor_ids = {
            episode.episode_id
            for episode in episodes
            if "opening" in reasons_by_id.get(episode.episode_id, set())
            or "anchor" in reasons_by_id.get(episode.episode_id, set())
        }
        remaining_capacity = max(
            0,
            _REFLECTION_DECISION_EPISODE_ABSOLUTE_ITEM_FUSE - len(anchor_ids),
        )
        non_anchor = tuple(
            episode
            for episode in episodes
            if episode.episode_id in reasons_by_id and episode.episode_id not in anchor_ids
        )
        kept_non_anchor = {
            episode.episode_id
            for episode in sorted(
                non_anchor,
                key=_decision_episode_selection_priority,
                reverse=True,
            )[:remaining_capacity]
        }
        kept_ids = anchor_ids | kept_non_anchor
        reasons_by_id = {
            episode_id: reasons
            for episode_id, reasons in reasons_by_id.items()
            if episode_id in kept_ids
        }

    return {
        episode_id: tuple(sorted(reasons))
        for episode_id, reasons in reasons_by_id.items()
    }


def _is_decision_episode_anchor(episode: object) -> bool:
    analysis = episode.analysis_record
    return (
        bool(episode.validation_marks)
        or episode.pm_decision_record is not None
        or bool(episode.reflection_records)
        or bool(episode.repair_records)
        or (analysis is not None and analysis.status == "failed")
    )


def _decision_episode_touched_after(
    episode: object,
    previous_memory_update_at: datetime,
) -> bool:
    return any(
        value > previous_memory_update_at
        for value in _decision_episode_business_times(episode)
    )


def _decision_episode_business_times(episode: object) -> tuple[datetime, ...]:
    values: list[datetime] = []
    for record in (
        episode.attention_record,
        episode.analysis_record,
        episode.pm_decision_record,
    ):
        if record is not None:
            values.append(record.business_at)
    for collection in (
        episode.validation_marks,
        episode.reflection_records,
        episode.repair_records,
    ):
        values.extend(record.business_at for record in collection)
    if not values:
        values.append(episode.opened_at)
    return tuple(values)


def _decision_episode_selection_priority(episode: object) -> tuple[int, int, datetime]:
    analysis = episode.analysis_record
    has_analysis = 1 if analysis is not None else 0
    has_non_attention_status = 1 if episode.status != "open_attention" else 0
    return (has_non_attention_status, has_analysis, episode.opened_at)


def _decision_episode_status_counts(episodes: tuple[object, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for episode in episodes:
        status = str(episode.status)
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _serialize_decision_episode_summary_item(
    episode: object,
    *,
    selection_reasons: tuple[str, ...],
) -> dict[str, object]:
    analysis = episode.analysis_record
    validation_state_change_ids = tuple(
        str(mark.payload.get("state_change_id"))
        for mark in episode.validation_marks
        if isinstance(mark.payload.get("state_change_id"), str)
    )
    context_packet_ids = tuple(
        record.context_packet_id
        for record in (episode.attention_record, analysis)
        if record is not None and record.context_packet_id is not None
    )
    return {
        "episode_id": episode.episode_id,
        "status": episode.status,
        "opened_at": episode.opened_at.isoformat(),
        "selection_reasons": list(selection_reasons),
        "event_ids": list(episode.event_ids),
        "context_packet_ids": list(context_packet_ids),
        "analysis_outcome": None if analysis is None else analysis.status,
        "validation_state_change_ids": list(validation_state_change_ids),
    }


def _serialize_evidence_record_brief(record: EvidenceLedgerRecord) -> dict[str, object]:
    return {
        "event_id": record.event_id,
        "target_key": record.target_key,
        "source_ref": record.source_ref,
        "title": record.title,
        "content_sha256": sha256(record.content.encode("utf-8")).hexdigest(),
        "content_char_count": len(record.content),
        "content_available_via": "read_evidence/list_evidence_window",
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
        "content_available_via": "read_target_page/read_target_section/search_target_memory",
        "page_map": _serialize_page_map_brief(page.content_md),
    }


def _serialize_page_map_brief(content_md: str) -> list[dict[str, object]]:
    try:
        page_map = build_markdown_page_map(
            content_md,
            excerpt_char_limit=_REFLECTION_PAGE_MAP_SECTION_EXCERPT_CHAR_LIMIT,
        )
    except MarkdownContextError as exc:
        raise MiroThinkerReflectionRuntimeError(
            f"Failed to build reflection prompt page map: {exc}"
        ) from exc
    return [
        {
            "heading": section["heading"],
            "level": section["level"],
            "start_offset": section["start_offset"],
            "end_offset": section["end_offset"],
            "content_sha256": section["content_sha256"],
            "content_char_count": section["content_char_count"],
            "content_truncated": section["content_truncated"],
        }
        for section in page_map[:_REFLECTION_PAGE_MAP_SECTION_LIMIT]
    ]


def _serialize_brief_markdown(value: str) -> dict[str, object]:
    return _serialize_bounded_markdown(value)


def _serialize_reflection_tool_read_brief() -> dict[str, object]:
    return {
        "server_hint": _REFLECTION_MCP_SERVER_NAME,
        "tool_names": [tool_name for _, tool_name in _REFLECTION_REQUIRED_MCP_TOOLS],
        "required_before_strong_conclusion": [
            "Use read_evidence for original/later evidence records tied to the conclusion.",
            "Use list_evidence_window when later evidence count or time range matters.",
            "Use list_state_changes for state-change rationale and source-event links.",
            "Use list_decision_episodes/read_decision_episode when omitted decision "
            "audit trail context matters.",
            "Use read_target_section or read_target_page when page excerpts are truncated.",
        ],
    }


def _install_reflection_mcp_server(
    *,
    tool_manager: Any,
    vendor_root: Path,
    workspace_root: Path,
    target_key: str | None,
    window_start: datetime,
    window_end: datetime,
) -> None:
    existing_configs = list(getattr(tool_manager, "server_configs", ()))
    base_params = existing_configs[0]["params"] if existing_configs else None
    params_env = dict(getattr(base_params, "env", {}) or {})
    cwd = getattr(base_params, "cwd", None) if base_params is not None else str(workspace_root)
    server_env = dict(params_env)
    server_env.update(
        {
            "EVENT_TRADER_WORKSPACE_ROOT": str(workspace_root),
            "EVENT_TRADER_REFLECTION_TARGET_KEY": target_key or "",
            "EVENT_TRADER_REFLECTION_WINDOW_START_AT": window_start.isoformat(),
            "EVENT_TRADER_REFLECTION_WINDOW_END_AT": window_end.isoformat(),
            "PYTHONPATH": build_mirothinker_child_pythonpath(
                vendor_root=vendor_root,
                project_src=Path(__file__).resolve(strict=False).parents[2],
            ),
        }
    )
    server_params = _make_stdio_server_parameters(
        command=sys.executable,
        args=["-m", "event_trader.integrations.reflection_mcp_server"],
        env=server_env,
        cwd=cwd,
    )
    tool_manager.server_configs = [
        *existing_configs,
        {"name": _REFLECTION_MCP_SERVER_NAME, "params": server_params},
    ]
    tool_manager.server_dict = {
        config["name"]: config["params"] for config in tool_manager.server_configs
    }


def _make_stdio_server_parameters(
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
    cwd: str | None,
) -> Any:
    try:
        from mcp import StdioServerParameters
    except ModuleNotFoundError as exc:
        raise MiroThinkerReflectionRuntimeError(
            "MCP runtime is not installed; cannot configure reflection read tools."
        ) from exc
    return StdioServerParameters(command=command, args=args, env=env, cwd=cwd)


def _target_reflection_window_end(context: EpisodeReflectionContext) -> datetime:
    closed_at = _require_datetime(context.episode.closed_at, field_name="episode.closed_at")
    horizon_end = context.episode.opened_at + timedelta(
        hours=max(context.coverage.horizons_hours)
    )
    return max(closed_at, horizon_end)


def _serialize_shared_context(context: ReflectionLedgerContext) -> dict[str, object]:
    return {
        "anchor": {
            "scope_key": context.anchor.scope_key,
            "target_key": context.anchor.target_key,
            "log_page_path": context.anchor.log_page_path,
            "entry_key": context.anchor.entry_key,
            "logged_at": context.anchor.logged_at.isoformat(),
        },
        "anchor_entry": _serialize_anchor_entry(context.anchor_entry),
        "coverage": {
            "lookback_hours": context.coverage.lookback_hours,
            "horizons_hours": list(context.coverage.horizons_hours),
        },
        "original_evidence": [
            _serialize_evidence_record(record) for record in context.original_evidence
        ],
        "later_evidence": [
            _serialize_evidence_record(record) for record in context.later_evidence
        ],
        "outcome_context": (
            None
            if context.outcome_context is None
            else {
                "observed_at": context.outcome_context.observed_at.isoformat(),
                "summary": _serialize_bounded_markdown(
                    context.outcome_context.summary_md
                ),
            }
        ),
        "trade_context": (
            None
            if context.trade_context is None
            else {
                "observed_at": context.trade_context.observed_at.isoformat(),
                "summary": _serialize_bounded_markdown(
                    context.trade_context.summary_md
                ),
            }
        ),
    }


def _serialize_target_context(context: EpisodeReflectionContext) -> dict[str, object]:
    return {
        "episode": {
            "episode_id": context.episode.episode_id,
            "target_key": context.episode.target_key,
            "direction": context.episode.direction,
            "opened_at": context.episode.opened_at.isoformat(),
            "closed_at": (
                None
                if context.episode.closed_at is None
                else context.episode.closed_at.isoformat()
            ),
            "close_reason": context.episode.close_reason,
        },
        "coverage": {
            "lookback_hours": context.coverage.lookback_hours,
            "horizons_hours": list(context.coverage.horizons_hours),
        },
        "state_changes": [
            _serialize_state_change(state_change) for state_change in context.state_changes
        ],
        "original_evidence": [
            _serialize_evidence_record(record) for record in context.original_evidence
        ],
        "later_evidence": [
            _serialize_evidence_record(record) for record in context.later_evidence
        ],
        "market_mapping": {
            "market_symbol": context.market_mapping.market_symbol,
            "market_session": context.market_mapping.market_session,
            "exchange_session_scope": context.market_mapping.exchange_session_scope,
            "exchange": context.market_mapping.exchange,
            "bar_granularity": context.market_mapping.bar_granularity,
        },
        "market_returns": _serialize_market_returns(context.market_returns),
        "portfolio_feedback": _serialize_portfolio_feedback(context.portfolio_feedback),
        "market_context_usage": _serialize_market_context_usage(
            context.market_context_usage
        ),
        "counterfactual_evaluation": _serialize_counterfactual_summary(
            context.counterfactual_report
        ),
        "target_log_context": (
            None
            if context.target_log_context is None
            else _serialize_page(context.target_log_context)
        ),
        "watchlist_context": (
            None
            if context.watchlist_context is None
            else _serialize_page(context.watchlist_context)
        ),
    }


def _serialize_open_position_context(
    context: OpenPositionReflectionContext,
) -> dict[str, object]:
    return {
        "episode": {
            "episode_id": context.episode.episode_id,
            "target_key": context.episode.target_key,
            "direction": context.episode.direction,
            "opened_at": context.episode.opened_at.isoformat(),
            "closed_at": None,
        },
        "open_segment": {
            "segment_id": context.open_segment.segment_id,
            "target_weight": context.open_segment.target_weight,
            "opened_at": context.open_segment.opened_at.isoformat(),
            "opened_by_state_change_id": context.open_segment.opened_by_state_change_id,
        },
        "state_changes": [
            _serialize_state_change(state_change) for state_change in context.state_changes
        ],
        "original_evidence": [
            _serialize_evidence_record(record) for record in context.original_evidence
        ],
        "later_evidence": [
            _serialize_evidence_record(record) for record in context.later_evidence
        ],
        "market_mapping": {
            "market_symbol": context.market_mapping.market_symbol,
            "market_session": context.market_mapping.market_session,
            "exchange_session_scope": context.market_mapping.exchange_session_scope,
            "exchange": context.market_mapping.exchange,
            "bar_granularity": context.market_mapping.bar_granularity,
        },
        "terminal_mark": {
            "episode_id": context.terminal_mark.episode_id,
            "replay_end_at": context.terminal_mark.replay_end_at.isoformat(),
            "entry_bar_start_at": context.terminal_mark.entry_bar_start_at.isoformat(),
            "mark_bar_start_at": context.terminal_mark.mark_bar_start_at.isoformat(),
            "entry_price": context.terminal_mark.entry_price,
            "mark_price": context.terminal_mark.mark_price,
            "underlying_return": context.terminal_mark.underlying_return,
            "strategy_return": context.terminal_mark.strategy_return,
            "target_weight": context.terminal_mark.target_weight,
        },
        "portfolio_feedback": _serialize_portfolio_feedback(context.portfolio_feedback),
        "market_context_usage": _serialize_market_context_usage(
            context.market_context_usage
        ),
        "watchlist_context": _serialize_page(context.watchlist_context),
        "target_log_context": (
            None
            if context.target_log_context is None
            else _serialize_page(context.target_log_context)
        ),
    }


def _serialize_target_close_context(
    context: CloseEpisodeReflectionContext,
) -> dict[str, object]:
    return {
        "episode": {
            "episode_id": context.episode.episode_id,
            "target_key": context.episode.target_key,
            "direction": context.episode.direction,
            "opened_at": context.episode.opened_at.isoformat(),
            "closed_at": (
                None
                if context.episode.closed_at is None
                else context.episode.closed_at.isoformat()
            ),
            "close_reason": context.episode.close_reason,
        },
        "state_changes": [
            _serialize_state_change(state_change) for state_change in context.state_changes
        ],
        "original_evidence": [
            _serialize_evidence_record(record) for record in context.original_evidence
        ],
        "later_evidence": [
            _serialize_evidence_record(record) for record in context.later_evidence
        ],
        "market_mapping": {
            "market_symbol": context.market_mapping.market_symbol,
            "market_session": context.market_mapping.market_session,
            "exchange_session_scope": context.market_mapping.exchange_session_scope,
            "exchange": context.market_mapping.exchange,
            "bar_granularity": context.market_mapping.bar_granularity,
        },
        "market_returns": _serialize_market_returns(context.market_returns),
        "portfolio_feedback": _serialize_portfolio_feedback(context.portfolio_feedback),
        "market_context_usage": _serialize_market_context_usage(
            context.market_context_usage
        ),
        "watchlist_context": _serialize_page(context.watchlist_context),
        "target_log_context": (
            None
            if context.target_log_context is None
            else _serialize_page(context.target_log_context)
        ),
    }


def _serialize_anchor_entry(entry: ReflectionLogEntry) -> dict[str, object]:
    entry_text = bounded_text_payload(
        entry.entry_md,
        limit=_REFLECTION_PAGE_EXCERPT_CHAR_LIMIT,
    )
    body_text = bounded_text_payload(
        entry.body_md,
        limit=_REFLECTION_PAGE_EXCERPT_CHAR_LIMIT,
    )
    return {
        "heading_text": entry.heading_text,
        "entry_sha256": entry_text["sha256"],
        "entry_char_count": entry_text["char_count"],
        "entry_truncated": entry_text["truncated"],
        "entry_excerpt_md": entry_text["excerpt"],
        "body_sha256": body_text["sha256"],
        "body_char_count": body_text["char_count"],
        "body_truncated": body_text["truncated"],
        "body_excerpt_md": body_text["excerpt"],
        "cited_event_ids": list(entry.cited_event_ids),
        "cited_source_refs": list(entry.cited_source_refs),
    }


def _serialize_evidence_record(record: EvidenceLedgerRecord) -> dict[str, object]:
    content = bounded_text_payload(
        record.content,
        limit=_REFLECTION_EVIDENCE_EXCERPT_CHAR_LIMIT,
    )
    return {
        "event_id": record.event_id,
        "target_key": record.target_key,
        "source_ref": record.source_ref,
        "title": record.title,
        "content_sha256": content["sha256"],
        "content_char_count": content["char_count"],
        "content_truncated": content["truncated"],
        "content_excerpt": content["excerpt"],
        "labels": list(record.labels),
        "ts_source": record.ts_source.isoformat(),
        "ts_event": record.ts_event.isoformat(),
        "ts_init": record.ts_init.isoformat(),
    }


def _serialize_page(page: PageReadResult) -> dict[str, object]:
    content = bounded_text_payload(
        page.content_md,
        limit=_REFLECTION_PAGE_EXCERPT_CHAR_LIMIT,
    )
    return {
        "page_path": page.page_path,
        "content_sha256": content["sha256"],
        "content_char_count": content["char_count"],
        "content_truncated": content["truncated"],
        "content_excerpt_md": content["excerpt"],
    }


def _serialize_bounded_markdown(value: str) -> dict[str, object]:
    content = bounded_text_payload(
        value,
        limit=_REFLECTION_PAGE_EXCERPT_CHAR_LIMIT,
    )
    return {
        "sha256": content["sha256"],
        "char_count": content["char_count"],
        "truncated": content["truncated"],
        "excerpt_md": content["excerpt"],
    }


def _serialize_state_change(state_change: ViewStateChange) -> dict[str, object]:
    rationale = bounded_text_payload(
        state_change.rationale_md,
        limit=_REFLECTION_STATE_RATIONALE_EXCERPT_CHAR_LIMIT,
    )
    return {
        "state_change_id": state_change.state_change_id,
        "state": state_change.state,
        "direction": state_change.direction,
        "conviction": state_change.conviction,
        "target_weight": state_change.target_weight,
        "effective_at": state_change.effective_at.isoformat(),
        "source_event_ids": list(state_change.source_event_ids),
        "rationale_sha256": rationale["sha256"],
        "rationale_char_count": rationale["char_count"],
        "rationale_truncated": rationale["truncated"],
        "rationale_excerpt_md": rationale["excerpt"],
    }


def _serialize_market_returns(result: ValidationReturnsResult) -> dict[str, object]:
    return {
        "episode_returns": [
            {
                "episode_id": item.episode_id,
                "target_key": item.target_key,
                "direction": item.direction,
                "entry_bar_start_at": item.entry_bar_start_at.isoformat(),
                "exit_bar_start_at": item.exit_bar_start_at.isoformat(),
                "entry_price": item.entry_price,
                "exit_price": item.exit_price,
                "underlying_return": item.underlying_return,
                "strategy_return": item.strategy_return,
            }
            for item in result.episode_returns
        ],
        "horizon_baselines": [
            _serialize_horizon_baseline(item) for item in result.horizon_baselines
        ],
    }


def _serialize_portfolio_feedback(
    feedback: PortfolioFeedbackSnapshot | None,
) -> dict[str, object] | None:
    if feedback is None:
        return None
    return {
        "target_key": feedback.target_key,
        "run_id": feedback.run_id,
        "window_start": feedback.window_start.isoformat(),
        "window_end": feedback.window_end.isoformat(),
        "portfolio_facts": feedback.portfolio_facts.to_dict(),
        "segment_facts": [item.to_dict() for item in feedback.segment_facts],
        "decision_facts": [item.to_dict() for item in feedback.decision_facts],
        "exposure_path_count": len(feedback.exposure_path),
    }


def _serialize_portfolio_feedback_brief(
    feedback: PortfolioFeedbackSnapshot | None,
) -> dict[str, object] | None:
    if feedback is None:
        return None
    return {
        "target_key": feedback.target_key,
        "run_id": feedback.run_id,
        "window_start": feedback.window_start.isoformat(),
        "window_end": feedback.window_end.isoformat(),
        "portfolio_facts": feedback.portfolio_facts.to_dict(),
        "segment_facts": [item.to_dict() for item in feedback.segment_facts],
        "decision_facts": [item.to_dict() for item in feedback.decision_facts],
        "exposure_path_count": len(feedback.exposure_path),
        "exposure_path_available_from_validation": True,
    }


def _serialize_counterfactual_summary(
    report: CounterfactualEvaluationReport | None,
) -> dict[str, object] | None:
    if report is None:
        return None
    available = tuple(
        result
        for result in report.baseline_results
        if result.status == "available" and result.baseline_return is not None
    )
    best = max(available, key=_counterfactual_baseline_return, default=None)
    worst = min(available, key=_counterfactual_baseline_return, default=None)
    return {
        "artifact_kind": "post_horizon_counterfactual_evaluation",
        "usage_boundary": "post_horizon_error_attribution_only_not_decision_time_context",
        "episode_id": report.episode_id,
        "target_key": report.target_key,
        "actual_return": report.actual_return,
        "market_provenance_hash": report.market_provenance_hash,
        "baseline_returns": {
            result.baseline_type: {
                "status": result.status,
                "return": result.baseline_return,
                "delta_vs_actual": result.delta_vs_actual,
                "reason": result.reason,
            }
            for result in report.baseline_results
        },
        "best_baseline": None if best is None else best.baseline_type,
        "worst_baseline": None if worst is None else worst.baseline_type,
        "episode_metrics": (
            None
            if report.episode_metrics is None
            else report.episode_metrics.to_json_payload()
        ),
        "decision_quality_attribution": (
            None
            if report.decision_quality_attribution is None
            else report.decision_quality_attribution.to_json_payload()
        ),
    }


def _counterfactual_baseline_return(result: CounterfactualBaselineResult) -> float:
    if result.baseline_return is None:
        raise RuntimeError("available counterfactual baseline is missing baseline_return.")
    return result.baseline_return


def _serialize_market_context_usage(
    usage: MarketContextUsageFacts | None,
) -> dict[str, object] | None:
    if usage is None:
        return None
    return {
        "context_visible": usage.context_visible,
        "source_event_ids": list(usage.source_event_ids),
        "warnings": list(usage.warnings),
        "decision_usages": [
            {
                "state_change_id": item.state_change_id,
                "effective_at": item.effective_at.isoformat(),
                "context_visible": item.context_visible,
                "market_context_hash": item.market_context_hash,
                "component_statuses": dict(sorted(item.component_statuses.items())),
                "calculation_versions": dict(
                    sorted(item.calculation_versions.items())
                ),
                "available_components": list(item.available_components),
                "partial_or_unavailable_components": list(
                    item.partial_or_unavailable_components
                ),
                "source_receipt_type": item.source_receipt_type,
                "source_event_ids": list(item.source_event_ids),
                "decision_text_reference_status": item.decision_text_reference_status,
                "referenced_components": list(item.referenced_components),
                "unreferenced_available_components": list(
                    item.unreferenced_available_components
                ),
                "market_tool_call_count": item.market_tool_call_count,
                "market_tool_statuses": list(item.market_tool_statuses),
                "market_tool_calls": [
                    call.to_dict() for call in item.market_tool_calls
                ],
                "warnings": list(item.warnings),
            }
            for item in usage.decision_usages
        ],
        "post_decision_outcome_summary": usage.post_decision_outcome_summary,
    }


def _serialize_horizon_baseline(item: HorizonBaseline) -> dict[str, object]:
    return {
        "episode_id": item.episode_id,
        "target_key": item.target_key,
        "horizon_hours": item.horizon.total_seconds() / 3600.0,
        "horizon_end_at": item.horizon_end_at.isoformat(),
        "status": item.status,
        "entry_bar_start_at": item.entry_bar_start_at.isoformat(),
        "exit_bar_start_at": (
            None if item.exit_bar_start_at is None else item.exit_bar_start_at.isoformat()
        ),
        "underlying_return": item.underlying_return,
        "strategy_return": item.strategy_return,
        "bar_count": item.bar_count,
    }


def _load_reflection_payload(
    *,
    final_boxed_answer: str,
    final_summary: str,
    task_id: str,
    log_dir: Path,
    log_file_path: str | None = None,
    required_fields: tuple[str, ...] = (),
    excluded_fields: tuple[str, ...] = (),
    payload_preprocessor: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    del task_id, log_dir

    def _is_matching_reflection_payload(payload: dict[str, Any]) -> bool:
        if payload_preprocessor is not None:
            payload_preprocessor(payload)
        try:
            _normalize_reflection_payload_object(
                payload,
                required_fields=required_fields,
                excluded_fields=excluded_fields,
            )
        except MiroThinkerReflectionRuntimeError as exc:
            raise ValueError(str(exc)) from exc
        return True

    try:
        return load_first_boxed_json_object(
            final_boxed_answer=final_boxed_answer,
            final_summary=final_summary,
            fallback_payload_texts=mirothinker_structured_output_fallback_texts_from_log(
                log_file_path
            ),
            payload_validator=_is_matching_reflection_payload,
            object_start_fields=(
                "assessed_horizons",
                "thesis_assessment",
                "trade_assessment",
                "review_decision",
                "decision_rationale",
                "learning_decision",
                "episode_id",
                "target_key",
            ),
            missing_error=(
                "MiroThinker reflection agent did not return a boxed JSON payload. "
                f"Final summary was: {final_summary}"
            ),
            non_json_error="MiroThinker reflection agent returned non-JSON boxed output.",
            non_object_error="MiroThinker reflection agent must return one JSON object.",
        )
    except BoxedJsonPayloadError as exc:
        raise MiroThinkerReflectionRuntimeError(str(exc)) from exc


def _normalize_reflection_payload_object(
    payload: Mapping[str, object],
    *,
    required_fields: tuple[str, ...] = (),
    excluded_fields: tuple[str, ...] = (),
) -> dict[str, object]:
    normalized_payload = dict(payload)
    trade_assessment = normalized_payload.get("trade_assessment")
    if "return_pct" in normalized_payload and isinstance(trade_assessment, dict):
        trade_payload = dict(trade_assessment)
        trade_payload.setdefault("return_pct", normalized_payload["return_pct"])
        normalized_payload["trade_assessment"] = trade_payload
        del normalized_payload["return_pct"]
    base_fields = {
        "assessed_horizons",
        "thesis_assessment",
        "trade_assessment",
        "review_decision",
        "decision_rationale",
    }
    optional_fields = {*_MARKET_CONTEXT_USAGE_QUALITY_FIELD_NAMES, "learning_decision"}
    expected_fields = (
        base_fields
        | set(required_fields)
        | (set(normalized_payload) & optional_fields)
    ) - set(excluded_fields)
    missing_fields = sorted(expected_fields - set(normalized_payload))
    unexpected_fields = sorted(set(normalized_payload) - expected_fields)
    if missing_fields or unexpected_fields:
        problems: list[str] = []
        if missing_fields:
            problems.append(f"missing field(s): {', '.join(missing_fields)}")
        if unexpected_fields:
            problems.append(f"unexpected field(s): {', '.join(unexpected_fields)}")
        raise MiroThinkerReflectionRuntimeError(
            "MiroThinker reflection agent returned the wrong payload shape; "
            f"{'; '.join(problems)}."
        )
    _normalize_usage_quality_payload(normalized_payload)
    return normalized_payload


def _normalize_horizon_assessments(value: object) -> tuple[ReflectionHorizonAssessment, ...]:
    if not isinstance(value, list) or not value:
        raise MiroThinkerReflectionRuntimeError(
            "assessed_horizons must be a non-empty JSON array."
        )
    assessments: list[ReflectionHorizonAssessment] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise MiroThinkerReflectionRuntimeError(
                f"assessed_horizons[{index}] must be a JSON object."
            )
        assessments.append(
            ReflectionHorizonAssessment(
                horizon_hours=_require_number(
                    item.get("horizon_hours"),
                    field_name=f"assessed_horizons[{index}].horizon_hours",
                ),
                return_pct=_require_number(
                    item.get("return_pct"),
                    field_name=f"assessed_horizons[{index}].return_pct",
                ),
                path_verdict=_require_path_verdict(
                    item.get("path_verdict"),
                    field_name=f"assessed_horizons[{index}].path_verdict",
                ),
                tradability_verdict=_require_tradability_verdict(
                    item.get("tradability_verdict"),
                    field_name=f"assessed_horizons[{index}].tradability_verdict",
                ),
                summary_md=_require_string(
                    item.get("summary_md"),
                    field_name=f"assessed_horizons[{index}].summary_md",
                ),
            )
        )
    return tuple(assessments)


def _normalize_thesis_assessment(value: object) -> ReflectionThesisAssessment:
    if not isinstance(value, dict):
        raise MiroThinkerReflectionRuntimeError(
            "thesis_assessment must be a JSON object."
        )
    return ReflectionThesisAssessment(
        verdict=_require_thesis_verdict(
            value.get("verdict"),
            field_name="thesis_assessment.verdict",
        ),
        summary_md=_require_string(
            value.get("summary_md"),
            field_name="thesis_assessment.summary_md",
        ),
    )


def _normalize_trade_assessment(value: object) -> ReflectionTradeAssessment | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MiroThinkerReflectionRuntimeError(
            "trade_assessment must be a JSON object or null."
        )
    return ReflectionTradeAssessment(
        verdict=_require_trade_verdict(
            value.get("verdict"),
            field_name="trade_assessment.verdict",
        ),
        summary_md=_require_string(
            value.get("summary_md"),
            field_name="trade_assessment.summary_md",
        ),
        return_pct=_optional_number(
            value.get("return_pct"),
            field_name="trade_assessment.return_pct",
        ),
    )


def _normalize_exposure_assessment(value: object) -> ReflectionExposureAssessment:
    if not isinstance(value, dict):
        raise MiroThinkerReflectionRuntimeError(
            "exposure_assessment must be a JSON object."
        )
    required_fields = {"summary_md"}
    missing_fields = sorted(required_fields - set(value))
    unexpected_fields = sorted(set(value) - required_fields)
    if missing_fields or unexpected_fields:
        details: list[str] = []
        if missing_fields:
            details.append(f"missing fields: {', '.join(missing_fields)}")
        if unexpected_fields:
            details.append(f"unexpected fields: {', '.join(unexpected_fields)}")
        raise MiroThinkerReflectionRuntimeError(
            "exposure_assessment " + "; ".join(details)
        )
    return ReflectionExposureAssessment(
        summary_md=_require_string(
            value.get("summary_md"),
            field_name="exposure_assessment.summary_md",
        ),
    )


def _normalize_watchlist_assessment(value: object) -> ReflectionWatchlistAssessment:
    if not isinstance(value, dict):
        raise MiroThinkerReflectionRuntimeError(
            "watchlist_assessment must be a JSON object."
        )
    return ReflectionWatchlistAssessment(
        verdict=_require_watchlist_verdict(
            value.get("verdict"),
            field_name="watchlist_assessment.verdict",
        ),
        summary_md=_require_string(
            value.get("summary_md"),
            field_name="watchlist_assessment.summary_md",
        ),
    )


def _normalize_usage_quality_payload(
    payload: Mapping[str, object],
) -> _UsageQualityPayload:
    label = _optional_usage_quality_label(
        payload.get("usage_quality_label"),
        field_name="usage_quality_label",
    )
    return {
        "usage_quality_label": label,
        "usage_quality_summary": _optional_usage_quality_summary(
            payload.get("usage_quality_summary"),
            label=label,
        ),
        "market_context_error_labels": _optional_usage_quality_error_labels(
            payload.get("market_context_error_labels"),
            usage_quality_label=label,
        ),
    }


def _optional_usage_quality_label(
    value: object,
    *,
    field_name: str,
) -> MarketContextUsageQualityLabel | None:
    if value is None:
        return None
    normalized = _require_string(value, field_name=field_name)
    for allowed_label in MARKET_CONTEXT_USAGE_QUALITY_LABELS:
        if normalized == allowed_label:
            return allowed_label
    raise MiroThinkerReflectionRuntimeError(
        f"{field_name} must be one of: "
        f"{', '.join(MARKET_CONTEXT_USAGE_QUALITY_LABELS)}."
    )


def _optional_usage_quality_summary(
    value: object,
    *,
    label: MarketContextUsageQualityLabel | None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MiroThinkerReflectionRuntimeError(
            "usage_quality_summary must be a string or null."
        )
    normalized = value.strip()
    if not normalized and label not in {None, "not_applicable"}:
        raise MiroThinkerReflectionRuntimeError(
            "usage_quality_summary may be empty only when "
            "usage_quality_label is absent or not_applicable."
        )
    return normalized


def _optional_usage_quality_error_labels(
    value: object,
    *,
    usage_quality_label: MarketContextUsageQualityLabel | None,
) -> tuple[MarketContextUsageQualityLabel, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MiroThinkerReflectionRuntimeError(
            "market_context_error_labels must be a JSON array."
        )
    if usage_quality_label == "not_applicable":
        return ()
    normalized: list[MarketContextUsageQualityLabel] = []
    seen: set[MarketContextUsageQualityLabel] = set()
    for index, item in enumerate(value):
        label = _optional_usage_quality_error_label(
            item,
            field_name=f"market_context_error_labels[{index}]",
            usage_quality_label=usage_quality_label,
        )
        if label is None:
            raise MiroThinkerReflectionRuntimeError(
                "market_context_error_labels must contain only strings."
            )
        if label in seen:
            continue
        seen.add(label)
        normalized.append(label)
    return tuple(normalized)


def _optional_usage_quality_error_label(
    value: object,
    *,
    field_name: str,
    usage_quality_label: MarketContextUsageQualityLabel | None,
) -> MarketContextUsageQualityLabel | None:
    if value is None:
        return None
    normalized = _require_string(value, field_name=field_name)
    for allowed_label in MARKET_CONTEXT_USAGE_QUALITY_LABELS:
        if normalized == allowed_label:
            return allowed_label
    if usage_quality_label not in {None, "not_applicable"}:
        return usage_quality_label
    return cast(MarketContextUsageQualityLabel, "inconclusive")


def _require_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MiroThinkerReflectionRuntimeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MiroThinkerReflectionRuntimeError(f"{field_name} must not be blank.")
    return normalized


def _require_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise MiroThinkerReflectionRuntimeError(f"{field_name} must be a datetime.")
    return value


def _require_review_decision(
    value: object,
    *,
    field_name: str,
) -> ReflectionReviewDecision:
    normalized = _require_string(value, field_name=field_name)
    if normalized not in {"write_review", "skip_review"}:
        raise MiroThinkerReflectionRuntimeError(
            f"{field_name} must be write_review or skip_review."
        )
    return cast(ReflectionReviewDecision, normalized)


def _require_path_verdict(
    value: object,
    *,
    field_name: str,
) -> ReflectionPathVerdict:
    normalized = _require_string(value, field_name=field_name)
    if normalized not in {"favorable", "mixed", "adverse", "unclear"}:
        raise MiroThinkerReflectionRuntimeError(
            f"{field_name} must be favorable, mixed, adverse, or unclear."
        )
    return cast(ReflectionPathVerdict, normalized)


def _require_tradability_verdict(
    value: object,
    *,
    field_name: str,
) -> ReflectionTradabilityVerdict:
    normalized = _require_string(value, field_name=field_name)
    if normalized not in {"tradable", "mixed", "poor", "unclear"}:
        raise MiroThinkerReflectionRuntimeError(
            f"{field_name} must be tradable, mixed, poor, or unclear."
        )
    return cast(ReflectionTradabilityVerdict, normalized)


def _require_thesis_verdict(
    value: object,
    *,
    field_name: str,
) -> ReflectionThesisVerdict:
    normalized = _require_string(value, field_name=field_name)
    if normalized not in {"validated", "mixed", "invalidated", "unclear"}:
        raise MiroThinkerReflectionRuntimeError(
            f"{field_name} must be validated, mixed, invalidated, or unclear."
        )
    return cast(ReflectionThesisVerdict, normalized)


def _require_trade_verdict(
    value: object,
    *,
    field_name: str,
) -> ReflectionTradeVerdict:
    normalized = _require_string(value, field_name=field_name)
    if normalized not in {"win", "loss", "scratch", "missed", "not_taken", "open"}:
        raise MiroThinkerReflectionRuntimeError(
            f"{field_name} must be win, loss, scratch, missed, not_taken, or open."
        )
    return cast(ReflectionTradeVerdict, normalized)


def _require_watchlist_verdict(
    value: object,
    *,
    field_name: str,
) -> ReflectionWatchlistVerdict:
    normalized = _require_string(value, field_name=field_name)
    if normalized not in {"useful", "mixed", "stale", "missing", "unclear"}:
        raise MiroThinkerReflectionRuntimeError(
            f"{field_name} must be useful, mixed, stale, missing, or unclear."
        )
    return cast(ReflectionWatchlistVerdict, normalized)


def _require_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MiroThinkerReflectionRuntimeError(f"{field_name} must be a number.")
    return float(value)


def _optional_number(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    return _require_number(value, field_name=field_name)


def _validate_existing_dir(path: Path, *, field_name: str) -> Path:
    validated = _validate_path(path, field_name=field_name)
    if not validated.exists():
        raise MiroThinkerReflectionRuntimeError(
            f"{field_name} does not exist: {validated}"
        )
    if not validated.is_dir():
        raise MiroThinkerReflectionRuntimeError(
            f"{field_name} must be a directory: {validated}"
        )
    return validated


def _validate_path(path: Path, *, field_name: str) -> Path:
    if not isinstance(path, Path):
        raise MiroThinkerReflectionRuntimeError(
            f"{field_name} must be a pathlib.Path."
        )
    return path.resolve(strict=False)


def _validate_non_blank_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MiroThinkerReflectionRuntimeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MiroThinkerReflectionRuntimeError(f"{field_name} must not be blank.")
    return normalized


__all__ = [
    "MiroThinkerReflectionRuntimeConfig",
    "MiroThinkerReflectionRuntimeError",
    "build_mirothinker_reflection_runner",
    "build_mirothinker_target_close_reflection_runner",
    "build_mirothinker_target_reflection_runner",
    "build_mirothinker_open_position_reflection_runner",
]


