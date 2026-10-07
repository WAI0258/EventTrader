"""Provider-neutral deterministic log entry for one analysis commit."""

from __future__ import annotations

from datetime import datetime

from event_trader.contracts.runtime import AnalysisResult
from event_trader.reasoning.analysis_write_receipts import (
    AnalysisWriteReceiptContractError,
    is_analysis_material_write_receipt,
    serialize_analysis_write_receipt_summary,
)
from event_trader.research_memory import FileBackedIndexLogWriter
from event_trader.storage import WorkspaceLayout


class AnalysisCommitLogError(RuntimeError):
    """Raised when the deterministic analysis commit log cannot be appended."""


def append_analysis_log_entry(
    *,
    layout: WorkspaceLayout,
    result: AnalysisResult,
    write_receipts: tuple[dict[str, object], ...],
    task_id: str,
    business_at: datetime,
    committed_at: datetime,
    why_escalated: str,
) -> None:
    """Append one idempotent target log entry for a material analysis outcome."""

    if result.outcome == "no_update":
        return

    log_path = layout.targets_root / result.target_key / "log.md"
    log_record_id = _analysis_log_record_id(task_id)
    try:
        existing_log = log_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AnalysisCommitLogError("Failed to read target analysis log page.") from exc
    if log_record_id in existing_log:
        return

    try:
        entry_md = _render_analysis_log_entry(
            result=result,
            write_receipts=write_receipts,
            task_id=task_id,
            business_at=business_at,
            committed_at=committed_at,
            why_escalated=why_escalated,
            log_record_id=log_record_id,
        )
    except AnalysisWriteReceiptContractError as exc:
        raise AnalysisCommitLogError(str(exc)) from exc
    try:
        FileBackedIndexLogWriter(layout).append_log_entry(
            f"target:{result.target_key}",
            entry_md,
        )
    except Exception as exc:
        raise AnalysisCommitLogError("Failed to append deterministic analysis log entry.") from exc


def _render_analysis_log_entry(
    *,
    result: AnalysisResult,
    write_receipts: tuple[dict[str, object], ...],
    task_id: str,
    business_at: datetime,
    committed_at: datetime,
    why_escalated: str,
    log_record_id: str,
) -> str:
    lines = [
        f"## {business_at.isoformat()} analysis {result.outcome}",
        "",
        f"- Record: `{log_record_id}`",
        f"- Task: `{task_id}`",
        f"- Committed At: `{committed_at.isoformat()}`",
        f"- Event IDs: {', '.join(f'`{event_id}`' for event_id in result.event_ids)}",
        f"- Why Escalated: {why_escalated}",
    ]
    material_writes = [
        serialize_analysis_write_receipt_summary(receipt)
        for receipt in write_receipts
        if is_analysis_material_write_receipt(receipt)
    ]
    if material_writes:
        lines.append("- Material Writes:")
        for write in material_writes:
            location = write.get("page_path") or write.get("scope_key") or "unknown"
            section_name = write.get("section_name")
            section_suffix = f" section `{section_name}`" if section_name else ""
            lines.append(
                f"  - `{write['operation']}` `{location}`{section_suffix} "
                f"sha256 `{write['content_sha256']}`"
            )
    return "\n".join(lines) + "\n"


def _analysis_log_record_id(task_id: str) -> str:
    return f"analysis-log:{task_id}"


__all__ = [
    "AnalysisCommitLogError",
    "append_analysis_log_entry",
]
