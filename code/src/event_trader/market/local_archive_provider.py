"""Project-owned local archive market-data provider."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from event_trader.config import KernelConfig
from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    MarketMapping,
    ViewStateChangeContractError,
)
from event_trader.market.stock_bar_adapter import parse_stock_bar_granularity

_ARCHIVE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_EXPECTED_ADJUSTMENT_POLICY = "raw"
_EXPECTED_TIMESTAMP_POLICY = "end_at"
_REQUIRED_BAR_COLUMNS = (
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)


class LocalArchiveMarketDataProvider:
    """Read canonical bars from one formal local archive root."""

    def __init__(self, archive_root: Path) -> None:
        self._archive_root = archive_root.resolve(strict=False)
        self._manifest = _load_archive_manifest(self._archive_root)
        self._symbol = _manifest_text(self._manifest, "symbol")
        self._exchange = _manifest_text(self._manifest, "exchange")
        self._timezone = _manifest_text(self._manifest, "timezone")
        self._bar_granularity = _manifest_text(self._manifest, "bar_granularity")
        self._adjustment_policy = _manifest_text(self._manifest, "price_adjustment")
        self._timestamp_policy = _manifest_text(self._manifest, "timestamp_policy")
        self._bars_path = (
            self._archive_root / "bars" / self._bar_granularity / f"{self._symbol}.csv"
        )
        self._bar_delta = _validate_manifest_contract(
            archive_root=self._archive_root,
            bar_granularity=self._bar_granularity,
            adjustment_policy=self._adjustment_policy,
            timestamp_policy=self._timestamp_policy,
        )
        self._bars = _load_archive_bars(
            bars_path=self._bars_path,
            timezone_name=self._timezone,
            bar_delta=self._bar_delta,
        )
        self._source_metadata: dict[str, object] = {}

    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        normalized_start = _ensure_utc(start_at, field_name="start_at")
        normalized_end = _ensure_utc(end_at, field_name="end_at")
        if normalized_start >= normalized_end:
            raise ValueError("invalid window: start_at must be earlier than end_at.")

        archive_symbol = _archive_symbol_for_market_symbol(mapping.market_symbol)
        if archive_symbol != self._symbol:
            raise ValueError(
                "local_archive symbol mismatch: "
                f"requested {mapping.market_symbol!r}, archive provides {self._symbol!r}."
            )
        if mapping.exchange is not None and mapping.exchange.strip().upper() != self._exchange:
            raise ValueError(
                "local_archive exchange mismatch: "
                f"requested {mapping.exchange!r}, archive provides {self._exchange!r}."
            )
        if mapping.bar_granularity.strip().lower() != self._bar_granularity.lower():
            raise ValueError(
                "local_archive granularity mismatch: "
                "requested "
                f"{mapping.bar_granularity!r}, archive provides {self._bar_granularity!r}."
            )

        bars = tuple(
            bar
            for bar in self._bars
            if bar.end_at > normalized_start and bar.start_at < normalized_end
        )
        self._source_metadata = {
            "provider": "local_archive",
            "source_path": self._bars_path.resolve(strict=False).as_posix(),
            "symbol": self._symbol,
            "exchange": self._exchange,
            "timezone": self._timezone,
            "bar_granularity": self._bar_granularity,
            "adjustment_policy": self._adjustment_policy,
            "timestamp_policy": self._timestamp_policy,
            "returned_bar_count": len(bars),
        }
        if not bars:
            raise ValueError(
                "empty local archive bars for symbol "
                f"{self._symbol!r} in {self._bars_path.as_posix()}."
            )
        try:
            return MarketDataSeries(bars=bars)
        except ViewStateChangeContractError as exc:
            raise ValueError(
                f"invalid local archive bar series for {self._symbol!r}: {exc}"
            ) from exc

    def pop_source_metadata(self) -> dict[str, object]:
        metadata = dict(self._source_metadata)
        self._source_metadata.clear()
        return metadata


def build_local_archive_market_data_provider(
    config: KernelConfig,
) -> LocalArchiveMarketDataProvider:
    """Build the concrete local archive provider from validation config."""
    if config.validation is None:
        raise ValueError("local_archive provider requires validation config.")
    archive_root = config.validation.market_data.local_archive_root
    if archive_root is None:
        raise ValueError(
            "local_archive provider requires validation.market_data.local_archive_root."
        )
    return LocalArchiveMarketDataProvider(archive_root)


def _load_archive_manifest(archive_root: Path) -> dict[str, object]:
    manifest_path = archive_root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"local_archive manifest is missing: {manifest_path.as_posix()}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"local_archive manifest must be valid UTF-8 JSON: {manifest_path.as_posix()}"
        ) from exc
    if not isinstance(manifest, dict):
        raise ValueError(
            f"local_archive manifest must be a JSON object: {manifest_path.as_posix()}"
        )
    return manifest


def _manifest_text(manifest: dict[str, object], field_name: str) -> str:
    raw_value = manifest.get(field_name)
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError(f"local_archive manifest field {field_name!r} must be a non-empty string.")
    return raw_value.strip()


def _validate_manifest_contract(
    *,
    archive_root: Path,
    bar_granularity: str,
    adjustment_policy: str,
    timestamp_policy: str,
) -> timedelta:
    try:
        bar_delta = parse_stock_bar_granularity(bar_granularity)
    except ValueError as exc:
        raise ValueError(
            "local_archive manifest bar_granularity is invalid: "
            f"{bar_granularity!r}: {archive_root.as_posix()}"
        ) from exc
    if adjustment_policy != _EXPECTED_ADJUSTMENT_POLICY:
        raise ValueError(
            "local_archive manifest price_adjustment must be "
            f"{_EXPECTED_ADJUSTMENT_POLICY!r}: {archive_root.as_posix()}"
        )
    if timestamp_policy != _EXPECTED_TIMESTAMP_POLICY:
        raise ValueError(
            "local_archive manifest timestamp_policy must be "
            f"{_EXPECTED_TIMESTAMP_POLICY!r}: {archive_root.as_posix()}"
        )
    return bar_delta


def _load_archive_bars(
    *,
    bars_path: Path,
    timezone_name: str,
    bar_delta: timedelta,
) -> tuple[MarketDataBar, ...]:
    if not bars_path.is_file():
        raise ValueError(f"local_archive bars file is missing: {bars_path.as_posix()}")
    timezone = ZoneInfo(timezone_name)
    bars: list[MarketDataBar] = []
    previous_end_at: datetime | None = None
    with bars_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = tuple(reader.fieldnames or ())
        missing_columns = [name for name in _REQUIRED_BAR_COLUMNS if name not in fieldnames]
        if missing_columns:
            raise ValueError(
                "local_archive bars file is missing required columns "
                f"{missing_columns!r}: {bars_path.as_posix()}"
            )
        for line_number, row in enumerate(reader, start=2):
            bar = _parse_archive_bar_row(
                bars_path=bars_path,
                timezone=timezone,
                bar_delta=bar_delta,
                line_number=line_number,
                row=row,
            )
            if previous_end_at is not None:
                if bar.end_at == previous_end_at:
                    raise ValueError(
                        "local_archive bars file contains duplicate datetime "
                        f"at {bars_path.as_posix()}:{line_number}"
                    )
                if bar.end_at < previous_end_at:
                    raise ValueError(
                        "local_archive bars file must be sorted ascending by datetime: "
                        f"{bars_path.as_posix()}:{line_number}"
                    )
            bars.append(bar)
            previous_end_at = bar.end_at
    if not bars:
        raise ValueError(f"local_archive bars file is empty: {bars_path.as_posix()}")
    return tuple(bars)


def _parse_archive_bar_row(
    *,
    bars_path: Path,
    timezone: ZoneInfo,
    bar_delta: timedelta,
    line_number: int,
    row: dict[str, str | None],
) -> MarketDataBar:
    datetime_text = _required_row_value(
        row=row,
        bars_path=bars_path,
        line_number=line_number,
        column="datetime",
    )
    try:
        local_end_at = datetime.strptime(datetime_text, _ARCHIVE_TIME_FORMAT).replace(
            tzinfo=timezone
        )
    except ValueError as exc:
        raise ValueError(
            "local_archive bars file has invalid datetime "
            f"{datetime_text!r} at {bars_path.as_posix()}:{line_number}"
        ) from exc

    numeric_values = {
        column: _required_decimal_value(
            row=row,
            bars_path=bars_path,
            line_number=line_number,
            column=column,
        )
        for column in ("open", "high", "low", "close", "volume", "amount")
    }
    _ = numeric_values["amount"]
    start_at = (local_end_at - bar_delta).astimezone(UTC)
    end_at = local_end_at.astimezone(UTC)
    try:
        return MarketDataBar(
            start_at=start_at,
            end_at=end_at,
            open_price=float(numeric_values["open"]),
            high_price=float(numeric_values["high"]),
            low_price=float(numeric_values["low"]),
            close_price=float(numeric_values["close"]),
            volume=float(numeric_values["volume"]),
            vwap=None,
        )
    except ViewStateChangeContractError as exc:
        raise ValueError(
            "local_archive bars file has invalid OHLCV row "
            f"at {bars_path.as_posix()}:{line_number}: {exc}"
        ) from exc


def _required_row_value(
    *,
    row: dict[str, str | None],
    bars_path: Path,
    line_number: int,
    column: str,
) -> str:
    raw_value = row.get(column)
    if raw_value is None:
        raise ValueError(
            f"local_archive bars file is missing {column!r} at {bars_path.as_posix()}:{line_number}"
        )
    value = raw_value.strip()
    if not value:
        raise ValueError(
            f"local_archive bars file has blank {column!r} at {bars_path.as_posix()}:{line_number}"
        )
    return value


def _required_decimal_value(
    *,
    row: dict[str, str | None],
    bars_path: Path,
    line_number: int,
    column: str,
) -> Decimal:
    value = _required_row_value(
        row=row,
        bars_path=bars_path,
        line_number=line_number,
        column=column,
    )
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(
            "local_archive bars file has invalid numeric value for "
            f"{column!r} at {bars_path.as_posix()}:{line_number}: {value!r}"
        ) from exc


def _archive_symbol_for_market_symbol(market_symbol: str) -> str:
    normalized = market_symbol.strip().upper()
    if "." not in normalized:
        return normalized
    left, right = normalized.split(".", 1)
    if left in {"SH", "SZ"} and right:
        return f"{right}.{left}"
    return normalized


def _ensure_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


__all__ = [
    "LocalArchiveMarketDataProvider",
    "build_local_archive_market_data_provider",
]
