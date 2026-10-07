"""Project-owned bounded read tools for reflection workflows."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from inspect import signature
from typing import Any

from event_trader.analysis import FileBackedResearchMemoryReader
from event_trader.contracts._validators import (
    validate_event_id,
    validate_target_key,
    validate_timestamp,
)
from event_trader.decision_memory import (
    FileBackedDecisionEpisodeStore,
    project_decision_episodes,
)
from event_trader.evidence_ledger import FileBackedEvidenceLedger
from event_trader.evidence_ledger import read_evidence as read_ledger_evidence
from event_trader.evidence_ledger import (
    read_evidence_window as read_ledger_evidence_window,
)
from event_trader.integrations.bounded_context import bounded_text_payload
from event_trader.integrations.markdown_context import (
    MarkdownContextError,
    build_markdown_page_map,
    find_markdown_section,
)
from event_trader.reflection.agent_contract import (
    REFLECTION_TOOL_SERVER_NAME,
    REQUIRED_REFLECTION_TOOL_NAMES,
)
from event_trader.storage import WorkspaceLayout, validate_workspace_layout
from event_trader.validation import read_state_changes

DEFAULT_REFLECTION_PAGE_SIZE = 20
MAX_REFLECTION_PAGE_SIZE = 200
MAX_REFLECTION_EVENT_IDS_PER_CALL = 200

_PAGE_EXCERPT_CHAR_LIMIT = 4_000
_EVIDENCE_EXCERPT_CHAR_LIMIT = 1_200
_STATE_RATIONALE_EXCERPT_CHAR_LIMIT = 1_200
_DECISION_EPISODE_PAYLOAD_EXCERPT_CHAR_LIMIT = 2_000


@dataclass(frozen=True, slots=True)
class ReflectionToolSpec:
    """Canonical description and argument schema for one Reflection read tool."""

    name: str
    description: str
    input_schema: Mapping[str, object]


def _object_schema(
    *,
    properties: dict[str, object],
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = list(required)
    return schema


_NON_BLANK_STRING_SCHEMA: dict[str, object] = {
    "type": "string",
    "minLength": 1,
}
_PAGE_LIMIT_SCHEMA: dict[str, object] = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_REFLECTION_PAGE_SIZE,
}
_PAGE_OFFSET_SCHEMA: dict[str, object] = {
    "type": "integer",
    "minimum": 0,
}

REFLECTION_TOOL_SPECS: tuple[ReflectionToolSpec, ...] = (
    ReflectionToolSpec(
        name="list_evidence_window",
        description="List admitted evidence inside the bounded Reflection time window.",
        input_schema=_object_schema(
            properties={
                "start_at": _NON_BLANK_STRING_SCHEMA,
                "end_at": _NON_BLANK_STRING_SCHEMA,
                "target_keys": {
                    "type": "array",
                    "items": _NON_BLANK_STRING_SCHEMA,
                    "minItems": 1,
                    "uniqueItems": True,
                },
                "exclude_event_ids": {
                    "type": "array",
                    "items": _NON_BLANK_STRING_SCHEMA,
                },
                "limit": _PAGE_LIMIT_SCHEMA,
                "offset": _PAGE_OFFSET_SCHEMA,
            }
        ),
    ),
    ReflectionToolSpec(
        name="read_evidence",
        description="Read admitted evidence records by canonical event id list.",
        input_schema=_object_schema(
            properties={
                "event_ids": {
                    "type": "array",
                    "items": _NON_BLANK_STRING_SCHEMA,
                    "maxItems": MAX_REFLECTION_EVENT_IDS_PER_CALL,
                }
            },
            required=("event_ids",),
        ),
    ),
    ReflectionToolSpec(
        name="list_state_changes",
        description=("List canonical state changes for one target inside the visible window."),
        input_schema=_object_schema(
            properties={
                "target_key": _NON_BLANK_STRING_SCHEMA,
                "start_at": _NON_BLANK_STRING_SCHEMA,
                "end_at": _NON_BLANK_STRING_SCHEMA,
                "limit": _PAGE_LIMIT_SCHEMA,
                "offset": _PAGE_OFFSET_SCHEMA,
            }
        ),
    ),
    ReflectionToolSpec(
        name="list_decision_episodes",
        description=("List projected decision episodes for one target inside the visible window."),
        input_schema=_object_schema(
            properties={
                "target_key": _NON_BLANK_STRING_SCHEMA,
                "start_at": _NON_BLANK_STRING_SCHEMA,
                "end_at": _NON_BLANK_STRING_SCHEMA,
                "status": _NON_BLANK_STRING_SCHEMA,
                "limit": _PAGE_LIMIT_SCHEMA,
                "offset": _PAGE_OFFSET_SCHEMA,
            }
        ),
    ),
    ReflectionToolSpec(
        name="read_decision_episode",
        description="Read one projected decision episode and its bounded audit records.",
        input_schema=_object_schema(
            properties={
                "episode_id": {
                    "type": "string",
                    "pattern": "^decision-episode:",
                },
                "target_key": _NON_BLANK_STRING_SCHEMA,
            },
            required=("episode_id",),
        ),
    ),
    ReflectionToolSpec(
        name="read_target_page",
        description="Read one canonical target-scoped ResearchMemory page.",
        input_schema=_object_schema(
            properties={"page_path": _NON_BLANK_STRING_SCHEMA},
            required=("page_path",),
        ),
    ),
    ReflectionToolSpec(
        name="read_target_section",
        description="Read one named section from a target-scoped ResearchMemory page.",
        input_schema=_object_schema(
            properties={
                "page_path": _NON_BLANK_STRING_SCHEMA,
                "section_name": _NON_BLANK_STRING_SCHEMA,
            },
            required=("page_path", "section_name"),
        ),
    ),
    ReflectionToolSpec(
        name="search_target_memory",
        description="Search target-scoped ResearchMemory and return bounded excerpts.",
        input_schema=_object_schema(
            properties={
                "query": _NON_BLANK_STRING_SCHEMA,
                "target_key": _NON_BLANK_STRING_SCHEMA,
                "limit": _PAGE_LIMIT_SCHEMA,
                "offset": _PAGE_OFFSET_SCHEMA,
            },
            required=("query",),
        ),
    ),
)


class ReflectionToolGateway:
    """Serve the canonical Reflection reads without depending on an agent runtime."""

    def __init__(
        self,
        *,
        layout: WorkspaceLayout,
        configured_target_key: str | None = None,
        fixed_window: tuple[datetime, datetime] | None = None,
    ) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise RuntimeError("layout must be a WorkspaceLayout instance.")
        validate_workspace_layout(layout)
        self._layout = layout
        self._configured_target_key = (
            None
            if configured_target_key is None
            else validate_target_key(configured_target_key, error_type=RuntimeError)
        )
        self._fixed_window = _validate_fixed_window(fixed_window)
        self._ledger = FileBackedEvidenceLedger(layout)
        self._reader = FileBackedResearchMemoryReader(layout)
        self._decision_episode_store = FileBackedDecisionEpisodeStore(layout)

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Dispatch one validated Reflection read by its canonical contract name."""
        if tool_name not in REQUIRED_REFLECTION_TOOL_NAMES:
            raise RuntimeError(f"Unknown reflection tool {tool_name!r}.")
        if not isinstance(arguments, dict):
            raise RuntimeError("Reflection tool arguments must be an object.")
        operation = getattr(self, tool_name, None)
        if not callable(operation):
            raise RuntimeError(f"Reflection tool {tool_name!r} is not installed.")
        typed_operation = operation
        try:
            signature(typed_operation).bind(**arguments)
        except TypeError as exc:
            raise RuntimeError(
                f"Invalid arguments for reflection tool {tool_name!r}: {exc}"
            ) from exc
        return typed_operation(**arguments)

    def list_evidence_window(
        self,
        start_at: str | None = None,
        end_at: str | None = None,
        target_keys: list[str] | None = None,
        exclude_event_ids: list[str] | None = None,
        limit: int = DEFAULT_REFLECTION_PAGE_SIZE,
        offset: int = 0,
    ) -> str:
        """List admitted evidence inside a bounded time window."""
        window_start, window_end, window_source = self._resolve_window(
            start_at=start_at,
            end_at=end_at,
        )
        resolved_target_keys = self._resolve_target_keys(target_keys)
        resolved_excluded_event_ids = _normalize_event_ids(
            exclude_event_ids,
            field_name="exclude_event_ids",
        )
        records = [
            _serialize_evidence_record(record)
            for record in read_ledger_evidence_window(
                start_at=window_start,
                end_at=window_end,
                ledger=self._ledger,
                target_keys=resolved_target_keys,
                exclude_event_ids=resolved_excluded_event_ids,
            )
        ]
        paged_records, page = _paginate(records, limit=limit, offset=offset)
        return _json_text(
            {
                "items": paged_records,
                "page": page,
                "window": {
                    "start_at": window_start.isoformat(),
                    "end_at": window_end.isoformat(),
                    "source": window_source,
                },
                "target_keys": resolved_target_keys,
            }
        )

    def read_evidence(self, event_ids: list[str]) -> str:
        """Read admitted evidence records by canonical event id list."""
        if not isinstance(event_ids, list):
            raise RuntimeError("event_ids must be a list of event ids.")
        if len(event_ids) > MAX_REFLECTION_EVENT_IDS_PER_CALL:
            raise RuntimeError(
                f"event_ids must contain at most {MAX_REFLECTION_EVENT_IDS_PER_CALL} values."
            )
        records = [
            _serialize_evidence_record(record)
            for record in read_ledger_evidence(event_ids, ledger=self._ledger)
        ]
        if self._configured_target_key is not None:
            for record in records:
                if record["target_key"] != self._configured_target_key:
                    raise RuntimeError(
                        "read_evidence is scoped to one target_key by the configured "
                        f"reflection target {self._configured_target_key!r}."
                    )
        return _json_text({"items": records, "requested_event_ids": list(event_ids)})

    def list_state_changes(
        self,
        target_key: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: int = DEFAULT_REFLECTION_PAGE_SIZE,
        offset: int = 0,
    ) -> str:
        """List canonical state changes for one target, optionally inside a window."""
        resolved_target_key = self._resolve_target_key(target_key)
        window = self._resolve_window_for_optional_filter(
            start_at=start_at,
            end_at=end_at,
        )
        state_changes = read_state_changes(self._layout, resolved_target_key)
        filtered = [
            _serialize_state_change(item)
            for item in state_changes
            if _state_change_in_window(
                item,
                start_at=None if window is None else window[0],
                end_at=None if window is None else window[1],
            )
        ]
        paged_items, page = _paginate(filtered, limit=limit, offset=offset)
        return _json_text(
            {
                "items": paged_items,
                "page": page,
                "target_key": resolved_target_key,
                "window": _serialize_window(window),
            }
        )

    def list_decision_episodes(
        self,
        target_key: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_REFLECTION_PAGE_SIZE,
        offset: int = 0,
    ) -> str:
        """List projected decision episodes for one target, optionally filtered."""
        resolved_target_key = self._resolve_target_key(target_key)
        window = self._resolve_window_for_optional_filter(
            start_at=start_at,
            end_at=end_at,
        )
        normalized_status = _normalize_optional_status(status)
        projections = project_decision_episodes(
            self._decision_episode_store.read_records(target_key=resolved_target_key)
        )
        filtered = [
            _serialize_decision_episode_projection_summary(item)
            for item in projections
            if _decision_episode_in_window(
                item,
                start_at=None if window is None else window[0],
                end_at=None if window is None else window[1],
            )
            and (normalized_status is None or item.status == normalized_status)
        ]
        paged_items, page = _paginate(filtered, limit=limit, offset=offset)
        return _json_text(
            {
                "items": paged_items,
                "page": page,
                "target_key": resolved_target_key,
                "status": normalized_status,
                "window": _serialize_window(window),
            }
        )

    def read_decision_episode(
        self,
        episode_id: str,
        target_key: str | None = None,
    ) -> str:
        """Read one projected decision episode and its bounded audit records."""
        resolved_target_key = self._resolve_target_key(target_key)
        normalized_episode_id = _validate_decision_episode_id(episode_id)
        persisted = self._decision_episode_store.read_episode_records(
            target_key=resolved_target_key,
            episode_id=normalized_episode_id,
        )
        if not persisted:
            raise RuntimeError(
                "No decision episode records found for "
                f"episode_id={normalized_episode_id!r} "
                f"target_key={resolved_target_key!r}."
            )
        projections = project_decision_episodes(persisted)
        if len(projections) != 1:
            raise RuntimeError("Expected exactly one projected decision episode.")
        projection = projections[0]
        return _json_text(
            {
                "target_key": resolved_target_key,
                "episode": _serialize_decision_episode_projection_summary(projection),
                "records": [_serialize_decision_episode_record(item.record) for item in persisted],
            }
        )

    def read_target_page(self, page_path: str) -> str:
        """Read one canonical target page by page_path."""
        page = self._reader.read_page(page_path)
        self._validate_target_page_scope(page.page_path)
        return _json_text(_serialize_page_read(page.page_path, page.content_md))

    def read_target_section(self, page_path: str, section_name: str) -> str:
        """Read one named markdown section from a canonical target page."""
        page = self._reader.read_page(page_path)
        self._validate_target_page_scope(page.page_path)
        return _json_text(
            _serialize_section_read(
                page_path=page.page_path,
                content_md=page.content_md,
                section_name=section_name,
            )
        )

    def search_target_memory(
        self,
        query: str,
        target_key: str | None = None,
        limit: int = DEFAULT_REFLECTION_PAGE_SIZE,
        offset: int = 0,
    ) -> str:
        """Search target-scoped research-memory pages and return excerpt snippets."""
        resolved_target_key = self._resolve_target_key(target_key)
        matches = [
            asdict(match)
            for match in self._reader.search_wiki(
                query=query,
                scope=f"target:{resolved_target_key}",
            )
        ]
        paged_matches, page = _paginate(matches, limit=limit, offset=offset)
        return _json_text(
            {
                "items": paged_matches,
                "page": page,
                "target_key": resolved_target_key,
            }
        )

    def _resolve_window(
        self,
        *,
        start_at: str | None,
        end_at: str | None,
    ) -> tuple[datetime, datetime, str]:
        requested_window = _resolve_optional_window(start_at=start_at, end_at=end_at)
        if self._fixed_window is None:
            if requested_window is None:
                raise RuntimeError(
                    "list_evidence_window requires start_at and end_at when no fixed "
                    "reflection window is configured."
                )
            return requested_window[0], requested_window[1], "request"

        fixed_start, fixed_end = self._fixed_window
        if requested_window is None:
            return fixed_start, fixed_end, "fixed_env"

        requested_start, requested_end = requested_window
        clamped_start = max(requested_start, fixed_start)
        clamped_end = min(requested_end, fixed_end)
        if clamped_end <= clamped_start:
            raise RuntimeError(
                "Requested window does not overlap the configured fixed reflection window."
            )
        if clamped_start == requested_start and clamped_end == requested_end:
            return clamped_start, clamped_end, "request"
        return clamped_start, clamped_end, "request_clamped_to_fixed_env"

    def _resolve_window_for_optional_filter(
        self,
        *,
        start_at: str | None,
        end_at: str | None,
    ) -> tuple[datetime, datetime] | None:
        requested_window = _resolve_optional_window(start_at=start_at, end_at=end_at)
        if self._fixed_window is None:
            return requested_window
        if requested_window is None:
            return self._fixed_window

        requested_start, requested_end = requested_window
        fixed_start, fixed_end = self._fixed_window
        clamped_start = max(requested_start, fixed_start)
        clamped_end = min(requested_end, fixed_end)
        if clamped_end <= clamped_start:
            raise RuntimeError(
                "Requested window does not overlap the configured fixed reflection window."
            )
        return clamped_start, clamped_end

    def _resolve_target_key(self, target_key: str | None) -> str:
        if target_key is not None:
            validated = validate_target_key(target_key, error_type=RuntimeError)
            if self._configured_target_key is not None and validated != self._configured_target_key:
                raise RuntimeError(
                    "target_key is outside the configured reflection target scope "
                    f"{self._configured_target_key!r}."
                )
            return validated
        if self._configured_target_key is not None:
            return self._configured_target_key
        raise RuntimeError("target_key must be provided when no reflection target is configured.")

    def _resolve_target_keys(self, target_keys: list[str] | None) -> list[str] | None:
        if target_keys is not None:
            if not isinstance(target_keys, list):
                raise RuntimeError("target_keys must be a list of target keys when provided.")
            if not target_keys:
                raise RuntimeError("target_keys must not be an empty list when provided.")
            normalized: list[str] = []
            seen: set[str] = set()
            for target_key in target_keys:
                validated = validate_target_key(target_key, error_type=RuntimeError)
                if (
                    self._configured_target_key is not None
                    and validated != self._configured_target_key
                ):
                    raise RuntimeError(
                        "target_keys must stay inside the configured reflection target "
                        f"scope {self._configured_target_key!r}."
                    )
                if validated in seen:
                    raise RuntimeError("target_keys must not contain duplicates.")
                seen.add(validated)
                normalized.append(validated)
            return normalized

        if self._configured_target_key is None:
            return None
        return [self._configured_target_key]

    def _validate_target_page_scope(self, page_path: str) -> None:
        if not page_path.startswith("targets/"):
            raise RuntimeError("page_path must point to a target-scoped page under targets/.")
        if self._configured_target_key is None:
            return
        scoped_prefix = f"targets/{self._configured_target_key}/"
        if not page_path.startswith(scoped_prefix):
            raise RuntimeError(
                "page_path is outside the configured reflection target scope "
                f"{self._configured_target_key!r}."
            )


