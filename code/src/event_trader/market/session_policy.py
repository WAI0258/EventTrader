from __future__ import annotations

from typing import Literal

MarketSession = Literal["continuous", "exchange_session"]
ExchangeSessionScope = Literal["regular", "extended"]

ALLOWED_MARKET_SESSIONS: tuple[MarketSession, ...] = (
    "continuous",
    "exchange_session",
)
ALLOWED_EXCHANGE_SESSION_SCOPES: tuple[ExchangeSessionScope, ...] = (
    "regular",
    "extended",
)


def validate_market_session_contract_fields(
    *,
    market_session: str,
    exchange: str | None,
    exchange_session_scope: str | None,
    error_type: type[Exception],
    market_session_field_name: str = "market_session",
    exchange_field_name: str = "exchange",
    exchange_session_scope_field_name: str = "exchange_session_scope",
    invalid_market_session_message: str | None = None,
    invalid_exchange_session_scope_message: str | None = None,
    continuous_exchange_message: str | None = None,
    continuous_exchange_session_scope_message: str | None = None,
    exchange_session_exchange_message: str | None = None,
    exchange_session_scope_message: str | None = None,
) -> None:
    """Validate that market session + exchange fields obey internal session policy."""
    if invalid_market_session_message is None:
        invalid_market_session_message = (
            f"{market_session_field_name} must be 'continuous' or 'exchange_session'."
        )
    if invalid_exchange_session_scope_message is None:
        invalid_exchange_session_scope_message = (
            f"{exchange_session_scope_field_name} must be 'regular' or 'extended'."
        )
    if continuous_exchange_message is None:
        continuous_exchange_message = (
            f"{exchange_field_name} must be absent when "
            f"{market_session_field_name} = 'continuous'."
        )
    if continuous_exchange_session_scope_message is None:
        continuous_exchange_session_scope_message = (
            f"{exchange_session_scope_field_name} must be absent when "
            f"{market_session_field_name} = 'continuous'."
        )
    if exchange_session_exchange_message is None:
        exchange_session_exchange_message = (
            f"{exchange_field_name} is required when "
            f"{market_session_field_name} = 'exchange_session'."
        )
    if exchange_session_scope_message is None:
        exchange_session_scope_message = (
            f"{exchange_session_scope_field_name} is required when "
            f"{market_session_field_name} = 'exchange_session'."
        )

    if market_session not in ALLOWED_MARKET_SESSIONS:
        raise error_type(invalid_market_session_message)
    if (
        exchange_session_scope is not None
        and exchange_session_scope not in ALLOWED_EXCHANGE_SESSION_SCOPES
    ):
        raise error_type(invalid_exchange_session_scope_message)

    if market_session == "continuous":
        if exchange is not None:
            raise error_type(continuous_exchange_message)
        if exchange_session_scope is not None:
            raise error_type(continuous_exchange_session_scope_message)
        return

    if market_session == "exchange_session":
        if exchange is None:
            raise error_type(exchange_session_exchange_message)
        if exchange_session_scope is None:
            raise error_type(exchange_session_scope_message)


def build_market_session_identity(
    *,
    market_session: str,
    exchange: str | None,
    exchange_session_scope: str | None,
    bar_granularity: str,
    market_symbol: str,
) -> dict[str, object]:
    """Build canonical session identity for market-context metadata."""
    return {
        "market_session": market_session,
        "exchange": exchange,
        "exchange_session_scope": exchange_session_scope,
        "bar_granularity": bar_granularity,
        "market_symbol": market_symbol,
    }


def compact_market_session_identity(
    source_metadata: dict[str, object],
) -> dict[str, object]:
    """Extract canonical session identity from source metadata for compact snapshots."""
    keys = (
        "market_session",
        "exchange",
        "exchange_session_scope",
        "bar_granularity",
        "market_symbol",
    )
    identity = {key: source_metadata[key] for key in keys if key in source_metadata}
    if "market_symbol" not in identity:
        requested_symbol = source_metadata.get("requested_symbol")
        if requested_symbol is not None:
            identity["market_symbol"] = requested_symbol
    return identity


__all__ = [
    "ALLOWED_EXCHANGE_SESSION_SCOPES",
    "ALLOWED_MARKET_SESSIONS",
    "ExchangeSessionScope",
    "MarketSession",
    "build_market_session_identity",
    "compact_market_session_identity",
    "validate_market_session_contract_fields",
]
