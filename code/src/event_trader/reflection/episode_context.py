"""Read-side context loader for closed episode reflection.

The loader consumes a persisted closed episode snapshot as its primary runtime
surface. That snapshot is a derived artifact refreshed from canonical
state-changes.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime, timedelta

from event_trader.contracts import EvidenceLedgerRecord, MarketDataPort, PageReadResult
from event_trader.contracts.view_state_change import (
    MarketDataSeries,
    MarketMapping,
    ViewStateChange,
)
from event_trader.counterfactuals import CounterfactualEvaluationReport
from event_trader.decision_memory import DecisionEpisodeProjection
from event_trader.execution.contracts import ExecutionRecord, execution_contract_hash
from event_trader.market.adjustments import (
    MarketAdjustmentSidecar,
    MarketDataAdjustmentPolicy,
)
from event_trader.market.target_series import (
    RealizedTargetSeries,
    read_realized_target_series,
)
from event_trader.validation.episode_snapshot import ClosedEpisodeSnapshot
from event_trader.validation.execution_linkage import (
    execution_lookup_ids_for_state_changes,
)
from event_trader.validation.returns import (
    ValidationReturnsError,
    ValidationReturnsResult,
    calculate_returns,
)

from .contracts import ReviewCoverage
from .feedback_context import (
    ReflectionFeedbackContextError,
    ReflectionFeedbackContextFacts,
    build_reflection_feedback_context_facts,
)
from .market_context_usage import (
    MarketContextReceiptReader,
    empty_market_context_receipt_reader,
)
from .view_contracts import CloseEpisodeReflectionContext, EpisodeReflectionContext

type EpisodeSnapshotReader = Callable[[str, str], ClosedEpisodeSnapshot]
type EvidenceReader = Callable[[list[str]], list[EvidenceLedgerRecord]]
type LaterEvidenceReader = Callable[
    [datetime, datetime, list[str] | None, list[str]],
    list[EvidenceLedgerRecord],
]
type TargetLogContextReader = Callable[[str], PageReadResult | None]
type TargetWatchlistContextReader = Callable[[str], PageReadResult | None]
type DecisionEpisodeReader = Callable[[str], tuple[DecisionEpisodeProjection, ...]]
type ExecutionRecordReader = Callable[[str, Collection[str]], tuple[ExecutionRecord, ...]]
type CounterfactualReportReader = Callable[[str, str], CounterfactualEvaluationReport]


class EpisodeReflectionContextError(ValueError):
    """Raised when closed-episode reflection context reads are malformed."""


class EpisodeReflectionContextLoader:
    """Load target-scoped reflection context for one persisted closed episode."""

    def __init__(
        self,
        *,
        read_evidence: EvidenceReader,
        read_later_evidence: LaterEvidenceReader,
        market_mapping: MarketMapping,
        market_data: MarketDataPort,
        coverage: ReviewCoverage,
        read_execution_records: ExecutionRecordReader,
        read_episode_snapshot: EpisodeSnapshotReader | None = None,
        read_target_log_context: TargetLogContextReader | None = None,
        read_target_watchlist_context: TargetWatchlistContextReader | None = None,
        read_analysis_outcomes: MarketContextReceiptReader | None = None,
        read_checker_receipts: MarketContextReceiptReader | None = None,
        read_decision_episodes: DecisionEpisodeReader | None = None,
        read_counterfactual_report: CounterfactualReportReader | None = None,
        adjustment_policy: MarketDataAdjustmentPolicy | None = None,
    ) -> None:
        if not callable(read_evidence):
            raise EpisodeReflectionContextError("read_evidence must be callable.")
        if not callable(read_later_evidence):
            raise EpisodeReflectionContextError(
                "read_later_evidence must be callable."
            )
        if not isinstance(market_mapping, MarketMapping):
            raise EpisodeReflectionContextError(
                "market_mapping must be a MarketMapping instance."
            )
        if not isinstance(coverage, ReviewCoverage):
            raise EpisodeReflectionContextError(
                "coverage must be a ReviewCoverage instance."
            )
        if not callable(getattr(market_data, "read_series", None)):
            raise EpisodeReflectionContextError(
                "market_data must implement MarketDataPort.read_series."
            )
        if not callable(read_execution_records):
            raise EpisodeReflectionContextError("read_execution_records must be callable.")
        if read_episode_snapshot is not None and not callable(read_episode_snapshot):
            raise EpisodeReflectionContextError(
                "read_episode_snapshot must be callable when provided."
            )
        if read_target_log_context is not None and not callable(read_target_log_context):
            raise EpisodeReflectionContextError(
                "read_target_log_context must be callable when provided."
            )
        if (
            read_target_watchlist_context is not None
            and not callable(read_target_watchlist_context)
        ):
            raise EpisodeReflectionContextError(
                "read_target_watchlist_context must be callable when provided."
            )
        if read_analysis_outcomes is not None and not callable(read_analysis_outcomes):
            raise EpisodeReflectionContextError(
                "read_analysis_outcomes must be callable when provided."
            )
        if read_checker_receipts is not None and not callable(read_checker_receipts):
            raise EpisodeReflectionContextError(
                "read_checker_receipts must be callable when provided."
            )
        if read_decision_episodes is not None and not callable(read_decision_episodes):
            raise EpisodeReflectionContextError(
                "read_decision_episodes must be callable when provided."
            )
        if read_counterfactual_report is not None and not callable(read_counterfactual_report):
            raise EpisodeReflectionContextError(
                "read_counterfactual_report must be callable when provided."
            )

        self._read_evidence = read_evidence
        self._read_later_evidence = read_later_evidence
        self._market_mapping = market_mapping
        self._market_data = market_data
        self._coverage = coverage
        self._read_execution_records = read_execution_records
        self._read_episode_snapshot = read_episode_snapshot
        self._read_target_log_context = read_target_log_context
        self._read_target_watchlist_context = read_target_watchlist_context
        self._read_analysis_outcomes = (
            read_analysis_outcomes or empty_market_context_receipt_reader
        )
        self._read_checker_receipts = (
            read_checker_receipts or empty_market_context_receipt_reader
        )
        self._read_decision_episodes = read_decision_episodes
        self._read_counterfactual_report = read_counterfactual_report
        self._adjustment_policy = adjustment_policy

    def load_context(
        self,
        *,
        episode_snapshot: ClosedEpisodeSnapshot | None = None,
        episode_id: str | None = None,
        as_of_at: datetime | None = None,
    ) -> EpisodeReflectionContext:
        """Load one closed episode reflection context from a persisted snapshot."""
        snapshot = self._resolve_snapshot(
            episode_snapshot=episode_snapshot,
            episode_id=episode_id,
        )
        if snapshot.episode.target_key != self._market_mapping.target_key:
            raise EpisodeReflectionContextError(
                "snapshot target_key must match market_mapping.target_key."
            )

        original_event_ids = _collect_original_event_ids(snapshot.state_changes)
        original_evidence = _read_original_evidence(
            read_evidence=self._read_evidence,
            event_ids=original_event_ids,
            target_key=snapshot.episode.target_key,
        )

        later_window_end = _later_window_end(
            opened_at=snapshot.episode.opened_at,
            closed_at=snapshot.episode.closed_at,
            coverage=self._coverage,
        )
        later_evidence = _read_later_window_evidence(
            read_later_evidence=self._read_later_evidence,
            start_at=snapshot.episode.opened_at,
            end_at=later_window_end,
            target_key=snapshot.episode.target_key,
            exclude_event_ids=original_event_ids,
        )
        horizons = tuple(
            timedelta(hours=horizon_hours) for horizon_hours in self._coverage.horizons_hours
        )
        market_series = _read_market_series(
            market_data=self._market_data,
            market_mapping=self._market_mapping,
            start_at=snapshot.episode.opened_at,
            end_at=later_window_end,
            adjustment_policy=self._adjustment_policy,
        )
        execution_records = _read_execution_records_for_state_changes(
            read_execution_records=self._read_execution_records,
            target_key=snapshot.episode.target_key,
            state_changes=snapshot.state_changes,
        )
        market_returns = _calculate_market_returns(
            market_data=market_series.series,
            snapshot=snapshot,
            horizons=horizons,
            execution_records=execution_records,
            adjustment_sidecar=market_series.adjustment_sidecar,
        )
        feedback_context = _build_feedback_context_facts(
            target_key=snapshot.episode.target_key,
            episode_id=snapshot.episode.episode_id,
            state_changes=snapshot.state_changes,
            market_mapping=self._market_mapping,
            market_data=market_series.series,
            window_start=snapshot.episode.opened_at,
            window_end=later_window_end,
            read_analysis_outcomes=self._read_analysis_outcomes,
            read_checker_receipts=self._read_checker_receipts,
            market_returns=market_returns,
            execution_records=execution_records,
            adjustment_sidecar=market_series.adjustment_sidecar,
        )

        target_log_context = _load_target_log_context(
            read_target_log_context=self._read_target_log_context,
            target_key=snapshot.episode.target_key,
        )
        watchlist_context = _load_target_watchlist_context(
            read_target_watchlist_context=self._read_target_watchlist_context,
            target_key=snapshot.episode.target_key,
        )
        decision_episodes = _load_decision_episodes_for_state_changes(
            read_decision_episodes=self._read_decision_episodes,
            target_key=snapshot.episode.target_key,
            state_changes=snapshot.state_changes,
        )
        counterfactual_report = _load_counterfactual_report(
            read_counterfactual_report=self._read_counterfactual_report,
            target_key=snapshot.episode.target_key,
            episode_id=snapshot.episode.episode_id,
            expected_provenance_hash=_execution_market_provenance_hash(execution_records),
            horizon_end_at=later_window_end,
            as_of_at=as_of_at or later_window_end,
        )

        return EpisodeReflectionContext(
            episode=snapshot.episode,
            segments=snapshot.segments,
            state_changes=snapshot.state_changes,
            coverage=self._coverage,
            original_evidence=original_evidence,
            later_evidence=later_evidence,
            market_mapping=self._market_mapping,
            market_returns=market_returns,
            portfolio_feedback=feedback_context.portfolio_feedback,
            market_context_usage=feedback_context.market_context_usage,
            watchlist_context=watchlist_context,
            target_log_context=target_log_context,
            decision_episodes=decision_episodes,
            counterfactual_report=counterfactual_report,
        )

    def _resolve_snapshot(
        self,
        *,
        episode_snapshot: ClosedEpisodeSnapshot | None,
        episode_id: str | None,
    ) -> ClosedEpisodeSnapshot:
        if episode_snapshot is not None and episode_id is not None:
            raise EpisodeReflectionContextError(
                "Provide either episode_snapshot or episode_id, not both."
            )
        if episode_snapshot is not None:
            if not isinstance(episode_snapshot, ClosedEpisodeSnapshot):
                raise EpisodeReflectionContextError(
                    "episode_snapshot must be a ClosedEpisodeSnapshot instance."
                )
            return episode_snapshot

        if self._read_episode_snapshot is None:
            raise EpisodeReflectionContextError(
                "episode_snapshot is required when no read_episode_snapshot "
                "dependency is configured."
            )
        if episode_id is None or not isinstance(episode_id, str) or not episode_id.strip():
            raise EpisodeReflectionContextError(
                "episode_id must be a non-blank string when snapshot is read through dependency."
            )

        snapshot = self._read_episode_snapshot(episode_id, self._market_mapping.target_key)
        if not isinstance(snapshot, ClosedEpisodeSnapshot):
            raise EpisodeReflectionContextError(
                "read_episode_snapshot must return a ClosedEpisodeSnapshot instance."
            )
        return snapshot


class CloseEpisodeReflectionContextLoader:
    """Load close-time reflection context without horizon lookahead."""

    def __init__(
        self,
        *,
        read_evidence: EvidenceReader,
        read_later_evidence: LaterEvidenceReader,
        market_mapping: MarketMapping,
        market_data: MarketDataPort,
        read_execution_records: ExecutionRecordReader,
        read_target_log_context: TargetLogContextReader | None = None,
        read_target_watchlist_context: TargetWatchlistContextReader | None = None,
        read_analysis_outcomes: MarketContextReceiptReader | None = None,
        read_checker_receipts: MarketContextReceiptReader | None = None,
        read_decision_episodes: DecisionEpisodeReader | None = None,
        adjustment_policy: MarketDataAdjustmentPolicy | None = None,
    ) -> None:
        if not callable(read_evidence):
            raise EpisodeReflectionContextError("read_evidence must be callable.")
        if not callable(read_later_evidence):
            raise EpisodeReflectionContextError(
                "read_later_evidence must be callable."
            )
        if not isinstance(market_mapping, MarketMapping):
            raise EpisodeReflectionContextError(
                "market_mapping must be a MarketMapping instance."
            )
        if not callable(getattr(market_data, "read_series", None)):
            raise EpisodeReflectionContextError(
                "market_data must implement MarketDataPort.read_series."
            )
        if not callable(read_execution_records):
            raise EpisodeReflectionContextError("read_execution_records must be callable.")
        if read_target_log_context is not None and not callable(read_target_log_context):
            raise EpisodeReflectionContextError(
                "read_target_log_context must be callable when provided."
            )
        if (
            read_target_watchlist_context is not None
            and not callable(read_target_watchlist_context)
        ):
            raise EpisodeReflectionContextError(
                "read_target_watchlist_context must be callable when provided."
            )
        if read_analysis_outcomes is not None and not callable(read_analysis_outcomes):
            raise EpisodeReflectionContextError(
                "read_analysis_outcomes must be callable when provided."
            )
        if read_checker_receipts is not None and not callable(read_checker_receipts):
            raise EpisodeReflectionContextError(
                "read_checker_receipts must be callable when provided."
            )
        if read_decision_episodes is not None and not callable(read_decision_episodes):
            raise EpisodeReflectionContextError(
                "read_decision_episodes must be callable when provided."
            )

        self._read_evidence = read_evidence
        self._read_later_evidence = read_later_evidence
        self._market_mapping = market_mapping
        self._market_data = market_data
        self._read_execution_records = read_execution_records
        self._read_target_log_context = read_target_log_context
        self._read_target_watchlist_context = read_target_watchlist_context
        self._read_analysis_outcomes = (
            read_analysis_outcomes or empty_market_context_receipt_reader
        )
        self._read_checker_receipts = (
            read_checker_receipts or empty_market_context_receipt_reader
        )
        self._read_decision_episodes = read_decision_episodes
        self._adjustment_policy = adjustment_policy

    def load_context(
        self,
        *,
        episode_snapshot: ClosedEpisodeSnapshot,
    ) -> CloseEpisodeReflectionContext:
        if not isinstance(episode_snapshot, ClosedEpisodeSnapshot):
            raise EpisodeReflectionContextError(
                "episode_snapshot must be a ClosedEpisodeSnapshot instance."
            )
        snapshot = episode_snapshot
        if snapshot.episode.closed_at is None:
            raise EpisodeReflectionContextError(
                "episode must be closed before close reflection context can load it."
            )
        if snapshot.episode.target_key != self._market_mapping.target_key:
            raise EpisodeReflectionContextError(
                "snapshot target_key must match market_mapping.target_key."
            )

        original_event_ids = _collect_original_event_ids(snapshot.state_changes)
        original_evidence = _read_original_evidence(
            read_evidence=self._read_evidence,
            event_ids=original_event_ids,
            target_key=snapshot.episode.target_key,
        )
        later_evidence = _read_later_window_evidence(
            read_later_evidence=self._read_later_evidence,
            start_at=snapshot.episode.opened_at,
            end_at=snapshot.episode.closed_at,
            target_key=snapshot.episode.target_key,
            exclude_event_ids=original_event_ids,
        )
        execution_records = _read_execution_records_for_state_changes(
            read_execution_records=self._read_execution_records,
            target_key=snapshot.episode.target_key,
            state_changes=snapshot.state_changes,
        )
        market_window_end = _market_window_end_for_execution(
            snapshot.episode.closed_at,
            execution_records,
        )
        market_series = _read_market_series(
            market_data=self._market_data,
            market_mapping=self._market_mapping,
            start_at=snapshot.episode.opened_at,
            end_at=market_window_end,
            adjustment_policy=self._adjustment_policy,
        )
        market_returns = _calculate_market_returns(
            market_data=market_series.series,
            snapshot=snapshot,
            horizons=(),
            execution_records=execution_records,
            adjustment_sidecar=market_series.adjustment_sidecar,
        )
        feedback_context = _build_feedback_context_facts(
            target_key=snapshot.episode.target_key,
            episode_id=snapshot.episode.episode_id,
            state_changes=snapshot.state_changes,
            market_mapping=self._market_mapping,
            market_data=market_series.series,
            window_start=snapshot.episode.opened_at,
            window_end=snapshot.episode.closed_at,
            read_analysis_outcomes=self._read_analysis_outcomes,
            read_checker_receipts=self._read_checker_receipts,
            market_returns=market_returns,
            execution_records=execution_records,
            adjustment_sidecar=market_series.adjustment_sidecar,
        )
        watchlist_context = _load_target_watchlist_context(
            read_target_watchlist_context=self._read_target_watchlist_context,
            target_key=snapshot.episode.target_key,
        )
        if watchlist_context is None:
            raise EpisodeReflectionContextError(
                "close reflection requires target watchlist context."
            )
        target_log_context = _load_target_log_context(
            read_target_log_context=self._read_target_log_context,
            target_key=snapshot.episode.target_key,
        )
        return CloseEpisodeReflectionContext(
            episode=snapshot.episode,
            segments=snapshot.segments,
            state_changes=snapshot.state_changes,
            original_evidence=original_evidence,
            later_evidence=later_evidence,
            market_mapping=self._market_mapping,
            market_returns=market_returns,
            watchlist_context=watchlist_context,
            portfolio_feedback=feedback_context.portfolio_feedback,
            market_context_usage=feedback_context.market_context_usage,
            target_log_context=target_log_context,
            decision_episodes=_load_decision_episodes_for_state_changes(
                read_decision_episodes=self._read_decision_episodes,
                target_key=snapshot.episode.target_key,
                state_changes=snapshot.state_changes,
            ),
        )


def _collect_original_event_ids(state_changes: tuple) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for state_change in state_changes:
        for event_id in state_change.source_event_ids:
            if event_id in seen:
                continue
            seen.add(event_id)
            ordered.append(event_id)
    if not ordered:
        raise EpisodeReflectionContextError(
            "episode snapshot state-changes must include at least one source_event_id."
        )
    return tuple(ordered)


def _load_decision_episodes_for_state_changes(
    *,
    read_decision_episodes: DecisionEpisodeReader | None,
    target_key: str,
    state_changes: tuple[ViewStateChange, ...],
) -> tuple[DecisionEpisodeProjection, ...]:
    if read_decision_episodes is None:
        return ()
    state_change_ids = {state_change.state_change_id for state_change in state_changes}
    projections = read_decision_episodes(target_key)
    if not isinstance(projections, tuple):
        raise EpisodeReflectionContextError(
            "read_decision_episodes must return a tuple."
        )
    selected: list[DecisionEpisodeProjection] = []
    for projection in projections:
        if not isinstance(projection, DecisionEpisodeProjection):
            raise EpisodeReflectionContextError(
                "read_decision_episodes must return DecisionEpisodeProjection values."
            )
        linked_state_change_ids = {
            str(mark.payload.get("state_change_id"))
            for mark in projection.validation_marks
            if isinstance(mark.payload.get("state_change_id"), str)
        }
        if linked_state_change_ids.intersection(state_change_ids):
            selected.append(projection)
    return tuple(selected)


def _load_counterfactual_report(
    *,
    read_counterfactual_report: CounterfactualReportReader | None,
    target_key: str,
    episode_id: str,
    expected_provenance_hash: str | None,
    horizon_end_at: datetime,
    as_of_at: datetime,
) -> CounterfactualEvaluationReport | None:
    if read_counterfactual_report is None:
        return None
    if as_of_at < horizon_end_at:
        return None
    if expected_provenance_hash is None:
        raise EpisodeReflectionContextError(
            "counterfactual report loading requires execution market provenance."
        )
    report = read_counterfactual_report(target_key, episode_id)
    if not isinstance(report, CounterfactualEvaluationReport):
        raise EpisodeReflectionContextError(
            "read_counterfactual_report must return CounterfactualEvaluationReport."
        )
    if report.target_key != target_key:
        raise EpisodeReflectionContextError(
            "counterfactual report target_key must match reflection target_key."
        )
    if report.episode_id != episode_id:
        raise EpisodeReflectionContextError(
            "counterfactual report episode_id must match reflection episode_id."
        )
    if report.market_provenance_hash != expected_provenance_hash:
        raise EpisodeReflectionContextError(
            "counterfactual report market provenance hash mismatch."
        )
    return report


def _execution_market_provenance_hash(
    execution_records: tuple[ExecutionRecord, ...],
) -> str | None:
    for record in execution_records:
        if record.status == "executed" and record.provenance is not None:
            return execution_contract_hash(record.provenance.to_json_payload())
    return None


def _market_window_end_for_execution(
    closed_at: datetime,
    execution_records: tuple[ExecutionRecord, ...],
) -> datetime:
    latest = closed_at
    for record in execution_records:
        if record.status != "executed":
            continue
        if record.executed_at is not None and record.executed_at > latest:
            latest = record.executed_at
        provenance = record.provenance
        if (
            provenance is not None
            and provenance.selected_bar_end_at is not None
            and provenance.selected_bar_end_at > latest
        ):
            latest = provenance.selected_bar_end_at
    return latest


def _read_original_evidence(
    *,
    read_evidence: EvidenceReader,
    event_ids: tuple[str, ...],
    target_key: str,
) -> tuple[EvidenceLedgerRecord, ...]:
    records = read_evidence(list(event_ids))
    if not isinstance(records, list):
        raise EpisodeReflectionContextError(
            "read_evidence must return a list of EvidenceLedgerRecord instances."
        )
    if len(records) != len(event_ids):
        raise EpisodeReflectionContextError(
            "read_evidence must return one record for each requested original event_id."
        )

    normalized: list[EvidenceLedgerRecord] = []
    for index, record in enumerate(records):
        if not isinstance(record, EvidenceLedgerRecord):
            raise EpisodeReflectionContextError(
                "read_evidence must return only EvidenceLedgerRecord instances."
            )
        expected_event_id = event_ids[index]
        if record.event_id != expected_event_id:
            raise EpisodeReflectionContextError(
                "read_evidence must preserve requested event_id order."
            )
        if record.target_key != target_key:
            raise EpisodeReflectionContextError(
                "original evidence target_key must match the episode target."
            )
        normalized.append(record)
    return tuple(normalized)


def _read_later_window_evidence(
    *,
    read_later_evidence: LaterEvidenceReader,
    start_at: datetime,
    end_at: datetime,
    target_key: str,
    exclude_event_ids: tuple[str, ...],
) -> tuple[EvidenceLedgerRecord, ...]:
    records = read_later_evidence(
        start_at,
        end_at,
        [target_key],
        list(exclude_event_ids),
    )
    if not isinstance(records, list):
        raise EpisodeReflectionContextError(
            "read_later_evidence must return a list of EvidenceLedgerRecord instances."
        )

    normalized: list[EvidenceLedgerRecord] = []
    seen: set[str] = set()
    previous_key: tuple[datetime, datetime, str] | None = None
    excluded = set(exclude_event_ids)
    for record in records:
        if not isinstance(record, EvidenceLedgerRecord):
            raise EpisodeReflectionContextError(
                "read_later_evidence must return only EvidenceLedgerRecord instances."
            )
        if record.event_id in excluded:
            raise EpisodeReflectionContextError(
                "later evidence must exclude original view-state-change source_event_ids."
            )
        if record.target_key != target_key:
            raise EpisodeReflectionContextError(
                "later evidence must stay target-scoped to the episode target."
            )
        if record.event_id in seen:
            raise EpisodeReflectionContextError(
                "later evidence must not contain duplicate event_ids."
            )
        if not (start_at < record.ts_event <= end_at):
            raise EpisodeReflectionContextError(
                "later evidence must stay within the configured episode window."
            )
        record_key = (record.ts_event, record.ts_init, record.event_id)
        if previous_key is not None and record_key < previous_key:
            raise EpisodeReflectionContextError(
                "read_later_evidence must preserve chronological event order."
            )
        seen.add(record.event_id)
        previous_key = record_key
        normalized.append(record)
    return tuple(normalized)


def _read_market_series(
    *,
    market_data: MarketDataPort,
    market_mapping: MarketMapping,
    start_at: datetime,
    end_at: datetime,
    adjustment_policy: MarketDataAdjustmentPolicy | None,
) -> RealizedTargetSeries:
    market_series = read_realized_target_series(
        market_data=market_data,
        market_mapping=market_mapping,
        start_at=start_at,
        end_at=end_at,
        adjustment_policy=adjustment_policy,
    )
    return market_series


def _read_execution_records_for_state_changes(
    *,
    read_execution_records: ExecutionRecordReader,
    target_key: str,
    state_changes: tuple[ViewStateChange, ...],
) -> tuple[ExecutionRecord, ...]:
    records = read_execution_records(
        target_key,
        _execution_lookup_ids(state_changes),
    )
    if not isinstance(records, tuple):
        raise EpisodeReflectionContextError(
            "read_execution_records must return a tuple."
        )
    for record in records:
        if not isinstance(record, ExecutionRecord):
            raise EpisodeReflectionContextError(
                "read_execution_records must return ExecutionRecord values."
            )
    return records


def _calculate_market_returns(
    *,
    market_data: MarketDataSeries,
    snapshot: ClosedEpisodeSnapshot,
    horizons: tuple[timedelta, ...],
    execution_records: tuple[ExecutionRecord, ...],
    adjustment_sidecar: MarketAdjustmentSidecar | None,
) -> ValidationReturnsResult:
    try:
        return calculate_returns(
            closed_segments=snapshot.segments,
            closed_episodes=(snapshot.episode,),
            market_data=market_data,
            horizons=horizons,
            execution_records=execution_records,
            state_changes=snapshot.state_changes,
            require_execution_records=True,
            adjustment_sidecar=adjustment_sidecar,
        )
    except ValidationReturnsError as exc:
        raise EpisodeReflectionContextError(str(exc)) from exc


def _execution_lookup_ids(state_changes: tuple[ViewStateChange, ...]) -> tuple[str, ...]:
    return execution_lookup_ids_for_state_changes(state_changes)


def _build_feedback_context_facts(
    *,
    target_key: str,
    episode_id: str,
    state_changes: tuple[ViewStateChange, ...],
    market_mapping: MarketMapping,
    market_data: MarketDataSeries,
    window_start: datetime,
    window_end: datetime,
    read_analysis_outcomes: MarketContextReceiptReader,
    read_checker_receipts: MarketContextReceiptReader,
    market_returns: ValidationReturnsResult,
    execution_records: tuple[ExecutionRecord, ...],
    adjustment_sidecar: MarketAdjustmentSidecar | None,
) -> ReflectionFeedbackContextFacts:
    try:
        return build_reflection_feedback_context_facts(
            target_key=target_key,
            episode_id=episode_id,
            state_changes=state_changes,
            market_mapping=market_mapping,
            market_data=market_data,
            window_start=window_start,
            window_end=window_end,
            read_analysis_outcomes=read_analysis_outcomes,
            read_checker_receipts=read_checker_receipts,
            market_returns=market_returns,
            execution_records=execution_records,
            require_execution_records=True,
            adjustment_sidecar=adjustment_sidecar,
        )
    except ReflectionFeedbackContextError as exc:
        raise EpisodeReflectionContextError(str(exc)) from exc


def _load_target_log_context(
    *,
    read_target_log_context: TargetLogContextReader | None,
    target_key: str,
) -> PageReadResult | None:
    if read_target_log_context is None:
        return None
    page = read_target_log_context(target_key)
    if page is None:
        return None
    if not isinstance(page, PageReadResult):
        raise EpisodeReflectionContextError(
            "read_target_log_context must return a PageReadResult or None."
        )
    expected_page_path = f"targets/{target_key}/log.md"
    if page.page_path != expected_page_path:
        raise EpisodeReflectionContextError(
            "target log context page_path must match episode target log page."
        )
    return page


def _load_target_watchlist_context(
    *,
    read_target_watchlist_context: TargetWatchlistContextReader | None,
    target_key: str,
) -> PageReadResult | None:
    if read_target_watchlist_context is None:
        return None
    page = read_target_watchlist_context(target_key)
    if page is None:
        return None
    if not isinstance(page, PageReadResult):
        raise EpisodeReflectionContextError(
            "read_target_watchlist_context must return a PageReadResult or None."
        )
    expected_page_path = f"targets/{target_key}/watchlist.md"
    if page.page_path != expected_page_path:
        raise EpisodeReflectionContextError(
            "watchlist context page_path must match episode target watchlist page."
        )
    return page


def _later_window_end(
    *,
    opened_at: datetime,
    closed_at: datetime | None,
    coverage: ReviewCoverage,
) -> datetime:
    if closed_at is None:
        raise EpisodeReflectionContextError(
            "episode must be closed before reflection context can load it."
        )
    horizon_end = opened_at + timedelta(hours=max(coverage.horizons_hours))
    return max(closed_at, horizon_end)


__all__ = [
    "CloseEpisodeReflectionContextLoader",
    "EpisodeSnapshotReader",
    "ExecutionRecordReader",
    "EvidenceReader",
    "LaterEvidenceReader",
    "TargetLogContextReader",
    "TargetWatchlistContextReader",
    "EpisodeReflectionContextError",
    "EpisodeReflectionContextLoader",
]

