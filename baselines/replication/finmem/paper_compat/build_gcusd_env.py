from __future__ import annotations

import argparse
import csv
import json
import pickle
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DAILY_OHLCV = (
    WORKSPACE_ROOT
    / "gold_futures"
    / "experiment_gcusd_inputs"
    / "project_inputs"
    / "FinMem-LLM-StockTrading"
    / "gcusd_daily_ohlcv_20250630_20260630.csv"
)
DEFAULT_NEWS_ARCHIVE = Path(
    r"baselines\.local\finmem_gold_gcusd_20260101_20260630_processed_news_by_day"
)
DEFAULT_OUTPUT_PICKLE = (
    WORKSPACE_ROOT
    / "experiments"
    / "finmem_gcusd_smoke"
    / "inputs"
    / "env_data_gcusd_20250630_20260103.pkl"
)
DEFAULT_RECEIPT_PATH = DEFAULT_OUTPUT_PICKLE.with_suffix(".receipt.json")


@dataclass(frozen=True)
class DailyRow:
    day: date
    close: float


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_daily_prices(path: Path, start_date: date, end_date: date) -> list[DailyRow]:
    rows: list[DailyRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw_day = (row.get("date") or row.get("Date") or "").strip()
            raw_close = (row.get("close") or row.get("Close") or "").strip()
            if not raw_day or not raw_close:
                continue
            day = datetime.strptime(raw_day, "%Y-%m-%d").date()
            if day < start_date or day > end_date:
                continue
            rows.append(DailyRow(day=day, close=float(raw_close)))
    rows.sort(key=lambda item: item.day)
    return rows


def _news_path(news_archive: Path, day: date) -> Path:
    jsonl_path = news_archive / f"{day.isoformat()}.jsonl"
    if jsonl_path.exists():
        return jsonl_path
    return news_archive / f"{day.isoformat()}.json"


def _iter_news_items(news_archive: Path, day: date) -> list[dict[str, Any]]:
    path = _news_path(news_archive, day)
    if not path.exists():
        return []

    items: list[dict[str, Any]] = []
    if path.suffix == ".jsonl":
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    items.append(parsed)
        return items

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("records"), list):
            return [item for item in payload["records"] if isinstance(item, dict)]
        return [payload]
    return []


def _format_news_item(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or item.get("summary") or "").strip()
    labels = [str(label).strip() for label in item.get("labels") or [] if str(label).strip()]
    source_ref = str(item.get("source_ref") or "").strip()
    published_at = str(item.get("published_at") or "").strip()

    lines: list[str] = []
    if title:
        lines.append(title)
    if content:
        lines.append(content)
    if labels:
        lines.append("Labels: " + ", ".join(labels))
    if published_at:
        lines.append(f"Published: {published_at}")
    if source_ref:
        lines.append(f"Source: {source_ref}")
    return "\n".join(lines).strip()


def _load_daily_news(
    news_archive: Path,
    day: date,
    *,
    max_news_per_day: int,
) -> tuple[list[str], dict[str, Any]]:
    visible_deadline = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)
    seen: set[str] = set()
    formatted: list[str] = []
    raw_visible: list[dict[str, Any]] = []
    for item in _iter_news_items(news_archive, day):
        visible_at = _parse_iso_dt(str(item.get("visible_at") or ""))
        if visible_at is None or visible_at > visible_deadline:
            continue
        dedupe_key = str(item.get("source_ref") or item.get("title") or json.dumps(item, sort_keys=True))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rendered = _format_news_item(item)
        if not rendered:
            continue
        formatted.append(rendered)
        raw_visible.append(
            {
                "title": item.get("title"),
                "source_ref": item.get("source_ref"),
                "published_at": item.get("published_at"),
                "visible_at": item.get("visible_at"),
                "labels": item.get("labels") or [],
            }
        )
        if len(formatted) >= max_news_per_day:
            break

    receipt = {
        "day": day.isoformat(),
        "source_path": str(_news_path(news_archive, day)),
        "visible_count": len(formatted),
        "visible_records": raw_visible,
    }
    return formatted, receipt


def build_env_data(
    *,
    daily_ohlcv: Path,
    news_archive: Path,
    start_date: date,
    end_date: date,
    max_news_per_day: int = 20,
) -> tuple[OrderedDict[date, dict[str, Any]], dict[str, Any]]:
    daily_rows = _load_daily_prices(daily_ohlcv, start_date=start_date, end_date=end_date)
    env_data: OrderedDict[date, dict[str, Any]] = OrderedDict()
    daily_receipts: list[dict[str, Any]] = []

    for row in daily_rows:
        day_news, news_receipt = _load_daily_news(
            news_archive,
            row.day,
            max_news_per_day=max_news_per_day,
        )
        env_data[row.day] = {
            "price": {"GCUSD": row.close},
            "filing_k": {},
            "filing_q": {},
            "news": {"GCUSD": day_news} if day_news else {},
        }
        daily_receipts.append(
            {
                "day": row.day.isoformat(),
                "close": row.close,
                "news_count": len(day_news),
                "news_source_path": news_receipt["source_path"],
                "visible_records": news_receipt["visible_records"],
            }
        )

    receipt = {
        "symbol": "GCUSD",
        "daily_ohlcv": str(daily_ohlcv),
        "news_archive": str(news_archive),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "row_count": len(env_data),
        "days": daily_receipts,
    }
    return env_data, receipt


def _write_outputs(
    *,
    env_data: OrderedDict[date, dict[str, Any]],
    receipt: dict[str, Any],
    output_pickle: Path,
    receipt_path: Path,
) -> None:
    output_pickle.parent.mkdir(parents=True, exist_ok=True)
    with output_pickle.open("wb") as handle:
        pickle.dump(env_data, handle)
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-ohlcv", type=Path, default=DEFAULT_DAILY_OHLCV)
    parser.add_argument("--news-archive", type=Path, default=DEFAULT_NEWS_ARCHIVE)
    parser.add_argument("--start-date", default="2025-06-30")
    parser.add_argument("--end-date", default="2026-01-03")
    parser.add_argument("--max-news-per-day", type=int, default=20)
    parser.add_argument("--output-pickle", type=Path, default=DEFAULT_OUTPUT_PICKLE)
    parser.add_argument("--receipt-path", type=Path, default=DEFAULT_RECEIPT_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    env_data, receipt = build_env_data(
        daily_ohlcv=args.daily_ohlcv.resolve(),
        news_archive=args.news_archive.resolve(),
        start_date=start_date,
        end_date=end_date,
        max_news_per_day=args.max_news_per_day,
    )
    _write_outputs(
        env_data=env_data,
        receipt=receipt,
        output_pickle=args.output_pickle.resolve(),
        receipt_path=args.receipt_path.resolve(),
    )
    print(f"Wrote env pickle: {args.output_pickle.resolve()}")
    print(f"Wrote receipt: {args.receipt_path.resolve()}")
    print(f"Rows: {len(env_data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
