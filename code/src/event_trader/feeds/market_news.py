"""Market-news API gathering boundary for symbol-tagged articles."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import cast
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from event_trader.contracts._validators import (
    validate_source_ref,
    validate_timestamp,
)
from event_trader.feeds.models import FeedModelError, LiveNewsStreamInput


class MarketNewsGatherError(ValueError):
    """Raised when market-news API material cannot cross the feed boundary."""


@dataclass(frozen=True, slots=True)
class MarketNewsArticle:
    """Canonical article shape returned by the market-news source."""

    article_id: str
    headline: str
    author: str
    created_at: datetime
    updated_at: datetime | None
    summary: str
    content_html: str
    content_text: str
    url: str | None
    images: tuple[Mapping[str, object], ...]
    symbols: tuple[str, ...]
    source: str
    raw_payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "article_id",
            _validate_non_blank_text(self.article_id, field_name="article_id"),
        )
        object.__setattr__(
            self,
            "headline",
            _validate_non_blank_text(self.headline, field_name="headline"),
        )
        object.__setattr__(
            self,
            "author",
            "" if self.author is None else str(self.author).strip(),
        )
        object.__setattr__(
            self,
            "created_at",
            validate_timestamp(
                self.created_at,
                field_name="created_at",
                error_type=MarketNewsGatherError,
            ),
        )
        if self.updated_at is not None:
            object.__setattr__(
                self,
                "updated_at",
                validate_timestamp(
                    self.updated_at,
                    field_name="updated_at",
                    error_type=MarketNewsGatherError,
                ),
            )
        object.__setattr__(self, "summary", _normalize_optional_text(self.summary))
        object.__setattr__(
            self,
            "content_html",
            _normalize_optional_text(self.content_html),
        )
        object.__setattr__(
            self,
            "content_text",
            _validate_non_blank_text(self.content_text, field_name="content_text"),
        )
        if self.url is not None:
            object.__setattr__(
                self,
                "url",
                validate_source_ref(self.url, error_type=MarketNewsGatherError),
            )
        object.__setattr__(
            self,
            "symbols",
            tuple(_validate_non_blank_text(symbol, field_name="symbol") for symbol in self.symbols),
        )
        object.__setattr__(self, "source", _normalize_optional_text(self.source))
        if not isinstance(self.raw_payload, Mapping):
            raise MarketNewsGatherError("raw_payload must be a JSON object.")
        object.__setattr__(
            self,
            "raw_payload",
            cast(Mapping[str, object], dict(self.raw_payload)),
        )

    @property
    def source_ref(self) -> str:
        """Return the stable source identity used by evidence admission."""
        if self.url is not None:
            return self.url
        return f"adapter://market-news/{self.article_id}"


@dataclass(frozen=True, slots=True)
class MarketNewsPage:
    """One decoded market-news response page."""

    articles: tuple[MarketNewsArticle, ...]
    next_page_token: str | None


def request_market_news_page(
    *,
    base_url: str,
    api_key: str,
    symbols: tuple[str, ...],
    start_at: datetime,
    end_at: datetime,
    limit: int,
    include_content: bool,
    exclude_contentless: bool,
    page_token: str | None = None,
    timeout_seconds: int = 30,
) -> MarketNewsPage:
    """Fetch and validate one page from the market-news API."""
    endpoint = f"{base_url.rstrip('/')}/market/news"
    params: dict[str, str] = {
        "symbols": ",".join(symbols),
        "start": validate_timestamp(
            start_at,
            field_name="start_at",
            error_type=MarketNewsGatherError,
        ).isoformat(),
        "end": validate_timestamp(
            end_at,
            field_name="end_at",
            error_type=MarketNewsGatherError,
        ).isoformat(),
        "limit": str(limit),
        "include_content": str(include_content).lower(),
        "exclude_contentless": str(exclude_contentless).lower(),
        "sort": "asc",
    }
    if page_token:
        params["page_token"] = page_token
    request = Request(
        f"{endpoint}?{urlencode(params)}",
        headers={"X-API-KEY": api_key},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except OSError as exc:
        raise MarketNewsGatherError(f"Failed to request market news: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MarketNewsGatherError("Market-news API response was not valid JSON.") from exc
    return shape_market_news_page(payload)


def shape_market_news_page(raw_payload: object) -> MarketNewsPage:
    """Validate one raw market-news API response."""
    if not isinstance(raw_payload, Mapping):
        raise MarketNewsGatherError("market-news response must be a JSON object.")
    raw_articles = raw_payload.get("news")
    if not isinstance(raw_articles, list):
        raise MarketNewsGatherError("market-news response must contain a news array.")
    next_page_token = raw_payload.get("next_page_token")
    if next_page_token is not None and not isinstance(next_page_token, str):
        raise MarketNewsGatherError("next_page_token must be a string or null.")
    return MarketNewsPage(
        articles=tuple(
            _article_from_payload(item, index=index)
            for index, item in enumerate(raw_articles)
        ),
        next_page_token=(
            (next_page_token.strip() or None)
            if isinstance(next_page_token, str)
            else None
        ),
    )


def market_news_article_to_live_input(
    article: MarketNewsArticle,
    *,
    captured_at: datetime,
) -> LiveNewsStreamInput:
    """Map one article into the existing live news-stream ingress shape."""
    if not isinstance(article, MarketNewsArticle):
        raise MarketNewsGatherError("article must be a MarketNewsArticle instance.")
    try:
        return LiveNewsStreamInput(
            source_ref=article.source_ref,
            headline=article.headline,
            body=article.content_text,
            published_at=article.created_at,
            captured_at=validate_timestamp(
                captured_at,
                field_name="captured_at",
                error_type=MarketNewsGatherError,
            ),
        )
    except FeedModelError as exc:
        raise MarketNewsGatherError(str(exc)) from exc


def _article_from_payload(raw_item: object, *, index: int) -> MarketNewsArticle:
    if not isinstance(raw_item, Mapping):
        raise MarketNewsGatherError(f"news[{index}] must be a JSON object.")
    article_id = str(_require_present(raw_item, "id", index=index))
    headline = _require_string(raw_item, "headline", index=index)
    content_html = _optional_string(raw_item.get("content"))
    summary = _optional_string(raw_item.get("summary"))
    content_text = _clean_html_to_text(content_html) if content_html else summary
    if not content_text.strip():
        content_text = headline
    return MarketNewsArticle(
        article_id=article_id,
        headline=headline,
        author=_optional_string(raw_item.get("author")),
        created_at=_parse_timestamp(
            raw_item.get("created_at"),
            field_name=f"news[{index}].created_at",
        ),
        updated_at=(
            None
            if raw_item.get("updated_at") is None
            else _parse_timestamp(
                raw_item.get("updated_at"),
                field_name=f"news[{index}].updated_at",
            )
        ),
        summary=summary,
        content_html=content_html,
        content_text=content_text,
        url=_optional_string(raw_item.get("url")) or None,
        images=_validate_images(raw_item.get("images"), index=index),
        symbols=_validate_symbols(raw_item.get("symbols"), index=index),
        source=_optional_string(raw_item.get("source")),
        raw_payload=cast(Mapping[str, object], dict(raw_item)),
    )


def _require_present(raw_item: Mapping[object, object], field_name: str, *, index: int) -> object:
    if field_name not in raw_item:
        raise MarketNewsGatherError(f"news[{index}].{field_name} is required.")
    return raw_item[field_name]


def _require_string(raw_item: Mapping[object, object], field_name: str, *, index: int) -> str:
    value = _require_present(raw_item, field_name, index=index)
    if not isinstance(value, str):
        raise MarketNewsGatherError(f"news[{index}].{field_name} must be a string.")
    return value


def _optional_string(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    return value


def _parse_timestamp(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise MarketNewsGatherError(f"{field_name} must be an ISO8601 string.")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketNewsGatherError(f"{field_name} must be an ISO8601 timestamp.") from exc


def _validate_images(value: object, *, index: int) -> tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MarketNewsGatherError(f"news[{index}].images must be an array when present.")
    images: list[Mapping[str, object]] = []
    for image in value:
        if not isinstance(image, Mapping):
            raise MarketNewsGatherError(f"news[{index}].images must contain only objects.")
        images.append(cast(Mapping[str, object], dict(image)))
    return tuple(images)


def _validate_symbols(value: object, *, index: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MarketNewsGatherError(f"news[{index}].symbols must be an array when present.")
    symbols: list[str] = []
    for symbol in value:
        if not isinstance(symbol, str) or not symbol.strip():
            raise MarketNewsGatherError(
                f"news[{index}].symbols must contain only non-empty strings."
            )
        symbols.append(symbol.strip())
    return tuple(symbols)


def _validate_non_blank_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MarketNewsGatherError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MarketNewsGatherError(f"{field_name} must not be blank.")
    return normalized


def _normalize_optional_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _clean_html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return " ".join(parser.text.split())


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    @property
    def text(self) -> str:
        return " ".join(self._parts)

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._parts.append(data.strip())


__all__ = [
    "MarketNewsArticle",
    "MarketNewsGatherError",
    "MarketNewsPage",
    "market_news_article_to_live_input",
    "request_market_news_page",
    "shape_market_news_page",
]
