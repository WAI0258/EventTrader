"""Target-to-market mapping resolver for deterministic validation."""

from __future__ import annotations

from event_trader.config import KernelConfig
from event_trader.contracts._validators import validate_target_key
from event_trader.contracts.view_state_change import MarketMapping, ViewStateChangeContractError


class ValidationMarketMappingError(ValueError):
    """Raised when validation market mapping resolution is invalid."""


def resolve_market_mapping(config: KernelConfig, target_key: str) -> MarketMapping:
    """Resolve one validated target-to-market mapping from kernel config."""
    if not isinstance(config, KernelConfig):
        raise ValidationMarketMappingError("config must be a KernelConfig instance.")

    if config.validation is None:
        raise ValidationMarketMappingError("config.validation is required.")

    validated_target_key = validate_target_key(
        target_key,
        error_type=ValidationMarketMappingError,
    )
    mapping_config = config.validation.market_mappings.get(validated_target_key)
    if mapping_config is None:
        raise ValidationMarketMappingError(
            "Missing validation.market_mappings entry for target_key "
            f"{validated_target_key!r}."
        )

    try:
        return MarketMapping(
            target_key=validated_target_key,
            market_symbol=mapping_config.market_symbol,
            market_session=mapping_config.market_session,
            exchange=mapping_config.exchange,
            bar_granularity=mapping_config.bar_granularity,
            exchange_session_scope=mapping_config.exchange_session_scope,
        )
    except ViewStateChangeContractError as exc:
        raise ValidationMarketMappingError(
            "Resolved market mapping is invalid for target_key "
            f"{validated_target_key!r}: {exc}"
        ) from exc


__all__ = [
    "ValidationMarketMappingError",
    "resolve_market_mapping",
]



