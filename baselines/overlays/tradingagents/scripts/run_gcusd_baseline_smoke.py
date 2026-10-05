"""Run a two-day GCUSD TradingAgents baseline smoke with local data.

Default dates are 2026-01-05 and 2026-01-06. The run uses local GCUSD daily
OHLCV plus the event-trader web-search archive, and writes an audit JSON file
per date under the output directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cli.stats_handler import StatsCallbackHandler

from tradingagents.dataflows.gcusd_local import (
    build_gcusd_visibility_guard,
    collect_gcusd_news_entries,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.api_key_env import get_api_key_env


SCRIPT_PATH = Path(__file__).resolve()
TRADINGAGENTS_ROOT = SCRIPT_PATH.parents[1]
BASELINE_ROOT = SCRIPT_PATH.parents[2]

DEFAULT_DAILY_OHLCV = (
    BASELINE_ROOT
    / "gold_futures"
    / "experiment_gcusd_inputs"
    / "project_inputs"
    / "tradingagents"
    / "gcusd_daily_ohlcv_20250630_20260630.csv"
)
DEFAULT_NEWS_ARCHIVE = Path(
    r"code\data\gold_gcusd_20260101_20260630.jsonl"
)
DEFAULT_OUTPUT_DIR = BASELINE_ROOT / "experiments" / "tradingagents_gcusd_smoke"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _estimate_cost(stats: dict[str, Any], input_price_per_mtok: float | None, output_price_per_mtok: float | None) -> float | None:
    if input_price_per_mtok is None or output_price_per_mtok is None:
        return None
    return (
        stats.get("tokens_in", 0) / 1_000_000 * input_price_per_mtok
        + stats.get("tokens_out", 0) / 1_000_000 * output_price_per_mtok
    )


def _news_audit_for_date(trade_date: str, limit: int) -> list[dict[str, Any]]:
    end_dt = datetime.fromisoformat(trade_date).replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc
    )
    start_dt = end_dt - timedelta(days=7)
    entries = collect_gcusd_news_entries(start_dt, end_dt, limit=limit)
    return [
        {
            "title": item.get("title"),
            "source_ref": item.get("source_ref"),
            "published_at": item.get("published_at"),
            "visible_at": item.get("visible_at"),
            "labels": item.get("labels") or [],
        }
        for item in entries
    ]


def _build_config(args: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    config.update(
        {
            "llm_provider": args.provider,
            "deep_think_llm": args.deep_model,
            "quick_think_llm": args.quick_model,
            "backend_url": args.backend_url,
            "results_dir": str(output_dir / "tradingagents_results"),
            "data_cache_dir": str(output_dir / "cache"),
            "memory_log_path": str(output_dir / "memory" / "trading_memory.md"),
            "memory_log_max_entries": None,
            "disable_deferred_reflection": True,
            "checkpoint_enabled": False,
            "max_debate_rounds": args.debate_rounds,
            "max_risk_discuss_rounds": args.risk_rounds,
            "max_recur_limit": args.max_recur_limit,
            "output_language": args.output_language,
            "llm_timeout": args.llm_timeout_seconds,
            "llm_max_retries": args.llm_max_retries,
            "news_article_limit": args.news_limit,
            "global_news_article_limit": args.news_limit,
            "global_news_lookback_days": 7,
            "gcusd_daily_ohlcv_path": str(args.daily_ohlcv),
            "gcusd_news_archive_dir": str(args.news_archive),
            "data_vendors": {
                "core_stock_apis": "local_gcusd",
                "technical_indicators": "local_gcusd",
                "fundamental_data": "yfinance",
                "news_data": "local_gcusd",
                "macro_data": "baseline_disabled",
                "prediction_markets": "baseline_disabled",
            },
            "tool_vendors": {
                "get_stock_data": "local_gcusd",
                "get_indicators": "local_gcusd",
                "get_news": "local_gcusd",
                "get_global_news": "local_gcusd",
                "get_insider_transactions": "local_gcusd",
                "get_macro_indicators": "baseline_disabled",
                "get_prediction_markets": "baseline_disabled",
            },
        }
    )
    if args.temperature is not None:
        config["temperature"] = args.temperature
    return config


def _run_one_date(args: argparse.Namespace, trade_date: str, output_dir: Path) -> dict[str, Any]:
    day_dir = output_dir / trade_date
    day_dir.mkdir(parents=True, exist_ok=True)

    stats_handler = StatsCallbackHandler()
    config = _build_config(args, output_dir)
    config["baseline_visible_through_date"] = trade_date
    input_guard = build_gcusd_visibility_guard(
        config,
        "GCUSD",
        trade_date,
        news_limit=args.news_limit,
    )
    started = datetime.now(timezone.utc)
    wall_start = time.perf_counter()

    graph = TradingAgentsGraph(
        selected_analysts=("market", "news"),
        debug=False,
        config=config,
        callbacks=[stats_handler],
    )
    final_state, decision = graph.propagate("GCUSD", trade_date, asset_type="commodity")
    reports_dir = graph.save_reports(final_state, "GCUSD", save_path=day_dir / "reports")

    elapsed = time.perf_counter() - wall_start
    finished = datetime.now(timezone.utc)
    stats = stats_handler.get_stats()
    audit = {
        "baseline": "tradingagents",
        "symbol": "GCUSD",
        "trade_date": trade_date,
        "reported_window": {"start": "2026-01-01", "end": "2026-06-30"},
        "selected_analysts": ["market", "news"],
        "asset_type": "commodity",
        "model": {
            "provider": args.provider,
            "deep": args.deep_model,
            "quick": args.quick_model,
            "backend_url": args.backend_url,
        },
        "inputs": {
            "daily_ohlcv": str(args.daily_ohlcv),
            "news_archive": str(args.news_archive),
            "news_records_visible": _news_audit_for_date(trade_date, args.news_limit),
            "visibility_guard": input_guard,
        },
        "timing": {
            "started_at_utc": started.isoformat(),
            "finished_at_utc": finished.isoformat(),
            "elapsed_seconds": elapsed,
        },
        "llm_usage": {
            **stats,
            "estimated_cost_usd": _estimate_cost(
                stats, args.input_price_per_mtok, args.output_price_per_mtok
            ),
            "cost_note": (
                "estimated from --input-price-per-mtok/--output-price-per-mtok"
                if args.input_price_per_mtok is not None and args.output_price_per_mtok is not None
                else "not estimated; pass both price flags to compute USD cost"
            ),
        },
        "decision": decision,
        "final_trade_decision": final_state.get("final_trade_decision"),
        "artifacts": {
            "state_log_dir": str(Path(config["results_dir"]) / "GCUSD" / "TradingAgentsStrategy_logs"),
            "reports_dir": str(reports_dir),
        },
    }
    audit_path = day_dir / "audit.json"
    audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8")
    return audit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", nargs="+", default=["2026-01-05", "2026-01-06"])
    parser.add_argument("--provider", default="minimax")
    parser.add_argument("--deep-model", default="MiniMax-M2.1")
    parser.add_argument("--quick-model", default="MiniMax-M2.1")
    parser.add_argument("--backend-url", default=None)
    parser.add_argument("--daily-ohlcv", type=Path, default=DEFAULT_DAILY_OHLCV)
    parser.add_argument("--news-archive", type=Path, default=DEFAULT_NEWS_ARCHIVE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--news-limit", type=int, default=20)
    parser.add_argument("--debate-rounds", type=int, default=1)
    parser.add_argument("--risk-rounds", type=int, default=1)
    parser.add_argument("--max-recur-limit", type=int, default=100)
    parser.add_argument("--output-language", default="English")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--llm-timeout-seconds", type=float, default=900)
    parser.add_argument("--llm-max-retries", type=int, default=2)
    parser.add_argument("--input-price-per-mtok", type=float, default=None)
    parser.add_argument("--output-price-per-mtok", type=float, default=None)
    return parser.parse_args()


def main() -> int:
    _load_dotenv(TRADINGAGENTS_ROOT / ".env")
    _load_dotenv(BASELINE_ROOT / ".env")
    args = parse_args()
    if (
        args.provider.lower() == "minimax-cn"
        and not os.environ.get("MINIMAX_CN_API_KEY")
        and os.environ.get("MINIMAX_API_KEY")
    ):
        os.environ["MINIMAX_CN_API_KEY"] = os.environ["MINIMAX_API_KEY"]
    args.daily_ohlcv = args.daily_ohlcv.resolve()
    args.news_archive = args.news_archive.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not args.daily_ohlcv.exists():
        print(f"Daily OHLCV file not found: {args.daily_ohlcv}", file=sys.stderr)
        return 2
    if not args.news_archive.exists():
        print(f"News archive directory not found: {args.news_archive}", file=sys.stderr)
        return 2

    key_env = get_api_key_env(args.provider)
    if key_env and not os.environ.get(key_env):
        print(f"Missing API key env for provider {args.provider!r}: {key_env}", file=sys.stderr)
        return 2

    audits = []
    for trade_date in args.dates:
        print(f"Running GCUSD TradingAgents smoke for {trade_date}...")
        audit = _run_one_date(args, trade_date, output_dir)
        audits.append(audit)
        usage = audit["llm_usage"]
        print(
            f"  decision={audit['decision']} elapsed={audit['timing']['elapsed_seconds']:.1f}s "
            f"llm_calls={usage['llm_calls']} tool_calls={usage['tool_calls']} "
            f"tokens_in={usage['tokens_in']} tokens_out={usage['tokens_out']}"
        )

    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps({"runs": audits}, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    print(f"Audit summary written to {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
