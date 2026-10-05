"""Nonblocking PMReview worker for the Nautilus side stream."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .contracts import PMReviewCompleted, PMReviewFailed, base_message_kwargs
from .pm_review_outbox import (
    FileBackedPMReviewCompletionOutbox,
    PMReviewCompletionOutboxEntry,
)
from .pm_review_queue import (
    FileBackedPMReviewWorkQueue,
    PMReviewQueueCompletion,
    PMReviewQueueFailure,
)
from .pm_review_resolver import RuntimePMReviewRequestResolver, RuntimePMReviewResolution

type PMReviewWorkerStatus = Literal["no_work", "completed", "failed"]
type RuntimePublisher = Callable[[str, object, bool], None]


class PMReviewWorkerError(ValueError):
    """Raised when PMReview worker dependencies or outputs are invalid."""


@dataclass(frozen=True, slots=True)
class RuntimePMReviewExecutionResult:
    """Normalized PMReview execution outcome for the runtime worker."""

    status: Literal["completed", "failed"]
    pm_decision_ref: str | None = None
    failure_record_ref: str | None = None
    failure_reason: str | None = None
    failure_error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed"}:
            raise PMReviewWorkerError("status must be completed or failed.")
        if self.status == "completed":
            if not isinstance(self.pm_decision_ref, str) or not self.pm_decision_ref.strip():
                raise PMReviewWorkerError("completed result requires pm_decision_ref.")
            if any(
                value is not None
                for value in (
                    self.failure_record_ref,
                    self.failure_reason,
                    self.failure_error,
                )
            ):
                raise PMReviewWorkerError("completed result must not carry failure fields.")
            return
        if not isinstance(self.failure_record_ref, str) or not self.failure_record_ref.strip():
            raise PMReviewWorkerError("failed result requires failure_record_ref.")
        if not isinstance(self.failure_reason, str) or not self.failure_reason.strip():
            raise PMReviewWorkerError("failed result requires failure_reason.")
        if not isinstance(self.failure_error, str) or not self.failure_error.strip():
            raise PMReviewWorkerError("failed result requires failure_error.")
        if self.pm_decision_ref is not None:
            raise PMReviewWorkerError("failed result must not carry pm_decision_ref.")


type PMReviewExecutor = Callable[[RuntimePMReviewResolution], RuntimePMReviewExecutionResult]


@dataclass(frozen=True, slots=True)
class PMReviewWorkerProcessResult:
    status: PMReviewWorkerStatus
    work_item_id: str | None = None
    completion: PMReviewQueueCompletion | None = None
    failure: PMReviewQueueFailure | None = None
    outbox_entry: PMReviewCompletionOutboxEntry | None = None
    published_message: PMReviewCompleted | PMReviewFailed | None = None

    def __post_init__(self) -> None:
        if self.status not in {"no_work", "completed", "failed"}:
            raise PMReviewWorkerError("status must be no_work, completed, or failed.")
        if self.status == "no_work":
            if any(
                value is not None
                for value in (
                    self.work_item_id,
                    self.completion,
                    self.failure,
                    self.outbox_entry,
                    self.published_message,
                )
            ):
                raise PMReviewWorkerError("no_work result must not carry work state.")
            return
        if not isinstance(self.work_item_id, str) or not self.work_item_id.strip():
            raise PMReviewWorkerError("work_item_id must be present for work results.")
        if self.status == "completed":
            if not isinstance(self.completion, PMReviewQueueCompletion):
                raise PMReviewWorkerError("completed result requires completion.")
            if not isinstance(self.outbox_entry, PMReviewCompletionOutboxEntry):
                raise PMReviewWorkerError("completed result requires outbox_entry.")
            if self.outbox_entry.entry_kind != "completed":
                raise PMReviewWorkerError("completed result requires completed outbox entry.")
            if self.published_message is not None and not isinstance(
                self.published_message,
                PMReviewCompleted,
            ):
                raise PMReviewWorkerError(
                    "completed result published_message must be PMReviewCompleted."
                )
            if self.failure is not None:
                raise PMReviewWorkerError("completed result must not carry failure.")
        if self.status == "failed":
            if not isinstance(self.failure, PMReviewQueueFailure):
                raise PMReviewWorkerError("failed result requires failure.")
            if not isinstance(self.outbox_entry, PMReviewCompletionOutboxEntry):
                raise PMReviewWorkerError("failed result requires outbox_entry.")
            if self.outbox_entry.entry_kind != "failed":
                raise PMReviewWorkerError("failed result requires failed outbox entry.")
            if self.published_message is not None and not isinstance(
                self.published_message,
                PMReviewFailed,
            ):
                raise PMReviewWorkerError("failed result published_message must be PMReviewFailed.")
            if self.completion is not None:
                raise PMReviewWorkerError("failed result must not carry completion.")


class NautilusPMReviewWorker:
    """Drain PMReview queue items outside Nautilus MessageBus callbacks."""

    def __init__(
        self,
        *,
        queue: FileBackedPMReviewWorkQueue,
        resolver: RuntimePMReviewRequestResolver,
        execute: PMReviewExecutor,
        outbox: FileBackedPMReviewCompletionOutbox,
        publish: RuntimePublisher | None = None,
    ) -> None:
        if not isinstance(queue, FileBackedPMReviewWorkQueue):
            raise PMReviewWorkerError("queue must be a FileBackedPMReviewWorkQueue.")
        if not isinstance(resolver, RuntimePMReviewRequestResolver):
            raise PMReviewWorkerError("resolver must be a RuntimePMReviewRequestResolver.")
        if not callable(execute):
            raise PMReviewWorkerError("execute must be callable.")
        if not isinstance(outbox, FileBackedPMReviewCompletionOutbox):
            raise PMReviewWorkerError("outbox must be a FileBackedPMReviewCompletionOutbox.")
        if publish is not None and not callable(publish):
            raise PMReviewWorkerError("publish must be callable when provided.")
        self._queue = queue
        self._resolver = resolver
        self._execute = execute
        self._outbox = outbox
        self._publish = publish

    def process_next(self) -> PMReviewWorkerProcessResult:
        claim = self._queue.claim_next()
        if claim is None:
            return PMReviewWorkerProcessResult(status="no_work")
        try:
            resolution = self._resolver.resolve_work_item(claim.item)
            if resolution.existing_pm_decision_ref is not None:
                completion = self._queue.complete(
                    claim,
                    completion_payload=_reused_completion_payload(),
                    pm_decision_ref=resolution.existing_pm_decision_ref,
                )
                message = _pm_review_completed_message(
                    item=claim.item,
                    completion=completion,
                    source_episode_id=resolution.request.source_episode_id,
                )
                outbox_entry = self._outbox.record_completed(
                    work_item_id=claim.work_item_id,
                    idempotency_key=claim.idempotency_key,
                    message=message,
                )
                published_message = self._publish_if_configured(outbox_entry)
                return PMReviewWorkerProcessResult(
                    status="completed",
                    work_item_id=claim.work_item_id,
                    completion=completion,
                    outbox_entry=outbox_entry,
                    published_message=published_message,
                )
            if resolution.existing_failure_record_ref is not None:
                failure = self._queue.fail(
                    claim,
                    reason="legacy_pm_review_failed",
                    error="legacy PMReview failure already persisted",
                    failure_record_ref=resolution.existing_failure_record_ref,
                )
                failed_message = _pm_review_failed_message(item=claim.item, failure=failure)
                outbox_entry = self._outbox.record_failed(
                    work_item_id=claim.work_item_id,
                    idempotency_key=claim.idempotency_key,
                    message=failed_message,
                )
                published_message = self._publish_if_configured(outbox_entry)
                return PMReviewWorkerProcessResult(
                    status="failed",
                    work_item_id=claim.work_item_id,
                    failure=failure,
                    outbox_entry=outbox_entry,
                    published_message=published_message,
                )
            execution_result = _normalize_execution_result(self._execute(resolution))
            if execution_result.status == "completed":
                completion = self._queue.complete(
                    claim,
                    completion_payload={"source": "runtime_pm_review_execution"},
                    pm_decision_ref=execution_result.pm_decision_ref,
                )
                message = _pm_review_completed_message(
                    item=claim.item,
                    completion=completion,
                    source_episode_id=resolution.request.source_episode_id,
                )
                outbox_entry = self._outbox.record_completed(
                    work_item_id=claim.work_item_id,
                    idempotency_key=claim.idempotency_key,
                    message=message,
                )
                published_message = self._publish_if_configured(outbox_entry)
                return PMReviewWorkerProcessResult(
                    status="completed",
                    work_item_id=claim.work_item_id,
                    completion=completion,
                    outbox_entry=outbox_entry,
                    published_message=published_message,
                )
            if (
                execution_result.failure_reason is None
                or execution_result.failure_error is None
            ):
                raise PMReviewWorkerError(
                    "failed PMReview execution must carry failure reason and error."
                )
            failure = self._queue.fail(
                claim,
                reason=execution_result.failure_reason,
                error=execution_result.failure_error,
                failure_record_ref=execution_result.failure_record_ref,
            )
            failed_message = _pm_review_failed_message(item=claim.item, failure=failure)
            outbox_entry = self._outbox.record_failed(
                work_item_id=claim.work_item_id,
                idempotency_key=claim.idempotency_key,
                message=failed_message,
            )
            published_message = self._publish_if_configured(outbox_entry)
            return PMReviewWorkerProcessResult(
                status="failed",
                work_item_id=claim.work_item_id,
                failure=failure,
                outbox_entry=outbox_entry,
                published_message=published_message,
            )
        except Exception as exc:
            failure = self._queue.fail(
                claim,
                reason=type(exc).__name__,
                error=str(exc),
            )
            failed_message = _pm_review_failed_message(item=claim.item, failure=failure)
            outbox_entry = self._outbox.record_failed(
                work_item_id=claim.work_item_id,
                idempotency_key=claim.idempotency_key,
                message=failed_message,
            )
            published_message = self._publish_if_configured(outbox_entry)
            return PMReviewWorkerProcessResult(
                status="failed",
                work_item_id=claim.work_item_id,
                failure=failure,
                outbox_entry=outbox_entry,
                published_message=published_message,
            )

    def drain(self, *, limit: int | None = None) -> tuple[PMReviewWorkerProcessResult, ...]:
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise PMReviewWorkerError("limit must be a positive integer when provided.")
        results: list[PMReviewWorkerProcessResult] = []
        while limit is None or len(results) < limit:
            result = self.process_next()
            if result.status == "no_work":
                break
            results.append(result)
        return tuple(results)

    def _publish_if_configured(
        self,
        entry: PMReviewCompletionOutboxEntry,
    ) -> PMReviewCompleted | PMReviewFailed | None:
        if self._publish is None:
            return None
        self._publish(entry.message.topic, entry.message, False)
        self._outbox.mark_published(entry)
        return entry.message


def _normalize_execution_result(
    result: RuntimePMReviewExecutionResult,
) -> RuntimePMReviewExecutionResult:
    if not isinstance(result, RuntimePMReviewExecutionResult):
        raise PMReviewWorkerError("execute must return a RuntimePMReviewExecutionResult.")
    return result


def _reused_completion_payload() -> dict[str, object]:
    return {"source": "existing_runtime_completion"}


def _pm_review_completed_message(
    *,
    item: object,
    completion: PMReviewQueueCompletion,
    source_episode_id: str | None,
) -> PMReviewCompleted:
    from .pm_review_queue import PMReviewWorkItem

    if not isinstance(item, PMReviewWorkItem):
        raise PMReviewWorkerError("item must be a PMReviewWorkItem.")
    return PMReviewCompleted(
        **base_message_kwargs(
            message_id=f"pm-review-completed:{item.pm_review_request_id}",
            event_time=item.event_time,
            recorded_at=completion.completed_at,
            correlation_id=item.correlation_id,
            causation_id=item.work_item_id,
            idempotency_key=f"pm_review_completed:{item.idempotency_key}",
        ),
        target_key=item.target_key,
        pm_review_request_id=item.pm_review_request_id,
        source_episode_id=source_episode_id,
        pm_decision_ref=completion.pm_decision_ref,
    )


def _pm_review_failed_message(
    *,
    item: object,
    failure: PMReviewQueueFailure,
) -> PMReviewFailed:
    from .pm_review_queue import PMReviewWorkItem

    if not isinstance(item, PMReviewWorkItem):
        raise PMReviewWorkerError("item must be a PMReviewWorkItem.")
    return PMReviewFailed(
        **base_message_kwargs(
            message_id=f"pm-review-failed:{item.pm_review_request_id}",
            event_time=item.event_time,
            recorded_at=failure.recorded_at,
            correlation_id=item.correlation_id,
            causation_id=item.work_item_id,
            idempotency_key=f"pm_review_failed:{item.idempotency_key}",
        ),
        target_key=item.target_key,
        pm_review_request_id=item.pm_review_request_id,
        failure_record_ref=failure.failure_record_ref,
    )


__all__ = [
    "NautilusPMReviewWorker",
    "PMReviewExecutor",
    "PMReviewWorkerError",
    "PMReviewWorkerProcessResult",
    "RuntimePMReviewExecutionResult",
]
