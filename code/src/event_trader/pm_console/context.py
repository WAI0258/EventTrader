"""PM Console runtime/workspace context DTO builder."""

from __future__ import annotations

from datetime import UTC, datetime

from event_trader.pm_console.schemas import (
    PMConsoleContextResponseDTO,
    PMConsoleThesisTranslationAvailabilityDTO,
)
from event_trader.pm_console.thesis_translation import (
    PMConsoleThesisTranslationService,
    inspect_thesis_translation_availability,
)
from event_trader.position_monitoring import list_position_monitoring_targets
from event_trader.storage import WorkspaceLayout


def build_context_response(
    layout: WorkspaceLayout,
    *,
    runtime_mode: str = "unknown",
    thesis_translation_service: PMConsoleThesisTranslationService | None = None,
) -> PMConsoleContextResponseDTO:
    """Build the local PM Console context response."""

    if not isinstance(layout, WorkspaceLayout):
        raise TypeError("layout must be a WorkspaceLayout instance.")

    position_targets = list_position_monitoring_targets(layout)
    data_source = "workspace_snapshot" if position_targets else "unknown"

    explanation_parts = [
        "PM Console API availability is separate from trading runtime status.",
        (
            "runtime_status remains unknown because the current workspace "
            "surfaces do not expose a crash-safe running/not_running lease."
        ),
    ]

    if runtime_mode == "unknown":
        explanation_parts.append(
            "runtime_mode is unknown because no explicit PM Console server mode was supplied."
        )
    else:
        explanation_parts.append(
            f"runtime_mode={runtime_mode} comes from the local PM Console server startup setting."
        )

    if data_source == "workspace_snapshot":
        targets_text = ", ".join(position_targets)
        explanation_parts.append(
            "Persisted workspace artifacts were found for "
            f"targets [{targets_text}], so PM Console is reading a workspace snapshot."
        )
    else:
        explanation_parts.append(
            "No persisted position-monitoring workspace artifacts were found "
            "yet, so missing data should be read as an empty workspace state "
            "rather than a live-runtime failure."
        )
    thesis_translation = _resolve_thesis_translation_availability(
        thesis_translation_service=thesis_translation_service,
    )

    return PMConsoleContextResponseDTO(
        generated_at=datetime.now(UTC).isoformat(),
        workspace_root=str(layout.root),
        runtime_status="unknown",
        runtime_mode=runtime_mode,
        data_source=data_source,
        explanation=" ".join(explanation_parts),
        thesis_translation=thesis_translation,
    )


def _resolve_thesis_translation_availability(
    *,
    thesis_translation_service: PMConsoleThesisTranslationService | None,
) -> PMConsoleThesisTranslationAvailabilityDTO:
    if thesis_translation_service is None:
        return inspect_thesis_translation_availability(
            translator=None,
            locale="zh-CN",
        )
    return thesis_translation_service.translation_availability(locale="zh-CN")


__all__ = ["build_context_response"]
