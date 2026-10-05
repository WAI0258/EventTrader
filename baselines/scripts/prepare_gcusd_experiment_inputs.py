from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = WORKSPACE_ROOT / "gold_futures" / "fmp_gcusd_20250630_20260630"
OUTPUT_ROOT = WORKSPACE_ROOT / "gold_futures" / "experiment_gcusd_inputs"
CANONICAL_ROOT = OUTPUT_ROOT / "canonical"
PROJECT_ROOT = OUTPUT_ROOT / "project_inputs"

SOURCE_1MIN = SOURCE_ROOT / "gcusd_1min_ohlcv_20250630_20260630.csv"
SOURCE_5MIN = SOURCE_ROOT / "gcusd_5min_ohlcv_from_1min_20250630_20260630.csv"
SOURCE_1H = SOURCE_ROOT / "gcusd_1h_ohlcv_from_1min_20250630_20260630.csv"
SOURCE_1D_FROM_1MIN = SOURCE_ROOT / "gcusd_1d_ohlcv_from_1min_20250630_20260630.csv"
SOURCE_1D_EOD = SOURCE_ROOT / "gcusd_1d_eod_ohlcv_20250630_20260630.csv"
SOURCE_MANIFEST = SOURCE_ROOT / "manifest.json"

CANONICAL_START = datetime(2025, 6, 30, 0, 0, 0, tzinfo=UTC)
EXPERIMENT_START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
EXPERIMENT_END = datetime(2026, 6, 30, 23, 59, 0, tzinfo=UTC)
EXPERIMENT_END_DATE = EXPERIMENT_END.date()

OUT_1MIN = CANONICAL_ROOT / "gcusd_1min_canonical_20250630_20260630.csv"
OUT_5MIN = CANONICAL_ROOT / "gcusd_5min_canonical_20250630_20260630.csv"
OUT_1H = CANONICAL_ROOT / "gcusd_1h_canonical_20250630_20260630.csv"
OUT_1D_FROM_1MIN = CANONICAL_ROOT / "gcusd_1d_from_1min_canonical_20250630_20260630.csv"
OUT_1D_EOD = CANONICAL_ROOT / "gcusd_1d_eod_canonical_20250630_20260630.csv"

EVENT_TRADER_5MIN = PROJECT_ROOT / "event_trader" / "gcusd_5min_20250630_20260630.csv"
EVENT_TRADER_1H = PROJECT_ROOT / "event_trader" / "gcusd_1h_20250630_20260630.csv"
EVENT_TRADER_1MIN = PROJECT_ROOT / "event_trader" / "gcusd_1min_20250630_20260630.csv"
TRADINGAGENTS_DAILY = PROJECT_ROOT / "tradingagents" / "gcusd_daily_ohlcv_20250630_20260630.csv"
FINMEM_DAILY = PROJECT_ROOT / "FinMem-LLM-StockTrading" / "gcusd_daily_ohlcv_20250630_20260630.csv"
AI_HEDGE_FUND_DAILY = PROJECT_ROOT / "ai-hedge-fund" / "gcusd_daily_ohlcv_20250630_20260630.csv"
MANIFEST_PATH = OUTPUT_ROOT / "manifest.json"


@dataclass(frozen=True)
class CsvSliceSpec:
    source_path: Path
    output_path: Path
    datetime_field: str
    start_at: datetime
    end_at: datetime
    daily_field: bool = False


