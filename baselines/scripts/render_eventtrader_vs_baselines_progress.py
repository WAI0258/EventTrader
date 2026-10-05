from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


INITIAL_EQUITY = 100_000.0
WINDOW_START = date(2026, 1, 1)
WINDOW_CAP_END = date(2026, 6, 30)
COST_BPS_PER_SIDE = 0.5
COST_RATE = COST_BPS_PER_SIDE / 10_000.0
ACTION_TURNOVER_EPSILON = 1e-8
ACTION_REVERSAL_TURNOVER_THRESHOLD = 1.5
UTC = timezone.utc

BASE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = BASE_DIR.parent
EVAL_DIR = WORKSPACE_ROOT / "experiments" / "gold_baseline_eval_20260101_20260630"
BASELINE_EQUITY_CSV = EVAL_DIR / "equity_curve.csv"
BASELINE_POSITION_CSV = EVAL_DIR / "position_timeline.csv"
BASELINE_TRADE_LOG_CSV = EVAL_DIR / "trade_log.csv"
BASELINE_SUMMARY_CSV = EVAL_DIR / "trading_summary.csv"
BASELINE_DAILY_BARS_CSV = (
    WORKSPACE_ROOT
    / "gold_futures"
    / "experiment_gcusd_inputs"
    / "project_inputs"
    / "tradingagents"
    / "gcusd_daily_ohlcv_20250630_20260630.csv"
)

EVENT_TRADER_WORKSPACE = Path(
    r"code\.local\server-gold-20260101-20260630\replay-gold-20260101_20260630-workspace"
)
EVENT_TRADER_BARS = (
    EVENT_TRADER_WORKSPACE
    / "runtime"
    / "market_data"
    / "gold"
    / "gold-gcusd-20260101-20260630"
    / "bars"
    / "GCUSD_5min.jsonl"
)
EVENT_TRADER_ANALYSIS_DIR = (
    EVENT_TRADER_WORKSPACE / "runtime" / "analysis_assessments" / "gold"
)

DEFAULT_OUTPUT_STEM = "eventtrader_vs_baselines_progress"
PLOTTED_BASELINES = (
    "tradingagents",
    "ai_hedge_fund",
    "finmem",
    "buy_and_hold",
)


@dataclass(frozen=True)
class BaselinePoint:
    trade_date: date
    equity_close: float
    transaction_cost: float


@dataclass(frozen=True)
class EventStateChange:
    effective_at: datetime
    target_weight: float


@dataclass(frozen=True)
class MarketBar:
    start_at: datetime
    end_at: datetime
    open_price: float
    close_price: float


@dataclass(frozen=True)
class DailyBar:
    trade_date: date
    open_price: float
    close_price: float


@dataclass(frozen=True)
class SummaryRow:
    strategy: str
    final_equity: float
    total_return: float
    max_drawdown: float
    max_drawdown_date: date | None
    fees: float
    orders: int


@dataclass(frozen=True)
class PositionSnapshot:
    trade_date: date
    long_units: float
    short_units: float


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render EventTrader vs baseline progress chart for GCUSD."
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=None,
        help="Optional inclusive window end date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--output-stem",
        default=DEFAULT_OUTPUT_STEM,
        help="Output filename stem written under the current evaluation directory.",
    )
    return parser.parse_args()


