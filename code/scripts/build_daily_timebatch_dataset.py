"""Build fixed daily calendar-batch replay input from event-level replay JSONL."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any


_DEFAULT_LABELS = [
    "event_type:other",
    "source_kind:operator_brief",
    "operator_confidence:high",
    "operator_source_basis:daily_calendar_timebatch",
    "event:daily_timebatch",
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compress event-level replay JSONL into one historical_manual_dataset "
            "record per UTC day. The output is intended for ET daily calendar "
            "time-batch runs."
        )
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-key", default="gold")
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--max-content-chars", type=int, default=7600)
    parser.add_argument("--max-item-chars", type=int, default=900)
    parser.add_argument("--include-empty", action="store_true")
    parser.add_argument("--audit-output", type=Path)
    args = parser.parse_args()

    window_start = _parse_window_boundary(args.window_start, end_of_day=False)
    window_end = _parse_window_boundary(args.window_end, end_of_day=True)
    if window_end < window_start:
        raise SystemExit("window-end must be greater than or equal to window-start")
    if args.max_content_chars < 1000:
        raise SystemExit("--max-content-chars must be at least 1000")
    if args.max_item_chars < 100:
        raise SystemExit("--max-item-chars must be at least 100")

    rows = _load_jsonl(args.input)
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    excluded = 0
    for row in rows:
        visible_at = _visible_at(row)
        if visible_at is None or not (window_start <= visible_at <= window_end):
            excluded += 1
            continue
        grouped[visible_at.date()].append(row)

    output_rows: list[dict[str, Any]] = []
    audit_days: list[dict[str, Any]] = []
    previous_batch_at = datetime.combine(
        window_start.date() - timedelta(days=1),
        time(23, 59, 59),
        tzinfo=UTC,
    )
    for batch_day in _iter_days(window_start.date(), window_end.date()):
        batch_at = datetime.combine(batch_day, time(23, 59, 59), tzinfo=UTC)
        day_rows = sorted(
            grouped.get(batch_day, []),
            key=lambda item: (_visible_at(item) or batch_at, str(item.get("source_ref") or "")),
        )
        if day_rows or args.include_empty:
            packet, truncated = _build_batch_packet(
                target_key=args.target_key,
                batch_day=batch_day,
                previous_batch_at=previous_batch_at,
                batch_at=batch_at,
                rows=day_rows,
                max_content_chars=args.max_content_chars,
                max_item_chars=args.max_item_chars,
            )
            output_rows.append(packet)
            audit_days.append(
                {
                    "batch_day": batch_day.isoformat(),
                    "batch_at": batch_at.isoformat(),
                    "previous_batch_at": previous_batch_at.isoformat(),
                    "new_evidence_count": len(day_rows),
                    "max_visible_at": (
                        max((_visible_at(item) for item in day_rows if _visible_at(item) is not None), default=None)
                    ).isoformat()
                    if day_rows
                    else None,
                    "content_truncated": truncated,
                    "source_refs": [str(item.get("source_ref") or "") for item in day_rows],
                }
            )
        previous_batch_at = batch_at

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in output_rows),
        encoding="utf-8",
    )

    audit = {
        "mode": "daily_calendar_timebatch_dataset",
        "target_key": args.target_key,
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
        "days": audit_days,
    }
    audit_output = args.audit_output
    if audit_output is None:
        audit_output = args.output.with_suffix(args.output.suffix + ".audit.json")
    audit_output.parent.mkdir(parents=True, exist_ok=True)
    audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    print(
        "daily time-batch dataset:",
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
    batch_day: date,
    previous_batch_at: datetime,
    batch_at: datetime,
    rows: list[dict[str, Any]],
    max_content_chars: int,
    max_item_chars: int,
) -> tuple[dict[str, Any], bool]:
    lines = [
        f"ET daily calendar time-batch packet for target={target_key}",
        f"batch_day={batch_day.isoformat()}",
        f"previous_batch_at_exclusive={previous_batch_at.isoformat()}",
        f"batch_at_inclusive={batch_at.isoformat()}",
        f"new_evidence_count={len(rows)}",
        "",
        "Calendar-batch rule: this packet is created by the daily UTC clock, not by an individual evidence arrival.",
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
    packet = {
        "source_shape": "historical_manual_dataset",
        "source_ref": f"event-trader://timebatch/{target_key}/1d/{batch_day.isoformat()}",
        "title": f"ET daily calendar time-batch {target_key} {batch_day.isoformat()}",
        "content": content,
        "provided_at": batch_at.isoformat(),
        "visible_at": batch_at.isoformat(),
        "labels": list(_DEFAULT_LABELS),
    }
    if f"topic:{target_key}" not in packet["labels"]:
        packet["labels"].append(f"topic:{target_key}")
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


def _iter_days(start_day: date, end_day: date):
    current = start_day
    while current <= end_day:
        yield current
        current += timedelta(days=1)


def _truncate(value: str, max_chars: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 20].rstrip() + " [truncated]"


if __name__ == "__main__":
    raise SystemExit(main())
