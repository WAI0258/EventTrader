"""Command-line entry surface for event-trader."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from event_trader import __version__
from event_trader.composition import compose_kernel
from event_trader.composition_error import CompositionError
from event_trader.config import BootstrapConfigError, load_kernel_config
from event_trader.kernel import KernelLifecycleError, KernelRuntimeError
from event_trader.pm_review.runtime import validate_pm_review_runtime_preflight
from event_trader.workspace import WorkspaceBootstrapError, bootstrap_workspace

DEFAULT_STATUS_MESSAGE = "event-trader runtime ready"
KERNEL_EXIT_MESSAGE = "kernel exit: clean"


def build_parser() -> argparse.ArgumentParser:
    """Build the project-owned argument parser."""
    parser = argparse.ArgumentParser(
        prog="event-trader",
        description=(
            "Project-owned entry surface for the event-trader runtime."
        ),
    )
    parser.add_argument(
        "--config",
        help="Load, validate, and run the committed TOML kernel config.",
    )
    parser.add_argument(
        "--pm-review-preflight-only",
        action="store_true",
        help="Validate PMReview runtime readiness for the config and exit.",
    )
    parser.add_argument(
        "--pm-review-preflight-target",
        action="append",
        default=[],
        help="Target key to validate for PMReview workspace readiness. Repeatable.",
    )
    parser.add_argument(
        "--status-message",
        default=DEFAULT_STATUS_MESSAGE,
        help="Text emitted when the CLI runs without a config.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the committed CLI entry surface."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.config is None:
        print(args.status_message)
        return 0

    composed = None
    try:
        if args.pm_review_preflight_only:
            config = load_kernel_config(args.config)
            prepared_workspace = bootstrap_workspace(config.workspace_root)
            if prepared_workspace.layout is None:
                raise CompositionError("PMReview preflight requires a workspace layout.")
            receipt = validate_pm_review_runtime_preflight(
                config=config,
                layout=prepared_workspace.layout,
                target_keys=tuple(args.pm_review_preflight_target),
            )
            print(
                "pm review preflight: "
                f"enabled={receipt.enabled} "
                f"execute_approved={receipt.execute_approved} "
                f"workspace_ready_checked={receipt.workspace_ready_checked} "
                f"runner_checked={receipt.runner_checked} "
                f"targets={','.join(receipt.target_keys)}"
            )
            return 0
        composed = compose_kernel(args.config, emit=print)
        heartbeat_count = composed.require_live_host().run()
    except (BootstrapConfigError, WorkspaceBootstrapError, CompositionError) as exc:
        print(f"Runtime setup failed: {exc}", file=sys.stderr)
        return 1
    except KernelRuntimeError as exc:
        print(f"Kernel failed during {exc.phase}: {exc}", file=sys.stderr)
        return 1
    except KernelLifecycleError as exc:
        print(f"Kernel lifecycle failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if composed is not None:
            try:
                composed.close()
            except Exception as exc:  # pragma: no cover - cleanup guard
                print(f"Runtime cleanup failed: {exc}", file=sys.stderr)

    print(
        f"{KERNEL_EXIT_MESSAGE} heartbeats={heartbeat_count} "
        f"workspace_root={composed.workspace.root} runtime_root={composed.workspace.runtime_root}"
    )
    return 0
