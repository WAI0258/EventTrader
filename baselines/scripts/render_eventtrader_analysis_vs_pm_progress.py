from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path


INITIAL_EQUITY = 100_000.0
COST_BPS_PER_SIDE = 0.5
COST_RATE = COST_BPS_PER_SIDE / 10_000.0
WINDOW_START = date(2026, 1, 1)
WINDOW_END = date(2026, 6, 30)
ACTION_TURNOVER_EPSILON = 1e-12
UTC = timezone.utc

BASELINE_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = BASELINE_ROOT / "experiments" / "gold_baseline_eval_20260101_20260630"
DAILY_BARS_CSV = (
    BASELINE_ROOT
    / "gold_futures"
    / "experiment_gcusd_inputs"
    / "project_inputs"
    / "tradingagents"
    / "gcusd_daily_ohlcv_20250630_20260630.csv"
)
EVENT_TRADER_WORKSPACE = Path(
    r"code\.local\server-gold-20260101-20260630\replay-gold-20260101_20260630-workspace"
)
ANALYSIS_DIR = EVENT_TRADER_WORKSPACE / "runtime" / "analysis_assessments" / "gold"
PM_STATE_CHANGES_DIR = (
    EVENT_TRADER_WORKSPACE / "runtime" / "validation" / "state-changes" / "gold"
)
EVENT_BARS_JSONL = (
    EVENT_TRADER_WORKSPACE
    / "runtime"
    / "market_data"
    / "gold"
    / "gold-gcusd-20260101-20260630"
    / "bars"
    / "GCUSD_5min.jsonl"
)
DEFAULT_OUTPUT_STEM = "eventtrader_analysis_vs_pm_progress"


@dataclass(frozen=True)
class Change:
    effective_at: datetime
    target_weight: float


@dataclass(frozen=True)
class Bar:
    start_at: datetime
    end_at: datetime
    open_price: float
    close_price: float


@dataclass(frozen=True)
class Point:
    trade_date: date
    equity_close: float


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render an independent analysis-only versus PM exposure comparison."
    )
    parser.add_argument("--output-stem", default=DEFAULT_OUTPUT_STEM)
    return parser.parse_args()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _read_daily_dates() -> list[date]:
    dates: list[date] = []
    with DAILY_BARS_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            trade_day = date.fromisoformat(row["date"])
            if WINDOW_START <= trade_day <= WINDOW_END:
                dates.append(trade_day)
    return dates


def _read_bars() -> list[Bar]:
    end_dt = datetime.combine(WINDOW_END + timedelta(days=1), time.min, tzinfo=UTC)
    bars: list[Bar] = []
    for row in _read_jsonl(EVENT_BARS_JSONL):
        start_at = datetime.fromisoformat(str(row["start_at"])).astimezone(UTC)
        end_at = datetime.fromisoformat(str(row["end_at"])).astimezone(UTC)
        if end_at <= datetime.combine(WINDOW_START, time.min, tzinfo=UTC):
            continue
        if start_at >= end_dt:
            continue
        bars.append(
            Bar(
                start_at=start_at,
                end_at=end_at,
                open_price=float(row["open_price"]),
                close_price=float(row["close_price"]),
            )
        )
    bars.sort(key=lambda item: item.start_at)
    return bars


def _state_to_weight(state: str) -> float:
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
        raise ValueError(f"Unsupported analysis state: {state}") from exc


def _deduplicate_changes(changes_by_time: dict[datetime, Change]) -> list[Change]:
    return [changes_by_time[key] for key in sorted(changes_by_time)]


def _read_analysis_changes() -> list[Change]:
    changes: dict[datetime, Change] = {}
    for path in sorted(ANALYSIS_DIR.glob("*.jsonl")):
        for row in _read_jsonl(path):
            if row.get("target_key") != "gold":
                continue
            effective_at = datetime.fromisoformat(str(row["business_at"])).astimezone(UTC)
            changes[effective_at] = Change(
                effective_at=effective_at,
                target_weight=_state_to_weight(str(row["as_if_flat_state"])),
            )
    return _deduplicate_changes(changes)


def _read_pm_changes() -> list[Change]:
    changes: dict[datetime, Change] = {}
    for path in sorted(PM_STATE_CHANGES_DIR.glob("*.jsonl")):
        for row in _read_jsonl(path):
            if row.get("target_key") != "gold":
                continue
            effective_at = datetime.fromisoformat(str(row["effective_at"])).astimezone(UTC)
            changes[effective_at] = Change(
                effective_at=effective_at,
                target_weight=float(row["target_weight"]),
            )
    return _deduplicate_changes(changes)


