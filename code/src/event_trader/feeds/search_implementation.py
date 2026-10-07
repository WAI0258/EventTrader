"""Production implementation selection for the search-agent role."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from event_trader.agent_implementation import (
    MIROTHINKER_IMPLEMENTATION,
    SearchAgentImplementation,
)
from event_trader.config import LiveWebSearchConfig
from event_trader.feeds.web_search import SearchAgentRunner
from event_trader.feeds.web_search_backfill import SearchCollectionRunner
from event_trader.integrations.mirothinker_search import (
    MiroThinkerSearchRuntimeConfig,
    build_mirothinker_search_collection_runner,
    build_mirothinker_search_runner,
)


class SearchImplementationError(ValueError):
    """Raised when the configured search implementation cannot be built."""


@dataclass(frozen=True, slots=True)
class SearchImplementationConfig:
    """Provider-neutral inputs needed by the current search implementations."""

    implementation: SearchAgentImplementation
    vendor_root: Path | None
    workspace_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_max_context_length: int
    llm_reasoning_effort: str | None
    wall_clock_timeout_seconds: int
    serper_api_key: str
    serper_base_url: str
    jina_api_key: str
    jina_base_url: str
    summary_llm_api_key: str
    summary_llm_base_url: str
    summary_llm_model_name: str
    native_web_search_api_key: str
    native_web_search_base_url: str
    native_web_search_model: str
    native_web_search_tool_type: str
    anthropic_web_search_api_key: str
    anthropic_web_search_base_url: str
    anthropic_web_search_model: str
    anthropic_web_search_tool_type: str
    anthropic_web_search_max_uses: int
    anthropic_web_search_version: str
    acquisition_tool_names: tuple[str, ...]

    @classmethod
    def from_live_config(
        cls,
        config: LiveWebSearchConfig,
        *,
        workspace_root: Path,
        wall_clock_timeout_seconds: int = 0,
    ) -> SearchImplementationConfig:
        return cls(
            implementation=config.implementation,
            vendor_root=config.vendor_root,
            workspace_root=workspace_root,
            log_dir=config.log_dir,
            llm_provider=config.llm_provider,
            llm_model_name=config.llm_model_name,
            llm_api_key=config.llm_api_key,
            llm_base_url=config.llm_base_url,
            llm_max_context_length=config.llm_max_context_length,
            llm_reasoning_effort=config.llm_reasoning_effort,
            wall_clock_timeout_seconds=wall_clock_timeout_seconds,
            serper_api_key=config.serper_api_key,
            serper_base_url=config.serper_base_url,
            jina_api_key=config.jina_api_key,
            jina_base_url=config.jina_base_url,
            summary_llm_api_key=config.summary_llm_api_key,
            summary_llm_base_url=config.summary_llm_base_url,
            summary_llm_model_name=config.summary_llm_model_name,
            native_web_search_api_key=config.native_web_search_api_key,
            native_web_search_base_url=config.native_web_search_base_url,
            native_web_search_model=config.native_web_search_model,
            native_web_search_tool_type=config.native_web_search_tool_type,
            anthropic_web_search_api_key=config.anthropic_web_search_api_key,
            anthropic_web_search_base_url=config.anthropic_web_search_base_url,
            anthropic_web_search_model=config.anthropic_web_search_model,
            anthropic_web_search_tool_type=config.anthropic_web_search_tool_type,
            anthropic_web_search_max_uses=config.anthropic_web_search_max_uses,
            anthropic_web_search_version=config.anthropic_web_search_version,
            acquisition_tool_names=config.acquisition_tool_names,
        )


def build_search_runner(*, config: SearchImplementationConfig) -> SearchAgentRunner:
    """Build the selected live search implementation."""

    return build_mirothinker_search_runner(
        config=_build_mirothinker_config(config),
    )


def build_search_collection_runner(
    *,
    config: SearchImplementationConfig,
) -> SearchCollectionRunner:
    """Build the selected historical collection implementation."""

    return build_mirothinker_search_collection_runner(
        config=_build_mirothinker_config(config),
    )


def _build_mirothinker_config(
    config: SearchImplementationConfig,
) -> MiroThinkerSearchRuntimeConfig:
    implementation = config.implementation
    if implementation != MIROTHINKER_IMPLEMENTATION:
        raise SearchImplementationError(
            f"search implementation is unavailable: {implementation!r}."
        )
    if config.vendor_root is None:
        raise SearchImplementationError(
            "search vendor_root is required for implementation='mirothinker'."
        )
    return MiroThinkerSearchRuntimeConfig(
        vendor_root=config.vendor_root,
        workspace_root=config.workspace_root,
        log_dir=config.log_dir,
        llm_provider=config.llm_provider,
        llm_model_name=config.llm_model_name,
        llm_api_key=config.llm_api_key,
        llm_base_url=config.llm_base_url,
        llm_max_context_length=config.llm_max_context_length,
        llm_reasoning_effort=config.llm_reasoning_effort,
        wall_clock_timeout_seconds=config.wall_clock_timeout_seconds,
        serper_api_key=config.serper_api_key,
        serper_base_url=config.serper_base_url,
        jina_api_key=config.jina_api_key,
        jina_base_url=config.jina_base_url,
        summary_llm_api_key=config.summary_llm_api_key,
        summary_llm_base_url=config.summary_llm_base_url,
        summary_llm_model_name=config.summary_llm_model_name,
        native_web_search_api_key=config.native_web_search_api_key,
        native_web_search_base_url=config.native_web_search_base_url,
        native_web_search_model=config.native_web_search_model,
        native_web_search_tool_type=config.native_web_search_tool_type,
        anthropic_web_search_api_key=config.anthropic_web_search_api_key,
        anthropic_web_search_base_url=config.anthropic_web_search_base_url,
        anthropic_web_search_model=config.anthropic_web_search_model,
        anthropic_web_search_tool_type=config.anthropic_web_search_tool_type,
        anthropic_web_search_max_uses=config.anthropic_web_search_max_uses,
        anthropic_web_search_version=config.anthropic_web_search_version,
        acquisition_tool_names=config.acquisition_tool_names,
    )


__all__ = [
    "SearchImplementationConfig",
    "SearchImplementationError",
    "build_search_collection_runner",
    "build_search_runner",
]
