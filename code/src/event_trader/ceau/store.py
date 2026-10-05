"""File-backed canonical append-only CEAU runtime log and runtime-state projection."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Lock, RLock
from typing import Any, TypeAlias

from event_trader.ceau.contracts import (
    AnalysisCompletedStatus,
    CEAUAnalysisCompletedPointer,
    CEAUAnalysisCompletedRecord,
    CEAUAnalysisFailedRecord,
    CEAUAnalysisQueuedRecord,
    CEAUAnalysisStartedRecord,
    CEAUContractError,
    CEAUEventRouteRecord,
    CEAUSourceCompletenessObservedRecord,
    CEAUUnitAppendedRecord,
    CEAUUnitEmittedRecord,
    CEAUUnitOpenedRecord,
    CEAUWatermarkObservedRecord,
    CompletenessDomain,
    DomainWatermark,
    SourceCompletenessObservation,
    json_dumps,
)
from event_trader.contracts._validators import validate_target_key
from event_trader.storage import WorkspaceLayout

_CEAU_ROOT: Path = Path("ceau")
_APPEND_LOCKS_GUARD = Lock()
_TARGET_LOCKS: dict[str, Any] = {}


class FileBackedCEAUStoreError(ValueError):
    """Raised when CEAU runtime persistence is invalid or conflicting."""


_RecordType: TypeAlias = (
    CEAUEventRouteRecord
    | CEAUUnitOpenedRecord
    | CEAUUnitAppendedRecord
    | CEAUUnitEmittedRecord
    | CEAUAnalysisQueuedRecord
    | CEAUAnalysisStartedRecord
    | CEAUAnalysisFailedRecord
    | CEAUAnalysisCompletedRecord
    | CEAUSourceCompletenessObservedRecord
    | CEAUWatermarkObservedRecord
)


@dataclass(frozen=True, slots=True)
class PersistedCEAURecord:
    record: _RecordType
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class CEAUEventRouteProjection:
    event_id: str
    routing_lane: str
    final_route: str
    analysis_unit_id: str | None
    watch_id: str | None


@dataclass(frozen=True, slots=True)
class CEAUUnitProjection:
    analysis_unit_id: str
    target_key: str
    event_ids: tuple[str, ...]
    primary_event_ids: tuple[str, ...]
    context_event_ids: tuple[str, ...]
    emitted: bool
    completed: bool
    completed_status: str | None


@dataclass(frozen=True, slots=True)
class CEAUAnalysisCompletedProjection:
    analysis_unit_id: str
    completed_status: AnalysisCompletedStatus
    pointer: CEAUAnalysisCompletedPointer


@dataclass(frozen=True, slots=True)
class CEAURuntimeProjection:
    event_routes: tuple[CEAUEventRouteProjection, ...]
    unit_projections: tuple[CEAUUnitProjection, ...]
    analysis_completions: tuple[CEAUAnalysisCompletedProjection, ...]
    latest_source_observations: tuple[SourceCompletenessObservation, ...]
    latest_domain_watermarks: tuple[DomainWatermark, ...]


class FileBackedCEAUStore:
    """Canonical append-only CEAU runtime store."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def append_record(self, record: object) -> Path:
        canonical_record = _coerce_record(record)
        path = self.record_path(
            target_key=canonical_record.target_key,
            recorded_at=canonical_record.recorded_at,
        )
        with _lock_for_target(canonical_record.target_key):
            payload_hash = _record_hash(canonical_record)
            for persisted in self.read_records(target_key=canonical_record.target_key):
                if persisted.record.record_id == canonical_record.record_id:
                    if persisted.record_hash == payload_hash:
                        return path
                    raise FileBackedCEAUStoreError(
                        "duplicate CEAU record_id has different payload: "
                        f"{canonical_record.record_id!r}"
                    )
                if _has_business_key_conflict(
                    candidate=canonical_record,
                    candidate_hash=payload_hash,
                    existing=persisted.record,
                    existing_hash=persisted.record_hash,
                ):
                    raise FileBackedCEAUStoreError(
                        "duplicate CEAU business key has different payload."
                    )
            _append_json_line(path, canonical_record.to_json_payload())
        return path

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedCEAURecord, ...]:
        normalized_target = validate_target_key(
            target_key,
            error_type=FileBackedCEAUStoreError,
        )
        with _lock_for_target(normalized_target):
            return tuple(
                PersistedCEAURecord(
                    record=record,
                    path=path,
                    line_number=line_number,
                    record_hash=_record_hash(record),
                )
                for record, path, line_number in _read_records(
                    paths=_paths(
                        root=self._layout.runtime_root / _CEAU_ROOT,
                        target_key=normalized_target,
                        year_month=year_month,
                    ),
                    target_key=normalized_target,
                )
            )

    def rebuild_projection(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> CEAURuntimeProjection:
        return rebuild_ceu_runtime_projection(
            self.read_records(target_key=target_key, year_month=year_month)
        )

    def record_path(self, *, target_key: str, recorded_at: datetime) -> Path:
        return _record_path_for_month(
            root=self._layout.runtime_root / _CEAU_ROOT,
            target_key=target_key,
            recorded_at=recorded_at,
        )


def rebuild_ceu_runtime_projection(
    records: tuple[PersistedCEAURecord, ...],
) -> CEAURuntimeProjection:
    """Rebuild a derivable runtime projection from append-only records."""
    event_route_by_event: dict[str, tuple[tuple[datetime, str, int], CEAUEventRouteProjection]] = {}
    unit_states: dict[str, tuple[tuple[datetime, str, int], CEAUUnitProjection]] = {}
    analysis_completed: dict[str, tuple[tuple[datetime, str, int], CEAUAnalysisCompletedProjection]] = {}
    source_observations: dict[
        tuple[str, CompletenessDomain], tuple[tuple[datetime, str, int], SourceCompletenessObservation]
    ] = {}
    domain_watermarks: dict[
        CompletenessDomain,
        tuple[tuple[datetime, str, int], DomainWatermark],
    ] = {}

    for item in records:
        key = (item.record.recorded_at, item.path.as_posix(), item.line_number)
        record = item.record
        if isinstance(record, CEAUEventRouteRecord):
            event_projection = CEAUEventRouteProjection(
                event_id=record.event_id,
                routing_lane=record.routing_lane,
                final_route=record.final_route,
                analysis_unit_id=record.analysis_unit_id,
                watch_id=record.watch_id,
            )
            if _is_newer(
                current=_event_projection_key(event_route_by_event.get(record.event_id)),
                candidate=key,
            ):
                event_route_by_event[record.event_id] = (key, event_projection)
            continue

        if isinstance(record, CEAUUnitEmittedRecord):
            existing = unit_states.get(record.analysis_unit_id)
            emitted_unit_projection = CEAUUnitProjection(
                analysis_unit_id=record.analysis_unit_id,
                target_key=record.target_key,
                event_ids=record.event_ids,
                primary_event_ids=record.primary_event_ids,
                context_event_ids=record.context_event_ids,
                emitted=True,
                completed=False,
                completed_status=None,
            )
            if _is_newer(current=_unit_projection_key(existing), candidate=key):
                unit_states[record.analysis_unit_id] = (key, emitted_unit_projection)
            continue

        if isinstance(record, (CEAUUnitOpenedRecord, CEAUUnitAppendedRecord)):
            existing = unit_states.get(record.analysis_unit_id)
            lifecycle_unit_projection = CEAUUnitProjection(
                analysis_unit_id=record.analysis_unit_id,
                target_key=record.target_key,
                event_ids=record.event_ids,
                primary_event_ids=record.primary_event_ids,
                context_event_ids=record.context_event_ids,
                emitted=False,
                completed=False,
                completed_status=None,
            )
            if _is_newer(current=_unit_projection_key(existing), candidate=key):
                unit_states[record.analysis_unit_id] = (key, lifecycle_unit_projection)
            continue

        if isinstance(record, CEAUAnalysisCompletedRecord):
            existing_status = analysis_completed.get(record.analysis_unit_id)
            completion_projection = CEAUAnalysisCompletedProjection(
                analysis_unit_id=record.analysis_unit_id,
                completed_status=record.pointer.completed_status,
                pointer=record.pointer,
            )
            if _is_newer(current=_completion_projection_key(existing_status), candidate=key):
                analysis_completed[record.analysis_unit_id] = (key, completion_projection)
            completed = _ensure_unit_entry(unit_states, record.analysis_unit_id, record.target_key)
            existing_unit_projection = unit_states.get(record.analysis_unit_id, completed)
            if _is_newer(
                current=_unit_projection_key(existing_unit_projection),
                candidate=key,
            ):
                unit_states[record.analysis_unit_id] = (
                    key,
                    CEAUUnitProjection(
                        analysis_unit_id=completed[1].analysis_unit_id,
                        target_key=completed[1].target_key,
                        event_ids=completed[1].event_ids,
                        primary_event_ids=completed[1].primary_event_ids,
                        context_event_ids=completed[1].context_event_ids,
                        emitted=completed[1].emitted,
                        completed=True,
                        completed_status=record.pointer.completed_status,
                    ),
                )
            continue

        if isinstance(
            record,
            (
                CEAUAnalysisQueuedRecord,
                CEAUAnalysisStartedRecord,
                CEAUAnalysisFailedRecord,
            ),
        ):
            continue

        if isinstance(record, CEAUSourceCompletenessObservedRecord):
            source = record.source_observation
            source_key = (source.source_name, source.completeness_domain)
            if _is_newer(
                current=_source_projection_key(source_observations.get(source_key)),
                candidate=key,
            ):
                source_observations[source_key] = (key, source)
            continue

        if isinstance(record, CEAUWatermarkObservedRecord):
            observation = record.watermark_observation
            for domain, watermark in observation.domain_watermarks.items():
                if _is_newer(
                    current=_watermark_projection_key(domain_watermarks.get(domain)),
                    candidate=key,
                ):
                    domain_watermarks[domain] = (key, watermark)
            continue

        raise FileBackedCEAUStoreError(
            f"unsupported CEAU record type in projection rebuild: {type(record)}"
        )

    for analysis_unit_id in analysis_completed:
        completion = analysis_completed[analysis_unit_id][1]
        existing_unit = unit_states.get(analysis_unit_id)
        if existing_unit is None:
            unit_states[analysis_unit_id] = (
                (datetime.min.replace(tzinfo=UTC), "", 0),
                CEAUUnitProjection(
                    analysis_unit_id=completion.analysis_unit_id,
                    target_key="",
                    event_ids=(),
                    primary_event_ids=(),
                    context_event_ids=(),
                    emitted=False,
                    completed=True,
                    completed_status=completion.completed_status,
                ),
            )
        else:
            existing_unit_record_key = existing_unit[0]
            prior_unit_projection = existing_unit[1]
            unit_states[analysis_unit_id] = (
                existing_unit_record_key,
                CEAUUnitProjection(
                    analysis_unit_id=prior_unit_projection.analysis_unit_id,
                    target_key=prior_unit_projection.target_key,
                    event_ids=prior_unit_projection.event_ids,
                    primary_event_ids=prior_unit_projection.primary_event_ids,
                    context_event_ids=prior_unit_projection.context_event_ids,
                    emitted=prior_unit_projection.emitted,
                    completed=True,
                    completed_status=completion.completed_status,
                ),
            )

    latest_source_observations = tuple(
        payload[1]
        for _, payload in sorted(
            source_observations.items(),
            key=lambda item: (item[0][0], item[0][1]),
        )
    )
    latest_domain_watermarks = tuple(
        value[1]
        for _, value in sorted(
            domain_watermarks.items(),
            key=lambda item: item[0],
        )
    )
    latest_event_routes = tuple(
        projection
        for _, projection in sorted(
            ((event_id, value[1]) for event_id, value in event_route_by_event.items()),
            key=lambda item: item[0],
        )
    )
    latest_unit_states = tuple(
        projection
        for _, projection in sorted(
            ((unit_id, value[1]) for unit_id, value in unit_states.items()),
            key=lambda item: item[0],
        )
    )
    latest_completions = tuple(
        projection
        for _, projection in sorted(
            ((unit_id, value[1]) for unit_id, value in analysis_completed.items()),
            key=lambda item: item[0],
        )
    )

    return CEAURuntimeProjection(
        event_routes=latest_event_routes,
        unit_projections=latest_unit_states,
        analysis_completions=latest_completions,
        latest_source_observations=latest_source_observations,
        latest_domain_watermarks=latest_domain_watermarks,
    )


def _coerce_record(payload: object) -> _RecordType:
    if isinstance(payload, (
        CEAUEventRouteRecord,
        CEAUUnitOpenedRecord,
        CEAUUnitAppendedRecord,
        CEAUUnitEmittedRecord,
        CEAUAnalysisQueuedRecord,
        CEAUAnalysisStartedRecord,
        CEAUAnalysisFailedRecord,
        CEAUAnalysisCompletedRecord,
        CEAUSourceCompletenessObservedRecord,
        CEAUWatermarkObservedRecord,
    )):
        return payload

    if not isinstance(payload, Mapping):
        raise FileBackedCEAUStoreError(
            "record must be a CEAU record object or JSON payload mapping."
        )
    try:
        record_type = payload.get("record_type")
        if record_type == "event_route":
            return CEAUEventRouteRecord.from_json_payload(payload)
        if record_type == "unit_opened":
            return CEAUUnitOpenedRecord.from_json_payload(payload)
        if record_type == "unit_appended":
            return CEAUUnitAppendedRecord.from_json_payload(payload)
        if record_type == "unit_emitted":
            return CEAUUnitEmittedRecord.from_json_payload(payload)
        if record_type == "analysis_queued":
            return CEAUAnalysisQueuedRecord.from_json_payload(payload)
        if record_type == "analysis_started":
            return CEAUAnalysisStartedRecord.from_json_payload(payload)
        if record_type == "analysis_failed":
            return CEAUAnalysisFailedRecord.from_json_payload(payload)
        if record_type == "analysis_completed":
            return CEAUAnalysisCompletedRecord.from_json_payload(payload)
        if record_type == "source_completeness_observed":
            return CEAUSourceCompletenessObservedRecord.from_json_payload(payload)
        if record_type == "watermark_observed":
            return CEAUWatermarkObservedRecord.from_json_payload(payload)
        raise FileBackedCEAUStoreError("unsupported CEAU record_type for runtime log.")
    except CEAUContractError as exc:
        raise FileBackedCEAUStoreError(str(exc)) from exc
    except KeyError as exc:  # pragma: no cover
        raise FileBackedCEAUStoreError(f"record payload missing field: {exc}") from exc


def _ensure_unit_entry(
    unit_states: dict[str, tuple[tuple[datetime, str, int], CEAUUnitProjection]],
    analysis_unit_id: str,
    target_key: str,
) -> tuple[tuple[datetime, str, int], CEAUUnitProjection]:
    existing = unit_states.get(analysis_unit_id)
    if existing is not None:
        return existing
    placeholder = CEAUUnitProjection(
        analysis_unit_id=analysis_unit_id,
        target_key=target_key,
        event_ids=(),
        primary_event_ids=(),
        context_event_ids=(),
        emitted=False,
        completed=False,
        completed_status=None,
    )
    stamp = (datetime.min.replace(tzinfo=UTC), "", 0)
    unit_states[analysis_unit_id] = (stamp, placeholder)
    return unit_states[analysis_unit_id]


def _has_business_key_conflict(
    *,
    candidate: _RecordType,
    candidate_hash: str,
    existing: _RecordType,
    existing_hash: str,
) -> bool:
    if (
        isinstance(candidate, CEAUEventRouteRecord)
        and isinstance(existing, CEAUEventRouteRecord)
    ):
        if candidate.event_id != existing.event_id:
            return False
        return (
            candidate.record_id != existing.record_id
            or candidate_hash != existing_hash
        )

    if (
        isinstance(candidate, CEAUUnitEmittedRecord)
        and isinstance(existing, CEAUUnitEmittedRecord)
    ):
        if candidate.analysis_unit_id != existing.analysis_unit_id:
            return False
        return (
            candidate.record_id != existing.record_id
            or candidate_hash != existing_hash
        )

    if (
        isinstance(candidate, CEAUUnitOpenedRecord)
        and isinstance(existing, CEAUUnitOpenedRecord)
    ):
        if candidate.analysis_unit_id != existing.analysis_unit_id:
            return False
        return (
            candidate.record_id != existing.record_id
            or candidate_hash != existing_hash
        )

    if (
        isinstance(candidate, CEAUUnitAppendedRecord)
        and isinstance(existing, CEAUUnitAppendedRecord)
    ):
        candidate_key = (candidate.analysis_unit_id, candidate.event_ids[-1])
        existing_key = (existing.analysis_unit_id, existing.event_ids[-1])
        if candidate_key != existing_key:
            return False
        return (
            candidate.record_id != existing.record_id
            or candidate_hash != existing_hash
        )

    if isinstance(candidate, (CEAUUnitOpenedRecord, CEAUUnitAppendedRecord)) and isinstance(
        existing, CEAUUnitEmittedRecord
    ):
        return candidate.analysis_unit_id == existing.analysis_unit_id

    if (
        isinstance(candidate, CEAUAnalysisCompletedRecord)
        and isinstance(existing, CEAUAnalysisCompletedRecord)
    ):
        if candidate.analysis_unit_id != existing.analysis_unit_id:
            return False
        return (
            candidate.record_id != existing.record_id
            or candidate_hash != existing_hash
        )

    if (
        isinstance(candidate, CEAUAnalysisQueuedRecord)
        and isinstance(existing, CEAUAnalysisQueuedRecord)
    ):
        if candidate.analysis_unit_id != existing.analysis_unit_id:
            return False
        return (
            candidate.record_id != existing.record_id
            or candidate_hash != existing_hash
        )

    if (
        isinstance(candidate, CEAUAnalysisStartedRecord)
        and isinstance(existing, CEAUAnalysisStartedRecord)
    ):
        if candidate.analysis_unit_id != existing.analysis_unit_id:
            return False
        return (
            candidate.record_id != existing.record_id
            or candidate_hash != existing_hash
        )

    if (
        isinstance(candidate, CEAUAnalysisFailedRecord)
        and isinstance(existing, CEAUAnalysisFailedRecord)
    ):
        if candidate.analysis_unit_id != existing.analysis_unit_id:
            return False
        return (
            candidate.record_id != existing.record_id
            or candidate_hash != existing_hash
        )

    return False


def _is_newer(
    *,
    current: tuple[datetime, str, int] | None,
    candidate: tuple[datetime, str, int],
) -> bool:
    return (
        current is None
        or candidate > current
    )


def _event_projection_key(
    entry: tuple[tuple[datetime, str, int], CEAUEventRouteProjection] | None,
) -> tuple[datetime, str, int] | None:
    if entry is None:
        return None
    return entry[0]


def _unit_projection_key(
    entry: tuple[tuple[datetime, str, int], CEAUUnitProjection] | None,
) -> tuple[datetime, str, int] | None:
    if entry is None:
        return None
    return entry[0]


def _completion_projection_key(
    entry: tuple[tuple[datetime, str, int], CEAUAnalysisCompletedProjection] | None,
) -> tuple[datetime, str, int] | None:
    if entry is None:
        return None
    return entry[0]


def _source_projection_key(
    entry: tuple[tuple[datetime, str, int], SourceCompletenessObservation] | None,
) -> tuple[datetime, str, int] | None:
    if entry is None:
        return None
    return entry[0]


def _watermark_projection_key(
    entry: tuple[tuple[datetime, str, int], DomainWatermark] | None,
) -> tuple[datetime, str, int] | None:
    if entry is None:
        return None
    return entry[0]


def _record_hash(record: _RecordType) -> str:
    return sha256(json_dumps(record.to_json_payload()).encode("utf-8")).hexdigest()


def _validate_layout(layout: WorkspaceLayout) -> None:
    if not isinstance(layout, WorkspaceLayout):
        raise FileBackedCEAUStoreError("layout must be a WorkspaceLayout instance.")


def _record_path_for_month(
    *,
    root: Path,
    target_key: str,
    recorded_at: datetime,
) -> Path:
    normalized_target = validate_target_key(target_key, error_type=FileBackedCEAUStoreError)
    if not isinstance(recorded_at, datetime):
        raise FileBackedCEAUStoreError("recorded_at must be a datetime.")
    return (
        root / normalized_target / f"{recorded_at.strftime('%Y-%m')}.jsonl"
    ).resolve(strict=False)


def _paths(*, root: Path, target_key: str, year_month: str | None) -> tuple[Path, ...]:
    target_root = root / target_key
    if year_month is None:
        return tuple(sorted(target_root.glob("*.jsonl")))
    if not isinstance(year_month, str) or len(year_month) != 7:
        raise FileBackedCEAUStoreError("year_month must be YYYY-MM.")
    return ((target_root / f"{year_month}.jsonl").resolve(strict=False),)


def _read_records(
    *,
    paths: tuple[Path, ...],
    target_key: str,
) -> tuple[tuple[_RecordType, Path, int], ...]:
    return tuple(
        (record, path, line_number)
        for record, path, line_number in _read_payloads(
            paths=paths,
            target_key=target_key,
        )
    )


def _read_payloads(
    *,
    paths: tuple[Path, ...],
    target_key: str,
) -> tuple[tuple[_RecordType, Path, int], ...]:
    records: list[tuple[_RecordType, Path, int]] = []
    for path in paths:
        if not path.exists():
            continue
        if not path.is_file():
            raise FileBackedCEAUStoreError(f"CEAU runtime path must be a file: {path}")
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    raise FileBackedCEAUStoreError(
                        f"CEAU runtime line {line_number} is invalid JSON."
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise FileBackedCEAUStoreError(
                        f"CEAU runtime line {line_number} must be an object."
                    )
                if payload.get("target_key") != target_key:
                    raise FileBackedCEAUStoreError(
                        "CEAU runtime shard contains mixed target_key."
                    )
                try:
                    record = _coerce_record(payload)
                except FileBackedCEAUStoreError as exc:
                    raise FileBackedCEAUStoreError(
                        f"CEAU runtime line {line_number} has invalid record payload: {exc}"
                    ) from exc
                records.append((record, path, line_number))
    return tuple(records)


def _append_json_line(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(dict(payload), ensure_ascii=False))
            handle.write("\n")
    except OSError as exc:
        raise FileBackedCEAUStoreError(
            f"failed to append CEAU runtime record: {path}"
        ) from exc


def _lock_for_target(target_key: str):
    with _APPEND_LOCKS_GUARD:
        lock = _TARGET_LOCKS.get(target_key)
        if lock is None:
            lock = RLock()
            _TARGET_LOCKS[target_key] = lock
        return lock


__all__ = [
    "CEAUAnalysisCompletedProjection",
    "CEAUEventRouteProjection",
    "CEAURuntimeProjection",
    "CEAUUnitProjection",
    "FileBackedCEAUStore",
    "FileBackedCEAUStoreError",
    "PersistedCEAURecord",
    "rebuild_ceu_runtime_projection",
]
