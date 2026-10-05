from __future__ import annotations

import csv
import html
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path



def HexColor(value: str) -> str:
    return value


class Drawing:
    def __init__(self, width: float, height: float):
        self.width = width
        self.height = height
        self.items = []

    def add(self, item) -> None:
        self.items.append(item)

    def write_svg(self, path: Path) -> None:
        body = "\n".join(item.to_svg(self.height) for item in self.items)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{self.width:.3f}pt" height="{self.height:.3f}pt" '
            f'viewBox="0 0 {self.width:.3f} {self.height:.3f}">\n'
            f'<rect width="100%" height="100%" fill="white"/>\n{body}\n</svg>\n'
        )
        path.write_text(svg, encoding="utf-8")


class Line:
    def __init__(
        self,
        x1,
        y1,
        x2,
        y2,
        *,
        strokeColor="#000000",
        strokeWidth=1,
        strokeDashArray=None,
    ):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.strokeColor = strokeColor
        self.strokeWidth = strokeWidth
        self.strokeDashArray = strokeDashArray

    def to_svg(self, canvas_height: float) -> str:
        dash = ""
        if self.strokeDashArray:
            dash = f' stroke-dasharray="{",".join(map(str, self.strokeDashArray))}"'
        return (
            f'<line x1="{self.x1:.3f}" y1="{canvas_height-self.y1:.3f}" '
            f'x2="{self.x2:.3f}" y2="{canvas_height-self.y2:.3f}" '
            f'stroke="{self.strokeColor}" stroke-width="{self.strokeWidth}"{dash}/>'
        )


class Circle:
    def __init__(
        self,
        x,
        y,
        radius,
        *,
        fillColor="none",
        strokeColor="#000000",
        strokeWidth=1,
        fillOpacity=1,
    ):
        self.x, self.y, self.radius = x, y, radius
        self.fillColor = fillColor or "none"
        self.strokeColor = strokeColor or "none"
        self.strokeWidth = strokeWidth
        self.fillOpacity = fillOpacity

    def to_svg(self, canvas_height: float) -> str:
        return (
            f'<circle cx="{self.x:.3f}" cy="{canvas_height-self.y:.3f}" '
            f'r="{self.radius:.3f}" fill="{self.fillColor}" '
            f'fill-opacity="{self.fillOpacity}" stroke="{self.strokeColor}" '
            f'stroke-width="{self.strokeWidth}"/>'
        )


class Polygon:
    def __init__(
        self,
        points,
        *,
        fillColor="none",
        strokeColor="#000000",
        strokeWidth=1,
    ):
        self.points = points
        self.fillColor = fillColor or "none"
        self.strokeColor = strokeColor or "none"
        self.strokeWidth = strokeWidth

    def to_svg(self, canvas_height: float) -> str:
        points = " ".join(
            f"{x:.3f},{canvas_height-y:.3f}" for x, y in self.points
        )
        return (
            f'<polygon points="{points}" fill="{self.fillColor}" '
            f'stroke="{self.strokeColor}" stroke-width="{self.strokeWidth}" '
            f'stroke-linejoin="round"/>'
        )


class Rect:
    def __init__(
        self,
        x,
        y,
        width,
        height,
        *,
        fillColor="none",
        strokeColor="#000000",
        strokeWidth=1,
    ):
        self.x, self.y, self.width, self.height = x, y, width, height
        self.fillColor = fillColor or "none"
        self.strokeColor = strokeColor or "none"
        self.strokeWidth = strokeWidth

    def to_svg(self, canvas_height: float) -> str:
        return (
            f'<rect x="{self.x:.3f}" y="{canvas_height-self.y-self.height:.3f}" '
            f'width="{self.width:.3f}" height="{self.height:.3f}" '
            f'fill="{self.fillColor}" stroke="{self.strokeColor}" '
            f'stroke-width="{self.strokeWidth}"/>'
        )


class String:
    def __init__(
        self,
        x,
        y,
        text,
        *,
        fontName="Times-Roman",
        fontSize=7,
        fillColor="#000000",
        textAnchor="start",
        angle=0,
    ):
        self.x, self.y, self.text = x, y, text
        self.fontName = fontName
        self.fontSize = fontSize
        self.fillColor = fillColor
        self.textAnchor = textAnchor
        self.angle = angle

    def to_svg(self, canvas_height: float) -> str:
        y = canvas_height - self.y
        weight = "bold" if self.fontName.endswith("Bold") else "normal"
        transform = ""
        if self.angle:
            transform = f' transform="rotate({-self.angle:.3f} {self.x:.3f} {y:.3f})"'
        return (
            f'<text x="{self.x:.3f}" y="{y:.3f}" fill="{self.fillColor}" '
            f'font-family="Times New Roman, Times, serif" font-size="{self.fontSize}" '
            f'font-weight="{weight}" text-anchor="{self.textAnchor}"{transform}>'
            f'{html.escape(str(self.text))}</text>'
        )


class RLPath:
    def __init__(self):
        self.commands = []
        self.strokeColor = "#000000"
        self.strokeWidth = 1
        self.fillColor = None

    def moveTo(self, x, y) -> None:
        self.commands.append(("M", x, y))

    def lineTo(self, x, y) -> None:
        self.commands.append(("L", x, y))

    def to_svg(self, canvas_height: float) -> str:
        path = " ".join(
            f"{command} {x:.3f} {canvas_height-y:.3f}"
            for command, x, y in self.commands
        )
        fill = self.fillColor or "none"
        return (
            f'<path d="{path}" fill="{fill}" stroke="{self.strokeColor}" '
            f'stroke-width="{self.strokeWidth}" stroke-linejoin="round" '
            f'stroke-linecap="round"/>'
        )


