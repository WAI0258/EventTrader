"""Production implementation selection for the PMReview role."""

from __future__ import annotations

from pathlib import Path

from event_trader.agent_implementation import (
    EVENT_TRADER_IMPLEMENTATION,
    MIROTHINKER_IMPLEMENTATION,
)
from event_trader.composition_error import CompositionError
from event_trader.config import KernelConfig, PMReviewAgentConfig
from event_trader.pm_review.prompt import PMReviewOutputProtocol
from event_trader.reasoning.runtime import AgentRuntime


def build_event_trader_pm_review_task_runner(
    *,
    agent_config: PMReviewAgentConfig,
    diagnostic_log_dir: Path | None = None,
) -> AgentRuntime:
    """Build the project-owned PMReview workflow used by every runtime surface."""

    if not isinstance(agent_config, PMReviewAgentConfig):
        raise CompositionError("agent_config must be a PMReviewAgentConfig instance.")
    from event_trader.pm_review.workflow import (
        PMReviewWorkflowConfig,
        build_pm_review_workflow,
    )

    return build_pm_review_workflow(
        config=PMReviewWorkflowConfig(
            log_dir=(
                agent_config.log_dir
                if diagnostic_log_dir is None
                else diagnostic_log_dir
            ),
            llm_provider=agent_config.llm_provider,
            llm_model_name=agent_config.llm_model_name,
            llm_api_key=agent_config.llm_api_key,
            llm_base_url=agent_config.llm_base_url,
            llm_reasoning_effort=agent_config.llm_reasoning_effort,
            llm_max_context_length=agent_config.llm_max_context_length,
            llm_max_output_tokens=agent_config.llm_max_output_tokens,
            wall_clock_timeout_seconds=agent_config.wall_clock_timeout_seconds,
        )
    )


def build_pm_review_task_runner(*, config: KernelConfig) -> AgentRuntime:
    """Build the explicitly selected production PMReview implementation."""

    if not isinstance(config, KernelConfig):
        raise CompositionError("config must be a KernelConfig instance.")
    pm_review_agent = config.pm_review_agent
    if pm_review_agent is None:
        raise CompositionError(
            "production PMReview runner requires [pm_review_agent] config."
        )
    implementation = pm_review_agent.implementation
    try:
        if implementation == MIROTHINKER_IMPLEMENTATION:
            if pm_review_agent.vendor_root is None:
                raise CompositionError(
                    "pm_review_agent.vendor_root is required for "
                    "implementation='mirothinker'."
                )
            from event_trader.integrations.mirothinker_pm_review import (
                MiroThinkerPMReviewRuntimeConfig,
                build_production_mirothinker_pm_review_task_runner,
            )

            return build_production_mirothinker_pm_review_task_runner(
                config=MiroThinkerPMReviewRuntimeConfig(
                    vendor_root=pm_review_agent.vendor_root,
                    log_dir=pm_review_agent.log_dir,
                    llm_provider=pm_review_agent.llm_provider,
                    llm_model_name=pm_review_agent.llm_model_name,
                    llm_api_key=pm_review_agent.llm_api_key,
                    llm_base_url=pm_review_agent.llm_base_url,
                    llm_max_context_length=pm_review_agent.llm_max_context_length,
                    llm_reasoning_effort=pm_review_agent.llm_reasoning_effort,
                    wall_clock_timeout_seconds=(
                        pm_review_agent.wall_clock_timeout_seconds
                    ),
                )
            )
        if implementation == EVENT_TRADER_IMPLEMENTATION:
            return build_event_trader_pm_review_task_runner(
                agent_config=pm_review_agent
            )
        raise CompositionError(
            "PMReview agent implementation is unavailable: "
            f"{implementation!r}."
        )
    except Exception as exc:
        raise CompositionError(
            f"failed to build production PMReview runner: {exc}"
        ) from exc


def pm_review_output_protocol_for_implementation(
    implementation: str,
) -> PMReviewOutputProtocol:
    """Return the output protocol owned by one PMReview implementation."""

    if implementation == MIROTHINKER_IMPLEMENTATION:
        return "boxed_json"
    if implementation == EVENT_TRADER_IMPLEMENTATION:
        return "structured_tool"
    raise CompositionError(
        "PMReview output protocol is unavailable for implementation "
        f"{implementation!r}."
    )


__all__ = [
    "build_event_trader_pm_review_task_runner",
    "build_pm_review_task_runner",
    "pm_review_output_protocol_for_implementation",
]
