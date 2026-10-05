"""Resolve runtime AnalysisRequested refs into canonical project analysis inputs."""

from __future__ import annotations

from dataclasses import dataclass

from event_trader.ceau.contracts import (
    CEAUUnitEmittedRecord,
    UnitFormationLane,
)
from event_trader.ceau.policy import StreamRoutingConfig
from event_trader.ceau.replay import (
    build_analysis_request_from_unit_emitted_record,
    build_unit_formation_lane_from_unit_emitted_record,
)
from event_trader.ceau.store import FileBackedCEAUStore, PersistedCEAURecord
from event_trader.contracts import AnalysisRequest, EvidenceLedgerRecord
from event_trader.contracts.ports import EvidenceLedgerPort
from event_trader.decision_memory import derive_decision_episode_id
from event_trader.evidence_ledger import read_evidence

from .analysis_queue import AnalysisWorkItem


class RuntimeAnalysisResolutionError(ValueError):
    """Raised when runtime analysis refs cannot be resolved from durable state."""


@dataclass(frozen=True, slots=True)
class RuntimeAnalysisResolution:
    """Canonical analysis input rebuilt from emitted CEAU state."""

    work_item: AnalysisWorkItem
    emitted_record: CEAUUnitEmittedRecord
    request: AnalysisRequest
    unit_formation_lane: UnitFormationLane

    def __post_init__(self) -> None:
        if not isinstance(self.work_item, AnalysisWorkItem):
            raise RuntimeAnalysisResolutionError("work_item must be an AnalysisWorkItem.")
        if not isinstance(self.emitted_record, CEAUUnitEmittedRecord):
            raise RuntimeAnalysisResolutionError(
                "emitted_record must be a CEAUUnitEmittedRecord."
            )
        if not isinstance(self.request, AnalysisRequest):
            raise RuntimeAnalysisResolutionError("request must be an AnalysisRequest.")
        if not isinstance(self.unit_formation_lane, UnitFormationLane):
            raise RuntimeAnalysisResolutionError(
                "unit_formation_lane must be a UnitFormationLane."
            )
        if self.request.target_key != self.work_item.target_key:
            raise RuntimeAnalysisResolutionError("request target_key must match work_item.")
        if self.emitted_record.analysis_unit_id != self.work_item.analysis_unit_id:
            raise RuntimeAnalysisResolutionError(
                "emitted_record analysis_unit_id must match work_item."
            )
        if self.emitted_record.record_id != self.work_item.record_id:
            raise RuntimeAnalysisResolutionError("emitted_record record_id must match work_item.")


class RuntimeAnalysisRequestResolver:
    """Reload emitted CEAU units and reconstruct canonical analysis inputs."""

    def __init__(
        self,
        *,
        ledger: EvidenceLedgerPort,
        ceau_store: FileBackedCEAUStore,
        stream_routing_config: StreamRoutingConfig,
    ) -> None:
        read = getattr(ledger, "read", None)
        if not callable(read):
            raise RuntimeAnalysisResolutionError("ledger must provide read(event_id).")
        if not isinstance(ceau_store, FileBackedCEAUStore):
            raise RuntimeAnalysisResolutionError("ceau_store must be a FileBackedCEAUStore.")
        if not isinstance(stream_routing_config, StreamRoutingConfig):
            raise RuntimeAnalysisResolutionError(
                "stream_routing_config must be a StreamRoutingConfig."
            )
        self._ledger = ledger
        self._ceau_store = ceau_store
        self._stream_routing_config = stream_routing_config

    def resolve(self, work_item: AnalysisWorkItem) -> RuntimeAnalysisResolution:
        if not isinstance(work_item, AnalysisWorkItem):
            raise RuntimeAnalysisResolutionError("work_item must be an AnalysisWorkItem.")
        records = self._ceau_store.read_records(target_key=work_item.target_key)
        emitted_record = _find_emitted_record(records=records, work_item=work_item)
        evidence_records = _read_evidence_records(
            ledger=self._ledger,
            event_ids=emitted_record.event_ids,
        )
        evidence_by_id = {record.event_id: record for record in evidence_records}
        primary_record = evidence_by_id[emitted_record.primary_event_ids[0]]
        unit_policy = self._stream_routing_config.lane_policies.get(
            emitted_record.unit_key.routing_lane,
            self._stream_routing_config.default_unit_policy,
        )
        request = build_analysis_request_from_unit_emitted_record(
            emitted_record=emitted_record,
            decision_episode_id=derive_decision_episode_id(
                target_key=emitted_record.target_key,
                first_event_id=emitted_record.primary_event_ids[0],
                checker_business_at=primary_record.ts_event,
            ),
        )
        unit_formation_lane = build_unit_formation_lane_from_unit_emitted_record(
            emitted_record=emitted_record,
            unit_type=emitted_record.unit_key.routing_lane,
            completeness_requirements=unit_policy.completeness_requirements,
            policy_status=self._stream_routing_config.policy_status,
        )
        return RuntimeAnalysisResolution(
            work_item=work_item,
            emitted_record=emitted_record,
            request=request,
            unit_formation_lane=unit_formation_lane,
        )


def _find_emitted_record(
    *,
    records: tuple[PersistedCEAURecord, ...],
    work_item: AnalysisWorkItem,
) -> CEAUUnitEmittedRecord:
    for persisted in records:
        record = persisted.record
        if not isinstance(record, CEAUUnitEmittedRecord):
            continue
        if record.analysis_unit_id != work_item.analysis_unit_id:
            continue
        if record.record_id != work_item.record_id:
            continue
        return record
    raise RuntimeAnalysisResolutionError(
        "CEAU unit_emitted record not found for "
        f"analysis_unit_id={work_item.analysis_unit_id!r} record_id={work_item.record_id!r}."
    )


def _read_evidence_records(
    *,
    ledger: EvidenceLedgerPort,
    event_ids: tuple[str, ...],
) -> tuple[EvidenceLedgerRecord, ...]:
    evidence_records = tuple(read_evidence(list(event_ids), ledger=ledger))
    if len(evidence_records) != len(event_ids):
        raise RuntimeAnalysisResolutionError(
            "emitted CEAU unit references evidence missing from the ledger."
        )
    return evidence_records


__all__ = [
    "RuntimeAnalysisRequestResolver",
    "RuntimeAnalysisResolution",
    "RuntimeAnalysisResolutionError",
]
