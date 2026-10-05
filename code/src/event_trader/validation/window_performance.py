"""Windowed validation performance over canonical state-changes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from statistics import mean, stdev

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.contracts.view_state_change import (
    MarketDataBar,
    MarketDataSeries,
    ViewState,
    ViewStateChange,
)
from event_trader.execution.contracts import ExecutionRecord
from event_trader.market.adjustments import (
    MarketAdjustmentSidecar,
    adjust_price_for_realized_policy,
    apply_adjustment_policy_to_series,
)
from event_trader.validation.execution_linkage import execution_record_by_state_change


class ValidationWindowPerformanceError(ValueError):
    """Raised when windowed validation performance inputs are invalid."""


@dataclass(frozen=True, slots=True)
class ValidationWindowBarReturn:
    """One deterministic bar-to-bar or terminal-mark return inside a window."""

    target_key: str
    state: ViewState
    target_weight: float
    interval_start_at: datetime
    interval_end_at: datetime
    mark_mode: str
    entry_price: float
    exit_price: float
    underlying_return: float
    strategy_return: float
    cumulative_underlying_equity: float
    cumulative_strategy_equity: float
    rolling_sharpe: float | None


@dataclass(frozen=True, slots=True)
class ValidationWindowPerformanceSummary:
    """Compact window-level summary for deterministic validation review."""

    target_key: str
    window_start: datetime
    window_end: datetime
    bar_count: int
    total_underlying_return: float
    total_strategy_return: float
    mean_bar_strategy_return: float
    bar_return_volatility: float
    sharpe_ratio: float | None
    max_drawdown: float
    annual_periods: float
    rolling_sharpe_bars: int


@dataclass(frozen=True, slots=True)
class ValidationWindowPerformanceResult:
    """Bar return stream plus summary for one validation time window."""

    summary: ValidationWindowPerformanceSummary
    bar_returns: tuple[ValidationWindowBarReturn, ...]


@dataclass(frozen=True, slots=True)
class _EffectiveStateChange:
    state_change_id: str
    target_key: str
    state: ViewState
    target_weight: float
    effective_at: datetime
    execution_entry_price: float | None


def calculate_window_performance(
    *,
    target_key: str,
    state_changes: tuple[ViewStateChange, ...],
    market_data: MarketDataSeries,
    window_start: datetime,
    window_end: datetime,
    annual_periods: float,
    rolling_sharpe_bars: int = 20,
    execution_records: tuple[ExecutionRecord, ...] = (),
    require_execution_records: bool = False,
    adjustment_sidecar: MarketAdjustmentSidecar | None = None,
) -> ValidationWindowPerformanceResult:
    """Calculate deterministic window performance without replay reruns."""
    validated_target_key = validate_target_key(
        target_key,
        error_type=ValidationWindowPerformanceError,
    )
    validated_window_start = validate_timestamp(
        window_start,
        field_name="window_start",
        error_type=ValidationWindowPerformanceError,
    )
    validated_window_end = validate_timestamp(
        window_end,
        field_name="window_end",
        error_type=ValidationWindowPerformanceError,
    )
    if validated_window_end < validated_window_start:
        raise ValidationWindowPerformanceError(
            "window_end must be greater than or equal to window_start."
        )
    if annual_periods <= 0.0:
        raise ValidationWindowPerformanceError("annual_periods must be greater than zero.")
    if rolling_sharpe_bars <= 1:
        raise ValidationWindowPerformanceError(
            "rolling_sharpe_bars must be greater than one."
        )

    series = (
        apply_adjustment_policy_to_series(series=market_data, sidecar=adjustment_sidecar).series
        if adjustment_sidecar is not None
        else market_data
    )
    bars = _select_window_bars(
        market_data=series,
        window_start=validated_window_start,
        window_end=validated_window_end,
    )
    relevant_state_changes = _effective_state_changes(
        target_key=validated_target_key,
        state_changes=tuple(
            sorted(
                (
                    state_change
                    for state_change in state_changes
                    if state_change.target_key == validated_target_key
                    and state_change.effective_at <= validated_window_end
                ),
                key=lambda state_change: state_change.effective_at,
            )
        ),
        execution_records=execution_records,
        require_execution_records=require_execution_records,
    )
    if not relevant_state_changes:
        raise ValidationWindowPerformanceError(
            "No canonical state-changes exist for target "
            f"{validated_target_key!r} at or before window_end."
    )
    current_state_change_index = 0
    active_state: ViewState = "flat"
    active_weight = 0.0
    while (
        current_state_change_index < len(relevant_state_changes)
        and relevant_state_changes[current_state_change_index].effective_at < bars[0].start_at
    ):
        current = relevant_state_changes[current_state_change_index]
        active_state = current.state
        active_weight = current.target_weight
        current_state_change_index += 1

    cumulative_underlying_equity = 1.0
    cumulative_strategy_equity = 1.0
    strategy_returns: list[float] = []
    bar_returns: list[ValidationWindowBarReturn] = []

    for index, bar in enumerate(bars):
        entry_price = bar.open_price
        while (
            current_state_change_index < len(relevant_state_changes)
            and relevant_state_changes[current_state_change_index].effective_at <= bar.start_at
        ):
            current = relevant_state_changes[current_state_change_index]
            active_state = current.state
            active_weight = current.target_weight
            if (
                current.effective_at == bar.start_at
                and current.execution_entry_price is not None
            ):
                entry_price = (
                    adjust_price_for_realized_policy(
                        raw_price=current.execution_entry_price,
                        timestamp=current.effective_at,
                        sidecar=adjustment_sidecar,
                    )
                    if adjustment_sidecar is not None
                    else current.execution_entry_price
                )
            current_state_change_index += 1

        next_bar = bars[index + 1] if index + 1 < len(bars) else None
        if next_bar is not None and next_bar.start_at <= validated_window_end:
            interval_end_at = next_bar.start_at
            exit_price = next_bar.open_price
            mark_mode = "next_open"
        else:
            interval_end_at = min(validated_window_end, bar.end_at)
            exit_price = bar.close_price
            mark_mode = "terminal_close"

        underlying_return = exit_price / entry_price - 1.0
        strategy_return = active_weight * underlying_return
        cumulative_underlying_equity *= 1.0 + underlying_return
        cumulative_strategy_equity *= 1.0 + strategy_return
        strategy_returns.append(strategy_return)
        rolling_sharpe = _calculate_sharpe(
            strategy_returns[-rolling_sharpe_bars:],
            annual_periods=annual_periods,
        )
        if len(strategy_returns) < rolling_sharpe_bars:
            rolling_sharpe = None

        bar_returns.append(
            ValidationWindowBarReturn(
                target_key=validated_target_key,
                state=active_state,
                target_weight=active_weight,
                interval_start_at=bar.start_at,
                interval_end_at=interval_end_at,
                mark_mode=mark_mode,
                entry_price=entry_price,
                exit_price=exit_price,
                underlying_return=underlying_return,
                strategy_return=strategy_return,
                cumulative_underlying_equity=cumulative_underlying_equity,
                cumulative_strategy_equity=cumulative_strategy_equity,
                rolling_sharpe=rolling_sharpe,
            )
        )
        if mark_mode == "terminal_close":
            break

    max_drawdown = _calculate_max_drawdown(
        tuple(item.cumulative_strategy_equity for item in bar_returns)
    )
    summary = ValidationWindowPerformanceSummary(
        target_key=validated_target_key,
        window_start=validated_window_start,
        window_end=validated_window_end,
        bar_count=len(bar_returns),
        total_underlying_return=cumulative_underlying_equity - 1.0,
        total_strategy_return=cumulative_strategy_equity - 1.0,
        mean_bar_strategy_return=mean(strategy_returns),
        bar_return_volatility=_calculate_volatility(strategy_returns),
        sharpe_ratio=_calculate_sharpe(strategy_returns, annual_periods=annual_periods),
        max_drawdown=max_drawdown,
        annual_periods=annual_periods,
        rolling_sharpe_bars=rolling_sharpe_bars,
    )
    return ValidationWindowPerformanceResult(
        summary=summary,
        bar_returns=tuple(bar_returns),
    )


def _effective_state_changes(
    *,
    target_key: str,
    state_changes: tuple[ViewStateChange, ...],
    execution_records: tuple[ExecutionRecord, ...],
    require_execution_records: bool,
) -> tuple[_EffectiveStateChange, ...]:
    if not execution_records:
        if require_execution_records:
            raise ValidationWindowPerformanceError(
                "execution-era validation requires execution records."
            )
        return tuple(
            _EffectiveStateChange(
                state_change_id=item.state_change_id,
                target_key=item.target_key,
                state=item.state,
                target_weight=item.target_weight,
                effective_at=item.effective_at,
                execution_entry_price=None,
            )
            for item in state_changes
        )

    records_by_state_change = _latest_execution_records_by_state_change(
        target_key=target_key,
        state_changes=state_changes,
        execution_records=execution_records,
    )
    effective: list[_EffectiveStateChange] = []
    for item in state_changes:
        record = records_by_state_change.get(item.state_change_id)
        if record is None:
            if require_execution_records:
                raise ValidationWindowPerformanceError(
                    "Missing execution record for state_change_id "
                    f"{item.state_change_id!r}."
                )
            effective.append(
                _EffectiveStateChange(
                    state_change_id=item.state_change_id,
                    target_key=item.target_key,
                    state=item.state,
                    target_weight=item.target_weight,
                    effective_at=item.effective_at,
                    execution_entry_price=None,
                )
            )
            continue
        if record.status != "executed":
            if require_execution_records:
                raise ValidationWindowPerformanceError(
                    "Execution record for state_change_id "
                    f"{item.state_change_id!r} was not executed."
                )
            continue
        if (
            record.executed_at is None
            or record.target_weight is None
            or record.adjusted_price is None
        ):
            raise ValidationWindowPerformanceError(
                "Executed record is missing executed_at, target_weight, or adjusted_price."
            )
        effective.append(
            _EffectiveStateChange(
                state_change_id=item.state_change_id,
                target_key=item.target_key,
                state=item.state,
                target_weight=record.target_weight,
                effective_at=record.executed_at,
                execution_entry_price=record.adjusted_price,
            )
        )
    return tuple(sorted(effective, key=lambda item: item.effective_at))


def _latest_execution_records_by_state_change(
    *,
    target_key: str,
    state_changes: tuple[ViewStateChange, ...],
    execution_records: tuple[ExecutionRecord, ...],
) -> dict[str, ExecutionRecord]:
    return execution_record_by_state_change(
        target_key=target_key,
        state_changes=state_changes,
        execution_records=execution_records,
    )


def render_window_performance_svg(
    *,
    result: ValidationWindowPerformanceResult,
    title: str,
) -> str:
    """Render a compact SVG with cumulative strategy equity and rolling Sharpe."""
    if not isinstance(result, ValidationWindowPerformanceResult):
        raise ValidationWindowPerformanceError(
            "result must be a ValidationWindowPerformanceResult instance."
        )
    width = 960
    height = 640
    left = 70
    right = 30
    top = 40
    middle = 330
    panel_height = 220
    plot_width = width - left - right
    equity_values = [item.cumulative_strategy_equity for item in result.bar_returns]
    sharpe_values = [
        0.0 if item.rolling_sharpe is None else item.rolling_sharpe
        for item in result.bar_returns
    ]
    equity_path = _series_path(
        values=equity_values,
        left=left,
        top=top + 30,
        width=plot_width,
        height=panel_height,
    )
    sharpe_path = _series_path(
        values=sharpe_values,
        left=left,
        top=middle + 30,
        width=plot_width,
        height=panel_height,
    )
    last_equity = equity_values[-1]
    last_sharpe = sharpe_values[-1]
    sharpe_summary = (
        "n/a"
        if result.summary.sharpe_ratio is None
        else f"{result.summary.sharpe_ratio:.4f}"
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        'viewBox="0 0 960 640" role="img" aria-labelledby="title desc">'
        f'<title>{_escape_xml(title)}</title>'
        '<desc>Validation window equity curve and rolling Sharpe.</desc>'
        '<rect width="100%" height="100%" fill="#faf7f0"/>'
        f'<text x="{left}" y="28" font-family="Arial" font-size="20" fill="#1f2937">'
        f'{_escape_xml(title)}</text>'
        f'<text x="{left}" y="58" font-family="Arial" font-size="12" fill="#4b5563">'
        f'Window: {result.summary.window_start.date().isoformat()} to '
        f'{result.summary.window_end.date().isoformat()}</text>'
        f'<text x="{left}" y="86" font-family="Arial" font-size="14" fill="#1f2937">'
        'Cumulative Strategy Equity</text>'
        f'<rect x="{left}" y="{top + 30}" width="{plot_width}" height="{panel_height}" '
        'fill="none" stroke="#d1d5db" stroke-width="1"/>'
        f'<path d="{equity_path}" fill="none" stroke="#0f766e" stroke-width="2.5"/>'
        f'<text x="{width - right - 180}" y="{top + 54}" font-family="Arial" '
        f'font-size="12" fill="#0f766e">Last equity: {last_equity:.4f}</text>'
        f'<text x="{left}" y="{middle + 18}" font-family="Arial" font-size="14" fill="#1f2937">'
        f'Rolling Sharpe ({result.summary.rolling_sharpe_bars} bars)</text>'
        f'<rect x="{left}" y="{middle + 30}" width="{plot_width}" height="{panel_height}" '
        'fill="none" stroke="#d1d5db" stroke-width="1"/>'
        f'<path d="{sharpe_path}" fill="none" stroke="#b45309" stroke-width="2.5"/>'
        f'<line x1="{left}" y1="{middle + 140}" x2="{left + plot_width}" y2="{middle + 140}" '
        'stroke="#e5e7eb" stroke-width="1" stroke-dasharray="4 4"/>'
        f'<text x="{width - right - 180}" y="{middle + 54}" font-family="Arial" '
        f'font-size="12" fill="#b45309">Last rolling Sharpe: {last_sharpe:.4f}</text>'
        f'<text x="{left}" y="{height - 58}" font-family="Arial" font-size="12" fill="#374151">'
        f'Total strategy return: {result.summary.total_strategy_return * 100.0:.2f}%</text>'
        f'<text x="{left}" y="{height - 40}" font-family="Arial" font-size="12" fill="#374151">'
        f"Sharpe: {sharpe_summary}</text>"
        f'<text x="{left + 220}" y="{height - 40}" font-family="Arial" '
        'font-size="12" fill="#374151">'
        f'Max drawdown: {result.summary.max_drawdown * 100.0:.2f}%</text>'
        '</svg>'
    )


def render_window_market_kline_svg(
    *,
    target_key: str,
    market_data: MarketDataSeries,
    state_changes: tuple[ViewStateChange, ...],
    window_start: datetime,
    window_end: datetime,
    title: str,
) -> str:
    """Render the validation window's actual OHLC bars with state exposure."""
    validated_target_key = validate_target_key(
        target_key,
        error_type=ValidationWindowPerformanceError,
    )
    validated_window_start = validate_timestamp(
        window_start,
        field_name="window_start",
        error_type=ValidationWindowPerformanceError,
    )
    validated_window_end = validate_timestamp(
        window_end,
        field_name="window_end",
        error_type=ValidationWindowPerformanceError,
    )
    if validated_window_end < validated_window_start:
        raise ValidationWindowPerformanceError(
            "window_end must be greater than or equal to window_start."
        )

    bars = _select_window_bars(
        market_data=market_data,
        window_start=validated_window_start,
        window_end=validated_window_end,
    )
    relevant_state_changes = tuple(
        state_change
        for state_change in state_changes
        if state_change.target_key == validated_target_key
        and state_change.effective_at <= validated_window_end
    )
    state_by_bar = _resolve_bar_states(
        bars=bars,
        state_changes=relevant_state_changes,
    )

    width = 1080
    height = 560
    left = 76
    right = 34
    top = 78
    plot_height = 350
    plot_width = width - left - right
    bottom = top + plot_height
    label_y = bottom + 46
    min_price = min(bar.low_price for bar in bars)
    max_price = max(bar.high_price for bar in bars)
    if min_price == max_price:
        min_price -= 1.0
        max_price += 1.0
    candle_slot = plot_width / len(bars)
    candle_width = max(2.0, min(10.0, candle_slot * 0.58))

    exposure_rects: list[str] = []
    candles: list[str] = []
    for index, bar in enumerate(bars):
        x = left + candle_slot * (index + 0.5)
        state, target_weight = state_by_bar[index]
        exposure_color = _state_color(state)
        exposure_opacity = 0.04 + min(abs(target_weight), 1.0) * 0.12
        exposure_rects.append(
            f'<rect x="{x - candle_slot / 2.0:.2f}" y="{top}" '
            f'width="{candle_slot:.2f}" height="{plot_height}" '
            f'fill="{exposure_color}" opacity="{exposure_opacity:.3f}"/>'
        )

        open_y = _scale_value(bar.open_price, min_price, max_price, top, plot_height)
        close_y = _scale_value(bar.close_price, min_price, max_price, top, plot_height)
        high_y = _scale_value(bar.high_price, min_price, max_price, top, plot_height)
        low_y = _scale_value(bar.low_price, min_price, max_price, top, plot_height)
        body_top = min(open_y, close_y)
        body_height = max(abs(close_y - open_y), 1.0)
        candle_color = "#0f766e" if bar.close_price >= bar.open_price else "#b91c1c"
        candles.append(
            f'<line x1="{x:.2f}" y1="{high_y:.2f}" x2="{x:.2f}" y2="{low_y:.2f}" '
            f'stroke="{candle_color}" stroke-width="1.2"/>'
            f'<rect x="{x - candle_width / 2.0:.2f}" y="{body_top:.2f}" '
            f'width="{candle_width:.2f}" height="{body_height:.2f}" '
            f'fill="{candle_color}" opacity="0.82"/>'
        )

    first_bar = bars[0]
    last_bar = bars[-1]
    last_close = last_bar.close_price
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        'viewBox="0 0 1080 560" role="img" aria-labelledby="title desc">'
        f'<title>{_escape_xml(title)}</title>'
        '<desc>Actual OHLC candlestick chart for the validation window with '
        'background exposure bands from canonical state changes.</desc>'
        '<rect width="100%" height="100%" fill="#fbf7ef"/>'
        f'<text x="{left}" y="32" font-family="Arial" font-size="20" fill="#1f2937">'
        f'{_escape_xml(title)}</text>'
        f'<text x="{left}" y="56" font-family="Arial" font-size="12" fill="#4b5563">'
        f'Window: {validated_window_start.isoformat()} to '
        f'{validated_window_end.isoformat()} | Bars: {len(bars)}</text>'
        f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" '
        'fill="none" stroke="#d1d5db" stroke-width="1"/>'
        f'{"".join(exposure_rects)}'
        f'{"".join(candles)}'
        f'<line x1="{left}" y1="{top}" x2="{left + plot_width}" y2="{top}" '
        'stroke="#e5e7eb" stroke-width="1"/>'
        f'<line x1="{left}" y1="{bottom}" x2="{left + plot_width}" y2="{bottom}" '
        'stroke="#e5e7eb" stroke-width="1"/>'
        f'<text x="{left}" y="{label_y}" font-family="Arial" font-size="12" fill="#374151">'
        f'First bar: {first_bar.start_at.isoformat()} open={first_bar.open_price:.4f}</text>'
        f'<text x="{left + 390}" y="{label_y}" font-family="Arial" font-size="12" '
        f'fill="#374151">Last bar: {last_bar.start_at.isoformat()} close={last_close:.4f}</text>'
        f'<text x="{left}" y="{label_y + 22}" font-family="Arial" font-size="12" '
        'fill="#374151">Exposure bands: green=long, red=short, gray=flat; '
        'opacity follows absolute target weight.</text>'
        f'<text x="{width - right - 170}" y="{top + 18}" font-family="Arial" '
        f'font-size="12" fill="#374151">High: {max_price:.4f}</text>'
        f'<text x="{width - right - 170}" y="{bottom - 8}" font-family="Arial" '
        f'font-size="12" fill="#374151">Low: {min_price:.4f}</text>'
        '</svg>'
    )


