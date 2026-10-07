"""Deterministic Thesis anchor resolution for PM Console read models."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from event_trader.pm_console.schemas import PMConsoleReferenceDTO, PMConsoleStatusDTO
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.store import ThesisRevisionStore, sort_persisted_thesis_revisions


@dataclass(frozen=True, slots=True)
class ThesisAnchorResolution:
    reference: PMConsoleReferenceDTO
    revision: object | None


def resolve_thesis_anchor(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    assessment: object,
) -> ThesisAnchorResolution:
    revisions = sort_persisted_thesis_revisions(
        ThesisRevisionStore(layout).read_records(target_key=target_key)
    )
    assessment_id = getattr(assessment, "assessment_id", None)
    business_at = getattr(assessment, "business_at", None)
    direct = tuple(
        item
        for item in revisions
        if assessment_id is not None and item.record.analysis_assessment_id == assessment_id
    )
    if len(direct) > 1:
        return ThesisAnchorResolution(
            reference=PMConsoleReferenceDTO(
                reference_id=None,
                href=None,
                status=PMConsoleStatusDTO(
                    code="integrity_ambiguous_thesis_anchor",
                    explanation=(
                        "Multiple persisted Thesis revisions directly reference this "
                        "AnalysisAssessment."
                    ),
                ),
            ),
            revision=None,
        )
    selected = direct[0] if direct else None
    if selected is None and business_at is not None:
        eligible = tuple(item for item in revisions if item.record.business_at <= business_at)
        if eligible:
            selected = eligible[-1]
    if selected is None:
        return ThesisAnchorResolution(
            reference=PMConsoleReferenceDTO(
                reference_id=None,
                href=None,
                status=PMConsoleStatusDTO(
                    code="integrity_missing_thesis_anchor",
                    explanation=(
                        "No persisted Thesis revision is effective at this AnalysisAssessment."
                    ),
                ),
            ),
            revision=None,
        )
    direct_link = bool(direct)
    revision_id = selected.record.revision_id
    return ThesisAnchorResolution(
        reference=PMConsoleReferenceDTO(
            reference_id=revision_id,
            href=(
                f"/targets/{quote(target_key, safe='')}/thesis?revision="
                f"{quote(revision_id, safe='')}"
            ),
            status=PMConsoleStatusDTO(
                code="direct_thesis_anchor" if direct_link else "effective_thesis_anchor",
                explanation=(
                    "AnalysisAssessment directly committed this Thesis revision."
                    if direct_link
                    else "Thesis revision effective at the AnalysisAssessment business time."
                ),
            ),
        ),
        revision=selected,
    )


__all__ = ["ThesisAnchorResolution", "resolve_thesis_anchor"]
