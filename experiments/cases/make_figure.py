from __future__ import annotations

import csv
import html
import json
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path


def HexColor(value: str) -> str:
    return value


white = "#FFFFFF"


class Drawing:
    def __init__(self, width: float, height: float):
        self.width = width
        self.height = height
        self.items = []

    def add(self, item) -> None:
        self.items.append(item)

    def write_svg(self, path: Path) -> None:
        body = "\n".join(item.to_svg(self.height) for item in self.items)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{self.width:.3f}pt" height="{self.height:.3f}pt" '
            f'viewBox="0 0 {self.width:.3f} {self.height:.3f}">\n'
            f'<rect width="100%" height="100%" fill="white"/>\n{body}\n</svg>\n'
        )
        path.write_text(svg, encoding="utf-8")


class Line:
    def __init__(self, x1, y1, x2, y2, *, strokeColor="#000000", strokeWidth=1, strokeDashArray=None):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.strokeColor = strokeColor or "none"
        self.strokeWidth = strokeWidth
        self.strokeDashArray = strokeDashArray

    def to_svg(self, canvas_height: float) -> str:
        dash = ""
        if self.strokeDashArray:
            dash = f' stroke-dasharray="{",".join(map(str, self.strokeDashArray))}"'
        return (
            f'<line x1="{self.x1:.3f}" y1="{canvas_height-self.y1:.3f}" '
            f'x2="{self.x2:.3f}" y2="{canvas_height-self.y2:.3f}" '
            f'stroke="{self.strokeColor}" stroke-width="{self.strokeWidth}"{dash}/>'
        )


class Circle:
    def __init__(self, x, y, radius, *, fillColor="none", strokeColor="#000000", strokeWidth=1):
        self.x, self.y, self.radius = x, y, radius
        self.fillColor = fillColor or "none"
        self.strokeColor = strokeColor or "none"
        self.strokeWidth = strokeWidth

    def to_svg(self, canvas_height: float) -> str:
        return (
            f'<circle cx="{self.x:.3f}" cy="{canvas_height-self.y:.3f}" r="{self.radius:.3f}" '
            f'fill="{self.fillColor}" stroke="{self.strokeColor}" stroke-width="{self.strokeWidth}"/>'
        )


class Rect:
    def __init__(self, x, y, width, height, *, fillColor="none", strokeColor="#000000", strokeWidth=1):
        self.x, self.y, self.width, self.height = x, y, width, height
        self.fillColor = fillColor or "none"
        self.strokeColor = strokeColor or "none"
        self.strokeWidth = strokeWidth

    def to_svg(self, canvas_height: float) -> str:
        return (
            f'<rect x="{self.x:.3f}" y="{canvas_height-self.y-self.height:.3f}" width="{self.width:.3f}" '
            f'height="{self.height:.3f}" fill="{self.fillColor}" '
            f'stroke="{self.strokeColor}" stroke-width="{self.strokeWidth}"/>'
        )


class String:
    def __init__(self, x, y, text, *, fontName="Times-Roman", fontSize=7, fillColor="#000000", textAnchor="start", angle=0):
        self.x, self.y, self.text = x, y, text
        self.fontName = fontName
        self.fontSize = fontSize
        self.fillColor = fillColor
        self.textAnchor = {"start": "start", "middle": "middle", "end": "end"}[textAnchor]
        self.angle = angle

    def to_svg(self, canvas_height: float) -> str:
        weight = "bold" if self.fontName.endswith("Bold") else "normal"
        y = canvas_height - self.y
        transform = ""
        if self.angle:
            transform = f' transform="rotate({-self.angle:.3f} {self.x:.3f} {y:.3f})"'
        return (
            f'<text x="{self.x:.3f}" y="{y:.3f}" fill="{self.fillColor}" '
            f'font-family="Times New Roman, Times, serif" font-size="{self.fontSize}" '
            f'font-weight="{weight}" text-anchor="{self.textAnchor}"{transform}>'
            f'{html.escape(str(self.text))}</text>'
        )


