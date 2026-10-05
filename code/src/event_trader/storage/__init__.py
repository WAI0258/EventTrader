"""Storage boundaries for canonical file-backed workspace surfaces."""

from .layout import (
    CanonicalSurface,
    InitializedWorkspaceLayout,
    WorkspaceLayout,
    WorkspaceLayoutError,
    build_workspace_layout,
    initialize_workspace_layout,
    validate_workspace_layout,
)

__all__ = [
    "CanonicalSurface",
    "InitializedWorkspaceLayout",
    "WorkspaceLayout",
    "WorkspaceLayoutError",
    "build_workspace_layout",
    "initialize_workspace_layout",
    "validate_workspace_layout",
]
