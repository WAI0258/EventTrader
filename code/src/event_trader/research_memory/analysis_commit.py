"""Provider-neutral contract for committing analysis-owned research memory."""

from __future__ import annotations

from dataclasses import dataclass

from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.research_memory.active_price_projection import (
    project_active_price_sections,
)
from event_trader.research_memory.current_view import (
    MARKET_SETUP_DASHBOARD_SECTION,
    MarketSetupDashboardError,
    validate_market_setup_dashboard,
    validate_market_setup_dashboard_page,
)

_MARKET_SETUP_DASHBOARD_REPAIR_ACTION = (
    "Repair analysis_assessment.market_setup_dashboard_md and any thesis.md "
    "Market Setup Dashboard write. The section body must be exposure-blind "
    "market setup memory. Include `### Market Reference`, `### Setup Frame`, "
    "`### Watch Triggers`, and `### Evidence Gaps`; do not claim actual current "
    "exposure, target weight, entry, PnL, MFE, MAE, or open/close/reverse/resize "
    "action."
)


@dataclass(frozen=True, slots=True)
class AnalysisCommitWrite:
    """One normalized research-memory write proposed by an analysis agent."""

    operation: str
    page_path: str
    content_md: str
    section_name: str | None = None


class AnalysisCommitContractError(ValueError):
    """A provider-neutral, repairable violation of the analysis commit contract."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        recoverable: bool,
        suggested_action: str,
    ) -> None:
        self.error_code = error_code
        self.recoverable = recoverable
        self.suggested_action = suggested_action
        super().__init__(message)


def validate_analysis_commit_contract(
    *,
    target_key: str,
    analysis_assessment: AnalysisAssessment | None,
    writes: tuple[AnalysisCommitWrite, ...],
) -> None:
    """Reject noncanonical Dashboard content before any permanent analysis write."""

    try:
        if analysis_assessment is not None:
            validate_market_setup_dashboard(analysis_assessment.market_setup_dashboard_md)

        target_thesis_path = f"targets/{target_key}/thesis.md"
        projected_sections = project_active_price_sections(analysis_assessment)
        projected_dashboard_ids = {
            section.section_id
            for section in projected_sections
            if section.page_path == target_thesis_path
            and section.section_name == MARKET_SETUP_DASHBOARD_SECTION
        }
        for section in projected_sections:
            if section.section_id in projected_dashboard_ids:
                validate_market_setup_dashboard(section.content_md)

        for write in writes:
            if write.page_path != target_thesis_path:
                continue
            if write.operation == "update_page_section":
                section_id = f"{write.page_path}:{write.section_name}"
                if section_id in projected_dashboard_ids:
                    continue
                if write.section_name == MARKET_SETUP_DASHBOARD_SECTION:
                    validate_market_setup_dashboard(write.content_md)
                continue
            if write.operation in {"rewrite_page", "create_page"}:
                if projected_dashboard_ids:
                    continue
                validate_market_setup_dashboard_page(write.content_md)
    except MarketSetupDashboardError as exc:
        raise AnalysisCommitContractError(
            str(exc),
            error_code=exc.error_code,
            recoverable=exc.recoverable,
            suggested_action=_MARKET_SETUP_DASHBOARD_REPAIR_ACTION,
        ) from exc


__all__ = [
    "AnalysisCommitContractError",
    "AnalysisCommitWrite",
    "validate_analysis_commit_contract",
]
