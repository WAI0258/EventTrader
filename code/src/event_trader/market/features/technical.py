"""Deterministic technical market-context features."""

from __future__ import annotations

from math import sqrt

from event_trader.contracts.view_state_change import MarketDataBar
from event_trader.market.contracts import (
    BollingerBandsFeature,
    EmaFeature,
    IchimokuFeature,
    MarketContextComponentStatus,
    RsiFeature,
    TechnicalContext,
    TechnicalDivergenceFeature,
)

_TECHNICAL_VERSION = "technical_completed_bars_indicator_panel_2026_05"
_ICHIMOKU_VERSION = "standard_ichimoku_9_26_52_displacement_26_2026_05"
_EMA_VERSION = "ema_direct_completed_bars_2026_05"
_BOLLINGER_VERSION = "bollinger_completed_closes_2026_05"
_RSI_VERSION = "rsi_wilder_completed_closes_2026_05"
_DIVERGENCE_VERSION = "technical_divergence_pivots_2026_05"


def build_technical_context(
    bars: tuple[MarketDataBar, ...],
    *,
    ema_periods: tuple[int, ...],
    ichimoku_enabled: bool,
    bollinger_period: int = 20,
    bollinger_stddev: float = 2.0,
    rsi_period: int = 14,
    rsi_overbought: float = 70.0,
    rsi_oversold: float = 30.0,
    divergence_lookback_bars: int = 50,
    divergence_pivot_window: int = 2,
) -> TechnicalContext:
    """Build deterministic technical context from visible completed bars."""
    ema_136 = _build_ema_feature(bars, period=136) if 136 in ema_periods else None
    bollinger = _build_bollinger_feature(
        bars,
        period=bollinger_period,
        stddev_multiplier=bollinger_stddev,
    )
    rsi = _build_rsi_feature(
        bars,
        period=rsi_period,
        overbought=rsi_overbought,
        oversold=rsi_oversold,
    )
    divergence = _build_divergence_feature(
        bars,
        rsi_period=rsi_period,
        lookback_bars=divergence_lookback_bars,
        pivot_window=divergence_pivot_window,
    )
    latest_close = bars[-1].close_price if bars else None
    price_vs_ema_136 = None
    if latest_close is not None and ema_136 is not None and ema_136.value is not None:
        price_vs_ema_136 = latest_close / ema_136.value - 1.0

    ichimoku = (
        _build_standard_ichimoku(bars)
        if ichimoku_enabled
        else IchimokuFeature(
            tenkan=None,
            kijun=None,
            senkou_a_known_at_as_of=None,
            senkou_b_known_at_as_of=None,
            cloud_regime="unknown",
            price_location="unknown",
            input_bar_count=len(bars),
            status="unavailable",
        )
    )
    unavailable: list[str] = []
    if ema_136 is None:
        unavailable.extend(("ema_136", "price_vs_ema_136", "ema_136_slope"))
    elif ema_136.status != "available":
        unavailable.extend(("ema_136", "price_vs_ema_136", "ema_136_slope"))
    if ichimoku.status != "available":
        unavailable.extend(
            (
                "ichimoku_tenkan",
                "ichimoku_kijun",
                "ichimoku_senkou_a_known_at_as_of",
                "ichimoku_senkou_b_known_at_as_of",
                "ichimoku_cloud_regime",
                "ichimoku_price_location",
            )
        )
    if bollinger.status != "available":
        unavailable.append("bollinger_bands")
    if rsi.status != "available":
        unavailable.append("rsi")
    if divergence.status != "available":
        unavailable.append("technical_divergence")
    if not bars:
        status_value = "unavailable"
        reason = "no completed bars visible at as_of_at"
    elif unavailable:
        status_value = "partial"
        reason = "insufficient visible bars for some technical features"
    else:
        status_value = "available"
        reason = None

    return TechnicalContext(
        ema_136=ema_136,
        price_vs_ema_136=price_vs_ema_136,
        ema_136_slope=ema_136.slope if ema_136 is not None else None,
        ichimoku_tenkan=ichimoku.tenkan,
        ichimoku_kijun=ichimoku.kijun,
        ichimoku_senkou_a_known_at_as_of=ichimoku.senkou_a_known_at_as_of,
        ichimoku_senkou_b_known_at_as_of=ichimoku.senkou_b_known_at_as_of,
        ichimoku_cloud_regime=ichimoku.cloud_regime,
        ichimoku_price_location=ichimoku.price_location,
        bollinger_bands=bollinger,
        rsi=rsi,
        divergence=divergence,
        technical_trend_label=_technical_trend_label(
            price_vs_ema_136=price_vs_ema_136,
            ema_slope=ema_136.slope if ema_136 is not None else None,
            ichimoku_price_location=ichimoku.price_location,
        ),
        technical_context_status=MarketContextComponentStatus(
            component="technical",
            status=status_value,
            reason=reason,
            unavailable_fields=tuple(sorted(unavailable)),
        ),
        indicators={
            "ema": {
                "calculation_version": _EMA_VERSION,
                "configured_periods": list(ema_periods),
            },
            "standard_ichimoku": {
                "calculation_version": _ICHIMOKU_VERSION,
                "enabled": ichimoku_enabled,
                "tenkan": 9,
                "kijun": 26,
                "senkou_b": 52,
                "displacement": 26,
            },
            "bollinger_bands": {
                "calculation_version": _BOLLINGER_VERSION,
                "period": bollinger_period,
                "stddev_multiplier": bollinger_stddev,
            },
            "rsi": {
                "calculation_version": _RSI_VERSION,
                "period": rsi_period,
                "overbought": rsi_overbought,
                "oversold": rsi_oversold,
            },
            "technical_divergence": {
                "calculation_version": _DIVERGENCE_VERSION,
                "lookback_bars": divergence_lookback_bars,
                "pivot_window": divergence_pivot_window,
            },
        },
    )