ROOT = Path(__file__).resolve().parents[2] / "code"
OUT_DIR = Path(__file__).resolve().parent
IMAGE_DIR = Path(__file__).resolve().parents[2] / "paper" / "images"

WINDOW_START = datetime.fromisoformat("2026-02-04T00:00:00+00:00")
WINDOW_END = datetime.fromisoformat("2026-03-06T00:00:00+00:00")
LOG_TIMEZONE = timezone(timedelta(hours=8))

EVENT_WORKSPACE = (
    ROOT
    / ".local/server-gold-20260101-20260630/"
    "replay-gold-20260101_20260630-workspace"
)
EVENT_LOGS = (
    ROOT
    / ".local/server-gold-20260101-20260630/"
    "gold-gcusd-20260101_20260630-run/logs/mirothinker/analysis"
)

VARIANTS = {
    "Event-driven": (EVENT_WORKSPACE, EVENT_LOGS),
    "Time-batch (5 min)": (
        ROOT
        / ".local/time-batch-baseline/"
        "replay-gold-timebatch5m-20260204_20260305-workspace",
        ROOT
        / ".local/time-batch-baseline/logs/"
        "gold-timebatch5m-20260204_20260305-run/logs/mirothinker/analysis",
    ),
    "Time-batch (1 h)": (
        ROOT
        / ".local/time-batch-baseline/"
        "replay-gold-timebatch1h-20260204_20260305-workspace",
        ROOT
        / ".local/time-batch-baseline/logs/"
        "gold-timebatch1h-20260204_20260305-run/logs/mirothinker/analysis",
    ),
    "Time-batch (4 h)": (
        ROOT
        / ".local/time-batch-baseline/"
        "replay-gold-timebatch4h-20260204_20260305-workspace",
        ROOT
        / ".local/time-batch-baseline/logs/"
        "gold-timebatch4h-20260204_20260305-run/logs/mirothinker/analysis",
    ),
    "Time-batch (1 day)": (
        ROOT
        / ".local/time-batch-baseline/"
        "replay-gold-timebatch1d-20260204_20260305-workspace",
        ROOT
        / ".local/time-batch-baseline/logs/"
        "gold-timebatch1d-20260204_20260305-run/logs/mirothinker/analysis",
    ),
}

# These exclusions are provenance-based. They are not selected from a
# statistical threshold. Each record has a known external failure mode.
RUNTIME_EXCLUSIONS = {
    "event-trader-analysis-gold-3f8db2ee939e27abf4ef3162":
        "upstream_llm_fault",
}
DELAY_EXCLUSIONS = {
    "unit-e4f31303a30662095d3d13aa": "replay_watermark_gap",
}

TOKEN_RE = re.compile(
    r"Input:\s*(\d+),\s*Cache:\s*(\d+)\+(\d+),\s*Output:\s*(\d+)"
)

STATE_VALUE = {
    "strong_short": -2,
    "weak_short": -1,
    "flat": 0,
    "weak_long": 1,
    "strong_long": 2,
}
STATE_LABEL = {
    "strong_short": "SS",
    "weak_short": "WS",
    "flat": "F",
    "weak_long": "WL",
    "strong_long": "SL",
}
STATE_COLOR = {
    "strong_short": "#3B4CC0",
    "weak_short": "#9BB9FF",
    "flat": "#D9D9D9",
    "weak_long": "#F5A081",
    "strong_long": "#B40426",
}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def parse_log_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=LOG_TIMEZONE
    ).astimezone(timezone.utc)


def in_window(value: str) -> bool:
    timestamp = datetime.fromisoformat(value)
    return WINDOW_START <= timestamp < WINDOW_END


def load_outcomes(workspace: Path) -> dict[str, dict]:
    outcomes = {}
    for path in (workspace / "runtime/analysis_outcomes/gold").glob("*.jsonl"):
        for record in iter_jsonl(path):
            outcomes[record["task_id"]] = record
    return outcomes


def load_completed_tasks(workspace: Path) -> dict[str, dict]:
    tasks = {}
    queue = workspace / "runtime/analysis_work_queue/analysis/completed"
    for path in queue.glob("*.json"):
        record = read_json(path)
        if not in_window(record["item"]["event_time"]):
            continue
        event_ids = record.get("outcome", {}).get("event_ids") or []
        if not event_ids:
            continue
        task_id = f"event-trader-analysis-gold-{event_ids[0]}"
        tasks[task_id] = record
    return tasks


def count_triggers(workspace: Path) -> int:
    count = 0
    queue_root = workspace / "runtime/analysis_work_queue/analysis"
    for status in ("completed", "failed", "dead_letters", "pending", "in_flight"):
        path = queue_root / status
        if not path.exists():
            continue
        for item_path in path.glob("*.json"):
            record = read_json(item_path)
            item = record.get("item") or record
            event_time = item.get("event_time")
            if event_time and in_window(event_time):
                count += 1
    return count


def token_count(log: dict) -> int:
    total = 0
    for step in log.get("step_logs") or []:
        if "Token Usage" not in step.get("step_name", ""):
            continue
        match = TOKEN_RE.search(step.get("message", ""))
        if match:
            total += sum(map(int, match.groups()))
    return total


