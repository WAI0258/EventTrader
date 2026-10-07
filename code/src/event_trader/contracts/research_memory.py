"""Project-owned research-memory contracts for shared and target wiki surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.canonical_bundle import canonical_thesis_page_sections

from ._validators import (
    normalize_content,
    validate_page_key,
    validate_scope_key,
    validate_target_key,
)

PageKind = Literal[
    "thesis",
    "timeline",
    "risks",
    "watchlist",
    "operator",
    "index",
    "log",
    "topic",
    "entity",
    "source",
    "review",
]
ScopeKind = Literal["shared", "target"]
SnapshotSource = Literal["thesis_revision", "bootstrap_template"]


def _fixed_target_entry_template(title: str, page_name: str) -> str:
    lines = [f"# {title}", ""]
    for section_name in canonical_thesis_page_sections(page_name):
        lines.extend((f"## {section_name}", ""))
    return "\n".join(lines)


_ENTRY_PAGE_TEMPLATES: dict[str, str] = {
    "thesis.md": _fixed_target_entry_template("Thesis", "thesis.md"),
    "timeline.md": _fixed_target_entry_template("Timeline", "timeline.md"),
    "risks.md": _fixed_target_entry_template("Risks", "risks.md"),
    "watchlist.md": _fixed_target_entry_template("Watchlist", "watchlist.md"),
    "operator.md": "",
    "index.md": "\n".join(
        (
            "# Index",
            "",
            "## Current Reading Path",
            "",
            "## Core Pages",
            "",
            "## Active Topics",
            "",
            "## Key References",
            "",
        )
    ),
    "log.md": "# Log\n",
}
_FIXED_TARGET_PAGES: dict[str, PageKind] = {
    "thesis.md": "thesis",
    "timeline.md": "timeline",
    "risks.md": "risks",
    "watchlist.md": "watchlist",
    "operator.md": "operator",
    "index.md": "index",
    "log.md": "log",
}
_FIXED_SHARED_PAGES: dict[str, PageKind] = {
    "index.md": "index",
    "log.md": "log",
}


def target_page_template(page_name: str) -> str:
    """Return the canonical bootstrap template for one fixed target page."""
    if not isinstance(page_name, str):
        raise ResearchMemoryContractError("page_name must be a string.")
    normalized_page_name = page_name.strip()
    if not normalized_page_name:
        raise ResearchMemoryContractError("page_name must not be blank.")
    if normalized_page_name != page_name:
        raise ResearchMemoryContractError(
            "page_name must not include leading or trailing whitespace."
        )

    template = _ENTRY_PAGE_TEMPLATES.get(normalized_page_name)
    if template is None:
        allowed_pages = ", ".join(sorted(_ENTRY_PAGE_TEMPLATES))
        raise ResearchMemoryContractError(
            "page_name must resolve one fixed target page template: "
            f"{allowed_pages}."
        )
    return template


class ResearchMemoryContractError(ValueError):
    """Raised when research-memory layout inputs or paths are invalid."""


@dataclass(frozen=True, slots=True)
class SharedResearchLayout:
    """Resolved shared-scoped research-memory paths."""

    shared_root: Path
    index_file: Path
    log_file: Path
    topics_root: Path
    entities_root: Path

    def topic_page(self, topic_name: str) -> Path:
        """Resolve a shared topic page under the canonical topics family."""
        validated_name = validate_page_key(
            topic_name,
            field_name="topic",
            error_type=ResearchMemoryContractError,
        )
        return (self.topics_root / f"{validated_name}.md").resolve(strict=False)

    def entity_page(self, entity_name: str) -> Path:
        """Resolve a shared entity page under the canonical entities family."""
        validated_name = validate_page_key(
            entity_name,
            field_name="entity",
            error_type=ResearchMemoryContractError,
        )
        return (self.entities_root / f"{validated_name}.md").resolve(strict=False)


@dataclass(frozen=True, slots=True)
class TargetResearchLayout:
    """Resolved target-scoped research-memory paths."""

    target_key: str
    target_root: Path
    thesis_file: Path
    timeline_file: Path
    risks_file: Path
    watchlist_file: Path
    operator_file: Path
    index_file: Path
    log_file: Path
    topics_root: Path
    sources_root: Path
    reviews_root: Path

    def topic_page(self, topic_name: str) -> Path:
        """Resolve a target-scoped topic page under the canonical topics family."""
        validated_name = validate_page_key(
            topic_name,
            field_name="topic",
            error_type=ResearchMemoryContractError,
        )
        return (self.topics_root / f"{validated_name}.md").resolve(strict=False)

    def source_page(self, source_name: str) -> Path:
        """Resolve a durable target-scoped source page."""
        validated_name = validate_page_key(
            source_name,
            field_name="source",
            error_type=ResearchMemoryContractError,
        )
        return (self.sources_root / f"{validated_name}.md").resolve(strict=False)

    def review_page(self, review_name: str) -> Path:
        """Resolve a target-scoped episodic review page."""
        validated_name = validate_page_key(
            review_name,
            field_name="review",
            error_type=ResearchMemoryContractError,
        )
        return (self.reviews_root / f"{validated_name}.md").resolve(strict=False)


@dataclass(frozen=True, slots=True)
class ResearchScope:
    """One validated research-memory scope with canonical index/log surfaces."""

    scope_key: str
    scope_kind: ScopeKind
    target_key: str | None
    root: Path
    index_file: Path
    log_file: Path


@dataclass(frozen=True, slots=True)
class PageRef:
    """Logical reference to one wiki page inside the shared library."""

    page_path: str
    target_key: str | None
    page_kind: PageKind


@dataclass(frozen=True, slots=True)
class PageReadResult:
    """Typed read result for a single wiki page."""

    page_path: str
    content_md: str
    snapshot_source: SnapshotSource | None = None
    snapshot_revision_id: str | None = None
    snapshot_committed_at: datetime | None = None

    def __post_init__(self) -> None:
        page_ref = resolve_page_ref(self.page_path)
        normalized = normalize_content(
            self.content_md,
            field_name="content_md",
            error_type=ResearchMemoryContractError,
        )
        object.__setattr__(self, "page_path", page_ref.page_path)
        object.__setattr__(self, "content_md", normalized)
        if self.snapshot_source is None:
            if (
                self.snapshot_revision_id is not None
                or self.snapshot_committed_at is not None
            ):
                raise ResearchMemoryContractError(
                    "snapshot revision provenance requires snapshot_source."
                )
            return
        if self.snapshot_source not in {"thesis_revision", "bootstrap_template"}:
            raise ResearchMemoryContractError(
                "snapshot_source must be thesis_revision or bootstrap_template."
            )
        if self.snapshot_source == "bootstrap_template":
            if (
                self.snapshot_revision_id is not None
                or self.snapshot_committed_at is not None
            ):
                raise ResearchMemoryContractError(
                    "bootstrap_template reads must not include revision provenance."
                )
            return
        if (
            not isinstance(self.snapshot_revision_id, str)
            or not self.snapshot_revision_id.strip()
        ):
            raise ResearchMemoryContractError(
                "thesis_revision snapshot reads require snapshot_revision_id."
            )
        if not isinstance(self.snapshot_committed_at, datetime):
            raise ResearchMemoryContractError(
                "thesis_revision snapshot reads require snapshot_committed_at."
            )
        if (
            self.snapshot_committed_at.tzinfo is None
            or self.snapshot_committed_at.utcoffset() is None
        ):
            raise ResearchMemoryContractError(
                "snapshot_committed_at must be timezone-aware."
            )
        object.__setattr__(
            self,
            "snapshot_revision_id",
            self.snapshot_revision_id.strip(),
        )
        object.__setattr__(
            self,
            "snapshot_committed_at",
            self.snapshot_committed_at.astimezone(UTC),
        )


@dataclass(frozen=True, slots=True)
class WikiMatch:
    """Typed wiki-search match surfaced to checker or analysis."""

    page_path: str
    target_key: str | None
    snippet: str

    def __post_init__(self) -> None:
        page_ref = resolve_page_ref(self.page_path)
        normalized = normalize_content(
            self.snippet,
            field_name="snippet",
            error_type=ResearchMemoryContractError,
        )
        _validate_wiki_match_target(page_ref, self.target_key)
        object.__setattr__(self, "page_path", page_ref.page_path)
        object.__setattr__(self, "snippet", normalized)


def build_shared_research_layout(layout: WorkspaceLayout) -> SharedResearchLayout:
    """Build the canonical shared research-memory layout without creating it."""
    if not isinstance(layout, WorkspaceLayout):
        raise ResearchMemoryContractError("layout must be a WorkspaceLayout instance.")

    return SharedResearchLayout(
        shared_root=layout.shared_root,
        index_file=layout.shared_index_file,
        log_file=layout.shared_log_file,
        topics_root=layout.shared_topics_root,
        entities_root=layout.shared_entities_root,
    )


def build_target_research_layout(
    layout: WorkspaceLayout,
    target_key: str,
) -> TargetResearchLayout:
    """Build the canonical target-scoped research-memory layout without creating it."""
    if not isinstance(layout, WorkspaceLayout):
        raise ResearchMemoryContractError("layout must be a WorkspaceLayout instance.")

    validated_target_key = validate_target_key(
        target_key,
        error_type=ResearchMemoryContractError,
    )
    target_root = (layout.targets_root / validated_target_key).resolve(strict=False)
    return TargetResearchLayout(
        target_key=validated_target_key,
        target_root=target_root,
        thesis_file=(target_root / "thesis.md").resolve(strict=False),
        timeline_file=(target_root / "timeline.md").resolve(strict=False),
        risks_file=(target_root / "risks.md").resolve(strict=False),
        watchlist_file=(target_root / "watchlist.md").resolve(strict=False),
        operator_file=(target_root / "operator.md").resolve(strict=False),
        index_file=(target_root / "index.md").resolve(strict=False),
        log_file=(target_root / "log.md").resolve(strict=False),
        topics_root=(target_root / "topics").resolve(strict=False),
        sources_root=(target_root / "sources").resolve(strict=False),
        reviews_root=(target_root / "reviews").resolve(strict=False),
    )


def resolve_scope(layout: WorkspaceLayout, scope_key: str) -> ResearchScope:
    """Resolve one documented scope key into canonical index/log surfaces."""
    if not isinstance(layout, WorkspaceLayout):
        raise ResearchMemoryContractError("layout must be a WorkspaceLayout instance.")

    normalized_scope = validate_scope_key(
        scope_key,
        error_type=ResearchMemoryContractError,
    )
    if normalized_scope == "shared":
        shared_layout = build_shared_research_layout(layout)
        return ResearchScope(
            scope_key="shared",
            scope_kind="shared",
            target_key=None,
            root=shared_layout.shared_root,
            index_file=shared_layout.index_file,
            log_file=shared_layout.log_file,
        )

    target_key = normalized_scope.partition(":")[2]
    target_layout = build_target_research_layout(layout, target_key)
    return ResearchScope(
        scope_key=normalized_scope,
        scope_kind="target",
        target_key=target_layout.target_key,
        root=target_layout.target_root,
        index_file=target_layout.index_file,
        log_file=target_layout.log_file,
    )


def resolve_page_ref(page_path: str) -> PageRef:
    """Validate one logical wiki path and classify it into an allowed page family."""
    normalized_page_path = _normalize_page_path(page_path)
    parts = normalized_page_path.split("/")

    if parts[0] == "shared":
        return _resolve_shared_page_ref(parts, normalized_page_path)
    if parts[0] == "targets":
        return _resolve_target_page_ref(parts, normalized_page_path)

    raise ResearchMemoryContractError(
        "page_path must start with 'shared/' or 'targets/<target_key>/'."
    )


def resolve_reflection_anchor_page_ref(page_path: str) -> PageRef:
    """Resolve one canonical reflection-anchor page path to its logical page ref."""
    page_ref = resolve_page_ref(page_path)
    if page_ref.page_kind != "log":
        raise ResearchMemoryContractError(
            "reflection anchor page_path must resolve only 'shared/log.md' or "
            "'targets/<target_key>/log.md'."
        )
    return page_ref


def resolve_reflection_anchor_scope(page_path: str) -> str:
    """Resolve the documented scope key for one canonical reflection log surface."""
    page_ref = resolve_reflection_anchor_page_ref(page_path)
    if page_ref.target_key is None:
        return "shared"
    return f"target:{page_ref.target_key}"


def resolve_page_path(layout: WorkspaceLayout, page_path: str) -> Path:
    """Resolve one validated logical wiki path to its canonical absolute path."""
    if not isinstance(layout, WorkspaceLayout):
        raise ResearchMemoryContractError("layout must be a WorkspaceLayout instance.")

    page_ref = resolve_page_ref(page_path)
    normalized_page_path = page_ref.page_path
    parts = normalized_page_path.split("/")

    if parts[0] == "shared":
        shared_layout = build_shared_research_layout(layout)
        if page_ref.page_kind == "index":
            return shared_layout.index_file
        if page_ref.page_kind == "log":
            return shared_layout.log_file
        if page_ref.page_kind == "topic":
            return shared_layout.topic_page(parts[2][:-3])
        if page_ref.page_kind == "entity":
            return shared_layout.entity_page(parts[2][:-3])
        raise ResearchMemoryContractError(
            "Unsupported shared page kind "
            f"'{page_ref.page_kind}' for page_path '{page_ref.page_path}'."
        )

    target_layout = build_target_research_layout(layout, parts[1])
    if page_ref.page_kind == "thesis":
        return target_layout.thesis_file
    if page_ref.page_kind == "timeline":
        return target_layout.timeline_file
    if page_ref.page_kind == "risks":
        return target_layout.risks_file
    if page_ref.page_kind == "watchlist":
        return target_layout.watchlist_file
    if page_ref.page_kind == "operator":
        return target_layout.operator_file
    if page_ref.page_kind == "index":
        return target_layout.index_file
    if page_ref.page_kind == "log":
        return target_layout.log_file
    if page_ref.page_kind == "topic":
        return target_layout.topic_page(parts[3][:-3])
    if page_ref.page_kind == "source":
        return target_layout.source_page(parts[3][:-3])
    if page_ref.page_kind == "review":
        return target_layout.review_page(parts[3][:-3])

    raise ResearchMemoryContractError(
        f"Unsupported target page kind '{page_ref.page_kind}' for page_path '{page_ref.page_path}'."
    )


def initialize_target_research_layout(
    layout: WorkspaceLayout,
    target_key: str,
) -> TargetResearchLayout:
    """Create the canonical target tree on demand with rollback on failure."""
    target_layout = build_target_research_layout(layout, target_key)
    created_paths: list[Path] = []

    try:
        _ensure_directory(target_layout.target_root, created_paths)
        _ensure_markdown_file(
            target_layout.thesis_file,
            _ENTRY_PAGE_TEMPLATES["thesis.md"],
            created_paths,
        )
        _ensure_markdown_file(
            target_layout.timeline_file,
            _ENTRY_PAGE_TEMPLATES["timeline.md"],
            created_paths,
        )
        _ensure_markdown_file(
            target_layout.risks_file,
            _ENTRY_PAGE_TEMPLATES["risks.md"],
            created_paths,
        )
        _ensure_markdown_file(
            target_layout.watchlist_file,
            _ENTRY_PAGE_TEMPLATES["watchlist.md"],
            created_paths,
        )
        _ensure_markdown_file(
            target_layout.operator_file,
            _ENTRY_PAGE_TEMPLATES["operator.md"],
            created_paths,
        )
        _ensure_markdown_file(
            target_layout.index_file,
            _ENTRY_PAGE_TEMPLATES["index.md"],
            created_paths,
        )
        _ensure_markdown_file(
            target_layout.log_file,
            _ENTRY_PAGE_TEMPLATES["log.md"],
            created_paths,
        )
        _ensure_directory(target_layout.topics_root, created_paths)
        _ensure_directory(target_layout.sources_root, created_paths)
        _ensure_directory(target_layout.reviews_root, created_paths)
    except ResearchMemoryContractError:
        _rollback_created_paths(created_paths)
        raise
    except OSError as exc:
        _rollback_created_paths(created_paths)
        raise ResearchMemoryContractError(
            "Failed to initialize target research-memory layout under "
            f"{target_layout.target_root}: {exc}"
        ) from exc

    return target_layout


def _normalize_page_path(page_path: str) -> str:
    if not isinstance(page_path, str):
        raise ResearchMemoryContractError("page_path must be a string.")

    normalized = page_path.strip()
    if not normalized:
        raise ResearchMemoryContractError("page_path must not be blank.")
    if normalized != page_path:
        raise ResearchMemoryContractError(
            "page_path must not include leading or trailing whitespace."
        )
    if "\\" in normalized:
        raise ResearchMemoryContractError(
            "page_path must use logical '/' separators, not filesystem path separators."
        )
    if normalized.startswith("/") or normalized.endswith("/"):
        raise ResearchMemoryContractError(
            "page_path must be a logical library path, not an absolute path."
        )
    if "//" in normalized:
        raise ResearchMemoryContractError(
            "page_path must not include empty path segments."
        )

    return normalized


def _resolve_shared_page_ref(parts: list[str], page_path: str) -> PageRef:
    if len(parts) == 2:
        file_name = parts[1]
        if file_name not in _FIXED_SHARED_PAGES:
            raise ResearchMemoryContractError(
                "shared page_path must resolve only 'index.md' or 'log.md'."
            )
        return PageRef(
            page_path=page_path,
            target_key=None,
            page_kind=_FIXED_SHARED_PAGES[file_name],
        )

    if len(parts) == 3 and parts[1] == "topics":
        _validate_markdown_slug(parts[2], field_name="topic")
        return PageRef(page_path=page_path, target_key=None, page_kind="topic")
    if len(parts) == 3 and parts[1] == "entities":
        _validate_markdown_slug(parts[2], field_name="entity")
        return PageRef(page_path=page_path, target_key=None, page_kind="entity")

    raise ResearchMemoryContractError(
        "shared page_path must resolve only canonical shared families: "
        "index.md, log.md, topics/<slug>.md, or entities/<slug>.md."
    )


def _resolve_target_page_ref(parts: list[str], page_path: str) -> PageRef:
    if len(parts) < 3:
        raise ResearchMemoryContractError(
            "target page_path must follow targets/<target_key>/<page>.md."
        )

    target_key = validate_target_key(
        parts[1],
        error_type=ResearchMemoryContractError,
    )

    if len(parts) == 3:
        file_name = parts[2]
        if file_name not in _FIXED_TARGET_PAGES:
            raise ResearchMemoryContractError(
                "target page_path must resolve only canonical target entry pages: "
                "thesis.md, timeline.md, risks.md, watchlist.md, operator.md, "
                "index.md, or log.md."
            )
        return PageRef(
            page_path=page_path,
            target_key=target_key,
            page_kind=_FIXED_TARGET_PAGES[file_name],
        )

    if len(parts) == 4 and parts[2] == "topics":
        _validate_markdown_slug(parts[3], field_name="topic")
        return PageRef(page_path=page_path, target_key=target_key, page_kind="topic")
    if len(parts) == 4 and parts[2] == "sources":
        _validate_markdown_slug(parts[3], field_name="source")
        return PageRef(page_path=page_path, target_key=target_key, page_kind="source")
    if len(parts) == 4 and parts[2] == "reviews":
        _validate_markdown_slug(parts[3], field_name="review")
        return PageRef(page_path=page_path, target_key=target_key, page_kind="review")

    raise ResearchMemoryContractError(
        "target page_path must resolve only canonical target families: "
        "thesis.md, timeline.md, risks.md, watchlist.md, operator.md, index.md, "
        "log.md, topics/<slug>.md, sources/<slug>.md, or reviews/<slug>.md."
    )


def _validate_markdown_slug(file_name: str, *, field_name: str) -> str:
    if not file_name.endswith(".md"):
        raise ResearchMemoryContractError(
            f"{field_name} page_path must end with '.md'."
        )
    return validate_page_key(
        file_name[:-3],
        field_name=field_name,
        error_type=ResearchMemoryContractError,
    )


def _validate_wiki_match_target(page_ref: PageRef, target_key: str | None) -> None:
    if page_ref.target_key is None:
        if target_key is not None:
            raise ResearchMemoryContractError(
                "target_key must be None when page_path resolves to shared research memory."
            )
        return

    if target_key != page_ref.target_key:
        raise ResearchMemoryContractError(
            "target_key must match the target resolved from page_path."
        )


def _ensure_directory(path: Path, created_paths: list[Path]) -> None:
    if path.exists():
        if not path.is_dir():
            raise ResearchMemoryContractError(
                "Expected a directory in target research-memory layout, but found a "
                f"file at: {path.resolve(strict=False)}"
            )
        return

    path.mkdir()
    created_paths.append(path.resolve(strict=False))


def _ensure_markdown_file(path: Path, content: str, created_paths: list[Path]) -> None:
    if path.exists():
        if not path.is_file():
            raise ResearchMemoryContractError(
                "Expected a markdown file in target research-memory layout, but found a "
                f"directory at: {path.resolve(strict=False)}"
            )
        return

    path.write_text(content, encoding="utf-8")
    created_paths.append(path.resolve(strict=False))


def _rollback_created_paths(created_paths: list[Path]) -> None:
    for path in reversed(created_paths):
        try:
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        except OSError:
            continue


__all__ = [
    "PageKind",
    "PageReadResult",
    "PageRef",
    "ResearchMemoryContractError",
    "ResearchScope",
    "ScopeKind",
    "SharedResearchLayout",
    "SnapshotSource",
    "TargetResearchLayout",
    "WikiMatch",
    "build_shared_research_layout",
    "build_target_research_layout",
    "initialize_target_research_layout",
    "resolve_page_path",
    "resolve_page_ref",
    "resolve_reflection_anchor_page_ref",
    "resolve_reflection_anchor_scope",
    "resolve_scope",
    "target_page_template",
]
