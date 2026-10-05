from __future__ import annotations

import csv
import json
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from statistics import median

from make_figure import Circle, Drawing, Line, Rect, RLPath, String


ROOT = Path(__file__).resolve().parents[2] / "code"
OUT_DIR = Path(__file__).resolve().parent
IMAGE_DIR = Path(__file__).resolve().parents[2] / "paper" / "images"
WORKSPACE = (
    ROOT
    / ".local/server-gold-20260101-20260630/"
    "replay-gold-20260101_20260630-workspace"
)
BASELINE_ROOT = Path(__file__).resolve().parents[2] / "baselines" / "experiments"
EVAL_DIR = BASELINE_ROOT / "gold_baseline_eval_20260101_20260630"
AHF_DIR = BASELINE_ROOT / "ai_hedge_fund_gcusd_full_20260101_20260630"
FINMEM_DIR = (
    BASELINE_ROOT
    / "finmem_gcusd_paper_compat_20260101_20260630_m2_1_qinzhi_v1"
)

PRICE_PATH = (
    WORKSPACE
    / "runtime/market_data/gold/gold-gcusd-20260101-20260630/"
    "bars/GCUSD_5min.jsonl"
)
ASSESSMENT_DIR = WORKSPACE / "runtime/analysis_assessments/gold"
LEDGER_DIR = WORKSPACE / "ledger/gold"


LONG = "#D55E00"
SHORT = "#0072B2"
FLAT = "#6B7280"
EVENT = "#8B5CF6"
OURS_BG = "#EAF6F3"
OURS_LINE = "#1B7F79"
DARK = "#111827"
TEXT = "#374151"
GRID = "#D1D5DB"
LIGHT_GRID = "#E5E7EB"
PRE_EVENT = "#9CA3AF"
CLOSED_BG = "#F3F4F6"
WHITE = "#FFFFFF"


@dataclass(frozen=True)
class Case:
    panel: str
    title_lines: tuple[str, str]
    source_name: str
    event_at: datetime
    source_event_id: str
    assessment_id: str
    expected_state: str
    expected_baselines: dict[str, str]


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


CASES = [
    Case(
        panel="(a)",
        title_lines=(
            "January PPI: services rise while",
            "energy prices fall",
        ),
        source_name="BLS",
        event_at=dt("2026-02-27T13:30:00Z"),
        source_event_id="b59eea757e266d358ab25325",
        assessment_id="a7f2e3c1d9b8e4f5a6c7d8e9f0a1b2c3d4e5f6a7",
        expected_state="strong_long",
        expected_baselines={
            "TradingAgents": "flat",
            "FinMem": "long",
            "AI Hedge Fund": "long",
        },
    ),
    Case(
        panel="(b)",
        title_lines=(
            "Inflation rises as the Hormuz",
            "blockade threat returns",
        ),
        source_name="Yahoo Finance",
        event_at=dt("2026-04-13T11:10:30Z"),
        source_event_id="a9418997760e49cfb1431f6c",
        assessment_id="gold-2026-04-13-114030-unit-03a00315a1991d6cf7fdc40d",
        expected_state="weak_long",
        expected_baselines={
            "TradingAgents": "flat",
            "FinMem": "flat",
            "AI Hedge Fund": "short",
        },
    ),
    Case(
        panel="(c)",
        title_lines=(
            "U.S.-Iran talks continue after",
            "renewed U.S. strikes",
        ),
        source_name="Yahoo Finance",
        event_at=dt("2026-05-27T11:41:04Z"),
        source_event_id="6fec1a7310f3ac936b3d3a1e",
        assessment_id="gold-2026-05-27T12:11:04-6fec1a7310f3ac936b3d3a1e",
        expected_state="strong_short",
        expected_baselines={
            "TradingAgents": "long",
            "FinMem": "flat",
            "AI Hedge Fund": "long",
        },
    ),
    Case(
        panel="(d)",
        title_lines=(
            "U.S. and Iran agree to halt attacks",
            "and meet in Qatar",
        ),
        source_name="Fortune",
        event_at=dt("2026-06-29T00:28:46Z"),
        source_event_id="288633ddd84352f63380f9d2",
        assessment_id="analysis-assessment:gold:db06a1fb741804e97c94500c",
        expected_state="weak_short",
        expected_baselines={
            "TradingAgents": "flat",
            "FinMem": "flat",
            "AI Hedge Fund": "flat",
        },
    ),
]


