"""Heartbeat-driven reflection orchestration outside the checker path.

This module keeps reflection on the top-level kernel heartbeat while preserving
the event-driven evidence/checker/analysis path. It scans due target-horizon
obligations on every heartbeat, and on cadence-due heartbeats it also records
cadence review audit metadata without widening the primary runtime path.

For target reflection, the primary runtime read surface is the persisted
episode artifact under ``runtime/validation/episodes/...``. Canonical
state-changes remain the authoritative truth and refresh those artifacts before
reflection reads them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal

from event_trader.analysis import (
    AnalysisContextError,
    FileBackedResearchMemoryReader,
)
from event_trader.config import KernelConfig
from event_trader.contracts import PageReadResult, PageRef, WikiMatch
from event_trader.contracts.ports import MarketDataPort, OutcomeContextPort, TradeContextPort
from event_trader.contracts.view_state_change import (
    MarketMapping,
    ReflectionTrigger,
    ViewEpisode,
    ViewStateChange,
)
from event_trader.counterfactuals import CounterfactualReportWriter
from event_trader.decision_memory import (
    FileBackedDecisionEpisodeStore,
    project_decision_episodes,
)
from event_trader.episode_memory import FileBackedEpisodeMemoryStore
from event_trader.evidence_ledger import FileBackedEvidenceLedger, read_evidence_window
from event_trader.execution import ExecutionRecord, ExecutionRecordStore
from event_trader.market.adjustments import MarketDataAdjustmentPolicy
from event_trader.market.target_series import read_realized_target_series
from event_trader.research_memory import FileBackedIndexLogWriter
from event_trader.storage import WorkspaceLayout
from event_trader.validation import read_state_changes
from event_trader.validation.episode_runtime import (
    load_closed_episode_snapshot,
    load_target_episode_state,
)
from event_trader.validation.execution_linkage import (
    execution_lookup_ids_for_state_changes,
)
from event_trader.validation.returns import calculate_open_episode_terminal_mark

from .anchors import (
    SharedAnchorWriteReceipt,
    parse_reflection_log_entries,
    write_shared_follow_on_anchor,
)
from .cadence_orchestration import (
    CadencePatternReviewReceipt,
    run_cadence_pattern_review,
)
from .cadence_reviews import CadenceReviewWindowKind
from .context import ReflectionLedgerContext, ReflectionLedgerContextLoader
from .contracts import (
    OpenPositionEvaluationResult,
    OutcomeContextPacket,
    ReflectionEvaluationResult,
    ReflectionRunReceipt,
    ReviewCoverage,
    TargetCloseReflectionEvaluationResult,
    TargetReflectionEvaluationResult,
)
from .eligibility import (
    ReflectionEligibilityResult,
    evaluate_reflection_eligibility,
)
from .entry import (
    ReflectionDueCheck,
    ReflectionEmitter,
    ReflectionEntryReceipt,
    run_reflection_once,
)
from .episode_context import (
    CloseEpisodeReflectionContextLoader,
    EpisodeReflectionContextLoader,
)
from .feedback_context import (
    ReflectionFeedbackContextError,
    build_reflection_feedback_context_facts,
)
from .learning_contracts import ReflectionLearningReceipt
from .learning_lifecycle import apply_reflection_learning
from .market_context_usage import (
    read_analysis_outcome_receipts,
    read_checker_decision_receipts,
)
from .review_coverage_index import (
    ReviewCoverageIndexRecord,
    read_review_coverage_index,
)
from .review_writer import (
    TargetCloseReviewWriteReceipt,
    TargetReviewWriteReceipt,
    write_open_position_horizon_review,
    write_open_position_material_update_review,
    write_target_close_review,
    write_target_review,
)
from .trigger_policy import ReflectionObligationResolution
from .trigger_store import (
    FileBackedReflectionObligationStore,
    PersistedReflectionObligation,
)
from .view_contracts import (
    CloseEpisodeReflectionContext,
    EpisodeReflectionContext,
    OpenPositionReflectionContext,
)

type ReflectionHeartbeatStatus = Literal[
    "not_due",
    "no_eligible_candidates",
    "dependency_missing",
    "review_written",
    "review_skipped",
    "shared_anchor_written",
    "failed",
]
type ReflectionFailureStage = Literal[
    "due_check",
    "eligibility",
    "context_loading",
    "evaluation",
    "review_write",
    "learning_write",
    "shared_anchor_write",
    "cadence_review",
]
type ReflectionCandidateStatus = Literal[
    "dependency_missing",
    "review_written",
    "review_skipped",
    "shared_anchor_written",
    "failed",
]
type SharedLogAppendDecision = Literal["append_shared_anchor", "skip_shared_anchor"]
type ReflectionNow = Callable[[], datetime]

_RESUME_SKIP_REASON_MARKERS = (
    "already covered by the canonical review artifact",
    "already covered by the open-position horizon review artifact",
    "already covered by prior reflection outputs",
    "already records reflection coverage",
)
type ReflectionReviewEvaluator = Callable[[ReflectionLedgerContext], ReflectionEvaluationResult]
type TargetReflectionReviewEvaluator = Callable[
    [EpisodeReflectionContext],
    TargetReflectionEvaluationResult,
]
type TargetCloseReflectionReviewEvaluator = Callable[
    [CloseEpisodeReflectionContext],
    TargetCloseReflectionEvaluationResult,
]
type TargetOpenPositionHorizonReflectionEvaluator = Callable[
    [OpenPositionReflectionContext],
    OpenPositionEvaluationResult,
]
type MarketMappingResolver = Callable[[str], MarketMapping]

_TARGET_REVIEW_WRITE_BLOCKED_REASON = (
    "Target reflection review writing requires the injected market seam "
    "(market_data + resolve_market_mapping)."
)
_CADENCE_WINDOW_KINDS: tuple[CadenceReviewWindowKind, ...] = ("weekly", "monthly")
_CADENCE_MIN_REQUIRED_REVIEWS = 2
@dataclass(frozen=True, slots=True)
class _TargetHorizonObligation:
    episode_id: str
    target_key: str
    opened_at: datetime
    closed_at: datetime
    horizon_hours: float
    covered_horizons_hours: tuple[float, ...]
    is_open: bool = False
    exposure_anchor_at: datetime | None = None
    reflection_obligation_id: str | None = None

    @property
    def candidate_id(self) -> str:
        return f"{self.episode_id}#horizon={self.horizon_hours:g}"

    @property
    def due_at(self) -> datetime:
        anchor = self.exposure_anchor_at or self.opened_at
        return anchor + timedelta(hours=self.horizon_hours)

    @property
    def reviewable_horizons_hours(self) -> tuple[float, ...]:
        return (self.horizon_hours,)

    def __post_init__(self) -> None:
        if self.closed_at <= self.opened_at:
            raise ReflectionReviewLoopError(
                "Target horizon obligations must satisfy closed_at > opened_at."
            )
        if self.horizon_hours <= 0:
            raise ReflectionReviewLoopError(
                "Target horizon obligations require horizon_hours > 0."
            )
        if self.horizon_hours in self.covered_horizons_hours:
            raise ReflectionReviewLoopError(
                "Target horizon obligations must represent an uncovered horizon."
            )
        if self.exposure_anchor_at is not None and self.exposure_anchor_at < self.opened_at:
            raise ReflectionReviewLoopError(
                "Target horizon exposure_anchor_at must be >= opened_at when provided."
            )


@dataclass(frozen=True, slots=True)
class _TargetCloseObligation:
    episode_id: str
    target_key: str
    opened_at: datetime
    closed_at: datetime
    triggered_at: datetime
    reflection_obligation_id: str | None = None

    @property
    def candidate_id(self) -> str:
        return f"{self.episode_id}#close"

    @property
    def due_at(self) -> datetime:
        return self.triggered_at

    @property
    def reviewable_horizons_hours(self) -> tuple[float, ...]:
        return ()

    def __post_init__(self) -> None:
        if self.closed_at <= self.opened_at:
            raise ReflectionReviewLoopError(
                "Target close obligations must satisfy closed_at > opened_at."
            )
        if self.triggered_at < self.closed_at:
            raise ReflectionReviewLoopError(
                "Target close obligations require triggered_at >= closed_at."
            )


@dataclass(frozen=True, slots=True)
class _TargetOpenMaterialUpdateObligation:
    episode_id: str
    target_key: str
    opened_at: datetime
    closed_at: datetime
    previous_open_memory_update_at: datetime
    material_update_sequence: int
    exposure_anchor_at: datetime
    triggered_at: datetime
    reflection_obligation_id: str

    @property
    def candidate_id(self) -> str:
        return (
            f"{self.episode_id}#open_position_material_update="
            f"{self.material_update_sequence}"
        )

    @property
    def due_at(self) -> datetime:
        return self.triggered_at

    @property
    def reviewable_horizons_hours(self) -> tuple[float, ...]:
        return ()

    def __post_init__(self) -> None:
        if self.closed_at <= self.opened_at:
            raise ReflectionReviewLoopError(
                "Target open material-update obligations must satisfy "
                "closed_at > opened_at."
            )
        if self.previous_open_memory_update_at <= self.opened_at:
            raise ReflectionReviewLoopError(
                "Target open material-update obligations require a prior open "
                "memory update."
            )
        if self.previous_open_memory_update_at >= self.closed_at:
            raise ReflectionReviewLoopError(
                "Target open material-update prior memory update must be before "
                "closed_at."
            )
        if self.material_update_sequence <= 0:
            raise ReflectionReviewLoopError(
                "Target open material-update obligations require sequence > 0."
            )
        if self.triggered_at < self.previous_open_memory_update_at:
            raise ReflectionReviewLoopError(
                "Target open material-update obligations require triggered_at >= "
                "prior memory update."
            )
        if not self.reflection_obligation_id.strip():
            raise ReflectionReviewLoopError(
                "Target open material-update obligations require a "
                "reflection_obligation_id."
            )


@dataclass(frozen=True, slots=True)
class _OpenMaterialUpdateHistory:
    last_memory_update_at: datetime
    material_update_count: int


type _TargetObligation = (
    _TargetHorizonObligation
    | _TargetCloseObligation
    | _TargetOpenMaterialUpdateObligation
)


class ReflectionReviewLoopError(RuntimeError):
    """Raised when the heartbeat reflection loop cannot normalize its inputs."""


class _TargetHorizonNotReady(ReflectionReviewLoopError):
    """Raised when an open target horizon should be retried by a future heartbeat."""


@dataclass(frozen=True, slots=True)
class ReflectionCandidateReceipt:
    """Observable result of one processed reflection candidate."""

    candidate_id: str
    status: ReflectionCandidateStatus
    source_log_path: str | None = None
    covered_horizons_hours: tuple[float, ...] = ()
    missing_dependencies: tuple[str, ...] = ()
    review_artifact_path: Path | None = None
    learning_receipt: ReflectionLearningReceipt | None = None
    shared_anchor_id: str | None = None
    shared_anchor_artifact_path: Path | None = None
    shared_log_append_decision: SharedLogAppendDecision | None = None
    failure_stage: ReflectionFailureStage | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ReflectionReviewLoopError("candidate_id must be a non-blank string.")
        if self.status not in {
            "dependency_missing",
            "review_written",
            "review_skipped",
            "shared_anchor_written",
            "failed",
        }:
            raise ReflectionReviewLoopError(
                "candidate status must be one of the committed reflection states."
            )
        if self.source_log_path is not None:
            if not isinstance(self.source_log_path, str) or not self.source_log_path.strip():
                raise ReflectionReviewLoopError(
                    "source_log_path must be a non-blank string when provided."
                )
            if not self.candidate_id.startswith(f"{self.source_log_path}#"):
                raise ReflectionReviewLoopError(
                    "source_log_path must match the candidate surface when provided."
                )
        if self.status == "dependency_missing" and not self.missing_dependencies:
            raise ReflectionReviewLoopError(
                "candidate status='dependency_missing' requires missing_dependencies."
            )
        if self.status in {"dependency_missing", "failed"} and self.failure_stage is None:
            raise ReflectionReviewLoopError(
                "dependency_missing and failed candidate receipts require failure_stage."
            )
        if self.failure_stage is not None and self.failure_stage not in {
            "due_check",
            "eligibility",
            "context_loading",
            "evaluation",
            "review_write",
            "learning_write",
            "shared_anchor_write",
            "cadence_review",
        }:
            raise ReflectionReviewLoopError(
                "failure_stage must be one of the committed reflection failure stages."
            )
        if self.status == "failed" and not self.failure_reason:
            raise ReflectionReviewLoopError(
                "candidate status='failed' requires failure_reason."
            )
        if self.review_artifact_path is not None and not isinstance(
            self.review_artifact_path, Path
        ):
            raise ReflectionReviewLoopError(
                "review_artifact_path must be a pathlib.Path when provided."
            )
        if self.learning_receipt is not None and not isinstance(
            self.learning_receipt, ReflectionLearningReceipt
        ):
            raise ReflectionReviewLoopError(
                "learning_receipt must be a ReflectionLearningReceipt when provided."
            )
        if self.status == "review_written" and self.review_artifact_path is None:
            raise ReflectionReviewLoopError(
                "candidate status='review_written' requires review_artifact_path."
            )
        if self.shared_anchor_artifact_path is not None and not isinstance(
            self.shared_anchor_artifact_path, Path
        ):
            raise ReflectionReviewLoopError(
                "shared_anchor_artifact_path must be a pathlib.Path when provided."
            )
        if (
            self.shared_log_append_decision is not None
            and self.shared_log_append_decision
            not in {"append_shared_anchor", "skip_shared_anchor"}
        ):
            raise ReflectionReviewLoopError(
                "shared_log_append_decision must be 'append_shared_anchor' or "
                "'skip_shared_anchor' when provided."
            )


@dataclass(frozen=True, slots=True)
class ReflectionHeartbeatReceipt:
    """Observable result of one heartbeat-driven reflection attempt."""

    heartbeat_number: int
    checked_at: datetime
    status: ReflectionHeartbeatStatus
    due_check: ReflectionDueCheck
    run_receipt: ReflectionRunReceipt
    eligible_candidate_ids: tuple[str, ...] = ()
    processed_candidate_receipts: tuple[ReflectionCandidateReceipt, ...] = ()
    cadence_review_receipts: tuple[CadencePatternReviewReceipt, ...] = ()
    missing_dependencies: tuple[str, ...] = ()
    failure_stage: ReflectionFailureStage | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.heartbeat_number, int) or self.heartbeat_number < 1:
            raise ReflectionReviewLoopError("heartbeat_number must be >= 1.")
        if not isinstance(self.checked_at, datetime):
            raise ReflectionReviewLoopError("checked_at must be a datetime.")
        if self.status not in {
            "not_due",
            "no_eligible_candidates",
            "dependency_missing",
            "review_written",
            "review_skipped",
            "shared_anchor_written",
            "failed",
        }:
            raise ReflectionReviewLoopError(
                "status must be one of the committed heartbeat reflection states."
            )
        if not isinstance(self.due_check, ReflectionDueCheck):
            raise ReflectionReviewLoopError(
                "due_check must be a ReflectionDueCheck instance."
            )
        if not isinstance(self.run_receipt, ReflectionRunReceipt):
            raise ReflectionReviewLoopError(
                "run_receipt must be a ReflectionRunReceipt instance."
            )
        if self.status == "not_due" and self.due_check.due:
            raise ReflectionReviewLoopError(
                "status='not_due' requires due_check.due to be False."
            )
        if self.status not in {"not_due", "failed"} and not self.due_check.due:
            raise ReflectionReviewLoopError(
                "due statuses require due_check.due to be True."
            )
        eligible_candidate_ids = set(self.eligible_candidate_ids)
        if not isinstance(self.processed_candidate_receipts, tuple):
            raise ReflectionReviewLoopError(
                "processed_candidate_receipts must be a tuple of "
                "ReflectionCandidateReceipt instances."
            )
        for candidate_receipt in self.processed_candidate_receipts:
            if not isinstance(candidate_receipt, ReflectionCandidateReceipt):
                raise ReflectionReviewLoopError(
                    "processed_candidate_receipts must contain only "
                    "ReflectionCandidateReceipt instances."
                )
            if candidate_receipt.candidate_id not in eligible_candidate_ids:
                raise ReflectionReviewLoopError(
                    "processed candidate receipts must come from eligible_candidate_ids."
                )
        if (
            self.status == "dependency_missing"
            and not self.missing_dependencies
            and not any(
                item.status == "dependency_missing"
                for item in self.processed_candidate_receipts
            )
        ):
            raise ReflectionReviewLoopError(
                "status='dependency_missing' requires dependency details."
            )
        if self.status in {"dependency_missing", "failed"} and self.failure_stage is None:
            raise ReflectionReviewLoopError(
                "dependency_missing and failed receipts require failure_stage."
            )
        if not isinstance(self.cadence_review_receipts, tuple):
            raise ReflectionReviewLoopError(
                "cadence_review_receipts must be a tuple of CadencePatternReviewReceipt "
                "instances."
            )
        for cadence_receipt in self.cadence_review_receipts:
            if not isinstance(cadence_receipt, CadencePatternReviewReceipt):
                raise ReflectionReviewLoopError(
                    "cadence_review_receipts must contain only "
                    "CadencePatternReviewReceipt instances."
                )
        if self.failure_stage is not None and self.failure_stage not in {
            "due_check",
            "eligibility",
            "context_loading",
            "evaluation",
            "review_write",
            "learning_write",
            "shared_anchor_write",
            "cadence_review",
        }:
            raise ReflectionReviewLoopError(
                "failure_stage must be one of the committed reflection failure stages."
            )
        if self.status == "failed" and not self.failure_reason:
            raise ReflectionReviewLoopError(
                "status='failed' requires failure_reason."
            )


class ReflectionResearchMemoryReader:
    """Read canonical research-memory pages for reflection without write seams."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise ReflectionReviewLoopError("layout must be a WorkspaceLayout instance.")
        self._reader = FileBackedResearchMemoryReader(layout)

    def read_page(self, page_path: str) -> PageReadResult:
        try:
            return self._reader.read_page(page_path)
        except AnalysisContextError as exc:
            raise ReflectionReviewLoopError(str(exc)) from exc

    def list_pages(self, scope: str) -> list[PageRef]:
        try:
            return self._reader.list_pages(scope)
        except AnalysisContextError as exc:
            raise ReflectionReviewLoopError(str(exc)) from exc

    def search_wiki(self, query: str, scope: str) -> list[WikiMatch]:
        try:
            return self._reader.search_wiki(query, scope)
        except AnalysisContextError as exc:
            raise ReflectionReviewLoopError(str(exc)) from exc


