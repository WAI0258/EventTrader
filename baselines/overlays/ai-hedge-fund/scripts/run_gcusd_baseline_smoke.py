"""Run a two-day GCUSD AI Hedge Fund baseline smoke with local data.

Default dates are 2026-01-05 and 2026-01-06. The run keeps AI Hedge Fund's
daily business-day workflow and portfolio logic, but executes each decision at
the next available GCUSD 5-minute bar after the EOD decision cutoff.
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

import pandas as pd
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

SCRIPT_PATH = Path(__file__).resolve()
AI_HEDGE_FUND_ROOT = SCRIPT_PATH.parents[1]
BASELINE_ROOT = SCRIPT_PATH.parents[2]
if str(AI_HEDGE_FUND_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_HEDGE_FUND_ROOT))

from src.backtesting.metrics import PerformanceMetricsCalculator
from src.backtesting.portfolio import Portfolio
from src.backtesting.trader import TradeExecutor
from src.backtesting.valuation import calculate_portfolio_value, compute_exposures
from src.data.gcusd_local import GCUSDLocalDataProvider
from src.main import run_hedge_fund
from src.tools.api import clear_data_provider, set_data_provider

DEFAULT_DAILY_OHLCV = (
    BASELINE_ROOT
    / "gold_futures"
    / "experiment_gcusd_inputs"
    / "project_inputs"
    / "ai-hedge-fund"
    / "gcusd_daily_ohlcv_20250630_20260630.csv"
)
DEFAULT_EXECUTION_BARS = (
    BASELINE_ROOT
    / "gold_futures"
    / "experiment_gcusd_inputs"
    / "project_inputs"
    / "event_trader"
    / "gcusd_5min_20250630_20260630.csv"
)
DEFAULT_NEWS_ARCHIVE = Path(
    r"code\.local\replay-gold-20260101_20260630-workspace\helpers\source_archive\web_search\gold"
)
DEFAULT_OUTPUT_DIR = BASELINE_ROOT / "experiments" / "ai_hedge_fund_gcusd_smoke"
SELECTED_ANALYSTS = ["technical_analyst", "news_sentiment_analyst"]
COMPLETE_MARKER_NAME = "complete.json"


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _normalize_position(snapshot: dict[str, Any], ticker: str) -> str:
    position = (snapshot.get("positions") or {}).get(ticker, {})
    long_shares = int(position.get("long", 0) or 0)
    short_shares = int(position.get("short", 0) or 0)
    net = long_shares - short_shares
    if net > 0:
        return "long"
    if net < 0:
        return "short"
    return "flat"


def _position_after_execution(snapshot: dict[str, Any], ticker: str) -> dict[str, Any]:
    position = (snapshot.get("positions") or {}).get(ticker, {})
    long_shares = int(position.get("long", 0) or 0)
    short_shares = int(position.get("short", 0) or 0)
    net_units = long_shares - short_shares
    return {
        "long_units": long_shares,
        "short_units": short_shares,
        "net_units": net_units,
        "position_sign": _normalize_position(snapshot, ticker),
    }


def _write_json_atomic(path: Path, payload: Any) -> None:
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def _estimate_cost(
    usage: dict[str, Any],
    *,
    input_price_per_mtok: float | None,
    output_price_per_mtok: float | None,
) -> float | None:
    if input_price_per_mtok is None or output_price_per_mtok is None:
        return None
    return (
        usage.get("tokens_in", 0) / 1_000_000 * input_price_per_mtok
        + usage.get("tokens_out", 0) / 1_000_000 * output_price_per_mtok
    )


def _load_execution_bars(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path, encoding="utf-8")
    required = {"datetime_utc", "open", "high", "low", "close", "volume"}
    missing = required - set(raw.columns)
    if missing:
        raise ValueError(f"Execution-bar file is missing required columns: {sorted(missing)}")
    raw["datetime_utc"] = pd.to_datetime(raw["datetime_utc"], errors="coerce", utc=True)
    for column in ("open", "high", "low", "close", "volume"):
        raw[column] = pd.to_numeric(raw[column], errors="coerce")
    raw = raw.dropna(subset=["datetime_utc", "open"]).sort_values("datetime_utc")
    return raw.reset_index(drop=True)


def _next_execution_bar(bars: pd.DataFrame, decision_at: datetime) -> dict[str, Any] | None:
    match = bars[bars["datetime_utc"] > decision_at]
    if match.empty:
        return None
    row = match.iloc[0]
    return {
        "datetime_utc": row["datetime_utc"].to_pydatetime(),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row["volume"]),
    }


def _build_dates(args: argparse.Namespace) -> list[str]:
    if args.dates:
        return args.dates
    if args.smoke_defaults:
        return ["2026-01-05", "2026-01-06"]
    return [
        ts.strftime("%Y-%m-%d")
        for ts in pd.bdate_range(args.start_date, args.end_date)
    ]


def _build_usage_summary(
    usage: dict[str, Any],
    *,
    args: argparse.Namespace,
) -> dict[str, Any]:
    estimated_cost = _estimate_cost(
        usage,
        input_price_per_mtok=args.input_price_per_mtok,
        output_price_per_mtok=args.output_price_per_mtok,
    )
    return {
        **usage,
        "estimated_cost_usd": estimated_cost,
        "cost_note": (
            "estimated from --input-price-per-mtok/--output-price-per-mtok"
            if estimated_cost is not None
            else "usage metadata missing or price flags not provided"
        ),
    }


def _news_audit_rows(
    provider: GCUSDLocalDataProvider,
    *,
    start_dt: datetime,
    end_dt: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    rows = provider.collect_news_entries(start_dt=start_dt, end_dt=end_dt, limit=limit)
    return [
        {
            "observation_id": item.get("observation_id"),
            "title": item.get("title"),
            "source_ref": item.get("source_ref"),
            "published_at": item.get("published_at"),
            "visible_at": item.get("visible_at"),
            "labels": item.get("labels") or [],
        }
        for item in rows
    ]


def _aggregate_usage(audits: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate = {
        "llm_calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_total": 0,
        "usage_observed": False,
        "calls_missing_usage": 0,
        "estimated_cost_usd": 0.0,
    }
    cost_observed = False
    for audit in audits:
        usage = audit.get("llm_usage", {})
        aggregate["llm_calls"] += int(usage.get("llm_calls", 0) or 0)
        aggregate["tokens_in"] += int(usage.get("tokens_in", 0) or 0)
        aggregate["tokens_out"] += int(usage.get("tokens_out", 0) or 0)
        aggregate["tokens_total"] += int(usage.get("tokens_total", 0) or 0)
        aggregate["calls_missing_usage"] += int(usage.get("calls_missing_usage", 0) or 0)
        aggregate["usage_observed"] = aggregate["usage_observed"] or bool(usage.get("usage_observed"))
        if usage.get("estimated_cost_usd") is not None:
            cost_observed = True
            aggregate["estimated_cost_usd"] += float(usage["estimated_cost_usd"])
    if not cost_observed:
        aggregate["estimated_cost_usd"] = None
    return aggregate


def _build_complete_marker(audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_id": audit["decision_id"],
        "trade_date": audit["trade_date"],
        "status": audit["status"],
        "finished_at_utc": audit["timing"]["finished_at_utc"],
        "marker_version": 1,
    }


def _audit_is_complete(audit: dict[str, Any], *, trade_date: str) -> bool:
    if not isinstance(audit, dict):
        return False
    required_keys = {
        "decision_id",
        "trade_date",
        "status",
        "decision_at",
        "timing",
        "raw_output",
        "llm_usage",
        "portfolio_after_execution",
        "normalized_position",
    }
    if any(key not in audit for key in required_keys):
        return False
    if audit.get("trade_date") != trade_date:
        return False
    if audit.get("decision_id") != f"ai-hedge-fund|GCUSD|{trade_date}":
        return False
    if audit.get("status") not in {"filled", "unfilled_no_next_bar"}:
        return False
    if not isinstance(audit.get("timing"), dict):
        return False
    if not isinstance(audit.get("llm_usage"), dict):
        return False
    if not isinstance(audit.get("portfolio_after_execution"), dict):
        return False
    if audit.get("status") == "filled":
        execution = audit.get("execution")
        if not isinstance(execution, dict):
            return False
        if execution.get("policy") != "next_available_gcusd_5min_bar_open":
            return False
        if "price_open" not in execution or "bar_time_utc" not in execution:
            return False
    return True


def _portfolio_point_from_audit(audit: dict[str, Any]) -> dict[str, Any] | None:
    point = audit.get("performance_point")
    if isinstance(point, dict) and point.get("Date"):
        rebuilt = dict(point)
        rebuilt["Date"] = pd.Timestamp(point["Date"]).to_pydatetime()
        return rebuilt

    if audit.get("status") != "filled":
        return None

    snapshot = audit.get("portfolio_after_execution")
    execution = audit.get("execution")
    if not isinstance(snapshot, dict) or not isinstance(execution, dict):
        return None

    execution_price = execution.get("price_open")
    bar_time = execution.get("bar_time_utc")
    if execution_price is None or bar_time is None:
        return None

    portfolio = Portfolio.from_snapshot(snapshot)
    total_value = audit.get("portfolio_value_after_execution")
    if total_value is None:
        total_value = calculate_portfolio_value(portfolio, {"GCUSD": float(execution_price)})
    exposures = compute_exposures(portfolio, {"GCUSD": float(execution_price)})
    return {
        "Date": pd.Timestamp(bar_time).to_pydatetime(),
        "Portfolio Value": float(total_value),
        "Long Exposure": exposures["Long Exposure"],
        "Short Exposure": exposures["Short Exposure"],
        "Gross Exposure": exposures["Gross Exposure"],
        "Net Exposure": exposures["Net Exposure"],
        "Long/Short Ratio": exposures["Long/Short Ratio"],
    }


def _load_completed_audit(day_dir: Path, *, trade_date: str, repair_legacy_marker: bool) -> dict[str, Any] | None:
    audit_path = day_dir / "audit.json"
    if not audit_path.exists():
        return None

    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not _audit_is_complete(audit, trade_date=trade_date):
        return None

    marker_path = day_dir / COMPLETE_MARKER_NAME
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            marker.get("decision_id") != audit["decision_id"]
            or marker.get("trade_date") != trade_date
            or marker.get("status") != audit["status"]
        ):
            return None
        return audit

    if repair_legacy_marker:
        _write_json_atomic(marker_path, _build_complete_marker(audit))
        return audit
    return None


def _load_completed_audits_prefix(
    output_dir: Path,
    trade_dates: list[str],
    *,
    repair_legacy_markers: bool,
) -> list[dict[str, Any]]:
    audits: list[dict[str, Any]] = []
    for trade_date in trade_dates:
        day_dir = output_dir / trade_date
        audit = _load_completed_audit(
            day_dir,
            trade_date=trade_date,
            repair_legacy_marker=repair_legacy_markers,
        )
        if audit is None:
            break
        audits.append(audit)
    return audits


def _build_portfolio(args: argparse.Namespace, existing_audits: list[dict[str, Any]]) -> Portfolio:
    if existing_audits:
        return Portfolio.from_snapshot(existing_audits[-1]["portfolio_after_execution"])
    return Portfolio(
        tickers=["GCUSD"],
        initial_cash=args.initial_cash,
        margin_requirement=args.margin_requirement,
    )


def _run_one_date(
    args: argparse.Namespace,
    *,
    trade_date: str,
    portfolio: Portfolio,
    executor: TradeExecutor,
    execution_bars: pd.DataFrame,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    decision_at = datetime.fromisoformat(f"{trade_date}T23:59:59+00:00")
    lookback_start = (
        pd.Timestamp(trade_date) - relativedelta(months=1)
    ).strftime("%Y-%m-%d")
    execution_bar = _next_execution_bar(execution_bars, decision_at)

    provider = GCUSDLocalDataProvider(
        daily_ohlcv_path=args.daily_ohlcv,
        news_archive_dir=args.news_archive,
        visible_through=decision_at,
    )
    set_data_provider(provider)
    started = datetime.now(timezone.utc)
    wall_start = time.perf_counter()
    try:
        result = run_hedge_fund(
            tickers=["GCUSD"],
            start_date=lookback_start,
            end_date=trade_date,
            portfolio=portfolio.get_snapshot(),
            show_reasoning=False,
            selected_analysts=SELECTED_ANALYSTS,
            model_name=args.model_name,
            model_provider=args.provider,
        )
    finally:
        clear_data_provider()
    elapsed = time.perf_counter() - wall_start
    finished = datetime.now(timezone.utc)

    usage = _build_usage_summary((result.get("audit") or {}).get("llm_usage", {}), args=args)
    decision = (result.get("decisions") or {}).get("GCUSD", {})
    analyst_signals = result.get("analyst_signals") or {}
    news_rows = _news_audit_rows(
        provider,
        start_dt=datetime.fromisoformat(f"{lookback_start}T00:00:00+00:00"),
        end_dt=decision_at,
        limit=args.news_limit,
    )
    audit = {
        "decision_id": f"ai-hedge-fund|GCUSD|{trade_date}",
        "baseline_id": "ai-hedge-fund",
        "target_key": "gold",
        "market_symbol": "GCUSD",
        "trade_date": trade_date,
        "decision_at": decision_at.isoformat(),
        "visible_through": decision_at.isoformat(),
        "input_bar_window": {
            "daily_lookback_start": lookback_start,
            "daily_visible_end": trade_date,
        },
        "input_evidence_ids": [row["observation_id"] for row in news_rows if row.get("observation_id")],
        "inputs": {
            "daily_ohlcv": str(args.daily_ohlcv),
            "execution_bars_5min": str(args.execution_bars),
            "news_archive": str(args.news_archive),
            "news_records_visible": news_rows,
        },
        "timing": {
            "started_at_utc": started.isoformat(),
            "finished_at_utc": finished.isoformat(),
            "elapsed_seconds": elapsed,
        },
        "model_name": args.model_name,
        "model_provider": args.provider,
        "adapter_version": "gcusd_local_v1",
        "action_semantics": "native_execution_instruction",
        "selected_analysts": SELECTED_ANALYSTS,
        "raw_output": decision,
        "analyst_signals": analyst_signals,
        "llm_usage": usage,
    }

    if execution_bar is None:
        portfolio_snapshot = portfolio.get_snapshot()
        audit["status"] = "unfilled_no_next_bar"
        audit["execution"] = None
        audit["normalized_position"] = _normalize_position(portfolio_snapshot, "GCUSD")
        audit["position_after_execution"] = _position_after_execution(portfolio_snapshot, "GCUSD")
        audit["portfolio_after_execution"] = portfolio_snapshot
        audit["portfolio_value_after_execution"] = None
        audit["performance_point"] = None
        return audit, None

    execution_price = float(execution_bar["open"])
    executed_qty = executor.execute_trade(
        "GCUSD",
        decision.get("action", "hold"),
        decision.get("quantity", 0),
        execution_price,
        portfolio,
    )
    portfolio_snapshot = portfolio.get_snapshot()
    total_value = calculate_portfolio_value(portfolio, {"GCUSD": execution_price})
    exposures = compute_exposures(portfolio, {"GCUSD": execution_price})
    normalized_position = _normalize_position(portfolio_snapshot, "GCUSD")
    point = {
        "Date": execution_bar["datetime_utc"],
        "Portfolio Value": total_value,
        "Long Exposure": exposures["Long Exposure"],
        "Short Exposure": exposures["Short Exposure"],
        "Gross Exposure": exposures["Gross Exposure"],
        "Net Exposure": exposures["Net Exposure"],
        "Long/Short Ratio": exposures["Long/Short Ratio"],
    }

    audit.update(
        {
            "status": "filled",
            "execution": {
                "policy": "next_available_gcusd_5min_bar_open",
                "bar_time_utc": execution_bar["datetime_utc"].isoformat(),
                "price_open": execution_price,
                "executed_quantity": executed_qty,
            },
            "normalized_position": normalized_position,
            "position_after_execution": _position_after_execution(portfolio_snapshot, "GCUSD"),
            "portfolio_after_execution": portfolio_snapshot,
            "portfolio_value_after_execution": total_value,
            "performance_point": {
                "Date": execution_bar["datetime_utc"].isoformat(),
                "Portfolio Value": total_value,
                "Long Exposure": exposures["Long Exposure"],
                "Short Exposure": exposures["Short Exposure"],
                "Gross Exposure": exposures["Gross Exposure"],
                "Net Exposure": exposures["Net Exposure"],
                "Long/Short Ratio": exposures["Long/Short Ratio"],
            },
        }
    )
    return audit, point


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", nargs="+", default=None)
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default="2026-06-30")
    parser.add_argument(
        "--smoke-defaults",
        action="store_true",
        help="Run the default two-day smoke dates (2026-01-05 and 2026-01-06).",
    )
    parser.add_argument("--provider", default="Anthropic")
    parser.add_argument("--model-name", default="MiniMax-M2.1")
    parser.add_argument("--daily-ohlcv", type=Path, default=DEFAULT_DAILY_OHLCV)
    parser.add_argument("--execution-bars", type=Path, default=DEFAULT_EXECUTION_BARS)
    parser.add_argument("--news-archive", type=Path, default=DEFAULT_NEWS_ARCHIVE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--initial-cash", type=float, default=100000.0)
    parser.add_argument("--margin-requirement", type=float, default=0.0)
    parser.add_argument("--news-limit", type=int, default=20)
    parser.add_argument("--input-price-per-mtok", type=float, default=None)
    parser.add_argument("--output-price-per-mtok", type=float, default=None)
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resume from the longest completed-date prefix already written in the output directory.",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv(AI_HEDGE_FUND_ROOT / ".env")
    load_dotenv(BASELINE_ROOT / ".env")
    args = parse_args()
    args.daily_ohlcv = args.daily_ohlcv.resolve()
    args.execution_bars = args.execution_bars.resolve()
    args.news_archive = args.news_archive.resolve()
    args.output_dir = args.output_dir.resolve()

    if not args.daily_ohlcv.exists():
        print(f"Daily OHLCV file not found: {args.daily_ohlcv}", file=sys.stderr)
        return 2
    if not args.execution_bars.exists():
        print(f"Execution-bar file not found: {args.execution_bars}", file=sys.stderr)
        return 2
    if not args.news_archive.exists():
        print(f"News archive directory not found: {args.news_archive}", file=sys.stderr)
        return 2

    if args.provider.lower() == "anthropic" and args.model_name.lower().startswith("minimax"):
        if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("MINIMAX_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = os.environ["MINIMAX_API_KEY"]
        os.environ.setdefault("ANTHROPIC_BASE_URL", "https://api.minimaxi.com/anthropic")

    if args.provider.lower() == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("Missing ANTHROPIC_API_KEY (or MINIMAX_API_KEY for MiniMax Anthropic compatibility).", file=sys.stderr)
        return 2

    dates = _build_dates(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    execution_bars = _load_execution_bars(args.execution_bars)
    existing_audits = (
        _load_completed_audits_prefix(
            args.output_dir,
            dates,
            repair_legacy_markers=True,
        )
        if args.resume
        else []
    )
    completed_dates = {audit["trade_date"] for audit in existing_audits}
    portfolio = _build_portfolio(args, existing_audits)
    executor = TradeExecutor()

    if existing_audits:
        print(
            f"Resuming from {existing_audits[-1]['trade_date']} "
            f"(skipping {len(existing_audits)} completed day(s))."
        )

    for trade_date in dates:
        if trade_date in completed_dates:
            print(f"Skipping completed date {trade_date}.")
            continue
        print(f"Running GCUSD AI Hedge Fund baseline for {trade_date}...")
        audit, _ = _run_one_date(
            args,
            trade_date=trade_date,
            portfolio=portfolio,
            executor=executor,
            execution_bars=execution_bars,
        )
        day_dir = args.output_dir / trade_date
        day_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(day_dir / "audit.json", audit)
        _write_json_atomic(day_dir / COMPLETE_MARKER_NAME, _build_complete_marker(audit))
        print(
            f"  action={audit['raw_output'].get('action', 'hold')} "
            f"position={audit['position_after_execution']['position_sign']} "
            f"elapsed={audit['timing']['elapsed_seconds']:.1f}s "
            f"llm_calls={audit['llm_usage'].get('llm_calls', 0)} "
            f"tokens_total={audit['llm_usage'].get('tokens_total', 0)}"
        )

    completed_audits = _load_completed_audits_prefix(
        args.output_dir,
        dates,
        repair_legacy_markers=False,
    )
    completed_points = [
        point
        for audit in completed_audits
        if (point := _portfolio_point_from_audit(audit)) is not None
    ]
    metrics = None
    if len(completed_points) > 3:
        metrics = PerformanceMetricsCalculator().compute_metrics(completed_points)

    aggregate_usage = _aggregate_usage(completed_audits)
    summary = {
        "baseline": "ai-hedge-fund",
        "symbol": "GCUSD",
        "reported_window": {"start": args.start_date, "end": args.end_date},
        "selected_analysts": SELECTED_ANALYSTS,
        "model": {"provider": args.provider, "model_name": args.model_name},
        "resume": {
            "enabled": args.resume,
            "resumed_completed_days": len(existing_audits),
        },
        "aggregate_llm_usage": aggregate_usage,
        "performance_metrics": metrics,
        "final_portfolio": portfolio.get_snapshot(),
        "runs": completed_audits,
    }
    cost_report = {
        "baseline": "ai-hedge-fund",
        "symbol": "GCUSD",
        "llm_usage": aggregate_usage,
        "run_count": len(completed_audits),
        "filled_count": len([audit for audit in completed_audits if audit.get("status") == "filled"]),
    }

    _write_json_atomic(args.output_dir / "summary.json", summary)
    _write_json_atomic(args.output_dir / "cost_report.json", cost_report)
    print(f"Audit summary written to {args.output_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
