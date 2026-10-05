"""Composition-ready reflection entry seam outside the checker path.

This module intentionally stops at cadence gating and dependency reporting.
It does not scan eligible anchors, assemble review packets, or write review
artifacts yet. Those later layers can compose on top of this seam without
changing how the heartbeat loop asks whether reflection is due.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast

from event_trader.config import KernelConfig
from event_trader.contracts._validators import validate_timestamp
from event_trader.contracts.ports import OutcomeContextPort, TradeContextPort

from .contracts import ReflectionRunReceipt

type ReflectionEntryStatus = Literal["due", "not_due", "dependency_missing"]
type ReflectionEmitter = Callable[[str], None]


class ReflectionEntryError(ValueError):
    """Raised when reflection cadence or entry-seam inputs are malformed."""


@dataclass(frozen=True, slots=True)
class ReflectionDueCheck:
    """Typed cadence decision for one reflection heartbeat check."""

    cadence_hours: float
    checked_at: datetime
    last_completed_at: datetime | None
    next_due_at: datetime
    due: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cadence_hours",
            _validate_positive_hours(
                self.cadence_hours,
                field_name="cadence_hours",
            ),
        )
        checked_at = validate_timestamp(
            self.checked_at,
            field_name="checked_at",
            error_type=ReflectionEntryError,
        )
        object.__setattr__(self, "checked_at", checked_at)

        validated_last_completed_at = self.last_completed_at
        if validated_last_completed_at is not None:
            validated_last_completed_at = validate_timestamp(
                validated_last_completed_at,
                field_name="last_completed_at",
                error_type=ReflectionEntryError,
            )
            if checked_at < validated_last_completed_at:
                raise ReflectionEntryError(
                    "checked_at must be greater than or equal to last_completed_at."
                )
        object.__setattr__(self, "last_completed_at", validated_last_completed_at)

        next_due_at = validate_timestamp(
            self.next_due_at,
            field_name="next_due_at",
            error_type=ReflectionEntryError,
        )
        object.__setattr__(self, "next_due_at", next_due_at)

        if not isinstance(self.due, bool):
            raise ReflectionEntryError("due must be a bool.")


@dataclass(frozen=True, slots=True)
class ReflectionEntryReceipt:
    """Observable result of one reflection entry-seam invocation."""

    status: ReflectionEntryStatus
    due_check: ReflectionDueCheck
    run_receipt: ReflectionRunReceipt

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _validate_entry_status(self.status))
        if not isinstance(self.due_check, ReflectionDueCheck):
            raise ReflectionEntryError(
                "due_check must be a ReflectionDueCheck instance."
            )
        if not isinstance(self.run_receipt, ReflectionRunReceipt):
            raise ReflectionEntryError(
                "run_receipt must be a ReflectionRunReceipt instance."
            )
        if self.status == "not_due" and self.due_check.due:
            raise ReflectionEntryError(
                "status='not_due' requires a due_check with due=False."
            )
        if self.status in {"due", "dependency_missing"} and not self.due_check.due:
            raise ReflectionEntryError(
                "status='due' or 'dependency_missing' requires due_check.due=True."
            )
        if self.status == "dependency_missing" and not self.run_receipt.missing_dependencies:
            raise ReflectionEntryError(
                "status='dependency_missing' requires missing_dependencies to be reported."
            )
        if self.status != "dependency_missing" and self.run_receipt.missing_dependencies:
            raise ReflectionEntryError(
                "Only status='dependency_missing' may report missing_dependencies."
            )


def check_reflection_due(
    *,
    cadence_hours: float,
    checked_at: datetime,
    last_completed_at: datetime | None = None,
    force_due: bool = False,
) -> ReflectionDueCheck:
    """Return the typed due-check result for one reflection cadence check."""
    normalized_cadence_hours = _validate_positive_hours(
        cadence_hours,
        field_name="cadence_hours",
    )
    validated_checked_at = validate_timestamp(
        checked_at,
        field_name="checked_at",
        error_type=ReflectionEntryError,
    )
    validated_last_completed_at = None
    if last_completed_at is not None:
        validated_last_completed_at = validate_timestamp(
            last_completed_at,
            field_name="last_completed_at",
            error_type=ReflectionEntryError,
        )
        if validated_checked_at < validated_last_completed_at:
            raise ReflectionEntryError(
                "checked_at must be greater than or equal to last_completed_at."
            )
        next_due_at = validated_last_completed_at + timedelta(
            hours=normalized_cadence_hours
        )
    else:
        next_due_at = validated_checked_at

    return ReflectionDueCheck(
        cadence_hours=normalized_cadence_hours,
        checked_at=validated_checked_at,
        last_completed_at=validated_last_completed_at,
        next_due_at=next_due_at,
        due=force_due or validated_checked_at >= next_due_at,
    )


def run_reflection_once(
    config: KernelConfig,
    *,
    checked_at: datetime,
    last_completed_at: datetime | None = None,
    force_due: bool = False,
    eligible_anchor_count: int = 0,
    outcome_context: OutcomeContextPort | None = None,
    trade_context: TradeContextPort | None = None,
    emit: ReflectionEmitter | None = None,
) -> ReflectionEntryReceipt:
    """Run one composition-ready reflection entry check without scanning or writes.

    The current seam only answers three questions explicitly:
    - is reflection due yet?
    - is the required outcome-context dependency present?
    - if due and wired, what config-defined cadence/lookback/horizons apply?

    Future eligibility scanning, packet assembly, and review writing can compose on
    top of this seam without changing the heartbeat-facing entry point.
    """
    if not isinstance(config, KernelConfig):
        raise ReflectionEntryError("config must be a KernelConfig instance.")

    due_check = check_reflection_due(
        cadence_hours=config.reflection_interval_hours,
        checked_at=checked_at,
        last_completed_at=last_completed_at,
        force_due=force_due,
    )

    if not due_check.due:
        receipt = ReflectionEntryReceipt(
            status="not_due",
            due_check=due_check,
            run_receipt=_build_run_receipt(
                config,
                eligible_anchor_count=0,
            ),
        )
        _emit_receipt(emit, receipt)
        return receipt

    missing_dependencies = _missing_dependencies(outcome_context=outcome_context)
    if missing_dependencies:
        receipt = ReflectionEntryReceipt(
            status="dependency_missing",
            due_check=due_check,
            run_receipt=_build_run_receipt(
                config,
                eligible_anchor_count=eligible_anchor_count,
                missing_dependencies=missing_dependencies,
                failure_reason=(
                    "Missing reflection dependencies: "
                    f"{', '.join(missing_dependencies)}."
                ),
            ),
        )
        _emit_receipt(emit, receipt)
        return receipt

    _ = trade_context
    receipt = ReflectionEntryReceipt(
        status="due",
        due_check=due_check,
        run_receipt=_build_run_receipt(
            config,
            eligible_anchor_count=eligible_anchor_count,
        ),
    )
    _emit_receipt(emit, receipt)
    return receipt


def _build_run_receipt(
    config: KernelConfig,
    *,
    eligible_anchor_count: int,
    missing_dependencies: tuple[str, ...] = (),
    failure_reason: str | None = None,
) -> ReflectionRunReceipt:
    return ReflectionRunReceipt(
        cadence_hours=config.reflection_interval_hours,
        lookback_hours=config.reflection_lookback_hours,
        horizons_hours=config.reflection_horizons_hours,
        eligible_anchor_count=eligible_anchor_count,
        missing_dependencies=missing_dependencies,
        failure_reason=failure_reason,
    )


def _missing_dependencies(
    *,
    outcome_context: OutcomeContextPort | None,
) -> tuple[str, ...]:
    if outcome_context is None:
        return ("outcome_context",)
    return ()


def _validate_positive_hours(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReflectionEntryError(
            f"{field_name} must be a positive number of hours."
        )

    normalized = float(value)
    if normalized <= 0:
        raise ReflectionEntryError(
            f"{field_name} must be greater than zero hours."
        )
    return normalized


def _validate_entry_status(value: str) -> ReflectionEntryStatus:
    if value not in {"due", "not_due", "dependency_missing"}:
        raise ReflectionEntryError(
            "status must be one of: due, not_due, dependency_missing."
        )
    return cast(ReflectionEntryStatus, value)


def _emit_due_check(
    emit: ReflectionEmitter | None,
    due_check: ReflectionDueCheck,
) -> None:
    if emit is None:
        return
    if not due_check.due:
        return

    last_completed_at = (
        due_check.last_completed_at.isoformat()
        if due_check.last_completed_at is not None
        else "none"
    )
    emit(
        "reflection due-check: "
        f"due={due_check.due} "
        f"cadence_hours={due_check.cadence_hours} "
        f"checked_at={due_check.checked_at.isoformat()} "
        f"last_completed_at={last_completed_at} "
        f"next_due_at={due_check.next_due_at.isoformat()}"
    )


def _emit_receipt(
    emit: ReflectionEmitter | None,
    receipt: ReflectionEntryReceipt,
) -> None:
    if emit is None:
        return
    if not _should_emit_receipt(receipt):
        return
    _emit_due_check(emit, receipt.due_check)
    _emit_status(emit, receipt)


def _should_emit_receipt(receipt: ReflectionEntryReceipt) -> bool:
    if receipt.status in {"not_due", "dependency_missing"}:
        return receipt.status == "dependency_missing"
    return receipt.run_receipt.eligible_anchor_count > 0


def _emit_status(
    emit: ReflectionEmitter | None,
    receipt: ReflectionEntryReceipt,
) -> None:
    if emit is None:
        return
    if receipt.status == "not_due":
        return

    missing_dependencies = (
        f" missing_dependencies={list(receipt.run_receipt.missing_dependencies)}"
        if receipt.run_receipt.missing_dependencies
        else ""
    )
    emit(
        "reflection entry: "
        f"status={receipt.status} "
        f"eligible_anchor_count={receipt.run_receipt.eligible_anchor_count} "
        f"lookback_hours={receipt.run_receipt.lookback_hours} "
        f"horizons_hours={list(receipt.run_receipt.horizons_hours)}"
        f"{missing_dependencies}"
    )


__all__ = [
    "ReflectionDueCheck",
    "ReflectionEmitter",
    "ReflectionEntryError",
    "ReflectionEntryReceipt",
    "ReflectionEntryStatus",
    "check_reflection_due",
    "run_reflection_once",
]
