"""Archived market-news source records."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from event_trader.contracts._validators import (
    format_identity_timestamp,
    normalize_content,
    validate_labels,
    validate_source_ref,
    validate_target_key,
    validate_timestamp,
)
from event_trader.feeds.market_news import MarketNewsArticle
from event_trader.feeds.models import LiveNewsStreamInput
from event_trader.source_policy import (
    OPERATOR_CONFIDENCE_LABEL_PREFIX,
    OPERATOR_SOURCE_BASIS_LABEL_PREFIX,
)
from event_trader.storage import WorkspaceLayout


class MarketNewsArchiveError(ValueError):
    """Raised when archived market-news writes or reads are invalid."""


_RETIRED_OPERATOR_BASIS_LABEL_PREFIX = OPERATOR_SOURCE_BASIS_LABEL_PREFIX.removeprefix(
    "operator_"
)
_FORBIDDEN_OPERATOR_ONLY_LABEL_PREFIXES = (
    OPERATOR_CONFIDENCE_LABEL_PREFIX,
    OPERATOR_SOURCE_BASIS_LABEL_PREFIX,
    _RETIRED_OPERATOR_BASIS_LABEL_PREFIX,
)


@dataclass(frozen=True, slots=True)
class MarketNewsTargetLayout:
    """Resolved helper paths for one target's archived market-news records."""

    target_key: str
    target_root: Path

    def day_partition(self, visible_at: date | datetime) -> Path:
        resolved_day = _normalize_partition_day(visible_at)
        return (self.target_root / f"{resolved_day.isoformat()}.jsonl").resolve(
            strict=False
        )


@dataclass(frozen=True, slots=True)
class MarketNewsArchiveRecord:
    """One canonical archived market-news article."""

    target_key: str
    provider: str
    article_id: str
    source: str
    source_ref: str
    headline: str
    summary: str
    content_html: str
    content_text: str
    labels: list[str]
    symbols: tuple[str, ...]
    raw_payload: Mapping[str, object]
    created_at: datetime
    updated_at: datetime | None
    captured_at: datetime
    visible_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=MarketNewsArchiveError),
        )
        object.__setattr__(self, "provider", _validate_non_blank_text(self.provider, "provider"))
        object.__setattr__(
            self,
            "article_id",
            _validate_non_blank_text(self.article_id, "article_id"),
        )
        object.__setattr__(self, "source", "" if self.source is None else str(self.source).strip())
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=MarketNewsArchiveError),
        )
        object.__setattr__(self, "headline", _validate_non_blank_text(self.headline, "headline"))
        object.__setattr__(
            self,
            "summary",
            "" if self.summary is None else str(self.summary).strip(),
        )
        object.__setattr__(
            self,
            "content_html",
            "" if self.content_html is None else str(self.content_html).strip(),
        )
        object.__setattr__(
            self,
            "content_text",
            normalize_content(
                self.content_text,
                field_name="content_text",
                error_type=MarketNewsArchiveError,
            ),
        )
        object.__setattr__(
            self,
            "labels",
            validate_labels(self.labels, error_type=MarketNewsArchiveError),
        )
        _reject_operator_only_labels(self.labels)
        object.__setattr__(
            self,
            "symbols",
            tuple(_validate_non_blank_text(symbol, "symbols") for symbol in self.symbols),
        )
        if not isinstance(self.raw_payload, Mapping):
            raise MarketNewsArchiveError("raw_payload must be a JSON object.")
        object.__setattr__(self, "raw_payload", dict(self.raw_payload))
        created_at = validate_timestamp(
            self.created_at,
            field_name="created_at",
            error_type=MarketNewsArchiveError,
        )
        object.__setattr__(self, "created_at", created_at)
        updated_at = None
        if self.updated_at is not None:
            updated_at = validate_timestamp(
                self.updated_at,
                field_name="updated_at",
                error_type=MarketNewsArchiveError,
            )
            object.__setattr__(
                self,
                "updated_at",
                updated_at,
            )
        captured_at = validate_timestamp(
            self.captured_at,
            field_name="captured_at",
            error_type=MarketNewsArchiveError,
        )
        visible_at = validate_timestamp(
            self.visible_at,
            field_name="visible_at",
            error_type=MarketNewsArchiveError,
        )
        if visible_at < created_at:
            raise MarketNewsArchiveError(
                "visible_at must be greater than or equal to created_at."
            )
        if updated_at is not None and visible_at < updated_at:
            raise MarketNewsArchiveError(
                "visible_at must be greater than or equal to updated_at."
            )
        object.__setattr__(self, "captured_at", captured_at)
        object.__setattr__(self, "visible_at", visible_at)


@dataclass(frozen=True, slots=True)
class MarketNewsArchiveWriteReceipt:
    """Observable result from one archived market-news write."""

    status: Literal["written", "noop_existing"]
    record_id: str
    partition_path: Path