def match_committed_logs(
    workspace: Path, log_dir: Path
) -> list[dict[str, object]]:
    tasks = load_completed_tasks(workspace)
    outcomes = load_outcomes(workspace)
    logs_by_task: dict[str, list[tuple[Path, dict]]] = defaultdict(list)
    for path in log_dir.glob("*.json"):
        log = read_json(path)
        task_id = log.get("task_id")
        if task_id in tasks and log.get("status") == "success":
            logs_by_task[task_id].append((path, log))

    matched = []
    for task_id, queue_record in tasks.items():
        committed_at = datetime.fromisoformat(outcomes[task_id]["committed_at"])
        candidates = logs_by_task[task_id]
        if not candidates:
            raise RuntimeError(f"No successful log for {task_id}")
        path, log = min(
            candidates,
            key=lambda item: abs(
                (parse_log_time(item[1]["end_time"]) - committed_at).total_seconds()
            ),
        )
        commit_delta = abs(
            (parse_log_time(log["end_time"]) - committed_at).total_seconds()
        )
        # Normal queue publication follows the worker log within a few seconds.
        # The largest observed handoff is 9.8 seconds in the five-minute run.
        if commit_delta > 15:
            raise RuntimeError(
                f"Log/commit mismatch for {task_id}: {commit_delta:.3f} seconds"
            )
        duration = (
            parse_log_time(log["end_time"]) - parse_log_time(log["start_time"])
        ).total_seconds()
        matched.append(
            {
                "task_id": task_id,
                "analysis_unit_id": queue_record["item"]["analysis_unit_id"],
                "event_time": queue_record["item"]["event_time"],
                "duration_seconds": duration,
                "tokens": token_count(log),
                "log_path": str(path.relative_to(ROOT)),
                "commit_delta_seconds": commit_delta,
            }
        )
    return matched


def write_cost_metrics() -> list[dict[str, object]]:
    rows = []
    for variant, (workspace, log_dir) in VARIANTS.items():
        matched = match_committed_logs(workspace, log_dir)
        included = [
            row
            for row in matched
            if row["task_id"] not in RUNTIME_EXCLUSIONS
        ]
        excluded = len(matched) - len(included)
        total_seconds = sum(float(row["duration_seconds"]) for row in included)
        total_tokens = sum(int(row["tokens"]) for row in included)
        rows.append(
            {
                "variant": variant,
                "included_successful_analyses": len(included),
                "excluded_successful_analyses": excluded,
                "analysis_triggers": count_triggers(workspace),
                "average_analysis_seconds": total_seconds / len(included),
                "total_analysis_seconds": total_seconds,
                "total_analysis_hours": total_seconds / 3600,
                "successful_tokens": total_tokens,
                "successful_tokens_millions": total_tokens / 1_000_000,
            }
        )

    path = OUT_DIR / "analysis_cost_clean.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return rows


def load_event_visibility() -> dict[str, datetime]:
    visibility = {}
    for path in (EVENT_WORKSPACE / "ledger/gold").glob("2026-*.jsonl"):
        for record in iter_jsonl(path):
            value = record.get("ts_init") or record.get("ts_event")
            if value:
                visibility[record["event_id"]] = datetime.fromisoformat(value)
    return visibility


def write_delay_points() -> list[dict[str, object]]:
    completed = load_completed_tasks(EVENT_WORKSPACE)
    successful_records = {
        record["item"]["record_id"]: record["item"]["analysis_unit_id"]
        for record in completed.values()
    }
    visibility = load_event_visibility()
    rows = []
    for month in ("2026-02.jsonl", "2026-03.jsonl"):
        path = EVENT_WORKSPACE / "runtime/ceau/gold" / month
        for record in iter_jsonl(path):
            if record.get("record_type") != "unit_emitted":
                continue
            if record.get("record_id") not in successful_records:
                continue
            unit_id = record["analysis_unit_id"]
            if unit_id in DELAY_EXCLUSIONS:
                continue
            emitted_at = datetime.fromisoformat(record["recorded_at"])
            mode = (
                "hard trigger"
                if record.get("emission_reason") == "hard_trigger"
                else "market-reaction maturity"
            )
            for event_id in record["event_ids"]:
                visible_at = visibility[event_id]
                rows.append(
                    {
                        "event_id": event_id,
                        "analysis_unit_id": unit_id,
                        "visible_at": visible_at.isoformat(),
                        "emitted_at": emitted_at.isoformat(),
                        "delay_minutes": (
                            emitted_at - visible_at
                        ).total_seconds()
                        / 60,
                        "emission_mode": mode,
                    }
                )
    rows.sort(key=lambda row: row["visible_at"])
    if len(rows) != 235:
        raise RuntimeError(f"Expected 235 cleaned delay points, found {len(rows)}")
    path = OUT_DIR / "event_driven_delay_points_clean.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return rows


def add_text(
    drawing: Drawing,
    x: float,
    y: float,
    text: str,
    *,
    size: float = 7,
    anchor: str = "start",
    color: str = "#202020",
    bold: bool = False,
    angle: float = 0,
) -> None:
    drawing.add(
        String(
            x,
            y,
            text,
            fontName="Times-Bold" if bold else "Times-Roman",
            fontSize=size,
            fillColor=HexColor(color),
            textAnchor=anchor,
            angle=angle,
        )
    )


def save_drawing(drawing: Drawing, stem: str) -> None:
    drawing.write_svg(IMAGE_DIR / f"{stem}.svg")


def linear_map(
    value: float, source_min: float, source_max: float, target_min: float, target_max: float
) -> float:
    return target_min + (value - source_min) / (source_max - source_min) * (
        target_max - target_min
    )


def time_value(value: datetime) -> float:
    return value.timestamp()


