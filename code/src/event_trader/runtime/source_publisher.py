"""Raw source publishers for the Nautilus-backed runtime graph."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from nautilus_trader.common.component import MessageBus

from .contracts import (
    EvidenceAdmitted,
    EvidenceQuarantined,
    NewsRaw,
    WebResultRaw,
    base_message_kwargs,
)
from .topics import evidence_admitted_topic, evidence_quarantined_topic


class RuntimeRawSourcePublisherError(ValueError):
    """Raised when raw source publishing is misconfigured."""


@dataclass(frozen=True, slots=True)
class RuntimeRawSourcePublishReceipt:
    """Observable receipt for one raw source message published to Nautilus."""

    target_key: str
    source_ref: str
    record_id: str
    topic: str
    message_id: str
    admitted_event_ids: tuple[str, ...] = ()
    quarantined: bool = False


class RuntimeRawSourcePublisher:
    """Publish raw source refs onto the Nautilus MessageBus."""

    def __init__(
        self,
        *,
        msgbus: MessageBus,
        drain_runtime_graph: Callable[[], None],
    ) -> None:
        if not isinstance(msgbus, MessageBus):
            raise RuntimeRawSourcePublisherError(
                "msgbus must be a Nautilus MessageBus instance."
            )
        if not callable(drain_runtime_graph):
            raise RuntimeRawSourcePublisherError("drain_runtime_graph must be callable.")
        self._msgbus = msgbus
        self._drain_runtime_graph = drain_runtime_graph

    def publish_news_raw(
        self,
        *,
        target_key: str,
        source_ref: str,
        record_id: str,
        event_time: datetime,
        recorded_at: datetime,
    ) -> RuntimeRawSourcePublishReceipt:
        message = NewsRaw(
            **base_message_kwargs(
                message_id=f"news-raw:{target_key}:{record_id}",
                event_time=event_time,
                recorded_at=recorded_at,
                correlation_id=f"raw-source:{target_key}:{record_id}",
                causation_id=None,
                idempotency_key=f"news_raw:{target_key}:{record_id}:v1",
            ),
            target_key=target_key,
            source_ref=source_ref,
            record_id=record_id,
        )
        return self._publish(message)

    def publish_web_result_raw(
        self,
        *,
        target_key: str,
        source_ref: str,
        record_id: str,
        event_time: datetime,
        recorded_at: datetime,
    ) -> RuntimeRawSourcePublishReceipt:
        message = WebResultRaw(
            **base_message_kwargs(
                message_id=f"web-result-raw:{target_key}:{record_id}",
                event_time=event_time,
                recorded_at=recorded_at,
                correlation_id=f"raw-source:{target_key}:{record_id}",
                causation_id=None,
                idempotency_key=f"web_result_raw:{target_key}:{record_id}:v1",
            ),
            target_key=target_key,
            source_ref=source_ref,
            record_id=record_id,
        )
        return self._publish(message)

    def _publish(
        self,
        message: NewsRaw | WebResultRaw,
    ) -> RuntimeRawSourcePublishReceipt:
        admitted_event_ids: list[str] = []
        quarantined = False

        def on_admitted(event: EvidenceAdmitted) -> None:
            if event.correlation_id == message.correlation_id:
                admitted_event_ids.append(event.event_id)

        def on_quarantined(event: EvidenceQuarantined) -> None:
            nonlocal quarantined
            if event.correlation_id == message.correlation_id:
                quarantined = True

        admitted_topic = evidence_admitted_topic(message.target_key)
        quarantined_topic = evidence_quarantined_topic(message.target_key)
        self._msgbus.subscribe(admitted_topic, on_admitted)
        self._msgbus.subscribe(quarantined_topic, on_quarantined)
        try:
            self._msgbus.publish(message.topic, message, False)
            self._drain_runtime_graph()
        finally:
            self._msgbus.unsubscribe(admitted_topic, on_admitted)
            self._msgbus.unsubscribe(quarantined_topic, on_quarantined)
        return RuntimeRawSourcePublishReceipt(
            target_key=message.target_key,
            source_ref=message.source_ref,
            record_id=message.record_id,
            topic=message.topic,
            message_id=message.message_id,
            admitted_event_ids=tuple(admitted_event_ids),
            quarantined=quarantined,
        )


__all__ = [
    "RuntimeRawSourcePublishReceipt",
    "RuntimeRawSourcePublisher",
    "RuntimeRawSourcePublisherError",
]
