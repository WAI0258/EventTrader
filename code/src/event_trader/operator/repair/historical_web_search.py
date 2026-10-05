"""Deterministic repair for historical web-search archive artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from json import JSONDecodeError, JSONDecoder
from pathlib import Path
from typing import Literal

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.operator.repair.artifacts import backup_file
from event_trader.source_archive.web_search import (
    HistoricalWebSearchLibraryError,
    build_historical_web_search_target_layout,
    normalize_historical_web_search_payload,
)
from event_trader.source_release.quarantine import (
    SourceEventQuarantineError,
    normalize_source_event_quarantine_payload,
)
from event_trader.storage import WorkspaceLayout

_LEGACY_RETIRED_RECORD_FIELDS = frozenset(
    {
        "fetched_at",
        "historical_visibility_status",
        "historical_visibility_reason",
    }
)
_REPAIRABLE_DERIVED_IDENTITY_ERRORS = (
    "historical web_search partition payload observation_id does not match deterministic identity.",
    "historical web_search partition payload candidate_fingerprint does not match deterministic identity.",
)


class HistoricalWebSearchRepairError(ValueError):
    """Raised when historical web-search repair cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class HistoricalWebSearchSkippedFragment:
    path: Path
    line_number: int
    fragment_index: int
    reason: str
    fragment_preview: str

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "path": _relative_path(self.path, workspace_root),
            "line_number": self.line_number,
            "fragment_index": self.fragment_index,
            "reason": self.reason,
            "fragment_preview": self.fragment_preview,
        }


@dataclass(frozen=True, slots=True)
class HistoricalWebSearchArtifactIssue:
    path: Path
    line_number: int
    reason: str

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "path": _relative_path(self.path, workspace_root),
            "line_number": self.line_number,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class HistoricalWebSearchRepairReceipt:
    target_key: str
    apply: bool
    partition_date: date | None
    status: Literal["dry_run", "applied", "noop"]
    archive_partitions_scanned: int
    archive_partitions_rewritten: int
    rows_scanned: int
    rows_upgraded: int
    malformed_fragments_recovered: int
    fragments_skipped_fail_closed: int
    quarantine_files_scanned: int
    quarantine_files_rewritten: int
    quarantine_records_scanned: int
    quarantine_records_upgraded: int
    downstream_artifact_rewrites: int
    backup_root: Path | None
    rewritten_paths: tuple[Path, ...]
    skipped_fragments: tuple[HistoricalWebSearchSkippedFragment, ...]
    artifact_issues: tuple[HistoricalWebSearchArtifactIssue, ...]
    receipt_path: Path | None

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "apply": self.apply,
            "partition_date": (
                None if self.partition_date is None else self.partition_date.isoformat()
            ),
            "status": self.status,
            "archive_partitions_scanned": self.archive_partitions_scanned,
            "archive_partitions_rewritten": self.archive_partitions_rewritten,
            "rows_scanned": self.rows_scanned,
            "rows_upgraded": self.rows_upgraded,
            "malformed_fragments_recovered": self.malformed_fragments_recovered,
            "fragments_skipped_fail_closed": self.fragments_skipped_fail_closed,
            "quarantine_files_scanned": self.quarantine_files_scanned,
            "quarantine_files_rewritten": self.quarantine_files_rewritten,
            "quarantine_records_scanned": self.quarantine_records_scanned,
            "quarantine_records_upgraded": self.quarantine_records_upgraded,
            "downstream_artifact_rewrites": self.downstream_artifact_rewrites,
            "backup_root": (
                None
                if self.backup_root is None
                else _relative_path(self.backup_root, workspace_root)
            ),
            "rewritten_paths": [
                _relative_path(path, workspace_root) for path in self.rewritten_paths
            ],
            "skipped_fragments": [
                fragment.to_json_payload(workspace_root=workspace_root)
                for fragment in self.skipped_fragments
            ],
            "artifact_issues": [
                issue.to_json_payload(workspace_root=workspace_root)
                for issue in self.artifact_issues
            ],
            "receipt_path": (
                None
                if self.receipt_path is None
                else _relative_path(self.receipt_path, workspace_root)
            ),
        }


