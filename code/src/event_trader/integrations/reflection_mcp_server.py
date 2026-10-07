"""MCP transport adapter for the project-owned Reflection read tools."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.reflection.tools import (
    DEFAULT_REFLECTION_PAGE_SIZE,
    ReflectionToolGateway,
)
from event_trader.storage import build_workspace_layout

_WORKSPACE_ROOT_ENV = "EVENT_TRADER_WORKSPACE_ROOT"
_REFLECTION_TARGET_KEY_ENV = "EVENT_TRADER_REFLECTION_TARGET_KEY"
_REFLECTION_WINDOW_START_ENV = "EVENT_TRADER_REFLECTION_WINDOW_START_AT"
_REFLECTION_WINDOW_END_ENV = "EVENT_TRADER_REFLECTION_WINDOW_END_AT"


def _load_workspace_root() -> Path:
    raw_value = os.environ.get(_WORKSPACE_ROOT_ENV)
    if raw_value is None or not raw_value.strip():
        raise RuntimeError(
            f"{_WORKSPACE_ROOT_ENV} is required for event-trader reflection MCP server."
        )
    return Path(raw_value).expanduser().resolve(strict=False)


def _load_optional_target_key() -> str | None:
    raw_value = os.environ.get(_REFLECTION_TARGET_KEY_ENV)
    if raw_value is None:
        return None
    normalized = raw_value.strip()
    if not normalized:
        return None
    return validate_target_key(normalized, error_type=RuntimeError)


def _parse_timestamp_text(value: str, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{field_name} must be an ISO8601 datetime string.")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise RuntimeError(f"{field_name} must be a valid ISO8601 datetime.") from exc
    return validate_timestamp(parsed, field_name=field_name, error_type=RuntimeError)


def _load_optional_timestamp_env(name: str) -> datetime | None:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return None
    return _parse_timestamp_text(raw_value, field_name=name)


def _load_fixed_window() -> tuple[datetime, datetime] | None:
    start_at = _load_optional_timestamp_env(_REFLECTION_WINDOW_START_ENV)
    end_at = _load_optional_timestamp_env(_REFLECTION_WINDOW_END_ENV)
    if start_at is None and end_at is None:
        return None
    if start_at is None or end_at is None:
        raise RuntimeError(
            f"{_REFLECTION_WINDOW_START_ENV} and {_REFLECTION_WINDOW_END_ENV} "
            "must both be set when either is configured."
        )
    if end_at <= start_at:
        raise RuntimeError(
            f"{_REFLECTION_WINDOW_END_ENV} must be later than {_REFLECTION_WINDOW_START_ENV}."
        )
    return start_at, end_at


_gateway = ReflectionToolGateway(
    layout=build_workspace_layout(_load_workspace_root()),
    configured_target_key=_load_optional_target_key(),
    fixed_window=_load_fixed_window(),
)
mcp = FastMCP("event_trader_reflection")


@mcp.tool()
def list_evidence_window(
    start_at: str | None = None,
    end_at: str | None = None,
    target_keys: list[str] | None = None,
    exclude_event_ids: list[str] | None = None,
    limit: int = DEFAULT_REFLECTION_PAGE_SIZE,
    offset: int = 0,
) -> str:
    """List admitted evidence inside a bounded time window."""
    return _gateway.list_evidence_window(
        start_at=start_at,
        end_at=end_at,
        target_keys=target_keys,
        exclude_event_ids=exclude_event_ids,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def read_evidence(event_ids: list[str]) -> str:
    """Read admitted evidence records by canonical event id list."""
    return _gateway.read_evidence(event_ids)


@mcp.tool()
def list_state_changes(
    target_key: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int = DEFAULT_REFLECTION_PAGE_SIZE,
    offset: int = 0,
) -> str:
    """List canonical state changes for one target, optionally inside a window."""
    return _gateway.list_state_changes(
        target_key=target_key,
        start_at=start_at,
        end_at=end_at,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def list_decision_episodes(
    target_key: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    status: str | None = None,
    limit: int = DEFAULT_REFLECTION_PAGE_SIZE,
    offset: int = 0,
) -> str:
    """List projected decision episodes for one target, optionally filtered."""
    return _gateway.list_decision_episodes(
        target_key=target_key,
        start_at=start_at,
        end_at=end_at,
        status=status,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def read_decision_episode(episode_id: str, target_key: str | None = None) -> str:
    """Read one projected decision episode and its bounded audit records."""
    return _gateway.read_decision_episode(episode_id, target_key=target_key)


@mcp.tool()
def read_target_page(page_path: str) -> str:
    """Read one canonical target page by page_path."""
    return _gateway.read_target_page(page_path)


@mcp.tool()
def read_target_section(page_path: str, section_name: str) -> str:
    """Read one named markdown section from a canonical target page."""
    return _gateway.read_target_section(page_path, section_name)


@mcp.tool()
def search_target_memory(
    query: str,
    target_key: str | None = None,
    limit: int = DEFAULT_REFLECTION_PAGE_SIZE,
    offset: int = 0,
) -> str:
    """Search target-scoped research-memory pages and return excerpt snippets."""
    return _gateway.search_target_memory(
        query,
        target_key=target_key,
        limit=limit,
        offset=offset,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
