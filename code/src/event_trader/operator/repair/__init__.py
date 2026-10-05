"""Operator repair planning and application surfaces."""

from __future__ import annotations

from importlib import import_module

_EXPORTS: dict[str, tuple[str, str]] = {
    "OperatorRepairError": (".contracts", "OperatorRepairError"),
    "RepairAction": (".contracts", "RepairAction"),
    "RepairBlocker": (".contracts", "RepairBlocker"),
    "RepairPlan": (".contracts", "RepairPlan"),
    "RepairReceipt": (".contracts", "RepairReceipt"),
    "apply_replay_repair_plan": (".replay", "apply_replay_repair_plan"),
    "default_replay_run_id": (".replay", "default_replay_run_id"),
    "plan_replay_failed_event_repair": (
        ".replay",
        "plan_replay_failed_event_repair",
    ),
    "replay_run_key": (".replay", "replay_run_key"),
    "LiveCatchupRuntimeRepairError": (
        ".live_catchup_runtime",
        "LiveCatchupRuntimeRepairError",
    ),
    "LiveCatchupRuntimeRepairReport": (
        ".live_catchup_runtime",
        "LiveCatchupRuntimeRepairReport",
    ),
    "repair_live_catchup_runtime": (
        ".live_catchup_runtime",
        "repair_live_catchup_runtime",
    ),
    "PMExecutionRecoveryDecision": (
        ".runtime_pm",
        "PMExecutionRecoveryDecision",
    ),
    "PMExecutionRecoveryError": (
        ".runtime_pm",
        "PMExecutionRecoveryError",
    ),
    "PMExecutionRecoveryReport": (
        ".runtime_pm",
        "PMExecutionRecoveryReport",
    ),
    "plan_pm_execution_recovery": (
        ".runtime_pm",
        "plan_pm_execution_recovery",
    ),
    "ReflectionRecoveryDecision": (
        ".runtime_reflection",
        "ReflectionRecoveryDecision",
    ),
    "ReflectionRecoveryError": (
        ".runtime_reflection",
        "ReflectionRecoveryError",
    ),
    "ReflectionRecoveryReport": (
        ".runtime_reflection",
        "ReflectionRecoveryReport",
    ),
    "plan_reflection_recovery": (
        ".runtime_reflection",
        "plan_reflection_recovery",
    ),
    "HistoricalWebSearchRepairError": (
        ".historical_web_search",
        "HistoricalWebSearchRepairError",
    ),
    "HistoricalWebSearchRepairReceipt": (
        ".historical_web_search",
        "HistoricalWebSearchRepairReceipt",
    ),
    "repair_historical_web_search_workspace": (
        ".historical_web_search",
        "repair_historical_web_search_workspace",
    ),
    "SourceArchiveCleanupError": (
        ".source_archive",
        "SourceArchiveCleanupError",
    ),
    "SourceArchiveCleanupReceipt": (
        ".source_archive",
        "SourceArchiveCleanupReceipt",
    ),
    "cleanup_live_catchup_source_archive": (
        ".source_archive",
        "cleanup_live_catchup_source_archive",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals()) + __all__)
