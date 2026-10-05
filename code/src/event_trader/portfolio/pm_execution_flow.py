"""Deterministic PM decision execution flow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from event_trader.contracts.view_state_change import (
    ViewConviction,
    ViewDirection,
    ViewState,
    ViewStateChange,
)
from event_trader.execution.contracts import ExecutionIntent, ExecutionRecord
from event_trader.execution.engine import PaperExecutionEngine
from event_trader.execution.store import (
    ExecutionIntentStore,
    ExecutionRecordStore,
    ExecutionStoreError,
)
from event_trader.portfolio.contracts import PMDecision, PortfolioState
from event_trader.portfolio.store import (
    PortfolioStateStore,
    PortfolioStoreError,
)
from event_trader.storage import WorkspaceLayout
from event_trader.validation.state_change_store import (
    StateChangeStoreError,
    append_state_change_once,
)
from event_trader.validation.episode_artifacts import (
    EpisodeArtifactStoreError,
    refresh_episode_artifacts,
)


class PMExecutionFlowError(ValueError):
    """Raised when the new PM execution flow cannot be deterministically applied."""


@dataclass(frozen=True, slots=True)
class PMExecutionFlowResult:
    pm_decision: PMDecision | None
    execution_intent: ExecutionIntent | None
    execution_record: ExecutionRecord | None
    portfolio_state: PortfolioState | None
    view_state_change: ViewStateChange | None


def execute_pm_decision(
    *,
    layout: WorkspaceLayout,
    decision: PMDecision,
    execution_engine: PaperExecutionEngine,
) -> PMExecutionFlowResult:
    """Execute one persisted PMDecision on the active decision-first hot path."""

    _validate_decision_flow_inputs(
        layout=layout,
        decision=decision,
        execution_engine=execution_engine,
    )
    intent = execution_engine.build_intent(
        decision=decision,
    )
    record = execution_engine.execute(intent=intent)
    try:
        ExecutionIntentStore(layout).append(intent)
        ExecutionRecordStore(layout).append(record)
    except ExecutionStoreError as exc:
        raise PMExecutionFlowError(str(exc)) from exc

    if record.status != "executed":
        return PMExecutionFlowResult(
            pm_decision=decision,
            execution_intent=intent,
            execution_record=record,
            portfolio_state=None,
            view_state_change=None,
        )

    try:
        state = PortfolioStateStore(layout).apply_execution_record(
            decision=decision,
            execution_record=record,
        )
    except PortfolioStoreError as exc:
        raise PMExecutionFlowError(str(exc)) from exc

    state_change = materialize_view_state_change_from_execution(
        decision=decision,
        execution_record=record,
    )
    try:
        append_state_change_once(layout, state_change)
    except StateChangeStoreError as exc:
        raise PMExecutionFlowError(str(exc)) from exc
    try:
        refresh_episode_artifacts(layout, state_change.target_key)
    except EpisodeArtifactStoreError as exc:
        raise PMExecutionFlowError(str(exc)) from exc

    return PMExecutionFlowResult(
        pm_decision=decision,
        execution_intent=intent,
        execution_record=record,
        portfolio_state=state,
        view_state_change=state_change,
    )


def materialize_view_state_change_from_execution(
    *,
    decision: PMDecision,
    execution_record: ExecutionRecord,
) -> ViewStateChange:
    """Build the validation sidecar after PM execution has completed."""

    if execution_record.status != "executed" or execution_record.executed_at is None:
        raise PMExecutionFlowError("ViewStateChange requires an executed record.")
    if execution_record.target_weight != decision.requested_target_weight:
        raise PMExecutionFlowError(
            "execution target_weight must match PMDecision requested_target_weight."
        )
    direction, conviction = _state_direction_conviction(decision.requested_state)
    rationale_lines = [
        "PM execution validation sidecar.",
        "",
        f"pm_decision_id: {decision.decision_id}",
        f"execution_record_id: {execution_record.execution_record_id}",
    ]
    return ViewStateChange(
        state_change_id=_derive_pm_execution_state_change_id(
            decision=decision,
            execution_record=execution_record,
        ),
        target_key=decision.target_key,
        state=decision.requested_state,
        direction=direction,
        conviction=conviction,
        target_weight=decision.requested_target_weight,
        effective_at=execution_record.executed_at,
        source_event_ids=decision.source_event_ids,
        rationale_md="\n".join(rationale_lines),
        used_lesson_ids=(),
        source_kind="pm_execution_sidecar",
        pm_decision_id=decision.decision_id,
        execution_record_id=execution_record.execution_record_id,
    )


def _validate_decision_flow_inputs(
    *,
    layout: WorkspaceLayout,
    decision: PMDecision,
    execution_engine: PaperExecutionEngine,
) -> None:
    if not isinstance(layout, WorkspaceLayout):
        raise PMExecutionFlowError("layout must be a WorkspaceLayout instance.")
    if not isinstance(decision, PMDecision):
        raise PMExecutionFlowError("decision must be a PMDecision instance.")
    if not isinstance(execution_engine, PaperExecutionEngine):
        raise PMExecutionFlowError(
            "execution_engine must be a PaperExecutionEngine instance."
        )


def _state_direction_conviction(state: ViewState) -> tuple[ViewDirection, ViewConviction]:
    if state == "flat":
        return "flat", "none"
    if state == "weak_long":
        return "long", "weak"
    if state == "strong_long":
        return "long", "strong"
    if state == "weak_short":
        return "short", "weak"
    if state == "strong_short":
        return "short", "strong"
    raise PMExecutionFlowError("approved state is not a valid ViewState.")


def _derive_pm_execution_state_change_id(
    *,
    decision: PMDecision,
    execution_record: ExecutionRecord,
) -> str:
    payload = {
        "source": "pm_execution_sidecar",
        "pm_decision_id": decision.decision_id,
        "execution_record_id": execution_record.execution_record_id,
        "target_key": decision.target_key,
        "state": decision.requested_state,
        "target_weight": decision.requested_target_weight,
        "effective_at": (
            None
            if execution_record.executed_at is None
            else execution_record.executed_at.isoformat()
        ),
    }
    digest = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]
    return f"state-change:pm-execution:{digest}"


__all__ = [
    "PMExecutionFlowError",
    "PMExecutionFlowResult",
    "execute_pm_decision",
    "materialize_view_state_change_from_execution",
]
