"""Project-owned view state change and market-data contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Literal

from ._validators import validate_event_id, validate_target_key, validate_timestamp

ViewState = Literal[
    "flat",
    "weak_long",
    "strong_long",
    "weak_short",
    "strong_short",
]
SystemViewState = Literal[
    "uninitialized",
    "flat",
    "weak_long",
    "strong_long",
    "weak_short",
    "strong_short",
]
ViewDirection = Literal["flat", "long", "short"]
ViewConviction = Literal["none", "weak", "strong"]
ViewStateChangeSourceKind = Literal["pm_execution_sidecar"]
MarketSession = Literal["continuous", "exchange_session"]
ExchangeSessionScope = Literal["regular", "extended"]
ViewCloseReason = Literal[
    "state_changed_to_flat",
    "state_changed_to_opposite_direction",
    "manual_close",
    "target_disabled",
]
SegmentCloseReason = Literal[
    "weight_changed",
    "view_closed",
    "manual_close",
    "target_disabled",
]
TransitionAction = Literal[
    "record_flat",
    "open_view",
    "open_segment",
    "noop",
    "resize_segment",
    "close_view",
    "reverse_view",
    "trigger_reflection",
]
HorizonBaselineStatus = Literal["final", "pending_market_data"]

_VIEW_STATE_TABLE: dict[ViewState, tuple[ViewDirection, ViewConviction, float]] = {
    "flat": ("flat", "none", 0.0),
    "weak_long": ("long", "weak", 0.5),
    "strong_long": ("long", "strong", 1.0),
    "weak_short": ("short", "weak", -0.5),
    "strong_short": ("short", "strong", -1.0),
}
_VIEW_CLOSE_REASONS: frozenset[ViewCloseReason] = frozenset(
    {
        "state_changed_to_flat",
        "state_changed_to_opposite_direction",
        "manual_close",
        "target_disabled",
    }
)
_SEGMENT_CLOSE_REASONS: frozenset[SegmentCloseReason] = frozenset(
    {"weight_changed", "view_closed", "manual_close", "target_disabled"}
)
_REFLECTION_CLOSE_REASONS: frozenset[
    Literal["state_changed_to_flat", "state_changed_to_opposite_direction"]
] = frozenset({"state_changed_to_flat", "state_changed_to_opposite_direction"})


def canonical_view_state_target_weight(state: ViewState) -> float:
    """Return the project-owned target weight for a ViewState."""

    if state not in _VIEW_STATE_TABLE:
        raise ViewStateChangeContractError(
            "state must be one of the five allowed view states."
        )
    return _VIEW_STATE_TABLE[state][2]


class ViewStateChangeContractError(ValueError):
    """Raised when a view-state-change or market-data contract is invalid."""


@dataclass(frozen=True, slots=True)
class ViewStateChange:
    """Minimal persisted quantitative view sidecar for validation."""

    state_change_id: str
    target_key: str
    state: ViewState
    direction: ViewDirection
    conviction: ViewConviction
    target_weight: float
    effective_at: datetime
    source_event_ids: tuple[str, ...]
    rationale_md: str
    source_kind: ViewStateChangeSourceKind
    used_lesson_ids: tuple[str, ...] = ()
    pm_decision_id: str | None = None
    execution_record_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "state_change_id",
            _validate_non_blank(self.state_change_id, "state_change_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ViewStateChangeContractError),
        )
        if self.state not in _VIEW_STATE_TABLE:
            raise ViewStateChangeContractError(
                "state must be one of the five allowed view states."
            )

        expected_direction, expected_conviction, expected_weight = _VIEW_STATE_TABLE[self.state]
        if self.direction != expected_direction:
            raise ViewStateChangeContractError(
                f"direction '{self.direction}' does not match state '{self.state}'."
            )
        if self.conviction != expected_conviction:
            raise ViewStateChangeContractError(
                f"conviction '{self.conviction}' does not match state '{self.state}'."
            )
        if self.target_weight != expected_weight:
            raise ViewStateChangeContractError(
                f"target_weight {self.target_weight} does not match state '{self.state}'."
            )
        if not isfinite(self.target_weight):
            raise ViewStateChangeContractError("target_weight must be finite.")

        object.__setattr__(
            self,
            "effective_at",
            _validate_utc_timestamp(self.effective_at, field_name="effective_at"),
        )
        object.__setattr__(
            self,
            "source_event_ids",
            _validate_event_ids(
                self.source_event_ids,
                field_name="source_event_ids",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self,
            "rationale_md",
            _validate_non_blank(self.rationale_md, "rationale_md"),
        )
        object.__setattr__(
            self,
            "used_lesson_ids",
            _validate_lesson_ids(self.used_lesson_ids),
        )
        if self.source_kind != "pm_execution_sidecar":
            raise ViewStateChangeContractError(
                "source_kind must be pm_execution_sidecar."
            )
        object.__setattr__(self, "source_kind", self.source_kind)
        pm_lineage = {
            "pm_decision_id": self.pm_decision_id,
            "execution_record_id": self.execution_record_id,
        }
        normalized_lineage = {
            field_name: _validate_optional_non_blank(value, field_name)
            for field_name, value in pm_lineage.items()
        }
        required_lineage_fields = (
            "pm_decision_id",
            "execution_record_id",
        )
        missing = [
            field_name
            for field_name in required_lineage_fields
            if normalized_lineage[field_name] is None
        ]
        if missing:
            raise ViewStateChangeContractError(
                "pm_execution_sidecar ViewStateChange requires structured PM lineage ids: "
                f"{missing!r}."
            )
        for field_name, value in normalized_lineage.items():
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class MarketDataBar:
    """One validated UTC OHLCV bar for deterministic validation math."""

    start_at: datetime
    end_at: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    vwap: float | None = None

    def __post_init__(self) -> None:
        normalized_start = _validate_utc_timestamp(self.start_at, field_name="start_at")
        normalized_end = _validate_utc_timestamp(self.end_at, field_name="end_at")
        if normalized_start >= normalized_end:
            raise ViewStateChangeContractError("bar start_at must be before end_at.")
        _validate_positive_finite(self.open_price, field_name="open_price")
        _validate_positive_finite(self.high_price, field_name="high_price")
        _validate_positive_finite(self.low_price, field_name="low_price")
        _validate_positive_finite(self.close_price, field_name="close_price")
        _validate_non_negative_finite(self.volume, field_name="volume")
        if self.vwap is not None:
            _validate_positive_finite(self.vwap, field_name="vwap")
        if not (self.low_price <= self.open_price <= self.high_price):
            raise ViewStateChangeContractError(
                "open_price must be inside [low_price, high_price]."
            )
        if not (self.low_price <= self.close_price <= self.high_price):
            raise ViewStateChangeContractError(
                "close_price must be inside [low_price, high_price]."
            )
        if self.vwap is not None and not (self.low_price <= self.vwap <= self.high_price):
            raise ViewStateChangeContractError(
                "vwap must be inside [low_price, high_price] when present."
            )

        object.__setattr__(self, "start_at", normalized_start)
        object.__setattr__(self, "end_at", normalized_end)


@dataclass(frozen=True, slots=True)
class MarketDataSeries:
    """Validated non-empty, non-overlapping UTC bar series."""

    bars: tuple[MarketDataBar, ...]

    def __post_init__(self) -> None:
        if not self.bars:
            raise ViewStateChangeContractError("market bar series must not be empty.")

        normalized_bars = tuple(self.bars)
        previous_bar: MarketDataBar | None = None
        for bar in normalized_bars:
            if not isinstance(bar, MarketDataBar):
                raise ViewStateChangeContractError("bars must contain only MarketDataBar values.")
            if previous_bar is not None and bar.start_at < previous_bar.end_at:
                raise ViewStateChangeContractError("market bars must not overlap.")
            previous_bar = bar

        object.__setattr__(self, "bars", normalized_bars)


@dataclass(frozen=True, slots=True)
class MarketMapping:
    """Explicit target-to-market mapping for deterministic validation."""

    target_key: str
    market_symbol: str
    market_session: MarketSession
    exchange: str | None
    bar_granularity: str
    exchange_session_scope: ExchangeSessionScope | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ViewStateChangeContractError),
        )
        object.__setattr__(
            self,
            "market_symbol",
            _validate_non_blank(self.market_symbol, "market_symbol"),
        )
        normalized_exchange = None
        if self.exchange is not None:
            normalized_exchange = _validate_non_blank(self.exchange, "exchange")
        normalized_scope = self.exchange_session_scope
        from event_trader.market.session_policy import validate_market_session_contract_fields

        validate_market_session_contract_fields(
            market_session=self.market_session,
            exchange=normalized_exchange,
            exchange_session_scope=normalized_scope,
            error_type=ViewStateChangeContractError,
        )

        object.__setattr__(self, "exchange", normalized_exchange)
        object.__setattr__(self, "exchange_session_scope", normalized_scope)
        object.__setattr__(
            self,
            "bar_granularity",
            _validate_non_blank(self.bar_granularity, "bar_granularity"),
        )


@dataclass(frozen=True, slots=True)
class ViewEpisode:
    """Directional validation episode used as the reflection unit."""

    episode_id: str
    target_key: str
    direction: Literal["long", "short"]
    opened_at: datetime
    opened_by_state_change_id: str
    opened_by_event_ids: tuple[str, ...]
    closed_at: datetime | None = None
    closed_by_state_change_id: str | None = None
    closed_by_event_ids: tuple[str, ...] | None = None
    close_reason: ViewCloseReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _validate_non_blank(self.episode_id, "episode_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ViewStateChangeContractError),
        )
        if self.direction not in {"long", "short"}:
            raise ViewStateChangeContractError("episode direction must be 'long' or 'short'.")
        object.__setattr__(
            self,
            "opened_at",
            _validate_utc_timestamp(self.opened_at, field_name="opened_at"),
        )
        object.__setattr__(
            self,
            "opened_by_state_change_id",
            _validate_non_blank(self.opened_by_state_change_id, "opened_by_state_change_id"),
        )
        object.__setattr__(
            self,
            "opened_by_event_ids",
            _validate_event_ids(
                self.opened_by_event_ids,
                field_name="opened_by_event_ids",
                allow_empty=False,
            ),
        )
        (
            normalized_closed_at,
            normalized_closed_by_state_change_id,
            normalized_closed_by_event_ids,
        ) = _validate_close_fields(
            opened_at=self.opened_at,
            closed_at=self.closed_at,
            closed_by_state_change_id=self.closed_by_state_change_id,
            closed_by_event_ids=self.closed_by_event_ids,
            close_reason=self.close_reason,
            allowed_close_reasons=_VIEW_CLOSE_REASONS,
        )
        object.__setattr__(self, "closed_at", normalized_closed_at)
        object.__setattr__(self, "closed_by_state_change_id", normalized_closed_by_state_change_id)
        object.__setattr__(self, "closed_by_event_ids", normalized_closed_by_event_ids)


@dataclass(frozen=True, slots=True)
class PositionSegment:
    """Directional exposure segment for deterministic return math."""

    segment_id: str
    episode_id: str
    target_key: str
    target_weight: float
    opened_at: datetime
    opened_by_state_change_id: str
    closed_at: datetime | None = None
    closed_by_state_change_id: str | None = None
    close_reason: SegmentCloseReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "segment_id", _validate_non_blank(self.segment_id, "segment_id"))
        object.__setattr__(self, "episode_id", _validate_non_blank(self.episode_id, "episode_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ViewStateChangeContractError),
        )
        if not isfinite(self.target_weight) or self.target_weight == 0.0:
            raise ViewStateChangeContractError("target_weight must be finite and non-zero.")
        object.__setattr__(
            self,
            "opened_at",
            _validate_utc_timestamp(self.opened_at, field_name="opened_at"),
        )
        object.__setattr__(
            self,
            "opened_by_state_change_id",
            _validate_non_blank(self.opened_by_state_change_id, "opened_by_state_change_id"),
        )
        (
            normalized_closed_at,
            normalized_closed_by_state_change_id,
            _,
        ) = _validate_close_fields(
            opened_at=self.opened_at,
            closed_at=self.closed_at,
            closed_by_state_change_id=self.closed_by_state_change_id,
            closed_by_event_ids=None,
            close_reason=self.close_reason,
            allowed_close_reasons=_SEGMENT_CLOSE_REASONS,
        )
        object.__setattr__(self, "closed_at", normalized_closed_at)
        object.__setattr__(self, "closed_by_state_change_id", normalized_closed_by_state_change_id)


@dataclass(frozen=True, slots=True)
class ReflectionTrigger:
    """Trigger packet emitted when a directional episode closes."""

    episode_id: str
    target_key: str
    triggered_at: datetime
    close_reason: Literal["state_changed_to_flat", "state_changed_to_opposite_direction"]
    closed_by_state_change_id: str
    closed_by_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _validate_non_blank(self.episode_id, "episode_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ViewStateChangeContractError),
        )
        object.__setattr__(
            self,
            "triggered_at",
            _validate_utc_timestamp(self.triggered_at, field_name="triggered_at"),
        )
        object.__setattr__(
            self,
            "closed_by_state_change_id",
            _validate_non_blank(self.closed_by_state_change_id, "closed_by_state_change_id"),
        )
        object.__setattr__(
            self,
            "closed_by_event_ids",
            _validate_event_ids(
                self.closed_by_event_ids,
                field_name="closed_by_event_ids",
                allow_empty=False,
            ),
        )
        _validate_in(
            value=self.close_reason,
            allowed_values=_REFLECTION_CLOSE_REASONS,
            field_name="close_reason",
        )


def _validate_close_fields(
    *,
    opened_at: datetime,
    closed_at: datetime | None,
    closed_by_state_change_id: str | None,
    closed_by_event_ids: tuple[str, ...] | None,
    close_reason: str | None,
    allowed_close_reasons: frozenset[str],
) -> tuple[datetime | None, str | None, tuple[str, ...] | None]:
    if closed_at is None:
        if (
            closed_by_state_change_id is not None
            or closed_by_event_ids is not None
            or close_reason is not None
        ):
            raise ViewStateChangeContractError(
                "closed_by_state_change_id, closed_by_event_ids, and "
                "close_reason require closed_at."
            )
        return None, None, None

    normalized_closed_at = _validate_utc_timestamp(closed_at, field_name="closed_at")
    if normalized_closed_at <= opened_at:
        raise ViewStateChangeContractError("closed_at must be later than opened_at.")
    if closed_by_state_change_id is None:
        raise ViewStateChangeContractError(
            "closed_by_state_change_id is required when closed_at is set."
        )
    normalized_closed_by_state_change_id = _validate_non_blank(
        closed_by_state_change_id, "closed_by_state_change_id"
    )
    if close_reason is None:
        raise ViewStateChangeContractError("close_reason is required when closed_at is set.")
    _validate_in(
        value=close_reason,
        allowed_values=allowed_close_reasons,
        field_name="close_reason",
    )
    normalized_closed_by_event_ids: tuple[str, ...] | None = None
    if closed_by_event_ids is not None:
        normalized_closed_by_event_ids = _validate_event_ids(
            closed_by_event_ids,
            field_name="closed_by_event_ids",
            allow_empty=False,
        )
    return (
        normalized_closed_at,
        normalized_closed_by_state_change_id,
        normalized_closed_by_event_ids,
    )


def _validate_event_ids(
    values: tuple[str, ...],
    *,
    field_name: str,
    allow_empty: bool,
) -> tuple[str, ...]:
    normalized_values = tuple(values)
    if not allow_empty and not normalized_values:
        raise ViewStateChangeContractError(f"{field_name} must not be empty.")

    seen: set[str] = set()
    for event_id in normalized_values:
        validate_event_id(event_id, error_type=ViewStateChangeContractError)
        if event_id in seen:
            raise ViewStateChangeContractError(
                f"{field_name} must not contain duplicate event_ids."
            )
        seen.add(event_id)

    return normalized_values


def _validate_lesson_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized_values = tuple(values)
    seen: set[str] = set()
    for lesson_id in normalized_values:
        if not isinstance(lesson_id, str):
            raise ViewStateChangeContractError("used_lesson_ids must contain only strings.")
        normalized = lesson_id.strip()
        if not normalized:
            raise ViewStateChangeContractError("used_lesson_ids must not contain blank values.")
        if normalized != lesson_id:
            raise ViewStateChangeContractError(
                "used_lesson_ids must not include leading or trailing whitespace."
            )
        if normalized in seen:
            raise ViewStateChangeContractError("used_lesson_ids must not contain duplicates.")
        seen.add(normalized)
    return normalized_values


def _validate_utc_timestamp(value: datetime, *, field_name: str) -> datetime:
    normalized = validate_timestamp(
        value,
        field_name=field_name,
        error_type=ViewStateChangeContractError,
    )
    if normalized.utcoffset() != UTC.utcoffset(None):
        raise ViewStateChangeContractError(f"{field_name} must be timezone-aware UTC.")
    return normalized.astimezone(UTC)


def _validate_non_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ViewStateChangeContractError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ViewStateChangeContractError(f"{field_name} must not be blank.")
    if normalized != value:
        raise ViewStateChangeContractError(
            f"{field_name} must not include leading or trailing whitespace."
        )
    return normalized


def _validate_optional_non_blank(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ViewStateChangeContractError(f"{field_name} must be a string when set.")
    return _validate_non_blank(value, field_name)


def _validate_positive_finite(value: float, *, field_name: str) -> None:
    if not isfinite(value) or value <= 0.0:
        raise ViewStateChangeContractError(
            f"{field_name} must be a finite value greater than zero."
        )


def _validate_non_negative_finite(value: float, *, field_name: str) -> None:
    if not isfinite(value) or value < 0.0:
        raise ViewStateChangeContractError(
            f"{field_name} must be a finite value greater than or equal to zero."
        )


def _validate_in(
    *,
    value: str,
    allowed_values: frozenset[str],
    field_name: str,
) -> None:
    if value not in allowed_values:
        allowed = ", ".join(sorted(allowed_values))
        raise ViewStateChangeContractError(f"{field_name} must be one of: {allowed}.")


__all__ = [
    "ViewStateChange",
    "HorizonBaselineStatus",
    "MarketDataBar",
    "MarketDataSeries",
    "MarketMapping",
    "MarketSession",
    "ExchangeSessionScope",
    "PositionSegment",
    "ReflectionTrigger",
    "SegmentCloseReason",
    "SystemViewState",
    "TransitionAction",
    "ViewStateChangeContractError",
    "ViewStateChangeSourceKind",
    "ViewConviction",
    "ViewDirection",
    "ViewState",
    "ViewCloseReason",
    "ViewEpisode",
    "canonical_view_state_target_weight",
]

