"""Canonical target-scoped review writer for one closed episode review."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from event_trader.contracts import PageReadResult
from event_trader.contracts.ports import ResearchMemoryPageWritePort
from event_trader.decision_memory import (
    DecisionEpisodeRecord,
    FileBackedDecisionEpisodeStore,
    find_episode_ids_for_state_change,
)
from event_trader.research_memory import (
    ReceiptedResearchMemoryPageWriter,
    ResearchMemoryWriteAttribution,
)
from event_trader.storage import WorkspaceLayout

from .boundaries import enforce_reflection_write_surface
from .contracts import (
    OpenPositionEvaluationResult,
    TargetCloseReflectionEvaluationResult,
    TargetReflectionEvaluationResult,
)
from .market_context_usage import MarketContextUsageFacts
from .review_coverage_index import (
    ReviewCoverageIndexRecord,
    upsert_review_coverage_record,
)
from .view_contracts import (
    CloseEpisodeReflectionContext,
    EpisodeReflectionContext,
    OpenPositionReflectionContext,
)

_COVERAGE_COMMENT_RE = re.compile(
    r"<!--\s*reflection-coverage:\s*episode_id=(?P<episode_id>[^;]+?)\s*;\s*"
    r"covered_horizons_hours=(?P<horizons>[^>]+?)\s*-->",
    re.IGNORECASE,
)

type _MarketContextUsageEvaluation = (
    TargetReflectionEvaluationResult
    | TargetCloseReflectionEvaluationResult
    | OpenPositionEvaluationResult
)


class ReflectionReviewWriteError(ValueError):
    """Raised when a canonical target review artifact cannot be written."""


@dataclass(frozen=True, slots=True)
class TargetReviewWriteReceipt:
    """Observable receipt for one target-scoped review write attempt."""

    target_key: str
    episode_id: str
    review_page_path: str
    covered_horizons_hours: tuple[float, ...]
    artifact_path: Path
    wrote_review_episode: bool
    review_decision: str
    written_horizons_hours: tuple[float, ...] = ()
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TargetCloseReviewWriteReceipt:
    """Observable receipt for one immediate close-review write attempt."""

    target_key: str
    episode_id: str
    review_page_path: str
    artifact_path: Path
    wrote_review_episode: bool
    review_decision: str
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class OpenPositionHorizonReviewWriteReceipt:
    """Observable receipt for one open-position horizon review write attempt."""

    target_key: str
    episode_id: str
    review_page_path: str
    covered_horizons_hours: tuple[float, ...]
    reviewed_horizon_hours: float
    replay_end_at: datetime
    artifact_path: Path
    wrote_review_episode: bool
    review_decision: str
    written_horizons_hours: tuple[float, ...] = ()
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class OpenPositionMaterialUpdateReviewWriteReceipt:
    """Observable receipt for one obligation-gated open-position material update."""

    target_key: str
    episode_id: str
    review_page_path: str
    replay_end_at: datetime
    artifact_path: Path
    wrote_review_episode: bool
    review_decision: str
    material_update_sequence: int
    skip_reason: str | None = None


def write_target_review(
    *,
    context: EpisodeReflectionContext,
    evaluation: TargetReflectionEvaluationResult,
    layout: WorkspaceLayout,
    page_writer: ResearchMemoryPageWritePort | None = None,
) -> TargetReviewWriteReceipt:
    """Write one canonical target review page or skip deterministically."""

    if not isinstance(context, EpisodeReflectionContext):
        raise ReflectionReviewWriteError(
            "context must be an EpisodeReflectionContext instance."
        )
    if not isinstance(evaluation, TargetReflectionEvaluationResult):
        raise ReflectionReviewWriteError(
            "evaluation must be a TargetReflectionEvaluationResult instance."
        )
    if not isinstance(layout, WorkspaceLayout):
        raise ReflectionReviewWriteError("layout must be a WorkspaceLayout instance.")
    if context.coverage != evaluation.coverage:
        raise ReflectionReviewWriteError(
            "context.coverage must match evaluation.coverage for review writing."
        )
    if evaluation.target_key != context.episode.target_key:
        raise ReflectionReviewWriteError(
            "evaluation.target_key must match context.episode.target_key."
        )
    if evaluation.episode_id != context.episode.episode_id:
        raise ReflectionReviewWriteError(
            "evaluation.episode_id must match context.episode.episode_id."
        )
    if context.episode.closed_at is None:
        raise ReflectionReviewWriteError(
            "closed episode timestamps are required for canonical review paths."
        )

    review_page_path = _review_page_path(
        target_key=context.episode.target_key,
        opened_at=context.episode.opened_at,
        closed_at=context.episode.closed_at,
    )
    artifact_path = enforce_reflection_write_surface(
        page_path=review_page_path,
        layout=layout,
    ).artifact_path
    writer = page_writer or _receipted_reflection_writer(
        layout=layout,
        actor_id=context.episode.episode_id,
        business_at=context.episode.closed_at,
    )
    _validate_page_writer(writer)

    existing_content: str | None = None
    existing_covered_horizons: tuple[float, ...] = ()
    if artifact_path.exists():
        if not artifact_path.is_file():
            raise _write_error(
                target_key=context.episode.target_key,
                episode_id=context.episode.episode_id,
                artifact_path=artifact_path,
                blocked_page_family="reviews",
                detail="Expected a review markdown file, but found a directory.",
            )
        existing_content = artifact_path.read_text(encoding="utf-8")
        existing_covered_horizons = _existing_covered_horizons(
            content_md=existing_content,
            episode_id=context.episode.episode_id,
            target_key=context.episode.target_key,
            artifact_path=artifact_path,
        )

    if evaluation.review_decision == "skip_review":
        return TargetReviewWriteReceipt(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            review_page_path=review_page_path,
            covered_horizons_hours=existing_covered_horizons,
            artifact_path=artifact_path,
            wrote_review_episode=False,
            review_decision=evaluation.review_decision,
            written_horizons_hours=(),
            skip_reason=evaluation.decision_rationale,
        )

    assessed_horizons = _assessed_horizons(evaluation)
    if set(assessed_horizons).issubset(existing_covered_horizons):
        _upsert_target_review_coverage(
            layout=layout,
            context=context,
            review_page_path=review_page_path,
            artifact_path=artifact_path,
            covered_horizons_hours=existing_covered_horizons,
        )
        return TargetReviewWriteReceipt(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            review_page_path=review_page_path,
            covered_horizons_hours=existing_covered_horizons,
            artifact_path=artifact_path,
            wrote_review_episode=False,
            review_decision=evaluation.review_decision,
            written_horizons_hours=(),
            skip_reason=(
                "All assessed horizons are already covered by the canonical review "
                "artifact."
            ),
        )

    covered_horizons_hours = tuple(sorted({*existing_covered_horizons, *assessed_horizons}))
    written_horizons_hours = tuple(
        horizon for horizon in assessed_horizons if horizon not in existing_covered_horizons
    )
    page_md = _render_review_page(
        context=context,
        evaluation=evaluation,
        covered_horizons_hours=covered_horizons_hours,
        assessed_horizons_hours=assessed_horizons,
    )

    try:
        if existing_content is None:
            writer.create_page(review_page_path, page_md)
        else:
            writer.rewrite_page(review_page_path, page_md)
    except Exception as exc:  # pragma: no cover
        raise _write_error(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            artifact_path=artifact_path,
            blocked_page_family="reviews",
            detail=str(exc),
        ) from exc
    _append_reflection_decision_records(
        layout=layout,
        target_key=context.episode.target_key,
        event_ids=tuple(record.event_id for record in context.original_evidence),
        business_at=context.episode.closed_at,
        review_page_path=review_page_path,
        review_episode_id=context.episode.episode_id,
        state_change_ids=tuple(
            state_change_id
            for state_change_id in (
                context.episode.opened_by_state_change_id,
                context.episode.closed_by_state_change_id,
            )
            if state_change_id is not None
        ),
        review_decision=evaluation.review_decision,
    )
    _upsert_target_review_coverage(
        layout=layout,
        context=context,
        review_page_path=review_page_path,
        artifact_path=artifact_path,
        covered_horizons_hours=covered_horizons_hours,
    )

    return TargetReviewWriteReceipt(
        target_key=context.episode.target_key,
        episode_id=context.episode.episode_id,
        review_page_path=review_page_path,
        covered_horizons_hours=covered_horizons_hours,
        artifact_path=artifact_path,
        wrote_review_episode=True,
        review_decision=evaluation.review_decision,
        written_horizons_hours=written_horizons_hours,
        skip_reason=None,
    )


def _append_reflection_decision_records(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_ids: tuple[str, ...],
    business_at: datetime,
    review_page_path: str,
    review_episode_id: str,
    state_change_ids: tuple[str, ...],
    review_decision: str,
) -> None:
    if not state_change_ids:
        return
    store = FileBackedDecisionEpisodeStore(layout)
    persisted_records = store.read_records(target_key=target_key)
    episode_ids = find_episode_ids_for_state_change(
        persisted_records,
        state_change_ids=state_change_ids,
    )
    recorded_at = datetime.now(UTC)
    source_record_id = f"reflection:{review_episode_id}:{review_page_path}"
    existing_keys = {
        persisted.record.natural_key
        for persisted in persisted_records
    }
    for decision_episode_id in episode_ids:
        natural_key = (decision_episode_id, "reflection", source_record_id)
        if natural_key in existing_keys:
            continue
        store.append_record(
            DecisionEpisodeRecord(
                episode_id=decision_episode_id,
                target_key=target_key,
                record_type="reflection",
                business_at=business_at,
                recorded_at=recorded_at,
                event_ids=event_ids,
                source_record_id=source_record_id,
                source_path=review_page_path,
                source_line=None,
                context_packet_id=None,
                context_packet_hash=None,
                status="reflected",
                payload={
                    "validation_view_episode_id": review_episode_id,
                    "review_page_path": review_page_path,
                    "review_decision": review_decision,
                    "linked_state_change_ids": list(state_change_ids),
                },
            )
        )


def _upsert_target_review_coverage(
    *,
    layout: WorkspaceLayout,
    context: EpisodeReflectionContext,
    review_page_path: str,
    artifact_path: Path,
    covered_horizons_hours: tuple[float, ...],
) -> None:
    closed_at = context.episode.closed_at
    if closed_at is None:
        raise ReflectionReviewWriteError(
            "target review coverage requires a closed episode timestamp."
        )
    upsert_review_coverage_record(
        layout,
        ReviewCoverageIndexRecord(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            review_kind="target_review",
            review_page_path=review_page_path,
            artifact_path=artifact_path,
            opened_at=context.episode.opened_at,
            closed_at=closed_at,
            covered_horizons_hours=covered_horizons_hours,
            recorded_at=datetime.now(UTC),
        ),
    )


def _upsert_close_review_coverage(
    *,
    layout: WorkspaceLayout,
    context: CloseEpisodeReflectionContext,
    review_page_path: str,
    artifact_path: Path,
) -> None:
    closed_at = context.episode.closed_at
    if closed_at is None:
        raise ReflectionReviewWriteError(
            "close review coverage requires a closed episode timestamp."
        )
    upsert_review_coverage_record(
        layout,
        ReviewCoverageIndexRecord(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            review_kind="episode_close",
            review_page_path=review_page_path,
            artifact_path=artifact_path,
            opened_at=context.episode.opened_at,
            closed_at=closed_at,
            recorded_at=datetime.now(UTC),
        ),
    )


def _upsert_open_position_horizon_coverage(
    *,
    layout: WorkspaceLayout,
    context: OpenPositionReflectionContext,
    review_page_path: str,
    artifact_path: Path,
    covered_horizons_hours: tuple[float, ...],
) -> None:
    upsert_review_coverage_record(
        layout,
        ReviewCoverageIndexRecord(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            review_kind="open_position_horizon",
            review_page_path=review_page_path,
            artifact_path=artifact_path,
            opened_at=context.episode.opened_at,
            replay_end_at=context.terminal_mark.replay_end_at,
            covered_horizons_hours=covered_horizons_hours,
            recorded_at=datetime.now(UTC),
        ),
    )


def _upsert_open_position_material_update_coverage(
    *,
    layout: WorkspaceLayout,
    context: OpenPositionReflectionContext,
    review_page_path: str,
    artifact_path: Path,
    material_update_sequence: int,
) -> None:
    upsert_review_coverage_record(
        layout,
        ReviewCoverageIndexRecord(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            review_kind="open_position_material_update",
            review_page_path=review_page_path,
            artifact_path=artifact_path,
            opened_at=context.episode.opened_at,
            replay_end_at=context.terminal_mark.replay_end_at,
            material_update_sequence=material_update_sequence,
            recorded_at=datetime.now(UTC),
        ),
    )


def _open_position_state_change_ids(
    context: OpenPositionReflectionContext,
) -> tuple[str, ...]:
    return (context.open_segment.opened_by_state_change_id,)


def write_target_close_review(
    *,
    context: CloseEpisodeReflectionContext,
    evaluation: TargetCloseReflectionEvaluationResult,
    layout: WorkspaceLayout,
    page_writer: ResearchMemoryPageWritePort | None = None,
) -> TargetCloseReviewWriteReceipt:
    """Write one canonical immediate close review page or skip deterministically."""

    if not isinstance(context, CloseEpisodeReflectionContext):
        raise ReflectionReviewWriteError(
            "context must be a CloseEpisodeReflectionContext instance."
        )
    if not isinstance(evaluation, TargetCloseReflectionEvaluationResult):
        raise ReflectionReviewWriteError(
            "evaluation must be a TargetCloseReflectionEvaluationResult instance."
        )
    if not isinstance(layout, WorkspaceLayout):
        raise ReflectionReviewWriteError("layout must be a WorkspaceLayout instance.")
    if evaluation.target_key != context.episode.target_key:
        raise ReflectionReviewWriteError(
            "evaluation.target_key must match context.episode.target_key."
        )
    if evaluation.episode_id != context.episode.episode_id:
        raise ReflectionReviewWriteError(
            "evaluation.episode_id must match context.episode.episode_id."
        )
    if context.episode.closed_at is None:
        raise ReflectionReviewWriteError(
            "closed episode timestamps are required for canonical close review paths."
        )

    review_page_path = _close_review_page_path(
        target_key=context.episode.target_key,
        opened_at=context.episode.opened_at,
        closed_at=context.episode.closed_at,
    )
    artifact_path = enforce_reflection_write_surface(
        page_path=review_page_path,
        layout=layout,
    ).artifact_path
    writer = page_writer or _receipted_reflection_writer(
        layout=layout,
        actor_id=context.episode.episode_id,
        business_at=context.episode.closed_at,
    )
    _validate_page_writer(writer)

    page_md = _render_close_review_page(context=context, evaluation=evaluation)
    try:
        if artifact_path.exists():
            writer.rewrite_page(review_page_path, page_md)
        else:
            writer.create_page(review_page_path, page_md)
    except Exception as exc:  # pragma: no cover
        raise _write_error(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            artifact_path=artifact_path,
            blocked_page_family="reviews",
            detail=str(exc),
        ) from exc
    if evaluation.review_decision == "write_review":
        _append_reflection_decision_records(
            layout=layout,
            target_key=context.episode.target_key,
            event_ids=tuple(record.event_id for record in context.original_evidence),
            business_at=context.episode.closed_at,
            review_page_path=review_page_path,
            review_episode_id=context.episode.episode_id,
            state_change_ids=tuple(
                state_change.state_change_id for state_change in context.state_changes
            ),
            review_decision=evaluation.review_decision,
        )
        _upsert_close_review_coverage(
            layout=layout,
            context=context,
            review_page_path=review_page_path,
            artifact_path=artifact_path,
        )

    return TargetCloseReviewWriteReceipt(
        target_key=context.episode.target_key,
        episode_id=context.episode.episode_id,
        review_page_path=review_page_path,
        artifact_path=artifact_path,
        wrote_review_episode=evaluation.review_decision == "write_review",
        review_decision=evaluation.review_decision,
        skip_reason=(
            None
            if evaluation.review_decision == "write_review"
            else evaluation.decision_rationale
        ),
    )


def write_open_position_horizon_review(
    *,
    context: OpenPositionReflectionContext,
    evaluation: OpenPositionEvaluationResult,
    review_horizon_hours: float,
    layout: WorkspaceLayout,
    page_writer: ResearchMemoryPageWritePort | None = None,
) -> OpenPositionHorizonReviewWriteReceipt:
    """Write one open-position horizon review page for an open-episode as-of mark."""

    if not isinstance(context, OpenPositionReflectionContext):
        raise ReflectionReviewWriteError(
            "context must be a OpenPositionReflectionContext instance."
        )
    if not isinstance(evaluation, OpenPositionEvaluationResult):
        raise ReflectionReviewWriteError(
            "evaluation must be a OpenPositionEvaluationResult instance."
        )
    if not isinstance(layout, WorkspaceLayout):
        raise ReflectionReviewWriteError("layout must be a WorkspaceLayout instance.")
    if context.episode.closed_at is not None:
        raise ReflectionReviewWriteError(
            "open-position horizon reviews must be written only for open episodes."
        )
    if evaluation.target_key != context.episode.target_key:
        raise ReflectionReviewWriteError(
            "evaluation.target_key must match context.episode.target_key."
        )
    if evaluation.episode_id != context.episode.episode_id:
        raise ReflectionReviewWriteError(
            "evaluation.episode_id must match context.episode.episode_id."
        )

    review_horizon_hours = float(review_horizon_hours)
    if review_horizon_hours <= 0:
        raise ReflectionReviewWriteError(
            "review_horizon_hours must be a positive number of hours."
        )
    reviewed_horizons_hours = _normalize_horizons_hours((review_horizon_hours,))

    review_page_path = _open_position_horizon_review_page_path(
        target_key=context.episode.target_key,
        opened_at=context.episode.opened_at,
        replay_end_at=context.terminal_mark.replay_end_at,
        review_horizon_hours=review_horizon_hours,
    )
    artifact_path = enforce_reflection_write_surface(
        page_path=review_page_path,
        layout=layout,
    ).artifact_path
    writer = page_writer or _receipted_reflection_writer(
        layout=layout,
        actor_id=context.episode.episode_id,
        business_at=context.terminal_mark.replay_end_at,
    )
    _validate_page_writer(writer)

    existing_content: str | None = None
    existing_covered_horizons: tuple[float, ...] = ()
    if artifact_path.exists():
        if not artifact_path.is_file():
            raise _write_error(
                target_key=context.episode.target_key,
                episode_id=context.episode.episode_id,
                artifact_path=artifact_path,
                blocked_page_family="reviews",
                detail="Expected a review markdown file, but found a directory.",
            )
        existing_content = artifact_path.read_text(encoding="utf-8")
        coverage_matches = tuple(_COVERAGE_COMMENT_RE.finditer(existing_content))
        if len(coverage_matches) > 1:
            raise ReflectionReviewWriteError(
                "Open-position horizon reviews must contain at most one "
                "reflection-coverage comment."
            )
        if len(coverage_matches) == 1:
            existing_covered_horizons = _parse_review_coverage_comment_horizons(
                coverage_comment=coverage_matches[0],
                target_key=context.episode.target_key,
                episode_id=context.episode.episode_id,
                artifact_path=artifact_path,
            )

    if evaluation.review_decision == "skip_review":
        return OpenPositionHorizonReviewWriteReceipt(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            review_page_path=review_page_path,
            covered_horizons_hours=existing_covered_horizons,
            reviewed_horizon_hours=review_horizon_hours,
            replay_end_at=context.terminal_mark.replay_end_at,
            artifact_path=artifact_path,
            wrote_review_episode=False,
            review_decision=evaluation.review_decision,
            written_horizons_hours=(),
            skip_reason=evaluation.decision_rationale,
        )

    if set(reviewed_horizons_hours).issubset(existing_covered_horizons):
        _upsert_open_position_horizon_coverage(
            layout=layout,
            context=context,
            review_page_path=review_page_path,
            artifact_path=artifact_path,
            covered_horizons_hours=existing_covered_horizons,
        )
        return OpenPositionHorizonReviewWriteReceipt(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            review_page_path=review_page_path,
            covered_horizons_hours=existing_covered_horizons,
            reviewed_horizon_hours=review_horizon_hours,
            replay_end_at=context.terminal_mark.replay_end_at,
            artifact_path=artifact_path,
            wrote_review_episode=False,
            review_decision=evaluation.review_decision,
            written_horizons_hours=(),
            skip_reason=(
                "All assessed horizons are already covered by the open-position "
                "horizon review artifact."
            ),
        )

    covered_horizons_hours = tuple(
        sorted({*existing_covered_horizons, *reviewed_horizons_hours})
    )
    written_horizons_hours = tuple(
        horizon
        for horizon in reviewed_horizons_hours
        if horizon not in existing_covered_horizons
    )
    page_md = _render_open_position_horizon_review_page(
        context=context,
        evaluation=evaluation,
        review_horizon_hours=review_horizon_hours,
        covered_horizons_hours=covered_horizons_hours,
    )
    try:
        if existing_content is None:
            writer.create_page(review_page_path, page_md)
        else:
            writer.rewrite_page(review_page_path, page_md)
    except Exception as exc:  # pragma: no cover
        raise _write_error(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            artifact_path=artifact_path,
            blocked_page_family="reviews",
            detail=str(exc),
        ) from exc
    _append_reflection_decision_records(
        layout=layout,
        target_key=context.episode.target_key,
        event_ids=tuple(record.event_id for record in context.original_evidence),
        business_at=context.terminal_mark.replay_end_at,
        review_page_path=review_page_path,
        review_episode_id=context.episode.episode_id,
        state_change_ids=_open_position_state_change_ids(context),
        review_decision=evaluation.review_decision,
    )
    _upsert_open_position_horizon_coverage(
        layout=layout,
        context=context,
        review_page_path=review_page_path,
        artifact_path=artifact_path,
        covered_horizons_hours=covered_horizons_hours,
    )

    return OpenPositionHorizonReviewWriteReceipt(
        target_key=context.episode.target_key,
        episode_id=context.episode.episode_id,
        review_page_path=review_page_path,
        covered_horizons_hours=covered_horizons_hours,
        reviewed_horizon_hours=review_horizon_hours,
        replay_end_at=context.terminal_mark.replay_end_at,
        artifact_path=artifact_path,
        wrote_review_episode=True,
        review_decision=evaluation.review_decision,
        written_horizons_hours=written_horizons_hours,
        skip_reason=None,
    )


def write_open_position_material_update_review(
    *,
    context: OpenPositionReflectionContext,
    evaluation: OpenPositionEvaluationResult,
    material_update_sequence: int,
    layout: WorkspaceLayout,
    page_writer: ResearchMemoryPageWritePort | None = None,
) -> OpenPositionMaterialUpdateReviewWriteReceipt:
    """Write one obligation-gated open-position material update review."""
    if not isinstance(context, OpenPositionReflectionContext):
        raise ReflectionReviewWriteError(
            "context must be a OpenPositionReflectionContext instance."
        )
    if context.review_kind != "open_position_material_update":
        raise ReflectionReviewWriteError(
            "material-update reviews require "
            "context.review_kind='open_position_material_update'."
        )
    if not isinstance(evaluation, OpenPositionEvaluationResult):
        raise ReflectionReviewWriteError(
            "evaluation must be a OpenPositionEvaluationResult instance."
        )
    if not isinstance(layout, WorkspaceLayout):
        raise ReflectionReviewWriteError("layout must be a WorkspaceLayout instance.")
    if (
        isinstance(material_update_sequence, bool)
        or not isinstance(material_update_sequence, int)
    ):
        raise ReflectionReviewWriteError(
            "material_update_sequence must be a positive integer."
        )
    if material_update_sequence <= 0:
        raise ReflectionReviewWriteError(
            "material_update_sequence must be a positive integer."
        )
    if context.open_position_material_update_sequence != material_update_sequence:
        raise ReflectionReviewWriteError(
            "material_update_sequence must match "
            "context.open_position_material_update_sequence."
        )
    if evaluation.target_key != context.episode.target_key:
        raise ReflectionReviewWriteError(
            "evaluation.target_key must match context.episode.target_key."
        )
    if evaluation.episode_id != context.episode.episode_id:
        raise ReflectionReviewWriteError(
            "evaluation.episode_id must match context.episode.episode_id."
        )

    review_page_path = _open_position_material_update_review_page_path(
        target_key=context.episode.target_key,
        opened_at=context.episode.opened_at,
        replay_end_at=context.terminal_mark.replay_end_at,
        material_update_sequence=material_update_sequence,
    )
    artifact_path = enforce_reflection_write_surface(
        page_path=review_page_path,
        layout=layout,
    ).artifact_path
    writer = page_writer or _receipted_reflection_writer(
        layout=layout,
        actor_id=context.episode.episode_id,
        business_at=context.terminal_mark.replay_end_at,
    )
    _validate_page_writer(writer)

    if evaluation.review_decision == "skip_review":
        return OpenPositionMaterialUpdateReviewWriteReceipt(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            review_page_path=review_page_path,
            replay_end_at=context.terminal_mark.replay_end_at,
            artifact_path=artifact_path,
            wrote_review_episode=False,
            review_decision=evaluation.review_decision,
            material_update_sequence=material_update_sequence,
            skip_reason=evaluation.decision_rationale,
        )
    page_md = _render_open_position_material_update_review_page(
        context=context,
        evaluation=evaluation,
        material_update_sequence=material_update_sequence,
    )
    try:
        if artifact_path.exists():
            writer.rewrite_page(review_page_path, page_md)
        else:
            writer.create_page(review_page_path, page_md)
    except Exception as exc:  # pragma: no cover
        raise _write_error(
            target_key=context.episode.target_key,
            episode_id=context.episode.episode_id,
            artifact_path=artifact_path,
            blocked_page_family="reviews",
            detail=str(exc),
        ) from exc
    _append_reflection_decision_records(
        layout=layout,
        target_key=context.episode.target_key,
        event_ids=tuple(record.event_id for record in context.original_evidence),
        business_at=context.terminal_mark.replay_end_at,
        review_page_path=review_page_path,
        review_episode_id=context.episode.episode_id,
        state_change_ids=_open_position_state_change_ids(context),
        review_decision=evaluation.review_decision,
    )
    _upsert_open_position_material_update_coverage(
        layout=layout,
        context=context,
        review_page_path=review_page_path,
        artifact_path=artifact_path,
        material_update_sequence=material_update_sequence,
    )

    return OpenPositionMaterialUpdateReviewWriteReceipt(
        target_key=context.episode.target_key,
        episode_id=context.episode.episode_id,
        review_page_path=review_page_path,
        replay_end_at=context.terminal_mark.replay_end_at,
        artifact_path=artifact_path,
        wrote_review_episode=True,
        review_decision=evaluation.review_decision,
        material_update_sequence=material_update_sequence,
        skip_reason=None,
    )


def _validate_page_writer(page_writer: ResearchMemoryPageWritePort) -> None:
    for method_name in ("create_page", "rewrite_page"):
        if not callable(getattr(page_writer, method_name, None)):
            raise ReflectionReviewWriteError(
                "page_writer must implement create_page(page_path, initial_content_md) "
                "and rewrite_page(page_path, new_content_md)."
            )


def _receipted_reflection_writer(
    *,
    layout: WorkspaceLayout,
    actor_id: str,
    business_at: datetime,
) -> ReceiptedResearchMemoryPageWriter:
    return ReceiptedResearchMemoryPageWriter(
        layout=layout,
        attribution=ResearchMemoryWriteAttribution(
            actor_type="reflection",
            actor_id=actor_id,
            business_at=business_at,
            committed_at=datetime.now(UTC),
        ),
    )


def _assessed_horizons(evaluation: TargetReflectionEvaluationResult) -> tuple[float, ...]:
    return tuple(sorted(assessment.horizon_hours for assessment in evaluation.assessed_horizons))


def _existing_covered_horizons(
    *,
    content_md: str,
    episode_id: str,
    target_key: str,
    artifact_path: Path,
) -> tuple[float, ...]:
    matches = tuple(_COVERAGE_COMMENT_RE.finditer(content_md))
    if len(matches) != 1:
        raise _write_error(
            target_key=target_key,
            episode_id=episode_id,
            artifact_path=artifact_path,
            blocked_page_family="reviews",
            detail=(
                "Canonical review artifacts must contain exactly one reflection-coverage "
                "record."
            ),
        )
    recorded_episode_id = matches[0].group("episode_id").strip()
    if recorded_episode_id != episode_id:
        raise _write_error(
            target_key=target_key,
            episode_id=episode_id,
            artifact_path=artifact_path,
            blocked_page_family="reviews",
            detail=(
                "Existing review coverage metadata points at a different episode_id "
                f"({recorded_episode_id!r})."
            ),
        )
    return _parse_horizons_csv(matches[0].group("horizons"))


def _context_reference_lines(
    *,
    target_log_context: PageReadResult | None = None,
    watchlist_context: PageReadResult | None = None,
) -> list[str]:
    references = (
        ("Target Log", target_log_context),
        ("Watchlist", watchlist_context),
    )
    present = tuple((label, page) for label, page in references if page is not None)
    if not present:
        return []
    lines = ["## Context References", ""]
    for label, page in present:
        content = page.content_md
        digest = sha256(content.encode("utf-8")).hexdigest()
        lines.extend(
            (
                f"### {label}",
                "",
                f"- Page: `{page.page_path}`",
                f"- Content SHA256: `{digest}`",
                f"- Characters: `{len(content)}`",
                f"- Lines: `{len(content.splitlines())}`",
                "",
            )
        )
    return lines


def _render_review_page(
    *,
    context: EpisodeReflectionContext,
    evaluation: TargetReflectionEvaluationResult,
    covered_horizons_hours: tuple[float, ...],
    assessed_horizons_hours: tuple[float, ...],
) -> str:
    closed_at = context.episode.closed_at
    if closed_at is None:
        raise ReflectionReviewWriteError(
            "closed episode timestamps are required for canonical review paths."
        )
    lines = [
        "# Review",
        "",
        _coverage_comment(
            episode_id=context.episode.episode_id,
            covered_horizons_hours=covered_horizons_hours,
        ),
        "",
        "## Review Episode",
        "",
        f"- Target Key: `{context.episode.target_key}`",
        f"- Episode ID: `{context.episode.episode_id}`",
        f"- Opened At (UTC): `{_format_timestamp(context.episode.opened_at)}`",
        f"- Closed At (UTC): `{_format_timestamp(closed_at)}`",
        f"- Covered Horizons (hours): {_format_hours_code_list(covered_horizons_hours)}",
        f"- Assessed Horizons (hours): {_format_hours_code_list(assessed_horizons_hours)}",
        f"- Review Decision: `{evaluation.review_decision}`",
        f"- Decision Rationale: {evaluation.decision_rationale}",
        "",
        "## Thesis Assessment",
        "",
        f"- Verdict: `{evaluation.thesis_assessment.verdict}`",
        "",
        evaluation.thesis_assessment.summary_md,
        "",
        "## Watchlist Assessment",
        "",
        f"- Verdict: `{evaluation.watchlist_assessment.verdict}`",
        "",
        evaluation.watchlist_assessment.summary_md,
        "",
        "## Horizon Assessments",
        "",
    ]
    for assessment in sorted(
        evaluation.assessed_horizons,
        key=lambda item: item.horizon_hours,
    ):
        lines.extend(
            (
                f"### Horizon {_format_hour_label(assessment.horizon_hours)}",
                "",
                f"- Return (%): `{_format_number(assessment.return_pct)}`",
                f"- Path Verdict: `{assessment.path_verdict}`",
                f"- Tradability Verdict: `{assessment.tradability_verdict}`",
                "",
                assessment.summary_md,
                "",
            )
        )
    lines.extend(
        _market_context_usage_evaluation_lines(
            market_context_usage=context.market_context_usage,
            evaluation=evaluation,
        )
    )
    lines.extend(
        _context_reference_lines(
            target_log_context=context.target_log_context,
            watchlist_context=context.watchlist_context,
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def _coverage_comment(
    *,
    episode_id: str,
    covered_horizons_hours: tuple[float, ...],
) -> str:
    normalized_horizons = _normalize_horizons_hours(covered_horizons_hours)
    return (
        "<!-- reflection-coverage: "
        f"episode_id={episode_id}; covered_horizons_hours="
        f"{','.join(_format_hour_value(hour) for hour in normalized_horizons)} -->"
    )


def _render_close_review_page(
    *,
    context: CloseEpisodeReflectionContext,
    evaluation: TargetCloseReflectionEvaluationResult,
) -> str:
    closed_at = context.episode.closed_at
    if closed_at is None:
        raise ReflectionReviewWriteError(
            "closed episode timestamps are required for canonical close review paths."
        )
    episode_return = context.market_returns.episode_returns[0]
    lines = [
        "# Close Review",
        "",
        "<!-- target-close-review: "
        f"episode_id={context.episode.episode_id}; review_kind=episode_close; "
        f"closed_at={_format_timestamp(closed_at)} -->",
        "",
        "## Review Episode",
        "",
        f"- Target Key: `{context.episode.target_key}`",
        f"- Episode ID: `{context.episode.episode_id}`",
        f"- Direction: `{context.episode.direction}`",
        f"- Opened At (UTC): `{_format_timestamp(context.episode.opened_at)}`",
        f"- Closed At (UTC): `{_format_timestamp(closed_at)}`",
        f"- Close Reason: `{context.episode.close_reason}`",
        f"- Review Decision: `{evaluation.review_decision}`",
        f"- Decision Rationale: {evaluation.decision_rationale}",
        "",
        "## Close Return",
        "",
        f"- Entry Bar Start: `{_format_timestamp(episode_return.entry_bar_start_at)}`",
        f"- Exit Bar Start: `{_format_timestamp(episode_return.exit_bar_start_at)}`",
        f"- Entry Price: `{_format_number(episode_return.entry_price)}`",
        f"- Exit Price: `{_format_number(episode_return.exit_price)}`",
        "- Underlying Return (%): "
        f"`{_format_number(episode_return.underlying_return * 100.0)}`",
        f"- Strategy Return (%): `{_format_number(episode_return.strategy_return * 100.0)}`",
        "",
        "## Thesis Assessment",
        "",
        f"- Verdict: `{evaluation.thesis_assessment.verdict}`",
        "",
        evaluation.thesis_assessment.summary_md,
        "",
        "## Watchlist Assessment",
        "",
        f"- Verdict: `{evaluation.watchlist_assessment.verdict}`",
        "",
        evaluation.watchlist_assessment.summary_md,
        "",
        "## Trade Assessment",
        "",
        f"- Verdict: `{evaluation.trade_assessment.verdict}`",
        f"- Return (%): `{_format_number(evaluation.trade_assessment.return_pct or 0.0)}`",
        "",
        evaluation.trade_assessment.summary_md,
        "",
        *_market_context_usage_evaluation_lines(
            market_context_usage=context.market_context_usage,
            evaluation=evaluation,
        ),
        *_context_reference_lines(
            target_log_context=context.target_log_context,
            watchlist_context=context.watchlist_context,
        ),
    ]
    return "\n".join(lines).rstrip() + "\n"


def _render_open_position_horizon_review_page(
    *,
    context: OpenPositionReflectionContext,
    evaluation: OpenPositionEvaluationResult,
    review_horizon_hours: float,
    covered_horizons_hours: tuple[float, ...],
) -> str:
    lines = [
        "# Open Position Horizon Review",
        "",
        "<!-- open-position-review: "
        f"episode_id={context.episode.episode_id}; "
        f"review_kind=open_position_horizon; "
        f"reviewed_horizon_hours={_format_hour_value(review_horizon_hours)}; "
        f"replay_end_at={_format_timestamp(context.terminal_mark.replay_end_at)} -->",
        "",
        _coverage_comment(
            episode_id=context.episode.episode_id,
            covered_horizons_hours=covered_horizons_hours,
        ),
        "",
        "## Review Episode",
        "",
        f"- Target Key: `{context.episode.target_key}`",
        f"- Episode ID: `{context.episode.episode_id}`",
        f"- Opened At (UTC): `{_format_timestamp(context.episode.opened_at)}`",
        f"- Review As-Of (UTC): `{_format_timestamp(context.terminal_mark.replay_end_at)}`",
        f"- Reviewed Horizon (hours): `{_format_hour_value(review_horizon_hours)}`",
        f"- Covered Horizons (hours): {_format_hours_code_list(covered_horizons_hours)}",
        f"- Review Decision: `{evaluation.review_decision}`",
        f"- Decision Rationale: {evaluation.decision_rationale}",
        "",
        "## Terminal Mark",
        "",
        f"- Direction: `{context.terminal_mark.direction}`",
        f"- Entry Bar Start: `{_format_timestamp(context.terminal_mark.entry_bar_start_at)}`",
        f"- Mark Bar Start: `{_format_timestamp(context.terminal_mark.mark_bar_start_at)}`",
        f"- Entry Price: `{_format_number(context.terminal_mark.entry_price)}`",
        f"- Mark Price: `{_format_number(context.terminal_mark.mark_price)}`",
        f"- Target Weight: `{_format_number(context.terminal_mark.target_weight)}`",
        "- Underlying Return (%): "
        f"`{_format_number(context.terminal_mark.underlying_return * 100.0)}`",
        f"- Strategy Return (%): `{_format_number(context.terminal_mark.strategy_return * 100.0)}`",
        "",
        "## Thesis Assessment",
        "",
        f"- Verdict: `{evaluation.thesis_assessment.verdict}`",
        "",
        evaluation.thesis_assessment.summary_md,
        "",
        "## Watchlist Assessment",
        "",
        f"- Verdict: `{evaluation.watchlist_assessment.verdict}`",
        "",
        evaluation.watchlist_assessment.summary_md,
        "",
        "## Trade Assessment",
        "",
        f"- Verdict: `{evaluation.trade_assessment.verdict}`",
        f"- Return (%): `{_format_number(evaluation.trade_assessment.return_pct or 0.0)}`",
        "",
        evaluation.trade_assessment.summary_md,
        "",
        "## Exposure Assessment",
        "",
        evaluation.exposure_assessment.summary_md,
        "",
        *_market_context_usage_evaluation_lines(
            market_context_usage=context.market_context_usage,
            evaluation=evaluation,
        ),
        *_context_reference_lines(
            target_log_context=context.target_log_context,
            watchlist_context=context.watchlist_context,
        ),
    ]
    return "\n".join(lines).rstrip() + "\n"


def _render_open_position_material_update_review_page(
    *,
    context: OpenPositionReflectionContext,
    evaluation: OpenPositionEvaluationResult,
    material_update_sequence: int,
) -> str:
    previous_open_memory_update_at = context.previous_open_memory_update_at
    if previous_open_memory_update_at is None:
        raise ReflectionReviewWriteError(
            "open-position material update pages require "
            "previous_open_memory_update_at."
        )
    lines = [
        "# Open Position Material Update Review",
        "",
        "<!-- open-position-review: "
        f"episode_id={context.episode.episode_id}; "
        "review_kind=open_position_material_update; "
        f"material_update_sequence={material_update_sequence}; "
        "previous_open_memory_update_at="
        f"{_format_timestamp(previous_open_memory_update_at)}; "
        f"replay_end_at={_format_timestamp(context.terminal_mark.replay_end_at)} -->",
        "",
        "## Review Episode",
        "",
        f"- Target Key: `{context.episode.target_key}`",
        f"- Episode ID: `{context.episode.episode_id}`",
        f"- Opened At (UTC): `{_format_timestamp(context.episode.opened_at)}`",
        "- Previous Open Memory Update At (UTC): "
        f"`{_format_timestamp(previous_open_memory_update_at)}`",
        f"- Review As-Of (UTC): `{_format_timestamp(context.terminal_mark.replay_end_at)}`",
        f"- Material Update Sequence: `{material_update_sequence}`",
        f"- Review Decision: `{evaluation.review_decision}`",
        f"- Decision Rationale: {evaluation.decision_rationale}",
        "",
        "## Terminal Mark",
        "",
        f"- Direction: `{context.terminal_mark.direction}`",
        f"- Entry Bar Start: `{_format_timestamp(context.terminal_mark.entry_bar_start_at)}`",
        f"- Mark Bar Start: `{_format_timestamp(context.terminal_mark.mark_bar_start_at)}`",
        f"- Entry Price: `{_format_number(context.terminal_mark.entry_price)}`",
        f"- Mark Price: `{_format_number(context.terminal_mark.mark_price)}`",
        f"- Target Weight: `{_format_number(context.terminal_mark.target_weight)}`",
        "- Underlying Return (%): "
        f"`{_format_number(context.terminal_mark.underlying_return * 100.0)}`",
        f"- Strategy Return (%): `{_format_number(context.terminal_mark.strategy_return * 100.0)}`",
        "",
        "## Thesis Assessment",
        "",
        f"- Verdict: `{evaluation.thesis_assessment.verdict}`",
        "",
        evaluation.thesis_assessment.summary_md,
        "",
        "## Watchlist Assessment",
        "",
        f"- Verdict: `{evaluation.watchlist_assessment.verdict}`",
        "",
        evaluation.watchlist_assessment.summary_md,
        "",
        "## Trade Assessment",
        "",
        f"- Verdict: `{evaluation.trade_assessment.verdict}`",
        f"- Return (%): `{_format_number(evaluation.trade_assessment.return_pct or 0.0)}`",
        "",
        evaluation.trade_assessment.summary_md,
        "",
        "## Exposure Assessment",
        "",
        evaluation.exposure_assessment.summary_md,
        "",
        *_market_context_usage_evaluation_lines(
            market_context_usage=context.market_context_usage,
            evaluation=evaluation,
        ),
        *_context_reference_lines(
            target_log_context=context.target_log_context,
            watchlist_context=context.watchlist_context,
        ),
    ]
    return "\n".join(lines).rstrip() + "\n"


def _parse_review_coverage_comment_horizons(
    *,
    coverage_comment: re.Match[str],
    target_key: str,
    episode_id: str,
    artifact_path: Path,
) -> tuple[float, ...]:
    recorded_episode_id = coverage_comment.group("episode_id").strip()
    if recorded_episode_id != episode_id:
        raise ReflectionReviewWriteError(
            "Open-position horizon review coverage metadata episode_id does not match "
            f"context.episode.episode_id ({episode_id!r})."
        )
    try:
        return _parse_horizons_csv(coverage_comment.group("horizons"))
    except Exception as exc:
        raise _write_error(
            target_key=target_key,
            episode_id=episode_id,
            artifact_path=artifact_path,
            blocked_page_family="reviews",
            detail=f"Failed to parse reflection-coverage horizons: {exc}",
        ) from exc


def _review_page_path(*, target_key: str, opened_at: datetime, closed_at: datetime) -> str:
    return (
        f"targets/{target_key}/reviews/"
        f"{_format_review_timestamp(opened_at)}_{_format_review_timestamp(closed_at)}.md"
    )


def _close_review_page_path(
    *,
    target_key: str,
    opened_at: datetime,
    closed_at: datetime,
) -> str:
    return (
        f"targets/{target_key}/reviews/"
        f"{_format_review_timestamp(opened_at)}_"
        f"{_format_review_timestamp(closed_at)}_close.md"
    )


def _open_position_horizon_review_page_path(
    *,
    target_key: str,
    opened_at: datetime,
    replay_end_at: datetime,
    review_horizon_hours: float,
) -> str:
    return (
        f"targets/{target_key}/reviews/"
        f"{_format_review_timestamp(opened_at)}_"
        f"{_format_review_timestamp(replay_end_at)}_"
        f"open_position_horizon_{_format_hour_label(review_horizon_hours)}.md"
    )


def _open_position_material_update_review_page_path(
    *,
    target_key: str,
    opened_at: datetime,
    replay_end_at: datetime,
    material_update_sequence: int,
) -> str:
    return (
        f"targets/{target_key}/reviews/"
        f"{_format_review_timestamp(opened_at)}_"
        f"{_format_review_timestamp(replay_end_at)}_"
        f"open_position_material_update_n{material_update_sequence}.md"
    )


def _format_review_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%y%m%dT%H%M%S")


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _format_hours_code_list(hours: tuple[float, ...]) -> str:
    return ", ".join(f"`{_format_hour_value(hour)}`" for hour in hours)


def _market_context_usage_evaluation_lines(
    *,
    market_context_usage: MarketContextUsageFacts | None,
    evaluation: _MarketContextUsageEvaluation,
) -> list[str]:
    if market_context_usage is None:
        return []

    label = evaluation.usage_quality_label or "unavailable"
    summary = (
        evaluation.usage_quality_summary
        or "No market-context usage evaluation was emitted."
    )
    error_labels = evaluation.market_context_error_labels
    return [
        "## Market Context Usage Evaluation",
        "",
        f"- Usage Quality Label: `{label}`",
        f"- Error Labels: {_format_label_code_list(error_labels)}",
        "",
        summary,
        "",
    ]


def _format_label_code_list(labels: tuple[object, ...]) -> str:
    if not labels:
        return "`none`"
    return ", ".join(f"`{label}`" for label in labels)


def _format_hour_label(hour: float) -> str:
    return f"{_format_hour_value(hour)}h"


def _format_hour_value(hour: float) -> str:
    if float(hour).is_integer():
        return str(int(hour))
    return format(hour, "g")


def _format_number(value: float) -> str:
    return format(value, "g")


def _parse_horizons_csv(value: str) -> tuple[float, ...]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        raise ReflectionReviewWriteError(
            "covered_horizons_hours metadata must include one or more horizon values."
        )
    return _normalize_horizons_hours(tuple(float(part) for part in parts))


def _normalize_horizons_hours(hours: tuple[float, ...]) -> tuple[float, ...]:
    normalized: set[float] = set()
    for hour in hours:
        value = float(hour)
        if value <= 0:
            raise ReflectionReviewWriteError(
                "covered_horizons_hours metadata must contain only positive values."
            )
        normalized.add(value)
    if not normalized:
        raise ReflectionReviewWriteError(
            "covered_horizons_hours metadata must include one or more horizon values."
        )
    return tuple(sorted(normalized))


def _write_error(
    *,
    target_key: str,
    episode_id: str,
    artifact_path: Path,
    blocked_page_family: str,
    detail: str,
) -> ReflectionReviewWriteError:
    return ReflectionReviewWriteError(
        "Failed to write canonical target review artifact: "
        f"target_key={target_key!r} "
        f"episode_id={episode_id!r} "
        f"intended_artifact_path={artifact_path.as_posix()!r} "
        f"blocked_page_family={blocked_page_family!r}. "
        f"{detail}"
    )


__all__ = [
    "ReflectionReviewWriteError",
    "OpenPositionHorizonReviewWriteReceipt",
    "OpenPositionMaterialUpdateReviewWriteReceipt",
    "TargetCloseReviewWriteReceipt",
    "TargetReviewWriteReceipt",
    "write_open_position_material_update_review",
    "write_open_position_horizon_review",
    "write_target_close_review",
    "write_target_review",
]