def render_window_presentation_zh_svg(
    *,
    result: ValidationWindowPerformanceResult,
    market_symbol: str,
    title: str,
) -> str:
    """Render a Chinese presentation chart for human review."""
    if not isinstance(result, ValidationWindowPerformanceResult):
        raise ValidationWindowPerformanceError(
            "result must be a ValidationWindowPerformanceResult instance."
        )
    width = 1180
    height = 620
    left = 86
    right = 46
    top = 150
    plot_height = 320
    plot_width = width - left - right
    strategy_values = [item.cumulative_strategy_equity for item in result.bar_returns]
    underlying_values = [
        item.cumulative_underlying_equity for item in result.bar_returns
    ]
    min_value = min((*strategy_values, *underlying_values))
    max_value = max((*strategy_values, *underlying_values))
    if min_value == max_value:
        min_value -= 1.0
        max_value += 1.0
    strategy_path = _series_path_with_range(
        values=strategy_values,
        left=left,
        top=top,
        width=plot_width,
        height=plot_height,
        minimum=min_value,
        maximum=max_value,
    )
    underlying_path = _series_path_with_range(
        values=underlying_values,
        left=left,
        top=top,
        width=plot_width,
        height=plot_height,
        minimum=min_value,
        maximum=max_value,
    )
    exposure_bands = _presentation_exposure_bands(
        result=result,
        left=left,
        top=top,
        width=plot_width,
        height=plot_height,
    )
    state_labels = _presentation_state_labels(
        result=result,
        left=left,
        top=top,
        width=plot_width,
    )
    sharpe = (
        "无"
        if result.summary.sharpe_ratio is None
        else f"{result.summary.sharpe_ratio:.2f}"
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        'viewBox="0 0 1180 620" role="img" aria-labelledby="title desc">'
        f'<title>{_escape_xml(title)}</title>'
        '<desc>中文汇报图：比较策略净值、标的净值和持仓状态。</desc>'
        '<rect width="100%" height="100%" fill="#fffaf0"/>'
        f'<text x="{left}" y="42" font-family="Microsoft YaHei, SimHei, Arial" '
        f'font-size="26" font-weight="700" fill="#111827">{_escape_xml(title)}</text>'
        f'<text x="{left}" y="74" font-family="Microsoft YaHei, SimHei, Arial" '
        'font-size="14" fill="#4b5563">'
        f'窗口：{result.summary.window_start.date().isoformat()} 至 '
        f'{result.summary.window_end.date().isoformat()} | 标的：{_escape_xml(market_symbol)} | '
        f'{result.summary.bar_count} 根K线</text>'
        f'{_metric_card(86, 96, "策略收益", result.summary.total_strategy_return, "#0f766e")}'
        f'{_metric_card(318, 96, "标的涨跌", result.summary.total_underlying_return, "#2563eb")}'
        f'{_metric_card(550, 96, "最大回撤", -result.summary.max_drawdown, "#b91c1c")}'
        f'<text x="782" y="120" font-family="Microsoft YaHei, SimHei, Arial" '
        'font-size="15" fill="#374151">Sharpe：'
        f'<tspan font-weight="700">{sharpe}</tspan></text>'
        f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" '
        'rx="8" fill="#fffdf7" stroke="#d1d5db" stroke-width="1"/>'
        f'{exposure_bands}'
        f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" '
        f'y2="{top + plot_height}" stroke="#9ca3af" stroke-width="1"/>'
        f'<path d="{underlying_path}" fill="none" stroke="#2563eb" stroke-width="3"/>'
        f'<path d="{strategy_path}" fill="none" stroke="#0f766e" stroke-width="3.5"/>'
        f'{state_labels}'
        f'<text x="{left}" y="{height - 92}" font-family="Microsoft YaHei, SimHei, Arial" '
        'font-size="14" fill="#111827">怎么读这张图：</text>'
        f'<text x="{left}" y="{height - 66}" font-family="Microsoft YaHei, SimHei, Arial" '
        'font-size="13" fill="#374151">绿色线 = 系统策略净值；蓝色线 = 单纯持有 '
        f'{_escape_xml(market_symbol)}；背景绿色深浅 = 多头仓位大小；灰色 = 空仓。</text>'
        f'<text x="{left}" y="{height - 40}" font-family="Microsoft YaHei, SimHei, Arial" '
        'font-size="13" fill="#374151">这是一份 legacy validation：'
        '它验证旧 replay 已产生的持仓决策，'
        '不代表 agent 已按新代码重新思考。</text>'
        f'<rect x="{width - 338}" y="{height - 82}" width="18" height="4" fill="#0f766e"/>'
        f'<text x="{width - 314}" y="{height - 76}" font-family="Microsoft YaHei, SimHei, Arial" '
        'font-size="12" fill="#374151">策略净值</text>'
        f'<rect x="{width - 236}" y="{height - 82}" width="18" height="4" fill="#2563eb"/>'
        f'<text x="{width - 212}" y="{height - 76}" font-family="Microsoft YaHei, SimHei, Arial" '
        f'font-size="12" fill="#374151">{_escape_xml(market_symbol)} 净值</text>'
        '</svg>'
    )


