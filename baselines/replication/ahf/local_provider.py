"""Frozen TSLA provider for the isolated AHF AMA-protocol reproduction."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def _utc_end(day: str) -> datetime:
    return datetime.fromisoformat(f"{pd.Timestamp(day).date()}T23:59:59+00:00")


class TSLALocalDataProvider:
    def __init__(self, *, price_path: Path, news_path: Path, price_visible_through: datetime, news_visible_through: datetime) -> None:
        self.price_path = Path(price_path)
        self.news_path = Path(news_path)
        self.price_visible_through = price_visible_through.astimezone(timezone.utc)
        self.news_visible_through = news_visible_through.astimezone(timezone.utc)
        self._prices: pd.DataFrame | None = None
        self._news: list[dict] | None = None

    @staticmethod
    def _ticker(ticker: str) -> None:
        if ticker.strip().upper() != "TSLA":
            raise ValueError(f"This reproduction only supports TSLA, got {ticker!r}")

    def _load_prices(self) -> pd.DataFrame:
        if self._prices is None:
            raw = pd.read_csv(self.price_path, encoding="utf-8")
            self._prices = pd.DataFrame(
                {
                    "date": pd.to_datetime(raw["time_key"], errors="raise").dt.normalize(),
                    "open": pd.to_numeric(raw["open"], errors="raise"),
                    "high": pd.to_numeric(raw["high"], errors="raise"),
                    "low": pd.to_numeric(raw["low"], errors="raise"),
                    "close": pd.to_numeric(raw["close"], errors="raise"),
                    "volume": pd.to_numeric(raw["volume"], errors="raise").fillna(0).astype(int),
                }
            ).sort_values("date")
        return self._prices.copy()

    def _load_news(self) -> list[dict]:
        if self._news is None:
            if not self.news_path.exists():
                self._news = []
            else:
                self._news = [
                    json.loads(line)
                    for line in self.news_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
        return list(self._news)

    def get_prices(self, ticker: str, start_date: str, end_date: str, api_key: str | None = None):
        del api_key
        self._ticker(ticker)
        from src.data.models import Price

        end = min(pd.Timestamp(end_date).normalize(), pd.Timestamp(self.price_visible_through.date()))
        frame = self._load_prices()
        frame = frame[(frame["date"] >= pd.Timestamp(start_date).normalize()) & (frame["date"] <= end)]
        return [
            Price(
                open=float(row.open), high=float(row.high), low=float(row.low), close=float(row.close),
                volume=int(row.volume), time=f"{row.date:%Y-%m-%d}T00:00:00+00:00",
            )
            for row in frame.itertuples(index=False)
        ]

    def get_company_news(self, ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000, api_key: str | None = None):
        del api_key
        self._ticker(ticker)
        from src.data.models import CompanyNews

        start = datetime.fromisoformat(f"{pd.Timestamp(start_date or end_date).date()}T00:00:00+00:00")
        # The price end date is the prior close; news may include items published
        # before the current decision's fixed pre-market cutoff.
        end = self.news_visible_through
        rows = []
        for item in self._load_news():
            visible = datetime.fromisoformat(str(item["visible_at"]).replace("Z", "+00:00"))
            if start <= visible <= end:
                rows.append(item)
        rows.sort(key=lambda item: str(item["visible_at"]), reverse=True)
        return [
            CompanyNews(
                ticker="TSLA", title=str(item["title"]), source=str(item.get("source") or "archive"),
                date=str(item.get("published_at") or item["visible_at"]), url=str(item["url"]),
            )
            for item in rows[:limit]
        ]

    def get_financial_metrics(self, *args, **kwargs):
        return []

    def get_insider_trades(self, *args, **kwargs):
        return []

    def get_market_cap(self, *args, **kwargs):
        return None
