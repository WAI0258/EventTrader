"""Formal replay execution boundary over canonical historical inputs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.feeds.models import (
    ReplayIngressInput,
    validate_replay_ingress_input,
)
from event_trader.ingest.admission import (
    derive_admission_event_id,
    shape_admission_outputs,
)
from event_trader.replay.admissibility import validate_replay_admission_request
from event_trader.replay.scheduler import (
    release_due_replay_events,
    schedule_replay_release,
)
from event_trader.runtime.release import AdmissionReleasePath


class ReplayRunnerError(ValueError):
    """Raised when replay execution inputs or step state are invalid."""


@dataclass(frozen=True, slots=True)
class ReplayStepReceipt:
    """Observable result from one replay step at one replay clock time."""

    target_key: str
    replay_at: datetime
    admitted_event_ids: tuple[str, ...]
    delivered_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ReplayRunnerError),
        )
        object.__setattr__(
            self,
            "replay_at",
            validate_timestamp(
                self.replay_at,
                field_name="replay_at",
                error_type=ReplayRunnerError,
            ),
        )
        object.__setattr__(
            self,
            "admitted_event_ids",
            _validate_event_ids(self.admitted_event_ids),
        )
        object.__setattr__(
            self,
            "delivered_event_ids",
            _validate_event_ids(self.delivered_event_ids),
        )
        if self.admitted_event_ids != self.delivered_event_ids:
            raise ReplayRunnerError(
                "admitted_event_ids and delivered_event_ids must match for one "
                "replay step."
            )


class ReplayRunner:
    """Replay execution surface for one stream of historically admissible steps."""

    def __init__(self, *, downstream_path: AdmissionReleasePath) -> None:
        if not callable(getattr(downstream_path, "release_admitted", None)):
            raise ReplayRunnerError(
                "downstream_path must provide release_admitted(outputs)."
            )
        self._downstream_path = downstream_path
        self._released_event_ids: set[str] = set()

    @property
    def released_event_ids(self) -> tuple[str, ...]:
        """Return released replay event ids in stable order."""
        return tuple(sorted(self._released_event_ids))

    def run_step(
        self,
        *,
        target_key: str,
        replay_at: datetime,
        historical_inputs: Iterable[ReplayIngressInput],
        labels: list[str] | None = None,
        ts_init: datetime | None = None,
    ) -> ReplayStepReceipt:
        """Admit and release one replay step's historically visible evidence."""
        validated_target_key = validate_target_key(
            target_key,
            error_type=ReplayRunnerError,
        )
        validated_replay_at = validate_timestamp(
            replay_at,
            field_name="replay_at",
            error_type=ReplayRunnerError,
        )
        validated_ts_init = validate_timestamp(
            validated_replay_at if ts_init is None else ts_init,
            field_name="ts_init",
            error_type=ReplayRunnerError,
        )
        step_inputs = _validate_historical_inputs(historical_inputs)
        step_labels = None if labels is None else list(labels)

        outputs_by_event_id = {}
        for ingress_input in step_inputs:
            request = validate_replay_admission_request(
                target_key=validated_target_key,
                ingress_input=ingress_input,
                replay_at=validated_replay_at,
                labels=step_labels,
            )
            event_id = derive_admission_event_id(request)
            if event_id in self._released_event_ids:
                raise ReplayRunnerError(
                    "Replay step attempted to re-admit an event that was already "
                    f"released earlier: event_id={event_id}."
                )
            if event_id in outputs_by_event_id:
                raise ReplayRunnerError(
                    "Replay step must not contain duplicate admitted evidence "
                    f"identity: event_id={event_id}."
                )
            outputs_by_event_id[event_id] = shape_admission_outputs(
                request,
                ts_init=validated_ts_init,
            )

        scheduled_releases = tuple(
            schedule_replay_release(outputs)
            for outputs in outputs_by_event_id.values()
        )
        intended_admitted_event_ids = tuple(
            schedule.runtime_event.event_id
            for schedule in sorted(
                scheduled_releases,
                key=lambda item: (item.release_at, item.runtime_event.event_id),
            )
        )
        release_receipts = release_due_replay_events(
            scheduled_releases,
            replay_at=validated_replay_at,
            downstream_path=self._downstream_path,
        )
        delivered_event_ids = tuple(
            receipt.appended_event_id for receipt in release_receipts
        )
        if intended_admitted_event_ids != delivered_event_ids:
            raise ReplayRunnerError(
                "Replay step delivered ids must exactly match intended admitted ids; "
                f"admitted_event_ids={intended_admitted_event_ids} "
                f"delivered_event_ids={delivered_event_ids}."
            )
        self._released_event_ids.update(delivered_event_ids)
        return ReplayStepReceipt(
            target_key=validated_target_key,
            replay_at=validated_replay_at,
            admitted_event_ids=intended_admitted_event_ids,
            delivered_event_ids=delivered_event_ids,
        )


def _validate_historical_inputs(
    historical_inputs: Iterable[ReplayIngressInput],
) -> tuple[ReplayIngressInput, ...]:
    try:
        inputs = tuple(historical_inputs)
    except TypeError as exc:
        raise ReplayRunnerError(
            "historical_inputs must be an iterable of canonical replay feed "
            "boundary models."
        ) from exc
    for item in inputs:
        validate_replay_ingress_input(
            item,
            error_type=ReplayRunnerError,
            message=(
                "historical_inputs must contain only canonical replay feed "
                "boundary models."
            ),
        )
    return inputs


def _validate_event_ids(event_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(event_ids, tuple):
        raise ReplayRunnerError("event_ids must be stored as a tuple[str, ...].")
    seen: set[str] = set()
    normalized_ids: list[str] = []
    for event_id in event_ids:
        if not isinstance(event_id, str):
            raise ReplayRunnerError("event_ids must contain only strings.")
        normalized = event_id.strip()
        if not normalized:
            raise ReplayRunnerError("event_ids must not contain blank values.")
        if normalized != event_id:
            raise ReplayRunnerError(
                "event_ids must not include leading or trailing whitespace."
            )
        if normalized in seen:
            raise ReplayRunnerError("event_ids must not contain duplicates.")
        seen.add(normalized)
        normalized_ids.append(normalized)
    return tuple(normalized_ids)


__all__ = [
    "ReplayRunner",
    "ReplayRunnerError",
    "ReplayStepReceipt",
]
