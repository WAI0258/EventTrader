"""Deterministic anti-cheating guard for historical web-search material."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

_REASON_CODE = "future_observed_content_after_visible_at"
_SNIPPET_RADIUS = 50
_DATE_CONTEXT_RADIUS = 32
_IMPLICIT_YEAR_MAX_FUTURE_DAYS = 45

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))
_MONTH_DAY_RE = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(?P<year>\d{4}))?\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b(?P<year>20\d{2})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_CURRENT_PAGE_RE = re.compile(
    r"\b(?:current|dated|as of|accessed|page accessed|page rendered)\b",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(
    r"(?:"
    r"[$€£¥]\s*[+-]?\d[\d,]*(?:\.\d+)?s?"
    r"|[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?s?"
    r"|[+-]?\d+\.\d+s?"
    r"|[+-]?\d[\d,]*(?:\.\d+)?\s*(?:/oz|per ounce|oz)"
    r")",
    re.IGNORECASE,
)
_OBSERVED_MARKET_RE = re.compile(
    r"\b(?:fix|fixing|close|closed|settle|settled|traded|trading|spot|"
    r"recorded|printed|last price|market price|day low|day high|open|"
    r"previous close|5-day change|52-week high|52-week low)\b",
    re.IGNORECASE,
)
_FORWARD_LOOKING_RE = re.compile(
    r"\b(?:forecast|target|expects?|expected|sees?|projects?|projection|estimate|"
    r"outlook|scheduled|schedule|deadline|expires?|will|may|could|"
    r"begin|begins|start|starts|launch|launches|retiring|retire|"
    r"resigning|resign|joining|join|year-end|q[1-4])\b",
    re.IGNORECASE,
)
_SCHEDULE_CATALYST_RE = re.compile(
    r"(?:"
    r"\b(?:due|scheduled|schedule|expects?|expected|upcoming|set|slated)\b"
    r"(?:[^.]{0,40})"
    r"\b(?:report|reported|release|released|earnings|results|call|webcast)\b"
    r"|"
    r"\b(?:earnings|results|call|webcast)\b"
    r"(?:[^.]{0,40})"
    r"\b(?:due|scheduled|expected|upcoming|set|slated)\b"
    r")",
    re.IGNORECASE,
)
_OBSERVED_SOURCE_RE = re.compile(
    r"(?:gold-price-today|/fix\b|kitco\.com/fix|historical[-_/ ]price|price[-_/ ]history)",
    re.IGNORECASE,
)
_HARD_OBSERVED_EVENT_RE = re.compile(
    r"\b(?:after|by|through|continued|ended|reached|hit|fell|rose|surged|"
    r"dropped|closed|settled|reported|confirmed|released|published|"
    r"formed|broke|breached|reversed|rallied|retreated|pulled back|"
    r"beat|beats|beating|missed|misses|missing)\b",
    re.IGNORECASE,
)
_SOFT_OBSERVED_EVENT_RE = re.compile(r"\bannounced\b", re.IGNORECASE)

type FutureDateClassification = Literal[
    "future_schedule_or_forecast",
    "future_observed_fact",
    "current_or_price_history_fact",
    "ambiguous",
]
@dataclass(frozen=True, slots=True)
class HistoricalWebSearchFutureLeakage:
    """High-confidence future observed fact embedded in historical web-search text."""

    reason_code: str
    matched_date: date
    matched_snippet: str

    @property
    def rejection_reason(self) -> str:
        return (
            f"{self.reason_code}: matched_date={self.matched_date.isoformat()} "
            f"matched_snippet={self.matched_snippet!r}"
        )


def detect_historical_web_search_future_leakage(
    *,
    source_ref: str,
    title: str,
    content: str,
    visible_at: datetime,
) -> HistoricalWebSearchFutureLeakage | None:
    """Return a finding when text contains future observed market facts.

    Forward-looking forecasts and schedules are allowed. The guard only catches
    explicit future dates that are tied to observed/current market-price language.
    """
    if not isinstance(visible_at, datetime):
        raise TypeError("visible_at must be a datetime.")
    if visible_at.tzinfo is None or visible_at.utcoffset() is None:
        raise ValueError("visible_at must be timezone-aware.")
    visible_date = visible_at.astimezone(UTC).date()
    searchable_text = "\n".join((title, content))
    source_context = f"{source_ref}\n{title}"

    for match, matched_date in _iter_explicit_dates(searchable_text, visible_date):
        if matched_date <= visible_date:
            continue
        snippet = _snippet(searchable_text, match.start(), match.end())
        date_context = _date_context(searchable_text, match.start(), match.end())
        classification = _classify_future_dated_text(
            snippet=snippet,
            date_context=date_context,
            source_context=source_context,
        )
        if classification in {
            "future_observed_fact",
            "current_or_price_history_fact",
        }:
            return HistoricalWebSearchFutureLeakage(
                reason_code=_REASON_CODE,
                matched_date=matched_date,
                matched_snippet=snippet,
            )
    return None


def _iter_explicit_dates(text: str, visible_date: date):
    for match in _ISO_DATE_RE.finditer(text):
        try:
            yield match, date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            continue
    for match in _MONTH_DAY_RE.finditer(text):
        month = _MONTHS[match.group("month").casefold().rstrip(".")]
        year_text = match.group("year")
        try:
            matched_date = date(
                int(year_text) if year_text is not None else visible_date.year,
                month,
                int(match.group("day")),
            )
        except ValueError:
            continue
        if year_text is None and (
            matched_date - visible_date > timedelta(days=_IMPLICIT_YEAR_MAX_FUTURE_DAYS)
        ):
            continue
        yield match, matched_date


def _classify_future_dated_text(
    *,
    snippet: str,
    date_context: str,
    source_context: str,
) -> FutureDateClassification:
    observed_source = _OBSERVED_SOURCE_RE.search(source_context) is not None
    has_price = _PRICE_RE.search(snippet) is not None
    has_current_page = _CURRENT_PAGE_RE.search(snippet) is not None
    has_observed_market = _OBSERVED_MARKET_RE.search(date_context) is not None
    has_hard_observed_event = _HARD_OBSERVED_EVENT_RE.search(date_context) is not None
    has_soft_observed_event = _SOFT_OBSERVED_EVENT_RE.search(date_context) is not None
    has_forward_looking = _FORWARD_LOOKING_RE.search(date_context) is not None
    has_schedule_catalyst = _SCHEDULE_CATALYST_RE.search(date_context) is not None

    if has_current_page or (has_price and has_observed_market):
        return "current_or_price_history_fact"
    if observed_source and (has_price or has_observed_market):
        return "current_or_price_history_fact"
    if has_schedule_catalyst:
        return "future_schedule_or_forecast"
    if has_hard_observed_event:
        return "future_observed_fact"
    if has_soft_observed_event:
        return (
            "future_schedule_or_forecast"
            if has_forward_looking
            else "future_observed_fact"
        )
    if has_forward_looking:
        return "future_schedule_or_forecast"
    return "ambiguous"


def _snippet(text: str, start: int, end: int) -> str:
    left = max(0, start - _SNIPPET_RADIUS)
    right = min(len(text), end + _SNIPPET_RADIUS)
    return " ".join(text[left:right].split())


def _date_context(text: str, start: int, end: int) -> str:
    left = max(0, start - _DATE_CONTEXT_RADIUS)
    right = min(len(text), end + _DATE_CONTEXT_RADIUS)
    return " ".join(text[left:right].split())


def _validate_timestamp(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


__all__ = [
    "HistoricalWebSearchFutureLeakage",
    "detect_historical_web_search_future_leakage",
]
