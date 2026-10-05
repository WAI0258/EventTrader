"""Freeze TSLA price and optional historical-news inputs for the AHF AMA protocol run."""

from __future__ import annotations

import argparse
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from futu import AuType, KLType, OpenQuoteContext, RET_OK


HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
DEFAULT_DATA_ROOT = WORKSPACE / "replication" / "data" / "ahf_ama_tsla_2025"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_futu_daily(start: str, end: str) -> pd.DataFrame:
    context = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        pages: list[pd.DataFrame] = []
        page_key = None
        while True:
            ret, data, page_key = context.request_history_kline(
                "US.TSLA",
                start=start,
                end=end,
                ktype=KLType.K_DAY,
                autype=AuType.QFQ,
                max_count=1000,
                page_req_key=page_key,
            )
            if ret != RET_OK:
                raise RuntimeError(f"Futu request_history_kline failed: {data}")
            pages.append(data)
            if page_key is None:
                break
        frame = pd.concat(pages, ignore_index=True)
    finally:
        context.close()
    columns = ["code", "name", "time_key", "open", "high", "low", "close", "volume"]
    return frame.loc[:, columns].drop_duplicates(subset=["time_key"]).sort_values("time_key")


def _gdelt_day(day: date) -> list[dict[str, str]]:
    start = f"{day:%Y%m%d}000000"
    end = f"{day:%Y%m%d}235959"
    response = requests.get(
        "https://api.gdeltproject.org/api/v2/doc/doc",
        params={
            "query": "Tesla",
            "mode": "artlist",
            "format": "json",
            "maxrecords": 250,
            "startdatetime": start,
            "enddatetime": end,
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    rows: list[dict[str, str]] = []
    for article in payload.get("articles", []):
        title = str(article.get("title") or "").strip()
        url = str(article.get("url") or "").strip()
        seen_at = str(article.get("seendate") or "").strip()
        if title and url:
            rows.append(
                {
                    "ticker": "TSLA",
                    "title": title,
                    "url": url,
                    "source": str(article.get("domain") or "gdelt"),
                    "published_at": seen_at,
                    "visible_at": f"{day.isoformat()}T23:59:59+00:00",
                    "source_type": "gdelt_doc_api",
                }
            )
    return rows


def _fetch_gdelt_news(start: date, end: date, *, pause_seconds: float) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current = start
    while current <= end:
        try:
            rows.extend(_gdelt_day(current))
        except (requests.RequestException, ValueError) as exc:
            print(f"{current}: GDELT unavailable: {exc}")
        current += timedelta(days=1)
        if current <= end:
            time.sleep(pause_seconds)
    return rows


def _google_news_day(day: date) -> tuple[bytes, list[dict[str, str]]]:
    # Search one day either side, then trust and filter the RSS timestamp rather
    # than Google's date-query boundary, which can vary by publisher timezone.
    response = requests.get(
        "https://news.google.com/rss/search",
        params={
            "q": f"(TSLA OR Tesla) after:{day - timedelta(days=1)} before:{day + timedelta(days=1)}",
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        },
        timeout=45,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    rows: list[dict[str, str]] = []
    for item in root.findall("./channel/item"):
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if published.date() != day:
            continue
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "Google News").strip()
        description = html.unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or ""))
        if title and url:
            rows.append(
                {
                    "ticker": "TSLA",
                    "title": title,
                    "summary": description.strip(),
                    "url": url,
                    "source": source,
                    "published_at": published.isoformat(),
                    "visible_at": published.isoformat(),
                    "source_type": "google_news_rss",
                }
            )
    return response.content, rows


def _fetch_google_news(start: date, end: date, *, raw_dir: Path, pause_seconds: float) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current = start
    while current <= end:
        try:
            raw, daily_rows = _google_news_day(current)
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"{current.isoformat()}.xml").write_bytes(raw)
            rows.extend(daily_rows)
            print(f"{current}: Google RSS {len(daily_rows)} articles")
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            print(f"{current}: Google RSS unavailable: {exc}")
        current += timedelta(days=1)
        if current <= end:
            time.sleep(pause_seconds)
    deduped = { (row["published_at"], row["title"], row["url"]): row for row in rows }
    return sorted(deduped.values(), key=lambda row: row["published_at"])


