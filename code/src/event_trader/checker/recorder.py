"""Checker runtime outcome execution and auditable decision receipts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from event_trader.checker.context_pack import (
    CheckerContextPack,
    serialize_context_pack,
)
from event_trader.context_assembly import (
    CURRENT_MEMORY_READ_POLICY,
    ContextPacketRuntimeScope,
    FileBackedContextPacketStore,
    build_checker_context_packet,
    validate_context_packet_visibility,
)
from event_trader.contracts import (
    CheckerDecision,
    LLMUsageReceipt,
)
from event_trader.decision_memory import (
    DecisionEpisodeRecord,
    FileBackedDecisionEpisodeStore,
    derive_decision_episode_id,
)
from event_trader.storage import WorkspaceLayout

_CHECKER_DECISION_DIR_NAME = "checker_decisions"
_CHECKER_POLICY_NAME = "single_pass_context_pack"
_CHECKER_POLICY_VERSION = "checker_single_pass_context_pack_2026_05"


class CheckerExecutionError(ValueError):
    """Raised when checker runtime effect wiring is malformed."""


class FileBackedCheckerDecisionRecorder:
    """Append checker decision receipts by evidence business time."""

    def __init__(
        self,
        layout: WorkspaceLayout,
        *,
        memory_read_policy: str = CURRENT_MEMORY_READ_POLICY,
        runtime_scope: ContextPacketRuntimeScope = "live",
        run_id: str = "",
    ) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise CheckerExecutionError("layout must be a WorkspaceLayout instance.")
        self._layout = layout
        self._memory_read_policy = memory_read_policy
        self._runtime_scope = runtime_scope
        self._run_id = run_id

    def append_pack(
        self,
        *,
        pack: CheckerContextPack,
        decision: CheckerDecision,
        caused_analysis_request: bool,
        validator_action: str,
        raw_policy_output: dict[str, object] | None = None,
        llm_usage: LLMUsageReceipt | None = None,
    ) -> Path:
        if not isinstance(pack, CheckerContextPack):
            raise CheckerExecutionError("pack must be a CheckerContextPack instance.")
        if not isinstance(decision, CheckerDecision):
            raise CheckerExecutionError("decision must be a CheckerDecision instance.")
        if not isinstance(caused_analysis_request, bool):
            raise CheckerExecutionError(
                "caused_analysis_request must be a boolean."
            )
        if not isinstance(validator_action, str) or not validator_action.strip():
            raise CheckerExecutionError("validator_action must not be blank.")
        if raw_policy_output is not None and not isinstance(raw_policy_output, dict):
            raise CheckerExecutionError("raw_policy_output must be a dict when present.")
        if llm_usage is not None and not isinstance(llm_usage, LLMUsageReceipt):
            raise CheckerExecutionError("llm_usage must be an LLMUsageReceipt or None.")
        if decision.target_key != pack.request.target_key:
            raise CheckerExecutionError(
                "checker decision target_key must match context pack."
            )
        if decision.event_ids != list(pack.request.event_ids):
            raise CheckerExecutionError(
                "checker decision event_ids must match context pack."
            )

        path = checker_decision_path(
            self._layout,
            target_key=decision.target_key,
            business_at=pack.evidence.ts_event,
        )
        pack_payload = serialize_context_pack(pack)
        context_packet = build_checker_context_packet(
            pack,
            memory_read_policy=self._memory_read_policy,
            runtime_scope=self._runtime_scope,
            run_id=self._run_id,
        )
        context_store = FileBackedContextPacketStore(
            self._layout,
            runtime_scope=self._runtime_scope,
            run_id=self._run_id,
        )
        context_store.append_packet(
            context_packet,
            required_memory_read_policy=(
                self._memory_read_policy
                if self._memory_read_policy != CURRENT_MEMORY_READ_POLICY
                else None
            ),
        )
        context_visibility_audit = validate_context_packet_visibility(
            context_packet,
            required_memory_read_policy=(
                self._memory_read_policy
                if self._memory_read_policy != CURRENT_MEMORY_READ_POLICY
                else None
            ),
            required_runtime_scope=self._runtime_scope,
            required_run_id=(self._run_id if self._runtime_scope == "replay" else None),
        )
        decision_episode_id = (
            derive_decision_episode_id(
                target_key=decision.target_key,
                first_event_id=decision.event_ids[0],
                checker_business_at=pack.evidence.ts_event,
            )
            if decision.decision == "escalate"
            else ""
        )
        if decision_episode_id:
            FileBackedDecisionEpisodeStore(self._layout).append_record(
                DecisionEpisodeRecord(
                    episode_id=decision_episode_id,
                    target_key=decision.target_key,
                    record_type="attention",
                    business_at=pack.evidence.ts_event,
                    recorded_at=pack.evidence.ts_event,
                    event_ids=tuple(decision.event_ids),
                    source_record_id=context_packet.packet_id,
                    source_path=str(path),
                    source_line=None,
                    context_packet_id=context_packet.packet_id,
                    context_packet_hash=context_packet.packet_hash,
                    status="open_attention",
                    payload={
                        "decision": decision.decision,
                        "rationale": decision.rationale,
                        "attention_hint": decision.attention_hint,
                        "requires_watchlist_maintenance": (
                            decision.requires_watchlist_maintenance
                        ),
                        "checker_policy_version": _CHECKER_POLICY_VERSION,
                    },
                )
            )
        payload = {
            "target_key": decision.target_key,
            "event_id": pack.evidence.event_id,
            "source_ref": pack.evidence.source_ref,
            "title": pack.evidence.title,
            "evidence_review_dimensions": (
                pack.evidence.review_dimensions.to_dict()
            ),
            "business_at": pack.evidence.ts_event.isoformat(),
            "recorded_at": datetime.now(UTC).isoformat(),
            "checker_policy": _CHECKER_POLICY_NAME,
            "checker_policy_version": _CHECKER_POLICY_VERSION,
            "context_pack_schema": pack.context_pack_schema,
            "context_pack_hash": pack.context_pack_hash,
            "context_packet_id": context_packet.packet_id,
            "context_packet_hash": context_packet.packet_hash,
            "context_packet_stage": context_packet.stage,
            "context_packet_runtime_scope": context_packet.runtime_scope,
            "context_packet_run_id": context_packet.run_id,
            "decision_episode_id": decision_episode_id,
            "context_visibility_status": context_visibility_audit.status,
            "context_visibility_violation_count": (
                context_visibility_audit.violation_count
            ),
            "coverage": pack_payload["coverage"],
            "page_hashes": pack_payload["page_hashes"],
            "market_context_present": pack.market_context is not None,
            "market_context_audit": (
                pack.market_context.compact_audit()
                if pack.market_context is not None
                else None
            ),
            "decision": decision.decision,
            "rationale": decision.rationale,
            "attention_hint": decision.attention_hint,
            "requires_watchlist_maintenance": decision.requires_watchlist_maintenance,
            "caused_analysis_request": caused_analysis_request,
            "validator_action": validator_action.strip(),
            "llm_usage": (
                None if llm_usage is None else llm_usage.to_json_payload()
            ),
        }
        if raw_policy_output is not None:
            payload["raw_policy_output"] = raw_policy_output
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.write("\n")
        except OSError as exc:
            raise CheckerExecutionError(
                f"Failed to append checker decision receipt {path}: {exc}"
            ) from exc
        return path


def checker_decision_path(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    business_at: datetime,
) -> Path:
    if not isinstance(layout, WorkspaceLayout):
        raise CheckerExecutionError("layout must be a WorkspaceLayout instance.")
    if not isinstance(business_at, datetime):
        raise CheckerExecutionError("business_at must be a datetime.")
    return (
        layout.runtime_root
        / _CHECKER_DECISION_DIR_NAME
        / target_key
        / f"{business_at.strftime('%Y-%m')}.jsonl"
    ).resolve(strict=False)


__all__ = [
    "CheckerExecutionError",
    "FileBackedCheckerDecisionRecorder",
    "checker_decision_path",
]
