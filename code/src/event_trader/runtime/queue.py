"""Durable local queue primitives for checker work."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from event_trader.contracts._validators import (
    validate_event_id,
    validate_target_key,
    validate_timestamp,
)


class CheckerQueueError(ValueError):
    """Raised when checker queue wiring or payloads are invalid."""


type CheckerQueueEnqueueStatus = str


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CheckerWorkItem:
    """Durable checker work identified by admitted evidence refs only."""

    work_item_id: str
    target_key: str
    event_id: str
    source_ref: str
    evidence_record_ref: str
    event_time: datetime
    correlation_id: str
    causation_id: str | None
    idempotency_key: str
    enqueued_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "work_item_id",
            _validate_identifier(self.work_item_id, "work_item_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CheckerQueueError),
        )
        object.__setattr__(
            self,
            "event_id",
            validate_event_id(self.event_id, error_type=CheckerQueueError),
        )
        object.__setattr__(self, "source_ref", _validate_identifier(self.source_ref, "source_ref"))
        object.__setattr__(
            self,
            "evidence_record_ref",
            _validate_identifier(self.evidence_record_ref, "evidence_record_ref"),
        )
        object.__setattr__(
            self,
            "event_time",
            _validate_timestamp(self.event_time, field_name="event_time"),
        )
        object.__setattr__(
            self,
            "correlation_id",
            _validate_identifier(self.correlation_id, "correlation_id"),
        )
        object.__setattr__(
            self,
            "causation_id",
            _validate_optional_identifier(self.causation_id, "causation_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _validate_identifier(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(
            self,
            "enqueued_at",
            _validate_timestamp(self.enqueued_at, field_name="enqueued_at"),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "work_item_id": self.work_item_id,
            "target_key": self.target_key,
            "event_id": self.event_id,
            "source_ref": self.source_ref,
            "evidence_record_ref": self.evidence_record_ref,
            "event_time": self.event_time.isoformat(),
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "idempotency_key": self.idempotency_key,
            "enqueued_at": self.enqueued_at.isoformat(),
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> CheckerWorkItem:
        payload_map = _require_dict(payload, "CheckerWorkItem")
        return cls(
            work_item_id=_require_text(payload_map.get("work_item_id"), "work_item_id"),
            target_key=_require_text(payload_map.get("target_key"), "target_key"),
            event_id=_require_text(payload_map.get("event_id"), "event_id"),
            source_ref=_require_text(payload_map.get("source_ref"), "source_ref"),
            evidence_record_ref=_require_text(
                payload_map.get("evidence_record_ref"),
                "evidence_record_ref",
            ),
            event_time=_require_datetime(payload_map.get("event_time"), "event_time"),
            correlation_id=_require_text(payload_map.get("correlation_id"), "correlation_id"),
            causation_id=_optional_text(payload_map.get("causation_id"), "causation_id"),
            idempotency_key=_require_text(payload_map.get("idempotency_key"), "idempotency_key"),
            enqueued_at=_require_datetime(payload_map.get("enqueued_at"), "enqueued_at"),
        )


@dataclass(frozen=True, slots=True)
class CheckerQueueEnqueueReceipt:
    """Observable queue receipt for one checker enqueue attempt."""

    queue_name: str
    work_item_id: str
    idempotency_key: str
    status: CheckerQueueEnqueueStatus
    persisted_at: datetime
    queue_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "queue_name", _validate_identifier(self.queue_name, "queue_name"))
        object.__setattr__(
            self,
            "work_item_id",
            _validate_identifier(self.work_item_id, "work_item_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _validate_identifier(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(self, "status", _validate_status(self.status))
        object.__setattr__(
            self,
            "persisted_at",
            _validate_timestamp(self.persisted_at, field_name="persisted_at"),
        )
        if not isinstance(self.queue_path, Path):
            raise CheckerQueueError("queue_path must be a Path.")


@dataclass(frozen=True, slots=True)
class CheckerQueueDeadLetter:
    """Terminal queue failure metadata for one checker work item."""

    queue_name: str
    work_item_id: str
    idempotency_key: str
    reason: str
    recorded_at: datetime
    dead_letter_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "queue_name", _validate_identifier(self.queue_name, "queue_name"))
        object.__setattr__(
            self,
            "work_item_id",
            _validate_identifier(self.work_item_id, "work_item_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _validate_identifier(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(self, "reason", _validate_identifier(self.reason, "reason"))
        object.__setattr__(
            self,
            "recorded_at",
            _validate_timestamp(self.recorded_at, field_name="recorded_at"),
        )
        if not isinstance(self.dead_letter_path, Path):
            raise CheckerQueueError("dead_letter_path must be a Path.")


@dataclass(frozen=True, slots=True)
class CheckerQueueClaim:
    """Claimed checker work moved out of pending processing."""

    queue_name: str
    work_item_id: str
    idempotency_key: str
    item: CheckerWorkItem
    claimed_at: datetime
    claim_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "queue_name", _validate_identifier(self.queue_name, "queue_name"))
        object.__setattr__(
            self,
            "work_item_id",
            _validate_identifier(self.work_item_id, "work_item_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _validate_identifier(self.idempotency_key, "idempotency_key"),
        )
        if not isinstance(self.item, CheckerWorkItem):
            raise CheckerQueueError("item must be a CheckerWorkItem.")
        if self.item.work_item_id != self.work_item_id:
            raise CheckerQueueError("claim work_item_id must match item.")
        if self.item.idempotency_key != self.idempotency_key:
            raise CheckerQueueError("claim idempotency_key must match item.")
        object.__setattr__(
            self,
            "claimed_at",
            _validate_timestamp(self.claimed_at, field_name="claimed_at"),
        )
        if not isinstance(self.claim_path, Path):
            raise CheckerQueueError("claim_path must be a Path.")


@dataclass(frozen=True, slots=True)
class CheckerQueueCompletion:
    """Durable completion receipt for one checker work item."""

    queue_name: str
    work_item_id: str
    idempotency_key: str
    decision_record_ref: str
    completed_at: datetime
    completion_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "queue_name", _validate_identifier(self.queue_name, "queue_name"))
        object.__setattr__(
            self,
            "work_item_id",
            _validate_identifier(self.work_item_id, "work_item_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _validate_identifier(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(
            self,
            "decision_record_ref",
            _validate_identifier(self.decision_record_ref, "decision_record_ref"),
        )
        object.__setattr__(
            self,
            "completed_at",
            _validate_timestamp(self.completed_at, field_name="completed_at"),
        )
        if not isinstance(self.completion_path, Path):
            raise CheckerQueueError("completion_path must be a Path.")


@dataclass(frozen=True, slots=True)
class CheckerQueueFailure:
    """Durable failure receipt for one checker work item."""

    queue_name: str
    work_item_id: str
    idempotency_key: str
    reason: str
    error: str
    recorded_at: datetime
    failure_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "queue_name", _validate_identifier(self.queue_name, "queue_name"))
        object.__setattr__(
            self,
            "work_item_id",
            _validate_identifier(self.work_item_id, "work_item_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _validate_identifier(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(self, "reason", _validate_identifier(self.reason, "reason"))
        object.__setattr__(self, "error", _validate_identifier(self.error, "error"))
        object.__setattr__(
            self,
            "recorded_at",
            _validate_timestamp(self.recorded_at, field_name="recorded_at"),
        )
        if not isinstance(self.failure_path, Path):
            raise CheckerQueueError("failure_path must be a Path.")


class FileBackedCheckerWorkQueue:
    """Single-process durable queue for checker work items.

    This implementation is intentionally local and file-backed. It persists
    queue receipts and dead letters durably, but it does not yet implement
    multi-process claiming or lease renewal.
    """

    def __init__(
        self,
        *,
        root: Path,
        queue_name: str = "checker",
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(root, Path):
            raise CheckerQueueError("root must be a Path.")
        if not callable(now):
            raise CheckerQueueError("now must be callable.")
        self._root = root
        self._queue_name = _validate_identifier(queue_name, "queue_name")
        self._now = now
        self._pending_root = root / self._queue_name / "pending"
        self._in_flight_root = root / self._queue_name / "in_flight"
        self._completed_root = root / self._queue_name / "completed"
        self._failed_root = root / self._queue_name / "failed"
        self._dead_letter_root = root / self._queue_name / "dead_letters"
        self._pending_root.mkdir(parents=True, exist_ok=True)
        self._in_flight_root.mkdir(parents=True, exist_ok=True)
        self._completed_root.mkdir(parents=True, exist_ok=True)
        self._failed_root.mkdir(parents=True, exist_ok=True)
        self._dead_letter_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def enqueue(self, item: CheckerWorkItem) -> CheckerQueueEnqueueReceipt:
        with self._lock:
            if not isinstance(item, CheckerWorkItem):
                raise CheckerQueueError("item must be a CheckerWorkItem.")
            queue_path = self._pending_path(item.idempotency_key)
            status = "duplicate" if self._has_known_state(item.idempotency_key) else "enqueued"
            if status == "enqueued":
                queue_path.write_text(
                    json.dumps(item.to_json_payload(), sort_keys=True),
                    encoding="utf-8",
                )
            return CheckerQueueEnqueueReceipt(
                queue_name=self._queue_name,
                work_item_id=item.work_item_id,
                idempotency_key=item.idempotency_key,
                status=status,
                persisted_at=self._now(),
                queue_path=queue_path,
            )

    def claim_next(self) -> CheckerQueueClaim | None:
        """Move the next pending work item into in-flight processing."""
        with self._lock:
            now = self._now()
            for pending_path in sorted(self._pending_root.glob("*.json")):
                try:
                    item = _read_work_item(pending_path)
                except FileNotFoundError:
                    continue
                if item.event_time > now:
                    continue
                if self._completed_path(item.idempotency_key).exists():
                    self._remove_if_exists(pending_path)
                    continue
                claim_path = self._in_flight_path(item.idempotency_key)
                if claim_path.exists():
                    continue
                try:
                    pending_path.replace(claim_path)
                except FileNotFoundError:
                    continue
                return CheckerQueueClaim(
                    queue_name=self._queue_name,
                    work_item_id=item.work_item_id,
                    idempotency_key=item.idempotency_key,
                    item=item,
                    claimed_at=now,
                    claim_path=claim_path,
                )
            return None

    def complete(
        self,
        claim: CheckerQueueClaim,
        *,
        decision_payload: dict[str, object],
        decision_record_ref: str | None = None,
    ) -> CheckerQueueCompletion:
        with self._lock:
            if not isinstance(claim, CheckerQueueClaim):
                raise CheckerQueueError("claim must be a CheckerQueueClaim.")
            if claim.queue_name != self._queue_name:
                raise CheckerQueueError("claim queue_name does not match this queue.")
            if not isinstance(decision_payload, dict):
                raise CheckerQueueError("decision_payload must be a dict.")
            completion_path = self._completed_path(claim.idempotency_key)
            normalized_decision_record_ref = (
                f"{completion_path.resolve(strict=False)}#{claim.work_item_id}"
                if decision_record_ref is None
                else _validate_identifier(decision_record_ref, "decision_record_ref")
            )
            completed_at = self._now()
            if not completion_path.exists():
                completion_path.write_text(
                    json.dumps(
                        {
                            "queue_name": self._queue_name,
                            "work_item_id": claim.work_item_id,
                            "idempotency_key": claim.idempotency_key,
                            "decision_record_ref": normalized_decision_record_ref,
                            "completed_at": completed_at.isoformat(),
                            "item": claim.item.to_json_payload(),
                            "decision": decision_payload,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
            completion = _read_completion(completion_path)
            self._remove_if_exists(claim.claim_path)
            self._remove_if_exists(self._pending_path(claim.idempotency_key))
            return completion

    def fail(
        self,
        claim: CheckerQueueClaim,
        *,
        reason: str,
        error: str,
    ) -> CheckerQueueFailure:
        with self._lock:
            if not isinstance(claim, CheckerQueueClaim):
                raise CheckerQueueError("claim must be a CheckerQueueClaim.")
            if claim.queue_name != self._queue_name:
                raise CheckerQueueError("claim queue_name does not match this queue.")
            failure_path = self._failed_path(claim.idempotency_key)
            failure = CheckerQueueFailure(
                queue_name=self._queue_name,
                work_item_id=claim.work_item_id,
                idempotency_key=claim.idempotency_key,
                reason=reason,
                error=error,
                recorded_at=self._now(),
                failure_path=failure_path,
            )
            failure_path.write_text(
                json.dumps(
                    {
                        "queue_name": failure.queue_name,
                        "work_item_id": failure.work_item_id,
                        "idempotency_key": failure.idempotency_key,
                        "reason": failure.reason,
                        "error": failure.error,
                        "recorded_at": failure.recorded_at.isoformat(),
                        "item": claim.item.to_json_payload(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            self._remove_if_exists(claim.claim_path)
            self._remove_if_exists(self._pending_path(claim.idempotency_key))
            return failure

    def dead_letter(self, item: CheckerWorkItem, *, reason: str) -> CheckerQueueDeadLetter:
        with self._lock:
            if not isinstance(item, CheckerWorkItem):
                raise CheckerQueueError("item must be a CheckerWorkItem.")
            dead_letter_path = self._dead_letter_path(item.idempotency_key)
            dead_letter = CheckerQueueDeadLetter(
                queue_name=self._queue_name,
                work_item_id=item.work_item_id,
                idempotency_key=item.idempotency_key,
                reason=reason,
                recorded_at=self._now(),
                dead_letter_path=dead_letter_path,
            )
            dead_letter_path.write_text(
                json.dumps(
                    {
                        "queue_name": dead_letter.queue_name,
                        "work_item_id": dead_letter.work_item_id,
                        "idempotency_key": dead_letter.idempotency_key,
                        "reason": dead_letter.reason,
                        "recorded_at": dead_letter.recorded_at.isoformat(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            self._remove_if_exists(self._pending_path(item.idempotency_key))
            self._remove_if_exists(self._in_flight_path(item.idempotency_key))
            return dead_letter

    def load_pending(self) -> tuple[CheckerWorkItem, ...]:
        items: list[CheckerWorkItem] = []
        for path in sorted(self._pending_root.glob("*.json")):
            items.append(_read_work_item(path))
        return tuple(items)

    def load_in_flight(self) -> tuple[CheckerWorkItem, ...]:
        items: list[CheckerWorkItem] = []
        for path in sorted(self._in_flight_root.glob("*.json")):
            items.append(_read_work_item(path))
        return tuple(items)

    def reclaim_in_flight(self) -> tuple[CheckerWorkItem, ...]:
        reclaimed: list[CheckerWorkItem] = []
        with self._lock:
            for claim_path in sorted(self._in_flight_root.glob("*.json")):
                try:
                    item = _read_work_item(claim_path)
                except FileNotFoundError:
                    continue
                if any(
                    path.exists()
                    for path in (
                        self._completed_path(item.idempotency_key),
                        self._failed_path(item.idempotency_key),
                        self._dead_letter_path(item.idempotency_key),
                    )
                ):
                    self._remove_if_exists(claim_path)
                    continue
                pending_path = self._pending_path(item.idempotency_key)
                if pending_path.exists():
                    self._remove_if_exists(claim_path)
                    continue
                try:
                    claim_path.replace(pending_path)
                except FileNotFoundError:
                    continue
                reclaimed.append(item)
        return tuple(reclaimed)

    def load_completed(self) -> tuple[CheckerQueueCompletion, ...]:
        completions: list[CheckerQueueCompletion] = []
        for path in sorted(self._completed_root.glob("*.json")):
            completions.append(_read_completion(path))
        return tuple(completions)

    def load_failures(self) -> tuple[CheckerQueueFailure, ...]:
        failures: list[CheckerQueueFailure] = []
        for path in sorted(self._failed_root.glob("*.json")):
            failures.append(_read_failure(path))
        return tuple(failures)

    def _pending_path(self, idempotency_key: str) -> Path:
        return self._pending_root / f"{_idempotency_digest(idempotency_key)}.json"

    def _in_flight_path(self, idempotency_key: str) -> Path:
        return self._in_flight_root / f"{_idempotency_digest(idempotency_key)}.json"

    def _completed_path(self, idempotency_key: str) -> Path:
        return self._completed_root / f"{_idempotency_digest(idempotency_key)}.json"

    def _failed_path(self, idempotency_key: str) -> Path:
        return self._failed_root / f"{_idempotency_digest(idempotency_key)}.json"

    def _dead_letter_path(self, idempotency_key: str) -> Path:
        return self._dead_letter_root / f"{_idempotency_digest(idempotency_key)}.json"

    def _has_known_state(self, idempotency_key: str) -> bool:
        return any(
            path.exists()
            for path in (
                self._pending_path(idempotency_key),
                self._in_flight_path(idempotency_key),
                self._completed_path(idempotency_key),
                self._failed_path(idempotency_key),
                self._dead_letter_path(idempotency_key),
            )
        )

    def _remove_if_exists(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return


def _validate_identifier(value: str, field_name: str) -> str:
    return _require_text(value, field_name)


def _validate_optional_identifier(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_identifier(value, field_name)


def _validate_timestamp(value: datetime, *, field_name: str) -> datetime:
    return validate_timestamp(value, field_name=field_name, error_type=CheckerQueueError)


def _validate_status(value: str) -> CheckerQueueEnqueueStatus:
    if value not in {"enqueued", "duplicate"}:
        raise CheckerQueueError("status must be 'enqueued' or 'duplicate'.")
    return value


def _idempotency_digest(value: str) -> str:
    normalized = _validate_identifier(value, "idempotency_key")
    return sha256(normalized.encode("utf-8")).hexdigest()


def _require_dict(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CheckerQueueError(f"{field_name} must be a JSON object.")
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise CheckerQueueError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise CheckerQueueError(f"{field_name} must not be blank.")
    return normalized


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name)


def _require_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise CheckerQueueError(f"{field_name} must be an ISO8601 string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CheckerQueueError(f"{field_name} must be an ISO8601 string.") from exc
    return _validate_timestamp(parsed, field_name=field_name)


def _read_work_item(path: Path) -> CheckerWorkItem:
    return CheckerWorkItem.from_json_payload(json.loads(path.read_text(encoding="utf-8-sig")))


def _read_completion(path: Path) -> CheckerQueueCompletion:
    payload = _require_dict(json.loads(path.read_text(encoding="utf-8-sig")), "CheckerQueueCompletion")
    return CheckerQueueCompletion(
        queue_name=_require_text(payload.get("queue_name"), "queue_name"),
        work_item_id=_require_text(payload.get("work_item_id"), "work_item_id"),
        idempotency_key=_require_text(payload.get("idempotency_key"), "idempotency_key"),
        decision_record_ref=_require_text(
            payload.get("decision_record_ref"),
            "decision_record_ref",
        ),
        completed_at=_require_datetime(payload.get("completed_at"), "completed_at"),
        completion_path=path,
    )


def _read_failure(path: Path) -> CheckerQueueFailure:
    payload = _require_dict(json.loads(path.read_text(encoding="utf-8-sig")), "CheckerQueueFailure")
    return CheckerQueueFailure(
        queue_name=_require_text(payload.get("queue_name"), "queue_name"),
        work_item_id=_require_text(payload.get("work_item_id"), "work_item_id"),
        idempotency_key=_require_text(payload.get("idempotency_key"), "idempotency_key"),
        reason=_require_text(payload.get("reason"), "reason"),
        error=_require_text(payload.get("error"), "error"),
        recorded_at=_require_datetime(payload.get("recorded_at"), "recorded_at"),
        failure_path=path,
    )


__all__ = [
    "CheckerQueueClaim",
    "CheckerQueueCompletion",
    "CheckerQueueDeadLetter",
    "CheckerQueueEnqueueReceipt",
    "CheckerQueueError",
    "CheckerQueueFailure",
    "CheckerWorkItem",
    "FileBackedCheckerWorkQueue",
]
