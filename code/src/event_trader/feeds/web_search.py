"""Project-owned live web-search gathering boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import cast

from event_trader.contracts._validators import (
    validate_labels,
    validate_target_key,
    validate_timestamp,
)
from event_trader.feeds.models import FeedModelError, LiveWebSearchInput
from event_trader.source_policy import (
    OPERATOR_CONFIDENCE_LABEL_PREFIX,
    OPERATOR_SOURCE_BASIS_LABEL_PREFIX,
    SourcePolicyError,
    extract_event_type,
    extract_source_kind,
)

type SearchAgentRunner = Callable[..., Mapping[str, object]]

_REQUIRED_AGENT_FIELDS = ("query", "discovered_at", "items")
_REQUIRED_ITEM_FIELDS = ("source_ref", "title", "content", "labels")
_OPTIONAL_ITEM_FIELDS = ("published_at",)
_REQUIRED_SINGLE_ITEM_FIELDS = ("query", "discovered_at", *_REQUIRED_ITEM_FIELDS)
_OPTIONAL_SINGLE_ITEM_FIELDS = _OPTIONAL_ITEM_FIELDS
_RETIRED_OPERATOR_BASIS_LABEL_PREFIX = OPERATOR_SOURCE_BASIS_LABEL_PREFIX.removeprefix(
    "operator_"
)
_FORBIDDEN_OPERATOR_ONLY_LABEL_PREFIXES = (
    OPERATOR_CONFIDENCE_LABEL_PREFIX,
    OPERATOR_SOURCE_BASIS_LABEL_PREFIX,
    _RETIRED_OPERATOR_BASIS_LABEL_PREFIX,
)


class LiveWebSearchGatherError(ValueError):
    """Raised when a live web-search gather pass fails to match the boundary."""


def gather_live_web_search_materials(
    *,
    target_key: str,
    query: str,
    discovered_at: datetime,
    run_search_agent: SearchAgentRunner,
) -> tuple[tuple[LiveWebSearchInput, list[str]], ...]:
    """Run one search-agent pass and return all admission-ready source materials."""
    validated_target_key = validate_target_key(
        target_key,
        error_type=LiveWebSearchGatherError,
    )
    validated_query = _validate_non_blank_text(query, field_name="query")
    validated_discovered_at = validate_timestamp(
        discovered_at,
        field_name="discovered_at",
        error_type=LiveWebSearchGatherError,
    )
    if not callable(run_search_agent):
        raise LiveWebSearchGatherError("run_search_agent must be callable.")

    raw_payload = run_search_agent(
        target_key=validated_target_key,
        query=validated_query,
        discovered_at=validated_discovered_at,
    )
    return shape_live_web_search_materials(
        target_key=validated_target_key,
        query=validated_query,
        discovered_at=validated_discovered_at,
        raw_payload=raw_payload,
    )


def shape_live_web_search_materials(
    *,
    target_key: str,
    query: str,
    discovered_at: datetime,
    raw_payload: Mapping[str, object],
) -> tuple[tuple[LiveWebSearchInput, list[str]], ...]:
    """Validate one raw agent payload against the canonical live web-search boundary."""
    validate_target_key(
        target_key,
        error_type=LiveWebSearchGatherError,
    )
    validated_query = _validate_non_blank_text(query, field_name="query")
    validated_discovered_at = validate_timestamp(
        discovered_at,
        field_name="discovered_at",
        error_type=LiveWebSearchGatherError,
    )
    payload = _validate_agent_payload(raw_payload)
    payload_query = _validate_non_blank_text(
        cast(str, payload["query"]),
        field_name="query",
    )
    if payload_query != validated_query:
        raise LiveWebSearchGatherError("run_search_agent must return the exact requested query.")
    payload_discovered_at = validate_timestamp(
        cast(datetime, payload["discovered_at"]),
        field_name="discovered_at",
        error_type=LiveWebSearchGatherError,
    )
    if payload_discovered_at != validated_discovered_at:
        raise LiveWebSearchGatherError(
            "run_search_agent must return the exact requested discovered_at."
        )

    materials: list[tuple[LiveWebSearchInput, list[str]]] = []
    for index, item in enumerate(_validate_items_payload(payload["items"])):
        item_payload = _validate_item_payload(item, index=index)
        labels = validate_labels(
            _validate_labels_payload(item_payload["labels"], field_name=f"items[{index}].labels"),
            error_type=LiveWebSearchGatherError,
        )
        _validate_web_search_classification_labels(
            labels,
            field_name=f"items[{index}].labels",
        )
        _reject_operator_only_labels(
            labels,
            field_name=f"items[{index}].labels",
        )
        published_at = _validate_optional_published_at(
            item_payload.get("published_at"),
            field_name=f"items[{index}].published_at",
        )
        if published_at is not None and published_at > payload_discovered_at:
            raise LiveWebSearchGatherError(
                f"items[{index}].published_at must not be later than discovered_at."
            )
        try:
            ingress_input = LiveWebSearchInput(
                query=payload_query,
                source_ref=cast(str, item_payload["source_ref"]),
                title=cast(str, item_payload["title"]),
                content=cast(str, item_payload["content"]),
                discovered_at=payload_discovered_at,
                published_at=published_at,
            )
        except FeedModelError as exc:
            raise LiveWebSearchGatherError(str(exc)) from exc
        materials.append((ingress_input, labels))
    return _validate_unique_source_refs(tuple(materials))


def shape_live_web_search_material(
    *,
    target_key: str,
    query: str,
    discovered_at: datetime,
    raw_payload: Mapping[str, object],
    require_exact_discovered_at: bool = True,
) -> tuple[LiveWebSearchInput, list[str]]:
    """Validate one source item for historical one-by-one append tooling."""
    if not require_exact_discovered_at:
        raise LiveWebSearchGatherError(
            "single web-search material validation requires exact discovered_at."
        )
    single_payload = _validate_single_item_payload(raw_payload)
    payload_query = _validate_non_blank_text(
        cast(str, single_payload["query"]),
        field_name="query",
    )
    validated_query = _validate_non_blank_text(query, field_name="query")
    if payload_query != validated_query:
        raise LiveWebSearchGatherError("run_search_agent must return the exact requested query.")
    payload_discovered_at = validate_timestamp(
        cast(datetime, single_payload["discovered_at"]),
        field_name="discovered_at",
        error_type=LiveWebSearchGatherError,
    )
    validated_discovered_at = validate_timestamp(
        discovered_at,
        field_name="discovered_at",
        error_type=LiveWebSearchGatherError,
    )
    if payload_discovered_at != validated_discovered_at:
        raise LiveWebSearchGatherError(
            "run_search_agent must return the exact requested discovered_at."
        )
    materials = shape_live_web_search_materials(
        target_key=target_key,
        query=payload_query,
        discovered_at=payload_discovered_at,
        raw_payload={
            "query": payload_query,
            "discovered_at": payload_discovered_at,
            "items": [
                {
                    field_name: single_payload[field_name]
                    for field_name in (*_REQUIRED_ITEM_FIELDS, *_OPTIONAL_ITEM_FIELDS)
                    if field_name in single_payload
                }
            ],
        },
    )
    if len(materials) != 1:
        raise LiveWebSearchGatherError(
            "single web-search material validation must produce exactly one item."
        )
    return materials[0]


def _validate_agent_payload(raw_payload: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(raw_payload, Mapping):
        raise LiveWebSearchGatherError(
            "run_search_agent must return a mapping of canonical web-search fields."
        )

    allowed_fields = set(_REQUIRED_AGENT_FIELDS)
    missing_fields = tuple(
        field_name for field_name in _REQUIRED_AGENT_FIELDS if field_name not in raw_payload
    )
    unexpected_fields = tuple(
        str(field_name) for field_name in raw_payload if field_name not in allowed_fields
    )
    if missing_fields or unexpected_fields:
        problems: list[str] = []
        if missing_fields:
            problems.append(f"missing required field(s): {', '.join(missing_fields)}")
        if unexpected_fields:
            problems.append(f"unexpected field(s): {', '.join(unexpected_fields)}")
        raise LiveWebSearchGatherError(
            "run_search_agent did not match the committed web-search boundary; "
            f"{'; '.join(problems)}."
        )

    return {field_name: raw_payload[field_name] for field_name in _REQUIRED_AGENT_FIELDS}


def _validate_single_item_payload(raw_payload: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(raw_payload, Mapping):
        raise LiveWebSearchGatherError(
            "run_search_agent must return a mapping of canonical web-search fields."
        )

    allowed_fields = set(_REQUIRED_SINGLE_ITEM_FIELDS) | set(_OPTIONAL_SINGLE_ITEM_FIELDS)
    missing_fields = tuple(
        field_name for field_name in _REQUIRED_SINGLE_ITEM_FIELDS if field_name not in raw_payload
    )
    unexpected_fields = tuple(
        str(field_name) for field_name in raw_payload if field_name not in allowed_fields
    )
    if missing_fields or unexpected_fields:
        problems: list[str] = []
        if missing_fields:
            problems.append(f"missing required field(s): {', '.join(missing_fields)}")
        if unexpected_fields:
            problems.append(f"unexpected field(s): {', '.join(unexpected_fields)}")
        raise LiveWebSearchGatherError(
            "run_search_agent did not match the committed web-search boundary; "
            f"{'; '.join(problems)}."
        )

    return {field_name: raw_payload[field_name] for field_name in _REQUIRED_SINGLE_ITEM_FIELDS} | {
        "published_at": raw_payload.get("published_at")
    }


def _validate_items_payload(value: object) -> Sequence[object]:
    if not isinstance(value, list):
        raise LiveWebSearchGatherError("items must be provided as a list.")
    return value


def _validate_item_payload(raw_item: object, *, index: int) -> dict[str, object]:
    if not isinstance(raw_item, Mapping):
        raise LiveWebSearchGatherError(f"items[{index}] must be a mapping.")

    allowed_fields = set(_REQUIRED_ITEM_FIELDS) | set(_OPTIONAL_ITEM_FIELDS)
    missing_fields = tuple(
        field_name for field_name in _REQUIRED_ITEM_FIELDS if field_name not in raw_item
    )
    unexpected_fields = tuple(
        str(field_name) for field_name in raw_item if field_name not in allowed_fields
    )
    if missing_fields or unexpected_fields:
        problems: list[str] = []
        if missing_fields:
            problems.append(f"missing required field(s): {', '.join(missing_fields)}")
        if unexpected_fields:
            problems.append(f"unexpected field(s): {', '.join(unexpected_fields)}")
        raise LiveWebSearchGatherError(
            "run_search_agent item did not match the committed web-search boundary; "
            f"items[{index}]: {'; '.join(problems)}."
        )

    return {field_name: raw_item[field_name] for field_name in _REQUIRED_ITEM_FIELDS} | {
        "published_at": raw_item.get("published_at")
    }


def _validate_labels_payload(value: object, *, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise LiveWebSearchGatherError(f"{field_name} must be provided as a list[str].")
    return cast(list[str], value)


def _validate_optional_published_at(value: object, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    return validate_timestamp(
        cast(datetime, value),
        field_name=field_name,
        error_type=LiveWebSearchGatherError,
    )


def _validate_unique_source_refs(
    materials: tuple[tuple[LiveWebSearchInput, list[str]], ...],
) -> tuple[tuple[LiveWebSearchInput, list[str]], ...]:
    seen_source_refs: dict[str, int] = {}
    for index, (ingress_input, _) in enumerate(materials):
        first_index = seen_source_refs.get(ingress_input.source_ref)
        if first_index is not None:
            raise LiveWebSearchGatherError(
                "run_search_agent returned duplicate source_ref values in one "
                f"batch; items[{index}].source_ref duplicates "
                f"items[{first_index}].source_ref: {ingress_input.source_ref}"
            )
        seen_source_refs[ingress_input.source_ref] = index
    return materials


def _validate_web_search_classification_labels(
    labels: list[str],
    *,
    field_name: str,
) -> None:
    try:
        extract_event_type(labels)
        source_kind = extract_source_kind(labels)
    except SourcePolicyError as exc:
        raise LiveWebSearchGatherError(f"{field_name} {exc}") from exc
    if source_kind == "operator_brief":
        raise LiveWebSearchGatherError(
            f"{field_name} must not contain source_kind:operator_brief for external web-search."
        )


def _reject_operator_only_labels(
    labels: list[str],
    *,
    field_name: str,
) -> None:
    if any(label.startswith(_FORBIDDEN_OPERATOR_ONLY_LABEL_PREFIXES) for label in labels):
        raise LiveWebSearchGatherError(
            f"{field_name} must not contain operator-only metadata labels."
        )


def _validate_non_blank_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise LiveWebSearchGatherError(f"{field_name} must be a string.")

    normalized = value.strip()
    if not normalized:
        raise LiveWebSearchGatherError(f"{field_name} must not be blank.")
    return normalized


__all__ = [
    "LiveWebSearchGatherError",
    "SearchAgentRunner",
    "gather_live_web_search_materials",
    "shape_live_web_search_material",
    "shape_live_web_search_materials",
]