def _select_window_bars(
    *,
    market_data: MarketDataSeries,
    window_start: datetime,
    window_end: datetime,
) -> tuple[MarketDataBar, ...]:
    bars = tuple(
        bar
        for bar in market_data.bars
        if bar.start_at <= window_end and bar.end_at > window_start
    )
    if not bars:
        raise ValidationWindowPerformanceError(
            "No market-data bars overlap the requested validation window."
        )
    return bars


def _resolve_bar_states(
    *,
    bars: tuple[MarketDataBar, ...],
    state_changes: tuple[ViewStateChange, ...],
) -> tuple[tuple[ViewState, float], ...]:
    state_change_index = 0
    active_state: ViewState = "flat"
    active_weight = 0.0
    states: list[tuple[ViewState, float]] = []
    for bar in bars:
        while (
            state_change_index < len(state_changes)
            and state_changes[state_change_index].effective_at <= bar.start_at
        ):
            current = state_changes[state_change_index]
            active_state = current.state
            active_weight = current.target_weight
            state_change_index += 1
        states.append((active_state, active_weight))
    return tuple(states)


def _presentation_exposure_bands(
    *,
    result: ValidationWindowPerformanceResult,
    left: int,
    top: int,
    width: int,
    height: int,
) -> str:
    if not result.bar_returns:
        return ""
    slot_width = width / len(result.bar_returns)
    bands: list[str] = []
    for index, item in enumerate(result.bar_returns):
        color = _state_color(item.state)
        opacity = 0.04 + min(abs(item.target_weight), 1.0) * 0.12
        x = left + slot_width * index
        bands.append(
            f'<rect x="{x:.2f}" y="{top}" width="{slot_width:.2f}" '
            f'height="{height}" fill="{color}" opacity="{opacity:.3f}"/>'
        )
    return "".join(bands)


