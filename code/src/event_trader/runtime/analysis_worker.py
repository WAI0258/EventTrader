"""Nonblocking analysis worker for the Nautilus side stream."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from event_trader.ceau.execution import CEAUAnalysisDispatchResult
from event_trader.contracts import AnalysisResult

from .analysis_outbox import (
    AnalysisCompletionOutboxEntry,
    FileBackedAnalysisCompletionOutbox,
)
from .analysis_queue import (
    AnalysisQueueCompletion,
    AnalysisQueueFailure,
    AnalysisWorkItem,
    FileBackedAnalysisWorkQueue,
)
from .analysis_resolver import RuntimeAnalysisRequestResolver, RuntimeAnalysisResolution
from .contracts import AnalysisFailed, AnalysisOutcome, base_message_kwargs

type AnalysisWorkerStatus = Literal["no_work", "completed", "failed"]
type AnalysisExecutor = Callable[
    [RuntimeAnalysisResolution],
    CEAUAnalysisDispatchResult,
]
type RuntimePublisher = Callable[[str, object, bool], None]


class AnalysisWorkerError(ValueError):
    """Raised when analysis worker dependencies or outputs are invalid."""


@dataclass(frozen=True, slots=True)
class AnalysisWorkerProcessResult:
    """Observable result from one worker processing attempt."""

    status: AnalysisWorkerStatus
    work_item_id: str | None = None
    completion: AnalysisQueueCompletion | None = None
    failure: AnalysisQueueFailure | None = None
    outbox_entry: AnalysisCompletionOutboxEntry | None = None
    published_message: AnalysisOutcome | AnalysisFailed | None = None

    def __post_init__(self) -> None:
        if self.status not in {"no_work", "completed", "failed"}:
            raise AnalysisWorkerError("status must be no_work, completed, or failed.")
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
                raise AnalysisWorkerError("no_work result must not carry work state.")
            return
        if not isinstance(self.work_item_id, str) or not self.work_item_id.strip():
            raise AnalysisWorkerError("work_item_id must be present for work results.")
        if self.status == "completed":
            if not isinstance(self.completion, AnalysisQueueCompletion):
                raise AnalysisWorkerError("completed result requires completion.")
            if not isinstance(self.outbox_entry, AnalysisCompletionOutboxEntry):
                raise AnalysisWorkerError("completed result requires outbox_entry.")
            if self.outbox_entry.entry_kind != "outcome":
                raise AnalysisWorkerError("completed result requires outcome outbox entry.")
            if self.published_message is not None and not isinstance(
                self.published_message,
                AnalysisOutcome,
            ):
                raise AnalysisWorkerError(
                    "completed result published_message must be AnalysisOutcome."
                )
            if self.failure is not None:
                raise AnalysisWorkerError("completed result must not carry failure.")
        if self.status == "failed":
            if not isinstance(self.failure, AnalysisQueueFailure):
                raise AnalysisWorkerError("failed result requires failure.")
            if not isinstance(self.outbox_entry, AnalysisCompletionOutboxEntry):
                raise AnalysisWorkerError("failed result requires outbox_entry.")
            if self.outbox_entry.entry_kind != "failed":
                raise AnalysisWorkerError("failed result requires failed outbox entry.")
            if self.published_message is not None and not isinstance(
                self.published_message,
                AnalysisFailed,
            ):
                raise AnalysisWorkerError("failed result published_message must be AnalysisFailed.")
            if self.completion is not None:
                raise AnalysisWorkerError("failed result must not carry completion.")


class NautilusAnalysisWorker:
    """Drain analysis queue items outside Nautilus MessageBus callbacks."""

    def __init__(
        self,
        *,
        queue: FileBackedAnalysisWorkQueue,
        resolver: RuntimeAnalysisRequestResolver,
        execute: AnalysisExecutor,
        outbox: FileBackedAnalysisCompletionOutbox,
        publish: RuntimePublisher | None = None,
    ) -> None:
        if not isinstance(queue, FileBackedAnalysisWorkQueue):
            raise AnalysisWorkerError("queue must be a FileBackedAnalysisWorkQueue.")
        if not isinstance(resolver, RuntimeAnalysisRequestResolver):
            raise AnalysisWorkerError("resolver must be a RuntimeAnalysisRequestResolver.")
        if not callable(execute):
            raise AnalysisWorkerError("execute must be callable.")
        if not isinstance(outbox, FileBackedAnalysisCompletionOutbox):
            raise AnalysisWorkerError(
                "outbox must be a FileBackedAnalysisCompletionOutbox."
            )
        if publish is not None and not callable(publish):
            raise AnalysisWorkerError("publish must be callable when provided.")
        self._queue = queue
        self._resolver = resolver
        self._execute = execute
        self._outbox = outbox
        self._publish = publish

    def process_next(self) -> AnalysisWorkerProcessResult:
        claim = self._queue.claim_next()
        if claim is None:
            return AnalysisWorkerProcessResult(status="no_work")
        try:
            resolution = self._resolver.resolve(claim.item)
            dispatch_result = _normalize_dispatch_result(self._execute(resolution))
            _validate_analysis_result(
                request=resolution.request,
                result=dispatch_result.analysis_result,
            )
            completion = self._queue.complete(
                claim,
                outcome_payload=_analysis_result_payload(dispatch_result.analysis_result),
                outcome_record_ref=_outcome_record_ref(dispatch_result),
            )
            message = _analysis_outcome_message(item=claim.item, completion=completion)
            outbox_entry = self._outbox.record_outcome(
                work_item_id=claim.work_item_id,
                idempotency_key=claim.idempotency_key,
                message=message,
            )
            published_message = self._publish_if_configured(outbox_entry)
            return AnalysisWorkerProcessResult(
                status="completed",
                work_item_id=claim.work_item_id,
                completion=completion,
                outbox_entry=outbox_entry,
                published_message=published_message,
            )
        except Exception as exc:
            failure = self._queue.fail(
                claim,
                reason=type(exc).__name__,
                error=str(exc),
            )
            message = _analysis_failed_message(item=claim.item, failure=failure)
            outbox_entry = self._outbox.record_failure(
                work_item_id=claim.work_item_id,
                idempotency_key=claim.idempotency_key,
                message=message,
            )
            published_message = self._publish_if_configured(outbox_entry)
            return AnalysisWorkerProcessResult(
                status="failed",
                work_item_id=claim.work_item_id,
                failure=failure,
                outbox_entry=outbox_entry,
                published_message=published_message,
            )

    def drain(self, *, limit: int | None = None) -> tuple[AnalysisWorkerProcessResult, ...]:
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise AnalysisWorkerError("limit must be a positive integer when provided.")
        results: list[AnalysisWorkerProcessResult] = []
        while limit is None or len(results) < limit:
            result = self.process_next()
            if result.status == "no_work":
                break
            results.append(result)
        return tuple(results)

    def _publish_if_configured(
        self,
        entry: AnalysisCompletionOutboxEntry,
    ) -> AnalysisOutcome | AnalysisFailed | None:
        if self._publish is None:
            return None
        self._publish(entry.message.topic, entry.message, False)
        self._outbox.mark_published(entry)
        return entry.message


def _normalize_dispatch_result(result: CEAUAnalysisDispatchResult) -> CEAUAnalysisDispatchResult:
    if not isinstance(result, CEAUAnalysisDispatchResult):
        raise AnalysisWorkerError("execute must return a CEAUAnalysisDispatchResult.")
    return result


def _validate_analysis_result(
    *,
    request,
    result: AnalysisResult,
) -> None:
    if not isinstance(result, AnalysisResult):
        raise AnalysisWorkerError("execute must return an AnalysisResult.")
    if result.target_key != request.target_key:
        raise AnalysisWorkerError("analysis result target_key must match request.")
    if result.event_ids != request.event_ids:
        raise AnalysisWorkerError("analysis result event_ids must match request.")


def _analysis_result_payload(result: AnalysisResult) -> dict[str, object]:
    return {
        "target_key": result.target_key,
        "event_ids": list(result.event_ids),
        "outcome": result.outcome,
        "used_lesson_ids": list(result.used_lesson_ids),
        "analysis_assessment": (
            None
            if result.analysis_assessment is None
            else result.analysis_assessment.to_json_payload()
        ),
    }


def _outcome_record_ref(result: CEAUAnalysisDispatchResult) -> str:
    pointer = result.outcome_receipt_ref
    return f"{pointer.analysis_outcome_path}#{pointer.analysis_outcome_record_id}"


def _analysis_outcome_message(
    *,
    item: AnalysisWorkItem,
    completion: AnalysisQueueCompletion,
) -> AnalysisOutcome:
    return AnalysisOutcome(
        **base_message_kwargs(
            message_id=f"analysis-outcome:{item.analysis_unit_id}:{item.record_id}",
            event_time=item.event_time,
            recorded_at=completion.completed_at,
            correlation_id=item.correlation_id,
            causation_id=item.work_item_id,
            idempotency_key=f"analysis_outcome:{item.idempotency_key}",
        ),
        target_key=item.target_key,
        analysis_unit_id=item.analysis_unit_id,
        record_id=item.record_id,
        outcome_record_ref=completion.outcome_record_ref,
    )


def _analysis_failed_message(
    *,
    item: AnalysisWorkItem,
    failure: AnalysisQueueFailure,
) -> AnalysisFailed:
    return AnalysisFailed(
        **base_message_kwargs(
            message_id=f"analysis-failed:{item.analysis_unit_id}:{item.record_id}",
            event_time=item.event_time,
            recorded_at=failure.recorded_at,
            correlation_id=item.correlation_id,
            causation_id=item.work_item_id,
            idempotency_key=f"analysis_failed:{item.idempotency_key}",
        ),
        target_key=item.target_key,
        analysis_unit_id=item.analysis_unit_id,
        record_id=item.record_id,
        failure_record_ref=failure.failure_record_ref,
    )


__all__ = [
    "AnalysisExecutor",
    "AnalysisWorkerError",
    "AnalysisWorkerProcessResult",
    "NautilusAnalysisWorker",
]