class ReflectionReviewLoop:
    """Run one reflection review attempt on each top-level kernel heartbeat."""

    def __init__(
        self,
        *,
        config: KernelConfig,
        layout: WorkspaceLayout,
        ledger: FileBackedEvidenceLedger,
        outcome_context: OutcomeContextPort | None = None,
        trade_context: TradeContextPort | None = None,
        evaluate_review: ReflectionReviewEvaluator | None = None,
        evaluate_target_review: TargetReflectionReviewEvaluator | None = None,
        evaluate_target_open_position_review: TargetOpenPositionHorizonReflectionEvaluator
        | None = None,
        evaluate_target_close_review: TargetCloseReflectionReviewEvaluator | None = None,
        market_data: MarketDataPort | None = None,
        resolve_market_mapping: MarketMappingResolver | None = None,
        emit: ReflectionEmitter | None = None,
        now: ReflectionNow | None = None,
    ) -> None:
        if not isinstance(config, KernelConfig):
            raise ReflectionReviewLoopError("config must be a KernelConfig instance.")
        if not isinstance(layout, WorkspaceLayout):
            raise ReflectionReviewLoopError("layout must be a WorkspaceLayout instance.")
        if not isinstance(ledger, FileBackedEvidenceLedger):
            raise ReflectionReviewLoopError(
                "ledger must be a FileBackedEvidenceLedger instance."
            )
        if evaluate_review is not None and not callable(evaluate_review):
            raise ReflectionReviewLoopError(
                "evaluate_review must be callable when provided."
            )
        if evaluate_target_review is not None and not callable(evaluate_target_review):
            raise ReflectionReviewLoopError(
                "evaluate_target_review must be callable when provided."
            )
        if (
            evaluate_target_open_position_review is not None
            and not callable(evaluate_target_open_position_review)
        ):
            raise ReflectionReviewLoopError(
                "evaluate_target_open_position_review must be callable when provided."
            )
        if evaluate_target_close_review is not None and not callable(
            evaluate_target_close_review
        ):
            raise ReflectionReviewLoopError(
                "evaluate_target_close_review must be callable when provided."
            )
        if market_data is not None and not callable(getattr(market_data, "read_series", None)):
            raise ReflectionReviewLoopError(
                "market_data must implement MarketDataPort.read_series when provided."
            )
        if resolve_market_mapping is not None and not callable(resolve_market_mapping):
            raise ReflectionReviewLoopError(
                "resolve_market_mapping must be callable when provided."
            )
        if (market_data is None) != (resolve_market_mapping is None):
            raise ReflectionReviewLoopError(
                "market_data and resolve_market_mapping must be provided together."
            )
        if now is not None and not callable(now):
            raise ReflectionReviewLoopError("now must be callable when provided.")
        if emit is not None and not callable(emit):
            raise ReflectionReviewLoopError("emit must be callable when provided.")

        self._config = config
        self._layout = layout
        self._ledger = ledger
        self._outcome_context = outcome_context
        self._trade_context = trade_context
        self._evaluate_review = evaluate_review
        self._evaluate_target_review = evaluate_target_review
        self._evaluate_target_open_position_review = evaluate_target_open_position_review
        self._evaluate_target_close_review = evaluate_target_close_review
        self._market_data = market_data
        self._resolve_market_mapping = resolve_market_mapping
        self._emit = emit
        self._now = now or _utc_now
        self._research_memory = ReflectionResearchMemoryReader(layout)
        self._memory_wrapper = FileBackedIndexLogWriter(layout)
        self._obligation_store = FileBackedReflectionObligationStore(layout)
        self._last_completed_at: datetime | None = None
        self._last_receipt: ReflectionHeartbeatReceipt | None = None

    @property
    def last_completed_at(self) -> datetime | None:
        """Return when the most recent successful reflection heartbeat completed."""
        return self._last_completed_at

    @property
    def last_receipt(self) -> ReflectionHeartbeatReceipt | None:
        """Return the most recent reflection heartbeat receipt, if any."""
        return self._last_receipt

    def on_heartbeat(self, heartbeat_number: int) -> None:
        """Run one heartbeat-driven reflection step and retain the receipt."""
        _ = self.run_and_record_heartbeat(heartbeat_number)

    def run_and_record_heartbeat(
        self,
        heartbeat_number: int,
    ) -> ReflectionHeartbeatReceipt:
        """Run one heartbeat and retain the emitted receipt."""
        receipt = self.run_heartbeat(heartbeat_number)
        self._last_receipt = receipt
        self._emit_summary(receipt)
        return receipt

    def run_heartbeat(self, heartbeat_number: int) -> ReflectionHeartbeatReceipt:
        """Return the structured reflection result for one kernel heartbeat."""
        return self.run_heartbeat_at(
            heartbeat_number=heartbeat_number,
            checked_at=self._now(),
        )

    def run_heartbeat_at(
        self,
        *,
        heartbeat_number: int,
        checked_at: datetime,
    ) -> ReflectionHeartbeatReceipt:
        """Return the structured reflection result for one explicit business time."""
        cadence_probe = run_reflection_once(
            self._config,
            checked_at=checked_at,
            last_completed_at=self._last_completed_at,
            eligible_anchor_count=0,
            outcome_context=_NoopOutcomeContextPort(),
            trade_context=self._trade_context,
            emit=self._emit,
        )
        cadence_due = cadence_probe.status != "not_due"

        try:
            target_obligations = self._target_obligations(checked_at)
            eligible_results = self._eligible_results(checked_at) if cadence_due else ()
        except Exception as exc:
            return ReflectionHeartbeatReceipt(
                heartbeat_number=heartbeat_number,
                checked_at=checked_at,
                status="failed",
                due_check=cadence_probe.due_check,
                run_receipt=cadence_probe.run_receipt,
                failure_stage="eligibility",
                failure_reason=str(exc),
            )

        if not cadence_due and not target_obligations:
            return ReflectionHeartbeatReceipt(
                heartbeat_number=heartbeat_number,
                checked_at=checked_at,
                status="not_due",
                due_check=cadence_probe.due_check,
                run_receipt=cadence_probe.run_receipt,
            )

        eligible_candidate_ids = (
            *(obligation.candidate_id for obligation in target_obligations),
            *(result.anchor_id for result in eligible_results),
        )
        if not eligible_candidate_ids:
            receipt = self._with_cadence_reviews(
                ReflectionHeartbeatReceipt(
                    heartbeat_number=heartbeat_number,
                    checked_at=checked_at,
                    status="no_eligible_candidates",
                    due_check=cadence_probe.due_check,
                    run_receipt=cadence_probe.run_receipt,
                ),
                cadence_due=cadence_due,
            )
            self._record_cadence_completion(receipt, cadence_due=cadence_due)
            return receipt

        entry_receipt = run_reflection_once(
            self._config,
            checked_at=checked_at,
            last_completed_at=self._last_completed_at,
            force_due=not cadence_due,
            eligible_anchor_count=len(eligible_candidate_ids),
            outcome_context=_NoopOutcomeContextPort(),
            trade_context=self._trade_context,
            emit=None,
        )
        if entry_receipt.status == "dependency_missing":
            receipt = self._with_cadence_reviews(
                ReflectionHeartbeatReceipt(
                    heartbeat_number=heartbeat_number,
                    checked_at=checked_at,
                    status="dependency_missing",
                    due_check=entry_receipt.due_check,
                    run_receipt=entry_receipt.run_receipt,
                    eligible_candidate_ids=eligible_candidate_ids,
                    missing_dependencies=entry_receipt.run_receipt.missing_dependencies,
                    failure_stage="due_check",
                    failure_reason=entry_receipt.run_receipt.failure_reason,
                ),
                cadence_due=cadence_due,
            )
            self._record_cadence_completion(receipt, cadence_due=cadence_due)
            return receipt

        processed_candidate_receipts: list[ReflectionCandidateReceipt] = []
        for obligation in target_obligations:
            candidate_receipt = self._process_target_obligation(obligation)
            processed_candidate_receipts.append(candidate_receipt)
            if candidate_receipt.status in {"dependency_missing", "failed"}:
                break
        else:
            for result in eligible_results:
                candidate_receipt = self._process_shared_candidate(result)
                processed_candidate_receipts.append(candidate_receipt)
                if candidate_receipt.status in {"dependency_missing", "failed"}:
                    break

        receipt = self._with_cadence_reviews(
            self._heartbeat_receipt(
                heartbeat_number=heartbeat_number,
                checked_at=checked_at,
                entry_receipt=entry_receipt,
                eligible_candidate_ids=eligible_candidate_ids,
                processed_candidate_receipts=tuple(processed_candidate_receipts),
            ),
            cadence_due=cadence_due,
        )
        self._record_cadence_completion(receipt, cadence_due=cadence_due)
        return receipt

    def _eligible_results(
        self,
        checked_at: datetime,
    ) -> tuple[ReflectionEligibilityResult, ...]:
        page = self._research_memory.read_page("shared/log.md")
        entries = parse_reflection_log_entries(
            log_page_path=page.page_path,
            content_md=page.content_md,
        )
        if not entries:
            return ()
        return tuple(
            result
            for result in evaluate_reflection_eligibility(
                log_entries=entries,
                checked_at=checked_at,
                horizons_hours=self._config.reflection_horizons_hours,
                coverage_pages=(page,),
            )
            if result.status in {"eligible_new", "eligible_revisit"}
        )

    def _target_obligations(
        self,
        checked_at: datetime,
    ) -> tuple[_TargetObligation, ...]:
        coverage_records = read_review_coverage_index(self._layout)
        coverage_by_episode = self._target_coverage_by_episode_id(coverage_records)
        close_covered_episode_ids = self._target_close_covered_episode_ids(
            coverage_records
        )
        open_review_history_by_episode = self._target_open_review_history_by_episode_id(
            coverage_records
        )
        candidates: list[_TargetObligation] = []
        for target_key in self._target_keys():
            due_material_by_episode = self._due_material_obligations_by_episode(
                target_key=target_key,
                checked_at=checked_at,
            )
            # Target selection reads persisted episode artifacts as the primary
            # inspect surface; those artifacts are refreshed from canonical
            # state-changes during append.
            episode_state = load_target_episode_state(self._layout, target_key)
            completed_episodes_by_id = {
                episode.episode_id: episode
                for episode in episode_state.completed_episodes
            }
            close_obligations = self._build_target_close_obligations(
                checked_at=checked_at,
                triggers=episode_state.reflection_triggers,
                completed_episodes_by_id=completed_episodes_by_id,
                close_covered_episode_ids=close_covered_episode_ids,
                due_material_by_episode=due_material_by_episode,
            )
            terminal_episode_ids = set(close_covered_episode_ids)
            terminal_episode_ids.update(
                obligation.episode_id for obligation in close_obligations
            )
            candidates.extend(close_obligations)
            for episode in episode_state.completed_episodes:
                if episode.episode_id in terminal_episode_ids:
                    continue
                candidates.extend(
                    self._build_target_horizon_obligations(
                        checked_at=checked_at,
                        episode=episode,
                        coverage_by_episode=coverage_by_episode,
                        allow_open=False,
                    )
                )
            if episode_state.open_episode is not None:
                material_obligation = _first_due_obligation_for_episode(
                    due_material_by_episode,
                    episode_state.open_episode.episode_id,
                )
                open_position_horizon_obligations = (
                    self._build_target_horizon_obligations(
                        checked_at=checked_at,
                        episode=episode_state.open_episode,
                        coverage_by_episode=coverage_by_episode,
                        allow_open=True,
                        reflection_obligation_id=material_obligation.record.obligation_id,
                    )
                    if material_obligation is not None
                    else ()
                )
                candidates.extend(open_position_horizon_obligations)
                if material_obligation is not None and not open_position_horizon_obligations:
                    candidates.extend(
                        self._build_target_open_position_material_update_obligations(
                            checked_at=checked_at,
                            episode=episode_state.open_episode,
                            coverage_by_episode=coverage_by_episode,
                            open_review_history_by_episode=open_review_history_by_episode,
                            material_obligation=material_obligation,
                        )
                    )
        return tuple(
            sorted(
                candidates,
                key=lambda candidate: (
                    candidate.due_at,
                    candidate.closed_at,
                    candidate.opened_at,
                    candidate.episode_id,
                    candidate.candidate_id,
                ),
            )
        )

    def _build_target_close_obligations(
        self,
        *,
        checked_at: datetime,
        triggers: tuple[ReflectionTrigger, ...],
        completed_episodes_by_id: dict[str, ViewEpisode],
        close_covered_episode_ids: set[str],
        due_material_by_episode: dict[str, tuple[PersistedReflectionObligation, ...]],
    ) -> tuple[_TargetCloseObligation, ...]:
        obligations: list[_TargetCloseObligation] = []
        covered_or_added_episode_ids: set[str] = set(close_covered_episode_ids)
        for trigger in triggers:
            if trigger.triggered_at > checked_at:
                continue
            if trigger.episode_id in covered_or_added_episode_ids:
                continue
            episode = completed_episodes_by_id.get(trigger.episode_id)
            if episode is None:
                raise ReflectionReviewLoopError(
                    "Reflection trigger must reference an existing completed episode "
                    f"once triggered_at is due: {trigger.episode_id}."
                )
            if trigger.target_key != episode.target_key:
                raise ReflectionReviewLoopError(
                    "Reflection trigger target_key must match its completed episode."
                )
            closed_at = episode.closed_at
            if closed_at is None:
                raise ReflectionReviewLoopError(
                    "completed_episodes must only contain closed episodes."
                )
            if closed_at > checked_at:
                continue
            obligations.append(
                _TargetCloseObligation(
                    episode_id=episode.episode_id,
                    target_key=episode.target_key,
                    opened_at=episode.opened_at,
                    closed_at=closed_at,
                    triggered_at=trigger.triggered_at,
                )
            )
            covered_or_added_episode_ids.add(trigger.episode_id)
        for episode_id, due_obligations in due_material_by_episode.items():
            if episode_id in covered_or_added_episode_ids:
                continue
            episode = completed_episodes_by_id.get(episode_id)
            if episode is None:
                continue
            closed_at = episode.closed_at
            if closed_at is None or closed_at > checked_at:
                continue
            material_obligation = due_obligations[0]
            obligations.append(
                _TargetCloseObligation(
                    episode_id=episode.episode_id,
                    target_key=episode.target_key,
                    opened_at=episode.opened_at,
                    closed_at=closed_at,
                    triggered_at=material_obligation.record.due_at,
                    reflection_obligation_id=material_obligation.record.obligation_id,
                )
            )
            covered_or_added_episode_ids.add(episode_id)
        return tuple(obligations)

    def _build_target_horizon_obligations(
        self,
        *,
        checked_at: datetime,
        episode: ViewEpisode,
        coverage_by_episode: dict[str, set[float]],
        allow_open: bool,
        reflection_obligation_id: str | None = None,
    ) -> tuple[_TargetHorizonObligation, ...]:
        closed_at = episode.closed_at
        if closed_at is None:
            if not allow_open:
                raise ReflectionReviewLoopError(
                    "completed_episodes must only contain closed episodes."
                )
            closed_at = checked_at
        else:
            if closed_at <= episode.opened_at:
                raise ReflectionReviewLoopError(
                    "target review coverage windows require positive length."
                )
            if closed_at > checked_at:
                return ()
        is_open_episode = episode.closed_at is None
        exposure_anchor_at = (
            self._open_episode_exposure_anchor(episode) if is_open_episode else None
        )
        horizon_anchor_at = exposure_anchor_at or episode.opened_at
        if is_open_episode and exposure_anchor_at is None:
            return ()
        matured_horizons_hours = tuple(
            horizon
            for horizon in self._config.reflection_horizons_hours
            if checked_at >= horizon_anchor_at + timedelta(hours=horizon)
        )
        if not matured_horizons_hours:
            return ()
        covered_horizons = coverage_by_episode.get(episode.episode_id, set())
        invalid_covered_horizons = covered_horizons.difference(
            self._config.reflection_horizons_hours
        )
        if invalid_covered_horizons:
            raise ReflectionReviewLoopError(
                "target review coverage metadata must stay within configured reflection horizons."
            )
        covered_horizons_hours = tuple(
            horizon for horizon in matured_horizons_hours if horizon in covered_horizons
        )
        reviewable_horizons_hours = tuple(
            horizon for horizon in matured_horizons_hours if horizon not in covered_horizons
        )
        if reflection_obligation_id is not None:
            reviewable_horizons_hours = reviewable_horizons_hours[:1]
        return tuple(
            _TargetHorizonObligation(
                episode_id=episode.episode_id,
                target_key=episode.target_key,
                opened_at=episode.opened_at,
                closed_at=closed_at,
                horizon_hours=horizon,
                covered_horizons_hours=covered_horizons_hours,
                is_open=episode.closed_at is None,
                exposure_anchor_at=exposure_anchor_at,
                reflection_obligation_id=reflection_obligation_id,
            )
            for horizon in reviewable_horizons_hours
        )

    def _build_target_open_position_material_update_obligations(
        self,
        *,
        checked_at: datetime,
        episode: ViewEpisode,
        coverage_by_episode: dict[str, set[float]],
        open_review_history_by_episode: dict[str, _OpenMaterialUpdateHistory],
        material_obligation: PersistedReflectionObligation | None = None,
    ) -> tuple[_TargetOpenMaterialUpdateObligation, ...]:
        if material_obligation is None:
            return ()
        if episode.closed_at is not None:
            return ()
        exposure_anchor_at = self._open_episode_exposure_anchor(episode)
        if exposure_anchor_at is None:
            return ()
        max_horizon = max(self._config.reflection_horizons_hours)
        covered_horizons = coverage_by_episode.get(episode.episode_id, set())
        if max_horizon not in covered_horizons:
            return ()
        matured_uncovered_horizons = tuple(
            horizon
            for horizon in self._config.reflection_horizons_hours
            if (
                checked_at >= exposure_anchor_at + timedelta(hours=horizon)
                and horizon not in covered_horizons
            )
        )
        if matured_uncovered_horizons:
            return ()
        history = open_review_history_by_episode.get(episode.episode_id)
        if history is None:
            return ()
        if checked_at < material_obligation.record.due_at:
            return ()
        return (
            _TargetOpenMaterialUpdateObligation(
                episode_id=episode.episode_id,
                target_key=episode.target_key,
                opened_at=episode.opened_at,
                closed_at=checked_at,
                previous_open_memory_update_at=history.last_memory_update_at,
                material_update_sequence=history.material_update_count + 1,
                exposure_anchor_at=exposure_anchor_at,
                triggered_at=material_obligation.record.due_at,
                reflection_obligation_id=material_obligation.record.obligation_id,
            ),
        )

    def _due_material_obligations_by_episode(
        self,
        *,
        target_key: str,
        checked_at: datetime,
    ) -> dict[str, tuple[PersistedReflectionObligation, ...]]:
        grouped: dict[str, list[PersistedReflectionObligation]] = {}
        for persisted in self._obligation_store.read_due_obligations(
            target_key=target_key,
            as_of=checked_at,
        ):
            episode_id = persisted.record.episode_id
            if episode_id is None:
                continue
            grouped.setdefault(episode_id, []).append(persisted)
        return {
            episode_id: tuple(
                sorted(
                    obligations,
                    key=lambda item: (
                        item.record.due_at,
                        item.record.trigger_kind,
                        item.record.obligation_id,
                    ),
                )
            )
            for episode_id, obligations in grouped.items()
        }

    def _target_coverage_by_episode_id(
        self,
        coverage_records: tuple[ReviewCoverageIndexRecord, ...],
    ) -> dict[str, set[float]]:
        coverage_by_episode: dict[str, set[float]] = {}
        for record in coverage_records:
            if record.review_kind not in {"target_review", "open_position_horizon"}:
                continue
            coverage_by_episode.setdefault(record.episode_id, set()).update(
                record.covered_horizons_hours
            )
        return coverage_by_episode

    def _target_open_review_history_by_episode_id(
        self,
        coverage_records: tuple[ReviewCoverageIndexRecord, ...],
    ) -> dict[str, _OpenMaterialUpdateHistory]:
        history_by_episode: dict[str, _OpenMaterialUpdateHistory] = {}
        for record in coverage_records:
            if record.review_kind not in {
                "open_position_horizon",
                "open_position_material_update",
            }:
                continue
            replay_end_at = record.replay_end_at
            if replay_end_at is None:
                raise ReflectionReviewLoopError(
                    "open-position coverage records must carry replay_end_at."
                )
            material_update_count = (
                1 if record.review_kind == "open_position_material_update" else 0
            )
            existing = history_by_episode.get(record.episode_id)
            if existing is None:
                history_by_episode[record.episode_id] = _OpenMaterialUpdateHistory(
                    last_memory_update_at=replay_end_at,
                    material_update_count=material_update_count,
                )
                continue
            history_by_episode[record.episode_id] = _OpenMaterialUpdateHistory(
                last_memory_update_at=max(existing.last_memory_update_at, replay_end_at),
                material_update_count=(
                    existing.material_update_count + material_update_count
                ),
            )
        return history_by_episode

    def _target_close_covered_episode_ids(
        self,
        coverage_records: tuple[ReviewCoverageIndexRecord, ...],
    ) -> set[str]:
        covered_episode_ids: set[str] = set()
        for record in coverage_records:
            if record.review_kind != "episode_close":
                continue
            if record.episode_id in covered_episode_ids:
                raise ReflectionReviewLoopError(
                    "target close review coverage must not contain duplicate "
                    f"episode_id metadata: {record.episode_id}."
                )
            covered_episode_ids.add(record.episode_id)
        return covered_episode_ids

    def _target_keys(self) -> tuple[str, ...]:
        return tuple(
            target_dir.name
            for target_dir in sorted(self._layout.targets_root.iterdir())
            if target_dir.is_dir()
        )

    def _load_context(
        self,
        selected: ReflectionEligibilityResult,
    ) -> ReflectionLedgerContext:
        if selected.anchor is None:
            raise ReflectionReviewLoopError(
                "Eligible reflection results must include a canonical anchor."
            )
        coverage = ReviewCoverage(
            lookback_hours=self._config.reflection_lookback_hours,
            horizons_hours=selected.eligible_horizons_hours,
        )
        loader = ReflectionLedgerContextLoader(
            read_evidence=self._ledger.read_many,
            read_later_evidence=(
                lambda start_at, end_at, target_keys, exclude_event_ids: (
                    read_evidence_window(
                        start_at=start_at,
                        end_at=end_at,
                        ledger=self._ledger,
                        target_keys=target_keys,
                        exclude_event_ids=exclude_event_ids,
                    )
                )
            ),
            research_memory=self._research_memory,
            outcome_context=self._outcome_context,
            trade_context=self._trade_context,
        )
        context = loader.load_context(anchor=selected.anchor, coverage=coverage)
        return context

    def _load_target_context(
        self,
        selected: _TargetObligation,
    ) -> EpisodeReflectionContext:
        if isinstance(selected, _TargetCloseObligation):
            raise ReflectionReviewLoopError(
                "close obligations must use close reflection context loading."
            )
        if isinstance(selected, _TargetOpenMaterialUpdateObligation):
            raise ReflectionReviewLoopError(
                "open material-update obligations must use open horizon context loading."
            )
        if selected.is_open:
            raise ReflectionReviewLoopError(
                "open horizon obligations must use open horizon context loading."
            )
        if self._market_data is None or self._resolve_market_mapping is None:
            raise ReflectionReviewLoopError(
                "market_data and resolve_market_mapping are required to load target "
                "episode reflection context."
            )
        coverage = ReviewCoverage(
            lookback_hours=self._config.reflection_lookback_hours,
            horizons_hours=selected.reviewable_horizons_hours,
        )
        episode_snapshot = load_closed_episode_snapshot(
            self._layout,
            selected.target_key,
            selected.episode_id,
        )
        market_mapping = self._resolve_market_mapping(selected.target_key)
        counterfactual_writer = CounterfactualReportWriter(self._layout)
        counterfactual_reader = (
            counterfactual_writer.read_for_episode
            if counterfactual_writer.path_for_episode(
                target_key=selected.target_key,
                episode_id=selected.episode_id,
            ).exists()
            else None
        )
        loader = EpisodeReflectionContextLoader(
            read_evidence=self._ledger.read_many,
            read_later_evidence=(
                lambda start_at, end_at, target_keys, exclude_event_ids: (
                    read_evidence_window(
                        start_at=start_at,
                        end_at=end_at,
                        ledger=self._ledger,
                        target_keys=target_keys,
                        exclude_event_ids=exclude_event_ids,
                    )
                )
            ),
            market_mapping=market_mapping,
            market_data=self._market_data,
            coverage=coverage,
            read_execution_records=self._read_execution_records,
            read_target_log_context=(
                lambda target_key: self._research_memory.read_page(
                    f"targets/{target_key}/log.md"
                )
            ),
            read_target_watchlist_context=(
                lambda target_key: self._research_memory.read_page(
                    f"targets/{target_key}/watchlist.md"
                )
            ),
            read_analysis_outcomes=(
                lambda target_key, source_event_ids: read_analysis_outcome_receipts(
                    self._layout.runtime_root,
                    target_key,
                    source_event_ids,
                )
            ),
            read_checker_receipts=(
                lambda target_key, source_event_ids: read_checker_decision_receipts(
                    self._layout.runtime_root,
                    target_key,
                    source_event_ids,
                )
            ),
            read_decision_episodes=self._read_decision_episodes,
            read_counterfactual_report=counterfactual_reader,
            adjustment_policy=_validation_realized_adjustment_policy(
                config=self._config,
                target_key=selected.target_key,
            ),
        )
        return loader.load_context(episode_snapshot=episode_snapshot)

    def _load_open_target_horizon_context(
        self,
        selected: _TargetHorizonObligation | _TargetOpenMaterialUpdateObligation,
    ) -> OpenPositionReflectionContext:
        if self._market_data is None or self._resolve_market_mapping is None:
            raise ReflectionReviewLoopError(
                "market_data and resolve_market_mapping are required to load open "
                "horizon target context."
            )
        as_of_at = selected.closed_at
        episode_state = load_target_episode_state(self._layout, selected.target_key)
        open_episode = episode_state.open_episode
        open_segment = episode_state.open_segment
        if open_episode is None or open_segment is None:
            raise ReflectionReviewLoopError(
                "open horizon target context requires a currently open episode."
            )
        if open_episode.episode_id != selected.episode_id:
            raise ReflectionReviewLoopError(
                f"target-review candidate episode_id={selected.episode_id!r} was not found "
                "as the open episode."
            )
        if open_segment.closed_at is not None:
            raise ReflectionReviewLoopError(
                "open segment must remain open for open horizon target context."
            )
        if as_of_at <= open_episode.opened_at:
            raise ReflectionReviewLoopError(
                "open horizon review as_of_at must be after episode.opened_at."
            )

        state_changes = tuple(
            state_change
            for state_change in read_state_changes(self._layout, selected.target_key)
            if open_episode.opened_at <= state_change.effective_at <= as_of_at
        )
        if not state_changes:
            raise ReflectionReviewLoopError(
                "open horizon review requires persisted state changes for the open episode."
            )
        original_event_ids = self._collect_open_target_original_event_ids(
            state_changes=state_changes
        )
        original_evidence = tuple(self._ledger.read_many(list(original_event_ids)))
        if len(original_evidence) != len(original_event_ids):
            raise ReflectionReviewLoopError(
                "open horizon review requires original evidence for all source events."
            )
        market_mapping = self._resolve_market_mapping(selected.target_key)
        execution_records = self._read_execution_records(
            selected.target_key,
            _execution_lookup_ids(state_changes),
        )
        exposure_anchor_at = _execution_exposure_anchor(
            state_change_id=open_segment.opened_by_state_change_id,
            execution_records=execution_records,
            state_changes=state_changes,
        )
        if exposure_anchor_at is None:
            raise _TargetHorizonNotReady(
                "open horizon review awaits an executed entry record."
            )
        if isinstance(selected, _TargetHorizonObligation) and (
            as_of_at < exposure_anchor_at + timedelta(hours=selected.horizon_hours)
        ):
            raise _TargetHorizonNotReady(
                "open horizon review awaits maturity from actual execution time: "
                f"executed_at={exposure_anchor_at.isoformat()} "
                f"horizon_hours={selected.horizon_hours:g}."
            )
        try:
            market_series = read_realized_target_series(
                market_data=self._market_data,
                market_mapping=market_mapping,
                start_at=exposure_anchor_at,
                end_at=as_of_at,
                adjustment_policy=_validation_realized_adjustment_policy(
                    config=self._config,
                    target_key=selected.target_key,
                ),
            )
        except Exception as exc:
            if _is_missing_prefetched_market_bars(exc):
                raise _TargetHorizonNotReady(
                    "open horizon review awaits prefetched market bars from actual "
                    f"execution time: executed_at={exposure_anchor_at.isoformat()} "
                    f"as_of_at={as_of_at.isoformat()}."
                ) from exc
            raise
        terminal_mark = calculate_open_episode_terminal_mark(
            open_segment=open_segment,
            open_episode=open_episode,
            market_data=market_series.series,
            replay_end_at=as_of_at,
            execution_records=execution_records,
            state_changes=state_changes,
            require_execution_records=True,
            adjustment_sidecar=market_series.adjustment_sidecar,
        )
        later_evidence = read_evidence_window(
            start_at=open_episode.opened_at,
            end_at=as_of_at,
            ledger=self._ledger,
            target_keys=[selected.target_key],
            exclude_event_ids=list(original_event_ids),
        )
        try:
            feedback_context = build_reflection_feedback_context_facts(
                target_key=open_episode.target_key,
                episode_id=open_episode.episode_id,
                state_changes=state_changes,
                market_mapping=market_mapping,
                market_data=market_series.series,
                window_start=open_episode.opened_at,
                window_end=as_of_at,
                read_analysis_outcomes=(
                    lambda target_key, source_event_ids: read_analysis_outcome_receipts(
                        self._layout.runtime_root,
                        target_key,
                        source_event_ids,
                    )
                ),
                read_checker_receipts=(
                    lambda target_key, source_event_ids: read_checker_decision_receipts(
                        self._layout.runtime_root,
                        target_key,
                        source_event_ids,
                    )
                ),
                terminal_mark=terminal_mark,
                execution_records=execution_records,
                require_execution_records=True,
                adjustment_sidecar=market_series.adjustment_sidecar,
            )
        except ReflectionFeedbackContextError as exc:
            raise ReflectionReviewLoopError(str(exc)) from exc

        return OpenPositionReflectionContext(
            episode=open_episode,
            open_segment=open_segment,
            state_changes=state_changes,
            original_evidence=original_evidence,
            later_evidence=tuple(later_evidence),
            market_mapping=market_mapping,
            terminal_mark=terminal_mark,
            review_horizon_hours=(
                selected.horizon_hours
                if isinstance(selected, _TargetHorizonObligation)
                else max(self._config.reflection_horizons_hours)
            ),
            review_kind=(
                "open_position_horizon"
                if isinstance(selected, _TargetHorizonObligation)
                else "open_position_material_update"
            ),
            previous_open_memory_update_at=(
                None
                if isinstance(selected, _TargetHorizonObligation)
                else selected.previous_open_memory_update_at
            ),
            open_position_material_update_sequence=(
                None
                if isinstance(selected, _TargetHorizonObligation)
                else selected.material_update_sequence
            ),
            watchlist_context=self._research_memory.read_page(
                f"targets/{selected.target_key}/watchlist.md"
            ),
            portfolio_feedback=feedback_context.portfolio_feedback,
            market_context_usage=feedback_context.market_context_usage,
            target_log_context=self._research_memory.read_page(
                f"targets/{selected.target_key}/log.md"
            ),
            decision_episodes=self._read_decision_episodes(selected.target_key),
        )

    def _collect_open_target_original_event_ids(
        self,
        state_changes: tuple[ViewStateChange, ...],
    ) -> tuple[str, ...]:
        ordered: list[str] = []
        seen: set[str] = set()
        for state_change in state_changes:
            for event_id in state_change.source_event_ids:
                if event_id in seen:
                    continue
                ordered.append(event_id)
                seen.add(event_id)
        return tuple(ordered)


    def _load_target_close_context(
        self,
        selected: _TargetCloseObligation,
    ) -> CloseEpisodeReflectionContext:
        if self._market_data is None or self._resolve_market_mapping is None:
            raise ReflectionReviewLoopError(
                "market_data and resolve_market_mapping are required to load target "
                "close reflection context."
            )
        episode_snapshot = load_closed_episode_snapshot(
            self._layout,
            selected.target_key,
            selected.episode_id,
        )
        market_mapping = self._resolve_market_mapping(selected.target_key)
        loader = CloseEpisodeReflectionContextLoader(
            read_evidence=self._ledger.read_many,
            read_later_evidence=(
                lambda start_at, end_at, target_keys, exclude_event_ids: (
                    read_evidence_window(
                        start_at=start_at,
                        end_at=end_at,
                        ledger=self._ledger,
                        target_keys=target_keys,
                        exclude_event_ids=exclude_event_ids,
                    )
                )
            ),
            market_mapping=market_mapping,
            market_data=self._market_data,
            read_execution_records=self._read_execution_records,
            read_target_log_context=(
                lambda target_key: self._research_memory.read_page(
                    f"targets/{target_key}/log.md"
                )
            ),
            read_target_watchlist_context=(
                lambda target_key: self._research_memory.read_page(
                    f"targets/{target_key}/watchlist.md"
                )
            ),
            read_analysis_outcomes=(
                lambda target_key, source_event_ids: read_analysis_outcome_receipts(
                    self._layout.runtime_root,
                    target_key,
                    source_event_ids,
                )
            ),
            read_checker_receipts=(
                lambda target_key, source_event_ids: read_checker_decision_receipts(
                    self._layout.runtime_root,
                    target_key,
                    source_event_ids,
                )
            ),
            read_decision_episodes=self._read_decision_episodes,
            adjustment_policy=_validation_realized_adjustment_policy(
                config=self._config,
                target_key=selected.target_key,
            ),
        )
        return loader.load_context(episode_snapshot=episode_snapshot)

    def _read_decision_episodes(self, target_key: str):
        return project_decision_episodes(
            FileBackedDecisionEpisodeStore(self._layout).read_records(
                target_key=target_key
            )
        )

    def _read_execution_records(self, target_key: str, execution_lookup_ids):
        lookup_ids = tuple(execution_lookup_ids)
        lookup_id_set = set(lookup_ids)
        store = ExecutionRecordStore(self._layout)
        records = tuple(
            persisted.record
            for persisted in store.read_records(target_key=target_key)
            if persisted.record.execution_record_id in lookup_id_set
        )
        return tuple(
            sorted(
                records,
                key=lambda item: (item.business_at, item.execution_record_id),
            )
        )

    def _open_episode_exposure_anchor(self, episode: ViewEpisode) -> datetime | None:
        if episode.opened_by_state_change_id is None:
            return None
        state_changes = tuple(
            state_change
            for state_change in read_state_changes(self._layout, episode.target_key)
            if state_change.state_change_id == episode.opened_by_state_change_id
        )
        return _execution_exposure_anchor(
            state_change_id=episode.opened_by_state_change_id,
            execution_records=self._read_execution_records(
                episode.target_key,
                _execution_lookup_ids(state_changes),
            ),
            state_changes=state_changes,
        )

    def _process_target_obligation(
        self,
        selected: _TargetObligation,
    ) -> ReflectionCandidateReceipt:
        if isinstance(selected, _TargetCloseObligation):
            return self._process_target_close_obligation(selected)
        if isinstance(selected, _TargetOpenMaterialUpdateObligation):
            return self._process_target_open_position_material_update_obligation(selected)
        if selected.is_open:
            return self._process_target_open_position_horizon_obligation(selected)
        if self._market_data is None:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=selected.reviewable_horizons_hours,
                failure_stage="review_write",
                failure_reason=_TARGET_REVIEW_WRITE_BLOCKED_REASON,
            )
        try:
            target_context = self._load_target_context(selected)
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=selected.reviewable_horizons_hours,
                failure_stage="context_loading",
                failure_reason=str(exc),
            )
        if self._evaluate_target_review is None:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="dependency_missing",
                covered_horizons_hours=selected.reviewable_horizons_hours,
                missing_dependencies=("target_review_evaluator",),
                failure_stage="evaluation",
                failure_reason="Missing reflection dependencies: target_review_evaluator.",
            )
        try:
            target_evaluation = self._evaluate_target_review(target_context)
            if not isinstance(target_evaluation, TargetReflectionEvaluationResult):
                raise ReflectionReviewLoopError(
                    "evaluate_target_review must return a "
                    "TargetReflectionEvaluationResult instance."
                )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=selected.reviewable_horizons_hours,
                failure_stage="evaluation",
                failure_reason=str(exc),
            )
        try:
            review_receipt = write_target_review(
                context=target_context,
                evaluation=target_evaluation,
                layout=self._layout,
            )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=selected.reviewable_horizons_hours,
                failure_stage="review_write",
                failure_reason=str(exc),
            )
        try:
            learning_receipt = apply_reflection_learning(
                context=target_context,
                evaluation=target_evaluation,
                review_receipt=review_receipt,
                layout=self._layout,
            )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=review_receipt.covered_horizons_hours,
                review_artifact_path=review_receipt.artifact_path,
                failure_stage="learning_write",
                failure_reason=str(exc),
            )
        return self._target_candidate_receipt(
            selected=selected,
            review_receipt=review_receipt,
            learning_receipt=learning_receipt,
        )

    def _process_target_open_position_horizon_obligation(
        self,
        selected: _TargetHorizonObligation,
    ) -> ReflectionCandidateReceipt:
        if self._market_data is None or self._resolve_market_mapping is None:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=selected.reviewable_horizons_hours,
                failure_stage="review_write",
                failure_reason=_TARGET_REVIEW_WRITE_BLOCKED_REASON,
            )
        if self._evaluate_target_open_position_review is None:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="dependency_missing",
                covered_horizons_hours=selected.reviewable_horizons_hours,
                missing_dependencies=("target_open_position_reflection_evaluator",),
                failure_stage="evaluation",
                failure_reason=(
                    "Missing reflection dependencies: target_open_position_reflection_evaluator."
                ),
            )
        try:
            open_target_context = self._load_open_target_horizon_context(selected)
        except _TargetHorizonNotReady as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="review_skipped",
                covered_horizons_hours=(),
                failure_reason=str(exc),
            )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=selected.reviewable_horizons_hours,
                failure_stage="context_loading",
                failure_reason=str(exc),
            )
        try:
            open_position_evaluation = self._evaluate_target_open_position_review(
                open_target_context
            )
            if not isinstance(
                open_position_evaluation,
                OpenPositionEvaluationResult,
            ):
                raise ReflectionReviewLoopError(
                    "evaluate_target_open_position_review must return a "
                    "OpenPositionEvaluationResult instance."
                )
        except Exception as exc:
            if _is_unfinished_open_position_horizon_evaluation_contract_failure(exc):
                return ReflectionCandidateReceipt(
                    candidate_id=selected.candidate_id,
                    status="review_skipped",
                    covered_horizons_hours=(),
                    failure_reason=str(exc),
                )
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=selected.reviewable_horizons_hours,
                failure_stage="evaluation",
                failure_reason=str(exc),
            )
        try:
            open_review_receipt = write_open_position_horizon_review(
                context=open_target_context,
                evaluation=open_position_evaluation,
                review_horizon_hours=selected.horizon_hours,
                layout=self._layout,
            )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=selected.reviewable_horizons_hours,
                failure_stage="review_write",
                failure_reason=str(exc),
            )
        try:
            learning_receipt = apply_reflection_learning(
                context=open_target_context,
                evaluation=open_position_evaluation,
                review_receipt=open_review_receipt,
                layout=self._layout,
            )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=open_review_receipt.covered_horizons_hours,
                review_artifact_path=open_review_receipt.artifact_path,
                failure_stage="learning_write",
                failure_reason=str(exc),
            )
        learning_failure_reason = _learning_failure_reason(learning_receipt)
        resolution_failure_reason: str | None = None
        if learning_failure_reason is None:
            try:
                self._resolve_reflection_obligation_from_learning(
                    selected=selected,
                    learning_receipt=learning_receipt,
                )
            except Exception as exc:
                resolution_failure_reason = str(exc)
        return ReflectionCandidateReceipt(
            candidate_id=selected.candidate_id,
            status=(
                "failed"
                if learning_failure_reason is not None or resolution_failure_reason is not None
                else
                "review_written"
                if open_review_receipt.wrote_review_episode
                else "review_skipped"
            ),
            covered_horizons_hours=open_review_receipt.covered_horizons_hours,
            review_artifact_path=open_review_receipt.artifact_path,
            learning_receipt=learning_receipt,
            failure_stage=(
                "learning_write"
                if learning_failure_reason is not None or resolution_failure_reason is not None
                else None
            ),
            failure_reason=(
                learning_failure_reason
                or resolution_failure_reason
                or open_review_receipt.skip_reason
            ),
        )

    def _process_target_open_position_material_update_obligation(
        self,
        selected: _TargetOpenMaterialUpdateObligation,
    ) -> ReflectionCandidateReceipt:
        if self._market_data is None or self._resolve_market_mapping is None:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=(),
                failure_stage="review_write",
                failure_reason=_TARGET_REVIEW_WRITE_BLOCKED_REASON,
            )
        if self._evaluate_target_open_position_review is None:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="dependency_missing",
                covered_horizons_hours=(),
                missing_dependencies=("target_open_position_reflection_evaluator",),
                failure_stage="evaluation",
                failure_reason=(
                    "Missing reflection dependencies: target_open_position_reflection_evaluator."
                ),
            )
        try:
            open_target_context = self._load_open_target_horizon_context(selected)
        except _TargetHorizonNotReady as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="review_skipped",
                covered_horizons_hours=(),
                failure_reason=str(exc),
            )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=(),
                failure_stage="context_loading",
                failure_reason=str(exc),
            )
        try:
            open_position_evaluation = self._evaluate_target_open_position_review(
                open_target_context
            )
            if not isinstance(
                open_position_evaluation,
                OpenPositionEvaluationResult,
            ):
                raise ReflectionReviewLoopError(
                    "evaluate_target_open_position_review must return a "
                    "OpenPositionEvaluationResult instance."
                )
        except Exception as exc:
            if _is_unfinished_open_position_horizon_evaluation_contract_failure(exc):
                return ReflectionCandidateReceipt(
                    candidate_id=selected.candidate_id,
                    status="review_skipped",
                    covered_horizons_hours=(),
                    failure_reason=str(exc),
                )
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=(),
                failure_stage="evaluation",
                failure_reason=str(exc),
            )
        try:
            open_review_receipt = write_open_position_material_update_review(
                context=open_target_context,
                evaluation=open_position_evaluation,
                material_update_sequence=selected.material_update_sequence,
                layout=self._layout,
            )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=(),
                failure_stage="review_write",
                failure_reason=str(exc),
            )
        try:
            learning_receipt = apply_reflection_learning(
                context=open_target_context,
                evaluation=open_position_evaluation,
                review_receipt=open_review_receipt,
                layout=self._layout,
            )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=(),
                review_artifact_path=open_review_receipt.artifact_path,
                failure_stage="learning_write",
                failure_reason=str(exc),
            )
        learning_failure_reason = _learning_failure_reason(learning_receipt)
        resolution_failure_reason: str | None = None
        if learning_failure_reason is None:
            try:
                self._resolve_reflection_obligation_from_learning(
                    selected=selected,
                    learning_receipt=learning_receipt,
                )
            except Exception as exc:
                resolution_failure_reason = str(exc)
        return ReflectionCandidateReceipt(
            candidate_id=selected.candidate_id,
            status=(
                "failed"
                if learning_failure_reason is not None or resolution_failure_reason is not None
                else
                "review_written"
                if open_review_receipt.wrote_review_episode
                else "review_skipped"
            ),
            covered_horizons_hours=(),
            review_artifact_path=open_review_receipt.artifact_path,
            learning_receipt=learning_receipt,
            failure_stage=(
                "learning_write"
                if learning_failure_reason is not None or resolution_failure_reason is not None
                else None
            ),
            failure_reason=(
                learning_failure_reason
                if learning_failure_reason is not None
                else
                resolution_failure_reason
                if resolution_failure_reason is not None
                else open_review_receipt.skip_reason
            ),
        )

    def _process_target_close_obligation(
        self,
        selected: _TargetCloseObligation,
    ) -> ReflectionCandidateReceipt:
        if self._market_data is None:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=(),
                failure_stage="review_write",
                failure_reason=_TARGET_REVIEW_WRITE_BLOCKED_REASON,
            )
        try:
            close_context = self._load_target_close_context(selected)
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=(),
                failure_stage="context_loading",
                failure_reason=str(exc),
            )
        if self._evaluate_target_close_review is None:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="dependency_missing",
                covered_horizons_hours=(),
                missing_dependencies=("target_close_review_evaluator",),
                failure_stage="evaluation",
                failure_reason=(
                    "Missing reflection dependencies: target_close_review_evaluator."
                ),
            )
        try:
            close_evaluation = self._evaluate_target_close_review(close_context)
            if not isinstance(
                close_evaluation,
                TargetCloseReflectionEvaluationResult,
            ):
                raise ReflectionReviewLoopError(
                    "evaluate_target_close_review must return a "
                    "TargetCloseReflectionEvaluationResult instance."
                )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=(),
                failure_stage="evaluation",
                failure_reason=str(exc),
            )
        try:
            close_receipt = write_target_close_review(
                context=close_context,
                evaluation=close_evaluation,
                layout=self._layout,
            )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=(),
                failure_stage="review_write",
                failure_reason=str(exc),
            )
        try:
            learning_receipt = apply_reflection_learning(
                context=close_context,
                evaluation=close_evaluation,
                review_receipt=close_receipt,
                layout=self._layout,
            )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.candidate_id,
                status="failed",
                covered_horizons_hours=(),
                review_artifact_path=close_receipt.artifact_path,
                failure_stage="learning_write",
                failure_reason=str(exc),
            )
        return self._target_close_candidate_receipt(
            selected=selected,
            review_receipt=close_receipt,
            learning_receipt=learning_receipt,
        )

    def _process_shared_candidate(
        self,
        selected: ReflectionEligibilityResult,
    ) -> ReflectionCandidateReceipt:
        if self._outcome_context is None:
            return ReflectionCandidateReceipt(
                candidate_id=selected.anchor_id,
                status="dependency_missing",
                source_log_path=selected.source_log_path,
                covered_horizons_hours=selected.eligible_horizons_hours,
                missing_dependencies=("outcome_context",),
                failure_stage="context_loading",
                failure_reason="Missing reflection dependencies: outcome_context.",
            )
        if self._evaluate_review is None:
            return ReflectionCandidateReceipt(
                candidate_id=selected.anchor_id,
                status="dependency_missing",
                source_log_path=selected.source_log_path,
                covered_horizons_hours=selected.eligible_horizons_hours,
                missing_dependencies=("review_evaluator",),
                failure_stage="evaluation",
                failure_reason="Missing reflection dependencies: review_evaluator.",
            )
        try:
            context = self._load_context(selected)
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.anchor_id,
                status="failed",
                source_log_path=selected.source_log_path,
                failure_stage="context_loading",
                failure_reason=str(exc),
            )
        try:
            evaluation = self._evaluate_review(context)
            if not isinstance(evaluation, ReflectionEvaluationResult):
                raise ReflectionReviewLoopError(
                    "evaluate_review must return a ReflectionEvaluationResult instance."
                )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.anchor_id,
                status="failed",
                source_log_path=selected.source_log_path,
                failure_stage="evaluation",
                failure_reason=str(exc),
            )
        if selected.anchor is None:
            return ReflectionCandidateReceipt(
                candidate_id=selected.anchor_id,
                status="failed",
                source_log_path=selected.source_log_path,
                failure_stage="context_loading",
                failure_reason=(
                    "Selected eligible reflection result must include a canonical anchor."
                ),
            )
        if selected.anchor.target_key is not None:
            return ReflectionCandidateReceipt(
                candidate_id=selected.anchor_id,
                status="failed",
                source_log_path=selected.source_log_path,
                failure_stage="review_write",
                failure_reason=_TARGET_REVIEW_WRITE_BLOCKED_REASON,
            )
        try:
            shared_anchor_receipt = write_shared_follow_on_anchor(
                source_entry=context.anchor_entry,
                original_evidence=context.original_evidence,
                evaluation=evaluation,
                layout=self._layout,
                research_memory=self._memory_wrapper,
            )
        except Exception as exc:
            return ReflectionCandidateReceipt(
                candidate_id=selected.anchor_id,
                status="failed",
                source_log_path=selected.source_log_path,
                covered_horizons_hours=selected.eligible_horizons_hours,
                failure_stage="shared_anchor_write",
                failure_reason=str(exc),
            )
        return self._shared_candidate_receipt(
            selected=selected,
            shared_anchor_receipt=shared_anchor_receipt,
        )

    def _shared_candidate_receipt(
        self,
        *,
        selected: ReflectionEligibilityResult,
        shared_anchor_receipt: SharedAnchorWriteReceipt,
    ) -> ReflectionCandidateReceipt:
        status: ReflectionCandidateStatus = (
            "shared_anchor_written"
            if shared_anchor_receipt.wrote_shared_anchor
            else "review_skipped"
        )
        return ReflectionCandidateReceipt(
            candidate_id=selected.anchor_id,
            status=status,
            source_log_path=selected.source_log_path,
            covered_horizons_hours=selected.eligible_horizons_hours,
            shared_anchor_id=shared_anchor_receipt.appended_anchor_id,
            shared_anchor_artifact_path=shared_anchor_receipt.artifact_path,
            shared_log_append_decision=shared_anchor_receipt.shared_log_append_decision,
            failure_reason=(
                shared_anchor_receipt.skip_reason
                if not shared_anchor_receipt.wrote_shared_anchor
                else None
            ),
        )

    def _with_cadence_reviews(
        self,
        receipt: ReflectionHeartbeatReceipt,
        *,
        cadence_due: bool,
    ) -> ReflectionHeartbeatReceipt:
        if receipt.status in {"not_due", "failed"} or not cadence_due:
            return receipt
        try:
            cadence_review_receipts = self._run_cadence_reviews(receipt.checked_at)
        except Exception as exc:
            return replace(
                receipt,
                status="failed",
                cadence_review_receipts=(),
                failure_stage="cadence_review",
                failure_reason=str(exc),
            )
        return replace(receipt, cadence_review_receipts=cadence_review_receipts)

    def _record_cadence_completion(
        self,
        receipt: ReflectionHeartbeatReceipt,
        *,
        cadence_due: bool,
    ) -> None:
        if not cadence_due:
            return
        if receipt.status in {
            "no_eligible_candidates",
            "review_written",
            "review_skipped",
            "shared_anchor_written",
        }:
            self._last_completed_at = receipt.checked_at

    def _run_cadence_reviews(
        self,
        checked_at: datetime,
    ) -> tuple[CadencePatternReviewReceipt, ...]:
        return tuple(
            run_cadence_pattern_review(
                layout=self._layout,
                window_kind=window_kind,
                checked_at=checked_at,
                min_required_reviews=_CADENCE_MIN_REQUIRED_REVIEWS,
            )
            for window_kind in _CADENCE_WINDOW_KINDS
        )

    def _target_candidate_receipt(
        self,
        *,
        selected: _TargetObligation,
        review_receipt: TargetReviewWriteReceipt,
        learning_receipt: ReflectionLearningReceipt | None,
    ) -> ReflectionCandidateReceipt:
        status: ReflectionCandidateStatus = (
            "review_written"
            if review_receipt.wrote_review_episode
            else "review_skipped"
        )
        learning_failure_reason = _learning_failure_reason(learning_receipt)
        resolution_failure_reason: str | None = None
        if learning_failure_reason is not None:
            status = "failed"
        elif learning_receipt is not None:
            try:
                self._resolve_reflection_obligation_from_learning(
                    selected=selected,
                    learning_receipt=learning_receipt,
                )
            except Exception as exc:
                status = "failed"
                resolution_failure_reason = str(exc)
        return ReflectionCandidateReceipt(
            candidate_id=selected.candidate_id,
            status=status,
            covered_horizons_hours=review_receipt.covered_horizons_hours,
            review_artifact_path=review_receipt.artifact_path,
            learning_receipt=learning_receipt,
            failure_stage=(
                "learning_write"
                if learning_failure_reason is not None or resolution_failure_reason is not None
                else None
            ),
            failure_reason=(
                learning_failure_reason
                if learning_failure_reason is not None
                else
                resolution_failure_reason
                if resolution_failure_reason is not None
                else
                review_receipt.skip_reason
                if not review_receipt.wrote_review_episode
                else None
            ),
        )

    def _target_close_candidate_receipt(
        self,
        *,
        selected: _TargetCloseObligation,
        review_receipt: TargetCloseReviewWriteReceipt,
        learning_receipt: ReflectionLearningReceipt | None,
    ) -> ReflectionCandidateReceipt:
        status: ReflectionCandidateStatus = (
            "review_written"
            if review_receipt.wrote_review_episode
            else "review_skipped"
        )
        learning_failure_reason = _learning_failure_reason(learning_receipt)
        resolution_failure_reason: str | None = None
        if learning_failure_reason is not None:
            status = "failed"
        elif learning_receipt is not None:
            try:
                self._resolve_reflection_obligation_from_learning(
                    selected=selected,
                    learning_receipt=learning_receipt,
                )
            except Exception as exc:
                status = "failed"
                resolution_failure_reason = str(exc)
        return ReflectionCandidateReceipt(
            candidate_id=selected.candidate_id,
            status=status,
            covered_horizons_hours=(),
            review_artifact_path=review_receipt.artifact_path,
            learning_receipt=learning_receipt,
            failure_stage=(
                "learning_write"
                if learning_failure_reason is not None or resolution_failure_reason is not None
                else None
            ),
            failure_reason=(
                learning_failure_reason
                if learning_failure_reason is not None
                else
                resolution_failure_reason
                if resolution_failure_reason is not None
                else
                review_receipt.skip_reason
                if not review_receipt.wrote_review_episode
                else None
            ),
        )

    def _resolve_reflection_obligation_from_learning(
        self,
        *,
        selected: _TargetObligation,
        learning_receipt: ReflectionLearningReceipt,
    ) -> None:
        obligation_id = selected.reflection_obligation_id
        if obligation_id is None:
            return
        resolution = self._episode_memory_resolution_for_learning(
            selected=selected,
            obligation_id=obligation_id,
            learning_receipt=learning_receipt,
        )
        if resolution is None:
            raise ReflectionReviewLoopError(
                "material reflection obligations require a committed "
                "EpisodeMemoryDelta or EpisodeMemoryNoUpdate."
            )
        self._obligation_store.append_resolution(resolution)

    def _episode_memory_resolution_for_learning(
        self,
        *,
        selected: _TargetObligation,
        obligation_id: str,
        learning_receipt: ReflectionLearningReceipt,
    ) -> ReflectionObligationResolution | None:
        store = FileBackedEpisodeMemoryStore(self._layout)
        for action_receipt in learning_receipt.action_receipts:
            if (
                action_receipt.outcome != "emit_episode_memory_candidate"
                or action_receipt.status != "written"
                or action_receipt.artifact_path is None
            ):
                continue
            artifact_path = action_receipt.artifact_path.resolve(strict=False)
            delta_path = store.delta_path(
                target_key=selected.target_key,
                episode_id=selected.episode_id,
            )
            if artifact_path == delta_path.resolve(strict=False):
                matching_deltas = tuple(
                    persisted
                    for persisted in store.read_deltas(
                        target_key=selected.target_key,
                        episode_id=selected.episode_id,
                    )
                    if learning_receipt.source_review_path
                    in persisted.record.source_refs.review_paths
                )
                if not matching_deltas:
                    raise ReflectionReviewLoopError(
                        "written episode-memory delta artifact did not contain a "
                        "matching canonical delta record."
                    )
                return _build_obligation_resolution(
                    obligation_id=obligation_id,
                    target_key=selected.target_key,
                    resolution_kind="episode_memory_delta",
                    source_record_ids=tuple(
                        persisted.record.delta_id for persisted in matching_deltas
                    ),
                    source_record_hashes=tuple(
                        persisted.record_hash for persisted in matching_deltas
                    ),
                    resolved_at=max(
                        persisted.record.recorded_at for persisted in matching_deltas
                    ),
                )
            no_update_path = store.no_update_path(
                target_key=selected.target_key,
                episode_id=selected.episode_id,
            )
            if artifact_path == no_update_path.resolve(strict=False):
                matching_no_updates = tuple(
                    persisted
                    for persisted in store.read_no_updates(
                        target_key=selected.target_key,
                        episode_id=selected.episode_id,
                    )
                    if persisted.record.review_path == learning_receipt.source_review_path
                )
                if not matching_no_updates:
                    raise ReflectionReviewLoopError(
                        "written episode-memory no-update artifact did not contain a "
                        "matching canonical no-update record."
                    )
                return _build_obligation_resolution(
                    obligation_id=obligation_id,
                    target_key=selected.target_key,
                    resolution_kind="episode_memory_no_update",
                    source_record_ids=tuple(
                        persisted.record.receipt_id for persisted in matching_no_updates
                    ),
                    source_record_hashes=tuple(
                        persisted.record_hash for persisted in matching_no_updates
                    ),
                    resolved_at=max(
                        persisted.record.recorded_at for persisted in matching_no_updates
                    ),
                )
        return None

    def _heartbeat_receipt(
        self,
        *,
        heartbeat_number: int,
        checked_at: datetime,
        entry_receipt: ReflectionEntryReceipt,
        eligible_candidate_ids: tuple[str, ...],
        processed_candidate_receipts: tuple[ReflectionCandidateReceipt, ...],
    ) -> ReflectionHeartbeatReceipt:
        heartbeat_status = self._heartbeat_status(processed_candidate_receipts)
        failed_candidate = next(
            (
                item
                for item in processed_candidate_receipts
                if item.status in {"dependency_missing", "failed"}
            ),
            None,
        )
        detail_candidate = failed_candidate
        if detail_candidate is None:
            detail_candidate = next(
                (
                    item
                    for item in processed_candidate_receipts
                    if item.failure_reason is not None
                ),
                None,
            )
        return ReflectionHeartbeatReceipt(
            heartbeat_number=heartbeat_number,
            checked_at=checked_at,
            status=heartbeat_status,
            due_check=entry_receipt.due_check,
            run_receipt=entry_receipt.run_receipt,
            eligible_candidate_ids=eligible_candidate_ids,
            processed_candidate_receipts=processed_candidate_receipts,
            missing_dependencies=(
                failed_candidate.missing_dependencies if failed_candidate is not None else ()
            ),
            failure_stage=(
                failed_candidate.failure_stage if failed_candidate is not None else None
            ),
            failure_reason=(
                detail_candidate.failure_reason if detail_candidate is not None else None
            ),
        )

    def _heartbeat_status(
        self,
        processed_candidate_receipts: tuple[ReflectionCandidateReceipt, ...],
    ) -> ReflectionHeartbeatStatus:
        if not processed_candidate_receipts:
            raise ReflectionReviewLoopError(
                "processed_candidate_receipts must be non-empty for due heartbeats."
            )
        if any(item.status == "failed" for item in processed_candidate_receipts):
            return "failed"
        if any(item.status == "dependency_missing" for item in processed_candidate_receipts):
            return "dependency_missing"
        if any(item.status == "review_written" for item in processed_candidate_receipts):
            return "review_written"
        if any(item.status == "shared_anchor_written" for item in processed_candidate_receipts):
            return "shared_anchor_written"
        return "review_skipped"

    def _emit_summary(self, receipt: ReflectionHeartbeatReceipt) -> None:
        if self._emit is None:
            return
        if not _should_emit_heartbeat_summary(receipt):
            return
        self._emit(
            "reflection heartbeat: "
            + " ".join(_reflection_heartbeat_summary_details(receipt))
        )