def _presentation_state_labels(
    *,
    result: ValidationWindowPerformanceResult,
    left: int,
    top: int,
    width: int,
) -> str:
    if not result.bar_returns:
        return ""
    labels: list[str] = []
    previous_state: ViewState | None = None
    last_label_x = -999.0
    for index, item in enumerate(result.bar_returns):
        if item.state == previous_state:
            continue
        x = left + width * index / max(len(result.bar_returns) - 1, 1)
        previous_state = item.state
        if x - last_label_x < 120.0:
            continue
        last_label_x = x
        labels.append(
            f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{top + 320}" '
            'stroke="#6b7280" stroke-width="1" stroke-dasharray="4 5"/>'
            f'<text x="{x + 6:.2f}" y="{top + 20}" '
            'font-family="Microsoft YaHei, SimHei, Arial" font-size="12" '
            f'fill="#374151">{_escape_xml(_state_label_zh(item.state))}</text>'
        )
    return "".join(labels)


def _state_label_zh(state: ViewState) -> str:
    return {
        "flat": "空仓",
        "weak_long": "弱多",
        "strong_long": "强多",
        "weak_short": "弱空",
        "strong_short": "强空",
    }[state]


def _state_color(state: ViewState) -> str:
    if state in {"weak_long", "strong_long"}:
        return "#0f766e"
    if state in {"weak_short", "strong_short"}:
        return "#b91c1c"
    return "#9ca3af"


