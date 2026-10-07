"""Provider seam for shared live/replay market bars."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any, Protocol, cast
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from event_trader.config import (
    KernelConfig,
    MarketDataProviderConfig,
    ValidationMarketDataConfig,
)
from event_trader.contracts.view_state_change import (
    ExchangeSessionScope,
    MarketDataBar,
    MarketDataSeries,
    MarketMapping,
    MarketSession,
    ViewStateChangeContractError,
)
from event_trader.market.contracts import (
    OptionBarObservation,
    OptionContractSnapshot,
    OptionSelectionPolicy,
    OptionTradeObservation,
)
from event_trader.market.local_archive_provider import (
    build_local_archive_market_data_provider,
)
from event_trader.validation.market_data import ApiStocksMarketDataPort

_BC_PRIVATE_STOCK_BARS_PER_REQUEST = 120
_BC_PRIVATE_STOCK_BAR_RETRY_COUNT = 2
_BC_PRIVATE_STOCK_BAR_MIN_CHUNK_MULTIPLE = 24
_BC_PRIVATE_OPTION_PAGE_LIMIT = 3
_FUTU_US_EQUITY_EXCHANGES = frozenset({"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"})
_FUTU_TIMEZONE_BY_CODE_PREFIX = {
    "US": "America/New_York",
    "HK": "Asia/Hong_Kong",
    "SH": "Asia/Shanghai",
    "SZ": "Asia/Shanghai",
}
_FUTU_APPDATA_FALLBACK_DIRNAME = "event_trader_futu_appdata"
_BINANCE_SPOT_KLINES_ROUTE = "/api/v3/klines"
_BINANCE_SPOT_KLINES_PER_REQUEST = 1_000
_BINANCE_SPOT_KLINE_INTERVALS = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
}
_HYPERLIQUID_INFO_ROUTE = "/info"
_HYPERLIQUID_CANDLES_PER_REQUEST = 500
_HYPERLIQUID_CANDLE_INTERVALS = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
}


@dataclass(frozen=True, slots=True)
class _StockBarChunkRead:
    bars: tuple[MarketDataBar, ...]
    successful_chunk_count: int
    empty_chunk_count: int
    failed_chunks: tuple[dict[str, str], ...]
    retry_count: int


class MarketBarsProvider(Protocol):
    """Read target market bars through a deterministic market-data seam."""

    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        """Return OHLCV bars for the requested market mapping and time window."""


class MarketOptionsProvider(Protocol):
    """Read option-market observations for deterministic derivatives context."""

    def read_option_chain(
        self,
        *,
        underlying_symbol: str,
        as_of_at: datetime,
        underlying_price: float,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        """Return visible option-chain snapshots for the configured underlying."""

    def read_option_chain_metadata(
        self,
        *,
        underlying_symbol: str,
        as_of_at: datetime,
        underlying_price: float,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        """Return latest-chain contract identities only for replay candidate selection."""

    def read_latest_option_trades(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionTradeObservation, ...]:
        """Read the latest trade observation per requested option contract."""

    def read_option_trades(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        start_at: datetime,
        end_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionTradeObservation, ...]:
        """Return visible option trades for selected contracts."""

    def read_latest_option_quotes(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        """Read latest quote-bearing snapshots for requested option contracts."""

    def read_option_bars(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        start_at: datetime,
        end_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionBarObservation, ...]:
        """Return visible option bars for selected contracts."""


class MarketDataProvider(MarketBarsProvider, MarketOptionsProvider, Protocol):
    """Combined market-data seam used by the shared market-context builder."""


class BcPrivateMarketDataProvider:
    """BC private API-backed market provider for bars and option context."""

    def __init__(self, config: KernelConfig) -> None:
        if config.validation is None:
            raise ValueError("validation config is required for BC private market data.")
        self._stock_bars = ApiStocksMarketDataPort(config.validation.market_data)
        market_data_config = config.validation.market_data
        self._provider = market_data_config.provider
        self._api_root = _derive_api_root(market_data_config.base_url)
        self._api_key = market_data_config.api_key
        self._timeout_seconds = market_data_config.timeout_seconds
        self._source_metadata: dict[str, object] = {}

    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        series, metadata = self.read_series_with_metadata(
            mapping,
            start_at=start_at,
            end_at=end_at,
        )
        self._source_metadata.update(metadata)
        return series

    def read_series_with_metadata(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[MarketDataSeries, dict[str, object]]:
        if self._provider != "bc_private_v1":
            return (
                self._stock_bars.read_series(
                    mapping,
                    start_at=start_at,
                    end_at=end_at,
                ),
                {},
            )
        try:
            from event_trader.market.stock_bar_adapter import (
                parse_stock_bar_granularity,
                validate_stock_bar_capability,
            )

            validate_stock_bar_capability(provider=self._provider, mapping=mapping)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        bar_delta = parse_stock_bar_granularity(mapping.bar_granularity, allow_minute_suffix=True)
        chunk_delta = bar_delta * _BC_PRIVATE_STOCK_BARS_PER_REQUEST
        min_chunk_delta = bar_delta * _BC_PRIVATE_STOCK_BAR_MIN_CHUNK_MULTIPLE
        bars_by_key: dict[tuple[datetime, datetime], MarketDataBar] = {}
        successful_chunk_count = 0
        empty_chunk_count = 0
        failed_chunks: list[dict[str, str]] = []
        retry_count = 0
        chunk_windows = tuple(_chunk_window(start_at, end_at, chunk_delta))
        for chunk_start, chunk_end in chunk_windows:
            chunk = self._read_stock_bar_chunk_resilient(
                mapping=mapping,
                start_at=chunk_start,
                end_at=chunk_end,
                requested_end_at=end_at,
                min_chunk_delta=min_chunk_delta,
            )
            successful_chunk_count += chunk.successful_chunk_count
            empty_chunk_count += chunk.empty_chunk_count
            failed_chunks.extend(chunk.failed_chunks)
            retry_count += chunk.retry_count
            for bar in chunk.bars:
                bars_by_key[(bar.start_at, bar.end_at)] = bar

        ordered_bars = tuple(
            bar
            for _, bar in sorted(
                bars_by_key.items(),
                key=lambda item: (item[0][0], item[0][1]),
            )
        )
        metadata: dict[str, object] = {
            "stock_bar_chunk_count": (
                successful_chunk_count + empty_chunk_count + len(failed_chunks)
            ),
            "stock_bar_successful_chunk_count": successful_chunk_count,
            "stock_bar_empty_chunk_count": empty_chunk_count,
            "stock_bar_failed_chunk_count": len(failed_chunks),
            "stock_bar_partial": bool(failed_chunks),
            "stock_bar_retry_count": retry_count,
            "stock_bar_min_chunk_hours": min_chunk_delta.total_seconds() / 3600.0,
            "stock_bar_chunked": len(chunk_windows) > 1,
            "stock_bar_chunk_bars_per_request": _BC_PRIVATE_STOCK_BARS_PER_REQUEST,
            "stock_bar_failed_chunks": failed_chunks,
        }
        if ordered_bars:
            return MarketDataSeries(bars=ordered_bars), metadata
        if failed_chunks:
            self._source_metadata.update(metadata)
            raise ValueError(
                "market data request failed for all stock bar chunks; "
                f"failed_chunk_count={len(failed_chunks)}"
            )
        self._source_metadata.update(metadata)
        raise ValueError(f"empty bars for symbol {mapping.market_symbol!r}.")

    def _read_stock_bar_chunk_resilient(
        self,
        *,
        mapping: MarketMapping,
        start_at: datetime,
        end_at: datetime,
        requested_end_at: datetime,
        min_chunk_delta: timedelta,
    ) -> _StockBarChunkRead:
        retry_count = 0
        last_error: Exception | None = None
        while True:
            try:
                return self._read_stock_bar_chunk(
                    mapping=mapping,
                    start_at=start_at,
                    end_at=end_at,
                    requested_end_at=requested_end_at,
                )
            except Exception as exc:
                last_error = exc
                if _is_empty_bar_error(exc):
                    return _StockBarChunkRead(
                        bars=(),
                        successful_chunk_count=0,
                        empty_chunk_count=1,
                        failed_chunks=(),
                        retry_count=retry_count,
                    )
                if not _is_recoverable_stock_bar_error(exc):
                    raise
                if retry_count < _BC_PRIVATE_STOCK_BAR_RETRY_COUNT:
                    retry_count += 1
                    continue
                if end_at - start_at > min_chunk_delta:
                    midpoint = start_at + ((end_at - start_at) / 2)
                    if midpoint <= start_at or midpoint >= end_at:
                        break
                    left = self._read_stock_bar_chunk_resilient(
                        mapping=mapping,
                        start_at=start_at,
                        end_at=midpoint,
                        requested_end_at=requested_end_at,
                        min_chunk_delta=min_chunk_delta,
                    )
                    right = self._read_stock_bar_chunk_resilient(
                        mapping=mapping,
                        start_at=midpoint,
                        end_at=end_at,
                        requested_end_at=requested_end_at,
                        min_chunk_delta=min_chunk_delta,
                    )
                    return _combine_stock_bar_reads(left, right, retry_count=retry_count)
                break
        if last_error is None:
            raise ValueError("stock bar chunk failed without an error.")
        return _StockBarChunkRead(
            bars=(),
            successful_chunk_count=0,
            empty_chunk_count=0,
            failed_chunks=(
                _failed_chunk_summary(
                    start_at=start_at,
                    end_at=end_at,
                    error=last_error,
                ),
            ),
            retry_count=retry_count,
        )

    def _read_stock_bar_chunk(
        self,
        *,
        mapping: MarketMapping,
        start_at: datetime,
        end_at: datetime,
        requested_end_at: datetime,
    ) -> _StockBarChunkRead:
        series = self._stock_bars.read_series(
            mapping,
            start_at=start_at,
            end_at=end_at,
        )
        return _StockBarChunkRead(
            bars=tuple(bar for bar in series.bars if bar.end_at <= requested_end_at),
            successful_chunk_count=1,
            empty_chunk_count=0,
            failed_chunks=(),
            retry_count=0,
        )

    def pop_source_metadata(self) -> dict[str, object]:
        """Return and clear provider-local request metadata for snapshot audit."""
        metadata = dict(self._source_metadata)
        self._source_metadata.clear()
        return metadata

    def read_option_chain(
        self,
        *,
        underlying_symbol: str,
        as_of_at: datetime,
        underlying_price: float,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        expiry_start = as_of_at.date().isoformat()
        expiry_end = (as_of_at.date()).toordinal() + policy.expiry_days_max
        expiry_end_date = datetime.fromordinal(expiry_end).date().isoformat()
        query = {
            "data_feed": policy.data_feed,
            "strike_min": str(underlying_price * (1.0 - policy.strike_pct_window)),
            "strike_max": str(underlying_price * (1.0 + policy.strike_pct_window)),
            "expiry_start": expiry_start,
            "expiry_end": expiry_end_date,
            "max_rows": str(policy.chain_max_rows),
        }
        rows: list[OptionContractSnapshot] = []
        for payload in self._read_paginated(
            f"/market/options/snapshots/{underlying_symbol}",
            query=query,
            metadata_key="option_chain",
        ):
            rows.extend(
                _parse_option_chain_payload(
                    payload,
                    underlying_symbol=underlying_symbol,
                )
            )
        return tuple(row for row in rows if _option_observed_at(row) <= as_of_at)

    def read_option_chain_metadata(
        self,
        *,
        underlying_symbol: str,
        as_of_at: datetime,
        underlying_price: float,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        expiry_start = as_of_at.date().isoformat()
        expiry_end = (as_of_at.date()).toordinal() + policy.expiry_days_max
        expiry_end_date = datetime.fromordinal(expiry_end).date().isoformat()
        query = {
            "data_feed": policy.data_feed,
            "strike_min": str(underlying_price * (1.0 - policy.strike_pct_window)),
            "strike_max": str(underlying_price * (1.0 + policy.strike_pct_window)),
            "expiry_start": expiry_start,
            "expiry_end": expiry_end_date,
            "max_rows": str(policy.chain_max_rows),
        }
        rows: list[OptionContractSnapshot] = []
        for payload in self._read_paginated(
            f"/market/options/snapshots/{underlying_symbol}",
            query=query,
            metadata_key="option_chain_metadata",
        ):
            rows.extend(
                _parse_option_chain_payload(
                    payload,
                    underlying_symbol=underlying_symbol,
                )
            )
        return tuple(_metadata_only_contract(row) for row in rows)

    def read_latest_option_trades(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionTradeObservation, ...]:
        if not contracts:
            return ()
        query = {
            "contracts": ",".join(contract.contract_symbol for contract in contracts),
        }
        if policy.data_feed:
            query["data_feed"] = policy.data_feed
        observations: list[OptionTradeObservation] = []
        contract_lookup = {contract.contract_symbol: contract for contract in contracts}
        try:
            for payload in self._read_paginated(
                "/market/options/trades/latest",
                query=query,
                metadata_key="option_latest_trades",
            ):
                observations.extend(
                    _parse_option_trades_payload(
                        payload,
                        contract_lookup=contract_lookup,
                    )
                )
        except Exception as exc:
            self._source_metadata["option_latest_trades_error"] = (
                _provider_error_summary(exc)
            )
            return ()
        return tuple(
            row
            for row in observations
            if row.observed_at <= as_of_at
        )

    def read_latest_option_quotes(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        if not contracts:
            return ()
        query = {
            "contracts": ",".join(contract.contract_symbol for contract in contracts),
        }
        if policy.data_feed:
            query["data_feed"] = policy.data_feed
        rows: list[OptionContractSnapshot] = []
        try:
            for payload in self._read_paginated(
                "/market/options/quotes/latest",
                query=query,
                metadata_key="option_latest_quotes",
            ):
                rows.extend(
                    _parse_option_chain_payload(
                        payload,
                        underlying_symbol=contracts[0].underlying_symbol,
                    )
                )
        except Exception as exc:
            self._source_metadata["option_latest_quotes_error"] = (
                _provider_error_summary(exc)
            )
            return ()
        return tuple(
            row
            for row in rows
            if row.latest_quote_at is not None and row.latest_quote_at <= as_of_at
        )

    def read_option_latest_trades(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionTradeObservation, ...]:
        return self.read_latest_option_trades(
            contracts=contracts,
            as_of_at=as_of_at,
            policy=policy,
        )

    def read_option_latest_quotes(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        return self.read_latest_option_quotes(
            contracts=contracts,
            as_of_at=as_of_at,
            policy=policy,
        )

    def read_option_trades(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        start_at: datetime,
        end_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionTradeObservation, ...]:
        if not contracts:
            return ()
        query = {
            "contracts": ",".join(contract.contract_symbol for contract in contracts),
            "start_time": _format_utc_timestamp(start_at),
            "end_time": _format_utc_timestamp(end_at),
            "max_rows": str(policy.chain_max_rows),
            "order": "asc",
        }
        observations: list[OptionTradeObservation] = []
        contract_lookup = {contract.contract_symbol: contract for contract in contracts}
        for payload in self._read_paginated(
            "/market/options/trades",
            query=query,
            metadata_key="option_trades",
        ):
            observations.extend(
                _parse_option_trades_payload(payload, contract_lookup=contract_lookup)
            )
        return tuple(item for item in observations if start_at <= item.observed_at <= end_at)

    def read_option_bars(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        start_at: datetime,
        end_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionBarObservation, ...]:
        if not contracts:
            return ()
        query = {
            "contracts": ",".join(contract.contract_symbol for contract in contracts),
            "interval": _translate_bc_private_interval(policy.bars_granularity),
            "start_time": _format_utc_timestamp(start_at),
            "end_time": _format_utc_timestamp(end_at),
            "max_rows": str(policy.chain_max_rows),
            "order": "asc",
        }
        observations: list[OptionBarObservation] = []
        contract_lookup = {contract.contract_symbol: contract for contract in contracts}
        for payload in self._read_paginated(
            "/market/options/bars",
            query=query,
            metadata_key="option_bars",
        ):
            observations.extend(
                _parse_option_bars_payload(
                    payload,
                    contract_lookup=contract_lookup,
                    bar_granularity=policy.bars_granularity,
                )
            )
        return tuple(item for item in observations if item.end_at <= end_at)

    def _read_paginated(
        self,
        route: str,
        *,
        query: dict[str, str],
        metadata_key: str,
        max_pages: int = _BC_PRIVATE_OPTION_PAGE_LIMIT,
    ) -> tuple[dict[str, object], ...]:
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        payloads: list[dict[str, object]] = []
        while len(payloads) < max_pages:
            page_query = dict(query)
            if page_token is not None:
                page_query["page_token"] = page_token
            payload = self._fetch_json(route, page_query)
            payloads.append(payload)
            raw_next_page_token = payload.get("next_page_token")
            if raw_next_page_token is None:
                page_token = None
                break
            if not isinstance(raw_next_page_token, str) or not raw_next_page_token.strip():
                raise ValueError("next_page_token must be a non-empty string.")
            normalized_next_page_token = raw_next_page_token.strip()
            if normalized_next_page_token in seen_page_tokens:
                raise ValueError("next_page_token must not repeat.")
            seen_page_tokens.add(normalized_next_page_token)
            page_token = normalized_next_page_token
        self._source_metadata[f"{metadata_key}_page_count"] = len(payloads)
        self._source_metadata[f"{metadata_key}_page_limit"] = max_pages
        self._source_metadata[f"{metadata_key}_pagination_truncated"] = (
            page_token is not None
        )
        if page_token is not None:
            self._source_metadata[f"{metadata_key}_next_page_token_after_truncation"] = (
                page_token
            )
        return tuple(payloads)

    def _fetch_json(self, route: str, query: dict[str, str]) -> dict[str, object]:
        if self._provider != "bc_private_v1":
            raise ValueError(f"Unsupported options market-data provider: {self._provider!r}.")
        request = Request(
            url=f"{self._api_root}{route}?{urlencode(query)}",
            headers={"Accept": "application/json", "X-API-KEY": self._api_key},
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:
            payload_bytes = response.read()
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("market data response must be valid UTF-8 JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("market data response must be a JSON object.")
        return payload


class BinanceSpotMarketDataProvider:
    """Binance Spot REST-backed provider for completed UTC crypto OHLCV bars."""

    def __init__(self, config: KernelConfig) -> None:
        if config.validation is None:
            raise ValueError("validation config is required for Binance Spot market data.")
        market_data_config = config.validation.market_data
        if market_data_config.provider != "binance_spot":
            raise ValueError(
                "Binance Spot provider requires validation.market_data.provider=binance_spot."
            )
        self._base_url = market_data_config.base_url.rstrip("/")
        self._timeout_seconds = market_data_config.timeout_seconds
        self._source_metadata: dict[str, object] = {}

    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        normalized_start = _ensure_utc(start_at, field_name="start_at")
        normalized_end = _ensure_utc(end_at, field_name="end_at")
        if normalized_start >= normalized_end:
            raise ValueError("invalid window: start_at must be earlier than end_at.")
        if mapping.market_session != "continuous" or mapping.exchange is not None:
            raise ValueError(
                "binance_spot bars require a continuous market mapping without exchange."
            )
        interval, bar_delta = _binance_spot_kline_interval(mapping.bar_granularity)
        symbol = mapping.market_symbol.strip().upper()
        if not symbol:
            raise ValueError("binance_spot market_symbol must not be empty.")

        bars_by_key: dict[tuple[datetime, datetime], MarketDataBar] = {}
        cursor = normalized_start
        request_count = 0
        while cursor < normalized_end:
            request_count += 1
            rows = self._fetch_klines(
                symbol=symbol,
                interval=interval,
                start_at=cursor,
                end_at=normalized_end,
            )
            if not rows:
                break
            previous_cursor = cursor
            for index, row in enumerate(rows):
                bar = _parse_binance_spot_kline(
                    row,
                    bar_delta=bar_delta,
                    symbol=symbol,
                    item_index=index,
                )
                if bar.end_at > normalized_start and bar.end_at <= normalized_end:
                    bars_by_key[(bar.start_at, bar.end_at)] = bar
                if bar.start_at >= cursor:
                    cursor = bar.start_at + bar_delta
            if cursor <= previous_cursor:
                raise ValueError(
                    f"Binance Spot K-line response did not advance for symbol {symbol!r}."
                )
            if len(rows) < _BINANCE_SPOT_KLINES_PER_REQUEST:
                break

        bars = tuple(
            bar
            for _, bar in sorted(
                bars_by_key.items(),
                key=lambda item: (item[0][0], item[0][1]),
            )
        )
        self._source_metadata.update(
            {
                "provider": "binance_spot",
                "remote_fallback": True,
                "requested_symbol": mapping.market_symbol,
                "binance_symbol": symbol,
                "interval": interval,
                "request_count": request_count,
                "returned_bar_count": len(bars),
                "timeout_seconds": self._timeout_seconds,
                "timezone": "UTC",
                "timestamp_policy": "completed_interval_end",
                "market_session": mapping.market_session,
                "adjustment_policy": "raw",
            }
        )
        if not bars:
            raise ValueError(f"empty Binance Spot bars for symbol {symbol!r}.")
        try:
            return MarketDataSeries(bars=bars)
        except ViewStateChangeContractError as exc:
            raise ValueError(
                f"invalid Binance Spot bar series for symbol {symbol!r}: {exc}"
            ) from exc

    def pop_source_metadata(self) -> dict[str, object]:
        metadata = dict(self._source_metadata)
        self._source_metadata.clear()
        return metadata

    def _fetch_klines(
        self,
        *,
        symbol: str,
        interval: str,
        start_at: datetime,
        end_at: datetime,
    ) -> list[object]:
        query = urlencode(
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": _epoch_milliseconds(start_at),
                "endTime": _epoch_milliseconds(end_at) - 1,
                "limit": _BINANCE_SPOT_KLINES_PER_REQUEST,
            }
        )
        request = Request(
            url=f"{self._base_url}{_BINANCE_SPOT_KLINES_ROUTE}?{query}",
            headers={"Accept": "application/json"},
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:
            payload_bytes = response.read()
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Binance Spot K-line response must be valid UTF-8 JSON.") from exc
        if not isinstance(payload, list):
            raise ValueError("Binance Spot K-line response must be a JSON array.")
        return payload


class HyperliquidPerpMarketDataProvider:
    """Hyperliquid perpetual REST-backed provider for completed UTC OHLCV bars."""

    def __init__(self, config: KernelConfig) -> None:
        market_data_config = _market_data_provider_config(
            config,
            provider_name="hyperliquid_perp",
        )
        self._base_url = market_data_config.base_url.rstrip("/")
        self._timeout_seconds = market_data_config.timeout_seconds
        self._source_metadata: dict[str, object] = {}

    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        normalized_start = _ensure_utc(start_at, field_name="start_at")
        normalized_end = _ensure_utc(end_at, field_name="end_at")
        if normalized_start >= normalized_end:
            raise ValueError("invalid window: start_at must be earlier than end_at.")
        if mapping.market_session != "continuous" or mapping.exchange is not None:
            raise ValueError(
                "hyperliquid_perp bars require a continuous market mapping without exchange."
            )
        interval, bar_delta = _hyperliquid_candle_interval(mapping.bar_granularity)
        coin = _hyperliquid_perp_coin(mapping.market_symbol)
        bars_by_key: dict[tuple[datetime, datetime], MarketDataBar] = {}
        cursor = normalized_start
        request_count = 0
        while cursor < normalized_end:
            request_count += 1
            rows = self._fetch_candles(
                coin=coin,
                interval=interval,
                start_at=cursor,
                end_at=normalized_end,
            )
            if not rows:
                break
            previous_cursor = cursor
            for index, row in enumerate(rows):
                bar = _parse_hyperliquid_candle(
                    row,
                    bar_delta=bar_delta,
                    coin=coin,
                    item_index=index,
                )
                if bar.end_at > normalized_start and bar.end_at <= normalized_end:
                    bars_by_key[(bar.start_at, bar.end_at)] = bar
                if bar.start_at >= cursor:
                    cursor = bar.start_at + bar_delta
            if cursor <= previous_cursor:
                raise ValueError(
                    "Hyperliquid candle response did not advance for "
                    f"coin {coin!r}."
                )
            if len(rows) < _HYPERLIQUID_CANDLES_PER_REQUEST:
                break

        bars = tuple(
            bar
            for _, bar in sorted(
                bars_by_key.items(),
                key=lambda item: (item[0][0], item[0][1]),
            )
        )
        self._source_metadata.update(
            {
                "provider": "hyperliquid_perp",
                "remote_fallback": True,
                "requested_symbol": mapping.market_symbol,
                "hyperliquid_coin": coin,
                "interval": interval,
                "request_count": request_count,
                "returned_bar_count": len(bars),
                "timeout_seconds": self._timeout_seconds,
                "timezone": "UTC",
                "timestamp_policy": "completed_interval_end",
                "market_session": "continuous",
                "adjustment_policy": "raw",
            }
        )
        return MarketDataSeries(bars=bars)

    def pop_source_metadata(self) -> dict[str, object]:
        metadata = dict(self._source_metadata)
        self._source_metadata.clear()
        return metadata

    def _fetch_candles(
        self,
        *,
        coin: str,
        interval: str,
        start_at: datetime,
        end_at: datetime,
    ) -> list[object]:
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": _epoch_milliseconds(start_at),
                "endTime": _epoch_milliseconds(end_at),
            },
        }
        request = Request(
            url=f"{self._base_url}{_HYPERLIQUID_INFO_ROUTE}",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:
            payload_bytes = response.read()
        try:
            response_payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Hyperliquid candle response must be valid UTF-8 JSON.") from exc
        if not isinstance(response_payload, list):
            raise ValueError("Hyperliquid candle response must be a JSON array.")
        return response_payload


class SubscriptionRoutedMarketBarsProvider:
    """Route each configured live subscription to its declared provider."""

    def __init__(
        self,
        *,
        providers: Mapping[str, MarketBarsProvider],
        provider_names: Mapping[tuple[str, str], str],
        default_provider_name: str,
    ) -> None:
        self._providers = dict(providers)
        self._provider_names = {
            (target_key, symbol.upper()): provider_name
            for (target_key, symbol), provider_name in provider_names.items()
        }
        self._default_provider_name = default_provider_name
        self._source_metadata: dict[str, object] = {}
        self._lock = RLock()

    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        provider_name = self._provider_names.get(
            (mapping.target_key, mapping.market_symbol.upper()),
            self._default_provider_name,
        )
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ValueError(f"No market-bars provider is configured for {provider_name!r}.")
        with self._lock:
            series = provider.read_series(mapping, start_at=start_at, end_at=end_at)
            consume = getattr(provider, "pop_source_metadata", None)
            metadata = consume() if callable(consume) else {}
            self._source_metadata = (
                dict(metadata) if isinstance(metadata, dict) else {"provider": provider_name}
            )
        return series

    def pop_source_metadata(self) -> dict[str, object]:
        with self._lock:
            metadata = dict(self._source_metadata)
            self._source_metadata.clear()
            return metadata

    def close(self) -> None:
        for provider in self._providers.values():
            close = getattr(provider, "close", None)
            if callable(close):
                close()


class FutuOpenApiMarketDataProvider:
    """Futu OpenD-backed stock/ETF bar provider."""

    def __init__(
        self,
        config: KernelConfig,
        *,
        futu_module: Any | None = None,
    ) -> None:
        market_data_config = _market_data_provider_config(
            config,
            provider_name="futu_openapi",
        )
        host, port = _parse_futu_openapi_endpoint(market_data_config.base_url)
        self._host = host
        self._port = port
        self._timeout_seconds = market_data_config.timeout_seconds
        self._futu_module = futu_module
        self._source_metadata: dict[str, object] = {}
        self._quote_ctx: Any | None = None
        self._quote_ctx_lock = RLock()

    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        futu = self._futu_module or _load_futu_module()
        normalized_start = _ensure_utc(start_at, field_name="start_at")
        normalized_end = _ensure_utc(end_at, field_name="end_at")
        if normalized_start >= normalized_end:
            raise ValueError("invalid window: start_at must be earlier than end_at.")

        futu_code = _futu_security_code(mapping)
        ktype = _futu_kl_type(futu, mapping.bar_granularity)
        timezone_name = _futu_history_timezone_name(mapping=mapping, futu_code=futu_code)
        session_params = _futu_history_session_params(
            futu,
            mapping=mapping,
            bar_granularity=mapping.bar_granularity,
            futu_code=futu_code,
        )
        autype, adjustment_policy = _futu_history_autype(futu)
        request_start, request_end = _futu_history_dates(
            start_at=normalized_start,
            end_at=normalized_end,
            timezone_name=timezone_name,
        )
        page_req_key: object | None = None
        bars_by_key: dict[tuple[datetime, datetime], MarketDataBar] = {}
        request_count = 0
        with self._quote_ctx_lock:
            quote_ctx = self._quote_context(futu)
            try:
                while True:
                    request_count += 1
                    ret, data, page_req_key = quote_ctx.request_history_kline(
                        futu_code,
                        start=request_start,
                        end=request_end,
                        ktype=ktype,
                        autype=autype,
                        max_count=1000,
                        page_req_key=page_req_key,
                        **session_params,
                    )
                    if ret != futu.RET_OK:
                        raise ValueError(f"Futu historical K-line request failed: {data}")
                    for bar in _futu_rows_to_bars(
                        data,
                        bar_granularity=mapping.bar_granularity,
                        symbol=futu_code,
                        timezone_name=timezone_name,
                    ):
                        if bar.end_at > normalized_start and bar.start_at < normalized_end:
                            bars_by_key[(bar.start_at, bar.end_at)] = bar
                    if page_req_key is None:
                        break
            except Exception:
                self._close_quote_context_locked(suppress_errors=True)
                raise

        bars = tuple(
            bar
            for _, bar in sorted(
                bars_by_key.items(),
                key=lambda item: (item[0][0], item[0][1]),
            )
        )
        self._source_metadata.update(
            {
                "provider": "futu_openapi",
                "remote_fallback": True,
                "requested_symbol": mapping.market_symbol,
                "futu_code": futu_code,
                "request_start_date": request_start,
                "request_end_date": request_end,
                "request_count": request_count,
                "returned_bar_count": len(bars),
                "timeout_seconds": self._timeout_seconds,
                "exchange": mapping.exchange,
                "timezone": timezone_name,
                "adjustment_policy": adjustment_policy,
                "timestamp_policy": "end_at",
                "exchange_session_scope": mapping.exchange_session_scope,
            }
        )
        if "extended_time" in session_params:
            self._source_metadata["extended_time"] = session_params["extended_time"]
        if "session" in session_params:
            self._source_metadata["session"] = session_params["session"]
        if not bars:
            raise ValueError(f"empty Futu bars for symbol {futu_code!r}.")
        try:
            return MarketDataSeries(bars=bars)
        except ViewStateChangeContractError as exc:
            raise ValueError(f"invalid Futu bar series for symbol {futu_code!r}: {exc}") from exc

    def pop_source_metadata(self) -> dict[str, object]:
        metadata = dict(self._source_metadata)
        self._source_metadata.clear()
        return metadata

    def close(self) -> None:
        with self._quote_ctx_lock:
            self._close_quote_context_locked(suppress_errors=False)

    def _quote_context(self, futu: Any) -> Any:
        if self._quote_ctx is None:
            self._quote_ctx = futu.OpenQuoteContext(host=self._host, port=self._port)
        return self._quote_ctx

    def _close_quote_context_locked(self, *, suppress_errors: bool) -> None:
        quote_ctx = self._quote_ctx
        self._quote_ctx = None
        if quote_ctx is None:
            return
        try:
            quote_ctx.close()
        except Exception:
            if not suppress_errors:
                raise


def build_default_market_bars_provider(config: KernelConfig) -> MarketBarsProvider | None:
    """Build the shared market-bars provider from existing validation config."""
    if config.validation is None:
        return None
    default_provider_name = config.validation.market_data.provider
    provider_names = _live_subscription_provider_names(config)
    distinct_provider_names = {default_provider_name, *provider_names.values()}
    if not distinct_provider_names or distinct_provider_names == {default_provider_name}:
        return _build_market_bars_provider_for_name(
            config,
            provider_name=default_provider_name,
        )
    providers = {
        provider_name: _build_market_bars_provider_for_name(
            config,
            provider_name=provider_name,
        )
        for provider_name in sorted(distinct_provider_names)
    }
    return SubscriptionRoutedMarketBarsProvider(
        providers=providers,
        provider_names=provider_names,
        default_provider_name=default_provider_name,
    )


def build_market_bars_provider(
    config: KernelConfig,
    *,
    provider_name: str,
) -> MarketBarsProvider:
    """Build one supported replay/live market-bars provider by concrete name."""
    if provider_name == "local_archive":
        return build_local_archive_market_data_provider(config)
    return _build_market_bars_provider_for_name(config, provider_name=provider_name)


def _build_market_bars_provider_for_name(
    config: KernelConfig,
    *,
    provider_name: str,
) -> MarketBarsProvider:
    configured_provider = (
        None if config.validation is None else config.validation.market_data.provider
    )
    if provider_name == "hyperliquid_perp":
        return HyperliquidPerpMarketDataProvider(config)
    if provider_name == "futu_openapi":
        return FutuOpenApiMarketDataProvider(config)
    if provider_name != configured_provider:
        raise ValueError(
            "market-data provider is not available from current validation.market_data config: "
            f"{provider_name!r}"
        )
    if provider_name == "bc_private_v1":
        return BcPrivateMarketDataProvider(config)
    if provider_name == "binance_spot":
        return BinanceSpotMarketDataProvider(config)
    if config.validation is None:
        raise ValueError("market-data provider requires validation.market_data config.")
    return ApiStocksMarketDataPort(config.validation.market_data)


def _market_data_provider_config(
    config: KernelConfig,
    *,
    provider_name: str,
) -> ValidationMarketDataConfig | MarketDataProviderConfig:
    if config.validation is None:
        raise ValueError("validation config is required for market data.")
    primary = config.validation.market_data
    if primary.provider == provider_name:
        return primary
    supplemental = next(
        (
            connection
            for name, connection in primary.supplemental_providers.items()
            if name == provider_name
        ),
        None,
    )
    if supplemental is None:
        raise ValueError(
            "market-data provider is not configured in validation.market_data: "
            f"{provider_name!r}"
        )
    return supplemental


def _live_subscription_provider_names(config: KernelConfig) -> dict[tuple[str, str], str]:
    live = config.live
    market_data = None if live is None else live.market_data
    if market_data is None or not market_data.enabled:
        return {}
    return {
        (target.target_key, subscription.symbol): subscription.provider
        for target in market_data.targets
        for subscription in target.subscriptions
    }


def market_mapping_from_profile(
    *,
    target_key: str,
    tradable_proxy_symbol: str,
    bar_granularity: str,
    market_session: str = "continuous",
    exchange: str | None = None,
    exchange_session_scope: str | None = None,
) -> MarketMapping:
    """Map a market-context profile onto the existing bar-provider contract."""
    try:
        return MarketMapping(
            target_key=target_key,
            market_symbol=tradable_proxy_symbol,
            market_session=cast(MarketSession, market_session),
            exchange=exchange,
            bar_granularity=bar_granularity,
            exchange_session_scope=cast(ExchangeSessionScope | None, exchange_session_scope),
        )
    except ViewStateChangeContractError as exc:
        raise ValueError(f"invalid market-context market mapping: {exc}") from exc


def _derive_api_root(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    marker = "/market/"
    marker_index = normalized.find(marker)
    if marker_index >= 0:
        return normalized[:marker_index]
    return normalized


def _parse_futu_openapi_endpoint(raw_value: str) -> tuple[str, int]:
    parsed = urlparse(raw_value)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if parsed.scheme != "tcp" or not parsed.hostname or port is None:
        raise ValueError("futu_openapi base_url must be tcp://host:port.")
    return parsed.hostname, port


def _load_futu_module() -> Any:
    try:
        return importlib.import_module("futu")
    except ImportError as exc:  # pragma: no cover - depends on local runtime package
        raise ValueError(
            "futu_openapi provider requires the 'futu-api' Python package."
        ) from exc
    except FileExistsError as exc:
        if not _is_futu_log_path_conflict(exc):
            raise
        _prepare_futu_import_retry()
        try:
            return importlib.import_module("futu")
        except ImportError as retry_exc:  # pragma: no cover - depends on local runtime package
            raise ValueError(
                "futu_openapi provider requires the 'futu-api' Python package."
            ) from retry_exc


def _is_futu_log_path_conflict(exc: FileExistsError) -> bool:
    filename = getattr(exc, "filename", None)
    error_code = getattr(exc, "winerror", None)
    if error_code is None:
        error_code = getattr(exc, "errno", None)
    if error_code != 183 or not isinstance(filename, str):
        return False
    normalized = filename.replace("\\", "/").lower()
    return normalized.endswith("/com.futunn.futuopend/log")


def _prepare_futu_import_retry() -> None:
    fallback_appdata = os.path.join(
        tempfile.gettempdir(),
        _FUTU_APPDATA_FALLBACK_DIRNAME,
    )
    os.makedirs(fallback_appdata, exist_ok=True)
    os.environ["APPDATA"] = fallback_appdata
    os.environ["appdata"] = fallback_appdata
    for module_name in tuple(sys.modules):
        if module_name == "futu" or module_name.startswith("futu."):
            sys.modules.pop(module_name, None)


def _futu_history_session_params(
    futu: Any,
    *,
    mapping: MarketMapping,
    bar_granularity: str,
    futu_code: str,
) -> dict[str, object]:
    if mapping.market_session != "exchange_session":
        raise ValueError("futu_openapi stock bars require market_session='exchange_session'.")
    if _futu_code_prefix(futu_code) != "US":
        return {}
    from event_trader.market.stock_bar_adapter import parse_stock_bar_granularity

    bar_delta = parse_stock_bar_granularity(bar_granularity, allow_minute_suffix=False)
    if bar_delta > timedelta(minutes=60):
        raise ValueError(
            "futu_openapi exchange-session stock bars require intraday granularity "
            "of 60 minutes or less because Futu historical session requests are "
            "only documented for that range."
        )
    if mapping.exchange_session_scope == "regular":
        session = futu.Session.RTH
    elif mapping.exchange_session_scope == "extended":
        session = futu.Session.ETH
    else:
        raise ValueError(
            "futu_openapi stock bars require exchange_session_scope='regular' or 'extended'."
        )
    return {"extended_time": False, "session": session}


def _futu_security_code(mapping: MarketMapping) -> str:
    symbol = mapping.market_symbol.strip().upper()
    if "." in symbol:
        return symbol
    exchange = "" if mapping.exchange is None else mapping.exchange.strip().upper()
    if exchange in _FUTU_US_EQUITY_EXCHANGES:
        return f"US.{symbol}"
    if exchange in {"HK", "HKEX"}:
        return f"HK.{symbol}"
    if exchange in {"SH", "SSE"}:
        return f"SH.{symbol}"
    if exchange in {"SZ", "SZSE"}:
        return f"SZ.{symbol}"
    raise ValueError(
        "futu_openapi requires a Futu-prefixed market_symbol such as 'US.SPY' "
        "or a supported exchange mapping."
    )


def _futu_kl_type(futu: Any, raw_granularity: str) -> Any:
    normalized = raw_granularity.strip().lower()
    if normalized.endswith("min"):
        normalized = f"{int(normalized[:-3])}m"
    lookup = {
        "1m": futu.KLType.K_1M,
        "3m": futu.KLType.K_3M,
        "5m": futu.KLType.K_5M,
        "10m": futu.KLType.K_10M,
        "15m": futu.KLType.K_15M,
        "30m": futu.KLType.K_30M,
        "60m": futu.KLType.K_60M,
        "1h": futu.KLType.K_60M,
        "2h": futu.KLType.K_120M,
        "3h": futu.KLType.K_180M,
        "4h": futu.KLType.K_240M,
        "1d": futu.KLType.K_DAY,
    }
    try:
        return lookup[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported Futu bar granularity: {raw_granularity!r}.") from exc


def _futu_history_timezone_name(*, mapping: MarketMapping, futu_code: str) -> str:
    exchange = "" if mapping.exchange is None else mapping.exchange.strip().upper()
    if exchange in _FUTU_US_EQUITY_EXCHANGES:
        return "America/New_York"
    if exchange in {"HK", "HKEX"}:
        return "Asia/Hong_Kong"
    if exchange in {"SH", "SSE", "SZ", "SZSE"}:
        return "Asia/Shanghai"
    try:
        return _FUTU_TIMEZONE_BY_CODE_PREFIX[_futu_code_prefix(futu_code)]
    except KeyError as exc:
        raise ValueError(
            f"unsupported Futu market for exchange {mapping.exchange!r} and code {futu_code!r}."
        ) from exc


def _futu_history_autype(futu: Any) -> tuple[Any, str]:
    """Acquire provider bars on their raw basis.

    Adjustment is a target-level policy applied explicitly by consumers that
    configure one.  Requesting Futu QFQ data here silently turns every U.S.
    ETF into an adjusted input, including raw targets such as SOXX.
    """

    return futu.AuType.NONE, "raw"


def _futu_history_dates(
    *,
    start_at: datetime,
    end_at: datetime,
    timezone_name: str,
) -> tuple[str, str]:
    timezone = ZoneInfo(timezone_name)
    start_date = start_at.astimezone(timezone).date().isoformat()
    end_date = end_at.astimezone(timezone).date().isoformat()
    return start_date, end_date


def _futu_rows_to_bars(
    raw_rows: object,
    *,
    bar_granularity: str,
    symbol: str,
    timezone_name: str,
) -> tuple[MarketDataBar, ...]:
    from event_trader.market.stock_bar_adapter import parse_futu_rows_to_stock_bars

    return parse_futu_rows_to_stock_bars(
        raw_rows,
        bar_granularity=bar_granularity,
        symbol=symbol,
        timezone_name=timezone_name,
    )


def _futu_code_prefix(futu_code: str) -> str:
    return futu_code.split(".", 1)[0].upper()


def _ensure_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


def _binance_spot_kline_interval(raw_value: str) -> tuple[str, timedelta]:
    normalized = raw_value.strip().lower()
    if normalized.endswith("min") and normalized[:-3].isdigit():
        normalized = f"{normalized[:-3]}m"
    bar_delta = _BINANCE_SPOT_KLINE_INTERVALS.get(normalized)
    if bar_delta is None:
        supported = ", ".join(_BINANCE_SPOT_KLINE_INTERVALS)
        raise ValueError(
            "binance_spot bar_granularity must be one of the official Binance Spot "
            f"K-line intervals: {supported}."
        )
    return normalized, bar_delta


def _hyperliquid_candle_interval(raw_value: str) -> tuple[str, timedelta]:
    normalized = raw_value.strip().lower()
    if normalized.endswith("min") and normalized[:-3].isdigit():
        normalized = f"{normalized[:-3]}m"
    bar_delta = _HYPERLIQUID_CANDLE_INTERVALS.get(normalized)
    if bar_delta is None:
        supported = ", ".join(_HYPERLIQUID_CANDLE_INTERVALS)
        raise ValueError(
            "hyperliquid_perp bar_granularity must be one of the official "
            f"Hyperliquid candle intervals: {supported}."
        )
    return normalized, bar_delta


def _hyperliquid_perp_coin(raw_value: str) -> str:
    normalized = raw_value.strip()
    if not normalized:
        raise ValueError("hyperliquid_perp market_symbol must not be empty.")
    prefix, separator, coin = normalized.partition(":")
    if not separator:
        return normalized.upper()
    if not prefix or not coin:
        raise ValueError(
            "hyperliquid_perp HIP-3 symbols must use the '<dex>:<coin>' form."
        )
    return f"{prefix.lower()}:{coin.upper()}"


def _epoch_milliseconds(value: datetime) -> int:
    normalized = _ensure_utc(value, field_name="timestamp")
    return int(normalized.timestamp() * 1_000)


def _parse_binance_spot_kline(
    row: object,
    *,
    bar_delta: timedelta,
    symbol: str,
    item_index: int,
) -> MarketDataBar:
    if not isinstance(row, list) or len(row) < 6:
        raise ValueError(
            f"Binance Spot K-line {item_index} for symbol {symbol!r} must contain "
            "at least six values."
        )
    open_time = row[0]
    if not isinstance(open_time, int):
        raise ValueError(
            f"Binance Spot K-line {item_index} for symbol {symbol!r} has invalid open time."
        )
    try:
        start_at = datetime.fromtimestamp(open_time / 1_000, tz=UTC)
        return MarketDataBar(
            start_at=start_at,
            end_at=start_at + bar_delta,
            open_price=float(row[1]),
            high_price=float(row[2]),
            low_price=float(row[3]),
            close_price=float(row[4]),
            volume=float(row[5]),
        )
    except (TypeError, ValueError, OverflowError, OSError, ViewStateChangeContractError) as exc:
        raise ValueError(
            f"Binance Spot K-line {item_index} for symbol {symbol!r} is invalid: {exc}"
        ) from exc


def _parse_hyperliquid_candle(
    row: object,
    *,
    bar_delta: timedelta,
    coin: str,
    item_index: int,
) -> MarketDataBar:
    if not isinstance(row, dict):
        raise ValueError(
            f"Hyperliquid candle {item_index} for coin {coin!r} must be a JSON object."
        )
    open_time = row.get("t")
    close_time = row.get("T")
    if not isinstance(open_time, int) or not isinstance(close_time, int):
        raise ValueError(
            f"Hyperliquid candle {item_index} for coin {coin!r} has invalid timestamps."
        )
    try:
        start_at = datetime.fromtimestamp(open_time / 1_000, tz=UTC)
        end_at = datetime.fromtimestamp((close_time + 1) / 1_000, tz=UTC)
        if end_at != start_at + bar_delta:
            raise ValueError("close timestamp does not match the requested interval")
        return MarketDataBar(
            start_at=start_at,
            end_at=end_at,
            open_price=float(row["o"]),
            high_price=float(row["h"]),
            low_price=float(row["l"]),
            close_price=float(row["c"]),
            volume=float(row["v"]),
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        OSError,
        ViewStateChangeContractError,
    ) as exc:
        raise ValueError(
            f"Hyperliquid candle {item_index} for coin {coin!r} is invalid: {exc}"
        ) from exc


def _parse_option_chain_payload(
    payload: dict[str, object],
    *,
    underlying_symbol: str,
) -> tuple[OptionContractSnapshot, ...]:
    raw_rows = _extract_option_rows(payload)
    rows: list[OptionContractSnapshot] = []
    for contract_symbol, raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            continue
        row = _parse_option_snapshot_row(
            raw_row,
            contract_symbol=contract_symbol,
            underlying_symbol=underlying_symbol,
        )
        if row is not None:
            rows.append(row)
    return tuple(rows)


def _extract_option_rows(payload: dict[str, object]) -> tuple[tuple[str | None, object], ...]:
    for key in ("snapshots", "options", "contracts", "quotes"):
        raw_value = payload.get(key)
        if isinstance(raw_value, dict):
            return tuple((str(contract), row) for contract, row in raw_value.items())
        if isinstance(raw_value, list):
            return tuple((None, row) for row in raw_value)
    return ()


def _parse_option_snapshot_row(
    row: dict[str, object],
    *,
    contract_symbol: str | None,
    underlying_symbol: str,
) -> OptionContractSnapshot | None:
    symbol = _optional_string(
        row.get("contract_symbol")
        or row.get("contractSymbol")
        or row.get("symbol")
        or row.get("contract")
        or contract_symbol
    )
    if symbol is None:
        return None
    option_type = _optional_string(
        row.get("option_type") or row.get("optionType") or row.get("type")
    )
    expiry = _optional_string(
        row.get("expiry_date") or row.get("expiryDate") or row.get("expiration_date")
    )
    strike = _optional_float(row.get("strike_price") or row.get("strikePrice") or row.get("strike"))
    parsed_identity = _parse_osi_identity(symbol)
    if option_type is None:
        option_type = parsed_identity[0]
    if expiry is None:
        expiry = parsed_identity[1]
    if strike is None:
        strike = parsed_identity[2]
    if option_type is None or expiry is None or strike is None:
        return None

    latest_trade = row.get("latestTrade")
    latest_quote = row.get("latestQuote")
    latest_trade_dict = latest_trade if isinstance(latest_trade, dict) else {}
    latest_quote_dict = latest_quote if isinstance(latest_quote, dict) else {}
    if not latest_quote_dict and any(key in row for key in ("bp", "ap", "bs", "as")):
        latest_quote_dict = row
    latest_trade_at = _optional_datetime(latest_trade_dict.get("t"))
    latest_quote_at = _optional_datetime(latest_quote_dict.get("t"))
    observed_at = _latest_datetime(
        _optional_datetime(row.get("t") or row.get("updated_at") or row.get("updatedAt")),
        latest_trade_at,
        latest_quote_at,
    )
    greeks = row.get("greeks")
    greeks_dict = greeks if isinstance(greeks, dict) else {}
    latest_trade_price = _optional_float(latest_trade_dict.get("p"))
    latest_trade_size = _optional_float(latest_trade_dict.get("s"))
    return OptionContractSnapshot(
        contract_symbol=symbol,
        underlying_symbol=underlying_symbol,
        option_type=option_type,
        expiry_date=expiry,
        strike_price=strike,
        observed_at=observed_at,
        volume=_optional_float(row.get("volume") or row.get("v")),
        trade_count=_optional_int(row.get("trade_count") or row.get("tradeCount") or row.get("n")),
        latest_trade_price=latest_trade_price,
        latest_trade_size=latest_trade_size,
        latest_trade_at=latest_trade_at,
        latest_quote_at=latest_quote_at,
        bid_price=_optional_float(latest_quote_dict.get("bp")),
        ask_price=_optional_float(latest_quote_dict.get("ap")),
        bid_size=_optional_float(latest_quote_dict.get("bs")),
        ask_size=_optional_float(latest_quote_dict.get("as")),
        implied_volatility=_optional_float(
            row.get("impliedVolatility") or row.get("implied_volatility")
        ),
        delta=_optional_float(greeks_dict.get("delta")),
        gamma=_optional_float(greeks_dict.get("gamma")),
        rho=_optional_float(greeks_dict.get("rho")),
        theta=_optional_float(greeks_dict.get("theta")),
        vega=_optional_float(greeks_dict.get("vega")),
        open_interest=_optional_float(row.get("openInterest") or row.get("open_interest")),
    )


def _metadata_only_contract(row: OptionContractSnapshot) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        contract_symbol=row.contract_symbol,
        underlying_symbol=row.underlying_symbol,
        option_type=row.option_type,
        expiry_date=row.expiry_date,
        strike_price=row.strike_price,
        observed_at=None,
    )


def _chunk_window(
    start_at: datetime,
    end_at: datetime,
    chunk_delta: timedelta,
) -> tuple[tuple[datetime, datetime], ...]:
    windows: list[tuple[datetime, datetime]] = []
    cursor = start_at
    while cursor < end_at:
        chunk_end = min(cursor + chunk_delta, end_at)
        windows.append((cursor, chunk_end))
        cursor = chunk_end
    return tuple(windows)


def _combine_stock_bar_reads(
    left: _StockBarChunkRead,
    right: _StockBarChunkRead,
    *,
    retry_count: int,
) -> _StockBarChunkRead:
    return _StockBarChunkRead(
        bars=left.bars + right.bars,
        successful_chunk_count=(
            left.successful_chunk_count + right.successful_chunk_count
        ),
        empty_chunk_count=left.empty_chunk_count + right.empty_chunk_count,
        failed_chunks=left.failed_chunks + right.failed_chunks,
        retry_count=retry_count + left.retry_count + right.retry_count,
    )


def _is_empty_bar_error(error: Exception) -> bool:
    message = _exception_message(error).lower()
    return "empty bars" in message or "missing symbol" in message


def _is_recoverable_stock_bar_error(error: Exception) -> bool:
    message = _exception_message(error).lower()
    if "market data request failed" not in message:
        return False
    recoverable_fragments = (
        "remote end closed connection without response",
        "timed out",
        "timeout",
        "urlopen error",
        "connection reset",
        "temporarily unavailable",
    )
    return any(fragment in message for fragment in recoverable_fragments)


def _failed_chunk_summary(
    *,
    start_at: datetime,
    end_at: datetime,
    error: Exception,
) -> dict[str, str]:
    return {
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "error_type": type(error.__cause__ or error).__name__,
        "message": _provider_error_summary(error),
    }


def _provider_error_summary(error: Exception) -> str:
    return _exception_message(error).replace("\n", " ")[:240]


def _exception_message(error: Exception) -> str:
    parts = [str(error)]
    cause = error.__cause__
    if cause is not None:
        parts.append(str(cause))
    return " ".join(part for part in parts if part)


def _parse_option_trades_payload(
    payload: dict[str, object],
    *,
    contract_lookup: dict[str, OptionContractSnapshot],
) -> tuple[OptionTradeObservation, ...]:
    rows: list[OptionTradeObservation] = []
    for contract_symbol, raw_rows in _extract_contract_table(payload, "trades"):
        contract = contract_lookup.get(contract_symbol)
        if contract is None:
            continue
        row_values = raw_rows if isinstance(raw_rows, list) else (raw_rows,)
        for raw_row in row_values:
            if not isinstance(raw_row, dict):
                continue
            observed_at = _optional_datetime(raw_row.get("t"))
            price = _optional_float(raw_row.get("p"))
            size = _optional_float(raw_row.get("s"))
            if observed_at is None or price is None or size is None:
                continue
            rows.append(
                OptionTradeObservation(
                    contract_symbol=contract.contract_symbol,
                    option_type=contract.option_type,
                    expiry_date=contract.expiry_date,
                    strike_price=contract.strike_price,
                    observed_at=observed_at,
                    price=price,
                    size=size,
                    notional=price * size * 100.0,
                )
            )
    return tuple(rows)


def _parse_option_bars_payload(
    payload: dict[str, object],
    *,
    contract_lookup: dict[str, OptionContractSnapshot],
    bar_granularity: str,
) -> tuple[OptionBarObservation, ...]:
    rows: list[OptionBarObservation] = []
    for contract_symbol, raw_rows in _extract_contract_table(payload, "bars"):
        contract = contract_lookup.get(contract_symbol)
        if contract is None or not isinstance(raw_rows, list):
            continue
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                continue
            start_at = _optional_datetime(raw_row.get("t"))
            close_price = _optional_float(raw_row.get("c"))
            volume = _optional_float(raw_row.get("v"))
            if start_at is None or close_price is None or volume is None:
                continue
            rows.append(
                OptionBarObservation(
                    contract_symbol=contract.contract_symbol,
                    option_type=contract.option_type,
                    expiry_date=contract.expiry_date,
                    strike_price=contract.strike_price,
                    start_at=start_at,
                    end_at=start_at + _parse_option_bar_delta(bar_granularity),
                    close_price=close_price,
                    volume=volume,
                    trade_count=_optional_int(raw_row.get("n")),
                    vwap=_optional_float(raw_row.get("vw")),
                )
            )
    return tuple(rows)


def _extract_contract_table(
    payload: dict[str, object],
    field_name: str,
) -> tuple[tuple[str, object], ...]:
    raw_table = payload.get(field_name)
    if isinstance(raw_table, dict):
        return tuple((str(contract_symbol), rows) for contract_symbol, rows in raw_table.items())
    if isinstance(raw_table, list):
        grouped: dict[str, list[object]] = {}
        for raw_row in raw_table:
            if not isinstance(raw_row, dict):
                continue
            contract_symbol = _optional_string(
                raw_row.get("contract")
                or raw_row.get("contract_symbol")
                or raw_row.get("contractSymbol")
                or raw_row.get("symbol")
            )
            if contract_symbol is None:
                continue
            grouped.setdefault(contract_symbol, []).append(raw_row)
        return tuple(sorted(grouped.items()))
    return ()


def _option_observed_at(row: OptionContractSnapshot) -> datetime:
    values = [
        value
        for value in (row.observed_at, row.latest_trade_at, row.latest_quote_at)
        if value is not None
    ]
    if values:
        return max(values)
    return datetime.max.replace(tzinfo=UTC)


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, str | bytes | int | float):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str | bytes | int):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(_normalize_iso_timestamp(value.strip()))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _normalize_iso_timestamp(value: str) -> str:
    normalized = value.replace("Z", "+00:00")
    dot_index = normalized.find(".")
    if dot_index < 0:
        return normalized
    tz_index = len(normalized)
    for marker in ("+", "-"):
        marker_index = normalized.find(marker, dot_index)
        if marker_index > dot_index:
            tz_index = marker_index
            break
    fractional = normalized[dot_index + 1 : tz_index]
    if len(fractional) <= 6:
        return normalized
    return f"{normalized[:dot_index + 1]}{fractional[:6]}{normalized[tz_index:]}"


def _latest_datetime(*values: datetime | None) -> datetime | None:
    normalized = tuple(value for value in values if value is not None)
    return max(normalized) if normalized else None


def _parse_osi_identity(symbol: str) -> tuple[str | None, str | None, float | None]:
    if len(symbol) < 15:
        return None, None, None
    type_marker = symbol[-9:-8]
    if type_marker not in {"C", "P"}:
        return None, None, None
    raw_date = symbol[-15:-9]
    raw_strike = symbol[-8:]
    try:
        expiry = f"20{raw_date[0:2]}-{raw_date[2:4]}-{raw_date[4:6]}"
        strike = int(raw_strike) / 1000.0
    except ValueError:
        return None, None, None
    return ("call" if type_marker == "C" else "put"), expiry, strike


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _translate_bc_private_interval(raw_value: str) -> str:
    normalized = raw_value.strip().lower()
    if normalized.endswith("min"):
        return f"{int(normalized[:-3])}Minute"
    unit = normalized[-1]
    count = int(normalized[:-1])
    if unit == "m":
        return f"{count}Minute"
    if unit == "h":
        return f"{count}Hour"
    if unit == "d":
        return f"{count}Day"
    raise ValueError("unsupported options bar granularity.")


def _parse_option_bar_delta(raw_value: str):
    from datetime import timedelta

    normalized = raw_value.strip().lower()
    if normalized.endswith("min"):
        return timedelta(minutes=int(normalized[:-3]))
    count = int(normalized[:-1])
    unit = normalized[-1]
    if unit == "m":
        return timedelta(minutes=count)
    if unit == "h":
        return timedelta(hours=count)
    return timedelta(days=count)


__all__ = [
    "BcPrivateMarketDataProvider",
    "BinanceSpotMarketDataProvider",
    "FutuOpenApiMarketDataProvider",
    "HyperliquidPerpMarketDataProvider",
    "MarketBarsProvider",
    "MarketDataProvider",
    "MarketOptionsProvider",
    "build_market_bars_provider",
    "build_default_market_bars_provider",
    "market_mapping_from_profile",
]
