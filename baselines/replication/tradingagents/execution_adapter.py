"""Deterministic execution adapters for an already-completed TradingAgents run.

These adapters are evaluation scaffolding, not part of upstream TradingAgents.
They make every assumption visible in the generated mapping audit.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd


INITIAL_EQUITY = 100_000.0
FALLBACK_ENTRY_WEIGHT = 0.10


@dataclass
class Instruction:
    date: str
    target_weight: float | None
    stop_loss: float | None
    mapping_reason: str
    scale_current: float | None = None
    stop_on_close: bool = False


def _mean_percent(match: re.Match[str]) -> float:
    return (float(match.group(1)) + float(match.group(2))) / 200.0


def _find_stop(text: str) -> float | None:
    match = re.search(r"\bstop(?:-loss)?(?:es)?\s*(?:at|below|near)?\s*\$?(\d+(?:\.\d+)?)", text, re.I)
    return float(match.group(1)) if match else None


def _trader_action(text: str) -> str | None:
    match = re.search(r"\*\*Action\*\*:\s*(Buy|Sell|Hold)", text, re.I)
    return match.group(1).title() if match else None


def _rating(text: str, fallback: str) -> str:
    match = re.search(r"\*\*Rating\*\*:\s*(Buy|Sell|Hold|Overweight|Underweight)", text, re.I)
    return match.group(1).title() if match else fallback.title()


def text_faithful_instruction(date: str, decision: str, trader_text: str, pm_text: str, current_weight: float) -> Instruction:
    """Map explicit prose sizing first; only then use a documented small entry fallback.

    Underweight is deliberately never mapped to a new short.  The completed
    M2.1 reports repeatedly describe it as reducing an existing long position.
    """

    rating = _rating(pm_text, decision)
    if rating == "Hold":
        return Instruction(date, None, None, "Hold preserves current long-only exposure")

    text = f"{trader_text}\n{pm_text}"
    stop = _find_stop(pm_text) or _find_stop(trader_text)

    exit_match = re.search(r"\bexit\s+(?:approximately\s+)?(\d+(?:\.\d+)?)%", text, re.I)
    if exit_match:
        target = current_weight * (1.0 - float(exit_match.group(1)) / 100.0)
        return Instruction(date, target, stop, f"exit {exit_match.group(1)}% of existing weight")

    reduce_match = re.search(r"\b(?:reduce|trim)\b.{0,45}?(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)%", text, re.I | re.S)
    if reduce_match:
        target = current_weight * (1.0 - _mean_percent(reduce_match))
        return Instruction(date, target, stop, "reduce existing weight by stated range")

    allocation_match = re.search(
        r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)%\s*(?:of (?:a |the )?(?:diversified )?portfolio|portfolio weight|of tech allocation|allocation)",
        text,
        re.I,
    )
    if allocation_match:
        return Instruction(date, _mean_percent(allocation_match), stop, "explicit allocation range")

    if re.search(r"\b(?:initial )?half[- ]position\b", text, re.I):
        return Instruction(date, 0.50, stop, "explicit half-position")

    if rating == "Sell":
        return Instruction(date, 0.0, stop, "Sell rating exits long-only exposure")
    if rating in {"Buy", "Overweight"}:
        if current_weight >= FALLBACK_ENTRY_WEIGHT:
            return Instruction(date, None, stop, "existing long already exceeds fixed 10% fallback entry")
        return Instruction(date, FALLBACK_ENTRY_WEIGHT, stop, "fixed 10% fallback entry; no explicit size")
    return Instruction(date, None, stop, f"{rating} preserves current long-only exposure")


def directional_instruction(date: str, trader_text: str, current_weight: float) -> Instruction:
    """Fixed-notional long/short sensitivity proxy using only Trader action."""

    action = _trader_action(trader_text)
    stop = _find_stop(trader_text)
    if action == "Buy":
        return Instruction(date, 1.0, stop, "Trader Buy -> +100% target exposure")
    if action == "Sell":
        return Instruction(date, -1.0, stop, "Trader Sell -> -100% target exposure")
    return Instruction(date, None, stop, "Trader Hold preserves exposure")


def trader_direct_long_only_instruction(date: str, trader_text: str, current_weight: float) -> Instruction:
    """Use only the Trader action, with Sell flattening rather than shorting."""

    action = _trader_action(trader_text)
    stop = _find_stop(trader_text)
    if action == "Buy":
        return Instruction(date, 1.0, stop, "Trader Buy -> +100% target exposure")
    if action == "Sell":
        return Instruction(date, 0.0, stop, "Trader Sell -> 0% target exposure")
    return Instruction(date, None, stop, "Trader Hold preserves long-only exposure")


def manual_pm_full_initial_instruction(date: str) -> Instruction:
    """Hand-transcribed, unconditional PM directions for the full-initial-long scenario.

    Conditional phrases without a disclosed order-persistence rule are left as
    no-ops rather than inventing an order.  ``half-position`` is interpreted
    as 50% of portfolio equity, and the tech-allocation range as portfolio
    weight; both interpretations are recorded in the audit output.
    """

    instructions = {
        "2024-01-03": Instruction(date, None, 175.0, "PM: reduce exposure 25-30% from current levels", scale_current=0.725),
        "2024-01-04": Instruction(date, None, 177.55, "PM: closing-basis stop at 177.55", stop_on_close=True),
        "2024-01-08": Instruction(date, None, 177.80, "PM: stop loss at 177.80"),
        "2024-01-11": Instruction(date, None, 180.0, "PM: controlled 15-20% reduction from current exposure", scale_current=0.825),
        "2024-01-25": Instruction(date, 0.09, 186.0, "PM: 8-10% tech allocation interpreted as 9% portfolio weight"),
        "2024-02-01": Instruction(date, 0.04, 179.50, "PM: 3-5% diversified-portfolio allocation interpreted as 4%"),
        "2024-02-09": Instruction(date, None, 180.0, "PM: stop loss at 180"),
        "2024-02-12": Instruction(date, None, 182.0, "PM: modified stop at 182"),
        "2024-02-13": Instruction(date, 0.175, 178.0, "PM: initiate 15-20% portfolio weight interpreted as 17.5%"),
        "2024-02-14": Instruction(date, None, 181.04, "PM: exit approximately 70%, retain core", scale_current=0.30),
        "2024-02-20": Instruction(date, None, 175.0, "PM: keep stop loss at 175"),
        "2024-02-21": Instruction(date, None, 176.50, "PM: adjust stop loss to 176.50"),
        "2024-02-22": Instruction(date, 0.025, None, "PM: modest position should remain 2-3% max, interpreted as 2.5%"),
        "2024-02-28": Instruction(date, None, 165.0, "PM: stop loss at 165"),
        "2024-02-29": Instruction(date, None, 170.0, "PM: reduce exposure 25-30% from current levels", scale_current=0.725),
        "2024-03-05": Instruction(date, None, 160.0, "PM: reduce allocation 20-25% from current levels", scale_current=0.775),
        "2024-03-06": Instruction(date, None, 165.0, "PM: stop loss at 165"),
        "2024-03-08": Instruction(date, None, 165.0, "PM: stop at 165"),
        "2024-03-11": Instruction(date, None, 165.0, "PM: stop losses at 165; conditional rally trim is not an unconditional order"),
        "2024-03-12": Instruction(date, None, 168.0, "PM: stop at 168; core-position size is not disclosed"),
        "2024-03-13": Instruction(date, None, 181.82, "PM: reduce exposure 25-30% from current levels", scale_current=0.725),
        "2024-03-18": Instruction(date, None, 160.0, "PM: stop loss at 160"),
        "2024-03-25": Instruction(date, None, 160.0, "PM: stop loss at 160"),
        "2024-03-26": Instruction(date, 0.50, 164.0, "PM: initial half-position interpreted as 50% portfolio weight"),
        "2024-03-27": Instruction(date, None, 166.0, "PM: stop at 166; scaled-long size is not disclosed"),
    }
    return instructions.get(date, Instruction(date, None, None, "No unconditional numeric PM order"))


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _metrics(equity: pd.Series, initial_equity: float | None = None) -> dict[str, float]:
    starting_equity = float(equity.iloc[0]) if initial_equity is None else initial_equity
    daily = equity.pct_change().dropna()
    sharpe = 0.0 if daily.std(ddof=1) == 0 else float(daily.mean() / daily.std(ddof=1) * (252**0.5))
    equity_path = equity if initial_equity is None else pd.concat([pd.Series([initial_equity]), equity], ignore_index=True)
    drawdown = equity_path / equity_path.cummax() - 1.0
    return {
        "initial_equity": starting_equity,
        "final_equity": float(equity.iloc[-1]),
        "total_return_pct": float((equity.iloc[-1] / starting_equity - 1.0) * 100.0),
        "annualized_sharpe": sharpe,
        "max_drawdown_pct": float(drawdown.min() * 100.0),
    }


def run_adapter(
    name: str,
    instructions: list[Instruction],
    bars: pd.DataFrame,
    output: Path,
    initial_weight: float = 0.0,
    update_stop_on_hold: bool = False,
) -> dict:
    """Trade on the next bar open, mark at close, and honour a single stop level."""

    by_date = {item.date: item for item in instructions}
    cash = INITIAL_EQUITY
    shares = 0.0
    stop_loss: float | None = None
    stop_on_close = False
    trades: list[dict] = []
    daily: list[dict] = []
    current_weight = 0.0

    for index, bar in bars.reset_index(drop=True).iterrows():
        date = str(bar["date"])
        if index == 0 and initial_weight:
            shares = INITIAL_EQUITY * initial_weight / float(bar["open"])
            cash -= shares * float(bar["open"])
            trades.append({"date": date, "event": "initial_allocation", "price": float(bar["open"]), "shares_delta": shares, "target_weight": initial_weight, "mapping_reason": "scenario initial allocation"})
        # Yesterday's recommendation is executable at today's open.
        previous = str(bars.iloc[index - 1]["date"]) if index else None
        instruction = by_date.get(previous) if previous else None
        target_weight = None
        if instruction and instruction.target_weight is not None:
            target_weight = instruction.target_weight
        elif instruction and instruction.scale_current is not None:
            equity_at_open = cash + shares * float(bar["open"])
            current_weight_at_open = 0.0 if equity_at_open == 0 else shares * float(bar["open"]) / equity_at_open
            target_weight = current_weight_at_open * instruction.scale_current
        if instruction and target_weight is not None:
            equity_at_open = cash + shares * float(bar["open"])
            target_value = equity_at_open * target_weight
            target_shares = target_value / float(bar["open"])
            delta = target_shares - shares
            if abs(delta) > 1e-12:
                cash -= delta * float(bar["open"])
                shares = target_shares
                trades.append({
                    "date": date,
                    "event": "target_rebalance",
                    "price": float(bar["open"]),
                    "shares_delta": delta,
                    "target_weight": target_weight,
                    "mapping_reason": instruction.mapping_reason,
                })
        if instruction and instruction.stop_loss is not None and (target_weight is not None or update_stop_on_hold):
            stop_loss = instruction.stop_loss
            stop_on_close = instruction.stop_on_close

        if shares > 0 and stop_loss is not None and stop_loss >= float(bar["open"]):
            stop_loss = None  # A stop above a long entry is a short-stop, not a long exit.
        stop_hit = False
        if shares > 0 and stop_loss is not None:
            stop_hit = float(bar["close"]) <= stop_loss if stop_on_close else float(bar["low"]) <= stop_loss
        if shares > 0 and stop_loss is not None and stop_hit:
            fill = float(bar["close"]) if stop_on_close else min(float(bar["open"]), stop_loss)
            cash += shares * fill
            event = "long_stop_close" if stop_on_close else "long_stop"
            trades.append({"date": date, "event": event, "price": fill, "shares_delta": -shares, "target_weight": 0.0, "mapping_reason": "stop loss"})
            shares = 0.0
            stop_loss = None
            stop_on_close = False
        elif shares < 0 and stop_loss is not None and float(bar["high"]) >= stop_loss:
            fill = max(float(bar["open"]), stop_loss)
            cash += shares * fill
            trades.append({"date": date, "event": "short_stop", "price": fill, "shares_delta": -shares, "target_weight": 0.0, "mapping_reason": "stop loss"})
            shares = 0.0
            stop_loss = None

        equity = cash + shares * float(bar["close"])
        current_weight = 0.0 if equity == 0 else shares * float(bar["close"]) / equity
        daily.append({"date": date, "close": float(bar["close"]), "cash": cash, "shares": shares, "equity": equity, "realized_weight": current_weight, "stop_loss": stop_loss})

    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "trades.csv", trades)
    _write_csv(output / "daily_equity.csv", daily)
    summary = _metrics(pd.Series([row["equity"] for row in daily]), INITIAL_EQUITY if initial_weight else None)
    summary.update({"adapter": name, "trade_events": len(trades), "cost_bps": 0.0, "initial_weight": initial_weight, "execution": "next trading-day open; stop checked against same-day OHLC unless explicitly closing-basis"})
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def load_run(run_root: Path, price_path: Path) -> tuple[list[dict], pd.DataFrame]:
    audits = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(run_root.glob("2024-*/audit.json"))]
    bars = pd.read_csv(price_path)
    bars["date"] = bars["time_key"].str[:10]
    return audits, bars[["date", "open", "high", "low", "close"]]
