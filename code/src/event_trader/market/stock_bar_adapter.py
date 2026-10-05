"""Shared stock-bar parsing and protocol helpers for market providers."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from event_trader.contracts.view_state_change import (
    MarketDataBar,
    ViewStateChangeContractError,
    MarketMapping,
)

_GRANULARITY_RE = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<unit>m|min|h|d)$", re.IGNORECASE)


def validate_stock_bar_capability(*, provider: str, mapping: MarketMapping) -> None:
    """Validate bar-specific session/scope constraints for a provider."""
    if provider == "bc_private_v1":
        if (
            mapping.market_session != "exchange_session"
            or mapping.exchange_session_scope != "extended"
        ):
            raise ValueError(
                "bc_private_v1 stock bars only support "
                "market_session='exchange_session' with "
                "exchange_session_scope='extended'."
            )
        return
    if provider == "alpaca_like" and mapping.market_session == "exchange_session":
        raise ValueError(
            "alpaca_like market data does not declare support for explicit "
            "exchange-session scopes."
        )


def parse_stock_bar_granularity(raw_value: str, *, allow_minute_suffix: bool = True) -> timedelta:
    """Parse stock-bar granularity strings into timedelta."""
    if not isinstance(raw_value, str):
        raise ValueError("bar_granularity must be a string.")
    normalized = raw_value.strip()
    match = _GRANULARITY_RE.fullmatch(normalized)
    if match is None and allow_minute_suffix:
        normalized = normalized.strip()
        if normalized.endswith("min"):
            normalized = f"{normalized[:-3]}m"
        match = _GRANULARITY_RE.fullmatch(normalized)
    if match is None:
        raise ValueError(
            f"bar_granularity {raw_value!r} must match '<N>m', '<N>min', '<N>h', or '<N>d'."
        )
    count = int(match.group("count"))
    unit = match.group("unit").lower()
    if unit in {"m", "min"}:
        return timedelta(minutes=count)
    if unit == "h":
        return timedelta(hours=count)
    return timedelta(days=count)


def build_stock_bar_request_headers(*, provider: str, api_key: str) -> dict[str, str]:
    """Build HTTP headers for stock-bar validation endpoints."""
    if provider == "alpaca_like":
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
    if provider == "bc_private_v1":
        return {
            "Accept": "application/json",
            "X-API-KEY": api_key,
        }
    raise ValueError(f"Unsupported market-data provider: {provider!r}.")


def build_stock_bar_request_query(
    *,
    provider: str,
    symbol: str,
    bar_granularity: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[str, str]:
    """Build provider-specific query params for stock-bar pages."""
    if provider == "alpaca_like":
        return {
            "symbols": symbol,
            "timeframe": bar_granularity,
            "start": _format_utc_timestamp(start_at),
            "end": _format_utc_timestamp(end_at),
        }
    if provider == "bc_private_v1":
        return {
            "tickers": symbol,
            "interval": translate_bc_private_bar_interval(bar_granularity),
            "start_time": _format_utc_timestamp(start_at),
            "end_time": _format_utc_timestamp(end_at),
        }
    raise ValueError(f"Unsupported market-data provider: {provider!r}.")


def extract_stock_symbol_bars(payload: dict[str, object], *, symbol: str) -> list[object]:
    """Extract the provider payload bar list for the requested market symbol."""
    raw_bars_table = payload.get("bars")
    if not isinstance(raw_bars_table, dict):
        raise ValueError("market data response field 'bars' must be an object.")
    if symbol not in raw_bars_table:
        raise ValueError(f"missing symbol {symbol!r} in market data response bars.")
    raw_symbol_bars = raw_bars_table[symbol]
    if not isinstance(raw_symbol_bars, list):
        raise ValueError(f"response bars for symbol {symbol!r} must be a JSON array.")
    return raw_symbol_bars


def parse_stock_bar_row(
    raw_bar: object,
    *,
    bar_granularity: timedelta,
    symbol: str,
    item_index: int,
) -> MarketDataBar:
    """Parse one HTTP stock-bar row into canonical OHLCV format."""
    if not isinstance(raw_bar, dict):
        raise ValueError(f"bar {item_index} for symbol {symbol!r} must be a JSON object.")
    required_fields = ("o", "h", "l", "c", "t", "v")
    missing_fields = [field_name for field_name in required_fields if field_name not in raw_bar]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise ValueError(f"bar {item_index} for symbol {symbol!r} is missing fields: {missing}.")
    start_at = parse_utc_timestamp(
        raw_bar["t"],
        field_name=f"bars[{item_index}].t",
    )
    try:
        vwap = None
        if "vw" in raw_bar:
            vwap = float(raw_bar["vw"])
        return MarketDataBar(
            start_at=start_at,
            end_at=start_at + bar_granularity,
            open_price=float(raw_bar["o"]),
            high_price=float(raw_bar["h"]),
            low_price=float(raw_bar["l"]),
            close_price=float(raw_bar["c"]),
            volume=float(raw_bar["v"]),
            vwap=vwap,
        )
    except (TypeError, ValueError, ViewStateChangeContractError) as exc:
        raise ValueError(f"bar {item_index} for symbol {symbol!r} is invalid: {exc}") from exc


def parse_futu_rows_to_stock_bars(
    raw_rows: object,
    *,
    bar_granularity: str,
    symbol: str,
    timezone_name: str = "America/New_York",
) -> tuple[MarketDataBar, ...]:
    """Parse Futu-style rows into canonical stock bars."""
    rows = parse_futu_records(raw_rows)
    bar_delta = parse_stock_bar_granularity(bar_granularity, allow_minute_suffix=False)
    bars: list[MarketDataBar] = []
    previous_end_at: datetime | None = None
    for index, row in enumerate(rows):
        if row.get("is_blank") is True:
            continue
        try:
            end_at = parse_futu_time_key(row["time_key"], timezone_name=timezone_name)
            start_at = (
                end_at - bar_delta
                if previous_end_at is None or end_at - previous_end_at > bar_delta
                else previous_end_at
            )
            bars.append(
                MarketDataBar(
                    start_at=start_at,
                    end_at=end_at,
                    open_price=_required_float(row, "open"),
                    high_price=_required_float(row, "high"),
                    low_price=_required_float(row, "low"),
                    close_price=_required_float(row, "close"),
                    volume=_required_float(row, "volume"),
                    vwap=None,
                )
            )
            previous_end_at = end_at
        except (KeyError, TypeError, ValueError, ViewStateChangeContractError) as exc:
            raise ValueError(f"invalid Futu bar {index} for symbol {symbol!r}: {exc}") from exc
    return tuple(bars)


def parse_futu_records(raw_rows: object) -> tuple[dict[str, object], ...]:
    """Normalize dataframe-like or list-based Futu rows into dict records."""
    to_dict = getattr(raw_rows, "to_dict", None)
    if callable(to_dict):
        records = to_dict("records")
    else:
        records = raw_rows
    if not isinstance(records, list):
        raise ValueError("Futu K-line data must be dataframe-like or a list of records.")
    normalized: list[dict[str, object]] = []
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("Futu K-line records must be objects.")
        normalized.append(row)
    return tuple(normalized)


def parse_futu_time_key(
    raw_value: object,
    *,
    timezone_name: str = "America/New_York",
) -> datetime:
    """Parse Futu 'YYYY-MM-DD HH:MM:SS' end-anchor timestamps."""
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError("time_key must be a non-empty string.")
    try:
        naive = datetime.strptime(raw_value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError("time_key must match YYYY-MM-DD HH:MM:SS.") from exc
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unsupported Futu timezone: {timezone_name!r}.") from exc
    return naive.replace(tzinfo=timezone).astimezone(UTC)


def parse_utc_timestamp(raw_value: object, *, field_name: str) -> datetime:
    """Parse an ISO8601 timestamp and enforce UTC."""
    if not isinstance(raw_value, str):
        raise ValueError(f"{field_name} must be an ISO8601 UTC string.")
    normalized = raw_value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty.")
    iso_value = normalized.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information.")
    if parsed.utcoffset() != UTC.utcoffset(None):
        raise ValueError(f"{field_name} must be UTC.")
    return parsed.astimezone(UTC)


def translate_bc_private_bar_interval(raw_value: str) -> str:
    normalized = raw_value.strip().lower()
    if not normalized:
        raise ValueError(
            "bc_private_v1 interval must match '<N>m', '<N>min', '<N>h', or '<N>d'."
        )
    match = _GRANULARITY_RE.fullmatch(normalized)
    if match is None:
        raise ValueError(
            "bc_private_v1 interval must match '<N>m', '<N>min', '<N>h', or '<N>d'."
        )
    count = int(match.group("count"))
    unit = match.group("unit").lower()
    if unit in {"m", "min"}:
        return f"{count}Minute"
    if unit == "h":
        return f"{count}Hour"
    return f"{count}Day"


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _required_float(row: dict[str, object], field_name: str) -> float:
    value = row[field_name]
    if not isinstance(value, str | bytes | int | float):
        raise ValueError(f"{field_name} must be numeric.")
    return float(value)


__all__ = [
    "build_stock_bar_request_headers",
    "build_stock_bar_request_query",
    "extract_stock_symbol_bars",
    "parse_futu_records",
    "parse_futu_rows_to_stock_bars",
    "parse_futu_time_key",
    "parse_stock_bar_granularity",
    "parse_stock_bar_row",
    "parse_utc_timestamp",
    "translate_bc_private_bar_interval",
    "validate_stock_bar_capability",
]
