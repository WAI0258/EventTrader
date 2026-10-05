"""Per-target execution fence for emitted CEAU analysis units."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Lock

from event_trader.ceau.contracts import (
    CEAUAnalysisCompletedPointer,
    CEAUAnalysisCompletedRecord,
    CEAUAnalysisFailedRecord,
    CEAUAnalysisQueuedRecord,
    CEAUAnalysisStartedRecord,
    CEAUUnitEmittedRecord,
    UnitFormationLane,
)
from event_trader.ceau.store import FileBackedCEAUStore
from event_trader.contracts import AnalysisRequest, AnalysisResult, analysis_lane
from event_trader.contracts.analysis_assessment import parse_analysis_assessment

type CEAUAnalysisDispatch = Callable[
    [str, AnalysisRequest, UnitFormationLane | None],
    "CEAUAnalysisDispatchResult",
]


@dataclass(frozen=True, slots=True)
class CEAUAnalysisExecutionItem:
    """One emitted CEAU ready to enter the analysis execution fence."""

    emitted_record: CEAUUnitEmittedRecord
    request: AnalysisRequest
    unit_formation_lane: UnitFormationLane
    business_at: datetime


@dataclass(frozen=True, slots=True)
class CEAUAnalysisDispatchResult:
    """Deterministic analysis completion surface for one fenced CEAU dispatch."""

    analysis_result: AnalysisResult
    outcome_receipt_ref: CEAUAnalysisOutcomeReceiptRef


@dataclass(frozen=True, slots=True)
class CEAUAnalysisOutcomeReceiptRef:
    """Exact durable receipt identity for one analysis outcome record."""

    analysis_outcome_record_id: str
    analysis_outcome_path: str
    analysis_outcome_sha256: str


@dataclass(frozen=True, slots=True)
class _AnalysisLifecycleState:
    queued: CEAUAnalysisQueuedRecord | None = None
    started: CEAUAnalysisStartedRecord | None = None
    completed: CEAUAnalysisCompletedRecord | None = None
    failed: CEAUAnalysisFailedRecord | None = None


class CEAUAnalysisExecutionFence:
    """Serialize ResearchMemory-reading/writing analysis starts per target."""

    def __init__(
        self,
        *,
        store: FileBackedCEAUStore,
        dispatch_analysis: CEAUAnalysisDispatch,
        now: Callable[[], datetime] | None = None,
        raise_on_failure: bool = True,
    ) -> None:
        if not isinstance(raise_on_failure, bool):
            raise TypeError("raise_on_failure must be a boolean.")
        self._store = store
        self._dispatch_analysis = dispatch_analysis
        self._now = now or (lambda: datetime.now(UTC))
        self._raise_on_failure = raise_on_failure
        self._lock = Lock()
        self._pending_by_target: dict[str, list[CEAUAnalysisExecutionItem]] = {}
        self._running_targets: set[str] = set()

    def enqueue_and_drain(
        self,
        item: CEAUAnalysisExecutionItem,
    ) -> CEAUAnalysisDispatchResult:
        """Queue an emitted unit and synchronously drain its target when possible."""
        lifecycle = self._load_lifecycle(item)
        if lifecycle.completed is not None:
            return _dispatch_result_from_completed_record(
                completed_record=lifecycle.completed,
                request=item.request,
            )
        if lifecycle.queued is None:
            self._append_queued(item)
        target_key = item.emitted_record.target_key
        with self._lock:
            queue = self._pending_by_target.setdefault(target_key, [])
            _insert_by_business_time(queue, item)
        return self._drain_target(
            target_key,
            requested_analysis_unit_id=item.emitted_record.analysis_unit_id,
        )

    def _drain_target(
        self,
        target_key: str,
        *,
        requested_analysis_unit_id: str,
    ) -> CEAUAnalysisDispatchResult:
        first_failure: Exception | None = None
        completed_result: CEAUAnalysisDispatchResult | None = None
        while True:
            with self._lock:
                if target_key in self._running_targets:
                    break
                queue = self._pending_by_target.get(target_key)
                if not queue:
                    self._pending_by_target.pop(target_key, None)
                    break
                item = queue.pop(0)
                self._running_targets.add(target_key)
            try:
                result = self._run_once(item)
                if (
                    result is not None
                    and item.emitted_record.analysis_unit_id == requested_analysis_unit_id
                ):
                    completed_result = result
            except Exception as exc:
                if first_failure is None:
                    first_failure = exc
            finally:
                with self._lock:
                    self._running_targets.discard(target_key)
            if first_failure is not None:
                break
        if first_failure is not None:
            raise first_failure
        if completed_result is None:
            raise RuntimeError(
                "analysis execution fence drained without a completion result for "
                f"analysis_unit_id={requested_analysis_unit_id!r}."
            )
        return completed_result

    def _run_once(
        self,
        item: CEAUAnalysisExecutionItem,
    ) -> CEAUAnalysisDispatchResult | None:
        lifecycle = self._load_lifecycle(item)
        if lifecycle.completed is not None:
            return _dispatch_result_from_completed_record(
                completed_record=lifecycle.completed,
                request=item.request,
            )
        if lifecycle.started is None:
            self._store.append_record(
                CEAUAnalysisStartedRecord(
                    record_type="analysis_started",
                    record_id=_analysis_lifecycle_record_id(
                        stage="analysis_started",
                        analysis_unit_id=item.emitted_record.analysis_unit_id,
                    ),
                    target_key=item.emitted_record.target_key,
                    recorded_at=self._now(),
                    analysis_unit_id=item.emitted_record.analysis_unit_id,
                )
            )
        try:
            dispatch_result = self._dispatch_analysis(
                analysis_lane(item.request.target_key),
                item.request,
                item.unit_formation_lane,
            )
            completed_at = self._now()
            self._store.append_record(
                CEAUAnalysisCompletedRecord(
                    record_type="analysis_completed",
                    record_id=_analysis_lifecycle_record_id(
                        stage="analysis_completed",
                        analysis_unit_id=item.emitted_record.analysis_unit_id,
                    ),
                    target_key=item.emitted_record.target_key,
                    recorded_at=completed_at,
                    analysis_unit_id=item.emitted_record.analysis_unit_id,
                    pointer=CEAUAnalysisCompletedPointer(
                        analysis_outcome_record_id=(
                            dispatch_result.outcome_receipt_ref.analysis_outcome_record_id
                        ),
                        analysis_outcome_path=(
                            dispatch_result.outcome_receipt_ref.analysis_outcome_path
                        ),
                        analysis_outcome_sha256=(
                            dispatch_result.outcome_receipt_ref.analysis_outcome_sha256
                        ),
                        completed_status=dispatch_result.analysis_result.outcome,
                        completed_at=completed_at,
                    ),
                )
            )
            return dispatch_result
        except Exception as exc:
            failed_lifecycle = self._load_lifecycle(item)
            if failed_lifecycle.failed is None:
                self._store.append_record(
                    CEAUAnalysisFailedRecord(
                        record_type="analysis_failed",
                        record_id=_analysis_lifecycle_record_id(
                            stage="analysis_failed",
                            analysis_unit_id=item.emitted_record.analysis_unit_id,
                        ),
                        target_key=item.emitted_record.target_key,
                        recorded_at=self._now(),
                        analysis_unit_id=item.emitted_record.analysis_unit_id,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
            if self._raise_on_failure:
                raise
            return None

    def _append_queued(self, item: CEAUAnalysisExecutionItem) -> None:
        self._store.append_record(
            CEAUAnalysisQueuedRecord(
                record_type="analysis_queued",
                record_id=_analysis_lifecycle_record_id(
                    stage="analysis_queued",
                    analysis_unit_id=item.emitted_record.analysis_unit_id,
                ),
                target_key=item.emitted_record.target_key,
                recorded_at=self._now(),
                analysis_unit_id=item.emitted_record.analysis_unit_id,
            )
        )

    def _load_lifecycle(self, item: CEAUAnalysisExecutionItem) -> _AnalysisLifecycleState:
        queued: CEAUAnalysisQueuedRecord | None = None
        started: CEAUAnalysisStartedRecord | None = None
        completed: CEAUAnalysisCompletedRecord | None = None
        failed: CEAUAnalysisFailedRecord | None = None
        for persisted in self._store.read_records(target_key=item.emitted_record.target_key):
            record = persisted.record
            if getattr(record, "analysis_unit_id", None) != item.emitted_record.analysis_unit_id:
                continue
            if isinstance(record, CEAUAnalysisQueuedRecord) and _is_newer_record(queued, record):
                queued = record
            elif isinstance(record, CEAUAnalysisStartedRecord) and _is_newer_record(
                started,
                record,
            ):
                started = record
            elif isinstance(record, CEAUAnalysisCompletedRecord) and _is_newer_record(
                completed,
                record,
            ):
                completed = record
            elif isinstance(record, CEAUAnalysisFailedRecord) and _is_newer_record(
                failed,
                record,
            ):
                failed = record
        return _AnalysisLifecycleState(
            queued=queued,
            started=started,
            completed=completed,
            failed=failed,
        )


def _insert_by_business_time(
    queue: list[CEAUAnalysisExecutionItem],
    item: CEAUAnalysisExecutionItem,
) -> None:
    for index, existing in enumerate(queue):
        if (item.business_at, item.emitted_record.recorded_at) < (
            existing.business_at,
            existing.emitted_record.recorded_at,
        ):
            queue.insert(index, item)
            return
    queue.append(item)


def _analysis_lifecycle_record_id(*, stage: str, analysis_unit_id: str) -> str:
    return f"ceau-{stage}:{analysis_unit_id}"


def _is_newer_record(current: object | None, candidate: object) -> bool:
    if current is None:
        return True
    return bool(getattr(candidate, "recorded_at") >= getattr(current, "recorded_at"))


def _dispatch_result_from_completed_record(
    *,
    completed_record: CEAUAnalysisCompletedRecord,
    request: AnalysisRequest,
) -> CEAUAnalysisDispatchResult:
    outcome_payload = _read_analysis_outcome_payload(completed_record.pointer)
    return CEAUAnalysisDispatchResult(
        analysis_result=_analysis_result_from_outcome_payload(
            outcome_payload=outcome_payload,
            request=request,
            completed_status=completed_record.pointer.completed_status,
        ),
        outcome_receipt_ref=CEAUAnalysisOutcomeReceiptRef(
            analysis_outcome_record_id=completed_record.pointer.analysis_outcome_record_id,
            analysis_outcome_path=completed_record.pointer.analysis_outcome_path,
            analysis_outcome_sha256=completed_record.pointer.analysis_outcome_sha256,
        ),
    )


def _read_analysis_outcome_payload(
    pointer: CEAUAnalysisCompletedPointer,
) -> dict[str, object]:
    outcome_path = Path(pointer.analysis_outcome_path)
    if not outcome_path.exists():
        raise RuntimeError(f"analysis outcome path is missing: {outcome_path}")
    for raw_line in outcome_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        payload = json.loads(raw_line)
        if not isinstance(payload, dict):
            continue
        if payload.get("record_id") != pointer.analysis_outcome_record_id:
            continue
        actual_sha256 = sha256(raw_line.encode("utf-8")).hexdigest()
        if actual_sha256 != pointer.analysis_outcome_sha256:
            raise RuntimeError(
                "analysis outcome sha256 mismatch for "
                f"{pointer.analysis_outcome_record_id!r}."
            )
        return payload
    raise RuntimeError(
        "analysis outcome record is missing for "
        f"{pointer.analysis_outcome_record_id!r} in {outcome_path}."
    )


def _analysis_result_from_outcome_payload(
    *,
    outcome_payload: dict[str, object],
    request: AnalysisRequest,
    completed_status: str,
) -> AnalysisResult:
    payload_target_key = outcome_payload.get("target_key")
    if not isinstance(payload_target_key, str) or payload_target_key != request.target_key:
        raise RuntimeError("analysis outcome payload target_key does not match the request.")
    payload_event_ids = outcome_payload.get("event_ids")
    if not isinstance(payload_event_ids, list):
        raise RuntimeError("analysis outcome payload event_ids must be a list.")
    if tuple(payload_event_ids) != tuple(request.event_ids):
        raise RuntimeError("analysis outcome payload event_ids do not match the request.")
    payload_outcome = outcome_payload.get("outcome")
    if not isinstance(payload_outcome, str) or payload_outcome != completed_status:
        raise RuntimeError("analysis outcome payload outcome does not match CEAU completion.")
    used_lesson_ids_payload = outcome_payload.get("used_lesson_ids", [])
    if not isinstance(used_lesson_ids_payload, list) or any(
        not isinstance(item, str) or not item.strip() for item in used_lesson_ids_payload
    ):
        raise RuntimeError("analysis outcome payload used_lesson_ids is invalid.")
    analysis_assessment_payload = outcome_payload.get("analysis_assessment")
    if analysis_assessment_payload is not None and not isinstance(
        analysis_assessment_payload,
        dict,
    ):
        raise RuntimeError("analysis outcome payload analysis_assessment must be an object.")
    return AnalysisResult(
        target_key=request.target_key,
        event_ids=list(request.event_ids),
        outcome=payload_outcome,
        analysis_assessment=(
            None
            if analysis_assessment_payload is None
            else parse_analysis_assessment(analysis_assessment_payload)
        ),
        used_lesson_ids=tuple(used_lesson_ids_payload),
    )


__all__ = [
    "CEAUAnalysisDispatch",
    "CEAUAnalysisDispatchResult",
    "CEAUAnalysisOutcomeReceiptRef",
    "CEAUAnalysisExecutionFence",
    "CEAUAnalysisExecutionItem",
]
