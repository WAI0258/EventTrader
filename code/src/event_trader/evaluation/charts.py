"""Small SVG charts for replay and baseline evaluation outputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import TypeAlias

from event_trader.evaluation.baselines import DailyStrategyPoint

ChartTimePoint: TypeAlias = date | datetime

_DEFAULT_COLORS = (
    "#ff4b4b",
    "#6f42c1",
    "#ffb000",
    "#4daf4a",
    "#66c2a5",
    "#7f7f7f",
    "#cdb5e8",
    "#1f77b4",
)


class EvaluationChartError(ValueError):
    """Raised when chart inputs are invalid."""


@dataclass(frozen=True, slots=True)
class CumulativeReturnSeries:
    name: str
    points: tuple[tuple[ChartTimePoint, float], ...]
    color: str


def cumulative_return_series_from_points(
    name: str,
    points: tuple[DailyStrategyPoint, ...],
    *,
    color: str | None = None,
    use_underlying: bool = False,
    include_start_anchor: bool = True,
    x_values: tuple[ChartTimePoint, ...] | None = None,
) -> CumulativeReturnSeries:
    """Convert evaluated points to a cumulative-return chart series."""
    if not points:
        raise EvaluationChartError("points must not be empty.")
    if x_values is not None and len(x_values) != len(points):
        raise EvaluationChartError("x_values must align 1:1 with points.")
    selected_color = color or _DEFAULT_COLORS[0]
    plotted_points = tuple(
        (
            x_values[index] if x_values is not None else point.date,
            (
                point.cumulative_underlying_equity
                if use_underlying
                else point.cumulative_strategy_equity
            )
            - 1.0,
        )
        for index, point in enumerate(points)
    )
    if include_start_anchor:
        plotted_points = ((plotted_points[0][0], 0.0),) + plotted_points
    return CumulativeReturnSeries(
        name=_validate_name(name),
        points=plotted_points,
        color=_validate_color(selected_color),
    )


def render_cumulative_return_svg(
    series: tuple[CumulativeReturnSeries, ...],
    *,
    title: str,
    width: int = 540,
    height: int = 250,
    show_legend: bool = True,
) -> str:
    """Render a compact multi-series cumulative return chart as SVG."""
    if not series:
        raise EvaluationChartError("series must not be empty.")
    if width < 360 or height < 220:
        raise EvaluationChartError("chart width/height are too small.")
    title = _validate_name(title)
    normalized_series = _normalize_series(series)

    left = 56
    right = 18
    top = 26
    bottom = 24
    plot_width = width - left - right
    plot_height = height - top - bottom
    min_date, max_date = _date_range(normalized_series)
    min_value, max_value = _value_range(normalized_series)
    if min_value > 0.0:
        min_value = 0.0
    if max_value < 0.0:
        max_value = 0.0
    min_value, max_value = _padded_range(min_value, max_value)

    y_ticks = _nice_ticks(min_value, max_value)
    paths = tuple(
        _series_path(
            item,
            min_date=min_date,
            max_date=max_date,
            min_value=min_value,
            max_value=max_value,
            left=left,
            top=top,
            plot_width=plot_width,
            plot_height=plot_height,
        )
        for item in normalized_series
    )

    x_labels = _x_labels(min_date, max_date)
    y_axis = "".join(
        (
            f'<line x1="{left}" y1="{_scale_y(value, min_value, max_value, top, plot_height):.2f}" '
            f'x2="{left + plot_width}" '
            f'y2="{_scale_y(value, min_value, max_value, top, plot_height):.2f}" '
            'stroke="#e6e6e6" stroke-width="1"/>'
            f'<text x="{left - 8}" '
            f'y="{_scale_y(value, min_value, max_value, top, plot_height) + 4:.2f}" '
            'font-family="Arial" font-size="12" text-anchor="end" fill="#111111">'
            f'{value * 100.0:.0f}</text>'
        )
        for value in y_ticks
    )
    x_axis = "".join(
        (
            f'<text x="{_scale_x(label_date, min_date, max_date, left, plot_width):.2f}" '
            f'y="{height - 6}" font-family="Arial" font-size="12" '
            'text-anchor="middle" fill="#111111">'
            f'{escape(label)}</text>'
        )
        for label_date, label in x_labels
    )
    line_paths = "".join(
        f'<path d="{path}" fill="none" stroke="{item.color}" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
        for item, path in zip(normalized_series, paths, strict=True)
    )
    legend = _legend(normalized_series, left=left + 8, top=top + 8) if show_legend else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">'
        f'<title>{escape(title)}</title>'
        '<desc>Cumulative return comparison chart.</desc>'
        '<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<text x="{width / 2:.2f}" y="18" font-family="Arial" font-size="18" '
        f'font-weight="700" text-anchor="middle" fill="#111111">{escape(title)}</text>'
        f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" '
        'fill="none" stroke="#8f8f8f" stroke-width="1.2"/>'
        f'{y_axis}'
        f'{line_paths}'
        f'<text x="16" y="{top + plot_height / 2:.2f}" font-family="Arial" '
        'font-size="13" text-anchor="middle" fill="#111111" '
        'transform="rotate(-90 16 '
        f'{top + plot_height / 2:.2f})">Cumulative Return (%)</text>'
        f'{x_axis}'
        f'{legend}'
        '</svg>'
    )


def _normalize_series(
    series: tuple[CumulativeReturnSeries, ...],
) -> tuple[CumulativeReturnSeries, ...]:
    normalized: list[CumulativeReturnSeries] = []
    for index, item in enumerate(series):
        if not isinstance(item, CumulativeReturnSeries):
            raise EvaluationChartError("series must contain CumulativeReturnSeries values.")
        if not item.points:
            raise EvaluationChartError("series points must not be empty.")
        ordered = tuple(sorted(item.points, key=lambda point: point[0]))
        for point_date, point_value in ordered:
            if not isinstance(point_date, date):
                raise EvaluationChartError("series point dates must be date values.")
            if not isinstance(point_value, int | float):
                raise EvaluationChartError("series point values must be numeric.")
        normalized.append(
            CumulativeReturnSeries(
                name=_validate_name(item.name),
                points=tuple(
                    (point_date, float(point_value)) for point_date, point_value in ordered
                ),
                color=_validate_color(item.color or _DEFAULT_COLORS[index % len(_DEFAULT_COLORS)]),
            )
        )
    return tuple(normalized)


def _date_range(series: tuple[CumulativeReturnSeries, ...]) -> tuple[ChartTimePoint, ChartTimePoint]:
    dates = tuple(point_date for item in series for point_date, _ in item.points)
    ordered = sorted(dates, key=_time_scalar)
    return ordered[0], ordered[-1]


def _value_range(series: tuple[CumulativeReturnSeries, ...]) -> tuple[float, float]:
    values = tuple(point_value for item in series for _, point_value in item.points)
    return min(values), max(values)


def _padded_range(min_value: float, max_value: float) -> tuple[float, float]:
    if min_value == max_value:
        spread = max(abs(min_value), 0.01)
        return min_value - spread, max_value + spread
    padding = (max_value - min_value) * 0.08
    return min_value - padding, max_value + padding


def _nice_ticks(min_value: float, max_value: float) -> tuple[float, float, float]:
    return (min_value, 0.0, max_value)


def _series_path(
    series: CumulativeReturnSeries,
    *,
    min_date: ChartTimePoint,
    max_date: ChartTimePoint,
    min_value: float,
    max_value: float,
    left: int,
    top: int,
    plot_width: int,
    plot_height: int,
) -> str:
    commands: list[str] = []
    for index, (point_date, value) in enumerate(series.points):
        x = _scale_x(point_date, min_date, max_date, left, plot_width)
        y = _scale_y(value, min_value, max_value, top, plot_height)
        command = "M" if index == 0 else "L"
        commands.append(f"{command}{x:.2f},{y:.2f}")
    return " ".join(commands)


def _scale_x(
    point_date: ChartTimePoint,
    min_date: ChartTimePoint,
    max_date: ChartTimePoint,
    left: int,
    width: int,
) -> float:
    min_scalar = _time_scalar(min_date)
    max_scalar = _time_scalar(max_date)
    point_scalar = _time_scalar(point_date)
    span = max(max_scalar - min_scalar, 1.0)
    return left + (point_scalar - min_scalar) / span * width


def _scale_y(value: float, min_value: float, max_value: float, top: int, height: int) -> float:
    span = max_value - min_value
    if span == 0.0:
        return top + height / 2.0
    return top + (max_value - value) / span * height


def _x_labels(
    min_date: ChartTimePoint,
    max_date: ChartTimePoint,
) -> tuple[tuple[ChartTimePoint, str], ...]:
    if _time_scalar(min_date) == _time_scalar(max_date):
        return ((min_date, _format_time_label(min_date)),)
    midpoint = _midpoint(min_date, max_date)
    return (
        (min_date, _format_time_label(min_date)),
        (midpoint, _format_time_label(midpoint)),
        (max_date, _format_time_label(max_date)),
    )


def _legend(series: tuple[CumulativeReturnSeries, ...], *, left: int, top: int) -> str:
    items: list[str] = []
    for index, item in enumerate(series):
        y = top + index * 16
        items.append(
            f'<line x1="{left}" y1="{y}" x2="{left + 16}" y2="{y}" '
            f'stroke="{item.color}" stroke-width="2"/>'
            f'<text x="{left + 22}" y="{y + 4}" font-family="Arial" font-size="11" '
            f'fill="#111111">{escape(item.name)}</text>'
        )
    return "".join(items)


def _validate_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationChartError("name must be a non-blank string.")
    return value.strip()


def _validate_color(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationChartError("color must be a non-blank string.")
    return value.strip()


def _time_scalar(value: ChartTimePoint) -> float:
    if isinstance(value, datetime):
        normalized = value.astimezone(UTC) if value.tzinfo is not None else value
        midnight = datetime(
            normalized.year,
            normalized.month,
            normalized.day,
            tzinfo=normalized.tzinfo,
        )
        return normalized.toordinal() + (normalized - midnight).total_seconds() / 86400.0
    return float(value.toordinal())


def _midpoint(start: ChartTimePoint, end: ChartTimePoint) -> ChartTimePoint:
    if isinstance(start, datetime) or isinstance(end, datetime):
        start_dt = _as_datetime(start)
        end_dt = _as_datetime(end)
        return start_dt + (end_dt - start_dt) / 2
    return date.fromordinal((start.toordinal() + end.toordinal()) // 2)


def _as_datetime(value: ChartTimePoint) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo is not None else value
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _format_time_label(value: ChartTimePoint) -> str:
    if isinstance(value, datetime):
        normalized = value.astimezone(UTC) if value.tzinfo is not None else value
        return normalized.strftime("%m-%d %H:%M")
    return value.isoformat()
