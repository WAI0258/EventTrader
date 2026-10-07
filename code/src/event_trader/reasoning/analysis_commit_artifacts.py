"""Provider-neutral persistence for committed analysis artifacts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from event_trader.analysis_assessment_store import (
    AnalysisAssessmentStore,
    AnalysisAssessmentStoreError,
)
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.runtime import AnalysisResult
from event_trader.reasoning.analysis_memory_commit import (
    CommittedAnalysisMemoryWrites,
)
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.builder import (
    ThesisRevisionBuilder,
    ThesisRevisionBuildError,
)
from event_trader.thesis_revision.canonical_bundle import (
    is_canonical_thesis_bundle_write,
)
from event_trader.thesis_revision.contracts import ThesisRevision
from event_trader.thesis_revision.store import (
    ThesisRevisionStore,
    ThesisRevisionStoreError,
)


@dataclass(frozen=True, slots=True)
class PersistedAnalysisCommitArtifacts:
    thesis_revision_required: bool
    analysis_assessment_id: str | None
    analysis_assessment_path: Path | None
    thesis_revision: ThesisRevision | None
    thesis_revision_path: Path | None


class AnalysisCommitArtifactPersistenceError(RuntimeError):
    """Raised when append-only artifact persistence fails mid-commit."""

    def __init__(
        self,
        message: str,
        *,
        failure_stage: str,
        partial_artifacts: PersistedAnalysisCommitArtifacts,
    ) -> None:
        self.failure_stage = failure_stage
        self.partial_artifacts = partial_artifacts
        super().__init__(message)


class AnalysisOutcomeArtifactValidationError(RuntimeError):
    """Raised when an outcome would reference unpersisted analysis artifacts."""


class AnalysisCommitArtifactPreflightError(RuntimeError):
    """Raised before commit when an assessment cannot be appended safely."""


def preflight_analysis_assessment_persistence(
    *,
    layout: WorkspaceLayout,
    assessment: AnalysisAssessment | None,
) -> None:
    """Reject an unappendable assessment before the commit journal begins."""

    if assessment is None:
        return
    try:
        AnalysisAssessmentStore(layout).validate_appendable(assessment)
    except AnalysisAssessmentStoreError as exc:
        raise AnalysisCommitArtifactPreflightError(
            f"Analysis assessment persistence preflight failed: {exc}"
        ) from exc


def persist_analysis_commit_artifacts(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    business_at: datetime,
    committed_at: datetime,
    analysis_assessment: AnalysisAssessment | None,
    context_packet_id: str,
    context_packet_hash: str,
    source_event_ids: tuple[str, ...],
    committed_memory_writes: CommittedAnalysisMemoryWrites,
) -> PersistedAnalysisCommitArtifacts:
    """Persist the assessment and any required canonical thesis revision."""

    thesis_revision_required = analysis_commit_requires_thesis_revision(committed_memory_writes)
    artifacts = PersistedAnalysisCommitArtifacts(
        thesis_revision_required=thesis_revision_required,
        analysis_assessment_id=(
            None if analysis_assessment is None else analysis_assessment.assessment_id
        ),
        analysis_assessment_path=None,
        thesis_revision=None,
        thesis_revision_path=None,
    )
    if not thesis_revision_required:
        try:
            return replace(
                artifacts,
                analysis_assessment_path=_persist_analysis_assessment(
                    layout=layout,
                    assessment=analysis_assessment,
                ),
            )
        except AnalysisAssessmentStoreError as exc:
            raise AnalysisCommitArtifactPersistenceError(
                str(exc),
                failure_stage="analysis_assessment_persist",
                partial_artifacts=artifacts,
            ) from exc

    if analysis_assessment is None:
        raise AnalysisCommitArtifactPersistenceError(
            "Canonical thesis bundle writes require an AnalysisAssessment before commit.",
            failure_stage="analysis_assessment_required",
            partial_artifacts=artifacts,
        )
    builder = ThesisRevisionBuilder(layout=layout)
    try:
        thesis_revision = builder.build(
            target_key=target_key,
            business_at=business_at,
            committed_at=committed_at,
            source="analysis_commit",
            analysis_assessment=analysis_assessment,
            context_packet_id=context_packet_id,
            context_packet_hash=context_packet_hash,
            source_event_ids=source_event_ids,
            research_memory_write_receipt_ids=committed_memory_writes.receipt_ids,
            committed_write_receipts=committed_memory_writes.receipts,
        )
    except ThesisRevisionBuildError as exc:
        raise AnalysisCommitArtifactPersistenceError(
            "Failed to build thesis revision from canonical thesis bundle.",
            failure_stage="thesis_revision_build",
            partial_artifacts=artifacts,
        ) from exc

    artifacts = replace(artifacts, thesis_revision=thesis_revision)
    try:
        artifacts = replace(
            artifacts,
            analysis_assessment_path=_persist_analysis_assessment(
                layout=layout,
                assessment=analysis_assessment,
            ),
        )
    except AnalysisAssessmentStoreError as exc:
        raise AnalysisCommitArtifactPersistenceError(
            str(exc),
            failure_stage="analysis_assessment_persist",
            partial_artifacts=artifacts,
        ) from exc
    try:
        thesis_revision_path = ThesisRevisionStore(layout).append(thesis_revision)
    except ThesisRevisionStoreError as exc:
        raise AnalysisCommitArtifactPersistenceError(
            "Failed to append thesis revision record.",
            failure_stage="thesis_revision_persist",
            partial_artifacts=artifacts,
        ) from exc
    return replace(artifacts, thesis_revision_path=thesis_revision_path)


def validate_persisted_analysis_outcome_dependencies(
    *,
    layout: WorkspaceLayout,
    result: AnalysisResult,
    analysis_assessment_path: Path | None,
    thesis_revision: ThesisRevision | None,
    thesis_revision_path: Path | None,
) -> None:
    """Require outcome artifact references to exist in their canonical stores."""

    if result.analysis_assessment is not None:
        if analysis_assessment_path is None:
            raise AnalysisOutcomeArtifactValidationError(
                "Analysis outcome requires a persisted AnalysisAssessment path."
            )
        if not analysis_assessment_path.exists():
            raise AnalysisOutcomeArtifactValidationError(
                "Analysis outcome cannot reference a missing AnalysisAssessment path."
            )
    elif analysis_assessment_path is not None:
        raise AnalysisOutcomeArtifactValidationError(
            "Analysis outcome must not reference an AnalysisAssessment path when the "
            "result has no AnalysisAssessment."
        )
    if thesis_revision is None:
        if thesis_revision_path is not None:
            raise AnalysisOutcomeArtifactValidationError(
                "Analysis outcome must not reference a thesis revision path without a "
                "ThesisRevision."
            )
        return
    if thesis_revision_path is None:
        raise AnalysisOutcomeArtifactValidationError(
            "Analysis outcome requires a persisted ThesisRevision path."
        )
    if not thesis_revision_path.exists():
        raise AnalysisOutcomeArtifactValidationError(
            "Analysis outcome cannot reference a missing ThesisRevision path."
        )
    persisted_revisions = ThesisRevisionStore(layout).read_records(
        target_key=thesis_revision.target_key,
        year_month=thesis_revision.business_at.strftime("%Y-%m"),
    )
    normalized_revision_path = thesis_revision_path.resolve(strict=False)
    if not any(
        persisted.record.revision_id == thesis_revision.revision_id
        and persisted.path.resolve(strict=False) == normalized_revision_path
        for persisted in persisted_revisions
    ):
        raise AnalysisOutcomeArtifactValidationError(
            "Analysis outcome cannot reference a ThesisRevision that is not persisted."
        )


def _persist_analysis_assessment(
    *,
    layout: WorkspaceLayout,
    assessment: AnalysisAssessment | None,
) -> Path | None:
    if assessment is None:
        return None
    return AnalysisAssessmentStore(layout).append(assessment)


def analysis_commit_requires_thesis_revision(
    committed_memory_writes: CommittedAnalysisMemoryWrites,
) -> bool:
    """Return whether committed writes touched the canonical thesis bundle."""

    return any(
        is_canonical_thesis_bundle_write(
            receipt.page_path,
            receipt.section_name,
            receipt.operation,
        )
        for receipt in committed_memory_writes.receipts
    )


__all__ = [
    "AnalysisCommitArtifactPreflightError",
    "AnalysisCommitArtifactPersistenceError",
    "AnalysisOutcomeArtifactValidationError",
    "PersistedAnalysisCommitArtifacts",
    "analysis_commit_requires_thesis_revision",
    "persist_analysis_commit_artifacts",
    "preflight_analysis_assessment_persistence",
    "validate_persisted_analysis_outcome_dependencies",
]
