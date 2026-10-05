"""Filesystem helpers for operator repair plans."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from event_trader.operator.repair.contracts import OperatorRepairError
from event_trader.storage import WorkspaceLayout


@dataclass(frozen=True, slots=True)
class JsonlRecord:
    path: Path
    line_number: int
    line: str
    payload: dict[str, object]


def read_jsonl_records(path: Path) -> tuple[JsonlRecord, ...]:
    if not path.exists():
        return ()
    records: list[JsonlRecord] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OperatorRepairError(f"Invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(payload, dict):
            raise OperatorRepairError(
                f"JSONL payload must be an object at {path}:{line_number}"
            )
        records.append(
            JsonlRecord(
                path=path.resolve(strict=False),
                line_number=line_number,
                line=line,
                payload=payload,
            )
        )
    return tuple(records)


def rewrite_jsonl_removing_lines(
    *,
    path: Path,
    records: Sequence[JsonlRecord],
    line_numbers: set[int],
    backup_root: Path,
    layout: WorkspaceLayout,
) -> int:
    if not line_numbers:
        return 0
    kept_lines = [
        record.line
        for record in records
        if record.line_number not in line_numbers
    ]
    removed_count = len(records) - len(kept_lines)
    if removed_count == 0:
        return 0
    backup_file(path=path, backup_root=backup_root, layout=layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in kept_lines:
            handle.write(line)
            handle.write("\n")
    return removed_count


def backup_file(*, path: Path, backup_root: Path, layout: WorkspaceLayout) -> None:
    destination = backup_root / relative_to_root(path, layout.root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)


def backup_and_delete_directory(
    *,
    directory: Path,
    backup_root: Path,
    layout: WorkspaceLayout,
) -> None:
    destination = backup_root / relative_to_root(directory, layout.root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(directory, destination)
    shutil.rmtree(directory)


def relative_to_root(path: Path, root: Path) -> Path:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise OperatorRepairError(f"Repair path is outside workspace root: {path}") from exc


def json_value_mentions(value: object, needle: str) -> bool:
    if value == needle:
        return True
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(json_value_mentions(child, needle) for child in value.values())
    if isinstance(value, list | tuple):
        return any(json_value_mentions(child, needle) for child in value)
    return False