def plot_delay(rows: list[dict[str, object]]) -> None:
    width, height = 7.1 * 72, 2.6 * 72
    left, right, bottom, top = 55, width - 10, 31, height - 10
    drawing = Drawing(width, height)
    x_min, x_max = time_value(WINDOW_START), time_value(WINDOW_END)
    y_min, y_max = -1.5, 33.5

    def x_map(value: datetime) -> float:
        return linear_map(time_value(value), x_min, x_max, left, right)

    def y_map(value: float) -> float:
        return linear_map(value, y_min, y_max, bottom, top)

    for tick in range(0, 31, 5):
        y = y_map(tick)
        drawing.add(Line(left, y, right, y, strokeColor=HexColor("#D0D0D0"), strokeWidth=0.35))
        add_text(drawing, left - 6, y - 2.2, str(tick), anchor="end", size=6.8)
    drawing.add(Line(left, bottom, left, top, strokeColor=HexColor("#202020"), strokeWidth=0.65))
    drawing.add(Line(left, bottom, right, bottom, strokeColor=HexColor("#202020"), strokeWidth=0.65))

    tick = WINDOW_START
    while tick <= WINDOW_END:
        x = x_map(tick)
        drawing.add(Line(x, bottom, x, bottom - 3, strokeColor=HexColor("#202020"), strokeWidth=0.55))
        add_text(drawing, x, bottom - 12, tick.strftime("%b %d"), anchor="middle", size=6.6)
        tick += timedelta(days=4)

    maturity_color = "#0072B2"
    hard_color = "#D55E00"
    for row in rows:
        x = x_map(datetime.fromisoformat(str(row["visible_at"])))
        y = y_map(float(row["delay_minutes"]))
        if row["emission_mode"] == "hard trigger":
            drawing.add(Line(x - 2.4, y - 2.4, x + 2.4, y + 2.4, strokeColor=HexColor(hard_color), strokeWidth=0.9))
            drawing.add(Line(x - 2.4, y + 2.4, x + 2.4, y - 2.4, strokeColor=HexColor(hard_color), strokeWidth=0.9))
        else:
            drawing.add(Circle(x, y, 1.8, fillColor=HexColor(maturity_color), strokeColor=None, fillOpacity=0.78))

    maturity_y = y_map(30)
    drawing.add(
        Line(
            left,
            maturity_y,
            right,
            maturity_y,
            strokeColor=HexColor("#555555"),
            strokeWidth=0.8,
            strokeDashArray=[3, 2],
        )
    )
    add_text(
        drawing,
        right - 2,
        maturity_y + 4,
        "30-minute maturity boundary",
        anchor="end",
        size=6.8,
        color="#444444",
    )
    add_text(
        drawing,
        (left + right) / 2,
        4,
        "Evidence visibility time (UTC)",
        anchor="middle",
        size=7.5,
    )
    add_text(
        drawing,
        12,
        (bottom + top) / 2,
        "Evidence-to-analysis-input delay (min)",
        anchor="middle",
        size=7.5,
        angle=90,
    )

    # Keep the key above the observed range. Placing it near zero hides the
    # hard-trigger observations that the figure is meant to expose.
    legend_y = y_map(32.3)
    drawing.add(Circle(left + 5, legend_y, 1.8, fillColor=HexColor(maturity_color), strokeColor=None))
    add_text(drawing, left + 11, legend_y - 2.2, "Market-reaction maturity", size=6.8)
    cross_x = left + 115
    drawing.add(Line(cross_x - 2.2, legend_y - 2.2, cross_x + 2.2, legend_y + 2.2, strokeColor=HexColor(hard_color), strokeWidth=0.9))
    drawing.add(Line(cross_x - 2.2, legend_y + 2.2, cross_x + 2.2, legend_y - 2.2, strokeColor=HexColor(hard_color), strokeWidth=0.9))
    add_text(drawing, cross_x + 7, legend_y - 2.2, "Hard trigger", size=6.8)
    save_drawing(drawing, "event_driven_evidence_delay")


def load_bars() -> list[dict]:
    path = (
        EVENT_WORKSPACE
        / "runtime/market_data/gold/"
        "gold-gcusd-20260101-20260630/bars/GCUSD_5min.jsonl"
    )
    bars = []
    for record in iter_jsonl(path):
        start = datetime.fromisoformat(record["start_at"])
        if WINDOW_START - timedelta(days=1) <= start < WINDOW_END + timedelta(days=1):
            record["start_dt"] = start
            record["end_dt"] = datetime.fromisoformat(record["end_at"])
            bars.append(record)
    return bars


def load_state_timeline(workspace: Path) -> list[tuple[datetime, str]]:
    timeline = []
    for outcome in load_outcomes(workspace).values():
        assessment = outcome.get("analysis_assessment") or {}
        state = assessment.get("as_if_flat_state")
        if state:
            timeline.append((datetime.fromisoformat(outcome["business_at"]), state))
    return sorted(timeline)


def state_segments(
    timeline: list[tuple[datetime, str]], start: datetime, end: datetime
) -> list[tuple[datetime, datetime, str]]:
    state = "flat"
    for timestamp, candidate in timeline:
        if timestamp <= start:
            state = candidate
        else:
            break
    changes = [(start, state)] + [
        (timestamp, candidate)
        for timestamp, candidate in timeline
        if start < timestamp < end
    ]
    segments = []
    for index, (timestamp, candidate) in enumerate(changes):
        segment_end = changes[index + 1][0] if index + 1 < len(changes) else end
        segments.append((timestamp, segment_end, candidate))
    return segments


