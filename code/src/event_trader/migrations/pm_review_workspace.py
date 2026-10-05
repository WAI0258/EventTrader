"""High-level workspace bootstrap for the PMReview runtime architecture."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from event_trader.contracts._validators import validate_target_key
from event_trader.execution.store import ExecutionRecordStore
from event_trader.migrations.cutover_baseline import (
    CUTOVER_RUNTIME_SCHEMA_VERSION,
    DEFAULT_CUTOVER_MIGRATION_ID,
    CutoverMigrationResult,
    read_cutover_baseline_payload,
    run_cutover_baseline_migration,
    validate_cutover_baseline,
)
from event_trader.research_memory.current_view import (
    migrate_current_view_to_market_setup_dashboard,
)
from event_trader.storage import WorkspaceLayout


class PMReviewWorkspaceMigrationError(ValueError):
    """Raised when a workspace cannot be prepared for the PMReview runtime."""


@dataclass(frozen=True, slots=True)
class PMReviewWorkspaceMigrationResult:
    schema_path: Path
    baseline_path: Path
    baseline_payload: dict[str, object]
    legacy_audit_index_path: Path | None
    migrated_research_memory_paths: tuple[Path, ...]


def prepare_pm_review_workspace(
    *,
    layout: WorkspaceLayout,
    target_keys: tuple[str, ...],
    migrated_at: datetime,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
    schema_version: str = CUTOVER_RUNTIME_SCHEMA_VERSION,
) -> PMReviewWorkspaceMigrationResult:
    """Prepare one workspace for PMReview execution without inventing runtime history."""

    _validate_layout(layout)
    normalized_targets = _normalize_target_keys(target_keys)
    if not isinstance(migrated_at, datetime) or migrated_at.tzinfo is None:
        raise PMReviewWorkspaceMigrationError(
            "migrated_at must be a timezone-aware datetime."
        )

    cutover_result = run_cutover_baseline_migration(
        layout=layout,
        target_keys=normalized_targets,
        migrated_at=migrated_at,
        migration_id=migration_id,
        schema_version=schema_version,
    )
    legacy_index_path = _write_legacy_audit_index_if_needed(
        layout=layout,
        target_keys=normalized_targets,
        migrated_at=migrated_at,
        migration_id=migration_id,
        schema_version=schema_version,
    )
    migrated_research_memory_paths = tuple(
        migrate_current_view_to_market_setup_dashboard(
            layout=layout,
            target_key=target_key,
        )
        for target_key in normalized_targets
    )
    validate_pm_review_workspace_ready(
        layout=layout,
        target_keys=normalized_targets,
        migration_id=migration_id,
        expected_schema_version=schema_version,
    )
    return _from_cutover_result(
        cutover_result,
        legacy_audit_index_path=legacy_index_path,
        migrated_research_memory_paths=migrated_research_memory_paths,
    )


def validate_pm_review_workspace_ready(
    *,
    layout: WorkspaceLayout,
    target_keys: tuple[str, ...],
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
    expected_schema_version: str = CUTOVER_RUNTIME_SCHEMA_VERSION,
) -> None:
    """Fail loudly unless PMReview runtime schema and baseline truth are present."""

    _validate_layout(layout)
    normalized_targets = _normalize_target_keys(target_keys)
    try:
        from event_trader.portfolio.active_exposure import (
            validate_active_exposure_runtime,
        )

        validate_cutover_baseline(
            layout=layout,
            migration_id=migration_id,
            expected_schema_version=expected_schema_version,
        )
        validate_active_exposure_runtime(
            layout=layout,
            migration_id=migration_id,
            expected_schema_version=expected_schema_version,
        )
    except ValueError as exc:
        raise PMReviewWorkspaceMigrationError(str(exc)) from exc

    payload = read_cutover_baseline_payload(
        layout=layout,
        migration_id=migration_id,
        expected_schema_version=expected_schema_version,
    )
    baseline_targets = payload.get("targets")
    if not isinstance(baseline_targets, list):
        raise PMReviewWorkspaceMigrationError("cutover baseline targets must be a list.")
    available_targets = {
        item.get("target_key")
        for item in baseline_targets
        if isinstance(item, dict)
    }
    missing = [target_key for target_key in normalized_targets if target_key not in available_targets]
    if missing:
        raise PMReviewWorkspaceMigrationError(
            "cutover baseline is missing target_key entries: " + ", ".join(missing)
        )
    for item in baseline_targets:
        if not isinstance(item, dict):
            continue
        if item.get("target_key") not in normalized_targets:
            continue


def _write_legacy_audit_index_if_needed(
    *,
    layout: WorkspaceLayout,
    target_keys: tuple[str, ...],
    migrated_at: datetime,
    migration_id: str,
    schema_version: str,
) -> Path | None:
    targets = [
        _legacy_audit_target_entry(layout=layout, target_key=target_key)
        for target_key in target_keys
    ]
    if not any(_target_entry_has_legacy_records(item) for item in targets):
        return None
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "migration_id": migration_id,
        "index_kind": "legacy_runtime_audit_index",
        "indexed_at": migrated_at.isoformat(),
        "active_exposure_policy": "legacy_runtime_audit_only_never_active_exposure",
        "targets": targets,
    }
    path = _legacy_audit_index_path(layout, migration_id)
    _write_json_atomic(path, payload)
    return path


def _legacy_audit_target_entry(*, layout: WorkspaceLayout, target_key: str) -> dict[str, object]:
    return {
        "target_key": target_key,
        "legacy_execution_record_ids": [
            persisted.record.execution_record_id
            for persisted in ExecutionRecordStore(layout).read_records(target_key=target_key)
        ],
    }


def _target_entry_has_legacy_records(payload: dict[str, object]) -> bool:
    for key in ("legacy_execution_record_ids",):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return True
    return False


def _from_cutover_result(
    result: CutoverMigrationResult,
    *,
    legacy_audit_index_path: Path | None,
    migrated_research_memory_paths: tuple[Path, ...],
) -> PMReviewWorkspaceMigrationResult:
    return PMReviewWorkspaceMigrationResult(
        schema_path=result.schema_path,
        baseline_path=result.baseline_path,
        baseline_payload=result.baseline_payload,
        legacy_audit_index_path=legacy_audit_index_path,
        migrated_research_memory_paths=migrated_research_memory_paths,
    )


def _normalize_target_keys(target_keys: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(target_keys, tuple):
        raise PMReviewWorkspaceMigrationError("target_keys must be a tuple.")
    normalized = tuple(
        validate_target_key(target_key, error_type=PMReviewWorkspaceMigrationError)
        for target_key in target_keys
    )
    if not normalized:
        raise PMReviewWorkspaceMigrationError("target_keys must not be empty.")
    if len(set(normalized)) != len(normalized):
        raise PMReviewWorkspaceMigrationError("target_keys must be unique.")
    return normalized


def _validate_layout(layout: WorkspaceLayout) -> None:
    if not isinstance(layout, WorkspaceLayout):
        raise PMReviewWorkspaceMigrationError("layout must be a WorkspaceLayout instance.")


def _legacy_audit_index_path(layout: WorkspaceLayout, migration_id: str) -> Path:
    return (
        layout.runtime_root
        / "migrations"
        / migration_id
        / "legacy_audit_index.json"
    ).resolve(strict=False)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        raise PMReviewWorkspaceMigrationError(
            f"failed to write PMReview migration artifact: {path}"
        ) from exc


__all__ = [
    "PMReviewWorkspaceMigrationError",
    "PMReviewWorkspaceMigrationResult",
    "prepare_pm_review_workspace",
    "validate_pm_review_workspace_ready",
]
