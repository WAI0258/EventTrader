"""Shared market-context bar subscription resolution."""

from __future__ import annotations

from dataclasses import dataclass

from event_trader.config import (
    KernelConfig,
    MarketContextTargetProfileConfig,
    MarketDataSubscriptionConfig,
)
from event_trader.contracts.view_state_change import MarketMapping
from event_trader.market.contracts import MarketContextProfile
from event_trader.market.provider import market_mapping_from_profile
from event_trader.validation.market_mapping import (
    ValidationMarketMappingError,
    resolve_market_mapping,
)


@dataclass(frozen=True, slots=True)
class MarketContextBarSubscription:
    symbol: str
    provider: str
    purpose: str
    mapping: MarketMapping


def resolve_market_context_bar_subscriptions(
    *,
    config: KernelConfig,
    profile: MarketContextProfile,
    profile_config: MarketContextTargetProfileConfig,
    default_provider: str,
) -> tuple[MarketContextBarSubscription, ...]:
    configured = _configured_profile_bar_subscriptions(profile_config=profile_config)
    default_mapping = _default_market_context_mapping(config=config, profile=profile)
    subscriptions: list[MarketContextBarSubscription] = []
    for symbol in _bar_symbols(profile):
        explicit = configured.get(symbol)
        if explicit is not None:
            subscriptions.append(
                MarketContextBarSubscription(
                    symbol=explicit.symbol,
                    provider=explicit.provider,
                    purpose=explicit.purpose,
                    mapping=_mapping_from_subscription(
                        target_key=profile.target_key,
                        subscription=explicit,
                    ),
                )
            )
            continue
        subscriptions.append(
            MarketContextBarSubscription(
                symbol=symbol,
                provider=default_provider,
                purpose=(
                    "primary_tradable"
                    if symbol == profile.tradable_proxy_symbol
                    else "context_proxy"
                ),
                mapping=market_mapping_from_profile(
                    target_key=profile.target_key,
                    tradable_proxy_symbol=symbol,
                    bar_granularity=profile.bar_granularity,
                    market_session=default_mapping.market_session,
                    exchange=default_mapping.exchange,
                    exchange_session_scope=default_mapping.exchange_session_scope,
                ),
            )
        )
    return tuple(subscriptions)


def resolve_market_context_mapping_for_symbol(
    *,
    config: KernelConfig,
    profile: MarketContextProfile,
    profile_config: MarketContextTargetProfileConfig,
    symbol: str,
    bar_granularity: str | None = None,
) -> MarketMapping:
    configured = _configured_profile_bar_subscriptions(profile_config=profile_config).get(
        symbol
    )
    if configured is not None:
        return MarketMapping(
            target_key=profile.target_key,
            market_symbol=configured.symbol,
            market_session=configured.market_session,
            exchange=configured.exchange,
            bar_granularity=bar_granularity or configured.bar_granularity,
            exchange_session_scope=configured.exchange_session_scope,
        )
    default_mapping = _default_market_context_mapping(config=config, profile=profile)
    return market_mapping_from_profile(
        target_key=profile.target_key,
        tradable_proxy_symbol=symbol,
        bar_granularity=bar_granularity or profile.bar_granularity,
        market_session=default_mapping.market_session,
        exchange=default_mapping.exchange,
        exchange_session_scope=default_mapping.exchange_session_scope,
    )


def resolve_market_context_primary_mapping(
    *,
    config: KernelConfig,
    profile: MarketContextProfile,
    profile_config: MarketContextTargetProfileConfig,
) -> MarketMapping:
    for subscription in profile_config.market_data_subscriptions:
        if (
            subscription.purpose == "primary_tradable"
            or subscription.symbol == profile.tradable_proxy_symbol
        ):
            return _mapping_from_subscription(
                target_key=profile.target_key,
                subscription=subscription,
            )
    return _default_market_context_mapping(config=config, profile=profile)


def primary_market_context_bar_subscription(
    *,
    profile: MarketContextProfile,
    subscriptions: tuple[MarketContextBarSubscription, ...],
) -> MarketContextBarSubscription:
    for subscription in subscriptions:
        if (
            subscription.purpose == "primary_tradable"
            or subscription.symbol == profile.tradable_proxy_symbol
        ):
            return subscription
    raise ValueError(
        f"market_context subscriptions require a primary entry for {profile.target_key!r}."
    )


def _bar_symbols(profile: MarketContextProfile) -> tuple[str, ...]:
    symbols: list[str] = [profile.tradable_proxy_symbol]
    if "macro_cross_asset" in profile.enabled_components and profile.macro_cross_asset is not None:
        for group_symbols in profile.macro_cross_asset.proxy_groups.values():
            symbols.extend(group_symbols)
    return tuple(dict.fromkeys(symbols))


def _configured_profile_bar_subscriptions(
    *,
    profile_config: MarketContextTargetProfileConfig,
) -> dict[str, MarketDataSubscriptionConfig]:
    return {
        subscription.symbol: subscription
        for subscription in profile_config.market_data_subscriptions
    }


def _mapping_from_subscription(
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


def _default_market_context_mapping(
    *,
    config: KernelConfig,
    profile: MarketContextProfile,
) -> MarketMapping:
    try:
        return resolve_market_mapping(config, profile.target_key)
    except ValidationMarketMappingError:
        return market_mapping_from_profile(
            target_key=profile.target_key,
            tradable_proxy_symbol=profile.tradable_proxy_symbol,
            bar_granularity=profile.bar_granularity,
            market_session="continuous",
            exchange=None,
        )


__all__ = [
    "MarketContextBarSubscription",
    "primary_market_context_bar_subscription",
    "resolve_market_context_bar_subscriptions",
    "resolve_market_context_mapping_for_symbol",
    "resolve_market_context_primary_mapping",
]
