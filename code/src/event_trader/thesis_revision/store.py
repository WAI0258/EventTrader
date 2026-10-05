"""Append-only store for thesis revision audit records."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from event_trader.contracts._validators import validate_target_key
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.contracts import (
    ThesisRevision,
    ThesisRevisionContractError,
    parse_thesis_revision,
    thesis_revision_record_hash,
)

_ROOT = Path("thesis_revisions")


class ThesisRevisionStoreError(ValueError):
    """Raised when thesis revision persistence is invalid or conflicting."""


@dataclass(frozen=True, slots=True)
class PersistedThesisRevision:
    record: ThesisRevision
    path: Path
    line_number: int
    record_hash: str


class ThesisRevisionStore:
    """Append and read thesis revisions by target/month."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise ThesisRevisionStoreError("layout must be a WorkspaceLayout instance.")
        self._layout = layout

    def append(self, revision: ThesisRevision) -> Path:
        if not isinstance(revision, ThesisRevision):
            raise ThesisRevisionStoreError("revision must be a ThesisRevision instance.")
        path = self.path_for(revision.target_key, revision.business_at)
        record_hash = thesis_revision_record_hash(revision.to_json_payload())
        for persisted in self.read_records(target_key=revision.target_key):
            if persisted.record.revision_id != revision.revision_id:
                continue
            if persisted.record_hash == record_hash:
                return persisted.path
            raise ThesisRevisionStoreError(
                "duplicate revision_id has different payload."
            )
        _append_json_line(path, revision.to_json_payload())
        return path

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedThesisRevision, ...]:
        return tuple(
            PersistedThesisRevision(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=thesis_revision_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_records(
                paths=_paths(
                    root=self._layout.runtime_root / _ROOT,
                    target_key=target_key,
                    year_month=year_month,
                ),
                target_key=target_key,
            )
        )

    def read_latest(self, *, target_key: str) -> PersistedThesisRevision | None:
        ordered_records = sort_persisted_thesis_revisions(
            self.read_records(target_key=target_key)
        )
        if not ordered_records:
            return None
        return ordered_records[-1]

    def read_latest_at_or_before(
        self,
        *,
        target_key: str,
        business_at: datetime,
    ) -> PersistedThesisRevision | None:
        cutoff = _normalize_business_at(business_at)
        selected: PersistedThesisRevision | None = None
        for persisted in sort_persisted_thesis_revisions(
            self.read_records(target_key=target_key)
        ):
            if persisted.record.business_at > cutoff:
                break
            selected = persisted
        return selected

    def path_for(self, target_key: str, business_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _ROOT,
            target_key=target_key,
            business_at=business_at,
        )


def _read_records(
    *,
    paths: tuple[Path, ...],
    target_key: str,
) -> tuple[tuple[ThesisRevision, Path, int], ...]:
    records: list[tuple[ThesisRevision, Path, int]] = []
    for payload, path, line_number in _read_payloads(
        paths=paths,
        target_key=target_key,
        label="thesis revision",
    ):
        try:
            records.append((parse_thesis_revision(payload), path, line_number))
        except ThesisRevisionContractError as exc:
            raise ThesisRevisionStoreError(str(exc)) from exc
    return tuple(records)


def thesis_revision_order_key(
    persisted: PersistedThesisRevision,
) -> tuple[datetime, datetime, str, int]:
    return (
        persisted.record.business_at,
        persisted.record.committed_at,
        persisted.path.as_posix(),
        persisted.line_number,
    )


def sort_persisted_thesis_revisions(
    records: Iterable[PersistedThesisRevision],
) -> tuple[PersistedThesisRevision, ...]:
    return tuple(sorted(records, key=thesis_revision_order_key))


def _monthly_path(*, root: Path, target_key: str, business_at: datetime) -> Path:
    normalized_business_at = _normalize_business_at(business_at)
    normalized_target = validate_target_key(
        target_key,
        error_type=ThesisRevisionStoreError,
    )
    return (
        root / normalized_target / f"{normalized_business_at.strftime('%Y-%m')}.jsonl"
    ).resolve(strict=False)


def _paths(*, root: Path, target_key: str, year_month: str | None) -> tuple[Path, ...]:
    normalized_target = validate_target_key(
        target_key,
        error_type=ThesisRevisionStoreError,
    )
    target_root = root / normalized_target
    if year_month is not None:
        if not isinstance(year_month, str) or len(year_month) != 7:
            raise ThesisRevisionStoreError("year_month must be YYYY-MM.")
        return ((target_root / f"{year_month}.jsonl").resolve(strict=False),)
    return tuple(sorted(target_root.glob("*.jsonl")))


def _read_payloads(
    *,
    paths: tuple[Path, ...],
    target_key: str,
    label: str,
) -> tuple[tuple[Mapping[str, object], Path, int], ...]:
    normalized_target = validate_target_key(
        target_key,
        error_type=ThesisRevisionStoreError,
    )
    records: list[tuple[Mapping[str, object], Path, int]] = []
    for path in paths:
        if not path.exists():
            continue
        if not path.is_file():
            raise ThesisRevisionStoreError(f"{label} path must be a file: {path}")
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    raise ThesisRevisionStoreError(
                        f"{label} line {line_number} is invalid JSON."
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise ThesisRevisionStoreError(
                        f"{label} line {line_number} must be an object."
                    )
                if payload.get("target_key") != normalized_target:
                    raise ThesisRevisionStoreError(
                        f"{label} shard contains mixed target_key records."
                    )
                records.append((payload, path, line_number))
    return tuple(records)


def _append_json_line(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(dict(payload), ensure_ascii=False))
            handle.write("\n")
    except OSError as exc:
        raise ThesisRevisionStoreError(
            f"failed to append thesis revision record: {path}"
        ) from exc


def _normalize_business_at(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ThesisRevisionStoreError("business_at must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ThesisRevisionStoreError("business_at must be timezone-aware.")
    return value.astimezone(UTC)


__all__ = [
    "PersistedThesisRevision",
    "ThesisRevisionStore",
    "ThesisRevisionStoreError",
    "sort_persisted_thesis_revisions",
    "thesis_revision_order_key",
]
