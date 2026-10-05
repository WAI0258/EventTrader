"""Append-only stores for paper execution intents and records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from event_trader.contracts._validators import validate_target_key
from event_trader.execution.contracts import (
    ExecutionIntent,
    ExecutionRecord,
    execution_contract_hash,
    parse_execution_intent,
    parse_execution_record,
)
from event_trader.storage import WorkspaceLayout

_INTENTS_ROOT = Path("execution") / "intents"
_RECORDS_ROOT = Path("execution") / "records"


class ExecutionStoreError(ValueError):
    """Raised when execution persistence is invalid or conflicting."""


@dataclass(frozen=True, slots=True)
class PersistedExecutionIntent:
    record: ExecutionIntent
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedExecutionRecord:
    record: ExecutionRecord
    path: Path
    line_number: int
    record_hash: str


class ExecutionIntentStore:
    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def append(self, intent: ExecutionIntent) -> Path:
        if not isinstance(intent, ExecutionIntent):
            raise ExecutionStoreError("intent must be an ExecutionIntent instance.")
        path = self.path_for(intent.target_key, intent.business_at)
        record_hash = execution_contract_hash(intent.to_json_payload())
        for persisted in self.read_records(
            target_key=intent.target_key,
            year_month=intent.business_at.strftime("%Y-%m"),
        ):
            if persisted.record.intent_id != intent.intent_id:
                continue
            if persisted.record_hash == record_hash:
                return path
            raise ExecutionStoreError("duplicate execution intent id has different payload.")
        _append_json_line(path, intent.to_json_payload())
        return path

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedExecutionIntent, ...]:
        return tuple(
            PersistedExecutionIntent(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=execution_contract_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_intents(
                paths=_paths(
                    root=self._layout.runtime_root / _INTENTS_ROOT,
                    target_key=target_key,
                    year_month=year_month,
                ),
                target_key=target_key,
            )
        )

    def path_for(self, target_key: str, business_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _INTENTS_ROOT,
            target_key=target_key,
            business_at=business_at,
        )


class ExecutionRecordStore:
    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def append(self, record: ExecutionRecord) -> Path:
        if not isinstance(record, ExecutionRecord):
            raise ExecutionStoreError("record must be an ExecutionRecord instance.")
        path = self.path_for(record.target_key, record.business_at)
        record_hash = execution_contract_hash(record.to_json_payload())
        for persisted in self.read_records(
            target_key=record.target_key,
            year_month=record.business_at.strftime("%Y-%m"),
        ):
            if persisted.record.execution_record_id != record.execution_record_id:
                continue
            if persisted.record_hash == record_hash:
                return path
            raise ExecutionStoreError("duplicate execution record id has different payload.")
        _append_json_line(path, record.to_json_payload())
        return path

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedExecutionRecord, ...]:
        return tuple(
            PersistedExecutionRecord(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=execution_contract_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_records(
                paths=_paths(
                    root=self._layout.runtime_root / _RECORDS_ROOT,
                    target_key=target_key,
                    year_month=year_month,
                ),
                target_key=target_key,
            )
        )

    def read_all_records(self) -> tuple[PersistedExecutionRecord, ...]:
        root = self._layout.runtime_root / _RECORDS_ROOT
        if not root.exists():
            return ()
        records: list[PersistedExecutionRecord] = []
        for target_root in sorted(path for path in root.iterdir() if path.is_dir()):
            records.extend(self.read_records(target_key=target_root.name))
        return tuple(
            sorted(
                records,
                key=lambda item: (
                    item.record.business_at,
                    item.path.as_posix(),
                    item.line_number,
                ),
            )
        )

    def path_for(self, target_key: str, business_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _RECORDS_ROOT,
            target_key=target_key,
            business_at=business_at,
        )


def _validate_layout(layout: WorkspaceLayout) -> None:
    if not isinstance(layout, WorkspaceLayout):
        raise ExecutionStoreError("layout must be a WorkspaceLayout instance.")


def _monthly_path(*, root: Path, target_key: str, business_at: datetime) -> Path:
    normalized_target = validate_target_key(target_key, error_type=ExecutionStoreError)
    if not isinstance(business_at, datetime):
        raise ExecutionStoreError("business_at must be a datetime.")
    return (root / normalized_target / f"{business_at.strftime('%Y-%m')}.jsonl").resolve(
        strict=False
    )


def _paths(*, root: Path, target_key: str, year_month: str | None) -> tuple[Path, ...]:
    normalized_target = validate_target_key(target_key, error_type=ExecutionStoreError)
    target_root = root / normalized_target
    if year_month is not None:
        if not isinstance(year_month, str) or len(year_month) != 7:
            raise ExecutionStoreError("year_month must be YYYY-MM.")
        return ((target_root / f"{year_month}.jsonl").resolve(strict=False),)
    return tuple(sorted(target_root.glob("*.jsonl")))


def _read_intents(
    *,
    paths: tuple[Path, ...],
    target_key: str,
) -> tuple[tuple[ExecutionIntent, Path, int], ...]:
    return tuple(
        (parse_execution_intent(payload), path, line_number)
        for payload, path, line_number in _read_payloads(
            paths=paths,
            target_key=target_key,
            label="execution intent",
        )
    )


def _read_records(
    *,
    paths: tuple[Path, ...],
    target_key: str,
) -> tuple[tuple[ExecutionRecord, Path, int], ...]:
    return tuple(
        (
            parse_execution_record(payload),
            path,
            line_number,
        )
        for payload, path, line_number in _read_payloads(
            paths=paths,
            target_key=target_key,
            label="execution record",
        )
    )


def _read_payloads(
    *,
    paths: tuple[Path, ...],
    target_key: str,
    label: str,
) -> tuple[tuple[Mapping[str, object], Path, int], ...]:
    normalized_target = validate_target_key(target_key, error_type=ExecutionStoreError)
    rows: list[tuple[Mapping[str, object], Path, int]] = []
    for path in paths:
        if not path.exists():
            continue
        if not path.is_file():
            raise ExecutionStoreError(f"{label} path must be a file: {path}")
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    raise ExecutionStoreError(
                        f"{label} line {line_number} is invalid JSON."
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise ExecutionStoreError(f"{label} line {line_number} must be an object.")
                if payload.get("target_key") != normalized_target:
                    raise ExecutionStoreError(f"{label} shard contains mixed target_key records.")
                rows.append((payload, path, line_number))
    return tuple(rows)


def _append_json_line(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(dict(payload), ensure_ascii=False))
            handle.write("\n")
    except OSError as exc:
        raise ExecutionStoreError(f"failed to append execution record: {path}") from exc


__all__ = [
    "ExecutionIntentStore",
    "ExecutionRecordStore",
    "ExecutionStoreError",
    "PersistedExecutionIntent",
    "PersistedExecutionRecord",
]
