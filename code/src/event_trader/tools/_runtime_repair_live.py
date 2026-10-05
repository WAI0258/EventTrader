"""Live-specific runtime repair subcommands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from event_trader.audit import write_live_smoke_trace_bundle
from event_trader.config import load_kernel_config
from event_trader.market.provider import (
    CachedReplayMarketDataProvider,
    build_default_market_bars_provider,
)
from event_trader.market.store import FileBackedMarketDataStore, read_active_replay_run_id
from event_trader.operator.repair.runtime_analysis import (
    AnalysisRecoveryReport,
    audit_live_analysis_consistency,
    plan_analysis_recovery,
)
from event_trader.operator.repair.runtime_pm import (
    PMExecutionRecoveryApplyReport,
    PMExecutionRecoveryReport,
    apply_pm_execution_recovery,
    plan_pm_execution_recovery,
)
from event_trader.operator.repair.runtime_reflection import (
    ReflectionRecoveryReport,
    plan_reflection_recovery,
)
from event_trader.operator.research_memory_migration import migrate_research_memory_citations
from event_trader.pm_review.runtime import build_pm_review_execution_inputs
from event_trader.storage import build_workspace_layout


def register_runtime_repair_subcommands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    scan_parser = subparsers.add_parser(
        "scan-analysis-candidates",
        help="Plan lifecycle-aware analysis recovery from checker receipts and commit audit truth.",
    )
    scan_parser.add_argument(
        "--workspace-root",
        required=True,
        help="Canonical event-trader workspace root to inspect.",
    )
    scan_parser.add_argument(
        "--target-key",
        help="Restrict inspection to one target key.",
    )
    scan_parser.add_argument(
        "--event-id",
        help="Restrict inspection to one evidence event id.",
    )
    audit_parser = subparsers.add_parser(
        "audit-analysis-consistency",
        help="Audit live analysis context/outcome/write/CEAU consistency.",
    )
    audit_parser.add_argument(
        "--workspace-root",
        required=True,
        help="Canonical event-trader workspace root to inspect.",
    )
    audit_parser.add_argument(
        "--target-key",
        required=True,
        help="Audit one target key.",
    )
    migration_parser = subparsers.add_parser(
        "migrate-research-memory-citations",
        help="Normalize canonical research-memory citations using ledger truth.",
    )
    migration_parser.add_argument(
        "--workspace-root",
        required=True,
        help="Canonical event-trader workspace root to inspect or update.",
    )
    migration_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write canonicalized markdown files. Omit for dry-run.",
    )
    smoke_parser = subparsers.add_parser(
        "smoke-trace-bundle",
        help="Write a deterministic live smoke trace bundle from existing artifacts.",
    )
    smoke_parser.add_argument(
        "--workspace-root",
        required=True,
        help="Canonical event-trader workspace root to inspect.",
    )
    smoke_parser.add_argument(
        "--target-key",
        required=True,
        help="Target key to trace.",
    )
    smoke_parser.add_argument(
        "--event-id",
        required=True,
        help="Source event id anchoring the trace bundle.",
    )
    smoke_parser.add_argument(
        "--view-episode-id",
        required=True,
        help="View episode id anchoring validation and reflection artifacts.",
    )
    smoke_parser.add_argument(
        "--decision-episode-id",
        required=True,
        help="Decision episode id anchoring decision-stage artifacts.",
    )
    pm_execution_parser = subparsers.add_parser(
        "scan-pm-execution-recovery",
        help=(
            "Plan lifecycle-aware PM/execution recovery from active "
            "request/decision/execution truth."
        ),
    )
    pm_execution_parser.add_argument(
        "--workspace-root",
        required=True,
        help="Canonical event-trader workspace root to inspect.",
    )
    pm_execution_parser.add_argument(
        "--target-key",
        help="Restrict inspection to one target key.",
    )
    pm_execution_parser.add_argument(
        "--event-id",
        help="Restrict inspection to one evidence event id.",
    )
    apply_pm_execution_parser = subparsers.add_parser(
        "apply-pm-execution-recovery",
        help="Apply safe PMDecision -> execution/state recovery without rerunning analysis.",
    )
    apply_pm_execution_parser.add_argument(
        "--config",
        required=True,
        help="Kernel config used to build the paper execution engine.",
    )
    apply_pm_execution_parser.add_argument(
        "--workspace-root",
        required=True,
        help="Canonical event-trader workspace root to inspect or update.",
    )
    apply_pm_execution_parser.add_argument(
        "--target-key",
        required=True,
        help="Target key to recover.",
    )
    apply_pm_execution_parser.add_argument(
        "--event-id",
        help="Restrict recovery to one evidence event id.",
    )
    apply_pm_execution_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write execution/state artifacts. Omit for dry-run.",
    )
    reflection_parser = subparsers.add_parser(
        "scan-reflection-recovery",
        help="Plan lifecycle-aware reflection recovery from obligation and resolution truth.",
    )
    reflection_parser.add_argument(
        "--workspace-root",
        required=True,
        help="Canonical event-trader workspace root to inspect.",
    )
    reflection_parser.add_argument(
        "--target-key",
        help="Restrict inspection to one target key.",
    )


def run_runtime_repair_command(args: argparse.Namespace) -> int:
    if args.command == "scan-analysis-candidates":
        return _run_scan_analysis_candidates(args)
    if args.command == "audit-analysis-consistency":
        return _run_audit_analysis_consistency(args)
    if args.command == "migrate-research-memory-citations":
        return _run_migrate_research_memory_citations(args)
    if args.command == "smoke-trace-bundle":
        return _run_smoke_trace_bundle(args)
    if args.command == "scan-pm-execution-recovery":
        return _run_scan_pm_execution_recovery(args)
    if args.command == "apply-pm-execution-recovery":
        return _run_apply_pm_execution_recovery(args)
    if args.command == "scan-reflection-recovery":
        return _run_scan_reflection_recovery(args)
    raise RuntimeError(f"Unhandled runtime repair command: {args.command}")


def _run_scan_analysis_candidates(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve(strict=False)
    layout = build_workspace_layout(workspace_root)
    report = plan_analysis_recovery(
        layout=layout,
        target_key=args.target_key,
        event_id=args.event_id,
    )
    print(
        json.dumps(
            report.to_json_payload(workspace_root=layout.root),
            ensure_ascii=False,
            indent=2,
        )
    )
    return _analysis_recovery_exit_code(report)


def _run_audit_analysis_consistency(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve(strict=False)
    layout = build_workspace_layout(workspace_root)
    report = audit_live_analysis_consistency(
        layout=layout,
        target_key=args.target_key,
    )
    print(
        json.dumps(
            report.to_json_payload(workspace_root=layout.root),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report.status == "passed" else 1


def _run_scan_pm_execution_recovery(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve(strict=False)
    layout = build_workspace_layout(workspace_root)
    report = plan_pm_execution_recovery(
        layout=layout,
        target_key=args.target_key,
        event_id=args.event_id,
    )
    print(
        json.dumps(
            report.to_json_payload(workspace_root=layout.root),
            ensure_ascii=False,
            indent=2,
        )
    )
    return _pm_execution_recovery_exit_code(report)


def _run_apply_pm_execution_recovery(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve(strict=False)
    layout = build_workspace_layout(workspace_root)
    config = load_kernel_config(Path(args.config))
    market_data_provider = _build_pm_execution_recovery_market_provider(
        layout=layout,
        config=config,
        target_key=args.target_key,
    )
    try:
        execution_inputs = build_pm_review_execution_inputs(
            layout=layout,
            config=config,
            market_data_provider=market_data_provider,
        )
        report = apply_pm_execution_recovery(
            layout=layout,
            execution_engine=execution_inputs.execution_engine,
            target_key=args.target_key,
            event_id=args.event_id,
            apply=args.apply,
        )
    finally:
        close = getattr(market_data_provider, "close", None)
        if callable(close):
            close()
    print(json.dumps(report.to_json_payload(), ensure_ascii=False, indent=2))
    return _pm_execution_recovery_apply_exit_code(report)


def _build_pm_execution_recovery_market_provider(
    *,
    layout,
    config,
    target_key: str,
):
    target_market_root = layout.runtime_root / "market_data" / target_key
    replay_run_id = read_active_replay_run_id(target_market_root)
    if replay_run_id is not None:
        return CachedReplayMarketDataProvider(
            FileBackedMarketDataStore(target_market_root / replay_run_id)
        )
    provider = build_default_market_bars_provider(config)
    if provider is None:
        raise RuntimeError(
            "PM execution recovery requires cached replay market data or a configured "
            "validation.market_data provider."
        )
    return provider


def _run_scan_reflection_recovery(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve(strict=False)
    layout = build_workspace_layout(workspace_root)
    report = plan_reflection_recovery(
        layout=layout,
        target_key=args.target_key,
    )
    print(
        json.dumps(
            report.to_json_payload(workspace_root=layout.root),
            ensure_ascii=False,
            indent=2,
        )
    )
    return _reflection_recovery_exit_code(report)


def _run_smoke_trace_bundle(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve(strict=False)
    layout = build_workspace_layout(workspace_root)
    persisted = write_live_smoke_trace_bundle(
        layout=layout,
        target_key=args.target_key,
        event_id=args.event_id,
        view_episode_id=args.view_episode_id,
        decision_episode_id=args.decision_episode_id,
    )
    print("")
    print("live smoke trace bundle:")
    print(f"- status={persisted.report.status}")
    print(f"- json={persisted.json_path.as_posix()}")
    print(f"- markdown={persisted.markdown_path.as_posix()}")
    return 0 if persisted.report.status == "passed" else 1


def _run_migrate_research_memory_citations(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve(strict=False)
    layout = build_workspace_layout(workspace_root)
    receipts = migrate_research_memory_citations(
        layout=layout,
        apply=args.apply,
    )
    changed_receipts = [receipt for receipt in receipts if receipt.changed]
    payload = {
        "mode": "apply" if args.apply else "dry_run",
        "page_count": len(receipts),
        "changed_page_count": len(changed_receipts),
        "changed_pages": [
            {
                "page_path": receipt.page_path,
                "event_ids": list(receipt.event_ids),
            }
            for receipt in changed_receipts
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _analysis_recovery_exit_code(report: AnalysisRecoveryReport) -> int:
    return 0 if all(
        decision.planned_action in {"no_action", "rollback_precommit", "resume", "finalize"}
        for decision in report.decisions
    ) else 1


def _pm_execution_recovery_exit_code(report: PMExecutionRecoveryReport) -> int:
    return 0 if all(
        decision.planned_action in {"no_action", "resume", "finalize"}
        for decision in report.decisions
    ) else 1


def _pm_execution_recovery_apply_exit_code(
    report: PMExecutionRecoveryApplyReport,
) -> int:
    return 0 if report.blocked_count == 0 else 1


def _reflection_recovery_exit_code(report: ReflectionRecoveryReport) -> int:
    return 0 if all(
        decision.planned_action in {"no_action", "resume", "finalize"}
        for decision in report.decisions
    ) else 1
