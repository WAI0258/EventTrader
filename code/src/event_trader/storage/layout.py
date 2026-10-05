"""Canonical file-backed workspace layout for event-trader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SurfaceClassification = Literal["truth", "operational", "helper"]
SurfaceKind = Literal["directory", "markdown"]


class WorkspaceLayoutError(RuntimeError):
    """Raised when the canonical workspace layout cannot be validated or created."""


@dataclass(frozen=True, slots=True)
class CanonicalSurface:
    """A single canonical filesystem surface in the workspace layout."""

    relative_path: Path
    kind: SurfaceKind
    classification: SurfaceClassification
    default_content: str | None = None

    def absolute_path(self, root: Path) -> Path:
        """Resolve this surface under a workspace root."""
        return (root / self.relative_path).resolve(strict=False)


@dataclass(frozen=True, slots=True)
class WorkspaceLayout:
    """Resolved canonical workspace paths plus their explicit classifications."""

    root: Path
    research_memory_root: Path
    shared_root: Path
    shared_topics_root: Path
    shared_entities_root: Path
    shared_index_file: Path
    shared_log_file: Path
    targets_root: Path
    ledger_root: Path
    runtime_root: Path
    helpers_root: Path
    surfaces: tuple[CanonicalSurface, ...]

    @property
    def truth_surfaces(self) -> tuple[Path, ...]:
        """Return canonical truth surfaces only, excluding helper and runtime paths."""
        return tuple(
            surface.absolute_path(self.root)
            for surface in self.surfaces
            if surface.classification == "truth"
        )

    @property
    def truth_roots(self) -> tuple[Path, Path]:
        """Return top-level canonical truth roots for observability."""
        return (self.research_memory_root, self.ledger_root)

    @property
    def operational_roots(self) -> tuple[Path, ...]:
        """Return project-owned operational/runtime roots."""
        return (self.runtime_root,)

    @property
    def helper_roots(self) -> tuple[Path, ...]:
        """Return derived helper roots, never canonical truth surfaces."""
        return (self.helpers_root,)

    def format_observability_line(self) -> str:
        """Format the committed one-line workspace layout signal."""
        truth_roots = ",".join(str(path) for path in self.truth_roots)
        return (
            "workspace layout: "
            f"truth_roots={truth_roots} "
            f"operational_root={self.runtime_root} "
            f"helper_root={self.helpers_root}"
        )


@dataclass(frozen=True, slots=True)
class InitializedWorkspaceLayout:
    """A validated layout plus the paths created during initialization."""

    layout: WorkspaceLayout
    created_paths: tuple[Path, ...]


_CANONICAL_SURFACES: tuple[CanonicalSurface, ...] = (
    CanonicalSurface(Path("research_memory"), "directory", "truth"),
    CanonicalSurface(Path("research_memory/shared"), "directory", "truth"),
    CanonicalSurface(Path("research_memory/shared/topics"), "directory", "truth"),
    CanonicalSurface(Path("research_memory/shared/entities"), "directory", "truth"),
    CanonicalSurface(
        Path("research_memory/shared/index.md"),
        "markdown",
        "truth",
        default_content="# shared index\n",
    ),
    CanonicalSurface(
        Path("research_memory/shared/log.md"),
        "markdown",
        "truth",
        default_content="# shared log\n",
    ),
    CanonicalSurface(Path("research_memory/targets"), "directory", "truth"),
    CanonicalSurface(Path("ledger"), "directory", "truth"),
    CanonicalSurface(Path("runtime"), "directory", "operational"),
    CanonicalSurface(Path("helpers"), "directory", "helper"),
)


def build_workspace_layout(workspace_root: str | Path) -> WorkspaceLayout:
    """Resolve the canonical workspace layout from a configured root."""
    root = Path(workspace_root).expanduser().resolve(strict=False)
    return WorkspaceLayout(
        root=root,
        research_memory_root=(root / "research_memory").resolve(strict=False),
        shared_root=(root / "research_memory" / "shared").resolve(strict=False),
        shared_topics_root=(root / "research_memory" / "shared" / "topics").resolve(
            strict=False
        ),
        shared_entities_root=(
            root / "research_memory" / "shared" / "entities"
        ).resolve(strict=False),
        shared_index_file=(root / "research_memory" / "shared" / "index.md").resolve(
            strict=False
        ),
        shared_log_file=(root / "research_memory" / "shared" / "log.md").resolve(
            strict=False
        ),
        targets_root=(root / "research_memory" / "targets").resolve(strict=False),
        ledger_root=(root / "ledger").resolve(strict=False),
        runtime_root=(root / "runtime").resolve(strict=False),
        helpers_root=(root / "helpers").resolve(strict=False),
        surfaces=_CANONICAL_SURFACES,
    )



def initialize_workspace_layout(workspace_root: str | Path) -> InitializedWorkspaceLayout:
    """Validate or create the canonical workspace layout with rollback on failure."""
    layout = build_workspace_layout(workspace_root)
    root_exists = layout.root.exists()

    if root_exists and not layout.root.is_dir():
        raise WorkspaceLayoutError(
            f"Expected a directory during bootstrap, but found a file at: {layout.root}"
        )

    if root_exists:
        _reject_incomplete_precreated_layout(layout)

    created_paths: list[Path] = []

    try:
        if _ensure_directory_chain(layout.root, created_paths):
            pass

        for surface in layout.surfaces:
            absolute_path = surface.absolute_path(layout.root)
            if surface.kind == "directory":
                _ensure_directory(absolute_path, created_paths)
            else:
                _ensure_markdown_file(absolute_path, created_paths, surface.default_content)
    except WorkspaceLayoutError:
        _rollback_created_paths(created_paths)
        raise
    except OSError as exc:
        _rollback_created_paths(created_paths)
        raise WorkspaceLayoutError(
            f"Failed to initialize canonical workspace layout under {layout.root}: {exc}"
        ) from exc

    return InitializedWorkspaceLayout(layout=layout, created_paths=tuple(created_paths))



def validate_workspace_layout(layout: WorkspaceLayout) -> None:
    """Validate that an existing workspace already satisfies the canonical layout."""
    if not layout.root.exists() or not layout.root.is_dir():
        raise WorkspaceLayoutError(
            f"Expected a directory during bootstrap, but found a file at: {layout.root}"
            if layout.root.exists()
            else f"Workspace root does not exist: {layout.root}"
        )

    for surface in layout.surfaces:
        absolute_path = surface.absolute_path(layout.root)
        if surface.kind == "directory":
            if not absolute_path.exists():
                raise WorkspaceLayoutError(
                    f"Incomplete canonical workspace layout; missing required path: {absolute_path}"
                )
            if not absolute_path.is_dir():
                raise WorkspaceLayoutError(
                    f"Expected a directory during bootstrap, but found a file at: {absolute_path}"
                )
        else:
            if not absolute_path.exists():
                raise WorkspaceLayoutError(
                    f"Incomplete canonical workspace layout; missing required path: {absolute_path}"
                )
            if not absolute_path.is_file():
                raise WorkspaceLayoutError(
                    "Expected a markdown file during bootstrap, "
                    f"but found a directory at: {absolute_path}"
                )



def _reject_incomplete_precreated_layout(layout: WorkspaceLayout) -> None:
    existing_classifications: set[SurfaceClassification] = set()

    for surface in layout.surfaces:
        absolute_path = surface.absolute_path(layout.root)
        if not absolute_path.exists():
            continue

        if surface.kind == "directory" and not absolute_path.is_dir():
            raise WorkspaceLayoutError(
                f"Expected a directory during bootstrap, but found a file at: {absolute_path}"
            )
        if surface.kind == "markdown" and not absolute_path.is_file():
            raise WorkspaceLayoutError(
                "Expected a markdown file during bootstrap, "
                f"but found a directory at: {absolute_path}"
            )

        existing_classifications.add(surface.classification)

    if not existing_classifications:
        return

    if existing_classifications <= {"operational", "helper"}:
        return

    for surface in layout.surfaces:
        absolute_path = surface.absolute_path(layout.root)
        if not absolute_path.exists():
            raise WorkspaceLayoutError(
                f"Incomplete canonical workspace layout; missing required path: {absolute_path}"
            )



def _ensure_directory_chain(path: Path, created_paths: list[Path]) -> bool:
    if path.exists():
        if not path.is_dir():
            raise WorkspaceLayoutError(
                f"Expected a directory during bootstrap, but found a file at: {path}"
            )
        return False

    missing_paths: list[Path] = []
    current = path
    while not current.exists():
        missing_paths.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    for missing_path in reversed(missing_paths):
        missing_path.mkdir()
        created_paths.append(missing_path.resolve(strict=False))

    return True



def _ensure_directory(path: Path, created_paths: list[Path]) -> bool:
    if path.exists():
        if not path.is_dir():
            raise WorkspaceLayoutError(
                f"Expected a directory during bootstrap, but found a file at: {path}"
            )
        return False

    path.mkdir()
    created_paths.append(path.resolve(strict=False))
    return True



def _ensure_markdown_file(
    path: Path,
    created_paths: list[Path],
    default_content: str | None,
) -> bool:
    if path.exists():
        if not path.is_file():
            raise WorkspaceLayoutError(
                f"Expected a markdown file during bootstrap, but found a directory at: {path}"
            )
        return False

    path.write_text(default_content or "", encoding="utf-8")
    created_paths.append(path.resolve(strict=False))
    return True



def _rollback_created_paths(created_paths: list[Path]) -> None:
    for path in reversed(created_paths):
        try:
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        except OSError:
            continue
