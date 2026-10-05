"""Append-only quarantine records for archived source events."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from event_trader.contracts._validators import (
    validate_event_id,
    validate_source_ref,
    validate_target_key,
    validate_timestamp,
)
from event_trader.feeds.historical_web_search_guard import (
    detect_historical_web_search_future_leakage,
)
from event_trader.feeds.models import LiveNewsStreamInput, LiveWebSearchInput
from event_trader.ingest.admission import derive_admission_event_id, validate_admission_request
from event_trader.replay.admissibility import (
    ReplayAdmissibilityError,
    validate_replay_admission_request,
)
from event_trader.source_archive.market_news import (
    MarketNewsArchiveRecord,
    market_news_record_id,
    read_market_news_records,
)
from event_trader.source_archive.web_search import (
    HistoricalWebSearchRecord,
    historical_web_search_candidate_fingerprint,
    historical_web_search_observation_id,
    read_historical_web_search_records,
)
from event_trader.storage import WorkspaceLayout

SourceQuarantineKind = Literal["market_news", "web_search"]
_READ_FLOOR = datetime.min.replace(tzinfo=UTC)
_QUARANTINE_PAYLOAD_FIELDS = frozenset(
    {
        "target_key",
        "source_kind",
        "requested_event_id",
        "observation_id",
        "record_id",
        "candidate_fingerprint",
        "live_event_id",
        "replay_event_id",
        "source_ref",
        "title",
        "release_at",
        "observed_at",
        "reason",
        "created_at",
    }
)


class SourceEventQuarantineError(ValueError):
    """Raised when source-event quarantine data is malformed."""


@dataclass(frozen=True, slots=True)
class SourceEventIdentities:
    """Deterministic identities for one archived source record."""

    target_key: str
    source_kind: SourceQuarantineKind
    observation_id: str
    candidate_fingerprint: str | None
    live_event_id: str
    replay_event_id: str | None
    source_ref: str
    title: str
    release_at: datetime
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=SourceEventQuarantineError,
            ),
        )
        if self.source_kind not in {"market_news", "web_search"}:
            raise SourceEventQuarantineError(
                "source_kind must be 'market_news' or 'web_search'."
            )
        for field_name in ("observation_id", "live_event_id", "title"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise SourceEventQuarantineError(f"{field_name} must not be blank.")
        if self.replay_event_id is not None and (
            not isinstance(self.replay_event_id, str) or not self.replay_event_id.strip()
        ):
            raise SourceEventQuarantineError("replay_event_id must not be blank.")
        if self.candidate_fingerprint is not None and (
            not isinstance(self.candidate_fingerprint, str)
            or not self.candidate_fingerprint.strip()
        ):
            raise SourceEventQuarantineError("candidate_fingerprint must not be blank.")
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(
                self.source_ref,
                error_type=SourceEventQuarantineError,
            ),
        )
        object.__setattr__(
            self,
            "release_at",
            validate_timestamp(
                self.release_at,
                field_name="release_at",
                error_type=SourceEventQuarantineError,
            ).astimezone(UTC),
        )
        object.__setattr__(
            self,
            "observed_at",
            validate_timestamp(
                self.observed_at,
                field_name="observed_at",
                error_type=SourceEventQuarantineError,
            ).astimezone(UTC),
        )


@dataclass(frozen=True, slots=True)
class SourceEventQuarantineRecord:
    """One append-only quarantine tombstone for an archived source record."""

    target_key: str
    source_kind: SourceQuarantineKind
    requested_event_id: str
    observation_id: str
    candidate_fingerprint: str | None
    live_event_id: str
    replay_event_id: str | None
    source_ref: str
    title: str
    release_at: datetime
    observed_at: datetime
    reason: str
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=SourceEventQuarantineError,
            ),
        )
        if self.source_kind not in {"market_news", "web_search"}:
            raise SourceEventQuarantineError(
                "source_kind must be 'market_news' or 'web_search'."
            )
        for field_name in ("requested_event_id", "live_event_id"):
            object.__setattr__(
                self,
                field_name,
                validate_event_id(
                    getattr(self, field_name),
                    error_type=SourceEventQuarantineError,
                ),
            )
        if self.replay_event_id is not None:
            object.__setattr__(
                self,
                "replay_event_id",
                validate_event_id(
                    self.replay_event_id,
                    error_type=SourceEventQuarantineError,
                ),
            )
        for field_name in ("observation_id", "title"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise SourceEventQuarantineError(f"{field_name} must not be blank.")
        if self.candidate_fingerprint is not None and (
            not isinstance(self.candidate_fingerprint, str)
            or not self.candidate_fingerprint.strip()
        ):
            raise SourceEventQuarantineError("candidate_fingerprint must not be blank.")
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(
                self.source_ref,
                error_type=SourceEventQuarantineError,
            ),
        )
        object.__setattr__(
            self,
            "release_at",
            validate_timestamp(
                self.release_at,
                field_name="release_at",
                error_type=SourceEventQuarantineError,
            ).astimezone(UTC),
        )
        object.__setattr__(
            self,
            "observed_at",
            validate_timestamp(
                self.observed_at,
                field_name="observed_at",
                error_type=SourceEventQuarantineError,
            ).astimezone(UTC),
        )
        object.__setattr__(
            self,
            "created_at",
            validate_timestamp(
                self.created_at,
                field_name="created_at",
                error_type=SourceEventQuarantineError,
            ).astimezone(UTC),
        )
        normalized_reason = self.reason.strip() if isinstance(self.reason, str) else ""
        if not normalized_reason:
            raise SourceEventQuarantineError("reason must not be blank.")
        object.__setattr__(self, "reason", normalized_reason)

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "source_kind": self.source_kind,
            "requested_event_id": self.requested_event_id,
            "observation_id": self.observation_id,
            "record_id": self.observation_id,
            "candidate_fingerprint": self.candidate_fingerprint,
            "live_event_id": self.live_event_id,
            "replay_event_id": self.replay_event_id,
            "source_ref": self.source_ref,
            "title": self.title,
            "release_at": self.release_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SourceEventQuarantineWriteReceipt:
    """Observable result from one quarantine append."""

    status: Literal["written", "noop_existing"]
    path: Path
    observation_id: str

    def __post_init__(self) -> None:
        if self.status not in {"written", "noop_existing"}:
            raise SourceEventQuarantineError(
                "status must be 'written' or 'noop_existing'."
            )
        if not isinstance(self.path, Path):
            raise SourceEventQuarantineError("path must be a pathlib.Path.")
        if not isinstance(self.observation_id, str) or not self.observation_id.strip():
            raise SourceEventQuarantineError("observation_id must not be blank.")


def read_source_event_quarantine_records(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    source_kind: SourceQuarantineKind | None = None,
) -> tuple[SourceEventQuarantineRecord, ...]:
    """Read append-only quarantine records for one target."""
    if not isinstance(layout, WorkspaceLayout):
        raise SourceEventQuarantineError("layout must be a WorkspaceLayout instance.")
    validated_target_key = validate_target_key(
        target_key,
        error_type=SourceEventQuarantineError,
    )
    if source_kind is not None and source_kind not in {"market_news", "web_search"}:
        raise SourceEventQuarantineError(
            "source_kind must be 'market_news' or 'web_search' when provided."
        )
    root = _target_root(layout, validated_target_key)
    if not root.exists():
        return ()
    records: list[SourceEventQuarantineRecord] = []
    for path in sorted(root.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    raise SourceEventQuarantineError(
                        f"Invalid JSON in quarantine file {path} line {line_number}."
                    ) from exc
                record = _record_from_payload(payload)
                if source_kind is not None and record.source_kind != source_kind:
                    continue
                records.append(record)
    return tuple(records)


def append_source_event_quarantine(
    layout: WorkspaceLayout,
    *,
    record: SourceEventQuarantineRecord,
) -> SourceEventQuarantineWriteReceipt:
    """Append one quarantine record unless the archived source row is already quarantined."""
    if not isinstance(layout, WorkspaceLayout):
        raise SourceEventQuarantineError("layout must be a WorkspaceLayout instance.")
    if not isinstance(record, SourceEventQuarantineRecord):
        raise SourceEventQuarantineError(
            "record must be a SourceEventQuarantineRecord instance."
        )
    existing = read_source_event_quarantine_records(
        layout,
        target_key=record.target_key,
        source_kind=record.source_kind,
    )
    if any(item.observation_id == record.observation_id for item in existing):
        return SourceEventQuarantineWriteReceipt(
            status="noop_existing",
            path=_partition_path(layout, record.target_key, record.created_at),
            observation_id=record.observation_id,
        )
    path = _partition_path(layout, record.target_key, record.created_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record.to_json_payload(), ensure_ascii=False))
        handle.write("\n")
    return SourceEventQuarantineWriteReceipt(
        status="written",
        path=path,
        observation_id=record.observation_id,
    )


def normalize_source_event_quarantine_payload(
    layout: WorkspaceLayout,
    payload: object,
) -> dict[str, object]:
    """Normalize one persisted quarantine payload into the canonical current shape."""
    if not isinstance(layout, WorkspaceLayout):
        raise SourceEventQuarantineError("layout must be a WorkspaceLayout instance.")
    if not isinstance(payload, dict):
        raise SourceEventQuarantineError("quarantine payload must be a JSON object.")
    unexpected_fields = set(payload) - _QUARANTINE_PAYLOAD_FIELDS
    if unexpected_fields:
        raise SourceEventQuarantineError(
            "quarantine payload contains unsupported fields: "
            f"{', '.join(sorted(unexpected_fields))}."
        )

    record = _record_from_payload(payload)
    if record.source_kind != "web_search":
        return record.to_json_payload()
    if payload.get("observation_id") is not None and payload.get("candidate_fingerprint") is not None:
        return record.to_json_payload()

    resolved = _resolve_legacy_web_search_quarantine_identities(
        layout=layout,
        payload=payload,
        record=record,
    )
    return SourceEventQuarantineRecord(
        target_key=record.target_key,
        source_kind=record.source_kind,
        requested_event_id=record.requested_event_id,
        observation_id=resolved.observation_id,
        candidate_fingerprint=resolved.candidate_fingerprint,
        live_event_id=record.live_event_id,
        replay_event_id=record.replay_event_id,
        source_ref=record.source_ref,
        title=record.title,
        release_at=record.release_at,
        observed_at=record.observed_at,
        reason=record.reason,
        created_at=record.created_at,
    ).to_json_payload()


def read_quarantined_event_ids(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    source_kind: SourceQuarantineKind,
) -> frozenset[str]:
    """Return every known event id mapped to quarantined archived source rows."""
    records = read_source_event_quarantine_records(
        layout,
        target_key=target_key,
        source_kind=source_kind,
    )
    return frozenset(
        event_id
        for record in records
        for event_id in (
            record.requested_event_id,
            record.live_event_id,
            record.replay_event_id,
        )
        if event_id is not None
    )


def resolve_archived_web_search_event(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    event_id: str,
    source_ref: str | None = None,
) -> SourceEventIdentities:
    """Resolve one archived web-search row by either its live or replay event id."""
    validated_event_id = validate_event_id(
        event_id,
        error_type=SourceEventQuarantineError,
    )
    validated_source_ref = None
    if source_ref is not None:
        validated_source_ref = validate_source_ref(
            source_ref,
            error_type=SourceEventQuarantineError,
        )
    matches: list[tuple[HistoricalWebSearchRecord, SourceEventIdentities]] = []
    for record in read_historical_web_search_records(
        layout,
        target_key=target_key,
        start_at=_READ_FLOOR,
        end_at=datetime.max.replace(tzinfo=UTC),
    ):
        identities = web_search_event_identities(record)
        if validated_event_id not in {
            identities.live_event_id,
            identities.replay_event_id,
        }:
            continue
        if (
            validated_source_ref is not None
            and identities.source_ref != validated_source_ref
        ):
            continue
        matches.append((record, identities))
    return _resolve_web_search_match(
        matches,
        target_key=target_key,
        event_id=validated_event_id,
        source_ref=validated_source_ref,
    )


def web_search_event_identities(
    record: HistoricalWebSearchRecord,
) -> SourceEventIdentities:
    """Derive live and replay event ids for one archived web-search record."""
    if not isinstance(record, HistoricalWebSearchRecord):
        raise SourceEventQuarantineError(
            "record must be a HistoricalWebSearchRecord instance."
        )
    live_request = validate_admission_request(
        target_key=record.target_key,
        ingress_input=LiveWebSearchInput(
            query=record.query,
            source_ref=record.source_ref,
            title=record.title,
            content=record.content,
            discovered_at=record.discovered_at,
            published_at=record.published_at,
        ),
        labels=list(record.labels),
    )
    replay_visible_at = _web_search_release_visible_at(record)
    replay_input = _historical_web_search_input(record, visible_at=replay_visible_at)
    replay_event_id: str | None = None
    try:
        replay_request = validate_replay_admission_request(
            target_key=record.target_key,
            ingress_input=replay_input,
            replay_at=replay_visible_at,
            labels=list(record.labels),
        )
    except ReplayAdmissibilityError:
        replay_event_id = None
    else:
        replay_event_id = derive_admission_event_id(replay_request)
    return SourceEventIdentities(
        target_key=record.target_key,
        source_kind="web_search",
        observation_id=historical_web_search_observation_id(record),
        candidate_fingerprint=historical_web_search_candidate_fingerprint(record),
        live_event_id=derive_admission_event_id(live_request),
        replay_event_id=replay_event_id,
        source_ref=record.source_ref,
        title=record.title,
        release_at=replay_visible_at,
        observed_at=record.discovered_at,
    )


def market_news_event_identities(
    record: MarketNewsArchiveRecord,
) -> SourceEventIdentities:
    """Derive live and replay event ids for one archived market-news record."""
    if not isinstance(record, MarketNewsArchiveRecord):
        raise SourceEventQuarantineError(
            "record must be a MarketNewsArchiveRecord instance."
        )
    live_request = validate_admission_request(
        target_key=record.target_key,
        ingress_input=LiveNewsStreamInput(
            source_ref=record.source_ref,
            headline=record.headline,
            body=record.content_text,
            published_at=record.created_at,
            captured_at=record.visible_at,
        ),
        labels=list(record.labels),
    )
    replay_visible_at = _market_news_release_visible_at(record)
    replay_request = validate_replay_admission_request(
        target_key=record.target_key,
        ingress_input=_historical_market_news_input(record, visible_at=replay_visible_at),
        replay_at=replay_visible_at,
        labels=list(record.labels),
    )
    return SourceEventIdentities(
        target_key=record.target_key,
        source_kind="market_news",
        observation_id=market_news_record_id(record),
        candidate_fingerprint=None,
        live_event_id=derive_admission_event_id(live_request),
        replay_event_id=derive_admission_event_id(replay_request),
        source_ref=record.source_ref,
        title=record.headline,
        release_at=replay_visible_at,
        observed_at=record.captured_at,
    )


def resolve_archived_market_news_event(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    event_id: str,
    source_ref: str | None = None,
) -> SourceEventIdentities:
    """Resolve one archived market-news row by either its live or replay event id."""
    validated_event_id = validate_event_id(
        event_id,
        error_type=SourceEventQuarantineError,
    )
    validated_source_ref = None
    if source_ref is not None:
        validated_source_ref = validate_source_ref(
            source_ref,
            error_type=SourceEventQuarantineError,
        )
    matches: list[SourceEventIdentities] = []
    for record in read_market_news_records(
        layout,
        target_key=target_key,
        start_at=_READ_FLOOR,
        end_at=datetime.max.replace(tzinfo=UTC),
    ):
        identities = market_news_event_identities(record)
        if validated_event_id not in {
            identities.live_event_id,
            identities.replay_event_id,
        }:
            continue
        if (
            validated_source_ref is not None
            and identities.source_ref != validated_source_ref
        ):
            continue
        matches.append(identities)
    return _resolve_single_match(
        matches,
        target_key=target_key,
        event_id=validated_event_id,
        source_kind="market_news",
        source_ref=validated_source_ref,
    )


def _resolve_single_match(
    matches: list[SourceEventIdentities],
    *,
    target_key: str,
    event_id: str,
    source_kind: SourceQuarantineKind,
    source_ref: str | None,
) -> SourceEventIdentities:
    if not matches:
        suffix = "" if source_ref is None else f" source_ref={source_ref}"
        raise SourceEventQuarantineError(
            "No archived source record matched the requested event id: "
            f"target_key={target_key} source_kind={source_kind} "
            f"event_id={event_id}{suffix}."
        )
    if len(matches) > 1:
        suffix = "" if source_ref is None else f" source_ref={source_ref}"
        raise SourceEventQuarantineError(
            "Multiple archived source records matched the requested event id: "
            f"target_key={target_key} source_kind={source_kind} "
            f"event_id={event_id}{suffix}."
    )
    return matches[0]


def _resolve_web_search_match(
    matches: list[tuple[HistoricalWebSearchRecord, SourceEventIdentities]],
    *,
    target_key: str,
    event_id: str,
    source_ref: str | None,
) -> SourceEventIdentities:
    if not matches:
        suffix = "" if source_ref is None else f" source_ref={source_ref}"
        raise SourceEventQuarantineError(
            "No archived source record matched the requested event id: "
            f"target_key={target_key} source_kind=web_search "
            f"event_id={event_id}{suffix}."
        )
    if len(matches) == 1:
        return matches[0][1]

    candidate_fingerprints = {
        identities.candidate_fingerprint for _record, identities in matches
    }
    if len(candidate_fingerprints) != 1:
        suffix = "" if source_ref is None else f" source_ref={source_ref}"
        raise SourceEventQuarantineError(
            "Multiple archived source records matched the requested event id: "
            f"target_key={target_key} source_kind=web_search "
            f"event_id={event_id}{suffix}."
        )

    from event_trader.source_release.gate import select_archived_web_search_records

    batch = select_archived_web_search_records(
        tuple(record for record, _identities in matches),
        layout=None,
    )
    if len(batch.decisions) == 1:
        released_observation_id = batch.decisions[0].released.observation_id
        for _record, identities in matches:
            if identities.observation_id == released_observation_id:
                return identities

    suffix = "" if source_ref is None else f" source_ref={source_ref}"
    raise SourceEventQuarantineError(
        "Multiple archived source records matched the requested event id: "
        f"target_key={target_key} source_kind=web_search "
        f"event_id={event_id}{suffix}."
    )


def _resolve_legacy_web_search_quarantine_identities(
    *,
    layout: WorkspaceLayout,
    payload: dict[str, object],
    record: SourceEventQuarantineRecord,
) -> SourceEventIdentities:
    observation_id = _optional_text(payload.get("observation_id"), "observation_id")
    if observation_id is not None:
        matches = _web_search_archive_matches(
            layout=layout,
            target_key=record.target_key,
            predicate=lambda archived, _identities: (
                historical_web_search_observation_id(archived) == observation_id
            ),
        )
        if len(matches) == 1:
            return matches[0][1]
        if len(matches) > 1:
            raise SourceEventQuarantineError(
                "legacy web_search quarantine payload observation_id matched multiple "
                f"archive rows: target_key={record.target_key} observation_id={observation_id}."
            )

    for event_id in (
        record.requested_event_id,
        record.live_event_id,
        record.replay_event_id,
    ):
        if event_id is None:
            continue
        try:
            return resolve_archived_web_search_event(
                layout,
                target_key=record.target_key,
                event_id=event_id,
                source_ref=record.source_ref,
            )
        except SourceEventQuarantineError:
            continue

    legacy_record_id = _optional_text(payload.get("record_id"), "record_id")
    if legacy_record_id is None:
        raise SourceEventQuarantineError(
            "legacy web_search quarantine payload cannot be normalized without "
            "observation_id, candidate_fingerprint, or record_id."
        )
    matches = _web_search_archive_matches(
        layout=layout,
        target_key=record.target_key,
        predicate=lambda archived, identities: (
            historical_web_search_candidate_fingerprint(archived) == legacy_record_id
            and identities.source_ref == record.source_ref
            and identities.title == record.title
            and identities.release_at == record.release_at
            and identities.observed_at == record.observed_at
        ),
    )
    if len(matches) == 1:
        return matches[0][1]
    if not matches:
        raise SourceEventQuarantineError(
            "legacy web_search quarantine payload could not be matched to any archived "
            f"row: target_key={record.target_key} record_id={legacy_record_id}."
        )
    raise SourceEventQuarantineError(
        "legacy web_search quarantine payload matched multiple archived rows and cannot "
        "be upgraded safely: "
        f"target_key={record.target_key} record_id={legacy_record_id}."
    )


def _web_search_archive_matches(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    predicate: Callable[
        [HistoricalWebSearchRecord, SourceEventIdentities],
        bool,
    ],
) -> list[tuple[HistoricalWebSearchRecord, SourceEventIdentities]]:
    matches: list[tuple[HistoricalWebSearchRecord, SourceEventIdentities]] = []
    for archived in read_historical_web_search_records(
        layout,
        target_key=target_key,
        start_at=_READ_FLOOR,
        end_at=datetime.max.replace(tzinfo=UTC),
    ):
        identities = web_search_event_identities(archived)
        if predicate(archived, identities):
            matches.append((archived, identities))
    return matches


def _record_from_payload(payload: object) -> SourceEventQuarantineRecord:
    if not isinstance(payload, dict):
        raise SourceEventQuarantineError("quarantine payload must be a JSON object.")
    return SourceEventQuarantineRecord(
        target_key=_require_text(payload.get("target_key"), "target_key"),
        source_kind=_parse_source_kind(payload.get("source_kind")),
        requested_event_id=_require_text(
            payload.get("requested_event_id"),
            "requested_event_id",
        ),
        observation_id=_require_text(
            payload.get("observation_id", payload.get("record_id")),
            "observation_id",
        ),
        candidate_fingerprint=_optional_text(
            payload.get("candidate_fingerprint"),
            "candidate_fingerprint",
        ),
        live_event_id=_require_text(payload.get("live_event_id"), "live_event_id"),
        replay_event_id=_optional_text(
            payload.get("replay_event_id"),
            "replay_event_id",
        ),
        source_ref=_require_text(payload.get("source_ref"), "source_ref"),
        title=_require_text(payload.get("title"), "title"),
        release_at=_parse_datetime(payload.get("release_at"), "release_at"),
        observed_at=_parse_datetime(payload.get("observed_at"), "observed_at"),
        reason=_require_text(payload.get("reason"), "reason"),
        created_at=_parse_datetime(payload.get("created_at"), "created_at"),
    )


def _target_root(layout: WorkspaceLayout, target_key: str) -> Path:
    return (
        layout.helpers_root / "source_quarantine" / validate_target_key(
            target_key,
            error_type=SourceEventQuarantineError,
        )
    ).resolve(strict=False)


def _partition_path(layout: WorkspaceLayout, target_key: str, created_at: datetime) -> Path:
    stamp = validate_timestamp(
        created_at,
        field_name="created_at",
        error_type=SourceEventQuarantineError,
    ).astimezone(UTC)
    return (_target_root(layout, target_key) / f"{stamp:%Y-%m}.jsonl").resolve(
        strict=False
    )


def _market_news_release_visible_at(record: MarketNewsArchiveRecord) -> datetime:
    source_timestamp = record.created_at
    if record.updated_at is not None:
        source_timestamp = max(source_timestamp, record.updated_at)
    if source_timestamp <= record.captured_at:
        return source_timestamp.astimezone(UTC)
    return record.captured_at.astimezone(UTC)


def _web_search_release_visible_at(record: HistoricalWebSearchRecord) -> datetime:
    published_at = record.published_at
    if (
        published_at is not None
        and published_at <= record.discovered_at
        and detect_historical_web_search_future_leakage(
            source_ref=record.source_ref,
            title=record.title,
            content=record.content,
            visible_at=published_at,
        )
        is None
    ):
        return published_at.astimezone(UTC)
    return record.discovered_at.astimezone(UTC)


def _historical_market_news_input(
    record: MarketNewsArchiveRecord,
    *,
    visible_at: datetime,
):
    from event_trader.feeds.models import HistoricalMarketNewsInput

    return HistoricalMarketNewsInput(
        source_ref=record.source_ref,
        headline=record.headline,
        body=record.content_text,
        published_at=record.created_at,
        updated_at=record.updated_at,
        captured_at=record.captured_at,
        visible_at=visible_at,
    )


def _historical_web_search_input(
    record: HistoricalWebSearchRecord,
    *,
    visible_at: datetime,
):
    from event_trader.feeds.models import HistoricalWebSearchInput

    return HistoricalWebSearchInput(
        query=record.query,
        source_ref=record.source_ref,
        title=record.title,
        content=record.content,
        published_at=record.published_at,
        discovered_at=record.discovered_at,
        visible_at=visible_at,
    )


def _parse_source_kind(value: object) -> SourceQuarantineKind:
    if value == "market_news":
        return "market_news"
    if value == "web_search":
        return "web_search"
    raise SourceEventQuarantineError(
        "source_kind must be 'market_news' or 'web_search'."
    )


def _parse_datetime(value: object, field_name: str) -> datetime:
    raw_value = _require_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceEventQuarantineError(
            f"{field_name} must be an ISO8601 timestamp."
        ) from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=SourceEventQuarantineError,
    ).astimezone(UTC)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise SourceEventQuarantineError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise SourceEventQuarantineError(f"{field_name} must not be blank.")
    return normalized


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name)


__all__ = [
    "SourceEventIdentities",
    "SourceEventQuarantineError",
    "SourceEventQuarantineRecord",
    "SourceEventQuarantineWriteReceipt",
    "append_source_event_quarantine",
    "market_news_event_identities",
    "normalize_source_event_quarantine_payload",
    "read_quarantined_event_ids",
    "read_source_event_quarantine_records",
    "resolve_archived_market_news_event",
    "resolve_archived_web_search_event",
    "web_search_event_identities",
]
