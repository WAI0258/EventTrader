"""Deterministic market-context terminal reads for future agent tools."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from event_trader.config import KernelConfig, MarketContextTargetProfileConfig
from event_trader.contracts.view_state_change import MarketDataBar, MarketMapping
from event_trader.market.adjustments import (
    MarketDataAdjustmentPolicy,
    apply_adjustment_policy_to_bars,
    load_adjustment_sidecar_from_source_metadata,
)
from event_trader.market.context_builder import build_market_context_snapshot
from event_trader.market.contracts import (
    DerivativesContext,
    MacroCrossAssetContext,
    MarketContextComponentStatus,
    MarketContextError,
    MarketContextProfile,
    MarketContextSnapshot,
    TechnicalContext,
)
from event_trader.market.subscriptions import resolve_market_context_mapping_for_symbol
from event_trader.market.provider import (
    MarketBarsProvider,
    build_default_market_bars_provider,
)

_TERMINAL_TOOLS = (
    "read_market_overview",
    "read_price_volume_window",
    "read_technical_panel",
    "read_option_activity",
    "read_cross_asset_context",
    "read_cross_asset_window",
)
_TERMINAL_CONSTRAINTS = {
    "as_of_visibility_enforced": True,
    "bounded_results": True,
    "raw_dump_allowed": False,
}


class MarketContextTerminalError(ValueError):
    """Raised when market terminal inputs are invalid."""


@dataclass(frozen=True, slots=True)
class MarketTerminalAvailabilityHeader:
    target_key: str
    as_of_at: datetime
    available: bool
    tradable_proxy_symbol: str | None
    bar_granularity: str | None
    enabled_components: tuple[str, ...]
    available_tools: tuple[str, ...]
    constraints: dict[str, bool]
    unavailable_reason: str | None

    def to_dict(self) -> dict[str, object]:
        return _jsonable_dict(self)


@dataclass(frozen=True, slots=True)
class MarketTerminalOverview:
    target_key: str
    as_of_at: datetime
    tradable_proxy_symbol: str | None
    bar_granularity: str | None
    availability: dict[str, object]
    staleness: dict[str, object]
    latest_completed_bar_end_at: datetime | None
    latest_close: float | None
    trailing_return_1d: float | None
    trailing_return_5d: float | None
    trailing_return_20d: float | None
    component_status: dict[str, object]
    source_metadata_summary: dict[str, object]
    market_context_hash: str | None

    def to_dict(self) -> dict[str, object]:
        return _jsonable_dict(self)


@dataclass(frozen=True, slots=True)
class MarketTerminalPriceVolumeWindow:
    target_key: str
    as_of_at: datetime
    symbol: str | None
    granularity: str | None
    lookback_start_at: datetime
    latest_visible_bar_end_at: datetime | None
    row_count: int
    truncated: bool
    bars: tuple[dict[str, object], ...]
    status: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return _jsonable_dict(self)


@dataclass(frozen=True, slots=True)
class MarketTerminalTechnicalPanel:
    target_key: str
    as_of_at: datetime
    symbol: str | None
    status: dict[str, object]
    ema_136: dict[str, object] | None
    price_vs_ema_136: float | None
    ema_136_slope: float | None
    ichimoku: dict[str, object]
    bollinger_bands: dict[str, object] | None
    rsi: dict[str, object] | None
    divergence: dict[str, object] | None
    technical_trend_label: str | None
    field_status: dict[str, object]
    calculation_versions: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return _jsonable_dict(self)


@dataclass(frozen=True, slots=True)
class MarketTerminalOptionActivity:
    target_key: str
    as_of_at: datetime
    underlying_symbol: str | None
    underlying_price: float | None
    effective_lookback_hours: int | None
    status: dict[str, object]
    availability: dict[str, object] | None
    volume_summary: dict[str, object] | None
    activity_summary: dict[str, object] | None
    iv_greeks_summary: dict[str, object] | None
    unusual_activity_summary: dict[str, object] | None
    selected_contract_count: int
    selected_contract_symbols: tuple[str, ...]
    top_contracts_by_volume: tuple[dict[str, object], ...]
    top_contracts_by_notional: tuple[dict[str, object], ...]
    top_contracts_by_trade_count: tuple[dict[str, object], ...]
    notable_contracts: tuple[dict[str, object], ...]
    field_status: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return _jsonable_dict(self)


@dataclass(frozen=True, slots=True)
class MarketTerminalCrossAssetContext:
    target_key: str
    as_of_at: datetime
    status: dict[str, object]
    proxy_summaries: tuple[dict[str, object], ...]
    group_summaries: tuple[dict[str, object], ...]
    usd_pressure: str | None
    rates_pressure: str | None
    risk_regime: str | None
    cross_asset_confirmation: str | None
    divergence_notes: tuple[str, ...]
    field_status: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return _jsonable_dict(self)


@dataclass(frozen=True, slots=True)
class MarketTerminalCrossAssetWindow:
    target_key: str
    as_of_at: datetime
    lookback_start_at: datetime
    max_rows_per_symbol: int
    proxy_windows: tuple[dict[str, object], ...]
    status: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return _jsonable_dict(self)


class MarketContextTerminal:
    """Deterministic shared market terminal service for future tool consumers."""

    def __init__(
        self,
        *,
        config: KernelConfig,
        market_data_provider: MarketBarsProvider | None = None,
    ) -> None:
        if not isinstance(config, KernelConfig):
            raise MarketContextTerminalError("config must be a KernelConfig instance.")
        self._config = config
        self._market_data_provider = market_data_provider

    def availability_header(
        self,
        *,
        target_key: str,
        as_of_at: datetime,
    ) -> MarketTerminalAvailabilityHeader:
        normalized_as_of = _validate_utc_as_of(as_of_at)
        profile, profile_config, unavailable_reason = _load_profile(self._config, target_key)
        return MarketTerminalAvailabilityHeader(
            target_key=target_key,
            as_of_at=normalized_as_of,
            available=profile is not None,
            tradable_proxy_symbol=(
                profile.tradable_proxy_symbol if profile is not None else None
            ),
            bar_granularity=profile.bar_granularity if profile is not None else None,
            enabled_components=profile.enabled_components if profile is not None else (),
            available_tools=_TERMINAL_TOOLS,
            constraints=dict(_TERMINAL_CONSTRAINTS),
            unavailable_reason=unavailable_reason,
        )

    def read_market_overview(
        self,
        *,
        target_key: str,
        as_of_at: datetime,
    ) -> MarketTerminalOverview:
        normalized_as_of = _validate_utc_as_of(as_of_at)
        profile, profile_config, unavailable_reason = _load_profile(self._config, target_key)
        if profile is None:
            return _unavailable_overview(
                target_key=target_key,
                as_of_at=normalized_as_of,
                reason=unavailable_reason or "market_context unavailable",
            )
        snapshot = self._snapshot_or_unavailable(
            target_key=target_key,
            as_of_at=normalized_as_of,
        )
        if snapshot is None:
            return _unavailable_overview(
                target_key=target_key,
                as_of_at=normalized_as_of,
                reason="market_context unavailable",
                profile=profile,
            )
        price_volume = snapshot.price_volume
        return MarketTerminalOverview(
            target_key=target_key,
            as_of_at=normalized_as_of,
            tradable_proxy_symbol=snapshot.tradable_proxy_symbol,
            bar_granularity=snapshot.bar_granularity,
            availability=dict(snapshot.availability),
            staleness=dict(snapshot.staleness),
            latest_completed_bar_end_at=(
                price_volume.latest_completed_bar_end_at
                if price_volume is not None
                else None
            ),
            latest_close=price_volume.latest_close if price_volume is not None else None,
            trailing_return_1d=(
                price_volume.trailing_return_1d if price_volume is not None else None
            ),
            trailing_return_5d=(
                price_volume.trailing_return_5d if price_volume is not None else None
            ),
            trailing_return_20d=(
                price_volume.trailing_return_20d if price_volume is not None else None
            ),
            component_status=dict(snapshot.availability),
            source_metadata_summary=_source_metadata_summary(snapshot),
            market_context_hash=snapshot.payload_hash(),
        )

    def read_price_volume_window(
        self,
        *,
        target_key: str,
        as_of_at: datetime,
        lookback: timedelta,
        granularity: str | None = None,
        max_rows: int = 80,
    ) -> MarketTerminalPriceVolumeWindow:
        normalized_as_of = _validate_utc_as_of(as_of_at)
        if not isinstance(lookback, timedelta) or lookback <= timedelta(0):
            raise MarketContextTerminalError("lookback must be a positive timedelta.")
        if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows <= 0:
            raise MarketContextTerminalError("max_rows must be a positive integer.")
        profile, profile_config, unavailable_reason = _load_profile(self._config, target_key)
        lookback_start = normalized_as_of - lookback
        if profile is None:
            return _unavailable_price_volume_window(
                target_key=target_key,
                as_of_at=normalized_as_of,
                lookback_start_at=lookback_start,
                reason=unavailable_reason or "market_context unavailable",
            )
        resolved_granularity = granularity or profile.bar_granularity
        mapping = _mapping_for_terminal(
            config=self._config,
            profile=profile,
            profile_config=profile_config,
            granularity=resolved_granularity,
        )
        provider = self._market_data_provider or build_default_market_bars_provider(
            self._config
        )
        if provider is None:
            return _unavailable_price_volume_window(
                target_key=target_key,
                as_of_at=normalized_as_of,
                lookback_start_at=lookback_start,
                reason="market data provider unavailable",
                mapping=mapping,
            )
        try:
            visible_bars, source_metadata = _read_visible_bars_with_metadata(
                provider=provider,
                mapping=mapping,
                start_at=lookback_start,
                as_of_at=normalized_as_of,
            )
            visible_bars = _adjust_bars_for_terminal_subscription(
                bars=visible_bars,
                market_symbol=mapping.market_symbol,
                profile_config=profile_config,
                source_metadata=source_metadata,
                reference_at=(visible_bars[-1].end_at if visible_bars else None),
            )
        except Exception as exc:  # pragma: no cover - defensive provider boundary
            return _unavailable_price_volume_window(
                target_key=target_key,
                as_of_at=normalized_as_of,
                lookback_start_at=lookback_start,
                reason=f"market data read failed: {exc}",
                mapping=mapping,
            )
        window_bars = tuple(bar for bar in visible_bars if bar.end_at > lookback_start)
        truncated = len(window_bars) > max_rows
        bounded_bars = window_bars[-max_rows:] if truncated else window_bars
        latest_end = bounded_bars[-1].end_at if bounded_bars else None
        status = _component_status(
            "price_volume_window",
            "available" if bounded_bars else "unavailable",
            None if bounded_bars else "no completed bars visible in lookback",
        )
        return MarketTerminalPriceVolumeWindow(
            target_key=target_key,
            as_of_at=normalized_as_of,
            symbol=mapping.market_symbol,
            granularity=resolved_granularity,
            lookback_start_at=lookback_start,
            latest_visible_bar_end_at=latest_end,
            row_count=len(bounded_bars),
            truncated=truncated,
            bars=tuple(_bar_row(bar) for bar in bounded_bars),
            status=status,
        )

    def read_technical_panel(
        self,
        *,
        target_key: str,
        as_of_at: datetime,
    ) -> MarketTerminalTechnicalPanel:
        normalized_as_of = _validate_utc_as_of(as_of_at)
        profile, profile_config, unavailable_reason = _load_profile(self._config, target_key)
        if profile is None:
            return _unavailable_technical_panel(
                target_key=target_key,
                as_of_at=normalized_as_of,
                reason=unavailable_reason or "market_context unavailable",
            )
        snapshot = self._snapshot_or_unavailable(
            target_key=target_key,
            as_of_at=normalized_as_of,
        )
        if snapshot is None or snapshot.technical is None:
            status = "disabled" if "technical" not in profile.enabled_components else "unavailable"
            reason = (
                "technical component disabled"
                if status == "disabled"
                else "technical context unavailable"
            )
            return _unavailable_technical_panel(
                target_key=target_key,
                as_of_at=normalized_as_of,
                reason=reason,
                profile=profile,
                status=status,
            )
        return _technical_panel_from_context(
            target_key=target_key,
            as_of_at=normalized_as_of,
            snapshot=snapshot,
            technical=snapshot.technical,
        )

    def read_option_activity(
        self,
        *,
        target_key: str,
        as_of_at: datetime,
        max_contracts: int = 10,
    ) -> MarketTerminalOptionActivity:
        normalized_as_of = _validate_utc_as_of(as_of_at)
        if (
            not isinstance(max_contracts, int)
            or isinstance(max_contracts, bool)
            or max_contracts <= 0
        ):
            raise MarketContextTerminalError(
                "max_contracts must be a positive integer."
            )
        profile, profile_config, unavailable_reason = _load_profile(self._config, target_key)
        if profile is None:
            return _unavailable_option_activity(
                target_key=target_key,
                as_of_at=normalized_as_of,
                reason=unavailable_reason or "market_context unavailable",
            )
        snapshot = self._snapshot_or_unavailable(
            target_key=target_key,
            as_of_at=normalized_as_of,
        )
        if snapshot is None or snapshot.derivatives is None:
            status = (
                "disabled"
                if "derivatives" not in profile.enabled_components
                else "unavailable"
            )
            reason = (
                "derivatives component disabled"
                if status == "disabled"
                else "derivatives context unavailable"
            )
            return _unavailable_option_activity(
                target_key=target_key,
                as_of_at=normalized_as_of,
                reason=reason,
                profile=profile,
                status=status,
            )
        return _option_activity_from_context(
            target_key=target_key,
            as_of_at=normalized_as_of,
            derivatives=snapshot.derivatives,
            max_contracts=max_contracts,
        )

    def read_cross_asset_context(
        self,
        *,
        target_key: str,
        as_of_at: datetime,
    ) -> MarketTerminalCrossAssetContext:
        normalized_as_of = _validate_utc_as_of(as_of_at)
        profile, profile_config, unavailable_reason = _load_profile(self._config, target_key)
        if profile is None:
            return _unavailable_cross_asset_context(
                target_key=target_key,
                as_of_at=normalized_as_of,
                reason=unavailable_reason or "market_context unavailable",
            )
        snapshot = self._snapshot_or_unavailable(
            target_key=target_key,
            as_of_at=normalized_as_of,
        )
        if snapshot is None or snapshot.macro_cross_asset is None:
            status = (
                "disabled"
                if "macro_cross_asset" not in profile.enabled_components
                else "unavailable"
            )
            reason = (
                "macro_cross_asset component disabled"
                if status == "disabled"
                else "macro_cross_asset context unavailable"
            )
            return _unavailable_cross_asset_context(
                target_key=target_key,
                as_of_at=normalized_as_of,
                reason=reason,
                status=status,
            )
        return _cross_asset_context_from_context(
            target_key=target_key,
            as_of_at=normalized_as_of,
            macro=snapshot.macro_cross_asset,
        )

    def read_cross_asset_window(
        self,
        *,
        target_key: str,
        as_of_at: datetime,
        lookback: timedelta,
        max_rows_per_symbol: int = 80,
    ) -> MarketTerminalCrossAssetWindow:
        normalized_as_of = _validate_utc_as_of(as_of_at)
        if not isinstance(lookback, timedelta) or lookback <= timedelta(0):
            raise MarketContextTerminalError("lookback must be a positive timedelta.")
        if (
            not isinstance(max_rows_per_symbol, int)
            or isinstance(max_rows_per_symbol, bool)
            or max_rows_per_symbol <= 0
        ):
            raise MarketContextTerminalError(
                "max_rows_per_symbol must be a positive integer."
            )
        lookback_start = normalized_as_of - lookback
        profile, profile_config, unavailable_reason = _load_profile(self._config, target_key)
        if profile is None:
            return _unavailable_cross_asset_window(
                target_key=target_key,
                as_of_at=normalized_as_of,
                lookback_start_at=lookback_start,
                max_rows_per_symbol=max_rows_per_symbol,
                reason=unavailable_reason or "market_context unavailable",
            )
        policy = profile.macro_cross_asset
        if policy is None or "macro_cross_asset" not in profile.enabled_components:
            return _unavailable_cross_asset_window(
                target_key=target_key,
                as_of_at=normalized_as_of,
                lookback_start_at=lookback_start,
                max_rows_per_symbol=max_rows_per_symbol,
                reason="macro_cross_asset component disabled",
                status="disabled",
            )
        provider = self._market_data_provider or build_default_market_bars_provider(
            self._config
        )
        proxy_pairs = tuple(
            (group, symbol)
            for group, symbols in policy.proxy_groups.items()
            for symbol in symbols
        )
        if provider is None:
            return MarketTerminalCrossAssetWindow(
                target_key=target_key,
                as_of_at=normalized_as_of,
                lookback_start_at=lookback_start,
                max_rows_per_symbol=max_rows_per_symbol,
                proxy_windows=tuple(
                    _proxy_window_unavailable(
                        group=group,
                        symbol=symbol,
                        reason="market data provider unavailable",
                    )
                    for group, symbol in proxy_pairs
                ),
                status=_component_status(
                    "cross_asset_window",
                    "unavailable",
                    "market data provider unavailable",
                ),
            )
        proxy_windows = tuple(
            _read_proxy_window(
                provider=provider,
                config=self._config,
                profile=profile,
                profile_config=profile_config,
                group=group,
                symbol=symbol,
                lookback_start_at=lookback_start,
                as_of_at=normalized_as_of,
                max_rows_per_symbol=max_rows_per_symbol,
            )
            for group, symbol in proxy_pairs
        )
        statuses = tuple(_proxy_window_status(window) for window in proxy_windows)
        if statuses and all(status == "available" for status in statuses):
            status_value = "available"
            reason = None
        elif statuses and any(status == "available" for status in statuses):
            status_value = "partial"
            reason = "one or more proxy windows unavailable"
        else:
            status_value = "unavailable"
            reason = "no proxy windows available"
        return MarketTerminalCrossAssetWindow(
            target_key=target_key,
            as_of_at=normalized_as_of,
            lookback_start_at=lookback_start,
            max_rows_per_symbol=max_rows_per_symbol,
            proxy_windows=proxy_windows,
            status=_component_status("cross_asset_window", status_value, reason),
        )

    def _snapshot_or_unavailable(
        self,
        *,
        target_key: str,
        as_of_at: datetime,
    ) -> MarketContextSnapshot | None:
        try:
            return build_market_context_snapshot(
                config=self._config,
                target_key=target_key,
                as_of_at=as_of_at,
                market_data_provider=self._market_data_provider,
            )
        except MarketContextError as exc:
            raise MarketContextTerminalError(str(exc)) from exc


def _validate_utc_as_of(as_of_at: datetime) -> datetime:
    if not isinstance(as_of_at, datetime):
        raise MarketContextTerminalError("as_of_at must be a datetime.")
    if as_of_at.tzinfo is None or as_of_at.utcoffset() is None:
        raise MarketContextTerminalError("as_of_at must be timezone-aware UTC.")
    if as_of_at.utcoffset() != UTC.utcoffset(None):
        raise MarketContextTerminalError("as_of_at must be timezone-aware UTC.")
    return as_of_at.astimezone(UTC)


def _load_profile(
    config: KernelConfig,
    target_key: str,
) -> tuple[
    MarketContextProfile | None,
    MarketContextTargetProfileConfig | None,
    str | None,
]:
    if not isinstance(target_key, str) or not target_key.strip():
        raise MarketContextTerminalError("target_key must be a non-blank string.")
    if config.market_context is None:
        return None, None, "market_context missing"
    if not config.market_context.enabled:
        return None, None, "market_context disabled"
    profile_config = config.market_context.target_profiles.get(target_key)
    if profile_config is None:
        raise MarketContextTerminalError(
            f"target_key {target_key!r} is not configured under market_context."
        )
    try:
        return (
            MarketContextProfile.from_config(
                target_key=target_key,
                profile_config=profile_config,
            ),
            profile_config,
            None,
        )
    except MarketContextError as exc:
        raise MarketContextTerminalError(str(exc)) from exc


def _mapping_for_terminal(
    *,
    config: KernelConfig,
    profile: MarketContextProfile,
    profile_config: MarketContextTargetProfileConfig,
    symbol: str | None = None,
    granularity: str | None = None,
) -> MarketMapping:
    try:
        return resolve_market_context_mapping_for_symbol(
            config=config,
            profile=profile,
            profile_config=profile_config,
            symbol=symbol or profile.tradable_proxy_symbol,
            bar_granularity=granularity,
        )
    except ValueError as exc:
        raise MarketContextTerminalError(str(exc)) from exc


def _read_visible_bars(
    *,
    provider: MarketBarsProvider,
    mapping: MarketMapping,
    start_at: datetime,
    as_of_at: datetime,
) -> tuple[MarketDataBar, ...]:
    visible_bars, _ = _read_visible_bars_with_metadata(
        provider=provider,
        mapping=mapping,
        start_at=start_at,
        as_of_at=as_of_at,
    )
    return visible_bars


def _read_visible_bars_with_metadata(
    *,
    provider: MarketBarsProvider,
    mapping: MarketMapping,
    start_at: datetime,
    as_of_at: datetime,
) -> tuple[tuple[MarketDataBar, ...], dict[str, object]]:
    series = provider.read_series(mapping, start_at=start_at, end_at=as_of_at)
    return (
        tuple(
            sorted(
                (bar for bar in series.bars if bar.end_at <= as_of_at),
                key=lambda bar: (bar.start_at, bar.end_at),
            )
        ),
        _consume_provider_source_metadata(provider),
    )


def _consume_provider_source_metadata(
    provider: MarketBarsProvider | None,
) -> dict[str, object]:
    if provider is None:
        return {}
    consume = getattr(provider, "pop_source_metadata", None)
    if not callable(consume):
        return {}
    payload = consume()
    return payload if isinstance(payload, dict) else {}


def _adjust_bars_for_terminal_subscription(
    *,
    bars: tuple[MarketDataBar, ...],
    market_symbol: str,
    profile_config: MarketContextTargetProfileConfig,
    source_metadata: dict[str, object],
    reference_at: datetime | None,
) -> tuple[MarketDataBar, ...]:
    policy = _market_context_adjustment_policy_for_symbol(
        profile_config=profile_config,
        market_symbol=market_symbol,
    )
    if policy is None or not bars:
        return bars
    sidecar = load_adjustment_sidecar_from_source_metadata(
        market_symbol=market_symbol,
        policy=policy,
        source_metadata=source_metadata,
    )
    adjusted_bars, _ = apply_adjustment_policy_to_bars(
        bars=bars,
        sidecar=sidecar,
        reference_at=reference_at,
    )
    return adjusted_bars


def _market_context_adjustment_policy_for_symbol(
    *,
    profile_config: MarketContextTargetProfileConfig,
    market_symbol: str,
) -> MarketDataAdjustmentPolicy | None:
    for subscription in profile_config.market_data_subscriptions:
        if subscription.symbol == market_symbol:
            return subscription.adjustment_policy
    return None


def _bar_row(bar: MarketDataBar) -> dict[str, object]:
    return {
        "start_at": bar.start_at,
        "end_at": bar.end_at,
        "open": bar.open_price,
        "high": bar.high_price,
        "low": bar.low_price,
        "close": bar.close_price,
        "volume": bar.volume,
        "vwap": bar.vwap,
    }


def _technical_panel_from_context(
    *,
    target_key: str,
    as_of_at: datetime,
    snapshot: MarketContextSnapshot,
    technical: TechnicalContext,
) -> MarketTerminalTechnicalPanel:
    return MarketTerminalTechnicalPanel(
        target_key=target_key,
        as_of_at=as_of_at,
        symbol=snapshot.tradable_proxy_symbol,
        status=_jsonable_dict(technical.technical_context_status),
        ema_136=_jsonable_or_none(technical.ema_136),
        price_vs_ema_136=technical.price_vs_ema_136,
        ema_136_slope=technical.ema_136_slope,
        ichimoku={
            "tenkan": technical.ichimoku_tenkan,
            "kijun": technical.ichimoku_kijun,
            "senkou_a_known_at_as_of": technical.ichimoku_senkou_a_known_at_as_of,
            "senkou_b_known_at_as_of": technical.ichimoku_senkou_b_known_at_as_of,
            "cloud_regime": technical.ichimoku_cloud_regime,
            "price_location": technical.ichimoku_price_location,
        },
        bollinger_bands=_jsonable_or_none(technical.bollinger_bands),
        rsi=_jsonable_or_none(technical.rsi),
        divergence=_jsonable_or_none(technical.divergence),
        technical_trend_label=technical.technical_trend_label,
        field_status=_technical_field_status(technical),
        calculation_versions={
            key: value
            for key, value in snapshot.calculation_versions.items()
            if key
            in {
                "technical",
                "ema",
                "standard_ichimoku",
                "bollinger_bands",
                "rsi",
                "technical_divergence",
            }
        },
    )


def _option_activity_from_context(
    *,
    target_key: str,
    as_of_at: datetime,
    derivatives: DerivativesContext,
    max_contracts: int,
) -> MarketTerminalOptionActivity:
    return MarketTerminalOptionActivity(
        target_key=target_key,
        as_of_at=as_of_at,
        underlying_symbol=derivatives.underlying_symbol,
        underlying_price=derivatives.underlying_price,
        effective_lookback_hours=derivatives.policy.lookback_hours,
        status=_jsonable_dict(derivatives.status),
        availability=_jsonable_dict(derivatives.availability),
        volume_summary=_jsonable_or_none(derivatives.volume_summary),
        activity_summary=_jsonable_or_none(derivatives.activity_summary),
        iv_greeks_summary=_jsonable_or_none(derivatives.iv_greeks_summary),
        unusual_activity_summary=_jsonable_or_none(
            derivatives.unusual_activity_summary
        ),
        selected_contract_count=len(derivatives.selected_contract_symbols),
        selected_contract_symbols=derivatives.selected_contract_symbols[:max_contracts],
        top_contracts_by_volume=tuple(
            _jsonable_dict(item)
            for item in derivatives.top_contracts_by_volume[:max_contracts]
        ),
        top_contracts_by_notional=tuple(
            _jsonable_dict(item)
            for item in derivatives.top_contracts_by_notional[:max_contracts]
        ),
        top_contracts_by_trade_count=tuple(
            _jsonable_dict(item)
            for item in derivatives.top_contracts_by_trade_count[:max_contracts]
        ),
        notable_contracts=tuple(
            _jsonable_dict(item)
            for item in derivatives.notable_contracts[:max_contracts]
        ),
        field_status=dict(derivatives.field_status),
    )


def _cross_asset_context_from_context(
    *,
    target_key: str,
    as_of_at: datetime,
    macro: MacroCrossAssetContext,
) -> MarketTerminalCrossAssetContext:
    return MarketTerminalCrossAssetContext(
        target_key=target_key,
        as_of_at=as_of_at,
        status=_jsonable_dict(macro.status),
        proxy_summaries=tuple(
            _jsonable_dict(item) for item in macro.proxy_summaries
        ),
        group_summaries=tuple(
            _jsonable_dict(item) for item in macro.group_summaries
        ),
        usd_pressure=macro.usd_pressure,
        rates_pressure=macro.rates_pressure,
        risk_regime=macro.risk_regime,
        cross_asset_confirmation=macro.cross_asset_confirmation,
        divergence_notes=macro.divergence_notes,
        field_status=dict(macro.field_status),
    )


def _read_proxy_window(
    *,
    provider: MarketBarsProvider,
    config: KernelConfig,
    profile: MarketContextProfile,
    profile_config: MarketContextTargetProfileConfig,
    group: str,
    symbol: str,
    lookback_start_at: datetime,
    as_of_at: datetime,
    max_rows_per_symbol: int,
) -> dict[str, object]:
    mapping = _mapping_for_terminal(
        config=config,
        profile=profile,
        profile_config=profile_config,
        symbol=symbol,
    )
    try:
        visible_bars, source_metadata = _read_visible_bars_with_metadata(
            provider=provider,
            mapping=mapping,
            start_at=lookback_start_at,
            as_of_at=as_of_at,
        )
        visible_bars = _adjust_bars_for_terminal_subscription(
            bars=visible_bars,
            market_symbol=mapping.market_symbol,
            profile_config=profile_config,
            source_metadata=source_metadata,
            reference_at=(visible_bars[-1].end_at if visible_bars else None),
        )
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        return _proxy_window_unavailable(
            group=group,
            symbol=symbol,
            reason=f"market data read failed: {exc}",
        )
    window_bars = tuple(bar for bar in visible_bars if bar.end_at > lookback_start_at)
    truncated = len(window_bars) > max_rows_per_symbol
    bounded_bars = window_bars[-max_rows_per_symbol:] if truncated else window_bars
    if not bounded_bars:
        return _proxy_window_unavailable(
            group=group,
            symbol=symbol,
            reason="no completed bars visible in lookback",
        )
    return {
        "symbol": symbol,
        "group": group,
        "row_count": len(bounded_bars),
        "truncated": truncated,
        "latest_visible_bar_end_at": bounded_bars[-1].end_at,
        "window_return": _window_return(bounded_bars),
        "bars": tuple(_bar_row(bar) for bar in bounded_bars),
        "status": _component_status("cross_asset_window_proxy", "available", None),
    }


def _proxy_window_unavailable(
    *,
    group: str,
    symbol: str,
    reason: str,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "group": group,
        "row_count": 0,
        "truncated": False,
        "latest_visible_bar_end_at": None,
        "window_return": None,
        "bars": (),
        "status": _component_status(
            "cross_asset_window_proxy",
            "unavailable",
            reason,
        ),
    }


def _window_return(bars: tuple[MarketDataBar, ...]) -> float | None:
    if len(bars) < 2:
        return None
    first_close = bars[0].close_price
    if first_close == 0:
        return None
    return bars[-1].close_price / first_close - 1.0


def _proxy_window_status(window: dict[str, object]) -> str:
    status = window.get("status")
    if not isinstance(status, dict):
        return "unavailable"
    status_value = status.get("status")
    return status_value if isinstance(status_value, str) else "unavailable"


def _unavailable_overview(
    *,
    target_key: str,
    as_of_at: datetime,
    reason: str,
    profile: MarketContextProfile | None = None,
) -> MarketTerminalOverview:
    return MarketTerminalOverview(
        target_key=target_key,
        as_of_at=as_of_at,
        tradable_proxy_symbol=(
            profile.tradable_proxy_symbol if profile is not None else None
        ),
        bar_granularity=profile.bar_granularity if profile is not None else None,
        availability={"market_context": "unavailable"},
        staleness={"latest_completed_bar_end_at": None, "lag_seconds": None},
        latest_completed_bar_end_at=None,
        latest_close=None,
        trailing_return_1d=None,
        trailing_return_5d=None,
        trailing_return_20d=None,
        component_status={"market_context": "unavailable"},
        source_metadata_summary={"unavailable_reason": reason},
        market_context_hash=None,
    )


def _unavailable_price_volume_window(
    *,
    target_key: str,
    as_of_at: datetime,
    lookback_start_at: datetime,
    reason: str,
    mapping: MarketMapping | None = None,
) -> MarketTerminalPriceVolumeWindow:
    return MarketTerminalPriceVolumeWindow(
        target_key=target_key,
        as_of_at=as_of_at,
        symbol=mapping.market_symbol if mapping is not None else None,
        granularity=mapping.bar_granularity if mapping is not None else None,
        lookback_start_at=lookback_start_at,
        latest_visible_bar_end_at=None,
        row_count=0,
        truncated=False,
        bars=(),
        status=_component_status("price_volume_window", "unavailable", reason),
    )


def _unavailable_technical_panel(
    *,
    target_key: str,
    as_of_at: datetime,
    reason: str,
    profile: MarketContextProfile | None = None,
    status: str = "unavailable",
) -> MarketTerminalTechnicalPanel:
    return MarketTerminalTechnicalPanel(
        target_key=target_key,
        as_of_at=as_of_at,
        symbol=profile.tradable_proxy_symbol if profile is not None else None,
        status=_component_status("technical", status, reason),
        ema_136=None,
        price_vs_ema_136=None,
        ema_136_slope=None,
        ichimoku={},
        bollinger_bands=None,
        rsi=None,
        divergence=None,
        technical_trend_label=None,
        field_status={},
        calculation_versions={},
    )


def _unavailable_option_activity(
    *,
    target_key: str,
    as_of_at: datetime,
    reason: str,
    profile: MarketContextProfile | None = None,
    status: str = "unavailable",
) -> MarketTerminalOptionActivity:
    return MarketTerminalOptionActivity(
        target_key=target_key,
        as_of_at=as_of_at,
        underlying_symbol=(
            profile.derivatives_underlying_symbol if profile is not None else None
        ),
        underlying_price=None,
        effective_lookback_hours=(
            profile.derivatives.lookback_hours
            if profile is not None and profile.derivatives is not None
            else None
        ),
        status=_component_status("derivatives", status, reason),
        availability=None,
        volume_summary=None,
        activity_summary=None,
        iv_greeks_summary=None,
        unusual_activity_summary=None,
        selected_contract_count=0,
        selected_contract_symbols=(),
        top_contracts_by_volume=(),
        top_contracts_by_notional=(),
        top_contracts_by_trade_count=(),
        notable_contracts=(),
        field_status={},
    )


def _unavailable_cross_asset_context(
    *,
    target_key: str,
    as_of_at: datetime,
    reason: str,
    status: str = "unavailable",
) -> MarketTerminalCrossAssetContext:
    return MarketTerminalCrossAssetContext(
        target_key=target_key,
        as_of_at=as_of_at,
        status=_component_status("macro_cross_asset", status, reason),
        proxy_summaries=(),
        group_summaries=(),
        usd_pressure=None,
        rates_pressure=None,
        risk_regime=None,
        cross_asset_confirmation=None,
        divergence_notes=(),
        field_status={},
    )


def _unavailable_cross_asset_window(
    *,
    target_key: str,
    as_of_at: datetime,
    lookback_start_at: datetime,
    max_rows_per_symbol: int,
    reason: str,
    status: str = "unavailable",
) -> MarketTerminalCrossAssetWindow:
    return MarketTerminalCrossAssetWindow(
        target_key=target_key,
        as_of_at=as_of_at,
        lookback_start_at=lookback_start_at,
        max_rows_per_symbol=max_rows_per_symbol,
        proxy_windows=(),
        status=_component_status("cross_asset_window", status, reason),
    )


def _component_status(
    component: str,
    status: str,
    reason: str | None,
) -> dict[str, object]:
    return _jsonable_dict(
        MarketContextComponentStatus(
            component=component,
            status=status,
            reason=reason,
        )
    )


def _technical_field_status(technical: TechnicalContext) -> dict[str, object]:
    unavailable = set(technical.technical_context_status.unavailable_fields)
    return {
        "ema_136": _feature_status(technical.ema_136, unavailable=unavailable),
        "price_vs_ema_136": (
            "unavailable"
            if "price_vs_ema_136" in unavailable
            else "available"
        ),
        "ema_136_slope": (
            "unavailable" if "ema_136_slope" in unavailable else "available"
        ),
        "ichimoku": (
            "unavailable"
            if any(field.startswith("ichimoku_") for field in unavailable)
            else "available"
        ),
        "bollinger_bands": _feature_status(
            technical.bollinger_bands,
            unavailable=unavailable,
        ),
        "rsi": _feature_status(technical.rsi, unavailable=unavailable),
        "divergence": _feature_status(technical.divergence, unavailable=unavailable),
    }


def _feature_status(value: object, *, unavailable: set[str]) -> str:
    if value is None:
        return "unavailable"
    status = getattr(value, "status", None)
    if isinstance(status, str):
        return status
    name = value.__class__.__name__
    if name in unavailable:
        return "unavailable"
    return "available"


def _source_metadata_summary(snapshot: MarketContextSnapshot) -> dict[str, object]:
    allowed = {
        "provider",
        "provider_available",
        "remote_fallback",
        "requested_symbol",
        "requested_start_at",
        "requested_end_at",
        "returned_visible_bar_count",
        "first_visible_bar_start_at",
        "latest_visible_bar_end_at",
        "stock_bar_partial",
        "stock_bar_failed_chunk_count",
        "derivatives_provider_available",
        "derivatives_selected_contract_count",
        "derivatives_visible_trade_count",
        "derivatives_visible_bar_count",
        "macro_cross_asset_requested_symbols",
        "macro_cross_asset_visible_bar_counts",
    }
    return {
        key: value
        for key, value in snapshot.source_metadata.items()
        if key in allowed
    }


def _jsonable_or_none(value: object | None) -> dict[str, object] | None:
    if value is None:
        return None
    return _jsonable_dict(value)


def _jsonable_dict(value: object) -> dict[str, object]:
    payload = _jsonable(value)
    if not isinstance(payload, dict):
        raise MarketContextTerminalError("terminal serialization must produce a dict.")
    return payload


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {
            field_name: _jsonable(getattr(value, field_name))
            for field_name in value.__dataclass_fields__
        }
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _jsonable(raw_value)
            for key, raw_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return value


__all__ = [
    "MarketContextTerminal",
    "MarketContextTerminalError",
    "MarketTerminalAvailabilityHeader",
    "MarketTerminalCrossAssetContext",
    "MarketTerminalCrossAssetWindow",
    "MarketTerminalOptionActivity",
    "MarketTerminalOverview",
    "MarketTerminalPriceVolumeWindow",
    "MarketTerminalTechnicalPanel",
]
