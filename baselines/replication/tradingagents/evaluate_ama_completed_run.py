"""Score a completed TradingAgents TSLA run under explicit AMA mappings."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter
from pathlib import Path


HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
DEFAULT_RUN = HERE / "runs" / "tsla_minimax_m21_google_rss_sec_ama_2025q3"
DEFAULT_PRICE = (
    WORKSPACE
    / "replication"
    / "data"
    / "tradingagents_ama_tsla_2025"
    / "price"
    / "tsla_daily_futu_qfq_20241001_20251024.csv"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--price", type=Path, default=DEFAULT_PRICE)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def _read_prices(path: Path) -> dict[str, float]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    closes: dict[str, float] = {}
    for row in rows:
        closes[row["time_key"][:10]] = float(row["close"])
    return closes


def _trader_action(report: Path) -> str | None:
    text = report.read_text(encoding="utf-8")
    match = re.search(r"\*\*Action\*\*:\s*(Buy|Sell|Hold)", text, re.I)
    return match.group(1).title() if match else None


def _pm_fallback(rating: str) -> str:
    return {
        "Buy": "Buy",
        "Overweight": "Buy",
        "Sell": "Sell",
        "Underweight": "Sell",
        "Hold": "Hold",
    }[rating]


def _metrics(weights: list[int], returns: list[float]) -> dict[str, float | int]:
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    strategy_returns: list[float] = []
    for weight, market_return in zip(weights, returns, strict=True):
        daily = weight * market_return
        strategy_returns.append(daily)
        equity *= 1.0 + daily
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
    mean = sum(strategy_returns) / len(strategy_returns)
    variance = (
        sum((item - mean) ** 2 for item in strategy_returns) / (len(strategy_returns) - 1)
        if len(strategy_returns) > 1
        else 0.0
    )
    std = math.sqrt(variance)
    return {
        "cumulative_return_pct": round((equity - 1.0) * 100.0, 6),
        "annualized_sharpe_rf0": round(0.0 if std == 0.0 else mean / std * math.sqrt(252), 6),
        "max_drawdown_pct": round(drawdown * 100.0, 6),
        "long_days": weights.count(1),
        "short_days": weights.count(-1),
        "flat_days": weights.count(0),
    }


def _direct(values: list[str], mapping: dict[str, int]) -> list[int]:
    return [mapping.get(value, 0) for value in values]


def _stateful(values: list[str], mapping: dict[str, int]) -> list[int]:
    weight = 0
    output: list[int] = []
    for value in values:
        if value in mapping:
            weight = mapping[value]
        output.append(weight)
    return output


def build_summary(run_root: Path, price_path: Path) -> tuple[dict, list[dict]]:
    audits = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(run_root.glob("2025-*/audit.json"))]
    if not audits:
        raise ValueError(f"No daily audit receipts found under {run_root}")
    closes = _read_prices(price_path)
    dates = sorted(closes)
    previous_close = {date: closes[dates[index - 1]] for index, date in enumerate(dates) if index}
    rows: list[dict] = []
    for audit in audits:
        date = audit["trade_date"]
        rating = audit["decision"].title()
        action = _trader_action(run_root / date / "reports" / "3_trading" / "trader.md")
        action_source = "trader_report"
        if action is None:
            action = _pm_fallback(rating)
            action_source = "final_pm_rating_fallback"
        if date not in previous_close:
            raise ValueError(f"No prior close available for {date}")
        rows.append(
            {
                "date": date,
                "pm_rating": rating,
                "trader_action": action,
                "trader_action_source": action_source,
                "previous_close": previous_close[date],
                "close": closes[date],
                "market_return": closes[date] / previous_close[date] - 1.0,
            }
        )

    mapping_rules = {
        "ama_official_trader_3way": lambda: _direct(
            [row["trader_action"] for row in rows], {"Buy": 1, "Sell": -1, "Hold": 0}
        ),
        "pm_5tier_collapsed_3way": lambda: _direct(
            [row["pm_rating"] for row in rows],
            {"Buy": 1, "Overweight": 1, "Sell": -1, "Underweight": -1, "Hold": 0},
        ),
        "pm_extremes_only_3way": lambda: _direct(
            [row["pm_rating"] for row in rows], {"Buy": 1, "Sell": -1}
        ),
        "pm_5tier_long_only": lambda: _direct(
            [row["pm_rating"] for row in rows], {"Buy": 1, "Overweight": 1}
        ),
        "pm_buy_only_long_only": lambda: _direct([row["pm_rating"] for row in rows], {"Buy": 1}),
        "pm_stateful_long_only": lambda: _stateful(
            [row["pm_rating"] for row in rows],
            {"Buy": 1, "Overweight": 1, "Sell": 0, "Underweight": 0},
        ),
        "pm_stateful_long_short": lambda: _stateful(
            [row["pm_rating"] for row in rows],
            {"Buy": 1, "Overweight": 1, "Sell": -1, "Underweight": -1},
        ),
    }
    for name, make_weights in mapping_rules.items():
        for row, weight in zip(rows, make_weights(), strict=True):
            row[name] = weight

    def score(window_rows: list[dict]) -> dict:
        returns = [row["market_return"] for row in window_rows]
        return {
            "sessions": len(window_rows),
            "ratings": dict(Counter(row["pm_rating"] for row in window_rows)),
            "trader_actions": dict(Counter(row["trader_action"] for row in window_rows)),
            "fallback_dates": [row["date"] for row in window_rows if row["trader_action_source"] != "trader_report"],
            "buy_and_hold": _metrics([1] * len(window_rows), returns),
            "mappings": {
                name: _metrics([row[name] for row in window_rows], returns)
                for name in mapping_rules
            },
        }

    return {
        "protocol": "AMA PAI daily scoring: r_t = w_t * (P_t - P_t-1) / P_t-1; no fees",
        "canonical_mapping": "AMA-paper PAI formula proxy from final PM: Buy/Overweight=BUY, Sell/Underweight=SELL, Hold=HOLD; scored as +1/-1/0.",
        "mapping_definitions": {
        "ama_official_trader_3way": "Internal Trader proposal BUY=+1, SELL=-1, HOLD=0; sensitivity only.",
        "pm_5tier_collapsed_3way": "AMA-paper PAI formula proxy from final PM: Buy/Overweight=+1, Sell/Underweight=-1, Hold=0.",
            "pm_extremes_only_3way": "Only PM Buy/Sell trade; Overweight/Underweight/Hold are flat.",
            "pm_5tier_long_only": "PM Buy/Overweight=+1; all other ratings are flat.",
            "pm_buy_only_long_only": "Only PM Buy=+1; all other ratings are flat.",
            "pm_stateful_long_only": "Buy/Overweight sets long; Sell/Underweight sets flat; Hold preserves prior state.",
            "pm_stateful_long_short": "Buy/Overweight sets long; Sell/Underweight sets short; Hold preserves prior state.",
        },
        "paper_window_aug1_sep30": score([row for row in rows if row["date"] <= "2025-09-30"]),
        "full_window_aug1_oct24": score(rows),
    }, rows


def main() -> int:
    args = parse_args()
    run_root = args.run_dir.resolve()
    output = (args.output_dir or run_root / "execution_ama").resolve()
    summary, rows = build_summary(run_root, args.price.resolve())
    output.mkdir(parents=True, exist_ok=True)
    with (output / "mapping_audit.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (output / "mapping_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "canonical": summary["full_window_aug1_oct24"]["mappings"]["pm_5tier_collapsed_3way"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
