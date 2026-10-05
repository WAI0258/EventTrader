"""Map position monitoring read models into PM Console DTOs."""

from __future__ import annotations

from datetime import UTC, datetime

from event_trader.pm_console.schemas import (
    PMConsoleAnalysisShadowSourceDTO,
    PMConsoleAuditDTO,
    PMConsoleCurrentPortfolioStateDTO,
    PMConsoleDecisionDTO,
    PMConsoleExecutionDTO,
    PMConsoleLineDTO,
    PMConsoleMarkerDTO,
    PMConsolePointDTO,
    PMConsolePositionResponseDTO,
    PMConsoleStatusDTO,
    PMConsoleTargetDTO,
    PMConsoleTargetsResponseDTO,
    _isoformat,
)
from event_trader.pm_console.thesis import map_pm_console_target_views
from event_trader.position_monitoring import (
    PositionMonitoringSnapshot,
)
from event_trader.storage import WorkspaceLayout


def build_targets_response(layout: WorkspaceLayout) -> PMConsoleTargetsResponseDTO:
    """Build the PM Console targets listing."""

    generated_at = datetime.now(UTC).isoformat()
    target_views = map_pm_console_target_views(layout)
    return PMConsoleTargetsResponseDTO(
        generated_at=generated_at,
        targets=tuple(
            PMConsoleTargetDTO(
                target_key=target_key,
                implemented_views=implemented_views,
                default_view="workbench",
            )
            for target_key, implemented_views in target_views.items()
        ),
    )


def build_position_response(snapshot: PositionMonitoringSnapshot) -> PMConsolePositionResponseDTO:
    """Map a position monitoring snapshot into the PM Console position DTO."""

    return PMConsolePositionResponseDTO(
        target_key=snapshot.target_key,
        generated_at=snapshot.generated_at.isoformat(),
        current_portfolio_state=PMConsoleCurrentPortfolioStateDTO(
            status=_status(snapshot.current_portfolio_state.status),
            state=snapshot.current_portfolio_state.state,
            target_weight=snapshot.current_portfolio_state.target_weight,
            updated_at=_isoformat(snapshot.current_portfolio_state.updated_at),
            source_pm_decision_id=snapshot.current_portfolio_state.source_pm_decision_id,
            source_execution_record_id=snapshot.current_portfolio_state.source_execution_record_id,
            decision_episode_id=snapshot.current_portfolio_state.decision_episode_id,
        ),
        market_data_status=_status(snapshot.market_data_status),
        comparison_status=_status(snapshot.comparison_status),
        market_symbol=snapshot.market_symbol,
        price_display_decimals=_price_display_decimals_for_market_symbol(snapshot.market_symbol),
        bar_granularity=snapshot.bar_granularity,
        market_data_path=(
            None if snapshot.market_data_path is None else str(snapshot.market_data_path)
        ),
        base_value=snapshot.base_value,
        lines=tuple(
            PMConsoleLineDTO(
                key=line.key,
                label=line.label,
                role=line.role,
                status=_status(line.status),
            )
            for line in snapshot.lines
        ),
        points=tuple(
            PMConsolePointDTO(
                time=point.time.isoformat(),
                price=point.price,
                pm_pipeline_value=point.pm_pipeline_value,
                buy_hold_value=point.buy_hold_value,
                analysis_direct_shadow_value=point.analysis_direct_shadow_value,
                pm_target_weight=point.pm_target_weight,
                analysis_shadow_target_weight=point.analysis_shadow_target_weight,
                pm_state=point.pm_state,
                analysis_shadow_state=point.analysis_shadow_state,
            )
            for point in snapshot.points
        ),
        markers=tuple(
            PMConsoleMarkerDTO(
                marker_id=marker.marker_id,
                kind=marker.kind,
                lane=marker.lane,
                label=marker.label,
                time=marker.time.isoformat(),
                business_at=marker.business_at.isoformat(),
                state=marker.state,
                target_weight=marker.target_weight,
                price=marker.price,
                line_value=marker.line_value,
                shape=marker.shape,
                position=marker.position,
                text=marker.text,
                source_id=marker.source_id,
            )
            for marker in snapshot.markers
        ),
        latest_pm_decision=(
            None
            if snapshot.latest_pm_decision is None
            else PMConsoleDecisionDTO(
                decision_id=snapshot.latest_pm_decision.decision_id,
                business_at=snapshot.latest_pm_decision.business_at.isoformat(),
                requested_state=snapshot.latest_pm_decision.requested_state,
                requested_target_weight=snapshot.latest_pm_decision.requested_target_weight,
                execution_required=snapshot.latest_pm_decision.execution_required,
                pm_review_request_id=snapshot.latest_pm_decision.pm_review_request_id,
            )
        ),
        latest_execution=(
            None
            if snapshot.latest_execution is None
            else PMConsoleExecutionDTO(
                execution_record_id=snapshot.latest_execution.execution_record_id,
                business_at=snapshot.latest_execution.business_at.isoformat(),
                status=snapshot.latest_execution.status,
                pm_decision_id=snapshot.latest_execution.pm_decision_id,
                requested_target_weight=snapshot.latest_execution.requested_target_weight,
                executed_at=_isoformat(snapshot.latest_execution.executed_at),
                target_weight=snapshot.latest_execution.target_weight,
            )
        ),
        latest_analysis_shadow_source=(
            None
            if snapshot.latest_analysis_shadow_source is None
            else PMConsoleAnalysisShadowSourceDTO(
                assessment_id=snapshot.latest_analysis_shadow_source.assessment_id,
                business_at=snapshot.latest_analysis_shadow_source.business_at.isoformat(),
                as_if_flat_state=snapshot.latest_analysis_shadow_source.as_if_flat_state,
                target_weight=snapshot.latest_analysis_shadow_source.target_weight,
            )
        ),
        notes=snapshot.notes,
        audit=(
            None
            if snapshot.audit is None
            else PMConsoleAuditDTO(
                source_paths=tuple(str(path) for path in snapshot.audit.source_paths),
                counts=snapshot.audit.counts,
                warnings=snapshot.audit.warnings,
            )
        ),
    )


def _status(status) -> PMConsoleStatusDTO:
    return PMConsoleStatusDTO(code=status.code, explanation=status.explanation)


def _price_display_decimals_for_market_symbol(market_symbol: str | None) -> int:
    if market_symbol == "SZ.159516":
        return 3
    return 2


__all__ = [
    "build_position_response",
    "build_targets_response",
]
