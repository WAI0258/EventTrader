"""Replay wall-time and LLM-token cost report over existing runtime artifacts."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from event_trader.contracts import LLMUsageReceipt, RuntimeContractError
from event_trader.contracts._validators import validate_target_key
from event_trader.replay.checkpoint import replay_checkpoint_path
from event_trader.storage import WorkspaceLayout

_REPORT_DIR_NAME = "replay_cost"
_USAGE_RE = re.compile(
    r"Total Input:\s*(?P<input>\d+),\s*"
    r"Cache Creation:\s*(?P<cache_creation>\d+),\s*"
    r"Cache Read:\s*(?P<cache_read>\d+),\s*"
    r"Output:\s*(?P<output>\d+)"
)
_STEP_USAGE_RE = re.compile(
    r"Input:\s*(?P<input>\d+),\s*"
    r"Cache:\s*(?P<cache_creation>\d+)\+(?P<cache_read>\d+),\s*"
    r"Output:\s*(?P<output>\d+)"
)


class ReplayCostReportError(ValueError):
    """Raised when replay cost report inputs or payloads are malformed."""


@dataclass(frozen=True, slots=True)
class TokenUsageSummary:
    record_count: int
    provider_reported_count: int
    unavailable_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def to_json_payload(self) -> dict[str, object]:
        return {
            "record_count": self.record_count,
            "provider_reported_count": self.provider_reported_count,
            "unavailable_count": self.unavailable_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class TimingSummary:
    count: int
    total_seconds: float
    average_seconds: float
    p50_seconds: float
    p90_seconds: float
    max_seconds: float

    def to_json_payload(self) -> dict[str, object]:
        return {
            "count": self.count,
            "total_seconds": round(self.total_seconds, 3),
            "average_seconds": round(self.average_seconds, 3),
            "p50_seconds": round(self.p50_seconds, 3),
            "p90_seconds": round(self.p90_seconds, 3),
            "max_seconds": round(self.max_seconds, 3),
        }


@dataclass(frozen=True, slots=True)
class ReplayMonthSummary:
    month: str
    completed_event_count: int
    active_pipeline_timing: TimingSummary
    checker_llm_usage: TokenUsageSummary
    analysis_llm_usage: TokenUsageSummary

    def to_json_payload(self) -> dict[str, object]:
        return {
            "month": self.month,
            "completed_event_count": self.completed_event_count,
            "active_pipeline_timing": self.active_pipeline_timing.to_json_payload(),
            "checker_llm_usage": self.checker_llm_usage.to_json_payload(),
            "analysis_llm_usage": self.analysis_llm_usage.to_json_payload(),
        }


@dataclass(frozen=True, slots=True)
class MiroThinkerTaskSummary:
    agent_role: str
    task_count: int
    status_counts: Mapping[str, int]
    timing: TimingSummary
    llm_call_count: int
    final_usage_reported_count: int
    final_input_tokens: int
    final_output_tokens: int
    final_non_cache_tokens: int
    final_cache_creation_tokens: int
    final_cache_read_tokens: int
    step_usage_call_count: int
    step_input_tokens: int
    step_output_tokens: int
    step_cache_creation_tokens: int
    step_cache_read_tokens: int

    def to_json_payload(self) -> dict[str, object]:
        return {
            "agent_role": self.agent_role,
            "task_count": self.task_count,
            "status_counts": dict(self.status_counts),
            "timing": self.timing.to_json_payload(),
            "llm_call_count": self.llm_call_count,
            "final_usage_reported_count": self.final_usage_reported_count,
            "final_input_tokens": self.final_input_tokens,
            "final_output_tokens": self.final_output_tokens,
            "final_non_cache_tokens": self.final_non_cache_tokens,
            "final_cache_creation_tokens": self.final_cache_creation_tokens,
            "final_cache_read_tokens": self.final_cache_read_tokens,
            "step_usage_call_count": self.step_usage_call_count,
            "step_input_tokens": self.step_input_tokens,
            "step_output_tokens": self.step_output_tokens,
            "step_cache_creation_tokens": self.step_cache_creation_tokens,
            "step_cache_read_tokens": self.step_cache_read_tokens,
        }


@dataclass(frozen=True, slots=True)
class ReplayCostModuleBreakdown:
    module_name: str
    data_source: str
    runtime_usage: TokenUsageSummary | None = None
    task_log_summary: MiroThinkerTaskSummary | None = None

    @property
    def item_count(self) -> int:
        if self.runtime_usage is not None:
            return self.runtime_usage.record_count
        if self.task_log_summary is not None:
            return self.task_log_summary.task_count
        return 0

    @property
    def total_seconds(self) -> float:
        if self.task_log_summary is None:
            return 0.0
        return self.task_log_summary.timing.total_seconds

    @property
    def total_token_count(self) -> int:
        if self.runtime_usage is not None:
            return self.runtime_usage.total_tokens
        if self.task_log_summary is not None:
            return self.task_log_summary.final_non_cache_tokens
        return 0

    @property
    def token_basis(self) -> str:
        if self.runtime_usage is not None:
            return "provider_total_tokens"
        if self.task_log_summary is not None:
            return "final_non_cache_tokens"
        return "none"

    def to_json_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "module_name": self.module_name,
            "data_source": self.data_source,
            "item_count": self.item_count,
            "total_seconds": round(self.total_seconds, 3),
            "total_token_count": self.total_token_count,
            "token_basis": self.token_basis,
        }
        if self.runtime_usage is not None:
            payload["runtime_usage"] = self.runtime_usage.to_json_payload()
        if self.task_log_summary is not None:
            payload["task_log_summary"] = self.task_log_summary.to_json_payload()
        return payload


@dataclass(frozen=True, slots=True)
class SlowTask:
    agent_role: str
    task_id: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float
    final_non_cache_tokens: int
    llm_call_count: int
    path: Path

    def to_json_payload(self) -> dict[str, object]:
        return {
            "agent_role": self.agent_role,
            "task_id": self.task_id,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "ended_at": None if self.ended_at is None else self.ended_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 3),
            "final_non_cache_tokens": self.final_non_cache_tokens,
            "llm_call_count": self.llm_call_count,
            "path": str(self.path),
        }


@dataclass(frozen=True, slots=True)
class ReplayCostReport:
    report_id: str
    run_id: str
    target_key: str
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    replay_wall_started_at: datetime | None
    replay_wall_finished_at: datetime | None
    replay_wall_span_seconds: float
    completed_event_count: int
    active_pipeline_timing: TimingSummary
    module_breakdown: tuple[ReplayCostModuleBreakdown, ...]
    runtime_total_llm_usage: TokenUsageSummary
    by_replay_month: tuple[ReplayMonthSummary, ...]
    slowest_tasks: tuple[SlowTask, ...]
    warnings: tuple[str, ...]
    json_path: Path | None = None
    markdown_path: Path | None = None

    def to_json_payload(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "run_id": self.run_id,
            "target_key": self.target_key,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "replay_wall_started_at": (
                None
                if self.replay_wall_started_at is None
                else self.replay_wall_started_at.isoformat()
            ),
            "replay_wall_finished_at": (
                None
                if self.replay_wall_finished_at is None
                else self.replay_wall_finished_at.isoformat()
            ),
            "replay_wall_span_seconds": round(self.replay_wall_span_seconds, 3),
            "completed_event_count": self.completed_event_count,
            "active_pipeline_timing": self.active_pipeline_timing.to_json_payload(),
            "module_breakdown": [item.to_json_payload() for item in self.module_breakdown],
            "runtime_total_llm_usage": self.runtime_total_llm_usage.to_json_payload(),
            "by_replay_month": [item.to_json_payload() for item in self.by_replay_month],
            "slowest_tasks": [item.to_json_payload() for item in self.slowest_tasks],
            "warnings": list(self.warnings),
            "json_path": None if self.json_path is None else str(self.json_path),
            "markdown_path": None if self.markdown_path is None else str(self.markdown_path),
        }


@dataclass(frozen=True, slots=True)
class PersistedReplayCostReport:
    report: ReplayCostReport
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True, slots=True)
class _RuntimeRecord:
    payload: Mapping[str, object]
    business_at: datetime


@dataclass(frozen=True, slots=True)
class _TaskLogRecord:
    agent_role: str
    task_id: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float
    llm_call_count: int
    final_input_tokens: int
    final_output_tokens: int
    final_cache_creation_tokens: int
    final_cache_read_tokens: int
    step_input_tokens: int
    step_output_tokens: int
    step_cache_creation_tokens: int
    step_cache_read_tokens: int
    step_usage_call_count: int
    path: Path

    @property
    def final_non_cache_tokens(self) -> int:
        return self.final_input_tokens + self.final_output_tokens


def build_replay_cost_report(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    target_key: str,
    window_start: datetime,
    window_end: datetime,
    pm_review_log_dir: Path,
    reflection_log_dir: Path,
    generated_at: datetime | None = None,
) -> ReplayCostReport:
    if not isinstance(layout, WorkspaceLayout):
        raise ReplayCostReportError("layout must be a WorkspaceLayout instance.")
    normalized_run_id = _non_blank(run_id, "run_id")
    normalized_target_key = validate_target_key(target_key, error_type=ReplayCostReportError)
    start = _normalize_datetime(window_start, "window_start")
    end = _normalize_datetime(window_end, "window_end")
    if end < start:
        raise ReplayCostReportError("window_end must be greater than or equal to window_start.")
    report_generated_at = (generated_at or datetime.now(UTC)).astimezone(UTC)

    checkpoints, checkpoint_run_key_fallback_used = _read_replay_checkpoints(
        layout=layout,
        run_id=normalized_run_id,
        target_key=normalized_target_key,
        window_start=start,
        window_end=end,
    )
    completed_checkpoints = [item for item in checkpoints if item["status"] == "completed"]
    wall_times = [_require_datetime(item["recorded_at"], "recorded_at") for item in checkpoints]
    wall_started = min(wall_times) if wall_times else None
    wall_finished = max(wall_times) if wall_times else None
    wall_span = 0.0
    if wall_started is not None and wall_finished is not None:
        wall_span = max(0.0, (wall_finished - wall_started).total_seconds())

    event_durations = _event_pipeline_durations(checkpoints)
    checker_records = _read_runtime_records(
        layout.runtime_root / "checker_decisions" / normalized_target_key,
        timestamp_field="business_at",
        window_start=start,
        window_end=end,
    )
    analysis_records = _read_runtime_records(
        layout.runtime_root / "analysis_outcomes" / normalized_target_key,
        timestamp_field="business_at",
        window_start=start,
        window_end=end,
    )
    checker_usage = _summarize_runtime_usage(checker_records, role="checker")
    analysis_usage = _summarize_runtime_usage(analysis_records, role="analysis")
    total_usage = _sum_token_usage(checker_usage, analysis_usage)

    log_window_start = wall_started
    log_window_end = wall_finished
    pm_review_tasks = _read_task_logs(
        log_dir=pm_review_log_dir,
        module_name="pm_review",
        target_key=normalized_target_key,
        wall_start=log_window_start,
        wall_end=log_window_end,
        task_matcher=_pm_review_task_matches_target,
    )
    reflection_tasks = _read_task_logs(
        log_dir=reflection_log_dir,
        module_name="reflection",
        target_key=normalized_target_key,
        wall_start=log_window_start,
        wall_end=log_window_end,
        task_matcher=_reflection_task_matches_target,
    )
    module_breakdown = (
        ReplayCostModuleBreakdown(
            module_name="checker",
            data_source="runtime_receipts",
            runtime_usage=checker_usage,
        ),
        ReplayCostModuleBreakdown(
            module_name="analysis",
            data_source="runtime_receipts",
            runtime_usage=analysis_usage,
        ),
        ReplayCostModuleBreakdown(
            module_name="pm_review",
            data_source="mirothinker_task_logs",
            task_log_summary=_summarize_task_logs("pm_review", pm_review_tasks),
        ),
        ReplayCostModuleBreakdown(
            module_name="reflection",
            data_source="mirothinker_task_logs",
            task_log_summary=_summarize_task_logs("reflection", reflection_tasks),
        ),
    )

    months = _build_month_summaries(
        completed_checkpoints=completed_checkpoints,
        event_durations=event_durations,
        checker_records=checker_records,
        analysis_records=analysis_records,
    )
    slowest_tasks = tuple(
        SlowTask(
            agent_role=item.agent_role,
            task_id=item.task_id,
            status=item.status,
            started_at=item.started_at,
            ended_at=item.ended_at,
            duration_seconds=item.duration_seconds,
            final_non_cache_tokens=item.final_non_cache_tokens,
            llm_call_count=item.llm_call_count,
            path=item.path,
        )
        for item in sorted(
            (*pm_review_tasks, *reflection_tasks),
            key=lambda item: item.duration_seconds,
            reverse=True,
        )[:10]
    )

    warning_items = [
        "Checker and analysis usage comes from runtime receipts. Their per-module surface "
        "does not include independent task-log wall time.",
        "PMReview and reflection usage comes from MiroThinker task logs. Their token totals "
        "reflect final non-cache tokens, with cache creation/read tokens reported separately.",
        "PMReview task logs are counted only when task_id carries a target-bearing contract "
        "for the requested target. Legacy PM logs without target-bearing task_id are excluded.",
        "MiroThinker task-log timestamps are timezone-naive; this report interprets them as UTC "
        "and filters them by the replay checkpoint wall span.",
    ]
    if checkpoint_run_key_fallback_used:
        warning_items.append(
            "No replay checkpoints matched run_id exactly; checkpoint timing used "
            "target/window matching instead."
        )
    report_id = (
        f"replay-cost:{normalized_run_id}:{normalized_target_key}:"
        f"{start.date()}:{end.date()}"
    )
    return ReplayCostReport(
        report_id=report_id,
        run_id=normalized_run_id,
        target_key=normalized_target_key,
        window_start=start,
        window_end=end,
        generated_at=report_generated_at,
        replay_wall_started_at=wall_started,
        replay_wall_finished_at=wall_finished,
        replay_wall_span_seconds=wall_span,
        completed_event_count=len(completed_checkpoints),
        active_pipeline_timing=_timing_summary(tuple(event_durations.values())),
        module_breakdown=module_breakdown,
        runtime_total_llm_usage=total_usage,
        by_replay_month=months,
        slowest_tasks=slowest_tasks,
        warnings=tuple(warning_items),
    )


def write_replay_cost_report(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    target_key: str,
    window_start: datetime,
    window_end: datetime,
    pm_review_log_dir: Path,
    reflection_log_dir: Path,
    generated_at: datetime | None = None,
) -> PersistedReplayCostReport:
    report = build_replay_cost_report(
        layout=layout,
        run_id=run_id,
        target_key=target_key,
        window_start=window_start,
        window_end=window_end,
        pm_review_log_dir=pm_review_log_dir,
        reflection_log_dir=reflection_log_dir,
        generated_at=generated_at,
    )
    json_path, markdown_path = replay_cost_report_paths(
        layout,
        run_id=report.run_id,
        target_key=report.target_key,
    )
    persisted_report = replace(report, json_path=json_path, markdown_path=markdown_path)
    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(
                persisted_report.to_json_payload(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(_render_markdown(persisted_report), encoding="utf-8")
    except OSError as exc:
        raise ReplayCostReportError(f"Failed to write replay cost report: {exc}") from exc
    return PersistedReplayCostReport(
        report=persisted_report,
        json_path=json_path,
        markdown_path=markdown_path,
    )


def replay_cost_report_paths(
    layout: WorkspaceLayout,
    *,
    run_id: str,
    target_key: str,
) -> tuple[Path, Path]:
    if not isinstance(layout, WorkspaceLayout):
        raise ReplayCostReportError("layout must be a WorkspaceLayout instance.")
    normalized_run_id = _validate_path_segment(run_id, "run_id")
    normalized_target_key = validate_target_key(target_key, error_type=ReplayCostReportError)
    root = layout.runtime_root / "audit" / _REPORT_DIR_NAME / normalized_run_id
    return (
        (root / f"{normalized_target_key}.json").resolve(strict=False),
        (root / f"{normalized_target_key}.md").resolve(strict=False),
    )


def _read_replay_checkpoints(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    target_key: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[tuple[Mapping[str, object], ...], bool]:
    path = replay_checkpoint_path(layout, target_key)
    if not path.exists():
        return (), False
    exact_rows: list[Mapping[str, object]] = []
    fallback_rows: list[Mapping[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            normalized = line.strip()
            if not normalized:
                continue
            try:
                payload = json.loads(normalized)
            except json.JSONDecodeError as exc:
                raise ReplayCostReportError(
                    f"Replay checkpoint line {line_number} is invalid JSON."
                ) from exc
            if not isinstance(payload, dict):
                raise ReplayCostReportError(
                    f"Replay checkpoint line {line_number} must be a JSON object."
                )
            if payload.get("target_key") != target_key:
                continue
            replay_at = _require_datetime(payload.get("replay_at"), "replay_at")
            if window_start <= replay_at <= window_end:
                fallback_rows.append(payload)
                if payload.get("run_key") == run_id:
                    exact_rows.append(payload)
    if exact_rows:
        return tuple(exact_rows), False
    return tuple(fallback_rows), bool(fallback_rows)


def _event_pipeline_durations(checkpoints: Iterable[Mapping[str, object]]) -> dict[str, float]:
    started_by_event: dict[str, datetime] = {}
    durations: dict[str, float] = {}
    for item in sorted(
        checkpoints,
        key=lambda payload: _require_datetime(payload.get("recorded_at"), "recorded_at"),
    ):
        event_id = _require_text(item.get("event_id"), "event_id")
        status = _require_text(item.get("status"), "status")
        recorded_at = _require_datetime(item.get("recorded_at"), "recorded_at")
        if status == "started":
            started_by_event[event_id] = recorded_at
        elif status == "completed" and event_id in started_by_event:
            durations[event_id] = max(
                0.0,
                (recorded_at - started_by_event[event_id]).total_seconds(),
            )
    return durations


def _read_runtime_records(
    root: Path,
    *,
    timestamp_field: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[_RuntimeRecord, ...]:
    if not root.exists():
        return ()
    records: list[_RuntimeRecord] = []
    for path in sorted(root.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    raise ReplayCostReportError(
                        f"{path} line {line_number} is invalid JSON."
                    ) from exc
                if not isinstance(payload, dict):
                    raise ReplayCostReportError(f"{path} line {line_number} must be an object.")
                business_at = _require_datetime(payload.get(timestamp_field), timestamp_field)
                if window_start <= business_at <= window_end:
                    records.append(_RuntimeRecord(payload=payload, business_at=business_at))
    return tuple(records)


def _summarize_runtime_usage(
    records: Iterable[_RuntimeRecord],
    *,
    role: str,
) -> TokenUsageSummary:
    count = 0
    reported = 0
    unavailable = 0
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    for record in records:
        count += 1
        try:
            usage = LLMUsageReceipt.from_json_payload_optional(record.payload.get("llm_usage"))
        except RuntimeContractError as exc:
            raise ReplayCostReportError(f"{role} llm_usage is malformed: {exc}") from exc
        if usage is None or usage.usage_source == "unavailable":
            unavailable += 1
            continue
        reported += 1
        input_tokens += usage.input_tokens
        output_tokens += usage.output_tokens
        total_tokens += usage.total_tokens
    return TokenUsageSummary(
        record_count=count,
        provider_reported_count=reported,
        unavailable_count=unavailable,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _sum_token_usage(*items: TokenUsageSummary) -> TokenUsageSummary:
    return TokenUsageSummary(
        record_count=sum(item.record_count for item in items),
        provider_reported_count=sum(item.provider_reported_count for item in items),
        unavailable_count=sum(item.unavailable_count for item in items),
        input_tokens=sum(item.input_tokens for item in items),
        output_tokens=sum(item.output_tokens for item in items),
        total_tokens=sum(item.total_tokens for item in items),
    )


def _read_task_logs(
    *,
    log_dir: Path,
    module_name: str,
    target_key: str,
    wall_start: datetime | None,
    wall_end: datetime | None,
    task_matcher: Callable[[str, str], bool],
) -> tuple[_TaskLogRecord, ...]:
    if not log_dir.exists():
        return ()
    records: list[_TaskLogRecord] = []
    for path in sorted(log_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ReplayCostReportError(f"MiroThinker task log is invalid JSON: {path}") from exc
        except OSError as exc:
            raise ReplayCostReportError(
                f"Failed to read MiroThinker task log {path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            continue
        task_id = str(payload.get("task_id") or "")
        if not task_matcher(task_id, target_key):
            continue
        started_at = _parse_miro_time(payload.get("start_time"))
        if started_at is None:
            continue
        if wall_start is not None and started_at < wall_start:
            continue
        if wall_end is not None and started_at > wall_end:
            continue
        ended_at = _parse_miro_time(payload.get("end_time"))
        duration = max(0.0, (ended_at - started_at).total_seconds()) if ended_at else 0.0
        step_logs = payload.get("step_logs")
        steps = step_logs if isinstance(step_logs, list) else []
        final_usage = _extract_final_usage(steps)
        step_usage = _extract_step_usage(steps)
        records.append(
            _TaskLogRecord(
                agent_role=module_name,
                task_id=task_id,
                status=str(payload.get("status") or "unknown"),
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=duration,
                llm_call_count=step_usage["count"],
                final_input_tokens=final_usage["input"],
                final_output_tokens=final_usage["output"],
                final_cache_creation_tokens=final_usage["cache_creation"],
                final_cache_read_tokens=final_usage["cache_read"],
                step_input_tokens=step_usage["input"],
                step_output_tokens=step_usage["output"],
                step_cache_creation_tokens=step_usage["cache_creation"],
                step_cache_read_tokens=step_usage["cache_read"],
                step_usage_call_count=step_usage["count"],
                path=path.resolve(strict=False),
            )
        )
    return tuple(records)


def _pm_review_task_matches_target(task_id: str, target_key: str) -> bool:
    return _task_id_matches_prefix_with_target(
        task_id,
        prefix="pm-review",
        target_key=target_key,
    )


def _reflection_task_matches_target(task_id: str, target_key: str) -> bool:
    prefixes = (
        "event-trader-reflection",
        "event-trader-target-reflection",
        "event-trader-terminal-reflection",
        "event-trader-close-reflection",
    )
    normalized_task_id = task_id.strip()
    return any(
        normalized_task_id.startswith(prefix)
        and _task_id_contains_target_segment(normalized_task_id, target_key)
        for prefix in prefixes
    )


def _task_id_matches_prefix_with_target(task_id: str, *, prefix: str, target_key: str) -> bool:
    normalized_task_id = task_id.strip()
    normalized_target_key = target_key.strip()
    if not normalized_task_id or not normalized_target_key:
        return False
    pattern = re.compile(
        rf"^{re.escape(prefix)}(?:[:_-]){re.escape(normalized_target_key)}(?:[:_-]|$)"
    )
    return pattern.search(normalized_task_id) is not None


def _task_id_contains_target_segment(task_id: str, target_key: str) -> bool:
    normalized_target_key = target_key.strip()
    if not normalized_target_key:
        return False
    pattern = re.compile(rf"(^|[:_-]){re.escape(normalized_target_key)}($|[:_-])")
    return pattern.search(task_id) is not None


def _extract_final_usage(steps: Iterable[object]) -> dict[str, int]:
    usage = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    for item in steps:
        if not isinstance(item, dict):
            continue
        if "Usage Calculation" not in str(item.get("step_name") or ""):
            continue
        match = _USAGE_RE.search(str(item.get("message") or ""))
        if match is None:
            continue
        usage = {key: int(value) for key, value in match.groupdict().items()}
    return usage


def _extract_step_usage(steps: Iterable[object]) -> dict[str, int]:
    usage = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "count": 0}
    for item in steps:
        if not isinstance(item, dict):
            continue
        if "Token Usage" not in str(item.get("step_name") or ""):
            continue
        match = _STEP_USAGE_RE.search(str(item.get("message") or ""))
        if match is None:
            continue
        usage["count"] += 1
        for key, value in match.groupdict().items():
            usage[key] += int(value)
    return usage


def _summarize_task_logs(
    agent_role: str,
    records: tuple[_TaskLogRecord, ...],
) -> MiroThinkerTaskSummary:
    statuses = Counter(record.status for record in records)
    final_reported = sum(1 for item in records if item.final_non_cache_tokens > 0)
    return MiroThinkerTaskSummary(
        agent_role=agent_role,
        task_count=len(records),
        status_counts=dict(sorted(statuses.items())),
        timing=_timing_summary(tuple(item.duration_seconds for item in records)),
        llm_call_count=sum(item.llm_call_count for item in records),
        final_usage_reported_count=final_reported,
        final_input_tokens=sum(item.final_input_tokens for item in records),
        final_output_tokens=sum(item.final_output_tokens for item in records),
        final_non_cache_tokens=sum(item.final_non_cache_tokens for item in records),
        final_cache_creation_tokens=sum(item.final_cache_creation_tokens for item in records),
        final_cache_read_tokens=sum(item.final_cache_read_tokens for item in records),
        step_usage_call_count=sum(item.step_usage_call_count for item in records),
        step_input_tokens=sum(item.step_input_tokens for item in records),
        step_output_tokens=sum(item.step_output_tokens for item in records),
        step_cache_creation_tokens=sum(item.step_cache_creation_tokens for item in records),
        step_cache_read_tokens=sum(item.step_cache_read_tokens for item in records),
    )


def _build_month_summaries(
    *,
    completed_checkpoints: Iterable[Mapping[str, object]],
    event_durations: Mapping[str, float],
    checker_records: tuple[_RuntimeRecord, ...],
    analysis_records: tuple[_RuntimeRecord, ...],
) -> tuple[ReplayMonthSummary, ...]:
    completed_by_month: dict[str, list[str]] = defaultdict(list)
    for item in completed_checkpoints:
        month = _require_datetime(item.get("replay_at"), "replay_at").strftime("%Y-%m")
        completed_by_month[month].append(_require_text(item.get("event_id"), "event_id"))
    checker_by_month: dict[str, list[_RuntimeRecord]] = defaultdict(list)
    analysis_by_month: dict[str, list[_RuntimeRecord]] = defaultdict(list)
    for record in checker_records:
        checker_by_month[record.business_at.strftime("%Y-%m")].append(record)
    for record in analysis_records:
        analysis_by_month[record.business_at.strftime("%Y-%m")].append(record)
    months = sorted(set(completed_by_month) | set(checker_by_month) | set(analysis_by_month))
    summaries: list[ReplayMonthSummary] = []
    for month in months:
        event_ids = completed_by_month.get(month, [])
        durations = tuple(
            event_durations[event_id]
            for event_id in event_ids
            if event_id in event_durations
        )
        summaries.append(
            ReplayMonthSummary(
                month=month,
                completed_event_count=len(event_ids),
                active_pipeline_timing=_timing_summary(durations),
                checker_llm_usage=_summarize_runtime_usage(
                    checker_by_month.get(month, ()),
                    role="checker",
                ),
                analysis_llm_usage=_summarize_runtime_usage(
                    analysis_by_month.get(month, ()),
                    role="analysis",
                ),
            )
        )
    return tuple(summaries)


def _timing_summary(values: tuple[float, ...]) -> TimingSummary:
    if not values:
        return TimingSummary(
            count=0,
            total_seconds=0.0,
            average_seconds=0.0,
            p50_seconds=0.0,
            p90_seconds=0.0,
            max_seconds=0.0,
        )
    ordered = tuple(sorted(max(0.0, value) for value in values))
    total = sum(ordered)
    return TimingSummary(
        count=len(ordered),
        total_seconds=total,
        average_seconds=total / len(ordered),
        p50_seconds=_percentile(ordered, 50),
        p90_seconds=_percentile(ordered, 90),
        max_seconds=ordered[-1],
    )


def _percentile(ordered_values: tuple[float, ...], percentile: int) -> float:
    if not ordered_values:
        return 0.0
    index = round((len(ordered_values) - 1) * (percentile / 100))
    return ordered_values[index]


def _render_markdown(report: ReplayCostReport) -> str:
    runtime_modules = tuple(
        item for item in report.module_breakdown if item.data_source == "runtime_receipts"
    )
    task_log_modules = tuple(
        item for item in report.module_breakdown if item.data_source == "mirothinker_task_logs"
    )
    lines = [
        "# Replay Cost Report",
        "",
        f"- Run id: `{report.run_id}`",
        f"- Target: `{report.target_key}`",
        "- Replay window: "
        f"`{report.window_start.isoformat()}` to `{report.window_end.isoformat()}`",
        f"- Generated at: `{report.generated_at.isoformat()}`",
        "",
        "## Wall Time",
        "",
        f"- Wall span: {_format_seconds(report.replay_wall_span_seconds)}",
        f"- Completed events: {report.completed_event_count}",
        "- Active event pipeline time: "
        f"{_format_seconds(report.active_pipeline_timing.total_seconds)}",
        f"- Event duration avg/p50/p90/max: "
        f"{_format_seconds(report.active_pipeline_timing.average_seconds)} / "
        f"{_format_seconds(report.active_pipeline_timing.p50_seconds)} / "
        f"{_format_seconds(report.active_pipeline_timing.p90_seconds)} / "
        f"{_format_seconds(report.active_pipeline_timing.max_seconds)}",
        "",
        "## Module Breakdown",
        "",
        "| Module | Data Source | Count | Time | Token Basis | Tokens | Detail |",
        "| --- | --- | ---: | ---: | --- | ---: | --- |",
    ]
    for item in report.module_breakdown:
        lines.append(_module_row(item))
    lines.extend(
        [
            "",
            "## Runtime Receipt Modules",
            "",
            "| Module | Records | Reported | Unavailable | Input | Output | Total |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in runtime_modules:
        if item.runtime_usage is None:
            continue
        lines.append(_usage_row(item.module_name, item.runtime_usage))
    lines.extend(
        [
            "",
            "## Task-Log Modules",
            "",
            "| Module | Tasks | Statuses | Wall Time | Avg | P90 | LLM Calls | "
            "Non-cache Tokens | Cache Creation | Cache Read |",
            "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in task_log_modules:
        if item.task_log_summary is None:
            continue
        lines.append(_task_row(item.task_log_summary))
    lines.extend(
        [
            "",
            "## Runtime Receipt Month Breakdown",
            "",
            "| Month | Events | Active Time | Checker Tokens | Analysis Tokens |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in report.by_replay_month:
        lines.append(
            f"| {item.month} | {item.completed_event_count} | "
            f"{_format_seconds(item.active_pipeline_timing.total_seconds)} | "
            f"{item.checker_llm_usage.total_tokens:,} | {item.analysis_llm_usage.total_tokens:,} |"
        )
    lines.extend(
        [
            "",
            "## Slowest Task-Log Tasks",
            "",
            "| Role | Task | Status | Duration | Tokens | Path |",
            "| --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for item in report.slowest_tasks:
        lines.append(
            f"| {item.agent_role} | `{item.task_id}` | {item.status} | "
            f"{_format_seconds(item.duration_seconds)} | {item.final_non_cache_tokens:,} | "
            f"`{item.path.as_posix()}` |"
        )
    lines.extend(["", "## Notes", ""])
    for warning in report.warnings:
        lines.append(f"- {warning}")
    return "\n".join(lines) + "\n"


def _usage_row(label: str, usage: TokenUsageSummary) -> str:
    return (
        f"| {label} | {usage.record_count} | {usage.provider_reported_count} | "
        f"{usage.unavailable_count} | {usage.input_tokens:,} | "
        f"{usage.output_tokens:,} | {usage.total_tokens:,} |"
    )


def _task_row(summary: MiroThinkerTaskSummary) -> str:
    statuses = ", ".join(f"{key}:{value}" for key, value in summary.status_counts.items()) or "-"
    return (
        f"| {summary.agent_role} | {summary.task_count} | {statuses} | "
        f"{_format_seconds(summary.timing.total_seconds)} | "
        f"{_format_seconds(summary.timing.average_seconds)} | "
        f"{_format_seconds(summary.timing.p90_seconds)} | "
        f"{summary.llm_call_count} | {summary.final_non_cache_tokens:,} | "
        f"{summary.final_cache_creation_tokens:,} | {summary.final_cache_read_tokens:,} |"
    )


def _module_row(summary: ReplayCostModuleBreakdown) -> str:
    if summary.runtime_usage is not None:
        detail = (
            f"reported:{summary.runtime_usage.provider_reported_count}, "
            f"unavailable:{summary.runtime_usage.unavailable_count}"
        )
        return (
            f"| {summary.module_name} | {summary.data_source} | {summary.item_count} | - | "
            f"{summary.token_basis} | {summary.total_token_count:,} | {detail} |"
        )
    task_log_summary = summary.task_log_summary
    if task_log_summary is None:
        return (
            f"| {summary.module_name} | {summary.data_source} | 0 | 0s | "
            f"{summary.token_basis} | 0 | - |"
        )
    statuses = ", ".join(
        f"{key}:{value}" for key, value in task_log_summary.status_counts.items()
    ) or "-"
    detail = (
        f"llm_calls:{task_log_summary.llm_call_count}, "
        f"cache_create:{task_log_summary.final_cache_creation_tokens:,}, "
        f"cache_read:{task_log_summary.final_cache_read_tokens:,}, "
        f"statuses:{statuses}"
    )
    return (
        f"| {summary.module_name} | {summary.data_source} | {summary.item_count} | "
        f"{_format_seconds(summary.total_seconds)} | {summary.token_basis} | "
        f"{summary.total_token_count:,} | {detail} |"
    )


def _format_seconds(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(round(seconds % 60))
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _parse_miro_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ReplayCostReportError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReplayCostReportError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


def _require_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return _normalize_datetime(value, field_name)
    raw = _require_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayCostReportError(f"{field_name} must be an ISO8601 timestamp.") from exc
    return _normalize_datetime(parsed, field_name)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayCostReportError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _non_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayCostReportError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _validate_path_segment(value: str, field_name: str) -> str:
    normalized = _non_blank(value, field_name)
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ReplayCostReportError(f"{field_name} must be a path-safe segment.")
    return normalized


__all__ = [
    "MiroThinkerTaskSummary",
    "PersistedReplayCostReport",
    "ReplayCostReport",
    "ReplayCostModuleBreakdown",
    "ReplayCostReportError",
    "ReplayMonthSummary",
    "SlowTask",
    "TimingSummary",
    "TokenUsageSummary",
    "build_replay_cost_report",
    "replay_cost_report_paths",
    "write_replay_cost_report",
]