def _simulate(changes: list[Change], bars: list[Bar], anchor_dates: list[date]) -> tuple[list[Point], float, int]:
    current_weight = 0.0
    equity = INITIAL_EQUITY
    total_fees = 0.0
    action_count = 0
    change_index = 0
    daily_equity: dict[date, float] = {}

    for bar in bars:
        while change_index < len(changes) and changes[change_index].effective_at <= bar.start_at:
            next_weight = changes[change_index].target_weight
            turnover = abs(next_weight - current_weight)
            if turnover > ACTION_TURNOVER_EPSILON:
                fee = equity * turnover * COST_RATE
                equity -= fee
                total_fees += fee
                action_count += 2 if current_weight * next_weight < 0.0 else 1
            current_weight = next_weight
            change_index += 1
        equity *= 1.0 + current_weight * (bar.close_price / bar.open_price - 1.0)
        daily_equity[bar.start_at.date()] = equity

    carry = INITIAL_EQUITY
    series: list[Point] = []
    for trade_day in anchor_dates:
        if trade_day in daily_equity:
            carry = daily_equity[trade_day]
        series.append(Point(trade_day, carry))
    return series, total_fees, action_count


def _summary(series: list[Point], fees: float, actions: int) -> dict[str, object]:
    peak = INITIAL_EQUITY
    max_drawdown = 0.0
    max_drawdown_date: date | None = None
    for point in series:
        peak = max(peak, point.equity_close)
        drawdown = 1.0 - point.equity_close / peak
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_date = point.trade_date
    final_equity = series[-1].equity_close
    return {
        "final_equity": final_equity,
        "total_return": final_equity / INITIAL_EQUITY - 1.0,
        "max_drawdown": max_drawdown,
        "max_drawdown_date": max_drawdown_date,
        "fees": fees,
        "actions": actions,
    }


