"""Append-only sidecar registry for explicit ResearchMemory claim blocks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

from event_trader.contracts.research_memory import resolve_page_ref
from event_trader.storage import WorkspaceLayout

ResearchClaimStatus = Literal[
    "active",
    "contested",
    "superseded",
    "invalidated",
    "stale",
    "archived",
]

_CLAIM_BLOCK_RE = re.compile(
    r"<!--\s*research-claim:start\s+(?P<attrs>.*?)-->\s*\n"
    r"(?P<body>.*?)"
    r"\n<!--\s*research-claim:end\s*-->",
    re.DOTALL | re.IGNORECASE,
)
_CLAIM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")
_CLAIM_STATUSES = {
    "active",
    "contested",
    "superseded",
    "invalidated",
    "stale",
    "archived",
}


class ResearchClaimRegistryError(ValueError):
    """Raised when explicit ResearchMemory claim lifecycle data is malformed."""


@dataclass(frozen=True, slots=True)
class ResearchClaimUpdate:
    status: ResearchClaimStatus
    claim_id: str | None
    claim_text: str
    supporting_event_ids: tuple[str, ...]
    contradicting_event_ids: tuple[str, ...]
    superseded_by: str | None
    visible_from: datetime | None


@dataclass(frozen=True, slots=True)
class ResearchClaimChange:
    change_id: str
    claim_id: str
    scope_key: str
    target_key: str | None
    page_path: str
    section_name: str
    status: ResearchClaimStatus
    claim_text: str
    supporting_event_ids: tuple[str, ...]
    contradicting_event_ids: tuple[str, ...]
    superseded_by: str | None
    visible_from: datetime
    created_at: datetime
    updated_at: datetime
    source_write_receipt_id: str

    def to_json_payload(self) -> dict[str, object]:
        return {
            "change_id": self.change_id,
            "claim_id": self.claim_id,
            "scope_key": self.scope_key,
            "target_key": self.target_key,
            "page_path": self.page_path,
            "section_name": self.section_name,
            "status": self.status,
            "claim_text": self.claim_text,
            "supporting_event_ids": list(self.supporting_event_ids),
            "contradicting_event_ids": list(self.contradicting_event_ids),
            "superseded_by": self.superseded_by,
            "visible_from": self.visible_from.isoformat(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "source_write_receipt_id": self.source_write_receipt_id,
        }

    @classmethod
    def from_json_payload(cls, payload: dict[str, object]) -> ResearchClaimChange:
        return cls(
            change_id=_require_text(payload, "change_id"),
            claim_id=_require_text(payload, "claim_id"),
            scope_key=_require_text(payload, "scope_key"),
            target_key=_optional_text(payload.get("target_key")),
            page_path=_require_text(payload, "page_path"),
            section_name=_optional_text(payload.get("section_name")) or "",
            status=_parse_status(_require_text(payload, "status")),
            claim_text=_require_text(payload, "claim_text"),
            supporting_event_ids=_parse_text_tuple(payload.get("supporting_event_ids")),
            contradicting_event_ids=_parse_text_tuple(
                payload.get("contradicting_event_ids")
            ),
            superseded_by=_optional_text(payload.get("superseded_by")),
            visible_from=_parse_datetime(_require_text(payload, "visible_from")),
            created_at=_parse_datetime(_require_text(payload, "created_at")),
            updated_at=_parse_datetime(_require_text(payload, "updated_at")),
            source_write_receipt_id=_require_text(payload, "source_write_receipt_id"),
        )


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    claim_id: str
    scope_key: str
    target_key: str | None
    page_path: str
    section_name: str
    claim_text: str
    status: ResearchClaimStatus
    supporting_event_ids: tuple[str, ...]
    contradicting_event_ids: tuple[str, ...]
    superseded_by: str | None
    visible_from: datetime
    created_at: datetime
    updated_at: datetime
    source_write_receipt_ids: tuple[str, ...]


class FileBackedClaimRegistry:
    """Rebuildable append-only ResearchMemory claim registry."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise ResearchClaimRegistryError(
                "layout must be a WorkspaceLayout instance."
            )
        self._layout = layout

    def append_change(self, change: ResearchClaimChange) -> str:
        path = self.change_path(
            scope_key=change.scope_key,
            year_month=change.visible_from.strftime("%Y-%m"),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if _jsonl_record_exists(path, record_id=change.change_id):
            return change.change_id
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(change.to_json_payload(), ensure_ascii=False))
            handle.write("\n")
        return change.change_id

    def read_changes(
        self,
        *,
        scope_key: str,
        as_of: datetime | None = None,
    ) -> tuple[ResearchClaimChange, ...]:
        normalized_scope = _normalize_scope_key(scope_key)
        cutoff = _ensure_datetime(as_of) if as_of is not None else None
        root = self._layout.runtime_root / "research_claims" / normalized_scope / "changes"
        if not root.exists():
            return ()
        changes: list[ResearchClaimChange] = []
        for path in sorted(root.glob("*.jsonl")):
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    normalized = line.strip()
                    if not normalized:
                        continue
                    payload = json.loads(normalized)
                    if not isinstance(payload, dict):
                        raise ResearchClaimRegistryError(
                            "claim registry change entries must be JSON objects."
                        )
                    change = ResearchClaimChange.from_json_payload(payload)
                    if cutoff is not None and change.visible_from > cutoff:
                        continue
                    changes.append(change)
        return tuple(
            sorted(
                changes,
                key=lambda item: (item.visible_from, item.updated_at, item.change_id),
            )
        )

    def read_claims(
        self,
        *,
        scope_key: str,
        as_of: datetime | None = None,
        statuses: tuple[ResearchClaimStatus, ...] | None = None,
    ) -> tuple[ResearchClaim, ...]:
        projection = _project_claims(self.read_changes(scope_key=scope_key, as_of=as_of))
        status_filter = set(statuses) if statuses is not None else None
        claims = tuple(
            claim
            for claim in projection.values()
            if status_filter is None or claim.status in status_filter
        )
        return tuple(sorted(claims, key=lambda item: item.claim_id))

    def read_active_claims(
        self,
        *,
        scope_key: str,
        as_of: datetime | None = None,
    ) -> tuple[ResearchClaim, ...]:
        return self.read_claims(scope_key=scope_key, as_of=as_of, statuses=("active",))

    def change_path(self, *, scope_key: str, year_month: str) -> Path:
        normalized_scope = _normalize_scope_key(scope_key)
        normalized_month = year_month.strip() if isinstance(year_month, str) else ""
        if not normalized_month:
            raise ResearchClaimRegistryError("year_month must not be blank.")
        return (
            self._layout.runtime_root
            / "research_claims"
            / normalized_scope
            / "changes"
            / f"{normalized_month}.jsonl"
        ).resolve(strict=False)


