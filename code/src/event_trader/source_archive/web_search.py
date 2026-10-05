"""Project-owned archived web-search source records."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from event_trader.contracts._validators import (
    format_identity_timestamp,
    normalize_content,
    validate_labels,
    validate_source_ref,
    validate_target_key,
    validate_timestamp,
)
from event_trader.source_policy import (
    OPERATOR_CONFIDENCE_LABEL_PREFIX,
    OPERATOR_SOURCE_BASIS_LABEL_PREFIX,
    SourcePolicyError,
    extract_event_type,
    extract_source_kind,
)
from event_trader.storage import WorkspaceLayout

if TYPE_CHECKING:
    from event_trader.feeds.models import LiveWebSearchInput


_RETIRED_OPERATOR_BASIS_LABEL_PREFIX = OPERATOR_SOURCE_BASIS_LABEL_PREFIX.removeprefix(
    "operator_"
)
_FORBIDDEN_OPERATOR_ONLY_LABEL_PREFIXES = (
    OPERATOR_CONFIDENCE_LABEL_PREFIX,
    OPERATOR_SOURCE_BASIS_LABEL_PREFIX,
    _RETIRED_OPERATOR_BASIS_LABEL_PREFIX,
)
_DERIVED_RECORD_FIELDS = frozenset({"candidate_fingerprint", "observation_id"})
_SERIALIZED_PROVENANCE_FIELDS = frozenset(
    {
        "prompt_profile_id",
        "search_intent_hash",
        "control_language",
        "retrieval_languages",
    }
)
_SERIALIZED_RECORD_FIELDS = frozenset(
    {
        "target_key",
        "query",
        "source_ref",
        "title",
        "content",
        "labels",
        "published_at",
        "discovered_at",
        "visible_at",
        "acquisition_provenance",
    }
)


class HistoricalWebSearchLibraryError(ValueError):
    """Raised when archived web-search writes or reads are invalid."""


@dataclass(frozen=True, slots=True)
class WebSearchAcquisitionProvenance:
    """Optional acquisition metadata persisted on historical web-search rows."""

    prompt_profile_id: str
    search_intent_hash: str
    control_language: str
    retrieval_languages: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "prompt_profile_id",
            _validate_non_blank_text(
                self.prompt_profile_id,
                field_name="prompt_profile_id",
            ),
        )
        object.__setattr__(
            self,
            "search_intent_hash",
            _validate_non_blank_text(
                self.search_intent_hash,
                field_name="search_intent_hash",
            ),
        )
        object.__setattr__(
            self,
            "control_language",
            _validate_non_blank_text(
                self.control_language,
                field_name="control_language",
            ),
        )
        if not isinstance(self.retrieval_languages, tuple):
            raise HistoricalWebSearchLibraryError(
                "retrieval_languages must be a tuple[str, ...]."
            )
        object.__setattr__(
            self,
            "retrieval_languages",
            tuple(
                _require_string_list(
                    list(self.retrieval_languages),
                    field_name="retrieval_languages",
                    require_non_empty=True,
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class HistoricalWebSearchTargetLayout:
    """Resolved helper paths for one target's archived web-search records."""

    target_key: str
    target_root: Path

    def day_partition(self, visible_at: date | datetime) -> Path:
        """Resolve the target day partition from visible_at."""
        resolved_day = _normalize_partition_day(visible_at)
        return (self.target_root / f"{resolved_day.isoformat()}.jsonl").resolve(
            strict=False
        )


