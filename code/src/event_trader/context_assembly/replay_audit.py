"""Replay visibility audit persistence for context packets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from event_trader.context_assembly.packets import (
    ContextPacket,
    HISTORICAL_THESIS_SNAPSHOT_MEMORY_READ_POLICY,
    ReplayVisibilityAuditReport,
    build_replay_visibility_audit_report,
)
from event_trader.context_assembly.store import FileBackedContextPacketStore
from event_trader.storage import WorkspaceLayout

_VISIBILITY_AUDIT_DIR_NAME = "visibility_audits"


class ReplayVisibilityAuditError(RuntimeError):
    """Raised when replay context visibility audit fails."""


@dataclass(frozen=True, slots=True)
class PersistedReplayVisibilityAudit:
    run_id: str
    created_at: datetime
    report: ReplayVisibilityAuditReport
    path: Path

    def to_json_payload(self) -> dict[str, object]:
        payload = self.report.to_json_payload()
        return {
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            **payload,
        }


def write_replay_visibility_audit(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    packets: tuple[ContextPacket, ...],
) -> PersistedReplayVisibilityAudit:
    if not isinstance(layout, WorkspaceLayout):
        raise ReplayVisibilityAuditError("layout must be a WorkspaceLayout instance.")
    normalized_run_id = _validate_run_id(run_id)
    report = build_replay_visibility_audit_report(
        packets,
        required_memory_read_policy=HISTORICAL_THESIS_SNAPSHOT_MEMORY_READ_POLICY,
        required_runtime_scope="replay",
        required_run_id=normalized_run_id,
    )
    audit = PersistedReplayVisibilityAudit(
        run_id=normalized_run_id,
        created_at=datetime.now(UTC),
        report=report,
        path=replay_visibility_audit_path(layout, run_id=normalized_run_id),
    )
    try:
        audit.path.parent.mkdir(parents=True, exist_ok=True)
        audit.path.write_text(
            json.dumps(audit.to_json_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        raise ReplayVisibilityAuditError(
            f"Failed to write replay visibility audit {audit.path}: {exc}"
        ) from exc
    if report.status == "failed":
        raise ReplayVisibilityAuditError(
            "Replay visibility audit failed: "
            f"violation_count={report.violation_count} "
            f"failed_stages={list(report.failed_stages)!r}."
        )
    return audit


def write_replay_visibility_audit_from_store(
    *,
    layout: WorkspaceLayout,
    run_id: str,
) -> PersistedReplayVisibilityAudit:
    normalized_run_id = _validate_run_id(run_id)
    persisted_packets = FileBackedContextPacketStore(
        layout,
        runtime_scope="replay",
        run_id=normalized_run_id,
    ).read_all_packets()
    return write_replay_visibility_audit(
        layout=layout,
        run_id=normalized_run_id,
        packets=tuple(record.packet for record in persisted_packets),
    )


def load_replay_visibility_audit(
    *,
    layout: WorkspaceLayout,
    run_id: str,
) -> dict[str, object]:
    path = replay_visibility_audit_path(layout, run_id=run_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReplayVisibilityAuditError(
            f"Failed to read replay visibility audit {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ReplayVisibilityAuditError(
            f"Replay visibility audit is invalid JSON: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReplayVisibilityAuditError("replay visibility audit must be a JSON object.")
    return payload


def replay_visibility_audit_path(layout: WorkspaceLayout, *, run_id: str) -> Path:
    return (
        layout.runtime_root
        / _VISIBILITY_AUDIT_DIR_NAME
        / "replay"
        / f"{_validate_run_id(run_id)}.json"
    ).resolve(strict=False)


def _validate_run_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayVisibilityAuditError("run_id must be non-blank.")
    normalized = value.strip()
    if normalized in {".", ".."} or any(ch in normalized for ch in ("/", "\\")):
        raise ReplayVisibilityAuditError("run_id must be a clean path segment.")
    return normalized


__all__ = [
    "PersistedReplayVisibilityAudit",
    "ReplayVisibilityAuditError",
    "load_replay_visibility_audit",
    "replay_visibility_audit_path",
    "write_replay_visibility_audit",
    "write_replay_visibility_audit_from_store",
]
