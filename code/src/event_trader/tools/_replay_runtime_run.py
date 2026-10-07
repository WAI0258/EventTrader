"""Replay runtime run-path orchestration extracted from the CLI shell module."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from event_trader.projection import generate_current_state_report
from event_trader.replay.checkpoint import (
    ReplayEventCheckpoint,
    append_replay_checkpoint,
    read_latest_replay_checkpoints,
)
from event_trader.replay.slow_path_failure import (
    ReplaySlowPathFailureGuard as _ReplaySlowPathFailureGuard,
)
from event_trader.replay.slow_path_failure import (
    advance_replay_deferred_until as _advance_replay_deferred_until,
)
from event_trader.replay.slow_path_failure import (
    resume_replay_runtime_backlog as _resume_replay_runtime_backlog,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from event_trader.replay.runner import ReplayStepReceipt
    from event_trader.storage import WorkspaceLayout
    from event_trader.tools import replay_runtime


def run_replay_command(args: argparse.Namespace) -> int:
    from event_trader.composition import compose_kernel
    from event_trader.tools import replay_runtime as runtime_tool

    target_key = runtime_tool.validate_target_key(
        args.target_key,
        error_type=runtime_tool.ReplayRuntimeToolError,
    )
    window_start = runtime_tool._parse_window_boundary(args.window_start, end_of_day=False)
    window_end = runtime_tool._parse_window_boundary(args.window_end, end_of_day=True)
    reflection_end = (
        window_end
        if args.reflection_end is None
        else runtime_tool._parse_window_boundary(args.reflection_end, end_of_day=True)
    )
    if reflection_end < window_end:
        raise runtime_tool.ReplayRuntimeToolError(
            "reflection_end must be greater than or equal to window_end."
        )

    dataset_arg = args.dataset or args.input_dataset
    if dataset_arg is None:
        raise runtime_tool.ReplayRuntimeToolError("Replay run requires --dataset or --input.")
    dataset_path = Path(dataset_arg).resolve(strict=False)
    config_path = Path(args.config).resolve(strict=False)
    kernel_config = runtime_tool.load_kernel_config(config_path)
    replay_market_mapping = runtime_tool._resolve_replay_market_mapping(
        config=kernel_config,
        target_key=target_key,
        market_symbol=args.market_symbol,
        market_session=args.market_session,
        exchange=args.exchange,
        exchange_session_scope=args.exchange_session_scope,
        bar_granularity=args.bar_granularity,
    )
    global_labels = tuple(
        runtime_tool.validate_labels(args.label, error_type=runtime_tool.ReplayRuntimeToolError)
    )
    stage_tracker = runtime_tool._ReplayPipelineStageTracker()
    dataset_rows = runtime_tool._load_dataset(dataset_path)
    dataset_source_classification_counts = (
        runtime_tool._source_classification_counts_for_dataset_rows(dataset_rows)
    )
    checker_policy, analysis_callback = runtime_tool._resolve_primary_research_overrides(
        cast(runtime_tool._ReplayPrimaryResearchMode, args.primary_research_mode)
    )
    run_id = args.run_id or runtime_tool._default_replay_run_id(
        target_key=target_key,
        window_start=window_start,
        window_end=window_end,
    )
    prefetch_setup = runtime_tool._prepare_replay_market_prefetch(
        config_path=config_path,
        target_key=target_key,
        run_id=run_id,
        dataset_rows=tuple(dataset_rows),
        window_start=window_start,
        window_end=window_end,
        reflection_end=reflection_end,
    )
    if prefetch_setup.receipt is not None:
        print(
            "market prefetch: "
            f"target_key={target_key} "
            f"run_id={run_id} "
            f"reused_existing={prefetch_setup.receipt.reused_existing} "
            f"snapshot_id={prefetch_setup.receipt.snapshot_id} "
            f"store={prefetch_setup.receipt.store.database_path.as_posix()}"
        )
    if args.primary_research_mode != "production":
        print(
            "primary research mode: "
            f"{args.primary_research_mode} target_key={target_key} run_id={run_id}"
        )
    if runtime_tool._ensure_replay_pm_review_workspace_ready(
        config_path=config_path,
        target_key=target_key,
        migrated_at=window_start,
    ):
        print(f"pm review workspace auto-prepared: target_key={target_key}")

    composed = None
    try:
        composed = compose_kernel(
            config_path,
            emit=print,
            pipeline_stage_observer=stage_tracker.observe,
            outcome_context=runtime_tool._NoopOutcomeContextPort(),
            evaluate_review=runtime_tool._unexpected_shared_review,
            market_data_provider_override=prefetch_setup.market_data_provider_override,
            market_data_store_root=prefetch_setup.market_data_store_root,
            market_data_snapshot_id=prefetch_setup.market_data_snapshot_id,
            replay_run_id=run_id,
            pm_review_preflight_target_keys=(target_key,),
            replay_market_mapping=replay_market_mapping,
            checker_policy=checker_policy,
            analysis_callback=analysis_callback,
        )
        replay_runtime = composed.require_replay_runtime()
        nautilus_node = composed.require_nautilus_node()
        layout = composed.workspace.layout
        if layout is None:
            raise runtime_tool.ReplayRuntimeToolError("Workspace layout is required for replay.")
        reflection_driver = runtime_tool._build_replay_reflection_cycle_driver(
            replay_runtime=replay_runtime,
            clock=nautilus_node.clock,
            reflection_start=window_start,
            step_hours=args.reflection_step_hours,
        )
        failure_guard = _ReplaySlowPathFailureGuard(layout=layout)
        step_receipts, step_stats = run_replay_steps(
            replay_runtime=replay_runtime,
            layout=layout,
            config=composed.config,
            target_key=target_key,
            dataset_rows=dataset_rows,
            global_labels=global_labels,
            window_start=window_start,
            window_end=window_end,
            run_id=run_id,
            auto_repair_failed_checkpoint=bool(args.auto_repair_failed_checkpoint),
            log_skipped_checkpoints=bool(args.log_skipped_checkpoints),
            stage_tracker=stage_tracker,
            reflection_driver=reflection_driver,
            failure_guard=failure_guard,
        )
        try:
            _advance_replay_deferred_until(
                replay_runtime=replay_runtime,
                replay_at=window_end,
                include_boundary=True,
                failure_guard=failure_guard,
                context="advance_after_window",
            )
        except RuntimeError as exc:
            raise runtime_tool.ReplayRuntimeToolError(str(exc)) from exc
        replay_runtime.finalize_pipeline()
        try:
            failure_guard.raise_if_new_failure(
                replay_at=window_end,
                context="finalize_pipeline",
            )
        except RuntimeError as exc:
            raise runtime_tool.ReplayRuntimeToolError(str(exc)) from exc
        report = generate_current_state_report(target_key=target_key, layout=layout)
        reflection_driver.advance_after_window(
            window_end=window_end,
            reflection_end=reflection_end,
        )
        reflection_receipts = reflection_driver.receipts
        open_position_terminal_summary, _terminal_block = (
            runtime_tool._build_open_position_terminal_outputs(
                config=composed.config,
                layout=layout,
                target_key=target_key,
                replay_end_at=window_end,
                market_mapping=replay_market_mapping,
                market_data_base_url_override=args.market_data_base_url,
                market_data_api_key_env_override=args.market_data_api_key_env,
                market_data_timeout_seconds_override=args.market_data_timeout_seconds,
                market_data_provider_override=prefetch_setup.market_data_provider_override,
            )
        )
        current_state_artifact_path = report.receipt.artifact_path
        if current_state_artifact_path is None:
            raise runtime_tool.ReplayRuntimeToolError(
                "Current-state report did not produce an artifact path."
            )
        runtime_tool._print_run_summary(
            layout=layout,
            target_key=target_key,
            step_receipts=step_receipts,
            step_stats=step_stats,
            reflection_receipts=reflection_receipts,
            current_state_artifact_path=current_state_artifact_path,
            open_position_terminal_summary=open_position_terminal_summary,
            source_classification_counts=dataset_source_classification_counts,
        )
        return 0
    finally:
        if composed is not None:
            try:
                composed.close()
            except Exception as exc:  # pragma: no cover - cleanup guard
                print(f"replay cleanup failed: {exc}", file=sys.stderr)


def run_replay_steps(
    *,
    replay_runtime,
    layout: WorkspaceLayout,
    config,
    target_key: str,
    dataset_rows: Sequence[replay_runtime._ReplayDatasetRow],
    global_labels: tuple[str, ...],
    window_start: datetime,
    window_end: datetime,
    run_id: str,
    auto_repair_failed_checkpoint: bool,
    log_skipped_checkpoints: bool,
    stage_tracker: replay_runtime._ReplayPipelineStageTracker,
    reflection_driver: replay_runtime._ReplayReflectionCycleDriver | None = None,
    failure_guard: _ReplaySlowPathFailureGuard,
) -> tuple[tuple[ReplayStepReceipt, ...], replay_runtime._ReplayRunStepStats]:
    from event_trader.tools import replay_runtime as runtime_tool

    selected_rows = tuple(
        row
        for row in dataset_rows
        if window_start <= runtime_tool.replay_ts_event(row.ingress_input) <= window_end
    )
    if not selected_rows:
        raise runtime_tool.ReplayRuntimeToolError(
            "No dataset rows fall inside the requested replay window."
        )

    run_key = runtime_tool._replay_run_key(
        target_key=target_key,
        window_start=window_start,
        window_end=window_end,
    )
    latest_checkpoints = read_latest_replay_checkpoints(
        layout,
        target_key=target_key,
        run_key=run_key,
    )
    step_receipts: list[ReplayStepReceipt] = []
    skipped_count = 0
    quarantined_count = 0
    quarantine_path: Path | None = None
    last_completed_replay_at = None
    pending_completed_replay_at = None
    reflection_fast_forwarded = False

    def mark_completed_checkpoint_skip(replay_at: datetime) -> None:
        nonlocal last_completed_replay_at, pending_completed_replay_at
        last_completed_replay_at = replay_at
        pending_completed_replay_at = replay_at

    def flush_completed_checkpoint_skips() -> None:
        nonlocal pending_completed_replay_at
        if pending_completed_replay_at is None:
            return
        try:
            _advance_replay_deferred_until(
                replay_runtime=replay_runtime,
                replay_at=pending_completed_replay_at,
                include_boundary=True,
                failure_guard=failure_guard,
                context="completed_checkpoint_resume_fast_forward",
            )
        except RuntimeError as exc:
            raise runtime_tool.ReplayRuntimeToolError(str(exc)) from exc
        pending_completed_replay_at = None

    def advance_before_uncheckpointed_event(replay_at: datetime) -> None:
        flush_completed_checkpoint_skips()
        try:
            _advance_replay_deferred_until(
                replay_runtime=replay_runtime,
                replay_at=replay_at,
                include_boundary=False,
                failure_guard=failure_guard,
                context="advance_before_event",
            )
        except RuntimeError as exc:
            raise runtime_tool.ReplayRuntimeToolError(str(exc)) from exc

    def fast_forward_reflection_once() -> None:
        nonlocal reflection_fast_forwarded
        if (
            reflection_driver is None
            or last_completed_replay_at is None
            or reflection_fast_forwarded
        ):
            return
        reflection_driver.fast_forward_after(last_completed_replay_at)
        reflection_fast_forwarded = True

    for row in selected_rows:
        replay_at = runtime_tool.replay_ts_event(row.ingress_input)
        merged_labels = runtime_tool._merge_labels(row.labels, global_labels)
        future_leakage = runtime_tool._historical_web_search_future_leakage(row.ingress_input)
        if future_leakage is not None:
            event_id = runtime_tool._derive_quarantined_replay_event_id(
                target_key=target_key,
                row=row,
                labels=merged_labels,
            )
            latest_checkpoint = latest_checkpoints.get(event_id)
            if latest_checkpoint is not None and latest_checkpoint.status == "completed":
                mark_completed_checkpoint_skip(replay_at)
                quarantined_count += 1
                if log_skipped_checkpoints:
                    print(
                        "replay step skipped:",
                        f"event_id={event_id}",
                        f"replay_at={replay_at.isoformat()}",
                        "reason=checkpoint_completed",
                    )
                continue
            advance_before_uncheckpointed_event(replay_at)
            _resume_replay_runtime_backlog(
                replay_runtime=replay_runtime,
                replay_at=replay_at,
                failure_guard=failure_guard,
                context="resume_runtime_backlog",
            )
            fast_forward_reflection_once()
            runtime_tool._maybe_cutover_active_price_basis_for_replay(
                layout=layout,
                config=config,
                target_key=target_key,
                replay_at=replay_at,
            )
            if reflection_driver is not None:
                reflection_driver.advance_before(replay_at)
            if latest_checkpoint is not None:
                runtime_tool._repair_unfinished_checkpoint_or_raise(
                    layout=layout,
                    latest_checkpoints=latest_checkpoints,
                    latest_checkpoint=latest_checkpoint,
                    target_key=target_key,
                    run_key=run_key,
                    run_id=run_id,
                    auto_repair=auto_repair_failed_checkpoint,
                )
            append_replay_checkpoint(
                layout,
                quarantined_checkpoint := ReplayEventCheckpoint(
                    run_key=run_key,
                    target_key=target_key,
                    event_id=event_id,
                    replay_at=replay_at,
                    source_kind=row.ingress_input.source_shape,
                    source_ref=row.ingress_input.source_ref,
                    stage="quarantined",
                    status="completed",
                    error=None,
                    recorded_at=datetime.now(tz=UTC),
                ),
            )
            latest_checkpoints[event_id] = quarantined_checkpoint
            quarantine_path = runtime_tool._append_replay_quarantine_receipt(
                layout=layout,
                target_key=target_key,
                event_id=event_id,
                replay_at=replay_at,
                ingress_input=row.ingress_input,
                finding=future_leakage,
            )
            try:
                _advance_replay_deferred_until(
                    replay_runtime=replay_runtime,
                    replay_at=replay_at,
                    include_boundary=True,
                    failure_guard=failure_guard,
                    context="quarantined_event",
                )
            except RuntimeError as exc:
                raise runtime_tool.ReplayRuntimeToolError(str(exc)) from exc
            quarantined_count += 1
            print(
                "replay step quarantined:",
                f"event_id={event_id}",
                f"replay_at={replay_at.isoformat()}",
                f"reason={future_leakage.reason_code}",
            )
            if reflection_driver is not None:
                reflection_driver.advance_after_event(replay_at)
            continue
        event_id = runtime_tool._derive_replay_event_id(
            target_key=target_key,
            replay_at=replay_at,
            row=row,
            labels=merged_labels,
        )
        latest_checkpoint = latest_checkpoints.get(event_id)
        if latest_checkpoint is not None and latest_checkpoint.status == "completed":
            mark_completed_checkpoint_skip(replay_at)
            skipped_count += 1
            if log_skipped_checkpoints:
                print(
                    "replay step skipped:",
                    f"event_id={event_id}",
                    f"replay_at={replay_at.isoformat()}",
                    "reason=checkpoint_completed",
                )
            continue
        advance_before_uncheckpointed_event(replay_at)
        _resume_replay_runtime_backlog(
            replay_runtime=replay_runtime,
            replay_at=replay_at,
            failure_guard=failure_guard,
            context="resume_runtime_backlog",
        )
        fast_forward_reflection_once()
        runtime_tool._maybe_cutover_active_price_basis_for_replay(
            layout=layout,
            config=config,
            target_key=target_key,
            replay_at=replay_at,
        )
        if latest_checkpoint is not None:
            runtime_tool._repair_unfinished_checkpoint_or_raise(
                layout=layout,
                latest_checkpoints=latest_checkpoints,
                latest_checkpoint=latest_checkpoint,
                target_key=target_key,
                run_key=run_key,
                run_id=run_id,
                auto_repair=auto_repair_failed_checkpoint,
            )
        if reflection_driver is not None:
            reflection_driver.advance_before(replay_at)
        stage_tracker.start_event(event_id)
        started_checkpoint = ReplayEventCheckpoint(
            run_key=run_key,
            target_key=target_key,
            event_id=event_id,
            replay_at=replay_at,
            source_kind=row.ingress_input.source_shape,
            source_ref=row.ingress_input.source_ref,
            stage="started",
            status="started",
            error=None,
            recorded_at=datetime.now(tz=UTC),
        )
        append_replay_checkpoint(layout, started_checkpoint)
        latest_checkpoints[event_id] = started_checkpoint
        try:
            receipt = replay_runtime.run_step(
                target_key=target_key,
                replay_at=replay_at,
                historical_inputs=(row.ingress_input,),
                labels=list(merged_labels),
                ts_init=replay_at,
            )
            _advance_replay_deferred_until(
                replay_runtime=replay_runtime,
                replay_at=replay_at,
                include_boundary=True,
                failure_guard=failure_guard,
                context="post_event_deferred",
            )
        except Exception as exc:
            stage = stage_tracker.stage_for(event_id)
            append_replay_checkpoint(
                layout,
                ReplayEventCheckpoint(
                    run_key=run_key,
                    target_key=target_key,
                    event_id=event_id,
                    replay_at=replay_at,
                    source_kind=row.ingress_input.source_shape,
                    source_ref=row.ingress_input.source_ref,
                    stage=stage,
                    status="failed",
                    error=str(exc),
                    recorded_at=datetime.now(tz=UTC),
                ),
            )
            raise runtime_tool.ReplayRuntimeToolError(
                "replay event pipeline failed: "
                f"event_id={event_id} replay_at={replay_at.isoformat()} "
                f"stage={stage} error={exc}"
            ) from exc
        append_replay_checkpoint(
            layout,
            completed_checkpoint := ReplayEventCheckpoint(
                run_key=run_key,
                target_key=target_key,
                event_id=event_id,
                replay_at=replay_at,
                source_kind=row.ingress_input.source_shape,
                source_ref=row.ingress_input.source_ref,
                stage="completed",
                status="completed",
                error=None,
                recorded_at=datetime.now(tz=UTC),
            ),
        )
        latest_checkpoints[event_id] = completed_checkpoint
        print(
            "replay step:",
            f"replay_at={replay_at.isoformat()}",
            f"source_ref={row.ingress_input.source_ref!r}",
            f"delivered_event_ids={list(receipt.delivered_event_ids)!r}",
        )
        step_receipts.append(receipt)
        if reflection_driver is not None:
            reflection_driver.advance_after_event(replay_at)
    flush_completed_checkpoint_skips()
    fast_forward_reflection_once()
    checkpoint_path = (
        layout.runtime_root / "replay_run" / target_key / "events.jsonl"
    ).resolve(strict=False)
    return tuple(step_receipts), runtime_tool._ReplayRunStepStats(
        completed_count=len(step_receipts),
        skipped_count=skipped_count,
        quarantined_count=quarantined_count,
        failed_count=0,
        checkpoint_path=checkpoint_path,
        quarantine_path=quarantine_path,
    )


