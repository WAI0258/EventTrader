"""Shared live/replay market-context snapshot builder."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from event_trader.config import KernelConfig, MarketContextTargetProfileConfig
from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    MarketMapping,
)
from event_trader.market.adjustments import (
    MarketDataAdjustmentPolicy,
    apply_adjustment_policy_to_bars,
    load_adjustment_sidecar_from_source_metadata,
)
from event_trader.market.contracts import (
    CausalPivot,
    CausalSwingSnapshot,
    CurrentLeg,
    DerivativesContext,
    MacroCrossAssetContext,
    MarketContextComponentStatus,
    MarketContextError,
    MarketPathContext,
    MarketContextProfile,
    MarketContextSnapshot,
    OptionBarObservation,
    OptionContractSnapshot,
    OptionSelectionPolicy,
    OptionTradeObservation,
    RangePathSnapshot,
)
from event_trader.market.session_policy import build_market_session_identity
from event_trader.market.features.derivatives import (
    build_derivatives_context,
    build_unavailable_derivatives_context,
    derivatives_calculation_version,
    select_derivatives_contracts,
)
from event_trader.market.features.macro_cross_asset import (
    build_macro_cross_asset_context,
    macro_cross_asset_calculation_version,
)
from event_trader.market.features.price_volume import (
    build_price_volume_context,
    parse_granularity_delta,
    parse_window_delta,
    price_volume_calculation_version,
)
from event_trader.market.features.technical import (
    bollinger_calculation_version,
    build_technical_context,
    ema_calculation_version,
    ichimoku_calculation_version,
    rsi_calculation_version,
    technical_calculation_version,
    technical_divergence_calculation_version,
)
from event_trader.market.subscriptions import (
    resolve_market_context_mapping_for_symbol,
    resolve_market_context_primary_mapping,
)
from event_trader.market.provider import (
    MarketBarsProvider,
    MarketOptionsProvider,
    build_default_market_bars_provider,
)

_SNAPSHOT_VERSION = "market_context_snapshot_2026_05"


def build_market_context_snapshot(
    *,
    config: KernelConfig,
    target_key: str,
    as_of_at: datetime,
    market_data_provider: MarketBarsProvider | None = None,
) -> MarketContextSnapshot | None:
    """Build one replay-safe market-context snapshot, or None when not configured."""
    if not isinstance(config, KernelConfig):
        raise MarketContextError("config must be a KernelConfig instance.")
    if config.market_context is None or not config.market_context.enabled:
        return None
    profile_config = config.market_context.target_profiles.get(target_key)
    if profile_config is None:
        return None
    profile = MarketContextProfile.from_config(
        target_key=target_key,
        profile_config=profile_config,
    )
    normalized_as_of = _validate_utc_datetime(as_of_at, field_name="as_of_at")
    mapping = _mapping_for_profile(
        config=config,
        profile=profile,
        profile_config=profile_config,
    )
    start_at = _request_start_at(
        profile=profile,
        as_of_at=normalized_as_of,
        market_session=mapping.market_session,
    )
    provider = market_data_provider or build_default_market_bars_provider(config)
    source_metadata: dict[str, object] = {
        **_snapshot_session_identity(mapping),
        "requested_symbol": mapping.market_symbol,
        "requested_start_at": start_at.isoformat(),
        "requested_end_at": normalized_as_of.isoformat(),
    }
    if provider is None:
        source_metadata["provider_available"] = False
        return _unavailable_snapshot(
            profile=profile,
            as_of_at=normalized_as_of,
            reason="market data provider unavailable",
            source_metadata=source_metadata,
        )

    source_metadata["provider_available"] = True
    try:
        series = provider.read_series(mapping, start_at=start_at, end_at=normalized_as_of)
        _consume_provider_source_metadata(provider, source_metadata)
        visible_bars = tuple(
            bar for bar in series.bars if bar.end_at <= normalized_as_of
        )
        visible_bars = _adjust_bars_for_market_context_subscription(
            bars=visible_bars,
            market_symbol=mapping.market_symbol,
            profile_config=profile_config,
            source_metadata=source_metadata,
            metadata_prefix="primary_",
            reference_at=(visible_bars[-1].end_at if visible_bars else None),
        )
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        _consume_provider_source_metadata(provider, source_metadata)
        return _unavailable_snapshot(
            profile=profile,
            as_of_at=normalized_as_of,
            reason=f"market data read failed: {exc}",
            source_metadata={**source_metadata, "provider_error": str(exc)},
        )
    source_metadata["returned_visible_bar_count"] = len(visible_bars)
    if visible_bars:
        source_metadata["first_visible_bar_start_at"] = visible_bars[0].start_at.isoformat()
        source_metadata["latest_visible_bar_end_at"] = visible_bars[-1].end_at.isoformat()

    return _snapshot_from_visible_bars(
        profile=profile,
        profile_config=profile_config,
        config=config,
        as_of_at=normalized_as_of,
        visible_bars=visible_bars,
        target_mapping=mapping,
        config_mode=config.mode,
        market_data_provider=provider,
        source_metadata=source_metadata,
    )


def _snapshot_session_identity(mapping: MarketMapping) -> dict[str, object]:
    """Canonical session identity metadata for a target market mapping."""
    return build_market_session_identity(
        market_session=mapping.market_session,
        exchange=mapping.exchange,
        exchange_session_scope=mapping.exchange_session_scope,
        bar_granularity=mapping.bar_granularity,
        market_symbol=mapping.market_symbol,
    )


def _snapshot_from_visible_bars(
    *,
    profile: MarketContextProfile,
    profile_config: MarketContextTargetProfileConfig,
    config: KernelConfig,
    as_of_at: datetime,
    visible_bars: tuple[MarketDataBar, ...],
    target_mapping: MarketMapping,
    config_mode: str,
    market_data_provider: MarketBarsProvider | None,
    source_metadata: dict[str, object],
) -> MarketContextSnapshot:
    price_volume = None
    market_path = None
    technical = None
    derivatives = None
    macro_cross_asset = None
    availability: dict[str, str] = {}
    calculation_versions = {
        "snapshot": _SNAPSHOT_VERSION,
    }
    components = tuple(profile.enabled_components)
    if "price_volume" in components:
        price_volume = build_price_volume_context(
            visible_bars,
            bar_granularity=profile.bar_granularity,
            lookback_bars=profile.price_volume_lookback_bars,
            trailing_return_windows=profile.trailing_return_windows,
        )
        availability["price_volume"] = price_volume.status.status
        calculation_versions["price_volume"] = price_volume_calculation_version()
    if profile.market_path_enabled:
        market_path = _build_market_path_context(
            profile=profile,
            as_of_at=as_of_at,
            visible_bars=visible_bars,
            target_mapping=target_mapping,
        )
        calculation_versions["market_path"] = "market_path_range_causal_swing_2026_06"
    if "technical" in components:
        technical = build_technical_context(
            visible_bars,
            ema_periods=profile.ema_periods,
            ichimoku_enabled=profile.ichimoku_enabled,
            bollinger_period=profile.bollinger_period,
            bollinger_stddev=profile.bollinger_stddev,
            rsi_period=profile.rsi_period,
            rsi_overbought=profile.rsi_overbought,
            rsi_oversold=profile.rsi_oversold,
            divergence_lookback_bars=profile.divergence_lookback_bars,
            divergence_pivot_window=profile.divergence_pivot_window,
        )
        availability["technical"] = technical.technical_context_status.status
        calculation_versions["technical"] = technical_calculation_version()
        calculation_versions["ema"] = ema_calculation_version()
        calculation_versions["standard_ichimoku"] = ichimoku_calculation_version()
        calculation_versions["bollinger_bands"] = bollinger_calculation_version()
        calculation_versions["rsi"] = rsi_calculation_version()
        calculation_versions["technical_divergence"] = (
            technical_divergence_calculation_version()
        )
    if "derivatives" in components:
        derivatives = _build_derivatives_component(
            profile=profile,
            as_of_at=as_of_at,
            visible_bars=visible_bars,
            config_mode=config_mode,
            market_data_provider=market_data_provider,
            source_metadata=source_metadata,
        )
        availability["derivatives"] = derivatives.status.status
        calculation_versions["derivatives"] = derivatives_calculation_version()
    if "macro_cross_asset" in components:
        macro_cross_asset = _build_macro_cross_asset_component(
            config=config,
            profile=profile,
            profile_config=profile_config,
            as_of_at=as_of_at,
            visible_bars=visible_bars,
            market_data_provider=market_data_provider,
            source_metadata=source_metadata,
        )
        availability["macro_cross_asset"] = macro_cross_asset.status.status
        calculation_versions["macro_cross_asset"] = (
            macro_cross_asset_calculation_version()
        )
    latest_end = visible_bars[-1].end_at if visible_bars else None
    return MarketContextSnapshot(
        target_key=profile.target_key,
        as_of_at=as_of_at,
        tradable_proxy_symbol=profile.tradable_proxy_symbol,
        bar_granularity=profile.bar_granularity,
        components=components,
        availability=availability,
        staleness={
            "latest_completed_bar_end_at": latest_end.isoformat() if latest_end else None,
            "lag_seconds": (
                (as_of_at - latest_end).total_seconds() if latest_end is not None else None
            ),
        },
        source_metadata=source_metadata,
        calculation_versions=calculation_versions,
        price_volume=price_volume,
        market_path=market_path,
        technical=technical,
        derivatives=derivatives,
        macro_cross_asset=macro_cross_asset,
    )


def _unavailable_snapshot(
    *,
    profile: MarketContextProfile,
    as_of_at: datetime,
    reason: str,
    source_metadata: dict[str, object],
) -> MarketContextSnapshot:
    status = MarketContextComponentStatus(
        component="market_context",
        status="unavailable",
        reason=reason,
    )
    availability = {component: status.status for component in profile.enabled_components}
    return MarketContextSnapshot(
        target_key=profile.target_key,
        as_of_at=as_of_at,
        tradable_proxy_symbol=profile.tradable_proxy_symbol,
        bar_granularity=profile.bar_granularity,
        components=tuple(profile.enabled_components),
        availability=availability,
        staleness={
            "latest_completed_bar_end_at": None,
            "lag_seconds": None,
        },
        source_metadata=source_metadata,
        calculation_versions={"snapshot": _SNAPSHOT_VERSION},
        price_volume=None,
        market_path=None,
        technical=None,
        derivatives=None,
        macro_cross_asset=None,
    )


def _build_market_path_context(
    *,
    profile: MarketContextProfile,
    as_of_at: datetime,
    visible_bars: tuple[MarketDataBar, ...],
    target_mapping: MarketMapping,
) -> MarketPathContext:
    session = profile.market_path_session or target_mapping.market_session
    range_bars, requested_label, range_coverage = _market_path_range_bars(
        profile=profile,
        visible_bars=visible_bars,
        as_of_at=as_of_at,
        session=session,
    )
    range_path = _build_range_path_snapshot(
        bars=range_bars,
        requested_label=requested_label,
        range_coverage=range_coverage,
        min_required_bars=max(profile.market_path_atr_period + 1, 3),
    )
    swing_path = _build_causal_swing_snapshot(profile=profile, bars=range_bars)
    path_flags = _market_path_flags(
        profile=profile,
        range_path=range_path,
        swing_path=swing_path,
    )
    return MarketPathContext(
        target_key=profile.target_key,
        business_at=as_of_at,
        proxy_symbol=target_mapping.market_symbol,
        bar_granularity=profile.bar_granularity,
        session=session,
        range_path=range_path,
        swing_path=swing_path,
        path_flags=path_flags,
        config={
            "range_lookback": requested_label,
            "atr_period": profile.market_path_atr_period,
            "atr_multiple": profile.market_path_atr_multiple,
            "pct_floor": profile.market_path_pct_floor,
            "min_leg_bars": profile.market_path_min_leg_bars,
            "large_move_pct": profile.market_path_large_move_pct,
            "near_extreme_pct": profile.market_path_near_extreme_pct,
        },
    )


def _market_path_range_bars(
    *,
    profile: MarketContextProfile,
    visible_bars: tuple[MarketDataBar, ...],
    as_of_at: datetime,
    session: str,
) -> tuple[tuple[MarketDataBar, ...], str, str]:
    bar_delta = parse_granularity_delta(profile.bar_granularity)
    if bar_delta >= timedelta(days=1):
        count = profile.market_path_range_lookback_daily_bars
        bars = visible_bars[-count:]
        coverage = "full" if len(bars) >= count else "partial"
        return bars, f"{count} daily bars", coverage
    if session == "continuous":
        days = profile.market_path_range_lookback_calendar_days
        start_at = as_of_at - timedelta(days=days)
        bars = tuple(bar for bar in visible_bars if bar.end_at > start_at)
        coverage = "full" if bars and bars[0].start_at <= start_at else "partial"
        return bars, f"{days} calendar days", coverage
    sessions = profile.market_path_range_lookback_sessions
    start_at = as_of_at - _exchange_session_elapsed_lookback(sessions)
    bars = tuple(bar for bar in visible_bars if bar.end_at > start_at)
    return bars, f"{sessions} exchange sessions", "partial"


def _build_range_path_snapshot(
    *,
    bars: tuple[MarketDataBar, ...],
    requested_label: str,
    range_coverage: str,
    min_required_bars: int,
) -> RangePathSnapshot:
    if not bars:
        return RangePathSnapshot(
            requested_lookback_label=requested_label,
            coverage_status="unavailable",
            available_bar_count=0,
            lookback_start_at=None,
            lookback_end_at=None,
            latest_price=None,
            window_low=None,
            window_low_at=None,
            window_high=None,
            window_high_at=None,
            move_from_window_low_points=None,
            move_from_window_low_pct=None,
            move_from_window_high_points=None,
            move_from_window_high_pct=None,
            missing_reason="no completed bars visible",
        )
    latest = bars[-1]
    low_bar = min(bars, key=lambda bar: bar.low_price)
    high_bar = max(bars, key=lambda bar: bar.high_price)
    coverage_status = (
        "insufficient"
        if len(bars) < min_required_bars
        else "full"
        if range_coverage == "full"
        else "partial"
    )
    latest_price = latest.close_price
    return RangePathSnapshot(
        requested_lookback_label=requested_label,
        coverage_status=coverage_status,
        available_bar_count=len(bars),
        lookback_start_at=bars[0].start_at,
        lookback_end_at=latest.end_at,
        latest_price=latest_price,
        window_low=low_bar.low_price,
        window_low_at=low_bar.end_at,
        window_high=high_bar.high_price,
        window_high_at=high_bar.end_at,
        move_from_window_low_points=latest_price - low_bar.low_price,
        move_from_window_low_pct=(latest_price / low_bar.low_price) - 1.0,
        move_from_window_high_points=latest_price - high_bar.high_price,
        move_from_window_high_pct=(latest_price / high_bar.high_price) - 1.0,
        missing_reason=(
            None
            if coverage_status == "full"
            else "insufficient bars"
            if coverage_status == "insufficient"
            else "partial requested-window coverage"
        ),
    )


def _build_causal_swing_snapshot(
    *,
    profile: MarketContextProfile,
    bars: tuple[MarketDataBar, ...],
) -> CausalSwingSnapshot:
    min_bars = max(profile.market_path_atr_period + profile.market_path_min_leg_bars, 5)
    params = {
        "atr_period": profile.market_path_atr_period,
        "atr_multiple": profile.market_path_atr_multiple,
        "pct_floor": profile.market_path_pct_floor,
        "min_leg_bars": profile.market_path_min_leg_bars,
    }
    if len(bars) < min_bars:
        return CausalSwingSnapshot(
            status="insufficient",
            algorithm="causal_atr_zigzag_v1",
            params=params,
            confirmed_pivots=(),
            current_leg=None,
            structure_flags=(),
            missing_reason="insufficient bars for causal swing confirmation",
        )
    pivots = _causal_atr_zigzag_pivots(profile=profile, bars=bars)
    current_leg = _current_leg_from_pivots(pivots=pivots, latest_bar=bars[-1])
    structure_flags: list[str] = []
    if current_leg is not None:
        if current_leg.leg_direction == "up":
            structure_flags.append("confirmed_rebound_leg")
        elif current_leg.leg_direction == "down":
            structure_flags.append("confirmed_selloff_leg")
        if current_leg.maturity == "late":
            structure_flags.append("late_current_leg")
    return CausalSwingSnapshot(
        status="available" if pivots else "partial",
        algorithm="causal_atr_zigzag_v1",
        params=params,
        confirmed_pivots=pivots,
        current_leg=current_leg,
        structure_flags=tuple(structure_flags),
        missing_reason=None if pivots else "no confirmed pivots in visible window",
    )


def _causal_atr_zigzag_pivots(
    *,
    profile: MarketContextProfile,
    bars: tuple[MarketDataBar, ...],
) -> tuple[CausalPivot, ...]:
    pivots: list[CausalPivot] = []
    candidate_kind = "swing_low"
    candidate_bar = bars[0]
    candidate_index = 0
    for index, bar in enumerate(bars[1:], start=1):
        atr = _average_true_range(
            bars[: index + 1],
            period=profile.market_path_atr_period,
        )
        if candidate_kind == "swing_low":
            if bar.low_price < candidate_bar.low_price:
                candidate_bar = bar
                candidate_index = index
                continue
            if index - candidate_index < profile.market_path_min_leg_bars:
                continue
            threshold = _swing_reversal_threshold(
                candidate_price=candidate_bar.low_price,
                atr=atr,
                atr_multiple=profile.market_path_atr_multiple,
                pct_floor=profile.market_path_pct_floor,
            )
            reversal = bar.high_price - candidate_bar.low_price
            if reversal >= threshold:
                pivots.append(
                    CausalPivot(
                        kind="swing_low",
                        pivot_price=candidate_bar.low_price,
                        pivot_at=candidate_bar.end_at,
                        confirmed_at=bar.end_at,
                        confirmation_lag_bars=index - candidate_index,
                        reversal_points=reversal,
                        reversal_pct=reversal / candidate_bar.low_price,
                        no_future_bars_used=True,
                    )
                )
                candidate_kind = "swing_high"
                candidate_bar = bar
                candidate_index = index
        else:
            if bar.high_price > candidate_bar.high_price:
                candidate_bar = bar
                candidate_index = index
                continue
            if index - candidate_index < profile.market_path_min_leg_bars:
                continue
            threshold = _swing_reversal_threshold(
                candidate_price=candidate_bar.high_price,
                atr=atr,
                atr_multiple=profile.market_path_atr_multiple,
                pct_floor=profile.market_path_pct_floor,
            )
            reversal = candidate_bar.high_price - bar.low_price
            if reversal >= threshold:
                pivots.append(
                    CausalPivot(
                        kind="swing_high",
                        pivot_price=candidate_bar.high_price,
                        pivot_at=candidate_bar.end_at,
                        confirmed_at=bar.end_at,
                        confirmation_lag_bars=index - candidate_index,
                        reversal_points=reversal,
                        reversal_pct=reversal / candidate_bar.high_price,
                        no_future_bars_used=True,
                    )
                )
                candidate_kind = "swing_low"
                candidate_bar = bar
                candidate_index = index
    return tuple(pivots[-6:])


def _average_true_range(
    bars: tuple[MarketDataBar, ...],
    *,
    period: int,
) -> float | None:
    if len(bars) < 2:
        return None
    recent = bars[-(period + 1) :]
    ranges: list[float] = []
    for previous, current in zip(recent, recent[1:], strict=False):
        ranges.append(
            max(
                current.high_price - current.low_price,
                abs(current.high_price - previous.close_price),
                abs(current.low_price - previous.close_price),
            )
        )
    if not ranges:
        return None
    return sum(ranges) / len(ranges)


def _swing_reversal_threshold(
    *,
    candidate_price: float,
    atr: float | None,
    atr_multiple: float,
    pct_floor: float,
) -> float:
    pct_threshold = candidate_price * pct_floor
    if atr is None:
        return pct_threshold
    return max(atr_multiple * atr, pct_threshold)


def _current_leg_from_pivots(
    *,
    pivots: tuple[CausalPivot, ...],
    latest_bar: MarketDataBar,
) -> CurrentLeg | None:
    if not pivots:
        return None
    pivot = pivots[-1]
    latest_price = latest_bar.close_price
    move_points = latest_price - pivot.pivot_price
    move_pct = latest_price / pivot.pivot_price - 1.0
    if abs(move_pct) < 0.01:
        direction = "sideways"
    elif move_points > 0:
        direction = "up"
    else:
        direction = "down"
    abs_move_pct = abs(move_pct)
    if abs_move_pct < 0.04:
        maturity = "early"
    elif abs_move_pct < 0.10:
        maturity = "developing"
    else:
        maturity = "late"
    return CurrentLeg(
        from_pivot_kind=pivot.kind,
        from_pivot_price=pivot.pivot_price,
        from_pivot_at=pivot.pivot_at,
        latest_price=latest_price,
        move_points=move_points,
        move_pct=move_pct,
        leg_direction=direction,
        maturity=maturity,
    )


def _market_path_flags(
    *,
    profile: MarketContextProfile,
    range_path: RangePathSnapshot,
    swing_path: CausalSwingSnapshot,
) -> tuple[str, ...]:
    flags: list[str] = []
    low_move = range_path.move_from_window_low_pct
    high_move = range_path.move_from_window_high_pct
    near = profile.market_path_near_extreme_pct
    large = profile.market_path_large_move_pct
    if low_move is not None and low_move >= large:
        flags.append("large_rebound_from_window_low")
    if high_move is not None and high_move <= -large:
        flags.append("large_selloff_from_window_high")
    if low_move is not None and high_move is not None:
        if low_move >= large and abs(high_move) <= near:
            flags.append("late_confirmation_level_risk")
    if low_move is not None and low_move <= near:
        flags.append("near_window_low")
    if high_move is not None and abs(high_move) <= near:
        flags.append("near_window_high")
    flags.extend(swing_path.structure_flags)
    return tuple(dict.fromkeys(flags))


def _build_derivatives_component(
    *,
    profile: MarketContextProfile,
    as_of_at: datetime,
    visible_bars: tuple[MarketDataBar, ...],
    config_mode: str,
    market_data_provider: MarketBarsProvider | None,
    source_metadata: dict[str, object],
) -> DerivativesContext:
    policy = profile.derivatives
    if policy is None:
        raise MarketContextError("derivatives policy is required when derivatives is enabled.")
    underlying_price = visible_bars[-1].close_price if visible_bars else None
    options_provider = _as_options_provider(market_data_provider)
    if underlying_price is None:
        source_metadata["derivatives_provider_available"] = options_provider is not None
        return build_derivatives_context(
            underlying_symbol=profile.derivatives_underlying_symbol,
            underlying_price=underlying_price,
            as_of_at=as_of_at,
            policy=policy,
            chain=(),
            trades=(),
            bars=(),
        )
    if options_provider is None:
        source_metadata["derivatives_provider_available"] = False
        return build_unavailable_derivatives_context(
            underlying_symbol=profile.derivatives_underlying_symbol,
            underlying_price=underlying_price,
            policy=policy,
            reason="provider_unavailable",
        )
    lookback_start = as_of_at - timedelta(hours=policy.lookback_hours)
    replay_mode = config_mode == "replay"
    replay_field_status = (
        {
            "contract_universe_source": "latest_chain_metadata",
            "contract_universe_visibility": "metadata_only_not_historical_snapshot",
            "iv": "unavailable:latest_chain_metadata_only_not_historical_snapshot",
            "greeks": "unavailable:latest_chain_metadata_only_not_historical_snapshot",
            "quotes": "unavailable:latest_chain_metadata_only_not_historical_snapshot",
        }
        if replay_mode
        else {}
    )
    try:
        if replay_mode:
            source_metadata["contract_universe_source"] = "latest_chain_metadata"
            source_metadata["contract_universe_visibility"] = (
                "metadata_only_not_historical_snapshot"
            )
            chain = _read_replay_option_contract_universe(
                options_provider=options_provider,
                underlying_symbol=profile.derivatives_underlying_symbol,
                as_of_at=as_of_at,
                underlying_price=underlying_price,
                policy=policy,
            )
        else:
            source_metadata["contract_universe_source"] = "latest_chain_snapshot"
            source_metadata["contract_universe_visibility"] = "latest_snapshot_fields_visible"
            chain = options_provider.read_option_chain(
                underlying_symbol=profile.derivatives_underlying_symbol,
                as_of_at=as_of_at,
                underlying_price=underlying_price,
                policy=policy,
            )
        _consume_provider_source_metadata(market_data_provider, source_metadata)
        selected = select_derivatives_contracts(
            chain,
            underlying_price=underlying_price,
            as_of_at=as_of_at,
            policy=policy,
        )
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        source_metadata["derivatives_provider_error"] = str(exc)
        return build_unavailable_derivatives_context(
            underlying_symbol=profile.derivatives_underlying_symbol,
            underlying_price=underlying_price,
            policy=policy,
            reason="provider_read_failed",
        )
    trades = _read_optional_option_trades(
        options_provider=options_provider,
        market_data_provider=market_data_provider,
        selected=selected,
        start_at=lookback_start,
        end_at=as_of_at,
        policy=policy,
        source_metadata=source_metadata,
    )
    bars = _read_optional_option_bars(
        options_provider=options_provider,
        market_data_provider=market_data_provider,
        selected=selected,
        start_at=lookback_start,
        end_at=as_of_at,
        policy=policy,
        source_metadata=source_metadata,
    )
    chain = _enrich_selected_contracts_with_latest(
        options_provider=options_provider,
        market_data_provider=market_data_provider,
        chain=chain,
        selected=selected,
        as_of_at=as_of_at,
        policy=policy,
        source_metadata=source_metadata,
    )
    source_metadata["derivatives_chain_visible_row_count"] = len(chain)
    source_metadata["derivatives_selected_contract_count"] = len(selected)
    source_metadata["derivatives_visible_trade_count"] = len(trades)
    source_metadata["derivatives_visible_bar_count"] = len(bars)
    source_metadata["historical_trades_status"] = _historical_observation_status(
        enabled=policy.include_historical_trades,
        selected=bool(selected),
        observation_count=len(trades),
    )
    source_metadata["historical_bars_status"] = _historical_observation_status(
        enabled=policy.include_historical_bars,
        selected=bool(selected),
        observation_count=len(bars),
    )
    context = build_derivatives_context(
        underlying_symbol=profile.derivatives_underlying_symbol,
        underlying_price=underlying_price,
        as_of_at=as_of_at,
        policy=policy,
        chain=chain,
        trades=trades,
        bars=bars,
    )
    context = _apply_latest_availability_status(
        context,
        latest_trades_status=str(source_metadata.get("latest_trades_status", "")),
        latest_quotes_status=str(source_metadata.get("latest_quotes_status", "")),
    )
    if replay_field_status and context.status.status != "unavailable":
        merged = {**context.field_status, **replay_field_status}
        context = _replace_derivatives_field_status(context, merged)
    return context


def _read_replay_option_contract_universe(
    *,
    options_provider: MarketOptionsProvider,
    underlying_symbol: str,
    as_of_at: datetime,
    underlying_price: float,
    policy: OptionSelectionPolicy,
) -> tuple[OptionContractSnapshot, ...]:
    metadata_reader = getattr(options_provider, "read_option_chain_metadata", None)
    if callable(metadata_reader):
        return cast(
            tuple[OptionContractSnapshot, ...],
            metadata_reader(
                underlying_symbol=underlying_symbol,
                as_of_at=as_of_at,
                underlying_price=underlying_price,
                policy=policy,
            ),
        )
    return tuple(
        _metadata_only_contract(row)
        for row in options_provider.read_option_chain(
            underlying_symbol=underlying_symbol,
            as_of_at=as_of_at,
            underlying_price=underlying_price,
            policy=policy,
        )
    )


def _read_optional_option_trades(
    *,
    options_provider: MarketOptionsProvider,
    market_data_provider: MarketBarsProvider | None,
    selected: tuple[OptionContractSnapshot, ...],
    start_at: datetime,
    end_at: datetime,
    policy: OptionSelectionPolicy,
    source_metadata: dict[str, object],
) -> tuple[OptionTradeObservation, ...]:
    if not policy.include_historical_trades or not selected:
        return ()
    try:
        rows = options_provider.read_option_trades(
            contracts=selected,
            start_at=start_at,
            end_at=end_at,
            policy=policy,
        )
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        source_metadata["historical_trades_error"] = str(exc)
        return ()
    _consume_provider_source_metadata(market_data_provider, source_metadata)
    return rows


def _read_optional_option_bars(
    *,
    options_provider: MarketOptionsProvider,
    market_data_provider: MarketBarsProvider | None,
    selected: tuple[OptionContractSnapshot, ...],
    start_at: datetime,
    end_at: datetime,
    policy: OptionSelectionPolicy,
    source_metadata: dict[str, object],
) -> tuple[OptionBarObservation, ...]:
    if not policy.include_historical_bars or not selected:
        return ()
    try:
        rows = options_provider.read_option_bars(
            contracts=selected,
            start_at=start_at,
            end_at=end_at,
            policy=policy,
        )
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        source_metadata["historical_bars_error"] = str(exc)
        return ()
    _consume_provider_source_metadata(market_data_provider, source_metadata)
    return rows


def _enrich_selected_contracts_with_latest(
    *,
    options_provider: MarketOptionsProvider,
    market_data_provider: MarketBarsProvider | None,
    chain: tuple[OptionContractSnapshot, ...],
    selected: tuple[OptionContractSnapshot, ...],
    as_of_at: datetime,
    policy: OptionSelectionPolicy,
    source_metadata: dict[str, object],
) -> tuple[OptionContractSnapshot, ...]:
    if not selected:
        source_metadata["latest_trades_status"] = "unavailable:no_selected_contracts"
        source_metadata["latest_quotes_status"] = "unavailable:no_selected_contracts"
        return chain
    enriched_by_symbol = {contract.contract_symbol: contract for contract in chain}
    latest_trades_status = "disabled:not_configured"
    if getattr(policy, "include_latest_trades", False):
        latest_reader = getattr(options_provider, "read_latest_option_trades", None)
        if callable(latest_reader):
            try:
                latest_trades = tuple(
                    trade
                    for trade in latest_reader(
                        contracts=selected,
                        as_of_at=as_of_at,
                        policy=policy,
                    )
                    if trade.observed_at <= as_of_at
                )
                _consume_provider_source_metadata(market_data_provider, source_metadata)
                latest_trades_status = (
                    "available" if latest_trades else "unavailable:no_safe_latest_trades"
                )
                for trade in latest_trades:
                    current = enriched_by_symbol.get(trade.contract_symbol)
                    if current is not None:
                        enriched_by_symbol[trade.contract_symbol] = replace(
                            current,
                            latest_trade_price=trade.price,
                            latest_trade_size=trade.size,
                            latest_trade_at=trade.observed_at,
                        )
            except Exception as exc:  # pragma: no cover - defensive provider boundary
                latest_trades_status = f"unavailable:{type(exc).__name__}"
                source_metadata["latest_trades_error"] = str(exc)
        else:
            latest_trades_status = "unavailable:provider_method_missing"
    latest_quotes_status = "disabled:not_configured"
    if getattr(policy, "include_latest_quotes", False):
        latest_reader = getattr(options_provider, "read_latest_option_quotes", None)
        if callable(latest_reader):
            try:
                latest_quotes = tuple(
                    quote
                    for quote in latest_reader(
                        contracts=selected,
                        as_of_at=as_of_at,
                        policy=policy,
                    )
                    if quote.latest_quote_at is not None and quote.latest_quote_at <= as_of_at
                )
                _consume_provider_source_metadata(market_data_provider, source_metadata)
                latest_quotes_status = (
                    "available" if latest_quotes else "unavailable:no_safe_latest_quotes"
                )
                for quote in latest_quotes:
                    current = enriched_by_symbol.get(quote.contract_symbol)
                    if current is not None:
                        enriched_by_symbol[quote.contract_symbol] = replace(
                            current,
                            latest_quote_at=quote.latest_quote_at,
                            bid_price=quote.bid_price,
                            ask_price=quote.ask_price,
                            bid_size=quote.bid_size,
                            ask_size=quote.ask_size,
                        )
            except Exception as exc:  # pragma: no cover - defensive provider boundary
                latest_quotes_status = f"unavailable:{type(exc).__name__}"
                source_metadata["latest_quotes_error"] = str(exc)
        else:
            latest_quotes_status = "unavailable:provider_method_missing"
    source_metadata["latest_trades_status"] = latest_trades_status
    source_metadata["latest_quotes_status"] = latest_quotes_status
    return tuple(enriched_by_symbol[contract.contract_symbol] for contract in chain)


def _apply_latest_availability_status(
    context: DerivativesContext,
    *,
    latest_trades_status: str,
    latest_quotes_status: str,
) -> DerivativesContext:
    if context.status.status == "unavailable":
        return context
    updates = {
        "latest_trades": latest_trades_status,
        "latest_quotes": latest_quotes_status,
    }
    field_status = {
        **context.field_status,
        **{key: value for key, value in updates.items() if value},
    }
    return _replace_derivatives_field_status(
        replace(
            context,
            availability=replace(
                context.availability,
                latest_trades_status=(
                    latest_trades_status or context.availability.latest_trades_status
                ),
                latest_quotes_status=(
                    latest_quotes_status or context.availability.latest_quotes_status
                ),
            ),
        ),
        field_status,
    )


def _replace_derivatives_field_status(
    context: DerivativesContext,
    field_status: dict[str, str],
) -> DerivativesContext:
    unavailable_fields = tuple(
        sorted(field for field, status in field_status.items() if status != "available")
    )
    return replace(
        context,
        field_status=field_status,
        status=MarketContextComponentStatus(
            component="derivatives",
            status="available" if not unavailable_fields else "partial",
            reason=context.status.reason,
            unavailable_fields=unavailable_fields,
        ),
    )


def _metadata_only_contract(row: OptionContractSnapshot) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        contract_symbol=row.contract_symbol,
        underlying_symbol=row.underlying_symbol,
        option_type=row.option_type,
        expiry_date=row.expiry_date,
        strike_price=row.strike_price,
        observed_at=None,
    )


def _historical_observation_status(
    *,
    enabled: bool,
    selected: bool,
    observation_count: int,
) -> str:
    if not enabled:
        return "disabled:not_configured"
    if not selected:
        return "unavailable:no_selected_contracts"
    return "available" if observation_count > 0 else "unavailable:no_visible_observations"


def _as_options_provider(provider: MarketBarsProvider | None) -> MarketOptionsProvider | None:
    if provider is None:
        return None
    required = ("read_option_chain", "read_option_trades", "read_option_bars")
    if all(callable(getattr(provider, name, None)) for name in required):
        return provider  # type: ignore[return-value]
    return None


def _build_macro_cross_asset_component(
    *,
    config: KernelConfig,
    profile: MarketContextProfile,
    profile_config: MarketContextTargetProfileConfig,
    as_of_at: datetime,
    visible_bars: tuple[MarketDataBar, ...],
    market_data_provider: MarketBarsProvider | None,
    source_metadata: dict[str, object],
) -> MacroCrossAssetContext:
    policy = profile.macro_cross_asset
    if policy is None:
        raise MarketContextError(
            "macro_cross_asset policy is required when macro_cross_asset is enabled."
        )
    if market_data_provider is None:
        source_metadata["macro_cross_asset_provider_available"] = False
        return build_macro_cross_asset_context(
            policy=policy,
            target_bars=visible_bars,
            proxy_bars_by_symbol={},
            as_of_at=as_of_at,
        )
    proxy_start_at = _macro_request_start_at(
        profile=profile,
        as_of_at=as_of_at,
        market_session=resolve_market_context_primary_mapping(
            config=config,
            profile=profile,
            profile_config=profile_config,
        ).market_session,
    )
    proxy_bars_by_symbol: dict[str, tuple[MarketDataBar, ...]] = {}
    proxy_symbols = tuple(
        dict.fromkeys(
            symbol
            for symbols in policy.proxy_groups.values()
            for symbol in symbols
        )
    )
    errors: dict[str, str] = {}
    if callable(getattr(market_data_provider, "read_series_with_metadata", None)):
        max_workers = min(8, len(proxy_symbols)) or 1
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = executor.map(
                lambda symbol: _read_macro_proxy_series(
                    market_data_provider=market_data_provider,
                    config=config,
                    profile=profile,
                    profile_config=profile_config,
                    symbol=symbol,
                    proxy_start_at=proxy_start_at,
                    as_of_at=as_of_at,
                ),
                proxy_symbols,
            )
            for symbol, bars, metadata, error in results:
                proxy_bars_by_symbol[symbol] = bars
                if error is not None:
                    errors[symbol] = error
                source_metadata.update(
                    {
                        f"macro_cross_asset_{symbol}_{key}": value
                        for key, value in metadata.items()
                    }
                )
    else:
        for symbol in proxy_symbols:
            mapping = resolve_market_context_mapping_for_symbol(
                config=config,
                profile=profile,
                profile_config=profile_config,
                symbol=symbol,
            )
            try:
                series = market_data_provider.read_series(
                    mapping,
                    start_at=proxy_start_at,
                    end_at=as_of_at,
                )
                _consume_provider_source_metadata(
                    market_data_provider,
                    source_metadata,
                    prefix=f"macro_cross_asset_{symbol}_",
                )
            except Exception as exc:  # pragma: no cover - defensive provider boundary
                errors[symbol] = str(exc)
                proxy_bars_by_symbol[symbol] = ()
                continue
            bars = tuple(
                bar for bar in series.bars if bar.end_at <= as_of_at
            )
            proxy_bars_by_symbol[symbol] = _adjust_bars_for_market_context_subscription(
                bars=bars,
                market_symbol=symbol,
                profile_config=profile_config,
                source_metadata=source_metadata,
                metadata_prefix=f"macro_cross_asset_{symbol}_adjustment_",
                reference_at=(bars[-1].end_at if bars else None),
            )
    source_metadata["macro_cross_asset_requested_symbols"] = list(proxy_symbols)
    source_metadata["macro_cross_asset_visible_bar_counts"] = {
        symbol: len(bars) for symbol, bars in sorted(proxy_bars_by_symbol.items())
    }
    if errors:
        source_metadata["macro_cross_asset_provider_errors"] = dict(sorted(errors.items()))
    return build_macro_cross_asset_context(
        policy=policy,
        target_bars=visible_bars,
        proxy_bars_by_symbol=proxy_bars_by_symbol,
        as_of_at=as_of_at,
    )


def _read_macro_proxy_series(
    *,
    market_data_provider: MarketBarsProvider,
    config: KernelConfig,
    profile: MarketContextProfile,
    profile_config: MarketContextTargetProfileConfig,
    symbol: str,
    proxy_start_at: datetime,
    as_of_at: datetime,
) -> tuple[str, tuple[MarketDataBar, ...], dict[str, object], str | None]:
    mapping = resolve_market_context_mapping_for_symbol(
        config=config,
        profile=profile,
        profile_config=profile_config,
        symbol=symbol,
    )
    try:
        read_with_metadata = cast(Any, market_data_provider).read_series_with_metadata
        raw_series, metadata = cast(
            tuple[MarketDataSeries, dict[str, object]],
            read_with_metadata(
                mapping,
                start_at=proxy_start_at,
                end_at=as_of_at,
            ),
        )
        bars = tuple(bar for bar in raw_series.bars if bar.end_at <= as_of_at)
        bars = _adjust_bars_for_market_context_subscription(
            bars=bars,
            market_symbol=symbol,
            profile_config=profile_config,
            source_metadata=metadata,
            metadata_prefix="adjustment_",
            reference_at=(bars[-1].end_at if bars else None),
        )
        return symbol, bars, metadata, None
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        return symbol, (), {}, str(exc)


def _consume_provider_source_metadata(
    provider: MarketBarsProvider | None,
    source_metadata: dict[str, object],
    *,
    prefix: str = "",
) -> None:
    if provider is None:
        return
    consume = getattr(provider, "pop_source_metadata", None)
    if callable(consume):
        source_metadata.update(
            {
                f"{prefix}{key}": value
                for key, value in cast(dict[str, object], consume()).items()
            }
        )


def _adjust_bars_for_market_context_subscription(
    *,
    bars: tuple[MarketDataBar, ...],
    market_symbol: str,
    profile_config: MarketContextTargetProfileConfig,
    source_metadata: dict[str, object],
    metadata_prefix: str,
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
    adjusted_bars, metadata = apply_adjustment_policy_to_bars(
        bars=bars,
        sidecar=sidecar,
        reference_at=reference_at,
    )
    source_metadata.update(
        {f"{metadata_prefix}{key}": value for key, value in metadata.items()}
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


def _macro_request_start_at(
    *,
    profile: MarketContextProfile,
    as_of_at: datetime,
    market_session: str,
) -> datetime:
    policy = profile.macro_cross_asset
    if policy is None:
        return as_of_at
    bar_delta = parse_granularity_delta(profile.bar_granularity)
    required_elapsed = bar_delta * (policy.lookback_bars + 2)
    for window in policy.trailing_return_windows:
        required_elapsed = max(required_elapsed, parse_window_delta(window) + bar_delta)
    return as_of_at - required_elapsed


def _request_start_at(
    *,
    profile: MarketContextProfile,
    as_of_at: datetime,
    market_session: str,
) -> datetime:
    bar_delta = parse_granularity_delta(profile.bar_granularity)
    required_bars = profile.price_volume_lookback_bars
    required_elapsed = bar_delta * required_bars
    for window in profile.trailing_return_windows:
        window_delta = parse_window_delta(window)
        required_elapsed = max(required_elapsed, window_delta + bar_delta)
    if profile.ema_periods:
        required_bars = max(required_bars, max(profile.ema_periods) + 1)
    if profile.ichimoku_enabled:
        required_bars = max(required_bars, 52)
    if profile.macro_cross_asset is not None:
        required_bars = max(required_bars, profile.macro_cross_asset.lookback_bars)
        for window in profile.macro_cross_asset.trailing_return_windows:
            required_elapsed = max(required_elapsed, parse_window_delta(window) + bar_delta)
    if profile.market_path_enabled:
        if bar_delta >= timedelta(days=1):
            required_bars = max(
                required_bars,
                profile.market_path_range_lookback_daily_bars,
            )
        elif profile.market_path_session == "continuous" or market_session == "continuous":
            required_elapsed = max(
                required_elapsed,
                timedelta(days=profile.market_path_range_lookback_calendar_days),
            )
        else:
            required_elapsed = max(
                required_elapsed,
                _exchange_session_elapsed_lookback(
                    profile.market_path_range_lookback_sessions
                ),
            )
    bar_count_elapsed = bar_delta * (required_bars + 2)
    return as_of_at - max(required_elapsed, bar_count_elapsed)


def _exchange_session_elapsed_lookback(sessions: int) -> timedelta:
    """Conservative stopgap until exchange calendars own exact session spans."""
    weekend_padding_days = (sessions // 5) * 2
    holiday_padding_days = 2
    return timedelta(days=sessions + weekend_padding_days + holiday_padding_days)


def _mapping_for_profile(
    *,
    config: KernelConfig,
    profile: MarketContextProfile,
    profile_config: MarketContextTargetProfileConfig,
) -> MarketMapping:
    return resolve_market_context_primary_mapping(
        config=config,
        profile=profile,
        profile_config=profile_config,
    )


def _validate_utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise MarketContextError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketContextError(f"{field_name} must be timezone-aware UTC.")
    if value.utcoffset() != UTC.utcoffset(None):
        raise MarketContextError(f"{field_name} must be timezone-aware UTC.")
    return value.astimezone(UTC)


__all__ = [
    "build_market_context_snapshot",
]