def load_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def exposure(state: str) -> float:
    return {
        "strong_long": 1.0,
        "weak_long": 0.5,
        "flat": 0.0,
        "weak_short": -0.5,
        "strong_short": -1.0,
    }[state]


def direction(state: str) -> str:
    if "long" in state:
        return "long"
    if "short" in state:
        return "short"
    return "flat"


def state_label(state: str) -> str:
    return {
        "strong_long": "Strong Long",
        "weak_long": "Weak Long",
        "long": "Long",
        "flat": "No new direction",
        "weak_short": "Weak Short",
        "strong_short": "Strong Short",
        "short": "Short",
    }[state]


def state_color(state: str) -> str:
    return {"long": LONG, "short": SHORT, "flat": FLAT}[direction(state)]


def first_bar_after(bars: list[dict], when: datetime) -> dict:
    candidates = [row for row in bars if dt(row["start_at"]) >= when]
    assert candidates, when
    return min(candidates, key=lambda row: row["start_at"])


def last_bar_on_day(bars: list[dict], day) -> dict:
    candidates = [row for row in bars if dt(row["end_at"]).date() == day]
    assert candidates, day
    return max(candidates, key=lambda row: row["end_at"])


def price_at_or_after(bars: list[dict], when: datetime) -> tuple[datetime, float]:
    row = first_bar_after(bars, when)
    return dt(row["start_at"]), float(row["open_price"])


def net_pnl_pct(state: str, entry_price: float, exit_price: float) -> float:
    target = exposure(state)
    gross = target * (exit_price / entry_price - 1.0)
    entry_fee = abs(target) * 0.00005
    exit_fee = abs(target * exit_price / entry_price) * 0.00005
    return (gross - entry_fee - exit_fee) * 100.0


def ta_state(row: dict[str, str]) -> str:
    rating = row["pm_rating"]
    if rating in {"Buy", "Overweight"}:
        return "long"
    if rating in {"Sell", "Underweight"}:
        return "short"
    return "flat"


def finmem_state(audit: dict) -> str:
    action = audit["decision"].get("native_action")
    if action == "buy":
        return "long"
    if action == "sell":
        return "short"
    return "flat"


def ahf_state(audit: dict) -> str:
    action = audit["native_action"]
    if action == "buy":
        return "long"
    if action in {"sell", "short"}:
        return "short"
    return "flat"


def find_case_data(case: Case, bars: list[dict], ta_rows: list[dict]) -> dict:
    ledger_rows = load_jsonl(LEDGER_DIR / f"{case.event_at:%Y-%m-%d}.jsonl")
    source_matches = [
        row for row in ledger_rows if row["event_id"] == case.source_event_id
    ]
    assert len(source_matches) == 1
    source = source_matches[0]
    assert dt(source["ts_init"]) == case.event_at

    assessment_rows = load_jsonl(
        ASSESSMENT_DIR / f"{case.event_at:%Y-%m}.jsonl"
    )
    assessment_matches = [
        row for row in assessment_rows if row["assessment_id"] == case.assessment_id
    ]
    assert len(assessment_matches) == 1
    assessment = assessment_matches[0]
    assert assessment["as_if_flat_state"] == case.expected_state
    assert case.source_event_id in assessment["source_event_ids"]

    analysis_at = dt(assessment["business_at"])
    entry_at, entry_price = price_at_or_after(bars, analysis_at)
    cutoff_bar = last_bar_on_day(bars, case.event_at.date())
    cutoff_at = dt(cutoff_bar["end_at"])
    cutoff_price = float(cutoff_bar["close_price"])
    pnl = net_pnl_pct(case.expected_state, entry_price, cutoff_price)
    assert pnl > 0.0

    date_value = f"{case.event_at:%Y-%m-%d}"
    ta_matches = [
        row
        for row in ta_rows
        if row["baseline"] == "tradingagents"
        and row["trade_date"] == date_value
    ]
    assert len(ta_matches) == 1
    finmem = json.loads(
        (FINMEM_DIR / "days" / date_value / "audit.json").read_text(
            encoding="utf-8"
        )
    )
    ahf = json.loads(
        (AHF_DIR / date_value / "audit.json").read_text(encoding="utf-8")
    )
    baselines = {
        "TradingAgents": ta_state(ta_matches[0]),
        "FinMem": finmem_state(finmem),
        "AI Hedge Fund": ahf_state(ahf),
    }
    assert baselines == case.expected_baselines, (case.panel, baselines)

    eod = datetime.combine(case.event_at.date(), time(23, 59, 59), timezone.utc)
    source_price_bar = first_bar_after(bars, case.event_at)
    source_price = float(source_price_bar["open_price"])
    price_rows = sorted(
        (
            row
            for row in bars
            if dt(row["start_at"]).date() == case.event_at.date()
            and dt(row["end_at"]) <= cutoff_at
        ),
        key=lambda row: dt(row["start_at"]),
    )
    assert price_rows, case.panel
    first_price_row = price_rows[0]
    points = [
        (
            dt(first_price_row["start_at"]),
            (float(first_price_row["open_price"]) / source_price - 1.0) * 100.0,
        )
    ]
    for row in price_rows:
        points.append(
            (
                dt(row["end_at"]),
                (float(row["close_price"]) / source_price - 1.0) * 100.0,
            )
        )

    analysis_return = (entry_price / source_price - 1.0) * 100.0
    return {
        "source": source,
        "assessment": assessment,
        "analysis_at": analysis_at,
        "entry_at": entry_at,
        "entry_price": entry_price,
        "cutoff_at": cutoff_at,
        "cutoff_price": cutoff_price,
        "pnl_pct": pnl,
        "price_return_pct": (cutoff_price / entry_price - 1.0) * 100.0,
        "analysis_return": analysis_return,
        "points": points,
        "baselines": baselines,
        "eod": eod,
    }