def _should_emit_heartbeat_summary(receipt: ReflectionHeartbeatReceipt) -> bool:
    if receipt.status == "not_due":
        return False
    if receipt.status in {"failed", "dependency_missing"}:
        return True
    if receipt.eligible_candidate_ids or receipt.processed_candidate_receipts:
        return True
    return bool(receipt.cadence_review_receipts)


def _reflection_heartbeat_summary_details(
    receipt: ReflectionHeartbeatReceipt,
) -> list[str]:
    resume_skip_receipts = tuple(
        item
        for item in receipt.processed_candidate_receipts
        if _is_resume_skip_candidate(item)
    )
    visible_candidate_receipts = tuple(
        item
        for item in receipt.processed_candidate_receipts
        if not _is_resume_skip_candidate(item)
    )
    visible_candidate_ids = {
        item.candidate_id for item in visible_candidate_receipts
    }

    details = [
        f"status={receipt.status}",
        f"eligible_candidates={len(receipt.eligible_candidate_ids)}",
        f"processed_candidates={len(receipt.processed_candidate_receipts)}",
    ]
    if resume_skip_receipts:
        details.append(f"resume_skipped_candidates={len(resume_skip_receipts)}")
    if visible_candidate_receipts:
        details.append(
            "eligible_candidate_ids="
            f"{[item for item in receipt.eligible_candidate_ids if item in visible_candidate_ids]}"
        )
        details.append(
            "processed_candidate_ids="
            f"{[item.candidate_id for item in visible_candidate_receipts]}"
        )
        details.append(
            "processed_candidate_statuses="
            f"{[item.status for item in visible_candidate_receipts]}"
        )
        review_artifact_paths = [
            item.review_artifact_path.as_posix()
            for item in visible_candidate_receipts
            if item.review_artifact_path is not None
        ]
        if review_artifact_paths:
            details.append(f"review_artifact_paths={review_artifact_paths}")
        shared_anchor_ids = [
            item.shared_anchor_id
            for item in visible_candidate_receipts
            if item.shared_anchor_id is not None
        ]
        if shared_anchor_ids:
            details.append(f"shared_anchor_ids={shared_anchor_ids}")
        learning_action_count = sum(
            item.learning_receipt.action_count
            for item in visible_candidate_receipts
            if item.learning_receipt is not None
        )
        learning_written_count = sum(
            item.learning_receipt.written_count
            for item in visible_candidate_receipts
            if item.learning_receipt is not None
        )
        learning_failed_count = sum(
            item.learning_receipt.failed_count
            for item in visible_candidate_receipts
            if item.learning_receipt is not None
        )
        if learning_action_count:
            details.append(f"learning_actions={learning_action_count}")
            details.append(f"learning_written={learning_written_count}")
            details.append(f"learning_failed={learning_failed_count}")
    if receipt.cadence_review_receipts:
        cadence_signals = [
            item.decision.pattern_signal for item in receipt.cadence_review_receipts
        ]
        details.append(f"cadence_reviews={len(receipt.cadence_review_receipts)}")
        details.append(f"cadence_pattern_signals={cadence_signals}")
    if receipt.missing_dependencies:
        details.append(f"missing_dependencies={list(receipt.missing_dependencies)}")
    if receipt.failure_stage is not None:
        details.append(f"failure_stage={receipt.failure_stage}")
    if _should_emit_failure_reason(
        receipt,
        visible_candidate_receipts=visible_candidate_receipts,
    ):
        details.append(f"failure_reason={receipt.failure_reason}")
    return details


