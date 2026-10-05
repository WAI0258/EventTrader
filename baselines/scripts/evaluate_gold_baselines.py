from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any


UTC = timezone.utc
INITIAL_EQUITY = 100_000.0
DEFAULT_COST_BPS_PER_SIDE = 0.5
WINDOW_START = date(2026, 1, 1)
WINDOW_END = date(2026, 6, 30)
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS_ROOT = WORKSPACE_ROOT / "experiments"
COMMON_DAILY_BARS = (
    WORKSPACE_ROOT
    / "gold_futures"
    / "experiment_gcusd_inputs"
    / "project_inputs"
    / "tradingagents"
    / "gcusd_daily_ohlcv_20250630_20260630.csv"
)
COMMON_5MIN_BARS = (
    WORKSPACE_ROOT
    / "gold_futures"
    / "experiment_gcusd_inputs"
    / "project_inputs"
    / "event_trader"
    / "gcusd_5min_20250630_20260630.csv"
)
FINMEM_ROOT = (
    EXPERIMENTS_ROOT / "finmem_gcusd_paper_compat_20260101_20260630_m2_1_qinzhi_v1"
)
AIHF_ROOT = EXPERIMENTS_ROOT / "ai_hedge_fund_gcusd_full_20260101_20260630"
TRADINGAGENTS_ROOT = (
    EXPERIMENTS_ROOT / "tradingagents_gcusd_full_20260101_20260630_processed_news"
)

RULE_BASELINES = (
    "buy_and_hold",
    "tsmom_20d",
    "ma_10ema_50sma",
    "rsi_14",
    "macd_12_26_9",
    "bollinger_20_2",
)
ACTION_SCORE_ANNUALIZATION = 252.0
ACTION_TURNOVER_EPSILON = 1e-8
ACTION_REVERSAL_TURNOVER_THRESHOLD = 1.5

@dataclass
class DailyBar:
    trade_date: date
    open: float
    close: float


@dataclass
class ExecutionBar:
    execution_day: date
    execution_at: datetime
    open_price: float


@dataclass
class RuntimeRecord:
    baseline: str
    trade_date: date
    llm_calls: int
    tokens_in: int
    tokens_out: int
    elapsed_seconds: float
    source_path: str | None


@dataclass
class FinMemDecision:
    trade_date: date
    native_action: str
    action_direction: int
    position_units_before: int
    position_units_after: int
    runtime: RuntimeRecord


@dataclass
class AIHFDecision:
    trade_date: date
    execution_day: date | None
    native_action: str
    target_position: str
    execution_price: float
    execution_status: str
    was_executed: bool
    runtime: RuntimeRecord


@dataclass
class TradingAgentsDecision:
    trade_date: date
    pm_rating: str
    trader_action: str
    target_exposure: float
    runtime: RuntimeRecord
    source_path: str


@dataclass(frozen=True)
class RuleDecision:
    baseline: str
    trade_date: date
    target_exposure: float
    detail: str


@dataclass
class OrderLogRow:
    baseline: str
    source_trade_date: str
    execution_day: str
    action_label: str
    quantity: float
    execution_price: float
    turnover_ratio: float
    transaction_cost: float
    realized_pnl_after_cost: float | None
    long_units_after: float
    short_units_after: float
    cash_after: float


@dataclass
class DayPerformance:
    baseline: str
    trade_date: date
    native_action: str
    decision_detail: str
    long_units: float
    short_units: float
    net_units: float
    gross_exposure_ratio: float
    net_exposure_ratio: float
    turnover_ratio: float
    transaction_cost: float
    realized_pnl_after_cost: float
    open_price: float
    close_price: float
    equity_close: float
    net_return: float


@dataclass
class PortfolioState:
    cash: float = INITIAL_EQUITY
    long_units: float = 0.0
    short_units: float = 0.0
    long_cost_basis: float = 0.0
    short_cost_basis: float = 0.0
    margin_used: float = 0.0
    margin_requirement: float = 0.0
    short_margin_used: float = 0.0

    def equity_at(self, mark_price: float) -> float:
        return self.cash + self.long_units * mark_price - self.short_units * mark_price


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a GCUSD baseline evaluation from completed experiment artifacts."
    )
    parser.add_argument(
        "--output-dir",
        default=str(EXPERIMENTS_ROOT / "gold_baseline_eval_20260101_20260630"),
        help="Directory where evaluation artifacts are written.",
    )
    parser.add_argument(
        "--cost-bps-per-side",
        type=float,
        default=DEFAULT_COST_BPS_PER_SIDE,
        help="Transaction cost in basis points charged per side on executed notional.",
    )
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_daily_bars(path: Path) -> list[DailyBar]:
    bars: list[DailyBar] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            day = datetime.strptime(row["date"], "%Y-%m-%d").date()
            if WINDOW_START <= day <= WINDOW_END:
                bars.append(
                    DailyBar(
                        trade_date=day,
                        open=float(row["open"]),
                        close=float(row["close"]),
                    )
                )
    return bars


