"""Feed-boundary ingress models for explicit live source shapes."""

from __future__ import annotations

from importlib import import_module

_EXPORTS: dict[str, tuple[str, str]] = {
    "BenzingaNewsWebSocketError": (
        ".benzinga_news_ws",
        "BenzingaNewsWebSocketError",
    ),
    "BenzingaNewsWebSocketEvent": (
        ".benzinga_news_ws",
        "BenzingaNewsWebSocketEvent",
    ),
    "parse_benzinga_news_ws_message": (
        ".benzinga_news_ws",
        "parse_benzinga_news_ws_message",
    ),
    "MarketNewsArticle": (".market_news", "MarketNewsArticle"),
    "MarketNewsGatherError": (".market_news", "MarketNewsGatherError"),
    "MarketNewsPage": (".market_news", "MarketNewsPage"),
    "market_news_article_to_live_input": (
        ".market_news",
        "market_news_article_to_live_input",
    ),
    "request_market_news_page": (".market_news", "request_market_news_page"),
    "shape_market_news_page": (".market_news", "shape_market_news_page"),
    "MarketNewsBackfillError": (
        ".market_news_backfill",
        "MarketNewsBackfillError",
    ),
    "MarketNewsBackfillReceipt": (
        ".market_news_backfill",
        "MarketNewsBackfillReceipt",
    ),
    "MarketNewsBackfillSlice": (
        ".market_news_backfill",
        "MarketNewsBackfillSlice",
    ),
    "backfill_market_news": (".market_news_backfill", "backfill_market_news"),
    "FeedModelError": (".models", "FeedModelError"),
    "LiveIngressInput": (".models", "LiveIngressInput"),
    "LiveMacroApiInput": (".models", "LiveMacroApiInput"),
    "LiveManualSourceInput": (".models", "LiveManualSourceInput"),
    "LiveNewsStreamInput": (".models", "LiveNewsStreamInput"),
    "LiveSourceShape": (".models", "LiveSourceShape"),
    "LiveWebSearchInput": (".models", "LiveWebSearchInput"),
    "LiveWebSearchGatherError": (".web_search", "LiveWebSearchGatherError"),
    "SearchAgentRunner": (".web_search", "SearchAgentRunner"),
    "gather_live_web_search_materials": (
        ".web_search",
        "gather_live_web_search_materials",
    ),
    "HistoricalWebSearchBackfillError": (
        ".web_search_backfill",
        "HistoricalWebSearchBackfillError",
    ),
    "HistoricalWebSearchBackfillReceipt": (
        ".web_search_backfill",
        "HistoricalWebSearchBackfillReceipt",
    ),
    "HistoricalWebSearchBackfillSlice": (
        ".web_search_backfill",
        "HistoricalWebSearchBackfillSlice",
    ),
    "backfill_historical_web_search": (
        ".web_search_backfill",
        "backfill_historical_web_search",
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