def technical_calculation_version() -> str:
    return _TECHNICAL_VERSION


def ema_calculation_version() -> str:
    return _EMA_VERSION


def ichimoku_calculation_version() -> str:
    return _ICHIMOKU_VERSION


def bollinger_calculation_version() -> str:
    return _BOLLINGER_VERSION


def rsi_calculation_version() -> str:
    return _RSI_VERSION


def technical_divergence_calculation_version() -> str:
    return _DIVERGENCE_VERSION


def _build_ema_feature(bars: tuple[MarketDataBar, ...], *, period: int) -> EmaFeature:
    values = _ema_values(tuple(bar.close_price for bar in bars), period=period)
    if values is None:
        return EmaFeature(
            period=period,
            value=None,
            slope=None,
            input_bar_count=len(bars),
            status="pending" if bars else "unavailable",
        )
    slope = None
    if len(values) >= 2 and values[-2] != 0:
        slope = values[-1] / values[-2] - 1.0
    return EmaFeature(
        period=period,
        value=values[-1],
        slope=slope,
        input_bar_count=len(bars),
        status="available",
    )


def _ema_values(closes: tuple[float, ...], *, period: int) -> tuple[float, ...] | None:
    if len(closes) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    seed = sum(closes[:period]) / period
    values = [seed]
    ema = seed
    for close in closes[period:]:
        ema = alpha * close + (1.0 - alpha) * ema
        values.append(ema)
    return tuple(values)


def _build_bollinger_feature(
    bars: tuple[MarketDataBar, ...],
    *,
    period: int,
    stddev_multiplier: float,
) -> BollingerBandsFeature:
    if len(bars) < period:
        return BollingerBandsFeature(
            period=period,
            stddev_multiplier=stddev_multiplier,
            middle=None,
            upper=None,
            lower=None,
            bandwidth=None,
            percent_b=None,
            state="unavailable",
            input_bar_count=len(bars),
            status="pending" if bars else "unavailable",
        )
    closes = tuple(bar.close_price for bar in bars[-period:])
    middle = sum(closes) / period
    variance = sum((close - middle) ** 2 for close in closes) / period
    band_width = sqrt(variance) * stddev_multiplier
    upper = middle + band_width
    lower = middle - band_width
    latest_close = closes[-1]
    percent_b = (latest_close - lower) / (upper - lower) if upper != lower else None
    bandwidth = (upper - lower) / middle if middle != 0 else None
    if latest_close > upper:
        state = "above_upper_band"
    elif latest_close < lower:
        state = "below_lower_band"
    else:
        state = "inside_band"
    return BollingerBandsFeature(
        period=period,
        stddev_multiplier=stddev_multiplier,
        middle=middle,
        upper=upper,
        lower=lower,
        bandwidth=bandwidth,
        percent_b=percent_b,
        state=state,
        input_bar_count=len(bars),
        status="available",
    )