@dataclass(frozen=True, slots=True)
class ReflectionAgentToolGateway:
    """Expose the direct Reflection reads through the provider-neutral async port."""

    gateway: ReflectionToolGateway

    @property
    def server_name(self) -> str:
        return REFLECTION_TOOL_SERVER_NAME

    @property
    def available_tool_names(self) -> tuple[str, ...]:
        return REQUIRED_REFLECTION_TOOL_NAMES

    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> object:
        return self.gateway.call_tool(tool_name, dict(arguments))


def _validate_fixed_window(
    fixed_window: tuple[datetime, datetime] | None,
) -> tuple[datetime, datetime] | None:
    if fixed_window is None:
        return None
    if not isinstance(fixed_window, tuple) or len(fixed_window) != 2:
        raise RuntimeError("fixed_window must contain exactly start_at and end_at.")
    start_at = validate_timestamp(
        fixed_window[0], field_name="fixed_window.start_at", error_type=RuntimeError
    )
    end_at = validate_timestamp(
        fixed_window[1], field_name="fixed_window.end_at", error_type=RuntimeError
    )
    if end_at <= start_at:
        raise RuntimeError("fixed_window.end_at must be later than fixed_window.start_at.")
    return start_at, end_at


def _parse_timestamp_text(value: str, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{field_name} must be an ISO8601 datetime string.")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise RuntimeError(f"{field_name} must be a valid ISO8601 datetime.") from exc
    return validate_timestamp(parsed, field_name=field_name, error_type=RuntimeError)


def _resolve_optional_window(
    *,
    start_at: str | None,
    end_at: str | None,
) -> tuple[datetime, datetime] | None:
    if start_at is None and end_at is None:
        return None
    if start_at is None or end_at is None:
        raise RuntimeError("start_at and end_at must both be set when either is provided.")
    parsed_start = _parse_timestamp_text(start_at, field_name="start_at")
    parsed_end = _parse_timestamp_text(end_at, field_name="end_at")
    if parsed_end <= parsed_start:
        raise RuntimeError("end_at must be later than start_at.")
    return parsed_start, parsed_end


def _normalize_event_ids(
    event_ids: list[str] | None,
    *,
    field_name: str,
) -> list[str] | None:
    if event_ids is None:
        return None
    if not isinstance(event_ids, list):
        raise RuntimeError(f"{field_name} must be a list of event ids when provided.")
    return [validate_event_id(item, error_type=RuntimeError) for item in event_ids]


def _normalize_optional_status(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("status must be a string when provided.")
    normalized = value.strip()
    if not normalized:
        raise RuntimeError("status must not be blank when provided.")
    return normalized


def _validate_decision_episode_id(value: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError("episode_id must be a string.")
    normalized = value.strip()
    if not normalized.startswith("decision-episode:"):
        raise RuntimeError("episode_id must start with 'decision-episode:'.")
    return normalized


def _state_change_in_window(
    state_change: Any,
    *,
    start_at: datetime | None,
    end_at: datetime | None,
) -> bool:
    if start_at is not None and state_change.effective_at < start_at:
        return False
    if end_at is not None and state_change.effective_at >= end_at:
        return False
    return True


def _decision_episode_in_window(
    episode: Any,
    *,
    start_at: datetime | None,
    end_at: datetime | None,
) -> bool:
    if start_at is not None and episode.opened_at < start_at:
        return False
    if end_at is not None and episode.opened_at >= end_at:
        return False
    return True


def _serialize_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _serialize_mapping(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_mapping(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _serialize_evidence_record(record: Any) -> dict[str, Any]:
    payload = _serialize_mapping(asdict(record))
    content = payload.get("content")
    if not isinstance(content, str):
        raise RuntimeError("ledger evidence record content must be a string.")
    bounded = bounded_text_payload(content, limit=_EVIDENCE_EXCERPT_CHAR_LIMIT)
    payload["content"] = bounded["excerpt"]
    payload["content_sha256"] = bounded["sha256"]
    payload["content_char_count"] = bounded["char_count"]
    payload["content_truncated"] = bounded["truncated"]
    return payload


def _serialize_state_change(state_change: Any) -> dict[str, Any]:
    payload = _serialize_mapping(asdict(state_change))
    rationale_md = payload.get("rationale_md")
    if not isinstance(rationale_md, str):
        raise RuntimeError("state-change rationale_md must be a string.")
    bounded = bounded_text_payload(
        rationale_md,
        limit=_STATE_RATIONALE_EXCERPT_CHAR_LIMIT,
    )
    payload["rationale_md"] = bounded["excerpt"]
    payload["rationale_sha256"] = bounded["sha256"]
    payload["rationale_char_count"] = bounded["char_count"]
    payload["rationale_truncated"] = bounded["truncated"]
    return payload


def _serialize_decision_episode_projection_summary(episode: Any) -> dict[str, Any]:
    analysis = episode.analysis_record
    validation_state_change_ids = tuple(
        str(mark.payload.get("state_change_id"))
        for mark in episode.validation_marks
        if isinstance(mark.payload.get("state_change_id"), str)
    )
    context_packet_ids = tuple(
        record.context_packet_id
        for record in (episode.attention_record, analysis)
        if record is not None and record.context_packet_id is not None
    )
    return {
        "episode_id": episode.episode_id,
        "target_key": episode.target_key,
        "opened_at": episode.opened_at.isoformat(),
        "last_updated_at": episode.last_updated_at.isoformat(),
        "status": episode.status,
        "event_ids": list(episode.event_ids),
        "context_packet_ids": list(context_packet_ids),
        "analysis_outcome": None if analysis is None else analysis.status,
        "validation_state_change_ids": list(validation_state_change_ids),
        "record_counts": {
            "validation_marks": len(episode.validation_marks),
            "reflection_records": len(episode.reflection_records),
            "repair_records": len(episode.repair_records),
            "has_attention": episode.attention_record is not None,
            "has_analysis": episode.analysis_record is not None,
            "has_pm_decision": episode.pm_decision_record is not None,
        },
    }


def _serialize_decision_episode_record(record: Any) -> dict[str, Any]:
    payload_text = json.dumps(
        _serialize_mapping(dict(record.payload)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    bounded = bounded_text_payload(
        payload_text,
        limit=_DECISION_EPISODE_PAYLOAD_EXCERPT_CHAR_LIMIT,
    )
    return {
        "episode_id": record.episode_id,
        "target_key": record.target_key,
        "record_type": record.record_type,
        "business_at": record.business_at.isoformat(),
        "recorded_at": record.recorded_at.isoformat(),
        "event_ids": list(record.event_ids),
        "source_record_id": record.source_record_id,
        "source_path": record.source_path,
        "source_line": record.source_line,
        "context_packet_id": record.context_packet_id,
        "context_packet_hash": record.context_packet_hash,
        "status": record.status,
        "payload_excerpt": bounded["excerpt"],
        "payload_sha256": sha256(payload_text.encode("utf-8")).hexdigest(),
        "payload_char_count": len(payload_text),
        "payload_truncated": bounded["truncated"],
    }


def _serialize_page_read(page_path: str, content_md: str) -> dict[str, object]:
    bounded = bounded_text_payload(content_md, limit=_PAGE_EXCERPT_CHAR_LIMIT)
    return {
        "page_path": page_path,
        "content_md": bounded["excerpt"],
        "content_sha256": bounded["sha256"],
        "content_char_count": bounded["char_count"],
        "content_truncated": bounded["truncated"],
        "page_map": _serialize_page_map(content_md),
    }


def _serialize_section_read(
    *,
    page_path: str,
    content_md: str,
    section_name: str,
) -> dict[str, object]:
    try:
        section = find_markdown_section(
            content_md,
            heading=section_name.strip(),
            excerpt_char_limit=_PAGE_EXCERPT_CHAR_LIMIT,
        )
    except MarkdownContextError as exc:
        raise RuntimeError(f"Failed to read markdown section: {exc}") from exc
    if section is None:
        raise RuntimeError(f"Section {section_name.strip()!r} was not found in the requested page.")
    return {
        "page_path": page_path,
        "section_name": section["heading"],
        "heading_level": section["level"],
        "start_offset": section["start_offset"],
        "end_offset": section["end_offset"],
        "content_md": section["content_excerpt"],
        "content_sha256": section["content_sha256"],
        "content_char_count": section["content_char_count"],
        "content_truncated": section["content_truncated"],
    }


def _serialize_page_map(content_md: str) -> list[dict[str, object]]:
    try:
        sections = build_markdown_page_map(
            content_md,
            excerpt_char_limit=_PAGE_EXCERPT_CHAR_LIMIT,
        )
    except MarkdownContextError as exc:
        raise RuntimeError(f"Failed to build markdown page map: {exc}") from exc
    return [
        {
            "heading": section["heading"],
            "level": section["level"],
            "start_offset": section["start_offset"],
            "end_offset": section["end_offset"],
            "content_start_offset": section["content_start_offset"],
            "content_end_offset": section["content_end_offset"],
            "content_sha256": section["content_sha256"],
            "content_char_count": section["content_char_count"],
            "content_truncated": section["content_truncated"],
        }
        for section in sections
    ]


def _serialize_window(
    window: tuple[datetime, datetime] | None,
) -> dict[str, str] | None:
    if window is None:
        return None
    return {"start_at": window[0].isoformat(), "end_at": window[1].isoformat()}


def _paginate(
    items: list[Any],
    *,
    limit: int,
    offset: int,
) -> tuple[list[Any], dict[str, object]]:
    if not isinstance(limit, int):
        raise RuntimeError("limit must be an integer.")
    if limit < 1 or limit > MAX_REFLECTION_PAGE_SIZE:
        raise RuntimeError(f"limit must be between 1 and {MAX_REFLECTION_PAGE_SIZE}.")
    if not isinstance(offset, int) or offset < 0:
        raise RuntimeError("offset must be a non-negative integer.")

    total = len(items)
    paged_items = items[offset : offset + limit]
    return (
        paged_items,
        {
            "offset": offset,
            "limit": limit,
            "returned": len(paged_items),
            "total": total,
            "has_more": offset + len(paged_items) < total,
        },
    )


def _json_text(payload: Any) -> str:
    return json.dumps(_serialize_mapping(payload), ensure_ascii=False, indent=2)


__all__ = [
    "DEFAULT_REFLECTION_PAGE_SIZE",
    "MAX_REFLECTION_EVENT_IDS_PER_CALL",
    "MAX_REFLECTION_PAGE_SIZE",
    "REFLECTION_TOOL_SPECS",
    "ReflectionAgentToolGateway",
    "ReflectionToolGateway",
    "ReflectionToolSpec",
]
