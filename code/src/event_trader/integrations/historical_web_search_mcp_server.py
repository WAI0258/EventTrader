"""Project-owned MCP server for one-by-one historical web-search candidate appends."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]

from event_trader.contracts._validators import validate_timestamp
from event_trader.feeds.web_search_backfill import (
    archive_historical_web_search_candidate,
)
from event_trader.integrations.web_source_published_at import resolve_web_source_published_at
from event_trader.source_archive.web_search import (
    HistoricalWebSearchWriteReceipt,
    WebSearchAcquisitionProvenance,
)
from event_trader.storage import build_workspace_layout, validate_workspace_layout

WORKSPACE_ROOT_ENV = "EVENT_TRADER_WORKSPACE_ROOT"
HISTORICAL_WEB_SEARCH_RECEIPT_PATH_ENV = "EVENT_TRADER_HISTORICAL_WEB_SEARCH_RECEIPT_PATH"
HISTORICAL_WEB_SEARCH_TARGET_KEY_ENV = "EVENT_TRADER_HISTORICAL_WEB_SEARCH_TARGET_KEY"
HISTORICAL_WEB_SEARCH_QUERY_ENV = "EVENT_TRADER_HISTORICAL_WEB_SEARCH_QUERY"
HISTORICAL_WEB_SEARCH_DISCOVERED_AT_ENV = "EVENT_TRADER_HISTORICAL_WEB_SEARCH_DISCOVERED_AT"
HISTORICAL_WEB_SEARCH_ACQUISITION_PROVENANCE_ENV = (
    "EVENT_TRADER_HISTORICAL_WEB_SEARCH_ACQUISITION_PROVENANCE"
)


def _load_required_text_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required for historical web-search MCP server.")
    return value.strip()


def _load_required_timestamp_env(name: str) -> datetime:
    raw_value = _load_required_text_env(name)
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a valid ISO8601 datetime.") from exc
    return validate_timestamp(parsed, field_name=name, error_type=RuntimeError)


def _load_workspace_root() -> Path:
    return Path(_load_required_text_env(WORKSPACE_ROOT_ENV)).expanduser().resolve(strict=False)


def _load_optional_acquisition_provenance() -> WebSearchAcquisitionProvenance | None:
    raw_value = os.environ.get(HISTORICAL_WEB_SEARCH_ACQUISITION_PROVENANCE_ENV, "").strip()
    if not raw_value:
        return None
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{HISTORICAL_WEB_SEARCH_ACQUISITION_PROVENANCE_ENV} must be valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"{HISTORICAL_WEB_SEARCH_ACQUISITION_PROVENANCE_ENV} must decode to a JSON object."
        )
    retrieval_languages = payload.get("retrieval_languages")
    if not isinstance(retrieval_languages, list):
        raise RuntimeError(
            f"{HISTORICAL_WEB_SEARCH_ACQUISITION_PROVENANCE_ENV}.retrieval_languages "
            "must be a JSON array of strings."
        )
    return WebSearchAcquisitionProvenance(
        prompt_profile_id=str(payload.get("prompt_profile_id", "")),
        search_intent_hash=str(payload.get("search_intent_hash", "")),
        control_language=str(payload.get("control_language", "")),
        retrieval_languages=tuple(str(item) for item in retrieval_languages),
    )


_layout = build_workspace_layout(_load_workspace_root())
validate_workspace_layout(_layout)
_target_key = _load_required_text_env(HISTORICAL_WEB_SEARCH_TARGET_KEY_ENV)
_query = _load_required_text_env(HISTORICAL_WEB_SEARCH_QUERY_ENV)
_discovered_at = _load_required_timestamp_env(HISTORICAL_WEB_SEARCH_DISCOVERED_AT_ENV)
_acquisition_provenance = _load_optional_acquisition_provenance()
_receipt_path = (
    Path(_load_required_text_env(HISTORICAL_WEB_SEARCH_RECEIPT_PATH_ENV))
    .expanduser()
    .resolve(strict=False)
)
_seen_source_refs: set[str] = set()
mcp = FastMCP("event_trader_historical_web_search")


class _HistoricalAppendTerminalReceiptWriteError(RuntimeError):
    """Raised when a terminal historical append receipt cannot be persisted."""


@mcp.tool()
def append_historical_web_search_candidate(
    source_ref: str,
    title: str,
    content: str,
    published_at: str | None,
    labels: list[str],
) -> str:
    """Validate and append one historical web-search candidate immediately."""
    _append_receipt_entry(
        {
            "status": "append_attempt",
            "source_ref": source_ref,
            "title_blank": _is_blank_text(title),
            "content_blank": _is_blank_text(content),
            "labels_count": len(labels) if isinstance(labels, list) else None,
            "recorded_at": datetime.now().isoformat(),
        }
    )
    resolved_source_ref = source_ref
    if source_ref in _seen_source_refs:
        _append_terminal_receipt_entry(
            {
                "status": "noop_duplicate_source_ref",
                "source_ref": source_ref,
                "recorded_at": datetime.now().isoformat(),
            }
        )
        return _json_text(
            {
                "status": "noop_duplicate_source_ref",
                "source_ref": source_ref,
            }
        )
    try:
        normalized_published_at = _parse_optional_timestamp_text(
            published_at,
            field_name="published_at",
        )
        raw_payload: dict[str, object] = {
            "query": _query,
            "source_ref": source_ref,
            "title": title,
            "content": content,
            "discovered_at": _discovered_at,
            "published_at": normalized_published_at,
            "labels": labels,
        }
        write_receipt, rejection_reason, resolved_source_ref = (
            archive_historical_web_search_candidate(
                layout=_layout,
                target_key=_target_key,
                query=_query,
                discovered_at=_discovered_at,
                raw_candidate=raw_payload,
                resolve_published_at=resolve_web_source_published_at,
                acquisition_provenance=_acquisition_provenance,
            )
        )
    except _HistoricalAppendTerminalReceiptWriteError:
        raise
    except Exception as exc:
        _append_failed_receipt(
            source_ref=resolved_source_ref or source_ref,
            exc=exc,
        )
        raise RuntimeError(str(exc)) from exc
    if rejection_reason is not None:
        _append_terminal_receipt_entry(
            {
                "status": "rejected",
                "source_ref": resolved_source_ref or source_ref,
                "rejection_reason": rejection_reason,
                "rejection_code": _derive_rejection_code(rejection_reason),
                "recorded_at": datetime.now().isoformat(),
            }
        )
        return _json_text(
            {
                "status": "rejected",
                "source_ref": resolved_source_ref or source_ref,
                "rejection_reason": rejection_reason,
                "rejection_code": _derive_rejection_code(rejection_reason),
            }
        )
    if write_receipt is None:
        raise RuntimeError(
            "archive_historical_web_search_candidate must return a receipt when "
            "no rejection reason is present."
        )
    _seen_source_refs.add(source_ref)
    _append_written_receipt(
        write_receipt=write_receipt,
        source_ref=resolved_source_ref or source_ref,
    )
    return _json_text(
        {
            "status": write_receipt.status,
            "record_id": write_receipt.record_id,
            "source_ref": source_ref,
        }
    )


def _append_written_receipt(
    *,
    write_receipt: HistoricalWebSearchWriteReceipt,
    source_ref: str,
) -> None:
    _append_terminal_receipt_entry(
        {
            "status": write_receipt.status,
            "source_ref": source_ref,
            "record_id": write_receipt.record_id,
            "partition_path": str(write_receipt.partition_path.resolve(strict=False)),
            "recorded_at": datetime.now().isoformat(),
        }
    )


def _append_failed_receipt(*, source_ref: str, exc: Exception) -> None:
    _append_terminal_receipt_entry(
        {
            "status": "failed",
            "source_ref": source_ref,
            "error_code": _derive_failure_code(str(exc)),
            "error_message": str(exc),
            "recorded_at": datetime.now().isoformat(),
        }
    )


def _append_terminal_receipt_entry(payload: dict[str, Any]) -> None:
    try:
        _append_receipt_entry(payload)
    except Exception as exc:
        raise _HistoricalAppendTerminalReceiptWriteError(
            "historical web-search terminal receipt writing failed."
        ) from exc


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
    return validate_timestamp(parsed, field_name=field_name, error_type=RuntimeError)


def _looks_like_calendar_date(value: str) -> bool:
    return len(value) == 10 and value.count("-") == 2


def _derive_rejection_code(reason: str) -> str:
    normalized = reason.strip().lower()
    if "date-only values are not allowed" in normalized:
        return "published_at_date_only"
    if "must be timezone-aware" in normalized:
        return "published_at_not_timezone_aware"
    if "must be a valid iso8601 string" in normalized:
        return "published_at_invalid_iso8601"
    if "source_ref must not be blank" in normalized:
        return "blank_source_ref"
    if "title must not be blank" in normalized:
        return "blank_title"
    if "content must not be blank" in normalized:
        return "blank_content"
    if "operator-only metadata labels" in normalized:
        return "operator_only_metadata_labels"
    if "labels must use only the 'event:' or 'topic:' prefixes." in normalized:
        return "invalid_label_prefix"
    return "candidate_rejected"


def _derive_failure_code(reason: str) -> str:
    normalized = reason.strip().lower()
    if "date-only values are not allowed" in normalized:
        return "published_at_date_only"
    if "must be timezone-aware" in normalized:
        return "published_at_not_timezone_aware"
    if "must be a valid iso8601 string" in normalized:
        return "published_at_invalid_iso8601"
    return "append_tool_internal_error"


def _is_blank_text(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _append_receipt_entry(payload: dict[str, Any]) -> None:
    _receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with _receipt_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False))
        handle.write("\n")


def _json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