def _output_paths(output_stem: str) -> tuple[Path, Path, Path]:
    return (
        EVAL_DIR / f"{output_stem}.svg",
        EVAL_DIR / f"{output_stem}.csv",
        EVAL_DIR / f"{output_stem}.md",
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _read_daily_bars() -> dict[date, DailyBar]:
    bars: dict[date, DailyBar] = {}
    with BASELINE_DAILY_BARS_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            trade_day = date.fromisoformat(row["date"])
            if WINDOW_START <= trade_day <= WINDOW_CAP_END:
                bars[trade_day] = DailyBar(
                    trade_date=trade_day,
                    open_price=float(row["open"]),
                    close_price=float(row["close"]),
                )
    if not bars:
        raise RuntimeError(f"No GCUSD daily bars found in {BASELINE_DAILY_BARS_CSV}")
    return bars


def _rebuild_baseline_curves_from_positions() -> dict[str, list[BaselinePoint]]:
    daily_bars = _read_daily_bars()
    fees_by_day: dict[tuple[str, date], float] = {}
    cash_after_by_day: dict[tuple[str, date], float] = {}

    with BASELINE_TRADE_LOG_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            baseline = row["baseline"]
            execution_day = date.fromisoformat(row["execution_day"])
            if not (WINDOW_START <= execution_day <= WINDOW_CAP_END):
                continue
            key = (baseline, execution_day)
            fees_by_day[key] = fees_by_day.get(key, 0.0) + float(row["transaction_cost"])
            cash_after_by_day[key] = float(row["cash_after"])

    positions: dict[str, list[PositionSnapshot]] = {}
    with BASELINE_POSITION_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            baseline = row["baseline"]
            trade_day = date.fromisoformat(row["trade_date"])
            if not (WINDOW_START <= trade_day <= WINDOW_CAP_END):
                continue
            positions.setdefault(baseline, []).append(
                PositionSnapshot(
                    trade_date=trade_day,
                    long_units=float(row["long_units"]),
                    short_units=float(row["short_units"]),
                )
            )

    curves: dict[str, list[BaselinePoint]] = {}
    for baseline, snapshots in positions.items():
        snapshots.sort(key=lambda item: item.trade_date)
        carry_cash = INITIAL_EQUITY
        series: list[BaselinePoint] = []
        for snapshot in snapshots:
            key = (baseline, snapshot.trade_date)
            if key in cash_after_by_day:
                carry_cash = cash_after_by_day[key]
            daily_bar = daily_bars.get(snapshot.trade_date)
            if daily_bar is None:
                raise RuntimeError(
                    f"Missing GCUSD daily close for {baseline} on {snapshot.trade_date.isoformat()}"
                )
            equity_close = (
                carry_cash
                + snapshot.long_units * daily_bar.close_price
                - snapshot.short_units * daily_bar.close_price
            )
            series.append(
                BaselinePoint(
                    trade_date=snapshot.trade_date,
                    equity_close=equity_close,
                    transaction_cost=fees_by_day.get(key, 0.0),
                )
            )
        if series:
            curves[baseline] = series

    if not curves:
        raise RuntimeError(
            f"Unable to rebuild baseline curves from {BASELINE_POSITION_CSV} and {BASELINE_TRADE_LOG_CSV}"
        )
    return curves


def _validate_rebuilt_curves(curves: dict[str, list[BaselinePoint]]) -> None:
    if not BASELINE_SUMMARY_CSV.exists():
        return

    expected: dict[str, float] = {}
    with BASELINE_SUMMARY_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            expected[row["baseline"]] = float(row["final_equity"])

    mismatches: list[str] = []
    for baseline, series in curves.items():
        if baseline not in expected or not series:
            continue
        actual = series[-1].equity_close
        if abs(actual - expected[baseline]) > 0.05:
            mismatches.append(
                f"{baseline}: rebuilt={actual:.6f}, summary={expected[baseline]:.6f}"
            )
    if mismatches:
        raise RuntimeError(
            "Rebuilt baseline curves do not match trading_summary.csv final_equity: "
            + "; ".join(mismatches)
        )


def _read_baseline_curves() -> dict[str, list[BaselinePoint]]:
    curves: dict[str, list[BaselinePoint]] = {}
    if BASELINE_EQUITY_CSV.exists():
        with BASELINE_EQUITY_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                trade_day = date.fromisoformat(row["trade_date"])
                curves.setdefault(row["baseline"], []).append(
                    BaselinePoint(
                        trade_date=trade_day,
                        equity_close=float(row["equity_close"]),
                        transaction_cost=float(row["transaction_cost"]),
                    )
                )
        return {k: v for k, v in curves.items() if k in PLOTTED_BASELINES}

    if not BASELINE_POSITION_CSV.exists():
        raise FileNotFoundError(
            f"Missing baseline equity source {BASELINE_EQUITY_CSV} and rebuild source {BASELINE_POSITION_CSV}"
        )

    curves = _rebuild_baseline_curves_from_positions()
    _validate_rebuilt_curves(curves)
    return {k: v for k, v in curves.items() if k in PLOTTED_BASELINES}


def _read_baseline_order_counts(end_date: date) -> dict[str, int]:
    counts: dict[str, int] = {}
    with BASELINE_TRADE_LOG_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            execution_day = date.fromisoformat(row["execution_day"])
            if execution_day > end_date:
                continue
            baseline = row["baseline"]
            if baseline not in PLOTTED_BASELINES:
                continue
            turnover = float(row["turnover_ratio"])
            realized = row.get("realized_pnl_after_cost", "")
            action_label = row.get("action_label", "").lower()
            if (
                "hold" not in action_label
                and "unavailable" not in action_label
                and (turnover > ACTION_TURNOVER_EPSILON or realized not in ("", "None"))
            ):
                counts[baseline] = counts.get(baseline, 0) + 1 + int(
                    turnover > ACTION_REVERSAL_TURNOVER_THRESHOLD
                )
    return counts


def _latest_baseline_date(curves: dict[str, list[BaselinePoint]]) -> date:
    return max(series[-1].trade_date for series in curves.values() if series)


def _latest_event_trader_date() -> date:
    latest: date | None = None
    for path in sorted(EVENT_TRADER_ANALYSIS_DIR.glob("*.jsonl")):
        for row in _read_jsonl(path):
            business_at = datetime.fromisoformat(str(row["business_at"])).astimezone(UTC).date()
            if latest is None or business_at > latest:
                latest = business_at
    if latest is None:
        raise RuntimeError("No event-trader analysis assessments found.")
    return latest


def _analysis_state_weight(state: str) -> float:
    mapping = {
        "strong_long": 1.0,
        "weak_long": 0.5,
        "flat": 0.0,
        "weak_short": -0.5,
        "strong_short": -1.0,
    }
    try:
        return mapping[state.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"Unsupported EventTrader analysis state: {state}") from exc


def _read_event_state_changes(end_date: date) -> list[EventStateChange]:
    end_dt = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    changes_by_time: dict[datetime, EventStateChange] = {}
    for path in sorted(EVENT_TRADER_ANALYSIS_DIR.glob("*.jsonl")):
        for row in _read_jsonl(path):
            if row.get("target_key") != "gold":
                continue
            effective_at = datetime.fromisoformat(str(row["business_at"])).astimezone(UTC)
            if effective_at >= end_dt:
                continue
            changes_by_time[effective_at] = EventStateChange(
                effective_at=effective_at,
                target_weight=_analysis_state_weight(str(row["as_if_flat_state"])),
            )
    return [changes_by_time[key] for key in sorted(changes_by_time)]


def _read_event_bars(end_date: date) -> list[MarketBar]:
    end_dt = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    start_dt = datetime.combine(WINDOW_START, datetime.min.time(), tzinfo=UTC)
    bars: list[MarketBar] = []
    for row in _read_jsonl(EVENT_TRADER_BARS):
        start_at = datetime.fromisoformat(str(row["start_at"])).astimezone(UTC)
        end_at = datetime.fromisoformat(str(row["end_at"])).astimezone(UTC)
        if end_at <= start_dt or start_at >= end_dt:
            continue
        bars.append(
            MarketBar(
                start_at=start_at,
                end_at=end_at,
                open_price=float(row["open_price"]),
                close_price=float(row["close_price"]),
            )
        )
    bars.sort(key=lambda item: item.start_at)
    return bars


def _simulate_event_trader_progress(end_date: date) -> tuple[list[BaselinePoint], float, int]:
    bars = _read_event_bars(end_date)
    changes = _read_event_state_changes(end_date)
    anchor_days = sorted(day for day in _read_daily_bars() if WINDOW_START <= day <= end_date)
    if not bars:
        raise RuntimeError("No GCUSD 5-minute bars found for event-trader window.")
    if not anchor_days:
        raise RuntimeError("No GCUSD daily anchor bars found for event-trader window.")

    current_weight = 0.0
    equity = INITIAL_EQUITY
    total_fees = 0.0
    order_count = 0
    change_index = 0
    daily_equity: dict[date, float] = {}

    for bar in bars:
        while change_index < len(changes) and changes[change_index].effective_at <= bar.start_at:
            next_weight = changes[change_index].target_weight
            turnover = abs(next_weight - current_weight)
            if turnover > 0.0:
                fee = equity * turnover * COST_RATE
                equity -= fee
                total_fees += fee
                order_count += 2 if current_weight * next_weight < 0.0 else 1
            current_weight = next_weight
            change_index += 1
        underlying_return = bar.close_price / bar.open_price - 1.0
        equity *= 1.0 + current_weight * underlying_return
        daily_equity[bar.start_at.date()] = equity

    series: list[BaselinePoint] = []
    carry = INITIAL_EQUITY
    for anchor_day in anchor_days:
        if anchor_day in daily_equity:
            carry = daily_equity[anchor_day]
        series.append(
            BaselinePoint(
                trade_date=anchor_day,
                equity_close=carry,
                transaction_cost=0.0,
            )
        )
    return series, total_fees, order_count




def _truncate_baselines(
    curves: dict[str, list[BaselinePoint]],
    end_date: date,
) -> dict[str, list[BaselinePoint]]:
    filtered_curves: dict[str, list[BaselinePoint]] = {}
    for baseline, series in curves.items():
        filtered = [item for item in series if WINDOW_START <= item.trade_date <= end_date]
        if filtered:
            filtered_curves[baseline] = filtered

    calendar = sorted(
        {
            item.trade_date
            for series in filtered_curves.values()
            for item in series
        }
    )
    aligned: dict[str, list[BaselinePoint]] = {}
    for baseline, series in filtered_curves.items():
        points_by_day = {item.trade_date: item for item in series}
        last_point: BaselinePoint | None = None
        aligned_series: list[BaselinePoint] = []
        for trade_day in calendar:
            point = points_by_day.get(trade_day)
            if point is not None:
                last_point = point
            elif last_point is not None:
                point = BaselinePoint(
                    trade_date=trade_day,
                    equity_close=last_point.equity_close,
                    transaction_cost=0.0,
                )
            if point is not None:
                aligned_series.append(point)
        aligned[baseline] = aligned_series
    return aligned


def _build_summary(
    strategy: str,
    series: list[BaselinePoint],
    fees_override: float | None,
    orders: int,
) -> SummaryRow:
    peak = series[0].equity_close
    max_drawdown = 0.0
    max_drawdown_date: date | None = None
    for item in series:
        if item.equity_close > peak:
            peak = item.equity_close
        drawdown = 1.0 - item.equity_close / peak
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_date = item.trade_date
    final_equity = series[-1].equity_close
    fees = fees_override if fees_override is not None else sum(item.transaction_cost for item in series)
    return SummaryRow(
        strategy=strategy,
        final_equity=final_equity,
        total_return=final_equity / INITIAL_EQUITY - 1.0,
        max_drawdown=max_drawdown,
        max_drawdown_date=max_drawdown_date,
        fees=fees,
        orders=orders,
    )


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _write_csv(path: Path, curves: dict[str, list[BaselinePoint]]) -> None:
    rows: list[dict[str, str]] = []
    for strategy, series in sorted(curves.items()):
        cumulative_fees = 0.0
        for item in series:
            cumulative_fees += item.transaction_cost
            rows.append(
                {
                    "strategy": strategy,
                    "trade_date": item.trade_date.isoformat(),
                    "equity_close": f"{item.equity_close:.6f}",
                    "transaction_cost": f"{item.transaction_cost:.10f}",
                    "cumulative_transaction_cost": f"{cumulative_fees:.10f}",
                }
            )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "strategy",
                "trade_date",
                "equity_close",
                "transaction_cost",
                "cumulative_transaction_cost",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def _render_svg(
    output_svg: Path,
    curves: dict[str, list[BaselinePoint]],
    summaries: list[SummaryRow],
    end_date: date,
    *,
    latest_event_end: date,
) -> None:
    width = 1600
    height = 900
    margin_left = 116
    margin_right = 48
    margin_top = 126
    margin_bottom = 102
    plot_x = margin_left
    plot_y = 150
    plot_width = width - margin_left - margin_right
    plot_height = 610

    colors = {
        "event_trader": "#d62728",
        "tradingagents": "#1f77b4",
        "ai_hedge_fund": "#2ca02c",
        "finmem": "#9467bd",
        "buy_and_hold": "#6b7280",
    }
    labels = {
        "event_trader": "EventTrader",
        "tradingagents": "TradingAgents",
        "ai_hedge_fund": "AI Hedge Fund",
        "finmem": "FinMem",
        "buy_and_hold": "Buy & Hold",
    }

    ref_series = next(iter(curves.values()))
    total_steps = len(ref_series)

    def to_return_pct(point: BaselinePoint) -> float:
        return (point.equity_close / INITIAL_EQUITY - 1.0) * 100.0

    overview_returns = [
        to_return_pct(point)
        for series in curves.values()
        for point in series
    ]
    plot_min = min(overview_returns)
    plot_max = max(overview_returns)
    plot_pad = max((plot_max - plot_min) * 0.10, 0.8)
    plot_y_min = min(plot_min - plot_pad, -1.0)
    plot_y_max = max(plot_max + plot_pad, 1.0)

    def sx(index: int, *, x0: float, width_px: float) -> float:
        if total_steps <= 1:
            return x0
        return x0 + width_px * index / (total_steps - 1)

    def sy(value: float, *, y0: float, height_px: float, ymin: float, ymax: float) -> float:
        ratio = (value - ymin) / (ymax - ymin)
        return y0 + height_px * (1.0 - ratio)

    def money(value: float) -> str:
        return f"${value:,.0f}"

    def pct(value: float) -> str:
        return f"{value * 100:.2f}%"

    def fmt_return_pct(value: float) -> str:
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.1f}%"

    def add_y_axis(
        parts: list[str],
        *,
        x0: float,
        width_px: float,
        y0: float,
        height_px: float,
        vmin: float,
        vmax: float,
        font_size: int,
    ) -> None:
        for tick in range(6):
            value = vmin + (vmax - vmin) * tick / 5
            y = sy(value, y0=y0, height_px=height_px, ymin=vmin, ymax=vmax)
            parts.append(
                f'<line x1="{x0}" y1="{y:.2f}" x2="{x0 + width_px}" y2="{y:.2f}" stroke="#e5e7eb" stroke-width="1" />'
            )
            parts.append(
                f'<text x="{x0 - 14}" y="{y + 5:.2f}" text-anchor="end" font-family="Arial, sans-serif" font-size="{font_size}" fill="#374151">{fmt_return_pct(value)}</text>'
            )

    def add_x_axis(
        parts: list[str],
        *,
        x0: float,
        width_px: float,
        panel_y: float,
        panel_height: float,
        label_y: float,
        font_size: int,
    ) -> None:
        for tick in range(6):
            index = round((total_steps - 1) * tick / 5)
            x = sx(index, x0=x0, width_px=width_px)
            day = ref_series[index].trade_date.isoformat()
            parts.append(
                f'<line x1="{x:.2f}" y1="{panel_y}" x2="{x:.2f}" y2="{panel_y + panel_height}" stroke="#f3f4f6" stroke-width="1" />'
            )
            parts.append(
                f'<text x="{x:.2f}" y="{label_y}" text-anchor="middle" font-family="Arial, sans-serif" font-size="{font_size}" fill="#374151">{day}</text>'
            )

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="#ffffff" />',
        f'<rect x="{plot_x}" y="{plot_y}" width="{plot_width}" height="{plot_height}" fill="#ffffff" stroke="#9ca3af" stroke-width="1.2" />',
    ]

    add_y_axis(
        parts,
        x0=plot_x,
        width_px=plot_width,
        y0=plot_y,
        height_px=plot_height,
        vmin=plot_y_min,
        vmax=plot_y_max,
        font_size=18,
    )

    init_y = sy(0.0, y0=plot_y, height_px=plot_height, ymin=plot_y_min, ymax=plot_y_max)
    parts.append(
        f'<line x1="{plot_x}" y1="{init_y:.2f}" x2="{plot_x + plot_width}" y2="{init_y:.2f}" stroke="#6b7280" stroke-width="1.4" stroke-dasharray="5 5" />'
    )

    add_x_axis(
        parts,
        x0=plot_x,
        width_px=plot_width,
        panel_y=plot_y,
        panel_height=plot_height,
        label_y=plot_y + plot_height + 30,
        font_size=17,
    )

    draw_order = ["event_trader", *PLOTTED_BASELINES]
    for strategy in draw_order:
        series = curves[strategy]
        color = colors[strategy]
        points = " ".join(
            f"{sx(idx, x0=plot_x, width_px=plot_width):.2f},{sy(to_return_pct(item), y0=plot_y, height_px=plot_height, ymin=plot_y_min, ymax=plot_y_max):.2f}"
            for idx, item in enumerate(series)
        )
        stroke_width = "4.8" if strategy == "event_trader" else "2.8"
        dasharray = ' stroke-dasharray="8 6"' if strategy == "buy_and_hold" else ""
        parts.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="{stroke_width}"{dasharray} stroke-linecap="round" stroke-linejoin="round" points="{points}" />'
        )

    for index, strategy in enumerate(draw_order):
        legend_y = 100
        legend_x = plot_x + 270 * index
        dasharray = ' stroke-dasharray="8 6"' if strategy == "buy_and_hold" else ""
        parts.append(
            f'<line x1="{legend_x}" y1="{legend_y - 5}" x2="{legend_x + 32}" y2="{legend_y - 5}" stroke="{colors[strategy]}" stroke-width="{"4.8" if strategy == "event_trader" else "2.8"}"{dasharray} stroke-linecap="round" />'
        )
        parts.append(
            f'<text x="{legend_x + 44}" y="{legend_y + 1}" font-family="Arial, sans-serif" font-size="18" fill="#111827">{_xml_escape(labels[strategy])}</text>'
        )

    parts.extend(
        [
            f'<text x="{margin_left}" y="48" font-family="Arial, sans-serif" font-size="30" font-weight="700" fill="#111827">{_xml_escape("GCUSD cumulative return")}</text>',
            f'<text x="{margin_left}" y="76" font-family="Arial, sans-serif" font-size="18" fill="#4b5563">{WINDOW_START.isoformat()} to {end_date.isoformat()} | initial equity {money(INITIAL_EQUITY)} | EventTrader is ours</text>',
            f'<text transform="translate(32 {plot_y + plot_height / 2:.2f}) rotate(-90)" font-family="Arial, sans-serif" font-size="19" fill="#374151">Cumulative return (%)</text>',
            f'<text x="{plot_x + plot_width / 2:.2f}" y="{height - 30}" text-anchor="middle" font-family="Arial, sans-serif" font-size="20" fill="#374151">Trading day</text>',
            "</svg>",
        ]
    )
    output_svg.write_text("".join(parts), encoding="utf-8")


