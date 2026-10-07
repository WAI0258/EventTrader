"""Environment codec for the Analysis MCP transport boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from event_trader.config import load_kernel_config
from event_trader.context_assembly import (
    parse_context_packet_payload,
    serialize_context_packet,
)
from event_trader.contracts.execution_direction_policy import (
    validate_execution_direction_mode,
)
from event_trader.integrations.analysis_citations import (
    AnalysisCitation,
    load_analysis_citations,
    serialize_analysis_citations,
)
from event_trader.operator_context import OperatorContextSnapshot
from event_trader.reasoning.analysis_tools import AnalysisToolContext
from event_trader.storage import build_workspace_layout

_WORKSPACE_ROOT_ENV = "EVENT_TRADER_WORKSPACE_ROOT"
_ANALYSIS_STAGE_ROOT_ENV = "EVENT_TRADER_ANALYSIS_STAGE_ROOT"
_ANALYSIS_RECEIPT_PATH_ENV = "EVENT_TRADER_ANALYSIS_RECEIPT_PATH"
_ANALYSIS_CITATIONS_ENV = "EVENT_TRADER_ANALYSIS_CITATIONS"
_ANALYSIS_CONTEXT_CITATIONS_ENV = "EVENT_TRADER_ANALYSIS_CONTEXT_CITATIONS"
_ANALYSIS_TARGET_KEY_ENV = "EVENT_TRADER_ANALYSIS_TARGET_KEY"
_ANALYSIS_BUSINESS_AT_ENV = "EVENT_TRADER_ANALYSIS_BUSINESS_AT"
_ANALYSIS_RUNTIME_SCOPE_ENV = "EVENT_TRADER_ANALYSIS_RUNTIME_SCOPE"
_ANALYSIS_EXECUTION_DIRECTION_MODE_ENV = "EVENT_TRADER_ANALYSIS_EXECUTION_DIRECTION_MODE"
_CONFIG_PATH_ENV = "EVENT_TRADER_CONFIG_PATH"
_MARKET_DATA_STORE_ROOT_ENV = "EVENT_TRADER_MARKET_DATA_STORE_ROOT"
_MARKET_DATA_SNAPSHOT_ID_ENV = "EVENT_TRADER_MARKET_DATA_SNAPSHOT_ID"
_ANALYSIS_CONTEXT_PACKET_ENV = "EVENT_TRADER_ANALYSIS_CONTEXT_PACKET"
_ANALYSIS_INCLUDED_LESSON_IDS_ENV = "EVENT_TRADER_ANALYSIS_INCLUDED_LESSON_IDS"
_ANALYSIS_OPERATOR_CONTEXT_ENV = "EVENT_TRADER_ANALYSIS_OPERATOR_CONTEXT"
ANALYSIS_MCP_SERVER_NAME = "event_trader_analysis"
ANALYSIS_MCP_SERVER_MODULE = "event_trader.integrations.analysis_mcp_server"


def _required_path_env(env: Mapping[str, str], name: str) -> Path:
    raw_value = env.get(name)
    if raw_value is None or not raw_value.strip():
        raise RuntimeError(f"{name} is required for event-trader analysis tools.")
    return Path(raw_value).expanduser().resolve(strict=False)


def _required_text_env(env: Mapping[str, str], name: str) -> str:
    raw_value = env.get(name)
    if raw_value is None or not raw_value.strip():
        raise RuntimeError(f"{name} is required for event-trader analysis tools.")
    return raw_value.strip()


def _optional_path_env(env: Mapping[str, str], name: str) -> Path | None:
    raw_value = env.get(name)
    if raw_value is None or not raw_value.strip():
        return None
    return Path(raw_value).expanduser().resolve(strict=False)


def _optional_business_at(env: Mapping[str, str]) -> datetime | None:
    raw_value = env.get(_ANALYSIS_BUSINESS_AT_ENV)
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return datetime.fromisoformat(raw_value.strip())
    except ValueError as exc:
        raise RuntimeError(f"{_ANALYSIS_BUSINESS_AT_ENV} must be an ISO-8601 datetime.") from exc


def _runtime_scope(env: Mapping[str, str]) -> Literal["live", "replay"]:
    raw_value = env.get(_ANALYSIS_RUNTIME_SCOPE_ENV, "live").strip()
    if raw_value not in {"live", "replay"}:
        raise RuntimeError(f"{_ANALYSIS_RUNTIME_SCOPE_ENV} must be 'live' or 'replay'.")
    return cast(Literal["live", "replay"], raw_value)


def _included_lesson_ids(env: Mapping[str, str]) -> tuple[str, ...]:
    raw_value = env.get(_ANALYSIS_INCLUDED_LESSON_IDS_ENV)
    if raw_value is None or not raw_value.strip():
        return ()
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{_ANALYSIS_INCLUDED_LESSON_IDS_ENV} must be a JSON array of strings."
        ) from exc
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise RuntimeError(f"{_ANALYSIS_INCLUDED_LESSON_IDS_ENV} must be a JSON array of strings.")
    return tuple(payload)


def _optional_citations_env(
    env: Mapping[str, str],
    name: str,
) -> tuple[AnalysisCitation, ...]:
    raw_value = env.get(name)
    if raw_value is None or not raw_value.strip():
        return ()
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return load_analysis_citations(raw_value)
    if payload == []:
        return ()
    return load_analysis_citations(raw_value)


def _operator_context_from_env(env: Mapping[str, str]) -> OperatorContextSnapshot:
    raw_value = _required_text_env(env, _ANALYSIS_OPERATOR_CONTEXT_ENV)
    try:
        payload = json.loads(raw_value)
        return OperatorContextSnapshot(
            target_key=payload["target_key"],
            page_path=payload["page_path"],
            content_md=payload["content_md"],
            content_sha256=payload["content_sha256"],
            read_at=datetime.fromisoformat(payload["read_at"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{_ANALYSIS_OPERATOR_CONTEXT_ENV} is invalid.") from exc


def analysis_tool_context_from_env(env: Mapping[str, str]) -> AnalysisToolContext:
    """Decode one MCP child-process environment into a project tool context."""
    config_path = _optional_path_env(env, _CONFIG_PATH_ENV)
    raw_context_packet = env.get(_ANALYSIS_CONTEXT_PACKET_ENV)
    return AnalysisToolContext(
        canonical_layout=build_workspace_layout(_required_path_env(env, _WORKSPACE_ROOT_ENV)),
        stage_layout=build_workspace_layout(_required_path_env(env, _ANALYSIS_STAGE_ROOT_ENV)),
        receipt_path=_required_path_env(env, _ANALYSIS_RECEIPT_PATH_ENV),
        active_citations=load_analysis_citations(_required_text_env(env, _ANALYSIS_CITATIONS_ENV)),
        context_citations=_optional_citations_env(
            env,
            _ANALYSIS_CONTEXT_CITATIONS_ENV,
        ),
        target_key=_required_text_env(env, _ANALYSIS_TARGET_KEY_ENV),
        business_at=_optional_business_at(env),
        runtime_scope=_runtime_scope(env),
        execution_direction_mode=validate_execution_direction_mode(
            env.get(
                _ANALYSIS_EXECUTION_DIRECTION_MODE_ENV,
                "long_short",
            ).strip(),
            field_name=_ANALYSIS_EXECUTION_DIRECTION_MODE_ENV,
            error_type=RuntimeError,
        ),
        market_config=(None if config_path is None else load_kernel_config(config_path)),
        market_data_store_root=_optional_path_env(env, _MARKET_DATA_STORE_ROOT_ENV),
        market_data_snapshot_id=(
            env.get(_MARKET_DATA_SNAPSHOT_ID_ENV, "").strip() or None
        ),
        context_packet=(
            None
            if raw_context_packet is None or not raw_context_packet.strip()
            else parse_context_packet_payload(raw_context_packet)
        ),
        included_lesson_ids=_included_lesson_ids(env),
        operator_context=_operator_context_from_env(env),
    )


def analysis_tool_context_to_env(
    context: AnalysisToolContext,
    *,
    config_path: Path | None,
    pythonpath: str | None = None,
) -> dict[str, str]:
    """Encode a project tool context for the legacy stdio MCP transport."""
    env = {
        _WORKSPACE_ROOT_ENV: str(context.canonical_layout.root),
        _ANALYSIS_STAGE_ROOT_ENV: str(context.stage_layout.root),
        _ANALYSIS_RECEIPT_PATH_ENV: str(context.receipt_path),
        _ANALYSIS_CITATIONS_ENV: serialize_analysis_citations(context.active_citations),
        _ANALYSIS_CONTEXT_CITATIONS_ENV: json.dumps(
            [
                {"event_id": citation.event_id, "source_ref": citation.source_ref}
                for citation in context.context_citations
            ],
            ensure_ascii=False,
        ),
        _ANALYSIS_TARGET_KEY_ENV: context.target_key,
        _ANALYSIS_RUNTIME_SCOPE_ENV: context.runtime_scope,
        _ANALYSIS_EXECUTION_DIRECTION_MODE_ENV: context.execution_direction_mode,
        _ANALYSIS_INCLUDED_LESSON_IDS_ENV: json.dumps(
            list(context.included_lesson_ids),
            ensure_ascii=False,
        ),
        _ANALYSIS_OPERATOR_CONTEXT_ENV: json.dumps(
            {
                "target_key": context.operator_context.target_key,
                "page_path": context.operator_context.page_path,
                "content_md": context.operator_context.content_md,
                "content_sha256": context.operator_context.content_sha256,
                "read_at": context.operator_context.read_at.isoformat(),
            },
            ensure_ascii=False,
        ),
    }
    if context.business_at is not None:
        env[_ANALYSIS_BUSINESS_AT_ENV] = context.business_at.isoformat()
    if context.market_data_store_root is not None:
        env[_MARKET_DATA_STORE_ROOT_ENV] = str(context.market_data_store_root)
    if context.market_data_snapshot_id is not None:
        env[_MARKET_DATA_SNAPSHOT_ID_ENV] = context.market_data_snapshot_id
    if context.context_packet is not None:
        env[_ANALYSIS_CONTEXT_PACKET_ENV] = serialize_context_packet(context.context_packet)
    if config_path is not None:
        env[_CONFIG_PATH_ENV] = str(config_path.expanduser().resolve(strict=False))
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    return env


__all__ = [
    "ANALYSIS_MCP_SERVER_MODULE",
    "ANALYSIS_MCP_SERVER_NAME",
    "analysis_tool_context_from_env",
    "analysis_tool_context_to_env",
]
