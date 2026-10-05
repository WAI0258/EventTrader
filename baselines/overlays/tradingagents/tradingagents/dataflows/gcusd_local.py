"""Local GCUSD market/news vendor for baseline experiments.

This adapter is intentionally config-driven so normal TradingAgents stock runs
keep using their configured online vendors. GCUSD baseline runners opt in by
setting ``gcusd_daily_ohlcv_path`` and ``gcusd_news_archive_dir``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
from stockstats import wrap

from .config import get_config
from .errors import NoMarketDataError, VendorNotConfiguredError

GCUSD_ALIASES = {"GCUSD", "GC=F", "XAUUSD", "XAUUSD+", "XAU", "GOLD"}

SUPPORTED_INDICATORS: dict[str, str] = {
    "close_50_sma": "50 SMA: medium-term trend direction and dynamic support/resistance.",
    "close_200_sma": "200 SMA: long-term trend benchmark.",
    "close_10_ema": "10 EMA: short-term trend and momentum response.",
    "macd": "MACD: momentum from fast/slow EMA differences.",
    "macds": "MACD Signal: smoothed MACD line.",
    "macdh": "MACD Histogram: distance between MACD and signal.",
    "rsi": "RSI: momentum and overbought/oversold pressure.",
    "boll": "Bollinger middle band.",
    "boll_ub": "Bollinger upper band.",
    "boll_lb": "Bollinger lower band.",
    "atr": "ATR: recent trading range volatility.",
    "vwma": "VWMA: volume-weighted moving average.",
}


def is_gcusd_symbol(symbol: str) -> bool:
    if not isinstance(symbol, str):
        return False
    return symbol.strip().upper().rstrip("+") in {s.rstrip("+") for s in GCUSD_ALIASES}


def _require_gcusd(symbol: str) -> None:
    if not is_gcusd_symbol(symbol):
        raise NoMarketDataError(symbol, symbol, "local GCUSD vendor only covers GCUSD/gold aliases")


def _configured_path(key: str) -> Path:
    raw = get_config().get(key)
    if not raw:
        raise VendorNotConfiguredError(f"{key} is not configured for local GCUSD baseline data.")
    return Path(str(raw)).expanduser()


def _visibility_cutoff_date() -> pd.Timestamp | None:
    raw = get_config().get("baseline_visible_through_date")
    if not raw:
        return None
    cutoff = pd.to_datetime(str(raw), errors="coerce")
    if pd.isna(cutoff):
        return None
    return cutoff.normalize()


def _clamp_to_visibility(value: pd.Timestamp) -> pd.Timestamp:
    cutoff = _visibility_cutoff_date()
    if cutoff is not None and value > cutoff:
        return cutoff
    return value


def _normalize_ohlcv_columns(raw: pd.DataFrame) -> pd.DataFrame:
    lower_to_actual = {c.lower(): c for c in raw.columns}
    required = {
        "date": "Date",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    missing = [name for name in required if name not in lower_to_actual]
    if missing:
        raise ValueError(f"GCUSD OHLCV file is missing required columns: {missing}")

    out = pd.DataFrame()
    for lower_name, canonical in required.items():
        out[canonical] = raw[lower_to_actual[lower_name]]
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
    for col in ("Open", "High", "Low", "Close", "Volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Date", "Close"]).sort_values("Date")
    return out.reset_index(drop=True)


def load_local_ohlcv(symbol: str, curr_date: str | None = None) -> pd.DataFrame:
    """Load local GCUSD daily OHLCV and optionally filter to ``curr_date``."""
    _require_gcusd(symbol)
    path = _configured_path("gcusd_daily_ohlcv_path")
    if not path.exists():
        raise NoMarketDataError(symbol, "GCUSD", f"configured file does not exist: {path}")

    df = _normalize_ohlcv_columns(pd.read_csv(path, encoding="utf-8"))
    if curr_date:
        cutoff = pd.to_datetime(curr_date, errors="coerce")
        if pd.isna(cutoff):
            raise ValueError(f"Invalid curr_date for GCUSD OHLCV: {curr_date!r}")
        cutoff = _clamp_to_visibility(cutoff)
        df = df[df["Date"] <= cutoff]
    if df.empty:
        raise NoMarketDataError(symbol, "GCUSD", f"no rows on or before {curr_date}")
    return df


def get_gcusd_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    _require_gcusd(symbol)
    start = pd.to_datetime(start_date, errors="raise")
    end = _clamp_to_visibility(pd.to_datetime(end_date, errors="raise"))
    df = load_local_ohlcv(symbol)
    df = df[(df["Date"] >= start) & (df["Date"] <= end)]
    if df.empty:
        raise NoMarketDataError(symbol, "GCUSD", f"no rows between {start_date} and {end_date}")

    rendered = df.copy()
    rendered["Date"] = rendered["Date"].dt.strftime("%Y-%m-%d")
    header = (
        f"# Local GCUSD daily OHLCV from {start_date} to {end.strftime('%Y-%m-%d')}\n"
        f"# Source: {get_config().get('gcusd_daily_ohlcv_path')}\n"
        f"# Total records: {len(rendered)}\n\n"
    )
    return header + rendered.to_csv(index=False)


def get_gcusd_indicator(symbol: str, indicator: str, curr_date: str, look_back_days: int = 30) -> str:
    _require_gcusd(symbol)
    indicator = indicator.strip().lower()
    if indicator not in SUPPORTED_INDICATORS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(SUPPORTED_INDICATORS)}"
        )

    curr_dt = _clamp_to_visibility(pd.to_datetime(curr_date, errors="raise"))
    start_dt = curr_dt - pd.Timedelta(days=int(look_back_days))
    df = load_local_ohlcv(symbol, curr_date)
    stock_df = wrap(df.copy())
    stock_df["Date"] = pd.to_datetime(stock_df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    stock_df[indicator]
    values = {
        row["Date"]: ("N/A" if pd.isna(row[indicator]) else str(row[indicator]))
        for _, row in stock_df.iterrows()
    }

    lines = []
    current = curr_dt
    while current >= start_dt:
        date_str = current.strftime("%Y-%m-%d")
        value = values.get(date_str, "N/A: Not a trading day")
        lines.append(f"{date_str}: {value}")
        current -= pd.Timedelta(days=1)

    return (
        f"## {indicator} values from {start_dt.strftime('%Y-%m-%d')} to {curr_dt.strftime('%Y-%m-%d')}:\n\n"
        + "\n".join(lines)
        + "\n\n"
        + SUPPORTED_INDICATORS[indicator]
    )


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _archive_day_path(day: datetime) -> Path:
    return _configured_path("gcusd_news_archive_dir") / f"{day.strftime('%Y-%m-%d')}.jsonl"


def _iter_news_file(path: Path, start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            visible_at = _parse_dt(item.get("visible_at"))
            published_at = _parse_dt(item.get("published_at"))
            if visible_at is None or visible_at > end_dt or visible_at < start_dt:
                continue
            if published_at is not None and published_at < start_dt - timedelta(days=30):
                continue
            entries.append(item)
    return entries


def _iter_news_day_files(root: Path, start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    day = start_dt.date()
    while day <= end_dt.date():
        path = root / f"{day.strftime('%Y-%m-%d')}.jsonl"
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    visible_at = _parse_dt(item.get("visible_at"))
                    published_at = _parse_dt(item.get("published_at"))
                    if visible_at is None or visible_at > end_dt:
                        continue
                    if published_at is not None and published_at < start_dt - timedelta(days=30):
                        continue
                    entries.append(item)
        day += timedelta(days=1)
    return entries


def _iter_news(start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
    archive_path = _configured_path("gcusd_news_archive_dir")
    if archive_path.is_file():
        entries = _iter_news_file(archive_path, start_dt, end_dt)
    else:
        entries = _iter_news_day_files(archive_path, start_dt, end_dt)
    entries.sort(key=lambda x: (x.get("visible_at") or "", x.get("title") or ""))
    return entries


def collect_gcusd_news_entries(
    start_dt: datetime,
    end_dt: datetime,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return deduplicated local-news records for audit/reporting."""
    entries = _iter_news(start_dt.astimezone(timezone.utc), end_dt.astimezone(timezone.utc))
    return _dedupe_news(entries, limit=limit)


