"""Replay market-data prefetch planning and execution."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from math import ceil, floor
from pathlib import Path
from typing import TypedDict, cast

from event_trader.config import (
    KernelConfig,
    MarketContextTargetProfileConfig,
)
from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
)
from event_trader.market.adjustments import (
    AdjustmentDirection,
    MarketDataAdjustmentPolicy,
    normalize_archive_symbol,
    read_adjustment_sidecar_hash,
)
from event_trader.market.context_builder import _request_start_at
from event_trader.market.contracts import (
    MarketContextProfile,
    OptionBarObservation,
    OptionContractSnapshot,
    OptionSelectionPolicy,
    OptionTradeObservation,
)
from event_trader.market.features.derivatives import select_derivatives_contracts
from event_trader.market.provider import (
    MarketBarsProvider,
    MarketOptionsProvider,
    build_default_market_bars_provider,
    build_market_bars_provider,
)
from event_trader.market.shared_store import (
    MarketDataSnapshot,
    MarketSeriesIdentity,
    SharedMarketDataProvider,
    SharedMarketDataStore,
    shared_market_data_root,
    write_workspace_snapshot_ref,
)
from event_trader.market.subscriptions import (
    MarketContextBarSubscription,
    primary_market_context_bar_subscription,
    resolve_market_context_bar_subscriptions,
)


class ReplayMarketPrefetchError(RuntimeError):
    """Raised when required replay market data cannot be prefetched."""


_PREFETCH_SCHEMA_VERSION = "market_context_prefetch.v1"
_OPTION_CONTRACTS_PER_PREFETCH_REQUEST = 40
type ReplayMarketPrefetchProgress = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ReplayMarketPrefetchEvent:
    """One event timestamp needed by the replay market-context substrate."""

    target_key: str
    visible_at: datetime


@dataclass(frozen=True, slots=True)
class ReplayMarketPrefetchReceipt:
    """Result of one replay market-data prefetch phase."""

    store: SharedMarketDataStore
    snapshot: MarketDataSnapshot
    reused_existing: bool

    def cached_provider(self) -> SharedMarketDataProvider:
        target_key = self.snapshot.metadata.get("target_key")
        providers_payload = self.snapshot.metadata.get("providers")
        providers = providers_payload if isinstance(providers_payload, dict) else {}
        provider_names = {
            (target_key, symbol): provider
            for symbol, provider in providers.items()
            if isinstance(target_key, str)
            and isinstance(symbol, str)
            and isinstance(provider, str)
        }
        return SharedMarketDataProvider(
            store=self.store,
            provider_name=str(self.snapshot.metadata.get("provider", "unknown")),
            snapshot_id=self.snapshot.snapshot_id,
            provider_names=provider_names,
        )

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id


@dataclass(frozen=True, slots=True)
class _BarPrefetchResult:
    subscription: MarketContextBarSubscription
    bars: tuple[MarketDataBar, ...]
    metadata: dict[str, object]
    error: Exception | None


@dataclass(frozen=True, slots=True)
class _OptionDateBucket:
    event_date: date
    as_of_at: datetime
    price_reference: float
    price_min: float
    price_max: float
    policy: OptionSelectionPolicy


class _OptionUniverseReconstructionMetadata(TypedDict):
    candidate_count: int
    confirmed_count: int
    failed_count: int
    skipped_count: int
    coverage_reason: str


def _emit_progress(
    progress: ReplayMarketPrefetchProgress | None,
    message: str,
) -> None:
    if progress is not None:
        progress(message)


def _should_emit_bucket_progress(index: int, total: int) -> bool:
    if total < 20:
        return False
    if index == total:
        return True
    step = max(total // 4, 20)
    return index % step == 0


def prefetch_replay_market_data(
    *,
    config: KernelConfig,
    target_key: str,
    run_id: str,
    events: tuple[ReplayMarketPrefetchEvent, ...],
    window_start: datetime,
    window_end: datetime,
    provider: MarketBarsProvider | None = None,
    progress: ReplayMarketPrefetchProgress | None = None,
) -> ReplayMarketPrefetchReceipt | None:
    """Fetch replay market observations once and persist them for local replay reads."""
    return _prefetch_shared_market_data(
        config=config,
        target_key=target_key,
        run_id=run_id,
        events=events,
        window_start=window_start,
        window_end=window_end,
        provider=provider,
        progress=progress,
    )


def _prefetch_shared_market_data(
    *,
    config: KernelConfig,
    target_key: str,
    run_id: str,
    events: tuple[ReplayMarketPrefetchEvent, ...],
    window_start: datetime,
    window_end: datetime,
    provider: MarketBarsProvider | None,
    progress: ReplayMarketPrefetchProgress | None,
) -> ReplayMarketPrefetchReceipt | None:
    """Materialize and pin replay bars in the shared catalog."""
    if config.market_context is None or not config.market_context.enabled:
        return None
    profile_config = config.market_context.target_profiles.get(target_key)
    if profile_config is None:
        return None
    if not events:
        raise ReplayMarketPrefetchError("replay market prefetch requires at least one event.")
    profile = MarketContextProfile.from_config(
        target_key=target_key,
        profile_config=profile_config,
    )
    store = SharedMarketDataStore(shared_market_data_root(config))
    remote_provider = _resolve_replay_prefetch_provider(
        config=config,
        provider=provider,
    )
    bar_subscriptions = _resolve_bar_subscriptions(
        config=config,
        profile=profile,
        profile_config=profile_config,
        default_provider=_default_bar_subscription_provider(
            config=config,
            provider=remote_provider,
        ),
    )
    _assert_supported_bar_subscription_providers(
        config=config,
        provider_override=provider,
        subscriptions=bar_subscriptions,
    )
    primary_subscription = _primary_bar_subscription(
        profile=profile,
        subscriptions=bar_subscriptions,
    )
    target_events = tuple(event for event in events if event.target_key == target_key)
    if not target_events:
        raise ReplayMarketPrefetchError(
            f"replay market prefetch has no events for target {target_key!r}."
        )
    prefetch_start = _request_start_at(
        profile=profile,
        as_of_at=min(event.visible_at for event in target_events),
        market_session=primary_subscription.mapping.market_session,
    )
    fingerprint = _prefetch_fingerprint(
        config=config,
        profile=profile,
        profile_config=profile_config,
        target_key=target_key,
        run_id=run_id,
        events=target_events,
        window_start=window_start,
        window_end=window_end,
        prefetch_start=prefetch_start,
        subscriptions=bar_subscriptions,
        provider_override=provider,
    )
    reads: list[tuple[MarketSeriesIdentity, datetime, datetime]] = []
    all_reused = True
    target_bars: tuple[MarketDataBar, ...] = ()
    option_source_metadata: dict[str, object] = {}
    option_failures: list[dict[str, object]] = []
    for subscription in bar_subscriptions:
        identity = MarketSeriesIdentity.from_mapping(
            subscription.mapping,
            provider=subscription.provider,
            adjustment_policy=(
                _market_context_subscription_adjustment_policy(
                    profile_config=profile_config,
                    market_symbol=subscription.symbol,
                )
                or "raw"
            ),
        )
        had_rows = store.covers_window(
            identity,
            start_at=prefetch_start,
            end_at=window_end,
        )
        remote = remote_provider
        if subscription.provider != _default_bar_subscription_provider(
            config=config,
            provider=remote_provider,
        ) and provider is None:
            remote = build_market_data_provider_for_replay_subscription(
                config=config,
                provider_name=subscription.provider,
            )
        try:
            def fetch(
                fetch_start: datetime,
                fetch_end: datetime,
                *,
                fetch_remote: MarketBarsProvider = remote,
                fetch_mapping=subscription.mapping,
            ) -> MarketDataSeries:
                return fetch_remote.read_series(
                    fetch_mapping,
                    start_at=fetch_start,
                    end_at=fetch_end,
                )

            series = store.materialize(
                identity,
                start_at=prefetch_start,
                end_at=window_end,
                fetch=fetch,
            )
        finally:
            if remote is not remote_provider:
                _close_market_bars_provider(remote)
        if _is_primary_bar_subscription(profile, subscription):
            target_bars = series.bars
        all_reused = all_reused and had_rows
        reads.append((identity, prefetch_start, window_end))
    if not target_bars:
        raise ReplayMarketPrefetchError(
            f"required primary bars unavailable for {profile.tradable_proxy_symbol}."
        )
    if "derivatives" in profile.enabled_components and profile.derivatives is not None:
        _prefetch_options(
            store=store,
            provider=remote_provider,
            profile=profile,
            policy=profile.derivatives,
            target_bars=target_bars,
            events=target_events,
            window_start=window_start,
            window_end=window_end,
            source_metadata=option_source_metadata,
            failures=option_failures,
            progress=progress,
        )
    snapshot = store.create_snapshot(
        mode="replay",
        reads=reads,
        metadata={
            "target_key": target_key,
            "run_id": run_id,
            "provider": _default_bar_subscription_provider(
                config=config,
                provider=remote_provider,
            ),
            "providers": {
                subscription.symbol: subscription.provider
                for subscription in bar_subscriptions
            },
            "prefetch_fingerprint": fingerprint,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "option_source_metadata": option_source_metadata,
            "option_failures": option_failures,
        },
    )
    write_workspace_snapshot_ref(
        config.workspace_root,
        target_key=target_key,
        run_id=run_id,
        snapshot_id=snapshot.snapshot_id,
    )
    _emit_progress(
        progress,
        "market prefetch: shared snapshot "
        f"snapshot_id={snapshot.snapshot_id} reused_existing={all_reused}",
    )
    return ReplayMarketPrefetchReceipt(
        store=store,
        snapshot=snapshot,
        reused_existing=all_reused,
    )

def _prefetch_options(
    *,
    store: SharedMarketDataStore,
    provider: MarketBarsProvider,
    profile: MarketContextProfile,
    policy: OptionSelectionPolicy,
    target_bars: tuple[MarketDataBar, ...],
    events: tuple[ReplayMarketPrefetchEvent, ...],
    window_start: datetime,
    window_end: datetime,
    source_metadata: dict[str, object],
    failures: list[dict[str, object]],
    progress: ReplayMarketPrefetchProgress | None,
) -> tuple[OptionContractSnapshot, ...]:
    underlying_symbol = profile.derivatives_underlying_symbol
    if underlying_symbol is None:
        raise ReplayMarketPrefetchError(
            "derivatives prefetch requires derivatives_underlying_symbol."
        )
    options_provider = _as_options_provider(provider)
    if options_provider is None:
        _emit_progress(progress, "market prefetch: options unavailable provider=none")
        failures.append({"component": "options", "reason": "provider_unavailable"})
        store.write_option_chain_metadata(
            underlying_symbol=underlying_symbol,
            contracts=(),
        )
        return ()
    buckets = _option_date_buckets(
        events=events,
        target_bars=target_bars,
        policy=policy,
    )
    if not buckets:
        _emit_progress(progress, "market prefetch: options unavailable buckets=0")
        failures.append({"component": "options", "reason": "underlying_price_unavailable"})
        return ()
    _emit_progress(
        progress,
        "market prefetch: options "
        f"buckets={len(buckets)} underlying={underlying_symbol}",
    )
    replay_days = max((window_end.date() - window_start.date()).days, 0)
    expanded_policy = replace(
        policy,
        expiry_days_max=max(policy.expiry_days_max, replay_days + policy.expiry_days_max),
    )
    lookback_start = window_start - timedelta(hours=expanded_policy.lookback_hours)
    chain_by_symbol: dict[str, OptionContractSnapshot] = {}
    selected_by_symbol: dict[str, OptionContractSnapshot] = {}
    chain_bucket_failures = 0
    chain_sources: set[str] = set()
    reconstruction_candidate_count = 0
    reconstruction_confirmed_count = 0
    reconstruction_failed_count = 0
    reconstruction_skipped_count = 0
    reconstruction_reasons: list[str] = []
    for bucket_index, bucket in enumerate(buckets, start=1):
        bucket_policy = replace(
            bucket.policy,
            expiry_days_max=expanded_policy.expiry_days_max,
        )
        chain_reader = getattr(options_provider, "read_option_chain_metadata", None)
        try:
            if callable(chain_reader):
                bucket_chain = cast(
                    tuple[OptionContractSnapshot, ...],
                    chain_reader(
                        underlying_symbol=underlying_symbol,
                        as_of_at=bucket.as_of_at,
                        underlying_price=bucket.price_reference,
                        policy=bucket_policy,
                    ),
                )
            else:
                bucket_chain = tuple(
                    _metadata_only_contract(row)
                    for row in options_provider.read_option_chain(
                        underlying_symbol=underlying_symbol,
                        as_of_at=bucket.as_of_at,
                        underlying_price=bucket.price_reference,
                        policy=bucket_policy,
                    )
                )
            _consume_provider_metadata(
                provider,
                source_metadata,
                prefix=f"options_chain_{bucket.event_date.isoformat()}_",
            )
            if bucket_chain:
                chain_sources.add("latest_chain_metadata")
        except Exception as exc:
            chain_bucket_failures += 1
            failures.append(
                _failure(
                    "option_chain_metadata",
                    f"{underlying_symbol}:{bucket.event_date.isoformat()}",
                    exc,
                )
            )
            continue
        if (
            not bucket_chain
            and bucket_policy.historical_universe_reconstruction_enabled
        ):
            (
                bucket_chain,
                reconstruction_metadata,
            ) = _reconstruct_option_universe_for_bucket(
                provider=options_provider,
                underlying_symbol=underlying_symbol,
                as_of_at=bucket.as_of_at,
                policy=bucket_policy,
                price_min=bucket.price_min,
                price_max=bucket.price_max,
                start_at=lookback_start,
                end_at=window_end,
            )
            reconstruction_candidate_count += reconstruction_metadata["candidate_count"]
            reconstruction_confirmed_count += reconstruction_metadata["confirmed_count"]
            reconstruction_failed_count += reconstruction_metadata["failed_count"]
            reconstruction_skipped_count += reconstruction_metadata["skipped_count"]
            reconstruction_reasons.append(
                f"{bucket.event_date.isoformat()}:{reconstruction_metadata['coverage_reason']}"
            )
            if bucket_chain:
                chain_sources.add("historical_universe_reconstruction")
        for contract in bucket_chain:
            chain_by_symbol[contract.contract_symbol] = contract
        for contract in select_derivatives_contracts(
            bucket_chain,
            underlying_price=bucket.price_reference,
            as_of_at=bucket.as_of_at,
            policy=bucket_policy,
        ):
            selected_by_symbol[contract.contract_symbol] = contract
        if _should_emit_bucket_progress(bucket_index, len(buckets)):
            _emit_progress(
                progress,
                "market prefetch: option chains "
                f"{bucket_index}/{len(buckets)} contracts={len(chain_by_symbol)} "
                f"selected={len(selected_by_symbol)} failures={chain_bucket_failures}",
            )

    chain = tuple(chain_by_symbol[symbol] for symbol in sorted(chain_by_symbol))
    selected = tuple(selected_by_symbol[symbol] for symbol in sorted(selected_by_symbol))
    if not chain:
        _emit_progress(
            progress,
            "market prefetch: options complete contracts=0 selected=0",
        )
        store.write_option_chain_metadata(
            underlying_symbol=underlying_symbol,
            contracts=(),
        )
        source_metadata["contract_universe_source"] = _option_universe_source_label(chain_sources)
        source_metadata["contract_universe_visibility"] = (
            "metadata_only_not_historical_snapshot"
        )
        source_metadata["option_chain_bucket_count"] = len(buckets)
        source_metadata["option_chain_failed_bucket_count"] = chain_bucket_failures
        source_metadata["historical_universe_candidate_count"] = reconstruction_candidate_count
        source_metadata["historical_universe_confirmed_count"] = (
            reconstruction_confirmed_count
        )
        source_metadata["historical_universe_failed_count"] = (
            reconstruction_failed_count
        )
        source_metadata["historical_universe_skipped_count"] = (
            reconstruction_skipped_count
        )
        source_metadata["historical_universe_coverage_reason"] = _merge_reconstruction_reason(
            reconstruction_reasons,
            chain_sources=chain_sources,
        )
        return ()
    store.write_option_chain_metadata(
        underlying_symbol=underlying_symbol,
        contracts=chain,
    )
    _emit_progress(
        progress,
        "market prefetch: options selected "
        f"contracts={len(chain)} selected={len(selected)}",
    )
    trades: tuple[OptionTradeObservation, ...] = ()
    bars: tuple[OptionBarObservation, ...] = ()
    if selected and policy.include_historical_trades:
        _emit_progress(progress, "market prefetch: option trades")
        trades = _read_option_trades_for_prefetch(
            provider=options_provider,
            metadata_provider=provider,
            contracts=selected,
            start_at=lookback_start,
            end_at=window_end,
            policy=policy,
            source_metadata=source_metadata,
            failures=failures,
            underlying_symbol=underlying_symbol,
        )
    if selected and policy.include_historical_bars:
        _emit_progress(progress, "market prefetch: option bars")
        bars = _read_option_bars_for_prefetch(
            provider=options_provider,
            metadata_provider=provider,
            contracts=selected,
            start_at=lookback_start,
            end_at=window_end,
            policy=policy,
            source_metadata=source_metadata,
            failures=failures,
            underlying_symbol=underlying_symbol,
        )
    store.write_option_trades(trades=trades)
    store.write_option_bars(bars=bars)
    source_metadata["contract_universe_source"] = _option_universe_source_label(chain_sources)
    source_metadata["contract_universe_visibility"] = "metadata_only_not_historical_snapshot"
    source_metadata["option_chain_bucket_count"] = len(buckets)
    source_metadata["option_chain_failed_bucket_count"] = chain_bucket_failures
    source_metadata["historical_universe_candidate_count"] = reconstruction_candidate_count
    source_metadata["historical_universe_confirmed_count"] = (
        reconstruction_confirmed_count
    )
    source_metadata["historical_universe_failed_count"] = reconstruction_failed_count
    source_metadata["historical_universe_skipped_count"] = reconstruction_skipped_count
    source_metadata["historical_universe_coverage_reason"] = _merge_reconstruction_reason(
        reconstruction_reasons,
        chain_sources=chain_sources,
    )
    source_metadata["prefetched_option_contract_count"] = len(chain)
    source_metadata["prefetched_selected_option_contract_count"] = len(selected)
    source_metadata["prefetched_option_trade_count"] = len(trades)
    source_metadata["prefetched_option_bar_count"] = len(bars)
    _emit_progress(
        progress,
        "market prefetch: options complete "
        f"trades={len(trades)} bars={len(bars)} failures={len(failures)}",
    )
    return chain


def _resolve_bar_subscriptions(
    *,
    config: KernelConfig,
    profile: MarketContextProfile,
    profile_config: MarketContextTargetProfileConfig,
    default_provider: str,
) -> tuple[MarketContextBarSubscription, ...]:
    return resolve_market_context_bar_subscriptions(
        config=config,
        profile=profile,
        profile_config=profile_config,
        default_provider=default_provider,
    )


def _default_bar_subscription_provider(
    *,
    config: KernelConfig,
    provider: MarketBarsProvider,
) -> str:
    if config.validation is not None:
        return config.validation.market_data.provider
    return _provider_name(provider)


def _primary_bar_subscription(
    *,
    profile: MarketContextProfile,
    subscriptions: tuple[MarketContextBarSubscription, ...],
) -> MarketContextBarSubscription:
    try:
        return primary_market_context_bar_subscription(
            profile=profile,
            subscriptions=subscriptions,
        )
    except ValueError as exc:
        raise ReplayMarketPrefetchError(str(exc)) from exc


def _is_primary_bar_subscription(
    profile: MarketContextProfile,
    subscription: MarketContextBarSubscription,
) -> bool:
    return (
        subscription.purpose == "primary_tradable"
        or subscription.symbol == profile.tradable_proxy_symbol
    )


def _assert_supported_bar_subscription_providers(
    *,
    config: KernelConfig,
    provider_override: MarketBarsProvider | None,
    subscriptions: tuple[MarketContextBarSubscription, ...],
) -> None:
    if provider_override is not None:
        return
    configured_provider = (
        None if config.validation is None else config.validation.market_data.provider
    )
    unsupported = sorted(
        {
            subscription.provider
            for subscription in subscriptions
            if subscription.provider not in {configured_provider, "local_archive"}
        }
    )
    if unsupported:
        joined = ", ".join(unsupported)
        raise ReplayMarketPrefetchError(
            "replay market prefetch subscription providers are not available from the "
            f"configured validation.market_data provider {configured_provider!r}: {joined}"
        )
    requires_local_archive = any(
        subscription.provider == "local_archive" for subscription in subscriptions
    )
    if requires_local_archive and (
        config.validation is None
        or config.validation.market_data.local_archive_root is None
    ):
        raise ReplayMarketPrefetchError(
            "replay market prefetch local_archive subscriptions require "
            "validation.market_data.local_archive_root."
        )


def _build_prefetch_subscription_provider(
    *,
    config: KernelConfig,
    provider_name: str,
) -> MarketBarsProvider:
    configured_provider = (
        None if config.validation is None else config.validation.market_data.provider
    )
    if provider_name == configured_provider:
        return _build_configured_market_bars_provider(config)
    try:
        return build_market_bars_provider(config, provider_name=provider_name)
    except ValueError as exc:
        raise ReplayMarketPrefetchError(str(exc)) from exc


def _close_market_bars_provider(provider: MarketBarsProvider) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        close()


def _option_date_buckets(
    *,
    events: tuple[ReplayMarketPrefetchEvent, ...],
    target_bars: tuple[MarketDataBar, ...],
    policy: OptionSelectionPolicy,
) -> tuple[_OptionDateBucket, ...]:
    buckets: list[_OptionDateBucket] = []
    event_dates = tuple(sorted({event.visible_at.date() for event in events}))
    for event_date in event_dates:
        event_times = tuple(
            event.visible_at for event in events if event.visible_at.date() == event_date
        )
        if not event_times:
            continue
        as_of_at = max(event_times)
        day_start = datetime.combine(event_date, time.min, tzinfo=as_of_at.tzinfo)
        day_bars = tuple(
            bar
            for bar in target_bars
            if day_start < bar.end_at <= as_of_at
        )
        if not day_bars:
            latest_close = _latest_close_at_or_before(target_bars, as_of_at)
            if latest_close is None:
                continue
            price_min = latest_close
            price_max = latest_close
        else:
            price_min = min(bar.low_price for bar in day_bars)
            price_max = max(bar.high_price for bar in day_bars)
        price_reference, bucket_policy = _option_policy_for_price_range(
            policy=policy,
            price_min=price_min,
            price_max=price_max,
        )
        buckets.append(
            _OptionDateBucket(
                event_date=event_date,
                as_of_at=as_of_at,
                price_reference=price_reference,
                price_min=price_min,
                price_max=price_max,
                policy=bucket_policy,
            )
        )
    return tuple(buckets)


def _option_policy_for_price_range(
    *,
    policy: OptionSelectionPolicy,
    price_min: float,
    price_max: float,
) -> tuple[float, OptionSelectionPolicy]:
    price_reference = (price_min + price_max) / 2.0
    range_window = 0.0
    if price_reference > 0:
        range_window = max(
            abs(price_reference - price_min),
            abs(price_max - price_reference),
        ) / price_reference
    return (
        price_reference,
        replace(
            policy,
            strike_pct_window=policy.strike_pct_window + range_window,
        ),
    )


def _option_universe_source_label(chain_sources: set[str]) -> str:
    if (
        "latest_chain_metadata" in chain_sources
        and "historical_universe_reconstruction" in chain_sources
    ):
        return "mixed_chain_and_reconstruction"
    if "historical_universe_reconstruction" in chain_sources:
        return "historical_universe_reconstruction"
    if "latest_chain_metadata" in chain_sources:
        return "latest_chain_metadata"
    return "unavailable"


def _merge_reconstruction_reason(
    reasons: list[str],
    *,
    chain_sources: set[str],
) -> str:
    if reasons:
        return ";".join(reasons)
    if chain_sources:
        return "chain_metadata_only"
    return "chain_metadata_empty"


def _reconstruct_option_universe_for_bucket(
    *,
    provider: MarketOptionsProvider,
    underlying_symbol: str,
    as_of_at: datetime,
    policy: OptionSelectionPolicy,
    price_min: float,
    price_max: float,
    start_at: datetime,
    end_at: datetime,
) -> tuple[
    tuple[OptionContractSnapshot, ...],
    _OptionUniverseReconstructionMetadata,
]:
    candidates, skipped_count = _generate_option_candidates(
        underlying_symbol=underlying_symbol,
        as_of_at=as_of_at,
        policy=policy,
        price_min=price_min,
        price_max=price_max,
    )
    candidate_count = len(candidates)
    if candidate_count == 0:
        return (), {
            "candidate_count": 0,
            "confirmed_count": 0,
            "failed_count": 0,
            "skipped_count": skipped_count,
            "coverage_reason": "no_historical_candidates",
        }
    if not (policy.include_historical_trades or policy.include_historical_bars):
        return (), {
            "candidate_count": candidate_count,
            "confirmed_count": 0,
            "failed_count": 0,
            "skipped_count": skipped_count,
            "coverage_reason": "historical_observation_checks_disabled",
        }

    confirmed, failed_count = _confirm_option_candidates_with_history(
        provider=provider,
        candidates=candidates,
        policy=policy,
        start_at=start_at,
        end_at=end_at,
    )
    if confirmed:
        coverage_reason = (
            "partially_confirmed"
            if len(confirmed) < candidate_count
            else "fully_confirmed"
        )
    else:
        coverage_reason = "no_confirmed_candidates"
    return (
        confirmed,
        {
            "candidate_count": candidate_count,
            "confirmed_count": len(confirmed),
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "coverage_reason": coverage_reason,
        },
    )


def _confirm_option_candidates_with_history(
    *,
    provider: MarketOptionsProvider,
    candidates: tuple[OptionContractSnapshot, ...],
    policy: OptionSelectionPolicy,
    start_at: datetime,
    end_at: datetime,
) -> tuple[tuple[OptionContractSnapshot, ...], int]:
    if not candidates:
        return (), 0
    confirmed_symbols: set[str] = set()
    failed = 0
    for chunk in _chunk_contracts(candidates):
        if policy.include_historical_trades:
            try:
                for trade in provider.read_option_trades(
                    contracts=chunk,
                    start_at=start_at,
                    end_at=end_at,
                    policy=policy,
                ):
                    confirmed_symbols.add(trade.contract_symbol)
            except Exception:
                failed += 1
        if policy.include_historical_bars:
            try:
                for bar in provider.read_option_bars(
                    contracts=chunk,
                    start_at=start_at,
                    end_at=end_at,
                    policy=policy,
                ):
                    confirmed_symbols.add(bar.contract_symbol)
            except Exception:
                failed += 1
    return (
        tuple(
            contract
            for contract in candidates
            if contract.contract_symbol in confirmed_symbols
        ),
        failed,
    )


def _generate_option_candidates(
    *,
    underlying_symbol: str,
    as_of_at: datetime,
    policy: OptionSelectionPolicy,
    price_min: float,
    price_max: float,
) -> tuple[tuple[OptionContractSnapshot, ...], int]:
    if policy.historical_universe_strike_step <= 0:
        return (), 0
    if price_min <= 0 or price_max <= 0:
        return (), 0
    expiry_min_date = as_of_at.date() + timedelta(days=policy.expiry_days_min)
    expiry_max_date = as_of_at.date() + timedelta(days=policy.expiry_days_max)
    if expiry_max_date < expiry_min_date:
        return (), 0
    reference_price = (price_min + price_max) / 2.0
    strike_window_min = min(price_min, price_max) * (1.0 - policy.strike_pct_window)
    strike_window_max = max(price_min, price_max) * (1.0 + policy.strike_pct_window)
    strike_step = policy.historical_universe_strike_step
    min_strike_index = ceil((strike_window_min / strike_step) - 1e-12)
    max_strike_index = floor((strike_window_max / strike_step) + 1e-12)
    if max_strike_index < min_strike_index:
        return (), 0
    expiry_dates = tuple(
        (
            expiry_min_date + timedelta(days=day_offset)
        ) for day_offset in range((expiry_max_date - expiry_min_date).days + 1)
    )
    total_strikes = max_strike_index - min_strike_index + 1
    total_candidates = len(expiry_dates) * total_strikes * 2
    if total_candidates <= 0:
        return (), 0
    max_candidates = min(
        total_candidates,
        policy.historical_universe_max_candidates_per_bucket,
    )
    skipped_count = total_candidates - max_candidates
    strike_indices = tuple(
        sorted(
            range(min_strike_index, max_strike_index + 1),
            key=lambda strike_index: (
                abs((strike_index * strike_step) - reference_price),
                strike_index * strike_step,
            ),
        )
    )
    generated: list[OptionContractSnapshot] = []
    for strike_index in strike_indices:
        strike = round(strike_index * strike_step, 3)
        for expiry_date in expiry_dates:
            for option_type in ("call", "put"):
                if len(generated) >= max_candidates:
                    break
                generated.append(
                    _build_option_osi(
                        underlying_symbol=underlying_symbol,
                        expiry_date=expiry_date.isoformat(),
                        option_type=option_type,
                        strike=strike,
                    )
                )
            if len(generated) >= max_candidates:
                break
        if len(generated) >= max_candidates:
            break
    return tuple(generated), skipped_count


def _build_option_osi(
    *,
    underlying_symbol: str,
    expiry_date: str,
    option_type: str,
    strike: float,
) -> OptionContractSnapshot:
    expiry_code = expiry_date.replace("-", "")[2:]
    contract_symbol = (
        f"{underlying_symbol}{expiry_code}"
        f"{'C' if option_type == 'call' else 'P'}{int(round(strike * 1000)):08d}"
    )
    return OptionContractSnapshot(
        contract_symbol=contract_symbol,
        underlying_symbol=underlying_symbol,
        option_type=option_type,
        expiry_date=expiry_date,
        strike_price=round(strike, 3),
        observed_at=None,
    )


def _read_option_trades_for_prefetch(
    *,
    provider: MarketOptionsProvider,
    metadata_provider: MarketBarsProvider,
    contracts: tuple[OptionContractSnapshot, ...],
    start_at: datetime,
    end_at: datetime,
    policy: OptionSelectionPolicy,
    source_metadata: dict[str, object],
    failures: list[dict[str, object]],
    underlying_symbol: str,
) -> tuple[OptionTradeObservation, ...]:
    trades: list[OptionTradeObservation] = []
    for chunk_index, chunk in enumerate(_chunk_contracts(contracts), start=1):
        try:
            trades.extend(
                provider.read_option_trades(
                    contracts=chunk,
                    start_at=start_at,
                    end_at=end_at,
                    policy=policy,
                )
            )
            _consume_provider_metadata(
                metadata_provider,
                source_metadata,
                prefix=f"options_trades_chunk_{chunk_index}_",
            )
        except Exception as exc:
            failures.append(_failure("option_trades", underlying_symbol, exc))
    return tuple(trades)


def _read_option_bars_for_prefetch(
    *,
    provider: MarketOptionsProvider,
    metadata_provider: MarketBarsProvider,
    contracts: tuple[OptionContractSnapshot, ...],
    start_at: datetime,
    end_at: datetime,
    policy: OptionSelectionPolicy,
    source_metadata: dict[str, object],
    failures: list[dict[str, object]],
    underlying_symbol: str,
) -> tuple[OptionBarObservation, ...]:
    bars: list[OptionBarObservation] = []
    for chunk_index, chunk in enumerate(_chunk_contracts(contracts), start=1):
        try:
            bars.extend(
                provider.read_option_bars(
                    contracts=chunk,
                    start_at=start_at,
                    end_at=end_at,
                    policy=policy,
                )
            )
            _consume_provider_metadata(
                metadata_provider,
                source_metadata,
                prefix=f"options_bars_chunk_{chunk_index}_",
            )
        except Exception as exc:
            failures.append(_failure("option_bars", underlying_symbol, exc))
    return tuple(bars)


def _chunk_contracts(
    contracts: tuple[OptionContractSnapshot, ...],
) -> tuple[tuple[OptionContractSnapshot, ...], ...]:
    return tuple(
        contracts[index : index + _OPTION_CONTRACTS_PER_PREFETCH_REQUEST]
        for index in range(0, len(contracts), _OPTION_CONTRACTS_PER_PREFETCH_REQUEST)
    )


def _prefetch_bar_results(
    *,
    config: KernelConfig,
    provider: MarketBarsProvider,
    provider_override: MarketBarsProvider | None,
    default_provider_name: str,
    subscriptions: tuple[MarketContextBarSubscription, ...],
    start_at: datetime,
    end_at: datetime,
) -> tuple[_BarPrefetchResult, ...]:
    if provider_override is not None:
        return tuple(
            _read_prefetch_bars(
                provider=provider,
                subscription=subscription,
                start_at=start_at,
                end_at=end_at,
                close_after_read=False,
            )
            for subscription in subscriptions
        )
    if len(subscriptions) <= 1:
        subscription = subscriptions[0]
        if subscription.provider == default_provider_name:
            return (
                _read_prefetch_bars(
                    provider=provider,
                    subscription=subscription,
                    start_at=start_at,
                    end_at=end_at,
                    close_after_read=False,
                ),
            )
        return (
            _read_prefetch_bars(
                provider=_build_prefetch_subscription_provider(
                    config=config,
                    provider_name=subscription.provider,
                ),
                subscription=subscription,
                start_at=start_at,
                end_at=end_at,
                close_after_read=True,
            ),
        )

    max_workers = min(4, len(subscriptions))

    def read_with_dedicated_provider(
        subscription: MarketContextBarSubscription,
    ) -> _BarPrefetchResult:
        return _read_prefetch_bars(
            provider=_build_prefetch_subscription_provider(
                config=config,
                provider_name=subscription.provider,
            ),
            subscription=subscription,
            start_at=start_at,
            end_at=end_at,
            close_after_read=True,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return tuple(executor.map(read_with_dedicated_provider, subscriptions))


def _read_prefetch_bars(
    *,
    provider: MarketBarsProvider,
    subscription: MarketContextBarSubscription,
    start_at: datetime,
    end_at: datetime,
    close_after_read: bool,
) -> _BarPrefetchResult:
    try:
        series = provider.read_series(
            subscription.mapping,
            start_at=start_at,
            end_at=end_at,
        )
        metadata = _prefetch_bar_metadata(provider, subscription)
        return _BarPrefetchResult(
            subscription=subscription,
            bars=tuple(series.bars),
            metadata=metadata,
            error=None,
        )
    except Exception as exc:
        metadata = _prefetch_bar_metadata(provider, subscription)
        return _BarPrefetchResult(
            subscription=subscription,
            bars=(),
            metadata=metadata,
            error=exc,
        )
    finally:
        if close_after_read:
            _close_market_bars_provider(provider)


def _prefetch_bar_metadata(
    provider: MarketBarsProvider,
    subscription: MarketContextBarSubscription,
) -> dict[str, object]:
    metadata = _pop_provider_metadata(provider)
    metadata.update(
        {
            "configured_provider": subscription.provider,
            "configured_purpose": subscription.purpose,
            "configured_market_symbol": subscription.mapping.market_symbol,
            "configured_market_session": subscription.mapping.market_session,
            "configured_exchange": subscription.mapping.exchange,
            "configured_exchange_session_scope": subscription.mapping.exchange_session_scope,
            "configured_bar_granularity": subscription.mapping.bar_granularity,
        }
    )
    return metadata


def _resolve_replay_prefetch_provider(
    *,
    config: KernelConfig,
    provider: MarketBarsProvider | None,
) -> MarketBarsProvider:
    if provider is not None:
        return provider
    return _build_configured_market_bars_provider(config)


def _build_configured_market_bars_provider(config: KernelConfig) -> MarketBarsProvider:
    provider = build_default_market_bars_provider(config)
    if provider is None:
        raise ReplayMarketPrefetchError(
            "replay market prefetch requires a configured market-data provider "
            "or an explicit provider override."
        )
    return provider


def build_market_data_provider_for_replay_subscription(
    *,
    config: KernelConfig,
    provider_name: str,
) -> MarketBarsProvider:
    """Build one explicitly configured subscription provider for shared materialization."""
    return build_market_bars_provider(config, provider_name=provider_name)


def _prefetch_fingerprint(
    *,
    config: KernelConfig,
    profile: MarketContextProfile,
    profile_config: MarketContextTargetProfileConfig,
    target_key: str,
    run_id: str,
    events: tuple[ReplayMarketPrefetchEvent, ...],
    window_start: datetime,
    window_end: datetime,
    prefetch_start: datetime,
    subscriptions: tuple[MarketContextBarSubscription, ...],
    provider_override: MarketBarsProvider | None,
) -> str:
    payload = {
        "schema_version": _PREFETCH_SCHEMA_VERSION,
        "target_key": target_key,
        "run_id": run_id,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "prefetch_start": prefetch_start.isoformat(),
        "events": _events_fingerprint_payload(events),
        "profile": _profile_fingerprint_payload(profile),
        "subscriptions": [
            {
                "symbol": subscription.symbol,
                "provider": subscription.provider,
                "purpose": subscription.purpose,
                "adjustment_policy": _market_context_subscription_adjustment_policy(
                    profile_config=profile_config,
                    market_symbol=subscription.symbol,
                ),
                "market_session": subscription.mapping.market_session,
                "exchange": subscription.mapping.exchange,
                "exchange_session_scope": subscription.mapping.exchange_session_scope,
                "bar_granularity": subscription.mapping.bar_granularity,
            }
            for subscription in subscriptions
        ],
        "adjustments": _adjustment_fingerprint_payload(
            config=config,
            profile_config=profile_config,
            subscriptions=subscriptions,
        ),
        "provider_override": (
            _provider_instance_fingerprint_payload(provider_override)
            if provider_override is not None
            else None
        ),
        "subscription_provider_configs": {
            provider_name: _provider_config_fingerprint_payload(
                config=config,
                provider_name=provider_name,
            )
            for provider_name in sorted({subscription.provider for subscription in subscriptions})
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _events_fingerprint_payload(
    events: tuple[ReplayMarketPrefetchEvent, ...],
) -> dict[str, object]:
    visible_at_values = tuple(
        sorted(event.visible_at.isoformat() for event in events)
    )
    event_dates = tuple(sorted({event.visible_at.date().isoformat() for event in events}))
    visible_at_hash = sha256(
        json.dumps(visible_at_values, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "event_count": len(visible_at_values),
        "event_dates": list(event_dates),
        "visible_at_hash": visible_at_hash,
    }


def _profile_fingerprint_payload(profile: MarketContextProfile) -> dict[str, object]:
    return {
        "target_key": profile.target_key,
        "tradable_proxy_symbol": profile.tradable_proxy_symbol,
        "derivatives_underlying_symbol": profile.derivatives_underlying_symbol,
        "bar_granularity": profile.bar_granularity,
        "enabled_components": list(profile.enabled_components),
        "price_volume": {
            "lookback_bars": profile.price_volume_lookback_bars,
            "trailing_return_windows": list(profile.trailing_return_windows),
        },
        "technical": {
            "ema_periods": list(profile.ema_periods),
            "ichimoku_enabled": profile.ichimoku_enabled,
        },
        "macro_cross_asset": _macro_fingerprint_payload(profile),
        "derivatives": _derivatives_fingerprint_payload(profile.derivatives),
    }


def _macro_fingerprint_payload(profile: MarketContextProfile) -> dict[str, object] | None:
    if profile.macro_cross_asset is None:
        return None
    return {
        "lookback_bars": profile.macro_cross_asset.lookback_bars,
        "trailing_return_windows": list(profile.macro_cross_asset.trailing_return_windows),
        "proxy_groups": {
            group: list(symbols)
            for group, symbols in sorted(profile.macro_cross_asset.proxy_groups.items())
        },
        "group_pressure_rules": dict(
            sorted(profile.macro_cross_asset.group_pressure_rules.items())
        ),
    }


def _derivatives_fingerprint_payload(
    policy: OptionSelectionPolicy | None,
) -> dict[str, object] | None:
    if policy is None:
        return None
    return {
        "data_feed": policy.data_feed,
        "chain_max_rows": policy.chain_max_rows,
        "max_contracts_per_snapshot": policy.max_contracts_per_snapshot,
        "expiry_days_min": policy.expiry_days_min,
        "expiry_days_max": policy.expiry_days_max,
        "strike_pct_window": policy.strike_pct_window,
        "atm_contract_count": policy.atm_contract_count,
        "otm_contract_count": policy.otm_contract_count,
        "min_trade_count": policy.min_trade_count,
        "large_trade_notional_threshold": policy.large_trade_notional_threshold,
        "include_historical_trades": policy.include_historical_trades,
        "include_historical_bars": policy.include_historical_bars,
        "include_latest_snapshot_quotes": policy.include_latest_snapshot_quotes,
        "include_latest_trades": policy.include_latest_trades,
        "include_latest_quotes": policy.include_latest_quotes,
        "historical_universe_reconstruction_enabled": (
            policy.historical_universe_reconstruction_enabled
        ),
        "historical_universe_max_candidates_per_bucket": (
            policy.historical_universe_max_candidates_per_bucket
        ),
        "historical_universe_strike_step": policy.historical_universe_strike_step,
        "bars_granularity": policy.bars_granularity,
        "lookback_hours": policy.lookback_hours,
    }


def _provider_instance_fingerprint_payload(
    provider: MarketBarsProvider,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "class": f"{provider.__class__.__module__}.{provider.__class__.__qualname__}",
    }
    return payload


def _provider_config_fingerprint_payload(
    *,
    config: KernelConfig,
    provider_name: str,
) -> dict[str, object]:
    payload: dict[str, object] = {"provider": provider_name}
    if config.validation is None:
        return payload
    market_data = config.validation.market_data
    if provider_name == market_data.provider:
        payload.update(
            {
                "base_url": market_data.base_url,
                "timeout_seconds": market_data.timeout_seconds,
            }
        )
    if provider_name == "local_archive" and market_data.local_archive_root is not None:
        payload["local_archive_root"] = market_data.local_archive_root.resolve(
            strict=False
        ).as_posix()
    return payload


def _adjustment_fingerprint_payload(
    *,
    config: KernelConfig,
    profile_config: MarketContextTargetProfileConfig,
    subscriptions: tuple[MarketContextBarSubscription, ...],
) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for subscription in subscriptions:
        policy = _market_context_subscription_adjustment_policy(
            profile_config=profile_config,
            market_symbol=subscription.symbol,
        )
        if policy is None:
            continue
        sidecar_root = _local_archive_adjustment_root(
            config=config,
            subscription=subscription,
        )
        payload.append(
            {
                "symbol": subscription.symbol,
                "provider": subscription.provider,
                "policy": policy,
                "source_hash": (
                    None
                    if sidecar_root is None
                    else read_adjustment_sidecar_hash(
                        root=sidecar_root,
                        market_symbol=subscription.symbol,
                        policy=policy,
                    )
                ),
            }
        )
    return payload


def _market_context_subscription_adjustment_policy(
    *,
    profile_config: MarketContextTargetProfileConfig,
    market_symbol: str,
) -> MarketDataAdjustmentPolicy | None:
    for subscription in profile_config.market_data_subscriptions:
        if subscription.symbol == market_symbol:
            return subscription.adjustment_policy
    return None


def _resolve_adjustment_source_path(
    *,
    config: KernelConfig,
    subscription: MarketContextBarSubscription,
    provider_metadata: dict[str, object] | None,
    direction: AdjustmentDirection,
) -> Path | None:
    if provider_metadata is not None:
        source_path = provider_metadata.get("source_path")
        if isinstance(source_path, str) and source_path.strip():
            bars_path = Path(source_path).resolve(strict=False)
            return bars_path.parent.parent.parent / "adjustments" / direction / (
                f"{normalize_archive_symbol(subscription.symbol)}.csv"
            )
    root = _local_archive_adjustment_root(config=config, subscription=subscription)
    if root is None:
        return None
    return root / "adjustments" / direction / f"{normalize_archive_symbol(subscription.symbol)}.csv"


def _local_archive_adjustment_root(
    *,
    config: KernelConfig,
    subscription: MarketContextBarSubscription,
) -> Path | None:
    if subscription.provider != "local_archive":
        return None
    if config.validation is None:
        return None
    root = config.validation.market_data.local_archive_root
    if root is None:
        return None
    return root.resolve(strict=False)


def _latest_close_at_or_before(
    bars: tuple[MarketDataBar, ...],
    as_of_at: datetime,
) -> float | None:
    visible = tuple(bar for bar in bars if bar.end_at <= as_of_at)
    if visible:
        return visible[-1].close_price
    return bars[0].close_price if bars else None


def _metadata_only_contract(row: OptionContractSnapshot) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        contract_symbol=row.contract_symbol,
        underlying_symbol=row.underlying_symbol,
        option_type=row.option_type,
        expiry_date=row.expiry_date,
        strike_price=row.strike_price,
        observed_at=None,
    )


def _as_options_provider(provider: MarketBarsProvider) -> MarketOptionsProvider | None:
    required = ("read_option_chain", "read_option_trades", "read_option_bars")
    if all(callable(getattr(provider, name, None)) for name in required):
        return provider  # type: ignore[return-value]
    return None


def _consume_provider_metadata(
    provider: MarketBarsProvider,
    source_metadata: dict[str, object],
    *,
    prefix: str,
) -> None:
    source_metadata.update(
        {f"{prefix}{key}": value for key, value in _pop_provider_metadata(provider).items()}
    )


def _pop_provider_metadata(provider: MarketBarsProvider) -> dict[str, object]:
    consume = getattr(provider, "pop_source_metadata", None)
    if callable(consume):
        return cast(dict[str, object], consume())
    return {}


def _failure(component: str, symbol: str, exc: Exception) -> dict[str, object]:
    return {
        "component": component,
        "symbol": symbol,
        "error_type": type(exc).__name__,
        "message": str(exc)[:240],
    }


def _provider_name(provider: MarketBarsProvider) -> str:
    return provider.__class__.__name__


__all__ = [
    "ReplayMarketPrefetchError",
    "ReplayMarketPrefetchEvent",
    "ReplayMarketPrefetchReceipt",
    "prefetch_replay_market_data",
]
