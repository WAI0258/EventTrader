"""Deterministic macro cross-asset market-context features."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime

from event_trader.contracts.view_state_change import MarketDataBar
from event_trader.market.contracts import (
    MacroCrossAssetContext,
    MacroCrossAssetGroupSummary,
    MacroCrossAssetPolicy,
    MacroCrossAssetProxySummary,
    MarketContextComponentStatus,
)
from event_trader.market.features.price_volume import parse_window_delta

_MACRO_CROSS_ASSET_VERSION = "macro_cross_asset_proxy_bars_2026_05"
_RETURN_FIELDS = {
    "1d": "trailing_return_1d",
    "5d": "trailing_return_5d",
    "20d": "trailing_return_20d",
}
_LABEL_THRESHOLD = 0.001


def build_macro_cross_asset_context(
    *,
    policy: MacroCrossAssetPolicy,
    target_bars: tuple[MarketDataBar, ...],
    proxy_bars_by_symbol: dict[str, tuple[MarketDataBar, ...]],
    as_of_at: datetime,
) -> MacroCrossAssetContext:
    """Build compact cross-asset context from visible completed proxy bars."""
    target_returns = _build_returns(target_bars, policy.trailing_return_windows)
    proxy_summaries: list[MacroCrossAssetProxySummary] = []
    for group, symbols in sorted(policy.proxy_groups.items()):
        for symbol in symbols:
            visible = tuple(
                bar
                for bar in proxy_bars_by_symbol.get(symbol, ())
                if bar.end_at <= as_of_at
            )
            proxy_summaries.append(
                _proxy_summary(
                    group=group,
                    symbol=symbol,
                    bars=visible,
                    as_of_at=as_of_at,
                    policy=policy,
                    target_returns=target_returns,
                )
            )
    group_summaries = tuple(
        _group_summary(group=group, policy=policy, summaries=tuple(proxy_summaries))
        for group in sorted(policy.proxy_groups)
    )
    usd_pressure, rates_pressure, risk_regime, confirmation, divergence_notes = _labels(
        target_return_1d=target_returns.get("trailing_return_1d"),
        group_summaries=group_summaries,
    )
    unavailable = tuple(
        sorted(
            summary.symbol
            for summary in proxy_summaries
            if summary.status.status != "available"
        )
    )
    available_count = len(proxy_summaries) - len(unavailable)
    if not proxy_summaries or available_count == 0:
        status_value = "unavailable"
        reason = "no configured proxy bars visible at as_of_at"
    elif unavailable:
        status_value = "partial"
        reason = "some configured proxy bars unavailable or incomplete"
    else:
        status_value = "available"
        reason = None

    return MacroCrossAssetContext(
        policy=policy,
        proxy_summaries=tuple(proxy_summaries),
        group_summaries=group_summaries,
        usd_pressure=usd_pressure,
        rates_pressure=rates_pressure,
        risk_regime=risk_regime,
        cross_asset_confirmation=confirmation,
        divergence_notes=tuple(divergence_notes),
        field_status={
            "proxy_count": str(len(proxy_summaries)),
            "available_proxy_count": str(available_count),
            "unavailable_proxy_symbols": ",".join(unavailable),
        },
        status=MarketContextComponentStatus(
            component="macro_cross_asset",
            status=status_value,
            reason=reason,
            unavailable_fields=unavailable,
        ),
    )


def macro_cross_asset_calculation_version() -> str:
    return _MACRO_CROSS_ASSET_VERSION


def _proxy_summary(
    *,
    group: str,
    symbol: str,
    bars: tuple[MarketDataBar, ...],
    as_of_at: datetime,
    policy: MacroCrossAssetPolicy,
    target_returns: dict[str, float | None],
) -> MacroCrossAssetProxySummary:
    if not bars:
        status = MarketContextComponentStatus(
            component=f"macro_cross_asset:{symbol}",
            status="unavailable",
            reason="no completed proxy bars visible at as_of_at",
            unavailable_fields=(
                "latest_close",
                "trailing_returns",
                "relative_returns",
            ),
        )
        return MacroCrossAssetProxySummary(
            group=group,
            symbol=symbol,
            latest_completed_bar_end_at=None,
            latest_close=None,
            trailing_return_1d=None,
            trailing_return_5d=None,
            trailing_return_20d=None,
            relative_return_1d=None,
            relative_return_5d=None,
            relative_return_20d=None,
            realized_volatility=None,
            lag_seconds=None,
            status=status,
        )

    lookback = bars[-policy.lookback_bars :]
    returns = _build_returns(bars, policy.trailing_return_windows)
    unavailable_fields = tuple(
        sorted(field for field, value in returns.items() if value is None)
    )
    status_value = "available" if not unavailable_fields else "partial"
    relative_returns = {
        field: _relative_return(returns.get(field), target_returns.get(field))
        for field in _RETURN_FIELDS.values()
    }
    latest = bars[-1]
    return MacroCrossAssetProxySummary(
        group=group,
        symbol=symbol,
        latest_completed_bar_end_at=latest.end_at,
        latest_close=latest.close_price,
        trailing_return_1d=returns.get("trailing_return_1d"),
        trailing_return_5d=returns.get("trailing_return_5d"),
        trailing_return_20d=returns.get("trailing_return_20d"),
        relative_return_1d=relative_returns.get("trailing_return_1d"),
        relative_return_5d=relative_returns.get("trailing_return_5d"),
        relative_return_20d=relative_returns.get("trailing_return_20d"),
        realized_volatility=_realized_volatility(lookback),
        lag_seconds=(as_of_at - latest.end_at).total_seconds(),
        status=MarketContextComponentStatus(
            component=f"macro_cross_asset:{symbol}",
            status=status_value,
            reason="insufficient history for some windows" if unavailable_fields else None,
            unavailable_fields=unavailable_fields,
        ),
    )


def _group_summary(
    *,
    group: str,
    policy: MacroCrossAssetPolicy,
    summaries: tuple[MacroCrossAssetProxySummary, ...],
) -> MacroCrossAssetGroupSummary:
    group_items = tuple(item for item in summaries if item.group == group)
    available = tuple(item for item in group_items if item.latest_close is not None)
    avg_1d = _mean(item.trailing_return_1d for item in available)
    avg_5d = _mean(item.trailing_return_5d for item in available)
    avg_20d = _mean(item.trailing_return_20d for item in available)
    avg_rel_1d = _mean(item.relative_return_1d for item in available)
    status_value = "available" if len(available) == len(group_items) else "partial"
    if not available:
        status_value = "unavailable"
    return MacroCrossAssetGroupSummary(
        group=group,
        proxy_count=len(group_items),
        available_proxy_count=len(available),
        average_return_1d=avg_1d,
        average_return_5d=avg_5d,
        average_return_20d=avg_20d,
        average_relative_return_1d=avg_rel_1d,
        pressure_label=_pressure_label(
            group=group,
            policy=policy,
            average_return_1d=avg_1d,
        ),
        status=MarketContextComponentStatus(
            component=f"macro_cross_asset:{group}",
            status=status_value,
            unavailable_fields=tuple(
                item.symbol for item in group_items if item.latest_close is None
            ),
        ),
    )


def _labels(
    *,
    target_return_1d: float | None,
    group_summaries: tuple[MacroCrossAssetGroupSummary, ...],
) -> tuple[str, str, str, str, list[str]]:
    by_group = {summary.group: summary for summary in group_summaries}
    usd_pressure = _group_label(by_group, "usd", default="unavailable")
    rates_pressure = _group_label(by_group, "rates_duration", default="unavailable")
    risk_regime = _risk_regime(by_group)
    confirmation = _confirmation_label(
        target_return_1d=target_return_1d,
        group_summaries=group_summaries,
    )
    return (
        usd_pressure,
        rates_pressure,
        risk_regime,
        confirmation,
        _divergence_notes(
            target_return_1d=target_return_1d,
            group_summaries=group_summaries,
        ),
    )


def _build_returns(
    bars: tuple[MarketDataBar, ...],
    windows: tuple[str, ...],
) -> dict[str, float | None]:
    returns: dict[str, float | None] = {field: None for field in _RETURN_FIELDS.values()}
    if not bars:
        return returns
    latest = bars[-1]
    for window in windows:
        field = _RETURN_FIELDS.get(window)
        if field is None:
            continue
        anchor_end_at = latest.end_at - parse_window_delta(window)
        anchor = _latest_bar_at_or_before(bars, anchor_end_at=anchor_end_at)
        if anchor is None:
            continue
        returns[field] = latest.close_price / anchor.close_price - 1.0
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


def _relative_return(
    proxy_return: float | None,
    target_return: float | None,
) -> float | None:
    if proxy_return is None or target_return is None:
        return None
    return proxy_return - target_return


def _realized_volatility(bars: tuple[MarketDataBar, ...]) -> float | None:
    if len(bars) < 3:
        return None
    returns = [
        math.log(current.close_price / previous.close_price)
        for previous, current in zip(bars, bars[1:], strict=False)
    ]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(variance)


def _mean(values: Iterable[float | None]) -> float | None:
    realized = tuple(value for value in values if value is not None)
    if not realized:
        return None
    return sum(realized) / len(realized)


def _pressure_label(
    *,
    group: str,
    policy: MacroCrossAssetPolicy,
    average_return_1d: float | None,
) -> str:
    if average_return_1d is None:
        return "unavailable"
    if abs(average_return_1d) <= _LABEL_THRESHOLD:
        return "mixed"
    rule = policy.group_pressure_rules.get(group, "contextual")
    if rule == "positive_supportive":
        return "supportive" if average_return_1d > 0 else "headwind"
    if rule == "positive_headwind":
        return "headwind" if average_return_1d > 0 else "supportive"
    return "mixed"


def _group_label(
    by_group: dict[str, MacroCrossAssetGroupSummary],
    group: str,
    *,
    default: str,
) -> str:
    summary = by_group.get(group)
    return summary.pressure_label if summary is not None else default


def _risk_regime(by_group: dict[str, MacroCrossAssetGroupSummary]) -> str:
    risk = by_group.get("risk_assets")
    volatility = by_group.get("volatility_proxy")
    risk_return = risk.average_return_1d if risk is not None else None
    vol_return = volatility.average_return_1d if volatility is not None else None
    risk_on_votes = 0
    risk_off_votes = 0
    if risk_return is not None:
        if risk_return > _LABEL_THRESHOLD:
            risk_on_votes += 1
        elif risk_return < -_LABEL_THRESHOLD:
            risk_off_votes += 1
    if vol_return is not None:
        if vol_return > _LABEL_THRESHOLD:
            risk_off_votes += 1
        elif vol_return < -_LABEL_THRESHOLD:
            risk_on_votes += 1
    if risk_on_votes == 0 and risk_off_votes == 0:
        return "unavailable"
    if risk_on_votes > 0 and risk_off_votes > 0:
        return "mixed"
    return "risk_on" if risk_on_votes > risk_off_votes else "risk_off"


def _confirmation_label(
    *,
    target_return_1d: float | None,
    group_summaries: tuple[MacroCrossAssetGroupSummary, ...],
) -> str:
    if target_return_1d is None or abs(target_return_1d) <= _LABEL_THRESHOLD:
        return "unavailable"
    supportive = sum(1 for item in group_summaries if item.pressure_label == "supportive")
    headwind = sum(1 for item in group_summaries if item.pressure_label == "headwind")
    if supportive == headwind:
        return "mixed"
    if target_return_1d > 0:
        return "confirmed" if supportive > headwind else "contradicted"
    return "confirmed" if headwind > supportive else "contradicted"


def _divergence_notes(
    *,
    target_return_1d: float | None,
    group_summaries: tuple[MacroCrossAssetGroupSummary, ...],
) -> list[str]:
    if target_return_1d is None or abs(target_return_1d) <= _LABEL_THRESHOLD:
        return []
    notes: list[str] = []
    for summary in group_summaries:
        if target_return_1d > 0 and summary.pressure_label == "headwind":
            notes.append(f"{summary.group}:headwind_against_target_strength")
        if target_return_1d < 0 and summary.pressure_label == "supportive":
            notes.append(f"{summary.group}:supportive_against_target_weakness")
    return notes


__all__ = [
    "build_macro_cross_asset_context",
    "macro_cross_asset_calculation_version",
]
