"""Normalize repo-staged local market data into the formal local archive contract."""

from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, TextIO
from zoneinfo import ZoneInfo

_DEFAULT_INPUT_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_OUTPUT_BAR_COLUMNS = (
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)
_REQUIRED_RAW_BAR_COLUMNS = (
    "datetime",
    "code",
    "open",
    "close",
    "high",
    "low",
    "volume",
    "amount",
)


class LocalArchiveBuildError(RuntimeError):
    """Raised when staged raw intake cannot be normalized into a local archive."""


@dataclass(frozen=True, slots=True)
class LocalArchiveBuildSpec:
    source_root: Path
    archive_root: Path
    symbol: str
    exchange: str
    timezone: str
    bar_granularity: str
    price_adjustment: str = "raw"
    timestamp_policy: str = "end_at"
    input_datetime_format: str = _DEFAULT_INPUT_DATETIME_FORMAT

    @property
    def bars_source_root(self) -> Path:
        return self.source_root / f"bars_{self.bar_granularity}_by_day"

    @property
    def forward_adjustments_source(self) -> Path:
        return self.source_root / "adjustments" / f"{self.symbol}.forward.csv"

    @property
    def backward_adjustments_source(self) -> Path:
        return self.source_root / "adjustments" / f"{self.symbol}.backward.csv"

    @property
    def bars_output_path(self) -> Path:
        return self.archive_root / "bars" / self.bar_granularity / f"{self.symbol}.csv"

    @property
    def manifest_path(self) -> Path:
        return self.archive_root / "manifest.json"

    @property
    def forward_adjustments_output(self) -> Path:
        return self.archive_root / "adjustments" / "forward" / f"{self.symbol}.csv"

    @property
    def backward_adjustments_output(self) -> Path:
        return self.archive_root / "adjustments" / "backward" / f"{self.symbol}.csv"


@dataclass(frozen=True, slots=True)
class LocalArchiveBuildReceipt:
    archive_root: Path
    bars_path: Path
    manifest_path: Path
    source_root: Path
    coverage_start: datetime
    coverage_end: datetime
    bar_count: int
    identical_duplicates_collapsed: int


@dataclass(frozen=True, slots=True)
class _NormalizedBarRow:
    datetime_text: str
    local_end_at: datetime
    open_price: str
    high_price: str
    low_price: str
    close_price: str
    volume: str
    amount: str

    def as_csv_row(self) -> dict[str, str]:
        return {
            "datetime": self.datetime_text,
            "open": self.open_price,
            "high": self.high_price,
            "low": self.low_price,
            "close": self.close_price,
            "volume": self.volume,
            "amount": self.amount,
        }


def build_local_archive(spec: LocalArchiveBuildSpec) -> LocalArchiveBuildReceipt:
    if not spec.bars_source_root.is_dir():
        raise LocalArchiveBuildError(
            f"Missing staged bars source directory: {spec.bars_source_root.as_posix()}"
        )
    _require_file(spec.forward_adjustments_source)
    _require_file(spec.backward_adjustments_source)

    rows_by_datetime: dict[datetime, _NormalizedBarRow] = {}
    identical_duplicates_collapsed = 0
    bar_files = tuple(sorted(spec.bars_source_root.glob("*.csv")))
    if not bar_files:
        raise LocalArchiveBuildError(
            f"No staged {spec.bar_granularity} bar files found in "
            f"{spec.bars_source_root.as_posix()}"
        )

    for path in bar_files:
        duplicate_count = _read_raw_bar_file(path=path, spec=spec, rows_by_datetime=rows_by_datetime)
        identical_duplicates_collapsed += duplicate_count

    if not rows_by_datetime:
        raise LocalArchiveBuildError(
            f"No bar rows were loaded from {spec.bars_source_root.as_posix()}"
        )

    normalized_rows = tuple(sorted(rows_by_datetime.values(), key=lambda row: row.local_end_at))
    timezone = ZoneInfo(spec.timezone)
    coverage_start = normalized_rows[0].local_end_at.replace(tzinfo=timezone)
    coverage_end = normalized_rows[-1].local_end_at.replace(tzinfo=timezone)

    _write_bar_file(spec.bars_output_path, normalized_rows)
    _copy_companion_file(spec.forward_adjustments_source, spec.forward_adjustments_output)
    _copy_companion_file(spec.backward_adjustments_source, spec.backward_adjustments_output)
    _write_manifest(
        spec=spec,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        bar_count=len(normalized_rows),
    )

    return LocalArchiveBuildReceipt(
        archive_root=spec.archive_root,
        bars_path=spec.bars_output_path,
        manifest_path=spec.manifest_path,
        source_root=spec.source_root,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        bar_count=len(normalized_rows),
        identical_duplicates_collapsed=identical_duplicates_collapsed,
    )


def _require_file(path: Path) -> None:
    if not path.is_file():
        raise LocalArchiveBuildError(f"Missing staged source file: {path.as_posix()}")


