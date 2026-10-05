"""Typed runtime config loading for event-trader."""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Final, Literal, cast
from urllib.parse import urlparse

from event_trader.ceau.contracts import CEAUContractError
from event_trader.ceau.policy import StreamRoutingConfig
from event_trader.contracts._validators import validate_labels, validate_target_key
from event_trader.integrations.analysis_structured_finalizer import (
    AnalysisStructuredFinalizerError,
    AnalysisStructuredOutputMode,
    normalize_analysis_structured_output_mode,
)
from event_trader.integrations.mirothinker_llm_config import (
    normalize_mirothinker_reasoning_effort,
)

RuntimeMode = Literal["live", "replay"]
LiveSourceChannel = Literal["web_search", "market_news"]
LiveMarketNewsWebSocketSessionMode = Literal["rth_only"]
ValidationMarketSession = Literal["continuous", "exchange_session"]
ValidationExchangeSessionScope = Literal["regular", "extended"]
ValidationMarketDataProvider = Literal["alpaca_like", "bc_private_v1", "futu_openapi"]
ValidationExecutionDirectionMode = Literal["long_only", "long_short"]
MarketDataAdjustmentPolicy = Literal[
    "forward_adjusted_visible",
    "forward_adjusted_realized",
]
MarketDataSubscriptionProvider = Literal[
    "alpaca_like",
    "bc_private_v1",
    "futu_openapi",
    "local_archive",
]
ExecutionMode = Literal["paper"]
ExecutionPriceBasis = Literal["open", "close"]
ExecutionMissingBarPolicy = Literal["reject"]
_ALLOWED_MODES: Final[tuple[str, ...]] = ("live", "replay")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_LIVE_SOURCE_CHANNELS: Final[tuple[str, ...]] = (
    "web_search",
    "market_news",
)
_ALLOWED_VALIDATION_MARKET_SESSIONS: Final[tuple[str, ...]] = (
    "continuous",
    "exchange_session",
)
_ALLOWED_VALIDATION_EXCHANGE_SESSION_SCOPES: Final[tuple[str, ...]] = (
    "regular",
    "extended",
)
_ALLOWED_VALIDATION_EXECUTION_DIRECTION_MODES: Final[tuple[str, ...]] = (
    "long_only",
    "long_short",
)
_ALLOWED_MARKET_DATA_ADJUSTMENT_POLICIES: Final[tuple[str, ...]] = (
    "forward_adjusted_visible",
    "forward_adjusted_realized",
)
_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "workspace_root",
    "mode",
    "heartbeat_interval_seconds",
    "reflection_interval_hours",
    "reflection_lookback_hours",
    "reflection_horizons_hours",
)
_OPTIONAL_TOP_LEVEL_FIELDS: Final[tuple[str, ...]] = (
    "live",
    "live_catchup",
    "analysis_agent",
    "pm_review_agent",
    "checker_agent",
    "reflection_agent",
    "validation",
    "market_context",
    "execution",
    "pm_review_runtime",
    "runtime_workers",
    "replay_build",
    "replay_backfill",
    "artifact_retention",
    "stream_routing",
)
_MARKET_CONTEXT_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "enabled",
    "max_prompt_chars",
)
_MARKET_CONTEXT_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    "enabled",
    "max_prompt_chars",
    "target_profiles",
)
_MARKET_CONTEXT_PROFILE_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "tradable_proxy_symbol",
    "bar_granularity",
    "enabled_components",
)
_MARKET_CONTEXT_PROFILE_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    "tradable_proxy_symbol",
    "derivatives_underlying_symbol",
    "bar_granularity",
    "market_data_subscriptions",
    "enabled_components",
    "price_volume",
    "market_path",
    "technical",
    "derivatives",
    "macro_cross_asset",
)
_MARKET_CONTEXT_MARKET_PATH_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    "enabled",
    "session",
    "range_lookback_calendar_days",
    "range_lookback_sessions",
    "range_lookback_daily_bars",
    "atr_period",
    "atr_multiple",
    "pct_floor",
    "min_leg_bars",
    "large_move_pct",
    "near_extreme_pct",
)
_MARKET_CONTEXT_PRICE_VOLUME_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "lookback_bars",
    "trailing_return_windows",
)
_MARKET_CONTEXT_TECHNICAL_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "ema_periods",
    "ichimoku_enabled",
)
_MARKET_CONTEXT_TECHNICAL_OPTIONAL_FIELDS: Final[tuple[str, ...]] = (
    "bollinger_period",
    "bollinger_stddev",
    "rsi_period",
    "rsi_overbought",
    "rsi_oversold",
    "divergence_lookback_bars",
    "divergence_pivot_window",
)
_MARKET_CONTEXT_TECHNICAL_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    *_MARKET_CONTEXT_TECHNICAL_REQUIRED_FIELDS,
    *_MARKET_CONTEXT_TECHNICAL_OPTIONAL_FIELDS,
)
_MARKET_CONTEXT_DERIVATIVES_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "enabled",
    "data_feed",
    "chain_max_rows",
    "max_contracts_per_snapshot",
    "expiry_days_min",
    "expiry_days_max",
    "strike_pct_window",
    "atm_contract_count",
    "otm_contract_count",
    "min_trade_count",
    "large_trade_notional_threshold",
    "include_historical_trades",
    "include_historical_bars",
    "include_latest_snapshot_quotes",
    "bars_granularity",
    "lookback_hours",
)
_MARKET_CONTEXT_DERIVATIVES_OPTIONAL_FIELDS: Final[tuple[str, ...]] = (
    "include_latest_trades",
    "include_latest_quotes",
    "historical_universe_reconstruction_enabled",
    "historical_universe_max_candidates_per_bucket",
    "historical_universe_strike_step",
    "unusual_baseline_windows_days",
    "unusual_min_baseline_observations",
    "unusual_elevated_ratio",
    "unusual_unusual_ratio",
    "unusual_extreme_ratio",
    "notable_contract_limit",
)
_MARKET_CONTEXT_DERIVATIVES_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    *_MARKET_CONTEXT_DERIVATIVES_REQUIRED_FIELDS,
    *_MARKET_CONTEXT_DERIVATIVES_OPTIONAL_FIELDS,
)
_MARKET_CONTEXT_MACRO_CROSS_ASSET_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "enabled",
    "lookback_bars",
    "trailing_return_windows",
    "proxy_groups",
)
_MARKET_CONTEXT_MACRO_CROSS_ASSET_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    *_MARKET_CONTEXT_MACRO_CROSS_ASSET_REQUIRED_FIELDS,
    "group_pressure_rules",
)
_MARKET_CONTEXT_MACRO_CROSS_ASSET_PRESSURE_RULES: Final[tuple[str, ...]] = (
    "positive_supportive",
    "positive_headwind",
    "contextual",
)
_ALLOWED_MARKET_CONTEXT_COMPONENTS: Final[tuple[str, ...]] = (
    "price_volume",
    "technical",
    "derivatives",
    "macro_cross_asset",
)
_MARKET_CONTEXT_GRANULARITY_RE: Final[re.Pattern[str]] = re.compile(
    r"^[1-9][0-9]*(m|min|h|d)$",
    re.IGNORECASE,
)
_ALLOWED_MARKET_CONTEXT_RETURN_WINDOWS: Final[tuple[str, ...]] = (
    "1h",
    "4h",
    "1d",
    "5d",
    "20d",
)
_LIVE_REQUIRED_FIELDS: Final[tuple[str, ...]] = ("enabled_channels",)
_LIVE_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    "enabled_channels",
    "web_search",
    "market_news",
    "market_data",
)
_LIVE_CATCHUP_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    "web_search",
    "market_news",
)
_ANALYSIS_AGENT_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "vendor_root",
    "log_dir",
    "llm_provider",
    "llm_model_name",
    "llm_api_key_env",
    "llm_base_url",
)
_CHECKER_AGENT_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "log_dir",
    "llm_provider",
    "llm_model_name",
    "llm_api_key_env",
    "llm_base_url",
)
_CHECKER_AGENT_OPTIONAL_FIELDS: Final[tuple[str, ...]] = (
    "llm_reasoning_effort",
    "evidence_excerpt_chars",
    "section_excerpt_chars",
    "recent_timeline_items",
    "max_pack_chars",
    "force_escalate_on_insufficient_coverage",
)
_ANALYSIS_AGENT_OPTIONAL_FIELDS: Final[tuple[str, ...]] = (
    "llm_reasoning_effort",
    "llm_max_context_length",
    "wall_clock_timeout_seconds",
    "structured_output_mode",
    "structured_output_probe",
)
_PM_REVIEW_AGENT_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "vendor_root",
    "log_dir",
    "llm_provider",
    "llm_model_name",
    "llm_api_key_env",
    "llm_base_url",
)
_PM_REVIEW_AGENT_OPTIONAL_FIELDS: Final[tuple[str, ...]] = (
    "llm_reasoning_effort",
    "llm_max_context_length",
    "wall_clock_timeout_seconds",
)
_REFLECTION_AGENT_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "vendor_root",
    "log_dir",
    "llm_provider",
    "llm_model_name",
    "llm_api_key_env",
    "llm_base_url",
)
_REFLECTION_AGENT_OPTIONAL_FIELDS: Final[tuple[str, ...]] = ("llm_max_context_length",)
_VALIDATION_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "market_data",
    "market_mappings",
)
_VALIDATION_MARKET_DATA_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "provider",
    "base_url",
    "timeout_seconds",
)
_VALIDATION_MARKET_DATA_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    *_VALIDATION_MARKET_DATA_REQUIRED_FIELDS,
    "api_key_env",
    "local_archive_root",
)
_VALIDATION_MARKET_MAPPING_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "market_symbol",
    "market_session",
    "bar_granularity",
)
_VALIDATION_MARKET_MAPPING_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    "market_symbol",
    "market_session",
    "exchange",
    "exchange_session_scope",
    "bar_granularity",
    "execution_direction_mode",
    "adjustment_policy",
)
_WEB_SEARCH_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "targets",
    "cadence_profile",
    "vendor_root",
    "log_dir",
    "llm_provider",
    "llm_model_name",
    "llm_api_key_env",
    "llm_base_url",
)
_WEB_SEARCH_OPTIONAL_FIELDS: Final[tuple[str, ...]] = (
    "cadence_profiles",
    "acquisition_tool_names",
    "llm_reasoning_effort",
    "llm_max_context_length",
    "serper_api_key_env",
    "serper_base_url",
    "jina_api_key_env",
    "jina_base_url",
    "summary_llm_api_key_env",
    "summary_llm_base_url",
    "summary_llm_model_name",
    "native_web_search_api_key_env",
    "native_web_search_base_url",
    "native_web_search_model",
    "native_web_search_tool_type",
    "anthropic_web_search_api_key_env",
    "anthropic_web_search_base_url",
    "anthropic_web_search_model",
    "anthropic_web_search_tool_type",
    "anthropic_web_search_max_uses",
    "anthropic_web_search_version",
)
_WEB_SEARCH_TARGET_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "target_key",
    "search_intent",
    "prompt_profile_id",
    "control_language",
    "retrieval_languages",
)
_MARKET_NEWS_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "targets",
)
_MARKET_NEWS_REST_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "enabled",
    "base_url",
    "api_key_env",
    "cadence_profile",
    "limit",
    "include_content",
    "exclude_contentless",
    "soft_fail",
)
_LIVE_CATCHUP_WEB_SEARCH_REQUIRED_FIELDS: Final[tuple[str, ...]] = ("max_slice_hours",)
_LIVE_CATCHUP_MARKET_NEWS_REQUIRED_FIELDS: Final[tuple[str, ...]] = ("max_slice_hours",)
_MARKET_NEWS_REST_OPTIONAL_FIELDS: Final[tuple[str, ...]] = (
    "cadence_profiles",
)
_MARKET_NEWS_OPTIONAL_FIELDS: Final[tuple[str, ...]] = (
    "rest",
    "websocket",
)
_MARKET_NEWS_WEBSOCKET_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "enabled",
    "provider",
    "url",
    "token_env",
    "tickers",
    "soft_fail",
    "reconnect_seconds_min",
    "reconnect_seconds_max",
    "heartbeat_seconds",
    "session",
)
_MARKET_NEWS_WEBSOCKET_SESSION_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "mode",
    "timezone",
    "calendar",
    "open_buffer_minutes",
    "close_buffer_minutes",
)
_MARKET_NEWS_TARGET_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "target_key",
    "symbols",
)
_MARKET_NEWS_TARGET_OPTIONAL_FIELDS: Final[tuple[str, ...]] = (
    "labels",
)
_MARKET_NEWS_MAX_LIMIT: Final[int] = 50
_MARKET_NEWS_REST_ALLOWED_CADENCE_PROFILES: Final[tuple[str, ...]] = (
    "us_equity_news",
    "cn_a_share_news",
)
_MARKET_NEWS_REST_CADENCE_PROFILE_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "timezone",
    "calendar",
    "rth_local_times",
    "trading_day_off_rth_local_times",
    "closed_day_local_times",
)
_MARKET_NEWS_WEBSOCKET_SESSION_MODES: Final[tuple[str, ...]] = ("rth_only",)
_LIVE_MARKET_DATA_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "enabled",
    "targets",
    "canonical_bar_granularity",
    "warmup",
)
_LIVE_MARKET_DATA_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    *_LIVE_MARKET_DATA_REQUIRED_FIELDS,
)
_LIVE_MARKET_DATA_TARGET_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "target_key",
    "subscriptions",
)
_LIVE_MARKET_DATA_SUBSCRIPTION_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "symbol",
    "provider",
    "purpose",
    "market_session",
    "exchange",
    "exchange_session_scope",
    "bar_granularity",
)
_LIVE_MARKET_DATA_SUBSCRIPTION_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    *_LIVE_MARKET_DATA_SUBSCRIPTION_REQUIRED_FIELDS,
    "adjustment_policy",
)
_LIVE_MARKET_DATA_WARMUP_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "enabled",
    "min_lookback_days",
)
_WEB_SEARCH_ALLOWED_CADENCE_PROFILES: Final[tuple[str, ...]] = (
    "us_equity_active",
    "cn_a_share_active",
)
_WEB_SEARCH_CADENCE_PROFILE_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "timezone",
    "trading_day_local_times",
    "closed_day_local_times",
)
_DEFAULT_WEB_SEARCH_PROFILE_TIMEZONE: Final[str] = "America/New_York"
_DEFAULT_WEB_SEARCH_TRADING_DAY_LOCAL_TIMES: Final[tuple[str, ...]] = (
    "08:00",
    "10:15",
    "12:30",
    "15:30",
    "17:15",
)
_DEFAULT_WEB_SEARCH_CLOSED_DAY_LOCAL_TIMES: Final[tuple[str, ...]] = (
    "10:30",
    "18:00",
)
_WEB_SEARCH_ALLOWED_ACQUISITION_TOOL_NAMES: Final[tuple[str, ...]] = (
    "search_and_scrape_webpage",
    "jina_scrape_llm_summary",
    "event_trader_openai_web_search",
    "event_trader_anthropic_web_search",
)
_ARTIFACT_RETENTION_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "analysis_debug_log_days",
    "pm_review_debug_log_days",
    "checker_debug_log_days",
    "search_debug_log_days",
    "reflection_debug_log_days",
)
_DEFAULT_CHECKER_EVIDENCE_EXCERPT_CHARS: Final[int] = 4_000
_DEFAULT_CHECKER_SECTION_EXCERPT_CHARS: Final[int] = 2_000
_DEFAULT_CHECKER_RECENT_TIMELINE_ITEMS: Final[int] = 10
_DEFAULT_CHECKER_MAX_PACK_CHARS: Final[int] = 16_000
_DEFAULT_CHECKER_FORCE_ESCALATE_ON_INSUFFICIENT_COVERAGE: Final[bool] = True
_DEFAULT_ANALYSIS_AGENT_WALL_CLOCK_TIMEOUT_SECONDS: Final[int] = 0
_DEFAULT_AGENT_LLM_MAX_CONTEXT_LENGTH: Final[int] = 204_800
_DEFAULT_WEB_SEARCH_LLM_MAX_CONTEXT_LENGTH: Final[int] = 65_536
_EXECUTION_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    "mode",
    "price_basis",
    "buy_cost_bps",
    "sell_cost_bps",
    "target_cost_overrides",
    "slippage_bps",
    "commission_bps",
    "missing_bar_policy",
    "max_next_bar_wait_hours",
)
_DEFAULT_EXECUTION_MODE: Final[ExecutionMode] = "paper"
_DEFAULT_EXECUTION_PRICE_BASIS: Final[ExecutionPriceBasis] = "open"
_DEFAULT_EXECUTION_BUY_COST_BPS: Final[float] = 0.0
_DEFAULT_EXECUTION_SELL_COST_BPS: Final[float] = 0.0
_DEFAULT_EXECUTION_SLIPPAGE_BPS: Final[float] = 0.0
_DEFAULT_EXECUTION_COMMISSION_BPS: Final[float] = 0.0
_DEFAULT_EXECUTION_MISSING_BAR_POLICY: Final[ExecutionMissingBarPolicy] = "reject"
_DEFAULT_EXECUTION_MAX_NEXT_BAR_WAIT_HOURS: Final[int] = 72
_EXECUTION_TARGET_COST_OVERRIDE_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    "buy_cost_bps",
    "sell_cost_bps",
    "slippage_bps",
    "commission_bps",
)
_PM_REVIEW_RUNTIME_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    "auto_dispatch_after_analysis",
    "execute_pm_decisions",
    "require_workspace_ready",
    "position_review_gate_enabled",
    "position_review_cooldown_minutes",
    "position_review_rearm_buffer_bps",
)
_RUNTIME_WORKERS_ALLOWED_FIELDS: Final[tuple[str, ...]] = (
    "checker_concurrency",
    "analysis_concurrency",
    "pm_review_concurrency",
)
_DEFAULT_PM_REVIEW_AUTO_DISPATCH_AFTER_ANALYSIS: Final[bool] = False
_DEFAULT_PM_REVIEW_EXECUTE_PM_DECISIONS: Final[bool] = False
_DEFAULT_PM_REVIEW_REQUIRE_WORKSPACE_READY: Final[bool] = True
_DEFAULT_PM_POSITION_REVIEW_GATE_ENABLED: Final[bool] = False
_DEFAULT_PM_POSITION_REVIEW_COOLDOWN_MINUTES: Final[int] = 60
_DEFAULT_PM_POSITION_REVIEW_REARM_BUFFER_BPS: Final[float] = 25.0
_DEFAULT_RUNTIME_WORKER_CONCURRENCY: Final[int] = 1


class BootstrapConfigError(ValueError):
    """Raised when bootstrap configuration cannot be loaded or validated."""


@dataclass(frozen=True, slots=True)
class LiveWebSearchTargetConfig:
    """Validated per-target live web-search intent."""

    target_key: str
    search_intent: str
    prompt_profile_id: str
    control_language: str
    retrieval_languages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LiveWebSearchCadenceProfileConfig:
    """Validated business-time cadence pins for a named live profile."""

    timezone: str
    trading_day_local_times: tuple[str, ...]
    closed_day_local_times: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LiveWebSearchConfig:
    """Validated shared live web-search runtime configuration."""

    targets: tuple[LiveWebSearchTargetConfig, ...]
    cadence_profile: str
    cadence_profile_config: LiveWebSearchCadenceProfileConfig
    vendor_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_reasoning_effort: str | None
    llm_max_context_length: int
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


