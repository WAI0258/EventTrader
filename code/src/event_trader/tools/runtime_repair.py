"""Operator-facing runtime repair commands for live and replay workspaces."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path

from event_trader.audit import LiveSmokeTraceBundleError
from event_trader.config import (
    BootstrapConfigError,
    load_kernel_config,
    resolve_validation_execution_direction_mode,
)
from event_trader.migrations import (
    ActivePriceBasisCutoverError,
    ActivePriceBasisRepairError,
    AnalysisDirectionPolicyRepairError,
    repair_active_price_basis_chain,
    repair_analysis_direction_policy,
    run_active_price_basis_cutover_from_config,
)
from event_trader.operator.repair import (
    OperatorRepairError,
    apply_replay_repair_plan,
    default_replay_run_id,
    plan_replay_failed_event_repair,
    replay_run_key,
)
from event_trader.operator.repair.runtime_analysis import RuntimeRepairInspectionError
from event_trader.operator.repair.runtime_reflection import ReflectionRecoveryError
from event_trader.operator.research_memory_migration import ResearchMemoryMigrationError
from event_trader.storage import WorkspaceLayout, build_workspace_layout
from event_trader.tools._runtime_repair_live import (
    register_runtime_repair_subcommands,
    run_runtime_repair_command,
)
from event_trader.workspace import WorkspaceBootstrapError, bootstrap_workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m event_trader.tools.runtime_repair",
        description="Plan and apply runtime repair actions for event-trader workspaces.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay_parser = subparsers.add_parser(
        "replay-failed-event",
        help="Plan or apply repair for the latest unfinished replay event.",
    )
    replay_parser.add_argument(
        "--config",
        default="config/kernel.replay.gold.toml",
        help="Replay kernel config path used to resolve the workspace root.",
    )
    replay_parser.add_argument(
        "--workspace-root",
        help="Optional workspace root override for an isolated replay workspace.",
    )
    replay_parser.add_argument("--target-key", default="gold")
    replay_parser.add_argument("--window-start", required=True)
    replay_parser.add_argument("--window-end", required=True)
    replay_parser.add_argument(
        "--run-id",
        help="Replay run id used by replay-scoped context packets.",
    )
    replay_parser.add_argument(
        "--event-id",
        help="Optional event id. Defaults to the latest started/failed checkpoint.",
    )
    replay_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the repair. Omit for dry-run planning.",
    )
    register_runtime_repair_subcommands(subparsers)
    cutover_parser = subparsers.add_parser(
        "cutover-active-price-basis",
        help="Deterministically cut over one target's active chain to the current price basis.",
    )
    cutover_parser.add_argument(
        "--config",
        default="config/kernel.replay.gold.toml",
        help="Kernel config path used to resolve validation market mapping and workspace root.",
    )
    cutover_parser.add_argument(
        "--workspace-root",
        help="Optional workspace root override.",
    )
    cutover_parser.add_argument("--target-key", required=True)
    cutover_parser.add_argument(
        "--cutover-at",
        required=True,
        help="Timezone-aware ISO8601 timestamp for the active-chain cutover boundary.",
    )
    cutover_parser.add_argument(
        "--legacy-policy",
        help="Required for pointerless legacy workspaces, for example raw.",
    )
    repair_parser = subparsers.add_parser(
        "repair-active-price-basis-chain",
        help="Rewrite post-cutover analysis assessments and active sections onto the current active basis.",
    )
    repair_parser.add_argument(
        "--config",
        default="config/kernel.replay.gold.toml",
        help="Kernel config path used to resolve validation market mapping and workspace root.",
    )
    repair_parser.add_argument(
        "--workspace-root",
        help="Optional workspace root override.",
    )
    repair_parser.add_argument("--target-key", required=True)
    direction_policy_parser = subparsers.add_parser(
        "repair-analysis-direction-policy",
        help="Deterministically repair long-only analysis direction-policy pollution.",
    )
    direction_policy_parser.add_argument(
        "--config",
        default="config/kernel.replay.gold.toml",
        help="Kernel config path used to resolve target execution_direction_mode and workspace root.",
    )
    direction_policy_parser.add_argument(
        "--workspace-root",
        help="Optional workspace root override.",
    )
    direction_policy_parser.add_argument("--target-key", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "replay-failed-event":
            return _run_replay_failed_event(args)
        if args.command == "cutover-active-price-basis":
            return _run_cutover_active_price_basis(args)
        if args.command == "repair-active-price-basis-chain":
            return _run_repair_active_price_basis_chain(args)
        if args.command == "repair-analysis-direction-policy":
            return _run_repair_analysis_direction_policy(args)
        return run_runtime_repair_command(args)
    except OperatorRepairError as exc:
        print(f"runtime repair failed: {exc}", file=sys.stderr)
        return 1
    except (
        ActivePriceBasisCutoverError,
        ActivePriceBasisRepairError,
        AnalysisDirectionPolicyRepairError,
        BootstrapConfigError,
        RuntimeRepairInspectionError,
        ReflectionRecoveryError,
        LiveSmokeTraceBundleError,
        ResearchMemoryMigrationError,
        WorkspaceBootstrapError,
    ) as exc:
        print(f"runtime repair failed: {exc}", file=sys.stderr)
        return 1


def _run_replay_failed_event(args: argparse.Namespace) -> int:
    layout = _workspace_layout(args)
    window_start = _parse_window_boundary(args.window_start, end_of_day=False)
    window_end = _parse_window_boundary(args.window_end, end_of_day=True)
    if window_end < window_start:
        raise OperatorRepairError("window_end must be greater than or equal to window_start.")
    run_key = replay_run_key(
        target_key=args.target_key,
        window_start=window_start,
        window_end=window_end,
    )
    run_id = args.run_id or default_replay_run_id(
        target_key=args.target_key,
        window_start=window_start,
        window_end=window_end,
    )
    plan = plan_replay_failed_event_repair(
        layout=layout,
        target_key=args.target_key,
        run_key=run_key,
        run_id=run_id,
        event_id=args.event_id,
    )
    if args.apply:
        receipt = apply_replay_repair_plan(layout=layout, plan=plan)
        print(
            json.dumps(
                receipt.to_json_payload(workspace_root=layout.root),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(
        json.dumps(
            plan.to_json_payload(workspace_root=layout.root),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if plan.blockers else 0


def _run_cutover_active_price_basis(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve(strict=False)
    config = load_kernel_config(config_path)
    layout = _workspace_layout(args)
    cutover_at = _parse_timestamp_argument(args.cutover_at, field_name="cutover_at")
    result = run_active_price_basis_cutover_from_config(
        layout=layout,
        config=config,
        target_key=args.target_key,
        cutover_at=cutover_at,
        legacy_policy=args.legacy_policy,
    )
    print(
        json.dumps(
            {
                "target_key": result.target_key,
                "cutover_applied": result.cutover_applied,
                "old_basis_id": result.old_basis_id,
                "new_basis_id": result.new_basis_id,
                "predecessor_assessment_id": result.predecessor_assessment_id,
                "successor_assessment_id": result.successor_assessment_id,
                "successor_revision_id": result.successor_revision_id,
                "pm_refresh_required": result.pm_refresh_required,
                "workspace_root": layout.root.as_posix(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _run_repair_active_price_basis_chain(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve(strict=False)
    config = load_kernel_config(config_path)
    layout = _workspace_layout(args)
    receipt = repair_active_price_basis_chain(
        layout=layout,
        target_key=args.target_key,
        validation_config=config.validation,
        market_context_config=config.market_context,
    )
    print(
        json.dumps(
            receipt.to_json_payload(workspace_root=layout.root),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _run_repair_analysis_direction_policy(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve(strict=False)
    config = load_kernel_config(config_path)
    layout = _workspace_layout(args)
    receipt = repair_analysis_direction_policy(
        layout=layout,
        target_key=args.target_key,
        execution_direction_mode=resolve_validation_execution_direction_mode(
            config,
            target_key=args.target_key,
        ),
    )
    print(
        json.dumps(
            receipt.to_json_payload(workspace_root=layout.root),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _workspace_layout(args: argparse.Namespace) -> WorkspaceLayout:
    if args.workspace_root:
        return build_workspace_layout(Path(args.workspace_root))
    config = load_kernel_config(Path(args.config).resolve(strict=False))
    workspace = bootstrap_workspace(config.workspace_root)
    if workspace.layout is None:
        raise OperatorRepairError("Workspace layout is required for runtime repair.")
    return workspace.layout


def _parse_window_boundary(raw_value: str, *, end_of_day: bool) -> datetime:
    normalized = raw_value.strip()
    if not normalized:
        raise OperatorRepairError("Window values must not be blank.")
    if len(normalized) == 10:
        parsed_date = date.fromisoformat(normalized)
        parsed_time = time(23, 59, 59, tzinfo=UTC) if end_of_day else time(0, 0, tzinfo=UTC)
        return datetime.combine(parsed_date, parsed_time)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperatorRepairError(
            f"Invalid ISO8601 datetime or YYYY-MM-DD value: {raw_value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OperatorRepairError(
            f"Window value must include timezone information: {raw_value!r}"
        )
    return parsed.astimezone(UTC)


def _parse_timestamp_argument(raw_value: str, *, field_name: str) -> datetime:
    normalized = raw_value.strip()
    if not normalized:
        raise OperatorRepairError(f"{field_name} must not be blank.")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperatorRepairError(
            f"{field_name} must be a timezone-aware ISO8601 timestamp: {raw_value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OperatorRepairError(
            f"{field_name} must include timezone information: {raw_value!r}"
        )
    return parsed.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
