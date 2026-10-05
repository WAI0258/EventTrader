"""Project-owned MCP server for OpenAI-compatible native web-search retrieval."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP

API_KEY_ENV = "EVENT_TRADER_OPENAI_WEB_SEARCH_API_KEY"
BASE_URL_ENV = "EVENT_TRADER_OPENAI_WEB_SEARCH_BASE_URL"
MODEL_ENV = "EVENT_TRADER_OPENAI_WEB_SEARCH_MODEL"
TOOL_TYPE_ENV = "EVENT_TRADER_OPENAI_WEB_SEARCH_TOOL_TYPE"
DEFAULT_TOOL_TYPE = "web_search"


@dataclass(frozen=True, slots=True)
class WebSearchCandidate:
    source_ref: str
    title: str
    content: str
    published_at: None
    labels: tuple[str, ...]


def _load_required_text_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required for OpenAI web-search MCP server.")
    return value.strip()


def _load_tool_type() -> str:
    value = os.environ.get(TOOL_TYPE_ENV, DEFAULT_TOOL_TYPE).strip()
    if value not in {"web_search", "web_search_preview"}:
        raise RuntimeError(
            f"{TOOL_TYPE_ENV} must be web_search or web_search_preview."
        )
    return value


_api_key = _load_required_text_env(API_KEY_ENV)
_base_url = _load_required_text_env(BASE_URL_ENV)
_model = _load_required_text_env(MODEL_ENV)
_tool_type = _load_tool_type()
mcp = FastMCP("event_trader_openai_web_search")


@mcp.tool()
def openai_web_search(query: str, as_of_cutoff: str | None = None) -> str:
    """Run native web-search and return source-backed citation candidates."""
    normalized_query = _require_non_blank_text(query, field_name="query")
    normalized_cutoff = None
    if as_of_cutoff is not None:
        normalized_cutoff = _require_non_blank_text(
            as_of_cutoff,
            field_name="as_of_cutoff",
        )
    response_payload = _request_responses_web_search(
        query=normalized_query,
        as_of_cutoff=normalized_cutoff,
    )
    text = _extract_output_text(response_payload)
    candidates = _extract_candidates(response_payload=response_payload, text=text)
    if not candidates:
        return _json_text(
            {
                "success": False,
                "error_code": "native_web_search_no_cited_source",
                "error": "OpenAI web-search response did not contain cited source URLs.",
                "raw_text": text,
            }
        )
    return _json_text(
        {
            "success": True,
            "raw_text": text,
            "candidates": [
                {
                    "source_ref": candidate.source_ref,
                    "title": candidate.title,
                    "content": candidate.content,
                    "published_at": candidate.published_at,
                    "labels": list(candidate.labels),
                }
                for candidate in candidates
            ],
        }
    )


def _request_responses_web_search(
    *,
    query: str,
    as_of_cutoff: str | None,
) -> dict[str, Any]:
    payload = {
        "model": _model,
        "tools": [{"type": _tool_type}],
        "tool_choice": "auto",
        "include": ["web_search_call.action.sources"],
        "input": _build_input(query=query, as_of_cutoff=as_of_cutoff),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _responses_url(_base_url),
        data=body,
        headers={
            "Authorization": f"Bearer {_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            "OpenAI web-search request failed: "
            f"http_status={exc.code} body={error_text[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI web-search request failed: {exc}") from exc
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenAI web-search response was not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenAI web-search response must be a JSON object.")
    return parsed


def _build_input(*, query: str, as_of_cutoff: str | None) -> str:
    cutoff_text = ""
    if as_of_cutoff is not None:
        cutoff_text = (
            f"\nAs-of cutoff: {as_of_cutoff}. Only use sources that were visible "
            "at or before this cutoff. Do not use later retrospective coverage."
        )
    return (
        "Find source-backed market material for the following trading-research "
        "query. Return concise factual notes with citations. Do not judge "
        "importance, tradability, thesis impact, or position impact."
        f"{cutoff_text}\nQuery: {query}"
    )


def _extract_output_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    texts: list[str] = []
    for item in _walk_objects(payload):
        if item.get("type") == "output_text" and isinstance(item.get("text"), str):
            text = item["text"].strip()
            if text:
                texts.append(text)
    return "\n\n".join(texts)


def _extract_candidates(
    *,
    response_payload: dict[str, Any],
    text: str,
) -> tuple[WebSearchCandidate, ...]:
    citations = _extract_url_citations(response_payload)
    candidates: list[WebSearchCandidate] = []
    seen_urls: set[str] = set()
    content = text.strip()
    if not content:
        return ()
    for citation in citations:
        url = citation["url"].strip()
        if url in seen_urls:
            continue
        seen_urls.add(url)
        title = citation["title"].strip() or url
        candidates.append(
            WebSearchCandidate(
                source_ref=url,
                title=title,
                content=content,
                published_at=None,
                labels=(),
            )
        )
    return tuple(candidates)


def _extract_url_citations(payload: dict[str, Any]) -> tuple[dict[str, str], ...]:
    citations: list[dict[str, str]] = []
    for item in _walk_objects(payload):
        if item.get("type") != "url_citation":
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        title = item.get("title")
        citations.append(
            {
                "url": url.strip(),
                "title": title.strip() if isinstance(title, str) else url.strip(),
            }
        )
    return tuple(citations)


def _walk_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_objects(child)


def _responses_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    return f"{normalized}/responses"


def _require_non_blank_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise RuntimeError(f"{field_name} must not be blank.")
    return normalized


def _json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
