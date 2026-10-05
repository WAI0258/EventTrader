"""Project-owned kernel lifecycle for the event-trader runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from event_trader.config import KernelConfig
from event_trader.workspace import BootstrapWorkspace

type KernelState = Literal[
    "created",
    "bootstrapped",
    "running",
    "stopping",
    "stopped",
]

type LifecycleEmitter = Callable[[str], None]
type SleepHook = Callable[[float], None]


class KernelLifecycleError(RuntimeError):
    """Raised when an impossible lifecycle transition is requested."""


class KernelRuntimeError(RuntimeError):
    """Raised when the kernel fails while already running."""

    def __init__(self, message: str, *, phase: KernelState) -> None:
        super().__init__(message)
        self.phase = phase


class EventTraderKernel:
    """Minimal single-process kernel with explicit lifecycle states."""

    def __init__(
        self,
        *,
        config: KernelConfig,
        workspace: BootstrapWorkspace,
        emit: LifecycleEmitter,
        sleep: SleepHook,
    ) -> None:
        if not callable(emit):
            raise KernelLifecycleError("emit must be an explicit callable.")
        if not callable(sleep):
            raise KernelLifecycleError("sleep must be an explicit callable.")
        self.config = config
        self.workspace = workspace
        self._emit = emit
        self._sleep = sleep
        self._state: KernelState = "created"
        self._heartbeat_count = 0
        self._stop_requested = False
        self._transition_history: list[KernelState] = [self._state]
        self._emit(_format_lifecycle_message(self._state))
        self._emit_workspace_layout()

    @property
    def state(self) -> KernelState:
        """Return the current lifecycle state."""
        return self._state

    @property
    def heartbeat_count(self) -> int:
        """Return the number of completed heartbeats."""
        return self._heartbeat_count

    @property
    def transition_history(self) -> tuple[KernelState, ...]:
        """Return the ordered lifecycle states reached by this kernel."""
        return tuple(self._transition_history)

    def bootstrap(self) -> None:
        """Validate the kernel can begin its run lifecycle."""
        self._require_state("created", action="bootstrap")
        self._transition(
            "bootstrapped",
            details=(
                f"mode={self.config.mode} "
                f"workspace_root={self.workspace.root} "
                f"runtime_root={self.workspace.runtime_root} "
                f"heartbeat_interval_seconds={self.config.heartbeat_interval_seconds} "
                f"reflection_interval_hours={self.config.reflection_interval_hours} "
                f"reflection_lookback_hours={self.config.reflection_lookback_hours} "
                "reflection_horizons_hours="
                f"{list(self.config.reflection_horizons_hours)}"
            ),
        )

    def stop(self, *, reason: str = "operator requested stop") -> bool:
        """Request a deterministic stop without relying on external signals."""
        if self._state == "created":
            raise KernelLifecycleError(
                "Cannot stop kernel before bootstrap from state 'created'."
            )
        if self._state == "bootstrapped":
            self._stop_requested = True
            self._transition("stopping", details=f"reason={reason}")
            return True
        if self._state == "running":
            self._stop_requested = True
            self._transition("stopping", details=f"reason={reason}")
            return True
        return False

    def run(self) -> int:
        """Enter the long-running kernel loop until an explicit stop is requested."""
        if self._state == "created":
            self.bootstrap()

        self._require_state("bootstrapped", action="run")
        self._transition("running")

        try:
            while not self._stop_requested:
                next_heartbeat = self._heartbeat_count + 1
                self._emit(f"kernel heartbeat: {next_heartbeat}")
                self._heartbeat_count = next_heartbeat

                self._sleep(self.config.heartbeat_interval_seconds)
        except Exception as exc:
            failed_phase = self._state
            if self._state == "running":
                self.stop(reason=f"heartbeat failure: {exc}")
            self._finalize_stop()
            raise KernelRuntimeError(
                f"Kernel run loop failed after {self._heartbeat_count} heartbeats: {exc}",
                phase=failed_phase,
            ) from exc

        self._finalize_stop()
        return self._heartbeat_count

    def _finalize_stop(self) -> None:
        if self._state == "running":
            self.stop(reason="run loop completed")
        if self._state == "stopping":
            self._transition("stopped")

    def _require_state(self, expected: KernelState, *, action: str) -> None:
        if self._state != expected:
            raise KernelLifecycleError(
                f"Cannot {action} kernel from state '{self._state}'; expected '{expected}'."
            )

    def _transition(self, next_state: KernelState, *, details: str | None = None) -> None:
        self._state = next_state
        self._transition_history.append(next_state)
        self._emit(_format_lifecycle_message(next_state, details=details))

    def _emit_workspace_layout(self) -> None:
        if self.workspace.layout is None:
            return
        self._emit(self.workspace.layout.format_observability_line())



def _format_lifecycle_message(state: KernelState, *, details: str | None = None) -> str:
    if details:
        return f"kernel lifecycle: {state} {details}"
    return f"kernel lifecycle: {state}"
