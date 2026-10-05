"""Conservative validation for single-pass checker decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from event_trader.checker.context_pack import CheckerContextPack
from event_trader.checker.recorder import CheckerExecutionError
from event_trader.contracts import (
    CheckerDecision,
    LLMUsageReceipt,
    RuntimeContractError,
)

type RawCheckerDecisionValue = Literal["no_action", "escalate"]
type RawCheckerOperationalStatus = Literal["ok", "parse_failure"]
type NoActionSupportType = Literal[
    "duplicate",
    "already_covered",
    "low_relevance",
    "low_materiality",
]
type ValidatorAction = Literal[
    "accepted",
    "forced_escalate",
    "operational_no_action",
]


@dataclass(frozen=True, slots=True)
class NoActionSupport:
    """Structured positive evidence required for a final no_action."""

    support_type: NoActionSupportType
    supporting_sections: tuple[str, ...]
    support_summary: str

    def __post_init__(self) -> None:
        if self.support_type not in {
            "duplicate",
            "already_covered",
            "low_relevance",
            "low_materiality",
        }:
            raise CheckerExecutionError("no_action support_type is invalid.")
        if not isinstance(self.supporting_sections, tuple):
            raise CheckerExecutionError("supporting_sections must be a tuple.")
        if not all(
            isinstance(section, str) and section.strip()
            for section in self.supporting_sections
        ):
            raise CheckerExecutionError("supporting_sections must contain non-blank strings.")
        if not isinstance(self.support_summary, str) or not self.support_summary.strip():
            raise CheckerExecutionError("support_summary must not be blank.")


@dataclass(frozen=True, slots=True)
class RawCheckerDecision:
    """Raw single-pass checker output before conservative validation."""

    target_key: str
    event_ids: list[str]
    decision: RawCheckerDecisionValue
    rationale: str
    attention_hint: str | None
    requires_watchlist_maintenance: bool
    uncertainty: bool
    possible_watchlist_trigger: bool
    no_action_support: NoActionSupport | None
    operational_status: RawCheckerOperationalStatus = "ok"
    operational_detail: str | None = None
    llm_usage: LLMUsageReceipt | None = None

    def __post_init__(self) -> None:
        if self.decision not in {"no_action", "escalate"}:
            raise CheckerExecutionError("decision must be no_action or escalate.")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise CheckerExecutionError("rationale must not be blank.")
        if self.decision == "no_action" and self.attention_hint is not None:
            raise CheckerExecutionError("no_action must have attention_hint=None.")
        if self.decision == "escalate" and (
            not isinstance(self.attention_hint, str) or not self.attention_hint.strip()
        ):
            raise CheckerExecutionError("escalate must include attention_hint.")
        if not isinstance(self.requires_watchlist_maintenance, bool):
            raise CheckerExecutionError("requires_watchlist_maintenance must be boolean.")
        if not isinstance(self.uncertainty, bool):
            raise CheckerExecutionError("uncertainty must be boolean.")
        if not isinstance(self.possible_watchlist_trigger, bool):
            raise CheckerExecutionError("possible_watchlist_trigger must be boolean.")
        if self.operational_status not in {"ok", "parse_failure"}:
            raise CheckerExecutionError("operational_status is invalid.")
        if self.operational_status == "ok":
            if self.operational_detail is not None:
                raise CheckerExecutionError(
                    "operational_detail must be None when operational_status is ok."
                )
        elif (
            not isinstance(self.operational_detail, str)
            or not self.operational_detail.strip()
        ):
            raise CheckerExecutionError(
                "operational_detail must be non-blank when operational_status is not ok."
            )
        if self.llm_usage is not None and not isinstance(
            self.llm_usage,
            LLMUsageReceipt,
        ):
            raise CheckerExecutionError("llm_usage must be an LLMUsageReceipt or None.")


@dataclass(frozen=True, slots=True)
class CheckerValidationResult:
    """Final checker decision plus validator-side operational disposition."""

    decision: CheckerDecision
    validator_action: ValidatorAction

    def __post_init__(self) -> None:
        if not isinstance(self.decision, CheckerDecision):
            raise CheckerExecutionError("decision must be a CheckerDecision instance.")
        if self.validator_action not in {
            "accepted",
            "forced_escalate",
            "operational_no_action",
        }:
            raise CheckerExecutionError("validator_action is invalid.")


@dataclass(frozen=True, slots=True)
class _NoActionValidationIssue:
    reason: str
    resolution: Literal["forced_escalate", "operational_no_action"]


class ConservativeCheckerValidator:
    """Convert unsafe checker no_action outputs into escalation handoffs."""

    def __init__(self, *, force_escalate_on_insufficient_coverage: bool = True) -> None:
        if not isinstance(force_escalate_on_insufficient_coverage, bool):
            raise CheckerExecutionError(
                "force_escalate_on_insufficient_coverage must be boolean."
            )
        self._force_escalate_on_insufficient_coverage = (
            force_escalate_on_insufficient_coverage
        )

    def validate(
        self,
        *,
        pack: CheckerContextPack,
        raw_decision: RawCheckerDecision,
    ) -> CheckerValidationResult:
        if not isinstance(pack, CheckerContextPack):
            raise CheckerExecutionError("pack must be a CheckerContextPack instance.")
        if not isinstance(raw_decision, RawCheckerDecision):
            raise CheckerExecutionError(
                "raw_decision must be a RawCheckerDecision instance."
            )
        if raw_decision.target_key != pack.request.target_key:
            raise CheckerExecutionError("raw decision target_key must match pack request.")
        if raw_decision.event_ids != list(pack.request.event_ids):
            raise CheckerExecutionError("raw decision event_ids must match pack request.")

        if raw_decision.decision == "escalate":
            return CheckerValidationResult(
                decision=_build_final_decision(raw_decision),
                validator_action="accepted",
            )

        if raw_decision.operational_status != "ok":
            return CheckerValidationResult(
                decision=_build_operational_no_action_decision(
                    pack=pack,
                    reason=(
                        raw_decision.operational_detail
                        or "checker operational status is not ok"
                    ),
                ),
                validator_action="operational_no_action",
            )

        issue = self._unsafe_no_action_reason(pack, raw_decision)
        if issue is not None:
            if issue.resolution == "operational_no_action":
                return CheckerValidationResult(
                    decision=_build_operational_no_action_decision(
                        pack=pack,
                        reason=issue.reason,
                    ),
                    validator_action="operational_no_action",
                )
            return CheckerValidationResult(
                decision=CheckerDecision(
                    target_key=pack.request.target_key,
                    event_ids=list(pack.request.event_ids),
                    decision="escalate",
                    rationale=(
                        "Checker validator escalated unsupported no_action: "
                        f"{issue.reason}"
                    ),
                    attention_hint=(
                        "Analyze the admitted evidence because checker context did not "
                        "provide enough positive support for no_action."
                    ),
                    requires_watchlist_maintenance=False,
                ),
                validator_action="forced_escalate",
            )
        return CheckerValidationResult(
            decision=_build_final_decision(raw_decision),
            validator_action="accepted",
        )

    def _unsafe_no_action_reason(
        self,
        pack: CheckerContextPack,
        raw_decision: RawCheckerDecision,
    ) -> _NoActionValidationIssue | None:
        if raw_decision.uncertainty:
            return _NoActionValidationIssue(
                reason="checker marked the decision uncertain",
                resolution="forced_escalate",
            )
        support = raw_decision.no_action_support
        has_deterministic_duplicate_coverage = (
            support is not None
            and support.support_type in {"duplicate", "already_covered"}
            and bool(pack.coverage.prior_source_event_ids)
        )
        if (
            self._force_escalate_on_insufficient_coverage
            and not pack.coverage.sufficient_for_no_action
            and not has_deterministic_duplicate_coverage
        ):
            return _NoActionValidationIssue(
                reason="context coverage is insufficient for no_action",
                resolution="operational_no_action",
            )
        if support is None:
            return _NoActionValidationIssue(
                reason="no_action lacks structured positive support",
                resolution="forced_escalate",
            )
        if not has_deterministic_duplicate_coverage:
            invalid_section = _invalid_supporting_section(pack, support)
            if invalid_section is not None:
                return invalid_section
        if raw_decision.possible_watchlist_trigger:
            trigger_issue = _watchlist_trigger_issue(pack, support)
            if trigger_issue is not None:
                return trigger_issue
        return None


def _build_final_decision(raw_decision: RawCheckerDecision) -> CheckerDecision:
    try:
        return CheckerDecision(
            target_key=raw_decision.target_key,
            event_ids=list(raw_decision.event_ids),
            decision=raw_decision.decision,
            rationale=raw_decision.rationale,
            attention_hint=raw_decision.attention_hint,
            requires_watchlist_maintenance=raw_decision.requires_watchlist_maintenance,
        )
    except RuntimeContractError as exc:
        raise CheckerExecutionError(str(exc)) from exc


def _build_operational_no_action_decision(
    *,
    pack: CheckerContextPack,
    reason: str,
) -> CheckerDecision:
    try:
        return CheckerDecision(
            target_key=pack.request.target_key,
            event_ids=list(pack.request.event_ids),
            decision="no_action",
            rationale=(
                "Checker validator recorded a non-analysis operational no_action: "
                f"{reason}"
            ),
            attention_hint=None,
            requires_watchlist_maintenance=False,
        )
    except RuntimeContractError as exc:
        raise CheckerExecutionError(str(exc)) from exc


def _invalid_supporting_section(
    pack: CheckerContextPack,
    support: NoActionSupport,
) -> _NoActionValidationIssue | None:
    known_sections = {section.section_id for section in pack.memory.sections}
    loaded = set(pack.coverage.material_sections_loaded)
    empty = set(pack.coverage.material_sections_empty)
    truncated = set(pack.coverage.material_sections_truncated)
    for section_id in support.supporting_sections:
        if section_id not in known_sections:
            return _NoActionValidationIssue(
                reason=f"no_action cites unsupported support section {section_id!r}",
                resolution="forced_escalate",
            )
        if section_id not in loaded:
            return _NoActionValidationIssue(
                reason=f"no_action cites unavailable support section {section_id!r}",
                resolution="operational_no_action",
            )
        if section_id in empty:
            return _NoActionValidationIssue(
                reason=f"no_action cites empty support section {section_id!r}",
                resolution="operational_no_action",
            )
        if section_id in truncated:
            return _NoActionValidationIssue(
                reason=f"no_action cites truncated support section {section_id!r}",
                resolution="operational_no_action",
            )
    return None


def _trigger_section_id(pack: CheckerContextPack) -> str | None:
    suffix = "/watchlist.md:Triggers To Escalate"
    for section in pack.memory.sections:
        if section.section_id.endswith(suffix):
            return section.section_id
    return None


def _watchlist_trigger_issue(
    pack: CheckerContextPack,
    support: NoActionSupport,
) -> _NoActionValidationIssue | None:
    trigger_section_id = _trigger_section_id(pack)
    if trigger_section_id is None:
        return _NoActionValidationIssue(
            reason="possible watchlist trigger but trigger section is unavailable",
            resolution="operational_no_action",
        )
    if trigger_section_id in set(pack.coverage.material_sections_empty):
        return _NoActionValidationIssue(
            reason="possible watchlist trigger but trigger section is empty",
            resolution="operational_no_action",
        )
    if trigger_section_id in set(pack.coverage.material_sections_truncated):
        return _NoActionValidationIssue(
            reason="possible watchlist trigger but trigger section is truncated",
            resolution="operational_no_action",
        )
    if trigger_section_id not in set(pack.coverage.material_sections_loaded):
        return _NoActionValidationIssue(
            reason="possible watchlist trigger but trigger section is unavailable",
            resolution="operational_no_action",
        )
    if trigger_section_id not in support.supporting_sections:
        return _NoActionValidationIssue(
            reason="possible watchlist trigger lacks trigger-section support",
            resolution="forced_escalate",
        )
    return None


__all__ = [
    "CheckerValidationResult",
    "ConservativeCheckerValidator",
    "NoActionSupport",
    "RawCheckerDecision",
]
