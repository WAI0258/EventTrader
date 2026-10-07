"""Deterministic paper execution engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    MarketMapping,
)
from event_trader.execution.contracts import (
    ExecutionCostModel,
    ExecutionIntent,
    ExecutionRecord,
    MarketDataProvenanceReceipt,
    MissingBarPolicy,
    execution_contract_hash,
    selected_bar_hash,
)
from event_trader.portfolio import PMDecision


class ExecutionError(ValueError):
    """Raised when deterministic paper execution cannot be evaluated."""


@dataclass(frozen=True, slots=True)
class ExecutionDeferred:
    """A non-terminal execution attempt awaiting observable market data."""

    intent_id: str
    retry_at: datetime
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.intent_id, str) or not self.intent_id.strip():
            raise ExecutionError("intent_id must be a non-blank string.")
        if not isinstance(self.retry_at, datetime) or self.retry_at.tzinfo is None:
            raise ExecutionError("retry_at must be a timezone-aware datetime.")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ExecutionError("reason must be a non-blank string.")


class MarketBarsProviderLike(Protocol):
    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at,
        end_at,
    ) -> MarketDataSeries:
        """Return market bars for execution selection."""


type MarketMappingResolver = Callable[[str], MarketMapping]
type ExecutionCostModelResolver = Callable[[str], ExecutionCostModel]


class PaperExecutionEngine:
    def __init__(
        self,
        *,
        resolve_market_mapping: MarketMappingResolver,
        market_data_provider: MarketBarsProviderLike,
        cost_model: ExecutionCostModel,
        resolve_cost_model: ExecutionCostModelResolver | None = None,
        missing_bar_policy: MissingBarPolicy = "reject",
        max_next_bar_wait: timedelta = timedelta(hours=72),
        provider_name: str = "market_data_provider",
    ) -> None:
        if not callable(resolve_market_mapping):
            raise ExecutionError("resolve_market_mapping must be callable.")
        if not callable(getattr(market_data_provider, "read_series", None)):
            raise ExecutionError("market_data_provider must implement read_series.")
        if not isinstance(cost_model, ExecutionCostModel):
            raise ExecutionError("cost_model must be an ExecutionCostModel instance.")
        if resolve_cost_model is not None and not callable(resolve_cost_model):
            raise ExecutionError("resolve_cost_model must be callable when provided.")
        if missing_bar_policy != "reject":
            raise ExecutionError("missing_bar_policy must be reject.")
        if max_next_bar_wait <= timedelta(0):
            raise ExecutionError("max_next_bar_wait must be greater than zero.")
        self._resolve_market_mapping = resolve_market_mapping
        self._market_data_provider = market_data_provider
        self._cost_model = cost_model
        self._resolve_cost_model = resolve_cost_model
        self._missing_bar_policy = missing_bar_policy
        self._max_next_bar_wait = max_next_bar_wait
        self._provider_name = provider_name

    def build_intent(
        self,
        *,
        decision: PMDecision,
        source_state_change_id: str | None = None,
    ) -> ExecutionIntent:
        return ExecutionIntent(
            intent_id=derive_execution_intent_id(
                decision=decision,
                source_state_change_id=source_state_change_id,
            ),
            pm_decision_id=decision.decision_id,
            decision_episode_id=decision.decision_episode_id,
            source_state_change_id=source_state_change_id,
            target_key=decision.target_key,
            business_at=decision.business_at,
            decision_available_at=decision.decision_available_at,
            target_weight=decision.requested_target_weight,
            actual_target_weight_before_decision=decision.actual_target_weight_before_decision,
            requested_target_weight=decision.requested_target_weight,
        )

    @property
    def resolve_market_mapping(self) -> MarketMappingResolver:
        return self._resolve_market_mapping

    @property
    def market_data_provider(self) -> MarketBarsProviderLike:
        return self._market_data_provider

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def max_next_bar_wait(self) -> timedelta:
        return self._max_next_bar_wait

    def execute(
        self,
        *,
        intent: ExecutionIntent,
        observed_at: datetime,
    ) -> ExecutionRecord | ExecutionDeferred:
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise ExecutionError("observed_at must be a timezone-aware datetime.")
        cost_model = self._cost_model_for_target(intent.target_key)
        mapping = self._resolve_market_mapping(intent.target_key)
        execution_available_at = intent.decision_available_at
        expiry_at = execution_available_at + self._max_next_bar_wait
        if observed_at <= execution_available_at:
            return _deferred_execution(
                intent=intent,
                observed_at=observed_at,
                expiry_at=expiry_at,
                reason="no market bar is observable after the PM decision yet.",
            )
        requested_end = min(observed_at, expiry_at)
        try:
            series = self._market_data_provider.read_series(
                mapping,
                start_at=execution_available_at,
                end_at=requested_end,
            )
        except Exception as exc:
            if observed_at < expiry_at:
                return _deferred_execution(
                    intent=intent,
                    observed_at=observed_at,
                    expiry_at=expiry_at,
                    reason=(
                        "market data is not observable for paper execution yet: "
                        f"{exc}"
                    ),
                )
            provenance = _provenance(
                provider=self._provider_name,
                mapping=mapping,
                price_field_used=cost_model.price_basis,
                requested_start_at=execution_available_at,
                requested_end_at=requested_end,
                selected_bar=None,
            )
            return _rejected_record(
                intent=intent,
                cost_model=cost_model,
                provenance=provenance,
                reason=f"market data unavailable for paper execution: {exc}",
            )
        if not isinstance(series, MarketDataSeries):
            if observed_at < expiry_at:
                return _deferred_execution(
                    intent=intent,
                    observed_at=observed_at,
                    expiry_at=expiry_at,
                    reason="market data provider has not produced an observable series yet.",
                )
            provenance = _provenance(
                provider=self._provider_name,
                mapping=mapping,
                price_field_used=cost_model.price_basis,
                requested_start_at=execution_available_at,
                requested_end_at=requested_end,
                selected_bar=None,
            )
            return _rejected_record(
                intent=intent,
                cost_model=cost_model,
                provenance=provenance,
                reason="market data provider did not return MarketDataSeries.",
            )
        selected = _select_next_observable_bar(
            series.bars,
            available_at=execution_available_at,
            observed_at=observed_at,
            price_basis=cost_model.price_basis,
        )
        provenance = _provenance(
            provider=self._provider_name,
            mapping=mapping,
            price_field_used=cost_model.price_basis,
            requested_start_at=execution_available_at,
            requested_end_at=requested_end,
            selected_bar=selected,
        )
        if selected is None:
            if observed_at < expiry_at:
                return _deferred_execution(
                    intent=intent,
                    observed_at=observed_at,
                    expiry_at=expiry_at,
                    reason="no tradable market bar is observable after the PM decision yet.",
                )
            return _rejected_record(
                intent=intent,
                cost_model=cost_model,
                provenance=provenance,
                reason="no tradable bar found for paper execution.",
            )
        raw_price = (
            selected.open_price
            if cost_model.price_basis == "open"
            else selected.close_price
        )
        delta_weight = intent.delta_weight
        total_cost_bps, adjusted_price = resolve_adjusted_execution_price(
            raw_price=raw_price,
            delta_weight=delta_weight,
            cost_model=cost_model,
        )
        return ExecutionRecord(
            execution_record_id=derive_execution_record_id(intent=intent),
            intent_id=intent.intent_id,
            pm_decision_id=intent.pm_decision_id,
            decision_episode_id=intent.decision_episode_id,
            source_state_change_id=intent.source_state_change_id,
            target_key=intent.target_key,
            status="executed",
            execution_mode="paper",
            business_at=intent.business_at,
            decision_available_at=intent.decision_available_at,
            recorded_at=intent.decision_available_at,
            executed_at=selected.start_at,
            requested_target_weight=intent.requested_target_weight,
            target_weight=intent.target_weight,
            gross_target_weight=abs(intent.target_weight),
            price_basis=cost_model.price_basis,
            raw_price=raw_price,
            adjusted_price=adjusted_price,
            buy_cost_bps=cost_model.buy_cost_bps,
            sell_cost_bps=cost_model.sell_cost_bps,
            total_cost_bps=total_cost_bps,
            cost_model_id=_cost_model_id(cost_model),
            slippage_model_id=_slippage_model_id(cost_model),
            provenance=provenance,
        )

    def _cost_model_for_target(self, target_key: str) -> ExecutionCostModel:
        if self._resolve_cost_model is None:
            return self._cost_model
        cost_model = self._resolve_cost_model(target_key)
        if not isinstance(cost_model, ExecutionCostModel):
            raise ExecutionError("resolve_cost_model must return an ExecutionCostModel instance.")
        return cost_model


def derive_execution_intent_id(
    *,
    decision: PMDecision,
    source_state_change_id: str | None,
) -> str:
    digest = execution_contract_hash(
        {
            "pm_decision_id": decision.decision_id,
            "decision_episode_id": decision.decision_episode_id,
            "source_state_change_id": source_state_change_id,
            "target_key": decision.target_key,
            "business_at": decision.business_at.isoformat(),
            "decision_available_at": decision.decision_available_at.isoformat(),
            "actual_target_weight_before_decision": decision.actual_target_weight_before_decision,
            "requested_target_weight": decision.requested_target_weight,
        }
    )
    return f"execution-intent:{digest[:32]}"


def derive_execution_record_id(*, intent: ExecutionIntent) -> str:
    digest = execution_contract_hash(
        {
            "intent_id": intent.intent_id,
            "source_state_change_id": intent.source_state_change_id,
            "target_key": intent.target_key,
            "business_at": intent.business_at.isoformat(),
            "decision_available_at": intent.decision_available_at.isoformat(),
        }
    )
    return f"execution-record:{digest[:32]}"


def _select_next_observable_bar(
    bars: tuple[MarketDataBar, ...],
    *,
    available_at: datetime,
    observed_at: datetime,
    price_basis: str,
) -> MarketDataBar | None:
    for bar in sorted(bars, key=lambda item: item.start_at):
        price_observable_at = (
            bar.start_at if price_basis == "open" else bar.end_at
        )
        if bar.start_at >= available_at and price_observable_at <= observed_at:
            return bar
    return None


def _deferred_execution(
    *,
    intent: ExecutionIntent,
    observed_at: datetime,
    expiry_at: datetime,
    reason: str,
) -> ExecutionDeferred:
    return ExecutionDeferred(
        intent_id=intent.intent_id,
        retry_at=min(observed_at + timedelta(minutes=1), expiry_at),
        reason=reason,
    )


def _provenance(
    *,
    provider: str,
    mapping: MarketMapping,
    price_field_used,
    requested_start_at,
    requested_end_at,
    selected_bar: MarketDataBar | None,
) -> MarketDataProvenanceReceipt:
    bar_hash = None if selected_bar is None else selected_bar_hash(selected_bar)
    payload = {
        "provider": provider,
        "target_key": mapping.target_key,
        "market_symbol": mapping.market_symbol,
        "instrument_symbol": mapping.market_symbol,
        "proxy_symbol": mapping.market_symbol,
        "market_session": mapping.market_session,
        "exchange_session_scope": mapping.exchange_session_scope,
        "exchange": mapping.exchange,
        "bar_granularity": mapping.bar_granularity,
        "price_field_used": price_field_used,
        "market_data_hash": bar_hash,
        "availability_status": "unavailable" if selected_bar is None else "available",
        "requested_start_at": requested_start_at.isoformat(),
        "requested_end_at": requested_end_at.isoformat(),
        "selected_bar_start_at": (
            None if selected_bar is None else selected_bar.start_at.isoformat()
        ),
        "selected_bar_end_at": (
            None if selected_bar is None else selected_bar.end_at.isoformat()
        ),
        "selected_bar_hash": bar_hash,
    }
    return MarketDataProvenanceReceipt(
        provenance_id=f"market-provenance:{execution_contract_hash(payload)[:32]}",
        provider=provider,
        target_key=mapping.target_key,
        market_symbol=mapping.market_symbol,
        instrument_symbol=mapping.market_symbol,
        proxy_symbol=mapping.market_symbol,
        market_session=mapping.market_session,
        exchange_session_scope=mapping.exchange_session_scope,
        exchange=mapping.exchange,
        bar_granularity=mapping.bar_granularity,
        price_field_used=price_field_used,
        market_data_hash=bar_hash,
        availability_status="unavailable" if selected_bar is None else "available",
        requested_start_at=requested_start_at,
        requested_end_at=requested_end_at,
        selected_bar_start_at=None if selected_bar is None else selected_bar.start_at,
        selected_bar_end_at=None if selected_bar is None else selected_bar.end_at,
        selected_bar_hash=bar_hash,
    )


def _rejected_record(
    *,
    intent: ExecutionIntent,
    cost_model: ExecutionCostModel,
    provenance: MarketDataProvenanceReceipt,
    reason: str,
) -> ExecutionRecord:
    return ExecutionRecord(
        execution_record_id=derive_execution_record_id(intent=intent),
        intent_id=intent.intent_id,
        pm_decision_id=intent.pm_decision_id,
        decision_episode_id=intent.decision_episode_id,
        source_state_change_id=intent.source_state_change_id,
        target_key=intent.target_key,
        status="rejected",
        execution_mode="paper",
        business_at=intent.business_at,
        decision_available_at=intent.decision_available_at,
        recorded_at=intent.decision_available_at,
        executed_at=None,
        requested_target_weight=intent.requested_target_weight,
        target_weight=None,
        gross_target_weight=None,
        price_basis=cost_model.price_basis,
        raw_price=None,
        adjusted_price=None,
        buy_cost_bps=cost_model.buy_cost_bps,
        sell_cost_bps=cost_model.sell_cost_bps,
        total_cost_bps=0.0,
        cost_model_id=_cost_model_id(cost_model),
        slippage_model_id=_slippage_model_id(cost_model),
        provenance=provenance,
        rejection_reason=reason,
    )


def _cost_model_id(cost_model: ExecutionCostModel) -> str:
    return (
        f"paper-cost:buy-{cost_model.buy_cost_bps}:"
        f"sell-{cost_model.sell_cost_bps}:price-{cost_model.price_basis}"
    )


def _slippage_model_id(cost_model: ExecutionCostModel) -> str:
    return (
        "paper-execution-cost:"
        f"buy-{cost_model.buy_cost_bps}:sell-{cost_model.sell_cost_bps}:delta-weight"
    )


def resolve_adjusted_execution_price(
    *,
    raw_price: float,
    delta_weight: float,
    cost_model: ExecutionCostModel,
) -> tuple[float, float]:
    if delta_weight > 0.0:
        applied_cost_bps = cost_model.buy_cost_bps * abs(delta_weight)
        return applied_cost_bps, raw_price * (1.0 + applied_cost_bps / 10_000.0)
    if delta_weight < 0.0:
        applied_cost_bps = cost_model.sell_cost_bps * abs(delta_weight)
        return applied_cost_bps, raw_price * (1.0 - applied_cost_bps / 10_000.0)
    return 0.0, raw_price


__all__ = [
    "ExecutionError",
    "ExecutionDeferred",
    "MarketBarsProviderLike",
    "MarketMappingResolver",
    "PaperExecutionEngine",
    "derive_execution_intent_id",
    "derive_execution_record_id",
    "resolve_adjusted_execution_price",
    "selected_bar_hash",
]
