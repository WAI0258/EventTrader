"""Workspace bootstrap seam over the canonical storage layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from event_trader.storage import (
    WorkspaceLayout,
    WorkspaceLayoutError,
    initialize_workspace_layout,
)


class WorkspaceBootstrapError(RuntimeError):
    """Raised when the canonical workspace cannot be initialized."""


@dataclass(frozen=True, slots=True)
class BootstrapWorkspace:
    """Bootstrap result for callers that need the canonical workspace layout."""

    root: Path
    runtime_root: Path
    created_paths: tuple[Path, ...]
    layout: WorkspaceLayout | None = None



def bootstrap_workspace(workspace_root: str | Path) -> BootstrapWorkspace:
    """Create or validate the canonical workspace layout."""
    root = Path(workspace_root).expanduser().resolve(strict=False)

    try:
        initialized_layout = initialize_workspace_layout(root)
    except WorkspaceLayoutError as exc:
        raise WorkspaceBootstrapError(
            f"Could not initialize workspace root '{root}': {exc}"
        ) from exc

    layout = initialized_layout.layout
    return BootstrapWorkspace(
        root=layout.root,
        runtime_root=layout.runtime_root,
        created_paths=initialized_layout.created_paths,
        layout=layout,
    )
