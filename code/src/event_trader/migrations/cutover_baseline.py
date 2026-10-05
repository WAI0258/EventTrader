"""Cutover baseline portfolio migration for the PMReview architecture."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from event_trader.contracts._validators import validate_target_key
from event_trader.contracts.view_state_change import (
    ViewState,
    canonical_view_state_target_weight,
)
from event_trader.execution.contracts import ExecutionRecord
from event_trader.execution.store import ExecutionRecordStore
from event_trader.portfolio.contracts import PMDecision, PortfolioState
from event_trader.portfolio.store import PMDecisionStore, PortfolioStateStore
from event_trader.storage import WorkspaceLayout

CUTOVER_RUNTIME_SCHEMA_VERSION = "pm_review_refactor_runtime_v1"
DEFAULT_CUTOVER_MIGRATION_ID = "pm_review_refactor_cutover_baseline_v1"


class CutoverMigrationError(ValueError):
    """Raised when the cutover baseline cannot be produced or validated."""


@dataclass(frozen=True, slots=True)
class CutoverMigrationResult:
    schema_path: Path
    baseline_path: Path
    baseline_payload: dict[str, object]


def run_cutover_baseline_migration(
    *,
    layout: WorkspaceLayout,
    target_keys: tuple[str, ...],
    migrated_at: datetime,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
    schema_version: str = CUTOVER_RUNTIME_SCHEMA_VERSION,
) -> CutoverMigrationResult:
    """Write schema marker plus one cutover baseline portfolio artifact."""

    _validate_layout(layout)
    normalized_targets = _normalize_target_keys(target_keys)
    _validate_non_blank(migration_id, "migration_id")
    _validate_non_blank(schema_version, "schema_version")
    if not isinstance(migrated_at, datetime) or migrated_at.tzinfo is None:
        raise CutoverMigrationError("migrated_at must be a timezone-aware datetime.")

    baseline_records = [
        _baseline_record_for_target(layout=layout, target_key=target_key)
        for target_key in normalized_targets
    ]
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "migration_id": migration_id,
        "migrated_at": migrated_at.isoformat(),
        "baseline_kind": "cutover_portfolio_state",
        "targets": baseline_records,
    }
    schema_payload = {
        "schema_version": schema_version,
        "migration_id": migration_id,
        "cutover_baseline_path": _baseline_path(layout, migration_id).relative_to(
            layout.runtime_root
        ).as_posix(),
    }
    schema_path = _schema_path(layout)
    baseline_path = _baseline_path(layout, migration_id)
    _write_json_atomic(schema_path, schema_payload)
    _write_json_atomic(baseline_path, payload)
    validate_cutover_baseline(
        layout=layout,
        migration_id=migration_id,
        expected_schema_version=schema_version,
    )
    return CutoverMigrationResult(
        schema_path=schema_path,
        baseline_path=baseline_path,
        baseline_payload=payload,
    )


def validate_cutover_baseline(
    *,
    layout: WorkspaceLayout,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
    expected_schema_version: str = CUTOVER_RUNTIME_SCHEMA_VERSION,
) -> None:
    """Validate that runtime schema and cutover baseline artifacts are present."""

    _validate_layout(layout)
    schema_path = _schema_path(layout)
    baseline_path = _baseline_path(layout, migration_id)
    if not schema_path.exists():
        raise CutoverMigrationError("runtime schema marker is missing.")
    if not baseline_path.exists():
        raise CutoverMigrationError("cutover baseline portfolio state is missing.")
    schema = _read_json_object(schema_path, "runtime schema marker")
    baseline = _read_json_object(baseline_path, "cutover baseline")
    schema_version = schema.get("schema_version")
    if schema_version != expected_schema_version:
        raise CutoverMigrationError("runtime schema marker version mismatch.")
    if baseline.get("schema_version") != expected_schema_version:
        raise CutoverMigrationError("cutover baseline schema version mismatch.")
    if schema.get("migration_id") != migration_id or baseline.get("migration_id") != migration_id:
        raise CutoverMigrationError("cutover migration id mismatch.")
    targets = baseline.get("targets")
    if not isinstance(targets, list):
        raise CutoverMigrationError("cutover baseline targets must be a list.")
    for item in targets:
        if not isinstance(item, dict):
            raise CutoverMigrationError("cutover baseline target entries must be objects.")
        _validate_baseline_target_payload(item)


def read_cutover_baseline_payload(
    *,
    layout: WorkspaceLayout,
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
    expected_schema_version: str = CUTOVER_RUNTIME_SCHEMA_VERSION,
) -> dict[str, object]:
    """Read a validated cutover baseline payload for runtime boundary helpers."""

    validate_cutover_baseline(
        layout=layout,
        migration_id=migration_id,
        expected_schema_version=expected_schema_version,
    )
    return _read_json_object(_baseline_path(layout, migration_id), "cutover baseline")


def _baseline_record_for_target(*, layout: WorkspaceLayout, target_key: str) -> dict[str, object]:
    portfolio_state = PortfolioStateStore(layout).read(target_key=target_key)
    if portfolio_state is not None:
        return _portfolio_state_baseline(portfolio_state)

    execution_baseline = _latest_execution_baseline(layout=layout, target_key=target_key)
    if execution_baseline is not None:
        return execution_baseline

    return _flat_baseline(target_key)


def _portfolio_state_baseline(state: PortfolioState) -> dict[str, object]:
    return {
        "target_key": state.target_key,
        "state": state.state,
        "target_weight": state.target_weight,
        "source_type": "portfolio_state",
        "confidence": "high",
        "source_ids": {
            "source_pm_decision_id": state.source_pm_decision_id,
            "source_execution_record_id": state.source_execution_record_id,
            "decision_episode_id": state.decision_episode_id,
        },
        "source_updated_at": state.updated_at.isoformat(),
    }


def _latest_execution_baseline(
    *,
    layout: WorkspaceLayout,
    target_key: str,
) -> dict[str, object] | None:
    execution_records = tuple(
        persisted.record
        for persisted in ExecutionRecordStore(layout).read_records(target_key=target_key)
        if persisted.record.status == "executed"
        and persisted.record.executed_at is not None
        and persisted.record.target_weight is not None
    )
    if not execution_records:
        return None
    pm_decisions = {
        persisted.record.decision_id: persisted.record
        for persisted in PMDecisionStore(layout).read_records(target_key=target_key)
    }
    execution_record = sorted(
        execution_records,
        key=lambda item: (item.executed_at or item.business_at, item.execution_record_id),
    )[-1]
    decision = pm_decisions.get(execution_record.pm_decision_id)
    if decision is None or not _execution_chain_matches(
        execution_record=execution_record,
        decision=decision,
    ):
        raise CutoverMigrationError(
            "latest executed ExecutionRecord cannot be matched to a deterministic "
            "PMDecision record."
        )
    return {
        "target_key": execution_record.target_key,
        "state": decision.requested_state,
        "target_weight": execution_record.target_weight,
        "source_type": "execution_record",
        "confidence": "high",
        "source_ids": {
            "source_pm_decision_id": decision.decision_id,
            "source_execution_record_id": execution_record.execution_record_id,
            "decision_episode_id": execution_record.decision_episode_id,
        },
        "source_updated_at": (
            execution_record.executed_at or execution_record.business_at
        ).isoformat(),
    }


def _execution_chain_matches(
    *,
    execution_record: ExecutionRecord,
    decision: PMDecision,
) -> bool:
    return (
        execution_record.pm_decision_id == decision.decision_id
        and execution_record.decision_episode_id == decision.decision_episode_id
        and execution_record.target_key == decision.target_key
        and execution_record.target_weight == decision.requested_target_weight
    )


def _flat_baseline(target_key: str) -> dict[str, object]:
    return {
        "target_key": target_key,
        "state": "flat",
        "target_weight": 0.0,
        "source_type": "explicit_flat",
        "confidence": "high",
        "source_ids": {},
        "source_updated_at": None,
    }


def _validate_baseline_target_payload(payload: dict[str, object]) -> None:
    target_key = payload.get("target_key")
    if not isinstance(target_key, str) or not target_key.strip():
        raise CutoverMigrationError("baseline target_key must be non-blank.")
    state = payload.get("state")
    if state not in {"flat", "weak_long", "strong_long", "weak_short", "strong_short"}:
        raise CutoverMigrationError("baseline state is invalid.")
    target_weight = payload.get("target_weight")
    if not isinstance(target_weight, int | float) or isinstance(target_weight, bool):
        raise CutoverMigrationError("baseline target_weight must be numeric.")
    if float(target_weight) != canonical_view_state_target_weight(cast(ViewState, state)):
        raise CutoverMigrationError("baseline target_weight does not match state.")
    if payload.get("source_type") not in {
        "portfolio_state",
        "execution_record",
        "explicit_flat",
    }:
        raise CutoverMigrationError("baseline source_type is invalid.")
    if payload.get("confidence") not in {"high", "medium", "low"}:
        raise CutoverMigrationError("baseline confidence is invalid.")
    if not isinstance(payload.get("source_ids"), dict):
        raise CutoverMigrationError("baseline source_ids must be an object.")


def _schema_path(layout: WorkspaceLayout) -> Path:
    return (layout.runtime_root / "schema.json").resolve(strict=False)


def _baseline_path(layout: WorkspaceLayout, migration_id: str) -> Path:
    return (
        layout.runtime_root
        / "migrations"
        / migration_id
        / "baseline_portfolio_state.json"
    ).resolve(strict=False)


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CutoverMigrationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise CutoverMigrationError(f"{label} must be a JSON object.")
    return payload


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
        raise CutoverMigrationError(f"failed to write migration artifact: {path}") from exc


def _validate_layout(layout: WorkspaceLayout) -> None:
    if not isinstance(layout, WorkspaceLayout):
        raise CutoverMigrationError("layout must be a WorkspaceLayout instance.")


def _validate_non_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CutoverMigrationError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _normalize_target_keys(target_keys: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(target_keys, tuple) or not target_keys:
        raise CutoverMigrationError("target_keys must be a non-empty tuple.")
    seen: set[str] = set()
    normalized: list[str] = []
    for target_key in target_keys:
        text = validate_target_key(target_key, error_type=CutoverMigrationError)
        if text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return tuple(normalized)


__all__ = [
    "CUTOVER_RUNTIME_SCHEMA_VERSION",
    "DEFAULT_CUTOVER_MIGRATION_ID",
    "CutoverMigrationError",
    "CutoverMigrationResult",
    "read_cutover_baseline_payload",
    "run_cutover_baseline_migration",
    "validate_cutover_baseline",
]
