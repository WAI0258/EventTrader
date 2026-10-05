"""Project-owned historical source archive surfaces."""

from __future__ import annotations

from importlib import import_module

_EXPORTS: dict[str, tuple[str, str]] = {
    "MarketNewsArchiveError": (".market_news", "MarketNewsArchiveError"),
    "MarketNewsArchiveRecord": (".market_news", "MarketNewsArchiveRecord"),
    "MarketNewsArchiveWriteReceipt": (
        ".market_news",
        "MarketNewsArchiveWriteReceipt",
    ),
    "append_market_news_record": (".market_news", "append_market_news_record"),
    "build_market_news_target_layout": (
        ".market_news",
        "build_market_news_target_layout",
    ),
    "map_live_market_news_to_archive_record": (
        ".market_news",
        "map_live_market_news_to_archive_record",
    ),
    "market_news_partition_path": (".market_news", "market_news_partition_path"),
    "market_news_record_id": (".market_news", "market_news_record_id"),
    "read_market_news_records": (".market_news", "read_market_news_records"),
    "write_live_market_news_record": (".market_news", "write_live_market_news_record"),
    "HistoricalWebSearchLibraryError": (
        ".web_search",
        "HistoricalWebSearchLibraryError",
    ),
    "HistoricalWebSearchRecord": (".web_search", "HistoricalWebSearchRecord"),
    "HistoricalWebSearchTargetLayout": (
        ".web_search",
        "HistoricalWebSearchTargetLayout",
    ),
    "HistoricalWebSearchWriteReceipt": (
        ".web_search",
        "HistoricalWebSearchWriteReceipt",
    ),
    "append_historical_web_search_record": (
        ".web_search",
        "append_historical_web_search_record",
    ),
    "build_historical_web_search_target_layout": (
        ".web_search",
        "build_historical_web_search_target_layout",
    ),
    "historical_web_search_candidate_fingerprint": (
        ".web_search",
        "historical_web_search_candidate_fingerprint",
    ),
    "historical_web_search_observation_id": (
        ".web_search",
        "historical_web_search_observation_id",
    ),
    "historical_web_search_partition_path": (
        ".web_search",
        "historical_web_search_partition_path",
    ),
    "historical_web_search_record_id": (
        ".web_search",
        "historical_web_search_record_id",
    ),
    "map_live_web_search_to_historical_record": (
        ".web_search",
        "map_live_web_search_to_historical_record",
    ),
    "normalize_historical_web_search_payload": (
        ".web_search",
        "normalize_historical_web_search_payload",
    ),
    "read_historical_web_search_records": (
        ".web_search",
        "read_historical_web_search_records",
    ),
    "write_historical_web_search_backfill_record": (
        ".web_search",
        "write_historical_web_search_backfill_record",
    ),
    "write_live_web_search_record": (".web_search", "write_live_web_search_record"),
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