def x_for_time(value: datetime, x_left: float, x_right: float) -> float:
    hour = (
        value.hour
        + value.minute / 60.0
        + value.second / 3600.0
        + value.microsecond / 3_600_000_000.0
    )
    return x_left + hour / 24.0 * (x_right - x_left)


def y_for_value(
    value: float, low: float, high: float, y_bottom: float, y_top: float
) -> float:
    return y_bottom + (value - low) / (high - low) * (y_top - y_bottom)


def rounded_limits(values: list[float]) -> tuple[float, float]:
    low = min(values + [0.0])
    high = max(values + [0.0])
    span = max(high - low, 0.25)
    low -= span * 0.14
    high += span * 0.14
    if low > -0.08:
        low = -0.08
    if high < 0.08:
        high = 0.08
    return low, high


def median_plot_points(
    points: list[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """Use 30-minute medians for the displayed curve only.

    The execution and PnL calculations continue to use the native five-minute
    bars. The median curve prevents isolated basis-seam bars from dominating
    the visual path while preserving the intraday direction and endpoints.
    """
    if len(points) <= 2:
        return points
    grouped: dict[datetime, list[tuple[datetime, float]]] = {}
    for when, value in points[1:]:
        bucket = when.replace(
            minute=(when.minute // 30) * 30, second=0, microsecond=0
        )
        grouped.setdefault(bucket, []).append((when, value))
    result = [points[0]]
    for bucket in sorted(grouped):
        rows = grouped[bucket]
        result.append((max(when for when, _ in rows), median(v for _, v in rows)))
    if result[-1][0] != points[-1][0]:
        result.append(points[-1])
    return result


def split_points_at(
    points: list[tuple[datetime, float]], split_at: datetime
) -> tuple[list[tuple[datetime, float]], list[tuple[datetime, float]]]:
    """Split a continuous path at a timestamp without creating a visual gap."""
    before = [(when, value) for when, value in points if when <= split_at]
    after = [(when, value) for when, value in points if when >= split_at]
    if any(when == split_at for when, _ in points):
        return before, after

    left = max(
        ((when, value) for when, value in points if when < split_at),
        default=None,
        key=lambda item: item[0],
    )
    right = min(
        ((when, value) for when, value in points if when > split_at),
        default=None,
        key=lambda item: item[0],
    )
    if left is None or right is None:
        return before, after

    elapsed = (split_at - left[0]).total_seconds()
    interval = (right[0] - left[0]).total_seconds()
    split_value = left[1] + (right[1] - left[1]) * elapsed / interval
    split_point = (split_at, split_value)
    return before + [split_point], [split_point] + after


def add_direction_marker(
    drawing: Drawing,
    x: float,
    y: float,
    state: str,
    *,
    filled: bool,
    size: float = 3.2,
) -> None:
    color = state_color(state)
    fill = color if filled else WHITE
    shape = direction(state)
    if shape == "flat":
        drawing.add(
            Circle(
                x,
                y,
                size,
                fillColor=fill,
                strokeColor=color,
                strokeWidth=1.0,
            )
        )
        return
    path = RLPath()
    if shape == "long":
        path.moveTo(x, y + size + 0.6)
        path.lineTo(x + size + 0.5, y - size)
        path.lineTo(x - size - 0.5, y - size)
    else:
        path.moveTo(x, y - size - 0.6)
        path.lineTo(x + size + 0.5, y + size)
        path.lineTo(x - size - 0.5, y + size)
    path.close()
    path.fillColor = fill
    path.strokeColor = color
    path.strokeWidth = 1.0
    drawing.add(path)


def add_price_path(
    drawing: Drawing,
    points: list[tuple[datetime, float]],
    x_left: float,
    x_right: float,
    y_bottom: float,
    y_top: float,
    low: float,
    high: float,
    *,
    color: str,
    width: float,
) -> None:
    if len(points) < 2:
        return
    path = RLPath()
    for index, (when, value) in enumerate(points):
        x = x_for_time(when, x_left, x_right)
        y = y_for_value(value, low, high, y_bottom, y_top)
        if index == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.strokeColor = color
    path.strokeWidth = width
    drawing.add(path)


def add_panel(
    drawing: Drawing,
    case: Case,
    data: dict,
    x0: float,
    y0: float,
    width: float,
    height: float,
    *,
    show_x_label: bool,
) -> None:
    x_left = x0 + 51.0
    x_right = x0 + width - 8.0
    price_bottom = y0 + 91.0
    price_top = y0 + height - 45.0
    row_y = {
        "EventTrader": y0 + 70.0,
        "TradingAgents": y0 + 53.0,
        "FinMem": y0 + 36.0,
        "AI Hedge Fund": y0 + 19.0,
    }

    drawing.add(
        String(
            x0 + 1,
            y0 + height - 9,
            case.panel,
            fontName="Times-Bold",
            fontSize=8.2,
            fillColor=DARK,
        )
    )
    drawing.add(
        String(
            x0 + 19,
            y0 + height - 9,
            case.title_lines[0],
            fontName="Times-Bold",
            fontSize=7.7,
            fillColor=DARK,
        )
    )
    drawing.add(
        String(
            x0 + 19,
            y0 + height - 19,
            case.title_lines[1],
            fontName="Times-Bold",
            fontSize=7.7,
            fillColor=DARK,
        )
    )
    drawing.add(
        String(
            x0 + 19,
            y0 + height - 31,
            f"{case.source_name}, {case.event_at:%b %d, %H:%M UTC}",
            fontName="Times-Roman",
            fontSize=6.4,
            fillColor=TEXT,
        )
    )
    drawing.add(
        String(
            x0 + width - 8,
            y0 + height - 31,
            f"EventTrader return {data['pnl_pct']:+.2f}%",
            fontName="Times-Bold",
            fontSize=6.4,
            fillColor=state_color(case.expected_state),
            textAnchor="end",
        )
    )

    plot_points = median_plot_points(data["points"])
    values = [value for _, value in plot_points]
    low, high = rounded_limits(values)
    zero_y = y_for_value(0.0, low, high, price_bottom, price_top)
    cutoff_x = x_for_time(data["cutoff_at"], x_left, x_right)

    if data["cutoff_at"].hour < 23:
        drawing.add(
            Rect(
                cutoff_x,
                price_bottom,
                x_right - cutoff_x,
                price_top - price_bottom,
                fillColor=CLOSED_BG,
                strokeColor=None,
                strokeWidth=0,
            )
        )

    for hour in (0, 6, 12, 18, 24):
        x = x_left + hour / 24.0 * (x_right - x_left)
        drawing.add(
            Line(
                x,
                y0 + 12,
                x,
                price_top,
                strokeColor=LIGHT_GRID,
                strokeWidth=0.55,
            )
        )
        drawing.add(
            String(
                x,
                y0 + 5,
                str(hour),
                fontName="Times-Roman",
                fontSize=5.8,
                fillColor=TEXT,
                textAnchor="middle",
            )
        )

    drawing.add(
        Line(
            x_left,
            zero_y,
            x_right,
            zero_y,
            strokeColor=GRID,
            strokeWidth=0.75,
        )
    )
    drawing.add(
        Line(
            x_left,
            price_bottom,
            x_left,
            price_top,
            strokeColor=TEXT,
            strokeWidth=0.7,
        )
    )
    for fraction in (0.0, 0.5, 1.0):
        value = low + fraction * (high - low)
        y = y_for_value(value, low, high, price_bottom, price_top)
        drawing.add(
            Line(
                x_left - 2,
                y,
                x_left,
                y,
                strokeColor=TEXT,
                strokeWidth=0.6,
            )
        )
        drawing.add(
            String(
                x_left - 4,
                y - 2,
                f"{value:+.1f}",
                fontName="Times-Roman",
                fontSize=5.6,
                fillColor=TEXT,
                textAnchor="end",
            )
        )
    drawing.add(
        String(
            x0 + 7,
            (price_bottom + price_top) / 2,
            "GCUSD return (30-min median, %)",
            fontName="Times-Roman",
            fontSize=6.0,
            fillColor=TEXT,
            textAnchor="middle",
            angle=90,
        )
    )

    event_x = x_for_time(case.event_at, x_left, x_right)
    analysis_x = x_for_time(data["analysis_at"], x_left, x_right)
    eod_x = x_for_time(data["eod"], x_left, x_right)
    pre_event_points, post_event_points = split_points_at(
        plot_points, case.event_at
    )
    add_price_path(
        drawing,
        pre_event_points,
        x_left,
        x_right,
        price_bottom,
        price_top,
        low,
        high,
        color=PRE_EVENT,
        width=0.95,
    )
    add_price_path(
        drawing,
        post_event_points,
        x_left,
        x_right,
        price_bottom,
        price_top,
        low,
        high,
        color=DARK,
        width=1.15,
    )
    drawing.add(
        Line(
            event_x,
            y0 + 12,
            event_x,
            price_top,
            strokeColor=EVENT,
            strokeWidth=0.9,
            strokeDashArray=[2.2, 2.0],
        )
    )
    drawing.add(
        String(
            min(event_x + 2, x_right - 35),
            price_top + 3,
            "Evidence visible",
            fontName="Times-Roman",
            fontSize=5.7,
            fillColor=EVENT,
        )
    )
    analysis_y = y_for_value(
        data["analysis_return"], low, high, price_bottom, price_top
    )
    add_direction_marker(
        drawing,
        analysis_x,
        analysis_y,
        case.expected_state,
        filled=True,
        size=3.4,
    )

    drawing.add(
        Rect(
            x0,
            row_y["EventTrader"] - 6,
            width,
            12,
            fillColor=OURS_BG,
            strokeColor=None,
            strokeWidth=0,
        )
    )
    for method, y in row_y.items():
        drawing.add(
            Line(
                x_left,
                y - 7.5,
                x_right,
                y - 7.5,
                strokeColor=LIGHT_GRID,
                strokeWidth=0.55,
            )
        )
        drawing.add(
            String(
                x_left - 4,
                y - 2,
                method,
                fontName="Times-Bold" if method == "EventTrader" else "Times-Roman",
                fontSize=5.7,
                fillColor=OURS_LINE if method == "EventTrader" else TEXT,
                textAnchor="end",
            )
        )

    drawing.add(
        Rect(
            analysis_x,
            row_y["EventTrader"] - 4.2,
            max(eod_x - analysis_x, 0.8),
            8.4,
            fillColor="#D8EEE9",
            strokeColor=state_color(case.expected_state),
            strokeWidth=0.65,
        )
    )
    add_direction_marker(
        drawing,
        analysis_x,
        row_y["EventTrader"],
        case.expected_state,
        filled=True,
        size=2.9,
    )
    band_width = eod_x - analysis_x
    if band_width < 55:
        state_x = min(eod_x + 4, x_right - 2)
        state_anchor = "start" if cutoff_x + 42 < x_right else "end"
        if state_anchor == "end":
            state_x = x_right - 2
    else:
        state_x = analysis_x + band_width * 0.56
        state_anchor = "middle"
    drawing.add(
        String(
            state_x,
            row_y["EventTrader"] - 2,
            state_label(case.expected_state),
            fontName="Times-Bold",
            fontSize=5.8,
            fillColor=state_color(case.expected_state),
            textAnchor=state_anchor,
        )
    )

    for method in ("TradingAgents", "FinMem", "AI Hedge Fund"):
        state = data["baselines"][method]
        y = row_y[method]
        label = state_label(state)
        drawing.add(
            String(
                eod_x - 5,
                y - 2,
                label,
                fontName="Times-Roman",
                fontSize=5.3,
                fillColor=state_color(state),
                textAnchor="end",
            )
        )
        add_direction_marker(
            drawing, eod_x, y, state, filled=False, size=2.7
        )

    if data["cutoff_at"].hour < 23:
        drawing.add(
            Line(
                cutoff_x,
                price_bottom,
                cutoff_x,
                price_top,
                strokeColor=FLAT,
                strokeWidth=0.7,
            )
        )
        drawing.add(
            String(
                (cutoff_x + x_right) / 2,
                price_top - 9,
                "GCUSD session closed",
                fontName="Times-Roman",
                fontSize=5.4,
                fillColor=FLAT,
                textAnchor="middle",
            )
        )
        drawing.add(
            String(
                cutoff_x - 2,
                price_bottom + 3,
                f"Friday close, {data['cutoff_at']:%H:%M} UTC",
                fontName="Times-Roman",
                fontSize=5.2,
                fillColor=FLAT,
                textAnchor="end",
            )
        )

    if show_x_label:
        drawing.add(
            String(
                (x_left + x_right) / 2,
                y0 - 4,
                "UTC hour",
                fontName="Times-Roman",
                fontSize=6.2,
                fillColor=TEXT,
                textAnchor="middle",
            )
        )


def add_legend(drawing: Drawing) -> None:
    y = 20.0
    x = 18.0
    drawing.add(Line(x, y, x + 15, y, strokeColor=PRE_EVENT, strokeWidth=0.95))
    drawing.add(
        String(
            x + 20,
            y - 2,
            "Price before evidence",
            fontName="Times-Roman",
            fontSize=6.2,
            fillColor=TEXT,
        )
    )
    x += 116
    drawing.add(Line(x, y, x + 15, y, strokeColor=DARK, strokeWidth=1.15))
    drawing.add(
        String(
            x + 20,
            y - 2,
            "Price after evidence",
            fontName="Times-Roman",
            fontSize=6.2,
            fillColor=TEXT,
        )
    )
    x += 112
    drawing.add(
        Line(
            x,
            y,
            x + 15,
            y,
            strokeColor=EVENT,
            strokeWidth=0.9,
            strokeDashArray=[2.2, 2.0],
        )
    )
    drawing.add(
        String(
            x + 20,
            y - 2,
            "Evidence visible",
            fontName="Times-Roman",
            fontSize=6.2,
            fillColor=TEXT,
        )
    )
    y = 8.0
    x = 18.0
    add_direction_marker(drawing, x, y, "long", filled=True, size=2.8)
    drawing.add(
        String(
            x + 7,
            y - 2,
            "EventTrader analysis",
            fontName="Times-Roman",
            fontSize=6.2,
            fillColor=TEXT,
        )
    )
    x += 112
    add_direction_marker(drawing, x, y, "long", filled=False, size=2.8)
    drawing.add(
        String(
            x + 7,
            y - 2,
            "Daily agent at EOD",
            fontName="Times-Roman",
            fontSize=6.2,
            fillColor=TEXT,
        )
    )
    x += 108
    add_direction_marker(drawing, x, y, "long", filled=True, size=2.6)
    drawing.add(
        String(
            x + 7,
            y - 2,
            "Long",
            fontName="Times-Roman",
            fontSize=6.2,
            fillColor=TEXT,
        )
    )
    x += 48
    add_direction_marker(drawing, x, y, "short", filled=True, size=2.6)
    drawing.add(
        String(
            x + 7,
            y - 2,
            "Short",
            fontName="Times-Roman",
            fontSize=6.2,
            fillColor=TEXT,
        )
    )
    x += 52
    add_direction_marker(drawing, x, y, "flat", filled=True, size=2.5)
    drawing.add(
        String(
            x + 7,
            y - 2,
            "No new direction",
            fontName="Times-Roman",
            fontSize=6.2,
            fillColor=TEXT,
        )
    )


def write_audit(rows: list[dict]) -> None:
    path = OUT_DIR / "prebaseline_case_audit.csv"
    fields = [
        "panel",
        "event_title",
        "source",
        "source_url",
        "event_visible_at_utc",
        "eventtrader_analysis_at_utc",
        "analysis_delay_minutes",
        "eventtrader_state",
        "entry_at_utc",
        "entry_price",
        "cutoff_at_utc",
        "cutoff_price",
        "price_return_pct",
        "eventtrader_return_pct",
        "tradingagents_state",
        "finmem_state",
        "ai_hedge_fund_state",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def export_with_browser(html_path: Path, width: int, height: int) -> None:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    browser = next((path for path in candidates if path.exists()), None)
    if browser is None:
        raise RuntimeError("Chrome or Edge is required for PDF and PNG export")
    url = html_path.resolve().as_uri()
    pdf_path = (IMAGE_DIR / "agent_prebaseline_cases_preview.pdf").resolve()
    png_path = (IMAGE_DIR / "agent_prebaseline_cases_preview.png").resolve()
    with tempfile.TemporaryDirectory(prefix="prebaseline-pdf-") as profile:
        subprocess.run(
            [
                str(browser),
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                f"--user-data-dir={profile}",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                url,
            ],
            check=True,
        )
    with tempfile.TemporaryDirectory(prefix="prebaseline-png-") as profile:
        subprocess.run(
            [
                str(browser),
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--default-background-color=ffffffff",
                "--force-color-profile=srgb",
                f"--user-data-dir={profile}",
                "--force-device-scale-factor=2",
                f"--window-size={width},{height}",
                f"--screenshot={png_path}",
                url,
            ],
            check=True,
        )


def main() -> None:
    bars = load_jsonl(PRICE_PATH)
    ta_rows = load_csv(EVAL_DIR / "tradingagents_decision_diagnostics.csv")

    width, height = 540.0, 492.0
    drawing = Drawing(width, height)
    panel_width, panel_height = 262.0, 220.0
    origins = [(5, 264), (273, 264), (5, 39), (273, 39)]
    audit_rows = []

    for index, (case, (x0, y0)) in enumerate(zip(CASES, origins)):
        data = find_case_data(case, bars, ta_rows)
        add_panel(
            drawing,
            case,
            data,
            x0,
            y0,
            panel_width,
            panel_height,
            show_x_label=index >= 2,
        )
        audit_rows.append(
            {
                "panel": case.panel,
                "event_title": " ".join(case.title_lines),
                "source": case.source_name,
                "source_url": data["source"]["source_ref"],
                "event_visible_at_utc": case.event_at.isoformat(),
                "eventtrader_analysis_at_utc": data["analysis_at"].isoformat(),
                "analysis_delay_minutes": f"{(data['analysis_at'] - case.event_at).total_seconds() / 60.0:.3f}",
                "eventtrader_state": case.expected_state,
                "entry_at_utc": data["entry_at"].isoformat(),
                "entry_price": f"{data['entry_price']:.6f}",
                "cutoff_at_utc": data["cutoff_at"].isoformat(),
                "cutoff_price": f"{data['cutoff_price']:.6f}",
                "price_return_pct": f"{data['price_return_pct']:.6f}",
                "eventtrader_return_pct": f"{data['pnl_pct']:.6f}",
                "tradingagents_state": data["baselines"]["TradingAgents"],
                "finmem_state": data["baselines"]["FinMem"],
                "ai_hedge_fund_state": data["baselines"]["AI Hedge Fund"],
            }
        )

    add_legend(drawing)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    write_audit(audit_rows)
    svg_path = IMAGE_DIR / "agent_prebaseline_cases_preview.svg"
    drawing.write_svg(svg_path)
    html_path = OUT_DIR / "agent_prebaseline_cases_preview.html"
    html_path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>@page{{size:{width:.0f}pt {height:.0f}pt;margin:0}}"
        "html,body{margin:0;padding:0;overflow:hidden;background:white}"
        f"svg{{display:block;width:{width:.0f}pt;height:{height:.0f}pt}}"
        "</style></head><body>"
        + svg_path.read_text(encoding="utf-8")
        + "</body></html>",
        encoding="utf-8",
    )
    export_with_browser(html_path, 720, 656)


if __name__ == "__main__":
    main()
