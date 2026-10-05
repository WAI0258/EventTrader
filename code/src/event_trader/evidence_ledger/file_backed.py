"""Thin file-backed evidence-ledger implementation.

The module writes canonical append-only evidence truth under the workspace
ledger root.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from event_trader.contracts import (
    EvidenceContractError,
    EvidenceLedgerRecord,
    ledger_path_for_record,
)
from event_trader.contracts._validators import (
    validate_event_id,
    validate_target_key,
    validate_timestamp,
)
from event_trader.storage import WorkspaceLayout


class FileBackedEvidenceLedgerError(ValueError):
    """Raised when file-backed ledger resolution or writes fail."""


@dataclass(frozen=True, slots=True)
class TargetLedgerLayout:
    """Resolved target-scoped paths under the canonical ledger root."""

    target_key: str
    target_root: Path

    def day_partition(self, event_day: date | datetime) -> Path:
        """Resolve the append-only daily JSONL partition for a target."""
        resolved_day = _normalize_event_day(event_day)
        return (
            self.target_root / f"{resolved_day.isoformat()}.jsonl"
        ).resolve(strict=False)


@dataclass(frozen=True, slots=True)
class FileBackedEvidenceLedger:
    """Thin concrete wrapper for canonical file-backed evidence truth."""

    layout: WorkspaceLayout

    def __post_init__(self) -> None:
        if not isinstance(self.layout, WorkspaceLayout):
            raise FileBackedEvidenceLedgerError(
                "layout must be a WorkspaceLayout instance."
            )

    @property
    def root(self) -> Path:
        """Return the canonical evidence-ledger truth root."""
        return self.layout.ledger_root

    def target(self, target_key: str) -> TargetLedgerLayout:
        """Resolve a target-scoped layout without creating it."""
        return build_target_ledger_layout(self.layout, target_key)

    def initialize_target(self, target_key: str) -> TargetLedgerLayout:
        """Create the target-scoped ledger directory on demand."""
        return initialize_target_ledger_layout(self.layout, target_key)

    def path_for_record(self, record: EvidenceLedgerRecord) -> Path:
        """Resolve the canonical append-only partition path."""
        _validate_record(record)
        return ledger_path_for_record(self.layout, record)

    def append(self, record: EvidenceLedgerRecord) -> str:
        """Append one durable evidence record and return its event id."""
        _validate_record(record)
        self.initialize_target(record.target_key)
        partition_path = self.path_for_record(record)
        payload = _serialize_record(record)
        existing_record = self._read_optional(record.event_id)
        if existing_record is not None:
            if existing_record != record:
                raise FileBackedEvidenceLedgerError(
                    "Cannot append evidence ledger record with an existing event_id "
                    f"and different content: {record.event_id}."
                )
            return record.event_id

        try:
            with partition_path.open("ab") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise FileBackedEvidenceLedgerError(
                "Failed to append evidence ledger record "
                f"{record.event_id} to {partition_path}: {exc}"
            ) from exc

        return record.event_id

    def read(self, event_id: str) -> EvidenceLedgerRecord:
        """Read one durable evidence record from canonical ledger truth."""
        validated_event_id = _validate_event_id(event_id)

        for record in self._iter_records():
            if record.event_id == validated_event_id:
                return record

        raise FileBackedEvidenceLedgerError(
            "No evidence ledger record found for event_id "
            f"{validated_event_id} under canonical ledger root {self.root}."
        )

    def _read_optional(self, event_id: str) -> EvidenceLedgerRecord | None:
        validated_event_id = _validate_event_id(event_id)
        for record in self._iter_records():
            if record.event_id == validated_event_id:
                return record
        return None

    def read_many(self, event_ids: list[str]) -> list[EvidenceLedgerRecord]:
        """Read multiple durable evidence records in requested order."""
        validated_event_ids = _validate_event_ids(event_ids)
        if not validated_event_ids:
            return []

        remaining_event_ids = set(validated_event_ids)
        found_by_event_id: dict[str, EvidenceLedgerRecord] = {}

        for record in self._iter_records():
            event_id = record.event_id
            if event_id not in remaining_event_ids:
                continue

            found_by_event_id[event_id] = record
            remaining_event_ids.remove(event_id)
            if not remaining_event_ids:
                break

        if remaining_event_ids:
            missing_ids = ", ".join(sorted(remaining_event_ids))
            raise FileBackedEvidenceLedgerError(
                "No evidence ledger records found for event_ids "
                f"[{missing_ids}] under canonical ledger root {self.root}."
            )

        return [found_by_event_id[event_id] for event_id in validated_event_ids]

    def read_window(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        target_keys: list[str] | None = None,
        exclude_event_ids: list[str] | None = None,
    ) -> list[EvidenceLedgerRecord]:
        """Read later ledger evidence inside one bounded review window."""
        validated_start_at, validated_end_at = _validate_window_bounds(
            start_at=start_at,
            end_at=end_at,
        )
        validated_target_keys = _validate_target_keys(target_keys)
        excluded_event_ids = set(_validate_optional_event_ids(exclude_event_ids))

        records = [
            record
            for record in self._iter_records()
            if _record_in_window(
                record,
                start_at=validated_start_at,
                end_at=validated_end_at,
                target_keys=validated_target_keys,
                excluded_event_ids=excluded_event_ids,
            )
        ]
        records.sort(key=lambda record: (record.ts_event, record.ts_init, record.event_id))
        return records

    def _iter_records(self) -> list[EvidenceLedgerRecord]:
        records: list[EvidenceLedgerRecord] = []
        for partition_path in _canonical_partition_paths(self.root):
            records.extend(_read_partition_records(partition_path))
        return records


def read_evidence(
    event_ids: list[str],
    *,
    ledger: FileBackedEvidenceLedger,
) -> list[EvidenceLedgerRecord]:
    """Business-shaped read surface for checker, analysis, and reflection."""
    if not isinstance(ledger, FileBackedEvidenceLedger):
        raise FileBackedEvidenceLedgerError(
            "ledger must be a FileBackedEvidenceLedger instance."
        )
    return ledger.read_many(event_ids)


def read_evidence_window(
    *,
    start_at: datetime,
    end_at: datetime,
    ledger: FileBackedEvidenceLedger,
    target_keys: list[str] | None = None,
    exclude_event_ids: list[str] | None = None,
) -> list[EvidenceLedgerRecord]:
    """Read later ledger evidence inside one bounded review window."""
    if not isinstance(ledger, FileBackedEvidenceLedger):
        raise FileBackedEvidenceLedgerError(
            "ledger must be a FileBackedEvidenceLedger instance."
        )
    return ledger.read_window(
        start_at=start_at,
        end_at=end_at,
        target_keys=target_keys,
        exclude_event_ids=exclude_event_ids,
    )


def build_target_ledger_layout(
    layout: WorkspaceLayout,
    target_key: str,
) -> TargetLedgerLayout:
    """Build the canonical target-scoped ledger layout without creating it."""
    if not isinstance(layout, WorkspaceLayout):
        raise FileBackedEvidenceLedgerError(
            "layout must be a WorkspaceLayout instance."
        )

    validated_target_key = validate_target_key(
        target_key,
        error_type=FileBackedEvidenceLedgerError,
    )
    target_root = (
        layout.ledger_root / validated_target_key
    ).resolve(strict=False)
    return TargetLedgerLayout(
        target_key=validated_target_key,
        target_root=target_root,
    )


def initialize_target_ledger_layout(
    layout: WorkspaceLayout,
    target_key: str,
) -> TargetLedgerLayout:
    """Create the target-scoped ledger directory on demand."""
    target_layout = build_target_ledger_layout(layout, target_key)

    try:
        _ensure_directory(target_layout.target_root)
    except FileBackedEvidenceLedgerError:
        raise
    except OSError as exc:
        raise FileBackedEvidenceLedgerError(
            "Failed to initialize target evidence-ledger layout under "
            f"{target_layout.target_root}: {exc}"
        ) from exc

    return target_layout


def _validate_record(record: EvidenceLedgerRecord) -> None:
    if not isinstance(record, EvidenceLedgerRecord):
        raise FileBackedEvidenceLedgerError(
            "record must be an EvidenceLedgerRecord instance."
        )


def _validate_event_id(event_id: str) -> str:
    return validate_event_id(
        event_id,
        error_type=FileBackedEvidenceLedgerError,
    )


def _validate_event_ids(event_ids: list[str]) -> list[str]:
    if not isinstance(event_ids, list):
        raise FileBackedEvidenceLedgerError(
            "event_ids must be provided as a list of event ids."
        )
    return [_validate_event_id(event_id) for event_id in event_ids]


def _validate_optional_event_ids(event_ids: list[str] | None) -> tuple[str, ...]:
    if event_ids is None:
        return ()
    return tuple(_validate_event_ids(event_ids))


def _validate_target_keys(target_keys: list[str] | None) -> tuple[str, ...] | None:
    if target_keys is None:
        return None
    if not isinstance(target_keys, list):
        raise FileBackedEvidenceLedgerError(
            "target_keys must be provided as a list of target keys when set."
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for target_key in target_keys:
        validated_target_key = validate_target_key(
            target_key,
            error_type=FileBackedEvidenceLedgerError,
        )
        if validated_target_key in seen:
            raise FileBackedEvidenceLedgerError(
                "target_keys must not contain duplicates."
            )
        seen.add(validated_target_key)
        normalized.append(validated_target_key)
    return tuple(normalized)


def _validate_window_bounds(*, start_at: datetime, end_at: datetime) -> tuple[datetime, datetime]:
    validated_start_at = validate_timestamp(
        start_at,
        field_name="start_at",
        error_type=FileBackedEvidenceLedgerError,
    )
    validated_end_at = validate_timestamp(
        end_at,
        field_name="end_at",
        error_type=FileBackedEvidenceLedgerError,
    )
    if validated_end_at <= validated_start_at:
        raise FileBackedEvidenceLedgerError(
            "end_at must be later than start_at for evidence window reads."
        )
    return validated_start_at, validated_end_at


def _record_in_window(
    record: EvidenceLedgerRecord,
    *,
    start_at: datetime,
    end_at: datetime,
    target_keys: tuple[str, ...] | None,
    excluded_event_ids: set[str],
) -> bool:
    if record.event_id in excluded_event_ids:
        return False
    if target_keys is not None and record.target_key not in target_keys:
        return False
    return start_at < record.ts_event <= end_at


def _normalize_event_day(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return validate_timestamp(
            value,
            field_name="event_day",
            error_type=FileBackedEvidenceLedgerError,
        ).date()
    if isinstance(value, date):
        return value
    raise FileBackedEvidenceLedgerError(
        "event_day must be a date or timezone-aware datetime."
    )


def _serialize_record(record: EvidenceLedgerRecord) -> bytes:
    payload = {
        "event_id": record.event_id,
        "target_key": record.target_key,
        "source_ref": record.source_ref,
        "title": record.title,
        "content": record.content,
        "labels": list(record.labels),
        "ts_source": record.ts_source.isoformat(),
        "ts_event": record.ts_event.isoformat(),
        "ts_init": record.ts_init.isoformat(),
    }
    return f"{json.dumps(payload, ensure_ascii=False)}\n".encode()


def _canonical_partition_paths(root: Path) -> tuple[Path, ...]:
    if root.exists() and not root.is_dir():
        raise FileBackedEvidenceLedgerError(
            "Expected the canonical evidence-ledger root to be a directory, "
            f"but found a file at: {root.resolve(strict=False)}"
        )
    if not root.exists():
        return ()
    return tuple(
        sorted(
            path.resolve(strict=False)
            for path in root.glob("*/*.jsonl")
            if path.is_file()
        )
    )


def _read_partition_records(partition_path: Path) -> list[EvidenceLedgerRecord]:
    try:
        lines = partition_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FileBackedEvidenceLedgerError(
            "Failed to read evidence ledger partition "
            f"{partition_path}: {exc}"
        ) from exc

    records: list[EvidenceLedgerRecord] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise FileBackedEvidenceLedgerError(
                "Encountered a blank line in canonical evidence ledger partition "
                f"{partition_path}:{line_number}."
            )
        records.append(
            _deserialize_record(
                line,
                partition_path=partition_path,
                line_number=line_number,
            )
        )
    return records


def _deserialize_record(
    raw_line: str,
    *,
    partition_path: Path,
    line_number: int,
) -> EvidenceLedgerRecord:
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise FileBackedEvidenceLedgerError(
            "Failed to decode canonical evidence ledger JSON at "
            f"{partition_path}:{line_number}: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise FileBackedEvidenceLedgerError(
            "Expected a JSON object in canonical evidence ledger partition at "
            f"{partition_path}:{line_number}."
        )

    try:
        return EvidenceLedgerRecord(
            event_id=payload["event_id"],
            target_key=payload["target_key"],
            source_ref=payload["source_ref"],
            title=payload["title"],
            content=payload["content"],
            labels=list(payload["labels"]),
            ts_source=_parse_timestamp(
                payload["ts_source"],
                field_name="ts_source",
                partition_path=partition_path,
                line_number=line_number,
            ),
            ts_event=_parse_timestamp(
                payload["ts_event"],
                field_name="ts_event",
                partition_path=partition_path,
                line_number=line_number,
            ),
            ts_init=_parse_timestamp(
                payload["ts_init"],
                field_name="ts_init",
                partition_path=partition_path,
                line_number=line_number,
            ),
        )
    except KeyError as exc:
        raise FileBackedEvidenceLedgerError(
            "Missing required evidence ledger field "
            f"{exc.args[0]!r} at {partition_path}:{line_number}."
        ) from exc
    except (EvidenceContractError, TypeError, ValueError) as exc:
        raise FileBackedEvidenceLedgerError(
            "Invalid evidence ledger record encountered at "
            f"{partition_path}:{line_number}: {exc}"
        ) from exc


def _parse_timestamp(
    value: object,
    *,
    field_name: str,
    partition_path: Path,
    line_number: int,
) -> datetime:
    if not isinstance(value, str):
        raise FileBackedEvidenceLedgerError(
            f"{field_name} must be an ISO-8601 string at {partition_path}:{line_number}."
        )

    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise FileBackedEvidenceLedgerError(
            f"{field_name} must be valid ISO-8601 data at {partition_path}:{line_number}: {exc}"
        ) from exc


def _ensure_directory(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise FileBackedEvidenceLedgerError(
                "Expected a directory in file-backed evidence-ledger layout, "
                f"but found a file at: {path.resolve(strict=False)}"
            )
        return

    path.mkdir()


__all__ = [
    "FileBackedEvidenceLedger",
    "FileBackedEvidenceLedgerError",
    "TargetLedgerLayout",
    "build_target_ledger_layout",
    "initialize_target_ledger_layout",
    "read_evidence",
    "read_evidence_window",
]
