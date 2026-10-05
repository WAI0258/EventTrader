"""Helpers for normalizing MiroThinker boxed JSON payload text."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from json import JSONDecoder
from typing import Any


class BoxedJsonPayloadError(ValueError):
    """Raised when a boxed payload cannot be normalized into JSON."""


def load_first_boxed_json_object(
    *,
    final_boxed_answer: str,
    final_summary: str,
    fallback_payload_texts: Iterable[str] = (),
    payload_validator: Callable[[dict[str, Any]], bool] | None = None,
    object_start_fields: Iterable[str],
    missing_error: str,
    non_json_error: str,
    non_object_error: str,
) -> dict[str, Any]:
    """Load the first valid JSON object from ordered MiroThinker payload candidates."""
    payload_errors: list[str] = []
    for payload_text in _iter_boxed_json_payload_text_candidates(
        final_boxed_answer=final_boxed_answer,
        final_summary=final_summary,
        fallback_payload_texts=fallback_payload_texts,
    ):
        try:
            payload = load_boxed_json_object(
                payload_text,
                object_start_fields=object_start_fields,
                non_json_error=non_json_error,
                non_object_error=non_object_error,
            )
        except BoxedJsonPayloadError as exc:
            payload_errors.append(str(exc))
            continue
        if payload_validator is not None:
            try:
                if not payload_validator(payload):
                    payload_errors.append(
                        "MiroThinker boxed payload was rejected by validation."
                    )
                    continue
            except Exception as exc:
                payload_errors.append(str(exc))
                continue
        return payload
    if payload_errors:
        raise BoxedJsonPayloadError(payload_errors[0])
    raise BoxedJsonPayloadError(missing_error)


def select_boxed_json_payload_text(
    *,
    final_boxed_answer: str,
    final_summary: str,
    fallback_payload_texts: Iterable[str] = (),
    missing_error: str,
) -> str:
    """Select the real boxed payload from MiroThinker pipeline outputs."""
    for payload_text in _iter_boxed_json_payload_text_candidates(
        final_boxed_answer=final_boxed_answer,
        final_summary=final_summary,
        fallback_payload_texts=fallback_payload_texts,
    ):
        return payload_text
    raise BoxedJsonPayloadError(missing_error)


def _iter_boxed_json_payload_text_candidates(
    *,
    final_boxed_answer: str,
    final_summary: str,
    fallback_payload_texts: Iterable[str],
) -> tuple[str, ...]:
    candidates: list[str] = []
    for candidate in (final_boxed_answer, final_summary, *tuple(fallback_payload_texts)):
        normalized = candidate.strip()
        if not normalized or _is_vendor_no_boxed_sentinel(normalized):
            continue
        boxed_payload = _extract_embedded_boxed_payload(normalized)
        if boxed_payload is not None:
            candidates.append(boxed_payload)
            continue
        candidates.append(normalized)
    return tuple(candidates)


def load_boxed_json_object(
    payload_text: str,
    *,
    object_start_fields: Iterable[str],
    non_json_error: str,
    non_object_error: str,
) -> dict[str, Any]:
    """Load a boxed JSON object, accepting vendor output that dropped outer braces."""
    normalized = normalize_boxed_json_object_text(
        payload_text,
        object_start_fields=object_start_fields,
    )
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise BoxedJsonPayloadError(non_json_error) from exc
    if not isinstance(payload, dict):
        raise BoxedJsonPayloadError(non_object_error)
    return payload


def normalize_boxed_json_object_text(
    payload_text: str,
    *,
    object_start_fields: Iterable[str],
) -> str:
    """Return JSON text for a boxed object or object-member fragment."""
    normalized = _strip_boxed_markup(payload_text)
    normalized = _strip_json_object_suffix_if_only_boxed_markup(normalized)
    normalized = _normalize_latex_escaped_json_braces(normalized)
    if normalized.startswith("{"):
        return normalized
    field_prefixes = tuple(f'"{field}"' for field in object_start_fields)
    if normalized.startswith(field_prefixes):
        normalized = _strip_trailing_boxed_closers(normalized)
        return "{" + normalized + "}"
    return normalized


def _strip_boxed_markup(payload_text: str) -> str:
    normalized = payload_text.strip()
    if normalized.startswith("<boxed>"):
        close_index = normalized.find("</boxed>")
        if close_index != -1:
            return normalized[len("<boxed>") : close_index].strip()
        return normalized[len("<boxed>") :].strip()
    for prefix in ("\\boxed{", "\\\\boxed{"):
        if not normalized.startswith(prefix):
            continue
        boxed_payload = _extract_boxed_curly_payload(normalized, prefix=prefix)
        if boxed_payload is not None:
            return boxed_payload.strip()
        return normalized[len(prefix) :].strip()
    return normalized


def _is_vendor_no_boxed_sentinel(value: str) -> bool:
    return value.startswith("No \\boxed{} content found")


def _extract_embedded_boxed_payload(value: str) -> str | None:
    xml_start = value.find("<boxed>")
    if xml_start != -1:
        xml_end = value.find("</boxed>", xml_start + len("<boxed>"))
        if xml_end != -1:
            return value[xml_start + len("<boxed>") : xml_end].strip()

    for prefix in ("\\boxed{", "\\\\boxed{"):
        latex_start = value.find(prefix)
        if latex_start == -1:
            continue
        boxed_payload = _extract_boxed_curly_payload(value[latex_start:], prefix=prefix)
        if boxed_payload is not None:
            return boxed_payload.strip()
    return None


def _extract_boxed_curly_payload(value: str, *, prefix: str) -> str | None:
    start_index = len(prefix)
    depth = 1
    in_string = False
    escaped = False
    for index in range(start_index, len(value)):
        character = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            continue
        if character == "{":
            depth += 1
            continue
        if character == "}":
            depth -= 1
            if depth == 0:
                return value[start_index:index]
    return None


def _strip_json_object_suffix_if_only_boxed_markup(value: str) -> str:
    normalized = value.strip()
    if not normalized.startswith("{"):
        return normalized
    try:
        _, end_index = JSONDecoder().raw_decode(normalized)
    except json.JSONDecodeError:
        return normalized
    suffix = normalized[end_index:].strip()
    if not suffix:
        return normalized
    if _strip_trailing_boxed_closers(suffix) == "":
        return normalized[:end_index].strip()
    return normalized


def _strip_trailing_boxed_closers(value: str) -> str:
    normalized = value.strip()
    while normalized.endswith("</boxed>"):
        normalized = normalized[: -len("</boxed>")].strip()
    return normalized


def _normalize_latex_escaped_json_braces(value: str) -> str:
    normalized = value.strip()
    if not normalized.startswith("\\{"):
        return normalized
    return normalized.replace("\\{", "{").replace("\\}", "}")
