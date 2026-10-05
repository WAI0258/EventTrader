"""Benzinga realtime news WebSocket feed boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from event_trader.feeds.market_news import MarketNewsArticle


class BenzingaNewsWebSocketError(ValueError):
    """Raised when a Benzinga news-stream message cannot be adapted safely."""


type BenzingaNewsAction = Literal["created", "updated", "deleted"]


@dataclass(frozen=True, slots=True)
class BenzingaNewsWebSocketEvent:
    """One decoded Benzinga news-stream event."""

    action: BenzingaNewsAction
    article: MarketNewsArticle | None
    raw_payload: Mapping[str, object]


def parse_benzinga_news_ws_message(
    message: str,
) -> tuple[BenzingaNewsWebSocketEvent, ...]:
    """Parse one Benzinga news WebSocket text frame into canonical events."""
    try:
        payload = json.loads(message)
    except json.JSONDecodeError as exc:
        raise BenzingaNewsWebSocketError("Benzinga WS message was not valid JSON.") from exc
    items = payload if isinstance(payload, list) else [payload]
    events: list[BenzingaNewsWebSocketEvent] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise BenzingaNewsWebSocketError(
                f"Benzinga WS message item {index} must be a JSON object."
            )
        events.append(_event_from_payload(cast(Mapping[str, object], dict(item))))
    return tuple(events)


def _event_from_payload(
    payload: Mapping[str, object],
) -> BenzingaNewsWebSocketEvent:
    data = payload.get("data")
    action_value = payload.get("action")
    if action_value is None and isinstance(data, Mapping):
        action_value = data.get("action")
    action = _normalize_action(action_value)
    content = _extract_content(payload)
    if action == "deleted":
        return BenzingaNewsWebSocketEvent(
            action=action,
            article=None,
            raw_payload=payload,
        )
    article = _article_from_content(action=action, content=content, raw_payload=payload)
    return BenzingaNewsWebSocketEvent(
        action=action,
        article=article,
        raw_payload=payload,
    )


def _normalize_action(value: object) -> BenzingaNewsAction:
    action = "created" if value is None else str(value).strip().lower()
    if action in {"create", "created", "new"}:
        return "created"
    if action in {"update", "updated"}:
        return "updated"
    if action in {"delete", "deleted"}:
        return "deleted"
    raise BenzingaNewsWebSocketError(f"Unsupported Benzinga WS action: {action!r}.")


def _extract_content(payload: Mapping[str, object]) -> Mapping[str, object]:
    data = payload.get("data")
    if isinstance(data, Mapping):
        nested_content = data.get("content")
        if isinstance(nested_content, Mapping):
            return _merge_wrapper_fields(
                content=cast(Mapping[str, object], dict(nested_content)),
                wrapper=cast(Mapping[str, object], data),
            )
    for field_name in ("content", "data", "news"):
        value = payload.get(field_name)
        if isinstance(value, Mapping):
            return cast(Mapping[str, object], dict(value))
    return payload


def _merge_wrapper_fields(
    *,
    content: Mapping[str, object],
    wrapper: Mapping[str, object],
) -> Mapping[str, object]:
    merged = dict(content)
    for field_name in (
        "id",
        "news_id",
        "story_id",
        "created",
        "created_at",
        "published",
        "published_at",
        "updated",
        "updated_at",
        "last_updated",
        "stocks",
        "symbols",
        "tickers",
        "url",
        "link",
        "source",
        "author",
    ):
        if field_name not in merged and field_name in wrapper:
            merged[field_name] = wrapper[field_name]
    return merged


def _article_from_content(
    *,
    action: BenzingaNewsAction,
    content: Mapping[str, object],
    raw_payload: Mapping[str, object],
) -> MarketNewsArticle:
    article_id = _first_text(content, "id", "news_id", "story_id")
    headline = _first_text(content, "headline", "title")
    content_html = _first_text(content, "body", "content", "html", default="")
    summary = _first_text(content, "teaser", "summary", "abstract", default="")
    content_text = _strip_html(content_html) if content_html else summary
    if not content_text:
        content_text = headline
    created_at = _parse_timestamp(
        _first_present(content, "created_at", "created", "published_at", "published"),
        field_name="created_at",
    )
    updated_value = _first_present(
        content,
        "updated_at",
        "updated",
        "last_updated",
        default=None,
    )
    updated_at = (
        None
        if updated_value is None
        else _parse_timestamp(updated_value, field_name="updated_at")
    )
    return MarketNewsArticle(
        article_id=article_id,
        headline=headline,
        author=_first_text(content, "author", "source", default=""),
        created_at=created_at,
        updated_at=updated_at,
        summary=summary,
        content_html=content_html,
        content_text=content_text,
        url=_first_text(content, "url", "link", default="") or None,
        images=_extract_images(content.get("images")),
        symbols=_extract_symbols(content),
        source=_first_text(content, "source", default="benzinga") or "benzinga",
        raw_payload={
            "provider": "benzinga_news_v1",
            "action": action,
            "payload": dict(raw_payload),
        },
    )


def _first_present(
    payload: Mapping[str, object],
    *field_names: str,
    default: object = ...,
) -> object:
    for field_name in field_names:
        value = payload.get(field_name)
        if value is not None:
            return value
    if default is ...:
        names = ", ".join(field_names)
        raise BenzingaNewsWebSocketError(f"Benzinga WS article requires one of: {names}.")
    return default


def _first_text(
    payload: Mapping[str, object],
    *field_names: str,
    default: str | None = None,
) -> str:
    value = _first_present(payload, *field_names, default=default)
    if value is None:
        return ""
    text = str(value).strip()
    if not text and default is None:
        names = ", ".join(field_names)
        raise BenzingaNewsWebSocketError(f"Benzinga WS article field is blank: {names}.")
    return text


def _parse_timestamp(value: object, *, field_name: str) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if not isinstance(value, str) or not value.strip():
        raise BenzingaNewsWebSocketError(f"Benzinga WS field {field_name!r} must be a timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BenzingaNewsWebSocketError(
            f"Benzinga WS field {field_name!r} must be an ISO8601 timestamp."
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _extract_symbols(content: Mapping[str, object]) -> tuple[str, ...]:
    candidates = content.get("stocks")
    if candidates is None:
        candidates = content.get("symbols")
    if candidates is None:
        candidates = content.get("tickers")
    if isinstance(candidates, str):
        return tuple(
            symbol.strip().upper()
            for symbol in candidates.split(",")
            if symbol.strip()
        )
    if not isinstance(candidates, list):
        return ()
    symbols: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            symbol = item.strip().upper()
        elif isinstance(item, Mapping):
            symbol = _symbol_from_mapping(item)
        else:
            continue
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return tuple(symbols)


def _symbol_from_mapping(item: Mapping[object, object]) -> str:
    for field_name in ("symbol", "ticker", "name"):
        value = item.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return ""


def _extract_images(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        cast(Mapping[str, object], dict(item))
        for item in value
        if isinstance(item, Mapping)
    )


def _strip_html(value: str) -> str:
    in_tag = False
    parts: list[str] = []
    for char in value:
        if char == "<":
            in_tag = True
            parts.append(" ")
            continue
        if char == ">":
            in_tag = False
            continue
        if not in_tag:
            parts.append(char)
    return " ".join("".join(parts).split())


__all__ = [
    "BenzingaNewsWebSocketError",
    "BenzingaNewsWebSocketEvent",
    "parse_benzinga_news_ws_message",
]
