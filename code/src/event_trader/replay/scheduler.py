"""Replay-specific release scheduling built on explicit historical visibility time."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from event_trader.contracts._validators import validate_timestamp
from event_trader.contracts.evidence import EvidenceEvent, EvidenceLedgerRecord
from event_trader.ingest.admission import AdmissionOutputs
from event_trader.runtime.release import (
    AdmissionReleasePath,
    RuntimeReleaseReceipt,
)


class ReplayReleaseError(ValueError):
    """Raised when replay release scheduling or timing is invalid."""


@dataclass(frozen=True, slots=True)
class ReplayScheduledRelease:
    """Explicit replay release plan for one admitted evidence event."""

    release_at: datetime
    ledger_record: EvidenceLedgerRecord
    runtime_event: EvidenceEvent

    def __post_init__(self) -> None:
        validated_release_at = validate_timestamp(
            self.release_at,
            field_name="release_at",
            error_type=ReplayReleaseError,
        )
        if not isinstance(self.ledger_record, EvidenceLedgerRecord):
            raise ReplayReleaseError(
                "ledger_record must be an EvidenceLedgerRecord instance."
            )
        if not isinstance(self.runtime_event, EvidenceEvent):
            raise ReplayReleaseError(
                "runtime_event must be an EvidenceEvent instance."
            )

        _validate_release_pair(self.ledger_record, self.runtime_event)
        if (
            validated_release_at != self.ledger_record.ts_event
            or validated_release_at != self.runtime_event.ts_event
        ):
            raise ReplayReleaseError(
                "release_at must equal the admitted replay ts_event for both "
                "ledger_record and runtime_event."
            )

        object.__setattr__(self, "release_at", validated_release_at)


def schedule_replay_release(outputs: AdmissionOutputs) -> ReplayScheduledRelease:
    """Build an explicit replay release plan from admitted outputs."""
    validated_outputs = _validate_admission_outputs(outputs)
    return ReplayScheduledRelease(
        release_at=validated_outputs.runtime_event.ts_event,
        ledger_record=validated_outputs.ledger_record,
        runtime_event=validated_outputs.runtime_event,
    )


def is_replay_release_due(
    schedule: ReplayScheduledRelease,
    *,
    replay_at: datetime,
) -> bool:
    """Return whether one scheduled replay release is due at replay_at."""
    validated_schedule = _validate_schedule(schedule)
    validated_replay_at = validate_timestamp(
        replay_at,
        field_name="replay_at",
        error_type=ReplayReleaseError,
    )
    return validated_replay_at >= validated_schedule.release_at



def release_replay_incoming_when_due(
    schedule: ReplayScheduledRelease,
    *,
    replay_at: datetime,
    downstream_path: AdmissionReleasePath,
) -> RuntimeReleaseReceipt:
    """Append and publish one replay event only when its release schedule is due."""
    validated_schedule = _validate_schedule(schedule)
    if not callable(getattr(downstream_path, "release_admitted", None)):
        raise ReplayReleaseError(
            "downstream_path must provide release_admitted(outputs)."
        )
    validated_replay_at = validate_timestamp(
        replay_at,
        field_name="replay_at",
        error_type=ReplayReleaseError,
    )
    if validated_replay_at < validated_schedule.release_at:
        raise ReplayReleaseError(
            "Replay scheduled release is not due yet; "
            f"replay_at={validated_replay_at.isoformat()} "
            f"release_at={validated_schedule.release_at.isoformat()} "
            f"event_id={validated_schedule.runtime_event.event_id}."
        )

    return downstream_path.release_admitted(
        AdmissionOutputs(
            ledger_record=validated_schedule.ledger_record,
            runtime_event=validated_schedule.runtime_event,
        )
    )



def release_due_replay_events(
    schedules: Iterable[ReplayScheduledRelease],
    *,
    replay_at: datetime,
    downstream_path: AdmissionReleasePath,
) -> tuple[RuntimeReleaseReceipt, ...]:
    """Release all due replay events in deterministic replay order."""
    if not callable(getattr(downstream_path, "release_admitted", None)):
        raise ReplayReleaseError(
            "downstream_path must provide release_admitted(outputs)."
        )
    validated_replay_at = validate_timestamp(
        replay_at,
        field_name="replay_at",
        error_type=ReplayReleaseError,
    )
    try:
        validated_schedules = tuple(
            _validate_schedule(schedule) for schedule in schedules
        )
    except TypeError as exc:
        raise ReplayReleaseError(
            "schedules must be an iterable of ReplayScheduledRelease instances."
        ) from exc

    not_due_schedule = next(
        (
            validated_schedule
            for validated_schedule in validated_schedules
            if validated_replay_at < validated_schedule.release_at
        ),
        None,
    )
    if not_due_schedule is not None:
        raise ReplayReleaseError(
            "Replay batch release requires every schedule to be due at replay_at; "
            f"replay_at={validated_replay_at.isoformat()} "
            f"release_at={not_due_schedule.release_at.isoformat()} "
            f"event_id={not_due_schedule.runtime_event.event_id}."
        )

    ordered_schedules = tuple(sorted(validated_schedules, key=_release_order))
    return tuple(
        release_replay_incoming_when_due(
            schedule,
            replay_at=validated_replay_at,
            downstream_path=downstream_path,
        )
        for schedule in ordered_schedules
    )



def _validate_admission_outputs(outputs: AdmissionOutputs) -> AdmissionOutputs:
    if not isinstance(outputs, AdmissionOutputs):
        raise ReplayReleaseError(
            "outputs must be an AdmissionOutputs instance."
        )
    return outputs



def _validate_schedule(schedule: ReplayScheduledRelease) -> ReplayScheduledRelease:
    if not isinstance(schedule, ReplayScheduledRelease):
        raise ReplayReleaseError(
            "schedule must be a ReplayScheduledRelease instance."
        )
    return schedule



def _validate_release_pair(
    ledger_record: EvidenceLedgerRecord,
    runtime_event: EvidenceEvent,
) -> None:
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
        raise ReplayReleaseError(
            "ledger_record and runtime_event must match across: "
            f"{mismatched}."
        )



def _release_order(schedule: ReplayScheduledRelease) -> tuple[datetime, str]:
    return (schedule.release_at, schedule.runtime_event.event_id)


__all__ = [
    "ReplayReleaseError",
    "ReplayScheduledRelease",
    "is_replay_release_due",
    "release_due_replay_events",
    "release_replay_incoming_when_due",
    "schedule_replay_release",
]
