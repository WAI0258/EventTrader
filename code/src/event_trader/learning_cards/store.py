"""Append-only file-backed store for structured learning cards."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from event_trader.contracts._validators import validate_scope_key, validate_target_key
from event_trader.learning_cards.contracts import (
    LearningCard,
    LearningCardContractError,
    learning_card_record_hash,
    parse_learning_card,
)
from event_trader.storage import WorkspaceLayout

_LEARNING_CARDS_DIR = Path("learning_cards")
_CARDS_FILE = "cards.jsonl"


class LearningCardStoreError(ValueError):
    """Raised when learning-card storage is invalid or conflicting."""


@dataclass(frozen=True, slots=True)
class PersistedLearningCard:
    record: LearningCard
    path: Path
    line_number: int
    record_hash: str


class FileBackedLearningCardStore:
    """Append and read structured learning cards under helpers."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise LearningCardStoreError("layout must be a WorkspaceLayout instance.")
        self._layout = layout

    def append_card(self, card: LearningCard) -> Path:
        if not isinstance(card, LearningCard):
            raise LearningCardStoreError("card must be a LearningCard.")
        path = self.cards_path()
        card_hash = learning_card_record_hash(card.to_json_payload())
        for persisted in self.read_all_cards():
            if persisted.record.card_id != card.card_id:
                continue
            if persisted.record_hash == card_hash:
                return path
            raise LearningCardStoreError("duplicate card_id has different payload.")
        _append_json_line(path, card.to_json_payload())
        return path

    def read_all_cards(self) -> tuple[PersistedLearningCard, ...]:
        return _read_cards_from_path(self.cards_path())

    def read_cards(
        self,
        *,
        scope_key: str | None = None,
        target_key: str | None = None,
    ) -> tuple[PersistedLearningCard, ...]:
        normalized_scope = (
            validate_scope_key(scope_key, error_type=LearningCardStoreError)
            if scope_key is not None
            else None
        )
        normalized_target = (
            validate_target_key(target_key, error_type=LearningCardStoreError)
            if target_key is not None
            else None
        )
        return tuple(
            persisted
            for persisted in self.read_all_cards()
            if (normalized_scope is None or persisted.record.scope_key == normalized_scope)
            and (normalized_target is None or persisted.record.target_key == normalized_target)
        )

    def cards_path(self) -> Path:
        return (
            self._layout.helpers_root / _LEARNING_CARDS_DIR / _CARDS_FILE
        ).resolve(strict=False)


def _read_cards_from_path(path: Path) -> tuple[PersistedLearningCard, ...]:
    if not path.exists():
        return ()
    if not path.is_file():
        raise LearningCardStoreError(f"learning-card path must be a file: {path}")
    records: list[PersistedLearningCard] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            normalized = line.strip()
            if not normalized:
                continue
            try:
                payload = json.loads(normalized)
            except json.JSONDecodeError as exc:
                raise LearningCardStoreError(
                    f"learning-card line {line_number} is invalid JSON."
                ) from exc
            if not isinstance(payload, Mapping):
                raise LearningCardStoreError(
                    f"learning-card line {line_number} must be an object."
                )
            try:
                card = parse_learning_card(payload)
            except LearningCardContractError as exc:
                raise LearningCardStoreError(str(exc)) from exc
            records.append(
                PersistedLearningCard(
                    record=card,
                    path=path,
                    line_number=line_number,
                    record_hash=learning_card_record_hash(card.to_json_payload()),
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
        raise LearningCardStoreError(f"failed to append learning-card record: {path}") from exc


__all__ = [
    "FileBackedLearningCardStore",
    "LearningCardStoreError",
    "PersistedLearningCard",
]
