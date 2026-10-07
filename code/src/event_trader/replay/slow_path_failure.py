"""Fail replay orchestration when a durable runtime worker records a new failure."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from event_trader.storage import WorkspaceLayout


class ReplaySlowPathFailureError(RuntimeError):
    """Raised when replay observes a new durable slow-path failure receipt."""


class ReplaySlowPathFailureGuard:
    """Track failure receipts created after one replay run begins."""

    def __init__(self, *, layout: WorkspaceLayout) -> None:
        self._failure_roots = (
            layout.runtime_root / "checker_work_queue" / "checker" / "failed",
            layout.runtime_root / "analysis_work_queue" / "analysis" / "failed",
            layout.runtime_root / "pm_review_work_queue" / "pm_review" / "failed",
            layout.runtime_root / "reflection_work_queue" / "reflection" / "failed",
        )
        self._known_failure_paths = {
            path.resolve(strict=False)
            for root in self._failure_roots
            for path in root.glob("*.json")
        }

    def raise_if_new_failure(
        self,
        *,
        replay_at: datetime,
        context: str,
    ) -> None:
        failure = self._next_new_failure()
        if failure is None:
            return
        raise ReplaySlowPathFailureError(
            "replay slow-path failed: "
            f"replay_at={replay_at.isoformat()} "
            f"context={context} "
            f"queue={failure['queue_name']} "
            f"work_item_id={failure['work_item_id']} "
            f"reason={failure['reason']} "
            f"error={failure['error']} "
            f"path={failure['failure_path']}"
        )

    def _next_new_failure(self) -> dict[str, str] | None:
        for root in self._failure_roots:
            for path in sorted(root.glob("*.json")):
                normalized_path = path.resolve(strict=False)
                if normalized_path in self._known_failure_paths:
                    continue
                self._known_failure_paths.add(normalized_path)
                payload = json.loads(path.read_text(encoding="utf-8"))
                return {
                    "queue_name": str(payload.get("queue_name") or path.parent.parent.name),
                    "work_item_id": str(payload.get("work_item_id") or ""),
                    "reason": str(payload.get("reason") or "worker_failed"),
                    "error": str(payload.get("error") or ""),
                    "failure_path": str(normalized_path),
                }
        return None


def advance_replay_deferred_until(
    *,
    replay_runtime: Any,
    replay_at: datetime,
    include_boundary: bool,
    failure_guard: ReplaySlowPathFailureGuard,
    context: str,
) -> None:
    """Advance deferred work and inspect failures after every internal advance."""

    def _raise_if_failed(advanced_at: datetime) -> None:
        failure_guard.raise_if_new_failure(
            replay_at=advanced_at,
            context=(
                context if advanced_at == replay_at else f"{context}:deferred_runtime"
            ),
        )

    replay_runtime.advance_deferred_until(
        replay_at=replay_at,
        include_boundary=include_boundary,
        after_advance=_raise_if_failed,
    )


def resume_replay_runtime_backlog(
    *,
    replay_runtime: Any,
    replay_at: datetime,
    failure_guard: ReplaySlowPathFailureGuard,
    context: str,
) -> None:
    """Resume durable replay work and fail on newly persisted worker failures."""

    if not callable(getattr(replay_runtime, "resume_runtime_backlog", None)):
        return
    replay_runtime.resume_runtime_backlog(replay_at=replay_at)
    failure_guard.raise_if_new_failure(replay_at=replay_at, context=context)


__all__ = [
    "ReplaySlowPathFailureError",
    "ReplaySlowPathFailureGuard",
    "advance_replay_deferred_until",
    "resume_replay_runtime_backlog",
]
