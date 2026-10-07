"""Read-only position-lifecycle episode projections for the PM Console."""

from __future__ import annotations

from datetime import UTC, datetime
from math import prod

from event_trader.episode_memory import FileBackedEpisodeMemoryStore
from event_trader.execution.store import ExecutionRecordStore
from event_trader.pm_console.schemas import (
    PMConsoleEpisodeDetailDTO,
    PMConsoleEpisodeDetailResponseDTO,
    PMConsoleEpisodeReflectionDTO,
    PMConsoleEpisodeSegmentDTO,
    PMConsoleEpisodesResponseDTO,
    PMConsoleEpisodeSummaryDTO,
    PMConsoleEpisodeTransitionDTO,
    PMConsoleStatusDTO,
)
from event_trader.portfolio.store import PMDecisionStore
from event_trader.reflection.trigger_store import FileBackedReflectionObligationStore
from event_trader.storage import WorkspaceLayout
from event_trader.validation.episode_artifacts import (
    EpisodeArtifactStoreError,
    read_closed_episode_artifact,
    read_target_episode_artifacts,
)
from event_trader.validation.returns import calculate_mark_return

EPISODE_PHASES = frozenset({"open", "completed", "all"})


def list_episode_targets(layout: WorkspaceLayout) -> tuple[str, ...]:
    root = layout.runtime_root / "validation" / "episodes"
    if not root.is_dir():
        return ()
    return tuple(sorted(path.name for path in root.iterdir() if path.is_dir()))


def build_episodes_response(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    offset: int = 0,
    limit: int = 50,
    phase: str | None = None,
    query: str | None = None,
    anchor_episode_id: str | None = None,
) -> PMConsoleEpisodesResponseDTO:
    _validate_paging(offset, limit)
    normalized_phase = phase or "all"
    if normalized_phase not in EPISODE_PHASES:
        raise ValueError(f"unknown episode phase: {normalized_phase}")
    state = _read_episode_state(layout, target_key)
    records = _episode_records(state, phase=normalized_phase, query=query)
    resolved_offset = _anchored_offset(
        records,
        offset=offset,
        limit=limit,
        anchor_episode_id=anchor_episode_id,
    )
    page = records[resolved_offset : resolved_offset + limit]
    return PMConsoleEpisodesResponseDTO(
        generated_at=datetime.now(UTC).isoformat(),
        target_key=target_key,
        total=len(records),
        offset=resolved_offset,
        limit=limit,
        episodes=tuple(
            _summary(layout, target_key, episode, segments) for episode, segments in page
        ),
    )


def build_episode_detail_response(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    episode_id: str,
) -> PMConsoleEpisodeDetailResponseDTO | None:
    state = _read_episode_state(layout, target_key)
    for episode, segments in _episode_records(state, phase="all", query=None):
        if episode.episode_id != episode_id:
            continue
        reflection = _reflection(layout, target_key, episode.episode_id)
        detail = PMConsoleEpisodeDetailDTO(
            summary=_summary(layout, target_key, episode, segments, reflection=reflection),
            segments=tuple(_segment_dto(layout, target_key, segment) for segment in segments),
            transitions=_transitions(layout, target_key, episode),
            reflection=reflection,
        )
        return PMConsoleEpisodeDetailResponseDTO(
            generated_at=datetime.now(UTC).isoformat(),
            target_key=target_key,
            episode=detail,
        )
    return None


def _read_episode_state(layout: WorkspaceLayout, target_key: str):
    try:
        return read_target_episode_artifacts(layout, target_key)
    except EpisodeArtifactStoreError as exc:
        raise ValueError(str(exc)) from exc


