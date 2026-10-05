"""Price-adjustment helpers for archive-backed and cached-store market bars."""

from __future__ import annotations

from bisect import bisect_right
import csv
import json
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from event_trader.contracts.view_state_change import MarketDataBar, MarketDataSeries

MarketDataAdjustmentPolicy = Literal[
    "forward_adjusted_visible",
    "forward_adjusted_realized",
]
AdjustmentDirection = Literal["forward", "backward"]
_FORWARD_POLICIES = frozenset({"forward_adjusted_visible", "forward_adjusted_realized"})
_SUPPORTED_POLICIES = frozenset(_FORWARD_POLICIES)
_DATE_COLUMN_ALIASES = ("交易日期", "浜ゆ槗鏃ユ湡")
_FACTOR_COLUMN_ALIASES = ("复权因子", "澶嶆潈鍥犲瓙")


class MarketAdjustmentError(ValueError):
    """Raised when configured market-data adjustments cannot be loaded or applied."""


@dataclass(frozen=True, slots=True)
class MarketAdjustmentSidecar:
    """Daily factor sidecar loaded from a normalized archive or replay store."""

    symbol: str
    policy: MarketDataAdjustmentPolicy
    direction: AdjustmentDirection
    timezone_name: str
    source_kind: Literal["local_archive", "replay_store", "live_store"]
    source_path: Path
    factors_by_date: dict[date, float]
    trading_dates: tuple[date, ...]

    def factor_for_timestamp(self, timestamp: datetime) -> float:
        trading_date = timestamp.astimezone(ZoneInfo(self.timezone_name)).date()
        factor_date = _factor_date_on_or_before(
            trading_dates=self.trading_dates,
            requested_date=trading_date,
        )
        factor = None if factor_date is None else self.factors_by_date.get(factor_date)
        if factor is None:
            raise MarketAdjustmentError(
                "missing adjustment factor for "
                f"{self.symbol} on {trading_date.isoformat()} in {self.source_path.as_posix()}."
            )
        if factor <= 0.0:
            raise MarketAdjustmentError(
                "adjustment factor must be positive for "
                f"{self.symbol} on {trading_date.isoformat()}."
            )
        return factor


@dataclass(frozen=True, slots=True)
class AppliedAdjustment:
    """Adjusted series plus audit metadata."""

    series: MarketDataSeries
    metadata: dict[str, object]


def normalize_adjustment_policy(
    value: object,
    *,
    field_name: str,
) -> MarketDataAdjustmentPolicy:
    if not isinstance(value, str):
        raise MarketAdjustmentError(f"{field_name} must be a string.")
    normalized = value.strip()
    if normalized not in _SUPPORTED_POLICIES:
        allowed = ", ".join(sorted(_SUPPORTED_POLICIES))
        raise MarketAdjustmentError(f"{field_name} must be one of: {allowed}.")
    return normalized  # type: ignore[return-value]


def adjustment_direction_for_policy(
    policy: MarketDataAdjustmentPolicy,
) -> AdjustmentDirection:
    if policy in _FORWARD_POLICIES:
        return "forward"
    raise MarketAdjustmentError(f"unsupported market-data adjustment policy: {policy!r}.")


def normalize_archive_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." not in normalized:
        return normalized
    left, right = normalized.split(".", 1)
    if left in {"SH", "SZ", "US", "HK"} and right:
        return f"{right}.{left}"
    return normalized


