"""Stable read-model contracts for position monitoring."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Literal, cast

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.contracts.view_state_change import ViewState, canonical_view_state_target_weight

PositionLineKey = Literal["pm_pipeline", "buy_hold", "analysis_direct_shadow"]
PositionLineRole = Literal["actual", "baseline", "counterfactual"]
PositionMarkerKind = Literal["pm_decision", "analysis_assessment"]
PositionMarkerLane = Literal["pm", "analysis"]
PositionMarkerShape = Literal["circle", "square", "arrowUp", "arrowDown"]
PositionMarkerPosition = Literal["aboveBar", "belowBar", "inBar"]

_VIEW_STATES: tuple[ViewState, ...] = (
    "flat",
    "weak_long",
    "strong_long",
    "weak_short",
    "strong_short",
)


class PositionMonitoringContractError(ValueError):
    """Raised when a position monitoring read model is malformed."""


def canonical_state_for_weight(weight: float) -> ViewState | None:
    """Resolve a canonical view state from a canonical target weight."""

    if isinstance(weight, bool) or not isinstance(weight, int | float):
        raise PositionMonitoringContractError("weight must be numeric.")
    normalized = float(weight)
    for state in _VIEW_STATES:
        if canonical_view_state_target_weight(state) == normalized:
            return state
    return None


@dataclass(frozen=True, slots=True)
class PositionMonitoringStatus:
    code: str
    explanation: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _require_non_blank(self.code, "code"))
        object.__setattr__(
            self,
            "explanation",
            _require_non_blank(self.explanation, "explanation"),
        )


@dataclass(frozen=True, slots=True)
class CurrentPortfolioStateSnapshot:
    status: PositionMonitoringStatus
    state: ViewState | None
    target_weight: float | None
    updated_at: datetime | None
    source_pm_decision_id: str | None = None
    source_execution_record_id: str | None = None
    decision_episode_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PositionMonitoringStatus):
            raise PositionMonitoringContractError("status must be a PositionMonitoringStatus.")
        state = _validate_optional_view_state(self.state, "state")
        weight = _validate_optional_weight(self.target_weight, "target_weight")
        if (state is None) != (weight is None):
            raise PositionMonitoringContractError(
                "state and target_weight must both be set or both be None."
            )
        if state is not None and weight != canonical_view_state_target_weight(state):
            raise PositionMonitoringContractError(
                "target_weight must match the canonical state weight."
            )
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "target_weight", weight)
        if self.updated_at is not None:
            object.__setattr__(
                self,
                "updated_at",
                validate_timestamp(
                    self.updated_at,
                    field_name="updated_at",
                    error_type=PositionMonitoringContractError,
                ),
            )
        for field_name in (
            "source_pm_decision_id",
            "source_execution_record_id",
            "decision_episode_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_non_blank(getattr(self, field_name), field_name),
            )


@dataclass(frozen=True, slots=True)
class PositionMonitoringLine:
    key: PositionLineKey
    label: str
    role: PositionLineRole
    status: PositionMonitoringStatus

    def __post_init__(self) -> None:
        if self.key not in {"pm_pipeline", "buy_hold", "analysis_direct_shadow"}:
            raise PositionMonitoringContractError("line key is invalid.")
        object.__setattr__(self, "label", _require_non_blank(self.label, "label"))
        if self.role not in {"actual", "baseline", "counterfactual"}:
            raise PositionMonitoringContractError("line role is invalid.")
        if not isinstance(self.status, PositionMonitoringStatus):
            raise PositionMonitoringContractError("status must be a PositionMonitoringStatus.")


@dataclass(frozen=True, slots=True)
class PositionMonitoringPoint:
    time: datetime
    price: float
    pm_pipeline_value: float | None
    buy_hold_value: float | None
    analysis_direct_shadow_value: float | None
    pm_target_weight: float | None
    analysis_shadow_target_weight: float | None
    pm_state: ViewState | None
    analysis_shadow_state: ViewState | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "time",
            validate_timestamp(
                self.time,
                field_name="time",
                error_type=PositionMonitoringContractError,
            ),
        )
        object.__setattr__(self, "price", _validate_finite_float(self.price, "price"))
        for field_name in (
            "pm_pipeline_value",
            "buy_hold_value",
            "analysis_direct_shadow_value",
            "pm_target_weight",
            "analysis_shadow_target_weight",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_optional_weight(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "pm_state",
            _validate_optional_view_state(self.pm_state, "pm_state"),
        )
        object.__setattr__(
            self,
            "analysis_shadow_state",
            _validate_optional_view_state(self.analysis_shadow_state, "analysis_shadow_state"),
        )
        _validate_optional_state_weight_pair(
            state=self.pm_state,
            weight=self.pm_target_weight,
            state_field_name="pm_state",
            weight_field_name="pm_target_weight",
        )
        _validate_optional_state_weight_pair(
            state=self.analysis_shadow_state,
            weight=self.analysis_shadow_target_weight,
            state_field_name="analysis_shadow_state",
            weight_field_name="analysis_shadow_target_weight",
        )


@dataclass(frozen=True, slots=True)
class PositionMonitoringMarker:
    marker_id: str
    kind: PositionMarkerKind
    lane: PositionMarkerLane
    label: str
    time: datetime
    business_at: datetime
    state: ViewState
    target_weight: float
    price: float
    line_value: float | None
    shape: PositionMarkerShape
    position: PositionMarkerPosition
    text: str
    source_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "marker_id", _require_non_blank(self.marker_id, "marker_id"))
        if self.kind not in {"pm_decision", "analysis_assessment"}:
            raise PositionMonitoringContractError("kind is invalid.")
        if self.lane not in {"pm", "analysis"}:
            raise PositionMonitoringContractError("lane is invalid.")
        object.__setattr__(self, "label", _require_non_blank(self.label, "label"))
        object.__setattr__(
            self,
            "time",
            validate_timestamp(
                self.time,
                field_name="time",
                error_type=PositionMonitoringContractError,
            ),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PositionMonitoringContractError,
            ),
        )
        state = _validate_optional_view_state(self.state, "state")
        if state is None:
            raise PositionMonitoringContractError("state must be set.")
        object.__setattr__(self, "state", state)
        weight = _validate_finite_float(self.target_weight, "target_weight")
        if weight != canonical_view_state_target_weight(state):
            raise PositionMonitoringContractError(
                "target_weight must match canonical weight for state."
            )
        object.__setattr__(self, "target_weight", weight)
        object.__setattr__(self, "price", _validate_finite_float(self.price, "price"))
        object.__setattr__(
            self,
            "line_value",
            _validate_optional_weight(self.line_value, "line_value"),
        )
        if self.shape not in {"circle", "square", "arrowUp", "arrowDown"}:
            raise PositionMonitoringContractError("shape is invalid.")
        if self.position not in {"aboveBar", "belowBar", "inBar"}:
            raise PositionMonitoringContractError("position is invalid.")
        object.__setattr__(self, "text", _require_non_blank(self.text, "text"))
        object.__setattr__(self, "source_id", _require_non_blank(self.source_id, "source_id"))


@dataclass(frozen=True, slots=True)
class PMDecisionSummary:
    decision_id: str
    business_at: datetime
    requested_state: ViewState
    requested_target_weight: float
    execution_required: bool
    pm_review_request_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_id",
            _require_non_blank(self.decision_id, "decision_id"),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PositionMonitoringContractError,
            ),
        )
        state = _validate_optional_view_state(self.requested_state, "requested_state")
        if state is None:
            raise PositionMonitoringContractError("requested_state must be set.")
        object.__setattr__(self, "requested_state", state)
        weight = _validate_finite_float(
            self.requested_target_weight,
            "requested_target_weight",
        )
        if weight != canonical_view_state_target_weight(state):
            raise PositionMonitoringContractError(
                "requested_target_weight must match requested_state."
            )
        object.__setattr__(self, "requested_target_weight", weight)
        if not isinstance(self.execution_required, bool):
            raise PositionMonitoringContractError("execution_required must be a boolean.")
        object.__setattr__(
            self,
            "pm_review_request_id",
            _optional_non_blank(self.pm_review_request_id, "pm_review_request_id"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    execution_record_id: str
    business_at: datetime
    status: str
    pm_decision_id: str
    requested_target_weight: float
    executed_at: datetime | None = None
    target_weight: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "execution_record_id",
            _require_non_blank(self.execution_record_id, "execution_record_id"),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PositionMonitoringContractError,
            ),
        )
        object.__setattr__(self, "status", _require_non_blank(self.status, "status"))
        object.__setattr__(
            self,
            "pm_decision_id",
            _require_non_blank(self.pm_decision_id, "pm_decision_id"),
        )
        object.__setattr__(
            self,
            "requested_target_weight",
            _validate_finite_float(
                self.requested_target_weight,
                "requested_target_weight",
            ),
        )
        if self.executed_at is not None:
            object.__setattr__(
                self,
                "executed_at",
                validate_timestamp(
                    self.executed_at,
                    field_name="executed_at",
                    error_type=PositionMonitoringContractError,
                ),
            )
        object.__setattr__(
            self,
            "target_weight",
            _validate_optional_weight(self.target_weight, "target_weight"),
        )


@dataclass(frozen=True, slots=True)
class AnalysisShadowSourceSummary:
    assessment_id: str
    business_at: datetime
    as_if_flat_state: ViewState
    target_weight: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assessment_id",
            _require_non_blank(self.assessment_id, "assessment_id"),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PositionMonitoringContractError,
            ),
        )
        state = _validate_optional_view_state(self.as_if_flat_state, "as_if_flat_state")
        if state is None:
            raise PositionMonitoringContractError("as_if_flat_state must be set.")
        object.__setattr__(self, "as_if_flat_state", state)
        weight = _validate_finite_float(self.target_weight, "target_weight")
        if weight != canonical_view_state_target_weight(state):
            raise PositionMonitoringContractError(
                "target_weight must match as_if_flat_state."
            )
        object.__setattr__(self, "target_weight", weight)


@dataclass(frozen=True, slots=True)
class PositionMonitoringAudit:
    source_paths: tuple[Path, ...]
    counts: Mapping[str, int]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_paths",
            tuple(
                Path(path).resolve(strict=False)
                for path in self.source_paths
            ),
        )
        if not isinstance(self.counts, Mapping):
            raise PositionMonitoringContractError("counts must be a mapping.")
        normalized_counts: dict[str, int] = {}
        for key, value in self.counts.items():
            normalized_key = _require_non_blank(key, "counts key")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PositionMonitoringContractError(
                    "counts values must be non-negative integers."
                )
            normalized_counts[normalized_key] = value
        object.__setattr__(self, "counts", normalized_counts)
        object.__setattr__(
            self,
            "warnings",
            tuple(_require_non_blank(item, "warnings") for item in self.warnings),
        )


@dataclass(frozen=True, slots=True)
class PositionMonitoringSnapshot:
    target_key: str
    generated_at: datetime
    current_portfolio_state: CurrentPortfolioStateSnapshot
    market_data_status: PositionMonitoringStatus
    comparison_status: PositionMonitoringStatus
    market_symbol: str | None
    bar_granularity: str | None
    market_data_path: Path | None
    base_value: float
    lines: tuple[PositionMonitoringLine, ...]
    points: tuple[PositionMonitoringPoint, ...]
    markers: tuple[PositionMonitoringMarker, ...] = ()
    latest_pm_decision: PMDecisionSummary | None = None
    latest_execution: ExecutionSummary | None = None
    latest_analysis_shadow_source: AnalysisShadowSourceSummary | None = None
    notes: tuple[str, ...] = ()
    audit: PositionMonitoringAudit | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=PositionMonitoringContractError,
            ),
        )
        object.__setattr__(
            self,
            "generated_at",
            validate_timestamp(
                self.generated_at,
                field_name="generated_at",
                error_type=PositionMonitoringContractError,
            ),
        )
        if not isinstance(self.current_portfolio_state, CurrentPortfolioStateSnapshot):
            raise PositionMonitoringContractError(
                "current_portfolio_state must be a CurrentPortfolioStateSnapshot."
            )
        if not isinstance(self.market_data_status, PositionMonitoringStatus):
            raise PositionMonitoringContractError(
                "market_data_status must be a PositionMonitoringStatus."
            )
        if not isinstance(self.comparison_status, PositionMonitoringStatus):
            raise PositionMonitoringContractError(
                "comparison_status must be a PositionMonitoringStatus."
            )
        object.__setattr__(
            self,
            "market_symbol",
            _optional_non_blank(self.market_symbol, "market_symbol"),
        )
        object.__setattr__(
            self,
            "bar_granularity",
            _optional_non_blank(self.bar_granularity, "bar_granularity"),
        )
        if self.market_data_path is not None:
            object.__setattr__(
                self,
                "market_data_path",
                Path(self.market_data_path).resolve(strict=False),
            )
        object.__setattr__(
            self,
            "base_value",
            _validate_finite_float(self.base_value, "base_value"),
        )
        if self.base_value <= 0.0:
            raise PositionMonitoringContractError("base_value must be greater than zero.")
        for line in self.lines:
            if not isinstance(line, PositionMonitoringLine):
                raise PositionMonitoringContractError(
                    "lines must contain PositionMonitoringLine values."
                )
        for point in self.points:
            if not isinstance(point, PositionMonitoringPoint):
                raise PositionMonitoringContractError(
                    "points must contain PositionMonitoringPoint values."
                )
        for marker in self.markers:
            if not isinstance(marker, PositionMonitoringMarker):
                raise PositionMonitoringContractError(
                    "markers must contain PositionMonitoringMarker values."
                )
        if self.latest_pm_decision is not None and not isinstance(
            self.latest_pm_decision, PMDecisionSummary
        ):
            raise PositionMonitoringContractError(
                "latest_pm_decision must be a PMDecisionSummary."
            )
        if self.latest_execution is not None and not isinstance(
            self.latest_execution, ExecutionSummary
        ):
            raise PositionMonitoringContractError(
                "latest_execution must be an ExecutionSummary."
            )
        if self.latest_analysis_shadow_source is not None and not isinstance(
            self.latest_analysis_shadow_source,
            AnalysisShadowSourceSummary,
        ):
            raise PositionMonitoringContractError(
                "latest_analysis_shadow_source must be an AnalysisShadowSourceSummary."
            )
        object.__setattr__(
            self,
            "notes",
            tuple(_require_non_blank(item, "notes") for item in self.notes),
        )
        if self.audit is not None and not isinstance(self.audit, PositionMonitoringAudit):
            raise PositionMonitoringContractError(
                "audit must be a PositionMonitoringAudit."
            )


def _validate_optional_state_weight_pair(
    *,
    state: ViewState | None,
    weight: float | None,
    state_field_name: str,
    weight_field_name: str,
) -> None:
    if (state is None) != (weight is None):
        raise PositionMonitoringContractError(
            f"{state_field_name} and {weight_field_name} must be set together."
        )
    if state is not None and weight != canonical_view_state_target_weight(state):
        raise PositionMonitoringContractError(
            f"{weight_field_name} must match canonical weight for {state_field_name}."
        )


def _validate_optional_view_state(value: object, field_name: str) -> ViewState | None:
    if value is None:
        return None
    if value not in _VIEW_STATES:
        raise PositionMonitoringContractError(f"{field_name} is not a supported view state.")
    return cast(ViewState, value)


def _validate_finite_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PositionMonitoringContractError(f"{field_name} must be numeric.")
    normalized = float(value)
    if not isfinite(normalized):
        raise PositionMonitoringContractError(f"{field_name} must be finite.")
    return normalized


def _validate_optional_weight(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    return _validate_finite_float(value, field_name)


def _require_non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PositionMonitoringContractError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _optional_non_blank(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_non_blank(value, field_name)


__all__ = [
    "AnalysisShadowSourceSummary",
    "CurrentPortfolioStateSnapshot",
    "ExecutionSummary",
    "PMDecisionSummary",
    "PositionMarkerKind",
    "PositionMarkerLane",
    "PositionMarkerPosition",
    "PositionMarkerShape",
    "PositionLineKey",
    "PositionLineRole",
    "PositionMonitoringAudit",
    "PositionMonitoringContractError",
    "PositionMonitoringLine",
    "PositionMonitoringMarker",
    "PositionMonitoringPoint",
    "PositionMonitoringSnapshot",
    "PositionMonitoringStatus",
    "canonical_state_for_weight",
]
