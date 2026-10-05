"""Typed contracts for post-episode counterfactual evaluation reports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Literal, cast

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.execution.contracts import (
    MarketDataProvenanceReceipt,
    execution_contract_hash,
    parse_market_data_provenance_receipt,
)

CounterfactualBaselineType = Literal[
    "no_trade",
    "buy_and_hold_proxy",
    "delayed_entry",
    "full_hold",
    "random_exit",
    "rule_based_exit",
]
CounterfactualBaselineStatus = Literal["available", "unavailable"]
QualityStatus = Literal["available", "unavailable", "not_evaluated"]

REQUIRED_BASELINE_TYPES: tuple[CounterfactualBaselineType, ...] = (
    "no_trade",
    "buy_and_hold_proxy",
    "delayed_entry",
    "full_hold",
    "random_exit",
    "rule_based_exit",
)
_BASELINE_TYPES: frozenset[str] = frozenset(REQUIRED_BASELINE_TYPES)
_BASELINE_STATUSES: frozenset[str] = frozenset({"available", "unavailable"})
_QUALITY_STATUSES: frozenset[str] = frozenset(
    {"available", "unavailable", "not_evaluated"}
)


class CounterfactualContractError(ValueError):
    """Raised when counterfactual evaluation contracts are malformed."""


@dataclass(frozen=True, slots=True)
class CounterfactualBaselineResult:
    """One deterministic baseline result for a completed episode."""

    counterfactual_id: str
    episode_id: str
    decision_episode_id: str | None
    pm_decision_id: str | None
    baseline_type: CounterfactualBaselineType
    baseline_return: float | None
    delta_vs_actual: float | None
    entry_bar_start_at: datetime | None
    exit_bar_start_at: datetime | None
    status: CounterfactualBaselineStatus
    reason: str | None
    provenance_hash: str
    assumptions: dict[str, object]
    market_data_hash: str | None
    result_metrics: dict[str, object]
    comparison_to_actual: dict[str, object]
    computed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "counterfactual_id",
            _validate_non_blank(self.counterfactual_id, "counterfactual_id"),
        )
        object.__setattr__(self, "episode_id", _validate_non_blank(self.episode_id, "episode_id"))
        if self.decision_episode_id is not None:
            object.__setattr__(
                self,
                "decision_episode_id",
                _validate_non_blank(self.decision_episode_id, "decision_episode_id"),
            )
        if self.pm_decision_id is not None:
            object.__setattr__(
                self,
                "pm_decision_id",
                _validate_non_blank(self.pm_decision_id, "pm_decision_id"),
            )
        if self.baseline_type not in _BASELINE_TYPES:
            raise CounterfactualContractError(
                "baseline_type must be a known deterministic baseline."
            )
        if self.status not in _BASELINE_STATUSES:
            raise CounterfactualContractError("status must be available or unavailable.")
        object.__setattr__(
            self,
            "provenance_hash",
            _validate_non_blank(self.provenance_hash, "provenance_hash"),
        )
        if self.market_data_hash is not None:
            object.__setattr__(
                self,
                "market_data_hash",
                _validate_non_blank(self.market_data_hash, "market_data_hash"),
            )
        object.__setattr__(
            self,
            "assumptions",
            _validate_object_dict(self.assumptions, "assumptions"),
        )
        object.__setattr__(
            self,
            "result_metrics",
            _validate_object_dict(self.result_metrics, "result_metrics"),
        )
        object.__setattr__(
            self,
            "comparison_to_actual",
            _validate_object_dict(self.comparison_to_actual, "comparison_to_actual"),
        )

        if self.status == "available":
            _validate_finite_required(self.baseline_return, "baseline_return")
            _validate_finite_required(self.delta_vs_actual, "delta_vs_actual")
            if self.reason is not None:
                raise CounterfactualContractError(
                    "available baselines must not include an unavailable reason."
                )
        else:
            if self.baseline_return is not None or self.delta_vs_actual is not None:
                raise CounterfactualContractError(
                    "unavailable baselines must not include return values."
                )
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise CounterfactualContractError(
                    "unavailable baselines require a non-blank reason."
                )
            object.__setattr__(self, "reason", self.reason.strip())

        object.__setattr__(
            self,
            "entry_bar_start_at",
            _optional_timestamp(self.entry_bar_start_at, "entry_bar_start_at"),
        )
        object.__setattr__(
            self,
            "exit_bar_start_at",
            _optional_timestamp(self.exit_bar_start_at, "exit_bar_start_at"),
        )
        object.__setattr__(
            self,
            "computed_at",
            validate_timestamp(
                self.computed_at,
                field_name="computed_at",
                error_type=CounterfactualContractError,
            ),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "counterfactual_id": self.counterfactual_id,
            "episode_id": self.episode_id,
            "decision_episode_id": self.decision_episode_id,
            "pm_decision_id": self.pm_decision_id,
            "baseline_type": self.baseline_type,
            "baseline_return": self.baseline_return,
            "delta_vs_actual": self.delta_vs_actual,
            "entry_bar_start_at": _timestamp_payload(self.entry_bar_start_at),
            "exit_bar_start_at": _timestamp_payload(self.exit_bar_start_at),
            "status": self.status,
            "reason": self.reason,
            "provenance_hash": self.provenance_hash,
            "assumptions": self.assumptions,
            "market_data_hash": self.market_data_hash,
            "result_metrics": self.result_metrics,
            "comparison_to_actual": self.comparison_to_actual,
            "computed_at": self.computed_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class CounterfactualEpisodeMetrics:
    """Deterministic metrics for the actual episode outcome."""

    episode_id: str
    target_key: str
    return_pct: float
    max_drawdown_pct: float | None
    time_to_max_gain_minutes: float | None
    time_to_max_loss_minutes: float | None
    holding_period_minutes: float
    bar_count: int
    hit_by_horizon: bool
    return_vs_baseline: dict[str, float | None]

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _validate_non_blank(self.episode_id, "episode_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CounterfactualContractError),
        )
        object.__setattr__(self, "return_pct", _validate_finite(self.return_pct, "return_pct"))
        if self.max_drawdown_pct is not None:
            object.__setattr__(
                self,
                "max_drawdown_pct",
                _validate_finite(self.max_drawdown_pct, "max_drawdown_pct"),
            )
        if self.time_to_max_gain_minutes is not None:
            object.__setattr__(
                self,
                "time_to_max_gain_minutes",
                _validate_non_negative_finite(
                    self.time_to_max_gain_minutes,
                    "time_to_max_gain_minutes",
                ),
            )
        if self.time_to_max_loss_minutes is not None:
            object.__setattr__(
                self,
                "time_to_max_loss_minutes",
                _validate_non_negative_finite(
                    self.time_to_max_loss_minutes,
                    "time_to_max_loss_minutes",
                ),
            )
        object.__setattr__(
            self,
            "holding_period_minutes",
            _validate_non_negative_finite(
                self.holding_period_minutes,
                "holding_period_minutes",
            ),
        )
        if isinstance(self.bar_count, bool) or not isinstance(self.bar_count, int):
            raise CounterfactualContractError("bar_count must be an integer.")
        if self.bar_count < 0:
            raise CounterfactualContractError("bar_count must be non-negative.")
        if not isinstance(self.hit_by_horizon, bool):
            raise CounterfactualContractError("hit_by_horizon must be a bool.")
        normalized: dict[str, float | None] = {}
        if not isinstance(self.return_vs_baseline, dict):
            raise CounterfactualContractError("return_vs_baseline must be a dict.")
        for key, value in self.return_vs_baseline.items():
            normalized[_validate_non_blank(key, "return_vs_baseline key")] = (
                None if value is None else _validate_finite(value, "return_vs_baseline")
            )
        object.__setattr__(self, "return_vs_baseline", normalized)

    def to_json_payload(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "return_pct": self.return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "time_to_max_gain_minutes": self.time_to_max_gain_minutes,
            "time_to_max_loss_minutes": self.time_to_max_loss_minutes,
            "holding_period_minutes": self.holding_period_minutes,
            "bar_count": self.bar_count,
            "hit_by_horizon": self.hit_by_horizon,
            "return_vs_baseline": self.return_vs_baseline,
        }


@dataclass(frozen=True, slots=True)
class DecisionQualityAttribution:
    """Deterministic, non-LLM decision quality attribution."""

    episode_id: str
    target_key: str
    entry_quality: QualityStatus
    exit_quality: QualityStatus
    execution_quality: QualityStatus
    reasons: dict[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _validate_non_blank(self.episode_id, "episode_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CounterfactualContractError),
        )
        for field_name in (
            "entry_quality",
            "exit_quality",
            "execution_quality",
        ):
            value = getattr(self, field_name)
            if value not in _QUALITY_STATUSES:
                raise CounterfactualContractError(
                    f"{field_name} must be available, unavailable, or not_evaluated."
                )
        object.__setattr__(
            self,
            "reasons",
            _validate_object_dict(self.reasons or {}, "reasons"),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "entry_quality": self.entry_quality,
            "exit_quality": self.exit_quality,
            "execution_quality": self.execution_quality,
            "reasons": self.reasons or {},
        }


@dataclass(frozen=True, slots=True)
class CounterfactualEvaluationReport:
    """Post-episode evaluation artifact comparing actual return to baselines."""

    episode_id: str
    target_key: str
    actual_return: float
    market_provenance: MarketDataProvenanceReceipt
    baseline_results: tuple[CounterfactualBaselineResult, ...]
    created_at: datetime
    episode_metrics: CounterfactualEpisodeMetrics | None = None
    decision_quality_attribution: DecisionQualityAttribution | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _validate_non_blank(self.episode_id, "episode_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CounterfactualContractError),
        )
        object.__setattr__(
            self,
            "actual_return",
            _validate_finite(self.actual_return, "actual_return"),
        )
        if not isinstance(self.market_provenance, MarketDataProvenanceReceipt):
            raise CounterfactualContractError(
                "market_provenance must be a MarketDataProvenanceReceipt."
            )
        if self.market_provenance.target_key != self.target_key:
            raise CounterfactualContractError(
                "market_provenance.target_key must match report target_key."
            )
        expected_provenance_hash = self.market_provenance_hash
        baseline_results = tuple(self.baseline_results)
        baseline_types: list[CounterfactualBaselineType] = []
        for result in baseline_results:
            if not isinstance(result, CounterfactualBaselineResult):
                raise CounterfactualContractError(
                    "baseline_results must contain CounterfactualBaselineResult values."
                )
            if result.episode_id != self.episode_id:
                raise CounterfactualContractError(
                    "baseline result episode_id must match report episode_id."
                )
            baseline_types.append(result.baseline_type)
            if result.provenance_hash != expected_provenance_hash:
                raise CounterfactualContractError(
                    "baseline result provenance_hash must match report market_provenance_hash."
                )
        if len(set(baseline_types)) != len(baseline_types):
            raise CounterfactualContractError(
                "baseline_results must not contain duplicate baseline_type values."
            )
        if set(baseline_types) != set(REQUIRED_BASELINE_TYPES):
            raise CounterfactualContractError(
                "baseline_results must contain exactly the six Phase 8 baselines."
            )
        object.__setattr__(self, "baseline_results", baseline_results)
        object.__setattr__(
            self,
            "created_at",
            validate_timestamp(
                self.created_at,
                field_name="created_at",
                error_type=CounterfactualContractError,
            ),
        )
        if self.episode_metrics is not None:
            if not isinstance(self.episode_metrics, CounterfactualEpisodeMetrics):
                raise CounterfactualContractError(
                    "episode_metrics must be CounterfactualEpisodeMetrics when provided."
                )
            if self.episode_metrics.episode_id != self.episode_id:
                raise CounterfactualContractError(
                    "episode_metrics.episode_id must match report episode_id."
                )
            if self.episode_metrics.target_key != self.target_key:
                raise CounterfactualContractError(
                    "episode_metrics.target_key must match report target_key."
                )
        if self.decision_quality_attribution is not None:
            if not isinstance(
                self.decision_quality_attribution,
                DecisionQualityAttribution,
            ):
                raise CounterfactualContractError(
                    "decision_quality_attribution must be DecisionQualityAttribution."
                )
            if self.decision_quality_attribution.episode_id != self.episode_id:
                raise CounterfactualContractError(
                    "decision_quality_attribution.episode_id must match report episode_id."
                )
            if self.decision_quality_attribution.target_key != self.target_key:
                raise CounterfactualContractError(
                    "decision_quality_attribution.target_key must match report target_key."
                )

    @property
    def market_provenance_hash(self) -> str:
        return execution_contract_hash(self.market_provenance.to_json_payload())

    def to_json_payload(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "actual_return": self.actual_return,
            "market_provenance": self.market_provenance.to_json_payload(),
            "market_provenance_hash": self.market_provenance_hash,
            "baseline_results": [
                result.to_json_payload() for result in self.baseline_results
            ],
            "created_at": self.created_at.isoformat(),
            "episode_metrics": (
                None
                if self.episode_metrics is None
                else self.episode_metrics.to_json_payload()
            ),
            "decision_quality_attribution": (
                None
                if self.decision_quality_attribution is None
                else self.decision_quality_attribution.to_json_payload()
            ),
        }


def parse_counterfactual_evaluation_report(
    payload: Mapping[str, object],
) -> CounterfactualEvaluationReport:
    """Parse a persisted report payload and re-run contract validation."""
    provenance_payload = payload.get("market_provenance")
    if not isinstance(provenance_payload, Mapping):
        raise CounterfactualContractError("market_provenance must be an object.")
    baseline_payloads = payload.get("baseline_results")
    if not isinstance(baseline_payloads, list):
        raise CounterfactualContractError("baseline_results must be a JSON array.")
    episode_metrics_payload = payload.get("episode_metrics")
    episode_metrics = None
    if episode_metrics_payload is not None:
        if not isinstance(episode_metrics_payload, Mapping):
            raise CounterfactualContractError("episode_metrics must be an object.")
        episode_metrics = _parse_episode_metrics(episode_metrics_payload)
    attribution_payload = payload.get("decision_quality_attribution")
    attribution = None
    if attribution_payload is not None:
        if not isinstance(attribution_payload, Mapping):
            raise CounterfactualContractError(
                "decision_quality_attribution must be an object."
            )
        attribution = _parse_attribution(attribution_payload)
    return CounterfactualEvaluationReport(
        episode_id=_require_text(payload, "episode_id"),
        target_key=_require_text(payload, "target_key"),
        actual_return=_require_float(payload, "actual_return"),
        market_provenance=parse_market_data_provenance_receipt(provenance_payload),
        baseline_results=tuple(
            _parse_baseline_result(item) for item in baseline_payloads
        ),
        created_at=_parse_timestamp(payload.get("created_at"), "created_at"),
        episode_metrics=episode_metrics,
        decision_quality_attribution=attribution,
    )


def _parse_baseline_result(payload: object) -> CounterfactualBaselineResult:
    if not isinstance(payload, Mapping):
        raise CounterfactualContractError("baseline result must be an object.")
    return CounterfactualBaselineResult(
        counterfactual_id=_require_text(payload, "counterfactual_id"),
        episode_id=_require_text(payload, "episode_id"),
        decision_episode_id=_optional_text(payload.get("decision_episode_id")),
        pm_decision_id=_optional_text(payload.get("pm_decision_id")),
        baseline_type=cast(CounterfactualBaselineType, _require_text(payload, "baseline_type")),
        baseline_return=_optional_float(payload.get("baseline_return")),
        delta_vs_actual=_optional_float(payload.get("delta_vs_actual")),
        entry_bar_start_at=_parse_optional_timestamp(
            payload.get("entry_bar_start_at"),
            "entry_bar_start_at",
        ),
        exit_bar_start_at=_parse_optional_timestamp(
            payload.get("exit_bar_start_at"),
            "exit_bar_start_at",
        ),
        status=cast(CounterfactualBaselineStatus, _require_text(payload, "status")),
        reason=_optional_text(payload.get("reason")),
        provenance_hash=_require_text(payload, "provenance_hash"),
        assumptions=_require_object_dict(payload, "assumptions"),
        market_data_hash=_optional_text(payload.get("market_data_hash")),
        result_metrics=_require_object_dict(payload, "result_metrics"),
        comparison_to_actual=_require_object_dict(payload, "comparison_to_actual"),
        computed_at=_parse_timestamp(payload.get("computed_at"), "computed_at"),
    )


def _parse_episode_metrics(payload: Mapping[str, object]) -> CounterfactualEpisodeMetrics:
    return CounterfactualEpisodeMetrics(
        episode_id=_require_text(payload, "episode_id"),
        target_key=_require_text(payload, "target_key"),
        return_pct=_require_float(payload, "return_pct"),
        max_drawdown_pct=_optional_float(payload.get("max_drawdown_pct")),
        time_to_max_gain_minutes=_optional_float(payload.get("time_to_max_gain_minutes")),
        time_to_max_loss_minutes=_optional_float(payload.get("time_to_max_loss_minutes")),
        holding_period_minutes=_require_float(payload, "holding_period_minutes"),
        bar_count=_require_int(payload, "bar_count"),
        hit_by_horizon=_require_bool(payload, "hit_by_horizon"),
        return_vs_baseline=_return_vs_baseline(payload.get("return_vs_baseline")),
    )


def _parse_attribution(payload: Mapping[str, object]) -> DecisionQualityAttribution:
    return DecisionQualityAttribution(
        episode_id=_require_text(payload, "episode_id"),
        target_key=_require_text(payload, "target_key"),
        entry_quality=cast(QualityStatus, _require_text(payload, "entry_quality")),
        exit_quality=cast(QualityStatus, _require_text(payload, "exit_quality")),
        execution_quality=cast(QualityStatus, _require_text(payload, "execution_quality")),
        reasons=_require_object_dict(payload, "reasons"),
    )


def _return_vs_baseline(value: object) -> dict[str, float | None]:
    if not isinstance(value, Mapping):
        raise CounterfactualContractError("return_vs_baseline must be an object.")
    return {
        _validate_non_blank(key, "return_vs_baseline key"): (
            None if item is None else _validate_finite(item, "return_vs_baseline")
        )
        for key, item in value.items()
        if isinstance(key, str)
    }


def _validate_non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CounterfactualContractError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _validate_finite(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CounterfactualContractError(f"{field_name} must be numeric.")
    normalized = float(value)
    if not isfinite(normalized):
        raise CounterfactualContractError(f"{field_name} must be finite.")
    return normalized


def _validate_finite_required(value: object, field_name: str) -> float:
    if value is None:
        raise CounterfactualContractError(f"{field_name} is required when status is available.")
    return _validate_finite(value, field_name)


def _validate_non_negative_finite(value: object, field_name: str) -> float:
    normalized = _validate_finite(value, field_name)
    if normalized < 0:
        raise CounterfactualContractError(f"{field_name} must be non-negative.")
    return normalized


def _validate_object_dict(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CounterfactualContractError(f"{field_name} must be a dict.")
    normalized: dict[str, object] = {}
    for key, item in value.items():
        normalized[_validate_non_blank(key, f"{field_name} key")] = item
    return normalized


def _require_object_dict(
    payload: Mapping[str, object],
    field_name: str,
) -> dict[str, object]:
    value = payload.get(field_name)
    if not isinstance(value, dict):
        raise CounterfactualContractError(f"{field_name} must be an object.")
    return _validate_object_dict(value, field_name)


def _optional_timestamp(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    return validate_timestamp(
        value,
        field_name=field_name,
        error_type=CounterfactualContractError,
    )


def _timestamp_payload(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_non_blank(payload.get(field_name), field_name)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _validate_non_blank(value, "optional_text")


def _require_float(payload: Mapping[str, object], field_name: str) -> float:
    return _validate_finite(payload.get(field_name), field_name)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return _validate_finite(value, "optional_float")


def _require_int(payload: Mapping[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CounterfactualContractError(f"{field_name} must be an integer.")
    return value


def _require_bool(payload: Mapping[str, object], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise CounterfactualContractError(f"{field_name} must be a bool.")
    return value


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise CounterfactualContractError(f"{field_name} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CounterfactualContractError(
            f"{field_name} must be an ISO timestamp."
        ) from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=CounterfactualContractError,
    )


def _parse_optional_timestamp(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_timestamp(value, field_name)


__all__ = [
    "CounterfactualBaselineResult",
    "CounterfactualBaselineStatus",
    "CounterfactualBaselineType",
    "CounterfactualContractError",
    "CounterfactualEpisodeMetrics",
    "CounterfactualEvaluationReport",
    "DecisionQualityAttribution",
    "QualityStatus",
    "REQUIRED_BASELINE_TYPES",
    "parse_counterfactual_evaluation_report",
]