def _episode_records(state, *, phase: str, query: str | None):
    records: list[tuple[object, tuple[object, ...]]] = []
    if phase in {"all", "completed"}:
        for episode in state.completed_episodes:
            records.append(
                (
                    episode,
                    tuple(
                        segment
                        for segment in state.completed_segments
                        if segment.episode_id == episode.episode_id
                    ),
                )
            )
    if phase in {"all", "open"} and state.open_episode is not None:
        segments = tuple(
            segment
            for segment in state.completed_segments
            if segment.episode_id == state.open_episode.episode_id
        )
        if state.open_segment is not None:
            segments = (*segments, state.open_segment)
        records.append((state.open_episode, segments))
    needle = query.strip().lower() if query else ""
    if needle:
        records = [
            item
            for item in records
            if needle
            in " ".join((item[0].episode_id, item[0].direction, item[0].close_reason or "")).lower()
        ]
    return tuple(sorted(records, key=lambda item: item[0].opened_at, reverse=True))


def _anchored_offset(records, *, offset: int, limit: int, anchor_episode_id: str | None) -> int:
    """Keep a deep-linked position episode on its archive page when available."""
    if not anchor_episode_id:
        return offset
    for index, (episode, _) in enumerate(records):
        if episode.episode_id == anchor_episode_id:
            return (index // limit) * limit
    return offset


def _summary(layout: WorkspaceLayout, target_key: str, episode, segments, *, reflection=None):
    outcome_status, strategy_return = _episode_outcome(layout, target_key, episode, segments)
    reflection = reflection or _reflection(layout, target_key, episode.episode_id)
    return PMConsoleEpisodeSummaryDTO(
        episode_id=episode.episode_id,
        target_key=target_key,
        phase="completed" if episode.closed_at is not None else "open",
        direction=episode.direction,
        opened_at=episode.opened_at.isoformat(),
        closed_at=None if episode.closed_at is None else episode.closed_at.isoformat(),
        close_reason=episode.close_reason,
        segment_count=len(segments),
        outcome_status=outcome_status,
        strategy_return=strategy_return,
        reflection_status=reflection.status,
    )


def _segment_dto(layout: WorkspaceLayout, target_key: str, segment):
    status, value = _segment_outcome(layout, target_key, segment)
    return PMConsoleEpisodeSegmentDTO(
        segment_id=segment.segment_id,
        target_weight=segment.target_weight,
        opened_at=segment.opened_at.isoformat(),
        closed_at=None if segment.closed_at is None else segment.closed_at.isoformat(),
        close_reason=segment.close_reason,
        strategy_return=value,
        outcome_status=status,
    )


def _episode_outcome(layout: WorkspaceLayout, target_key: str, episode, segments):
    if episode.closed_at is None:
        return _status("open_position", "This position episode is still active."), None
    outcomes = [_segment_outcome(layout, target_key, segment) for segment in segments]
    if not outcomes or any(value is None for _, value in outcomes):
        return _status(
            "missing_execution_price",
            "Outcome requires persisted adjusted execution prices for every segment.",
        ), None
    return _status(
        "available", "Compounded from persisted execution-adjusted segment returns."
    ), prod(1.0 + value for _, value in outcomes if value is not None) - 1.0


def _segment_outcome(layout: WorkspaceLayout, target_key: str, segment):
    if segment.closed_at is None or segment.closed_by_state_change_id is None:
        return _status("open_segment", "This segment is active and has no realized outcome."), None
    state_changes = _state_changes_for_episode(layout, target_key, segment.episode_id)
    changes = {change.state_change_id: change for change in state_changes}
    entry_change = changes.get(segment.opened_by_state_change_id)
    exit_change = changes.get(segment.closed_by_state_change_id)
    if entry_change is None or exit_change is None:
        return _status(
            "missing_state_change", "The persisted episode snapshot is missing a segment boundary."
        ), None
    executions = _executions_by_id(layout, target_key)
    entry = executions.get(entry_change.execution_record_id)
    exit_record = executions.get(exit_change.execution_record_id)
    entry_price = None if entry is None else entry.adjusted_price
    exit_price = None if exit_record is None else exit_record.adjusted_price
    if entry_price is None or exit_price is None:
        return _status(
            "missing_execution_price",
            "A segment boundary lacks a persisted adjusted execution price.",
        ), None
    return _status(
        "available", "Calculated from persisted adjusted execution prices."
    ), calculate_mark_return(
        entry_price=entry_price,
        mark_price=exit_price,
        target_weight=segment.target_weight,
    ).strategy_return


def _transitions(layout: WorkspaceLayout, target_key: str, episode):
    if episode.closed_at is None:
        return ()
    state_changes = _state_changes_for_episode(layout, target_key, episode.episode_id)
    decisions = {
        item.record.decision_id: item.record
        for item in PMDecisionStore(layout).read_records(target_key=target_key)
    }
    executions = _executions_by_id(layout, target_key)
    return tuple(
        PMConsoleEpisodeTransitionDTO(
            state_change_id=change.state_change_id,
            occurred_at=change.effective_at.isoformat(),
            kind=(
                "entry"
                if change.state_change_id == episode.opened_by_state_change_id
                else "exit"
                if change.state_change_id == episode.closed_by_state_change_id
                else "resize"
            ),
            state=change.state,
            previous_target_weight=(
                None
                if change.pm_decision_id not in decisions
                else decisions[change.pm_decision_id].actual_target_weight_before_decision
            ),
            target_weight=change.target_weight,
            pm_decision_id=change.pm_decision_id,
            execution_record_id=change.execution_record_id,
            adjusted_price=(
                None
                if change.execution_record_id not in executions
                else executions[change.execution_record_id].adjusted_price
            ),
            raw_price=(
                None
                if change.execution_record_id not in executions
                else executions[change.execution_record_id].raw_price
            ),
            total_cost_bps=(
                None
                if change.execution_record_id not in executions
                else executions[change.execution_record_id].total_cost_bps
            ),
            price_basis=(
                None
                if change.execution_record_id not in executions
                else executions[change.execution_record_id].price_basis
            ),
            rationale_md=(
                decisions[change.pm_decision_id].rationale_md
                if change.pm_decision_id in decisions
                else change.rationale_md
            ),
        )
        for change in state_changes
    )


def _state_changes_for_episode(layout: WorkspaceLayout, target_key: str, episode_id: str):
    return read_closed_episode_artifact(layout, target_key, episode_id).state_changes


def _executions_by_id(layout: WorkspaceLayout, target_key: str):
    return {
        item.record.execution_record_id: item.record
        for item in ExecutionRecordStore(layout).read_records(target_key=target_key)
    }


def _reflection(
    layout: WorkspaceLayout, target_key: str, episode_id: str
) -> PMConsoleEpisodeReflectionDTO:
    obligations = tuple(
        item.record
        for item in FileBackedReflectionObligationStore(layout).read_obligations(
            target_key=target_key
        )
        if item.record.episode_id == episode_id
    )
    memory = FileBackedEpisodeMemoryStore(layout)
    learning_recorded = bool(
        memory.read_deltas(target_key=target_key, episode_id=episode_id)
        or memory.read_no_updates(target_key=target_key, episode_id=episode_id)
    )
    if learning_recorded:
        status = _status("recorded", "A persisted episode-learning record is available.")
    elif any(item.status == "pending" for item in obligations):
        status = _status(
            "pending", "Reflection is due but no episode-learning record is available."
        )
    elif obligations:
        status = _status(
            "closed_without_learning",
            "Reflection obligations resolved without an episode-learning record.",
        )
    else:
        status = _status(
            "not_produced",
            "No persisted reflection obligation or episode-learning record is available.",
        )
    return PMConsoleEpisodeReflectionDTO(
        status=status, obligation_count=len(obligations), learning_recorded=learning_recorded
    )


def _status(code: str, explanation: str) -> PMConsoleStatusDTO:
    return PMConsoleStatusDTO(code=code, explanation=explanation)


def _validate_paging(offset: int, limit: int) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")


__all__ = [
    "EPISODE_PHASES",
    "build_episode_detail_response",
    "build_episodes_response",
    "list_episode_targets",
]