def load_adjustment_sidecar(
    *,
    root: Path,
    market_symbol: str,
    policy: MarketDataAdjustmentPolicy,
    timezone_name: str,
    source_kind: Literal["local_archive", "replay_store", "live_store"],
) -> MarketAdjustmentSidecar:
    direction = adjustment_direction_for_policy(policy)
    archive_symbol = normalize_archive_symbol(market_symbol)
    source_path = (
        root.resolve(strict=False) / "adjustments" / direction / f"{archive_symbol}.csv"
    )
    if not source_path.is_file():
        raise MarketAdjustmentError(
            "adjustment sidecar is missing for "
            f"{archive_symbol}: {source_path.as_posix()}."
        )
    factors_by_date: dict[date, float] = {}
    with source_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = frozenset(reader.fieldnames or ())
        missing_columns = []
        if not _has_any_alias(fieldnames, _DATE_COLUMN_ALIASES):
            missing_columns.append("交易日期")
        if not _has_any_alias(fieldnames, _FACTOR_COLUMN_ALIASES):
            missing_columns.append("复权因子")
        if missing_columns:
            raise MarketAdjustmentError(
                "adjustment sidecar is missing required columns "
                f"{missing_columns!r}: {source_path.as_posix()}."
            )
        for line_number, row in enumerate(reader, start=2):
            trading_date = _parse_factor_date(
                _row_value(row, _DATE_COLUMN_ALIASES),
                source_path=source_path,
                line_number=line_number,
            )
            factor = _parse_factor_value(
                _row_value(row, _FACTOR_COLUMN_ALIASES),
                source_path=source_path,
                line_number=line_number,
            )
            factors_by_date[trading_date] = factor
    if not factors_by_date:
        raise MarketAdjustmentError(
            f"adjustment sidecar is empty: {source_path.as_posix()}."
        )
    trading_dates = tuple(sorted(factors_by_date))
    return MarketAdjustmentSidecar(
        symbol=archive_symbol,
        policy=policy,
        direction=direction,
        timezone_name=timezone_name,
        source_kind=source_kind,
        source_path=source_path,
        factors_by_date=factors_by_date,
        trading_dates=trading_dates,
    )


def load_adjustment_sidecar_from_archive_root(
    *,
    archive_root: Path,
    market_symbol: str,
    policy: MarketDataAdjustmentPolicy,
) -> MarketAdjustmentSidecar:
    manifest_path = archive_root.resolve(strict=False) / "manifest.json"
    if not manifest_path.is_file():
        raise MarketAdjustmentError(
            f"archive manifest is missing: {manifest_path.as_posix()}."
        )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise MarketAdjustmentError(
            f"archive manifest must be a JSON object: {manifest_path.as_posix()}."
        )
    timezone_name = payload.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise MarketAdjustmentError(
            f"archive manifest timezone is missing: {manifest_path.as_posix()}."
        )
    return load_adjustment_sidecar(
        root=archive_root,
        market_symbol=market_symbol,
        policy=policy,
        timezone_name=timezone_name.strip(),
        source_kind="local_archive",
    )


def load_adjustment_sidecar_from_source_metadata(
    *,
    market_symbol: str,
    policy: MarketDataAdjustmentPolicy,
    source_metadata: dict[str, object],
) -> MarketAdjustmentSidecar:
    timezone_name = _metadata_timezone_name(source_metadata)
    if timezone_name is None:
        raise MarketAdjustmentError(
            f"missing timezone metadata for adjusted symbol {market_symbol!r}."
        )
    store_root = _metadata_path(source_metadata, "cached_store_root")
    if store_root is not None:
        return load_adjustment_sidecar(
            root=store_root,
            market_symbol=market_symbol,
            policy=policy,
            timezone_name=timezone_name,
            source_kind=_metadata_store_source_kind(source_metadata),
        )
    source_path = _metadata_path(source_metadata, "source_path")
    if source_path is None:
        raise MarketAdjustmentError(
            "missing source_path or cached_store_root for "
            f"adjusted symbol {market_symbol!r}."
        )
    archive_root = source_path.parent.parent.parent
    return load_adjustment_sidecar(
        root=archive_root,
        market_symbol=market_symbol,
        policy=policy,
        timezone_name=timezone_name,
        source_kind="local_archive",
    )


def apply_adjustment_policy_to_series(
    *,
    series: MarketDataSeries,
    sidecar: MarketAdjustmentSidecar,
    reference_at: datetime | None = None,
) -> AppliedAdjustment:
    adjusted_bars, metadata = apply_adjustment_policy_to_bars(
        bars=series.bars,
        sidecar=sidecar,
        reference_at=reference_at,
    )
    return AppliedAdjustment(
        series=MarketDataSeries(bars=adjusted_bars),
        metadata=metadata,
    )


