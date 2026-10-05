"""Thin optional integrations for project-owned bootstrap composition."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from .capabilities import (
    CapabilityDescriptor,
    CapabilityDiscoveryError,
    CapabilityLookupError,
    CapabilityStatus,
    capability_catalog,
    capability_descriptor,
    discover_capability_status,
)

if TYPE_CHECKING:
    from .mirothinker_analysis import (
        MiroThinkerAnalysisRuntimeConfig,
        MiroThinkerAnalysisRuntimeError,
        build_mirothinker_analysis_runner,
    )
    from .mirothinker_reflection import (
        MiroThinkerReflectionRuntimeConfig,
        MiroThinkerReflectionRuntimeError,
        build_mirothinker_reflection_runner,
        build_mirothinker_target_close_reflection_runner,
        build_mirothinker_target_reflection_runner,
        build_mirothinker_open_position_reflection_runner,
    )
    from .mirothinker_search import (
        MiroThinkerSearchRuntimeConfig,
        MiroThinkerSearchRuntimeError,
        build_mirothinker_search_collection_runner,
        build_mirothinker_search_runner,
    )

__all__ = [
    "CapabilityDescriptor",
    "CapabilityDiscoveryError",
    "CapabilityLookupError",
    "CapabilityStatus",
    "MiroThinkerAnalysisRuntimeConfig",
    "MiroThinkerAnalysisRuntimeError",
    "MiroThinkerReflectionRuntimeConfig",
    "MiroThinkerReflectionRuntimeError",
    "MiroThinkerSearchRuntimeConfig",
    "MiroThinkerSearchRuntimeError",
    "build_mirothinker_analysis_runner",
    "build_mirothinker_reflection_runner",
    "build_mirothinker_target_close_reflection_runner",
    "build_mirothinker_target_reflection_runner",
    "build_mirothinker_open_position_reflection_runner",
    "build_mirothinker_search_collection_runner",
    "build_mirothinker_search_runner",
    "capability_catalog",
    "capability_descriptor",
    "discover_capability_status",
]

_LAZY_EXPORTS = {
    "MiroThinkerAnalysisRuntimeConfig": (
        "event_trader.integrations.mirothinker_analysis",
        "MiroThinkerAnalysisRuntimeConfig",
    ),
    "MiroThinkerAnalysisRuntimeError": (
        "event_trader.integrations.mirothinker_analysis",
        "MiroThinkerAnalysisRuntimeError",
    ),
    "build_mirothinker_analysis_runner": (
        "event_trader.integrations.mirothinker_analysis",
        "build_mirothinker_analysis_runner",
    ),
    "MiroThinkerReflectionRuntimeConfig": (
        "event_trader.integrations.mirothinker_reflection",
        "MiroThinkerReflectionRuntimeConfig",
    ),
    "MiroThinkerReflectionRuntimeError": (
        "event_trader.integrations.mirothinker_reflection",
        "MiroThinkerReflectionRuntimeError",
    ),
    "build_mirothinker_reflection_runner": (
        "event_trader.integrations.mirothinker_reflection",
        "build_mirothinker_reflection_runner",
    ),
    "build_mirothinker_target_reflection_runner": (
        "event_trader.integrations.mirothinker_reflection",
        "build_mirothinker_target_reflection_runner",
    ),
    "build_mirothinker_target_close_reflection_runner": (
        "event_trader.integrations.mirothinker_reflection",
        "build_mirothinker_target_close_reflection_runner",
    ),
    "build_mirothinker_open_position_reflection_runner": (
        "event_trader.integrations.mirothinker_reflection",
        "build_mirothinker_open_position_reflection_runner",
    ),
    "MiroThinkerSearchRuntimeConfig": (
        "event_trader.integrations.mirothinker_search",
        "MiroThinkerSearchRuntimeConfig",
    ),
    "MiroThinkerSearchRuntimeError": (
        "event_trader.integrations.mirothinker_search",
        "MiroThinkerSearchRuntimeError",
    ),
    "build_mirothinker_search_collection_runner": (
        "event_trader.integrations.mirothinker_search",
        "build_mirothinker_search_collection_runner",
    ),
    "build_mirothinker_search_runner": (
        "event_trader.integrations.mirothinker_search",
        "build_mirothinker_search_runner",
    ),
}


def __getattr__(name: str) -> object:
    export = _LAZY_EXPORTS.get(name)
    if export is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = export
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value

