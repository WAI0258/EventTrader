"""Shared lifecycle-aware planner for replay residue repair boundaries."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from event_trader.operator.repair.artifacts import JsonlRecord, json_value_mentions, read_jsonl_records
from event_trader.operator.repair.contracts import (
    OperatorRepairError,
    RepairAction,
    RepairBlocker,
    RepairBoundaryKind,
    RepairBoundarySummary,
    RepairLifecycleStage,
    RepairPlan,
    RepairScope,
    RepairStageCount,
    RepairTouchedArtifact,
)
from event_trader.storage import WorkspaceLayout

_TOKEN_FIELDS: tuple[str, ...] = (
    "analysis_assessment_id",
    "analysis_outcome_record_id",
    "analysis_unit_id",
    "candidate_anchor_id",
    "candidate_review_anchor_id",
    "context_packet_id",
    "decision_episode_id",
    "decision_id",
    "episode_id",
    "execution_record_id",
    "failure_id",
    "obligation_id",
    "pm_decision_id",
    "pm_review_request_id",
    "read_id",
    "receipt_id",
    "request_id",
    "resolution_id",
    "source_execution_record_id",
    "source_pm_decision_id",
    "source_record_id",
    "source_state_change_id",
    "state_change_id",
)
_LIST_TOKEN_FIELDS: tuple[str, ...] = (
    "event_ids",
    "research_memory_write_receipt_ids",
    "source_event_ids",
)
_STAGE_ORDER: tuple[RepairLifecycleStage, ...] = (
    "orchestration_residue",
    "analysis_truth",
    "pm_execution_truth",
    "reflection_truth",
    "projection",
    "unknown",
)


@dataclass(frozen=True, slots=True)
class LifecycleRepairBoundary:
    scope: RepairScope
    target_key: str
    boundary_kind: RepairBoundaryKind
    event_id: str | None = None
    run_key: str | None = None
    run_id: str | None = None
    repair_replay_at: datetime | None = None
    preserved_event_ids: tuple[str, ...] = ()
    affected_event_ids: tuple[str, ...] = ()
    initial_actions: tuple[RepairAction, ...] = ()
    replay_context_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if self.repair_replay_at is None:
            return
        if (
            self.repair_replay_at.tzinfo is None
            or self.repair_replay_at.utcoffset() is None
        ):
            raise OperatorRepairError("repair_replay_at must be timezone-aware.")
        object.__setattr__(
            self,
            "repair_replay_at",
            self.repair_replay_at.astimezone(UTC),
        )


@dataclass(frozen=True, slots=True)
class LifecyclePlannerResult:
    plan: RepairPlan
    event_ids: tuple[str, ...]
    analysis_unit_ids: tuple[str, ...]
    decision_episode_ids: tuple[str, ...]
    context_packet_ids: tuple[str, ...]


@dataclass(slots=True)
class _RepairTokens:
    event_ids: set[str]
    analysis_unit_ids: set[str]
    decision_episode_ids: set[str]
    context_packet_ids: set[str]
    related_ids: set[str]

    def all(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    *self.event_ids,
                    *self.analysis_unit_ids,
                    *self.decision_episode_ids,
                    *self.context_packet_ids,
                    *self.related_ids,
                }
            )
        )


def build_lifecycle_repair_plan(
    *,
    layout: WorkspaceLayout,
    boundary: LifecycleRepairBoundary,
) -> LifecyclePlannerResult:
    if not isinstance(layout, WorkspaceLayout):
        raise OperatorRepairError("layout must be a WorkspaceLayout instance.")
    tokens = _RepairTokens(
        event_ids=set(boundary.affected_event_ids),
        analysis_unit_ids=set(),
        decision_episode_ids=set(),
        context_packet_ids=set(),
        related_ids=set(),
    )
    candidate_paths = _candidate_record_paths(layout=layout, boundary=boundary)
    _expand_tokens(
        layout=layout,
        target_key=boundary.target_key,
        candidate_paths=candidate_paths,
        tokens=tokens,
    )

    stage_counts: Counter[RepairLifecycleStage] = Counter()
    actions: list[RepairAction] = []
    blocked_artifacts: list[RepairTouchedArtifact] = []
    rebuildable_artifacts: list[RepairTouchedArtifact] = []

    for action in boundary.initial_actions:
        actions.append(action)
        stage_counts[_classify_stage(layout=layout, target_key=boundary.target_key, path=action.path)] += 1

    touched_by_path: set[Path] = {action.path.resolve(strict=False) for action in boundary.initial_actions}
    needles = tokens.all()
    for path in candidate_paths:
        records = _read_structured_records(path)
        matched_lines = tuple(
            record.line_number
            for record in records
            if _target_matches(record.payload, boundary.target_key)
            and _record_mentions_tokens(record.payload, needles)
        )
        if not matched_lines:
            continue
        resolved = path.resolve(strict=False)
        stage = _classify_stage(layout=layout, target_key=boundary.target_key, path=resolved)
        observation = RepairTouchedArtifact(
            path=resolved,
            stage=stage,
            record_count=len(matched_lines),
            line_numbers=matched_lines,
        )
        if resolved not in touched_by_path:
            stage_counts[stage] += 1
            touched_by_path.add(resolved)
        if stage == "orchestration_residue":
            actions.append(
                RepairAction(
                    kind="remove_jsonl_records",
                    path=resolved,
                    record_count=len(matched_lines),
                    line_numbers=matched_lines,
                )
            )
        elif stage == "projection":
            rebuildable_artifacts.append(observation)
        else:
            blocked_artifacts.append(observation)

    for directory in _analysis_staging_dirs(
        layout=layout,
        target_key=boundary.target_key,
        needles=needles,
    ):
        resolved = directory.resolve(strict=False)
        if resolved not in touched_by_path:
            stage_counts["orchestration_residue"] += 1
            touched_by_path.add(resolved)
        actions.append(RepairAction(kind="delete_directory", path=resolved))

    for path in _projection_inventory(layout=layout, target_key=boundary.target_key):
        resolved = path.resolve(strict=False)
        if resolved in touched_by_path:
            continue
        touched_by_path.add(resolved)
        stage_counts["projection"] += 1
        rebuildable_artifacts.append(
            RepairTouchedArtifact(path=resolved, stage="projection")
        )

    merged_actions = _merge_actions(actions)
    blockers = tuple(
        RepairBlocker(reason=_blocker_reason(item.stage), path=item.path)
        for item in blocked_artifacts
    )
    plan = RepairPlan(
        scope=boundary.scope,
        status="blocked" if blockers else "dry_run",
        target_key=boundary.target_key,
        event_id=boundary.event_id,
        run_key=boundary.run_key,
        run_id=boundary.run_id,
        actions=merged_actions,
        blockers=blockers,
        boundary_summary=RepairBoundarySummary(
            kind=boundary.boundary_kind,
            repair_replay_at=boundary.repair_replay_at,
            preserved_event_ids=tuple(sorted(boundary.preserved_event_ids)),
            affected_event_ids=tuple(sorted(boundary.affected_event_ids)),
        ),
        touched_stage_counts=tuple(
            RepairStageCount(stage=stage, artifact_count=stage_counts[stage])
            for stage in _STAGE_ORDER
            if stage_counts[stage]
        ),
        blocked_artifacts=tuple(blocked_artifacts),
        rebuildable_artifacts=tuple(rebuildable_artifacts),
    )
    return LifecyclePlannerResult(
        plan=plan,
        event_ids=tuple(sorted(tokens.event_ids)),
        analysis_unit_ids=tuple(sorted(tokens.analysis_unit_ids)),
        decision_episode_ids=tuple(sorted(tokens.decision_episode_ids)),
        context_packet_ids=tuple(sorted(tokens.context_packet_ids)),
    )


def _expand_tokens(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    candidate_paths: tuple[Path, ...],
    tokens: _RepairTokens,
) -> None:
    previous_size = -1
    while previous_size != len(tokens.all()):
        previous_size = len(tokens.all())
        needles = tokens.all()
        if not needles:
            return
        for path in candidate_paths:
            for record in _read_structured_records(path):
                payload = record.payload
                if not _target_matches(payload, target_key):
                    continue
                if not _record_mentions_tokens(payload, needles):
                    continue
                _collect_tokens_from_payload(payload, tokens)


def _candidate_record_paths(
    *,
    layout: WorkspaceLayout,
    boundary: LifecycleRepairBoundary,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    target_key = boundary.target_key
    runtime_root = layout.runtime_root

    paths.extend(_structured_paths_under(runtime_root / "checker_decisions" / target_key))
    paths.extend(_structured_paths_under(runtime_root / "ceau" / target_key))
    paths.extend(
        _structured_paths_under(
            runtime_root / "context_packets" / "live" / "analysis" / target_key
        )
    )
    paths.extend(_structured_paths_under(runtime_root / "research_memory_writes" / target_key))
    paths.extend(_structured_paths_under(runtime_root / "analysis_assessments" / target_key))
    paths.extend(_structured_paths_under(runtime_root / "thesis_revisions" / target_key))
    paths.extend(_structured_paths_under(runtime_root / "analysis_outcomes" / target_key))
    paths.extend(_structured_paths_under(runtime_root / "analysis_commits" / target_key))
    paths.extend(_structured_paths_under(runtime_root / "decision_episodes" / target_key))
    paths.extend(_structured_paths_under(runtime_root / "portfolio" / "pm-decisions" / target_key))
    paths.extend(_structured_paths_under(runtime_root / "execution" / "intents" / target_key))
    paths.extend(_structured_paths_under(runtime_root / "execution" / "records" / target_key))
    paths.extend(_structured_paths_under(runtime_root / "validation" / "state-changes" / target_key))

    portfolio_state_file = runtime_root / "portfolio" / "state" / f"{target_key}.json"
    if portfolio_state_file.exists():
        paths.append(portfolio_state_file.resolve(strict=False))

    pm_review_root = runtime_root / "pm_review"
    if pm_review_root.exists():
        for child in sorted(item for item in pm_review_root.iterdir() if item.is_dir()):
            paths.extend(_structured_paths_under(child / target_key))

    for root in boundary.replay_context_roots:
        paths.extend(_structured_paths_under(root))

    target_memory_root = layout.targets_root / target_key
    for filename in (
        "reflection_obligations.jsonl",
        "reflection_obligation_resolutions.jsonl",
    ):
        path = target_memory_root / filename
        if path.exists():
            paths.append(path.resolve(strict=False))

    return tuple(dict.fromkeys(paths))


def _structured_paths_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        if root.suffix in {".json", ".jsonl"}:
            return [root.resolve(strict=False)]
        return []
    return [
        path.resolve(strict=False)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".json", ".jsonl"}
    ]


def _read_structured_records(path: Path) -> tuple[JsonlRecord, ...]:
    if path.suffix == ".jsonl":
        return read_jsonl_records(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OperatorRepairError(f"Invalid JSON at {path}") from exc
    if not isinstance(payload, dict):
        raise OperatorRepairError(f"JSON payload must be an object at {path}")
    return (
        JsonlRecord(
            path=path.resolve(strict=False),
            line_number=1,
            line="",
            payload=payload,
        ),
    )


def _collect_tokens_from_payload(payload: dict[str, object], tokens: _RepairTokens) -> None:
    raw_event_ids = payload.get("event_ids")
    if isinstance(raw_event_ids, list):
        for raw_value in raw_event_ids:
            text = _text(raw_value)
            if text is not None:
                tokens.event_ids.add(text)
    for field_name in _TOKEN_FIELDS:
        _add_token(payload.get(field_name), tokens)
    for field_name in _LIST_TOKEN_FIELDS:
        raw_values = payload.get(field_name)
        if not isinstance(raw_values, list):
            continue
        for raw_value in raw_values:
            _add_token(raw_value, tokens)
    analysis_unit_id = _text(payload.get("analysis_unit_id"))
    if analysis_unit_id is not None:
        tokens.analysis_unit_ids.add(analysis_unit_id)
    context_packet_id = _text(payload.get("context_packet_id"))
    if context_packet_id is not None:
        tokens.context_packet_ids.add(context_packet_id)
    decision_episode_id = _text(payload.get("decision_episode_id"))
    if decision_episode_id is not None:
        tokens.decision_episode_ids.add(decision_episode_id)


def _add_token(value: object, tokens: _RepairTokens) -> None:
    text = _text(value)
    if text is not None:
        tokens.related_ids.add(text)


def _analysis_staging_dirs(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    needles: tuple[str, ...],
) -> tuple[Path, ...]:
    root = layout.runtime_root / "analysis_staging"
    if not root.exists() or not needles:
        return ()
    return tuple(
        path.resolve(strict=False)
        for path in sorted(item for item in root.iterdir() if item.is_dir())
        if target_key in path.name and any(needle in path.name for needle in needles)
    )


def _projection_inventory(
    *,
    layout: WorkspaceLayout,
    target_key: str,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    current_state = (
        layout.runtime_root
        / "projection"
        / "current-state"
        / f"{target_key}.md"
    )
    if current_state.exists():
        paths.append(current_state.resolve(strict=False))
    evaluation_root = layout.runtime_root / "evaluation"
    if evaluation_root.exists():
        for path in sorted(item for item in evaluation_root.rglob("*") if item.is_file()):
            relative_parts = path.resolve(strict=False).relative_to(
                evaluation_root.resolve(strict=False)
            ).parts
            if target_key in relative_parts:
                paths.append(path.resolve(strict=False))
    return tuple(dict.fromkeys(paths))


def _record_mentions_tokens(payload: dict[str, object], needles: tuple[str, ...]) -> bool:
    return bool(needles) and any(json_value_mentions(payload, needle) for needle in needles)


def _target_matches(payload: dict[str, object], target_key: str) -> bool:
    payload_target = payload.get("target_key")
    return payload_target is None or payload_target == target_key


def _classify_stage(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    path: Path,
) -> RepairLifecycleStage:
    try:
        normalized = path.resolve(strict=False).relative_to(
            layout.root.resolve(strict=False)
        ).as_posix()
    except ValueError:
        return "unknown"
    if normalized.startswith("runtime/replay_run/"):
        return "orchestration_residue"
    if normalized.startswith("runtime/checker_decisions/"):
        return "orchestration_residue"
    if normalized.startswith("runtime/ceau/"):
        return "orchestration_residue"
    if normalized.startswith("runtime/context_packets/replay/"):
        return "orchestration_residue"
    if normalized.startswith("runtime/analysis_staging/"):
        return "orchestration_residue"
    if normalized.startswith("runtime/context_packets/live/analysis/"):
        return "analysis_truth"
    if normalized.startswith("runtime/research_memory_writes/"):
        return "analysis_truth"
    if normalized.startswith("runtime/analysis_assessments/"):
        return "analysis_truth"
    if normalized.startswith("runtime/thesis_revisions/"):
        return "analysis_truth"
    if normalized.startswith("runtime/analysis_outcomes/"):
        return "analysis_truth"
    if normalized.startswith("runtime/analysis_commits/"):
        return "analysis_truth"
    if normalized.startswith("runtime/decision_episodes/"):
        return "analysis_truth"
    if normalized.startswith("runtime/pm_review/"):
        return "pm_execution_truth"
    if normalized.startswith("runtime/portfolio/pm-decisions/"):
        return "pm_execution_truth"
    if normalized.startswith("runtime/execution/intents/"):
        return "pm_execution_truth"
    if normalized.startswith("runtime/execution/records/"):
        return "pm_execution_truth"
    if normalized.startswith("runtime/portfolio/state/"):
        return "pm_execution_truth"
    if normalized.startswith("runtime/validation/state-changes/"):
        return "pm_execution_truth"
    if normalized == f"research_memory/targets/{target_key}/reflection_obligations.jsonl":
        return "reflection_truth"
    if (
        normalized
        == f"research_memory/targets/{target_key}/reflection_obligation_resolutions.jsonl"
    ):
        return "reflection_truth"
    if normalized.startswith("runtime/projection/"):
        return "projection"
    if normalized.startswith("runtime/evaluation/"):
        return "projection"
    return "unknown"


def _merge_actions(actions: list[RepairAction]) -> tuple[RepairAction, ...]:
    line_actions: dict[Path, set[int]] = {}
    directory_actions: dict[Path, RepairAction] = {}
    for action in actions:
        if action.kind == "remove_jsonl_records":
            line_actions.setdefault(action.path, set()).update(action.line_numbers)
            continue
        directory_actions[action.path] = action
    merged = [
        RepairAction(
            kind="remove_jsonl_records",
            path=path,
            record_count=len(line_numbers),
            line_numbers=tuple(sorted(line_numbers)),
        )
        for path, line_numbers in sorted(line_actions.items())
        if line_numbers
    ]
    merged.extend(directory_actions[path] for path in sorted(directory_actions))
    return tuple(merged)


def _blocker_reason(stage: RepairLifecycleStage) -> str:
    if stage == "analysis_truth":
        return (
            "generic repair cannot mutate analysis-owned durable truth; "
            "run runtime_repair scan-analysis-candidates for the analysis "
            "recovery report"
        )
    if stage == "pm_execution_truth":
        return (
            "generic repair cannot mutate PM/execution durable truth; "
            "run runtime_repair scan-pm-execution-recovery for the "
            "PM/execution recovery report"
        )
    if stage == "reflection_truth":
        return (
            "generic repair cannot mutate reflection durable truth; "
            "run runtime_repair scan-reflection-recovery for the reflection "
            "recovery report"
        )
    if stage == "unknown":
        return "generic repair touched an unmapped artifact outside replay residue rules"
    raise OperatorRepairError(f"Unexpected blocked lifecycle stage: {stage}")


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