@dataclass(frozen=True, slots=True)
class HistoricalWebSearchRecord:
    """One canonical archived web-search source record."""

    target_key: str
    query: str
    source_ref: str
    title: str
    content: str
    labels: list[str]
    published_at: datetime | None
    discovered_at: datetime
    visible_at: datetime
    acquisition_provenance: WebSearchAcquisitionProvenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=HistoricalWebSearchLibraryError,
            ),
        )
        object.__setattr__(
            self,
            "query",
            _validate_non_blank_text(self.query, field_name="query"),
        )
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(
                self.source_ref,
                error_type=HistoricalWebSearchLibraryError,
            ),
        )
        object.__setattr__(
            self,
            "title",
            _validate_non_blank_text(self.title, field_name="title"),
        )
        object.__setattr__(
            self,
            "content",
            normalize_content(
                self.content,
                field_name="content",
                error_type=HistoricalWebSearchLibraryError,
            ),
        )
        object.__setattr__(
            self,
            "labels",
            validate_labels(
                self.labels,
                error_type=HistoricalWebSearchLibraryError,
            ),
        )
        _validate_web_search_classification_labels(self.labels)
        _reject_operator_only_labels(self.labels)

        published_at = None
        if self.published_at is not None:
            published_at = validate_timestamp(
                self.published_at,
                field_name="published_at",
                error_type=HistoricalWebSearchLibraryError,
            )
        discovered_at = validate_timestamp(
            self.discovered_at,
            field_name="discovered_at",
            error_type=HistoricalWebSearchLibraryError,
        )
        visible_at = validate_timestamp(
            self.visible_at,
            field_name="visible_at",
            error_type=HistoricalWebSearchLibraryError,
        )
        anchor_timestamp = discovered_at if published_at is None else published_at
        anchor_field_name = "discovered_at" if published_at is None else "published_at"
        if visible_at < anchor_timestamp:
            raise HistoricalWebSearchLibraryError(
                "visible_at must be greater than or equal to "
                f"{anchor_field_name} for historical web_search records."
            )
        if self.acquisition_provenance is not None and not isinstance(
            self.acquisition_provenance,
            WebSearchAcquisitionProvenance,
        ):
            raise HistoricalWebSearchLibraryError(
                "acquisition_provenance must be a WebSearchAcquisitionProvenance "
                "instance when set."
            )


@dataclass(frozen=True, slots=True)
class HistoricalWebSearchWriteReceipt:
    """Observable result from one archived web-search write."""

    status: Literal["written", "noop_existing"]
    record_id: str
    partition_path: Path

    def __post_init__(self) -> None:
        if self.status not in {"written", "noop_existing"}:
            raise HistoricalWebSearchLibraryError(
                "status must be 'written' or 'noop_existing'."
            )
        object.__setattr__(
            self,
            "record_id",
            _validate_non_blank_text(self.record_id, field_name="record_id"),
        )
        if not isinstance(self.partition_path, Path):
            raise HistoricalWebSearchLibraryError(
                "partition_path must be a pathlib.Path."
            )


@dataclass(frozen=True, slots=True)
class HistoricalWebSearchSliceCompletion:
    """One explicit completed-slice marker for deterministic backfill resume."""

    target_key: str
    query: str
    visible_at: datetime
    outcome: Literal["source_appended", "no_source_found"]
    acquisition_provenance: WebSearchAcquisitionProvenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=HistoricalWebSearchLibraryError,
            ),
        )
        object.__setattr__(
            self,
            "query",
            _validate_non_blank_text(self.query, field_name="query"),
        )
        object.__setattr__(
            self,
            "visible_at",
            validate_timestamp(
                self.visible_at,
                field_name="visible_at",
                error_type=HistoricalWebSearchLibraryError,
            ),
        )
        if self.outcome not in {"source_appended", "no_source_found"}:
            raise HistoricalWebSearchLibraryError(
                "outcome must be 'source_appended' or 'no_source_found'."
            )
        if self.acquisition_provenance is not None and not isinstance(
            self.acquisition_provenance,
            WebSearchAcquisitionProvenance,
        ):
            raise HistoricalWebSearchLibraryError(
                "acquisition_provenance must be a WebSearchAcquisitionProvenance "
                "instance when set."
            )


@dataclass(frozen=True, slots=True)
class HistoricalWebSearchSliceCompletionWriteReceipt:
    """Observable result from one completed-slice write."""

    status: Literal["written", "noop_existing"]
    completion_id: str
    partition_path: Path

    def __post_init__(self) -> None:
        if self.status not in {"written", "noop_existing"}:
            raise HistoricalWebSearchLibraryError(
                "status must be 'written' or 'noop_existing'."
            )
        object.__setattr__(
            self,
            "completion_id",
            _validate_non_blank_text(self.completion_id, field_name="completion_id"),
        )
        if not isinstance(self.partition_path, Path):
            raise HistoricalWebSearchLibraryError(
                "partition_path must be a pathlib.Path."
            )


