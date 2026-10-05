"""Contracts for append-only thesis revision audit snapshots."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, cast

from event_trader.contracts._validators import validate_event_id, validate_target_key
from event_trader.contracts.research_memory import resolve_page_ref
from event_trader.thesis_revision.canonical_bundle import (
    canonical_thesis_bundle_sections,
    canonical_thesis_page_sections,
)

ThesisRevisionSource = Literal[
    "analysis_commit",
    "migration_baseline",
    "active_price_basis_cutover",
]


class ThesisRevisionContractError(ValueError):
    """Raised when a thesis revision payload is malformed."""


@dataclass(frozen=True, slots=True)
class ThesisRevisionSectionSnapshot:
    page_path: str
    section_name: str
    content_md: str
    content_sha256: str

    def __post_init__(self) -> None:
        page_ref = resolve_page_ref(self.page_path)
        page_name = page_ref.page_path.split("/")[-1]
        allowed_sections = canonical_thesis_page_sections(page_name)
        if page_ref.target_key is None or not allowed_sections:
            raise ThesisRevisionContractError(
                "section snapshot page_path must be a canonical thesis bundle page."
            )
        object.__setattr__(self, "page_path", page_ref.page_path)
        object.__setattr__(
            self,
            "section_name",
            _validate_non_blank(self.section_name, "section_name"),
        )
        object.__setattr__(
            self,
            "content_sha256",
            _validate_sha256(self.content_sha256, "content_sha256"),
        )
        if self.section_name not in allowed_sections:
            raise ThesisRevisionContractError(
                "section snapshot section_name must belong to the canonical thesis bundle page."
            )
        if not isinstance(self.content_md, str):
            raise ThesisRevisionContractError("content_md must be a string.")

    @property
    def section_id(self) -> str:
        return f"{self.page_path}:{self.section_name}"

    def to_json_payload(self) -> dict[str, object]:
        return {
            "page_path": self.page_path,
            "section_name": self.section_name,
            "content_md": self.content_md,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class ThesisRevisionSectionDiff:
    page_path: str
    section_name: str
    previous_content_sha256: str
    content_sha256: str
    unified_diff_md: str

    def __post_init__(self) -> None:
        page_ref = resolve_page_ref(self.page_path)
        page_name = page_ref.page_path.split("/")[-1]
        allowed_sections = canonical_thesis_page_sections(page_name)
        if page_ref.target_key is None or not allowed_sections:
            raise ThesisRevisionContractError(
                "section diff page_path must be a canonical thesis bundle page."
            )
        object.__setattr__(self, "page_path", page_ref.page_path)
        object.__setattr__(
            self,
            "section_name",
            _validate_non_blank(self.section_name, "section_name"),
        )
        object.__setattr__(
            self,
            "previous_content_sha256",
            _validate_sha256(self.previous_content_sha256, "previous_content_sha256"),
        )
        object.__setattr__(
            self,
            "content_sha256",
            _validate_sha256(self.content_sha256, "content_sha256"),
        )
        if self.section_name not in allowed_sections:
            raise ThesisRevisionContractError(
                "section diff section_name must belong to the canonical thesis bundle page."
            )
        if not isinstance(self.unified_diff_md, str):
            raise ThesisRevisionContractError("unified_diff_md must be a string.")

    @property
    def section_id(self) -> str:
        return f"{self.page_path}:{self.section_name}"

    def to_json_payload(self) -> dict[str, object]:
        return {
            "page_path": self.page_path,
            "section_name": self.section_name,
            "previous_content_sha256": self.previous_content_sha256,
            "content_sha256": self.content_sha256,
            "unified_diff_md": self.unified_diff_md,
        }


@dataclass(frozen=True, slots=True)
class ThesisRevision:
    revision_id: str
    target_key: str
    business_at: datetime
    committed_at: datetime
    source: ThesisRevisionSource
    analysis_assessment_id: str | None
    context_packet_id: str | None
    context_packet_hash: str | None
    source_event_ids: tuple[str, ...]
    research_memory_write_receipt_ids: tuple[str, ...]
    changed_claim_ids: tuple[str, ...]
    sections: tuple[ThesisRevisionSectionSnapshot, ...]
    diffs_from_previous: tuple[ThesisRevisionSectionDiff, ...]
    key_claim_ids: tuple[str, ...]
    contested_prior_claim_ids: tuple[str, ...]
    price_level_role_ids: tuple[str, ...]
    previous_revision_id: str | None
    canonical_bundle_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "revision_id",
            _validate_non_blank(self.revision_id, "revision_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=ThesisRevisionContractError,
            ),
        )
        object.__setattr__(self, "business_at", _validate_datetime(self.business_at, "business_at"))
        object.__setattr__(
            self,
            "committed_at",
            _validate_datetime(self.committed_at, "committed_at"),
        )
        object.__setattr__(self, "source", _validate_source(self.source))
        object.__setattr__(
            self,
            "analysis_assessment_id",
            _validate_optional_non_blank(
                self.analysis_assessment_id,
                "analysis_assessment_id",
            ),
        )
        object.__setattr__(
            self,
            "context_packet_id",
            _validate_optional_non_blank(self.context_packet_id, "context_packet_id"),
        )
        object.__setattr__(
            self,
            "context_packet_hash",
            _validate_optional_sha256(self.context_packet_hash, "context_packet_hash"),
        )
        allow_empty_source_event_ids = self.source == "migration_baseline"
        allow_empty_receipt_ids = self.source == "migration_baseline"
        object.__setattr__(
            self,
            "source_event_ids",
            _validate_event_ids(
                self.source_event_ids,
                allow_empty=allow_empty_source_event_ids,
            ),
        )
        object.__setattr__(
            self,
            "research_memory_write_receipt_ids",
            _validate_string_tuple(
                self.research_memory_write_receipt_ids,
                "research_memory_write_receipt_ids",
                allow_empty=allow_empty_receipt_ids,
            ),
        )
        object.__setattr__(
            self,
            "changed_claim_ids",
            _validate_string_tuple(
                self.changed_claim_ids,
                "changed_claim_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "sections",
            _validate_sections(self.sections, self.target_key),
        )
        object.__setattr__(
            self,
            "diffs_from_previous",
            _validate_diffs(self.diffs_from_previous, self.target_key),
        )
        object.__setattr__(
            self,
            "key_claim_ids",
            _validate_string_tuple(self.key_claim_ids, "key_claim_ids", allow_empty=True),
        )
        object.__setattr__(
            self,
            "contested_prior_claim_ids",
            _validate_string_tuple(
                self.contested_prior_claim_ids,
                "contested_prior_claim_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "price_level_role_ids",
            _validate_string_tuple(
                self.price_level_role_ids,
                "price_level_role_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "previous_revision_id",
            _validate_optional_non_blank(self.previous_revision_id, "previous_revision_id"),
        )
        object.__setattr__(
            self,
            "canonical_bundle_sha256",
            _validate_sha256(self.canonical_bundle_sha256, "canonical_bundle_sha256"),
        )
        if self.source == "analysis_commit":
            if self.analysis_assessment_id is None:
                raise ThesisRevisionContractError(
                    "analysis_commit thesis revisions require analysis_assessment_id."
                )
            if self.context_packet_id is None or self.context_packet_hash is None:
                raise ThesisRevisionContractError(
                    "analysis_commit thesis revisions require context packet linkage."
                )
        elif self.source == "active_price_basis_cutover":
            if self.analysis_assessment_id is None:
                raise ThesisRevisionContractError(
                    "active_price_basis_cutover thesis revisions require "
                    "analysis_assessment_id."
                )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "committed_at": self.committed_at.isoformat(),
            "source": self.source,
            "analysis_assessment_id": self.analysis_assessment_id,
            "context_packet_id": self.context_packet_id,
            "context_packet_hash": self.context_packet_hash,
            "source_event_ids": list(self.source_event_ids),
            "research_memory_write_receipt_ids": list(self.research_memory_write_receipt_ids),
            "changed_claim_ids": list(self.changed_claim_ids),
            "sections": [section.to_json_payload() for section in self.sections],
            "diffs_from_previous": [
                diff.to_json_payload() for diff in self.diffs_from_previous
            ],
            "key_claim_ids": list(self.key_claim_ids),
            "contested_prior_claim_ids": list(self.contested_prior_claim_ids),
            "price_level_role_ids": list(self.price_level_role_ids),
            "previous_revision_id": self.previous_revision_id,
            "canonical_bundle_sha256": self.canonical_bundle_sha256,
        }


def parse_thesis_revision(payload: Mapping[str, object]) -> ThesisRevision:
    _require_exact_fields(
        payload,
        required={
            "revision_id",
            "target_key",
            "business_at",
            "committed_at",
            "source",
            "analysis_assessment_id",
            "context_packet_id",
            "context_packet_hash",
            "source_event_ids",
            "research_memory_write_receipt_ids",
            "changed_claim_ids",
            "sections",
            "diffs_from_previous",
            "key_claim_ids",
            "contested_prior_claim_ids",
            "price_level_role_ids",
            "previous_revision_id",
            "canonical_bundle_sha256",
        },
        surface="thesis_revision",
    )
    return ThesisRevision(
        revision_id=_require_text(payload, "revision_id"),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_datetime(payload.get("business_at"), "business_at"),
        committed_at=_parse_datetime(payload.get("committed_at"), "committed_at"),
        source=cast(ThesisRevisionSource, _require_text(payload, "source")),
        analysis_assessment_id=_optional_text(payload.get("analysis_assessment_id")),
        context_packet_id=_optional_text(payload.get("context_packet_id")),
        context_packet_hash=_optional_text(payload.get("context_packet_hash")),
        source_event_ids=tuple(_require_string_list(payload, "source_event_ids")),
        research_memory_write_receipt_ids=tuple(
            _require_string_list(payload, "research_memory_write_receipt_ids")
        ),
        changed_claim_ids=tuple(_require_string_list(payload, "changed_claim_ids")),
        sections=tuple(
            _parse_section_snapshot(item)
            for item in _require_mapping_list(payload, "sections")
        ),
        diffs_from_previous=tuple(
            _parse_section_diff(item)
            for item in _require_mapping_list(payload, "diffs_from_previous")
        ),
        key_claim_ids=tuple(_require_string_list(payload, "key_claim_ids")),
        contested_prior_claim_ids=tuple(
            _require_string_list(payload, "contested_prior_claim_ids")
        ),
        price_level_role_ids=tuple(_require_string_list(payload, "price_level_role_ids")),
        previous_revision_id=_optional_text(payload.get("previous_revision_id")),
        canonical_bundle_sha256=_require_text(payload, "canonical_bundle_sha256"),
    )


def thesis_revision_record_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _parse_section_snapshot(payload: Mapping[str, object]) -> ThesisRevisionSectionSnapshot:
    _require_exact_fields(
        payload,
        required={"page_path", "section_name", "content_md", "content_sha256"},
        surface="thesis_revision.sections[]",
    )
    content_md = payload.get("content_md")
    if not isinstance(content_md, str):
        raise ThesisRevisionContractError("thesis_revision.sections[].content_md must be a string.")
    return ThesisRevisionSectionSnapshot(
        page_path=_require_text(payload, "page_path"),
        section_name=_require_text(payload, "section_name"),
        content_md=content_md,
        content_sha256=_require_text(payload, "content_sha256"),
    )


def _parse_section_diff(payload: Mapping[str, object]) -> ThesisRevisionSectionDiff:
    _require_exact_fields(
        payload,
        required={
            "page_path",
            "section_name",
            "previous_content_sha256",
            "content_sha256",
            "unified_diff_md",
        },
        surface="thesis_revision.diffs_from_previous[]",
    )
    unified_diff_md = payload.get("unified_diff_md")
    if not isinstance(unified_diff_md, str):
        raise ThesisRevisionContractError(
            "thesis_revision.diffs_from_previous[].unified_diff_md must be a string."
        )
    return ThesisRevisionSectionDiff(
        page_path=_require_text(payload, "page_path"),
        section_name=_require_text(payload, "section_name"),
        previous_content_sha256=_require_text(payload, "previous_content_sha256"),
        content_sha256=_require_text(payload, "content_sha256"),
        unified_diff_md=unified_diff_md,
    )


def _validate_sections(
    sections: tuple[ThesisRevisionSectionSnapshot, ...],
    target_key: str,
) -> tuple[ThesisRevisionSectionSnapshot, ...]:
    expected = tuple(
        section.section_id for section in canonical_thesis_bundle_sections(target_key)
    )
    actual = tuple(section.section_id for section in sections)
    if actual != expected:
        raise ThesisRevisionContractError(
            "sections must contain the full canonical thesis bundle in canonical order."
        )
    return tuple(sections)


def _validate_diffs(
    diffs: tuple[ThesisRevisionSectionDiff, ...],
    target_key: str,
) -> tuple[ThesisRevisionSectionDiff, ...]:
    if not diffs:
        return ()
    expected = tuple(
        section.section_id for section in canonical_thesis_bundle_sections(target_key)
    )
    actual = tuple(diff.section_id for diff in diffs)
    if actual != expected:
        raise ThesisRevisionContractError(
            "diffs_from_previous must align to the canonical thesis bundle order."
        )
    return tuple(diffs)


def _validate_source(value: object) -> ThesisRevisionSource:
    if value not in {
        "analysis_commit",
        "migration_baseline",
        "active_price_basis_cutover",
    }:
        raise ThesisRevisionContractError(
            "source must be analysis_commit, migration_baseline, or "
            "active_price_basis_cutover."
        )
    return cast(ThesisRevisionSource, value)


def _validate_event_ids(values: tuple[str, ...], *, allow_empty: bool) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized and not allow_empty:
        raise ThesisRevisionContractError("source_event_ids must not be empty.")
    seen: set[str] = set()
    for value in normalized:
        validated = validate_event_id(value, error_type=ThesisRevisionContractError)
        if validated in seen:
            raise ThesisRevisionContractError("source_event_ids must not contain duplicates.")
        seen.add(validated)
    return normalized


def _validate_string_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized and not allow_empty:
        raise ThesisRevisionContractError(f"{field_name} must not be empty.")
    seen: set[str] = set()
    for value in normalized:
        text = _validate_non_blank(value, field_name)
        if text in seen:
            raise ThesisRevisionContractError(f"{field_name} must not contain duplicates.")
        seen.add(text)
    return normalized


def _validate_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ThesisRevisionContractError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ThesisRevisionContractError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


def _validate_sha256(value: object, field_name: str) -> str:
    text = _validate_non_blank(value, field_name)
    if len(text) != 64:
        raise ThesisRevisionContractError(f"{field_name} must be a sha256 digest.")
    return text


def _validate_optional_sha256(value: object, field_name: str) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    if len(text) != 64:
        raise ThesisRevisionContractError(f"{field_name} must be a sha256 digest when set.")
    return text


def _validate_non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ThesisRevisionContractError(f"{field_name} must be a non-blank string.")
    normalized = value.strip()
    if normalized != value:
        raise ThesisRevisionContractError(
            f"{field_name} must not include leading or trailing whitespace."
        )
    return normalized


def _validate_optional_non_blank(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_non_blank(value, field_name)


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_non_blank(payload.get(field_name), field_name)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ThesisRevisionContractError("optional text value must be a string.")
    normalized = value.strip()
    return normalized or None


def _require_string_list(payload: Mapping[str, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ThesisRevisionContractError(f"{field_name} must be a list of strings.")
    return value


def _require_mapping_list(
    payload: Mapping[str, object],
    field_name: str,
) -> list[Mapping[str, object]]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise ThesisRevisionContractError(f"{field_name} must be a list.")
    mappings: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ThesisRevisionContractError(f"{field_name} entries must be objects.")
        mappings.append(item)
    return mappings


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ThesisRevisionContractError(f"{field_name} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ThesisRevisionContractError(f"{field_name} must be an ISO timestamp.") from exc
    return _validate_datetime(parsed, field_name)


def _require_exact_fields(
    payload: Mapping[str, object],
    *,
    required: set[str],
    surface: str,
) -> None:
    actual = set(str(key) for key in payload)
    missing = sorted(required - actual)
    unexpected = sorted(actual - required)
    if missing or unexpected:
        problems: list[str] = []
        if missing:
            problems.append(f"missing fields: {', '.join(missing)}")
        if unexpected:
            problems.append(f"unexpected fields: {', '.join(unexpected)}")
        raise ThesisRevisionContractError(f"{surface} " + "; ".join(problems))


__all__ = [
    "ThesisRevision",
    "ThesisRevisionContractError",
    "ThesisRevisionSectionDiff",
    "ThesisRevisionSectionSnapshot",
    "ThesisRevisionSource",
    "parse_thesis_revision",
    "thesis_revision_record_hash",
]
