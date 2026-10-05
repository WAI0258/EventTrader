"""Current-state report rendering and runtime artifact writing.

This module keeps projection narrow: it turns the already-loaded current-state
render model into trader-facing markdown, then writes the derived artifact under
runtime/ without touching research-memory or ledger truth surfaces.
"""

from __future__ import annotations

import re
from pathlib import Path

from event_trader.analysis import FileBackedResearchMemoryReader
from event_trader.contracts._validators import validate_target_key
from event_trader.decision_memory import (
    FileBackedDecisionEpisodeStore,
    project_decision_episodes,
)
from event_trader.evidence_ledger import FileBackedEvidenceLedger, read_evidence
from event_trader.portfolio import PMDecisionStore, PortfolioStateStore
from event_trader.projection.current_state import (
    CurrentStateProjectionLoader,
    CurrentStateRenderer,
    CurrentStateRenderModel,
    CurrentStateRenderOutput,
    CurrentStateRenderReceipt,
)
from event_trader.storage import WorkspaceLayout

_ARTIFACT_ROOT = Path("projection/current-state")
_MAX_FAILED_DECISION_EPISODE_DETAILS = 5
_MAX_OPEN_ATTENTION_DECISION_EPISODE_DETAILS = 5
_MAX_SUPPORTING_EVIDENCE_RECORDS = 12
_SUPPORTING_EVIDENCE_EXCERPT_CHARS = 900
_PM_DECISION_RATIONALE_EXCERPT_CHARS = 500
_PAGE_LABELS: tuple[tuple[str, str], ...] = (
    ("Thesis", "thesis"),
    ("Timeline", "timeline"),
    ("Risks", "risks"),
    ("Watchlist", "watchlist"),
    ("Index", "index"),
    ("Log", "log"),
)
_SECOND_LEVEL_HEADING_RE = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


class CurrentStateReportArtifactError(ValueError):
    """Raised when report rendering or artifact writing is malformed."""


class MarkdownCurrentStateReportRenderer(CurrentStateRenderer):
    """Render one current-state report as trader-facing markdown."""

    def render(self, model: CurrentStateRenderModel) -> CurrentStateRenderOutput:
        """Render the report body from current research memory plus cited evidence."""
        if not isinstance(model, CurrentStateRenderModel):
            raise CurrentStateReportArtifactError(
                "model must be a CurrentStateRenderModel instance."
            )

        return CurrentStateRenderOutput(
            content_md=_render_report_body(model),
            receipt=model.build_receipt(),
        )


