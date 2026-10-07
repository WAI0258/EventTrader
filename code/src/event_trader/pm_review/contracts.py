"""Typed PM-review request and action proposal contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from math import isclose, isfinite
from typing import Literal, cast

from event_trader.contracts._validators import (
    normalize_content,
    validate_event_id,
    validate_target_key,
    validate_timestamp,
)
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.evidence import EvidenceLedgerRecord
from event_trader.contracts.execution_direction_policy import (
    allowed_view_states_for_execution_direction_mode,
    analysis_assessment_direction_policy_violations,
    validate_execution_direction_mode,
)
from event_trader.contracts.pm_review_reason import (
    PMReviewReason,
    validate_pm_review_reasons,
)
from event_trader.contracts.price_level_role import (
    PriceLevelRole,
    PriceLevelRoleContractError,
    parse_price_level_role,
)
from event_trader.contracts.temporal_visibility import (
    DecisionVisibilityBoundary,
    DecisionVisibilityBoundaryError,
)
from event_trader.contracts.view_state_change import (
    MarketDataBar,
    ViewState,
    canonical_view_state_target_weight,
)
from event_trader.episode_memory.contracts import (
    parse_episode_memory_delta_source_refs,
)
from event_trader.episode_memory.projection import (
    PMEpisodeMemoryView,
    PMEpisodeMemoryViewDelta,
)
from event_trader.research_memory.active_price_projection import (
    projected_market_setup_dashboard_md,
)

PMReviewSource = Literal[
    "analysis_event",
    "open_position_horizon",
    "open_position_material_update",
    "flat_candidate",
]
CandidateReviewAnchorSource = Literal[
    "analysis_assessment",
    "watch_trigger",
    "operator_admission",
    "prior_candidate_review",
]
Confidence = Literal["low", "medium", "high"]
PMReviewExecutionDirectionMode = Literal["long_only", "long_short"]
PMPortfolioRiskEntrySource = Literal[
    "same_basis_execution",
    "active_basis_market_bar",
]
PMReviewFailureStage = Literal[
    "pm_review_run",
    "pm_review_run_and_execute",
]
PMReviewDispatchOutcome = Literal[
    "processed",
    "skipped",
]
PMReviewTerminalPolicySkipReason = Literal[
    "analysis_event_current_exposure_flat",
]
PMReviewEpisodeMemoryReadReceiptStatus = Literal[
    "read",
    "empty",
    "skipped",
    "failed",
]

_EXPLICIT_HOLD_REVIEW_REASONS: frozenset[PMReviewReason] = frozenset(
    {
        "current_exposure_pressure",
        "risk_reward_compression",
        "invalidation_touched",
    }
)

_PM_REVIEW_SOURCES = frozenset(
    {
        "analysis_event",
        "open_position_horizon",
        "open_position_material_update",
        "flat_candidate",
    }
)
_ANCHOR_SOURCES = frozenset(
    {
        "analysis_assessment",
        "watch_trigger",
        "operator_admission",
        "prior_candidate_review",
    }
)
_CONFIDENCE_VALUES = frozenset({"low", "medium", "high"})
_DISPATCH_OUTCOMES = frozenset({"processed", "skipped"})
_TERMINAL_PM_REVIEW_POLICY_SKIP_REASONS = frozenset(
    {
        "analysis_event_current_exposure_flat",
    }
)
_STATE_TARGET_WEIGHT: dict[ViewState, float] = {
    "flat": 0.0,
    "weak_long": 0.5,
    "strong_long": 1.0,
    "weak_short": -0.5,
    "strong_short": -1.0,
}


def is_terminal_pm_review_policy_skip_reason(reason: str | None) -> bool:
    return reason in _TERMINAL_PM_REVIEW_POLICY_SKIP_REASONS


class PMReviewContractError(ValueError):
    """Raised when a PM-review contract is malformed."""


@dataclass(frozen=True, slots=True)
class PMReviewRequest:
    request_id: str
    target_key: str
    business_at: datetime

    source: PMReviewSource
    source_assessment_id: str | None
    source_episode_id: str | None
    source_event_ids: tuple[str, ...]

    review_reasons: tuple[PMReviewReason, ...]
    current_exposure_required: bool
    candidate_review_allowed: bool

    max_visible_event_time: datetime
    max_visible_market_time: datetime

    required_price_level_ids: tuple[str, ...]
    required_claim_ids: tuple[str, ...]
    candidate_anchor_id: str | None
    _decision_visibility: DecisionVisibilityBoundary = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _validate_non_blank(self.request_id, "request_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PMReviewContractError),
        )
        try:
            decision_visibility = DecisionVisibilityBoundary(
                business_at=validate_timestamp(
                    self.business_at,
                    field_name="business_at",
                    error_type=PMReviewContractError,
                ),
                max_visible_event_time=self.max_visible_event_time,
                max_visible_market_time=self.max_visible_market_time,
            )
        except DecisionVisibilityBoundaryError as exc:
            raise PMReviewContractError(str(exc)) from exc
        object.__setattr__(self, "business_at", decision_visibility.business_at)
        object.__setattr__(
            self,
            "max_visible_event_time",
            decision_visibility.max_visible_event_time,
        )
        object.__setattr__(
            self,
            "max_visible_market_time",
            decision_visibility.max_visible_market_time,
        )
        object.__setattr__(self, "_decision_visibility", decision_visibility)
        object.__setattr__(self, "source", _validate_review_source(self.source))
        object.__setattr__(
            self,
            "source_assessment_id",
            _validate_optional_text(self.source_assessment_id, "source_assessment_id"),
        )
        object.__setattr__(
            self,
            "source_episode_id",
            _validate_optional_text(self.source_episode_id, "source_episode_id"),
        )
        object.__setattr__(
            self,
            "source_event_ids",
            _validate_event_ids(self.source_event_ids, allow_empty=True),
        )
        object.__setattr__(
            self,
            "review_reasons",
            validate_pm_review_reasons(
                self.review_reasons,
                field_name="review_reasons",
                allow_none=False,
                allow_empty=False,
                error_type=PMReviewContractError,
            ),
        )
        if not isinstance(self.current_exposure_required, bool):
            raise PMReviewContractError("current_exposure_required must be a boolean.")
        if not isinstance(self.candidate_review_allowed, bool):
            raise PMReviewContractError("candidate_review_allowed must be a boolean.")
        object.__setattr__(
            self,
            "required_price_level_ids",
            _validate_string_tuple(
                self.required_price_level_ids,
                "required_price_level_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "required_claim_ids",
            _validate_string_tuple(self.required_claim_ids, "required_claim_ids", allow_empty=True),
        )
        object.__setattr__(
            self,
            "candidate_anchor_id",
            _validate_optional_text(self.candidate_anchor_id, "candidate_anchor_id"),
        )
        if self.source == "flat_candidate" and self.candidate_anchor_id is None:
            raise PMReviewContractError("flat_candidate PMReviewRequest requires candidate_anchor_id.")
        if self.source != "flat_candidate" and self.candidate_anchor_id is not None:
            if not self.candidate_review_allowed:
                raise PMReviewContractError(
                    "candidate_anchor_id requires candidate_review_allowed=true."
                )

    @property
    def decision_visibility(self) -> DecisionVisibilityBoundary:
        return self._decision_visibility

    def to_json_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "source": self.source,
            "source_assessment_id": self.source_assessment_id,
            "source_episode_id": self.source_episode_id,
            "source_event_ids": list(self.source_event_ids),
            "review_reasons": list(self.review_reasons),
            "current_exposure_required": self.current_exposure_required,
            "candidate_review_allowed": self.candidate_review_allowed,
            "max_visible_event_time": self.max_visible_event_time.isoformat(),
            "max_visible_market_time": self.max_visible_market_time.isoformat(),
            "required_price_level_ids": list(self.required_price_level_ids),
            "required_claim_ids": list(self.required_claim_ids),
            "candidate_anchor_id": self.candidate_anchor_id,
        }


@dataclass(frozen=True, slots=True)
class CandidateReviewAnchor:
    anchor_id: str
    target_key: str
    created_at: datetime
    source: CandidateReviewAnchorSource
    source_assessment_id: str | None
    source_event_ids: tuple[str, ...]
    watch_trigger_ids: tuple[str, ...]
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "anchor_id", _validate_non_blank(self.anchor_id, "anchor_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PMReviewContractError),
        )
        object.__setattr__(
            self,
            "created_at",
            validate_timestamp(
                self.created_at,
                field_name="created_at",
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(self, "source", _validate_anchor_source(self.source))
        object.__setattr__(
            self,
            "source_assessment_id",
            _validate_optional_text(self.source_assessment_id, "source_assessment_id"),
        )
        object.__setattr__(
            self,
            "source_event_ids",
            _validate_event_ids(self.source_event_ids, allow_empty=True),
        )
        object.__setattr__(
            self,
            "watch_trigger_ids",
            _validate_string_tuple(self.watch_trigger_ids, "watch_trigger_ids", allow_empty=True),
        )
        object.__setattr__(
            self,
            "expires_at",
            validate_timestamp(
                self.expires_at,
                field_name="expires_at",
                error_type=PMReviewContractError,
            ),
        )
        if self.expires_at <= self.created_at:
            raise PMReviewContractError("expires_at must be after created_at.")
        if self.source == "analysis_assessment" and self.source_assessment_id is None:
            raise PMReviewContractError(
                "analysis_assessment candidate anchors require source_assessment_id."
            )
        if self.source == "watch_trigger" and not self.watch_trigger_ids:
            raise PMReviewContractError("watch_trigger candidate anchors require watch_trigger_ids.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "anchor_id": self.anchor_id,
            "target_key": self.target_key,
            "created_at": self.created_at.isoformat(),
            "source": self.source,
            "source_assessment_id": self.source_assessment_id,
            "source_event_ids": list(self.source_event_ids),
            "watch_trigger_ids": list(self.watch_trigger_ids),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class PMAnalysisSnapshot:
    """Slim analysis-owned context surfaced deterministically to PM."""

    assessment_id: str
    target_key: str
    business_at: datetime
    as_if_flat_state: ViewState
    if_flat_implication_md: str
    if_already_long_implication_md: str
    if_already_short_implication_md: str | None
    price_level_roles: tuple[PriceLevelRole, ...]
    market_setup_dashboard_md: str
    key_claim_ids: tuple[str, ...]
    contested_prior_claim_ids: tuple[str, ...]
    pm_review_reasons: tuple[PMReviewReason, ...]
    confidence: Confidence

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assessment_id",
            _validate_non_blank(self.assessment_id, "assessment_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PMReviewContractError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(
            self,
            "as_if_flat_state",
            _validate_view_state(self.as_if_flat_state, "as_if_flat_state"),
        )
        for field_name in (
            "if_flat_implication_md",
            "if_already_long_implication_md",
            "market_setup_dashboard_md",
        ):
            object.__setattr__(
                self,
                field_name,
                normalize_content(
                    getattr(self, field_name),
                    field_name=field_name,
                    error_type=PMReviewContractError,
                ),
            )
        object.__setattr__(
            self,
            "if_already_short_implication_md",
            _validate_optional_markdown(
                self.if_already_short_implication_md,
                "if_already_short_implication_md",
            ),
        )
        object.__setattr__(
            self,
            "price_level_roles",
            _validate_price_level_roles(self.price_level_roles, self.target_key),
        )
        object.__setattr__(
            self,
            "key_claim_ids",
            _validate_string_tuple(self.key_claim_ids, "key_claim_ids", allow_empty=True),
        )
        object.__setattr__(
            self,
            "contested_prior_claim_ids",
            _validate_string_tuple(
                self.contested_prior_claim_ids,
                "contested_prior_claim_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "pm_review_reasons",
            validate_pm_review_reasons(
                self.pm_review_reasons,
                field_name="pm_review_reasons",
                allow_none=True,
                allow_empty=True,
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(self, "confidence", _validate_confidence(self.confidence))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "assessment_id": self.assessment_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "as_if_flat_state": self.as_if_flat_state,
            "if_flat_implication_md": self.if_flat_implication_md,
            "if_already_long_implication_md": self.if_already_long_implication_md,
            "price_level_roles": [
                level_role.to_json_payload() for level_role in self.price_level_roles
            ],
            "market_setup_dashboard_md": self.market_setup_dashboard_md,
            "key_claim_ids": list(self.key_claim_ids),
            "contested_prior_claim_ids": list(self.contested_prior_claim_ids),
            "pm_review_reasons": list(self.pm_review_reasons),
            "confidence": self.confidence,
            **(
                {}
                if self.if_already_short_implication_md is None
                else {
                    "if_already_short_implication_md": (
                        self.if_already_short_implication_md
                    )
                }
            ),
        }


@dataclass(frozen=True, slots=True)
class PMPortfolioRiskSnapshot:
    """Deterministic portfolio-path risk context surfaced to PMReview."""

    episode_id: str
    segment_id: str
    instrument_basis: str
    segment_opened_at: datetime
    entry_reference_at: datetime
    entry_reference_price: float
    entry_source: PMPortfolioRiskEntrySource
    target_weight: float
    current_mark_price: float
    mark_as_of: datetime
    underlying_return: float
    strategy_return: float
    drawdown_from_best_strategy_return: float

    def __post_init__(self) -> None:
        for field_name in ("episode_id", "segment_id", "instrument_basis"):
            object.__setattr__(
                self,
                field_name,
                _validate_non_blank(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "segment_opened_at",
            validate_timestamp(
                self.segment_opened_at,
                field_name="segment_opened_at",
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(
            self,
            "entry_reference_at",
            validate_timestamp(
                self.entry_reference_at,
                field_name="entry_reference_at",
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(
            self,
            "mark_as_of",
            validate_timestamp(
                self.mark_as_of,
                field_name="mark_as_of",
                error_type=PMReviewContractError,
            ),
        )
        if self.entry_reference_at < self.segment_opened_at:
            raise PMReviewContractError(
                "entry_reference_at must be at or after segment_opened_at."
            )
        if self.mark_as_of < self.entry_reference_at:
            raise PMReviewContractError(
                "mark_as_of must be at or after entry_reference_at."
            )
        if self.entry_source not in {
            "same_basis_execution",
            "active_basis_market_bar",
        }:
            raise PMReviewContractError("entry_source is unsupported.")
        entry_reference_price = _validate_finite_float(
            self.entry_reference_price,
            "entry_reference_price",
        )
        current_mark_price = _validate_finite_float(
            self.current_mark_price,
            "current_mark_price",
        )
        if entry_reference_price <= 0.0:
            raise PMReviewContractError(
                "entry_reference_price must be greater than zero."
            )
        if current_mark_price <= 0.0:
            raise PMReviewContractError(
                "current_mark_price must be greater than zero."
            )
        object.__setattr__(self, "entry_reference_price", entry_reference_price)
        object.__setattr__(self, "current_mark_price", current_mark_price)
        target_weight = _validate_finite_float(self.target_weight, "target_weight")
        if target_weight == 0.0:
            raise PMReviewContractError("target_weight must be non-zero.")
        object.__setattr__(self, "target_weight", target_weight)
        underlying_return = _validate_finite_float(
            self.underlying_return,
            "underlying_return",
        )
        strategy_return = _validate_finite_float(
            self.strategy_return,
            "strategy_return",
        )
        expected_underlying_return = current_mark_price / entry_reference_price - 1.0
        if not isclose(
            underlying_return,
            expected_underlying_return,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise PMReviewContractError(
                "underlying_return must match entry_reference_price and current_mark_price."
            )
        if not isclose(
            strategy_return,
            target_weight * underlying_return,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise PMReviewContractError(
                "strategy_return must equal target_weight * underlying_return."
            )
        object.__setattr__(
            self,
            "underlying_return",
            underlying_return,
        )
        object.__setattr__(self, "strategy_return", strategy_return)
        drawdown = _validate_finite_float(
            self.drawdown_from_best_strategy_return,
            "drawdown_from_best_strategy_return",
        )
        if drawdown < 0.0:
            raise PMReviewContractError(
                "drawdown_from_best_strategy_return must be non-negative."
            )
        object.__setattr__(
            self,
            "drawdown_from_best_strategy_return",
            drawdown,
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "segment_id": self.segment_id,
            "instrument_basis": self.instrument_basis,
            "segment_opened_at": self.segment_opened_at.isoformat(),
            "entry_reference_at": self.entry_reference_at.isoformat(),
            "entry_reference_price": self.entry_reference_price,
            "entry_source": self.entry_source,
            "target_weight": self.target_weight,
            "current_mark_price": self.current_mark_price,
            "mark_as_of": self.mark_as_of.isoformat(),
            "underlying_return": self.underlying_return,
            "strategy_return": self.strategy_return,
            "drawdown_from_best_strategy_return": (
                self.drawdown_from_best_strategy_return
            ),
        }


@dataclass(frozen=True, slots=True)
class PMReviewInput:
    """Repo-owned deterministic PM input surface for the bounded PM agent."""

    pm_review_request_id: str
    target_key: str
    business_at: datetime
    source: PMReviewSource
    source_assessment_id: str | None
    source_episode_id: str | None
    source_event_ids: tuple[str, ...]
    review_reasons: tuple[PMReviewReason, ...]
    current_exposure_required: bool
    candidate_review_allowed: bool
    execution_direction_mode: PMReviewExecutionDirectionMode
    decision_visibility: DecisionVisibilityBoundary
    required_price_level_ids: tuple[str, ...]
    required_claim_ids: tuple[str, ...]
    actual_current_state: ViewState
    actual_target_weight_before: float
    analysis_snapshot: PMAnalysisSnapshot | None
    portfolio_risk_snapshot: PMPortfolioRiskSnapshot | None
    visible_evidence: tuple[EvidenceLedgerRecord, ...]
    visible_market_bars: tuple[MarketDataBar, ...]
    visible_pm_history: tuple[PMReviewRequest, ...]
    source_episode_memory_view: PMEpisodeMemoryView | None = None
    source_episode_memory_read_receipt: PMReviewEpisodeMemoryReadReceipt | None = None
    candidate_anchor: CandidateReviewAnchor | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "pm_review_request_id",
            _validate_non_blank(self.pm_review_request_id, "pm_review_request_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PMReviewContractError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PMReviewContractError,
            ),
        )
        if not isinstance(self.decision_visibility, DecisionVisibilityBoundary):
            raise PMReviewContractError(
                "decision_visibility must be a DecisionVisibilityBoundary instance."
            )
        if self.decision_visibility.business_at != self.business_at:
            raise PMReviewContractError(
                "decision_visibility.business_at must match business_at."
            )
        object.__setattr__(self, "source", _validate_review_source(self.source))
        object.__setattr__(
            self,
            "source_assessment_id",
            _validate_optional_text(self.source_assessment_id, "source_assessment_id"),
        )
        object.__setattr__(
            self,
            "source_episode_id",
            _validate_optional_text(self.source_episode_id, "source_episode_id"),
        )
        object.__setattr__(
            self,
            "source_event_ids",
            _validate_event_ids(self.source_event_ids, allow_empty=True),
        )
        object.__setattr__(
            self,
            "review_reasons",
            validate_pm_review_reasons(
                self.review_reasons,
                field_name="review_reasons",
                allow_none=False,
                allow_empty=False,
                error_type=PMReviewContractError,
            ),
        )
        if not isinstance(self.current_exposure_required, bool):
            raise PMReviewContractError("current_exposure_required must be a boolean.")
        if not isinstance(self.candidate_review_allowed, bool):
            raise PMReviewContractError("candidate_review_allowed must be a boolean.")
        object.__setattr__(
            self,
            "execution_direction_mode",
            _validate_execution_direction_mode(self.execution_direction_mode),
        )
        object.__setattr__(
            self,
            "required_price_level_ids",
            _validate_string_tuple(
                self.required_price_level_ids,
                "required_price_level_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "required_claim_ids",
            _validate_string_tuple(self.required_claim_ids, "required_claim_ids", allow_empty=True),
        )
        object.__setattr__(
            self,
            "actual_current_state",
            _validate_view_state(self.actual_current_state, "actual_current_state"),
        )
        actual_target_weight_before = _validate_finite_float(
            self.actual_target_weight_before,
            "actual_target_weight_before",
        )
        if actual_target_weight_before != canonical_view_state_target_weight(
            self.actual_current_state
        ):
            raise PMReviewContractError(
                "actual_target_weight_before must match the canonical system weight "
                "for actual_current_state."
            )
        object.__setattr__(
            self,
            "actual_target_weight_before",
            actual_target_weight_before,
        )
        if self.analysis_snapshot is not None:
            if not isinstance(self.analysis_snapshot, PMAnalysisSnapshot):
                raise PMReviewContractError(
                    "analysis_snapshot must be a PMAnalysisSnapshot when provided."
                )
            if self.analysis_snapshot.target_key != self.target_key:
                raise PMReviewContractError(
                    "analysis_snapshot target_key must match target_key."
                )
            direction_violations = analysis_assessment_direction_policy_violations(
                as_if_flat_state=self.analysis_snapshot.as_if_flat_state,
                role_if_already_short_values=tuple(
                    level.role_if_already_short
                    for level in self.analysis_snapshot.price_level_roles
                ),
                execution_direction_mode=self.execution_direction_mode,
                field_prefix="analysis_snapshot",
            )
            if direction_violations:
                raise PMReviewContractError(direction_violations[0].message)
            if (
                self.execution_direction_mode == "long_short"
                and self.analysis_snapshot.if_already_short_implication_md is None
            ):
                raise PMReviewContractError(
                    "analysis_snapshot.if_already_short_implication_md must be set "
                    "for execution_direction_mode 'long_short'."
                )
        if self.portfolio_risk_snapshot is not None:
            if not isinstance(self.portfolio_risk_snapshot, PMPortfolioRiskSnapshot):
                raise PMReviewContractError(
                    "portfolio_risk_snapshot must be a PMPortfolioRiskSnapshot when provided."
                )
            if self.actual_current_state == "flat":
                raise PMReviewContractError(
                    "portfolio_risk_snapshot must be None when actual_current_state is flat."
                )
            if (
                self.portfolio_risk_snapshot.mark_as_of
                > self.decision_visibility.max_visible_market_time
            ):
                raise PMReviewContractError(
                    "portfolio_risk_snapshot.mark_as_of exceeds "
                    "decision_visibility.max_visible_market_time."
                )
            if self.portfolio_risk_snapshot.target_weight != actual_target_weight_before:
                raise PMReviewContractError(
                    "portfolio_risk_snapshot.target_weight must match "
                    "actual_target_weight_before."
                )
        elif self.current_exposure_required and self.actual_current_state != "flat":
            raise PMReviewContractError(
                "portfolio_risk_snapshot is required for a non-flat current-exposure review."
            )
        object.__setattr__(
            self,
            "visible_evidence",
            _validate_visible_evidence(
                self.visible_evidence,
                target_key=self.target_key,
                max_visible_event_time=self.decision_visibility.max_visible_event_time,
            ),
        )
        object.__setattr__(
            self,
            "visible_market_bars",
            _validate_visible_market_bars(
                self.visible_market_bars,
                max_visible_market_time=self.decision_visibility.max_visible_market_time,
            ),
        )
        object.__setattr__(
            self,
            "visible_pm_history",
            _validate_visible_pm_history(
                self.visible_pm_history,
                target_key=self.target_key,
                request_id=self.pm_review_request_id,
                business_at=self.business_at,
            ),
        )
        if self.source_episode_memory_view is not None:
            if not isinstance(self.source_episode_memory_view, PMEpisodeMemoryView):
                raise PMReviewContractError(
                    "source_episode_memory_view must be a PMEpisodeMemoryView when provided."
                )
            if self.source_episode_id is None:
                raise PMReviewContractError(
                    "source_episode_memory_view requires source_episode_id."
                )
            if self.source_episode_memory_view.episode_id != self.source_episode_id:
                raise PMReviewContractError(
                    "source_episode_memory_view.episode_id must match source_episode_id."
                )
            if self.source_episode_memory_view.target_key != self.target_key:
                raise PMReviewContractError(
                    "source_episode_memory_view.target_key must match target_key."
                )
            if self.source_episode_memory_view.business_at != self.business_at:
                raise PMReviewContractError(
                    "source_episode_memory_view.business_at must match business_at."
                )
        if self.source_episode_memory_read_receipt is not None:
            if not isinstance(
                self.source_episode_memory_read_receipt,
                PMReviewEpisodeMemoryReadReceipt,
            ):
                raise PMReviewContractError(
                    "source_episode_memory_read_receipt must be a PMReviewEpisodeMemoryReadReceipt when provided."
                )
            if self.source_episode_memory_read_receipt.request_id != self.pm_review_request_id:
                raise PMReviewContractError(
                    "source_episode_memory_read_receipt.request_id must match pm_review_request_id."
                )
            if self.source_episode_memory_read_receipt.target_key != self.target_key:
                raise PMReviewContractError(
                    "source_episode_memory_read_receipt.target_key must match target_key."
                )
            if self.source_episode_memory_read_receipt.business_at != self.business_at:
                raise PMReviewContractError(
                    "source_episode_memory_read_receipt.business_at must match business_at."
                )
            if self.source_episode_memory_read_receipt.source_episode_id != self.source_episode_id:
                raise PMReviewContractError(
                    "source_episode_memory_read_receipt.source_episode_id must match source_episode_id."
                )
            if self.source_episode_memory_read_receipt.status in {"read", "empty"}:
                if self.source_episode_memory_view is None:
                    raise PMReviewContractError(
                        "source_episode_memory_view is required when source episode memory status is read or empty."
                    )
            elif self.source_episode_memory_read_receipt.status == "skipped":
                if self.source_episode_id is not None:
                    raise PMReviewContractError(
                        "skipped source episode memory receipts require source_episode_id=None."
                    )
                if self.source_episode_memory_view is not None:
                    raise PMReviewContractError(
                        "skipped source episode memory receipts must not include source_episode_memory_view."
                    )
            elif self.source_episode_memory_read_receipt.status == "failed":
                if self.source_episode_memory_view is not None:
                    raise PMReviewContractError(
                        "failed source episode memory receipts must not include source_episode_memory_view."
                    )
        elif self.source_episode_memory_view is not None:
            raise PMReviewContractError(
                "source_episode_memory_view requires source_episode_memory_read_receipt."
            )
        _validate_input_candidate_anchor(self)

    @property
    def allowed_requested_states(self) -> tuple[ViewState, ...]:
        return allowed_requested_states_for_execution_direction_mode(
            self.execution_direction_mode
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "pm_review_request_id": self.pm_review_request_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "source": self.source,
            "source_assessment_id": self.source_assessment_id,
            "source_episode_id": self.source_episode_id,
            "source_event_ids": list(self.source_event_ids),
            "review_reasons": list(self.review_reasons),
            "current_exposure_required": self.current_exposure_required,
            "candidate_review_allowed": self.candidate_review_allowed,
            "execution_direction_mode": self.execution_direction_mode,
            "allowed_requested_states": list(self.allowed_requested_states),
            "decision_visibility": _decision_visibility_to_json_payload(
                self.decision_visibility
            ),
            "required_price_level_ids": list(self.required_price_level_ids),
            "required_claim_ids": list(self.required_claim_ids),
            "actual_current_state": self.actual_current_state,
            "actual_target_weight_before": self.actual_target_weight_before,
            "analysis_snapshot": (
                None
                if self.analysis_snapshot is None
                else self.analysis_snapshot.to_json_payload()
            ),
            "portfolio_risk_snapshot": (
                None
                if self.portfolio_risk_snapshot is None
                else self.portfolio_risk_snapshot.to_json_payload()
            ),
            "visible_evidence": [
                _evidence_record_to_json_payload(record)
                for record in self.visible_evidence
            ],
            "visible_market_bars": [
                _market_bar_to_json_payload(record) for record in self.visible_market_bars
            ],
            "visible_pm_history": [
                record.to_json_payload() for record in self.visible_pm_history
            ],
            "source_episode_memory_view": (
                None
                if self.source_episode_memory_view is None
                else self.source_episode_memory_view.to_json_payload()
            ),
            "source_episode_memory_read_receipt": (
                None
                if self.source_episode_memory_read_receipt is None
                else self.source_episode_memory_read_receipt.to_json_payload()
            ),
            "candidate_anchor": (
                None if self.candidate_anchor is None else self.candidate_anchor.to_json_payload()
            ),
        }


def pm_review_hold_reason_required(
    *,
    pm_review_input: PMReviewInput,
    requested_state: ViewState,
) -> bool:
    """Return whether an unchanged risk-reviewed exposure requires a hold code."""

    if not pm_review_input.current_exposure_required:
        return False
    if requested_state != pm_review_input.actual_current_state:
        return False
    return any(
        reason in _EXPLICIT_HOLD_REVIEW_REASONS
        for reason in pm_review_input.review_reasons
    )


@dataclass(frozen=True, slots=True)
class PMDecisionDraft:
    """Thin PM judgment output before deterministic materialization."""

    pm_review_request_id: str
    target_key: str
    business_at: datetime
    requested_state: ViewState
    rationale_md: str
    cited_event_ids: tuple[str, ...]
    cited_market_bar_ids: tuple[str, ...]
    fallback_state_if_clamped: ViewState | None = None
    fallback_rationale_md: str | None = None
    hold_reason_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "pm_review_request_id",
            _validate_non_blank(self.pm_review_request_id, "pm_review_request_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PMReviewContractError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(
            self,
            "requested_state",
            _validate_view_state(self.requested_state, "requested_state"),
        )
        object.__setattr__(
            self,
            "rationale_md",
            normalize_content(
                self.rationale_md,
                field_name="rationale_md",
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(
            self,
            "cited_event_ids",
            _validate_event_ids(self.cited_event_ids, allow_empty=True),
        )
        object.__setattr__(
            self,
            "cited_market_bar_ids",
            _validate_string_tuple(
                self.cited_market_bar_ids,
                "cited_market_bar_ids",
                allow_empty=True,
            ),
        )
        fallback_state = (
            None
            if self.fallback_state_if_clamped is None
            else _validate_view_state(
                self.fallback_state_if_clamped,
                "fallback_state_if_clamped",
            )
        )
        fallback_rationale = _validate_optional_markdown(
            self.fallback_rationale_md,
            "fallback_rationale_md",
        )
        if (fallback_state is None) != (fallback_rationale is None):
            raise PMReviewContractError(
                "fallback_state_if_clamped and fallback_rationale_md must be set together."
            )
        object.__setattr__(self, "fallback_state_if_clamped", fallback_state)
        object.__setattr__(self, "fallback_rationale_md", fallback_rationale)
        object.__setattr__(
            self,
            "hold_reason_code",
            _validate_optional_reason_code(self.hold_reason_code, "hold_reason_code"),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "pm_review_request_id": self.pm_review_request_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "requested_state": self.requested_state,
            "rationale_md": self.rationale_md,
            "cited_event_ids": list(self.cited_event_ids),
            "cited_market_bar_ids": list(self.cited_market_bar_ids),
            "fallback_state_if_clamped": self.fallback_state_if_clamped,
            "fallback_rationale_md": self.fallback_rationale_md,
            "hold_reason_code": self.hold_reason_code,
        }


@dataclass(frozen=True, slots=True)
class PMPositionReviewTriggers:
    """Deterministic follow-up PM re-review triggers for maintained/open positions."""

    pm_review_request_id: str
    target_key: str
    business_at: datetime
    requested_state: ViewState
    review_trigger_level_ids: tuple[str, ...]
    invalidation_trigger_level_ids: tuple[str, ...]
    path_context_required: bool
    market_confirmation_required: bool
    trigger_reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "pm_review_request_id",
            _validate_non_blank(self.pm_review_request_id, "pm_review_request_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PMReviewContractError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(
            self,
            "requested_state",
            _validate_view_state(self.requested_state, "requested_state"),
        )
        object.__setattr__(
            self,
            "review_trigger_level_ids",
            _validate_string_tuple(
                self.review_trigger_level_ids,
                "review_trigger_level_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "invalidation_trigger_level_ids",
            _validate_string_tuple(
                self.invalidation_trigger_level_ids,
                "invalidation_trigger_level_ids",
                allow_empty=True,
            ),
        )
        if not isinstance(self.path_context_required, bool):
            raise PMReviewContractError("path_context_required must be a boolean.")
        if not isinstance(self.market_confirmation_required, bool):
            raise PMReviewContractError("market_confirmation_required must be a boolean.")
        object.__setattr__(
            self,
            "trigger_reason_codes",
            _validate_reason_code_tuple(
                self.trigger_reason_codes,
                "trigger_reason_codes",
                allow_empty=True,
            ),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "pm_review_request_id": self.pm_review_request_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "requested_state": self.requested_state,
            "review_trigger_level_ids": list(self.review_trigger_level_ids),
            "invalidation_trigger_level_ids": list(self.invalidation_trigger_level_ids),
            "path_context_required": self.path_context_required,
            "market_confirmation_required": self.market_confirmation_required,
            "trigger_reason_codes": list(self.trigger_reason_codes),
        }


@dataclass(frozen=True, slots=True)
class PMReviewToolReadReceipt:
    read_id: str
    pm_review_request_id: str
    tool_name: str
    target_key: str
    business_at: datetime

    requested_start_at: datetime | None
    requested_end_at: datetime | None
    enforced_max_visible_time: datetime | None

    returned_record_ids: tuple[str, ...]
    returned_time_range_start: datetime | None
    returned_time_range_end: datetime | None

    visibility_passed: bool

    def __post_init__(self) -> None:
        for field_name in ("read_id", "pm_review_request_id", "tool_name"):
            object.__setattr__(
                self,
                field_name,
                _validate_non_blank(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PMReviewContractError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(
            self,
            "requested_start_at",
            _validate_optional_timestamp(self.requested_start_at, "requested_start_at"),
        )
        object.__setattr__(
            self,
            "requested_end_at",
            _validate_optional_timestamp(self.requested_end_at, "requested_end_at"),
        )
        if (
            self.requested_start_at is not None
            and self.requested_end_at is not None
            and self.requested_end_at <= self.requested_start_at
        ):
            raise PMReviewContractError("requested_end_at must be after requested_start_at.")
        object.__setattr__(
            self,
            "enforced_max_visible_time",
            _validate_optional_timestamp(
                self.enforced_max_visible_time,
                "enforced_max_visible_time",
            ),
        )
        object.__setattr__(
            self,
            "returned_record_ids",
            _validate_string_tuple(
                self.returned_record_ids,
                "returned_record_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "returned_time_range_start",
            _validate_optional_timestamp(
                self.returned_time_range_start,
                "returned_time_range_start",
            ),
        )
        object.__setattr__(
            self,
            "returned_time_range_end",
            _validate_optional_timestamp(
                self.returned_time_range_end,
                "returned_time_range_end",
            ),
        )
        if (self.returned_time_range_start is None) != (self.returned_time_range_end is None):
            raise PMReviewContractError(
                "returned_time_range_start and returned_time_range_end must be set together."
            )
        if (
            self.returned_time_range_start is not None
            and self.returned_time_range_end is not None
            and self.returned_time_range_end < self.returned_time_range_start
        ):
            raise PMReviewContractError(
                "returned_time_range_end must not be before returned_time_range_start."
            )
        if not isinstance(self.visibility_passed, bool):
            raise PMReviewContractError("visibility_passed must be a boolean.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "read_id": self.read_id,
            "pm_review_request_id": self.pm_review_request_id,
            "tool_name": self.tool_name,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "requested_start_at": (
                None
                if self.requested_start_at is None
                else self.requested_start_at.isoformat()
            ),
            "requested_end_at": (
                None if self.requested_end_at is None else self.requested_end_at.isoformat()
            ),
            "enforced_max_visible_time": (
                None
                if self.enforced_max_visible_time is None
                else self.enforced_max_visible_time.isoformat()
            ),
            "returned_record_ids": list(self.returned_record_ids),
            "returned_time_range_start": (
                None
                if self.returned_time_range_start is None
                else self.returned_time_range_start.isoformat()
            ),
            "returned_time_range_end": (
                None
                if self.returned_time_range_end is None
                else self.returned_time_range_end.isoformat()
            ),
            "visibility_passed": self.visibility_passed,
        }


@dataclass(frozen=True, slots=True)
class PMReviewDispatchConsideration:
    consideration_id: str
    request_id: str
    target_key: str
    business_at: datetime
    dispatch_run_until: datetime
    outcome: PMReviewDispatchOutcome
    skip_reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "consideration_id",
            _validate_non_blank(self.consideration_id, "consideration_id"),
        )
        object.__setattr__(
            self,
            "request_id",
            _validate_non_blank(self.request_id, "request_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PMReviewContractError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(
            self,
            "dispatch_run_until",
            validate_timestamp(
                self.dispatch_run_until,
                field_name="dispatch_run_until",
                error_type=PMReviewContractError,
            ),
        )
        if self.outcome not in _DISPATCH_OUTCOMES:
            raise PMReviewContractError("outcome is not a supported dispatch outcome.")
        object.__setattr__(
            self,
            "skip_reason",
            _validate_optional_text(self.skip_reason, "skip_reason"),
        )
        if self.outcome == "skipped":
            if self.skip_reason is None:
                raise PMReviewContractError("skipped dispatch rows require skip_reason.")
        else:
            if self.skip_reason is not None:
                raise PMReviewContractError(
                    "processed dispatch rows must not include skip_reason."
                )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "consideration_id": self.consideration_id,
            "request_id": self.request_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "dispatch_run_until": self.dispatch_run_until.isoformat(),
            "outcome": self.outcome,
            "skip_reason": self.skip_reason,
        }


@dataclass(frozen=True, slots=True)
class PMReviewEpisodeMemoryReadReceipt:
    """Read receipt proving PM-facing episode-memory projection reads."""

    request_id: str
    target_key: str
    business_at: datetime
    source_episode_id: str | None
    projection_path: str | None
    projection_hash: str | None
    built_from_delta_ids: tuple[str, ...]
    built_from_delta_hashes: tuple[str, ...]
    source_visible_through_max: datetime | None
    usable_from_max: datetime | None
    excluded_delta_ids: tuple[str, ...]
    excluded_delta_reasons: tuple[str, ...]
    status: PMReviewEpisodeMemoryReadReceiptStatus
    failure_reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _validate_non_blank(self.request_id, "request_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PMReviewContractError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(
            self,
            "source_episode_id",
            _validate_optional_text(self.source_episode_id, "source_episode_id"),
        )
        object.__setattr__(
            self,
            "projection_path",
            _validate_optional_text(self.projection_path, "projection_path"),
        )
        object.__setattr__(
            self,
            "projection_hash",
            _optional_sha256_hex(self.projection_hash, "projection_hash"),
        )
        object.__setattr__(
            self,
            "built_from_delta_ids",
            _validate_string_tuple(
                self.built_from_delta_ids,
                "built_from_delta_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "built_from_delta_hashes",
            _validate_sha256_hash_tuple(
                self.built_from_delta_hashes,
                "built_from_delta_hashes",
            ),
        )
        object.__setattr__(
            self,
            "source_visible_through_max",
            _validate_optional_timestamp(
                self.source_visible_through_max,
                "source_visible_through_max",
            ),
        )
        object.__setattr__(
            self,
            "usable_from_max",
            _validate_optional_timestamp(
                self.usable_from_max,
                "usable_from_max",
            ),
        )
        object.__setattr__(
            self,
            "excluded_delta_ids",
            _validate_string_tuple(
                self.excluded_delta_ids,
                "excluded_delta_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "excluded_delta_reasons",
            _validate_string_tuple(
                self.excluded_delta_reasons,
                "excluded_delta_reasons",
                allow_empty=True,
            ),
        )
        if self.status not in {
            "read",
            "empty",
            "skipped",
            "failed",
        }:
            raise PMReviewContractError("status is not a supported episode-memory read status.")
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                _validate_optional_text(self.failure_reason, "failure_reason"),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "source_episode_id": self.source_episode_id,
            "projection_path": self.projection_path,
            "projection_hash": self.projection_hash,
            "built_from_delta_ids": list(self.built_from_delta_ids),
            "built_from_delta_hashes": list(self.built_from_delta_hashes),
            "source_visible_through_max": (
                None
                if self.source_visible_through_max is None
                else self.source_visible_through_max.isoformat()
            ),
            "usable_from_max": (
                None
                if self.usable_from_max is None
                else self.usable_from_max.isoformat()
            ),
            "excluded_delta_ids": list(self.excluded_delta_ids),
            "excluded_delta_reasons": list(self.excluded_delta_reasons),
            "status": self.status,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class PMReviewFailureRecord:
    failure_id: str
    pm_review_request_id: str
    target_key: str
    business_at: datetime
    failed_at: datetime
    stage: PMReviewFailureStage
    error_type: str
    error_message: str
    retry_allowed: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "failure_id",
            "pm_review_request_id",
            "error_type",
            "error_message",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_non_blank(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PMReviewContractError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(
            self,
            "failed_at",
            validate_timestamp(
                self.failed_at,
                field_name="failed_at",
                error_type=PMReviewContractError,
            ),
        )
        object.__setattr__(self, "stage", _validate_failure_stage(self.stage))
        if not isinstance(self.retry_allowed, bool):
            raise PMReviewContractError("retry_allowed must be a boolean.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "failure_id": self.failure_id,
            "pm_review_request_id": self.pm_review_request_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "failed_at": self.failed_at.isoformat(),
            "stage": self.stage,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "retry_allowed": self.retry_allowed,
        }


def derive_pm_review_failure_id(
    *,
    pm_review_request_id: str,
    stage: PMReviewFailureStage,
    error_type: str,
    error_message: str,
    failed_at: datetime,
) -> str:
    request_id = _validate_non_blank(pm_review_request_id, "pm_review_request_id")
    normalized_stage = cast(PMReviewFailureStage, _validate_non_blank(stage, "stage"))
    normalized_error_type = _validate_non_blank(error_type, "error_type")
    normalized_error_message = _validate_non_blank(error_message, "error_message")
    normalized_failed_at = validate_timestamp(
        failed_at,
        field_name="failed_at",
        error_type=PMReviewContractError,
    )
    digest = sha256(
        "|".join(
            (
                request_id,
                normalized_stage,
                normalized_error_type,
                normalized_error_message,
                normalized_failed_at.isoformat(),
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"pm-review-failure:{digest}"


def derive_pm_review_dispatch_consideration_id(
    *,
    request_id: str,
    dispatch_run_until: datetime,
    outcome: PMReviewDispatchOutcome,
    skip_reason: str | None,
) -> str:
    normalized_request_id = _validate_non_blank(request_id, "request_id")
    normalized_run_until = validate_timestamp(
        dispatch_run_until,
        field_name="dispatch_run_until",
        error_type=PMReviewContractError,
    )
    if outcome not in _DISPATCH_OUTCOMES:
        raise PMReviewContractError("outcome is not a supported dispatch outcome.")
    normalized_skip_reason = _validate_optional_text(skip_reason, "skip_reason")
    digest = sha256(
        "|".join(
            (
                normalized_request_id,
                normalized_run_until.isoformat(),
                outcome,
                "" if normalized_skip_reason is None else normalized_skip_reason,
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"pm-review-dispatch-consideration:{digest}"


def parse_pm_review_request(payload: Mapping[str, object]) -> PMReviewRequest:
    return PMReviewRequest(
        request_id=_require_text(payload, "request_id"),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        source=cast(PMReviewSource, _require_text(payload, "source")),
        source_assessment_id=_optional_text(payload.get("source_assessment_id")),
        source_episode_id=_optional_text(payload.get("source_episode_id")),
        source_event_ids=tuple(_require_string_list(payload, "source_event_ids")),
        review_reasons=tuple(
            cast(PMReviewReason, value)
            for value in _require_string_list(payload, "review_reasons")
        ),
        current_exposure_required=_require_bool(payload, "current_exposure_required"),
        candidate_review_allowed=_require_bool(payload, "candidate_review_allowed"),
        max_visible_event_time=_parse_timestamp(
            payload.get("max_visible_event_time"),
            "max_visible_event_time",
        ),
        max_visible_market_time=_parse_timestamp(
            payload.get("max_visible_market_time"),
            "max_visible_market_time",
        ),
        required_price_level_ids=tuple(
            _require_string_list(payload, "required_price_level_ids")
        ),
        required_claim_ids=tuple(_require_string_list(payload, "required_claim_ids")),
        candidate_anchor_id=_optional_text(payload.get("candidate_anchor_id")),
    )


def parse_pm_analysis_snapshot(payload: Mapping[str, object]) -> PMAnalysisSnapshot:
    _require_exact_fields(
        payload,
        required={
            "assessment_id",
            "target_key",
            "business_at",
            "as_if_flat_state",
            "if_flat_implication_md",
            "if_already_long_implication_md",
            "price_level_roles",
            "market_setup_dashboard_md",
            "key_claim_ids",
            "contested_prior_claim_ids",
            "pm_review_reasons",
            "confidence",
        },
        optional={"if_already_short_implication_md"},
        surface="pm_analysis_snapshot",
    )
    raw_level_roles = payload.get("price_level_roles")
    if not isinstance(raw_level_roles, list):
        raise PMReviewContractError("price_level_roles must be a list.")
    price_level_roles: list[PriceLevelRole] = []
    for index, item in enumerate(raw_level_roles):
        if not isinstance(item, Mapping):
            raise PMReviewContractError(
                f"pm_analysis_snapshot.price_level_roles[{index}] must be a JSON object."
            )
        try:
            price_level_roles.append(parse_price_level_role(item))
        except PriceLevelRoleContractError as exc:
            raise PMReviewContractError(
                f"pm_analysis_snapshot.price_level_roles[{index}] is invalid: {exc}"
            ) from exc
    return PMAnalysisSnapshot(
        assessment_id=_require_text(payload, "assessment_id"),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        as_if_flat_state=cast(ViewState, _require_text(payload, "as_if_flat_state")),
        if_flat_implication_md=_require_text(payload, "if_flat_implication_md"),
        if_already_long_implication_md=_require_text(
            payload,
            "if_already_long_implication_md",
        ),
        if_already_short_implication_md=_require_optional_markdown(
            payload,
            "if_already_short_implication_md",
        ),
        price_level_roles=tuple(price_level_roles),
        market_setup_dashboard_md=_require_text(payload, "market_setup_dashboard_md"),
        key_claim_ids=tuple(_require_string_list(payload, "key_claim_ids")),
        contested_prior_claim_ids=tuple(
            _require_string_list(payload, "contested_prior_claim_ids")
        ),
        pm_review_reasons=tuple(
            cast(PMReviewReason, value)
            for value in _require_string_list(payload, "pm_review_reasons")
        ),
        confidence=cast(Confidence, _require_text(payload, "confidence")),
    )


def parse_pm_review_input(payload: Mapping[str, object]) -> PMReviewInput:
    _require_exact_fields(
        payload,
        required={
            "pm_review_request_id",
            "target_key",
            "business_at",
            "source",
            "source_assessment_id",
            "source_episode_id",
            "source_event_ids",
            "review_reasons",
            "current_exposure_required",
            "candidate_review_allowed",
            "execution_direction_mode",
            "allowed_requested_states",
            "decision_visibility",
            "required_price_level_ids",
            "required_claim_ids",
            "actual_current_state",
            "actual_target_weight_before",
            "analysis_snapshot",
            "portfolio_risk_snapshot",
            "visible_evidence",
            "visible_market_bars",
            "visible_pm_history",
            "source_episode_memory_view",
            "source_episode_memory_read_receipt",
            "candidate_anchor",
        },
        surface="pm_review_input",
    )
    raw_analysis_snapshot = payload.get("analysis_snapshot")
    if raw_analysis_snapshot is not None and not isinstance(raw_analysis_snapshot, Mapping):
        raise PMReviewContractError("analysis_snapshot must be a JSON object or null.")
    raw_portfolio_risk_snapshot = payload.get("portfolio_risk_snapshot")
    if raw_portfolio_risk_snapshot is not None and not isinstance(
        raw_portfolio_risk_snapshot,
        Mapping,
    ):
        raise PMReviewContractError(
            "portfolio_risk_snapshot must be a JSON object or null."
        )
    raw_source_episode_memory_view = payload.get("source_episode_memory_view")
    if raw_source_episode_memory_view is not None and not isinstance(
        raw_source_episode_memory_view,
        Mapping,
    ):
        raise PMReviewContractError("source_episode_memory_view must be a JSON object or null.")
    raw_source_episode_memory_read_receipt = payload.get(
        "source_episode_memory_read_receipt"
    )
    if raw_source_episode_memory_read_receipt is not None and not isinstance(
        raw_source_episode_memory_read_receipt,
        Mapping,
    ):
        raise PMReviewContractError(
            "source_episode_memory_read_receipt must be a JSON object or null."
        )
    raw_candidate_anchor = payload.get("candidate_anchor")
    if raw_candidate_anchor is not None and not isinstance(raw_candidate_anchor, Mapping):
        raise PMReviewContractError("candidate_anchor must be a JSON object or null.")
    execution_direction_mode = cast(
        PMReviewExecutionDirectionMode,
        _require_text(payload, "execution_direction_mode"),
    )
    expected_allowed_requested_states = allowed_requested_states_for_execution_direction_mode(
        execution_direction_mode
    )
    serialized_allowed_requested_states = tuple(
        cast(ViewState, value)
        for value in _require_string_list(payload, "allowed_requested_states")
    )
    if serialized_allowed_requested_states != expected_allowed_requested_states:
        raise PMReviewContractError(
            "allowed_requested_states must match execution_direction_mode."
        )
    return PMReviewInput(
        pm_review_request_id=_require_text(payload, "pm_review_request_id"),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        source=cast(PMReviewSource, _require_text(payload, "source")),
        source_assessment_id=_optional_text(payload.get("source_assessment_id")),
        source_episode_id=_optional_text(payload.get("source_episode_id")),
        source_event_ids=tuple(_require_string_list(payload, "source_event_ids")),
        review_reasons=tuple(
            cast(PMReviewReason, value)
            for value in _require_string_list(payload, "review_reasons")
        ),
        current_exposure_required=_require_bool(payload, "current_exposure_required"),
        candidate_review_allowed=_require_bool(payload, "candidate_review_allowed"),
        execution_direction_mode=execution_direction_mode,
        decision_visibility=_parse_decision_visibility(
            payload.get("decision_visibility"),
            "decision_visibility",
        ),
        required_price_level_ids=tuple(
            _require_string_list(payload, "required_price_level_ids")
        ),
        required_claim_ids=tuple(_require_string_list(payload, "required_claim_ids")),
        actual_current_state=cast(ViewState, _require_text(payload, "actual_current_state")),
        actual_target_weight_before=_require_float(payload, "actual_target_weight_before"),
        analysis_snapshot=(
            None
            if raw_analysis_snapshot is None
            else parse_pm_analysis_snapshot(raw_analysis_snapshot)
        ),
        portfolio_risk_snapshot=(
            None
            if raw_portfolio_risk_snapshot is None
            else parse_pm_portfolio_risk_snapshot(raw_portfolio_risk_snapshot)
        ),
        visible_evidence=tuple(_parse_visible_evidence(payload, "visible_evidence")),
        visible_market_bars=tuple(
            _parse_visible_market_bars(payload, "visible_market_bars")
        ),
        visible_pm_history=tuple(
            _parse_visible_pm_history_requests(payload, "visible_pm_history")
        ),
        source_episode_memory_view=(
            None
            if raw_source_episode_memory_view is None
            else _parse_pm_episode_memory_view(raw_source_episode_memory_view)
        ),
        source_episode_memory_read_receipt=(
            None
            if raw_source_episode_memory_read_receipt is None
            else parse_pm_review_episode_memory_read_receipt(
                raw_source_episode_memory_read_receipt
            )
        ),
        candidate_anchor=(
            None
            if raw_candidate_anchor is None
            else parse_candidate_review_anchor(raw_candidate_anchor)
        ),
    )


def parse_pm_decision_draft(payload: Mapping[str, object]) -> PMDecisionDraft:
    _require_exact_fields(
        payload,
        required={
            "pm_review_request_id",
            "target_key",
            "business_at",
            "requested_state",
            "rationale_md",
            "cited_event_ids",
            "cited_market_bar_ids",
            "fallback_state_if_clamped",
            "fallback_rationale_md",
            "hold_reason_code",
        },
        surface="pm_decision_draft",
    )
    return PMDecisionDraft(
        pm_review_request_id=_require_text(payload, "pm_review_request_id"),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        requested_state=cast(ViewState, _require_text(payload, "requested_state")),
        rationale_md=_require_text(payload, "rationale_md"),
        cited_event_ids=tuple(_require_string_list(payload, "cited_event_ids")),
        cited_market_bar_ids=tuple(
            _require_string_list(payload, "cited_market_bar_ids")
        ),
        fallback_state_if_clamped=cast(
            ViewState | None,
            _optional_text(payload.get("fallback_state_if_clamped")),
        ),
        fallback_rationale_md=_optional_text(payload.get("fallback_rationale_md")),
        hold_reason_code=_optional_text(payload.get("hold_reason_code")),
    )


def parse_pm_position_review_triggers(
    payload: Mapping[str, object],
) -> PMPositionReviewTriggers:
    _require_exact_fields(
        payload,
        required={
            "pm_review_request_id",
            "target_key",
            "business_at",
            "requested_state",
            "review_trigger_level_ids",
            "invalidation_trigger_level_ids",
            "path_context_required",
            "market_confirmation_required",
            "trigger_reason_codes",
        },
        surface="pm_position_review_triggers",
    )
    return PMPositionReviewTriggers(
        pm_review_request_id=_require_text(payload, "pm_review_request_id"),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        requested_state=cast(ViewState, _require_text(payload, "requested_state")),
        review_trigger_level_ids=tuple(
            _require_string_list(payload, "review_trigger_level_ids")
        ),
        invalidation_trigger_level_ids=tuple(
            _require_string_list(payload, "invalidation_trigger_level_ids")
        ),
        path_context_required=_require_bool(payload, "path_context_required"),
        market_confirmation_required=_require_bool(
            payload,
            "market_confirmation_required",
        ),
        trigger_reason_codes=tuple(
            _require_string_list(payload, "trigger_reason_codes")
        ),
    )


def parse_pm_review_failure_record(payload: Mapping[str, object]) -> PMReviewFailureRecord:
    return PMReviewFailureRecord(
        failure_id=_require_text(payload, "failure_id"),
        pm_review_request_id=_require_text(payload, "pm_review_request_id"),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        failed_at=_parse_timestamp(payload.get("failed_at"), "failed_at"),
        stage=cast(PMReviewFailureStage, _require_text(payload, "stage")),
        error_type=_require_text(payload, "error_type"),
        error_message=_require_text(payload, "error_message"),
        retry_allowed=_require_bool(payload, "retry_allowed"),
    )


def parse_candidate_review_anchor(payload: Mapping[str, object]) -> CandidateReviewAnchor:
    return CandidateReviewAnchor(
        anchor_id=_require_text(payload, "anchor_id"),
        target_key=_require_text(payload, "target_key"),
        created_at=_parse_timestamp(payload.get("created_at"), "created_at"),
        source=cast(CandidateReviewAnchorSource, _require_text(payload, "source")),
        source_assessment_id=_optional_text(payload.get("source_assessment_id")),
        source_event_ids=tuple(_require_string_list(payload, "source_event_ids")),
        watch_trigger_ids=tuple(_require_string_list(payload, "watch_trigger_ids")),
        expires_at=_parse_timestamp(payload.get("expires_at"), "expires_at"),
    )


def parse_pm_review_tool_read_receipt(
    payload: Mapping[str, object],
) -> PMReviewToolReadReceipt:
    return PMReviewToolReadReceipt(
        read_id=_require_text(payload, "read_id"),
        pm_review_request_id=_require_text(payload, "pm_review_request_id"),
        tool_name=_require_text(payload, "tool_name"),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        requested_start_at=_parse_optional_timestamp(
            payload.get("requested_start_at"),
            "requested_start_at",
        ),
        requested_end_at=_parse_optional_timestamp(
            payload.get("requested_end_at"),
            "requested_end_at",
        ),
        enforced_max_visible_time=_parse_optional_timestamp(
            payload.get("enforced_max_visible_time"),
            "enforced_max_visible_time",
        ),
        returned_record_ids=tuple(_require_string_list(payload, "returned_record_ids")),
        returned_time_range_start=_parse_optional_timestamp(
            payload.get("returned_time_range_start"),
            "returned_time_range_start",
        ),
        returned_time_range_end=_parse_optional_timestamp(
            payload.get("returned_time_range_end"),
            "returned_time_range_end",
        ),
        visibility_passed=_require_bool(payload, "visibility_passed"),
    )


def parse_pm_review_dispatch_consideration(
    payload: Mapping[str, object],
) -> PMReviewDispatchConsideration:
    return PMReviewDispatchConsideration(
        consideration_id=_require_text(payload, "consideration_id"),
        request_id=_require_text(payload, "request_id"),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        dispatch_run_until=_parse_timestamp(
            payload.get("dispatch_run_until"),
            "dispatch_run_until",
        ),
        outcome=cast(PMReviewDispatchOutcome, _require_text(payload, "outcome")),
        skip_reason=_optional_text(payload.get("skip_reason")),
    )


def parse_pm_review_episode_memory_read_receipt(
    payload: Mapping[str, object],
) -> PMReviewEpisodeMemoryReadReceipt:
    return PMReviewEpisodeMemoryReadReceipt(
        request_id=_require_text(payload, "request_id"),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        source_episode_id=_optional_text(payload.get("source_episode_id")),
        projection_path=_optional_text(payload.get("projection_path")),
        projection_hash=_optional_text(payload.get("projection_hash")),
        built_from_delta_ids=tuple(_require_string_list(payload, "built_from_delta_ids")),
        built_from_delta_hashes=tuple(
            _require_string_list(payload, "built_from_delta_hashes")
        ),
        source_visible_through_max=_parse_optional_timestamp(
            payload.get("source_visible_through_max"),
            "source_visible_through_max",
        ),
        usable_from_max=_parse_optional_timestamp(
            payload.get("usable_from_max"),
            "usable_from_max",
        ),
        excluded_delta_ids=tuple(_require_string_list(payload, "excluded_delta_ids")),
        excluded_delta_reasons=tuple(
            _require_string_list(payload, "excluded_delta_reasons")
        ),
        status=cast(
            PMReviewEpisodeMemoryReadReceiptStatus,
            _require_text(payload, "status"),
        ),
        failure_reason=_optional_text(payload.get("failure_reason")),
    )


def _parse_pm_episode_memory_view_delta(
    payload: Mapping[str, object],
) -> PMEpisodeMemoryViewDelta:
    _require_exact_fields(
        payload,
        required={
            "delta_id",
            "summary_md",
            "thesis_delta",
            "pm_management_delta",
            "risk_delta",
            "invalidation_delta",
            "source_refs",
        },
        surface="pm_episode_memory_view_delta",
    )
    raw_source_refs = payload.get("source_refs")
    if not isinstance(raw_source_refs, Mapping):
        raise PMReviewContractError("source_refs must be a JSON object.")
    return PMEpisodeMemoryViewDelta(
        delta_id=_require_text(payload, "delta_id"),
        summary_md=_require_text(payload, "summary_md"),
        thesis_delta=_require_text(payload, "thesis_delta"),
        pm_management_delta=_require_text(payload, "pm_management_delta"),
        risk_delta=_require_text(payload, "risk_delta"),
        invalidation_delta=_require_text(payload, "invalidation_delta"),
        source_refs=parse_episode_memory_delta_source_refs(raw_source_refs),
    )


def _parse_pm_episode_memory_view(payload: Mapping[str, object]) -> PMEpisodeMemoryView:
    _require_exact_fields(
        payload,
        required={
            "episode_id",
            "target_key",
            "business_at",
            "provisional_warnings",
            "validated_lessons",
            "finalized_hindsight",
            "built_from_delta_ids",
            "built_from_delta_hashes",
            "excluded_delta_ids",
            "excluded_delta_reasons",
            "source_visible_through_max",
            "usable_from_max",
            "drilldown_refs",
        },
        surface="pm_episode_memory_view",
    )
    for field_name in (
        "provisional_warnings",
        "validated_lessons",
        "finalized_hindsight",
        "drilldown_refs",
    ):
        raw_value = payload.get(field_name)
        if not isinstance(raw_value, list) or not all(
            isinstance(item, Mapping) for item in raw_value
        ):
            raise PMReviewContractError(f"{field_name} must be a list of JSON objects.")
    return PMEpisodeMemoryView(
        episode_id=_require_text(payload, "episode_id"),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        provisional_warnings=tuple(
            _parse_pm_episode_memory_view_delta(cast(Mapping[str, object], item))
            for item in cast(list[Mapping[str, object]], payload.get("provisional_warnings"))
        ),
        validated_lessons=tuple(
            _parse_pm_episode_memory_view_delta(cast(Mapping[str, object], item))
            for item in cast(list[Mapping[str, object]], payload.get("validated_lessons"))
        ),
        finalized_hindsight=tuple(
            _parse_pm_episode_memory_view_delta(cast(Mapping[str, object], item))
            for item in cast(list[Mapping[str, object]], payload.get("finalized_hindsight"))
        ),
        built_from_delta_ids=tuple(_require_string_list(payload, "built_from_delta_ids")),
        built_from_delta_hashes=tuple(
            _require_string_list(payload, "built_from_delta_hashes")
        ),
        excluded_delta_ids=tuple(_require_string_list(payload, "excluded_delta_ids")),
        excluded_delta_reasons=tuple(
            _require_string_list(payload, "excluded_delta_reasons")
        ),
        source_visible_through_max=_parse_timestamp(
            payload.get("source_visible_through_max"),
            "source_visible_through_max",
        ),
        usable_from_max=_parse_timestamp(
            payload.get("usable_from_max"),
            "usable_from_max",
        ),
        drilldown_refs=tuple(
            parse_episode_memory_delta_source_refs(cast(Mapping[str, object], item))
            for item in cast(list[Mapping[str, object]], payload.get("drilldown_refs"))
        ),
        projection_hash="",
    )


def analysis_snapshot_from_assessment(
    assessment: AnalysisAssessment,
) -> PMAnalysisSnapshot:
    market_setup_dashboard_md = projected_market_setup_dashboard_md(assessment)
    return PMAnalysisSnapshot(
        assessment_id=assessment.assessment_id,
        target_key=assessment.target_key,
        business_at=assessment.business_at,
        as_if_flat_state=assessment.as_if_flat_state,
        if_flat_implication_md=assessment.if_flat_implication_md,
        if_already_long_implication_md=assessment.if_already_long_implication_md,
        if_already_short_implication_md=assessment.if_already_short_implication_md,
        price_level_roles=assessment.price_level_roles,
        market_setup_dashboard_md=(
            assessment.market_setup_dashboard_md
            if market_setup_dashboard_md is None
            else market_setup_dashboard_md
        ),
        key_claim_ids=assessment.key_claim_ids,
        contested_prior_claim_ids=assessment.contested_prior_claim_ids,
        pm_review_reasons=assessment.pm_review_reasons,
        confidence=cast(Confidence, assessment.confidence),
    )


def _validate_price_level_roles(
    values: tuple[PriceLevelRole, ...],
    target_key: str,
) -> tuple[PriceLevelRole, ...]:
    normalized = tuple(values)
    seen: set[str] = set()
    for value in normalized:
        if not isinstance(value, PriceLevelRole):
            raise PMReviewContractError(
                "price_level_roles must contain only PriceLevelRole instances."
            )
        if value.target_key != target_key:
            raise PMReviewContractError(
                "price_level_roles target_key must match parent target_key."
            )
        if value.level_id in seen:
            raise PMReviewContractError(
                "price_level_roles must not contain duplicate level_id values."
            )
        seen.add(value.level_id)
    return normalized


def _validate_visible_evidence(
    values: tuple[EvidenceLedgerRecord, ...],
    *,
    target_key: str,
    max_visible_event_time: datetime,
) -> tuple[EvidenceLedgerRecord, ...]:
    normalized = tuple(values)
    seen: set[str] = set()
    for value in normalized:
        if not isinstance(value, EvidenceLedgerRecord):
            raise PMReviewContractError(
                "visible_evidence must contain only EvidenceLedgerRecord values."
            )
        if value.target_key != target_key:
            raise PMReviewContractError(
                "visible_evidence target_key must match target_key."
            )
        if value.ts_event > max_visible_event_time:
            raise PMReviewContractError(
                "visible_evidence exceeds decision_visibility.max_visible_event_time."
            )
        if value.event_id in seen:
            raise PMReviewContractError(
                "visible_evidence must not contain duplicate event_id values."
            )
        seen.add(value.event_id)
    return normalized


def _validate_visible_market_bars(
    values: tuple[MarketDataBar, ...],
    *,
    max_visible_market_time: datetime,
) -> tuple[MarketDataBar, ...]:
    normalized = tuple(values)
    seen: set[str] = set()
    for value in normalized:
        if not isinstance(value, MarketDataBar):
            raise PMReviewContractError(
                "visible_market_bars must contain only MarketDataBar values."
            )
        if value.end_at > max_visible_market_time:
            raise PMReviewContractError(
                "visible_market_bars exceeds decision_visibility.max_visible_market_time."
            )
        bar_id = _market_bar_id(value)
        if bar_id in seen:
            raise PMReviewContractError(
                "visible_market_bars must not contain duplicate market bars."
            )
        seen.add(bar_id)
    return normalized


def _validate_visible_pm_history(
    values: tuple[PMReviewRequest, ...],
    *,
    target_key: str,
    request_id: str,
    business_at: datetime,
) -> tuple[PMReviewRequest, ...]:
    normalized = tuple(values)
    seen: set[str] = set()
    for value in normalized:
        if not isinstance(value, PMReviewRequest):
            raise PMReviewContractError(
                "visible_pm_history must contain only PMReviewRequest values."
            )
        if value.target_key != target_key:
            raise PMReviewContractError(
                "visible_pm_history target_key must match target_key."
            )
        if value.request_id == request_id:
            raise PMReviewContractError(
                "visible_pm_history must exclude the active pm_review_request_id."
            )
        if value.business_at >= business_at:
            raise PMReviewContractError(
                "visible_pm_history must contain only records before business_at."
            )
        if value.request_id in seen:
            raise PMReviewContractError(
                "visible_pm_history must not contain duplicate request_id values."
            )
        seen.add(value.request_id)
    return normalized


def _validate_input_candidate_anchor(value: PMReviewInput) -> None:
    if value.candidate_anchor is None:
        if _request_requires_candidate_anchor(
            source=value.source,
            review_reasons=value.review_reasons,
        ):
            raise PMReviewContractError(
                "candidate_anchor is required for this PMReviewInput."
            )
        return
    if not isinstance(value.candidate_anchor, CandidateReviewAnchor):
        raise PMReviewContractError(
            "candidate_anchor must be a CandidateReviewAnchor when provided."
        )
    if value.candidate_anchor.target_key != value.target_key:
        raise PMReviewContractError("candidate_anchor target_key must match target_key.")
    if not value.candidate_review_allowed:
        raise PMReviewContractError(
            "candidate_anchor requires candidate_review_allowed=true."
        )
    if value.candidate_anchor.expires_at <= value.business_at:
        raise PMReviewContractError("candidate_anchor must be unexpired at business_at.")
    if value.source_assessment_id is not None:
        if value.candidate_anchor.source_assessment_id not in {
            None,
            value.source_assessment_id,
        }:
            raise PMReviewContractError(
                "candidate_anchor source_assessment_id must match source_assessment_id "
                "when present."
            )
    if not set(value.candidate_anchor.source_event_ids).issubset(value.source_event_ids):
        raise PMReviewContractError(
            "candidate_anchor source_event_ids must be visible to the request."
        )


def _request_requires_candidate_anchor(
    *,
    source: PMReviewSource,
    review_reasons: tuple[PMReviewReason, ...],
) -> bool:
    return source == "flat_candidate" or "flat_candidate_setup" in review_reasons


def _decision_visibility_to_json_payload(
    value: DecisionVisibilityBoundary,
) -> dict[str, object]:
    return {
        "business_at": value.business_at.isoformat(),
        "max_visible_event_time": value.max_visible_event_time.isoformat(),
        "max_visible_market_time": value.max_visible_market_time.isoformat(),
    }


def _parse_decision_visibility(
    value: object,
    field_name: str,
) -> DecisionVisibilityBoundary:
    if not isinstance(value, Mapping):
        raise PMReviewContractError(f"{field_name} must be a JSON object.")
    _require_exact_fields(
        value,
        required={
            "business_at",
            "max_visible_event_time",
            "max_visible_market_time",
        },
        surface=field_name,
    )
    try:
        return DecisionVisibilityBoundary(
            business_at=_parse_timestamp(value.get("business_at"), "business_at"),
            max_visible_event_time=_parse_timestamp(
                value.get("max_visible_event_time"),
                "max_visible_event_time",
            ),
            max_visible_market_time=_parse_timestamp(
                value.get("max_visible_market_time"),
                "max_visible_market_time",
            ),
        )
    except DecisionVisibilityBoundaryError as exc:
        raise PMReviewContractError(str(exc)) from exc


def _evidence_record_to_json_payload(record: EvidenceLedgerRecord) -> dict[str, object]:
    return {
        "event_id": record.event_id,
        "target_key": record.target_key,
        "source_ref": record.source_ref,
        "title": record.title,
        "content": record.content,
        "labels": list(record.labels),
        "ts_source": record.ts_source.isoformat(),
        "ts_event": record.ts_event.isoformat(),
        "ts_init": record.ts_init.isoformat(),
    }


def _market_bar_to_json_payload(record: MarketDataBar) -> dict[str, object]:
    return {
        "bar_id": _market_bar_id(record),
        "start_at": record.start_at.isoformat(),
        "end_at": record.end_at.isoformat(),
        "open_price": record.open_price,
        "high_price": record.high_price,
        "low_price": record.low_price,
        "close_price": record.close_price,
        "volume": record.volume,
        "vwap": record.vwap,
    }


def _market_bar_id(value: MarketDataBar) -> str:
    return f"bar:{value.start_at.isoformat()}:{value.end_at.isoformat()}"


def _parse_visible_evidence(
    payload: Mapping[str, object],
    field_name: str,
) -> tuple[EvidenceLedgerRecord, ...]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise PMReviewContractError(f"{field_name} must be a list.")
    items: list[EvidenceLedgerRecord] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise PMReviewContractError(
                f"{field_name}[{index}] must be a JSON object."
            )
        _require_exact_fields(
            item,
            required={
                "event_id",
                "target_key",
                "source_ref",
                "title",
                "content",
                "labels",
                "ts_source",
                "ts_event",
                "ts_init",
            },
            surface=f"{field_name}[{index}]",
        )
        labels = item.get("labels")
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise PMReviewContractError(f"{field_name}[{index}].labels must be a list of strings.")
        items.append(
            EvidenceLedgerRecord(
                event_id=_require_text(item, "event_id"),
                target_key=_require_text(item, "target_key"),
                source_ref=_require_text(item, "source_ref"),
                title=_require_text(item, "title"),
                content=_require_text(item, "content"),
                labels=list(labels),
                ts_source=_parse_timestamp(item.get("ts_source"), "ts_source"),
                ts_event=_parse_timestamp(item.get("ts_event"), "ts_event"),
                ts_init=_parse_timestamp(item.get("ts_init"), "ts_init"),
            )
        )
    return tuple(items)


def _parse_visible_market_bars(
    payload: Mapping[str, object],
    field_name: str,
) -> tuple[MarketDataBar, ...]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise PMReviewContractError(f"{field_name} must be a list.")
    items: list[MarketDataBar] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise PMReviewContractError(
                f"{field_name}[{index}] must be a JSON object."
            )
        _require_exact_fields(
            item,
            required={
                "bar_id",
                "start_at",
                "end_at",
                "open_price",
                "high_price",
                "low_price",
                "close_price",
                "volume",
                "vwap",
            },
            surface=f"{field_name}[{index}]",
        )
        bar = MarketDataBar(
            start_at=_parse_timestamp(item.get("start_at"), "start_at"),
            end_at=_parse_timestamp(item.get("end_at"), "end_at"),
            open_price=_require_float(item, "open_price"),
            high_price=_require_float(item, "high_price"),
            low_price=_require_float(item, "low_price"),
            close_price=_require_float(item, "close_price"),
            volume=_require_float(item, "volume"),
            vwap=_optional_float(item.get("vwap")),
        )
        if _require_text(item, "bar_id") != _market_bar_id(bar):
            raise PMReviewContractError(
                f"{field_name}[{index}].bar_id must match its market-bar timestamps."
            )
        items.append(bar)
    return tuple(items)


def _parse_visible_pm_history_requests(
    payload: Mapping[str, object],
    field_name: str,
) -> tuple[PMReviewRequest, ...]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise PMReviewContractError(f"{field_name} must be a list.")
    items: list[PMReviewRequest] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise PMReviewContractError(
                f"{field_name}[{index}] must be a JSON object."
            )
        items.append(parse_pm_review_request(item))
    return tuple(items)


def _validate_non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PMReviewContractError(f"{field_name} must be a non-blank string.")
    normalized = value.strip()
    if normalized != value:
        raise PMReviewContractError(
            f"{field_name} must not include leading or trailing whitespace."
        )
    return normalized


def _validate_optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_non_blank(value, field_name)


def _validate_optional_markdown(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PMReviewContractError(f"{field_name} must be a string when set.")
    return normalize_content(value, field_name=field_name, error_type=PMReviewContractError)


def _validate_reason_code(value: object, field_name: str) -> str:
    text = _validate_non_blank(value, field_name)
    if not re.fullmatch(r"[a-z][a-z0-9_]*", text):
        raise PMReviewContractError(
            f"{field_name} must be lower_snake_case when set."
        )
    return text


def _validate_optional_reason_code(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_reason_code(value, field_name)


def _validate_reason_code_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized and not allow_empty:
        raise PMReviewContractError(f"{field_name} must not be empty.")
    seen: set[str] = set()
    for value in normalized:
        code = _validate_reason_code(value, field_name)
        if code in seen:
            raise PMReviewContractError(f"{field_name} must not contain duplicates.")
        seen.add(code)
    return normalized


def _validate_sha256_hex(value: object, field_name: str) -> str:
    text = _validate_non_blank(value, field_name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PMReviewContractError(
            f"{field_name} must be a lower-case sha256 hex digest."
        )
    return text


def _optional_sha256_hex(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_sha256_hex(value, field_name)


def _validate_sha256_hash_tuple(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise PMReviewContractError(f"{field_name} must be a tuple.")
    normalized = values
    for value in normalized:
        _validate_sha256_hex(value, field_name)
    return normalized


def _validate_event_ids(values: tuple[str, ...], *, allow_empty: bool) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized and not allow_empty:
        raise PMReviewContractError("event id tuple must not be empty.")
    seen: set[str] = set()
    for value in normalized:
        validated = validate_event_id(value, error_type=PMReviewContractError)
        if validated in seen:
            raise PMReviewContractError("event id tuple must not contain duplicates.")
        seen.add(validated)
    return normalized


def _validate_string_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized and not allow_empty:
        raise PMReviewContractError(f"{field_name} must not be empty.")
    seen: set[str] = set()
    for value in normalized:
        text = _validate_non_blank(value, field_name)
        if text in seen:
            raise PMReviewContractError(f"{field_name} must not contain duplicates.")
        seen.add(text)
    return normalized


def _validate_review_source(value: object) -> PMReviewSource:
    if value not in _PM_REVIEW_SOURCES:
        raise PMReviewContractError("PM review source is not supported.")
    return cast(PMReviewSource, value)


def _validate_anchor_source(value: object) -> CandidateReviewAnchorSource:
    if value not in _ANCHOR_SOURCES:
        raise PMReviewContractError("candidate anchor source is not supported.")
    return cast(CandidateReviewAnchorSource, value)


def _validate_failure_stage(value: object) -> PMReviewFailureStage:
    if value == "pm_review_run":
        return "pm_review_run"
    if value == "pm_review_run_and_execute":
        return "pm_review_run_and_execute"
    raise PMReviewContractError("PM review failure stage is not supported.")


def _validate_confidence(value: object) -> Confidence:
    if value not in _CONFIDENCE_VALUES:
        raise PMReviewContractError("confidence must be low, medium, or high.")
    return cast(Confidence, value)


def _validate_execution_direction_mode(value: object) -> PMReviewExecutionDirectionMode:
    return cast(
        PMReviewExecutionDirectionMode,
        validate_execution_direction_mode(
            value,
            error_type=PMReviewContractError,
        ),
    )


def allowed_requested_states_for_execution_direction_mode(
    execution_direction_mode: PMReviewExecutionDirectionMode,
) -> tuple[ViewState, ...]:
    return allowed_view_states_for_execution_direction_mode(
        _validate_execution_direction_mode(execution_direction_mode)
    )


def requested_state_allowed_for_execution_direction_mode(
    requested_state: ViewState,
    execution_direction_mode: PMReviewExecutionDirectionMode,
) -> bool:
    return requested_state in allowed_requested_states_for_execution_direction_mode(
        execution_direction_mode
    )


def _validate_view_state(value: object, field_name: str) -> ViewState:
    if value not in _STATE_TARGET_WEIGHT:
        raise PMReviewContractError(f"{field_name} is not a supported view state.")
    return cast(ViewState, value)


def _validate_finite_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PMReviewContractError(f"{field_name} must be numeric.")
    normalized = float(value)
    if not isfinite(normalized):
        raise PMReviewContractError(f"{field_name} must be finite.")
    return normalized


def _validate_optional_timestamp(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return validate_timestamp(
        cast(datetime, value),
        field_name=field_name,
        error_type=PMReviewContractError,
    )


def _require_exact_fields(
    payload: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] | None = None,
    surface: str,
) -> None:
    allowed_optional = optional or set()
    actual = set(str(key) for key in payload)
    missing = sorted(required - actual)
    unexpected = sorted(actual - required - allowed_optional)
    if not missing and not unexpected:
        return
    problems: list[str] = []
    if missing:
        problems.append(f"missing fields: {', '.join(missing)}")
    if unexpected:
        problems.append(f"unexpected fields: {', '.join(unexpected)}")
    raise PMReviewContractError(f"{surface} " + "; ".join(problems))


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_non_blank(payload.get(field_name), field_name)


def _require_optional_markdown(
    payload: Mapping[str, object],
    field_name: str,
) -> str | None:
    return _validate_optional_markdown(payload.get(field_name), field_name)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _validate_non_blank(value, "optional_text")


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return _validate_finite_float(value, "optional_float")


def _require_string_list(payload: Mapping[str, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PMReviewContractError(f"{field_name} must be a list of strings.")
    return value


def _require_bool(payload: Mapping[str, object], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise PMReviewContractError(f"{field_name} must be a boolean.")
    return value


def _require_float(payload: Mapping[str, object], field_name: str) -> float:
    return _validate_finite_float(payload.get(field_name), field_name)


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise PMReviewContractError(f"{field_name} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PMReviewContractError(f"{field_name} must be an ISO timestamp.") from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=PMReviewContractError,
    )


def parse_pm_portfolio_risk_snapshot(
    payload: Mapping[str, object],
) -> PMPortfolioRiskSnapshot:
    _require_exact_fields(
        payload,
        required={
            "episode_id",
            "segment_id",
            "instrument_basis",
            "segment_opened_at",
            "entry_reference_at",
            "entry_reference_price",
            "entry_source",
            "target_weight",
            "current_mark_price",
            "mark_as_of",
            "underlying_return",
            "strategy_return",
            "drawdown_from_best_strategy_return",
        },
        surface="pm_portfolio_risk_snapshot",
    )
    return PMPortfolioRiskSnapshot(
        episode_id=_require_text(payload, "episode_id"),
        segment_id=_require_text(payload, "segment_id"),
        instrument_basis=_require_text(payload, "instrument_basis"),
        segment_opened_at=_parse_timestamp(
            payload.get("segment_opened_at"), "segment_opened_at"
        ),
        entry_reference_at=_parse_timestamp(
            payload.get("entry_reference_at"), "entry_reference_at"
        ),
        entry_reference_price=_require_float(payload, "entry_reference_price"),
        entry_source=cast(
            PMPortfolioRiskEntrySource,
            _require_text(payload, "entry_source"),
        ),
        target_weight=_require_float(payload, "target_weight"),
        current_mark_price=_require_float(payload, "current_mark_price"),
        mark_as_of=_parse_timestamp(payload.get("mark_as_of"), "mark_as_of"),
        underlying_return=_require_float(payload, "underlying_return"),
        strategy_return=_require_float(payload, "strategy_return"),
        drawdown_from_best_strategy_return=_require_float(
            payload, "drawdown_from_best_strategy_return"
        ),
    )


def _parse_optional_timestamp(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_timestamp(value, field_name)


__all__ = [
    "CandidateReviewAnchor",
    "CandidateReviewAnchorSource",
    "Confidence",
    "PMAnalysisSnapshot",
    "PMDecisionDraft",
    "PMPortfolioRiskEntrySource",
    "PMPortfolioRiskSnapshot",
    "PMReviewExecutionDirectionMode",
    "PMPositionReviewTriggers",
    "PMReviewInput",
    "PMReviewDispatchConsideration",
    "PMReviewDispatchOutcome",
    "PMReviewContractError",
    "PMReviewFailureRecord",
    "PMReviewFailureStage",
    "PMReviewRequest",
    "PMReviewEpisodeMemoryReadReceipt",
    "PMReviewEpisodeMemoryReadReceiptStatus",
    "PMReviewSource",
    "PMReviewToolReadReceipt",
    "allowed_requested_states_for_execution_direction_mode",
    "analysis_snapshot_from_assessment",
    "derive_pm_review_dispatch_consideration_id",
    "derive_pm_review_failure_id",
    "is_terminal_pm_review_policy_skip_reason",
    "parse_candidate_review_anchor",
    "parse_pm_analysis_snapshot",
    "parse_pm_decision_draft",
    "parse_pm_portfolio_risk_snapshot",
    "parse_pm_position_review_triggers",
    "parse_pm_review_dispatch_consideration",
    "parse_pm_review_failure_record",
    "parse_pm_review_input",
    "parse_pm_review_request",
    "parse_pm_review_episode_memory_read_receipt",
    "parse_pm_review_tool_read_receipt",
    "pm_review_hold_reason_required",
    "requested_state_allowed_for_execution_direction_mode",
]