@dataclass(frozen=True, slots=True)
class _RecoveredPayload:
    payload: dict[str, object]
    recovered_from_malformed_line: bool


@dataclass(frozen=True, slots=True)
class _PartitionPlan:
    path: Path
    normalized_lines: tuple[str, ...]
    rows_scanned: int
    rows_upgraded: int
    malformed_fragments_recovered: int
    skipped_fragments: tuple[HistoricalWebSearchSkippedFragment, ...]
    needs_rewrite: bool


@dataclass(frozen=True, slots=True)
class _QuarantinePlan:
    path: Path
    normalized_lines: tuple[str, ...]
    records_scanned: int
    records_upgraded: int
    issues: tuple[HistoricalWebSearchArtifactIssue, ...]
    needs_rewrite: bool


def repair_historical_web_search_workspace(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    partition_date: date | None = None,
    apply: bool,
    created_at: datetime | None = None,
) -> HistoricalWebSearchRepairReceipt:
    """Normalize and repair historical web-search archive artifacts in one workspace."""
    if not isinstance(layout, WorkspaceLayout):
        raise HistoricalWebSearchRepairError(
            "layout must be a WorkspaceLayout instance."
        )
    normalized_target_key = validate_target_key(
        target_key,
        error_type=HistoricalWebSearchRepairError,
    )
    normalized_partition_date = _validate_partition_date(partition_date)
    archive_plans = tuple(
        _plan_archive_partition(path)
        for path in _archive_partition_paths(
            layout=layout,
            target_key=normalized_target_key,
            partition_date=normalized_partition_date,
        )
    )
    quarantine_plans = tuple(
        _plan_quarantine_file(layout=layout, path=path)
        for path in _quarantine_paths(
            layout=layout,
            target_key=normalized_target_key,
        )
    )

    rows_scanned = sum(plan.rows_scanned for plan in archive_plans)
    rows_upgraded = sum(plan.rows_upgraded for plan in archive_plans)
    malformed_fragments_recovered = sum(
        plan.malformed_fragments_recovered for plan in archive_plans
    )
    skipped_fragments = tuple(
        fragment
        for plan in archive_plans
        for fragment in plan.skipped_fragments
    )
    quarantine_records_scanned = sum(plan.records_scanned for plan in quarantine_plans)
    quarantine_records_upgraded = sum(plan.records_upgraded for plan in quarantine_plans)
    artifact_issues = tuple(
        issue
        for plan in quarantine_plans
        for issue in plan.issues
    )

    archive_rewrites = tuple(plan for plan in archive_plans if plan.needs_rewrite)
    quarantine_rewrites = tuple(
        plan for plan in quarantine_plans if plan.needs_rewrite and not plan.issues
    )
    rewritten_paths = tuple(
        path
        for path in (
            *(plan.path for plan in archive_rewrites),
            *(plan.path for plan in quarantine_rewrites),
        )
    )

    backup_root: Path | None = None
    receipt_path: Path | None = None
    if apply and rewritten_paths:
        now = _normalize_created_at(created_at)
        backup_root = _backup_root(
            layout=layout,
            target_key=normalized_target_key,
            created_at=now,
        )
        backup_root.mkdir(parents=True, exist_ok=False)
        for plan in archive_rewrites:
            _rewrite_file(
                path=plan.path,
                lines=plan.normalized_lines,
                backup_root=backup_root,
                layout=layout,
            )
        for plan in quarantine_rewrites:
            _rewrite_file(
                path=plan.path,
                lines=plan.normalized_lines,
                backup_root=backup_root,
                layout=layout,
            )
        receipt_path = _receipt_path(
            layout=layout,
            target_key=normalized_target_key,
            created_at=now,
        )

    status: Literal["dry_run", "applied", "noop"]
    if apply and rewritten_paths:
        status = "applied"
    elif rewritten_paths:
        status = "dry_run"
    else:
        status = "noop"

    receipt = HistoricalWebSearchRepairReceipt(
        target_key=normalized_target_key,
        apply=apply,
        partition_date=normalized_partition_date,
        status=status,
        archive_partitions_scanned=len(archive_plans),
        archive_partitions_rewritten=len(archive_rewrites),
        rows_scanned=rows_scanned,
        rows_upgraded=rows_upgraded,
        malformed_fragments_recovered=malformed_fragments_recovered,
        fragments_skipped_fail_closed=len(skipped_fragments),
        quarantine_files_scanned=len(quarantine_plans),
        quarantine_files_rewritten=len(quarantine_rewrites),
        quarantine_records_scanned=quarantine_records_scanned,
        quarantine_records_upgraded=quarantine_records_upgraded,
        downstream_artifact_rewrites=len(quarantine_rewrites),
        backup_root=backup_root,
        rewritten_paths=rewritten_paths,
        skipped_fragments=skipped_fragments,
        artifact_issues=artifact_issues,
        receipt_path=receipt_path,
    )
    if receipt_path is not None:
        _write_receipt(layout=layout, receipt=receipt)
    return receipt


