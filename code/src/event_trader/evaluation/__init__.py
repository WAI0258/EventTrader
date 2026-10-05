"""Replay evaluation helpers for comparing executed views with simple baselines."""

from .baselines import (
    BaselineDirectionMode,
    BaselineEvaluationError,
    DailyStrategyPoint,
    calculate_bollinger_bands_baseline,
    calculate_buy_and_hold_baseline,
    calculate_ema_crossover_baseline,
    calculate_macd_baseline,
    calculate_rsi_baseline,
    calculate_sma_crossover_baseline,
    daily_bars_from_intraday,
)
from .catalog import (
    RuleBaselineCatalogError,
    RuleBaselineParameters,
    RuleBaselineSpec,
    RuleBaselineSeries,
    available_rule_baseline_ids,
    baseline_parameter_payload_for_id,
    build_rule_baseline_series,
)
from .charts import (
    CumulativeReturnSeries,
    EvaluationChartError,
    cumulative_return_series_from_points,
    render_cumulative_return_svg,
)
from .metrics import (
    AlphaBeta,
    MetricSummary,
    calculate_alpha_beta,
    calculate_metric_summary,
    cumulative_return,
    max_drawdown,
)
from .replay_performance import (
    ReplayPerformanceError,
    calculate_replay_daily_performance,
)
from .reports import (
    MetricReportRow,
    build_metric_report_rows,
    render_metric_report_markdown,
)

__all__ = [
    "AlphaBeta",
    "BaselineDirectionMode",
    "BaselineEvaluationError",
    "CumulativeReturnSeries",
    "DailyStrategyPoint",
    "EvaluationChartError",
    "MetricSummary",
    "MetricReportRow",
    "ReplayPerformanceError",
    "RuleBaselineCatalogError",
    "RuleBaselineParameters",
    "RuleBaselineSpec",
    "RuleBaselineSeries",
    "available_rule_baseline_ids",
    "baseline_parameter_payload_for_id",
    "build_rule_baseline_series",
    "calculate_alpha_beta",
    "calculate_bollinger_bands_baseline",
    "calculate_buy_and_hold_baseline",
    "calculate_ema_crossover_baseline",
    "calculate_macd_baseline",
    "calculate_metric_summary",
    "calculate_replay_daily_performance",
    "calculate_rsi_baseline",
    "calculate_sma_crossover_baseline",
    "cumulative_return",
    "cumulative_return_series_from_points",
    "daily_bars_from_intraday",
    "build_metric_report_rows",
    "max_drawdown",
    "render_metric_report_markdown",
    "render_cumulative_return_svg",
]
