"""Thin MiroThinker-backed runtime binding for one live web-search gather pass."""

from __future__ import annotations

import asyncio
import importlib
import json
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlsplit

from event_trader.contracts._validators import validate_timestamp
from event_trader.feeds.web_search import SearchAgentRunner
from event_trader.feeds.web_search_backfill import (
    HistoricalWebSearchCollectionResult,
    derive_historical_web_search_exit_reason,
    derive_historical_web_search_root_cause_detail,
)
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
    build_mirothinker_child_pythonpath,
    prepare_mirothinker_runtime,
)
from event_trader.integrations.mirothinker_task_log import (
    MiroThinkerCollectionToolTrace,
    extract_collection_tool_trace_from_tool_result,
)
from event_trader.integrations.strict_mirothinker_agent import (
    preflight_required_mcp_tools,
    raise_on_mirothinker_limit_failure,
)
from event_trader.source_policy import EVENT_TYPES, SOURCE_KINDS
from event_trader.source_archive.web_search import (
    HistoricalWebSearchWriteReceipt,
    WebSearchAcquisitionProvenance,
)

type SearchCollectionRunner = Callable[..., HistoricalWebSearchCollectionResult]

_COLLECTION_MAX_TURNS = 60
_COLLECTION_KEEP_TOOL_RESULTS = 12
_SEARCH_STAGING_DIR_NAME = "search_staging"
_LIVE_WEB_SEARCH_RECEIPT_PATH_ENV = "EVENT_TRADER_LIVE_WEB_SEARCH_RECEIPT_PATH"
_LIVE_WEB_SEARCH_TARGET_KEY_ENV = "EVENT_TRADER_LIVE_WEB_SEARCH_TARGET_KEY"
_LIVE_WEB_SEARCH_QUERY_ENV = "EVENT_TRADER_LIVE_WEB_SEARCH_QUERY"
_LIVE_WEB_SEARCH_DISCOVERED_AT_ENV = "EVENT_TRADER_LIVE_WEB_SEARCH_DISCOVERED_AT"
_HISTORICAL_WEB_SEARCH_RECEIPT_PATH_ENV = "EVENT_TRADER_HISTORICAL_WEB_SEARCH_RECEIPT_PATH"
_HISTORICAL_WEB_SEARCH_TARGET_KEY_ENV = "EVENT_TRADER_HISTORICAL_WEB_SEARCH_TARGET_KEY"
_HISTORICAL_WEB_SEARCH_QUERY_ENV = "EVENT_TRADER_HISTORICAL_WEB_SEARCH_QUERY"
_HISTORICAL_WEB_SEARCH_DISCOVERED_AT_ENV = "EVENT_TRADER_HISTORICAL_WEB_SEARCH_DISCOVERED_AT"
_HISTORICAL_WEB_SEARCH_ACQUISITION_PROVENANCE_ENV = (
    "EVENT_TRADER_HISTORICAL_WEB_SEARCH_ACQUISITION_PROVENANCE"
)
_LIVE_APPEND_TOOL_NAME = "append_live_web_search_candidate"
_HISTORICAL_APPEND_TOOL_NAME = "append_historical_web_search_candidate"
_LIVE_COLLECTION_MCP_SERVER_NAME = "event_trader_live_web_search"
_HISTORICAL_COLLECTION_MCP_SERVER_NAME = "event_trader_historical_web_search"
_OPENAI_WEB_SEARCH_TOOL_NAME = "event_trader_openai_web_search"
_ANTHROPIC_WEB_SEARCH_TOOL_NAME = "event_trader_anthropic_web_search"
_ACQUISITION_FAILURE_BUDGET = 10
_DEFAULT_ACQUISITION_TOOL_NAMES = (
    "search_and_scrape_webpage",
    "jina_scrape_llm_summary",
)
_PROJECT_OWNED_ACQUISITION_TOOL_NAMES = frozenset(
    {_OPENAI_WEB_SEARCH_TOOL_NAME, _ANTHROPIC_WEB_SEARCH_TOOL_NAME}
)
_SUPPORTED_ACQUISITION_TOOL_NAMES = frozenset(
    (*_DEFAULT_ACQUISITION_TOOL_NAMES, *_PROJECT_OWNED_ACQUISITION_TOOL_NAMES)
)
_PROJECT_COLLECTION_MCP_PRE_FLIGHT_SPECS = (
    (_LIVE_COLLECTION_MCP_SERVER_NAME, _LIVE_APPEND_TOOL_NAME),
    (_HISTORICAL_COLLECTION_MCP_SERVER_NAME, _HISTORICAL_APPEND_TOOL_NAME),
)
_ACQUISITION_MCP_PRE_FLIGHT_SPEC_BY_TOOL_NAME: dict[str, tuple[str, str]] = {
    "search_and_scrape_webpage": ("search_and_scrape_webpage", "google_search"),
    "jina_scrape_llm_summary": ("jina_scrape_llm_summary", "scrape_and_extract_info"),
}
_LIVE_SEARCH_MAX_ATTEMPTS = 2
_HISTORICAL_COLLECTION_MAX_ATTEMPTS = 2
_TRACKING_GROUNDING_QUERY_PARAM_NAMES = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
    }
)
_INSPECTED_ONLY_GROUNDING_NOISE_QUERY_PARAM_NAMES = frozenset(
    {
        "domshim",
        "noservercache",
        "noservertelemetry",
        "batchservertelemetry",
        "renderwebcomponents",
        "wcseo",
    }
)
_INSPECTED_ONLY_APIVERSION_QUERY_PARAM_NAME = "apiversion"
_WEB_SEARCH_EVENT_TYPE_LABELS = tuple(
    f"event_type:{event_type}" for event_type in sorted(EVENT_TYPES)
)
_WEB_SEARCH_ALLOWED_SOURCE_KIND_LABELS = tuple(
    f"source_kind:{source_kind}"
    for source_kind in sorted(SOURCE_KINDS - {"operator_brief"})
)


class MiroThinkerSearchRuntimeError(RuntimeError):
    """Raised when the committed MiroThinker search runtime cannot execute."""


class _AcquisitionFailureBudgetExceeded(BaseException):
    """Internal stop signal that must bypass MiroFlow rollback handling."""


class _LiveSearchAgentNeedsRetry(MiroThinkerSearchRuntimeError):
    def __init__(
        self,
        *,
        message: str,
        feedback: str,
    ) -> None:
        self.feedback = feedback
        super().__init__(message)


class _HistoricalCollectionNeedsRetry(MiroThinkerSearchRuntimeError):
    def __init__(
        self,
        *,
        message: str,
        feedback: str,
    ) -> None:
        self.feedback = feedback
        super().__init__(message)


@dataclass(slots=True)
class _RuntimeCollectionToolTraceRecorder:
    tracked_tools: frozenset[str]
    search_attempt_count: int = 0
    search_success_count: int = 0
    search_failure_count: int = 0
    scrape_attempt_count: int = 0
    scrape_success_count: int = 0
    scrape_failure_count: int = 0
    failure_details: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    inspected_source_refs: list[str] = field(default_factory=list)

    def record(
        self,
        *,
        server_name: str,
        tool_name: str,
        tool_result: object,
    ) -> None:
        if server_name not in self.tracked_tools and tool_name not in self.tracked_tools:
            return
        result_text = _tool_result_trace_text(tool_result)
        if result_text is None:
            return
        trace = extract_collection_tool_trace_from_tool_result(
            tool_name=tool_name,
            result_text=result_text,
        )
        self.search_attempt_count += trace.search_attempt_count
        self.search_success_count += trace.search_success_count
        self.search_failure_count += trace.search_failure_count
        self.scrape_attempt_count += trace.scrape_attempt_count
        self.scrape_success_count += trace.scrape_success_count
        self.scrape_failure_count += trace.scrape_failure_count
        self.failure_details.extend(trace.failure_details)
        self.source_refs.extend(trace.source_refs)
        self.inspected_source_refs.extend(trace.inspected_source_refs)

    def snapshot(self) -> MiroThinkerCollectionToolTrace:
        return MiroThinkerCollectionToolTrace(
            search_attempt_count=self.search_attempt_count,
            search_success_count=self.search_success_count,
            search_failure_count=self.search_failure_count,
            scrape_attempt_count=self.scrape_attempt_count,
            scrape_success_count=self.scrape_success_count,
            scrape_failure_count=self.scrape_failure_count,
            failure_details=tuple(self.failure_details[:5]),
            source_refs=_unique_runtime_texts(self.source_refs),
            inspected_source_refs=_unique_runtime_texts(self.inspected_source_refs),
        )


@dataclass(frozen=True, slots=True)
class _LiveWebSearchCollectionPayload:
    raw_payload: Mapping[str, object]
    accepted_count: int
    accepted_source_refs: tuple[str, ...]
    append_attempt_count: int
    append_rejected_count: int
    append_failed_count: int
    first_rejection_reason: str | None