def _build_rsi_feature(
    bars: tuple[MarketDataBar, ...],
    *,
    period: int,
    overbought: float,
    oversold: float,
) -> RsiFeature:
    values = _rsi_values(tuple(bar.close_price for bar in bars), period=period)
    if values is None or values[-1] is None:
        return RsiFeature(
            period=period,
            value=None,
            state="unavailable",
            input_bar_count=len(bars),
            status="pending" if bars else "unavailable",
        )
    value = values[-1]
    if value >= overbought:
        state = "overbought"
    elif value <= oversold:
        state = "oversold"
    else:
        state = "neutral"
    return RsiFeature(
        period=period,
        value=value,
        state=state,
        input_bar_count=len(bars),
        status="available",
    )


def _rsi_values(closes: tuple[float, ...], *, period: int) -> tuple[float | None, ...] | None:
    if len(closes) < period + 1:
        return None
    changes = tuple(closes[index] - closes[index - 1] for index in range(1, len(closes)))
    gains = tuple(max(change, 0.0) for change in changes)
    losses = tuple(max(-change, 0.0) for change in changes)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    values: list[float | None] = [None] * period
    values.append(_rsi_from_averages(avg_gain=avg_gain, avg_loss=avg_loss))
    for index in range(period, len(changes)):
        avg_gain = ((avg_gain * (period - 1)) + gains[index]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[index]) / period
        values.append(_rsi_from_averages(avg_gain=avg_gain, avg_loss=avg_loss))
    return tuple(values)


