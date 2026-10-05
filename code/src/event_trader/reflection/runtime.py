"""Reflection runtime helpers owned outside the composition root."""

from __future__ import annotations

from event_trader.composition_error import CompositionError
from event_trader.contracts.ports import OutcomeContextPort
from event_trader.kernel import EventTraderKernel
from event_trader.reflection.review_loop import (
    ReflectionReviewEvaluator,
    TargetCloseReflectionReviewEvaluator,
    TargetOpenPositionHorizonReflectionEvaluator,
    TargetReflectionReviewEvaluator,
)
from event_trader.runtime.bootstrap import _ResidentKernelRunner
from event_trader.runtime.reflection_scheduler import ReflectionCycleScheduler


class LiveReflectionHostRunner:
    """Coordinate live host residency with the reflection scheduler."""

    def __init__(
        self,
        *,
        kernel: EventTraderKernel,
        resident_runner: _ResidentKernelRunner,
        reflection_scheduler: ReflectionCycleScheduler,
    ) -> None:
        if not isinstance(kernel, EventTraderKernel):
            raise CompositionError("kernel must be an EventTraderKernel instance.")
        if not isinstance(resident_runner, _ResidentKernelRunner):
            raise CompositionError(
                "resident_runner must be a _ResidentKernelRunner instance."
            )
        if not (
            callable(getattr(reflection_scheduler, "start", None))
            and callable(getattr(reflection_scheduler, "close", None))
        ):
            raise CompositionError(
                "reflection_scheduler must provide start() and close()."
            )
        self._kernel = kernel
        self._resident_runner = resident_runner
        self._reflection_scheduler = reflection_scheduler

    def run_blocking(self) -> int:
        self._reflection_scheduler.start()
        try:
            return self._resident_runner.run_blocking()
        finally:
            self._reflection_scheduler.close()

    def start(self) -> None:
        self._reflection_scheduler.start()
        try:
            self._resident_runner.start()
        except BaseException:
            self._reflection_scheduler.close()
            raise

    def join(self, timeout_seconds: float | None) -> int | None:
        try:
            heartbeat_count = self._resident_runner.join(timeout_seconds)
        except BaseException:
            self._reflection_scheduler.close()
            raise
        if heartbeat_count is not None:
            self._reflection_scheduler.close()
        return heartbeat_count

    def stop(self, *, reason: str) -> bool:
        try:
            return self._kernel.stop(reason=reason)
        finally:
            self._reflection_scheduler.close()


def missing_reflection_dependencies(
    *,
    outcome_context: OutcomeContextPort | None,
    evaluate_review: ReflectionReviewEvaluator | None,
    evaluate_target_review: TargetReflectionReviewEvaluator | None,
    evaluate_target_close_review: TargetCloseReflectionReviewEvaluator | None,
    evaluate_target_open_position_review: (
        TargetOpenPositionHorizonReflectionEvaluator | None
    ),
) -> tuple[str, ...]:
    missing: list[str] = []
    if outcome_context is None:
        missing.append("outcome_context")
    if evaluate_review is None:
        missing.append("review_evaluator")
    if evaluate_target_review is None:
        missing.append("target_review_evaluator")
    if evaluate_target_close_review is None:
        missing.append("target_close_review_evaluator")
    if evaluate_target_open_position_review is None:
        missing.append("target_open_position_reflection_evaluator")
    return tuple(missing)


__all__ = [
    "LiveReflectionHostRunner",
    "missing_reflection_dependencies",
]
