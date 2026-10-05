"""Durable analysis outcome receipt helpers for the runtime pipeline."""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from event_trader.analysis import AnalysisContextLoader
from event_trader.ceau import CEAUAnalysisOutcomeReceiptRef, UnitFormationLane
from event_trader.composition_error import CompositionError
from event_trader.contracts import AnalysisResult
from event_trader.decision_memory import (
    DecisionEpisodeRecord,
    FileBackedDecisionEpisodeStore,
)
from event_trader.storage import WorkspaceLayout


def _ensure_analysis_outcome_receipt(
    *,
    layout: WorkspaceLayout,
    context_loader: AnalysisContextLoader,
    request,
    unit_formation_lane: UnitFormationLane | None,
    result: AnalysisResult,
    committed_at: datetime,
) -> CEAUAnalysisOutcomeReceiptRef:
    context = context_loader.load_context(
        request,
        unit_formation_lane=unit_formation_lane,
    )
    matched = _read_matching_analysis_outcome_record(
        layout=layout,
        target_key=result.target_key,
        business_at=context.business_at,
        event_ids=tuple(result.event_ids),
        outcome=result.outcome,
        decision_episode_id=request.decision_episode_id,
    )
    if matched is not None:
        return _analysis_outcome_receipt_ref(*matched)
    return _append_injected_analysis_outcome_receipt(
        layout=layout,
        context=context,
        result=result,
        committed_at=committed_at,
    )


def _read_matching_analysis_outcome_record(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    business_at: datetime,
    event_ids: tuple[str, ...],
    outcome: str,
    decision_episode_id: str,
) -> tuple[Path, dict[str, object], str] | None:
    outcome_path = (
        layout.runtime_root
        / "analysis_outcomes"
        / target_key
        / f"{business_at:%Y-%m}.jsonl"
    )
    if not outcome_path.exists():
        return None
    matched: tuple[dict[str, object], str] | None = None
    for raw_line in outcome_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise CompositionError(
                f"analysis outcome receipt is not valid JSON: {outcome_path}"
            ) from exc
        if not isinstance(payload, dict):
            continue
        if payload.get("target_key") != target_key:
            continue
        if tuple(payload.get("event_ids", ())) != event_ids:
            continue
        if payload.get("outcome") != outcome:
            continue
        if payload.get("decision_episode_id", "") != decision_episode_id:
            continue
        matched = (payload, raw_line)
    if matched is None:
        return None
    payload, raw_line = matched
    return outcome_path, payload, raw_line


def _analysis_outcome_receipt_ref(
    outcome_path: Path,
    payload: dict[str, object],
    raw_line: str,
) -> CEAUAnalysisOutcomeReceiptRef:
    record_id = payload.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise CompositionError("analysis outcome record is missing record_id.")
    return CEAUAnalysisOutcomeReceiptRef(
        analysis_outcome_record_id=record_id,
        analysis_outcome_path=str(outcome_path),
        analysis_outcome_sha256=sha256(raw_line.encode("utf-8")).hexdigest(),
    )


