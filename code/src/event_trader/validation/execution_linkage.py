"""Execution lookup helpers for PM-sourced validation state changes."""

from __future__ import annotations

from collections.abc import Sequence

from event_trader.contracts.view_state_change import ViewStateChange
from event_trader.execution.contracts import ExecutionRecord


def execution_lookup_ids_for_state_changes(
    state_changes: Sequence[ViewStateChange],
) -> tuple[str, ...]:
    """Return PM sidecar execution record ids in stable order."""

    ids: list[str] = []
    seen: set[str] = set()
    for state_change in state_changes:
        value = state_change.execution_record_id
        if value is None or value in seen:
            continue
        ids.append(value)
        seen.add(value)
    return tuple(ids)


def execution_record_by_state_change(
    *,
    target_key: str,
    state_changes: Sequence[ViewStateChange],
    execution_records: Sequence[ExecutionRecord],
) -> dict[str, ExecutionRecord]:
    """Return latest/linked execution record keyed by state_change_id."""

    sorted_records = tuple(
        sorted(
            execution_records,
            key=lambda item: (item.business_at, item.execution_record_id),
        )
    )
    records_by_id = {record.execution_record_id: record for record in sorted_records}

    linked: dict[str, ExecutionRecord] = {}
    for state_change in state_changes:
        if state_change.target_key != target_key:
            continue
        record = _pm_sidecar_execution_record(
            state_change=state_change,
            records=sorted_records,
            records_by_id=records_by_id,
        )
        if record is not None:
            linked[state_change.state_change_id] = record
    return linked


def _pm_sidecar_execution_record(
    *,
    state_change: ViewStateChange,
    records: Sequence[ExecutionRecord],
    records_by_id: dict[str, ExecutionRecord],
) -> ExecutionRecord | None:
    record = records_by_id.get(state_change.execution_record_id)
    if record is not None and _matches_pm_sidecar_lineage(
        record=record,
        state_change=state_change,
    ):
        return record
    for candidate in reversed(records):
        if _matches_pm_sidecar_lineage(record=candidate, state_change=state_change):
            return candidate
    return None


def _matches_pm_sidecar_lineage(
    *,
    record: ExecutionRecord,
    state_change: ViewStateChange,
) -> bool:
    return (
        record.target_key == state_change.target_key
        and record.pm_decision_id == state_change.pm_decision_id
        and record.execution_record_id == state_change.execution_record_id
    )


__all__ = [
    "execution_lookup_ids_for_state_changes",
    "execution_record_by_state_change",
]
