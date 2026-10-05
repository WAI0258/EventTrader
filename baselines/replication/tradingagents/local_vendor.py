"""Frozen Futu-price and historical-news vendor for the 2024Q1 reproduction."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from stockstats import wrap

TICKERS = {"AAPL", "GOOGL", "AMZN", "TSLA"}

INDICATORS = {
    "close_50_sma": "50-day simple moving average.",
    "close_200_sma": "200-day simple moving average.",
    "close_10_ema": "10-day exponential moving average.",
    "macd": "MACD line.",
    "macds": "MACD signal line.",
    "macdh": "MACD histogram.",
    "rsi": "Relative strength index.",
    "boll": "Bollinger middle band.",
    "boll_ub": "Bollinger upper band.",
    "boll_lb": "Bollinger lower band.",
    "atr": "Average true range.",
    "vwma": "Volume-weighted moving average.",
}


def _config() -> dict:
    from tradingagents.dataflows.config import get_config

    return get_config()


def _root() -> Path:
    raw = _config().get("paper_repro_data_root")
    if not raw:
        raise ValueError("paper_repro_data_root is not configured")
    return Path(raw)


def _ticker(symbol: str) -> str:
    ticker = str(symbol).strip().upper()
    if ticker not in TICKERS:
        raise ValueError(f"Frozen paper vendor does not cover {symbol!r}")
    return ticker


def _cutoff(value: str) -> pd.Timestamp:
    requested = pd.to_datetime(value, errors="raise").normalize()
    visible = _config().get("baseline_visible_through_date")
    if visible:
        requested = min(requested, pd.to_datetime(visible, errors="raise").normalize())
    return requested


def _news_time(value: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC").tz_localize(None)
    return stamp


def _news_cutoff(value: str) -> pd.Timestamp:
    requested = _news_time(value)
    timestamp_visible = _config().get("baseline_news_visible_through")
    if timestamp_visible:
        return min(requested, _news_time(timestamp_visible))
    date_visible = _config().get("baseline_visible_through_date")
    if date_visible:
        end_of_visible_day = _news_time(date_visible).normalize() + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
        return min(requested, end_of_visible_day)
    return requested


def _price_path(ticker: str) -> Path:
    configured = _config().get("paper_repro_price_path")
    if configured:
        return Path(configured)
    return _root() / "price" / f"{ticker.lower()}_daily_futu_qfq_warmup_20230101_20240329.csv"


def load_ohlcv(symbol: str, curr_date: str | None = None) -> pd.DataFrame:
    ticker = _ticker(symbol)
    path = _price_path(ticker)
    raw = pd.read_csv(path, encoding="utf-8")
    columns = {name.lower(): name for name in raw.columns}
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(raw[columns["time_key"]], errors="coerce"),
            "Open": pd.to_numeric(raw[columns["open"]], errors="coerce"),
            "High": pd.to_numeric(raw[columns["high"]], errors="coerce"),
            "Low": pd.to_numeric(raw[columns["low"]], errors="coerce"),
            "Close": pd.to_numeric(raw[columns["close"]], errors="coerce"),
            "Volume": pd.to_numeric(raw[columns["volume"]], errors="coerce"),
        }
    ).dropna(subset=["Date", "Close"])
    frame = frame.sort_values("Date").reset_index(drop=True)
    if curr_date:
        frame = frame[frame["Date"] <= _cutoff(curr_date)].reset_index(drop=True)
    if frame.empty:
        raise ValueError(f"No frozen OHLCV for {ticker} on or before {curr_date}")
    return frame


def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    ticker = _ticker(symbol)
    start = pd.to_datetime(start_date, errors="raise")
    end = _cutoff(end_date)
    frame = load_ohlcv(ticker)
    frame = frame[(frame["Date"] >= start) & (frame["Date"] <= end)].copy()
    frame["Date"] = frame["Date"].dt.strftime("%Y-%m-%d")
    return (
        f"# Frozen Futu QFQ daily OHLCV for {ticker}; rows after {end:%Y-%m-%d} excluded\n\n"
        + frame.to_csv(index=False)
    )


def get_indicator(symbol: str, indicator: str, curr_date: str, look_back_days: int = 30) -> str:
    ticker = _ticker(symbol)
    name = indicator.strip().lower()
    if name not in INDICATORS:
        raise ValueError(f"Unsupported indicator {indicator!r}; choose from {sorted(INDICATORS)}")
    end = _cutoff(curr_date)
    start = end - pd.Timedelta(days=int(look_back_days))
    stock = wrap(load_ohlcv(ticker, curr_date).copy())
    stock[name]
    dates = pd.to_datetime(stock["Date"], errors="coerce")
    lines = []
    for date, value in zip(dates, stock[name]):
        if date < start:
            continue
        rendered = "N/A" if pd.isna(value) else str(value)
        lines.append(f"{date:%Y-%m-%d}: {rendered}")
    return f"## {name} for {ticker}\n\n" + "\n".join(lines) + "\n\n" + INDICATORS[name]


def _news_rows(ticker: str | None = None) -> list[dict]:
    configured = _config().get("paper_repro_news_path")
    if configured:
        with Path(configured).open("r", encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        return [
            {
                "ticker": item.get("ticker", "GLOBAL"),
                "title": item.get("title"),
                "content": item.get("summary") or item.get("title") or "",
                "timestamp": item.get("visible_at") or item.get("published_at"),
            }
            for item in rows
            if ticker is None or str(item.get("ticker", "")).upper() == ticker
        ]
    name = (
        f"{ticker.lower()}_news_20240101_20240329.jsonl"
        if ticker
        else "combined_news_20240101_20240329.jsonl"
    )
    path = _root() / "news" / "paper_window" / name
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _render_news(rows: list[dict], limit: int) -> str:
    rows = sorted(rows, key=lambda item: item["timestamp"], reverse=True)[:limit]
    if not rows:
        return "NO_DATA_AVAILABLE: no frozen news in the requested window."
    parts = []
    for item in rows:
        content = " ".join(str(item.get("content") or "").split())
        parts.append(
            f"### {item.get('title') or 'Untitled'}\n"
            f"- ticker: {item.get('ticker', 'GLOBAL')}\n"
            f"- published_at: {item['timestamp']} (source timezone undocumented)\n"
            f"- content: {content[:3000]}"
        )
    return "\n\n".join(parts)


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    symbol = _ticker(ticker)
    start = _news_time(start_date)
    end = _news_cutoff(end_date)
    rows = [
        item
        for item in _news_rows(symbol)
        if start <= _news_time(item["timestamp"]) <= end
    ]
    return _render_news(rows, int(_config().get("news_article_limit", 20)))


def get_global_news(curr_date: str, look_back_days: int = 7, limit: int = 50) -> str:
    end = _news_cutoff(curr_date)
    effective_look_back = 7 if look_back_days is None else int(look_back_days)
    effective_limit = (
        _config().get("global_news_article_limit", 50)
        if limit is None
        else limit
    )
    start = end - timedelta(days=effective_look_back)
    rows = [
        item
        for item in _news_rows()
        if start <= _news_time(item["timestamp"]) <= end
    ]
    return _render_news(rows, int(effective_limit))


def get_insider_transactions(symbol: str) -> str:
    _ticker(symbol)
    return "DATA_UNAVAILABLE: the frozen reproduction bundle contains no insider transactions."


def disabled_optional(*_args, **_kwargs) -> str:
    return "DATA_UNAVAILABLE: disabled in the frozen paper-window reproduction."


def register_vendor() -> None:
    """Register the isolated vendor without modifying the upstream package."""
    from tradingagents.dataflows import interface
    from tradingagents.dataflows import market_data_validator
    import tradingagents.graph.trading_graph as trading_graph

    vendor = "paper_repro_local"
    if vendor not in interface.VENDOR_LIST:
        interface.VENDOR_LIST.append(vendor)
    interface.VENDOR_METHODS["get_stock_data"][vendor] = get_stock_data
    interface.VENDOR_METHODS["get_indicators"][vendor] = get_indicator
    interface.VENDOR_METHODS["get_news"][vendor] = get_news
    interface.VENDOR_METHODS["get_global_news"][vendor] = get_global_news
    interface.VENDOR_METHODS["get_insider_transactions"][vendor] = get_insider_transactions
    market_data_validator.load_ohlcv = load_ohlcv

    identities = {
        "AAPL": {"name": "Apple Inc.", "quoteType": "EQUITY", "exchange": "NASDAQ"},
        "GOOGL": {"name": "Alphabet Inc.", "quoteType": "EQUITY", "exchange": "NASDAQ"},
        "AMZN": {"name": "Amazon.com, Inc.", "quoteType": "EQUITY", "exchange": "NASDAQ"},
        "TSLA": {"name": "Tesla, Inc.", "quoteType": "EQUITY", "exchange": "NASDAQ"},
    }
    trading_graph.resolve_instrument_identity = lambda ticker: identities[_ticker(ticker)]