@dataclass(frozen=True, slots=True)
class MiroThinkerSearchRuntimeConfig:
    """Explicit runtime inputs required to execute one MiroThinker search pass."""

    vendor_root: Path
    workspace_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_max_context_length: int
    serper_api_key: str
    serper_base_url: str
    jina_api_key: str
    jina_base_url: str
    summary_llm_api_key: str
    summary_llm_base_url: str
    summary_llm_model_name: str
    llm_reasoning_effort: str | None = None
    wall_clock_timeout_seconds: int = 0
    native_web_search_api_key: str = ""
    native_web_search_base_url: str = ""
    native_web_search_model: str = ""
    native_web_search_tool_type: str = "web_search"
    anthropic_web_search_api_key: str = ""
    anthropic_web_search_base_url: str = ""
    anthropic_web_search_model: str = ""
    anthropic_web_search_tool_type: str = "web_search_20250305"
    anthropic_web_search_max_uses: int = 5
    anthropic_web_search_version: str = "2023-06-01"
    acquisition_tool_names: tuple[str, ...] = _DEFAULT_ACQUISITION_TOOL_NAMES

    def __post_init__(self) -> None:
        acquisition_tool_names = _validate_tool_names(
            self.acquisition_tool_names,
            field_name="acquisition_tool_names",
        )
        object.__setattr__(self, "acquisition_tool_names", acquisition_tool_names)
        object.__setattr__(
            self,
            "vendor_root",
            _validate_existing_dir(self.vendor_root, field_name="vendor_root"),
        )
        object.__setattr__(
            self,
            "workspace_root",
            _validate_existing_dir(self.workspace_root, field_name="workspace_root"),
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
        object.__setattr__(
            self,
            "llm_max_context_length",
            _validate_positive_int(
                self.llm_max_context_length,
                field_name="llm_max_context_length",
            ),
        )
        object.__setattr__(
            self,
            "serper_api_key",
            _validate_tool_config_text(
                self.serper_api_key,
                field_name="serper_api_key",
                required="search_and_scrape_webpage" in acquisition_tool_names,
            ),
        )
        object.__setattr__(
            self,
            "serper_base_url",
            _validate_tool_config_text(
                self.serper_base_url,
                field_name="serper_base_url",
                required="search_and_scrape_webpage" in acquisition_tool_names,
            ),
        )
        object.__setattr__(
            self,
            "jina_api_key",
            _validate_tool_config_text(
                self.jina_api_key,
                field_name="jina_api_key",
                required="jina_scrape_llm_summary" in acquisition_tool_names,
            ),
        )
        object.__setattr__(
            self,
            "jina_base_url",
            _validate_tool_config_text(
                self.jina_base_url,
                field_name="jina_base_url",
                required="jina_scrape_llm_summary" in acquisition_tool_names,
            ),
        )
        object.__setattr__(
            self,
            "summary_llm_api_key",
            _validate_tool_config_text(
                self.summary_llm_api_key,
                field_name="summary_llm_api_key",
                required="jina_scrape_llm_summary" in acquisition_tool_names,
            ),
        )
        object.__setattr__(
            self,
            "summary_llm_base_url",
            _validate_tool_config_text(
                self.summary_llm_base_url,
                field_name="summary_llm_base_url",
                required="jina_scrape_llm_summary" in acquisition_tool_names,
            ),
        )
        object.__setattr__(
            self,
            "summary_llm_model_name",
            _validate_tool_config_text(
                self.summary_llm_model_name,
                field_name="summary_llm_model_name",
                required="jina_scrape_llm_summary" in acquisition_tool_names,
            ),
        )
        object.__setattr__(
            self,
            "llm_reasoning_effort",
            normalize_mirothinker_reasoning_effort(
                self.llm_reasoning_effort,
                field_name="llm_reasoning_effort",
                error_type=MiroThinkerSearchRuntimeError,
            ),
        )
        object.__setattr__(
            self,
            "wall_clock_timeout_seconds",
            _validate_non_negative_int(
                self.wall_clock_timeout_seconds,
                field_name="wall_clock_timeout_seconds",
            ),
        )
        native_web_search_required = _OPENAI_WEB_SEARCH_TOOL_NAME in acquisition_tool_names
        object.__setattr__(
            self,
            "native_web_search_api_key",
            _validate_tool_config_text(
                self.native_web_search_api_key,
                field_name="native_web_search_api_key",
                required=native_web_search_required,
            ),
        )
        object.__setattr__(
            self,
            "native_web_search_base_url",
            _validate_tool_config_text(
                self.native_web_search_base_url,
                field_name="native_web_search_base_url",
                required=native_web_search_required,
            ),
        )
        object.__setattr__(
            self,
            "native_web_search_model",
            _validate_tool_config_text(
                self.native_web_search_model,
                field_name="native_web_search_model",
                required=native_web_search_required,
            ),
        )
        object.__setattr__(
            self,
            "native_web_search_tool_type",
            _validate_native_web_search_tool_type(
                self.native_web_search_tool_type,
                required=native_web_search_required,
            ),
        )
        anthropic_web_search_required = _ANTHROPIC_WEB_SEARCH_TOOL_NAME in acquisition_tool_names
        object.__setattr__(
            self,
            "anthropic_web_search_api_key",
            _validate_tool_config_text(
                self.anthropic_web_search_api_key,
                field_name="anthropic_web_search_api_key",
                required=anthropic_web_search_required,
            ),
        )
        object.__setattr__(
            self,
            "anthropic_web_search_base_url",
            _validate_tool_config_text(
                self.anthropic_web_search_base_url,
                field_name="anthropic_web_search_base_url",
                required=anthropic_web_search_required,
            ),
        )
        object.__setattr__(
            self,
            "anthropic_web_search_model",
            _validate_tool_config_text(
                self.anthropic_web_search_model,
                field_name="anthropic_web_search_model",
                required=anthropic_web_search_required,
            ),
        )
        object.__setattr__(
            self,
            "anthropic_web_search_tool_type",
            _validate_tool_config_text(
                self.anthropic_web_search_tool_type,
                field_name="anthropic_web_search_tool_type",
                required=anthropic_web_search_required,
            ),
        )
        object.__setattr__(
            self,
            "anthropic_web_search_max_uses",
            _validate_positive_int(
                self.anthropic_web_search_max_uses,
                field_name="anthropic_web_search_max_uses",
            ),
        )
        object.__setattr__(
            self,
            "anthropic_web_search_version",
            _validate_tool_config_text(
                self.anthropic_web_search_version,
                field_name="anthropic_web_search_version",
                required=anthropic_web_search_required,
            ),
        )


def build_mirothinker_search_runner(
    *,
    config: MiroThinkerSearchRuntimeConfig,
) -> SearchAgentRunner:
    """Build the committed `run_search_agent` callable using vendored MiroThinker."""
    vendor_root = config.vendor_root.resolve(strict=False)

    def run_search_agent(
        *,
        target_key: str,
        query: str,
        discovered_at: datetime,
    ) -> Mapping[str, object]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise MiroThinkerSearchRuntimeError(
                "build_mirothinker_search_runner currently supports only "
                "synchronous callers; do not call it from an active event loop."
            )

        return asyncio.run(
            _run_search_agent_once(
                config=config,
                vendor_root=vendor_root,
                target_key=target_key,
                query=query,
                discovered_at=discovered_at,
            )
        )

    return run_search_agent


def build_mirothinker_search_collection_runner(
    *,
    config: MiroThinkerSearchRuntimeConfig,
) -> SearchCollectionRunner:
    """Build the committed `run_search_agent` callable for historical collection."""
    vendor_root = config.vendor_root.resolve(strict=False)

    def run_search_agent(
        *,
        target_key: str,
        query: str,
        discovered_at: datetime,
        acquisition_provenance: WebSearchAcquisitionProvenance | None = None,
    ) -> HistoricalWebSearchCollectionResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise MiroThinkerSearchRuntimeError(
                "build_mirothinker_search_collection_runner currently supports only "
                "synchronous callers; do not call it from an active event loop."
            )

        return asyncio.run(
            _run_search_collection_with_retries(
                config=config,
                vendor_root=vendor_root,
                target_key=target_key,
                query=query,
                discovered_at=discovered_at,
                acquisition_provenance=acquisition_provenance,
            )
        )

    return run_search_agent


async def _run_search_agent_once(
    *,
    config: MiroThinkerSearchRuntimeConfig,
    vendor_root: Path,
    target_key: str,
    query: str,
    discovered_at: datetime,
) -> Mapping[str, object]:
    base_task_id = _build_task_id(target_key=target_key, discovered_at=discovered_at)
    base_task_description = _build_search_task_prompt(
        target_key=target_key,
        query=query,
        discovered_at=discovered_at,
    )
    task_description = base_task_description
    for attempt_index in range(_LIVE_SEARCH_MAX_ATTEMPTS):
        task_id = (
            base_task_id
            if attempt_index == 0
            else f"{base_task_id}-retry{attempt_index}"
        )
        try:
            return await _run_live_search_agent_attempt(
                config=config,
                vendor_root=vendor_root,
                target_key=target_key,
                query=query,
                discovered_at=discovered_at,
                task_id=task_id,
                task_description=task_description,
            )
        except _LiveSearchAgentNeedsRetry as exc:
            if attempt_index + 1 >= _LIVE_SEARCH_MAX_ATTEMPTS:
                raise
            task_description = _build_search_task_prompt_after_validation_failure(
                base_task_description=base_task_description,
                feedback=exc.feedback,
            )
    raise MiroThinkerSearchRuntimeError("MiroThinker live search agent did not run.")


async def _run_live_search_agent_attempt(
    *,
    config: MiroThinkerSearchRuntimeConfig,
    vendor_root: Path,
    target_key: str,
    query: str,
    discovered_at: datetime,
    task_id: str,
    task_description: str,
) -> Mapping[str, object]:
    receipt_path = _live_web_search_receipt_path(
        workspace_root=config.workspace_root,
        task_id=task_id,
    )
    stage_root = _collection_stage_root(workspace_root=config.workspace_root, task_id=task_id)
    staged_receipt = _receipt_is_in_stage(receipt_path=receipt_path, stage_root=stage_root)
    try:
        if staged_receipt:
            _reset_collection_stage(stage_root)
        else:
            _reset_collection_artifact(receipt_path)
        task_started_at_ns = time.time_ns()
        runtime_tool_trace = _RuntimeCollectionToolTraceRecorder(
            tracked_tools=frozenset(config.acquisition_tool_names)
        )
        payload_text = await _run_search_task_once(
            config=config,
            vendor_root=vendor_root,
            target_key=target_key,
            query=query,
            discovered_at=discovered_at,
            task_description=task_description,
            max_turns=200,
            task_id=task_id,
            tool_names=config.acquisition_tool_names,
            live_collection_receipt_path=receipt_path,
            runtime_tool_trace=runtime_tool_trace,
        )
        collection_payload = _load_live_collection_payload(
            receipt_path,
            query=query,
            discovered_at=discovered_at,
        )
        if collection_payload.accepted_count > 0:
            tool_trace = _effective_collection_tool_trace(
                task_id=task_id,
                log_dir=config.log_dir,
                not_before_mtime_ns=task_started_at_ns,
                runtime_tool_trace=runtime_tool_trace,
            )
            if _collection_tool_success_count(tool_trace) <= 0:
                raise MiroThinkerSearchRuntimeError(
                    "MiroThinker live search agent accepted source candidates without "
                    "a successful acquisition tool call."
                )
            ungrounded_source_refs = _ungrounded_live_source_refs(
                accepted_source_refs=collection_payload.accepted_source_refs,
                acquisition_source_refs=tool_trace.inspected_source_refs,
            )
            if ungrounded_source_refs:
                message = (
                    "MiroThinker live search agent accepted source candidate(s) that "
                    "were not present in successful inspected source output: "
                    f"{', '.join(ungrounded_source_refs)}"
                )
                raise _LiveSearchAgentNeedsRetry(
                    message=message,
                    feedback=_build_source_grounding_retry_feedback(
                        ungrounded_source_refs=ungrounded_source_refs,
                        inspected_source_refs=tool_trace.inspected_source_refs,
                    ),
                )
            return collection_payload.raw_payload

        if collection_payload.append_rejected_count > 0:
            first_rejection_reason = (
                collection_payload.first_rejection_reason or "unknown rejection"
            )
            raise _LiveSearchAgentNeedsRetry(
                message=(
                    "MiroThinker live search agent submitted only rejected candidate(s); "
                    f"first rejection: {first_rejection_reason}"
                ),
                feedback=_build_append_rejected_retry_feedback(
                    first_rejection_reason=first_rejection_reason,
                ),
            )
        if collection_payload.append_failed_count > 0:
            raise MiroThinkerSearchRuntimeError(
                "MiroThinker live search agent candidate collection failed."
            )
        if collection_payload.append_attempt_count > 0:
            raise MiroThinkerSearchRuntimeError(
                "MiroThinker live search agent submitted candidate(s), but none were "
                "accepted by deterministic validation."
            )

        tool_trace = _effective_collection_tool_trace(
            task_id=task_id,
            log_dir=config.log_dir,
            not_before_mtime_ns=task_started_at_ns,
            runtime_tool_trace=runtime_tool_trace,
        )
        final_status = _normalize_collection_final_status(payload_text)
        if final_status == "no_source_found":
            if (
                _collection_tool_success_count(tool_trace) > 0
                and _collection_tool_has_source_ref_coverage(tool_trace=tool_trace)
                and _collection_tool_failure_count(tool_trace) <= 0
            ):
                return collection_payload.raw_payload
            raise _LiveSearchAgentNeedsRetry(
                message=(
                    "MiroThinker live search agent produced no accepted source candidates "
                    "and did not provide a verified no_source_found result."
                ),
                feedback=_build_unverified_no_source_found_retry_feedback(
                    tool_trace=tool_trace,
                ),
            )
        raise MiroThinkerSearchRuntimeError(
            "MiroThinker live search agent produced no accepted source candidates and "
            "did not provide a verified no_source_found result."
        )
    finally:
        if staged_receipt:
            _remove_collection_stage(stage_root)
        else:
            _reset_collection_artifact(receipt_path)


async def _run_search_collection_with_retries(
    *,
    config: MiroThinkerSearchRuntimeConfig,
    vendor_root: Path,
    target_key: str,
    query: str,
    discovered_at: datetime,
    acquisition_provenance: WebSearchAcquisitionProvenance | None = None,
) -> HistoricalWebSearchCollectionResult:
    base_task_id = _build_task_id(target_key=target_key, discovered_at=discovered_at)
    base_task_description = _build_search_collection_prompt(
        target_key=target_key,
        query=query,
        discovered_at=discovered_at,
    )
    task_description = base_task_description
    for attempt_index in range(_HISTORICAL_COLLECTION_MAX_ATTEMPTS):
        task_id = (
            base_task_id
            if attempt_index == 0
            else f"{base_task_id}-retry{attempt_index}"
        )
        try:
            return await _run_search_collection_once(
                config=config,
                vendor_root=vendor_root,
                target_key=target_key,
                query=query,
                discovered_at=discovered_at,
                acquisition_provenance=acquisition_provenance,
                task_id=task_id,
                task_description=task_description,
            )
        except _HistoricalCollectionNeedsRetry as exc:
            if attempt_index + 1 >= _HISTORICAL_COLLECTION_MAX_ATTEMPTS:
                raise
            task_description = _build_search_task_prompt_after_validation_failure(
                base_task_description=base_task_description,
                feedback=exc.feedback,
            )
    raise MiroThinkerSearchRuntimeError(
        "MiroThinker historical search agent did not run."
    )


async def _run_search_collection_once(
    *,
    config: MiroThinkerSearchRuntimeConfig,
    vendor_root: Path,
    target_key: str,
    query: str,
    discovered_at: datetime,
    acquisition_provenance: WebSearchAcquisitionProvenance | None,
    task_id: str,
    task_description: str,
) -> HistoricalWebSearchCollectionResult:
    receipt_path = _historical_web_search_receipt_path(
        workspace_root=config.workspace_root,
        task_id=task_id,
    )
    stage_root = _collection_stage_root(workspace_root=config.workspace_root, task_id=task_id)
    staged_receipt = _receipt_is_in_stage(receipt_path=receipt_path, stage_root=stage_root)
    if staged_receipt:
        _reset_collection_stage(stage_root)
    else:
        _reset_collection_artifact(receipt_path)
    task_started_at_ns = time.time_ns()
    runtime_tool_trace = _RuntimeCollectionToolTraceRecorder(
        tracked_tools=frozenset(config.acquisition_tool_names)
    )
    try:
        payload_text = await _run_search_task_once(
            config=config,
            vendor_root=vendor_root,
            target_key=target_key,
            query=query,
            discovered_at=discovered_at,
            historical_acquisition_provenance=acquisition_provenance,
            task_description=task_description,
            max_turns=_COLLECTION_MAX_TURNS,
            task_id=task_id,
            tool_names=config.acquisition_tool_names,
            historical_collection_receipt_path=receipt_path,
            runtime_tool_trace=runtime_tool_trace,
        )
        interim_result = _load_historical_collection_result(
            receipt_path,
            final_status="completed",
        )
        if not _collection_final_status_required(interim_result):
            result = interim_result
        else:
            tool_trace = _effective_collection_tool_trace(
                task_id=task_id,
                log_dir=config.log_dir,
                not_before_mtime_ns=task_started_at_ns,
                runtime_tool_trace=runtime_tool_trace,
            )
            try:
                final_status = _normalize_collection_final_status(payload_text)
            except MiroThinkerSearchRuntimeError as exc:
                if _is_retriable_historical_collection_final_status_error(str(exc)):
                    raise _HistoricalCollectionNeedsRetry(
                        message=(
                            "MiroThinker historical search agent returned an invalid "
                            "final status payload."
                        ),
                        feedback=_build_historical_final_status_retry_feedback(
                            error_text=str(exc)
                        ),
                    ) from exc
                raise
            result = _load_historical_collection_result(
                receipt_path,
                final_status=final_status,
                tool_trace=tool_trace,
            )
        return result
    finally:
        if staged_receipt:
            _remove_collection_stage(stage_root)
        else:
            _reset_collection_artifact(receipt_path)


async def _run_search_task_once(
    *,
    config: MiroThinkerSearchRuntimeConfig,
    vendor_root: Path,
    target_key: str,
    query: str,
    discovered_at: datetime,
    historical_acquisition_provenance: WebSearchAcquisitionProvenance | None = None,
    task_description: str,
    max_turns: int,
    task_id: str | None = None,
    tool_names: tuple[str, ...] = (
        "search_and_scrape_webpage",
        "jina_scrape_llm_summary",
    ),
    live_collection_receipt_path: Path | None = None,
    historical_collection_receipt_path: Path | None = None,
    runtime_tool_trace: _RuntimeCollectionToolTraceRecorder | None = None,
) -> str:
    task_id = task_id or _build_task_id(target_key=target_key, discovered_at=discovered_at)
    _prepare_vendor_environment(config)
    execute_task_pipeline, create_pipeline_components, OmegaConf = _load_vendor_runtime(vendor_root)
    keep_tool_result = _resolve_keep_tool_result(
        live_collection_receipt_path=live_collection_receipt_path,
        historical_collection_receipt_path=historical_collection_receipt_path,
    )

    agent_cfg = OmegaConf.create(
        {
            "project_name": "event-trader",
            "debug_dir": str(config.log_dir),
            "llm": build_mirothinker_llm_config(
                provider=config.llm_provider,
                model_name=config.llm_model_name,
                api_key=config.llm_api_key,
                base_url=config.llm_base_url,
                temperature=0.2,
                max_tokens=4096,
                max_context_length=config.llm_max_context_length,
                reasoning_effort=config.llm_reasoning_effort,
            ),
            "agent": {
                "main_agent": {
                    "tools": _vendor_tool_names(tool_names),
                    "tool_blacklist": [
                        ["search_and_scrape_webpage", "sogou_search"],
                    ],
                    "max_turns": max_turns,
                },
                "sub_agents": {},
                "keep_tool_result": keep_tool_result,
                "context_compress_limit": 0,
            },
            "benchmark": {},
        }
    )
    config.log_dir.mkdir(parents=True, exist_ok=True)

    main_agent_tool_manager, sub_agent_tool_managers, output_formatter = create_pipeline_components(
        agent_cfg
    )
    if live_collection_receipt_path is not None:
        _install_live_collection_mcp_server(
            tool_manager=main_agent_tool_manager,
            vendor_root=config.vendor_root,
            workspace_root=config.workspace_root,
            target_key=target_key,
            query=query,
            discovered_at=discovered_at,
            receipt_path=live_collection_receipt_path,
        )
    if historical_collection_receipt_path is not None:
        _install_historical_collection_mcp_server(
            tool_manager=main_agent_tool_manager,
            vendor_root=config.vendor_root,
            workspace_root=config.workspace_root,
            target_key=target_key,
            query=query,
            discovered_at=discovered_at,
            receipt_path=historical_collection_receipt_path,
            acquisition_provenance=historical_acquisition_provenance,
        )
    if _OPENAI_WEB_SEARCH_TOOL_NAME in tool_names:
        _install_openai_web_search_mcp_server(
            tool_manager=main_agent_tool_manager,
            config=config,
        )
    if _ANTHROPIC_WEB_SEARCH_TOOL_NAME in tool_names:
        _install_anthropic_web_search_mcp_server(
            tool_manager=main_agent_tool_manager,
            config=config,
        )
    _install_child_pythonpath(
        tool_manager=main_agent_tool_manager,
        vendor_root=config.vendor_root,
    )
    if live_collection_receipt_path is not None or historical_collection_receipt_path is not None:
        _install_project_collection_append_argument_normalizer(
            tool_manager=main_agent_tool_manager,
        )
    if live_collection_receipt_path is not None or historical_collection_receipt_path is not None:
        _install_acquisition_failure_budget_guard(
            tool_manager=main_agent_tool_manager,
            acquisition_tool_names=tool_names,
            runtime_tool_trace=runtime_tool_trace,
        )
    required_project_collection_tools = tuple(
        spec
        for spec in _PROJECT_COLLECTION_MCP_PRE_FLIGHT_SPECS
        if (
            live_collection_receipt_path is not None
            and spec[0] == _LIVE_COLLECTION_MCP_SERVER_NAME
        )
        or (
            historical_collection_receipt_path is not None
            and spec[0] == _HISTORICAL_COLLECTION_MCP_SERVER_NAME
        )
    )
    required_acquisition_tools = tuple(
        _ACQUISITION_MCP_PRE_FLIGHT_SPEC_BY_TOOL_NAME[tool_name]
        for tool_name in tool_names
        if tool_name in _ACQUISITION_MCP_PRE_FLIGHT_SPEC_BY_TOOL_NAME
    )
    required_preflight_tools = (
        *required_project_collection_tools,
        *required_acquisition_tools,
    )
    if required_preflight_tools:
        await preflight_required_mcp_tools(
            tool_manager=main_agent_tool_manager,
            required_tools=required_preflight_tools,
            error_factory=MiroThinkerSearchRuntimeError,
            agent_name="MiroThinker search agent",
        )
    try:
        run_coro = execute_task_pipeline(
            cfg=agent_cfg,
            task_id=task_id,
            task_description=task_description,
            task_file_name="",
            main_agent_tool_manager=main_agent_tool_manager,
            sub_agent_tool_managers=sub_agent_tool_managers,
            output_formatter=output_formatter,
            log_dir=str(config.log_dir),
        )
        final_summary, final_boxed_answer, log_file_path, _ = (
            await asyncio.wait_for(
                run_coro,
                timeout=config.wall_clock_timeout_seconds,
            )
            if config.wall_clock_timeout_seconds
            else await run_coro
        )
        raise_on_mirothinker_limit_failure(
            log_file_path=log_file_path,
            error_factory=MiroThinkerSearchRuntimeError,
            agent_name="MiroThinker search agent",
        )
    except _AcquisitionFailureBudgetExceeded as exc:
        raise MiroThinkerSearchRuntimeError(str(exc)) from None
    except TimeoutError as exc:
        raise MiroThinkerSearchRuntimeError(
            "MiroThinker search agent timed out after "
            f"{config.wall_clock_timeout_seconds} seconds."
        ) from exc
    try:
        return _extract_payload_text(
            final_boxed_answer=final_boxed_answer,
            final_summary=final_summary,
        )
    except MiroThinkerSearchRuntimeError:
        if (
            live_collection_receipt_path is not None
            or historical_collection_receipt_path is not None
        ):
            return ""
        raise


def _vendor_tool_names(tool_names: tuple[str, ...]) -> list[str]:
    return [
        tool_name
        for tool_name in tool_names
        if tool_name not in _PROJECT_OWNED_ACQUISITION_TOOL_NAMES
    ]


def _load_vendor_runtime(vendor_root: Path) -> tuple[Any, Any, Any]:
    _append_vendor_import_roots(vendor_root)
    try:
        pipeline_module = importlib.import_module("src.core.pipeline")
        omegaconf_module = importlib.import_module("omegaconf")
    except ModuleNotFoundError as exc:
        raise MiroThinkerSearchRuntimeError(
            "MiroThinker search runtime dependencies are not installed. "
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
        raise MiroThinkerSearchRuntimeError(str(exc)) from exc


def _prepare_vendor_environment(config: MiroThinkerSearchRuntimeConfig) -> None:
    import os

    project_src = Path(__file__).resolve(strict=False).parents[2]

    # MiroThinker snapshots optional MCP env at import time and passes it through
    # Pydantic, which rejects None even for tools we do not intend to call.
    env_updates = {
        "SERPER_API_KEY": config.serper_api_key,
        "SERPER_BASE_URL": config.serper_base_url,
        "JINA_API_KEY": config.jina_api_key,
        "JINA_BASE_URL": config.jina_base_url,
        "SUMMARY_LLM_API_KEY": config.summary_llm_api_key,
        "SUMMARY_LLM_BASE_URL": config.summary_llm_base_url,
        "SUMMARY_LLM_MODEL_NAME": config.summary_llm_model_name,
        "OPENAI_API_KEY": config.llm_api_key,
        "OPENAI_BASE_URL": config.llm_base_url,
        "TENCENTCLOUD_SECRET_ID": os.environ.get("TENCENTCLOUD_SECRET_ID", ""),
        "TENCENTCLOUD_SECRET_KEY": os.environ.get("TENCENTCLOUD_SECRET_KEY", ""),
        "PYTHONPATH": build_mirothinker_child_pythonpath(
            vendor_root=config.vendor_root,
            project_src=project_src,
        ),
    }
    for key, value in env_updates.items():
        os.environ[key] = value
    _refresh_vendor_settings_module_env(env_updates)


def _refresh_vendor_settings_module_env(env_updates: Mapping[str, str]) -> None:
    settings_module = sys.modules.get("src.config.settings")
    if settings_module is None:
        return
    for key, value in env_updates.items():
        if hasattr(settings_module, key):
            setattr(settings_module, key, value)


def _install_child_pythonpath(*, tool_manager: Any, vendor_root: Path) -> None:
    pythonpath = build_mirothinker_child_pythonpath(
        vendor_root=vendor_root,
        project_src=Path(__file__).resolve(strict=False).parents[2],
    )
    updated_configs: list[dict[str, object]] = []
    for server_config in tool_manager.server_configs:
        server_params = server_config["params"]
        params_env = getattr(server_params, "env", None)
        if params_env is None:
            updated_configs.append(server_config)
            continue
        updated_env = dict(params_env)
        updated_env["PYTHONPATH"] = pythonpath
        updated_params = server_params.__class__(
            command=server_params.command,
            args=list(server_params.args),
            env=updated_env,
            cwd=server_params.cwd,
            encoding=server_params.encoding,
            encoding_error_handler=server_params.encoding_error_handler,
        )
        updated_config = dict(server_config)
        updated_config["params"] = updated_params
        updated_configs.append(updated_config)
    tool_manager.server_configs = updated_configs
    tool_manager.server_dict = {config["name"]: config["params"] for config in updated_configs}


def _install_historical_collection_mcp_server(
    *,
    tool_manager: Any,
    vendor_root: Path,
    workspace_root: Path,
    target_key: str,
    query: str,
    discovered_at: datetime,
    receipt_path: Path,
    acquisition_provenance: WebSearchAcquisitionProvenance | None = None,
) -> None:
    existing_configs = list(getattr(tool_manager, "server_configs", ()))
    base_params = existing_configs[0]["params"] if existing_configs else None
    params_env = dict(getattr(base_params, "env", {}) or {})
    cwd = getattr(base_params, "cwd", None) if base_params is not None else str(workspace_root)
    pythonpath = build_mirothinker_child_pythonpath(
        vendor_root=vendor_root,
        project_src=Path(__file__).resolve(strict=False).parents[2],
    )
    server_env = dict(params_env)
    server_env.update(
        {
            "EVENT_TRADER_WORKSPACE_ROOT": str(workspace_root),
            _HISTORICAL_WEB_SEARCH_RECEIPT_PATH_ENV: str(receipt_path),
            _HISTORICAL_WEB_SEARCH_TARGET_KEY_ENV: target_key,
            _HISTORICAL_WEB_SEARCH_QUERY_ENV: query,
            _HISTORICAL_WEB_SEARCH_DISCOVERED_AT_ENV: discovered_at.isoformat(),
            _HISTORICAL_WEB_SEARCH_ACQUISITION_PROVENANCE_ENV: (
                ""
                if acquisition_provenance is None
                else json.dumps(
                    {
                        "prompt_profile_id": acquisition_provenance.prompt_profile_id,
                        "search_intent_hash": acquisition_provenance.search_intent_hash,
                        "control_language": acquisition_provenance.control_language,
                        "retrieval_languages": list(
                            acquisition_provenance.retrieval_languages
                        ),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            ),
            "PYTHONPATH": pythonpath,
        }
    )
    server_params = _make_stdio_server_parameters(
        command=sys.executable,
        args=["-m", "event_trader.integrations.historical_web_search_mcp_server"],
        env=server_env,
        cwd=cwd,
    )
    tool_manager.server_configs = [
        *existing_configs,
        {"name": "event_trader_historical_web_search", "params": server_params},
    ]
    tool_manager.server_dict = {
        config["name"]: config["params"] for config in tool_manager.server_configs
    }


def _install_live_collection_mcp_server(
    *,
    tool_manager: Any,
    vendor_root: Path,
    workspace_root: Path,
    target_key: str,
    query: str,
    discovered_at: datetime,
    receipt_path: Path,
) -> None:
    existing_configs = list(getattr(tool_manager, "server_configs", ()))
    base_params = existing_configs[0]["params"] if existing_configs else None
    params_env = dict(getattr(base_params, "env", {}) or {})
    cwd = getattr(base_params, "cwd", None) if base_params is not None else str(workspace_root)
    pythonpath = build_mirothinker_child_pythonpath(
        vendor_root=vendor_root,
        project_src=Path(__file__).resolve(strict=False).parents[2],
    )
    server_env = dict(params_env)
    server_env.update(
        {
            _LIVE_WEB_SEARCH_RECEIPT_PATH_ENV: str(receipt_path),
            _LIVE_WEB_SEARCH_TARGET_KEY_ENV: target_key,
            _LIVE_WEB_SEARCH_QUERY_ENV: query,
            _LIVE_WEB_SEARCH_DISCOVERED_AT_ENV: discovered_at.isoformat(),
            "PYTHONPATH": pythonpath,
        }
    )
    server_params = _make_stdio_server_parameters(
        command=sys.executable,
        args=["-m", "event_trader.integrations.live_web_search_mcp_server"],
        env=server_env,
        cwd=cwd,
    )
    tool_manager.server_configs = [
        *existing_configs,
        {"name": "event_trader_live_web_search", "params": server_params},
    ]
    tool_manager.server_dict = {
        config["name"]: config["params"] for config in tool_manager.server_configs
    }


def _install_openai_web_search_mcp_server(
    *,
    tool_manager: Any,
    config: MiroThinkerSearchRuntimeConfig,
) -> None:
    existing_configs = list(getattr(tool_manager, "server_configs", ()))
    base_params = existing_configs[0]["params"] if existing_configs else None
    params_env = dict(getattr(base_params, "env", {}) or {})
    cwd = (
        getattr(base_params, "cwd", None) if base_params is not None else str(config.workspace_root)
    )
    pythonpath = build_mirothinker_child_pythonpath(
        vendor_root=config.vendor_root,
        project_src=Path(__file__).resolve(strict=False).parents[2],
    )
    server_env = dict(params_env)
    server_env.update(
        {
            "EVENT_TRADER_OPENAI_WEB_SEARCH_API_KEY": config.native_web_search_api_key,
            "EVENT_TRADER_OPENAI_WEB_SEARCH_BASE_URL": config.native_web_search_base_url,
            "EVENT_TRADER_OPENAI_WEB_SEARCH_MODEL": config.native_web_search_model,
            "EVENT_TRADER_OPENAI_WEB_SEARCH_TOOL_TYPE": config.native_web_search_tool_type,
            "PYTHONPATH": pythonpath,
        }
    )
    server_params = _make_stdio_server_parameters(
        command=sys.executable,
        args=["-m", "event_trader.integrations.openai_web_search_mcp_server"],
        env=server_env,
        cwd=cwd,
    )
    tool_manager.server_configs = [
        *existing_configs,
        {"name": _OPENAI_WEB_SEARCH_TOOL_NAME, "params": server_params},
    ]
    tool_manager.server_dict = {
        config["name"]: config["params"] for config in tool_manager.server_configs
    }


def _install_anthropic_web_search_mcp_server(
    *,
    tool_manager: Any,
    config: MiroThinkerSearchRuntimeConfig,
) -> None:
    existing_configs = list(getattr(tool_manager, "server_configs", ()))
    base_params = existing_configs[0]["params"] if existing_configs else None
    params_env = dict(getattr(base_params, "env", {}) or {})
    cwd = (
        getattr(base_params, "cwd", None) if base_params is not None else str(config.workspace_root)
    )
    pythonpath = build_mirothinker_child_pythonpath(
        vendor_root=config.vendor_root,
        project_src=Path(__file__).resolve(strict=False).parents[2],
    )
    server_env = dict(params_env)
    server_env.update(
        {
            "EVENT_TRADER_ANTHROPIC_WEB_SEARCH_API_KEY": (config.anthropic_web_search_api_key),
            "EVENT_TRADER_ANTHROPIC_WEB_SEARCH_BASE_URL": (config.anthropic_web_search_base_url),
            "EVENT_TRADER_ANTHROPIC_WEB_SEARCH_MODEL": (config.anthropic_web_search_model),
            "EVENT_TRADER_ANTHROPIC_WEB_SEARCH_TOOL_TYPE": (config.anthropic_web_search_tool_type),
            "EVENT_TRADER_ANTHROPIC_WEB_SEARCH_MAX_USES": str(config.anthropic_web_search_max_uses),
            "EVENT_TRADER_ANTHROPIC_VERSION": config.anthropic_web_search_version,
            "PYTHONPATH": pythonpath,
        }
    )
    server_params = _make_stdio_server_parameters(
        command=sys.executable,
        args=["-m", "event_trader.integrations.anthropic_web_search_mcp_server"],
        env=server_env,
        cwd=cwd,
    )
    tool_manager.server_configs = [
        *existing_configs,
        {"name": _ANTHROPIC_WEB_SEARCH_TOOL_NAME, "params": server_params},
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
        raise MiroThinkerSearchRuntimeError(
            "MCP runtime is not installed; cannot configure project-owned search tools."
        ) from exc
    return StdioServerParameters(command=command, args=args, env=env, cwd=cwd)


def _build_task_id(*, target_key: str, discovered_at: datetime) -> str:
    timestamp = discovered_at.strftime("%Y%m%dT%H%M%S")
    return f"event-trader-search-{target_key}-{timestamp}"


def _build_search_task_prompt(
    *,
    target_key: str,
    query: str,
    discovered_at: datetime,
) -> str:
    discovered_iso = discovered_at.isoformat()
    live_append_example = json.dumps(
        {
            "source_ref": f"https://example.com/{target_key}-update",
            "title": f"{target_key} update",
            "content": "Source-grounded summary of what the opened page says.",
            "published_at": None,
            "labels": ["event_type:other", "source_kind:news_report", f"topic:{target_key}"],
        },
        separators=(",", ":"),
    )
    return (
        f"{_live_search_role_prompt(target_key=target_key)}"
        f"{_live_search_operating_boundary_prompt()}"
        "Tool protocol:\n"
        "Use the configured acquisition tools to find source-backed material. "
        "Acquisition tools are only retrieval aids; they are not the collection "
        f"result. Only {_LIVE_APPEND_TOOL_NAME} creates a committed live "
        "candidate.\n\n"
        "For every real opened source candidate surfaced by the retrieval process, "
        "capture what the source says, where it came from, and when it was "
        f"published. Then call {_LIVE_APPEND_TOOL_NAME} once to submit it for "
        "deterministic validation.\n\n"
        "Do not accumulate a final candidate list. The append tool is the source of "
        "truth for live collection results.\n\n"
        "Collection standard:\n"
        "- Ask what concrete thing the source documents.\n"
        "- Separate factual material from interpretation.\n"
        "- Preserve the original source identity and publication timing.\n"
        "- Keep the content grounded in the inspected source.\n"
        "- Do not judge importance, urgency, tradability, thesis impact, or "
        "position impact.\n\n"
        "Allowed reasoning:\n"
        "- You may decide whether a source is real and inspected.\n"
        "- You may skip placeholders, tool-error pages, empty pages, unopened search "
        "snippets, and sources clearly outside the active query window.\n"
        "- You may let exact duplicate source_ref values be ignored by the append "
        "tool, but do not perform similarity-based deduplication.\n"
        "- You may classify accepted sources only from explicit source facts using "
        "the committed event_type/source_kind label enums below.\n"
        "- You may add supplemental event/topic labels only when they are explicit "
        "from the source material.\n\n"
        "Forbidden reasoning:\n"
        "- Do not say the source is important or unimportant.\n"
        "- Do not say it should escalate.\n"
        "- Do not decide drop, watch, or escalate.\n"
        "- Do not say it confirms or invalidates a thesis.\n"
        "- Do not say it supports long, short, or flat.\n"
        "- Do not choose sources because they fit a desired market conclusion.\n\n"
        "Hard rules:\n"
        f"- The active query is {json.dumps(query)}.\n"
        f"- The collection timestamp is {json.dumps(discovered_iso)}.\n"
        "- For each distinct opened source candidate, call the append tool "
        "immediately with: source_ref, title, content, published_at, labels.\n"
        "- labels must be a JSON array of strings. Example append arguments: "
        f"{live_append_example}.\n"
        "- Every accepted item must emit exactly one event_type:* label and exactly "
        "one source_kind:* label.\n"
        "- event_type:* must be chosen only from these committed labels: "
        f"{', '.join(_WEB_SEARCH_EVENT_TYPE_LABELS)}.\n"
        "- source_kind:* must be chosen only from these committed external-web-search "
        f"labels: {', '.join(_WEB_SEARCH_ALLOWED_SOURCE_KIND_LABELS)}.\n"
        "- source_kind:operator_brief is forbidden for external web-search.\n"
        "- event:/topic: labels are optional supplemental metadata only; do not omit "
        "the required event_type/source_kind labels.\n"
        "- Do not search for sources because they fit a label taxonomy.\n"
        "- source_ref must identify the original source URL or canonical source "
        "identity. Do not submit a jina URL, mcp URL, tool name, or "
        "retrieval-method placeholder.\n"
        "- content must be grounded in the inspected source snapshot, not in "
        "search snippets alone.\n"
        "- Never submit placeholder, no-source, tool-error, or summary-only "
        "candidates.\n"
        "- If you do not have a real inspected source, do not call the append tool "
        "with invented fallback content.\n"
        "- If a source exposes an exact publication timestamp, published_at must use "
        "that exact timezone-aware ISO8601 timestamp.\n"
        "- If a source exposes only a calendar date, a relative time, or no reliable "
        "timestamp, pass null for published_at instead of guessing.\n"
        "- Do not wait for a better source if you already have a valid one. Append "
        "valid candidates immediately when you encounter them.\n"
        "- Do not skip weak, tangential, duplicate-looking, or low-conviction "
        "candidates because the checker may drop them later.\n\n"
        "Bounded search pass:\n"
        "- Do not stop merely because the first candidate was accepted. Continue "
        "through the current retrieval path and inspect natural follow-up leads.\n"
        "- Once the visible source pass is exhausted, return completed; do not keep "
        "searching solely to find one more source.\n\n"
        "Label rules:\n"
        "- labels must use only namespace-prefixed slugs.\n"
        "- Required classification labels: exactly one event_type:* and exactly one "
        "source_kind:*.\n"
        '- Optional supplemental labels: "event:", "topic:".\n'
        "- Do not output operator_source_basis, operator_confidence, "
        "source_authority, trade-decision, priority, quality, urgency, importance, "
        "confidence, or thesis labels.\n"
        "\n"
        "Output contract:\n"
        "Return exactly one tiny JSON object wrapped in \\boxed{...}. Do not include "
        "any text before or after the boxed JSON.\n\n"
        "The final JSON object must be exactly one of:\n"
        f'- `{{ "status": "completed" }}` if you called {_LIVE_APPEND_TOOL_NAME} '
        "at least once.\n"
        '- `{ "status": "no_source_found" }` if no real opened source is available '
        "for this live query.\n"
    )


def _live_search_role_prompt(*, target_key: str) -> str:
    return (
        "Role:\n"
        f"You are the market source collector for target '{target_key}'.\n"
        "Your job is to build a raw source blotter showing what is visible to the "
        "market now.\n\n"
    )


def _live_search_operating_boundary_prompt() -> str:
    return (
        "Operating boundary:\n"
        "- You are not the checker.\n"
        "- You are not the research analyst.\n"
        "- You are not the portfolio manager.\n"
        "- Do not judge importance, urgency, tradability, thesis impact, or "
        "position impact.\n\n"
    )


def _build_search_task_prompt_after_validation_failure(
    *,
    base_task_description: str,
    feedback: str,
) -> str:
    return base_task_description + "\n\n" + feedback


def _build_source_grounding_retry_feedback(
    *,
    ungrounded_source_refs: tuple[str, ...],
    inspected_source_refs: tuple[str, ...],
) -> str:
    return (
        "Previous search attempt failed deterministic source audit.\n"
        "The agent submitted accepted source_ref value(s) that were not proven by "
        "successful inspected source output.\n\n"
        "Submitted but unproven source_ref value(s):\n"
        f"{_format_retry_value_list(ungrounded_source_refs)}\n\n"
        "Successful inspected source output proved these source URL(s):\n"
        f"{_format_retry_value_list(inspected_source_refs)}\n\n"
        "For this new attempt, do not submit an unproven source_ref again unless "
        "you first inspect that source_ref with an acquisition tool. If a source "
        "cannot be inspected, submit an actually inspected source, find another "
        "inspectable source, or return no_source_found. Do not treat this audit "
        "feedback as source material."
    )


def _build_append_rejected_retry_feedback(*, first_rejection_reason: str) -> str:
    return (
        "Previous search attempt failed deterministic append validation.\n"
        "The append tool rejected every submitted candidate.\n\n"
        "First rejection reason:\n"
        f"- {first_rejection_reason}\n\n"
        "For this new attempt, use the original task and submit only candidates "
        "that satisfy the append contract. If no real inspected source exists, "
        "return no_source_found. Do not treat this validation feedback as source "
        "material."
    )


def _build_unverified_no_source_found_retry_feedback(
    *,
    tool_trace: MiroThinkerCollectionToolTrace,
) -> str:
    return (
        "Previous search attempt returned no_source_found, but deterministic "
        "audit could not verify that result.\n\n"
        "Audit facts:\n"
        f"- acquisition successes: {_collection_tool_success_count(tool_trace)}\n"
        f"- acquisition failures: {_collection_tool_failure_count(tool_trace)}\n"
        "- inspected source URL(s):\n"
        f"{_format_retry_value_list(tool_trace.inspected_source_refs)}\n"
        "- source URL(s) surfaced by acquisition tools:\n"
        f"{_format_retry_value_list(tool_trace.source_refs)}\n\n"
        "For this new attempt, inspect source material before returning "
        "no_source_found. If source candidates cannot be inspected, find another "
        "inspectable source or return no_source_found only after the acquisition "
        "trace proves the result. Do not treat this audit feedback as source "
        "material."
    )


def _build_historical_final_status_retry_feedback(*, error_text: str) -> str:
    return (
        "Previous historical search attempt failed the final output contract.\n"
        "The agent did not finish with the required boxed JSON status payload.\n\n"
        "Deterministic audit failure:\n"
        f"- {error_text}\n\n"
        "For this new attempt, keep using the append tool exactly as instructed, "
        "then end with exactly one tiny JSON object wrapped in \\boxed{...} and no "
        "extra text before or after it.\n"
        '- If one or more acceptable sources were appended, end with `{"status":"completed"}`.\n'
        '- If no acceptable inspected source is available after validation, end with `{"status":"no_source_found"}`.\n'
        "Do not treat this validation feedback as source material."
    )


def _format_retry_value_list(values: tuple[str, ...]) -> str:
    if not values:
        return "- (none)"
    return "\n".join(f"- {value}" for value in values)


def _build_search_collection_prompt(
    *,
    target_key: str,
    query: str,
    discovered_at: datetime,
) -> str:
    discovered_iso = discovered_at.isoformat()
    return (
        f"{_historical_search_role_prompt(target_key=target_key)}"
        f"{_historical_search_operating_boundary_prompt()}"
        "Tool protocol:\n"
        "Use the available search/scrape tools to discover concrete sources for the "
        "requested window from the perspective of what the market could have known "
        "by the active slice end. For each source candidate, call "
        f"{_HISTORICAL_APPEND_TOOL_NAME} once to submit it immediately for "
        "deterministic archive validation and append.\n\n"
        "Do not accumulate a final candidate list. The append tool is the source of "
        "truth for collection results.\n\n"
        "Output contract:\n"
        "Return exactly one tiny JSON object wrapped in \\boxed{...}. Do not include "
        "any text before or after the boxed JSON.\n\n"
        "The final JSON object must be exactly one of:\n"
        f'- `{{ "status": "completed" }}` if {_HISTORICAL_APPEND_TOOL_NAME} '
        "successfully appended one or more acceptable sources.\n"
        '- `{ "status": "no_source_found" }` if no acceptable inspected source is '
        "available for this cutoff after validation.\n\n"
        "Hard rules:\n"
        f"- The active query is {json.dumps(query)}.\n"
        f"- The active slice end (discovered_at/visible_at) is {json.dumps(discovered_iso)}.\n"
        "- For each distinct source candidate, call the append tool immediately with:\n"
        "  source_ref, title, content, published_at (ISO8601 or null), labels.\n"
        "- source_ref must identify the original source URL/canonical identity, never "
        "a tool endpoint.\n"
        "- Never submit placeholder, no-source, tool-error, or summary-only "
        "candidates. If you do not have a real source, do not call the append tool "
        "with invented fallback content.\n"
        "- Not every slice contains a big news event. A contemporaneous price page, "
        "exchange or futures market page, already-published institutional outlook, "
        "or other market-facing source that was genuinely available by the active "
        "slice end is still a valid candidate.\n"
        "- Do not wait for a better source if you already have a valid one. Append "
        "valid candidates immediately when you encounter them.\n"
        "- Do not stop merely because the first candidate was written. Continue "
        "through the current retrieval path and inspect natural follow-up leads.\n"
        "- If multiple candidate sources mostly repeat the same fact or the same "
        "market narrative, prefer the stronger, more original, more market-facing, "
        "or more information-dense one instead of submitting all low-increment "
        "variants.\n"
        "- Do not force aggressive deduplication. If two similar-looking sources "
        "still add distinct information, distinct evidence, or materially different "
        "market framing, it is acceptable to submit both.\n"
        "- Search as if you were standing at the active slice end and asking what was "
        "already knowable then.\n"
        "- Treat sources published later than the active slice end as out of scope, "
        "not merely lower quality.\n"
        "- Exclude later retrospective coverage, later recaps, and later summaries "
        "about earlier moves.\n"
        "- Prioritize contemporaneous or already-published market-facing material: "
        "primary releases, market reports, institutional outlooks already available, "
        "exchange/holiday notices, and contemporaneous price pages.\n"
        "- If a source has only date-level or relative publication timing, pass null "
        "for published_at.\n"
        "- Once the visible source pass is exhausted, return completed if at least "
        "one acceptable source was appended; otherwise return no_source_found.\n"
        "- Every accepted item must emit exactly one event_type:* label and exactly "
        "one source_kind:* label.\n"
        "- event_type:* must be chosen only from these committed labels: "
        f"{', '.join(_WEB_SEARCH_EVENT_TYPE_LABELS)}.\n"
        "- source_kind:* must be chosen only from these committed external-web-search "
        f"labels: {', '.join(_WEB_SEARCH_ALLOWED_SOURCE_KIND_LABELS)}.\n"
        "- source_kind:operator_brief is forbidden for external web-search.\n"
        '- Optional supplemental labels are limited to "event:" and "topic:".\n'
        "- Do not search for sources because they fit a classification taxonomy.\n"
        "- Do not output operator_source_basis, operator_confidence, "
        "source_authority, or trade-decision labels.\n"
    )


def _historical_search_role_prompt(*, target_key: str) -> str:
    return (
        "Role:\n"
        f"You are the historical market source collector for target '{target_key}'.\n"
        "Your job is to gather concrete sources that were genuinely available by the "
        "requested historical cut-off.\n\n"
    )


def _historical_search_operating_boundary_prompt() -> str:
    return (
        "Operating boundary:\n"
        "- You are not performing hindsight analysis.\n"
        "- You are not gathering later retrospective commentary about the window.\n"
        "- You are not the checker, research analyst, or portfolio manager.\n"
        "- Do not rank sources by trade thesis fit; preserve what the market could "
        "have known at the time.\n\n"
    )


def _resolve_keep_tool_result(
    *,
    live_collection_receipt_path: Path | None,
    historical_collection_receipt_path: Path | None,
) -> int:
    if live_collection_receipt_path is not None:
        return _COLLECTION_KEEP_TOOL_RESULTS
    if historical_collection_receipt_path is not None:
        return _COLLECTION_KEEP_TOOL_RESULTS
    return 5


def _shorten_error_text(value: str, *, max_length: int = 200) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[:max_length]}..."


def _tool_call_error_text(tool_result: object) -> str | None:
    if not isinstance(tool_result, Mapping):
        return None
    error = tool_result.get("error")
    if isinstance(error, str):
        error = error.strip()
        if error:
            return error

    raw_result = tool_result.get("result")
    if raw_result is None:
        return None
    return _extract_failure_text_from_tool_result(raw_result)


def _extract_failure_text_from_tool_result(tool_result_payload: object) -> str | None:
    payload = _normalize_tool_result_payload(tool_result_payload)
    if payload is None or not isinstance(payload, Mapping):
        return None
    success = payload.get("success")
    if isinstance(success, bool) and not success:
        raw_error = payload.get("error")
        if isinstance(raw_error, str):
            raw_error = raw_error.strip()
            if raw_error:
                return raw_error
        return "success=false"
    if isinstance(success, str) and success.strip().lower() in {"false", "0"}:
        raw_error = payload.get("error")
        if isinstance(raw_error, str):
            raw_error = raw_error.strip()
            if raw_error:
                return raw_error
        return "success=false"
    raw_error = payload.get("error")
    if isinstance(raw_error, str):
        raw_error = raw_error.strip()
        if raw_error:
            return raw_error
    return None


def _normalize_tool_result_payload(raw_payload: object) -> object | None:
    if isinstance(raw_payload, Mapping):
        return raw_payload
    if not isinstance(raw_payload, str):
        return None
    payload_text = raw_payload.strip()
    if not payload_text:
        return None
    try:
        return json.loads(payload_text)
    except json.JSONDecodeError:
        return None


def _tool_result_trace_text(tool_result: object) -> str | None:
    if isinstance(tool_result, str):
        normalized = tool_result.strip()
        return normalized or None
    if not isinstance(tool_result, Mapping):
        return None
    error = tool_result.get("error")
    if isinstance(error, str) and error.strip():
        return f"Tool call failed: {error.strip()}"
    raw_result = tool_result.get("result")
    if isinstance(raw_result, str):
        normalized = raw_result.strip()
        return normalized or None
    if isinstance(raw_result, Mapping):
        return json.dumps(raw_result)
    return None


def _install_acquisition_failure_budget_guard(
    *,
    tool_manager: Any,
    acquisition_tool_names: tuple[str, ...],
    consecutive_failure_budget: int = _ACQUISITION_FAILURE_BUDGET,
    runtime_tool_trace: _RuntimeCollectionToolTraceRecorder | None = None,
) -> None:
    tracked_tools = frozenset(acquisition_tool_names)
    if not tracked_tools:
        return

    original_execute_tool_call = getattr(tool_manager, "execute_tool_call", None)
    if not callable(original_execute_tool_call):
        raise MiroThinkerSearchRuntimeError(
            "Could not install acquisition failure budget guard; "
            "ToolManager execute_tool_call is unavailable."
        )

    failure_count = 0
    last_fail_server = ""
    last_fail_tool = ""
    last_fail_error = ""

    async def guarded_execute_tool_call(
        *args: object,
        **kwargs: object,
    ) -> Any:
        nonlocal failure_count, last_fail_server, last_fail_tool, last_fail_error
        if args:
            if len(args) != 3:
                raise TypeError("execute_tool_call expects server_name, tool_name, arguments.")
            server_name, tool_name, arguments = args
        else:
            if not all(
                isinstance(value, str)
                for value in (
                    kwargs.get("server_name"),
                    kwargs.get("tool_name"),
                )
            ):
                raise TypeError("execute_tool_call expects server_name and tool_name.")
            server_name = cast(str, kwargs.get("server_name"))
            tool_name = cast(str, kwargs.get("tool_name"))
            arguments = kwargs.get("arguments")
        if not isinstance(server_name, str) or not isinstance(tool_name, str):
            raise TypeError("execute_tool_call expects server_name and tool_name as strings.")

        tool_result = await original_execute_tool_call(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
        )
        if runtime_tool_trace is not None:
            runtime_tool_trace.record(
                server_name=server_name,
                tool_name=tool_name,
                tool_result=tool_result,
            )

        if server_name not in tracked_tools and tool_name not in tracked_tools:
            return tool_result

        failure_text = _tool_call_error_text(tool_result)
        if failure_text is None:
            failure_count = 0
            return tool_result

        failure_count += 1
        last_fail_server = server_name
        last_fail_tool = tool_name
        last_fail_error = _shorten_error_text(failure_text, max_length=180)
        if failure_count >= consecutive_failure_budget:
            raise _AcquisitionFailureBudgetExceeded(
                "Acquisition failure budget exceeded: "
                f"{failure_count} consecutive acquisition tool failures (threshold="
                f"{consecutive_failure_budget}). Last failure was "
                f"server={last_fail_server} tool={last_fail_tool}: "
                f"{last_fail_error}"
            )
        return tool_result

    tool_manager.execute_tool_call = guarded_execute_tool_call


def _install_project_collection_append_argument_normalizer(
    *,
    tool_manager: Any,
) -> None:
    original_execute_tool_call = getattr(tool_manager, "execute_tool_call", None)
    if not callable(original_execute_tool_call):
        raise MiroThinkerSearchRuntimeError(
            "Could not install collection append argument normalizer; "
            "ToolManager execute_tool_call is unavailable."
        )

    async def guarded_execute_tool_call(
        *args: object,
        **kwargs: object,
    ) -> Any:
        if args:
            if len(args) != 3:
                raise TypeError(
                    "execute_tool_call expects server_name, tool_name, arguments."
                )
            server_name, tool_name, arguments = args
        else:
            server_name = kwargs.get("server_name")
            tool_name = kwargs.get("tool_name")
            arguments = kwargs.get("arguments")
            if not isinstance(server_name, str) or not isinstance(tool_name, str):
                raise TypeError("execute_tool_call expects server_name and tool_name.")

        if not isinstance(server_name, str) or not isinstance(tool_name, str):
            raise TypeError("execute_tool_call expects server_name and tool_name.")

        normalized_arguments = _normalize_collection_append_arguments(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
        )

        tool_result = await original_execute_tool_call(
            server_name=server_name,
            tool_name=tool_name,
            arguments=normalized_arguments,
        )

        return tool_result

    tool_manager.execute_tool_call = guarded_execute_tool_call


def _normalize_collection_append_arguments(
    *,
    server_name: str,
    tool_name: str,
    arguments: object,
) -> object:
    if (
        server_name == _LIVE_COLLECTION_MCP_SERVER_NAME
        and tool_name == _LIVE_APPEND_TOOL_NAME
    ) or (
        server_name == _HISTORICAL_COLLECTION_MCP_SERVER_NAME
        and tool_name == _HISTORICAL_APPEND_TOOL_NAME
    ):
        if not isinstance(arguments, Mapping):
            return arguments
        normalized = dict(arguments)
        if "published_at" not in normalized:
            normalized["published_at"] = None
        else:
            normalized["published_at"] = _normalize_append_published_at_argument(
                normalized["published_at"]
            )
        if "labels" not in normalized:
            normalized["labels"] = []
        else:
            normalized["labels"] = _normalize_append_labels_argument(normalized["labels"])
        return normalized
    return arguments


def _normalize_append_published_at_argument(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        return None
    if _looks_like_calendar_date(normalized):
        return None
    return value


def _normalize_append_labels_argument(value: object) -> object:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping):
        return [
            key
            for key in value
            if isinstance(key, str) and key.strip()
        ]
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        return []
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return [
            item.strip()
            for item in normalized.split(",")
            if item.strip()
        ]
    if isinstance(parsed, list):
        return parsed
    return value


def _looks_like_calendar_date(value: str) -> bool:
    return len(value) == 10 and value.count("-") == 2


def _live_web_search_receipt_path(*, workspace_root: Path, task_id: str) -> Path:
    receipt_dir = _collection_stage_root(workspace_root=workspace_root, task_id=task_id)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    return (receipt_dir / "live_candidate_receipts.jsonl").resolve(strict=False)


def _historical_web_search_receipt_path(*, workspace_root: Path, task_id: str) -> Path:
    receipt_dir = _collection_stage_root(workspace_root=workspace_root, task_id=task_id)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    return (receipt_dir / "historical_candidate_receipts.jsonl").resolve(strict=False)


def _collection_stage_root(*, workspace_root: Path, task_id: str) -> Path:
    return (
        workspace_root / "runtime" / _SEARCH_STAGING_DIR_NAME / task_id
    ).resolve(strict=False)


def _receipt_is_in_stage(*, receipt_path: Path, stage_root: Path) -> bool:
    try:
        receipt_path.resolve(strict=False).relative_to(stage_root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _reset_collection_artifact(path: Path) -> None:
    if not path.exists():
        return
    if not path.is_file():
        raise MiroThinkerSearchRuntimeError(
            f"Web-search collection artifact path must be a file: {path}"
        )
    path.unlink()


def _reset_historical_collection_artifact(path: Path) -> None:
    _reset_collection_artifact(path)


def _reset_collection_stage(stage_root: Path) -> None:
    if stage_root.exists():
        if not stage_root.is_dir():
            raise MiroThinkerSearchRuntimeError(
                f"Web-search collection staging path must be a directory: {stage_root}"
            )
        shutil.rmtree(stage_root)
    stage_root.mkdir(parents=True, exist_ok=True)


def _remove_collection_stage(stage_root: Path) -> None:
    try:
        if stage_root.exists():
            shutil.rmtree(stage_root)
        staging_root = stage_root.parent
        if staging_root.exists() and staging_root.is_dir() and not any(staging_root.iterdir()):
            staging_root.rmdir()
    except OSError:
        return


def _load_live_collection_payload(
    receipt_path: Path,
    *,
    query: str,
    discovered_at: datetime,
) -> _LiveWebSearchCollectionPayload:
    receipt_payloads = _load_live_collection_receipts(receipt_path)
    _validate_collection_receipt_sequence(
        receipt_payloads,
        context="live web-search collection receipt",
        terminal_statuses={"accepted", "rejected", "failed", "noop_duplicate_source_ref"},
        source_ref_terminal_statuses={
            "accepted",
            "rejected",
            "failed",
            "noop_duplicate_source_ref",
        },
        allow_trailing_attempt=False,
    )
    items: list[dict[str, object]] = []
    append_attempt_count = 0
    append_rejected_count = 0
    append_failed_count = 0
    append_terminal_count = 0
    first_rejection_reason: str | None = None
    for payload in receipt_payloads:
        status = _require_receipt_text(
            payload,
            field_name="status",
            context="live web-search collection receipt",
        )
        if status == "append_attempt":
            append_attempt_count += 1
            continue
        if status == "accepted":
            append_terminal_count += 1
            labels = payload.get("labels")
            if not isinstance(labels, list):
                raise MiroThinkerSearchRuntimeError(
                    "Live web-search accepted receipt labels must be a list."
                )
            if not all(isinstance(label, str) for label in labels):
                raise MiroThinkerSearchRuntimeError(
                    "Live web-search accepted receipt labels must contain only strings."
                )
            items.append(
                {
                    "source_ref": _require_receipt_text(
                        payload,
                        field_name="source_ref",
                        context="live web-search collection receipt",
                    ),
                    "title": _require_receipt_text(
                        payload,
                        field_name="title",
                        context="live web-search collection receipt",
                    ),
                    "content": _require_receipt_text(
                        payload,
                        field_name="content",
                        context="live web-search collection receipt",
                    ),
                    "published_at": payload.get("published_at"),
                    "labels": cast(list[str], labels),
                }
            )
            continue
        if status == "rejected":
            append_terminal_count += 1
            append_rejected_count += 1
            if first_rejection_reason is None:
                first_rejection_reason = _require_receipt_text(
                    payload,
                    field_name="rejection_reason",
                    context="live web-search collection receipt",
                )
            continue
        if status == "failed":
            append_terminal_count += 1
            append_failed_count += 1
            continue
        if status == "noop_duplicate_source_ref":
            append_terminal_count += 1
            continue
        raise MiroThinkerSearchRuntimeError(
            f"Unknown live web-search collection receipt status: {status}"
        )
    if append_terminal_count != append_attempt_count:
        raise MiroThinkerSearchRuntimeError(
            "Live web-search collection receipt terminal entries must match "
            "append_attempt entries."
        )
    if items and append_failed_count > 0:
        raise MiroThinkerSearchRuntimeError(
            "Live web-search collection receipt contains accepted candidate(s) "
            "and failed append attempt(s)."
        )
    raw_payload = _normalize_agent_payload_object(
        {
            "query": query,
            "discovered_at": discovered_at.isoformat(),
            "items": items,
        }
    )
    return _LiveWebSearchCollectionPayload(
        raw_payload=raw_payload,
        accepted_count=len(items),
        accepted_source_refs=tuple(cast(str, item["source_ref"]) for item in items),
        append_attempt_count=append_attempt_count,
        append_rejected_count=append_rejected_count,
        append_failed_count=append_failed_count,
        first_rejection_reason=first_rejection_reason,
    )


def _collection_tool_success_count(tool_trace: MiroThinkerCollectionToolTrace) -> int:
    return tool_trace.search_success_count + tool_trace.scrape_success_count


def _collection_tool_failure_count(tool_trace: MiroThinkerCollectionToolTrace) -> int:
    return tool_trace.search_failure_count + tool_trace.scrape_failure_count


def _collection_tool_has_source_ref_coverage(
    *,
    tool_trace: MiroThinkerCollectionToolTrace,
) -> bool:
    if tool_trace.inspected_source_refs:
        return True
    return not tool_trace.source_refs


def _ungrounded_live_source_refs(
    *,
    accepted_source_refs: tuple[str, ...],
    acquisition_source_refs: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        source_ref
        for source_ref in accepted_source_refs
        if not any(
            _source_ref_grounding_match(
                accepted_source_ref=source_ref,
                inspected_source_ref=inspected_source_ref,
            )
            for inspected_source_ref in acquisition_source_refs
        )
    )


def _source_ref_grounding_match(
    *,
    accepted_source_ref: str,
    inspected_source_ref: str,
) -> bool:
    accepted = _parse_source_ref_for_grounding(accepted_source_ref)
    inspected = _parse_source_ref_for_grounding(inspected_source_ref)
    if accepted[:4] != inspected[:4]:
        return False
    return _grounding_query_match(
        accepted_query_items=accepted[4],
        inspected_query_items=inspected[4],
    )


def _parse_source_ref_for_grounding(
    source_ref: str,
) -> tuple[str, str, str, str, tuple[tuple[str, str], ...]]:
    parsed = urlsplit(source_ref.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    fragment = parsed.fragment
    query_items = tuple(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_grounding_query_param(key)
    )
    return (scheme, netloc, path, fragment, query_items)


def _grounding_query_match(
    *,
    accepted_query_items: tuple[tuple[str, str], ...],
    inspected_query_items: tuple[tuple[str, str], ...],
) -> bool:
    accepted_query_param_names = {
        _normalize_query_param_name(key) for key, _ in accepted_query_items
    }
    inspected_has_strong_transport_marker = any(
        _is_inspected_only_grounding_noise_query_param(key) for key, _ in inspected_query_items
    )
    accepted_index = 0
    accepted_count = len(accepted_query_items)
    for inspected_item in inspected_query_items:
        if (
            accepted_index < accepted_count
            and inspected_item == accepted_query_items[accepted_index]
        ):
            accepted_index += 1
            continue
        if (
            _is_inspected_only_grounding_noise_query_param(inspected_item[0])
            and _normalize_query_param_name(inspected_item[0])
            not in accepted_query_param_names
        ):
            continue
        if (
            _is_inspected_only_apiversion_query_param(inspected_item[0])
            and inspected_has_strong_transport_marker
            and _normalize_query_param_name(inspected_item[0])
            not in accepted_query_param_names
        ):
            continue
        return False
    return accepted_index == accepted_count


def _normalize_query_param_name(name: str) -> str:
    normalized = name.strip().lower()
    return normalized


def _is_tracking_grounding_query_param(name: str) -> bool:
    normalized = _normalize_query_param_name(name)
    return normalized.startswith("utm_") or normalized in _TRACKING_GROUNDING_QUERY_PARAM_NAMES


def _is_inspected_only_grounding_noise_query_param(name: str) -> bool:
    normalized = _normalize_query_param_name(name)
    return normalized in _INSPECTED_ONLY_GROUNDING_NOISE_QUERY_PARAM_NAMES


def _is_inspected_only_apiversion_query_param(name: str) -> bool:
    normalized = _normalize_query_param_name(name)
    return normalized == _INSPECTED_ONLY_APIVERSION_QUERY_PARAM_NAME


def _load_historical_collection_result(
    receipt_path: Path,
    *,
    final_status: str = "completed",
    tool_trace: MiroThinkerCollectionToolTrace | None = None,
) -> HistoricalWebSearchCollectionResult:
    if final_status not in {"completed", "no_source_found"}:
        raise MiroThinkerSearchRuntimeError(
            "Historical web-search collection final status must be completed or no_source_found."
        )
    if tool_trace is None:
        tool_trace = MiroThinkerCollectionToolTrace(
            search_attempt_count=0,
            search_success_count=0,
            search_failure_count=0,
            scrape_attempt_count=0,
            scrape_success_count=0,
            scrape_failure_count=0,
            failure_details=(),
        )
    receipt_payloads = _load_historical_collection_receipts(receipt_path)
    _validate_collection_receipt_sequence(
        receipt_payloads,
        context="historical web-search collection receipt",
        terminal_statuses={
            "written",
            "noop_existing",
            "rejected",
            "failed",
            "noop_duplicate_source_ref",
        },
        source_ref_terminal_statuses={
            "written",
            "noop_existing",
            "rejected",
            "failed",
            "noop_duplicate_source_ref",
        },
        allow_trailing_attempt=True,
    )
    write_receipts: list[HistoricalWebSearchWriteReceipt] = []
    boundary_rejection_reasons: list[str] = []
    failure_codes: list[str] = []
    successful_append_count = 0
    append_failed_count = 0
    append_terminal_count = 0
    for payload in receipt_payloads:
        status = _require_receipt_text(
            payload,
            field_name="status",
            context="historical web-search collection receipt",
        )
        if status == "append_attempt":
            continue
        if status == "written":
            successful_append_count += 1
            append_terminal_count += 1
            write_receipts.append(
                HistoricalWebSearchWriteReceipt(
                    status=cast(Any, status),
                    record_id=_require_receipt_text(
                        payload,
                        field_name="record_id",
                        context="historical web-search collection receipt",
                    ),
                    partition_path=Path(
                        _require_receipt_text(
                            payload,
                            field_name="partition_path",
                            context="historical web-search collection receipt",
                        )
                    ).resolve(strict=False),
                )
            )
            continue
        if status == "noop_existing":
            append_terminal_count += 1
            continue
        if status == "rejected":
            append_terminal_count += 1
            boundary_rejection_reasons.append(
                _require_receipt_text(
                    payload,
                    field_name="rejection_reason",
                    context="historical web-search collection receipt",
                )
            )
            continue
        if status == "failed":
            append_terminal_count += 1
            append_failed_count += 1
            error_code = payload.get("error_code")
            if isinstance(error_code, str) and error_code.strip():
                failure_codes.append(error_code.strip())
            else:
                failure_codes.append("append_tool_internal_error")
            continue
        if status == "noop_duplicate_source_ref":
            append_terminal_count += 1
            continue
        raise MiroThinkerSearchRuntimeError(
            f"Unknown historical web-search collection receipt status: {status}"
        )
    append_attempt_count = sum(
        1 for payload in receipt_payloads if payload.get("status") == "append_attempt"
    )
    append_rejected_count = len(boundary_rejection_reasons)
    collection_tool_success_count = (
        tool_trace.search_success_count + tool_trace.scrape_success_count
    )
    collection_tool_failure_count = (
        tool_trace.search_failure_count + tool_trace.scrape_failure_count
    )
    if (
        final_status == "no_source_found"
        and successful_append_count <= 0
        and append_failed_count <= 0
        and collection_tool_success_count > 0
        and _collection_tool_has_source_ref_coverage(tool_trace=tool_trace)
        and collection_tool_failure_count <= 0
    ):
        final_exit_reason = "no_source_found"
    else:
        final_exit_reason = derive_historical_web_search_exit_reason(
            successful_append_count=successful_append_count,
            search_attempt_count=tool_trace.search_attempt_count,
            search_success_count=tool_trace.search_success_count,
            search_failure_count=tool_trace.search_failure_count,
            scrape_attempt_count=tool_trace.scrape_attempt_count,
            scrape_success_count=tool_trace.scrape_success_count,
            scrape_failure_count=tool_trace.scrape_failure_count,
            append_attempt_count=append_attempt_count,
            append_failed_count=append_failed_count,
            append_terminal_count=append_terminal_count,
        )
    return HistoricalWebSearchCollectionResult(
        write_receipts=tuple(write_receipts),
        boundary_rejection_reasons=tuple(boundary_rejection_reasons),
        eligible_for_completion=(
            successful_append_count > 0 or final_exit_reason == "no_source_found"
        ),
        successful_append_count=successful_append_count,
        final_exit_reason=final_exit_reason,
        root_cause_detail=derive_historical_web_search_root_cause_detail(
            final_exit_reason=final_exit_reason,
            failure_codes=tuple(failure_codes),
        ),
        search_attempt_count=tool_trace.search_attempt_count,
        search_success_count=tool_trace.search_success_count,
        search_failure_count=tool_trace.search_failure_count,
        scrape_attempt_count=tool_trace.scrape_attempt_count,
        scrape_success_count=tool_trace.scrape_success_count,
        scrape_failure_count=tool_trace.scrape_failure_count,
        append_attempt_count=append_attempt_count,
        append_rejected_count=append_rejected_count,
        append_failed_count=append_failed_count,
        append_terminal_count=append_terminal_count,
        collection_failure_details=tool_trace.failure_details,
    )


def _collection_final_status_required(
    result: HistoricalWebSearchCollectionResult,
) -> bool:
    return result.successful_append_count <= 0


def _normalize_collection_final_status(payload_text: str) -> str:
    try:
        payload = load_first_boxed_json_object(
            final_boxed_answer=payload_text,
            final_summary="",
            object_start_fields=("status",),
            missing_error=(
                "MiroThinker historical search agent did not return a boxed JSON status payload."
            ),
            non_json_error=("MiroThinker historical search agent returned non-JSON boxed output."),
            non_object_error=("MiroThinker historical search agent must return one JSON object."),
        )
    except BoxedJsonPayloadError as exc:
        raise MiroThinkerSearchRuntimeError(str(exc)) from exc
    if set(payload) != {"status"}:
        unexpected_fields = sorted(set(payload) - {"status"})
        missing_fields = sorted({"status"} - set(payload))
        problems: list[str] = []
        if missing_fields:
            problems.append(f"missing field(s): {', '.join(missing_fields)}")
        if unexpected_fields:
            problems.append(f"unexpected field(s): {', '.join(unexpected_fields)}")
        raise MiroThinkerSearchRuntimeError(
            "MiroThinker historical search agent returned the wrong final payload "
            f"shape; {'; '.join(problems)}."
        )
    status = payload["status"]
    if status not in {"completed", "no_source_found"}:
        raise MiroThinkerSearchRuntimeError(
            "MiroThinker historical search agent final status must be completed or no_source_found."
        )
    return status


def _is_retriable_historical_collection_final_status_error(error_text: str) -> bool:
    return error_text.startswith(
        "MiroThinker historical search agent did not return a boxed JSON status payload."
    ) or error_text.startswith(
        "MiroThinker historical search agent returned non-JSON boxed output."
    ) or error_text.startswith(
        "MiroThinker historical search agent must return one JSON object."
    ) or error_text.startswith(
        "MiroThinker historical search agent returned the wrong final payload shape;"
    ) or error_text.startswith(
        "MiroThinker historical search agent final status must be completed or no_source_found."
    )


def _effective_collection_tool_trace(
    *,
    task_id: str,
    log_dir: Path,
    not_before_mtime_ns: int | None,
    runtime_tool_trace: _RuntimeCollectionToolTraceRecorder,
) -> MiroThinkerCollectionToolTrace:
    runtime_snapshot = runtime_tool_trace.snapshot()
    if _collection_tool_attempt_count(runtime_snapshot) > 0:
        return runtime_snapshot
    return _load_collection_tool_trace(
        task_id=task_id,
        log_dir=log_dir,
        not_before_mtime_ns=not_before_mtime_ns,
    )


def _load_collection_tool_trace(
    *,
    task_id: str,
    log_dir: Path,
    not_before_mtime_ns: int | None = None,
) -> MiroThinkerCollectionToolTrace:
    del task_id, log_dir, not_before_mtime_ns
    raise MiroThinkerSearchRuntimeError(
        "MiroThinker task logs are debug-only and are not a collection tool-trace source."
    )


def _collection_tool_attempt_count(tool_trace: MiroThinkerCollectionToolTrace) -> int:
    return tool_trace.search_attempt_count + tool_trace.scrape_attempt_count


def _unique_runtime_texts(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return tuple(unique_values)


def _write_historical_collection_summary(
    *,
    summary_path: Path,
    task_id: str,
    target_key: str,
    query: str,
    discovered_at: datetime,
    receipt_path: Path,
    completion_status: str,
    result: HistoricalWebSearchCollectionResult | None,
    final_exit_reason: str,
    error_text: str | None,
) -> None:
    receipt_count_error: str | None = None
    try:
        counts = _count_historical_collection_receipt_statuses(receipt_path=receipt_path)
    except MiroThinkerSearchRuntimeError as exc:
        counts = {
            "append_attempt": 0,
            "written": 0,
            "noop_existing": 0,
            "rejected": 0,
            "failed": 0,
            "noop_duplicate_source_ref": 0,
            "unknown_status": 0,
        }
        receipt_count_error = str(exc)
    if result is None:
        successful_append_count = counts["written"]
        boundary_rejections = counts["rejected"]
        eligible_for_completion = successful_append_count > 0
        search_attempt_count = 0
        search_success_count = 0
        search_failure_count = 0
        scrape_attempt_count = 0
        scrape_success_count = 0
        scrape_failure_count = 0
        collection_failure_details: tuple[str, ...] = ()
    else:
        successful_append_count = result.successful_append_count
        boundary_rejections = len(result.boundary_rejection_reasons)
        eligible_for_completion = result.eligible_for_completion
        search_attempt_count = result.search_attempt_count
        search_success_count = result.search_success_count
        search_failure_count = result.search_failure_count
        scrape_attempt_count = result.scrape_attempt_count
        scrape_success_count = result.scrape_success_count
        scrape_failure_count = result.scrape_failure_count
        collection_failure_details = result.collection_failure_details
    summary_payload: dict[str, object] = {
        "task_id": task_id,
        "target_key": target_key,
        "query": query,
        "discovered_at": discovered_at.isoformat(),
        "receipt_path": str(receipt_path.resolve(strict=False)),
        "completion_status": completion_status,
        "search_attempt_count": search_attempt_count,
        "search_success_count": search_success_count,
        "search_failure_count": search_failure_count,
        "scrape_attempt_count": scrape_attempt_count,
        "scrape_success_count": scrape_success_count,
        "scrape_failure_count": scrape_failure_count,
        "append_attempt_count": counts["append_attempt"],
        "append_written_count": counts["written"],
        "append_noop_existing_count": counts["noop_existing"],
        "append_rejected_count": counts["rejected"],
        "append_failed_count": counts["failed"],
        "append_noop_duplicate_source_ref_count": counts["noop_duplicate_source_ref"],
        "append_unknown_status_count": counts["unknown_status"],
        "successful_append_count": successful_append_count,
        "boundary_rejections": boundary_rejections,
        "eligible_for_completion": eligible_for_completion,
        "final_exit_reason": final_exit_reason,
        "collection_failure_details": list(collection_failure_details),
    }
    if result is not None:
        summary_payload["root_cause_detail"] = result.root_cause_detail
    if error_text is not None:
        summary_payload["error"] = error_text
    if receipt_count_error is not None:
        summary_payload["receipt_count_error"] = receipt_count_error
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _count_historical_collection_receipt_statuses(
    *,
    receipt_path: Path,
) -> dict[str, int]:
    counts = {
        "append_attempt": 0,
        "written": 0,
        "noop_existing": 0,
        "rejected": 0,
        "failed": 0,
        "noop_duplicate_source_ref": 0,
        "unknown_status": 0,
    }
    for payload in _load_historical_collection_receipts(receipt_path):
        status = _require_receipt_text(
            payload,
            field_name="status",
            context="historical web-search collection receipt",
        )
        if status not in counts:
            counts["unknown_status"] += 1
            continue
        counts[status] += 1
    return counts


def _load_live_collection_receipts(
    receipt_path: Path,
) -> tuple[dict[str, object], ...]:
    if not receipt_path.exists():
        return ()
    if not receipt_path.is_file():
        raise MiroThinkerSearchRuntimeError(
            f"Live web-search receipt path must be a file: {receipt_path}"
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
                raise MiroThinkerSearchRuntimeError(
                    "Live web-search receipt file contains invalid JSON."
                ) from exc
            if not isinstance(payload, dict):
                raise MiroThinkerSearchRuntimeError(
                    "Live web-search receipt entries must be JSON objects."
                )
            receipts.append(payload)
    return tuple(receipts)


def _load_historical_collection_receipts(
    receipt_path: Path,
) -> tuple[dict[str, object], ...]:
    if not receipt_path.exists():
        return ()
    if not receipt_path.is_file():
        raise MiroThinkerSearchRuntimeError(
            f"Historical web-search receipt path must be a file: {receipt_path}"
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
                raise MiroThinkerSearchRuntimeError(
                    "Historical web-search receipt file contains invalid JSON."
                ) from exc
            if not isinstance(payload, dict):
                raise MiroThinkerSearchRuntimeError(
                    "Historical web-search receipt entries must be JSON objects."
                )
            receipts.append(payload)
    return tuple(receipts)


def _validate_collection_receipt_sequence(
    receipt_payloads: tuple[dict[str, object], ...],
    *,
    context: str,
    terminal_statuses: set[str],
    source_ref_terminal_statuses: set[str],
    allow_trailing_attempt: bool,
) -> None:
    pending_source_ref: str | None = None
    for payload in receipt_payloads:
        status = _require_receipt_text(
            payload,
            field_name="status",
            context=context,
        )
        if status == "append_attempt":
            if pending_source_ref is not None:
                raise MiroThinkerSearchRuntimeError(
                    f"{context} has a new append_attempt before the previous "
                    "attempt reached a terminal receipt."
                )
            pending_source_ref = _require_receipt_text(
                payload,
                field_name="source_ref",
                context=context,
            )
            continue
        if status not in terminal_statuses:
            raise MiroThinkerSearchRuntimeError(f"Unknown {context} status: {status}")
        if pending_source_ref is None:
            raise MiroThinkerSearchRuntimeError(
                f"{context} terminal receipt has no preceding append_attempt."
            )
        if status in source_ref_terminal_statuses:
            terminal_source_ref = _require_receipt_text(
                payload,
                field_name="source_ref",
                context=context,
            )
            if terminal_source_ref != pending_source_ref:
                raise MiroThinkerSearchRuntimeError(
                    f"{context} terminal receipt source_ref does not match its "
                    "append_attempt source_ref."
                )
        pending_source_ref = None
    if pending_source_ref is not None and not allow_trailing_attempt:
        raise MiroThinkerSearchRuntimeError(
            f"{context} append_attempt has no terminal receipt."
        )


def _require_receipt_text(
    payload: dict[str, object],
    *,
    field_name: str,
    context: str,
) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise MiroThinkerSearchRuntimeError(f"{context} field {field_name!r} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MiroThinkerSearchRuntimeError(f"{context} field {field_name!r} must not be blank.")
    return normalized


def _extract_payload_text(
    *,
    final_boxed_answer: str,
    final_summary: str,
) -> str:
    try:
        payload = load_first_boxed_json_object(
            final_boxed_answer=final_boxed_answer,
            final_summary=final_summary,
            object_start_fields=("status", "query"),
            missing_error=(
                "MiroThinker search agent did not return a boxed JSON payload. "
                f"Final summary was: {final_summary}"
            ),
            non_json_error="MiroThinker search agent returned non-JSON boxed output.",
            non_object_error="MiroThinker search agent must return one JSON object.",
        )
    except BoxedJsonPayloadError as exc:
        raise MiroThinkerSearchRuntimeError(str(exc)) from exc
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[1:-1]


def _normalize_agent_payload_object(payload: object) -> Mapping[str, object]:
    if not isinstance(payload, dict):
        raise MiroThinkerSearchRuntimeError("MiroThinker search agent must return one JSON object.")

    required_fields = {
        "query",
        "discovered_at",
        "items",
    }
    allowed_fields = required_fields

    missing_fields = sorted(required_fields - set(payload))
    unexpected_fields = sorted(set(payload) - allowed_fields)
    if missing_fields or unexpected_fields:
        problems: list[str] = []
        if missing_fields:
            problems.append(f"missing field(s): {', '.join(missing_fields)}")
        if unexpected_fields:
            problems.append(f"unexpected field(s): {', '.join(unexpected_fields)}")
        raise MiroThinkerSearchRuntimeError(
            f"MiroThinker search agent returned the wrong payload shape; {'; '.join(problems)}."
        )

    discovered_at = _parse_timestamp(
        cast(object, payload["discovered_at"]),
        field_name="discovered_at",
    )
    items = payload["items"]
    if not isinstance(items, list):
        raise MiroThinkerSearchRuntimeError("items must be a JSON array.")

    return {
        "query": _require_string(payload["query"], field_name="query"),
        "discovered_at": discovered_at,
        "items": [
            _normalize_agent_payload_item(item, index=index) for index, item in enumerate(items)
        ],
    }


def _normalize_agent_payload_item(item: object, *, index: int) -> Mapping[str, object]:
    if not isinstance(item, dict):
        raise MiroThinkerSearchRuntimeError(f"items[{index}] must be a JSON object.")
    required_fields = {"source_ref", "title", "content", "labels"}
    optional_fields = {"published_at"}
    allowed_fields = required_fields | optional_fields
    missing_fields = sorted(required_fields - set(item))
    unexpected_fields = sorted(set(item) - allowed_fields)
    if missing_fields or unexpected_fields:
        problems: list[str] = []
        if missing_fields:
            problems.append(f"missing field(s): {', '.join(missing_fields)}")
        if unexpected_fields:
            problems.append(f"unexpected field(s): {', '.join(unexpected_fields)}")
        raise MiroThinkerSearchRuntimeError(
            "MiroThinker search agent returned the wrong item shape; "
            f"items[{index}]: {'; '.join(problems)}."
        )

    labels = item["labels"]
    if not isinstance(labels, list):
        raise MiroThinkerSearchRuntimeError(f"items[{index}].labels must be a JSON array.")
    if not all(isinstance(label, str) for label in labels):
        raise MiroThinkerSearchRuntimeError(f"items[{index}].labels must contain only strings.")
    published_at_raw = item.get("published_at")
    published_at = (
        None
        if published_at_raw is None
        else _parse_timestamp(
            published_at_raw,
            field_name=f"items[{index}].published_at",
        )
    )
    return {
        "source_ref": _require_string(item["source_ref"], field_name="source_ref"),
        "title": _require_string(item["title"], field_name="title"),
        "content": _require_string(item["content"], field_name="content"),
        "published_at": published_at,
        "labels": cast(list[str], labels),
    }


def _parse_timestamp(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise MiroThinkerSearchRuntimeError(f"{field_name} must be an ISO8601 string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MiroThinkerSearchRuntimeError(
            f"{field_name} must be a valid ISO8601 string."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if field_name == "published_at" or field_name.endswith(".published_at"):
            raise MiroThinkerSearchRuntimeError(
                "published_at must be a timezone-aware ISO8601 datetime or null; "
                "date-only values such as YYYY-MM-DD are not allowed."
            )
        raise MiroThinkerSearchRuntimeError(
            f"{field_name} must be a timezone-aware ISO8601 datetime."
        )
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=MiroThinkerSearchRuntimeError,
    )


def _require_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MiroThinkerSearchRuntimeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MiroThinkerSearchRuntimeError(f"{field_name} must not be blank.")
    return normalized


def _validate_existing_dir(path: Path, *, field_name: str) -> Path:
    validated = _validate_path(path, field_name=field_name)
    if not validated.exists():
        raise MiroThinkerSearchRuntimeError(f"{field_name} does not exist: {validated}")
    if not validated.is_dir():
        raise MiroThinkerSearchRuntimeError(f"{field_name} must be a directory: {validated}")
    return validated


def _validate_path(path: Path, *, field_name: str) -> Path:
    if not isinstance(path, Path):
        raise MiroThinkerSearchRuntimeError(f"{field_name} must be a pathlib.Path.")
    return path.resolve(strict=False)


def _validate_non_blank_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MiroThinkerSearchRuntimeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MiroThinkerSearchRuntimeError(f"{field_name} must not be blank.")
    return normalized


def _validate_tool_config_text(value: str, *, field_name: str, required: bool) -> str:
    if not isinstance(value, str):
        raise MiroThinkerSearchRuntimeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if required and not normalized:
        raise MiroThinkerSearchRuntimeError(f"{field_name} must not be blank.")
    return normalized


def _validate_native_web_search_tool_type(value: str, *, required: bool) -> str:
    normalized = _validate_tool_config_text(
        value,
        field_name="native_web_search_tool_type",
        required=required,
    )
    if not normalized:
        return normalized
    if normalized not in {"web_search", "web_search_preview"}:
        raise MiroThinkerSearchRuntimeError(
            "native_web_search_tool_type must be web_search or web_search_preview."
        )
    return normalized


def _validate_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MiroThinkerSearchRuntimeError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise MiroThinkerSearchRuntimeError(f"{field_name} must be greater than zero.")
    return value


def _validate_non_negative_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MiroThinkerSearchRuntimeError(f"{field_name} must be a non-negative integer.")
    if value < 0:
        raise MiroThinkerSearchRuntimeError(f"{field_name} must be greater than or equal to zero.")
    return value


def _validate_tool_names(value: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise MiroThinkerSearchRuntimeError(f"{field_name} must be a tuple of strings.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MiroThinkerSearchRuntimeError(f"{field_name} must contain only strings.")
        tool_name = item.strip()
        if not tool_name:
            raise MiroThinkerSearchRuntimeError(f"{field_name} must not contain blank tool names.")
        if tool_name not in _SUPPORTED_ACQUISITION_TOOL_NAMES:
            allowed = ", ".join(sorted(_SUPPORTED_ACQUISITION_TOOL_NAMES))
            raise MiroThinkerSearchRuntimeError(
                f"{field_name} contains unsupported tool name {tool_name!r}; "
                f"supported values: {allowed}."
            )
        if tool_name in normalized:
            raise MiroThinkerSearchRuntimeError(
                f"{field_name} must not contain duplicate tool names."
            )
        normalized.append(tool_name)
    return tuple(normalized)


__all__ = [
    "MiroThinkerSearchRuntimeConfig",
    "MiroThinkerSearchRuntimeError",
    "build_mirothinker_search_collection_runner",
    "build_mirothinker_search_runner",
]