def _rsi_from_averages(*, avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    if avg_gain == 0:
        return 0.0
    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def _build_divergence_feature(
    bars: tuple[MarketDataBar, ...],
    *,
    rsi_period: int,
    lookback_bars: int,
    pivot_window: int,
) -> TechnicalDivergenceFeature:
    min_bars = max(rsi_period + 1, pivot_window * 2 + 1)
    if len(bars) < min_bars:
        return TechnicalDivergenceFeature(
            rsi_divergence="unavailable",
            volume_divergence="unavailable",
            lookback_bars=lookback_bars,
            pivot_window=pivot_window,
            input_bar_count=len(bars),
            status="pending" if bars else "unavailable",
        )
    scoped_bars = bars[-lookback_bars:]
    closes = tuple(bar.close_price for bar in scoped_bars)
    rsi_values = _rsi_values(closes, period=rsi_period)
    if rsi_values is None:
        return TechnicalDivergenceFeature(
            rsi_divergence="unavailable",
            volume_divergence=_volume_divergence(scoped_bars),
            lookback_bars=lookback_bars,
            pivot_window=pivot_window,
            input_bar_count=len(bars),
            status="pending",
        )
    return TechnicalDivergenceFeature(
        rsi_divergence=_rsi_divergence(
            closes=closes,
            rsi_values=rsi_values,
            pivot_window=pivot_window,
        ),
        volume_divergence=_volume_divergence(scoped_bars),
        lookback_bars=lookback_bars,
        pivot_window=pivot_window,
        input_bar_count=len(bars),
        status="available",
    )


def _rsi_divergence(
    *,
    closes: tuple[float, ...],
    rsi_values: tuple[float | None, ...],
    pivot_window: int,
) -> str:
    high_pivots = tuple(
        index
        for index in _pivot_indices(closes, pivot_window=pivot_window, mode="high")
        if rsi_values[index] is not None
    )
    low_pivots = tuple(
        index
        for index in _pivot_indices(closes, pivot_window=pivot_window, mode="low")
        if rsi_values[index] is not None
    )
    if len(high_pivots) >= 2:
        previous, latest = high_pivots[-2], high_pivots[-1]
        previous_rsi = rsi_values[previous]
        latest_rsi = rsi_values[latest]
        if (
            closes[latest] > closes[previous]
            and latest_rsi is not None
            and previous_rsi is not None
            and latest_rsi < previous_rsi
        ):
            return "bearish_rsi_divergence"
    if len(low_pivots) >= 2:
        previous, latest = low_pivots[-2], low_pivots[-1]
        previous_rsi = rsi_values[previous]
        latest_rsi = rsi_values[latest]
        if (
            closes[latest] < closes[previous]
            and latest_rsi is not None
            and previous_rsi is not None
            and latest_rsi > previous_rsi
        ):
            return "bullish_rsi_divergence"
    return "none"


def _pivot_indices(
    values: tuple[float, ...],
    *,
    pivot_window: int,
    mode: str,
) -> tuple[int, ...]:
    indices: list[int] = []
    for index in range(pivot_window, len(values) - pivot_window):
        value = values[index]
        before = values[index - pivot_window : index]
        after = values[index + 1 : index + pivot_window + 1]
        if mode == "high" and value > max(before) and value > max(after):
            indices.append(index)
        if mode == "low" and value < min(before) and value < min(after):
            indices.append(index)
    return tuple(indices)


def _volume_divergence(bars: tuple[MarketDataBar, ...]) -> str:
    if len(bars) < 3:
        return "unavailable"
    latest = bars[-1]
    previous = bars[:-1]
    previous_average_volume = sum(bar.volume for bar in previous) / len(previous)
    price_change = latest.close_price / bars[0].close_price - 1.0 if bars[0].close_price else 0.0
    if price_change > 0 and latest.volume < previous_average_volume:
        return "price_up_volume_not_confirming"
    if price_change < 0 and latest.volume < previous_average_volume:
        return "price_down_volume_not_confirming"
    return "none"


def _build_standard_ichimoku(bars: tuple[MarketDataBar, ...]) -> IchimokuFeature:
    tenkan = _midpoint_high_low(bars[-9:]) if len(bars) >= 9 else None
    kijun = _midpoint_high_low(bars[-26:]) if len(bars) >= 26 else None
    senkou_b = _midpoint_high_low(bars[-52:]) if len(bars) >= 52 else None
    senkou_a = None
    if tenkan is not None and kijun is not None:
        senkou_a = (tenkan + kijun) / 2.0
    cloud_regime = _cloud_regime(senkou_a=senkou_a, senkou_b=senkou_b)
    latest_close = bars[-1].close_price if bars else None
    return IchimokuFeature(
        tenkan=tenkan,
        kijun=kijun,
        senkou_a_known_at_as_of=senkou_a,
        senkou_b_known_at_as_of=senkou_b,
        cloud_regime=cloud_regime,
        price_location=_price_location(
            latest_close=latest_close,
            senkou_a=senkou_a,
            senkou_b=senkou_b,
        ),
        input_bar_count=len(bars),
        status="available" if senkou_a is not None and senkou_b is not None else "pending",
    )


def _midpoint_high_low(bars: tuple[MarketDataBar, ...]) -> float:
    high = max(bar.high_price for bar in bars)
    low = min(bar.low_price for bar in bars)
    return (high + low) / 2.0


def _cloud_regime(*, senkou_a: float | None, senkou_b: float | None) -> str:
    if senkou_a is None or senkou_b is None:
        return "unknown"
    if senkou_a > senkou_b:
        return "bullish"
    if senkou_a < senkou_b:
        return "bearish"
    return "flat"


def _price_location(
    *,
    latest_close: float | None,
    senkou_a: float | None,
    senkou_b: float | None,
) -> str:
    if latest_close is None or senkou_a is None or senkou_b is None:
        return "unknown"
    cloud_high = max(senkou_a, senkou_b)
    cloud_low = min(senkou_a, senkou_b)
    if latest_close > cloud_high:
        return "above_cloud"
    if latest_close < cloud_low:
        return "below_cloud"
    return "inside_cloud"


def _technical_trend_label(
    *,
    price_vs_ema_136: float | None,
    ema_slope: float | None,
    ichimoku_price_location: str,
) -> str:
    if price_vs_ema_136 is None or ema_slope is None:
        return "unknown"
    ema_positive = price_vs_ema_136 > 0 and ema_slope > 0
    ema_negative = price_vs_ema_136 < 0 and ema_slope < 0
    if ema_positive and ichimoku_price_location == "above_cloud":
        return "uptrend_confirmed"
    if ema_negative and ichimoku_price_location == "below_cloud":
        return "downtrend_confirmed"
    if ema_positive:
        return "ema_uptrend_unconfirmed"
    if ema_negative:
        return "ema_downtrend_unconfirmed"
    return "mixed_or_flat"


__all__ = [
    "bollinger_calculation_version",
    "build_technical_context",
    "ema_calculation_version",
    "ichimoku_calculation_version",
    "rsi_calculation_version",
    "technical_divergence_calculation_version",
    "technical_calculation_version",
]
