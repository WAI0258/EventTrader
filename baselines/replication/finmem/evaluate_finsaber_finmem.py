from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN_DIR = (
    ROOT
    / "replication"
    / "finmem"
    / "runs"
    / "finsaber_finmem_tsla_20221006_20230410_m2_1_qinzhi_eod"
)
DEFAULT_PRICE_CSV = (
    ROOT
    / "replication"
    / "data"
    / "finmem_tsla_paper"
    / "finsaber_finmem"
    / "price"
    / "tsla_adjusted_close_20210817_20230411.csv"
)


def _read_audits(run_dir: Path) -> list[dict[str, Any]]:
    audits = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((run_dir / "days").glob("*/audit.json"))
    ]
    if not audits:
        raise ValueError(f"No daily audits found under {run_dir}")
    return audits


def _read_prices(path: Path) -> dict[date, float]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            date.fromisoformat(row["date"]): float(row["close"])
            for row in csv.DictReader(handle)
        }


def _metrics(daily_returns: list[float]) -> dict[str, float]:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for daily_return in daily_returns:
        equity *= 1.0 + daily_return
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    volatility = statistics.stdev(daily_returns) * math.sqrt(252) if len(daily_returns) > 1 else 0.0
    sharpe = (
        statistics.mean(daily_returns) / statistics.stdev(daily_returns) * math.sqrt(252)
        if len(daily_returns) > 1 and statistics.stdev(daily_returns) > 0
        else 0.0
    )
    return {
        "cumulative_return_pct": (equity - 1.0) * 100.0,
        "sharpe": sharpe,
        "max_drawdown_pct": max_drawdown * 100.0,
        "annualized_volatility_pct": volatility * 100.0,
    }


def _round_metrics(metrics: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 3) for key, value in metrics.items()}


def evaluate(*, run_dir: Path, price_csv: Path, cost_bps: float) -> dict[str, Any]:
    audits = _read_audits(run_dir)
    prices = _read_prices(price_csv)
    decision_days = [date.fromisoformat(audit["trade_date"]) for audit in audits]
    if any(day not in prices for day in decision_days):
        raise ValueError("Price CSV is missing a decision date")

    # FinSaber's outer wrapper is long/flat: buy enters long, sell exits, hold preserves.
    target_position: dict[date, int] = {}
    position = 0
    transitions: list[dict[str, Any]] = []
    actions = Counter()
    for decision_day, audit in zip(decision_days, audits):
        action = str(audit["decision"]["native_action"])
        actions[action] += 1
        next_position = 1 if action == "buy" else 0 if action == "sell" else position
        if next_position != position:
            transitions.append(
                {
                    "decision_date": decision_day.isoformat(),
                    "native_action": action,
                    "target_position": "long" if next_position else "flat",
                    "decision_close": prices[decision_day],
                }
            )
        position = next_position
        target_position[decision_day] = position

    eod_next_close_returns: list[float] = []
    eod_next_close_cost_returns: list[float] = []
    compatibility_same_close_returns: list[float] = []
    position = 0
    trade_count = 0
    for index in range(len(decision_days) - 1):
        current_day = decision_days[index]
        next_day = decision_days[index + 1]

        # An EOD decision made on D fills at D+1 close, then earns D+1 -> D+2.
        # Therefore the first test interval remains flat and uses no decision yet.
        trade_cost = 0.0
        if index > 0:
            desired_position = target_position[decision_days[index - 1]]
            if desired_position != position:
                position = desired_position
                trade_count += 1
                trade_cost = cost_bps / 10_000.0
        gross_return = position * (prices[next_day] / prices[current_day] - 1.0)
        eod_next_close_returns.append(gross_return)
        eod_next_close_cost_returns.append((1.0 + gross_return) * (1.0 - trade_cost) - 1.0)

        # Compatibility only: the original legacy wrapper submits at today's price.
        compatibility_position = target_position[current_day]
        compatibility_same_close_returns.append(
            compatibility_position * (prices[next_day] / prices[current_day] - 1.0)
        )

    buy_and_hold_returns = [
        prices[decision_days[index]] / prices[decision_days[index - 1]] - 1.0
        for index in range(1, len(decision_days))
    ]
    return {
        "schema_version": 1,
        "run_dir": str(run_dir),
        "methodology": {
            "action_mapping": "buy -> long, sell -> flat, hold -> preserve",
            "starting_position": "flat; FinMem warmup virtual holdings are not traded capital",
            "execution": "primary: EOD decision fills at next available close; compatibility: same-close legacy wrapper",
            "primary_cost": "zero for paper-window comparison",
            "cost_sensitivity_bps_per_fill": cost_bps,
        },
        "window": {
            "start": decision_days[0].isoformat(),
            "end": decision_days[-1].isoformat(),
            "decision_count": len(decision_days),
            "mark_to_market_close_start": prices[decision_days[0]],
            "mark_to_market_close_end": prices[decision_days[-1]],
        },
        "native_actions": dict(sorted(actions.items())),
        "trade_count": trade_count,
        "position_transitions": transitions,
        "metrics": {
            "finmem_long_flat_eod_next_close_zero_cost": _round_metrics(
                _metrics(eod_next_close_returns)
            ),
            f"finmem_long_flat_eod_next_close_{cost_bps:g}bps_per_fill": _round_metrics(
                _metrics(eod_next_close_cost_returns)
            ),
            "finmem_long_flat_legacy_same_close_zero_cost": _round_metrics(
                _metrics(compatibility_same_close_returns)
            ),
            "buy_and_hold_zero_cost": _round_metrics(_metrics(buy_and_hold_returns)),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate FinMem actions on the FINSABER TSLA paper window.")
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--price-csv", type=Path, default=DEFAULT_PRICE_CSV)
    parser.add_argument("--cost-bps", type=float, default=15.0)
    args = parser.parse_args()

    report = evaluate(
        run_dir=args.run_dir.resolve(),
        price_csv=args.price_csv.resolve(),
        cost_bps=args.cost_bps,
    )
    output_path = args.run_dir.resolve() / "execution_report.json"
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Execution report: {output_path}")
    print(json.dumps(report["metrics"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