def _is_resume_skip_candidate(candidate: ReflectionCandidateReceipt) -> bool:
    if candidate.status != "review_skipped" or candidate.failure_reason is None:
        return False
    normalized_reason = candidate.failure_reason.casefold()
    return any(
        marker in normalized_reason for marker in _RESUME_SKIP_REASON_MARKERS
    )


def _should_emit_failure_reason(
    receipt: ReflectionHeartbeatReceipt,
    *,
    visible_candidate_receipts: tuple[ReflectionCandidateReceipt, ...],
) -> bool:
    if receipt.failure_reason is None:
        return False
    if not receipt.processed_candidate_receipts:
        return True
    return any(
        item.failure_reason == receipt.failure_reason
        for item in visible_candidate_receipts
    )


def _is_unfinished_open_position_horizon_evaluation_contract_failure(exc: Exception) -> bool:
    message = str(exc)
    return (
        "Reflection contract repair required:" in message
        and "surface=final_output" in message
        and (
            "error_code=boxed_non_json" in message
            or "error_code=missing_boxed_json" in message
        )
    )


def _learning_failure_reason(receipt: ReflectionLearningReceipt | None) -> str | None:
    if receipt is None or receipt.failed_count == 0:
        return None
    reasons = [
        f"{action_receipt.action_id}: {action_receipt.failure_reason}"
        for action_receipt in receipt.action_receipts
        if action_receipt.status == "failed"
    ]
    return "; ".join(reasons)


