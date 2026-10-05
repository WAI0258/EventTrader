"""Nautilus-backed post-admission evidence release path."""

from __future__ import annotations

from pathlib import Path

from nautilus_trader.common.component import MessageBus

from event_trader.contracts.evidence import EvidenceEvent, EvidenceLedgerRecord
from event_trader.contracts.ports import EvidenceLedgerPort
from event_trader.ingest.admission import AdmissionOutputs
from event_trader.runtime.contracts import EvidenceAdmitted, base_message_kwargs
from event_trader.runtime.release import (
    RuntimeReleaseError,
    RuntimeReleaseReceipt,
)
from event_trader.runtime.topics import evidence_admitted_wildcard


class NautilusAdmittedEvidencePath:
    """Append admitted evidence truth, then publish EvidenceAdmitted on Nautilus."""

    def __init__(
        self,
        *,
        ledger: EvidenceLedgerPort,
        msgbus: MessageBus,
        require_subscribers: bool = True,
    ) -> None:
        append = getattr(ledger, "append", None)
        if not callable(append):
            raise RuntimeReleaseError(
                "ledger must provide an append(record) -> event_id method."
            )
        path_for_record = getattr(ledger, "path_for_record", None)
        if not callable(path_for_record):
            raise RuntimeReleaseError(
                "ledger must provide path_for_record(record) for stable evidence refs."
            )
        if not isinstance(msgbus, MessageBus):
            raise RuntimeReleaseError("msgbus must be a Nautilus MessageBus instance.")
        if not isinstance(require_subscribers, bool):
            raise RuntimeReleaseError("require_subscribers must be a boolean.")
        self._ledger = ledger
        self._msgbus = msgbus
        self._require_subscribers = require_subscribers

    def release_admitted(self, outputs: AdmissionOutputs) -> RuntimeReleaseReceipt:
        """Append admitted evidence truth, then publish its typed runtime event."""
        if not isinstance(outputs, AdmissionOutputs):
            raise RuntimeReleaseError("outputs must be an AdmissionOutputs instance.")
        ledger_record = outputs.ledger_record
        runtime_event = outputs.runtime_event
        _validate_release_pair(ledger_record, runtime_event)

        evidence_record_ref = self._evidence_record_ref(ledger_record)
        message = _evidence_admitted_message(
            runtime_event=runtime_event,
            evidence_record_ref=evidence_record_ref,
        )
        delivered_count = _evidence_admitted_delivery_count(self._msgbus, message.topic)
        if self._require_subscribers and delivered_count < 1:
            raise RuntimeReleaseError(
                "Cannot release admitted evidence through Nautilus without a "
                f"subscriber for topic {message.topic!r}."
            )

        appended_event_id = self._ledger.append(ledger_record)
        if appended_event_id != ledger_record.event_id:
            raise RuntimeReleaseError(
                "ledger.append(record) must return the appended record event_id."
            )

        self._publish_evidence_admitted(message)
        return RuntimeReleaseReceipt(
            appended_event_id=appended_event_id,
            delivered_count=delivered_count,
        )

    def _evidence_record_ref(self, record: EvidenceLedgerRecord) -> str:
        path = self._ledger.path_for_record(record)
        if not isinstance(path, Path):
            raise RuntimeReleaseError(
                "ledger.path_for_record(record) must return a Path."
            )
        return f"{path.resolve(strict=False)}#{record.event_id}"

    def _publish_evidence_admitted(self, message: EvidenceAdmitted) -> None:
        self._msgbus.publish(message.topic, message, False)


def _evidence_admitted_message(
    *,
    runtime_event: EvidenceEvent,
    evidence_record_ref: str,
) -> EvidenceAdmitted:
    return EvidenceAdmitted(
        **base_message_kwargs(
            message_id=f"evidence-admitted:{runtime_event.event_id}",
            event_time=runtime_event.ts_event,
            recorded_at=runtime_event.ts_init,
            correlation_id=f"evidence:{runtime_event.event_id}",
            causation_id=None,
            idempotency_key=(
                f"evidence_admitted:{runtime_event.target_key}:"
                f"{runtime_event.event_id}:v1"
            ),
        ),
        target_key=runtime_event.target_key,
        event_id=runtime_event.event_id,
        source_ref=runtime_event.source_ref,
        evidence_record_ref=evidence_record_ref,
    )


def _evidence_admitted_delivery_count(msgbus: MessageBus, topic: str) -> int:
    return _subscription_delivery_count(msgbus, topic) + _exact_subscription_count(
        msgbus,
        evidence_admitted_wildcard(),
    )


def _subscription_delivery_count(msgbus: MessageBus, topic: str) -> int:
    try:
        subscriptions = msgbus.subscriptions(topic)
    except TypeError:
        return 1 if msgbus.has_subscribers(topic) else 0
    if subscriptions is None:
        return 0
    try:
        return len(subscriptions)
    except TypeError:
        return 1 if msgbus.has_subscribers(topic) else 0


def _exact_subscription_count(msgbus: MessageBus, topic: str) -> int:
    try:
        subscriptions = msgbus.subscriptions(topic)
    except TypeError:
        return 1 if msgbus.has_subscribers(topic) else 0
    if subscriptions is None:
        return 0
    count = 0
    for subscription in subscriptions:
        if str(getattr(subscription, "topic", "")) == topic:
            count += 1
    return count


def _validate_release_pair(
    ledger_record: EvidenceLedgerRecord,
    runtime_event: EvidenceEvent,
) -> None:
    if not isinstance(ledger_record, EvidenceLedgerRecord):
        raise RuntimeReleaseError(
            "ledger_record must be an EvidenceLedgerRecord instance."
        )
    if not isinstance(runtime_event, EvidenceEvent):
        raise RuntimeReleaseError("runtime_event must be an EvidenceEvent instance.")

    mismatched_fields = [
        field_name
        for field_name in (
            "event_id",
            "target_key",
            "source_ref",
            "ts_source",
            "ts_event",
            "ts_init",
        )
        if getattr(ledger_record, field_name) != getattr(runtime_event, field_name)
    ]
    if mismatched_fields:
        mismatched = ", ".join(mismatched_fields)
        raise RuntimeReleaseError(
            "ledger_record and runtime_event must match across: "
            f"{mismatched}."
        )


__all__ = ["NautilusAdmittedEvidencePath"]