def build_historical_web_search_target_layout(
    layout: WorkspaceLayout,
    target_key: str,
) -> HistoricalWebSearchTargetLayout:
    """Resolve one target's archived web-search root."""
    if not isinstance(layout, WorkspaceLayout):
        raise HistoricalWebSearchLibraryError(
            "layout must be a WorkspaceLayout instance."
        )
    validated_target_key = validate_target_key(
        target_key,
        error_type=HistoricalWebSearchLibraryError,
    )
    return HistoricalWebSearchTargetLayout(
        target_key=validated_target_key,
        target_root=(
            layout.helpers_root / "source_archive" / "web_search" / validated_target_key
        ).resolve(strict=False),
    )


def historical_web_search_partition_path(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    visible_at: date | datetime,
) -> Path:
    """Resolve the archived web-search day partition without creating directories."""
    return build_historical_web_search_target_layout(layout, target_key).day_partition(
        visible_at
    )


def historical_web_search_record_id(record: HistoricalWebSearchRecord) -> str:
    """Compatibility alias for the legacy candidate fingerprint."""
    return historical_web_search_candidate_fingerprint(record)


def historical_web_search_candidate_fingerprint(
    record: HistoricalWebSearchRecord,
) -> str:
    """Derive the stable candidate/content fingerprint for one record."""
    if not isinstance(record, HistoricalWebSearchRecord):
        raise HistoricalWebSearchLibraryError(
            "record must be a HistoricalWebSearchRecord instance."
        )
    identity_material = "\n".join(
        (
            record.target_key,
            record.source_ref,
            format_identity_timestamp(record.visible_at),
            record.content,
        )
    )
    return sha256(identity_material.encode("utf-8")).hexdigest()


