from __future__ import annotations

import argparse
import json
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from event_trader.config import (
    load_kernel_config,
    resolve_validation_execution_direction_mode,
)
from event_trader.execution.contracts import ExecutionRecord
from event_trader.execution.store import ExecutionIntentStore, ExecutionRecordStore
from event_trader.market.provider import CachedReplayMarketDataProvider
from event_trader.market.store import FileBackedMarketDataStore
from event_trader.pm_review.runtime import (
    build_pm_review_execution_inputs,
    build_pm_review_task_runner,
    build_pm_review_visible_market_bars_for_requests,
    process_pending_pm_review_request_and_execute,
)
from event_trader.pm_review.store import PMReviewRequestStore
from event_trader.portfolio.contracts import PortfolioState
from event_trader.portfolio.store import PMDecisionStore, PortfolioStateStore
from event_trader.projection.report_artifacts import (
    current_state_artifact_path,
    generate_current_state_report,
)
from event_trader.storage import WorkspaceLayout, build_workspace_layout
from event_trader.validation.episode_artifacts import refresh_episode_artifacts
from event_trader.validation.state_change_store import read_state_changes


@dataclass(frozen=True, slots=True)
class RemovalPlan:
    label: str
    path: Path
    removed_count: int


@dataclass(frozen=True, slots=True)
class ReplayReceipt:
    request_id: str
    business_at: str
    requested_state: str
    execution_required: bool
    execution_record_id: str | None


