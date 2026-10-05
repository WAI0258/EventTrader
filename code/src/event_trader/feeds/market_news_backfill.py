"""Deterministic historical backfill for structured market-news API articles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from event_trader.contracts._validators import (
    validate_labels,
    validate_target_key,
    validate_timestamp,
)
from event_trader.feeds.market_news import (
    MarketNewsArticle,
    MarketNewsPage,
    market_news_article_to_live_input,
    request_market_news_page,
)
from event_trader.source_archive.market_news import (
    MarketNewsArchiveWriteReceipt,
    write_live_market_news_record,
)
from event_trader.storage import WorkspaceLayout


class MarketNewsBackfillError(ValueError):
    """Raised when historical market-news backfill cannot proceed safely."""


type MarketNewsPageFetcher = Callable[..., MarketNewsPage]

_MAX_PAGES_PER_SLICE = 100


@dataclass(frozen=True, slots=True)
class MarketNewsBackfillSlice:
    """One deterministic market-news API backfill slice."""

    start_at: datetime
    end_at: datetime


@dataclass(frozen=True, slots=True)
class MarketNewsBackfillReceipt:
    """Observable receipt for one market-news backfill pass."""

    target_key: str
    window_start: datetime
    window_end: datetime
    slice_hours: int
    slices: tuple[MarketNewsBackfillSlice, ...]
    write_receipts: tuple[MarketNewsArchiveWriteReceipt, ...]
    rejected_article_count: int


def backfill_market_news(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    symbols: tuple[str, ...],
    labels: tuple[str, ...],
    base_url: str,
    api_key: str,
    window_start: datetime,
    window_end: datetime,
    slice_hours: int,
    limit: int,
    include_content: bool,
    exclude_contentless: bool,
    fetch_page: MarketNewsPageFetcher = request_market_news_page,
) -> MarketNewsBackfillReceipt:
    """Backfill one replay window of structured market-news material."""
    if not isinstance(layout, WorkspaceLayout):
        raise MarketNewsBackfillError("layout must be a WorkspaceLayout instance.")
    validated_target_key = validate_target_key(
        target_key,
        error_type=MarketNewsBackfillError,
    )
    validated_symbols = _validate_symbols(symbols)
    validated_labels = tuple(
        validate_labels(list(labels), error_type=MarketNewsBackfillError)
    )
    validated_window_start = validate_timestamp(
        window_start,
        field_name="window_start",
        error_type=MarketNewsBackfillError,
    ).astimezone(UTC)
    validated_window_end = validate_timestamp(
        window_end,
        field_name="window_end",
        error_type=MarketNewsBackfillError,
    ).astimezone(UTC)
    if validated_window_end < validated_window_start:
        raise MarketNewsBackfillError("window_end must be greater than or equal to window_start.")
    validated_slice_hours = _validate_positive_int(slice_hours, field_name="slice_hours")
    validated_limit = _validate_positive_int(limit, field_name="limit")
    if not callable(fetch_page):
        raise MarketNewsBackfillError("fetch_page must be callable.")

    slices = _build_backfill_slices(
        window_start=validated_window_start,
        window_end=validated_window_end,
        slice_hours=validated_slice_hours,
    )
    write_receipts: list[MarketNewsArchiveWriteReceipt] = []
    rejected_article_count = 0
    for slice_item in slices:
        articles = _fetch_slice_articles(
            base_url=base_url,
            api_key=api_key,
            symbols=validated_symbols,
            slice_item=slice_item,
            limit=validated_limit,
            include_content=include_content,
            exclude_contentless=exclude_contentless,
            fetch_page=fetch_page,
        )
        for article in articles:
            if article.created_at > slice_item.end_at:
                rejected_article_count += 1
                continue
            if article.updated_at is not None and article.updated_at > slice_item.end_at:
                rejected_article_count += 1
                continue
            ingress_input = market_news_article_to_live_input(
                article,
                captured_at=slice_item.end_at,
            )
            write_receipts.append(
                write_live_market_news_record(
                    layout,
                    target_key=validated_target_key,
                    article=article,
                    ingress_input=ingress_input,
                    labels=list(validated_labels),
                )
            )

    return MarketNewsBackfillReceipt(
        target_key=validated_target_key,
        window_start=validated_window_start,
        window_end=validated_window_end,
        slice_hours=validated_slice_hours,
        slices=slices,
        write_receipts=tuple(write_receipts),
        rejected_article_count=rejected_article_count,
    )


def _fetch_slice_articles(
    *,
    base_url: str,
    api_key: str,
    symbols: tuple[str, ...],
    slice_item: MarketNewsBackfillSlice,
    limit: int,
    include_content: bool,
    exclude_contentless: bool,
    fetch_page: MarketNewsPageFetcher,
) -> tuple[MarketNewsArticle, ...]:
    articles: list[MarketNewsArticle] = []
    seen_page_tokens: set[str] = set()
    page_token: str | None = None
    for _page_number in range(_MAX_PAGES_PER_SLICE):
        if page_token is not None:
            if page_token in seen_page_tokens:
                raise MarketNewsBackfillError("market-news pagination token repeated.")
            seen_page_tokens.add(page_token)
        page = fetch_page(
            base_url=base_url,
            api_key=api_key,
            symbols=symbols,
            start_at=slice_item.start_at,
            end_at=slice_item.end_at,
            limit=limit,
            include_content=include_content,
            exclude_contentless=exclude_contentless,
            page_token=page_token,
        )
        if not isinstance(page, MarketNewsPage):
            raise MarketNewsBackfillError("fetch_page must return a MarketNewsPage.")
        articles.extend(page.articles)
        page_token = page.next_page_token
        if page_token is None or not page.articles:
            break
    else:
        raise MarketNewsBackfillError("market-news backfill exceeded max page limit.")
    return tuple(articles)


def _build_backfill_slices(
    *,
    window_start: datetime,
    window_end: datetime,
    slice_hours: int,
) -> tuple[MarketNewsBackfillSlice, ...]:
    step = timedelta(hours=slice_hours)
    cursor = window_start.astimezone(UTC)
    ceiling = window_end.astimezone(UTC)
    slices: list[MarketNewsBackfillSlice] = []
    while cursor <= ceiling:
        slice_end = min(cursor + step - timedelta(seconds=1), ceiling)
        slices.append(MarketNewsBackfillSlice(start_at=cursor, end_at=slice_end))
        cursor = slice_end + timedelta(seconds=1)
    return tuple(slices)


def _validate_symbols(symbols: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(symbols, tuple) or not symbols:
        raise MarketNewsBackfillError("symbols must be a non-empty tuple.")
    normalized: list[str] = []
    for symbol in symbols:
        if not isinstance(symbol, str) or not symbol.strip():
            raise MarketNewsBackfillError("symbols must contain only non-empty strings.")
        normalized.append(symbol.strip().upper())
    return tuple(normalized)


def _validate_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketNewsBackfillError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise MarketNewsBackfillError(f"{field_name} must be greater than zero.")
    return value


__all__ = [
    "MarketNewsBackfillError",
    "MarketNewsBackfillReceipt",
    "MarketNewsBackfillSlice",
    "backfill_market_news",
]
