"""Shared validation helpers for project-owned contract modules."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlparse

_SEGMENT_DISALLOWED_VALUES = frozenset({".", ".."})
_FORBIDDEN_SOURCE_REF_SCHEMES = frozenset({"browser", "jina", "mcp"})
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_ALLOWED_LABEL_PREFIXES = (
    "event:",
    "topic:",
    "event_type:",
    "source_kind:",
    "operator_confidence:",
    "operator_source_" + "basis:",
)


def validate_target_key(value: str, *, error_type: type[Exception]) -> str:
    """Validate a target-scoped identity segment used in paths and topics."""
    return _validate_path_segment(value, field_name="target_key", error_type=error_type)


def validate_page_key(value: str, *, field_name: str, error_type: type[Exception]) -> str:
    """Validate a dynamic page identity segment used under target directories."""
    return _validate_path_segment(value, field_name=field_name, error_type=error_type)


def validate_scope_key(value: str, *, error_type: type[Exception]) -> str:
    """Validate the documented shared or target scope identifier."""
    if not isinstance(value, str):
        raise error_type("scope_key must be a string.")

    normalized = value.strip()
    if not normalized:
        raise error_type("scope_key must not be blank.")
    if normalized != value:
        raise error_type("scope_key must not include leading or trailing whitespace.")
    if normalized == "shared":
        return normalized
    if not normalized.startswith("target:"):
        raise error_type("scope_key must be 'shared' or 'target:<target_key>'.")

    target_key = normalized.partition(":")[2]
    validate_target_key(target_key, error_type=error_type)
    return normalized


def validate_event_id(value: str, *, error_type: type[Exception]) -> str:
    """Validate the persisted evidence event identifier shape."""
    if not isinstance(value, str) or not _EVENT_ID_RE.fullmatch(value):
        raise error_type(
            "event_id must be a 24-character lowercase hex identifier derived from "
            "admitted evidence identity."
        )
    return value


def validate_source_ref(value: str, *, error_type: type[Exception]) -> str:
    """Validate the canonical source identity without retrieval-method leakage."""
    if not isinstance(value, str):
        raise error_type("source_ref must be a string.")

    normalized = value.strip()
    if not normalized:
        raise error_type("source_ref must not be blank.")

    parsed = urlparse(normalized)
    scheme = parsed.scheme.lower()
    if not scheme:
        raise error_type(
            "source_ref must use a canonical source scheme such as https:// or "
            "adapter://resource/id."
        )
    if scheme in _FORBIDDEN_SOURCE_REF_SCHEMES:
        raise error_type(
            "source_ref must name the source, not the retrieval method "
            f"('{scheme}://')."
        )
    if scheme in {"http", "https"}:
        if not parsed.netloc:
            raise error_type("source_ref must include a host for web sources.")
        return normalized

    remainder = normalized.partition("://")[2]
    if not remainder or remainder.startswith("/") or "/" not in remainder:
        raise error_type(
            "source_ref must follow <adapter>://<resource>/<stable-id> for non-web "
            "sources."
        )

    return normalized


def validate_labels(labels: list[str], *, error_type: type[Exception]) -> list[str]:
    """Validate evidence labels against the allowed namespace prefixes."""
    normalized_labels: list[str] = []
    for label in labels:
        if not isinstance(label, str):
            raise error_type("labels must contain only strings.")

        normalized = label.strip()
        if not normalized:
            raise error_type("labels must not contain blank values.")
        if any(ch.isspace() for ch in normalized):
            raise error_type(
                "labels must not contain whitespace; use namespace-prefixed slugs only."
            )
        if not normalized.startswith(_ALLOWED_LABEL_PREFIXES):
            raise error_type(
                "labels must use only the 'event:', 'topic:', 'event_type:', "
                "'source_kind:', 'operator_confidence:', or the "
                "'operator_source_basis' namespace prefix."
                "prefixes."
            )

        prefix, _, suffix = normalized.partition(":")
        if not suffix:
            raise error_type(
                f"labels with the '{prefix}:' prefix must include a non-empty value."
            )

        normalized_labels.append(normalized)

    return normalized_labels


def validate_timestamp(
    value: datetime,
    *,
    field_name: str,
    error_type: type[Exception],
) -> datetime:
    """Validate an aware datetime used in evidence identity or storage."""
    if not isinstance(value, datetime):
        raise error_type(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise error_type(f"{field_name} must be timezone-aware.")
    return value


def normalize_content(
    content: str,
    *,
    field_name: str,
    error_type: type[Exception],
) -> str:
    """Normalize content text for stable hashing and durable storage."""
    if not isinstance(content, str):
        raise error_type(f"{field_name} must be a string.")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise error_type(f"{field_name} must not be blank.")
    return normalized


def format_identity_timestamp(value: datetime) -> str:
    """Format a timestamp in a stable UTC identity representation."""
    return value.astimezone(UTC).isoformat()


def _validate_path_segment(
    value: str,
    *,
    field_name: str,
    error_type: type[Exception],
) -> str:
    if not isinstance(value, str):
        raise error_type(f"{field_name} must be a string.")

    normalized = value.strip()
    if not normalized:
        raise error_type(f"{field_name} must not be blank.")
    if normalized != value:
        raise error_type(f"{field_name} must not include leading or trailing whitespace.")
    if normalized in _SEGMENT_DISALLOWED_VALUES:
        raise error_type(f"{field_name} must not be '.' or '..'.")
    if "/" in normalized or "\\" in normalized:
        raise error_type(f"{field_name} must not include path separators.")
    if any(ch.isspace() for ch in normalized):
        raise error_type(f"{field_name} must not include whitespace.")

    return normalized