def draw_state_tracks(
    drawing: Drawing,
    panel_left: float,
    panel_right: float,
    panel_bottom: float,
    panel_top: float,
    timelines: dict[str, list[tuple[datetime, str]]],
    start: datetime,
    end: datetime,
    *,
    show_labels: bool,
) -> None:
    names = list(VARIANTS)
    row_height = (panel_top - panel_bottom) / len(names)
    for row_index, name in enumerate(names):
        y = panel_top - (row_index + 1) * row_height
        for segment_start, segment_end, state in state_segments(
            timelines[name], start, end
        ):
            x0 = linear_map(time_value(segment_start), time_value(start), time_value(end), panel_left, panel_right)
            x1 = linear_map(time_value(segment_end), time_value(start), time_value(end), panel_left, panel_right)
            drawing.add(
                Rect(
                    x0,
                    y + 1.4,
                    x1 - x0,
                    row_height - 2.8,
                    fillColor=HexColor(STATE_COLOR[state]),
                    strokeColor=HexColor("#FFFFFF"),
                    strokeWidth=0.45,
                )
            )
            minutes = (segment_end - segment_start).total_seconds() / 60
            if minutes >= 18:
                text_color = "white" if abs(STATE_VALUE[state]) == 2 else "black"
                add_text(
                    drawing,
                    (x0 + x1) / 2,
                    y + row_height / 2 - 2,
                    STATE_LABEL[state],
                    anchor="middle",
                    size=6.3,
                    color="#FFFFFF" if text_color == "white" else "#202020",
                )
        if show_labels:
            add_text(
                drawing,
                panel_left - 5,
                y + row_height / 2 - 2,
                name,
                anchor="end",
                size=6.4,
            )


def plot_cases() -> None:
    bars = load_bars()
    timelines = {
        name: load_state_timeline(workspace)
        for name, (workspace, _) in VARIANTS.items()
    }
    cases = [
        {
            "label": "(a) Bullish continuation",
            "start": datetime.fromisoformat("2026-03-02T15:20:00+00:00"),
            "end": datetime.fromisoformat("2026-03-02T21:00:00+00:00"),
            "evidence": [datetime.fromisoformat("2026-03-02T15:41:03+00:00")],
            "decision": datetime.fromisoformat("2026-03-02T15:42:10+00:00"),
        },
        {
            "label": "(b) Bearish continuation",
            "start": datetime.fromisoformat("2026-02-05T13:30:00+00:00"),
            "end": datetime.fromisoformat("2026-02-05T20:35:00+00:00"),
            "evidence": [datetime.fromisoformat("2026-02-05T14:00:00+00:00")],
            "decision": datetime.fromisoformat("2026-02-05T14:30:00+00:00"),
        },
    ]

    width, height = 7.2 * 72, 5.25 * 72
    drawing = Drawing(width, height)
    panel_width = 205
    panel_lefts = [57, 305]
    price_bottom, price_top = 177, 365
    state_bottom, state_top = 49, 150
    for column, case in enumerate(cases):
        panel_left = panel_lefts[column]
        panel_right = panel_left + panel_width
        case_bars = [
            bar
            for bar in bars
            if case["start"] <= bar["end_dt"] <= case["end"]
        ]
        prices = [bar["close_price"] for bar in case_bars]
        price_min, price_max = min(prices), max(prices)
        pad = max((price_max - price_min) * 0.08, 1)
        price_min -= pad
        price_max += pad

        def x_map(value: datetime) -> float:
            return linear_map(time_value(value), time_value(case["start"]), time_value(case["end"]), panel_left, panel_right)

        def y_map(value: float) -> float:
            return linear_map(value, price_min, price_max, price_bottom, price_top)

        drawing.add(Line(panel_left, price_bottom, panel_left, price_top, strokeColor=HexColor("#202020"), strokeWidth=0.65))
        drawing.add(Line(panel_left, price_bottom, panel_right, price_bottom, strokeColor=HexColor("#202020"), strokeWidth=0.65))
        for tick_index in range(4):
            tick_value = price_min + tick_index * (price_max - price_min) / 3
            tick_y = y_map(tick_value)
            drawing.add(Line(panel_left, tick_y, panel_right, tick_y, strokeColor=HexColor("#D0D0D0"), strokeWidth=0.35))
            add_text(drawing, panel_left - 5, tick_y - 2.2, f"{tick_value:.0f}", anchor="end", size=6.4)
        path = RLPath()
        for index, bar in enumerate(case_bars):
            x, y = x_map(bar["end_dt"]), y_map(bar["close_price"])
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        path.strokeColor = HexColor("#202020")
        path.strokeWidth = 1.1
        path.fillColor = None
        drawing.add(path)
        for evidence_time in case["evidence"]:
            x = x_map(evidence_time)
            drawing.add(Line(x, state_bottom, x, price_top, strokeColor=HexColor("#7B3294"), strokeWidth=0.75, strokeDashArray=[3, 2]))
        decision_x = x_map(case["decision"])
        drawing.add(Line(decision_x, state_bottom, decision_x, price_top, strokeColor=HexColor("#008837"), strokeWidth=1.0))
        # The vertical lines and common legend carry the timing information.
        # A small white title plate keeps the panel label legible without
        # covering the price path with case-specific prose.
        drawing.add(Rect(panel_left + 1, price_top - 14, 94, 14, fillColor="#FFFFFF", strokeColor=None))
        add_text(drawing, panel_left + 4, price_top - 10, case["label"], size=8.2, bold=True)
        add_text(drawing, panel_left - 36, (price_bottom + price_top) / 2, "GCUSD", anchor="middle", size=7.2, angle=90)
        draw_state_tracks(
            drawing,
            panel_left,
            panel_right,
            state_bottom,
            state_top,
            timelines,
            case["start"],
            case["end"],
            show_labels=column == 0,
        )
        tick = case["start"].replace(minute=0, second=0, microsecond=0)
        if tick < case["start"]:
            tick += timedelta(hours=1)
        while tick <= case["end"]:
            tick_x = x_map(tick)
            drawing.add(Line(tick_x, state_bottom, tick_x, state_bottom - 3, strokeColor=HexColor("#202020"), strokeWidth=0.5))
            add_text(drawing, tick_x, state_bottom - 12, tick.strftime("%H:%M"), anchor="middle", size=6.2)
            tick += timedelta(hours=1)
        add_text(drawing, (panel_left + panel_right) / 2, state_bottom - 24, "UTC", anchor="middle", size=7)

    long_bars = [
        bar
        for bar in bars
        if cases[0]["decision"] <= bar["end_dt"] <= cases[0]["end"]
    ]
    long_peak = max(long_bars, key=lambda bar: bar["high_price"])
    long_entry = next(
        bar
        for bar in bars
        if bar["start_dt"] >= datetime.fromisoformat("2026-03-02T15:45:00+00:00")
    )
    long_return = long_peak["high_price"] / long_entry["open_price"] - 1
    long_case = cases[0]
    long_x = linear_map(time_value(long_peak["end_dt"]), time_value(long_case["start"]), time_value(long_case["end"]), panel_lefts[0], panel_lefts[0] + panel_width)
    long_prices = [bar["close_price"] for bar in bars if long_case["start"] <= bar["end_dt"] <= long_case["end"]]
    long_pad = max((max(long_prices) - min(long_prices)) * 0.08, 1)
    long_y = linear_map(long_peak["high_price"], min(long_prices) - long_pad, max(long_prices) + long_pad, price_bottom, price_top)
    drawing.add(Circle(long_x, long_y, 2.2, fillColor=HexColor("#D55E00"), strokeColor=None))
    add_text(
        drawing,
        long_x - 4,
        long_y - 12,
        f"+{long_return * 100:.2f}% to session peak",
        anchor="end",
        size=6.8,
        color="#8A3500",
    )

    short_entry = next(
        bar
        for bar in bars
        if bar["start_dt"] >= datetime.fromisoformat("2026-02-05T14:30:00+00:00")
    )
    short_low = min(
        (
            bar
            for bar in bars
            if short_entry["start_dt"] <= bar["start_dt"]
            <= datetime.fromisoformat("2026-02-05T20:35:00+00:00")
        ),
        key=lambda bar: bar["low_price"],
    )
    short_return = short_low["low_price"] / short_entry["open_price"] - 1
    short_case = cases[1]
    short_prices = [bar["close_price"] for bar in bars if short_case["start"] <= bar["end_dt"] <= short_case["end"]]
    short_pad = max((max(short_prices) - min(short_prices)) * 0.08, 1)
    short_x = linear_map(time_value(short_low["end_dt"]), time_value(short_case["start"]), time_value(short_case["end"]), panel_lefts[1], panel_lefts[1] + panel_width)
    short_y = linear_map(short_low["low_price"], min(short_prices) - short_pad, max(short_prices) + short_pad, price_bottom, price_top)
    drawing.add(Circle(short_x, short_y, 2.2, fillColor=HexColor("#0072B2"), strokeColor=None))
    add_text(
        drawing,
        short_x - 4,
        short_y + 6,
        f"{short_return * 100:.2f}% to local low",
        anchor="end",
        size=6.8,
        color="#00527E",
    )
    legend_y = 10
    drawing.add(Line(8, legend_y + 3, 24, legend_y + 3, strokeColor=HexColor("#7B3294"), strokeWidth=0.8, strokeDashArray=[3, 2]))
    add_text(drawing, 28, legend_y, "Evidence visible", size=6.2)
    drawing.add(Line(98, legend_y + 3, 114, legend_y + 3, strokeColor=HexColor("#008837"), strokeWidth=1.0))
    add_text(drawing, 118, legend_y, "Event-driven analysis", size=6.2)
    legend_x = 224
    for state in ("strong_long", "weak_long", "flat", "weak_short", "strong_short"):
        drawing.add(Rect(legend_x, legend_y - 1, 10, 8, fillColor=HexColor(STATE_COLOR[state]), strokeColor=None))
        add_text(drawing, legend_x + 13, legend_y, STATE_LABEL[state], size=6.2)
        legend_x += 47
    save_drawing(drawing, "event_driven_directional_cases")


