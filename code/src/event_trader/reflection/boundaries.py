"""Central bounded guardrails for reflection-owned write surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from event_trader.contracts.research_memory import (
    ResearchMemoryContractError,
    resolve_page_path,
    resolve_page_ref,
)
from event_trader.storage import WorkspaceLayout

type ReflectionOwnedSurface = Literal[
    "target_review",
    "shared_log_anchor",
]
type ReflectionBoundaryRule = Literal[
    "evidence_identity_non_self_modifying",
    "admission_semantics_non_self_modifying",
    "anti_cheating_non_self_modifying",
    "historical_visibility_non_self_modifying",
]

_ALLOWED_SURFACE_GUIDANCE = (
    "Reflection-owned writes are limited to targets/<target_key>/reviews/*.md, "
    "and justified shared/log.md anchors only."
)
_RULE_DETAILS: dict[ReflectionBoundaryRule, str] = {
    "evidence_identity_non_self_modifying": (
        "Evidence identity remains derived from admitted evidence inputs and must "
        "stay deterministic."
    ),
    "admission_semantics_non_self_modifying": (
        "Admission semantics remain a deterministic ingest boundary outside "
        "reflection-owned learning."
    ),
    "anti_cheating_non_self_modifying": (
        "Replay anti-cheating rules remain deterministic and must not be revised by "
        "reflection outputs."
    ),
    "historical_visibility_non_self_modifying": (
        "Historical visibility remains the replay release-scheduling boundary and "
        "must not be widened by reflection."
    ),
}


class ReflectionBoundaryError(ValueError):
    """Raised when reflection tries to widen beyond bounded learning surfaces."""


@dataclass(frozen=True, slots=True)
class ReflectionWriteSurface:
    """One resolved reflection-owned write surface inside canonical research memory."""

    page_path: str
    artifact_path: Path
    allowed_surface: ReflectionOwnedSurface

    def __post_init__(self) -> None:
        if not isinstance(self.page_path, str) or not self.page_path:
            raise ReflectionBoundaryError("page_path must be a non-empty string.")
        if not isinstance(self.artifact_path, Path):
            raise ReflectionBoundaryError("artifact_path must be a pathlib.Path.")
        if self.allowed_surface not in {
            "target_review",
            "shared_log_anchor",
        }:
            raise ReflectionBoundaryError(
                "allowed_surface must be one of target_review or shared_log_anchor."
            )


def enforce_reflection_write_surface(
    *,
    page_path: str,
    layout: WorkspaceLayout,
) -> ReflectionWriteSurface:
    """Resolve and enforce one allowed reflection-owned write surface.

    Reflection may only write two audit surfaces:
    - target review artifacts under `targets/<target_key>/reviews/*.md`
    - justified shared follow-on anchors at `shared/log.md`
    """

    if not isinstance(layout, WorkspaceLayout):
        raise ReflectionBoundaryError("layout must be a WorkspaceLayout instance.")

    attempted_surface = str(page_path)
    try:
        page_ref = resolve_page_ref(page_path)
        artifact_path = resolve_page_path(layout, page_ref.page_path)
    except ResearchMemoryContractError as exc:
        raise ReflectionBoundaryError(
            _format_boundary_rejection(
                attempted_surface=attempted_surface,
                blocked_surface="unknown",
                detail=(
                    "Requested page_path is not a canonical research-memory surface "
                    f"({exc})."
                ),
            )
        ) from exc

    if page_ref.page_kind == "review":
        return ReflectionWriteSurface(
            page_path=page_ref.page_path,
            artifact_path=artifact_path,
            allowed_surface="target_review",
        )
    if page_ref.page_path == "shared/log.md":
        return ReflectionWriteSurface(
            page_path=page_ref.page_path,
            artifact_path=artifact_path,
            allowed_surface="shared_log_anchor",
        )

    raise ReflectionBoundaryError(
        _format_boundary_rejection(
            attempted_surface=page_ref.page_path,
            blocked_surface=page_ref.page_kind,
            detail="Requested page_path stays outside the reflection-owned write surface.",
        )
    )


def reject_reflection_rule_mutation(
    *,
    rule: ReflectionBoundaryRule,
    attempted_surface: str | None = None,
) -> NoReturn:
    """Reject mutation attempts against non-self-modifying deterministic rules."""

    detail = _RULE_DETAILS.get(rule)
    if detail is None:
        raise ReflectionBoundaryError(
            f"rule must be one of: {', '.join(sorted(_RULE_DETAILS))}."
        )

    raise ReflectionBoundaryError(
        _format_boundary_rejection(
            attempted_surface=attempted_surface,
            blocked_rule=rule,
            detail=detail,
        )
    )


def _format_boundary_rejection(
    *,
    detail: str,
    attempted_surface: str | None = None,
    blocked_surface: str | None = None,
    blocked_rule: ReflectionBoundaryRule | None = None,
) -> str:
    parts = ["Reflection boundary rejected attempted mutation:"]
    if attempted_surface is not None:
        parts.append(f"attempted_surface={attempted_surface!r}")
    if blocked_surface is not None:
        parts.append(f"blocked_surface={blocked_surface!r}")
    if blocked_rule is not None:
        parts.append(f"blocked_rule={blocked_rule!r}")
    return " ".join(parts) + f". {_ALLOWED_SURFACE_GUIDANCE} {detail}"


__all__ = [
    "ReflectionBoundaryError",
    "ReflectionBoundaryRule",
    "ReflectionOwnedSurface",
    "ReflectionWriteSurface",
    "enforce_reflection_write_surface",
    "reject_reflection_rule_mutation",
]
