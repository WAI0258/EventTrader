"""Durable checker completion outbox for Nautilus-safe republishing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from .contracts import (
    CheckerDecision,
    CheckerFailed,
)

type CheckerCompletionOutboxEntryKind = Literal["decision", "failed"]
type CheckerCompletionMessage = CheckerDecision | CheckerFailed


class CheckerCompletionOutboxError(ValueError):
    """Raised when checker completion outbox payloads or wiring are invalid."""


@dataclass(frozen=True, slots=True)
class CheckerCompletionOutboxEntry:
    """Durable outbox entry awaiting runtime-owned publication."""

    outbox_name: str
    entry_kind: CheckerCompletionOutboxEntryKind
    work_item_id: str
    idempotency_key: str
    message: CheckerCompletionMessage
    recorded_at: datetime
    entry_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "outbox_name", _require_text(self.outbox_name, "outbox_name"))
        if self.entry_kind not in {"decision", "failed"}:
            raise CheckerCompletionOutboxError("entry_kind must be decision or failed.")
        object.__setattr__(self, "work_item_id", _require_text(self.work_item_id, "work_item_id"))
        object.__setattr__(
            self,
            "idempotency_key",
            _require_text(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(self, "recorded_at", _require_datetime(self.recorded_at, "recorded_at"))
        if not isinstance(self.entry_path, Path):
            raise CheckerCompletionOutboxError("entry_path must be a Path.")
        if self.entry_kind == "decision" and not isinstance(self.message, CheckerDecision):
            raise CheckerCompletionOutboxError("decision entry must carry CheckerDecision.")
        if self.entry_kind == "failed" and not isinstance(self.message, CheckerFailed):
            raise CheckerCompletionOutboxError("failed entry must carry CheckerFailed.")

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


class FileBackedCheckerCompletionOutbox:
    """Persist checker completions until the runtime thread republishes them."""

    def __init__(self, *, root: Path, outbox_name: str = "checker_completions") -> None:
        if not isinstance(root, Path):
            raise CheckerCompletionOutboxError("root must be a Path.")
        self._root = root
        self._outbox_name = _require_text(outbox_name, "outbox_name")
        self._pending_root = root / self._outbox_name / "pending"
        self._published_root = root / self._outbox_name / "published"
        self._pending_root.mkdir(parents=True, exist_ok=True)
        self._published_root.mkdir(parents=True, exist_ok=True)

    def record_decision(
        self,
        *,
        work_item_id: str,
        idempotency_key: str,
        message: CheckerDecision,
    ) -> CheckerCompletionOutboxEntry:
        return self._record(
            entry_kind="decision",
            work_item_id=work_item_id,
            idempotency_key=idempotency_key,
            message=message,
        )

    def record_failure(
        self,
        *,
        work_item_id: str,
        idempotency_key: str,
        message: CheckerFailed,
    ) -> CheckerCompletionOutboxEntry:
        return self._record(
            entry_kind="failed",
            work_item_id=work_item_id,
            idempotency_key=idempotency_key,
            message=message,
        )

    def load_pending(self) -> tuple[CheckerCompletionOutboxEntry, ...]:
        return tuple(
            _read_outbox_entry(path)
            for path in sorted(self._pending_root.glob("*.json"))
        )

    def load_published(self) -> tuple[CheckerCompletionOutboxEntry, ...]:
        return tuple(
            _read_outbox_entry(path)
            for path in sorted(self._published_root.glob("*.json"))
        )

    def mark_published(
        self,
        entry: CheckerCompletionOutboxEntry,
    ) -> CheckerCompletionOutboxEntry:
        if not isinstance(entry, CheckerCompletionOutboxEntry):
            raise CheckerCompletionOutboxError("entry must be a CheckerCompletionOutboxEntry.")
        if entry.outbox_name != self._outbox_name:
            raise CheckerCompletionOutboxError("entry outbox_name does not match this outbox.")
        pending_path = self._pending_path(entry.idempotency_key)
        published_path = self._published_path(entry.idempotency_key)
        if published_path.exists():
            return _read_outbox_entry(published_path)
        if not pending_path.exists():
            raise CheckerCompletionOutboxError(
                f"pending outbox entry is missing for {entry.idempotency_key}."
            )
        pending_path.replace(published_path)
        return _read_outbox_entry(published_path)

    def _record(
        self,
        *,
        entry_kind: CheckerCompletionOutboxEntryKind,
        work_item_id: str,
        idempotency_key: str,
        message: CheckerCompletionMessage,
    ) -> CheckerCompletionOutboxEntry:
        normalized_work_item_id = _require_text(work_item_id, "work_item_id")
        normalized_idempotency_key = _require_text(idempotency_key, "idempotency_key")
        pending_path = self._pending_path(normalized_idempotency_key)
        published_path = self._published_path(normalized_idempotency_key)
        if published_path.exists():
            return _read_outbox_entry(published_path)
        if pending_path.exists():
            return _read_outbox_entry(pending_path)
        entry = CheckerCompletionOutboxEntry(
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


def _read_outbox_entry(path: Path) -> CheckerCompletionOutboxEntry:
    payload = _require_dict(json.loads(path.read_text(encoding="utf-8")), "outbox entry")
    entry_kind = _require_text(payload.get("entry_kind"), "entry_kind")
    message_payload = _require_dict(payload.get("message"), "message")
    if entry_kind == "decision":
        message: CheckerCompletionMessage = _checker_decision_from_payload(message_payload)
    elif entry_kind == "failed":
        message = _checker_failed_from_payload(message_payload)
    else:
        raise CheckerCompletionOutboxError("entry_kind must be decision or failed.")
    return CheckerCompletionOutboxEntry(
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


def _checker_decision_from_payload(payload: dict[str, object]) -> CheckerDecision:
    return CheckerDecision(
        message_id=_require_text(payload.get("message_id"), "message_id"),
        schema_version=_require_text(payload.get("schema_version"), "schema_version"),
        topic=_require_text(payload.get("topic"), "topic"),
        event_time=_require_datetime(payload.get("event_time"), "event_time"),
        recorded_at=_require_datetime(payload.get("recorded_at"), "recorded_at"),
        correlation_id=_require_text(payload.get("correlation_id"), "correlation_id"),
        causation_id=_optional_text(payload.get("causation_id"), "causation_id"),
        idempotency_key=_require_text(payload.get("idempotency_key"), "idempotency_key"),
        target_key=_require_text(payload.get("target_key"), "target_key"),
        event_id=_require_text(payload.get("event_id"), "event_id"),
        evidence_record_ref=_require_text(
            payload.get("evidence_record_ref"),
            "evidence_record_ref",
        ),
        decision_record_ref=_require_text(
            payload.get("decision_record_ref"),
            "decision_record_ref",
        ),
    )


def _checker_failed_from_payload(payload: dict[str, object]) -> CheckerFailed:
    return CheckerFailed(
        message_id=_require_text(payload.get("message_id"), "message_id"),
        schema_version=_require_text(payload.get("schema_version"), "schema_version"),
        topic=_require_text(payload.get("topic"), "topic"),
        event_time=_require_datetime(payload.get("event_time"), "event_time"),
        recorded_at=_require_datetime(payload.get("recorded_at"), "recorded_at"),
        correlation_id=_require_text(payload.get("correlation_id"), "correlation_id"),
        causation_id=_optional_text(payload.get("causation_id"), "causation_id"),
        idempotency_key=_require_text(payload.get("idempotency_key"), "idempotency_key"),
        target_key=_require_text(payload.get("target_key"), "target_key"),
        event_id=_require_text(payload.get("event_id"), "event_id"),
        evidence_record_ref=_require_text(
            payload.get("evidence_record_ref"),
            "evidence_record_ref",
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
        raise CheckerCompletionOutboxError(f"{field_name} must be a JSON object.")
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise CheckerCompletionOutboxError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise CheckerCompletionOutboxError(f"{field_name} must not be blank.")
    return normalized


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name)


def _require_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise CheckerCompletionOutboxError(f"{field_name} must be an ISO8601 string.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise CheckerCompletionOutboxError(
            f"{field_name} must be an ISO8601 string."
        ) from exc


__all__ = [
    "CheckerCompletionMessage",
    "CheckerCompletionOutboxEntry",
    "CheckerCompletionOutboxEntryKind",
    "CheckerCompletionOutboxError",
    "FileBackedCheckerCompletionOutbox",
]
