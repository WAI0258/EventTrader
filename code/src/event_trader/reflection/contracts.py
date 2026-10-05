"""Typed reflection contracts outside the checker path.

Reflection is a separate review module, not a second analysis path and not a
checker-path concern. These contracts keep the seam narrow enough for future
composition while naming the review anchor, coverage window, run receipts, and
injected outcome/trade context packets explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Literal

from event_trader.contracts._validators import (
    normalize_content,
    validate_page_key,
    validate_scope_key,
    validate_target_key,
    validate_timestamp,
)
from event_trader.contracts.research_memory import resolve_reflection_anchor_page_ref

from .learning_contracts import ReflectionLearningDecision


class ReflectionContractError(ValueError):
    """Raised when reflection contracts or review context packets are malformed."""


@dataclass(frozen=True, slots=True)
class ReviewAnchorIdentity:
    """Identity for one review-eligible log entry."""

    scope_key: str
    target_key: str | None
    log_page_path: str
    entry_key: str
    logged_at: datetime

    def __post_init__(self) -> None:
        validated_scope_key = validate_scope_key(
            self.scope_key,
            error_type=ReflectionContractError,
        )
        normalized_target_key = _normalize_anchor_target_key(
            validated_scope_key,
            self.target_key,
        )
        expected_log_page_path = _expected_log_page_path(
            validated_scope_key,
            target_key=normalized_target_key,
        )
        if self.log_page_path != expected_log_page_path:
            raise ReflectionContractError(
                "log_page_path must be the canonical reflection anchor surface for "
                f"scope_key={validated_scope_key!r}; expected "
                f"{expected_log_page_path!r}."
            )

        object.__setattr__(self, "scope_key", validated_scope_key)
        object.__setattr__(self, "target_key", normalized_target_key)
        object.__setattr__(self, "log_page_path", expected_log_page_path)
        object.__setattr__(
            self,
            "entry_key",
            validate_page_key(
                self.entry_key,
                field_name="entry_key",
                error_type=ReflectionContractError,
            ),
        )
        object.__setattr__(
            self,
            "logged_at",
            validate_timestamp(
                self.logged_at,
                field_name="logged_at",
                error_type=ReflectionContractError,
            ),
        )


@dataclass(frozen=True, slots=True)
class ReviewCoverage:
    """Configured review lookback and horizon set for one reflection attempt."""

    lookback_hours: float
    horizons_hours: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "lookback_hours",
            _validate_positive_hours(
                self.lookback_hours,
                field_name="lookback_hours",
            ),
        )
        object.__setattr__(
            self,
            "horizons_hours",
            _normalize_horizons_hours(self.horizons_hours),
        )


@dataclass(frozen=True, slots=True)
class OutcomeContextPacket:
    """Read-only structured market/context outcome data for one review anchor."""

    anchor: ReviewAnchorIdentity
    coverage: ReviewCoverage
    observed_at: datetime
    summary_md: str

    def __post_init__(self) -> None:
        _validate_context_packet_members(
            anchor=self.anchor,
            coverage=self.coverage,
            observed_at=self.observed_at,
            summary_md=self.summary_md,
            packet_name="OutcomeContextPacket",
            owner=self,
        )


@dataclass(frozen=True, slots=True)
class TradeContextPacket:
    """Optional read-only trade outcome context for one review anchor."""

    anchor: ReviewAnchorIdentity
    coverage: ReviewCoverage
    observed_at: datetime
    summary_md: str

    def __post_init__(self) -> None:
        _validate_context_packet_members(
            anchor=self.anchor,
            coverage=self.coverage,
            observed_at=self.observed_at,
            summary_md=self.summary_md,
            packet_name="TradeContextPacket",
            owner=self,
        )


@dataclass(frozen=True, slots=True)
class ReflectionContextPacket:
    """Assembled reflection input packet without introducing a new truth surface."""

    anchor: ReviewAnchorIdentity
    coverage: ReviewCoverage
    outcome_context: OutcomeContextPacket
    trade_context: TradeContextPacket | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, ReviewAnchorIdentity):
            raise ReflectionContractError(
                "anchor must be a ReviewAnchorIdentity instance."
            )
        if not isinstance(self.coverage, ReviewCoverage):
            raise ReflectionContractError(
                "coverage must be a ReviewCoverage instance."
            )
        if not isinstance(self.outcome_context, OutcomeContextPacket):
            raise ReflectionContractError(
                "outcome_context must be an OutcomeContextPacket instance."
            )
        if self.outcome_context.anchor != self.anchor:
            raise ReflectionContractError(
                "outcome_context.anchor must match ReflectionContextPacket.anchor."
            )
        if self.outcome_context.coverage != self.coverage:
            raise ReflectionContractError(
                "outcome_context.coverage must match ReflectionContextPacket.coverage."
            )
        if self.trade_context is not None:
            if not isinstance(self.trade_context, TradeContextPacket):
                raise ReflectionContractError(
                    "trade_context must be a TradeContextPacket instance when provided."
                )
            if self.trade_context.anchor != self.anchor:
                raise ReflectionContractError(
                    "trade_context.anchor must match ReflectionContextPacket.anchor."
                )
            if self.trade_context.coverage != self.coverage:
                raise ReflectionContractError(
                    "trade_context.coverage must match ReflectionContextPacket.coverage."
                )


@dataclass(frozen=True, slots=True)
class ReflectionRunReceipt:
    """Observable receipt for one composition-ready reflection run attempt."""

    cadence_hours: float
    lookback_hours: float
    horizons_hours: tuple[float, ...]
    eligible_anchor_count: int
    missing_dependencies: tuple[str, ...] = ()
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cadence_hours",
            _validate_positive_hours(
                self.cadence_hours,
                field_name="cadence_hours",
            ),
        )
        object.__setattr__(
            self,
            "lookback_hours",
            _validate_positive_hours(
                self.lookback_hours,
                field_name="lookback_hours",
            ),
        )
        object.__setattr__(
            self,
            "horizons_hours",
            _normalize_horizons_hours(self.horizons_hours),
        )
        if not isinstance(self.eligible_anchor_count, int) or self.eligible_anchor_count < 0:
            raise ReflectionContractError("eligible_anchor_count must be >= 0.")
        object.__setattr__(
            self,
            "missing_dependencies",
            _normalize_missing_dependencies(self.missing_dependencies),
        )
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                normalize_content(
                    self.failure_reason,
                    field_name="failure_reason",
                    error_type=ReflectionContractError,
                ),
            )
        if self.missing_dependencies and self.failure_reason is None:
            raise ReflectionContractError(
                "failure_reason is required when missing_dependencies are reported."
            )


type ReflectionReviewDecision = Literal["write_review", "skip_review"]
type ReflectionPathVerdict = Literal["favorable", "mixed", "adverse", "unclear"]
type ReflectionTradabilityVerdict = Literal[
    "tradable",
    "mixed",
    "poor",
    "unclear",
]
type ReflectionThesisVerdict = Literal[
    "validated",
    "mixed",
    "invalidated",
    "unclear",
]
type ReflectionTradeVerdict = Literal[
    "win",
    "loss",
    "scratch",
    "missed",
    "not_taken",
    "open",
]
type MarketContextUsageQualityLabel = Literal[
    "ignored_available_market_context",
    "overtrusted_partial_market_context",
    "correct_context_wrong_positioning",
    "market_context_misleading",
    "context_unavailable_but_decision_needed",
    "not_applicable",
    "inconclusive",
]
MARKET_CONTEXT_USAGE_QUALITY_LABELS: tuple[MarketContextUsageQualityLabel, ...] = (
    "ignored_available_market_context",
    "overtrusted_partial_market_context",
    "correct_context_wrong_positioning",
    "market_context_misleading",
    "context_unavailable_but_decision_needed",
    "not_applicable",
    "inconclusive",
)
type ReflectionWatchlistVerdict = Literal[
    "useful",
    "mixed",
    "stale",
    "missing",
    "unclear",
]


@dataclass(frozen=True, slots=True)
class ReflectionHorizonAssessment:
    """Trader-style review assessment for one configured reflection horizon."""

    horizon_hours: float
    return_pct: float
    path_verdict: ReflectionPathVerdict
    tradability_verdict: ReflectionTradabilityVerdict
    summary_md: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "horizon_hours",
            _validate_positive_hours(
                self.horizon_hours,
                field_name="horizon_hours",
            ),
        )
        object.__setattr__(
            self,
            "return_pct",
            _validate_finite_number(
                self.return_pct,
                field_name="return_pct",
            ),
        )
        object.__setattr__(
            self,
            "path_verdict",
            _normalize_path_verdict(self.path_verdict),
        )
        object.__setattr__(
            self,
            "tradability_verdict",
            _normalize_tradability_verdict(self.tradability_verdict),
        )
        object.__setattr__(
            self,
            "summary_md",
            normalize_content(
                self.summary_md,
                field_name="summary_md",
                error_type=ReflectionContractError,
            ),
        )


@dataclass(frozen=True, slots=True)
class ReflectionThesisAssessment:
    """Trader-style verdict on whether the prior thesis held up."""

    verdict: ReflectionThesisVerdict
    summary_md: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", _normalize_thesis_verdict(self.verdict))
        object.__setattr__(
            self,
            "summary_md",
            normalize_content(
                self.summary_md,
                field_name="summary_md",
                error_type=ReflectionContractError,
            ),
        )


@dataclass(frozen=True, slots=True)
class ReflectionTradeAssessment:
    """Optional trader-style verdict on an executed or missed trade outcome."""

    verdict: ReflectionTradeVerdict
    summary_md: str
    return_pct: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", _normalize_trade_verdict(self.verdict))
        object.__setattr__(
            self,
            "summary_md",
            normalize_content(
                self.summary_md,
                field_name="summary_md",
                error_type=ReflectionContractError,
            ),
        )
        if self.return_pct is not None:
            object.__setattr__(
                self,
                "return_pct",
                _validate_finite_number(
                    self.return_pct,
                    field_name="return_pct",
                ),
            )
        if self.verdict == "not_taken" and self.return_pct is not None:
            raise ReflectionContractError(
                "return_pct must be None when verdict='not_taken'."
            )


@dataclass(frozen=True, slots=True)
class ReflectionExposureAssessment:
    """Reflection-only audit of open exposure, not a trading instruction."""

    summary_md: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "summary_md",
            normalize_content(
                self.summary_md,
                field_name="summary_md",
                error_type=ReflectionContractError,
            ),
        )


@dataclass(frozen=True, slots=True)
class ReflectionWatchlistAssessment:
    """Trader-style verdict on whether watchlist items helped future attention."""

    verdict: ReflectionWatchlistVerdict
    summary_md: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "verdict",
            _normalize_watchlist_verdict(self.verdict),
        )
        object.__setattr__(
            self,
            "summary_md",
            normalize_content(
                self.summary_md,
                field_name="summary_md",
                error_type=ReflectionContractError,
            ),
        )


@dataclass(frozen=True, slots=True)
class ReflectionEvaluationReceipt:
    """Observable receipt for one reflection evaluation decision."""

    anchor_id: str
    target_key: str | None
    assessed_horizons_hours: tuple[float, ...]
    review_decision: ReflectionReviewDecision
    decision_rationale: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "anchor_id", _normalize_anchor_id(self.anchor_id))
        if self.target_key is not None:
            object.__setattr__(
                self,
                "target_key",
                validate_target_key(
                    self.target_key,
                    error_type=ReflectionContractError,
                ),
            )
        object.__setattr__(
            self,
            "assessed_horizons_hours",
            _normalize_horizons_hours(self.assessed_horizons_hours),
        )
        object.__setattr__(
            self,
            "review_decision",
            _normalize_review_decision(self.review_decision),
        )
        object.__setattr__(
            self,
            "decision_rationale",
            normalize_content(
                self.decision_rationale,
                field_name="decision_rationale",
                error_type=ReflectionContractError,
            ),
        )


@dataclass(frozen=True, slots=True)
class ReflectionEvaluationResult:
    """Trader-style reflection evaluation output with explicit write-or-skip surface."""

    anchor: ReviewAnchorIdentity
    coverage: ReviewCoverage
    assessed_horizons: tuple[ReflectionHorizonAssessment, ...]
    thesis_assessment: ReflectionThesisAssessment
    trade_assessment: ReflectionTradeAssessment | None
    review_decision: ReflectionReviewDecision
    decision_rationale: str
    usage_quality_label: MarketContextUsageQualityLabel | None = None
    usage_quality_summary: str | None = None
    market_context_error_labels: tuple[MarketContextUsageQualityLabel, ...] = ()
    receipt: ReflectionEvaluationReceipt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, ReviewAnchorIdentity):
            raise ReflectionContractError(
                "anchor must be a ReviewAnchorIdentity instance."
            )
        if not isinstance(self.coverage, ReviewCoverage):
            raise ReflectionContractError(
                "coverage must be a ReviewCoverage instance."
            )
        object.__setattr__(
            self,
            "assessed_horizons",
            _normalize_horizon_assessments(self.assessed_horizons),
        )
        if not isinstance(self.thesis_assessment, ReflectionThesisAssessment):
            raise ReflectionContractError(
                "thesis_assessment must be a ReflectionThesisAssessment instance."
            )
        if self.trade_assessment is not None and not isinstance(
            self.trade_assessment,
            ReflectionTradeAssessment,
        ):
            raise ReflectionContractError(
                "trade_assessment must be a ReflectionTradeAssessment instance when provided."
            )
        object.__setattr__(
            self,
            "review_decision",
            _normalize_review_decision(self.review_decision),
        )
        object.__setattr__(
            self,
            "decision_rationale",
            normalize_content(
                self.decision_rationale,
                field_name="decision_rationale",
                error_type=ReflectionContractError,
            ),
        )
        _normalize_market_context_usage_quality(owner=self)

        assessed_horizons_hours = tuple(
            assessment.horizon_hours for assessment in self.assessed_horizons
        )
        if not set(assessed_horizons_hours).issubset(self.coverage.horizons_hours):
            raise ReflectionContractError(
                "assessed_horizons must stay within coverage.horizons_hours."
            )

        expected_receipt = ReflectionEvaluationReceipt(
            anchor_id=_anchor_id_for_identity(self.anchor),
            target_key=self.anchor.target_key,
            assessed_horizons_hours=assessed_horizons_hours,
            review_decision=self.review_decision,
            decision_rationale=self.decision_rationale,
        )
        if self.receipt is None:
            object.__setattr__(self, "receipt", expected_receipt)
            return
        if not isinstance(self.receipt, ReflectionEvaluationReceipt):
            raise ReflectionContractError(
                "receipt must be a ReflectionEvaluationReceipt instance when provided."
            )
        if self.receipt != expected_receipt:
            raise ReflectionContractError(
                "receipt must match the anchor identity, assessed horizons, and review decision."
            )


@dataclass(frozen=True, slots=True)
class TargetReflectionEvaluationResult:
    """Trader-style target reflection evaluation output for closed episodes."""

    episode_id: str
    target_key: str
    coverage: ReviewCoverage
    assessed_horizons: tuple[ReflectionHorizonAssessment, ...]
    thesis_assessment: ReflectionThesisAssessment
    watchlist_assessment: ReflectionWatchlistAssessment
    trade_assessment: ReflectionTradeAssessment | None
    review_decision: ReflectionReviewDecision
    decision_rationale: str
    learning_decision: ReflectionLearningDecision
    usage_quality_label: MarketContextUsageQualityLabel | None = None
    usage_quality_summary: str | None = None
    market_context_error_labels: tuple[MarketContextUsageQualityLabel, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _normalize_episode_id(self.episode_id))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=ReflectionContractError,
            ),
        )
        if not isinstance(self.coverage, ReviewCoverage):
            raise ReflectionContractError(
                "coverage must be a ReviewCoverage instance."
            )
        object.__setattr__(
            self,
            "assessed_horizons",
            _normalize_horizon_assessments(self.assessed_horizons),
        )
        if not isinstance(self.thesis_assessment, ReflectionThesisAssessment):
            raise ReflectionContractError(
                "thesis_assessment must be a ReflectionThesisAssessment instance."
            )
        if not isinstance(self.watchlist_assessment, ReflectionWatchlistAssessment):
            raise ReflectionContractError(
                "watchlist_assessment must be a ReflectionWatchlistAssessment instance."
            )
        if self.trade_assessment is not None and not isinstance(
            self.trade_assessment,
            ReflectionTradeAssessment,
        ):
            raise ReflectionContractError(
                "trade_assessment must be a ReflectionTradeAssessment instance when provided."
            )
        object.__setattr__(
            self,
            "review_decision",
            _normalize_review_decision(self.review_decision),
        )
        object.__setattr__(
            self,
            "decision_rationale",
            normalize_content(
                self.decision_rationale,
                field_name="decision_rationale",
                error_type=ReflectionContractError,
            ),
        )
        if not isinstance(self.learning_decision, ReflectionLearningDecision):
            raise ReflectionContractError(
                "learning_decision must be a ReflectionLearningDecision instance."
            )
        self.learning_decision.validate_for_review_decision(self.review_decision)
        _normalize_market_context_usage_quality(owner=self)

        assessed_horizons_hours = tuple(
            assessment.horizon_hours for assessment in self.assessed_horizons
        )
        if not set(assessed_horizons_hours).issubset(self.coverage.horizons_hours):
            raise ReflectionContractError(
                "assessed_horizons must stay within coverage.horizons_hours."
            )


@dataclass(frozen=True, slots=True)
class TargetCloseReflectionEvaluationResult:
    """Trader-style immediate review output for a just-closed target episode."""

    episode_id: str
    target_key: str
    thesis_assessment: ReflectionThesisAssessment
    watchlist_assessment: ReflectionWatchlistAssessment
    trade_assessment: ReflectionTradeAssessment
    review_decision: ReflectionReviewDecision
    decision_rationale: str
    learning_decision: ReflectionLearningDecision
    usage_quality_label: MarketContextUsageQualityLabel | None = None
    usage_quality_summary: str | None = None
    market_context_error_labels: tuple[MarketContextUsageQualityLabel, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _normalize_episode_id(self.episode_id))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=ReflectionContractError,
            ),
        )
        if not isinstance(self.thesis_assessment, ReflectionThesisAssessment):
            raise ReflectionContractError(
                "thesis_assessment must be a ReflectionThesisAssessment instance."
            )
        if not isinstance(self.watchlist_assessment, ReflectionWatchlistAssessment):
            raise ReflectionContractError(
                "watchlist_assessment must be a ReflectionWatchlistAssessment instance."
            )
        if not isinstance(self.trade_assessment, ReflectionTradeAssessment):
            raise ReflectionContractError(
                "trade_assessment must be a ReflectionTradeAssessment instance."
            )
        if self.trade_assessment.verdict == "open":
            raise ReflectionContractError(
                "target close reflection trade_assessment.verdict must not be 'open'."
            )
        object.__setattr__(
            self,
            "review_decision",
            _normalize_review_decision(self.review_decision),
        )
        object.__setattr__(
            self,
            "decision_rationale",
            normalize_content(
                self.decision_rationale,
                field_name="decision_rationale",
                error_type=ReflectionContractError,
            ),
        )
        if not isinstance(self.learning_decision, ReflectionLearningDecision):
            raise ReflectionContractError(
                "learning_decision must be a ReflectionLearningDecision instance."
            )
        self.learning_decision.validate_for_review_decision(self.review_decision)
        _normalize_market_context_usage_quality(owner=self)


@dataclass(frozen=True, slots=True)
class OpenPositionEvaluationResult:
    """Trader-style target review evaluation for a still-open terminal mark."""

    episode_id: str
    target_key: str
    thesis_assessment: ReflectionThesisAssessment
    watchlist_assessment: ReflectionWatchlistAssessment
    trade_assessment: ReflectionTradeAssessment
    exposure_assessment: ReflectionExposureAssessment
    review_decision: ReflectionReviewDecision
    decision_rationale: str
    learning_decision: ReflectionLearningDecision
    usage_quality_label: MarketContextUsageQualityLabel | None = None
    usage_quality_summary: str | None = None
    market_context_error_labels: tuple[MarketContextUsageQualityLabel, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _normalize_episode_id(self.episode_id))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=ReflectionContractError,
            ),
        )
        if not isinstance(self.thesis_assessment, ReflectionThesisAssessment):
            raise ReflectionContractError(
                "thesis_assessment must be a ReflectionThesisAssessment instance."
            )
        if not isinstance(self.watchlist_assessment, ReflectionWatchlistAssessment):
            raise ReflectionContractError(
                "watchlist_assessment must be a ReflectionWatchlistAssessment instance."
            )
        if not isinstance(self.trade_assessment, ReflectionTradeAssessment):
            raise ReflectionContractError(
                "trade_assessment must be a ReflectionTradeAssessment instance."
            )
        if self.trade_assessment.verdict != "open":
            raise ReflectionContractError(
                "open-position trade_assessment.verdict must be 'open'."
            )
        if not isinstance(self.exposure_assessment, ReflectionExposureAssessment):
            raise ReflectionContractError(
                "exposure_assessment must be a ReflectionExposureAssessment instance."
            )
        object.__setattr__(
            self,
            "review_decision",
            _normalize_review_decision(self.review_decision),
        )
        object.__setattr__(
            self,
            "decision_rationale",
            normalize_content(
                self.decision_rationale,
                field_name="decision_rationale",
                error_type=ReflectionContractError,
            ),
        )
        if not isinstance(self.learning_decision, ReflectionLearningDecision):
            raise ReflectionContractError(
                "learning_decision must be a ReflectionLearningDecision instance."
            )
        self.learning_decision.validate_for_review_decision(self.review_decision)
        _normalize_market_context_usage_quality(owner=self)


type _MarketContextUsageQualityOwner = (
    ReflectionEvaluationResult
    | TargetReflectionEvaluationResult
    | TargetCloseReflectionEvaluationResult
    | OpenPositionEvaluationResult
)


def _normalize_anchor_target_key(scope_key: str, target_key: str | None) -> str | None:
    if scope_key == "shared":
        if target_key is not None:
            raise ReflectionContractError(
                "target_key must be None when scope_key is 'shared'."
            )
        return None

    expected_target_key = scope_key.partition(":")[2]
    if target_key is None:
        return expected_target_key

    validated_target_key = validate_target_key(
        target_key,
        error_type=ReflectionContractError,
    )
    if validated_target_key != expected_target_key:
        raise ReflectionContractError(
            "target_key must match the target segment encoded in scope_key."
        )
    return validated_target_key


def _expected_log_page_path(scope_key: str, *, target_key: str | None) -> str:
    if scope_key == "shared":
        return "shared/log.md"
    if target_key is None:
        raise ReflectionContractError(
            "target_key is required for target-scoped review anchors."
        )
    return f"targets/{target_key}/log.md"


def _validate_positive_hours(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReflectionContractError(
            f"{field_name} must be a positive number of hours."
        )

    normalized = float(value)
    if normalized <= 0:
        raise ReflectionContractError(
            f"{field_name} must be greater than zero hours."
        )
    return normalized


def _normalize_horizons_hours(value: tuple[float, ...]) -> tuple[float, ...]:
    if not isinstance(value, tuple) or not value:
        raise ReflectionContractError(
            "horizons_hours must be a non-empty tuple of positive horizon hours."
        )

    normalized: list[float] = []
    seen: set[float] = set()
    for index, item in enumerate(value):
        horizon = _validate_positive_hours(
            item,
            field_name=f"horizons_hours[{index}]",
        )
        if horizon in seen:
            raise ReflectionContractError(
                "horizons_hours must not contain duplicate horizon values."
            )
        seen.add(horizon)
        normalized.append(horizon)

    return tuple(normalized)


def _normalize_missing_dependencies(value: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ReflectionContractError(
            "missing_dependencies must be a tuple of dependency names."
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for dependency in value:
        if not isinstance(dependency, str):
            raise ReflectionContractError(
                "missing_dependencies must contain only strings."
            )
        clean = dependency.strip()
        if not clean:
            raise ReflectionContractError(
                "missing_dependencies must not contain blank values."
            )
        if clean != dependency:
            raise ReflectionContractError(
                "missing_dependencies must not include leading or trailing whitespace."
            )
        if clean in seen:
            raise ReflectionContractError(
                "missing_dependencies must not contain duplicates."
            )
        seen.add(clean)
        normalized.append(clean)

    return tuple(normalized)


def _validate_context_packet_members(
    *,
    anchor: ReviewAnchorIdentity,
    coverage: ReviewCoverage,
    observed_at: datetime,
    summary_md: str,
    packet_name: str,
    owner: object,
) -> None:
    if not isinstance(anchor, ReviewAnchorIdentity):
        raise ReflectionContractError(
            f"{packet_name}.anchor must be a ReviewAnchorIdentity instance."
        )
    if not isinstance(coverage, ReviewCoverage):
        raise ReflectionContractError(
            f"{packet_name}.coverage must be a ReviewCoverage instance."
        )
    object.__setattr__(
        owner,
        "observed_at",
        validate_timestamp(
            observed_at,
            field_name="observed_at",
            error_type=ReflectionContractError,
        ),
    )
    object.__setattr__(
        owner,
        "summary_md",
        normalize_content(
            summary_md,
            field_name="summary_md",
            error_type=ReflectionContractError,
        ),
    )


def _normalize_market_context_usage_quality(
    *,
    owner: _MarketContextUsageQualityOwner,
) -> None:
    label = _normalize_usage_quality_label(owner.usage_quality_label)
    object.__setattr__(owner, "usage_quality_label", label)
    object.__setattr__(
        owner,
        "usage_quality_summary",
        _normalize_usage_quality_summary(
            owner.usage_quality_summary,
            label=label,
        ),
    )
    object.__setattr__(
        owner,
        "market_context_error_labels",
        _normalize_usage_quality_error_labels(owner.market_context_error_labels),
    )


def _normalize_usage_quality_label(
    value: object,
) -> MarketContextUsageQualityLabel | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReflectionContractError("usage_quality_label must be a string or None.")
    normalized = value.strip()
    if normalized != value:
        raise ReflectionContractError(
            "usage_quality_label must not include leading or trailing whitespace."
        )
    for allowed_label in MARKET_CONTEXT_USAGE_QUALITY_LABELS:
        if normalized == allowed_label:
            return allowed_label
    raise ReflectionContractError(
        "usage_quality_label must be one of: "
        f"{', '.join(MARKET_CONTEXT_USAGE_QUALITY_LABELS)}."
    )


def _normalize_usage_quality_summary(
    value: object,
    *,
    label: MarketContextUsageQualityLabel | None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReflectionContractError("usage_quality_summary must be a string or None.")
    normalized = value.strip()
    if not normalized and label not in {None, "not_applicable"}:
        raise ReflectionContractError(
            "usage_quality_summary may be empty only when "
            "usage_quality_label is absent or 'not_applicable'."
        )
    return normalized


def _normalize_usage_quality_error_labels(
    value: object,
) -> tuple[MarketContextUsageQualityLabel, ...]:
    if not isinstance(value, tuple):
        raise ReflectionContractError(
            "market_context_error_labels must be a tuple of strings."
        )
    normalized: list[MarketContextUsageQualityLabel] = []
    seen: set[MarketContextUsageQualityLabel] = set()
    for item in value:
        label = _normalize_usage_quality_label(item)
        if label is None:
            raise ReflectionContractError(
                "market_context_error_labels must not contain None."
            )
        if label in seen:
            raise ReflectionContractError(
                "market_context_error_labels must not contain duplicate labels."
            )
        seen.add(label)
        normalized.append(label)
    return tuple(normalized)


def _normalize_path_verdict(value: ReflectionPathVerdict) -> ReflectionPathVerdict:
    allowed: tuple[ReflectionPathVerdict, ...] = (
        "favorable",
        "mixed",
        "adverse",
        "unclear",
    )
    if value not in allowed:
        raise ReflectionContractError(
            "path_verdict must be one of favorable, mixed, adverse, or unclear."
        )
    return value


def _normalize_tradability_verdict(
    value: ReflectionTradabilityVerdict,
) -> ReflectionTradabilityVerdict:
    allowed: tuple[ReflectionTradabilityVerdict, ...] = (
        "tradable",
        "mixed",
        "poor",
        "unclear",
    )
    if value not in allowed:
        raise ReflectionContractError(
            "tradability_verdict must be one of tradable, mixed, poor, or unclear."
        )
    return value


def _normalize_thesis_verdict(
    value: ReflectionThesisVerdict,
) -> ReflectionThesisVerdict:
    allowed: tuple[ReflectionThesisVerdict, ...] = (
        "validated",
        "mixed",
        "invalidated",
        "unclear",
    )
    if value not in allowed:
        raise ReflectionContractError(
            "thesis verdict must be one of validated, mixed, invalidated, or unclear."
        )
    return value


def _normalize_trade_verdict(value: ReflectionTradeVerdict) -> ReflectionTradeVerdict:
    allowed: tuple[ReflectionTradeVerdict, ...] = (
        "win",
        "loss",
        "scratch",
        "missed",
        "not_taken",
        "open",
    )
    if value not in allowed:
        raise ReflectionContractError(
            "trade verdict must be one of win, loss, scratch, missed, not_taken, or open."
        )
    return value


def _normalize_watchlist_verdict(
    value: ReflectionWatchlistVerdict,
) -> ReflectionWatchlistVerdict:
    allowed: tuple[ReflectionWatchlistVerdict, ...] = (
        "useful",
        "mixed",
        "stale",
        "missing",
        "unclear",
    )
    if value not in allowed:
        raise ReflectionContractError(
            "watchlist verdict must be one of useful, mixed, stale, missing, or unclear."
        )
    return value


def _normalize_review_decision(
    value: ReflectionReviewDecision,
) -> ReflectionReviewDecision:
    if value not in {"write_review", "skip_review"}:
        raise ReflectionContractError(
            "review_decision must be either 'write_review' or 'skip_review'."
        )
    return value


def _normalize_horizon_assessments(
    value: tuple[ReflectionHorizonAssessment, ...],
) -> tuple[ReflectionHorizonAssessment, ...]:
    if not isinstance(value, tuple) or not value:
        raise ReflectionContractError(
            "assessed_horizons must be a non-empty tuple of ReflectionHorizonAssessment values."
        )

    normalized: list[ReflectionHorizonAssessment] = []
    seen: set[float] = set()
    for assessment in value:
        if not isinstance(assessment, ReflectionHorizonAssessment):
            raise ReflectionContractError(
                "assessed_horizons must contain only ReflectionHorizonAssessment instances."
            )
        if assessment.horizon_hours in seen:
            raise ReflectionContractError(
                "assessed_horizons must not contain duplicate horizon_hours values."
            )
        seen.add(assessment.horizon_hours)
        normalized.append(assessment)

    return tuple(normalized)


def _validate_finite_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReflectionContractError(f"{field_name} must be a finite number.")

    normalized = float(value)
    if not isfinite(normalized):
        raise ReflectionContractError(f"{field_name} must be a finite number.")
    return normalized


def _normalize_anchor_id(value: str) -> str:
    if not isinstance(value, str):
        raise ReflectionContractError("anchor_id must be a string.")

    anchor_id = value.strip()
    if not anchor_id:
        raise ReflectionContractError("anchor_id must not be blank.")
    if anchor_id != value:
        raise ReflectionContractError(
            "anchor_id must not include leading or trailing whitespace."
        )
    log_page_path, separator, entry_key = anchor_id.partition("#")
    if separator != "#" or not log_page_path or not entry_key:
        raise ReflectionContractError(
            "anchor_id must use the canonical '<log_page_path>#<entry_key>' format."
        )
    resolve_reflection_anchor_page_ref(log_page_path)
    validate_page_key(
        entry_key,
        field_name="anchor_id entry_key",
        error_type=ReflectionContractError,
    )
    return anchor_id


def _normalize_episode_id(value: str) -> str:
    if not isinstance(value, str):
        raise ReflectionContractError("episode_id must be a string.")
    episode_id = value.strip()
    if not episode_id:
        raise ReflectionContractError("episode_id must not be blank.")
    if episode_id != value:
        raise ReflectionContractError(
            "episode_id must not include leading or trailing whitespace."
        )
    return episode_id


def _anchor_id_for_identity(anchor: ReviewAnchorIdentity) -> str:
    return f"{anchor.log_page_path}#{anchor.entry_key}"


__all__ = [
    "MARKET_CONTEXT_USAGE_QUALITY_LABELS",
    "MarketContextUsageQualityLabel",
    "OutcomeContextPacket",
    "ReflectionContextPacket",
    "ReflectionContractError",
    "ReflectionExposureAssessment",
    "ReflectionRunReceipt",
    "ReflectionWatchlistAssessment",
    "ReviewAnchorIdentity",
    "ReviewCoverage",
    "TargetCloseReflectionEvaluationResult",
    "OpenPositionEvaluationResult",
    "TradeContextPacket",
]