def _write_csv(path: Path, curves: dict[str, list[Point]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["strategy", "trade_date", "equity_close"])
        writer.writeheader()
        for strategy, series in curves.items():
            for point in series:
                writer.writerow(
                    {
                        "strategy": strategy,
                        "trade_date": point.trade_date.isoformat(),
                        "equity_close": f"{point.equity_close:.6f}",
                    }
                )


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _write_svg(path: Path, curves: dict[str, list[Point]]) -> None:
    width, height = 1600, 900
    left, right, top, bottom = 116, 48, 150, 102
    plot_width, plot_height = width - left - right, 610
    reference = next(iter(curves.values()))
    total_steps = len(reference)
    values = [point.equity_close / INITIAL_EQUITY * 100.0 - 100.0 for series in curves.values() for point in series]
    ymin, ymax = min(values), max(values)
    pad = max((ymax - ymin) * 0.1, 0.8)
    ymin, ymax = min(ymin - pad, -1.0), max(ymax + pad, 1.0)
    colors = {"analysis_only": "#CC6677", "pm": "#0072B2"}
    labels = {"analysis_only": "Analysis-only", "pm": "PM"}

    def sx(index: int) -> float:
        return left + plot_width * index / max(total_steps - 1, 1)

    def sy(value: float) -> float:
        return top + plot_height * (1.0 - (value - ymin) / (ymax - ymin))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="#ffffff" />',
        f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" fill="#ffffff" stroke="#9ca3af" stroke-width="1.2" />',
    ]
    for tick in range(6):
        value = ymin + (ymax - ymin) * tick / 5.0
        y = sy(value)
        parts.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_width}" y2="{y:.2f}" stroke="#e5e7eb" />')
        parts.append(f'<text x="{left - 14}" y="{y + 5:.2f}" text-anchor="end" font-family="Arial, sans-serif" font-size="18" fill="#374151">{value:.1f}%</text>')
    for tick in range(6):
        index = round((total_steps - 1) * tick / 5.0)
        x = sx(index)
        parts.append(f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{top + plot_height}" stroke="#f3f4f6" />')
        parts.append(f'<text x="{x:.2f}" y="{top + plot_height + 30}" text-anchor="middle" font-family="Arial, sans-serif" font-size="17" fill="#374151">{reference[index].trade_date.isoformat()}</text>')
    zero_y = sy(0.0)
    parts.append(f'<line x1="{left}" y1="{zero_y:.2f}" x2="{left + plot_width}" y2="{zero_y:.2f}" stroke="#6b7280" stroke-dasharray="5 5" />')
    for strategy, series in curves.items():
        points = " ".join(f"{sx(index):.2f},{sy(point.equity_close / INITIAL_EQUITY * 100.0 - 100.0):.2f}" for index, point in enumerate(series))
        parts.append(f'<polyline fill="none" stroke="{colors[strategy]}" stroke-width="4.0" stroke-linecap="round" stroke-linejoin="round" points="{points}" />')
    for index, strategy in enumerate(curves):
        x = left + 300 * index
        parts.append(f'<line x1="{x}" y1="100" x2="{x + 34}" y2="100" stroke="{colors[strategy]}" stroke-width="4" />')
        parts.append(f'<text x="{x + 48}" y="106" font-family="Arial, sans-serif" font-size="20" fill="#111827">{labels[strategy]}</text>')
    parts.extend(
        [
            f'<text x="{left}" y="48" font-family="Arial, sans-serif" font-size="30" font-weight="700" fill="#111827">{_xml_escape("GCUSD analysis-only vs PM exposure")}</text>',
            f'<text x="{left}" y="78" font-family="Arial, sans-serif" font-size="18" fill="#4b5563">2026-01-01 to 2026-06-30 | initial equity $100,000 | 0.5 bps per side</text>',
            f'<text transform="translate(32 {top + plot_height / 2:.2f}) rotate(-90)" font-family="Arial, sans-serif" font-size="19" fill="#374151">Cumulative return (%)</text>',
            f'<text x="{left + plot_width / 2:.2f}" y="{height - 30}" text-anchor="middle" font-family="Arial, sans-serif" font-size="20" fill="#374151">Trading day</text>',
            "</svg>",
        ]
    )
    path.write_text("".join(parts), encoding="utf-8")


def _write_md(path: Path, output_svg: Path, output_csv: Path, summaries: dict[str, dict[str, object]], analysis_count: int, pm_count: int) -> None:
    lines = [
        "# GCUSD Analysis-only vs PM Exposure",
        "",
        "- window: `2026-01-01` through `2026-06-30`",
        "- source workspace: `code\\.local\\server-gold-20260101-20260630\\replay-gold-20260101_20260630-workspace`",
        "- initial equity: `$100,000`",
        "- transaction cost: `0.5` bps per side on executed notional",
        "- Analysis-only mapping: `strong_long=1.0`, `weak_long=0.5`, `flat=0.0`, `weak_short=-0.5`, `strong_short=-1.0` from `as_if_flat_state`.",
        "- PM mapping: executed `state-changes` `target_weight`; repeated PM reviews are not treated as separate trades.",
        "- same 5-minute GCUSD bars and daily anchor calendar are used for both curves.",
        "",
        "| Strategy | Assessments / Changes | Final Equity | Return | MDD | MDD Date | Fees | Actions |",
        "| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: |",
    ]
    for strategy, count in (("analysis_only", analysis_count), ("pm", pm_count)):
        summary = summaries[strategy]
        lines.append(
            f"| {strategy} | {count} | ${summary['final_equity']:,.2f} | {summary['total_return'] * 100:.2f}% | {summary['max_drawdown'] * 100:.2f}% | {summary['max_drawdown_date'].isoformat() if summary['max_drawdown_date'] else 'n/a'} | ${summary['fees']:,.2f} | {summary['actions']} |"
        )
    lines.extend(["", f"- figure: `{output_svg.name}`", f"- data: `{output_csv.name}`"])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = _parse_args()
    output_svg = EVAL_DIR / f"{args.output_stem}.svg"
    output_csv = EVAL_DIR / f"{args.output_stem}.csv"
    output_md = EVAL_DIR / f"{args.output_stem}.md"
    bars = _read_bars()
    anchors = _read_daily_dates()
    analysis_changes = _read_analysis_changes()
    pm_changes = _read_pm_changes()
    analysis_series, analysis_fees, analysis_actions = _simulate(analysis_changes, bars, anchors)
    pm_series, pm_fees, pm_actions = _simulate(pm_changes, bars, anchors)
    curves = {"analysis_only": analysis_series, "pm": pm_series}
    summaries = {
        "analysis_only": _summary(analysis_series, analysis_fees, analysis_actions),
        "pm": _summary(pm_series, pm_fees, pm_actions),
    }
    _write_csv(output_csv, curves)
    _write_svg(output_svg, curves)
    _write_md(output_md, output_svg, output_csv, summaries, len(analysis_changes), len(pm_changes))
    print(json.dumps({"svg": str(output_svg), "csv": str(output_csv), "md": str(output_md), "summaries": summaries}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
