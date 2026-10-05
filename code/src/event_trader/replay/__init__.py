"""Replay-facing package exports."""

from __future__ import annotations

from typing import Any

__all__ = [
    "ReplayRunner",
    "ReplayRunnerError",
    "ReplayStepReceipt",
    "inject_historical_macro_api",
    "inject_historical_manual_dataset",
    "inject_historical_news_stream",
]


def __getattr__(name: str) -> Any:
    if name in {"ReplayRunner", "ReplayRunnerError", "ReplayStepReceipt"}:
        from .runner import ReplayRunner, ReplayRunnerError, ReplayStepReceipt

        exports: dict[str, Any] = {
            "ReplayRunner": ReplayRunner,
            "ReplayRunnerError": ReplayRunnerError,
            "ReplayStepReceipt": ReplayStepReceipt,
        }
        return exports[name]
    if name in {
        "inject_historical_macro_api",
        "inject_historical_manual_dataset",
        "inject_historical_news_stream",
    }:
        from .historical_steps import (
            inject_historical_macro_api,
            inject_historical_manual_dataset,
            inject_historical_news_stream,
        )

        historical_exports: dict[str, Any] = {
            "inject_historical_macro_api": inject_historical_macro_api,
            "inject_historical_manual_dataset": inject_historical_manual_dataset,
            "inject_historical_news_stream": inject_historical_news_stream,
        }
        return historical_exports[name]
    raise AttributeError(name)
