"""Runtime-side operator command entry surfaces."""

from .operator_commands import (
    OperatorCommandError,
    OperatorInjectionMode,
    OperatorInjectionReceipt,
    OperatorWikiWriteMode,
    inject_authoritative_wiki,
    inject_live_macro_api,
    inject_live_news_stream,
    inject_live_web_search,
    inject_manual_evidence,
)

__all__ = [
    "OperatorCommandError",
    "OperatorInjectionMode",
    "OperatorInjectionReceipt",
    "OperatorWikiWriteMode",
    "inject_authoritative_wiki",
    "inject_live_macro_api",
    "inject_live_news_stream",
    "inject_live_web_search",
    "inject_manual_evidence",
]
