"""Deterministic exact published-at resolution for web source pages."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from event_trader.contracts._validators import validate_source_ref, validate_timestamp

_REQUEST_TIMEOUT_SECONDS = 20.0
_REQUEST_USER_AGENT = (
    "Mozilla/5.0 (compatible; event-trader/0.1; +https://example.invalid/event-trader)"
)
_PUBLISHED_META_KEYS = (
    "article:published_time",
    "og:article:published_time",
    "datepublished",
    "datecreated",
    "pubdate",
    "publishdate",
    "parsely-pub-date",
)
_PUBLISHED_ITEMPROPS = frozenset(
    {
        "datepublished",
        "datecreated",
        "pubdate",
    }
)
_JSON_LD_PUBLISHED_KEYS = frozenset(
    {
        "datepublished",
        "datecreated",
    }
)


class WebSourcePublishedAtResolutionError(ValueError):
    """Raised when a web source page cannot yield an exact published timestamp."""


type WebPageFetcher = Callable[[str], str]
_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def resolve_web_source_published_at(
    *,
    source_ref: str,
    agent_published_at: datetime | None,
    fetch_html: WebPageFetcher | None = None,
) -> datetime:
    """Resolve one exact published timestamp from webpage metadata or agent output."""
    validated_source_ref = validate_source_ref(
        source_ref,
        error_type=WebSourcePublishedAtResolutionError,
    )
    parsed_source_ref = urlparse(validated_source_ref)
    if parsed_source_ref.scheme not in {"http", "https"}:
        raise WebSourcePublishedAtResolutionError(
            "web source published-at resolution requires an http(s) source_ref."
        )

    validated_agent_published_at = None
    if agent_published_at is not None:
        validated_agent_published_at = validate_timestamp(
            agent_published_at,
            field_name="agent_published_at",
            error_type=WebSourcePublishedAtResolutionError,
        )

    extracted_published_at = extract_exact_web_source_published_at(
        source_ref=validated_source_ref,
        fetch_html=fetch_html,
    )
    if extracted_published_at is not None:
        if (
            validated_agent_published_at is not None
            and validated_agent_published_at != extracted_published_at
        ):
            raise WebSourcePublishedAtResolutionError(
                "agent published_at does not match the deterministically extracted "
                "webpage published_at."
            )
        return extracted_published_at

    if validated_agent_published_at is None:
        raise WebSourcePublishedAtResolutionError(
            "could not determine an exact published_at from webpage metadata, and the "
            "agent did not provide an exact published_at either."
        )
    return validated_agent_published_at


def extract_exact_web_source_published_at(
    *,
    source_ref: str,
    fetch_html: WebPageFetcher | None = None,
) -> datetime | None:
    """Extract one exact published timestamp from page metadata, or return None."""
    validated_source_ref = validate_source_ref(
        source_ref,
        error_type=WebSourcePublishedAtResolutionError,
    )
    parsed_source_ref = urlparse(validated_source_ref)
    if parsed_source_ref.scheme not in {"http", "https"}:
        raise WebSourcePublishedAtResolutionError(
            "web source published-at extraction requires an http(s) source_ref."
        )

    html_fetcher = _fetch_web_page_html if fetch_html is None else fetch_html
    if not callable(html_fetcher):
        raise WebSourcePublishedAtResolutionError("fetch_html must be callable.")

    try:
        html = html_fetcher(validated_source_ref)
    except WebSourcePublishedAtResolutionError:
        raise
    except Exception as exc:
        raise WebSourcePublishedAtResolutionError(
            f"failed to fetch webpage HTML for published-at extraction: {exc}"
        ) from exc
    if not isinstance(html, str):
        raise WebSourcePublishedAtResolutionError("fetch_html must return a string HTML body.")

    parser = _PublishedAtHtmlParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise WebSourcePublishedAtResolutionError(
            f"failed to parse webpage HTML for published-at extraction: {exc}"
        ) from exc

    try:
        exact_candidates = tuple(_iter_exact_candidates(parser))
    except WebSourcePublishedAtResolutionError:
        raise
    except Exception as exc:
        raise WebSourcePublishedAtResolutionError(
            f"failed to inspect webpage metadata for published-at extraction: {exc}"
        ) from exc
    if not exact_candidates:
        return None
    if len(exact_candidates) > 1:
        unique_candidates = tuple(dict.fromkeys(exact_candidates))
        if len(unique_candidates) > 1:
            raise WebSourcePublishedAtResolutionError(
                "webpage metadata exposed conflicting exact published_at timestamps."
            )
        return unique_candidates[0]
    return exact_candidates[0]


class _PublishedAtHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_candidates: list[tuple[str, str]] = []
        self.time_candidates: list[str] = []
        self._json_ld_chunks: list[str] = []
        self._inside_json_ld = False
        self._current_json_ld_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {
            key.lower(): value
            for key, value in attrs
            if isinstance(key, str) and value is not None
        }
        if tag == "meta":
            key = _first_non_blank(attr_map.get("property"), attr_map.get("name"))
            value = _first_non_blank(attr_map.get("content"))
            if key is not None and value is not None:
                self.meta_candidates.append((key.lower(), value))
            itemprop = _first_non_blank(attr_map.get("itemprop"))
            if itemprop is not None and itemprop.lower() in _PUBLISHED_ITEMPROPS:
                itemprop_value = _first_non_blank(
                    attr_map.get("content"),
                    attr_map.get("datetime"),
                )
                if itemprop_value is not None:
                    self.meta_candidates.append((itemprop.lower(), itemprop_value))
        elif tag == "time":
            datetime_value = _first_non_blank(attr_map.get("datetime"))
            itemprop = _first_non_blank(attr_map.get("itemprop"))
            if datetime_value is not None and (
                itemprop is None
                or itemprop.lower() in _PUBLISHED_ITEMPROPS
                or "pubdate" in attr_map
            ):
                self.time_candidates.append(datetime_value)
        elif tag == "script":
            script_type = _first_non_blank(attr_map.get("type"))
            if script_type is not None and script_type.lower() == "application/ld+json":
                self._inside_json_ld = True
                self._current_json_ld_chunks = []

    def handle_data(self, data: str) -> None:
        if self._inside_json_ld:
            self._current_json_ld_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._inside_json_ld:
            self._inside_json_ld = False
            payload = "".join(self._current_json_ld_chunks).strip()
            if payload:
                self._json_ld_chunks.append(payload)
            self._current_json_ld_chunks = []

    @property
    def json_ld_chunks(self) -> tuple[str, ...]:
        return tuple(self._json_ld_chunks)


def _iter_exact_candidates(parser: _PublishedAtHtmlParser) -> Iterable[datetime]:
    for key, raw_value in parser.meta_candidates:
        if key in _PUBLISHED_META_KEYS or key in _PUBLISHED_ITEMPROPS:
            parsed = _parse_exact_timestamp(raw_value)
            if parsed is not None:
                yield parsed
    for raw_value in parser.time_candidates:
        parsed = _parse_exact_timestamp(raw_value)
        if parsed is not None:
            yield parsed
    for payload in parser.json_ld_chunks:
        yield from _iter_json_ld_exact_candidates(payload)


def _iter_json_ld_exact_candidates(payload: str) -> Iterable[datetime]:
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return ()
    return tuple(_walk_json_ld_exact_candidates(decoded))


def _walk_json_ld_exact_candidates(value: object) -> Iterable[datetime]:
    if isinstance(value, dict):
        for key, nested_value in value.items():
            if isinstance(key, str) and key.lower() in _JSON_LD_PUBLISHED_KEYS:
                if isinstance(nested_value, str):
                    parsed = _parse_exact_timestamp(nested_value)
                    if parsed is not None:
                        yield parsed
            yield from _walk_json_ld_exact_candidates(nested_value)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json_ld_exact_candidates(item)


def _parse_exact_timestamp(raw_value: str) -> datetime | None:
    normalized = raw_value.strip()
    if not normalized:
        return None
    if len(normalized) <= 10:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return validate_timestamp(
        parsed,
        field_name="published_at",
        error_type=WebSourcePublishedAtResolutionError,
    )


def _fetch_web_page_html(source_ref: str) -> str:
    request = Request(
        source_ref,
        headers={
            "User-Agent": _REQUEST_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get_content_type().lower()
            if content_type not in _HTML_CONTENT_TYPES:
                raise WebSourcePublishedAtResolutionError(
                    "web source published-at extraction requires HTML content; "
                    f"got content_type={content_type}."
                )
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (HTTPError, URLError, OSError) as exc:
        raise WebSourcePublishedAtResolutionError(
            f"failed to fetch source_ref {source_ref!r}: {exc}"
        ) from exc


def _first_non_blank(*values: str | None) -> str | None:
    for value in values:
        if value is None:
            continue
        normalized = value.strip()
        if normalized:
            return normalized
    return None


__all__ = [
    "WebPageFetcher",
    "WebSourcePublishedAtResolutionError",
    "extract_exact_web_source_published_at",
    "resolve_web_source_published_at",
]
