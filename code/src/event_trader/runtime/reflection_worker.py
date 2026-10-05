"""Nonblocking reflection worker for the durable reflection side stream."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from event_trader.reflection.review_loop import ReflectionHeartbeatReceipt

from .reflection_outbox import (
    FileBackedReflectionCompletionOutbox,
    ReflectionCompletionOutboxEntry,
)
from .reflection_queue import (
    FileBackedReflectionWorkQueue,
    ReflectionQueueCompletion,
    ReflectionQueueFailure,
)

type ReflectionWorkerStatus = Literal["no_work", "completed", "failed"]
type ReflectionCycleExecutor = Callable[[int, datetime], ReflectionHeartbeatReceipt]


class ReflectionWorkerError(ValueError):
    """Raised when reflection worker dependencies or outputs are invalid."""


@dataclass(frozen=True, slots=True)
class ReflectionWorkerProcessResult:
    """Observable result from one reflection worker processing attempt."""

    status: ReflectionWorkerStatus
    work_item_id: str | None = None
    completion: ReflectionQueueCompletion | None = None
    failure: ReflectionQueueFailure | None = None
    outbox_entry: ReflectionCompletionOutboxEntry | None = None

    def __post_init__(self) -> None:
        if self.status not in {"no_work", "completed", "failed"}:
            raise ReflectionWorkerError("status must be no_work, completed, or failed.")
        if self.status == "no_work":
            if any(
                value is not None
                for value in (self.work_item_id, self.completion, self.failure, self.outbox_entry)
            ):
                raise ReflectionWorkerError("no_work result must not carry work state.")
            return
        if not isinstance(self.work_item_id, str) or not self.work_item_id.strip():
            raise ReflectionWorkerError("work_item_id must be present for work results.")
        if self.status == "completed":
            if not isinstance(self.completion, ReflectionQueueCompletion):
                raise ReflectionWorkerError("completed result requires completion.")
            if not isinstance(self.outbox_entry, ReflectionCompletionOutboxEntry):
                raise ReflectionWorkerError("completed result requires outbox_entry.")
            if self.outbox_entry.entry_kind != "completed":
                raise ReflectionWorkerError("completed result requires completed outbox entry.")
            if self.failure is not None:
                raise ReflectionWorkerError("completed result must not carry failure.")
            return
        if not isinstance(self.failure, ReflectionQueueFailure):
            raise ReflectionWorkerError("failed result requires failure.")
        if not isinstance(self.outbox_entry, ReflectionCompletionOutboxEntry):
            raise ReflectionWorkerError("failed result requires outbox_entry.")
        if self.outbox_entry.entry_kind != "failed":
            raise ReflectionWorkerError("failed result requires failed outbox entry.")
        if self.completion is not None:
            raise ReflectionWorkerError("failed result must not carry completion.")


class NautilusReflectionWorker:
    """Drain reflection queue items outside the scheduler callback thread."""

    def __init__(
        self,
        *,
        queue: FileBackedReflectionWorkQueue,
        execute: ReflectionCycleExecutor,
        outbox: FileBackedReflectionCompletionOutbox,
    ) -> None:
        if not isinstance(queue, FileBackedReflectionWorkQueue):
            raise ReflectionWorkerError("queue must be a FileBackedReflectionWorkQueue.")
        if not callable(execute):
            raise ReflectionWorkerError("execute must be callable.")
        if not isinstance(outbox, FileBackedReflectionCompletionOutbox):
            raise ReflectionWorkerError(
                "outbox must be a FileBackedReflectionCompletionOutbox."
            )
        self._queue = queue
        self._execute = execute
        self._outbox = outbox

    def process_next(self) -> ReflectionWorkerProcessResult:
        claim = self._queue.claim_next()
        if claim is None:
            return ReflectionWorkerProcessResult(status="no_work")
        try:
            receipt = _normalize_receipt(
                self._execute(claim.item.cycle_number, claim.item.checked_at)
            )
            completion = self._queue.complete(
                claim,
                completion_payload={"status": receipt.status},
            )
            outbox_entry = self._outbox.record_completed(
                work_item_id=claim.work_item_id,
                idempotency_key=claim.idempotency_key,
                cycle_number=claim.item.cycle_number,
                checked_at=claim.item.checked_at,
                recorded_at=completion.completed_at,
                receipt=receipt,
            )
            return ReflectionWorkerProcessResult(
                status="completed",
                work_item_id=claim.work_item_id,
                completion=completion,
                outbox_entry=outbox_entry,
            )
        except Exception as exc:
            failure = self._queue.fail(
                claim,
                reason=type(exc).__name__,
                error=str(exc),
            )
            outbox_entry = self._outbox.record_failed(
                work_item_id=claim.work_item_id,
                idempotency_key=claim.idempotency_key,
                cycle_number=claim.item.cycle_number,
                checked_at=claim.item.checked_at,
                recorded_at=failure.recorded_at,
                failure_reason=failure.reason,
                failure_error=failure.error,
                failure_record_ref=failure.failure_record_ref,
            )
            return ReflectionWorkerProcessResult(
                status="failed",
                work_item_id=claim.work_item_id,
                failure=failure,
                outbox_entry=outbox_entry,
            )

    def drain(self, *, limit: int | None = None) -> tuple[ReflectionWorkerProcessResult, ...]:
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise ReflectionWorkerError("limit must be a positive integer when provided.")
        results: list[ReflectionWorkerProcessResult] = []
        while limit is None or len(results) < limit:
            result = self.process_next()
            if result.status == "no_work":
                break
            results.append(result)
        return tuple(results)


def _normalize_receipt(receipt: ReflectionHeartbeatReceipt) -> ReflectionHeartbeatReceipt:
    if not isinstance(receipt, ReflectionHeartbeatReceipt):
        raise ReflectionWorkerError("execute must return a ReflectionHeartbeatReceipt.")
    return receipt


__all__ = [
    "NautilusReflectionWorker",
    "ReflectionCycleExecutor",
    "ReflectionWorkerError",
    "ReflectionWorkerProcessResult",
]
