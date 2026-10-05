"""Deterministic live market-data warmup and canonical bar backfill."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from event_trader.config import (
    KernelConfig,
    LiveMarketDataConfig,
    MarketDataSubscriptionConfig,
)
from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    MarketMapping,
    MarketSession,
)
from event_trader.market.adjustments import (
    AdjustmentDirection,
    adjustment_direction_for_policy,
    normalize_archive_symbol,
)
from event_trader.market.context_builder import _request_start_at
from event_trader.market.contracts import (
    MarketContextProfile,
    OptionBarObservation,
    OptionContractSnapshot,
    OptionSelectionPolicy,
    OptionTradeObservation,
)
from event_trader.market.provider import (
    MarketBarsProvider,
    MarketOptionsProvider,
    build_default_market_bars_provider,
)
from event_trader.storage import WorkspaceLayout
from event_trader.validation.market_mapping import (
    ValidationMarketMappingError,
    resolve_market_mapping,
)

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


class FileBackedLiveMarketDataStore:
    """File-backed raw and canonical market data for live dryrun audit."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=False)
        self.raw_rest_bars_root = self.root / "raw" / "rest_bars"
        self.canonical_bars_root = self.root / "canonical" / "bars"
        self.adjustments_root = self.root / "adjustments"
        self.manifest_root = self.root / "manifest"

    def write_rest_bars(
        self,
        *,
        target_key: str,
        symbol: str,
        granularity: str,
        bars: tuple[MarketDataBar, ...],
        recorded_at: datetime,
    ) -> None:
        self._write_bars_file(
            self.raw_rest_bars_root,
            target_key=target_key,
            symbol=symbol,
            granularity=granularity,
            bars=bars,
            recorded_at=recorded_at,
        )

    def write_canonical_bars(
        self,
        *,
        mapping: MarketMapping,
        bars: tuple[MarketDataBar, ...],
        recorded_at: datetime,
    ) -> None:
        existing = self.read_canonical_bars_or_empty(
            mapping=mapping,
            start_at=datetime.min.replace(tzinfo=UTC),
            end_at=datetime.max.replace(tzinfo=UTC),
        )
        merged = _dedupe_bars((*existing, *bars))
        self._write_canonical_bars_file(
            mapping=mapping,
            bars=merged,
            recorded_at=recorded_at,
        )

    def read_canonical_bars(
        self,
        *,
        mapping: MarketMapping,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        bars = self.read_canonical_bars_or_empty(
            mapping=mapping,
            start_at=start_at,
            end_at=end_at,
        )
        if not bars:
            raise LiveMarketDataRuntimeError(
                "no live canonical bars for "
                f"{mapping.target_key}/{mapping.market_symbol} "
                f"{mapping.bar_granularity} {mapping.market_session} "
                f"{mapping.exchange or 'none'} {mapping.exchange_session_scope or 'none'}."
            )
        return MarketDataSeries(bars=bars)

    def read_canonical_bars_or_empty(
        self,
        *,
        mapping: MarketMapping,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[MarketDataBar, ...]:
        path = self._canonical_bars_path(mapping)
        rows = _read_jsonl(path)
        return tuple(
            bar
            for bar in (_bar_from_dict(row) for row in rows)
            if bar.end_at > start_at and bar.start_at < end_at
        )

    def latest_canonical_bar_end(
        self,
        *,
        mapping: MarketMapping,
    ) -> datetime | None:
        bars = self.read_canonical_bars_or_empty(
            mapping=mapping,
            start_at=datetime.min.replace(tzinfo=UTC),
            end_at=datetime.max.replace(tzinfo=UTC),
        )
        if not bars:
            return None
        return max(bar.end_at for bar in bars)

    def write_manifest(self, *, target_key: str, payload: dict[str, object]) -> None:
        target = validate_target_key(target_key, error_type=LiveMarketDataRuntimeError)
        path = self.manifest_root / f"{target}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def read_manifest_or_none(self, *, target_key: str) -> dict[str, object] | None:
        target = validate_target_key(target_key, error_type=LiveMarketDataRuntimeError)
        path = self.manifest_root / f"{target}.json"
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise LiveMarketDataRuntimeError(
                f"live market-data manifest must be a JSON object: {path.as_posix()}."
            )
        return payload

    def cached_timezone_for_symbol(
        self,
        *,
        target_key: str,
        symbol: str,
    ) -> str | None:
        payload = self.read_manifest_or_none(target_key=target_key)
        if payload is None:
            return None
        timezones = payload.get("bars_timezone_by_symbol")
        if isinstance(timezones, dict):
            value = timezones.get(symbol)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def update_symbol_manifest_metadata(
        self,
        *,
        target_key: str,
        symbol: str,
        timezone_name: str | None,
    ) -> None:
        payload = self.read_manifest_or_none(target_key=target_key) or {
            "target_key": validate_target_key(
                target_key,
                error_type=LiveMarketDataRuntimeError,
            )
        }
        timezones = payload.get("bars_timezone_by_symbol")
        timezone_by_symbol: dict[str, object]
        if isinstance(timezones, dict):
            timezone_by_symbol = dict(timezones)
        else:
            timezone_by_symbol = {}
        if isinstance(timezone_name, str) and timezone_name.strip():
            timezone_by_symbol[symbol] = timezone_name.strip()
        payload["bars_timezone_by_symbol"] = timezone_by_symbol
        self.write_manifest(target_key=target_key, payload=payload)

    def write_adjustment_sidecar(
        self,
        *,
        symbol: str,
        direction: AdjustmentDirection,
        content: str,
    ) -> None:
        path = self.adjustment_sidecar_path(symbol=symbol, direction=direction)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def has_adjustment_sidecar(
        self,
        *,
        symbol: str,
        direction: AdjustmentDirection,
    ) -> bool:
        return self.adjustment_sidecar_path(symbol=symbol, direction=direction).is_file()

    def adjustment_sidecar_path(
        self,
        *,
        symbol: str,
        direction: AdjustmentDirection,
    ) -> Path:
        archive_symbol = normalize_archive_symbol(symbol)
        return self.adjustments_root / direction / f"{archive_symbol}.csv"

    def _write_bars_file(
        self,
        root: Path,
        *,
        target_key: str,
        symbol: str,
        granularity: str,
        bars: tuple[MarketDataBar, ...],
        recorded_at: datetime,
    ) -> None:
        _ = _validate_utc_datetime(recorded_at, field_name="recorded_at")
        path = self._bars_path(
            root,
            target_key=target_key,
            symbol=symbol,
            granularity=granularity,
        )
        existing = tuple(_bar_from_dict(row) for row in _read_jsonl(path))
        rows = (_bar_to_dict(bar) for bar in _dedupe_bars((*existing, *bars)))
        _write_jsonl(path, rows)

    def _write_canonical_bars_file(
        self,
        *,
        mapping: MarketMapping,
        bars: tuple[MarketDataBar, ...],
        recorded_at: datetime,
    ) -> None:
        _ = _validate_utc_datetime(recorded_at, field_name="recorded_at")
        rows = (_bar_to_dict(bar) for bar in _dedupe_bars(bars))
        _write_jsonl(self._canonical_bars_path(mapping), rows)

    def _canonical_bars_path(self, mapping: MarketMapping) -> Path:
        if not isinstance(mapping, MarketMapping):
            raise LiveMarketDataRuntimeError("mapping must be a MarketMapping instance.")
        target = validate_target_key(
            mapping.target_key,
            error_type=LiveMarketDataRuntimeError,
        )
        file_name = (
            f"{_safe_name(mapping.market_symbol)}_"
            f"{_safe_name(mapping.bar_granularity)}.jsonl"
        )
        return (
            self.canonical_bars_root
            / target
            / _safe_name(mapping.market_session)
            / _safe_name(mapping.exchange or "none")
            / _safe_name(mapping.exchange_session_scope or "none")
            / file_name
        )

    def _bars_path(
        self,
        root: Path,
        *,
        target_key: str,
        symbol: str,
        granularity: str,
    ) -> Path:
        target = validate_target_key(target_key, error_type=LiveMarketDataRuntimeError)
        return root / target / f"{_safe_name(symbol)}_{_safe_name(granularity)}.jsonl"


class LiveMarketDataStoreProvider:
    """MarketBarsProvider backed by live canonical bars with optional REST fallback."""

    def __init__(
        self,
        *,
        store: FileBackedLiveMarketDataStore,
        fallback: MarketBarsProvider | None = None,
        local_archive_root: Path | None = None,
        subscription_index: dict[tuple[str, str], MarketDataSubscriptionConfig] | None = None,
    ) -> None:
        self._store = store
        self._fallback = fallback
        self._local_archive_root = (
            None if local_archive_root is None else local_archive_root.resolve(strict=False)
        )
        self._subscription_index = {} if subscription_index is None else dict(subscription_index)
        self._source_metadata: dict[str, object] = {}

    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        cached_bars = self._store.read_canonical_bars_or_empty(
            mapping=mapping,
            start_at=start_at,
            end_at=end_at,
        )
        missing_windows = _missing_cached_windows(
            cached_bars,
            mapping=mapping,
            start_at=start_at,
            end_at=end_at,
        )
        if not missing_windows:
            series = MarketDataSeries(bars=cached_bars)
            self._source_metadata.update(
                self._cached_series_metadata(
                    mapping=mapping,
                    cached_bar_count=len(series.bars),
                    remote_fallback=False,
                    backfill_window_count=0,
                )
            )
            return series

        if self._fallback is None:
            if cached_bars:
                self._source_metadata.update(
                    self._cached_series_metadata(
                        mapping=mapping,
                        cached_bar_count=len(cached_bars),
                        remote_fallback=False,
                        backfill_window_count=len(missing_windows),
                    )
                )
                self._source_metadata["backfill_skipped_reason"] = "fallback_unavailable"
                return MarketDataSeries(bars=cached_bars)
            raise ValueError(
                "missing live canonical market bars and no fallback provider is available."
            )

        backfilled_bar_count = 0
        try:
            for backfill_start, backfill_end in missing_windows:
                series, _provider_metadata = self._read_fallback_and_store(
                    mapping,
                    start_at=backfill_start,
                    end_at=backfill_end,
                    recorded_at=datetime.now(UTC),
                )
                backfilled_bar_count += len(series.bars)
        except Exception as exc:
            if not cached_bars:
                raise ValueError(
                    "missing live canonical market bars and fallback backfill failed."
                ) from exc
            self._source_metadata.update(
                self._cached_series_metadata(
                    mapping=mapping,
                    cached_bar_count=len(cached_bars),
                    remote_fallback=True,
                    backfill_window_count=len(missing_windows),
                )
            )
            self._source_metadata["backfill_error"] = _exception_summary(exc)
            return MarketDataSeries(bars=cached_bars)

        merged_bars = self._store.read_canonical_bars_or_empty(
            mapping=mapping,
            start_at=start_at,
            end_at=end_at,
        )
        series = MarketDataSeries(bars=merged_bars)
        self._source_metadata.update(
            self._cached_series_metadata(
                mapping=mapping,
                cached_bar_count=len(cached_bars),
                remote_fallback=True,
                backfill_window_count=len(missing_windows),
            )
        )
        self._source_metadata["backfilled_bar_count"] = backfilled_bar_count
        self._source_metadata["merged_bar_count"] = len(series.bars)
        return series

    def _read_fallback_and_store(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
        recorded_at: datetime,
    ) -> tuple[MarketDataSeries, dict[str, object]]:
        if self._fallback is None:
            raise LiveMarketDataRuntimeError("fallback provider unavailable.")
        series = self._fallback.read_series(mapping, start_at=start_at, end_at=end_at)
        provider_metadata = _pop_provider_source_metadata(self._fallback)
        self._store.write_rest_bars(
            target_key=mapping.target_key,
            symbol=mapping.market_symbol,
            granularity=mapping.bar_granularity,
            bars=series.bars,
            recorded_at=recorded_at,
        )
        self._store.write_canonical_bars(
            mapping=mapping,
            bars=series.bars,
            recorded_at=recorded_at,
        )
        self._persist_cached_series_metadata(
            mapping=mapping,
            provider_metadata=provider_metadata,
        )
        self._source_metadata.update(provider_metadata)
        return series, provider_metadata

    def pop_source_metadata(self) -> dict[str, object]:
        metadata = dict(self._source_metadata)
        consume = getattr(self._fallback, "pop_source_metadata", None)
        if callable(consume):
            fallback_metadata = cast(dict[str, object], consume())
            metadata.update(fallback_metadata)
        self._source_metadata.clear()
        return metadata

    def close(self) -> None:
        close = getattr(self._fallback, "close", None)
        if callable(close):
            close()

    def read_option_chain(
        self,
        *,
        underlying_symbol: str,
        as_of_at: datetime,
        underlying_price: float,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        return self._options_provider().read_option_chain(
            underlying_symbol=underlying_symbol,
            as_of_at=as_of_at,
            underlying_price=underlying_price,
            policy=policy,
        )

    def read_option_chain_metadata(
        self,
        *,
        underlying_symbol: str,
        as_of_at: datetime,
        underlying_price: float,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        return self._options_provider().read_option_chain_metadata(
            underlying_symbol=underlying_symbol,
            as_of_at=as_of_at,
            underlying_price=underlying_price,
            policy=policy,
        )

    def read_latest_option_trades(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionTradeObservation, ...]:
        return self._options_provider().read_latest_option_trades(
            contracts=contracts,
            as_of_at=as_of_at,
            policy=policy,
        )

    def read_option_trades(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        start_at: datetime,
        end_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionTradeObservation, ...]:
        return self._options_provider().read_option_trades(
            contracts=contracts,
            start_at=start_at,
            end_at=end_at,
            policy=policy,
        )

    def read_latest_option_quotes(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        return self._options_provider().read_latest_option_quotes(
            contracts=contracts,
            as_of_at=as_of_at,
            policy=policy,
        )

    def read_option_bars(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        start_at: datetime,
        end_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionBarObservation, ...]:
        return self._options_provider().read_option_bars(
            contracts=contracts,
            start_at=start_at,
            end_at=end_at,
            policy=policy,
        )

    def _options_provider(self) -> MarketOptionsProvider:
        if self._fallback is None:
            raise LiveMarketDataRuntimeError("options provider unavailable.")
        required = (
            "read_option_chain",
            "read_option_chain_metadata",
            "read_latest_option_trades",
            "read_option_trades",
            "read_latest_option_quotes",
            "read_option_bars",
        )
        if not all(callable(getattr(self._fallback, name, None)) for name in required):
            raise LiveMarketDataRuntimeError("fallback provider does not support options.")
        return cast(MarketOptionsProvider, self._fallback)

    def _cached_series_metadata(
        self,
        *,
        mapping: MarketMapping,
        cached_bar_count: int,
        remote_fallback: bool,
        backfill_window_count: int,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "provider": "live_market_data_store",
            "remote_fallback": remote_fallback,
            "cached_symbol": mapping.market_symbol,
            "cached_bar_count": cached_bar_count,
            "cached_store_root": self._store.root.as_posix(),
            "cached_store_kind": "live_store",
            "backfill_window_count": backfill_window_count,
        }
        timezone_name = self._store.cached_timezone_for_symbol(
            target_key=mapping.target_key,
            symbol=mapping.market_symbol,
        )
        if timezone_name is not None:
            metadata["cached_timezone"] = timezone_name
        return metadata

    def _persist_cached_series_metadata(
        self,
        *,
        mapping: MarketMapping,
        provider_metadata: dict[str, object],
    ) -> None:
        timezone_name = _provider_timezone_name(provider_metadata)
        subscription = self._subscription_index.get((mapping.target_key, mapping.market_symbol))
        if subscription is not None and subscription.adjustment_policy is not None:
            if timezone_name is None:
                raise LiveMarketDataRuntimeError(
                    "live market-data provider metadata is missing timezone for adjusted "
                    f"symbol {mapping.market_symbol!r}."
                )
            _freeze_live_adjustment_sidecar(
                store=self._store,
                subscription=subscription,
                local_archive_root=self._local_archive_root,
                provider_metadata=provider_metadata,
            )
        self._store.update_symbol_manifest_metadata(
            target_key=mapping.target_key,
            symbol=mapping.market_symbol,
            timezone_name=timezone_name,
        )


def build_live_market_data_store(layout: WorkspaceLayout) -> FileBackedLiveMarketDataStore:
    if not isinstance(layout, WorkspaceLayout):
        raise LiveMarketDataRuntimeError("layout must be a WorkspaceLayout instance.")
    return FileBackedLiveMarketDataStore(layout.runtime_root / "live_market_data")


def prepare_live_market_data(
    *,
    config: KernelConfig,
    layout: WorkspaceLayout,
    now: datetime | None = None,
    provider: MarketBarsProvider | None = None,
    emit: LiveMarketDataEmitter | None = None,
) -> tuple[LiveMarketDataStoreProvider | None, LiveMarketDataWarmupReceipt]:
    """Warm live canonical market bars and return a store-backed provider."""
    live_market_data = _live_market_data_config(config)
    if live_market_data is None:
        return None, LiveMarketDataWarmupReceipt(enabled=False)
    store = build_live_market_data_store(layout)
    fallback = provider or build_default_market_bars_provider(config)
    subscription_index = _live_subscription_index(live_market_data)
    local_archive_root = _live_local_archive_root(config)
    store_provider = LiveMarketDataStoreProvider(
        store=store,
        fallback=fallback,
        local_archive_root=local_archive_root,
        subscription_index=subscription_index,
    )
    if not live_market_data.warmup.enabled:
        return store_provider, LiveMarketDataWarmupReceipt(enabled=True)
    if fallback is None:
        raise LiveMarketDataRuntimeError(
            "live market-data warmup requires validation.market_data provider."
        )
    warmup_now = _validate_utc_datetime(now or datetime.now(UTC), field_name="now")
    bar_count = 0
    symbol_count = 0
    for target in live_market_data.targets:
        target_bar_count = 0
        bars_timezone_by_symbol: dict[str, str] = {}
        _assert_live_subscription_providers_supported(
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
            series = fallback.read_series(mapping, start_at=start_at, end_at=warmup_now)
            provider_metadata = _pop_provider_source_metadata(fallback)
            store.write_rest_bars(
                target_key=target.target_key,
                symbol=subscription.symbol,
                granularity=subscription.bar_granularity,
                bars=series.bars,
                recorded_at=warmup_now,
            )
            store.write_canonical_bars(
                mapping=mapping,
                bars=series.bars,
                recorded_at=warmup_now,
            )
            timezone_name = _provider_timezone_name(provider_metadata)
            if timezone_name is not None:
                bars_timezone_by_symbol[subscription.symbol] = timezone_name
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
            bar_count += len(series.bars)
            target_bar_count += len(series.bars)
        store.write_manifest(
            target_key=target.target_key,
            payload={
                "target_key": target.target_key,
                "canonical_bar_granularity": live_market_data.canonical_bar_granularity,
                "warmup_recorded_at": warmup_now.isoformat(),
                "warmup_min_lookback_days": live_market_data.warmup.min_lookback_days,
                "primary_subscription": _subscription_payload(
                    _primary_subscription(target.subscriptions)
                ),
                "subscriptions": [
                    _subscription_payload(subscription)
                    for subscription in target.subscriptions
                ],
                "bars_timezone_by_symbol": bars_timezone_by_symbol,
                "warmup_bar_count": target_bar_count,
            },
        )
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


def _live_subscription_index(
    live_market_data: LiveMarketDataConfig,
) -> dict[tuple[str, str], MarketDataSubscriptionConfig]:
    index: dict[tuple[str, str], MarketDataSubscriptionConfig] = {}
    for target in live_market_data.targets:
        for subscription in target.subscriptions:
            index[(target.target_key, subscription.symbol)] = subscription
    return index


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
    if purpose != "primary_tradable" and symbol != profile.tradable_proxy_symbol.upper():
        return min_start
    profile_start = _request_start_at(
        profile=profile,
        as_of_at=end_at,
        market_session=mapping.market_session,
    )
    return min(min_start, profile_start)


def _mapping_for_live_subscription(
    *,
    target_key: str,
    subscription: MarketDataSubscriptionConfig,
) -> MarketMapping:
    return MarketMapping(
        target_key=target_key,
        market_symbol=subscription.symbol,
        market_session=cast(MarketSession, subscription.market_session),
        exchange=subscription.exchange,
        bar_granularity=subscription.bar_granularity,
        exchange_session_scope=subscription.exchange_session_scope,
    )


def _primary_subscription(
    subscriptions: tuple[MarketDataSubscriptionConfig, ...],
) -> MarketDataSubscriptionConfig:
    for subscription in subscriptions:
        if subscription.purpose == "primary_tradable":
            return subscription
    raise LiveMarketDataRuntimeError(
        "live market-data target requires one primary_tradable subscription."
    )


def _subscription_payload(
    subscription: MarketDataSubscriptionConfig,
) -> dict[str, str]:
    return {
        "symbol": subscription.symbol,
        "provider": subscription.provider,
        "purpose": subscription.purpose,
        "market_session": subscription.market_session,
        "exchange": subscription.exchange,
        "exchange_session_scope": subscription.exchange_session_scope,
        "bar_granularity": subscription.bar_granularity,
    }


def _assert_live_subscription_providers_supported(
    *,
    config: KernelConfig,
    subscriptions: tuple[MarketDataSubscriptionConfig, ...],
    provider_override: MarketBarsProvider | None,
    target_key: str,
) -> None:
    if provider_override is not None:
        return
    if config.validation is None:
        return
    configured_provider = config.validation.market_data.provider
    unsupported = sorted(
        {
            subscription.provider
            for subscription in subscriptions
            if subscription.provider != configured_provider
        }
    )
    if not unsupported:
        return
    joined = ", ".join(unsupported)
    raise LiveMarketDataRuntimeError(
        "live market-data warmup does not have provider implementations for "
        f"target {target_key!r}: configured provider={configured_provider!r}, "
        f"subscription providers={joined}"
    )


def _bar_to_dict(bar: MarketDataBar) -> dict[str, object]:
    return {
        "start_at": bar.start_at.isoformat(),
        "end_at": bar.end_at.isoformat(),
        "open": bar.open_price,
        "high": bar.high_price,
        "low": bar.low_price,
        "close": bar.close_price,
        "volume": bar.volume,
        "vwap": bar.vwap,
    }


def _bar_from_dict(payload: dict[str, object]) -> MarketDataBar:
    return MarketDataBar(
        start_at=_parse_timestamp(payload["start_at"]),
        end_at=_parse_timestamp(payload["end_at"]),
        open_price=_parse_float_field(payload, "open"),
        high_price=_parse_float_field(payload, "high"),
        low_price=_parse_float_field(payload, "low"),
        close_price=_parse_float_field(payload, "close"),
        volume=_parse_float_field(payload, "volume"),
        vwap=(
            None
            if payload.get("vwap") is None
            else _parse_float_field(payload, "vwap")
        ),
    )


def _dedupe_bars(bars: tuple[MarketDataBar, ...]) -> tuple[MarketDataBar, ...]:
    by_key = {(bar.start_at, bar.end_at): bar for bar in bars}
    return tuple(bar for _, bar in sorted(by_key.items(), key=lambda item: item[0]))


def _missing_cached_windows(
    bars: tuple[MarketDataBar, ...],
    *,
    mapping: MarketMapping,
    start_at: datetime,
    end_at: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    if start_at >= end_at:
        return ()
    if not bars:
        return ((start_at, end_at),)
    delta = _parse_granularity_delta(mapping.bar_granularity)
    ordered = _dedupe_bars(bars)
    tolerance = delta
    windows: list[tuple[datetime, datetime]] = []
    first = ordered[0]
    if mapping.market_session == "continuous":
        if first.start_at > start_at + tolerance:
            windows.append((start_at, first.start_at))
        previous = first
        for current in ordered[1:]:
            if current.start_at > previous.end_at + tolerance:
                windows.append((previous.end_at, current.start_at))
            previous = current
    last = ordered[-1]
    if last.end_at <= end_at - tolerance:
        windows.append((last.end_at, end_at))
    return tuple((left, right) for left, right in windows if left < right)


def _write_jsonl(path: Path, rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> tuple[dict[str, object], ...]:
    if not path.exists():
        return ()
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise LiveMarketDataRuntimeError(f"JSONL row must be an object: {path}")
        rows.append(payload)
    return tuple(rows)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise LiveMarketDataRuntimeError("timestamp must be a string.")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise LiveMarketDataRuntimeError(f"invalid timestamp: {value!r}") from exc
    return _validate_utc_datetime(parsed, field_name="timestamp")


def _parse_float_field(payload: dict[str, object], field_name: str) -> float:
    value = payload.get(field_name)
    if not isinstance(value, str | bytes | int | float):
        raise LiveMarketDataRuntimeError(f"{field_name} must be numeric.")
    return float(value)


def _validate_utc_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise LiveMarketDataRuntimeError(f"{field_name} must be a datetime.")
    return validate_timestamp(
        value,
        field_name=field_name,
        error_type=LiveMarketDataRuntimeError,
    ).astimezone(UTC)


def _parse_granularity_delta(value: str) -> timedelta:
    raw = value.strip().lower()
    if raw.endswith("min"):
        return timedelta(minutes=int(raw[:-3]))
    if raw.endswith("m"):
        return timedelta(minutes=int(raw[:-1]))
    if raw.endswith("h"):
        return timedelta(hours=int(raw[:-1]))
    if raw.endswith("d"):
        return timedelta(days=int(raw[:-1]))
    raise LiveMarketDataRuntimeError(f"unsupported granularity: {value!r}")


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)


def _exception_summary(error: Exception) -> str:
    return str(error).replace("\n", " ")[:240]


def _pop_provider_source_metadata(provider: MarketBarsProvider) -> dict[str, object]:
    consume = getattr(provider, "pop_source_metadata", None)
    if callable(consume):
        return cast(dict[str, object], consume())
    return {}


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
    store: FileBackedLiveMarketDataStore,
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
    stored_path = store.adjustment_sidecar_path(
        symbol=subscription.symbol,
        direction=direction,
    )
    if stored_path.is_file() and stored_path.read_text(encoding="utf-8") == sidecar_content:
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
    "FileBackedLiveMarketDataStore",
    "LiveMarketDataRuntimeError",
    "LiveMarketDataStoreProvider",
    "LiveMarketDataWarmupReceipt",
    "build_live_market_data_store",
    "prepare_live_market_data",
]
