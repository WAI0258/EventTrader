"""Thin project-owned source-shape adapters for feed ingress boundaries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import cast

from event_trader.contracts._validators import validate_labels, validate_target_key

from .models import (
    FeedModelError,
    HistoricalMacroApiInput,
    HistoricalManualDatasetInput,
    HistoricalMarketNewsInput,
    HistoricalNewsStreamInput,
    HistoricalWebSearchInput,
    LiveIngressInput,
    LiveMacroApiInput,
    LiveNewsStreamInput,
    LiveSourceShape,
    LiveWebSearchInput,
    ReplayIngressInput,
    ReplaySourceShape,
)


class FeedAdapterError(ValueError):
    """Raised when a source-shape adapter payload does not match the boundary."""


type LiveSourceAdapter = Callable[[Mapping[str, object]], LiveIngressInput]
type ReplaySourceAdapter = Callable[[Mapping[str, object]], ReplayIngressInput]

_NEWS_STREAM_FIELDS = (
    "source_ref",
    "headline",
    "body",
    "published_at",
    "captured_at",
)
_WEB_SEARCH_FIELDS = (
    "query",
    "source_ref",
    "title",
    "content",
    "discovered_at",
    "published_at",
)
_MACRO_API_FIELDS = (
    "source_ref",
    "release_key",
    "period_label",
    "value_text",
    "unit",
    "released_at",
)
_HISTORICAL_NEWS_STREAM_FIELDS = (*_NEWS_STREAM_FIELDS, "visible_at")
_HISTORICAL_MARKET_NEWS_FIELDS = (
    "source_ref",
    "headline",
    "body",
    "published_at",
    "updated_at",
    "captured_at",
    "visible_at",
)
_HISTORICAL_MACRO_API_FIELDS = (*_MACRO_API_FIELDS, "visible_at")
_HISTORICAL_MANUAL_DATASET_FIELDS = (
    "source_ref",
    "title",
    "content",
    "provided_at",
    "visible_at",
)
_HISTORICAL_WEB_SEARCH_FIELDS = (
    *_WEB_SEARCH_FIELDS,
    "visible_at",
)
_HISTORICAL_WEB_SEARCH_RECORD_FIELDS = (
    "target_key",
    *_HISTORICAL_WEB_SEARCH_FIELDS,
    "labels",
)


def adapt_news_stream(payload: Mapping[str, object]) -> LiveNewsStreamInput:
    """Adapt one canonical news-stream payload into the live feed boundary."""
    data = _payload_fields(
        payload,
        source_shape="news_stream",
        required_fields=_NEWS_STREAM_FIELDS,
    )
    return LiveNewsStreamInput(
        source_ref=cast(str, data["source_ref"]),
        headline=cast(str, data["headline"]),
        body=cast(str, data["body"]),
        published_at=cast(datetime, data["published_at"]),
        captured_at=cast(datetime, data["captured_at"]),
    )



def adapt_web_search(payload: Mapping[str, object]) -> LiveWebSearchInput:
    """Adapt one canonical web-search payload into the live feed boundary."""
    data = _payload_fields(
        payload,
        source_shape="web_search",
        required_fields=_WEB_SEARCH_FIELDS,
    )
    return LiveWebSearchInput(
        query=cast(str, data["query"]),
        source_ref=cast(str, data["source_ref"]),
        title=cast(str, data["title"]),
        content=cast(str, data["content"]),
        discovered_at=cast(datetime, data["discovered_at"]),
        published_at=cast(datetime | None, data["published_at"]),
    )



def adapt_macro_api(payload: Mapping[str, object]) -> LiveMacroApiInput:
    """Adapt one canonical macro-api payload into the live feed boundary."""
    data = _payload_fields(
        payload,
        source_shape="macro_api",
        required_fields=_MACRO_API_FIELDS,
    )
    return LiveMacroApiInput(
        source_ref=cast(str, data["source_ref"]),
        release_key=cast(str, data["release_key"]),
        period_label=cast(str, data["period_label"]),
        value_text=cast(str, data["value_text"]),
        unit=cast(str | None, data["unit"]),
        released_at=cast(datetime, data["released_at"]),
    )



def adapt_historical_news_stream(
    payload: Mapping[str, object],
) -> HistoricalNewsStreamInput:
    """Adapt one canonical historical news-stream payload into replay ingress."""
    data = _payload_fields(
        payload,
        source_shape="historical_news_stream",
        required_fields=_HISTORICAL_NEWS_STREAM_FIELDS,
    )
    return HistoricalNewsStreamInput(
        source_ref=cast(str, data["source_ref"]),
        headline=cast(str, data["headline"]),
        body=cast(str, data["body"]),
        published_at=cast(datetime, data["published_at"]),
        captured_at=cast(datetime, data["captured_at"]),
        visible_at=cast(datetime, data["visible_at"]),
    )


def adapt_historical_market_news(
    payload: Mapping[str, object],
) -> HistoricalMarketNewsInput:
    """Adapt one canonical historical market-news payload into replay ingress."""
    data = _payload_fields(
        payload,
        source_shape="historical_market_news",
        required_fields=_HISTORICAL_MARKET_NEWS_FIELDS,
    )
    return HistoricalMarketNewsInput(
        source_ref=cast(str, data["source_ref"]),
        headline=cast(str, data["headline"]),
        body=cast(str, data["body"]),
        published_at=cast(datetime, data["published_at"]),
        updated_at=cast(datetime | None, data["updated_at"]),
        captured_at=cast(datetime, data["captured_at"]),
        visible_at=cast(datetime, data["visible_at"]),
    )



def adapt_historical_macro_api(
    payload: Mapping[str, object],
) -> HistoricalMacroApiInput:
    """Adapt one canonical historical macro-api payload into replay ingress."""
    data = _payload_fields(
        payload,
        source_shape="historical_macro_api",
        required_fields=_HISTORICAL_MACRO_API_FIELDS,
    )
    return HistoricalMacroApiInput(
        source_ref=cast(str, data["source_ref"]),
        release_key=cast(str, data["release_key"]),
        period_label=cast(str, data["period_label"]),
        value_text=cast(str, data["value_text"]),
        unit=cast(str | None, data["unit"]),
        released_at=cast(datetime, data["released_at"]),
        visible_at=cast(datetime, data["visible_at"]),
    )



def adapt_historical_manual_dataset(
    payload: Mapping[str, object],
) -> HistoricalManualDatasetInput:
    """Adapt one canonical historical manual dataset payload into replay ingress."""
    data = _payload_fields(
        payload,
        source_shape="historical_manual_dataset",
        required_fields=_HISTORICAL_MANUAL_DATASET_FIELDS,
    )
    return HistoricalManualDatasetInput(
        source_ref=cast(str, data["source_ref"]),
        title=cast(str, data["title"]),
        content=cast(str, data["content"]),
        provided_at=cast(datetime, data["provided_at"]),
        visible_at=cast(datetime, data["visible_at"]),
    )


def adapt_historical_web_search(
    payload: Mapping[str, object],
) -> HistoricalWebSearchInput:
    """Adapt one canonical historical web-search payload into replay ingress."""
    data = _payload_fields(
        payload,
        source_shape="historical_web_search",
        required_fields=_HISTORICAL_WEB_SEARCH_FIELDS,
    )
    return HistoricalWebSearchInput(
        query=cast(str, data["query"]),
        source_ref=cast(str, data["source_ref"]),
        title=cast(str, data["title"]),
        content=cast(str, data["content"]),
        published_at=cast(datetime | None, data["published_at"]),
        discovered_at=cast(datetime, data["discovered_at"]),
        visible_at=cast(datetime, data["visible_at"]),
    )


def split_historical_web_search_record(
    payload: Mapping[str, object],
) -> tuple[str, HistoricalWebSearchInput, list[str]]:
    """Split one historical web-search library record into replay inputs."""
    data = _payload_fields(
        payload,
        source_shape="historical_web_search_record",
        required_fields=_HISTORICAL_WEB_SEARCH_RECORD_FIELDS,
    )
    return (
        validate_target_key(cast(str, data["target_key"]), error_type=FeedAdapterError),
        adapt_historical_web_search(
            {
                field_name: data[field_name]
                for field_name in _HISTORICAL_WEB_SEARCH_FIELDS
            }
        ),
        validate_labels(cast(list[str], data["labels"]), error_type=FeedAdapterError),
    )


_LIVE_SOURCE_ADAPTERS: dict[LiveSourceShape, LiveSourceAdapter] = {
    "news_stream": adapt_news_stream,
    "web_search": adapt_web_search,
    "macro_api": adapt_macro_api,
}
_REPLAY_SOURCE_ADAPTERS: dict[ReplaySourceShape, ReplaySourceAdapter] = {
    "historical_news_stream": adapt_historical_news_stream,
    "historical_market_news": adapt_historical_market_news,
    "historical_macro_api": adapt_historical_macro_api,
    "historical_manual_dataset": adapt_historical_manual_dataset,
    "historical_web_search": adapt_historical_web_search,
}



def live_adapter_for(source_shape: LiveSourceShape) -> LiveSourceAdapter:
    """Return the live adapter committed for the requested source shape."""
    try:
        return _LIVE_SOURCE_ADAPTERS[source_shape]
    except KeyError as exc:
        available = ", ".join(_LIVE_SOURCE_ADAPTERS)
        raise FeedAdapterError(
            "No live adapter is committed for "
            f"'{source_shape}'. Available live adapters: {available}."
        ) from exc



def replay_adapter_for(source_shape: ReplaySourceShape) -> ReplaySourceAdapter:
    """Return the replay adapter committed for the requested source shape."""
    try:
        return _REPLAY_SOURCE_ADAPTERS[source_shape]
    except KeyError as exc:
        available = ", ".join(_REPLAY_SOURCE_ADAPTERS)
        raise FeedAdapterError(
            "No replay adapter is committed for "
            f"'{source_shape}'. Available replay adapters: {available}."
        ) from exc



def _payload_fields(
    payload: Mapping[str, object],
    *,
    source_shape: str,
    required_fields: tuple[str, ...],
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise FeedAdapterError(
            f"{source_shape} adapter payload must be a mapping of canonical fields."
        )

    missing = tuple(field for field in required_fields if field not in payload)
    unexpected = tuple(str(field) for field in payload if field not in required_fields)
    if missing or unexpected:
        problems: list[str] = []
        if missing:
            problems.append(f"missing required field(s): {', '.join(missing)}")
        if unexpected:
            problems.append(f"unexpected field(s): {', '.join(unexpected)}")
        raise FeedAdapterError(
            f"{source_shape} adapter payload does not match the committed boundary; "
            f"{'; '.join(problems)}."
        )

    return {field: payload[field] for field in required_fields}


__all__ = [
    "FeedAdapterError",
    "FeedModelError",
    "LiveSourceAdapter",
    "ReplaySourceAdapter",
    "adapt_historical_macro_api",
    "adapt_historical_market_news",
    "adapt_historical_manual_dataset",
    "adapt_historical_news_stream",
    "adapt_historical_web_search",
    "adapt_macro_api",
    "adapt_news_stream",
    "adapt_web_search",
    "live_adapter_for",
    "replay_adapter_for",
    "split_historical_web_search_record",
]
