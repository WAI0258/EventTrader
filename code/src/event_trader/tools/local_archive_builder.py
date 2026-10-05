"""Operator-facing CLI for building normalized local archive datasets."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from event_trader.market.local_archive_builder import (
    LocalArchiveBuildSpec,
    build_local_archive,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m event_trader.tools.local_archive_builder",
        description=(
            "Normalize repo-staged local market data into the formal local archive contract."
        ),
    )
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--archive-root", required=True, type=Path)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--exchange", required=True)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--bar-granularity", required=True)
    parser.add_argument("--price-adjustment", default="raw")
    parser.add_argument("--timestamp-policy", default="end_at")
    parser.add_argument("--input-datetime-format", default="%Y-%m-%d %H:%M:%S")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    spec = LocalArchiveBuildSpec(
        source_root=args.source_root,
        archive_root=args.archive_root,
        symbol=args.symbol,
        exchange=args.exchange,
        timezone=args.timezone,
        bar_granularity=args.bar_granularity,
        price_adjustment=args.price_adjustment,
        timestamp_policy=args.timestamp_policy,
        input_datetime_format=args.input_datetime_format,
    )
    receipt = build_local_archive(spec)
    print("local archive build complete:")
    print(f"- source_root={receipt.source_root.as_posix()}")
    print(f"- archive_root={receipt.archive_root.as_posix()}")
    print(f"- bars_path={receipt.bars_path.as_posix()}")
    print(f"- manifest_path={receipt.manifest_path.as_posix()}")
    print(f"- coverage_start={receipt.coverage_start.isoformat()}")
    print(f"- coverage_end={receipt.coverage_end.isoformat()}")
    print(f"- bar_count={receipt.bar_count}")
    print(
        "- identical_duplicates_collapsed="
        f"{receipt.identical_duplicates_collapsed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
