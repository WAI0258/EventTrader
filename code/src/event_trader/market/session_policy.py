from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

MarketSession = Literal["continuous", "exchange_session"]
ExchangeSessionScope = Literal["regular", "extended"]

ALLOWED_MARKET_SESSIONS: tuple[MarketSession, ...] = (
    "continuous",
    "exchange_session",
)
ALLOWED_EXCHANGE_SESSION_SCOPES: tuple[ExchangeSessionScope, ...] = (
    "regular",
    "extended",
)

_SUPPORTED_EXCHANGES = {"NYSE", "NASDAQ"}


def expected_market_intervals(
    *,
    market_session: str,
    exchange: str | None,
    exchange_session_scope: str | None,
    start_at: datetime,
    end_at: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    """Return the UTC portions of a request during which bars are expected.

    Session-bound series are constrained by the exchange calendar; closed
    periods therefore do not become false market-data gaps.  This is kept in
    the session policy module so every market-data consumer uses one rule.
    """
    if start_at.tzinfo is None or start_at.utcoffset() is None:
        raise ValueError("start_at must be timezone-aware.")
    if end_at.tzinfo is None or end_at.utcoffset() is None:
        raise ValueError("end_at must be timezone-aware.")
    start = start_at.astimezone(UTC)
    end = end_at.astimezone(UTC)
    if start >= end:
        raise ValueError("start_at must be before end_at.")
    normalized_market_session = market_session.lower()
    normalized_scope = None if exchange_session_scope is None else exchange_session_scope.lower()
    if normalized_market_session == "continuous":
        return ((start, end),)
    if normalized_market_session != "exchange_session":
        raise ValueError(f"unsupported market_session: {market_session!r}")
    if exchange is None or exchange.upper() not in _SUPPORTED_EXCHANGES:
        raise ValueError(f"unsupported exchange: {exchange!r}")
    if normalized_scope not in ALLOWED_EXCHANGE_SESSION_SCOPES:
        raise ValueError(f"unsupported exchange_session_scope: {exchange_session_scope!r}")

    import pandas_market_calendars as mcal  # type: ignore[import-untyped]

    calendar = mcal.get_calendar(exchange.upper())
    local_start = start.astimezone(calendar.tz).date()
    local_end = end.astimezone(calendar.tz).date()
    schedule = calendar.schedule(
        start_date=local_start,
        end_date=local_end,
        start="pre" if normalized_scope == "extended" else "market_open",
        end="post" if normalized_scope == "extended" else "market_close",
    )
    open_column = "pre" if normalized_scope == "extended" else "market_open"
    close_column = "post" if normalized_scope == "extended" else "market_close"
    intervals: list[tuple[datetime, datetime]] = []
    for row in schedule.itertuples(index=False):
        interval_start = getattr(row, open_column).to_pydatetime().astimezone(UTC)
        interval_end = getattr(row, close_column).to_pydatetime().astimezone(UTC)
        clipped_start = max(interval_start, start)
        clipped_end = min(interval_end, end)
        if clipped_start < clipped_end:
            intervals.append((clipped_start, clipped_end))
    return tuple(intervals)


def expected_market_bar_intervals(
    *,
    market_session: str,
    exchange: str | None,
    exchange_session_scope: str | None,
    bar_granularity: str,
    start_at: datetime,
    end_at: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    """Return canonical expected bar slots for an exchange-session series.

    Intraday exchange bars are anchored to the exchange's regular-session open
    when the extended session crosses that boundary.  This matches canonical
    end-anchored feeds while keeping the session policy provider-neutral.
    """
    start, end = _validate_interval_bounds(start_at, end_at)
    normalized_market_session = market_session.lower()
    if normalized_market_session == "continuous":
        delta = _bar_delta(bar_granularity)
        return _bar_slots(((start, end),), delta=delta)
    if normalized_market_session != "exchange_session":
        raise ValueError(f"unsupported market_session: {market_session!r}")
    segments = _exchange_session_segments(
        exchange=exchange,
        exchange_session_scope=exchange_session_scope,
        start_at=start,
        end_at=end,
    )
    return _bar_slots(segments, delta=_bar_delta(bar_granularity), clip=(start, end))


def completed_market_end(
    *,
    market_session: str,
    exchange: str | None,
    exchange_session_scope: str | None,
    bar_granularity: str,
    end_at: datetime,
) -> datetime:
    """Return the latest canonical exchange bar end not after ``end_at``."""
    end = _validate_timestamp(end_at, "end_at")
    if market_session.lower() == "continuous":
        delta = _bar_delta(bar_granularity)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        elapsed = end - epoch
        completed_boundary = epoch + (elapsed // delta) * delta
        return completed_boundary
    if market_session.lower() != "exchange_session":
        raise ValueError(f"unsupported market_session: {market_session!r}")
    # Include prior sessions so a closed period/weekend resolves to the last
    # observed boundary without turning the closure into a missing interval.
    start = end - timedelta(days=14)
    segments = _exchange_session_segments(
        exchange=exchange,
        exchange_session_scope=exchange_session_scope,
        start_at=start,
        end_at=end,
    )
    intervals = _bar_slots(segments, delta=_bar_delta(bar_granularity))
    completed_ends = [
        interval_end for _interval_start, interval_end in intervals if interval_end <= end
    ]
    if completed_ends:
        return max(completed_ends)
    return start


def aligned_market_start(
    *,
    market_session: str,
    exchange: str | None,
    exchange_session_scope: str | None,
    bar_granularity: str,
    start_at: datetime,
) -> datetime:
    """Return the canonical bar start containing ``start_at`` when active."""
    start = _validate_timestamp(start_at, "start_at")
    if market_session.lower() == "continuous":
        delta = _bar_delta(bar_granularity)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        elapsed = start - epoch
        return epoch + (elapsed // delta) * delta
    if market_session.lower() != "exchange_session":
        raise ValueError(f"unsupported market_session: {market_session!r}")
    segments = _exchange_session_segments(
        exchange=exchange,
        exchange_session_scope=exchange_session_scope,
        start_at=start - timedelta(days=1),
        end_at=start + timedelta(days=1),
    )
    for slot_start, slot_end in _bar_slots(segments, delta=_bar_delta(bar_granularity)):
        if slot_start <= start < slot_end:
            return slot_start
    # A closed period has no containing bar.  Preserve the request boundary so
    # closure handling remains session-aware and does not invent observations.
    return start


def _exchange_session_segments(
    *,
    exchange: str | None,
    exchange_session_scope: str | None,
    start_at: datetime,
    end_at: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    normalized_scope = None if exchange_session_scope is None else exchange_session_scope.lower()
    if exchange is None or exchange.upper() not in _SUPPORTED_EXCHANGES:
        raise ValueError(f"unsupported exchange: {exchange!r}")
    if normalized_scope not in ALLOWED_EXCHANGE_SESSION_SCOPES:
        raise ValueError(f"unsupported exchange_session_scope: {exchange_session_scope!r}")

    import pandas_market_calendars as mcal

    calendar = mcal.get_calendar(exchange.upper())
    local_start = start_at.astimezone(calendar.tz).date()
    local_end = end_at.astimezone(calendar.tz).date()
    schedule = calendar.schedule(
        start_date=local_start,
        end_date=local_end,
        start="pre" if normalized_scope == "extended" else "market_open",
        end="post" if normalized_scope == "extended" else "market_close",
    )
    segments: list[tuple[datetime, datetime]] = []
    for row in schedule.itertuples(index=False):
        market_open = row.market_open.to_pydatetime().astimezone(UTC)
        market_close = row.market_close.to_pydatetime().astimezone(UTC)
        candidates: tuple[tuple[datetime, datetime], ...]
        if normalized_scope == "regular":
            candidates = ((market_open, market_close),)
        else:
            pre = row.pre.to_pydatetime().astimezone(UTC)
            post = row.post.to_pydatetime().astimezone(UTC)
            candidates = (
                (pre, market_open),
                (market_open, market_close),
                (market_close, post),
            )
        for segment_start, segment_end in candidates:
            if segment_start < end_at and start_at < segment_end:
                segments.append((segment_start, segment_end))
    return tuple(segments)


def _bar_slots(
    segments: tuple[tuple[datetime, datetime], ...],
    *,
    delta: timedelta,
    clip: tuple[datetime, datetime] | None = None,
) -> tuple[tuple[datetime, datetime], ...]:
    intervals: list[tuple[datetime, datetime]] = []
    for segment_start, segment_end in segments:
        cursor = segment_start
        while cursor < segment_end:
            interval_end = min(cursor + delta, segment_end)
            clipped_start = cursor
            clipped_end = interval_end
            if clip is not None:
                clipped_start = max(clipped_start, clip[0])
                clipped_end = min(clipped_end, clip[1])
            if clipped_start < clipped_end:
                intervals.append((clipped_start, clipped_end))
            cursor = interval_end
    return tuple(intervals)


def _bar_delta(bar_granularity: str) -> timedelta:
    normalized = bar_granularity.strip().lower()
    if normalized.endswith("min"):
        normalized = f"{normalized[:-3]}m"
    if normalized.endswith("m"):
        return timedelta(minutes=int(normalized[:-1]))
    if normalized.endswith("h"):
        return timedelta(hours=int(normalized[:-1]))
    if normalized.endswith("d"):
        return timedelta(days=int(normalized[:-1]))
    raise ValueError(f"unsupported bar granularity: {bar_granularity!r}")


def _validate_timestamp(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


def _validate_interval_bounds(start_at: datetime, end_at: datetime) -> tuple[datetime, datetime]:
    start = _validate_timestamp(start_at, "start_at")
    end = _validate_timestamp(end_at, "end_at")
    if start >= end:
        raise ValueError("start_at must be before end_at.")
    return start, end


def validate_market_session_contract_fields(
    *,
    market_session: str,
    exchange: str | None,
    exchange_session_scope: str | None,
    error_type: type[Exception],
    market_session_field_name: str = "market_session",
    exchange_field_name: str = "exchange",
    exchange_session_scope_field_name: str = "exchange_session_scope",
    invalid_market_session_message: str | None = None,
    invalid_exchange_session_scope_message: str | None = None,
    continuous_exchange_message: str | None = None,
    continuous_exchange_session_scope_message: str | None = None,
    exchange_session_exchange_message: str | None = None,
    exchange_session_scope_message: str | None = None,
) -> None:
    """Validate that market session + exchange fields obey internal session policy."""
    if invalid_market_session_message is None:
        invalid_market_session_message = (
            f"{market_session_field_name} must be 'continuous' or 'exchange_session'."
        )
    if invalid_exchange_session_scope_message is None:
        invalid_exchange_session_scope_message = (
            f"{exchange_session_scope_field_name} must be 'regular' or 'extended'."
        )
    if continuous_exchange_message is None:
        continuous_exchange_message = (
            f"{exchange_field_name} must be absent when "
            f"{market_session_field_name} = 'continuous'."
        )
    if continuous_exchange_session_scope_message is None:
        continuous_exchange_session_scope_message = (
            f"{exchange_session_scope_field_name} must be absent when "
            f"{market_session_field_name} = 'continuous'."
        )
    if exchange_session_exchange_message is None:
        exchange_session_exchange_message = (
            f"{exchange_field_name} is required when "
            f"{market_session_field_name} = 'exchange_session'."
        )
    if exchange_session_scope_message is None:
        exchange_session_scope_message = (
            f"{exchange_session_scope_field_name} is required when "
            f"{market_session_field_name} = 'exchange_session'."
        )

    if market_session not in ALLOWED_MARKET_SESSIONS:
        raise error_type(invalid_market_session_message)
    if (
        exchange_session_scope is not None
        and exchange_session_scope not in ALLOWED_EXCHANGE_SESSION_SCOPES
    ):
        raise error_type(invalid_exchange_session_scope_message)

    if market_session == "continuous":
        if exchange is not None:
            raise error_type(continuous_exchange_message)
        if exchange_session_scope is not None:
            raise error_type(continuous_exchange_session_scope_message)
        return

    if market_session == "exchange_session":
        if exchange is None:
            raise error_type(exchange_session_exchange_message)
        if exchange_session_scope is None:
            raise error_type(exchange_session_scope_message)


def build_market_session_identity(
    *,
    market_session: str,
    exchange: str | None,
    exchange_session_scope: str | None,
    bar_granularity: str,
    market_symbol: str,
) -> dict[str, object]:
    """Build canonical session identity for market-context metadata."""
    return {
        "market_session": market_session,
        "exchange": exchange,
        "exchange_session_scope": exchange_session_scope,
        "bar_granularity": bar_granularity,
        "market_symbol": market_symbol,
    }


def compact_market_session_identity(
    source_metadata: dict[str, object],
) -> dict[str, object]:
    """Extract canonical session identity from source metadata for compact snapshots."""
    keys = (
        "market_session",
        "exchange",
        "exchange_session_scope",
        "bar_granularity",
        "market_symbol",
    )
    identity = {key: source_metadata[key] for key in keys if key in source_metadata}
    if "market_symbol" not in identity:
        requested_symbol = source_metadata.get("requested_symbol")
        if requested_symbol is not None:
            identity["market_symbol"] = requested_symbol
    return identity


__all__ = [
    "ALLOWED_EXCHANGE_SESSION_SCOPES",
    "ALLOWED_MARKET_SESSIONS",
    "ExchangeSessionScope",
    "MarketSession",
    "build_market_session_identity",
    "compact_market_session_identity",
    "aligned_market_start",
    "completed_market_end",
    "expected_market_bar_intervals",
    "expected_market_intervals",
    "validate_market_session_contract_fields",
]
