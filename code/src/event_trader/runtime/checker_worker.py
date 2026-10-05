"""Nonblocking checker worker for the Nautilus side stream."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from event_trader.contracts import CheckerDecision as ProjectCheckerDecision

from .contracts import (
    CheckerDecision,
    CheckerFailed,
    base_message_kwargs,
)
from .outbox import (
    CheckerCompletionOutboxEntry,
    FileBackedCheckerCompletionOutbox,
)
from .queue import (
    CheckerQueueCompletion,
    CheckerQueueFailure,
    CheckerWorkItem,
    FileBackedCheckerWorkQueue,
)

type CheckerWorkerStatus = Literal["no_work", "completed", "failed"]
type CheckerDecisionRunner = Callable[
    [CheckerWorkItem],
    ProjectCheckerDecision | "CheckerWorkerDecision",
]
type RuntimePublisher = Callable[[str, object, bool], None]


class CheckerWorkerError(ValueError):
    """Raised when checker worker dependencies or outputs are invalid."""


@dataclass(frozen=True, slots=True)
class CheckerWorkerDecision:
    """Checker result plus optional business receipt reference."""

    decision: ProjectCheckerDecision
    decision_record_ref: str | None = None
    validator_action: str = "accepted"
    decision_episode_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ProjectCheckerDecision):
            raise CheckerWorkerError("decision must be a CheckerDecision.")
        if self.decision_record_ref is not None:
            normalized = self.decision_record_ref.strip()
            if not normalized:
                raise CheckerWorkerError("decision_record_ref must not be blank.")
            object.__setattr__(self, "decision_record_ref", normalized)
        if not isinstance(self.validator_action, str) or not self.validator_action.strip():
            raise CheckerWorkerError("validator_action must not be blank.")
        object.__setattr__(self, "validator_action", self.validator_action.strip())
        if not isinstance(self.decision_episode_id, str):
            raise CheckerWorkerError("decision_episode_id must be a string.")
        object.__setattr__(self, "decision_episode_id", self.decision_episode_id.strip())


@dataclass(frozen=True, slots=True)
class CheckerWorkerProcessResult:
    """Observable result from one worker processing attempt."""

    status: CheckerWorkerStatus
    work_item_id: str | None = None
    completion: CheckerQueueCompletion | None = None
    failure: CheckerQueueFailure | None = None
    outbox_entry: CheckerCompletionOutboxEntry | None = None
    published_message: CheckerDecision | CheckerFailed | None = None

    def __post_init__(self) -> None:
        if self.status not in {"no_work", "completed", "failed"}:
            raise CheckerWorkerError("status must be no_work, completed, or failed.")
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
                raise CheckerWorkerError("no_work result must not carry work state.")
            return
        if not isinstance(self.work_item_id, str) or not self.work_item_id.strip():
            raise CheckerWorkerError("work_item_id must be present for work results.")
        if self.status == "completed":
            if not isinstance(self.completion, CheckerQueueCompletion):
                raise CheckerWorkerError("completed result requires completion.")
            if not isinstance(self.outbox_entry, CheckerCompletionOutboxEntry):
                raise CheckerWorkerError("completed result requires outbox_entry.")
            if self.outbox_entry.entry_kind != "decision":
                raise CheckerWorkerError("completed result requires decision outbox entry.")
            if self.published_message is not None and not isinstance(
                self.published_message,
                CheckerDecision,
            ):
                raise CheckerWorkerError(
                    "completed result published_message must be CheckerDecision."
                )
            if self.failure is not None:
                raise CheckerWorkerError("completed result must not carry failure.")
        if self.status == "failed":
            if not isinstance(self.failure, CheckerQueueFailure):
                raise CheckerWorkerError("failed result requires failure.")
            if not isinstance(self.outbox_entry, CheckerCompletionOutboxEntry):
                raise CheckerWorkerError("failed result requires outbox_entry.")
            if self.outbox_entry.entry_kind != "failed":
                raise CheckerWorkerError("failed result requires failed outbox entry.")
            if self.published_message is not None and not isinstance(
                self.published_message,
                CheckerFailed,
            ):
                raise CheckerWorkerError("failed result published_message must be CheckerFailed.")
            if self.completion is not None:
                raise CheckerWorkerError("failed result must not carry completion.")


class NautilusCheckerWorker:
    """Drain checker queue items outside Nautilus MessageBus callbacks."""

    def __init__(
        self,
        *,
        queue: FileBackedCheckerWorkQueue,
        checker: CheckerDecisionRunner,
        outbox: FileBackedCheckerCompletionOutbox,
        publish: RuntimePublisher | None = None,
    ) -> None:
        if not isinstance(queue, FileBackedCheckerWorkQueue):
            raise CheckerWorkerError("queue must be a FileBackedCheckerWorkQueue.")
        if not callable(checker):
            raise CheckerWorkerError("checker must be callable.")
        if not isinstance(outbox, FileBackedCheckerCompletionOutbox):
            raise CheckerWorkerError("outbox must be a FileBackedCheckerCompletionOutbox.")
        if publish is not None and not callable(publish):
            raise CheckerWorkerError("publish must be callable when provided.")
        self._queue = queue
        self._checker = checker
        self._outbox = outbox
        self._publish = publish

    def process_next(self) -> CheckerWorkerProcessResult:
        """Process at most one claimed checker work item."""
        claim = self._queue.claim_next()
        if claim is None:
            return CheckerWorkerProcessResult(status="no_work")
        try:
            checker_result = _normalize_checker_result(self._checker(claim.item))
            _validate_project_decision(claim.item, checker_result.decision)
            completion = self._queue.complete(
                claim,
                decision_payload=_project_decision_payload(checker_result),
                decision_record_ref=checker_result.decision_record_ref,
            )
            message = _checker_decision_message(
                item=claim.item,
                completion=completion,
            )
            outbox_entry = self._outbox.record_decision(
                work_item_id=claim.work_item_id,
                idempotency_key=claim.idempotency_key,
                message=message,
            )
            published_message = self._publish_if_configured(outbox_entry)
            return CheckerWorkerProcessResult(
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
            message = _checker_failed_message(item=claim.item, failure=failure)
            outbox_entry = self._outbox.record_failure(
                work_item_id=claim.work_item_id,
                idempotency_key=claim.idempotency_key,
                message=message,
            )
            published_message = self._publish_if_configured(outbox_entry)
            return CheckerWorkerProcessResult(
                status="failed",
                work_item_id=claim.work_item_id,
                failure=failure,
                outbox_entry=outbox_entry,
                published_message=published_message,
            )

    def drain(self, *, limit: int | None = None) -> tuple[CheckerWorkerProcessResult, ...]:
        """Process available work until the queue is empty or limit is reached."""
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise CheckerWorkerError("limit must be a positive integer when provided.")
        results: list[CheckerWorkerProcessResult] = []
        while limit is None or len(results) < limit:
            result = self.process_next()
            if result.status == "no_work":
                break
            results.append(result)
        return tuple(results)

    def _publish_if_configured(
        self,
        entry: CheckerCompletionOutboxEntry,
    ) -> CheckerDecision | CheckerFailed | None:
        if self._publish is None:
            return None
        self._publish(entry.message.topic, entry.message, False)
        self._outbox.mark_published(entry)
        return entry.message


def _normalize_checker_result(
    result: ProjectCheckerDecision | CheckerWorkerDecision,
) -> CheckerWorkerDecision:
    if isinstance(result, CheckerWorkerDecision):
        return result
    if isinstance(result, ProjectCheckerDecision):
        return CheckerWorkerDecision(decision=result)
    raise CheckerWorkerError("checker must return CheckerDecision or CheckerWorkerDecision.")


def _validate_project_decision(
    item: CheckerWorkItem,
    decision: ProjectCheckerDecision,
) -> None:
    if not isinstance(decision, ProjectCheckerDecision):
        raise CheckerWorkerError("checker must return a CheckerDecision.")
    if decision.target_key != item.target_key:
        raise CheckerWorkerError("checker decision target_key must match work item.")
    if decision.event_ids != [item.event_id]:
        raise CheckerWorkerError("checker decision event_ids must match work item.")


def _project_decision_payload(result: CheckerWorkerDecision) -> dict[str, object]:
    decision = result.decision
    return {
        "target_key": decision.target_key,
        "event_ids": list(decision.event_ids),
        "decision": decision.decision,
        "rationale": decision.rationale,
        "attention_hint": decision.attention_hint,
        "requires_watchlist_maintenance": decision.requires_watchlist_maintenance,
        "validator_action": result.validator_action,
        "decision_episode_id": result.decision_episode_id,
    }


def _checker_decision_message(
    *,
    item: CheckerWorkItem,
    completion: CheckerQueueCompletion,
) -> CheckerDecision:
    return CheckerDecision(
        **base_message_kwargs(
            message_id=f"checker-decision:{item.event_id}",
            event_time=item.event_time,
            recorded_at=completion.completed_at,
            correlation_id=item.correlation_id,
            causation_id=item.work_item_id,
            idempotency_key=f"checker_decision:{item.idempotency_key}",
        ),
        target_key=item.target_key,
        event_id=item.event_id,
        evidence_record_ref=item.evidence_record_ref,
        decision_record_ref=completion.decision_record_ref,
    )


def _checker_failed_message(
    *,
    item: CheckerWorkItem,
    failure: CheckerQueueFailure,
) -> CheckerFailed:
    return CheckerFailed(
        **base_message_kwargs(
            message_id=f"checker-failed:{item.event_id}",
            event_time=item.event_time,
            recorded_at=failure.recorded_at,
            correlation_id=item.correlation_id,
            causation_id=item.work_item_id,
            idempotency_key=f"checker_failed:{item.idempotency_key}",
        ),
        target_key=item.target_key,
        event_id=item.event_id,
        evidence_record_ref=item.evidence_record_ref,
        failure_record_ref=str(failure.failure_path.resolve(strict=False)),
    )


__all__ = [
    "CheckerDecisionRunner",
    "CheckerWorkerDecision",
    "CheckerWorkerError",
    "CheckerWorkerProcessResult",
    "NautilusCheckerWorker",
]
