"""Single-process forward-only live runtime with scheduled source acquisition."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from event_trader.composition import ComposedKernel
from event_trader.config import (
    LiveMarketNewsRestConfig,
    LiveMarketNewsWebSocketSessionConfig,
    LiveWebSearchConfig,
)
from event_trader.contracts._validators import validate_timestamp
from event_trader.feeds.search_implementation import (
    SearchImplementationConfig,
    build_search_runner,
)
from event_trader.live_source_checkpoint import (
    advance_live_source_checkpoint,
    live_source_checkpoint_path,
)
from event_trader.market_news_runtime import (
    LiveMarketNewsBatchReceipt,
    MarketNewsBatchDriver,
)
from event_trader.market_news_schedule import (
    MarketNewsRestCadenceProfile,
    MarketNewsWebSocketSessionPolicy,
)
from event_trader.market_news_ws_runtime import (
    MarketNewsWebSocketDriver,
    MarketNewsWebSocketRoute,
)
from event_trader.market_news_ws_status import MarketNewsWebSocketStatusWriter
from event_trader.runtime.bootstrap import LiveRuntimeHost
from event_trader.storage import WorkspaceLayout
from event_trader.web_search_runtime import LiveWebSearchBatchReceipt, WebSearchBatchDriver
from event_trader.web_search_schedule import (
    LiveWebSearchCadenceProfile,
    plan_live_web_search_due_windows,
)

type LiveRuntimeEmitter = Callable[[str], None]
type Clock = Callable[[], datetime]
type Sleep = Callable[[float], None]

_HOST_JOIN_POLL_SECONDS = 0.1
_MAX_SOURCE_SLEEP_SECONDS = 60.0
_LIVE_WEB_SEARCH_WALL_CLOCK_TIMEOUT_SECONDS = 1200


class LiveRuntimeError(RuntimeError):
    """Raised when the combined live runtime cannot run cleanly."""


@dataclass(frozen=True, slots=True)
class LiveRuntimeReceipt:
    """Observable result from a bounded live runtime run."""

    source_batch_count: int
    heartbeat_count: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_live_web_search_drivers(
    composed: ComposedKernel,
    *,
    startup_at: datetime | None = None,
) -> tuple[WebSearchBatchDriver, ...]:
    """Build configured live web-search source drivers, if enabled."""
    raw_source_publisher = composed.require_raw_source_publisher()
    live_config = composed.config.live
    if live_config is None:
        raise LiveRuntimeError("live runtime requires [live] config.")
    if "web_search" not in live_config.enabled_channels:
        return ()
    if live_config.web_search is None:
        raise LiveRuntimeError(
            "live.enabled_channels includes 'web_search' but [live.web_search] is missing."
        )
    if composed.workspace.layout is None:
        raise LiveRuntimeError("live web-search driver requires a workspace layout.")

    web_search_config = live_config.web_search
    current_time = _resolve_live_startup_anchor(startup_at)
    cadence_profile = _build_web_search_cadence_profile(web_search_config)
    run_search_agent = build_search_runner(
        config=SearchImplementationConfig.from_live_config(
            web_search_config,
            workspace_root=composed.workspace.root,
            wall_clock_timeout_seconds=_LIVE_WEB_SEARCH_WALL_CLOCK_TIMEOUT_SECONDS,
        ),
    )
    drivers: list[WebSearchBatchDriver] = []
    for target_config in web_search_config.targets:
        # Live startup never replays old source windows from durable checkpoints.
        # Historical recovery is owned exclusively by live_catchup.
        last_successful_batch_end = current_time
        next_due_at = plan_live_web_search_due_windows(
            cadence_profile,
            cursor_at=last_successful_batch_end,
            now=current_time,
            max_windows=1,
        ).next_due_at
        drivers.append(
            WebSearchBatchDriver(
                target_key=target_config.target_key,
                search_intent=target_config.search_intent,
                cadence_profile=cadence_profile,
                last_successful_batch_end=last_successful_batch_end,
                next_due_at=next_due_at,
                run_search_agent=run_search_agent,
                raw_source_publisher=raw_source_publisher,
                historical_layout=composed.workspace.layout,
            )
        )
    return tuple(sorted(drivers, key=_driver_sort_key))


def build_live_market_news_drivers(
    composed: ComposedKernel,
    *,
    startup_at: datetime | None = None,
) -> tuple[MarketNewsBatchDriver, ...]:
    """Build configured live market-news source drivers, if enabled."""
    live_config = composed.config.live
    if live_config is None:
        raise LiveRuntimeError("live runtime requires [live] config.")
    if "market_news" not in live_config.enabled_channels:
        return ()
    if live_config.market_news is None:
        raise LiveRuntimeError(
            "live.enabled_channels includes 'market_news' but [live.market_news] is missing."
        )
    if composed.workspace.layout is None:
        raise LiveRuntimeError("live market-news driver requires a workspace layout.")

    market_news_config = live_config.market_news
    rest_config = market_news_config.rest
    if rest_config is None or not rest_config.enabled:
        return ()
    raw_source_publisher = composed.require_raw_source_publisher()
    current_time = _resolve_live_startup_anchor(startup_at)
    cadence_profile = _build_market_news_cadence_profile(rest_config)
    drivers: list[MarketNewsBatchDriver] = []
    for target_config in market_news_config.targets:
        # Keep durable checkpoints for last-success bookkeeping only; they no
        # longer seed live startup scheduling.
        last_successful_batch_end = current_time
        drivers.append(
            MarketNewsBatchDriver(
                target_key=target_config.target_key,
                symbols=target_config.symbols,
                labels=target_config.labels,
                base_url=rest_config.base_url,
                api_key=rest_config.api_key,
                cadence_profile=cadence_profile,
                last_successful_batch_end=last_successful_batch_end,
                next_due_at=cadence_profile.next_due_after(last_successful_batch_end),
                limit=rest_config.limit,
                include_content=rest_config.include_content,
                exclude_contentless=rest_config.exclude_contentless,
                soft_fail=rest_config.soft_fail,
                raw_source_publisher=raw_source_publisher,
                historical_layout=composed.workspace.layout,
                emit=None,
            )
        )
    return tuple(sorted(drivers, key=_driver_sort_key))


def build_live_market_news_websocket_drivers(
    composed: ComposedKernel,
    *,
    emit: LiveRuntimeEmitter | None = None,
) -> tuple[MarketNewsWebSocketDriver, ...]:
    """Build optional realtime market-news WebSocket drivers."""
    raw_source_publisher = composed.require_raw_source_publisher()
    live_config = composed.config.live
    if live_config is None or "market_news" not in live_config.enabled_channels:
        return ()
    if live_config.market_news is None:
        return ()
    if composed.workspace.layout is None:
        raise LiveRuntimeError("live market-news websocket requires a workspace layout.")
    websocket_config = live_config.market_news.websocket
    if websocket_config is None or not websocket_config.enabled:
        return ()
    route_target_keys = tuple(target.target_key for target in live_config.market_news.targets)
    return (
        MarketNewsWebSocketDriver(
            provider=websocket_config.provider,
            url=websocket_config.url,
            token=websocket_config.token,
            tickers=websocket_config.tickers,
            routes=tuple(
                MarketNewsWebSocketRoute(
                    target_key=target.target_key,
                    symbols=target.symbols,
                    labels=target.labels,
                )
                for target in live_config.market_news.targets
            ),
            soft_fail=websocket_config.soft_fail,
            reconnect_seconds_min=websocket_config.reconnect_seconds_min,
            reconnect_seconds_max=websocket_config.reconnect_seconds_max,
            heartbeat_seconds=websocket_config.heartbeat_seconds,
            session_policy=_build_market_news_websocket_session_policy(
                websocket_config.session
            ),
            raw_source_publisher=raw_source_publisher,
            historical_layout=composed.workspace.layout,
            status_writer=MarketNewsWebSocketStatusWriter(
                layout=composed.workspace.layout,
                provider=websocket_config.provider,
                tickers=websocket_config.tickers,
                route_target_keys=route_target_keys,
            ),
            emit=emit,
        ),
    )


def run_live_runtime_loop(
    *,
    live_host: LiveRuntimeHost,
    web_search_drivers: tuple[WebSearchBatchDriver, ...] = (),
    market_news_drivers: tuple[MarketNewsBatchDriver, ...] = (),
    market_news_websocket_drivers: tuple[MarketNewsWebSocketDriver, ...] = (),
    checkpoint_bookkeeping_layout: WorkspaceLayout | None = None,
    emit: LiveRuntimeEmitter,
    now: Clock = _utc_now,
    sleep: Sleep = time.sleep,
    max_source_batches: int | None = None,
) -> LiveRuntimeReceipt:
    """Run the live host and configured source drivers in one process."""
    if not isinstance(live_host, LiveRuntimeHost):
        raise LiveRuntimeError("live_host must be a LiveRuntimeHost instance.")
    if not isinstance(web_search_drivers, tuple):
        raise LiveRuntimeError(
            "web_search_drivers must be a tuple of WebSearchBatchDriver instances."
        )
    if not isinstance(market_news_drivers, tuple):
        raise LiveRuntimeError(
            "market_news_drivers must be a tuple of MarketNewsBatchDriver instances."
        )
    if not isinstance(market_news_websocket_drivers, tuple):
        raise LiveRuntimeError(
            "market_news_websocket_drivers must be a tuple of "
            "MarketNewsWebSocketDriver instances."
        )
    for index, web_search_driver in enumerate(web_search_drivers):
        if not isinstance(web_search_driver, WebSearchBatchDriver):
            raise LiveRuntimeError(
                "web_search_drivers must contain only WebSearchBatchDriver "
                f"instances; item {index} is invalid."
            )
    for index, market_news_driver in enumerate(market_news_drivers):
        if not isinstance(market_news_driver, MarketNewsBatchDriver):
            raise LiveRuntimeError(
                "market_news_drivers must contain only MarketNewsBatchDriver "
                f"instances; item {index} is invalid."
            )
    for index, market_news_websocket_driver in enumerate(market_news_websocket_drivers):
        if not isinstance(market_news_websocket_driver, MarketNewsWebSocketDriver):
            raise LiveRuntimeError(
                "market_news_websocket_drivers must contain only "
                f"MarketNewsWebSocketDriver instances; item {index} is invalid."
            )
    _validate_unique_web_search_drivers(web_search_drivers)
    _validate_unique_market_news_drivers(market_news_drivers)
    if checkpoint_bookkeeping_layout is not None and not isinstance(
        checkpoint_bookkeeping_layout,
        WorkspaceLayout,
    ):
        raise LiveRuntimeError(
            "checkpoint_bookkeeping_layout must be a WorkspaceLayout instance when "
            "provided."
        )
    if not callable(emit):
        raise LiveRuntimeError("emit must be callable.")
    if not callable(now):
        raise LiveRuntimeError("now must be callable.")
    if not callable(sleep):
        raise LiveRuntimeError("sleep must be callable.")
    if max_source_batches is not None:
        if isinstance(max_source_batches, bool) or not isinstance(max_source_batches, int):
            raise LiveRuntimeError("max_source_batches must be a positive integer.")
        if max_source_batches <= 0:
            raise LiveRuntimeError("max_source_batches must be greater than zero.")
        if not web_search_drivers and not market_news_drivers:
            raise LiveRuntimeError(
                "max_source_batches requires at least one source driver."
            )
    for market_news_driver in market_news_drivers:
        if market_news_driver.emit is None:
            market_news_driver.emit = emit

    source_batch_count = 0
    live_host.start()
    emit("live runtime: host started")
    try:
        while max_source_batches is None or source_batch_count < max_source_batches:
            completed_heartbeats = live_host.join(
                timeout_seconds=_HOST_JOIN_POLL_SECONDS,
            )
            if completed_heartbeats is not None:
                _stop_market_news_websocket_drivers(market_news_websocket_drivers)
                emit(f"live runtime: host exited heartbeats={completed_heartbeats}")
                return LiveRuntimeReceipt(
                    source_batch_count=source_batch_count,
                    heartbeat_count=completed_heartbeats,
                )

            current_time = _validate_utc_now(now())
            _sync_market_news_websocket_drivers(
                market_news_websocket_drivers,
                current_time=current_time,
                emit=emit,
            )
            time_observation = live_host.advance_runtime_time(
                current_time=current_time,
            )
            ceau_emitted_count = int(
                getattr(time_observation, "ceau_emitted_count", 0) or 0
            )
            pm_review_request_count = len(
                getattr(time_observation, "pm_review_requests", ()) or ()
            )
            if ceau_emitted_count or pm_review_request_count:
                emit(
                    "live runtime time advance: "
                    f"ceau_emitted={ceau_emitted_count} "
                    f"pm_review_requests={pm_review_request_count}"
                )
            for web_search_driver in _due_drivers(
                web_search_drivers,
                current_time,
            ):
                web_search_receipt = web_search_driver.run_due(current_time)
                if web_search_receipt is not None:
                    source_batch_count += 1
                    if checkpoint_bookkeeping_layout is not None:
                        checkpoint_path = _write_web_search_checkpoint(
                            layout=checkpoint_bookkeeping_layout,
                            driver=web_search_driver,
                        )
                        emit(f"live source checkpoint: {checkpoint_path}")
                    _emit_source_batch_receipt(
                        emit=emit,
                        channel="web_search",
                        target_key=web_search_driver.target_key,
                        receipt=web_search_receipt,
                        source_batch_count=source_batch_count,
                    )
                if (
                    max_source_batches is not None
                    and source_batch_count >= max_source_batches
                ):
                    break

            if max_source_batches is not None and source_batch_count >= max_source_batches:
                continue

            for market_news_driver in _due_drivers(
                market_news_drivers,
                current_time,
            ):
                market_news_receipt = market_news_driver.run_due(current_time)
                if market_news_receipt is not None:
                    source_batch_count += 1
                    if checkpoint_bookkeeping_layout is not None:
                        checkpoint_path = _write_market_news_checkpoint(
                            layout=checkpoint_bookkeeping_layout,
                            driver=market_news_driver,
                        )
                        emit(f"live source checkpoint: {checkpoint_path}")
                    _emit_source_batch_receipt(
                        emit=emit,
                        channel="market_news",
                        target_key=market_news_driver.target_key,
                        receipt=market_news_receipt,
                        source_batch_count=source_batch_count,
                    )
                if (
                    max_source_batches is not None
                    and source_batch_count >= max_source_batches
                ):
                    break

            if max_source_batches is None or source_batch_count < max_source_batches:
                sleep(
                    _source_loop_sleep_seconds(
                        web_search_drivers,
                        market_news_drivers,
                        current_time,
                    )
                )
    except BaseException:
        for market_news_websocket_driver in market_news_websocket_drivers:
            market_news_websocket_driver.stop()
        live_host.stop(reason="live source driver failure")
        live_host.join(timeout_seconds=_HOST_JOIN_POLL_SECONDS)
        for market_news_websocket_driver in market_news_websocket_drivers:
            market_news_websocket_driver.join(timeout_seconds=_HOST_JOIN_POLL_SECONDS)
        raise

    for market_news_websocket_driver in market_news_websocket_drivers:
        market_news_websocket_driver.stop()
    live_host.stop(reason="live runtime source batch limit reached")
    heartbeat_count = live_host.join(timeout_seconds=_HOST_JOIN_POLL_SECONDS)
    for market_news_websocket_driver in market_news_websocket_drivers:
        market_news_websocket_driver.join(timeout_seconds=_HOST_JOIN_POLL_SECONDS)
    if heartbeat_count is None:
        raise LiveRuntimeError("live host did not stop after source batch limit.")
    return LiveRuntimeReceipt(
        source_batch_count=source_batch_count,
        heartbeat_count=heartbeat_count,
    )


def _build_web_search_cadence_profile(
    web_search_config: LiveWebSearchConfig,
) -> LiveWebSearchCadenceProfile:
    profile_config = web_search_config.cadence_profile_config
    return LiveWebSearchCadenceProfile(
        name=web_search_config.cadence_profile,
        timezone=profile_config.timezone,
        trading_day_local_times=profile_config.trading_day_local_times,
        closed_day_local_times=profile_config.closed_day_local_times,
    )


def _build_market_news_cadence_profile(
    rest_config: LiveMarketNewsRestConfig,
) -> MarketNewsRestCadenceProfile:
    profile_config = rest_config.cadence_profile_config
    return MarketNewsRestCadenceProfile(
        name=rest_config.cadence_profile,
        timezone=profile_config.timezone,
        calendar=profile_config.calendar,
        rth_local_times=profile_config.rth_local_times,
        trading_day_off_rth_local_times=(
            profile_config.trading_day_off_rth_local_times
        ),
        closed_day_local_times=profile_config.closed_day_local_times,
    )


def _build_market_news_websocket_session_policy(
    session_config: LiveMarketNewsWebSocketSessionConfig,
) -> MarketNewsWebSocketSessionPolicy:
    return MarketNewsWebSocketSessionPolicy(
        mode=session_config.mode,
        timezone=session_config.timezone,
        calendar=session_config.calendar,
        open_buffer_minutes=session_config.open_buffer_minutes,
        close_buffer_minutes=session_config.close_buffer_minutes,
    )


def _driver_sort_key(
    driver: WebSearchBatchDriver | MarketNewsBatchDriver,
) -> tuple[datetime, str]:
    return (driver.next_due_at, driver.target_key)


def _validate_utc_now(current_time: object) -> datetime:
    if not isinstance(current_time, datetime):
        raise LiveRuntimeError("now must return a datetime.")
    return validate_timestamp(
        current_time,
        field_name="now",
        error_type=LiveRuntimeError,
    ).astimezone(UTC)


def _resolve_live_startup_anchor(startup_at: datetime | None) -> datetime:
    if startup_at is None:
        return _utc_now()
    return _validate_utc_now(startup_at)


def _due_drivers[SourceDriverT: WebSearchBatchDriver | MarketNewsBatchDriver](
    drivers: tuple[SourceDriverT, ...],
    current_time: datetime,
) -> tuple[SourceDriverT, ...]:
    return tuple(
        sorted(
            (
                driver
                for driver in drivers
                if current_time >= driver.next_due_at
            ),
            key=_driver_sort_key,
        )
    )


def _validate_unique_web_search_drivers(
    drivers: tuple[WebSearchBatchDriver, ...],
) -> None:
    seen_target_keys: dict[str, int] = {}
    for index, driver in enumerate(drivers):
        first_index = seen_target_keys.get(driver.target_key)
        if first_index is not None:
            raise LiveRuntimeError(
                "web_search_drivers must not contain duplicate target_key "
                f"{driver.target_key!r}; item {index} duplicates item {first_index}."
            )
        seen_target_keys[driver.target_key] = index


def _validate_unique_market_news_drivers(
    drivers: tuple[MarketNewsBatchDriver, ...],
) -> None:
    seen_target_keys: dict[str, int] = {}
    for index, driver in enumerate(drivers):
        first_index = seen_target_keys.get(driver.target_key)
        if first_index is not None:
            raise LiveRuntimeError(
                "market_news_drivers must not contain duplicate target_key "
                f"{driver.target_key!r}; item {index} duplicates item {first_index}."
            )
        seen_target_keys[driver.target_key] = index


def _stop_market_news_websocket_drivers(
    drivers: tuple[MarketNewsWebSocketDriver, ...],
) -> None:
    for driver in drivers:
        driver.stop()
    for driver in drivers:
        driver.join(timeout_seconds=_HOST_JOIN_POLL_SECONDS)


def _sync_market_news_websocket_drivers(
    drivers: tuple[MarketNewsWebSocketDriver, ...],
    *,
    current_time: datetime,
    emit: LiveRuntimeEmitter,
) -> None:
    for driver in drivers:
        should_run = driver.session_policy.is_active(current_time)
        driver.record_session_state(
            current_time=current_time,
            session_active=should_run,
        )
        if should_run:
            if not driver.is_running and not driver.start_blocked:
                driver.start()
                if driver.is_running:
                    emit(
                        "live market_news websocket started: "
                        f"provider={driver.provider} session=active"
                    )
            continue
        if driver.is_running:
            driver.stop()
            driver.join(timeout_seconds=_HOST_JOIN_POLL_SECONDS)
            emit(
                "live market_news websocket stopped: "
                f"provider={driver.provider} session=inactive"
            )


def _write_web_search_checkpoint(
    *,
    layout: WorkspaceLayout,
    driver: WebSearchBatchDriver,
) -> Path:
    path = advance_live_source_checkpoint(
        layout,
        target_key=driver.target_key,
        channel="web_search",
        last_successful_batch_end=driver.last_successful_batch_end,
    )
    expected_path = live_source_checkpoint_path(
        layout,
        target_key=driver.target_key,
        channel="web_search",
    )
    if path != expected_path:
        raise LiveRuntimeError("live source checkpoint writer returned an unexpected path.")
    return path


def _write_market_news_checkpoint(
    *,
    layout: WorkspaceLayout,
    driver: MarketNewsBatchDriver,
) -> Path:
    path = advance_live_source_checkpoint(
        layout,
        target_key=driver.target_key,
        channel="market_news",
        last_successful_batch_end=driver.last_successful_batch_end,
    )
    expected_path = live_source_checkpoint_path(
        layout,
        target_key=driver.target_key,
        channel="market_news",
    )
    if path != expected_path:
        raise LiveRuntimeError("live source checkpoint writer returned an unexpected path.")
    return path


def _source_loop_sleep_seconds(
    web_search_drivers: tuple[WebSearchBatchDriver, ...],
    market_news_drivers: tuple[MarketNewsBatchDriver, ...],
    current_time: datetime,
) -> float:
    next_due_times = [driver.next_due_at for driver in web_search_drivers]
    next_due_times.extend(driver.next_due_at for driver in market_news_drivers)
    if not next_due_times:
        return _MAX_SOURCE_SLEEP_SECONDS
    next_due_at = min(next_due_times)
    seconds_until_due = max(0.0, (next_due_at - current_time).total_seconds())
    return min(
        _MAX_SOURCE_SLEEP_SECONDS,
        seconds_until_due,
    )


def _emit_source_batch_receipt(
    *,
    emit: LiveRuntimeEmitter,
    channel: str,
    target_key: str,
    receipt: LiveWebSearchBatchReceipt | LiveMarketNewsBatchReceipt,
    source_batch_count: int,
) -> None:
    emit(
        "live source batch: "
        f"count={source_batch_count} "
        f"channel={channel} "
        f"target_key={target_key} "
        f"material_count={receipt.material_count} "
        f"raw_published_count={len(receipt.raw_source_receipts)}"
    )

__all__ = [
    "LiveRuntimeError",
    "LiveRuntimeReceipt",
    "build_live_market_news_drivers",
    "build_live_market_news_websocket_drivers",
    "build_live_web_search_drivers",
    "run_live_runtime_loop",
]