def extract_claim_updates_from_markdown(content_md: str) -> tuple[ResearchClaimUpdate, ...]:
    if not isinstance(content_md, str):
        raise ResearchClaimRegistryError("content_md must be a string.")
    updates: list[ResearchClaimUpdate] = []
    for match in _CLAIM_BLOCK_RE.finditer(content_md):
        try:
            attrs = _parse_attrs(match.group("attrs"))
            status = _parse_status(attrs.get("status", ""))
            claim_id = _optional_claim_id(attrs.get("id"))
            fields = _parse_claim_body(match.group("body"))
            claim_text = fields.get("claim", "").strip()
            if not claim_text:
                continue
            updates.append(
                ResearchClaimUpdate(
                    status=status,
                    claim_id=claim_id,
                    claim_text=claim_text,
                    supporting_event_ids=_parse_csv_text(
                        fields.get("supporting_event_ids", "")
                    ),
                    contradicting_event_ids=_parse_csv_text(
                        fields.get("contradicting_event_ids", "")
                    ),
                    superseded_by=_optional_claim_id(fields.get("superseded_by", "")),
                    visible_from=(
                        None
                        if not fields.get("visible_from", "").strip()
                        else _parse_datetime(fields["visible_from"].strip())
                    ),
                )
            )
        except ResearchClaimRegistryError:
            continue
    return tuple(updates)


def apply_claim_updates_for_write(
    *,
    registry: FileBackedClaimRegistry,
    page_path: str,
    section_name: str,
    content_md: str,
    source_write_receipt_id: str,
    business_at: datetime,
    committed_at: datetime,
) -> tuple[str, ...]:
    page_ref = resolve_page_ref(page_path)
    scope_key = page_ref.target_key or "shared"
    changed_ids: list[str] = []
    existing_claim_ids = {
        claim.claim_id for claim in registry.read_claims(scope_key=scope_key)
    }
    changes: list[ResearchClaimChange] = []
    business_time = _ensure_datetime(business_at)
    committed = _ensure_datetime(committed_at)
    for update in extract_claim_updates_from_markdown(content_md):
        claim_id = update.claim_id or _generated_claim_id(
            target_key=page_ref.target_key,
            page_path=page_ref.page_path,
            section_name=section_name,
            claim_text=update.claim_text,
        )
        if update.status != "active" and claim_id not in existing_claim_ids:
            continue
        visible_from = update.visible_from or business_time
        if visible_from < business_time:
            raise ResearchClaimRegistryError(
                "claim visible_from must not be earlier than the source write "
                "business_at."
            )
        changes.append(
            ResearchClaimChange(
                change_id=_change_id(
                    claim_id=claim_id,
                    status=update.status,
                    claim_text=update.claim_text,
                    source_write_receipt_id=source_write_receipt_id,
                    visible_from=visible_from,
                ),
                claim_id=claim_id,
                scope_key=scope_key,
                target_key=page_ref.target_key,
                page_path=page_ref.page_path,
                section_name=section_name,
                status=update.status,
                claim_text=update.claim_text,
                supporting_event_ids=update.supporting_event_ids,
                contradicting_event_ids=update.contradicting_event_ids,
                superseded_by=update.superseded_by,
                visible_from=visible_from,
                created_at=committed,
                updated_at=committed,
                source_write_receipt_id=source_write_receipt_id,
            )
        )
        changed_ids.append(claim_id)
        existing_claim_ids.add(claim_id)
    for change in changes:
        registry.append_change(change)
    return tuple(dict.fromkeys(changed_ids))


