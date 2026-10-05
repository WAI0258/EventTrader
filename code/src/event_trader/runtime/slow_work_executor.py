"""Slow-work executor owned by the runtime graph."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

type SlowWorkStep = Callable[[], object]


@dataclass(frozen=True, slots=True)
class _SlowWorkLane:
    name: str
    drain_one: SlowWorkStep
    pump: SlowWorkStep
    concurrency: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("slow-work lane name must not be empty.")
        if not callable(self.drain_one):
            raise TypeError("slow-work lane drain_one must be callable.")
        if not callable(self.pump):
            raise TypeError("slow-work lane pump must be callable.")
        if not isinstance(self.concurrency, int) or isinstance(self.concurrency, bool):
            raise TypeError("slow-work lane concurrency must be an integer.")
        if self.concurrency <= 0:
            raise ValueError("slow-work lane concurrency must be greater than zero.")


class SlowWorkExecutor:
    """Own one composed slow-work pass across runtime worker lanes."""

    def __init__(
        self,
        *,
        checker_drain_one: SlowWorkStep,
        checker_pump: SlowWorkStep,
        checker_concurrency: int = 1,
        analysis_drain_one: SlowWorkStep,
        analysis_pump: SlowWorkStep,
        analysis_concurrency: int = 1,
        pm_review_drain_one: SlowWorkStep,
        pm_review_pump: SlowWorkStep,
        pm_review_concurrency: int = 1,
    ) -> None:
        self._lanes = (
            _SlowWorkLane(
                name="checker",
                drain_one=checker_drain_one,
                pump=checker_pump,
                concurrency=checker_concurrency,
            ),
            _SlowWorkLane(
                name="analysis",
                drain_one=analysis_drain_one,
                pump=analysis_pump,
                concurrency=analysis_concurrency,
            ),
            _SlowWorkLane(
                name="pm_review",
                drain_one=pm_review_drain_one,
                pump=pm_review_pump,
                concurrency=pm_review_concurrency,
            ),
        )

    def drain_once(self) -> None:
        for lane in self._lanes:
            self._drain_lane(lane)
            lane.pump()

    def _drain_lane(self, lane: _SlowWorkLane) -> None:
        if lane.concurrency == 1:
            self._run_lane_worker(drain_one=lane.drain_one)
            return
        with ThreadPoolExecutor(
            max_workers=lane.concurrency,
            thread_name_prefix=f"event-trader-{lane.name}",
        ) as executor:
            futures = [
                executor.submit(self._run_lane_worker, drain_one=lane.drain_one)
                for _ in range(lane.concurrency)
            ]
            for future in futures:
                future.result()

    def _run_lane_worker(self, *, drain_one: SlowWorkStep) -> None:
        while self._drain_result_has_work(drain_one()):
            continue

    @staticmethod
    def _drain_result_has_work(result: object) -> bool:
        if result is None:
            return False
        if isinstance(result, tuple | list):
            return len(result) > 0
        return True
