"""Evaluate a completed AAPL TradingAgents run with explicit adapters."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from execution_adapter import (
    directional_instruction,
    load_run,
    manual_pm_full_initial_instruction,
    run_adapter,
    text_faithful_instruction,
    trader_direct_long_only_instruction,
    _write_csv,
)


HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
DEFAULT_RUN = HERE / "runs" / "aapl_minimax_m21_2024q1"
PRICE = WORKSPACE / "replication" / "data" / "tradingagents_paper_2024q1" / "price" / "aapl_daily_futu_qfq_20240101_20240329.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--price", type=Path, default=PRICE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_root = args.run_dir.resolve()
    audits, bars = load_run(run_root, args.price)
    text_instructions = []
    directional_instructions = []
    trader_long_only_instructions = []
    manual_pm_instructions = []
    mapping_rows = []
    text_weight = 0.0
    directional_weight = 0.0
    trader_long_only_weight = 0.0

    for audit in audits:
        date = audit["trade_date"]
        report_dir = run_root / date / "reports"
        pm_text = (report_dir / "5_portfolio" / "decision.md").read_text(encoding="utf-8")
        trader_text = (report_dir / "3_trading" / "trader.md").read_text(encoding="utf-8")
        text_item = text_faithful_instruction(date, audit["decision"], trader_text, pm_text, text_weight)
        directional_item = directional_instruction(date, trader_text, directional_weight)
        trader_long_only_item = trader_direct_long_only_instruction(date, trader_text, trader_long_only_weight)
        if text_item.target_weight is not None:
            text_weight = text_item.target_weight
        if directional_item.target_weight is not None:
            directional_weight = directional_item.target_weight
        if trader_long_only_item.target_weight is not None:
            trader_long_only_weight = trader_long_only_item.target_weight
        text_instructions.append(text_item)
        directional_instructions.append(directional_item)
        trader_long_only_instructions.append(trader_long_only_item)
        manual_item = manual_pm_full_initial_instruction(date)
        manual_pm_instructions.append(manual_item)
        mapping_rows.append({
            "date": date,
            "pm_rating": audit["decision"],
            "text_target_weight": text_item.target_weight,
            "text_stop_loss": text_item.stop_loss,
            "text_mapping_reason": text_item.mapping_reason,
            "directional_target_weight": directional_item.target_weight,
            "directional_stop_loss": directional_item.stop_loss,
            "directional_mapping_reason": directional_item.mapping_reason,
            "trader_long_only_target_weight": trader_long_only_item.target_weight,
            "trader_long_only_stop_loss": trader_long_only_item.stop_loss,
            "trader_long_only_mapping_reason": trader_long_only_item.mapping_reason,
            "manual_pm_target_weight": manual_item.target_weight,
            "manual_pm_scale_current": manual_item.scale_current,
            "manual_pm_stop_loss": manual_item.stop_loss,
            "manual_pm_stop_on_close": manual_item.stop_on_close,
            "manual_pm_mapping_reason": manual_item.mapping_reason,
        })

    output = run_root / "execution"
    _write_csv(output / "mapping_audit.csv", mapping_rows)
    text_summary = run_adapter("text_faithful_long_only", text_instructions, bars, output / "text_faithful_long_only")
    directional_summary = run_adapter("fixed_notional_directional", directional_instructions, bars, output / "fixed_notional_directional")
    trader_long_only_summary = run_adapter("trader_direct_long_only", trader_long_only_instructions, bars, output / "trader_direct_long_only")
    manual_pm_long_only_summary = run_adapter("manual_pm_full_initial_long_only", manual_pm_instructions, bars, output / "manual_pm_full_initial_long_only", initial_weight=1.0, update_stop_on_hold=True)
    manual_pm_long_short_summary = run_adapter("manual_pm_full_initial_long_short", manual_pm_instructions, bars, output / "manual_pm_full_initial_long_short", initial_weight=1.0, update_stop_on_hold=True)
    manual_pm_zero_initial_long_only_summary = run_adapter("manual_pm_zero_initial_long_only", manual_pm_instructions, bars, output / "manual_pm_zero_initial_long_only", update_stop_on_hold=True)
    manual_pm_zero_initial_long_short_summary = run_adapter("manual_pm_zero_initial_long_short", manual_pm_instructions, bars, output / "manual_pm_zero_initial_long_short", update_stop_on_hold=True)
    (output / "README.json").write_text(json.dumps({
        "scope": "post-hoc deterministic evaluation of an existing LLM run; not upstream TradingAgents",
        "text_faithful_long_only": "Uses explicit portfolio-manager prose first. Underweight reduces existing long exposure and never opens a short. Buy/Overweight without an explicit size use a fixed 10% fallback entry.",
        "fixed_notional_directional": "Uses Trader Buy/Hold/Sell as +100%/maintain/-100% target exposure.",
        "trader_direct_long_only": "Uses Trader Buy/Hold/Sell as +100%/maintain/0% target exposure; it never opens a short.",
        "manual_pm_full_initial_long_only": "Hand-transcribed unconditional PM sizing and stops. Starts 100% long AAPL at the first open. Conditional wording without an order-persistence rule is recorded but not executed.",
        "manual_pm_full_initial_long_short": "Identical to the manual long-only scenario because no PM report explicitly directs a new short position.",
        "manual_pm_zero_initial_long_only": "Same hand-transcribed PM mapping, but starts with 0% AAPL because the paper does not disclose an initial position.",
        "manual_pm_zero_initial_long_short": "Identical to the zero-initial manual long-only scenario because no PM report explicitly directs a new short position.",
        "common_execution": "Initial equity 100000; next-trading-day open execution; same-day OHLC stop check; zero costs because the paper does not disclose costs.",
        "summaries": [text_summary, directional_summary, trader_long_only_summary, manual_pm_long_only_summary, manual_pm_long_short_summary, manual_pm_zero_initial_long_only_summary, manual_pm_zero_initial_long_short_summary],
    }, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "summaries": [text_summary, directional_summary, trader_long_only_summary, manual_pm_long_only_summary, manual_pm_long_short_summary, manual_pm_zero_initial_long_only_summary, manual_pm_zero_initial_long_short_summary]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