def _archive_partition_paths(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    partition_date: date | None,
) -> tuple[Path, ...]:
    target_layout = build_historical_web_search_target_layout(layout, target_key)
    if partition_date is not None:
        path = target_layout.day_partition(partition_date)
        return (path,) if path.exists() else ()
    if not target_layout.target_root.exists():
        return ()
    return tuple(sorted(target_layout.target_root.glob("*.jsonl")))


def _quarantine_paths(
    *,
    layout: WorkspaceLayout,
    target_key: str,
) -> tuple[Path, ...]:
    root = (layout.helpers_root / "source_quarantine" / target_key).resolve(strict=False)
    if not root.exists():
        return ()
    return tuple(sorted(root.glob("*.jsonl")))


def _plan_archive_partition(path: Path) -> _PartitionPlan:
    resolved_path = path.resolve(strict=False)
    try:
        lines = resolved_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HistoricalWebSearchRepairError(
            f"Failed to read historical web_search partition {resolved_path}: {exc}"
        ) from exc

    normalized_lines: list[str] = []
    skipped_fragments: list[HistoricalWebSearchSkippedFragment] = []
    rows_scanned = 0
    rows_upgraded = 0
    malformed_fragments_recovered = 0
    needs_rewrite = False
    for line_number, raw_line in enumerate(lines, start=1):
        recovered_payloads, recovered_skips = _recover_partition_line(
            path=resolved_path,
            line_number=line_number,
            raw_line=raw_line,
        )
        skipped_fragments.extend(recovered_skips)
        if recovered_skips:
            needs_rewrite = True
        for recovered in recovered_payloads:
            rows_scanned += 1
            malformed_fragments_recovered += int(
                recovered.recovered_from_malformed_line
            )
            try:
                normalized_payload, payload_upgraded = _normalize_repair_payload(
                    recovered.payload
                )
            except HistoricalWebSearchLibraryError as exc:
                skipped_fragments.append(
                    HistoricalWebSearchSkippedFragment(
                        path=resolved_path,
                        line_number=line_number,
                        fragment_index=len(skipped_fragments) + 1,
                        reason=str(exc),
                        fragment_preview=_fragment_preview(
                            json.dumps(
                                recovered.payload,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        ),
                    )
                )
                needs_rewrite = True
                continue
            if payload_upgraded or normalized_payload != recovered.payload:
                rows_upgraded += 1
                needs_rewrite = True
            if recovered.recovered_from_malformed_line:
                needs_rewrite = True
            normalized_lines.append(_json_line(normalized_payload))
    return _PartitionPlan(
        path=resolved_path,
        normalized_lines=tuple(normalized_lines),
        rows_scanned=rows_scanned,
        rows_upgraded=rows_upgraded,
        malformed_fragments_recovered=malformed_fragments_recovered,
        skipped_fragments=tuple(skipped_fragments),
        needs_rewrite=needs_rewrite,
    )


def _recover_partition_line(
    *,
    path: Path,
    line_number: int,
    raw_line: str,
) -> tuple[
    tuple[_RecoveredPayload, ...],
    tuple[HistoricalWebSearchSkippedFragment, ...],
]:
    if not raw_line.strip():
        return (), ()
    try:
        payload = json.loads(raw_line)
    except JSONDecodeError:
        return _recover_malformed_partition_line(
            path=path,
            line_number=line_number,
            raw_line=raw_line,
        )
    if not isinstance(payload, dict):
        return (), (
            HistoricalWebSearchSkippedFragment(
                path=path,
                line_number=line_number,
                fragment_index=1,
                reason="partition line must decode to a JSON object.",
                fragment_preview=_fragment_preview(raw_line),
            ),
        )
    return (_RecoveredPayload(payload=payload, recovered_from_malformed_line=False),), ()


def _recover_malformed_partition_line(
    *,
    path: Path,
    line_number: int,
    raw_line: str,
) -> tuple[
    tuple[_RecoveredPayload, ...],
    tuple[HistoricalWebSearchSkippedFragment, ...],
]:
    decoder = JSONDecoder()
    payloads: list[_RecoveredPayload] = []
    skipped: list[HistoricalWebSearchSkippedFragment] = []
    index = 0
    fragment_index = 0
    while index < len(raw_line):
        while index < len(raw_line) and raw_line[index].isspace():
            index += 1
        if index >= len(raw_line):
            break
        fragment_index += 1
        try:
            payload, end_index = decoder.raw_decode(raw_line, index)
        except JSONDecodeError:
            skipped.append(
                HistoricalWebSearchSkippedFragment(
                    path=path,
                    line_number=line_number,
                    fragment_index=fragment_index,
                    reason="unreconstructable malformed JSON fragment.",
                    fragment_preview=_fragment_preview(raw_line[index:]),
                )
            )
            break
        if not isinstance(payload, dict):
            skipped.append(
                HistoricalWebSearchSkippedFragment(
                    path=path,
                    line_number=line_number,
                    fragment_index=fragment_index,
                    reason="recovered fragment did not decode to a JSON object.",
                    fragment_preview=_fragment_preview(raw_line[index:end_index]),
                )
            )
            break
        payloads.append(
            _RecoveredPayload(
                payload=payload,
                recovered_from_malformed_line=True,
            )
        )
        index = end_index
    return tuple(payloads), tuple(skipped)


def _normalize_repair_payload(payload: dict[str, object]) -> tuple[dict[str, object], bool]:
    sanitized_payload = dict(payload)
    if any(field in sanitized_payload for field in _LEGACY_RETIRED_RECORD_FIELDS):
        sanitized_payload = {
            key: value
            for key, value in sanitized_payload.items()
            if key not in _LEGACY_RETIRED_RECORD_FIELDS
        }
    try:
        normalized_payload = normalize_historical_web_search_payload(sanitized_payload)
        return normalized_payload, sanitized_payload != payload
    except HistoricalWebSearchLibraryError as exc:
        unsupported_prefix = (
            "historical web_search partition payload contains unsupported fields: "
        )
        error_text = str(exc)
        if error_text.startswith(unsupported_prefix):
            unsupported_fields = {
                field.strip()
                for field in error_text.removeprefix(unsupported_prefix).rstrip(".").split(",")
                if field.strip()
            }
            if not unsupported_fields or not unsupported_fields.issubset(
                _LEGACY_RETIRED_RECORD_FIELDS
            ):
                raise
            stripped_payload = {
                key: value
                for key, value in sanitized_payload.items()
                if key not in _LEGACY_RETIRED_RECORD_FIELDS
            }
            normalized_payload = normalize_historical_web_search_payload(stripped_payload)
            return normalized_payload, True
        if error_text not in _REPAIRABLE_DERIVED_IDENTITY_ERRORS:
            raise
        stripped_payload = {
            key: value
            for key, value in sanitized_payload.items()
            if key not in {"candidate_fingerprint", "observation_id"}
        }
        normalized_payload = normalize_historical_web_search_payload(stripped_payload)
        return normalized_payload, True


def _plan_quarantine_file(
    *,
    layout: WorkspaceLayout,
    path: Path,
) -> _QuarantinePlan:
    resolved_path = path.resolve(strict=False)
    try:
        lines = resolved_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HistoricalWebSearchRepairError(
            f"Failed to read quarantine file {resolved_path}: {exc}"
        ) from exc

    normalized_lines: list[str] = []
    issues: list[HistoricalWebSearchArtifactIssue] = []
    records_scanned = 0
    records_upgraded = 0
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except JSONDecodeError as exc:
            raise HistoricalWebSearchRepairError(
                f"Invalid JSON in quarantine file {resolved_path}:{line_number}"
            ) from exc
        records_scanned += 1
        try:
            normalized_payload = normalize_source_event_quarantine_payload(
                layout,
                payload,
            )
        except SourceEventQuarantineError as exc:
            issues.append(
                HistoricalWebSearchArtifactIssue(
                    path=resolved_path,
                    line_number=line_number,
                    reason=str(exc),
                )
            )
            normalized_lines.append(raw_line.strip())
            continue
        if normalized_payload != payload:
            records_upgraded += 1
        normalized_lines.append(_json_line(normalized_payload))
    return _QuarantinePlan(
        path=resolved_path,
        normalized_lines=tuple(normalized_lines),
        records_scanned=records_scanned,
        records_upgraded=records_upgraded,
        issues=tuple(issues),
        needs_rewrite=records_upgraded > 0,
    )


def _rewrite_file(
    *,
    path: Path,
    lines: tuple[str, ...],
    backup_root: Path,
    layout: WorkspaceLayout,
) -> None:
    backup_file(path=path, backup_root=backup_root, layout=layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line)
            handle.write("\n")


def _write_receipt(
    *,
    layout: WorkspaceLayout,
    receipt: HistoricalWebSearchRepairReceipt,
) -> None:
    if receipt.receipt_path is None:
        raise HistoricalWebSearchRepairError("repair receipt path is required.")
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
        / "historical_web_search"
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
        / "repair"
        / "historical_web_search"
        / target_key
        / f"{stamp}-repair.json"
    ).resolve(strict=False)


def _json_line(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fragment_preview(raw_value: str) -> str:
    normalized = raw_value.strip()
    if len(normalized) <= 160:
        return normalized
    return normalized[:157] + "..."


def _relative_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(
            workspace_root.resolve(strict=False)
        ).as_posix()
    except ValueError:
        return path.resolve(strict=False).as_posix()


def _normalize_created_at(value: datetime | None) -> datetime:
    return validate_timestamp(
        value or datetime.now(UTC),
        field_name="created_at",
        error_type=HistoricalWebSearchRepairError,
    ).astimezone(UTC)


def _validate_partition_date(value: date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        raise HistoricalWebSearchRepairError(
            "partition_date must be a date, not a datetime."
        )
    if not isinstance(value, date):
        raise HistoricalWebSearchRepairError("partition_date must be a date instance.")
    return value


__all__ = [
    "HistoricalWebSearchArtifactIssue",
    "HistoricalWebSearchRepairError",
    "HistoricalWebSearchRepairReceipt",
    "HistoricalWebSearchSkippedFragment",
    "repair_historical_web_search_workspace",
]
