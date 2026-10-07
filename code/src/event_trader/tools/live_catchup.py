"""Operator-facing historical catch-up for the live PMReview runtime."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, NoReturn, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from event_trader.composition import compose_kernel
from event_trader.composition_error import CompositionError
from event_trader.config import BootstrapConfigError, KernelConfig, load_kernel_config
from event_trader.contracts._validators import validate_labels, validate_target_key
from event_trader.contracts.ports import OutcomeContextPort
from event_trader.contracts.view_state_change import MarketMapping
from event_trader.feeds.market_news_backfill import (
    MarketNewsBackfillReceipt,
    backfill_market_news,
)
from event_trader.feeds.models import ReplayIngressInput, ReplaySourceShape
from event_trader.feeds.payload_mappers import replay_adapter_for
from event_trader.feeds.search_implementation import (
    SearchImplementationConfig,
    build_search_collection_runner,
)
from event_trader.feeds.web_search_backfill import (
    ArchivedWebSearchWindowReceipt,
    HistoricalWebSearchBackfillReceipt,
    backfill_historical_web_search,
    collect_archived_web_search_window_once,
)
from event_trader.ingest.admission import derive_admission_event_id
from event_trader.live_catchup_identity import (
    live_catchup_chain_run_key,
    live_catchup_replay_run_id,
    matches_live_catchup_chain_run_key,
)
from event_trader.live_market_data_runtime import prepare_live_market_data
from event_trader.live_runtime import (
    _build_web_search_cadence_profile,
)
from event_trader.live_source_checkpoint import (
    LiveSourceCheckpointError,
    advance_live_source_checkpoint,
    live_source_checkpoint_path,
    read_live_source_checkpoint,
)
from event_trader.market.context_builder import (
    _macro_request_start_at,
    _request_start_at,
)
from event_trader.market.contracts import MarketContextProfile
from event_trader.market.provider import MarketBarsProvider
from event_trader.market.shared_store import (
    MarketSeriesIdentity,
    SharedMarketDataError,
    SharedMarketDataProvider,
    SharedMarketDataStore,
    shared_market_data_root,
)
from event_trader.migrations.pm_review_workspace import (
    PMReviewWorkspaceMigrationError,
    prepare_pm_review_workspace,
)
from event_trader.operator.repair.live_catchup_runtime import (
    LiveCatchupRuntimeRepairError,
    repair_live_catchup_runtime,
)
from event_trader.operator.repair.runtime_analysis import plan_analysis_recovery
from event_trader.operator.repair.source_archive import (
    SourceArchiveCleanupError,
    cleanup_live_catchup_source_archive,
)
from event_trader.pm_review.runtime import validate_pm_review_runtime_preflight
from event_trader.reflection.context import ReflectionLedgerContext
from event_trader.reflection.contracts import (
    OutcomeContextPacket,
    ReviewAnchorIdentity,
    ReviewCoverage,
)
from event_trader.replay.admissibility import validate_replay_admission_request
from event_trader.replay.build import ReplayBuildError, ReplayBuildInput, build_replay_input
from event_trader.replay.checkpoint import (
    ReplayEventCheckpoint,
    append_replay_checkpoint,
    read_persisted_replay_checkpoints,
)
from event_trader.replay.slow_path_failure import (
    ReplaySlowPathFailureError,
    ReplaySlowPathFailureGuard,
    advance_replay_deferred_until,
    resume_replay_runtime_backlog,
)
from event_trader.replay.timestamps import replay_ts_event
from event_trader.source_archive.web_search import (
    read_historical_web_search_records,
)
from event_trader.storage import WorkspaceLayout
from event_trader.web_search_schedule import plan_live_web_search_due_windows
from event_trader.workspace import WorkspaceBootstrapError, bootstrap_workspace

LiveCatchupChannel = Literal["web_search", "market_news"]
WebSearchCatchupMode = Literal["acquire", "archive_only"]
Clock = Callable[[], datetime]
Emitter = Callable[[str], None]
_DEFAULT_START_TIMEZONE = "America/New_York"


class LiveCatchupError(RuntimeError):
    """Raised when live catch-up cannot complete safely."""


@dataclass(frozen=True, slots=True)
class LiveCatchupReceipt:
    """Durable summary for one live catch-up attempt."""

    target_key: str
    start_date: str
    start_timezone: str
    start_at: datetime
    end_at: datetime
    channels: tuple[LiveCatchupChannel, ...]
    web_search_mode: WebSearchCatchupMode
    record_counts: dict[str, int]
    released_count: int
    checkpoint_paths: tuple[Path, ...]
    manifest_path: Path
    resume_live_requested: bool
    workspace_auto_prepared: bool
    status: Literal["success"]


@dataclass(frozen=True, slots=True)
class _CatchupDependencies:
    preflight_replay: Callable[..., None]
    acquire_web_search: Callable[..., HistoricalWebSearchBackfillReceipt]
    acquire_market_news: Callable[..., MarketNewsBackfillReceipt]
    release_replay: Callable[..., int]
    resume_live: Callable[[Sequence[str]], int]


@dataclass(frozen=True, slots=True)
class _WebSearchCatchupAcquisition:
    historical_receipt: HistoricalWebSearchBackfillReceipt | None
    today_window_receipts: tuple[ArchivedWebSearchWindowReceipt, ...]
    checkpoint_path: Path | None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m event_trader.tools.live_catchup",
        description=(
            "Run historical catch-up for one live target from an operator start "
            "date to an explicit or current UTC boundary."
        ),
    )
    parser.add_argument("--config", required=True, help="Committed live kernel TOML config.")
    parser.add_argument("--target-key", required=True, help="Live target key to catch up.")
    parser.add_argument(
        "--start-date",
        required=True,
        help="Operator-local catch-up start date, formatted YYYY-MM-DD.",
    )
    parser.add_argument(
        "--start-timezone",
        help=(
            "Timezone for --start-date midnight. Defaults to the live source "
            "cadence timezone, then America/New_York."
        ),
    )
    parser.add_argument(
        "--end-at",
        help=(
            "Inclusive timezone-aware ISO8601 catch-up boundary. Defaults to current UTC."
        ),
    )
    parser.add_argument(
        "--web-search-mode",
        choices=("acquire", "archive_only"),
        default="acquire",
        help=(
            "Acquire missing web-search slices or require and reuse only the existing "
            "archive without invoking a search implementation."
        ),
    )
    parser.add_argument(
        "--resume-live",
        action="store_true",
        help="Start the forward-only live runtime after catch-up succeeds.",
    )
    return parser


def build_cleanup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m event_trader.tools.live_catchup cleanup",
        description="Inspect or safely clean source archive duplicates for live catch-up.",
    )
    parser.add_argument("--config", required=True, help="Committed live kernel TOML config.")
    parser.add_argument("--target-key", required=True, help="Live target key to clean.")
    parser.add_argument(
        "--start-date",
        required=True,
        help="Operator-local catch-up start date, formatted YYYY-MM-DD.",
    )
    parser.add_argument(
        "--start-timezone",
        help=(
            "Timezone for --start-date midnight. Defaults to the live source "
            "cadence timezone, then America/New_York."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite source archive files. Without this, cleanup is a dry-run report.",
    )
    return parser


def build_repair_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m event_trader.tools.live_catchup repair",
        description="Repair failed partial live catch-up runtime artifacts.",
    )
    parser.add_argument("--config", required=True, help="Committed live kernel TOML config.")
    parser.add_argument("--target-key", required=True, help="Live target key to repair.")
    parser.add_argument(
        "--start-date",
        required=True,
        help="Operator-local catch-up start date, formatted YYYY-MM-DD.",
    )
    parser.add_argument(
        "--start-timezone",
        help=(
            "Timezone for --start-date midnight. Defaults to the live source "
            "cadence timezone, then America/New_York."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite/delete failed runtime artifacts. Without this, repair is dry-run.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = tuple(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "cleanup":
        return _main_cleanup(raw_argv[1:])
    if raw_argv and raw_argv[0] == "repair":
        return _main_repair(raw_argv[1:])
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    try:
        receipt = run_live_catchup(
            config_path=Path(args.config),
            target_key=args.target_key,
            start_date=args.start_date,
            start_timezone=args.start_timezone,
            end_at=_parse_operator_end_at(args.end_at),
            web_search_mode=cast(WebSearchCatchupMode, args.web_search_mode),
            resume_live=bool(args.resume_live),
            emit=print,
        )
    except (
        BootstrapConfigError,
        WorkspaceBootstrapError,
        CompositionError,
        LiveSourceCheckpointError,
        PMReviewWorkspaceMigrationError,
        SourceArchiveCleanupError,
        ReplayBuildError,
        LiveCatchupError,
    ) as exc:
        print(f"Live catch-up failed: {exc}", file=sys.stderr)
        return 1

    print(
        "live catch-up complete: "
        f"target_key={receipt.target_key} "
        f"start_at={receipt.start_at.isoformat()} "
        f"end_at={receipt.end_at.isoformat()} "
        f"channels={','.join(receipt.channels)} "
        f"released={receipt.released_count} "
        f"manifest={receipt.manifest_path}"
    )
    return 0


def _main_cleanup(argv: Sequence[str]) -> int:
    parser = build_cleanup_parser()
    args = parser.parse_args(argv)
    try:
        receipt = run_source_archive_cleanup(
            config_path=Path(args.config),
            target_key=args.target_key,
            start_date=args.start_date,
            start_timezone=args.start_timezone,
            apply=bool(args.apply),
            emit=print,
        )
    except (
        BootstrapConfigError,
        WorkspaceBootstrapError,
        SourceArchiveCleanupError,
        LiveCatchupError,
    ) as exc:
        print(f"Live catch-up source archive cleanup failed: {exc}", file=sys.stderr)
        return 1
    print(
        "live catch-up source archive cleanup complete: "
        f"target_key={receipt.target_key} "
        f"status={receipt.status} "
        f"repairable={len(receipt.repairable_duplicates)} "
        f"removed={receipt.removed_count} "
        f"receipt={receipt.receipt_path}"
    )
    return 0


def _main_repair(argv: Sequence[str]) -> int:
    parser = build_repair_parser()
    args = parser.parse_args(argv)
    try:
        report = run_runtime_repair(
            config_path=Path(args.config),
            target_key=args.target_key,
            start_date=args.start_date,
            start_timezone=args.start_timezone,
            apply=bool(args.apply),
            emit=print,
        )
    except (
        BootstrapConfigError,
        WorkspaceBootstrapError,
        LiveCatchupRuntimeRepairError,
        LiveCatchupError,
    ) as exc:
        print(f"Live catch-up runtime repair failed: {exc}", file=sys.stderr)
        return 1
    print(
        "live catch-up runtime repair complete: "
        f"target_key={report.target_key} "
        f"status={report.status} "
        f"actions={len(report.actions)} "
        f"blockers={len(report.blockers)} "
        f"removed={report.removed_count} "
        f"receipt={report.receipt_path}"
    )
    return 0


def run_live_catchup(
    *,
    config_path: Path,
    target_key: str,
    start_date: str,
    start_timezone: str | None = None,
    end_at: datetime | None = None,
    web_search_mode: WebSearchCatchupMode = "acquire",
    resume_live: bool,
    emit: Emitter,
    now: Clock = lambda: datetime.now(UTC),
    dependencies: _CatchupDependencies | None = None,
) -> LiveCatchupReceipt:
    if not callable(emit):
        raise LiveCatchupError("emit must be callable.")
    validated_target_key = validate_target_key(target_key, error_type=LiveCatchupError)
    observed_now = _validate_utc_now(now())
    resolved_end_at = observed_now if end_at is None else _validate_utc_now(end_at)
    if resolved_end_at > observed_now:
        raise LiveCatchupError("end-at must not be later than current UTC.")
    resolved_web_search_mode = _validate_web_search_mode(web_search_mode)
    resolved_config_path = Path(config_path).expanduser().resolve(strict=False)
    config = load_kernel_config(resolved_config_path)
    if config.mode != "live":
        raise LiveCatchupError("live catch-up requires config mode='live'.")
    workspace = bootstrap_workspace(config.workspace_root)
    layout = workspace.layout
    if layout is None:
        raise LiveCatchupError("live catch-up requires a workspace layout.")
    workspace_auto_prepared = False
    if config.pm_review_runtime.require_workspace_ready:
        workspace_auto_prepared = _ensure_pm_review_workspace_ready(
            config=config,
            layout=layout,
            target_key=validated_target_key,
            migrated_at=resolved_end_at,
            emit=emit,
        )

    deps = dependencies or _default_dependencies()
    channels = _configured_historical_channels(
        config=config,
        target_key=validated_target_key,
    )
    if not channels:
        raise LiveCatchupError(
            "No enabled historical catch-up channels are configured for "
            f"target_key={validated_target_key!r}."
        )
    resolved_start_timezone = _resolve_start_timezone(
        config=config,
        target_key=validated_target_key,
        explicit_timezone=start_timezone,
    )
    start_at = parse_operator_start_date(
        start_date,
        timezone_name=resolved_start_timezone,
    )
    if resolved_end_at < start_at:
        raise LiveCatchupError("start-date resolves after the current UTC catch-up end.")

    shared_market_provider, market_data_store_root, market_data_snapshot_id = (
        _prepare_replay_market_data(
            config=config,
            layout=layout,
            target_key=validated_target_key,
            start_at=start_at,
            end_at=resolved_end_at,
            emit=emit,
        )
    )
    try:
        deps.preflight_replay(
            config_path=resolved_config_path,
            config=config,
            layout=layout,
            target_key=validated_target_key,
            start_at=start_at,
            end_at=resolved_end_at,
            emit=emit,
            market_data_store_root=market_data_store_root,
            market_data_snapshot_id=market_data_snapshot_id,
        )
    except Exception:
        _close_market_data_provider(shared_market_provider)
        raise

    record_counts: dict[str, int] = {}
    checkpoint_paths: list[Path] = []
    emit(
        "live catch-up: acquiring historical sources "
        f"target_key={validated_target_key} "
        f"window={start_at.isoformat()}..{resolved_end_at.isoformat()} "
        f"web_search_mode={resolved_web_search_mode} "
        f"channels={','.join(channels)}"
    )
    if "web_search" in channels:
        if resolved_web_search_mode == "archive_only":
            record_counts["web_search"] = _verify_archived_web_search(
                config=config,
                layout=layout,
                target_key=validated_target_key,
                start_at=start_at,
                end_at=resolved_end_at,
            )
        else:
            web_acquisition = _acquire_web_search(
                config=config,
                layout=layout,
                target_key=validated_target_key,
                start_at=start_at,
                end_at=resolved_end_at,
                acquire=deps.acquire_web_search,
            )
            historical_count = (
                0
                if web_acquisition.historical_receipt is None
                else len(web_acquisition.historical_receipt.write_receipts)
            )
            today_count = sum(
                len(receipt.write_receipts)
                for receipt in web_acquisition.today_window_receipts
            )
            record_counts["web_search"] = historical_count + today_count
            if web_acquisition.checkpoint_path is not None:
                checkpoint_paths.append(web_acquisition.checkpoint_path)
    if "market_news" in channels:
        market_receipt = _acquire_market_news(
            config=config,
            layout=layout,
            target_key=validated_target_key,
            start_at=start_at,
            end_at=resolved_end_at,
            acquire=deps.acquire_market_news,
        )
        record_counts["market_news"] = len(market_receipt.write_receipts)

    try:
        released_count = deps.release_replay(
            config_path=resolved_config_path,
            config=config,
            layout=layout,
            target_key=validated_target_key,
            start_at=start_at,
            end_at=resolved_end_at,
            channels=channels,
            emit=emit,
            market_data_store_root=market_data_store_root,
            market_data_snapshot_id=market_data_snapshot_id,
        )
    finally:
        _close_market_data_provider(shared_market_provider)

    checkpoint_paths.extend(
        _sync_live_source_bookkeeping_checkpoints(
            layout=layout,
            target_key=validated_target_key,
            channels=channels,
            end_at=resolved_end_at,
        )
    )
    manifest_path = _write_manifest(
        layout=layout,
        target_key=validated_target_key,
        start_date=start_date,
        start_timezone=resolved_start_timezone,
        start_at=start_at,
        end_at=resolved_end_at,
        channels=channels,
        web_search_mode=resolved_web_search_mode,
        record_counts=record_counts,
        released_count=released_count,
        checkpoint_paths=tuple(dict.fromkeys(checkpoint_paths)),
        resume_live_requested=resume_live,
        workspace_auto_prepared=workspace_auto_prepared,
        status="success",
    )
    receipt = LiveCatchupReceipt(
        target_key=validated_target_key,
        start_date=start_date,
        start_timezone=resolved_start_timezone,
        start_at=start_at,
        end_at=resolved_end_at,
        channels=channels,
        web_search_mode=resolved_web_search_mode,
        record_counts=record_counts,
        released_count=released_count,
        checkpoint_paths=tuple(dict.fromkeys(checkpoint_paths)),
        manifest_path=manifest_path,
        resume_live_requested=resume_live,
        workspace_auto_prepared=workspace_auto_prepared,
        status="success",
    )
    if resume_live:
        exit_code = deps.resume_live(["--config", str(resolved_config_path)])
        if exit_code != 0:
            raise LiveCatchupError(f"live runtime exited with code {exit_code}.")
    return receipt


def run_source_archive_cleanup(
    *,
    config_path: Path,
    target_key: str,
    start_date: str,
    start_timezone: str | None = None,
    apply: bool,
    emit: Emitter,
    now: Clock = lambda: datetime.now(UTC),
):
    if not callable(emit):
        raise LiveCatchupError("emit must be callable.")
    validated_target_key = validate_target_key(target_key, error_type=LiveCatchupError)
    end_at = _validate_utc_now(now())
    resolved_config_path = Path(config_path).expanduser().resolve(strict=False)
    config = load_kernel_config(resolved_config_path)
    if config.mode != "live":
        raise LiveCatchupError("live catch-up cleanup requires config mode='live'.")
    workspace = bootstrap_workspace(config.workspace_root)
    layout = workspace.layout
    if layout is None:
        raise LiveCatchupError("live catch-up cleanup requires a workspace layout.")
    channels = _configured_historical_channels(
        config=config,
        target_key=validated_target_key,
    )
    resolved_start_timezone = _resolve_start_timezone(
        config=config,
        target_key=validated_target_key,
        explicit_timezone=start_timezone,
    )
    start_at = parse_operator_start_date(
        start_date,
        timezone_name=resolved_start_timezone,
    )
    if end_at < start_at:
        raise LiveCatchupError("start-date resolves after the current UTC cleanup end.")
    receipt = cleanup_live_catchup_source_archive(
        layout=layout,
        target_key=validated_target_key,
        start_at=start_at,
        end_at=end_at,
        channels=channels,
        apply=apply,
        created_at=end_at,
    )
    mode = "apply" if apply else "dry-run"
    emit(
        "live catch-up source archive cleanup: "
        f"mode={mode} target_key={validated_target_key} "
        f"repairable={len(receipt.repairable_duplicates)} "
        f"removed={receipt.removed_count} receipt={receipt.receipt_path}"
    )
    return receipt


def run_runtime_repair(
    *,
    config_path: Path,
    target_key: str,
    start_date: str,
    start_timezone: str | None = None,
    apply: bool,
    emit: Emitter,
    now: Clock = lambda: datetime.now(UTC),
):
    if not callable(emit):
        raise LiveCatchupError("emit must be callable.")
    validated_target_key = validate_target_key(target_key, error_type=LiveCatchupError)
    repaired_at = _validate_utc_now(now())
    resolved_config_path = Path(config_path).expanduser().resolve(strict=False)
    config = load_kernel_config(resolved_config_path)
    if config.mode != "live":
        raise LiveCatchupError("live catch-up repair requires config mode='live'.")
    workspace = bootstrap_workspace(config.workspace_root)
    layout = workspace.layout
    if layout is None:
        raise LiveCatchupError("live catch-up repair requires a workspace layout.")
    resolved_start_timezone = _resolve_start_timezone(
        config=config,
        target_key=validated_target_key,
        explicit_timezone=start_timezone,
    )
    start_at = parse_operator_start_date(
        start_date,
        timezone_name=resolved_start_timezone,
    )
    report = repair_live_catchup_runtime(
        layout=layout,
        target_key=validated_target_key,
        start_at=start_at,
        apply=apply,
        created_at=repaired_at,
    )
    mode = "apply" if apply else "dry-run"
    emit(
        "live catch-up runtime repair: "
        f"mode={mode} target_key={validated_target_key} "
        f"status={report.status} actions={len(report.actions)} "
        f"blockers={len(report.blockers)} removed={report.removed_count} "
        f"receipt={report.receipt_path}"
    )
    if report.plan is not None and report.plan.boundary_summary is not None:
        boundary = report.plan.boundary_summary
        emit(
            "  boundary: "
            f"kind={boundary.kind} "
            f"repair_replay_at={boundary.repair_replay_at} "
            f"preserved={len(boundary.preserved_event_ids)} "
            f"affected={len(boundary.affected_event_ids)}"
        )
        if report.plan.touched_stage_counts:
            counts = ", ".join(
                f"{item.stage}={item.artifact_count}"
                for item in report.plan.touched_stage_counts
            )
            emit(f"  touched stages: {counts}")
    for action in report.actions:
        if action.kind == "remove_jsonl_records":
            emit(
                "  would remove jsonl records: "
                f"path={action.path} count={action.record_count} "
                f"lines={','.join(str(line) for line in action.line_numbers)}"
            )
        elif action.kind == "delete_directory":
            emit(f"  would delete directory: path={action.path}")
    for blocker in report.blockers:
        location = "" if blocker.path is None else f" path={blocker.path}"
        emit(f"  blocker: {blocker.reason}{location}")
    if report.plan is not None:
        for artifact in report.plan.blocked_artifacts:
            lines = (
                ""
                if not artifact.line_numbers
                else f" lines={','.join(str(line) for line in artifact.line_numbers)}"
            )
            emit(
                "  blocked artifact: "
                f"stage={artifact.stage} path={artifact.path}{lines}"
            )
        for artifact in report.plan.rebuildable_artifacts:
            emit(
                "  rebuildable artifact: "
                f"stage={artifact.stage} path={artifact.path}"
            )
    return report


def parse_operator_start_date(raw_value: str, *, timezone_name: str) -> datetime:
    try:
        parsed_date = date.fromisoformat(raw_value)
    except ValueError as exc:
        raise LiveCatchupError("start-date must be formatted YYYY-MM-DD.") from exc
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise LiveCatchupError(f"Unknown start timezone: {timezone_name!r}.") from exc
    start_boundary = datetime.combine(parsed_date, time.min, tzinfo=timezone)
    return start_boundary.astimezone(UTC)


def _default_dependencies() -> _CatchupDependencies:
    from event_trader.tools.live_runtime import main as live_runtime_main

    return _CatchupDependencies(
        preflight_replay=_preflight_replay_runtime_from_live_config,
        acquire_web_search=backfill_historical_web_search,
        acquire_market_news=backfill_market_news,
        release_replay=_release_replay_from_live_config,
        resume_live=live_runtime_main,
    )


class _NoopOutcomeContextPort(OutcomeContextPort):
    def read_outcome_context(
        self,
        anchor: ReviewAnchorIdentity,
        coverage: ReviewCoverage,
    ) -> OutcomeContextPacket:
        _ = (anchor, coverage)
        raise AssertionError(
            "No shared-anchor reflection context should be requested during "
            "target-only live catch-up replay."
        )


def _unexpected_shared_review(
    _context: ReflectionLedgerContext,
) -> NoReturn:
    raise LiveCatchupError(
        "Live catch-up target replay must not evaluate shared reflection anchors."
    )


def _ensure_pm_review_workspace_ready(
    *,
    config: KernelConfig,
    layout: WorkspaceLayout,
    target_key: str,
    migrated_at: datetime,
    emit: Emitter,
) -> bool:
    try:
        validate_pm_review_runtime_preflight(
            config=config,
            layout=layout,
            target_keys=(target_key,),
        )
        return False
    except CompositionError as exc:
        if not _is_fresh_pm_review_workspace_error(exc):
            raise
    prepare_pm_review_workspace(
        layout=layout,
        target_keys=(target_key,),
        migrated_at=migrated_at,
    )
    emit(f"pm review workspace auto-prepared: target_key={target_key}")
    validate_pm_review_runtime_preflight(
        config=config,
        layout=layout,
        target_keys=(target_key,),
    )
    return True


def _is_fresh_pm_review_workspace_error(exc: CompositionError) -> bool:
    message = str(exc)
    return (
        "runtime schema marker is missing" in message
        or "cutover baseline portfolio state is missing" in message
    )


def _configured_historical_channels(
    *,
    config: KernelConfig,
    target_key: str,
) -> tuple[LiveCatchupChannel, ...]:
    live_config = config.live
    if live_config is None:
        raise LiveCatchupError("live catch-up requires [live] config.")
    enabled = set(live_config.enabled_channels)
    channels: list[LiveCatchupChannel] = []
    if "web_search" in enabled and live_config.web_search is not None:
        if any(target.target_key == target_key for target in live_config.web_search.targets):
            channels.append("web_search")
    if "market_news" in enabled and live_config.market_news is not None:
        rest_config = live_config.market_news.rest
        if rest_config is not None and rest_config.enabled:
            if any(
                target.target_key == target_key
                for target in live_config.market_news.targets
            ):
                channels.append("market_news")
    return tuple(channels)


def _resolve_start_timezone(
    *,
    config: KernelConfig,
    target_key: str,
    explicit_timezone: str | None,
) -> str:
    if explicit_timezone is not None:
        return _validate_timezone_name(explicit_timezone)
    live_config = config.live
    if live_config is None:
        return _DEFAULT_START_TIMEZONE
    web_config = live_config.web_search
    if web_config is not None and any(
        target.target_key == target_key for target in web_config.targets
    ):
        return _validate_timezone_name(web_config.cadence_profile_config.timezone)
    market_config = live_config.market_news
    rest_config = None if market_config is None else market_config.rest
    if (
        market_config is not None
        and rest_config is not None
        and any(target.target_key == target_key for target in market_config.targets)
    ):
        return _validate_timezone_name(rest_config.cadence_profile_config.timezone)
    return _DEFAULT_START_TIMEZONE


def _validate_timezone_name(value: str) -> str:
    if not isinstance(value, str):
        raise LiveCatchupError("start timezone must be a string.")
    normalized = value.strip()
    if not normalized:
        raise LiveCatchupError("start timezone must not be blank.")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise LiveCatchupError(f"Unknown start timezone: {normalized!r}.") from exc
    return normalized


def _parse_operator_end_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise LiveCatchupError("end-at must be a non-blank ISO8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveCatchupError(
            "end-at must be a valid timezone-aware ISO8601 timestamp."
        ) from exc
    return _validate_utc_now(parsed)


def _validate_web_search_mode(value: object) -> WebSearchCatchupMode:
    if not isinstance(value, str):
        raise LiveCatchupError("web-search mode must be a string.")
    normalized = value.strip().lower()
    if normalized not in {"acquire", "archive_only"}:
        raise LiveCatchupError(
            "web-search mode must be either 'acquire' or 'archive_only'."
        )
    return cast(WebSearchCatchupMode, normalized)


def _current_local_day_start_utc(*, timezone_name: str, timestamp: datetime) -> datetime:
    timezone = ZoneInfo(_validate_timezone_name(timezone_name))
    local_timestamp = timestamp.astimezone(timezone)
    return datetime.combine(
        local_timestamp.date(),
        time.min,
        tzinfo=timezone,
    ).astimezone(UTC)


def _acquire_web_search(
    *,
    config: KernelConfig,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
    acquire: Callable[..., HistoricalWebSearchBackfillReceipt],
) -> _WebSearchCatchupAcquisition:
    web_config = config.live.web_search if config.live is not None else None
    catchup_config = (
        config.live_catchup.web_search if config.live_catchup is not None else None
    )
    if web_config is None:
        raise LiveCatchupError("[live.web_search] is required for web_search catch-up.")
    if catchup_config is None:
        raise LiveCatchupError(
            "[live_catchup.web_search] is required for web_search historical slicing."
        )
    target_config = _single_target(
        web_config.targets,
        target_key=target_key,
        channel="web_search",
    )
    checkpoint = read_live_source_checkpoint(
        layout,
        target_key=target_key,
        channel="web_search",
    )
    run_search_agent = build_search_collection_runner(
        config=SearchImplementationConfig.from_live_config(
            web_config,
            workspace_root=layout.root,
        )
    )
    historical_receipt: HistoricalWebSearchBackfillReceipt | None = None
    today_window_receipts: list[ArchivedWebSearchWindowReceipt] = []
    checkpoint_path: Path | None = None

    today_start_utc = _current_local_day_start_utc(
        timezone_name=web_config.cadence_profile_config.timezone,
        timestamp=end_at,
    )
    if start_at < today_start_utc:
        historical_receipt = acquire(
            layout=layout,
            target_key=target_key,
            search_intent=target_config.search_intent,
            prompt_profile_id=target_config.prompt_profile_id,
            control_language=target_config.control_language,
            retrieval_languages=target_config.retrieval_languages,
            window_start=start_at,
            window_end=today_start_utc - timedelta(seconds=1),
            slice_hours=catchup_config.max_slice_hours,
            run_search_agent=run_search_agent,
        )

    today_window_start = max(start_at, today_start_utc)
    if end_at < today_window_start:
        return _WebSearchCatchupAcquisition(
            historical_receipt=historical_receipt,
            today_window_receipts=(),
            checkpoint_path=checkpoint_path,
        )

    # Historical slices always start from the operator-requested boundary. The
    # durable live source checkpoint only suppresses same-day due windows that a
    # prior catch-up run already completed; live_runtime never uses it as a
    # startup cursor.
    cursor_at = today_window_start
    if checkpoint is not None and checkpoint.last_successful_batch_end > cursor_at:
        cursor_at = checkpoint.last_successful_batch_end
    due_plan = plan_live_web_search_due_windows(
        _build_web_search_cadence_profile(web_config),
        cursor_at=cursor_at,
        now=end_at,
    )
    for due_window in due_plan.due_windows:
        receipt = collect_archived_web_search_window_once(
            layout=layout,
            target_key=target_key,
            search_intent=target_config.search_intent,
            window_start=due_window.window_start,
            window_end=due_window.window_end,
            run_search_agent=run_search_agent,
        )
        today_window_receipts.append(receipt)
        checkpoint_path = _write_live_source_bookkeeping_checkpoint(
            layout=layout,
            target_key=target_key,
            channel="web_search",
            last_successful_batch_end=due_window.window_end,
        )
    return _WebSearchCatchupAcquisition(
        historical_receipt=historical_receipt,
        today_window_receipts=tuple(today_window_receipts),
        checkpoint_path=checkpoint_path,
    )


def _verify_archived_web_search(
    *,
    config: KernelConfig,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
) -> int:
    web_config = config.live.web_search if config.live is not None else None
    catchup_config = (
        config.live_catchup.web_search if config.live_catchup is not None else None
    )
    if web_config is None:
        raise LiveCatchupError(
            "[live.web_search] is required for archive-only web-search catch-up."
        )
    if catchup_config is None:
        raise LiveCatchupError(
            "[live_catchup.web_search] is required for archive-only slice verification."
        )
    target_config = _single_target(
        web_config.targets,
        target_key=target_key,
        channel="web_search",
    )
    try:
        verification = backfill_historical_web_search(
            layout=layout,
            target_key=target_key,
            search_intent=target_config.search_intent,
            prompt_profile_id=target_config.prompt_profile_id,
            control_language=target_config.control_language,
            retrieval_languages=target_config.retrieval_languages,
            window_start=start_at,
            window_end=end_at,
            slice_hours=catchup_config.max_slice_hours,
            run_search_agent=_archive_only_search_forbidden,
        )
        records = read_historical_web_search_records(
            layout,
            target_key=target_key,
            start_at=start_at,
            end_at=end_at,
        )
    except LiveCatchupError:
        raise
    except Exception as exc:
        raise LiveCatchupError(
            f"archive-only web-search verification failed: {exc}"
        ) from exc
    if len(verification.resume_skipped_slices) != len(verification.slices):
        raise LiveCatchupError(
            "archive-only web-search verification did not prove every expected slice complete."
        )
    if verification.write_receipts:
        raise LiveCatchupError(
            "archive-only web-search verification unexpectedly wrote source records."
        )
    return len(records)


def _archive_only_search_forbidden(**kwargs: object) -> NoReturn:
    query = kwargs.get("query")
    query_summary = query if isinstance(query, str) else "unknown"
    raise LiveCatchupError(
        "archive-only web-search catch-up is missing a required completed slice; "
        f"network acquisition is forbidden. query={query_summary!r}"
    )


def _acquire_market_news(
    *,
    config: KernelConfig,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
    acquire: Callable[..., MarketNewsBackfillReceipt],
) -> MarketNewsBackfillReceipt:
    market_config = config.live.market_news if config.live is not None else None
    catchup_config = (
        config.live_catchup.market_news if config.live_catchup is not None else None
    )
    if market_config is None or market_config.rest is None:
        raise LiveCatchupError("[live.market_news.rest] is required for catch-up.")
    if catchup_config is None:
        raise LiveCatchupError(
            "[live_catchup.market_news] is required for market-news historical slicing."
        )
    rest_config = market_config.rest
    if not rest_config.enabled:
        raise LiveCatchupError("[live.market_news.rest] must be enabled for catch-up.")
    target_config = _single_target(
        market_config.targets,
        target_key=target_key,
        channel="market_news",
    )
    return acquire(
        layout=layout,
        target_key=target_key,
        symbols=target_config.symbols,
        labels=target_config.labels,
        base_url=rest_config.base_url,
        api_key=rest_config.api_key,
        window_start=start_at,
        window_end=end_at,
        slice_hours=catchup_config.max_slice_hours,
        limit=rest_config.limit,
        include_content=rest_config.include_content,
        exclude_contentless=rest_config.exclude_contentless,
    )


def _preflight_replay_runtime_from_live_config(
    *,
    config_path: Path,
    config: KernelConfig,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
    emit: Emitter,
    market_data_store_root: Path | None = None,
    market_data_snapshot_id: str | None = None,
) -> None:
    temp_replay_config = _write_temp_replay_config(
        original_config_path=config_path,
        target_key=target_key,
        start_at=start_at,
        end_at=end_at,
    )
    composed = None
    if market_data_snapshot_id is None:
        (
            market_data_provider,
            market_data_store_root,
            market_data_snapshot_id,
        ) = _prepare_replay_market_data(
            config=config,
            layout=layout,
            target_key=target_key,
            start_at=start_at,
            end_at=end_at,
            emit=emit,
        )
    else:
        market_data_provider = _snapshot_market_data_provider(
            config=config,
            target_key=target_key,
            market_data_store_root=market_data_store_root,
            market_data_snapshot_id=market_data_snapshot_id,
        )
    try:
        composed = compose_kernel(
            temp_replay_config,
            emit=emit,
            outcome_context=_NoopOutcomeContextPort(),
            evaluate_review=_unexpected_shared_review,
            market_data_provider_override=market_data_provider,
            market_data_store_root=market_data_store_root,
            market_data_snapshot_id=market_data_snapshot_id,
            replay_market_mapping=_market_mapping_from_config(
                config=config,
                target_key=target_key,
            ),
            replay_run_id=live_catchup_replay_run_id(
                target_key=target_key,
                attempt_at=end_at,
            ),
            pm_review_preflight_target_keys=(target_key,),
        )
        composed.require_replay_runtime()
    finally:
        if composed is not None:
            try:
                composed.close()
            except Exception as exc:  # pragma: no cover - cleanup guard
                print(f"Replay preflight cleanup failed: {exc}", file=sys.stderr)
        if market_data_provider is not None:
            try:
                close_market_data = getattr(market_data_provider, "close", None)
                if callable(close_market_data):
                    close_market_data()
            except Exception as exc:  # pragma: no cover - cleanup guard
                print(f"Replay preflight market-data cleanup failed: {exc}", file=sys.stderr)
        try:
            temp_replay_config.unlink()
        except FileNotFoundError:
            pass


def _release_replay_from_live_config(
    *,
    config_path: Path,
    config: KernelConfig,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
    channels: tuple[LiveCatchupChannel, ...],
    emit: Emitter,
    market_data_store_root: Path | None = None,
    market_data_snapshot_id: str | None = None,
) -> int:
    build_input = ReplayBuildInput(
        target_key=target_key,
        start_at=start_at,
        end_at=end_at,
        include_archived_web_search="web_search" in channels,
        include_archived_market_news="market_news" in channels,
    )
    try:
        build_output = build_replay_input(layout, build_input)
    except ReplayBuildError as exc:
        if "selected zero rows" in str(exc):
            emit("live catch-up replay: no archived rows selected for release")
            return 0
        raise
    rows = tuple(
        _adapt_replay_row(row, line_number=index + 1)
        for index, row in enumerate(build_output.rows)
    )
    if not rows:
        return 0

    temp_replay_config = _write_temp_replay_config(
        original_config_path=config_path,
        target_key=target_key,
        start_at=start_at,
        end_at=end_at,
    )
    composed = None
    market_data_provider = None
    try:
        market_mapping = _market_mapping_from_config(config=config, target_key=target_key)
        if market_data_snapshot_id is None:
            (
                market_data_provider,
                market_data_store_root,
                market_data_snapshot_id,
            ) = _prepare_replay_market_data(
                config=config,
                layout=layout,
                target_key=target_key,
                start_at=start_at,
                end_at=end_at,
                emit=emit,
            )
        else:
            market_data_provider = _snapshot_market_data_provider(
                config=config,
                target_key=target_key,
                market_data_store_root=market_data_store_root,
                market_data_snapshot_id=market_data_snapshot_id,
            )
        composed = compose_kernel(
            temp_replay_config,
            emit=emit,
            outcome_context=_NoopOutcomeContextPort(),
            evaluate_review=_unexpected_shared_review,
            market_data_provider_override=market_data_provider,
            market_data_store_root=market_data_store_root,
            market_data_snapshot_id=market_data_snapshot_id,
            replay_market_mapping=market_mapping,
            replay_run_id=live_catchup_replay_run_id(
                target_key=target_key,
                attempt_at=end_at,
            ),
            pm_review_preflight_target_keys=(target_key,),
        )
        replay_runtime = composed.require_replay_runtime()
        failure_guard = ReplaySlowPathFailureGuard(layout=layout)
        released_count = _run_replay_rows(
            replay_runtime=replay_runtime,
            layout=layout,
            target_key=target_key,
            rows=rows,
            start_at=start_at,
            end_at=end_at,
            failure_guard=failure_guard,
        )
        replay_runtime.finalize_pipeline()
        _raise_live_catchup_slow_path_failure(
            failure_guard=failure_guard,
            replay_at=end_at,
            context="finalize_pipeline",
        )
        return released_count
    finally:
        if composed is not None:
            try:
                composed.close()
            except Exception as exc:  # pragma: no cover - cleanup guard
                print(f"Replay cleanup failed: {exc}", file=sys.stderr)
        if market_data_provider is not None:
            try:
                close_market_data = getattr(market_data_provider, "close", None)
                if callable(close_market_data):
                    close_market_data()
            except Exception as exc:  # pragma: no cover - cleanup guard
                print(f"Replay market-data cleanup failed: {exc}", file=sys.stderr)
        try:
            temp_replay_config.unlink()
        except FileNotFoundError:
            pass


def _prepare_replay_market_data(
    *,
    config: KernelConfig,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
    emit: Emitter,
) -> tuple[MarketBarsProvider | None, Path | None, str | None]:
    live_config = getattr(config, "live", None)
    if live_config is None or getattr(live_config, "market_data", None) is None:
        return None, None, None
    if not live_config.market_data.enabled:
        return None, None, None
    remote_provider, _warmup_receipt = prepare_live_market_data(
        config=config,
        layout=layout,
        as_of_at=end_at,
        emit=emit,
    )
    try:
        return _prepare_replay_market_data_from_remote(
            config=config,
            target_key=target_key,
            start_at=start_at,
            end_at=end_at,
            remote_provider=remote_provider,
            emit=emit,
        )
    finally:
        # The returned provider is pinned and must not retain the live Futu
        # connection.  Close it here on both success and every failure path.
        _close_market_data_provider(remote_provider)


def _prepare_replay_market_data_from_remote(
    *,
    config: KernelConfig,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
    remote_provider: MarketBarsProvider | None,
    emit: Emitter,
) -> tuple[MarketBarsProvider | None, Path | None, str | None]:
    live_config = getattr(config, "live", None)
    if live_config is None or getattr(live_config, "market_data", None) is None:
        return None, None, None
    market_data_config = live_config.market_data
    if not market_data_config.enabled:
        return None, None, None
    store = SharedMarketDataStore(shared_market_data_root(config))
    target_configs = tuple(
        target for target in market_data_config.targets if target.target_key == target_key
    )
    if len(target_configs) != 1:
        raise LiveCatchupError(
            "live catch-up requires exactly one live market-data target for "
            f"target_key={target_key!r}."
        )
    reads: list[tuple[MarketSeriesIdentity, datetime, datetime]] = []
    warmup_config = getattr(market_data_config, "warmup", None)
    for subscription in target_configs[0].subscriptions:
        mapping = MarketMapping(
            target_key=target_key,
            market_symbol=subscription.symbol,
            market_session=subscription.market_session,
            exchange=subscription.exchange,
            bar_granularity=subscription.bar_granularity,
            exchange_session_scope=subscription.exchange_session_scope,
        )
        identity = MarketSeriesIdentity.from_mapping(
            mapping,
            provider=subscription.provider,
            adjustment_policy=subscription.adjustment_policy or "raw",
        )
        if warmup_config is None:
            context_start_at = start_at
        elif (
            config.market_context is not None
            and config.market_context.enabled
            and target_key in config.market_context.target_profiles
        ):
            profile = MarketContextProfile.from_config(
                target_key=target_key,
                profile_config=config.market_context.target_profiles[target_key],
            )
            if subscription.symbol.upper() == profile.tradable_proxy_symbol.upper():
                context_start_at = _request_start_at(
                    profile=profile,
                    as_of_at=start_at,
                    market_session=mapping.market_session,
                )
            elif profile.macro_cross_asset is not None:
                context_start_at = _macro_request_start_at(
                    profile=profile,
                    as_of_at=start_at,
                    market_session=mapping.market_session,
                )
            else:
                context_start_at = start_at
        else:
            context_start_at = start_at
        try:
            covered = store.covers_window(
                identity,
                start_at=context_start_at,
                end_at=end_at,
            )
            if not covered and remote_provider is not None:
                synchronize = getattr(remote_provider, "synchronize", None)
                if callable(synchronize):
                    synchronize(mapping, start_at=context_start_at, end_at=end_at)
                else:
                    remote_provider.read_series(
                        mapping,
                        start_at=context_start_at,
                        end_at=end_at,
                    )
                covered = store.covers_window(
                    identity,
                    start_at=context_start_at,
                    end_at=end_at,
                )
            if not covered:
                raise SharedMarketDataError(
                    f"shared market data is missing {subscription.symbol} in "
                    f"{start_at.isoformat()}..{end_at.isoformat()}"
                )
        except (SharedMarketDataError, ValueError) as exc:
            raise LiveCatchupError(str(exc)) from exc
        reads.append((identity, context_start_at, end_at))
    snapshot = store.create_snapshot(
        mode="live_catchup",
        reads=reads,
        metadata={
            "target_key": target_key,
            "window_start": start_at.isoformat(),
            "window_end": end_at.isoformat(),
            "context_window_start": min(
                context_start.isoformat() for _identity, context_start, _end in reads
            ),
        },
    )
    provider = SharedMarketDataProvider(
        store=store,
        provider_name=(
            target_configs[0].subscriptions[0].provider
            if target_configs[0].subscriptions
            else "unknown"
        ),
        snapshot_id=snapshot.snapshot_id,
        provider_names={
            (target_key, subscription.symbol): subscription.provider
            for subscription in target_configs[0].subscriptions
        },
    )
    emit(
        "live catch-up market-data: pinned shared snapshot "
        f"snapshot_id={snapshot.snapshot_id}"
    )
    return provider, store.root, snapshot.snapshot_id


def _snapshot_market_data_provider(
    *,
    config: KernelConfig,
    target_key: str,
    market_data_store_root: Path | None,
    market_data_snapshot_id: str,
) -> SharedMarketDataProvider:
    """Reopen one already-pinned snapshot for preflight or release."""
    store = SharedMarketDataStore(
        market_data_store_root or shared_market_data_root(config)
    )
    market_data = config.live.market_data if config.live is not None else None
    target = None if market_data is None else next(
        (item for item in market_data.targets if item.target_key == target_key),
        None,
    )
    provider_names = {} if target is None else {
        (target_key, subscription.symbol): subscription.provider
        for subscription in target.subscriptions
    }
    return SharedMarketDataProvider(
        store=store,
        provider_name="shared_market_data",
        snapshot_id=market_data_snapshot_id,
        provider_names=provider_names,
    )


def _close_market_data_provider(provider: object | None) -> None:
    if provider is None:
        return
    close = getattr(provider, "close", None)
    if callable(close):
        close()


@dataclass(frozen=True, slots=True)
class _ReplayRow:
    ingress_input: ReplayIngressInput
    labels: tuple[str, ...]


def _adapt_replay_row(raw_payload: dict[str, object], *, line_number: int) -> _ReplayRow:
    source_shape = raw_payload.get("source_shape")
    if not isinstance(source_shape, str) or not source_shape.strip():
        raise LiveCatchupError(f"Replay row {line_number} is missing source_shape.")
    payload = dict(raw_payload)
    labels = tuple(
        validate_labels(
            cast(list[str], payload.pop("labels", [])),
            error_type=LiveCatchupError,
        )
    )
    payload.pop("source_shape")
    normalized_payload = {
        key: _parse_replay_row_timestamp(value) if _is_timestamp_field(key, value) else value
        for key, value in payload.items()
    }
    adapter = replay_adapter_for(cast(ReplaySourceShape, source_shape))
    return _ReplayRow(
        ingress_input=adapter(normalized_payload),
        labels=labels,
    )


def _run_replay_rows(
    *,
    replay_runtime,
    layout: WorkspaceLayout,
    target_key: str,
    rows: tuple[_ReplayRow, ...],
    start_at: datetime,
    end_at: datetime,
    failure_guard: ReplaySlowPathFailureGuard,
) -> int:
    run_key = live_catchup_chain_run_key(target_key=target_key)
    repair_report = _plan_repair_after_resuming_analysis_backlog(
        replay_runtime=replay_runtime,
        layout=layout,
        target_key=target_key,
        start_at=start_at,
        failure_guard=failure_guard,
    )
    if repair_report.status == "blocked":
        reasons = "; ".join(blocker.reason for blocker in repair_report.blockers)
        raise LiveCatchupError(
            "live catch-up replay repair is blocked before retry: "
            f"{reasons}"
        )
    if repair_report.status == "dry_run":
        repair_report = repair_live_catchup_runtime(
            layout=layout,
            target_key=target_key,
            start_at=start_at,
            apply=True,
        )
    latest_checkpoints = _read_reusable_live_catchup_checkpoints(
        layout=layout,
        target_key=target_key,
        run_key=run_key,
    )
    released_count = 0
    for row in rows:
        replay_at = replay_ts_event(row.ingress_input)
        event_id = _derive_replay_event_id(
            target_key=target_key,
            replay_at=replay_at,
            row=row,
        )
        existing_checkpoint = latest_checkpoints.get(event_id)
        if existing_checkpoint is not None:
            if existing_checkpoint.status == "completed":
                _advance_live_catchup_deferred_until(
                    replay_runtime=replay_runtime,
                    replay_at=replay_at,
                    include_boundary=True,
                    failure_guard=failure_guard,
                    context="completed_checkpoint",
                )
                continue
            raise LiveCatchupError(
                "live catch-up replay has an unfinished checkpoint; repair before "
                f"resuming: event_id={event_id} status={existing_checkpoint.status} "
                f"stage={existing_checkpoint.stage}"
            )
        _advance_live_catchup_deferred_until(
            replay_runtime=replay_runtime,
            replay_at=replay_at,
            include_boundary=False,
            failure_guard=failure_guard,
            context="advance_before_event",
        )
        append_replay_checkpoint(
            layout,
            ReplayEventCheckpoint(
                run_key=run_key,
                target_key=target_key,
                event_id=event_id,
                replay_at=replay_at,
                source_kind=row.ingress_input.source_shape,
                source_ref=row.ingress_input.source_ref,
                stage="live_catchup_replay",
                status="started",
                error=None,
                recorded_at=datetime.now(UTC),
            ),
        )
        try:
            replay_runtime.run_step(
                target_key=target_key,
                replay_at=replay_at,
                historical_inputs=(row.ingress_input,),
                labels=list(row.labels),
                ts_init=replay_at,
            )
            _advance_live_catchup_deferred_until(
                replay_runtime=replay_runtime,
                replay_at=replay_at,
                include_boundary=True,
                failure_guard=failure_guard,
                context="event_boundary",
            )
        except Exception as exc:
            append_replay_checkpoint(
                layout,
                ReplayEventCheckpoint(
                    run_key=run_key,
                    target_key=target_key,
                    event_id=event_id,
                    replay_at=replay_at,
                    source_kind=row.ingress_input.source_shape,
                    source_ref=row.ingress_input.source_ref,
                    stage="live_catchup_replay",
                    status="failed",
                    error=str(exc),
                    recorded_at=datetime.now(UTC),
                ),
            )
            raise
        append_replay_checkpoint(
            layout,
            ReplayEventCheckpoint(
                run_key=run_key,
                target_key=target_key,
                event_id=event_id,
                replay_at=replay_at,
                source_kind=row.ingress_input.source_shape,
                source_ref=row.ingress_input.source_ref,
                stage="live_catchup_replay",
                status="completed",
                error=None,
                recorded_at=datetime.now(UTC),
            ),
        )
        latest_checkpoints[event_id] = ReplayEventCheckpoint(
            run_key=run_key,
            target_key=target_key,
            event_id=event_id,
            replay_at=replay_at,
            source_kind=row.ingress_input.source_shape,
            source_ref=row.ingress_input.source_ref,
            stage="live_catchup_replay",
            status="completed",
            error=None,
            recorded_at=datetime.now(UTC),
        )
        released_count += 1
    _advance_live_catchup_deferred_until(
        replay_runtime=replay_runtime,
        replay_at=end_at,
        include_boundary=True,
        failure_guard=failure_guard,
        context="advance_after_window",
    )
    return released_count


def _plan_repair_after_resuming_analysis_backlog(
    *,
    replay_runtime,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    failure_guard: ReplaySlowPathFailureGuard,
):
    """Give durable analysis work one production retry before generic repair."""
    repair_report = repair_live_catchup_runtime(
        layout=layout,
        target_key=target_key,
        start_at=start_at,
        apply=False,
    )
    plan = repair_report.plan
    has_analysis_truth_blocker = (
        repair_report.status == "blocked"
        and plan is not None
        and any(item.stage == "analysis_truth" for item in plan.blocked_artifacts)
    )
    if not has_analysis_truth_blocker or repair_report.repair_replay_at is None:
        return repair_report

    _resume_live_catchup_runtime_backlog(
        replay_runtime=replay_runtime,
        replay_at=repair_report.repair_replay_at,
        failure_guard=failure_guard,
        context="resume_analysis_backlog",
    )
    repair_report = repair_live_catchup_runtime(
        layout=layout,
        target_key=target_key,
        start_at=start_at,
        apply=False,
    )
    if _complete_checkpoint_for_recovered_analysis(
        repair_report=repair_report,
        layout=layout,
        target_key=target_key,
    ):
        return repair_live_catchup_runtime(
            layout=layout,
            target_key=target_key,
            start_at=start_at,
            apply=False,
        )
    return repair_report


def _advance_live_catchup_deferred_until(
    *,
    replay_runtime,
    replay_at: datetime,
    include_boundary: bool,
    failure_guard: ReplaySlowPathFailureGuard,
    context: str,
) -> None:
    try:
        advance_replay_deferred_until(
            replay_runtime=replay_runtime,
            replay_at=replay_at,
            include_boundary=include_boundary,
            failure_guard=failure_guard,
            context=context,
        )
    except ReplaySlowPathFailureError as exc:
        raise LiveCatchupError(str(exc)) from exc


def _resume_live_catchup_runtime_backlog(
    *,
    replay_runtime,
    replay_at: datetime,
    failure_guard: ReplaySlowPathFailureGuard,
    context: str,
) -> None:
    try:
        resume_replay_runtime_backlog(
            replay_runtime=replay_runtime,
            replay_at=replay_at,
            failure_guard=failure_guard,
            context=context,
        )
    except ReplaySlowPathFailureError as exc:
        raise LiveCatchupError(str(exc)) from exc


def _raise_live_catchup_slow_path_failure(
    *,
    failure_guard: ReplaySlowPathFailureGuard,
    replay_at: datetime,
    context: str,
) -> None:
    try:
        failure_guard.raise_if_new_failure(replay_at=replay_at, context=context)
    except ReplaySlowPathFailureError as exc:
        raise LiveCatchupError(str(exc)) from exc


def _complete_checkpoint_for_recovered_analysis(
    *,
    repair_report,
    layout: WorkspaceLayout,
    target_key: str,
) -> bool:
    plan = repair_report.plan
    if (
        repair_report.status != "blocked"
        or repair_report.repair_replay_at is None
        or plan is None
        or {item.stage for item in plan.blocked_artifacts} != {"analysis_truth"}
    ):
        return False

    run_key = live_catchup_chain_run_key(target_key=target_key)
    latest_checkpoints = _read_reusable_live_catchup_checkpoints(
        layout=layout,
        target_key=target_key,
        run_key=run_key,
    )
    candidates = tuple(
        checkpoint
        for checkpoint in latest_checkpoints.values()
        if checkpoint.stage == "live_catchup_replay"
        and checkpoint.status in {"started", "failed"}
        and checkpoint.replay_at == repair_report.repair_replay_at
    )
    if not candidates:
        return False

    for checkpoint in candidates:
        recovery = plan_analysis_recovery(
            layout=layout,
            target_key=target_key,
            event_id=checkpoint.event_id,
        )
        if recovery.decision_count != 1:
            return False
        decision = recovery.decisions[0]
        if (
            decision.event_id != checkpoint.event_id
            or decision.planned_action != "no_action"
            or decision.completion_state != "complete"
            or decision.commit_status != "complete"
            or decision.rerun_allowed
        ):
            return False

    recorded_at = datetime.now(UTC)
    for checkpoint in candidates:
        append_replay_checkpoint(
            layout,
            ReplayEventCheckpoint(
                run_key=checkpoint.run_key,
                target_key=checkpoint.target_key,
                event_id=checkpoint.event_id,
                replay_at=checkpoint.replay_at,
                source_kind=checkpoint.source_kind,
                source_ref=checkpoint.source_ref,
                stage=checkpoint.stage,
                status="completed",
                error=None,
                recorded_at=recorded_at,
            ),
        )
    return True


def _derive_replay_event_id(
    *,
    target_key: str,
    replay_at: datetime,
    row: _ReplayRow,
) -> str:
    request = validate_replay_admission_request(
        target_key=target_key,
        ingress_input=row.ingress_input,
        replay_at=replay_at,
        labels=list(row.labels),
    )
    return derive_admission_event_id(request)


def _sync_live_source_bookkeeping_checkpoints(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    channels: tuple[LiveCatchupChannel, ...],
    end_at: datetime,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    if "market_news" in channels:
        paths.append(
            _write_live_source_bookkeeping_checkpoint(
                layout=layout,
                target_key=target_key,
                channel="market_news",
                last_successful_batch_end=end_at,
            )
        )
    return tuple(paths)


def _write_live_source_bookkeeping_checkpoint(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    channel: LiveCatchupChannel,
    last_successful_batch_end: datetime,
) -> Path:
    path = advance_live_source_checkpoint(
        layout,
        target_key=target_key,
        channel=channel,
        last_successful_batch_end=last_successful_batch_end,
    )
    expected = live_source_checkpoint_path(layout, target_key=target_key, channel=channel)
    if path != expected:
        raise LiveCatchupError("live source checkpoint writer returned an unexpected path.")
    return path


def _write_manifest(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    start_date: str,
    start_timezone: str,
    start_at: datetime,
    end_at: datetime,
    channels: tuple[LiveCatchupChannel, ...],
    web_search_mode: WebSearchCatchupMode,
    record_counts: dict[str, int],
    released_count: int,
    checkpoint_paths: tuple[Path, ...],
    resume_live_requested: bool,
    workspace_auto_prepared: bool,
    status: str,
) -> Path:
    root = layout.runtime_root / "live_catchup" / target_key
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_timestamp_token(end_at)}.json"
    payload = {
        "target_key": target_key,
        "start_date": start_date,
        "start_timezone": start_timezone,
        "start_at": start_at.isoformat(),
        "resolved_start_at_utc": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "channels": list(channels),
        "web_search_mode": web_search_mode,
        "record_counts": record_counts,
        "released_count": released_count,
        "checkpoint_paths": [str(path) for path in checkpoint_paths],
        "resume_live_requested": resume_live_requested,
        "workspace_auto_prepared": workspace_auto_prepared,
        "status": status,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path.resolve(strict=False)


def _write_temp_replay_config(
    *,
    original_config_path: Path,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
) -> Path:
    raw_config = original_config_path.read_text(encoding="utf-8")
    replay_config, replacements = re.subn(
        r'(?m)^(\s*mode\s*=\s*)"live"(\s*)$',
        r'\1"replay"\2',
        raw_config,
        count=1,
    )
    if replacements != 1:
        raise LiveCatchupError("Could not derive replay config from live config mode line.")
    temp_path = (
        original_config_path.parent
        / f".live_catchup_{target_key}_{_timestamp_token(start_at)}_{_timestamp_token(end_at)}.toml"
    ).resolve(strict=False)
    temp_path.write_text(replay_config, encoding="utf-8")
    return temp_path


def _market_mapping_from_config(
    *,
    config: KernelConfig,
    target_key: str,
) -> MarketMapping:
    if config.validation is None:
        raise LiveCatchupError(
            "live catch-up replay requires validation.market_mappings configuration."
        )
    mapping = config.validation.market_mappings.get(target_key)
    if mapping is None:
        raise LiveCatchupError(
            "live catch-up replay requires validation.market_mappings entry for "
            f"target_key={target_key!r}."
        )
    return MarketMapping(
        target_key=target_key,
        market_symbol=mapping.market_symbol,
        market_session=mapping.market_session,
        exchange=mapping.exchange,
        bar_granularity=mapping.bar_granularity,
        exchange_session_scope=mapping.exchange_session_scope,
    )


def _single_target(targets, *, target_key: str, channel: str):
    matches = [target for target in targets if target.target_key == target_key]
    if len(matches) != 1:
        raise LiveCatchupError(
            f"Expected exactly one live.{channel} target for target_key={target_key!r}."
        )
    return matches[0]


def _validate_utc_now(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise LiveCatchupError("now must return a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise LiveCatchupError("now must return a timezone-aware datetime.")
    return value.astimezone(UTC)


def _is_timestamp_field(key: str, value: object) -> bool:
    return key.endswith("_at") and isinstance(value, str)


def _parse_replay_row_timestamp(value: object) -> datetime:
    if value is None:
        raise LiveCatchupError("timestamp parser received None.")
    if not isinstance(value, str):
        raise LiveCatchupError("timestamp parser requires an ISO8601 string.")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _read_reusable_live_catchup_checkpoints(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_key: str,
) -> dict[str, ReplayEventCheckpoint]:
    latest: dict[str, ReplayEventCheckpoint] = {}
    completed: dict[str, ReplayEventCheckpoint] = {}
    for persisted in read_persisted_replay_checkpoints(layout, target_key=target_key):
        checkpoint = persisted.checkpoint
        if not matches_live_catchup_chain_run_key(
            candidate_run_key=checkpoint.run_key,
            chain_run_key=run_key,
        ):
            continue
        latest[checkpoint.event_id] = checkpoint
        if checkpoint.status == "completed":
            completed[checkpoint.event_id] = checkpoint
    return {
        event_id: completed.get(event_id, checkpoint)
        for event_id, checkpoint in latest.items()
    }


def _timestamp_token(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
