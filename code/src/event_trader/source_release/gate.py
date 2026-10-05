"""Shared source-release gate for archived source observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Generic, Literal, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from event_trader.contracts._validators import (
    normalize_content,
    validate_source_ref,
    validate_target_key,
    validate_timestamp,
)
from event_trader.feeds.historical_web_search_guard import (
    detect_historical_web_search_future_leakage,
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
from event_trader.source_release.quarantine import (
    market_news_event_identities,
    read_quarantined_event_ids,
    web_search_event_identities,
)
from event_trader.storage import WorkspaceLayout

SourceReleaseKind = Literal["market_news", "web_search"]

_RECORD_T = TypeVar("_RECORD_T", HistoricalWebSearchRecord, MarketNewsArchiveRecord)
_READ_FLOOR = datetime.min.replace(tzinfo=UTC)
_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref_src",
        "s_cid",
    }
)


class SourceReleaseError(ValueError):
    """Raised when source-release normalization or selection is invalid."""


@dataclass(frozen=True, slots=True)
class SourceReleaseKey:
    """Deterministic release identity for one canonical source observation."""

    target_key: str
    source_kind: SourceReleaseKind
    source_identity: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=SourceReleaseError),
        )
        if self.source_kind not in {"market_news", "web_search"}:
            raise SourceReleaseError(
                "source_kind must be 'market_news' or 'web_search'."
            )
        normalized_identity = _validate_non_blank_text(
            self.source_identity,
            field_name="source_identity",
        )
        object.__setattr__(self, "source_identity", normalized_identity)


@dataclass(frozen=True, slots=True)
class SourceReleaseCandidate(Generic[_RECORD_T]):
    """One archived source row plus the deterministic release metadata."""

    source_key: SourceReleaseKey
    release_at: datetime
    observation_at: datetime
    observation_id: str
    source_ref: str
    title: str
    content_hash: str
    candidate_fingerprint: str | None
    payload: _RECORD_T

    def __post_init__(self) -> None:
        if not isinstance(self.source_key, SourceReleaseKey):
            raise SourceReleaseError("source_key must be a SourceReleaseKey instance.")
        object.__setattr__(
            self,
            "release_at",
            validate_timestamp(
                self.release_at,
                field_name="release_at",
                error_type=SourceReleaseError,
            ).astimezone(UTC),
        )
        object.__setattr__(
            self,
            "observation_at",
            validate_timestamp(
                self.observation_at,
                field_name="observation_at",
                error_type=SourceReleaseError,
            ).astimezone(UTC),
        )
        object.__setattr__(
            self,
            "observation_id",
            _validate_non_blank_text(
                self.observation_id,
                field_name="observation_id",
            ),
        )
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=SourceReleaseError),
        )
        object.__setattr__(
            self,
            "title",
            _validate_non_blank_text(self.title, field_name="title"),
        )
        object.__setattr__(
            self,
            "content_hash",
            _validate_non_blank_text(self.content_hash, field_name="content_hash"),
        )
        if self.candidate_fingerprint is not None:
            object.__setattr__(
                self,
                "candidate_fingerprint",
                _validate_non_blank_text(
                    self.candidate_fingerprint,
                    field_name="candidate_fingerprint",
                ),
            )


@dataclass(frozen=True, slots=True)
class SourceReleaseDecision(Generic[_RECORD_T]):
    """Selected canonical archived row plus suppressed duplicates."""

    source_key: SourceReleaseKey
    released: SourceReleaseCandidate[_RECORD_T]
    suppressed_duplicates: tuple[SourceReleaseCandidate[_RECORD_T], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_key, SourceReleaseKey):
            raise SourceReleaseError("source_key must be a SourceReleaseKey instance.")
        if not isinstance(self.released, SourceReleaseCandidate):
            raise SourceReleaseError(
                "released must be a SourceReleaseCandidate instance."
            )
        if self.released.source_key != self.source_key:
            raise SourceReleaseError(
                "released.source_key must match decision.source_key."
            )
        if not isinstance(self.suppressed_duplicates, tuple):
            raise SourceReleaseError(
                "suppressed_duplicates must be a tuple of SourceReleaseCandidate items."
            )
        for candidate in self.suppressed_duplicates:
            if not isinstance(candidate, SourceReleaseCandidate):
                raise SourceReleaseError(
                    "suppressed_duplicates must contain only SourceReleaseCandidate items."
                )
            if candidate.source_key != self.source_key:
                raise SourceReleaseError(
                    "suppressed duplicate source_key must match decision.source_key."
                )


@dataclass(frozen=True, slots=True)
class SourceReleaseBatch(Generic[_RECORD_T]):
    """Deterministic release decisions for one set of archived rows."""

    decisions: tuple[SourceReleaseDecision[_RECORD_T], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.decisions, tuple):
            raise SourceReleaseError(
                "decisions must be a tuple of SourceReleaseDecision items."
            )
        for decision in self.decisions:
            if not isinstance(decision, SourceReleaseDecision):
                raise SourceReleaseError(
                    "decisions must contain only SourceReleaseDecision items."
                )

    @property
    def released_count(self) -> int:
        return len(self.decisions)

    @property
    def suppressed_duplicate_count(self) -> int:
        return sum(len(decision.suppressed_duplicates) for decision in self.decisions)


def normalize_web_source_ref(source_ref: str) -> str:
    """Conservatively normalize a canonical web source_ref for dedupe keys."""
    validated_source_ref = validate_source_ref(
        source_ref,
        error_type=SourceReleaseError,
    )
    parsed = urlsplit(validated_source_ref)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return validated_source_ref
    hostname = parsed.hostname
    if hostname is None:
        raise SourceReleaseError("web source_ref must include a host.")
    netloc = hostname.lower()
    if parsed.username is not None:
        netloc = parsed.username + (
            "" if parsed.password is None else f":{parsed.password}"
        ) + f"@{netloc}"
    if parsed.port is not None and parsed.port != _default_port(scheme):
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not _tracking_query_key(key)
        ],
        doseq=True,
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def web_search_release_visible_at(record: HistoricalWebSearchRecord) -> datetime:
    """Return the earliest replay-safe visible time for one archived web-search row."""
    if not isinstance(record, HistoricalWebSearchRecord):
        raise SourceReleaseError(
            "record must be a HistoricalWebSearchRecord instance."
        )
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


def market_news_release_visible_at(record: MarketNewsArchiveRecord) -> datetime:
    """Return the earliest replay-safe visible time for one archived market-news row."""
    if not isinstance(record, MarketNewsArchiveRecord):
        raise SourceReleaseError("record must be a MarketNewsArchiveRecord instance.")
    source_timestamp = record.created_at
    if record.updated_at is not None:
        source_timestamp = max(source_timestamp, record.updated_at)
    if source_timestamp <= record.captured_at:
        return source_timestamp.astimezone(UTC)
    return record.captured_at.astimezone(UTC)


def select_archived_web_search_records(
    records: tuple[HistoricalWebSearchRecord, ...],
    *,
    layout: WorkspaceLayout | None = None,
) -> SourceReleaseBatch[HistoricalWebSearchRecord]:
    """Select one canonical archived web-search row per deterministic source key."""
    candidates: list[SourceReleaseCandidate[HistoricalWebSearchRecord]] = []
    quarantined_by_target: dict[str, frozenset[str]] = {}
    for record in records:
        if layout is not None:
            quarantined_event_ids = quarantined_by_target.setdefault(
                record.target_key,
                read_quarantined_event_ids(
                    layout,
                    target_key=record.target_key,
                    source_kind="web_search",
                ),
            )
            identities = web_search_event_identities(record)
            if (
                identities.live_event_id in quarantined_event_ids
                or identities.replay_event_id in quarantined_event_ids
            ):
                continue
        if not _web_search_record_is_releaseable(record):
            continue
        candidates.append(_web_search_candidate(record))
    return _select_records(tuple(candidates))


def select_archived_market_news_records(
    records: tuple[MarketNewsArchiveRecord, ...],
    *,
    layout: WorkspaceLayout | None = None,
) -> SourceReleaseBatch[MarketNewsArchiveRecord]:
    """Select one canonical archived market-news row per deterministic source key."""
    candidates: list[SourceReleaseCandidate[MarketNewsArchiveRecord]] = []
    quarantined_by_target: dict[str, frozenset[str]] = {}
    for record in records:
        if layout is not None:
            quarantined_event_ids = quarantined_by_target.setdefault(
                record.target_key,
                read_quarantined_event_ids(
                    layout,
                    target_key=record.target_key,
                    source_kind="market_news",
                ),
            )
            identities = market_news_event_identities(record)
            if (
                identities.live_event_id in quarantined_event_ids
                or identities.replay_event_id in quarantined_event_ids
            ):
                continue
        candidates.append(_market_news_candidate(record))
    return _select_records(tuple(candidates))


def release_current_web_search_observation_ids(
    *,
    layout: WorkspaceLayout | None,
    target_key: str,
    current_records: tuple[HistoricalWebSearchRecord, ...],
) -> frozenset[str]:
    """Return the current archived web-search observation ids eligible for release."""
    if not current_records:
        return frozenset()
    history = (
        current_records
        if layout is None
        else read_historical_web_search_records(
            layout,
            target_key=validate_target_key(target_key, error_type=SourceReleaseError),
            start_at=_READ_FLOOR,
            end_at=max(record.visible_at for record in current_records),
        )
    )
    current_observation_ids = {
        historical_web_search_observation_id(record) for record in current_records
    }
    batch = select_archived_web_search_records(history, layout=layout)
    return frozenset(
        decision.released.observation_id
        for decision in batch.decisions
        if decision.released.observation_id in current_observation_ids
    )


def release_current_web_search_record_ids(
    *,
    layout: WorkspaceLayout | None,
    target_key: str,
    current_records: tuple[HistoricalWebSearchRecord, ...],
) -> frozenset[str]:
    """Compatibility alias for current archived web-search observation ids."""
    return release_current_web_search_observation_ids(
        layout=layout,
        target_key=target_key,
        current_records=current_records,
    )


def release_current_market_news_record_ids(
    *,
    layout: WorkspaceLayout | None,
    target_key: str,
    current_records: tuple[MarketNewsArchiveRecord, ...],
) -> frozenset[str]:
    """Return the current archived market-news record ids eligible for release."""
    if not current_records:
        return frozenset()
    history = (
        current_records
        if layout is None
        else read_market_news_records(
            layout,
            target_key=validate_target_key(target_key, error_type=SourceReleaseError),
            start_at=_READ_FLOOR,
            end_at=max(record.visible_at for record in current_records),
        )
    )
    current_ids = {market_news_record_id(record) for record in current_records}
    batch = select_archived_market_news_records(history, layout=layout)
    return frozenset(
        decision.released.observation_id
        for decision in batch.decisions
        if decision.released.observation_id in current_ids
    )


def _select_records(
    candidates: tuple[SourceReleaseCandidate[_RECORD_T], ...],
) -> SourceReleaseBatch[_RECORD_T]:
    grouped: dict[SourceReleaseKey, list[SourceReleaseCandidate[_RECORD_T]]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.source_key, []).append(candidate)
    decisions = tuple(
        _select_group(source_key=source_key, candidates=grouped[source_key])
        for source_key in sorted(
            grouped,
            key=lambda key: (key.target_key, key.source_kind, key.source_identity),
        )
    )
    return SourceReleaseBatch(decisions=decisions)


def _select_group(
    *,
    source_key: SourceReleaseKey,
    candidates: list[SourceReleaseCandidate[_RECORD_T]],
) -> SourceReleaseDecision[_RECORD_T]:
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            candidate.release_at,
            candidate.observation_at,
            candidate.source_ref,
            candidate.title,
            candidate.content_hash,
            candidate.observation_id,
        ),
    )
    released = ranked[0]
    return SourceReleaseDecision(
        source_key=source_key,
        released=released,
        suppressed_duplicates=tuple(ranked[1:]),
    )


def _web_search_candidate(
    record: HistoricalWebSearchRecord,
) -> SourceReleaseCandidate[HistoricalWebSearchRecord]:
    if not isinstance(record, HistoricalWebSearchRecord):
        raise SourceReleaseError(
            "record must be a HistoricalWebSearchRecord instance."
        )
    return SourceReleaseCandidate(
        source_key=SourceReleaseKey(
            target_key=record.target_key,
            source_kind="web_search",
            source_identity=normalize_web_source_ref(record.source_ref),
        ),
        release_at=web_search_release_visible_at(record),
        observation_at=record.discovered_at,
        observation_id=historical_web_search_observation_id(record),
        source_ref=record.source_ref,
        title=record.title,
        content_hash=_content_hash(record.content),
        candidate_fingerprint=historical_web_search_candidate_fingerprint(record),
        payload=record,
    )


def _web_search_record_is_releaseable(record: HistoricalWebSearchRecord) -> bool:
    return (
        detect_historical_web_search_future_leakage(
            source_ref=record.source_ref,
            title=record.title,
            content=record.content,
            visible_at=record.visible_at,
        )
        is None
    )


def _market_news_candidate(
    record: MarketNewsArchiveRecord,
) -> SourceReleaseCandidate[MarketNewsArchiveRecord]:
    if not isinstance(record, MarketNewsArchiveRecord):
        raise SourceReleaseError("record must be a MarketNewsArchiveRecord instance.")
    return SourceReleaseCandidate(
        source_key=SourceReleaseKey(
            target_key=record.target_key,
            source_kind="market_news",
            source_identity=_market_news_source_identity(record),
        ),
        release_at=market_news_release_visible_at(record),
        observation_at=record.captured_at,
        observation_id=market_news_record_id(record),
        source_ref=record.source_ref,
        title=record.headline,
        content_hash=_content_hash(record.content_text),
        candidate_fingerprint=None,
        payload=record,
    )


def _market_news_source_identity(record: MarketNewsArchiveRecord) -> str:
    if record.provider.strip() and record.article_id.strip():
        return f"article:{record.provider.strip()}:{record.article_id.strip()}"
    return normalize_web_source_ref(record.source_ref)


def _content_hash(value: str) -> str:
    normalized = normalize_content(
        value,
        field_name="content",
        error_type=SourceReleaseError,
    )
    return sha256(normalized.encode("utf-8")).hexdigest()


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _tracking_query_key(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized.startswith("utm_") or normalized in _TRACKING_QUERY_KEYS


def _validate_non_blank_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise SourceReleaseError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise SourceReleaseError(f"{field_name} must not be blank.")
    return normalized


__all__ = [
    "SourceReleaseBatch",
    "SourceReleaseCandidate",
    "SourceReleaseDecision",
    "SourceReleaseError",
    "SourceReleaseKey",
    "market_news_release_visible_at",
    "normalize_web_source_ref",
    "release_current_market_news_record_ids",
    "release_current_web_search_observation_ids",
    "release_current_web_search_record_ids",
    "select_archived_market_news_records",
    "select_archived_web_search_records",
    "web_search_release_visible_at",
]
