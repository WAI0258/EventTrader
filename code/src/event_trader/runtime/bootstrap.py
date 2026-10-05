"""Runtime bootstrap primitives used by the composition root."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock, Thread

from event_trader.composition_error import CompositionError
from event_trader.ingest.admission import AdmissionOutputs
from event_trader.kernel import EventTraderKernel
from event_trader.reflection.review_loop import ReflectionHeartbeatReceipt
from event_trader.replay.runner import ReplayRunner
from event_trader.runtime.graph import EventTraderRuntimeGraph
from event_trader.runtime.release import RuntimeReleaseReceipt

type ResourceCloser = Callable[[], None]


class _OwnedRuntimeResources:
    """Track runtime-owned cleanup callbacks with idempotent close semantics."""

    def __init__(self) -> None:
        self._closers: list[ResourceCloser] = []
        self._closed = False
        self._lock = Lock()

    def register(self, close: ResourceCloser) -> None:
        if not callable(close):
            raise CompositionError("owned runtime resource closer must be callable.")
        with self._lock:
            if self._closed:
                raise CompositionError(
                    "cannot register a composed runtime resource after close()."
                )
            self._closers.append(close)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            closers = tuple(reversed(self._closers))
            self._closers.clear()
            self._closed = True
        errors: list[Exception] = []
        for close in closers:
            try:
                close()
            except Exception as exc:  # pragma: no cover - defensive cleanup guard
                errors.append(exc)
        if errors:
            raise CompositionError(
                f"failed to close composed runtime resources: {errors[0]}"
            ) from errors[0]


class _RuntimeGraphAdmissionReleasePath:
    """Composition-owned release path that drains and pumps the runtime graph."""

    def __init__(
        self,
        *,
        downstream,
        runtime_graph: EventTraderRuntimeGraph,
        observe_admitted_event: Callable[[AdmissionOutputs], None] | None = None,
    ) -> None:
        if not callable(getattr(downstream, "release_admitted", None)):
            raise CompositionError(
                "runtime graph release path requires downstream.release_admitted(outputs)."
            )
        if not isinstance(runtime_graph, EventTraderRuntimeGraph):
            raise CompositionError(
                "runtime graph release path requires an EventTraderRuntimeGraph."
            )
        if observe_admitted_event is not None and not callable(observe_admitted_event):
            raise CompositionError(
                "runtime graph release path observe_admitted_event must be callable."
            )
        self._downstream = downstream
        self._runtime_graph = runtime_graph
        self._observe_admitted_event = observe_admitted_event

    def release_admitted(self, outputs: AdmissionOutputs) -> RuntimeReleaseReceipt:
        if self._observe_admitted_event is not None:
            self._observe_admitted_event(outputs)
        receipt = self._downstream.release_admitted(outputs)
        self._runtime_graph.drain_all()
        return receipt


@dataclass(frozen=True, slots=True)
class LiveRuntimeHost:
    """Committed long-running live-host boundary."""

    _run: Callable[[], int]
    _start: Callable[[], None]
    _join: Callable[[float | None], int | None]
    _stop: Callable[[str], bool]
    _release_admitted: Callable[[AdmissionOutputs], RuntimeReleaseReceipt]
    _state: Callable[[], str]
    _heartbeat_count: Callable[[], int]
    _transition_history: Callable[[], tuple[str, ...]]
    _last_reflection_receipt: Callable[[], ReflectionHeartbeatReceipt | None]
    _advance_runtime_time: Callable[[datetime], object] = field(default=lambda _now: None)

    @property
    def state(self) -> str:
        """Return the current live-host lifecycle state."""
        return self._state()

    @property
    def heartbeat_count(self) -> int:
        """Return the number of completed host heartbeats."""
        return self._heartbeat_count()

    @property
    def transition_history(self) -> tuple[str, ...]:
        """Return the ordered live-host lifecycle states reached so far."""
        return self._transition_history()

    @property
    def last_reflection_receipt(self) -> ReflectionHeartbeatReceipt | None:
        """Return the most recent live reflection receipt, if any."""
        return self._last_reflection_receipt()

    def run(self) -> int:
        """Run the long-running live host."""
        return self._run()

    def start(self) -> None:
        """Start the live host residency in the background."""
        self._start()

    def join(self, *, timeout_seconds: float | None = None) -> int | None:
        """Wait for a background live-host run to finish."""
        if timeout_seconds is not None:
            if isinstance(timeout_seconds, bool) or not isinstance(
                timeout_seconds, (int, float)
            ):
                raise CompositionError("timeout_seconds must be a positive number.")
            if timeout_seconds <= 0:
                raise CompositionError("timeout_seconds must be greater than zero.")
        return self._join(timeout_seconds)

    def stop(self, *, reason: str = "operator requested stop") -> bool:
        """Request a deterministic host stop."""
        return self._stop(reason)

    def release_admitted(self, outputs: AdmissionOutputs) -> RuntimeReleaseReceipt:
        """Release one admitted live evidence payload through the live host."""
        if not isinstance(outputs, AdmissionOutputs):
            raise CompositionError("live host intake requires an AdmissionOutputs instance.")
        return self._release_admitted(outputs)

    def advance_runtime_time(self, *, current_time: datetime) -> object:
        """Advance live time-driven runtime effects to current_time."""
        return self._advance_runtime_time(current_time)


@dataclass(frozen=True, slots=True)
class BoundedReplayRuntime:
    """Committed bounded replay execution boundary."""

    _runner: ReplayRunner
    _drain_reflection_cycle: Callable[[], ReflectionHeartbeatReceipt]
    _last_reflection_receipt: Callable[[], ReflectionHeartbeatReceipt | None]
    _finalize_pipeline: Callable[[], None]
    _observe_replay_time: Callable[[datetime], object]
    _next_replay_deferred_time_before: Callable[[datetime], datetime | None]
    _resume_runtime_backlog: Callable[[datetime], object]
    _advance_replay_clock: Callable[[datetime], None]
    _last_replay_at: datetime | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def released_event_ids(self) -> tuple[str, ...]:
        """Return replay releases in stable order."""
        return self._runner.released_event_ids

    @property
    def last_reflection_receipt(self) -> ReflectionHeartbeatReceipt | None:
        """Return the most recent replay reflection receipt, if any."""
        return self._last_reflection_receipt()

    def run_step(
        self,
        *,
        target_key: str,
        replay_at,
        historical_inputs,
        labels: list[str] | None = None,
        ts_init=None,
    ):
        """Run one bounded replay step through the committed replay runtime."""
        self._advance_to_replay_at(replay_at)
        return self._runner.run_step(
            target_key=target_key,
            replay_at=replay_at,
            historical_inputs=historical_inputs,
            labels=labels,
            ts_init=ts_init,
        )

    def drain_reflection_cycle(self) -> ReflectionHeartbeatReceipt:
        """Run one bounded replay reflection cycle explicitly."""
        return self._drain_reflection_cycle()

    def observe_replay_time(self, *, replay_at: datetime) -> object:
        """Advance replay-only deferred runtime effects without re-running an event."""
        self._advance_to_replay_at(replay_at)
        return self._observe_replay_time(replay_at)

    def resume_runtime_backlog(self, *, replay_at: datetime) -> object:
        """Drain reclaimed runtime backlog at replay_at without injecting new evidence."""
        self._advance_to_replay_at(replay_at)
        return self._resume_runtime_backlog(replay_at)

    def next_replay_deferred_time_before(
        self,
        *,
        boundary: datetime,
    ) -> datetime | None:
        """Return the next pending replay deferred time at or before the boundary."""
        return self._next_replay_deferred_time_before(boundary)

    def advance_deferred_until(
        self,
        *,
        replay_at: datetime,
        include_boundary: bool,
        after_advance: Callable[[datetime], None] | None = None,
    ) -> None:
        """Advance replay time-driven effects due before or at replay_at."""
        while True:
            deadline = self.next_replay_deferred_time_before(boundary=replay_at)
            if deadline is None or deadline >= replay_at:
                break
            self.observe_replay_time(replay_at=deadline)
            if after_advance is not None:
                after_advance(deadline)
        if include_boundary:
            self.observe_replay_time(replay_at=replay_at)
            if after_advance is not None:
                after_advance(replay_at)

    def finalize_pipeline(self) -> None:
        """Flush replay-only deferred runtime effects after the final event."""
        self._finalize_pipeline()

    def _advance_to_replay_at(self, replay_at: datetime) -> None:
        self._advance_replay_clock(replay_at)
        object.__setattr__(self, "_last_replay_at", replay_at)


class _ResidentKernelRunner:
    """Own background residency for the committed live host."""

    def __init__(self, *, kernel: EventTraderKernel) -> None:
        if not isinstance(kernel, EventTraderKernel):
            raise CompositionError("kernel must be an EventTraderKernel instance.")
        self._kernel = kernel
        self._lock = Lock()
        self._thread: Thread | None = None
        self._completed_heartbeat_count: int | None = None
        self._run_error: BaseException | None = None

    def run_blocking(self) -> int:
        with self._lock:
            if self._thread is not None:
                raise CompositionError(
                    "live host run() is unavailable after start(); use join() "
                    "for background residency."
                )
        return self._kernel.run()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise CompositionError(
                    "live host background residency has already been started."
                )
            self._thread = Thread(
                target=self._run_in_background,
                name="event-trader-live-host",
                daemon=True,
            )
            self._thread.start()

    def join(self, timeout_seconds: float | None) -> int | None:
        with self._lock:
            thread = self._thread
        if thread is None:
            raise CompositionError("live host join() requires a prior start() call.")
        thread.join(timeout_seconds)
        if thread.is_alive():
            return None
        if self._run_error is not None:
            raise self._run_error
        if self._completed_heartbeat_count is None:
            raise CompositionError(
                "live host background residency finished without a completion result."
            )
        return self._completed_heartbeat_count

    def _run_in_background(self) -> None:
        try:
            self._completed_heartbeat_count = self._kernel.run()
        except BaseException as exc:
            self._run_error = exc