def _iter_5min_bars(path: Path) -> list[ExecutionBar]:
    rows: list[ExecutionBar] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            bar_dt = datetime.strptime(row["datetime_utc"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=UTC
            )
            rows.append(
                ExecutionBar(
                    execution_day=bar_dt.date(),
                    execution_at=bar_dt,
                    open_price=float(row["open"]),
                )
            )
    return rows


def _calendar_days() -> list[date]:
    current = WINDOW_START
    out: list[date] = []
    while current <= WINDOW_END:
        out.append(current)
        current += timedelta(days=1)
    return out


def _build_execution_lookup(bars_5m: list[ExecutionBar], trade_days: list[date]) -> dict[date, ExecutionBar]:
    lookup: dict[date, ExecutionBar] = {}
    idx = 0
    for trade_day in trade_days:
        cutoff = datetime.combine(trade_day, time(23, 59, 59), tzinfo=UTC)
        while idx < len(bars_5m) and bars_5m[idx].execution_at <= cutoff:
            idx += 1
        if idx < len(bars_5m):
            lookup[trade_day] = bars_5m[idx]
    return lookup


def _safe_mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _annualization_factor(common_days: list[date]) -> float:
    elapsed_days = (common_days[-1] - common_days[0]).days + 1
    return len(common_days) / elapsed_days * 365.0


def _baseline_runtime_row(
    baseline: str,
    records: list[RuntimeRecord],
    raw_decision_count: int,
    aligned_real_decision_count: int,
    synthetic_hold_count: int,
    transient_failure_count: int = 0,
    evaluation_mode: str = "native",
) -> dict[str, Any]:
    elapsed_values = [record.elapsed_seconds for record in records if record.elapsed_seconds > 0.0]
    llm_calls = sum(record.llm_calls for record in records)
    tokens_in = sum(record.tokens_in for record in records)
    tokens_out = sum(record.tokens_out for record in records)
    return {
        "baseline": baseline,
        "raw_decision_count": raw_decision_count,
        "aligned_real_decision_count": aligned_real_decision_count,
        "decision_count": aligned_real_decision_count,
        "evaluation_mode": evaluation_mode,
        "excluded_non_common_calendar_count": raw_decision_count - aligned_real_decision_count,
        "synthetic_hold_count": synthetic_hold_count,
        "failed_decision_count": 0,
        "transient_failure_artifact_count": transient_failure_count,
        "llm_calls": llm_calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "wall_clock_seconds_sum": sum(elapsed_values),
        "average_decision_latency_seconds": _safe_mean(elapsed_values),
        "p50_decision_latency_seconds": _percentile(elapsed_values, 0.50),
        "p90_decision_latency_seconds": _percentile(elapsed_values, 0.90),
        "max_decision_latency_seconds": max(elapsed_values) if elapsed_values else None,
        "provider_usage_observed": any(
            record.tokens_in > 0 or record.tokens_out > 0 for record in records
        ),
    }


def _apply_cost(notional: float, cost_rate: float) -> float:
    return abs(notional) * cost_rate


def _open_long(
    state: PortfolioState,
    quantity: float,
    price: float,
    cost_rate: float,
    *,
    cash_limited: bool = False,
) -> tuple[float, float]:
    if quantity <= 0.0 or price <= 0.0:
        return 0.0, 0.0
    if cash_limited:
        quantity = min(quantity, math.floor(max(state.cash, 0.0) / price))
    if quantity <= 0.0:
        return 0.0, 0.0
    old_units = state.long_units
    new_units = old_units + quantity
    if new_units > 0.0:
        state.long_cost_basis = (
            (state.long_cost_basis * old_units) + (price * quantity)
        ) / new_units
    state.long_units = new_units
    notional = quantity * price
    cost = _apply_cost(notional, cost_rate)
    state.cash -= notional + cost
    return quantity, cost


def _close_long(
    state: PortfolioState,
    quantity: float,
    price: float,
    cost_rate: float,
) -> tuple[float, float]:
    if quantity <= 0.0:
        return 0.0, 0.0
    quantity = min(quantity, state.long_units)
    if quantity <= 0.0:
        return 0.0, 0.0
    notional = quantity * price
    cost = _apply_cost(notional, cost_rate)
    realized = (price - state.long_cost_basis) * quantity - cost
    state.cash += notional - cost
    state.long_units -= quantity
    if state.long_units <= 1e-12:
        state.long_units = 0.0
        state.long_cost_basis = 0.0
    return cost, realized


def _open_short(
    state: PortfolioState,
    quantity: float,
    price: float,
    cost_rate: float,
) -> tuple[float, float]:
    if quantity <= 0.0 or price <= 0.0:
        return 0.0, 0.0
    if state.margin_requirement > 0.0:
        available_cash = max(0.0, state.cash - state.margin_used)
        max_quantity = math.floor(available_cash / (price * state.margin_requirement))
        quantity = min(quantity, max_quantity)
    if quantity <= 0.0:
        return 0.0, 0.0
    old_units = state.short_units
    new_units = old_units + quantity
    if new_units > 0.0:
        state.short_cost_basis = (
            (state.short_cost_basis * old_units) + (price * quantity)
        ) / new_units
    state.short_units = new_units
    notional = quantity * price
    cost = _apply_cost(notional, cost_rate)
    margin_required = notional * state.margin_requirement
    state.short_margin_used += margin_required
    state.margin_used += margin_required
    state.cash += notional - margin_required - cost
    return quantity, cost


def _close_short(
    state: PortfolioState,
    quantity: float,
    price: float,
    cost_rate: float,
) -> tuple[float, float]:
    if quantity <= 0.0:
        return 0.0, 0.0
    quantity = min(quantity, state.short_units)
    if quantity <= 0.0:
        return 0.0, 0.0
    notional = quantity * price
    cost = _apply_cost(notional, cost_rate)
    realized = (state.short_cost_basis - price) * quantity - cost
    portion = quantity / state.short_units if state.short_units > 0.0 else 1.0
    margin_to_release = portion * state.short_margin_used
    state.short_margin_used -= margin_to_release
    state.margin_used -= margin_to_release
    state.cash += margin_to_release
    state.cash -= notional + cost
    state.short_units -= quantity
    if state.short_units <= 1e-12:
        state.short_units = 0.0
        state.short_cost_basis = 0.0
        state.short_margin_used = 0.0
    return cost, realized


def _apply_inventory_delta(
    state: PortfolioState,
    delta_units: float,
    price: float,
    cost_rate: float,
) -> tuple[str, float, float, float]:
    if math.isclose(delta_units, 0.0, abs_tol=1e-12):
        return "hold", 0.0, 0.0, 0.0
    total_cost = 0.0
    realized = 0.0
    if delta_units > 0.0:
        remaining = delta_units
        if state.short_units > 0.0:
            close_qty = min(remaining, state.short_units)
            cost, pnl = _close_short(state, close_qty, price, cost_rate)
            total_cost += cost
            realized += pnl
            remaining -= close_qty
        if remaining > 0.0:
            _, cost = _open_long(state, remaining, price, cost_rate)
            total_cost += cost
        return "buy", delta_units, total_cost, realized
    remaining = abs(delta_units)
    if state.long_units > 0.0:
        close_qty = min(remaining, state.long_units)
        cost, pnl = _close_long(state, close_qty, price, cost_rate)
        total_cost += cost
        realized += pnl
        remaining -= close_qty
    if remaining > 0.0:
        _, cost = _open_short(state, remaining, price, cost_rate)
        total_cost += cost
    return "sell", abs(delta_units), total_cost, realized


def _pm_rating_target_exposure(prev_exposure: float, rating: str) -> float:
    normalized = (rating or "").strip().lower()
    if normalized == "buy":
        return 1.0
    if normalized == "overweight":
        return 1.0
    if normalized == "hold":
        return prev_exposure
    if normalized == "underweight":
        return -1.0
    if normalized == "sell":
        return -1.0
    raise ValueError(f"Unsupported TradingAgents rating: {rating}")


def _apply_target_long_exposure(
    state: PortfolioState,
    target_exposure: float,
    price: float,
    cost_rate: float,
) -> tuple[float, float, float]:
    equity_before = state.equity_at(price)
    target_units = (target_exposure * equity_before) / price if price > 0.0 else 0.0
    current_units = state.long_units
    delta_units = target_units - current_units
    if math.isclose(delta_units, 0.0, abs_tol=1e-10):
        return 0.0, 0.0, target_units
    if delta_units > 0.0:
        _, cost = _open_long(state, delta_units, price, cost_rate)
        return cost, 0.0, target_units
    cost, realized = _close_long(state, abs(delta_units), price, cost_rate)
    return cost, realized, target_units


def _apply_target_signed_exposure(
    state: PortfolioState,
    target_exposure: float,
    price: float,
    cost_rate: float,
) -> tuple[float, float, float]:
    equity_before = state.equity_at(price)
    target_units = (target_exposure * equity_before) / price if price > 0.0 else 0.0
    current_units = state.long_units - state.short_units
    _, _, transaction_cost, realized = _apply_inventory_delta(
        state,
        target_units - current_units,
        price,
        cost_rate,
    )
    return transaction_cost, realized, target_units


def _iter_finmem_decisions() -> list[FinMemDecision]:
    decisions: list[FinMemDecision] = []
    for audit_path in sorted((FINMEM_ROOT / "days").glob("*/audit.json")):
        audit = _load_json(audit_path)
        trade_day = datetime.strptime(audit["trade_date"], "%Y-%m-%d").date()
        decision = audit.get("decision") or {}
        portfolio = audit.get("portfolio") or {}
        llm_usage = (audit.get("llm_usage") or {}).get("day_audit", {})
        timing = (audit.get("timing") or {}).get("day_step", {})
        runtime = RuntimeRecord(
            baseline="finmem",
            trade_date=trade_day,
            llm_calls=int(llm_usage.get("llm_calls", 0) or 0),
            tokens_in=int(llm_usage.get("tokens_in", 0) or 0),
            tokens_out=int(llm_usage.get("tokens_out", 0) or 0),
            elapsed_seconds=float(timing.get("elapsed_seconds", 0.0) or 0.0),
            source_path=str(audit_path),
        )
        decisions.append(
            FinMemDecision(
                trade_date=trade_day,
                native_action=str(decision.get("native_action", decision.get("raw", "hold"))),
                action_direction=int(portfolio.get("action_direction", 0) or 0),
                position_units_before=int(portfolio.get("position_units_before_decision", 0) or 0),
                position_units_after=int(portfolio.get("position_units_after_decision", 0) or 0),
                runtime=runtime,
            )
        )
    return decisions


def _iter_aihf_decisions() -> list[AIHFDecision]:
    decisions: list[AIHFDecision] = []
    for audit_path in sorted(AIHF_ROOT.glob("*/audit.json")):
        audit = _load_json(audit_path)
        trade_day = datetime.strptime(audit["trade_date"], "%Y-%m-%d").date()
        execution = audit.get("execution")
        status = str(audit.get("status", "unknown"))
        execution_day: date | None = None
        execution_price = 0.0
        if isinstance(execution, dict):
            bar_time_utc = execution.get("bar_time_utc")
            if bar_time_utc:
                execution_day = datetime.fromisoformat(
                    str(bar_time_utc).replace("Z", "+00:00")
                ).date()
            execution_price = float(execution.get("price_open", 0.0) or 0.0)
        usage = audit.get("llm_usage") or {}
        timing = audit.get("timing") or {}
        runtime = RuntimeRecord(
            baseline="ai_hedge_fund",
            trade_date=trade_day,
            llm_calls=int(usage.get("llm_calls", 0) or 0),
            tokens_in=int(usage.get("tokens_in", 0) or 0),
            tokens_out=int(usage.get("tokens_out", 0) or 0),
            elapsed_seconds=float(timing.get("elapsed_seconds", 0.0) or 0.0),
            source_path=str(audit_path),
        )
        position_after_execution = audit.get("position_after_execution") or {}
        target_position = str(
            position_after_execution.get("position_sign", audit.get("normalized_position", ""))
        )
        if target_position not in {"long", "flat", "short"}:
            raise ValueError(f"Unsupported native AHF position in {audit_path}: {target_position!r}")
        raw_output = audit.get("raw_output") or {}
        native_action = str(audit.get("native_action", raw_output.get("action", "hold"))).lower()
        decisions.append(
            AIHFDecision(
                trade_date=trade_day,
                execution_day=execution_day,
                native_action=native_action,
                target_position=target_position,
                execution_price=execution_price,
                execution_status=status,
                was_executed=status == "filled" and execution_day is not None and execution_price > 0.0,
                runtime=runtime,
            )
        )
    return decisions


def _extract_trader_action_from_text(text: str) -> str:
    trader_match = re.search(
        r"## III\. Trading Team Plan(.*?)(?:\n## IV\. |\Z)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    trader_block = trader_match.group(1) if trader_match else text
    action_match = re.search(
        r"\*\*Action\*\*:\s*(Buy|Hold|Sell)\b",
        trader_block,
        flags=re.IGNORECASE,
    )
    if action_match:
        return action_match.group(1).title()
    proposal_match = re.search(
        r"FINAL TRANSACTION PROPOSAL:\s*\**(BUY|HOLD|SELL)\**",
        trader_block,
        flags=re.IGNORECASE,
    )
    if proposal_match:
        return proposal_match.group(1).title()
    raise ValueError("Unable to parse TradingAgents trader action")


def _read_tradingagents_trader_action(audit: dict[str, Any], audit_path: Path) -> tuple[str, str]:
    artifact_path = Path(str((audit.get("artifacts") or {}).get("reports_dir", "")))
    candidate_paths = []
    if artifact_path:
        if artifact_path.name.lower() == "complete_report.md":
            candidate_paths.append(artifact_path.parent / "3_trading" / "trader.md")
            candidate_paths.append(artifact_path)
        else:
            candidate_paths.append(artifact_path / "3_trading" / "trader.md")
            candidate_paths.append(artifact_path / "complete_report.md")
    candidate_paths.append(audit_path.parent / "reports" / "3_trading" / "trader.md")
    candidate_paths.append(audit_path.parent / "reports" / "complete_report.md")

    for candidate in candidate_paths:
        if candidate.exists():
            try:
                return _extract_trader_action_from_text(candidate.read_text(encoding="utf-8")), str(candidate)
            except ValueError:
                continue
    return "Unavailable", "missing_or_unparseable"


def _iter_tradingagents_decisions(
    execution_lookup: dict[date, ExecutionBar],
) -> tuple[list[TradingAgentsDecision], list[dict[str, Any]]]:
    decisions: list[TradingAgentsDecision] = []
    diagnostics: list[dict[str, Any]] = []
    prev_target_exposure = 0.0
    for audit_path in sorted(TRADINGAGENTS_ROOT.glob("*/audit.json")):
        audit = _load_json(audit_path)
        trade_day = datetime.strptime(audit["trade_date"], "%Y-%m-%d").date()
        llm_usage = audit.get("llm_usage") or {}
        timing = audit.get("timing") or {}
        pm_rating = str(audit.get("decision", "Hold"))
        trader_action, trader_action_source = _read_tradingagents_trader_action(audit, audit_path)
        target_exposure = _pm_rating_target_exposure(prev_target_exposure, pm_rating)
        prev_target_exposure = target_exposure
        runtime = RuntimeRecord(
            baseline="tradingagents",
            trade_date=trade_day,
            llm_calls=int(llm_usage.get("llm_calls", 0) or 0),
            tokens_in=int(llm_usage.get("tokens_in", 0) or 0),
            tokens_out=int(llm_usage.get("tokens_out", 0) or 0),
            elapsed_seconds=float(timing.get("elapsed_seconds", 0.0) or 0.0),
            source_path=str(audit_path),
        )
        decisions.append(
            TradingAgentsDecision(
                trade_date=trade_day,
                pm_rating=pm_rating,
                trader_action=trader_action,
                target_exposure=target_exposure,
                runtime=runtime,
                source_path=str(audit_path),
            )
        )
        diagnostics.append(
            {
                "baseline": "tradingagents",
                "trade_date": trade_day.isoformat(),
                "pm_rating": pm_rating,
                "trader_action": trader_action,
                "trader_action_source": trader_action_source,
                "target_exposure_after_decision": target_exposure,
                "source_path": str(audit_path),
                "execution_day": execution_lookup[trade_day].execution_day.isoformat()
                if trade_day in execution_lookup
                else None,
            }
        )
    return decisions, diagnostics


def _evaluate_finmem_action_score(
    common_days: list[date],
    bars_by_day: dict[date, DailyBar],
    decisions: list[FinMemDecision],
    cost_rate: float,
) -> tuple[list[DayPerformance], list[OrderLogRow], dict[str, Any]]:
    """Evaluate FinMem using the repository's signed daily action score.

    FinMem's reference metric treats buy/hold/sell as +1/0/-1 exposure for the
    next close-to-close interval. The original repository uses signed log
    reward without transaction costs; this evaluator adds the shared cost
    overlay whenever that daily action changes.
    """

    decisions_by_day = {decision.trade_date: decision for decision in decisions}
    missing_days = [day for day in common_days if day not in decisions_by_day]
    if missing_days:
        raise ValueError(f"FinMem action-score inputs are missing dates: {missing_days[:5]}")

    performance: list[DayPerformance] = []
    order_rows: list[OrderLogRow] = []
    equity = INITIAL_EQUITY
    previous_action = 0

    for index in range(len(common_days) - 1):
        trade_day = common_days[index]
        next_day = common_days[index + 1]
        decision = decisions_by_day[trade_day]
        action = int(decision.action_direction)
        current_price = bars_by_day[trade_day].close
        next_price = bars_by_day[next_day].close
        gross_return = (next_price / current_price) ** action - 1.0 if action else 0.0
        transition_units = abs(action - previous_action)
        fee_rate = transition_units * cost_rate
        equity_before = equity
        equity_after_gross = equity_before * (1.0 + gross_return)
        transaction_cost = equity_after_gross * fee_rate
        equity_close = equity_after_gross - transaction_cost
        net_return = equity_close / equity_before - 1.0 if equity_before > 0.0 else 0.0

        if transition_units:
            order_rows.append(
                OrderLogRow(
                    baseline="finmem",
                    source_trade_date=trade_day.isoformat(),
                    execution_day=trade_day.isoformat(),
                    action_label=f"action_score:{action:+d}",
                    quantity=float(transition_units),
                    execution_price=current_price,
                    turnover_ratio=float(transition_units),
                    transaction_cost=transaction_cost,
                    realized_pnl_after_cost=None,
                    long_units_after=float(action > 0),
                    short_units_after=float(action < 0),
                    cash_after=equity_close,
                )
            )

        performance.append(
            DayPerformance(
                baseline="finmem",
                trade_date=trade_day,
                native_action=decision.native_action,
                decision_detail=(
                    f"action_score={action:+d}; next_day={next_day.isoformat()}; "
                    f"gross_return={gross_return:.8f}"
                ),
                long_units=float(action > 0),
                short_units=float(action < 0),
                net_units=float(action),
                gross_exposure_ratio=float(abs(action)),
                net_exposure_ratio=float(action),
                turnover_ratio=float(transition_units),
                transaction_cost=transaction_cost,
                realized_pnl_after_cost=0.0,
                open_price=current_price,
                close_price=next_price,
                equity_close=equity_close,
                net_return=net_return,
            )
        )
        equity = equity_close
        previous_action = action

    returns = [item.net_return for item in performance]
    sample_std = statistics.stdev(returns) if len(returns) > 1 else 0.0
    downside = [value for value in returns if value < 0.0]
    downside_std = statistics.stdev(downside) if len(downside) > 1 else 0.0
    average_return = statistics.mean(returns) if returns else 0.0
    peak = INITIAL_EQUITY
    max_drawdown = 0.0
    max_drawdown_date: str | None = None
    for item in performance:
        peak = max(peak, item.equity_close)
        drawdown = item.equity_close / peak - 1.0
        if drawdown < max_drawdown:
            max_drawdown = drawdown
            max_drawdown_date = item.trade_date.isoformat()

    long_days = sum(item.net_units > 0.0 for item in performance)
    short_days = sum(item.net_units < 0.0 for item in performance)
    flat_days = sum(item.net_units == 0.0 for item in performance)
    summary = {
        "baseline": "finmem",
        "evaluation_semantics": "repository_action_score",
        "execution_semantics": "same_close_signed_log_reward_with_cost_overlay",
        "window_start": common_days[0].isoformat(),
        "window_end": common_days[-1].isoformat(),
        "decision_days": len(decisions),
        "evaluation_observations": len(performance),
        "executed_order_count": _reportable_action_count("finmem", order_rows),
        "close_event_count": 0,
        "total_return": equity / INITIAL_EQUITY - 1.0,
        "annualized_return": (
            (equity / INITIAL_EQUITY) ** (ACTION_SCORE_ANNUALIZATION / len(performance)) - 1.0
            if performance
            else 0.0
        ),
        "sharpe": average_return / sample_std * math.sqrt(ACTION_SCORE_ANNUALIZATION) if sample_std else 0.0,
        "sortino": average_return / downside_std * math.sqrt(ACTION_SCORE_ANNUALIZATION) if downside_std else None,
        "max_drawdown": max_drawdown,
        "max_drawdown_date": max_drawdown_date,
        "hit_rate": None,
        "average_gross_exposure": statistics.mean(item.gross_exposure_ratio for item in performance),
        "average_net_exposure": statistics.mean(item.net_exposure_ratio for item in performance),
        "turnover": sum(item.turnover_ratio for item in performance),
        "transaction_cost_sum": sum(item.transaction_cost for item in performance),
        "long_day_ratio": long_days / len(performance) if performance else None,
        "flat_day_ratio": flat_days / len(performance) if performance else None,
        "short_day_ratio": short_days / len(performance) if performance else None,
        "final_equity": equity,
        "max_long_units": 1.0,
        "max_short_units": 1.0,
        "annualization_factor": ACTION_SCORE_ANNUALIZATION,
        "cost_bps_per_side": cost_rate * 10_000.0,
    }
    return performance, order_rows, summary


def _evaluate_aihf(
    common_days: list[date],
    bars_by_day: dict[date, DailyBar],
    decisions: list[AIHFDecision],
    cost_rate: float,
) -> tuple[list[DayPerformance], list[OrderLogRow]]:
    events_by_day = {
        decision.execution_day: decision
        for decision in decisions
        if decision.execution_day is not None
    }
    unexecuted_by_trade_day = {
        decision.trade_date: decision
        for decision in decisions
        if not decision.was_executed
    }
    state = PortfolioState()
    performance: list[DayPerformance] = []
    order_rows: list[OrderLogRow] = []
    prev_equity = INITIAL_EQUITY

    for day in common_days:
        bar = bars_by_day[day]
        turnover_ratio = 0.0
        transaction_cost = 0.0
        realized = 0.0
        native_action = "hold"
        detail = "carry"
        pending = unexecuted_by_trade_day.get(day)
        if pending is not None:
            detail = (
                f"no_fill(status={pending.execution_status}; target={pending.target_position})"
            )
        event = events_by_day.get(day)
        if event is not None:
            equity_before = state.equity_at(event.execution_price)
            native_action = event.native_action
            target_exposure = {"long": 1.0, "flat": 0.0, "short": -1.0}[event.target_position]
            detail = (
                f"native_action={event.native_action}; native_state={event.target_position}; "
                f"exposure={target_exposure:.2f}; "
                f"exec={event.execution_price:.4f}; status={event.execution_status}"
            )
            if event.was_executed:
                current_units = state.long_units - state.short_units
                transaction_cost, realized, target_units = _apply_target_signed_exposure(
                    state,
                    target_exposure,
                    event.execution_price,
                    cost_rate,
                )
                traded_units = abs(target_units - current_units)
                turnover_ratio = (
                    (traded_units * event.execution_price) / equity_before
                    if equity_before > 0.0
                    else 0.0
                )
                if traded_units > 0.0 or not math.isclose(realized, 0.0, abs_tol=1e-12):
                    order_rows.append(
                        OrderLogRow(
                            baseline="ai_hedge_fund",
                            source_trade_date=event.trade_date.isoformat(),
                            execution_day=day.isoformat(),
                            action_label=(
                                f"native_action={event.native_action}; "
                                f"target={event.target_position}"
                            ),
                            quantity=traded_units,
                            execution_price=event.execution_price,
                            turnover_ratio=turnover_ratio,
                            transaction_cost=transaction_cost,
                            realized_pnl_after_cost=realized if not math.isclose(realized, 0.0, abs_tol=1e-12) else None,
                            long_units_after=state.long_units,
                            short_units_after=state.short_units,
                            cash_after=state.cash,
                        )
                    )
                detail = (
                    f"{detail}; traded_units={traded_units:.4f}; "
                    f"long={state.long_units:.4f}; short={state.short_units:.4f}"
                )
        equity_close = state.equity_at(bar.close)
        net_return = (equity_close / prev_equity) - 1.0 if prev_equity > 0.0 else 0.0
        gross_notional = (state.long_units + state.short_units) * bar.close
        net_notional = (state.long_units - state.short_units) * bar.close
        performance.append(
            DayPerformance(
                baseline="ai_hedge_fund",
                trade_date=day,
                native_action=native_action,
                decision_detail=detail,
                long_units=state.long_units,
                short_units=state.short_units,
                net_units=state.long_units - state.short_units,
                gross_exposure_ratio=gross_notional / equity_close if equity_close != 0.0 else 0.0,
                net_exposure_ratio=net_notional / equity_close if equity_close != 0.0 else 0.0,
                turnover_ratio=turnover_ratio,
                transaction_cost=transaction_cost,
                realized_pnl_after_cost=realized,
                open_price=bar.open,
                close_price=bar.close,
                equity_close=equity_close,
                net_return=net_return,
            )
        )
        prev_equity = equity_close
    return performance, order_rows


def _evaluate_tradingagents(
    common_days: list[date],
    bars_by_day: dict[date, DailyBar],
    execution_lookup: dict[date, ExecutionBar],
    decisions: list[TradingAgentsDecision],
    cost_rate: float,
) -> tuple[list[DayPerformance], list[OrderLogRow]]:
    events_by_day: dict[date, TradingAgentsDecision] = {}
    for decision in decisions:
        next_bar = execution_lookup.get(decision.trade_date)
        if next_bar is not None:
            events_by_day[next_bar.execution_day] = decision

    state = PortfolioState()
    performance: list[DayPerformance] = []
    order_rows: list[OrderLogRow] = []
    prev_equity = INITIAL_EQUITY

    for day in common_days:
        bar = bars_by_day[day]
        turnover_ratio = 0.0
        transaction_cost = 0.0
        realized = 0.0
        native_action = "hold"
        detail = "carry"
        event = events_by_day.get(day)
        if event is not None:
            equity_before = state.equity_at(bar.open)
            current_units = state.long_units - state.short_units
            transaction_cost, realized, target_units = _apply_target_signed_exposure(
                state,
                event.target_exposure,
                bar.open,
                cost_rate,
            )
            traded_units = abs(target_units - current_units)
            turnover_ratio = (traded_units * bar.open) / equity_before if equity_before > 0.0 else 0.0
            native_action = event.pm_rating
            detail = (
                f"trader={event.trader_action}; "
                f"target_exposure={event.target_exposure:.2f}"
            )
            if traded_units > 1e-10 or not math.isclose(realized, 0.0, abs_tol=1e-12):
                order_rows.append(
                    OrderLogRow(
                        baseline="tradingagents",
                        source_trade_date=event.trade_date.isoformat(),
                        execution_day=day.isoformat(),
                        action_label=f"pm={event.pm_rating}; trader={event.trader_action}",
                        quantity=traded_units,
                        execution_price=bar.open,
                        turnover_ratio=turnover_ratio,
                        transaction_cost=transaction_cost,
                        realized_pnl_after_cost=realized if not math.isclose(realized, 0.0, abs_tol=1e-12) else None,
                        long_units_after=state.long_units,
                        short_units_after=state.short_units,
                        cash_after=state.cash,
                    )
                )
        equity_close = state.equity_at(bar.close)
        net_return = (equity_close / prev_equity) - 1.0 if prev_equity > 0.0 else 0.0
        gross_notional = (state.long_units + state.short_units) * bar.close
        net_notional = (state.long_units - state.short_units) * bar.close
        performance.append(
            DayPerformance(
                baseline="tradingagents",
                trade_date=day,
                native_action=native_action,
                decision_detail=detail,
                long_units=state.long_units,
                short_units=state.short_units,
                net_units=state.long_units - state.short_units,
                gross_exposure_ratio=gross_notional / equity_close if equity_close != 0.0 else 0.0,
                net_exposure_ratio=net_notional / equity_close if equity_close != 0.0 else 0.0,
                turnover_ratio=turnover_ratio,
                transaction_cost=transaction_cost,
                realized_pnl_after_cost=realized,
                open_price=bar.open,
                close_price=bar.close,
                equity_close=equity_close,
                net_return=net_return,
            )
        )
        prev_equity = equity_close
    return performance, order_rows


def _iter_rule_history(path: Path) -> list[DailyBar]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [
            DailyBar(
                trade_date=datetime.strptime(row["date"], "%Y-%m-%d").date(),
                open=float(row["open"]),
                close=float(row["close"]),
            )
            for row in csv.DictReader(handle)
        ]


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    result: list[float] = []
    current = values[0]
    for value in values:
        current = alpha * value + (1.0 - alpha) * current
        result.append(current)
    return result


def _rsi(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = [max(values[index] - values[index - 1], 0.0) for index in range(1, period + 1)]
    losses = [max(values[index - 1] - values[index], 0.0) for index in range(1, period + 1)]
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    result[period] = 100.0 if average_loss == 0.0 else 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
        result[index] = 100.0 if average_loss == 0.0 else 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    return result


def _rule_action(exposure: float) -> str:
    return "long" if exposure > 0.0 else "short" if exposure < 0.0 else "flat"


def _build_rule_decisions(history: list[DailyBar], common_days: list[date]) -> dict[str, list[RuleDecision]]:
    closes = [bar.close for bar in history]
    index_by_day = {bar.trade_date: index for index, bar in enumerate(history)}
    ema10, ema12, ema26 = _ema(closes, 10), _ema(closes, 12), _ema(closes, 26)
    macd = [fast - slow for fast, slow in zip(ema12, ema26)]
    signal = _ema(macd, 9)
    rsi = _rsi(closes, 14)
    decisions = {baseline: [] for baseline in RULE_BASELINES}
    bollinger_target = 0.0
    rsi_target = 0.0
    for trade_day in common_days:
        index = index_by_day[trade_day]
        close = closes[index]
        if trade_day == common_days[0]:
            decisions["buy_and_hold"].append(RuleDecision("buy_and_hold", trade_day, 1.0, "initial full long exposure"))
        if index >= 20:
            value = close / closes[index - 20] - 1.0
            target = 1.0 if value >= 0.0 else -1.0
            decisions["tsmom_20d"].append(RuleDecision("tsmom_20d", trade_day, target, f"20d_return={value:.6f}"))
        if index >= 49:
            sma50 = sum(closes[index - 49 : index + 1]) / 50.0
            target = 1.0 if ema10[index] >= sma50 else -1.0
            decisions["ma_10ema_50sma"].append(RuleDecision("ma_10ema_50sma", trade_day, target, f"ema10={ema10[index]:.4f}; sma50={sma50:.4f}"))
        rsi_value = rsi[index]
        if rsi_value is not None:
            if rsi_value < 30.0:
                rsi_target = 1.0
            elif rsi_value > 70.0:
                rsi_target = -1.0
            decisions["rsi_14"].append(RuleDecision("rsi_14", trade_day, rsi_target, f"rsi14={rsi_value:.4f}"))
        if index >= 34:
            target = 1.0 if macd[index] >= signal[index] else -1.0
            decisions["macd_12_26_9"].append(RuleDecision("macd_12_26_9", trade_day, target, f"macd={macd[index]:.4f}; signal={signal[index]:.4f}"))
        if index >= 19:
            window = closes[index - 19 : index + 1]
            middle = sum(window) / len(window)
            deviation = statistics.pstdev(window)
            lower, upper = middle - 2.0 * deviation, middle + 2.0 * deviation
            if close <= lower:
                bollinger_target = 1.0
            elif close >= upper:
                bollinger_target = -1.0
            elif bollinger_target > 0.0 and close >= middle:
                bollinger_target = 0.0
            elif bollinger_target < 0.0 and close <= middle:
                bollinger_target = 0.0
            decisions["bollinger_20_2"].append(RuleDecision("bollinger_20_2", trade_day, bollinger_target, f"close={close:.4f}; middle={middle:.4f}; lower={lower:.4f}; upper={upper:.4f}"))
    return decisions


def _evaluate_rule_baseline(
    baseline: str,
    common_days: list[date],
    bars_by_day: dict[date, DailyBar],
    execution_lookup: dict[date, ExecutionBar],
    decisions: list[RuleDecision],
    cost_rate: float,
) -> tuple[list[DayPerformance], list[OrderLogRow]]:
    events = {
        execution_lookup[decision.trade_date].execution_day: decision
        for decision in decisions
        if decision.trade_date in execution_lookup
    }
    state = PortfolioState()
    performance: list[DayPerformance] = []
    orders: list[OrderLogRow] = []
    previous_equity = INITIAL_EQUITY
    for day in common_days:
        bar = bars_by_day[day]
        turnover = cost = realized = 0.0
        action, detail = "hold", "carry"
        event = events.get(day)
        if event is not None:
            equity_before = state.equity_at(bar.open)
            current_units = state.long_units - state.short_units
            cost, realized, target_units = _apply_target_signed_exposure(state, event.target_exposure, bar.open, cost_rate)
            traded_units = abs(target_units - current_units)
            turnover = traded_units * bar.open / equity_before if equity_before > 0.0 else 0.0
            action, detail = _rule_action(event.target_exposure), event.detail
            if traded_units > 1e-10 or not math.isclose(realized, 0.0, abs_tol=1e-12):
                orders.append(OrderLogRow(baseline, event.trade_date.isoformat(), day.isoformat(), action, traded_units, bar.open, turnover, cost, realized if not math.isclose(realized, 0.0, abs_tol=1e-12) else None, state.long_units, state.short_units, state.cash))
        equity_close = state.equity_at(bar.close)
        gross_notional = (state.long_units + state.short_units) * bar.close
        net_notional = (state.long_units - state.short_units) * bar.close
        performance.append(DayPerformance(
            baseline=baseline,
            trade_date=day,
            native_action=action,
            decision_detail=detail,
            long_units=state.long_units,
            short_units=state.short_units,
            net_units=state.long_units - state.short_units,
            gross_exposure_ratio=gross_notional / equity_close if equity_close else 0.0,
            net_exposure_ratio=net_notional / equity_close if equity_close else 0.0,
            turnover_ratio=turnover,
            transaction_cost=cost,
            realized_pnl_after_cost=realized,
            open_price=bar.open,
            close_price=bar.close,
            equity_close=equity_close,
            net_return=equity_close / previous_equity - 1.0 if previous_equity > 0.0 else 0.0,
        ))
        previous_equity = equity_close

    if baseline == "buy_and_hold" and performance and (state.long_units or state.short_units):
        final_day = common_days[-1]
        final_bar = bars_by_day[final_day]
        equity_before_close = state.equity_at(final_bar.close)
        current_units = state.long_units - state.short_units
        close_cost, close_realized, target_units = _apply_target_signed_exposure(
            state, 0.0, final_bar.close, cost_rate
        )
        traded_units = abs(target_units - current_units)
        close_turnover = (
            traded_units * final_bar.close / equity_before_close
            if equity_before_close > 0.0
            else 0.0
        )
        orders.append(
            OrderLogRow(
                baseline=baseline,
                source_trade_date=final_day.isoformat(),
                execution_day=final_day.isoformat(),
                action_label="terminal_close",
                quantity=traded_units,
                execution_price=final_bar.close,
                turnover_ratio=close_turnover,
                transaction_cost=close_cost,
                realized_pnl_after_cost=close_realized
                if not math.isclose(close_realized, 0.0, abs_tol=1e-12)
                else None,
                long_units_after=state.long_units,
                short_units_after=state.short_units,
                cash_after=state.cash,
            )
        )
        final_performance = performance[-1]
        previous_period_equity = (
            performance[-2].equity_close if len(performance) > 1 else INITIAL_EQUITY
        )
        final_performance.native_action = "terminal_close"
        final_performance.decision_detail = "terminal liquidation at evaluation-window close"
        final_performance.long_units = state.long_units
        final_performance.short_units = state.short_units
        final_performance.net_units = state.long_units - state.short_units
        final_performance.gross_exposure_ratio = 0.0
        final_performance.net_exposure_ratio = 0.0
        final_performance.turnover_ratio += close_turnover
        final_performance.transaction_cost += close_cost
        final_performance.realized_pnl_after_cost += close_realized
        final_performance.equity_close = state.equity_at(final_bar.close)
        final_performance.net_return = (
            final_performance.equity_close / previous_period_equity - 1.0
            if previous_period_equity > 0.0
            else 0.0
        )
    return performance, orders


def _reportable_action_count(baseline: str, order_rows: list[OrderLogRow]) -> int:
    return sum(
        1 + int(row.turnover_ratio > ACTION_REVERSAL_TURNOVER_THRESHOLD)
        for row in order_rows
        if (
            "hold" not in row.action_label.lower()
            and "unavailable" not in row.action_label.lower()
            and (
                row.turnover_ratio > ACTION_TURNOVER_EPSILON
                or row.realized_pnl_after_cost is not None
            )
        )
    )
def _summarize_performance(
    baseline: str,
    performance: list[DayPerformance],
    order_rows: list[OrderLogRow],
    annualization_factor: float,
) -> dict[str, Any]:
    net_returns = [item.net_return for item in performance]
    final_equity = performance[-1].equity_close if performance else INITIAL_EQUITY
    total_return = (final_equity / INITIAL_EQUITY) - 1.0
    annualized_return = (
        (final_equity / INITIAL_EQUITY) ** (annualization_factor / len(performance)) - 1.0
        if performance
        else 0.0
    )
    avg_return = _safe_mean(net_returns) or 0.0
    stdev = statistics.pstdev(net_returns) if len(net_returns) > 1 else 0.0
    downside = [value for value in net_returns if value < 0.0]
    downside_stdev = statistics.pstdev(downside) if len(downside) > 1 else 0.0
    sharpe = (avg_return / stdev * math.sqrt(annualization_factor)) if stdev > 0 else None
    sortino = (
        avg_return / downside_stdev * math.sqrt(annualization_factor)
        if downside_stdev > 0
        else None
    )

    peak = INITIAL_EQUITY
    max_drawdown = 0.0
    max_drawdown_date = None
    for item in performance:
        peak = max(peak, item.equity_close)
        drawdown = (item.equity_close / peak) - 1.0
        if drawdown < max_drawdown:
            max_drawdown = drawdown
            max_drawdown_date = item.trade_date.isoformat()

    long_days = sum(1 for item in performance if item.net_units > 0.0)
    short_days = sum(1 for item in performance if item.net_units < 0.0)
    flat_days = sum(1 for item in performance if math.isclose(item.net_units, 0.0, abs_tol=1e-12))
    close_events = [
        row for row in order_rows if row.realized_pnl_after_cost is not None
    ]
    hit_rate = (
        sum(1 for row in close_events if (row.realized_pnl_after_cost or 0.0) > 0.0) / len(close_events)
        if close_events
        else None
    )
    return {
        "baseline": baseline,
        "window_start": performance[0].trade_date.isoformat() if performance else None,
        "window_end": performance[-1].trade_date.isoformat() if performance else None,
        "decision_days": len(performance),
        "executed_order_count": _reportable_action_count(baseline, order_rows),
        "close_event_count": len(close_events),
        "total_return": total_return,
        "annualized_return": annualized_return,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "max_drawdown_date": max_drawdown_date,
        "hit_rate": hit_rate,
        "average_gross_exposure": _safe_mean([item.gross_exposure_ratio for item in performance]),
        "average_net_exposure": _safe_mean([item.net_exposure_ratio for item in performance]),
        "turnover": sum(item.turnover_ratio for item in performance),
        "transaction_cost_sum": sum(item.transaction_cost for item in performance),
        "long_day_ratio": long_days / len(performance) if performance else None,
        "flat_day_ratio": flat_days / len(performance) if performance else None,
        "short_day_ratio": short_days / len(performance) if performance else None,
        "final_equity": final_equity,
        "max_long_units": max((item.long_units for item in performance), default=0.0),
        "max_short_units": max((item.short_units for item in performance), default=0.0),
    }


def _native_action_distribution(rows: list[dict[str, Any]], key: str, label: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], int] = {}
    totals: dict[str, int] = {}
    for row in rows:
        baseline = str(row["baseline"])
        value = str(row[key])
        grouped[(baseline, value)] = grouped.get((baseline, value), 0) + 1
        totals[baseline] = totals.get(baseline, 0) + 1
    result: list[dict[str, Any]] = []
    for (baseline, value), count in sorted(grouped.items(), key=lambda item: (item[0][0], -item[1], item[0][1])):
        total = totals[baseline]
        result.append(
            {
                "baseline": baseline,
                "action_family": label,
                "action_value": value,
                "count": count,
                "ratio": count / total if total else 0.0,
            }
        )
    return result


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.2f}%"


def _format_num(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _write_equity_svg(path: Path, curves: dict[str, list[DayPerformance]]) -> None:
    width = 1200
    height = 520
    margin_left = 70
    margin_right = 30
    margin_top = 30
    margin_bottom = 50
    inner_width = width - margin_left - margin_right
    inner_height = height - margin_top - margin_bottom
    values = [item.equity_close for series in curves.values() for item in series]
    min_value = min(values)
    max_value = max(values)
    if math.isclose(min_value, max_value):
        max_value += 1.0
    colors = {
        "tradingagents": "#2563eb",
        "finmem": "#dc2626",
        "ai_hedge_fund": "#16a34a",
    }

    def scale_x(index: int, total: int) -> float:
        if total <= 1:
            return margin_left
        return margin_left + inner_width * index / (total - 1)

    def scale_y(value: float) -> float:
        ratio = (value - min_value) / (max_value - min_value)
        return margin_top + inner_height * (1.0 - ratio)

    series_paths: list[str] = []
    legend_items: list[str] = []
    for idx, (baseline, series) in enumerate(curves.items()):
        points = [
            f"{scale_x(point_index, len(series)):.2f},{scale_y(item.equity_close):.2f}"
            for point_index, item in enumerate(series)
        ]
        color = colors.get(baseline, "#111827")
        series_paths.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2.2" points="{" ".join(points)}" />'
        )
        legend_y = margin_top + 20 + idx * 22
        legend_items.append(
            f'<rect x="{width - 220}" y="{legend_y - 10}" width="14" height="14" fill="{color}" />'
            f'<text x="{width - 200}" y="{legend_y + 2}" font-size="13" fill="#111827">{baseline}</text>'
        )

    y_ticks = []
    for step in range(5):
        value = min_value + (max_value - min_value) * step / 4
        y = scale_y(value)
        y_ticks.append(
            f'<line x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}" stroke="#e5e7eb" />'
            f'<text x="{margin_left - 10}" y="{y + 4:.2f}" text-anchor="end" font-size="12" fill="#6b7280">{value:.0f}</text>'
        )

    total_points = max(len(series) for series in curves.values())
    x_ticks = []
    reference_series = next(iter(curves.values()))
    for step in range(6):
        index = round((total_points - 1) * step / 5)
        day = reference_series[index].trade_date.isoformat()
        x = scale_x(index, total_points)
        x_ticks.append(
            f'<line x1="{x:.2f}" y1="{margin_top}" x2="{x:.2f}" y2="{height - margin_bottom}" stroke="#f3f4f6" />'
            f'<text x="{x:.2f}" y="{height - margin_bottom + 20}" text-anchor="middle" font-size="12" fill="#6b7280">{day}</text>'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="#ffffff" />
<text x="{margin_left}" y="20" font-size="18" font-family="Arial, sans-serif" fill="#111827">GCUSD baseline equity curve (AMA recommended_action)</text>
<line x1="{margin_left}" y1="{height - margin_bottom}" x2="{width - margin_right}" y2="{height - margin_bottom}" stroke="#9ca3af" />
<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{height - margin_bottom}" stroke="#9ca3af" />
{''.join(y_ticks)}
{''.join(x_ticks)}
{''.join(series_paths)}
{''.join(legend_items)}
</svg>
"""
    path.write_text(svg, encoding="utf-8")


def _write_report(
    path: Path,
    trading_rows: list[dict[str, Any]],
    runtime_rows: list[dict[str, Any]],
    action_rows: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    output_dir: Path,
    annualization_factor: float,
    cost_bps_per_side: float,
) -> None:
    trading_table = _render_table(
        [
            "Baseline",
            "Total Return",
            "Annualized Return",
            "Sharpe",
            "Sortino",
            "Max Drawdown",
            "Hit Rate",
            "Avg Gross Exposure",
            "Turnover",
            "Long / Flat / Short",
            "Actions",
        ],
        [
            [
                row["baseline"],
                _format_pct(row["total_return"]),
                _format_pct(row["annualized_return"]),
                _format_num(row["sharpe"]),
                _format_num(row["sortino"]),
                _format_pct(row["max_drawdown"]),
                _format_pct(row["hit_rate"]),
                _format_num(row["average_gross_exposure"]),
                _format_num(row["turnover"]),
                f"{row['long_day_ratio'] * 100:.1f}% / {row['flat_day_ratio'] * 100:.1f}% / {row['short_day_ratio'] * 100:.1f}%"
                if row["long_day_ratio"] is not None
                else "n/a",
                str(row["executed_order_count"]),
            ]
            for row in trading_rows
        ],
    )
    runtime_table = _render_table(
        [
            "Baseline",
            "Eval Mode",
            "Raw Decisions",
            "Aligned Decisions",
            "Excluded",
            "Synthetic Holds",
            "LLM Calls",
            "Tokens In",
            "Tokens Out",
            "Avg Latency (s)",
            "P90 Latency (s)",
            "Transient Failures",
        ],
        [
            [
                row["baseline"],
                row["evaluation_mode"],
                str(row["raw_decision_count"]),
                str(row["aligned_real_decision_count"]),
                str(row["excluded_non_common_calendar_count"]),
                str(row["synthetic_hold_count"]),
                str(row["llm_calls"]),
                str(row["tokens_in"]),
                str(row["tokens_out"]),
                _format_num(row["average_decision_latency_seconds"]),
                _format_num(row["p90_decision_latency_seconds"]),
                str(row["transient_failure_artifact_count"]),
            ]
            for row in runtime_rows
        ],
    )
    action_table = _render_table(
        ["Baseline", "Family", "Action", "Count", "Ratio"],
        [
            [
                row["baseline"],
                row["action_family"],
                row["action_value"],
                str(row["count"]),
                _format_pct(row["ratio"]),
            ]
            for row in action_rows
        ],
    )
    mismatch_count = sum(
        1
        for row in diagnostics
        if row["pm_rating"].lower() == "underweight" and row["trader_action"].lower() == "hold"
    )
    report = f"""# Gold Baseline Evaluation

## Scope

- reported window: `2026-01-01` through `2026-06-30`
- instrument: `GCUSD`
- primary FinMem metric: repository action-score, `buy=+1`, `hold/empty=0`, `sell=-1`, applied to the current-close to next-close interval
- primary FinMem cost overlay: `{cost_bps_per_side:.3f}` bps per side whenever the signed action changes
- other baselines: next available `GCUSD` 5-minute bar open after each daily EOD decision
- transaction cost overlay: `{cost_bps_per_side:.3f}` bps per side on executed notional
- action count: actual evaluated exposure changes count once for an open, close, add, reduce, or material rebalance; a reversal that closes one side and opens the opposite side counts as two actions. Records labeled `Hold` or `Unavailable`, including evaluator-generated Hold rebalancing, do not count. For FinMem, `action_score:+0` counts when it closes a prior evaluated `+1` or `-1` exposure; a true `0 -> 0` no-op is not logged. Floating-point residue below `{ACTION_TURNOVER_EPSILON:g}` turnover also does not count. A Buy-and-Hold entry plus terminal liquidation counts as two actions.
- fee definition: `1 bps = 0.01%`; at `0.5` bps per side, a `$100,000` notional open or close costs `$5`, and a complete round trip costs `$10`. Fees are charged on executed notional rather than margin or cash balance.
- primary FinMem annualization factor: `252` observations/year
- other-baseline annualization factor: `{annualization_factor:.3f}` observations/year

## FinMem Evaluation Boundary

- `FinMem` primary: evaluated with the repository's signed daily action-score. This is the paper/repository metric and is not the native portfolio inventory carried internally for feedback.
- `AI Hedge Fund`: reuses the original completed `buy / sell / short / cover / hold` decisions and their native post-trade state. That resulting `long / flat / short` state maps to `+100% / 0% / -100%` gross exposure; no LLM is re-run.
- `TradingAgents`: final portfolio-manager rating is compressed to AMA's three-way `recommended_action` and evaluated with the AMA public stateful long-short convention:
  - `Buy` / `Overweight` -> full long exposure
  - `Hold` -> maintain exposure
  - `Underweight` / `Sell` -> full short exposure

This is the canonical AMA baseline for this Gold run. The internal Trader proposal is retained only as a diagnostic and is not used as the action source.

Trader `Buy / Hold / Sell` is preserved separately as a diagnostic layer. It is not forced into `short` semantics.

## Trading Metrics

{trading_table}

## Runtime And Cost Audit

{runtime_table}

## Action Distribution

{action_table}

## TradingAgents Diagnostic

- stored PM decisions: `{len(diagnostics)}`
- PM `Underweight` with Trader `Hold`: `{mismatch_count}`

This diagnostic exists because TradingAgents' final PM rating and Trader execution proposal can diverge in the official report tree.

## Artifacts

- trading summary: `trading_summary.csv`
- runtime summary: `runtime_summary.csv`
- action distribution: `action_distribution.csv`
- TradingAgents diagnostics: `tradingagents_decision_diagnostics.csv`
- equity curve: `equity_curve.csv`
- daily positions: `position_timeline.csv`
- executed orders: `trade_log.csv`
- equity curve chart: `equity_curve.svg`

All files are written under:

```text
{output_dir}
```
"""
    path.write_text(report, encoding="utf-8")


def main() -> int:
    args = _parse_args()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    daily_bars = _iter_daily_bars(COMMON_DAILY_BARS)
    common_days = [bar.trade_date for bar in daily_bars]
    bars_by_day = {bar.trade_date: bar for bar in daily_bars}
    execution_lookup = _build_execution_lookup(_iter_5min_bars(COMMON_5MIN_BARS), common_days)
    cost_rate = float(args.cost_bps_per_side) / 10_000.0
    annual_factor = _annualization_factor(common_days)

    finmem_decisions = _iter_finmem_decisions()
    aihf_decisions = _iter_aihf_decisions()
    tradingagents_decisions, tradingagents_diagnostics = _iter_tradingagents_decisions(
        execution_lookup
    )
    transient_failures = len(list(TRADINGAGENTS_ROOT.glob("*/failed_audit.json")))

    finmem_perf, finmem_orders, finmem_summary = _evaluate_finmem_action_score(
        common_days, bars_by_day, finmem_decisions, cost_rate
    )
    aihf_perf, aihf_orders = _evaluate_aihf(common_days, bars_by_day, aihf_decisions, cost_rate)
    tradingagents_perf, tradingagents_orders = _evaluate_tradingagents(
        common_days,
        bars_by_day,
        execution_lookup,
        tradingagents_decisions,
        cost_rate,
    )

    rule_decisions = _build_rule_decisions(_iter_rule_history(COMMON_DAILY_BARS), common_days)
    rule_results = {
        baseline: _evaluate_rule_baseline(
            baseline,
            common_days,
            bars_by_day,
            execution_lookup,
            decisions,
            cost_rate,
        )
        for baseline, decisions in rule_decisions.items()
    }
    trading_rows = [
        _summarize_performance("ai_hedge_fund", aihf_perf, aihf_orders, annual_factor),
        finmem_summary,
        _summarize_performance("tradingagents", tradingagents_perf, tradingagents_orders, annual_factor),
    ]
    trading_rows.extend(
        _summarize_performance(baseline, performance, orders, annual_factor)
        for baseline, (performance, orders) in rule_results.items()
    )

    trading_rows.sort(key=lambda row: row["baseline"])
    aihf_runtime_records = [item.runtime for item in aihf_decisions]
    finmem_runtime_records = [item.runtime for item in finmem_decisions]
    tradingagents_runtime_records = [item.runtime for item in tradingagents_decisions]
    aihf_real_days = {item.trade_date for item in aihf_decisions}
    runtime_alignment = {
        "ai_hedge_fund": {
            "raw_decision_count": len(aihf_decisions),
            "aligned_real_decision_count": len(aihf_real_days & set(common_days)),
            "excluded_real_decision_days": sorted(
                day.isoformat() for day in aihf_real_days if day not in set(common_days)
            ),
        },
        "finmem": {
            "raw_decision_count": len(finmem_decisions),
            "aligned_real_decision_count": len(finmem_decisions),
            "excluded_real_decision_days": [],
        },
        "tradingagents": {
            "raw_decision_count": len(tradingagents_decisions),
            "aligned_real_decision_count": len(tradingagents_decisions),
            "excluded_real_decision_days": [],
        },
    }
    runtime_rows = [
        _baseline_runtime_row(
            "ai_hedge_fund",
            aihf_runtime_records,
            raw_decision_count=runtime_alignment["ai_hedge_fund"]["raw_decision_count"],
            aligned_real_decision_count=runtime_alignment["ai_hedge_fund"]["aligned_real_decision_count"],
            synthetic_hold_count=len(common_days) - runtime_alignment["ai_hedge_fund"]["aligned_real_decision_count"],
            evaluation_mode="native_state_to_full_exposure",
        ),
        _baseline_runtime_row(
            "finmem",
            finmem_runtime_records,
            raw_decision_count=len(finmem_decisions),
            aligned_real_decision_count=len(finmem_decisions),
            synthetic_hold_count=0,
            evaluation_mode="repository_action_score_same_close",
        ),
        _baseline_runtime_row(
            "tradingagents",
            tradingagents_runtime_records,
            raw_decision_count=len(tradingagents_decisions),
            aligned_real_decision_count=len(tradingagents_decisions),
            synthetic_hold_count=0,
            transient_failure_count=transient_failures,
            evaluation_mode="ama_recommended_action_stateful_long_short",
        ),
    ]
    runtime_rows.sort(key=lambda row: row["baseline"])
    runtime_rows.extend(
        _baseline_runtime_row(
            baseline,
            [],
            raw_decision_count=len(decisions),
            aligned_real_decision_count=len(decisions),
            synthetic_hold_count=0,
            evaluation_mode="deterministic_price_rule",
        )
        for baseline, decisions in rule_decisions.items()
    )

    action_rows: list[dict[str, Any]] = []
    action_rows.extend(
        _native_action_distribution(
            [
                {"baseline": "ai_hedge_fund", "native_action": item.native_action}
                for item in aihf_decisions
            ],
            "native_action",
            "native_action",
        )
    )
    action_rows.extend(
        _native_action_distribution(
            [
                {"baseline": "ai_hedge_fund", "native_state": item.target_position}
                for item in aihf_decisions
            ],
            "native_state",
            "native_state",
        )
    )
    action_rows.extend(
        _native_action_distribution(
            [
                {"baseline": "finmem", "native_action": item.native_action}
                for item in finmem_decisions
            ],
            "native_action",
            "native_action",
        )
    )
    action_rows.extend(
        _native_action_distribution(
            tradingagents_diagnostics,
            "pm_rating",
            "pm_rating",
        )
    )
    action_rows.extend(
        _native_action_distribution(
            tradingagents_diagnostics,
            "trader_action",
            "trader_action",
        )
    )
    action_rows.sort(key=lambda row: (row["baseline"], row["action_family"], -row["count"], row["action_value"]))
    for baseline, decisions in rule_decisions.items():
        action_rows.extend(
            _native_action_distribution(
                [
                    {"baseline": baseline, "rule_target": _rule_action(item.target_exposure)}
                    for item in decisions
                ],
                "rule_target",
                "rule_target",
            )
        )

    curves = {
        "ai_hedge_fund": aihf_perf,
        "finmem": finmem_perf,
        "tradingagents": tradingagents_perf,
    }
    equity_rows: list[dict[str, Any]] = []
    curves.update(
        {baseline: performance for baseline, (performance, _) in rule_results.items()}
    )
    position_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    for series in curves.values():
        for item in series:
            equity_rows.append(
                {
                    "baseline": item.baseline,
                    "trade_date": item.trade_date.isoformat(),
                    "equity_close": f"{item.equity_close:.6f}",
                    "net_return": f"{item.net_return:.10f}",
                    "transaction_cost": f"{item.transaction_cost:.10f}",
                }
            )
            position_rows.append(
                {
                    "baseline": item.baseline,
                    "trade_date": item.trade_date.isoformat(),
                    "native_action": item.native_action,
                    "decision_detail": item.decision_detail,
                    "long_units": f"{item.long_units:.6f}",
                    "short_units": f"{item.short_units:.6f}",
                    "net_units": f"{item.net_units:.6f}",
                    "gross_exposure_ratio": f"{item.gross_exposure_ratio:.6f}",
                    "net_exposure_ratio": f"{item.net_exposure_ratio:.6f}",
                    "turnover_ratio": f"{item.turnover_ratio:.6f}",
                }
            )
    all_order_rows = [*aihf_orders, *finmem_orders, *tradingagents_orders]
    for _, orders in rule_results.values():
        all_order_rows.extend(orders)
    for row in all_order_rows:
        trade_rows.append(
            {
                "baseline": row.baseline,
                "source_trade_date": row.source_trade_date,
                "execution_day": row.execution_day,
                "action_label": row.action_label,
                "quantity": f"{row.quantity:.6f}",
                "execution_price": f"{row.execution_price:.6f}",
                "turnover_ratio": f"{row.turnover_ratio:.10f}",
                "transaction_cost": f"{row.transaction_cost:.10f}",
                "realized_pnl_after_cost": (
                    f"{row.realized_pnl_after_cost:.10f}"
                    if row.realized_pnl_after_cost is not None
                    else ""
                ),
                "long_units_after": f"{row.long_units_after:.6f}",
                "short_units_after": f"{row.short_units_after:.6f}",
                "cash_after": f"{row.cash_after:.10f}",
            }
        )
    equity_rows.sort(key=lambda row: (row["trade_date"], row["baseline"]))
    position_rows.sort(key=lambda row: (row["trade_date"], row["baseline"]))
    trade_rows.sort(key=lambda row: (row["baseline"], row["execution_day"], row["source_trade_date"]))

    _write_csv(output_dir / "trading_summary.csv", trading_rows)
    _write_csv(output_dir / "runtime_summary.csv", runtime_rows)
    _write_csv(output_dir / "action_distribution.csv", action_rows)
    _write_csv(output_dir / "tradingagents_decision_diagnostics.csv", tradingagents_diagnostics)
    _write_csv(output_dir / "equity_curve.csv", equity_rows)
    _write_csv(output_dir / "position_timeline.csv", position_rows)
    _write_csv(output_dir / "trade_log.csv", trade_rows)

    _write_json(output_dir / "trading_summary.json", trading_rows)
    _write_json(output_dir / "runtime_summary.json", runtime_rows)
    _write_json(output_dir / "action_distribution.json", action_rows)
    _write_json(output_dir / "tradingagents_decision_diagnostics.json", tradingagents_diagnostics)
    _write_json(
        output_dir / "calendar_alignment.json",
        {
            "window_start": WINDOW_START.isoformat(),
            "window_end": WINDOW_END.isoformat(),
            "common_calendar_day_count": len(common_days),
            "common_calendar_days": [day.isoformat() for day in common_days],
            "baseline_alignment": runtime_alignment,
        },
    )
    _write_equity_svg(output_dir / "equity_curve.svg", curves)
    _write_report(
        output_dir / "README.md",
        trading_rows=trading_rows,
        runtime_rows=runtime_rows,
        action_rows=action_rows,
        diagnostics=tradingagents_diagnostics,
        output_dir=output_dir,
        annualization_factor=annual_factor,
        cost_bps_per_side=float(args.cost_bps_per_side),
    )
    print(f"Wrote GCUSD baseline evaluation to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
