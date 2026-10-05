"""Daily rule-based baseline calculations over market bars."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from statistics import mean, pstdev
from typing import Literal

from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    ViewState,
    ViewStateChangeContractError,
)

BaselineDirectionMode = Literal["long_only", "long_short"]


class BaselineEvaluationError(ValueError):
    """Raised when baseline inputs or parameters are invalid."""


@dataclass(frozen=True, slots=True)
class DailyStrategyPoint:
    date: date
    state: ViewState
    target_weight: float
    open_price: float
    close_price: float
    underlying_return: float
    strategy_return: float
    cumulative_underlying_equity: float
    cumulative_strategy_equity: float


def daily_bars_from_intraday(market_data: MarketDataSeries) -> MarketDataSeries:
    """Aggregate replay intraday bars to daily OHLCV bars using available bars only."""
    if not isinstance(market_data, MarketDataSeries):
        raise BaselineEvaluationError("market_data must be a MarketDataSeries.")
    grouped: dict[date, list[MarketDataBar]] = defaultdict(list)
    for bar in market_data.bars:
        grouped[bar.start_at.date()].append(bar)
    daily_bars: list[MarketDataBar] = []
    for _, bars in sorted(grouped.items(), key=lambda item: item[0]):
        ordered = tuple(sorted(bars, key=lambda item: item.start_at))
        volume = sum(item.volume for item in ordered)
        vwap = None
        if volume > 0.0 and all(item.vwap is not None for item in ordered):
            vwap = (
                sum(item.vwap * item.volume for item in ordered if item.vwap is not None)
                / volume
            )
        daily_bars.append(
            MarketDataBar(
                start_at=ordered[0].start_at,
                end_at=ordered[-1].end_at,
                open_price=ordered[0].open_price,
                high_price=max(item.high_price for item in ordered),
                low_price=min(item.low_price for item in ordered),
                close_price=ordered[-1].close_price,
                volume=volume,
                vwap=vwap,
            )
        )
    try:
        return MarketDataSeries(bars=tuple(daily_bars))
    except ViewStateChangeContractError as exc:
        raise BaselineEvaluationError(f"invalid aggregated daily bars: {exc}") from exc


def calculate_buy_and_hold_baseline(
    market_data: MarketDataSeries,
    *,
    start_date: date | None = None,
    entry_buy_cost_bps: float = 0.0,
) -> tuple[DailyStrategyPoint, ...]:
    """Calculate a full-weight long baseline from the first available daily open."""
    points = _simulate_daily_baseline(
        _daily_bars(market_data),
        desired_weight_by_index=lambda _index: 1.0,
        initial_pending_weight=1.0,
        start_date=start_date,
    )
    return _apply_entry_buy_cost(points, entry_buy_cost_bps=entry_buy_cost_bps)


def calculate_sma_crossover_baseline(
    market_data: MarketDataSeries,
    *,
    fast_periods: int = 50,
    slow_periods: int = 200,
    direction_mode: BaselineDirectionMode = "long_short",
    start_date: date | None = None,
) -> tuple[DailyStrategyPoint, ...]:
    bars = _daily_bars(market_data)
    _validate_direction_mode(direction_mode)
    if fast_periods <= 0 or slow_periods <= 0:
        raise BaselineEvaluationError("SMA periods must be positive.")
    if fast_periods >= slow_periods:
        raise BaselineEvaluationError("fast_periods must be smaller than slow_periods.")
    closes = tuple(bar.close_price for bar in bars)

    def desired(index: int) -> float:
        if index + 1 < slow_periods:
            return 0.0
        fast = mean(closes[index + 1 - fast_periods : index + 1])
        slow = mean(closes[index + 1 - slow_periods : index + 1])
        if fast > slow:
            return 1.0
        if fast < slow:
            return _short_weight(direction_mode)
        return 0.0

    return _simulate_daily_baseline(
        bars,
        desired_weight_by_index=desired,
        start_date=start_date,
    )


def calculate_ema_crossover_baseline(
    market_data: MarketDataSeries,
    *,
    fast_periods: int = 20,
    slow_periods: int = 50,
    direction_mode: BaselineDirectionMode = "long_short",
    start_date: date | None = None,
) -> tuple[DailyStrategyPoint, ...]:
    bars = _daily_bars(market_data)
    _validate_direction_mode(direction_mode)
    if fast_periods <= 0 or slow_periods <= 0:
        raise BaselineEvaluationError("EMA periods must be positive.")
    if fast_periods >= slow_periods:
        raise BaselineEvaluationError("fast_periods must be smaller than slow_periods.")
    closes = tuple(bar.close_price for bar in bars)
    fast_ema = _ema_series(closes, fast_periods)
    slow_ema = _ema_series(closes, slow_periods)

    def desired(index: int) -> float:
        fast = fast_ema[index]
        slow = slow_ema[index]
        if fast is None or slow is None:
            return 0.0
        if fast > slow:
            return 1.0
        if fast < slow:
            return _short_weight(direction_mode)
        return 0.0

    return _simulate_daily_baseline(
        bars,
        desired_weight_by_index=desired,
        start_date=start_date,
    )


def calculate_macd_baseline(
    market_data: MarketDataSeries,
    *,
    fast_periods: int = 12,
    slow_periods: int = 26,
    signal_periods: int = 9,
    direction_mode: BaselineDirectionMode = "long_short",
    start_date: date | None = None,
) -> tuple[DailyStrategyPoint, ...]:
    bars = _daily_bars(market_data)
    _validate_direction_mode(direction_mode)
    if min(fast_periods, slow_periods, signal_periods) <= 0:
        raise BaselineEvaluationError("MACD periods must be positive.")
    if fast_periods >= slow_periods:
        raise BaselineEvaluationError("fast_periods must be smaller than slow_periods.")
    closes = tuple(bar.close_price for bar in bars)
    fast_ema = _ema_series(closes, fast_periods)
    slow_ema = _ema_series(closes, slow_periods)
    macd = tuple(
        None if fast is None or slow is None else fast - slow
        for fast, slow in zip(fast_ema, slow_ema, strict=True)
    )
    signal = _ema_series(macd, signal_periods)

    def desired(index: int) -> float:
        macd_value = macd[index]
        signal_value = signal[index]
        if macd_value is None or signal_value is None:
            return 0.0
        if macd_value > signal_value:
            return 1.0
        if macd_value < signal_value:
            return _short_weight(direction_mode)
        return 0.0

    return _simulate_daily_baseline(
        bars,
        desired_weight_by_index=desired,
        start_date=start_date,
    )


def calculate_rsi_baseline(
    market_data: MarketDataSeries,
    *,
    periods: int = 14,
    oversold: float = 30.0,
    overbought: float = 70.0,
    direction_mode: BaselineDirectionMode = "long_short",
    start_date: date | None = None,
) -> tuple[DailyStrategyPoint, ...]:
    bars = _daily_bars(market_data)
    _validate_direction_mode(direction_mode)
    if periods <= 0:
        raise BaselineEvaluationError("RSI periods must be positive.")
    if not (0.0 < oversold < overbought < 100.0):
        raise BaselineEvaluationError(
            "RSI thresholds must satisfy 0 < oversold < overbought < 100."
        )
    rsi = _rsi_series(tuple(bar.close_price for bar in bars), periods)

    def desired(index: int) -> float:
        value = rsi[index]
        if value is None:
            return 0.0
        if value < oversold:
            return 1.0
        if value > overbought:
            return _short_weight(direction_mode)
        return 0.0

    return _simulate_daily_baseline(
        bars,
        desired_weight_by_index=desired,
        start_date=start_date,
    )


def calculate_bollinger_bands_baseline(
    market_data: MarketDataSeries,
    *,
    periods: int = 20,
    stddev_multiplier: float = 2.0,
    direction_mode: BaselineDirectionMode = "long_short",
    start_date: date | None = None,
) -> tuple[DailyStrategyPoint, ...]:
    bars = _daily_bars(market_data)
    _validate_direction_mode(direction_mode)
    if periods <= 1:
        raise BaselineEvaluationError("Bollinger periods must be greater than one.")
    if stddev_multiplier <= 0.0:
        raise BaselineEvaluationError("stddev_multiplier must be positive.")
    closes = tuple(bar.close_price for bar in bars)

    def desired(index: int) -> float:
        if index + 1 < periods:
            return 0.0
        window = closes[index + 1 - periods : index + 1]
        middle = mean(window)
        band = pstdev(window) * stddev_multiplier
        lower = middle - band
        upper = middle + band
        close = closes[index]
        if close < lower:
            return 1.0
        if close > upper:
            return _short_weight(direction_mode)
        return 0.0

    return _simulate_daily_baseline(
        bars,
        desired_weight_by_index=desired,
        start_date=start_date,
    )


def _daily_bars(market_data: MarketDataSeries) -> tuple[MarketDataBar, ...]:
    if not isinstance(market_data, MarketDataSeries):
        raise BaselineEvaluationError("market_data must be a MarketDataSeries.")
    bars = tuple(sorted(market_data.bars, key=lambda item: item.start_at))
    if not bars:
        raise BaselineEvaluationError("market_data must contain at least one bar.")
    return bars


def _simulate_daily_baseline(
    bars: tuple[MarketDataBar, ...],
    *,
    desired_weight_by_index: Callable[[int], float],
    initial_pending_weight: float = 0.0,
    start_date: date | None = None,
) -> tuple[DailyStrategyPoint, ...]:
    start_index = _start_index(bars, start_date=start_date)
    active_weight = 0.0
    pending_weight = (
        initial_pending_weight
        if start_index == 0
        else desired_weight_by_index(start_index - 1)
    )
    previous_price: float | None = None
    underlying_equity = 1.0
    strategy_equity = 1.0
    points: list[DailyStrategyPoint] = []

    for index, bar in enumerate(bars[start_index:], start=start_index):
        day_start_equity = strategy_equity
        day_start_underlying_equity = underlying_equity
        if previous_price is not None:
            overnight_return = bar.open_price / previous_price - 1.0
            underlying_equity *= 1.0 + overnight_return
            strategy_equity *= 1.0 + active_weight * overnight_return

        active_weight = pending_weight
        intraday_return = bar.close_price / bar.open_price - 1.0
        underlying_equity *= 1.0 + intraday_return
        strategy_equity *= 1.0 + active_weight * intraday_return

        points.append(
            DailyStrategyPoint(
                date=bar.start_at.date(),
                state=_state_from_weight(active_weight),
                target_weight=active_weight,
                open_price=bar.open_price,
                close_price=bar.close_price,
                underlying_return=underlying_equity / day_start_underlying_equity - 1.0,
                strategy_return=strategy_equity / day_start_equity - 1.0,
                cumulative_underlying_equity=underlying_equity,
                cumulative_strategy_equity=strategy_equity,
            )
        )
        pending_weight = desired_weight_by_index(index)
        previous_price = bar.close_price

    return tuple(points)


def _start_index(bars: tuple[MarketDataBar, ...], *, start_date: date | None) -> int:
    if start_date is None:
        return 0
    if not isinstance(start_date, date):
        raise BaselineEvaluationError("start_date must be a date.")
    for index, bar in enumerate(bars):
        if bar.start_at.date() >= start_date:
            return index
    raise BaselineEvaluationError("start_date is after the last available market bar.")


def _apply_entry_buy_cost(
    points: tuple[DailyStrategyPoint, ...],
    *,
    entry_buy_cost_bps: float,
) -> tuple[DailyStrategyPoint, ...]:
    if entry_buy_cost_bps == 0.0:
        return points
    if entry_buy_cost_bps < 0.0:
        raise BaselineEvaluationError("entry_buy_cost_bps must be non-negative.")
    entry_cost_multiplier = 1.0 + entry_buy_cost_bps / 10_000.0
    prior_equity = 1.0
    adjusted_points: list[DailyStrategyPoint] = []
    for point in points:
        cumulative_strategy_equity = point.cumulative_strategy_equity / entry_cost_multiplier
        adjusted_points.append(
            replace(
                point,
                strategy_return=cumulative_strategy_equity / prior_equity - 1.0,
                cumulative_strategy_equity=cumulative_strategy_equity,
            )
        )
        prior_equity = cumulative_strategy_equity
    return tuple(adjusted_points)


def _ema_series(values: tuple[float | None, ...], periods: int) -> tuple[float | None, ...]:
    alpha = 2.0 / (periods + 1.0)
    output: list[float | None] = []
    seed: deque[float] = deque(maxlen=periods)
    current: float | None = None
    for value in values:
        if value is None:
            output.append(None)
            continue
        if current is None:
            seed.append(value)
            if len(seed) < periods:
                output.append(None)
                continue
            current = mean(seed)
            output.append(current)
            continue
        current = value * alpha + current * (1.0 - alpha)
        output.append(current)
    return tuple(output)


def _rsi_series(closes: tuple[float, ...], periods: int) -> tuple[float | None, ...]:
    if len(closes) < 2:
        return tuple(None for _ in closes)
    output: list[float | None] = [None]
    gains: list[float] = []
    losses: list[float] = []
    average_gain: float | None = None
    average_loss: float | None = None
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        if average_gain is None or average_loss is None:
            gains.append(gain)
            losses.append(loss)
            if len(gains) < periods:
                output.append(None)
                continue
            average_gain = mean(gains)
            average_loss = mean(losses)
        else:
            average_gain = (average_gain * (periods - 1) + gain) / periods
            average_loss = (average_loss * (periods - 1) + loss) / periods
        if average_loss == 0.0:
            output.append(100.0)
        else:
            relative_strength = average_gain / average_loss
            output.append(100.0 - 100.0 / (1.0 + relative_strength))
    return tuple(output)


def _short_weight(direction_mode: BaselineDirectionMode) -> float:
    return 0.0 if direction_mode == "long_only" else -1.0


def _validate_direction_mode(direction_mode: BaselineDirectionMode) -> None:
    if direction_mode not in {"long_only", "long_short"}:
        raise BaselineEvaluationError("direction_mode must be long_only or long_short.")


def _state_from_weight(weight: float) -> ViewState:
    if weight > 0.0:
        return "strong_long"
    if weight < 0.0:
        return "strong_short"
    return "flat"
