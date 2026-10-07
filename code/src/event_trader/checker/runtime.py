"""Production implementation selection for the checker role."""

from __future__ import annotations

from collections.abc import Callable

from event_trader.agent_implementation import SINGLE_PASS_IMPLEMENTATION
from event_trader.checker.context_pack import CheckerContextPack
from event_trader.checker.single_pass_policy import SinglePassCheckerPolicy
from event_trader.checker.validator import RawCheckerDecision
from event_trader.composition_error import CompositionError
from event_trader.config import CheckerAgentConfig

type CheckerPolicy = Callable[[CheckerContextPack], RawCheckerDecision]


def build_checker_policy(*, config: CheckerAgentConfig) -> CheckerPolicy:
    """Build the configured checker implementation without changing its gates."""

    implementation = config.implementation
    if implementation != SINGLE_PASS_IMPLEMENTATION:
        raise CompositionError(
            f"checker implementation is unavailable: {implementation!r}."
        )
    return SinglePassCheckerPolicy(
        log_dir=config.log_dir,
        llm_provider=config.llm_provider,
        llm_model_name=config.llm_model_name,
        llm_api_key=config.llm_api_key,
        llm_base_url=config.llm_base_url,
        llm_reasoning_effort=config.llm_reasoning_effort,
    )


__all__ = ["CheckerPolicy", "build_checker_policy"]
