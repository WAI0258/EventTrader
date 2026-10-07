"""Provider-neutral agent contract for reflection inference attempts."""

from __future__ import annotations

from typing import Literal

type ReflectionTaskKind = Literal[
    "shared",
    "target",
    "open_position",
    "target_close",
]

REFLECTION_AGENT_ROLE = "reflection"
REFLECTION_TOOL_SERVER_NAME = "event_trader_reflection"
REQUIRED_REFLECTION_TOOL_NAMES: tuple[str, ...] = (
    "list_evidence_window",
    "read_evidence",
    "list_state_changes",
    "list_decision_episodes",
    "read_decision_episode",
    "read_target_page",
    "read_target_section",
    "search_target_memory",
)


__all__ = [
    "REFLECTION_AGENT_ROLE",
    "REFLECTION_TOOL_SERVER_NAME",
    "ReflectionTaskKind",
    "REQUIRED_REFLECTION_TOOL_NAMES",
]
