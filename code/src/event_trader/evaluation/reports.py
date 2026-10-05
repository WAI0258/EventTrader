"""Markdown performance reports for replay evaluation results."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .baselines import DailyStrategyPoint
from .metrics import MetricSummary, calculate_metric_summary


@dataclass(frozen=True, slots=True)
class MetricReportRow:
    name: str
    summary: MetricSummary


def build_metric_report_rows(
    series: Sequence[tuple[str, Sequence[DailyStrategyPoint]]],
    *,
    annual_periods: float,
) -> tuple[MetricReportRow, ...]:
    rows: list[MetricReportRow] = []
    for name, points in series:
        returns = tuple(point.strategy_return for point in points)
        rows.append(
            MetricReportRow(
                name=name,
                summary=calculate_metric_summary(returns, annual_periods=annual_periods),
            )
        )
    return tuple(rows)


def render_metric_report_markdown(
    rows: Sequence[MetricReportRow],
    *,
    title: str,
) -> str:
    lines = [
        f"# {title}",
        "",
        "| Model | CR (%) | Sharpe | Calmar | Volatility (%) | MDD (%) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        summary = row.summary
        lines.append(
            "| "
            f"{row.name} | "
            f"{_percent(summary.cumulative_return)} | "
            f"{_number(summary.sharpe)} | "
            f"{_number(summary.calmar)} | "
            f"{_percent(summary.volatility)} | "
            f"{_percent(summary.max_drawdown)} |"
        )
    lines.extend(
        [
            "",
            "Returns are measured from the replay first execution date. "
            "Volatility, Sharpe, and Calmar are annualized from daily returns.",
            "",
        ]
    )
    return "\n".join(lines)


def _percent(value: float) -> str:
    return f"{value * 100.0:.4f}"


def _number(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"
