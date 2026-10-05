"""Market-news runtime driver for canonical live admission."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.feeds.market_news import (
    MarketNewsArticle,
    MarketNewsPage,
    market_news_article_to_live_input,
    request_market_news_page,
)
from event_trader.feeds.models import LiveNewsStreamInput
from event_trader.market_news_schedule import MarketNewsRestCadenceProfile
from event_trader.runtime.source_publisher import (
    RuntimeRawSourcePublisher,
    RuntimeRawSourcePublishReceipt,
)
from event_trader.source_archive.market_news import (
    MarketNewsArchiveRecord,
    append_market_news_record,
    map_live_market_news_to_archive_record,
    market_news_record_id,
)
from event_trader.source_release import release_current_market_news_record_ids
from event_trader.storage import WorkspaceLayout


class MarketNewsRuntimeError(ValueError):
    """Raised when the market-news runtime seam receives invalid state."""


MarketNewsPageFetcher = Callable[..., MarketNewsPage]

_MAX_PAGES_PER_BATCH = 100


@dataclass(frozen=True, slots=True)
class LiveMarketNewsBatchReceipt:
    """Observable result for one successful live market-news slice."""

    target_key: str
    window_start: datetime
    window_end: datetime
    material_count: int
    raw_source_receipts: tuple[RuntimeRawSourcePublishReceipt, ...]


def release_live_market_news_materials(
    *,
    target_key: str,
    symbols: tuple[str, ...],
    labels: tuple[str, ...],
    base_url: str,
    api_key: str,
    window_start: datetime,
    window_end: datetime,
    limit: int,
    include_content: bool,
    exclude_contentless: bool,
    raw_source_publisher: RuntimeRawSourcePublisher,
    ts_init: datetime,
    historical_layout: WorkspaceLayout,
    fetch_page: MarketNewsPageFetcher = request_market_news_page,
) -> LiveMarketNewsBatchReceipt:
    """Fetch, archive, and publish one market-news window as raw bus messages."""
    validated_target_key = validate_target_key(
        target_key,
        error_type=MarketNewsRuntimeError,
    )
    window_start_utc = _validate_utc_datetime(window_start, field_name="window_start")
    window_end_utc = _validate_utc_datetime(window_end, field_name="window_end")
    if window_end_utc <= window_start_utc:
        raise MarketNewsRuntimeError("window_end must be after window_start.")
    ts_init_utc = _validate_utc_datetime(ts_init, field_name="ts_init")
    if not symbols:
        raise MarketNewsRuntimeError("symbols must not be empty.")
    if not isinstance(raw_source_publisher, RuntimeRawSourcePublisher):
        raise MarketNewsRuntimeError(
            "raw_source_publisher must be a RuntimeRawSourcePublisher instance."
        )
    if not isinstance(historical_layout, WorkspaceLayout):
        raise MarketNewsRuntimeError("historical_layout must be a WorkspaceLayout instance.")
    if not callable(fetch_page):
        raise MarketNewsRuntimeError("fetch_page must be callable.")

    articles: list[MarketNewsArticle] = []
    seen_page_tokens: set[str] = set()
    page_token: str | None = None
    for _page_number in range(_MAX_PAGES_PER_BATCH):
        if page_token is not None:
            if page_token in seen_page_tokens:
                raise MarketNewsRuntimeError("market-news pagination token repeated.")
            seen_page_tokens.add(page_token)
        page = fetch_page(
            base_url=base_url,
            api_key=api_key,
            symbols=symbols,
            start_at=window_start_utc,
            end_at=window_end_utc,
            limit=limit,
            include_content=include_content,
            exclude_contentless=exclude_contentless,
            page_token=page_token,
        )
        if not isinstance(page, MarketNewsPage):
            raise MarketNewsRuntimeError("fetch_page must return a MarketNewsPage.")
        articles.extend(page.articles)
        page_token = page.next_page_token
        if page_token is None or not page.articles:
            break
    else:
        raise MarketNewsRuntimeError("market-news batch exceeded max page limit.")

    shaped_materials: list[tuple[MarketNewsArticle, LiveNewsStreamInput]] = []
    archived_records: list[MarketNewsArchiveRecord] = []
    for article in articles:
        ingress_input = market_news_article_to_live_input(
            article,
            captured_at=window_end_utc,
        )
        shaped_materials.append((article, ingress_input))
        archived_records.append(
            map_live_market_news_to_archive_record(
                target_key=validated_target_key,
                article=article,
                ingress_input=ingress_input,
                labels=list(labels),
            )
        )
    archived_record_tuple = tuple(archived_records)
    written_record_ids: set[str] = set()
    for record in archived_record_tuple:
        receipt = append_market_news_record(
            historical_layout,
            record=record,
        )
        if receipt.status == "written":
            written_record_ids.add(receipt.record_id)
    releaseable_record_ids = release_current_market_news_record_ids(
        layout=historical_layout,
        target_key=validated_target_key,
        current_records=archived_record_tuple,
    )
    raw_source_receipts: list[RuntimeRawSourcePublishReceipt] = []
    for (_article, ingress_input), record in zip(
        shaped_materials,
        archived_record_tuple,
        strict=True,
    ):
        record_id = market_news_record_id(record)
        if record_id not in releaseable_record_ids:
            continue
        if record_id not in written_record_ids:
            continue
        raw_source_receipts.append(
            raw_source_publisher.publish_news_raw(
                target_key=validated_target_key,
                source_ref=ingress_input.source_ref,
                record_id=record_id,
                event_time=record.visible_at,
                recorded_at=ts_init_utc,
            )
        )

    return LiveMarketNewsBatchReceipt(
        target_key=validated_target_key,
        window_start=window_start_utc,
        window_end=window_end_utc,
        material_count=len(raw_source_receipts),
        raw_source_receipts=tuple(raw_source_receipts),
    )


@dataclass(slots=True)
class MarketNewsBatchDriver:
    """Run at most one due market-news batch window per invocation."""

    target_key: str
    symbols: tuple[str, ...]
    labels: tuple[str, ...]
    base_url: str
    api_key: str
    cadence_profile: MarketNewsRestCadenceProfile
    last_successful_batch_end: datetime
    next_due_at: datetime
    limit: int
    include_content: bool
    exclude_contentless: bool
    soft_fail: bool
    raw_source_publisher: RuntimeRawSourcePublisher
    historical_layout: WorkspaceLayout
    fetch_page: MarketNewsPageFetcher = request_market_news_page
    emit: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        self.target_key = validate_target_key(
            self.target_key,
            error_type=MarketNewsRuntimeError,
        )
        self.symbols = _validate_symbols(self.symbols)
        self.labels = tuple(self.labels)
        self.last_successful_batch_end = _validate_utc_datetime(
            self.last_successful_batch_end,
            field_name="last_successful_batch_end",
        )
        self.next_due_at = _validate_utc_datetime(
            self.next_due_at,
            field_name="next_due_at",
        )
        self.limit = _validate_positive_int(self.limit, field_name="limit")
        if self.next_due_at <= self.last_successful_batch_end:
            raise MarketNewsRuntimeError(
                "next_due_at must be after last_successful_batch_end."
            )
        if not isinstance(self.cadence_profile, MarketNewsRestCadenceProfile):
            raise MarketNewsRuntimeError(
                "cadence_profile must be a MarketNewsRestCadenceProfile instance."
            )
        if not isinstance(self.soft_fail, bool):
            raise MarketNewsRuntimeError("soft_fail must be a bool.")
        if not isinstance(self.raw_source_publisher, RuntimeRawSourcePublisher):
            raise MarketNewsRuntimeError(
                "raw_source_publisher must be a RuntimeRawSourcePublisher instance."
            )
        if not isinstance(self.historical_layout, WorkspaceLayout):
            raise MarketNewsRuntimeError("historical_layout must be a WorkspaceLayout instance.")
        if not callable(self.fetch_page):
            raise MarketNewsRuntimeError("fetch_page must be callable.")

    def run_due(self, now: datetime) -> LiveMarketNewsBatchReceipt | None:
        """Run one due batch window ending at `now`, or return None."""
        validated_now = _validate_utc_datetime(now, field_name="now")
        if validated_now < self.next_due_at:
            return None

        window_start = self.last_successful_batch_end
        window_end = validated_now
        try:
            batch_receipt = release_live_market_news_materials(
                target_key=self.target_key,
                symbols=self.symbols,
                labels=self.labels,
                base_url=self.base_url,
                api_key=self.api_key,
                window_start=window_start,
                window_end=window_end,
                limit=self.limit,
                include_content=self.include_content,
                exclude_contentless=self.exclude_contentless,
                raw_source_publisher=self.raw_source_publisher,
                ts_init=window_end,
                historical_layout=self.historical_layout,
                fetch_page=self.fetch_page,
            )
        except Exception as exc:
            if not self.soft_fail:
                raise
            self.next_due_at = self.cadence_profile.next_due_after(validated_now)
            if self.emit is not None:
                self.emit(
                    "warning: market_news REST degraded: "
                    f"target_key={self.target_key} "
                    f"error={_exception_summary(exc)}"
                )
            return None
        self.last_successful_batch_end = window_end
        self.next_due_at = self.cadence_profile.next_due_after(window_end)
        return batch_receipt


def _validate_symbols(symbols: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(symbols, tuple) or not symbols:
        raise MarketNewsRuntimeError("symbols must be a non-empty tuple.")
    normalized: list[str] = []
    for symbol in symbols:
        if not isinstance(symbol, str) or not symbol.strip():
            raise MarketNewsRuntimeError("symbols must contain only non-empty strings.")
        normalized.append(symbol.strip().upper())
    return tuple(normalized)


def _validate_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketNewsRuntimeError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise MarketNewsRuntimeError(f"{field_name} must be greater than zero.")
    return value


def _validate_utc_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise MarketNewsRuntimeError(f"{field_name} must be a datetime.")
    return validate_timestamp(
        value,
        field_name=field_name,
        error_type=MarketNewsRuntimeError,
    ).astimezone(UTC)


def _exception_summary(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    normalized = " ".join(text.split())
    if len(normalized) <= 240:
        return normalized
    return normalized[:237] + "..."


__all__ = [
    "LiveMarketNewsBatchReceipt",
    "MarketNewsBatchDriver",
    "MarketNewsRuntimeError",
    "release_live_market_news_materials",
]
