"""Build fixed UTC calendar time-batch replay input from event-level replay JSONL."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compress event-level replay JSONL into one historical_manual_dataset "
            "record per fixed UTC calendar batch."
        )
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-key", default="gold")
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument(
        "--interval",
        required=True,
        help="UTC calendar batch interval, for example 5min, 15min, 1h, or 1d.",
    )
    parser.add_argument("--max-content-chars", type=int, default=7600)
    parser.add_argument("--max-item-chars", type=int, default=900)
    parser.add_argument("--include-empty", action="store_true")
    parser.add_argument("--audit-output", type=Path)
    args = parser.parse_args()

    window_start = _parse_window_boundary(args.window_start, end_of_day=False)
    window_end = _parse_window_boundary(args.window_end, end_of_day=True)
    interval_td, interval_tag = _parse_interval(args.interval)
    if window_end < window_start:
        raise SystemExit("window-end must be greater than or equal to window-start")
    if args.max_content_chars < 1000:
        raise SystemExit("--max-content-chars must be at least 1000")
    if args.max_item_chars < 100:
        raise SystemExit("--max-item-chars must be at least 100")

    rows = _load_jsonl(args.input)
    grouped: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    excluded = 0
    for row in rows:
        visible_at = _visible_at(row)
        if visible_at is None or not (window_start <= visible_at <= window_end):
            excluded += 1
            continue
        batch_at = _batch_end_for(visible_at, interval_td)
        grouped[batch_at].append(row)

    output_rows: list[dict[str, Any]] = []
    audit_batches: list[dict[str, Any]] = []
    previous_batch_at = _batch_end_before(window_start, interval_td)
    for batch_at in _iter_batch_ends(window_start, window_end, interval_td):
        batch_rows = sorted(
            grouped.get(batch_at, []),
            key=lambda item: (_visible_at(item) or batch_at, str(item.get("source_ref") or "")),
        )
        if batch_rows or args.include_empty:
            packet, truncated = _build_batch_packet(
                target_key=args.target_key,
                interval_tag=interval_tag,
                previous_batch_at=previous_batch_at,
                batch_at=batch_at,
                rows=batch_rows,
                max_content_chars=args.max_content_chars,
                max_item_chars=args.max_item_chars,
            )
            output_rows.append(packet)
            audit_batches.append(
                {
                    "batch_at": batch_at.isoformat(),
                    "previous_batch_at": previous_batch_at.isoformat(),
                    "interval": interval_tag,
                    "new_evidence_count": len(batch_rows),
                    "max_visible_at": (
                        max(
                            (_visible_at(item) for item in batch_rows if _visible_at(item) is not None),
                            default=None,
                        )
                    ).isoformat()
                    if batch_rows
                    else None,
                    "content_truncated": truncated,
                    "source_refs": [str(item.get("source_ref") or "") for item in batch_rows],
                }
            )
        previous_batch_at = batch_at

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in output_rows),
        encoding="utf-8",
    )

    audit = {
        "mode": "calendar_timebatch_dataset",
        "target_key": args.target_key,
        "interval": interval_tag,
        "input": str(args.input.resolve(strict=False)),
        "output": str(args.output.resolve(strict=False)),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "input_row_count": len(rows),
        "excluded_row_count": excluded,
        "output_batch_count": len(output_rows),
        "include_empty": bool(args.include_empty),
        "max_content_chars": args.max_content_chars,
        "max_item_chars": args.max_item_chars,
        "batches": audit_batches,
    }
    audit_output = args.audit_output
    if audit_output is None:
        audit_output = args.output.with_suffix(args.output.suffix + ".audit.json")
    audit_output.parent.mkdir(parents=True, exist_ok=True)
    audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    print(
        "calendar time-batch dataset:",
        f"interval={interval_tag}",
        f"input_rows={len(rows)}",
        f"excluded={excluded}",
        f"batches={len(output_rows)}",
        f"output={args.output}",
        f"audit={audit_output}",
    )
    return 0


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        raise SystemExit(f"input JSONL does not exist: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"line {line_number} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SystemExit(f"line {line_number} must decode to a JSON object")
        rows.append(payload)
    if not rows:
        raise SystemExit("input JSONL is empty")
    return rows


def _build_batch_packet(
    *,
    target_key: str,
    interval_tag: str,
    previous_batch_at: datetime,
    batch_at: datetime,
    rows: list[dict[str, Any]],
    max_content_chars: int,
    max_item_chars: int,
) -> tuple[dict[str, Any], bool]:
    lines = [
        f"ET calendar time-batch packet for target={target_key}",
        f"interval={interval_tag}",
        f"previous_batch_at_exclusive={previous_batch_at.isoformat()}",
        f"batch_at_inclusive={batch_at.isoformat()}",
        f"new_evidence_count={len(rows)}",
        "",
        "Calendar-batch rule: this packet is created by the fixed UTC clock, not by an individual evidence arrival.",
        "Only evidence visible no later than batch_at is included.",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        visible_at = _visible_at(row)
        labels = row.get("labels") if isinstance(row.get("labels"), list) else []
        content = _truncate(str(row.get("content") or row.get("body") or ""), max_item_chars)
        lines.extend(
            [
                f"[{index}] title={row.get('title') or row.get('headline') or '(untitled)'}",
                f"source_ref={row.get('source_ref') or ''}",
                f"visible_at={visible_at.isoformat() if visible_at else ''}",
                f"published_at={row.get('published_at')}",
                f"labels={labels}",
                f"summary={content}",
                "",
            ]
        )
    content = "\n".join(lines).strip()
    truncated = len(content) > max_content_chars
    if truncated:
        content = content[: max_content_chars - 160].rstrip() + (
            "\n\n[timebatch_packet_truncated: reduce max_item_chars or inspect audit sidecar for full source_refs]"
        )
    labels = [
        "event_type:other",
        "source_kind:operator_brief",
        "operator_confidence:high",
        f"operator_source_basis:calendar_timebatch_{interval_tag}",
        "event:calendar_timebatch",
        f"topic:{target_key}",
    ]
    batch_ref = batch_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    packet = {
        "source_shape": "historical_manual_dataset",
        "source_ref": f"event-trader://timebatch/{target_key}/{interval_tag}/{batch_ref}",
        "title": f"ET calendar time-batch {target_key} {interval_tag} {batch_ref}",
        "content": content,
        "provided_at": batch_at.isoformat(),
        "visible_at": batch_at.isoformat(),
        "labels": labels,
    }
    return packet, truncated


def _visible_at(row: dict[str, Any]) -> datetime | None:
    for field in ("visible_at", "discovered_at", "captured_at", "provided_at", "released_at"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return _parse_iso_datetime(value)
    return None


def _parse_window_boundary(raw_value: str, *, end_of_day: bool) -> datetime:
    text = raw_value.strip()
    if "T" not in text and len(text) == 10:
        suffix = "T23:59:59+00:00" if end_of_day else "T00:00:00+00:00"
        text = text + suffix
    return _parse_iso_datetime(text)


def _parse_iso_datetime(raw_value: str) -> datetime:
    parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SystemExit(f"timestamp must be timezone-aware: {raw_value}")
    return parsed.astimezone(UTC)


def _parse_interval(raw_value: str) -> tuple[timedelta, str]:
    text = raw_value.strip().lower()
    if text.endswith("min"):
        minutes = int(text[:-3])
        if minutes <= 0 or 1440 % minutes != 0:
            raise SystemExit("minute interval must be a positive divisor of 1440")
        return timedelta(minutes=minutes), f"{minutes}min"
    if text.endswith("h"):
        hours = int(text[:-1])
        if hours <= 0 or 24 % hours != 0:
            raise SystemExit("hour interval must be a positive divisor of 24")
        return timedelta(hours=hours), f"{hours}h"
    if text == "1d":
        return timedelta(days=1), "1d"
    raise SystemExit(f"unsupported interval: {raw_value}")


def _batch_end_for(value: datetime, interval: timedelta) -> datetime:
    day_start = datetime.combine(value.date(), time(0, 0), tzinfo=UTC)
    interval_seconds = int(interval.total_seconds())
    seconds_since_day_start = int((value - day_start).total_seconds())
    bucket_index = seconds_since_day_start // interval_seconds
    batch_start = day_start + timedelta(seconds=bucket_index * interval_seconds)
    return batch_start + interval - timedelta(seconds=1)


def _batch_end_before(value: datetime, interval: timedelta) -> datetime:
    current_batch_end = _batch_end_for(value, interval)
    if current_batch_end < value:
        return current_batch_end
    return current_batch_end - interval


def _iter_batch_ends(
    window_start: datetime,
    window_end: datetime,
    interval: timedelta,
):
    current = _batch_end_for(window_start, interval)
    while current <= window_end:
        yield current
        current += interval


def _truncate(value: str, max_chars: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 20].rstrip() + " [truncated]"


if __name__ == "__main__":
    raise SystemExit(main())