def _project_claims(
    changes: tuple[ResearchClaimChange, ...],
) -> dict[str, ResearchClaim]:
    projection: dict[str, ResearchClaim] = {}
    for change in changes:
        previous = projection.get(change.claim_id)
        supporting_ids = _merge_text(
            previous.supporting_event_ids if previous is not None else (),
            change.supporting_event_ids,
        )
        contradicting_ids = _merge_text(
            previous.contradicting_event_ids if previous is not None else (),
            change.contradicting_event_ids,
        )
        source_receipts = _merge_text(
            previous.source_write_receipt_ids if previous is not None else (),
            (change.source_write_receipt_id,),
        )
        projection[change.claim_id] = ResearchClaim(
            claim_id=change.claim_id,
            scope_key=change.scope_key,
            target_key=change.target_key,
            page_path=change.page_path,
            section_name=change.section_name,
            claim_text=change.claim_text or (previous.claim_text if previous else ""),
            status=change.status,
            supporting_event_ids=supporting_ids,
            contradicting_event_ids=contradicting_ids,
            superseded_by=change.superseded_by or (
                previous.superseded_by if previous is not None else None
            ),
            visible_from=change.visible_from,
            created_at=previous.created_at if previous is not None else change.created_at,
            updated_at=change.updated_at,
            source_write_receipt_ids=source_receipts,
        )
    return projection


def _parse_attrs(raw_attrs: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for token in raw_attrs.split():
        key, separator, value = token.partition("=")
        if separator:
            attrs[key.strip().lower()] = value.strip()
    return attrs


def _parse_claim_body(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in body.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip().lower()] = value.strip()
    return fields


def _parse_status(value: str) -> ResearchClaimStatus:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if normalized not in _CLAIM_STATUSES:
        raise ResearchClaimRegistryError(f"unknown research claim status: {value!r}")
    return cast(ResearchClaimStatus, normalized)


def _optional_claim_id(value: str | None) -> str | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    if not _CLAIM_ID_RE.fullmatch(normalized):
        raise ResearchClaimRegistryError("claim id contains unsupported characters.")
    return normalized


def _generated_claim_id(
    *,
    target_key: str | None,
    page_path: str,
    section_name: str,
    claim_text: str,
) -> str:
    identity = {
        "target_key": target_key,
        "page_path": page_path,
        "section_name": section_name,
        "claim_text": " ".join(claim_text.split()).lower(),
    }
    digest = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"claim:{digest}"


def _change_id(
    *,
    claim_id: str,
    status: ResearchClaimStatus,
    claim_text: str,
    source_write_receipt_id: str,
    visible_from: datetime,
) -> str:
    identity = {
        "claim_id": claim_id,
        "status": status,
        "claim_text": claim_text,
        "source_write_receipt_id": source_write_receipt_id,
        "visible_from": visible_from.isoformat(),
    }
    digest = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"research-claim-change:{digest}"


def _merge_text(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*left, *right)))


def _parse_csv_text(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _require_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ResearchClaimRegistryError(f"{key} must be a non-empty string.")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = value.strip() if isinstance(value, str) else ""
    return normalized or None


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ResearchClaimRegistryError("claim timestamps must be ISO datetimes.") from exc
    return _ensure_datetime(parsed)


def _ensure_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ResearchClaimRegistryError("claim timestamps must be datetimes.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ResearchClaimRegistryError("claim timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _normalize_scope_key(scope_key: str) -> str:
    normalized = scope_key.strip() if isinstance(scope_key, str) else ""
    if not normalized:
        raise ResearchClaimRegistryError("scope_key must not be blank.")
    return normalized


def _jsonl_record_exists(path: Path, *, record_id: str) -> bool:
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            normalized = line.strip()
            if not normalized:
                continue
            try:
                payload: Any = json.loads(normalized)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("change_id") == record_id:
                return True
    return False
