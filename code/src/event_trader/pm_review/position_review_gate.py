"""Deterministic gate for open-position PM re-review requests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from math import isfinite
from typing import Literal, cast

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.contracts.price_level_role import PriceLevelRole
from event_trader.contracts.view_state_change import MarketDataBar, ViewState
from event_trader.pm_review.position_review import PMPositionReviewTriggerHit
from event_trader.portfolio.contracts import PMDecision

PMPositionReviewGateOutcome = Literal["allowed", "skipped"]
PMPositionReviewGateReason = Literal[
    "disabled",
    "risk_trigger_bypass",
    "first_review_trigger",
    "rearmed_review_trigger",
    "cooldown_active",
    "not_rearmed",
]


class PMPositionReviewGateError(ValueError):
    """Raised when position-review gate inputs or records are malformed."""


@dataclass(frozen=True, slots=True)
class PMPositionReviewGateConfig:
    """Runtime policy for non-risk open-position PM review coalescing."""

    enabled: bool
    review_cooldown_minutes: int
    rearm_buffer_bps: float

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise PMPositionReviewGateError("enabled must be a boolean.")
        if (
            not isinstance(self.review_cooldown_minutes, int)
            or isinstance(self.review_cooldown_minutes, bool)
            or self.review_cooldown_minutes < 1
        ):
            raise PMPositionReviewGateError(
                "review_cooldown_minutes must be a positive integer."
            )
        if (
            not isinstance(self.rearm_buffer_bps, int | float)
            or isinstance(self.rearm_buffer_bps, bool)
            or not isfinite(float(self.rearm_buffer_bps))
            or float(self.rearm_buffer_bps) <= 0.0
        ):
            raise PMPositionReviewGateError("rearm_buffer_bps must be positive.")
        object.__setattr__(self, "rearm_buffer_bps", float(self.rearm_buffer_bps))


@dataclass(frozen=True, slots=True)
class PMPositionReviewGateDecision:
    """Pre-request gate outcome for one deterministic trigger hit."""

    outcome: PMPositionReviewGateOutcome
    reason: PMPositionReviewGateReason
    gate_key: str
    prior_request_id: str | None = None
    cooldown_until: datetime | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"allowed", "skipped"}:
            raise PMPositionReviewGateError("outcome is not supported.")
        if self.reason not in {
            "disabled",
            "risk_trigger_bypass",
            "first_review_trigger",
            "rearmed_review_trigger",
            "cooldown_active",
            "not_rearmed",
        }:
            raise PMPositionReviewGateError("reason is not supported.")
        object.__setattr__(self, "gate_key", _validate_non_blank(self.gate_key, "gate_key"))
        object.__setattr__(
            self,
            "prior_request_id",
            _validate_optional_text(self.prior_request_id, "prior_request_id"),
        )
        if self.cooldown_until is not None:
            object.__setattr__(
                self,
                "cooldown_until",
                validate_timestamp(
                    self.cooldown_until,
                    field_name="cooldown_until",
                    error_type=PMPositionReviewGateError,
                ),
            )


@dataclass(frozen=True, slots=True)
class PMPositionReviewGateRecord:
    """Append-only audit record for a position-review gate decision."""

    gate_record_id: str
    target_key: str
    business_at: datetime
    outcome: PMPositionReviewGateOutcome
    reason: PMPositionReviewGateReason
    gate_key: str
    source_pm_decision_id: str
    source_episode_id: str
    requested_state: ViewState
    trigger_kind: str
    touched_level_ids: tuple[str, ...]
    touched_market_bar_ids: tuple[str, ...]
    request_id: str | None
    prior_request_id: str | None
    cooldown_until: datetime | None
    rearm_buffer_bps: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gate_record_id",
            _validate_non_blank(self.gate_record_id, "gate_record_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PMPositionReviewGateError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PMPositionReviewGateError,
            ),
        )
        if self.outcome not in {"allowed", "skipped"}:
            raise PMPositionReviewGateError("outcome is not supported.")
        if self.reason not in {
            "disabled",
            "risk_trigger_bypass",
            "first_review_trigger",
            "rearmed_review_trigger",
            "cooldown_active",
            "not_rearmed",
        }:
            raise PMPositionReviewGateError("reason is not supported.")
        object.__setattr__(self, "gate_key", _validate_non_blank(self.gate_key, "gate_key"))
        object.__setattr__(
            self,
            "source_pm_decision_id",
            _validate_non_blank(self.source_pm_decision_id, "source_pm_decision_id"),
        )
        object.__setattr__(
            self,
            "source_episode_id",
            _validate_non_blank(self.source_episode_id, "source_episode_id"),
        )
        if self.requested_state not in {
            "weak_long",
            "strong_long",
            "weak_short",
            "strong_short",
        }:
            raise PMPositionReviewGateError(
                "requested_state must be a non-flat position state."
            )
        object.__setattr__(
            self,
            "trigger_kind",
            _validate_non_blank(self.trigger_kind, "trigger_kind"),
        )
        object.__setattr__(
            self,
            "touched_level_ids",
            _validate_string_tuple(self.touched_level_ids, "touched_level_ids"),
        )
        object.__setattr__(
            self,
            "touched_market_bar_ids",
            _validate_string_tuple(
                self.touched_market_bar_ids,
                "touched_market_bar_ids",
            ),
        )
        object.__setattr__(
            self,
            "request_id",
            _validate_optional_text(self.request_id, "request_id"),
        )
        object.__setattr__(
            self,
            "prior_request_id",
            _validate_optional_text(self.prior_request_id, "prior_request_id"),
        )
        if self.cooldown_until is not None:
            object.__setattr__(
                self,
                "cooldown_until",
                validate_timestamp(
                    self.cooldown_until,
                    field_name="cooldown_until",
                    error_type=PMPositionReviewGateError,
                ),
            )
        if (
            not isinstance(self.rearm_buffer_bps, int | float)
            or isinstance(self.rearm_buffer_bps, bool)
            or not isfinite(float(self.rearm_buffer_bps))
            or float(self.rearm_buffer_bps) <= 0.0
        ):
            raise PMPositionReviewGateError("rearm_buffer_bps must be positive.")
        object.__setattr__(self, "rearm_buffer_bps", float(self.rearm_buffer_bps))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "gate_record_id": self.gate_record_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "outcome": self.outcome,
            "reason": self.reason,
            "gate_key": self.gate_key,
            "source_pm_decision_id": self.source_pm_decision_id,
            "source_episode_id": self.source_episode_id,
            "requested_state": self.requested_state,
            "trigger_kind": self.trigger_kind,
            "touched_level_ids": list(self.touched_level_ids),
            "touched_market_bar_ids": list(self.touched_market_bar_ids),
            "request_id": self.request_id,
            "prior_request_id": self.prior_request_id,
            "cooldown_until": (
                None if self.cooldown_until is None else self.cooldown_until.isoformat()
            ),
            "rearm_buffer_bps": self.rearm_buffer_bps,
        }


def evaluate_pm_position_review_gate(
    *,
    config: PMPositionReviewGateConfig,
    source_decision: PMDecision,
    trigger_hit: PMPositionReviewTriggerHit,
    prior_gate_records: tuple[PMPositionReviewGateRecord, ...],
    market_bars: tuple[MarketDataBar, ...],
    price_level_roles: tuple[PriceLevelRole, ...],
) -> PMPositionReviewGateDecision:
    """Decide whether one open-position trigger should materialize a PMReview."""

    if not isinstance(config, PMPositionReviewGateConfig):
        raise PMPositionReviewGateError("config must be a PMPositionReviewGateConfig.")
    if not isinstance(source_decision, PMDecision):
        raise PMPositionReviewGateError("source_decision must be a PMDecision.")
    if not isinstance(trigger_hit, PMPositionReviewTriggerHit):
        raise PMPositionReviewGateError("trigger_hit must be a PMPositionReviewTriggerHit.")
    gate_key = derive_pm_position_review_gate_key(
        target_key=trigger_hit.target_key,
        source_episode_id=source_decision.decision_episode_id,
        requested_state=source_decision.requested_state,
        trigger_kind=trigger_hit.trigger_kind,
        touched_level_ids=trigger_hit.touched_level_ids,
    )
    if not config.enabled:
        return PMPositionReviewGateDecision(
            outcome="allowed",
            reason="disabled",
            gate_key=gate_key,
        )
    if trigger_hit.trigger_kind == "invalidation":
        return PMPositionReviewGateDecision(
            outcome="allowed",
            reason="risk_trigger_bypass",
            gate_key=gate_key,
        )
    prior_allowed = tuple(
        sorted(
            (
                record
                for record in prior_gate_records
                if record.gate_key == gate_key
                and record.outcome == "allowed"
                and record.request_id is not None
            ),
            key=lambda record: (record.business_at, record.gate_record_id),
        )
    )
    if not prior_allowed:
        return PMPositionReviewGateDecision(
            outcome="allowed",
            reason="first_review_trigger",
            gate_key=gate_key,
        )
    prior = prior_allowed[-1]
    cooldown_until = prior.business_at + timedelta(
        minutes=config.review_cooldown_minutes
    )
    if trigger_hit.business_at < cooldown_until:
        return PMPositionReviewGateDecision(
            outcome="skipped",
            reason="cooldown_active",
            gate_key=gate_key,
            prior_request_id=prior.request_id,
            cooldown_until=cooldown_until,
        )
    if not _trigger_rearmed(
        trigger_hit=trigger_hit,
        prior_business_at=prior.business_at,
        market_bars=market_bars,
        price_level_roles=price_level_roles,
        rearm_buffer_bps=config.rearm_buffer_bps,
    ):
        return PMPositionReviewGateDecision(
            outcome="skipped",
            reason="not_rearmed",
            gate_key=gate_key,
            prior_request_id=prior.request_id,
            cooldown_until=cooldown_until,
        )
    return PMPositionReviewGateDecision(
        outcome="allowed",
        reason="rearmed_review_trigger",
        gate_key=gate_key,
        prior_request_id=prior.request_id,
        cooldown_until=cooldown_until,
    )


def build_pm_position_review_gate_record(
    *,
    decision: PMPositionReviewGateDecision,
    source_decision: PMDecision,
    trigger_hit: PMPositionReviewTriggerHit,
    request_id: str | None,
    rearm_buffer_bps: float,
) -> PMPositionReviewGateRecord:
    gate_record_id = derive_pm_position_review_gate_record_id(
        gate_key=decision.gate_key,
        business_at=trigger_hit.business_at,
        outcome=decision.outcome,
        reason=decision.reason,
        touched_market_bar_ids=trigger_hit.touched_market_bar_ids,
        request_id=request_id,
    )
    return PMPositionReviewGateRecord(
        gate_record_id=gate_record_id,
        target_key=trigger_hit.target_key,
        business_at=trigger_hit.business_at,
        outcome=decision.outcome,
        reason=decision.reason,
        gate_key=decision.gate_key,
        source_pm_decision_id=source_decision.decision_id,
        source_episode_id=source_decision.decision_episode_id,
        requested_state=source_decision.requested_state,
        trigger_kind=trigger_hit.trigger_kind,
        touched_level_ids=trigger_hit.touched_level_ids,
        touched_market_bar_ids=trigger_hit.touched_market_bar_ids,
        request_id=request_id,
        prior_request_id=decision.prior_request_id,
        cooldown_until=decision.cooldown_until,
        rearm_buffer_bps=rearm_buffer_bps,
    )


def derive_pm_position_review_gate_key(
    *,
    target_key: str,
    source_episode_id: str,
    requested_state: ViewState,
    trigger_kind: str,
    touched_level_ids: tuple[str, ...],
) -> str:
    payload = {
        "target_key": validate_target_key(
            target_key,
            error_type=PMPositionReviewGateError,
        ),
        "source_episode_id": _validate_non_blank(
            source_episode_id,
            "source_episode_id",
        ),
        "requested_state": requested_state,
        "trigger_kind": _validate_non_blank(trigger_kind, "trigger_kind"),
        "touched_level_ids": list(_validate_string_tuple(touched_level_ids, "touched_level_ids")),
    }
    digest = _stable_digest(payload)
    return f"pm-position-review-gate:{digest[:24]}"


def derive_pm_position_review_gate_record_id(
    *,
    gate_key: str,
    business_at: datetime,
    outcome: PMPositionReviewGateOutcome,
    reason: PMPositionReviewGateReason,
    touched_market_bar_ids: tuple[str, ...],
    request_id: str | None,
) -> str:
    payload = {
        "gate_key": _validate_non_blank(gate_key, "gate_key"),
        "business_at": validate_timestamp(
            business_at,
            field_name="business_at",
            error_type=PMPositionReviewGateError,
        ).isoformat(),
        "outcome": outcome,
        "reason": reason,
        "touched_market_bar_ids": list(
            _validate_string_tuple(touched_market_bar_ids, "touched_market_bar_ids")
        ),
        "request_id": request_id,
    }
    digest = _stable_digest(payload)
    return f"pm-position-review-gate-record:{digest[:32]}"


def parse_pm_position_review_gate_record(
    payload: Mapping[str, object],
) -> PMPositionReviewGateRecord:
    required = {
        "gate_record_id",
        "target_key",
        "business_at",
        "outcome",
        "reason",
        "gate_key",
        "source_pm_decision_id",
        "source_episode_id",
        "requested_state",
        "trigger_kind",
        "touched_level_ids",
        "touched_market_bar_ids",
        "request_id",
        "prior_request_id",
        "cooldown_until",
        "rearm_buffer_bps",
    }
    if set(payload) != required:
        missing = sorted(required - set(payload))
        extra = sorted(set(payload) - required)
        detail = []
        if missing:
            detail.append(f"missing={missing!r}")
        if extra:
            detail.append(f"extra={extra!r}")
        raise PMPositionReviewGateError(
            "pm_position_review_gate_record fields mismatch: " + ", ".join(detail)
        )
    return PMPositionReviewGateRecord(
        gate_record_id=_require_text(payload, "gate_record_id"),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        outcome=cast(PMPositionReviewGateOutcome, _require_text(payload, "outcome")),
        reason=cast(PMPositionReviewGateReason, _require_text(payload, "reason")),
        gate_key=_require_text(payload, "gate_key"),
        source_pm_decision_id=_require_text(payload, "source_pm_decision_id"),
        source_episode_id=_require_text(payload, "source_episode_id"),
        requested_state=cast(ViewState, _require_text(payload, "requested_state")),
        trigger_kind=_require_text(payload, "trigger_kind"),
        touched_level_ids=tuple(_require_string_list(payload, "touched_level_ids")),
        touched_market_bar_ids=tuple(
            _require_string_list(payload, "touched_market_bar_ids")
        ),
        request_id=_optional_text(payload.get("request_id")),
        prior_request_id=_optional_text(payload.get("prior_request_id")),
        cooldown_until=_parse_optional_timestamp(
            payload.get("cooldown_until"),
            "cooldown_until",
        ),
        rearm_buffer_bps=_require_float(payload, "rearm_buffer_bps"),
    )


def _trigger_rearmed(
    *,
    trigger_hit: PMPositionReviewTriggerHit,
    prior_business_at: datetime,
    market_bars: tuple[MarketDataBar, ...],
    price_level_roles: tuple[PriceLevelRole, ...],
    rearm_buffer_bps: float,
) -> bool:
    level_lookup = {level.level_id: level for level in price_level_roles}
    candidate_bars = tuple(
        bar
        for bar in market_bars
        if prior_business_at < bar.end_at < trigger_hit.business_at
    )
    if not candidate_bars:
        return False
    for level_id in trigger_hit.touched_level_ids:
        level = level_lookup.get(level_id)
        if level is None:
            return False
        if not any(
            _bar_outside_rearm_buffer(
                bar=bar,
                level=level,
                rearm_buffer_bps=rearm_buffer_bps,
            )
            for bar in candidate_bars
        ):
            return False
    return True


def _bar_outside_rearm_buffer(
    *,
    bar: MarketDataBar,
    level: PriceLevelRole,
    rearm_buffer_bps: float,
) -> bool:
    lower, upper = _expanded_level_bounds(level=level, rearm_buffer_bps=rearm_buffer_bps)
    return bar.high_price < lower or bar.low_price > upper


def _expanded_level_bounds(
    *,
    level: PriceLevelRole,
    rearm_buffer_bps: float,
) -> tuple[float, float]:
    multiplier = rearm_buffer_bps / 10_000.0
    if level.value is not None:
        buffer = level.value * multiplier
        return level.value - buffer, level.value + buffer
    if level.lower is None or level.upper is None:
        raise PMPositionReviewGateError("level requires value or lower/upper bounds.")
    lower_buffer = level.lower * multiplier
    upper_buffer = level.upper * multiplier
    return level.lower - lower_buffer, level.upper + upper_buffer


def _validate_non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PMPositionReviewGateError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _validate_optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_non_blank(value, field_name)


def _validate_string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise PMPositionReviewGateError(f"{field_name} must be a tuple.")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized_item = _validate_non_blank(item, field_name)
        if normalized_item in seen:
            continue
        seen.add(normalized_item)
        normalized.append(normalized_item)
    if not normalized:
        raise PMPositionReviewGateError(f"{field_name} must not be empty.")
    return tuple(normalized)


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_non_blank(payload.get(field_name), field_name)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _validate_non_blank(value, "optional text")


def _require_string_list(
    payload: Mapping[str, object],
    field_name: str,
) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise PMPositionReviewGateError(f"{field_name} must be a list.")
    return [_validate_non_blank(item, field_name) for item in value]


def _require_float(payload: Mapping[str, object], field_name: str) -> float:
    value = payload.get(field_name)
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not isfinite(float(value))
    ):
        raise PMPositionReviewGateError(f"{field_name} must be a finite number.")
    return float(value)


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise PMPositionReviewGateError(f"{field_name} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PMPositionReviewGateError(
            f"{field_name} must be an ISO timestamp."
        ) from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=PMPositionReviewGateError,
    )


def _parse_optional_timestamp(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_timestamp(value, field_name)


def _stable_digest(payload: Mapping[str, object]) -> str:
    return sha256(
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "PMPositionReviewGateConfig",
    "PMPositionReviewGateDecision",
    "PMPositionReviewGateError",
    "PMPositionReviewGateOutcome",
    "PMPositionReviewGateReason",
    "PMPositionReviewGateRecord",
    "build_pm_position_review_gate_record",
    "derive_pm_position_review_gate_key",
    "derive_pm_position_review_gate_record_id",
    "evaluate_pm_position_review_gate",
    "parse_pm_position_review_gate_record",
]
