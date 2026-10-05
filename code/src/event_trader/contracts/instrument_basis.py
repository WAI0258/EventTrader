"""Canonical machine labels for analysis-owned instrument bases."""

from __future__ import annotations

import re

_PROXY_ALIASES = frozenset(
    {
        "proxy",
        "proxy_normalized",
        "proxy_price",
    }
)
_MARKET_BAR_ALIAS_PATTERN = re.compile(
    r"^(?P<symbol>[A-Z0-9._:-]+)(?:\s+\S+)*\s+bar(?:\s+close)?$"
)


def canonicalize_instrument_basis(instrument_basis: str) -> str:
    normalized = instrument_basis.strip()
    if normalized in _PROXY_ALIASES or normalized.startswith("proxy:"):
        return "proxy"
    match = _MARKET_BAR_ALIAS_PATTERN.fullmatch(normalized)
    if match is not None:
        return match.group("symbol")
    return normalized


def is_proxy_instrument_basis(instrument_basis: str) -> bool:
    return canonicalize_instrument_basis(instrument_basis) == "proxy"


__all__ = [
    "canonicalize_instrument_basis",
    "is_proxy_instrument_basis",
]
