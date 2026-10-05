"""Narrow helpers for target-market series reads at runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    MarketMapping,
)
from event_trader.market.adjustments import (
    MarketAdjustmentSidecar,
    MarketDataAdjustmentPolicy,
    apply_adjustment_policy_to_bars,
    load_adjustment_sidecar_from_source_metadata,
)


class TargetSeriesError(ValueError):
    """Raised when runtime target-market bars cannot be normalized."""


class TargetSeriesProvider(Protocol):
    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        """Return the raw provider-owned market series for one target."""


@dataclass(frozen=True, slots=True)
class RealizedTargetSeries:
    """Raw target bars plus the realized adjustment sidecar for consumers."""

    series: MarketDataSeries
    adjustment_sidecar: MarketAdjustmentSidecar | None


def read_visible_target_bars(
    *,
    market_data: TargetSeriesProvider,
    market_mapping: MarketMapping,
    start_at: datetime,
    end_at: datetime,
    adjustment_policy: MarketDataAdjustmentPolicy | None = None,
) -> tuple[MarketDataBar, ...]:
    """Return visible-adjusted bars for target-facing consumers."""

    if (
        adjustment_policy is not None
        and adjustment_policy != "forward_adjusted_visible"
    ):
        raise TargetSeriesError(
            "visible target bars require forward_adjusted_visible adjustment policy."
        )
    series, source_metadata = _read_target_series_with_metadata(
        market_data=market_data,
        market_mapping=market_mapping,
        start_at=start_at,
        end_at=end_at,
    )
    visible_bars = tuple(
        sorted(
            (bar for bar in series.bars if bar.end_at <= end_at),
            key=lambda bar: (bar.start_at, bar.end_at),
        )
    )
    if not visible_bars:
        return ()
    if adjustment_policy is None:
        return visible_bars
    sidecar = load_adjustment_sidecar_from_source_metadata(
        market_symbol=market_mapping.market_symbol,
        policy=adjustment_policy,
        source_metadata=source_metadata,
    )
    adjusted_bars, _ = apply_adjustment_policy_to_bars(
        bars=visible_bars,
        sidecar=sidecar,
        reference_at=visible_bars[-1].end_at,
    )
    return adjusted_bars


def read_realized_target_series(
    *,
    market_data: TargetSeriesProvider,
    market_mapping: MarketMapping,
    start_at: datetime,
    end_at: datetime,
    adjustment_policy: MarketDataAdjustmentPolicy | None = None,
) -> RealizedTargetSeries:
    """Return raw bars plus the realized sidecar for target-facing consumers."""

    if (
        adjustment_policy is not None
        and adjustment_policy != "forward_adjusted_realized"
    ):
        raise TargetSeriesError(
            "realized target series require forward_adjusted_realized adjustment policy."
        )
    series, source_metadata = _read_target_series_with_metadata(
        market_data=market_data,
        market_mapping=market_mapping,
        start_at=start_at,
        end_at=end_at,
    )
    sidecar = (
        None
        if adjustment_policy is None
        else load_adjustment_sidecar_from_source_metadata(
            market_symbol=market_mapping.market_symbol,
            policy=adjustment_policy,
            source_metadata=source_metadata,
        )
    )
    return RealizedTargetSeries(
        series=series,
        adjustment_sidecar=sidecar,
    )


def _read_target_series_with_metadata(
    *,
    market_data: TargetSeriesProvider,
    market_mapping: MarketMapping,
    start_at: datetime,
    end_at: datetime,
) -> tuple[MarketDataSeries, dict[str, object]]:
    series = market_data.read_series(
        market_mapping,
        start_at=start_at,
        end_at=end_at,
    )
    if not isinstance(series, MarketDataSeries):
        raise TargetSeriesError(
            "market_data.read_series must return a MarketDataSeries instance."
        )
    return series, _consume_source_metadata(market_data)


def _consume_source_metadata(
    market_data: TargetSeriesProvider,
) -> dict[str, object]:
    consume: Callable[[], object] | None = cast(
        Callable[[], object] | None,
        getattr(market_data, "pop_source_metadata", None),
    )
    if not callable(consume):
        return {}
    payload = consume()
    if not isinstance(payload, dict):
        raise TargetSeriesError("market_data.pop_source_metadata must return a dict.")
    return cast(dict[str, object], payload)


__all__ = [
    "RealizedTargetSeries",
    "TargetSeriesError",
    "TargetSeriesProvider",
    "read_realized_target_series",
    "read_visible_target_bars",
]