def _metric_card(x: int, y: int, label: str, value: float, color: str) -> str:
    return (
        f'<rect x="{x}" y="{y}" width="196" height="46" rx="10" fill="#ffffff" '
        'stroke="#e5e7eb" stroke-width="1"/>'
        f'<text x="{x + 14}" y="{y + 19}" font-family="Microsoft YaHei, SimHei, Arial" '
        f'font-size="12" fill="#6b7280">{_escape_xml(label)}</text>'
        f'<text x="{x + 14}" y="{y + 38}" font-family="Microsoft YaHei, SimHei, Arial" '
        f'font-size="18" font-weight="700" fill="{color}">{value * 100.0:.2f}%</text>'
    )


def _calculate_volatility(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return stdev(values)


def _calculate_sharpe(values: list[float], *, annual_periods: float) -> float | None:
    if len(values) < 2:
        return None
    volatility = stdev(values)
    if volatility == 0.0:
        return None
    return sqrt(annual_periods) * (mean(values) / volatility)


def _calculate_max_drawdown(equity_values: tuple[float, ...]) -> float:
    peak = 1.0
    max_drawdown = 0.0
    for equity in equity_values:
        if equity > peak:
            peak = equity
        drawdown = 1.0 - (equity / peak)
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    return max_drawdown


def _series_path(
    *,
    values: list[float],
    left: int,
    top: int,
    width: int,
    height: int,
) -> str:
    if len(values) == 1:
        x = left + width / 2.0
        y = _scale_value(values[0], min(values), max(values), top, height)
        return f"M {x:.2f} {y:.2f}"
    min_value = min(values)
    max_value = max(values)
    if min_value == max_value:
        min_value -= 1.0
        max_value += 1.0
    points: list[str] = []
    for index, value in enumerate(values):
        x = left + (width * index / (len(values) - 1))
        y = _scale_value(value, min_value, max_value, top, height)
        command = "M" if index == 0 else "L"
        points.append(f"{command} {x:.2f} {y:.2f}")
    return " ".join(points)


def _series_path_with_range(
    *,
    values: list[float],
    left: int,
    top: int,
    width: int,
    height: int,
    minimum: float,
    maximum: float,
) -> str:
    if len(values) == 1:
        x = left + width / 2.0
        y = _scale_value(values[0], minimum, maximum, top, height)
        return f"M {x:.2f} {y:.2f}"
    points: list[str] = []
    for index, value in enumerate(values):
        x = left + (width * index / (len(values) - 1))
        y = _scale_value(value, minimum, maximum, top, height)
        command = "M" if index == 0 else "L"
        points.append(f"{command} {x:.2f} {y:.2f}")
    return " ".join(points)


def _scale_value(
    value: float,
    minimum: float,
    maximum: float,
    top: int,
    height: int,
) -> float:
    ratio = (value - minimum) / (maximum - minimum)
    return top + height - ratio * height


def _escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


__all__ = [
    "ValidationWindowBarReturn",
    "ValidationWindowPerformanceError",
    "ValidationWindowPerformanceResult",
    "ValidationWindowPerformanceSummary",
    "calculate_window_performance",
    "render_window_market_kline_svg",
    "render_window_presentation_zh_svg",
    "render_window_performance_svg",
]
