"""Append-only file-backed store for reflection obligations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.reflection.trigger_policy import (
    ReflectionObligation,
    ReflectionObligationResolution,
    ReflectionTriggerPolicyError,
    parse_reflection_obligation,
    parse_reflection_obligation_resolution,
    reflection_obligation_record_hash,
)
from event_trader.storage import WorkspaceLayout

_OBLIGATIONS_FILE = "reflection_obligations.jsonl"
_RESOLUTIONS_FILE = "reflection_obligation_resolutions.jsonl"


class ReflectionObligationStoreError(ValueError):
    """Raised when reflection obligation persistence is invalid or conflicting."""


@dataclass(frozen=True, slots=True)
class PersistedReflectionObligation:
    record: ReflectionObligation
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedReflectionObligationResolution:
    record: ReflectionObligationResolution
    path: Path
    line_number: int
    record_hash: str


class FileBackedReflectionObligationStore:
    """Append and read target-scoped reflection obligations from research_memory."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise ReflectionObligationStoreError(
                "layout must be a WorkspaceLayout instance."
            )
        self._layout = layout

    def append_obligation(self, obligation: ReflectionObligation) -> Path:
        if not isinstance(obligation, ReflectionObligation):
            raise ReflectionObligationStoreError(
                "obligation must be a ReflectionObligation."
            )
        path = self.obligations_path(target_key=obligation.target_key)
        obligation_hash = reflection_obligation_record_hash(obligation.to_json_payload())
        for persisted in self.read_obligations(target_key=obligation.target_key):
            if persisted.record.obligation_id != obligation.obligation_id:
                continue
            if persisted.record_hash == obligation_hash:
                return path
            raise ReflectionObligationStoreError(
                "duplicate obligation_id has different payload."
            )
        _append_json_line(path, obligation.to_json_payload())
        return path

    def append_resolution(self, resolution: ReflectionObligationResolution) -> Path:
        if not isinstance(resolution, ReflectionObligationResolution):
            raise ReflectionObligationStoreError(
                "resolution must be a ReflectionObligationResolution."
            )
        path = self.resolutions_path(target_key=resolution.target_key)
        resolution_hash = reflection_obligation_record_hash(resolution.to_json_payload())
        for persisted in self.read_resolutions(target_key=resolution.target_key):
            if persisted.record.resolution_id != resolution.resolution_id:
                continue
            if persisted.record_hash == resolution_hash:
                return path
            raise ReflectionObligationStoreError(
                "duplicate resolution_id has different payload."
            )
        obligation = self._obligation_for_resolution(resolution)
        if resolution.resolved_at < obligation.source_observed_through:
            raise ReflectionObligationStoreError(
                "resolved_at must be at or after obligation source_observed_through."
            )
        _append_json_line(path, resolution.to_json_payload())
        return path

    def read_obligations(
        self,
        *,
        target_key: str,
    ) -> tuple[PersistedReflectionObligation, ...]:
        normalized_target = validate_target_key(
            target_key,
            error_type=ReflectionObligationStoreError,
        )
        return _read_obligations_from_path(
            self.obligations_path(target_key=normalized_target),
            target_key=normalized_target,
        )

    def read_resolutions(
        self,
        *,
        target_key: str,
    ) -> tuple[PersistedReflectionObligationResolution, ...]:
        normalized_target = validate_target_key(
            target_key,
            error_type=ReflectionObligationStoreError,
        )
        return _read_resolutions_from_path(
            self.resolutions_path(target_key=normalized_target),
            target_key=normalized_target,
        )

    def read_due_obligations(
        self,
        *,
        target_key: str,
        as_of: datetime,
    ) -> tuple[PersistedReflectionObligation, ...]:
        normalized_as_of = validate_timestamp(
            as_of,
            field_name="as_of",
            error_type=ReflectionObligationStoreError,
        )
        resolved_obligation_ids = self._valid_resolved_obligation_ids(
            target_key=target_key,
            as_of=normalized_as_of,
        )
        due = tuple(
            persisted
            for persisted in self.read_obligations(target_key=target_key)
            if persisted.record.status == "pending"
            and persisted.record.due_at <= normalized_as_of
            and persisted.record.obligation_id not in resolved_obligation_ids
        )
        return tuple(
            sorted(
                due,
                key=lambda persisted: (
                    persisted.record.due_at,
                    persisted.record.trigger_kind,
                    persisted.record.obligation_id,
                ),
            )
        )

    def obligations_path(self, *, target_key: str) -> Path:
        normalized_target = validate_target_key(
            target_key,
            error_type=ReflectionObligationStoreError,
        )
        return (
            self._layout.research_memory_root
            / "targets"
            / normalized_target
            / _OBLIGATIONS_FILE
        ).resolve(strict=False)

    def resolutions_path(self, *, target_key: str) -> Path:
        normalized_target = validate_target_key(
            target_key,
            error_type=ReflectionObligationStoreError,
        )
        return (
            self._layout.research_memory_root
            / "targets"
            / normalized_target
            / _RESOLUTIONS_FILE
        ).resolve(strict=False)

    def _obligation_for_resolution(
        self,
        resolution: ReflectionObligationResolution,
    ) -> ReflectionObligation:
        for persisted in self.read_obligations(target_key=resolution.target_key):
            if persisted.record.obligation_id == resolution.obligation_id:
                return persisted.record
        raise ReflectionObligationStoreError(
            "resolution requires an existing obligation in the same target shard."
        )

    def _valid_resolved_obligation_ids(
        self,
        *,
        target_key: str,
        as_of: datetime,
    ) -> frozenset[str]:
        obligations = {
            persisted.record.obligation_id: persisted.record
            for persisted in self.read_obligations(target_key=target_key)
        }
        resolved: set[str] = set()
        for persisted in self.read_resolutions(target_key=target_key):
            obligation = obligations.get(persisted.record.obligation_id)
            if obligation is None:
                continue
            if (
                obligation.source_observed_through
                <= persisted.record.resolved_at
                <= as_of
            ):
                resolved.add(persisted.record.obligation_id)
        return frozenset(resolved)