@dataclass(frozen=True, slots=True)
class LiveMarketNewsTargetConfig:
    """Validated per-target market-news symbol basket."""

    target_key: str
    symbols: tuple[str, ...]
    labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LiveMarketNewsRestCadenceProfileConfig:
    """Validated three-bucket business cadence for live market-news REST polling."""

    timezone: str
    calendar: str
    rth_local_times: tuple[str, ...]
    trading_day_off_rth_local_times: tuple[str, ...]
    closed_day_local_times: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LiveMarketNewsWebSocketSessionConfig:
    """Validated market-hours policy for realtime market-news WebSocket sessions."""

    mode: LiveMarketNewsWebSocketSessionMode
    timezone: str
    calendar: str
    open_buffer_minutes: int
    close_buffer_minutes: int


@dataclass(frozen=True, slots=True)
class LiveMarketNewsConfig:
    """Validated shared live market-news runtime configuration."""

    targets: tuple[LiveMarketNewsTargetConfig, ...]
    rest: LiveMarketNewsRestConfig | None = None
    websocket: LiveMarketNewsWebSocketConfig | None = None


@dataclass(frozen=True, slots=True)
class LiveMarketNewsRestConfig:
    """Validated optional live REST market-news polling configuration."""

    enabled: bool
    base_url: str
    api_key: str
    cadence_profile: str
    cadence_profile_config: LiveMarketNewsRestCadenceProfileConfig
    limit: int
    include_content: bool
    exclude_contentless: bool
    soft_fail: bool


@dataclass(frozen=True, slots=True)
class LiveCatchupWebSearchConfig:
    """Validated historical web-search slice controls for live catch-up."""

    max_slice_hours: int


@dataclass(frozen=True, slots=True)
class LiveCatchupMarketNewsConfig:
    """Validated historical market-news slice controls for live catch-up."""

    max_slice_hours: int


@dataclass(frozen=True, slots=True)
class LiveCatchupConfig:
    """Validated historical acquisition controls owned by live_catchup."""

    web_search: LiveCatchupWebSearchConfig | None = None
    market_news: LiveCatchupMarketNewsConfig | None = None


@dataclass(frozen=True, slots=True)
class LiveMarketNewsWebSocketConfig:
    """Validated optional realtime market-news WebSocket configuration."""

    enabled: bool
    provider: str
    url: str
    token: str
    tickers: tuple[str, ...]
    soft_fail: bool
    reconnect_seconds_min: int
    reconnect_seconds_max: int
    heartbeat_seconds: int
    session: LiveMarketNewsWebSocketSessionConfig


@dataclass(frozen=True, slots=True)
class MarketDataSubscriptionConfig:
    """Validated explicit market-data subscription entry."""

    symbol: str
    provider: MarketDataSubscriptionProvider
    purpose: str
    market_session: ValidationMarketSession
    exchange: str
    exchange_session_scope: ValidationExchangeSessionScope
    bar_granularity: str
    adjustment_policy: MarketDataAdjustmentPolicy | None = None


@dataclass(frozen=True, slots=True)
class LiveMarketDataTargetConfig:
    """Validated live market-data target subscription."""

    target_key: str
    subscriptions: tuple[MarketDataSubscriptionConfig, ...]


@dataclass(frozen=True, slots=True)
class LiveMarketDataWarmupConfig:
    """Validated live market-data historical warmup settings."""

    enabled: bool
    min_lookback_days: int


@dataclass(frozen=True, slots=True)
class LiveMarketDataConfig:
    """Validated deterministic live market-data substrate configuration."""

    enabled: bool
    targets: tuple[LiveMarketDataTargetConfig, ...]
    canonical_bar_granularity: str
    warmup: LiveMarketDataWarmupConfig


@dataclass(frozen=True, slots=True)
class LiveConfig:
    """Validated live runtime channel enablement with per-channel config where present."""

    enabled_channels: tuple[LiveSourceChannel, ...]
    web_search: LiveWebSearchConfig | None = None
    market_news: LiveMarketNewsConfig | None = None
    market_data: LiveMarketDataConfig | None = None


@dataclass(frozen=True, slots=True)
class AnalysisAgentConfig:
    """Validated MiroThinker analysis runtime configuration."""

    vendor_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_reasoning_effort: str | None
    llm_max_context_length: int
    wall_clock_timeout_seconds: int
    structured_output_mode: AnalysisStructuredOutputMode = "disabled"
    structured_output_probe: bool = True


@dataclass(frozen=True, slots=True)
class CheckerAgentConfig:
    """Validated checker runtime configuration."""

    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_reasoning_effort: str | None
    evidence_excerpt_chars: int
    section_excerpt_chars: int
    recent_timeline_items: int
    max_pack_chars: int
    force_escalate_on_insufficient_coverage: bool


@dataclass(frozen=True, slots=True)
class ReflectionAgentConfig:
    """Validated reflection runtime configuration."""

    vendor_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_max_context_length: int


@dataclass(frozen=True, slots=True)
class PMReviewAgentConfig:
    """Validated PM review runtime configuration."""

    vendor_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_reasoning_effort: str | None
    llm_max_context_length: int
    wall_clock_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class _AgentRuntimeConfigValues:
    """Validated shared agent runtime config fields."""

    vendor_root: Path
    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_reasoning_effort: str | None
    llm_max_context_length: int


@dataclass(frozen=True, slots=True)
class ValidationMarketDataConfig:
    """Validated deterministic market-data API configuration for validation."""

    provider: ValidationMarketDataProvider
    base_url: str
    api_key: str
    timeout_seconds: float
    local_archive_root: Path | None = None


@dataclass(frozen=True, slots=True)
class ValidationMarketMappingConfig:
    """Validated target-level market mapping configuration for validation."""

    market_symbol: str
    market_session: ValidationMarketSession
    exchange: str | None
    bar_granularity: str
    execution_direction_mode: ValidationExecutionDirectionMode = "long_short"
    exchange_session_scope: ValidationExchangeSessionScope | None = None
    adjustment_policy: MarketDataAdjustmentPolicy | None = None


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    """Validated market seam configuration for deterministic validation."""

    market_data: ValidationMarketDataConfig
    market_mappings: dict[str, ValidationMarketMappingConfig]


@dataclass(frozen=True, slots=True)
class MarketContextPriceVolumeConfig:
    """Validated price/volume context profile settings."""

    lookback_bars: int
    trailing_return_windows: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketContextMarketPathConfig:
    """Validated market path detector settings exposed to operators."""

    enabled: bool = True
    session: ValidationMarketSession | None = None
    range_lookback_calendar_days: int = 20
    range_lookback_sessions: int = 20
    range_lookback_daily_bars: int = 60
    atr_period: int = 14
    atr_multiple: float = 2.0
    pct_floor: float = 0.025
    min_leg_bars: int = 3
    large_move_pct: float = 0.08
    near_extreme_pct: float = 0.03


@dataclass(frozen=True, slots=True)
class MarketContextTechnicalConfig:
    """Validated deterministic technical context profile settings."""

    ema_periods: tuple[int, ...]
    ichimoku_enabled: bool
    bollinger_period: int = 20
    bollinger_stddev: float = 2.0
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    divergence_lookback_bars: int = 50
    divergence_pivot_window: int = 2


@dataclass(frozen=True, slots=True)
class MarketContextDerivativesConfig:
    """Validated deterministic derivatives context profile settings."""

    enabled: bool
    data_feed: str
    chain_max_rows: int
    max_contracts_per_snapshot: int
    expiry_days_min: int
    expiry_days_max: int
    strike_pct_window: float
    atm_contract_count: int
    otm_contract_count: int
    min_trade_count: int
    large_trade_notional_threshold: float
    include_historical_trades: bool
    include_historical_bars: bool
    include_latest_snapshot_quotes: bool
    bars_granularity: str
    lookback_hours: int
    include_latest_trades: bool = True
    include_latest_quotes: bool = True
    historical_universe_reconstruction_enabled: bool = True
    historical_universe_max_candidates_per_bucket: int = 300
    historical_universe_strike_step: float = 1.0
    unusual_baseline_windows_days: tuple[int, ...] = (7, 14)
    unusual_min_baseline_observations: int = 3
    unusual_elevated_ratio: float = 2.0
    unusual_unusual_ratio: float = 5.0
    unusual_extreme_ratio: float = 10.0
    notable_contract_limit: int = 5


@dataclass(frozen=True, slots=True)
class MarketContextMacroCrossAssetConfig:
    """Validated deterministic macro cross-asset context profile settings."""

    enabled: bool
    lookback_bars: int
    trailing_return_windows: tuple[str, ...]
    proxy_groups: dict[str, tuple[str, ...]]
    group_pressure_rules: dict[str, str]


@dataclass(frozen=True, slots=True)
class MarketContextTargetProfileConfig:
    """Validated target-level market-context profile."""

    tradable_proxy_symbol: str
    derivatives_underlying_symbol: str | None
    bar_granularity: str
    market_data_subscriptions: tuple[MarketDataSubscriptionConfig, ...]
    enabled_components: tuple[str, ...]
    price_volume: MarketContextPriceVolumeConfig | None
    market_path: MarketContextMarketPathConfig
    technical: MarketContextTechnicalConfig | None
    derivatives: MarketContextDerivativesConfig | None
    macro_cross_asset: MarketContextMacroCrossAssetConfig | None


@dataclass(frozen=True, slots=True)
class MarketContextConfig:
    """Validated shared live/replay market-context configuration."""

    enabled: bool
    max_prompt_chars: int
    target_profiles: dict[str, MarketContextTargetProfileConfig]


@dataclass(frozen=True, slots=True)
class ArtifactRetentionConfig:
    """Validated non-truth artifact retention horizons."""

    analysis_debug_log_days: int
    pm_review_debug_log_days: int
    checker_debug_log_days: int
    search_debug_log_days: int
    reflection_debug_log_days: int


