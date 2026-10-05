"""Actor boundaries for the Nautilus-aligned runtime."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.identifiers import ComponentId

from event_trader.reflection import (
    FileBackedReflectionObligationStore,
    PMReviewCompletionObservation,
    ReflectionTriggerPolicy,
)

from .analysis_queue import AnalysisWorkItem
from .contracts import (
    AnalysisOutcome,
    AnalysisRequested,
    CheckerDecision,
    EvidenceAdmitted,
    NewsRaw,
    PMReviewCompleted,
    PMReviewRequested,
    PMReviewSkipped,
    RuntimeDeadLetter,
    WebResultRaw,
    base_message_kwargs,
)
from .pm_review_queue import PMReviewWorkItem
from .pm_review_resolver import RuntimePMReviewRequestResolver
from .ports import (
    AnalysisWorkQueuePort,
    CEAUEnginePort,
    CheckerWorkQueuePort,
    EvidenceAdmissionPort,
    PMReviewWorkQueuePort,
)
from .queue import CheckerWorkItem
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


class RuntimeActorConfigurationError(ValueError):
    """Raised when a runtime actor is misconfigured."""


type RuntimePublisher = Callable[[str, object, bool], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EvidenceAdmissionEngineActor(Actor):
    """Deterministic admission actor for news/web raw source messages."""

    def __init__(
        self,
        *,
        admission_port: EvidenceAdmissionPort,
        component_id: str = "EVIDENCE-ADMISSION",
    ) -> None:
        super().__init__(ActorConfig(component_id=ComponentId(component_id)))
        if not hasattr(admission_port, "admit_news_raw") or not hasattr(
            admission_port,
            "admit_web_result_raw",
        ):
            raise RuntimeActorConfigurationError(
                "admission_port must provide news and web admission methods."
            )
        self._admission_port = admission_port

    def on_start(self) -> None:
        self.msgbus.subscribe(news_raw_wildcard(), self._handle_news_raw)
        self.msgbus.subscribe(web_result_raw_wildcard(), self._handle_web_result_raw)

    def on_stop(self) -> None:
        self.msgbus.unsubscribe(news_raw_wildcard(), self._handle_news_raw)
        self.msgbus.unsubscribe(web_result_raw_wildcard(), self._handle_web_result_raw)

    def _handle_news_raw(self, message: NewsRaw) -> None:
        self.handle_news_raw(message, self.msgbus.publish)

    def handle_news_raw(self, message: NewsRaw, publish: RuntimePublisher) -> None:
        """Admit one raw news message and publish the typed admission outcome."""
        if not callable(publish):
            raise RuntimeActorConfigurationError("publish must be callable.")
        outcome = self._admission_port.admit_news_raw(message)
        publish(outcome.topic, outcome, False)

    def _handle_web_result_raw(self, message: WebResultRaw) -> None:
        self.handle_web_result_raw(message, self.msgbus.publish)

    def handle_web_result_raw(
        self,
        message: WebResultRaw,
        publish: RuntimePublisher,
    ) -> None:
        """Admit one raw web result message and publish the typed outcome."""
        if not callable(publish):
            raise RuntimeActorConfigurationError("publish must be callable.")
        outcome = self._admission_port.admit_web_result_raw(message)
        publish(outcome.topic, outcome, False)


class CheckerAgentActor(Actor):
    """Checker actor that only enqueues durable work from admitted evidence."""

    def __init__(
        self,
        *,
        queue_port: CheckerWorkQueuePort,
        component_id: str = "CHECKER-AGENT",
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__(ActorConfig(component_id=ComponentId(component_id)))
        if not hasattr(queue_port, "enqueue") or not hasattr(queue_port, "dead_letter"):
            raise RuntimeActorConfigurationError(
                "queue_port must provide enqueue and dead_letter methods."
            )
        if not callable(now):
            raise RuntimeActorConfigurationError("now must be callable.")
        self._queue_port = queue_port
        self._now = now

    def on_start(self) -> None:
        self.msgbus.subscribe(evidence_admitted_wildcard(), self._handle_evidence_admitted)

    def on_stop(self) -> None:
        self.msgbus.unsubscribe(evidence_admitted_wildcard(), self._handle_evidence_admitted)

    def _handle_evidence_admitted(self, message: EvidenceAdmitted) -> None:
        self.handle_evidence_admitted(message, self.msgbus.publish)

    def handle_evidence_admitted(
        self,
        message: EvidenceAdmitted,
        publish: RuntimePublisher,
    ) -> None:
        """Enqueue checker work from admitted evidence without running checker."""
        if not callable(publish):
            raise RuntimeActorConfigurationError("publish must be callable.")
        enqueued_at = self._now()
        work_item = CheckerWorkItem(
            work_item_id=f"checker-work:{message.event_id}",
            target_key=message.target_key,
            event_id=message.event_id,
            source_ref=message.source_ref,
            evidence_record_ref=message.evidence_record_ref,
            event_time=message.event_time,
            correlation_id=message.correlation_id,
            causation_id=message.message_id,
            idempotency_key=f"checker:{message.target_key}:{message.event_id}:v1",
            enqueued_at=enqueued_at,
        )
        try:
            self._queue_port.enqueue(work_item)
        except Exception as exc:
            dead_letter = self._queue_port.dead_letter(work_item, reason=type(exc).__name__)
            dead_letter_message = RuntimeDeadLetter(
                **base_message_kwargs(
                    message_id=f"runtime-dead-letter:{work_item.work_item_id}",
                    event_time=message.event_time,
                    recorded_at=dead_letter.recorded_at,
                    correlation_id=message.correlation_id,
                    causation_id=message.message_id,
                    idempotency_key=f"dead-letter:{work_item.idempotency_key}",
                ),
                component=str(self.id),
                failed_topic=message.topic,
                dead_letter_ref=str(dead_letter.dead_letter_path),
            )
            publish(
                dead_letter_message.topic,
                dead_letter_message,
                False,
            )


class AnalysisAgentActor(Actor):
    """Analysis actor that only enqueues durable work from AnalysisRequested."""

    def __init__(
        self,
        *,
        queue_port: AnalysisWorkQueuePort,
        component_id: str = "ANALYSIS-AGENT",
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__(ActorConfig(component_id=ComponentId(component_id)))
        if not hasattr(queue_port, "enqueue") or not hasattr(queue_port, "dead_letter"):
            raise RuntimeActorConfigurationError(
                "queue_port must provide enqueue and dead_letter methods."
            )
        if not callable(now):
            raise RuntimeActorConfigurationError("now must be callable.")
        self._queue_port = queue_port
        self._now = now

    def on_start(self) -> None:
        self.msgbus.subscribe(analysis_requested_wildcard(), self._handle_analysis_requested)

    def on_stop(self) -> None:
        self.msgbus.unsubscribe(analysis_requested_wildcard(), self._handle_analysis_requested)

    def _handle_analysis_requested(self, message: AnalysisRequested) -> None:
        self.handle_analysis_requested(message, self.msgbus.publish)

    def handle_analysis_requested(
        self,
        message: AnalysisRequested,
        publish: RuntimePublisher,
    ) -> None:
        """Enqueue analysis work from emitted-unit refs without running analysis."""
        if not callable(publish):
            raise RuntimeActorConfigurationError("publish must be callable.")
        enqueued_at = self._now()
        work_item = AnalysisWorkItem(
            work_item_id=f"analysis-work:{message.analysis_unit_id}:{message.record_id}",
            target_key=message.target_key,
            analysis_unit_id=message.analysis_unit_id,
            record_id=message.record_id,
            event_time=message.event_time,
            correlation_id=message.correlation_id,
            causation_id=message.message_id,
            idempotency_key=(
                f"analysis:{message.target_key}:{message.analysis_unit_id}:{message.record_id}:v1"
            ),
            enqueued_at=enqueued_at,
        )
        try:
            self._queue_port.enqueue(work_item)
        except Exception as exc:
            dead_letter = self._queue_port.dead_letter(work_item, reason=type(exc).__name__)
            dead_letter_message = RuntimeDeadLetter(
                **base_message_kwargs(
                    message_id=f"runtime-dead-letter:{work_item.work_item_id}",
                    event_time=message.event_time,
                    recorded_at=dead_letter.recorded_at,
                    correlation_id=message.correlation_id,
                    causation_id=message.message_id,
                    idempotency_key=f"dead-letter:{work_item.idempotency_key}",
                ),
                component=str(self.id),
                failed_topic=message.topic,
                dead_letter_ref=str(dead_letter.dead_letter_path),
            )
            publish(dead_letter_message.topic, dead_letter_message, False)


class PMReviewEngineActor(Actor):
    """Deterministic PMReview materialization actor for AnalysisOutcome messages."""

    def __init__(
        self,
        *,
        resolver: RuntimePMReviewRequestResolver,
        component_id: str = "PMREVIEW-ENGINE",
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__(ActorConfig(component_id=ComponentId(component_id)))
        if not isinstance(resolver, RuntimePMReviewRequestResolver):
            raise RuntimeActorConfigurationError(
                "resolver must be a RuntimePMReviewRequestResolver."
            )
        if not callable(now):
            raise RuntimeActorConfigurationError("now must be callable.")
        self._resolver = resolver
        self._now = now

    def on_start(self) -> None:
        self.msgbus.subscribe(analysis_outcome_wildcard(), self._handle_analysis_outcome)

    def on_stop(self) -> None:
        self.msgbus.unsubscribe(analysis_outcome_wildcard(), self._handle_analysis_outcome)

    def _handle_analysis_outcome(self, message: AnalysisOutcome) -> None:
        self.handle_analysis_outcome(message, self.msgbus.publish)

    def handle_analysis_outcome(
        self,
        message: AnalysisOutcome,
        publish: RuntimePublisher,
    ) -> None:
        """Derive deterministic PMReview routing from one analysis outcome receipt."""
        if not callable(publish):
            raise RuntimeActorConfigurationError("publish must be callable.")
        try:
            resolution = self._resolver.resolve_analysis_outcome(message)
        except Exception as exc:
            recorded_at = self._now()
            dead_letter_message = RuntimeDeadLetter(
                **base_message_kwargs(
                    message_id=(
                        "runtime-dead-letter:pm-review-engine:"
                        f"{message.analysis_unit_id}:{message.record_id}"
                    ),
                    event_time=message.event_time,
                    recorded_at=recorded_at,
                    correlation_id=message.correlation_id,
                    causation_id=message.message_id,
                    idempotency_key=(
                        "dead-letter:pm_review_engine:"
                        f"{message.target_key}:{message.analysis_unit_id}:{message.record_id}:"
                        f"{type(exc).__name__}"
                    ),
                ),
                component=str(self.id),
                failed_topic=message.topic,
                dead_letter_ref=message.outcome_record_ref,
            )
            publish(dead_letter_message.topic, dead_letter_message, False)
            return
        if resolution.status == "skipped":
            if resolution.skip_reason is None:
                raise RuntimeActorConfigurationError(
                    "skipped PMReview resolution must carry skip_reason."
                )
            recorded_at = self._now()
            skipped = PMReviewSkipped(
                **base_message_kwargs(
                    message_id=f"pm-review-skipped:{message.analysis_unit_id}:{message.record_id}",
                    event_time=message.event_time,
                    recorded_at=recorded_at,
                    correlation_id=message.correlation_id,
                    causation_id=message.message_id,
                    idempotency_key=(
                        "pm_review_skipped:"
                        f"{message.target_key}:{message.analysis_unit_id}:{message.record_id}:"
                        f"{resolution.skip_reason}"
                    ),
                ),
                target_key=message.target_key,
                pm_review_request_id=resolution.pm_review_request_id,
                pm_review_request_ref=resolution.pm_review_request_ref,
                skip_reason=resolution.skip_reason,
                source_analysis_unit_id=message.analysis_unit_id,
                source_record_id=message.record_id,
                source_outcome_record_ref=message.outcome_record_ref,
            )
            publish(skipped.topic, skipped, False)
            return
        if resolution.pm_review_request_id is None or resolution.pm_review_request_ref is None:
            raise RuntimeActorConfigurationError(
                "requested PMReview resolution must carry request id and ref."
            )
        recorded_at = self._now()
        requested = PMReviewRequested(
            **base_message_kwargs(
                message_id=f"pm-review-requested:{resolution.pm_review_request_id}",
                event_time=message.event_time,
                recorded_at=recorded_at,
                correlation_id=message.correlation_id,
                causation_id=message.message_id,
                idempotency_key=(
                    "pm_review_requested:"
                    f"{resolution.pm_review_request_id}:{message.analysis_unit_id}:{message.record_id}"
                ),
            ),
            target_key=message.target_key,
            pm_review_request_id=resolution.pm_review_request_id,
            pm_review_request_ref=resolution.pm_review_request_ref,
            source_analysis_unit_id=message.analysis_unit_id,
            source_record_id=message.record_id,
            source_outcome_record_ref=message.outcome_record_ref,
        )
        publish(requested.topic, requested, False)


class PMReviewAgentActor(Actor):
    """PMReview actor that only enqueues durable work from PMReviewRequested."""

    def __init__(
        self,
        *,
        queue_port: PMReviewWorkQueuePort,
        component_id: str = "PMREVIEW-AGENT",
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__(ActorConfig(component_id=ComponentId(component_id)))
        if not hasattr(queue_port, "enqueue") or not hasattr(queue_port, "dead_letter"):
            raise RuntimeActorConfigurationError(
                "queue_port must provide enqueue and dead_letter methods."
            )
        if not callable(now):
            raise RuntimeActorConfigurationError("now must be callable.")
        self._queue_port = queue_port
        self._now = now

    def on_start(self) -> None:
        self.msgbus.subscribe(pm_review_requested_wildcard(), self._handle_pm_review_requested)

    def on_stop(self) -> None:
        self.msgbus.unsubscribe(pm_review_requested_wildcard(), self._handle_pm_review_requested)

    def _handle_pm_review_requested(self, message: PMReviewRequested) -> None:
        self.handle_pm_review_requested(message, self.msgbus.publish)

    def handle_pm_review_requested(
        self,
        message: PMReviewRequested,
        publish: RuntimePublisher,
    ) -> None:
        """Enqueue PMReview work from persisted request refs without running PMReview."""
        if not callable(publish):
            raise RuntimeActorConfigurationError("publish must be callable.")
        enqueued_at = self._now()
        work_item = PMReviewWorkItem(
            work_item_id=f"pm-review-work:{message.pm_review_request_id}",
            target_key=message.target_key,
            pm_review_request_id=message.pm_review_request_id,
            pm_review_request_ref=message.pm_review_request_ref,
            event_time=message.event_time,
            correlation_id=message.correlation_id,
            causation_id=message.message_id,
            idempotency_key=(
                "pm_review:"
                f"{message.target_key}:{message.pm_review_request_id}:v2"
            ),
            enqueued_at=enqueued_at,
            source_analysis_unit_id=message.source_analysis_unit_id,
            source_record_id=message.source_record_id,
            source_outcome_record_ref=message.source_outcome_record_ref,
        )
        try:
            self._queue_port.enqueue(work_item)
        except Exception as exc:
            dead_letter = self._queue_port.dead_letter(work_item, reason=type(exc).__name__)
            dead_letter_message = RuntimeDeadLetter(
                **base_message_kwargs(
                    message_id=f"runtime-dead-letter:{work_item.work_item_id}",
                    event_time=message.event_time,
                    recorded_at=dead_letter.recorded_at,
                    correlation_id=message.correlation_id,
                    causation_id=message.message_id,
                    idempotency_key=f"dead-letter:{work_item.idempotency_key}",
                ),
                component=str(self.id),
                failed_topic=message.topic,
                dead_letter_ref=str(dead_letter.dead_letter_path),
            )
            publish(dead_letter_message.topic, dead_letter_message, False)


class PMReviewReflectionActor(Actor):
    """Append reflection obligations from PMReview completions without running reflection."""

    def __init__(
        self,
        *,
        obligation_store: FileBackedReflectionObligationStore,
        trigger_policy: ReflectionTriggerPolicy | None = None,
        component_id: str = "PMREVIEW-REFLECTION",
    ) -> None:
        super().__init__(ActorConfig(component_id=ComponentId(component_id)))
        if not isinstance(obligation_store, FileBackedReflectionObligationStore):
            raise RuntimeActorConfigurationError(
                "obligation_store must be a FileBackedReflectionObligationStore."
            )
        if trigger_policy is not None and not isinstance(
            trigger_policy,
            ReflectionTriggerPolicy,
        ):
            raise RuntimeActorConfigurationError(
                "trigger_policy must be a ReflectionTriggerPolicy when provided."
            )
        self._obligation_store = obligation_store
        self._trigger_policy = (
            ReflectionTriggerPolicy() if trigger_policy is None else trigger_policy
        )

    def on_start(self) -> None:
        self.msgbus.subscribe(
            pm_review_completed_wildcard(),
            self._handle_pm_review_completed,
        )

    def on_stop(self) -> None:
        self.msgbus.unsubscribe(
            pm_review_completed_wildcard(),
            self._handle_pm_review_completed,
        )

    def _handle_pm_review_completed(self, message: PMReviewCompleted) -> None:
        self.handle_pm_review_completed(message)

    def handle_pm_review_completed(self, message: PMReviewCompleted) -> None:
        if message.source_episode_id is None:
            return
        obligations = self._trigger_policy.evaluate(
            target_key=message.target_key,
            checked_at=message.recorded_at,
            pm_review_completions=(
                PMReviewCompletionObservation(
                    completion_id=message.message_id,
                    target_key=message.target_key,
                    episode_id=message.source_episode_id,
                    observed_at=message.recorded_at,
                    pm_review_request_id=message.pm_review_request_id,
                    pm_decision_ref=message.pm_decision_ref,
                ),
            ),
        )
        for obligation in obligations:
            self._obligation_store.append_obligation(obligation)


class CEAUEngineActor(Actor):
    """Central deterministic CEAU actor boundary."""

    def __init__(
        self,
        *,
        ceau_port: CEAUEnginePort,
        component_id: str = "CEAU-ENGINE",
    ) -> None:
        super().__init__(ActorConfig(component_id=ComponentId(component_id)))
        if not hasattr(ceau_port, "handle_evidence_admitted") or not hasattr(
            ceau_port,
            "handle_checker_decision",
        ):
            raise RuntimeActorConfigurationError(
                "ceau_port must provide evidence and checker transition methods."
            )
        self._ceau_port = ceau_port

    def on_start(self) -> None:
        self.msgbus.subscribe(evidence_admitted_wildcard(), self._handle_evidence_admitted)
        self.msgbus.subscribe(checker_decision_wildcard(), self._handle_checker_decision)

    def on_stop(self) -> None:
        self.msgbus.unsubscribe(evidence_admitted_wildcard(), self._handle_evidence_admitted)
        self.msgbus.unsubscribe(checker_decision_wildcard(), self._handle_checker_decision)

    def _handle_evidence_admitted(self, message: EvidenceAdmitted) -> None:
        self.handle_evidence_admitted(message, self.msgbus.publish)

    def handle_evidence_admitted(
        self,
        message: EvidenceAdmitted,
        publish: RuntimePublisher,
    ) -> None:
        """Record admitted evidence in the central CEAU boundary."""
        if not callable(publish):
            raise RuntimeActorConfigurationError("publish must be callable.")
        self._publish_effects(
            self._ceau_port.handle_evidence_admitted(message),
            publish,
        )

    def _handle_checker_decision(self, message: CheckerDecision) -> None:
        self.handle_checker_decision(message, self.msgbus.publish)

    def handle_checker_decision(
        self,
        message: CheckerDecision,
        publish: RuntimePublisher,
    ) -> None:
        """Route a checker decision through CEAU without running analysis."""
        if not callable(publish):
            raise RuntimeActorConfigurationError("publish must be callable.")
        self._publish_effects(
            self._ceau_port.handle_checker_decision(message),
            publish,
        )

    def _publish_effects(self, effect_set, publish: RuntimePublisher) -> None:
        for message in effect_set.lifecycle_messages:
            publish(message.topic, message, False)
        for message in effect_set.analysis_requests:
            publish(message.topic, message, False)


__all__ = [
    "AnalysisAgentActor",
    "CEAUEngineActor",
    "CheckerAgentActor",
    "EvidenceAdmissionEngineActor",
    "PMReviewAgentActor",
    "PMReviewEngineActor",
    "RuntimeActorConfigurationError",
]
