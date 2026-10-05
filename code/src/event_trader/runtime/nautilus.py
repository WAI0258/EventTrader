"""Project-owned Nautilus node bootstrap for event-trader."""

from __future__ import annotations

import asyncio

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.component import Clock, MessageBus
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.live.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.trading.trader import Trader

_DEFAULT_TRADER_ID = "EVENT-TRADER-001"
_NAUTILUS_LOG_GUARD: object | None = None


class EventTraderNautilusNodeError(RuntimeError):
    """Raised when the Nautilus runtime node lifecycle is invalid."""


class EventTraderNautilusNode:
    """Thin wrapper around a real Nautilus TradingNode."""

    def __init__(
        self,
        *,
        config: TradingNodeConfig,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(config, TradingNodeConfig):
            raise TypeError("config must be a TradingNodeConfig.")
        if clock is not None and not isinstance(clock, Clock):
            raise TypeError("clock must be a Nautilus Clock when provided.")
        _install_fresh_event_loop_when_idle()
        self._node = TradingNode(config)
        if clock is not None:
            self._node.kernel._clock = clock
        _retain_nautilus_log_guard(self._node.kernel._log_guard)
        self._registered_actor_ids: list[object] = []
        self._started_actor_ids: list[object] = []
        self._disposed = False

    @property
    def node(self) -> TradingNode:
        return self._node

    @property
    def trader(self) -> Trader:
        return self._node.trader

    @property
    def msgbus(self) -> MessageBus:
        return self._node.kernel.msgbus

    @property
    def clock(self) -> Clock:
        return self._node.kernel.clock

    def register_actor(self, actor: Actor) -> None:
        self._ensure_not_disposed("register actor")
        if not isinstance(actor, Actor):
            raise TypeError("actor must be a Nautilus Actor.")
        self.trader.add_actor(actor)
        self._registered_actor_ids.append(actor.id)

    def start(self) -> None:
        self._ensure_not_disposed("start")
        for actor_id in self._registered_actor_ids:
            if actor_id in self._started_actor_ids:
                continue
            self.trader.start_actor(actor_id)
            self._started_actor_ids.append(actor_id)

    def stop(self) -> None:
        if self._disposed:
            return
        errors: list[Exception] = []
        for actor_id in reversed(self._started_actor_ids):
            try:
                self.trader.stop_actor(actor_id)
            except Exception as exc:
                errors.append(exc)
        self._started_actor_ids.clear()
        try:
            self._node.dispose()
        finally:
            self._disposed = True
            _install_fresh_event_loop_when_idle()
        if errors:
            raise EventTraderNautilusNodeError(
                f"failed to stop Nautilus actor: {errors[0]}"
            ) from errors[0]

    def _ensure_not_disposed(self, action: str) -> None:
        if self._disposed:
            raise EventTraderNautilusNodeError(
                f"cannot {action} after Nautilus node disposal."
            )


def build_event_trader_nautilus_node(
    *,
    trader_id: str = _DEFAULT_TRADER_ID,
    clock: Clock | None = None,
) -> EventTraderNautilusNode:
    """Build the project-owned Nautilus runtime node."""

    return EventTraderNautilusNode(
        config=TradingNodeConfig(
            trader_id=TraderId(trader_id),
            logging=LoggingConfig(
                log_level="OFF",
                log_level_file="OFF",
                print_config=False,
                use_pyo3=False,
            ),
        ),
        clock=clock,
    )


def _install_fresh_event_loop_when_idle() -> None:
    try:
        asyncio.get_running_loop()
        return
    except RuntimeError:
        pass
    asyncio.set_event_loop(asyncio.new_event_loop())


def _retain_nautilus_log_guard(log_guard: object | None) -> None:
    global _NAUTILUS_LOG_GUARD
    if _NAUTILUS_LOG_GUARD is None and log_guard is not None:
        _NAUTILUS_LOG_GUARD = log_guard


__all__ = [
    "EventTraderNautilusNode",
    "EventTraderNautilusNodeError",
    "build_event_trader_nautilus_node",
]
