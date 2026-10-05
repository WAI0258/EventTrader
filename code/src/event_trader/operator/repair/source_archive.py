"""Source archive hygiene repairs for operator catch-up workflows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.feeds.payload_mappers import replay_adapter_for
from event_trader.operator.repair.artifacts import (
    JsonlRecord,
    backup_file,
    read_jsonl_records,
)
from event_trader.replay import build as replay_build
from event_trader.source_archive.market_news import (
    MarketNewsArchiveRecord,
    build_market_news_target_layout,
    _record_from_payload,
)
from event_trader.storage import WorkspaceLayout

SourceArchiveCleanupChannel = Literal["web_search", "market_news"]


class SourceArchiveCleanupError(ValueError):
    """Raised when source archive cleanup cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class SourceArchiveDuplicate:
    event_id: str
    source_ref: str
    partition_path: Path
    line_number: int
    captured_at: datetime
    differing_fields: tuple[str, ...]

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "source_ref": self.source_ref,
            "partition_path": _relative_path(self.partition_path, workspace_root),
            "line_number": self.line_number,
            "captured_at": self.captured_at.isoformat(),
            "differing_fields": list(self.differing_fields),
        }


@dataclass(frozen=True, slots=True)
class SourceArchiveCleanupReceipt:
    target_key: str
    start_at: datetime
    end_at: datetime
    channels: tuple[SourceArchiveCleanupChannel, ...]
    apply: bool
    status: Literal["dry_run", "applied", "noop"]
    repairable_duplicates: tuple[SourceArchiveDuplicate, ...]
    removed_count: int
    rewritten_paths: tuple[Path, ...]
    backup_root: Path | None
    receipt_path: Path

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "channels": list(self.channels),
            "apply": self.apply,
            "status": self.status,
            "repairable_duplicates": [
                duplicate.to_json_payload(workspace_root=workspace_root)
                for duplicate in self.repairable_duplicates
            ],
            "removed_count": self.removed_count,
            "rewritten_paths": [
                _relative_path(path, workspace_root) for path in self.rewritten_paths
            ],
            "backup_root": (
                None
                if self.backup_root is None
                else _relative_path(self.backup_root, workspace_root)
            ),
            "receipt_path": _relative_path(self.receipt_path, workspace_root),
        }


@dataclass(frozen=True, slots=True)
class _MarketNewsReplayRow:
    record: MarketNewsArchiveRecord
    jsonl_record: JsonlRecord
    event_id: str
    normalized_payload: dict[str, object]


def cleanup_live_catchup_source_archive(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
    channels: tuple[SourceArchiveCleanupChannel, ...],
    apply: bool,
    created_at: datetime | None = None,
) -> SourceArchiveCleanupReceipt:
    if not isinstance(layout, WorkspaceLayout):
        raise SourceArchiveCleanupError("layout must be a WorkspaceLayout instance.")
    normalized_target_key = validate_target_key(
        target_key,
        error_type=SourceArchiveCleanupError,
    )
    normalized_start_at = validate_timestamp(
        start_at,
        field_name="start_at",
        error_type=SourceArchiveCleanupError,
    )
    normalized_end_at = validate_timestamp(
        end_at,
        field_name="end_at",
        error_type=SourceArchiveCleanupError,
    )
    if normalized_end_at < normalized_start_at:
        raise SourceArchiveCleanupError("end_at must be greater than or equal to start_at.")
    normalized_channels = _validate_channels(channels)

    repairable: list[SourceArchiveDuplicate] = []
    if "market_news" in normalized_channels:
        repairable.extend(
            _repairable_market_news_duplicates(
                layout=layout,
                target_key=normalized_target_key,
                start_at=normalized_start_at,
                end_at=normalized_end_at,
            )
        )

    now = (created_at or datetime.now(UTC)).astimezone(UTC)
    backup_root = (
        _backup_root(
            layout=layout,
            target_key=normalized_target_key,
            created_at=now,
        )
        if apply and repairable
        else None
    )
    rewritten_paths: tuple[Path, ...] = ()
    removed_count = 0
    if apply and repairable:
        assert backup_root is not None
        backup_root.mkdir(parents=True, exist_ok=False)
        removed_count, rewritten_paths = _remove_market_news_duplicates(
            duplicates=tuple(repairable),
            backup_root=backup_root,
            layout=layout,
        )

    status: Literal["dry_run", "applied", "noop"]
    if apply and removed_count:
        status = "applied"
    elif repairable:
        status = "dry_run"
    else:
        status = "noop"
    receipt_path = _write_receipt(
        layout=layout,
        receipt=SourceArchiveCleanupReceipt(
            target_key=normalized_target_key,
            start_at=normalized_start_at,
            end_at=normalized_end_at,
            channels=normalized_channels,
            apply=apply,
            status=status,
            repairable_duplicates=tuple(repairable),
            removed_count=removed_count,
            rewritten_paths=rewritten_paths,
            backup_root=backup_root,
            receipt_path=_receipt_path(
                layout=layout,
                target_key=normalized_target_key,
                created_at=now,
            ),
        ),
    )
    return SourceArchiveCleanupReceipt(
        target_key=normalized_target_key,
        start_at=normalized_start_at,
        end_at=normalized_end_at,
        channels=normalized_channels,
        apply=apply,
        status=status,
        repairable_duplicates=tuple(repairable),
        removed_count=removed_count,
        rewritten_paths=rewritten_paths,
        backup_root=backup_root,
        receipt_path=receipt_path,
    )


