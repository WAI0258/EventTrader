"""Durable reflection completion outbox for runtime-owned visibility updates."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from event_trader.reflection.review_loop import ReflectionHeartbeatReceipt

type ReflectionCompletionOutboxEntryKind = Literal["completed", "failed"]


class ReflectionCompletionOutboxError(ValueError):
    """Raised when reflection completion outbox payloads or wiring are invalid."""


@dataclass(frozen=True, slots=True)
class ReflectionCompletionOutboxEntry:
    """Durable reflection result awaiting runtime-owned visibility update."""

    outbox_name: str
    entry_kind: ReflectionCompletionOutboxEntryKind
    work_item_id: str
    idempotency_key: str
    cycle_number: int
    checked_at: datetime
    recorded_at: datetime
    entry_path: Path
    receipt: ReflectionHeartbeatReceipt | None = None
    failure_reason: str | None = None
    failure_error: str | None = None
    failure_record_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "outbox_name", _require_text(self.outbox_name, "outbox_name"))
        if self.entry_kind not in {"completed", "failed"}:
            raise ReflectionCompletionOutboxError("entry_kind must be completed or failed.")
        object.__setattr__(self, "work_item_id", _require_text(self.work_item_id, "work_item_id"))
        object.__setattr__(
            self,
            "idempotency_key",
            _require_text(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(
            self,
            "cycle_number",
            _require_positive_int(self.cycle_number, "cycle_number"),
        )
        object.__setattr__(self, "checked_at", _require_datetime(self.checked_at, "checked_at"))
        object.__setattr__(self, "recorded_at", _require_datetime(self.recorded_at, "recorded_at"))
        if not isinstance(self.entry_path, Path):
            raise ReflectionCompletionOutboxError("entry_path must be a Path.")
        if self.entry_kind == "completed":
            if not isinstance(self.receipt, ReflectionHeartbeatReceipt):
                raise ReflectionCompletionOutboxError(
                    "completed entry must carry ReflectionHeartbeatReceipt."
                )
            if self.receipt.heartbeat_number != self.cycle_number:
                raise ReflectionCompletionOutboxError(
                    "receipt heartbeat_number must match cycle_number."
                )
            if self.receipt.checked_at != self.checked_at:
                raise ReflectionCompletionOutboxError("receipt checked_at must match checked_at.")
            if any(
                value is not None
                for value in (
                    self.failure_reason,
                    self.failure_error,
                    self.failure_record_ref,
                )
            ):
                raise ReflectionCompletionOutboxError(
                    "completed entry must not carry failure fields."
                )
            return
        if self.receipt is not None:
            raise ReflectionCompletionOutboxError("failed entry must not carry receipt.")
        object.__setattr__(
            self,
            "failure_reason",
            _require_text(self.failure_reason, "failure_reason"),
        )
        object.__setattr__(
            self,
            "failure_error",
            _require_text(self.failure_error, "failure_error"),
        )
        object.__setattr__(
            self,
            "failure_record_ref",
            _require_text(self.failure_record_ref, "failure_record_ref"),
        )

    def to_json_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "outbox_name": self.outbox_name,
            "entry_kind": self.entry_kind,
            "work_item_id": self.work_item_id,
            "idempotency_key": self.idempotency_key,
            "cycle_number": self.cycle_number,
            "checked_at": self.checked_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
        }
        if self.entry_kind == "completed":
            payload["receipt"] = _serialize_value(self.receipt)
        else:
            payload["failure_reason"] = self.failure_reason
            payload["failure_error"] = self.failure_error
            payload["failure_record_ref"] = self.failure_record_ref
        return payload


class FileBackedReflectionCompletionOutbox:
    """Persist reflection results until the runtime graph makes them visible."""

    def __init__(self, *, root: Path, outbox_name: str = "reflection_completions") -> None:
        if not isinstance(root, Path):
            raise ReflectionCompletionOutboxError("root must be a Path.")
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
        cycle_number: int,
        checked_at: datetime,
        recorded_at: datetime,
        receipt: ReflectionHeartbeatReceipt,
    ) -> ReflectionCompletionOutboxEntry:
        return self._record(
            entry_kind="completed",
            work_item_id=work_item_id,
            idempotency_key=idempotency_key,
            cycle_number=cycle_number,
            checked_at=checked_at,
            recorded_at=recorded_at,
            receipt=receipt,
            failure_reason=None,
            failure_error=None,
            failure_record_ref=None,
        )

    def record_failed(
        self,
        *,
        work_item_id: str,
        idempotency_key: str,
        cycle_number: int,
        checked_at: datetime,
        recorded_at: datetime,
        failure_reason: str,
        failure_error: str,
        failure_record_ref: str,
    ) -> ReflectionCompletionOutboxEntry:
        return self._record(
            entry_kind="failed",
            work_item_id=work_item_id,
            idempotency_key=idempotency_key,
            cycle_number=cycle_number,
            checked_at=checked_at,
            recorded_at=recorded_at,
            receipt=None,
            failure_reason=failure_reason,
            failure_error=failure_error,
            failure_record_ref=failure_record_ref,
        )

    def load_pending(self) -> tuple[ReflectionCompletionOutboxEntry, ...]:
        return tuple(_read_outbox_entry(path) for path in sorted(self._pending_root.glob("*.json")))

    def load_published(self) -> tuple[ReflectionCompletionOutboxEntry, ...]:
        return tuple(
            _read_outbox_entry(path) for path in sorted(self._published_root.glob("*.json"))
        )

    def find_pending(self, idempotency_key: str) -> ReflectionCompletionOutboxEntry | None:
        normalized_key = _require_text(idempotency_key, "idempotency_key")
        path = self._pending_path(normalized_key)
        return _read_outbox_entry(path) if path.exists() else None

    def find_published(self, idempotency_key: str) -> ReflectionCompletionOutboxEntry | None:
        normalized_key = _require_text(idempotency_key, "idempotency_key")
        path = self._published_path(normalized_key)
        return _read_outbox_entry(path) if path.exists() else None

    def mark_published(
        self,
        entry: ReflectionCompletionOutboxEntry,
    ) -> ReflectionCompletionOutboxEntry:
        if not isinstance(entry, ReflectionCompletionOutboxEntry):
            raise ReflectionCompletionOutboxError(
                "entry must be a ReflectionCompletionOutboxEntry."
            )
        if entry.outbox_name != self._outbox_name:
            raise ReflectionCompletionOutboxError(
                "entry outbox_name does not match this outbox."
            )
        pending_path = self._pending_path(entry.idempotency_key)
        published_path = self._published_path(entry.idempotency_key)
        if published_path.exists():
            return _read_outbox_entry(published_path)
        if not pending_path.exists():
            raise ReflectionCompletionOutboxError(
                f"pending outbox entry is missing for {entry.idempotency_key}."
            )
        pending_path.replace(published_path)
        return _read_outbox_entry(published_path)

    def _record(
        self,
        *,
        entry_kind: ReflectionCompletionOutboxEntryKind,
        work_item_id: str,
        idempotency_key: str,
        cycle_number: int,
        checked_at: datetime,
        recorded_at: datetime,
        receipt: ReflectionHeartbeatReceipt | None,
        failure_reason: str | None,
        failure_error: str | None,
        failure_record_ref: str | None,
    ) -> ReflectionCompletionOutboxEntry:
        normalized_work_item_id = _require_text(work_item_id, "work_item_id")
        normalized_idempotency_key = _require_text(idempotency_key, "idempotency_key")
        pending_path = self._pending_path(normalized_idempotency_key)
        published_path = self._published_path(normalized_idempotency_key)
        if published_path.exists():
            return _read_outbox_entry(published_path)
        if pending_path.exists():
            return _read_outbox_entry(pending_path)
        entry = ReflectionCompletionOutboxEntry(
            outbox_name=self._outbox_name,
            entry_kind=entry_kind,
            work_item_id=normalized_work_item_id,
            idempotency_key=normalized_idempotency_key,
            cycle_number=cycle_number,
            checked_at=checked_at,
            recorded_at=recorded_at,
            entry_path=pending_path,
            receipt=receipt,
            failure_reason=failure_reason,
            failure_error=failure_error,
            failure_record_ref=failure_record_ref,
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


def _read_outbox_entry(path: Path) -> ReflectionCompletionOutboxEntry:
    payload = _require_dict(json.loads(path.read_text(encoding="utf-8")), "outbox entry")
    entry_kind = _require_text(payload.get("entry_kind"), "entry_kind")
    cycle_number = _require_positive_int(payload.get("cycle_number"), "cycle_number")
    checked_at = _require_datetime(
        _require_text(payload.get("checked_at"), "checked_at"),
        "checked_at",
    )
    recorded_at = _require_datetime(
        _require_text(payload.get("recorded_at"), "recorded_at"),
        "recorded_at",
    )
    if entry_kind == "completed":
        receipt = _deserialize_value(
            payload.get("receipt"),
            ReflectionHeartbeatReceipt,
            field_name="receipt",
        )
        return ReflectionCompletionOutboxEntry(
            outbox_name=_require_text(payload.get("outbox_name"), "outbox_name"),
            entry_kind=entry_kind,
            work_item_id=_require_text(payload.get("work_item_id"), "work_item_id"),
            idempotency_key=_require_text(payload.get("idempotency_key"), "idempotency_key"),
            cycle_number=cycle_number,
            checked_at=checked_at,
            recorded_at=recorded_at,
            entry_path=path,
            receipt=receipt,
        )
    if entry_kind == "failed":
        return ReflectionCompletionOutboxEntry(
            outbox_name=_require_text(payload.get("outbox_name"), "outbox_name"),
            entry_kind=entry_kind,
            work_item_id=_require_text(payload.get("work_item_id"), "work_item_id"),
            idempotency_key=_require_text(payload.get("idempotency_key"), "idempotency_key"),
            cycle_number=cycle_number,
            checked_at=checked_at,
            recorded_at=recorded_at,
            entry_path=path,
            failure_reason=_require_text(payload.get("failure_reason"), "failure_reason"),
            failure_error=_require_text(payload.get("failure_error"), "failure_error"),
            failure_record_ref=_require_text(
                payload.get("failure_record_ref"),
                "failure_record_ref",
            ),
        )
    raise ReflectionCompletionOutboxError("entry_kind must be completed or failed.")


def _serialize_value(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, Path):
        return {"__path__": str(value)}
    if isinstance(value, tuple):
        return [_serialize_value(item) for item in value]
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if is_dataclass(value):
        return {
            field.name: _serialize_value(getattr(value, field.name))
            for field in fields(value)
        }
    raise ReflectionCompletionOutboxError(
        f"cannot serialize reflection outbox value of type {type(value)!r}."
    )


def _deserialize_value(value: object, expected_type: object, *, field_name: str) -> object:
    expected_type = _unwrap_alias(expected_type)
    origin = get_origin(expected_type)
    if origin is Literal:
        allowed = get_args(expected_type)
        if value not in allowed:
            raise ReflectionCompletionOutboxError(
                f"{field_name} must be one of {allowed!r}."
            )
        return value
    if origin is not None:
        if origin in {list, tuple}:
            if not isinstance(value, list):
                raise ReflectionCompletionOutboxError(f"{field_name} must be a JSON array.")
            item_type = get_args(expected_type)[0]
            return tuple(
                _deserialize_value(item, item_type, field_name=f"{field_name}[]")
                for item in value
            )
        if origin in {dict}:
            if not isinstance(value, dict):
                raise ReflectionCompletionOutboxError(f"{field_name} must be a JSON object.")
            return {str(key): item for key, item in value.items()}
        if origin in {UnionType, Union}:
            for option in get_args(expected_type):
                if option is type(None) and value is None:
                    return None
                if option is type(None):
                    continue
                try:
                    return _deserialize_value(value, option, field_name=field_name)
                except ReflectionCompletionOutboxError:
                    continue
            raise ReflectionCompletionOutboxError(
                f"{field_name} could not be decoded for {expected_type!r}."
            )
    if expected_type in {Any, object}:
        if isinstance(value, dict):
            if "__datetime__" in value:
                return _require_datetime(value["__datetime__"], field_name)
            if "__path__" in value:
                return Path(_require_text(value["__path__"], field_name))
            return {str(key): item for key, item in value.items()}
        return value
    if expected_type is datetime:
        if not isinstance(value, dict) or "__datetime__" not in value:
            raise ReflectionCompletionOutboxError(f"{field_name} must encode a datetime.")
        return _require_datetime(value["__datetime__"], field_name)
    if expected_type is Path:
        if not isinstance(value, dict) or "__path__" not in value:
            raise ReflectionCompletionOutboxError(f"{field_name} must encode a path.")
        return Path(_require_text(value["__path__"], field_name))
    if expected_type is str:
        return _require_text(value, field_name)
    if expected_type is int:
        if field_name.endswith("cycle_number"):
            return _require_positive_int(value, field_name)
        return _require_int(value, field_name)
    if expected_type is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ReflectionCompletionOutboxError(f"{field_name} must be a number.")
        return float(value)
    if expected_type is bool:
        if not isinstance(value, bool):
            raise ReflectionCompletionOutboxError(f"{field_name} must be a bool.")
        return value
    if isinstance(expected_type, type) and is_dataclass(expected_type):
        if not isinstance(value, dict):
            raise ReflectionCompletionOutboxError(f"{field_name} must be a JSON object.")
        type_hints = get_type_hints(expected_type)
        kwargs: dict[str, object] = {}
        for field in fields(expected_type):
            if field.name not in value:
                raise ReflectionCompletionOutboxError(
                    f"{field_name}.{field.name} is missing from serialized payload."
                )
            kwargs[field.name] = _deserialize_value(
                value[field.name],
                type_hints[field.name],
                field_name=f"{field_name}.{field.name}",
            )
        return expected_type(**kwargs)
    raise ReflectionCompletionOutboxError(
        f"unsupported reflection outbox decode type {expected_type!r} for {field_name}."
    )


def _unwrap_alias(expected_type: object) -> object:
    alias_value = getattr(expected_type, "__value__", None)
    if alias_value is None:
        return expected_type
    return alias_value


def _idempotency_digest(value: str) -> str:
    return sha256(_require_text(value, "idempotency_key").encode("utf-8")).hexdigest()


def _require_dict(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReflectionCompletionOutboxError(f"{field_name} must be a JSON object.")
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ReflectionCompletionOutboxError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ReflectionCompletionOutboxError(f"{field_name} must not be blank.")
    return normalized


def _require_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReflectionCompletionOutboxError(f"{field_name} must be an integer.")
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    normalized = _require_int(value, field_name)
    if normalized < 1:
        raise ReflectionCompletionOutboxError(f"{field_name} must be >= 1.")
    return normalized


def _require_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ReflectionCompletionOutboxError(f"{field_name} must be an ISO8601 string.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReflectionCompletionOutboxError(
            f"{field_name} must be an ISO8601 string."
        ) from exc


__all__ = [
    "FileBackedReflectionCompletionOutbox",
    "ReflectionCompletionOutboxEntry",
    "ReflectionCompletionOutboxEntryKind",
    "ReflectionCompletionOutboxError",
]
