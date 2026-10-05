"""Thin cadence decisions for cross-review weekly/monthly pattern windows.

This seam owns complete wall-clock window selection and sample gating.
It does not aggregate completed reflections, write shared learning, or wire
into the runtime loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast

from event_trader.contracts._validators import normalize_content, validate_timestamp

type CadenceReviewWindowKind = Literal["weekly", "monthly"]


class ReflectionCadenceReviewError(ValueError):
    """Raised when cadence-review window inputs or receipts are malformed."""


@dataclass(frozen=True, slots=True)
class CadenceReviewWindowReceipt:
    """Thin decision receipt for one weekly/monthly cadence-review window."""

    window_kind: CadenceReviewWindowKind
    window_start: datetime
    window_end: datetime
    review_count: int
    min_required_reviews: int
    skipped: bool
    skip_reason: str | None
    decision_summary: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "window_kind",
            _validate_window_kind(self.window_kind),
        )
        window_start = validate_timestamp(
            self.window_start,
            field_name="window_start",
            error_type=ReflectionCadenceReviewError,
        )
        window_end = validate_timestamp(
            self.window_end,
            field_name="window_end",
            error_type=ReflectionCadenceReviewError,
        )
        if window_end <= window_start:
            raise ReflectionCadenceReviewError(
                "window_end must be later than window_start."
            )
        object.__setattr__(self, "window_start", window_start)
        object.__setattr__(self, "window_end", window_end)

        object.__setattr__(
            self,
            "review_count",
            _validate_non_negative_int(self.review_count, field_name="review_count"),
        )
        object.__setattr__(
            self,
            "min_required_reviews",
            _validate_positive_int(
                self.min_required_reviews,
                field_name="min_required_reviews",
            ),
        )
        if not isinstance(self.skipped, bool):
            raise ReflectionCadenceReviewError("skipped must be a boolean.")
        if self.skip_reason is not None:
            object.__setattr__(
                self,
                "skip_reason",
                normalize_content(
                    self.skip_reason,
                    field_name="skip_reason",
                    error_type=ReflectionCadenceReviewError,
                ),
            )
        object.__setattr__(
            self,
            "decision_summary",
            normalize_content(
                self.decision_summary,
                field_name="decision_summary",
                error_type=ReflectionCadenceReviewError,
            ),
        )
        if self.skipped and self.skip_reason is None:
            raise ReflectionCadenceReviewError(
                "skip_reason is required when skipped=True."
            )
        if not self.skipped and self.skip_reason is not None:
            raise ReflectionCadenceReviewError(
                "skip_reason must be None when skipped=False."
            )
        if self.skipped and self.review_count >= self.min_required_reviews:
            raise ReflectionCadenceReviewError(
                "skipped=True requires review_count to be below min_required_reviews."
            )
        if not self.skipped and self.review_count < self.min_required_reviews:
            raise ReflectionCadenceReviewError(
                "skipped=False requires review_count to be greater than or equal to "
                "min_required_reviews."
            )


def decide_cadence_review_window(
    *,
    window_kind: CadenceReviewWindowKind,
    checked_at: datetime,
    review_count: int,
    min_required_reviews: int,
) -> CadenceReviewWindowReceipt:
    """Return the complete weekly/monthly cadence window and sample-gate result."""

    normalized_window_kind = _validate_window_kind(window_kind)
    validated_checked_at = validate_timestamp(
        checked_at,
        field_name="checked_at",
        error_type=ReflectionCadenceReviewError,
    )
    normalized_review_count = _validate_non_negative_int(
        review_count,
        field_name="review_count",
    )
    normalized_min_required_reviews = _validate_positive_int(
        min_required_reviews,
        field_name="min_required_reviews",
    )

    window_start, window_end = select_complete_cadence_window(
        window_kind=normalized_window_kind,
        checked_at=validated_checked_at,
    )

    if normalized_review_count < normalized_min_required_reviews:
        skip_reason = (
            "review_count="
            f"{normalized_review_count} is below min_required_reviews="
            f"{normalized_min_required_reviews} for the complete "
            f"{normalized_window_kind} cadence-review window."
        )
        summary = (
            f"Skipped {normalized_window_kind} cadence review for the complete "
            f"window because there are not enough completed reflections yet."
        )
        return CadenceReviewWindowReceipt(
            window_kind=normalized_window_kind,
            window_start=window_start,
            window_end=window_end,
            review_count=normalized_review_count,
            min_required_reviews=normalized_min_required_reviews,
            skipped=True,
            skip_reason=skip_reason,
            decision_summary=summary,
        )

    return CadenceReviewWindowReceipt(
        window_kind=normalized_window_kind,
        window_start=window_start,
        window_end=window_end,
        review_count=normalized_review_count,
        min_required_reviews=normalized_min_required_reviews,
        skipped=False,
        skip_reason=None,
        decision_summary=(
            f"Ready to review the complete {normalized_window_kind} cadence window."
        ),
    )


def select_complete_cadence_window(
    *,
    window_kind: CadenceReviewWindowKind,
    checked_at: datetime,
) -> tuple[datetime, datetime]:
    """Return the most recent complete weekly/monthly wall-clock window."""

    day_boundary = datetime(
        checked_at.year,
        checked_at.month,
        checked_at.day,
        tzinfo=checked_at.tzinfo,
    )
    if window_kind == "weekly":
        return day_boundary - timedelta(days=7), day_boundary
    if window_kind == "monthly":
        month_boundary = datetime(
            checked_at.year,
            checked_at.month,
            1,
            tzinfo=checked_at.tzinfo,
        )
        previous_month_last_day = month_boundary - timedelta(days=1)
        previous_month_start = datetime(
            previous_month_last_day.year,
            previous_month_last_day.month,
            1,
            tzinfo=checked_at.tzinfo,
        )
        return previous_month_start, month_boundary
    raise ReflectionCadenceReviewError(
        "window_kind must be one of: weekly, monthly."
    )


def _validate_non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReflectionCadenceReviewError(f"{field_name} must be an integer >= 0.")
    if value < 0:
        raise ReflectionCadenceReviewError(f"{field_name} must be an integer >= 0.")
    return value


def _validate_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReflectionCadenceReviewError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise ReflectionCadenceReviewError(f"{field_name} must be a positive integer.")
    return value


def _validate_window_kind(value: str) -> CadenceReviewWindowKind:
    if value not in {"weekly", "monthly"}:
        raise ReflectionCadenceReviewError(
            "window_kind must be one of: weekly, monthly."
        )
    return cast(CadenceReviewWindowKind, value)


__all__ = [
    "CadenceReviewWindowKind",
    "CadenceReviewWindowReceipt",
    "ReflectionCadenceReviewError",
    "decide_cadence_review_window",
    "select_complete_cadence_window",
]
