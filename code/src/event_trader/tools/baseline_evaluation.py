"""Operator-facing replay baseline evaluation tool."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from event_trader.config import (
    BootstrapConfigError,
    ExecutionConfig,
    ValidationExecutionDirectionMode,
    load_execution_config_for_workspace,
    load_kernel_config,
    resolve_execution_buy_cost_bps_for_target,
    resolve_validation_execution_direction_mode,
)
from event_trader.contracts._validators import validate_target_key
from event_trader.contracts.view_state_change import MarketMapping
from event_trader.evaluation import (
    BaselineEvaluationError,
    DailyStrategyPoint,
    MetricSummary,
    RuleBaselineCatalogError,
    RuleBaselineParameters,
    RuleBaselineSeries,
    RuleBaselineSpec,
    available_rule_baseline_ids,
    baseline_parameter_payload_for_id,
    build_metric_report_rows,
    build_rule_baseline_series,
    calculate_metric_summary,
    calculate_replay_daily_performance,
    cumulative_return_series_from_points,
    daily_bars_from_intraday,
    render_cumulative_return_svg,
    render_metric_report_markdown,
)
from event_trader.execution import ExecutionRecord, ExecutionRecordStore
from event_trader.market.shared_store import (
    SharedMarketDataError,
    SharedMarketDataProvider,
    SharedMarketDataStore,
    read_workspace_snapshot_ref,
    shared_market_data_root,
)
from event_trader.storage import build_workspace_layout
from event_trader.validation import read_state_changes


class BaselineEvaluationCliError(ValueError):
    """Raised when replay baseline evaluation inputs are incomplete or invalid."""


_LEGACY_BASELINE_PARAMETER_ARG_NAMES: tuple[str, ...] = (
    "sma_fast_periods",
    "sma_slow_periods",
    "ema_fast_periods",
    "ema_slow_periods",
    "macd_fast_periods",
    "macd_slow_periods",
    "macd_signal_periods",
    "rsi_periods",
    "rsi_oversold",
    "rsi_overbought",
    "bollinger_periods",
    "bollinger_stddev",
)


@dataclass(frozen=True, slots=True)
class BaselineMetricsRow:
    spec_id: str
    baseline_id: str
    label: str
    color: str
    point_count: int
    nonzero_exposure: bool
    parameters: RuleBaselineParameters
    metrics: MetricSummary

    def to_json_payload(self) -> dict[str, object]:
        return {
            "spec_id": self.spec_id,
            "baseline_id": self.baseline_id,
            "label": self.label,
            "color": self.color,
            "point_count": self.point_count,
            "nonzero_exposure": self.nonzero_exposure,
            "parameters": baseline_parameter_payload_for_id(
                self.baseline_id,
                self.parameters,
            ),
            "metrics": _metric_summary_payload(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class BaselineEvaluationReport:
    target_key: str
    market_symbol: str
    bar_granularity: str
    market_data_snapshot_id: str
    start_date: date
    end_date: date
    day_count: int
    execution_direction_mode: ValidationExecutionDirectionMode
    annual_periods: float
    selected_spec_ids: tuple[str, ...]
    selected_baseline_ids: tuple[str, ...]
    replay_metrics: MetricSummary
    baseline_rows: tuple[BaselineMetricsRow, ...]

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "market_symbol": self.market_symbol,
            "bar_granularity": self.bar_granularity,
            "market_data_snapshot_id": self.market_data_snapshot_id,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "day_count": self.day_count,
            "execution_direction_mode": self.execution_direction_mode,
            "annual_periods": self.annual_periods,
            "selected_spec_ids": list(self.selected_spec_ids),
            "selected_baseline_ids": list(self.selected_baseline_ids),
            "replay_metrics": _metric_summary_payload(self.replay_metrics),
            "baselines": [row.to_json_payload() for row in self.baseline_rows],
        }


@dataclass(frozen=True, slots=True)
class PersistedBaselineEvaluation:
    report: BaselineEvaluationReport
    svg_path: Path
    markdown_path: Path
    json_path: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m event_trader.tools.baseline_evaluation",
        description=(
            "Evaluate replay execution against rule-based technical baselines and "
            "write comparison artifacts."
        ),
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        required=True,
        help="Replay workspace root, for example .local/replay-cn-...-workspace.",
    )
    parser.add_argument("--target-key", required=True, help="Target key, for example cn_159516.")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help=(
            "Committed kernel config that defines execution costs and "
            "execution-direction policy for the target."
        ),
    )
    parser.add_argument(
        "--market-symbol",
        default=None,
        help="Market bar symbol. Defaults to the first execution record provenance symbol.",
    )
    parser.add_argument("--bar-granularity", default="1h", help="Market bar granularity suffix.")
    parser.add_argument(
        "--market-data-snapshot-id",
        default=None,
        help=(
            "Pinned shared market-data snapshot id. Defaults to the workspace snapshot "
            "reference; evaluation never scans workspace market-data files."
        ),
    )
    parser.add_argument(
        "--baseline",
        action="append",
        choices=available_rule_baseline_ids(),
        default=None,
        help="Repeat to restrict evaluation to selected baselines. Defaults to all.",
    )
    parser.add_argument(
        "--baseline-spec",
        action="append",
        default=None,
        help=(
            "Repeat to compare multiple baseline specs explicitly, for example "
            "ema_crossover:10,30 or macd:12,26,9."
        ),
    )
    parser.add_argument("--sma-fast-periods", type=int, default=50)
    parser.add_argument("--sma-slow-periods", type=int, default=200)
    parser.add_argument("--ema-fast-periods", type=int, default=20)
    parser.add_argument("--ema-slow-periods", type=int, default=50)
    parser.add_argument("--macd-fast-periods", type=int, default=12)
    parser.add_argument("--macd-slow-periods", type=int, default=26)
    parser.add_argument("--macd-signal-periods", type=int, default=9)
    parser.add_argument("--rsi-periods", type=int, default=14)
    parser.add_argument("--rsi-oversold", type=float, default=30.0)
    parser.add_argument("--rsi-overbought", type=float, default=70.0)
    parser.add_argument("--bollinger-periods", type=int, default=20)
    parser.add_argument("--bollinger-stddev", type=float, default=2.0)
    parser.add_argument(
        "--svg-output",
        type=Path,
        default=None,
        help=(
            "Output SVG path. Defaults to "
            "runtime/evaluation/baselines/<target>/cumulative_returns.svg."
        ),
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=None,
        help=(
            "Output Markdown metrics path. Defaults to "
            "runtime/evaluation/baselines/<target>/metrics.md."
        ),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help=(
            "Output JSON summary path. Defaults to "
            "runtime/evaluation/baselines/<target>/summary.json."
        ),
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Chart title. Defaults to '<TARGET> Baseline Evaluation'.",
    )
    parser.add_argument(
        "--annual-periods",
        type=float,
        default=252.0,
        help="Annualization periods for daily metric calculations.",
    )
    parser.add_argument("--width", type=int, default=540, help="SVG width.")
    parser.add_argument("--height", type=int, default=250, help="SVG height.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        baseline_specs = _resolve_cli_baseline_specs(parser, args)
        legacy_baseline_parameters = (
            None
            if baseline_specs is not None
            else RuleBaselineParameters(
                sma_fast_periods=args.sma_fast_periods,
                sma_slow_periods=args.sma_slow_periods,
                ema_fast_periods=args.ema_fast_periods,
                ema_slow_periods=args.ema_slow_periods,
                macd_fast_periods=args.macd_fast_periods,
                macd_slow_periods=args.macd_slow_periods,
                macd_signal_periods=args.macd_signal_periods,
                rsi_periods=args.rsi_periods,
                rsi_oversold=args.rsi_oversold,
                rsi_overbought=args.rsi_overbought,
                bollinger_periods=args.bollinger_periods,
                bollinger_stddev=args.bollinger_stddev,
            )
        )
        persisted = run_baseline_evaluation(
            workspace_root=args.workspace_root,
            target_key=args.target_key,
            config_path=args.config,
            market_symbol=args.market_symbol,
            bar_granularity=args.bar_granularity,
            market_data_snapshot_id=args.market_data_snapshot_id,
            baseline_specs=baseline_specs,
            baseline_ids=args.baseline,
            baseline_parameters=legacy_baseline_parameters,
            svg_output_path=args.svg_output,
            markdown_output_path=args.markdown_output,
            json_output_path=args.json_output,
            title=args.title,
            annual_periods=args.annual_periods,
            width=args.width,
            height=args.height,
        )
    except (
        BaselineEvaluationCliError,
        BaselineEvaluationError,
        RuleBaselineCatalogError,
    ) as exc:
        raise SystemExit(f"error: {exc}") from exc
    print("baseline evaluation:")
    print(f"- target={persisted.report.target_key}")
    print(f"- symbol={persisted.report.market_symbol}")
    print(f"- market_data_snapshot_id={persisted.report.market_data_snapshot_id}")
    print(f"- specs={','.join(persisted.report.selected_spec_ids)}")
    print(f"- svg={persisted.svg_path.as_posix()}")
    print(f"- markdown={persisted.markdown_path.as_posix()}")
    print(f"- json={persisted.json_path.as_posix()}")
    return 0


def run_baseline_evaluation(
    *,
    workspace_root: Path,
    target_key: str,
    config_path: Path,
    market_symbol: str | None = None,
    bar_granularity: str = "1h",
    market_data_snapshot_id: str | None = None,
    baseline_specs: Sequence[RuleBaselineSpec] | None = None,
    baseline_ids: Sequence[str] | None = None,
    baseline_parameters: RuleBaselineParameters | None = None,
    svg_output_path: Path | None = None,
    markdown_output_path: Path | None = None,
    json_output_path: Path | None = None,
    title: str | None = None,
    annual_periods: float = 252.0,
    width: int = 540,
    height: int = 250,
) -> PersistedBaselineEvaluation:
    normalized_target = validate_target_key(target_key, error_type=BaselineEvaluationCliError)
    layout = build_workspace_layout(workspace_root)
    explicit_execution_config = _load_explicit_execution_config(
        config_path=config_path,
        workspace_root=layout.root,
    )
    state_changes = read_state_changes(layout, normalized_target)
    execution_records = tuple(
        item.record
        for item in ExecutionRecordStore(layout).read_records(target_key=normalized_target)
    )
    if not state_changes:
        raise BaselineEvaluationCliError(
            f"no state changes found for target {normalized_target!r}."
        )
    if not execution_records:
        raise BaselineEvaluationCliError(
            f"no execution records found for target {normalized_target!r}."
        )

    resolved_symbol = market_symbol or _infer_market_symbol(execution_records)
    resolved_snapshot_id = market_data_snapshot_id or read_workspace_snapshot_ref(
        layout.root,
        target_key=normalized_target,
    )
    if resolved_snapshot_id is None:
        raise BaselineEvaluationCliError(
            "a pinned market-data snapshot is required; provide "
            "--market-data-snapshot-id or write a workspace snapshot reference."
        )
    try:
        kernel_config = load_kernel_config(config_path)
        market_data_root = shared_market_data_root(kernel_config)
        mapping = _resolve_baseline_market_mapping(
            kernel_config=kernel_config,
            target_key=normalized_target,
            market_symbol=resolved_symbol,
            bar_granularity=bar_granularity,
        )
        market_data = SharedMarketDataProvider(
            store=SharedMarketDataStore(market_data_root),
            provider_name=kernel_config.validation.market_data.provider
            if kernel_config.validation is not None
            else "unknown",
            snapshot_id=resolved_snapshot_id,
        ).read_series(
            mapping,
            start_at=datetime.min.replace(tzinfo=UTC),
            end_at=datetime.now(UTC),
        )
    except (BootstrapConfigError, SharedMarketDataError, ValueError) as exc:
        raise BaselineEvaluationCliError(
            f"shared market-data snapshot {resolved_snapshot_id!r} could not be read: {exc}"
        ) from exc
    adjustment_sidecar = None
    daily_bars = daily_bars_from_intraday(market_data)
    replay_points = calculate_replay_daily_performance(
        state_changes=state_changes,
        execution_records=execution_records,
        market_data=market_data,
        adjustment_sidecar=adjustment_sidecar,
    )
    start_date = replay_points[0].date
    end_date = replay_points[-1].date
    execution_direction_mode = _resolve_target_execution_direction_mode(
        target_key=normalized_target,
        config_path=config_path,
    )
    resolved_baseline_parameters = baseline_parameters or RuleBaselineParameters()
    baseline_series = build_rule_baseline_series(
        daily_bars,
        start_date=start_date,
        direction_mode=execution_direction_mode,
        specs=baseline_specs,
        parameters=None if baseline_specs is not None else resolved_baseline_parameters,
        buy_hold_entry_cost_bps=_resolve_buy_hold_entry_cost_bps(
            target_key=normalized_target,
            explicit_execution_config=explicit_execution_config,
            execution_records=execution_records,
        ),
        baseline_ids=baseline_ids,
    )
    selected_spec_ids = tuple(item.spec_id for item in baseline_series)
    selected_baseline_ids = tuple(item.baseline_id for item in baseline_series)
    replay_metrics = calculate_metric_summary(
        tuple(point.strategy_return for point in replay_points),
        annual_periods=annual_periods,
    )
    baseline_rows = tuple(
        _baseline_metrics_row(item, annual_periods=annual_periods) for item in baseline_series
    )
    report = BaselineEvaluationReport(
        target_key=normalized_target,
        market_symbol=resolved_symbol,
        bar_granularity=bar_granularity,
        market_data_snapshot_id=resolved_snapshot_id,
        start_date=start_date,
        end_date=end_date,
        day_count=len(replay_points),
        execution_direction_mode=execution_direction_mode,
        annual_periods=annual_periods,
        selected_spec_ids=selected_spec_ids,
        selected_baseline_ids=selected_baseline_ids,
        replay_metrics=replay_metrics,
        baseline_rows=baseline_rows,
    )
    output_root = layout.runtime_root / "evaluation" / "baselines" / normalized_target
    svg_path = svg_output_path or (output_root / "cumulative_returns.svg")
    markdown_path = markdown_output_path or (output_root / "metrics.md")
    json_path = json_output_path or (output_root / "summary.json")
    _write_outputs(
        report=report,
        replay_points=replay_points,
        baseline_series=baseline_series,
        svg_path=svg_path,
        markdown_path=markdown_path,
        json_path=json_path,
        title=title or f"{normalized_target.upper()} Baseline Evaluation",
        width=width,
        height=height,
    )
    return PersistedBaselineEvaluation(
        report=report,
        svg_path=svg_path.resolve(strict=False),
        markdown_path=markdown_path.resolve(strict=False),
        json_path=json_path.resolve(strict=False),
    )


def _baseline_metrics_row(
    series: RuleBaselineSeries,
    *,
    annual_periods: float,
) -> BaselineMetricsRow:
    return BaselineMetricsRow(
        spec_id=series.spec_id,
        baseline_id=series.baseline_id,
        label=series.label,
        color=series.color,
        point_count=len(series.points),
        nonzero_exposure=_has_nonzero_exposure(series.points),
        parameters=series.parameters,
        metrics=calculate_metric_summary(
            tuple(point.strategy_return for point in series.points),
            annual_periods=annual_periods,
        ),
    )


def _write_outputs(
    *,
    report: BaselineEvaluationReport,
    replay_points: tuple[DailyStrategyPoint, ...],
    baseline_series: tuple[RuleBaselineSeries, ...],
    svg_path: Path,
    markdown_path: Path,
    json_path: Path,
    title: str,
    width: int,
    height: int,
) -> None:
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    chart_series = (
        cumulative_return_series_from_points(
            "Event Trader",
            replay_points,
            color="#ff4b4b",
        ),
        *tuple(
            cumulative_return_series_from_points(
                item.label,
                item.points,
                color=item.color,
            )
            for item in baseline_series
        ),
    )
    svg_path.write_text(
        render_cumulative_return_svg(
            chart_series,
            title=title,
            width=width,
            height=height,
        ),
        encoding="utf-8",
    )
    rows = build_metric_report_rows(
        (
            ("Event Trader", replay_points),
            *tuple((item.label, item.points) for item in baseline_series),
        ),
        annual_periods=report.annual_periods,
    )
    markdown_path.write_text(
        render_metric_report_markdown(
            rows,
            title=f"{report.target_key.upper()} Baseline Evaluation Metrics",
        ),
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(report.to_json_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _infer_market_symbol(execution_records: Sequence[ExecutionRecord]) -> str:
    for record in execution_records:
        provenance = getattr(record, "provenance", None)
        market_symbol = getattr(provenance, "market_symbol", None)
        if isinstance(market_symbol, str) and market_symbol.strip():
            return market_symbol.strip()
    raise BaselineEvaluationCliError(
        "could not infer market symbol from execution records; pass --market-symbol."
    )


def _resolve_cli_baseline_specs(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> tuple[RuleBaselineSpec, ...] | None:
    raw_specs = args.baseline_spec
    if raw_specs is None:
        return None
    if args.baseline is not None:
        raise BaselineEvaluationCliError(
            "--baseline-spec cannot be combined with legacy --baseline selection."
        )
    non_default_legacy_args = [
        arg_name
        for arg_name in _LEGACY_BASELINE_PARAMETER_ARG_NAMES
        if getattr(args, arg_name) != parser.get_default(arg_name)
    ]
    if non_default_legacy_args:
        raise BaselineEvaluationCliError(
            "--baseline-spec cannot be combined with legacy parameter flags: "
            + ", ".join(f"--{arg_name.replace('_', '-')}" for arg_name in non_default_legacy_args)
        )
    return tuple(_parse_baseline_spec(raw_value) for raw_value in raw_specs)


def _parse_baseline_spec(raw_value: object) -> RuleBaselineSpec:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise BaselineEvaluationCliError("baseline specs must be non-blank strings.")
    normalized = raw_value.strip()
    baseline_id, _, raw_parameters = normalized.partition(":")
    available = set(available_rule_baseline_ids())
    if baseline_id not in available:
        raise BaselineEvaluationCliError(f"unknown baseline id in spec: {baseline_id!r}")
    if baseline_id == "buy_and_hold":
        if raw_parameters:
            raise BaselineEvaluationCliError("buy_and_hold does not accept spec parameters.")
        return RuleBaselineSpec(
            spec_id="buy_and_hold",
            baseline_id="buy_and_hold",
            parameters=RuleBaselineParameters(),
        )
    if not raw_parameters:
        raise BaselineEvaluationCliError(
            f"baseline spec {baseline_id!r} requires explicit parameters."
        )
    tokens = tuple(part.strip() for part in raw_parameters.split(","))
    if any(not token for token in tokens):
        raise BaselineEvaluationCliError(f"baseline spec {normalized!r} contains blank parameters.")
    parameters = _parameters_from_spec_tokens(baseline_id, tokens, raw_spec=normalized)
    return RuleBaselineSpec(
        spec_id=normalized,
        baseline_id=baseline_id,
        parameters=parameters,
    )


def _parameters_from_spec_tokens(
    baseline_id: str,
    tokens: tuple[str, ...],
    *,
    raw_spec: str,
) -> RuleBaselineParameters:
    if baseline_id == "sma_crossover":
        if len(tokens) != 2:
            raise BaselineEvaluationCliError(f"{raw_spec!r} requires two integer parameters.")
        return RuleBaselineParameters(
            sma_fast_periods=_parse_spec_int(tokens[0], raw_spec=raw_spec),
            sma_slow_periods=_parse_spec_int(tokens[1], raw_spec=raw_spec),
        )
    if baseline_id == "ema_crossover":
        if len(tokens) != 2:
            raise BaselineEvaluationCliError(f"{raw_spec!r} requires two integer parameters.")
        return RuleBaselineParameters(
            ema_fast_periods=_parse_spec_int(tokens[0], raw_spec=raw_spec),
            ema_slow_periods=_parse_spec_int(tokens[1], raw_spec=raw_spec),
        )
    if baseline_id == "macd":
        if len(tokens) != 3:
            raise BaselineEvaluationCliError(f"{raw_spec!r} requires three integer parameters.")
        return RuleBaselineParameters(
            macd_fast_periods=_parse_spec_int(tokens[0], raw_spec=raw_spec),
            macd_slow_periods=_parse_spec_int(tokens[1], raw_spec=raw_spec),
            macd_signal_periods=_parse_spec_int(tokens[2], raw_spec=raw_spec),
        )
    if baseline_id == "rsi":
        if len(tokens) != 3:
            raise BaselineEvaluationCliError(
                f"{raw_spec!r} requires periods, oversold, overbought."
            )
        return RuleBaselineParameters(
            rsi_periods=_parse_spec_int(tokens[0], raw_spec=raw_spec),
            rsi_oversold=_parse_spec_float(tokens[1], raw_spec=raw_spec),
            rsi_overbought=_parse_spec_float(tokens[2], raw_spec=raw_spec),
        )
    if baseline_id == "bollinger":
        if len(tokens) != 2:
            raise BaselineEvaluationCliError(f"{raw_spec!r} requires periods and stddev.")
        return RuleBaselineParameters(
            bollinger_periods=_parse_spec_int(tokens[0], raw_spec=raw_spec),
            bollinger_stddev=_parse_spec_float(tokens[1], raw_spec=raw_spec),
        )
    raise BaselineEvaluationCliError(f"unsupported baseline spec family: {baseline_id!r}")


def _parse_spec_int(value: str, *, raw_spec: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise BaselineEvaluationCliError(
            f"baseline spec {raw_spec!r} requires integer parameters."
        ) from exc


def _parse_spec_float(value: str, *, raw_spec: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise BaselineEvaluationCliError(
            f"baseline spec {raw_spec!r} requires numeric parameters."
        ) from exc


def _load_explicit_execution_config(
    *,
    config_path: Path,
    workspace_root: Path,
) -> ExecutionConfig:
    try:
        return load_execution_config_for_workspace(
            config_path,
            workspace_root=workspace_root,
        )
    except BootstrapConfigError as exc:
        raise BaselineEvaluationCliError(str(exc)) from exc


def _resolve_buy_hold_entry_cost_bps(
    *,
    target_key: str,
    explicit_execution_config: ExecutionConfig,
    execution_records: Sequence[ExecutionRecord],
) -> float:
    try:
        return resolve_execution_buy_cost_bps_for_target(
            explicit_execution_config,
            target_key=target_key,
        )
    except BootstrapConfigError:
        executed_records = tuple(
            record for record in execution_records if record.status == "executed"
        )
        if not executed_records:
            return 0.0
        latest_record = max(
            executed_records,
            key=lambda record: (
                record.recorded_at,
                record.business_at,
                record.execution_record_id,
            ),
        )
        return latest_record.buy_cost_bps


def _resolve_target_execution_direction_mode(
    *,
    target_key: str,
    config_path: Path,
) -> ValidationExecutionDirectionMode:
    try:
        config = load_kernel_config(config_path)
        return resolve_validation_execution_direction_mode(
            config,
            target_key=target_key,
        )
    except BootstrapConfigError as exc:
        raise BaselineEvaluationCliError(str(exc)) from exc


def _resolve_baseline_market_mapping(
    *,
    kernel_config,
    target_key: str,
    market_symbol: str,
    bar_granularity: str,
) -> MarketMapping:
    """Resolve the exact shared-series identity used by baseline evaluation."""

    configured = None
    if kernel_config.validation is not None:
        configured = kernel_config.validation.market_mappings.get(target_key)
    if configured is not None:
        return MarketMapping(
            target_key=target_key,
            market_symbol=configured.market_symbol,
            market_session=configured.market_session,
            exchange=configured.exchange,
            bar_granularity=configured.bar_granularity,
            exchange_session_scope=configured.exchange_session_scope,
        )
    return MarketMapping(
        target_key=target_key,
        market_symbol=market_symbol,
        market_session="continuous",
        exchange=None,
        bar_granularity=bar_granularity,
        exchange_session_scope=None,
    )


def _has_nonzero_exposure(points: Sequence[DailyStrategyPoint]) -> bool:
    return any(point.target_weight != 0.0 for point in points)


def _metric_summary_payload(summary: MetricSummary) -> dict[str, object]:
    return {
        "cumulative_return": summary.cumulative_return,
        "annualized_return": summary.annualized_return,
        "volatility": summary.volatility,
        "sharpe": summary.sharpe,
        "max_drawdown": summary.max_drawdown,
        "calmar": summary.calmar,
        "annual_periods": summary.annual_periods,
        "period_count": summary.period_count,
    }


if __name__ == "__main__":
    raise SystemExit(main())
