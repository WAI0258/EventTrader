"""Replay input time semantics."""

from __future__ import annotations

from datetime import datetime

from event_trader.feeds.models import (
    FeedModelError,
    HistoricalMacroApiInput,
    HistoricalManualDatasetInput,
    HistoricalMarketNewsInput,
    HistoricalNewsStreamInput,
    HistoricalWebSearchInput,
    ReplayIngressInput,
    ReplaySourceShape,
)


def replay_ts_source(item: ReplayIngressInput) -> datetime:
    """Map a replay ingress input to its canonical source timestamp."""
    if isinstance(item, HistoricalNewsStreamInput):
        return item.published_at
    if isinstance(item, HistoricalMarketNewsInput):
        if item.updated_at is not None:
            return max(item.published_at, item.updated_at)
        return item.published_at
    if isinstance(item, HistoricalWebSearchInput):
        return item.discovered_at if item.published_at is None else item.published_at
    if isinstance(item, HistoricalMacroApiInput):
        return item.released_at
    return item.provided_at


def replay_ts_event(item: ReplayIngressInput) -> datetime:
    """Map a replay ingress input to its historical visibility timestamp."""
    return item.visible_at


__all__ = [
    "FeedModelError",
    "HistoricalMacroApiInput",
    "HistoricalMarketNewsInput",
    "HistoricalManualDatasetInput",
    "HistoricalNewsStreamInput",
    "HistoricalWebSearchInput",
    "ReplayIngressInput",
    "ReplaySourceShape",
    "replay_ts_event",
    "replay_ts_source",
]