def _write_md(output_md: Path, output_svg: Path, output_csv: Path, end_date: date, summaries: list[SummaryRow], *, latest_event_end: date) -> None:
    ordered = sorted(summaries, key=lambda item: item.final_equity, reverse=True)
    by_strategy = {item.strategy: item for item in summaries}
    buy_hold = by_strategy["buy_and_hold"]
    lines = [
        "# GCUSD EventTrader vs Agent Baselines",
        "",
        f"- window start: `{WINDOW_START.isoformat()}`",
        f"- {'auto-detected' if end_date == latest_event_end else 'requested'} window end: `{end_date.isoformat()}`",
        f"- initial equity: `${INITIAL_EQUITY:,.0f}`",
        f"- transaction-cost assumption: `{COST_BPS_PER_SIDE:.3f}` bps per side on executed notional",
        "- action definition: actual evaluated exposure changes count once for an open, close, add, reduce, or material rebalance; a reversal that closes one side and opens the opposite side counts as 2 actions. Records labeled Hold or Unavailable, including evaluator-generated Hold rebalancing, are excluded. For FinMem, an `action_score:+0` record counts when it closes a prior evaluated `+1` or `-1` exposure; a true `0 -> 0` no-op is not logged. Floating-point residue below `1e-8` turnover is also excluded. Buy & Hold is counted as entry plus terminal liquidation, so it has 2 actions.",
        "- fee definition: `1 bps = 0.01%`; at `0.5` bps per side, a `$100,000` notional open or close costs `$5`, and a complete round trip costs `$10`. Fees use executed notional, not margin or cash balance.",
        "- Buy & Hold is included as a passive reference and is not an agent baseline",
        f"- event-trader note: {'this figure is clipped to the current EventTrader progress window, so the comparison is an in-progress snapshot rather than a full-period final ranking' if end_date == latest_event_end else 'this figure uses a fixed historical cutoff, so all strategies are compared only through the requested date'}",
        "",
        "| Rank | Strategy | Final Equity | Total Return | Excess vs Buy & Hold | Max Drawdown | MaxDD Date | Fees | Fees / Gross PnL | Actions |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for rank, summary in enumerate(ordered, start=1):
        gross_pnl = summary.final_equity - INITIAL_EQUITY + summary.fees
        fee_share = (
            f"{(summary.fees / gross_pnl) * 100:.2f}%"
            if abs(gross_pnl) > 1e-9
            else "n/a"
        )
        lines.append(
            f"| {rank} | {summary.strategy} | ${summary.final_equity:,.2f} | {summary.total_return * 100:.2f}% | {(summary.total_return - buy_hold.total_return) * 100:.2f}% | {summary.max_drawdown * 100:.2f}% | {summary.max_drawdown_date.isoformat() if summary.max_drawdown_date is not None else 'n/a'} | ${summary.fees:,.2f} | {fee_share} | {summary.orders} |"
        )
    lines.extend(
        [
            "",
            "## Reading Guide",
            "",
            "- `Excess vs Buy & Hold` is the return spread versus the passive reference over the same window.",
            "- `Fees / Gross PnL` uses `fees / (final_equity - initial_equity + fees)` and is shown only when gross PnL is non-zero.",
            "- `MaxDD Date` is the date when the worst peak-to-trough equity drawdown is observed within the clipped window.",
            "- The Buy & Hold terminal liquidation is included so its two-sided fee treatment matches the action definition.",
            "",
            "## Artifacts",
            "",
            f"- figure: `{output_svg.name}`",
            f"- data: `{output_csv.name}`",
        ]
    )
    output_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = _parse_args()
    output_svg, output_csv, output_md = _output_paths(args.output_stem)
    baseline_curves = _read_baseline_curves()
    baseline_end = _latest_baseline_date(baseline_curves)
    event_end = _latest_event_trader_date()
    available_end = min(WINDOW_CAP_END, baseline_end, event_end)
    if args.end_date is None:
        end_date = available_end
    else:
        if args.end_date < WINDOW_START:
            raise ValueError(
                f"Requested end date {args.end_date.isoformat()} is before window start {WINDOW_START.isoformat()}."
            )
        if args.end_date > available_end:
            raise ValueError(
                f"Requested end date {args.end_date.isoformat()} exceeds available progress end {available_end.isoformat()}."
            )
        end_date = args.end_date

    truncated = _truncate_baselines(baseline_curves, end_date)
    baseline_orders = _read_baseline_order_counts(end_date)
    event_series, event_fees, event_orders = _simulate_event_trader_progress(end_date)
    truncated["event_trader"] = event_series

    summaries: list[SummaryRow] = []
    for strategy, series in truncated.items():
        fees_override = event_fees if strategy == "event_trader" else None
        orders = event_orders if strategy == "event_trader" else baseline_orders.get(strategy, 0)
        summaries.append(_build_summary(strategy, series, fees_override, orders))

    _write_csv(output_csv, truncated)
    _render_svg(output_svg, truncated, summaries, end_date, latest_event_end=event_end)
    _write_md(output_md, output_svg, output_csv, end_date, summaries, latest_event_end=event_end)
    print(
        json.dumps(
            {
                "window_start": WINDOW_START.isoformat(),
                "window_end": end_date.isoformat(),
                "svg": str(output_svg),
                "csv": str(output_csv),
                "md": str(output_md),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