def _parse_memory_tags(memory_log_path: Path, ticker: str) -> list[dict[str, Any]]:
    if not memory_log_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    ticker_field = f" | {ticker} |"
    for raw_line in memory_log_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not (line.startswith("[") and line.endswith("]") and ticker_field in line):
            continue
        fields = [part.strip() for part in line[1:-1].split("|")]
        if len(fields) < 4:
            continue
        entries.append(
            {
                "date": fields[0],
                "ticker": fields[1],
                "rating": fields[2],
                "pending": fields[3] == "pending",
                "tag": line,
            }
        )
    return entries


def build_gcusd_visibility_guard(
    config: Mapping[str, Any],
    symbol: str,
    trade_date: str,
    *,
    news_limit: int | None = None,
) -> dict[str, Any]:
    """Validate local GCUSD baseline inputs cannot cross the decision cutoff.

    This is a fail-fast guard for replay baselines. It checks the actual local
    input paths and config that will be passed to TradingAgents, not just the
    default adapter behavior.
    """
    _require_gcusd(symbol)
    cutoff_day = pd.to_datetime(trade_date, errors="raise").normalize()
    cutoff_dt = cutoff_day.to_pydatetime().replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc
    )
    failures: list[str] = []

    visible_through = str(config.get("baseline_visible_through_date") or "")
    if visible_through != trade_date:
        failures.append(
            "baseline_visible_through_date must equal trade_date "
            f"({visible_through!r} != {trade_date!r})"
        )

    expected_data_vendors = {
        "core_stock_apis": "local_gcusd",
        "technical_indicators": "local_gcusd",
        "news_data": "local_gcusd",
        "macro_data": "baseline_disabled",
        "prediction_markets": "baseline_disabled",
    }
    data_vendors = dict(config.get("data_vendors") or {})
    for key, expected in expected_data_vendors.items():
        if data_vendors.get(key) != expected:
            failures.append(f"data_vendors[{key!r}] must be {expected!r}")

    expected_tool_vendors = {
        "get_stock_data": "local_gcusd",
        "get_indicators": "local_gcusd",
        "get_news": "local_gcusd",
        "get_global_news": "local_gcusd",
        "get_insider_transactions": "local_gcusd",
        "get_macro_indicators": "baseline_disabled",
        "get_prediction_markets": "baseline_disabled",
    }
    tool_vendors = dict(config.get("tool_vendors") or {})
    for key, expected in expected_tool_vendors.items():
        if tool_vendors.get(key) != expected:
            failures.append(f"tool_vendors[{key!r}] must be {expected!r}")

    if config.get("disable_deferred_reflection") is not True:
        failures.append("disable_deferred_reflection must be True")
    if config.get("checkpoint_enabled") is not False:
        failures.append("checkpoint_enabled must be False")

    daily_path = Path(str(config.get("gcusd_daily_ohlcv_path") or "")).expanduser()
    max_bar_date = None
    visible_bar_rows = 0
    future_bar_rows_excluded = 0
    if not daily_path.exists():
        failures.append(f"GCUSD daily OHLCV file does not exist: {daily_path}")
    else:
        df = _normalize_ohlcv_columns(pd.read_csv(daily_path, encoding="utf-8"))
        visible_df = df[df["Date"] <= cutoff_day]
        future_bar_rows_excluded = int((df["Date"] > cutoff_day).sum())
        visible_bar_rows = int(len(visible_df))
        if visible_df.empty:
            failures.append(f"no visible GCUSD daily bars on or before {trade_date}")
        else:
            max_bar_ts = visible_df["Date"].max()
            max_bar_date = max_bar_ts.strftime("%Y-%m-%d")
            if max_bar_ts > cutoff_day:
                failures.append(f"max visible bar date {max_bar_date} exceeds {trade_date}")

    news_start_dt = cutoff_dt - timedelta(days=7)
    news_archive = Path(str(config.get("gcusd_news_archive_dir") or "")).expanduser()
    news_records: list[dict[str, Any]] = []
    max_news_visible_at = None
    max_news_published_at = None
    if not news_archive.exists():
        failures.append(f"GCUSD news archive directory does not exist: {news_archive}")
    else:
        previous_config = get_config()
        try:
            from .config import set_config

            set_config({"gcusd_news_archive_dir": str(news_archive)})
            all_entries = _iter_news(news_start_dt, cutoff_dt)
        finally:
            from .config import set_config

            set_config(previous_config)

        for item in all_entries:
            visible_at = _parse_dt(item.get("visible_at"))
            published_at = _parse_dt(item.get("published_at"))
            if visible_at is None:
                failures.append(f"news item missing/invalid visible_at: {item.get('title')!r}")
                continue
            if visible_at > cutoff_dt:
                failures.append(
                    f"news visible_at {visible_at.isoformat()} exceeds cutoff {cutoff_dt.isoformat()}"
                )
            if published_at is not None and published_at > visible_at:
                failures.append(
                    "news published_at exceeds visible_at: "
                    f"{published_at.isoformat()} > {visible_at.isoformat()} "
                    f"for {item.get('title')!r}"
                )
            if max_news_visible_at is None or visible_at > max_news_visible_at:
                max_news_visible_at = visible_at
            if published_at is not None and (
                max_news_published_at is None or published_at > max_news_published_at
            ):
                max_news_published_at = published_at

        news_records = [
            {
                "title": item.get("title"),
                "source_ref": item.get("source_ref"),
                "published_at": item.get("published_at"),
                "visible_at": item.get("visible_at"),
                "labels": item.get("labels") or [],
            }
            for item in _dedupe_news(all_entries, limit=news_limit)
        ]

    memory_path = Path(str(config.get("memory_log_path") or "")).expanduser()
    memory_entries = _parse_memory_tags(memory_path, symbol)
    resolved_memory_entries = [entry for entry in memory_entries if not entry["pending"]]
    future_memory_entries = [
        entry for entry in memory_entries if entry["date"] > trade_date
    ]
    future_resolved_memory_entries = [
        entry for entry in future_memory_entries if not entry["pending"]
    ]
    if resolved_memory_entries:
        failures.append(
            "memory log contains resolved/reflection entries that can leak future outcomes: "
            + ", ".join(entry["tag"] for entry in resolved_memory_entries[:3])
        )
    if future_resolved_memory_entries:
        failures.append(
            "memory log contains future-dated resolved/reflection entries: "
            + ", ".join(entry["tag"] for entry in future_resolved_memory_entries[:3])
        )

    guard = {
        "status": "passed" if not failures else "failed",
        "cutoff": {
            "trade_date": trade_date,
            "visible_through_utc": cutoff_dt.isoformat(),
        },
        "vendors": {
            "data_vendors": {key: data_vendors.get(key) for key in expected_data_vendors},
            "tool_vendors": {key: tool_vendors.get(key) for key in expected_tool_vendors},
        },
        "market_data": {
            "daily_ohlcv": str(daily_path),
            "visible_bar_rows": visible_bar_rows,
            "max_bar_date": max_bar_date,
            "future_bar_rows_excluded": future_bar_rows_excluded,
        },
        "news": {
            "archive": str(news_archive),
            "lookback_start_utc": news_start_dt.isoformat(),
            "record_count_before_limit": len(all_entries) if news_archive.exists() else 0,
            "record_count_after_limit": len(news_records),
            "max_visible_at": max_news_visible_at.isoformat() if max_news_visible_at else None,
            "max_published_at": max_news_published_at.isoformat() if max_news_published_at else None,
            "records_visible": news_records,
        },
        "memory": {
            "memory_log_path": str(memory_path),
            "entry_count": len(memory_entries),
            "pending_entry_count": len([entry for entry in memory_entries if entry["pending"]]),
            "resolved_entry_count": len(resolved_memory_entries),
            "future_entry_count": len(future_memory_entries),
            "future_resolved_entry_count": len(future_resolved_memory_entries),
        },
        "failures": failures,
    }
    if failures:
        raise ValueError("GCUSD baseline input guard failed: " + "; ".join(failures))
    return guard


