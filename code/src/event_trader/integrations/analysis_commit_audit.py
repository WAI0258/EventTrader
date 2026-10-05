"""Append-only analysis commit audit-state helpers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from event_trader.contracts._validators import validate_target_key
from event_trader.storage import WorkspaceLayout

ANALYSIS_COMMIT_DIR_NAME = "analysis_commits"

AnalysisCommitStatus = Literal["complete", "failed", "incomplete"]
AnalysisCommitCompletionState = Literal[
    "complete",
    "incomplete_before_context_packet_persistence",
    "incomplete_after_context_packet_persistence",
    "incomplete_after_memory_writes",
    "incomplete_after_analysis_assessment",
    "incomplete_after_thesis_revision",
    "incomplete_after_outcome",
]
ThesisRevisionObligationStatus = Literal[
    "not_required",
    "required_unpersisted",
    "satisfied",
]


class AnalysisCommitAuditError(ValueError):
    """Raised when append-only analysis commit audit state cannot be read."""


@dataclass(frozen=True, slots=True)
class AnalysisCommitProgress:
    context_packet_path: Path | None = None
    research_memory_writes_committed: bool = False
    research_memory_write_receipt_ids: tuple[str, ...] = ()
    thesis_revision_required: bool = False
    analysis_assessment_id: str | None = None
    analysis_assessment_path: Path | None = None
    thesis_revision_id: str | None = None
    thesis_revision_path: Path | None = None
    analysis_outcome_record_id: str | None = None
    analysis_outcome_path: Path | None = None


@dataclass(frozen=True, slots=True)
class AnalysisCommitJournalRow:
    task_id: str
    target_key: str
    business_at: datetime
    record_type: str | None
    payload: dict[str, object]
    path: Path
    line_number: int


@dataclass(frozen=True, slots=True)
class AnalysisCommitAuditState:
    task_id: str
    target_key: str
    business_at: datetime
    commit_started_seen: bool
    record_types_seen: tuple[str, ...]
    terminal_record_type: str | None
    terminal_journal_path: Path
    terminal_journal_line_number: int
    status: AnalysisCommitStatus
    completion_state: AnalysisCommitCompletionState
    failure_stage: str | None
    error_type: str | None
    error_message: str | None
    thesis_revision_required: bool
    context_packet_path: Path | None
    research_memory_writes_committed: bool
    research_memory_write_receipt_ids: tuple[str, ...]
    analysis_assessment_id: str | None
    analysis_assessment_path: Path | None
    thesis_revision_id: str | None
    thesis_revision_path: Path | None
    analysis_outcome_record_id: str | None
    analysis_outcome_path: Path | None

    @property
    def context_packet_persisted(self) -> bool:
        return self.context_packet_path is not None

    @property
    def analysis_assessment_persisted(self) -> bool:
        return self.analysis_assessment_path is not None

    @property
    def thesis_revision_persisted(self) -> bool:
        return self.thesis_revision_path is not None

    @property
    def analysis_outcome_persisted(self) -> bool:
        return self.analysis_outcome_path is not None

    @property
    def thesis_revision_obligation_status(self) -> ThesisRevisionObligationStatus:
        return thesis_revision_obligation_status_from_progress(
            AnalysisCommitProgress(
                context_packet_path=self.context_packet_path,
                research_memory_writes_committed=self.research_memory_writes_committed,
                research_memory_write_receipt_ids=self.research_memory_write_receipt_ids,
                thesis_revision_required=self.thesis_revision_required,
                analysis_assessment_id=self.analysis_assessment_id,
                analysis_assessment_path=self.analysis_assessment_path,
                thesis_revision_id=self.thesis_revision_id,
                thesis_revision_path=self.thesis_revision_path,
                analysis_outcome_record_id=self.analysis_outcome_record_id,
                analysis_outcome_path=self.analysis_outcome_path,
            )
        )


def analysis_commit_journal_path(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    business_at: datetime,
) -> Path:
    if not isinstance(layout, WorkspaceLayout):
        raise AnalysisCommitAuditError("layout must be a WorkspaceLayout instance.")
    if not isinstance(business_at, datetime):
        raise AnalysisCommitAuditError("business_at must be a datetime.")
    return (
        layout.runtime_root
        / ANALYSIS_COMMIT_DIR_NAME
        / validate_target_key(target_key, error_type=AnalysisCommitAuditError)
        / f"{business_at:%Y-%m}.jsonl"
    ).resolve(strict=False)


def analysis_commit_state_payload(
    *,
    progress: AnalysisCommitProgress,
    status: AnalysisCommitStatus,
) -> dict[str, object]:
    return {
        "commit_status": status,
        "commit_completion_state": analysis_commit_completion_state(
            progress=progress,
            status=status,
        ),
        "thesis_revision_obligation_status": thesis_revision_obligation_status_from_progress(
            progress
        ),
        "context_packet_path": (
            None if progress.context_packet_path is None else str(progress.context_packet_path)
        ),
        "context_packet_persisted": progress.context_packet_path is not None,
        "research_memory_writes_committed": progress.research_memory_writes_committed,
        "research_memory_write_receipt_ids": list(progress.research_memory_write_receipt_ids),
        "thesis_revision_required": progress.thesis_revision_required,
        "analysis_assessment_id": progress.analysis_assessment_id,
        "analysis_assessment_path": (
            None
            if progress.analysis_assessment_path is None
            else str(progress.analysis_assessment_path)
        ),
        "analysis_assessment_persisted": progress.analysis_assessment_path is not None,
        "thesis_revision_id": progress.thesis_revision_id,
        "thesis_revision_path": (
            None
            if progress.thesis_revision_path is None
            else str(progress.thesis_revision_path)
        ),
        "thesis_revision_persisted": progress.thesis_revision_path is not None,
        "analysis_outcome_record_id": progress.analysis_outcome_record_id,
        "analysis_outcome_path": (
            None
            if progress.analysis_outcome_path is None
            else str(progress.analysis_outcome_path)
        ),
        "analysis_outcome_persisted": progress.analysis_outcome_path is not None,
    }


def analysis_commit_completion_state(
    *,
    progress: AnalysisCommitProgress,
    status: AnalysisCommitStatus,
) -> AnalysisCommitCompletionState:
    if status == "complete":
        return "complete"
    if progress.analysis_outcome_path is not None:
        return "incomplete_after_outcome"
    if progress.thesis_revision_path is not None:
        return "incomplete_after_thesis_revision"
    if progress.analysis_assessment_path is not None:
        return "incomplete_after_analysis_assessment"
    if progress.research_memory_writes_committed:
        return "incomplete_after_memory_writes"
    if progress.context_packet_path is not None:
        return "incomplete_after_context_packet_persistence"
    return "incomplete_before_context_packet_persistence"


def thesis_revision_obligation_status_from_progress(
    progress: AnalysisCommitProgress,
) -> ThesisRevisionObligationStatus:
    if not progress.thesis_revision_required:
        return "not_required"
    return "satisfied" if progress.thesis_revision_path is not None else "required_unpersisted"


def read_analysis_commit_audit_state(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    business_at: datetime,
    task_id: str,
) -> AnalysisCommitAuditState | None:
    for state in read_target_analysis_commit_audit_states(
        layout=layout,
        target_key=target_key,
        year_month=business_at.strftime("%Y-%m"),
    ):
        if state.task_id == task_id and state.business_at == business_at:
            return state
    return None


def read_target_analysis_commit_audit_states(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    year_month: str | None = None,
) -> tuple[AnalysisCommitAuditState, ...]:
    if not isinstance(layout, WorkspaceLayout):
        raise AnalysisCommitAuditError("layout must be a WorkspaceLayout instance.")
    journal_rows = _load_target_analysis_commit_journal_rows(
        layout=layout,
        target_key=target_key,
        year_month=year_month,
    )
    rows_by_commit: dict[tuple[str, str], list[AnalysisCommitJournalRow]] = {}
    for row in journal_rows:
        rows_by_commit.setdefault(
            (row.task_id, row.business_at.isoformat()),
            [],
        ).append(row)
    states = [
        _build_analysis_commit_audit_state(rows)
        for rows in rows_by_commit.values()
    ]
    return tuple(
        sorted(
            states,
            key=lambda state: (
                state.business_at,
                state.terminal_journal_path.as_posix(),
                state.terminal_journal_line_number,
                state.task_id,
            ),
        )
    )


def _build_analysis_commit_audit_state(
    rows: Iterable[AnalysisCommitJournalRow],
) -> AnalysisCommitAuditState:
    ordered_rows = tuple(rows)
    if not ordered_rows:
        raise AnalysisCommitAuditError("analysis commit audit state requires at least one row.")
    first_row = ordered_rows[0]
    progress = AnalysisCommitProgress()
    failure_stage: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    record_types_seen: list[str] = []

    for row in ordered_rows:
        if row.record_type is not None and row.record_type not in record_types_seen:
            record_types_seen.append(row.record_type)
        payload_dict = row.payload
        progress = AnalysisCommitProgress(
            context_packet_path=(
                _optional_payload_path(payload_dict, "context_packet_path")
                or progress.context_packet_path
            ),
            research_memory_writes_committed=(
                progress.research_memory_writes_committed
                or _optional_payload_bool(payload_dict, "research_memory_writes_committed")
            ),
            research_memory_write_receipt_ids=(
                _optional_payload_string_tuple(
                    payload_dict,
                    "research_memory_write_receipt_ids",
                )
                or progress.research_memory_write_receipt_ids
            ),
            thesis_revision_required=_optional_payload_bool(
                payload_dict,
                "thesis_revision_required",
                default=progress.thesis_revision_required,
            ),
            analysis_assessment_id=(
                _optional_payload_text(payload_dict, "analysis_assessment_id")
                or progress.analysis_assessment_id
            ),
            analysis_assessment_path=(
                _optional_payload_path(payload_dict, "analysis_assessment_path")
                or progress.analysis_assessment_path
            ),
            thesis_revision_id=(
                _optional_payload_text(payload_dict, "thesis_revision_id")
                or progress.thesis_revision_id
            ),
            thesis_revision_path=(
                _optional_payload_path(payload_dict, "thesis_revision_path")
                or progress.thesis_revision_path
            ),
            analysis_outcome_record_id=(
                _optional_payload_text(payload_dict, "analysis_outcome_record_id")
                or progress.analysis_outcome_record_id
            ),
            analysis_outcome_path=(
                _optional_payload_path(payload_dict, "analysis_outcome_path")
                or progress.analysis_outcome_path
            ),
        )
        if row.record_type == "commit_failed":
            failure_stage = _optional_payload_text(payload_dict, "failure_stage")
            error_type = _optional_payload_text(payload_dict, "error_type")
            error_message = _optional_payload_text(payload_dict, "error_message")

    terminal_row = ordered_rows[-1]
    if terminal_row.record_type == "commit_completed":
        status: AnalysisCommitStatus = "complete"
    elif terminal_row.record_type == "commit_failed":
        status = "failed"
    else:
        status = "incomplete"

    return AnalysisCommitAuditState(
        task_id=first_row.task_id,
        target_key=first_row.target_key,
        business_at=first_row.business_at,
        commit_started_seen="commit_started" in record_types_seen,
        record_types_seen=tuple(record_types_seen),
        terminal_record_type=terminal_row.record_type,
        terminal_journal_path=terminal_row.path,
        terminal_journal_line_number=terminal_row.line_number,
        status=status,
        completion_state=analysis_commit_completion_state(
            progress=progress,
            status=status,
        ),
        failure_stage=failure_stage,
        error_type=error_type,
        error_message=error_message,
        thesis_revision_required=progress.thesis_revision_required,
        context_packet_path=progress.context_packet_path,
        research_memory_writes_committed=progress.research_memory_writes_committed,
        research_memory_write_receipt_ids=progress.research_memory_write_receipt_ids,
        analysis_assessment_id=progress.analysis_assessment_id,
        analysis_assessment_path=progress.analysis_assessment_path,
        thesis_revision_id=progress.thesis_revision_id,
        thesis_revision_path=progress.thesis_revision_path,
        analysis_outcome_record_id=progress.analysis_outcome_record_id,
        analysis_outcome_path=progress.analysis_outcome_path,
    )


def _load_target_analysis_commit_journal_rows(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    year_month: str | None,
) -> tuple[AnalysisCommitJournalRow, ...]:
    normalized_target_key = validate_target_key(
        target_key,
        error_type=AnalysisCommitAuditError,
    )
    paths = _analysis_commit_journal_paths(
        layout=layout,
        target_key=normalized_target_key,
        year_month=year_month,
    )
    rows: list[AnalysisCommitJournalRow] = []
    for path in paths:
        rows.extend(
            _load_analysis_commit_journal_rows(
                path,
                expected_target_key=normalized_target_key,
            )
        )
    return tuple(rows)


def _analysis_commit_journal_paths(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    year_month: str | None,
) -> tuple[Path, ...]:
    normalized_target_key = validate_target_key(
        target_key,
        error_type=AnalysisCommitAuditError,
    )
    journal_root = layout.runtime_root / ANALYSIS_COMMIT_DIR_NAME / normalized_target_key
    if year_month is not None:
        if not isinstance(year_month, str) or len(year_month) != 7:
            raise AnalysisCommitAuditError("year_month must be YYYY-MM.")
        return ((journal_root / f"{year_month}.jsonl").resolve(strict=False),)
    return tuple(sorted(journal_root.glob("*.jsonl")))


def _load_analysis_commit_journal_rows(
    path: Path,
    *,
    expected_target_key: str | None = None,
) -> tuple[AnalysisCommitJournalRow, ...]:
    rows: list[AnalysisCommitJournalRow] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    raise AnalysisCommitAuditError(
                        "Analysis commit journal contains invalid JSON."
                    ) from exc
                if not isinstance(payload, dict):
                    raise AnalysisCommitAuditError(
                        "Analysis commit journal entries must be JSON objects."
                    )
                task_id = _optional_payload_text(payload, "task_id")
                target_key = _optional_payload_text(payload, "target_key")
                business_at = _optional_payload_datetime(payload, "business_at")
                if task_id is None or target_key is None or business_at is None:
                    continue
                if expected_target_key is not None and target_key != expected_target_key:
                    raise AnalysisCommitAuditError(
                        "Analysis commit journal shard contains mixed target_key rows."
                    )
                row_payload = payload.get("payload")
                payload_dict = row_payload if isinstance(row_payload, dict) else {}
                rows.append(
                    AnalysisCommitJournalRow(
                        task_id=task_id,
                        target_key=target_key,
                        business_at=business_at,
                        record_type=_optional_payload_text(payload, "record_type"),
                        payload=payload_dict,
                        path=path.resolve(strict=False),
                        line_number=line_number,
                    )
                )
    except OSError as exc:
        raise AnalysisCommitAuditError(
            "Failed to read analysis commit journal."
        ) from exc
    return tuple(rows)


def _optional_payload_datetime(
    payload: dict[str, object],
    field_name: str,
) -> datetime | None:
    value = _optional_payload_text(payload, field_name)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise AnalysisCommitAuditError(
            f"{field_name} must be an ISO datetime string."
        ) from exc


def _optional_payload_text(payload: dict[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_payload_bool(
    payload: dict[str, object],
    field_name: str,
    *,
    default: bool = False,
) -> bool:
    value = payload.get(field_name)
    return value if isinstance(value, bool) else default


def _optional_payload_path(payload: dict[str, object], field_name: str) -> Path | None:
    value = _optional_payload_text(payload, field_name)
    return None if value is None else Path(value).resolve(strict=False)


def _optional_payload_string_tuple(
    payload: dict[str, object],
    field_name: str,
) -> tuple[str, ...] | None:
    value = payload.get(field_name)
    if not isinstance(value, list):
        return None
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


__all__ = [
    "ANALYSIS_COMMIT_DIR_NAME",
    "AnalysisCommitAuditError",
    "AnalysisCommitJournalRow",
    "AnalysisCommitAuditState",
    "AnalysisCommitCompletionState",
    "AnalysisCommitProgress",
    "AnalysisCommitStatus",
    "ThesisRevisionObligationStatus",
    "analysis_commit_completion_state",
    "analysis_commit_journal_path",
    "analysis_commit_state_payload",
    "read_analysis_commit_audit_state",
    "read_target_analysis_commit_audit_states",
    "thesis_revision_obligation_status_from_progress",
]