def _read_raw_bar_file(
    *,
    path: Path,
    spec: LocalArchiveBuildSpec,
    rows_by_datetime: dict[datetime, _NormalizedBarRow],
) -> int:
    identical_duplicates_collapsed = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise LocalArchiveBuildError(
                f"Bar file is missing a header row: {path.as_posix()}"
            )
        missing_columns = [
            column for column in _REQUIRED_RAW_BAR_COLUMNS if column not in reader.fieldnames
        ]
        if missing_columns:
            raise LocalArchiveBuildError(
                "Bar file is missing required columns "
                f"{missing_columns!r}: {path.as_posix()}"
            )
        for line_number, row in enumerate(reader, start=2):
            normalized = _normalize_raw_bar_row(
                path=path,
                line_number=line_number,
                row=row,
                spec=spec,
            )
            existing = rows_by_datetime.get(normalized.local_end_at)
            if existing is None:
                rows_by_datetime[normalized.local_end_at] = normalized
                continue
            if existing == normalized:
                identical_duplicates_collapsed += 1
                continue
            raise LocalArchiveBuildError(
                "Conflicting duplicate datetime "
                f"{normalized.datetime_text} encountered in {path.as_posix()}:{line_number}"
            )
    return identical_duplicates_collapsed


def _normalize_raw_bar_row(
    *,
    path: Path,
    line_number: int,
    row: dict[str, str | None],
    spec: LocalArchiveBuildSpec,
) -> _NormalizedBarRow:
    values = {
        key: _required_row_value(path=path, line_number=line_number, row=row, column=key)
        for key in _REQUIRED_RAW_BAR_COLUMNS
    }
    if values["code"].strip().upper() != spec.symbol.strip().upper():
        raise LocalArchiveBuildError(
            "Unexpected symbol in staged bar row "
            f"{path.as_posix()}:{line_number}: expected {spec.symbol}, got {values['code']}"
        )

    local_end_at = _parse_local_datetime(
        path=path,
        line_number=line_number,
        value=values["datetime"],
        spec=spec,
    )
    for column in ("open", "high", "low", "close", "volume", "amount"):
        _validate_decimal(
            path=path,
            line_number=line_number,
            column=column,
            value=values[column],
        )

    return _NormalizedBarRow(
        datetime_text=local_end_at.strftime(spec.input_datetime_format),
        local_end_at=local_end_at,
        open_price=values["open"],
        high_price=values["high"],
        low_price=values["low"],
        close_price=values["close"],
        volume=values["volume"],
        amount=values["amount"],
    )


def _required_row_value(
    *,
    path: Path,
    line_number: int,
    row: dict[str, str | None],
    column: str,
) -> str:
    raw_value = row.get(column)
    if raw_value is None:
        raise LocalArchiveBuildError(
            f"Missing column {column!r} in {path.as_posix()}:{line_number}"
        )
    value = raw_value.strip()
    if not value:
        raise LocalArchiveBuildError(
            f"Blank {column!r} value in {path.as_posix()}:{line_number}"
        )
    return value


def _parse_local_datetime(
    *,
    path: Path,
    line_number: int,
    value: str,
    spec: LocalArchiveBuildSpec,
) -> datetime:
    try:
        return datetime.strptime(value, spec.input_datetime_format)
    except ValueError as exc:
        raise LocalArchiveBuildError(
            f"Invalid datetime {value!r} in {path.as_posix()}:{line_number}"
        ) from exc


def _validate_decimal(
    *,
    path: Path,
    line_number: int,
    column: str,
    value: str,
) -> None:
    try:
        Decimal(value)
    except InvalidOperation as exc:
        raise LocalArchiveBuildError(
            f"Invalid numeric value for {column!r} in {path.as_posix()}:{line_number}: {value!r}"
        ) from exc


def _write_bar_file(path: Path, rows: Sequence[_NormalizedBarRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(handle: TextIO) -> None:
        writer = csv.DictWriter(handle, fieldnames=_OUTPUT_BAR_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_csv_row())

    _atomic_write_csv(path, write)


def _copy_companion_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "wb",
        delete=False,
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    ) as handle:
        temp_path = Path(handle.name)
    try:
        shutil.copyfile(source, temp_path)
        os.replace(temp_path, destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_manifest(
    *,
    spec: LocalArchiveBuildSpec,
    coverage_start: datetime,
    coverage_end: datetime,
    bar_count: int,
) -> None:
    spec.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "bar_count": bar_count,
        "bar_granularity": spec.bar_granularity,
        "coverage_end": coverage_end.isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "exchange": spec.exchange,
        "price_adjustment": spec.price_adjustment,
        "source_root": _format_repo_relative(spec.source_root),
        "symbol": spec.symbol,
        "timestamp_policy": spec.timestamp_policy,
        "timezone": spec.timezone,
    }
    _atomic_write_text(
        spec.manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _atomic_write_csv(path: Path, write: Callable[[TextIO], None]) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            write(handle)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _atomic_write_text(path: Path, content: str) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _format_repo_relative(path: Path) -> str:
    repo_root = _repo_root()
    try:
        return path.resolve(strict=False).relative_to(repo_root).as_posix()
    except ValueError:
        return path.resolve(strict=False).as_posix()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


__all__ = [
    "LocalArchiveBuildError",
    "LocalArchiveBuildReceipt",
    "LocalArchiveBuildSpec",
    "build_local_archive",
]
