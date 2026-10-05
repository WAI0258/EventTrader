"""Durable analysis completion outbox for Nautilus-safe republishing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from .contracts import AnalysisFailed, AnalysisOutcome

type AnalysisCompletionOutboxEntryKind = Literal["outcome", "failed"]
type AnalysisCompletionMessage = AnalysisOutcome | AnalysisFailed


class AnalysisCompletionOutboxError(ValueError):
    """Raised when analysis completion outbox payloads or wiring are invalid."""


@dataclass(frozen=True, slots=True)
class AnalysisCompletionOutboxEntry:
    """Durable outbox entry awaiting runtime-owned publication."""

    outbox_name: str
    entry_kind: AnalysisCompletionOutboxEntryKind
    work_item_id: str
    idempotency_key: str
    message: AnalysisCompletionMessage
    recorded_at: datetime
    entry_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "outbox_name", _require_text(self.outbox_name, "outbox_name"))
        if self.entry_kind not in {"outcome", "failed"}:
            raise AnalysisCompletionOutboxError("entry_kind must be outcome or failed.")
        object.__setattr__(self, "work_item_id", _require_text(self.work_item_id, "work_item_id"))
        object.__setattr__(
            self,
            "idempotency_key",
            _require_text(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(self, "recorded_at", _require_datetime(self.recorded_at, "recorded_at"))
        if not isinstance(self.entry_path, Path):
            raise AnalysisCompletionOutboxError("entry_path must be a Path.")
        if self.entry_kind == "outcome" and not isinstance(self.message, AnalysisOutcome):
            raise AnalysisCompletionOutboxError("outcome entry must carry AnalysisOutcome.")
        if self.entry_kind == "failed" and not isinstance(self.message, AnalysisFailed):
            raise AnalysisCompletionOutboxError("failed entry must carry AnalysisFailed.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "outbox_name": self.outbox_name,
            "entry_kind": self.entry_kind,
            "work_item_id": self.work_item_id,
            "idempotency_key": self.idempotency_key,
            "recorded_at": self.recorded_at.isoformat(),
            "message_type": type(self.message).__name__,
            "message": self.message.to_json_payload(),
        }


class FileBackedAnalysisCompletionOutbox:
    """Persist analysis completions until the runtime thread republishes them."""

    def __init__(self, *, root: Path, outbox_name: str = "analysis_completions") -> None:
        if not isinstance(root, Path):
            raise AnalysisCompletionOutboxError("root must be a Path.")
        self._root = root
        self._outbox_name = _require_text(outbox_name, "outbox_name")
        self._pending_root = root / self._outbox_name / "pending"
        self._published_root = root / self._outbox_name / "published"
        self._pending_root.mkdir(parents=True, exist_ok=True)
        self._published_root.mkdir(parents=True, exist_ok=True)

    def record_outcome(
        self,
        *,
        work_item_id: str,
        idempotency_key: str,
        message: AnalysisOutcome,
    ) -> AnalysisCompletionOutboxEntry:
        return self._record(
            entry_kind="outcome",
            work_item_id=work_item_id,
            idempotency_key=idempotency_key,
            message=message,
        )

    def record_failure(
        self,
        *,
        work_item_id: str,
        idempotency_key: str,
        message: AnalysisFailed,
    ) -> AnalysisCompletionOutboxEntry:
        return self._record(
            entry_kind="failed",
            work_item_id=work_item_id,
            idempotency_key=idempotency_key,
            message=message,
        )

    def load_pending(self) -> tuple[AnalysisCompletionOutboxEntry, ...]:
        return tuple(
            _read_outbox_entry(path)
            for path in sorted(self._pending_root.glob("*.json"))
        )

    def load_published(self) -> tuple[AnalysisCompletionOutboxEntry, ...]:
        return tuple(
            _read_outbox_entry(path)
            for path in sorted(self._published_root.glob("*.json"))
        )

    def mark_published(
        self,
        entry: AnalysisCompletionOutboxEntry,
    ) -> AnalysisCompletionOutboxEntry:
        if not isinstance(entry, AnalysisCompletionOutboxEntry):
            raise AnalysisCompletionOutboxError("entry must be an AnalysisCompletionOutboxEntry.")
        if entry.outbox_name != self._outbox_name:
            raise AnalysisCompletionOutboxError("entry outbox_name does not match this outbox.")
        pending_path = self._pending_path(entry.idempotency_key)
        published_path = self._published_path(entry.idempotency_key)
        if published_path.exists():
            return _read_outbox_entry(published_path)
        if not pending_path.exists():
            raise AnalysisCompletionOutboxError(
                f"pending outbox entry is missing for {entry.idempotency_key}."
            )
        pending_path.replace(published_path)
        return _read_outbox_entry(published_path)

    def _record(
        self,
        *,
        entry_kind: AnalysisCompletionOutboxEntryKind,
        work_item_id: str,
        idempotency_key: str,
        message: AnalysisCompletionMessage,
    ) -> AnalysisCompletionOutboxEntry:
        normalized_work_item_id = _require_text(work_item_id, "work_item_id")
        normalized_idempotency_key = _require_text(idempotency_key, "idempotency_key")
        pending_path = self._pending_path(normalized_idempotency_key)
        published_path = self._published_path(normalized_idempotency_key)
        if published_path.exists():
            return _read_outbox_entry(published_path)
        if pending_path.exists():
            return _read_outbox_entry(pending_path)
        entry = AnalysisCompletionOutboxEntry(
            outbox_name=self._outbox_name,
            entry_kind=entry_kind,
            work_item_id=normalized_work_item_id,
            idempotency_key=normalized_idempotency_key,
            message=message,
            recorded_at=message.recorded_at,
            entry_path=pending_path,
        )
        pending_path.write_text(
            json.dumps(entry.to_json_payload(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return _read_outbox_entry(pending_path)

    def _pending_path(self, idempotency_key: str) -> Path:
        return self._pending_root / f"{_idempotency_digest(idempotency_key)}.json"

    def _published_path(self, idempotency_key: str) -> Path:
        return self._published_root / f"{_idempotency_digest(idempotency_key)}.json"


def _read_outbox_entry(path: Path) -> AnalysisCompletionOutboxEntry:
    payload = _require_dict(json.loads(path.read_text(encoding="utf-8")), "outbox entry")
    entry_kind = _require_text(payload.get("entry_kind"), "entry_kind")
    message_payload = _require_dict(payload.get("message"), "message")
    if entry_kind == "outcome":
        message: AnalysisCompletionMessage = _analysis_outcome_from_payload(message_payload)
    elif entry_kind == "failed":
        message = _analysis_failed_from_payload(message_payload)
    else:
        raise AnalysisCompletionOutboxError("entry_kind must be outcome or failed.")
    return AnalysisCompletionOutboxEntry(
        outbox_name=_require_text(payload.get("outbox_name"), "outbox_name"),
        entry_kind=entry_kind,
        work_item_id=_require_text(payload.get("work_item_id"), "work_item_id"),
        idempotency_key=_require_text(payload.get("idempotency_key"), "idempotency_key"),
        message=message,
        recorded_at=_require_datetime(
            _require_text(payload.get("recorded_at"), "recorded_at"),
            "recorded_at",
        ),
        entry_path=path,
    )


def _analysis_outcome_from_payload(payload: dict[str, object]) -> AnalysisOutcome:
    return AnalysisOutcome(
        message_id=_require_text(payload.get("message_id"), "message_id"),
        schema_version=_require_text(payload.get("schema_version"), "schema_version"),
        topic=_require_text(payload.get("topic"), "topic"),
        event_time=_require_datetime(payload.get("event_time"), "event_time"),
        recorded_at=_require_datetime(payload.get("recorded_at"), "recorded_at"),
        correlation_id=_require_text(payload.get("correlation_id"), "correlation_id"),
        causation_id=_optional_text(payload.get("causation_id"), "causation_id"),
        idempotency_key=_require_text(payload.get("idempotency_key"), "idempotency_key"),
        target_key=_require_text(payload.get("target_key"), "target_key"),
        analysis_unit_id=_require_text(payload.get("analysis_unit_id"), "analysis_unit_id"),
        record_id=_require_text(payload.get("record_id"), "record_id"),
        outcome_record_ref=_require_text(
            payload.get("outcome_record_ref"),
            "outcome_record_ref",
        ),
    )


def _analysis_failed_from_payload(payload: dict[str, object]) -> AnalysisFailed:
    return AnalysisFailed(
        message_id=_require_text(payload.get("message_id"), "message_id"),
        schema_version=_require_text(payload.get("schema_version"), "schema_version"),
        topic=_require_text(payload.get("topic"), "topic"),
        event_time=_require_datetime(payload.get("event_time"), "event_time"),
        recorded_at=_require_datetime(payload.get("recorded_at"), "recorded_at"),
        correlation_id=_require_text(payload.get("correlation_id"), "correlation_id"),
        causation_id=_optional_text(payload.get("causation_id"), "causation_id"),
        idempotency_key=_require_text(payload.get("idempotency_key"), "idempotency_key"),
        target_key=_require_text(payload.get("target_key"), "target_key"),
        analysis_unit_id=_require_text(payload.get("analysis_unit_id"), "analysis_unit_id"),
        record_id=_require_text(payload.get("record_id"), "record_id"),
        failure_record_ref=_require_text(
            payload.get("failure_record_ref"),
            "failure_record_ref",
        ),
    )


def _idempotency_digest(value: str) -> str:
    return sha256(_require_text(value, "idempotency_key").encode("utf-8")).hexdigest()


def _require_dict(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AnalysisCompletionOutboxError(f"{field_name} must be a JSON object.")
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise AnalysisCompletionOutboxError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise AnalysisCompletionOutboxError(f"{field_name} must not be blank.")
    return normalized


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name)


def _require_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise AnalysisCompletionOutboxError(f"{field_name} must be an ISO8601 string.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise AnalysisCompletionOutboxError(
            f"{field_name} must be an ISO8601 string."
        ) from exc


__all__ = [
    "AnalysisCompletionMessage",
    "AnalysisCompletionOutboxEntry",
    "AnalysisCompletionOutboxEntryKind",
    "AnalysisCompletionOutboxError",
    "FileBackedAnalysisCompletionOutbox",
]
