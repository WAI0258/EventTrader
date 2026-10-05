"""Deterministic price/volume market-context features."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from event_trader.contracts.view_state_change import MarketDataBar
from event_trader.market.contracts import (
    MarketContextComponentStatus,
    PriceVolumeContext,
)

_PRICE_VOLUME_VERSION = "price_volume_ohlcv_completed_bars_2026_05"
_FIXED_RETURN_FIELDS = {
    "1h": "trailing_return_1h",
    "4h": "trailing_return_4h",
    "1d": "trailing_return_1d",
    "5d": "trailing_return_5d",
    "20d": "trailing_return_20d",
}


def build_price_volume_context(
    bars: tuple[MarketDataBar, ...],
    *,
    bar_granularity: str,
    lookback_bars: int,
    trailing_return_windows: tuple[str, ...],
) -> PriceVolumeContext:
    """Build compact price/volume context from visible completed bars."""
    if not bars:
        unavailable = tuple(
            field
            for field in _PRICE_VOLUME_FIELDS
            if field not in {"breakout_label", "reversal_label", "price_recap_risk"}
        )
        return PriceVolumeContext(
            latest_completed_bar_start_at=None,
            latest_completed_bar_end_at=None,
            latest_open=None,
            latest_high=None,
            latest_low=None,
            latest_close=None,
            latest_volume=None,
            latest_vwap=None,
            trailing_return_1h=None,
            trailing_return_4h=None,
            trailing_return_1d=None,
            trailing_return_5d=None,
            trailing_return_20d=None,
            volume_vs_lookback=None,
            range_vs_lookback=None,
            realized_volatility=None,
            distance_to_recent_high=None,
            distance_to_recent_low=None,
            breakout_label="unknown",
            reversal_label="unknown",
            price_recap_risk="unavailable",
            field_status={field: "unavailable" for field in unavailable},
            status=MarketContextComponentStatus(
                component="price_volume",
                status="unavailable",
                reason="no completed bars visible at as_of_at",
                unavailable_fields=unavailable,
            ),
        )

    latest = bars[-1]
    field_status: dict[str, str] = {}
    field_status["latest_vwap"] = (
        "available" if latest.vwap is not None else "unavailable:no_vwap_in_provider_payload"
    )
    returns = _build_trailing_returns(
        bars,
        bar_granularity=bar_granularity,
        trailing_return_windows=trailing_return_windows,
        field_status=field_status,
    )
    lookback = bars[-lookback_bars:] if len(bars) > lookback_bars else bars
    volume_vs_lookback = _ratio_to_prior_mean(
        latest.volume,
        tuple(bar.volume for bar in lookback[:-1]),
    )
    field_status["volume_vs_lookback"] = (
        "available" if volume_vs_lookback is not None else "pending:requires_prior_bars"
    )
    latest_range = latest.high_price - latest.low_price
    range_vs_lookback = _ratio_to_prior_mean(
        latest_range,
        tuple(bar.high_price - bar.low_price for bar in lookback[:-1]),
    )
    field_status["range_vs_lookback"] = (
        "available" if range_vs_lookback is not None else "pending:requires_prior_bars"
    )
    realized_volatility = _realized_volatility(lookback)
    field_status["realized_volatility"] = (
        "available" if realized_volatility is not None else "pending:requires_return_history"
    )
    recent_high = max(bar.high_price for bar in lookback)
    recent_low = min(bar.low_price for bar in lookback)
    distance_to_recent_high = latest.close_price / recent_high - 1.0
    distance_to_recent_low = latest.close_price / recent_low - 1.0
    previous_high = max((bar.high_price for bar in lookback[:-1]), default=None)
    previous_low = min((bar.low_price for bar in lookback[:-1]), default=None)
    breakout_label = _breakout_label(
        latest_close=latest.close_price,
        previous_high=previous_high,
        previous_low=previous_low,
    )
    reversal_label = _reversal_label(latest)
    price_recap_risk = _price_recap_risk(returns)

    for field in (
        "latest_completed_bar_start_at",
        "latest_completed_bar_end_at",
        "latest_open",
        "latest_high",
        "latest_low",
        "latest_close",
        "latest_volume",
        "distance_to_recent_high",
        "distance_to_recent_low",
        "breakout_label",
        "reversal_label",
        "price_recap_risk",
    ):
        field_status.setdefault(field, "available")
    unavailable_fields = tuple(
        sorted(field for field, status in field_status.items() if status != "available")
    )
    status = "available" if not unavailable_fields else "partial"

    return PriceVolumeContext(
        latest_completed_bar_start_at=latest.start_at,
        latest_completed_bar_end_at=latest.end_at,
        latest_open=latest.open_price,
        latest_high=latest.high_price,
        latest_low=latest.low_price,
        latest_close=latest.close_price,
        latest_volume=latest.volume,
        latest_vwap=latest.vwap,
        trailing_return_1h=returns.get("trailing_return_1h"),
        trailing_return_4h=returns.get("trailing_return_4h"),
        trailing_return_1d=returns.get("trailing_return_1d"),
        trailing_return_5d=returns.get("trailing_return_5d"),
        trailing_return_20d=returns.get("trailing_return_20d"),
        volume_vs_lookback=volume_vs_lookback,
        range_vs_lookback=range_vs_lookback,
        realized_volatility=realized_volatility,
        distance_to_recent_high=distance_to_recent_high,
        distance_to_recent_low=distance_to_recent_low,
        breakout_label=breakout_label,
        reversal_label=reversal_label,
        price_recap_risk=price_recap_risk,
        field_status=field_status,
        status=MarketContextComponentStatus(
            component="price_volume",
            status=status,
            unavailable_fields=unavailable_fields,
        ),
    )


def price_volume_calculation_version() -> str:
    return _PRICE_VOLUME_VERSION


def _build_trailing_returns(
    bars: tuple[MarketDataBar, ...],
    *,
    bar_granularity: str,
    trailing_return_windows: tuple[str, ...],
    field_status: dict[str, str],
) -> dict[str, float | None]:
    returns: dict[str, float | None] = {
        field_name: None for field_name in _FIXED_RETURN_FIELDS.values()
    }
    bar_delta = parse_granularity_delta(bar_granularity)
    for window in trailing_return_windows:
        field_name = _FIXED_RETURN_FIELDS.get(window)
        if field_name is None:
            continue
        window_delta = parse_window_delta(window)
        if window_delta < bar_delta:
            field_status[field_name] = "unavailable:window_not_aligned_to_granularity"
            continue
        anchor_end_at = bars[-1].end_at - window_delta
        anchor_bar = _latest_bar_at_or_before(bars, anchor_end_at=anchor_end_at)
        if anchor_bar is None:
            field_status[field_name] = "pending:insufficient_history"
            continue
        returns[field_name] = bars[-1].close_price / anchor_bar.close_price - 1.0
        field_status[field_name] = "available"
    for window, field_name in _FIXED_RETURN_FIELDS.items():
        if window not in trailing_return_windows:
            field_status[field_name] = "unavailable:not_configured"
    return returns


def _latest_bar_at_or_before(
    bars: tuple[MarketDataBar, ...],
    *,
    anchor_end_at: datetime,
) -> MarketDataBar | None:
    for bar in reversed(bars):
        if bar.end_at <= anchor_end_at:
            return bar
    return None


def parse_window_delta(raw_value: str) -> timedelta:
    if len(raw_value) < 2:
        raise ValueError("window must use '<N>h' or '<N>d'.")
    count = int(raw_value[:-1])
    unit = raw_value[-1]
    if count <= 0:
        raise ValueError("window count must be positive.")
    if unit == "h":
        return timedelta(hours=count)
    if unit == "d":
        return timedelta(days=count)
    raise ValueError("window must use '<N>h' or '<N>d'.")


def parse_granularity_delta(raw_value: str) -> timedelta:
    if len(raw_value) < 2:
        raise ValueError("bar granularity must use '<N>m', '<N>min', '<N>h', or '<N>d'.")
    if raw_value.endswith("min"):
        count = int(raw_value[:-3])
        return timedelta(minutes=count)
    count = int(raw_value[:-1])
    unit = raw_value[-1]
    if count <= 0:
        raise ValueError("bar granularity count must be positive.")
    if unit == "m":
        return timedelta(minutes=count)
    if unit == "h":
        return timedelta(hours=count)
    if unit == "d":
        return timedelta(days=count)
    raise ValueError("bar granularity must use '<N>m', '<N>min', '<N>h', or '<N>d'.")


def _ratio_to_prior_mean(value: float, prior_values: tuple[float, ...]) -> float | None:
    if not prior_values:
        return None
    mean = sum(prior_values) / len(prior_values)
    if mean <= 0:
        return None
    return value / mean


def _realized_volatility(bars: tuple[MarketDataBar, ...]) -> float | None:
    if len(bars) < 3:
        return None
    log_returns = [
        math.log(current.close_price / previous.close_price)
        for previous, current in zip(bars, bars[1:], strict=False)
    ]
    mean = sum(log_returns) / len(log_returns)
    variance = sum((value - mean) ** 2 for value in log_returns) / (len(log_returns) - 1)
    return math.sqrt(variance)


def _breakout_label(
    *,
    latest_close: float,
    previous_high: float | None,
    previous_low: float | None,
) -> str:
    if previous_high is None or previous_low is None:
        return "unknown"
    if latest_close > previous_high:
        return "breakout_up"
    if latest_close < previous_low:
        return "breakdown_down"
    return "inside_recent_range"


def _reversal_label(latest: MarketDataBar) -> str:
    bar_range = latest.high_price - latest.low_price
    if bar_range <= 0:
        return "unknown"
    close_position = (latest.close_price - latest.low_price) / bar_range
    open_position = (latest.open_price - latest.low_price) / bar_range
    if open_position < 0.25 and close_position > 0.75:
        return "bullish_intrabar_reversal"
    if open_position > 0.75 and close_position < 0.25:
        return "bearish_intrabar_reversal"
    return "no_clear_reversal"


def _price_recap_risk(returns: dict[str, float | None]) -> str:
    configured_returns = [value for value in returns.values() if value is not None]
    if not configured_returns:
        return "unknown"
    largest_abs = max(abs(value) for value in configured_returns)
    if largest_abs >= 0.03:
        return "high_prior_price_move"
    if largest_abs >= 0.01:
        return "moderate_prior_price_move"
    return "low_prior_price_move"


_PRICE_VOLUME_FIELDS = (
    "latest_completed_bar_start_at",
    "latest_completed_bar_end_at",
    "latest_open",
    "latest_high",
    "latest_low",
    "latest_close",
    "latest_volume",
    "latest_vwap",
    "trailing_return_1h",
    "trailing_return_4h",
    "trailing_return_1d",
    "trailing_return_5d",
    "trailing_return_20d",
    "volume_vs_lookback",
    "range_vs_lookback",
    "realized_volatility",
    "distance_to_recent_high",
    "distance_to_recent_low",
    "breakout_label",
    "reversal_label",
    "price_recap_risk",
)


__all__ = [
    "build_price_volume_context",
    "parse_granularity_delta",
    "parse_window_delta",
    "price_volume_calculation_version",
]
