"""Project-owned MCP server for one-by-one live web-search candidate appends."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from event_trader.contracts._validators import validate_timestamp
from event_trader.feeds.web_search import (
    LiveWebSearchGatherError,
    shape_live_web_search_material,
)

LIVE_WEB_SEARCH_RECEIPT_PATH_ENV = "EVENT_TRADER_LIVE_WEB_SEARCH_RECEIPT_PATH"
LIVE_WEB_SEARCH_TARGET_KEY_ENV = "EVENT_TRADER_LIVE_WEB_SEARCH_TARGET_KEY"
LIVE_WEB_SEARCH_QUERY_ENV = "EVENT_TRADER_LIVE_WEB_SEARCH_QUERY"
LIVE_WEB_SEARCH_DISCOVERED_AT_ENV = "EVENT_TRADER_LIVE_WEB_SEARCH_DISCOVERED_AT"


def _load_required_text_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required for live web-search MCP server.")
    return value.strip()


def _load_required_timestamp_env(name: str) -> datetime:
    raw_value = _load_required_text_env(name)
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a valid ISO8601 datetime.") from exc
    return validate_timestamp(parsed, field_name=name, error_type=RuntimeError)


_target_key = _load_required_text_env(LIVE_WEB_SEARCH_TARGET_KEY_ENV)
_query = _load_required_text_env(LIVE_WEB_SEARCH_QUERY_ENV)
_discovered_at = _load_required_timestamp_env(LIVE_WEB_SEARCH_DISCOVERED_AT_ENV)
_receipt_path = (
    Path(_load_required_text_env(LIVE_WEB_SEARCH_RECEIPT_PATH_ENV))
    .expanduser()
    .resolve(strict=False)
)
_seen_source_refs: set[str] = set()
mcp = FastMCP("event_trader_live_web_search")


@mcp.tool()
def append_live_web_search_candidate(
    source_ref: str,
    title: str,
    content: str,
    published_at: str | None,
    labels: list[str],
) -> str:
    """Validate and collect one live web-search candidate immediately."""
    _append_receipt_entry(
        {
            "status": "append_attempt",
            "source_ref": source_ref,
            "title_blank": _is_blank_text(title),
            "content_blank": _is_blank_text(content),
            "labels_count": len(labels) if isinstance(labels, list) else None,
            "recorded_at": _now_iso(),
        }
    )
    try:
        normalized_published_at = _parse_optional_timestamp_text(
            published_at,
            field_name="published_at",
        )
        ingress_input, normalized_labels = shape_live_web_search_material(
            target_key=_target_key,
            query=_query,
            discovered_at=_discovered_at,
            raw_payload={
                "query": _query,
                "source_ref": source_ref,
                "title": title,
                "content": content,
                "discovered_at": _discovered_at,
                "published_at": normalized_published_at,
                "labels": labels,
            },
        )
        rejection_reason = _placeholder_rejection_reason(
            source_ref=ingress_input.source_ref,
            title=ingress_input.title,
            content=ingress_input.content,
        )
        if rejection_reason is not None:
            _append_rejection_receipt(
                source_ref=ingress_input.source_ref,
                rejection_reason=rejection_reason,
            )
            return _json_text(
                {
                    "status": "rejected",
                    "source_ref": ingress_input.source_ref,
                    "rejection_reason": rejection_reason,
                    "rejection_code": _derive_rejection_code(rejection_reason),
                }
            )
        if ingress_input.source_ref in _seen_source_refs:
            _append_receipt_entry(
                {
                    "status": "noop_duplicate_source_ref",
                    "source_ref": ingress_input.source_ref,
                    "recorded_at": _now_iso(),
                }
            )
            return _json_text(
                {
                    "status": "noop_duplicate_source_ref",
                    "source_ref": ingress_input.source_ref,
                }
            )
    except (RuntimeError, LiveWebSearchGatherError) as exc:
        _append_rejection_receipt(
            source_ref=source_ref,
            rejection_reason=str(exc),
        )
        return _json_text(
            {
                "status": "rejected",
                "source_ref": source_ref,
                "rejection_reason": str(exc),
                "rejection_code": _derive_rejection_code(str(exc)),
            }
        )

    _seen_source_refs.add(ingress_input.source_ref)
    _append_receipt_entry(
        {
            "status": "accepted",
            "source_ref": ingress_input.source_ref,
            "title": ingress_input.title,
            "content": ingress_input.content,
            "published_at": (
                None
                if ingress_input.published_at is None
                else ingress_input.published_at.isoformat()
            ),
            "labels": list(normalized_labels),
            "recorded_at": _now_iso(),
        }
    )
    return _json_text(
        {
            "status": "accepted",
            "source_ref": ingress_input.source_ref,
        }
    )


def _parse_optional_timestamp_text(
    value: str | None,
    *,
    field_name: str,
) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"{field_name} must be an ISO8601 string or null.")
    normalized = value.strip()
    if not normalized:
        raise RuntimeError(f"{field_name} must be an ISO8601 string or null.")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        if _looks_like_calendar_date(normalized):
            raise RuntimeError(
                f"{field_name} must be a timezone-aware ISO8601 timestamp or null; "
                "date-only values are not allowed."
            ) from exc
        raise RuntimeError(f"{field_name} must be a valid ISO8601 string.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if _looks_like_calendar_date(normalized):
            raise RuntimeError(
                f"{field_name} must be a timezone-aware ISO8601 timestamp or null; "
                "date-only values are not allowed."
            )
        raise RuntimeError(f"{field_name} must be timezone-aware.")
    return validate_timestamp(parsed, field_name=field_name, error_type=RuntimeError)


def _placeholder_rejection_reason(
    *,
    source_ref: str,
    title: str,
    content: str,
) -> str | None:
    normalized_source_ref = source_ref.strip().lower().rstrip("/")
    if normalized_source_ref in {
        "search://google/unavailable",
        "search://no-results-found",
        "no-sources-available",
    }:
        return "source_ref must identify a real source, not a placeholder marker."
    if not normalized_source_ref.startswith(("http://", "https://")):
        return "source_ref must be an http/https source URL for live web-search."
    normalized_title = " ".join(title.strip().lower().split())
    normalized_content = " ".join(content.strip().lower().split())
    placeholder_texts = (
        "no sources available",
        "no source available",
        "no results found",
        "no search results found",
        "no matching sources found",
        "unable to retrieve any valid sources",
        "all search attempts returned errors",
        "future window relative to current date",
    )
    if any(text in normalized_title for text in placeholder_texts) or any(
        text in normalized_content for text in placeholder_texts
    ):
        return "candidate title/content must not be a no-source placeholder."
    return None


def _append_rejection_receipt(
    *,
    source_ref: str,
    rejection_reason: str,
) -> None:
    _append_receipt_entry(
        {
            "status": "rejected",
            "source_ref": source_ref,
            "rejection_reason": rejection_reason,
            "rejection_code": _derive_rejection_code(rejection_reason),
            "recorded_at": _now_iso(),
        }
    )


def _derive_rejection_code(reason: str) -> str:
    normalized = reason.strip().lower()
    if "date-only values are not allowed" in normalized:
        return "published_at_date_only"
    if "must be timezone-aware" in normalized:
        return "published_at_not_timezone_aware"
    if "must be a valid iso8601 string" in normalized:
        return "published_at_invalid_iso8601"
    if "published_at must not be later than discovered_at" in normalized:
        return "published_at_later_than_discovered_at"
    if "source_ref must not be blank" in normalized:
        return "blank_source_ref"
    if "title must not be blank" in normalized:
        return "blank_title"
    if "content must not be blank" in normalized:
        return "blank_content"
    if "must identify a real source" in normalized:
        return "placeholder_source_ref"
    if "must be an http/https source url" in normalized:
        return "non_web_source_ref"
    if "must not be a no-source placeholder" in normalized:
        return "placeholder_content"
    if "operator-only metadata labels" in normalized:
        return "operator_only_metadata_labels"
    if "labels must use only" in normalized:
        return "invalid_label_prefix"
    return "candidate_rejected"


def _is_blank_text(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _looks_like_calendar_date(value: str) -> bool:
    return len(value) == 10 and value.count("-") == 2


def _append_receipt_entry(payload: dict[str, Any]) -> None:
    _receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with _receipt_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False))
        handle.write("\n")


def _json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    mcp.run(transport="stdio")
