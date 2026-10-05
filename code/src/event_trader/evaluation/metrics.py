"""Pure performance metric calculations over periodic strategy returns."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import mean, stdev


class EvaluationMetricError(ValueError):
    """Raised when metric inputs are invalid."""


@dataclass(frozen=True, slots=True)
class MetricSummary:
    cumulative_return: float
    annualized_return: float
    volatility: float
    sharpe: float | None
    max_drawdown: float
    calmar: float | None
    annual_periods: float
    period_count: int


@dataclass(frozen=True, slots=True)
class AlphaBeta:
    alpha: float
    annualized_alpha: float
    beta: float
    observation_count: int


def calculate_metric_summary(
    returns: tuple[float, ...],
    *,
    annual_periods: float,
    risk_free_return_per_period: float = 0.0,
) -> MetricSummary:
    """Calculate the core comparison metrics for one periodic return stream."""
    validated_returns = _validate_returns(returns)
    if annual_periods <= 0.0:
        raise EvaluationMetricError("annual_periods must be greater than zero.")
    if not validated_returns:
        raise EvaluationMetricError("returns must not be empty.")

    total_return = cumulative_return(validated_returns)
    annualized = (1.0 + total_return) ** (annual_periods / len(validated_returns)) - 1.0
    vol = volatility(validated_returns, annual_periods=annual_periods)
    sharpe_value = sharpe(
        validated_returns,
        annual_periods=annual_periods,
        risk_free_return_per_period=risk_free_return_per_period,
    )
    drawdown = max_drawdown_from_returns(validated_returns)
    calmar = None if drawdown == 0.0 else annualized / drawdown
    return MetricSummary(
        cumulative_return=total_return,
        annualized_return=annualized,
        volatility=vol,
        sharpe=sharpe_value,
        max_drawdown=drawdown,
        calmar=calmar,
        annual_periods=annual_periods,
        period_count=len(validated_returns),
    )


def cumulative_return(returns: tuple[float, ...]) -> float:
    equity = 1.0
    for item in _validate_returns(returns):
        equity *= 1.0 + item
    return equity - 1.0


def volatility(returns: tuple[float, ...], *, annual_periods: float) -> float:
    validated_returns = _validate_returns(returns)
    if annual_periods <= 0.0:
        raise EvaluationMetricError("annual_periods must be greater than zero.")
    if len(validated_returns) < 2:
        return 0.0
    return stdev(validated_returns) * sqrt(annual_periods)


def sharpe(
    returns: tuple[float, ...],
    *,
    annual_periods: float,
    risk_free_return_per_period: float = 0.0,
) -> float | None:
    validated_returns = _validate_returns(returns)
    if annual_periods <= 0.0:
        raise EvaluationMetricError("annual_periods must be greater than zero.")
    if len(validated_returns) < 2:
        return None
    excess = tuple(item - risk_free_return_per_period for item in validated_returns)
    excess_stdev = stdev(excess)
    if excess_stdev == 0.0:
        return None
    return mean(excess) / excess_stdev * sqrt(annual_periods)


def max_drawdown(equity_curve: tuple[float, ...]) -> float:
    if not equity_curve:
        raise EvaluationMetricError("equity_curve must not be empty.")
    peak = equity_curve[0]
    if peak <= 0.0:
        raise EvaluationMetricError("equity values must be positive.")
    worst = 0.0
    for equity in equity_curve:
        if equity <= 0.0:
            raise EvaluationMetricError("equity values must be positive.")
        peak = max(peak, equity)
        worst = max(worst, 1.0 - equity / peak)
    return worst


def max_drawdown_from_returns(returns: tuple[float, ...]) -> float:
    equity = 1.0
    curve = [equity]
    for item in _validate_returns(returns):
        equity *= 1.0 + item
        curve.append(equity)
    return max_drawdown(tuple(curve))


def calculate_alpha_beta(
    strategy_returns: tuple[float, ...],
    benchmark_returns: tuple[float, ...],
    *,
    annual_periods: float,
    risk_free_return_per_period: float = 0.0,
) -> AlphaBeta:
    """Calculate single-factor alpha and beta over already-aligned return series."""
    strategy = _validate_returns(strategy_returns)
    benchmark = _validate_returns(benchmark_returns)
    if annual_periods <= 0.0:
        raise EvaluationMetricError("annual_periods must be greater than zero.")
    if len(strategy) != len(benchmark):
        raise EvaluationMetricError("strategy and benchmark returns must have equal length.")
    if len(strategy) < 2:
        raise EvaluationMetricError("at least two aligned observations are required.")

    strategy_excess = tuple(item - risk_free_return_per_period for item in strategy)
    benchmark_excess = tuple(item - risk_free_return_per_period for item in benchmark)
    benchmark_mean = mean(benchmark_excess)
    strategy_mean = mean(strategy_excess)
    variance = sum((item - benchmark_mean) ** 2 for item in benchmark_excess)
    if variance == 0.0:
        raise EvaluationMetricError("benchmark returns must have non-zero variance.")
    covariance = sum(
        (strategy_item - strategy_mean) * (benchmark_item - benchmark_mean)
        for strategy_item, benchmark_item in zip(strategy_excess, benchmark_excess, strict=True)
    )
    beta = covariance / variance
    alpha = strategy_mean - beta * benchmark_mean
    annualized_alpha = (1.0 + alpha) ** annual_periods - 1.0
    return AlphaBeta(
        alpha=alpha,
        annualized_alpha=annualized_alpha,
        beta=beta,
        observation_count=len(strategy),
    )


def _validate_returns(returns: tuple[float, ...]) -> tuple[float, ...]:
    if isinstance(returns, list):
        returns = tuple(returns)
    if not isinstance(returns, tuple):
        raise EvaluationMetricError("returns must be a tuple of floats.")
    for item in returns:
        if not isinstance(item, int | float):
            raise EvaluationMetricError("returns must contain only numeric values.")
        if item <= -1.0:
            raise EvaluationMetricError("period returns must be greater than -100%.")
    return tuple(float(item) for item in returns)
