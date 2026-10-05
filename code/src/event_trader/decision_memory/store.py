"""File-backed append-only store for decision episode records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from event_trader.contracts._validators import validate_target_key
from event_trader.decision_memory.contracts import (
    DecisionEpisodeRecord,
    DecisionMemoryContractError,
    decision_episode_record_hash,
    decision_episode_record_matches_retry,
    parse_decision_episode_record,
)
from event_trader.storage import WorkspaceLayout

_DECISION_EPISODE_DIR_NAME = "decision_episodes"


class DecisionEpisodeStoreError(ValueError):
    """Raised when decision episode storage is invalid or conflicting."""


@dataclass(frozen=True, slots=True)
class PersistedDecisionEpisodeRecord:
    record: DecisionEpisodeRecord
    path: Path
    line_number: int
    record_hash: str


class FileBackedDecisionEpisodeStore:
    """Append and read decision episode records by target/month."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise DecisionEpisodeStoreError("layout must be a WorkspaceLayout instance.")
        self._layout = layout

    def append_record(self, record: DecisionEpisodeRecord) -> Path:
        if not isinstance(record, DecisionEpisodeRecord):
            raise DecisionEpisodeStoreError(
                "record must be a DecisionEpisodeRecord instance."
            )
        if f":{record.target_key}:" not in record.episode_id:
            raise DecisionEpisodeStoreError("record episode_id target mismatch.")
        path = self.record_path(
            target_key=record.target_key,
            business_at=record.business_at,
        )
        record_hash = decision_episode_record_hash(record)
        for persisted in self.read_records(
            target_key=record.target_key,
            year_month=record.business_at.strftime("%Y-%m"),
        ):
            if persisted.record.natural_key != record.natural_key:
                continue
            if persisted.record_hash == record_hash or decision_episode_record_matches_retry(
                persisted.record,
                record,
            ):
                return path
            raise DecisionEpisodeStoreError(
                "decision episode duplicate natural key has different record payload."
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record.to_json_payload(), ensure_ascii=False))
                handle.write("\n")
        except OSError as exc:
            raise DecisionEpisodeStoreError(
                f"Failed to append decision episode record {record.episode_id!r}: {exc}"
            ) from exc
        return path

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedDecisionEpisodeRecord, ...]:
        normalized_target = validate_target_key(
            target_key,
            error_type=DecisionEpisodeStoreError,
        )
        paths = (
            (self._record_path_for_month(target_key=normalized_target, year_month=year_month),)
            if year_month is not None
            else tuple(sorted((self._store_root() / normalized_target).glob("*.jsonl")))
        )
        records: list[PersistedDecisionEpisodeRecord] = []
        for path in paths:
            if not path.exists():
                continue
            if not path.is_file():
                raise DecisionEpisodeStoreError(
                    f"decision episode path must be a file: {path}"
                )
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    normalized = line.strip()
                    if not normalized:
                        continue
                    try:
                        payload = json.loads(normalized)
                    except json.JSONDecodeError as exc:
                        raise DecisionEpisodeStoreError(
                            f"decision episode line {line_number} is invalid JSON."
                        ) from exc
                    if not isinstance(payload, dict):
                        raise DecisionEpisodeStoreError(
                            f"decision episode line {line_number} must be an object."
                        )
                    try:
                        record = parse_decision_episode_record(payload)
                    except DecisionMemoryContractError as exc:
                        raise DecisionEpisodeStoreError(str(exc)) from exc
                    if record.target_key != normalized_target:
                        raise DecisionEpisodeStoreError(
                            "decision episode shard contains mixed target_key records."
                        )
                    records.append(
                        PersistedDecisionEpisodeRecord(
                            record=record,
                            path=path,
                            line_number=line_number,
                            record_hash=decision_episode_record_hash(record),
                        )
                    )
        return tuple(records)

    def read_episode_records(
        self,
        *,
        target_key: str,
        episode_id: str,
    ) -> tuple[PersistedDecisionEpisodeRecord, ...]:
        return tuple(
            persisted
            for persisted in self.read_records(target_key=target_key)
            if persisted.record.episode_id == episode_id
        )

    def record_path(self, *, target_key: str, business_at: datetime) -> Path:
        if not isinstance(business_at, datetime):
            raise DecisionEpisodeStoreError("business_at must be a datetime.")
        return self._record_path_for_month(
            target_key=target_key,
            year_month=business_at.strftime("%Y-%m"),
        )

    def _record_path_for_month(self, *, target_key: str, year_month: str) -> Path:
        normalized_target = validate_target_key(
            target_key,
            error_type=DecisionEpisodeStoreError,
        )
        if not isinstance(year_month, str) or len(year_month) != 7:
            raise DecisionEpisodeStoreError("year_month must be YYYY-MM.")
        return (
            self._store_root() / normalized_target / f"{year_month}.jsonl"
        ).resolve(strict=False)

    def _store_root(self) -> Path:
        return self._layout.runtime_root / _DECISION_EPISODE_DIR_NAME


__all__ = [
    "DecisionEpisodeStoreError",
    "FileBackedDecisionEpisodeStore",
    "PersistedDecisionEpisodeRecord",
]
