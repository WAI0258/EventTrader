"""Operator-facing quarantine-and-repair command for archived source events."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from event_trader.config import BootstrapConfigError, load_kernel_config
from event_trader.operator.repair.source_event_quarantine import (
    SourceEventQuarantineRepairError,
    quarantine_and_repair_web_search_event,
)
from event_trader.tools.live_catchup import _release_replay_from_live_config
from event_trader.workspace import WorkspaceBootstrapError, bootstrap_workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m event_trader.tools.source_event_quarantine_repair",
        description=(
            "Quarantine one archived source event and rebuild affected live-catchup "
            "runtime state in place."
        ),
    )
    parser.add_argument("--config", required=True, help="Committed live kernel TOML config.")
    parser.add_argument("--target-key", required=True, help="Live target key to repair.")
    parser.add_argument("--event-id", required=True, help="Contaminated source event id.")
    parser.add_argument(
        "--source-ref",
        help="Optional source_ref guard when resolving the archived source row.",
    )
    parser.add_argument(
        "--reason",
        default="operator_quarantine_contaminated_source_event",
        help="Audit reason written into the append-only quarantine record.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the quarantine record and rebuild in place. Omit for dry-run.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = Path(args.config).resolve(strict=False)
        config = load_kernel_config(config_path)
        workspace = bootstrap_workspace(config.workspace_root)
        if workspace.layout is None:
            raise SourceEventQuarantineRepairError(
                "Workspace layout is required for source-event quarantine repair."
            )
        receipt = quarantine_and_repair_web_search_event(
            layout=workspace.layout,
            target_key=args.target_key,
            event_id=args.event_id,
            source_ref=args.source_ref,
            reason=args.reason,
            apply=bool(args.apply),
            rebuild_replay_range=lambda **kwargs: _release_replay_from_live_config(
                config_path=config_path,
                config=config,
                layout=workspace.layout,
                target_key=kwargs["target_key"],
                start_at=kwargs["start_at"],
                end_at=kwargs["end_at"],
                channels=("web_search", "market_news"),
                emit=print,
            ),
        )
    except (
        BootstrapConfigError,
        SourceEventQuarantineRepairError,
        WorkspaceBootstrapError,
    ) as exc:
        print(f"source-event quarantine repair failed: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            receipt.to_json_payload(workspace_root=workspace.layout.root),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if receipt.status == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
