"""Target-scoped contracts for closed episode reflection context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from event_trader.contracts import (
    EvidenceLedgerRecord,
    MarketMapping,
    PageReadResult,
    PositionSegment,
    ViewEpisode,
    ViewStateChange,
)
from event_trader.counterfactuals import CounterfactualEvaluationReport
from event_trader.decision_memory import DecisionEpisodeProjection
from event_trader.validation.episode_snapshot import ClosedEpisodeSnapshot
from event_trader.validation.portfolio_feedback import PortfolioFeedbackSnapshot
from event_trader.validation.returns import OpenEpisodeTerminalMark, ValidationReturnsResult

from .contracts import ReflectionContractError, ReviewCoverage
from .market_context_usage import MarketContextUsageFacts


@dataclass(frozen=True, slots=True)
class EpisodeReflectionContext:
    """Read-only target-scoped reflection context for one closed episode."""

    episode: ViewEpisode
    segments: tuple[PositionSegment, ...]
    state_changes: tuple[ViewStateChange, ...]
    coverage: ReviewCoverage
    original_evidence: tuple[EvidenceLedgerRecord, ...]
    later_evidence: tuple[EvidenceLedgerRecord, ...]
    market_mapping: MarketMapping
    market_returns: ValidationReturnsResult
    portfolio_feedback: PortfolioFeedbackSnapshot | None = None
    market_context_usage: MarketContextUsageFacts | None = None
    watchlist_context: PageReadResult | None = None
    target_log_context: PageReadResult | None = None
    decision_episodes: tuple[DecisionEpisodeProjection, ...] = ()
    counterfactual_report: CounterfactualEvaluationReport | None = None

    def __post_init__(self) -> None:
        snapshot = ClosedEpisodeSnapshot(
            episode=self.episode,
            segments=self.segments,
            state_changes=self.state_changes,
        )
        object.__setattr__(self, "episode", snapshot.episode)
        object.__setattr__(self, "segments", snapshot.segments)
        object.__setattr__(self, "state_changes", snapshot.state_changes)

        if not isinstance(self.coverage, ReviewCoverage):
            raise ReflectionContractError("coverage must be a ReviewCoverage instance.")
        if not isinstance(self.original_evidence, tuple) or not self.original_evidence:
            raise ReflectionContractError(
                "original_evidence must be a non-empty tuple of EvidenceLedgerRecord values."
            )
        expected_original_event_ids = _source_event_ids_first_seen(self.state_changes)
        original_event_ids: list[str] = []
        for record in self.original_evidence:
            if not isinstance(record, EvidenceLedgerRecord):
                raise ReflectionContractError(
                    "original_evidence must contain only EvidenceLedgerRecord instances."
                )
            if record.target_key != self.episode.target_key:
                raise ReflectionContractError(
                    "original_evidence records must match episode.target_key."
                )
            original_event_ids.append(record.event_id)
        if tuple(original_event_ids) != expected_original_event_ids:
            raise ReflectionContractError(
                "original_evidence event_ids must exactly match state_changes "
                "source_event_ids recorded in the episode snapshot in first-seen order."
            )

        if not isinstance(self.later_evidence, tuple):
            raise ReflectionContractError(
                "later_evidence must be a tuple of EvidenceLedgerRecord values."
            )
        closed_at = self.episode.closed_at
        if closed_at is None:
            raise ReflectionContractError(
                "closed episode is required to validate later_evidence window."
            )
        episode_window_end = max(
            closed_at,
            self.episode.opened_at + timedelta(hours=max(self.coverage.horizons_hours)),
        )
        seen_later_event_ids: set[str] = set()
        original_event_id_set = set(original_event_ids)
        previous_later_key: tuple[datetime, datetime, str] | None = None
        for record in self.later_evidence:
            if not isinstance(record, EvidenceLedgerRecord):
                raise ReflectionContractError(
                    "later_evidence must contain only EvidenceLedgerRecord instances."
                )
            if record.target_key != self.episode.target_key:
                raise ReflectionContractError(
                    "later_evidence records must match episode.target_key."
                )
            if record.event_id in original_event_id_set:
                raise ReflectionContractError(
                    "later_evidence must exclude original_evidence event_ids."
                )
            if record.event_id in seen_later_event_ids:
                raise ReflectionContractError(
                    "later_evidence must not contain duplicate event_ids."
                )
            if not (self.episode.opened_at < record.ts_event <= episode_window_end):
                raise ReflectionContractError(
                    "later_evidence ts_event must satisfy episode.opened_at < ts_event <= "
                    "max(episode.closed_at, episode.opened_at + max(coverage.horizons_hours))."
                )
            later_key = (record.ts_event, record.ts_init, record.event_id)
            if previous_later_key is not None and later_key < previous_later_key:
                raise ReflectionContractError(
                    "later_evidence must be chronologically ordered by "
                    "(ts_event, ts_init, event_id)."
                )
            previous_later_key = later_key
            seen_later_event_ids.add(record.event_id)

        if not isinstance(self.market_mapping, MarketMapping):
            raise ReflectionContractError("market_mapping must be a MarketMapping instance.")
        if self.market_mapping.target_key != self.episode.target_key:
            raise ReflectionContractError(
                "market_mapping.target_key must match episode.target_key."
            )
        if not isinstance(self.market_returns, ValidationReturnsResult):
            raise ReflectionContractError(
                "market_returns must be a ValidationReturnsResult instance."
            )
        if len(self.market_returns.episode_returns) != 1:
            raise ReflectionContractError(
                "market_returns must contain exactly one episode return for this context."
            )
        episode_return = self.market_returns.episode_returns[0]
        if episode_return.episode_id != self.episode.episode_id:
            raise ReflectionContractError(
                "market_returns episode_id must match context episode_id."
            )
        if episode_return.target_key != self.episode.target_key:
            raise ReflectionContractError(
                "market_returns episode_return.target_key must match context target_key."
            )
        if episode_return.direction != self.episode.direction:
            raise ReflectionContractError(
                "market_returns episode_return.direction must match context episode direction."
            )
        if len(self.market_returns.segment_returns) != len(self.segments):
            raise ReflectionContractError(
                "market_returns segment count must match context segments."
            )
        for segment, segment_return in zip(
            self.segments,
            self.market_returns.segment_returns,
            strict=True,
        ):
            if segment_return.segment_id != segment.segment_id:
                raise ReflectionContractError(
                    "market_returns.segment_returns must match context segments "
                    "by segment_id in order."
                )
            if segment_return.episode_id != segment.episode_id:
                raise ReflectionContractError(
                    "market_returns.segment_returns episode_id must match context segments."
                )
            if segment_return.target_key != segment.target_key:
                raise ReflectionContractError(
                    "market_returns.segment_returns target_key must match context segments."
                )

        if len(self.market_returns.horizon_baselines) != len(self.coverage.horizons_hours):
            raise ReflectionContractError(
                "market_returns horizon baselines must match coverage.horizons_hours."
            )
        expected_horizons = tuple(
            timedelta(hours=horizon_hours)
            for horizon_hours in self.coverage.horizons_hours
        )
        horizon_values = tuple(
            baseline.horizon for baseline in self.market_returns.horizon_baselines
        )
        if horizon_values != expected_horizons:
            raise ReflectionContractError(
                "market_returns.horizon_baselines horizons must match "
                "coverage.horizons_hours in order."
            )
        for baseline in self.market_returns.horizon_baselines:
            if baseline.episode_id != self.episode.episode_id:
                raise ReflectionContractError(
                    "market_returns.horizon_baselines episode_id must match context episode_id."
                )
            if baseline.target_key != self.episode.target_key:
                raise ReflectionContractError(
                    "market_returns.horizon_baselines target_key must match context target_key."
                )

        expected_feedback_window_end = max(
            closed_at,
            self.episode.opened_at + timedelta(hours=max(self.coverage.horizons_hours)),
        )
        _validate_portfolio_feedback(
            feedback=self.portfolio_feedback,
            target_key=self.episode.target_key,
            window_start=self.episode.opened_at,
            window_end=expected_feedback_window_end,
        )
        _validate_market_context_usage(self.market_context_usage)

        if self.watchlist_context is not None:
            if not isinstance(self.watchlist_context, PageReadResult):
                raise ReflectionContractError(
                    "watchlist_context must be a PageReadResult when provided."
                )
            expected_watchlist_path = f"targets/{self.episode.target_key}/watchlist.md"
            if self.watchlist_context.page_path != expected_watchlist_path:
                raise ReflectionContractError(
                    "watchlist_context.page_path must match the episode target watchlist page."
                )

        if self.target_log_context is not None:
            if not isinstance(self.target_log_context, PageReadResult):
                raise ReflectionContractError(
                    "target_log_context must be a PageReadResult when provided."
                )
            expected_log_page_path = f"targets/{self.episode.target_key}/log.md"
            if self.target_log_context.page_path != expected_log_page_path:
                raise ReflectionContractError(
                    "target_log_context.page_path must match the episode target log page."
                )
        object.__setattr__(
            self,
            "decision_episodes",
            _validate_decision_episodes(self.decision_episodes),
        )
        _validate_counterfactual_report(
            report=self.counterfactual_report,
            episode_id=self.episode.episode_id,
            target_key=self.episode.target_key,
        )


@dataclass(frozen=True, slots=True)
class CloseEpisodeReflectionContext:
    """Read-only target-scoped reflection context at episode close/reversal time."""

    episode: ViewEpisode
    segments: tuple[PositionSegment, ...]
    state_changes: tuple[ViewStateChange, ...]
    original_evidence: tuple[EvidenceLedgerRecord, ...]
    later_evidence: tuple[EvidenceLedgerRecord, ...]
    market_mapping: MarketMapping
    market_returns: ValidationReturnsResult
    watchlist_context: PageReadResult
    portfolio_feedback: PortfolioFeedbackSnapshot | None = None
    market_context_usage: MarketContextUsageFacts | None = None
    target_log_context: PageReadResult | None = None
    decision_episodes: tuple[DecisionEpisodeProjection, ...] = ()

    def __post_init__(self) -> None:
        snapshot = ClosedEpisodeSnapshot(
            episode=self.episode,
            segments=self.segments,
            state_changes=self.state_changes,
        )
        object.__setattr__(self, "episode", snapshot.episode)
        object.__setattr__(self, "segments", snapshot.segments)
        object.__setattr__(self, "state_changes", snapshot.state_changes)
        closed_at = self.episode.closed_at
        if closed_at is None:
            raise ReflectionContractError(
                "close episode reflection context requires a closed episode."
            )

        if not isinstance(self.original_evidence, tuple) or not self.original_evidence:
            raise ReflectionContractError(
                "original_evidence must be a non-empty tuple of EvidenceLedgerRecord values."
            )
        expected_original_event_ids = _source_event_ids_first_seen(self.state_changes)
        original_event_ids: list[str] = []
        for record in self.original_evidence:
            if not isinstance(record, EvidenceLedgerRecord):
                raise ReflectionContractError(
                    "original_evidence must contain only EvidenceLedgerRecord instances."
                )
            if record.target_key != self.episode.target_key:
                raise ReflectionContractError(
                    "original_evidence records must match episode.target_key."
                )
            original_event_ids.append(record.event_id)
        if tuple(original_event_ids) != expected_original_event_ids:
            raise ReflectionContractError(
                "original_evidence event_ids must exactly match state_changes "
                "source_event_ids recorded in the episode snapshot in first-seen order."
            )

        if not isinstance(self.later_evidence, tuple):
            raise ReflectionContractError(
                "later_evidence must be a tuple of EvidenceLedgerRecord values."
            )
        original_event_id_set = set(original_event_ids)
        previous_later_key: tuple[datetime, datetime, str] | None = None
        seen_later_event_ids: set[str] = set()
        for record in self.later_evidence:
            if not isinstance(record, EvidenceLedgerRecord):
                raise ReflectionContractError(
                    "later_evidence must contain only EvidenceLedgerRecord instances."
                )
            if record.target_key != self.episode.target_key:
                raise ReflectionContractError(
                    "later_evidence records must match episode.target_key."
                )
            if record.event_id in original_event_id_set:
                raise ReflectionContractError(
                    "later_evidence must exclude original_evidence event_ids."
                )
            if record.event_id in seen_later_event_ids:
                raise ReflectionContractError(
                    "later_evidence must not contain duplicate event_ids."
                )
            if not (self.episode.opened_at < record.ts_event <= closed_at):
                raise ReflectionContractError(
                    "later_evidence ts_event must satisfy episode.opened_at < "
                    "ts_event <= episode.closed_at."
                )
            later_key = (record.ts_event, record.ts_init, record.event_id)
            if previous_later_key is not None and later_key < previous_later_key:
                raise ReflectionContractError(
                    "later_evidence must be chronologically ordered by "
                    "(ts_event, ts_init, event_id)."
                )
            previous_later_key = later_key
            seen_later_event_ids.add(record.event_id)

        if not isinstance(self.market_mapping, MarketMapping):
            raise ReflectionContractError("market_mapping must be a MarketMapping instance.")
        if self.market_mapping.target_key != self.episode.target_key:
            raise ReflectionContractError(
                "market_mapping.target_key must match episode.target_key."
            )
        if not isinstance(self.market_returns, ValidationReturnsResult):
            raise ReflectionContractError(
                "market_returns must be a ValidationReturnsResult instance."
            )
        if len(self.market_returns.episode_returns) != 1:
            raise ReflectionContractError(
                "market_returns must contain exactly one episode return for this context."
            )
        episode_return = self.market_returns.episode_returns[0]
        if episode_return.episode_id != self.episode.episode_id:
            raise ReflectionContractError(
                "market_returns episode_id must match context episode_id."
            )
        if episode_return.target_key != self.episode.target_key:
            raise ReflectionContractError(
                "market_returns episode_return.target_key must match context target_key."
            )
        if episode_return.direction != self.episode.direction:
            raise ReflectionContractError(
                "market_returns episode_return.direction must match context episode direction."
            )
        if len(self.market_returns.segment_returns) != len(self.segments):
            raise ReflectionContractError(
                "market_returns segment count must match context segments."
            )
        if self.market_returns.horizon_baselines:
            raise ReflectionContractError(
                "close episode reflection context must not include horizon baselines."
            )
        for segment, segment_return in zip(
            self.segments,
            self.market_returns.segment_returns,
            strict=True,
        ):
            if segment_return.segment_id != segment.segment_id:
                raise ReflectionContractError(
                    "market_returns.segment_returns must match context segments "
                    "by segment_id in order."
                )
            if segment_return.episode_id != segment.episode_id:
                raise ReflectionContractError(
                    "market_returns.segment_returns episode_id must match context segments."
                )
            if segment_return.target_key != segment.target_key:
                raise ReflectionContractError(
                    "market_returns.segment_returns target_key must match context segments."
                )

        _validate_portfolio_feedback(
            feedback=self.portfolio_feedback,
            target_key=self.episode.target_key,
            window_start=self.episode.opened_at,
            window_end=closed_at,
        )
        _validate_market_context_usage(self.market_context_usage)

        if not isinstance(self.watchlist_context, PageReadResult):
            raise ReflectionContractError(
                "watchlist_context must be a PageReadResult instance."
            )
        expected_watchlist_path = f"targets/{self.episode.target_key}/watchlist.md"
        if self.watchlist_context.page_path != expected_watchlist_path:
            raise ReflectionContractError(
                "watchlist_context.page_path must match the episode target watchlist page."
            )
        if self.target_log_context is not None:
            if not isinstance(self.target_log_context, PageReadResult):
                raise ReflectionContractError(
                    "target_log_context must be a PageReadResult when provided."
                )
            expected_log_page_path = f"targets/{self.episode.target_key}/log.md"
            if self.target_log_context.page_path != expected_log_page_path:
                raise ReflectionContractError(
                    "target_log_context.page_path must match the episode target log page."
                )
        object.__setattr__(
            self,
            "decision_episodes",
            _validate_decision_episodes(self.decision_episodes),
        )


@dataclass(frozen=True, slots=True)
class OpenPositionReflectionContext:
    """Replay/live cut-off review context for an episode that remains open."""

    episode: ViewEpisode
    open_segment: PositionSegment
    state_changes: tuple[ViewStateChange, ...]
    original_evidence: tuple[EvidenceLedgerRecord, ...]
    later_evidence: tuple[EvidenceLedgerRecord, ...]
    market_mapping: MarketMapping
    terminal_mark: OpenEpisodeTerminalMark
    review_horizon_hours: float
    watchlist_context: PageReadResult
    review_kind: Literal[
        "open_position_horizon", "open_position_material_update"
    ] = "open_position_horizon"
    previous_open_memory_update_at: datetime | None = None
    open_position_material_update_sequence: int | None = None
    portfolio_feedback: PortfolioFeedbackSnapshot | None = None
    market_context_usage: MarketContextUsageFacts | None = None
    target_log_context: PageReadResult | None = None
    decision_episodes: tuple[DecisionEpisodeProjection, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.episode, ViewEpisode):
            raise ReflectionContractError("episode must be a ViewEpisode instance.")
        if self.episode.closed_at is not None:
            raise ReflectionContractError(
                "open-position context requires an open episode."
            )
        if not isinstance(self.open_segment, PositionSegment):
            raise ReflectionContractError(
                "open_segment must be a PositionSegment instance."
            )
        if self.open_segment.closed_at is not None:
            raise ReflectionContractError(
                "open-position context requires an open segment."
            )
        if self.open_segment.episode_id != self.episode.episode_id:
            raise ReflectionContractError(
                "open_segment.episode_id must match episode.episode_id."
            )
        if self.open_segment.target_key != self.episode.target_key:
            raise ReflectionContractError(
                "open_segment.target_key must match episode.target_key."
            )
        if not isinstance(self.state_changes, tuple) or not self.state_changes:
            raise ReflectionContractError(
                "state_changes must be a non-empty tuple for terminal context."
            )
        for state_change in self.state_changes:
            if not isinstance(state_change, ViewStateChange):
                raise ReflectionContractError(
                    "state_changes must contain only ViewStateChange instances."
                )
            if state_change.target_key != self.episode.target_key:
                raise ReflectionContractError(
                    "state_changes target_key must match episode.target_key."
                )
        expected_original_event_ids = _source_event_ids_first_seen(self.state_changes)
        original_event_ids: list[str] = []
        for record in self.original_evidence:
            if not isinstance(record, EvidenceLedgerRecord):
                raise ReflectionContractError(
                    "original_evidence must contain only EvidenceLedgerRecord instances."
                )
            if record.target_key != self.episode.target_key:
                raise ReflectionContractError(
                    "original_evidence records must match episode.target_key."
                )
            original_event_ids.append(record.event_id)
        if tuple(original_event_ids) != expected_original_event_ids:
            raise ReflectionContractError(
                "original_evidence event_ids must match state_changes source_event_ids."
            )
        if not isinstance(self.later_evidence, tuple):
            raise ReflectionContractError(
                "later_evidence must be a tuple of EvidenceLedgerRecord values."
            )
        original_event_id_set = set(original_event_ids)
        previous_later_key: tuple[datetime, datetime, str] | None = None
        seen_later_event_ids: set[str] = set()
        for record in self.later_evidence:
            if not isinstance(record, EvidenceLedgerRecord):
                raise ReflectionContractError(
                    "later_evidence must contain only EvidenceLedgerRecord instances."
                )
            if record.target_key != self.episode.target_key:
                raise ReflectionContractError(
                    "later_evidence records must match episode.target_key."
                )
            if record.event_id in original_event_id_set:
                raise ReflectionContractError(
                    "later_evidence must exclude original_evidence event_ids."
                )
            if not (self.episode.opened_at < record.ts_event <= self.terminal_mark.replay_end_at):
                raise ReflectionContractError(
                    "later_evidence ts_event must satisfy episode.opened_at < "
                    "ts_event <= terminal_mark.replay_end_at."
                )
            if record.event_id in seen_later_event_ids:
                raise ReflectionContractError(
                    "later_evidence must not contain duplicate event_ids."
                )
            later_key = (record.ts_event, record.ts_init, record.event_id)
            if previous_later_key is not None and later_key < previous_later_key:
                raise ReflectionContractError(
                    "later_evidence must be chronologically ordered by "
                    "(ts_event, ts_init, event_id)."
                )
            previous_later_key = later_key
            seen_later_event_ids.add(record.event_id)
        if not isinstance(self.market_mapping, MarketMapping):
            raise ReflectionContractError("market_mapping must be a MarketMapping instance.")
        if self.market_mapping.target_key != self.episode.target_key:
            raise ReflectionContractError(
                "market_mapping.target_key must match episode.target_key."
            )
        if not isinstance(self.terminal_mark, OpenEpisodeTerminalMark):
            raise ReflectionContractError(
                "terminal_mark must be an OpenEpisodeTerminalMark instance."
            )
        if self.terminal_mark.episode_id != self.episode.episode_id:
            raise ReflectionContractError(
                "terminal_mark.episode_id must match episode.episode_id."
            )
        if self.terminal_mark.target_key != self.episode.target_key:
            raise ReflectionContractError(
                "terminal_mark.target_key must match episode.target_key."
            )
        if (
            isinstance(self.review_horizon_hours, bool)
            or not isinstance(self.review_horizon_hours, int | float)
            or float(self.review_horizon_hours) <= 0
        ):
            raise ReflectionContractError(
                "review_horizon_hours must be greater than zero."
            )
        object.__setattr__(
            self,
            "review_horizon_hours",
            float(self.review_horizon_hours),
        )
        if self.review_kind not in {
            "open_position_horizon",
            "open_position_material_update",
        }:
            raise ReflectionContractError(
                "review_kind must be 'open_position_horizon' or "
                "'open_position_material_update'."
            )
        if self.review_kind == "open_position_material_update":
            if self.previous_open_memory_update_at is None:
                raise ReflectionContractError(
                    "open material-update context requires previous_open_memory_update_at."
                )
            if not (
                self.episode.opened_at
                < self.previous_open_memory_update_at
                < self.terminal_mark.replay_end_at
            ):
                raise ReflectionContractError(
                    "previous_open_memory_update_at must be after episode.opened_at "
                    "and before terminal_mark.replay_end_at."
                )
            if (
                isinstance(self.open_position_material_update_sequence, bool)
                or not isinstance(self.open_position_material_update_sequence, int)
                or self.open_position_material_update_sequence <= 0
            ):
                raise ReflectionContractError(
                    "open material-update context requires "
                    "open_position_material_update_sequence > 0."
                )
        elif (
            self.previous_open_memory_update_at is not None
            or self.open_position_material_update_sequence is not None
        ):
            raise ReflectionContractError(
                "open material-update metadata is only valid for "
                "open_position_material_update context."
            )
        _validate_portfolio_feedback(
            feedback=self.portfolio_feedback,
            target_key=self.episode.target_key,
            window_start=self.episode.opened_at,
            window_end=self.terminal_mark.replay_end_at,
        )
        _validate_market_context_usage(self.market_context_usage)
        if not isinstance(self.watchlist_context, PageReadResult):
            raise ReflectionContractError(
                "watchlist_context must be a PageReadResult instance."
            )
        expected_watchlist_path = f"targets/{self.episode.target_key}/watchlist.md"
        if self.watchlist_context.page_path != expected_watchlist_path:
            raise ReflectionContractError(
                "watchlist_context.page_path must match the episode target watchlist page."
            )
        if self.target_log_context is not None:
            if not isinstance(self.target_log_context, PageReadResult):
                raise ReflectionContractError(
                    "target_log_context must be a PageReadResult when provided."
                )
            expected_log_page_path = f"targets/{self.episode.target_key}/log.md"
            if self.target_log_context.page_path != expected_log_page_path:
                raise ReflectionContractError(
                    "target_log_context.page_path must match the episode target log page."
                )


def _source_event_ids_first_seen(state_changes: tuple[ViewStateChange, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for state_change in state_changes:
        for event_id in state_change.source_event_ids:
            if event_id in seen:
                continue
            seen.add(event_id)
            ordered.append(event_id)
    return tuple(ordered)


def _validate_portfolio_feedback(
    *,
    feedback: PortfolioFeedbackSnapshot | None,
    target_key: str,
    window_start: datetime,
    window_end: datetime,
) -> None:
    if feedback is None:
        return
    if not isinstance(feedback, PortfolioFeedbackSnapshot):
        raise ReflectionContractError(
            "portfolio_feedback must be a PortfolioFeedbackSnapshot when provided."
        )
    if feedback.target_key != target_key:
        raise ReflectionContractError(
            "portfolio_feedback.target_key must match context target_key."
        )
    if feedback.window_start != window_start:
        raise ReflectionContractError(
            "portfolio_feedback.window_start must match context episode.opened_at."
        )
    if feedback.window_end != window_end:
        raise ReflectionContractError(
            "portfolio_feedback.window_end must match the reflection validation window."
        )


def _validate_market_context_usage(
    usage: MarketContextUsageFacts | None,
) -> None:
    if usage is None:
        return
    if not isinstance(usage, MarketContextUsageFacts):
        raise ReflectionContractError(
            "market_context_usage must be a MarketContextUsageFacts when provided."
        )


def _validate_decision_episodes(
    value: tuple[DecisionEpisodeProjection, ...],
) -> tuple[DecisionEpisodeProjection, ...]:
    if not isinstance(value, tuple):
        raise ReflectionContractError("decision_episodes must be a tuple.")
    for episode in value:
        if not isinstance(episode, DecisionEpisodeProjection):
            raise ReflectionContractError(
                "decision_episodes must contain only DecisionEpisodeProjection values."
            )
    return value


def _validate_counterfactual_report(
    *,
    report: CounterfactualEvaluationReport | None,
    episode_id: str,
    target_key: str,
) -> None:
    if report is None:
        return
    if not isinstance(report, CounterfactualEvaluationReport):
        raise ReflectionContractError(
            "counterfactual_report must be CounterfactualEvaluationReport when provided."
        )
    if report.episode_id != episode_id:
        raise ReflectionContractError("counterfactual_report episode_id must match context.")
    if report.target_key != target_key:
        raise ReflectionContractError("counterfactual_report target_key must match context.")
    if report.episode_metrics is None:
        raise ReflectionContractError(
            "counterfactual_report must include episode_metrics."
        )
    if report.decision_quality_attribution is None:
        raise ReflectionContractError(
            "counterfactual_report must include decision_quality_attribution."
        )


__all__ = [
    "CloseEpisodeReflectionContext",
    "ClosedEpisodeSnapshot",
    "EpisodeReflectionContext",
    "MarketContextUsageFacts",
    "OpenPositionReflectionContext",
]




