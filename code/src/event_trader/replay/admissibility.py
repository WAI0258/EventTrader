"""Replay-specific admission gating that enforces historical visibility."""

from __future__ import annotations

from datetime import datetime

from event_trader.contracts._validators import validate_timestamp
from event_trader.feeds.historical_web_search_guard import (
    detect_historical_web_search_future_leakage,
)
from event_trader.feeds.models import validate_replay_ingress_input
from event_trader.ingest.admission import (
    AdmissionRequest,
    validate_admission_request,
)
from event_trader.replay.timestamps import (
    HistoricalWebSearchInput,
    ReplayIngressInput,
    replay_ts_event,
)


class ReplayAdmissibilityError(ValueError):
    """Raised when replay evidence crosses the boundary before visibility."""


def validate_replay_admissibility(
    ingress_input: ReplayIngressInput,
    *,
    replay_at: datetime,
) -> ReplayIngressInput:
    """Reject replay evidence that is not yet historically visible."""
    validated_input = _validate_replay_ingress_input(ingress_input)
    validated_replay_at = validate_timestamp(
        replay_at,
        field_name="replay_at",
        error_type=ReplayAdmissibilityError,
    )
    visible_at = replay_ts_event(validated_input)
    if validated_replay_at < visible_at:
        raise ReplayAdmissibilityError(
            "Replay ingress_input is not historically visible yet; "
            f"replay_at={validated_replay_at.isoformat()} "
            f"visible_at={visible_at.isoformat()} "
            f"source_ref={validated_input.source_ref}."
        )
    if isinstance(validated_input, HistoricalWebSearchInput):
        future_leakage = detect_historical_web_search_future_leakage(
            source_ref=validated_input.source_ref,
            title=validated_input.title,
            content=validated_input.content,
            visible_at=validated_input.visible_at,
        )
        if future_leakage is not None:
            raise ReplayAdmissibilityError(future_leakage.rejection_reason)
    return validated_input


def validate_replay_admission_request(
    *,
    target_key: str,
    ingress_input: ReplayIngressInput,
    replay_at: datetime,
    labels: list[str] | None = None,
) -> AdmissionRequest:
    """Gate replay ingress at the historical boundary before admission."""
    validated_input = validate_replay_admissibility(
        ingress_input,
        replay_at=replay_at,
    )
    return validate_admission_request(
        target_key=target_key,
        ingress_input=validated_input,
        labels=labels,
    )


def _validate_replay_ingress_input(value: object) -> ReplayIngressInput:
    return validate_replay_ingress_input(
        value,
        error_type=ReplayAdmissibilityError,
        message=(
            "ingress_input must be one of the canonical replay feed boundary "
            "models."
        ),
    )


__all__ = [
    "ReplayAdmissibilityError",
    "validate_replay_admissibility",
    "validate_replay_admission_request",
]
