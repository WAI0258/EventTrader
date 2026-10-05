"""Shared live/replay market-context contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite
from typing import Any, Literal

from event_trader.contracts._validators import validate_target_key
from event_trader.market.session_policy import compact_market_session_identity


class MarketContextError(ValueError):
    """Raised when market-context contracts or deterministic builders are invalid."""


@dataclass(frozen=True, slots=True)
class MarketContextProfile:
    """Target-agnostic profile for building one market-context snapshot."""

    target_key: str
    tradable_proxy_symbol: str
    derivatives_underlying_symbol: str | None
    bar_granularity: str
    enabled_components: tuple[str, ...]
    price_volume_lookback_bars: int
    trailing_return_windows: tuple[str, ...]
    ema_periods: tuple[int, ...]
    ichimoku_enabled: bool
    bollinger_period: int = 20
    bollinger_stddev: float = 2.0
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    divergence_lookback_bars: int = 50
    divergence_pivot_window: int = 2
    market_path_enabled: bool = True
    market_path_session: Literal["continuous", "exchange_session"] | None = None
    market_path_range_lookback_calendar_days: int = 20
    market_path_range_lookback_sessions: int = 20
    market_path_range_lookback_daily_bars: int = 60
    market_path_atr_period: int = 14
    market_path_atr_multiple: float = 2.0
    market_path_pct_floor: float = 0.025
    market_path_min_leg_bars: int = 3
    market_path_large_move_pct: float = 0.08
    market_path_near_extreme_pct: float = 0.03
    derivatives: OptionSelectionPolicy | None = None
    macro_cross_asset: MacroCrossAssetPolicy | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=MarketContextError),
        )
        object.__setattr__(
            self,
            "tradable_proxy_symbol",
            _validate_non_blank(self.tradable_proxy_symbol, "tradable_proxy_symbol"),
        )
        object.__setattr__(
            self,
            "enabled_components",
            _validate_non_blank_tuple(
                self.enabled_components,
                field_name="enabled_components",
            ),
        )
        if "derivatives" in self.enabled_components:
            if self.derivatives_underlying_symbol is None:
                raise MarketContextError(
                    "derivatives_underlying_symbol is required when derivatives is enabled."
                )
            object.__setattr__(
                self,
                "derivatives_underlying_symbol",
                _validate_non_blank(
                    self.derivatives_underlying_symbol,
                    "derivatives_underlying_symbol",
                ),
            )
        elif self.derivatives_underlying_symbol is not None:
            object.__setattr__(
                self,
                "derivatives_underlying_symbol",
                _validate_non_blank(
                    self.derivatives_underlying_symbol,
                    "derivatives_underlying_symbol",
                ),
            )
        object.__setattr__(
            self,
            "bar_granularity",
            _validate_non_blank(self.bar_granularity, "bar_granularity"),
        )
        object.__setattr__(
            self,
            "price_volume_lookback_bars",
            _validate_positive_int(
                self.price_volume_lookback_bars,
                "price_volume_lookback_bars",
            ),
        )
        object.__setattr__(
            self,
            "trailing_return_windows",
            _validate_non_blank_tuple(
                self.trailing_return_windows,
                field_name="trailing_return_windows",
            ),
        )
        object.__setattr__(
            self,
            "ema_periods",
            _validate_positive_int_tuple(self.ema_periods, "ema_periods"),
        )
        if not isinstance(self.ichimoku_enabled, bool):
            raise MarketContextError("ichimoku_enabled must be a boolean.")
        for field_name in (
            "bollinger_period",
            "rsi_period",
            "divergence_lookback_bars",
            "divergence_pivot_window",
            "market_path_range_lookback_calendar_days",
            "market_path_range_lookback_sessions",
            "market_path_range_lookback_daily_bars",
            "market_path_atr_period",
            "market_path_min_leg_bars",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_positive_int(getattr(self, field_name), field_name),
            )
        _validate_optional_finite(self.bollinger_stddev, "bollinger_stddev")
        if self.bollinger_stddev <= 0:
            raise MarketContextError("bollinger_stddev must be positive.")
        for field_name in ("rsi_overbought", "rsi_oversold"):
            value = getattr(self, field_name)
            _validate_optional_finite(value, field_name)
            if value < 0 or value > 100:
                raise MarketContextError(f"{field_name} must be between 0 and 100.")
        if self.rsi_oversold >= self.rsi_overbought:
            raise MarketContextError("rsi_oversold must be less than rsi_overbought.")
        if self.divergence_lookback_bars < self.rsi_period + 1:
            raise MarketContextError(
                "divergence_lookback_bars must be >= rsi_period + 1."
            )
        if self.divergence_lookback_bars < self.divergence_pivot_window * 2 + 1:
            raise MarketContextError(
                "divergence_lookback_bars must be >= "
                "divergence_pivot_window * 2 + 1."
            )
        if not isinstance(self.market_path_enabled, bool):
            raise MarketContextError("market_path_enabled must be a boolean.")
        if self.market_path_session not in {None, "continuous", "exchange_session"}:
            raise MarketContextError("market_path_session is invalid.")
        for field_name in (
            "market_path_atr_multiple",
            "market_path_pct_floor",
            "market_path_large_move_pct",
            "market_path_near_extreme_pct",
        ):
            value = getattr(self, field_name)
            _validate_optional_finite(value, field_name)
            if value <= 0:
                raise MarketContextError(f"{field_name} must be positive.")
        if self.derivatives is not None and not isinstance(
            self.derivatives,
            OptionSelectionPolicy,
        ):
            raise MarketContextError("derivatives must be an OptionSelectionPolicy.")
        if self.macro_cross_asset is not None and not isinstance(
            self.macro_cross_asset,
            MacroCrossAssetPolicy,
        ):
            raise MarketContextError(
                "macro_cross_asset must be a MacroCrossAssetPolicy."
            )

    @classmethod
    def from_config(
        cls,
        *,
        target_key: str,
        profile_config: Any,
    ) -> MarketContextProfile:
        price_volume = getattr(profile_config, "price_volume", None)
        market_path = profile_config.market_path
        technical = getattr(profile_config, "technical", None)
        derivatives = getattr(profile_config, "derivatives", None)
        macro_cross_asset = getattr(profile_config, "macro_cross_asset", None)
        return cls(
            target_key=target_key,
            tradable_proxy_symbol=profile_config.tradable_proxy_symbol,
            derivatives_underlying_symbol=profile_config.derivatives_underlying_symbol,
            bar_granularity=profile_config.bar_granularity,
            enabled_components=tuple(profile_config.enabled_components),
            price_volume_lookback_bars=(
                price_volume.lookback_bars if price_volume is not None else 1
            ),
            trailing_return_windows=(
                tuple(price_volume.trailing_return_windows)
                if price_volume is not None
                else ("1h",)
            ),
            ema_periods=tuple(technical.ema_periods) if technical is not None else (136,),
            ichimoku_enabled=(
                bool(technical.ichimoku_enabled) if technical is not None else False
            ),
            bollinger_period=(
                technical.bollinger_period if technical is not None else 20
            ),
            bollinger_stddev=(
                technical.bollinger_stddev if technical is not None else 2.0
            ),
            rsi_period=technical.rsi_period if technical is not None else 14,
            rsi_overbought=(
                technical.rsi_overbought if technical is not None else 70.0
            ),
            rsi_oversold=technical.rsi_oversold if technical is not None else 30.0,
            divergence_lookback_bars=(
                technical.divergence_lookback_bars if technical is not None else 50
            ),
            divergence_pivot_window=(
                technical.divergence_pivot_window if technical is not None else 2
            ),
            market_path_enabled=market_path.enabled,
            market_path_session=market_path.session,
            market_path_range_lookback_calendar_days=(
                market_path.range_lookback_calendar_days
            ),
            market_path_range_lookback_sessions=market_path.range_lookback_sessions,
            market_path_range_lookback_daily_bars=(
                market_path.range_lookback_daily_bars
            ),
            market_path_atr_period=market_path.atr_period,
            market_path_atr_multiple=market_path.atr_multiple,
            market_path_pct_floor=market_path.pct_floor,
            market_path_min_leg_bars=market_path.min_leg_bars,
            market_path_large_move_pct=market_path.large_move_pct,
            market_path_near_extreme_pct=market_path.near_extreme_pct,
            derivatives=(
                OptionSelectionPolicy.from_config(derivatives)
                if derivatives is not None
                else None
            ),
            macro_cross_asset=(
                MacroCrossAssetPolicy.from_config(macro_cross_asset)
                if macro_cross_asset is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class MarketContextComponentStatus:
    """Availability state for one deterministic market-context component."""

    component: str
    status: str
    reason: str | None = None
    unavailable_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "component", _validate_non_blank(self.component, "component"))
        if self.status not in {"available", "partial", "pending", "unavailable", "disabled"}:
            raise MarketContextError(
                "status must be available, partial, pending, unavailable, or disabled."
            )
        if self.reason is not None:
            object.__setattr__(self, "reason", _validate_non_blank(self.reason, "reason"))
        object.__setattr__(
            self,
            "unavailable_fields",
            _validate_string_tuple(self.unavailable_fields, field_name="unavailable_fields"),
        )


@dataclass(frozen=True, slots=True)
class EmaFeature:
    """Deterministic EMA feature calculated from visible completed bars."""

    period: int
    value: float | None
    slope: float | None
    input_bar_count: int
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "period", _validate_positive_int(self.period, "period"))
        _validate_optional_finite(self.value, "value")
        _validate_optional_finite(self.slope, "slope")
        object.__setattr__(
            self,
            "input_bar_count",
            _validate_non_negative_int(self.input_bar_count, "input_bar_count"),
        )
        if self.status not in {"available", "pending", "unavailable"}:
            raise MarketContextError("EMA status must be available, pending, or unavailable.")


@dataclass(frozen=True, slots=True)
class IchimokuFeature:
    """Standard Ichimoku values calculated only from visible completed bars."""

    tenkan: float | None
    kijun: float | None
    senkou_a_known_at_as_of: float | None
    senkou_b_known_at_as_of: float | None
    cloud_regime: str
    price_location: str
    input_bar_count: int
    status: str

    def __post_init__(self) -> None:
        _validate_optional_finite(self.tenkan, "tenkan")
        _validate_optional_finite(self.kijun, "kijun")
        _validate_optional_finite(
            self.senkou_a_known_at_as_of,
            "senkou_a_known_at_as_of",
        )
        _validate_optional_finite(
            self.senkou_b_known_at_as_of,
            "senkou_b_known_at_as_of",
        )
        if self.cloud_regime not in {"bullish", "bearish", "flat", "unknown"}:
            raise MarketContextError("ichimoku cloud_regime is invalid.")
        if self.price_location not in {"above_cloud", "below_cloud", "inside_cloud", "unknown"}:
            raise MarketContextError("ichimoku price_location is invalid.")
        object.__setattr__(
            self,
            "input_bar_count",
            _validate_non_negative_int(self.input_bar_count, "input_bar_count"),
        )
        if self.status not in {"available", "pending", "unavailable"}:
            raise MarketContextError(
                "Ichimoku status must be available, pending, or unavailable."
            )


@dataclass(frozen=True, slots=True)
class BollingerBandsFeature:
    """Standard Bollinger Bands calculated from visible completed closes."""

    period: int
    stddev_multiplier: float
    middle: float | None
    upper: float | None
    lower: float | None
    bandwidth: float | None
    percent_b: float | None
    state: str
    input_bar_count: int
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "period", _validate_positive_int(self.period, "period"))
        _validate_optional_finite(self.stddev_multiplier, "stddev_multiplier")
        if self.stddev_multiplier <= 0:
            raise MarketContextError("stddev_multiplier must be positive.")
        for field_name in (
            "middle",
            "upper",
            "lower",
            "bandwidth",
            "percent_b",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
        if self.state not in {
            "above_upper_band",
            "below_lower_band",
            "inside_band",
            "unavailable",
        }:
            raise MarketContextError("Bollinger state is invalid.")
        object.__setattr__(
            self,
            "input_bar_count",
            _validate_non_negative_int(self.input_bar_count, "input_bar_count"),
        )
        if self.status not in {"available", "pending", "unavailable"}:
            raise MarketContextError(
                "Bollinger status must be available, pending, or unavailable."
            )


@dataclass(frozen=True, slots=True)
class RsiFeature:
    """Wilder-style RSI calculated from visible completed closes."""

    period: int
    value: float | None
    state: str
    input_bar_count: int
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "period", _validate_positive_int(self.period, "period"))
        _validate_optional_finite(self.value, "value")
        if self.value is not None and not 0 <= self.value <= 100:
            raise MarketContextError("RSI value must be between 0 and 100.")
        if self.state not in {"overbought", "oversold", "neutral", "unavailable"}:
            raise MarketContextError("RSI state is invalid.")
        object.__setattr__(
            self,
            "input_bar_count",
            _validate_non_negative_int(self.input_bar_count, "input_bar_count"),
        )
        if self.status not in {"available", "pending", "unavailable"}:
            raise MarketContextError("RSI status must be available, pending, or unavailable.")


@dataclass(frozen=True, slots=True)
class TechnicalDivergenceFeature:
    """Mechanical divergence facts from visible price, RSI, and volume pivots."""

    rsi_divergence: str
    volume_divergence: str
    lookback_bars: int
    pivot_window: int
    input_bar_count: int
    status: str

    def __post_init__(self) -> None:
        if self.rsi_divergence not in {
            "bearish_rsi_divergence",
            "bullish_rsi_divergence",
            "none",
            "unavailable",
        }:
            raise MarketContextError("RSI divergence state is invalid.")
        if self.volume_divergence not in {
            "price_up_volume_not_confirming",
            "price_down_volume_not_confirming",
            "none",
            "unavailable",
        }:
            raise MarketContextError("Volume divergence state is invalid.")
        object.__setattr__(
            self,
            "lookback_bars",
            _validate_positive_int(self.lookback_bars, "lookback_bars"),
        )
        object.__setattr__(
            self,
            "pivot_window",
            _validate_positive_int(self.pivot_window, "pivot_window"),
        )
        object.__setattr__(
            self,
            "input_bar_count",
            _validate_non_negative_int(self.input_bar_count, "input_bar_count"),
        )
        if self.status not in {"available", "pending", "unavailable"}:
            raise MarketContextError(
                "Divergence status must be available, pending, or unavailable."
            )


@dataclass(frozen=True, slots=True)
class PriceVolumeContext:
    """Compact price/volume facts from completed OHLCV bars."""

    latest_completed_bar_start_at: datetime | None
    latest_completed_bar_end_at: datetime | None
    latest_open: float | None
    latest_high: float | None
    latest_low: float | None
    latest_close: float | None
    latest_volume: float | None
    latest_vwap: float | None
    trailing_return_1h: float | None
    trailing_return_4h: float | None
    trailing_return_1d: float | None
    trailing_return_5d: float | None
    trailing_return_20d: float | None
    volume_vs_lookback: float | None
    range_vs_lookback: float | None
    realized_volatility: float | None
    distance_to_recent_high: float | None
    distance_to_recent_low: float | None
    breakout_label: str
    reversal_label: str
    price_recap_risk: str
    field_status: dict[str, str]
    status: MarketContextComponentStatus

    def __post_init__(self) -> None:
        if self.latest_completed_bar_start_at is not None:
            object.__setattr__(
                self,
                "latest_completed_bar_start_at",
                _validate_utc_datetime(
                    self.latest_completed_bar_start_at,
                    "latest_completed_bar_start_at",
                ),
            )
        if self.latest_completed_bar_end_at is not None:
            object.__setattr__(
                self,
                "latest_completed_bar_end_at",
                _validate_utc_datetime(
                    self.latest_completed_bar_end_at,
                    "latest_completed_bar_end_at",
                ),
            )
        for field_name in (
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
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "breakout_label",
            _validate_non_blank(self.breakout_label, "breakout_label"),
        )
        object.__setattr__(
            self,
            "reversal_label",
            _validate_non_blank(self.reversal_label, "reversal_label"),
        )
        object.__setattr__(
            self,
            "price_recap_risk",
            _validate_non_blank(self.price_recap_risk, "price_recap_risk"),
        )
        if not isinstance(self.field_status, dict):
            raise MarketContextError("field_status must be a dict.")
        if not isinstance(self.status, MarketContextComponentStatus):
            raise MarketContextError("status must be a MarketContextComponentStatus.")


@dataclass(frozen=True, slots=True)
class RangePathSnapshot:
    """Windowed factual price path; it does not claim structural pivots."""

    requested_lookback_label: str
    coverage_status: Literal["full", "partial", "insufficient", "unavailable"]
    available_bar_count: int
    lookback_start_at: datetime | None
    lookback_end_at: datetime | None
    latest_price: float | None
    window_low: float | None
    window_low_at: datetime | None
    window_high: float | None
    window_high_at: datetime | None
    move_from_window_low_points: float | None
    move_from_window_low_pct: float | None
    move_from_window_high_points: float | None
    move_from_window_high_pct: float | None
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "requested_lookback_label",
            _validate_non_blank(
                self.requested_lookback_label,
                "requested_lookback_label",
            ),
        )
        if self.coverage_status not in {
            "full",
            "partial",
            "insufficient",
            "unavailable",
        }:
            raise MarketContextError("range_path coverage_status is invalid.")
        object.__setattr__(
            self,
            "available_bar_count",
            _validate_non_negative_int(
                self.available_bar_count,
                "available_bar_count",
            ),
        )
        for field_name in (
            "lookback_start_at",
            "lookback_end_at",
            "window_low_at",
            "window_high_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _validate_utc_datetime(value, field_name),
                )
        for field_name in (
            "latest_price",
            "window_low",
            "window_high",
            "move_from_window_low_points",
            "move_from_window_low_pct",
            "move_from_window_high_points",
            "move_from_window_high_pct",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
        if self.missing_reason is not None:
            object.__setattr__(
                self,
                "missing_reason",
                _validate_non_blank(self.missing_reason, "missing_reason"),
            )


@dataclass(frozen=True, slots=True)
class CausalPivot:
    """One replay-safe swing pivot, confirmed only after visible reversal."""

    kind: Literal["swing_low", "swing_high"]
    pivot_price: float
    pivot_at: datetime
    confirmed_at: datetime
    confirmation_lag_bars: int
    reversal_points: float
    reversal_pct: float
    no_future_bars_used: bool

    def __post_init__(self) -> None:
        if self.kind not in {"swing_low", "swing_high"}:
            raise MarketContextError("causal pivot kind is invalid.")
        for field_name in ("pivot_price", "reversal_points", "reversal_pct"):
            _validate_optional_finite(getattr(self, field_name), field_name)
        if self.pivot_price <= 0:
            raise MarketContextError("pivot_price must be positive.")
        object.__setattr__(
            self,
            "pivot_at",
            _validate_utc_datetime(self.pivot_at, "pivot_at"),
        )
        object.__setattr__(
            self,
            "confirmed_at",
            _validate_utc_datetime(self.confirmed_at, "confirmed_at"),
        )
        if self.confirmed_at < self.pivot_at:
            raise MarketContextError("confirmed_at must be >= pivot_at.")
        object.__setattr__(
            self,
            "confirmation_lag_bars",
            _validate_non_negative_int(
                self.confirmation_lag_bars,
                "confirmation_lag_bars",
            ),
        )
        if self.no_future_bars_used is not True:
            raise MarketContextError("causal pivots must set no_future_bars_used=true.")


@dataclass(frozen=True, slots=True)
class CurrentLeg:
    """Current visible move from the last confirmed pivot."""

    from_pivot_kind: Literal["swing_low", "swing_high"]
    from_pivot_price: float
    from_pivot_at: datetime
    latest_price: float
    move_points: float
    move_pct: float
    leg_direction: Literal["up", "down", "sideways"]
    maturity: Literal["early", "developing", "late"]

    def __post_init__(self) -> None:
        if self.from_pivot_kind not in {"swing_low", "swing_high"}:
            raise MarketContextError("current_leg from_pivot_kind is invalid.")
        for field_name in (
            "from_pivot_price",
            "latest_price",
            "move_points",
            "move_pct",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
        if self.from_pivot_price <= 0 or self.latest_price <= 0:
            raise MarketContextError("current_leg prices must be positive.")
        object.__setattr__(
            self,
            "from_pivot_at",
            _validate_utc_datetime(self.from_pivot_at, "from_pivot_at"),
        )
        if self.leg_direction not in {"up", "down", "sideways"}:
            raise MarketContextError("current_leg leg_direction is invalid.")
        if self.maturity not in {"early", "developing", "late"}:
            raise MarketContextError("current_leg maturity is invalid.")


@dataclass(frozen=True, slots=True)
class CausalSwingSnapshot:
    """Causal swing structure from visible bars, separate from range facts."""

    status: Literal["available", "partial", "insufficient", "unavailable"]
    algorithm: Literal["causal_atr_zigzag_v1"]
    params: dict[str, object]
    confirmed_pivots: tuple[CausalPivot, ...]
    current_leg: CurrentLeg | None
    structure_flags: tuple[str, ...]
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"available", "partial", "insufficient", "unavailable"}:
            raise MarketContextError("causal swing status is invalid.")
        if self.algorithm != "causal_atr_zigzag_v1":
            raise MarketContextError("causal swing algorithm is invalid.")
        if not isinstance(self.params, dict):
            raise MarketContextError("causal swing params must be a dict.")
        object.__setattr__(self, "params", dict(sorted(self.params.items())))
        object.__setattr__(self, "confirmed_pivots", tuple(self.confirmed_pivots))
        for pivot in self.confirmed_pivots:
            if not isinstance(pivot, CausalPivot):
                raise MarketContextError(
                    "confirmed_pivots must contain only CausalPivot instances."
                )
        if self.current_leg is not None and not isinstance(self.current_leg, CurrentLeg):
            raise MarketContextError("current_leg must be a CurrentLeg when present.")
        object.__setattr__(
            self,
            "structure_flags",
            _validate_string_tuple(self.structure_flags, field_name="structure_flags"),
        )
        if self.missing_reason is not None:
            object.__setattr__(
                self,
                "missing_reason",
                _validate_non_blank(self.missing_reason, "missing_reason"),
            )


@dataclass(frozen=True, slots=True)
class MarketPathContext:
    """Market path facts for setup/exposure separation."""

    target_key: str
    business_at: datetime
    proxy_symbol: str
    bar_granularity: str
    session: Literal["continuous", "exchange_session"]
    range_path: RangePathSnapshot
    swing_path: CausalSwingSnapshot
    path_flags: tuple[str, ...]
    config: dict[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=MarketContextError),
        )
        object.__setattr__(
            self,
            "business_at",
            _validate_utc_datetime(self.business_at, "business_at"),
        )
        object.__setattr__(
            self,
            "proxy_symbol",
            _validate_non_blank(self.proxy_symbol, "proxy_symbol"),
        )
        object.__setattr__(
            self,
            "bar_granularity",
            _validate_non_blank(self.bar_granularity, "bar_granularity"),
        )
        if self.session not in {"continuous", "exchange_session"}:
            raise MarketContextError("market_path session is invalid.")
        if not isinstance(self.range_path, RangePathSnapshot):
            raise MarketContextError("market_path.range_path must be RangePathSnapshot.")
        if not isinstance(self.swing_path, CausalSwingSnapshot):
            raise MarketContextError("market_path.swing_path must be CausalSwingSnapshot.")
        object.__setattr__(
            self,
            "path_flags",
            _validate_string_tuple(self.path_flags, field_name="path_flags"),
        )
        if not isinstance(self.config, dict):
            raise MarketContextError("market_path config must be a dict.")
        object.__setattr__(self, "config", dict(sorted(self.config.items())))


@dataclass(frozen=True, slots=True)
class TechnicalContext:
    """Compact deterministic technical context from completed OHLCV bars."""

    ema_136: EmaFeature | None
    price_vs_ema_136: float | None
    ema_136_slope: float | None
    ichimoku_tenkan: float | None
    ichimoku_kijun: float | None
    ichimoku_senkou_a_known_at_as_of: float | None
    ichimoku_senkou_b_known_at_as_of: float | None
    ichimoku_cloud_regime: str
    ichimoku_price_location: str
    bollinger_bands: BollingerBandsFeature | None
    rsi: RsiFeature | None
    divergence: TechnicalDivergenceFeature | None
    technical_trend_label: str
    technical_context_status: MarketContextComponentStatus
    indicators: dict[str, dict[str, object]]

    def __post_init__(self) -> None:
        if self.ema_136 is not None and not isinstance(self.ema_136, EmaFeature):
            raise MarketContextError("ema_136 must be an EmaFeature when present.")
        if self.bollinger_bands is not None and not isinstance(
            self.bollinger_bands,
            BollingerBandsFeature,
        ):
            raise MarketContextError(
                "bollinger_bands must be a BollingerBandsFeature when present."
            )
        if self.rsi is not None and not isinstance(self.rsi, RsiFeature):
            raise MarketContextError("rsi must be an RsiFeature when present.")
        if self.divergence is not None and not isinstance(
            self.divergence,
            TechnicalDivergenceFeature,
        ):
            raise MarketContextError(
                "divergence must be a TechnicalDivergenceFeature when present."
            )
        for field_name in (
            "price_vs_ema_136",
            "ema_136_slope",
            "ichimoku_tenkan",
            "ichimoku_kijun",
            "ichimoku_senkou_a_known_at_as_of",
            "ichimoku_senkou_b_known_at_as_of",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
        if self.ichimoku_cloud_regime not in {"bullish", "bearish", "flat", "unknown"}:
            raise MarketContextError("ichimoku_cloud_regime is invalid.")
        if self.ichimoku_price_location not in {
            "above_cloud",
            "below_cloud",
            "inside_cloud",
            "unknown",
        }:
            raise MarketContextError("ichimoku_price_location is invalid.")
        object.__setattr__(
            self,
            "technical_trend_label",
            _validate_non_blank(self.technical_trend_label, "technical_trend_label"),
        )
        if not isinstance(self.technical_context_status, MarketContextComponentStatus):
            raise MarketContextError(
                "technical_context_status must be a MarketContextComponentStatus."
            )
        if not isinstance(self.indicators, dict):
            raise MarketContextError("indicators must be a dict.")


@dataclass(frozen=True, slots=True)
class OptionSelectionPolicy:
    """Profile-driven policy for deterministic option-contract selection."""

    data_feed: str
    chain_max_rows: int
    max_contracts_per_snapshot: int
    expiry_days_min: int
    expiry_days_max: int
    strike_pct_window: float
    atm_contract_count: int
    otm_contract_count: int
    min_trade_count: int
    large_trade_notional_threshold: float
    include_historical_trades: bool
    include_historical_bars: bool
    include_latest_snapshot_quotes: bool
    bars_granularity: str
    lookback_hours: int
    include_latest_trades: bool = True
    include_latest_quotes: bool = True
    historical_universe_reconstruction_enabled: bool = True
    historical_universe_max_candidates_per_bucket: int = 300
    historical_universe_strike_step: float = 1.0
    unusual_baseline_windows_days: tuple[int, ...] = (7, 14)
    unusual_min_baseline_observations: int = 3
    unusual_elevated_ratio: float = 2.0
    unusual_unusual_ratio: float = 5.0
    unusual_extreme_ratio: float = 10.0
    notable_contract_limit: int = 5

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_feed", _validate_non_blank(self.data_feed, "data_feed"))
        object.__setattr__(
            self,
            "chain_max_rows",
            _validate_positive_int(self.chain_max_rows, "chain_max_rows"),
        )
        object.__setattr__(
            self,
            "max_contracts_per_snapshot",
            _validate_positive_int(
                self.max_contracts_per_snapshot,
                "max_contracts_per_snapshot",
            ),
        )
        object.__setattr__(
            self,
            "expiry_days_min",
            _validate_non_negative_int(self.expiry_days_min, "expiry_days_min"),
        )
        object.__setattr__(
            self,
            "expiry_days_max",
            _validate_non_negative_int(self.expiry_days_max, "expiry_days_max"),
        )
        if self.expiry_days_min > self.expiry_days_max:
            raise MarketContextError("expiry_days_min must be <= expiry_days_max.")
        _validate_optional_finite(self.strike_pct_window, "strike_pct_window")
        if self.strike_pct_window <= 0:
            raise MarketContextError("strike_pct_window must be positive.")
        object.__setattr__(
            self,
            "atm_contract_count",
            _validate_non_negative_int(self.atm_contract_count, "atm_contract_count"),
        )
        object.__setattr__(
            self,
            "otm_contract_count",
            _validate_non_negative_int(self.otm_contract_count, "otm_contract_count"),
        )
        object.__setattr__(
            self,
            "min_trade_count",
            _validate_non_negative_int(self.min_trade_count, "min_trade_count"),
        )
        _validate_optional_finite(
            self.large_trade_notional_threshold,
            "large_trade_notional_threshold",
        )
        if self.large_trade_notional_threshold <= 0:
            raise MarketContextError("large_trade_notional_threshold must be positive.")
        for field_name in (
            "include_historical_trades",
            "include_historical_bars",
            "include_latest_snapshot_quotes",
            "include_latest_trades",
            "include_latest_quotes",
            "historical_universe_reconstruction_enabled",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise MarketContextError(f"{field_name} must be a boolean.")
        object.__setattr__(
            self,
            "historical_universe_max_candidates_per_bucket",
            _validate_positive_int(
                self.historical_universe_max_candidates_per_bucket,
                "historical_universe_max_candidates_per_bucket",
            ),
        )
        if self.historical_universe_strike_step is not None:
            _validate_optional_finite(
                self.historical_universe_strike_step,
                "historical_universe_strike_step",
            )
            if self.historical_universe_strike_step <= 0:
                raise MarketContextError(
                    "historical_universe_strike_step must be positive."
                )
        unusual_days = _normalize_int_tuple(
            self.unusual_baseline_windows_days,
            field_name="unusual_baseline_windows_days",
            min_value=1,
        )
        object.__setattr__(self, "unusual_baseline_windows_days", unusual_days)
        object.__setattr__(
            self,
            "bars_granularity",
            _validate_non_blank(self.bars_granularity, "bars_granularity"),
        )
        object.__setattr__(
            self,
            "unusual_min_baseline_observations",
            _validate_non_negative_int(
                self.unusual_min_baseline_observations,
                "unusual_min_baseline_observations",
            ),
        )
        for field_name in (
            "unusual_elevated_ratio",
            "unusual_unusual_ratio",
            "unusual_extreme_ratio",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
            if getattr(self, field_name) <= 1.0:
                raise MarketContextError(f"{field_name} must be greater than 1.0.")
        object.__setattr__(
            self,
            "notable_contract_limit",
            _validate_positive_int(self.notable_contract_limit, "notable_contract_limit"),
        )
        if not (
            self.unusual_elevated_ratio
            < self.unusual_unusual_ratio
            < self.unusual_extreme_ratio
        ):
            raise MarketContextError(
                "unusual ratio thresholds must satisfy "
                "elevated < unusual < extreme."
            )
        object.__setattr__(
            self,
            "bars_granularity",
            _validate_non_blank(self.bars_granularity, "bars_granularity"),
        )
        object.__setattr__(
            self,
            "lookback_hours",
            _validate_positive_int(self.lookback_hours, "lookback_hours"),
        )

    @classmethod
    def from_config(cls, config: Any) -> OptionSelectionPolicy:
        unusual_baseline_windows_days = getattr(
            config,
            "unusual_baseline_windows_days",
            (7, 14),
        )
        return cls(
            data_feed=config.data_feed,
            chain_max_rows=config.chain_max_rows,
            max_contracts_per_snapshot=config.max_contracts_per_snapshot,
            expiry_days_min=config.expiry_days_min,
            expiry_days_max=config.expiry_days_max,
            strike_pct_window=config.strike_pct_window,
            atm_contract_count=config.atm_contract_count,
            otm_contract_count=config.otm_contract_count,
            min_trade_count=config.min_trade_count,
            large_trade_notional_threshold=config.large_trade_notional_threshold,
            include_historical_trades=config.include_historical_trades,
            include_historical_bars=config.include_historical_bars,
            include_latest_snapshot_quotes=config.include_latest_snapshot_quotes,
            include_latest_trades=getattr(config, "include_latest_trades", True),
            include_latest_quotes=getattr(config, "include_latest_quotes", True),
            historical_universe_reconstruction_enabled=getattr(
                config,
                "historical_universe_reconstruction_enabled",
                True,
            ),
            historical_universe_max_candidates_per_bucket=getattr(
                config,
                "historical_universe_max_candidates_per_bucket",
                300,
            ),
            historical_universe_strike_step=getattr(
                config,
                "historical_universe_strike_step",
                1.0,
            ),
            unusual_baseline_windows_days=unusual_baseline_windows_days,
            unusual_min_baseline_observations=getattr(
                config,
                "unusual_min_baseline_observations",
                3,
            ),
            unusual_elevated_ratio=getattr(config, "unusual_elevated_ratio", 2.0),
            unusual_unusual_ratio=getattr(config, "unusual_unusual_ratio", 5.0),
            unusual_extreme_ratio=getattr(config, "unusual_extreme_ratio", 10.0),
            notable_contract_limit=getattr(config, "notable_contract_limit", 5),
            bars_granularity=config.bars_granularity,
            lookback_hours=config.lookback_hours,
        )


@dataclass(frozen=True, slots=True)
class OptionContractSnapshot:
    """One visible option-chain snapshot row normalized for market context."""

    contract_symbol: str
    underlying_symbol: str
    option_type: str
    expiry_date: str
    strike_price: float
    observed_at: datetime | None
    volume: float | None = None
    trade_count: int | None = None
    latest_trade_price: float | None = None
    latest_trade_size: float | None = None
    latest_trade_at: datetime | None = None
    latest_quote_at: datetime | None = None
    bid_price: float | None = None
    ask_price: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    rho: float | None = None
    theta: float | None = None
    vega: float | None = None
    open_interest: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "contract_symbol",
            _validate_non_blank(self.contract_symbol, "contract_symbol"),
        )
        object.__setattr__(
            self,
            "underlying_symbol",
            _validate_non_blank(self.underlying_symbol, "underlying_symbol"),
        )
        if self.option_type not in {"call", "put"}:
            raise MarketContextError("option_type must be call or put.")
        object.__setattr__(
            self,
            "expiry_date",
            _validate_non_blank(self.expiry_date, "expiry_date"),
        )
        _validate_optional_finite(self.strike_price, "strike_price")
        if self.strike_price <= 0:
            raise MarketContextError("strike_price must be positive.")
        for field_name in ("observed_at", "latest_trade_at", "latest_quote_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _validate_utc_datetime(value, field_name))
        for field_name in (
            "volume",
            "latest_trade_price",
            "latest_trade_size",
            "bid_price",
            "ask_price",
            "bid_size",
            "ask_size",
            "implied_volatility",
            "delta",
            "gamma",
            "rho",
            "theta",
            "vega",
            "open_interest",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
        if self.trade_count is not None:
            object.__setattr__(
                self,
                "trade_count",
                _validate_non_negative_int(self.trade_count, "trade_count"),
            )


@dataclass(frozen=True, slots=True)
class OptionTradeObservation:
    """One visible option trade observation for selected contracts."""

    contract_symbol: str
    option_type: str
    expiry_date: str
    strike_price: float
    observed_at: datetime
    price: float
    size: float
    notional: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "contract_symbol",
            _validate_non_blank(self.contract_symbol, "contract_symbol"),
        )
        if self.option_type not in {"call", "put"}:
            raise MarketContextError("option_type must be call or put.")
        object.__setattr__(
            self,
            "expiry_date",
            _validate_non_blank(self.expiry_date, "expiry_date"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _validate_utc_datetime(self.observed_at, "observed_at"),
        )
        for field_name in ("strike_price", "price", "size", "notional"):
            _validate_optional_finite(getattr(self, field_name), field_name)
            if getattr(self, field_name) < 0:
                raise MarketContextError(f"{field_name} must be non-negative.")


@dataclass(frozen=True, slots=True)
class OptionBarObservation:
    """One visible option bar observation for selected contracts."""

    contract_symbol: str
    option_type: str
    expiry_date: str
    strike_price: float
    start_at: datetime
    end_at: datetime
    close_price: float
    volume: float
    trade_count: int | None = None
    vwap: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "contract_symbol",
            _validate_non_blank(self.contract_symbol, "contract_symbol"),
        )
        if self.option_type not in {"call", "put"}:
            raise MarketContextError("option_type must be call or put.")
        object.__setattr__(
            self,
            "expiry_date",
            _validate_non_blank(self.expiry_date, "expiry_date"),
        )
        object.__setattr__(self, "start_at", _validate_utc_datetime(self.start_at, "start_at"))
        object.__setattr__(self, "end_at", _validate_utc_datetime(self.end_at, "end_at"))
        if self.start_at >= self.end_at:
            raise MarketContextError("option bar start_at must be before end_at.")
        for field_name in ("strike_price", "close_price", "volume", "vwap"):
            _validate_optional_finite(getattr(self, field_name), field_name)
        if self.strike_price <= 0 or self.close_price <= 0 or self.volume < 0:
            raise MarketContextError("option bar prices must be positive and volume non-negative.")
        if self.trade_count is not None:
            object.__setattr__(
                self,
                "trade_count",
                _validate_non_negative_int(self.trade_count, "trade_count"),
            )


@dataclass(frozen=True, slots=True)
class OptionContractActivity:
    """Compact prompt-facing activity for one selected option contract."""

    contract_symbol: str
    option_type: str
    expiry_date: str
    strike_price: float
    volume: float
    trade_count: int
    notional: float
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "contract_symbol",
            _validate_non_blank(self.contract_symbol, "contract_symbol"),
        )
        if self.option_type not in {"call", "put"}:
            raise MarketContextError("option_type must be call or put.")
        object.__setattr__(
            self,
            "expiry_date",
            _validate_non_blank(self.expiry_date, "expiry_date"),
        )
        for field_name in (
            "strike_price",
            "volume",
            "notional",
            "implied_volatility",
            "delta",
            "gamma",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "trade_count",
            _validate_non_negative_int(self.trade_count, "trade_count"),
        )


@dataclass(frozen=True, slots=True)
class OptionNotableContract:
    """Compact unusual-activity signal for one selected contract."""

    contract_symbol: str
    option_type: str
    expiry_date: str
    strike_price: float
    expiry_days: int
    unusual_label: str
    max_unusual_ratio: float
    unusual_ratio_volume: float | None
    unusual_ratio_notional: float | None
    unusual_ratio_trade_count: float | None
    notional: float
    volume: float
    trade_count: int
    near_expiry: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "contract_symbol",
            _validate_non_blank(self.contract_symbol, "contract_symbol"),
        )
        if self.option_type not in {"call", "put"}:
            raise MarketContextError("option_type must be call or put.")
        object.__setattr__(
            self,
            "expiry_date",
            _validate_non_blank(self.expiry_date, "expiry_date"),
        )
        _validate_optional_finite(self.strike_price, "strike_price")
        if self.strike_price <= 0:
            raise MarketContextError("strike_price must be positive.")
        object.__setattr__(
            self,
            "expiry_days",
            _validate_non_negative_int(self.expiry_days, "expiry_days"),
        )
        object.__setattr__(
            self,
            "unusual_label",
            _validate_non_blank(self.unusual_label, "unusual_label"),
        )
        _validate_optional_finite(self.unusual_ratio_volume, "unusual_ratio_volume")
        _validate_optional_finite(self.unusual_ratio_notional, "unusual_ratio_notional")
        _validate_optional_finite(
            self.unusual_ratio_trade_count,
            "unusual_ratio_trade_count",
        )
        _validate_optional_finite(self.max_unusual_ratio, "max_unusual_ratio")
        if self.max_unusual_ratio < 0:
            raise MarketContextError("max_unusual_ratio must be non-negative.")
        for field_name in (
            "notional",
            "volume",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "trade_count",
            _validate_non_negative_int(self.trade_count, "trade_count"),
        )
        if not isinstance(self.near_expiry, bool):
            raise MarketContextError("near_expiry must be a boolean.")


@dataclass(frozen=True, slots=True)
class OptionUnusualActivitySummary:
    """Compact unusual-activity aggregate for selected contracts."""

    status: str
    baseline_windows_days: tuple[int, ...]
    baseline_min_observations: int
    sufficient_baseline_contract_count: int
    insufficient_baseline_contract_count: int
    unusual_contract_count: int
    elevated_contract_count: int
    normal_contract_count: int
    aggregate_volume: float
    aggregate_notional: float
    aggregate_trade_count: int
    max_unusual_ratio: float | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            _validate_non_blank(self.status, "status"),
        )
        object.__setattr__(
            self,
            "baseline_windows_days",
            _normalize_int_tuple(
                self.baseline_windows_days,
                field_name="baseline_windows_days",
            ),
        )
        for field_name in (
            "baseline_min_observations",
            "sufficient_baseline_contract_count",
            "insufficient_baseline_contract_count",
            "unusual_contract_count",
            "elevated_contract_count",
            "normal_contract_count",
            "aggregate_trade_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_non_negative_int(getattr(self, field_name), field_name),
            )
        for field_name in (
            "aggregate_volume",
            "aggregate_notional",
            "max_unusual_ratio",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
@dataclass(frozen=True, slots=True)
class OptionVolumeSummary:
    """Aggregate option volume and notional facts."""

    total_option_volume: float
    call_volume: float
    put_volume: float
    call_put_volume_ratio: float | None
    total_trade_count: int
    total_notional: float
    near_expiry_volume: float
    atm_volume: float
    otm_volume: float

    def __post_init__(self) -> None:
        for field_name in (
            "total_option_volume",
            "call_volume",
            "put_volume",
            "call_put_volume_ratio",
            "total_notional",
            "near_expiry_volume",
            "atm_volume",
            "otm_volume",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "total_trade_count",
            _validate_non_negative_int(self.total_trade_count, "total_trade_count"),
        )


@dataclass(frozen=True, slots=True)
class OptionActivitySummary:
    """Aggregate option activity facts from selected visible observations."""

    large_trade_count: int
    large_trade_notional: float
    selected_contract_count: int
    eligible_contract_count: int
    filtered_low_trade_count: int
    source_contract_count: int
    visible_trade_count: int
    visible_bar_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "large_trade_count",
            "selected_contract_count",
            "eligible_contract_count",
            "filtered_low_trade_count",
            "source_contract_count",
            "visible_trade_count",
            "visible_bar_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_non_negative_int(getattr(self, field_name), field_name),
            )
        _validate_optional_finite(self.large_trade_notional, "large_trade_notional")


@dataclass(frozen=True, slots=True)
class OptionIvGreeksSummary:
    """Compact IV and Greeks availability/activity summary."""

    iv_min: float | None
    iv_max: float | None
    iv_median: float | None
    iv_available_count: int
    greeks_available_count: int
    gamma_available_count: int
    delta_weighted_activity: float | None
    gamma_weighted_activity: float | None
    strict_gex_status: str

    def __post_init__(self) -> None:
        for field_name in (
            "iv_min",
            "iv_max",
            "iv_median",
            "delta_weighted_activity",
            "gamma_weighted_activity",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
        for field_name in (
            "iv_available_count",
            "greeks_available_count",
            "gamma_available_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_non_negative_int(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "strict_gex_status",
            _validate_non_blank(self.strict_gex_status, "strict_gex_status"),
        )


@dataclass(frozen=True, slots=True)
class OptionContextAvailability:
    """Replay-safe status surface for the derivatives component."""

    chain_status: str
    trades_status: str
    bars_status: str
    latest_trades_status: str
    latest_quotes_status: str
    historical_universe_status: str
    unusual_activity_status: str
    iv_status: str
    greeks_status: str
    quote_status: str

    def __post_init__(self) -> None:
        for field_name in (
            "chain_status",
            "trades_status",
            "bars_status",
            "latest_trades_status",
            "latest_quotes_status",
            "historical_universe_status",
            "unusual_activity_status",
            "iv_status",
            "greeks_status",
            "quote_status",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_non_blank(getattr(self, field_name), field_name),
            )


@dataclass(frozen=True, slots=True)
class DerivativesContext:
    """Compact deterministic derivatives facts for one target snapshot."""

    underlying_symbol: str
    underlying_price: float | None
    policy: OptionSelectionPolicy
    availability: OptionContextAvailability
    volume_summary: OptionVolumeSummary | None
    activity_summary: OptionActivitySummary | None
    iv_greeks_summary: OptionIvGreeksSummary | None
    top_contracts_by_volume: tuple[OptionContractActivity, ...]
    top_contracts_by_notional: tuple[OptionContractActivity, ...]
    top_contracts_by_trade_count: tuple[OptionContractActivity, ...]
    notable_contracts: tuple[OptionNotableContract, ...]
    unusual_activity_summary: OptionUnusualActivitySummary | None
    selected_contract_symbols: tuple[str, ...]
    field_status: dict[str, str]
    status: MarketContextComponentStatus

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "underlying_symbol",
            _validate_non_blank(self.underlying_symbol, "underlying_symbol"),
        )
        _validate_optional_finite(self.underlying_price, "underlying_price")
        if self.policy is not None and not isinstance(self.policy, OptionSelectionPolicy):
            raise MarketContextError("policy must be an OptionSelectionPolicy.")
        if not isinstance(self.availability, OptionContextAvailability):
            raise MarketContextError("availability must be an OptionContextAvailability.")
        if self.volume_summary is not None and not isinstance(
            self.volume_summary,
            OptionVolumeSummary,
        ):
            raise MarketContextError("volume_summary must be an OptionVolumeSummary.")
        if self.activity_summary is not None and not isinstance(
            self.activity_summary,
            OptionActivitySummary,
        ):
            raise MarketContextError("activity_summary must be an OptionActivitySummary.")
        if self.iv_greeks_summary is not None and not isinstance(
            self.iv_greeks_summary,
            OptionIvGreeksSummary,
        ):
            raise MarketContextError("iv_greeks_summary must be an OptionIvGreeksSummary.")
        for field_name in (
            "top_contracts_by_volume",
            "top_contracts_by_notional",
            "top_contracts_by_trade_count",
        ):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(
                not isinstance(item, OptionContractActivity) for item in values
            ):
                raise MarketContextError(f"{field_name} must be option activity tuple.")
        if (
            not isinstance(self.notable_contracts, tuple)
            or any(
                not isinstance(item, OptionNotableContract) for item in self.notable_contracts
            )
        ):
            raise MarketContextError("notable_contracts must be an OptionNotableContract tuple.")
        if self.unusual_activity_summary is not None and not isinstance(
            self.unusual_activity_summary,
            OptionUnusualActivitySummary,
        ):
            raise MarketContextError(
                "unusual_activity_summary must be an OptionUnusualActivitySummary."
            )
        object.__setattr__(
            self,
            "selected_contract_symbols",
            _validate_string_tuple(
                self.selected_contract_symbols,
                field_name="selected_contract_symbols",
            ),
        )
        if not isinstance(self.field_status, dict):
            raise MarketContextError("field_status must be a dict.")
        if not isinstance(self.status, MarketContextComponentStatus):
            raise MarketContextError("status must be a MarketContextComponentStatus.")


@dataclass(frozen=True, slots=True)
class MacroCrossAssetPolicy:
    """Profile-driven policy for deterministic cross-asset proxy context."""

    lookback_bars: int
    trailing_return_windows: tuple[str, ...]
    proxy_groups: dict[str, tuple[str, ...]]
    group_pressure_rules: dict[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "lookback_bars",
            _validate_positive_int(self.lookback_bars, "lookback_bars"),
        )
        object.__setattr__(
            self,
            "trailing_return_windows",
            _validate_non_blank_tuple(
                self.trailing_return_windows,
                field_name="trailing_return_windows",
            ),
        )
        if not isinstance(self.proxy_groups, dict) or not self.proxy_groups:
            raise MarketContextError("proxy_groups must be a non-empty dict.")
        normalized: dict[str, tuple[str, ...]] = {}
        for raw_group, raw_symbols in self.proxy_groups.items():
            group = _validate_non_blank(raw_group, "proxy_group")
            normalized[group] = _validate_non_blank_tuple(
                raw_symbols,
                field_name=f"proxy_groups.{group}",
            )
        object.__setattr__(self, "proxy_groups", normalized)
        if not isinstance(self.group_pressure_rules, dict):
            raise MarketContextError("group_pressure_rules must be a dict.")
        pressure_rules: dict[str, str] = {}
        for raw_group, raw_rule in self.group_pressure_rules.items():
            group = _validate_non_blank(raw_group, "group_pressure_rule")
            rule = _validate_non_blank(raw_rule, f"group_pressure_rules.{group}")
            if rule not in (
                "positive_supportive",
                "positive_headwind",
                "contextual",
            ):
                raise MarketContextError(
                    "group_pressure_rules values must be positive_supportive, "
                    "positive_headwind, or contextual."
                )
            pressure_rules[group] = rule
        object.__setattr__(self, "group_pressure_rules", pressure_rules)

    @classmethod
    def from_config(cls, config: Any) -> MacroCrossAssetPolicy:
        return cls(
            lookback_bars=config.lookback_bars,
            trailing_return_windows=tuple(config.trailing_return_windows),
            proxy_groups={
                group: tuple(symbols)
                for group, symbols in config.proxy_groups.items()
            },
            group_pressure_rules=dict(config.group_pressure_rules),
        )


@dataclass(frozen=True, slots=True)
class MacroCrossAssetProxySummary:
    """Compact completed-bar summary for one configured cross-asset proxy."""

    group: str
    symbol: str
    latest_completed_bar_end_at: datetime | None
    latest_close: float | None
    trailing_return_1d: float | None
    trailing_return_5d: float | None
    trailing_return_20d: float | None
    relative_return_1d: float | None
    relative_return_5d: float | None
    relative_return_20d: float | None
    realized_volatility: float | None
    lag_seconds: float | None
    status: MarketContextComponentStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "group", _validate_non_blank(self.group, "group"))
        object.__setattr__(self, "symbol", _validate_non_blank(self.symbol, "symbol"))
        if self.latest_completed_bar_end_at is not None:
            object.__setattr__(
                self,
                "latest_completed_bar_end_at",
                _validate_utc_datetime(
                    self.latest_completed_bar_end_at,
                    "latest_completed_bar_end_at",
                ),
            )
        for field_name in (
            "latest_close",
            "trailing_return_1d",
            "trailing_return_5d",
            "trailing_return_20d",
            "relative_return_1d",
            "relative_return_5d",
            "relative_return_20d",
            "realized_volatility",
            "lag_seconds",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
        if not isinstance(self.status, MarketContextComponentStatus):
            raise MarketContextError("status must be a MarketContextComponentStatus.")


@dataclass(frozen=True, slots=True)
class MacroCrossAssetGroupSummary:
    """Compact aggregate label for one configured proxy group."""

    group: str
    proxy_count: int
    available_proxy_count: int
    average_return_1d: float | None
    average_return_5d: float | None
    average_return_20d: float | None
    average_relative_return_1d: float | None
    pressure_label: str
    status: MarketContextComponentStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "group", _validate_non_blank(self.group, "group"))
        object.__setattr__(
            self,
            "proxy_count",
            _validate_non_negative_int(self.proxy_count, "proxy_count"),
        )
        object.__setattr__(
            self,
            "available_proxy_count",
            _validate_non_negative_int(
                self.available_proxy_count,
                "available_proxy_count",
            ),
        )
        for field_name in (
            "average_return_1d",
            "average_return_5d",
            "average_return_20d",
            "average_relative_return_1d",
        ):
            _validate_optional_finite(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "pressure_label",
            _validate_non_blank(self.pressure_label, "pressure_label"),
        )
        if not isinstance(self.status, MarketContextComponentStatus):
            raise MarketContextError("status must be a MarketContextComponentStatus.")


@dataclass(frozen=True, slots=True)
class MacroCrossAssetContext:
    """Compact deterministic cross-asset facts for one target snapshot."""

    policy: MacroCrossAssetPolicy
    proxy_summaries: tuple[MacroCrossAssetProxySummary, ...]
    group_summaries: tuple[MacroCrossAssetGroupSummary, ...]
    usd_pressure: str
    rates_pressure: str
    risk_regime: str
    cross_asset_confirmation: str
    divergence_notes: tuple[str, ...]
    field_status: dict[str, str]
    status: MarketContextComponentStatus

    def __post_init__(self) -> None:
        if not isinstance(self.policy, MacroCrossAssetPolicy):
            raise MarketContextError("policy must be a MacroCrossAssetPolicy.")
        if not isinstance(self.proxy_summaries, tuple) or any(
            not isinstance(item, MacroCrossAssetProxySummary)
            for item in self.proxy_summaries
        ):
            raise MarketContextError(
                "proxy_summaries must be a MacroCrossAssetProxySummary tuple."
            )
        if not isinstance(self.group_summaries, tuple) or any(
            not isinstance(item, MacroCrossAssetGroupSummary)
            for item in self.group_summaries
        ):
            raise MarketContextError(
                "group_summaries must be a MacroCrossAssetGroupSummary tuple."
            )
        for field_name in (
            "usd_pressure",
            "rates_pressure",
            "risk_regime",
            "cross_asset_confirmation",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_non_blank(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "divergence_notes",
            _validate_string_tuple(
                self.divergence_notes,
                field_name="divergence_notes",
            ),
        )
        if not isinstance(self.field_status, dict):
            raise MarketContextError("field_status must be a dict.")
        if not isinstance(self.status, MarketContextComponentStatus):
            raise MarketContextError("status must be a MarketContextComponentStatus.")


@dataclass(frozen=True, slots=True)
class MarketContextSnapshot:
    """Shared deterministic market facts visible to checker and analysis."""

    target_key: str
    as_of_at: datetime
    tradable_proxy_symbol: str
    bar_granularity: str
    components: tuple[str, ...]
    availability: dict[str, str]
    staleness: dict[str, object]
    source_metadata: dict[str, object]
    calculation_versions: dict[str, str]
    price_volume: PriceVolumeContext | None = None
    market_path: MarketPathContext | None = None
    technical: TechnicalContext | None = None
    derivatives: DerivativesContext | None = None
    macro_cross_asset: MacroCrossAssetContext | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=MarketContextError),
        )
        object.__setattr__(self, "as_of_at", _validate_utc_datetime(self.as_of_at, "as_of_at"))
        object.__setattr__(
            self,
            "tradable_proxy_symbol",
            _validate_non_blank(self.tradable_proxy_symbol, "tradable_proxy_symbol"),
        )
        object.__setattr__(
            self,
            "bar_granularity",
            _validate_non_blank(self.bar_granularity, "bar_granularity"),
        )
        object.__setattr__(
            self,
            "components",
            _validate_string_tuple(self.components, field_name="components"),
        )
        for field_name in ("availability", "staleness", "source_metadata", "calculation_versions"):
            if not isinstance(getattr(self, field_name), dict):
                raise MarketContextError(f"{field_name} must be a dict.")
        if self.price_volume is not None and not isinstance(
            self.price_volume,
            PriceVolumeContext,
        ):
            raise MarketContextError("price_volume must be a PriceVolumeContext.")
        if self.market_path is not None and not isinstance(
            self.market_path,
            MarketPathContext,
        ):
            raise MarketContextError("market_path must be a MarketPathContext.")
        if self.technical is not None and not isinstance(self.technical, TechnicalContext):
            raise MarketContextError("technical must be a TechnicalContext.")
        if self.derivatives is not None and not isinstance(
            self.derivatives,
            DerivativesContext,
        ):
            raise MarketContextError("derivatives must be a DerivativesContext.")
        if self.macro_cross_asset is not None and not isinstance(
            self.macro_cross_asset,
            MacroCrossAssetContext,
        ):
            raise MarketContextError(
                "macro_cross_asset must be a MacroCrossAssetContext."
            )

    def to_dict(self) -> dict[str, object]:
        """Return canonical JSON-serializable snapshot payload plus audit hash."""
        payload = _to_jsonable(self)
        if not isinstance(payload, dict):
            raise MarketContextError("snapshot serialization must produce a dict.")
        payload["market_context_hash"] = self.payload_hash()
        return payload

    def to_prompt_dict(self, *, max_chars: int) -> dict[str, object]:
        """Return a prompt-safe payload bounded by the configured character budget."""
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
            raise MarketContextError("max_chars must be a positive integer.")
        full_payload = self.to_dict()
        encoded = _canonical_json(full_payload)
        if len(encoded) <= max_chars:
            return {
                "payload": full_payload,
                "sha256": sha256(encoded.encode("utf-8")).hexdigest(),
                "char_count": len(encoded),
                "truncated": False,
            }
        compact_payload = {
            **self.compact_audit(),
            "prompt_payload_truncated_reason": "market context exceeded max_prompt_chars",
        }
        compact_encoded = _canonical_json(compact_payload)
        return {
            "payload": compact_payload,
            "sha256": sha256(encoded.encode("utf-8")).hexdigest(),
            "char_count": len(encoded),
            "truncated": True,
            "compact_char_count": len(compact_encoded),
        }

    def compact_audit(self) -> dict[str, object]:
        """Return the compact audit surface used by runtime receipts."""
        market_session_identity = _compact_market_session_identity(self.source_metadata)
        latest_end = None
        if self.price_volume is not None:
            latest_end = self.price_volume.latest_completed_bar_end_at
        derivatives_status = None
        selected_option_contract_count = None
        strict_gex_status = None
        if self.derivatives is not None:
            derivatives_status = self.derivatives.status.status
            selected_option_contract_count = len(self.derivatives.selected_contract_symbols)
            if self.derivatives.iv_greeks_summary is not None:
                strict_gex_status = self.derivatives.iv_greeks_summary.strict_gex_status
        technical_summary = None
        if self.technical is not None:
            technical_summary = {
                "status": self.technical.technical_context_status.status,
                "ema_136": _to_jsonable(self.technical.ema_136),
                "price_vs_ema_136": self.technical.price_vs_ema_136,
                "ema_136_slope": self.technical.ema_136_slope,
                "ichimoku": {
                    "tenkan": self.technical.ichimoku_tenkan,
                    "kijun": self.technical.ichimoku_kijun,
                    "senkou_a_known_at_as_of": (
                        self.technical.ichimoku_senkou_a_known_at_as_of
                    ),
                    "senkou_b_known_at_as_of": (
                        self.technical.ichimoku_senkou_b_known_at_as_of
                    ),
                    "cloud_regime": self.technical.ichimoku_cloud_regime,
                    "price_location": self.technical.ichimoku_price_location,
                },
                "bollinger_bands": _to_jsonable(self.technical.bollinger_bands),
                "rsi": _to_jsonable(self.technical.rsi),
                "divergence": _to_jsonable(self.technical.divergence),
            }
        market_path_summary = None
        if self.market_path is not None:
            market_path_summary = {
                "range_coverage_status": self.market_path.range_path.coverage_status,
                "range_available_bar_count": (
                    self.market_path.range_path.available_bar_count
                ),
                "latest_price": self.market_path.range_path.latest_price,
                "window_low": self.market_path.range_path.window_low,
                "window_high": self.market_path.range_path.window_high,
                "move_from_window_low_pct": (
                    self.market_path.range_path.move_from_window_low_pct
                ),
                "move_from_window_high_pct": (
                    self.market_path.range_path.move_from_window_high_pct
                ),
                "swing_status": self.market_path.swing_path.status,
                "confirmed_pivot_count": len(self.market_path.swing_path.confirmed_pivots),
                "current_leg": _to_jsonable(self.market_path.swing_path.current_leg),
                "path_flags": list(self.market_path.path_flags),
            }
        derivatives_summary = None
        if self.derivatives is not None:
            volume_summary = self.derivatives.volume_summary
            iv_greeks = self.derivatives.iv_greeks_summary
            activity_summary = self.derivatives.activity_summary
            unusual_activity_summary = self.derivatives.unusual_activity_summary
            notable_limit = self.derivatives.policy.notable_contract_limit
            derivatives_summary = {
                "underlying_symbol": self.derivatives.underlying_symbol,
                "status": self.derivatives.status.status,
                "coverage": {
                    "source_contract_count": (
                        activity_summary.source_contract_count
                        if activity_summary is not None
                        else None
                    ),
                    "selected_contract_count": (
                        activity_summary.selected_contract_count
                        if activity_summary is not None
                        else None
                    ),
                    "visible_trade_count": (
                        activity_summary.visible_trade_count
                        if activity_summary is not None
                        else None
                    ),
                    "visible_bar_count": (
                        activity_summary.visible_bar_count
                        if activity_summary is not None
                        else None
                    ),
                    "filtered_low_trade_count": (
                        activity_summary.filtered_low_trade_count
                        if activity_summary is not None
                        else None
                    ),
                },
                "aggregate_activity": (
                    {
                        "total_option_volume": (
                            volume_summary.total_option_volume
                            if volume_summary is not None
                            else None
                        ),
                        "total_notional": (
                            volume_summary.total_notional
                            if volume_summary is not None
                            else None
                        ),
                        "total_trade_count": (
                            volume_summary.total_trade_count
                            if volume_summary is not None
                            else None
                        ),
                        "call_put_ratio": (
                            volume_summary.call_put_volume_ratio
                            if volume_summary is not None
                            else None
                        ),
                        "large_trade_count": (
                            activity_summary.large_trade_count
                            if activity_summary is not None
                            else None
                        ),
                        "large_trade_notional": (
                            activity_summary.large_trade_notional
                            if activity_summary is not None
                            else None
                        ),
                    }
                    if activity_summary is not None
                    else None
                ),
                "unusual_activity": _to_jsonable(unusual_activity_summary)
                if unusual_activity_summary is not None
                else None,
                "notable_contracts": [
                    {
                        "contract_symbol": item.contract_symbol,
                        "unusual_label": item.unusual_label,
                        "max_unusual_ratio": item.max_unusual_ratio,
                        "notional": item.notional,
                        "volume": item.volume,
                        "trade_count": item.trade_count,
                        "near_expiry": item.near_expiry,
                        "expiry_days": item.expiry_days,
                    }
                    for item in self.derivatives.notable_contracts[:notable_limit]
                ],
                "top_contracts_by_volume": [
                    item.contract_symbol
                    for item in self.derivatives.top_contracts_by_volume[:notable_limit]
                ],
                "top_contracts_by_notional": [
                    item.contract_symbol
                    for item in self.derivatives.top_contracts_by_notional[:notable_limit]
                ],
                "top_contracts_by_trade_count": [
                    item.contract_symbol
                    for item in self.derivatives.top_contracts_by_trade_count[
                        :notable_limit
                    ]
                ],
                "iv_available_count": (
                    iv_greeks.iv_available_count if iv_greeks is not None else None
                ),
                "greeks_available_count": (
                    iv_greeks.greeks_available_count if iv_greeks is not None else None
                ),
                "gamma_weighted_activity": (
                    iv_greeks.gamma_weighted_activity if iv_greeks is not None else None
                ),
                "strict_gex_status": strict_gex_status,
            }
        macro_summary = None
        if self.macro_cross_asset is not None:
            macro_summary = {
                "status": self.macro_cross_asset.status.status,
                "proxy_count": len(self.macro_cross_asset.proxy_summaries),
                "available_proxy_count": sum(
                    1
                    for item in self.macro_cross_asset.proxy_summaries
                    if item.status.status == "available"
                ),
                "usd_pressure": self.macro_cross_asset.usd_pressure,
                "rates_pressure": self.macro_cross_asset.rates_pressure,
                "risk_regime": self.macro_cross_asset.risk_regime,
                "cross_asset_confirmation": (
                    self.macro_cross_asset.cross_asset_confirmation
                ),
                "divergence_notes": list(
                    self.macro_cross_asset.divergence_notes[:5]
                ),
            }
        return {
            "target_key": self.target_key,
            "as_of_at": self.as_of_at.isoformat(),
            "tradable_proxy_symbol": self.tradable_proxy_symbol,
            "bar_granularity": self.bar_granularity,
            "market_session_identity": market_session_identity,
            "component_statuses": dict(sorted(self.availability.items())),
            "latest_completed_bar_end_at": (
                latest_end.isoformat() if latest_end is not None else None
            ),
            "derivatives_status": derivatives_status,
            "selected_option_contract_count": selected_option_contract_count,
            "strict_gex_status": strict_gex_status,
            "market_path_summary": market_path_summary,
            "technical_summary": technical_summary,
            "derivatives_summary": derivatives_summary,
            "macro_cross_asset_summary": macro_summary,
            "calculation_versions": dict(sorted(self.calculation_versions.items())),
            "market_context_hash": self.payload_hash(),
        }

    def payload_hash(self) -> str:
        payload = _to_jsonable(self)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


def _to_jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {
            field_name: _to_jsonable(getattr(value, field_name))
            for field_name in value.__dataclass_fields__
        }
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _to_jsonable(raw_value)
            for key, raw_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return value


def _compact_market_session_identity(
    source_metadata: dict[str, object],
) -> dict[str, object]:
    return compact_market_session_identity(source_metadata)


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_non_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise MarketContextError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MarketContextError(f"{field_name} must not be blank.")
    return normalized


def _validate_non_blank_tuple(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    normalized = _validate_string_tuple(values, field_name=field_name)
    if not normalized:
        raise MarketContextError(f"{field_name} must not be empty.")
    return normalized


def _validate_string_tuple(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise MarketContextError(f"{field_name} must be a tuple.")
    normalized: list[str] = []
    for value in values:
        normalized.append(_validate_non_blank(value, field_name))
    return tuple(normalized)


def _validate_positive_int_tuple(values: tuple[int, ...], field_name: str) -> tuple[int, ...]:
    if not isinstance(values, tuple) or not values:
        raise MarketContextError(f"{field_name} must be a non-empty tuple.")
    return tuple(_validate_positive_int(value, field_name) for value in values)


def _normalize_int_tuple(
    values: Any,
    *,
    field_name: str,
    min_value: int = 1,
) -> tuple[int, ...]:
    if isinstance(values, tuple):
        raw = values
    elif isinstance(values, list):
        raw = tuple(values)
    else:
        raise MarketContextError(f"{field_name} must be a tuple of integers.")
    if not raw:
        raise MarketContextError(f"{field_name} must be a non-empty tuple.")
    if not isinstance(min_value, int) or min_value < 0:
        raise MarketContextError("min_value must be a non-negative integer.")
    normalized = sorted(set(raw))
    filtered = tuple(
        value
        for value in (
            _validate_non_negative_int(value, field_name)
            for value in normalized
        )
        if value >= min_value
    )
    if not filtered:
        raise MarketContextError(f"{field_name} must include positive values.")
    return filtered


def _validate_positive_int(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MarketContextError(f"{field_name} must be a positive integer.")
    return value


def _validate_non_negative_int(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MarketContextError(f"{field_name} must be a non-negative integer.")
    return value


def _validate_optional_finite(value: float | None, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, int | float) or isinstance(value, bool) or not isfinite(value):
        raise MarketContextError(f"{field_name} must be finite when present.")


def _validate_utc_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise MarketContextError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketContextError(f"{field_name} must be timezone-aware UTC.")
    if value.utcoffset() != UTC.utcoffset(None):
        raise MarketContextError(f"{field_name} must be timezone-aware UTC.")
    return value.astimezone(UTC)


__all__ = [
    "BollingerBandsFeature",
    "CausalPivot",
    "CausalSwingSnapshot",
    "CurrentLeg",
    "DerivativesContext",
    "EmaFeature",
    "IchimokuFeature",
    "MacroCrossAssetContext",
    "MacroCrossAssetGroupSummary",
    "MacroCrossAssetPolicy",
    "MacroCrossAssetProxySummary",
    "MarketContextComponentStatus",
    "MarketContextError",
    "MarketContextProfile",
    "MarketContextSnapshot",
    "MarketPathContext",
    "OptionActivitySummary",
    "OptionNotableContract",
    "OptionUnusualActivitySummary",
    "OptionBarObservation",
    "OptionContextAvailability",
    "OptionContractActivity",
    "OptionContractSnapshot",
    "OptionIvGreeksSummary",
    "OptionSelectionPolicy",
    "OptionTradeObservation",
    "OptionVolumeSummary",
    "PriceVolumeContext",
    "RangePathSnapshot",
    "RsiFeature",
    "TechnicalContext",
    "TechnicalDivergenceFeature",
]
