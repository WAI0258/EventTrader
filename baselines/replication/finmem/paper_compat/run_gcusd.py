from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
WORKSPACE_ROOT = SCRIPT_PATH.parents[3]
FINMEM_ROOT = WORKSPACE_ROOT / "FinMem-LLM-StockTrading"
DEFAULT_CONFIG = SCRIPT_PATH.parent / "gcusd_m2_1_paper_compat.toml"
DEFAULT_DAILY_OHLCV = (
    WORKSPACE_ROOT
    / "gold_futures"
    / "experiment_gcusd_inputs"
    / "project_inputs"
    / "FinMem-LLM-StockTrading"
    / "gcusd_daily_ohlcv_20250630_20260630.csv"
)
DEFAULT_NEWS_ARCHIVE = WORKSPACE_ROOT / ".local" / "finmem_gold_gcusd_20260101_20260630_processed_news_by_day"
DEFAULT_OUTPUT = (
    WORKSPACE_ROOT
    / "experiments"
    / "finmem_gcusd_paper_compat_20260101_20260630_m2_1_qinzhi_v1"
)

for path in (SCRIPT_PATH.parent, FINMEM_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from bootstrap import install_paper_compat  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the paper-compatible FinMem GCUSD baseline.")
    parser.add_argument("--daily-ohlcv", type=Path, default=DEFAULT_DAILY_OHLCV)
    parser.add_argument("--news-archive", type=Path, default=DEFAULT_NEWS_ARCHIVE)
    parser.add_argument("--config-path", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--train-start", default="2025-06-30")
    parser.add_argument("--train-end", default="2025-12-31")
    parser.add_argument("--test-start", default="2026-01-01")
    parser.add_argument("--test-end", default="2026-06-30")
    parser.add_argument("--max-news-per-day", type=int, default=20)
    parser.add_argument("--input-price-per-mtok", type=float, default=None)
    parser.add_argument("--output-price-per-mtok", type=float, default=None)
    return parser.parse_args()


def _write_receipt(*, output_dir: Path, vendor_head: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "paper_compat_receipt.json").write_text(
        json.dumps(
            {
                "vendor_commit": vendor_head,
                "vendor_core": "clean FinMem puppy package",
                "chat_transport": "process-local MiniMax OpenAI-compatible adapter",
                "chat_api_key_env": "MINIMAX_API_KEY_1d",
                "embedding_transport": "original OpenAILongerThanContextEmb via Qinzhi OPENAI_API_BASE",
                "reflection": "original Guardrails implementation",
                "memory": "original vendored implementation",
                "test_future_record": "masked by the external runner guard",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = _parse_args()
    vendor_head = install_paper_compat(workspace_root=WORKSPACE_ROOT)

    from gcusd_runner import run_experiment

    output_dir = args.output_dir.resolve()
    run_experiment(
        output_dir=output_dir,
        daily_ohlcv=args.daily_ohlcv.resolve(),
        news_archive=args.news_archive.resolve(),
        config_path=args.config_path.resolve(),
        train_start=args.train_start,
        train_end=args.train_end,
        test_start=args.test_start,
        test_end=args.test_end,
        max_news_per_day=args.max_news_per_day,
        input_price_per_mtok=args.input_price_per_mtok,
        output_price_per_mtok=args.output_price_per_mtok,
    )
    _write_receipt(output_dir=output_dir, vendor_head=vendor_head)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
