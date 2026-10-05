"""Targeted quarantine-and-repair flow for archived source events."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from event_trader.operator.repair.live_catchup_runtime import (
    LiveCatchupRuntimeRepairReport,
    repair_live_catchup_runtime_from_replay_boundary,
)
from event_trader.replay.checkpoint import read_persisted_replay_checkpoints
from event_trader.source_release import (
    SourceEventIdentities,
    SourceEventQuarantineError,
    SourceEventQuarantineRecord,
    SourceEventQuarantineWriteReceipt,
    append_source_event_quarantine,
    resolve_archived_web_search_event,
)
from event_trader.storage import WorkspaceLayout

RebuildReplayRange = Callable[..., int]


class SourceEventQuarantineRepairError(ValueError):
    """Raised when a targeted source-event quarantine repair cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class SourceEventQuarantineRepairReceipt:
    """Auditable receipt for one targeted source-event quarantine repair."""

    target_key: str
    requested_event_id: str
    source_kind: Literal["web_search"]
    observation_id: str
    source_ref: str
    live_event_id: str
    replay_event_id: str | None
    replay_visible_at: datetime
    apply: bool
    quarantine_status: Literal["planned", "written", "noop_existing"]
    quarantine_path: Path | None
    repair_replay_at: datetime | None
    repair_end_at: datetime | None
    affected_checkpoint_count: int
    runtime_repair_status: Literal["dry_run", "applied", "blocked", "noop"] | None
    runtime_repair_receipt_path: Path | None
    rebuilt_event_count: int
    status: Literal["dry_run", "applied", "blocked", "noop"]
    receipt_path: Path | None

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "requested_event_id": self.requested_event_id,
            "source_kind": self.source_kind,
            "observation_id": self.observation_id,
            "source_ref": self.source_ref,
            "live_event_id": self.live_event_id,
            "replay_event_id": self.replay_event_id,
            "replay_visible_at": self.replay_visible_at.isoformat(),
            "apply": self.apply,
            "quarantine_status": self.quarantine_status,
            "quarantine_path": (
                None
                if self.quarantine_path is None
                else _relative_path(self.quarantine_path, workspace_root)
            ),
            "repair_replay_at": (
                None if self.repair_replay_at is None else self.repair_replay_at.isoformat()
            ),
            "repair_end_at": (
                None if self.repair_end_at is None else self.repair_end_at.isoformat()
            ),
            "affected_checkpoint_count": self.affected_checkpoint_count,
            "runtime_repair_status": self.runtime_repair_status,
            "runtime_repair_receipt_path": (
                None
                if self.runtime_repair_receipt_path is None
                else _relative_path(self.runtime_repair_receipt_path, workspace_root)
            ),
            "rebuilt_event_count": self.rebuilt_event_count,
            "status": self.status,
            "receipt_path": (
                None
                if self.receipt_path is None
                else _relative_path(self.receipt_path, workspace_root)
            ),
        }


