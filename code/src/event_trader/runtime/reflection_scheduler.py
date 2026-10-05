"""Live reflection scheduling on the Nautilus clock."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from threading import Event, Lock, Thread

from nautilus_trader.common.component import Clock

_DEFAULT_TIMER_NAME = "event-trader-live-reflection"


class ReflectionSchedulerError(RuntimeError):
    """Raised when the live reflection scheduler is misconfigured."""


class ReflectionCycleScheduler:
    """Schedule live reflection cycles on the Nautilus clock."""

    def __init__(
        self,
        *,
        clock: Clock,
        interval: timedelta,
        enqueue_reflection_cycle: Callable[[], object],
        drain_reflection_work: Callable[[], object],
        pump_reflection_completions: Callable[[], object],
        timer_name: str = _DEFAULT_TIMER_NAME,
    ) -> None:
        if not isinstance(clock, Clock):
            raise ReflectionSchedulerError("clock must be a Nautilus Clock.")
        if not isinstance(interval, timedelta) or interval <= timedelta(0):
            raise ReflectionSchedulerError("interval must be a positive timedelta.")
        if not callable(enqueue_reflection_cycle):
            raise ReflectionSchedulerError("enqueue_reflection_cycle must be callable.")
        if not callable(drain_reflection_work):
            raise ReflectionSchedulerError("drain_reflection_work must be callable.")
        if not callable(pump_reflection_completions):
            raise ReflectionSchedulerError("pump_reflection_completions must be callable.")
        if not isinstance(timer_name, str) or not timer_name.strip():
            raise ReflectionSchedulerError("timer_name must be a non-empty string.")
        self._clock = clock
        self._interval = interval
        self._enqueue_reflection_cycle = enqueue_reflection_cycle
        self._drain_reflection_work = drain_reflection_work
        self._pump_reflection_completions = pump_reflection_completions
        self._timer_name = timer_name
        self._lock = Lock()
        self._wakeup = Event()
        self._worker = Thread(
            target=self._run_worker,
            name="event-trader-reflection-worker",
            daemon=True,
        )
        self._started = False
        self._closed = False
        self._cycle_due = False

    @property
    def timer_name(self) -> str:
        return self._timer_name

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise ReflectionSchedulerError(
                    "cannot start a closed reflection scheduler."
                )
            if self._started:
                return
            self._clock.set_timer(
                self._timer_name,
                self._interval,
                callback=self._on_timer,
                allow_past=True,
                fire_immediately=False,
            )
            self._started = True
            self._worker.start()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            started = self._started
            self._cycle_due = False
        if started:
            self._clock.cancel_timer(self._timer_name)
        self._wakeup.set()
        if started:
            self._worker.join()

    def _on_timer(self, _event) -> None:
        with self._lock:
            if self._closed or self._cycle_due:
                return
            self._cycle_due = True
        try:
            self._enqueue_reflection_cycle()
        except Exception:
            with self._lock:
                self._cycle_due = False
            raise
        self._wakeup.set()

    def _run_worker(self) -> None:
        while True:
            self._wakeup.wait()
            self._wakeup.clear()
            while True:
                with self._lock:
                    if self._closed:
                        return
                    if not self._cycle_due:
                        break
                try:
                    self._drain_reflection_work()
                    self._pump_reflection_completions()
                finally:
                    with self._lock:
                        self._cycle_due = False


__all__ = [
    "ReflectionCycleScheduler",
    "ReflectionSchedulerError",
]
