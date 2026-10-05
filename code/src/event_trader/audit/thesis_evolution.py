"""Deterministic thesis evolution projection from thesis revisions only."""

from __future__ import annotations

from pathlib import Path

from event_trader.contracts._validators import validate_target_key
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.contracts import ThesisRevision
from event_trader.thesis_revision.store import (
    ThesisRevisionStore,
    sort_persisted_thesis_revisions,
)


class ThesisEvolutionReportError(ValueError):
    """Raised when thesis evolution reporting inputs are malformed."""


def thesis_evolution_report_path(layout: WorkspaceLayout, *, target_key: str) -> Path:
    if not isinstance(layout, WorkspaceLayout):
        raise ThesisEvolutionReportError("layout must be a WorkspaceLayout instance.")
    normalized_target_key = validate_target_key(
        target_key,
        error_type=ThesisEvolutionReportError,
    )
    return (
        layout.runtime_root
        / "reports"
        / "thesis-evolution"
        / f"{normalized_target_key}.md"
    ).resolve(strict=False)


def build_thesis_evolution_markdown(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    store: ThesisRevisionStore | None = None,
) -> str:
    if not isinstance(layout, WorkspaceLayout):
        raise ThesisEvolutionReportError("layout must be a WorkspaceLayout instance.")
    normalized_target_key = validate_target_key(
        target_key,
        error_type=ThesisEvolutionReportError,
    )
    revision_store = store or ThesisRevisionStore(layout)
    revisions = _ordered_revisions(
        revision_store=revision_store,
        target_key=normalized_target_key,
    )
    return _render_markdown(normalized_target_key, revisions)


def write_thesis_evolution_report(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    store: ThesisRevisionStore | None = None,
) -> Path:
    report_path = thesis_evolution_report_path(layout, target_key=target_key)
    markdown = build_thesis_evolution_markdown(
        layout=layout,
        target_key=target_key,
        store=store,
    )
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        raise ThesisEvolutionReportError(
            f"Failed to write thesis evolution report: {exc}"
        ) from exc
    return report_path


def _render_markdown(
    target_key: str,
    revisions: tuple[ThesisRevision, ...],
) -> str:
    latest = revisions[-1] if revisions else None
    lines = [
        f"# Thesis Evolution: {target_key}",
        "",
        "## Summary",
        "",
        "- Projection Scope: `ThesisRevisionStore only`",
        "- Commit Completeness: `not implied; use analysis commit audit state for incomplete or failed commits`",
        f"- Revision Count: `{len(revisions)}`",
        f"- Latest Revision ID: `{latest.revision_id if latest is not None else 'none'}`",
        f"- Latest Business At: `{latest.business_at.isoformat() if latest is not None else 'none'}`",
        "",
        "## Revision Timeline",
        "",
    ]
    if not revisions:
        lines.append("- No thesis revisions found.")
        lines.append("")
        return "\n".join(lines)

    for revision in revisions:
        changed_sections = [
            diff.section_id
            for diff in revision.diffs_from_previous
            if diff.unified_diff_md
        ]
        lines.extend(
            (
                f"### {revision.business_at.isoformat()} / {revision.revision_id}",
                "",
                "#### Source Metadata",
                "",
                f"- Source: `{revision.source}`",
                f"- Analysis Assessment ID: `{revision.analysis_assessment_id or 'none'}`",
                f"- Context Packet ID: `{revision.context_packet_id or 'none'}`",
                f"- Context Packet Hash: `{revision.context_packet_hash or 'none'}`",
                f"- Previous Revision ID: `{revision.previous_revision_id or 'none'}`",
                f"- ResearchMemory Write Receipts: `{list(revision.research_memory_write_receipt_ids)}`",
                f"- Changed Claim IDs: `{list(revision.changed_claim_ids)}`",
                f"- Key Claim IDs: `{list(revision.key_claim_ids)}`",
                f"- Contested Prior Claim IDs: `{list(revision.contested_prior_claim_ids)}`",
                f"- Price Level Role IDs: `{list(revision.price_level_role_ids)}`",
                f"- Canonical Bundle SHA256: `{revision.canonical_bundle_sha256}`",
                "",
                "#### Changed Sections",
                "",
            )
        )
        if changed_sections:
            lines.extend(f"- `{section_id}`" for section_id in changed_sections)
        else:
            lines.append("- None")
        lines.extend(("", "#### Diff Vs Previous", ""))
        if revision.diffs_from_previous:
            for diff in revision.diffs_from_previous:
                if not diff.unified_diff_md:
                    continue
                lines.extend(
                    (
                        f"##### {diff.section_id}",
                        "",
                        "```diff",
                        diff.unified_diff_md.rstrip("\n"),
                        "```",
                        "",
                    )
                )
            if not any(diff.unified_diff_md for diff in revision.diffs_from_previous):
                lines.append("- No content changes vs previous revision.")
                lines.append("")
        else:
            lines.append("- Baseline revision; no previous revision.")
            lines.append("")
        lines.extend(("#### Canonical Bundle Snapshot", ""))
        for section in revision.sections:
            lines.extend(
                (
                    f"##### {section.section_id}",
                    "",
                    "```md",
                    section.content_md.rstrip("\n"),
                    "```",
                    "",
                )
            )
    return "\n".join(lines)


def _ordered_revisions(
    *,
    revision_store: ThesisRevisionStore,
    target_key: str,
) -> tuple[ThesisRevision, ...]:
    return tuple(
        persisted.record
        for persisted in sort_persisted_thesis_revisions(
            revision_store.read_records(target_key=target_key)
        )
    )


__all__ = [
    "ThesisEvolutionReportError",
    "build_thesis_evolution_markdown",
    "thesis_evolution_report_path",
    "write_thesis_evolution_report",
]
