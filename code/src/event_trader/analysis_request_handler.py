"""Thin analysis request handling seam."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from event_trader.analysis import AnalysisContext, AnalysisContextLoader
from event_trader.ceau import UnitFormationLane
from event_trader.contracts import AnalysisRequest, AnalysisResult
from event_trader.contracts.runtime import analysis_lane


class RuntimeResearchLoopError(ValueError):
    """Raised when the narrow analysis seam is miswired."""


type AnalysisCallback = Callable[[AnalysisContext], AnalysisResult]


class AnalysisRequestHandler(Protocol):
    """Callable analysis seam with optional CEAU unit formation context."""

    def __call__(
        self,
        lane: str,
        request: AnalysisRequest,
        unit_formation_lane: UnitFormationLane | None = None,
    ) -> AnalysisResult: ...


def handle_analysis_request(
    lane: str,
    request: AnalysisRequest,
    *,
    context_loader: AnalysisContextLoader,
    analyze: AnalysisCallback,
    unit_formation_lane: UnitFormationLane | None = None,
) -> AnalysisResult:
    """Handle one canonical checker escalation by reloading analysis context."""
    if not isinstance(request, AnalysisRequest):
        raise RuntimeResearchLoopError(
            "request must be an AnalysisRequest instance."
        )
    if not isinstance(context_loader, AnalysisContextLoader):
        raise RuntimeResearchLoopError(
            "context_loader must be an AnalysisContextLoader instance."
        )
    if not callable(analyze):
        raise RuntimeResearchLoopError("analyze must be callable.")

    expected_lane = analysis_lane(request.target_key)
    if lane != expected_lane:
        raise RuntimeResearchLoopError(
            "Analysis handler requires the canonical analysis lane for "
            f"target_key={request.target_key!r}; got {lane!r}."
        )

    context = context_loader.load_context(
        request,
        unit_formation_lane=unit_formation_lane,
    )
    result = analyze(context)
    _validate_analysis_result(result, request)
    return result


def build_analysis_request_handler(
    *,
    context_loader: AnalysisContextLoader,
    analyze: AnalysisCallback,
) -> AnalysisRequestHandler:
    """Build a direct escalation-dispatch handler for analysis execution."""
    if not isinstance(context_loader, AnalysisContextLoader):
        raise RuntimeResearchLoopError(
            "context_loader must be an AnalysisContextLoader instance."
        )
    if not callable(analyze):
        raise RuntimeResearchLoopError("analyze must be callable.")

    def _handle(
        lane: str,
        request: AnalysisRequest,
        unit_formation_lane: UnitFormationLane | None = None,
    ) -> AnalysisResult:
        return handle_analysis_request(
            lane,
            request,
            context_loader=context_loader,
            analyze=analyze,
            unit_formation_lane=unit_formation_lane,
        )

    return _handle


def _validate_analysis_result(
    result: AnalysisResult,
    request: AnalysisRequest,
) -> None:
    if not isinstance(result, AnalysisResult):
        raise RuntimeResearchLoopError(
            "analyze must return an AnalysisResult instance."
        )
    if result.target_key != request.target_key:
        raise RuntimeResearchLoopError(
            "analyze must return an AnalysisResult with the same target_key as the "
            "active AnalysisRequest."
        )
    if result.event_ids != request.event_ids:
        raise RuntimeResearchLoopError(
            "analyze must return an AnalysisResult with the same event_ids as the "
            "active AnalysisRequest."
        )
    if result.outcome not in {"no_update", "memory_updated"}:
        raise RuntimeResearchLoopError(
            "analyze must return an exposure-blind outcome: no_update or memory_updated."
        )
    assessment = result.analysis_assessment
    if assessment is None:
        return
    if assessment.target_key != request.target_key:
        raise RuntimeResearchLoopError(
            "analysis_assessment.target_key must match the active AnalysisRequest."
        )
    if not set(request.event_ids).issubset(set(assessment.source_event_ids)):
        raise RuntimeResearchLoopError(
            "analysis_assessment.source_event_ids must cover the active "
            "AnalysisRequest event_ids."
        )


__all__ = [
    "AnalysisCallback",
    "AnalysisRequestHandler",
    "RuntimeResearchLoopError",
    "build_analysis_request_handler",
    "handle_analysis_request",
]