def _repairable_market_news_duplicates(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
) -> tuple[SourceArchiveDuplicate, ...]:
    rows = _load_market_news_replay_rows(
        layout=layout,
        target_key=target_key,
        start_at=start_at,
        end_at=end_at,
    )
    by_event_id: dict[str, _MarketNewsReplayRow] = {}
    repairable: list[SourceArchiveDuplicate] = []
    for row in rows:
        existing = by_event_id.get(row.event_id)
        if existing is None:
            by_event_id[row.event_id] = row
            continue
        differing_fields = _payload_differing_fields(
            existing.normalized_payload,
            row.normalized_payload,
        )
        if differing_fields != ("captured_at",):
            raise SourceArchiveCleanupError(
                "Unsafe market_news replay duplicate conflict: "
                f"event_id={row.event_id} source_ref={row.record.source_ref} "
                f"differing_fields={','.join(differing_fields) or '(none)'}. "
                "Manual source archive review is required."
            )
        keep, remove = _keep_earliest_captured_at(existing, row)
        by_event_id[row.event_id] = keep
        repairable.append(
            SourceArchiveDuplicate(
                event_id=remove.event_id,
                source_ref=remove.record.source_ref,
                partition_path=remove.jsonl_record.path,
                line_number=remove.jsonl_record.line_number,
                captured_at=remove.record.captured_at,
                differing_fields=differing_fields,
            )
        )
    return tuple(repairable)


def _load_market_news_replay_rows(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
) -> tuple[_MarketNewsReplayRow, ...]:
    target_layout = build_market_news_target_layout(layout, target_key)
    if not target_layout.target_root.exists():
        return ()
    rows: list[_MarketNewsReplayRow] = []
    for partition_path in sorted(target_layout.target_root.glob("*.jsonl")):
        for jsonl_record in read_jsonl_records(partition_path):
            record = _record_from_payload(jsonl_record.payload)
            visible_at = replay_build._canonical_market_news_visible_at(
                record,
                window_start=start_at,
            )
            if visible_at < start_at or visible_at > end_at:
                continue
            payload = {
                "source_ref": record.source_ref,
                "headline": record.headline,
                "body": record.content_text,
                "published_at": record.created_at,
                "updated_at": record.updated_at,
                "captured_at": record.captured_at,
                "visible_at": visible_at,
            }
            ingress_input = replay_adapter_for("historical_market_news")(payload)
            labels = tuple(record.labels)
            rows.append(
                _MarketNewsReplayRow(
                    record=record,
                    jsonl_record=jsonl_record,
                    event_id=replay_build._replay_identity(
                        target_key=target_key,
                        ingress_input=ingress_input,
                        labels=labels,
                    ),
                    normalized_payload=replay_build._serialize_dataset_row(
                        ingress_input=ingress_input,
                        labels=labels,
                    ),
                )
            )
    return tuple(rows)


def _remove_market_news_duplicates(
    *,
    duplicates: tuple[SourceArchiveDuplicate, ...],
    backup_root: Path,
    layout: WorkspaceLayout,
) -> tuple[int, tuple[Path, ...]]:
    removals_by_path: dict[Path, set[int]] = {}
    for duplicate in duplicates:
        removals_by_path.setdefault(duplicate.partition_path, set()).add(
            duplicate.line_number
        )

    removed_count = 0
    rewritten_paths: list[Path] = []
    for path, line_numbers in sorted(removals_by_path.items()):
        records = read_jsonl_records(path)
        kept_lines = [
            record.line
            for record in records
            if record.line_number not in line_numbers
        ]
        path_removed_count = len(records) - len(kept_lines)
        if path_removed_count == 0:
            continue
        backup_file(path=path, backup_root=backup_root, layout=layout)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for line in kept_lines:
                handle.write(line)
                handle.write("\n")
        removed_count += path_removed_count
        rewritten_paths.append(path.resolve(strict=False))
    return removed_count, tuple(rewritten_paths)


def _keep_earliest_captured_at(
    left: _MarketNewsReplayRow,
    right: _MarketNewsReplayRow,
) -> tuple[_MarketNewsReplayRow, _MarketNewsReplayRow]:
    if left.record.captured_at <= right.record.captured_at:
        return left, right
    return right, left


def _payload_differing_fields(
    left: dict[str, object],
    right: dict[str, object],
) -> tuple[str, ...]:
    keys = set(left) | set(right)
    return tuple(sorted(key for key in keys if left.get(key) != right.get(key)))


def _validate_channels(
    channels: tuple[SourceArchiveCleanupChannel, ...],
) -> tuple[SourceArchiveCleanupChannel, ...]:
    normalized: list[SourceArchiveCleanupChannel] = []
    for channel in channels:
        if channel not in {"web_search", "market_news"}:
            raise SourceArchiveCleanupError(f"Unsupported cleanup channel: {channel!r}.")
        if channel not in normalized:
            normalized.append(channel)
    return tuple(normalized)


def _write_receipt(
    *,
    layout: WorkspaceLayout,
    receipt: SourceArchiveCleanupReceipt,
) -> Path:
    path = receipt.receipt_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            receipt.to_json_payload(workspace_root=layout.root),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _backup_root(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    created_at: datetime,
) -> Path:
    stamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (
        layout.runtime_root
        / "repair"
        / "backups"
        / "source_archive"
        / target_key
        / stamp
    ).resolve(strict=False)


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
        / f"{stamp}-source-archive-cleanup.json"
    ).resolve(strict=False)


def _relative_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(
            workspace_root.resolve(strict=False)
        ).as_posix()
    except ValueError:
        return path.resolve(strict=False).as_posix()


__all__ = [
    "SourceArchiveCleanupError",
    "SourceArchiveCleanupReceipt",
    "cleanup_live_catchup_source_archive",
]
