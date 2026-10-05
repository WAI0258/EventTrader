from __future__ import annotations

import argparse
import csv
import math
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.pdfgen import canvas


BASELINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVAL_DIR = (
    BASELINE_ROOT / "experiments" / "gold_baseline_eval_20260101_20260630"
)
DEFAULT_INPUT = DEFAULT_EVAL_DIR / "eventtrader_vs_baselines_progress.csv"
DEFAULT_OUTPUT_STEM = "eventtrader_vs_agent_baselines_paper"

PAGE_WIDTH = 540.0
PAGE_HEIGHT = 250.0
PLOT_LEFT = 52.0
PLOT_RIGHT = 426.0
PLOT_BOTTOM = 35.0
PLOT_TOP = 236.0


@dataclass(frozen=True)
class Point:
    trade_date: date
    cumulative_return: float


@dataclass(frozen=True)
class SeriesStyle:
    label: str
    color: str
    width: float
    dash: tuple[float, ...]
    marker: str
    bold_label: bool = False


SERIES_ORDER = (
    "event_trader",
    "tradingagents",
    "finmem",
    "ai_hedge_fund",
    "buy_and_hold",
)

STYLES = {
    "event_trader": SeriesStyle(
        label="EventTrader",
        color="#005EA8",
        width=2.4,
        dash=(),
        marker="circle",
        bold_label=True,
    ),
    "tradingagents": SeriesStyle(
        label="TradingAgents",
        color="#D55E00",
        width=1.55,
        dash=(6.0, 2.6),
        marker="square",
    ),
    "finmem": SeriesStyle(
        label="FinMem",
        color="#A64CA6",
        width=1.55,
        dash=(5.0, 2.0, 1.2, 2.0),
        marker="diamond",
    ),
    "ai_hedge_fund": SeriesStyle(
        label="AI Hedge Fund",
        color="#00866A",
        width=1.55,
        dash=(1.3, 2.2),
        marker="triangle",
    ),
    "buy_and_hold": SeriesStyle(
        label="Buy & Hold",
        color="#555555",
        width=1.4,
        dash=(7.0, 3.0),
        marker="cross",
    ),
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render the paper-ready EventTrader agent-baseline figure."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_EVAL_DIR)
    parser.add_argument("--output-stem", default=DEFAULT_OUTPUT_STEM)
    parser.add_argument(
        "--paper-pdf",
        type=Path,
        help="Optional path that receives a copy of the generated PDF.",
    )
    return parser.parse_args()


def _read_curves(path: Path) -> dict[str, list[Point]]:
    curves: dict[str, list[Point]] = {name: [] for name in SERIES_ORDER}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            strategy = row["strategy"]
            if strategy not in curves:
                continue
            equity = float(row["equity_close"])
            curves[strategy].append(
                Point(
                    trade_date=date.fromisoformat(row["trade_date"]),
                    cumulative_return=(equity / 100_000.0 - 1.0) * 100.0,
                )
            )

    missing = [name for name, points in curves.items() if not points]
    if missing:
        raise ValueError(f"Missing required series: {', '.join(missing)}")

    calendars = []
    for points in curves.values():
        points.sort(key=lambda item: item.trade_date)
        calendars.append(tuple(item.trade_date for item in points))
    if any(calendar != calendars[0] for calendar in calendars[1:]):
        raise ValueError("Agent-baseline curves do not share one aligned calendar.")
    if calendars[0][0] != date(2026, 1, 1) or calendars[0][-1] != date(2026, 6, 30):
        raise ValueError("Expected the closed 2026-01-01 through 2026-06-30 window.")
    return curves


def _plot_bounds(curves: dict[str, list[Point]]) -> tuple[float, float]:
    values = [point.cumulative_return for points in curves.values() for point in points]
    lower = math.floor((min(values) - 1.0) / 5.0) * 5.0
    upper = math.ceil((max(values) + 1.0) / 5.0) * 5.0
    return min(lower, -10.0), max(upper, 30.0)


def _month_marker_indices(points: list[Point]) -> set[int]:
    indices = {0, len(points) - 1}
    previous_month = points[0].trade_date.month
    for index, point in enumerate(points[1:], start=1):
        if point.trade_date.month != previous_month:
            indices.add(index)
            previous_month = point.trade_date.month
    return indices


def _hex_to_rgb(color: str) -> tuple[float, float, float]:
    value = color.removeprefix("#")
    return tuple(int(value[index : index + 2], 16) / 255.0 for index in (0, 2, 4))


def _x_position(value: date, start: date, end: date) -> float:
    span = (end - start).days
    return PLOT_LEFT + ((value - start).days / span) * (PLOT_RIGHT - PLOT_LEFT)


def _y_position(value: float, lower: float, upper: float) -> float:
    return PLOT_BOTTOM + ((value - lower) / (upper - lower)) * (PLOT_TOP - PLOT_BOTTOM)


def _y_ticks(lower: float, upper: float) -> list[float]:
    first = math.ceil(lower / 10.0) * 10.0
    ticks = []
    current = first
    while current <= upper:
        ticks.append(current)
        current += 10.0
    return ticks


def _x_ticks(start: date, end: date) -> list[tuple[date, str]]:
    ticks = [
        (date(2026, 1, 1), "Jan"),
        (date(2026, 2, 1), "Feb"),
        (date(2026, 3, 1), "Mar"),
        (date(2026, 4, 1), "Apr"),
        (date(2026, 5, 1), "May"),
        (date(2026, 6, 1), "Jun"),
    ]
    return [(value, label) for value, label in ticks if start <= value <= end]


def _svg_marker(x: float, y: float, style: SeriesStyle) -> str:
    color = style.color
    if style.marker == "circle":
        return f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.45" fill="white" stroke="{color}" stroke-width="1.35"/>'
    if style.marker == "square":
        return f'<rect x="{x - 2.2:.2f}" y="{y - 2.2:.2f}" width="4.4" height="4.4" fill="white" stroke="{color}" stroke-width="1.2"/>'
    if style.marker == "diamond":
        return f'<polygon points="{x:.2f},{y - 2.7:.2f} {x + 2.7:.2f},{y:.2f} {x:.2f},{y + 2.7:.2f} {x - 2.7:.2f},{y:.2f}" fill="white" stroke="{color}" stroke-width="1.2"/>'
    if style.marker == "triangle":
        return f'<polygon points="{x:.2f},{y - 2.8:.2f} {x + 2.7:.2f},{y + 2.2:.2f} {x - 2.7:.2f},{y + 2.2:.2f}" fill="white" stroke="{color}" stroke-width="1.2"/>'
    return (
        f'<path d="M {x - 2.3:.2f} {y - 2.3:.2f} L {x + 2.3:.2f} {y + 2.3:.2f} '
        f'M {x - 2.3:.2f} {y + 2.3:.2f} L {x + 2.3:.2f} {y - 2.3:.2f}" '
        f'stroke="{color}" stroke-width="1.2"/>'
    )


def _render_svg(path: Path, curves: dict[str, list[Point]]) -> None:
    start = curves[SERIES_ORDER[0]][0].trade_date
    end = curves[SERIES_ORDER[0]][-1].trade_date
    lower, upper = _plot_bounds(curves)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_WIDTH:.0f}" height="{PAGE_HEIGHT:.0f}" viewBox="0 0 {PAGE_WIDTH:.0f} {PAGE_HEIGHT:.0f}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]

    for tick in _y_ticks(lower, upper):
        y = PAGE_HEIGHT - _y_position(tick, lower, upper)
        stroke = "#AFAFAF" if tick == 0 else "#E2E2E2"
        width = 0.9 if tick == 0 else 0.55
        parts.append(
            f'<line x1="{PLOT_LEFT:.2f}" y1="{y:.2f}" x2="{PLOT_RIGHT:.2f}" y2="{y:.2f}" stroke="{stroke}" stroke-width="{width}"/>'
        )
        parts.append(
            f'<text x="{PLOT_LEFT - 7:.2f}" y="{y + 3:.2f}" text-anchor="end" font-family="Times New Roman, Times, serif" font-size="8" fill="#333333">{tick:.0f}</text>'
        )

    axis_y = PAGE_HEIGHT - PLOT_BOTTOM
    parts.append(
        f'<line x1="{PLOT_LEFT:.2f}" y1="{axis_y:.2f}" x2="{PLOT_RIGHT:.2f}" y2="{axis_y:.2f}" stroke="#333333" stroke-width="0.75"/>'
    )
    for tick_date, label in _x_ticks(start, end):
        x = _x_position(tick_date, start, end)
        parts.append(
            f'<line x1="{x:.2f}" y1="{axis_y:.2f}" x2="{x:.2f}" y2="{axis_y + 3:.2f}" stroke="#333333" stroke-width="0.65"/>'
        )
        parts.append(
            f'<text x="{x:.2f}" y="{axis_y + 13:.2f}" text-anchor="middle" font-family="Times New Roman, Times, serif" font-size="8" fill="#333333">{label}</text>'
        )

    parts.extend(
        [
            f'<text x="{(PLOT_LEFT + PLOT_RIGHT) / 2:.2f}" y="{PAGE_HEIGHT - 8:.2f}" text-anchor="middle" font-family="Times New Roman, Times, serif" font-size="8.5" fill="#222222">Date (2026)</text>',
            f'<text transform="translate(13 {(PAGE_HEIGHT - (PLOT_BOTTOM + PLOT_TOP) / 2):.2f}) rotate(-90)" text-anchor="middle" font-family="Times New Roman, Times, serif" font-size="8.5" fill="#222222">Cumulative return (%)</text>',
            f'<text x="{PLOT_RIGHT + 7:.2f}" y="14" font-family="Times New Roman, Times, serif" font-size="7.6" font-weight="700" fill="#333333">Final return</text>',
        ]
    )

    for strategy in SERIES_ORDER:
        points = curves[strategy]
        style = STYLES[strategy]
        coordinates = [
            (
                _x_position(point.trade_date, start, end),
                PAGE_HEIGHT - _y_position(point.cumulative_return, lower, upper),
            )
            for point in points
        ]
        path_data = " ".join(
            f'{"M" if index == 0 else "L"} {x:.2f} {y:.2f}'
            for index, (x, y) in enumerate(coordinates)
        )
        dash = (
            f' stroke-dasharray="{" ".join(f"{value:g}" for value in style.dash)}"'
            if style.dash
            else ""
        )
        parts.append(
            f'<path d="{path_data}" fill="none" stroke="{style.color}" stroke-width="{style.width}"{dash} stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for index in _month_marker_indices(points):
            parts.append(_svg_marker(*coordinates[index], style))

        final_return = points[-1].cumulative_return
        label_y = coordinates[-1][1] + 2.6
        weight = "700" if style.bold_label else "400"
        parts.append(
            f'<text x="{PLOT_RIGHT + 7:.2f}" y="{label_y:.2f}" font-family="Times New Roman, Times, serif" font-size="7.8" font-weight="{weight}" fill="{style.color}">{escape(style.label)} {final_return:+.2f}%</text>'
        )

    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def _draw_pdf_marker(pdf: canvas.Canvas, x: float, y: float, style: SeriesStyle) -> None:
    size = 2.4
    pdf.setStrokeColorRGB(*_hex_to_rgb(style.color))
    pdf.setFillColorRGB(1.0, 1.0, 1.0)
    pdf.setLineWidth(1.0)
    if style.marker == "circle":
        pdf.circle(x, y, size, stroke=1, fill=1)
    elif style.marker == "square":
        pdf.rect(x - size, y - size, size * 2, size * 2, stroke=1, fill=1)
    elif style.marker == "diamond":
        marker = pdf.beginPath()
        marker.moveTo(x, y + size + 0.3)
        marker.lineTo(x + size + 0.3, y)
        marker.lineTo(x, y - size - 0.3)
        marker.lineTo(x - size - 0.3, y)
        marker.close()
        pdf.drawPath(marker, stroke=1, fill=1)
    elif style.marker == "triangle":
        marker = pdf.beginPath()
        marker.moveTo(x, y + size + 0.4)
        marker.lineTo(x + size + 0.3, y - size)
        marker.lineTo(x - size - 0.3, y - size)
        marker.close()
        pdf.drawPath(marker, stroke=1, fill=1)
    else:
        pdf.line(x - size, y - size, x + size, y + size)
        pdf.line(x - size, y + size, x + size, y - size)


def _render_pdf(path: Path, curves: dict[str, list[Point]]) -> None:
    start = curves[SERIES_ORDER[0]][0].trade_date
    end = curves[SERIES_ORDER[0]][-1].trade_date
    lower, upper = _plot_bounds(curves)
    pdf = canvas.Canvas(str(path), pagesize=(PAGE_WIDTH, PAGE_HEIGHT), pageCompression=1)

    for tick in _y_ticks(lower, upper):
        y = _y_position(tick, lower, upper)
        gray = 0.68 if tick == 0 else 0.88
        pdf.setStrokeColorRGB(gray, gray, gray)
        pdf.setLineWidth(0.9 if tick == 0 else 0.55)
        pdf.line(PLOT_LEFT, y, PLOT_RIGHT, y)
        pdf.setFont("Times-Roman", 8)
        pdf.setFillColorRGB(0.2, 0.2, 0.2)
        pdf.drawRightString(PLOT_LEFT - 7, y - 2.6, f"{tick:.0f}")

    pdf.setStrokeColorRGB(0.2, 0.2, 0.2)
    pdf.setLineWidth(0.75)
    pdf.line(PLOT_LEFT, PLOT_BOTTOM, PLOT_RIGHT, PLOT_BOTTOM)
    for tick_date, label in _x_ticks(start, end):
        x = _x_position(tick_date, start, end)
        pdf.line(x, PLOT_BOTTOM, x, PLOT_BOTTOM - 3)
        pdf.setFont("Times-Roman", 8)
        pdf.drawCentredString(x, PLOT_BOTTOM - 13, label)

    pdf.setFont("Times-Roman", 8.5)
    pdf.drawCentredString((PLOT_LEFT + PLOT_RIGHT) / 2, 8, "Date (2026)")
    pdf.saveState()
    pdf.translate(13, (PLOT_BOTTOM + PLOT_TOP) / 2)
    pdf.rotate(90)
    pdf.drawCentredString(0, 0, "Cumulative return (%)")
    pdf.restoreState()
    pdf.setFont("Times-Bold", 7.6)
    pdf.drawString(PLOT_RIGHT + 7, PAGE_HEIGHT - 14, "Final return")

    for strategy in SERIES_ORDER:
        points = curves[strategy]
        style = STYLES[strategy]
        coordinates = [
            (
                _x_position(point.trade_date, start, end),
                _y_position(point.cumulative_return, lower, upper),
            )
            for point in points
        ]
        pdf.setStrokeColorRGB(*_hex_to_rgb(style.color))
        pdf.setLineWidth(style.width)
        pdf.setDash(list(style.dash))
        line = pdf.beginPath()
        line.moveTo(*coordinates[0])
        for x, y in coordinates[1:]:
            line.lineTo(x, y)
        pdf.drawPath(line, stroke=1, fill=0)
        pdf.setDash()
        for index in _month_marker_indices(points):
            _draw_pdf_marker(pdf, *coordinates[index], style)

        final_return = points[-1].cumulative_return
        pdf.setFillColorRGB(*_hex_to_rgb(style.color))
        pdf.setFont("Times-Bold" if style.bold_label else "Times-Roman", 7.8)
        pdf.drawString(
            PLOT_RIGHT + 7,
            coordinates[-1][1] - 2.6,
            f"{style.label} {final_return:+.2f}%",
        )

    pdf.showPage()
    pdf.save()


def main() -> int:
    args = _parse_args()
    input_path = args.input.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    curves = _read_curves(input_path)

    svg_path = output_dir / f"{args.output_stem}.svg"
    pdf_path = output_dir / f"{args.output_stem}.pdf"
    _render_svg(svg_path, curves)
    _render_pdf(pdf_path, curves)

    if args.paper_pdf:
        paper_pdf = args.paper_pdf.resolve()
        paper_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf_path, paper_pdf)

    final_values = ", ".join(
        f"{STYLES[name].label}={curves[name][-1].cumulative_return:+.2f}%"
        for name in SERIES_ORDER
    )
    print(f"Wrote {svg_path}")
    print(f"Wrote {pdf_path}")
    if args.paper_pdf:
        print(f"Copied {args.paper_pdf.resolve()}")
    print(final_values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