def _first_due_obligation_for_episode(
    obligations_by_episode: dict[str, tuple[PersistedReflectionObligation, ...]],
    episode_id: str,
) -> PersistedReflectionObligation | None:
    obligations = obligations_by_episode.get(episode_id)
    if not obligations:
        return None
    return obligations[0]


def _build_obligation_resolution(
    *,
    obligation_id: str,
    target_key: str,
    resolution_kind: Literal["episode_memory_delta", "episode_memory_no_update"],
    source_record_ids: tuple[str, ...],
    source_record_hashes: tuple[str, ...],
    resolved_at: datetime,
) -> ReflectionObligationResolution:
    digest = sha256(
        "\n".join(
            (
                obligation_id,
                resolution_kind,
                *source_record_ids,
                *source_record_hashes,
            )
        ).encode("utf-8")
    ).hexdigest()
    return ReflectionObligationResolution(
        resolution_id=f"reflection_obligation_resolution:{digest}",
        obligation_id=obligation_id,
        target_key=target_key,
        resolved_at=resolved_at,
        resolution_kind=resolution_kind,
        source_record_ids=source_record_ids,
        source_record_hashes=source_record_hashes,
        reason="Validated episode-memory write resolved reflection obligation.",
    )


def _execution_exposure_anchor(
    *,
    state_change_id: str | None,
    execution_records: tuple[ExecutionRecord, ...],
    state_changes: tuple[ViewStateChange, ...] = (),
) -> datetime | None:
    if state_change_id is None:
        return None
    state_change = next(
        (
            item
            for item in state_changes
            if item.state_change_id == state_change_id
            and item.source_kind == "pm_execution_sidecar"
        ),
        None,
    )
    if state_change is None or state_change.execution_record_id is None:
        return None
    record = next(
        (
            item
            for item in execution_records
            if item.execution_record_id == state_change.execution_record_id
            and item.status == "executed"
        ),
        None,
    )
    if record is None:
        return None
    if record.executed_at is None:
        raise ReflectionReviewLoopError("executed entry record is missing executed_at.")
    return record.executed_at