def parse_dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def parse_date_field(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def slice_csv(spec: CsvSliceSpec) -> dict[str, Any]:
    ensure_parent(spec.output_path)
    row_count = 0
    first: str | None = None
    last: str | None = None

    with spec.source_path.open("r", encoding="utf-8", newline="") as src, spec.output_path.open(
        "w", encoding="utf-8", newline=""
    ) as dst:
        reader = csv.DictReader(src)
        fieldnames = list(reader.fieldnames or ())
        if spec.datetime_field not in fieldnames:
            raise RuntimeError(
                f"Missing {spec.datetime_field!r} in {spec.source_path}"
            )
        writer = csv.DictWriter(dst, fieldnames=fieldnames)
        writer.writeheader()

        for row in reader:
            raw_value = row[spec.datetime_field]
            if raw_value is None:
                continue
            dt = parse_date_field(raw_value) if spec.daily_field else parse_dt(raw_value)
            if dt < spec.start_at or dt > spec.end_at:
                continue
            writer.writerow(row)
            row_count += 1
            if first is None:
                first = raw_value
            last = raw_value

    return {
        "source_path": str(spec.source_path),
        "output_path": str(spec.output_path),
        "row_count": row_count,
        "first": first,
        "last": last,
    }


def load_source_manifest() -> dict[str, Any]:
    payload = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Source manifest must be a JSON object: {SOURCE_MANIFEST}")
    return payload


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    CANONICAL_ROOT.mkdir(parents=True, exist_ok=True)
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)

    specs = [
        CsvSliceSpec(SOURCE_1MIN, OUT_1MIN, "datetime_utc", CANONICAL_START, EXPERIMENT_END),
        CsvSliceSpec(SOURCE_5MIN, OUT_5MIN, "datetime_utc", CANONICAL_START, EXPERIMENT_END),
        CsvSliceSpec(SOURCE_1H, OUT_1H, "datetime_utc", CANONICAL_START, EXPERIMENT_END),
        CsvSliceSpec(
            SOURCE_1D_FROM_1MIN,
            OUT_1D_FROM_1MIN,
            "datetime_utc",
            CANONICAL_START,
            EXPERIMENT_END,
        ),
        CsvSliceSpec(
            SOURCE_1D_EOD,
            OUT_1D_EOD,
            "date",
            CANONICAL_START,
            datetime.combine(EXPERIMENT_END_DATE, datetime.min.time(), tzinfo=UTC),
            daily_field=True,
        ),
        CsvSliceSpec(SOURCE_1MIN, EVENT_TRADER_1MIN, "datetime_utc", CANONICAL_START, EXPERIMENT_END),
        CsvSliceSpec(SOURCE_5MIN, EVENT_TRADER_5MIN, "datetime_utc", CANONICAL_START, EXPERIMENT_END),
        CsvSliceSpec(SOURCE_1H, EVENT_TRADER_1H, "datetime_utc", CANONICAL_START, EXPERIMENT_END),
        CsvSliceSpec(
            SOURCE_1D_EOD,
            TRADINGAGENTS_DAILY,
            "date",
            CANONICAL_START,
            datetime.combine(EXPERIMENT_END_DATE, datetime.min.time(), tzinfo=UTC),
            daily_field=True,
        ),
        CsvSliceSpec(
            SOURCE_1D_EOD,
            FINMEM_DAILY,
            "date",
            CANONICAL_START,
            datetime.combine(EXPERIMENT_END_DATE, datetime.min.time(), tzinfo=UTC),
            daily_field=True,
        ),
        CsvSliceSpec(
            SOURCE_1D_EOD,
            AI_HEDGE_FUND_DAILY,
            "date",
            CANONICAL_START,
            datetime.combine(EXPERIMENT_END_DATE, datetime.min.time(), tzinfo=UTC),
            daily_field=True,
        ),
    ]

    summaries = [slice_csv(spec) for spec in specs]
    source_manifest = load_source_manifest()

    summary_by_output = {item["output_path"]: item for item in summaries}
    manifest = {
        "canonical_symbol": "GCUSD",
        "provider": "Financial Modeling Prep",
        "timezone": "UTC",
        "canonical_window": {
            "start": CANONICAL_START.strftime("%Y-%m-%d %H:%M:%S"),
            "end": EXPERIMENT_END.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "reported_experiment_window": {
            "start": EXPERIMENT_START.strftime("%Y-%m-%d %H:%M:%S"),
            "end": EXPERIMENT_END.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "source_manifest_path": str(SOURCE_MANIFEST),
        "source_intraday_row_count": source_manifest.get("intraday", {}).get("row_count"),
        "canonical_outputs": {
            "1min": summary_by_output[str(OUT_1MIN)],
            "5min": summary_by_output[str(OUT_5MIN)],
            "1h": summary_by_output[str(OUT_1H)],
            "1d_from_1min": summary_by_output[str(OUT_1D_FROM_1MIN)],
            "1d_eod": summary_by_output[str(OUT_1D_EOD)],
        },
        "project_inputs": {
            "event_trader": {
                "primary_bars_5min": summary_by_output[str(EVENT_TRADER_5MIN)],
                "context_bars_1h": summary_by_output[str(EVENT_TRADER_1H)],
                "audit_bars_1min": summary_by_output[str(EVENT_TRADER_1MIN)],
            },
            "tradingagents": {
                "daily_ohlcv": summary_by_output[str(TRADINGAGENTS_DAILY)],
            },
            "FinMem-LLM-StockTrading": {
                "daily_ohlcv": summary_by_output[str(FINMEM_DAILY)],
            },
            "ai-hedge-fund": {
                "daily_ohlcv": summary_by_output[str(AI_HEDGE_FUND_DAILY)],
            },
        },
        "notes": [
            "GCUSD is the only canonical gold market source for baseline experiments in this workspace.",
            "Canonical 5min, 1h, and 1d files are derived from the canonical GCUSD minute feed already materialized under gold_futures/fmp_gcusd_20250630_20260630.",
            "Project-specific files are convenience copies that point each baseline at one unambiguous GCUSD input path.",
        ],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Wrote manifest: {MANIFEST_PATH}")
    for item in summaries:
        print(f"{item['row_count']:>7} rows -> {item['output_path']}")


if __name__ == "__main__":
    main()
