"""Append-only stores for PM-review lifecycle records."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol, TypeVar

from event_trader.contracts._validators import validate_target_key
from event_trader.pm_review.contracts import (
    CandidateReviewAnchor,
    PMPositionReviewTriggers,
    PMReviewContractError,
    PMReviewDispatchConsideration,
    PMReviewFailureRecord,
    PMReviewEpisodeMemoryReadReceipt,
    PMReviewRequest,
    PMReviewToolReadReceipt,
    parse_candidate_review_anchor,
    parse_pm_position_review_triggers,
    parse_pm_review_dispatch_consideration,
    parse_pm_review_failure_record,
    parse_pm_review_request,
    parse_pm_review_episode_memory_read_receipt,
    parse_pm_review_tool_read_receipt,
)
from event_trader.pm_review.position_review_gate import (
    PMPositionReviewGateError,
    PMPositionReviewGateRecord,
    parse_pm_position_review_gate_record,
)
from event_trader.storage import WorkspaceLayout

_REQUESTS_ROOT = Path("pm_review") / "requests"
_CANDIDATE_ANCHORS_ROOT = Path("pm_review") / "candidate_anchors"
_TOOL_READS_ROOT = Path("pm_review") / "tool_reads"
_FAILURES_ROOT = Path("pm_review") / "failures"
_DISPATCH_CONSIDERATIONS_ROOT = Path("pm_review") / "dispatch_considerations"
_EPISODE_MEMORY_READS_ROOT = Path("pm_review") / "episode_memory_reads"
_POSITION_REVIEW_TRIGGERS_ROOT = Path("pm_review") / "position_review_triggers"
_POSITION_REVIEW_GATE_ROOT = Path("pm_review") / "position_review_gate"

_RecordT = TypeVar("_RecordT")


class _SupportsJsonPayload(Protocol):
    def to_json_payload(self) -> dict[str, object]: ...


class PMReviewStoreError(ValueError):
    """Raised when PM-review persistence is invalid or conflicting."""


@dataclass(frozen=True, slots=True)
class PersistedPMReviewRequest:
    record: PMReviewRequest
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedCandidateReviewAnchor:
    record: CandidateReviewAnchor
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedPMReviewToolReadReceipt:
    record: PMReviewToolReadReceipt
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedPMReviewFailureRecord:
    record: PMReviewFailureRecord
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedPMReviewEpisodeMemoryReadReceipt:
    record: PMReviewEpisodeMemoryReadReceipt
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedPMReviewDispatchConsideration:
    record: PMReviewDispatchConsideration
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedPMPositionReviewTriggers:
    record: PMPositionReviewTriggers
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedPMPositionReviewGateRecord:
    record: PMPositionReviewGateRecord
    path: Path
    line_number: int
    record_hash: str


class PMReviewRequestStore:
    """Append and read PMReviewRequest records by target/month."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def append(self, request: PMReviewRequest) -> Path:
        if not isinstance(request, PMReviewRequest):
            raise PMReviewStoreError("request must be a PMReviewRequest instance.")
        return _append_record(
            layout=self._layout,
            root=_REQUESTS_ROOT,
            target_key=request.target_key,
            business_at=request.business_at,
            record=request,
            record_id=request.request_id,
            id_label="request_id",
        )

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedPMReviewRequest, ...]:
        return tuple(
            PersistedPMReviewRequest(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_typed_records(
                layout=self._layout,
                root=_REQUESTS_ROOT,
                target_key=target_key,
                year_month=year_month,
                parser=parse_pm_review_request,
                label="PM review request",
            )
        )

    def path_for(self, target_key: str, business_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _REQUESTS_ROOT,
            target_key=target_key,
            business_at=business_at,
        )


class PMReviewFailureStore:
    """Append and read PMReviewFailureRecord records by target/month."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def append(self, failure: PMReviewFailureRecord) -> Path:
        if not isinstance(failure, PMReviewFailureRecord):
            raise PMReviewStoreError("failure must be a PMReviewFailureRecord instance.")
        return _append_record(
            layout=self._layout,
            root=_FAILURES_ROOT,
            target_key=failure.target_key,
            business_at=failure.business_at,
            record=failure,
            record_id=failure.failure_id,
            id_label="failure_id",
        )

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedPMReviewFailureRecord, ...]:
        return tuple(
            PersistedPMReviewFailureRecord(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_typed_records(
                layout=self._layout,
                root=_FAILURES_ROOT,
                target_key=target_key,
                year_month=year_month,
                parser=parse_pm_review_failure_record,
                label="PM review failure",
            )
        )

    def path_for(self, target_key: str, business_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _FAILURES_ROOT,
            target_key=target_key,
            business_at=business_at,
        )


class PMReviewDispatchConsiderationStore:
    """Append and read PMReviewDispatchConsideration records by target/month."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def append(self, consideration: PMReviewDispatchConsideration) -> Path:
        if not isinstance(consideration, PMReviewDispatchConsideration):
            raise PMReviewStoreError(
                "consideration must be a PMReviewDispatchConsideration instance."
            )
        return _append_record(
            layout=self._layout,
            root=_DISPATCH_CONSIDERATIONS_ROOT,
            target_key=consideration.target_key,
            business_at=consideration.business_at,
            record=consideration,
            record_id=consideration.consideration_id,
            id_label="consideration_id",
        )

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedPMReviewDispatchConsideration, ...]:
        return tuple(
            PersistedPMReviewDispatchConsideration(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_typed_records(
                layout=self._layout,
                root=_DISPATCH_CONSIDERATIONS_ROOT,
                target_key=target_key,
                year_month=year_month,
                parser=parse_pm_review_dispatch_consideration,
                label="PM review dispatch consideration",
            )
        )

    def path_for(self, target_key: str, business_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _DISPATCH_CONSIDERATIONS_ROOT,
            target_key=target_key,
            business_at=business_at,
        )


class CandidateReviewAnchorStore:
    """Append and read CandidateReviewAnchor records by target/month."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def append(self, anchor: CandidateReviewAnchor) -> Path:
        if not isinstance(anchor, CandidateReviewAnchor):
            raise PMReviewStoreError("anchor must be a CandidateReviewAnchor instance.")
        return _append_record(
            layout=self._layout,
            root=_CANDIDATE_ANCHORS_ROOT,
            target_key=anchor.target_key,
            business_at=anchor.created_at,
            record=anchor,
            record_id=anchor.anchor_id,
            id_label="anchor_id",
        )

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedCandidateReviewAnchor, ...]:
        return tuple(
            PersistedCandidateReviewAnchor(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_typed_records(
                layout=self._layout,
                root=_CANDIDATE_ANCHORS_ROOT,
                target_key=target_key,
                year_month=year_month,
                parser=parse_candidate_review_anchor,
                label="candidate review anchor",
            )
        )

    def path_for(self, target_key: str, created_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _CANDIDATE_ANCHORS_ROOT,
            target_key=target_key,
            business_at=created_at,
        )


class PMReviewToolReadReceiptStore:
    """Append and read PMReviewToolReadReceipt records by target/month."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def append(self, receipt: PMReviewToolReadReceipt) -> Path:
        if not isinstance(receipt, PMReviewToolReadReceipt):
            raise PMReviewStoreError("receipt must be a PMReviewToolReadReceipt instance.")
        return _append_record(
            layout=self._layout,
            root=_TOOL_READS_ROOT,
            target_key=receipt.target_key,
            business_at=receipt.business_at,
            record=receipt,
            record_id=receipt.read_id,
            id_label="read_id",
        )

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedPMReviewToolReadReceipt, ...]:
        return tuple(
            PersistedPMReviewToolReadReceipt(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_typed_records(
                layout=self._layout,
                root=_TOOL_READS_ROOT,
                target_key=target_key,
                year_month=year_month,
                parser=parse_pm_review_tool_read_receipt,
                label="PM review tool read receipt",
            )
        )

    def path_for(self, target_key: str, business_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _TOOL_READS_ROOT,
            target_key=target_key,
            business_at=business_at,
        )


class PMReviewEpisodeMemoryReadReceiptStore:
    """Append and read PMReviewEpisodeMemoryReadReceipt records by target/month."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def append(self, receipt: PMReviewEpisodeMemoryReadReceipt) -> Path:
        if not isinstance(receipt, PMReviewEpisodeMemoryReadReceipt):
            raise PMReviewStoreError(
                "receipt must be a PMReviewEpisodeMemoryReadReceipt instance."
            )
        return _append_record(
            layout=self._layout,
            root=_EPISODE_MEMORY_READS_ROOT,
            target_key=receipt.target_key,
            business_at=receipt.business_at,
            record=receipt,
            record_id=receipt.request_id,
            id_label="request_id",
        )

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedPMReviewEpisodeMemoryReadReceipt, ...]:
        return tuple(
            PersistedPMReviewEpisodeMemoryReadReceipt(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_typed_records(
                layout=self._layout,
                root=_EPISODE_MEMORY_READS_ROOT,
                target_key=target_key,
                year_month=year_month,
                parser=parse_pm_review_episode_memory_read_receipt,
                label="PM review episode memory read receipt",
            )
        )

    def path_for(self, target_key: str, business_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _EPISODE_MEMORY_READS_ROOT,
            target_key=target_key,
            business_at=business_at,
        )


class PMPositionReviewTriggerStore:
    """Append and read PMPositionReviewTriggers records by target/month."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def append(self, triggers: PMPositionReviewTriggers) -> Path:
        if not isinstance(triggers, PMPositionReviewTriggers):
            raise PMReviewStoreError("triggers must be a PMPositionReviewTriggers instance.")
        return _append_record(
            layout=self._layout,
            root=_POSITION_REVIEW_TRIGGERS_ROOT,
            target_key=triggers.target_key,
            business_at=triggers.business_at,
            record=triggers,
            record_id=triggers.pm_review_request_id,
            id_label="pm_review_request_id",
        )

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedPMPositionReviewTriggers, ...]:
        return tuple(
            PersistedPMPositionReviewTriggers(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_typed_records(
                layout=self._layout,
                root=_POSITION_REVIEW_TRIGGERS_ROOT,
                target_key=target_key,
                year_month=year_month,
                parser=parse_pm_position_review_triggers,
                label="PM position review triggers",
            )
        )

    def latest_for_request(
        self,
        *,
        target_key: str,
        pm_review_request_id: str,
    ) -> PersistedPMPositionReviewTriggers | None:
        matches = tuple(
            persisted
            for persisted in self.read_records(target_key=target_key)
            if persisted.record.pm_review_request_id == pm_review_request_id
        )
        if not matches:
            return None
        return sorted(
            matches,
            key=lambda item: (
                item.record.business_at,
                item.path.as_posix(),
                item.line_number,
            ),
        )[-1]

    def path_for(self, target_key: str, business_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _POSITION_REVIEW_TRIGGERS_ROOT,
            target_key=target_key,
            business_at=business_at,
        )


class PMPositionReviewGateStore:
    """Append and read PM position-review gate records by target/month."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def append(self, record: PMPositionReviewGateRecord) -> Path:
        if not isinstance(record, PMPositionReviewGateRecord):
            raise PMReviewStoreError(
                "record must be a PMPositionReviewGateRecord instance."
            )
        return _append_record(
            layout=self._layout,
            root=_POSITION_REVIEW_GATE_ROOT,
            target_key=record.target_key,
            business_at=record.business_at,
            record=record,
            record_id=record.gate_record_id,
            id_label="gate_record_id",
        )

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedPMPositionReviewGateRecord, ...]:
        return tuple(
            PersistedPMPositionReviewGateRecord(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_typed_records(
                layout=self._layout,
                root=_POSITION_REVIEW_GATE_ROOT,
                target_key=target_key,
                year_month=year_month,
                parser=parse_pm_position_review_gate_record,
                label="PM position review gate",
            )
        )

    def path_for(self, target_key: str, business_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _POSITION_REVIEW_GATE_ROOT,
            target_key=target_key,
            business_at=business_at,
        )


def _append_record(
    *,
    layout: WorkspaceLayout,
    root: Path,
    target_key: str,
    business_at: datetime,
    record,
    record_id: str,
    id_label: str,
) -> Path:
    path = _monthly_path(
        root=layout.runtime_root / root,
        target_key=target_key,
        business_at=business_at,
    )
    payload = record.to_json_payload()
    record_hash = _record_hash(payload)
    for persisted_record, _, _ in _read_typed_records(
        layout=layout,
        root=root,
        target_key=target_key,
        year_month=business_at.strftime("%Y-%m"),
        parser=_parser_for_root(root),
        label=root.as_posix(),
    ):
        if getattr(persisted_record, id_label) != record_id:
            continue
        if _record_hash(persisted_record.to_json_payload()) == record_hash:
            return path
        raise PMReviewStoreError(f"duplicate {id_label} has different payload.")
    _append_json_line(path, payload)
    return path


def _parser_for_root(root: Path) -> Callable[[Mapping[str, object]], _SupportsJsonPayload]:
    if root == _REQUESTS_ROOT:
        return parse_pm_review_request
    if root == _CANDIDATE_ANCHORS_ROOT:
        return parse_candidate_review_anchor
    if root == _TOOL_READS_ROOT:
        return parse_pm_review_tool_read_receipt
    if root == _FAILURES_ROOT:
        return parse_pm_review_failure_record
    if root == _DISPATCH_CONSIDERATIONS_ROOT:
        return parse_pm_review_dispatch_consideration
    if root == _EPISODE_MEMORY_READS_ROOT:
        return parse_pm_review_episode_memory_read_receipt
    if root == _POSITION_REVIEW_TRIGGERS_ROOT:
        return parse_pm_position_review_triggers
    if root == _POSITION_REVIEW_GATE_ROOT:
        return parse_pm_position_review_gate_record
    raise PMReviewStoreError("unsupported PM review store root.")


def _read_typed_records(
    *,
    layout: WorkspaceLayout,
    root: Path,
    target_key: str,
    year_month: str | None,
    parser: Callable[[Mapping[str, object]], _RecordT],
    label: str,
) -> tuple[tuple[_RecordT, Path, int], ...]:
    records: list[tuple[_RecordT, Path, int]] = []
    for payload, path, line_number in _read_payloads(
        paths=_paths(
            root=layout.runtime_root / root,
            target_key=target_key,
            year_month=year_month,
        ),
        target_key=target_key,
        label=label,
    ):
        try:
            records.append((parser(payload), path, line_number))
        except (PMReviewContractError, PMPositionReviewGateError) as exc:
            raise PMReviewStoreError(str(exc)) from exc
    return tuple(records)


def _validate_layout(layout: WorkspaceLayout) -> None:
    if not isinstance(layout, WorkspaceLayout):
        raise PMReviewStoreError("layout must be a WorkspaceLayout instance.")


def _monthly_path(*, root: Path, target_key: str, business_at: datetime) -> Path:
    if not isinstance(business_at, datetime):
        raise PMReviewStoreError("business_at must be a datetime.")
    normalized_target = validate_target_key(target_key, error_type=PMReviewStoreError)
    return (root / normalized_target / f"{business_at.strftime('%Y-%m')}.jsonl").resolve(
        strict=False
    )


def _paths(*, root: Path, target_key: str, year_month: str | None) -> tuple[Path, ...]:
    normalized_target = validate_target_key(target_key, error_type=PMReviewStoreError)
    target_root = root / normalized_target
    if year_month is not None:
        if not isinstance(year_month, str) or len(year_month) != 7:
            raise PMReviewStoreError("year_month must be YYYY-MM.")
        return ((target_root / f"{year_month}.jsonl").resolve(strict=False),)
    return tuple(sorted(target_root.glob("*.jsonl")))


def _read_payloads(
    *,
    paths: tuple[Path, ...],
    target_key: str,
    label: str,
) -> tuple[tuple[Mapping[str, object], Path, int], ...]:
    normalized_target = validate_target_key(target_key, error_type=PMReviewStoreError)
    records: list[tuple[Mapping[str, object], Path, int]] = []
    for path in paths:
        if not path.exists():
            continue
        if not path.is_file():
            raise PMReviewStoreError(f"{label} path must be a file: {path}")
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    raise PMReviewStoreError(f"{label} line {line_number} is invalid JSON.") from exc
                if not isinstance(payload, Mapping):
                    raise PMReviewStoreError(f"{label} line {line_number} must be an object.")
                if payload.get("target_key") != normalized_target:
                    raise PMReviewStoreError(f"{label} shard contains mixed target_key records.")
                records.append((payload, path, line_number))
    return tuple(records)


def _append_json_line(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(dict(payload), ensure_ascii=False))
            handle.write("\n")
    except OSError as exc:
        raise PMReviewStoreError(f"failed to append PM review record: {path}") from exc


def _record_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "CandidateReviewAnchorStore",
    "PMPositionReviewGateStore",
    "PMPositionReviewTriggerStore",
    "PMReviewDispatchConsiderationStore",
    "PMReviewFailureStore",
    "PMReviewRequestStore",
    "PMReviewToolReadReceiptStore",
    "PMReviewEpisodeMemoryReadReceiptStore",
    "PMReviewStoreError",
    "PersistedCandidateReviewAnchor",
    "PersistedPMPositionReviewGateRecord",
    "PersistedPMPositionReviewTriggers",
    "PersistedPMReviewDispatchConsideration",
    "PersistedPMReviewFailureRecord",
    "PersistedPMReviewRequest",
    "PersistedPMReviewToolReadReceipt",
    "PersistedPMReviewEpisodeMemoryReadReceipt",
]