def main() -> None:
    args = _parse_args()
    workspace_root = args.workspace_root.resolve(strict=False)
    layout = build_workspace_layout(workspace_root)
    config = load_kernel_config(args.config.resolve(strict=False))
    cutoff = _parse_dt(args.cutoff)
    audit_root = _make_audit_root(layout.root, apply=args.apply)

    request_store = PMReviewRequestStore(layout)
    requests = tuple(
        persisted.record
        for persisted in request_store.read_records(target_key=args.target_key)
        if persisted.record.business_at >= cutoff
    )
    if args.limit is not None:
        if args.limit < 1:
            raise RuntimeError("--limit must be a positive integer when provided.")
        requests = requests[: args.limit]
    rebuild_until = None if args.limit is None else requests[-1].business_at
    request_ids = {request.request_id for request in requests}

    if not requests:
        raise RuntimeError(
            f"no PMReview requests found at or after cutoff={cutoff.isoformat()}"
        )

    removal_plans, retained_state = _truncate_derived_surfaces(
        layout=layout,
        target_key=args.target_key,
        cutoff=cutoff,
        rebuild_until=rebuild_until,
        request_ids=request_ids,
        audit_root=audit_root,
        apply=args.apply,
    )

    _write_json(
        audit_root / "plan.json",
        {
            "workspace_root": str(layout.root),
            "target_key": args.target_key,
            "cutoff": cutoff.isoformat(),
            "apply": args.apply,
            "request_count": len(requests),
            "request_start": requests[0].business_at.isoformat(),
            "request_end": requests[-1].business_at.isoformat(),
            "removals": [
                {
                    "label": plan.label,
                    "path": str(plan.path),
                    "removed_count": plan.removed_count,
                }
                for plan in removal_plans
            ],
            "retained_state": None
            if retained_state is None
            else retained_state.to_json_payload(),
        },
    )

    print(
        f"pm rewind rebuild: requests={len(requests)} cutoff={cutoff.isoformat()} "
        f"apply={args.apply}"
    )
    for plan in removal_plans:
        if plan.removed_count <= 0:
            continue
        print(f"planned removal: {plan.label} count={plan.removed_count} path={plan.path}")

    if not args.apply:
        print(f"audit: {audit_root}")
        return

    if args.truncate_only:
        refresh_episode_artifacts(layout, args.target_key)
        report = generate_current_state_report(target_key=args.target_key, layout=layout)
        _write_json(
            audit_root / "summary.json",
            {
                "processed_count": 0,
                "truncate_only": True,
                "current_state_report": str(report.receipt.artifact_path),
                "portfolio_state": _portfolio_state_payload(layout, args.target_key),
                "latest_pm_decision": _latest_pm_decision_payload(layout, args.target_key),
            },
        )
        print(f"pm rewind rebuild truncate-only complete: audit={audit_root}")
        return

    market_provider = _build_cached_market_provider(
        layout=layout,
        target_key=args.target_key,
        run_id=args.run_id,
    )
    task_runner = build_pm_review_task_runner(config=config)
    execution_inputs = build_pm_review_execution_inputs(
        layout=layout,
        config=config,
        market_data_provider=market_provider,
    )
    execution_direction_mode = resolve_validation_execution_direction_mode(
        config,
        target_key=args.target_key,
    )

    processed: list[dict[str, object]] = []
    for request in requests:
        market_bars = build_pm_review_visible_market_bars_for_requests(
            layout=layout,
            target_key=args.target_key,
            run_until=request.max_visible_market_time,
            config=config,
            market_data_provider=market_provider,
            emit=print,
        )
        result = process_pending_pm_review_request_and_execute(
            layout=layout,
            task_runner=task_runner,
            execution_engine=execution_inputs.execution_engine,
            request=request,
            market_bars=market_bars,
            config=config,
            market_data_provider=market_provider,
            execution_direction_mode=execution_direction_mode,
            decision_available_at=request.business_at,
            emit=print,
        )
        decision = result.pm_review_orchestration.pm_review_result.pm_decision
        execution_record = result.execution_flow_result.execution_record
        receipt = ReplayReceipt(
            request_id=request.request_id,
            business_at=request.business_at.isoformat(),
            requested_state=decision.requested_state,
            execution_required=decision.execution_required,
            execution_record_id=(
                None if execution_record is None else execution_record.execution_record_id
            ),
        )
        processed.append(
            {
                "request_id": receipt.request_id,
                "business_at": receipt.business_at,
                "requested_state": receipt.requested_state,
                "execution_required": receipt.execution_required,
                "execution_record_id": receipt.execution_record_id,
            }
        )
        _write_json(audit_root / "processed.json", {"processed": processed})
        print(
            "replayed:",
            request.business_at.isoformat(),
            request.request_id,
            decision.requested_state,
            "execution_required=" + str(decision.execution_required),
        )

    refresh_episode_artifacts(layout, args.target_key)
    report = generate_current_state_report(target_key=args.target_key, layout=layout)
    _write_json(
        audit_root / "summary.json",
        {
            "processed_count": len(processed),
            "current_state_report": str(report.receipt.artifact_path),
            "portfolio_state": _portfolio_state_payload(layout, args.target_key),
            "latest_pm_decision": _latest_pm_decision_payload(layout, args.target_key),
        },
    )
    print(f"pm rewind rebuild complete: processed={len(processed)} audit={audit_root}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target-key", required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--truncate-only", action="store_true")
    return parser.parse_args()


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _make_audit_root(workspace_root: Path, *, apply: bool) -> Path:
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    root = workspace_root / "runtime" / "repair" / f"pm-rewind-rebuild-{timestamp}"
    if not apply:
        root = root.with_name(root.name + "-dry-run")
    root.mkdir(parents=True, exist_ok=False)
    return root


def _truncate_derived_surfaces(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    cutoff: datetime,
    rebuild_until: datetime | None,
    request_ids: set[str],
    audit_root: Path,
    apply: bool,
) -> tuple[list[RemovalPlan], PortfolioState | None]:
    backup_root = audit_root / "backup"
    removal_plans: list[RemovalPlan] = []

    removed_decision_ids = {
        persisted.record.decision_id
        for persisted in PMDecisionStore(layout).read_records(target_key=target_key)
        if _datetime_in_rebuild_window(
            persisted.record.business_at,
            start=cutoff,
            end=rebuild_until,
        )
    }
    removed_execution_record_ids = {
        persisted.record.execution_record_id
        for persisted in ExecutionRecordStore(layout).read_records(target_key=target_key)
        if _datetime_in_rebuild_window(
            persisted.record.business_at,
            start=cutoff,
            end=rebuild_until,
        )
        or persisted.record.pm_decision_id in removed_decision_ids
    }
    removed_pm_decision_episode_ids = {
        persisted.record.decision_episode_id
        for persisted in PMDecisionStore(layout).read_records(target_key=target_key)
        if _datetime_in_rebuild_window(
            persisted.record.business_at,
            start=cutoff,
            end=rebuild_until,
        )
    }

    removal_plans.extend(
        _truncate_store_family(
            layout=layout,
            audit_root=backup_root,
            label="pm_decisions",
            root=layout.runtime_root / "portfolio" / "pm-decisions" / target_key,
            matcher=lambda payload: _timestamp_within_rebuild_window(
                payload,
                "business_at",
                start=cutoff,
                end=rebuild_until,
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_store_family(
            layout=layout,
            audit_root=backup_root,
            label="execution_intents",
            root=layout.runtime_root / "execution" / "intents" / target_key,
            matcher=lambda payload: (
                _timestamp_within_rebuild_window(
                    payload,
                    "business_at",
                    start=cutoff,
                    end=rebuild_until,
                )
                or _text_value(payload, "pm_decision_id") in removed_decision_ids
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_store_family(
            layout=layout,
            audit_root=backup_root,
            label="execution_records",
            root=layout.runtime_root / "execution" / "records" / target_key,
            matcher=lambda payload: (
                _timestamp_within_rebuild_window(
                    payload,
                    "business_at",
                    start=cutoff,
                    end=rebuild_until,
                )
                or _text_value(payload, "pm_decision_id") in removed_decision_ids
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_store_family(
            layout=layout,
            audit_root=backup_root,
            label="pm_review_tool_reads",
            root=layout.runtime_root / "pm_review" / "tool_reads" / target_key,
            matcher=lambda payload: (
                _text_value(payload, "pm_review_request_id") in request_ids
                or _timestamp_within_rebuild_window(
                    payload,
                    "business_at",
                    start=cutoff,
                    end=rebuild_until,
                )
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_store_family(
            layout=layout,
            audit_root=backup_root,
            label="pm_review_episode_memory_reads",
            root=layout.runtime_root / "pm_review" / "episode_memory_reads" / target_key,
            matcher=lambda payload: (
                _text_value(payload, "request_id") in request_ids
                or _timestamp_within_rebuild_window(
                    payload,
                    "business_at",
                    start=cutoff,
                    end=rebuild_until,
                )
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_store_family(
            layout=layout,
            audit_root=backup_root,
            label="pm_review_failures",
            root=layout.runtime_root / "pm_review" / "failures" / target_key,
            matcher=lambda payload: (
                _text_value(payload, "pm_review_request_id") in request_ids
                or _timestamp_within_rebuild_window(
                    payload,
                    "business_at",
                    start=cutoff,
                    end=rebuild_until,
                )
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_store_family(
            layout=layout,
            audit_root=backup_root,
            label="pm_review_dispatch_considerations",
            root=layout.runtime_root / "pm_review" / "dispatch_considerations" / target_key,
            matcher=lambda payload: _timestamp_within_rebuild_window(
                payload,
                "business_at",
                start=cutoff,
                end=rebuild_until,
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_store_family(
            layout=layout,
            audit_root=backup_root,
            label="pm_review_position_review_gate",
            root=layout.runtime_root / "pm_review" / "position_review_gate" / target_key,
            matcher=lambda payload: _timestamp_within_rebuild_window(
                payload,
                "business_at",
                start=cutoff,
                end=rebuild_until,
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_store_family(
            layout=layout,
            audit_root=backup_root,
            label="pm_review_position_review_triggers",
            root=layout.runtime_root / "pm_review" / "position_review_triggers" / target_key,
            matcher=lambda payload: (
                _text_value(payload, "pm_review_request_id") in request_ids
                or _timestamp_within_rebuild_window(
                    payload,
                    "business_at",
                    start=cutoff,
                    end=rebuild_until,
                )
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_store_family(
            layout=layout,
            audit_root=backup_root,
            label="validation_state_changes",
            root=layout.runtime_root / "validation" / "state-changes" / target_key,
            matcher=lambda payload: (
                _text_value(payload, "pm_decision_id") in removed_decision_ids
                or _text_value(payload, "execution_record_id") in removed_execution_record_ids
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_store_family(
            layout=layout,
            audit_root=backup_root,
            label="decision_episodes",
            root=layout.runtime_root / "decision_episodes" / target_key,
            matcher=lambda payload: _decision_episode_should_truncate(
                payload=payload,
                cutoff=cutoff,
                rebuild_until=rebuild_until,
                removed_decision_ids=removed_decision_ids,
                removed_execution_record_ids=removed_execution_record_ids,
                removed_pm_decision_episode_ids=removed_pm_decision_episode_ids,
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_json_file_family(
            layout=layout,
            audit_root=backup_root,
            label="pm_review_work_queue",
            root=layout.runtime_root / "pm_review_work_queue" / "pm_review",
            matcher=lambda payload: _pm_review_runtime_payload_should_truncate(
                payload=payload,
                cutoff=cutoff,
                rebuild_until=rebuild_until,
                request_ids=request_ids,
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_json_file_family(
            layout=layout,
            audit_root=backup_root,
            label="pm_review_outbox",
            root=layout.runtime_root / "pm_review_outbox" / "pm_review_completions",
            matcher=lambda payload: _pm_review_runtime_payload_should_truncate(
                payload=payload,
                cutoff=cutoff,
                rebuild_until=rebuild_until,
                request_ids=request_ids,
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_json_file_family(
            layout=layout,
            audit_root=backup_root,
            label="pm_review_completions_legacy",
            root=layout.runtime_root / "pm_review_completions",
            matcher=lambda payload: _pm_review_runtime_payload_should_truncate(
                payload=payload,
                cutoff=cutoff,
                rebuild_until=rebuild_until,
                request_ids=request_ids,
            ),
            apply=apply,
        )
    )

    removal_plans.extend(
        _truncate_reflection_surfaces(
            layout=layout,
            audit_root=backup_root,
            target_key=target_key,
            cutoff=cutoff,
            rebuild_until=rebuild_until,
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_json_file_family(
            layout=layout,
            audit_root=backup_root,
            label="reflection_work_queue",
            root=layout.runtime_root / "reflection_work_queue" / "reflection",
            matcher=lambda payload: _reflection_runtime_payload_should_truncate(
                payload=payload,
                cutoff=cutoff,
                rebuild_until=rebuild_until,
            ),
            apply=apply,
        )
    )
    removal_plans.extend(
        _truncate_json_file_family(
            layout=layout,
            audit_root=backup_root,
            label="reflection_outbox",
            root=layout.runtime_root / "reflection_outbox" / "reflection_completions",
            matcher=lambda payload: _reflection_runtime_payload_should_truncate(
                payload=payload,
                cutoff=cutoff,
                rebuild_until=rebuild_until,
            ),
            apply=apply,
        )
    )

    retained_state = _portfolio_state_before_cutoff(
        layout=layout,
        target_key=target_key,
        cutoff=cutoff,
    )
    if apply:
        _reset_portfolio_state(
            layout=layout,
            target_key=target_key,
            retained_state=retained_state,
            audit_root=backup_root,
        )
        _delete_directory_if_present(
            layout=layout,
            directory=layout.runtime_root / "validation" / "episodes" / target_key,
            audit_root=backup_root,
        )
        _delete_file_if_present(
            layout=layout,
            path=layout.runtime_root / current_state_artifact_path(target_key),
            audit_root=backup_root,
        )
        refresh_episode_artifacts(layout, target_key)
    return removal_plans, retained_state


def _truncate_reflection_surfaces(
    *,
    layout: WorkspaceLayout,
    audit_root: Path,
    target_key: str,
    cutoff: datetime,
    rebuild_until: datetime | None,
    apply: bool,
) -> list[RemovalPlan]:
    plans: list[RemovalPlan] = []
    obligations_path = (
        layout.research_memory_root / "targets" / target_key / "reflection_obligations.jsonl"
    )
    resolutions_path = (
        layout.research_memory_root
        / "targets"
        / target_key
        / "reflection_obligation_resolutions.jsonl"
    )
    coverage_path = (
        layout.research_memory_root / "targets" / target_key / "reviews" / "coverage.jsonl"
    )
    plans.extend(
        _truncate_single_jsonl(
            layout=layout,
            audit_root=audit_root,
            label="reflection_obligations",
            path=obligations_path,
            matcher=lambda payload: (
                _timestamp_within_rebuild_window(
                    payload,
                    "created_at",
                    start=cutoff,
                    end=rebuild_until,
                )
                or _timestamp_within_rebuild_window(
                    payload,
                    "due_at",
                    start=cutoff,
                    end=rebuild_until,
                )
            ),
            apply=apply,
        )
    )
    plans.extend(
        _truncate_single_jsonl(
            layout=layout,
            audit_root=audit_root,
            label="reflection_resolutions",
            path=resolutions_path,
            matcher=lambda payload: _timestamp_within_rebuild_window(
                payload,
                "resolved_at",
                start=cutoff,
                end=rebuild_until,
            ),
            apply=apply,
        )
    )

    removed_review_pages: list[str] = []
    if coverage_path.exists():
        rows = _read_jsonl_payloads(coverage_path)
        keep_rows: list[dict[str, object]] = []
        for row in rows:
            remove = _timestamp_within_rebuild_window(
                row,
                "opened_at",
                start=cutoff,
                end=rebuild_until,
            ) or _timestamp_within_rebuild_window(
                row,
                "closed_at",
                start=cutoff,
                end=rebuild_until,
            )
            if remove:
                page_path = row.get("review_page_path")
                if isinstance(page_path, str) and page_path.strip():
                    removed_review_pages.append(page_path)
                continue
            keep_rows.append(row)
        removed_count = len(rows) - len(keep_rows)
        if removed_count > 0:
            plans.append(
                RemovalPlan(
                    label="review_coverage",
                    path=coverage_path,
                    removed_count=removed_count,
                )
            )
            if apply:
                _backup_file(layout=layout, path=coverage_path, audit_root=audit_root)
                _write_jsonl_payloads(coverage_path, keep_rows)

    if apply:
        for relative_page in removed_review_pages:
            page_path = (layout.research_memory_root / relative_page).resolve(strict=False)
            _delete_file_if_present(layout=layout, path=page_path, audit_root=audit_root)
    return plans


def _truncate_store_family(
    *,
    layout: WorkspaceLayout,
    audit_root: Path,
    label: str,
    root: Path,
    matcher: Callable[[dict[str, object]], bool],
    apply: bool,
) -> list[RemovalPlan]:
    plans: list[RemovalPlan] = []
    if not root.exists():
        return plans
    for path in sorted(root.glob("*.jsonl")):
        plans.extend(
            _truncate_single_jsonl(
                layout=layout,
                audit_root=audit_root,
                label=label,
                path=path,
                matcher=matcher,
                apply=apply,
            )
        )
    return plans


def _truncate_json_file_family(
    *,
    layout: WorkspaceLayout,
    audit_root: Path,
    label: str,
    root: Path,
    matcher: Callable[[dict[str, object]], bool],
    apply: bool,
) -> list[RemovalPlan]:
    plans: list[RemovalPlan] = []
    if not root.exists():
        return plans
    for path in sorted(root.rglob("*.json")):
        plans.extend(
            _truncate_single_json_file(
                layout=layout,
                audit_root=audit_root,
                label=label,
                path=path,
                matcher=matcher,
                apply=apply,
            )
        )
    return plans


def _truncate_single_jsonl(
    *,
    layout: WorkspaceLayout,
    audit_root: Path,
    label: str,
    path: Path,
    matcher: Callable[[dict[str, object]], bool],
    apply: bool,
) -> list[RemovalPlan]:
    if not path.exists():
        return []
    rows = _read_jsonl_payloads(path)
    keep_rows = [row for row in rows if not matcher(row)]
    removed_count = len(rows) - len(keep_rows)
    if removed_count <= 0:
        return []
    if apply:
        _backup_file(layout=layout, path=path, audit_root=audit_root)
        if keep_rows:
            _write_jsonl_payloads(path, keep_rows)
        else:
            path.unlink()
    return [RemovalPlan(label=label, path=path, removed_count=removed_count)]


def _truncate_single_json_file(
    *,
    layout: WorkspaceLayout,
    audit_root: Path,
    label: str,
    path: Path,
    matcher: Callable[[dict[str, object]], bool],
    apply: bool,
) -> list[RemovalPlan]:
    if not path.exists():
        return []
    payload = _read_json_payload(path)
    if not matcher(payload):
        return []
    if apply:
        _backup_file(layout=layout, path=path, audit_root=audit_root)
        path.unlink()
    return [RemovalPlan(label=label, path=path, removed_count=1)]


def _portfolio_state_before_cutoff(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    cutoff: datetime,
) -> PortfolioState | None:
    decisions = {
        persisted.record.decision_id: persisted.record
        for persisted in PMDecisionStore(layout).read_records(target_key=target_key)
        if persisted.record.business_at < cutoff
    }
    executed_records: list[ExecutionRecord] = [
        persisted.record
        for persisted in ExecutionRecordStore(layout).read_records(target_key=target_key)
        if persisted.record.business_at < cutoff and persisted.record.status == "executed"
    ]
    if not executed_records:
        return None
    latest_record = max(
        executed_records,
        key=lambda record: (record.executed_at or record.business_at, record.execution_record_id),
    )
    linked_decision = decisions.get(latest_record.pm_decision_id)
    if linked_decision is None or latest_record.executed_at is None or latest_record.target_weight is None:
        return None
    return PortfolioState(
        target_key=target_key,
        target_weight=latest_record.target_weight,
        state=linked_decision.requested_state,
        updated_at=latest_record.executed_at,
        source_pm_decision_id=linked_decision.decision_id,
        source_execution_record_id=latest_record.execution_record_id,
        decision_episode_id=linked_decision.decision_episode_id,
    )


def _reset_portfolio_state(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    retained_state: PortfolioState | None,
    audit_root: Path,
) -> None:
    state_path = PortfolioStateStore(layout).path_for(target_key)
    if retained_state is None:
        _delete_file_if_present(layout=layout, path=state_path, audit_root=audit_root)
        return
    if state_path.exists():
        _backup_file(layout=layout, path=state_path, audit_root=audit_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(retained_state.to_json_payload(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _build_cached_market_provider(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_id: str,
) -> CachedReplayMarketDataProvider:
    store_root = layout.runtime_root / "market_data" / target_key / run_id
    manifest_path = store_root / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"prefetched market data manifest is missing: {manifest_path}")
    return CachedReplayMarketDataProvider(FileBackedMarketDataStore(store_root))


def _timestamp_on_or_after(
    payload: Mapping[str, object],
    field_name: str,
    cutoff: datetime,
) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = _parse_dt(value)
    except ValueError:
        return False
    return parsed >= cutoff


def _timestamp_within_rebuild_window(
    payload: Mapping[str, object],
    field_name: str,
    *,
    start: datetime,
    end: datetime | None,
) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = _parse_dt(value)
    except ValueError:
        return False
    return _datetime_in_rebuild_window(parsed, start=start, end=end)


def _datetime_in_rebuild_window(
    value: datetime,
    *,
    start: datetime,
    end: datetime | None,
) -> bool:
    if value < start:
        return False
    return end is None or value <= end


def _text_value(payload: Mapping[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _read_jsonl_payloads(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _write_jsonl_payloads(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(
        json.dumps(dict(row), ensure_ascii=False) + "\n"
        for row in rows
    )
    path.write_text(content, encoding="utf-8")


def _backup_file(*, layout: WorkspaceLayout, path: Path, audit_root: Path) -> None:
    if not path.exists():
        return
    relative = path.resolve(strict=False).relative_to(layout.root.resolve(strict=False))
    destination = audit_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)


def _delete_file_if_present(
    *,
    layout: WorkspaceLayout,
    path: Path,
    audit_root: Path,
) -> None:
    if not path.exists():
        return
    _backup_file(layout=layout, path=path, audit_root=audit_root)
    path.unlink()


def _delete_directory_if_present(
    *,
    layout: WorkspaceLayout,
    directory: Path,
    audit_root: Path,
) -> None:
    if not directory.exists():
        return
    relative = directory.resolve(strict=False).relative_to(layout.root.resolve(strict=False))
    destination = audit_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(directory, destination)
    shutil.rmtree(directory)


def _portfolio_state_payload(
    layout: WorkspaceLayout,
    target_key: str,
) -> dict[str, object] | None:
    state = PortfolioStateStore(layout).read(target_key=target_key)
    return None if state is None else state.to_json_payload()


def _latest_pm_decision_payload(
    layout: WorkspaceLayout,
    target_key: str,
) -> dict[str, object] | None:
    decisions = PMDecisionStore(layout).read_records(target_key=target_key)
    if not decisions:
        return None
    return decisions[-1].record.to_json_payload()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_json_payload(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return payload


def _nested_value(payload: Mapping[str, object], *path: str) -> object:
    current: object = payload
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _nested_text_value(payload: Mapping[str, object], *path: str) -> str | None:
    value = _nested_value(payload, *path)
    return value if isinstance(value, str) and value.strip() else None


def _nested_timestamp_on_or_after(
    payload: Mapping[str, object],
    cutoff: datetime,
    *path: str,
) -> bool:
    value = _nested_value(payload, *path)
    if isinstance(value, Mapping) and "__datetime__" in value:
        nested = value.get("__datetime__")
        if isinstance(nested, str):
            value = nested
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = _parse_dt(value)
    except ValueError:
        return False
    return parsed >= cutoff


def _nested_timestamp_within_rebuild_window(
    payload: Mapping[str, object],
    *,
    start: datetime,
    end: datetime | None,
    path: tuple[str, ...],
) -> bool:
    value = _nested_value(payload, *path)
    if isinstance(value, Mapping) and "__datetime__" in value:
        nested = value.get("__datetime__")
        if isinstance(nested, str):
            value = nested
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = _parse_dt(value)
    except ValueError:
        return False
    return _datetime_in_rebuild_window(parsed, start=start, end=end)


def _pm_review_runtime_payload_should_truncate(
    *,
    payload: Mapping[str, object],
    cutoff: datetime,
    rebuild_until: datetime | None,
    request_ids: set[str],
) -> bool:
    request_id = _text_value(payload, "pm_review_request_id")
    if request_id in request_ids:
        return True
    if _nested_text_value(payload, "item", "pm_review_request_id") in request_ids:
        return True
    if _nested_text_value(payload, "message", "pm_review_request_id") in request_ids:
        return True
    for timestamp_path in (
        ("event_time",),
        ("enqueued_at",),
        ("completed_at",),
        ("recorded_at",),
        ("item", "event_time"),
        ("item", "enqueued_at"),
        ("message", "event_time"),
        ("message", "recorded_at"),
    ):
        if _nested_timestamp_within_rebuild_window(
            payload,
            start=cutoff,
            end=rebuild_until,
            path=timestamp_path,
        ):
            return True
    return False


def _reflection_runtime_payload_should_truncate(
    *,
    payload: Mapping[str, object],
    cutoff: datetime,
    rebuild_until: datetime | None,
) -> bool:
    for timestamp_path in (
        ("checked_at",),
        ("recorded_at",),
        ("completed_at",),
        ("item", "checked_at"),
        ("item", "enqueued_at"),
        ("receipt", "checked_at"),
        ("receipt", "due_check", "checked_at"),
        ("receipt", "due_check", "last_completed_at"),
        ("receipt", "due_check", "next_due_at"),
    ):
        if _nested_timestamp_within_rebuild_window(
            payload,
            start=cutoff,
            end=rebuild_until,
            path=timestamp_path,
        ):
            return True
    return False


def _decision_episode_should_truncate(
    *,
    payload: Mapping[str, object],
    cutoff: datetime,
    rebuild_until: datetime | None,
    removed_decision_ids: set[str],
    removed_execution_record_ids: set[str],
    removed_pm_decision_episode_ids: set[str],
) -> bool:
    record_type = _text_value(payload, "record_type")
    if record_type == "pm_decision":
        return _timestamp_within_rebuild_window(
            payload,
            "business_at",
            start=cutoff,
            end=rebuild_until,
        )
    if record_type == "validation_mark":
        if _timestamp_within_rebuild_window(
            payload,
            "business_at",
            start=cutoff,
            end=rebuild_until,
        ):
            return True
        nested_payload = _nested_value(payload, "payload")
        if isinstance(nested_payload, Mapping):
            if _text_value(nested_payload, "pm_decision_id") in removed_decision_ids:
                return True
            if _text_value(nested_payload, "execution_record_id") in removed_execution_record_ids:
                return True
        return _text_value(payload, "episode_id") in removed_pm_decision_episode_ids
    if record_type in {"reflection", "reflection_learning"}:
        return _timestamp_within_rebuild_window(
            payload,
            "recorded_at",
            start=cutoff,
            end=rebuild_until,
        )
    return False


if __name__ == "__main__":
    main()