def apply_adjustment_policy_to_bars(
    *,
    bars: tuple[MarketDataBar, ...],
    sidecar: MarketAdjustmentSidecar,
    reference_at: datetime | None = None,
) -> tuple[tuple[MarketDataBar, ...], dict[str, object]]:
    visible_reference_at: datetime | None = None
    visible_reference_factor: float | None = None
    if sidecar.policy == "forward_adjusted_visible":
        if reference_at is not None:
            visible_reference_at = reference_at
        elif bars:
            visible_reference_at = bars[-1].end_at
        if visible_reference_at is not None:
            visible_reference_factor = sidecar.factor_for_timestamp(visible_reference_at)
    adjusted_bars = tuple(
        _apply_factor_to_bar(
            bar,
            _bar_adjustment_factor(
                bar=bar,
                sidecar=sidecar,
                visible_reference_factor=visible_reference_factor,
            ),
        )
        for bar in bars
    )
    return adjusted_bars, _adjustment_metadata(
        sidecar=sidecar,
        adjusted_bar_count=len(adjusted_bars),
        visible_reference_at=visible_reference_at,
        visible_reference_factor=visible_reference_factor,
    )


def adjust_price_for_realized_policy(
    *,
    raw_price: float,
    timestamp: datetime,
    sidecar: MarketAdjustmentSidecar,
) -> float:
    if sidecar.policy != "forward_adjusted_realized":
        raise MarketAdjustmentError(
            "realized price adjustment requires forward_adjusted_realized policy."
        )
    return raw_price * sidecar.factor_for_timestamp(timestamp)


def load_realized_adjustment_sidecar(
    *,
    root: Path,
    market_symbol: str,
    timezone_name: str,
    source_kind: Literal["local_archive", "replay_store", "live_store"],
) -> MarketAdjustmentSidecar:
    return load_adjustment_sidecar(
        root=root,
        market_symbol=market_symbol,
        policy="forward_adjusted_realized",
        timezone_name=timezone_name,
        source_kind=source_kind,
    )


def read_adjustment_sidecar_hash(
    *,
    root: Path,
    market_symbol: str,
    policy: MarketDataAdjustmentPolicy,
) -> str | None:
    direction = adjustment_direction_for_policy(policy)
    archive_symbol = normalize_archive_symbol(market_symbol)
    source_path = (
        root.resolve(strict=False) / "adjustments" / direction / f"{archive_symbol}.csv"
    )
    if not source_path.is_file():
        return None
    return sha256(source_path.read_bytes()).hexdigest()


def adjustment_store_relative_path(
    *,
    market_symbol: str,
    policy: MarketDataAdjustmentPolicy,
) -> Path:
    direction = adjustment_direction_for_policy(policy)
    archive_symbol = normalize_archive_symbol(market_symbol)
    return Path("adjustments") / direction / f"{archive_symbol}.csv"


def _apply_factor_to_bar(bar: MarketDataBar, factor: float) -> MarketDataBar:
    if factor <= 0.0:
        raise MarketAdjustmentError("adjustment factor must be positive.")
    return MarketDataBar(
        start_at=bar.start_at,
        end_at=bar.end_at,
        open_price=bar.open_price * factor,
        high_price=bar.high_price * factor,
        low_price=bar.low_price * factor,
        close_price=bar.close_price * factor,
        volume=bar.volume / factor,
        vwap=(bar.vwap * factor if bar.vwap is not None else None),
    )


def _bar_adjustment_factor(
    *,
    bar: MarketDataBar,
    sidecar: MarketAdjustmentSidecar,
    visible_reference_factor: float | None,
) -> float:
    raw_factor = sidecar.factor_for_timestamp(bar.end_at)
    if sidecar.policy == "forward_adjusted_realized":
        return raw_factor
    if sidecar.policy == "forward_adjusted_visible":
        if visible_reference_factor is None:
            return 1.0
        return raw_factor / visible_reference_factor
    raise MarketAdjustmentError(f"unsupported adjustment policy: {sidecar.policy!r}.")