def build_market_news_target_layout(
    layout: WorkspaceLayout,
    target_key: str,
) -> MarketNewsTargetLayout:
    if not isinstance(layout, WorkspaceLayout):
        raise MarketNewsArchiveError("layout must be a WorkspaceLayout instance.")
    validated_target_key = validate_target_key(
        target_key,
        error_type=MarketNewsArchiveError,
    )
    return MarketNewsTargetLayout(
        target_key=validated_target_key,
        target_root=(
            layout.helpers_root / "source_archive" / "market_news" / validated_target_key
        ).resolve(strict=False),
    )


def market_news_partition_path(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    visible_at: date | datetime,
) -> Path:
    return build_market_news_target_layout(layout, target_key).day_partition(visible_at)


def market_news_record_id(record: MarketNewsArchiveRecord) -> str:
    if not isinstance(record, MarketNewsArchiveRecord):
        raise MarketNewsArchiveError("record must be a MarketNewsArchiveRecord instance.")
    identity_material = "\n".join(
        (
            record.target_key,
            record.provider,
            record.source_ref,
            format_identity_timestamp(record.visible_at),
            record.content_text,
        )
    )
    return sha256(identity_material.encode("utf-8")).hexdigest()


def append_market_news_record(
    layout: WorkspaceLayout,
    *,
    record: MarketNewsArchiveRecord,
) -> MarketNewsArchiveWriteReceipt:
    if not isinstance(layout, WorkspaceLayout):
        raise MarketNewsArchiveError("layout must be a WorkspaceLayout instance.")
    if not isinstance(record, MarketNewsArchiveRecord):
        raise MarketNewsArchiveError("record must be a MarketNewsArchiveRecord instance.")
    partition_path = market_news_partition_path(
        layout,
        target_key=record.target_key,
        visible_at=record.visible_at,
    )
    record_id = market_news_record_id(record)
    payload = _serialize_record(record)
    for existing_payload in _iter_partition_payloads(partition_path):
        existing_record = _record_from_payload(existing_payload)
        if market_news_record_id(existing_record) != record_id:
            continue
        if existing_payload == payload:
            return MarketNewsArchiveWriteReceipt(
                status="noop_existing",
                record_id=record_id,
                partition_path=partition_path,
            )
        raise MarketNewsArchiveError(
            "market_news partition already contains the same identity with a "
            f"different payload: record_id={record_id} partition_path={partition_path}."
        )

    partition_path.parent.mkdir(parents=True, exist_ok=True)
    payload_line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
    try:
        with partition_path.open("ab") as handle:
            handle.write(payload_line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise MarketNewsArchiveError(
            f"Failed to append market_news record {record_id} to {partition_path}: {exc}"
        ) from exc
    return MarketNewsArchiveWriteReceipt(
        status="written",
        record_id=record_id,
        partition_path=partition_path,
    )


def map_live_market_news_to_archive_record(
    *,
    target_key: str,
    article: MarketNewsArticle,
    ingress_input: LiveNewsStreamInput,
    labels: list[str],
    provider: str = "bc_private_v1",
) -> MarketNewsArchiveRecord:
    if not isinstance(article, MarketNewsArticle):
        raise MarketNewsArchiveError("article must be a MarketNewsArticle instance.")
    if not isinstance(ingress_input, LiveNewsStreamInput):
        raise MarketNewsArchiveError("ingress_input must be a LiveNewsStreamInput instance.")
    return MarketNewsArchiveRecord(
        target_key=target_key,
        provider=provider,
        article_id=article.article_id,
        source=article.source,
        source_ref=ingress_input.source_ref,
        headline=article.headline,
        summary=article.summary,
        content_html=article.content_html,
        content_text=ingress_input.body,
        labels=list(labels),
        symbols=article.symbols,
        raw_payload=article.raw_payload,
        created_at=article.created_at,
        updated_at=article.updated_at,
        captured_at=ingress_input.captured_at,
        visible_at=ingress_input.captured_at,
    )


def write_live_market_news_record(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    article: MarketNewsArticle,
    ingress_input: LiveNewsStreamInput,
    labels: list[str],
    provider: str = "bc_private_v1",
) -> MarketNewsArchiveWriteReceipt:
    record = map_live_market_news_to_archive_record(
        target_key=target_key,
        article=article,
        ingress_input=ingress_input,
        labels=labels,
        provider=provider,
    )
    return append_market_news_record(layout, record=record)


def read_market_news_records(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
) -> tuple[MarketNewsArchiveRecord, ...]:
    """Read archived market-news records in one visible_at window."""
    if not isinstance(layout, WorkspaceLayout):
        raise MarketNewsArchiveError("layout must be a WorkspaceLayout instance.")
    validated_target_key = validate_target_key(
        target_key,
        error_type=MarketNewsArchiveError,
    )
    validated_start_at = validate_timestamp(
        start_at,
        field_name="start_at",
        error_type=MarketNewsArchiveError,
    )
    validated_end_at = validate_timestamp(
        end_at,
        field_name="end_at",
        error_type=MarketNewsArchiveError,
    )
    if validated_end_at < validated_start_at:
        raise MarketNewsArchiveError("end_at must be greater than or equal to start_at.")

    target_layout = build_market_news_target_layout(layout, validated_target_key)
    if not target_layout.target_root.exists():
        return ()
    if not target_layout.target_root.is_dir():
        raise MarketNewsArchiveError(
            f"market_news target root must be a directory: {target_layout.target_root}"
        )

    records: list[MarketNewsArchiveRecord] = []
    for partition_path in sorted(target_layout.target_root.glob("*.jsonl")):
        for payload in _iter_partition_payloads(partition_path):
            record = _record_from_payload(payload)
            if record.visible_at < validated_start_at:
                continue
            if record.visible_at > validated_end_at:
                continue
            records.append(record)
    return tuple(sorted(records, key=lambda record: record.visible_at))


def _serialize_record(record: MarketNewsArchiveRecord) -> dict[str, object]:
    return {
        "target_key": record.target_key,
        "provider": record.provider,
        "article_id": record.article_id,
        "source": record.source,
        "source_ref": record.source_ref,
        "headline": record.headline,
        "summary": record.summary,
        "content_html": record.content_html,
        "content_text": record.content_text,
        "labels": list(record.labels),
        "symbols": list(record.symbols),
        "raw_payload": dict(record.raw_payload),
        "created_at": record.created_at.isoformat(),
        "updated_at": None if record.updated_at is None else record.updated_at.isoformat(),
        "captured_at": record.captured_at.isoformat(),
        "visible_at": record.visible_at.isoformat(),
    }


def _record_from_payload(payload: object) -> MarketNewsArchiveRecord:
    if not isinstance(payload, dict):
        raise MarketNewsArchiveError("market_news partition lines must be JSON objects.")
    return MarketNewsArchiveRecord(
        target_key=_require_string(payload, "target_key"),
        provider=_require_string(payload, "provider"),
        article_id=_require_string(payload, "article_id"),
        source=_require_string(payload, "source"),
        source_ref=_require_string(payload, "source_ref"),
        headline=_require_string(payload, "headline"),
        summary=_require_string(payload, "summary"),
        content_html=_require_string(payload, "content_html"),
        content_text=_require_string(payload, "content_text"),
        labels=_require_string_list(payload, "labels"),
        symbols=tuple(_require_string_list(payload, "symbols")),
        raw_payload=_optional_mapping(payload, "raw_payload"),
        created_at=_parse_timestamp(payload, "created_at"),
        updated_at=(
            None
            if payload.get("updated_at") is None
            else _parse_timestamp(payload, "updated_at")
        ),
        captured_at=_parse_timestamp(payload, "captured_at"),
        visible_at=_parse_timestamp(payload, "visible_at"),
    )


def _iter_partition_payloads(partition_path: Path) -> tuple[dict[str, object], ...]:
    if not partition_path.exists():
        return ()
    if not partition_path.is_file():
        raise MarketNewsArchiveError(
            f"market_news partition must be a file: {partition_path}"
        )
    payloads: list[dict[str, object]] = []
    for line_number, line in enumerate(
        partition_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MarketNewsArchiveError(
                f"market_news partition contains invalid JSON: {partition_path}:{line_number}"
            ) from exc
        if not isinstance(payload, dict):
            raise MarketNewsArchiveError(
                f"market_news partition lines must be JSON objects: {partition_path}:{line_number}"
            )
        payloads.append(payload)
    return tuple(payloads)


def _normalize_partition_day(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return validate_timestamp(
            value,
            field_name="visible_at",
            error_type=MarketNewsArchiveError,
        ).date()
    if isinstance(value, date):
        return value
    raise MarketNewsArchiveError("partition value must be a date or datetime.")


def _validate_non_blank_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MarketNewsArchiveError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _require_string(payload: dict[object, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise MarketNewsArchiveError(f"{field_name} must be a string.")
    return value


def _require_string_list(payload: dict[object, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MarketNewsArchiveError(f"{field_name} must be an array of strings.")
    return value


def _optional_mapping(payload: dict[object, object], field_name: str) -> dict[str, object]:
    value = payload.get(field_name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MarketNewsArchiveError(f"{field_name} must be a JSON object.")
    return {str(key): item for key, item in value.items()}


def _parse_timestamp(payload: dict[object, object], field_name: str) -> datetime:
    value = _require_string(payload, field_name)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MarketNewsArchiveError(f"{field_name} must be an ISO8601 timestamp.") from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=MarketNewsArchiveError,
    )


def _reject_operator_only_labels(labels: list[str]) -> None:
    if any(label.startswith(_FORBIDDEN_OPERATOR_ONLY_LABEL_PREFIXES) for label in labels):
        raise MarketNewsArchiveError(
            "market_news labels must not contain operator-only metadata labels."
        )


__all__ = [
    "MarketNewsArchiveError",
    "MarketNewsArchiveRecord",
    "MarketNewsArchiveWriteReceipt",
    "append_market_news_record",
    "build_market_news_target_layout",
    "map_live_market_news_to_archive_record",
    "market_news_partition_path",
    "market_news_record_id",
    "read_market_news_records",
    "write_live_market_news_record",
]
