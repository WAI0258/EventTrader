"""Run the isolated TSLA AHF reproduction under AMA daily action semantics."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv


HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
UPSTREAM = WORKSPACE / "ai-hedge-fund"
DATA_ROOT = WORKSPACE / "replication" / "data" / "ahf_ama_tsla_2025"
sys.path.insert(0, str(UPSTREAM))
sys.path.insert(0, str(HERE))

from local_provider import TSLALocalDataProvider  # noqa: E402
from src.main import run_hedge_fund  # noqa: E402
from src.tools.api import clear_data_provider, set_data_provider  # noqa: E402


SELECTED_ANALYSTS = ["technical_analyst", "news_sentiment_analyst"]


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _action_for_ama(native_action: str) -> str:
    return {"buy": "BUY", "cover": "BUY", "sell": "SELL", "short": "SELL"}.get(native_action.lower(), "HOLD")


def _initial_portfolio() -> dict[str, Any]:
    return {
        "cash": 100000.0,
        "margin_requirement": 0.5,
        "margin_used": 0.0,
        "positions": {"TSLA": {"long": 0, "short": 0, "long_cost_basis": 0.0, "short_cost_basis": 0.0, "short_margin_used": 0.0}},
        "realized_gains": {"TSLA": {"long": 0.0, "short": 0.0}},
    }


def _apply_native_decision(portfolio: dict[str, Any], decision: dict[str, Any], price: float) -> None:
    """Carry only the AHF order state forward; AMA scoring remains action-stream based."""
    action = str(decision.get("action", "hold")).lower()
    quantity = max(0, int(decision.get("quantity", 0) or 0))
    position = portfolio["positions"]["TSLA"]
    cash = float(portfolio["cash"])
    margin_requirement = float(portfolio["margin_requirement"])
    if action == "buy":
        executed = min(quantity, int(cash // price))
        old_long = int(position["long"])
        if executed:
            position["long_cost_basis"] = ((old_long * float(position["long_cost_basis"])) + executed * price) / (old_long + executed)
            position["long"] = old_long + executed
            portfolio["cash"] = cash - executed * price
    elif action == "sell":
        executed = min(quantity, int(position["long"]))
        if executed:
            position["long"] -= executed
            portfolio["cash"] = cash + executed * price
            if position["long"] == 0:
                position["long_cost_basis"] = 0.0
    elif action == "short":
        available_cash = max(0.0, cash - float(portfolio["margin_used"]))
        executed = min(quantity, int(available_cash // (price * margin_requirement))) if price > 0 else 0
        if executed:
            old_short = int(position["short"])
            position["short_cost_basis"] = ((old_short * float(position["short_cost_basis"])) + executed * price) / (old_short + executed)
            margin = executed * price * margin_requirement
            position["short"] = old_short + executed
            position["short_margin_used"] += margin
            portfolio["margin_used"] += margin
            portfolio["cash"] = cash + executed * price - margin
    elif action == "cover":
        executed = min(quantity, int(position["short"]))
        if executed:
            released_margin = float(position["short_margin_used"]) * executed / int(position["short"])
            position["short"] -= executed
            position["short_margin_used"] -= released_margin
            portfolio["margin_used"] -= released_margin
            portfolio["cash"] = cash + released_margin - executed * price
            if position["short"] == 0:
                position["short_cost_basis"] = 0.0
                position["short_margin_used"] = 0.0


def _ama_equity(rows: list[dict[str, Any]]) -> list[float]:
    """AMA PAI: r_t = w_t * (P_t - P_{t-1}) / P_{t-1}."""
    capital = 100000.0
    series = [capital]
    weights = {"BUY": 1.0, "SELL": -1.0, "HOLD": 0.0}
    for row in rows:
        capital *= 1 + weights[row["ama_action"]] * float(row["market_return"])
        series.append(capital)
    return series


def _metrics(series: list[float]) -> dict[str, float]:
    values = pd.Series(series, dtype=float)
    returns = values.pct_change().dropna()
    std = float(returns.std())
    peak = values.cummax()
    return {
        "total_return_pct": float((values.iloc[-1] / values.iloc[0] - 1) * 100),
        "annualized_return_pct": float(((values.iloc[-1] / values.iloc[0]) ** (252 / max(1, len(returns))) - 1) * 100),
        "annualized_volatility_pct": float(std * (252**0.5) * 100),
        "sharpe_rf0": float((returns.mean() / std) * (252**0.5)) if std > 0 else 0.0,
        "max_drawdown_pct": float(((values - peak) / peak).min() * 100),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--price-path", type=Path, default=DATA_ROOT / "source" / "price" / "tsla_daily_futu_qfq_20250501_20251024.csv")
    parser.add_argument("--news-path", type=Path, default=DATA_ROOT / "source" / "news" / "tsla_news_by_day.jsonl")
    parser.add_argument("--start-date", default="2025-08-01")
    parser.add_argument("--end-date", default="2025-10-24")
    parser.add_argument("--output-dir", type=Path, default=HERE / "runs" / "tsla_minimax_m21_google_rss_sec_ama_pti_2025q3")
    parser.add_argument("--model-name", default="MiniMax-M2.1")
    parser.add_argument("--provider", default="OpenAI")
    parser.add_argument("--news-limit", type=int, default=20)
    parser.add_argument("--allow-empty-news", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv(UPSTREAM / ".env")
    load_dotenv(WORKSPACE / ".env")
    args = parse_args()
    price_path, news_path, output = args.price_path.resolve(), args.news_path.resolve(), args.output_dir.resolve()
    if not price_path.exists():
        raise FileNotFoundError(price_path)
    if not news_path.exists() and not args.allow_empty_news:
        raise FileNotFoundError(f"Missing frozen historical news: {news_path}. Use --allow-empty-news only for the declared technical-only ablation.")
    if args.model_name.lower().startswith("minimax") and args.provider.lower() == "openai":
        if os.environ.get("MINIMAX_API_KEY"):
            os.environ["OPENAI_API_KEY"] = os.environ["MINIMAX_API_KEY"]
        os.environ["OPENAI_API_BASE"] = "https://model1.imfan.top/v1"
    elif args.model_name.lower().startswith("minimax") and args.provider.lower() == "anthropic":
        if os.environ.get("MINIMAX_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = os.environ["MINIMAX_API_KEY"]
        os.environ["ANTHROPIC_BASE_URL"] = "https://api.minimax.io/anthropic"
    required_key = "OPENAI_API_KEY" if args.provider.lower() == "openai" else "ANTHROPIC_API_KEY"
    if not args.dry_run and not os.environ.get(required_key):
        raise RuntimeError(f"Missing {required_key} or MINIMAX_API_KEY")

    prices = pd.read_csv(price_path, encoding="utf-8")
    prices["date"] = pd.to_datetime(prices["time_key"]).dt.strftime("%Y-%m-%d")
    prices = prices.sort_values("date")
    days = prices[(prices["date"] >= args.start_date) & (prices["date"] <= args.end_date)]["date"].tolist()
    if args.dry_run:
        print(json.dumps({"days": len(days), "price_path": str(price_path), "news_path": str(news_path), "output": str(output)}))
        return 0

    output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    portfolio = _initial_portfolio()
    for day in days:
        audit_path = output / "days" / day / "audit.json"
        if audit_path.exists():
            payload = json.loads(audit_path.read_text(encoding="utf-8"))
            rows.append(payload["ama_row"])
            _apply_native_decision(portfolio, payload.get("raw_output") or {}, float(payload["ama_row"]["price"]))
            continue
        historical = prices[prices["date"] < day]
        if historical.empty:
            raise RuntimeError(f"No prior trading close available for decision date {day}")
        previous = historical.iloc[-1]
        price_cutoff = datetime.fromisoformat(f"{previous['date']}T23:59:59+00:00")
        news_cutoff = datetime(
            pd.Timestamp(day).year,
            pd.Timestamp(day).month,
            pd.Timestamp(day).day,
            9,
            30,
            tzinfo=ZoneInfo("America/New_York"),
        ).astimezone(timezone.utc)
        provider = TSLALocalDataProvider(
            price_path=price_path,
            news_path=news_path,
            price_visible_through=price_cutoff,
            news_visible_through=news_cutoff,
        )
        set_data_provider(provider)
        started, wall = datetime.now(timezone.utc), time.perf_counter()
        try:
            result = run_hedge_fund(
                tickers=["TSLA"], start_date=(pd.Timestamp(previous["date"]) - pd.DateOffset(months=3)).strftime("%Y-%m-%d"),
                end_date=str(previous["date"]),
                portfolio=portfolio,
                selected_analysts=SELECTED_ANALYSTS, model_name=args.model_name, model_provider=args.provider,
            )
        finally:
            clear_data_provider()
        decision = (result.get("decisions") or {}).get("TSLA", {})
        close = float(prices.loc[prices["date"] == day, "close"].iloc[0])
        previous_close = float(previous["close"])
        row = {
            "date": day,
            "price": close,
            "previous_close": previous_close,
            "market_return": close / previous_close - 1,
            "native_action": str(decision.get("action", "hold")).lower(),
        }
        row["ama_action"] = _action_for_ama(row["native_action"])
        before = json.loads(json.dumps(portfolio))
        _apply_native_decision(portfolio, decision, row["price"])
        payload = {"trade_date": day, "model": args.model_name, "provider": args.provider, "selected_analysts": SELECTED_ANALYSTS, "input": {"price_path": str(price_path), "news_path": str(news_path), "price_visible_through": price_cutoff.isoformat(), "news_visible_through": news_cutoff.isoformat(), "empty_news_ablation": not news_path.exists()}, "raw_output": decision, "analyst_signals": result.get("analyst_signals") or {}, "llm_usage": (result.get("audit") or {}).get("llm_usage", {}), "native_portfolio_before": before, "native_portfolio_after": portfolio, "elapsed_seconds": time.perf_counter() - wall, "started_at": started.isoformat(), "ama_row": row}
        _write(audit_path, payload)
        rows.append(row)
        print(f"{day}: {row['native_action']} -> {row['ama_action']}", flush=True)
    equity = _ama_equity(rows)
    summary = {"protocol": "AMA PAI daily-signal scoring: r_t = w_t * (P_t - P_t-1) / P_t-1", "limitation": "Google RSS and SEC disclosure inputs are not AMA's non-public verified-news corpus.", "empty_news_ablation": not news_path.exists(), "native_portfolio_state": portfolio, "model": args.model_name, "window": {"start": args.start_date, "end": args.end_date}, "rows": rows, "equity": equity, "metrics": _metrics(equity)}
    _write(output / "summary.json", summary)
    print(json.dumps(summary["metrics"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
