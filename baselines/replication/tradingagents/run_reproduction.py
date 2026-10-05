"""Run the frozen 2024Q1 TradingAgents reproduction one date at a time."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
UPSTREAM = WORKSPACE / "tradingagents"
sys.path.insert(0, str(UPSTREAM))
sys.path.insert(0, str(HERE))

from local_vendor import load_ohlcv, register_vendor  # noqa: E402

DATA_ROOT = WORKSPACE / "replication" / "data" / "tradingagents_paper_2024q1"
DEFAULT_OUTPUT = HERE / "runs" / "aapl_minimax_m21_2024q1"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _dates(ticker: str, start: str, end: str) -> list[str]:
    frame = load_ohlcv(ticker)
    mask = (frame["Date"] >= start) & (frame["Date"] <= end)
    return frame.loc[mask, "Date"].dt.strftime("%Y-%m-%d").tolist()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", choices=["AAPL", "GOOGL", "AMZN", "TSLA"], default="AAPL")
    parser.add_argument("--start-date", default="2024-01-01")
    parser.add_argument("--end-date", default="2024-03-29")
    parser.add_argument("--dates", nargs="*")
    parser.add_argument("--provider", default="minimax-cn")
    parser.add_argument("--deep-model", default="MiniMax-M2.1")
    parser.add_argument("--quick-model", default="MiniMax-M2.1")
    parser.add_argument("--backend-url", default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    parser.add_argument("--price-path", type=Path)
    parser.add_argument("--news-path", type=Path)
    parser.add_argument("--ama-protocol", action="store_true")
    parser.add_argument("--news-limit", type=int, default=20)
    parser.add_argument("--llm-timeout", type=float, default=900)
    parser.add_argument("--llm-max-retries", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    _load_dotenv(UPSTREAM / ".env")
    _load_dotenv(WORKSPACE / ".env")
    args = parse_args()
    output = args.output_dir.resolve()
    data_root = args.data_root.resolve()

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.llm_clients.api_key_env import get_api_key_env
    from cli.stats_handler import StatsCallbackHandler

    config = DEFAULT_CONFIG.copy()
    config.update(
        {
            "llm_provider": args.provider,
            "deep_think_llm": args.deep_model,
            "quick_think_llm": args.quick_model,
            "backend_url": args.backend_url,
            "paper_repro_data_root": str(data_root),
            "paper_repro_price_path": str(args.price_path.resolve()) if args.price_path else None,
            "paper_repro_news_path": str(args.news_path.resolve()) if args.news_path else None,
            "results_dir": str(output / "state_logs"),
            "data_cache_dir": str(output / "cache"),
            "memory_log_path": str(output / "memory" / "trading_memory.md"),
            "disable_deferred_reflection": True,
            "checkpoint_enabled": False,
            "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1,
            "llm_timeout": args.llm_timeout,
            "llm_max_retries": args.llm_max_retries,
            "news_article_limit": args.news_limit,
            "global_news_article_limit": args.news_limit,
            "data_vendors": {
                "core_stock_apis": "paper_repro_local",
                "technical_indicators": "paper_repro_local",
                "fundamental_data": "yfinance",
                "news_data": "paper_repro_local",
                "macro_data": "baseline_disabled",
                "prediction_markets": "baseline_disabled",
            },
            "tool_vendors": {
                "get_stock_data": "paper_repro_local",
                "get_indicators": "paper_repro_local",
                "get_news": "paper_repro_local",
                "get_global_news": "paper_repro_local",
                "get_insider_transactions": "paper_repro_local",
                "get_macro_indicators": "baseline_disabled",
                "get_prediction_markets": "baseline_disabled",
            },
        }
    )

    from tradingagents.dataflows.config import set_config

    set_config(config)
    register_vendor()
    dates = args.dates or _dates(args.ticker, args.start_date, args.end_date)
    price_history = load_ohlcv(args.ticker)
    pending = [date for date in dates if not (output / date / "audit.json").exists()]
    print(json.dumps({"ticker": args.ticker, "dates": len(dates), "pending": len(pending), "output": str(output)}))
    if args.dry_run:
        return 0

    if (
        args.provider.lower() == "minimax-cn"
        and not os.environ.get("MINIMAX_CN_API_KEY")
        and os.environ.get("MINIMAX_API_KEY")
    ):
        os.environ["MINIMAX_CN_API_KEY"] = os.environ["MINIMAX_API_KEY"]

    key_env = get_api_key_env(args.provider)
    if key_env and not os.environ.get(key_env):
        raise RuntimeError(f"Missing API key environment variable: {key_env}")

    from tradingagents.graph.trading_graph import TradingAgentsGraph

    output.mkdir(parents=True, exist_ok=True)
    for trade_date in pending:
        prior = price_history[price_history["Date"] < trade_date]
        if args.ama_protocol:
            if prior.empty:
                raise RuntimeError(f"AMA protocol needs a prior close before {trade_date}")
            config["baseline_visible_through_date"] = prior.iloc[-1]["Date"].strftime("%Y-%m-%d")
            config["baseline_news_visible_through"] = datetime(
                int(trade_date[:4]), int(trade_date[5:7]), int(trade_date[8:10]), 9, 30,
                tzinfo=ZoneInfo("America/New_York"),
            ).astimezone(timezone.utc).isoformat()
        else:
            config["baseline_visible_through_date"] = trade_date
            config.pop("baseline_news_visible_through", None)
        stats = StatsCallbackHandler()
        started = datetime.now(timezone.utc)
        wall = time.perf_counter()
        graph = TradingAgentsGraph(
            selected_analysts=("market", "news"),
            debug=False,
            config=config,
            callbacks=[stats],
        )
        state, decision = graph.propagate(args.ticker, trade_date, asset_type="stock")
        day = output / trade_date
        report = graph.save_reports(state, args.ticker, save_path=day / "reports")
        audit = {
            "baseline": "tradingagents_ama_protocol" if args.ama_protocol else "tradingagents_paper_window_partial_reproduction",
            "ticker": args.ticker,
            "trade_date": trade_date,
            "status": "complete",
            "selected_analysts": ["market", "news"],
            "omitted_original_input": ["fundamentals", "historical Reddit/social sentiment"],
            "model": {"provider": args.provider, "deep": args.deep_model, "quick": args.quick_model},
            "data": {"root": str(data_root), "price": str(args.price_path) if args.price_path else "Futu QFQ daily", "news": str(args.news_path) if args.news_path else "jackzhousmu/news"},
            "decision": decision,
            "final_trade_decision": state.get("final_trade_decision"),
            "llm_usage": stats.get_stats(),
            "timing": {
                "price_visible_through": config["baseline_visible_through_date"],
                "news_visible_through": config.get("baseline_news_visible_through"),
                "started_at": started.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": time.perf_counter() - wall,
            },
            "report": str(report),
        }
        day.mkdir(parents=True, exist_ok=True)
        (day / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{trade_date}: {decision} ({audit['timing']['elapsed_seconds']:.1f}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
