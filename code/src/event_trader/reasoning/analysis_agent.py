"""Provider-neutral contract for one analysis-agent inference attempt."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from event_trader.contracts import AnalysisAssessment, LLMUsageReceipt
from event_trader.market.contracts import MarketContextSnapshot
from event_trader.reasoning.analysis_contract_repair import AnalysisContractFailure
from event_trader.reasoning.analysis_read_audit import AnalysisReadAuditRequirements
from event_trader.reasoning.analysis_tools import AnalysisToolContext


class AnalysisAgentContractError(RuntimeError):
    """Raised when an analysis-agent adapter violates the project-owned port."""


AnalysisRepairOwnership = Literal["supervisor_attempt", "stage_local"]


@dataclass(frozen=True, slots=True)
class AnalysisAgentContract:
    """Business tool contract every replaceable analysis agent must receive."""

    required_tool_names: tuple[str, ...]


ANALYSIS_AGENT_CONTRACT = AnalysisAgentContract(
    required_tool_names=(
        "validate_analysis_final_payload",
        "read_evidence",
        "read_page",
        "read_section",
        "read_around_citation",
        "list_pages",
        "search_wiki",
        "update_page_section",
        "rewrite_page",
        "create_page",
        "read_market_overview",
        "read_price_volume_window",
        "read_technical_panel",
        "read_option_activity",
        "read_cross_asset_context",
        "read_cross_asset_window",
    ),
)


@dataclass(frozen=True, slots=True)
class AnalysisBusinessTask:
    """Implementation-neutral Analysis brief plus a typed repair signal."""

    business_brief: str
    final_payload_contract: str
    repair_failure: AnalysisContractFailure | None = None


@dataclass(frozen=True, slots=True)
class AnalysisAgentAttemptRequest:
    """Inputs owned by the common runtime for one bounded agent attempt."""

    contract: AnalysisAgentContract
    task_id: str
    attempt_index: int
    analysis_assessment_id: str
    previous_assessment: AnalysisAssessment | None
    business_task: AnalysisBusinessTask
    requires_watchlist_maintenance: bool
    market_context: MarketContextSnapshot | None
    tool_context: AnalysisToolContext
    read_audit_requirements: AnalysisReadAuditRequirements | None = None


@dataclass(frozen=True, slots=True)
class AnalysisAgentAttemptResult:
    """Semantic draft consumed by project-owned audit and commit gates."""

    payload: Mapping[str, object]
    llm_usage: LLMUsageReceipt


class AnalysisAgentExecutor(Protocol):
    """Replaceable executor for exactly one analysis-agent attempt."""

    @property
    def repair_ownership(self) -> AnalysisRepairOwnership: ...

    async def execute(
        self,
        request: AnalysisAgentAttemptRequest,
    ) -> AnalysisAgentAttemptResult: ...


async def execute_analysis_agent_attempt(
    *,
    executor: AnalysisAgentExecutor,
    request: AnalysisAgentAttemptRequest,
) -> AnalysisAgentAttemptResult:
    """Execute one attempt and reject adapters that bypass the public result contract."""

    if request.contract != ANALYSIS_AGENT_CONTRACT:
        raise AnalysisAgentContractError(
            "analysis agent executor requires the canonical ANALYSIS_AGENT_CONTRACT."
        )
    if not isinstance(request.attempt_index, int) or isinstance(request.attempt_index, bool):
        raise AnalysisAgentContractError("analysis agent attempt_index must be an integer.")
    if request.attempt_index < 0:
        raise AnalysisAgentContractError("analysis agent attempt_index must be non-negative.")
    if not request.analysis_assessment_id.strip():
        raise AnalysisAgentContractError("analysis_assessment_id must not be blank.")
    if request.previous_assessment is not None and not isinstance(
        request.previous_assessment,
        AnalysisAssessment,
    ):
        raise AnalysisAgentContractError(
            "previous_assessment must be an AnalysisAssessment when supplied."
        )
    if not isinstance(request.requires_watchlist_maintenance, bool):
        raise AnalysisAgentContractError(
            "requires_watchlist_maintenance must be a boolean."
        )
    if request.market_context is not None and not isinstance(
        request.market_context,
        MarketContextSnapshot,
    ):
        raise AnalysisAgentContractError(
            "market_context must be a MarketContextSnapshot when supplied."
        )
    if not isinstance(request.business_task, AnalysisBusinessTask):
        raise AnalysisAgentContractError(
            "analysis agent executor requires an AnalysisBusinessTask."
        )
    if not request.business_task.business_brief.strip():
        raise AnalysisAgentContractError("analysis business_brief must not be blank.")
    if not request.business_task.final_payload_contract.strip():
        raise AnalysisAgentContractError(
            "analysis final_payload_contract must not be blank."
        )
    repair_failure = request.business_task.repair_failure
    if repair_failure is not None and not isinstance(repair_failure, AnalysisContractFailure):
        raise AnalysisAgentContractError(
            "analysis repair_failure must be an AnalysisContractFailure when supplied."
        )
    result = await executor.execute(request)
    if not isinstance(result, AnalysisAgentAttemptResult):
        raise AnalysisAgentContractError(
            "analysis agent executor must return AnalysisAgentAttemptResult."
        )
    if not isinstance(result.payload, Mapping):
        raise AnalysisAgentContractError("analysis agent executor payload must be an object.")
    if not isinstance(result.llm_usage, LLMUsageReceipt):
        raise AnalysisAgentContractError(
            "analysis agent executor llm_usage must be an LLMUsageReceipt."
        )
    if result.llm_usage.agent_role != "analysis":
        raise AnalysisAgentContractError(
            "analysis agent executor llm_usage must use agent_role='analysis'."
        )
    return result


__all__ = [
    "ANALYSIS_AGENT_CONTRACT",
    "AnalysisAgentAttemptRequest",
    "AnalysisAgentAttemptResult",
    "AnalysisBusinessTask",
    "AnalysisRepairOwnership",
    "AnalysisAgentContract",
    "AnalysisAgentContractError",
    "AnalysisAgentExecutor",
    "execute_analysis_agent_attempt",
]
