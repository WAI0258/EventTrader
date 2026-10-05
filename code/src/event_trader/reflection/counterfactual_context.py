"""Horizon-safe counterfactual report reads for target reflection."""

from __future__ import annotations

from datetime import datetime

from event_trader.counterfactuals import (
    CounterfactualEvaluationReport,
    CounterfactualReportWriter,
)
from event_trader.storage import WorkspaceLayout


class CounterfactualReflectionContextError(ValueError):
    """Raised when reflection cannot safely read a counterfactual report."""


def read_horizon_counterfactual_report(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    episode_id: str,
    expected_market_provenance_hash: str,
    horizon_end_at: datetime,
    as_of_at: datetime,
) -> CounterfactualEvaluationReport:
    """Read a counterfactual report only after the reflection horizon has matured."""
    if not isinstance(layout, WorkspaceLayout):
        raise CounterfactualReflectionContextError(
            "layout must be a WorkspaceLayout instance."
        )
    if not isinstance(as_of_at, datetime) or not isinstance(horizon_end_at, datetime):
        raise CounterfactualReflectionContextError(
            "as_of_at and horizon_end_at must be datetimes."
        )
    if as_of_at < horizon_end_at:
        raise CounterfactualReflectionContextError(
            "counterfactual report is not readable before the reflection horizon."
        )
    report = CounterfactualReportWriter(layout).read_for_episode(
        target_key=target_key,
        episode_id=episode_id,
    )
    if report.market_provenance_hash != expected_market_provenance_hash:
        raise CounterfactualReflectionContextError(
            "counterfactual report market provenance hash mismatch."
        )
    return report


__all__ = [
    "CounterfactualReflectionContextError",
    "read_horizon_counterfactual_report",
]