def historical_web_search_observation_id(record: HistoricalWebSearchRecord) -> str:
    """Derive the append-only observation identity for one persisted record."""
    if not isinstance(record, HistoricalWebSearchRecord):
        raise HistoricalWebSearchLibraryError(
            "record must be a HistoricalWebSearchRecord instance."
        )
    identity_material = json.dumps(
        _observation_identity_payload(record),
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(identity_material.encode("utf-8")).hexdigest()


def historical_web_search_slice_completion_partition_path(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    visible_at: date | datetime,
) -> Path:
    """Resolve the day partition that stores completed-slice markers."""
    target_layout = build_historical_web_search_target_layout(layout, target_key)
    resolved_day = _normalize_partition_day(visible_at)
    return (target_layout.target_root / "slice_completion" / f"{resolved_day}.jsonl").resolve(
        strict=False
    )


def historical_web_search_slice_completion_id(
    completion: HistoricalWebSearchSliceCompletion,
) -> str:
    """Derive the deterministic identity for one completed-slice marker."""
    if not isinstance(completion, HistoricalWebSearchSliceCompletion):
        raise HistoricalWebSearchLibraryError(
            "completion must be a HistoricalWebSearchSliceCompletion instance."
        )
    identity_material = "\n".join(
        (
            completion.target_key,
            completion.query,
            format_identity_timestamp(completion.visible_at),
        )
    )
    return sha256(identity_material.encode("utf-8")).hexdigest()


def append_historical_web_search_slice_completion(
    layout: WorkspaceLayout,
    *,
    completion: HistoricalWebSearchSliceCompletion,
) -> HistoricalWebSearchSliceCompletionWriteReceipt:
    """Persist one explicit completed-slice marker for resume semantics."""
    if not isinstance(layout, WorkspaceLayout):
        raise HistoricalWebSearchLibraryError(
            "layout must be a WorkspaceLayout instance."
        )
    if not isinstance(completion, HistoricalWebSearchSliceCompletion):
        raise HistoricalWebSearchLibraryError(
            "completion must be a HistoricalWebSearchSliceCompletion instance."
        )
    partition_path = historical_web_search_slice_completion_partition_path(
        layout,
        target_key=completion.target_key,
        visible_at=completion.visible_at,
    )
    completion_id = historical_web_search_slice_completion_id(completion)
    payload = _serialize_slice_completion(completion, completion_id=completion_id)
    for existing_payload in _iter_partition_payloads(partition_path):
        existing_completion = _slice_completion_from_payload(existing_payload)
        existing_completion_id = historical_web_search_slice_completion_id(
            existing_completion
        )
        if existing_completion_id != completion_id:
            continue
        if existing_payload == payload:
            return HistoricalWebSearchSliceCompletionWriteReceipt(
                status="noop_existing",
                completion_id=completion_id,
                partition_path=partition_path,
            )
        raise HistoricalWebSearchLibraryError(
            "historical web_search slice-completion partition already contains the "
            f"same identity with a different payload: completion_id={completion_id} "
            f"partition_path={partition_path}."
        )

    partition_path.parent.mkdir(parents=True, exist_ok=True)
    payload_line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
    try:
        with partition_path.open("ab") as handle:
            handle.write(payload_line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise HistoricalWebSearchLibraryError(
            "Failed to append historical web_search slice-completion marker "
            f"{completion_id} to {partition_path}: {exc}"
        ) from exc

    return HistoricalWebSearchSliceCompletionWriteReceipt(
        status="written",
        completion_id=completion_id,
        partition_path=partition_path,
    )


def read_historical_web_search_slice_completions(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
) -> tuple[HistoricalWebSearchSliceCompletion, ...]:
    """Read explicit completed-slice markers in one visible_at window."""
    if not isinstance(layout, WorkspaceLayout):
        raise HistoricalWebSearchLibraryError(
            "layout must be a WorkspaceLayout instance."
        )
    validated_target_key = validate_target_key(
        target_key,
        error_type=HistoricalWebSearchLibraryError,
    )
    validated_start_at = validate_timestamp(
        start_at,
        field_name="start_at",
        error_type=HistoricalWebSearchLibraryError,
    )
    validated_end_at = validate_timestamp(
        end_at,
        field_name="end_at",
        error_type=HistoricalWebSearchLibraryError,
    )
    if validated_end_at < validated_start_at:
        raise HistoricalWebSearchLibraryError(
            "end_at must be greater than or equal to start_at."
        )

    target_layout = build_historical_web_search_target_layout(
        layout,
        validated_target_key,
    )
    completion_root = target_layout.target_root / "slice_completion"
    if not completion_root.exists():
        return ()
    if not completion_root.is_dir():
        raise HistoricalWebSearchLibraryError(
            "historical web_search slice-completion root must be a directory: "
            f"{completion_root}"
        )

    completions: list[HistoricalWebSearchSliceCompletion] = []
    for partition_path in sorted(completion_root.glob("*.jsonl")):
        for payload in _iter_partition_payloads(partition_path):
            completion = _slice_completion_from_payload(payload)
            if completion.target_key != validated_target_key:
                raise HistoricalWebSearchLibraryError(
                    "historical web_search slice-completion marker target_key does "
                    "not match partition target_key."
                )
            if completion.visible_at < validated_start_at:
                continue
            if completion.visible_at > validated_end_at:
                continue
            completions.append(completion)
    return tuple(
        sorted(completions, key=lambda completion: (completion.visible_at, completion.query))
    )


def has_historical_web_search_slice_completion_marker(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    query: str,
    visible_at: datetime,
) -> bool:
    """Return whether one explicit completed-slice marker exists."""
    validated_query = _validate_non_blank_text(query, field_name="query")
    validated_visible_at = validate_timestamp(
        visible_at,
        field_name="visible_at",
        error_type=HistoricalWebSearchLibraryError,
    )
    for completion in read_historical_web_search_slice_completions(
        layout,
        target_key=target_key,
        start_at=validated_visible_at,
        end_at=validated_visible_at,
    ):
        if completion.query != validated_query:
            continue
        return True
    return False


def append_historical_web_search_record(
    layout: WorkspaceLayout,
    *,
    record: HistoricalWebSearchRecord,
) -> HistoricalWebSearchWriteReceipt:
    """Append one archived web-search record or no-op."""
    if not isinstance(layout, WorkspaceLayout):
        raise HistoricalWebSearchLibraryError(
            "layout must be a WorkspaceLayout instance."
        )
    if not isinstance(record, HistoricalWebSearchRecord):
        raise HistoricalWebSearchLibraryError(
            "record must be a HistoricalWebSearchRecord instance."
        )

    partition_path = historical_web_search_partition_path(
        layout,
        target_key=record.target_key,
        visible_at=record.visible_at,
    )
    record_id = historical_web_search_record_id(record)
    observation_id = historical_web_search_observation_id(record)
    record_payload = _serialize_record(record)
    for existing_payload in _iter_partition_payloads(partition_path):
        existing_record = _record_from_payload(existing_payload)
        existing_observation_id = historical_web_search_observation_id(existing_record)
        if existing_observation_id != observation_id:
            continue
        if _serialize_record(existing_record) == record_payload:
            return HistoricalWebSearchWriteReceipt(
                status="noop_existing",
                record_id=record_id,
                partition_path=partition_path,
            )
        raise HistoricalWebSearchLibraryError(
            "historical web_search partition already contains the same observation_id "
            f"with a different payload: observation_id={observation_id} "
            f"partition_path={partition_path}."
        )

    partition_path.parent.mkdir(parents=True, exist_ok=True)
    payload_line = (
        json.dumps(record_payload, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        with partition_path.open("ab") as handle:
            handle.write(payload_line)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise HistoricalWebSearchLibraryError(
            "Failed to append historical web_search record "
            f"{record_id} to {partition_path}: {exc}"
        ) from exc

    return HistoricalWebSearchWriteReceipt(
        status="written",
        record_id=record_id,
        partition_path=partition_path,
    )


def map_live_web_search_to_historical_record(
    *,
    target_key: str,
    ingress_input: LiveWebSearchInput,
    labels: list[str],
) -> HistoricalWebSearchRecord:
    """Map one live web-search result into the archived record shape."""
    from event_trader.feeds.models import LiveWebSearchInput

    if not isinstance(ingress_input, LiveWebSearchInput):
        raise HistoricalWebSearchLibraryError(
            "ingress_input must be a LiveWebSearchInput instance."
        )
    return HistoricalWebSearchRecord(
        target_key=target_key,
        query=ingress_input.query,
        source_ref=ingress_input.source_ref,
        title=ingress_input.title,
        content=ingress_input.content,
        labels=list(labels),
        published_at=ingress_input.published_at,
        discovered_at=ingress_input.discovered_at,
        visible_at=ingress_input.discovered_at,
    )


def write_live_web_search_record(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    ingress_input: LiveWebSearchInput,
    labels: list[str],
) -> HistoricalWebSearchWriteReceipt:
    """Persist one live web-search result into the archived source library."""
    record = map_live_web_search_to_historical_record(
        target_key=target_key,
        ingress_input=ingress_input,
        labels=labels,
    )
    return append_historical_web_search_record(layout, record=record)


def write_historical_web_search_backfill_record(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    query: str,
    source_ref: str,
    title: str,
    content: str,
    labels: list[str],
    published_at: datetime | None,
    discovered_at: datetime | None,
    visible_at: datetime | None,
    acquisition_provenance: WebSearchAcquisitionProvenance | None = None,
) -> HistoricalWebSearchWriteReceipt:
    """Persist one explicit historical web-search backfill record."""
    if discovered_at is None:
        raise HistoricalWebSearchLibraryError(
            "discovered_at is required for historical web_search backfill."
        )
    if visible_at is None:
        raise HistoricalWebSearchLibraryError(
            "visible_at is required for historical web_search backfill."
        )
    return append_historical_web_search_record(
        layout,
        record=HistoricalWebSearchRecord(
            target_key=target_key,
            query=query,
            source_ref=source_ref,
            title=title,
            content=content,
            labels=list(labels),
            published_at=published_at,
            discovered_at=discovered_at,
            visible_at=visible_at,
            acquisition_provenance=acquisition_provenance,
        ),
    )


def read_historical_web_search_records(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
) -> tuple[HistoricalWebSearchRecord, ...]:
    """Read archived web-search records in one visible_at window."""
    if not isinstance(layout, WorkspaceLayout):
        raise HistoricalWebSearchLibraryError(
            "layout must be a WorkspaceLayout instance."
        )
    validated_target_key = validate_target_key(
        target_key,
        error_type=HistoricalWebSearchLibraryError,
    )
    validated_start_at = validate_timestamp(
        start_at,
        field_name="start_at",
        error_type=HistoricalWebSearchLibraryError,
    )
    validated_end_at = validate_timestamp(
        end_at,
        field_name="end_at",
        error_type=HistoricalWebSearchLibraryError,
    )
    if validated_end_at < validated_start_at:
        raise HistoricalWebSearchLibraryError(
            "end_at must be greater than or equal to start_at."
        )

    target_layout = build_historical_web_search_target_layout(
        layout,
        validated_target_key,
    )
    if not target_layout.target_root.exists():
        return ()
    if not target_layout.target_root.is_dir():
        raise HistoricalWebSearchLibraryError(
            "historical web_search target root must be a directory: "
            f"{target_layout.target_root}"
        )

    records: list[HistoricalWebSearchRecord] = []
    for partition_path in sorted(target_layout.target_root.glob("*.jsonl")):
        for payload in _iter_partition_payloads(partition_path):
            record = _record_from_payload(payload)
            if record.visible_at < validated_start_at:
                continue
            if record.visible_at > validated_end_at:
                continue
            records.append(record)
    return tuple(sorted(records, key=lambda record: record.visible_at))


def normalize_historical_web_search_payload(payload: object) -> dict[str, object]:
    """Normalize one persisted payload into the canonical observation row shape."""
    if not isinstance(payload, dict):
        raise HistoricalWebSearchLibraryError(
            "historical web_search partition payload must decode to a JSON object."
        )
    unexpected_fields = set(payload) - _SERIALIZED_RECORD_FIELDS - _DERIVED_RECORD_FIELDS
    if unexpected_fields:
        raise HistoricalWebSearchLibraryError(
            "historical web_search partition payload contains unsupported fields: "
            f"{', '.join(sorted(unexpected_fields))}."
        )
    return _serialize_record(_record_from_payload(payload))


def _serialize_record(record: HistoricalWebSearchRecord) -> dict[str, object]:
    payload = _serialize_record_semantics(record)
    payload["candidate_fingerprint"] = historical_web_search_candidate_fingerprint(record)
    payload["observation_id"] = historical_web_search_observation_id(record)
    return payload


def _serialize_record_semantics(record: HistoricalWebSearchRecord) -> dict[str, object]:
    payload: dict[str, object] = {
        "target_key": record.target_key,
        "query": record.query,
        "source_ref": record.source_ref,
        "title": record.title,
        "content": record.content,
        "labels": list(record.labels),
        "published_at": (
            None if record.published_at is None else record.published_at.isoformat()
        ),
        "discovered_at": record.discovered_at.isoformat(),
        "visible_at": record.visible_at.isoformat(),
    }
    if record.acquisition_provenance is not None:
        payload["acquisition_provenance"] = _serialize_acquisition_provenance(
            record.acquisition_provenance
        )
    return payload


def _observation_identity_payload(
    record: HistoricalWebSearchRecord,
) -> dict[str, object]:
    return {
        "target_key": record.target_key,
        "query": record.query,
        "source_ref": record.source_ref,
        "title": record.title,
        "content": record.content,
        "labels": list(record.labels),
        "published_at": (
            None
            if record.published_at is None
            else format_identity_timestamp(record.published_at)
        ),
        "discovered_at": format_identity_timestamp(record.discovered_at),
        "visible_at": format_identity_timestamp(record.visible_at),
    }


def _serialize_slice_completion(
    completion: HistoricalWebSearchSliceCompletion,
    *,
    completion_id: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "target_key": completion.target_key,
        "query": completion.query,
        "visible_at": completion.visible_at.isoformat(),
        "outcome": completion.outcome,
        "completion_id": completion_id,
    }
    if completion.acquisition_provenance is not None:
        payload["acquisition_provenance"] = _serialize_acquisition_provenance(
            completion.acquisition_provenance
        )
    return payload


def _slice_completion_from_payload(
    payload: object,
) -> HistoricalWebSearchSliceCompletion:
    if not isinstance(payload, dict):
        raise HistoricalWebSearchLibraryError(
            "historical web_search slice-completion payload must decode to a JSON "
            "object."
        )
    try:
        completion = HistoricalWebSearchSliceCompletion(
            target_key=_require_string(payload["target_key"], field_name="target_key"),
            query=_require_string(payload["query"], field_name="query"),
            visible_at=_parse_timestamp(payload["visible_at"], field_name="visible_at"),
            outcome=cast(
                Literal["source_appended", "no_source_found"],
                _require_string(payload["outcome"], field_name="outcome"),
            ),
            acquisition_provenance=_optional_acquisition_provenance_from_payload(
                payload.get("acquisition_provenance"),
                field_name="acquisition_provenance",
            ),
        )
        completion_id = _require_string(
            payload["completion_id"],
            field_name="completion_id",
        )
        expected_completion_id = historical_web_search_slice_completion_id(completion)
        if completion_id != expected_completion_id:
            raise HistoricalWebSearchLibraryError(
                "historical web_search slice-completion payload completion_id does not "
                "match deterministic identity."
            )
        return completion
    except KeyError as exc:
        raise HistoricalWebSearchLibraryError(
            "historical web_search slice-completion payload is missing a required "
            f"field: {exc.args[0]}."
        ) from exc


def _record_from_payload(payload: object) -> HistoricalWebSearchRecord:
    if not isinstance(payload, dict):
        raise HistoricalWebSearchLibraryError(
            "historical web_search partition payload must decode to a JSON object."
        )
    unexpected_fields = set(payload) - _SERIALIZED_RECORD_FIELDS - _DERIVED_RECORD_FIELDS
    if unexpected_fields:
        raise HistoricalWebSearchLibraryError(
            "historical web_search partition payload contains unsupported fields: "
            f"{', '.join(sorted(unexpected_fields))}."
        )
    try:
        published_at_raw = payload["published_at"]
        published_at = (
            None
            if published_at_raw is None
            else _parse_timestamp(published_at_raw, field_name="published_at")
        )
        acquisition_provenance = _optional_acquisition_provenance_from_payload(
            payload.get("acquisition_provenance"),
            field_name="acquisition_provenance",
        )
        record = HistoricalWebSearchRecord(
            target_key=_require_string(payload["target_key"], field_name="target_key"),
            query=_require_string(payload["query"], field_name="query"),
            source_ref=_require_string(payload["source_ref"], field_name="source_ref"),
            title=_require_string(payload["title"], field_name="title"),
            content=_require_string(payload["content"], field_name="content"),
            labels=_require_labels(payload["labels"]),
            published_at=published_at,
            discovered_at=_parse_timestamp(
                payload["discovered_at"],
                field_name="discovered_at",
            ),
            visible_at=_parse_timestamp(payload["visible_at"], field_name="visible_at"),
            acquisition_provenance=acquisition_provenance,
        )
        payload_candidate_fingerprint = payload.get("candidate_fingerprint")
        if payload_candidate_fingerprint is not None:
            expected_candidate_fingerprint = historical_web_search_candidate_fingerprint(
                record
            )
            if _require_string(
                payload_candidate_fingerprint,
                field_name="candidate_fingerprint",
            ) != expected_candidate_fingerprint:
                raise HistoricalWebSearchLibraryError(
                    "historical web_search partition payload candidate_fingerprint "
                    "does not match deterministic identity."
                )
        payload_observation_id = payload.get("observation_id")
        if payload_observation_id is not None:
            expected_observation_id = historical_web_search_observation_id(record)
            if (
                _require_string(
                    payload_observation_id,
                    field_name="observation_id",
                )
                != expected_observation_id
            ):
                raise HistoricalWebSearchLibraryError(
                    "historical web_search partition payload observation_id does not "
                    "match deterministic identity."
                )
        return record
    except KeyError as exc:
        raise HistoricalWebSearchLibraryError(
            "historical web_search partition payload is missing a required field: "
            f"{exc.args[0]}."
        ) from exc


def _iter_partition_payloads(partition_path: Path) -> tuple[dict[str, object], ...]:
    if not partition_path.exists():
        return ()
    if not partition_path.is_file():
        raise HistoricalWebSearchLibraryError(
            f"historical web_search partition must be a file: {partition_path}"
        )
    try:
        lines = partition_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HistoricalWebSearchLibraryError(
            f"Failed to read historical web_search partition {partition_path}: {exc}"
        ) from exc

    payloads: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise HistoricalWebSearchLibraryError(
                "historical web_search partition contains invalid JSON: "
                f"path={partition_path} line={line_number}."
            ) from exc
        if not isinstance(payload, dict):
            raise HistoricalWebSearchLibraryError(
                "historical web_search partition lines must decode to JSON objects: "
                f"path={partition_path} line={line_number}."
            )
        payloads.append(payload)
    return tuple(payloads)


def _normalize_partition_day(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return validate_timestamp(
            value,
            field_name="visible_at",
            error_type=HistoricalWebSearchLibraryError,
        ).astimezone(UTC).date()
    if isinstance(value, date):
        return value
    raise HistoricalWebSearchLibraryError(
        "visible_at must be a date or timezone-aware datetime."
    )


def _parse_timestamp(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise HistoricalWebSearchLibraryError(f"{field_name} must be an ISO8601 string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HistoricalWebSearchLibraryError(
            f"{field_name} must be a valid ISO8601 string."
        ) from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=HistoricalWebSearchLibraryError,
    )


def _require_labels(value: object) -> list[str]:
    if not isinstance(value, list):
        raise HistoricalWebSearchLibraryError("labels must be provided as a list[str].")
    return [label for label in value]


def _require_string_list(
    value: object,
    *,
    field_name: str,
    require_non_empty: bool,
) -> list[str]:
    if not isinstance(value, list):
        raise HistoricalWebSearchLibraryError(f"{field_name} must be a list[str].")
    normalized = [
        _validate_non_blank_text(item, field_name=field_name)
        for item in value
    ]
    if require_non_empty and not normalized:
        raise HistoricalWebSearchLibraryError(f"{field_name} must not be empty.")
    return normalized


def _require_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise HistoricalWebSearchLibraryError(f"{field_name} must be a string.")
    return value


def _serialize_acquisition_provenance(
    provenance: WebSearchAcquisitionProvenance,
) -> dict[str, object]:
    return {
        "prompt_profile_id": provenance.prompt_profile_id,
        "search_intent_hash": provenance.search_intent_hash,
        "control_language": provenance.control_language,
        "retrieval_languages": list(provenance.retrieval_languages),
    }


def _optional_acquisition_provenance_from_payload(
    value: object,
    *,
    field_name: str,
) -> WebSearchAcquisitionProvenance | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HistoricalWebSearchLibraryError(
            f"{field_name} must decode to a JSON object when present."
        )
    unexpected_fields = set(value) - _SERIALIZED_PROVENANCE_FIELDS
    if unexpected_fields:
        raise HistoricalWebSearchLibraryError(
            f"{field_name} contains unsupported fields: "
            f"{', '.join(sorted(unexpected_fields))}."
        )
    return WebSearchAcquisitionProvenance(
        prompt_profile_id=_require_string(
            value["prompt_profile_id"],
            field_name=f"{field_name}.prompt_profile_id",
        ),
        search_intent_hash=_require_string(
            value["search_intent_hash"],
            field_name=f"{field_name}.search_intent_hash",
        ),
        control_language=_require_string(
            value["control_language"],
            field_name=f"{field_name}.control_language",
        ),
        retrieval_languages=tuple(
            _require_string_list(
                value["retrieval_languages"],
                field_name=f"{field_name}.retrieval_languages",
                require_non_empty=True,
            )
        ),
    )


def _validate_non_blank_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise HistoricalWebSearchLibraryError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise HistoricalWebSearchLibraryError(f"{field_name} must not be blank.")
    return normalized


def _reject_operator_only_labels(labels: list[str]) -> None:
    if any(label.startswith(_FORBIDDEN_OPERATOR_ONLY_LABEL_PREFIXES) for label in labels):
        raise HistoricalWebSearchLibraryError(
            "historical web_search labels must not contain operator-only metadata labels."
        )


def _validate_web_search_classification_labels(labels: list[str]) -> None:
    try:
        extract_event_type(labels)
        source_kind = extract_source_kind(labels)
    except SourcePolicyError as exc:
        raise HistoricalWebSearchLibraryError(str(exc)) from exc
    if source_kind == "operator_brief":
        raise HistoricalWebSearchLibraryError(
            "historical web_search labels must not contain source_kind:operator_brief."
        )


__all__ = [
    "HistoricalWebSearchLibraryError",
    "HistoricalWebSearchRecord",
    "HistoricalWebSearchSliceCompletion",
    "HistoricalWebSearchSliceCompletionWriteReceipt",
    "HistoricalWebSearchTargetLayout",
    "HistoricalWebSearchWriteReceipt",
    "WebSearchAcquisitionProvenance",
    "append_historical_web_search_slice_completion",
    "append_historical_web_search_record",
    "build_historical_web_search_target_layout",
    "has_historical_web_search_slice_completion_marker",
    "historical_web_search_candidate_fingerprint",
    "historical_web_search_observation_id",
    "historical_web_search_partition_path",
    "historical_web_search_record_id",
    "historical_web_search_slice_completion_id",
    "historical_web_search_slice_completion_partition_path",
    "map_live_web_search_to_historical_record",
    "normalize_historical_web_search_payload",
    "read_historical_web_search_records",
    "read_historical_web_search_slice_completions",
    "write_historical_web_search_backfill_record",
    "write_live_web_search_record",
]
