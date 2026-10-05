"""Configured pruning for non-truth runtime/debug artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from event_trader.config import ArtifactRetentionConfig, KernelConfig
from event_trader.storage import WorkspaceLayout


class ArtifactRetentionError(RuntimeError):
    """Raised when configured artifact retention would cross a truth boundary."""


@dataclass(frozen=True, slots=True)
class ArtifactRetentionRootReceipt:
    """One retention pass over one configured artifact root."""

    category: str
    root: Path
    retention_days: int
    pruned_files: int
    skipped_missing_root: bool


@dataclass(frozen=True, slots=True)
class ArtifactRetentionReceipt:
    """Summary of one configured retention pass."""

    enabled: bool
    root_receipts: tuple[ArtifactRetentionRootReceipt, ...]

    @property
    def pruned_files(self) -> int:
        return sum(receipt.pruned_files for receipt in self.root_receipts)


def prune_configured_artifacts(
    *,
    config: KernelConfig,
    layout: object,
    now: datetime,
) -> ArtifactRetentionReceipt:
    """Delete expired non-truth debug artifacts from explicitly configured roots."""
    if not isinstance(config, KernelConfig):
        raise ArtifactRetentionError("config must be a KernelConfig instance.")
    retention = config.artifact_retention
    if retention is None:
        return ArtifactRetentionReceipt(enabled=False, root_receipts=())
    if not isinstance(layout, WorkspaceLayout):
        raise ArtifactRetentionError("layout must be a WorkspaceLayout instance.")

    normalized_now = _normalize_now(now)
    receipts = tuple(
        _prune_root(
            category=category,
            root=root,
            retention_days=retention_days,
            layout=layout,
            now=normalized_now,
        )
        for category, root, retention_days in _configured_roots(config, retention)
    )
    return ArtifactRetentionReceipt(enabled=True, root_receipts=receipts)


def _configured_roots(
    config: KernelConfig,
    retention: ArtifactRetentionConfig,
) -> tuple[tuple[str, Path, int], ...]:
    roots: list[tuple[str, Path, int]] = []
    if config.analysis_agent is not None:
        roots.append(
            (
                "analysis_debug_log",
                config.analysis_agent.log_dir,
                retention.analysis_debug_log_days,
            )
        )
    if config.pm_review_agent is not None:
        roots.append(
            (
                "pm_review_debug_log",
                config.pm_review_agent.log_dir,
                retention.pm_review_debug_log_days,
            )
        )
    if config.checker_agent is not None:
        roots.append(
            (
                "checker_debug_log",
                config.checker_agent.log_dir,
                retention.checker_debug_log_days,
            )
        )
    if config.live is not None and config.live.web_search is not None:
        roots.append(
            (
                "search_debug_log",
                config.live.web_search.log_dir,
                retention.search_debug_log_days,
            )
        )
    if config.reflection_agent is not None:
        roots.append(
            (
                "reflection_debug_log",
                config.reflection_agent.log_dir,
                retention.reflection_debug_log_days,
            )
        )
    return tuple(roots)


def _prune_root(
    *,
    category: str,
    root: Path,
    retention_days: int,
    layout: WorkspaceLayout,
    now: datetime,
) -> ArtifactRetentionRootReceipt:
    resolved_root = root.resolve(strict=False)
    _reject_truth_overlap(category=category, root=resolved_root, layout=layout)
    if not resolved_root.exists():
        return ArtifactRetentionRootReceipt(
            category=category,
            root=resolved_root,
            retention_days=retention_days,
            pruned_files=0,
            skipped_missing_root=True,
        )
    if not resolved_root.is_dir():
        raise ArtifactRetentionError(
            f"artifact retention root for {category} must be a directory: {resolved_root}"
        )

    cutoff = now - timedelta(days=retention_days)
    pruned_files = 0
    try:
        artifact_paths = sorted(resolved_root.rglob("*"))
    except OSError as exc:
        raise ArtifactRetentionError(
            f"failed to scan artifact retention root for {category}: {resolved_root}"
        ) from exc

    for artifact_path in artifact_paths:
        try:
            if artifact_path.is_symlink() or not artifact_path.is_file():
                continue
            modified_at = datetime.fromtimestamp(artifact_path.stat().st_mtime, tz=UTC)
            if modified_at >= cutoff:
                continue
            artifact_path.unlink()
        except OSError as exc:
            raise ArtifactRetentionError(
                f"failed to prune artifact for {category}: {artifact_path}"
            ) from exc
        pruned_files += 1

    return ArtifactRetentionRootReceipt(
        category=category,
        root=resolved_root,
        retention_days=retention_days,
        pruned_files=pruned_files,
        skipped_missing_root=False,
    )


def _reject_truth_overlap(
    *,
    category: str,
    root: Path,
    layout: WorkspaceLayout,
) -> None:
    workspace_root = layout.root.resolve(strict=False)
    if root == workspace_root:
        raise ArtifactRetentionError(
            f"artifact retention root for {category} must not be the workspace root."
        )
    for truth_root in layout.truth_roots:
        resolved_truth_root = truth_root.resolve(strict=False)
        if _is_relative_to(root, resolved_truth_root) or _is_relative_to(
            resolved_truth_root,
            root,
        ):
            raise ArtifactRetentionError(
                f"artifact retention root for {category} overlaps truth root "
                f"{resolved_truth_root}: {root}"
            )
    if _is_relative_to(root, workspace_root) or _is_relative_to(workspace_root, root):
        raise ArtifactRetentionError(
            f"artifact retention root for {category} must stay outside workspace root "
            f"{workspace_root}: {root}"
        )


def _normalize_now(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ArtifactRetentionError("now must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ArtifactRetentionError("now must include timezone information.")
    return value.astimezone(UTC)


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
    except ValueError:
        return False
    return True
