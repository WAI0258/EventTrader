"""File-backed stores for PM decisions and portfolio state."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from event_trader.contracts._validators import validate_target_key
from event_trader.execution.contracts import ExecutionRecord
from event_trader.portfolio.contracts import (
    PMDecision,
    PortfolioContractError,
    PortfolioState,
    parse_pm_decision,
    parse_portfolio_state,
    portfolio_contract_hash,
)
from event_trader.storage import WorkspaceLayout

_PM_DECISIONS_ROOT = Path("portfolio") / "pm-decisions"
_STATE_ROOT = Path("portfolio") / "state"


class PortfolioStoreError(ValueError):
    """Raised when portfolio persistence is invalid or conflicting."""


@dataclass(frozen=True, slots=True)
class PersistedPMDecision:
    record: PMDecision
    path: Path
    line_number: int
    record_hash: str


class PMDecisionStore:
    """Append-only PM decision store, idempotent by decision_id."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def append(self, decision: PMDecision) -> Path:
        if not isinstance(decision, PMDecision):
            raise PortfolioStoreError("decision must be a PMDecision instance.")
        path = self.path_for(decision.target_key, decision.business_at)
        record_hash = portfolio_contract_hash(decision.to_json_payload())
        for persisted in self.read_records(
            target_key=decision.target_key,
            year_month=decision.business_at.strftime("%Y-%m"),
        ):
            if persisted.record.decision_id != decision.decision_id:
                continue
            if persisted.record_hash == record_hash:
                return path
            raise PortfolioStoreError("duplicate decision_id has different payload.")
        _append_json_line(path, decision.to_json_payload())
        return path

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedPMDecision, ...]:
        return tuple(
            PersistedPMDecision(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=portfolio_contract_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_pm_decision_records(
                paths=_paths(
                    root=self._layout.runtime_root / _PM_DECISIONS_ROOT,
                    target_key=target_key,
                    year_month=year_month,
                ),
                target_key=target_key,
            )
        )

    def read_all_records(self) -> tuple[PersistedPMDecision, ...]:
        root = self._layout.runtime_root / _PM_DECISIONS_ROOT
        if not root.exists():
            return ()
        records: list[PersistedPMDecision] = []
        for target_root in sorted(path for path in root.iterdir() if path.is_dir()):
            records.extend(self.read_records(target_key=target_root.name))
        return tuple(
            sorted(
                records,
                key=lambda item: (
                    item.record.business_at,
                    item.path.as_posix(),
                    item.line_number,
                ),
            )
        )

    def path_for(self, target_key: str, business_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _PM_DECISIONS_ROOT,
            target_key=target_key,
            business_at=business_at,
        )


class PortfolioStateStore:
    """Current deterministic portfolio state by target."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        _validate_layout(layout)
        self._layout = layout

    def read(self, *, target_key: str) -> PortfolioState | None:
        path = self.path_for(target_key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PortfolioStoreError(f"portfolio state is unreadable: {path}") from exc
        if not isinstance(payload, Mapping):
            raise PortfolioStoreError(f"portfolio state must be an object: {path}")
        try:
            state = parse_portfolio_state(payload)
        except PortfolioContractError as exc:
            raise PortfolioStoreError(str(exc)) from exc
        normalized_target = validate_target_key(target_key, error_type=PortfolioStoreError)
        if state.target_key != normalized_target:
            raise PortfolioStoreError("portfolio state shard contains mixed target_key.")
        return state

    def read_all(self) -> dict[str, PortfolioState]:
        root = self._layout.runtime_root / _STATE_ROOT
        if not root.exists():
            return {}
        states: dict[str, PortfolioState] = {}
        for path in sorted(root.glob("*.json")):
            state = self.read(target_key=path.stem)
            if state is not None:
                states[path.stem] = state
        return states

    def apply_execution_record(
        self,
        *,
        decision: PMDecision,
        execution_record: ExecutionRecord,
    ) -> PortfolioState:
        if not isinstance(decision, PMDecision):
            raise PortfolioStoreError("decision must be a PMDecision instance.")
        if not isinstance(execution_record, ExecutionRecord):
            raise PortfolioStoreError("execution_record must be an ExecutionRecord instance.")
        if execution_record.status != "executed":
            raise PortfolioStoreError("only executed paper records may update portfolio state.")
        if execution_record.executed_at is None or execution_record.target_weight is None:
            raise PortfolioStoreError("executed records require executed_at and target_weight.")
        if execution_record.pm_decision_id != decision.decision_id:
            raise PortfolioStoreError(
                "execution_record.pm_decision_id must match decision.decision_id."
            )
        if execution_record.decision_episode_id != decision.decision_episode_id:
            raise PortfolioStoreError(
                "execution_record.decision_episode_id must match decision.decision_episode_id."
            )
        if execution_record.target_key != decision.target_key:
            raise PortfolioStoreError("execution_record.target_key must match decision.target_key.")
        if execution_record.target_weight != decision.requested_target_weight:
            raise PortfolioStoreError(
                "execution_record.target_weight must match PM decision target weight."
            )

        existing = self.read(target_key=decision.target_key)
        state = PortfolioState(
            target_key=decision.target_key,
            target_weight=execution_record.target_weight,
            state=decision.requested_state,
            updated_at=execution_record.executed_at,
            source_pm_decision_id=decision.decision_id,
            source_execution_record_id=execution_record.execution_record_id,
            decision_episode_id=decision.decision_episode_id,
        )
        if (
            existing is not None
            and existing.source_execution_record_id == execution_record.execution_record_id
        ):
            if portfolio_contract_hash(existing.to_json_payload()) == portfolio_contract_hash(
                state.to_json_payload()
            ):
                return existing
            raise PortfolioStoreError(
                "duplicate state update execution record has different payload."
            )
        _write_json_atomic(self.path_for(decision.target_key), state.to_json_payload())
        return state

    def path_for(self, target_key: str) -> Path:
        normalized_target = validate_target_key(target_key, error_type=PortfolioStoreError)
        return (self._layout.runtime_root / _STATE_ROOT / f"{normalized_target}.json").resolve(
            strict=False
        )


def _validate_layout(layout: WorkspaceLayout) -> None:
    if not isinstance(layout, WorkspaceLayout):
        raise PortfolioStoreError("layout must be a WorkspaceLayout instance.")


def _monthly_path(*, root: Path, target_key: str, business_at: datetime) -> Path:
    if not isinstance(business_at, datetime):
        raise PortfolioStoreError("business_at must be a datetime.")
    normalized_target = validate_target_key(target_key, error_type=PortfolioStoreError)
    return (root / normalized_target / f"{business_at.strftime('%Y-%m')}.jsonl").resolve(
        strict=False
    )


def _paths(*, root: Path, target_key: str, year_month: str | None) -> tuple[Path, ...]:
    normalized_target = validate_target_key(target_key, error_type=PortfolioStoreError)
    target_root = root / normalized_target
    if year_month is not None:
        if not isinstance(year_month, str) or len(year_month) != 7:
            raise PortfolioStoreError("year_month must be YYYY-MM.")
        return ((target_root / f"{year_month}.jsonl").resolve(strict=False),)
    if not target_root.exists():
        return ()
    return tuple(
        sorted(
            path.resolve(strict=False)
            for path in target_root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl")
            if path.is_file()
        )
    )


def _read_pm_decision_records(
    *,
    paths: tuple[Path, ...],
    target_key: str,
) -> tuple[tuple[PMDecision, Path, int], ...]:
    return tuple(
        (parse_pm_decision(payload), path, line_number)
        for payload, path, line_number in _read_jsonl_payloads(
            paths=paths,
            target_key=target_key,
            label="PM decision",
        )
    )


def _read_jsonl_payloads(
    *,
    paths: tuple[Path, ...],
    target_key: str,
    label: str,
) -> tuple[tuple[Mapping[str, object], Path, int], ...]:
    normalized_target = validate_target_key(target_key, error_type=PortfolioStoreError)
    records: list[tuple[Mapping[str, object], Path, int]] = []
    for path in paths:
        if not path.exists():
            continue
        if not path.is_file():
            raise PortfolioStoreError(f"{label} path must be a file: {path}")
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    raise PortfolioStoreError(
                        f"{label} line {line_number} is invalid JSON."
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise PortfolioStoreError(f"{label} line {line_number} must be an object.")
                if payload.get("target_key") != normalized_target:
                    raise PortfolioStoreError(f"{label} shard contains mixed target_key records.")
                records.append((payload, path, line_number))
    return tuple(records)


def _append_json_line(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(dict(payload), ensure_ascii=False))
            handle.write("\n")
    except OSError as exc:
        raise PortfolioStoreError(f"failed to append portfolio record: {path}") from exc


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        raise PortfolioStoreError(f"failed to write portfolio state: {path}") from exc


__all__ = [
    "PMDecisionStore",
    "PersistedPMDecision",
    "PortfolioStateStore",
    "PortfolioStoreError",
]
