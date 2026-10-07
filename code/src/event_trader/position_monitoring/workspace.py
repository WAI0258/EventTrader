"""Workspace-backed read models for position monitoring."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from event_trader.analysis_assessment_store import AnalysisAssessmentStore
from event_trader.contracts._validators import validate_target_key
from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    MarketMapping,
    ViewState,
    canonical_view_state_target_weight,
)
from event_trader.execution.store import ExecutionRecordStore, PersistedExecutionRecord
from event_trader.market.adjustments import (
    MarketAdjustmentSidecar,
    adjust_price_for_realized_policy,
    apply_adjustment_policy_to_series,
)
from event_trader.market.shared_store import (
    MarketSeriesIdentity,
    SharedMarketDataError,
    SharedMarketDataProvider,
    SharedMarketDataStore,
    shared_market_data_root,
)
from event_trader.portfolio.contracts import PMDecision
from event_trader.portfolio.store import PMDecisionStore, PortfolioStateStore
from event_trader.position_monitoring.contracts import (
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
from event_trader.validation.state_change_store import read_pm_execution_sidecar_state_changes


class PositionMonitoringWorkspaceError(ValueError):
    """Raised when position monitoring cannot be built from a workspace."""


def _mapping_from_market_spec(*, target_key: str, symbol: str, granularity: str) -> MarketMapping:
    return MarketMapping(
        target_key=target_key,
        market_symbol=symbol,
        market_session="continuous",
        exchange=None,
        bar_granularity=granularity,
        exchange_session_scope=None,
    )


def _mapping_from_execution_provenance(
    *,
    target_key: str,
    execution_records: tuple[PersistedExecutionRecord, ...],
) -> MarketMapping | None:
    for persisted in reversed(execution_records):
        provenance = persisted.record.provenance
        if provenance is not None and provenance.market_symbol and provenance.bar_granularity:
            return _mapping_from_market_spec(
                target_key=target_key,
                symbol=provenance.market_symbol,
                granularity=provenance.bar_granularity,
            )
    return None


@dataclass(frozen=True, slots=True)
class AnalysisDirectMappingEvent:
    assessment_id: str
    effective_at: datetime
    business_at: datetime
    state: ViewState
    target_weight: float
    price: float


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
    runtime_mode: str = "live",
    display_start_at: datetime | None = None,
    market_data_root: Path | None = None,
    market_data_snapshot_id: str | None = None,
    market_data_provider_name: str | None = None,
    market_mapping: MarketMapping | None = None,
) -> PositionMonitoringSnapshot:
    """Build one workspace-backed position monitoring snapshot."""

    if not isinstance(layout, WorkspaceLayout):
        raise PositionMonitoringWorkspaceError("layout must be a WorkspaceLayout instance.")
    normalized_target = validate_target_key(target_key, error_type=PositionMonitoringWorkspaceError)
    generated = generated_at or datetime.now(UTC)
    if display_start_at is not None:
        if display_start_at.tzinfo is None or display_start_at.utcoffset() is None:
            raise PositionMonitoringWorkspaceError(
                "display_start_at must be timezone-aware."
            )
        display_start_at = display_start_at.astimezone(UTC)

    persisted_assessments = AnalysisAssessmentStore(layout).read_records(
        target_key=normalized_target
    )
    persisted_decisions = PMDecisionStore(layout).read_records(target_key=normalized_target)
    persisted_executions = ExecutionRecordStore(layout).read_records(target_key=normalized_target)
    portfolio_state = PortfolioStateStore(layout).read(target_key=normalized_target)
    warnings: list[str] = []
    state_changes = read_pm_execution_sidecar_state_changes(layout, normalized_target)

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
    comparison_start_at = display_start_at
    if comparison_start_at is None and ordered_assessments:
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
        comparison_start_at=comparison_start_at,
        market_data_root=market_data_root,
        market_data_snapshot_id=market_data_snapshot_id,
        market_data_provider_name=market_data_provider_name,
        market_mapping=market_mapping,
    )

    current_portfolio_snapshot = _build_current_portfolio_state_snapshot(
        portfolio_state=portfolio_state,
        decisions_by_id=decisions_by_id,
        execution_record_ids=execution_record_ids,
    )

    notes: list[str] = []
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
        analysis_direct_line = PositionMonitoringLine(
            key="analysis_direct",
            label="Analysis Direct",
            role="analysis",
            status=PositionMonitoringStatus(
                code="unavailable_market_data",
                explanation=(
                    "Market bars are unavailable, so Analysis Direct cannot be charted."
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
            lines=(pm_line, buy_hold_line, analysis_direct_line),
            points=(),
            markers=(),
            latest_pm_decision=latest_pm_decision,
            latest_execution=latest_execution,
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
        notes=tuple(notes),
        persisted_decisions=persisted_decisions,
        persisted_executions=persisted_executions,
        state_changes=state_changes,
        market_paths=market_source_paths,
        comparison_start_at=comparison_start_at,
    )
    if unavailable_snapshot is not None:
        return unavailable_snapshot
    if comparison_market_data is None:
        raise PositionMonitoringWorkspaceError(
            "comparison market data must be available when no unavailable snapshot is returned."
        )

    # Adjustments are part of the shared series identity.  Position monitoring
    # must never rediscover a sidecar by walking a workspace-owned market-data
    # directory.
    realized_adjustment_sidecar = None
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
        display_start_at=display_start_at,
    )
    (
        analysis_direct_series,
        analysis_direct_line,
        analysis_direct_notes,
        analysis_direct_events,
    ) = _build_analysis_direct_series(
        market_data=canonical_comparison_market_data,
        persisted_assessments=ordered_assessments,
        base_value=base_value,
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
    notes.extend(analysis_direct_notes)
    if display_start_at is None:
        notes.append(
            "Pipeline vs Benchmarks starts at the first market bar on or after the earliest "
            "analysis assessment."
        )
    else:
        notes.append(
            "Pipeline vs Benchmarks is rebased at the first market bar on or after the "
            "configured display start."
        )
    warnings.extend(pm_warnings)

    last_bar_end = comparison_market_data.bars[-1].end_at
    comparison_start_at = comparison_start_at or comparison_market_data.bars[0].start_at
    if decisions and decisions[-1].business_at > last_bar_end:
        notes.append(
            "The latest PM decision occurs after the last available market bar and "
            "is not plotted yet."
        )
    if ordered_assessments and ordered_assessments[-1].record.business_at > last_bar_end:
        notes.append(
            "The latest analysis assessment occurs after the last available market "
            "bar and is not plotted yet."
        )

    comparison_status = _comparison_status(
        pm_line=pm_line,
        analysis_direct_line=analysis_direct_line,
        market_status=market_status,
    )
    merged_points = _merge_points(
        market_data=canonical_comparison_market_data,
        pm_series=pm_series,
        buy_hold_series=buy_hold_series,
        analysis_direct_series=analysis_direct_series,
    )
    markers = _build_markers(
        points=merged_points,
        comparison_start_at=comparison_start_at,
        executed_records=executed_records,
        decisions_by_id=decisions_by_id,
        analysis_direct_events=analysis_direct_events,
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
        lines=(pm_line, buy_hold_line, analysis_direct_line),
        points=merged_points,
        markers=markers,
        latest_pm_decision=latest_pm_decision,
        latest_execution=latest_execution,
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
    decisions_by_id: Mapping[str, PMDecision],
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


def _build_pm_pipeline_series(
    *,
    market_data: MarketDataSeries,
    executed_records: tuple[PersistedExecutionRecord, ...],
    decisions_by_id: Mapping[str, PMDecision],
    sidecars_by_execution_id: Mapping[str, object],
    portfolio_state,
    base_value: float,
    adjustment_sidecar: MarketAdjustmentSidecar | None,
    display_start_at: datetime | None,
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
    carried_event: TargetWeightEvent | None = None
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
        event = TargetWeightEvent(
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
        if display_start_at is not None and event.effective_at < display_start_at:
            if carried_event is None or event.effective_at > carried_event.effective_at:
                carried_event = event
            continue
        events.append(event)
    if carried_event is not None and not any(
        event.effective_at == market_data.bars[0].start_at for event in events
    ):
        events.insert(
            0,
            TargetWeightEvent(
                effective_at=market_data.bars[0].start_at,
                target_weight=carried_event.target_weight,
                state=carried_event.state,
            ),
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


def _build_analysis_direct_series(
    *,
    market_data: MarketDataSeries,
    persisted_assessments,
    base_value: float,
) -> tuple[
    tuple[StrategyLinePoint, ...],
    PositionMonitoringLine,
    tuple[str, ...],
    tuple[AnalysisDirectMappingEvent, ...],
]:
    events: list[TargetWeightEvent] = []
    marker_events: list[AnalysisDirectMappingEvent] = []
    previous_state: ViewState = "flat"
    previous_weight = 0.0
    skipped_after_last_bar = 0
    for persisted in persisted_assessments:
        assessment = persisted.record
        state = assessment.as_if_flat_state
        target_weight = canonical_view_state_target_weight(state)
        if state == previous_state and target_weight == previous_weight:
            continue
        selected_bar = _select_containing_bar(
            bars=market_data.bars,
            business_at=assessment.business_at,
        )
        effective_at = assessment.business_at if selected_bar is not None else None
        execution_price = (
            _analysis_reference_price_at_assessment(assessment)
            if selected_bar is not None
            else None
        )
        if selected_bar is None:
            selected_bar = _select_next_bar_on_or_after(
                bars=market_data.bars,
                business_at=assessment.business_at,
            )
            effective_at = selected_bar.start_at if selected_bar is not None else None
        if selected_bar is None:
            skipped_after_last_bar += 1
            continue
        if effective_at is None:
            raise PositionMonitoringWorkspaceError(
                "analysis mapping bar selection did not resolve an effective time."
            )
        mapping_price = selected_bar.open_price
        if execution_price is not None:
            mapping_price = execution_price
        events.append(
            TargetWeightEvent(
                effective_at=effective_at,
                target_weight=target_weight,
                state=state,
                execution_price=execution_price,
            )
        )
        marker_events.append(
            AnalysisDirectMappingEvent(
                assessment_id=assessment.assessment_id,
                effective_at=effective_at,
                business_at=assessment.business_at,
                state=state,
                target_weight=target_weight,
                price=mapping_price,
            )
        )
        previous_state = state
        previous_weight = target_weight
    notes: tuple[str, ...] = ()
    if skipped_after_last_bar:
        notes = (
            "Some Analysis assessments occur after the last available market bar and "
            "are not charted yet.",
        )
    return (
        calculate_cumulative_equity_series(
            market_data=market_data,
            events=tuple(events),
            base_value=base_value,
            default_state="flat",
        ),
        PositionMonitoringLine(
            key="analysis_direct",
            label="Analysis Direct",
            role="analysis",
            status=PositionMonitoringStatus(
                code="ready" if not skipped_after_last_bar else "partial_after_last_market_bar",
                explanation=(
                    "Analysis Direct maps every persisted AnalysisAssessment state through "
                    "canonical_view_state_target_weight on the first eligible market bar."
                    if not skipped_after_last_bar
                    else (
                        "Analysis Direct maps persisted AnalysisAssessment states through "
                        "canonical_view_state_target_weight; some newer assessments await "
                        "market bars before they can be charted."
                    )
                ),
            ),
        ),
        notes,
        tuple(marker_events),
    )


def _merge_points(
    *,
    market_data: MarketDataSeries,
    pm_series: tuple[StrategyLinePoint, ...] | None,
    buy_hold_series: tuple[StrategyLinePoint, ...],
    analysis_direct_series: tuple[StrategyLinePoint, ...],
) -> tuple[PositionMonitoringPoint, ...]:
    pm_by_time = {} if pm_series is None else {point.time: point for point in pm_series}
    buy_hold_by_time = {point.time: point for point in buy_hold_series}
    analysis_direct_by_time = {point.time: point for point in analysis_direct_series}
    bars_by_start = {bar.start_at: bar for bar in market_data.bars}
    bars_by_end = {bar.end_at: bar for bar in market_data.bars}
    points: list[PositionMonitoringPoint] = []
    for time in sorted(set(pm_by_time) | set(buy_hold_by_time) | set(analysis_direct_by_time)):
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
        analysis_direct_point = analysis_direct_by_time.get(time)
        points.append(
            PositionMonitoringPoint(
                time=time,
                price=price,
                pm_pipeline_value=None if pm_point is None else pm_point.value,
                buy_hold_value=None if buy_hold_point is None else buy_hold_point.value,
                analysis_direct_value=(
                    None if analysis_direct_point is None else analysis_direct_point.value
                ),
                pm_target_weight=None if pm_point is None else pm_point.target_weight,
                analysis_direct_target_weight=(
                    None
                    if analysis_direct_point is None
                    else analysis_direct_point.target_weight
                ),
                pm_state=None if pm_point is None else pm_point.state,
                analysis_direct_state=(
                    None if analysis_direct_point is None else analysis_direct_point.state
                ),
            )
        )
    return tuple(points)


def _build_markers(
    *,
    points: tuple[PositionMonitoringPoint, ...],
    comparison_start_at: datetime,
    executed_records: tuple[PersistedExecutionRecord, ...],
    decisions_by_id: Mapping[str, PMDecision],
    analysis_direct_events: tuple[AnalysisDirectMappingEvent, ...],
) -> tuple[PositionMonitoringMarker, ...]:
    if not points:
        return ()
    included_points = tuple(point for point in points if point.time >= points[0].time)
    markers: list[PositionMonitoringMarker] = []
    previous_weight: float | None = None
    previous_state: ViewState | None = None
    for persisted in executed_records:
        record = persisted.record
        if (
            record.executed_at is None
            or record.target_weight is None
            or record.adjusted_price is None
            or record.executed_at < comparison_start_at
        ):
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
        if plotted_point is None or (
            previous_state == state and previous_weight == record.target_weight
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
                    if previous_weight is None
                    else ("arrowDown" if record.target_weight < previous_weight else "arrowUp")
                ),
                position=(
                    "belowBar"
                    if previous_weight is None or record.target_weight >= previous_weight
                    else "aboveBar"
                ),
                text="PM",
                source_id=record.execution_record_id,
                pm_decision_id=record.pm_decision_id,
                execution_record_id=record.execution_record_id,
            )
        )
        previous_state = state
        previous_weight = record.target_weight
    previous_analysis_weight: float | None = None
    previous_analysis_state: ViewState | None = None
    for event in analysis_direct_events:
        plotted_point = _point_for_business_time(included_points, event.business_at)
        if plotted_point is None or (
            previous_analysis_state == event.state
            and previous_analysis_weight == event.target_weight
        ):
            continue
        markers.append(
            PositionMonitoringMarker(
                marker_id=f"analysis:{event.assessment_id}",
                kind="analysis_assessment",
                lane="analysis",
                label="Analysis Direct mapping",
                time=plotted_point.time,
                business_at=event.business_at,
                state=event.state,
                target_weight=event.target_weight,
                price=event.price,
                line_value=plotted_point.analysis_direct_value,
                shape="circle" if previous_analysis_weight is None else "square",
                position=(
                    "inBar"
                    if previous_analysis_weight is None
                    or event.target_weight >= previous_analysis_weight
                    else "aboveBar"
                ),
                text="AN",
                source_id=event.assessment_id,
                analysis_assessment_id=event.assessment_id,
            )
        )
        previous_analysis_state = event.state
        previous_analysis_weight = event.target_weight
    return tuple(
        sorted(markers, key=lambda marker: (marker.time, marker.business_at, marker.marker_id))
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


def _select_containing_bar(
    *,
    bars: tuple[MarketDataBar, ...],
    business_at: datetime,
) -> MarketDataBar | None:
    for bar in bars:
        if bar.start_at < business_at < bar.end_at:
            return bar
    return None


def _analysis_reference_price_at_assessment(assessment) -> float | None:
    semantics = getattr(assessment, "analysis_price_semantics", None)
    if semantics is None or semantics.analysis_reference_at != assessment.business_at:
        return None
    return semantics.analysis_reference_price


def _comparison_status(
    *,
    pm_line: PositionMonitoringLine,
    analysis_direct_line: PositionMonitoringLine,
    market_status: PositionMonitoringStatus,
) -> PositionMonitoringStatus:
    if market_status.code.startswith("partial"):
        return PositionMonitoringStatus(
            code="partial",
            explanation=market_status.explanation,
        )
    if pm_line.status.code.startswith("unavailable"):
        return PositionMonitoringStatus(
            code="partial",
            explanation=(
                "Market data is present, but the PM pipeline cannot be fully "
                "reconstructed from the available execution history."
            ),
        )
    if pm_line.status.code.startswith("partial"):
        return PositionMonitoringStatus(
            code="partial",
            explanation=(
                "The PM pipeline is charted, but execution linkage is incomplete."
            ),
        )
    if analysis_direct_line.status.code.startswith("partial"):
        return PositionMonitoringStatus(
            code="partial",
            explanation=(
                "PM pipeline and Analysis Direct are available, but newer Analysis "
                "assessments await market bars before they can be charted."
            ),
        )
    return PositionMonitoringStatus(
        code="ready",
        explanation=(
            "PM pipeline, Buy & Hold, and Thesis-anchored Analysis Direct are available "
            "from workspace truth."
        ),
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
    comparison_start_at: datetime | None,
    market_data_root: Path | None = None,
    market_data_snapshot_id: str | None = None,
    market_data_provider_name: str | None = None,
    market_mapping: MarketMapping | None = None,
) -> tuple[
    MarketDataSeries | None,
    str | None,
    str | None,
    Path | None,
    tuple[Path, ...],
    PositionMonitoringStatus,
]:
    mapping = market_mapping or _mapping_from_execution_provenance(
        target_key=target_key,
        execution_records=execution_records,
    )
    if mapping is None:
        return (
            None,
            None,
            None,
            None,
            (),
            PositionMonitoringStatus(
                code="missing_market_spec",
                explanation=(
                    "Market mapping is required explicitly or from execution provenance; "
                    "workspace market-data files are not consulted."
                ),
            ),
        )
    resolved_market_data_root = (
        market_data_root if market_data_root is not None else shared_market_data_root()
    )
    try:
        provider_name = market_data_provider_name or "unknown"
        if runtime_mode == "replay" and market_data_snapshot_id is None:
            raise SharedMarketDataError(
                "replay position monitoring requires a pinned market-data snapshot."
            )
        store = SharedMarketDataStore(resolved_market_data_root)
        start_at = comparison_start_at or datetime.min.replace(tzinfo=UTC)
        end_at = datetime.now(UTC)
        if runtime_mode == "replay":
            provider = SharedMarketDataProvider(
                store=store,
                provider_name=provider_name,
                snapshot_id=market_data_snapshot_id,
            )
            market_data = provider.read_series(mapping, start_at=start_at, end_at=end_at)
            market_status = PositionMonitoringStatus(
                code="ready",
                explanation="Market bars were loaded from the pinned shared market-data snapshot.",
            )
        else:
            identity = MarketSeriesIdentity.from_mapping(mapping, provider=provider_name)
            observed = store.read_observed_prefix(
                identity,
                start_at=start_at,
                end_at=end_at,
            )
            if observed.series is None:
                raise SharedMarketDataError(
                    "no observed market bars are available on or after the comparison start."
                )
            market_data = observed.series
            if observed.complete_to_requested_end:
                market_status = PositionMonitoringStatus(
                    code="ready",
                    explanation=(
                        "Observed shared market data covers the requested completed window "
                        f"through {observed.requested_completed_end_at.isoformat()}."
                    ),
                )
            else:
                last_available = (
                    "none"
                    if observed.last_verified_available_end_at is None
                    else observed.last_verified_available_end_at.isoformat()
                )
                assert observed.first_missing_interval is not None
                first_missing_start, first_missing_end = observed.first_missing_interval
                market_status = PositionMonitoringStatus(
                    code="partial_stale_market_data",
                    explanation=(
                        "Observed shared market data is partial/stale: last available verified "
                        f"end is {last_available}; first missing interval is "
                        f"{first_missing_start.isoformat()}..{first_missing_end.isoformat()}."
                    ),
                )
        source = store.database_path
        return (
            market_data,
            mapping.market_symbol,
            mapping.bar_granularity,
            source,
            (source,),
            market_status,
        )
    except (SharedMarketDataError, ValueError) as exc:
        return (
            None,
            mapping.market_symbol,
            mapping.bar_granularity,
            resolved_market_data_root,
            (resolved_market_data_root,),
            PositionMonitoringStatus(
                code="missing_market_bars",
                explanation=str(exc),
            ),
        )


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
    comparison_start_at: datetime | None,
    current_portfolio_snapshot: CurrentPortfolioStateSnapshot,
    latest_pm_decision,
    latest_execution,
    notes: tuple[str, ...],
    persisted_decisions,
    persisted_executions,
    state_changes,
    market_paths: tuple[Path, ...],
) -> tuple[MarketDataSeries, None] | tuple[None, PositionMonitoringSnapshot]:
    start_at = comparison_start_at
    if start_at is None and ordered_assessments:
        start_at = ordered_assessments[0].record.business_at
    if not ordered_assessments:
        code = "unavailable_no_analysis_window"
        explanation = "Position comparison starts after the first analysis assessment."
    else:
        code = "unavailable_no_market_bars_after_analysis_start"
        explanation = "No market bars are available on or after the comparison start."
    if not ordered_assessments or start_at is None or not any(
        bar.start_at >= start_at for bar in market_data.bars
    ):
        return None, _build_unavailable_comparison_snapshot(
            target_key=target_key,
            generated_at=generated_at,
            current_portfolio_snapshot=current_portfolio_snapshot,
            market_status=market_status,
            comparison_status=PositionMonitoringStatus(code=code, explanation=explanation),
            market_symbol=market_symbol,
            bar_granularity=bar_granularity,
            market_path=market_path,
            base_value=base_value,
            lines=(
                PositionMonitoringLine(
                    key="pm_pipeline",
                    label="PM pipeline",
                    role="actual",
                    status=PositionMonitoringStatus(code=code, explanation=explanation),
                ),
                PositionMonitoringLine(
                    key="buy_hold",
                    label="Buy & Hold",
                    role="baseline",
                    status=PositionMonitoringStatus(code=code, explanation=explanation),
                ),
                PositionMonitoringLine(
                    key="analysis_direct",
                    label="Analysis Direct",
                    role="analysis",
                    status=PositionMonitoringStatus(code=code, explanation=explanation),
                ),
            ),
            latest_pm_decision=latest_pm_decision,
            latest_execution=latest_execution,
            notes=notes,
            persisted_assessments=ordered_assessments,
            persisted_decisions=persisted_decisions,
            persisted_executions=persisted_executions,
            state_changes=state_changes,
            market_paths=market_paths,
            warnings=(),
        )
    assert start_at is not None
    return MarketDataSeries(
        bars=tuple(bar for bar in market_data.bars if bar.start_at >= start_at)
    ), None


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
    latest_pm_decision,
    latest_execution,
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


def _decision_summary(record):
    return PMDecisionSummary(
        decision_id=record.decision_id,
        business_at=record.business_at,
        requested_state=record.requested_state,
        requested_target_weight=record.requested_target_weight,
        execution_required=record.execution_required,
        pm_review_request_id=record.pm_review_request_id,
    )


def _execution_summary(record):
    return ExecutionSummary(
        execution_record_id=record.execution_record_id,
        business_at=record.business_at,
        status=record.status,
        pm_decision_id=record.pm_decision_id,
        requested_target_weight=record.requested_target_weight,
        executed_at=record.executed_at,
        target_weight=record.target_weight,
    )


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
        runtime_root / "portfolio" / "state",
    )


__all__ = [
    "PositionMonitoringWorkspaceError",
    "build_position_monitoring_snapshot",
    "list_position_monitoring_targets",
]
