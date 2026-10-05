"""Durable PMReview completion outbox for Nautilus-safe republishing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

from .contracts import PMReviewCompleted, PMReviewFailed

type PMReviewCompletionOutboxEntryKind = Literal["completed", "failed"]
type PMReviewCompletionMessage = PMReviewCompleted | PMReviewFailed


class PMReviewCompletionOutboxError(ValueError):
    """Raised when PMReview completion outbox payloads or wiring are invalid."""


@dataclass(frozen=True, slots=True)
class PMReviewCompletionOutboxEntry:
    outbox_name: str
    entry_kind: PMReviewCompletionOutboxEntryKind
    work_item_id: str
    idempotency_key: str
    message: PMReviewCompletionMessage
    recorded_at: datetime
    entry_path: Path

    def __post_init__(self) -> None:
        for field_name in ("outbox_name", "work_item_id", "idempotency_key"):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        if self.entry_kind not in {"completed", "failed"}:
            raise PMReviewCompletionOutboxError("entry_kind must be completed or failed.")
        object.__setattr__(self, "recorded_at", _require_datetime(self.recorded_at, "recorded_at"))
        if not isinstance(self.entry_path, Path):
            raise PMReviewCompletionOutboxError("entry_path must be a Path.")
        if self.entry_kind == "completed" and not isinstance(self.message, PMReviewCompleted):
            raise PMReviewCompletionOutboxError(
                "completed entry must carry PMReviewCompleted."
            )
        if self.entry_kind == "failed" and not isinstance(self.message, PMReviewFailed):
            raise PMReviewCompletionOutboxError("failed entry must carry PMReviewFailed.")

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


class FileBackedPMReviewCompletionOutbox:
    """Persist PMReview completions until the runtime thread republishes them."""

    def __init__(self, *, root: Path, outbox_name: str = "pm_review_completions") -> None:
        if not isinstance(root, Path):
            raise PMReviewCompletionOutboxError("root must be a Path.")
        self._root = root
        self._outbox_name = _require_text(outbox_name, "outbox_name")
        self._pending_root = root / self._outbox_name / "pending"
        self._published_root = root / self._outbox_name / "published"
        self._pending_root.mkdir(parents=True, exist_ok=True)
        self._published_root.mkdir(parents=True, exist_ok=True)

    def record_completed(
        self,
        *,
        work_item_id: str,
        idempotency_key: str,
        message: PMReviewCompleted,
    ) -> PMReviewCompletionOutboxEntry:
        return self._record(
            entry_kind="completed",
            work_item_id=work_item_id,
            idempotency_key=idempotency_key,
            message=message,
        )

    def record_failed(
        self,
        *,
        work_item_id: str,
        idempotency_key: str,
        message: PMReviewFailed,
    ) -> PMReviewCompletionOutboxEntry:
        return self._record(
            entry_kind="failed",
            work_item_id=work_item_id,
            idempotency_key=idempotency_key,
            message=message,
        )

    def load_pending(self) -> tuple[PMReviewCompletionOutboxEntry, ...]:
        return tuple(
            _read_outbox_entry(path)
            for path in sorted(self._pending_root.glob("*.json"))
        )

    def load_published(self) -> tuple[PMReviewCompletionOutboxEntry, ...]:
        return tuple(
            _read_outbox_entry(path)
            for path in sorted(self._published_root.glob("*.json"))
        )

    def mark_published(
        self,
        entry: PMReviewCompletionOutboxEntry,
    ) -> PMReviewCompletionOutboxEntry:
        if not isinstance(entry, PMReviewCompletionOutboxEntry):
            raise PMReviewCompletionOutboxError("entry must be a PMReviewCompletionOutboxEntry.")
        if entry.outbox_name != self._outbox_name:
            raise PMReviewCompletionOutboxError(
                "entry outbox_name does not match this outbox."
            )
        pending_path = self._pending_path(entry.idempotency_key)
        published_path = self._published_path(entry.idempotency_key)
        if published_path.exists():
            return _read_outbox_entry(published_path)
        if not pending_path.exists():
            raise PMReviewCompletionOutboxError(
                f"pending outbox entry is missing for {entry.idempotency_key}."
            )
        pending_path.replace(published_path)
        return _read_outbox_entry(published_path)

    def _record(
        self,
        *,
        entry_kind: PMReviewCompletionOutboxEntryKind,
        work_item_id: str,
        idempotency_key: str,
        message: PMReviewCompletionMessage,
    ) -> PMReviewCompletionOutboxEntry:
        normalized_work_item_id = _require_text(work_item_id, "work_item_id")
        normalized_idempotency_key = _require_text(idempotency_key, "idempotency_key")
        pending_path = self._pending_path(normalized_idempotency_key)
        published_path = self._published_path(normalized_idempotency_key)
        if published_path.exists():
            return _read_outbox_entry(published_path)
        if pending_path.exists():
            return _read_outbox_entry(pending_path)
        entry = PMReviewCompletionOutboxEntry(
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


def _read_outbox_entry(path: Path) -> PMReviewCompletionOutboxEntry:
    payload = _require_dict(json.loads(path.read_text(encoding="utf-8")), "outbox entry")
    entry_kind = _require_text(payload.get("entry_kind"), "entry_kind")
    message_payload = _require_dict(payload.get("message"), "message")
    if entry_kind == "completed":
        message: PMReviewCompletionMessage = _pm_review_completed_from_payload(message_payload)
    elif entry_kind == "failed":
        message = _pm_review_failed_from_payload(message_payload)
    else:
        raise PMReviewCompletionOutboxError("entry_kind must be completed or failed.")
    return PMReviewCompletionOutboxEntry(
        outbox_name=_require_text(payload.get("outbox_name"), "outbox_name"),
        entry_kind=cast(PMReviewCompletionOutboxEntryKind, entry_kind),
        work_item_id=_require_text(payload.get("work_item_id"), "work_item_id"),
        idempotency_key=_require_text(payload.get("idempotency_key"), "idempotency_key"),
        message=message,
        recorded_at=_require_datetime(
            _require_text(payload.get("recorded_at"), "recorded_at"),
            "recorded_at",
        ),
        entry_path=path,
    )


def _pm_review_completed_from_payload(payload: dict[str, object]) -> PMReviewCompleted:
    return PMReviewCompleted(
        message_id=_require_text(payload.get("message_id"), "message_id"),
        schema_version=_require_text(payload.get("schema_version"), "schema_version"),
        topic=_require_text(payload.get("topic"), "topic"),
        event_time=_require_datetime(payload.get("event_time"), "event_time"),
        recorded_at=_require_datetime(payload.get("recorded_at"), "recorded_at"),
        correlation_id=_require_text(payload.get("correlation_id"), "correlation_id"),
        causation_id=_optional_text(payload.get("causation_id"), "causation_id"),
        idempotency_key=_require_text(payload.get("idempotency_key"), "idempotency_key"),
        target_key=_require_text(payload.get("target_key"), "target_key"),
        pm_review_request_id=_require_text(
            payload.get("pm_review_request_id"),
            "pm_review_request_id",
        ),
        source_episode_id=_optional_text(
            payload.get("source_episode_id"),
            "source_episode_id",
        ),
        pm_decision_ref=_require_text(payload.get("pm_decision_ref"), "pm_decision_ref"),
    )


def _pm_review_failed_from_payload(payload: dict[str, object]) -> PMReviewFailed:
    return PMReviewFailed(
        message_id=_require_text(payload.get("message_id"), "message_id"),
        schema_version=_require_text(payload.get("schema_version"), "schema_version"),
        topic=_require_text(payload.get("topic"), "topic"),
        event_time=_require_datetime(payload.get("event_time"), "event_time"),
        recorded_at=_require_datetime(payload.get("recorded_at"), "recorded_at"),
        correlation_id=_require_text(payload.get("correlation_id"), "correlation_id"),
        causation_id=_optional_text(payload.get("causation_id"), "causation_id"),
        idempotency_key=_require_text(payload.get("idempotency_key"), "idempotency_key"),
        target_key=_require_text(payload.get("target_key"), "target_key"),
        pm_review_request_id=_require_text(
            payload.get("pm_review_request_id"),
            "pm_review_request_id",
        ),
        failure_record_ref=_require_text(
            payload.get("failure_record_ref"),
            "failure_record_ref",
        ),
    )


def _idempotency_digest(value: str) -> str:
    return sha256(_require_text(value, "idempotency_key").encode("utf-8")).hexdigest()


def _require_dict(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PMReviewCompletionOutboxError(f"{field_name} must be a JSON object.")
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise PMReviewCompletionOutboxError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise PMReviewCompletionOutboxError(f"{field_name} must not be blank.")
    return normalized


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name)


def _require_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise PMReviewCompletionOutboxError(f"{field_name} must be an ISO8601 string.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise PMReviewCompletionOutboxError(
            f"{field_name} must be an ISO8601 string."
        ) from exc


__all__ = [
    "FileBackedPMReviewCompletionOutbox",
    "PMReviewCompletionMessage",
    "PMReviewCompletionOutboxEntry",
    "PMReviewCompletionOutboxEntryKind",
    "PMReviewCompletionOutboxError",
]
