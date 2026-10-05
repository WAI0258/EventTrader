"""Runtime CEAU adapter for Nautilus checker-decision events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from event_trader.ceau.contracts import (
    CEAUEventRouteRecord,
    CEAUUnitEmittedRecord,
)
from event_trader.ceau.live import LiveCEAUCoordinator
from event_trader.ceau.replay import ReplayCEAUCoordinator
from event_trader.ceau.routing import StreamRoutingEvent
from event_trader.contracts import EvidenceLedgerRecord
from event_trader.contracts.evidence_review import classify_evidence_review_dimensions
from event_trader.contracts.ports import EvidenceLedgerPort
from event_trader.source_policy import SourcePolicyError, extract_event_type, extract_source_kind

from .contracts import (
    AnalysisRequested,
    CEAUEventRouted,
    CEAUUnitEmitted,
    CheckerDecision,
    EvidenceAdmitted,
    base_message_kwargs,
)
from .ports import CEAUEngineEffectSet

_POSITION_RISK_EVENT_TYPES = frozenset(
    {
        "flow_positioning",
        "liquidity_funding",
        "market_structure",
        "volatility_risk",
    }
)

type RuntimeCEAUMode = Literal["replay", "live"]


class RuntimeCEAUError(ValueError):
    """Raised when runtime CEAU routing cannot proceed."""


@dataclass(frozen=True, slots=True)
class RuntimeCheckerDecisionReceipt:
    """Small checker receipt recovered from a durable decision reference."""

    decision: str
    validator_action: str
    attention_hint: str | None = None
    requires_watchlist_maintenance: bool = False
    decision_episode_id: str = ""


class RuntimeCheckerDecisionReceiptReader:
    """Read checker receipts from worker completions or checker receipt JSONL refs."""

    def load(
        self,
        *,
        decision_record_ref: str,
        target_key: str,
        event_id: str,
    ) -> RuntimeCheckerDecisionReceipt:
        path = _path_from_record_ref(decision_record_ref)
        if not path.exists():
            raise RuntimeCEAUError(f"checker decision ref does not exist: {path}")
        if path.suffix == ".json":
            return _load_worker_completion(path=path, target_key=target_key, event_id=event_id)
        if path.suffix == ".jsonl":
            return _load_checker_receipt_jsonl(
                path=path,
                target_key=target_key,
                event_id=event_id,
            )
        raise RuntimeCEAUError(f"unsupported checker decision ref path: {path}")


class RuntimeCEAUEngineAdapter:
    """Route checker decisions through the project CEAU coordinator."""

    def __init__(
        self,
        *,
        ledger: EvidenceLedgerPort,
        mode: RuntimeCEAUMode,
        replay_coordinator: ReplayCEAUCoordinator | None = None,
        live_coordinator: LiveCEAUCoordinator | None = None,
        receipt_reader: RuntimeCheckerDecisionReceiptReader | None = None,
    ) -> None:
        read = getattr(ledger, "read", None)
        if not callable(read):
            raise RuntimeCEAUError("ledger must provide read(event_id).")
        if mode not in {"replay", "live"}:
            raise RuntimeCEAUError("mode must be replay or live.")
        if mode == "replay" and not isinstance(replay_coordinator, ReplayCEAUCoordinator):
            raise RuntimeCEAUError("replay mode requires a ReplayCEAUCoordinator.")
        if mode == "live" and not isinstance(live_coordinator, LiveCEAUCoordinator):
            raise RuntimeCEAUError("live mode requires a LiveCEAUCoordinator.")
        self._ledger = ledger
        self._mode = mode
        self._replay_coordinator = replay_coordinator
        self._live_coordinator = live_coordinator
        self._receipt_reader = receipt_reader or RuntimeCheckerDecisionReceiptReader()
        self._pending_evidence_by_event_id: dict[str, EvidenceAdmitted] = {}

    def handle_evidence_admitted(self, message: EvidenceAdmitted) -> CEAUEngineEffectSet:
        if not isinstance(message, EvidenceAdmitted):
            raise RuntimeCEAUError("message must be an EvidenceAdmitted instance.")
        self._pending_evidence_by_event_id[message.event_id] = message
        return CEAUEngineEffectSet()

    def handle_checker_decision(self, message: CheckerDecision) -> CEAUEngineEffectSet:
        if not isinstance(message, CheckerDecision):
            raise RuntimeCEAUError("message must be a CheckerDecision instance.")
        evidence = self._ledger.read(message.event_id)
        if not isinstance(evidence, EvidenceLedgerRecord):
            raise RuntimeCEAUError("ledger.read(event_id) must return EvidenceLedgerRecord.")
        receipt = self._receipt_reader.load(
            decision_record_ref=message.decision_record_ref,
            target_key=message.target_key,
            event_id=message.event_id,
        )
        if receipt.validator_action == "operational_no_action":
            return CEAUEngineEffectSet()
        route_input = _build_stream_routing_event(
            evidence=evidence,
            checker_decision=receipt.decision,
            validator_action=receipt.validator_action,
        )
        route_result = self._route(route_input)
        lifecycle_messages: list[CEAUEventRouted | CEAUUnitEmitted] = [
            _event_routed_message(
                record=route_result.decision.event_route_record,
                checker_message=message,
            )
        ]
        analysis_requests: list[AnalysisRequested] = []
        current_emitted = route_result.decision.unit_emitted_record
        emitted_records = (
            *route_result.preclosed_emitted_records,
            *((current_emitted,) if current_emitted is not None else ()),
        )
        for emitted_record in emitted_records:
            lifecycle_messages.append(
                _unit_emitted_message(
                    record=emitted_record,
                    checker_message=message,
                )
            )
            analysis_requests.append(
                _analysis_requested_message(
                    record=emitted_record,
                    checker_message=message,
                )
            )
        return CEAUEngineEffectSet(
            lifecycle_messages=tuple(lifecycle_messages),
            analysis_requests=tuple(analysis_requests),
        )

    def _route(self, route_input: StreamRoutingEvent):
        if self._mode == "replay":
            if self._replay_coordinator is None:
                raise RuntimeCEAUError("replay coordinator is not configured.")
            return self._replay_coordinator.route_replay_event_with_preclose(route_input)
        if self._live_coordinator is None:
            raise RuntimeCEAUError("live coordinator is not configured.")
        return self._live_coordinator.route_live_event(route_input)


def _build_stream_routing_event(
    *,
    evidence: EvidenceLedgerRecord,
    checker_decision: str,
    validator_action: str,
) -> StreamRoutingEvent:
    labels = list(evidence.labels)
    try:
        event_type = extract_event_type(labels)
        source_kind = extract_source_kind(labels)
    except SourcePolicyError as exc:
        raise RuntimeCEAUError("CEAU stream routing requires source policy labels.") from exc

    review_dimensions = classify_evidence_review_dimensions(evidence)
    evidence_role = _stream_routing_evidence_role(
        source_kind=source_kind,
        event_type=event_type,
        review_evidence_role=review_dimensions.evidence_role,
    )
    reason_codes = (
        f"checker_{checker_decision}",
        f"validator_{validator_action}",
        f"evidence_role_{review_dimensions.evidence_role}",
        f"stream_evidence_role_{evidence_role}",
        f"source_kind_{source_kind}",
        f"event_type_{event_type}",
    )
    return StreamRoutingEvent(
        event_id=evidence.event_id,
        target_key=evidence.target_key,
        business_at=evidence.ts_event,
        recorded_at=evidence.ts_init,
        event_type=event_type,
        source_kind=source_kind,
        event_family=f"{event_type}:{source_kind}:{review_dimensions.evidence_role}",
        evidence_role=evidence_role,
        reason_codes=reason_codes,
        content_char_count=len(evidence.title) + len(evidence.content),
    )


def _stream_routing_evidence_role(
    *,
    source_kind: str,
    event_type: str,
    review_evidence_role: str,
) -> str:
    if source_kind == "operator_brief":
        return "operator_interrupt"
    if event_type in _POSITION_RISK_EVENT_TYPES:
        return "risk_interrupt"
    if review_evidence_role == "duplicate":
        return "episode_append"
    return review_evidence_role


def _event_routed_message(
    *,
    record: CEAUEventRouteRecord,
    checker_message: CheckerDecision,
) -> CEAUEventRouted:
    return CEAUEventRouted(
        **base_message_kwargs(
            message_id=f"ceau-event-routed:{record.record_id}",
            event_time=record.recorded_at,
            recorded_at=record.recorded_at,
            correlation_id=checker_message.correlation_id,
            causation_id=checker_message.message_id,
            idempotency_key=f"ceau_event_routed:{record.record_id}",
        ),
        target_key=record.target_key,
        event_id=record.event_id,
        evidence_record_ref=checker_message.evidence_record_ref,
        record_id=record.record_id,
        analysis_unit_id=record.analysis_unit_id,
    )


def _unit_emitted_message(
    *,
    record: CEAUUnitEmittedRecord,
    checker_message: CheckerDecision,
) -> CEAUUnitEmitted:
    return CEAUUnitEmitted(
        **base_message_kwargs(
            message_id=f"ceau-unit-emitted:{record.record_id}",
            event_time=record.recorded_at,
            recorded_at=record.recorded_at,
            correlation_id=checker_message.correlation_id,
            causation_id=checker_message.message_id,
            idempotency_key=f"ceau_unit_emitted:{record.record_id}",
        ),
        target_key=record.target_key,
        analysis_unit_id=record.analysis_unit_id,
        record_id=record.record_id,
    )


def _analysis_requested_message(
    *,
    record: CEAUUnitEmittedRecord,
    checker_message: CheckerDecision,
) -> AnalysisRequested:
    return AnalysisRequested(
        **base_message_kwargs(
            message_id=f"analysis-requested:{record.analysis_unit_id}:{record.record_id}",
            event_time=record.recorded_at,
            recorded_at=record.recorded_at,
            correlation_id=checker_message.correlation_id,
            causation_id=f"ceau-unit-emitted:{record.record_id}",
            idempotency_key=f"analysis_requested:{record.analysis_unit_id}:{record.record_id}",
        ),
        target_key=record.target_key,
        analysis_unit_id=record.analysis_unit_id,
        record_id=record.record_id,
    )


def _path_from_record_ref(record_ref: str) -> Path:
    if not isinstance(record_ref, str) or not record_ref.strip():
        raise RuntimeCEAUError("decision_record_ref must not be blank.")
    path_text = record_ref.split("#", 1)[0].strip()
    if not path_text:
        raise RuntimeCEAUError("decision_record_ref must include a path.")
    return Path(path_text).resolve(strict=False)


def _load_worker_completion(
    *,
    path: Path,
    target_key: str,
    event_id: str,
) -> RuntimeCheckerDecisionReceipt:
    payload = _load_json_object(path.read_text(encoding="utf-8"), path=path)
    decision_payload = _require_dict(payload.get("decision"), "decision", path=path)
    if _require_text(decision_payload.get("target_key"), "target_key", path=path) != target_key:
        raise RuntimeCEAUError("checker completion target_key does not match message.")
    event_ids = decision_payload.get("event_ids")
    if event_ids != [event_id]:
        raise RuntimeCEAUError("checker completion event_ids do not match message.")
    return RuntimeCheckerDecisionReceipt(
        decision=_require_text(decision_payload.get("decision"), "decision", path=path),
        validator_action=_optional_text(
            decision_payload.get("validator_action"),
            fallback="accepted",
        ),
        attention_hint=_optional_text(decision_payload.get("attention_hint")),
        requires_watchlist_maintenance=_optional_bool(
            decision_payload.get("requires_watchlist_maintenance")
        ),
        decision_episode_id=_optional_text(decision_payload.get("decision_episode_id")),
    )


def _load_checker_receipt_jsonl(
    *,
    path: Path,
    target_key: str,
    event_id: str,
) -> RuntimeCheckerDecisionReceipt:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = _load_json_object(line, path=path)
        if payload.get("target_key") != target_key or payload.get("event_id") != event_id:
            continue
        return RuntimeCheckerDecisionReceipt(
            decision=_require_text(payload.get("decision"), "decision", path=path),
            validator_action=_require_text(
                payload.get("validator_action"),
                "validator_action",
                path=path,
            ),
            attention_hint=_optional_text(payload.get("attention_hint")),
            requires_watchlist_maintenance=_optional_bool(
                payload.get("requires_watchlist_maintenance")
            ),
            decision_episode_id=_optional_text(payload.get("decision_episode_id")),
        )
    raise RuntimeCEAUError(f"checker decision receipt not found for {event_id}: {path}")


def _load_json_object(raw_text: str, *, path: Path) -> dict[str, object]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeCEAUError(f"invalid JSON receipt: {path}") from exc
    return _require_dict(payload, "receipt", path=path)


def _require_dict(value: object, field_name: str, *, path: Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeCEAUError(f"{field_name} must be a JSON object at {path}")
    return value


def _require_text(value: object, field_name: str, *, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeCEAUError(f"{field_name} must be a non-blank string at {path}")
    return value.strip()


def _optional_text(value: object, fallback: str | None = None) -> str:
    if value is None:
        return "" if fallback is None else fallback
    if not isinstance(value, str):
        return "" if fallback is None else fallback
    return value.strip() or ("" if fallback is None else fallback)


def _optional_bool(value: object) -> bool:
    return value if isinstance(value, bool) else False


__all__ = [
    "RuntimeCEAUEngineAdapter",
    "RuntimeCEAUError",
    "RuntimeCheckerDecisionReceipt",
    "RuntimeCheckerDecisionReceiptReader",
]