class FileBackedCurrentStateReportWriter:
    """Write projection artifacts under the canonical runtime root only."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise CurrentStateReportArtifactError(
                "layout must be a WorkspaceLayout instance."
            )
        self._layout = layout

    def write(self, output: CurrentStateRenderOutput) -> CurrentStateRenderOutput:
        """Write one rendered report artifact and attach the observable path."""
        if not isinstance(output, CurrentStateRenderOutput):
            raise CurrentStateReportArtifactError(
                "output must be a CurrentStateRenderOutput instance."
            )

        relative_artifact_path = current_state_artifact_path(output.receipt.target_key)
        absolute_artifact_path = (
            self._layout.runtime_root / relative_artifact_path
        ).resolve(strict=False)
        absolute_artifact_path.parent.mkdir(parents=True, exist_ok=True)

        receipt = CurrentStateRenderReceipt(
            target_key=output.receipt.target_key,
            source_pages=output.receipt.source_pages,
            cited_event_ids=output.receipt.cited_event_ids,
            cited_evidence_count=output.receipt.cited_evidence_count,
            artifact_path=Path("runtime") / relative_artifact_path,
            generated_at=output.receipt.generated_at,
            failure_reason=output.receipt.failure_reason,
        )
        content_md = _append_observability_metadata(output.content_md, receipt=receipt)
        absolute_artifact_path.write_text(content_md, encoding="utf-8")
        return CurrentStateRenderOutput(content_md=content_md, receipt=receipt)


def generate_current_state_report(
    *,
    target_key: str,
    layout: WorkspaceLayout,
) -> CurrentStateRenderOutput:
    """Generate one current-state report artifact through the projection surface."""
    if not isinstance(layout, WorkspaceLayout):
        raise CurrentStateReportArtifactError(
            "layout must be a WorkspaceLayout instance."
        )

    ledger = FileBackedEvidenceLedger(layout)
    projection_loader = CurrentStateProjectionLoader(
        read_evidence=lambda event_ids: read_evidence(event_ids, ledger=ledger),
        research_memory=FileBackedResearchMemoryReader(layout),
        read_decision_episodes=lambda target_key: project_decision_episodes(
            FileBackedDecisionEpisodeStore(layout).read_records(target_key=target_key)
        ),
        read_portfolio_state=lambda target_key: PortfolioStateStore(layout).read(
            target_key=target_key
        ),
        read_latest_pm_decision=lambda target_key: _latest_pm_decision(
            layout=layout,
            target_key=target_key,
        ),
    )
    renderer = MarkdownCurrentStateReportRenderer()
    writer = FileBackedCurrentStateReportWriter(layout)

    try:
        model = projection_loader.load_model(target_key)
        rendered = renderer.render(model)
        return writer.write(rendered)
    except (CurrentStateReportArtifactError, ValueError, OSError) as exc:
        raise CurrentStateReportArtifactError(
            f"Failed to generate current-state report for target_key={target_key!r}: {exc}"
        ) from exc


def current_state_artifact_path(target_key: str) -> Path:
    """Return the runtime-relative artifact path for one target report."""
    validated_target_key = validate_target_key(
        target_key,
        error_type=CurrentStateReportArtifactError,
    )
    return _ARTIFACT_ROOT / f"{validated_target_key}.md"


def _render_report_body(model: CurrentStateRenderModel) -> str:
    lines = [
        "# Current Research State",
        "",
        "## Market Setup Dashboard",
        "",
        _extract_market_setup_dashboard(model.source_pages.thesis.content_md).rstrip(),
        "",
        "## Why Now",
        "",
        _extract_named_section(
            model.source_pages.thesis.content_md,
            "Why Now",
            fallback="- No why-now rationale recorded.\n",
        ).rstrip(),
        "",
        "## Primary Risks",
        "",
        _extract_named_section(
            model.source_pages.risks.content_md,
            "Primary Risks",
            fallback="- No primary risks recorded.\n",
        ).rstrip(),
        "",
        "## Immediate Watch Items",
        "",
        _extract_named_section(
            model.source_pages.watchlist.content_md,
            "Immediate Watch Items",
            fallback="- No immediate watch items recorded.\n",
        ).rstrip(),
        "",
        "## Current Reading Path",
        "",
        _extract_named_section(
            model.source_pages.index.content_md,
            "Current Reading Path",
            fallback="- No current reading path recorded.\n",
        ).rstrip(),
        "",
        "## Latest Research Change",
        "",
        _extract_latest_log_entry(
            model.source_pages.log.content_md,
            fallback="- No meaningful research-evolution entry recorded yet.\n",
        ).rstrip(),
        "",
        "## Open Decision Episodes",
        "",
        _render_decision_episode_summaries(model).rstrip(),
        "",
        "## Portfolio / Risk State",
        "",
        _render_portfolio_risk_state(model).rstrip(),
        "",
        "## Research Memory Pages",
        "",
    ]

    for label, field_name in _PAGE_LABELS:
        page = getattr(model.source_pages, field_name)
        lines.append(f"- {label}: `{page.page_path}`")

    lines.extend(["", "## Supporting Evidence", ""])
    if not model.cited_evidence:
        lines.extend(["- No cited evidence recovered from current source pages.", ""])
        return "\n".join(lines).rstrip() + "\n"

    displayed_evidence = model.cited_evidence[:_MAX_SUPPORTING_EVIDENCE_RECORDS]
    for record in displayed_evidence:
        lines.extend(
            [
                f"### {record.title}",
                "",
                f"- Event ID: `{record.event_id}`",
                f"- Source Ref: {record.source_ref}",
                f"- Event Time: `{record.ts_event.isoformat()}`",
                f"- Labels: {', '.join(record.labels)}",
                "",
                _excerpt_text(
                    record.content,
                    max_chars=_SUPPORTING_EVIDENCE_EXCERPT_CHARS,
                ),
                "",
            ]
        )
    omitted_count = len(model.cited_evidence) - len(displayed_evidence)
    if omitted_count > 0:
        lines.append(f"- Omitted supporting evidence records: {omitted_count}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_decision_episode_summaries(model: CurrentStateRenderModel) -> str:
    if not model.decision_episodes:
        return "- No decision episodes recorded."

    status_counts: dict[str, int] = {}
    for episode in model.decision_episodes:
        status_counts[episode.status] = status_counts.get(episode.status, 0) + 1

    lines = [
        "- Status counts: "
        + ", ".join(
            f"`{status}`={count}" for status, count in sorted(status_counts.items())
        )
    ]

    failed = tuple(
        sorted(
            (
                episode
                for episode in model.decision_episodes
                if episode.status == "analysis_failed"
            ),
            key=lambda episode: episode.last_updated_at,
            reverse=True,
        )
    )
    open_attention = tuple(
        sorted(
            (
                episode
                for episode in model.decision_episodes
                if episode.status == "open_attention"
            ),
            key=lambda episode: episode.last_updated_at,
            reverse=True,
        )
    )
    open_or_stalled = (
        failed[:_MAX_FAILED_DECISION_EPISODE_DETAILS]
        + open_attention[:_MAX_OPEN_ATTENTION_DECISION_EPISODE_DETAILS]
    )
    if not open_or_stalled:
        lines.append("- No open attention or failed analysis episodes.")
        return "\n".join(lines)

    lines.append("- Actionable details:")
    for episode in open_or_stalled:
        lines.append(
            "  - "
            f"`{episode.episode_id}` "
            f"status=`{episode.status}` "
            f"last_updated_at=`{episode.last_updated_at.isoformat()}` "
            f"events={list(episode.event_ids)!r}"
        )

    omitted_failed_count = max(0, len(failed) - _MAX_FAILED_DECISION_EPISODE_DETAILS)
    omitted_open_attention_count = max(
        0,
        len(open_attention) - _MAX_OPEN_ATTENTION_DECISION_EPISODE_DETAILS,
    )
    omitted_detail_count = omitted_failed_count + omitted_open_attention_count
    if omitted_detail_count > 0:
        lines.append(f"- Omitted actionable episode details: {omitted_detail_count}")
    return "\n".join(lines)


def _render_portfolio_risk_state(model: CurrentStateRenderModel) -> str:
    state = model.portfolio_state
    latest_pm_decision = model.latest_pm_decision
    lines: list[str] = []
    if state is None:
        lines.append("- Current target weight: empty")
        lines.append("- Latest execution record id: empty")
        lines.append("- Decision episode id: empty")
    else:
        lines.append(f"- Current target weight: `{state.target_weight:.6f}`")
        lines.append(
            f"- Latest execution record id: `{state.source_execution_record_id}`"
        )
        lines.append(f"- Decision episode id: `{state.decision_episode_id}`")
    if latest_pm_decision is None:
        lines.append("- Latest PM decision id: empty")
    else:
        lines.append(f"- Latest PM decision id: `{latest_pm_decision.decision_id}`")
        lines.append(
            f"- Latest PM decision business_at: `{latest_pm_decision.business_at.isoformat()}`"
        )
        lines.append(
            "- Latest PM requested state: "
            f"`{latest_pm_decision.requested_state}` "
            f"target_weight=`{latest_pm_decision.requested_target_weight:.6f}` "
            f"execution_required=`{latest_pm_decision.execution_required}`"
        )
        lines.append(
            "- Latest PM actual state before decision: "
            f"`{latest_pm_decision.actual_state_before_decision}` "
            f"target_weight=`{latest_pm_decision.actual_target_weight_before_decision:.6f}`"
        )
        lines.append(
            "- Latest PM rationale: "
            + _excerpt_text(
                latest_pm_decision.rationale_md,
                max_chars=_PM_DECISION_RATIONALE_EXCERPT_CHARS,
            )
        )
    return "\n".join(lines)


def _latest_pm_decision(*, layout: WorkspaceLayout, target_key: str):
    decisions = PMDecisionStore(layout).read_records(target_key=target_key)
    if not decisions:
        return None
    return max(
        (item.record for item in decisions),
        key=lambda decision: (
            decision.decision_available_at,
            decision.business_at,
            decision.decision_id,
        ),
    )


def _append_observability_metadata(
    body_md: str,
    *,
    receipt: CurrentStateRenderReceipt,
) -> str:
    failure_reason = receipt.failure_reason or "None"
    source_pages = "\n".join(f"- `{page_path}`" for page_path in receipt.source_pages)
    artifact_path = (
        receipt.artifact_path.as_posix() if receipt.artifact_path is not None else ""
    )
    return (
        f"{body_md.rstrip()}\n\n"
        "## Artifact Metadata\n\n"
        f"- Target Key: `{receipt.target_key}`\n"
        f"- Artifact Path: `{artifact_path}`\n"
        f"- Generated At: `{receipt.generated_at.isoformat()}`\n"
        f"- Cited Evidence Count: {receipt.cited_evidence_count}\n"
        f"- Failure Reason: {failure_reason}\n\n"
        "## Source Pages\n\n"
        f"{source_pages}\n"
    )


def _extract_named_section(
    content_md: str,
    section_name: str,
    *,
    fallback: str,
) -> str:
    normalized = content_md.replace("\r\n", "\n").replace("\r", "\n")
    heading = f"## {section_name}"
    start = normalized.find(heading)
    if start < 0:
        return fallback

    section_start = start + len(heading)
    remaining = normalized[section_start:]
    next_heading_match = _SECOND_LEVEL_HEADING_RE.search(remaining)
    section_body = (
        remaining[: next_heading_match.start()]
        if next_heading_match is not None
        else remaining
    ).strip()
    if not section_body:
        return fallback
    return f"{section_body}\n"


def _extract_market_setup_dashboard(content_md: str) -> str:
    return _extract_named_section(
        content_md,
        "Market Setup Dashboard",
        fallback="- No market setup dashboard recorded.\n",
    )


def _extract_latest_log_entry(content_md: str, *, fallback: str) -> str:
    normalized = content_md.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_SECOND_LEVEL_HEADING_RE.finditer(normalized))
    if not matches:
        return fallback

    latest_match = matches[-1]
    next_heading_match = _SECOND_LEVEL_HEADING_RE.search(
        normalized,
        latest_match.end(),
    )
    latest_entry = (
        normalized[latest_match.start() : next_heading_match.start()]
        if next_heading_match is not None
        else normalized[latest_match.start() :]
    ).strip()
    if not latest_entry:
        return fallback
    return f"{latest_entry}\n"


def _excerpt_text(content: str, *, max_chars: int) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "..."


__all__ = [
    "CurrentStateReportArtifactError",
    "FileBackedCurrentStateReportWriter",
    "MarkdownCurrentStateReportRenderer",
    "current_state_artifact_path",
    "generate_current_state_report",
]