def _execution_lookup_ids(state_changes: tuple[ViewStateChange, ...]) -> tuple[str, ...]:
    return execution_lookup_ids_for_state_changes(state_changes)


def _validation_realized_adjustment_policy(
    *,
    config: KernelConfig,
    target_key: str,
) -> MarketDataAdjustmentPolicy | None:
    validation = config.validation
    if validation is None:
        return None
    mapping_config = validation.market_mappings.get(target_key)
    if mapping_config is None:
        return None
    policy = mapping_config.adjustment_policy
    if policy == "forward_adjusted_visible":
        raise ReflectionReviewLoopError(
            "Target reflection realized market series require realized adjustment "
            f"policy; target_key={target_key!r} configured forward_adjusted_visible."
        )
    return policy


def _is_missing_prefetched_market_bars(exc: Exception) -> bool:
    message = str(exc)
    return (
        "missing prefetched market bars" in message
        or "no prefetched bars" in message
    )


class _NoopOutcomeContextPort:
    def read_outcome_context(
        self,
        anchor: object,
        coverage: object,
    ) -> OutcomeContextPacket:
        _ = (anchor, coverage)
        raise AssertionError(
            "The noop outcome-context port should never be read when no anchors are eligible."
        )


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


__all__ = [
    "ReflectionCandidateReceipt",
    "ReflectionResearchMemoryReader",
    "ReflectionHeartbeatReceipt",
    "ReflectionHeartbeatStatus",
    "ReflectionReviewEvaluator",
    "ReflectionReviewLoop",
    "ReflectionReviewLoopError",
    "TargetCloseReflectionReviewEvaluator",
    "TargetReflectionReviewEvaluator",
]



