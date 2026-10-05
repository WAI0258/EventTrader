"""Read-only runtime repair inspection for live analysis artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from event_trader.checker.receipts import (
    CheckerDecisionReceipt,
    CheckerReceiptStoreError,
    iter_checker_decision_receipts,
)
from event_trader.context_assembly import (
    ContextAssemblyError,
    hash_analysis_workbench_payload,
    hash_context_packet,
    parse_context_packet_payload,
)
from event_trader.contracts._validators import validate_event_id, validate_target_key
from event_trader.evidence_ledger import FileBackedEvidenceLedger, FileBackedEvidenceLedgerError
from event_trader.integrations.analysis_commit_audit import (
    AnalysisCommitAuditState,
    read_target_analysis_commit_audit_states,
)
from event_trader.storage import WorkspaceLayout

_ANALYSIS_OUTCOME_DIR_NAME = "analysis_outcomes"
_CONTEXT_PACKET_DIR_NAME = "context_packets"
_CEAU_DIR_NAME = "ceau"
_REPAIR_REASON = "checker_escalated_but_analysis_outcome_missing"


class RuntimeRepairInspectionError(ValueError):
    """Raised when runtime repair inspection cannot prove workspace truth."""


@dataclass(frozen=True, slots=True)
class LiveAnalysisRepairCandidate:
    """One checker escalation whose committed analysis outcome is missing."""

    target_key: str
    event_id: str
    task_id: str
    reason: str
    business_at: datetime
    source_ref: str
    title: str
    attention_hint: str
    requires_watchlist_maintenance: bool
    decision_episode_id: str
    checker_decision_path: Path
    checker_decision_line: int
    expected_analysis_outcome_path: Path
    analysis_lifecycle: dict[str, object]

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "event_id": self.event_id,
            "task_id": self.task_id,
            "reason": self.reason,
            "business_at": self.business_at.isoformat(),
            "source_ref": self.source_ref,
            "title": self.title,
            "attention_hint": self.attention_hint,
            "requires_watchlist_maintenance": self.requires_watchlist_maintenance,
            "decision_episode_id": self.decision_episode_id,
            "checker_decision_path": _relative_path(
                self.checker_decision_path,
                workspace_root=workspace_root,
            ),
            "checker_decision_line": self.checker_decision_line,
            "expected_analysis_outcome_path": _relative_path(
                self.expected_analysis_outcome_path,
                workspace_root=workspace_root,
            ),
            "analysis_lifecycle": self.analysis_lifecycle,
        }


AnalysisRecoveryAction = Literal[
    "no_action",
    "rollback_precommit",
    "resume",
    "finalize",
    "block",
]


@dataclass(frozen=True, slots=True)
class AnalysisRecoveryDecision:
    """One lifecycle-aware analysis recovery decision."""

    target_key: str
    event_id: str | None
    task_id: str | None
    business_at: datetime | None
    decision_episode_id: str | None
    source_ref: str | None
    title: str | None
    completion_state: str
    commit_status: str
    durable_artifacts_committed: tuple[str, ...]
    planned_action: AnalysisRecoveryAction
    blocker_reason: str | None
    research_memory_writes_preserved: bool
    rerun_allowed: bool
    analysis_lifecycle: dict[str, object]
    journal_path: Path | None = None
    journal_line_number: int | None = None
    context_packet_path: Path | None = None
    research_memory_write_receipt_ids: tuple[str, ...] = ()
    analysis_assessment_path: Path | None = None
    thesis_revision_path: Path | None = None
    analysis_outcome_path: Path | None = None
    recovery_surface: str = "runtime_repair scan-analysis-candidates"

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "event_id": self.event_id,
            "task_id": self.task_id,
            "business_at": (
                None if self.business_at is None else self.business_at.isoformat()
            ),
            "decision_episode_id": self.decision_episode_id,
            "source_ref": self.source_ref,
            "title": self.title,
            "completion_state": self.completion_state,
            "commit_status": self.commit_status,
            "durable_artifacts_committed": list(self.durable_artifacts_committed),
            "planned_action": self.planned_action,
            "blocker_reason": self.blocker_reason,
            "research_memory_writes_preserved": self.research_memory_writes_preserved,
            "rerun_allowed": self.rerun_allowed,
            "analysis_lifecycle": self.analysis_lifecycle,
            "journal_path": (
                None
                if self.journal_path is None
                else _relative_path(self.journal_path, workspace_root=workspace_root)
            ),
            "journal_line_number": self.journal_line_number,
            "context_packet_path": (
                None
                if self.context_packet_path is None
                else _relative_path(self.context_packet_path, workspace_root=workspace_root)
            ),
            "research_memory_write_receipt_ids": list(
                self.research_memory_write_receipt_ids
            ),
            "analysis_assessment_path": (
                None
                if self.analysis_assessment_path is None
                else _relative_path(
                    self.analysis_assessment_path,
                    workspace_root=workspace_root,
                )
            ),
            "thesis_revision_path": (
                None
                if self.thesis_revision_path is None
                else _relative_path(
                    self.thesis_revision_path,
                    workspace_root=workspace_root,
                )
            ),
            "analysis_outcome_path": (
                None
                if self.analysis_outcome_path is None
                else _relative_path(
                    self.analysis_outcome_path,
                    workspace_root=workspace_root,
                )
            ),
            "recovery_surface": self.recovery_surface,
        }


@dataclass(frozen=True, slots=True)
class AnalysisRecoveryReport:
    """Operator-facing read-only analysis recovery report."""

    decision_count: int
    action_counts: dict[str, int]
    decisions: tuple[AnalysisRecoveryDecision, ...]

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
class LiveAnalysisConsistencyReport:
    """Consistency audit for live analysis commit artifacts."""

    target_key: str
    issue_counts: dict[str, int]
    issues: tuple[dict[str, object], ...]

    @property
    def status(self) -> str:
        return "failed" if self.issues else "passed"

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "status": self.status,
            "issue_counts": dict(self.issue_counts),
            "issues": [
                _relative_issue_paths(issue, workspace_root=workspace_root)
                for issue in self.issues
            ],
        }


def audit_live_analysis_consistency(
    *,
    layout: WorkspaceLayout,
    target_key: str,
) -> LiveAnalysisConsistencyReport:
    """Audit live analysis context/outcome/write/CEAU consistency."""
    if not isinstance(layout, WorkspaceLayout):
        raise RuntimeRepairInspectionError("layout must be a WorkspaceLayout instance.")
    normalized_target_key = validate_target_key(
        target_key,
        error_type=RuntimeRepairInspectionError,
    )
    context_packets = _load_analysis_context_packet_rows(layout, normalized_target_key)
    issues = _analysis_consistency_issues(
        layout=layout,
        target_key=normalized_target_key,
        context_packets=context_packets,
    )
    issue_counts: dict[str, int] = {}
    for issue in issues:
        issue_type = str(issue.get("issue_type", "unknown"))
        issue_counts[issue_type] = issue_counts.get(issue_type, 0) + 1
    return LiveAnalysisConsistencyReport(
        target_key=normalized_target_key,
        issue_counts=issue_counts,
        issues=tuple(issues),
    )


def plan_analysis_recovery(
    *,
    layout: WorkspaceLayout,
    target_key: str | None = None,
    event_id: str | None = None,
) -> AnalysisRecoveryReport:
    """Plan lifecycle-aware analysis recovery from commit audit truth."""
    if not isinstance(layout, WorkspaceLayout):
        raise RuntimeRepairInspectionError("layout must be a WorkspaceLayout instance.")
    selected_target_key = _validate_optional_target_key(target_key)
    selected_event_id = _validate_optional_event_id(event_id)

    checker_receipts = _iter_checker_escalation_receipts(
        layout=layout,
        target_key=selected_target_key,
        event_id=selected_event_id,
    )
    receipts_by_key = {
        (receipt.target_key, receipt.event_id): receipt
        for receipt in checker_receipts
    }
    decisions: list[AnalysisRecoveryDecision] = []
    seen_keys: set[tuple[str, str | None, str | None]] = set()

    candidate_target_keys = (
        (selected_target_key,)
        if selected_target_key is not None
        else tuple(
            sorted(
                {
                    *{receipt.target_key for receipt in checker_receipts},
                    *_analysis_commit_target_keys(layout),
                }
            )
        )
    )
    for normalized_target_key in candidate_target_keys:
        for audit_state in read_target_analysis_commit_audit_states(
            layout=layout,
            target_key=normalized_target_key,
        ):
            resolved_event_id = _event_id_from_task_id(
                audit_state.task_id,
                target_key=normalized_target_key,
            )
            if selected_event_id is not None and resolved_event_id != selected_event_id:
                continue
            receipt = None
            if resolved_event_id is not None:
                receipt = receipts_by_key.get((normalized_target_key, resolved_event_id))
            decision = _recovery_decision_from_audit_state(
                layout=layout,
                target_key=normalized_target_key,
                audit_state=audit_state,
                checker_receipt=receipt,
            )
            decision_key = (decision.target_key, decision.event_id, decision.task_id)
            seen_keys.add(decision_key)
            decisions.append(decision)

    for receipt in checker_receipts:
        decision = _recovery_decision_from_checker_receipt(
            layout=layout,
            receipt=receipt,
        )
        if decision is None:
            continue
        decision_key = (decision.target_key, decision.event_id, decision.task_id)
        if decision_key in seen_keys:
            continue
        seen_keys.add(decision_key)
        decisions.append(decision)

    sorted_decisions = tuple(
        sorted(
            decisions,
            key=lambda item: (
                item.business_at or datetime.min.replace(tzinfo=UTC),
                item.target_key,
                item.event_id or "",
                item.task_id or "",
            ),
        )
    )
    action_counts: dict[str, int] = {}
    for decision in sorted_decisions:
        action_counts[decision.planned_action] = (
            action_counts.get(decision.planned_action, 0) + 1
        )
    return AnalysisRecoveryReport(
        decision_count=len(sorted_decisions),
        action_counts=action_counts,
        decisions=sorted_decisions,
    )


def scan_live_analysis_repair_candidates(
    *,
    layout: WorkspaceLayout,
    target_key: str | None = None,
    event_id: str | None = None,
) -> tuple[LiveAnalysisRepairCandidate, ...]:
    """Return rerun-eligible analysis repair candidates from the recovery report."""
    if not isinstance(layout, WorkspaceLayout):
        raise RuntimeRepairInspectionError("layout must be a WorkspaceLayout instance.")
    selected_target_key = _validate_optional_target_key(target_key)
    selected_event_id = _validate_optional_event_id(event_id)
    receipts = {
        (receipt.target_key, receipt.event_id): receipt
        for receipt in _iter_checker_escalation_receipts(
            layout=layout,
            target_key=selected_target_key,
            event_id=selected_event_id,
        )
    }
    report = plan_analysis_recovery(
        layout=layout,
        target_key=selected_target_key,
        event_id=selected_event_id,
    )
    candidates: list[LiveAnalysisRepairCandidate] = []
    for decision in report.decisions:
        if not decision.rerun_allowed or decision.planned_action != "rollback_precommit":
            continue
        if decision.event_id is None or decision.task_id is None or decision.business_at is None:
            continue
        receipt = receipts.get((decision.target_key, decision.event_id))
        if receipt is None:
            continue
        candidates.append(
            LiveAnalysisRepairCandidate(
                target_key=decision.target_key,
                event_id=decision.event_id,
                task_id=decision.task_id,
                reason=_REPAIR_REASON,
                business_at=decision.business_at,
                source_ref=decision.source_ref or receipt.source_ref,
                title=decision.title or "",
                attention_hint=receipt.attention_hint,
                requires_watchlist_maintenance=receipt.requires_watchlist_maintenance,
                decision_episode_id=receipt.decision_episode_id,
                checker_decision_path=receipt.path,
                checker_decision_line=receipt.line_number,
                expected_analysis_outcome_path=_analysis_outcome_path(
                    layout,
                    target_key=receipt.target_key,
                    business_at=receipt.business_at,
                ),
                analysis_lifecycle=decision.analysis_lifecycle,
            )
        )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.business_at,
                item.target_key,
                item.event_id,
            ),
        )
    )


def _recovery_decision_from_checker_receipt(
    *,
    layout: WorkspaceLayout,
    receipt: CheckerDecisionReceipt,
) -> AnalysisRecoveryDecision | None:
    outcome_keys = _analysis_outcome_keys(layout, receipt.target_key)
    task_id = _analysis_task_id(receipt.target_key, receipt.event_id)
    if (task_id, (receipt.event_id,)) in outcome_keys:
        return None
    ledger_record = _read_checker_ledger_record(layout=layout, receipt=receipt)
    return AnalysisRecoveryDecision(
        target_key=receipt.target_key,
        event_id=receipt.event_id,
        task_id=task_id,
        business_at=receipt.business_at,
        decision_episode_id=receipt.decision_episode_id or None,
        source_ref=ledger_record.source_ref,
        title=ledger_record.title,
        completion_state="incomplete_before_context_packet_persistence",
        commit_status="not_started",
        durable_artifacts_committed=(),
        planned_action="rollback_precommit",
        blocker_reason=None,
        research_memory_writes_preserved=False,
        rerun_allowed=True,
        analysis_lifecycle=_analysis_lifecycle_diagnostics(
            layout=layout,
            target_key=receipt.target_key,
            event_id=receipt.event_id,
        ),
    )


def _recovery_decision_from_audit_state(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    audit_state: AnalysisCommitAuditState,
    checker_receipt: CheckerDecisionReceipt | None,
) -> AnalysisRecoveryDecision:
    event_id = _event_id_from_task_id(audit_state.task_id, target_key=target_key)
    durable_artifacts = _durable_artifacts(audit_state)
    research_memory_writes_preserved = audit_state.research_memory_writes_committed
    analysis_lifecycle = (
        {}
        if event_id is None
        else _analysis_lifecycle_diagnostics(
            layout=layout,
            target_key=target_key,
            event_id=event_id,
        )
    )
    source_ref = None if checker_receipt is None else checker_receipt.source_ref
    title = None
    if checker_receipt is not None:
        title = _read_checker_ledger_record(layout=layout, receipt=checker_receipt).title

    planned_action: AnalysisRecoveryAction
    blocker_reason: str | None = None
    rerun_allowed = False
    if audit_state.status == "complete":
        planned_action = "no_action"
    elif audit_state.completion_state == "incomplete_before_context_packet_persistence":
        if durable_artifacts:
            planned_action = "block"
            blocker_reason = (
                "analysis audit reports a precommit failure, but durable analysis truth "
                "was also recorded; manual review is required before any rerun."
            )
        else:
            planned_action = "rollback_precommit"
            rerun_allowed = True
    elif audit_state.completion_state == "incomplete_after_context_packet_persistence":
        if _path_missing(audit_state.context_packet_path):
            planned_action = "block"
            blocker_reason = (
                "analysis context packet was recorded in the audit trail, but the "
                "persisted context packet path is missing."
            )
        else:
            planned_action = "resume"
    elif audit_state.completion_state == "incomplete_after_memory_writes":
        if not audit_state.research_memory_write_receipt_ids:
            planned_action = "block"
            blocker_reason = (
                "analysis commit recorded committed research-memory writes, but no "
                "write receipt ids were persisted to prove ownership."
            )
        else:
            planned_action = "block"
            blocker_reason = (
                "committed research-memory writes exist; preserve them and use "
                "analysis recovery rather than generic deletion."
            )
    elif audit_state.completion_state == "incomplete_after_analysis_assessment":
        if _path_missing(audit_state.analysis_assessment_path):
            planned_action = "block"
            blocker_reason = (
                "analysis assessment was recorded in the audit trail, but the "
                "persisted assessment path is missing."
            )
        else:
            planned_action = "block"
            blocker_reason = (
                "analysis assessment is durable truth, but the missing downstream "
                "analysis outcome cannot be proven from the audit state alone."
            )
    elif audit_state.completion_state == "incomplete_after_thesis_revision":
        if _path_missing(audit_state.thesis_revision_path):
            planned_action = "block"
            blocker_reason = (
                "thesis revision was recorded in the audit trail, but the "
                "persisted thesis revision path is missing."
            )
        else:
            planned_action = "block"
            blocker_reason = (
                "thesis revision is durable truth, but the missing downstream "
                "analysis outcome cannot be proven from the persisted revision alone."
            )
    elif audit_state.completion_state == "incomplete_after_outcome":
        if _path_missing(audit_state.analysis_outcome_path):
            planned_action = "block"
            blocker_reason = (
                "analysis outcome was recorded in the audit trail, but the persisted "
                "outcome path is missing."
            )
        else:
            planned_action = "finalize"
    else:
        planned_action = "block"
        blocker_reason = (
            "analysis recovery encountered an unmapped completion state and requires "
            "manual review."
        )

    return AnalysisRecoveryDecision(
        target_key=target_key,
        event_id=event_id,
        task_id=audit_state.task_id,
        business_at=audit_state.business_at,
        decision_episode_id=(
            None if checker_receipt is None else checker_receipt.decision_episode_id or None
        ),
        source_ref=source_ref,
        title=title,
        completion_state=audit_state.completion_state,
        commit_status=audit_state.status,
        durable_artifacts_committed=durable_artifacts,
        planned_action=planned_action,
        blocker_reason=blocker_reason,
        research_memory_writes_preserved=research_memory_writes_preserved,
        rerun_allowed=rerun_allowed,
        analysis_lifecycle=analysis_lifecycle,
        journal_path=audit_state.terminal_journal_path,
        journal_line_number=audit_state.terminal_journal_line_number,
        context_packet_path=audit_state.context_packet_path,
        research_memory_write_receipt_ids=audit_state.research_memory_write_receipt_ids,
        analysis_assessment_path=audit_state.analysis_assessment_path,
        thesis_revision_path=audit_state.thesis_revision_path,
        analysis_outcome_path=audit_state.analysis_outcome_path,
    )


def _durable_artifacts(audit_state: AnalysisCommitAuditState) -> tuple[str, ...]:
    artifacts: list[str] = []
    if audit_state.context_packet_path is not None:
        artifacts.append("context_packet")
    if audit_state.research_memory_writes_committed:
        artifacts.append("research_memory_writes")
    if audit_state.analysis_assessment_path is not None:
        artifacts.append("analysis_assessment")
    if audit_state.thesis_revision_path is not None:
        artifacts.append("thesis_revision")
    if audit_state.analysis_outcome_path is not None:
        artifacts.append("analysis_outcome")
    return tuple(artifacts)


def _path_missing(path: Path | None) -> bool:
    return path is None or not path.exists()


def _analysis_commit_target_keys(layout: WorkspaceLayout) -> tuple[str, ...]:
    root = layout.runtime_root / "analysis_commits"
    if not root.exists():
        return ()
    return tuple(
        sorted(path.name for path in root.iterdir() if path.is_dir() and path.name.strip())
    )


def _event_id_from_task_id(task_id: str, *, target_key: str) -> str | None:
    prefix = f"event-trader-analysis-{target_key}-"
    if not task_id.startswith(prefix):
        return None
    value = task_id.removeprefix(prefix).strip()
    if not value:
        return None
    return validate_event_id(value, error_type=RuntimeRepairInspectionError)


def _read_checker_ledger_record(
    *,
    layout: WorkspaceLayout,
    receipt: CheckerDecisionReceipt,
):
    ledger = FileBackedEvidenceLedger(layout)
    try:
        ledger_record = ledger.read(receipt.event_id)
    except FileBackedEvidenceLedgerError as exc:
        raise RuntimeRepairInspectionError(
            "Cannot plan analysis recovery because ledger truth is missing for "
            f"event_id {receipt.event_id}."
        ) from exc
    _validate_receipt_matches_ledger(receipt, ledger_record)
    return ledger_record


def _iter_checker_escalation_receipts(
    *,
    layout: WorkspaceLayout,
    target_key: str | None,
    event_id: str | None,
) -> tuple[CheckerDecisionReceipt, ...]:
    try:
        return iter_checker_decision_receipts(
            layout=layout,
            target_key=target_key,
            event_id=event_id,
            decision="escalate",
            caused_analysis_request=True,
        )
    except CheckerReceiptStoreError as exc:
        raise RuntimeRepairInspectionError(str(exc)) from exc


def _analysis_outcome_keys(
    layout: WorkspaceLayout,
    target_key: str,
) -> set[tuple[str, tuple[str, ...]]]:
    outcome_root = layout.runtime_root / _ANALYSIS_OUTCOME_DIR_NAME / target_key
    if not outcome_root.exists():
        return set()
    if not outcome_root.is_dir():
        raise RuntimeRepairInspectionError(
            f"analysis_outcomes target path must be a directory: {outcome_root}"
        )
    outcome_keys: set[tuple[str, tuple[str, ...]]] = set()
    for shard_path in sorted(outcome_root.glob("*.jsonl")):
        with shard_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                payload = _load_json_object(
                    normalized,
                    path=shard_path,
                    line_number=line_number,
                )
                task_id = payload.get("task_id")
                event_ids = payload.get("event_ids")
                record_id = payload.get("record_id")
                resolved_task_id = _resolve_outcome_task_id(
                    task_id,
                    record_id,
                    path=shard_path,
                )
                if resolved_task_id is None:
                    continue
                if not isinstance(event_ids, list) or not all(
                    isinstance(event_id, str) and event_id.strip()
                    for event_id in event_ids
                ):
                    raise RuntimeRepairInspectionError(
                        f"analysis outcome event_ids must be a string list: {shard_path}"
                    )
                outcome_keys.add(
                    (
                        resolved_task_id,
                        tuple(event_id.strip() for event_id in event_ids),
                    )
                )
    return outcome_keys


def _resolve_outcome_task_id(
    task_id: object,
    record_id: object,
    *,
    path: Path,
) -> str | None:
    resolved_from_task_id = task_id.strip() if isinstance(task_id, str) else ""
    resolved_from_record_id = ""
    if isinstance(record_id, str) and record_id.startswith("analysis-outcome:"):
        resolved_from_record_id = record_id.removeprefix("analysis-outcome:").strip()
    if resolved_from_task_id and resolved_from_record_id:
        if resolved_from_task_id != resolved_from_record_id:
            raise RuntimeRepairInspectionError(
                f"analysis outcome task_id and record_id disagree: {path}"
            )
        return resolved_from_task_id
    return resolved_from_task_id or resolved_from_record_id or None


def _validate_receipt_matches_ledger(
    receipt: CheckerDecisionReceipt,
    ledger_record,
) -> None:
    if ledger_record.target_key != receipt.target_key:
        raise RuntimeRepairInspectionError(
            "checker receipt target_key does not match ledger record for event_id "
            f"{receipt.event_id}."
        )
    if ledger_record.source_ref != receipt.source_ref:
        raise RuntimeRepairInspectionError(
            "checker receipt source_ref does not match ledger record for event_id "
            f"{receipt.event_id}."
        )
    if ledger_record.ts_event != receipt.business_at:
        raise RuntimeRepairInspectionError(
            "checker receipt business_at does not match ledger ts_event for event_id "
            f"{receipt.event_id}."
        )


def _analysis_task_id(target_key: str, event_id: str) -> str:
    return f"event-trader-analysis-{target_key}-{event_id}"


def _analysis_outcome_path(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    business_at: datetime,
) -> Path:
    return (
        layout.runtime_root
        / _ANALYSIS_OUTCOME_DIR_NAME
        / target_key
        / f"{business_at.strftime('%Y-%m')}.jsonl"
    ).resolve(strict=False)


def _analysis_lifecycle_diagnostics(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_id: str,
) -> dict[str, object]:
    records = _load_ceau_records(layout=layout, target_key=target_key)
    analysis_unit_ids: set[str] = set()
    for payload in records:
        if (
            payload.get("record_type") == "event_route"
            and payload.get("event_id") == event_id
        ):
            unit_id = _optional_text(payload, "analysis_unit_id")
            if unit_id:
                analysis_unit_ids.add(unit_id)
        if payload.get("record_type") == "unit_emitted":
            event_ids = payload.get("event_ids")
            if isinstance(event_ids, list) and event_id in event_ids:
                unit_id = _optional_text(payload, "analysis_unit_id")
                if unit_id:
                    analysis_unit_ids.add(unit_id)
    lifecycle_records = [
        payload
        for payload in records
        if _optional_text(payload, "analysis_unit_id") in analysis_unit_ids
    ]
    queued_at = _latest_recorded_at(lifecycle_records, "analysis_queued")
    started_at = _latest_recorded_at(lifecycle_records, "analysis_started")
    completed_at = _latest_recorded_at(lifecycle_records, "analysis_completed")
    failed_at = _latest_recorded_at(lifecycle_records, "analysis_failed")
    terminal_record_type = None
    terminal_at = None
    if completed_at is not None and (failed_at is None or completed_at >= failed_at):
        terminal_record_type = "analysis_completed"
        terminal_at = completed_at
    elif failed_at is not None:
        terminal_record_type = "analysis_failed"
        terminal_at = failed_at
    started_without_terminal = started_at is not None and terminal_at is None
    age_seconds = None
    if started_without_terminal and started_at is not None:
        age_seconds = int((datetime.now(UTC) - started_at).total_seconds())
    return {
        "analysis_unit_ids": sorted(analysis_unit_ids),
        "analysis_queued_at": queued_at.isoformat() if queued_at is not None else None,
        "analysis_started_at": started_at.isoformat() if started_at is not None else None,
        "terminal_record_type": terminal_record_type,
        "terminal_recorded_at": terminal_at.isoformat() if terminal_at is not None else None,
        "started_without_terminal": started_without_terminal,
        "seconds_since_analysis_started": age_seconds,
    }


def _load_analysis_context_packet_rows(
    layout: WorkspaceLayout,
    target_key: str,
) -> dict[str, dict[str, object]]:
    context_root = (
        layout.runtime_root
        / _CONTEXT_PACKET_DIR_NAME
        / "live"
        / "analysis"
        / target_key
    )
    rows: dict[str, dict[str, object]] = {}
    if not context_root.exists():
        return rows
    if not context_root.is_dir():
        raise RuntimeRepairInspectionError(
            f"analysis context packet target path must be a directory: {context_root}"
        )
    for shard_path in sorted(context_root.glob("*.jsonl")):
        for line_number, payload, raw_line in _iter_jsonl_objects(shard_path):
            packet_id = _optional_text(payload, "packet_id")
            if not packet_id:
                rows[f"{shard_path}:{line_number}"] = {
                    "path": shard_path,
                    "line_number": line_number,
                    "payload": payload,
                    "raw_line": raw_line,
                    "packet_id": None,
                    "packet_hash": None,
                    "actual_packet_hash": None,
                    "status": "missing_packet_id",
                }
                continue
            status = "valid"
            actual_packet_hash = None
            actual_workbench_hash = None
            try:
                packet = parse_context_packet_payload(payload)
                actual_packet_hash = hash_context_packet(packet)
                if packet.packet_hash != actual_packet_hash:
                    status = "hash_mismatch"
                if packet.analysis_workbench is not None:
                    actual_workbench_hash = hash_analysis_workbench_payload(
                        packet.analysis_workbench
                    )
            except (ContextAssemblyError, ValueError):
                status = "corrupt"
                patched_payload = _payload_without_context_hashes(payload)
                try:
                    packet = parse_context_packet_payload(patched_payload)
                    actual_packet_hash = hash_context_packet(packet)
                    if packet.analysis_workbench is not None:
                        actual_workbench_hash = hash_analysis_workbench_payload(
                            packet.analysis_workbench
                        )
                except (ContextAssemblyError, ValueError):
                    pass
            rows[packet_id] = {
                "path": shard_path,
                "line_number": line_number,
                "payload": payload,
                "raw_line": raw_line,
                "packet_id": packet_id,
                "packet_hash": payload.get("packet_hash"),
                "actual_packet_hash": actual_packet_hash,
                "workbench_hash": (
                    payload.get("analysis_workbench", {}).get(
                        "analysis_workbench_payload_hash"
                    )
                    if isinstance(payload.get("analysis_workbench"), dict)
                    else None
                ),
                "actual_workbench_hash": actual_workbench_hash,
                "status": status,
            }
    return rows


def _analysis_consistency_issues(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    context_packets: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    for packet_id, row in sorted(context_packets.items()):
        if row["status"] == "valid":
            continue
        issues.append(
            {
                "issue_type": "context_packet_corrupt",
                "packet_id": packet_id,
                "path": str(row["path"]),
                "line_number": row["line_number"],
                "stored_packet_hash": row["packet_hash"],
                "actual_packet_hash": row["actual_packet_hash"],
                "stored_workbench_hash": row["workbench_hash"],
                "actual_workbench_hash": row["actual_workbench_hash"],
            }
        )
    outcome_rows = _load_target_jsonl_rows(
        layout.runtime_root / _ANALYSIS_OUTCOME_DIR_NAME / target_key
    )
    outcome_by_task = {
        _optional_text(payload, "task_id"): payload
        for _path, _line, payload, _raw_line in outcome_rows
        if _optional_text(payload, "task_id")
    }
    outcome_sha_by_record_id: dict[str, str] = {}
    for path, line_number, payload, raw_line in outcome_rows:
        record_id = _optional_text(payload, "record_id")
        packet_id = _optional_text(payload, "context_packet_id")
        packet_hash = _optional_text(payload, "context_packet_hash")
        if record_id:
            outcome_sha_by_record_id[record_id] = sha256(
                raw_line.encode("utf-8")
            ).hexdigest()
        if not packet_id:
            continue
        context_row = context_packets.get(packet_id)
        if context_row is None:
            issues.append(
                {
                    "issue_type": "outcome_context_packet_missing",
                    "record_id": record_id,
                    "context_packet_id": packet_id,
                    "path": str(path),
                    "line_number": line_number,
                }
            )
            continue
        if context_row["status"] != "valid":
            issues.append(
                {
                    "issue_type": "outcome_context_packet_corrupt",
                    "record_id": record_id,
                    "context_packet_id": packet_id,
                    "path": str(path),
                    "line_number": line_number,
                }
            )
        elif packet_hash != context_row["packet_hash"]:
            issues.append(
                {
                    "issue_type": "outcome_context_hash_mismatch",
                    "record_id": record_id,
                    "context_packet_id": packet_id,
                    "stored_context_packet_hash": packet_hash,
                    "actual_context_packet_hash": context_row["packet_hash"],
                    "path": str(path),
                    "line_number": line_number,
                }
            )
    write_rows = _load_target_jsonl_rows(
        layout.runtime_root / "research_memory_writes" / target_key
    )
    for path, line_number, payload, _raw_line in write_rows:
        actor_type = _optional_text(payload, "actor_type")
        if actor_type and actor_type != "analysis":
            continue
        packet_id = _optional_text(payload, "context_packet_id")
        packet_hash = _optional_text(payload, "context_packet_hash")
        actor_id = _optional_text(payload, "actor_id")
        if actor_id and actor_id not in outcome_by_task:
            issues.append(
                {
                    "issue_type": "write_without_outcome",
                    "actor_id": actor_id,
                    "receipt_id": _optional_text(payload, "receipt_id"),
                    "path": str(path),
                    "line_number": line_number,
                }
            )
        if not packet_id:
            continue
        context_row = context_packets.get(packet_id)
        if context_row is None:
            issues.append(
                {
                    "issue_type": "write_context_packet_missing",
                    "actor_id": actor_id,
                    "receipt_id": _optional_text(payload, "receipt_id"),
                    "context_packet_id": packet_id,
                    "path": str(path),
                    "line_number": line_number,
                }
            )
            continue
        if context_row["status"] != "valid":
            issues.append(
                {
                    "issue_type": "write_context_packet_corrupt",
                    "actor_id": actor_id,
                    "receipt_id": _optional_text(payload, "receipt_id"),
                    "context_packet_id": packet_id,
                    "path": str(path),
                    "line_number": line_number,
                }
            )
        elif packet_hash != context_row["packet_hash"]:
            issues.append(
                {
                    "issue_type": "write_context_hash_mismatch",
                    "actor_id": actor_id,
                    "receipt_id": _optional_text(payload, "receipt_id"),
                    "context_packet_id": packet_id,
                    "stored_context_packet_hash": packet_hash,
                    "actual_context_packet_hash": context_row["packet_hash"],
                    "path": str(path),
                    "line_number": line_number,
                }
            )
    for path, line_number, payload, _raw_line in _load_target_jsonl_rows(
        layout.runtime_root / _CEAU_DIR_NAME / target_key
    ):
        if payload.get("record_type") != "analysis_completed":
            continue
        outcome_record_id = _optional_text(payload, "analysis_outcome_record_id")
        if not outcome_record_id:
            continue
        actual_sha = outcome_sha_by_record_id.get(outcome_record_id)
        if actual_sha is None:
            issues.append(
                {
                    "issue_type": "ceau_completed_outcome_missing",
                    "analysis_outcome_record_id": outcome_record_id,
                    "path": str(path),
                    "line_number": line_number,
                }
            )
            continue
        if _optional_text(payload, "analysis_outcome_sha256") != actual_sha:
            issues.append(
                {
                    "issue_type": "ceau_completed_outcome_sha_mismatch",
                    "analysis_outcome_record_id": outcome_record_id,
                    "stored_sha256": _optional_text(payload, "analysis_outcome_sha256"),
                    "actual_sha256": actual_sha,
                    "path": str(path),
                    "line_number": line_number,
                }
            )
    _append_commit_journal_issues(
        issues=issues,
        layout=layout,
        target_key=target_key,
    )
    return issues


def _append_commit_journal_issues(
    *,
    issues: list[dict[str, object]],
    layout: WorkspaceLayout,
    target_key: str,
) -> None:
    for audit_state in read_target_analysis_commit_audit_states(
        layout=layout,
        target_key=target_key,
    ):
        if not audit_state.commit_started_seen or audit_state.status == "complete":
            continue
        if audit_state.status == "failed":
            issues.append(
                {
                    "issue_type": "commit_failed",
                    "task_id": audit_state.task_id,
                    "failure_stage": audit_state.failure_stage,
                    "completion_state": audit_state.completion_state,
                    "thesis_revision_obligation_status": (
                        audit_state.thesis_revision_obligation_status
                    ),
                    "path": str(audit_state.terminal_journal_path),
                    "line_number": audit_state.terminal_journal_line_number,
                }
            )
            continue
        issues.append(
            {
                "issue_type": "commit_started_without_completed",
                "task_id": audit_state.task_id,
                "completion_state": audit_state.completion_state,
                "thesis_revision_obligation_status": (
                    audit_state.thesis_revision_obligation_status
                ),
                "path": str(audit_state.terminal_journal_path),
                "line_number": audit_state.terminal_journal_line_number,
            }
        )


def _payload_without_context_hashes(payload: dict[str, object]) -> dict[str, object]:
    patched = dict(payload)
    patched.pop("packet_hash", None)
    workbench_payload = patched.get("analysis_workbench")
    if isinstance(workbench_payload, dict):
        patched_workbench = dict(workbench_payload)
        patched_workbench.pop("analysis_workbench_payload_hash", None)
        patched["analysis_workbench"] = patched_workbench
    return patched


def _load_target_jsonl_rows(root: Path) -> tuple[tuple[Path, int, dict[str, object], str], ...]:
    if not root.exists():
        return ()
    if not root.is_dir():
        raise RuntimeRepairInspectionError(f"target runtime path must be a directory: {root}")
    rows: list[tuple[Path, int, dict[str, object], str]] = []
    for shard_path in sorted(root.glob("*.jsonl")):
        rows.extend(
            (shard_path, line_number, payload, raw_line)
            for line_number, payload, raw_line in _iter_jsonl_objects(shard_path)
        )
    return tuple(rows)


def _iter_jsonl_objects(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw_line = line.rstrip("\n")
            normalized = raw_line.strip()
            if not normalized:
                continue
            yield (
                line_number,
                _load_json_object(normalized, path=path, line_number=line_number),
                normalized,
            )


def _relative_issue_paths(
    issue: dict[str, object],
    *,
    workspace_root: Path,
) -> dict[str, object]:
    payload = dict(issue)
    path = payload.get("path")
    if isinstance(path, str) and path.strip():
        payload["path"] = _relative_path(Path(path), workspace_root=workspace_root)
    return payload


def _load_ceau_records(
    *,
    layout: WorkspaceLayout,
    target_key: str,
) -> tuple[dict[str, object], ...]:
    target_root = layout.runtime_root / _CEAU_DIR_NAME / target_key
    if not target_root.exists():
        return ()
    if not target_root.is_dir():
        raise RuntimeRepairInspectionError(
            f"CEAU target path must be a directory: {target_root}"
        )
    records: list[dict[str, object]] = []
    for shard_path in sorted(target_root.glob("*.jsonl")):
        with shard_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                records.append(
                    _load_json_object(
                        normalized,
                        path=shard_path,
                        line_number=line_number,
                    )
                )
    return tuple(records)


def _latest_recorded_at(
    records: list[dict[str, object]],
    record_type: str,
) -> datetime | None:
    timestamps = [
        datetime.fromisoformat(recorded_at)
        for payload in records
        if payload.get("record_type") == record_type
        and isinstance((recorded_at := payload.get("recorded_at")), str)
        and recorded_at.strip()
    ]
    if not timestamps:
        return None
    return max(timestamp.astimezone(UTC) for timestamp in timestamps)


def _load_json_object(line: str, *, path: Path, line_number: int) -> dict[str, object]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeRepairInspectionError(
            f"Invalid JSON receipt at {path}:{line_number}."
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeRepairInspectionError(
            f"Receipt must be a JSON object at {path}:{line_number}."
        )
    return payload


def _require_target_key(payload: dict[str, object], field_name: str, *, path: Path) -> str:
    return validate_target_key(
        _require_text(payload, field_name, path=path),
        error_type=RuntimeRepairInspectionError,
    )


def _require_event_id(payload: dict[str, object], field_name: str, *, path: Path) -> str:
    return validate_event_id(
        _require_text(payload, field_name, path=path),
        error_type=RuntimeRepairInspectionError,
    )


def _require_text(payload: dict[str, object], field_name: str, *, path: Path) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeRepairInspectionError(f"{field_name} must be a non-blank string in {path}.")
    return value.strip()


def _optional_text(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    return value.strip() if isinstance(value, str) else ""


def _require_bool(payload: dict[str, object], field_name: str, *, path: Path) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise RuntimeRepairInspectionError(f"{field_name} must be a boolean in {path}.")
    return value


def _require_datetime(payload: dict[str, object], field_name: str, *, path: Path) -> datetime:
    raw_value = _require_text(payload, field_name, path=path)
    try:
        value = datetime.fromisoformat(raw_value)
    except ValueError as exc:
        raise RuntimeRepairInspectionError(
            f"{field_name} must be an ISO datetime in {path}."
        ) from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeRepairInspectionError(f"{field_name} must be timezone-aware in {path}.")
    return value


def _validate_optional_target_key(value: str | None) -> str | None:
    if value is None:
        return None
    return validate_target_key(value, error_type=RuntimeRepairInspectionError)


def _validate_optional_event_id(value: str | None) -> str | None:
    if value is None:
        return None
    return validate_event_id(value, error_type=RuntimeRepairInspectionError)


def _relative_path(path: Path, *, workspace_root: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "AnalysisRecoveryDecision",
    "AnalysisRecoveryReport",
    "LiveAnalysisRepairCandidate",
    "RuntimeRepairInspectionError",
    "audit_live_analysis_consistency",
    "plan_analysis_recovery",
    "scan_live_analysis_repair_candidates",
]


