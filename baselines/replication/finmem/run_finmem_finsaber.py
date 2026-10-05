from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime
from pathlib import Path

from finmem_tsla_runner import run_experiment
from prepare_finsaber_finmem_inputs import (
    DEFAULT_OUTPUT_ROOT as DEFAULT_INPUT_ROOT,
    TERMINAL_DAY,
    TEST_END,
    TEST_START,
    TRAIN_END,
    TRAIN_START,
)


ROOT = Path(__file__).resolve().parents[2]
RUN_ROOT = Path(__file__).resolve().parent / "runs"
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config" / "finsaber_finmem_minimax_qinzhi.toml"


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _csv_days(path: Path) -> list[date]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            _parse_day(str(row["date"]))
            for row in csv.DictReader(handle)
            if str(row.get("date") or "").strip()
        ]


def _preflight(*, input_root: Path, train_start: str, train_end: str, test_start: str, test_end: str) -> tuple[Path, Path, Path]:
    manifest_path = input_root / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Missing FinSaber input manifest: {manifest_path}. Run prepare_finsaber_finmem_inputs.py first."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    protocol = manifest.get("protocol") or {}
    if (protocol.get("train_start"), protocol.get("train_end"), protocol.get("test_start"), protocol.get("test_end")) != (
        TRAIN_START,
        TRAIN_END,
        TEST_START,
        TEST_END,
    ):
        raise ValueError("Prepared inputs do not declare the official FINSABER FinMem protocol.")

    artifacts = manifest.get("artifacts") or {}
    daily = Path(str(artifacts.get("daily_ohlcv") or "")).resolve()
    news = Path(str(artifacts.get("news_archive") or "")).resolve()
    filings = Path(str(artifacts.get("filing_summaries") or "")).resolve()
    for path in (daily, news, filings):
        if not path.exists():
            raise FileNotFoundError(f"Missing prepared FinSaber input: {path}")

    days = _csv_days(daily)
    required = (_parse_day(train_start), _parse_day(train_end), _parse_day(test_start), _parse_day(test_end))
    missing = [day.isoformat() for day in required if day not in days]
    if missing:
        raise ValueError(f"Prepared price data is missing required dates: {', '.join(missing)}")
    if _parse_day(test_end).isoformat() == TEST_END and _parse_day(TERMINAL_DAY) not in days:
        raise ValueError(f"Prepared price data is missing terminal day: {TERMINAL_DAY}")
    return daily, news, filings


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FinMem on the official FINSABER legacy TSLA protocol.")
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--config-path", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=RUN_ROOT / "finsaber_finmem_tsla_20221006_20230410_m2_1_qinzhi_eod")
    parser.add_argument("--train-start", default=TRAIN_START)
    parser.add_argument("--train-end", default=TEST_START)
    parser.add_argument("--test-start", default=TEST_START)
    parser.add_argument("--test-end", default=TEST_END)
    parser.add_argument("--max-news-per-day", type=int, default=1000)
    parser.add_argument("--input-price-per-mtok", type=float, default=None)
    parser.add_argument("--output-price-per-mtok", type=float, default=None)
    args = parser.parse_args()

    daily, news, filings = _preflight(
        input_root=args.input_root.resolve(),
        train_start=args.train_start,
        train_end=args.train_end,
        test_start=args.test_start,
        test_end=args.test_end,
    )
    summary = run_experiment(
        output_dir=args.output_dir.resolve(),
        daily_ohlcv=daily,
        news_archive=news,
        filing_summaries=filings,
        config_path=args.config_path.resolve(),
        train_start=args.train_start,
        train_end=args.train_end,
        test_start=args.test_start,
        test_end=args.test_end,
        max_news_per_day=args.max_news_per_day,
        input_price_per_mtok=args.input_price_per_mtok,
        output_price_per_mtok=args.output_price_per_mtok,
    )
    print(f"Summary: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
