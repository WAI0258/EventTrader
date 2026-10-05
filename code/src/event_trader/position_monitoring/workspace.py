"""Workspace-backed read models for position monitoring."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from event_trader.analysis_assessment_store import AnalysisAssessmentStore
from event_trader.contracts._validators import validate_target_key
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    canonical_view_state_target_weight,
)
from event_trader.execution import ExecutionCostModel, resolve_adjusted_execution_price
from event_trader.execution.store import ExecutionRecordStore, PersistedExecutionRecord
from event_trader.market.adjustments import (
    MarketAdjustmentSidecar,
    adjust_price_for_realized_policy,
    apply_adjustment_policy_to_series,
    load_adjustment_sidecar,
)
from event_trader.market.store import (
    MarketDataStoreError,
    MarketDataStoreManifest,
    market_data_bars_filename,
)
from event_trader.portfolio.store import PMDecisionStore, PortfolioStateStore
from event_trader.position_monitoring.contracts import (
    AnalysisShadowSourceSummary,
    CurrentPortfolioStateSnapshot,
    ExecutionSummary,
    PMDecisionSummary,
    PositionMonitoringAudit,
    PositionMonitoringLine,
    PositionMonitoringMarker,
    PositionMonitoringPoint,
    PositionMonitoringSnapshot,
    PositionMonitoringStatus,
    canonical_state_for_weight,
)
from event_trader.position_monitoring.performance import (
    StrategyLinePoint,
    TargetWeightEvent,
    calculate_cumulative_equity_series,
)
from event_trader.storage import WorkspaceLayout
from event_trader.validation.state_change_store import read_state_changes


class PositionMonitoringWorkspaceError(ValueError):
    """Raised when position monitoring cannot be built from a workspace."""


@dataclass(frozen=True, slots=True)
class ShadowShiftEvent:
    assessment_id: str
    effective_at: datetime
    business_at: datetime
    state: str
    target_weight: float
    adjusted_price: float


@dataclass(frozen=True, slots=True)
class _ReplayMarketRunDescriptor:
    run_root: Path
    bars_path: Path
    window_start: datetime | None
    window_end: datetime | None


def list_position_monitoring_targets(layout: WorkspaceLayout) -> tuple[str, ...]:
    """List target keys with any position-monitoring relevant runtime artifacts."""

    if not isinstance(layout, WorkspaceLayout):
        raise PositionMonitoringWorkspaceError("layout must be a WorkspaceLayout instance.")
    target_keys: set[str] = set()
    for path in _known_target_roots(layout):
        if path.suffix == ".json":
            if path.exists():
                try:
                    target_keys.add(
                        validate_target_key(
                            path.stem,
                            error_type=PositionMonitoringWorkspaceError,
                        )
                    )
                except PositionMonitoringWorkspaceError:
                    continue
            continue
        if not path.exists() or not path.is_dir():
            continue
        for child in path.iterdir():
            if not child.is_dir():
                continue
            try:
                target_keys.add(
                    validate_target_key(child.name, error_type=PositionMonitoringWorkspaceError)
                )
            except PositionMonitoringWorkspaceError:
                continue
    state_root = layout.runtime_root / "portfolio" / "state"
    if state_root.exists() and state_root.is_dir():
        for path in state_root.glob("*.json"):
            try:
                target_keys.add(
                    validate_target_key(path.stem, error_type=PositionMonitoringWorkspaceError)
                )
            except PositionMonitoringWorkspaceError:
                continue
    return tuple(sorted(target_keys))


def build_position_monitoring_snapshot(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    generated_at: datetime | None = None,
    base_value: float = 1.0,
    buy_hold_entry_cost_bps: float | None = None,
    shadow_cost_model: ExecutionCostModel | None = None,
    runtime_mode: str = "live",
    market_data_run_id: str | None = None,
) -> PositionMonitoringSnapshot:
    """Build one workspace-backed position monitoring snapshot."""

    if not isinstance(layout, WorkspaceLayout):
        raise PositionMonitoringWorkspaceError("layout must be a WorkspaceLayout instance.")
    normalized_target = validate_target_key(target_key, error_type=PositionMonitoringWorkspaceError)
    generated = generated_at or datetime.now(UTC)

    persisted_assessments = AnalysisAssessmentStore(layout).read_records(
        target_key=normalized_target
    )
    persisted_decisions = PMDecisionStore(layout).read_records(target_key=normalized_target)
    persisted_executions = ExecutionRecordStore(layout).read_records(target_key=normalized_target)
    portfolio_state = PortfolioStateStore(layout).read(target_key=normalized_target)
    warnings: list[str] = []
    state_changes = tuple(
        state_change
        for state_change in read_state_changes(layout, normalized_target)
        if state_change.source_kind == "pm_execution_sidecar"
    )

    ordered_assessments = tuple(
        sorted(
            persisted_assessments,
            key=lambda item: (
                item.record.business_at,
                item.record.assessment_id,
            ),
        )
    )
    ordered_decisions = tuple(
        sorted(
            persisted_decisions,
            key=lambda item: (
                item.record.business_at,
                item.record.decision_id,
            ),
        )
    )
    decisions = tuple(item.record for item in ordered_decisions)
    executed_records = tuple(
        item for item in persisted_executions if item.record.status == "executed"
    )
    decisions_by_id = {record.decision_id: record for record in decisions}
    sidecars_by_execution_id = {
        state_change.execution_record_id: state_change
        for state_change in state_changes
        if state_change.execution_record_id is not None
    }
    execution_record_ids = {
        item.record.execution_record_id
        for item in persisted_executions
    }
    comparison_start_at = None
    if ordered_assessments:
        comparison_start_at = ordered_assessments[0].record.business_at

    (
        market_data,
        market_symbol,
        bar_granularity,
        market_path,
        market_source_paths,
        market_status,
    ) = _load_market_data(
        layout=layout,
        target_key=normalized_target,
        execution_records=executed_records,
        runtime_mode=runtime_mode,
        market_data_run_id=market_data_run_id,
        comparison_start_at=comparison_start_at,
    )

    current_portfolio_snapshot = _build_current_portfolio_state_snapshot(
        portfolio_state=portfolio_state,
        decisions_by_id=decisions_by_id,
        execution_record_ids=execution_record_ids,
    )

    notes = [
        (
            "Analysis-direct shadow is a counterfactual baseline. It maps "
            "eligible AnalysisAssessment.as_if_flat_state values with memory write receipts "
            "through canonical_view_state_target_weight on the analysis assessment timeline "
            "and was not executed."
        ),
    ]
    latest_pm_decision = None if not decisions else _decision_summary(decisions[-1])
    latest_execution = None
    if persisted_executions:
        latest_execution = _execution_summary(
            max(
                (item.record for item in persisted_executions),
                key=lambda record: (
                    record.recorded_at,
                    record.business_at,
                    record.execution_record_id,
                ),
            )
        )
    eligible_shadow_assessments = _eligible_analysis_shadow_assessments(
        persisted_assessments=ordered_assessments,
    )
    latest_analysis_shadow_source = None
    if eligible_shadow_assessments:
        latest_analysis_shadow_source = _analysis_summary(eligible_shadow_assessments[-1])

    if market_data is None:
        pm_line = PositionMonitoringLine(
            key="pm_pipeline",
            label="PM pipeline",
            role="actual",
            status=PositionMonitoringStatus(
                code="unavailable_market_data",
                explanation="Market bars are unavailable, so the PM pipeline cannot be charted.",
            ),
        )
        buy_hold_line = PositionMonitoringLine(
            key="buy_hold",
            label="Buy & Hold",
            role="baseline",
            status=PositionMonitoringStatus(
                code="unavailable_market_data",
                explanation="Market bars are unavailable, so buy-and-hold cannot be charted.",
            ),
        )
        shadow_line = PositionMonitoringLine(
            key="analysis_direct_shadow",
            label="Analysis-direct shadow - not executed",
            role="counterfactual",
            status=PositionMonitoringStatus(
                code="unavailable_market_data",
                explanation=(
                    "Market bars are unavailable, so the shadow baseline cannot be charted."
                ),
            ),
        )
        comparison_status = PositionMonitoringStatus(
            code="unavailable",
            explanation="Position comparison is unavailable until market bars are present.",
        )
        return PositionMonitoringSnapshot(
            target_key=normalized_target,
            generated_at=generated,
            current_portfolio_state=current_portfolio_snapshot,
            market_data_status=market_status,
            comparison_status=comparison_status,
            market_symbol=market_symbol,
            bar_granularity=bar_granularity,
            market_data_path=market_path,
            base_value=base_value,
            lines=(pm_line, buy_hold_line, shadow_line),
            points=(),
            markers=(),
            latest_pm_decision=latest_pm_decision,
            latest_execution=latest_execution,
            latest_analysis_shadow_source=latest_analysis_shadow_source,
            notes=tuple(notes),
            audit=_build_audit(
                market_path=market_path,
                persisted_assessments=ordered_assessments,
                persisted_decisions=persisted_decisions,
                persisted_executions=persisted_executions,
                state_changes=state_changes,
                market_paths=market_source_paths,
                warnings=warnings,
            ),
        )

    comparison_market_data, unavailable_snapshot = _select_comparison_market_data(
        target_key=normalized_target,
        generated_at=generated,
        market_data=market_data,
        market_status=market_status,
        market_symbol=market_symbol,
        bar_granularity=bar_granularity,
        market_path=market_path,
        base_value=base_value,
        ordered_assessments=ordered_assessments,
        current_portfolio_snapshot=current_portfolio_snapshot,
        latest_pm_decision=latest_pm_decision,
        latest_execution=latest_execution,
        latest_analysis_shadow_source=latest_analysis_shadow_source,
        notes=tuple(notes),
        persisted_decisions=persisted_decisions,
        persisted_executions=persisted_executions,
        state_changes=state_changes,
        market_paths=market_source_paths,
    )
    if unavailable_snapshot is not None:
        return unavailable_snapshot

    realized_adjustment_sidecar = _load_realized_adjustment_sidecar_for_market_paths(
        market_paths=market_source_paths,
        market_symbol=market_symbol,
    )
    canonical_comparison_market_data = _canonicalize_market_data_series(
        market_data=comparison_market_data,
        adjustment_sidecar=realized_adjustment_sidecar,
    )
    pm_series, pm_line, pm_notes, pm_warnings = _build_pm_pipeline_series(
        market_data=canonical_comparison_market_data,
        executed_records=executed_records,
        decisions_by_id=decisions_by_id,
        sidecars_by_execution_id=sidecars_by_execution_id,
        portfolio_state=portfolio_state,
        base_value=base_value,
        adjustment_sidecar=realized_adjustment_sidecar,
    )
    resolved_shadow_cost_model = _resolve_shadow_cost_model(
        shadow_cost_model=shadow_cost_model,
        executed_records=executed_records,
    )
    if shadow_cost_model is None and not executed_records:
        notes.append(
            "Analysis-direct shadow uses a zero-cost next-bar-open fallback until "
            "executed PM records provide paper execution costs."
        )
    (
        shadow_series,
        shadow_line,
        shadow_notes,
        shadow_warnings,
        shadow_marker_events,
    ) = _build_analysis_shadow_series(
        market_data=canonical_comparison_market_data,
        persisted_assessments=ordered_assessments,
        base_value=base_value,
        shadow_cost_model=resolved_shadow_cost_model,
        adjustment_sidecar=None,
    )
    resolved_buy_hold_entry_cost_bps = _resolve_buy_hold_entry_cost_bps(
        buy_hold_entry_cost_bps=buy_hold_entry_cost_bps,
        executed_records=executed_records,
    )
    buy_hold_series = calculate_cumulative_equity_series(
        market_data=canonical_comparison_market_data,
        events=(
            TargetWeightEvent(
                effective_at=canonical_comparison_market_data.bars[0].start_at,
                target_weight=1.0,
                state="strong_long",
                execution_price=(
                    canonical_comparison_market_data.bars[0].open_price
                    * (1.0 + resolved_buy_hold_entry_cost_bps / 10_000.0)
                ),
            ),
        ),
        base_value=base_value,
        default_state="flat",
        adjustment_sidecar=None,
    )
    buy_hold_line = PositionMonitoringLine(
        key="buy_hold",
        label="Buy & Hold",
        role="baseline",
        status=PositionMonitoringStatus(
            code="ready",
            explanation=(
                "Buy-and-hold uses the same market bar window that starts once analysis "
                "is available, with one configured buy-side entry cost at the window start."
            ),
        ),
    )

    notes.extend(pm_notes)
    notes.extend(shadow_notes)
    notes.append(
        "Pipeline vs Benchmarks starts at the first market bar on or after the earliest "
        "analysis assessment."
    )
    warnings.extend(pm_warnings)
    warnings.extend(shadow_warnings)

    last_bar_start = comparison_market_data.bars[-1].start_at
    if decisions and decisions[-1].business_at > last_bar_start:
        notes.append(
            "The latest PM decision occurs after the last available market bar and "
            "is not plotted yet."
        )
    if ordered_assessments and ordered_assessments[-1].record.business_at > last_bar_start:
        notes.append(
            "The latest analysis assessment occurs after the last available market "
            "bar and is not plotted yet."
        )

    comparison_status = _comparison_status(
        pm_line=pm_line,
        shadow_line=shadow_line,
    )
    merged_points = _merge_points(
        market_data=canonical_comparison_market_data,
        pm_series=pm_series,
        buy_hold_series=buy_hold_series,
        shadow_series=shadow_series,
    )
    markers = _build_markers(
        points=merged_points,
        comparison_start_at=ordered_assessments[0].record.business_at,
        executed_records=executed_records,
        decisions_by_id=decisions_by_id,
        shadow_marker_events=shadow_marker_events,
    )

    return PositionMonitoringSnapshot(
        target_key=normalized_target,
        generated_at=generated,
        current_portfolio_state=current_portfolio_snapshot,
        market_data_status=market_status,
        comparison_status=comparison_status,
        market_symbol=market_symbol,
        bar_granularity=bar_granularity,
        market_data_path=market_path,
        base_value=base_value,
        lines=(pm_line, buy_hold_line, shadow_line),
        points=merged_points,
        markers=markers,
        latest_pm_decision=latest_pm_decision,
        latest_execution=latest_execution,
        latest_analysis_shadow_source=latest_analysis_shadow_source,
        notes=tuple(dict.fromkeys(notes)),
        audit=_build_audit(
            market_path=market_path,
            persisted_assessments=ordered_assessments,
            persisted_decisions=persisted_decisions,
            persisted_executions=persisted_executions,
            state_changes=state_changes,
            market_paths=market_source_paths,
            warnings=warnings,
        ),
        )


def _build_current_portfolio_state_snapshot(
    *,
    portfolio_state,
    decisions_by_id: dict[str, object],
    execution_record_ids: set[str],
) -> CurrentPortfolioStateSnapshot:
    if portfolio_state is None:
        return CurrentPortfolioStateSnapshot(
            status=PositionMonitoringStatus(
                code="missing_portfolio_state",
                explanation="No PortfolioState is present for this target.",
            ),
            state=None,
            target_weight=None,
            updated_at=None,
        )
    decision_known = portfolio_state.source_pm_decision_id in decisions_by_id
    execution_known = portfolio_state.source_execution_record_id in execution_record_ids
    if decision_known and execution_known:
        status = PositionMonitoringStatus(
            code="ready",
            explanation="PortfolioState is linked to known PM decision and execution truth.",
        )
    elif not decision_known and not execution_known:
        status = PositionMonitoringStatus(
            code="partial_missing_decision_and_execution_lineage",
            explanation=(
                "PortfolioState exists, but its PM decision and execution lineage are not present "
                "in the readable workspace artifacts."
            ),
        )
    elif not decision_known:
        status = PositionMonitoringStatus(
            code="partial_missing_decision_lineage",
            explanation="PortfolioState exists, but its source PM decision is not readable.",
        )
    else:
        status = PositionMonitoringStatus(
            code="partial_missing_execution_lineage",
            explanation="PortfolioState exists, but its source execution record is not readable.",
        )
    return CurrentPortfolioStateSnapshot(
        status=status,
        state=portfolio_state.state,
        target_weight=portfolio_state.target_weight,
        updated_at=portfolio_state.updated_at,
        source_pm_decision_id=portfolio_state.source_pm_decision_id,
        source_execution_record_id=portfolio_state.source_execution_record_id,
        decision_episode_id=portfolio_state.decision_episode_id,
    )


def _resolve_buy_hold_entry_cost_bps(
    *,
    buy_hold_entry_cost_bps: float | None,
    executed_records: tuple[PersistedExecutionRecord, ...],
) -> float:
    if buy_hold_entry_cost_bps is not None:
        return float(buy_hold_entry_cost_bps)
    if executed_records:
        latest_record = max(
            executed_records,
            key=lambda item: (
                item.record.recorded_at,
                item.record.business_at,
                item.record.execution_record_id,
            ),
        )
        return latest_record.record.buy_cost_bps
    return 0.0


def _resolve_shadow_cost_model(
    *,
    shadow_cost_model: ExecutionCostModel | None,
    executed_records: tuple[PersistedExecutionRecord, ...],
) -> ExecutionCostModel:
    if shadow_cost_model is not None:
        return shadow_cost_model
    if not executed_records:
        return ExecutionCostModel(
            buy_cost_bps=0.0,
            sell_cost_bps=0.0,
            price_basis="open",
        )
    latest_record = max(
        executed_records,
        key=lambda item: (
            item.record.recorded_at,
            item.record.business_at,
            item.record.execution_record_id,
        ),
    )
    return ExecutionCostModel(
        buy_cost_bps=latest_record.record.buy_cost_bps,
        sell_cost_bps=latest_record.record.sell_cost_bps,
        price_basis=latest_record.record.price_basis,
    )


def _build_pm_pipeline_series(
    *,
    market_data: MarketDataSeries,
    executed_records: tuple[PersistedExecutionRecord, ...],
    decisions_by_id: dict[str, object],
    sidecars_by_execution_id: dict[str, object],
    portfolio_state,
    base_value: float,
    adjustment_sidecar: MarketAdjustmentSidecar | None,
) -> tuple[
    tuple[StrategyLinePoint, ...] | None,
    PositionMonitoringLine,
    tuple[str, ...],
    tuple[str, ...],
]:
    if not executed_records:
        if portfolio_state is None:
            return (
                None,
                PositionMonitoringLine(
                    key="pm_pipeline",
                    label="PM pipeline",
                    role="actual",
                    status=PositionMonitoringStatus(
                        code="unavailable_missing_pm_truth",
                        explanation=(
                            "No PortfolioState or executed PM records are present, so the PM "
                            "pipeline cannot be reconstructed from workspace truth."
                        ),
                    ),
                ),
                (),
                (),
            )
        if portfolio_state is not None and portfolio_state.state != "flat":
            return (
                None,
                PositionMonitoringLine(
                    key="pm_pipeline",
                    label="PM pipeline",
                    role="actual",
                    status=PositionMonitoringStatus(
                        code="unavailable_missing_execution_history",
                        explanation=(
                            "PortfolioState is non-flat, but no executed records are readable "
                            "for reconstructing the PM pipeline."
                        ),
                    ),
                ),
                (),
                (),
            )
        return (
            calculate_cumulative_equity_series(
                market_data=market_data,
                events=(),
                base_value=base_value,
                default_state="flat",
                adjustment_sidecar=adjustment_sidecar,
            ),
            PositionMonitoringLine(
                key="pm_pipeline",
                label="PM pipeline",
                role="actual",
                status=PositionMonitoringStatus(
                    code="flat_no_executions",
                    explanation=(
                        "No executed PM records are present, so the PM pipeline stays flat."
                    ),
                ),
            ),
            (),
            (),
        )

    linkage_warnings: list[str] = []
    notes: list[str] = []
    events: list[TargetWeightEvent] = []
    for persisted in executed_records:
        record = persisted.record
        if record.executed_at is None or record.target_weight is None:
            linkage_warnings.append(
                f"Executed record missing execution fields: {record.execution_record_id}"
            )
            continue
        decision = decisions_by_id.get(record.pm_decision_id)
        state = None
        if decision is not None:
            state = decision.requested_state
            if decision.requested_target_weight != record.target_weight:
                linkage_warnings.append(
                    "Execution target_weight does not match PM decision weight: "
                    f"{record.execution_record_id}"
                )
                state = canonical_state_for_weight(record.target_weight)
        else:
            linkage_warnings.append(
                f"Execution missing PM decision linkage: {record.execution_record_id}"
            )
            state = canonical_state_for_weight(record.target_weight)
        if record.execution_record_id not in sidecars_by_execution_id:
            linkage_warnings.append(
                f"Execution missing PM sidecar linkage: {record.execution_record_id}"
            )
        if state is None:
            linkage_warnings.append(
                f"Execution uses a non-canonical target weight: {record.execution_record_id}"
            )
            continue
        events.append(
            TargetWeightEvent(
                effective_at=record.executed_at,
                target_weight=record.target_weight,
                state=state,
                execution_price=(
                    None
                    if record.adjusted_price is None
                    else (
                        adjust_price_for_realized_policy(
                            raw_price=record.adjusted_price,
                            timestamp=record.executed_at,
                            sidecar=adjustment_sidecar,
                        )
                        if adjustment_sidecar is not None
                        else record.adjusted_price
                    )
                ),
            )
        )
    if not events:
        return (
            None,
            PositionMonitoringLine(
                key="pm_pipeline",
                label="PM pipeline",
                role="actual",
                status=PositionMonitoringStatus(
                    code="unavailable_missing_execution_history",
                    explanation=(
                        "Executed records exist, but they cannot be mapped into canonical PM "
                        "weight events."
                    ),
                ),
            ),
            tuple(notes),
            tuple(linkage_warnings),
        )
    line_status = PositionMonitoringStatus(
        code="ready" if not linkage_warnings else "partial_execution_linkage",
        explanation=(
            "PM pipeline is derived from executed PM records."
            if not linkage_warnings
            else (
                "PM pipeline values are derived from executed PM records, but some decision or "
                "sidecar linkage is incomplete."
            )
        ),
    )
    if linkage_warnings:
        notes.append(
            "Some PM executions are charted from execution truth even though part "
            "of their decision or sidecar linkage is incomplete."
        )
    return (
        calculate_cumulative_equity_series(
            market_data=market_data,
            events=tuple(events),
            base_value=base_value,
            default_state="flat",
            adjustment_sidecar=None,
        ),
        PositionMonitoringLine(
            key="pm_pipeline",
            label="PM pipeline",
            role="actual",
            status=line_status,
        ),
        tuple(notes),
        tuple(linkage_warnings),
    )


def _build_analysis_shadow_series(
    *,
    market_data: MarketDataSeries,
    persisted_assessments,
    base_value: float,
    shadow_cost_model: ExecutionCostModel,
    adjustment_sidecar: MarketAdjustmentSidecar | None,
) -> tuple[
    tuple[StrategyLinePoint, ...] | None,
    PositionMonitoringLine,
    tuple[str, ...],
    tuple[str, ...],
    tuple[ShadowShiftEvent, ...],
]:
    if not persisted_assessments:
        return (
            None,
            PositionMonitoringLine(
                key="analysis_direct_shadow",
                label="Analysis-direct shadow - not executed",
                role="counterfactual",
                status=PositionMonitoringStatus(
                    code="unavailable_no_analysis_assessments",
                    explanation="No analysis assessments are available for the shadow baseline.",
                ),
            ),
            (),
            (),
            (),
        )
    assessments = _eligible_analysis_shadow_assessments(
        persisted_assessments=persisted_assessments,
    )
    if not assessments:
        return (
            None,
            PositionMonitoringLine(
                key="analysis_direct_shadow",
                label="Analysis-direct shadow - not executed",
                role="counterfactual",
                status=PositionMonitoringStatus(
                    code="unavailable_no_eligible_analysis_assessments",
                    explanation=(
                        "No analysis assessments with memory write receipts are available "
                        "for the shadow baseline."
                    ),
                ),
            ),
            (),
            (),
            (),
        )
    events: list[TargetWeightEvent] = []
    marker_events_by_effective_at: dict[datetime, ShadowShiftEvent] = {}
    previous_shadow_state = "flat"
    previous_shadow_weight = 0.0
    skipped_after_last_bar_count = 0
    for assessment in assessments:
        state = assessment.as_if_flat_state
        target_weight = _analysis_weight(assessment)
        if state == previous_shadow_state and target_weight == previous_shadow_weight:
            continue
        selected_bar = _select_next_bar_on_or_after(
            bars=market_data.bars,
            business_at=assessment.business_at,
        )
        if selected_bar is None:
            skipped_after_last_bar_count += 1
            continue
        raw_price = (
            selected_bar.open_price
            if shadow_cost_model.price_basis == "open"
            else selected_bar.close_price
        )
        _total_cost_bps, adjusted_price = resolve_adjusted_execution_price(
            raw_price=raw_price,
            delta_weight=target_weight - previous_shadow_weight,
            cost_model=shadow_cost_model,
        )
        events.append(
            TargetWeightEvent(
                effective_at=selected_bar.start_at,
                target_weight=target_weight,
                state=state,
                execution_price=adjusted_price,
            )
        )
        marker_events_by_effective_at[selected_bar.start_at] = ShadowShiftEvent(
            assessment_id=assessment.assessment_id,
            effective_at=selected_bar.start_at,
            business_at=assessment.business_at,
            state=state,
            target_weight=target_weight,
            adjusted_price=adjusted_price,
        )
        previous_shadow_state = state
        previous_shadow_weight = target_weight
    notes: list[str] = []
    if skipped_after_last_bar_count:
        notes.append(
            "Some eligible analysis assessments occur after the last available market bar, "
            "so the analysis-direct shadow cannot plot those stance shifts yet."
        )
    return (
        calculate_cumulative_equity_series(
            market_data=market_data,
            events=tuple(events),
            base_value=base_value,
            default_state="flat",
            adjustment_sidecar=adjustment_sidecar,
        ),
        PositionMonitoringLine(
            key="analysis_direct_shadow",
            label="Analysis-direct shadow - not executed",
            role="counterfactual",
            status=PositionMonitoringStatus(
                code=(
                    "ready"
                    if skipped_after_last_bar_count == 0
                    else "partial_after_last_market_bar"
                ),
                explanation=(
                    "The shadow baseline follows eligible analysis assessments with memory "
                    "write receipts on the analysis assessment timeline and uses paper "
                    "execution pricing on the first tradable bar on or after each assessment."
                    if skipped_after_last_bar_count == 0
                    else (
                        "The shadow baseline follows eligible analysis assessments with "
                        "memory write receipts on the analysis assessment timeline and uses "
                        "paper execution pricing on the first tradable bar on or after each "
                        "assessment, but some eligible assessment-driven shifts fall after "
                        "the last available market bar."
                    )
                ),
            ),
        ),
        tuple(notes),
        (),
        tuple(
            marker_events_by_effective_at[effective_at]
            for effective_at in sorted(marker_events_by_effective_at)
        ),
    )


def _merge_points(
    *,
    market_data: MarketDataSeries,
    pm_series: tuple[StrategyLinePoint, ...] | None,
    buy_hold_series: tuple[StrategyLinePoint, ...],
    shadow_series: tuple[StrategyLinePoint, ...] | None,
) -> tuple[PositionMonitoringPoint, ...]:
    pm_by_time = {} if pm_series is None else {point.time: point for point in pm_series}
    shadow_by_time = {} if shadow_series is None else {point.time: point for point in shadow_series}
    buy_hold_by_time = {point.time: point for point in buy_hold_series}
    bars_by_start = {bar.start_at: bar for bar in market_data.bars}
    bars_by_end = {bar.end_at: bar for bar in market_data.bars}
    points: list[PositionMonitoringPoint] = []
    all_times = sorted(
        set(pm_by_time)
        | set(shadow_by_time)
        | set(buy_hold_by_time)
    )
    for time in all_times:
        boundary_bar = bars_by_end.get(time)
        if boundary_bar is not None:
            price = boundary_bar.close_price
        else:
            boundary_bar = bars_by_start.get(time)
            if boundary_bar is None:
                raise PositionMonitoringWorkspaceError(
                    "comparison point time must match a market bar boundary."
                )
            price = boundary_bar.open_price
        pm_point = pm_by_time.get(time)
        buy_hold_point = buy_hold_by_time.get(time)
        shadow_point = shadow_by_time.get(time)
        points.append(
            PositionMonitoringPoint(
                time=time,
                price=price,
                pm_pipeline_value=None if pm_point is None else pm_point.value,
                buy_hold_value=None if buy_hold_point is None else buy_hold_point.value,
                analysis_direct_shadow_value=None if shadow_point is None else shadow_point.value,
                pm_target_weight=None if pm_point is None else pm_point.target_weight,
                analysis_shadow_target_weight=(
                    None if shadow_point is None else shadow_point.target_weight
                ),
                pm_state=None if pm_point is None else pm_point.state,
                analysis_shadow_state=None if shadow_point is None else shadow_point.state,
            )
        )
    return tuple(points)


def _build_markers(
    *,
    points: tuple[PositionMonitoringPoint, ...],
    comparison_start_at: datetime,
    executed_records: tuple[PersistedExecutionRecord, ...],
    decisions_by_id: dict[str, object],
    shadow_marker_events: tuple[ShadowShiftEvent, ...],
) -> tuple[PositionMonitoringMarker, ...]:
    if not points:
        return ()
    plot_start_at = points[0].time
    included_points = tuple(point for point in points if point.time >= plot_start_at)
    markers: list[PositionMonitoringMarker] = []
    previous_pm_weight: float | None = None
    previous_pm_state = None
    for persisted in executed_records:
        record = persisted.record
        if record.executed_at is None or record.target_weight is None:
            continue
        if record.executed_at < comparison_start_at:
            continue
        decision = decisions_by_id.get(record.pm_decision_id)
        state = None if decision is None else decision.requested_state
        if state is None or (
            decision is not None and decision.requested_target_weight != record.target_weight
        ):
            state = canonical_state_for_weight(record.target_weight)
        if state is None:
            continue
        plotted_point = _point_for_business_time(included_points, record.executed_at)
        if plotted_point is None:
            continue
        if (
            previous_pm_state == state
            and previous_pm_weight == record.target_weight
        ):
            continue
        markers.append(
            PositionMonitoringMarker(
                marker_id=f"pm:{record.execution_record_id}",
                kind="pm_decision",
                lane="pm",
                label="PM pipeline shift",
                time=plotted_point.time,
                business_at=record.executed_at,
                state=state,
                target_weight=record.target_weight,
                price=record.adjusted_price,
                line_value=plotted_point.pm_pipeline_value,
                shape=(
                    "circle"
                    if previous_pm_weight is None
                    else (
                        "arrowDown" if record.target_weight < previous_pm_weight else "arrowUp"
                    )
                ),
                position=(
                    "belowBar"
                    if previous_pm_weight is None
                    or record.target_weight >= previous_pm_weight
                    else "aboveBar"
                ),
                text="PM",
                source_id=record.execution_record_id,
            )
        )
        previous_pm_state = state
        previous_pm_weight = record.target_weight

    previous_analysis_weight: float | None = None
    previous_analysis_state = None
    for shadow_event in shadow_marker_events:
        if shadow_event.business_at < comparison_start_at:
            continue
        plotted_point = _point_for_business_time(included_points, shadow_event.business_at)
        if plotted_point is None:
            continue
        if (
            previous_analysis_state == shadow_event.state
            and previous_analysis_weight == shadow_event.target_weight
        ):
            continue
        markers.append(
            PositionMonitoringMarker(
                marker_id=f"analysis:{shadow_event.assessment_id}",
                kind="analysis_assessment",
                lane="analysis",
                label="Analysis shadow shift",
                time=shadow_event.effective_at,
                business_at=shadow_event.business_at,
                state=shadow_event.state,
                target_weight=shadow_event.target_weight,
                price=shadow_event.adjusted_price,
                line_value=plotted_point.analysis_direct_shadow_value,
                shape="circle" if previous_analysis_weight is None else "square",
                position=(
                    "inBar"
                    if previous_analysis_weight is None
                    or shadow_event.target_weight >= previous_analysis_weight
                    else "aboveBar"
                ),
                text="AN",
                source_id=shadow_event.assessment_id,
            )
        )
        previous_analysis_state = shadow_event.state
        previous_analysis_weight = shadow_event.target_weight

    return tuple(
        sorted(
            markers,
            key=lambda marker: (marker.time, marker.business_at, marker.marker_id),
        )
    )


def _point_for_business_time(
    points: tuple[PositionMonitoringPoint, ...],
    business_at: datetime,
) -> PositionMonitoringPoint | None:
    for point in points:
        if point.time >= business_at:
            return point
    return None


def _select_next_bar_on_or_after(
    *,
    bars: tuple[MarketDataBar, ...],
    business_at: datetime,
) -> MarketDataBar | None:
    for bar in bars:
        if bar.start_at >= business_at:
            return bar
    return None


def _comparison_status(
    *,
    pm_line: PositionMonitoringLine,
    shadow_line: PositionMonitoringLine,
) -> PositionMonitoringStatus:
    if pm_line.status.code.startswith("unavailable"):
        return PositionMonitoringStatus(
            code="partial",
            explanation=(
                "Market data is present, but the PM pipeline cannot be fully "
                "reconstructed from the available execution history."
            ),
        )
    if (
        shadow_line.status.code.startswith("unavailable")
        or shadow_line.status.code.startswith("partial")
    ):
        return PositionMonitoringStatus(
            code="partial",
            explanation=(
                "PM pipeline and buy-and-hold are available, but the shadow baseline "
                "is partial."
            ),
        )
    if pm_line.status.code.startswith("partial"):
        return PositionMonitoringStatus(
            code="partial",
            explanation=(
                "Position comparison is available, but some PM execution linkage is "
                "partial."
            ),
        )
    return PositionMonitoringStatus(
        code="ready",
        explanation="All three comparison lines are available on the shared market bar window.",
    )


def _build_audit(
    *,
    market_path: Path | None,
    persisted_assessments,
    persisted_decisions,
    persisted_executions,
    state_changes,
    market_paths: tuple[Path, ...],
    warnings: list[str] | tuple[str, ...],
) -> PositionMonitoringAudit:
    source_paths: list[Path] = []
    if market_path is not None:
        source_paths.append(market_path)
    source_paths.extend(market_paths)
    source_paths.extend(
        sorted(
            {
                persisted.path.resolve(strict=False)
                for persisted in (
                    *persisted_assessments,
                    *persisted_decisions,
                    *persisted_executions,
                )
            }
        )
    )
    source_paths.extend(
        sorted(
            {
                _state_change_path_for(state_change)
                for state_change in state_changes
            }
        )
    )
    return PositionMonitoringAudit(
        source_paths=tuple(dict.fromkeys(source_paths)),
        counts={
            "analysis_assessments": len(persisted_assessments),
            "pm_decisions": len(persisted_decisions),
            "execution_records": len(persisted_executions),
            "pm_sidecar_view_state_changes": len(state_changes),
        },
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _load_market_data(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    execution_records: tuple[PersistedExecutionRecord, ...],
    runtime_mode: str,
    market_data_run_id: str | None,
    comparison_start_at: datetime | None,
) -> tuple[
    MarketDataSeries | None,
    str | None,
    str | None,
    Path | None,
    tuple[Path, ...],
    PositionMonitoringStatus,
]:
    symbol, granularity = _infer_market_spec(
        layout=layout,
        target_key=target_key,
        execution_records=execution_records,
        runtime_mode=runtime_mode,
        market_data_run_id=market_data_run_id,
    )
    if symbol is None or granularity is None:
        return (
            None,
            None,
            None,
            None,
            (),
            PositionMonitoringStatus(
                code="missing_market_spec",
                explanation=(
                    "Market bars could not be resolved from execution provenance, the relevant "
                    "market-data manifest, or the available bar files."
                ),
            ),
        )
    if runtime_mode == "replay":
        (
            market_data,
            market_path,
            market_source_paths,
            market_status,
        ) = _load_replay_market_data_series(
            layout=layout,
            target_key=target_key,
            market_symbol=symbol,
            bar_granularity=granularity,
            market_data_run_id=market_data_run_id,
            comparison_start_at=comparison_start_at,
        )
        return (
            market_data,
            symbol,
            granularity,
            market_path,
            market_source_paths,
            market_status,
        )
    market_path, ready_explanation, missing_path, missing_status = _resolve_live_market_data_path(
        layout=layout,
        target_key=target_key,
        market_symbol=symbol,
        bar_granularity=granularity,
    )
    if market_path is None:
        return (
            None,
            symbol,
            granularity,
            missing_path,
            (() if missing_path is None else (missing_path,)),
            missing_status
            if missing_status is not None
            else PositionMonitoringStatus(
                code="missing_market_bars",
                explanation="Market bar file is missing.",
            ),
        )
    try:
        market_data = _read_market_data_series(market_path)
    except PositionMonitoringWorkspaceError as exc:
        return (
            None,
            symbol,
            granularity,
            market_path,
            (market_path,),
            PositionMonitoringStatus(
                code="unreadable_market_bars",
                explanation=str(exc),
            ),
        )
    replay_market_data, _replay_root, replay_paths, _replay_status = (
        _load_replay_market_data_series(
            layout=layout,
            target_key=target_key,
            market_symbol=symbol,
            bar_granularity=granularity,
            market_data_run_id=market_data_run_id,
            comparison_start_at=comparison_start_at,
        )
    )
    if replay_market_data is not None:
        combined_market_data = _prepend_replay_history_to_live_market_data(
            replay_market_data=replay_market_data,
            live_market_data=market_data,
        )
        if replay_paths and len(combined_market_data.bars) > len(market_data.bars):
            return (
                combined_market_data,
                symbol,
                granularity,
                market_path,
                tuple((*replay_paths, market_path)),
                PositionMonitoringStatus(
                    code="ready",
                    explanation=(
                        "Market bars were assembled from replay history plus the local "
                        "live market-data store."
                    ),
                ),
            )
    return (
        market_data,
        symbol,
        granularity,
        market_path,
        (market_path,),
        PositionMonitoringStatus(
            code="ready",
            explanation=ready_explanation,
        ),
    )


def _prepend_replay_history_to_live_market_data(
    *,
    replay_market_data: MarketDataSeries,
    live_market_data: MarketDataSeries,
) -> MarketDataSeries:
    first_live_start = live_market_data.bars[0].start_at
    replay_history = tuple(
        bar for bar in replay_market_data.bars if bar.start_at < first_live_start
    )
    if not replay_history:
        return live_market_data
    return MarketDataSeries(
        bars=tuple(
            sorted(
                (*replay_history, *live_market_data.bars),
                key=lambda bar: bar.start_at,
            )
        )
    )


def _infer_market_spec(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    execution_records: tuple[PersistedExecutionRecord, ...],
    runtime_mode: str,
    market_data_run_id: str | None,
) -> tuple[str | None, str | None]:
    for persisted in reversed(execution_records):
        provenance = persisted.record.provenance
        if provenance is None:
            continue
        if provenance.market_symbol and provenance.bar_granularity:
            return provenance.market_symbol, provenance.bar_granularity
    if runtime_mode == "replay":
        return _infer_replay_market_spec(
            layout=layout,
            target_key=target_key,
            market_data_run_id=market_data_run_id,
        )
    manifest_path = (
        layout.runtime_root / "live_market_data" / "manifest" / f"{target_key}.json"
    ).resolve(strict=False)
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            primary_subscription = _parse_primary_subscription(payload.get("primary_subscription"))
            if primary_subscription is not None:
                return primary_subscription
            subscriptions = payload.get("subscriptions")
            if isinstance(subscriptions, list):
                for item in subscriptions:
                    parsed = _parse_subscription_spec(item, require_primary=True)
                    if parsed is not None:
                        return parsed
    rest_bars_root = (
        layout.runtime_root / "live_market_data" / "raw" / "rest_bars" / target_key
    ).resolve(strict=False)
    if rest_bars_root.is_dir():
        matches = sorted(rest_bars_root.glob("*.jsonl"))
        if len(matches) == 1:
            parsed = _parse_symbol_granularity_from_bar_name(matches[0].name)
            if parsed is not None:
                return parsed
    return None, None


def _parse_primary_subscription(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    return _parse_subscription_spec(value, require_primary=False)


def _parse_subscription_spec(
    value: object,
    *,
    require_primary: bool,
) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    if require_primary and value.get("purpose") != "primary_tradable":
        return None
    symbol = value.get("symbol")
    granularity = value.get("bar_granularity")
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    if not isinstance(granularity, str) or not granularity.strip():
        return None
    return symbol.strip(), granularity.strip()


def _infer_replay_market_spec(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    market_data_run_id: str | None,
) -> tuple[str | None, str | None]:
    target_market_root = (layout.runtime_root / "market_data" / target_key).resolve(strict=False)
    run_roots = _iter_replay_market_run_roots(
        target_market_root=target_market_root,
        preferred_run_id=market_data_run_id,
    )
    if not run_roots:
        return None, None
    resolved_specs = tuple(
        spec
        for spec in (
            _infer_replay_market_spec_from_run_root(run_root)
            for run_root in run_roots
        )
        if spec != (None, None)
    )
    if not resolved_specs:
        return None, None
    unique_specs = tuple(dict.fromkeys(resolved_specs))
    if len(unique_specs) == 1:
        return unique_specs[0]
    return None, None


def _infer_replay_market_spec_from_run_root(run_root: Path) -> tuple[str | None, str | None]:
    manifest_path = run_root / "manifest.json"
    if manifest_path.is_file():
        resolved = _parse_replay_manifest_primary_market_spec(manifest_path)
        if resolved is not None:
            return resolved
    bars_root = run_root / "bars"
    if not bars_root.is_dir():
        return None, None
    matches = sorted(bars_root.glob("*.jsonl"))
    if len(matches) == 1:
        return _parse_symbol_granularity_from_bar_name(matches[0].name) or (None, None)
    return None, None


def _parse_replay_manifest_primary_market_spec(
    manifest_path: Path,
) -> tuple[str, str] | None:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    source_metadata = payload.get("source_metadata")
    bar_symbols = payload.get("bar_symbols")
    if not isinstance(source_metadata, dict) or not isinstance(bar_symbols, list):
        return None
    for raw_symbol in bar_symbols:
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            continue
        symbol = raw_symbol.strip()
        prefix = f"bars_{symbol}_"
        if source_metadata.get(prefix + "configured_purpose") != "primary_tradable":
            continue
        configured_symbol = _read_manifest_text(
            source_metadata.get(prefix + "configured_market_symbol")
        )
        granularity = _read_manifest_text(
            source_metadata.get(prefix + "configured_bar_granularity")
        )
        if configured_symbol is not None and granularity is not None:
            return configured_symbol, granularity
    return None


def _read_manifest_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped


def _load_replay_market_data_series(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    market_symbol: str,
    bar_granularity: str,
    market_data_run_id: str | None,
    comparison_start_at: datetime | None,
) -> tuple[
    MarketDataSeries | None,
    Path | None,
    tuple[Path, ...],
    PositionMonitoringStatus,
]:
    bars_name = market_data_bars_filename(
        symbol=market_symbol,
        granularity=bar_granularity,
    )
    target_market_root = (layout.runtime_root / "market_data" / target_key).resolve(strict=False)
    run_roots = _iter_replay_market_run_roots(
        target_market_root=target_market_root,
        preferred_run_id=market_data_run_id,
    )
    if not run_roots:
        return (
            None,
            target_market_root,
            (),
            PositionMonitoringStatus(
                code="missing_market_bars",
                explanation=(
                    "Replay market-data runs are missing under "
                    f"{target_market_root} for {bars_name}."
                ),
            ),
        )
    descriptors = tuple(
        descriptor
        for descriptor in (
            _describe_replay_market_run(run_root=run_root, bars_name=bars_name)
            for run_root in run_roots
        )
        if descriptor is not None
    )
    if not descriptors:
        return (
            None,
            target_market_root,
            (),
            PositionMonitoringStatus(
                code="missing_market_bars",
                explanation=(
                    "Replay market bar files are missing under "
                    f"{target_market_root} for {bars_name}."
                ),
            ),
        )
    overlapping_descriptors = tuple(
        descriptor
        for descriptor in descriptors
        if _replay_run_overlaps_window(
            descriptor=descriptor,
            comparison_start_at=comparison_start_at,
        )
    )
    candidate_descriptors = overlapping_descriptors or descriptors
    bars_by_start_at: dict[datetime, MarketDataBar] = {}
    contributing_paths: list[Path] = []
    for descriptor in _ordered_replay_market_run_descriptors(candidate_descriptors):
        try:
            series = _read_market_data_series(descriptor.bars_path)
        except PositionMonitoringWorkspaceError as exc:
            return (
                None,
                target_market_root,
                tuple(contributing_paths + [descriptor.bars_path]),
                PositionMonitoringStatus(
                    code="unreadable_market_bars",
                    explanation=str(exc),
                ),
            )
        contributing_paths.append(descriptor.bars_path)
        for bar in series.bars:
            bars_by_start_at[bar.start_at] = bar
    if not bars_by_start_at:
        return (
            None,
            target_market_root,
            tuple(contributing_paths),
            PositionMonitoringStatus(
                code="missing_market_bars",
                explanation=(
                    "Replay market bar files were found, but no bars could be assembled into "
                    "a target-level comparison timeline."
                ),
            ),
        )
    try:
        market_data = MarketDataSeries(
            bars=tuple(sorted(bars_by_start_at.values(), key=lambda bar: bar.start_at))
        )
    except ValueError as exc:
        return (
            None,
            target_market_root,
            tuple(contributing_paths),
            PositionMonitoringStatus(
                code="unreadable_market_bars",
                explanation=(
                    "Replay market bars could not be assembled into a non-overlapping target "
                    f"timeline: {exc}"
                ),
            ),
        )
    return (
        market_data,
        target_market_root,
        tuple(contributing_paths),
        PositionMonitoringStatus(
            code="ready",
            explanation=(
                "Market bars were assembled from overlapping replay market-data runs into one "
                "continuous target-level timeline."
            ),
        ),
    )


def _resolve_live_market_data_path(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    market_symbol: str,
    bar_granularity: str,
) -> tuple[Path | None, str, Path | None, PositionMonitoringStatus | None]:
    bars_name = market_data_bars_filename(
        symbol=market_symbol,
        granularity=bar_granularity,
    )
    market_path = (
        layout.runtime_root
        / "live_market_data"
        / "raw"
        / "rest_bars"
        / target_key
        / bars_name
    ).resolve(strict=False)
    if market_path.is_file():
        return (
            market_path,
            "Market bars were loaded from the local live market-data store.",
            None,
            None,
        )
    return (
        None,
        "",
        market_path,
        PositionMonitoringStatus(
            code="missing_market_bars",
            explanation=f"Market bar file is missing: {market_path}",
        ),
    )


def _iter_replay_market_run_roots(
    *,
    target_market_root: Path,
    preferred_run_id: str | None,
) -> tuple[Path, ...]:
    if not target_market_root.is_dir():
        return ()
    run_roots = tuple(sorted(path for path in target_market_root.iterdir() if path.is_dir()))
    if preferred_run_id is None:
        return run_roots
    preferred_root = (target_market_root / preferred_run_id).resolve(strict=False)
    if preferred_root not in run_roots:
        return run_roots
    return tuple(path for path in run_roots if path != preferred_root) + (preferred_root,)


def _describe_replay_market_run(
    *,
    run_root: Path,
    bars_name: str,
) -> _ReplayMarketRunDescriptor | None:
    bars_path = (run_root / "bars" / bars_name).resolve(strict=False)
    if not bars_path.is_file():
        return None
    manifest_path = run_root / "manifest.json"
    manifest_window_start = None
    manifest_window_end = None
    if manifest_path.is_file():
        try:
            manifest = MarketDataStoreManifest.from_dict(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, MarketDataStoreError, ValueError):
            manifest = None
        if manifest is not None:
            manifest_window_start = manifest.window_start
            manifest_window_end = manifest.window_end
    return _ReplayMarketRunDescriptor(
        run_root=run_root,
        bars_path=bars_path,
        window_start=manifest_window_start,
        window_end=manifest_window_end,
    )


def _replay_run_overlaps_window(
    *,
    descriptor: _ReplayMarketRunDescriptor,
    comparison_start_at: datetime | None,
) -> bool:
    if comparison_start_at is None:
        return True
    if descriptor.window_end is None:
        return True
    return descriptor.window_end >= comparison_start_at


def _ordered_replay_market_run_descriptors(
    descriptors: tuple[_ReplayMarketRunDescriptor, ...],
) -> tuple[_ReplayMarketRunDescriptor, ...]:
    return tuple(
        sorted(
            descriptors,
            key=lambda descriptor: (
                datetime.min.replace(tzinfo=UTC)
                if descriptor.window_start is None
                else descriptor.window_start,
                datetime.min.replace(tzinfo=UTC)
                if descriptor.window_end is None
                else descriptor.window_end,
                descriptor.run_root.name,
            ),
        )
    )


def _read_market_data_series(path: Path) -> MarketDataSeries:
    bars: list[MarketDataBar] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PositionMonitoringWorkspaceError(f"failed to read market bars: {path}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PositionMonitoringWorkspaceError(
                f"invalid market bar JSON at {path}:{line_number}"
            ) from exc
        if not isinstance(payload, dict):
            raise PositionMonitoringWorkspaceError(
                f"market bar payload must be an object at {path}:{line_number}"
            )
        bars.append(_parse_market_bar(payload))
    if not bars:
        raise PositionMonitoringWorkspaceError(f"market bar file is empty: {path}")
    return MarketDataSeries(bars=tuple(sorted(bars, key=lambda bar: bar.start_at)))


def _load_realized_adjustment_sidecar_for_market_paths(
    *,
    market_paths: tuple[Path, ...],
    market_symbol: str | None,
) -> MarketAdjustmentSidecar | None:
    if not market_paths or market_symbol is None:
        return None
    for market_path in reversed(market_paths):
        store_root = market_path.resolve(strict=False).parent.parent
        manifest_path = store_root / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = MarketDataStoreManifest.from_dict(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, MarketDataStoreError, ValueError):
            continue
        timezone_name = manifest.source_metadata.get(f"bars_{market_symbol}_timezone")
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            continue
        try:
            return load_adjustment_sidecar(
                root=store_root,
                market_symbol=market_symbol,
                policy="forward_adjusted_realized",
                timezone_name=timezone_name.strip(),
                source_kind="replay_store",
            )
        except ValueError:
            continue
    return None


def _canonicalize_market_data_series(
    *,
    market_data: MarketDataSeries,
    adjustment_sidecar: MarketAdjustmentSidecar | None,
) -> MarketDataSeries:
    if adjustment_sidecar is None:
        return market_data
    return apply_adjustment_policy_to_series(
        series=market_data,
        sidecar=adjustment_sidecar,
    ).series


def _parse_market_bar(payload: dict[str, object]) -> MarketDataBar:
    return MarketDataBar(
        start_at=_parse_timestamp(payload.get("start_at"), "start_at"),
        end_at=_parse_timestamp(payload.get("end_at"), "end_at"),
        open_price=_parse_float(payload, "open", alias="open_price"),
        high_price=_parse_float(payload, "high", alias="high_price"),
        low_price=_parse_float(payload, "low", alias="low_price"),
        close_price=_parse_float(payload, "close", alias="close_price"),
        volume=_parse_float(payload, "volume"),
        vwap=None if payload.get("vwap") is None else _parse_float(payload, "vwap"),
    )


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise PositionMonitoringWorkspaceError(f"{field_name} must be an ISO timestamp string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PositionMonitoringWorkspaceError(
            f"{field_name} must be an ISO timestamp string."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PositionMonitoringWorkspaceError(f"{field_name} must be timezone-aware.")
    return parsed.astimezone(UTC)


def _parse_float(payload: dict[str, object], key: str, *, alias: str | None = None) -> float:
    value = payload.get(key)
    if value is None and alias is not None:
        value = payload.get(alias)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PositionMonitoringWorkspaceError(f"{key} must be numeric.")
    return float(value)


def _analysis_weight(record: AnalysisAssessment) -> float:
    return canonical_view_state_target_weight(record.as_if_flat_state)


def _select_comparison_market_data(
    *,
    target_key: str,
    generated_at: datetime,
    market_data: MarketDataSeries,
    market_status: PositionMonitoringStatus,
    market_symbol: str | None,
    bar_granularity: str | None,
    market_path: Path | None,
    base_value: float,
    ordered_assessments,
    current_portfolio_snapshot: CurrentPortfolioStateSnapshot,
    latest_pm_decision: PMDecisionSummary | None,
    latest_execution: ExecutionSummary | None,
    latest_analysis_shadow_source: AnalysisShadowSourceSummary | None,
    notes: tuple[str, ...],
    persisted_decisions,
    persisted_executions,
    state_changes,
    market_paths: tuple[Path, ...],
) -> tuple[MarketDataSeries, None] | tuple[None, PositionMonitoringSnapshot]:
    if not ordered_assessments:
        return None, _build_unavailable_comparison_snapshot(
            target_key=target_key,
            generated_at=generated_at,
            current_portfolio_snapshot=current_portfolio_snapshot,
            market_status=market_status,
            comparison_status=PositionMonitoringStatus(
                code="unavailable_no_analysis_window",
                explanation=(
                    "No analysis assessments are available, so Pipeline vs Benchmarks "
                    "cannot start."
                ),
            ),
            market_symbol=market_symbol,
            bar_granularity=bar_granularity,
            market_path=market_path,
            base_value=base_value,
            lines=(
                PositionMonitoringLine(
                    key="pm_pipeline",
                    label="PM pipeline",
                    role="actual",
                    status=PositionMonitoringStatus(
                        code="unavailable_no_analysis_window",
                        explanation=(
                            "PM pipeline comparison is unavailable until the target has at least "
                            "one analysis assessment."
                        ),
                    ),
                ),
                PositionMonitoringLine(
                    key="buy_hold",
                    label="Buy & Hold",
                    role="baseline",
                    status=PositionMonitoringStatus(
                        code="unavailable_no_analysis_window",
                        explanation=(
                            "Buy-and-hold is not plotted before analysis is available for the "
                            "target."
                        ),
                    ),
                ),
                PositionMonitoringLine(
                    key="analysis_direct_shadow",
                    label="Analysis-direct shadow - not executed",
                    role="counterfactual",
                    status=PositionMonitoringStatus(
                        code="unavailable_no_analysis_assessments",
                        explanation=(
                            "No analysis assessments are available for the shadow baseline."
                        ),
                    ),
                ),
            ),
            latest_pm_decision=latest_pm_decision,
            latest_execution=latest_execution,
            latest_analysis_shadow_source=latest_analysis_shadow_source,
            notes=notes,
            persisted_assessments=ordered_assessments,
            persisted_decisions=persisted_decisions,
            persisted_executions=persisted_executions,
            state_changes=state_changes,
            market_paths=market_paths,
            warnings=(),
        )
    comparison_start_at = ordered_assessments[0].record.business_at
    comparison_bars = tuple(
        bar
        for bar in market_data.bars
        if bar.start_at >= comparison_start_at
    )
    if not comparison_bars:
        return None, _build_unavailable_comparison_snapshot(
            target_key=target_key,
            generated_at=generated_at,
            current_portfolio_snapshot=current_portfolio_snapshot,
            market_status=market_status,
            comparison_status=PositionMonitoringStatus(
                code="unavailable_no_market_bars_after_analysis_start",
                explanation=(
                    "Analysis assessments exist, but there are no market bars on or after the "
                    "first analysis assessment."
                ),
            ),
            market_symbol=market_symbol,
            bar_granularity=bar_granularity,
            market_path=market_path,
            base_value=base_value,
            lines=(
                PositionMonitoringLine(
                    key="pm_pipeline",
                    label="PM pipeline",
                    role="actual",
                    status=PositionMonitoringStatus(
                        code="unavailable_no_market_bars_after_analysis_start",
                        explanation=(
                            "No market bars are available on or after the first analysis "
                            "assessment, so the PM pipeline cannot be plotted."
                        ),
                    ),
                ),
                PositionMonitoringLine(
                    key="buy_hold",
                    label="Buy & Hold",
                    role="baseline",
                    status=PositionMonitoringStatus(
                        code="unavailable_no_market_bars_after_analysis_start",
                        explanation=(
                            "No market bars are available on or after the first analysis "
                            "assessment, so buy-and-hold cannot be plotted."
                        ),
                    ),
                ),
                PositionMonitoringLine(
                    key="analysis_direct_shadow",
                    label="Analysis-direct shadow - not executed",
                    role="counterfactual",
                    status=PositionMonitoringStatus(
                        code="unavailable_no_market_bars_after_analysis_start",
                        explanation=(
                            "No market bars are available on or after the first analysis "
                            "assessment, so the shadow baseline cannot be plotted."
                        ),
                    ),
                ),
            ),
            latest_pm_decision=latest_pm_decision,
            latest_execution=latest_execution,
            latest_analysis_shadow_source=latest_analysis_shadow_source,
            notes=notes,
            persisted_assessments=ordered_assessments,
            persisted_decisions=persisted_decisions,
            persisted_executions=persisted_executions,
            state_changes=state_changes,
            market_paths=market_paths,
            warnings=(),
        )
    return MarketDataSeries(bars=comparison_bars), None


def _build_unavailable_comparison_snapshot(
    *,
    target_key: str,
    generated_at: datetime,
    current_portfolio_snapshot: CurrentPortfolioStateSnapshot,
    market_status: PositionMonitoringStatus,
    comparison_status: PositionMonitoringStatus,
    market_symbol: str | None,
    bar_granularity: str | None,
    market_path: Path | None,
    base_value: float,
    lines: tuple[PositionMonitoringLine, ...],
    latest_pm_decision: PMDecisionSummary | None,
    latest_execution: ExecutionSummary | None,
    latest_analysis_shadow_source: AnalysisShadowSourceSummary | None,
    notes: tuple[str, ...],
    persisted_assessments,
    persisted_decisions,
    persisted_executions,
    state_changes,
    market_paths: tuple[Path, ...],
    warnings: tuple[str, ...],
) -> PositionMonitoringSnapshot:
    return PositionMonitoringSnapshot(
        target_key=target_key,
        generated_at=generated_at,
        current_portfolio_state=current_portfolio_snapshot,
        market_data_status=market_status,
        comparison_status=comparison_status,
        market_symbol=market_symbol,
        bar_granularity=bar_granularity,
        market_data_path=market_path,
        base_value=base_value,
        lines=lines,
        points=(),
        markers=(),
        latest_pm_decision=latest_pm_decision,
        latest_execution=latest_execution,
        latest_analysis_shadow_source=latest_analysis_shadow_source,
        notes=notes,
        audit=_build_audit(
            market_path=market_path,
            persisted_assessments=persisted_assessments,
            persisted_decisions=persisted_decisions,
            persisted_executions=persisted_executions,
            state_changes=state_changes,
            market_paths=market_paths,
            warnings=warnings,
        ),
    )


def _decision_summary(record) -> PMDecisionSummary:
    return PMDecisionSummary(
        decision_id=record.decision_id,
        business_at=record.business_at,
        requested_state=record.requested_state,
        requested_target_weight=record.requested_target_weight,
        execution_required=record.execution_required,
        pm_review_request_id=record.pm_review_request_id,
    )


def _execution_summary(record) -> ExecutionSummary:
    return ExecutionSummary(
        execution_record_id=record.execution_record_id,
        business_at=record.business_at,
        status=record.status,
        pm_decision_id=record.pm_decision_id,
        requested_target_weight=record.requested_target_weight,
        executed_at=record.executed_at,
        target_weight=record.target_weight,
    )


def _analysis_summary(record: AnalysisAssessment) -> AnalysisShadowSourceSummary:
    return AnalysisShadowSourceSummary(
        assessment_id=record.assessment_id,
        business_at=record.business_at,
        as_if_flat_state=record.as_if_flat_state,
        target_weight=_analysis_weight(record),
    )


def _eligible_analysis_shadow_assessments(
    *,
    persisted_assessments,
) -> tuple[AnalysisAssessment, ...]:
    return tuple(
        persisted.record
        for persisted in persisted_assessments
        if persisted.record.memory_write_receipt_ids
    )


def _parse_symbol_granularity_from_bar_name(file_name: str) -> tuple[str, str] | None:
    if not file_name.endswith(".jsonl"):
        return None
    stem = file_name[:-6]
    if "_" not in stem:
        return None
    symbol, granularity = stem.rsplit("_", 1)
    if not symbol or not granularity:
        return None
    return symbol, granularity


def _state_change_path_for(state_change) -> Path:
    month = state_change.effective_at.strftime("%Y-%m")
    return (
        Path("runtime")
        / "validation"
        / "state-changes"
        / state_change.target_key
        / f"{month}.jsonl"
    )


def _known_target_roots(layout: WorkspaceLayout) -> tuple[Path, ...]:
    runtime_root = layout.runtime_root
    return (
        runtime_root / "analysis_assessments",
        runtime_root / "portfolio" / "pm-decisions",
        runtime_root / "execution" / "records",
        runtime_root / "pm_review" / "requests",
        runtime_root / "validation" / "state-changes",
        runtime_root / "live_market_data" / "raw" / "rest_bars",
        runtime_root / "market_data",
        runtime_root / "portfolio" / "state",
    )


__all__ = [
    "PositionMonitoringWorkspaceError",
    "build_position_monitoring_snapshot",
    "list_position_monitoring_targets",
]
