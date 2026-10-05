from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pickle
import re
from datetime import date, datetime, time, timezone


UTC = timezone.utc
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = ROOT / "replication" / "data" / "finmem_tsla_paper"
DEFAULT_SOURCE = DATA_ROOT / "benchmark" / "stock_data_cherrypick_2000_2024_v2.pkl"
DEFAULT_OUTPUT_ROOT = DATA_ROOT / "finsaber_finmem"
SYMBOL = "TSLA"
TRAIN_START = "2021-08-17"
TRAIN_END = "2022-10-05"
TEST_START = "2022-10-06"
TEST_END = "2023-04-10"
TERMINAL_DAY = "2023-04-11"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _require_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _normalize_news(
    *,
    entries: list[Any],
    day: date,
    source_sha256: str,
) -> list[dict[str, Any]]:
    visible_at = datetime.combine(day, time.max, tzinfo=UTC).isoformat()
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        content = str(entry).strip()
        if not content:
            continue
        normalized.append(
            {
                "title": "",
                "content": content,
                "published_at": day.isoformat(),
                "visible_at": visible_at,
                "source_ref": (
                    f"finsaber_legacy:{source_sha256}:{SYMBOL}:{day.isoformat()}:{index}"
                ),
                "labels": ["finsaber_legacy_dataset"],
            }
        )
    return normalized


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_filing_context(*, filing_type: str, text: str) -> str:
    """Create a bounded, source-derived filing context without an additional LLM."""
    compact = _compact_text(text)
    if not compact:
        return ""

    section_numbers = ("1", "1A", "7", "7A") if filing_type == "10-K" else ("2", "1A")
    sections: list[str] = []
    seen_starts: set[int] = set()
    for number in section_numbers:
        match = re.search(rf"\bITEM\s+{re.escape(number)}\s*\.", compact, flags=re.IGNORECASE)
        if match is None or match.start() in seen_starts:
            continue
        seen_starts.add(match.start())
        sections.append(compact[match.start() : match.start() + 4_000])

    if not sections:
        sections.append(compact[:8_000])

    result = "\n\n".join(sections)
    return result[:12_000]


def prepare(*, source: Path, output_root: Path) -> Path:
    source = source.resolve()
    output_root = output_root.resolve()
    if not source.exists():
        raise FileNotFoundError(f"Missing FinSaber dataset: {source}")

    with source.open("rb") as handle:
        raw_data = pickle.load(handle)
    if not isinstance(raw_data, dict):
        raise TypeError("FinSaber dataset must be a date-keyed dictionary")

    start = _require_date(TRAIN_START)
    terminal = _require_date(TERMINAL_DAY)
    source_digest = _sha256(source)
    selected_days = sorted(
        day
        for day, record in raw_data.items()
        if isinstance(day, date)
        and start <= day <= terminal
        and isinstance(record, dict)
        and isinstance(record.get("price"), dict)
        and SYMBOL in record["price"]
    )
    if not selected_days or selected_days[0] != start or selected_days[-1] != terminal:
        raise ValueError(
            "FinSaber TSLA coverage does not contain the required train start and terminal day"
        )

    price_path = output_root / "price" / "tsla_adjusted_close_20210817_20230411.csv"
    news_root = output_root / "news" / "processed_news_by_day"
    raw_filings_path = output_root / "filings" / "raw_filings.jsonl"
    filing_context_path = output_root / "filings" / "filing_context.jsonl"
    price_path.parent.mkdir(parents=True, exist_ok=True)
    news_root.mkdir(parents=True, exist_ok=True)
    raw_filings_path.parent.mkdir(parents=True, exist_ok=True)

    filing_rows: list[dict[str, Any]] = []
    news_count = 0
    with price_path.open("w", encoding="utf-8", newline="") as price_handle:
        writer = csv.DictWriter(price_handle, fieldnames=["date", "close"])
        writer.writeheader()
        for day in selected_days:
            record = raw_data[day]
            bar = record["price"][SYMBOL]
            if not isinstance(bar, dict) or bar.get("adjusted_close") is None:
                raise ValueError(f"Missing adjusted_close for {SYMBOL} on {day.isoformat()}")
            writer.writerow({"date": day.isoformat(), "close": float(bar["adjusted_close"])})

            raw_news = record.get("news", {}).get(SYMBOL, [])
            if not isinstance(raw_news, list):
                raise TypeError(f"Unexpected news payload on {day.isoformat()}")
            normalized_news = _normalize_news(
                entries=raw_news,
                day=day,
                source_sha256=source_digest,
            )
            news_count += len(normalized_news)
            with (news_root / f"{day.isoformat()}.jsonl").open("w", encoding="utf-8") as handle:
                for item in normalized_news:
                    handle.write(json.dumps(item, ensure_ascii=False))
                    handle.write("\n")

            for filing_key, filing_type in (("filing_k", "10-K"), ("filing_q", "10-Q")):
                filing = record.get(filing_key, {}).get(SYMBOL)
                if filing:
                    filing_rows.append(
                        {
                            "filing_date": day.isoformat(),
                            "type": filing_type,
                            "summary": str(filing),
                            "source_ref": (
                                f"finsaber_legacy:{source_digest}:{SYMBOL}:{day.isoformat()}:{filing_type}"
                            ),
                        }
                    )

    with raw_filings_path.open("w", encoding="utf-8") as handle:
        for row in filing_rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")

    context_rows: list[dict[str, Any]] = []
    for row in filing_rows:
        source_text = str(row["summary"])
        context = _extract_filing_context(
            filing_type=str(row["type"]),
            text=source_text,
        )
        if not context:
            raise ValueError(f"Empty filing context for {row['filing_date']} {row['type']}")
        context_rows.append(
            {
                **row,
                "summary": context,
                "source_char_count": len(source_text),
                "context_char_count": len(context),
                "context_method": "deterministic_section_extract_v1",
            }
        )
    with filing_context_path.open("w", encoding="utf-8") as handle:
        for row in context_rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")

    manifest = {
        "schema_version": 1,
        "source": {
            "path": str(source),
            "sha256": source_digest,
            "format": "FINSABER legacy date-keyed pickle",
        },
        "adapter": {
            "symbol": SYMBOL,
            "price_field": "adjusted_close",
            "news_mapping": "preserved text; date-only records visible by EOD on their source date",
            "filing_mapping": "raw filings preserved; FinMem receives deterministic bounded section extracts",
        },
        "protocol": {
            "train_start": TRAIN_START,
            "train_end": TRAIN_END,
            "test_start": TEST_START,
            "test_end": TEST_END,
            "terminal_day": TERMINAL_DAY,
        },
        "artifacts": {
            "daily_ohlcv": str(price_path),
            "news_archive": str(news_root),
            "filing_summaries": str(filing_context_path),
            "raw_filings": str(raw_filings_path),
        },
        "counts": {
            "trading_days": len(selected_days),
            "news_records": news_count,
            "filing_records": len(filing_rows),
            "filing_context_records": len(context_rows),
            "max_filing_context_chars": max((row["context_char_count"] for row in context_rows), default=0),
        },
    }
    manifest_path = output_root / "manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Adapt the official FINSABER legacy TSLA dataset to FinMem inputs."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args()
    manifest_path = prepare(source=args.source, output_root=args.output_root)
    print(f"Prepared FinSaber FinMem inputs: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
