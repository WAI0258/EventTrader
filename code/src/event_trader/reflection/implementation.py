"""Production implementation selection for the Reflection role."""

from __future__ import annotations

from dataclasses import dataclass

from event_trader.agent_implementation import (
    EVENT_TRADER_IMPLEMENTATION,
    MIROTHINKER_IMPLEMENTATION,
)
from event_trader.composition_error import CompositionError
from event_trader.config import ReflectionAgentConfig
from event_trader.integrations.mirothinker_reflection import (
    MiroThinkerReflectionRuntimeConfig,
    build_mirothinker_open_position_reflection_runner,
    build_mirothinker_reflection_runner,
    build_mirothinker_target_close_reflection_runner,
    build_mirothinker_target_reflection_runner,
)
from event_trader.reflection.review_loop import (
    ReflectionReviewEvaluator,
    TargetCloseReflectionReviewEvaluator,
    TargetOpenPositionHorizonReflectionEvaluator,
    TargetReflectionReviewEvaluator,
)
from event_trader.reflection.supervisor import (
    ReflectionAttemptFactory,
    build_open_position_reflection_runner,
    build_reflection_runner,
    build_target_close_reflection_runner,
    build_target_reflection_runner,
)
from event_trader.reflection.workflow import (
    WorkflowReflectionRuntimeConfig,
    build_workflow_reflection_attempt_factory,
)
from event_trader.storage import WorkspaceLayout


@dataclass(frozen=True, slots=True)
class ReflectionEvaluators:
    shared: ReflectionReviewEvaluator
    target: TargetReflectionReviewEvaluator
    target_close: TargetCloseReflectionReviewEvaluator
    open_position: TargetOpenPositionHorizonReflectionEvaluator


def resolve_reflection_evaluators(
    *,
    config: ReflectionAgentConfig,
    layout: WorkspaceLayout,
    shared: ReflectionReviewEvaluator | None = None,
    target: TargetReflectionReviewEvaluator | None = None,
    target_close: TargetCloseReflectionReviewEvaluator | None = None,
    open_position: TargetOpenPositionHorizonReflectionEvaluator | None = None,
) -> ReflectionEvaluators:
    """Fill unprovided evaluator ports from the configured implementation."""

    _validate_selector_inputs(config=config, layout=layout)
    if (
        shared is not None
        and target is not None
        and target_close is not None
        and open_position is not None
    ):
        return ReflectionEvaluators(
            shared=shared,
            target=target,
            target_close=target_close,
            open_position=open_position,
        )

    implementation = config.implementation
    try:
        if implementation == MIROTHINKER_IMPLEMENTATION:
            runtime_config = _build_mirothinker_runtime_config(config)
            return ReflectionEvaluators(
                shared=(
                    shared
                    if shared is not None
                    else build_mirothinker_reflection_runner(
                        config=runtime_config,
                        layout=layout,
                    )
                ),
                target=(
                    target
                    if target is not None
                    else build_mirothinker_target_reflection_runner(
                        config=runtime_config,
                        layout=layout,
                    )
                ),
                target_close=(
                    target_close
                    if target_close is not None
                    else build_mirothinker_target_close_reflection_runner(
                        config=runtime_config,
                        layout=layout,
                    )
                ),
                open_position=(
                    open_position
                    if open_position is not None
                    else build_mirothinker_open_position_reflection_runner(
                        config=runtime_config,
                        layout=layout,
                    )
                ),
            )
        if implementation == EVENT_TRADER_IMPLEMENTATION:
            attempt_factory = _build_event_trader_attempt_factory(
                config=config,
                layout=layout,
            )
            return ReflectionEvaluators(
                shared=(
                    shared
                    if shared is not None
                    else build_reflection_runner(attempt_factory=attempt_factory)
                ),
                target=(
                    target
                    if target is not None
                    else build_target_reflection_runner(
                        attempt_factory=attempt_factory
                    )
                ),
                target_close=(
                    target_close
                    if target_close is not None
                    else build_target_close_reflection_runner(
                        attempt_factory=attempt_factory
                    )
                ),
                open_position=(
                    open_position
                    if open_position is not None
                    else build_open_position_reflection_runner(
                        attempt_factory=attempt_factory
                    )
                ),
            )
        raise CompositionError(
            f"reflection implementation is unavailable: {implementation!r}."
        )
    except CompositionError:
        raise
    except Exception as exc:
        raise CompositionError(
            f"failed to build production Reflection implementation: {exc}"
        ) from exc


def build_open_position_reflection_evaluator(
    *,
    config: ReflectionAgentConfig,
    layout: WorkspaceLayout,
) -> TargetOpenPositionHorizonReflectionEvaluator:
    """Build the configured open-position evaluator for terminal replay/catch-up."""

    _validate_selector_inputs(config=config, layout=layout)
    implementation = config.implementation
    try:
        if implementation == MIROTHINKER_IMPLEMENTATION:
            return build_mirothinker_open_position_reflection_runner(
                config=_build_mirothinker_runtime_config(config),
                layout=layout,
            )
        if implementation == EVENT_TRADER_IMPLEMENTATION:
            return build_open_position_reflection_runner(
                attempt_factory=_build_event_trader_attempt_factory(
                    config=config,
                    layout=layout,
                )
            )
        raise CompositionError(
            f"reflection implementation is unavailable: {implementation!r}."
        )
    except CompositionError:
        raise
    except Exception as exc:
        raise CompositionError(
            f"failed to build terminal Reflection implementation: {exc}"
        ) from exc


def _build_event_trader_attempt_factory(
    *,
    config: ReflectionAgentConfig,
    layout: WorkspaceLayout,
) -> ReflectionAttemptFactory:
    return build_workflow_reflection_attempt_factory(
        config=WorkflowReflectionRuntimeConfig(
            log_dir=config.log_dir,
            llm_provider=config.llm_provider,
            llm_model_name=config.llm_model_name,
            llm_api_key=config.llm_api_key,
            llm_base_url=config.llm_base_url,
            llm_max_context_length=config.llm_max_context_length,
            llm_max_output_tokens=config.llm_max_output_tokens,
            wall_clock_timeout_seconds=config.wall_clock_timeout_seconds,
        ),
        layout=layout,
    )


def _build_mirothinker_runtime_config(
    config: ReflectionAgentConfig,
) -> MiroThinkerReflectionRuntimeConfig:
    if config.vendor_root is None:
        raise CompositionError(
            "reflection_agent.vendor_root is required for "
            "implementation='mirothinker'."
        )
    return MiroThinkerReflectionRuntimeConfig(
        vendor_root=config.vendor_root,
        log_dir=config.log_dir,
        llm_provider=config.llm_provider,
        llm_model_name=config.llm_model_name,
        llm_api_key=config.llm_api_key,
        llm_base_url=config.llm_base_url,
        llm_max_context_length=config.llm_max_context_length,
    )


def _validate_selector_inputs(
    *,
    config: ReflectionAgentConfig,
    layout: WorkspaceLayout,
) -> None:
    if not isinstance(config, ReflectionAgentConfig):
        raise CompositionError("config must be a ReflectionAgentConfig instance.")
    if not isinstance(layout, WorkspaceLayout):
        raise CompositionError("layout must be a WorkspaceLayout instance.")


__all__ = [
    "ReflectionEvaluators",
    "build_open_position_reflection_evaluator",
    "resolve_reflection_evaluators",
]