@dataclass(frozen=True, slots=True)
class ExecutionTargetCostOverrideConfig:
    """Validated target-specific paper execution cost override."""

    buy_cost_bps: float
    sell_cost_bps: float


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Validated paper execution configuration."""

    mode: ExecutionMode
    price_basis: ExecutionPriceBasis
    buy_cost_bps: float
    sell_cost_bps: float
    target_cost_overrides: dict[str, ExecutionTargetCostOverrideConfig]
    missing_bar_policy: ExecutionMissingBarPolicy
    max_next_bar_wait_hours: int


@dataclass(frozen=True, slots=True)
class PMReviewRuntimeConfig:
    """Validated PMReview runtime routing policy."""

    auto_dispatch_after_analysis: bool
    execute_pm_decisions: bool
    require_workspace_ready: bool
    position_review_gate_enabled: bool
    position_review_cooldown_minutes: int
    position_review_rearm_buffer_bps: float


@dataclass(frozen=True, slots=True)
class RuntimeWorkersConfig:
    """Validated runtime worker-lane concurrency configuration."""

    checker_concurrency: int
    analysis_concurrency: int
    pm_review_concurrency: int


def _default_execution_config() -> ExecutionConfig:
    return ExecutionConfig(
        mode=_DEFAULT_EXECUTION_MODE,
        price_basis=_DEFAULT_EXECUTION_PRICE_BASIS,
        buy_cost_bps=_DEFAULT_EXECUTION_BUY_COST_BPS,
        sell_cost_bps=_DEFAULT_EXECUTION_SELL_COST_BPS,
        target_cost_overrides={},
        missing_bar_policy=_DEFAULT_EXECUTION_MISSING_BAR_POLICY,
        max_next_bar_wait_hours=_DEFAULT_EXECUTION_MAX_NEXT_BAR_WAIT_HOURS,
    )


def _default_pm_review_runtime_config() -> PMReviewRuntimeConfig:
    return PMReviewRuntimeConfig(
        auto_dispatch_after_analysis=_DEFAULT_PM_REVIEW_AUTO_DISPATCH_AFTER_ANALYSIS,
        execute_pm_decisions=_DEFAULT_PM_REVIEW_EXECUTE_PM_DECISIONS,
        require_workspace_ready=_DEFAULT_PM_REVIEW_REQUIRE_WORKSPACE_READY,
        position_review_gate_enabled=_DEFAULT_PM_POSITION_REVIEW_GATE_ENABLED,
        position_review_cooldown_minutes=(
            _DEFAULT_PM_POSITION_REVIEW_COOLDOWN_MINUTES
        ),
        position_review_rearm_buffer_bps=(
            _DEFAULT_PM_POSITION_REVIEW_REARM_BUFFER_BPS
        ),
    )


def _default_runtime_workers_config() -> RuntimeWorkersConfig:
    return RuntimeWorkersConfig(
        checker_concurrency=_DEFAULT_RUNTIME_WORKER_CONCURRENCY,
        analysis_concurrency=_DEFAULT_RUNTIME_WORKER_CONCURRENCY,
        pm_review_concurrency=_DEFAULT_RUNTIME_WORKER_CONCURRENCY,
    )


@dataclass(frozen=True, slots=True)
class KernelConfigAuditIdentity:
    """Generic config identity surface for run manifests and audit reports."""

    config_path: Path
    config_hash: str | None
    mode: RuntimeMode | None = None
    workspace_root: Path | None = None
    stream_routing_policy_version: str | None = None
    stream_routing_policy_status: str | None = None
    stream_routing_policy_hash: str | None = None
    stream_routing_payload: Mapping[str, object] | None = None
    load_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "config_path",
            _ensure_audit_path(self.config_path, "config_path"),
        )
        if self.config_hash is not None:
            object.__setattr__(
                self,
                "config_hash",
                _validate_audit_sha256(self.config_hash, "config_hash"),
            )
        if self.mode is not None and self.mode not in {"replay", "live"}:
            raise BootstrapConfigError("config audit mode must be replay or live when present.")
        if self.workspace_root is not None:
            object.__setattr__(
                self,
                "workspace_root",
                _ensure_audit_path(self.workspace_root, "workspace_root"),
            )
        if self.stream_routing_policy_version is not None:
            object.__setattr__(
                self,
                "stream_routing_policy_version",
                _validate_audit_text(
                    self.stream_routing_policy_version,
                    "stream_routing.policy_version",
                ),
            )
        if self.stream_routing_policy_status is not None:
            object.__setattr__(
                self,
                "stream_routing_policy_status",
                _validate_audit_text(
                    self.stream_routing_policy_status,
                    "stream_routing.policy_status",
                ),
            )
        if self.stream_routing_policy_hash is not None:
            object.__setattr__(
                self,
                "stream_routing_policy_hash",
                _validate_audit_sha256(
                    self.stream_routing_policy_hash,
                    "stream_routing.policy_hash",
                ),
            )
        if self.stream_routing_payload is not None:
            if not isinstance(self.stream_routing_payload, Mapping):
                raise BootstrapConfigError("stream_routing.payload must be a JSON object.")
            object.__setattr__(
                self,
                "stream_routing_payload",
                dict(self.stream_routing_payload),
            )
        if self.load_error is not None:
            object.__setattr__(
                self,
                "load_error",
                _validate_audit_text(self.load_error, "load_error"),
            )

    def to_json_payload(self) -> dict[str, object]:
        stream_routing: dict[str, object] | None = None
        if (
            self.stream_routing_policy_version is not None
            or self.stream_routing_policy_status is not None
            or self.stream_routing_policy_hash is not None
            or self.stream_routing_payload is not None
        ):
            stream_routing = {
                "policy_version": self.stream_routing_policy_version,
                "policy_status": self.stream_routing_policy_status,
                "policy_hash": self.stream_routing_policy_hash,
                "payload": (
                    None
                    if self.stream_routing_payload is None
                    else dict(self.stream_routing_payload)
                ),
            }
        return {
            "config_path": str(self.config_path),
            "config_hash": self.config_hash,
            "mode": self.mode,
            "workspace_root": None if self.workspace_root is None else str(self.workspace_root),
            "stream_routing": stream_routing,
            "load_error": self.load_error,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> KernelConfigAuditIdentity:
        data = _require_audit_mapping(payload, "config_audit")
        stream_routing = data.get("stream_routing")
        stream_payload: Mapping[str, object] | None = None
        stream_policy_version: str | None = None
        stream_policy_status: str | None = None
        stream_policy_hash: str | None = None
        if stream_routing is not None:
            stream_data = _require_audit_mapping(stream_routing, "stream_routing")
            stream_policy_version = _optional_audit_text(stream_data, "policy_version")
            stream_policy_status = _optional_audit_text(stream_data, "policy_status")
            stream_policy_hash = _optional_audit_text(stream_data, "policy_hash")
            raw_payload = stream_data.get("payload")
            if raw_payload is not None:
                stream_payload = _require_audit_mapping(raw_payload, "stream_routing.payload")
        return cls(
            config_path=Path(_require_audit_text(data, "config_path")),
            config_hash=_optional_audit_text(data, "config_hash"),
            mode=cast(RuntimeMode | None, _optional_audit_text(data, "mode")),
            workspace_root=(
                None
                if data.get("workspace_root") is None
                else Path(_require_audit_text(data, "workspace_root"))
            ),
            stream_routing_policy_version=stream_policy_version,
            stream_routing_policy_status=stream_policy_status,
            stream_routing_policy_hash=stream_policy_hash,
            stream_routing_payload=stream_payload,
            load_error=_optional_audit_text(data, "load_error"),
        )


@dataclass(frozen=True, slots=True)
class KernelConfig:
    """Validated configuration required for the event-trader runtime."""

    config_path: Path
    workspace_root: Path
    mode: RuntimeMode
    heartbeat_interval_seconds: float
    reflection_interval_hours: float
    reflection_lookback_hours: float
    reflection_horizons_hours: tuple[float, ...]
    live: LiveConfig | None = None
    live_catchup: LiveCatchupConfig | None = None
    analysis_agent: AnalysisAgentConfig | None = None
    pm_review_agent: PMReviewAgentConfig | None = None
    checker_agent: CheckerAgentConfig | None = None
    reflection_agent: ReflectionAgentConfig | None = None
    validation: ValidationConfig | None = None
    market_context: MarketContextConfig | None = None
    execution: ExecutionConfig = field(default_factory=_default_execution_config)
    pm_review_runtime: PMReviewRuntimeConfig = field(
        default_factory=_default_pm_review_runtime_config
    )
    runtime_workers: RuntimeWorkersConfig = field(
        default_factory=_default_runtime_workers_config
    )
    artifact_retention: ArtifactRetentionConfig | None = None
    stream_routing: StreamRoutingConfig | None = None
    config_hash: str = ""

    def to_audit_identity(self) -> KernelConfigAuditIdentity:
        stream_payload = (
            None
            if self.stream_routing is None
            else self.stream_routing.to_json_payload()
        )
        return KernelConfigAuditIdentity(
            config_path=self.config_path,
            config_hash=self.config_hash or None,
            mode=self.mode,
            workspace_root=self.workspace_root,
            stream_routing_policy_version=(
                None if self.stream_routing is None else self.stream_routing.policy_version
            ),
            stream_routing_policy_status=(
                None if self.stream_routing is None else self.stream_routing.policy_status
            ),
            stream_routing_policy_hash=(
                None if self.stream_routing is None else self.stream_routing.policy_hash
            ),
            stream_routing_payload=stream_payload,
        )

    def to_audit_payload(self) -> dict[str, object]:
        """Stable config identity payload for run manifests and audit reports."""
        return self.to_audit_identity().to_json_payload()


def load_kernel_config(config_path: str | Path) -> KernelConfig:
    """Load and validate the runtime config contract."""
    path = Path(config_path).expanduser()
    _load_dotenv_from_cwd()

    try:
        with path.open("rb") as config_file:
            raw_config = tomllib.load(config_file)
    except FileNotFoundError as exc:
        raise BootstrapConfigError(f"Config file does not exist: {path}") from exc
    except PermissionError as exc:
        raise BootstrapConfigError(f"Config file is not readable: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BootstrapConfigError(
            f"Config file contains invalid TOML: {path}: {exc}"
        ) from exc
    except OSError as exc:
        raise BootstrapConfigError(f"Could not read config file {path}: {exc}") from exc

    if not isinstance(raw_config, dict):
        raise BootstrapConfigError("Config file must decode to a TOML table.")

    missing_fields = [field for field in _REQUIRED_FIELDS if field not in raw_config]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(f"Missing required config fields: {missing}")

    extra_fields = sorted(
        set(raw_config) - set(_REQUIRED_FIELDS) - set(_OPTIONAL_TOP_LEVEL_FIELDS)
    )
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(f"Unknown config fields: {unknown}")

    config = KernelConfig(
        config_path=path.resolve(strict=False),
        workspace_root=_validate_workspace_root(raw_config["workspace_root"], path),
        mode=_validate_mode(raw_config["mode"]),
        heartbeat_interval_seconds=_validate_heartbeat_interval(
            raw_config["heartbeat_interval_seconds"]
        ),
        reflection_interval_hours=_validate_positive_hours(
            raw_config["reflection_interval_hours"],
            field_name="reflection_interval_hours",
        ),
        reflection_lookback_hours=_validate_positive_hours(
            raw_config["reflection_lookback_hours"],
            field_name="reflection_lookback_hours",
        ),
        reflection_horizons_hours=_validate_reflection_horizons(
            raw_config["reflection_horizons_hours"]
        ),
        live=_validate_optional_live_config(
            raw_config.get("live"),
            config_path=path,
        ),
        live_catchup=_validate_optional_live_catchup_config(
            raw_config.get("live_catchup")
        ),
        analysis_agent=_validate_optional_analysis_agent_config(
            raw_config.get("analysis_agent"),
            config_path=path,
        ),
        pm_review_agent=_validate_optional_pm_review_agent_config(
            raw_config.get("pm_review_agent"),
            config_path=path,
        ),
        checker_agent=_validate_optional_checker_agent_config(
            raw_config.get("checker_agent"),
            config_path=path,
        ),
        reflection_agent=_validate_optional_reflection_agent_config(
            raw_config.get("reflection_agent"),
            config_path=path,
        ),
        validation=_validate_optional_validation_config(
            raw_config.get("validation"),
            config_path=path,
        ),
        market_context=_validate_optional_market_context_config(
            raw_config.get("market_context")
        ),
        execution=_validate_execution_config(raw_config.get("execution")),
        pm_review_runtime=_validate_pm_review_runtime_config(
            raw_config.get("pm_review_runtime")
        ),
        runtime_workers=_validate_runtime_workers_config(
            raw_config.get("runtime_workers")
        ),
        artifact_retention=_validate_optional_artifact_retention_config(
            raw_config.get("artifact_retention"),
            config_path=path,
        ),
        stream_routing=_validate_optional_stream_routing_config(raw_config.get("stream_routing")),
        config_hash=_kernel_config_hash(raw_config),
    )
    _validate_live_source_market_mappings(config)
    return config


def load_kernel_config_audit_identity(config_path: str | Path) -> KernelConfigAuditIdentity:
    """Load the generic config audit identity, preserving load failures as audit evidence."""
    path = Path(config_path).expanduser().resolve(strict=False)
    try:
        return load_kernel_config(path).to_audit_identity()
    except BootstrapConfigError as exc:
        return KernelConfigAuditIdentity(
            config_path=path,
            config_hash=None,
            load_error=str(exc),
        )


def load_workspace_execution_config(
    config_path: str | Path,
) -> tuple[Path, ExecutionConfig]:
    """Load only workspace_root plus execution config, without unrelated env requirements."""

    path = Path(config_path).expanduser().resolve(strict=False)
    try:
        with path.open("rb") as config_file:
            raw_config = tomllib.load(config_file)
    except FileNotFoundError as exc:
        raise BootstrapConfigError(f"Config file does not exist: {path}") from exc
    except PermissionError as exc:
        raise BootstrapConfigError(f"Config file is not readable: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BootstrapConfigError(
            f"Config file contains invalid TOML: {path}: {exc}"
        ) from exc
    except OSError as exc:
        raise BootstrapConfigError(f"Could not read config file {path}: {exc}") from exc
    if not isinstance(raw_config, dict):
        raise BootstrapConfigError("Config file must decode to a TOML table.")
    if "workspace_root" not in raw_config:
        raise BootstrapConfigError("Missing required config field: workspace_root")
    return (
        _validate_workspace_root(raw_config["workspace_root"], path),
        _validate_execution_config(raw_config.get("execution")),
    )


def load_execution_config_for_workspace(
    config_path: str | Path,
    *,
    workspace_root: str | Path,
) -> ExecutionConfig:
    """Load one explicit execution config and verify it belongs to the requested workspace."""

    normalized_workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
    config_workspace_root, execution_config = load_workspace_execution_config(config_path)
    if config_workspace_root != normalized_workspace_root:
        raise BootstrapConfigError(
            "Config workspace_root does not match the requested workspace root: "
            f"config={config_workspace_root} requested={normalized_workspace_root}"
        )
    return execution_config


def resolve_execution_costs_for_target(
    execution_config: ExecutionConfig,
    *,
    target_key: str,
) -> tuple[float, float]:
    """Resolve global-vs-target execution side costs for one target."""

    normalized_target_key = validate_target_key(target_key, error_type=BootstrapConfigError)
    override = execution_config.target_cost_overrides.get(normalized_target_key)
    if override is None:
        return execution_config.buy_cost_bps, execution_config.sell_cost_bps
    return override.buy_cost_bps, override.sell_cost_bps


def resolve_execution_buy_cost_bps_for_target(
    execution_config: ExecutionConfig,
    *,
    target_key: str,
) -> float:
    """Resolve the configured buy-side execution cost for one target."""

    buy_cost_bps, _sell_cost_bps = resolve_execution_costs_for_target(
        execution_config,
        target_key=target_key,
    )
    return buy_cost_bps


def resolve_validation_execution_direction_mode(
    config: KernelConfig,
    *,
    target_key: str,
) -> ValidationExecutionDirectionMode:
    """Resolve target-level execution direction policy from validation mappings."""

    if not isinstance(config, KernelConfig):
        raise BootstrapConfigError("config must be a KernelConfig instance.")
    if config.validation is None:
        raise BootstrapConfigError("config.validation is required.")
    normalized_target_key = validate_target_key(target_key, error_type=BootstrapConfigError)
    mapping = config.validation.market_mappings.get(normalized_target_key)
    if mapping is None:
        raise BootstrapConfigError(
            "Missing validation.market_mappings entry for target_key "
            f"{normalized_target_key!r}."
        )
    return mapping.execution_direction_mode


def _validate_workspace_root(raw_value: object, config_path: Path) -> Path:
    if not isinstance(raw_value, str):
        raise BootstrapConfigError(
            "Config field 'workspace_root' must be a string path."
        )

    if not raw_value.strip():
        raise BootstrapConfigError("Config field 'workspace_root' must not be empty.")

    workspace_root = Path(raw_value).expanduser()
    if not workspace_root.is_absolute():
        workspace_root = resolve_config_relative_path(workspace_root, config_path=config_path)

    return workspace_root.resolve(strict=False)


def _validate_mode(raw_value: object) -> RuntimeMode:
    if not isinstance(raw_value, str):
        raise BootstrapConfigError("Config field 'mode' must be a string.")

    if raw_value not in _ALLOWED_MODES:
        allowed = ", ".join(_ALLOWED_MODES)
        raise BootstrapConfigError(
            f"Config field 'mode' must be one of: {allowed}. Received: {raw_value!r}"
        )

    return cast(RuntimeMode, raw_value)


def _validate_heartbeat_interval(raw_value: object) -> float:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        raise BootstrapConfigError(
            "Config field 'heartbeat_interval_seconds' must be a positive number."
        )

    heartbeat_interval = float(raw_value)
    if heartbeat_interval <= 0:
        raise BootstrapConfigError(
            "Config field 'heartbeat_interval_seconds' must be greater than zero."
        )

    return heartbeat_interval


def _validate_positive_number(
    raw_value: object,
    *,
    field_name: str,
    unit_name: str,
) -> float:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a positive number of {unit_name}."
        )

    normalized_value = float(raw_value)
    if normalized_value <= 0:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be greater than zero {unit_name}."
        )

    return normalized_value


def _validate_positive_hours(raw_value: object, *, field_name: str) -> float:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a positive number of hours."
        )

    hours_value = float(raw_value)
    if hours_value <= 0:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be greater than zero hours."
        )

    return hours_value


def _validate_optional_positive_hours(
    raw_value: object,
    *,
    field_name: str,
) -> float | None:
    if raw_value is None:
        return None
    return _validate_positive_hours(raw_value, field_name=field_name)


def _validate_reflection_horizons(raw_value: object) -> tuple[float, ...]:
    if not isinstance(raw_value, list) or not raw_value:
        raise BootstrapConfigError(
            "Config field 'reflection_horizons_hours' must be a non-empty "
            "array of positive numbers."
        )

    horizons: list[float] = []
    seen_horizons: dict[float, int] = {}
    for index, item in enumerate(raw_value):
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise BootstrapConfigError(
                "Config field 'reflection_horizons_hours' must contain only "
                f"positive numbers; item {index} is invalid."
            )

        horizon = float(item)
        if horizon <= 0:
            raise BootstrapConfigError(
                "Config field 'reflection_horizons_hours' must contain only "
                f"values greater than zero; item {index} is invalid."
            )
        if horizon in seen_horizons:
            first_index = seen_horizons[horizon]
            raise BootstrapConfigError(
                "Config field 'reflection_horizons_hours' must not contain "
                "duplicate horizon values; "
                f"item {index} duplicates item {first_index} ({horizon})."
            )

        seen_horizons[horizon] = index
        horizons.append(horizon)

    return tuple(horizons)


def _validate_optional_live_config(
    raw_value: object,
    *,
    config_path: Path,
) -> LiveConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError("Config field 'live' must be a TOML table.")

    missing_fields = [field for field in _LIVE_REQUIRED_FIELDS if field not in raw_value]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(f"Missing required live config fields: {missing}")

    extra_fields = sorted(set(raw_value) - set(_LIVE_ALLOWED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(f"Unknown live config fields: {unknown}")

    enabled_channels = _validate_live_enabled_channels(raw_value["enabled_channels"])
    web_search_config = raw_value.get("web_search")
    market_news_config = raw_value.get("market_news")
    market_data_config = raw_value.get("market_data")
    web_search_enabled = "web_search" in enabled_channels
    market_news_enabled = "market_news" in enabled_channels
    if web_search_config is not None and not web_search_enabled:
        raise BootstrapConfigError(
            "Config field 'live.web_search' is present but 'web_search' is not "
            "enabled in live.enabled_channels."
        )
    if market_news_config is not None and not market_news_enabled:
        raise BootstrapConfigError(
            "Config field 'live.market_news' is present but 'market_news' is not "
            "enabled in live.enabled_channels."
        )
    return LiveConfig(
        enabled_channels=enabled_channels,
        web_search=(
            _validate_live_web_search_config(
                web_search_config,
                config_path=config_path,
            )
            if web_search_enabled
            else None
        ),
        market_news=(
            _validate_live_market_news_config(market_news_config)
            if market_news_enabled
            else None
        ),
        market_data=(
            _validate_live_market_data_config(market_data_config)
            if market_data_config is not None
            else None
        ),
    )


def _validate_live_enabled_channels(
    raw_value: object,
) -> tuple[LiveSourceChannel, ...]:
    if not isinstance(raw_value, list) or not raw_value:
        raise BootstrapConfigError(
            "Config field 'live.enabled_channels' must be a non-empty array "
            "of live source channel names."
        )

    channels: list[LiveSourceChannel] = []
    seen_channels: set[str] = set()
    for index, raw_channel in enumerate(raw_value):
        if not isinstance(raw_channel, str):
            raise BootstrapConfigError(
                "Config field 'live.enabled_channels' must contain only "
                f"strings; item {index} is invalid."
            )
        if raw_channel not in _ALLOWED_LIVE_SOURCE_CHANNELS:
            allowed = ", ".join(_ALLOWED_LIVE_SOURCE_CHANNELS)
            raise BootstrapConfigError(
                "Config field 'live.enabled_channels' must contain only approved "
                f"channel names: {allowed}. Item {index} received: {raw_channel!r}"
            )
        if raw_channel in seen_channels:
            raise BootstrapConfigError(
                "Config field 'live.enabled_channels' must not contain duplicate "
                f"channel names; item {index} duplicates {raw_channel!r}."
            )
        seen_channels.add(raw_channel)
        channels.append(cast(LiveSourceChannel, raw_channel))

    return tuple(channels)


def _validate_live_web_search_config(
    raw_value: object,
    *,
    config_path: Path,
) -> LiveWebSearchConfig:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'live.web_search' must be a TOML table when "
            "'web_search' is enabled in live.enabled_channels."
        )

    missing_fields = [
        field_name
        for field_name in _WEB_SEARCH_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            "Missing required live.web_search config fields: "
            f"{missing}"
        )

    allowed_fields = set(_WEB_SEARCH_REQUIRED_FIELDS) | set(_WEB_SEARCH_OPTIONAL_FIELDS)
    extra_fields = sorted(set(raw_value) - allowed_fields)
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown live.web_search config fields: {unknown}"
        )

    acquisition_tool_names = _validate_tool_name_tuple(
        raw_value.get(
            "acquisition_tool_names",
            ["search_and_scrape_webpage", "jina_scrape_llm_summary"],
        ),
        field_name="live.web_search.acquisition_tool_names",
    )
    native_web_search_required = (
        "event_trader_openai_web_search" in acquisition_tool_names
    )
    anthropic_web_search_required = (
        "event_trader_anthropic_web_search" in acquisition_tool_names
    )
    cadence_profile = _validate_web_search_cadence_profile(
        raw_value["cadence_profile"],
        field_name="live.web_search.cadence_profile",
    )
    return LiveWebSearchConfig(
        targets=_validate_live_web_search_targets(raw_value["targets"]),
        cadence_profile=cadence_profile,
        cadence_profile_config=_validate_live_web_search_cadence_profile_config(
            raw_value.get("cadence_profiles"),
            cadence_profile=cadence_profile,
            field_name="live.web_search.cadence_profiles",
        ),
        vendor_root=_validate_relative_or_absolute_path(
            raw_value["vendor_root"],
            config_path=config_path,
            field_name="live.web_search.vendor_root",
        ),
        log_dir=_validate_relative_or_absolute_path(
            raw_value["log_dir"],
            config_path=config_path,
            field_name="live.web_search.log_dir",
        ),
        llm_provider=_validate_non_blank_string(
            raw_value["llm_provider"],
            field_name="live.web_search.llm_provider",
        ),
        llm_model_name=_validate_non_blank_string(
            raw_value["llm_model_name"],
            field_name="live.web_search.llm_model_name",
        ),
        llm_api_key=_validate_env_reference(
            raw_value["llm_api_key_env"],
            field_name="live.web_search.llm_api_key_env",
        ),
        llm_base_url=_validate_non_blank_string(
            raw_value["llm_base_url"],
            field_name="live.web_search.llm_base_url",
        ),
        llm_reasoning_effort=normalize_mirothinker_reasoning_effort(
            raw_value.get("llm_reasoning_effort"),
            field_name="live.web_search.llm_reasoning_effort",
            error_type=BootstrapConfigError,
        ),
        llm_max_context_length=_validate_optional_positive_int_with_default(
            raw_value.get("llm_max_context_length"),
            field_name="live.web_search.llm_max_context_length",
            default=_DEFAULT_WEB_SEARCH_LLM_MAX_CONTEXT_LENGTH,
        ),
        serper_api_key=_validate_optional_env_reference(
            raw_value.get("serper_api_key_env", ""),
            field_name="live.web_search.serper_api_key_env",
            required="search_and_scrape_webpage" in acquisition_tool_names,
        ),
        serper_base_url=_validate_optional_non_blank_string(
            raw_value.get("serper_base_url", ""),
            field_name="live.web_search.serper_base_url",
            required="search_and_scrape_webpage" in acquisition_tool_names,
        ),
        jina_api_key=_validate_optional_env_reference(
            raw_value.get("jina_api_key_env", ""),
            field_name="live.web_search.jina_api_key_env",
            required="jina_scrape_llm_summary" in acquisition_tool_names,
        ),
        jina_base_url=_validate_optional_non_blank_string(
            raw_value.get("jina_base_url", ""),
            field_name="live.web_search.jina_base_url",
            required="jina_scrape_llm_summary" in acquisition_tool_names,
        ),
        summary_llm_api_key=_validate_optional_env_reference(
            raw_value.get("summary_llm_api_key_env", ""),
            field_name="live.web_search.summary_llm_api_key_env",
            required="jina_scrape_llm_summary" in acquisition_tool_names,
        ),
        summary_llm_base_url=_validate_optional_non_blank_string(
            raw_value.get("summary_llm_base_url", ""),
            field_name="live.web_search.summary_llm_base_url",
            required="jina_scrape_llm_summary" in acquisition_tool_names,
        ),
        summary_llm_model_name=_validate_optional_non_blank_string(
            raw_value.get("summary_llm_model_name", ""),
            field_name="live.web_search.summary_llm_model_name",
            required="jina_scrape_llm_summary" in acquisition_tool_names,
        ),
        native_web_search_api_key=_validate_optional_env_reference(
            raw_value.get("native_web_search_api_key_env", ""),
            field_name="live.web_search.native_web_search_api_key_env",
            required=native_web_search_required,
        ),
        native_web_search_base_url=_validate_optional_non_blank_string(
            raw_value.get("native_web_search_base_url", ""),
            field_name="live.web_search.native_web_search_base_url",
            required=native_web_search_required,
        ),
        native_web_search_model=_validate_optional_non_blank_string(
            raw_value.get("native_web_search_model", ""),
            field_name="live.web_search.native_web_search_model",
            required=native_web_search_required,
        ),
        native_web_search_tool_type=_validate_optional_native_web_search_tool_type(
            raw_value.get("native_web_search_tool_type", "web_search"),
            required=native_web_search_required,
            field_name="live.web_search.native_web_search_tool_type",
        ),
        anthropic_web_search_api_key=_validate_optional_env_reference(
            raw_value.get("anthropic_web_search_api_key_env", ""),
            field_name="live.web_search.anthropic_web_search_api_key_env",
            required=anthropic_web_search_required,
        ),
        anthropic_web_search_base_url=_validate_optional_non_blank_string(
            raw_value.get("anthropic_web_search_base_url", ""),
            field_name="live.web_search.anthropic_web_search_base_url",
            required=anthropic_web_search_required,
        ),
        anthropic_web_search_model=_validate_optional_non_blank_string(
            raw_value.get("anthropic_web_search_model", ""),
            field_name="live.web_search.anthropic_web_search_model",
            required=anthropic_web_search_required,
        ),
        anthropic_web_search_tool_type=_validate_optional_non_blank_string(
            raw_value.get("anthropic_web_search_tool_type", "web_search_20250305"),
            field_name="live.web_search.anthropic_web_search_tool_type",
            required=anthropic_web_search_required,
        ),
        anthropic_web_search_max_uses=_validate_optional_positive_int(
            raw_value.get("anthropic_web_search_max_uses", 5),
            field_name="live.web_search.anthropic_web_search_max_uses",
            required=anthropic_web_search_required,
        ),
        anthropic_web_search_version=_validate_optional_non_blank_string(
            raw_value.get("anthropic_web_search_version", "2023-06-01"),
            field_name="live.web_search.anthropic_web_search_version",
            required=anthropic_web_search_required,
        ),
        acquisition_tool_names=acquisition_tool_names,
    )


def _validate_live_web_search_targets(
    raw_value: object,
) -> tuple[LiveWebSearchTargetConfig, ...]:
    if not isinstance(raw_value, list) or not raw_value:
        raise BootstrapConfigError(
            "Config field 'live.web_search.targets' must be a non-empty array "
            "of target tables."
        )

    targets: list[LiveWebSearchTargetConfig] = []
    seen_target_keys: dict[str, int] = {}
    for index, target_value in enumerate(raw_value):
        if not isinstance(target_value, dict):
            raise BootstrapConfigError(
                "Config field 'live.web_search.targets' must contain only "
                f"TOML tables; item {index} is invalid."
            )
        missing_fields = [
            field_name
            for field_name in _WEB_SEARCH_TARGET_REQUIRED_FIELDS
            if field_name not in target_value
        ]
        if missing_fields:
            missing = ", ".join(missing_fields)
            raise BootstrapConfigError(
                "Missing required live.web_search.targets"
                f"[{index}] config fields: {missing}"
            )
        extra_fields = sorted(set(target_value) - set(_WEB_SEARCH_TARGET_REQUIRED_FIELDS))
        if extra_fields:
            unknown = ", ".join(extra_fields)
            raise BootstrapConfigError(
                f"Unknown live.web_search.targets[{index}] config fields: {unknown}"
            )

        target_key = validate_target_key(
            target_value["target_key"],
            error_type=BootstrapConfigError,
        )
        if target_key in seen_target_keys:
            first_index = seen_target_keys[target_key]
            raise BootstrapConfigError(
                "Config field 'live.web_search.targets' must not contain "
                f"duplicate target_key {target_key!r}; item {index} duplicates "
                f"item {first_index}."
            )
        seen_target_keys[target_key] = index
        targets.append(
            LiveWebSearchTargetConfig(
                target_key=target_key,
                search_intent=_validate_non_blank_string(
                    target_value["search_intent"],
                    field_name=f"live.web_search.targets[{index}].search_intent",
                ),
                prompt_profile_id=_validate_non_blank_string(
                    target_value["prompt_profile_id"],
                    field_name=(
                        f"live.web_search.targets[{index}].prompt_profile_id"
                    ),
                ),
                control_language=_validate_non_blank_string(
                    target_value["control_language"],
                    field_name=f"live.web_search.targets[{index}].control_language",
                ),
                retrieval_languages=_validate_non_empty_string_tuple(
                    target_value["retrieval_languages"],
                    field_name=(
                        f"live.web_search.targets[{index}].retrieval_languages"
                    ),
                ),
            )
        )

    return tuple(targets)


def _validate_live_market_news_config(raw_value: object) -> LiveMarketNewsConfig:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'live.market_news' must be a TOML table when "
            "'market_news' is enabled in live.enabled_channels."
        )

    missing_fields = [
        field_name
        for field_name in _MARKET_NEWS_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            "Missing required live.market_news config fields: "
            f"{missing}"
        )

    allowed_fields = set(_MARKET_NEWS_REQUIRED_FIELDS) | set(_MARKET_NEWS_OPTIONAL_FIELDS)
    extra_fields = sorted(set(raw_value) - allowed_fields)
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown live.market_news config fields: {unknown}"
        )

    rest = _validate_live_market_news_rest_config(raw_value.get("rest"))
    websocket = _validate_live_market_news_websocket_config(raw_value.get("websocket"))
    rest_active = rest is not None and rest.enabled
    websocket_active = websocket is not None and websocket.enabled
    if not rest_active and not websocket_active:
        raise BootstrapConfigError(
            "live.market_news must enable REST or websocket acquisition; add "
            "[live.market_news.rest] with enabled = true or "
            "[live.market_news.websocket] with enabled = true."
        )

    return LiveMarketNewsConfig(
        targets=_validate_live_market_news_targets(raw_value["targets"]),
        rest=rest,
        websocket=websocket,
    )


def _validate_live_market_news_rest_config(
    raw_value: object,
) -> LiveMarketNewsRestConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'live.market_news.rest' must be a TOML table."
        )
    if "enabled" not in raw_value:
        raise BootstrapConfigError(
            "Missing required live.market_news.rest config fields: enabled"
        )
    enabled = _validate_bool(
        raw_value["enabled"],
        field_name="live.market_news.rest.enabled",
    )
    allowed_fields = set(_MARKET_NEWS_REST_REQUIRED_FIELDS) | set(
        _MARKET_NEWS_REST_OPTIONAL_FIELDS
    )
    extra_fields = sorted(set(raw_value) - allowed_fields)
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown live.market_news.rest config fields: {unknown}"
        )
    if not enabled:
        return LiveMarketNewsRestConfig(
            enabled=False,
            base_url="",
            api_key="",
            cadence_profile=_MARKET_NEWS_REST_ALLOWED_CADENCE_PROFILES[0],
            cadence_profile_config=_validate_live_market_news_rest_cadence_profile_config(
                None,
                cadence_profile=_MARKET_NEWS_REST_ALLOWED_CADENCE_PROFILES[0],
                field_name="live.market_news.rest.cadence_profiles",
            ),
            limit=50,
            include_content=True,
            exclude_contentless=True,
            soft_fail=True,
        )
    missing_fields = [
        field_name
        for field_name in _MARKET_NEWS_REST_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            f"Missing required live.market_news.rest config fields: {missing}"
        )
    cadence_profile = _validate_market_news_rest_cadence_profile(
        raw_value["cadence_profile"],
        field_name="live.market_news.rest.cadence_profile",
    )
    limit = _validate_positive_int(
        raw_value["limit"],
        field_name="live.market_news.rest.limit",
    )
    if limit > _MARKET_NEWS_MAX_LIMIT:
        raise BootstrapConfigError(
            "Config field 'live.market_news.rest.limit' must be less than or equal "
            f"to {_MARKET_NEWS_MAX_LIMIT}."
        )
    return LiveMarketNewsRestConfig(
        enabled=True,
        base_url=_validate_base_url(
            raw_value["base_url"],
            field_name="live.market_news.rest.base_url",
        ),
        api_key=_validate_env_reference(
            raw_value["api_key_env"],
            field_name="live.market_news.rest.api_key_env",
        ),
        cadence_profile=cadence_profile,
        cadence_profile_config=_validate_live_market_news_rest_cadence_profile_config(
            raw_value.get("cadence_profiles"),
            cadence_profile=cadence_profile,
            field_name="live.market_news.rest.cadence_profiles",
        ),
        limit=limit,
        include_content=_validate_bool(
            raw_value["include_content"],
            field_name="live.market_news.rest.include_content",
        ),
        exclude_contentless=_validate_bool(
            raw_value["exclude_contentless"],
            field_name="live.market_news.rest.exclude_contentless",
        ),
        soft_fail=_validate_bool(
            raw_value["soft_fail"],
            field_name="live.market_news.rest.soft_fail",
        ),
    )


def _validate_live_market_news_websocket_config(
    raw_value: object,
) -> LiveMarketNewsWebSocketConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'live.market_news.websocket' must be a TOML table."
        )
    missing_fields = [
        field_name
        for field_name in _MARKET_NEWS_WEBSOCKET_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            f"Missing required live.market_news.websocket config fields: {missing}"
        )
    extra_fields = sorted(
        set(raw_value) - set(_MARKET_NEWS_WEBSOCKET_REQUIRED_FIELDS)
    )
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown live.market_news.websocket config fields: {unknown}"
        )

    soft_fail = _validate_bool(
        raw_value["soft_fail"],
        field_name="live.market_news.websocket.soft_fail",
    )
    reconnect_seconds_min = _validate_positive_int(
        raw_value["reconnect_seconds_min"],
        field_name="live.market_news.websocket.reconnect_seconds_min",
    )
    reconnect_seconds_max = _validate_positive_int(
        raw_value["reconnect_seconds_max"],
        field_name="live.market_news.websocket.reconnect_seconds_max",
    )
    if reconnect_seconds_min > reconnect_seconds_max:
        raise BootstrapConfigError(
            "live.market_news.websocket.reconnect_seconds_min must be less than "
            "or equal to live.market_news.websocket.reconnect_seconds_max."
        )
    return LiveMarketNewsWebSocketConfig(
        enabled=_validate_bool(
            raw_value["enabled"],
            field_name="live.market_news.websocket.enabled",
        ),
        provider=_validate_non_blank_string(
            raw_value["provider"],
            field_name="live.market_news.websocket.provider",
        ),
        url=_validate_websocket_url(
            raw_value["url"],
            field_name="live.market_news.websocket.url",
        ),
        token=_validate_optional_env_reference(
            raw_value["token_env"],
            field_name="live.market_news.websocket.token_env",
            required=not soft_fail,
        ),
        tickers=_validate_market_news_symbol_tuple(
            raw_value["tickers"],
            field_name="live.market_news.websocket.tickers",
        ),
        soft_fail=soft_fail,
        reconnect_seconds_min=reconnect_seconds_min,
        reconnect_seconds_max=reconnect_seconds_max,
        heartbeat_seconds=_validate_positive_int(
            raw_value["heartbeat_seconds"],
            field_name="live.market_news.websocket.heartbeat_seconds",
        ),
        session=_validate_live_market_news_websocket_session_config(
            raw_value["session"]
        ),
    )


def _validate_optional_live_catchup_config(raw_value: object) -> LiveCatchupConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError("Config field 'live_catchup' must be a TOML table.")
    extra_fields = sorted(set(raw_value) - set(_LIVE_CATCHUP_ALLOWED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(f"Unknown live_catchup config fields: {unknown}")
    return LiveCatchupConfig(
        web_search=_validate_live_catchup_web_search_config(raw_value.get("web_search")),
        market_news=_validate_live_catchup_market_news_config(raw_value.get("market_news")),
    )


def _validate_live_catchup_web_search_config(
    raw_value: object,
) -> LiveCatchupWebSearchConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError("Config field 'live_catchup.web_search' must be a TOML table.")
    extra_fields = sorted(set(raw_value) - set(_LIVE_CATCHUP_WEB_SEARCH_REQUIRED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown live_catchup.web_search config fields: {unknown}"
        )
    missing_fields = [
        field_name
        for field_name in _LIVE_CATCHUP_WEB_SEARCH_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            "Missing required live_catchup.web_search config fields: "
            f"{missing}"
        )
    return LiveCatchupWebSearchConfig(
        max_slice_hours=_validate_positive_int(
            raw_value["max_slice_hours"],
            field_name="live_catchup.web_search.max_slice_hours",
        )
    )


def _validate_live_catchup_market_news_config(
    raw_value: object,
) -> LiveCatchupMarketNewsConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError("Config field 'live_catchup.market_news' must be a TOML table.")
    extra_fields = sorted(set(raw_value) - set(_LIVE_CATCHUP_MARKET_NEWS_REQUIRED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown live_catchup.market_news config fields: {unknown}"
        )
    missing_fields = [
        field_name
        for field_name in _LIVE_CATCHUP_MARKET_NEWS_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            "Missing required live_catchup.market_news config fields: "
            f"{missing}"
        )
    return LiveCatchupMarketNewsConfig(
        max_slice_hours=_validate_positive_int(
            raw_value["max_slice_hours"],
            field_name="live_catchup.market_news.max_slice_hours",
        )
    )


def _validate_live_market_news_targets(
    raw_value: object,
) -> tuple[LiveMarketNewsTargetConfig, ...]:
    if not isinstance(raw_value, list) or not raw_value:
        raise BootstrapConfigError(
            "Config field 'live.market_news.targets' must be a non-empty array "
            "of target tables."
        )

    targets: list[LiveMarketNewsTargetConfig] = []
    seen_target_keys: dict[str, int] = {}
    for index, target_value in enumerate(raw_value):
        if not isinstance(target_value, dict):
            raise BootstrapConfigError(
                "Config field 'live.market_news.targets' must contain only "
                f"TOML tables; item {index} is invalid."
            )
        allowed_fields = set(_MARKET_NEWS_TARGET_REQUIRED_FIELDS) | set(
            _MARKET_NEWS_TARGET_OPTIONAL_FIELDS
        )
        missing_fields = [
            field_name
            for field_name in _MARKET_NEWS_TARGET_REQUIRED_FIELDS
            if field_name not in target_value
        ]
        if missing_fields:
            missing = ", ".join(missing_fields)
            raise BootstrapConfigError(
                "Missing required live.market_news.targets"
                f"[{index}] config fields: {missing}"
            )
        extra_fields = sorted(set(target_value) - allowed_fields)
        if extra_fields:
            unknown = ", ".join(extra_fields)
            raise BootstrapConfigError(
                f"Unknown live.market_news.targets[{index}] config fields: {unknown}"
            )

        target_key = validate_target_key(
            target_value["target_key"],
            error_type=BootstrapConfigError,
        )
        if target_key in seen_target_keys:
            first_index = seen_target_keys[target_key]
            raise BootstrapConfigError(
                "Config field 'live.market_news.targets' must not contain "
                f"duplicate target_key {target_key!r}; item {index} duplicates "
                f"item {first_index}."
            )
        seen_target_keys[target_key] = index
        raw_labels = target_value.get("labels", [])
        if not isinstance(raw_labels, list):
            raise BootstrapConfigError(
                f"Config field 'live.market_news.targets[{index}].labels' must "
                "be a TOML array of strings."
            )
        targets.append(
            LiveMarketNewsTargetConfig(
                target_key=target_key,
                symbols=_validate_market_news_symbol_tuple(
                    target_value["symbols"],
                    field_name=f"live.market_news.targets[{index}].symbols",
                ),
                labels=tuple(
                    validate_labels(
                        raw_labels,
                        error_type=BootstrapConfigError,
                    )
                ),
            )
        )

    return tuple(targets)


def _validate_market_news_symbol_tuple(
    raw_value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    return _validate_upper_symbol_tuple(raw_value, field_name=field_name)


def _validate_live_market_data_config(raw_value: object) -> LiveMarketDataConfig:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError("Config field 'live.market_data' must be a TOML table.")
    missing_fields = [
        field_name
        for field_name in _LIVE_MARKET_DATA_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            f"Missing required live.market_data config fields: {missing}"
        )
    extra_fields = sorted(set(raw_value) - set(_LIVE_MARKET_DATA_ALLOWED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(f"Unknown live.market_data config fields: {unknown}")
    canonical_bar_granularity = _validate_market_context_bar_granularity(
        raw_value["canonical_bar_granularity"],
        field_name="live.market_data.canonical_bar_granularity",
    )
    return LiveMarketDataConfig(
        enabled=_validate_bool(
            raw_value["enabled"],
            field_name="live.market_data.enabled",
        ),
        targets=_validate_live_market_data_targets(
            raw_value["targets"],
            canonical_bar_granularity=canonical_bar_granularity,
        ),
        canonical_bar_granularity=canonical_bar_granularity,
        warmup=_validate_live_market_data_warmup_config(raw_value["warmup"]),
    )


def _validate_live_market_data_targets(
    raw_value: object,
    *,
    canonical_bar_granularity: str,
) -> tuple[LiveMarketDataTargetConfig, ...]:
    if not isinstance(raw_value, list) or not raw_value:
        raise BootstrapConfigError(
            "Config field 'live.market_data.targets' must be a non-empty array "
            "of target tables."
        )
    targets: list[LiveMarketDataTargetConfig] = []
    seen_target_keys: dict[str, int] = {}
    for index, target_value in enumerate(raw_value):
        if not isinstance(target_value, dict):
            raise BootstrapConfigError(
                "Config field 'live.market_data.targets' must contain only "
                f"TOML tables; item {index} is invalid."
            )
        missing_fields = [
            field_name
            for field_name in _LIVE_MARKET_DATA_TARGET_REQUIRED_FIELDS
            if field_name not in target_value
        ]
        if missing_fields:
            missing = ", ".join(missing_fields)
            raise BootstrapConfigError(
                "Missing required live.market_data.targets"
                f"[{index}] config fields: {missing}"
            )
        extra_fields = sorted(
            set(target_value) - set(_LIVE_MARKET_DATA_TARGET_REQUIRED_FIELDS)
        )
        if extra_fields:
            unknown = ", ".join(extra_fields)
            raise BootstrapConfigError(
                f"Unknown live.market_data.targets[{index}] config fields: {unknown}"
            )
        target_key = validate_target_key(
            target_value["target_key"],
            error_type=BootstrapConfigError,
        )
        if target_key in seen_target_keys:
            first_index = seen_target_keys[target_key]
            raise BootstrapConfigError(
                "Config field 'live.market_data.targets' must not contain "
                f"duplicate target_key {target_key!r}; item {index} duplicates "
                f"item {first_index}."
            )
        seen_target_keys[target_key] = index
        subscriptions = _validate_live_market_data_subscriptions(
            target_value["subscriptions"],
            field_name=f"live.market_data.targets[{index}].subscriptions",
            expected_bar_granularity=canonical_bar_granularity,
            expected_bar_granularity_field_name=(
                "live.market_data.canonical_bar_granularity"
            ),
        )
        primary_subscription_count = sum(
            1
            for subscription in subscriptions
            if subscription.purpose == "primary_tradable"
        )
        if primary_subscription_count != 1:
            raise BootstrapConfigError(
                "Config field "
                f"'live.market_data.targets[{index}].subscriptions' must contain "
                "exactly one subscription with purpose='primary_tradable'."
            )
        targets.append(
            LiveMarketDataTargetConfig(
                target_key=target_key,
                subscriptions=subscriptions,
            )
        )
    return tuple(targets)


def _validate_live_market_data_subscriptions(
    raw_value: object,
    *,
    field_name: str,
    expected_bar_granularity: str,
    expected_bar_granularity_field_name: str,
) -> tuple[MarketDataSubscriptionConfig, ...]:
    if not isinstance(raw_value, list) or not raw_value:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a non-empty array of subscription tables."
        )
    subscriptions: list[MarketDataSubscriptionConfig] = []
    seen_symbols: dict[str, int] = {}
    for index, item in enumerate(raw_value):
        item_field_name = f"{field_name}[{index}]"
        if not isinstance(item, dict):
            raise BootstrapConfigError(
                f"Config field '{field_name}' must contain only TOML tables; "
                f"item {index} is invalid."
            )
        missing_fields = [
            required
            for required in _LIVE_MARKET_DATA_SUBSCRIPTION_REQUIRED_FIELDS
            if required not in item
        ]
        if missing_fields:
            missing = ", ".join(missing_fields)
            raise BootstrapConfigError(
                f"Missing required {item_field_name} config fields: {missing}"
            )
        extra_fields = sorted(
            set(item) - set(_LIVE_MARKET_DATA_SUBSCRIPTION_ALLOWED_FIELDS)
        )
        if extra_fields:
            unknown = ", ".join(extra_fields)
            raise BootstrapConfigError(
                f"Unknown {item_field_name} config fields: {unknown}"
            )
        symbol = _validate_upper_symbol(
            item["symbol"],
            field_name=f"{item_field_name}.symbol",
        )
        if symbol in seen_symbols:
            first_index = seen_symbols[symbol]
            raise BootstrapConfigError(
                f"Config field '{field_name}' must not contain duplicate symbol {symbol!r}; "
                f"item {index} duplicates item {first_index}."
            )
        seen_symbols[symbol] = index
        bar_granularity = _validate_market_context_bar_granularity(
            item["bar_granularity"],
            field_name=f"{item_field_name}.bar_granularity",
        )
        if bar_granularity != expected_bar_granularity:
            raise BootstrapConfigError(
                f"Config field '{item_field_name}.bar_granularity' must match "
                f"{expected_bar_granularity_field_name}."
            )
        subscriptions.append(
            MarketDataSubscriptionConfig(
                symbol=symbol,
                provider=_validate_live_market_data_subscription_provider(
                    item["provider"],
                    field_name=f"{item_field_name}.provider",
                ),
                purpose=_validate_non_blank_string(
                    item["purpose"],
                    field_name=f"{item_field_name}.purpose",
                ),
                market_session=_validate_validation_market_session(
                    item["market_session"],
                    field_name=f"{item_field_name}.market_session",
                ),
                exchange=_validate_non_blank_string(
                    item["exchange"],
                    field_name=f"{item_field_name}.exchange",
                ).upper(),
                exchange_session_scope=_validate_validation_exchange_session_scope(
                    item["exchange_session_scope"],
                    field_name=f"{item_field_name}.exchange_session_scope",
                ),
                bar_granularity=bar_granularity,
                adjustment_policy=(
                    _validate_market_data_adjustment_policy(
                        item["adjustment_policy"],
                        field_name=f"{item_field_name}.adjustment_policy",
                    )
                    if "adjustment_policy" in item
                    else None
                ),
            )
        )
    return tuple(subscriptions)


def _validate_live_market_data_warmup_config(
    raw_value: object,
) -> LiveMarketDataWarmupConfig:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'live.market_data.warmup' must be a TOML table."
        )
    missing_fields = [
        field_name
        for field_name in _LIVE_MARKET_DATA_WARMUP_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            f"Missing required live.market_data.warmup config fields: {missing}"
        )
    extra_fields = sorted(
        set(raw_value) - set(_LIVE_MARKET_DATA_WARMUP_REQUIRED_FIELDS)
    )
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown live.market_data.warmup config fields: {unknown}"
        )
    return LiveMarketDataWarmupConfig(
        enabled=_validate_bool(
            raw_value["enabled"],
            field_name="live.market_data.warmup.enabled",
        ),
        min_lookback_days=_validate_positive_int(
            raw_value["min_lookback_days"],
            field_name="live.market_data.warmup.min_lookback_days",
        ),
    )


def _validate_upper_symbol_tuple(
    raw_value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    symbols = _validate_non_empty_string_tuple(raw_value, field_name=field_name)
    normalized: list[str] = []
    for symbol in symbols:
        upper_symbol = symbol.upper()
        if upper_symbol in normalized:
            raise BootstrapConfigError(
                f"Config field '{field_name}' must not contain duplicate symbols."
            )
        normalized.append(upper_symbol)
    return tuple(normalized)


def _validate_upper_symbol(
    raw_value: object,
    *,
    field_name: str,
) -> str:
    symbol = _validate_non_blank_string(raw_value, field_name=field_name)
    return symbol.upper()


def _validate_live_market_data_subscription_provider(
    value: object,
    *,
    field_name: str,
) -> MarketDataSubscriptionProvider:
    normalized = _validate_non_blank_string(value, field_name=field_name)
    if normalized not in {
        "alpaca_like",
        "bc_private_v1",
        "futu_openapi",
        "local_archive",
    }:
        raise BootstrapConfigError(
            f"'{field_name}' must be one of: alpaca_like, bc_private_v1, "
            "futu_openapi, local_archive."
        )
    return cast(MarketDataSubscriptionProvider, normalized)


def _validate_web_search_cadence_profile(value: object, *, field_name: str) -> str:
    normalized = _validate_non_blank_string(value, field_name=field_name)
    if normalized not in _WEB_SEARCH_ALLOWED_CADENCE_PROFILES:
        allowed = ", ".join(_WEB_SEARCH_ALLOWED_CADENCE_PROFILES)
        raise BootstrapConfigError(f"{field_name} must be one of: {allowed}.")
    return normalized


def _validate_market_news_rest_cadence_profile(
    value: object,
    *,
    field_name: str,
) -> str:
    normalized = _validate_non_blank_string(value, field_name=field_name)
    if normalized not in _MARKET_NEWS_REST_ALLOWED_CADENCE_PROFILES:
        allowed = ", ".join(_MARKET_NEWS_REST_ALLOWED_CADENCE_PROFILES)
        raise BootstrapConfigError(f"{field_name} must be one of: {allowed}.")
    return normalized


def _validate_live_web_search_cadence_profile_config(
    raw_value: object,
    *,
    cadence_profile: str,
    field_name: str,
) -> LiveWebSearchCadenceProfileConfig:
    if raw_value is None:
        return LiveWebSearchCadenceProfileConfig(
            timezone=_DEFAULT_WEB_SEARCH_PROFILE_TIMEZONE,
            trading_day_local_times=_DEFAULT_WEB_SEARCH_TRADING_DAY_LOCAL_TIMES,
            closed_day_local_times=_DEFAULT_WEB_SEARCH_CLOSED_DAY_LOCAL_TIMES,
        )
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a TOML table."
        )
    extra_profiles = sorted(set(raw_value) - {cadence_profile})
    if extra_profiles:
        unknown = ", ".join(extra_profiles)
        raise BootstrapConfigError(
            f"Unknown {field_name} entries: {unknown}"
        )
    profile_value = raw_value.get(cadence_profile)
    if profile_value is None:
        return LiveWebSearchCadenceProfileConfig(
            timezone=_DEFAULT_WEB_SEARCH_PROFILE_TIMEZONE,
            trading_day_local_times=_DEFAULT_WEB_SEARCH_TRADING_DAY_LOCAL_TIMES,
            closed_day_local_times=_DEFAULT_WEB_SEARCH_CLOSED_DAY_LOCAL_TIMES,
        )
    if not isinstance(profile_value, dict):
        raise BootstrapConfigError(
            f"Config field '{field_name}.{cadence_profile}' must be a TOML table."
        )
    missing_fields = [
        field_name
        for field_name in _WEB_SEARCH_CADENCE_PROFILE_REQUIRED_FIELDS
        if field_name not in profile_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            f"Missing required {field_name}.{cadence_profile} config fields: {missing}"
        )
    extra_fields = sorted(
        set(profile_value) - set(_WEB_SEARCH_CADENCE_PROFILE_REQUIRED_FIELDS)
    )
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown {field_name}.{cadence_profile} config fields: {unknown}"
        )
    return LiveWebSearchCadenceProfileConfig(
        timezone=_validate_non_blank_string(
            profile_value["timezone"],
            field_name=f"{field_name}.{cadence_profile}.timezone",
        ),
        trading_day_local_times=_validate_local_time_tuple(
            profile_value["trading_day_local_times"],
            field_name=f"{field_name}.{cadence_profile}.trading_day_local_times",
        ),
        closed_day_local_times=_validate_local_time_tuple(
            profile_value["closed_day_local_times"],
            field_name=f"{field_name}.{cadence_profile}.closed_day_local_times",
        ),
    )


def _validate_live_market_news_rest_cadence_profile_config(
    raw_value: object,
    *,
    cadence_profile: str,
    field_name: str,
) -> LiveMarketNewsRestCadenceProfileConfig:
    if raw_value is None:
        return LiveMarketNewsRestCadenceProfileConfig(
            timezone=_DEFAULT_WEB_SEARCH_PROFILE_TIMEZONE,
            calendar="NASDAQ",
            rth_local_times=("10:30", "12:30", "15:45"),
            trading_day_off_rth_local_times=("07:45", "17:30"),
            closed_day_local_times=("12:00",),
        )
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a TOML table."
        )
    extra_profiles = sorted(set(raw_value) - {cadence_profile})
    if extra_profiles:
        unknown = ", ".join(extra_profiles)
        raise BootstrapConfigError(
            f"Unknown {field_name} entries: {unknown}"
        )
    profile_value = raw_value.get(cadence_profile)
    if profile_value is None:
        return LiveMarketNewsRestCadenceProfileConfig(
            timezone=_DEFAULT_WEB_SEARCH_PROFILE_TIMEZONE,
            calendar="NASDAQ",
            rth_local_times=("10:30", "12:30", "15:45"),
            trading_day_off_rth_local_times=("07:45", "17:30"),
            closed_day_local_times=("12:00",),
        )
    if not isinstance(profile_value, dict):
        raise BootstrapConfigError(
            f"Config field '{field_name}.{cadence_profile}' must be a TOML table."
        )
    missing_fields = [
        required_field
        for required_field in _MARKET_NEWS_REST_CADENCE_PROFILE_REQUIRED_FIELDS
        if required_field not in profile_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            f"Missing required {field_name}.{cadence_profile} config fields: {missing}"
        )
    extra_fields = sorted(
        set(profile_value) - set(_MARKET_NEWS_REST_CADENCE_PROFILE_REQUIRED_FIELDS)
    )
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown {field_name}.{cadence_profile} config fields: {unknown}"
        )
    return LiveMarketNewsRestCadenceProfileConfig(
        timezone=_validate_non_blank_string(
            profile_value["timezone"],
            field_name=f"{field_name}.{cadence_profile}.timezone",
        ),
        calendar=_validate_non_blank_string(
            profile_value["calendar"],
            field_name=f"{field_name}.{cadence_profile}.calendar",
        ),
        rth_local_times=_validate_local_time_tuple(
            profile_value["rth_local_times"],
            field_name=f"{field_name}.{cadence_profile}.rth_local_times",
        ),
        trading_day_off_rth_local_times=_validate_local_time_tuple(
            profile_value["trading_day_off_rth_local_times"],
            field_name=(
                f"{field_name}.{cadence_profile}.trading_day_off_rth_local_times"
            ),
        ),
        closed_day_local_times=_validate_local_time_tuple(
            profile_value["closed_day_local_times"],
            field_name=f"{field_name}.{cadence_profile}.closed_day_local_times",
        ),
    )


def _validate_live_market_news_websocket_session_config(
    raw_value: object,
) -> LiveMarketNewsWebSocketSessionConfig:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'live.market_news.websocket.session' must be a TOML table."
        )
    missing_fields = [
        field_name
        for field_name in _MARKET_NEWS_WEBSOCKET_SESSION_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            "Missing required live.market_news.websocket.session config fields: "
            f"{missing}"
        )
    extra_fields = sorted(
        set(raw_value) - set(_MARKET_NEWS_WEBSOCKET_SESSION_REQUIRED_FIELDS)
    )
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown live.market_news.websocket.session config fields: {unknown}"
        )
    mode = _validate_non_blank_string(
        raw_value["mode"],
        field_name="live.market_news.websocket.session.mode",
    )
    if mode not in _MARKET_NEWS_WEBSOCKET_SESSION_MODES:
        allowed = ", ".join(_MARKET_NEWS_WEBSOCKET_SESSION_MODES)
        raise BootstrapConfigError(
            "live.market_news.websocket.session.mode must be one of: "
            f"{allowed}."
        )
    return LiveMarketNewsWebSocketSessionConfig(
        mode=cast(LiveMarketNewsWebSocketSessionMode, mode),
        timezone=_validate_non_blank_string(
            raw_value["timezone"],
            field_name="live.market_news.websocket.session.timezone",
        ),
        calendar=_validate_non_blank_string(
            raw_value["calendar"],
            field_name="live.market_news.websocket.session.calendar",
        ),
        open_buffer_minutes=_validate_non_negative_int_config(
            raw_value["open_buffer_minutes"],
            field_name="live.market_news.websocket.session.open_buffer_minutes",
        ),
        close_buffer_minutes=_validate_non_negative_int_config(
            raw_value["close_buffer_minutes"],
            field_name="live.market_news.websocket.session.close_buffer_minutes",
        ),
    )


def _validate_local_time_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise BootstrapConfigError(f"{field_name} must be a non-empty array of HH:MM strings.")
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise BootstrapConfigError(f"{field_name}[{index}] must be a string.")
        try:
            hour_text, minute_text = item.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except ValueError as exc:
            raise BootstrapConfigError(
                f"{field_name}[{index}] must use HH:MM format."
            ) from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise BootstrapConfigError(
                f"{field_name}[{index}] must use a valid HH:MM clock time."
            )
        normalized.append(f"{hour:02d}:{minute:02d}")
    if len(set(normalized)) != len(normalized):
        raise BootstrapConfigError(f"{field_name} must not contain duplicate times.")
    return tuple(normalized)


def _validate_optional_analysis_agent_config(
    raw_value: object,
    *,
    config_path: Path,
) -> AnalysisAgentConfig | None:
    normalized = _validate_optional_agent_runtime_config(
        raw_value,
        config_path=config_path,
        table_name="analysis_agent",
        required_fields=_ANALYSIS_AGENT_REQUIRED_FIELDS,
        optional_fields=_ANALYSIS_AGENT_OPTIONAL_FIELDS,
    )
    if normalized is None:
        return None
    return AnalysisAgentConfig(
        vendor_root=normalized.vendor_root,
        log_dir=normalized.log_dir,
        llm_provider=normalized.llm_provider,
        llm_model_name=normalized.llm_model_name,
        llm_api_key=normalized.llm_api_key,
        llm_base_url=normalized.llm_base_url,
        llm_reasoning_effort=normalized.llm_reasoning_effort,
        llm_max_context_length=normalized.llm_max_context_length,
        wall_clock_timeout_seconds=_validate_optional_non_negative_int_with_default(
            raw_value.get("wall_clock_timeout_seconds"),
            field_name="analysis_agent.wall_clock_timeout_seconds",
            default=_DEFAULT_ANALYSIS_AGENT_WALL_CLOCK_TIMEOUT_SECONDS,
        ),
        structured_output_mode=_validate_analysis_structured_output_mode(
            raw_value.get("structured_output_mode", "disabled")
        ),
        structured_output_probe=_validate_optional_bool(
            raw_value.get("structured_output_probe"),
            field_name="analysis_agent.structured_output_probe",
            default=True,
        ),
    )


def _validate_analysis_structured_output_mode(value: object) -> AnalysisStructuredOutputMode:
    try:
        return normalize_analysis_structured_output_mode(value)
    except AnalysisStructuredFinalizerError as exc:
        raise BootstrapConfigError(f"analysis_agent.{exc}") from exc


def _validate_optional_checker_agent_config(
    raw_value: object,
    *,
    config_path: Path,
) -> CheckerAgentConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'checker_agent' must be a TOML table."
        )

    missing_fields = [
        field_name
        for field_name in _CHECKER_AGENT_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            f"Missing required checker_agent config fields: {missing}"
        )

    allowed_fields = set(_CHECKER_AGENT_REQUIRED_FIELDS) | set(
        _CHECKER_AGENT_OPTIONAL_FIELDS
    )
    extra_fields = sorted(set(raw_value) - allowed_fields)
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(f"Unknown checker_agent config fields: {unknown}")

    log_dir = _validate_relative_or_absolute_path(
        raw_value["log_dir"],
        config_path=config_path,
        field_name="checker_agent.log_dir",
    )
    llm_provider = _validate_non_blank_string(
        raw_value["llm_provider"],
        field_name="checker_agent.llm_provider",
    )
    if llm_provider != "openai":
        raise BootstrapConfigError(
            "checker_agent.llm_provider must be 'openai' because checker uses "
            "one OpenAI-compatible chat completion."
        )
    llm_model_name = _validate_non_blank_string(
        raw_value["llm_model_name"],
        field_name="checker_agent.llm_model_name",
    )
    llm_api_key = _validate_env_reference(
        raw_value["llm_api_key_env"],
        field_name="checker_agent.llm_api_key_env",
    )
    llm_base_url = _validate_non_blank_string(
        raw_value["llm_base_url"],
        field_name="checker_agent.llm_base_url",
    )
    llm_reasoning_effort = normalize_mirothinker_reasoning_effort(
        raw_value.get("llm_reasoning_effort"),
        field_name="checker_agent.llm_reasoning_effort",
        error_type=BootstrapConfigError,
    )

    return CheckerAgentConfig(
        log_dir=log_dir,
        llm_provider=llm_provider,
        llm_model_name=llm_model_name,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_reasoning_effort=llm_reasoning_effort,
        evidence_excerpt_chars=_validate_optional_positive_int_with_default(
            raw_value.get("evidence_excerpt_chars"),
            field_name="checker_agent.evidence_excerpt_chars",
            default=_DEFAULT_CHECKER_EVIDENCE_EXCERPT_CHARS,
        ),
        section_excerpt_chars=_validate_optional_positive_int_with_default(
            raw_value.get("section_excerpt_chars"),
            field_name="checker_agent.section_excerpt_chars",
            default=_DEFAULT_CHECKER_SECTION_EXCERPT_CHARS,
        ),
        recent_timeline_items=_validate_optional_positive_int_with_default(
            raw_value.get("recent_timeline_items"),
            field_name="checker_agent.recent_timeline_items",
            default=_DEFAULT_CHECKER_RECENT_TIMELINE_ITEMS,
        ),
        max_pack_chars=_validate_optional_positive_int_with_default(
            raw_value.get("max_pack_chars"),
            field_name="checker_agent.max_pack_chars",
            default=_DEFAULT_CHECKER_MAX_PACK_CHARS,
        ),
        force_escalate_on_insufficient_coverage=_validate_optional_bool(
            raw_value.get("force_escalate_on_insufficient_coverage"),
            field_name="checker_agent.force_escalate_on_insufficient_coverage",
            default=_DEFAULT_CHECKER_FORCE_ESCALATE_ON_INSUFFICIENT_COVERAGE,
        ),
    )


def _validate_optional_pm_review_agent_config(
    raw_value: object,
    *,
    config_path: Path,
) -> PMReviewAgentConfig | None:
    normalized = _validate_optional_agent_runtime_config(
        raw_value,
        config_path=config_path,
        table_name="pm_review_agent",
        required_fields=_PM_REVIEW_AGENT_REQUIRED_FIELDS,
        optional_fields=_PM_REVIEW_AGENT_OPTIONAL_FIELDS,
    )
    if normalized is None:
        return None
    raw_table = cast(dict[str, object], raw_value)
    return PMReviewAgentConfig(
        vendor_root=normalized.vendor_root,
        log_dir=normalized.log_dir,
        llm_provider=normalized.llm_provider,
        llm_model_name=normalized.llm_model_name,
        llm_api_key=normalized.llm_api_key,
        llm_base_url=normalized.llm_base_url,
        llm_reasoning_effort=normalized.llm_reasoning_effort,
        llm_max_context_length=normalized.llm_max_context_length,
        wall_clock_timeout_seconds=_validate_optional_non_negative_int_with_default(
            raw_table.get("wall_clock_timeout_seconds"),
            field_name="pm_review_agent.wall_clock_timeout_seconds",
            default=_DEFAULT_ANALYSIS_AGENT_WALL_CLOCK_TIMEOUT_SECONDS,
        ),
    )


def _validate_optional_reflection_agent_config(
    raw_value: object,
    *,
    config_path: Path,
) -> ReflectionAgentConfig | None:
    normalized = _validate_optional_agent_runtime_config(
        raw_value,
        config_path=config_path,
        table_name="reflection_agent",
        required_fields=_REFLECTION_AGENT_REQUIRED_FIELDS,
        optional_fields=_REFLECTION_AGENT_OPTIONAL_FIELDS,
    )
    if normalized is None:
        return None
    return ReflectionAgentConfig(
        vendor_root=normalized.vendor_root,
        log_dir=normalized.log_dir,
        llm_provider=normalized.llm_provider,
        llm_model_name=normalized.llm_model_name,
        llm_api_key=normalized.llm_api_key,
        llm_base_url=normalized.llm_base_url,
        llm_max_context_length=normalized.llm_max_context_length,
    )


def _validate_optional_agent_runtime_config(
    raw_value: object,
    *,
    config_path: Path,
    table_name: str,
    required_fields: tuple[str, ...],
    optional_fields: tuple[str, ...] = (),
    default_llm_max_context_length: int = _DEFAULT_AGENT_LLM_MAX_CONTEXT_LENGTH,
) -> _AgentRuntimeConfigValues | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            f"Config field '{table_name}' must be a TOML table."
        )

    missing_fields = [
        field_name for field_name in required_fields if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            f"Missing required {table_name} config fields: {missing}"
        )

    allowed_fields = set(required_fields) | set(optional_fields)
    extra_fields = sorted(set(raw_value) - allowed_fields)
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown {table_name} config fields: {unknown}"
        )

    return _AgentRuntimeConfigValues(
        vendor_root=_validate_relative_or_absolute_path(
            raw_value["vendor_root"],
            config_path=config_path,
            field_name=f"{table_name}.vendor_root",
        ),
        log_dir=_validate_relative_or_absolute_path(
            raw_value["log_dir"],
            config_path=config_path,
            field_name=f"{table_name}.log_dir",
        ),
        llm_provider=_validate_non_blank_string(
            raw_value["llm_provider"],
            field_name=f"{table_name}.llm_provider",
        ),
        llm_model_name=_validate_non_blank_string(
            raw_value["llm_model_name"],
            field_name=f"{table_name}.llm_model_name",
        ),
        llm_api_key=_validate_env_reference(
            raw_value["llm_api_key_env"],
            field_name=f"{table_name}.llm_api_key_env",
        ),
        llm_base_url=_validate_non_blank_string(
            raw_value["llm_base_url"],
            field_name=f"{table_name}.llm_base_url",
        ),
        llm_reasoning_effort=normalize_mirothinker_reasoning_effort(
            raw_value.get("llm_reasoning_effort"),
            field_name=f"{table_name}.llm_reasoning_effort",
            error_type=BootstrapConfigError,
        ),
        llm_max_context_length=_validate_optional_positive_int_with_default(
            raw_value.get("llm_max_context_length"),
            field_name=f"{table_name}.llm_max_context_length",
            default=default_llm_max_context_length,
        ),
    )


def _validate_optional_validation_config(
    raw_value: object,
    *,
    config_path: Path,
) -> ValidationConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError("Config field 'validation' must be a TOML table.")

    missing_fields = [
        field_name for field_name in _VALIDATION_REQUIRED_FIELDS if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            f"Missing required validation config fields: {missing}"
        )

    extra_fields = sorted(set(raw_value) - set(_VALIDATION_REQUIRED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(f"Unknown validation config fields: {unknown}")

    return ValidationConfig(
        market_data=_validate_validation_market_data_config(
            raw_value["market_data"],
            config_path=config_path,
        ),
        market_mappings=_validate_validation_market_mappings(raw_value["market_mappings"]),
    )


def _validate_optional_artifact_retention_config(
    raw_value: object,
    *,
    config_path: Path,
) -> ArtifactRetentionConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'artifact_retention' must be a TOML table."
        )

    missing_fields = [
        field_name
        for field_name in _ARTIFACT_RETENTION_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            f"Missing required artifact_retention config fields: {missing}"
        )

    extra_fields = sorted(
        set(raw_value) - set(_ARTIFACT_RETENTION_REQUIRED_FIELDS)
    )
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown artifact_retention config fields: {unknown}"
        )

    return ArtifactRetentionConfig(
        analysis_debug_log_days=_validate_positive_int(
            raw_value["analysis_debug_log_days"],
            field_name="artifact_retention.analysis_debug_log_days",
        ),
        pm_review_debug_log_days=_validate_positive_int(
            raw_value["pm_review_debug_log_days"],
            field_name="artifact_retention.pm_review_debug_log_days",
        ),
        checker_debug_log_days=_validate_positive_int(
            raw_value["checker_debug_log_days"],
            field_name="artifact_retention.checker_debug_log_days",
        ),
        search_debug_log_days=_validate_positive_int(
            raw_value["search_debug_log_days"],
            field_name="artifact_retention.search_debug_log_days",
        ),
        reflection_debug_log_days=_validate_positive_int(
            raw_value["reflection_debug_log_days"],
            field_name="artifact_retention.reflection_debug_log_days",
        ),
    )


def _validate_execution_config(raw_value: object) -> ExecutionConfig:
    if raw_value is None:
        return _default_execution_config()
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError("Config field 'execution' must be a TOML table.")

    extra_fields = sorted(set(raw_value) - set(_EXECUTION_ALLOWED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(f"Unknown execution config fields: {unknown}")

    buy_cost_bps, sell_cost_bps = _resolve_execution_side_cost_pair(
        raw_value,
        field_prefix="execution",
        default_buy_cost_bps=_DEFAULT_EXECUTION_BUY_COST_BPS,
        default_sell_cost_bps=_DEFAULT_EXECUTION_SELL_COST_BPS,
    )

    return ExecutionConfig(
        mode=_validate_execution_mode(
            raw_value.get("mode", _DEFAULT_EXECUTION_MODE),
            field_name="execution.mode",
        ),
        price_basis=_validate_execution_price_basis(
            raw_value.get("price_basis", _DEFAULT_EXECUTION_PRICE_BASIS),
            field_name="execution.price_basis",
        ),
        buy_cost_bps=buy_cost_bps,
        sell_cost_bps=sell_cost_bps,
        target_cost_overrides=_validate_execution_target_cost_overrides(
            raw_value.get("target_cost_overrides"),
            default_buy_cost_bps=buy_cost_bps,
            default_sell_cost_bps=sell_cost_bps,
        ),
        missing_bar_policy=_validate_execution_missing_bar_policy(
            raw_value.get(
                "missing_bar_policy",
                _DEFAULT_EXECUTION_MISSING_BAR_POLICY,
            ),
            field_name="execution.missing_bar_policy",
        ),
        max_next_bar_wait_hours=_validate_positive_int(
            raw_value.get(
                "max_next_bar_wait_hours",
                _DEFAULT_EXECUTION_MAX_NEXT_BAR_WAIT_HOURS,
            ),
            field_name="execution.max_next_bar_wait_hours",
        ),
    )


def _validate_execution_target_cost_overrides(
    raw_value: object,
    *,
    default_buy_cost_bps: float,
    default_sell_cost_bps: float,
) -> dict[str, ExecutionTargetCostOverrideConfig]:
    if raw_value is None:
        return {}
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'execution.target_cost_overrides' must be a TOML table."
        )
    overrides: dict[str, ExecutionTargetCostOverrideConfig] = {}
    for raw_target_key, override_value in raw_value.items():
        target_key = validate_target_key(raw_target_key, error_type=BootstrapConfigError)
        if not isinstance(override_value, dict):
            raise BootstrapConfigError(
                "Config field 'execution.target_cost_overrides."
                f"{target_key}' must be a TOML table."
            )
        extra_fields = sorted(
            set(override_value) - set(_EXECUTION_TARGET_COST_OVERRIDE_ALLOWED_FIELDS)
        )
        if extra_fields:
            unknown = ", ".join(extra_fields)
            raise BootstrapConfigError(
                "Unknown execution.target_cost_overrides."
                f"{target_key} config fields: {unknown}"
            )
        if not any(
            field_name in override_value
            for field_name in _EXECUTION_TARGET_COST_OVERRIDE_ALLOWED_FIELDS
        ):
            raise BootstrapConfigError(
                "Config field 'execution.target_cost_overrides."
                f"{target_key}' must define buy/sell or legacy slippage/commission cost fields."
            )
        buy_cost_bps, sell_cost_bps = _resolve_execution_side_cost_pair(
            override_value,
            field_prefix=f"execution.target_cost_overrides.{target_key}",
            default_buy_cost_bps=default_buy_cost_bps,
            default_sell_cost_bps=default_sell_cost_bps,
        )
        overrides[target_key] = ExecutionTargetCostOverrideConfig(
            buy_cost_bps=buy_cost_bps,
            sell_cost_bps=sell_cost_bps,
        )
    return overrides


def _resolve_execution_side_cost_pair(
    raw_value: Mapping[str, object],
    *,
    field_prefix: str,
    default_buy_cost_bps: float,
    default_sell_cost_bps: float,
) -> tuple[float, float]:
    has_buy = "buy_cost_bps" in raw_value
    has_sell = "sell_cost_bps" in raw_value
    has_legacy = "slippage_bps" in raw_value or "commission_bps" in raw_value
    if has_buy != has_sell:
        raise BootstrapConfigError(
            f"Config field '{field_prefix}' must set both buy_cost_bps and sell_cost_bps together."
        )
    if (has_buy or has_sell) and has_legacy:
        raise BootstrapConfigError(
            f"Config field '{field_prefix}' cannot mix buy/sell cost fields with legacy "
            "slippage/commission fields."
        )
    if has_buy:
        return (
            _validate_execution_non_negative_float(
                raw_value["buy_cost_bps"],
                field_name=f"{field_prefix}.buy_cost_bps",
            ),
            _validate_execution_non_negative_float(
                raw_value["sell_cost_bps"],
                field_name=f"{field_prefix}.sell_cost_bps",
            ),
        )
    if has_legacy:
        legacy_total = _validate_execution_non_negative_float(
            raw_value.get("slippage_bps", _DEFAULT_EXECUTION_SLIPPAGE_BPS),
            field_name=f"{field_prefix}.slippage_bps",
        ) + _validate_execution_non_negative_float(
            raw_value.get("commission_bps", _DEFAULT_EXECUTION_COMMISSION_BPS),
            field_name=f"{field_prefix}.commission_bps",
        )
        return legacy_total, legacy_total
    return default_buy_cost_bps, default_sell_cost_bps


def _validate_pm_review_runtime_config(raw_value: object) -> PMReviewRuntimeConfig:
    if raw_value is None:
        return _default_pm_review_runtime_config()
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'pm_review_runtime' must be a TOML table."
        )

    extra_fields = sorted(set(raw_value) - set(_PM_REVIEW_RUNTIME_ALLOWED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown pm_review_runtime config fields: {unknown}"
        )
    auto_dispatch = _validate_bool(
        raw_value.get(
            "auto_dispatch_after_analysis",
            _DEFAULT_PM_REVIEW_AUTO_DISPATCH_AFTER_ANALYSIS,
        ),
        field_name="pm_review_runtime.auto_dispatch_after_analysis",
    )
    execute_pm_decisions = _validate_bool(
        raw_value.get(
            "execute_pm_decisions",
            _DEFAULT_PM_REVIEW_EXECUTE_PM_DECISIONS,
        ),
        field_name="pm_review_runtime.execute_pm_decisions",
    )
    if execute_pm_decisions and not auto_dispatch:
        raise BootstrapConfigError(
            "pm_review_runtime.execute_pm_decisions=true requires "
            "auto_dispatch_after_analysis=true."
        )
    return PMReviewRuntimeConfig(
        auto_dispatch_after_analysis=auto_dispatch,
        execute_pm_decisions=execute_pm_decisions,
        require_workspace_ready=_validate_bool(
            raw_value.get(
                "require_workspace_ready",
                _DEFAULT_PM_REVIEW_REQUIRE_WORKSPACE_READY,
            ),
            field_name="pm_review_runtime.require_workspace_ready",
        ),
        position_review_gate_enabled=_validate_bool(
            raw_value.get(
                "position_review_gate_enabled",
                _DEFAULT_PM_POSITION_REVIEW_GATE_ENABLED,
            ),
            field_name="pm_review_runtime.position_review_gate_enabled",
        ),
        position_review_cooldown_minutes=_validate_positive_int(
            raw_value.get(
                "position_review_cooldown_minutes",
                _DEFAULT_PM_POSITION_REVIEW_COOLDOWN_MINUTES,
            ),
            field_name="pm_review_runtime.position_review_cooldown_minutes",
        ),
        position_review_rearm_buffer_bps=_validate_positive_float_config(
            raw_value.get(
                "position_review_rearm_buffer_bps",
                _DEFAULT_PM_POSITION_REVIEW_REARM_BUFFER_BPS,
            ),
            field_name="pm_review_runtime.position_review_rearm_buffer_bps",
        ),
    )


def _validate_runtime_workers_config(raw_value: object) -> RuntimeWorkersConfig:
    if raw_value is None:
        return _default_runtime_workers_config()
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'runtime_workers' must be a TOML table."
        )

    extra_fields = sorted(set(raw_value) - set(_RUNTIME_WORKERS_ALLOWED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown runtime_workers config fields: {unknown}"
        )
    return RuntimeWorkersConfig(
        checker_concurrency=_validate_positive_int(
            raw_value.get(
                "checker_concurrency",
                _DEFAULT_RUNTIME_WORKER_CONCURRENCY,
            ),
            field_name="runtime_workers.checker_concurrency",
        ),
        analysis_concurrency=_validate_positive_int(
            raw_value.get(
                "analysis_concurrency",
                _DEFAULT_RUNTIME_WORKER_CONCURRENCY,
            ),
            field_name="runtime_workers.analysis_concurrency",
        ),
        pm_review_concurrency=_validate_positive_int(
            raw_value.get(
                "pm_review_concurrency",
                _DEFAULT_RUNTIME_WORKER_CONCURRENCY,
            ),
            field_name="runtime_workers.pm_review_concurrency",
        ),
    )


def _validate_optional_stream_routing_config(
    raw_value: object,
) -> StreamRoutingConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError("Config field 'stream_routing' must be a TOML table.")
    try:
        return StreamRoutingConfig.from_json_payload(raw_value)
    except CEAUContractError as exc:
        raise BootstrapConfigError(f"stream_routing config is invalid: {exc}") from exc


def _kernel_config_hash(raw_config: dict[str, object]) -> str:
    encoded = json.dumps(
        raw_config,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _ensure_audit_path(value: object, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise BootstrapConfigError(f"{field_name} must be a Path.")
    return value.expanduser().resolve(strict=False)


def _validate_audit_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BootstrapConfigError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _validate_audit_sha256(value: object, field_name: str) -> str:
    normalized = _validate_audit_text(value, field_name)
    if not _SHA256_RE.fullmatch(normalized):
        raise BootstrapConfigError(f"{field_name} must be a lowercase SHA-256 digest.")
    return normalized


def _require_audit_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BootstrapConfigError(f"{field_name} must be a JSON object.")
    return value


def _require_audit_text(data: Mapping[str, object], field_name: str) -> str:
    return _validate_audit_text(data.get(field_name), field_name)


def _optional_audit_text(data: Mapping[str, object], field_name: str) -> str | None:
    value = data.get(field_name)
    if value is None:
        return None
    return _validate_audit_text(value, field_name)


def _validate_execution_mode(raw_value: object, *, field_name: str) -> ExecutionMode:
    if raw_value != "paper":
        raise BootstrapConfigError(f"Config field '{field_name}' must be 'paper'.")
    return "paper"


def _validate_execution_price_basis(
    raw_value: object,
    *,
    field_name: str,
) -> ExecutionPriceBasis:
    if raw_value not in {"open", "close"}:
        raise BootstrapConfigError(f"Config field '{field_name}' must be 'open' or 'close'.")
    return cast(ExecutionPriceBasis, raw_value)


def _validate_execution_missing_bar_policy(
    raw_value: object,
    *,
    field_name: str,
) -> ExecutionMissingBarPolicy:
    if raw_value != "reject":
        raise BootstrapConfigError(f"Config field '{field_name}' must be 'reject'.")
    return "reject"


def _validate_execution_non_negative_float(
    raw_value: object,
    *,
    field_name: str,
) -> float:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a non-negative number."
        )
    value = float(raw_value)
    if not isfinite(value) or value < 0.0:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be greater than or equal to zero."
        )
    return value


def _validate_positive_float_config(raw_value: object, *, field_name: str) -> float:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a positive number."
        )
    value = float(raw_value)
    if not isfinite(value) or value <= 0.0:
        raise BootstrapConfigError(f"Config field '{field_name}' must be greater than zero.")
    return value


def _validate_optional_market_context_config(raw_value: object) -> MarketContextConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'market_context' must be a TOML table."
        )

    missing_fields = [
        field_name
        for field_name in _MARKET_CONTEXT_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            f"Missing required market_context config fields: {missing}"
        )

    extra_fields = sorted(set(raw_value) - set(_MARKET_CONTEXT_ALLOWED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(f"Unknown market_context config fields: {unknown}")

    enabled = _validate_bool(raw_value["enabled"], field_name="market_context.enabled")
    return MarketContextConfig(
        enabled=enabled,
        max_prompt_chars=_validate_positive_int(
            raw_value["max_prompt_chars"],
            field_name="market_context.max_prompt_chars",
        ),
        target_profiles=_validate_market_context_target_profiles(
            raw_value.get("target_profiles")
        ),
    )


def _validate_market_context_target_profiles(
    raw_value: object,
) -> dict[str, MarketContextTargetProfileConfig]:
    if raw_value is None:
        return {}
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'market_context.target_profiles' must be a TOML table."
        )
    profiles: dict[str, MarketContextTargetProfileConfig] = {}
    for raw_target_key, profile_value in raw_value.items():
        target_key = validate_target_key(raw_target_key, error_type=BootstrapConfigError)
        profiles[target_key] = _validate_market_context_target_profile(
            profile_value,
            target_key=target_key,
        )
    return profiles


def _validate_market_context_target_profile(
    raw_value: object,
    *,
    target_key: str,
) -> MarketContextTargetProfileConfig:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            f"Config field 'market_context.target_profiles.{target_key}' must be a TOML table."
        )
    missing_fields = [
        field_name
        for field_name in _MARKET_CONTEXT_PROFILE_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            "Missing required market_context.target_profiles."
            f"{target_key} config fields: {missing}"
        )
    extra_fields = sorted(set(raw_value) - set(_MARKET_CONTEXT_PROFILE_ALLOWED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown market_context.target_profiles.{target_key} config fields: {unknown}"
        )
    bar_granularity = _validate_market_context_bar_granularity(
        raw_value["bar_granularity"],
        field_name=f"market_context.target_profiles.{target_key}.bar_granularity",
    )
    market_data_subscriptions: tuple[MarketDataSubscriptionConfig, ...] = ()
    if "market_data_subscriptions" in raw_value:
        market_data_subscriptions = _validate_live_market_data_subscriptions(
            raw_value["market_data_subscriptions"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.market_data_subscriptions"
            ),
            expected_bar_granularity=bar_granularity,
            expected_bar_granularity_field_name=(
                "market_context.target_profiles."
                f"{target_key}.bar_granularity"
            ),
        )

    enabled_components = _validate_market_context_components(
        raw_value["enabled_components"],
        field_name=f"market_context.target_profiles.{target_key}.enabled_components",
    )
    price_volume = None
    if "price_volume" in raw_value:
        price_volume = _validate_market_context_price_volume_config(
            raw_value["price_volume"],
            target_key=target_key,
        )
    market_path = _validate_market_context_market_path_config(
        raw_value.get("market_path"),
        target_key=target_key,
    )
    technical = None
    if "technical" in raw_value:
        technical = _validate_market_context_technical_config(
            raw_value["technical"],
            target_key=target_key,
        )
    derivatives = None
    if "derivatives" in raw_value:
        derivatives = _validate_market_context_derivatives_config(
            raw_value["derivatives"],
            target_key=target_key,
        )
    macro_cross_asset = None
    if "macro_cross_asset" in raw_value:
        macro_cross_asset = _validate_market_context_macro_cross_asset_config(
            raw_value["macro_cross_asset"],
            target_key=target_key,
        )
    if "price_volume" in enabled_components and price_volume is None:
        raise BootstrapConfigError(
            f"market_context.target_profiles.{target_key}.price_volume is required "
            "when price_volume is enabled."
        )
    if "technical" in enabled_components and technical is None:
        raise BootstrapConfigError(
            f"market_context.target_profiles.{target_key}.technical is required "
            "when technical is enabled."
        )
    if "derivatives" in enabled_components:
        if derivatives is None:
            raise BootstrapConfigError(
                f"market_context.target_profiles.{target_key}.derivatives is required "
                "when derivatives is enabled."
            )
        if not derivatives.enabled:
            raise BootstrapConfigError(
                f"market_context.target_profiles.{target_key}.derivatives.enabled must "
                "be true when derivatives is in enabled_components."
            )
    if "macro_cross_asset" in enabled_components:
        if macro_cross_asset is None:
            raise BootstrapConfigError(
                f"market_context.target_profiles.{target_key}.macro_cross_asset is "
                "required when macro_cross_asset is enabled."
            )
        if not macro_cross_asset.enabled:
            raise BootstrapConfigError(
                "market_context.target_profiles."
                f"{target_key}.macro_cross_asset.enabled must be true when "
                "macro_cross_asset is in enabled_components."
            )
    derivatives_underlying_symbol = None
    if "derivatives_underlying_symbol" in raw_value:
        derivatives_underlying_symbol = _validate_non_blank_string(
            raw_value["derivatives_underlying_symbol"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives_underlying_symbol"
            ),
        )
    if "derivatives" in enabled_components and derivatives_underlying_symbol is None:
        raise BootstrapConfigError(
            "market_context.target_profiles."
            f"{target_key}.derivatives_underlying_symbol is required when "
            "derivatives is enabled."
        )

    return MarketContextTargetProfileConfig(
        tradable_proxy_symbol=_validate_non_blank_string(
            raw_value["tradable_proxy_symbol"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.tradable_proxy_symbol"
            ),
        ),
        derivatives_underlying_symbol=derivatives_underlying_symbol,
        bar_granularity=bar_granularity,
        market_data_subscriptions=market_data_subscriptions,
        enabled_components=enabled_components,
        price_volume=price_volume,
        market_path=market_path,
        technical=technical,
        derivatives=derivatives,
        macro_cross_asset=macro_cross_asset,
    )


def _validate_market_context_price_volume_config(
    raw_value: object,
    *,
    target_key: str,
) -> MarketContextPriceVolumeConfig:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'market_context.target_profiles."
            f"{target_key}.price_volume' must be a TOML table."
        )
    missing_fields = [
        field_name
        for field_name in _MARKET_CONTEXT_PRICE_VOLUME_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            "Missing required market_context.target_profiles."
            f"{target_key}.price_volume config fields: {missing}"
        )
    extra_fields = sorted(
        set(raw_value) - set(_MARKET_CONTEXT_PRICE_VOLUME_REQUIRED_FIELDS)
    )
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            "Unknown market_context.target_profiles."
            f"{target_key}.price_volume config fields: {unknown}"
        )
    return MarketContextPriceVolumeConfig(
        lookback_bars=_validate_positive_int(
            raw_value["lookback_bars"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.price_volume.lookback_bars"
            ),
        ),
        trailing_return_windows=_validate_market_context_return_windows(
            raw_value["trailing_return_windows"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.price_volume.trailing_return_windows"
            ),
        ),
    )


def _validate_market_context_market_path_config(
    raw_value: object,
    *,
    target_key: str,
) -> MarketContextMarketPathConfig:
    if raw_value is None:
        return MarketContextMarketPathConfig()
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'market_context.target_profiles."
            f"{target_key}.market_path' must be a TOML table."
        )
    extra_fields = sorted(
        set(raw_value) - set(_MARKET_CONTEXT_MARKET_PATH_ALLOWED_FIELDS)
    )
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            "Unknown market_context.target_profiles."
            f"{target_key}.market_path config fields: {unknown}"
        )
    session = raw_value.get("session")
    if session is not None:
        session = _validate_validation_market_session(
            session,
            field_name=f"market_context.target_profiles.{target_key}.market_path.session",
        )
    return MarketContextMarketPathConfig(
        enabled=_validate_bool(
            raw_value.get("enabled", True),
            field_name=f"market_context.target_profiles.{target_key}.market_path.enabled",
        ),
        session=session,
        range_lookback_calendar_days=_validate_positive_int(
            raw_value.get("range_lookback_calendar_days", 20),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.market_path.range_lookback_calendar_days"
            ),
        ),
        range_lookback_sessions=_validate_positive_int(
            raw_value.get("range_lookback_sessions", 20),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.market_path.range_lookback_sessions"
            ),
        ),
        range_lookback_daily_bars=_validate_positive_int(
            raw_value.get("range_lookback_daily_bars", 60),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.market_path.range_lookback_daily_bars"
            ),
        ),
        atr_period=_validate_positive_int(
            raw_value.get("atr_period", 14),
            field_name=f"market_context.target_profiles.{target_key}.market_path.atr_period",
        ),
        atr_multiple=_validate_positive_float_config(
            raw_value.get("atr_multiple", 2.0),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.market_path.atr_multiple"
            ),
        ),
        pct_floor=_validate_positive_float_config(
            raw_value.get("pct_floor", 0.025),
            field_name=f"market_context.target_profiles.{target_key}.market_path.pct_floor",
        ),
        min_leg_bars=_validate_positive_int(
            raw_value.get("min_leg_bars", 3),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.market_path.min_leg_bars"
            ),
        ),
        large_move_pct=_validate_positive_float_config(
            raw_value.get("large_move_pct", 0.08),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.market_path.large_move_pct"
            ),
        ),
        near_extreme_pct=_validate_positive_float_config(
            raw_value.get("near_extreme_pct", 0.03),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.market_path.near_extreme_pct"
            ),
        ),
    )


def _validate_market_context_technical_config(
    raw_value: object,
    *,
    target_key: str,
) -> MarketContextTechnicalConfig:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'market_context.target_profiles."
            f"{target_key}.technical' must be a TOML table."
        )
    missing_fields = [
        field_name
        for field_name in _MARKET_CONTEXT_TECHNICAL_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            "Missing required market_context.target_profiles."
            f"{target_key}.technical config fields: {missing}"
        )
    extra_fields = sorted(set(raw_value) - set(_MARKET_CONTEXT_TECHNICAL_ALLOWED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            "Unknown market_context.target_profiles."
            f"{target_key}.technical config fields: {unknown}"
        )
    ema_periods = _validate_positive_int_tuple(
        raw_value["ema_periods"],
        field_name=f"market_context.target_profiles.{target_key}.technical.ema_periods",
    )
    if 136 not in ema_periods:
        raise BootstrapConfigError(
            "Config field 'market_context.target_profiles."
            f"{target_key}.technical.ema_periods' must include 136."
        )
    rsi_period = _validate_positive_int(
        raw_value.get("rsi_period", 14),
        field_name=(
            "market_context.target_profiles."
            f"{target_key}.technical.rsi_period"
        ),
    )
    rsi_overbought = _validate_rsi_threshold(
        raw_value.get("rsi_overbought", 70.0),
        field_name=(
            "market_context.target_profiles."
            f"{target_key}.technical.rsi_overbought"
        ),
    )
    rsi_oversold = _validate_rsi_threshold(
        raw_value.get("rsi_oversold", 30.0),
        field_name=(
            "market_context.target_profiles."
            f"{target_key}.technical.rsi_oversold"
        ),
    )
    if rsi_oversold >= rsi_overbought:
        raise BootstrapConfigError(
            "Config field 'market_context.target_profiles."
            f"{target_key}.technical.rsi_oversold' must be less than rsi_overbought."
        )
    divergence_lookback_bars = _validate_positive_int(
        raw_value.get("divergence_lookback_bars", 50),
        field_name=(
            "market_context.target_profiles."
            f"{target_key}.technical.divergence_lookback_bars"
        ),
    )
    divergence_pivot_window = _validate_positive_int(
        raw_value.get("divergence_pivot_window", 2),
        field_name=(
            "market_context.target_profiles."
            f"{target_key}.technical.divergence_pivot_window"
        ),
    )
    if divergence_lookback_bars < rsi_period + 1:
        raise BootstrapConfigError(
            "Config field 'market_context.target_profiles."
            f"{target_key}.technical.divergence_lookback_bars' must be >= "
            "rsi_period + 1."
        )
    if divergence_lookback_bars < divergence_pivot_window * 2 + 1:
        raise BootstrapConfigError(
            "Config field 'market_context.target_profiles."
            f"{target_key}.technical.divergence_lookback_bars' must be >= "
            "divergence_pivot_window * 2 + 1."
        )
    return MarketContextTechnicalConfig(
        ema_periods=ema_periods,
        ichimoku_enabled=_validate_bool(
            raw_value["ichimoku_enabled"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.technical.ichimoku_enabled"
            ),
        ),
        bollinger_period=_validate_positive_int(
            raw_value.get("bollinger_period", 20),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.technical.bollinger_period"
            ),
        ),
        bollinger_stddev=_validate_positive_number(
            raw_value.get("bollinger_stddev", 2.0),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.technical.bollinger_stddev"
            ),
            unit_name="standard deviations",
        ),
        rsi_period=rsi_period,
        rsi_overbought=rsi_overbought,
        rsi_oversold=rsi_oversold,
        divergence_lookback_bars=divergence_lookback_bars,
        divergence_pivot_window=divergence_pivot_window,
    )


def _validate_rsi_threshold(raw_value: object, *, field_name: str) -> float:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        raise BootstrapConfigError(f"Config field '{field_name}' must be an RSI value.")
    value = float(raw_value)
    if not isfinite(value) or value < 0.0 or value > 100.0:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be between 0 and 100."
        )
    return value


def _validate_market_context_derivatives_config(
    raw_value: object,
    *,
    target_key: str,
) -> MarketContextDerivativesConfig:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'market_context.target_profiles."
            f"{target_key}.derivatives' must be a TOML table."
        )
    missing_fields = [
        field_name
        for field_name in _MARKET_CONTEXT_DERIVATIVES_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            "Missing required market_context.target_profiles."
            f"{target_key}.derivatives config fields: {missing}"
        )
    extra_fields = sorted(
        set(raw_value) - set(_MARKET_CONTEXT_DERIVATIVES_ALLOWED_FIELDS)
    )
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            "Unknown market_context.target_profiles."
            f"{target_key}.derivatives config fields: {unknown}"
        )
    expiry_days_min = _validate_non_negative_int_config(
        raw_value["expiry_days_min"],
        field_name=(
            "market_context.target_profiles."
            f"{target_key}.derivatives.expiry_days_min"
        ),
    )
    expiry_days_max = _validate_non_negative_int_config(
        raw_value["expiry_days_max"],
        field_name=(
            "market_context.target_profiles."
            f"{target_key}.derivatives.expiry_days_max"
        ),
    )
    if expiry_days_min > expiry_days_max:
        raise BootstrapConfigError(
            "Config field 'market_context.target_profiles."
            f"{target_key}.derivatives.expiry_days_min' must be <= expiry_days_max."
        )
    return MarketContextDerivativesConfig(
        enabled=_validate_bool(
            raw_value["enabled"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.enabled"
            ),
        ),
        data_feed=_validate_non_blank_string(
            raw_value["data_feed"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.data_feed"
            ),
        ),
        chain_max_rows=_validate_positive_int(
            raw_value["chain_max_rows"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.chain_max_rows"
            ),
        ),
        max_contracts_per_snapshot=_validate_positive_int(
            raw_value["max_contracts_per_snapshot"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.max_contracts_per_snapshot"
            ),
        ),
        expiry_days_min=expiry_days_min,
        expiry_days_max=expiry_days_max,
        strike_pct_window=_validate_positive_number(
            raw_value["strike_pct_window"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.strike_pct_window"
            ),
            unit_name="ratio",
        ),
        atm_contract_count=_validate_non_negative_int_config(
            raw_value["atm_contract_count"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.atm_contract_count"
            ),
        ),
        otm_contract_count=_validate_non_negative_int_config(
            raw_value["otm_contract_count"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.otm_contract_count"
            ),
        ),
        min_trade_count=_validate_non_negative_int_config(
            raw_value["min_trade_count"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.min_trade_count"
            ),
        ),
        large_trade_notional_threshold=_validate_positive_number(
            raw_value["large_trade_notional_threshold"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.large_trade_notional_threshold"
            ),
            unit_name="notional",
        ),
        include_historical_trades=_validate_bool(
            raw_value["include_historical_trades"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.include_historical_trades"
            ),
        ),
        include_historical_bars=_validate_bool(
            raw_value["include_historical_bars"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.include_historical_bars"
            ),
        ),
        include_latest_snapshot_quotes=_validate_bool(
            raw_value["include_latest_snapshot_quotes"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.include_latest_snapshot_quotes"
            ),
        ),
        bars_granularity=_validate_market_context_bar_granularity(
            raw_value["bars_granularity"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.bars_granularity"
            ),
        ),
        lookback_hours=_validate_positive_int(
            raw_value["lookback_hours"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.lookback_hours"
            ),
        ),
        include_latest_trades=_validate_bool(
            raw_value.get("include_latest_trades", True),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.include_latest_trades"
            ),
        ),
        include_latest_quotes=_validate_bool(
            raw_value.get("include_latest_quotes", True),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.include_latest_quotes"
            ),
        ),
        historical_universe_reconstruction_enabled=_validate_bool(
            raw_value.get("historical_universe_reconstruction_enabled", True),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.historical_universe_reconstruction_enabled"
            ),
        ),
        historical_universe_max_candidates_per_bucket=_validate_positive_int(
            raw_value.get("historical_universe_max_candidates_per_bucket", 300),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.historical_universe_max_candidates_per_bucket"
            ),
        ),
        historical_universe_strike_step=_validate_positive_number(
            raw_value.get("historical_universe_strike_step", 1.0),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.historical_universe_strike_step"
            ),
            unit_name="strike step",
        ),
        unusual_baseline_windows_days=_validate_positive_int_tuple(
            raw_value.get("unusual_baseline_windows_days", [7, 14]),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.unusual_baseline_windows_days"
            ),
        ),
        unusual_min_baseline_observations=_validate_positive_int(
            raw_value.get("unusual_min_baseline_observations", 3),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.unusual_min_baseline_observations"
            ),
        ),
        unusual_elevated_ratio=_validate_positive_number(
            raw_value.get("unusual_elevated_ratio", 2.0),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.unusual_elevated_ratio"
            ),
            unit_name="ratio",
        ),
        unusual_unusual_ratio=_validate_positive_number(
            raw_value.get("unusual_unusual_ratio", 5.0),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.unusual_unusual_ratio"
            ),
            unit_name="ratio",
        ),
        unusual_extreme_ratio=_validate_positive_number(
            raw_value.get("unusual_extreme_ratio", 10.0),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.unusual_extreme_ratio"
            ),
            unit_name="ratio",
        ),
        notable_contract_limit=_validate_positive_int(
            raw_value.get("notable_contract_limit", 5),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.derivatives.notable_contract_limit"
            ),
        ),
    )


def _validate_market_context_macro_cross_asset_config(
    raw_value: object,
    *,
    target_key: str,
) -> MarketContextMacroCrossAssetConfig:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'market_context.target_profiles."
            f"{target_key}.macro_cross_asset' must be a TOML table."
        )
    missing_fields = [
        field_name
        for field_name in _MARKET_CONTEXT_MACRO_CROSS_ASSET_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            "Missing required market_context.target_profiles."
            f"{target_key}.macro_cross_asset config fields: {missing}"
        )
    extra_fields = sorted(
        set(raw_value) - set(_MARKET_CONTEXT_MACRO_CROSS_ASSET_ALLOWED_FIELDS)
    )
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            "Unknown market_context.target_profiles."
            f"{target_key}.macro_cross_asset config fields: {unknown}"
        )
    return MarketContextMacroCrossAssetConfig(
        enabled=_validate_bool(
            raw_value["enabled"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.macro_cross_asset.enabled"
            ),
        ),
        lookback_bars=_validate_positive_int(
            raw_value["lookback_bars"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.macro_cross_asset.lookback_bars"
            ),
        ),
        trailing_return_windows=_validate_market_context_return_windows(
            raw_value["trailing_return_windows"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.macro_cross_asset.trailing_return_windows"
            ),
        ),
        proxy_groups=_validate_market_context_proxy_groups(
            raw_value["proxy_groups"],
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.macro_cross_asset.proxy_groups"
            ),
        ),
        group_pressure_rules=_validate_market_context_pressure_rules(
            raw_value.get("group_pressure_rules", {}),
            field_name=(
                "market_context.target_profiles."
                f"{target_key}.macro_cross_asset.group_pressure_rules"
            ),
        ),
    )


def _validate_market_context_return_windows(
    raw_value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    values = _validate_non_empty_string_tuple(raw_value, field_name=field_name)
    normalized: list[str] = []
    for value in values:
        if value not in _ALLOWED_MARKET_CONTEXT_RETURN_WINDOWS:
            allowed = ", ".join(_ALLOWED_MARKET_CONTEXT_RETURN_WINDOWS)
            raise BootstrapConfigError(
                f"Config field '{field_name}' contains unsupported window "
                f"{value!r}; supported values: {allowed}."
            )
        if value in normalized:
            raise BootstrapConfigError(
                f"Config field '{field_name}' must not contain duplicate strings."
            )
        normalized.append(value)
    return tuple(normalized)


def _validate_market_context_proxy_groups(
    raw_value: object,
    *,
    field_name: str,
) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a TOML table."
        )
    if not raw_value:
        raise BootstrapConfigError(f"Config field '{field_name}' must not be empty.")
    groups: dict[str, tuple[str, ...]] = {}
    for raw_group, raw_symbols in raw_value.items():
        group = _validate_non_blank_string(
            raw_group,
            field_name=f"{field_name}.<group>",
        )
        symbols = _validate_non_empty_string_tuple(
            raw_symbols,
            field_name=f"{field_name}.{group}",
        )
        groups[group] = symbols
    return groups


def _validate_market_context_pressure_rules(
    raw_value: object,
    *,
    field_name: str,
) -> dict[str, str]:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a TOML table."
        )
    rules: dict[str, str] = {}
    for raw_group, raw_rule in raw_value.items():
        group = _validate_non_blank_string(
            raw_group,
            field_name=f"{field_name}.<group>",
        )
        rule = _validate_non_blank_string(
            raw_rule,
            field_name=f"{field_name}.{group}",
        )
        if rule not in _MARKET_CONTEXT_MACRO_CROSS_ASSET_PRESSURE_RULES:
            allowed = ", ".join(_MARKET_CONTEXT_MACRO_CROSS_ASSET_PRESSURE_RULES)
            raise BootstrapConfigError(
                f"Config field '{field_name}.{group}' must be one of: {allowed}."
            )
        rules[group] = rule
    return rules


def _validate_market_context_bar_granularity(raw_value: object, *, field_name: str) -> str:
    normalized = _validate_non_blank_string(raw_value, field_name=field_name)
    if _MARKET_CONTEXT_GRANULARITY_RE.fullmatch(normalized) is None:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must match '<N>m', '<N>min', '<N>h', or '<N>d'."
        )
    return normalized


def _validate_market_context_components(
    raw_value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    values = _validate_non_empty_string_tuple(raw_value, field_name=field_name)
    normalized: list[str] = []
    for value in values:
        if value not in _ALLOWED_MARKET_CONTEXT_COMPONENTS:
            allowed = ", ".join(_ALLOWED_MARKET_CONTEXT_COMPONENTS)
            raise BootstrapConfigError(
                f"Config field '{field_name}' contains unsupported component "
                f"{value!r}; supported values: {allowed}."
            )
        if value in normalized:
            raise BootstrapConfigError(
                f"Config field '{field_name}' must not contain duplicate strings."
            )
        normalized.append(value)
    return tuple(normalized)


def _validate_live_source_market_mappings(config: KernelConfig) -> None:
    if config.mode != "live" or config.live is None:
        return
    target_keys = tuple(
        dict.fromkeys(
            (
                *(
                    target.target_key
                    for target in (
                        ()
                        if config.live.web_search is None
                        else config.live.web_search.targets
                    )
                ),
                *(
                    target.target_key
                    for target in (
                        ()
                        if config.live.market_news is None
                        else config.live.market_news.targets
                    )
                ),
                *(
                    target.target_key
                    for target in (
                        ()
                        if config.live.market_data is None
                        or not config.live.market_data.enabled
                        else config.live.market_data.targets
                    )
                ),
            )
        )
    )
    if not target_keys:
        return
    if config.validation is None:
        joined = ", ".join(target_keys)
        raise BootstrapConfigError(
            "Live source targets require validation.market_mappings so "
            f"reflection can evaluate positions. Missing validation config for: {joined}"
        )
    missing = [
        target_key
        for target_key in target_keys
        if target_key not in config.validation.market_mappings
    ]
    if missing:
        joined = ", ".join(missing)
        raise BootstrapConfigError(
            "Live source targets require matching validation.market_mappings "
            f"entries. Missing mappings for: {joined}"
        )


def _validate_validation_market_data_config(
    raw_value: object,
    *,
    config_path: Path,
) -> ValidationMarketDataConfig:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError("Config field 'validation.market_data' must be a TOML table.")

    missing_fields = [
        field_name
        for field_name in _VALIDATION_MARKET_DATA_REQUIRED_FIELDS
        if field_name not in raw_value
    ]
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise BootstrapConfigError(
            "Missing required validation.market_data config fields: "
            f"{missing}"
        )

    extra_fields = sorted(set(raw_value) - set(_VALIDATION_MARKET_DATA_ALLOWED_FIELDS))
    if extra_fields:
        unknown = ", ".join(extra_fields)
        raise BootstrapConfigError(
            f"Unknown validation.market_data config fields: {unknown}"
        )
    provider = _validate_validation_market_data_provider(
        raw_value["provider"],
        field_name="validation.market_data.provider",
    )
    api_key = ""
    if provider != "futu_openapi":
        if "api_key_env" not in raw_value:
            raise BootstrapConfigError(
                "Missing required validation.market_data config fields: api_key_env"
            )
        api_key = _validate_env_reference(
            raw_value["api_key_env"],
            field_name="validation.market_data.api_key_env",
        )

    return ValidationMarketDataConfig(
        provider=provider,
        base_url=_validate_market_data_base_url(
            raw_value["base_url"],
            provider=provider,
            field_name="validation.market_data.base_url",
        ),
        api_key=api_key,
        timeout_seconds=_validate_positive_number(
            raw_value["timeout_seconds"],
            field_name="validation.market_data.timeout_seconds",
            unit_name="seconds",
        ),
        local_archive_root=(
            _validate_relative_or_absolute_path(
                raw_value["local_archive_root"],
                config_path=config_path,
                field_name="validation.market_data.local_archive_root",
            )
            if "local_archive_root" in raw_value
            else None
        ),
    )


def _validate_validation_market_data_provider(
    raw_value: object,
    *,
    field_name: str,
) -> ValidationMarketDataProvider:
    if not isinstance(raw_value, str):
        raise BootstrapConfigError(f"Config field '{field_name}' must be a string.")
    normalized = raw_value.strip()
    if normalized not in {"alpaca_like", "bc_private_v1", "futu_openapi"}:
        raise BootstrapConfigError(
            "Config field "
            f"'{field_name}' must be one of: alpaca_like, bc_private_v1, futu_openapi."
        )
    return cast(ValidationMarketDataProvider, normalized)


def _validate_validation_market_mappings(
    raw_value: object,
) -> dict[str, ValidationMarketMappingConfig]:
    if not isinstance(raw_value, dict):
        raise BootstrapConfigError(
            "Config field 'validation.market_mappings' must be a TOML table."
        )
    if not raw_value:
        raise BootstrapConfigError("Config field 'validation.market_mappings' must not be empty.")

    mappings: dict[str, ValidationMarketMappingConfig] = {}
    for raw_target_key, mapping_value in raw_value.items():
        validated_target_key = validate_target_key(
            raw_target_key,
            error_type=BootstrapConfigError,
        )
        if not isinstance(mapping_value, dict):
            raise BootstrapConfigError(
                "Config field 'validation.market_mappings."
                f"{validated_target_key}' must be a TOML table."
            )

        missing_fields = [
            field_name
            for field_name in _VALIDATION_MARKET_MAPPING_REQUIRED_FIELDS
            if field_name not in mapping_value
        ]
        if missing_fields:
            missing = ", ".join(missing_fields)
            raise BootstrapConfigError(
                "Missing required validation.market_mappings."
                f"{validated_target_key} config fields: {missing}"
            )

        extra_fields = sorted(
            set(mapping_value) - set(_VALIDATION_MARKET_MAPPING_ALLOWED_FIELDS)
        )
        if extra_fields:
            unknown = ", ".join(extra_fields)
            raise BootstrapConfigError(
                "Unknown validation.market_mappings."
                f"{validated_target_key} config fields: {unknown}"
            )

        market_session = _validate_validation_market_session(
            mapping_value["market_session"],
            field_name=(
                "validation.market_mappings."
                f"{validated_target_key}.market_session"
            ),
        )
        exchange: str | None = None
        if "exchange" in mapping_value:
            exchange = _validate_non_blank_string(
                mapping_value["exchange"],
                field_name=f"validation.market_mappings.{validated_target_key}.exchange",
            )
        exchange_session_scope: ValidationExchangeSessionScope | None = None
        if "exchange_session_scope" in mapping_value:
            exchange_session_scope = _validate_validation_exchange_session_scope(
                mapping_value["exchange_session_scope"],
                field_name=(
                    "validation.market_mappings."
                    f"{validated_target_key}.exchange_session_scope"
                ),
            )

        mapping_prefix = f"validation.market_mappings.{validated_target_key}"
        from event_trader.market.session_policy import validate_market_session_contract_fields

        validate_market_session_contract_fields(
            market_session=market_session,
            exchange=exchange,
            exchange_session_scope=exchange_session_scope,
            error_type=BootstrapConfigError,
            exchange_field_name=f"{mapping_prefix}.exchange",
            exchange_session_scope_field_name=f"{mapping_prefix}.exchange_session_scope",
            market_session_field_name=f"{mapping_prefix}.market_session",
            continuous_exchange_message=(
                f"Config field '{mapping_prefix}.exchange' must be absent when "
                "market_session = 'continuous'."
            ),
            continuous_exchange_session_scope_message=(
                f"Config field '{mapping_prefix}.exchange_session_scope' must be "
                "absent when market_session = 'continuous'."
            ),
            exchange_session_exchange_message=(
                f"Config field '{mapping_prefix}.exchange' is required when "
                "market_session = 'exchange_session'."
            ),
            exchange_session_scope_message=(
                f"Config field '{mapping_prefix}.exchange_session_scope' is required "
                "when market_session = 'exchange_session'."
            ),
        )

        mappings[validated_target_key] = ValidationMarketMappingConfig(
            market_symbol=_validate_non_blank_string(
                mapping_value["market_symbol"],
                field_name=(
                    "validation.market_mappings."
                    f"{validated_target_key}.market_symbol"
                ),
            ),
            market_session=market_session,
            exchange=exchange,
            bar_granularity=_validate_non_blank_string(
                mapping_value["bar_granularity"],
                field_name=(
                    "validation.market_mappings."
                    f"{validated_target_key}.bar_granularity"
                ),
            ),
            execution_direction_mode=(
                _validate_validation_execution_direction_mode(
                    mapping_value["execution_direction_mode"],
                    field_name=(
                        "validation.market_mappings."
                        f"{validated_target_key}.execution_direction_mode"
                    ),
                )
                if "execution_direction_mode" in mapping_value
                else "long_short"
            ),
            exchange_session_scope=exchange_session_scope,
            adjustment_policy=(
                _validate_market_data_adjustment_policy(
                    mapping_value["adjustment_policy"],
                    field_name=(
                        "validation.market_mappings."
                        f"{validated_target_key}.adjustment_policy"
                    ),
                )
                if "adjustment_policy" in mapping_value
                else None
            ),
        )

    return mappings


def _validate_validation_market_session(
    raw_value: object,
    *,
    field_name: str,
) -> ValidationMarketSession:
    if not isinstance(raw_value, str):
        raise BootstrapConfigError(f"Config field '{field_name}' must be a string.")
    if raw_value not in _ALLOWED_VALIDATION_MARKET_SESSIONS:
        allowed = ", ".join(_ALLOWED_VALIDATION_MARKET_SESSIONS)
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be one of: {allowed}."
        )
    return cast(ValidationMarketSession, raw_value)


def _validate_validation_exchange_session_scope(
    raw_value: object,
    *,
    field_name: str,
) -> ValidationExchangeSessionScope:
    if not isinstance(raw_value, str):
        raise BootstrapConfigError(f"Config field '{field_name}' must be a string.")
    if raw_value not in _ALLOWED_VALIDATION_EXCHANGE_SESSION_SCOPES:
        allowed = ", ".join(_ALLOWED_VALIDATION_EXCHANGE_SESSION_SCOPES)
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be one of: {allowed}."
        )
    return cast(ValidationExchangeSessionScope, raw_value)


def _validate_validation_execution_direction_mode(
    raw_value: object,
    *,
    field_name: str,
) -> ValidationExecutionDirectionMode:
    if not isinstance(raw_value, str):
        raise BootstrapConfigError(f"Config field '{field_name}' must be a string.")
    normalized = raw_value.strip()
    if normalized not in _ALLOWED_VALIDATION_EXECUTION_DIRECTION_MODES:
        allowed = ", ".join(_ALLOWED_VALIDATION_EXECUTION_DIRECTION_MODES)
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be one of: {allowed}."
        )
    return cast(ValidationExecutionDirectionMode, normalized)


def _validate_market_data_adjustment_policy(
    raw_value: object,
    *,
    field_name: str,
) -> MarketDataAdjustmentPolicy:
    if not isinstance(raw_value, str):
        raise BootstrapConfigError(f"Config field '{field_name}' must be a string.")
    normalized = raw_value.strip()
    if normalized not in _ALLOWED_MARKET_DATA_ADJUSTMENT_POLICIES:
        allowed = ", ".join(_ALLOWED_MARKET_DATA_ADJUSTMENT_POLICIES)
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be one of: {allowed}."
        )
    return cast(MarketDataAdjustmentPolicy, normalized)


def _validate_relative_or_absolute_path(
    raw_value: object,
    *,
    config_path: Path,
    field_name: str,
) -> Path:
    if not isinstance(raw_value, str):
        raise BootstrapConfigError(f"Config field '{field_name}' must be a string path.")
    if not raw_value.strip():
        raise BootstrapConfigError(f"Config field '{field_name}' must not be empty.")

    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        path = resolve_config_relative_path(path, config_path=config_path)
    return path.resolve(strict=False)


def resolve_config_relative_path(path: Path, *, config_path: Path) -> Path:
    base_dir = _config_relative_base_dir(config_path)
    return base_dir / path


def _config_relative_base_dir(config_path: Path) -> Path:
    config_dir = config_path.resolve(strict=False).parent
    if config_dir.name == "config" and (config_dir.parent / "pyproject.toml").exists():
        return config_dir.parent
    return config_dir


def _validate_base_url(raw_value: object, *, field_name: str) -> str:
    normalized = _validate_non_blank_string(raw_value, field_name=field_name)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be an absolute http(s) URL."
        )
    return normalized.rstrip("/")


def _validate_market_data_base_url(
    raw_value: object,
    *,
    provider: ValidationMarketDataProvider,
    field_name: str,
) -> str:
    if provider != "futu_openapi":
        return _validate_base_url(raw_value, field_name=field_name)
    normalized = _validate_non_blank_string(raw_value, field_name=field_name)
    parsed = urlparse(normalized)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if parsed.scheme != "tcp" or not parsed.hostname or port is None:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be an absolute tcp://host:port URL "
            "for futu_openapi."
        )
    return normalized.rstrip("/")


def _validate_websocket_url(raw_value: object, *, field_name: str) -> str:
    normalized = _validate_non_blank_string(raw_value, field_name=field_name)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be an absolute ws(s) URL."
        )
    return normalized.rstrip("/")


def _validate_non_blank_string(raw_value: object, *, field_name: str) -> str:
    if not isinstance(raw_value, str):
        raise BootstrapConfigError(f"Config field '{field_name}' must be a string.")
    normalized = raw_value.strip()
    if not normalized:
        raise BootstrapConfigError(f"Config field '{field_name}' must not be empty.")
    return normalized


def _validate_optional_non_blank_string(
    raw_value: object,
    *,
    field_name: str,
    required: bool,
) -> str:
    if not isinstance(raw_value, str):
        raise BootstrapConfigError(f"Config field '{field_name}' must be a string.")
    normalized = raw_value.strip()
    if required and not normalized:
        raise BootstrapConfigError(f"Config field '{field_name}' must not be empty.")
    return normalized


def _validate_optional_native_web_search_tool_type(
    raw_value: object,
    *,
    field_name: str,
    required: bool,
) -> str:
    normalized = _validate_optional_non_blank_string(
        raw_value,
        field_name=field_name,
        required=required,
    )
    if not normalized:
        return normalized
    if normalized not in {"web_search", "web_search_preview"}:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be web_search or web_search_preview."
        )
    return normalized


def _validate_optional_positive_int(
    raw_value: object,
    *,
    field_name: str,
    required: bool,
) -> int:
    if raw_value is None:
        if not required:
            return 0
        raise BootstrapConfigError(f"Config field '{field_name}' must not be empty.")
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a positive integer."
        )
    if raw_value <= 0:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be greater than zero."
        )
    return raw_value


def _validate_optional_positive_int_with_default(
    raw_value: object,
    *,
    field_name: str,
    default: int,
) -> int:
    if raw_value is None:
        return default
    value = _validate_positive_int(raw_value, field_name=field_name)
    if value <= 0:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be greater than zero."
        )
    return value


def _validate_optional_non_negative_int_with_default(
    raw_value: object,
    *,
    field_name: str,
    default: int,
) -> int:
    if raw_value is None:
        return default
    return _validate_non_negative_int_config(raw_value, field_name=field_name)


def _validate_optional_bool(
    raw_value: object,
    *,
    field_name: str,
    default: bool,
) -> bool:
    if raw_value is None:
        return default
    if not isinstance(raw_value, bool):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a boolean."
        )
    return raw_value


def _validate_bool(raw_value: object, *, field_name: str) -> bool:
    if not isinstance(raw_value, bool):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a boolean."
        )
    return raw_value


def _validate_non_empty_string_tuple(
    raw_value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if not isinstance(raw_value, list):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a TOML array of strings."
        )
    if not raw_value:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must not be empty."
        )
    normalized: list[str] = []
    for item in raw_value:
        if not isinstance(item, str):
            raise BootstrapConfigError(
                f"Config field '{field_name}' must contain only strings."
            )
        value = item.strip()
        if not value:
            raise BootstrapConfigError(
                f"Config field '{field_name}' must not contain empty strings."
            )
        normalized.append(value)
    return tuple(normalized)


def _validate_positive_int_tuple(
    raw_value: object,
    *,
    field_name: str,
) -> tuple[int, ...]:
    if not isinstance(raw_value, list):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a TOML array of positive integers."
        )
    if not raw_value:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must not be empty."
        )
    normalized: list[int] = []
    for item in raw_value:
        value = _validate_positive_int(item, field_name=field_name)
        if value in normalized:
            raise BootstrapConfigError(
                f"Config field '{field_name}' must not contain duplicate integers."
            )
        normalized.append(value)
    return tuple(normalized)


def _validate_tool_name_tuple(raw_value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(raw_value, list):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a TOML array of strings."
        )
    normalized: list[str] = []
    for item in raw_value:
        if not isinstance(item, str):
            raise BootstrapConfigError(
                f"Config field '{field_name}' must contain only strings."
            )
        tool_name = item.strip()
        if not tool_name:
            raise BootstrapConfigError(
                f"Config field '{field_name}' must not contain empty strings."
            )
        if tool_name not in _WEB_SEARCH_ALLOWED_ACQUISITION_TOOL_NAMES:
            allowed = ", ".join(_WEB_SEARCH_ALLOWED_ACQUISITION_TOOL_NAMES)
            raise BootstrapConfigError(
                f"Config field '{field_name}' contains unsupported tool name "
                f"{tool_name!r}; supported values: {allowed}."
            )
        if tool_name in normalized:
            raise BootstrapConfigError(
                f"Config field '{field_name}' must not contain duplicate strings."
            )
        normalized.append(tool_name)
    return tuple(normalized)


def _validate_positive_int(raw_value: object, *, field_name: str) -> int:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a positive integer."
        )
    if raw_value <= 0:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be greater than zero."
        )
    return raw_value


def _validate_non_negative_int_config(raw_value: object, *, field_name: str) -> int:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be a non-negative integer."
        )
    if raw_value < 0:
        raise BootstrapConfigError(
            f"Config field '{field_name}' must be greater than or equal to zero."
        )
    return raw_value


def _validate_env_reference(raw_value: object, *, field_name: str) -> str:
    env_name = _validate_non_blank_string(raw_value, field_name=field_name)
    env_value = os.environ.get(env_name)
    if env_value is None:
        raise BootstrapConfigError(
            f"Environment variable '{env_name}' referenced by '{field_name}' is not set."
        )
    normalized = env_value.strip()
    if not normalized:
        raise BootstrapConfigError(
            f"Environment variable '{env_name}' referenced by '{field_name}' must not be empty."
        )
    return normalized


def _validate_optional_env_reference(
    raw_value: object,
    *,
    field_name: str,
    required: bool,
) -> str:
    if not isinstance(raw_value, str):
        raise BootstrapConfigError(f"Config field '{field_name}' must be a string.")
    env_name = raw_value.strip()
    if not env_name:
        if not required:
            return ""
        raise BootstrapConfigError(f"Config field '{field_name}' must not be empty.")
    env_value = os.environ.get(env_name)
    if env_value is None:
        if not required:
            return ""
        raise BootstrapConfigError(
            f"Environment variable '{env_name}' referenced by '{field_name}' is not set."
        )
    normalized = env_value.strip()
    if not normalized:
        if not required:
            return ""
        raise BootstrapConfigError(
            f"Environment variable '{env_name}' referenced by '{field_name}' must not be empty."
        )
    return normalized


def _load_dotenv_from_cwd() -> None:
    dotenv_path = (Path.cwd() / ".env").resolve(strict=False)
    if not dotenv_path.exists():
        return
    if not dotenv_path.is_file():
        raise BootstrapConfigError(f".env must be a file: {dotenv_path}")

    for line_number, raw_line in enumerate(
        dotenv_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise BootstrapConfigError(
                f"Invalid .env line {line_number} in {dotenv_path}: missing '='."
            )
        key, raw_value = line.split("=", 1)
        env_name = key.strip()
        if not env_name:
            raise BootstrapConfigError(
                f"Invalid .env line {line_number} in {dotenv_path}: empty variable name."
            )
        if env_name in os.environ:
            continue
        os.environ[env_name] = _normalize_dotenv_value(
            raw_value.strip(),
            dotenv_path=dotenv_path,
            line_number=line_number,
        )


def _normalize_dotenv_value(
    raw_value: str,
    *,
    dotenv_path: Path,
    line_number: int,
) -> str:
    if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {
        '"',
        "'",
    }:
        return raw_value[1:-1]
    if raw_value.startswith(("'", '"')) or raw_value.endswith(("'", '"')):
        raise BootstrapConfigError(
            f"Invalid .env line {line_number} in {dotenv_path}: unmatched quote."
        )
    return raw_value