RESPONSE_STATE_LABEL = {
    "strong_long": "Strong Long",
    "weak_long": "Weak Long",
    "flat": "Flat",
    "weak_short": "Weak Short",
    "strong_short": "Strong Short",
    "no_direction": "No directional assessment",
}


def draw_response_marker(
    drawing: Drawing,
    x: float,
    y: float,
    state: str,
    *,
    size: float = 3.7,
) -> None:
    if state in ("strong_long", "weak_long"):
        color = "#D55E00"
        fill = color if state == "strong_long" else "#FFFFFF"
        drawing.add(
            Polygon(
                [(x, y + size), (x - size, y - size), (x + size, y - size)],
                fillColor=fill,
                strokeColor=color,
                strokeWidth=1.15,
            )
        )
    elif state in ("strong_short", "weak_short"):
        color = "#0072B2"
        fill = color if state == "strong_short" else "#FFFFFF"
        drawing.add(
            Polygon(
                [(x, y - size), (x - size, y + size), (x + size, y + size)],
                fillColor=fill,
                strokeColor=color,
                strokeWidth=1.15,
            )
        )
    elif state == "flat":
        drawing.add(
            Circle(
                x,
                y,
                size,
                fillColor="#8C8C8C",
                strokeColor="#555555",
                strokeWidth=0.9,
            )
        )
    elif state == "no_direction":
        drawing.add(
            Rect(
                x - size,
                y - size,
                size * 2,
                size * 2,
                fillColor="#FFFFFF",
                strokeColor="#202020",
                strokeWidth=1.0,
            )
        )
    else:
        raise ValueError(f"Unknown response state: {state}")


