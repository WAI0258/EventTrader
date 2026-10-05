"""Append-only stores for canonical episode-memory records."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from hashlib import sha256
from typing import TypeVar

from event_trader.contracts._validators import validate_target_key
from event_trader.storage import WorkspaceLayout
from event_trader.episode_memory.contracts import (
    EpisodeMemoryDelta,
    EpisodeMemoryNoUpdate,
    EpisodeMemoryPromotionDecisionIntent,
    EpisodeMemoryPromotionDecisionView,
    EpisodeMemoryPromotionValidationReceipt,
    EpisodeMemoryPromotionWriteReceipt,
    EpisodeMemoryWriteReceipt,
    EpisodeMemoryContractError,
    LearningCardPromotionIntent,
    NoPromoteIntent,
    RiskPolicyCandidatePromotionIntent,
    build_promotion_decision_view,
    parse_episode_memory_delta,
    parse_episode_memory_no_update,
    parse_episode_memory_promotion_intent,
    parse_episode_memory_promotion_validation_receipt,
    parse_episode_memory_promotion_write_receipt,
    parse_episode_memory_write_receipt,
    episode_memory_record_hash,
)

_MEMORY_DIR = Path("memory")
_DELTAS_FILE = "deltas.jsonl"
_NO_UPDATES_FILE = "no_updates.jsonl"
_WRITE_RECEIPTS_FILE = "write_receipts.jsonl"
_PROMOTION_INTENTS_FILE = "promotion_intents.jsonl"
_PROMOTION_VALIDATION_RECEIPTS_FILE = "promotion_validation_receipts.jsonl"
_PROMOTION_WRITE_RECEIPTS_FILE = "promotion_write_receipts.jsonl"
_EPISODE_KEY_PREFIX = "episode_"

_TRecord = TypeVar("_TRecord")


class EpisodeMemoryStoreError(ValueError):
    """Raised when episode memory persistence is invalid or conflicting."""


@dataclass(frozen=True, slots=True)
class PersistedEpisodeMemoryDelta:
    record: EpisodeMemoryDelta
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedEpisodeMemoryNoUpdate:
    record: EpisodeMemoryNoUpdate
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedEpisodeMemoryWriteReceipt:
    record: EpisodeMemoryWriteReceipt
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedEpisodeMemoryPromotionIntent:
    record: EpisodeMemoryPromotionDecisionIntent
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedEpisodeMemoryPromotionValidationReceipt:
    record: EpisodeMemoryPromotionValidationReceipt
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedEpisodeMemoryPromotionWriteReceipt:
    record: EpisodeMemoryPromotionWriteReceipt
    path: Path
    line_number: int
    record_hash: str


class FileBackedEpisodeMemoryStore:
    """Append and read episode-memory records from research_memory."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise EpisodeMemoryStoreError(
                "layout must be a WorkspaceLayout instance."
            )
        self._layout = layout

    def append_delta(self, delta: EpisodeMemoryDelta) -> Path:
        if not isinstance(delta, EpisodeMemoryDelta):
            raise EpisodeMemoryStoreError("delta must be an EpisodeMemoryDelta.")
        path = self.delta_path(
            target_key=delta.target_key,
            episode_id=delta.episode_id,
        )
        for persisted in self.read_deltas(
            target_key=delta.target_key,
            episode_id=delta.episode_id,
        ):
            if persisted.record.delta_id != delta.delta_id:
                continue
            if persisted.record_hash == episode_memory_record_hash(
                delta.to_json_payload()
            ):
                return path
            raise EpisodeMemoryStoreError(
                "duplicate delta_id has different payload."
            )
        _append_json_line(path, delta.to_json_payload())
        return path

    def append_no_update(self, no_update: EpisodeMemoryNoUpdate) -> Path:
        if not isinstance(no_update, EpisodeMemoryNoUpdate):
            raise EpisodeMemoryStoreError(
                "no_update must be an EpisodeMemoryNoUpdate."
            )
        path = self.no_update_path(
            target_key=no_update.target_key,
            episode_id=no_update.episode_id,
        )
        for persisted in self.read_no_updates(
            target_key=no_update.target_key,
            episode_id=no_update.episode_id,
        ):
            if persisted.record.receipt_id != no_update.receipt_id:
                continue
            if persisted.record_hash == episode_memory_record_hash(no_update.to_json_payload()):
                return path
            raise EpisodeMemoryStoreError(
                "duplicate receipt_id has different payload."
            )
        _append_json_line(path, no_update.to_json_payload())
        return path

    def append_write_receipt(self, receipt: EpisodeMemoryWriteReceipt) -> Path:
        if not isinstance(receipt, EpisodeMemoryWriteReceipt):
            raise EpisodeMemoryStoreError(
                "receipt must be an EpisodeMemoryWriteReceipt."
            )
        path = self.write_receipt_path(
            target_key=receipt.target_key,
            episode_id=receipt.episode_id,
        )
        for persisted in self.read_write_receipts(
            target_key=receipt.target_key,
            episode_id=receipt.episode_id,
        ):
            if persisted.record.receipt_id != receipt.receipt_id:
                continue
            if persisted.record_hash == episode_memory_record_hash(receipt.to_json_payload()):
                return path
            raise EpisodeMemoryStoreError(
                "duplicate receipt_id has different payload."
            )
        _append_json_line(path, receipt.to_json_payload())
        return path

    def append_promotion_intent(
        self,
        intent: EpisodeMemoryPromotionDecisionIntent,
    ) -> Path:
        if not isinstance(
            intent,
            (NoPromoteIntent, LearningCardPromotionIntent, RiskPolicyCandidatePromotionIntent),
        ):
            raise EpisodeMemoryStoreError(
                "intent must be an episode memory promotion intent."
            )
        path = self.promotion_intent_path(
            target_key=intent.target_key,
            episode_id=intent.episode_id,
        )
        for persisted in self.read_promotion_intents(
            target_key=intent.target_key,
            episode_id=intent.episode_id,
        ):
            if persisted.record.decision_id != intent.decision_id:
                continue
            if persisted.record_hash == episode_memory_record_hash(intent.to_json_payload()):
                return path
            raise EpisodeMemoryStoreError(
                "duplicate decision_id has different payload."
            )
        _append_json_line(path, intent.to_json_payload())
        return path

    def append_promotion_validation_receipt(
        self,
        receipt: EpisodeMemoryPromotionValidationReceipt,
    ) -> Path:
        if not isinstance(receipt, EpisodeMemoryPromotionValidationReceipt):
            raise EpisodeMemoryStoreError(
                "receipt must be an EpisodeMemoryPromotionValidationReceipt."
            )
        path = self.promotion_validation_receipt_path(
            target_key=receipt.target_key,
            episode_id=receipt.episode_id,
        )
        for persisted in self.read_promotion_validation_receipts(
            target_key=receipt.target_key,
            episode_id=receipt.episode_id,
        ):
            if persisted.record.receipt_id != receipt.receipt_id:
                continue
            if persisted.record_hash == episode_memory_record_hash(receipt.to_json_payload()):
                return path
            raise EpisodeMemoryStoreError(
                "duplicate receipt_id has different payload."
            )
        _append_json_line(path, receipt.to_json_payload())
        return path

    def append_promotion_write_receipt(
        self,
        receipt: EpisodeMemoryPromotionWriteReceipt,
    ) -> Path:
        if not isinstance(receipt, EpisodeMemoryPromotionWriteReceipt):
            raise EpisodeMemoryStoreError(
                "receipt must be an EpisodeMemoryPromotionWriteReceipt."
            )
        path = self.promotion_write_receipt_path(
            target_key=receipt.target_key,
            episode_id=receipt.episode_id,
        )
        for persisted in self.read_promotion_write_receipts(
            target_key=receipt.target_key,
            episode_id=receipt.episode_id,
        ):
            if persisted.record.receipt_id != receipt.receipt_id:
                continue
            if persisted.record_hash == episode_memory_record_hash(receipt.to_json_payload()):
                return path
            raise EpisodeMemoryStoreError(
                "duplicate receipt_id has different payload."
            )
        _append_json_line(path, receipt.to_json_payload())
        return path

    def read_deltas(
        self,
        *,
        target_key: str,
        episode_id: str,
    ) -> tuple[PersistedEpisodeMemoryDelta, ...]:
        normalized_target = validate_target_key(
            target_key,
            error_type=EpisodeMemoryStoreError,
        )
        normalized_episode_id = _validate_episode_id(episode_id)
        payloads = _read_typed_records(
            paths=(self.delta_path(
                target_key=normalized_target,
                episode_id=normalized_episode_id,
            ),),
            target_key=normalized_target,
            episode_id=normalized_episode_id,
            parser=parse_episode_memory_delta,
            label="episode-memory delta",
        )
        return tuple(
            PersistedEpisodeMemoryDelta(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=episode_memory_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in payloads
        )

    def read_no_updates(
        self,
        *,
        target_key: str,
        episode_id: str,
    ) -> tuple[PersistedEpisodeMemoryNoUpdate, ...]:
        normalized_target = validate_target_key(
            target_key,
            error_type=EpisodeMemoryStoreError,
        )
        normalized_episode_id = _validate_episode_id(episode_id)
        payloads = _read_typed_records(
            paths=(self.no_update_path(
                target_key=normalized_target,
                episode_id=normalized_episode_id,
            ),),
            target_key=normalized_target,
            episode_id=normalized_episode_id,
            parser=parse_episode_memory_no_update,
            label="episode-memory no-update",
        )
        return tuple(
            PersistedEpisodeMemoryNoUpdate(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=episode_memory_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in payloads
        )

    def read_write_receipts(
        self,
        *,
        target_key: str,
        episode_id: str,
    ) -> tuple[PersistedEpisodeMemoryWriteReceipt, ...]:
        normalized_target = validate_target_key(
            target_key,
            error_type=EpisodeMemoryStoreError,
        )
        normalized_episode_id = _validate_episode_id(episode_id)
        payloads = _read_typed_records(
            paths=(self.write_receipt_path(
                target_key=normalized_target,
                episode_id=normalized_episode_id,
            ),),
            target_key=normalized_target,
            episode_id=normalized_episode_id,
            parser=parse_episode_memory_write_receipt,
            label="episode-memory write receipt",
        )
        return tuple(
            PersistedEpisodeMemoryWriteReceipt(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=episode_memory_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in payloads
        )

    def read_promotion_intents(
        self,
        *,
        target_key: str,
        episode_id: str,
    ) -> tuple[PersistedEpisodeMemoryPromotionIntent, ...]:
        normalized_target = validate_target_key(
            target_key,
            error_type=EpisodeMemoryStoreError,
        )
        normalized_episode_id = _validate_episode_id(episode_id)
        payloads = _read_typed_records(
            paths=(
                self.promotion_intent_path(
                    target_key=normalized_target,
                    episode_id=normalized_episode_id,
                ),
            ),
            target_key=normalized_target,
            episode_id=normalized_episode_id,
            parser=parse_episode_memory_promotion_intent,
            label="episode-memory promotion intent",
        )
        return tuple(
            PersistedEpisodeMemoryPromotionIntent(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=episode_memory_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in payloads
        )

    def read_promotion_validation_receipts(
        self,
        *,
        target_key: str,
        episode_id: str,
    ) -> tuple[PersistedEpisodeMemoryPromotionValidationReceipt, ...]:
        normalized_target = validate_target_key(
            target_key,
            error_type=EpisodeMemoryStoreError,
        )
        normalized_episode_id = _validate_episode_id(episode_id)
        payloads = _read_typed_records(
            paths=(
                self.promotion_validation_receipt_path(
                    target_key=normalized_target,
                    episode_id=normalized_episode_id,
                ),
            ),
            target_key=normalized_target,
            episode_id=normalized_episode_id,
            parser=parse_episode_memory_promotion_validation_receipt,
            label="episode-memory promotion validation receipt",
        )
        return tuple(
            PersistedEpisodeMemoryPromotionValidationReceipt(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=episode_memory_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in payloads
        )

    def read_promotion_write_receipts(
        self,
        *,
        target_key: str,
        episode_id: str,
    ) -> tuple[PersistedEpisodeMemoryPromotionWriteReceipt, ...]:
        normalized_target = validate_target_key(
            target_key,
            error_type=EpisodeMemoryStoreError,
        )
        normalized_episode_id = _validate_episode_id(episode_id)
        payloads = _read_typed_records(
            paths=(
                self.promotion_write_receipt_path(
                    target_key=normalized_target,
                    episode_id=normalized_episode_id,
                ),
            ),
            target_key=normalized_target,
            episode_id=normalized_episode_id,
            parser=parse_episode_memory_promotion_write_receipt,
            label="episode-memory promotion write receipt",
        )
        return tuple(
            PersistedEpisodeMemoryPromotionWriteReceipt(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=episode_memory_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in payloads
        )

    def build_promotion_decision_view(
        self,
        *,
        target_key: str,
        episode_id: str,
    ) -> tuple[EpisodeMemoryPromotionDecisionView, ...]:
        return build_promotion_decision_view(
            intents=tuple(
                persisted.record
                for persisted in self.read_promotion_intents(
                    target_key=target_key,
                    episode_id=episode_id,
                )
            ),
            validation_receipts=tuple(
                persisted.record
                for persisted in self.read_promotion_validation_receipts(
                    target_key=target_key,
                    episode_id=episode_id,
                )
            ),
            write_receipts=tuple(
                persisted.record
                for persisted in self.read_promotion_write_receipts(
                    target_key=target_key,
                    episode_id=episode_id,
                )
            ),
        )

    def delta_path(self, *, target_key: str, episode_id: str) -> Path:
        root = self._memory_root(
            target_key=_validate_target(target_key),
            episode_id=_validate_episode_id(episode_id),
        )
        return (root / _DELTAS_FILE).resolve(strict=False)

    def no_update_path(self, *, target_key: str, episode_id: str) -> Path:
        root = self._memory_root(
            target_key=_validate_target(target_key),
            episode_id=_validate_episode_id(episode_id),
        )
        return (root / _NO_UPDATES_FILE).resolve(strict=False)

    def write_receipt_path(self, *, target_key: str, episode_id: str) -> Path:
        root = self._memory_root(
            target_key=_validate_target(target_key),
            episode_id=_validate_episode_id(episode_id),
        )
        return (root / _WRITE_RECEIPTS_FILE).resolve(strict=False)

    def promotion_intent_path(self, *, target_key: str, episode_id: str) -> Path:
        root = self._memory_root(
            target_key=_validate_target(target_key),
            episode_id=_validate_episode_id(episode_id),
        )
        return (root / _PROMOTION_INTENTS_FILE).resolve(strict=False)

    def promotion_validation_receipt_path(
        self,
        *,
        target_key: str,
        episode_id: str,
    ) -> Path:
        root = self._memory_root(
            target_key=_validate_target(target_key),
            episode_id=_validate_episode_id(episode_id),
        )
        return (root / _PROMOTION_VALIDATION_RECEIPTS_FILE).resolve(strict=False)

    def promotion_write_receipt_path(
        self,
        *,
        target_key: str,
        episode_id: str,
    ) -> Path:
        root = self._memory_root(
            target_key=_validate_target(target_key),
            episode_id=_validate_episode_id(episode_id),
        )
        return (root / _PROMOTION_WRITE_RECEIPTS_FILE).resolve(strict=False)

    def _memory_root(self, *, target_key: str, episode_id: str) -> Path:
        normalized_target = validate_target_key(
            target_key,
            error_type=EpisodeMemoryStoreError,
        )
        return (
            self._layout.research_memory_root
            / "targets"
            / normalized_target
            / "episodes"
            / _safe_episode_id_key(episode_id)
            / _MEMORY_DIR
        ).resolve(strict=False)


def _read_typed_records(
    *,
    paths: tuple[Path, ...],
    target_key: str,
    episode_id: str,
    parser: Callable[[Mapping[str, object]], _TRecord],
    label: str,
) -> tuple[tuple[_TRecord, Path, int], ...]:
    records: list[tuple[_TRecord, Path, int]] = []
    for payload, path, line_number in _read_payloads(
        paths=paths,
        target_key=target_key,
        episode_id=episode_id,
        label=label,
    ):
        try:
            records.append((parser(payload), path, line_number))
        except EpisodeMemoryContractError as exc:
            raise EpisodeMemoryStoreError(str(exc)) from exc
    return tuple(records)


def _read_payloads(
    *,
    paths: tuple[Path, ...],
    target_key: str,
    episode_id: str,
    label: str,
) -> tuple[tuple[Mapping[str, object], Path, int], ...]:
    normalized_target = validate_target_key(
        target_key,
        error_type=EpisodeMemoryStoreError,
    )
    records: list[tuple[Mapping[str, object], Path, int]] = []
    for path in paths:
        if not path.exists():
            continue
        if not path.is_file():
            raise EpisodeMemoryStoreError(f"{label} path must be a file: {path}")
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    raise EpisodeMemoryStoreError(
                        f"{label} line {line_number} is invalid JSON."
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise EpisodeMemoryStoreError(
                        f"{label} line {line_number} must be an object."
                    )
                if payload.get("target_key") != normalized_target:
                    raise EpisodeMemoryStoreError(
                        f"{label} shard contains mixed target_key records."
                    )
                if payload.get("episode_id") != episode_id:
                    raise EpisodeMemoryStoreError(
                        f"{label} shard contains mixed episode_id records."
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
        raise EpisodeMemoryStoreError(f"failed to append episode memory record: {path}") from exc


def _validate_target(value: str) -> str:
    return validate_target_key(value, error_type=EpisodeMemoryStoreError)


def _validate_episode_id(value: str) -> str:
    if not isinstance(value, str):
        raise EpisodeMemoryStoreError("episode_id must be a string.")
    normalized = value.strip()
    if not normalized:
        raise EpisodeMemoryStoreError("episode_id must be non-blank.")
    if normalized in {".", ".."}:
        raise EpisodeMemoryStoreError("episode_id must not be '.' or '..'.")
    if normalized != value:
        raise EpisodeMemoryStoreError("episode_id must not include whitespace padding.")
    if "/" in normalized or "\\" in normalized:
        raise EpisodeMemoryStoreError("episode_id must not include path separators.")
    return normalized


def _safe_episode_id_key(episode_id: str) -> str:
    normalized = _validate_episode_id(episode_id)
    digest = sha256(normalized.encode("utf-8")).hexdigest()
    return f"{_EPISODE_KEY_PREFIX}{digest}"


__all__ = [
    "EpisodeMemoryStoreError",
    "FileBackedEpisodeMemoryStore",
    "PersistedEpisodeMemoryDelta",
    "PersistedEpisodeMemoryNoUpdate",
    "PersistedEpisodeMemoryPromotionIntent",
    "PersistedEpisodeMemoryPromotionValidationReceipt",
    "PersistedEpisodeMemoryPromotionWriteReceipt",
    "PersistedEpisodeMemoryWriteReceipt",
]
