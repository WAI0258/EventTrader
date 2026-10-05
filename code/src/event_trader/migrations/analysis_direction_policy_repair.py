"""Deterministic repair for persisted analysis direction-policy pollution."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from event_trader.analysis_assessment_store import (
    AnalysisAssessmentStore,
    AnalysisAssessmentStoreError,
    PersistedAnalysisAssessment,
)
from event_trader.contracts.analysis_direction_policy import (
    AnalysisDirectionPolicyNormalizationError,
    normalize_analysis_assessment_for_execution_direction_mode,
)
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.execution_direction_policy import (
    ExecutionDirectionMode,
    validate_execution_direction_mode,
)
from event_trader.contracts.price_level_role import PriceLevelRole
from event_trader.projection.report_artifacts import (
    CurrentStateReportArtifactError,
    generate_current_state_report,
)
from event_trader.research_memory.active_price_projection import (
    ActivePriceProjectionSection,
    project_active_price_sections,
    projected_market_setup_dashboard_md,
)
from event_trader.research_memory.write_receipts import (
    ReceiptedResearchMemoryPageWriter,
    ResearchMemoryWriteAttribution,
)
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.builder import ThesisRevisionBuildError, ThesisRevisionBuilder
from event_trader.thesis_revision.store import ThesisRevisionStore, ThesisRevisionStoreError

_REPAIR_ACTOR_TYPE = "operator"
_ANALYSIS_OUTCOME_ROOT = Path("analysis_outcomes")


class AnalysisDirectionPolicyRepairError(ValueError):
    """Raised when persisted direction-policy truth cannot be repaired."""


@dataclass(frozen=True, slots=True)
class AnalysisDirectionPolicyRepairReceipt:
    target_key: str
    execution_direction_mode: ExecutionDirectionMode
    assessment_count: int
    repaired_assessment_ids: tuple[str, ...]
    rewritten_paths: tuple[str, ...]
    thesis_revision_id: str | None
    current_state_artifact_path: str | None

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "execution_direction_mode": self.execution_direction_mode,
            "assessment_count": self.assessment_count,
            "repaired_assessment_ids": list(self.repaired_assessment_ids),
            "rewritten_paths": list(self.rewritten_paths),
            "thesis_revision_id": self.thesis_revision_id,
            "current_state_artifact_path": self.current_state_artifact_path,
            "workspace_root": workspace_root.as_posix(),
        }


def repair_analysis_direction_policy(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    execution_direction_mode: ExecutionDirectionMode,
) -> AnalysisDirectionPolicyRepairReceipt:
    normalized_mode = validate_execution_direction_mode(
        execution_direction_mode,
        error_type=AnalysisDirectionPolicyRepairError,
    )
    store = AnalysisAssessmentStore(layout)
    try:
        persisted = store.read_records(target_key=target_key)
    except AnalysisAssessmentStoreError as exc:
        raise AnalysisDirectionPolicyRepairError(str(exc)) from exc
    if normalized_mode != "long_only":
        return AnalysisDirectionPolicyRepairReceipt(
            target_key=target_key,
            execution_direction_mode=normalized_mode,
            assessment_count=len(persisted),
            repaired_assessment_ids=(),
            rewritten_paths=(),
            thesis_revision_id=None,
            current_state_artifact_path=None,
        )

    repaired_by_id: dict[str, AnalysisAssessment] = {}
    changed_ids: list[str] = []
    for record in persisted:
        repaired = _repair_assessment(
            assessment=record.record,
            execution_direction_mode=normalized_mode,
        )
        repaired_by_id[record.record.assessment_id] = repaired
        if repaired != record.record:
            changed_ids.append(record.record.assessment_id)

    changed_id_set = set(changed_ids)
    rewritten_paths: list[str] = []
    if changed_id_set:
        rewritten_paths.extend(
            path.as_posix()
            for path in _rewrite_assessment_files(
                persisted_records=tuple(
                    record
                    for record in persisted
                    if record.record.assessment_id in changed_id_set
                ),
                repaired_by_id=repaired_by_id,
            )
        )
        rewritten_paths.extend(
            path.as_posix()
            for path in _rewrite_analysis_outcome_files(
                layout=layout,
                target_key=target_key,
                changed_assessment_ids=changed_id_set,
                repaired_by_id=repaired_by_id,
            )
        )

    thesis_revision_id: str | None = None
    current_state_artifact_path: str | None = None
    latest_repaired = _latest_assessment(repaired_by_id.values())
    if changed_id_set and latest_repaired is not None:
        projected_sections = project_active_price_sections(latest_repaired)
        if projected_sections:
            receipts = _write_projected_sections(
                layout=layout,
                assessment=latest_repaired,
                projected_sections=projected_sections,
            )
            thesis_revision_id = _append_repair_revision(
                layout=layout,
                assessment=latest_repaired,
                receipts=receipts,
            )
            rewritten_paths.extend(section.page_path for section in projected_sections)
        try:
            current_state_report = generate_current_state_report(
                target_key=target_key,
                layout=layout,
            )
        except CurrentStateReportArtifactError as exc:
            raise AnalysisDirectionPolicyRepairError(str(exc)) from exc
        if current_state_report.receipt.artifact_path is not None:
            current_state_artifact_path = (
                current_state_report.receipt.artifact_path.as_posix()
            )
            rewritten_paths.append(current_state_artifact_path)

    return AnalysisDirectionPolicyRepairReceipt(
        target_key=target_key,
        execution_direction_mode=normalized_mode,
        assessment_count=len(persisted),
        repaired_assessment_ids=tuple(changed_ids),
        rewritten_paths=tuple(dict.fromkeys(rewritten_paths)),
        thesis_revision_id=thesis_revision_id,
        current_state_artifact_path=current_state_artifact_path,
    )


def _repair_assessment(
    *,
    assessment: AnalysisAssessment,
    execution_direction_mode: ExecutionDirectionMode,
) -> AnalysisAssessment:
    try:
        repaired = normalize_analysis_assessment_for_execution_direction_mode(
            assessment,
            execution_direction_mode=execution_direction_mode,
            error_type=AnalysisDirectionPolicyNormalizationError,
        )
    except AnalysisDirectionPolicyNormalizationError as exc:
        raise AnalysisDirectionPolicyRepairError(str(exc)) from exc
    projected_dashboard = projected_market_setup_dashboard_md(repaired)
    if (
        projected_dashboard is not None
        and projected_dashboard != repaired.market_setup_dashboard_md
    ):
        repaired = replace(repaired, market_setup_dashboard_md=projected_dashboard)
    return repaired


def _rewrite_assessment_files(
    *,
    persisted_records: tuple[PersistedAnalysisAssessment, ...],
    repaired_by_id: dict[str, AnalysisAssessment],
) -> tuple[Path, ...]:
    records_by_path: dict[Path, list[PersistedAnalysisAssessment]] = {}
    for record in persisted_records:
        records_by_path.setdefault(record.path, []).append(record)
    rewritten_paths: list[Path] = []
    for path, path_records in records_by_path.items():
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        changed = False
        for record in path_records:
            repaired = repaired_by_id[record.record.assessment_id]
            new_line = json.dumps(repaired.to_json_payload(), ensure_ascii=False)
            index = record.line_number - 1
            if index >= len(lines):
                raise AnalysisDirectionPolicyRepairError(
                    f"analysis assessment shard line is out of range: {path}:{record.line_number}"
                )
            if lines[index] != new_line:
                lines[index] = new_line
                changed = True
        if not changed:
            continue
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        rewritten_paths.append(path)
    return tuple(rewritten_paths)


def _rewrite_analysis_outcome_files(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    changed_assessment_ids: set[str],
    repaired_by_id: dict[str, AnalysisAssessment],
) -> tuple[Path, ...]:
    outcome_root = layout.runtime_root / _ANALYSIS_OUTCOME_ROOT / target_key
    if not outcome_root.exists():
        return ()
    rewritten_paths: list[Path] = []
    for path in sorted(outcome_root.glob("*.jsonl")):
        if not path.is_file():
            raise AnalysisDirectionPolicyRepairError(
                f"analysis_outcomes shard path must be a file: {path}"
            )
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        changed = False
        for index, line in enumerate(lines):
            normalized = line.strip()
            if not normalized:
                continue
            try:
                payload = json.loads(normalized)
            except json.JSONDecodeError as exc:
                raise AnalysisDirectionPolicyRepairError(
                    f"analysis outcome line {index + 1} is invalid JSON: {path}"
                ) from exc
            if not isinstance(payload, dict):
                raise AnalysisDirectionPolicyRepairError(
                    f"analysis outcome line {index + 1} must be an object: {path}"
                )
            embedded = payload.get("analysis_assessment")
            if not isinstance(embedded, dict):
                continue
            assessment_id = payload.get("analysis_assessment_id")
            if not isinstance(assessment_id, str) or assessment_id not in changed_assessment_ids:
                assessment_id = embedded.get("assessment_id")
            if not isinstance(assessment_id, str) or assessment_id not in changed_assessment_ids:
                continue
            payload["analysis_assessment"] = repaired_by_id[assessment_id].to_json_payload()
            new_line = json.dumps(payload, ensure_ascii=False)
            if new_line != lines[index]:
                lines[index] = new_line
                changed = True
        if not changed:
            continue
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        rewritten_paths.append(path)
    return tuple(rewritten_paths)


def _latest_assessment(
    assessments: object,
) -> AnalysisAssessment | None:
    items = tuple(
        assessment
        for assessment in assessments
        if isinstance(assessment, AnalysisAssessment)
    )
    if not items:
        return None
    return max(items, key=lambda assessment: (assessment.business_at, assessment.assessment_id))


def _write_projected_sections(
    *,
    layout: WorkspaceLayout,
    assessment: AnalysisAssessment,
    projected_sections: tuple[ActivePriceProjectionSection, ...],
):
    writer = ReceiptedResearchMemoryPageWriter(
        layout=layout,
        attribution=ResearchMemoryWriteAttribution(
            actor_type=_REPAIR_ACTOR_TYPE,
            actor_id=f"analysis-direction-policy-repair:{assessment.assessment_id}",
            business_at=assessment.business_at,
            committed_at=assessment.business_at,
        ),
    )
    for section in projected_sections:
        writer.update_page_section(
            section.page_path,
            section.section_name,
            section.content_md,
        )
    return writer.receipts


def _append_repair_revision(
    *,
    layout: WorkspaceLayout,
    assessment: AnalysisAssessment,
    receipts,
) -> str | None:
    if not receipts:
        return None
    try:
        revision = ThesisRevisionBuilder(
            layout=layout,
            store=ThesisRevisionStore(layout),
        ).build(
            target_key=assessment.target_key,
            business_at=assessment.business_at,
            committed_at=assessment.business_at,
            source="active_price_basis_cutover",
            analysis_assessment=assessment,
            context_packet_id=None,
            context_packet_hash=None,
            source_event_ids=assessment.source_event_ids,
            research_memory_write_receipt_ids=tuple(
                receipt.receipt_id for receipt in receipts
            ),
            committed_write_receipts=receipts,
        )
        ThesisRevisionStore(layout).append(revision)
        return revision.revision_id
    except (
        AnalysisDirectionPolicyRepairError,
        ThesisRevisionBuildError,
        ThesisRevisionStoreError,
    ):
        return None


__all__ = [
    "AnalysisDirectionPolicyRepairError",
    "AnalysisDirectionPolicyRepairReceipt",
    "repair_analysis_direction_policy",
]
