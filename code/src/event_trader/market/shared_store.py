"""Shared, process-safe market-data ownership for all runtimes.

The store deliberately lives outside a target workspace.  A target is a
consumer of a market series, not part of the series identity.  SQLite is used
as the coordination boundary: a short-lived lease claims one missing interval
while remote I/O happens outside the write transaction.  A second process can
therefore wait for the first fetch and reuse its rows without holding the
database write lock across the network call.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    MarketMapping,
)
from event_trader.market.contracts import (
    OptionBarObservation,
    OptionContractSnapshot,
    OptionSelectionPolicy,
    OptionTradeObservation,
)
from event_trader.market.session_policy import (
    aligned_market_start,
    completed_market_end,
    expected_market_bar_intervals,
    expected_market_intervals,
)


def shared_market_data_root(config: object | None = None) -> Path:
    """Resolve the one shared store root used by all runtime configurations."""
    configured = getattr(config, "market_data_root", None)
    if isinstance(configured, Path):
        return configured.resolve(strict=False)
    return (Path(__file__).resolve().parents[3] / ".local" / "market-data").resolve(
        strict=False
    )


def write_workspace_snapshot_ref(
    workspace_root: str | Path,
    *,
    target_key: str,
    run_id: str,
    snapshot_id: str,
) -> Path:
    root = Path(workspace_root).expanduser().resolve(strict=False)
    path = root / "runtime" / "market_data_refs" / target_key / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "target_key": target_key,
                "run_id": run_id,
                "snapshot_id": snapshot_id,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def read_workspace_snapshot_ref(
    workspace_root: str | Path,
    *,
    target_key: str,
    run_id: str | None = None,
) -> str | None:
    root = Path(workspace_root).expanduser().resolve(strict=False)
    target_root = root / "runtime" / "market_data_refs" / target_key
    paths = [target_root / f"{run_id}.json"] if run_id else sorted(target_root.glob("*.json"))
    for path in reversed(paths):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        value = payload.get("snapshot_id") if isinstance(payload, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class SharedMarketDataError(RuntimeError):
    """Raised when shared market data cannot satisfy a request."""


@dataclass(frozen=True, slots=True)
class MarketSeriesIdentity:
    """Physical identity of one canonical market-data series.

    ``target_key`` is intentionally absent.  Two targets may consume the same
    series while keeping their analysis and portfolio state isolated.
    """

    provider: str
    instrument: str
    market_session: str
    exchange: str | None
    exchange_session_scope: str | None
    bar_granularity: str
    adjustment_policy: str

    def __post_init__(self) -> None:
        for field_name in (
            "provider",
            "instrument",
            "market_session",
            "bar_granularity",
            "adjustment_policy",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise SharedMarketDataError(f"{field_name} must be non-empty.")
            object.__setattr__(self, field_name, value.strip())
        object.__setattr__(self, "provider", self.provider.lower())
        object.__setattr__(self, "instrument", self.instrument.upper())
        object.__setattr__(self, "market_session", self.market_session.lower())
        object.__setattr__(self, "bar_granularity", self.bar_granularity.lower())
        object.__setattr__(self, "adjustment_policy", self.adjustment_policy.lower())
        for field_name in ("exchange", "exchange_session_scope"):
            value = getattr(self, field_name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise SharedMarketDataError(f"{field_name} must be non-empty when set.")
                object.__setattr__(self, field_name, value.strip().upper())

    @classmethod
    def from_mapping(
        cls,
        mapping: MarketMapping,
        *,
        provider: str,
        adjustment_policy: str | None = None,
    ) -> MarketSeriesIdentity:
        return cls(
            provider=provider,
            instrument=mapping.market_symbol,
            market_session=mapping.market_session,
            exchange=mapping.exchange,
            exchange_session_scope=mapping.exchange_session_scope,
            bar_granularity=mapping.bar_granularity,
            adjustment_policy=adjustment_policy or "raw",
        )

    @property
    def canonical_payload(self) -> dict[str, str | None]:
        return {
            "provider": self.provider,
            "instrument": self.instrument,
            "market_session": self.market_session,
            "exchange": self.exchange,
            "exchange_session_scope": self.exchange_session_scope,
            "bar_granularity": self.bar_granularity,
            "adjustment_policy": self.adjustment_policy,
        }

    @property
    def series_key(self) -> str:
        encoded = json.dumps(
            self.canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MarketDataSnapshot:
    snapshot_id: str
    mode: str
    created_at: datetime
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class ObservedMarketDataPrefix:
    """Read-only view of the contiguous observed portion of a market window."""

    series: MarketDataSeries | None
    requested_start_at: datetime
    requested_completed_end_at: datetime
    last_verified_available_end_at: datetime | None
    first_missing_interval: tuple[datetime, datetime] | None
    complete_to_requested_end: bool

    @property
    def status(self) -> Literal["ready", "partial"]:
        return "ready" if self.complete_to_requested_end else "partial"


Fetcher = Callable[[datetime, datetime], MarketDataSeries]
# A primary context backfill can legitimately span several provider pages.
# Keep the lease longer than that work while still allowing restart recovery.
_SYNC_LEASE_SECONDS = 3600.0
# Bound one remote reconciliation request without reducing it to individual
# canonical bars. Providers that page internally can still fill this window.
_SYNC_FETCH_MAX_MISSING_INTERVALS = 1000


class SharedMarketDataStore:
    """SQLite-backed shared market-data catalog and snapshot store."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "market_data.sqlite3"
        self._initialize()

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(
                f"{self.database_path.as_uri()}?mode=ro",
                uri=True,
                timeout=120.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 120000")
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        connection = sqlite3.connect(
            self.database_path,
            timeout=120.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 120000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS series (
                    series_key TEXT PRIMARY KEY,
                    identity_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bars (
                    series_key TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    open_price REAL NOT NULL,
                    high_price REAL NOT NULL,
                    low_price REAL NOT NULL,
                    close_price REAL NOT NULL,
                    volume REAL NOT NULL,
                    vwap REAL,
                    content_sha256 TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (series_key, start_at, end_at, content_sha256),
                    FOREIGN KEY (series_key) REFERENCES series(series_key)
                );
                CREATE INDEX IF NOT EXISTS bars_window
                    ON bars(series_key, start_at, end_at);
                CREATE TABLE IF NOT EXISTS adjustment_sidecars (
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    PRIMARY KEY (symbol, direction)
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshot_bars (
                    snapshot_id TEXT NOT NULL,
                    series_key TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, series_key, start_at, end_at),
                    FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id),
                    FOREIGN KEY (series_key) REFERENCES series(series_key)
                );
                CREATE INDEX IF NOT EXISTS snapshot_series_window
                    ON snapshot_bars(snapshot_id, series_key, start_at, end_at);
                CREATE TABLE IF NOT EXISTS market_data_sync_leases (
                    series_key TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (series_key, start_at, end_at)
                );
                CREATE TABLE IF NOT EXISTS option_chain_metadata (
                    underlying_symbol TEXT NOT NULL,
                    contract_symbol TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (underlying_symbol, contract_symbol)
                );
                CREATE TABLE IF NOT EXISTS option_trades (
                    contract_symbol TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (contract_symbol, observed_at, price, size)
                );
                CREATE TABLE IF NOT EXISTS option_bars (
                    contract_symbol TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (contract_symbol, start_at, end_at)
                );
                """
            )
            self._migrate_legacy_bar_identity(connection)

    @staticmethod
    def _migrate_legacy_bar_identity(connection: sqlite3.Connection) -> None:
        """Preserve old rows while allowing later provider corrections.

        Early development versions keyed ``bars`` only by interval and rejected
        a corrected provider value.  A snapshot already stores the content hash,
        so retaining every revision is the only way for live reads to advance
        without changing a pinned replay.  This migration is deliberately
        in-place and copies all existing rows before replacing the old schema.
        """
        columns = connection.execute("PRAGMA table_info(bars)").fetchall()
        primary_key_columns = [str(row[1]) for row in columns if int(row[5]) > 0]
        expected = ["series_key", "start_at", "end_at", "content_sha256"]
        if primary_key_columns == expected:
            return
        if primary_key_columns != ["series_key", "start_at", "end_at"]:
            raise SharedMarketDataError("unsupported market-data bars schema")
        connection.execute("ALTER TABLE bars RENAME TO bars_legacy")
        connection.execute(
            """
            CREATE TABLE bars (
                series_key TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                open_price REAL NOT NULL,
                high_price REAL NOT NULL,
                low_price REAL NOT NULL,
                close_price REAL NOT NULL,
                volume REAL NOT NULL,
                vwap REAL,
                content_sha256 TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (series_key, start_at, end_at, content_sha256),
                FOREIGN KEY (series_key) REFERENCES series(series_key)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO bars
                (series_key, start_at, end_at, open_price, high_price, low_price,
                 close_price, volume, vwap, content_sha256, recorded_at)
            SELECT series_key, start_at, end_at, open_price, high_price, low_price,
                   close_price, volume, vwap, content_sha256, recorded_at
            FROM bars_legacy
            """
        )
        connection.execute("DROP TABLE bars_legacy")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS bars_window ON bars(series_key, start_at, end_at)"
        )

    def materialize(
        self,
        identity: MarketSeriesIdentity,
        *,
        start_at: datetime,
        end_at: datetime,
        fetch: Fetcher | None,
    ) -> MarketDataSeries:
        """Synchronize missing intervals, then read the requested bars."""
        requested_start = _utc_iso(start_at, "start_at")
        requested_end = _utc_iso(end_at, "end_at")
        if requested_start >= requested_end:
            raise SharedMarketDataError("start_at must be before end_at.")
        start = _aligned_start_iso(start_at, identity)
        end = _completed_end_iso(end_at, identity)
        self.synchronize(identity, start_at=start_at, end_at=end_at, fetch=fetch)
        with self._connect() as connection:
            rows = self._read_rows(
                connection,
                identity.series_key,
                requested_start,
                end,
            )
        if not _covers_window(rows, start, end, identity):
            raise SharedMarketDataError(
                f"provider returned incomplete data for {identity.instrument}."
            )
        return MarketDataSeries(bars=tuple(_bar_from_row(row) for row in rows))

    def synchronize(
        self,
        identity: MarketSeriesIdentity,
        *,
        start_at: datetime,
        end_at: datetime,
        fetch: Fetcher | None,
    ) -> int:
        """Fill only missing expected intervals, with restart-safe leases.

        The provider call is deliberately outside SQLite's write transaction.
        A short-lived lease prevents concurrent processes from making the same
        remote request; an expired lease is reclaimable after interruption.
        """
        requested_start = _utc_iso(start_at, "start_at")
        requested_end = _utc_iso(end_at, "end_at")
        if requested_start >= requested_end:
            raise SharedMarketDataError("start_at must be before end_at.")
        start = _aligned_start_iso(start_at, identity)
        end = _completed_end_iso(end_at, identity)
        inserted_total = 0
        if start >= end:
            return inserted_total
        while True:
            with self._connect() as connection:
                self._ensure_series(connection, identity)
                rows = self._read_rows(connection, identity.series_key, start, end)
            missing = _missing_intervals(rows, start, end, identity)
            if not missing:
                return inserted_total
            if fetch is None:
                raise SharedMarketDataError(
                    f"shared market data is missing for {identity.instrument}."
                )
            fetch_start = missing[0][0]
            fetch_end = missing[
                min(len(missing), _SYNC_FETCH_MAX_MISSING_INTERVALS) - 1
            ][1]
            owner = uuid.uuid4().hex
            if not self._claim_sync_lease(
                identity.series_key,
                fetch_start.isoformat(),
                fetch_end.isoformat(),
                owner,
            ):
                time.sleep(0.05)
                continue
            try:
                fetched = fetch(fetch_start, fetch_end)
                fetched_bars = tuple(
                    bar
                    for bar in fetched.bars
                    if fetch_start <= bar.start_at and bar.end_at <= fetch_end
                )
                if not fetched_bars:
                    raise SharedMarketDataError(
                        f"provider returned no bars for missing window "
                        f"{fetch_start.isoformat()}..{fetch_end.isoformat()}"
                    )
                inserted = self.record_bars(identity, fetched_bars)
                inserted_total += inserted
                with self._connect() as connection:
                    after = self._read_rows(connection, identity.series_key, start, end)
                if _missing_intervals(after, start, end, identity) == missing:
                    raise SharedMarketDataError(
                        f"provider made no progress for {identity.instrument} "
                        f"at {fetch_start.isoformat()}"
                    )
            finally:
                self._release_sync_lease(
                    identity.series_key,
                    fetch_start.isoformat(),
                    fetch_end.isoformat(),
                    owner,
                )

    def _claim_sync_lease(
        self,
        series_key: str,
        start_at: str,
        end_at: str,
        owner: str,
    ) -> bool:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM market_data_sync_leases WHERE expires_at <= ?",
                    (now,),
                )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO market_data_sync_leases
                        (series_key, start_at, end_at, owner, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (series_key, start_at, end_at, owner, now + _SYNC_LEASE_SECONDS),
                )
                connection.commit()
                return cursor.rowcount == 1
            except Exception:
                connection.rollback()
                raise

    def _release_sync_lease(
        self,
        series_key: str,
        start_at: str,
        end_at: str,
        owner: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM market_data_sync_leases
                WHERE series_key = ? AND start_at = ? AND end_at = ? AND owner = ?
                """,
                (series_key, start_at, end_at, owner),
            )

    def record_bars(
        self,
        identity: MarketSeriesIdentity,
        bars: Iterable[MarketDataBar],
    ) -> int:
        """Commit provider observations, retaining every content revision.

        This is intentionally separate from :meth:`materialize`: a read should
        not repeatedly call a provider merely to discover that a cached bar has
        not changed, while a live ingest/reconciliation pass must still be able
        to publish a corrected value for an already-covered interval.
        """
        materialized = tuple(bars)
        if not materialized:
            return 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_series(connection, identity)
                before = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM bars WHERE series_key = ?",
                        (identity.series_key,),
                    ).fetchone()["count"]
                )
                self._insert_bars(connection, identity, materialized)
                after = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM bars WHERE series_key = ?",
                        (identity.series_key,),
                    ).fetchone()["count"]
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return max(after - before, 0)

    def covers_window(
        self,
        identity: MarketSeriesIdentity,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> bool:
        start = _utc_iso(start_at, "start_at")
        end = _completed_end_iso(end_at, identity)
        if start >= end:
            return True
        with self._connect() as connection:
            rows = self._read_rows(connection, identity.series_key, start, end)
        return _covers_window(rows, start, end, identity)

    def read_observed_prefix(
        self,
        identity: MarketSeriesIdentity,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> ObservedMarketDataPrefix:
        """Read the contiguous stored prefix without synchronizing or writing.

        This is intentionally separate from :meth:`materialize`: live dashboards
        may display known bars while a current tail is still being ingested.  A
        later stored observation is never joined across the first expected gap.
        """
        requested_start = _utc_iso(start_at, "start_at")
        requested_end = _utc_iso(end_at, "end_at")
        if requested_start >= requested_end:
            raise SharedMarketDataError("start_at must be before end_at.")
        aligned_start = _aligned_start_at(start_at, identity)
        completed_end = _completed_end_at(end_at, identity)
        if aligned_start >= completed_end:
            return ObservedMarketDataPrefix(
                series=None,
                requested_start_at=_parse_utc(requested_start),
                requested_completed_end_at=completed_end,
                last_verified_available_end_at=None,
                first_missing_interval=None,
                complete_to_requested_end=True,
            )

        with self._connect(read_only=True) as connection:
            rows = self._read_rows(
                connection,
                identity.series_key,
                requested_start,
                completed_end.isoformat(),
            )
        expected = _expected_bar_intervals(identity, aligned_start, completed_end)
        ordered_rows = [
            (
                _parse_utc(row["start_at"]),
                _parse_utc(row["end_at"]),
                row,
            )
            for row in rows
        ]
        first_missing: tuple[datetime, datetime] | None = None
        verified_end: datetime | None = None
        row_index = 0
        for expected_start, expected_end in expected:
            while row_index < len(ordered_rows) and ordered_rows[row_index][1] <= expected_start:
                row_index += 1
            if (
                row_index >= len(ordered_rows)
                or ordered_rows[row_index][0] > expected_start
                or ordered_rows[row_index][1] < expected_end
            ):
                first_missing = (expected_start, expected_end)
                break
            verified_end = expected_end

        prefix_end = completed_end if first_missing is None else first_missing[0]
        prefix_rows = [
            row
            for row_start, row_end, row in ordered_rows
            if row_start < prefix_end and row_end <= prefix_end
        ]
        return ObservedMarketDataPrefix(
            series=(
                None
                if not prefix_rows
                else MarketDataSeries(bars=tuple(_bar_from_row(row) for row in prefix_rows))
            ),
            requested_start_at=_parse_utc(requested_start),
            requested_completed_end_at=completed_end,
            last_verified_available_end_at=verified_end,
            first_missing_interval=first_missing,
            complete_to_requested_end=first_missing is None,
        )

    def read_snapshot(
        self,
        snapshot_id: str,
        identity: MarketSeriesIdentity,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        start = _utc_iso(start_at, "start_at")
        end = _completed_end_iso(end_at, identity)
        if start >= end:
            raise SharedMarketDataError(
                f"snapshot {snapshot_id!r} has no completed bars for {identity.instrument}."
            )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT b.* FROM snapshot_bars sb
                JOIN bars b ON b.series_key = sb.series_key
                    AND b.start_at = sb.start_at
                    AND b.end_at = sb.end_at
                    AND b.content_sha256 = sb.content_sha256
                WHERE sb.snapshot_id = ? AND sb.series_key = ?
                  AND b.end_at > ? AND b.start_at < ?
                ORDER BY b.start_at, b.end_at
                """,
                (snapshot_id, identity.series_key, start, end),
            ).fetchall()
        if not rows:
            raise SharedMarketDataError(
                f"snapshot {snapshot_id!r} has no bars for {identity.instrument}."
            )
        return MarketDataSeries(bars=tuple(_bar_from_row(row) for row in rows))

    def create_snapshot(
        self,
        *,
        mode: str,
        reads: Iterable[tuple[MarketSeriesIdentity, datetime, datetime]],
        metadata: dict[str, object] | None = None,
    ) -> MarketDataSnapshot:
        snapshot_id = uuid.uuid4().hex
        created_at = datetime.now(UTC)
        payload = {} if metadata is None else dict(metadata)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO snapshots VALUES (?, ?, ?, ?)",
                    (
                        snapshot_id,
                        mode,
                        created_at.isoformat(),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                for identity, start_at, end_at in reads:
                    start = _utc_iso(start_at, "start_at")
                    end = _completed_end_iso(end_at, identity)
                    self._ensure_series(connection, identity)
                    rows = self._read_rows(connection, identity.series_key, start, end)
                    if start >= end:
                        continue
                    if not _covers_window(rows, start, end, identity):
                        raise SharedMarketDataError(
                            f"cannot pin incomplete series {identity.instrument}."
                        )
                    connection.executemany(
                        """
                        INSERT OR IGNORE INTO snapshot_bars
                            (snapshot_id, series_key, start_at, end_at, content_sha256)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                snapshot_id,
                                identity.series_key,
                                row["start_at"],
                                row["end_at"],
                                row["content_sha256"],
                            )
                            for row in rows
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return MarketDataSnapshot(snapshot_id, mode, created_at, payload)

    def snapshot(self, snapshot_id: str) -> MarketDataSnapshot:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise SharedMarketDataError(f"unknown market-data snapshot: {snapshot_id}")
        return MarketDataSnapshot(
            snapshot_id=row["snapshot_id"],
            mode=row["mode"],
            created_at=_parse_utc(row["created_at"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def write_adjustment_sidecar(self, *, symbol: str, direction: str, content: str) -> None:
        digest = sha256(content.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO adjustment_sidecars VALUES (?, ?, ?, ?)",
                (symbol.upper(), direction, content, digest),
            )

    def has_adjustment_sidecar(self, *, symbol: str, direction: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM adjustment_sidecars WHERE symbol = ? AND direction = ?",
                (symbol.upper(), direction),
            ).fetchone()
        return row is not None

    def read_adjustment_sidecar(self, *, symbol: str, direction: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT content FROM adjustment_sidecars WHERE symbol = ? AND direction = ?",
                (symbol.upper(), direction),
            ).fetchone()
        return None if row is None else str(row["content"])

    # Options are intentionally stored as a separate shared namespace. They
    # are not part of the OHLCV series identity/snapshot contract yet, but
    # keeping them here prevents the legacy target/run bar store from being
    # reintroduced merely to preserve derivatives context.
    def write_option_chain_metadata(
        self,
        *,
        underlying_symbol: str,
        contracts: tuple[OptionContractSnapshot, ...],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for contract in contracts:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO option_chain_metadata
                            (underlying_symbol, contract_symbol, payload_json, recorded_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            underlying_symbol.upper(),
                            contract.contract_symbol,
                            _option_json(contract),
                            now,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def read_option_chain_metadata(
        self,
        *,
        underlying_symbol: str,
    ) -> tuple[OptionContractSnapshot, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM option_chain_metadata
                WHERE underlying_symbol = ? ORDER BY contract_symbol
                """,
                (underlying_symbol.upper(),),
            ).fetchall()
        return tuple(_option_contract_from_json(str(row["payload_json"])) for row in rows)

    def write_option_trades(
        self,
        *,
        trades: tuple[OptionTradeObservation, ...],
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for trade in trades:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO option_trades
                            (contract_symbol, observed_at, price, size, payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            trade.contract_symbol,
                            trade.observed_at.isoformat(),
                            trade.price,
                            trade.size,
                            _option_json(trade),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def read_option_trades(
        self,
        *,
        contract_symbols: tuple[str, ...],
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[OptionTradeObservation, ...]:
        if not contract_symbols:
            return ()
        placeholders = ",".join("?" for _ in contract_symbols)
        params: tuple[object, ...] = (
            *contract_symbols,
            _utc_iso(start_at, "start_at"),
            _utc_iso(end_at, "end_at"),
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json FROM option_trades
                WHERE contract_symbol IN ({placeholders})
                  AND observed_at >= ? AND observed_at <= ?
                ORDER BY observed_at
                """,
                params,
            ).fetchall()
        return tuple(_option_trade_from_json(str(row["payload_json"])) for row in rows)

    def write_option_bars(self, *, bars: tuple[OptionBarObservation, ...]) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for bar in bars:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO option_bars
                            (contract_symbol, start_at, end_at, payload_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            bar.contract_symbol,
                            bar.start_at.isoformat(),
                            bar.end_at.isoformat(),
                            _option_json(bar),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def read_option_bars(
        self,
        *,
        contract_symbols: tuple[str, ...],
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[OptionBarObservation, ...]:
        if not contract_symbols:
            return ()
        placeholders = ",".join("?" for _ in contract_symbols)
        params: tuple[object, ...] = (
            *contract_symbols,
            _utc_iso(start_at, "start_at"),
            _utc_iso(end_at, "end_at"),
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json FROM option_bars
                WHERE contract_symbol IN ({placeholders})
                  AND start_at >= ? AND end_at <= ?
                ORDER BY start_at
                """,
                params,
            ).fetchall()
        return tuple(_option_bar_from_json(str(row["payload_json"])) for row in rows)

    def _ensure_series(
        self,
        connection: sqlite3.Connection,
        identity: MarketSeriesIdentity,
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO series VALUES (?, ?, ?)",
            (
                identity.series_key,
                json.dumps(identity.canonical_payload, sort_keys=True),
                datetime.now(UTC).isoformat(),
            ),
        )

    def _read_rows(
        self,
        connection: sqlite3.Connection,
        series_key: str,
        start_at: str,
        end_at: str,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            WITH ranked AS (
                SELECT b.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY b.series_key, b.start_at, b.end_at
                           ORDER BY b.recorded_at DESC, b.rowid DESC
                       ) AS revision_rank
                FROM bars b
                WHERE b.series_key = ? AND b.end_at > ? AND b.start_at < ?
            )
                SELECT * FROM ranked
                WHERE revision_rank = 1
                ORDER BY start_at, end_at
            """,
            (series_key, start_at, end_at),
        ).fetchall()

    def _insert_bars(
        self,
        connection: sqlite3.Connection,
        identity: MarketSeriesIdentity,
        bars: tuple[MarketDataBar, ...],
    ) -> None:
        recorded_at = datetime.now(UTC).isoformat()
        self._ensure_series(connection, identity)
        _validate_bar_intervals(
            bars,
            identity=identity,
            existing_rows=_read_all_rows(connection, identity.series_key),
        )
        for bar in bars:
            content = json.dumps(_bar_payload(bar), sort_keys=True, separators=(",", ":"))
            digest = sha256(content.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT OR IGNORE INTO bars
                    (series_key, start_at, end_at, open_price, high_price, low_price,
                     close_price, volume, vwap, content_sha256, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.series_key,
                    bar.start_at.isoformat(),
                    bar.end_at.isoformat(),
                    bar.open_price,
                    bar.high_price,
                    bar.low_price,
                    bar.close_price,
                    bar.volume,
                    bar.vwap,
                    digest,
                    recorded_at,
                ),
            )


class SharedMarketDataProvider:
    """Provider facade enforcing live versus pinned replay read semantics."""

    def __init__(
        self,
        *,
        store: SharedMarketDataStore,
        provider_name: str,
        remote_provider: object | None = None,
        snapshot_id: str | None = None,
        adjustment_policy: str | None = None,
        adjustment_policies: Mapping[tuple[str, str], str] | None = None,
        provider_names: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        self.store = store
        self.provider_name = provider_name
        self.remote_provider = remote_provider
        self.snapshot_id = snapshot_id
        self.adjustment_policy = adjustment_policy
        self.adjustment_policies = {} if adjustment_policies is None else dict(adjustment_policies)
        self.provider_names = {} if provider_names is None else dict(provider_names)
        self._source_metadata: dict[str, object] = {
            "provider": "shared_market_data",
            "market_data_provider": provider_name,
            "remote_fallback": snapshot_id is None,
            "shared_store_root": store.root.as_posix(),
        }

    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        identity = MarketSeriesIdentity.from_mapping(
            mapping,
            provider=self.provider_names.get(
                (mapping.target_key, mapping.market_symbol),
                self.provider_name,
            ),
            adjustment_policy=self.adjustment_policies.get(
                (mapping.target_key, mapping.market_symbol),
                self.adjustment_policy,
            ),
        )
        if self.snapshot_id is not None:
            series = self.store.read_snapshot(
                self.snapshot_id,
                identity,
                start_at=start_at,
                end_at=end_at,
            )
        else:
            fetch = None
            if self.remote_provider is not None:
                read = getattr(self.remote_provider, "read_series", None)
                if not callable(read):
                    raise SharedMarketDataError("remote provider must implement read_series.")

                def fetch(gap_start: datetime, gap_end: datetime) -> MarketDataSeries:
                    return read(mapping, start_at=gap_start, end_at=gap_end)
            series = self.store.materialize(
                identity,
                start_at=start_at,
                end_at=end_at,
                fetch=fetch,
            )
        consume = getattr(self.remote_provider, "pop_source_metadata", None)
        if callable(consume):
            metadata = consume()
            if isinstance(metadata, dict):
                self._source_metadata.update(metadata)
        self._source_metadata.update(
            {
                "series_key": identity.series_key,
                "instrument": identity.instrument,
                "snapshot_id": self.snapshot_id,
                "returned_bar_count": len(series.bars),
            }
        )
        return series

    def synchronize(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> int:
        """Synchronize the shared series without materializing its full history."""
        identity = MarketSeriesIdentity.from_mapping(
            mapping,
            provider=self.provider_names.get(
                (mapping.target_key, mapping.market_symbol),
                self.provider_name,
            ),
            adjustment_policy=self.adjustment_policies.get(
                (mapping.target_key, mapping.market_symbol),
                self.adjustment_policy,
            ),
        )
        if self.snapshot_id is not None:
            if not self.store.covers_window(identity, start_at=start_at, end_at=end_at):
                raise SharedMarketDataError(
                    f"snapshot provider cannot synchronize {identity.instrument}."
                )
            return 0
        if self.remote_provider is None:
            return self.store.synchronize(
                identity,
                start_at=start_at,
                end_at=end_at,
                fetch=None,
            )
        read = getattr(self.remote_provider, "read_series", None)
        if not callable(read):
            raise SharedMarketDataError("remote provider must implement read_series.")

        def fetch(gap_start: datetime, gap_end: datetime) -> MarketDataSeries:
            return read(mapping, start_at=gap_start, end_at=gap_end)

        return self.store.synchronize(
            identity,
            start_at=start_at,
            end_at=end_at,
            fetch=fetch,
        )

    def read_option_chain(
        self,
        *,
        underlying_symbol: str,
        as_of_at: datetime,
        underlying_price: float,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        remote = self._option_delegate("read_option_chain")
        if remote is not None:
            return cast(tuple[OptionContractSnapshot, ...], remote(
                underlying_symbol=underlying_symbol,
                as_of_at=as_of_at,
                underlying_price=underlying_price,
                policy=policy,
            ))
        return self.store.read_option_chain_metadata(underlying_symbol=underlying_symbol)

    def read_option_chain_metadata(
        self,
        *,
        underlying_symbol: str,
        as_of_at: datetime,
        underlying_price: float,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        remote = self._option_delegate("read_option_chain_metadata")
        if remote is not None:
            return cast(tuple[OptionContractSnapshot, ...], remote(
                underlying_symbol=underlying_symbol,
                as_of_at=as_of_at,
                underlying_price=underlying_price,
                policy=policy,
            ))
        return self.store.read_option_chain_metadata(underlying_symbol=underlying_symbol)

    def read_latest_option_trades(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionTradeObservation, ...]:
        remote = self._option_delegate("read_latest_option_trades")
        if remote is not None:
            return cast(
                tuple[OptionTradeObservation, ...],
                remote(contracts=contracts, as_of_at=as_of_at, policy=policy),
            )
        rows = self.store.read_option_trades(
            contract_symbols=tuple(contract.contract_symbol for contract in contracts),
            start_at=datetime.min.replace(tzinfo=UTC),
            end_at=as_of_at,
        )
        latest: dict[str, OptionTradeObservation] = {}
        for row in rows:
            previous = latest.get(row.contract_symbol)
            if previous is None or row.observed_at > previous.observed_at:
                latest[row.contract_symbol] = row
        return tuple(latest.values())

    def read_option_trades(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        start_at: datetime,
        end_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionTradeObservation, ...]:
        remote = self._option_delegate("read_option_trades")
        if remote is not None:
            return cast(tuple[OptionTradeObservation, ...], remote(
                contracts=contracts,
                start_at=start_at,
                end_at=end_at,
                policy=policy,
            ))
        return self.store.read_option_trades(
            contract_symbols=tuple(contract.contract_symbol for contract in contracts),
            start_at=start_at,
            end_at=end_at,
        )

    def read_latest_option_quotes(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        remote = self._option_delegate("read_latest_option_quotes")
        if remote is not None:
            return cast(
                tuple[OptionContractSnapshot, ...],
                remote(contracts=contracts, as_of_at=as_of_at, policy=policy),
            )
        requested = {contract.contract_symbol for contract in contracts}
        return tuple(
            contract
            for contract in self.store.read_option_chain_metadata(
                underlying_symbol=contracts[0].underlying_symbol if contracts else ""
            )
            if contract.contract_symbol in requested
        )

    def read_option_bars(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        start_at: datetime,
        end_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionBarObservation, ...]:
        remote = self._option_delegate("read_option_bars")
        if remote is not None:
            return cast(tuple[OptionBarObservation, ...], remote(
                contracts=contracts,
                start_at=start_at,
                end_at=end_at,
                policy=policy,
            ))
        return self.store.read_option_bars(
            contract_symbols=tuple(contract.contract_symbol for contract in contracts),
            start_at=start_at,
            end_at=end_at,
        )

    def _option_delegate(self, name: str) -> Callable[..., Any] | None:
        if self.snapshot_id is not None:
            return None
        remote = self.remote_provider
        if remote is None:
            return None
        delegate = getattr(remote, name, None)
        if not callable(delegate):
            raise SharedMarketDataError(f"remote provider must implement {name}.")
        return cast(Callable[..., Any], delegate)

    def pop_source_metadata(self) -> dict[str, object]:
        metadata = dict(self._source_metadata)
        self._source_metadata["remote_fallback"] = self.snapshot_id is None
        self._source_metadata.pop("returned_bar_count", None)
        return metadata

    def __getattr__(self, name: str) -> object:
        """Expose option reads through the owned remote provider when available."""
        remote = object.__getattribute__(self, "remote_provider")
        if remote is not None:
            attribute = getattr(remote, name, None)
            if attribute is not None:
                return attribute
        raise AttributeError(name)

    def close(self) -> None:
        close = getattr(self.remote_provider, "close", None)
        if callable(close):
            close()


def _bar_payload(bar: MarketDataBar) -> dict[str, object]:
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


def _option_json(value: Any) -> str:
    return json.dumps(
        asdict(value),
        default=lambda item: item.isoformat() if isinstance(item, datetime) else item,
        ensure_ascii=False,
        sort_keys=True,
    )


def _option_payload(value: str) -> dict[str, object]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise SharedMarketDataError("stored option payload must be an object.")
    return payload


def _option_datetime(payload: dict[str, object], field_name: str) -> datetime | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SharedMarketDataError(f"stored option {field_name} must be a timestamp.")
    return _parse_utc(value)


def _option_contract_from_json(value: str) -> OptionContractSnapshot:
    payload = _option_payload(value)
    for field_name in ("observed_at", "latest_trade_at", "latest_quote_at"):
        payload[field_name] = _option_datetime(payload, field_name)
    return OptionContractSnapshot(**cast(Any, payload))


def _option_trade_from_json(value: str) -> OptionTradeObservation:
    payload = _option_payload(value)
    payload["observed_at"] = _option_datetime(payload, "observed_at")
    return OptionTradeObservation(**cast(Any, payload))


def _option_bar_from_json(value: str) -> OptionBarObservation:
    payload = _option_payload(value)
    payload["start_at"] = _option_datetime(payload, "start_at")
    payload["end_at"] = _option_datetime(payload, "end_at")
    return OptionBarObservation(**cast(Any, payload))


def _bar_from_row(row: sqlite3.Row) -> MarketDataBar:
    return MarketDataBar(
        start_at=_parse_utc(row["start_at"]),
        end_at=_parse_utc(row["end_at"]),
        open_price=float(row["open_price"]),
        high_price=float(row["high_price"]),
        low_price=float(row["low_price"]),
        close_price=float(row["close_price"]),
        volume=float(row["volume"]),
        vwap=None if row["vwap"] is None else float(row["vwap"]),
    )


def _missing_intervals(
    rows: list[sqlite3.Row],
    start_at: str,
    end_at: str,
    identity: MarketSeriesIdentity,
) -> tuple[tuple[datetime, datetime], ...]:
    """Return only expected trading intervals not covered by stored bars."""
    start = _parse_utc(start_at)
    end = _completed_end_at(_parse_utc(end_at), identity)
    if start >= end:
        return ()
    expected = _expected_intervals(identity, start, end)
    ordered = sorted(
        (
            _parse_utc(row["start_at"]),
            _parse_utc(row["end_at"]),
        )
        for row in rows
    )
    missing: list[tuple[datetime, datetime]] = []
    for expected_start, expected_end in expected:
        cursor = expected_start
        for row_start, row_end in ordered:
            if row_end <= cursor:
                continue
            if row_start > cursor:
                missing.append((cursor, min(row_start, expected_end)))
            cursor = max(cursor, row_end)
            if cursor >= expected_end:
                break
        if cursor < expected_end:
            missing.append((cursor, expected_end))
    return tuple(
        (gap_start, gap_end)
        for gap_start, gap_end in missing
        if gap_start < gap_end
    )


def _covers_window(
    rows: list[sqlite3.Row],
    start_at: str,
    end_at: str,
    identity: MarketSeriesIdentity,
) -> bool:
    if not rows:
        effective_end = _completed_end_at(_parse_utc(end_at), identity)
        if _parse_utc(start_at) >= effective_end:
            return True
        expected = _expected_intervals(identity, _parse_utc(start_at), effective_end)
        return not expected
    ordered = sorted(rows, key=lambda row: (row["start_at"], row["end_at"]))
    _validate_bar_intervals(
        tuple(_bar_from_row(row) for row in ordered),
        identity=identity,
    )
    return not _missing_intervals(rows, start_at, end_at, identity)


def _expected_intervals(
    identity: MarketSeriesIdentity,
    start_at: datetime,
    end_at: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    if identity.market_session == "exchange_session":
        return expected_market_bar_intervals(
            market_session=identity.market_session,
            exchange=identity.exchange,
            exchange_session_scope=identity.exchange_session_scope,
            bar_granularity=identity.bar_granularity,
            start_at=start_at,
            end_at=end_at,
        )
    return expected_market_intervals(
        market_session=identity.market_session,
        exchange=identity.exchange,
        exchange_session_scope=identity.exchange_session_scope,
        start_at=start_at,
        end_at=end_at,
    )


def _expected_bar_intervals(
    identity: MarketSeriesIdentity,
    start_at: datetime,
    end_at: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    return expected_market_bar_intervals(
        market_session=identity.market_session,
        exchange=identity.exchange,
        exchange_session_scope=identity.exchange_session_scope,
        bar_granularity=identity.bar_granularity,
        start_at=start_at,
        end_at=end_at,
    )


def _validate_bar_intervals(
    bars: tuple[MarketDataBar, ...],
    *,
    identity: MarketSeriesIdentity,
    existing_rows: list[sqlite3.Row] | None = None,
) -> None:
    granularity = _granularity_seconds(identity.bar_granularity)
    intervals = sorted((bar.start_at, bar.end_at) for bar in bars)
    if granularity <= 0:
        raise SharedMarketDataError(
            f"unsupported bar granularity: {identity.bar_granularity!r}"
        )
    for start, end in intervals:
        if (end - start).total_seconds() > granularity:
            raise SharedMarketDataError(
                f"bar interval {start.isoformat()}..{end.isoformat()} exceeds "
                f"declared {identity.bar_granularity} granularity."
            )
    for previous, current in zip(intervals, intervals[1:], strict=False):
        if current[0] < previous[1] and current != previous:
            raise SharedMarketDataError(
                "market bars contain overlapping intervals: "
                f"{previous[0].isoformat()}..{previous[1].isoformat()} and "
                f"{current[0].isoformat()}..{current[1].isoformat()}"
            )
    if existing_rows is not None:
        existing = [
            (_parse_utc(row["start_at"]), _parse_utc(row["end_at"]))
            for row in existing_rows
        ]
        for start, end in intervals:
            for existing_start, existing_end in existing:
                if (start, end) == (existing_start, existing_end):
                    continue
                if start < existing_end and existing_start < end:
                    raise SharedMarketDataError(
                        "market bar overlaps an existing interval: "
                        f"{start.isoformat()}..{end.isoformat()} with "
                        f"{existing_start.isoformat()}..{existing_end.isoformat()}"
                    )
    if identity.market_session == "exchange_session" and intervals:
        calendar_start = min(start for start, _end in intervals) - timedelta(days=2)
        calendar_end = max(end for _start, end in intervals) + timedelta(days=2)
        expected = expected_market_intervals(
            market_session=identity.market_session,
            exchange=identity.exchange,
            exchange_session_scope=identity.exchange_session_scope,
            start_at=calendar_start,
            end_at=calendar_end,
        )
        for start, end in intervals:
            if not any(
                session_start <= start and end <= session_end
                for session_start, session_end in expected
            ):
                raise SharedMarketDataError(
                    "market bar is outside or crosses an exchange session: "
                    f"{start.isoformat()}..{end.isoformat()}"
                )


def _read_all_rows(connection: sqlite3.Connection, series_key: str) -> list[sqlite3.Row]:
    return connection.execute(
        """
        WITH ranked AS (
            SELECT b.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY b.series_key, b.start_at, b.end_at
                       ORDER BY b.recorded_at DESC, b.rowid DESC
                   ) AS revision_rank
            FROM bars b
            WHERE b.series_key = ?
        )
        SELECT * FROM ranked WHERE revision_rank = 1 ORDER BY start_at, end_at
        """,
        (series_key,),
    ).fetchall()


def _granularity_seconds(granularity: str) -> float:
    value = granularity.strip().lower()
    if value.endswith("min"):
        return float(value[:-3]) * 60.0
    if value.endswith("h"):
        return float(value[:-1]) * 3600.0
    if value.endswith("d"):
        return float(value[:-1]) * 86400.0
    return 0.0


def _completed_end_iso(end_at: datetime, identity: MarketSeriesIdentity) -> str:
    return _completed_end_at(end_at, identity).isoformat()


def _aligned_start_iso(start_at: datetime, identity: MarketSeriesIdentity) -> str:
    return _aligned_start_at(start_at, identity).isoformat()


def _completed_end_at(end_at: datetime, identity: MarketSeriesIdentity) -> datetime:
    """Return the last complete bar boundary visible at ``end_at``.

    A live request can end inside the currently forming bar.  Coverage and
    synchronization must stop at the preceding granularity boundary so that
    the provider is never asked for a bar which cannot yet exist.  Historical
    requests ending exactly on a boundary are unchanged.  This is a
    provider-neutral rule shared by continuous and exchange-session series.
    """
    if identity.market_session == "exchange_session":
        try:
            return completed_market_end(
                market_session=identity.market_session,
                exchange=identity.exchange,
                exchange_session_scope=identity.exchange_session_scope,
                bar_granularity=identity.bar_granularity,
                end_at=end_at,
            )
        except ValueError as exc:
            raise SharedMarketDataError(str(exc)) from exc
    return _bar_boundary_at(end_at, identity, field_name="end_at")


def _aligned_start_at(start_at: datetime, identity: MarketSeriesIdentity) -> datetime:
    """Include the completed bar that overlaps a non-boundary history start."""
    if identity.market_session == "exchange_session":
        try:
            return aligned_market_start(
                market_session=identity.market_session,
                exchange=identity.exchange,
                exchange_session_scope=identity.exchange_session_scope,
                bar_granularity=identity.bar_granularity,
                start_at=start_at,
            )
        except ValueError as exc:
            raise SharedMarketDataError(str(exc)) from exc
    return _bar_boundary_at(start_at, identity, field_name="start_at")


def _bar_boundary_at(
    value: datetime,
    identity: MarketSeriesIdentity,
    *,
    field_name: str,
) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SharedMarketDataError(f"{field_name} must be timezone-aware.")
    granularity_us = int(round(_granularity_seconds(identity.bar_granularity) * 1_000_000))
    if granularity_us <= 0:
        raise SharedMarketDataError(
            f"unsupported bar granularity: {identity.bar_granularity!r}"
        )
    end = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = end - epoch
    elapsed_us = (
        elapsed.days * 86_400 * 1_000_000
        + elapsed.seconds * 1_000_000
        + elapsed.microseconds
    )
    completed_us = elapsed_us - (elapsed_us % granularity_us)
    return epoch + timedelta(microseconds=completed_us)


def _utc_iso(value: datetime, field_name: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SharedMarketDataError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC).isoformat()


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


__all__ = [
    "MarketDataSnapshot",
    "MarketSeriesIdentity",
    "ObservedMarketDataPrefix",
    "SharedMarketDataError",
    "SharedMarketDataProvider",
    "SharedMarketDataStore",
    "shared_market_data_root",
    "read_workspace_snapshot_ref",
    "write_workspace_snapshot_ref",
]
