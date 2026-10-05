"""Deterministic post-episode counterfactual evaluation surface."""

from .contracts import (
    REQUIRED_BASELINE_TYPES,
    CounterfactualBaselineResult,
    CounterfactualBaselineStatus,
    CounterfactualBaselineType,
    CounterfactualContractError,
    CounterfactualEpisodeMetrics,
    CounterfactualEvaluationReport,
    DecisionQualityAttribution,
    QualityStatus,
    parse_counterfactual_evaluation_report,
)
from .deterministic import build_counterfactual_evaluation_report
from .metrics import (
    actual_return_from_validation,
    build_decision_quality_attribution,
    build_episode_metrics,
)
from .report_writer import CounterfactualReportWriter, CounterfactualReportWriterError

__all__ = [
    "CounterfactualBaselineResult",
    "CounterfactualBaselineStatus",
    "CounterfactualBaselineType",
    "CounterfactualContractError",
    "CounterfactualEpisodeMetrics",
    "CounterfactualEvaluationReport",
    "CounterfactualReportWriter",
    "CounterfactualReportWriterError",
    "DecisionQualityAttribution",
    "QualityStatus",
    "REQUIRED_BASELINE_TYPES",
    "actual_return_from_validation",
    "build_decision_quality_attribution",
    "build_counterfactual_evaluation_report",
    "build_episode_metrics",
    "parse_counterfactual_evaluation_report",
]
