"""Composition-ready runtime graph for checker and CEAU side streams."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from nautilus_trader.common.component import MessageBus

from event_trader.config import RuntimeWorkersConfig
from event_trader.reflection import FileBackedReflectionObligationStore
from event_trader.reflection.review_loop import ReflectionHeartbeatReceipt

from .actors import (
    AnalysisAgentActor,
    CEAUEngineActor,
    CheckerAgentActor,
    EvidenceAdmissionEngineActor,
    PMReviewAgentActor,
    PMReviewEngineActor,
    PMReviewReflectionActor,
)
from .analysis_outbox import (
    AnalysisCompletionOutboxEntry,
    FileBackedAnalysisCompletionOutbox,
)
from .analysis_queue import FileBackedAnalysisWorkQueue
from .analysis_resolver import RuntimeAnalysisRequestResolver
from .analysis_worker import (
    AnalysisExecutor,
    AnalysisWorkerProcessResult,
    NautilusAnalysisWorker,
)
from .checker_worker import (
    CheckerDecisionRunner,
    CheckerWorkerProcessResult,
    NautilusCheckerWorker,
)
from .contracts import (
    AnalysisFailed,
    AnalysisOutcome,
    AnalysisRequested,
    CheckerDecision,
    CheckerFailed,
    EvidenceAdmitted,
    NewsRaw,
    PMReviewCompleted,
    PMReviewFailed,
    PMReviewRequested,
    WebResultRaw,
)
from .nautilus import EventTraderNautilusNode
from .outbox import (
    CheckerCompletionOutboxEntry,
    FileBackedCheckerCompletionOutbox,
)
from .pm_review_outbox import (
    FileBackedPMReviewCompletionOutbox,
    PMReviewCompletionOutboxEntry,
)
from .pm_review_queue import FileBackedPMReviewWorkQueue
from .pm_review_resolver import RuntimePMReviewRequestResolver
from .pm_review_worker import (
    NautilusPMReviewWorker,
    PMReviewExecutor,
    PMReviewWorkerProcessResult,
)
from .ports import CEAUEnginePort, EvidenceAdmissionPort
from .queue import FileBackedCheckerWorkQueue
from .reflection_outbox import (
    FileBackedReflectionCompletionOutbox,
    ReflectionCompletionOutboxEntry,
)
from .reflection_queue import FileBackedReflectionWorkQueue, ReflectionQueueEnqueueReceipt
from .reflection_worker import NautilusReflectionWorker, ReflectionWorkerProcessResult
from .slow_work_executor import SlowWorkExecutor
from .topics import (
    analysis_outcome_wildcard,
    analysis_requested_wildcard,
    checker_decision_wildcard,
    evidence_admitted_wildcard,
    news_raw_wildcard,
    pm_review_completed_wildcard,
    pm_review_requested_wildcard,
    web_result_raw_wildcard,
)

type RuntimePublisher = Callable[[str, object, bool], None]
type CheckerCompletionPumpStatus = Literal["no_work", "published"]
type AnalysisCompletionPumpStatus = Literal["no_work", "published"]
type PMReviewCompletionPumpStatus = Literal["no_work", "published"]
type ReflectionCompletionPumpStatus = Literal["completed", "failed"]
type ReflectionCycleExecutor = Callable[[int, datetime], ReflectionHeartbeatReceipt]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RuntimeGraphError(ValueError):
    """Raised when runtime graph wiring is invalid."""


@dataclass(frozen=True, slots=True)
class ReflectionRuntimeWiring:
    """Reflection execution owned by the runtime graph."""

    queue: FileBackedReflectionWorkQueue
    outbox: FileBackedReflectionCompletionOutbox
    execute_cycle: ReflectionCycleExecutor

    def __post_init__(self) -> None:
        if not isinstance(self.queue, FileBackedReflectionWorkQueue):
            raise RuntimeGraphError(
                "reflection.queue must be a FileBackedReflectionWorkQueue."
            )
        if not isinstance(self.outbox, FileBackedReflectionCompletionOutbox):
            raise RuntimeGraphError(
                "reflection.outbox must be a FileBackedReflectionCompletionOutbox."
            )
        if not callable(self.execute_cycle):
            raise RuntimeGraphError("reflection.execute_cycle must be callable.")


@dataclass(frozen=True, slots=True)
class PMReviewRuntimeWiring:
    """Explicit PMReview side-stream wiring owned by the runtime graph."""

    queue: FileBackedPMReviewWorkQueue
    outbox: FileBackedPMReviewCompletionOutbox
    resolver: RuntimePMReviewRequestResolver
    execute: PMReviewExecutor

    def __post_init__(self) -> None:
        if not isinstance(self.queue, FileBackedPMReviewWorkQueue):
            raise RuntimeGraphError("pm_review.queue must be a FileBackedPMReviewWorkQueue.")
        if not isinstance(self.outbox, FileBackedPMReviewCompletionOutbox):
            raise RuntimeGraphError(
                "pm_review.outbox must be a FileBackedPMReviewCompletionOutbox."
            )
        if not isinstance(self.resolver, RuntimePMReviewRequestResolver):
            raise RuntimeGraphError(
                "pm_review.resolver must be a RuntimePMReviewRequestResolver."
            )
        if not callable(self.execute):
            raise RuntimeGraphError("pm_review.execute must be callable.")


@dataclass(frozen=True, slots=True)
class CheckerCompletionPumpResult:
    """Observable result from one runtime-owned outbox publish attempt."""

    status: CheckerCompletionPumpStatus
    entry: CheckerCompletionOutboxEntry | None = None
    published_message: CheckerDecision | CheckerFailed | None = None

    def __post_init__(self) -> None:
        if self.status not in {"no_work", "published"}:
            raise RuntimeGraphError("status must be no_work or published.")
        if self.status == "no_work":
            if self.entry is not None or self.published_message is not None:
                raise RuntimeGraphError("no_work pump result must not carry state.")
            return
        if not isinstance(self.entry, CheckerCompletionOutboxEntry):
            raise RuntimeGraphError("published pump result requires an outbox entry.")
        if self.published_message is not self.entry.message:
            raise RuntimeGraphError("published_message must match the outbox entry message.")


@dataclass(frozen=True, slots=True)
class AnalysisCompletionPumpResult:
    """Observable result from one runtime-owned analysis outbox publish attempt."""

    status: AnalysisCompletionPumpStatus
    entry: AnalysisCompletionOutboxEntry | None = None
    published_message: AnalysisOutcome | AnalysisFailed | None = None

    def __post_init__(self) -> None:
        if self.status not in {"no_work", "published"}:
            raise RuntimeGraphError("status must be no_work or published.")
        if self.status == "no_work":
            if self.entry is not None or self.published_message is not None:
                raise RuntimeGraphError("no_work pump result must not carry state.")
            return
        if not isinstance(self.entry, AnalysisCompletionOutboxEntry):
            raise RuntimeGraphError("published pump result requires an outbox entry.")
        if self.published_message is not self.entry.message:
            raise RuntimeGraphError("published_message must match the outbox entry message.")


@dataclass(frozen=True, slots=True)
class PMReviewCompletionPumpResult:
    """Observable result from one runtime-owned PMReview outbox publish attempt."""

    status: PMReviewCompletionPumpStatus
    entry: PMReviewCompletionOutboxEntry | None = None
    published_message: PMReviewCompleted | PMReviewFailed | None = None

    def __post_init__(self) -> None:
        if self.status not in {"no_work", "published"}:
            raise RuntimeGraphError("status must be no_work or published.")
        if self.status == "no_work":
            if self.entry is not None or self.published_message is not None:
                raise RuntimeGraphError("no_work pump result must not carry state.")
            return
        if not isinstance(self.entry, PMReviewCompletionOutboxEntry):
            raise RuntimeGraphError("published pump result requires an outbox entry.")
        if self.published_message is not self.entry.message:
            raise RuntimeGraphError("published_message must match the outbox entry message.")


@dataclass(frozen=True, slots=True)
class ReflectionCompletionPumpResult:
    """Observable result from one reflection outbox visibility update."""

    status: ReflectionCompletionPumpStatus
    entry: ReflectionCompletionOutboxEntry
    visible_receipt: ReflectionHeartbeatReceipt | None = None

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed"}:
            raise RuntimeGraphError("status must be completed or failed.")
        if not isinstance(self.entry, ReflectionCompletionOutboxEntry):
            raise RuntimeGraphError("reflection pump result requires an outbox entry.")
        if self.status == "completed":
            if self.entry.entry_kind != "completed":
                raise RuntimeGraphError("completed pump result requires a completed entry.")
            if self.visible_receipt is not self.entry.receipt:
                raise RuntimeGraphError(
                    "visible_receipt must match the completed outbox entry receipt."
                )
            return
        if self.entry.entry_kind != "failed":
            raise RuntimeGraphError("failed pump result requires a failed entry.")
        if self.visible_receipt is not None:
            raise RuntimeGraphError("failed pump result must not carry visible_receipt.")


class EventTraderRuntimeGraph:
    """Own the checker worker and runtime-thread completion pump boundary."""

    def __init__(
        self,
        *,
        msgbus: MessageBus,
        checker_queue: FileBackedCheckerWorkQueue,
        checker_outbox: FileBackedCheckerCompletionOutbox,
        checker: CheckerDecisionRunner,
        analysis_queue: FileBackedAnalysisWorkQueue,
        analysis_outbox: FileBackedAnalysisCompletionOutbox,
        analysis_resolver: RuntimeAnalysisRequestResolver,
        analysis: AnalysisExecutor,
        pm_review: PMReviewRuntimeWiring | None,
        admission_port: EvidenceAdmissionPort | None,
        ceau_port: CEAUEnginePort,
        nautilus_node: EventTraderNautilusNode | None = None,
        reflection: ReflectionRuntimeWiring | None = None,
        reflection_obligation_store: FileBackedReflectionObligationStore | None = None,
        runtime_workers: RuntimeWorkersConfig | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(msgbus, MessageBus):
            raise RuntimeGraphError("msgbus must be a Nautilus MessageBus instance.")
        if not isinstance(checker_queue, FileBackedCheckerWorkQueue):
            raise RuntimeGraphError("checker_queue must be a FileBackedCheckerWorkQueue.")
        if not isinstance(checker_outbox, FileBackedCheckerCompletionOutbox):
            raise RuntimeGraphError(
                "checker_outbox must be a FileBackedCheckerCompletionOutbox."
            )
        if not isinstance(analysis_queue, FileBackedAnalysisWorkQueue):
            raise RuntimeGraphError("analysis_queue must be a FileBackedAnalysisWorkQueue.")
        if not isinstance(analysis_outbox, FileBackedAnalysisCompletionOutbox):
            raise RuntimeGraphError(
                "analysis_outbox must be a FileBackedAnalysisCompletionOutbox."
            )
        if not isinstance(analysis_resolver, RuntimeAnalysisRequestResolver):
            raise RuntimeGraphError(
                "analysis_resolver must be a RuntimeAnalysisRequestResolver."
            )
        if not callable(checker):
            raise RuntimeGraphError("checker must be callable.")
        if not callable(analysis):
            raise RuntimeGraphError("analysis must be callable.")
        if pm_review is not None and not isinstance(pm_review, PMReviewRuntimeWiring):
            raise RuntimeGraphError("pm_review must be a PMReviewRuntimeWiring or None.")
        if reflection is not None and not isinstance(reflection, ReflectionRuntimeWiring):
            raise RuntimeGraphError("reflection must be a ReflectionRuntimeWiring or None.")
        if reflection_obligation_store is not None and not isinstance(
            reflection_obligation_store,
            FileBackedReflectionObligationStore,
        ):
            raise RuntimeGraphError(
                "reflection_obligation_store must be a FileBackedReflectionObligationStore or None."
            )
        if runtime_workers is not None and not isinstance(
            runtime_workers,
            RuntimeWorkersConfig,
        ):
            raise RuntimeGraphError(
                "runtime_workers must be a RuntimeWorkersConfig or None."
            )
        if admission_port is not None and not (
            hasattr(admission_port, "admit_news_raw")
            and hasattr(admission_port, "admit_web_result_raw")
        ):
            raise RuntimeGraphError(
                "admission_port must provide raw news and web admission methods."
            )
        if nautilus_node is not None and not isinstance(nautilus_node, EventTraderNautilusNode):
            raise RuntimeGraphError("nautilus_node must be an EventTraderNautilusNode.")
        if nautilus_node is not None and nautilus_node.msgbus is not msgbus:
            raise RuntimeGraphError("nautilus_node.msgbus must be the runtime graph msgbus.")
        resolved_now = nautilus_node.clock.utc_now if nautilus_node is not None else now
        if resolved_now is None:
            resolved_now = _utc_now
        if not callable(resolved_now):
            raise RuntimeGraphError("now must be callable when provided.")
        resolved_runtime_workers = (
            RuntimeWorkersConfig(
                checker_concurrency=1,
                analysis_concurrency=1,
                pm_review_concurrency=1,
            )
            if runtime_workers is None
            else runtime_workers
        )
        self._now = resolved_now
        self._msgbus = msgbus
        self._checker_queue = checker_queue
        self._checker_outbox = checker_outbox
        self._analysis_queue = analysis_queue
        self._analysis_outbox = analysis_outbox
        self._pm_review_runtime = pm_review
        self._reflection_runtime = reflection
        self._pm_review_queue = None if pm_review is None else pm_review.queue
        self._pm_review_outbox = None if pm_review is None else pm_review.outbox
        self._reflection_queue = None if reflection is None else reflection.queue
        self._reflection_outbox = None if reflection is None else reflection.outbox
        self._pm_review_reflection_actor = (
            None
            if reflection_obligation_store is None
            else PMReviewReflectionActor(obligation_store=reflection_obligation_store)
        )
        self._nautilus_node = nautilus_node
        self._admission_actor = (
            None
            if admission_port is None
            else EvidenceAdmissionEngineActor(admission_port=admission_port)
        )
        self._checker_actor = CheckerAgentActor(queue_port=checker_queue, now=resolved_now)
        self._analysis_actor = AnalysisAgentActor(queue_port=analysis_queue, now=resolved_now)
        self._ceau_actor = CEAUEngineActor(ceau_port=ceau_port)
        self._checker_worker = NautilusCheckerWorker(
            queue=checker_queue,
            checker=checker,
            outbox=checker_outbox,
        )
        self._analysis_worker = NautilusAnalysisWorker(
            queue=analysis_queue,
            resolver=analysis_resolver,
            execute=analysis,
            outbox=analysis_outbox,
        )
        if pm_review is None:
            self._pm_review_engine_actor = None
            self._pm_review_actor = None
            self._pm_review_worker = None
        else:
            self._pm_review_engine_actor = PMReviewEngineActor(
                resolver=pm_review.resolver,
                now=resolved_now,
            )
            self._pm_review_actor = PMReviewAgentActor(
                queue_port=pm_review.queue,
                now=resolved_now,
            )
            self._pm_review_worker = NautilusPMReviewWorker(
                queue=pm_review.queue,
                resolver=pm_review.resolver,
                execute=pm_review.execute,
                outbox=pm_review.outbox,
            )
        if reflection is None:
            self._reflection_worker = None
        else:
            self._reflection_worker = NautilusReflectionWorker(
                queue=reflection.queue,
                execute=reflection.execute_cycle,
                outbox=reflection.outbox,
            )
        self._slow_work_executor = SlowWorkExecutor(
            checker_drain_one=lambda: self.drain_checker_work(limit=1),
            checker_pump=self.pump_checker_completions,
            checker_concurrency=resolved_runtime_workers.checker_concurrency,
            analysis_drain_one=lambda: self.drain_analysis_work(limit=1),
            analysis_pump=self.pump_analysis_completions,
            analysis_concurrency=resolved_runtime_workers.analysis_concurrency,
            pm_review_drain_one=lambda: self.drain_pm_review_work(limit=1),
            pm_review_pump=self.pump_pm_review_completions,
            pm_review_concurrency=resolved_runtime_workers.pm_review_concurrency,
        )
        self._reflection_cycle_count = 0
        self._last_reflection_receipt: ReflectionHeartbeatReceipt | None = None
        self._started = False
        self._closed = False
        self._manual_subscriptions: tuple[tuple[str, Callable[[object], None]], ...] = ()

    @property
    def msgbus(self) -> MessageBus:
        return self._msgbus

    @property
    def checker_queue(self) -> FileBackedCheckerWorkQueue:
        return self._checker_queue

    @property
    def checker_outbox(self) -> FileBackedCheckerCompletionOutbox:
        return self._checker_outbox

    @property
    def analysis_queue(self) -> FileBackedAnalysisWorkQueue:
        return self._analysis_queue

    @property
    def analysis_outbox(self) -> FileBackedAnalysisCompletionOutbox:
        return self._analysis_outbox

    @property
    def pm_review_queue(self) -> FileBackedPMReviewWorkQueue | None:
        return self._pm_review_queue

    @property
    def pm_review_outbox(self) -> FileBackedPMReviewCompletionOutbox | None:
        return self._pm_review_outbox

    @property
    def reflection_queue(self) -> FileBackedReflectionWorkQueue | None:
        return self._reflection_queue

    @property
    def reflection_outbox(self) -> FileBackedReflectionCompletionOutbox | None:
        return self._reflection_outbox

    @property
    def has_reflection_runtime(self) -> bool:
        return self._reflection_runtime is not None

    @property
    def reflection_cycle_count(self) -> int:
        return self._reflection_cycle_count

    @property
    def last_reflection_receipt(self) -> ReflectionHeartbeatReceipt | None:
        return self._last_reflection_receipt

    @property
    def checker_actor(self) -> CheckerAgentActor:
        return self._checker_actor

    @property
    def admission_actor(self) -> EvidenceAdmissionEngineActor | None:
        return self._admission_actor

    @property
    def analysis_actor(self) -> AnalysisAgentActor:
        return self._analysis_actor

    @property
    def pm_review_engine_actor(self) -> PMReviewEngineActor | None:
        return self._pm_review_engine_actor

    @property
    def pm_review_actor(self) -> PMReviewAgentActor | None:
        return self._pm_review_actor

    @property
    def ceau_actor(self) -> CEAUEngineActor:
        return self._ceau_actor

    @property
    def checker_worker(self) -> NautilusCheckerWorker:
        return self._checker_worker

    @property
    def analysis_worker(self) -> NautilusAnalysisWorker:
        return self._analysis_worker

    @property
    def pm_review_worker(self) -> NautilusPMReviewWorker | None:
        return self._pm_review_worker

    @property
    def reflection_worker(self) -> NautilusReflectionWorker | None:
        return self._reflection_worker

    def start(self) -> None:
        if self._closed:
            raise RuntimeGraphError("cannot start a closed runtime graph.")
        if self._started:
            return
        self._reclaim_in_flight_work()
        if self._nautilus_node is not None:
            if self._admission_actor is not None:
                self._nautilus_node.register_actor(self._admission_actor)
            self._nautilus_node.register_actor(self._checker_actor)
            self._nautilus_node.register_actor(self._analysis_actor)
            if self._pm_review_engine_actor is not None:
                self._nautilus_node.register_actor(self._pm_review_engine_actor)
            if self._pm_review_actor is not None:
                self._nautilus_node.register_actor(self._pm_review_actor)
            if self._pm_review_reflection_actor is not None:
                self._nautilus_node.register_actor(self._pm_review_reflection_actor)
            self._nautilus_node.register_actor(self._ceau_actor)
            self._nautilus_node.start()
        else:
            self._subscribe_manual()
        self._started = True

    def drain_checker_work(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[CheckerWorkerProcessResult, ...]:
        return self._checker_worker.drain(limit=limit)

    def pump_checker_completions(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[CheckerCompletionPumpResult, ...]:
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise RuntimeGraphError("limit must be a positive integer when provided.")
        results: list[CheckerCompletionPumpResult] = []
        for entry in self._checker_outbox.load_pending():
            if limit is not None and len(results) >= limit:
                break
            self._publish(entry.message, self._msgbus.publish)
            self._checker_outbox.mark_published(entry)
            results.append(
                CheckerCompletionPumpResult(
                    status="published",
                    entry=entry,
                    published_message=entry.message,
                )
            )
        return tuple(results)

    def drain_analysis_work(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[AnalysisWorkerProcessResult, ...]:
        return self._analysis_worker.drain(limit=limit)

    def pump_analysis_completions(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[AnalysisCompletionPumpResult, ...]:
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise RuntimeGraphError("limit must be a positive integer when provided.")
        results: list[AnalysisCompletionPumpResult] = []
        for entry in self._analysis_outbox.load_pending():
            if limit is not None and len(results) >= limit:
                break
            self._publish(entry.message, self._msgbus.publish)
            self._analysis_outbox.mark_published(entry)
            results.append(
                AnalysisCompletionPumpResult(
                    status="published",
                    entry=entry,
                    published_message=entry.message,
                )
            )
        return tuple(results)

    def drain_pm_review_work(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[PMReviewWorkerProcessResult, ...]:
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise RuntimeGraphError("limit must be a positive integer when provided.")
        if self._pm_review_worker is None:
            return ()
        return self._pm_review_worker.drain(limit=limit)

    def pump_pm_review_completions(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[PMReviewCompletionPumpResult, ...]:
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise RuntimeGraphError("limit must be a positive integer when provided.")
        if self._pm_review_outbox is None:
            return ()
        results: list[PMReviewCompletionPumpResult] = []
        for entry in self._pm_review_outbox.load_pending():
            if limit is not None and len(results) >= limit:
                break
            self._publish(entry.message, self._msgbus.publish)
            self._pm_review_outbox.mark_published(entry)
            results.append(
                PMReviewCompletionPumpResult(
                    status="published",
                    entry=entry,
                    published_message=entry.message,
                )
            )
        return tuple(results)

    def enqueue_reflection_cycle(self) -> ReflectionQueueEnqueueReceipt:
        if self._reflection_runtime is None or self._reflection_queue is None:
            raise RuntimeGraphError("reflection runtime is not installed.")
        item = self._build_reflection_work_item(
            cycle_number=self._reflection_cycle_count + 1,
            checked_at=self._now(),
        )
        return self._reflection_queue.enqueue(item)

    def drain_reflection_work(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[ReflectionWorkerProcessResult, ...]:
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise RuntimeGraphError("limit must be a positive integer when provided.")
        if self._reflection_worker is None:
            return ()
        return self._reflection_worker.drain(limit=limit)

    def pump_reflection_completions(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[ReflectionCompletionPumpResult, ...]:
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise RuntimeGraphError("limit must be a positive integer when provided.")
        if self._reflection_outbox is None:
            return ()
        results: list[ReflectionCompletionPumpResult] = []
        for entry in self._reflection_outbox.load_pending():
            if limit is not None and len(results) >= limit:
                break
            self._reflection_outbox.mark_published(entry)
            if entry.entry_kind == "completed":
                self._reflection_cycle_count = entry.cycle_number
                self._last_reflection_receipt = entry.receipt
                results.append(
                    ReflectionCompletionPumpResult(
                        status="completed",
                        entry=entry,
                        visible_receipt=entry.receipt,
                    )
                )
                continue
            results.append(ReflectionCompletionPumpResult(status="failed", entry=entry))
        return tuple(results)

    def run_reflection_cycle(self) -> ReflectionHeartbeatReceipt:
        enqueue_receipt = self.enqueue_reflection_cycle()
        if enqueue_receipt.status == "enqueued":
            self.drain_reflection_work(limit=1)
        pump_results = self.pump_reflection_completions(limit=1)
        if not pump_results:
            recovered_receipt = self._recover_published_reflection_receipt(
                enqueue_receipt.idempotency_key
            )
            if recovered_receipt is not None:
                return recovered_receipt
            raise RuntimeGraphError("reflection cycle did not produce a visible result.")
        result = pump_results[-1]
        if result.status == "failed":
            raise RuntimeGraphError(
                "reflection cycle failed: "
                f"{result.entry.failure_reason}: {result.entry.failure_error}"
            )
        if result.visible_receipt is None:
            raise RuntimeGraphError("reflection completion did not expose a receipt.")
        return result.visible_receipt

    def _recover_published_reflection_receipt(
        self,
        idempotency_key: str,
    ) -> ReflectionHeartbeatReceipt | None:
        if self._reflection_outbox is None:
            return None
        for entry in self._reflection_outbox.load_published():
            if entry.idempotency_key != idempotency_key:
                continue
            if entry.entry_kind == "failed":
                raise RuntimeGraphError(
                    "reflection cycle failed: "
                    f"{entry.failure_reason}: {entry.failure_error}"
                )
            if entry.receipt is None:
                raise RuntimeGraphError("reflection completion did not expose a receipt.")
            self._reflection_cycle_count = entry.cycle_number
            self._last_reflection_receipt = entry.receipt
            return entry.receipt
        return None

    def drain_all(self) -> None:
        """Run one composed drain/pump pass across checker, analysis, and PMReview."""
        self._slow_work_executor.drain_once()

    def close(self) -> None:
        if self._closed:
            return
        if self._nautilus_node is not None:
            self._nautilus_node.stop()
        else:
            for topic, handler in reversed(self._manual_subscriptions):
                self._msgbus.unsubscribe(topic, handler)
            self._manual_subscriptions = ()
        self._started = False
        self._closed = True

    def _reclaim_in_flight_work(self) -> None:
        self._checker_queue.reclaim_in_flight()
        self._analysis_queue.reclaim_in_flight()
        if self._pm_review_queue is not None:
            self._pm_review_queue.reclaim_in_flight()
        if self._reflection_queue is not None:
            self._reflection_queue.reclaim_in_flight()

    def _subscribe_manual(self) -> None:
        def on_evidence_for_checker(message: object) -> None:
            if not isinstance(message, EvidenceAdmitted):
                raise RuntimeGraphError("expected EvidenceAdmitted on admitted wildcard.")
            self._checker_actor.handle_evidence_admitted(message, self._msgbus.publish)

        def on_news_raw(message: object) -> None:
            if not isinstance(message, NewsRaw):
                raise RuntimeGraphError("expected NewsRaw on raw news wildcard.")
            if self._admission_actor is None:
                raise RuntimeGraphError("unexpected NewsRaw without admission wiring.")
            self._admission_actor.handle_news_raw(message, self._msgbus.publish)

        def on_web_result_raw(message: object) -> None:
            if not isinstance(message, WebResultRaw):
                raise RuntimeGraphError("expected WebResultRaw on raw web wildcard.")
            if self._admission_actor is None:
                raise RuntimeGraphError("unexpected WebResultRaw without admission wiring.")
            self._admission_actor.handle_web_result_raw(message, self._msgbus.publish)

        def on_evidence_for_ceau(message: object) -> None:
            if not isinstance(message, EvidenceAdmitted):
                raise RuntimeGraphError("expected EvidenceAdmitted on admitted wildcard.")
            self._ceau_actor.handle_evidence_admitted(message, self._msgbus.publish)

        def on_checker_decision(message: object) -> None:
            if not isinstance(message, CheckerDecision):
                raise RuntimeGraphError("expected CheckerDecision on checker wildcard.")
            self._ceau_actor.handle_checker_decision(message, self._msgbus.publish)

        def on_analysis_requested(message: object) -> None:
            if not isinstance(message, AnalysisRequested):
                raise RuntimeGraphError("expected AnalysisRequested on analysis wildcard.")
            self._analysis_actor.handle_analysis_requested(message, self._msgbus.publish)

        def on_analysis_outcome(message: object) -> None:
            if not isinstance(message, AnalysisOutcome):
                raise RuntimeGraphError("expected AnalysisOutcome on analysis outcome wildcard.")
            self._pm_review_engine_actor.handle_analysis_outcome(message, self._msgbus.publish)

        def on_pm_review_requested(message: object) -> None:
            if not isinstance(message, PMReviewRequested):
                raise RuntimeGraphError("expected PMReviewRequested on PMReview wildcard.")
            if self._pm_review_actor is None:
                raise RuntimeGraphError("unexpected PMReviewRequested without PMReview wiring.")
            self._pm_review_actor.handle_pm_review_requested(message, self._msgbus.publish)

        def on_pm_review_completed(message: object) -> None:
            if not isinstance(message, PMReviewCompleted):
                raise RuntimeGraphError("expected PMReviewCompleted on PMReview wildcard.")
            if self._pm_review_reflection_actor is None:
                raise RuntimeGraphError(
                    "unexpected PMReviewCompleted without reflection obligation wiring."
                )
            self._pm_review_reflection_actor.handle_pm_review_completed(message)

        subscriptions: list[tuple[str, Callable[[object], None]]] = []
        if self._admission_actor is not None:
            subscriptions.extend(
                (
                    (news_raw_wildcard(), on_news_raw),
                    (web_result_raw_wildcard(), on_web_result_raw),
                )
            )
        subscriptions.extend(
            (
                (evidence_admitted_wildcard(), on_evidence_for_checker),
                (evidence_admitted_wildcard(), on_evidence_for_ceau),
                (checker_decision_wildcard(), on_checker_decision),
                (analysis_requested_wildcard(), on_analysis_requested),
            )
        )
        if self._pm_review_engine_actor is not None:
            subscriptions.append((analysis_outcome_wildcard(), on_analysis_outcome))
        if self._pm_review_actor is not None:
            subscriptions.append((pm_review_requested_wildcard(), on_pm_review_requested))
        if self._pm_review_reflection_actor is not None:
            subscriptions.append((pm_review_completed_wildcard(), on_pm_review_completed))
        for topic, handler in subscriptions:
            self._msgbus.subscribe(topic, handler)
        self._manual_subscriptions = tuple(subscriptions)

    def _publish(
        self,
        message: (
            CheckerDecision
            | CheckerFailed
            | AnalysisOutcome
            | AnalysisFailed
            | PMReviewCompleted
            | PMReviewFailed
        ),
        publish: RuntimePublisher,
    ) -> None:
        publish(message.topic, message, False)

    def _build_reflection_work_item(self, *, cycle_number: int, checked_at: datetime):
        from .reflection_queue import ReflectionWorkItem

        return ReflectionWorkItem(
            work_item_id=f"reflection-work:{cycle_number}",
            cycle_number=cycle_number,
            checked_at=checked_at,
            idempotency_key=f"reflection:{cycle_number}:{checked_at.isoformat()}",
            enqueued_at=self._now(),
        )


__all__ = [
    "AnalysisCompletionPumpResult",
    "CheckerCompletionPumpResult",
    "EventTraderRuntimeGraph",
    "PMReviewCompletionPumpResult",
    "PMReviewRuntimeWiring",
    "ReflectionCompletionPumpResult",
    "ReflectionRuntimeWiring",
    "RuntimeGraphError",
]
