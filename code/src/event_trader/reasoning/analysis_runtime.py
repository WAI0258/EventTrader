"""Project-owned production composition for the analysis-agent implementation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from event_trader.agent_implementation import (
    EVENT_TRADER_IMPLEMENTATION,
    MIROTHINKER_IMPLEMENTATION,
)
from event_trader.analysis import AnalysisContext
from event_trader.analysis_request_handler import AnalysisCallback
from event_trader.composition_error import CompositionError
from event_trader.config import AnalysisAgentConfig
from event_trader.contracts import AnalysisResult
from event_trader.contracts.analysis_direction_policy import ExecutionDirectionMode
from event_trader.reasoning.analysis_supervisor import (
    AnalysisSupervisorConfig,
    run_analysis_supervisor,
)
from event_trader.reasoning.analysis_workflow import (
    AnalysisWorkflowConfig,
    build_analysis_workflow_executor,
)
from event_trader.storage import WorkspaceLayout


def build_analysis_runner(
    *,
    analysis_agent: AnalysisAgentConfig,
    layout: WorkspaceLayout,
    runtime_mode: Literal["live", "replay"],
    config_path: Path,
    market_data_store_root: Path | None,
    market_data_snapshot_id: str | None,
    memory_read_policy: str,
    runtime_scope: Literal["live", "replay"],
    run_id: str,
    execution_direction_mode: ExecutionDirectionMode,
) -> AnalysisCallback:
    """Build the explicitly selected production analysis runner.

    Contract enforcement, repair supervision, and ResearchMemory commit stay in
    the project-owned analysis flow; this is only the provider-selection seam.
    """

    implementation = analysis_agent.implementation
    try:
        if implementation == MIROTHINKER_IMPLEMENTATION:
            if analysis_agent.vendor_root is None:
                raise CompositionError(
                    "analysis_agent.vendor_root is required for implementation='mirothinker'."
                )
            from event_trader.integrations.mirothinker_analysis import (
                MiroThinkerAnalysisRuntimeConfig,
                build_mirothinker_analysis_runner,
            )

            return build_mirothinker_analysis_runner(
                config=MiroThinkerAnalysisRuntimeConfig(
                    vendor_root=analysis_agent.vendor_root,
                    log_dir=analysis_agent.log_dir,
                    llm_provider=analysis_agent.llm_provider,
                    llm_model_name=analysis_agent.llm_model_name,
                    llm_api_key=analysis_agent.llm_api_key,
                    llm_base_url=analysis_agent.llm_base_url,
                    llm_max_context_length=analysis_agent.llm_max_context_length,
                    runtime_mode=runtime_mode,
                    config_path=config_path,
                    market_data_store_root=market_data_store_root,
                    market_data_snapshot_id=market_data_snapshot_id,
                    llm_reasoning_effort=analysis_agent.llm_reasoning_effort,
                    wall_clock_timeout_seconds=analysis_agent.wall_clock_timeout_seconds,
                    structured_output_mode=analysis_agent.structured_output_mode,
                    structured_output_probe=analysis_agent.structured_output_probe,
                    memory_read_policy=memory_read_policy,
                    runtime_scope=runtime_scope,
                    run_id=run_id,
                    execution_direction_mode=execution_direction_mode,
                ),
                layout=layout,
            )
        if implementation == EVENT_TRADER_IMPLEMENTATION:
            return _build_event_trader_analysis_runner(
                analysis_agent=analysis_agent,
                layout=layout,
                runtime_mode=runtime_mode,
                config_path=config_path,
                market_data_store_root=market_data_store_root,
                market_data_snapshot_id=market_data_snapshot_id,
                memory_read_policy=memory_read_policy,
                runtime_scope=runtime_scope,
                run_id=run_id,
                execution_direction_mode=execution_direction_mode,
            )
        raise CompositionError(f"analysis agent implementation is unavailable: {implementation!r}.")
    except CompositionError:
        raise
    except Exception as exc:
        raise CompositionError(
            f"failed to build production Analysis implementation: {exc}"
        ) from exc


def _build_event_trader_analysis_runner(
    *,
    analysis_agent: AnalysisAgentConfig,
    layout: WorkspaceLayout,
    runtime_mode: Literal["live", "replay"],
    config_path: Path,
    market_data_store_root: Path | None,
    market_data_snapshot_id: str | None,
    memory_read_policy: str,
    runtime_scope: Literal["live", "replay"],
    run_id: str,
    execution_direction_mode: ExecutionDirectionMode,
) -> AnalysisCallback:
    if analysis_agent.structured_output_mode != "disabled":
        raise CompositionError(
            "analysis_agent.structured_output_mode must be 'disabled' for "
            "implementation='event_trader'; the bounded owner already returns the "
            "canonical structured payload."
        )
    executor = build_analysis_workflow_executor(
        config=AnalysisWorkflowConfig(
            log_dir=analysis_agent.log_dir,
            llm_provider=analysis_agent.llm_provider,
            llm_model_name=analysis_agent.llm_model_name,
            llm_api_key=analysis_agent.llm_api_key,
            llm_base_url=analysis_agent.llm_base_url,
            llm_reasoning_effort=analysis_agent.llm_reasoning_effort,
            llm_max_context_length=analysis_agent.llm_max_context_length,
            llm_max_output_tokens=analysis_agent.llm_max_output_tokens,
            wall_clock_timeout_seconds=analysis_agent.wall_clock_timeout_seconds,
            execution_direction_mode=execution_direction_mode,
        )
    )
    supervisor_config = AnalysisSupervisorConfig(
        config_path=config_path,
        runtime_mode=runtime_mode,
        runtime_scope=runtime_scope,
        run_id=run_id,
        memory_read_policy=memory_read_policy,
        structured_output_mode="disabled",
        structured_output_probe=False,
        execution_direction_mode=execution_direction_mode,
        llm_provider=analysis_agent.llm_provider,
        llm_model_name=analysis_agent.llm_model_name,
        llm_api_key=analysis_agent.llm_api_key,
        llm_base_url=analysis_agent.llm_base_url,
        market_data_store_root=market_data_store_root,
        market_data_snapshot_id=market_data_snapshot_id,
        agent_label="event-trader bounded Analysis workflow",
    )

    def analyze(context: AnalysisContext) -> AnalysisResult:
        return asyncio.run(
            run_analysis_supervisor(
                config=supervisor_config,
                layout=layout,
                context=context,
                agent_executor=executor,
            )
        )

    return analyze


__all__ = ["build_analysis_runner"]