def _append_injected_analysis_outcome_receipt(
    *,
    layout: WorkspaceLayout,
    context,
    result: AnalysisResult,
    committed_at: datetime,
) -> CEAUAnalysisOutcomeReceiptRef:
    outcome_path = (
        layout.runtime_root
        / "analysis_outcomes"
        / result.target_key
        / f"{context.business_at:%Y-%m}.jsonl"
    )
    outcome_path.parent.mkdir(parents=True, exist_ok=True)
    record_id = _injected_analysis_outcome_record_id(
        target_key=result.target_key,
        business_at=context.business_at,
        event_ids=tuple(result.event_ids),
        outcome=result.outcome,
        decision_episode_id=context.request.decision_episode_id,
    )
    payload = {
        "record_id": record_id,
        "target_key": result.target_key,
        "event_ids": list(result.event_ids),
        "business_at": context.business_at.isoformat(),
        "committed_at": committed_at.isoformat(),
        "outcome": result.outcome,
        "context_packet_id": None,
        "context_packet_hash": None,
        "decision_episode_id": context.request.decision_episode_id,
        "analysis_assessment_path": None,
        "analysis_assessment": (
            None
            if result.analysis_assessment is None
            else result.analysis_assessment.to_json_payload()
        ),
        "thesis_revision_id": None,
        "thesis_revision_path": None,
        "pm_review_request_id": None,
        "candidate_review_anchor_id": None,
        "candidate_review_anchor_path": None,
        "candidate_review_anchor": None,
        "pm_review_request_path": None,
        "pm_review_request": None,
        "visible_lesson_ids": [],
        "included_lesson_ids": [],
        "used_lesson_ids": list(result.used_lesson_ids),
        "lesson_diagnostics": {
            "excluded_future_count": 0,
            "excluded_legacy_count": 0,
            "parse_warning_count": 0,
        },
        "material_memory_writes": [],
        "analysis_write_support_diagnostics": {
            "material_write_count": 0,
            "absence_support_payload_count": 0,
            "maturity_support_payload_count": 0,
            "absence_support_used_count": 0,
            "maturity_support_used_count": 0,
            "absence_support_payloads": [],
            "maturity_support_payloads": [],
        },
        "staged_material_write_count": 0,
        "committed_material_write_count": 0,
        "collapsed_material_write_count": 0,
        "research_memory_write_receipt_ids": [],
        "market_context_present": context.market_context is not None,
        "market_context_audit": (
            context.market_context.compact_audit()
            if context.market_context is not None
            else None
        ),
        "market_tool_usage": {
            "tool_call_count": 0,
            "duplicate_tool_call_count": 0,
            "tools_called": [],
            "statuses": [],
            "unavailable_count": 0,
            "partial_count": 0,
            "truncated_count": 0,
            "calls": [],
        },
        "llm_usage": None,
        "why_escalated": context.request.why_escalated,
    }
    raw_line = json.dumps(payload, ensure_ascii=False)
    with outcome_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(raw_line)
        handle.write("\n")
    if context.request.decision_episode_id:
        FileBackedDecisionEpisodeStore(layout).append_record(
            DecisionEpisodeRecord(
                episode_id=context.request.decision_episode_id,
                target_key=result.target_key,
                record_type="analysis",
                business_at=context.business_at,
                recorded_at=committed_at,
                event_ids=tuple(result.event_ids),
                source_record_id=record_id,
                source_path=str(outcome_path),
                source_line=None,
                context_packet_id=None,
                context_packet_hash=None,
                status=result.outcome,
                payload={
                    "outcome": result.outcome,
                    "analysis_assessment_id": (
                        None
                        if result.analysis_assessment is None
                        else result.analysis_assessment.assessment_id
                    ),
                    "research_memory_write_receipt_ids": [],
                    "task_id": "injected_primary_research",
                },
            )
        )
    return _analysis_outcome_receipt_ref(outcome_path, payload, raw_line)


def _injected_analysis_outcome_record_id(
    *,
    target_key: str,
    business_at: datetime,
    event_ids: tuple[str, ...],
    outcome: str,
    decision_episode_id: str,
) -> str:
    payload = json.dumps(
        {
            "target_key": target_key,
            "business_at": business_at.isoformat(),
            "event_ids": list(event_ids),
            "outcome": outcome,
            "decision_episode_id": decision_episode_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"analysis-outcome:injected:{digest}"


def _append_failed_analysis_decision_record(
    *,
    layout: WorkspaceLayout,
    request,
    business_at: datetime,
    error: Exception,
    recorded_at: datetime,
    context_packet_id: str | None = None,
    context_packet_hash: str | None = None,
) -> None:
    FileBackedDecisionEpisodeStore(layout).append_record(
        DecisionEpisodeRecord(
            episode_id=request.decision_episode_id,
            target_key=request.target_key,
            record_type="analysis",
            business_at=business_at,
            recorded_at=recorded_at,
            event_ids=tuple(request.event_ids),
            source_record_id=(
                f"analysis-failed:{request.decision_episode_id}:"
                f"{recorded_at.isoformat()}"
            ),
            source_path=None,
            source_line=None,
            context_packet_id=context_packet_id,
            context_packet_hash=context_packet_hash,
            status="failed",
            payload={
                "error_type": type(error).__name__,
                "error": str(error),
                "why_escalated": request.why_escalated,
            },
        )
    )


def _analysis_failure_business_at(
    *,
    layout: WorkspaceLayout,
    context_loader: AnalysisContextLoader,
    request,
    unit_formation_lane: UnitFormationLane | None = None,
) -> datetime:
    try:
        context = context_loader.load_context(
            request,
            unit_formation_lane=unit_formation_lane,
        )
    except Exception as exc:
        attention_records = tuple(
            persisted.record
            for persisted in FileBackedDecisionEpisodeStore(layout).read_records(
                target_key=request.target_key
            )
            if persisted.record.episode_id == request.decision_episode_id
            and persisted.record.record_type == "attention"
        )
        if attention_records:
            return attention_records[-1].business_at
        raise CompositionError(
            "failed analysis decision record requires evidence business time or "
            "an existing attention decision episode record."
        ) from exc
    return context.business_at


__all__ = [
    "_analysis_failure_business_at",
    "_analysis_outcome_receipt_ref",
    "_append_failed_analysis_decision_record",
    "_append_injected_analysis_outcome_receipt",
    "_ensure_analysis_outcome_receipt",
    "_injected_analysis_outcome_record_id",
    "_read_matching_analysis_outcome_record",
]