def _read_obligations_from_path(
    path: Path,
    *,
    target_key: str,
) -> tuple[PersistedReflectionObligation, ...]:
    if not path.exists():
        return ()
    if not path.is_file():
        raise ReflectionObligationStoreError(
            f"reflection obligation path must be a file: {path}"
        )
    records: list[PersistedReflectionObligation] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            normalized = line.strip()
            if not normalized:
                continue
            try:
                payload = json.loads(normalized)
            except json.JSONDecodeError as exc:
                raise ReflectionObligationStoreError(
                    f"reflection obligation line {line_number} is invalid JSON."
                ) from exc
            if not isinstance(payload, Mapping):
                raise ReflectionObligationStoreError(
                    f"reflection obligation line {line_number} must be an object."
                )
            if payload.get("target_key") != target_key:
                raise ReflectionObligationStoreError(
                    "reflection obligation shard contains mixed target_key records."
                )
            try:
                obligation = parse_reflection_obligation(payload)
            except ReflectionTriggerPolicyError as exc:
                raise ReflectionObligationStoreError(str(exc)) from exc
            records.append(
                PersistedReflectionObligation(
                    record=obligation,
                    path=path,
                    line_number=line_number,
                    record_hash=reflection_obligation_record_hash(
                        obligation.to_json_payload()
                    ),
                )
            )
    return tuple(records)


def _read_resolutions_from_path(
    path: Path,
    *,
    target_key: str,
) -> tuple[PersistedReflectionObligationResolution, ...]:
    if not path.exists():
        return ()
    if not path.is_file():
        raise ReflectionObligationStoreError(
            f"reflection obligation resolution path must be a file: {path}"
        )
    records: list[PersistedReflectionObligationResolution] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            normalized = line.strip()
            if not normalized:
                continue
            try:
                payload = json.loads(normalized)
            except json.JSONDecodeError as exc:
                raise ReflectionObligationStoreError(
                    "reflection obligation resolution line "
                    f"{line_number} is invalid JSON."
                ) from exc
            if not isinstance(payload, Mapping):
                raise ReflectionObligationStoreError(
                    "reflection obligation resolution line "
                    f"{line_number} must be an object."
                )
            if payload.get("target_key") != target_key:
                raise ReflectionObligationStoreError(
                    "reflection obligation resolution shard contains mixed "
                    "target_key records."
                )
            try:
                resolution = parse_reflection_obligation_resolution(payload)
            except ReflectionTriggerPolicyError as exc:
                raise ReflectionObligationStoreError(str(exc)) from exc
            records.append(
                PersistedReflectionObligationResolution(
                    record=resolution,
                    path=path,
                    line_number=line_number,
                    record_hash=reflection_obligation_record_hash(
                        resolution.to_json_payload()
                    ),
                )
            )
    return tuple(records)


def _append_json_line(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    dict(payload),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            handle.write("\n")
    except OSError as exc:
        raise ReflectionObligationStoreError(
            f"failed to append reflection obligation record: {path}"
        ) from exc


__all__ = [
    "FileBackedReflectionObligationStore",
    "PersistedReflectionObligation",
    "PersistedReflectionObligationResolution",
    "ReflectionObligationStoreError",
]
