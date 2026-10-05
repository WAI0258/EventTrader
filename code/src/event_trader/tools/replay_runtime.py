"""Build replay input and run replay runtime workflows."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from pathlib import Path
from typing import Literal, NoReturn, cast

from nautilus_trader.common.component import TestClock

from event_trader.analysis import FileBackedResearchMemoryReader
from event_trader.audit import (
    write_release_readiness_report,
    write_replay_acceptance_report,
    write_replay_cost_report,
    write_target_replay_report,
)
from event_trader.audit.pm_review_readiness import (
    build_pm_review_readiness_report,
    render_pm_review_readiness_markdown,
)
from event_trader.composition_error import CompositionError
from event_trader.config import (
    KernelConfig,
    ValidationMarketDataConfig,
    ValidationMarketMappingConfig,
    load_kernel_config,
    resolve_config_relative_path,
)
from event_trader.contracts._validators import validate_labels, validate_target_key
from event_trader.contracts.ports import OutcomeContextPort
from event_trader.contracts.view_state_change import (
    MarketDataSeries,
    MarketMapping,
)
from event_trader.evidence_ledger import FileBackedEvidenceLedger
from event_trader.execution import ExecutionRecord, ExecutionRecordStore
from event_trader.feeds.historical_web_search_guard import (
    HistoricalWebSearchFutureLeakage,
    detect_historical_web_search_future_leakage,
)
from event_trader.feeds.market_news_backfill import (
    MarketNewsBackfillReceipt,
    backfill_market_news,
)
from event_trader.feeds.models import (
    HistoricalWebSearchInput,
    ReplayIngressInput,
    ReplaySourceShape,
)
from event_trader.feeds.payload_mappers import replay_adapter_for
from event_trader.feeds.web_search_backfill import (
    HistoricalWebSearchBackfillReceipt,
    backfill_historical_web_search,
)
from event_trader.ingest.admission import (
    derive_admission_event_id,
    validate_admission_request,
)
from event_trader.integrations import (
    MiroThinkerReflectionRuntimeConfig,
    build_mirothinker_open_position_reflection_runner,
    build_mirothinker_search_collection_runner,
)
from event_trader.integrations.mirothinker_llm_config import (
    normalize_mirothinker_reasoning_effort,
)
from event_trader.integrations.mirothinker_search import (
    MiroThinkerSearchRuntimeConfig,
)
from event_trader.market.adjustments import (
    MarketAdjustmentSidecar,
    load_adjustment_sidecar_from_archive_root,
    load_adjustment_sidecar_from_source_metadata,
)
from event_trader.market.prefetch import (
    ReplayMarketPrefetchEvent,
    ReplayMarketPrefetchReceipt,
    prefetch_replay_market_data,
)
from event_trader.market.provider import MarketBarsProvider
from event_trader.migrations import run_active_price_basis_cutover_from_config
from event_trader.migrations.pm_review_workspace import prepare_pm_review_workspace
from event_trader.operator.repair import (
    OperatorRepairError,
    apply_replay_repair_plan,
    plan_replay_failed_event_repair,
)
from event_trader.pm_review.runtime import validate_pm_review_runtime_preflight
from event_trader.reflection.context import ReflectionLedgerContext
from event_trader.reflection.contracts import (
    OutcomeContextPacket,
    ReviewAnchorIdentity,
    ReviewCoverage,
)
from event_trader.reflection.feedback_context import (
    ReflectionFeedbackContextError,
    build_reflection_feedback_context_facts,
)
from event_trader.reflection.learning_lifecycle import apply_reflection_learning
from event_trader.reflection.market_context_usage import (
    read_analysis_outcome_receipts,
    read_checker_decision_receipts,
)
from event_trader.reflection.review_loop import ReflectionHeartbeatReceipt
from event_trader.reflection.review_writer import (
    write_open_position_horizon_review,
)
from event_trader.reflection.view_contracts import OpenPositionReflectionContext
from event_trader.replay.admissibility import validate_replay_admission_request
from event_trader.replay.build import (
    ReplayBuildInput,
    ReplayBuildOutput,
    build_replay_input,
    write_replay_input,
)
from event_trader.replay.checkpoint import (
    ReplayEventCheckpoint,
)
from event_trader.replay.runner import ReplayStepReceipt
from event_trader.replay.smoke_runtime import (
    deterministic_replay_smoke_analysis_callback,
    deterministic_replay_smoke_checker_policy,
    per_event_passthrough_checker_policy,
    timebatch_passthrough_checker_policy,
)
from event_trader.replay.timestamps import replay_ts_event
from event_trader.source_policy import (
    ensure_source_classification_labels,
    source_classification_counts,
)
from event_trader.storage import WorkspaceLayout
from event_trader.validation import (
    ValidationReturnsError,
    calculate_open_episode_terminal_mark,
    calculate_window_performance,
    load_target_episode_state,
    read_state_changes,
)
from event_trader.validation.execution_linkage import execution_lookup_ids_for_state_changes
from event_trader.validation.market_data import ApiStocksMarketDataPort
from event_trader.validation.portfolio_feedback import resolve_annual_periods
from event_trader.workspace import bootstrap_workspace

_TIMESTAMP_FIELDS = frozenset(
    {
        "published_at",
        "captured_at",
        "updated_at",
        "released_at",
        "provided_at",
        "visible_at",
        "discovered_at",
    }
)
_DEFAULT_MARKET_DATA_API_KEY_ENV = "STOCK_BARS_API_KEY"


class ReplayRuntimeToolError(RuntimeError):
    """Raised when the replay runtime workflow cannot complete deterministically."""


@dataclass(frozen=True, slots=True)
class _ReplayBackfillWebSearchConfig:
    search_intent: str
    prompt_profile_id: str
    control_language: str
    retrieval_languages: tuple[str, ...]
    slice_hours: int
    vendor_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key_env: str
    llm_base_url: str
    llm_reasoning_effort: str | None
    serper_api_key_env: str
    serper_base_url: str
    jina_api_key_env: str
    jina_base_url: str
    summary_llm_api_key_env: str
    summary_llm_base_url: str
    summary_llm_model_name: str
    native_web_search_api_key_env: str
    native_web_search_base_url: str
    native_web_search_model: str
    native_web_search_tool_type: str
    anthropic_web_search_api_key_env: str
    anthropic_web_search_base_url: str
    anthropic_web_search_model: str
    anthropic_web_search_tool_type: str
    anthropic_web_search_max_uses: int
    anthropic_web_search_version: str
    acquisition_tool_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ReplayBackfillMarketNewsConfig:
    symbols: tuple[str, ...]
    labels: tuple[str, ...]
    slice_hours: int
    base_url: str
    api_key_env: str
    limit: int
    include_content: bool
    exclude_contentless: bool


def _datetime_to_unix_nanos(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1_000_000_000)


@dataclass(frozen=True, slots=True)
class _ReplayDatasetRow:
    ingress_input: ReplayIngressInput
    labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ReplayRunStepStats:
    completed_count: int
    skipped_count: int
    failed_count: int
    checkpoint_path: Path
    quarantined_count: int = 0
    quarantine_path: Path | None = None


@dataclass(frozen=True, slots=True)
class _ReplayMarketPrefetchSetup:
    market_data_provider_override: MarketBarsProvider | None
    receipt: ReplayMarketPrefetchReceipt | None
    market_data_store_root: Path | None = None


type _ReplayPrimaryResearchMode = Literal[
    "production",
    "deterministic_smoke",
    "per_event_passthrough",
    "timebatch_passthrough",
]


@dataclass(slots=True)
class _ReplayReflectionCycleDriver:
    clock: TestClock
    drain_cycle: Callable[[], ReflectionHeartbeatReceipt]
    next_due_at: datetime
    step: timedelta
    _receipts: list[ReflectionHeartbeatReceipt] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.clock, TestClock):
            raise ReplayRuntimeToolError("reflection runtime clock must be a TestClock.")
        if self.step <= timedelta(0):
            raise ReplayRuntimeToolError("reflection_step_hours must be greater than zero.")
        self.next_due_at = self.next_due_at.astimezone(UTC)
        if self._receipts is None:
            self._receipts = []

    @property
    def receipts(self) -> tuple[ReflectionHeartbeatReceipt, ...]:
        if self._receipts is None:
            raise ReplayRuntimeToolError("Replay reflection receipts were not initialized.")
        return tuple(self._receipts)

    def advance_to(self, replay_at: datetime) -> None:
        current = replay_at.astimezone(UTC)
        while self.next_due_at <= current:
            self._drain_due_cycle()

    def advance_before(self, replay_at: datetime) -> None:
        current = replay_at.astimezone(UTC)
        while self.next_due_at < current:
            self._drain_due_cycle()

    def advance_after_event(self, replay_at: datetime) -> None:
        current = replay_at.astimezone(UTC)
        self.advance_to(current)
        if not self._receipts:
            self._drain_cycle_at(current)
            return
        if self._receipts[-1].checked_at != current:
            self._drain_cycle_at(current)

    def fast_forward_after(self, replay_at: datetime) -> None:
        current = replay_at.astimezone(UTC)
        while self.next_due_at <= current:
            self.next_due_at += self.step

    def advance_after_window(
        self,
        *,
        window_end: datetime,
        reflection_end: datetime,
    ) -> None:
        if reflection_end <= window_end:
            return
        boundary = window_end.astimezone(UTC)
        while self.next_due_at <= boundary:
            self.next_due_at += self.step
        self.advance_to(reflection_end)

    def _drain_due_cycle(self) -> None:
        self._drain_cycle_at(self.next_due_at)
        self.next_due_at += self.step

    def _drain_cycle_at(self, checked_at: datetime) -> None:
        if self._receipts is None:
            raise ReplayRuntimeToolError("Replay reflection receipts were not initialized.")
        self.clock.set_time(_datetime_to_unix_nanos(checked_at))
        receipt = self.drain_cycle()
        self._receipts.append(receipt)
        if receipt.status in {"failed", "dependency_missing"}:
            raise ReplayRuntimeToolError(
                "Reflection cycle failed at "
                f"{receipt.checked_at.isoformat()}: {receipt.failure_reason}"
            )


@dataclass(frozen=True, slots=True)
class _ReplayOpenPositionTerminalSummary:
    episode_id: str
    direction: str
    replay_end_at: datetime
    entry_bar_start_at: datetime
    mark_bar_start_at: datetime
    entry_price: float
    mark_price: float
    target_weight: float
    underlying_return_pct: float
    strategy_return_pct: float
    terminal_review_page_path: str | None = None
    learning_primary_outcome: str | None = None
    learning_action_count: int = 0
    learning_written_count: int = 0
    learning_failed_count: int = 0


@dataclass(frozen=True, slots=True)
class _ValidationWindowArtifactPaths:
    summary_path: Path
    bar_returns_path: Path


@dataclass(slots=True)
class _ReplayPipelineStageTracker:
    current_event_id: str | None = None
    current_stage: str = "not_started"

    def start_event(self, event_id: str) -> None:
        self.current_event_id = event_id
        self.current_stage = "admission"

    def observe(self, event_id: str, stage: str) -> None:
        if self.current_event_id == event_id:
            self.current_stage = stage

    def stage_for(self, event_id: str) -> str:
        if self.current_event_id != event_id:
            return "pipeline"
        return self.current_stage


class _NoopOutcomeContextPort(OutcomeContextPort):
    def read_outcome_context(
        self,
        anchor: ReviewAnchorIdentity,
        coverage: ReviewCoverage,
    ) -> OutcomeContextPacket:
        _ = (anchor, coverage)
        raise AssertionError(
            "No shared-anchor reflection context should be requested during target-only replay."
        )


def _unexpected_shared_review(
    _context: ReflectionLedgerContext,
) -> NoReturn:
    raise ReplayRuntimeToolError(
        "Replay runtime target reflection must not evaluate shared reflection anchors."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m event_trader.tools.replay_runtime",
        description="Build replay input and run replay runtime workflows.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    pm_review_preflight_parser = subparsers.add_parser(
        "pm-review-preflight",
        help="Validate PMReview runtime config/workspace readiness without replay.",
    )
    pm_review_preflight_parser.add_argument(
        "--config",
        default="config/kernel.replay.gold.toml",
        help="Replay kernel config path.",
    )
    pm_review_preflight_parser.add_argument(
        "--target-key",
        action="append",
        required=True,
        help="Target key to validate. May be passed multiple times.",
    )
    pm_review_readiness_parser = subparsers.add_parser(
        "pm-review-readiness-report",
        help="Print a read-only PMReview runtime readiness/lifecycle report.",
    )
    pm_review_readiness_parser.add_argument(
        "--config",
        default="config/kernel.replay.gold.toml",
        help="Replay kernel config path.",
    )
    pm_review_readiness_parser.add_argument(
        "--target-key",
        action="append",
        required=True,
        help="Target key to report. May be passed multiple times.",
    )
    pm_review_readiness_parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="Report format. Defaults to json.",
    )

    backfill_parser = subparsers.add_parser(
        "backfill-web-search",
        help="Backfill archived web-search source material for the replay window.",
    )
    backfill_parser.add_argument(
        "--config",
        default="config/kernel.replay.gold.toml",
        help="Kernel config path with replay_build and replay_backfill settings.",
    )
    backfill_parser.add_argument(
        "--window-start",
        help="Optional backfill window start. Accepts YYYY-MM-DD or ISO8601 datetime.",
    )
    backfill_parser.add_argument(
        "--window-end",
        help="Optional backfill window end. Accepts YYYY-MM-DD or ISO8601 datetime.",
    )

    market_news_backfill_parser = subparsers.add_parser(
        "backfill-market-news",
        help="Backfill archived structured market-news material for the replay window.",
    )
    market_news_backfill_parser.add_argument(
        "--config",
        default="config/kernel.replay.gold.toml",
        help="Kernel config path with replay_build and replay_backfill settings.",
    )
    market_news_backfill_parser.add_argument(
        "--window-start",
        help="Optional backfill window start. Accepts YYYY-MM-DD or ISO8601 datetime.",
    )
    market_news_backfill_parser.add_argument(
        "--window-end",
        help="Optional backfill window end. Accepts YYYY-MM-DD or ISO8601 datetime.",
    )

    build_parser = subparsers.add_parser(
        "build",
        help="Build one replay-ready dataset from archived sources and manual inputs.",
    )
    build_parser.add_argument(
        "--config",
        default="config/kernel.replay.gold.toml",
        help="Kernel config path used to resolve the workspace root.",
    )
    build_parser.add_argument(
        "--build-config",
        help=(
            "Optional TOML build spec for replay-input preparation. "
            "Defaults to [replay_build] in --config when present."
        ),
    )
    build_parser.add_argument(
        "--target-key",
        help="Replay target key. Defaults to gold.",
    )
    build_parser.add_argument(
        "--window-start",
        help="Build window start. Accepts YYYY-MM-DD or ISO8601 datetime.",
    )
    build_parser.add_argument(
        "--window-end",
        help="Build window end. Accepts YYYY-MM-DD or ISO8601 datetime.",
    )
    build_parser.add_argument(
        "--manual-input",
        help="Optional manual historical JSONL file.",
    )
    build_parser.add_argument(
        "--include-web-search",
        action="store_true",
        default=None,
        help="Include archived web-search rows from the workspace source archive.",
    )
    build_parser.add_argument(
        "--include-market-news",
        action="store_true",
        default=None,
        help="Include archived market-news rows from the workspace source archive.",
    )
    build_parser.add_argument(
        "--output",
        help="Optional explicit output JSONL path.",
    )
    build_parser.add_argument(
        "--write-report",
        action="store_true",
        default=None,
        help="Write a narrow markdown build report next to the output dataset.",
    )
    build_parser.add_argument(
        "--no-write-report",
        action="store_false",
        dest="write_report",
        help="Do not write the build report, even if the TOML config enables it.",
    )
    build_parser.set_defaults(
        include_market_news=None,
        include_web_search=None,
        write_report=None,
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Run one replay dataset through analysis, validation, and reflection.",
    )
    run_parser.add_argument(
        "--config",
        default="config/kernel.replay.gold.toml",
        help="Replay kernel config path.",
    )
    run_parser.add_argument(
        "--dataset",
        help="UTF-8 JSONL replay dataset path.",
    )
    run_parser.add_argument(
        "--input",
        dest="input_dataset",
        help="Alias for --dataset.",
    )
    run_parser.add_argument(
        "--run-id",
        help="Replay run id used for checkpoints and prefetched market data.",
    )
    run_parser.add_argument(
        "--auto-repair-failed-checkpoint",
        action="store_true",
        help=(
            "Automatically apply the existing failed-event repair plan when "
            "an unfinished replay checkpoint is found, then continue."
        ),
    )
    run_parser.add_argument(
        "--log-skipped-checkpoints",
        action="store_true",
        help=(
            "Print one line for each replay event skipped because its checkpoint "
            "is already completed. Defaults to summary-only skip reporting."
        ),
    )
    run_parser.add_argument(
        "--target-key",
        default="gold",
        help="Replay target key. Defaults to gold.",
    )
    run_parser.add_argument(
        "--window-start",
        required=True,
        help="Replay window start. Accepts YYYY-MM-DD or ISO8601 datetime.",
    )
    run_parser.add_argument(
        "--window-end",
        required=True,
        help="Replay window end. Accepts YYYY-MM-DD or ISO8601 datetime.",
    )
    run_parser.add_argument(
        "--reflection-end",
        help=(
            "Optional target-reflection cutoff. Defaults to window-end. "
            "Set this later than window-end if you want 72h/168h reviews for late episodes."
        ),
    )
    run_parser.add_argument(
        "--reflection-step-hours",
        type=int,
        default=1,
        help="Replay reflection cycle cadence in hours. Defaults to 1.",
    )
    run_parser.add_argument(
        "--market-symbol",
        default=None,
        help=(
            "Validation market symbol used for target reflection. "
            "Defaults to validation.market_mappings.<target>.market_symbol."
        ),
    )
    run_parser.add_argument(
        "--market-session",
        choices=("continuous", "exchange_session"),
        default=None,
        help=(
            "Validation market session. Defaults to "
            "validation.market_mappings.<target>.market_session."
        ),
    )
    run_parser.add_argument(
        "--exchange",
        default=None,
        help=(
            "Exchange identifier for exchange-session symbols. Defaults to "
            "validation.market_mappings.<target>.exchange."
        ),
    )
    run_parser.add_argument(
        "--exchange-session-scope",
        choices=("regular", "extended"),
        default=None,
        help=(
            "Exchange-session scope for validation market data. Defaults to "
            "validation.market_mappings.<target>.exchange_session_scope."
        ),
    )
    run_parser.add_argument(
        "--bar-granularity",
        default=None,
        help=(
            "Validation bar granularity. Defaults to "
            "validation.market_mappings.<target>.bar_granularity."
        ),
    )
    run_parser.add_argument(
        "--market-data-base-url",
        default=None,
        help=(
            "Optional override for the validation stock-bars API base URL. "
            "Defaults to validation.market_data.base_url from config."
        ),
    )
    run_parser.add_argument(
        "--market-data-api-key-env",
        default=None,
        help=(
            "Optional override for the validation stock-bars API key environment "
            "variable name. Defaults to validation.market_data.api_key_env from config."
        ),
    )
    run_parser.add_argument(
        "--market-data-timeout-seconds",
        type=float,
        default=None,
        help=(
            "Validation market-data timeout in seconds. Defaults to "
            "validation.market_data.timeout_seconds."
        ),
    )
    run_parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Optional additional admission label. May be passed multiple times.",
    )
    run_parser.add_argument(
        "--primary-research-mode",
        choices=(
            "production",
            "deterministic_smoke",
            "per_event_passthrough",
            "timebatch_passthrough",
        ),
        default="production",
        help=(
            "Primary research execution mode for replay runs. "
            "Use deterministic_smoke to exercise the existing replay runtime "
            "without external LLM calls; use per_event_passthrough or "
            "timebatch_passthrough to bypass checker LLM/filtering while keeping "
            "production analysis."
        ),
    )

    validation_window_parser = subparsers.add_parser(
        "validation-window",
        help="Read canonical validation state over a window and write performance artifacts.",
    )
    validation_window_parser.add_argument(
        "--config",
        default="config/kernel.replay.gold.toml",
        help="Replay kernel config path.",
    )
    validation_window_parser.add_argument(
        "--target-key",
        default="gold",
        help="Validation target key. Defaults to gold.",
    )
    validation_window_parser.add_argument(
        "--window-start",
        required=True,
        help="Validation window start. Accepts YYYY-MM-DD or ISO8601 datetime.",
    )
    validation_window_parser.add_argument(
        "--window-end",
        required=True,
        help="Validation window end. Accepts YYYY-MM-DD or ISO8601 datetime.",
    )
    validation_window_parser.add_argument(
        "--market-symbol",
        default=None,
        help=(
            "Validation market symbol used for return calculation. "
            "Defaults to validation.market_mappings.<target>.market_symbol."
        ),
    )
    validation_window_parser.add_argument(
        "--market-session",
        choices=("continuous", "exchange_session"),
        default=None,
        help=(
            "Validation market session. Defaults to "
            "validation.market_mappings.<target>.market_session."
        ),
    )
    validation_window_parser.add_argument(
        "--exchange",
        default=None,
        help=(
            "Exchange identifier for exchange-session symbols. Defaults to "
            "validation.market_mappings.<target>.exchange."
        ),
    )
    validation_window_parser.add_argument(
        "--exchange-session-scope",
        choices=("regular", "extended"),
        default=None,
        help=(
            "Exchange-session scope for validation market data. Defaults to "
            "validation.market_mappings.<target>.exchange_session_scope."
        ),
    )
    validation_window_parser.add_argument(
        "--bar-granularity",
        default=None,
        help=(
            "Validation bar granularity. Defaults to "
            "validation.market_mappings.<target>.bar_granularity."
        ),
    )
    validation_window_parser.add_argument(
        "--market-data-base-url",
        default=None,
        help=(
            "Optional override for the validation stock-bars API base URL. "
            "Defaults to validation.market_data.base_url from config."
        ),
    )
    validation_window_parser.add_argument(
        "--market-data-api-key-env",
        default=None,
        help=(
            "Optional override for the validation stock-bars API key environment "
            "variable name. Defaults to validation.market_data.api_key_env from config."
        ),
    )
    validation_window_parser.add_argument(
        "--market-data-timeout-seconds",
        type=float,
        default=None,
        help=(
            "Validation market-data timeout in seconds. Defaults to "
            "validation.market_data.timeout_seconds."
        ),
    )
    validation_window_parser.add_argument(
        "--annual-periods",
        type=float,
        default=None,
        help=(
            "Optional annualization factor for Sharpe. "
            "Defaults to an inferred value from market session and bar granularity."
        ),
    )
    validation_window_parser.add_argument(
        "--rolling-sharpe-bars",
        type=int,
        default=20,
        help="Rolling Sharpe lookback in bars. Defaults to 20.",
    )
    acceptance_parser = subparsers.add_parser(
        "acceptance-report",
        help="Write a deterministic replay acceptance evidence pack.",
    )
    acceptance_parser.add_argument(
        "--config",
        required=True,
        help="Replay kernel config path.",
    )
    acceptance_parser.add_argument(
        "--run-id",
        required=True,
        help="Completed replay run id to audit.",
    )
    acceptance_parser.add_argument(
        "--target-key",
        required=True,
        help="Replay target key to audit.",
    )
    acceptance_parser.add_argument(
        "--event-id",
        required=True,
        help="Source event id anchoring the trace bundle.",
    )
    acceptance_parser.add_argument(
        "--view-episode-id",
        required=True,
        help="View episode id anchoring validation and reflection artifacts.",
    )
    acceptance_parser.add_argument(
        "--decision-episode-id",
        required=True,
        help="Decision episode id anchoring decision-stage artifacts.",
    )
    acceptance_parser.add_argument(
        "--commit-hash",
        required=True,
        help="Pinned git commit hash used for the replay run.",
    )
    acceptance_parser.add_argument(
        "--replay-command",
        required=True,
        help="Exact replay command used to produce the audited workspace.",
    )

    target_report_parser = subparsers.add_parser(
        "target-report",
        help="Write a deterministic full target replay audit report.",
    )
    _add_release_readiness_common_report_args(target_report_parser)

    release_readiness_parser = subparsers.add_parser(
        "release-readiness-report",
        help="Write the final Release Readiness manifest from audit reports.",
    )
    release_readiness_parser.add_argument(
        "--config",
        required=True,
        help="Replay kernel config path.",
    )
    release_readiness_parser.add_argument(
        "--run-id",
        required=True,
        help="Completed replay run id to aggregate.",
    )
    release_readiness_parser.add_argument(
        "--target-key",
        required=True,
        help="Replay target key to aggregate.",
    )
    release_readiness_parser.add_argument(
        "--view-episode-id",
        required=True,
        help="Live smoke view episode id to aggregate.",
    )
    release_readiness_parser.add_argument(
        "--commit-hash",
        required=True,
        help="Pinned git commit hash used for release readiness evidence.",
    )
    release_readiness_parser.add_argument(
        "--replay-command",
        required=True,
        help="Exact replay command used to produce the audited workspace.",
    )
    release_readiness_parser.add_argument(
        "--git-status-summary",
        default="",
        help="Operator-supplied git status summary recorded in the manifest.",
    )
    cost_report_parser = subparsers.add_parser(
        "cost-report",
        help="Write a replay wall-time and LLM-token cost report.",
    )
    cost_report_parser.add_argument(
        "--config",
        required=True,
        help="Replay kernel config path.",
    )
    cost_report_parser.add_argument(
        "--run-id",
        required=True,
        help="Replay run id to summarize.",
    )
    cost_report_parser.add_argument(
        "--target-key",
        required=True,
        help="Replay target key to summarize.",
    )
    cost_report_parser.add_argument(
        "--window-start",
        required=True,
        help="Replay business window start as YYYY-MM-DD or ISO8601 datetime.",
    )
    cost_report_parser.add_argument(
        "--window-end",
        required=True,
        help="Replay business window end as YYYY-MM-DD or ISO8601 datetime.",
    )
    cost_report_parser.add_argument(
        "--pm-review-log-dir",
        default=".local/mirothinker-pm-review-logs",
        help="MiroThinker PMReview task log directory.",
    )
    cost_report_parser.add_argument(
        "--reflection-log-dir",
        default=".local/mirothinker-reflection-logs",
        help="MiroThinker reflection task log directory.",
    )
    return parser


def _add_release_readiness_common_report_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        required=True,
        help="Replay kernel config path.",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Completed replay run id to audit.",
    )
    parser.add_argument(
        "--target-key",
        required=True,
        help="Replay target key to audit.",
    )
    parser.add_argument(
        "--event-id",
        required=True,
        help="Source event id anchoring the trace bundle.",
    )
    parser.add_argument(
        "--view-episode-id",
        required=True,
        help="View episode id anchoring validation and reflection artifacts.",
    )
    parser.add_argument(
        "--decision-episode-id",
        required=True,
        help="Decision episode id anchoring decision-stage artifacts.",
    )
    parser.add_argument(
        "--commit-hash",
        required=True,
        help="Pinned git commit hash used for the replay run.",
    )
    parser.add_argument(
        "--replay-command",
        required=True,
        help="Exact replay command used to produce the audited workspace.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "backfill-web-search":
            return _run_backfill_web_search_command(args)
        if args.command == "pm-review-preflight":
            return _run_pm_review_preflight_command(args)
        if args.command == "pm-review-readiness-report":
            return _run_pm_review_readiness_report_command(args)
        if args.command == "backfill-market-news":
            return _run_backfill_market_news_command(args)
        if args.command == "build":
            return _run_build_command(args)
        if args.command == "validation-window":
            return _run_validation_window_command(args)
        if args.command == "acceptance-report":
            return _run_acceptance_report_command(args)
        if args.command == "target-report":
            return _run_target_report_command(args)
        if args.command == "release-readiness-report":
            return _run_release_readiness_report_command(args)
        if args.command == "cost-report":
            return _run_cost_report_command(args)
        return _run_replay_command(args)
    except Exception as exc:
        print(f"Replay runtime failed: {exc}", file=sys.stderr)
        return 1


def _run_pm_review_preflight_command(args: argparse.Namespace) -> int:
    config = load_kernel_config(args.config)
    prepared_workspace = bootstrap_workspace(config.workspace_root)
    if prepared_workspace.layout is None:
        raise ReplayRuntimeToolError("PMReview preflight requires a workspace layout.")
    receipt = validate_pm_review_runtime_preflight(
        config=config,
        layout=prepared_workspace.layout,
        target_keys=tuple(args.target_key),
    )
    print(
        "pm review preflight: "
        f"enabled={receipt.enabled} "
        f"execute_approved={receipt.execute_approved} "
        f"workspace_ready_checked={receipt.workspace_ready_checked} "
        f"runner_checked={receipt.runner_checked} "
        f"targets={','.join(receipt.target_keys)}"
    )
    return 0


def _ensure_replay_pm_review_workspace_ready(
    *,
    config_path: Path,
    target_key: str,
    migrated_at: datetime,
) -> bool:
    config = load_kernel_config(config_path)
    prepared_workspace = bootstrap_workspace(config.workspace_root)
    if prepared_workspace.layout is None:
        raise ReplayRuntimeToolError("PMReview preflight requires a workspace layout.")
    try:
        validate_pm_review_runtime_preflight(
            config=config,
            layout=prepared_workspace.layout,
            target_keys=(target_key,),
        )
        return False
    except CompositionError as exc:
        if not _is_fresh_pm_review_workspace_error(exc):
            raise
    prepare_pm_review_workspace(
        layout=prepared_workspace.layout,
        target_keys=(target_key,),
        migrated_at=migrated_at,
    )
    validate_pm_review_runtime_preflight(
        config=config,
        layout=prepared_workspace.layout,
        target_keys=(target_key,),
    )
    return True


def _is_fresh_pm_review_workspace_error(exc: CompositionError) -> bool:
    message = str(exc)
    return (
        "runtime schema marker is missing" in message
        or "cutover baseline portfolio state is missing" in message
    )


def _run_pm_review_readiness_report_command(args: argparse.Namespace) -> int:
    config = load_kernel_config(args.config)
    prepared_workspace = bootstrap_workspace(config.workspace_root)
    if prepared_workspace.layout is None:
        raise ReplayRuntimeToolError("PMReview readiness report requires a workspace layout.")
    report = build_pm_review_readiness_report(
        layout=prepared_workspace.layout,
        target_keys=tuple(args.target_key),
    )
    if args.format == "markdown":
        print(render_pm_review_readiness_markdown(report))
    else:
        print(json.dumps(report.to_json_payload(), sort_keys=True))
    return 0


def _run_build_command(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve(strict=False)
    build_config_path = _resolve_build_config_path(
        config_path=config_path,
        build_config_arg=args.build_config,
    )
    build_config = _load_build_config(
        None if build_config_path is None else str(build_config_path)
    )
    target_key_value = _build_arg_value(
        cli_value=args.target_key,
        config_value=build_config.get("target_key"),
        default="gold",
    )
    window_start_raw = _build_arg_value(
        cli_value=args.window_start,
        config_value=build_config.get("window_start"),
    )
    window_end_raw = _build_arg_value(
        cli_value=args.window_end,
        config_value=build_config.get("window_end"),
    )
    manual_input_raw = _build_arg_value(
        cli_value=args.manual_input,
        config_value=build_config.get("manual_input"),
    )
    output_raw = _build_arg_value(
        cli_value=args.output,
        config_value=build_config.get("output"),
    )
    include_web_search = _build_bool_value(
        cli_value=args.include_web_search,
        config_value=build_config.get("include_web_search"),
        default=False,
        field_name="include_web_search",
    )
    include_market_news = _build_bool_value(
        cli_value=args.include_market_news,
        config_value=build_config.get("include_market_news"),
        default=False,
        field_name="include_market_news",
    )
    write_report = _build_bool_value(
        cli_value=args.write_report,
        config_value=build_config.get("write_report"),
        default=False,
        field_name="write_report",
    )

    if target_key_value is None:
        raise ReplayRuntimeToolError("Build requires target_key from CLI or TOML config.")
    target_key = validate_target_key(
        target_key_value,
        error_type=ReplayRuntimeToolError,
    )
    if window_start_raw is None:
        raise ReplayRuntimeToolError("Build requires window_start from CLI or TOML config.")
    if window_end_raw is None:
        raise ReplayRuntimeToolError("Build requires window_end from CLI or TOML config.")
    window_start = _parse_window_boundary(window_start_raw, end_of_day=False)
    window_end = _parse_window_boundary(window_end_raw, end_of_day=True)
    manual_input_path = (
        None
        if manual_input_raw is None
        else _resolve_replay_build_path(
            manual_input_raw,
            config_path=(
                None if args.manual_input is not None else build_config_path
            ),
        )
    )
    output_path = (
        None
        if output_raw is None
        else _resolve_replay_build_path(
            output_raw,
            config_path=(
                None if args.output is not None else build_config_path
            ),
        )
    )

    config = load_kernel_config(config_path)
    workspace = bootstrap_workspace(config.workspace_root)
    layout = workspace.layout
    if layout is None:
        raise ReplayRuntimeToolError("Workspace layout is required for replay build.")

    build_input = ReplayBuildInput(
        target_key=target_key,
        start_at=window_start,
        end_at=window_end,
        include_archived_web_search=include_web_search,
        include_archived_market_news=include_market_news,
        manual_input_path=manual_input_path,
    )
    build_output = build_replay_input(layout, build_input)
    artifacts = write_replay_input(
        layout,
        build_output,
        output_path=output_path,
        write_report=write_report,
    )
    _print_build_summary(
        layout_root=layout.root,
        build_output=build_output,
        output_path=artifacts.output_path,
        report_path=artifacts.report_path,
        config_path=config.config_path,
        market_mapping=(
            None
            if config.validation is None
            else config.validation.market_mappings.get(target_key)
        ),
    )
    return 0


def _run_backfill_web_search_command(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve(strict=False)
    kernel_config = load_kernel_config(config_path)
    workspace = bootstrap_workspace(kernel_config.workspace_root)
    layout = workspace.layout
    if layout is None:
        raise ReplayRuntimeToolError("Workspace layout is required for replay backfill.")

    build_config = _load_build_config(str(config_path))
    backfill_config = _load_web_search_backfill_config(config_path)
    target_key_raw = _build_arg_value(
        cli_value=None,
        config_value=build_config.get("target_key"),
        default="gold",
    )
    window_start_raw = _build_arg_value(
        cli_value=args.window_start,
        config_value=build_config.get("window_start"),
    )
    window_end_raw = _build_arg_value(
        cli_value=args.window_end,
        config_value=build_config.get("window_end"),
    )
    if window_start_raw is None:
        raise ReplayRuntimeToolError(
            "Backfill requires window_start from CLI or replay_build.window_start in TOML."
        )
    if window_end_raw is None:
        raise ReplayRuntimeToolError(
            "Backfill requires window_end from CLI or replay_build.window_end in TOML."
        )
    if target_key_raw is None:
        raise ReplayRuntimeToolError(
            "Backfill requires target_key from CLI or replay_build.target_key in TOML."
        )
    target_key = validate_target_key(
        target_key_raw,
        error_type=ReplayRuntimeToolError,
    )
    window_start = _parse_window_boundary(window_start_raw, end_of_day=False)
    window_end = _parse_window_boundary(window_end_raw, end_of_day=True)

    runner_config = MiroThinkerSearchRuntimeConfig(
        vendor_root=backfill_config.vendor_root,
        workspace_root=layout.root,
        log_dir=backfill_config.log_dir,
        llm_provider=backfill_config.llm_provider,
        llm_model_name=backfill_config.llm_model_name,
        llm_api_key=_require_env_value(
            backfill_config.llm_api_key_env,
            field_name="replay_backfill.web_search.llm_api_key_env",
        ),
        llm_base_url=backfill_config.llm_base_url,
        llm_max_context_length=65_536,
        llm_reasoning_effort=backfill_config.llm_reasoning_effort,
        serper_api_key=_require_env_value(
            backfill_config.serper_api_key_env,
            field_name="replay_backfill.web_search.serper_api_key_env",
            required="search_and_scrape_webpage"
            in backfill_config.acquisition_tool_names,
        ),
        serper_base_url=backfill_config.serper_base_url,
        jina_api_key=_require_env_value(
            backfill_config.jina_api_key_env,
            field_name="replay_backfill.web_search.jina_api_key_env",
            required="jina_scrape_llm_summary"
            in backfill_config.acquisition_tool_names,
        ),
        jina_base_url=backfill_config.jina_base_url,
        summary_llm_api_key=_require_env_value(
            backfill_config.summary_llm_api_key_env,
            field_name="replay_backfill.web_search.summary_llm_api_key_env",
            required="jina_scrape_llm_summary"
            in backfill_config.acquisition_tool_names,
        ),
        summary_llm_base_url=backfill_config.summary_llm_base_url,
        summary_llm_model_name=backfill_config.summary_llm_model_name,
        native_web_search_api_key=_require_env_value(
            backfill_config.native_web_search_api_key_env,
            field_name="replay_backfill.web_search.native_web_search_api_key_env",
            required="event_trader_openai_web_search"
            in backfill_config.acquisition_tool_names,
        ),
        native_web_search_base_url=backfill_config.native_web_search_base_url,
        native_web_search_model=backfill_config.native_web_search_model,
        native_web_search_tool_type=backfill_config.native_web_search_tool_type,
        anthropic_web_search_api_key=_require_env_value(
            backfill_config.anthropic_web_search_api_key_env,
            field_name="replay_backfill.web_search.anthropic_web_search_api_key_env",
            required="event_trader_anthropic_web_search"
            in backfill_config.acquisition_tool_names,
        ),
        anthropic_web_search_base_url=backfill_config.anthropic_web_search_base_url,
        anthropic_web_search_model=backfill_config.anthropic_web_search_model,
        anthropic_web_search_tool_type=backfill_config.anthropic_web_search_tool_type,
        anthropic_web_search_max_uses=backfill_config.anthropic_web_search_max_uses,
        anthropic_web_search_version=backfill_config.anthropic_web_search_version,
        acquisition_tool_names=backfill_config.acquisition_tool_names,
    )
    run_search_agent = build_mirothinker_search_collection_runner(config=runner_config)
    receipt = backfill_historical_web_search(
        layout=layout,
        target_key=target_key,
        search_intent=backfill_config.search_intent,
        prompt_profile_id=backfill_config.prompt_profile_id,
        control_language=backfill_config.control_language,
        retrieval_languages=backfill_config.retrieval_languages,
        window_start=window_start,
        window_end=window_end,
        slice_hours=backfill_config.slice_hours,
        run_search_agent=run_search_agent,
    )
    _print_backfill_summary(
        layout_root=layout.root,
        receipt=receipt,
        config_path=config_path,
    )
    return 0


def _run_backfill_market_news_command(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve(strict=False)
    kernel_config = load_kernel_config(config_path)
    workspace = bootstrap_workspace(kernel_config.workspace_root)
    layout = workspace.layout
    if layout is None:
        raise ReplayRuntimeToolError("Workspace layout is required for replay backfill.")

    build_config = _load_build_config(str(config_path))
    backfill_config = _load_market_news_backfill_config(config_path)
    target_key_raw = _build_arg_value(
        cli_value=None,
        config_value=build_config.get("target_key"),
        default="gold",
    )
    window_start_raw = _build_arg_value(
        cli_value=args.window_start,
        config_value=build_config.get("window_start"),
    )
    window_end_raw = _build_arg_value(
        cli_value=args.window_end,
        config_value=build_config.get("window_end"),
    )
    if window_start_raw is None:
        raise ReplayRuntimeToolError(
            "Backfill requires window_start from CLI or replay_build.window_start in TOML."
        )
    if window_end_raw is None:
        raise ReplayRuntimeToolError(
            "Backfill requires window_end from CLI or replay_build.window_end in TOML."
        )
    if target_key_raw is None:
        raise ReplayRuntimeToolError(
            "Backfill requires target_key from CLI or replay_build.target_key in TOML."
        )
    target_key = validate_target_key(
        target_key_raw,
        error_type=ReplayRuntimeToolError,
    )
    receipt = backfill_market_news(
        layout=layout,
        target_key=target_key,
        symbols=backfill_config.symbols,
        labels=backfill_config.labels,
        base_url=backfill_config.base_url,
        api_key=_require_env_value(
            backfill_config.api_key_env,
            field_name="replay_backfill.market_news.api_key_env",
        ),
        window_start=_parse_window_boundary(window_start_raw, end_of_day=False),
        window_end=_parse_window_boundary(window_end_raw, end_of_day=True),
        slice_hours=backfill_config.slice_hours,
        limit=backfill_config.limit,
        include_content=backfill_config.include_content,
        exclude_contentless=backfill_config.exclude_contentless,
    )
    _print_market_news_backfill_summary(
        layout_root=layout.root,
        receipt=receipt,
        config_path=config_path,
    )
    return 0


def _load_build_config(build_config_path: str | None) -> dict[str, object]:
    if build_config_path is None:
        return {}
    resolved_path = Path(build_config_path).expanduser().resolve(strict=False)
    if not resolved_path.exists() or not resolved_path.is_file():
        raise ReplayRuntimeToolError(f"Build config file does not exist: {resolved_path}")
    try:
        with resolved_path.open("rb") as handle:
            payload = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ReplayRuntimeToolError(
            f"Build config contains invalid TOML: {resolved_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise ReplayRuntimeToolError(f"Could not read build config {resolved_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReplayRuntimeToolError("Build config must decode to a TOML table.")
    if "replay_build" in payload:
        build_payload = payload["replay_build"]
        if not isinstance(build_payload, dict):
            raise ReplayRuntimeToolError("Build config field 'replay_build' must be a TOML table.")
        payload = build_payload
    allowed_fields = {
        "target_key",
        "window_start",
        "window_end",
        "manual_input",
        "include_web_search",
        "include_market_news",
        "output",
        "write_report",
    }
    extra_fields = sorted(set(payload) - allowed_fields)
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise ReplayRuntimeToolError(f"Unknown build config fields: {unknown}")
    return payload


def _resolve_build_config_path(
    *,
    config_path: Path,
    build_config_arg: str | None,
) -> Path | None:
    if build_config_arg is not None:
        return Path(build_config_arg).expanduser().resolve(strict=False)
    payload = _load_toml_payload(config_path)
    replay_build = payload.get("replay_build")
    if replay_build is None:
        return None
    if not isinstance(replay_build, dict):
        raise ReplayRuntimeToolError("Build config field 'replay_build' must be a TOML table.")
    return config_path


def _resolve_replay_build_path(
    raw_path: str,
    *,
    config_path: Path | None,
) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute() and config_path is not None:
        path = resolve_config_relative_path(path, config_path=config_path)
    return path.resolve(strict=False)


def _load_web_search_backfill_config(
    config_path: Path,
) -> _ReplayBackfillWebSearchConfig:
    resolved_path = config_path.expanduser().resolve(strict=False)
    top_level_payload = _load_toml_payload(resolved_path)
    replay_backfill = top_level_payload.get("replay_backfill")
    if not isinstance(replay_backfill, dict):
        raise ReplayRuntimeToolError(
            "Replay config must include a replay_backfill TOML table."
        )
    extra_fields = sorted(set(replay_backfill) - {"market_news", "web_search"})
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise ReplayRuntimeToolError(
            f"Unknown replay_backfill config fields: {unknown}"
        )
    raw_config = replay_backfill.get("web_search")
    if not isinstance(raw_config, dict):
        raise ReplayRuntimeToolError(
            "Replay config field 'replay_backfill.web_search' must be a TOML table."
        )
    required_fields = {
        "search_intent",
        "prompt_profile_id",
        "control_language",
        "retrieval_languages",
        "slice_hours",
        "vendor_root",
        "log_dir",
        "llm_provider",
        "llm_model_name",
        "llm_api_key_env",
        "llm_base_url",
    }
    optional_fields = {
        "acquisition_tool_names",
        "llm_reasoning_effort",
        "serper_api_key_env",
        "serper_base_url",
        "jina_api_key_env",
        "jina_base_url",
        "summary_llm_api_key_env",
        "summary_llm_base_url",
        "summary_llm_model_name",
        "native_web_search_api_key_env",
        "native_web_search_base_url",
        "native_web_search_model",
        "native_web_search_tool_type",
        "anthropic_web_search_api_key_env",
        "anthropic_web_search_base_url",
        "anthropic_web_search_model",
        "anthropic_web_search_tool_type",
        "anthropic_web_search_max_uses",
        "anthropic_web_search_version",
    }
    missing_fields = sorted(required_fields - set(raw_config))
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise ReplayRuntimeToolError(
            "Missing required replay_backfill.web_search config fields: "
            f"{missing}"
        )
    allowed_fields = required_fields | optional_fields
    extra_fields = sorted(set(raw_config) - allowed_fields)
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise ReplayRuntimeToolError(
            f"Unknown replay_backfill.web_search config fields: {unknown}"
        )
    acquisition_tool_names = _require_tool_name_tuple(
        raw_config.get(
            "acquisition_tool_names",
            ["search_and_scrape_webpage", "jina_scrape_llm_summary"],
        ),
        field_name="replay_backfill.web_search.acquisition_tool_names",
    )
    _require_enabled_tool_fields(
        raw_config=raw_config,
        acquisition_tool_names=acquisition_tool_names,
    )
    return _ReplayBackfillWebSearchConfig(
        search_intent=_require_non_blank_string(
            raw_config["search_intent"],
            field_name="replay_backfill.web_search.search_intent",
        ),
        prompt_profile_id=_require_non_blank_string(
            raw_config["prompt_profile_id"],
            field_name="replay_backfill.web_search.prompt_profile_id",
        ),
        control_language=_require_non_blank_string(
            raw_config["control_language"],
            field_name="replay_backfill.web_search.control_language",
        ),
        retrieval_languages=_require_non_blank_string_tuple(
            raw_config["retrieval_languages"],
            field_name="replay_backfill.web_search.retrieval_languages",
        ),
        slice_hours=_require_positive_int(
            raw_config["slice_hours"],
            field_name="replay_backfill.web_search.slice_hours",
        ),
        vendor_root=_resolve_config_path(
            raw_config["vendor_root"],
            config_path=resolved_path,
            field_name="replay_backfill.web_search.vendor_root",
        ),
        log_dir=_resolve_config_path(
            raw_config["log_dir"],
            config_path=resolved_path,
            field_name="replay_backfill.web_search.log_dir",
        ),
        llm_provider=_require_non_blank_string(
            raw_config["llm_provider"],
            field_name="replay_backfill.web_search.llm_provider",
        ),
        llm_model_name=_require_non_blank_string(
            raw_config["llm_model_name"],
            field_name="replay_backfill.web_search.llm_model_name",
        ),
        llm_api_key_env=_require_non_blank_string(
            raw_config["llm_api_key_env"],
            field_name="replay_backfill.web_search.llm_api_key_env",
        ),
        llm_base_url=_require_non_blank_string(
            raw_config["llm_base_url"],
            field_name="replay_backfill.web_search.llm_base_url",
        ),
        llm_reasoning_effort=normalize_mirothinker_reasoning_effort(
            raw_config.get("llm_reasoning_effort"),
            field_name="replay_backfill.web_search.llm_reasoning_effort",
            error_type=ReplayRuntimeToolError,
        ),
        serper_api_key_env=_require_optional_non_blank_string(
            raw_config.get("serper_api_key_env", ""),
            field_name="replay_backfill.web_search.serper_api_key_env",
        ),
        serper_base_url=_require_optional_non_blank_string(
            raw_config.get("serper_base_url", ""),
            field_name="replay_backfill.web_search.serper_base_url",
        ),
        jina_api_key_env=_require_optional_non_blank_string(
            raw_config.get("jina_api_key_env", ""),
            field_name="replay_backfill.web_search.jina_api_key_env",
        ),
        jina_base_url=_require_optional_non_blank_string(
            raw_config.get("jina_base_url", ""),
            field_name="replay_backfill.web_search.jina_base_url",
        ),
        summary_llm_api_key_env=_require_optional_non_blank_string(
            raw_config.get("summary_llm_api_key_env", ""),
            field_name="replay_backfill.web_search.summary_llm_api_key_env",
        ),
        summary_llm_base_url=_require_optional_non_blank_string(
            raw_config.get("summary_llm_base_url", ""),
            field_name="replay_backfill.web_search.summary_llm_base_url",
        ),
        summary_llm_model_name=_require_optional_non_blank_string(
            raw_config.get("summary_llm_model_name", ""),
            field_name="replay_backfill.web_search.summary_llm_model_name",
        ),
        native_web_search_api_key_env=_require_optional_non_blank_string(
            raw_config.get("native_web_search_api_key_env", ""),
            field_name="replay_backfill.web_search.native_web_search_api_key_env",
        ),
        native_web_search_base_url=_require_optional_non_blank_string(
            raw_config.get("native_web_search_base_url", ""),
            field_name="replay_backfill.web_search.native_web_search_base_url",
        ),
        native_web_search_model=_require_optional_non_blank_string(
            raw_config.get("native_web_search_model", ""),
            field_name="replay_backfill.web_search.native_web_search_model",
        ),
        native_web_search_tool_type=_require_native_web_search_tool_type(
            raw_config.get("native_web_search_tool_type", "web_search"),
            field_name="replay_backfill.web_search.native_web_search_tool_type",
        ),
        anthropic_web_search_api_key_env=_require_optional_non_blank_string(
            raw_config.get("anthropic_web_search_api_key_env", ""),
            field_name="replay_backfill.web_search.anthropic_web_search_api_key_env",
        ),
        anthropic_web_search_base_url=_require_optional_non_blank_string(
            raw_config.get("anthropic_web_search_base_url", ""),
            field_name="replay_backfill.web_search.anthropic_web_search_base_url",
        ),
        anthropic_web_search_model=_require_optional_non_blank_string(
            raw_config.get("anthropic_web_search_model", ""),
            field_name="replay_backfill.web_search.anthropic_web_search_model",
        ),
        anthropic_web_search_tool_type=_require_optional_non_blank_string(
            raw_config.get("anthropic_web_search_tool_type", "web_search_20250305"),
            field_name="replay_backfill.web_search.anthropic_web_search_tool_type",
        ),
        anthropic_web_search_max_uses=_require_optional_positive_int(
            raw_config.get("anthropic_web_search_max_uses", 5),
            field_name="replay_backfill.web_search.anthropic_web_search_max_uses",
        ),
        anthropic_web_search_version=_require_optional_non_blank_string(
            raw_config.get("anthropic_web_search_version", "2023-06-01"),
            field_name="replay_backfill.web_search.anthropic_web_search_version",
        ),
        acquisition_tool_names=acquisition_tool_names,
    )


def _load_market_news_backfill_config(
    config_path: Path,
) -> _ReplayBackfillMarketNewsConfig:
    resolved_path = config_path.expanduser().resolve(strict=False)
    top_level_payload = _load_toml_payload(resolved_path)
    replay_backfill = top_level_payload.get("replay_backfill")
    if not isinstance(replay_backfill, dict):
        raise ReplayRuntimeToolError(
            "Replay config must include a replay_backfill TOML table."
        )
    extra_fields = sorted(set(replay_backfill) - {"market_news", "web_search"})
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise ReplayRuntimeToolError(
            f"Unknown replay_backfill config fields: {unknown}"
        )
    raw_config = replay_backfill.get("market_news")
    if not isinstance(raw_config, dict):
        raise ReplayRuntimeToolError(
            "Replay config field 'replay_backfill.market_news' must be a TOML table."
        )

    required_fields = {
        "symbols",
        "slice_hours",
        "base_url",
        "api_key_env",
        "limit",
        "include_content",
        "exclude_contentless",
    }
    optional_fields = {"labels"}
    missing_fields = sorted(required_fields - set(raw_config))
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise ReplayRuntimeToolError(
            "Missing required replay_backfill.market_news config fields: "
            f"{missing}"
        )
    allowed_fields = required_fields | optional_fields
    extra_fields = sorted(set(raw_config) - allowed_fields)
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise ReplayRuntimeToolError(
            f"Unknown replay_backfill.market_news config fields: {unknown}"
        )

    limit = _require_positive_int(
        raw_config["limit"],
        field_name="replay_backfill.market_news.limit",
    )
    if limit > 50:
        raise ReplayRuntimeToolError(
            "replay_backfill.market_news.limit must be less than or equal to 50."
        )
    return _ReplayBackfillMarketNewsConfig(
        symbols=_require_non_blank_string_tuple(
            raw_config["symbols"],
            field_name="replay_backfill.market_news.symbols",
        ),
        labels=tuple(
            validate_labels(
                list(
                    _require_optional_string_tuple(
                        raw_config.get("labels", []),
                        field_name="replay_backfill.market_news.labels",
                    )
                ),
                error_type=ReplayRuntimeToolError,
            )
        ),
        slice_hours=_require_positive_int(
            raw_config["slice_hours"],
            field_name="replay_backfill.market_news.slice_hours",
        ),
        base_url=_require_non_blank_string(
            raw_config["base_url"],
            field_name="replay_backfill.market_news.base_url",
        ),
        api_key_env=_require_non_blank_string(
            raw_config["api_key_env"],
            field_name="replay_backfill.market_news.api_key_env",
        ),
        limit=limit,
        include_content=_require_bool(
            raw_config["include_content"],
            field_name="replay_backfill.market_news.include_content",
        ),
        exclude_contentless=_require_bool(
            raw_config["exclude_contentless"],
            field_name="replay_backfill.market_news.exclude_contentless",
        ),
    )


def _load_toml_payload(config_path: Path) -> dict[str, object]:
    if not config_path.exists() or not config_path.is_file():
        raise ReplayRuntimeToolError(f"TOML config file does not exist: {config_path}")
    try:
        with config_path.open("rb") as handle:
            payload = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ReplayRuntimeToolError(
            f"TOML config contains invalid TOML: {config_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise ReplayRuntimeToolError(f"Could not read TOML config {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReplayRuntimeToolError("TOML config must decode to a TOML table.")
    return payload


def _require_enabled_tool_fields(
    *,
    raw_config: dict[str, object],
    acquisition_tool_names: tuple[str, ...],
) -> None:
    required_by_tool = {
        "search_and_scrape_webpage": (
            "serper_api_key_env",
            "serper_base_url",
        ),
        "jina_scrape_llm_summary": (
            "jina_api_key_env",
            "jina_base_url",
            "summary_llm_api_key_env",
            "summary_llm_base_url",
            "summary_llm_model_name",
        ),
        "event_trader_openai_web_search": (
            "native_web_search_api_key_env",
            "native_web_search_base_url",
            "native_web_search_model",
        ),
        "event_trader_anthropic_web_search": (
            "anthropic_web_search_api_key_env",
            "anthropic_web_search_base_url",
            "anthropic_web_search_model",
        ),
    }
    for tool_name in acquisition_tool_names:
        for field_name in required_by_tool.get(tool_name, ()):
            _require_non_blank_string(
                raw_config.get(field_name, ""),
                field_name=f"replay_backfill.web_search.{field_name}",
            )


def _build_arg_value(
    *,
    cli_value: str | None,
    config_value: object,
    default: str | None = None,
) -> str | None:
    if cli_value is not None:
        return cli_value
    if config_value is not None:
        if not isinstance(config_value, str):
            raise ReplayRuntimeToolError("Build config string fields must be strings.")
        normalized = config_value.strip()
        if not normalized:
            raise ReplayRuntimeToolError("Build config string fields must not be blank.")
        return normalized
    return default


def _build_bool_value(
    *,
    cli_value: bool | None,
    config_value: object,
    default: bool,
    field_name: str,
) -> bool:
    if cli_value is not None:
        return cli_value
    if config_value is None:
        return default
    if not isinstance(config_value, bool):
        raise ReplayRuntimeToolError(f"Build config field {field_name!r} must be a boolean.")
    return config_value


def _require_non_blank_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ReplayRuntimeToolError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ReplayRuntimeToolError(f"{field_name} must not be blank.")
    return normalized


def _require_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ReplayRuntimeToolError(f"{field_name} must be a boolean.")
    return value


def _require_optional_non_blank_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ReplayRuntimeToolError(f"{field_name} must be a string.")
    return value.strip()


def _require_native_web_search_tool_type(value: object, *, field_name: str) -> str:
    normalized = _require_optional_non_blank_string(value, field_name=field_name)
    if normalized not in {"web_search", "web_search_preview"}:
        raise ReplayRuntimeToolError(
            f"{field_name} must be web_search or web_search_preview."
        )
    return normalized


def _require_optional_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplayRuntimeToolError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise ReplayRuntimeToolError(f"{field_name} must be greater than zero.")
    return value


def _require_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplayRuntimeToolError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise ReplayRuntimeToolError(f"{field_name} must be greater than zero.")
    return value


def _require_tool_name_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ReplayRuntimeToolError(f"{field_name} must be a TOML array of strings.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ReplayRuntimeToolError(f"{field_name} must contain only strings.")
        tool_name = item.strip()
        if not tool_name:
            raise ReplayRuntimeToolError(f"{field_name} must not contain blank strings.")
        if tool_name in normalized:
            raise ReplayRuntimeToolError(f"{field_name} must not contain duplicates.")
        normalized.append(tool_name)
    return tuple(normalized)


def _require_non_blank_string_tuple(
    value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    normalized = _require_optional_string_tuple(value, field_name=field_name)
    if not normalized:
        raise ReplayRuntimeToolError(f"{field_name} must not be empty.")
    return normalized


def _require_optional_string_tuple(
    value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ReplayRuntimeToolError(f"{field_name} must be a TOML array of strings.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ReplayRuntimeToolError(f"{field_name} must contain only strings.")
        item_text = item.strip()
        if not item_text:
            raise ReplayRuntimeToolError(f"{field_name} must not contain blank strings.")
        if item_text in normalized:
            raise ReplayRuntimeToolError(f"{field_name} must not contain duplicates.")
        normalized.append(item_text)
    return tuple(normalized)


def _resolve_config_path(
    raw_value: object,
    *,
    config_path: Path,
    field_name: str,
) -> Path:
    path_text = _require_non_blank_string(raw_value, field_name=field_name)
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = resolve_config_relative_path(path, config_path=config_path)
    return path.resolve(strict=False)


def _require_env_value(env_name: str, *, field_name: str, required: bool = True) -> str:
    value = os.environ.get(env_name)
    if value is None or not value.strip():
        if not required:
            return ""
        raise ReplayRuntimeToolError(
            f"{field_name} references an unset environment variable: {env_name}"
        )
    return value.strip()


def _resolve_primary_research_overrides(
    mode: _ReplayPrimaryResearchMode,
) -> tuple[Callable | None, Callable | None]:
    if mode == "production":
        return None, None
    if mode == "deterministic_smoke":
        return (
            deterministic_replay_smoke_checker_policy,
            deterministic_replay_smoke_analysis_callback,
        )
    if mode == "timebatch_passthrough":
        return timebatch_passthrough_checker_policy, None
    if mode == "per_event_passthrough":
        return per_event_passthrough_checker_policy, None
    raise ReplayRuntimeToolError(f"Unsupported primary_research_mode: {mode!r}")


def _run_replay_command(args: argparse.Namespace) -> int:
    from event_trader.tools._replay_runtime_run import run_replay_command

    return run_replay_command(args)


def _prepare_replay_market_prefetch(
    *,
    config_path: Path,
    target_key: str,
    run_id: str,
    dataset_rows: Sequence[_ReplayDatasetRow],
    window_start: datetime,
    window_end: datetime,
    provider: MarketBarsProvider | None = None,
) -> _ReplayMarketPrefetchSetup:
    prefetch_config = load_kernel_config(config_path)
    prefetch_events = tuple(
        ReplayMarketPrefetchEvent(
            target_key=target_key,
            visible_at=replay_ts_event(row.ingress_input),
        )
        for row in dataset_rows
        if window_start <= replay_ts_event(row.ingress_input) <= window_end
    )
    prefetch_receipt = prefetch_replay_market_data(
        config=prefetch_config,
        target_key=target_key,
        run_id=run_id,
        events=prefetch_events,
        window_start=window_start,
        window_end=window_end,
        provider=provider,
        progress=lambda message: print(message, flush=True),
    )
    return _ReplayMarketPrefetchSetup(
        market_data_provider_override=(
            prefetch_receipt.cached_provider()
            if prefetch_receipt is not None
            else None
        ),
        receipt=prefetch_receipt,
        market_data_store_root=(
            prefetch_receipt.store.root if prefetch_receipt is not None else None
        ),
    )


def _run_validation_window_command(args: argparse.Namespace) -> int:
    target_key = validate_target_key(
        args.target_key,
        error_type=ReplayRuntimeToolError,
    )
    window_start = _parse_window_boundary(args.window_start, end_of_day=False)
    window_end = _parse_window_boundary(args.window_end, end_of_day=True)
    if window_end < window_start:
        raise ReplayRuntimeToolError("window_end must be greater than or equal to window_start.")

    config_path = Path(args.config).resolve(strict=False)
    config = load_kernel_config(config_path)
    workspace = bootstrap_workspace(config.workspace_root)
    layout = workspace.layout
    if layout is None:
        raise ReplayRuntimeToolError("Workspace layout is required for validation-window.")

    state_changes = read_state_changes(layout, target_key)
    if not state_changes:
        raise ReplayRuntimeToolError(
            "validation-window requires canonical state-changes, but none exist for "
            f"target_key={target_key!r} under "
            f"{(layout.runtime_root / 'validation' / 'state-changes' / target_key).resolve(
                strict=False
            )}"
        )

    market_data = _build_validation_market_data_port(
        config=config,
        market_data_base_url_override=args.market_data_base_url,
        market_data_api_key_env_override=args.market_data_api_key_env,
        market_data_timeout_seconds_override=args.market_data_timeout_seconds,
    )
    market_mapping = _resolve_replay_market_mapping(
        config=config,
        target_key=target_key,
        market_symbol=args.market_symbol,
        market_session=args.market_session,
        exchange=args.exchange,
        exchange_session_scope=args.exchange_session_scope,
        bar_granularity=args.bar_granularity,
    )
    market_series = market_data.read_series(
        market_mapping,
        start_at=window_start,
        end_at=window_end,
    )
    market_source_metadata = _pop_market_data_source_metadata(market_data)
    adjustment_sidecar = _load_validation_adjustment_sidecar(
        mapping_config=config.validation.market_mappings.get(target_key),
        archive_root=config.validation.market_data.local_archive_root,
        market_symbol=market_mapping.market_symbol,
        source_metadata=market_source_metadata,
    )
    annual_periods = _resolve_validation_window_annual_periods(
        explicit_value=args.annual_periods,
        market_session=market_mapping.market_session,
        exchange_session_scope=market_mapping.exchange_session_scope,
        bar_granularity=market_mapping.bar_granularity,
    )
    result = calculate_window_performance(
        target_key=target_key,
        state_changes=state_changes,
        market_data=market_series,
        window_start=window_start,
        window_end=window_end,
        annual_periods=annual_periods,
        rolling_sharpe_bars=args.rolling_sharpe_bars,
        execution_records=_read_execution_records_for_state_changes(
            layout=layout,
            target_key=target_key,
            state_changes=state_changes,
        ),
        require_execution_records=True,
        adjustment_sidecar=adjustment_sidecar,
    )
    artifact_paths = _write_validation_window_artifacts(
        layout=layout,
        target_key=target_key,
        market_mapping=market_mapping,
        result=result,
    )
    _print_validation_window_summary(
        layout_root=layout.root,
        target_key=target_key,
        market_symbol=market_mapping.market_symbol,
        result=result,
        annual_periods=annual_periods,
        artifact_paths=artifact_paths,
    )
    return 0


def _run_acceptance_report_command(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve(strict=False)
    config = load_kernel_config(config_path)
    workspace = bootstrap_workspace(config.workspace_root)
    layout = workspace.layout
    if layout is None:
        raise ReplayRuntimeToolError("Workspace layout is required for acceptance-report.")
    persisted = write_replay_acceptance_report(
        layout=layout,
        run_id=args.run_id,
        target_key=args.target_key,
        event_id=args.event_id,
        view_episode_id=args.view_episode_id,
        decision_episode_id=args.decision_episode_id,
        commit_hash=args.commit_hash,
        config_path=config.config_path,
        replay_command=args.replay_command,
    )
    print("")
    print("replay acceptance report:")
    print(f"- status={persisted.report.status}")
    print(f"- json={persisted.json_path.as_posix()}")
    print(f"- markdown={persisted.markdown_path.as_posix()}")
    return 0 if persisted.report.status == "passed" else 1


def _run_target_report_command(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve(strict=False)
    config = load_kernel_config(config_path)
    workspace = bootstrap_workspace(config.workspace_root)
    layout = workspace.layout
    if layout is None:
        raise ReplayRuntimeToolError("Workspace layout is required for target-report.")
    persisted = write_target_replay_report(
        layout=layout,
        run_id=args.run_id,
        target_key=args.target_key,
        event_id=args.event_id,
        view_episode_id=args.view_episode_id,
        decision_episode_id=args.decision_episode_id,
        commit_hash=args.commit_hash,
        config_path=config.config_path,
        replay_command=args.replay_command,
    )
    print("")
    print("target replay report:")
    print(f"- status={persisted.report.status}")
    print(f"- json={persisted.json_path.as_posix()}")
    print(f"- markdown={persisted.markdown_path.as_posix()}")
    return 0 if persisted.report.status == "passed" else 1


def _run_release_readiness_report_command(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve(strict=False)
    config = load_kernel_config(config_path)
    workspace = bootstrap_workspace(config.workspace_root)
    layout = workspace.layout
    if layout is None:
        raise ReplayRuntimeToolError("Workspace layout is required for release-readiness-report.")
    persisted = write_release_readiness_report(
        layout=layout,
        run_id=args.run_id,
        target_key=args.target_key,
        view_episode_id=args.view_episode_id,
        commit_hash=args.commit_hash,
        config_path=config.config_path,
        replay_command=args.replay_command,
        git_status_summary=args.git_status_summary,
    )
    print("")
    print("Release Readiness report:")
    print(f"- status={persisted.report.status}")
    print(f"- json={persisted.json_path.as_posix()}")
    print(f"- markdown={persisted.markdown_path.as_posix()}")
    return 0 if persisted.report.status == "passed" else 1


def _run_cost_report_command(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve(strict=False)
    config = load_kernel_config(config_path)
    workspace = bootstrap_workspace(config.workspace_root)
    layout = workspace.layout
    if layout is None:
        raise ReplayRuntimeToolError("Workspace layout is required for cost-report.")
    window_start = _parse_window_boundary(args.window_start, end_of_day=False)
    window_end = _parse_window_boundary(args.window_end, end_of_day=True)
    persisted = write_replay_cost_report(
        layout=layout,
        run_id=args.run_id,
        target_key=args.target_key,
        window_start=window_start,
        window_end=window_end,
        pm_review_log_dir=Path(args.pm_review_log_dir).resolve(strict=False),
        reflection_log_dir=Path(args.reflection_log_dir).resolve(strict=False),
    )
    module_breakdown = {
        item.module_name: item for item in persisted.report.module_breakdown
    }
    checker_module = module_breakdown["checker"]
    analysis_module = module_breakdown["analysis"]
    pm_review_module = module_breakdown["pm_review"]
    reflection_module = module_breakdown["reflection"]
    print("")
    print("replay cost report:")
    print(f"- target={persisted.report.target_key}")
    print(f"- completed_events={persisted.report.completed_event_count}")
    print(
        "- active_pipeline_seconds="
        f"{persisted.report.active_pipeline_timing.total_seconds:.3f}"
    )
    print(f"- runtime_total_tokens={persisted.report.runtime_total_llm_usage.total_tokens}")
    print(
        "- checker_receipt_tokens="
        f"{checker_module.total_token_count}"
    )
    print(
        "- analysis_receipt_tokens="
        f"{analysis_module.total_token_count}"
    )
    print(
        "- pm_review_task_log_seconds="
        f"{pm_review_module.total_seconds:.3f}"
    )
    print(
        "- pm_review_task_log_non_cache_tokens="
        f"{pm_review_module.total_token_count}"
    )
    print(
        "- reflection_task_log_seconds="
        f"{reflection_module.total_seconds:.3f}"
    )
    print(
        "- reflection_task_log_non_cache_tokens="
        f"{reflection_module.total_token_count}"
    )
    print(f"- json={persisted.json_path.as_posix()}")
    print(f"- markdown={persisted.markdown_path.as_posix()}")
    return 0


def _parse_window_boundary(raw_value: str, *, end_of_day: bool) -> datetime:
    normalized = raw_value.strip()
    if not normalized:
        raise ReplayRuntimeToolError("Window values must not be blank.")

    if len(normalized) == 10:
        parsed_date = date.fromisoformat(normalized)
        parsed_time = time(23, 59, 59, tzinfo=UTC) if end_of_day else time(0, 0, tzinfo=UTC)
        return datetime.combine(parsed_date, parsed_time)

    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayRuntimeToolError(
            f"Invalid ISO8601 datetime or YYYY-MM-DD value: {raw_value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReplayRuntimeToolError(
            f"Window value must include timezone information: {raw_value!r}"
        )
    return parsed.astimezone(UTC)


def _resolve_validation_window_annual_periods(
    *,
    explicit_value: float | None,
    market_session: str,
    exchange_session_scope: str,
    bar_granularity: str,
) -> float:
    if explicit_value is not None:
        if not isfinite(explicit_value) or explicit_value <= 0.0:
            raise ReplayRuntimeToolError("annual_periods must be a finite value greater than zero.")
        return explicit_value

    return resolve_annual_periods(
        market_session=market_session,
        exchange_session_scope=exchange_session_scope,
        bar_granularity=bar_granularity,
        error_type=ReplayRuntimeToolError,
    )

def _load_dataset(dataset_path: Path) -> tuple[_ReplayDatasetRow, ...]:
    if not dataset_path.exists() or not dataset_path.is_file():
        raise ReplayRuntimeToolError(f"Dataset file does not exist: {dataset_path}")

    dataset_rows: list[_ReplayDatasetRow] = []
    for line_number, line in enumerate(
        dataset_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw_payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ReplayRuntimeToolError(
                f"Dataset line {line_number} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(raw_payload, dict):
            raise ReplayRuntimeToolError(
                f"Dataset line {line_number} must decode to a JSON object."
            )
        dataset_rows.append(_adapt_replay_payload(raw_payload, line_number=line_number))

    if not dataset_rows:
        raise ReplayRuntimeToolError("Dataset is empty after removing blank lines.")

    return tuple(
        sorted(
            dataset_rows,
            key=lambda item: (
                replay_ts_event(item.ingress_input),
                item.ingress_input.source_ref,
                item.ingress_input.source_shape,
            ),
        )
    )


def _adapt_replay_payload(
    raw_payload: dict[str, object],
    *,
    line_number: int,
) -> _ReplayDatasetRow:
    source_shape = raw_payload.get("source_shape")
    if not isinstance(source_shape, str) or not source_shape.strip():
        raise ReplayRuntimeToolError(
            f"Dataset line {line_number} must include non-blank source_shape."
        )
    replay_source_shape = _require_replay_source_shape(source_shape, line_number)

    labels = _extract_optional_labels(raw_payload, line_number=line_number)
    payload = dict(raw_payload)
    payload.pop("source_shape")
    payload.pop("labels", None)
    normalized_payload = _coerce_timestamp_fields(payload, line_number=line_number)

    try:
        adapter = replay_adapter_for(replay_source_shape)
    except Exception as exc:
        raise ReplayRuntimeToolError(
            f"Dataset line {line_number} uses unsupported source_shape {source_shape!r}: {exc}"
        ) from exc
    try:
        ingress_input = adapter(normalized_payload)
    except Exception as exc:
        raise ReplayRuntimeToolError(
            f"Dataset line {line_number} failed replay adapter validation: {exc}"
        ) from exc
    return _ReplayDatasetRow(ingress_input=ingress_input, labels=labels)


def _require_replay_source_shape(
    value: str,
    line_number: int,
) -> ReplaySourceShape:
    if value not in {
        "historical_news_stream",
        "historical_macro_api",
        "historical_manual_dataset",
        "historical_market_news",
        "historical_web_search",
    }:
        raise ReplayRuntimeToolError(
            f"Dataset line {line_number} uses unsupported source_shape {value!r}."
        )
    return cast(ReplaySourceShape, value)


def _extract_optional_labels(
    raw_payload: dict[str, object],
    *,
    line_number: int,
) -> tuple[str, ...]:
    raw_labels = raw_payload.get("labels")
    if raw_labels is None:
        return ()
    if not isinstance(raw_labels, list):
        raise ReplayRuntimeToolError(
            f"Dataset line {line_number} field 'labels' must be a list[str] when present."
        )
    return tuple(validate_labels(raw_labels, error_type=ReplayRuntimeToolError))


def _coerce_timestamp_fields(
    payload: dict[str, object],
    *,
    line_number: int,
) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in payload.items():
        if key in _TIMESTAMP_FIELDS and value is not None:
            if not isinstance(value, str):
                raise ReplayRuntimeToolError(
                    f"Dataset line {line_number} field {key!r} must be an ISO8601 string."
                )
            normalized[key] = _parse_window_boundary(value, end_of_day=False)
            continue
        normalized[key] = value
    return normalized


def _repair_unfinished_checkpoint_or_raise(
    *,
    layout: WorkspaceLayout,
    latest_checkpoints: dict[str, ReplayEventCheckpoint],
    latest_checkpoint: ReplayEventCheckpoint,
    target_key: str,
    run_key: str,
    run_id: str,
    auto_repair: bool,
) -> None:
    event_id = latest_checkpoint.event_id
    if not auto_repair:
        raise ReplayRuntimeToolError(
            "replay event has an unfinished checkpoint; run runtime_repair "
            "before resuming: "
            f"event_id={event_id} "
            f"status={latest_checkpoint.status} "
            f"stage={latest_checkpoint.stage}"
        )

    try:
        plan = plan_replay_failed_event_repair(
            layout=layout,
            target_key=target_key,
            run_key=run_key,
            run_id=run_id,
            event_id=event_id,
        )
        receipt = apply_replay_repair_plan(layout=layout, plan=plan)
    except OperatorRepairError as exc:
        raise ReplayRuntimeToolError(
            "auto repair failed for unfinished replay checkpoint: "
            f"event_id={event_id} status={latest_checkpoint.status} "
            f"stage={latest_checkpoint.stage}: {exc}"
        ) from exc

    latest_checkpoints.pop(event_id, None)
    receipt_path = (
        "none" if receipt.receipt_path is None else receipt.receipt_path.as_posix()
    )
    print(
        "replay auto repair applied:",
        f"event_id={event_id}",
        f"status={latest_checkpoint.status}",
        f"stage={latest_checkpoint.stage}",
        f"receipt={receipt_path}",
    )


def _run_replay_steps(
    *,
    replay_runtime,
    layout: WorkspaceLayout,
    config: KernelConfig,
    target_key: str,
    dataset_rows: Sequence[_ReplayDatasetRow],
    global_labels: tuple[str, ...],
    window_start: datetime,
    window_end: datetime,
    run_id: str,
    auto_repair_failed_checkpoint: bool,
    log_skipped_checkpoints: bool,
    stage_tracker: _ReplayPipelineStageTracker,
    reflection_driver: _ReplayReflectionCycleDriver | None = None,
) -> tuple[tuple[ReplayStepReceipt, ...], _ReplayRunStepStats]:
    from event_trader.tools._replay_runtime_run import run_replay_steps

    return run_replay_steps(
        replay_runtime=replay_runtime,
        layout=layout,
        config=config,
        target_key=target_key,
        dataset_rows=dataset_rows,
        global_labels=global_labels,
        window_start=window_start,
        window_end=window_end,
        run_id=run_id,
        auto_repair_failed_checkpoint=auto_repair_failed_checkpoint,
        log_skipped_checkpoints=log_skipped_checkpoints,
        stage_tracker=stage_tracker,
        reflection_driver=reflection_driver,
    )


def _derive_replay_event_id(
    *,
    target_key: str,
    replay_at: datetime,
    row: _ReplayDatasetRow,
    labels: tuple[str, ...],
) -> str:
    request = validate_replay_admission_request(
        target_key=target_key,
        ingress_input=row.ingress_input,
        replay_at=replay_at,
        labels=list(labels),
    )
    return derive_admission_event_id(request)


def _maybe_cutover_active_price_basis_for_replay(
    *,
    layout: WorkspaceLayout,
    config: KernelConfig,
    target_key: str,
    replay_at: datetime,
) -> None:
    result = run_active_price_basis_cutover_from_config(
        layout=layout,
        config=config,
        target_key=target_key,
        cutover_at=replay_at,
        require_rebase_ratio_change=True,
    )
    if result.cutover_applied:
        print(
            "active price basis cutover:",
            f"target_key={target_key}",
            f"successor_assessment_id={result.successor_assessment_id}",
            f"old_basis_id={result.old_basis_id}",
            f"new_basis_id={result.new_basis_id}",
        )


def _derive_quarantined_replay_event_id(
    *,
    target_key: str,
    row: _ReplayDatasetRow,
    labels: tuple[str, ...],
) -> str:
    request = validate_admission_request(
        target_key=target_key,
        ingress_input=row.ingress_input,
        labels=list(labels),
    )
    return derive_admission_event_id(request)


def _historical_web_search_future_leakage(
    ingress_input: ReplayIngressInput,
) -> HistoricalWebSearchFutureLeakage | None:
    if not isinstance(ingress_input, HistoricalWebSearchInput):
        return None
    return detect_historical_web_search_future_leakage(
        source_ref=ingress_input.source_ref,
        title=ingress_input.title,
        content=ingress_input.content,
        visible_at=ingress_input.visible_at,
    )


def _append_replay_quarantine_receipt(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_id: str,
    replay_at: datetime,
    ingress_input: ReplayIngressInput,
    finding: HistoricalWebSearchFutureLeakage,
) -> Path:
    path = (
        layout.runtime_root
        / "replay_quarantine"
        / target_key
        / f"{replay_at.astimezone(UTC):%Y-%m}.jsonl"
    ).resolve(strict=False)
    payload = {
        "event_id": event_id,
        "target_key": target_key,
        "visible_at": replay_at.astimezone(UTC).isoformat(),
        "source_ref": ingress_input.source_ref,
        "reason_code": finding.reason_code,
        "matched_date": finding.matched_date.isoformat(),
        "matched_snippet": finding.matched_snippet,
        "created_at": replay_at.astimezone(UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False))
        handle.write("\n")
    return path


def _replay_run_key(
    *,
    target_key: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    return (
        f"{target_key}:"
        f"{window_start.astimezone(UTC).isoformat()}:"
        f"{window_end.astimezone(UTC).isoformat()}"
    )


def _default_market_data_run_id(
    *,
    target_key: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    return (
        f"{target_key}-"
        f"{window_start.astimezone(UTC).date().isoformat()}-"
        f"{window_end.astimezone(UTC).date().isoformat()}"
    )


def _merge_labels(
    row_labels: tuple[str, ...],
    global_labels: tuple[str, ...],
) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for label in (*row_labels, *global_labels):
        if label in seen:
            continue
        seen.add(label)
        merged.append(label)
    return tuple(validate_labels(merged, error_type=ReplayRuntimeToolError))


def _source_classification_counts_for_dataset_rows(
    dataset_rows: Sequence[_ReplayDatasetRow],
) -> tuple[tuple[str, tuple[tuple[str, int], ...]], ...]:
    return source_classification_counts(
        [
            ensure_source_classification_labels(
                source_ref=row.ingress_input.source_ref,
                title=_replay_ingress_title(row.ingress_input),
                content=_replay_ingress_content(row.ingress_input),
                labels=list(row.labels),
            )
            for row in dataset_rows
        ]
    )


def _replay_ingress_title(ingress_input) -> str:
    if hasattr(ingress_input, "title"):
        return ingress_input.title
    if hasattr(ingress_input, "headline"):
        return ingress_input.headline
    if hasattr(ingress_input, "release_key"):
        return ingress_input.release_key
    return ingress_input.source_ref


def _replay_ingress_content(ingress_input) -> str:
    if hasattr(ingress_input, "content"):
        return ingress_input.content
    if hasattr(ingress_input, "body"):
        return ingress_input.body
    if hasattr(ingress_input, "value_text"):
        return ingress_input.value_text
    return _replay_ingress_title(ingress_input)


def _build_replay_reflection_cycle_driver(
    *,
    replay_runtime,
    clock: TestClock,
    reflection_start: datetime,
    step_hours: int,
) -> _ReplayReflectionCycleDriver:
    if step_hours <= 0:
        raise ReplayRuntimeToolError("reflection_step_hours must be greater than zero.")
    if not isinstance(clock, TestClock):
        raise ReplayRuntimeToolError(
            "Replay reflection runtime requires a Nautilus TestClock."
        )
    if not callable(getattr(replay_runtime, "drain_reflection_cycle", None)):
        raise ReplayRuntimeToolError(
            "Replay runtime requires a drain_reflection_cycle() seam."
        )

    return _ReplayReflectionCycleDriver(
        clock=clock,
        drain_cycle=replay_runtime.drain_reflection_cycle,
        next_due_at=reflection_start,
        step=timedelta(hours=step_hours),
    )


def _build_validation_market_data_port(
    *,
    config: KernelConfig,
    market_data_base_url_override: str | None,
    market_data_api_key_env_override: str | None,
    market_data_timeout_seconds_override: float | None,
) -> ApiStocksMarketDataPort:
    validation_config = config.validation
    if market_data_base_url_override is not None:
        base_url = market_data_base_url_override
    elif validation_config is not None:
        base_url = validation_config.market_data.base_url
    else:
        raise ReplayRuntimeToolError(
            "Validation market-data base URL is required. Set "
            "validation.market_data.base_url in config or pass "
            "--market-data-base-url."
        )

    if market_data_api_key_env_override is not None:
        api_key = _require_env_value(
            market_data_api_key_env_override,
            field_name="market_data_api_key_env_override",
        )
    elif validation_config is not None:
        api_key = validation_config.market_data.api_key
    else:
        api_key = _require_env_value(
            _DEFAULT_MARKET_DATA_API_KEY_ENV,
            field_name="validation market-data api key",
        )

    if validation_config is None:
        raise ReplayRuntimeToolError(
            "Validation market-data provider must be configured in validation.market_data."
        )
    timeout_seconds = (
        market_data_timeout_seconds_override
        if market_data_timeout_seconds_override is not None
        else validation_config.market_data.timeout_seconds
    )
    return ApiStocksMarketDataPort(
        ValidationMarketDataConfig(
            provider=validation_config.market_data.provider,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
    )


def _build_replay_market_mapping(
    *,
    target_key: str,
    market_symbol: str,
    market_session: str,
    exchange: str,
    exchange_session_scope: str,
    bar_granularity: str,
) -> MarketMapping:
    normalized_exchange = None if market_session == "continuous" else exchange
    normalized_scope = None if market_session == "continuous" else exchange_session_scope
    from event_trader.market.session_policy import validate_market_session_contract_fields

    validate_market_session_contract_fields(
        market_session=market_session,
        exchange=normalized_exchange,
        exchange_session_scope=normalized_scope,
        error_type=ReplayRuntimeToolError,
    )
    return MarketMapping(
        target_key=target_key,
        market_symbol=market_symbol,
        market_session=market_session,
        exchange=normalized_exchange,
        bar_granularity=bar_granularity,
        exchange_session_scope=normalized_scope,
    )


def _resolve_replay_market_mapping(
    *,
    config: KernelConfig,
    target_key: str,
    market_symbol: str | None,
    market_session: str | None,
    exchange: str | None,
    exchange_session_scope: str | None,
    bar_granularity: str | None,
) -> MarketMapping:
    mapping_config = (
        None
        if config.validation is None
        else config.validation.market_mappings.get(target_key)
    )

    resolved_market_symbol = _resolve_mapping_text(
        cli_value=market_symbol,
        config_value=None if mapping_config is None else mapping_config.market_symbol,
        default="IAU",
        field_name="market_symbol",
    )
    resolved_market_session = _resolve_mapping_text(
        cli_value=market_session,
        config_value=None if mapping_config is None else mapping_config.market_session,
        default="exchange_session",
        field_name="market_session",
    )
    resolved_exchange = _resolve_mapping_text(
        cli_value=exchange,
        config_value=None if mapping_config is None else mapping_config.exchange,
        default="NYSE",
        field_name="exchange",
        allow_none=resolved_market_session == "continuous",
    )
    resolved_exchange_session_scope = _resolve_mapping_text(
        cli_value=exchange_session_scope,
        config_value=(
            None if mapping_config is None else mapping_config.exchange_session_scope
        ),
        default="extended",
        field_name="exchange_session_scope",
        allow_none=resolved_market_session == "continuous",
    )
    resolved_bar_granularity = _resolve_mapping_text(
        cli_value=bar_granularity,
        config_value=None if mapping_config is None else mapping_config.bar_granularity,
        default="1h",
        field_name="bar_granularity",
    )
    return _build_replay_market_mapping(
        target_key=target_key,
        market_symbol=resolved_market_symbol,
        market_session=resolved_market_session,
        exchange=resolved_exchange or "",
        exchange_session_scope=resolved_exchange_session_scope or "",
        bar_granularity=resolved_bar_granularity,
    )


def _resolve_mapping_text(
    *,
    cli_value: str | None,
    config_value: str | None,
    default: str,
    field_name: str,
    allow_none: bool = False,
) -> str | None:
    for value in (cli_value, config_value, default):
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    if allow_none:
        return None
    raise ReplayRuntimeToolError(f"Could not resolve replay market mapping {field_name}.")


def _pop_market_data_source_metadata(market_data: object) -> dict[str, object]:
    consume = getattr(market_data, "pop_source_metadata", None)
    if callable(consume):
        metadata = consume()
        if isinstance(metadata, dict):
            return cast(dict[str, object], metadata)
    return {}


def _load_validation_adjustment_sidecar(
    *,
    mapping_config: ValidationMarketMappingConfig | None,
    archive_root: Path | None,
    market_symbol: str,
    source_metadata: dict[str, object],
) -> MarketAdjustmentSidecar | None:
    if mapping_config is None or mapping_config.adjustment_policy is None:
        return None
    if source_metadata:
        try:
            return load_adjustment_sidecar_from_source_metadata(
                market_symbol=market_symbol,
                policy=mapping_config.adjustment_policy,
                source_metadata=source_metadata,
            )
        except ValueError:
            pass
    if archive_root is None:
        return None
    return load_adjustment_sidecar_from_archive_root(
        archive_root=archive_root,
        market_symbol=market_symbol,
        policy=mapping_config.adjustment_policy,
    )


def _build_open_position_terminal_outputs(
    *,
    config: KernelConfig,
    layout: WorkspaceLayout,
    target_key: str,
    replay_end_at: datetime,
    market_mapping: MarketMapping,
    market_data_base_url_override: str | None,
    market_data_api_key_env_override: str | None,
    market_data_timeout_seconds_override: float | None,
    market_data_provider_override: MarketBarsProvider | None = None,
) -> tuple[_ReplayOpenPositionTerminalSummary | None, str]:
    episode_state = load_target_episode_state(layout, target_key)
    open_episode = episode_state.open_episode
    open_segment = episode_state.open_segment
    lines = ["## Open Position At Replay End", ""]
    if open_episode is None:
        lines.append("- None")
        return None, "\n".join(lines)
    if open_segment is None:
        raise ReplayRuntimeToolError(
            "Open episode artifacts must include open_segment for replay terminal review."
        )

    market_data = (
        market_data_provider_override
        or _build_validation_market_data_port(
            config=config,
            market_data_base_url_override=market_data_base_url_override,
            market_data_api_key_env_override=market_data_api_key_env_override,
            market_data_timeout_seconds_override=market_data_timeout_seconds_override,
        )
    )
    market_series = market_data.read_series(
        market_mapping,
        start_at=open_episode.opened_at,
        end_at=replay_end_at,
    )
    market_source_metadata = _pop_market_data_source_metadata(market_data)
    adjustment_sidecar = _load_validation_adjustment_sidecar(
        mapping_config=config.validation.market_mappings.get(target_key),
        archive_root=config.validation.market_data.local_archive_root,
        market_symbol=market_mapping.market_symbol,
        source_metadata=market_source_metadata,
    )
    state_changes = tuple(
        state_change
        for state_change in read_state_changes(layout, target_key)
        if open_episode.opened_at <= state_change.effective_at <= replay_end_at
    )
    if not state_changes:
        raise ReplayRuntimeToolError(
            "Replay open-position summary requires persisted state changes "
            f"for target_key={target_key!r}."
        )
    execution_records = _read_execution_records_for_state_changes(
        layout=layout,
        target_key=target_key,
        state_changes=state_changes,
    )
    try:
        terminal_mark = calculate_open_episode_terminal_mark(
            open_segment=open_segment,
            open_episode=open_episode,
            market_data=market_series,
            replay_end_at=replay_end_at,
            execution_records=execution_records,
            state_changes=state_changes,
            require_execution_records=True,
            adjustment_sidecar=adjustment_sidecar,
        )
    except ValidationReturnsError as exc:
        raise ReplayRuntimeToolError(
            "Replay open-position summary requires terminal mark-to-market "
            f"for target_key={target_key!r}, episode_id={open_episode.episode_id!r}, "
            f"replay_end_at={replay_end_at.isoformat()}: {exc}"
        ) from exc

    if config.reflection_agent is None:
        raise ReplayRuntimeToolError(
            "Replay open-position reflection requires [reflection_agent] config."
        )
    reflection_runtime_config = MiroThinkerReflectionRuntimeConfig(
        vendor_root=config.reflection_agent.vendor_root,
        log_dir=config.reflection_agent.log_dir,
        llm_provider=config.reflection_agent.llm_provider,
        llm_model_name=config.reflection_agent.llm_model_name,
        llm_api_key=config.reflection_agent.llm_api_key,
        llm_base_url=config.reflection_agent.llm_base_url,
        llm_max_context_length=config.reflection_agent.llm_max_context_length,
    )
    review_horizon_hours = (
        (terminal_mark.replay_end_at - open_episode.opened_at).total_seconds() / 3600.0
    )
    terminal_context = _load_open_position_reflection_context(
        layout=layout,
        target_key=target_key,
        open_episode=open_episode,
        open_segment=open_segment,
        market_mapping=market_mapping,
        market_series=market_series,
        terminal_mark=terminal_mark,
        state_changes=state_changes,
        execution_records=execution_records,
        review_horizon_hours=review_horizon_hours,
    )
    terminal_evaluation = build_mirothinker_open_position_reflection_runner(
        config=reflection_runtime_config,
        layout=layout,
    )(terminal_context)
    terminal_review_receipt = write_open_position_horizon_review(
        context=terminal_context,
        evaluation=terminal_evaluation,
        review_horizon_hours=review_horizon_hours,
        layout=layout,
    )
    try:
        learning_receipt = apply_reflection_learning(
            context=terminal_context,
            evaluation=terminal_evaluation,
            review_receipt=terminal_review_receipt,
            layout=layout,
        )
    except Exception as exc:
        raise ReplayRuntimeToolError(
            "Replay open-position learning write failed: " f"{exc}"
        ) from exc
    if learning_receipt.failed_count:
        raise ReplayRuntimeToolError(
            "Replay open-position learning write failed: "
            f"{_reflection_learning_failure_reason(learning_receipt)}"
        )

    summary = _ReplayOpenPositionTerminalSummary(
        episode_id=terminal_mark.episode_id,
        direction=terminal_mark.direction,
        replay_end_at=terminal_mark.replay_end_at,
        entry_bar_start_at=terminal_mark.entry_bar_start_at,
        mark_bar_start_at=terminal_mark.mark_bar_start_at,
        entry_price=terminal_mark.entry_price,
        mark_price=terminal_mark.mark_price,
        target_weight=terminal_mark.target_weight,
        underlying_return_pct=terminal_mark.underlying_return * 100.0,
        strategy_return_pct=terminal_mark.strategy_return * 100.0,
        terminal_review_page_path=(
            terminal_review_receipt.review_page_path
            if terminal_review_receipt.wrote_review_episode
            else None
        ),
        learning_primary_outcome=learning_receipt.primary_outcome,
        learning_action_count=learning_receipt.action_count,
        learning_written_count=learning_receipt.written_count,
        learning_failed_count=learning_receipt.failed_count,
    )

    lines.extend(
        (
            f"- Episode ID: `{summary.episode_id}`",
            f"- Direction: `{summary.direction}`",
            f"- Replay End At: `{summary.replay_end_at.isoformat()}`",
            f"- Entry Bar Start: `{summary.entry_bar_start_at.isoformat()}`",
            f"- Mark Bar Start: `{summary.mark_bar_start_at.isoformat()}`",
            f"- Entry Price: `{summary.entry_price:.6f}`",
            f"- Mark Price: `{summary.mark_price:.6f}`",
            f"- Target Weight: `{summary.target_weight:.6f}`",
            f"- Underlying Return (%): `{summary.underlying_return_pct:.4f}`",
            f"- Strategy Return (%): `{summary.strategy_return_pct:.4f}`",
            f"- Terminal Reflection Decision: `{terminal_review_receipt.review_decision}`",
            "- Terminal Reflection Rationale: "
            f"{terminal_review_receipt.skip_reason or terminal_evaluation.decision_rationale}",
            f"- Learning Primary Outcome: `{summary.learning_primary_outcome}`",
            f"- Learning Action Count: `{summary.learning_action_count}`",
            f"- Learning Written Count: `{summary.learning_written_count}`",
            f"- Learning Failed Count: `{summary.learning_failed_count}`",
            "- Replay-only terminal mark-to-market for an episode that remained "
            "open at replay end.",
        )
    )
    if terminal_review_receipt.wrote_review_episode:
        lines.append(f"- Terminal Review Page: `{terminal_review_receipt.review_page_path}`")
    return summary, "\n".join(lines)


def _reflection_learning_failure_reason(learning_receipt) -> str:
    reasons = [
        f"{action_receipt.action_id}: {action_receipt.failure_reason}"
        for action_receipt in learning_receipt.action_receipts
        if action_receipt.status == "failed"
    ]
    return "; ".join(reasons)


def _load_open_position_reflection_context(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    open_episode,
    open_segment,
    market_mapping: MarketMapping,
    market_series: MarketDataSeries,
    terminal_mark,
    state_changes: tuple,
    execution_records: tuple[ExecutionRecord, ...],
    review_horizon_hours: float,
) -> OpenPositionReflectionContext:
    original_event_ids = _source_event_ids_first_seen(state_changes)
    ledger = FileBackedEvidenceLedger(layout)
    original_evidence = tuple(ledger.read_many(list(original_event_ids)))
    later_evidence = tuple(
        ledger.read_window(
            start_at=open_episode.opened_at,
            end_at=terminal_mark.replay_end_at,
            target_keys=[target_key],
            exclude_event_ids=list(original_event_ids),
        )
    )
    try:
        feedback_context = build_reflection_feedback_context_facts(
            target_key=target_key,
            episode_id=open_episode.episode_id,
            state_changes=state_changes,
            market_mapping=market_mapping,
            market_data=market_series,
            window_start=open_episode.opened_at,
            window_end=terminal_mark.replay_end_at,
            read_analysis_outcomes=(
                lambda target_key, source_event_ids: read_analysis_outcome_receipts(
                    layout.runtime_root,
                    target_key,
                    source_event_ids,
                )
            ),
            read_checker_receipts=(
                lambda target_key, source_event_ids: read_checker_decision_receipts(
                    layout.runtime_root,
                    target_key,
                    source_event_ids,
                )
            ),
            terminal_mark=terminal_mark,
            execution_records=execution_records,
            require_execution_records=True,
        )
    except ReflectionFeedbackContextError as exc:
        raise ReplayRuntimeToolError(str(exc)) from exc
    reader = FileBackedResearchMemoryReader(layout)
    return OpenPositionReflectionContext(
        episode=open_episode,
        open_segment=open_segment,
        state_changes=state_changes,
        original_evidence=original_evidence,
        later_evidence=later_evidence,
        market_mapping=market_mapping,
        terminal_mark=terminal_mark,
        review_horizon_hours=review_horizon_hours,
        watchlist_context=reader.read_page(f"targets/{target_key}/watchlist.md"),
        portfolio_feedback=feedback_context.portfolio_feedback,
        market_context_usage=feedback_context.market_context_usage,
        target_log_context=reader.read_page(f"targets/{target_key}/log.md"),
    )


def _source_event_ids_first_seen(state_changes) -> tuple[str, ...]:
    ordered_event_ids: list[str] = []
    seen_event_ids: set[str] = set()
    for state_change in state_changes:
        for event_id in state_change.source_event_ids:
            if event_id in seen_event_ids:
                continue
            ordered_event_ids.append(event_id)
            seen_event_ids.add(event_id)
    return tuple(ordered_event_ids)


def _read_execution_records_for_state_changes(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    state_changes: tuple,
) -> tuple[ExecutionRecord, ...]:
    store = ExecutionRecordStore(layout)
    execution_record_lookup_ids = execution_lookup_ids_for_state_changes(state_changes)
    if not execution_record_lookup_ids:
        return ()
    execution_record_lookup_id_set = set(execution_record_lookup_ids)
    records_by_id: dict[str, ExecutionRecord] = {}
    for item in store.read_records(target_key=target_key):
        if item.record.execution_record_id in execution_record_lookup_id_set:
            records_by_id[item.record.execution_record_id] = item.record
    return tuple(
        sorted(
            records_by_id.values(),
            key=lambda item: (item.business_at, item.execution_record_id),
        )
    )


def _write_validation_window_artifacts(
    *,
    layout,
    target_key: str,
    market_mapping: MarketMapping,
    result,
) -> _ValidationWindowArtifactPaths:
    artifact_root = (
        layout.runtime_root / "validation-window" / target_key
    ).resolve(strict=False)
    artifact_root.mkdir(parents=True, exist_ok=True)
    base_name = (
        f"{target_key}_{result.summary.window_start.strftime('%y%m%dT%H%M%S')}_"
        f"{result.summary.window_end.strftime('%y%m%dT%H%M%S')}"
    )
    summary_path = artifact_root / f"{base_name}_summary.json"
    bar_returns_path = artifact_root / f"{base_name}_bar_returns.jsonl"
    summary_payload = {
        "target_key": result.summary.target_key,
        "market_symbol": market_mapping.market_symbol,
        "market_session": market_mapping.market_session,
        "exchange": market_mapping.exchange,
        "exchange_session_scope": market_mapping.exchange_session_scope,
        "bar_granularity": market_mapping.bar_granularity,
        "window_start": result.summary.window_start.isoformat(),
        "window_end": result.summary.window_end.isoformat(),
        "bar_count": result.summary.bar_count,
        "total_underlying_return": result.summary.total_underlying_return,
        "total_strategy_return": result.summary.total_strategy_return,
        "mean_bar_strategy_return": result.summary.mean_bar_strategy_return,
        "bar_return_volatility": result.summary.bar_return_volatility,
        "sharpe_ratio": result.summary.sharpe_ratio,
        "max_drawdown": result.summary.max_drawdown,
        "annual_periods": result.summary.annual_periods,
        "rolling_sharpe_bars": result.summary.rolling_sharpe_bars,
    }
    summary_path.write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    bar_returns_path.write_text(
        "".join(
            json.dumps(
                {
                    "target_key": item.target_key,
                    "state": item.state,
                    "target_weight": item.target_weight,
                    "interval_start_at": item.interval_start_at.isoformat(),
                    "interval_end_at": item.interval_end_at.isoformat(),
                    "mark_mode": item.mark_mode,
                    "entry_price": item.entry_price,
                    "exit_price": item.exit_price,
                    "underlying_return": item.underlying_return,
                    "strategy_return": item.strategy_return,
                    "cumulative_underlying_equity": item.cumulative_underlying_equity,
                    "cumulative_strategy_equity": item.cumulative_strategy_equity,
                    "rolling_sharpe": item.rolling_sharpe,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
            for item in result.bar_returns
        ),
        encoding="utf-8",
    )
    return _ValidationWindowArtifactPaths(
        summary_path=summary_path,
        bar_returns_path=bar_returns_path,
    )

def _print_validation_window_summary(
    *,
    layout_root: Path,
    target_key: str,
    market_symbol: str,
    result,
    annual_periods: float,
    artifact_paths: _ValidationWindowArtifactPaths,
) -> None:
    print("")
    print("validation window summary:")
    print(f"- workspace_root={layout_root}")
    print(f"- target_key={target_key}")
    print(f"- market_symbol={market_symbol}")
    print(
        "- window="
        f"{result.summary.window_start.isoformat()}..{result.summary.window_end.isoformat()}"
    )
    print(f"- bar_count={result.summary.bar_count}")
    print(f"- total_underlying_return={result.summary.total_underlying_return:.8f}")
    print(f"- total_strategy_return={result.summary.total_strategy_return:.8f}")
    print(f"- bar_return_volatility={result.summary.bar_return_volatility:.8f}")
    if result.summary.sharpe_ratio is None:
        print("- sharpe_ratio=none")
    else:
        print(f"- sharpe_ratio={result.summary.sharpe_ratio:.8f}")
    print(f"- max_drawdown={result.summary.max_drawdown:.8f}")
    print(f"- annual_periods={annual_periods:.8f}")
    print(f"- rolling_sharpe_bars={result.summary.rolling_sharpe_bars}")
    print(f"- summary={artifact_paths.summary_path.as_posix()}")
    print(f"- bar_returns={artifact_paths.bar_returns_path.as_posix()}")


def _print_build_summary(
    *,
    layout_root: Path,
    build_output: ReplayBuildOutput,
    output_path: Path,
    report_path: Path | None,
    config_path: Path,
    market_mapping: ValidationMarketMappingConfig | None = None,
) -> None:
    print("")
    print("replay build summary:")
    print(f"- workspace_root={layout_root}")
    print(f"- target_key={build_output.target_key}")
    print(
        "- build_window="
        f"{build_output.start_at.astimezone(UTC).date().isoformat()}.."
        f"{build_output.end_at.astimezone(UTC).date().isoformat()}"
    )
    print(
        "- included_source_kinds="
        f"{list(build_output.report.included_source_kinds)!r}"
    )
    print(
        "- input_counts_by_source_kind="
        f"{list(build_output.report.input_counts_by_source_kind)!r}"
    )
    print(
        "- source_classification_counts="
        f"{list(build_output.report.source_classification_counts)!r}"
    )
    print(
        f"- duplicate_rows_collapsed={build_output.report.duplicate_rows_collapsed}"
    )
    print(f"- output_row_count={build_output.report.output_row_count}")
    print(f"- dataset={output_path.as_posix()}")
    if report_path is not None:
        print(f"- report={report_path.as_posix()}")
    print("- next_run_command=")
    run_parts = [
        "python -m event_trader.tools.replay_runtime run",
        f"--config {config_path.as_posix()}",
        f"--dataset {output_path.as_posix()}",
        "--run-id <fresh-run-id>",
        f"--target-key {build_output.target_key}",
        f"--window-start {build_output.start_at.astimezone(UTC).date().isoformat()}",
        f"--window-end {build_output.end_at.astimezone(UTC).date().isoformat()}",
        "--reflection-end <optional-later-cutoff>",
    ]
    if market_mapping is None:
        run_parts.extend(
            (
                "--market-symbol IAU",
                "--market-session exchange_session",
                "--exchange NYSE",
                "--bar-granularity 1h",
            )
        )
    else:
        run_parts.extend(
            (
                f"--market-symbol {market_mapping.market_symbol}",
                f"--market-session {market_mapping.market_session}",
                f"--bar-granularity {market_mapping.bar_granularity}",
            )
        )
        if market_mapping.exchange is not None:
            run_parts.append(f"--exchange {market_mapping.exchange}")
        if market_mapping.exchange_session_scope is not None:
            run_parts.append(
                f"--exchange-session-scope {market_mapping.exchange_session_scope}"
            )
    run_parts.append("--primary-research-mode deterministic_smoke")
    print("  " + " ".join(run_parts))


def _print_backfill_summary(
    *,
    layout_root: Path,
    receipt: HistoricalWebSearchBackfillReceipt,
    config_path: Path,
) -> None:
    written_count = sum(
        1 for item in receipt.write_receipts if item.status == "written"
    )
    existing_count = sum(
        1 for item in receipt.write_receipts if item.status == "noop_existing"
    )
    resume_skipped_count = len(receipt.resume_skipped_slices)
    print("")
    print("web-search backfill summary:")
    print(f"- workspace_root={layout_root}")
    print(f"- target_key={receipt.target_key}")
    print(
        "- backfill_window="
        f"{receipt.window_start.astimezone(UTC).isoformat()}.."
        f"{receipt.window_end.astimezone(UTC).isoformat()}"
    )
    print(f"- slice_hours={receipt.slice_hours}")
    print(f"- slice_count={len(receipt.slices)}")
    print(f"- resume_skipped_slices={resume_skipped_count}")
    print(f"- records_written={written_count}")
    print(f"- records_existing={existing_count}")
    print("- next_build_command=")
    print(
        "  "
        "python -m event_trader.tools.replay_runtime build "
        f"--config {config_path.as_posix()}"
    )


def _print_market_news_backfill_summary(
    *,
    layout_root: Path,
    receipt: MarketNewsBackfillReceipt,
    config_path: Path,
) -> None:
    written_count = sum(
        1 for item in receipt.write_receipts if item.status == "written"
    )
    existing_count = sum(
        1 for item in receipt.write_receipts if item.status == "noop_existing"
    )
    print("")
    print("market-news backfill summary:")
    print(f"- workspace_root={layout_root}")
    print(f"- target_key={receipt.target_key}")
    print(
        "- backfill_window="
        f"{receipt.window_start.astimezone(UTC).isoformat()}.."
        f"{receipt.window_end.astimezone(UTC).isoformat()}"
    )
    print(f"- slice_hours={receipt.slice_hours}")
    print(f"- slice_count={len(receipt.slices)}")
    print(f"- records_written={written_count}")
    print(f"- records_existing={existing_count}")
    print(f"- rejected_articles={receipt.rejected_article_count}")
    print("- next_build_command=")
    print(
        "  "
        "python -m event_trader.tools.replay_runtime build "
        f"--config {config_path.as_posix()}"
    )


def _print_run_summary(
    *,
    layout,
    target_key: str,
    step_receipts: Sequence[ReplayStepReceipt],
    step_stats: _ReplayRunStepStats,
    reflection_receipts: Sequence[ReflectionHeartbeatReceipt],
    current_state_artifact_path: Path,
    open_position_terminal_summary: _ReplayOpenPositionTerminalSummary | None = None,
    source_classification_counts: Sequence[
        tuple[str, Sequence[tuple[str, int]]]
    ] = (),
) -> None:
    delivered_event_count = sum(
        len(receipt.delivered_event_ids) for receipt in step_receipts
    )
    review_pages = tuple(
        sorted((layout.targets_root / target_key / "reviews").glob("*.md"))
    )
    wrote_review_count = sum(
        1
        for receipt in reflection_receipts
        for candidate in receipt.processed_candidate_receipts
        if candidate.status == "review_written"
    )
    skipped_review_count = sum(
        1
        for receipt in reflection_receipts
        for candidate in receipt.processed_candidate_receipts
        if candidate.status == "review_skipped"
    )

    print("")
    print("replay summary:")
    print(f"- workspace_root={layout.root}")
    print(f"- delivered_event_count={delivered_event_count}")
    print(f"- replay_step_count={len(step_receipts)}")
    print(f"- replay_events_completed={step_stats.completed_count}")
    print(f"- replay_events_checkpoint_skipped={step_stats.skipped_count}")
    print(f"- replay_events_quarantined={step_stats.quarantined_count}")
    print(f"- replay_events_failed={step_stats.failed_count}")
    print(f"- replay_checkpoint={step_stats.checkpoint_path.as_posix()}")
    if step_stats.quarantine_path is not None:
        print(f"- replay_quarantine={step_stats.quarantine_path.as_posix()}")
    for family, counts in source_classification_counts:
        for value, count in counts:
            print(f"- {family}_{value}_count={count}")
    print(f"- reflection_cycle_count={len(reflection_receipts)}")
    print(f"- target_review_pages={len(review_pages)}")
    print(f"- reflection_reviews_written={wrote_review_count}")
    print(f"- reflection_reviews_skipped={skipped_review_count}")
    if open_position_terminal_summary is None:
        print("- open_position_terminal_episode=none")
    else:
        print(
            "- open_position_terminal_episode="
            f"{open_position_terminal_summary.episode_id}"
        )
        print(
            "- open_position_terminal_direction="
            f"{open_position_terminal_summary.direction}"
        )
        print(
            "- open_position_terminal_replay_end_at="
            f"{open_position_terminal_summary.replay_end_at.isoformat()}"
        )
        print(
            "- open_position_terminal_mark_bar_start="
            f"{open_position_terminal_summary.mark_bar_start_at.isoformat()}"
        )
        print(
            "- open_position_terminal_underlying_return_pct="
            f"{open_position_terminal_summary.underlying_return_pct:.4f}"
        )
        print(
            "- open_position_terminal_strategy_return_pct="
            f"{open_position_terminal_summary.strategy_return_pct:.4f}"
        )
        if open_position_terminal_summary.terminal_review_page_path is not None:
            print(
                "- open_position_terminal_review_page="
                f"{open_position_terminal_summary.terminal_review_page_path}"
            )
    print(f"- current_state_report={current_state_artifact_path.as_posix()}")
    for review_page in review_pages:
        print(f"- target_review_page={review_page.as_posix()}")


if __name__ == "__main__":
    raise SystemExit(main())




