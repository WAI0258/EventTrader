"""Calendar-backed live web-search cadence profiles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from event_trader.contracts._validators import validate_timestamp

_US_EQUITY_PROFILE = "us_equity_active"
_CN_A_SHARE_PROFILE = "cn_a_share_active"
_TRADING_DAY_WINDOW_BEFORE_OPEN = timedelta(hours=12)
_TRADING_DAY_WINDOW_AFTER_CLOSE = timedelta(hours=12)
_SCHEDULE_SCAN_DAYS = 14

_PROFILE_CALENDARS: dict[str, str] = {
    _US_EQUITY_PROFILE: "NYSE",
    _CN_A_SHARE_PROFILE: "SSE",
}


class LiveWebSearchScheduleError(ValueError):
    """Raised when live web-search cadence state is invalid."""


@dataclass(frozen=True, slots=True)
class LiveWebSearchCadenceProfile:
    """Named business cadence for live web-search source acquisition."""

    name: str
    timezone: str
    trading_day_local_times: tuple[str, ...]
    closed_day_local_times: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.name not in _PROFILE_CALENDARS:
            raise LiveWebSearchScheduleError(
                f"unsupported live web_search cadence_profile: {self.name!r}."
            )
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise LiveWebSearchScheduleError(
                f"unknown live web_search timezone: {self.timezone!r}."
            ) from exc
        object.__setattr__(
            self,
            "trading_day_local_times",
            _validate_local_time_strings(
                self.trading_day_local_times,
                field_name="trading_day_local_times",
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
        """Return the next profile due time strictly after a UTC timestamp."""
        validated_after = _validate_utc_datetime(after, field_name="after")
        candidates: list[datetime] = []
        exchange_date = validated_after.astimezone(ZoneInfo(self.timezone)).date()
        for offset in range(-1, _SCHEDULE_SCAN_DAYS):
            candidates.extend(
                self._candidate_times_for_exchange_date(
                    exchange_date + timedelta(days=offset)
                )
            )
        for candidate in sorted(set(candidates)):
            if candidate > validated_after:
                return candidate
        raise LiveWebSearchScheduleError(
            "could not resolve the next live web_search due time within scan horizon."
        )

    def _candidate_times_for_exchange_date(self, exchange_date: date) -> tuple[datetime, ...]:
        market_open, market_close = _session_bounds(self.name, exchange_date)
        profile_tz = ZoneInfo(self.timezone)
        if market_open is None or market_close is None:
            return tuple(
                _local_datetime_to_utc(
                    local_date=exchange_date,
                    local_time=local_time,
                    timezone=profile_tz,
                )
                for local_time in _parse_local_times(self.closed_day_local_times)
            )

        lower_bound = market_open - _TRADING_DAY_WINDOW_BEFORE_OPEN
        upper_bound = market_close + _TRADING_DAY_WINDOW_AFTER_CLOSE
        local_start = market_open.astimezone(profile_tz).date() - timedelta(days=1)
        local_end = market_close.astimezone(profile_tz).date() + timedelta(days=1)
        candidates: list[datetime] = []
        local_times = _parse_local_times(self.trading_day_local_times)
        local_date = local_start
        while local_date <= local_end:
            for local_time in local_times:
                candidate = _local_datetime_to_utc(
                    local_date=local_date,
                    local_time=local_time,
                    timezone=profile_tz,
                )
                if lower_bound <= candidate <= upper_bound:
                    candidates.append(candidate)
            local_date += timedelta(days=1)
        return tuple(candidates)


@dataclass(frozen=True, slots=True)
class LiveWebSearchDueWindow:
    """One scheduled live web-search window bounded by due times."""

    window_start: datetime
    window_end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "window_start",
            _validate_utc_datetime(self.window_start, field_name="window_start"),
        )
        object.__setattr__(
            self,
            "window_end",
            _validate_utc_datetime(self.window_end, field_name="window_end"),
        )
        if self.window_end <= self.window_start:
            raise LiveWebSearchScheduleError(
                "window_end must be after window_start."
            )


@dataclass(frozen=True, slots=True)
class LiveWebSearchDuePlan:
    """All scheduled web-search windows due at or before a UTC timestamp."""

    cursor_at: datetime
    next_due_at: datetime
    due_windows: tuple[LiveWebSearchDueWindow, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cursor_at",
            _validate_utc_datetime(self.cursor_at, field_name="cursor_at"),
        )
        object.__setattr__(
            self,
            "next_due_at",
            _validate_utc_datetime(self.next_due_at, field_name="next_due_at"),
        )
        if self.next_due_at <= self.cursor_at:
            raise LiveWebSearchScheduleError(
                "next_due_at must be after cursor_at."
            )
        if not isinstance(self.due_windows, tuple):
            raise LiveWebSearchScheduleError("due_windows must be a tuple.")
        if not all(isinstance(item, LiveWebSearchDueWindow) for item in self.due_windows):
            raise LiveWebSearchScheduleError(
                "due_windows must contain only LiveWebSearchDueWindow instances."
            )


def plan_live_web_search_due_windows(
    cadence_profile: LiveWebSearchCadenceProfile,
    *,
    cursor_at: datetime,
    now: datetime,
    max_windows: int | None = None,
) -> LiveWebSearchDuePlan:
    """Plan scheduled web-search windows due after one durable cursor."""
    if not isinstance(cadence_profile, LiveWebSearchCadenceProfile):
        raise LiveWebSearchScheduleError(
            "cadence_profile must be a LiveWebSearchCadenceProfile instance."
        )
    validated_cursor = _validate_utc_datetime(cursor_at, field_name="cursor_at")
    validated_now = _validate_utc_datetime(now, field_name="now")
    if max_windows is not None:
        if isinstance(max_windows, bool) or not isinstance(max_windows, int):
            raise LiveWebSearchScheduleError("max_windows must be a positive integer.")
        if max_windows <= 0:
            raise LiveWebSearchScheduleError("max_windows must be greater than zero.")

    next_due_at = cadence_profile.next_due_after(validated_cursor)
    due_windows: list[LiveWebSearchDueWindow] = []
    window_start = validated_cursor
    while next_due_at <= validated_now:
        due_windows.append(
            LiveWebSearchDueWindow(
                window_start=window_start,
                window_end=next_due_at,
            )
        )
        window_start = next_due_at
        next_due_at = cadence_profile.next_due_after(window_start)
        if max_windows is not None and len(due_windows) >= max_windows:
            break

    return LiveWebSearchDuePlan(
        cursor_at=validated_cursor,
        next_due_at=next_due_at,
        due_windows=tuple(due_windows),
    )


@lru_cache(maxsize=512)
def _session_bounds(
    profile_name: str,
    exchange_date: date,
) -> tuple[datetime | None, datetime | None]:
    calendar = _calendar(_calendar_name(profile_name))
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


def _calendar_name(profile_name: str) -> str:
    try:
        return _PROFILE_CALENDARS[profile_name]
    except KeyError as exc:
        raise LiveWebSearchScheduleError(
            f"unsupported live web_search cadence_profile: {profile_name!r}."
        ) from exc


@lru_cache(maxsize=4)
def _calendar(calendar_name: str) -> Any:
    try:
        import pandas_market_calendars as mcal  # type: ignore[import-untyped]
    except ModuleNotFoundError as exc:
        raise LiveWebSearchScheduleError(
            "pandas-market-calendars is required for live web_search cadence."
        ) from exc
    try:
        return mcal.get_calendar(calendar_name)
    except Exception as exc:
        raise LiveWebSearchScheduleError(
            f"unknown live web_search calendar: {calendar_name!r}."
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
        raise LiveWebSearchScheduleError(f"{field_name} must be a non-empty tuple.")
    parsed = _parse_local_times(value)
    normalized = tuple(local_time.strftime("%H:%M") for local_time in parsed)
    if len(set(normalized)) != len(normalized):
        raise LiveWebSearchScheduleError(f"{field_name} must not contain duplicate times.")
    return normalized


def _parse_local_times(value: tuple[str, ...]) -> tuple[time, ...]:
    parsed_times: list[time] = []
    for raw_time in value:
        if not isinstance(raw_time, str):
            raise LiveWebSearchScheduleError("local cadence times must be strings.")
        try:
            hour_text, minute_text = raw_time.split(":", 1)
            parsed_times.append(time(hour=int(hour_text), minute=int(minute_text)))
        except (TypeError, ValueError) as exc:
            raise LiveWebSearchScheduleError(
                f"local cadence time must use HH:MM format: {raw_time!r}."
            ) from exc
    return tuple(parsed_times)


def _validate_utc_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise LiveWebSearchScheduleError(f"{field_name} must be a datetime.")
    return validate_timestamp(
        value,
        field_name=field_name,
        error_type=LiveWebSearchScheduleError,
    ).astimezone(UTC)


__all__ = [
    "LiveWebSearchCadenceProfile",
    "LiveWebSearchDuePlan",
    "LiveWebSearchDueWindow",
    "LiveWebSearchScheduleError",
    "plan_live_web_search_due_windows",
]