def interpolate_response_return(
    points: list[tuple[datetime, float]], timestamp: datetime
) -> float:
    if timestamp <= points[0][0]:
        return points[0][1]
    for index in range(1, len(points)):
        right_time, right_value = points[index]
        if timestamp <= right_time:
            left_time, left_value = points[index - 1]
            span = (right_time - left_time).total_seconds()
            weight = (timestamp - left_time).total_seconds() / span
            return left_value + weight * (right_value - left_value)
    return points[-1][1]


def plot_response_cases() -> None:
    """Plot post-evidence returns and the first focal-evidence analysis result."""
    bars = load_bars()
    cases = [
        {
            "title": "(a) Iranian counterattacks widen conflict after U.S. and Israeli strikes",
            "evidence": datetime.fromisoformat("2026-03-02T15:41:03+00:00"),
            "anchor": datetime.fromisoformat("2026-03-02T15:45:00+00:00"),
            "window_label": "Mar 2, 15:41–21:41 UTC",
            "y_ticks": [-0.5, 0.0, 0.5, 1.0],
            "responses": [
                ("Event-driven", "2026-03-02T15:42:10+00:00", "strong_long"),
                ("Time-batch (5 min)", "2026-03-02T15:44:59+00:00", "strong_long"),
                ("Time-batch (1 h)", "2026-03-02T15:59:59+00:00", "flat"),
                ("Time-batch (4 h)", "2026-03-02T15:59:59+00:00", "no_direction"),
                ("Time-batch (1 day)", None, None),
            ],
        },
        {
            "title": "(b) U.S.–Iran talks reach an understanding on guiding principles",
            "evidence": datetime.fromisoformat("2026-02-18T00:08:32.287000+00:00"),
            "anchor": datetime.fromisoformat("2026-02-18T00:10:00+00:00"),
            "window_label": "Feb 18, 00:08–06:08 UTC",
            "y_ticks": [-0.4, -0.2, 0.0, 0.2],
            "responses": [
                ("Event-driven", "2026-02-18T00:38:32.287000+00:00", "weak_short"),
                ("Time-batch (5 min)", "2026-02-18T00:09:59+00:00", "weak_short"),
                ("Time-batch (1 h)", "2026-02-18T00:59:59+00:00", "no_direction"),
                ("Time-batch (4 h)", "2026-02-18T03:59:59+00:00", "no_direction"),
                ("Time-batch (1 day)", None, None),
            ],
        },
    ]

    width, height = 7.16 * 72, 5.7 * 72
    drawing = Drawing(width, height)
    plot_left, plot_right = 128, width - 13
    case_tops = [height - 13, height - 212]

    for case_index, case in enumerate(cases):
        evidence = case["evidence"]
        horizon = evidence + timedelta(hours=6)
        eligible_bars = [
            bar
            for bar in bars
            if case["anchor"] <= bar["start_dt"] and bar["end_dt"] <= horizon
        ]
        if not eligible_bars:
            raise RuntimeError(f"No bars for case beginning {evidence.isoformat()}")
        base_price = eligible_bars[0]["open_price"]
        points = [(evidence, 0.0)] + [
            (bar["end_dt"], (bar["close_price"] / base_price - 1) * 100)
            for bar in eligible_bars
        ]
        y_ticks = case["y_ticks"]
        y_min, y_max = min(y_ticks) - 0.08 * (max(y_ticks) - min(y_ticks)), max(y_ticks) + 0.08 * (max(y_ticks) - min(y_ticks))

        top = case_tops[case_index]
        chart_top, chart_bottom = top - 22, top - 93
        lane_top, lane_spacing = chart_bottom - 12, 13
        lane_ys = [lane_top - index * lane_spacing for index in range(5)]
        axis_y = top - 168

        def x_map(value: datetime) -> float:
            hours = (value - evidence).total_seconds() / 3600
            return linear_map(hours, 0, 6, plot_left, plot_right)

        def y_map(value: float) -> float:
            return linear_map(value, y_min, y_max, chart_bottom, chart_top)

        add_text(drawing, 10, top, case["title"], size=8.8, bold=True)
        add_text(
            drawing,
            plot_right,
            top,
            case["window_label"],
            size=8,
            anchor="end",
            color="#444444",
        )

        for hour in range(7):
            x = linear_map(hour, 0, 6, plot_left, plot_right)
            drawing.add(
                Line(
                    x,
                    axis_y,
                    x,
                    chart_top,
                    strokeColor="#E2E2E2",
                    strokeWidth=0.45,
                )
            )
            drawing.add(
                Line(
                    x,
                    axis_y,
                    x,
                    axis_y - 3,
                    strokeColor="#202020",
                    strokeWidth=0.55,
                )
            )
            add_text(drawing, x, axis_y - 12, str(hour), size=8, anchor="middle")

        for tick in y_ticks:
            y = y_map(tick)
            drawing.add(
                Line(
                    plot_left,
                    y,
                    plot_right,
                    y,
                    strokeColor="#D0D0D0" if tick else "#9A9A9A",
                    strokeWidth=0.45 if tick else 0.75,
                    strokeDashArray=None if tick else [3, 2],
                )
            )
            add_text(
                drawing,
                plot_left - 7,
                y - 2.5,
                f"{tick:.1f}",
                size=8,
                anchor="end",
            )

        drawing.add(
            Line(
                plot_left,
                chart_bottom,
                plot_left,
                chart_top,
                strokeColor="#202020",
                strokeWidth=0.7,
            )
        )
        drawing.add(
            Line(
                plot_left,
                axis_y,
                plot_right,
                axis_y,
                strokeColor="#202020",
                strokeWidth=0.7,
            )
        )

        price_path = RLPath()
        for point_index, (timestamp, return_value) in enumerate(points):
            x, y = x_map(timestamp), y_map(return_value)
            if point_index == 0:
                price_path.moveTo(x, y)
            else:
                price_path.lineTo(x, y)
        price_path.strokeColor = "#202020"
        price_path.strokeWidth = 1.35
        drawing.add(price_path)

        add_text(
            drawing,
            16,
            (chart_bottom + chart_top) / 2,
            "GCUSD return (%)",
            size=8.3,
            anchor="middle",
            angle=90,
        )
        add_text(
            drawing,
            plot_left + 4,
            chart_top + 7,
            "Evidence visible",
            size=8,
            color="#7B3294",
        )

        for row_index, (method, timestamp_text, state) in enumerate(case["responses"]):
            row_y = lane_ys[row_index]
            if method == "Event-driven":
                drawing.add(
                    Rect(
                        6,
                        row_y - 5.5,
                        plot_right - 6,
                        11,
                        fillColor="#EAF6F1",
                        strokeColor=None,
                    )
                )
            add_text(
                drawing,
                plot_left - 7,
                row_y - 2.7,
                method,
                size=8,
                anchor="end",
                bold=method == "Event-driven",
            )
            drawing.add(
                Line(
                    plot_left,
                    row_y,
                    plot_right,
                    row_y,
                    strokeColor="#B8B8B8",
                    strokeWidth=0.55,
                )
            )

            if timestamp_text is None:
                add_text(
                    drawing,
                    plot_right - 3,
                    row_y - 2.7,
                    "No analysis result within 6 h",
                    size=8,
                    anchor="end",
                    color="#555555",
                )
                continue

            timestamp = datetime.fromisoformat(timestamp_text)
            response_x = x_map(timestamp)
            response_return = interpolate_response_return(points, timestamp)
            response_y = y_map(response_return)
            # Exact-time guides connect each price marker to its method row.
            # Small vertical offsets only separate coincident chart markers;
            # their horizontal positions remain the recorded result times.
            chart_offset = 0
            if case_index == 0 and row_index == 2:
                chart_offset = 4.5
            elif case_index == 0 and row_index == 3:
                chart_offset = -4.5
            guide_color = "#009E73" if method == "Event-driven" else "#8E8E8E"
            drawing.add(
                Line(
                    response_x,
                    response_y,
                    response_x,
                    row_y,
                    strokeColor=guide_color,
                    strokeWidth=0.7 if method == "Event-driven" else 0.5,
                    strokeDashArray=[2, 2],
                )
            )
            draw_response_marker(
                drawing,
                response_x,
                response_y + chart_offset,
                state,
                size=3.8,
            )
            draw_response_marker(drawing, response_x, row_y, state, size=3.4)
            delay_minutes = (timestamp - evidence).total_seconds() / 60
            delay_text = (
                f"{delay_minutes:.1f} min"
                if delay_minutes < 10
                else (
                    f"{delay_minutes:.0f} min"
                    if delay_minutes < 120
                    else f"{delay_minutes / 60:.1f} h"
                )
            )
            label_on_left = delay_minutes >= 180
            add_text(
                drawing,
                response_x - 7 if label_on_left else response_x + 7,
                row_y - 2.7,
                f"{RESPONSE_STATE_LABEL[state]} ({delay_text})",
                size=8,
                anchor="end" if label_on_left else "start",
                color="#202020",
            )

        end_time, end_return = points[-1]
        end_x, end_y = x_map(end_time), y_map(end_return)
        drawing.add(Circle(end_x, end_y, 2.2, fillColor="#202020", strokeColor=None))
        label_y = chart_top - 12
        drawing.add(
            Rect(
                plot_right - 88,
                label_y - 3,
                87,
                13,
                fillColor="#FFFFFF",
                strokeColor=None,
            )
        )
        add_text(
            drawing,
            plot_right - 3,
            label_y,
            f"6 h return: {end_return:+.2f}%",
            size=8.2,
            anchor="end",
            bold=True,
        )
        if case_index == 1:
            add_text(
                drawing,
                (plot_left + plot_right) / 2,
                axis_y - 26,
                "Hours since evidence visibility",
                size=8.3,
                anchor="middle",
            )
    save_drawing(drawing, "event_driven_response_cases")


def write_exclusion_log() -> None:
    rows = [
        {
            "metric": "analysis runtime and successful tokens",
            "record_id": task_id,
            "reason": reason,
        }
        for task_id, reason in RUNTIME_EXCLUSIONS.items()
    ] + [
        {
            "metric": "evidence-to-emission delay",
            "record_id": unit_id,
            "reason": reason,
        }
        for unit_id, reason in DELAY_EXCLUSIONS.items()
    ]
    with (OUT_DIR / "metric_exclusions.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    cost_rows = write_cost_metrics()
    delay_rows = write_delay_points()
    write_exclusion_log()
    plot_delay(delay_rows)
    plot_response_cases()
    event_row = next(row for row in cost_rows if row["variant"] == "Event-driven")
    print(
        "Event-driven cleaned cost: "
        f"n={event_row['included_successful_analyses']}, "
        f"mean={event_row['average_analysis_seconds']:.1f}s, "
        f"total={event_row['total_analysis_hours']:.2f}h, "
        f"tokens={event_row['successful_tokens_millions']:.2f}M"
    )
    print(f"Cleaned delay points: {len(delay_rows)}")


if __name__ == "__main__":
    main()
