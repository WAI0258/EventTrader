"""Append-only store for exposure-blind analysis assessments."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from event_trader.contracts._validators import validate_target_key
from event_trader.contracts.analysis_assessment import (
    AnalysisAssessment,
    AnalysisAssessmentContractError,
    parse_analysis_assessment,
)
from event_trader.storage import WorkspaceLayout

_ROOT = Path("analysis_assessments")


class AnalysisAssessmentStoreError(ValueError):
    """Raised when analysis assessment persistence is invalid or conflicting."""


@dataclass(frozen=True, slots=True)
class PersistedAnalysisAssessment:
    record: AnalysisAssessment
    path: Path
    line_number: int
    record_hash: str


class AnalysisAssessmentStore:
    """Append and read AnalysisAssessment records by target/month."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise AnalysisAssessmentStoreError("layout must be a WorkspaceLayout instance.")
        self._layout = layout

    def append(self, assessment: AnalysisAssessment) -> Path:
        path, already_persisted = self._appendability(assessment)
        if already_persisted:
            return path
        _append_json_line(path, assessment.to_json_payload())
        return path

    def validate_appendable(self, assessment: AnalysisAssessment) -> None:
        """Raise before side effects when this assessment cannot be appended."""
        self._appendability(assessment)

    def _appendability(self, assessment: AnalysisAssessment) -> tuple[Path, bool]:
        if not isinstance(assessment, AnalysisAssessment):
            raise AnalysisAssessmentStoreError(
                "assessment must be an AnalysisAssessment instance."
            )
        path = self.path_for(assessment.target_key, assessment.business_at)
        record_hash = _record_hash(assessment.to_json_payload())
        for persisted in self.read_records(
            target_key=assessment.target_key,
            year_month=assessment.business_at.strftime("%Y-%m"),
        ):
            if persisted.record.assessment_id != assessment.assessment_id:
                continue
            if persisted.record_hash == record_hash:
                return path, True
            raise AnalysisAssessmentStoreError(
                "duplicate assessment_id has different payload."
            )
        return path, False

    def read_records(
        self,
        *,
        target_key: str,
        year_month: str | None = None,
    ) -> tuple[PersistedAnalysisAssessment, ...]:
        return tuple(
            PersistedAnalysisAssessment(
                record=record,
                path=path,
                line_number=line_number,
                record_hash=_record_hash(record.to_json_payload()),
            )
            for record, path, line_number in _read_records(
                paths=_paths(
                    root=self._layout.runtime_root / _ROOT,
                    target_key=target_key,
                    year_month=year_month,
                ),
                target_key=target_key,
            )
        )

    def path_for(self, target_key: str, business_at: datetime) -> Path:
        return _monthly_path(
            root=self._layout.runtime_root / _ROOT,
            target_key=target_key,
            business_at=business_at,
        )


def _read_records(
    *,
    paths: tuple[Path, ...],
    target_key: str,
) -> tuple[tuple[AnalysisAssessment, Path, int], ...]:
    records: list[tuple[AnalysisAssessment, Path, int]] = []
    for payload, path, line_number in _read_payloads(
        paths=paths,
        target_key=target_key,
        label="analysis assessment",
    ):
        try:
            records.append(
                (
                    parse_analysis_assessment(
                        payload,
                        allow_legacy_pm_trigger_fields=True,
                    ),
                    path,
                    line_number,
                )
            )
        except AnalysisAssessmentContractError as exc:
            raise AnalysisAssessmentStoreError(str(exc)) from exc
    return tuple(records)


def _monthly_path(*, root: Path, target_key: str, business_at: datetime) -> Path:
    if not isinstance(business_at, datetime):
        raise AnalysisAssessmentStoreError("business_at must be a datetime.")
    normalized_target = validate_target_key(
        target_key,
        error_type=AnalysisAssessmentStoreError,
    )
    return (
        root / normalized_target / f"{business_at.strftime('%Y-%m')}.jsonl"
    ).resolve(strict=False)


def _paths(*, root: Path, target_key: str, year_month: str | None) -> tuple[Path, ...]:
    normalized_target = validate_target_key(
        target_key,
        error_type=AnalysisAssessmentStoreError,
    )
    target_root = root / normalized_target
    if year_month is not None:
        if not isinstance(year_month, str) or len(year_month) != 7:
            raise AnalysisAssessmentStoreError("year_month must be YYYY-MM.")
        return ((target_root / f"{year_month}.jsonl").resolve(strict=False),)
    return tuple(sorted(target_root.glob("*.jsonl")))


def _read_payloads(
    *,
    paths: tuple[Path, ...],
    target_key: str,
    label: str,
) -> tuple[tuple[Mapping[str, object], Path, int], ...]:
    normalized_target = validate_target_key(
        target_key,
        error_type=AnalysisAssessmentStoreError,
    )
    records: list[tuple[Mapping[str, object], Path, int]] = []
    for path in paths:
        if not path.exists():
            continue
        if not path.is_file():
            raise AnalysisAssessmentStoreError(f"{label} path must be a file: {path}")
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    raise AnalysisAssessmentStoreError(
                        f"{label} line {line_number} is invalid JSON."
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise AnalysisAssessmentStoreError(
                        f"{label} line {line_number} must be an object."
                    )
                if payload.get("target_key") != normalized_target:
                    raise AnalysisAssessmentStoreError(
                        f"{label} shard contains mixed target_key records."
                    )
                records.append((payload, path, line_number))
    return tuple(records)


def _append_json_line(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(dict(payload), ensure_ascii=False))
            handle.write("\n")
    except OSError as exc:
        raise AnalysisAssessmentStoreError(
            f"failed to append analysis assessment record: {path}"
        ) from exc


def _record_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "AnalysisAssessmentStore",
    "AnalysisAssessmentStoreError",
    "PersistedAnalysisAssessment",
]