class RLPath:
    def __init__(self):
        self.commands = []
        self.strokeColor = "#000000"
        self.strokeWidth = 1
        self.fillColor = None

    def moveTo(self, x, y) -> None:
        self.commands.append(("M", x, y))

    def lineTo(self, x, y) -> None:
        self.commands.append(("L", x, y))

    def close(self) -> None:
        self.commands.append(("Z", None, None))

    def to_svg(self, canvas_height: float) -> str:
        parts = []
        for command, x, y in self.commands:
            if command == "Z":
                parts.append("Z")
            else:
                parts.append(f"{command} {x:.3f} {canvas_height-y:.3f}")
        return (
            f'<path d="{" ".join(parts)}" fill="{self.fillColor or "none"}" '
            f'stroke="{self.strokeColor or "none"}" stroke-width="{self.strokeWidth}" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )


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
TA_DIR = (
    BASELINE_ROOT
    / "tradingagents_gcusd_full_20260101_20260630_processed_news"
)

PRICE_PATH = (
    WORKSPACE
    / "runtime/market_data/gold/gold-gcusd-20260101-20260630/"
    "bars/GCUSD_5min.jsonl"
)
ASSESSMENT_DIR = WORKSPACE / "runtime/analysis_assessments/gold"
LEDGER_DIR = WORKSPACE / "ledger/gold"


LONG = HexColor("#D55E00")
SHORT = HexColor("#0072B2")
FLAT = HexColor("#6B7280")
EVENT = HexColor("#8B5CF6")
OURS_BG = HexColor("#EAF6F3")
DARK = HexColor("#111827")
TEXT = HexColor("#374151")
GRID = HexColor("#D1D5DB")
LIGHT_GRID = HexColor("#E5E7EB")


@dataclass(frozen=True)
class Case:
    panel: str
    title_lines: tuple[str, str]
    source_line: str
    event_at: datetime
    source_event_id: str
    assessment_id: str
    expected_analysis_state: str
    expected_analysis_at: datetime
    expected_post_states: dict[str, str]


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


CASES = [
    Case(
        panel="(a)",
        title_lines=(
            "Trump nominates Kevin Warsh",
            "to chair the Federal Reserve",
        ),
        source_line="CNBC, Jan 30, 00:00 UTC",
        event_at=dt("2026-01-30T00:00:00Z"),
        source_event_id="569ce027514f3a055e5315bb",
        assessment_id="gold-warsh-reversal-20260130",
        expected_analysis_state="weak_short",
        expected_analysis_at=dt("2026-01-30T00:30:00Z"),
        expected_post_states={
            "EventTrader": "weak_short",
            "TradingAgents": "flat",
            "FinMem": "flat",
            "AI Hedge Fund": "flat",
        },
    ),
    Case(
        panel="(b)",
        title_lines=(
            "Dollar and yields reverse gold's",
            "Iran safe-haven rally",
        ),
        source_line="Reuters, Mar 3, 02:28 UTC",
        event_at=dt("2026-03-03T02:28:44Z"),
        source_event_id="7bfbf7629933aac7d85b1537",
        assessment_id="gold-2026-03-03-0300-dollar-overrides-geopolitical",
        expected_analysis_state="weak_short",
        expected_analysis_at=dt("2026-03-03T02:57:50Z"),
        expected_post_states={
            "EventTrader": "weak_short",
            "TradingAgents": "flat",
            "FinMem": "flat",
            "AI Hedge Fund": "flat",
        },
    ),
    Case(
        panel="(c)",
        title_lines=(
            "U.S.-Iran peace hopes and a weaker",
            "dollar lift gold",
        ),
        source_line="Reuters, May 6, 02:44 UTC",
        event_at=dt("2026-05-06T02:44:02Z"),
        source_event_id="a6c86de95c2a1d6b8ce60de4",
        assessment_id="gold-assessment-2026-05-06T03:14:02",
        expected_analysis_state="weak_long",
        expected_analysis_at=dt("2026-05-06T03:14:02Z"),
        expected_post_states={
            "EventTrader": "weak_long",
            "TradingAgents": "flat",
            "FinMem": "flat",
            "AI Hedge Fund": "flat",
        },
    ),
    Case(
        panel="(d)",
        title_lines=(
            "India raises gold and silver import",
            "tariffs to 15% amid mixed signals",
        ),
        source_line="Reuters, May 12, 08:25 UTC",
        event_at=dt("2026-05-12T08:25:00Z"),
        source_event_id="a7e2aefd47ceda633bc7bf9a",
        assessment_id="gold-2026-05-12T08:29:59-eval",
        expected_analysis_state="flat",
        expected_analysis_at=dt("2026-05-12T08:29:59Z"),
        expected_post_states={
            "EventTrader": "flat",
            "TradingAgents": "flat",
            "FinMem": "flat",
            "AI Hedge Fund": "long",
        },
    ),
]


def load_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def direction(state: str) -> str:
    state = state.lower()
    if "long" in state:
        return "long"
    if "short" in state:
        return "short"
    return "flat"


def state_color(state: str):
    return {"long": LONG, "short": SHORT, "flat": FLAT}[direction(state)]


def state_label(state: str) -> str:
    labels = {
        "strong_long": "Strong Long",
        "weak_long": "Weak Long",
        "long": "Long",
        "flat": "Flat",
        "weak_short": "Weak Short",
        "strong_short": "Strong Short",
        "short": "Short",
    }
    return labels[state]


def state_from_exposure(value: str) -> str:
    exposure = float(value)
    if exposure > 1e-8:
        return "long"
    if exposure < -1e-8:
        return "short"
    return "flat"


def find_assessment(case: Case) -> dict:
    rows = load_jsonl(ASSESSMENT_DIR / f"{case.event_at:%Y-%m}.jsonl")
    matches = [row for row in rows if row["assessment_id"] == case.assessment_id]
    assert len(matches) == 1, (case.panel, "assessment", len(matches))
    row = matches[0]
    assert dt(row["business_at"]) == case.expected_analysis_at
    assert row["as_if_flat_state"] == case.expected_analysis_state
    assert case.source_event_id in row["source_event_ids"]
    return row


def find_source(case: Case) -> dict:
    rows = load_jsonl(LEDGER_DIR / f"{case.event_at:%Y-%m-%d}.jsonl")
    matches = [row for row in rows if row["event_id"] == case.source_event_id]
    assert len(matches) == 1, (case.panel, "source", len(matches))
    row = matches[0]
    assert dt(row["ts_init"]) == case.event_at
    return row


def eod_at(case: Case) -> datetime:
    return datetime.combine(case.event_at.date(), time(23, 59, 59), timezone.utc)


def first_bar_after(bars: list[dict], when: datetime) -> dict:
    candidates = [row for row in bars if dt(row["start_at"]) >= when]
    assert candidates, when
    return min(candidates, key=lambda row: row["start_at"])


def baseline_rows() -> list[dict]:
    return load_csv(EVAL_DIR / "tradingagents_decision_diagnostics.csv")


def timeline_row(rows: list[dict], baseline: str, date_value: str) -> dict:
    matches = [
        row
        for row in rows
        if row["baseline"] == baseline and row["trade_date"] == date_value
    ]
    assert len(matches) == 1, (baseline, date_value, len(matches))
    return matches[0]


def exposure_for_state(state: str) -> float:
    return {
        "strong_long": 1.0,
        "weak_long": 0.5,
        "long": 1.0,
        "flat": 0.0,
        "weak_short": -0.5,
        "strong_short": -1.0,
        "short": -1.0,
    }[state]


def forward_return(
    bars: list[dict], decision_at: datetime, hours: int = 6
) -> tuple[datetime, float, datetime, float, float]:
    entry = first_bar_after(bars, decision_at)
    entry_at = dt(entry["start_at"])
    entry_price = float(entry["open_price"])
    target = entry_at + timedelta(hours=hours)
    candidates = [
        row
        for row in bars
        if dt(row["start_at"]) >= entry_at and dt(row["end_at"]) <= target
    ]
    assert candidates, (decision_at, target)
    last = max(candidates, key=lambda row: row["end_at"])
    exit_at = dt(last["end_at"])
    exit_price = float(last["close_price"])
    value = (exit_price / entry_price - 1.0) * 100.0
    return entry_at, entry_price, exit_at, exit_price, value


def temporary_event_pnl(state: str, entry_price: float, exit_price: float) -> tuple[float, float]:
    initial_equity = 100_000.0
    exposure = exposure_for_state(state)
    if exposure == 0.0:
        return 0.0, 0.0
    quantity = initial_equity * exposure / entry_price
    gross_pnl = quantity * (exit_price - entry_price)
    entry_fee = abs(quantity * entry_price) * 0.00005
    exit_fee = abs(quantity * exit_price) * 0.00005
    net_pnl = gross_pnl - entry_fee - exit_fee
    return net_pnl, net_pnl / initial_equity * 100.0


def build_method_records(case: Case, bars: list[dict], ta_rows: list[dict]) -> list[dict]:
    date_value = f"{case.event_at:%Y-%m-%d}"
    assessment = find_assessment(case)

    ahf_audit = json.loads((AHF_DIR / date_value / "audit.json").read_text(encoding="utf-8"))
    finmem_audit = json.loads(
        (FINMEM_DIR / "days" / date_value / "audit.json").read_text(encoding="utf-8")
    )
    ta_diag = timeline_row(ta_rows, "tradingagents", date_value)
    ta_rating = ta_diag["pm_rating"]
    ta_post = (
        "long"
        if ta_rating in {"Buy", "Overweight"}
        else "short"
        if ta_rating in {"Underweight", "Sell"}
        else "flat"
    )
    finmem_action = finmem_audit["decision"].get("native_action")
    finmem_post = (
        "long" if finmem_action == "buy" else "short" if finmem_action == "sell" else "flat"
    )
    ahf_action = ahf_audit["native_action"]
    ahf_post = "long" if ahf_action == "buy" else "short" if ahf_action == "short" else "flat"

    observed_post = {
        "EventTrader": assessment["as_if_flat_state"],
        "TradingAgents": ta_post,
        "FinMem": finmem_post,
        "AI Hedge Fund": ahf_post,
    }
    assert observed_post == case.expected_post_states, (case.panel, observed_post)

    records = [
        {
            "method": "EventTrader",
            "decision_at": dt(assessment["business_at"]),
            "decision_label": state_label(observed_post["EventTrader"]),
            "post_state": observed_post["EventTrader"],
            "marker_filled": True,
            "decision_kind": "event-linked analysis",
        },
        {
            "method": "TradingAgents",
            "decision_at": eod_at(case),
            "decision_label": state_label(ta_post) if ta_post != "flat" else "No direction",
            "post_state": ta_post,
            "marker_filled": False,
            "decision_kind": "scheduled EOD decision",
        },
        {
            "method": "FinMem",
            "decision_at": dt(finmem_audit["decision_at_utc"]),
            "decision_label": state_label(finmem_post) if finmem_post != "flat" else "No direction",
            "post_state": finmem_post,
            "marker_filled": False,
            "decision_kind": "scheduled EOD decision",
        },
        {
            "method": "AI Hedge Fund",
            "decision_at": dt(ahf_audit["decision_at"]),
            "decision_label": state_label(ahf_post) if ahf_post != "flat" else "No direction",
            "post_state": ahf_post,
            "marker_filled": False,
            "decision_kind": "scheduled EOD decision",
        },
    ]
    for record in records:
        entry_at, entry_price, exit_at, exit_price, price_return = forward_return(
            bars, record["decision_at"]
        )
        pnl_dollars, pnl_return = temporary_event_pnl(
            record["post_state"], entry_price, exit_price
        )
        record["measurement_entry_at"] = entry_at
        record["measurement_entry_price"] = entry_price
        record["measurement_exit_at"] = exit_at
        record["measurement_exit_price"] = exit_price
        record["price_return_6h"] = price_return
        record["event_pnl_dollars"] = pnl_dollars
        record["event_pnl_return_pct"] = pnl_return
    return records


def price_window(case: Case, bars: list[dict]) -> tuple[list[tuple[float, float]], float, float, float]:
    end_at = case.event_at.timestamp() + 24 * 3600
    candidates = [
        row
        for row in bars
        if dt(row["start_at"]).timestamp() >= case.event_at.timestamp()
        and dt(row["end_at"]).timestamp() <= end_at
    ]
    assert candidates, case.panel
    candidates.sort(key=lambda row: row["start_at"])
    base = float(candidates[0]["open_price"])
    points = [(0.0, 0.0)]
    for row in candidates:
        hour = (dt(row["end_at"]) - case.event_at).total_seconds() / 3600
        value = (float(row["close_price"]) / base - 1.0) * 100.0
        points.append((hour, value))
    final_return = points[-1][1]
    low = min(value for _, value in points)
    high = max(value for _, value in points)
    return points, final_return, low, high


def add_marker(drawing: Drawing, x: float, y: float, state: str, filled: bool, size: float = 3.4) -> None:
    color = state_color(state)
    fill = color if filled else white
    shape = direction(state)
    if shape == "flat":
        drawing.add(Circle(x, y, size, fillColor=fill, strokeColor=color, strokeWidth=1.05))
        return
    path = RLPath()
    if shape == "long":
        path.moveTo(x, y + size + 0.8)
        path.lineTo(x + size + 0.5, y - size)
        path.lineTo(x - size - 0.5, y - size)
    else:
        path.moveTo(x, y - size - 0.8)
        path.lineTo(x + size + 0.5, y + size)
        path.lineTo(x - size - 0.5, y + size)
    path.close()
    path.fillColor = fill
    path.strokeColor = color
    path.strokeWidth = 1.05
    drawing.add(path)


def hours_after(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600


def fmt_delay(value: float) -> str:
    if value < 1:
        return f"{value * 60:.1f} min"
    return f"{value:.1f} h"


def fmt_pnl(value: float) -> str:
    if abs(value) < 0.005:
        return "0.00%"
    return f"{value:+.2f}%"


def plot_panel(
    drawing: Drawing,
    case: Case,
    x0: float,
    y0: float,
    width: float,
    height: float,
    points: list[tuple[float, float]],
    final_return: float,
    low: float,
    high: float,
    methods: list[dict],
) -> None:
    drawing.add(String(x0, y0 + height - 9, case.panel, fontName="Times-Bold", fontSize=8.4, fillColor=DARK))
    drawing.add(String(x0 + 17, y0 + height - 9, case.title_lines[0], fontName="Times-Bold", fontSize=8.0, fillColor=DARK))
    drawing.add(String(x0 + 17, y0 + height - 19, case.title_lines[1], fontName="Times-Bold", fontSize=8.0, fillColor=DARK))
    drawing.add(String(x0 + width, y0 + height - 30, case.source_line, fontName="Times-Roman", fontSize=6.6, textAnchor="end", fillColor=TEXT))

    left = x0 + 67
    right = x0 + width - 7
    plot_width = right - left
    price_bottom = y0 + 99
    price_top = y0 + height - 41
    price_height = price_top - price_bottom

    span = max(high - low, 0.35)
    y_min = min(low - 0.12 * span, -0.12)
    y_max = max(high + 0.12 * span, 0.12)

    def xp(hour: float) -> float:
        return left + max(0.0, min(hour, 24.0)) / 24.0 * plot_width

    def yp(value: float) -> float:
        return price_bottom + (value - y_min) / (y_max - y_min) * price_height

    for hour in (0, 6, 12, 18, 24):
        x = xp(float(hour))
        drawing.add(Line(x, y0 + 23, x, price_top, strokeColor=LIGHT_GRID, strokeWidth=0.45))
    if y_min < 0 < y_max:
        drawing.add(Line(left, yp(0), right, yp(0), strokeColor=GRID, strokeWidth=0.55))

    for value in (y_min, (y_min + y_max) / 2, y_max):
        y = yp(value)
        drawing.add(Line(left - 2, y, left, y, strokeColor=TEXT, strokeWidth=0.5))
        drawing.add(String(left - 4, y - 2.4, f"{value:.1f}", fontName="Times-Roman", fontSize=6.6, textAnchor="end", fillColor=TEXT))

    path = RLPath()
    for index, (hour, value) in enumerate(points):
        if index == 0:
            path.moveTo(xp(hour), yp(value))
        else:
            path.lineTo(xp(hour), yp(value))
    path.strokeColor = DARK
    path.strokeWidth = 1.15
    path.fillColor = None
    drawing.add(path)
    drawing.add(Circle(xp(points[-1][0]), yp(points[-1][1]), 1.7, fillColor=DARK, strokeColor=DARK))

    ret_color = SHORT if final_return < -0.05 else LONG if final_return > 0.05 else FLAT
    drawing.add(String(right, price_top - 8, f"24 h return: {final_return:+.2f}%", fontName="Times-Bold", fontSize=6.8, textAnchor="end", fillColor=ret_color))
    drawing.add(String(x0 + 7, (price_bottom + price_top) / 2 - 14, "GCUSD return (%)", fontName="Times-Roman", fontSize=7.0, angle=90, fillColor=TEXT))

    event_x = xp(0)
    drawing.add(Line(event_x, y0 + 23, event_x, price_top, strokeColor=EVENT, strokeWidth=0.9, strokeDashArray=[2, 2]))
    drawing.add(String(event_x + 3, price_top - 7, "Evidence visible", fontName="Times-Roman", fontSize=6.2, fillColor=EVENT))

    row_y = {
        "EventTrader": y0 + 79,
        "TradingAgents": y0 + 63,
        "FinMem": y0 + 47,
        "AI Hedge Fund": y0 + 31,
    }
    drawing.add(String(right, y0 + 89, "Decision delay | 6 h event PnL", fontName="Times-Roman", fontSize=6.3, textAnchor="end", fillColor=TEXT))
    drawing.add(Rect(x0 + 1, row_y["EventTrader"] - 6, width - 2, 12, fillColor=OURS_BG, strokeColor=None))

    for record in methods:
        method = record["method"]
        y = row_y[method]
        font = "Times-Bold" if method == "EventTrader" else "Times-Roman"
        drawing.add(String(left - 5, y - 2.5, method, fontName=font, fontSize=6.9, textAnchor="end", fillColor=DARK))

        decision_hour = hours_after(case.event_at, record["decision_at"])
        marker_hour = max(0.0, min(decision_hour, 24.0))
        marker_x = xp(marker_hour)
        drawing.add(Line(left, y, marker_x, y, strokeColor=GRID, strokeWidth=1.35))
        add_marker(drawing, marker_x, y, record["post_state"], record["marker_filled"])

        label = (
            f"{record['decision_label']}, {fmt_delay(max(decision_hour, 0.0))}"
            f" | {fmt_pnl(record['event_pnl_return_pct'])}"
        )
        if marker_hour < 8.0:
            drawing.add(String(marker_x + 5, y + 4.0, label, fontName=font, fontSize=6.3, fillColor=TEXT))
        else:
            drawing.add(String(marker_x - 5, y + 4.0, label, fontName=font, fontSize=6.3, textAnchor="end", fillColor=TEXT))

    for hour in (0, 6, 12, 18, 24):
        x = xp(float(hour))
        drawing.add(String(x, y0 + 15, str(hour), fontName="Times-Roman", fontSize=6.6, textAnchor="middle", fillColor=TEXT))
    drawing.add(String((left + right) / 2, y0 + 5, "Hours since evidence visibility", fontName="Times-Roman", fontSize=7.0, textAnchor="middle", fillColor=TEXT))


def write_audit(rows: list[dict]) -> None:
    fields = [
        "panel",
        "event_title",
        "source",
        "source_url",
        "event_visible_at_utc",
        "method",
        "decision_at_utc",
        "decision_delay_minutes",
        "decision",
        "measurement_entry_at_utc",
        "measurement_entry_price",
        "measurement_exit_at_utc",
        "measurement_exit_price",
        "gcusd_6h_return_after_decision_pct",
        "event_pnl_return_pct",
        "event_pnl_dollars_on_100k",
        "decision_kind",
        "gcusd_24h_return_pct",
    ]
    with (OUT_DIR / "case_audit.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def add_legend(drawing: Drawing) -> None:
    y = 15
    x = 54
    add_marker(drawing, x, y, "long", True, 3.0)
    drawing.add(String(x + 7, y - 2.3, "Long", fontName="Times-Roman", fontSize=6.8, fillColor=TEXT))
    x += 53
    add_marker(drawing, x, y, "flat", True, 3.0)
    drawing.add(String(x + 7, y - 2.3, "Flat / no direction", fontName="Times-Roman", fontSize=6.8, fillColor=TEXT))
    x += 92
    add_marker(drawing, x, y, "short", True, 3.0)
    drawing.add(String(x + 7, y - 2.3, "Short", fontName="Times-Roman", fontSize=6.8, fillColor=TEXT))
    x += 55
    add_marker(drawing, x, y, "long", True, 3.0)
    drawing.add(String(x + 7, y - 2.3, "event-linked analysis", fontName="Times-Roman", fontSize=6.8, fillColor=TEXT))
    x += 105
    add_marker(drawing, x, y, "long", False, 3.0)
    drawing.add(String(x + 7, y - 2.3, "scheduled EOD decision", fontName="Times-Roman", fontSize=6.8, fillColor=TEXT))


def export_with_chrome(html_path: Path) -> None:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    browser = next((path for path in candidates if path.exists()), None)
    if browser is None:
        raise RuntimeError("Chrome or Edge is required to export the SVG preview to PDF and PNG")
    url = html_path.resolve().as_uri()
    pdf_path = (IMAGE_DIR / "agent_event_response_cases.pdf").resolve()
    png_path = (IMAGE_DIR / "agent_event_response_cases.png").resolve()
    with tempfile.TemporaryDirectory(prefix="agent-case-pdf-") as pdf_profile:
        subprocess.run(
            [
                str(browser),
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                f"--user-data-dir={pdf_profile}",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                url,
            ],
            check=True,
        )
    with tempfile.TemporaryDirectory(prefix="agent-case-png-") as png_profile:
        subprocess.run(
            [
                str(browser),
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--default-background-color=ffffffff",
                "--force-color-profile=srgb",
                f"--user-data-dir={png_profile}",
                "--force-device-scale-factor=2",
                "--window-size=680,592",
                f"--screenshot={png_path}",
                url,
            ],
            check=True,
        )


def main() -> None:
    bars = load_jsonl(PRICE_PATH)
    ta_rows = baseline_rows()

    width, height = 510.0, 444.0
    drawing = Drawing(width, height)
    audit_rows: list[dict] = []
    panel_width, panel_height = 247.0, 202.0
    panel_origins = [(4, 235), (259, 235), (4, 27), (259, 27)]

    for case, (x0, y0) in zip(CASES, panel_origins):
        source = find_source(case)
        methods = build_method_records(case, bars, ta_rows)
        points, final_return, low, high = price_window(case, bars)
        plot_panel(
            drawing,
            case,
            x0,
            y0,
            panel_width,
            panel_height,
            points,
            final_return,
            low,
            high,
            methods,
        )
        for record in methods:
            audit_rows.append(
                {
                    "panel": case.panel,
                    "event_title": " ".join(case.title_lines),
                    "source": case.source_line.split(",", 1)[0],
                    "source_url": source["source_ref"],
                    "event_visible_at_utc": case.event_at.isoformat(),
                    "method": record["method"],
                    "decision_at_utc": record["decision_at"].isoformat(),
                    "decision_delay_minutes": f"{hours_after(case.event_at, record['decision_at']) * 60:.3f}",
                    "decision": record["decision_label"],
                    "measurement_entry_at_utc": record["measurement_entry_at"].isoformat(),
                    "measurement_entry_price": f"{record['measurement_entry_price']:.6f}",
                    "measurement_exit_at_utc": record["measurement_exit_at"].isoformat(),
                    "measurement_exit_price": f"{record['measurement_exit_price']:.6f}",
                    "gcusd_6h_return_after_decision_pct": f"{record['price_return_6h']:.6f}",
                    "event_pnl_return_pct": f"{record['event_pnl_return_pct']:.6f}",
                    "event_pnl_dollars_on_100k": f"{record['event_pnl_dollars']:.2f}",
                    "decision_kind": record["decision_kind"],
                    "gcusd_24h_return_pct": f"{final_return:.6f}",
                }
            )

    add_legend(drawing)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    write_audit(audit_rows)
    svg_path = IMAGE_DIR / "agent_event_response_cases.svg"
    drawing.write_svg(svg_path)
    html_path = OUT_DIR / "agent_event_response_cases.html"
    html_path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>@page{size:510pt 444pt;margin:0}html,body{margin:0;padding:0;"
        "width:510pt;height:444pt;overflow:hidden;background:white}"
        "svg{display:block;width:510pt;height:444pt}</style></head><body>"
        + svg_path.read_text(encoding="utf-8")
        + "</body></html>",
        encoding="utf-8",
    )
    export_with_chrome(html_path)


if __name__ == "__main__":
    main()