def quarantine_and_repair_web_search_event(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_id: str,
    source_ref: str | None,
    reason: str,
    apply: bool,
    rebuild_replay_range: RebuildReplayRange,
    created_at: datetime | None = None,
    runtime_repair: Callable[..., LiveCatchupRuntimeRepairReport] = repair_live_catchup_runtime_from_replay_boundary,
) -> SourceEventQuarantineRepairReceipt:
    """Quarantine one archived web-search event and repair downstream live-catchup state."""
    if not isinstance(layout, WorkspaceLayout):
        raise SourceEventQuarantineRepairError(
            "layout must be a WorkspaceLayout instance."
        )
    if not callable(rebuild_replay_range):
        raise SourceEventQuarantineRepairError(
            "rebuild_replay_range must be callable."
        )
    if not callable(runtime_repair):
        raise SourceEventQuarantineRepairError("runtime_repair must be callable.")
    try:
        resolved = resolve_archived_web_search_event(
            layout,
            target_key=target_key,
            event_id=event_id,
            source_ref=source_ref,
        )
    except SourceEventQuarantineError as exc:
        raise SourceEventQuarantineRepairError(str(exc)) from exc

    now = (created_at or datetime.now(UTC)).astimezone(UTC)
    quarantine_record = _quarantine_record(
        resolved=resolved,
        requested_event_id=event_id,
        reason=reason,
        created_at=now,
    )
    repair_replay_at, repair_end_at, affected_checkpoint_count = _affected_replay_window(
        layout=layout,
        target_key=target_key,
        replay_visible_at=resolved.release_at,
    )

    quarantine_receipt: SourceEventQuarantineWriteReceipt | None = None
    runtime_report: LiveCatchupRuntimeRepairReport | None = None
    rebuilt_event_count = 0
    status: Literal["dry_run", "applied", "blocked", "noop"] = "dry_run"
    if apply:
        try:
            quarantine_receipt = append_source_event_quarantine(
                layout,
                record=quarantine_record,
            )
        except SourceEventQuarantineError as exc:
            raise SourceEventQuarantineRepairError(str(exc)) from exc
        if repair_replay_at is not None:
            runtime_report = runtime_repair(
                layout=layout,
                target_key=target_key,
                repair_replay_at=repair_replay_at,
                apply=True,
                created_at=now,
            )
            if runtime_report.status == "blocked":
                status = "blocked"
            else:
                rebuilt_event_count = rebuild_replay_range(
                    target_key=target_key,
                    start_at=repair_replay_at,
                    end_at=repair_end_at,
                )
                status = "applied"
        else:
            status = "applied"
    elif affected_checkpoint_count == 0:
        status = "dry_run"

    receipt = SourceEventQuarantineRepairReceipt(
        target_key=target_key,
        requested_event_id=event_id,
        source_kind="web_search",
        observation_id=resolved.observation_id,
        source_ref=resolved.source_ref,
        live_event_id=resolved.live_event_id,
        replay_event_id=resolved.replay_event_id,
        replay_visible_at=resolved.release_at,
        apply=apply,
        quarantine_status=(
            "planned" if quarantine_receipt is None else quarantine_receipt.status
        ),
        quarantine_path=None if quarantine_receipt is None else quarantine_receipt.path,
        repair_replay_at=repair_replay_at,
        repair_end_at=repair_end_at,
        affected_checkpoint_count=affected_checkpoint_count,
        runtime_repair_status=(
            None if runtime_report is None else runtime_report.status
        ),
        runtime_repair_receipt_path=(
            None if runtime_report is None else runtime_report.receipt_path
        ),
        rebuilt_event_count=rebuilt_event_count,
        status=status if affected_checkpoint_count or apply else "noop",
        receipt_path=None if not apply else _receipt_path(
            layout=layout,
            target_key=target_key,
            created_at=now,
        ),
    )
    if receipt.receipt_path is not None:
        _write_receipt(layout=layout, receipt=receipt)
    return receipt


def _affected_replay_window(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    replay_visible_at: datetime,
) -> tuple[datetime | None, datetime | None, int]:
    matching = [
        item.checkpoint
        for item in read_persisted_replay_checkpoints(layout, target_key=target_key)
        if item.checkpoint.stage == "live_catchup_replay"
        and item.checkpoint.replay_at >= replay_visible_at
    ]
    if not matching:
        return None, None, 0
    return (
        replay_visible_at,
        max(item.replay_at for item in matching),
        len({item.event_id for item in matching}),
    )


def _quarantine_record(
    *,
    resolved: SourceEventIdentities,
    requested_event_id: str,
    reason: str,
    created_at: datetime,
) -> SourceEventQuarantineRecord:
    return SourceEventQuarantineRecord(
        target_key=resolved.target_key,
        source_kind=resolved.source_kind,
        requested_event_id=requested_event_id,
        observation_id=resolved.observation_id,
        candidate_fingerprint=resolved.candidate_fingerprint,
        live_event_id=resolved.live_event_id,
        replay_event_id=resolved.replay_event_id,
        source_ref=resolved.source_ref,
        title=resolved.title,
        release_at=resolved.release_at,
        observed_at=resolved.observed_at,
        reason=reason,
        created_at=created_at,
    )


def _receipt_path(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    created_at: datetime,
) -> Path:
    stamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (
        layout.runtime_root
        / "live_catchup"
        / target_key
        / f"{stamp}-source-event-quarantine-repair.json"
    ).resolve(strict=False)


def _write_receipt(
    *,
    layout: WorkspaceLayout,
    receipt: SourceEventQuarantineRepairReceipt,
) -> None:
    if receipt.receipt_path is None:
        raise SourceEventQuarantineRepairError("repair receipt path is required.")
    receipt.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt.receipt_path.write_text(
        json.dumps(
            receipt.to_json_payload(workspace_root=layout.root),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _relative_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(
            workspace_root.resolve(strict=False)
        ).as_posix()
    except ValueError:
        return path.resolve(strict=False).as_posix()


__all__ = [
    "SourceEventQuarantineRepairError",
    "SourceEventQuarantineRepairReceipt",
    "quarantine_and_repair_web_search_event",
]
