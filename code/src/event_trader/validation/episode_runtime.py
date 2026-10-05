"""Deterministic seam from persisted episode artifacts to episode state."""

from __future__ import annotations

from event_trader.storage import WorkspaceLayout

from .episode_artifacts import (
    EpisodeArtifactStoreError,
    read_closed_episode_artifact,
    read_target_episode_artifacts,
)
from .episode_builder import EpisodeBuildResult
from .episode_snapshot import ClosedEpisodeSnapshot


def load_target_episode_state(layout: WorkspaceLayout, target_key: str) -> EpisodeBuildResult:
    """Load one target's persisted episode artifacts as episode state."""
    if not isinstance(layout, WorkspaceLayout):
        raise EpisodeArtifactStoreError("layout must be a WorkspaceLayout instance.")
    return read_target_episode_artifacts(layout, target_key)


def load_closed_episode_snapshot(
    layout: WorkspaceLayout,
    target_key: str,
    episode_id: str,
) -> ClosedEpisodeSnapshot:
    """Load one closed episode snapshot from persisted episode artifacts."""
    if not isinstance(layout, WorkspaceLayout):
        raise EpisodeArtifactStoreError("layout must be a WorkspaceLayout instance.")
    return read_closed_episode_artifact(layout, target_key, episode_id)


__all__ = ["load_closed_episode_snapshot", "load_target_episode_state"]
