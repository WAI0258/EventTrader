"""Read-only runtime repair inspection for reflection obligation truth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.episode_memory.store import (
    EpisodeMemoryStoreError,
    FileBackedEpisodeMemoryStore,
)
from event_trader.reflection.review_coverage_index import (
    ReviewCoverageIndexError,
    ReviewCoverageIndexRecord,
    read_review_coverage_index,
)
from event_trader.reflection.trigger_store import (
    FileBackedReflectionObligationStore,
    PersistedReflectionObligation,
    PersistedReflectionObligationResolution,
    ReflectionObligationStoreError,
)
from event_trader.storage import WorkspaceLayout

ReflectionRecoveryAction = Literal["no_action", "resume", "finalize", "block"]


class ReflectionRecoveryError(ValueError):
    """Raised when reflection recovery inspection cannot prove runtime truth."""


@dataclass(frozen=True, slots=True)
class ReflectionRecoveryDecision:
    """One operator-facing reflection lifecycle recovery decision."""

    target_key: str
    obligation_id: str | None
    resolution_id: str | None
    episode_id: str | None
    trigger_kind: str | None
    due_at: datetime | None
    source_observed_through: datetime | None
    current_lifecycle_state: str
    durable_artifacts_committed: tuple[str, ...]
    missing_artifacts: tuple[str, ...]
    planned_action: ReflectionRecoveryAction
    blocker_reason: str | None
    authoritative_artifact_kind: str
    authoritative_artifact_id: str | None
    authoritative_artifact_path: Path | None
    recovery_surface: str = "runtime_repair scan-reflection-recovery"

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "obligation_id": self.obligation_id,
            "resolution_id": self.resolution_id,
            "episode_id": self.episode_id,
            "trigger_kind": self.trigger_kind,
            "due_at": None if self.due_at is None else self.due_at.isoformat(),
            "source_observed_through": (
                None
                if self.source_observed_through is None
                else self.source_observed_through.isoformat()
            ),
            "current_lifecycle_state": self.current_lifecycle_state,
            "durable_artifacts_committed": list(self.durable_artifacts_committed),
            "missing_artifacts": list(self.missing_artifacts),
            "planned_action": self.planned_action,
            "blocker_reason": self.blocker_reason,
            "authoritative_artifact_kind": self.authoritative_artifact_kind,
            "authoritative_artifact_id": self.authoritative_artifact_id,
            "authoritative_artifact_path": (
                None
                if self.authoritative_artifact_path is None
                else _relative_path(
                    self.authoritative_artifact_path,
                    workspace_root=workspace_root,
                )
            ),
            "recovery_surface": self.recovery_surface,
        }


@dataclass(frozen=True, slots=True)
class ReflectionRecoveryReport:
    """Operator-facing read-only reflection recovery report."""

    decision_count: int
    action_counts: dict[str, int]
    decisions: tuple[ReflectionRecoveryDecision, ...]

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "decision_count": self.decision_count,
            "action_counts": dict(self.action_counts),
            "decisions": [
                decision.to_json_payload(workspace_root=workspace_root)
                for decision in self.decisions
            ],
        }


@dataclass(frozen=True, slots=True)
class _ReflectionProofArtifact:
    kind: str
    artifact_id: str | None
    path: Path | None
    recorded_at: datetime | None


def plan_reflection_recovery(
    *,
    layout: WorkspaceLayout,
    target_key: str | None = None,
    as_of: datetime | None = None,
) -> ReflectionRecoveryReport:
    """Plan lifecycle-aware reflection recovery from obligations and resolutions."""

    if not isinstance(layout, WorkspaceLayout):
        raise ReflectionRecoveryError("layout must be a WorkspaceLayout instance.")
    selected_target_key = _validate_optional_target_key(target_key)
    checked_at = _normalize_as_of(as_of)
    obligation_store = FileBackedReflectionObligationStore(layout)
    episode_memory_store = FileBackedEpisodeMemoryStore(layout)

    decisions: list[ReflectionRecoveryDecision] = []
    for normalized_target_key in _reflection_recovery_target_keys(
        layout,
        selected_target_key=selected_target_key,
    ):
        try:
            obligations = obligation_store.read_obligations(target_key=normalized_target_key)
            resolutions = obligation_store.read_resolutions(target_key=normalized_target_key)
            review_records = read_review_coverage_index(
                layout,
                target_key=normalized_target_key,
            )
        except (
            EpisodeMemoryStoreError,
            ReflectionObligationStoreError,
            ReviewCoverageIndexError,
        ) as exc:
            raise ReflectionRecoveryError(str(exc)) from exc

        obligations_by_id = {
            persisted.record.obligation_id: persisted for persisted in obligations
        }
        resolutions_by_obligation = _resolutions_by_obligation(resolutions)
        reviews_by_episode = _reviews_by_episode(review_records)

        for persisted_resolution in resolutions:
            if persisted_resolution.record.obligation_id in obligations_by_id:
                continue
            decisions.append(
                ReflectionRecoveryDecision(
                    target_key=normalized_target_key,
                    obligation_id=persisted_resolution.record.obligation_id,
                    resolution_id=persisted_resolution.record.resolution_id,
                    episode_id=None,
                    trigger_kind=None,
                    due_at=None,
                    source_observed_through=None,
                    current_lifecycle_state="resolution_without_obligation",
                    durable_artifacts_committed=(
                        "reflection_obligation_resolution",
                        *_resolution_artifact_labels(
                            persisted_resolution.record.resolution_kind
                        ),
                    ),
                    missing_artifacts=("reflection_obligation",),
                    planned_action="block",
                    blocker_reason=(
                        "ReflectionObligationResolution exists without the authoritative "
                        "ReflectionObligation needed to prove reflection lineage."
                    ),
                    authoritative_artifact_kind="reflection_obligation_resolution",
                    authoritative_artifact_id=persisted_resolution.record.resolution_id,
                    authoritative_artifact_path=persisted_resolution.path,
                )
            )

        for persisted_obligation in obligations:
            try:
                decisions.append(
                    _decision_for_obligation(
                        persisted_obligation=persisted_obligation,
                        matching_resolutions=resolutions_by_obligation.get(
                            persisted_obligation.record.obligation_id,
                            (),
                        ),
                        reviews_by_episode=reviews_by_episode,
                        episode_memory_store=episode_memory_store,
                        checked_at=checked_at,
                    )
                )
            except EpisodeMemoryStoreError as exc:
                raise ReflectionRecoveryError(str(exc)) from exc

    sorted_decisions = tuple(
        sorted(
            decisions,
            key=lambda item: (
                "" if item.due_at is None else item.due_at.isoformat(),
                item.target_key,
                "" if item.obligation_id is None else item.obligation_id,
                "" if item.resolution_id is None else item.resolution_id,
                item.current_lifecycle_state,
            ),
        )
    )
    action_counts: dict[str, int] = {}
    for decision in sorted_decisions:
        action_counts[decision.planned_action] = (
            action_counts.get(decision.planned_action, 0) + 1
        )
    return ReflectionRecoveryReport(
        decision_count=len(sorted_decisions),
        action_counts=action_counts,
        decisions=sorted_decisions,
    )


def _decision_for_obligation(
    *,
    persisted_obligation: PersistedReflectionObligation,
    matching_resolutions: tuple[PersistedReflectionObligationResolution, ...],
    reviews_by_episode: dict[str, tuple[ReviewCoverageIndexRecord, ...]],
    episode_memory_store: FileBackedEpisodeMemoryStore,
    checked_at: datetime,
) -> ReflectionRecoveryDecision:
    obligation = persisted_obligation.record
    committed_artifacts = ["reflection_obligation"]

    if len(matching_resolutions) > 1:
        committed_artifacts.append("reflection_obligation_resolution")
        return ReflectionRecoveryDecision(
            target_key=obligation.target_key,
            obligation_id=obligation.obligation_id,
            resolution_id=matching_resolutions[0].record.resolution_id,
            episode_id=obligation.episode_id,
            trigger_kind=obligation.trigger_kind,
            due_at=obligation.due_at,
            source_observed_through=obligation.source_observed_through,
            current_lifecycle_state="multiple_resolutions",
            durable_artifacts_committed=tuple(committed_artifacts),
            missing_artifacts=(),
            planned_action="block",
            blocker_reason=(
                "multiple ReflectionObligationResolution rows reference the same "
                "ReflectionObligation; active reflection lineage is ambiguous."
            ),
            authoritative_artifact_kind="reflection_obligation",
            authoritative_artifact_id=obligation.obligation_id,
            authoritative_artifact_path=persisted_obligation.path,
        )

    matching_resolution = matching_resolutions[0] if matching_resolutions else None
    if matching_resolution is not None:
        committed_artifacts.append("reflection_obligation_resolution")
        committed_artifacts.extend(
            _resolution_artifact_labels(matching_resolution.record.resolution_kind)
        )
        if (
            matching_resolution.record.resolved_at
            < obligation.source_observed_through
        ):
            return ReflectionRecoveryDecision(
                target_key=obligation.target_key,
                obligation_id=obligation.obligation_id,
                resolution_id=matching_resolution.record.resolution_id,
                episode_id=obligation.episode_id,
                trigger_kind=obligation.trigger_kind,
                due_at=obligation.due_at,
                source_observed_through=obligation.source_observed_through,
                current_lifecycle_state="resolution_before_obligation_boundary",
                durable_artifacts_committed=tuple(_stable_unique(committed_artifacts)),
                missing_artifacts=(),
                planned_action="block",
                blocker_reason=(
                    "ReflectionObligationResolution resolved_at is earlier than "
                    "source_observed_through; reflection lineage is invalid."
                ),
                authoritative_artifact_kind="reflection_obligation_resolution",
                authoritative_artifact_id=matching_resolution.record.resolution_id,
                authoritative_artifact_path=matching_resolution.path,
            )
        state = (
            "cancelled"
            if obligation.status == "cancelled"
            or matching_resolution.record.resolution_kind == "cancelled"
            else "resolved"
        )
        return ReflectionRecoveryDecision(
            target_key=obligation.target_key,
            obligation_id=obligation.obligation_id,
            resolution_id=matching_resolution.record.resolution_id,
            episode_id=obligation.episode_id,
            trigger_kind=obligation.trigger_kind,
            due_at=obligation.due_at,
            source_observed_through=obligation.source_observed_through,
            current_lifecycle_state=state,
            durable_artifacts_committed=tuple(_stable_unique(committed_artifacts)),
            missing_artifacts=(),
            planned_action="no_action",
            blocker_reason=None,
            authoritative_artifact_kind="reflection_obligation_resolution",
            authoritative_artifact_id=matching_resolution.record.resolution_id,
            authoritative_artifact_path=matching_resolution.path,
        )

    if obligation.status == "cancelled":
        return ReflectionRecoveryDecision(
            target_key=obligation.target_key,
            obligation_id=obligation.obligation_id,
            resolution_id=None,
            episode_id=obligation.episode_id,
            trigger_kind=obligation.trigger_kind,
            due_at=obligation.due_at,
            source_observed_through=obligation.source_observed_through,
            current_lifecycle_state="cancelled",
            durable_artifacts_committed=("reflection_obligation",),
            missing_artifacts=(),
            planned_action="no_action",
            blocker_reason=None,
            authoritative_artifact_kind="reflection_obligation",
            authoritative_artifact_id=obligation.obligation_id,
            authoritative_artifact_path=persisted_obligation.path,
        )

    if obligation.status == "resolved":
        return ReflectionRecoveryDecision(
            target_key=obligation.target_key,
            obligation_id=obligation.obligation_id,
            resolution_id=None,
            episode_id=obligation.episode_id,
            trigger_kind=obligation.trigger_kind,
            due_at=obligation.due_at,
            source_observed_through=obligation.source_observed_through,
            current_lifecycle_state="resolved_without_resolution",
            durable_artifacts_committed=("reflection_obligation",),
            missing_artifacts=("reflection_obligation_resolution",),
            planned_action="block",
            blocker_reason=(
                "ReflectionObligation is marked resolved, but no matching "
                "ReflectionObligationResolution was persisted."
            ),
            authoritative_artifact_kind="reflection_obligation",
            authoritative_artifact_id=obligation.obligation_id,
            authoritative_artifact_path=persisted_obligation.path,
        )

    if obligation.due_at > checked_at:
        return ReflectionRecoveryDecision(
            target_key=obligation.target_key,
            obligation_id=obligation.obligation_id,
            resolution_id=None,
            episode_id=obligation.episode_id,
            trigger_kind=obligation.trigger_kind,
            due_at=obligation.due_at,
            source_observed_through=obligation.source_observed_through,
            current_lifecycle_state="pending_not_due",
            durable_artifacts_committed=("reflection_obligation",),
            missing_artifacts=(),
            planned_action="no_action",
            blocker_reason=None,
            authoritative_artifact_kind="reflection_obligation",
            authoritative_artifact_id=obligation.obligation_id,
            authoritative_artifact_path=persisted_obligation.path,
        )

    proof_artifacts = _proof_artifacts_for_obligation(
        obligation=obligation,
        reviews_by_episode=reviews_by_episode,
        episode_memory_store=episode_memory_store,
    )
    durable_artifacts = ["reflection_obligation", *(item.kind for item in proof_artifacts)]
    memory_artifacts = tuple(
        item
        for item in proof_artifacts
        if item.kind in {"episode_memory_delta", "episode_memory_no_update"}
    )
    review_artifacts = tuple(
        item for item in proof_artifacts if item.kind == "review_artifact"
    )

    if len(memory_artifacts) == 1:
        authoritative_artifact = memory_artifacts[0]
        return ReflectionRecoveryDecision(
            target_key=obligation.target_key,
            obligation_id=obligation.obligation_id,
            resolution_id=None,
            episode_id=obligation.episode_id,
            trigger_kind=obligation.trigger_kind,
            due_at=obligation.due_at,
            source_observed_through=obligation.source_observed_through,
            current_lifecycle_state="pending_due_resolution_gap",
            durable_artifacts_committed=tuple(_stable_unique(durable_artifacts)),
            missing_artifacts=("reflection_obligation_resolution",),
            planned_action="finalize",
            blocker_reason=None,
            authoritative_artifact_kind=authoritative_artifact.kind,
            authoritative_artifact_id=authoritative_artifact.artifact_id,
            authoritative_artifact_path=authoritative_artifact.path,
        )

    if len(memory_artifacts) > 1:
        authoritative_artifact = memory_artifacts[0]
        return ReflectionRecoveryDecision(
            target_key=obligation.target_key,
            obligation_id=obligation.obligation_id,
            resolution_id=None,
            episode_id=obligation.episode_id,
            trigger_kind=obligation.trigger_kind,
            due_at=obligation.due_at,
            source_observed_through=obligation.source_observed_through,
            current_lifecycle_state="pending_due_ambiguous_memory_proof",
            durable_artifacts_committed=tuple(_stable_unique(durable_artifacts)),
            missing_artifacts=("reflection_obligation_resolution",),
            planned_action="block",
            blocker_reason=(
                "multiple committed episode-memory artifacts could resolve this "
                "ReflectionObligation; deterministic linkage is ambiguous."
            ),
            authoritative_artifact_kind=authoritative_artifact.kind,
            authoritative_artifact_id=authoritative_artifact.artifact_id,
            authoritative_artifact_path=authoritative_artifact.path,
        )

    if review_artifacts:
        authoritative_artifact = review_artifacts[0]
        return ReflectionRecoveryDecision(
            target_key=obligation.target_key,
            obligation_id=obligation.obligation_id,
            resolution_id=None,
            episode_id=obligation.episode_id,
            trigger_kind=obligation.trigger_kind,
            due_at=obligation.due_at,
            source_observed_through=obligation.source_observed_through,
            current_lifecycle_state="pending_due_failed_attempt",
            durable_artifacts_committed=tuple(_stable_unique(durable_artifacts)),
            missing_artifacts=("reflection_obligation_resolution",),
            planned_action="block",
            blocker_reason=(
                "a committed reflection review artifact exists for this pending "
                "obligation, but no matching resolution was persisted; retry safety "
                "is not clear."
            ),
            authoritative_artifact_kind=authoritative_artifact.kind,
            authoritative_artifact_id=authoritative_artifact.artifact_id,
            authoritative_artifact_path=authoritative_artifact.path,
        )

    return ReflectionRecoveryDecision(
        target_key=obligation.target_key,
        obligation_id=obligation.obligation_id,
        resolution_id=None,
        episode_id=obligation.episode_id,
        trigger_kind=obligation.trigger_kind,
        due_at=obligation.due_at,
        source_observed_through=obligation.source_observed_through,
        current_lifecycle_state="pending_due",
        durable_artifacts_committed=("reflection_obligation",),
        missing_artifacts=("reflection_obligation_resolution",),
        planned_action="resume",
        blocker_reason=None,
        authoritative_artifact_kind="reflection_obligation",
        authoritative_artifact_id=obligation.obligation_id,
        authoritative_artifact_path=persisted_obligation.path,
    )


def _proof_artifacts_for_obligation(
    *,
    obligation,
    reviews_by_episode: dict[str, tuple[ReviewCoverageIndexRecord, ...]],
    episode_memory_store: FileBackedEpisodeMemoryStore,
) -> tuple[_ReflectionProofArtifact, ...]:
    if obligation.episode_id is None:
        return ()
    proof_artifacts: list[_ReflectionProofArtifact] = []

    for record in reviews_by_episode.get(obligation.episode_id, ()):
        if record.recorded_at < obligation.source_observed_through:
            continue
        proof_artifacts.append(
            _ReflectionProofArtifact(
                kind="review_artifact",
                artifact_id=record.review_page_path,
                path=record.artifact_path,
                recorded_at=record.recorded_at,
            )
        )

    for persisted in episode_memory_store.read_deltas(
        target_key=obligation.target_key,
        episode_id=obligation.episode_id,
    ):
        if persisted.record.recorded_at < obligation.source_observed_through:
            continue
        proof_artifacts.append(
            _ReflectionProofArtifact(
                kind="episode_memory_delta",
                artifact_id=persisted.record.delta_id,
                path=persisted.path,
                recorded_at=persisted.record.recorded_at,
            )
        )

    for persisted in episode_memory_store.read_no_updates(
        target_key=obligation.target_key,
        episode_id=obligation.episode_id,
    ):
        if persisted.record.recorded_at < obligation.source_observed_through:
            continue
        proof_artifacts.append(
            _ReflectionProofArtifact(
                kind="episode_memory_no_update",
                artifact_id=persisted.record.receipt_id,
                path=persisted.path,
                recorded_at=persisted.record.recorded_at,
            )
        )

    return tuple(
        sorted(
            _dedupe_proof_artifacts(proof_artifacts),
            key=lambda item: (
                "" if item.recorded_at is None else item.recorded_at.isoformat(),
                item.kind,
                "" if item.artifact_id is None else item.artifact_id,
            ),
        )
    )


def _reflection_recovery_target_keys(
    layout: WorkspaceLayout,
    *,
    selected_target_key: str | None,
) -> tuple[str, ...]:
    if selected_target_key is not None:
        return (selected_target_key,)
    if not layout.targets_root.exists():
        return ()
    target_keys = {
        validate_target_key(path.name, error_type=ReflectionRecoveryError)
        for path in layout.targets_root.iterdir()
        if path.is_dir()
    }
    return tuple(sorted(target_keys))


def _resolutions_by_obligation(
    resolutions: tuple[PersistedReflectionObligationResolution, ...],
) -> dict[str, tuple[PersistedReflectionObligationResolution, ...]]:
    grouped: dict[str, list[PersistedReflectionObligationResolution]] = {}
    for item in resolutions:
        grouped.setdefault(item.record.obligation_id, []).append(item)
    return {
        obligation_id: tuple(
            sorted(
                items,
                key=lambda item: (
                    item.record.resolved_at,
                    item.path.as_posix(),
                    item.line_number,
                ),
            )
        )
        for obligation_id, items in grouped.items()
    }


def _reviews_by_episode(
    records: tuple[ReviewCoverageIndexRecord, ...],
) -> dict[str, tuple[ReviewCoverageIndexRecord, ...]]:
    grouped: dict[str, list[ReviewCoverageIndexRecord]] = {}
    for record in records:
        grouped.setdefault(record.episode_id, []).append(record)
    return {
        episode_id: tuple(
            sorted(
                items,
                key=lambda item: (
                    item.recorded_at,
                    item.review_page_path,
                ),
            )
        )
        for episode_id, items in grouped.items()
    }


def _resolution_artifact_labels(resolution_kind: str) -> tuple[str, ...]:
    if resolution_kind == "episode_memory_delta":
        return ("episode_memory_delta",)
    if resolution_kind == "episode_memory_no_update":
        return ("episode_memory_no_update",)
    return ()


def _dedupe_proof_artifacts(
    artifacts: list[_ReflectionProofArtifact],
) -> tuple[_ReflectionProofArtifact, ...]:
    seen: set[tuple[str, str | None, str | None]] = set()
    deduped: list[_ReflectionProofArtifact] = []
    for artifact in artifacts:
        path_text = None if artifact.path is None else artifact.path.as_posix()
        key = (artifact.kind, artifact.artifact_id, path_text)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(artifact)
    return tuple(deduped)


def _normalize_as_of(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return validate_timestamp(
        value,
        field_name="as_of",
        error_type=ReflectionRecoveryError,
    ).astimezone(UTC)


def _validate_optional_target_key(value: str | None) -> str | None:
    if value is None:
        return None
    return validate_target_key(value, error_type=ReflectionRecoveryError)


def _stable_unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _relative_path(path: Path, *, workspace_root: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "ReflectionRecoveryDecision",
    "ReflectionRecoveryError",
    "ReflectionRecoveryReport",
    "plan_reflection_recovery",
]
