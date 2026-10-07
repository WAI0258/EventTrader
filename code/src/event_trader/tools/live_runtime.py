"""Operator-facing live runtime entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from event_trader.artifact_retention import ArtifactRetentionError, prune_configured_artifacts
from event_trader.composition import compose_kernel
from event_trader.composition_error import CompositionError
from event_trader.config import BootstrapConfigError, KernelConfig, load_kernel_config
from event_trader.integrations.analysis_structured_provider import (
    AnalysisStructuredProviderError,
    capability_result_to_jsonable,
    run_analysis_structured_finalizer_preflight,
)
from event_trader.kernel import KernelLifecycleError, KernelRuntimeError
from event_trader.live_market_data_runtime import (
    LiveMarketDataRuntimeError,
    prepare_live_market_data,
)
from event_trader.live_runtime import (
    LiveRuntimeError,
    build_live_market_news_drivers,
    build_live_market_news_websocket_drivers,
    build_live_web_search_drivers,
    run_live_runtime_loop,
)
from event_trader.market.shared_store import shared_market_data_root
from event_trader.migrations import (
    prepare_pm_review_workspace,
    run_active_price_basis_cutover_from_config,
)
from event_trader.pm_review.runtime import validate_pm_review_runtime_preflight
from event_trader.storage import WorkspaceLayout
from event_trader.workspace import WorkspaceBootstrapError, bootstrap_workspace


def build_parser() -> argparse.ArgumentParser:
    """Build the live runtime parser."""
    parser = argparse.ArgumentParser(
        prog="python -m event_trader.tools.live_runtime",
        description=(
            "Run the forward-only live kernel and configured live source drivers."
        ),
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Load, validate, and run the committed live TOML kernel config.",
    )
    parser.add_argument(
        "--pm-review-preflight-only",
        action="store_true",
        help="Validate PMReview runtime readiness and exit without live runtime.",
    )
    parser.add_argument(
        "--pm-review-preflight-target",
        action="append",
        default=[],
        help="Target key to validate for PMReview workspace readiness. Repeatable.",
    )
    parser.add_argument(
        "--structured-finalizer-preflight-only",
        action="store_true",
        help=(
            "Probe configured Analysis structured-finalizer provider capability and "
            "exit without live runtime."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the combined live runtime."""
    parser = build_parser()
    args = parser.parse_args(argv)
    # One shared startup anchor keeps live source scheduling strictly forward-only
    # from this process start, independent of any stale durable checkpoints.
    startup_at = datetime.now(UTC)

    market_data_provider = None
    composed = None
    try:
        config = load_kernel_config(args.config)
        if args.structured_finalizer_preflight_only:
            result = run_analysis_structured_finalizer_preflight(config)
            print(json.dumps(capability_result_to_jsonable(result), sort_keys=True))
            return 0
        prepared_workspace = bootstrap_workspace(config.workspace_root)
        if prepared_workspace.layout is None:
            raise LiveRuntimeError("live runtime requires a workspace layout.")
        pm_review_target_keys = tuple(args.pm_review_preflight_target) or (
            _live_target_keys(config)
        )
        if args.pm_review_preflight_only:
            preflight_receipt = validate_pm_review_runtime_preflight(
                config=config,
                layout=prepared_workspace.layout,
                target_keys=pm_review_target_keys,
            )
            print(
                "pm review preflight: "
                f"enabled={preflight_receipt.enabled} "
                f"execute_approved={preflight_receipt.execute_approved} "
                f"workspace_ready_checked={preflight_receipt.workspace_ready_checked} "
                f"runner_checked={preflight_receipt.runner_checked} "
                f"targets={','.join(preflight_receipt.target_keys)}"
            )
            return 0
        _ensure_pm_review_workspace_ready(
            config=config,
            layout=prepared_workspace.layout,
            target_keys=pm_review_target_keys,
            migrated_at=startup_at,
            emit=print,
        )
        _run_startup_active_price_basis_cutovers(
            config=config,
            layout=prepared_workspace.layout,
            startup_at=startup_at,
            emit=print,
        )
        market_data_provider, warmup_receipt = prepare_live_market_data(
            config=config,
            layout=prepared_workspace.layout,
            as_of_at=startup_at,
            emit=print,
        )
        if warmup_receipt.enabled:
            print(
                "live market-data warmup receipt: "
                f"targets={warmup_receipt.target_count} "
                f"symbols={warmup_receipt.symbol_count} "
                f"bars={warmup_receipt.bar_count}"
            )
        composed = compose_kernel(
            args.config,
            emit=print,
            market_data_provider_override=market_data_provider,
            market_data_store_root=(
                shared_market_data_root(config)
                if market_data_provider is not None
                else None
            ),
            pm_review_preflight_target_keys=pm_review_target_keys,
        )
        workspace_layout = composed.workspace.layout
        if workspace_layout is None:
            raise LiveRuntimeError("live runtime requires a workspace layout.")
        retention_receipt = prune_configured_artifacts(
            config=composed.config,
            layout=workspace_layout,
            now=datetime.now(UTC),
        )
        if retention_receipt.enabled:
            print(
                "artifact retention: "
                f"pruned_files={retention_receipt.pruned_files} "
                f"roots={len(retention_receipt.root_receipts)}"
            )
        live_host = composed.require_live_host()
        web_search_drivers = build_live_web_search_drivers(
            composed,
            startup_at=startup_at,
        )
        market_news_drivers = build_live_market_news_drivers(
            composed,
            startup_at=startup_at,
        )
        market_news_websocket_drivers = build_live_market_news_websocket_drivers(
            composed,
            emit=print,
        )
        runtime_receipt = run_live_runtime_loop(
            live_host=live_host,
            web_search_drivers=web_search_drivers,
            market_news_drivers=market_news_drivers,
            market_news_websocket_drivers=market_news_websocket_drivers,
            checkpoint_bookkeeping_layout=workspace_layout,
            emit=print,
        )
    except KeyboardInterrupt:
        print("Live runtime interrupted by operator.", file=sys.stderr)
        return 130
    except (
        BootstrapConfigError,
        WorkspaceBootstrapError,
        CompositionError,
        ArtifactRetentionError,
        LiveMarketDataRuntimeError,
        LiveRuntimeError,
        AnalysisStructuredProviderError,
    ) as exc:
        print(f"Live runtime setup failed: {exc}", file=sys.stderr)
        return 1
    except KernelRuntimeError as exc:
        print(f"Live kernel failed during {exc.phase}: {exc}", file=sys.stderr)
        return 1
    except KernelLifecycleError as exc:
        print(f"Live kernel lifecycle failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if composed is not None:
            try:
                composed.close()
            except Exception as exc:  # pragma: no cover - cleanup guard
                print(f"Composed runtime close failed: {exc}", file=sys.stderr)
        if market_data_provider is not None:
            close = getattr(market_data_provider, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # pragma: no cover - cleanup guard
                    print(f"Live market-data provider close failed: {exc}", file=sys.stderr)

    print(
        "live runtime exit: clean "
        f"source_batches={runtime_receipt.source_batch_count} "
        f"heartbeats={runtime_receipt.heartbeat_count} "
        f"workspace_root={composed.workspace.root} "
        f"runtime_root={composed.workspace.runtime_root}"
    )
    return 0


def _live_target_keys(config) -> tuple[str, ...]:
    live_config = config.live
    if live_config is None:
        return ()
    target_keys: list[str] = []
    enabled_channels = set(live_config.enabled_channels)
    if "web_search" in enabled_channels and live_config.web_search is not None:
        target_keys.extend(target.target_key for target in live_config.web_search.targets)
    if "market_news" in enabled_channels and live_config.market_news is not None:
        target_keys.extend(target.target_key for target in live_config.market_news.targets)
    seen: set[str] = set()
    unique_target_keys: list[str] = []
    for target_key in target_keys:
        if target_key in seen:
            continue
        seen.add(target_key)
        unique_target_keys.append(target_key)
    return tuple(unique_target_keys)


def _run_startup_active_price_basis_cutovers(
    *,
    config,
    layout,
    startup_at: datetime,
    emit,
) -> None:
    for target_key in _live_target_keys(config):
        result = run_active_price_basis_cutover_from_config(
            layout=layout,
            config=config,
            target_key=target_key,
            cutover_at=startup_at,
        )
        if result.cutover_applied:
            emit(
                "active price basis cutover: "
                f"target_key={target_key} "
                f"successor_assessment_id={result.successor_assessment_id} "
                f"old_basis_id={result.old_basis_id} "
                f"new_basis_id={result.new_basis_id}"
        )


def _ensure_pm_review_workspace_ready(
    *,
    config: KernelConfig,
    layout: WorkspaceLayout,
    target_keys: tuple[str, ...],
    migrated_at: datetime,
    emit: Callable[[str], None],
) -> None:
    try:
        validate_pm_review_runtime_preflight(
            config=config,
            layout=layout,
            target_keys=target_keys,
        )
        return
    except CompositionError as exc:
        if not _is_fresh_pm_review_workspace_error(exc):
            raise
    prepare_pm_review_workspace(
        layout=layout,
        target_keys=target_keys,
        migrated_at=migrated_at,
    )
    emit("pm review workspace auto-prepared: targets=" + ",".join(target_keys))
    validate_pm_review_runtime_preflight(
        config=config,
        layout=layout,
        target_keys=target_keys,
    )


def _is_fresh_pm_review_workspace_error(exc: CompositionError) -> bool:
    message = str(exc)
    return (
        "runtime schema marker is missing" in message
        or "cutover baseline portfolio state is missing" in message
    )


if __name__ == "__main__":
    raise SystemExit(main())
