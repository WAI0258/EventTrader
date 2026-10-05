"""Catalog and orchestration helpers for replay rule-based baselines."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date

from event_trader.contracts.view_state_change import MarketDataSeries

from .baselines import (
    BaselineDirectionMode,
    DailyStrategyPoint,
    calculate_bollinger_bands_baseline,
    calculate_buy_and_hold_baseline,
    calculate_ema_crossover_baseline,
    calculate_macd_baseline,
    calculate_rsi_baseline,
    calculate_sma_crossover_baseline,
)


class RuleBaselineCatalogError(ValueError):
    """Raised when requested rule baselines are unknown or malformed."""


@dataclass(frozen=True, slots=True)
class RuleBaselineParameters:
    sma_fast_periods: int = 50
    sma_slow_periods: int = 200
    ema_fast_periods: int = 20
    ema_slow_periods: int = 50
    macd_fast_periods: int = 12
    macd_slow_periods: int = 26
    macd_signal_periods: int = 9
    rsi_periods: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    bollinger_periods: int = 20
    bollinger_stddev: float = 2.0

    def to_json_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RuleBaselineSpec:
    spec_id: str
    baseline_id: str
    parameters: RuleBaselineParameters


@dataclass(frozen=True, slots=True)
class RuleBaselineSeries:
    spec_id: str
    baseline_id: str
    label: str
    color: str
    parameters: RuleBaselineParameters
    points: tuple[DailyStrategyPoint, ...]


@dataclass(frozen=True, slots=True)
class _RuleBaselineDefinition:
    baseline_id: str
    color: str


_RULE_BASELINES: tuple[_RuleBaselineDefinition, ...] = (
    _RuleBaselineDefinition(
        baseline_id="buy_and_hold",
        color="#6f42c1",
    ),
    _RuleBaselineDefinition(
        baseline_id="sma_crossover",
        color="#ffb000",
    ),
    _RuleBaselineDefinition(
        baseline_id="ema_crossover",
        color="#1f77b4",
    ),
    _RuleBaselineDefinition(
        baseline_id="macd",
        color="#2ca02c",
    ),
    _RuleBaselineDefinition(
        baseline_id="rsi",
        color="#66c2a5",
    ),
    _RuleBaselineDefinition(
        baseline_id="bollinger",
        color="#7f7f7f",
    ),
)


def available_rule_baseline_ids() -> tuple[str, ...]:
    return tuple(item.baseline_id for item in _RULE_BASELINES)


def baseline_parameter_payload_for_id(
    baseline_id: str,
    parameters: RuleBaselineParameters,
) -> dict[str, object]:
    if baseline_id == "buy_and_hold":
        return {}
    if baseline_id == "sma_crossover":
        return {
            "fast_periods": parameters.sma_fast_periods,
            "slow_periods": parameters.sma_slow_periods,
        }
    if baseline_id == "ema_crossover":
        return {
            "fast_periods": parameters.ema_fast_periods,
            "slow_periods": parameters.ema_slow_periods,
        }
    if baseline_id == "macd":
        return {
            "fast_periods": parameters.macd_fast_periods,
            "slow_periods": parameters.macd_slow_periods,
            "signal_periods": parameters.macd_signal_periods,
        }
    if baseline_id == "rsi":
        return {
            "periods": parameters.rsi_periods,
            "oversold": parameters.rsi_oversold,
            "overbought": parameters.rsi_overbought,
        }
    if baseline_id == "bollinger":
        return {
            "periods": parameters.bollinger_periods,
            "stddev_multiplier": parameters.bollinger_stddev,
        }
    raise RuleBaselineCatalogError(f"unsupported baseline id: {baseline_id!r}")


def build_rule_baseline_series(
    market_data: MarketDataSeries,
    *,
    start_date: date | None,
    direction_mode: BaselineDirectionMode,
    specs: Sequence[RuleBaselineSpec] | None = None,
    parameters: RuleBaselineParameters | None = None,
    buy_hold_entry_cost_bps: float = 0.0,
    baseline_ids: Sequence[str] | None = None,
) -> tuple[RuleBaselineSeries, ...]:
    if specs is not None and (baseline_ids is not None or parameters is not None):
        raise RuleBaselineCatalogError(
            "specs mode cannot be combined with baseline_ids or parameters."
        )
    selected = _selected_specs(
        specs=specs,
        baseline_ids=baseline_ids,
        parameters=parameters,
    )
    series: list[RuleBaselineSeries] = []
    for spec, definition in selected:
        series.append(
            RuleBaselineSeries(
                spec_id=spec.spec_id,
                baseline_id=definition.baseline_id,
                label=_baseline_label(definition.baseline_id, spec.parameters),
                color=definition.color,
                parameters=spec.parameters,
                points=_build_baseline_points(
                    definition.baseline_id,
                    market_data=market_data,
                    start_date=start_date,
                    direction_mode=direction_mode,
                    parameters=spec.parameters,
                    buy_hold_entry_cost_bps=buy_hold_entry_cost_bps,
                ),
            )
        )
    return tuple(series)


def _selected_specs(
    *,
    specs: Sequence[RuleBaselineSpec] | None,
    baseline_ids: Sequence[str] | None,
    parameters: RuleBaselineParameters | None,
) -> tuple[tuple[RuleBaselineSpec, _RuleBaselineDefinition], ...]:
    definitions_by_id = {item.baseline_id: item for item in _RULE_BASELINES}
    if specs is not None:
        selected_specs = tuple(specs)
        if not selected_specs:
            raise RuleBaselineCatalogError("at least one baseline spec must be selected.")
        selected: list[tuple[RuleBaselineSpec, _RuleBaselineDefinition]] = []
        seen_spec_ids: set[str] = set()
        for spec in selected_specs:
            if not isinstance(spec, RuleBaselineSpec):
                raise RuleBaselineCatalogError(
                    "specs must contain only RuleBaselineSpec values."
                )
            if spec.spec_id in seen_spec_ids:
                raise RuleBaselineCatalogError(
                    f"baseline spec {spec.spec_id!r} was selected more than once."
                )
            definition = definitions_by_id.get(spec.baseline_id)
            if definition is None:
                raise RuleBaselineCatalogError(
                    f"unknown baseline id: {spec.baseline_id!r}"
                )
            selected.append((spec, definition))
            seen_spec_ids.add(spec.spec_id)
        return tuple(selected)

    resolved_parameters = parameters or RuleBaselineParameters()
    if baseline_ids is None:
        return tuple(
            (
                RuleBaselineSpec(
                    spec_id=definition.baseline_id,
                    baseline_id=definition.baseline_id,
                    parameters=resolved_parameters,
                ),
                definition,
            )
            for definition in _RULE_BASELINES
        )

    selected: list[tuple[RuleBaselineSpec, _RuleBaselineDefinition]] = []
    seen_baseline_ids: set[str] = set()
    for raw_value in baseline_ids:
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise RuleBaselineCatalogError("baseline ids must be non-blank strings.")
        baseline_id = raw_value.strip()
        if baseline_id in seen_baseline_ids:
            raise RuleBaselineCatalogError(
                f"baseline {baseline_id!r} was selected more than once."
            )
        definition = definitions_by_id.get(baseline_id)
        if definition is None:
            raise RuleBaselineCatalogError(f"unknown baseline id: {baseline_id!r}")
        selected.append(
            (
                RuleBaselineSpec(
                    spec_id=baseline_id,
                    baseline_id=baseline_id,
                    parameters=resolved_parameters,
                ),
                definition,
            )
        )
        seen_baseline_ids.add(baseline_id)
    if not selected:
        raise RuleBaselineCatalogError("at least one baseline must be selected.")
    return tuple(selected)


def _baseline_label(baseline_id: str, parameters: RuleBaselineParameters) -> str:
    if baseline_id == "buy_and_hold":
        return "Buy Hold"
    if baseline_id == "sma_crossover":
        return f"SMA {parameters.sma_fast_periods}/{parameters.sma_slow_periods}"
    if baseline_id == "ema_crossover":
        return f"EMA {parameters.ema_fast_periods}/{parameters.ema_slow_periods}"
    if baseline_id == "macd":
        return (
            "MACD "
            f"{parameters.macd_fast_periods}/"
            f"{parameters.macd_slow_periods}/"
            f"{parameters.macd_signal_periods}"
        )
    if baseline_id == "rsi":
        return f"RSI {parameters.rsi_periods}"
    if baseline_id == "bollinger":
        return (
            "Bollinger "
            f"{parameters.bollinger_periods}/"
            f"{parameters.bollinger_stddev:g}"
        )
    raise RuleBaselineCatalogError(f"unsupported baseline id: {baseline_id!r}")


def _build_baseline_points(
    baseline_id: str,
    *,
    market_data: MarketDataSeries,
    start_date: date | None,
    direction_mode: BaselineDirectionMode,
    parameters: RuleBaselineParameters,
    buy_hold_entry_cost_bps: float,
) -> tuple[DailyStrategyPoint, ...]:
    if baseline_id == "buy_and_hold":
        return calculate_buy_and_hold_baseline(
            market_data,
            start_date=start_date,
            entry_buy_cost_bps=buy_hold_entry_cost_bps,
        )
    if baseline_id == "sma_crossover":
        return calculate_sma_crossover_baseline(
            market_data,
            fast_periods=parameters.sma_fast_periods,
            slow_periods=parameters.sma_slow_periods,
            direction_mode=direction_mode,
            start_date=start_date,
        )
    if baseline_id == "ema_crossover":
        return calculate_ema_crossover_baseline(
            market_data,
            fast_periods=parameters.ema_fast_periods,
            slow_periods=parameters.ema_slow_periods,
            direction_mode=direction_mode,
            start_date=start_date,
        )
    if baseline_id == "macd":
        return calculate_macd_baseline(
            market_data,
            fast_periods=parameters.macd_fast_periods,
            slow_periods=parameters.macd_slow_periods,
            signal_periods=parameters.macd_signal_periods,
            direction_mode=direction_mode,
            start_date=start_date,
        )
    if baseline_id == "rsi":
        return calculate_rsi_baseline(
            market_data,
            periods=parameters.rsi_periods,
            oversold=parameters.rsi_oversold,
            overbought=parameters.rsi_overbought,
            direction_mode=direction_mode,
            start_date=start_date,
        )
    if baseline_id == "bollinger":
        return calculate_bollinger_bands_baseline(
            market_data,
            periods=parameters.bollinger_periods,
            stddev_multiplier=parameters.bollinger_stddev,
            direction_mode=direction_mode,
            start_date=start_date,
        )
    raise RuleBaselineCatalogError(f"unsupported baseline id: {baseline_id!r}")


__all__ = [
    "baseline_parameter_payload_for_id",
    "RuleBaselineCatalogError",
    "RuleBaselineParameters",
    "RuleBaselineSpec",
    "RuleBaselineSeries",
    "available_rule_baseline_ids",
    "build_rule_baseline_series",
]
