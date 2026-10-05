from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd

from src.data.models import CompanyNews, FinancialMetrics, InsiderTrade, Price


GCUSD_ALIASES = {"GCUSD", "GC=F", "XAUUSD", "XAU", "GOLD"}


def is_gcusd_symbol(ticker: str) -> bool:
    return isinstance(ticker, str) and ticker.strip().upper() in GCUSD_ALIASES


def _require_gcusd(ticker: str) -> None:
    if not is_gcusd_symbol(ticker):
        raise ValueError(f"GCUSD local provider only supports GCUSD/gold aliases, got {ticker!r}")


def _parse_iso_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_daily_ohlcv(raw: pd.DataFrame) -> pd.DataFrame:
    lower_to_actual = {column.lower(): column for column in raw.columns}
    required = ("date", "open", "high", "low", "close", "volume")
    missing = [column for column in required if column not in lower_to_actual]
    if missing:
        raise ValueError(f"GCUSD daily OHLCV file is missing required columns: {missing}")

    normalized = pd.DataFrame(
        {
            "date": pd.to_datetime(raw[lower_to_actual["date"]], errors="coerce"),
            "open": pd.to_numeric(raw[lower_to_actual["open"]], errors="coerce"),
            "high": pd.to_numeric(raw[lower_to_actual["high"]], errors="coerce"),
            "low": pd.to_numeric(raw[lower_to_actual["low"]], errors="coerce"),
            "close": pd.to_numeric(raw[lower_to_actual["close"]], errors="coerce"),
            "volume": pd.to_numeric(raw[lower_to_actual["volume"]], errors="coerce").fillna(0).astype(int),
        }
    )
    normalized = normalized.dropna(subset=["date", "open", "high", "low", "close"])
    return normalized.sort_values("date").reset_index(drop=True)


class GCUSDLocalDataProvider:
    def __init__(
        self,
        *,
        daily_ohlcv_path: Path,
        news_archive_dir: Path,
        visible_through: datetime,
    ) -> None:
        self.daily_ohlcv_path = Path(daily_ohlcv_path)
        self.news_archive_dir = Path(news_archive_dir)
        self.visible_through = visible_through.astimezone(timezone.utc)
        self._daily_cache: pd.DataFrame | None = None

    def load_daily_ohlcv(self) -> pd.DataFrame:
        if self._daily_cache is None:
            if not self.daily_ohlcv_path.exists():
                raise FileNotFoundError(f"GCUSD daily OHLCV file not found: {self.daily_ohlcv_path}")
            self._daily_cache = _normalize_daily_ohlcv(
                pd.read_csv(self.daily_ohlcv_path, encoding="utf-8")
            )
        return self._daily_cache.copy()

    def get_prices(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        api_key: str | None = None,
    ) -> list[Price]:
        del api_key
        _require_gcusd(ticker)
        start_dt = pd.to_datetime(start_date, errors="raise").normalize()
        end_dt = min(
            pd.to_datetime(end_date, errors="raise").normalize(),
            pd.Timestamp(self.visible_through.date()),
        )
        df = self.load_daily_ohlcv()
        df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]
        if df.empty:
            return []
        return [
            Price(
                open=float(row.open),
                close=float(row.close),
                high=float(row.high),
                low=float(row.low),
                volume=int(row.volume),
                time=row.date.strftime("%Y-%m-%dT00:00:00+00:00"),
            )
            for row in df.itertuples(index=False)
        ]

    def _news_day_path(self, day: datetime) -> Path:
        return self.news_archive_dir / f"{day.strftime('%Y-%m-%d')}.jsonl"

    def _iter_raw_news(self, start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        current_day = start_dt.date()
        while current_day <= end_dt.date():
            path = self._news_day_path(
                datetime.combine(current_day, datetime.min.time(), tzinfo=timezone.utc)
            )
            if path.exists():
                for raw_line in path.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    visible_at = _parse_iso_dt(item.get("visible_at"))
                    published_at = _parse_iso_dt(item.get("published_at"))
                    if visible_at is None:
                        continue
                    if visible_at < start_dt or visible_at > end_dt:
                        continue
                    if visible_at > self.visible_through:
                        continue
                    if published_at is not None and published_at > self.visible_through:
                        continue
                    entries.append(item)
            current_day += timedelta(days=1)
        entries.sort(
            key=lambda item: (
                item.get("visible_at") or "",
                item.get("published_at") or "",
                item.get("title") or "",
            ),
            reverse=True,
        )
        return entries

    def collect_news_entries(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in self._iter_raw_news(start_dt, end_dt):
            key = str(item.get("source_ref") or item.get("title") or json.dumps(item, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
            if limit is not None and len(deduped) >= limit:
                break
        return deduped

    def get_company_news(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
        api_key: str | None = None,
    ) -> list[CompanyNews]:
        del api_key
        _require_gcusd(ticker)
        end_dt = datetime.fromisoformat(f"{pd.to_datetime(end_date, errors='raise').date()}T23:59:59+00:00")
        end_dt = min(end_dt, self.visible_through)
        if start_date:
            start_dt = datetime.fromisoformat(
                f"{pd.to_datetime(start_date, errors='raise').date()}T00:00:00+00:00"
            )
        else:
            start_dt = end_dt - timedelta(days=30)

        source_rows = self.collect_news_entries(start_dt=start_dt, end_dt=end_dt, limit=limit)
        news_items: list[CompanyNews] = []
        for item in source_rows:
            source_ref = str(item.get("source_ref") or "")
            hostname = urlparse(source_ref).netloc or "archive"
            published_at = item.get("published_at") or item.get("visible_at") or end_dt.isoformat()
            news_items.append(
                CompanyNews(
                    ticker="GCUSD",
                    title=str(item.get("title") or "Untitled"),
                    author=None,
                    source=hostname,
                    date=str(published_at),
                    url=source_ref,
                    sentiment=None,
                )
            )
        return news_items

    def get_financial_metrics(
        self,
        ticker: str,
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
        api_key: str | None = None,
    ) -> list[FinancialMetrics]:
        del ticker, end_date, period, limit, api_key
        return []

    def get_insider_trades(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
        api_key: str | None = None,
    ) -> list[InsiderTrade]:
        del ticker, end_date, start_date, limit, api_key
        return []

    def get_market_cap(
        self,
        ticker: str,
        end_date: str,
        api_key: str | None = None,
    ) -> float | None:
        del ticker, end_date, api_key
        return None