def _adjustment_metadata(
    *,
    sidecar: MarketAdjustmentSidecar,
    adjusted_bar_count: int,
    visible_reference_at: datetime | None,
    visible_reference_factor: float | None,
) -> dict[str, object]:
    dates = sidecar.trading_dates
    return {
        "adjustment_policy": sidecar.policy,
        "adjustment_direction": sidecar.direction,
        "adjustment_timezone": sidecar.timezone_name,
        "adjustment_source_kind": sidecar.source_kind,
        "adjustment_source_path": sidecar.source_path.as_posix(),
        "adjustment_factor_count": len(dates),
        "adjustment_first_trading_date": dates[0].isoformat(),
        "adjustment_last_trading_date": dates[-1].isoformat(),
        "adjusted_bar_count": adjusted_bar_count,
        "visible_reference_at": (
            None if visible_reference_at is None else visible_reference_at.isoformat()
        ),
        "visible_reference_factor": visible_reference_factor,
    }


def _factor_date_on_or_before(
    *,
    trading_dates: tuple[date, ...],
    requested_date: date,
) -> date | None:
    index = bisect_right(trading_dates, requested_date) - 1
    if index < 0:
        return None
    return trading_dates[index]


def _metadata_timezone_name(source_metadata: dict[str, object]) -> str | None:
    for key in ("cached_timezone", "timezone"):
        value = source_metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _metadata_store_source_kind(
    source_metadata: dict[str, object],
) -> Literal["replay_store", "live_store"]:
    value = source_metadata.get("cached_store_kind")
    if isinstance(value, str) and value.strip() == "live_store":
        return "live_store"
    return "replay_store"


def _metadata_path(source_metadata: dict[str, object], key: str) -> Path | None:
    value = source_metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).resolve(strict=False)


def _has_any_alias(fieldnames: frozenset[str], aliases: tuple[str, ...]) -> bool:
    return any(alias in fieldnames for alias in aliases)


def _row_value(row: dict[str, str | None], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        if alias in row:
            return row.get(alias)
    return None


def _parse_factor_date(
    value: str | None,
    *,
    source_path: Path,
    line_number: int,
) -> date:
    if value is None or not value.strip():
        raise MarketAdjustmentError(
            "adjustment sidecar has blank trading date "
            f"at {source_path.as_posix()}:{line_number}."
        )
    try:
        return datetime.strptime(value.strip(), "%Y%m%d").date()
    except ValueError as exc:
        raise MarketAdjustmentError(
            "adjustment sidecar has invalid trading date "
            f"{value!r} at {source_path.as_posix()}:{line_number}."
        ) from exc


def _parse_factor_value(
    value: str | None,
    *,
    source_path: Path,
    line_number: int,
) -> float:
    if value is None or not value.strip():
        raise MarketAdjustmentError(
            "adjustment sidecar has blank factor "
            f"at {source_path.as_posix()}:{line_number}."
        )
    try:
        factor = float(value.strip())
    except ValueError as exc:
        raise MarketAdjustmentError(
            "adjustment sidecar has invalid factor "
            f"{value!r} at {source_path.as_posix()}:{line_number}."
        ) from exc
    if factor <= 0.0:
        raise MarketAdjustmentError(
            "adjustment sidecar factor must be positive "
            f"at {source_path.as_posix()}:{line_number}."
        )
    return factor


__all__ = [
    "AdjustmentDirection",
    "AppliedAdjustment",
    "MarketAdjustmentError",
    "MarketAdjustmentSidecar",
    "MarketDataAdjustmentPolicy",
    "adjust_price_for_realized_policy",
    "adjustment_direction_for_policy",
    "adjustment_store_relative_path",
    "apply_adjustment_policy_to_bars",
    "apply_adjustment_policy_to_series",
    "load_adjustment_sidecar",
    "load_adjustment_sidecar_from_archive_root",
    "load_realized_adjustment_sidecar",
    "load_adjustment_sidecar_from_source_metadata",
    "normalize_adjustment_policy",
    "normalize_archive_symbol",
    "read_adjustment_sidecar_hash",
]
