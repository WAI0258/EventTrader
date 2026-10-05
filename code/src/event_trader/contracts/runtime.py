"""Project-owned runtime handoff contracts and operational artifact helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from ._validators import (
    normalize_content,
    validate_event_id,
    validate_target_key,
)
from .analysis_assessment import AnalysisAssessment


class RuntimeContractError(ValueError):
    """Raised when a runtime handoff or operational path input is invalid."""

type CheckerDecisionValue = Literal["no_action", "escalate"]
type AnalysisOutcomeValue = Literal["no_update", "memory_updated"]
type LLMUsageAgentRole = Literal[
    "checker",
    "analysis",
    "pm_review",
    "reflection",
    "web_search_summary",
]
type LLMUsageSource = Literal["provider_reported", "unavailable"]

_RUNTIME_EVIDENCE_LANE_PREFIX = "evidence"
_RUNTIME_ADMISSION_LANE_SEGMENT = "admission"
_RUNTIME_ANALYSIS_LANE_SEGMENT = "analysis"


@dataclass(frozen=True, slots=True)
class LLMUsageReceipt:
    """Small optional MAS token-usage receipt carried by agent runtime records."""

    agent_role: LLMUsageAgentRole
    usage_source: LLMUsageSource
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        if self.agent_role not in {
            "checker",
            "analysis",
            "pm_review",
            "reflection",
            "web_search_summary",
        }:
            raise RuntimeContractError(
                "agent_role must be one of: checker, analysis, pm_review, "
                "reflection, web_search_summary."
            )
        if self.usage_source not in {"provider_reported", "unavailable"}:
            raise RuntimeContractError(
                "usage_source must be provider_reported or unavailable."
            )
        object.__setattr__(
            self,
            "provider",
            _validate_optional_usage_text(self.provider, field_name="provider"),
        )
        object.__setattr__(
            self,
            "model",
            _validate_optional_usage_text(self.model, field_name="model"),
        )
        for field_name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("total_tokens", self.total_tokens),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RuntimeContractError(
                    f"{field_name} must be a non-negative integer."
                )
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise RuntimeContractError(
                "total_tokens must equal input_tokens + output_tokens."
            )
        if self.usage_source == "provider_reported":
            if not self.provider or not self.model:
                raise RuntimeContractError(
                    "provider_reported llm usage requires provider and model."
                )
        if self.usage_source == "unavailable":
            if self.input_tokens or self.output_tokens or self.total_tokens:
                raise RuntimeContractError(
                    "unavailable llm usage must carry zero token counts."
                )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "agent_role": self.agent_role,
            "usage_source": self.usage_source,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> LLMUsageReceipt:
        if not isinstance(payload, dict):
            raise RuntimeContractError("llm_usage must be an object.")
        required = {
            "agent_role",
            "usage_source",
            "provider",
            "model",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        }
        missing = sorted(required - set(payload))
        unexpected = sorted(set(payload) - required)
        if missing or unexpected:
            problems: list[str] = []
            if missing:
                problems.append(f"missing fields: {', '.join(missing)}")
            if unexpected:
                problems.append(f"unexpected fields: {', '.join(unexpected)}")
            raise RuntimeContractError("llm_usage " + "; ".join(problems))
        return cls(
            agent_role=_validate_usage_agent_role(payload["agent_role"]),
            usage_source=_validate_usage_source(payload["usage_source"]),
            provider=_validate_usage_text_payload(payload["provider"], "provider"),
            model=_validate_usage_text_payload(payload["model"], "model"),
            input_tokens=_validate_usage_int(payload["input_tokens"], "input_tokens"),
            output_tokens=_validate_usage_int(
                payload["output_tokens"],
                "output_tokens",
            ),
            total_tokens=_validate_usage_int(payload["total_tokens"], "total_tokens"),
        )

    @classmethod
    def from_json_payload_optional(
        cls,
        payload: object | None,
    ) -> LLMUsageReceipt | None:
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise RuntimeContractError("llm_usage must be null or a JSON object.")
        return cls.from_json_payload(payload)

    @classmethod
    def unavailable(
        cls,
        *,
        agent_role: LLMUsageAgentRole,
        provider: str = "",
        model: str = "",
    ) -> LLMUsageReceipt:
        return cls(
            agent_role=agent_role,
            usage_source="unavailable",
            provider=provider,
            model=model,
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
        )


@dataclass(frozen=True, slots=True)
class CheckerRequest:
    """Minimal checker input identifying only admitted evidence to review."""

    target_key: str
    event_ids: list[str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=RuntimeContractError,
            ),
        )
        object.__setattr__(
            self,
            "event_ids",
            _validate_event_ids(self.event_ids),
        )


@dataclass(frozen=True, slots=True)
class CheckerDecision(CheckerRequest):
    """Checker routing result kept intentionally smaller than ledger truth."""

    decision: CheckerDecisionValue
    rationale: str | None = None
    attention_hint: str | None = None
    requires_watchlist_maintenance: bool = False

    def __post_init__(self) -> None:
        CheckerRequest.__post_init__(self)
        object.__setattr__(
            self,
            "decision",
            _validate_checker_decision(self.decision),
        )
        object.__setattr__(
            self,
            "rationale",
            _validate_checker_rationale(self.decision, self.rationale),
        )
        object.__setattr__(
            self,
            "attention_hint",
            _validate_checker_attention_hint(
                self.decision,
                self.attention_hint,
            ),
        )
        object.__setattr__(
            self,
            "requires_watchlist_maintenance",
            _validate_watchlist_maintenance_requirement(
                self.decision,
                self.requires_watchlist_maintenance,
            ),
        )


@dataclass(frozen=True, slots=True)
class AnalysisRequest(CheckerRequest):
    """Minimal escalation handoff from checker to analysis."""

    why_escalated: str
    requires_watchlist_maintenance: bool = False
    decision_episode_id: str = ""

    def __post_init__(self) -> None:
        CheckerRequest.__post_init__(self)
        object.__setattr__(
            self,
            "why_escalated",
            _validate_analysis_attention_hint(
                self.why_escalated,
                field_name="why_escalated",
            ),
        )
        object.__setattr__(
            self,
            "requires_watchlist_maintenance",
            _validate_bool(
                self.requires_watchlist_maintenance,
                field_name="requires_watchlist_maintenance",
            ),
        )
        object.__setattr__(
            self,
            "decision_episode_id",
            _validate_optional_decision_episode_id(self.decision_episode_id),
        )


@dataclass(frozen=True, slots=True)
class AnalysisResult(CheckerRequest):
    """Exposure-blind analysis outcome with optional assessment surface."""

    outcome: AnalysisOutcomeValue
    analysis_assessment: AnalysisAssessment | None = None
    used_lesson_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        CheckerRequest.__post_init__(self)
        object.__setattr__(
            self,
            "outcome",
            _validate_analysis_outcome(self.outcome),
        )
        object.__setattr__(
            self,
            "used_lesson_ids",
            _validate_lesson_ids(self.used_lesson_ids),
        )
        object.__setattr__(
            self,
            "analysis_assessment",
            _validate_analysis_assessment(self),
        )


def admission_lane(target_key: str) -> str:
    """Return the canonical business lane for newly admitted evidence."""
    validated_target_key = validate_target_key(
        target_key,
        error_type=RuntimeContractError,
    )
    return ".".join(
        (
            _RUNTIME_EVIDENCE_LANE_PREFIX,
            _RUNTIME_ADMISSION_LANE_SEGMENT,
            validated_target_key,
        )
    )


def analysis_lane(target_key: str) -> str:
    """Return the canonical business lane for evidence escalated to analysis."""
    validated_target_key = validate_target_key(
        target_key,
        error_type=RuntimeContractError,
    )
    return ".".join(
        (
            _RUNTIME_EVIDENCE_LANE_PREFIX,
            _RUNTIME_ANALYSIS_LANE_SEGMENT,
            validated_target_key,
        )
    )


def _validate_checker_decision(value: str) -> CheckerDecisionValue:
    if value not in {"no_action", "escalate"}:
        raise RuntimeContractError("decision must be one of: no_action, escalate.")
    return cast(CheckerDecisionValue, value)


def _validate_analysis_outcome(value: str) -> AnalysisOutcomeValue:
    if value not in {"no_update", "memory_updated"}:
        raise RuntimeContractError(
            "outcome must be one of: no_update, memory_updated."
        )
    return cast(AnalysisOutcomeValue, value)


def _validate_usage_agent_role(value: object) -> LLMUsageAgentRole:
    if value not in {
        "checker",
        "analysis",
        "pm_review",
        "reflection",
        "web_search_summary",
    }:
        raise RuntimeContractError("agent_role is not supported.")
    return cast(LLMUsageAgentRole, value)


def _validate_usage_source(value: object) -> LLMUsageSource:
    if value not in {"provider_reported", "unavailable"}:
        raise RuntimeContractError("usage_source is not supported.")
    return cast(LLMUsageSource, value)


def _validate_usage_text_payload(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeContractError(f"{field_name} must be a string.")
    return value.strip()


def _validate_optional_usage_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeContractError(f"{field_name} must be a string.")
    return value.strip()


def _validate_usage_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeContractError(f"{field_name} must be a non-negative integer.")
    return value


def _validate_analysis_assessment(
    result: AnalysisResult,
) -> AnalysisAssessment | None:
    assessment = result.analysis_assessment
    if assessment is None:
        return None
    if not isinstance(assessment, AnalysisAssessment):
        raise RuntimeContractError(
            "analysis_assessment must be an AnalysisAssessment instance."
        )
    if assessment.target_key != result.target_key:
        raise RuntimeContractError(
            "analysis_assessment.target_key must equal AnalysisResult.target_key."
        )
    if not set(result.event_ids).issubset(set(assessment.source_event_ids)):
        raise RuntimeContractError(
            "AnalysisResult.event_ids must be a subset of "
            "analysis_assessment.source_event_ids."
        )
    return assessment


def _validate_checker_rationale(
    decision: CheckerDecisionValue,
    rationale: str | None,
) -> str | None:
    if rationale is None:
        raise RuntimeContractError(
            "rationale is required when decision is no_action or escalate."
        )

    return _validate_runtime_text(rationale, field_name="rationale")


def _validate_checker_attention_hint(
    decision: CheckerDecisionValue,
    attention_hint: str | None,
) -> str | None:
    if decision == "no_action":
        if attention_hint is not None:
            raise RuntimeContractError(
                "no_action decisions must not carry an attention_hint."
            )
        return None
    if attention_hint is None:
        raise RuntimeContractError(
            "attention_hint is required when decision is escalate."
        )
    return _validate_runtime_text(attention_hint, field_name="attention_hint")


def _validate_watchlist_maintenance_requirement(
    decision: CheckerDecisionValue,
    requires_watchlist_maintenance: bool,
) -> bool:
    normalized = _validate_bool(
        requires_watchlist_maintenance,
        field_name="requires_watchlist_maintenance",
    )
    if decision == "no_action" and normalized:
        raise RuntimeContractError(
            "no_action decisions must not require watchlist maintenance."
        )
    return normalized


def _validate_bool(value: bool, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeContractError(f"{field_name} must be a boolean.")
    return value


def _validate_optional_decision_episode_id(value: str) -> str:
    if not isinstance(value, str):
        raise RuntimeContractError("decision_episode_id must be a string.")
    normalized = value.strip()
    if normalized and not normalized.startswith("decision-episode:"):
        raise RuntimeContractError(
            "decision_episode_id must start with 'decision-episode:' when set."
        )
    return normalized


def _validate_analysis_attention_hint(value: str, *, field_name: str) -> str:
    return _validate_runtime_text(value, field_name=field_name)


def _validate_runtime_text(value: str, *, field_name: str) -> str:
    return normalize_content(
        value,
        field_name=field_name,
        error_type=RuntimeContractError,
    )


def _validate_event_ids(event_ids: list[str]) -> list[str]:
    if not isinstance(event_ids, list):
        raise RuntimeContractError("event_ids must be a list[str].")
    if not event_ids:
        raise RuntimeContractError("event_ids must not be empty.")

    normalized_ids: list[str] = []
    seen: set[str] = set()
    for event_id in event_ids:
        if not isinstance(event_id, str):
            raise RuntimeContractError("event_ids must contain only strings.")

        normalized = event_id.strip()
        if not normalized:
            raise RuntimeContractError("event_ids must not contain blank values.")
        if normalized != event_id:
            raise RuntimeContractError(
                "event_ids must not include leading or trailing whitespace."
            )

        validate_event_id(normalized, error_type=RuntimeContractError)
        if normalized in seen:
            raise RuntimeContractError("event_ids must not contain duplicates.")

        seen.add(normalized)
        normalized_ids.append(normalized)

    return normalized_ids


def _validate_lesson_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized_values = tuple(values)
    seen: set[str] = set()
    for lesson_id in normalized_values:
        if not isinstance(lesson_id, str):
            raise RuntimeContractError("used_lesson_ids must contain only strings.")
        normalized = lesson_id.strip()
        if not normalized:
            raise RuntimeContractError("used_lesson_ids must not contain blank values.")
        if normalized != lesson_id:
            raise RuntimeContractError(
                "used_lesson_ids must not include leading or trailing whitespace."
            )
        if normalized in seen:
            raise RuntimeContractError("used_lesson_ids must not contain duplicates.")
        seen.add(normalized)
    return normalized_values


__all__ = [
    "AnalysisOutcomeValue",
    "AnalysisRequest",
    "AnalysisResult",
    "AnalysisAssessment",
    "CheckerDecision",
    "CheckerDecisionValue",
    "CheckerRequest",
    "LLMUsageAgentRole",
    "LLMUsageReceipt",
    "LLMUsageSource",
    "RuntimeContractError",
    "admission_lane",
    "analysis_lane",
]


