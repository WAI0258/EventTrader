"""Calendar-backed market-news REST cadence and WebSocket sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from event_trader.contracts._validators import validate_timestamp

MarketNewsWebSocketSessionMode = Literal["rth_only"]

_REST_CADENCE_PROFILE = "us_equity_news"
_SCHEDULE_SCAN_DAYS = 14


class MarketNewsScheduleError(ValueError):
    """Raised when market-news calendar scheduling cannot be resolved."""


@dataclass(frozen=True, slots=True)
class MarketNewsRestCadenceProfile:
    """Three-bucket cadence for scheduled market-news catch-up."""

    name: str
    timezone: str
    calendar: str
    rth_local_times: tuple[str, ...]
    trading_day_off_rth_local_times: tuple[str, ...]
    closed_day_local_times: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.name != _REST_CADENCE_PROFILE:
            raise MarketNewsScheduleError(
                f"unsupported live market_news REST cadence_profile: {self.name!r}."
            )
        _timezone(self.timezone)
        _calendar(self.calendar)
        object.__setattr__(
            self,
            "rth_local_times",
            _validate_local_time_strings(
                self.rth_local_times,
                field_name="rth_local_times",
            ),
        )
        object.__setattr__(
            self,
            "trading_day_off_rth_local_times",
            _validate_local_time_strings(
                self.trading_day_off_rth_local_times,
                field_name="trading_day_off_rth_local_times",
            ),
        )
        object.__setattr__(
            self,
            "closed_day_local_times",
            _validate_local_time_strings(
                self.closed_day_local_times,
                field_name="closed_day_local_times",
            ),
        )

    def next_due_after(self, after: datetime) -> datetime:
        """Return the next REST due time strictly after a UTC timestamp."""
        validated_after = _validate_utc_datetime(after, field_name="after")
        exchange_date = validated_after.astimezone(_timezone(self.timezone)).date()
        candidates: list[datetime] = []
        for offset in range(-1, _SCHEDULE_SCAN_DAYS):
            candidates.extend(
                self._candidate_times_for_exchange_date(
                    exchange_date + timedelta(days=offset)
                )
            )
        for candidate in sorted(set(candidates)):
            if candidate > validated_after:
                return candidate
        raise MarketNewsScheduleError(
            "could not resolve the next live market_news REST due time "
            "within scan horizon."
        )

    def _candidate_times_for_exchange_date(self, exchange_date: date) -> tuple[datetime, ...]:
        market_open, market_close = _session_bounds(self.calendar, exchange_date)
        profile_tz = _timezone(self.timezone)
        if market_open is None or market_close is None:
            return tuple(
                _local_datetime_to_utc(
                    local_date=exchange_date,
                    local_time=local_time,
                    timezone=profile_tz,
                )
                for local_time in _parse_local_times(self.closed_day_local_times)
            )

        candidates: list[datetime] = []
        for local_time in _parse_local_times(self.rth_local_times):
            candidate = _local_datetime_to_utc(
                local_date=exchange_date,
                local_time=local_time,
                timezone=profile_tz,
            )
            if market_open <= candidate <= market_close:
                candidates.append(candidate)
        for local_time in _parse_local_times(self.trading_day_off_rth_local_times):
            candidate = _local_datetime_to_utc(
                local_date=exchange_date,
                local_time=local_time,
                timezone=profile_tz,
            )
            if candidate < market_open or candidate > market_close:
                candidates.append(candidate)
        return tuple(candidates)


@dataclass(frozen=True, slots=True)
class MarketNewsWebSocketSessionPolicy:
    """Calendar policy for deciding whether a realtime news stream should run."""

    mode: MarketNewsWebSocketSessionMode
    timezone: str
    calendar: str
    open_buffer_minutes: int
    close_buffer_minutes: int

    def __post_init__(self) -> None:
        if self.mode != "rth_only":
            raise MarketNewsScheduleError(
                f"unsupported live market_news websocket session mode: {self.mode!r}."
            )
        _timezone(self.timezone)
        _calendar(self.calendar)
        if (
            not isinstance(self.open_buffer_minutes, int)
            or isinstance(self.open_buffer_minutes, bool)
            or self.open_buffer_minutes < 0
        ):
            raise MarketNewsScheduleError("open_buffer_minutes must be >= 0.")
        if (
            not isinstance(self.close_buffer_minutes, int)
            or isinstance(self.close_buffer_minutes, bool)
            or self.close_buffer_minutes < 0
        ):
            raise MarketNewsScheduleError("close_buffer_minutes must be >= 0.")

    def is_active(self, now: datetime) -> bool:
        """Return true when the WebSocket should be connected at a UTC timestamp."""
        validated_now = _validate_utc_datetime(now, field_name="now")
        exchange_date = validated_now.astimezone(_timezone(self.timezone)).date()
        for offset in (-1, 0, 1):
            market_open, market_close = _session_bounds(
                self.calendar,
                exchange_date + timedelta(days=offset),
            )
            if market_open is None or market_close is None:
                continue
            active_start = market_open - timedelta(minutes=self.open_buffer_minutes)
            active_end = market_close + timedelta(minutes=self.close_buffer_minutes)
            if active_start <= validated_now < active_end:
                return True
        return False


@lru_cache(maxsize=1024)
def _session_bounds(
    calendar_name: str,
    exchange_date: date,
) -> tuple[datetime | None, datetime | None]:
    calendar = _calendar(calendar_name)
    schedule = calendar.schedule(
        start_date=exchange_date.isoformat(),
        end_date=exchange_date.isoformat(),
    )
    if schedule.empty:
        return None, None
    row = schedule.iloc[0]
    return (
        row["market_open"].to_pydatetime().astimezone(UTC),
        row["market_close"].to_pydatetime().astimezone(UTC),
    )


@lru_cache(maxsize=32)
def _calendar(calendar_name: str) -> Any:
    try:
        import pandas_market_calendars as mcal  # type: ignore[import-untyped]
    except ModuleNotFoundError as exc:
        raise MarketNewsScheduleError(
            "pandas-market-calendars is required for live market_news scheduling."
        ) from exc
    try:
        return mcal.get_calendar(calendar_name)
    except Exception as exc:
        raise MarketNewsScheduleError(
            f"unknown live market_news calendar: {calendar_name!r}."
        ) from exc


@lru_cache(maxsize=32)
def _timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise MarketNewsScheduleError(
            f"unknown live market_news timezone: {timezone_name!r}."
        ) from exc


def _local_datetime_to_utc(
    *,
    local_date: date,
    local_time: time,
    timezone: ZoneInfo,
) -> datetime:
    return datetime.combine(local_date, local_time, tzinfo=timezone).astimezone(UTC)


def _validate_local_time_strings(
    value: tuple[str, ...],
    *,
    field_name: str,
) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        raise MarketNewsScheduleError(f"{field_name} must be a non-empty tuple.")
    parsed = _parse_local_times(value)
    normalized = tuple(local_time.strftime("%H:%M") for local_time in parsed)
    if len(set(normalized)) != len(normalized):
        raise MarketNewsScheduleError(f"{field_name} must not contain duplicate times.")
    return normalized


def _parse_local_times(value: tuple[str, ...]) -> tuple[time, ...]:
    parsed_times: list[time] = []
    for raw_time in value:
        if not isinstance(raw_time, str):
            raise MarketNewsScheduleError("local cadence times must be strings.")
        try:
            hour_text, minute_text = raw_time.split(":", 1)
            parsed_times.append(time(hour=int(hour_text), minute=int(minute_text)))
        except (TypeError, ValueError) as exc:
            raise MarketNewsScheduleError(
                f"local cadence time must use HH:MM format: {raw_time!r}."
            ) from exc
    return tuple(parsed_times)


def _validate_utc_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise MarketNewsScheduleError(f"{field_name} must be a datetime.")
    return validate_timestamp(
        value,
        field_name=field_name,
        error_type=MarketNewsScheduleError,
    ).astimezone(UTC)


__all__ = [
    "MarketNewsRestCadenceProfile",
    "MarketNewsScheduleError",
    "MarketNewsWebSocketSessionMode",
    "MarketNewsWebSocketSessionPolicy",
]
