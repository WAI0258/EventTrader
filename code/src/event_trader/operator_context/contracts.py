"""Contracts and receipts for the operator-owned `operator.md` surface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

from event_trader.contracts._validators import normalize_content, validate_target_key
from event_trader.contracts.research_memory import resolve_page_ref

OperatorContextWriteMode = Literal[
    "append_context_card",
    "retire_context_card",
    "rewrite_operator_page",
]


class OperatorContextContractError(ValueError):
    """Raised when an operator-context contract or receipt is malformed."""


class OperatorContextWriteModeError(OperatorContextContractError):
    """Raised when an operator-context write mode is unknown."""


@dataclass(frozen=True, slots=True)
class OperatorContextCard:
    """One human-authored operator context block."""

    card_id: str
    section_name: str
    body: str
    is_retired: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "card_id",
            _normalize_text(self.card_id, field_name="card_id", allow_blank=False),
        )
        object.__setattr__(
            self,
            "section_name",
            _normalize_text(
                self.section_name,
                field_name="section_name",
                allow_blank=False,
            ),
        )
        object.__setattr__(
            self,
            "body",
            _normalize_text(self.body, field_name="body", allow_blank=False),
        )
        if not isinstance(self.is_retired, bool):
            raise OperatorContextContractError("is_retired must be a boolean.")


@dataclass(frozen=True, slots=True)
class OperatorContextWriteReceipt:
    """Receipt for one dedicated operator-context write."""

    receipt_id: str
    target_key: str
    page_path: str
    section_name: str
    write_mode: OperatorContextWriteMode
    operator_command_id: str
    authoritative_write_intent: bool
    business_at: datetime
    committed_at: datetime
    before_sha256: str
    after_sha256: str
    changed_card_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        page_ref = resolve_page_ref(self.page_path)
        if page_ref.page_kind != "operator":
            raise OperatorContextContractError(
                "page_path must resolve to an operator page."
            )
        target_key = validate_target_key(
            self.target_key,
            error_type=OperatorContextContractError,
        )
        if page_ref.target_key != target_key:
            raise OperatorContextContractError(
                "target_key must match the target resolved from page_path."
            )
        if self.write_mode not in {
            "append_context_card",
            "retire_context_card",
            "rewrite_operator_page",
        }:
            raise OperatorContextWriteModeError(
                "write_mode must be one of append_context_card, "
                "retire_context_card, or rewrite_operator_page."
            )
        if self.authoritative_write_intent is not True:
            raise OperatorContextContractError(
                "authoritative_write_intent must be True for operator-context writes."
            )
        object.__setattr__(
            self,
            "section_name",
            _normalize_text(
                self.section_name,
                field_name="section_name",
                allow_blank=False,
            ),
        )
        object.__setattr__(
            self,
            "operator_command_id",
            _normalize_text(
                self.operator_command_id,
                field_name="operator_command_id",
                allow_blank=False,
            ),
        )
        object.__setattr__(
            self,
            "business_at",
            _normalize_datetime(self.business_at, field_name="business_at"),
        )
        object.__setattr__(
            self,
            "committed_at",
            _normalize_datetime(self.committed_at, field_name="committed_at"),
        )
        object.__setattr__(self, "target_key", target_key)
        object.__setattr__(self, "before_sha256", _normalize_sha(self.before_sha256))
        object.__setattr__(self, "after_sha256", _normalize_sha(self.after_sha256))
        object.__setattr__(
            self,
            "changed_card_ids",
            _dedupe_values(
                _normalize_text(
                    value,
                    field_name="changed_card_id",
                    allow_blank=False,
                )
                for value in self.changed_card_ids
            ),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "target_key": self.target_key,
            "page_path": self.page_path,
            "section_name": self.section_name,
            "write_mode": self.write_mode,
            "operator_command_id": self.operator_command_id,
            "authoritative_write_intent": self.authoritative_write_intent,
            "business_at": self.business_at.isoformat(),
            "committed_at": self.committed_at.isoformat(),
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "changed_card_ids": list(self.changed_card_ids),
        }


def make_operator_context_receipt_id(
    *,
    section_name: str,
    write_mode: OperatorContextWriteMode,
    target_key: str,
    operator_command_id: str,
    committed_at: datetime,
) -> str:
    normalized_section = _normalize_text(
        section_name,
        field_name="section_name",
        allow_blank=False,
    )
    normalized_target_key = validate_target_key(
        target_key,
        error_type=OperatorContextContractError,
    )
    normalized_command_id = _normalize_text(
        operator_command_id,
        field_name="operator_command_id",
        allow_blank=False,
    )
    identity = {
        "section_name": normalized_section,
        "write_mode": write_mode,
        "target_key": normalized_target_key,
        "operator_command_id": normalized_command_id,
        "committed_at": _normalize_datetime(
            committed_at,
            field_name="committed_at",
        ).isoformat(),
    }
    digest = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"operator-context-write:{digest}"


def _normalize_text(value: object, *, field_name: str, allow_blank: bool) -> str:
    if not isinstance(value, str):
        raise OperatorContextContractError(f"{field_name} must be a string.")
    if allow_blank:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    else:
        normalized = normalize_content(
            value,
            field_name=field_name,
            error_type=OperatorContextContractError,
        ).strip()
    if not allow_blank and not normalized:
        raise OperatorContextContractError(f"{field_name} must not be blank.")
    return normalized


def _normalize_datetime(value: datetime | None, *, field_name: str) -> datetime:
    if value is None:
        raise OperatorContextContractError(f"{field_name} must be a timezone-aware datetime.")
    if not isinstance(value, datetime):
        raise OperatorContextContractError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise OperatorContextContractError(
            f"{field_name} must be a timezone-aware datetime."
        )
    return value.astimezone(UTC)


def _normalize_sha(value: str) -> str:
    normalized = _normalize_text(value, field_name="sha", allow_blank=False)
    if len(normalized) != 64:
        raise OperatorContextContractError("sha must be a 64-character SHA-256 digest.")
    if any(ch not in "0123456789abcdefABCDEF" for ch in normalized):
        raise OperatorContextContractError("sha must be hexadecimal.")
    return normalized.lower()


def _dedupe_values(values: object) -> tuple[str, ...]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return tuple(deduped)


__all__ = [
    "OperatorContextCard",
    "OperatorContextContractError",
    "OperatorContextWriteMode",
    "OperatorContextWriteModeError",
    "OperatorContextWriteReceipt",
    "make_operator_context_receipt_id",
]
