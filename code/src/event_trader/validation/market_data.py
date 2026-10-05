"""HTTP-backed market-data port for deterministic validation bars."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from event_trader.config import ValidationMarketDataConfig
from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    MarketMapping,
    ViewStateChangeContractError,
)

class ValidationMarketDataError(ValueError):
    """Raised when validation market-data fetching or decoding is invalid."""


class ApiStocksMarketDataPort:
    """Single deterministic market-data port for stock bar validation."""

    def __init__(self, config: ValidationMarketDataConfig) -> None:
        if not isinstance(config, ValidationMarketDataConfig):
            raise ValidationMarketDataError(
                "config must be a ValidationMarketDataConfig instance."
            )
        self._provider = config.provider
        self._base_url = config.base_url
        self._api_key = config.api_key
        self._timeout_seconds = config.timeout_seconds

    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        if not isinstance(mapping, MarketMapping):
            raise ValidationMarketDataError("mapping must be a MarketMapping instance.")

        normalized_start = _validate_utc_datetime(start_at, field_name="start_at")
        normalized_end = _validate_utc_datetime(end_at, field_name="end_at")
        if normalized_start >= normalized_end:
            raise ValidationMarketDataError(
                "invalid window: start_at must be earlier than end_at."
            )
        try:
            from event_trader.market.stock_bar_adapter import validate_stock_bar_capability

            validate_stock_bar_capability(provider=self._provider, mapping=mapping)
        except ValueError as exc:
            raise ValidationMarketDataError(str(exc)) from exc

        try:
            from event_trader.market.stock_bar_adapter import parse_stock_bar_granularity

            granularity_delta = parse_stock_bar_granularity(mapping.bar_granularity)
        except ValueError as exc:
            raise ValidationMarketDataError(str(exc)) from exc
        bars: list[MarketDataBar] = []
        previous_bar_start: datetime | None = None
        seen_page_tokens: set[str] = set()
        page_token: str | None = None

        while True:
            payload = self._fetch_bars_page(
                symbol=mapping.market_symbol,
                bar_granularity=mapping.bar_granularity,
                start_at=normalized_start,
                end_at=normalized_end,
                page_token=page_token,
            )
            try:
                from event_trader.market.stock_bar_adapter import extract_stock_symbol_bars

                raw_bars = extract_stock_symbol_bars(payload, symbol=mapping.market_symbol)
            except ValueError as exc:
                raise ValidationMarketDataError(str(exc)) from exc
            if not raw_bars:
                raise ValidationMarketDataError(
                    f"empty bars for symbol {mapping.market_symbol!r}."
                )

            for index, raw_bar in enumerate(raw_bars):
                try:
                    from event_trader.market.stock_bar_adapter import parse_stock_bar_row

                    bar = parse_stock_bar_row(
                        raw_bar,
                        bar_granularity=granularity_delta,
                        symbol=mapping.market_symbol,
                        item_index=index,
                    )
                except ValueError as exc:
                    raise ValidationMarketDataError(str(exc)) from exc
                if bar.end_at <= normalized_start or bar.start_at >= normalized_end:
                    continue
                if previous_bar_start is not None and bar.start_at <= previous_bar_start:
                    raise ValidationMarketDataError(
                        "non-increasing timestamps for symbol "
                        f"{mapping.market_symbol!r}."
                    )
                previous_bar_start = bar.start_at
                bars.append(bar)

            raw_next_page_token = payload.get("next_page_token")
            if raw_next_page_token is None:
                break
            if not isinstance(raw_next_page_token, str) or not raw_next_page_token.strip():
                raise ValidationMarketDataError("next_page_token must be a non-empty string.")
            normalized_next_page_token = raw_next_page_token.strip()
            if normalized_next_page_token in seen_page_tokens:
                raise ValidationMarketDataError("next_page_token must not repeat.")
            seen_page_tokens.add(normalized_next_page_token)
            page_token = normalized_next_page_token

        if not bars:
            raise ValidationMarketDataError(
                f"empty bars for symbol {mapping.market_symbol!r}."
            )

        try:
            return MarketDataSeries(bars=tuple(bars))
        except ViewStateChangeContractError as exc:
            raise ValidationMarketDataError(
                f"invalid market bar series for symbol {mapping.market_symbol!r}: {exc}"
            ) from exc

    def _fetch_bars_page(
        self,
        *,
        symbol: str,
        bar_granularity: str,
        start_at: datetime,
        end_at: datetime,
        page_token: str | None,
    ) -> dict[str, object]:
        try:
            from event_trader.market.stock_bar_adapter import build_stock_bar_request_query

            query = build_stock_bar_request_query(
                provider=self._provider,
                symbol=symbol,
                bar_granularity=bar_granularity,
                start_at=start_at,
                end_at=end_at,
            )
        except ValueError as exc:
            raise ValidationMarketDataError(str(exc)) from exc
        if page_token is not None:
            query["page_token"] = page_token
        try:
            from event_trader.market.stock_bar_adapter import build_stock_bar_request_headers

            headers = build_stock_bar_request_headers(
                provider=self._provider,
                api_key=self._api_key,
            )
        except ValueError as exc:
            raise ValidationMarketDataError(str(exc)) from exc

        request = Request(
            url=f"{self._base_url}?{urlencode(query)}",
            headers=headers,
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload_bytes = response.read()
        except OSError as exc:
            raise ValidationMarketDataError(f"market data request failed: {exc}") from exc

        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationMarketDataError(
                "market data response must be valid UTF-8 JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise ValidationMarketDataError("market data response must be a JSON object.")
        return payload


def _validate_utc_datetime(raw_value: datetime, *, field_name: str) -> datetime:
    if not isinstance(raw_value, datetime):
        raise ValidationMarketDataError(f"{field_name} must be a datetime.")
    if raw_value.tzinfo is None or raw_value.utcoffset() is None:
        raise ValidationMarketDataError(f"{field_name} must be timezone-aware UTC.")
    if raw_value.utcoffset() != UTC.utcoffset(None):
        raise ValidationMarketDataError(f"{field_name} must be timezone-aware UTC.")
    return raw_value.astimezone(UTC)


__all__ = [
    "ApiStocksMarketDataPort",
    "ValidationMarketDataError",
]
