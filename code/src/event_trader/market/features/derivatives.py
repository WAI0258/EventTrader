"""Deterministic derivatives market-context features."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median

from event_trader.market.contracts import (
    DerivativesContext,
    MarketContextComponentStatus,
    OptionActivitySummary,
    OptionBarObservation,
    OptionContextAvailability,
    OptionContractActivity,
    OptionContractSnapshot,
    OptionIvGreeksSummary,
    OptionNotableContract,
    OptionSelectionPolicy,
    OptionTradeObservation,
    OptionUnusualActivitySummary,
    OptionVolumeSummary,
)

_DERIVATIVES_VERSION = "derivatives_option_activity_2026_05"
_ATM_MONEYNESS_WINDOW = 0.02
_UNUSUAL_SEVERITY_ORDER = {
    "insufficient_baseline": 0,
    "normal": 1,
    "elevated": 2,
    "unusual": 3,
    "extreme": 4,
}


def build_derivatives_context(
    *,
    underlying_symbol: str,
    underlying_price: float | None,
    as_of_at: datetime,
    policy: OptionSelectionPolicy,
    chain: tuple[OptionContractSnapshot, ...],
    trades: tuple[OptionTradeObservation, ...] = (),
    bars: tuple[OptionBarObservation, ...] = (),
) -> DerivativesContext:
    """Build compact replay-safe option activity from visible option observations."""
    as_of_at = _as_utc_datetime(as_of_at)
    if underlying_price is None:
        return _unavailable_context(
            underlying_symbol=underlying_symbol,
            underlying_price=None,
            policy=policy,
            reason="unavailable_underlying_price",
        )

    visible_chain = tuple(
        row
        for row in chain
        if _snapshot_visible_at(row, as_of_at=as_of_at)
        and _expiry_days(row, as_of_at=as_of_at) >= policy.expiry_days_min
        and _expiry_days(row, as_of_at=as_of_at) <= policy.expiry_days_max
        and _within_strike_window(row, underlying_price=underlying_price, policy=policy)
    )
    if not visible_chain:
        return _unavailable_context(
            underlying_symbol=underlying_symbol,
            underlying_price=underlying_price,
            policy=policy,
            reason="no_chain_rows_visible_at_as_of",
        )

    selected = _select_contracts(
        visible_chain,
        underlying_price=underlying_price,
        policy=policy,
    )
    if not selected:
        return _unavailable_context(
            underlying_symbol=underlying_symbol,
            underlying_price=underlying_price,
            policy=policy,
            reason="no_selected_contracts",
        )

    selected_symbols = frozenset(contract.contract_symbol for contract in selected)
    visible_trades = tuple(
        trade
        for trade in trades
        if trade.contract_symbol in selected_symbols and trade.observed_at <= as_of_at
    )
    visible_bars = tuple(
        bar
        for bar in bars
        if bar.contract_symbol in selected_symbols and bar.end_at <= as_of_at
    )
    activity_by_contract = _activity_by_contract(
        selected,
        trades=visible_trades,
        bars=visible_bars,
    )
    candidate_activities = tuple(
        activity_by_contract[contract.contract_symbol] for contract in selected
    )
    activities = tuple(
        activity
        for activity in candidate_activities
        if activity.trade_count >= policy.min_trade_count
    )
    filtered_low_trade_count = len(candidate_activities) - len(activities)
    if not activities:
        return _unavailable_context(
            underlying_symbol=underlying_symbol,
            underlying_price=underlying_price,
            policy=policy,
            reason="no_contracts_meet_min_trade_count",
            field_status={
                "chain": "available",
                "selected_contracts": "unavailable:no_contracts_meet_min_trade_count",
                "min_trade_count": (
                    f"unavailable:all_selected_contracts_below_{policy.min_trade_count}"
                ),
            },
            unavailable_fields=("selected_contracts", "min_trade_count"),
        )

    eligible_symbols = frozenset(activity.contract_symbol for activity in activities)
    eligible_selected = tuple(
        contract for contract in selected if contract.contract_symbol in eligible_symbols
    )
    selected_indices = dict((contract.contract_symbol, contract) for contract in eligible_selected)
    volume_summary = _volume_summary(
        activities,
        underlying_price=underlying_price,
        as_of_at=as_of_at,
    )
    activity_summary = OptionActivitySummary(
        large_trade_count=sum(
            1
            for trade in visible_trades
            if trade.notional >= policy.large_trade_notional_threshold
        ),
        large_trade_notional=sum(
            trade.notional
            for trade in visible_trades
            if trade.notional >= policy.large_trade_notional_threshold
        ),
        selected_contract_count=len(activities),
        eligible_contract_count=len(activities),
        filtered_low_trade_count=filtered_low_trade_count,
        source_contract_count=len(visible_chain),
        visible_trade_count=len(visible_trades),
        visible_bar_count=len(visible_bars),
    )
    iv_greeks_summary = _iv_greeks_summary(activities, selected=eligible_selected)
    notable_contracts, unusual_activity_summary = _unusual_activity_summary(
        selected=selected_indices,
        activities=activities,
        trades=visible_trades,
        bars=visible_bars,
        as_of_at=as_of_at,
        policy=policy,
    )
    field_status = _field_status(
        policy=policy,
        selected=eligible_selected,
        trades=visible_trades,
        bars=visible_bars,
        iv_greeks_summary=iv_greeks_summary,
        unusual_activity_summary=unusual_activity_summary,
        filtered_low_trade_count=filtered_low_trade_count,
    )
    unavailable_fields = tuple(
        sorted(field for field, status in field_status.items() if status != "available")
    )
    status_value = "available" if not unavailable_fields else "partial"
    return DerivativesContext(
        underlying_symbol=underlying_symbol,
        underlying_price=underlying_price,
        policy=policy,
        availability=OptionContextAvailability(
            chain_status="available",
            trades_status=field_status["trades"],
            bars_status=field_status["bars"],
            latest_trades_status=field_status["latest_trades"],
            latest_quotes_status=field_status["latest_quotes"],
            historical_universe_status=field_status["historical_universe"],
            unusual_activity_status=field_status["unusual_activity"],
            iv_status=field_status["iv"],
            greeks_status=field_status["greeks"],
            quote_status=_snapshot_quote_status(
                eligible_selected,
                include_latest_snapshot_quotes=policy.include_latest_snapshot_quotes,
            ),
        ),
        volume_summary=volume_summary,
        activity_summary=activity_summary,
        iv_greeks_summary=iv_greeks_summary,
        top_contracts_by_volume=_top_contracts(
            activities,
            key_name="volume",
            limit=policy.notable_contract_limit,
        ),
        top_contracts_by_notional=_top_contracts(
            activities,
            key_name="notional",
            limit=policy.notable_contract_limit,
        ),
        top_contracts_by_trade_count=_top_contracts(
            activities,
            key_name="trade_count",
            limit=policy.notable_contract_limit,
        ),
        notable_contracts=notable_contracts,
        unusual_activity_summary=unusual_activity_summary,
        selected_contract_symbols=tuple(activity.contract_symbol for activity in activities),
        field_status=field_status,
        status=MarketContextComponentStatus(
            component="derivatives",
            status=status_value,
            unavailable_fields=unavailable_fields,
        ),
    )


def derivatives_calculation_version() -> str:
    return _DERIVATIVES_VERSION


def build_unavailable_derivatives_context(
    *,
    underlying_symbol: str,
    underlying_price: float | None,
    policy: OptionSelectionPolicy,
    reason: str,
) -> DerivativesContext:
    """Build an explicit unavailable derivatives component."""
    return _unavailable_context(
        underlying_symbol=underlying_symbol,
        underlying_price=underlying_price,
        policy=policy,
        reason=reason,
    )


def select_derivatives_contracts(
    chain: tuple[OptionContractSnapshot, ...],
    *,
    underlying_price: float,
    as_of_at: datetime,
    policy: OptionSelectionPolicy,
) -> tuple[OptionContractSnapshot, ...]:
    """Return the deterministic selected-contract set used by derivatives context."""
    as_of_at = _as_utc_datetime(as_of_at)
    visible_chain = tuple(
        row
        for row in chain
        if _snapshot_visible_at(row, as_of_at=as_of_at)
        and _expiry_days(row, as_of_at=as_of_at) >= policy.expiry_days_min
        and _expiry_days(row, as_of_at=as_of_at) <= policy.expiry_days_max
        and _within_strike_window(row, underlying_price=underlying_price, policy=policy)
    )
    return _select_contracts(
        visible_chain,
        underlying_price=underlying_price,
        policy=policy,
    )


def _unavailable_context(
    *,
    underlying_symbol: str,
    underlying_price: float | None,
    policy: OptionSelectionPolicy,
    reason: str,
    field_status: dict[str, str] | None = None,
    unavailable_fields: tuple[str, ...] = ("chain", "selected_contracts"),
) -> DerivativesContext:
    normalized_field_status = field_status or {
        "chain": f"unavailable:{reason}",
        "selected_contracts": "unavailable",
    }
    return DerivativesContext(
        underlying_symbol=underlying_symbol,
        underlying_price=underlying_price,
        policy=policy,
        availability=OptionContextAvailability(
            chain_status=f"unavailable:{reason}",
            trades_status="unavailable:no_selected_contracts",
            bars_status="unavailable:no_selected_contracts",
            latest_trades_status="unavailable:no_selected_contracts",
            latest_quotes_status="unavailable:no_selected_contracts",
            historical_universe_status="unavailable:no_selected_contracts",
            unusual_activity_status="unavailable:no_selected_contracts",
            iv_status="unavailable:no_visible_snapshots",
            greeks_status="unavailable:no_visible_snapshots",
            quote_status="unavailable:no_visible_snapshots",
        ),
        volume_summary=None,
        activity_summary=None,
        iv_greeks_summary=None,
        top_contracts_by_volume=(),
        top_contracts_by_notional=(),
        top_contracts_by_trade_count=(),
        notable_contracts=(),
        unusual_activity_summary=None,
        selected_contract_symbols=(),
        field_status=normalized_field_status,
        status=MarketContextComponentStatus(
            component="derivatives",
            status="unavailable",
            reason=reason,
            unavailable_fields=unavailable_fields,
        ),
    )


def _select_contracts(
    chain: tuple[OptionContractSnapshot, ...],
    *,
    underlying_price: float,
    policy: OptionSelectionPolicy,
) -> tuple[OptionContractSnapshot, ...]:
    by_type = {
        "call": [row for row in chain if row.option_type == "call"],
        "put": [row for row in chain if row.option_type == "put"],
    }
    selected: list[OptionContractSnapshot] = []
    for option_type in ("call", "put"):
        rows = sorted(
            by_type[option_type],
            key=lambda row: (
                abs(row.strike_price - underlying_price),
                row.expiry_date,
                row.strike_price,
                row.contract_symbol,
            ),
        )
        selected.extend(rows[: policy.atm_contract_count])
        if option_type == "call":
            otm = [row for row in rows if row.strike_price > underlying_price]
        else:
            otm = [row for row in rows if row.strike_price < underlying_price]
        selected.extend(otm[: policy.otm_contract_count])

    deduped = tuple(dict.fromkeys(selected))
    ranked = sorted(
        deduped,
        key=lambda row: (
            -_snapshot_volume(row),
            abs(row.strike_price - underlying_price),
            row.expiry_date,
            row.option_type,
            row.contract_symbol,
        ),
    )
    return tuple(ranked[: policy.max_contracts_per_snapshot])


def _activity_by_contract(
    selected: tuple[OptionContractSnapshot, ...],
    *,
    trades: tuple[OptionTradeObservation, ...],
    bars: tuple[OptionBarObservation, ...],
) -> dict[str, OptionContractActivity]:
    trade_volume: defaultdict[str, float] = defaultdict(float)
    trade_count: defaultdict[str, int] = defaultdict(int)
    trade_notional: defaultdict[str, float] = defaultdict(float)
    bar_volume: defaultdict[str, float] = defaultdict(float)
    bar_count: defaultdict[str, int] = defaultdict(int)
    for trade in trades:
        trade_volume[trade.contract_symbol] += trade.size
        trade_count[trade.contract_symbol] += 1
        trade_notional[trade.contract_symbol] += trade.notional
    for bar in bars:
        bar_volume[bar.contract_symbol] += bar.volume
        bar_count[bar.contract_symbol] += bar.trade_count or 0

    activities: dict[str, OptionContractActivity] = {}
    for contract in selected:
        volume = trade_volume[contract.contract_symbol]
        if volume == 0:
            volume = bar_volume[contract.contract_symbol] or _snapshot_volume(contract)
        count = trade_count[contract.contract_symbol]
        if count == 0:
            count = bar_count[contract.contract_symbol] or contract.trade_count or 0
        notional = trade_notional[contract.contract_symbol]
        if notional == 0:
            reference_price = contract.latest_trade_price or _mid_quote(contract) or 0.0
            notional = volume * reference_price * 100.0
        activities[contract.contract_symbol] = OptionContractActivity(
            contract_symbol=contract.contract_symbol,
            option_type=contract.option_type,
            expiry_date=contract.expiry_date,
            strike_price=contract.strike_price,
            volume=volume,
            trade_count=count,
            notional=notional,
            implied_volatility=contract.implied_volatility,
            delta=contract.delta,
            gamma=contract.gamma,
        )
    return activities


def _unusual_activity_summary(
    *,
    selected: dict[str, OptionContractSnapshot],
    activities: tuple[OptionContractActivity, ...],
    trades: tuple[OptionTradeObservation, ...],
    bars: tuple[OptionBarObservation, ...],
    as_of_at: datetime,
    policy: OptionSelectionPolicy,
) -> tuple[tuple[OptionNotableContract, ...], OptionUnusualActivitySummary]:
    observation_series = _observation_series(trades=trades, bars=bars)
    contract_details: list[tuple[float, OptionNotableContract]] = []
    contracts_with_sufficient_baseline = 0
    contracts_with_insufficient_baseline = 0
    unusual_contract_count = 0
    elevated_contract_count = 0
    normal_contract_count = 0
    for activity in activities:
        (
            status,
            ratios,
            baseline_observation_count,
        ) = _unusual_status_and_ratios(
            events=observation_series.get(activity.contract_symbol, ()),
            as_of_at=as_of_at,
            policy=policy,
            current=activity,
        )
        if status == "insufficient_baseline":
            contracts_with_insufficient_baseline += 1
        else:
            contracts_with_sufficient_baseline += 1
        max_ratio = max((value for value in ratios.values() if value is not None), default=0.0)
        if status == "elevated":
            elevated_contract_count += 1
        elif status in ("unusual", "extreme"):
            unusual_contract_count += 1
        elif status == "normal":
            normal_contract_count += 1
        contract_details.append(
            (
                _unusual_severity_rank(status),
                OptionNotableContract(
                    contract_symbol=activity.contract_symbol,
                    option_type=activity.option_type,
                    expiry_date=activity.expiry_date,
                    strike_price=activity.strike_price,
                    expiry_days=_expiry_days_from_string(activity.expiry_date, as_of_at),
                    unusual_label=status,
                    max_unusual_ratio=max_ratio,
                    unusual_ratio_volume=ratios["volume"],
                    unusual_ratio_notional=ratios["notional"],
                    unusual_ratio_trade_count=ratios["trade_count"],
                    notional=activity.notional,
                    volume=activity.volume,
                    trade_count=activity.trade_count,
                    near_expiry=_expiry_days_from_string(activity.expiry_date, as_of_at)
                    <= 7,
                ),
            )
        )
    ranked = sorted(
        contract_details,
        key=lambda item: (
            -item[0],
            -item[1].max_unusual_ratio,
            -item[1].notional,
            -item[1].volume,
            -item[1].trade_count,
            -1 if item[1].near_expiry else 0,
            item[1].contract_symbol,
        ),
    )
    selected_notables = tuple(item[1] for item in ranked[: policy.notable_contract_limit])
    max_unusual_ratio = max(
        (item.max_unusual_ratio for item in selected_notables),
        default=None,
    )
    summary_status = "normal"
    if contracts_with_insufficient_baseline > 0 and (
        contracts_with_sufficient_baseline == 0
    ):
        summary_status = "insufficient_baseline"
    elif unusual_contract_count > 0:
        summary_status = "extreme" if any(
            contract.unusual_label == "extreme" for contract in selected_notables
        ) else "unusual"
    elif elevated_contract_count > 0:
        summary_status = "elevated"
    return selected_notables, OptionUnusualActivitySummary(
        status=summary_status,
        baseline_windows_days=policy.unusual_baseline_windows_days,
        baseline_min_observations=policy.unusual_min_baseline_observations,
        sufficient_baseline_contract_count=contracts_with_sufficient_baseline,
        insufficient_baseline_contract_count=contracts_with_insufficient_baseline,
        unusual_contract_count=unusual_contract_count,
        elevated_contract_count=elevated_contract_count,
        normal_contract_count=normal_contract_count,
        aggregate_volume=sum(item.volume for item in activities),
        aggregate_notional=sum(item.notional for item in activities),
        aggregate_trade_count=sum(item.trade_count for item in activities),
        max_unusual_ratio=max_unusual_ratio,
    )


def _observation_series(
    *,
    trades: tuple[OptionTradeObservation, ...],
    bars: tuple[OptionBarObservation, ...],
) -> dict[str, tuple[tuple[datetime, float, float, int], ...]]:
    series: defaultdict[str, list[tuple[datetime, float, float, int]]] = defaultdict(
        list
    )
    for trade in trades:
        series[trade.contract_symbol].append(
            (trade.observed_at, trade.size, trade.notional, 1),
        )
    for bar in bars:
        series[bar.contract_symbol].append(
            (
                bar.end_at,
                bar.volume,
                (bar.vwap or bar.close_price) * bar.volume * 100.0,
                bar.trade_count or 0,
            ),
        )
    return {
        symbol: tuple(sorted(values, key=lambda item: item[0]))
        for symbol, values in series.items()
    }


def _unusual_status_and_ratios(
    *,
    events: tuple[tuple[datetime, float, float, int], ...],
    as_of_at: datetime,
    policy: OptionSelectionPolicy,
    current: OptionContractActivity,
) -> tuple[str, dict[str, float | None], int]:
    windows_days = tuple(sorted(set(policy.unusual_baseline_windows_days)))
    if len(windows_days) == 1:
        windows_days = (windows_days[0], windows_days[0] * 2)
    current_window_days = windows_days[0]
    current_start = as_of_at - timedelta(days=current_window_days)
    current_volume, current_notional, current_trade_count, _ = _aggregate_window_totals(
        events,
        start=current_start,
        end=as_of_at,
    )
    baseline_windows: list[tuple[float, float, float, int]] = []
    for index in range(1, len(windows_days)):
        window_end = as_of_at - timedelta(days=windows_days[index - 1])
        window_start = as_of_at - timedelta(days=windows_days[index])
        baseline_windows.append(
            _aggregate_window_totals(
                events,
                start=window_start,
                end=window_end,
            )
        )

    baseline_observation_count = sum(item[3] for item in baseline_windows)
    if baseline_observation_count < policy.unusual_min_baseline_observations:
        return (
            "insufficient_baseline",
            {
                "volume": None,
                "notional": None,
                "trade_count": None,
            },
            baseline_observation_count,
        )
    baseline_span_days = [
        max((window_end - window_start).total_seconds() / 86400.0, 1 / 24)
        for window_start, window_end in (
            (
                as_of_at - timedelta(days=windows_days[index]),
                as_of_at - timedelta(days=windows_days[index - 1]),
            )
            for index in range(1, len(windows_days))
        )
    ]
    baseline_volume = sum(item[0] for item in baseline_windows)
    baseline_notional = sum(item[1] for item in baseline_windows)
    baseline_trade_count = sum(item[2] for item in baseline_windows)
    baseline_rates = {
        "volume": (
            baseline_volume
            / sum(baseline_span_days)
            if len(baseline_windows) > 0
            else None
        ),
        "notional": (
            baseline_notional
            / sum(baseline_span_days)
            if len(baseline_windows) > 0
            else None
        ),
        "trade_count": (
            baseline_trade_count
            / sum(baseline_span_days)
            if len(baseline_windows) > 0
            else None
        ),
    }
    current_rates = {
        "volume": _daily_rate(
            current_volume,
            (as_of_at - current_start).total_seconds(),
        ),
        "notional": _daily_rate(
            current_notional,
            (as_of_at - current_start).total_seconds(),
        ),
        "trade_count": _daily_rate(
            current_trade_count,
            (as_of_at - current_start).total_seconds(),
        ),
    }
    ratios: dict[str, float | None] = {}
    for key in ("volume", "notional", "trade_count"):
        baseline_rate = baseline_rates[key]
        if baseline_rate is None or baseline_rate <= 0:
            ratios[key] = None
        else:
            ratios[key] = current_rates[key] / baseline_rate
    max_ratio = max(
        (ratio for ratio in ratios.values() if ratio is not None), default=0.0
    )
    if max_ratio >= policy.unusual_extreme_ratio:
        return "extreme", ratios, baseline_observation_count
    if max_ratio >= policy.unusual_unusual_ratio:
        return "unusual", ratios, baseline_observation_count
    if max_ratio >= policy.unusual_elevated_ratio:
        return "elevated", ratios, baseline_observation_count
    return "normal", ratios, baseline_observation_count


def _aggregate_window_totals(
    events: tuple[tuple[datetime, float, float, int], ...],
    *,
    start: datetime,
    end: datetime,
) -> tuple[float, float, float, int]:
    volume = 0.0
    notional = 0.0
    trade_count = 0.0
    observations = 0
    for ts, event_volume, event_notional, event_count in events:
        if ts > start and ts <= end:
            volume += event_volume
            notional += event_notional
            trade_count += event_count
            observations += 1
    return volume, notional, trade_count, observations


def _daily_rate(value: float, delta_seconds: float) -> float:
    span_days = delta_seconds / 86400.0
    if span_days <= 0:
        return 0.0
    return value / span_days


def _field_status(
    *,
    policy: OptionSelectionPolicy,
    selected: tuple[OptionContractSnapshot, ...],
    trades: tuple[OptionTradeObservation, ...],
    bars: tuple[OptionBarObservation, ...],
    iv_greeks_summary: OptionIvGreeksSummary,
    unusual_activity_summary: OptionUnusualActivitySummary,
    filtered_low_trade_count: int,
) -> dict[str, str]:
    return {
        "chain": "available",
        "selected_contracts": "available",
        "min_trade_count": (
            f"partial:filtered_low_trade_count={filtered_low_trade_count}"
            if filtered_low_trade_count > 0
            else "available"
        ),
        "trades": (
            "available"
            if trades
            else _missing_status(policy.include_historical_trades)
        ),
        "bars": (
            "available" if bars else _missing_status(policy.include_historical_bars)
        ),
        "latest_trades": (
            "available"
            if any(
                contract.latest_trade_at is not None for contract in selected
            )
            else _missing_status(policy.include_latest_trades)
        ),
        "latest_quotes": (
            "available"
            if any(contract.latest_quote_at is not None for contract in selected)
            else _missing_status(policy.include_latest_quotes)
        ),
        "historical_universe": (
            "unavailable:historical_universe_reconstruction_disabled"
            if not policy.historical_universe_reconstruction_enabled
            else "available"
        ),
        "unusual_activity": (
            "available"
            if unusual_activity_summary.status != "insufficient_baseline"
            else "partial:insufficient_baseline"
        ),
        "iv": (
            "available"
            if iv_greeks_summary.iv_available_count > 0
            else "unavailable:no_iv_in_visible_snapshots"
        ),
        "greeks": (
            "available"
            if iv_greeks_summary.greeks_available_count > 0
            else "unavailable:no_greeks_in_visible_snapshots"
        ),
        "strict_gex": iv_greeks_summary.strict_gex_status,
    }


def _volume_summary(
    activities: tuple[OptionContractActivity, ...],
    *,
    underlying_price: float,
    as_of_at: datetime,
) -> OptionVolumeSummary:
    call_volume = sum(item.volume for item in activities if item.option_type == "call")
    put_volume = sum(item.volume for item in activities if item.option_type == "put")
    ratio = call_volume / put_volume if put_volume > 0 else None
    return OptionVolumeSummary(
        total_option_volume=call_volume + put_volume,
        call_volume=call_volume,
        put_volume=put_volume,
        call_put_volume_ratio=ratio,
        total_trade_count=sum(item.trade_count for item in activities),
        total_notional=sum(item.notional for item in activities),
        near_expiry_volume=sum(
            item.volume
            for item in activities
            if _expiry_days_from_string(item.expiry_date, as_of_at) <= 7
        ),
        atm_volume=sum(
            item.volume
            for item in activities
            if abs(item.strike_price / underlying_price - 1.0) <= _ATM_MONEYNESS_WINDOW
        ),
        otm_volume=sum(
            item.volume
            for item in activities
            if (
                (item.option_type == "call" and item.strike_price > underlying_price)
                or (item.option_type == "put" and item.strike_price < underlying_price)
            )
        ),
    )


def _iv_greeks_summary(
    activities: tuple[OptionContractActivity, ...],
    *,
    selected: tuple[OptionContractSnapshot, ...],
) -> OptionIvGreeksSummary:
    ivs = tuple(
        item.implied_volatility
        for item in activities
        if item.implied_volatility is not None
    )
    greeks_available = tuple(
        item
        for item in activities
        if item.delta is not None or item.gamma is not None
    )
    gamma_available = tuple(item for item in activities if item.gamma is not None)
    delta_weighted = _weighted_activity(
        tuple((item.delta, item.notional) for item in activities if item.delta is not None)
    )
    gamma_weighted = _weighted_activity(
        tuple((item.gamma, item.notional) for item in activities if item.gamma is not None)
    )
    strict_gex_status = (
        "available"
        if any(contract.open_interest is not None for contract in selected)
        else "unavailable_open_interest_not_documented"
    )
    return OptionIvGreeksSummary(
        iv_min=min(ivs) if ivs else None,
        iv_max=max(ivs) if ivs else None,
        iv_median=median(ivs) if ivs else None,
        iv_available_count=len(ivs),
        greeks_available_count=len(greeks_available),
        gamma_available_count=len(gamma_available),
        delta_weighted_activity=delta_weighted,
        gamma_weighted_activity=gamma_weighted,
        strict_gex_status=strict_gex_status,
    )


def _top_contracts(
    activities: tuple[OptionContractActivity, ...],
    *,
    key_name: str,
    limit: int,
) -> tuple[OptionContractActivity, ...]:
    return tuple(
        sorted(
            activities,
            key=lambda item: (
                -float(getattr(item, key_name)),
                item.expiry_date,
                item.option_type,
                item.strike_price,
                item.contract_symbol,
            ),
        )[:limit]
    )


def _snapshot_visible_at(row: OptionContractSnapshot, *, as_of_at: datetime) -> bool:
    timestamps = tuple(
        value
        for value in (row.observed_at, row.latest_trade_at, row.latest_quote_at)
        if value is not None
    )
    if not timestamps and _metadata_only_snapshot(row):
        return True
    return bool(timestamps) and max(timestamps) <= as_of_at


def _metadata_only_snapshot(row: OptionContractSnapshot) -> bool:
    return all(
        value is None
        for value in (
            row.observed_at,
            row.volume,
            row.trade_count,
            row.latest_trade_price,
            row.latest_trade_size,
            row.latest_trade_at,
            row.latest_quote_at,
            row.bid_price,
            row.ask_price,
            row.bid_size,
            row.ask_size,
            row.implied_volatility,
            row.delta,
            row.gamma,
            row.rho,
            row.theta,
            row.vega,
            row.open_interest,
        )
    )


def _within_strike_window(
    row: OptionContractSnapshot,
    *,
    underlying_price: float,
    policy: OptionSelectionPolicy,
) -> bool:
    return abs(row.strike_price / underlying_price - 1.0) <= policy.strike_pct_window


def _snapshot_volume(row: OptionContractSnapshot) -> float:
    return row.volume or row.latest_trade_size or 0.0


def _mid_quote(row: OptionContractSnapshot) -> float | None:
    if row.bid_price is None or row.ask_price is None:
        return None
    return (row.bid_price + row.ask_price) / 2.0


def _snapshot_quote_status(
    selected: tuple[OptionContractSnapshot, ...],
    *,
    include_latest_snapshot_quotes: bool,
) -> str:
    if not include_latest_snapshot_quotes:
        return "disabled:not_configured"
    if any(contract.latest_quote_at is not None for contract in selected):
        return "available"
    return "unavailable:no_latest_quote_in_visible_snapshots"


def _expiry_days(row: OptionContractSnapshot, *, as_of_at: datetime) -> int:
    return _expiry_days_from_string(row.expiry_date, as_of_at)


def _expiry_days_from_string(expiry_date: str, as_of_at: datetime) -> int:
    expiry = datetime.fromisoformat(expiry_date).date()
    return (expiry - as_of_at.date()).days


def _weighted_activity(values: tuple[tuple[float | None, float], ...]) -> float | None:
    if not values:
        return None
    return sum((value or 0.0) * weight for value, weight in values)


def _missing_status(configured: bool) -> str:
    return "unavailable:no_visible_observations" if configured else "disabled:not_configured"


def _unusual_severity_rank(label: str) -> int:
    return _UNUSUAL_SEVERITY_ORDER.get(label, 0)


def _as_utc_datetime(value: datetime) -> datetime:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC)


__all__ = [
    "build_derivatives_context",
    "build_unavailable_derivatives_context",
    "derivatives_calculation_version",
    "select_derivatives_contracts",
]
