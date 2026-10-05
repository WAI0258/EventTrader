"""File-backed raw market data store for deterministic replay context."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

from event_trader.contracts.view_state_change import MarketDataBar, MarketDataSeries
from event_trader.market.adjustments import (
    AdjustmentDirection,
    normalize_archive_symbol,
)
from event_trader.market.contracts import (
    OptionBarObservation,
    OptionContractSnapshot,
    OptionTradeObservation,
)


class MarketDataStoreError(RuntimeError):
    """Raised when the replay market-data store cannot satisfy a request."""


_ACTIVE_REPLAY_RUN_FILENAME = "active_run.json"


@dataclass(frozen=True, slots=True)
class MarketDataBarsIntegrity:
    """Deterministic digest of one persisted replay bar file."""

    bar_count: int
    first_start_at: datetime
    latest_end_at: datetime
    content_sha256: str


@dataclass(frozen=True, slots=True)
class MarketDataStoreManifest:
    """Stable manifest for one replay market-data prefetch run."""

    target_key: str
    run_id: str
    fingerprint: str
    provider: str
    subscription_providers: dict[str, str]
    window_start: datetime
    window_end: datetime
    bar_symbols: tuple[str, ...]
    option_underlyings: tuple[str, ...]
    option_contracts: tuple[str, ...]
    source_metadata: dict[str, object]
    failures: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "provider": self.provider,
            "subscription_providers": self.subscription_providers,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "bar_symbols": list(self.bar_symbols),
            "option_underlyings": list(self.option_underlyings),
            "option_contracts": list(self.option_contracts),
            "source_metadata": self.source_metadata,
            "failures": list(self.failures),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> MarketDataStoreManifest:
        return cls(
            target_key=_required_string(payload, "target_key"),
            run_id=_required_string(payload, "run_id"),
            fingerprint=_optional_string(payload.get("fingerprint")) or "",
            provider=_required_string(payload, "provider"),
            subscription_providers=_string_dict(payload.get("subscription_providers")),
            window_start=_parse_datetime(_required_string(payload, "window_start")),
            window_end=_parse_datetime(_required_string(payload, "window_end")),
            bar_symbols=tuple(_string_list(payload.get("bar_symbols"))),
            option_underlyings=tuple(_string_list(payload.get("option_underlyings"))),
            option_contracts=tuple(_string_list(payload.get("option_contracts"))),
            source_metadata=_object_dict(payload.get("source_metadata")),
            failures=tuple(
                item
                for item in _object_list(payload.get("failures"))
                if isinstance(item, dict)
            ),
        )


class FileBackedMarketDataStore:
    """Persist raw market observations for replay-local slicing."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=False)
        self.bars_root = self.root / "bars"
        self.adjustments_root = self.root / "adjustments"
        self.chain_root = self.root / "option_chain_metadata"
        self.trades_root = self.root / "option_trades"
        self.option_bars_root = self.root / "option_bars"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def write_active_run_pointer(self) -> None:
        write_active_replay_run_id(self.root.parent, self.root.name)

    def initialize(self) -> None:
        for path in (
            self.root,
            self.bars_root,
            self.adjustments_root,
            self.chain_root,
            self.trades_root,
            self.option_bars_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def write_manifest(self, manifest: MarketDataStoreManifest) -> None:
        self.initialize()
        self.manifest_path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    def read_manifest(self) -> MarketDataStoreManifest:
        if not self.manifest_path.exists():
            raise MarketDataStoreError(f"market data manifest is missing: {self.manifest_path}")
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise MarketDataStoreError("market data manifest must be a JSON object.")
        return MarketDataStoreManifest.from_dict(payload)

    def write_bars(
        self,
        *,
        symbol: str,
        granularity: str,
        bars: tuple[MarketDataBar, ...],
    ) -> None:
        self.initialize()
        path = self._bars_path(symbol=symbol, granularity=granularity)
        rows = {
            (bar.start_at.isoformat(), bar.end_at.isoformat()): bar
            for bar in bars
        }
        _write_jsonl(path, (_bar_to_dict(bar) for _, bar in sorted(rows.items())))

    def read_bars(
        self,
        *,
        symbol: str,
        granularity: str,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        path = self._bars_path(symbol=symbol, granularity=granularity)
        bars = tuple(
            bar
            for bar in (_bar_from_dict(row) for row in _read_jsonl(path))
            if bar.end_at > start_at and bar.start_at < end_at
        )
        if not bars:
            raise MarketDataStoreError(
                f"no prefetched bars for {symbol} {granularity} in requested window."
            )
        return MarketDataSeries(bars=bars)

    def read_bars_integrity(
        self,
        *,
        symbol: str,
        granularity: str,
    ) -> MarketDataBarsIntegrity:
        path = self._bars_path(symbol=symbol, granularity=granularity)
        rows = _read_jsonl(path)
        if not rows:
            raise MarketDataStoreError(
                f"no prefetched bars for {symbol} {granularity} in stored file."
            )
        bars = tuple(_bar_from_dict(row) for row in rows)
        return MarketDataBarsIntegrity(
            bar_count=len(bars),
            first_start_at=bars[0].start_at,
            latest_end_at=bars[-1].end_at,
            content_sha256=sha256(path.read_bytes()).hexdigest(),
        )

    def write_adjustment_sidecar(
        self,
        *,
        symbol: str,
        direction: AdjustmentDirection,
        content: str,
    ) -> None:
        self.initialize()
        path = self.adjustment_sidecar_path(symbol=symbol, direction=direction)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def has_adjustment_sidecar(
        self,
        *,
        symbol: str,
        direction: AdjustmentDirection,
    ) -> bool:
        return self.adjustment_sidecar_path(symbol=symbol, direction=direction).is_file()

    def adjustment_sidecar_path(
        self,
        *,
        symbol: str,
        direction: AdjustmentDirection,
    ) -> Path:
        archive_symbol = normalize_archive_symbol(symbol)
        return self.adjustments_root / direction / f"{archive_symbol}.csv"

    def write_option_chain_metadata(
        self,
        *,
        underlying_symbol: str,
        contracts: tuple[OptionContractSnapshot, ...],
    ) -> None:
        self.initialize()
        rows = {contract.contract_symbol: contract for contract in contracts}
        _write_jsonl(
            self._chain_path(underlying_symbol),
            (_contract_to_dict(contract) for _, contract in sorted(rows.items())),
        )

    def read_option_chain_metadata(
        self,
        *,
        underlying_symbol: str,
    ) -> tuple[OptionContractSnapshot, ...]:
        path = self._chain_path(underlying_symbol)
        return tuple(_contract_from_dict(row) for row in _read_jsonl(path))

    def write_option_trades(
        self,
        *,
        trades: tuple[OptionTradeObservation, ...],
    ) -> None:
        self.initialize()
        rows = {
            (trade.contract_symbol, trade.observed_at.isoformat(), trade.price, trade.size): trade
            for trade in trades
        }
        _write_jsonl(
            self.trades_root / "trades.jsonl",
            (_trade_to_dict(trade) for _, trade in sorted(rows.items())),
        )

    def read_option_trades(
        self,
        *,
        contract_symbols: tuple[str, ...],
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[OptionTradeObservation, ...]:
        symbols = frozenset(contract_symbols)
        path = self.trades_root / "trades.jsonl"
        return tuple(
            trade
            for trade in (_trade_from_dict(row) for row in _read_jsonl(path))
            if trade.contract_symbol in symbols and start_at <= trade.observed_at <= end_at
        )

    def write_option_bars(
        self,
        *,
        bars: tuple[OptionBarObservation, ...],
    ) -> None:
        self.initialize()
        rows = {
            (bar.contract_symbol, bar.start_at.isoformat(), bar.end_at.isoformat()): bar
            for bar in bars
        }
        _write_jsonl(
            self.option_bars_root / "bars.jsonl",
            (_option_bar_to_dict(bar) for _, bar in sorted(rows.items())),
        )

    def read_option_bars(
        self,
        *,
        contract_symbols: tuple[str, ...],
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[OptionBarObservation, ...]:
        symbols = frozenset(contract_symbols)
        return tuple(
            bar
            for bar in (
                _option_bar_from_dict(row)
                for row in _read_jsonl(self.option_bars_root / "bars.jsonl")
            )
            if bar.contract_symbol in symbols and bar.end_at <= end_at and bar.start_at >= start_at
        )

    def _bars_path(self, *, symbol: str, granularity: str) -> Path:
        return self.bars_root / market_data_bars_filename(
            symbol=symbol,
            granularity=granularity,
        )

    def _chain_path(self, underlying_symbol: str) -> Path:
        return self.chain_root / f"{_safe_name(underlying_symbol)}.jsonl"


def _write_jsonl(path: Path, rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> tuple[dict[str, object], ...]:
    if not path.exists():
        return ()
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return tuple(rows)


def market_data_bars_filename(*, symbol: str, granularity: str) -> str:
    return f"{_safe_name(symbol)}_{_safe_name(granularity)}.jsonl"


def active_replay_run_pointer_path(target_market_root: Path) -> Path:
    return target_market_root.resolve(strict=False) / _ACTIVE_REPLAY_RUN_FILENAME


def read_active_replay_run_id(target_market_root: Path) -> str | None:
    pointer_path = active_replay_run_pointer_path(target_market_root)
    if not pointer_path.is_file():
        return None
    try:
        payload = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    run_id = _optional_string(payload.get("run_id"))
    if run_id is None:
        return None
    if not (target_market_root.resolve(strict=False) / run_id).is_dir():
        return None
    return run_id


def write_active_replay_run_id(target_market_root: Path, run_id: str) -> None:
    normalized_run_id = run_id.strip()
    if not normalized_run_id:
        raise MarketDataStoreError("active replay run id must be a non-empty string.")
    resolved_target_root = target_market_root.resolve(strict=False)
    run_root = resolved_target_root / normalized_run_id
    if not run_root.is_dir():
        raise MarketDataStoreError(
            f"cannot activate missing replay market-data run: {run_root}"
        )
    pointer_path = active_replay_run_pointer_path(resolved_target_root)
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(
        json.dumps({"run_id": normalized_run_id}, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _bar_to_dict(bar: MarketDataBar) -> dict[str, object]:
    return {
        "start_at": bar.start_at.isoformat(),
        "end_at": bar.end_at.isoformat(),
        "open_price": bar.open_price,
        "high_price": bar.high_price,
        "low_price": bar.low_price,
        "close_price": bar.close_price,
        "volume": bar.volume,
        "vwap": bar.vwap,
    }


def _bar_from_dict(row: dict[str, object]) -> MarketDataBar:
    return MarketDataBar(
        start_at=_parse_datetime(_required_string(row, "start_at")),
        end_at=_parse_datetime(_required_string(row, "end_at")),
        open_price=_required_float(row, "open_price"),
        high_price=_required_float(row, "high_price"),
        low_price=_required_float(row, "low_price"),
        close_price=_required_float(row, "close_price"),
        volume=_required_float(row, "volume"),
        vwap=_optional_float(row.get("vwap")),
    )


def _contract_to_dict(contract: OptionContractSnapshot) -> dict[str, object]:
    return {
        "contract_symbol": contract.contract_symbol,
        "underlying_symbol": contract.underlying_symbol,
        "option_type": contract.option_type,
        "expiry_date": contract.expiry_date,
        "strike_price": contract.strike_price,
        "observed_at": contract.observed_at.isoformat() if contract.observed_at else None,
        "volume": contract.volume,
        "trade_count": contract.trade_count,
        "latest_trade_price": contract.latest_trade_price,
        "latest_trade_size": contract.latest_trade_size,
        "latest_trade_at": (
            contract.latest_trade_at.isoformat() if contract.latest_trade_at else None
        ),
        "latest_quote_at": (
            contract.latest_quote_at.isoformat() if contract.latest_quote_at else None
        ),
        "bid_price": contract.bid_price,
        "ask_price": contract.ask_price,
        "bid_size": contract.bid_size,
        "ask_size": contract.ask_size,
        "implied_volatility": contract.implied_volatility,
        "delta": contract.delta,
        "gamma": contract.gamma,
        "rho": contract.rho,
        "theta": contract.theta,
        "vega": contract.vega,
        "open_interest": contract.open_interest,
    }


def _contract_from_dict(row: dict[str, object]) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        contract_symbol=_required_string(row, "contract_symbol"),
        underlying_symbol=_required_string(row, "underlying_symbol"),
        option_type=_required_string(row, "option_type"),
        expiry_date=_required_string(row, "expiry_date"),
        strike_price=_required_float(row, "strike_price"),
        observed_at=_optional_datetime(row.get("observed_at")),
        volume=_optional_float(row.get("volume")),
        trade_count=_optional_int(row.get("trade_count")),
        latest_trade_price=_optional_float(row.get("latest_trade_price")),
        latest_trade_size=_optional_float(row.get("latest_trade_size")),
        latest_trade_at=_optional_datetime(row.get("latest_trade_at")),
        latest_quote_at=_optional_datetime(row.get("latest_quote_at")),
        bid_price=_optional_float(row.get("bid_price")),
        ask_price=_optional_float(row.get("ask_price")),
        bid_size=_optional_float(row.get("bid_size")),
        ask_size=_optional_float(row.get("ask_size")),
        implied_volatility=_optional_float(row.get("implied_volatility")),
        delta=_optional_float(row.get("delta")),
        gamma=_optional_float(row.get("gamma")),
        rho=_optional_float(row.get("rho")),
        theta=_optional_float(row.get("theta")),
        vega=_optional_float(row.get("vega")),
        open_interest=_optional_float(row.get("open_interest")),
    )


def _trade_to_dict(trade: OptionTradeObservation) -> dict[str, object]:
    return {
        "contract_symbol": trade.contract_symbol,
        "option_type": trade.option_type,
        "expiry_date": trade.expiry_date,
        "strike_price": trade.strike_price,
        "observed_at": trade.observed_at.isoformat(),
        "price": trade.price,
        "size": trade.size,
        "notional": trade.notional,
    }


def _trade_from_dict(row: dict[str, object]) -> OptionTradeObservation:
    return OptionTradeObservation(
        contract_symbol=_required_string(row, "contract_symbol"),
        option_type=_required_string(row, "option_type"),
        expiry_date=_required_string(row, "expiry_date"),
        strike_price=_required_float(row, "strike_price"),
        observed_at=_parse_datetime(_required_string(row, "observed_at")),
        price=_required_float(row, "price"),
        size=_required_float(row, "size"),
        notional=_required_float(row, "notional"),
    )


def _option_bar_to_dict(bar: OptionBarObservation) -> dict[str, object]:
    return {
        "contract_symbol": bar.contract_symbol,
        "option_type": bar.option_type,
        "expiry_date": bar.expiry_date,
        "strike_price": bar.strike_price,
        "start_at": bar.start_at.isoformat(),
        "end_at": bar.end_at.isoformat(),
        "close_price": bar.close_price,
        "volume": bar.volume,
        "trade_count": bar.trade_count,
        "vwap": bar.vwap,
    }


def _option_bar_from_dict(row: dict[str, object]) -> OptionBarObservation:
    return OptionBarObservation(
        contract_symbol=_required_string(row, "contract_symbol"),
        option_type=_required_string(row, "option_type"),
        expiry_date=_required_string(row, "expiry_date"),
        strike_price=_required_float(row, "strike_price"),
        start_at=_parse_datetime(_required_string(row, "start_at")),
        end_at=_parse_datetime(_required_string(row, "end_at")),
        close_price=_required_float(row, "close_price"),
        volume=_required_float(row, "volume"),
        trade_count=_optional_int(row.get("trade_count")),
        vwap=_optional_float(row.get("vwap")),
    )


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketDataStoreError("stored datetime must be timezone-aware.")
    return parsed.astimezone(UTC)


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MarketDataStoreError("stored optional datetime must be a string or null.")
    return _parse_datetime(value)


def _required_string(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MarketDataStoreError(f"stored field {key!r} must be a non-empty string.")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MarketDataStoreError("stored optional string field must be a string or null.")
    return value


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _object_dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _required_float(row: dict[str, object], key: str) -> float:
    value = _optional_float(row.get(key))
    if value is None:
        raise MarketDataStoreError(f"stored field {key!r} must be numeric.")
    return value


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float):
        raise MarketDataStoreError("stored numeric field must be numeric or null.")
    return float(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise MarketDataStoreError("stored integer field must be an integer or null.")
    return value


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)


__all__ = [
    "FileBackedMarketDataStore",
    "MarketDataBarsIntegrity",
    "MarketDataStoreError",
    "MarketDataStoreManifest",
]
