"""Deterministic live market-data warmup and canonical bar backfill."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from event_trader.config import (
    KernelConfig,
    LiveMarketDataConfig,
    MarketDataSubscriptionConfig,
)
from event_trader.contracts._validators import validate_timestamp
from event_trader.contracts.view_state_change import MarketMapping
from event_trader.market.adjustments import (
    AdjustmentDirection,
    adjustment_direction_for_policy,
    normalize_archive_symbol,
)
from event_trader.market.context_builder import (
    _macro_request_start_at,
    _request_start_at,
)
from event_trader.market.contracts import MarketContextProfile
from event_trader.market.provider import (
    MarketBarsProvider,
    build_default_market_bars_provider,
)
from event_trader.market.shared_store import (
    SharedMarketDataError,
    SharedMarketDataProvider,
    SharedMarketDataStore,
    shared_market_data_root,
)
from event_trader.storage import WorkspaceLayout

type LiveMarketDataEmitter = Callable[[str], None]


class LiveMarketDataRuntimeError(RuntimeError):
    """Raised when live market-data capture cannot continue safely."""


@dataclass(frozen=True, slots=True)
class LiveMarketDataWarmupReceipt:
    """Observable result from one live market-data warmup pass."""

    enabled: bool
    target_count: int = 0
    symbol_count: int = 0
    bar_count: int = 0


def prepare_live_market_data(
    *,
    config: KernelConfig,
    layout: WorkspaceLayout,
    as_of_at: datetime | None = None,
    provider: MarketBarsProvider | None = None,
    emit: LiveMarketDataEmitter | None = None,
) -> tuple[SharedMarketDataProvider | None, LiveMarketDataWarmupReceipt]:
    """Warm live canonical market bars and return a store-backed provider."""
    live_market_data = _live_market_data_config(config)
    if live_market_data is None:
        return None, LiveMarketDataWarmupReceipt(enabled=False)
    store = SharedMarketDataStore(shared_market_data_root(config))
    fallback = provider or build_default_market_bars_provider(config)
    local_archive_root = _live_local_archive_root(config)
    provider_name = (
        config.validation.market_data.provider
        if config.validation is not None
        else "unknown"
    )
    adjustment_policies = {
        (target.target_key, subscription.symbol): subscription.adjustment_policy
        for target in live_market_data.targets
        for subscription in target.subscriptions
        if subscription.adjustment_policy is not None
    }
    provider_names = {
        (target.target_key, subscription.symbol): subscription.provider
        for target in live_market_data.targets
        for subscription in target.subscriptions
    }
    store_provider = SharedMarketDataProvider(
        store=store,
        provider_name=provider_name,
        remote_provider=fallback,
        adjustment_policies=adjustment_policies,
        provider_names=provider_names,
    )
    if not live_market_data.warmup.enabled:
        return store_provider, LiveMarketDataWarmupReceipt(enabled=True)
    if fallback is None:
        raise LiveMarketDataRuntimeError(
            "live market-data warmup requires validation.market_data provider."
        )
    warmup_now = _validate_utc_datetime(
        as_of_at or datetime.now(UTC), field_name="as_of_at"
    )
    bar_count = 0
    symbol_count = 0
    for target in live_market_data.targets:
        _assert_live_subscription_providers_configured(
            config=config,
            subscriptions=target.subscriptions,
            provider_override=provider,
            target_key=target.target_key,
        )
        for subscription in target.subscriptions:
            mapping = _mapping_for_live_subscription(
                target_key=target.target_key,
                subscription=subscription,
            )
            start_at = _warmup_start_at(
                config=config,
                target_key=target.target_key,
                symbol=subscription.symbol,
                end_at=warmup_now,
                min_lookback_days=live_market_data.warmup.min_lookback_days,
                mapping=mapping,
                purpose=subscription.purpose,
            )
            try:
                synced_bars = store_provider.synchronize(
                    mapping,
                    start_at=start_at,
                    end_at=warmup_now,
                )
            except (OSError, SharedMarketDataError, ValueError) as exc:
                if subscription.purpose == "primary_tradable":
                    raise
                if emit is not None:
                    emit(
                        "live market-data warmup skipped optional symbol: "
                        f"target={target.target_key} symbol={subscription.symbol} "
                        f"reason={exc}"
                    )
                continue
            provider_metadata = store_provider.pop_source_metadata()
            timezone_name = _provider_timezone_name(provider_metadata)
            if subscription.adjustment_policy is not None:
                if timezone_name is None:
                    raise LiveMarketDataRuntimeError(
                        "live market-data warmup provider metadata is missing timezone "
                        f"for adjusted symbol {subscription.symbol!r}."
                    )
                _freeze_live_adjustment_sidecar(
                    store=store,
                    subscription=subscription,
                    local_archive_root=local_archive_root,
                    provider_metadata=provider_metadata,
                )
            symbol_count += 1
            bar_count += synced_bars
    if emit is not None:
        emit(
            "live market-data warmup: "
            f"targets={len(live_market_data.targets)} "
            f"symbols={symbol_count} bars={bar_count}"
        )
    return store_provider, LiveMarketDataWarmupReceipt(
        enabled=True,
        target_count=len(live_market_data.targets),
        symbol_count=symbol_count,
        bar_count=bar_count,
    )



def _live_market_data_config(config: KernelConfig) -> LiveMarketDataConfig | None:
    if config.live is None or config.live.market_data is None:
        return None
    if not config.live.market_data.enabled:
        return None
    return config.live.market_data


def _live_local_archive_root(config: KernelConfig) -> Path | None:
    if config.validation is None:
        return None
    root = config.validation.market_data.local_archive_root
    if root is None:
        return None
    return root.resolve(strict=False)


def _warmup_start_at(
    *,
    config: KernelConfig,
    target_key: str,
    symbol: str,
    end_at: datetime,
    min_lookback_days: int,
    mapping: MarketMapping,
    purpose: str,
) -> datetime:
    min_start = end_at - timedelta(days=min_lookback_days)
    profile_config = None if config.market_context is None else (
        config.market_context.target_profiles.get(target_key)
    )
    if profile_config is None:
        return min_start
    profile = MarketContextProfile.from_config(
        target_key=target_key,
        profile_config=profile_config,
    )
    if symbol == profile.tradable_proxy_symbol.upper():
        profile_start = _request_start_at(
            profile=profile,
            as_of_at=end_at,
            market_session=mapping.market_session,
        )
    else:
        if profile.macro_cross_asset is None:
            return min_start
        profile_start = _macro_request_start_at(
            profile=profile,
            as_of_at=end_at,
            market_session=mapping.market_session,
        )
        return profile_start
    return min(min_start, profile_start)


def _mapping_for_live_subscription(
    *,
    target_key: str,
    subscription: MarketDataSubscriptionConfig,
) -> MarketMapping:
    return MarketMapping(
        target_key=target_key,
        market_symbol=subscription.symbol,
        market_session=subscription.market_session,
        exchange=subscription.exchange,
        bar_granularity=subscription.bar_granularity,
        exchange_session_scope=subscription.exchange_session_scope,
    )


def _assert_live_subscription_providers_configured(
    *,
    config: KernelConfig,
    subscriptions: tuple[MarketDataSubscriptionConfig, ...],
    provider_override: MarketBarsProvider | None,
    target_key: str,
) -> None:
    if provider_override is not None or config.validation is None:
        return
    market_data = config.validation.market_data
    configured = {
        market_data.provider,
        *market_data.supplemental_providers,
    }
    unsupported = sorted(
        {subscription.provider for subscription in subscriptions} - configured
    )
    if unsupported:
        joined = ", ".join(unsupported)
        raise LiveMarketDataRuntimeError(
            "live market-data warmup does not have configured provider settings for "
            f"target {target_key!r}: {joined}"
        )


def _validate_utc_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise LiveMarketDataRuntimeError(f"{field_name} must be a datetime.")
    return validate_timestamp(
        value,
        field_name=field_name,
        error_type=LiveMarketDataRuntimeError,
    ).astimezone(UTC)


def _provider_timezone_name(provider_metadata: dict[str, object]) -> str | None:
    value = provider_metadata.get("timezone")
    if isinstance(value, str) and value.strip():
        return value.strip()
    cached_value = provider_metadata.get("cached_timezone")
    if isinstance(cached_value, str) and cached_value.strip():
        return cached_value.strip()
    return None


def _freeze_live_adjustment_sidecar(
    *,
    store: SharedMarketDataStore,
    subscription: MarketDataSubscriptionConfig,
    local_archive_root: Path | None,
    provider_metadata: dict[str, object],
) -> None:
    policy = subscription.adjustment_policy
    if policy is None:
        return
    direction = adjustment_direction_for_policy(policy)
    sidecar_path = _resolve_live_adjustment_source_path(
        subscription=subscription,
        local_archive_root=local_archive_root,
        provider_metadata=provider_metadata,
        direction=direction,
    )
    if sidecar_path is None or not sidecar_path.is_file():
        raise LiveMarketDataRuntimeError(
            "configured live market-data adjustment sidecar is missing for "
            f"{subscription.symbol}: {sidecar_path}"
        )
    sidecar_content = sidecar_path.read_text(encoding="utf-8")
    stored_content = store.read_adjustment_sidecar(
        symbol=subscription.symbol,
        direction=direction,
    )
    if stored_content == sidecar_content:
        return
    store.write_adjustment_sidecar(
        symbol=subscription.symbol,
        direction=direction,
        content=sidecar_content,
    )


def _resolve_live_adjustment_source_path(
    *,
    subscription: MarketDataSubscriptionConfig,
    local_archive_root: Path | None,
    provider_metadata: dict[str, object],
    direction: AdjustmentDirection,
) -> Path | None:
    source_path = provider_metadata.get("source_path")
    if isinstance(source_path, str) and source_path.strip():
        bars_path = Path(source_path).resolve(strict=False)
        return bars_path.parent.parent.parent / "adjustments" / direction / (
            f"{normalize_archive_symbol(subscription.symbol)}.csv"
        )
    if local_archive_root is None:
        return None
    return local_archive_root / "adjustments" / direction / (
        f"{normalize_archive_symbol(subscription.symbol)}.csv"
    )


__all__ = [
    "LiveMarketDataRuntimeError",
    "LiveMarketDataWarmupReceipt",
    "prepare_live_market_data",
]
