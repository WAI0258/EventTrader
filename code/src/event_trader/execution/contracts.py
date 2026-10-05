"""Contracts for deterministic paper execution and market provenance."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite
from typing import Literal, cast

from event_trader.contracts._validators import (
    validate_target_key,
    validate_timestamp,
)
from event_trader.contracts.view_state_change import (
    ExchangeSessionScope,
    MarketDataBar,
    MarketSession,
)

ExecutionStatus = Literal["executed", "rejected"]
ExecutionMode = Literal["paper"]
ExecutionPriceBasis = Literal["open", "close"]
MissingBarPolicy = Literal["reject"]
MarketDataAvailabilityStatus = Literal["available", "unavailable"]
ExecutionSourceKind = Literal["legacy_state_change", "pm_decision"]


class ExecutionContractError(ValueError):
    """Raised when paper execution contracts are malformed."""


@dataclass(frozen=True, slots=True)
class ExecutionCostModel:
    buy_cost_bps: float
    sell_cost_bps: float
    price_basis: ExecutionPriceBasis

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "buy_cost_bps",
            _validate_non_negative_float(self.buy_cost_bps, "buy_cost_bps"),
        )
        object.__setattr__(
            self,
            "sell_cost_bps",
            _validate_non_negative_float(self.sell_cost_bps, "sell_cost_bps"),
        )
        if self.price_basis not in {"open", "close"}:
            raise ExecutionContractError("price_basis must be open or close.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "buy_cost_bps": self.buy_cost_bps,
            "sell_cost_bps": self.sell_cost_bps,
            "price_basis": self.price_basis,
        }


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    intent_id: str
    pm_decision_id: str
    decision_episode_id: str
    source_state_change_id: str | None
    target_key: str
    business_at: datetime
    decision_available_at: datetime
    target_weight: float
    actual_target_weight_before_decision: float
    requested_target_weight: float
    execution_mode: ExecutionMode = "paper"

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _validate_id(self.intent_id, "intent_id"))
        object.__setattr__(
            self,
            "pm_decision_id",
            _validate_id(self.pm_decision_id, "pm_decision_id"),
        )
        object.__setattr__(
            self,
            "decision_episode_id",
            _validate_id(self.decision_episode_id, "decision_episode_id"),
        )
        object.__setattr__(
            self,
            "source_state_change_id",
            _validate_optional_id(self.source_state_change_id, "source_state_change_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ExecutionContractError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=ExecutionContractError,
            ),
        )
        object.__setattr__(
            self,
            "decision_available_at",
            validate_timestamp(
                self.decision_available_at,
                field_name="decision_available_at",
                error_type=ExecutionContractError,
            ),
        )
        if self.decision_available_at < self.business_at:
            raise ExecutionContractError(
                "decision_available_at must be at or after business_at."
            )
        object.__setattr__(self, "target_weight", _validate_finite_float(self.target_weight, "target_weight"))
        object.__setattr__(
            self,
            "actual_target_weight_before_decision",
            _validate_finite_float(
                self.actual_target_weight_before_decision,
                "actual_target_weight_before_decision",
            ),
        )
        object.__setattr__(
            self,
            "requested_target_weight",
            _validate_finite_float(self.requested_target_weight, "requested_target_weight"),
        )
        if self.target_weight != self.requested_target_weight:
            raise ExecutionContractError(
                "target_weight must match requested_target_weight."
            )
        if self.execution_mode != "paper":
            raise ExecutionContractError("execution_mode must be paper.")

    @property
    def delta_weight(self) -> float:
        return self.requested_target_weight - self.actual_target_weight_before_decision

    def to_json_payload(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "pm_decision_id": self.pm_decision_id,
            "decision_episode_id": self.decision_episode_id,
            "source_state_change_id": self.source_state_change_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "decision_available_at": self.decision_available_at.isoformat(),
            "target_weight": self.target_weight,
            "actual_target_weight_before_decision": self.actual_target_weight_before_decision,
            "requested_target_weight": self.requested_target_weight,
            "execution_mode": self.execution_mode,
        }


@dataclass(frozen=True, slots=True)
class MarketDataProvenanceReceipt:
    provenance_id: str
    provider: str
    target_key: str
    market_symbol: str
    instrument_symbol: str
    proxy_symbol: str
    market_session: MarketSession
    exchange_session_scope: ExchangeSessionScope | None
    exchange: str | None
    bar_granularity: str
    price_field_used: ExecutionPriceBasis
    market_data_hash: str | None
    availability_status: MarketDataAvailabilityStatus
    requested_start_at: datetime
    requested_end_at: datetime
    selected_bar_start_at: datetime | None
    selected_bar_end_at: datetime | None
    selected_bar_hash: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance_id", _validate_id(self.provenance_id, "provenance_id"))
        object.__setattr__(self, "provider", _validate_id(self.provider, "provider"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ExecutionContractError),
        )
        for field_name in ("market_symbol", "instrument_symbol", "proxy_symbol", "bar_granularity"):
            object.__setattr__(self, field_name, _validate_id(getattr(self, field_name), field_name))
        normalized_exchange = None
        if self.exchange is not None:
            normalized_exchange = _validate_id(self.exchange, "exchange")
        from event_trader.market.session_policy import validate_market_session_contract_fields

        validate_market_session_contract_fields(
            market_session=self.market_session,
            exchange=normalized_exchange,
            exchange_session_scope=self.exchange_session_scope,
            error_type=ExecutionContractError,
            continuous_exchange_message="continuous market_session must not provide an exchange.",
            continuous_exchange_session_scope_message=(
                "continuous market_session must not provide an exchange_session_scope."
            ),
            exchange_session_exchange_message="exchange_session market_session requires an exchange.",
            exchange_session_scope_message=(
                "exchange_session market_session requires an exchange_session_scope."
            ),
            invalid_market_session_message="market_session must be continuous or exchange_session.",
        )
        object.__setattr__(self, "exchange", normalized_exchange)
        if self.price_field_used not in {"open", "close"}:
            raise ExecutionContractError("price_field_used must be open or close.")
        if self.market_data_hash is not None:
            object.__setattr__(
                self,
                "market_data_hash",
                _validate_id(self.market_data_hash, "market_data_hash"),
            )
        if self.availability_status not in {"available", "unavailable"}:
            raise ExecutionContractError(
                "availability_status must be available or unavailable."
            )
        object.__setattr__(
            self,
            "requested_start_at",
            validate_timestamp(
                self.requested_start_at,
                field_name="requested_start_at",
                error_type=ExecutionContractError,
            ),
        )
        object.__setattr__(
            self,
            "requested_end_at",
            validate_timestamp(
                self.requested_end_at,
                field_name="requested_end_at",
                error_type=ExecutionContractError,
            ),
        )
        if self.requested_end_at <= self.requested_start_at:
            raise ExecutionContractError("requested_end_at must be after requested_start_at.")
        if self.selected_bar_start_at is not None:
            object.__setattr__(
                self,
                "selected_bar_start_at",
                validate_timestamp(
                    self.selected_bar_start_at,
                    field_name="selected_bar_start_at",
                    error_type=ExecutionContractError,
                ),
            )
        if self.selected_bar_end_at is not None:
            object.__setattr__(
                self,
                "selected_bar_end_at",
                validate_timestamp(
                    self.selected_bar_end_at,
                    field_name="selected_bar_end_at",
                    error_type=ExecutionContractError,
                ),
            )
        if (self.selected_bar_start_at is None) != (self.selected_bar_end_at is None):
            raise ExecutionContractError(
                "selected bar start/end must both be set or both be None."
            )
        if self.selected_bar_hash is not None:
            object.__setattr__(
                self,
                "selected_bar_hash",
                _validate_id(self.selected_bar_hash, "selected_bar_hash"),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "provenance_id": self.provenance_id,
            "provider": self.provider,
            "target_key": self.target_key,
            "market_symbol": self.market_symbol,
            "instrument_symbol": self.instrument_symbol,
            "proxy_symbol": self.proxy_symbol,
            "market_session": self.market_session,
            "exchange_session_scope": self.exchange_session_scope,
            "exchange": self.exchange,
            "bar_granularity": self.bar_granularity,
            "price_field_used": self.price_field_used,
            "market_data_hash": self.market_data_hash,
            "availability_status": self.availability_status,
            "requested_start_at": self.requested_start_at.isoformat(),
            "requested_end_at": self.requested_end_at.isoformat(),
            "selected_bar_start_at": (
                None if self.selected_bar_start_at is None else self.selected_bar_start_at.isoformat()
            ),
            "selected_bar_end_at": (
                None if self.selected_bar_end_at is None else self.selected_bar_end_at.isoformat()
            ),
            "selected_bar_hash": self.selected_bar_hash,
        }


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    execution_record_id: str
    intent_id: str
    pm_decision_id: str
    decision_episode_id: str
    source_state_change_id: str | None
    target_key: str
    status: ExecutionStatus
    execution_mode: ExecutionMode
    business_at: datetime
    decision_available_at: datetime
    recorded_at: datetime
    executed_at: datetime | None
    requested_target_weight: float
    target_weight: float | None
    gross_target_weight: float | None
    price_basis: ExecutionPriceBasis
    raw_price: float | None
    adjusted_price: float | None
    buy_cost_bps: float
    sell_cost_bps: float
    total_cost_bps: float
    cost_model_id: str
    slippage_model_id: str
    provenance: MarketDataProvenanceReceipt | None
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "execution_record_id",
            "intent_id",
            "pm_decision_id",
            "decision_episode_id",
            "cost_model_id",
            "slippage_model_id",
        ):
            object.__setattr__(self, field_name, _validate_id(getattr(self, field_name), field_name))
        object.__setattr__(
            self,
            "source_state_change_id",
            _validate_optional_id(self.source_state_change_id, "source_state_change_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ExecutionContractError),
        )
        if self.status not in {"executed", "rejected"}:
            raise ExecutionContractError("status must be executed or rejected.")
        if self.execution_mode != "paper":
            raise ExecutionContractError("execution_mode must be paper.")
        for field_name in ("business_at", "decision_available_at", "recorded_at"):
            object.__setattr__(
                self,
                field_name,
                validate_timestamp(
                    getattr(self, field_name),
                    field_name=field_name,
                    error_type=ExecutionContractError,
                ),
            )
        if self.decision_available_at < self.business_at:
            raise ExecutionContractError(
                "decision_available_at must be at or after business_at."
            )
        if self.executed_at is not None:
            object.__setattr__(
                self,
                "executed_at",
                validate_timestamp(
                    self.executed_at,
                    field_name="executed_at",
                    error_type=ExecutionContractError,
                ),
            )
        object.__setattr__(
            self,
            "requested_target_weight",
            _validate_finite_float(self.requested_target_weight, "requested_target_weight"),
        )
        object.__setattr__(self, "target_weight", _optional_finite_float(self.target_weight, "target_weight"))
        object.__setattr__(
            self,
            "gross_target_weight",
            _optional_finite_float(self.gross_target_weight, "gross_target_weight"),
        )
        object.__setattr__(self, "raw_price", _optional_price(self.raw_price, "raw_price"))
        object.__setattr__(
            self,
            "adjusted_price",
            _optional_price(self.adjusted_price, "adjusted_price"),
        )
        object.__setattr__(
            self,
            "buy_cost_bps",
            _validate_non_negative_float(self.buy_cost_bps, "buy_cost_bps"),
        )
        object.__setattr__(
            self,
            "sell_cost_bps",
            _validate_non_negative_float(self.sell_cost_bps, "sell_cost_bps"),
        )
        object.__setattr__(
            self,
            "total_cost_bps",
            _validate_non_negative_float(self.total_cost_bps, "total_cost_bps"),
        )
        if self.price_basis not in {"open", "close"}:
            raise ExecutionContractError("price_basis must be open or close.")
        if self.provenance is not None and not isinstance(self.provenance, MarketDataProvenanceReceipt):
            raise ExecutionContractError(
                "provenance must be a MarketDataProvenanceReceipt when provided."
            )
        if self.status == "executed":
            if (
                self.executed_at is None
                or self.target_weight is None
                or self.gross_target_weight is None
                or self.raw_price is None
                or self.adjusted_price is None
                or self.provenance is None
            ):
                raise ExecutionContractError(
                    "executed records require execution time, weights, prices, and provenance."
                )
            if self.rejection_reason is not None:
                raise ExecutionContractError(
                    "rejection_reason must be None for executed records."
                )
        else:
            if not isinstance(self.rejection_reason, str) or not self.rejection_reason.strip():
                raise ExecutionContractError(
                    "rejected records require a non-blank rejection_reason."
                )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "execution_record_id": self.execution_record_id,
            "intent_id": self.intent_id,
            "pm_decision_id": self.pm_decision_id,
            "decision_episode_id": self.decision_episode_id,
            "source_state_change_id": self.source_state_change_id,
            "target_key": self.target_key,
            "status": self.status,
            "execution_mode": self.execution_mode,
            "business_at": self.business_at.isoformat(),
            "decision_available_at": self.decision_available_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "executed_at": None if self.executed_at is None else self.executed_at.isoformat(),
            "requested_target_weight": self.requested_target_weight,
            "target_weight": self.target_weight,
            "gross_target_weight": self.gross_target_weight,
            "price_basis": self.price_basis,
            "raw_price": self.raw_price,
            "adjusted_price": self.adjusted_price,
            "buy_cost_bps": self.buy_cost_bps,
            "sell_cost_bps": self.sell_cost_bps,
            "total_cost_bps": self.total_cost_bps,
            "cost_model_id": self.cost_model_id,
            "slippage_model_id": self.slippage_model_id,
            "provenance": None if self.provenance is None else self.provenance.to_json_payload(),
            "rejection_reason": self.rejection_reason,
        }


def parse_execution_intent(payload: Mapping[str, object]) -> ExecutionIntent:
    return ExecutionIntent(
        intent_id=_require_text(payload, "intent_id"),
        pm_decision_id=_require_text(payload, "pm_decision_id"),
        decision_episode_id=_require_text(payload, "decision_episode_id"),
        source_state_change_id=_optional_text(payload.get("source_state_change_id")),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        decision_available_at=_parse_timestamp(
            payload.get("decision_available_at", payload.get("business_at")),
            "decision_available_at",
        ),
        target_weight=_require_float_fallback(
            payload,
            "target_weight",
            legacy_field_name="approved_target_weight",
        ),
        actual_target_weight_before_decision=_require_float_fallback(
            payload,
            "actual_target_weight_before_decision",
            legacy_field_name="target_weight",
        ),
        requested_target_weight=_require_float(payload, "requested_target_weight"),
        execution_mode=cast(ExecutionMode, payload.get("execution_mode", "paper")),
    )


def parse_execution_record(payload: Mapping[str, object]) -> ExecutionRecord:
    provenance_payload = payload.get("provenance")
    provenance = None
    if provenance_payload is not None:
        if not isinstance(provenance_payload, Mapping):
            raise ExecutionContractError("provenance must be an object.")
        provenance = parse_market_data_provenance_receipt(provenance_payload)
    return ExecutionRecord(
        execution_record_id=_require_text(payload, "execution_record_id"),
        intent_id=_require_text(payload, "intent_id"),
        pm_decision_id=_require_text(payload, "pm_decision_id"),
        decision_episode_id=_require_text(payload, "decision_episode_id"),
        source_state_change_id=_optional_text(payload.get("source_state_change_id")),
        target_key=_require_text(payload, "target_key"),
        status=cast(ExecutionStatus, _require_text(payload, "status")),
        execution_mode=cast(ExecutionMode, _require_text(payload, "execution_mode")),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        decision_available_at=_parse_timestamp(
            payload.get("decision_available_at", payload.get("business_at")),
            "decision_available_at",
        ),
        recorded_at=_parse_timestamp(payload.get("recorded_at"), "recorded_at"),
        executed_at=_parse_optional_timestamp(payload.get("executed_at"), "executed_at"),
        requested_target_weight=_require_float(payload, "requested_target_weight"),
        target_weight=_optional_float(payload.get("target_weight")),
        gross_target_weight=_optional_float(payload.get("gross_target_weight")),
        price_basis=cast(ExecutionPriceBasis, _require_text(payload, "price_basis")),
        raw_price=_optional_float(payload.get("raw_price")),
        adjusted_price=_optional_float(payload.get("adjusted_price")),
        buy_cost_bps=_parse_legacy_or_current_side_cost_bps(
            payload,
            field_name="buy_cost_bps",
        ),
        sell_cost_bps=_parse_legacy_or_current_side_cost_bps(
            payload,
            field_name="sell_cost_bps",
        ),
        total_cost_bps=_require_float(payload, "total_cost_bps"),
        cost_model_id=_require_text(payload, "cost_model_id"),
        slippage_model_id=_require_text(payload, "slippage_model_id"),
        provenance=provenance,
        rejection_reason=_optional_text(payload.get("rejection_reason")),
    )


def parse_market_data_provenance_receipt(
    payload: Mapping[str, object],
) -> MarketDataProvenanceReceipt:
    return MarketDataProvenanceReceipt(
        provenance_id=_require_text(payload, "provenance_id"),
        provider=_require_text(payload, "provider"),
        target_key=_require_text(payload, "target_key"),
        market_symbol=_require_text(payload, "market_symbol"),
        instrument_symbol=_require_text(payload, "instrument_symbol"),
        proxy_symbol=_require_text(payload, "proxy_symbol"),
        market_session=cast(MarketSession, _require_text(payload, "market_session")),
        exchange_session_scope=cast(
            ExchangeSessionScope | None,
            _optional_text(payload.get("exchange_session_scope")),
        ),
        exchange=_optional_text(payload.get("exchange")),
        bar_granularity=_require_text(payload, "bar_granularity"),
        price_field_used=cast(ExecutionPriceBasis, _require_text(payload, "price_field_used")),
        market_data_hash=_optional_text(payload.get("market_data_hash")),
        availability_status=cast(
            MarketDataAvailabilityStatus,
            _require_text(payload, "availability_status"),
        ),
        requested_start_at=_parse_timestamp(payload.get("requested_start_at"), "requested_start_at"),
        requested_end_at=_parse_timestamp(payload.get("requested_end_at"), "requested_end_at"),
        selected_bar_start_at=_parse_optional_timestamp(
            payload.get("selected_bar_start_at"),
            "selected_bar_start_at",
        ),
        selected_bar_end_at=_parse_optional_timestamp(
            payload.get("selected_bar_end_at"),
            "selected_bar_end_at",
        ),
        selected_bar_hash=_optional_text(payload.get("selected_bar_hash")),
    )


def execution_contract_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def selected_bar_hash(bar: MarketDataBar) -> str:
    payload = {
        "start_at": bar.start_at.isoformat(),
        "end_at": bar.end_at.isoformat(),
        "open_price": bar.open_price,
        "high_price": bar.high_price,
        "low_price": bar.low_price,
        "close_price": bar.close_price,
        "volume": bar.volume,
        "vwap": bar.vwap,
    }
    return sha256(execution_contract_hash(payload).encode("utf-8")).hexdigest()


def classify_execution_source(record: ExecutionIntent | ExecutionRecord) -> ExecutionSourceKind:
    """Classify whether an execution record is legacy state-change or PM sourced."""

    if record.source_state_change_id is None:
        return "pm_decision"
    return "legacy_state_change"


def _validate_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionContractError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _validate_optional_id(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_id(value, field_name)


def _validate_finite_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ExecutionContractError(f"{field_name} must be numeric.")
    normalized = float(value)
    if not isfinite(normalized):
        raise ExecutionContractError(f"{field_name} must be finite.")
    return normalized


def _validate_non_negative_float(value: object, field_name: str) -> float:
    normalized = _validate_finite_float(value, field_name)
    if normalized < 0.0:
        raise ExecutionContractError(f"{field_name} must be non-negative.")
    return normalized


def _optional_finite_float(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    return _validate_finite_float(value, field_name)


def _optional_price(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    price = _validate_finite_float(value, field_name)
    if price <= 0.0:
        raise ExecutionContractError(f"{field_name} must be greater than zero.")
    return price


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_id(payload.get(field_name), field_name)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _validate_id(value, "optional_text")


def _require_float(payload: Mapping[str, object], field_name: str) -> float:
    return _validate_finite_float(payload.get(field_name), field_name)


def _require_float_fallback(
    payload: Mapping[str, object],
    field_name: str,
    *,
    legacy_field_name: str,
) -> float:
    value = payload.get(field_name)
    if value is None:
        value = payload.get(legacy_field_name)
    return _validate_finite_float(value, field_name)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return _validate_finite_float(value, "optional_float")


def _parse_legacy_or_current_side_cost_bps(
    payload: Mapping[str, object],
    *,
    field_name: str,
) -> float:
    current_value = payload.get(field_name)
    if current_value is not None:
        return _validate_non_negative_float(current_value, field_name)
    legacy_total = 0.0
    legacy_seen = False
    for legacy_field in ("slippage_bps", "commission_bps"):
        legacy_value = payload.get(legacy_field)
        if legacy_value is None:
            continue
        legacy_seen = True
        legacy_total += _validate_non_negative_float(legacy_value, legacy_field)
    if legacy_seen:
        return legacy_total
    return _validate_non_negative_float(
        payload.get("total_cost_bps"),
        "total_cost_bps",
    )


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ExecutionContractError(f"{field_name} must be an ISO timestamp string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExecutionContractError(f"{field_name} must be an ISO timestamp string.") from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=ExecutionContractError,
    )


def _parse_optional_timestamp(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_timestamp(value, field_name)


__all__ = [
    "ExecutionContractError",
    "ExecutionCostModel",
    "ExecutionIntent",
    "ExecutionMode",
    "ExecutionPriceBasis",
    "ExecutionRecord",
    "ExecutionSourceKind",
    "ExecutionStatus",
    "MarketDataAvailabilityStatus",
    "MarketDataProvenanceReceipt",
    "MissingBarPolicy",
    "classify_execution_source",
    "execution_contract_hash",
    "parse_execution_intent",
    "parse_execution_record",
    "parse_market_data_provenance_receipt",
    "selected_bar_hash",
]