def _dedupe_news(entries: list[dict[str, Any]], *, limit: int | None) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in entries:
        key = item.get("source_ref") or item.get("title") or json.dumps(item, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if limit is not None and len(deduped) >= limit:
            break
    return deduped


def _render_news(title: str, entries: list[dict[str, Any]], limit: int) -> str:
    if not entries:
        return f"## {title}\n\nNo local GCUSD news found in the requested visible-time window."

    rendered: list[str] = []
    for item in _dedupe_news(entries, limit=limit):
        rendered.append(
            "\n".join(
                [
                    f"### {item.get('title', 'Untitled')}",
                    f"- Source: {item.get('source_ref', 'unknown')}",
                    f"- Published: {item.get('published_at', 'unknown')}",
                    f"- Visible at: {item.get('visible_at', 'unknown')}",
                    f"- Labels: {', '.join(item.get('labels') or []) or 'none'}",
                    "",
                    str(item.get("content") or "").strip(),
                ]
            )
        )
    return f"## {title}\n\n" + "\n\n".join(rendered)


def get_gcusd_news(ticker: str, start_date: str, end_date: str) -> str:
    _require_gcusd(ticker)
    limit = int(get_config().get("news_article_limit", 20))
    start_dt = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end_day = _clamp_to_visibility(pd.to_datetime(end_date, errors="raise")).to_pydatetime()
    end_dt = end_day.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    return _render_news(
        f"Local GCUSD news from {start_date} to {end_dt.strftime('%Y-%m-%d')}",
        _iter_news(start_dt, end_dt),
        limit,
    )


def get_gcusd_global_news(curr_date: str, look_back_days: int | None = None, limit: int | None = None) -> str:
    if look_back_days is None:
        look_back_days = int(get_config().get("global_news_lookback_days", 7))
    if limit is None:
        limit = int(get_config().get("global_news_article_limit", 10))
    end_day = _clamp_to_visibility(pd.to_datetime(curr_date, errors="raise")).to_pydatetime()
    end_dt = end_day.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    start_dt = end_dt - timedelta(days=int(look_back_days))
    return _render_news(
        f"Local GCUSD macro/cross-asset news visible by {end_dt.strftime('%Y-%m-%d')}",
        _iter_news(start_dt, end_dt),
        int(limit),
    )


def get_gcusd_insider_transactions(ticker: str) -> str:
    _require_gcusd(ticker)
    return (
        "DATA_UNAVAILABLE: insider-transaction data is not applicable to GCUSD "
        "gold futures/spot-gold baseline runs. Do not infer corporate insider activity."
    )


def get_disabled_macro_data(indicator: str, curr_date: str, look_back_days: int | None = None) -> str:
    return (
        "DATA_UNAVAILABLE: live FRED macro lookup is disabled for the local GCUSD "
        "baseline. Use the local news archive and market data already visible by "
        f"{curr_date}; do not fabricate macro-series values."
    )


def get_disabled_prediction_markets(topic: str, limit: int | None = None) -> str:
    return (
        "DATA_UNAVAILABLE: live prediction-market lookup is disabled for the local "
        "GCUSD baseline. Use only the provided historical news archive; do not "
        f"fabricate market-implied probabilities for {topic!r}."
    )