def _fetch_sec_disclosures(start: date, end: date) -> list[dict[str, str]]:
    response = requests.get(
        "https://data.sec.gov/submissions/CIK0001318605.json",
        headers={"User-Agent": "AHF academic reproduction contact@example.invalid"},
        timeout=45,
    )
    response.raise_for_status()
    recent = response.json().get("filings", {}).get("recent", {})
    rows: list[dict[str, str]] = []
    for form, filing_date, acceptance_at, accession, primary_document in zip(
        recent.get("form", []), recent.get("filingDate", []), recent.get("acceptanceDateTime", []), recent.get("accessionNumber", []), recent.get("primaryDocument", [])
    ):
        if form not in {"8-K", "10-Q", "10-K"}:
            continue
        published = datetime.fromisoformat(str(acceptance_at).replace("Z", "+00:00"))
        if not start <= published.date() <= end:
            continue
        accession_no_dashes = accession.replace("-", "")
        rows.append(
            {
                "ticker": "TSLA",
                "title": f"Tesla filed {form} with the SEC",
                "summary": "Official Tesla SEC disclosure.",
                "url": f"https://www.sec.gov/Archives/edgar/data/1318605/{accession_no_dashes}/{primary_document}",
                "source": "SEC EDGAR",
                "published_at": published.isoformat(),
                "visible_at": published.isoformat(),
                "source_type": "sec_edgar",
            }
        )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--price-path", type=Path)
    parser.add_argument("--price-start", default="2025-05-01")
    parser.add_argument("--price-end", default="2025-10-24")
    parser.add_argument("--fetch-gdelt-news", action="store_true")
    parser.add_argument("--gdelt-pause-seconds", type=float, default=6.0)
    parser.add_argument("--fetch-google-news", action="store_true")
    parser.add_argument("--google-pause-seconds", type=float, default=1.5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.data_root.resolve()
    price_path = args.price_path.resolve() if args.price_path else root / "source" / "price" / "tsla_daily_futu_qfq_20250501_20251024.csv"
    news_path = root / "source" / "news" / "tsla_news_by_day.jsonl"

    frame = _fetch_futu_daily(args.price_start, args.price_end)
    price_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(price_path, index=False, encoding="utf-8")

    news_rows: list[dict[str, str]] = []
    news_status = "not_requested"
    if args.fetch_google_news:
        start, end = date.fromisoformat(args.price_start), date.fromisoformat(args.price_end)
        news_rows = _fetch_google_news(
            start,
            end,
            raw_dir=root / "source" / "news_raw" / "google_news_rss",
            pause_seconds=args.google_pause_seconds,
        )
        news_rows.extend(_fetch_sec_disclosures(start, end))
        news_rows.sort(key=lambda row: row["published_at"])
        news_path.parent.mkdir(parents=True, exist_ok=True)
        news_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in news_rows),
            encoding="utf-8",
        )
        news_status = "frozen_google_rss_and_sec" if news_rows else "requested_but_empty"
    elif args.fetch_gdelt_news:
        news_rows = _fetch_gdelt_news(
            date.fromisoformat(args.price_start),
            date.fromisoformat(args.price_end),
            pause_seconds=args.gdelt_pause_seconds,
        )
        news_path.parent.mkdir(parents=True, exist_ok=True)
        news_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in news_rows),
            encoding="utf-8",
        )
        news_status = "frozen" if news_rows else "requested_but_empty"

    _write_json(
        root / "manifest.json",
        {
            "experiment": "ahf_ama_protocol_tsla_2025",
            "price": {
                "provider": "Futu OpenAPI",
                "symbol": "US.TSLA",
                "adjustment": "QFQ",
                "start": args.price_start,
                "end": args.price_end,
                "path": str(price_path),
                "rows": len(frame),
            },
            "news": {
                "status": news_status,
                "provider": "Google News RSS + SEC EDGAR" if args.fetch_google_news else ("GDELT DOC API" if args.fetch_gdelt_news else None),
                "path": str(news_path),
                "rows": len(news_rows),
                "limitation": "AMA exposes news count and sentiment, not its historical headline corpus.",
            },
        },
    )
    print(json.dumps({"price_rows": len(frame), "price_path": str(price_path), "news_status": news_status}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
