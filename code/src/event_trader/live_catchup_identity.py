"""Shared catch-up identity helpers for workspace-local live replay chains."""

from __future__ import annotations

from datetime import UTC, datetime


def live_catchup_chain_run_key(*, target_key: str) -> str:
    """Return the canonical checkpoint chain key for one workspace-local target."""
    return f"live_catchup:{target_key}"


def live_catchup_replay_run_id(*, target_key: str, attempt_at: datetime) -> str:
    """Return one replay attempt id under the canonical catch-up chain."""
    return (
        f"{live_catchup_run_id_prefix(target_key=target_key)}"
        f"attempt-{_timestamp_token(attempt_at)}"
    )


def live_catchup_run_id_prefix(*, target_key: str) -> str:
    """Return the stable replay artifact family prefix for one target."""
    return f"live-catchup-{target_key}-"


def matches_live_catchup_chain_run_key(
    *,
    candidate_run_key: str,
    chain_run_key: str,
) -> bool:
    """Match the canonical chain key plus legacy suffix-scoped variants."""
    return candidate_run_key == chain_run_key or candidate_run_key.startswith(
        chain_run_key + ":"
    )


def _timestamp_token(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
