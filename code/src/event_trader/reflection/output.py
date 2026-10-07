"""Project-owned parsing and conservative fallback for Reflection outputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isclose
from typing import TypedDict, cast

from event_trader.episode_memory.contracts import (
    EPISODE_MEMORY_LEARNING_CARD_CONSUMER_ROLES,
)
from event_trader.integrations.boxed_json import (
    BoxedJsonPayloadError,
    load_first_boxed_json_object,
)
from event_trader.reasoning.runtime import AgentRunReceipt
from event_trader.reflection.agent_contract import ReflectionTaskKind
from event_trader.reflection.context import ReflectionLedgerContext
from event_trader.reflection.contracts import (
    MARKET_CONTEXT_USAGE_QUALITY_LABELS,
    MarketContextUsageQualityLabel,
    OpenPositionEvaluationResult,
    ReflectionEvaluationResult,
    ReflectionExposureAssessment,
    ReflectionHorizonAssessment,
    ReflectionPathVerdict,
    ReflectionPriceRelation,
    ReflectionPriceRelationAssessment,
    ReflectionPriceTriggerAssessment,
    ReflectionPriceTriggerKind,
    ReflectionPriceTriggerStatus,
    ReflectionReviewDecision,
    ReflectionThesisAssessment,
    ReflectionThesisVerdict,
    ReflectionTradabilityVerdict,
    ReflectionTradeAssessment,
    ReflectionTradeVerdict,
    ReflectionWatchlistAssessment,
    ReflectionWatchlistVerdict,
    TargetCloseReflectionEvaluationResult,
    TargetReflectionEvaluationResult,
)
from event_trader.reflection.learning_contracts import (
    REFLECTION_ERROR_ATTRIBUTIONS,
    REFLECTION_LEARNING_OUTCOMES,
    ReflectionLearningAction,
    ReflectionLearningDecision,
    parse_learning_decision_payload,
)
from event_trader.reflection.price_facts import (
    open_position_watchlist_price_facts,
    open_position_watchlist_trigger_facts,
    price_relation_assessments,
    price_trigger_assessments,
)
from event_trader.reflection.view_contracts import (
    CloseEpisodeReflectionContext,
    EpisodeReflectionContext,
    OpenPositionReflectionContext,
)

type ReflectionRepairPath = tuple[str | int, ...]


class ReflectionOutputContractError(RuntimeError):
    """Raised when an agent receipt cannot satisfy a Reflection output contract."""

    def __init__(
        self,
        message: str,
        *,
        repair_paths: tuple[ReflectionRepairPath, ...] = (),
    ) -> None:
        self.repair_paths = tuple(tuple(path) for path in repair_paths)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _BusinessContractViolation:
    message: str
    repair_path: ReflectionRepairPath


class _UsageQualityPayload(TypedDict):
    usage_quality_label: MarketContextUsageQualityLabel | None
    usage_quality_summary: str | None
    market_context_error_labels: tuple[MarketContextUsageQualityLabel, ...]


_REFLECTION_TRADE_VERDICTS = (
    "win",
    "loss",
    "scratch",
    "missed",
    "not_taken",
    "open",
)
_CLOSED_REFLECTION_TRADE_VERDICTS = tuple(
    verdict for verdict in _REFLECTION_TRADE_VERDICTS if verdict != "open"
)
_EPISODE_MEMORY_STATUSES = (
    "provisional",
    "validated",
    "invalidated",
    "finalized",
)
_EPISODE_MEMORY_VALIDATION_BASES = (
    "current_evidence",
    "later_evidence",
    "market_return",
    "execution_feedback",
    "portfolio_feedback",
    "close_review",
)
_EPISODE_MEMORY_SOURCE_REF_KEYS = (
    "evidence_event_ids",
    "market_bar_refs",
    "review_paths",
    "pm_review_request_ids",
    "pm_decision_ids",
    "execution_record_ids",
    "portfolio_record_ids",
)
_RETURN_PCT_ABS_TOLERANCE = 5e-4


def reflection_output_schema(task_kind: ReflectionTaskKind) -> dict[str, object]:
    """Return the project-owned structured output schema for one Reflection task."""

    properties, required = _reflection_evaluation_shape(task_kind)
    if task_kind != "shared":
        properties = {
            **properties,
            "learning_decision": _learning_decision_schema(),
        }
        required = (*required, "learning_decision")
    return _object_schema(properties=properties, required=required)


def reflection_evaluation_schema(task_kind: ReflectionTaskKind) -> dict[str, object]:
    """Return the final-owner schema before any episode learning decision."""

    properties, required = _reflection_evaluation_shape(task_kind)
    return _object_schema(properties=properties, required=required)


def reflection_learning_decision_schema(
    *,
    target_key: str | None = None,
) -> dict[str, object]:
    """Return the isolated schema for one complete episode learning decision."""

    return _object_schema(
        properties={
            "learning_decision": _learning_decision_schema(target_key=target_key),
        },
        required=("learning_decision",),
    )


def _reflection_evaluation_shape(
    task_kind: ReflectionTaskKind,
) -> tuple[dict[str, object], tuple[str, ...]]:

    common_properties = {
        "thesis_assessment": _thesis_assessment_schema(),
        "trade_assessment": _nullable_schema(_trade_assessment_schema()),
        "review_decision": {
            "type": "string",
            "enum": ["write_review", "skip_review"],
        },
        "decision_rationale": _non_blank_string_schema(),
    }
    if task_kind == "shared":
        return (
            {
                "assessed_horizons": _assessed_horizons_schema(),
                **common_properties,
            },
            (
                "assessed_horizons",
                "thesis_assessment",
                "trade_assessment",
                "review_decision",
                "decision_rationale",
            ),
        )

    episode_properties = {
        "episode_id": _non_blank_string_schema(),
        "target_key": _non_blank_string_schema(),
        "thesis_assessment": _thesis_assessment_schema(),
        "watchlist_assessment": _watchlist_assessment_schema(),
        "usage_quality_label": {
            "type": "string",
            "enum": list(MARKET_CONTEXT_USAGE_QUALITY_LABELS),
        },
        "usage_quality_summary": {"type": "string"},
        "market_context_error_labels": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": list(MARKET_CONTEXT_USAGE_QUALITY_LABELS),
            },
            "uniqueItems": True,
        },
        "review_decision": common_properties["review_decision"],
        "decision_rationale": common_properties["decision_rationale"],
    }
    episode_required = (
        "episode_id",
        "target_key",
        "thesis_assessment",
        "watchlist_assessment",
        "trade_assessment",
        "usage_quality_label",
        "usage_quality_summary",
        "market_context_error_labels",
        "review_decision",
        "decision_rationale",
    )
    if task_kind == "target":
        return (
            {
                "episode_id": episode_properties["episode_id"],
                "target_key": episode_properties["target_key"],
                "assessed_horizons": _assessed_horizons_schema(),
                "thesis_assessment": episode_properties["thesis_assessment"],
                "watchlist_assessment": episode_properties["watchlist_assessment"],
                "trade_assessment": common_properties["trade_assessment"],
                "usage_quality_label": episode_properties["usage_quality_label"],
                "usage_quality_summary": episode_properties["usage_quality_summary"],
                "market_context_error_labels": episode_properties["market_context_error_labels"],
                "review_decision": episode_properties["review_decision"],
                "decision_rationale": episode_properties["decision_rationale"],
            },
            ("assessed_horizons", *episode_required),
        )
    if task_kind == "open_position":
        return (
            {
                **episode_properties,
                "watchlist_assessment": _watchlist_assessment_schema(
                    include_price_relations=True,
                    include_price_triggers=True,
                ),
                "trade_assessment": _trade_assessment_schema(
                    allowed_verdicts=("open",),
                ),
                "exposure_assessment": _exposure_assessment_schema(),
            },
            (*episode_required, "exposure_assessment"),
        )
    if task_kind == "target_close":
        return (
            {
                **episode_properties,
                "trade_assessment": _trade_assessment_schema(
                    allowed_verdicts=_CLOSED_REFLECTION_TRADE_VERDICTS,
                ),
            },
            episode_required,
        )
    raise ValueError(f"unsupported Reflection task kind: {task_kind!r}")


def _object_schema(
    *,
    properties: dict[str, object],
    required: tuple[str, ...],
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _non_blank_string_schema() -> dict[str, object]:
    return {"type": "string", "minLength": 1}


def _nullable_schema(schema: dict[str, object]) -> dict[str, object]:
    return {"anyOf": [schema, {"type": "null"}]}


def _assessed_horizons_schema() -> dict[str, object]:
    return {
        "type": "array",
        "minItems": 1,
        "items": _object_schema(
            properties={
                "horizon_hours": {"type": "number", "exclusiveMinimum": 0},
                "return_pct": {"type": "number"},
                "path_verdict": {
                    "type": "string",
                    "enum": ["favorable", "mixed", "adverse", "unclear"],
                },
                "tradability_verdict": {
                    "type": "string",
                    "enum": ["tradable", "mixed", "poor", "unclear"],
                },
                "summary_md": _non_blank_string_schema(),
            },
            required=(
                "horizon_hours",
                "return_pct",
                "path_verdict",
                "tradability_verdict",
                "summary_md",
            ),
        ),
    }


def _thesis_assessment_schema() -> dict[str, object]:
    return _object_schema(
        properties={
            "verdict": {
                "type": "string",
                "enum": ["validated", "mixed", "invalidated", "unclear"],
            },
            "summary_md": _non_blank_string_schema(),
        },
        required=("verdict", "summary_md"),
    )


def _trade_assessment_schema(
    *,
    allowed_verdicts: tuple[str, ...] = _REFLECTION_TRADE_VERDICTS,
) -> dict[str, object]:
    return _object_schema(
        properties={
            "verdict": {"type": "string", "enum": list(allowed_verdicts)},
            "summary_md": _non_blank_string_schema(),
            "return_pct": _nullable_schema({"type": "number"}),
        },
        required=("verdict", "summary_md", "return_pct"),
    )


def _watchlist_assessment_schema(
    *,
    include_price_relations: bool = False,
    include_price_triggers: bool = False,
) -> dict[str, object]:
    properties: dict[str, object] = {
        "verdict": {
            "type": "string",
            "enum": ["useful", "mixed", "stale", "missing", "unclear"],
        },
        "summary_md": _non_blank_string_schema(),
    }
    required: tuple[str, ...] = ("verdict", "summary_md")
    if include_price_relations:
        properties["price_relations"] = {
            "type": "array",
            "items": _object_schema(
                properties={
                    "level_id": _non_blank_string_schema(),
                    "relation": {
                        "type": "string",
                        "enum": ["below", "within", "above"],
                    },
                },
                required=("level_id", "relation"),
            ),
            "uniqueItems": True,
        }
        required = (*required, "price_relations")
    if include_price_triggers:
        properties["price_triggers"] = {
            "type": "array",
            "items": _object_schema(
                properties={
                    "level_id": _non_blank_string_schema(),
                    "trigger_kind": {
                        "type": "string",
                        "enum": ["refresh", "invalidation"],
                    },
                    "trigger_id": _non_blank_string_schema(),
                    "status": {
                        "type": "string",
                        "enum": ["met", "not_met", "not_evaluable"],
                    },
                },
                required=("level_id", "trigger_kind", "trigger_id", "status"),
            ),
            "uniqueItems": True,
        }
        required = (*required, "price_triggers")
    return _object_schema(
        properties=properties,
        required=required,
    )


def _exposure_assessment_schema() -> dict[str, object]:
    return _object_schema(
        properties={"summary_md": _non_blank_string_schema()},
        required=("summary_md",),
    )


def _learning_decision_schema(*, target_key: str | None = None) -> dict[str, object]:
    return _object_schema(
        properties={
            "primary_outcome": {
                "type": "string",
                "enum": list(REFLECTION_LEARNING_OUTCOMES),
            },
            "actions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "anyOf": [
                        _no_write_action_schema(),
                        _memory_action_schema(target_key=target_key),
                    ]
                },
            },
        },
        required=("primary_outcome", "actions"),
    )


def _learning_action_properties() -> dict[str, object]:
    return {
        "action_id": _non_blank_string_schema(),
        "rationale": _non_blank_string_schema(),
        "source_episode_id": _non_blank_string_schema(),
        "source_review_path": _non_blank_string_schema(),
        "effective_from": _non_blank_string_schema(),
        "error_attributions": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": list(REFLECTION_ERROR_ATTRIBUTIONS),
            },
            "uniqueItems": True,
        },
    }


def _no_write_action_schema() -> dict[str, object]:
    return _object_schema(
        properties={
            **_learning_action_properties(),
            "outcome": {"type": "string", "enum": ["no_learning_write"]},
            "payload": _object_schema(properties={}, required=()),
        },
        required=(
            "action_id",
            "outcome",
            "rationale",
            "source_episode_id",
            "source_review_path",
            "effective_from",
            "error_attributions",
            "payload",
        ),
    )


def _memory_action_schema(*, target_key: str | None = None) -> dict[str, object]:
    return _object_schema(
        properties={
            **_learning_action_properties(),
            "outcome": {
                "type": "string",
                "enum": ["emit_episode_memory_candidate"],
            },
            "payload": {
                "anyOf": [
                    _episode_memory_delta_schema(target_key=target_key),
                    _episode_memory_no_update_schema(),
                ]
            },
        },
        required=(
            "action_id",
            "outcome",
            "rationale",
            "source_episode_id",
            "source_review_path",
            "effective_from",
            "error_attributions",
            "payload",
        ),
    )


def _source_refs_schema() -> dict[str, object]:
    string_array = {
        "type": "array",
        "items": _non_blank_string_schema(),
    }
    return _object_schema(
        properties={key: dict(string_array) for key in _EPISODE_MEMORY_SOURCE_REF_KEYS},
        required=(),
    )


def _episode_memory_delta_schema(*, target_key: str | None = None) -> dict[str, object]:
    return _object_schema(
        properties={
            "candidate_kind": {"type": "string", "enum": ["delta"]},
            "delta_kind": _non_blank_string_schema(),
            "memory_status": {
                "type": "string",
                "enum": list(_EPISODE_MEMORY_STATUSES),
            },
            "validation_basis": {
                "type": "string",
                "enum": list(_EPISODE_MEMORY_VALIDATION_BASES),
            },
            "source_visible_through": _non_blank_string_schema(),
            "usable_from": _non_blank_string_schema(),
            "source_refs": _source_refs_schema(),
            "summary_md": _non_blank_string_schema(),
            "thesis_delta": _non_blank_string_schema(),
            "pm_management_delta": _non_blank_string_schema(),
            "risk_delta": _non_blank_string_schema(),
            "invalidation_delta": _non_blank_string_schema(),
            "error_attributions": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": list(REFLECTION_ERROR_ATTRIBUTIONS),
                },
                "uniqueItems": True,
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "supersedes_delta_ids": {
                "type": "array",
                "items": _non_blank_string_schema(),
                "uniqueItems": True,
            },
            "promotion": _promotion_schema(target_key=target_key),
        },
        required=(
            "candidate_kind",
            "delta_kind",
            "memory_status",
            "validation_basis",
            "source_visible_through",
            "usable_from",
            "source_refs",
            "summary_md",
            "thesis_delta",
            "pm_management_delta",
            "risk_delta",
            "invalidation_delta",
            "error_attributions",
            "confidence",
            "supersedes_delta_ids",
        ),
    )


def _episode_memory_no_update_schema() -> dict[str, object]:
    properties = {
        "candidate_kind": {"type": "string", "enum": ["no_update"]},
        "reason": _non_blank_string_schema(),
        "reason_md": _non_blank_string_schema(),
        "reason_code": _non_blank_string_schema(),
        "source_visible_through": _non_blank_string_schema(),
        "usable_from": _non_blank_string_schema(),
        "source_refs": _source_refs_schema(),
        "reviewed_delta_ids": {
            "type": "array",
            "items": _non_blank_string_schema(),
            "uniqueItems": True,
        },
        "receipt_id": _non_blank_string_schema(),
    }
    schema = _object_schema(
        properties=properties,
        required=(
            "candidate_kind",
            "reason_code",
            "source_visible_through",
            "usable_from",
            "source_refs",
            "reviewed_delta_ids",
        ),
    )
    schema["anyOf"] = [{"required": ["reason"]}, {"required": ["reason_md"]}]
    return schema


def _promotion_schema(*, target_key: str | None = None) -> dict[str, object]:
    scope_key_schema: dict[str, object] = _non_blank_string_schema()
    if target_key is not None:
        scope_key_schema = {"type": "string", "enum": ["shared", f"target:{target_key}"]}
    return _object_schema(
        properties={
            "consumer_role": {
                "type": "string",
                "enum": list(EPISODE_MEMORY_LEARNING_CARD_CONSUMER_ROLES),
            },
            "scope_key": scope_key_schema,
            "title": _non_blank_string_schema(),
            "summary_md": _non_blank_string_schema(),
            "body_md": _non_blank_string_schema(),
            "use_when": {
                "type": "array",
                "items": _non_blank_string_schema(),
            },
            "avoid_when": {
                "type": "array",
                "items": _non_blank_string_schema(),
            },
            "tags": {"type": "array", "items": _non_blank_string_schema()},
            "supersedes_card_ids": {
                "type": "array",
                "items": _non_blank_string_schema(),
                "uniqueItems": True,
            },
            "promotion_id": _non_blank_string_schema(),
            "promotion_decision_id": _non_blank_string_schema(),
            "card_id": _non_blank_string_schema(),
            "confidence": {"type": "number"},
            "usable_from": _non_blank_string_schema(),
            "created_at": _non_blank_string_schema(),
        },
        required=(
            "consumer_role",
            "scope_key",
            "title",
            "summary_md",
            "body_md",
            "use_when",
            "avoid_when",
        ),
    )


def parse_shared_reflection_output(
    receipt: AgentRunReceipt,
    *,
    context: ReflectionLedgerContext,
) -> ReflectionEvaluationResult:
    payload = _load_reflection_payload(receipt)
    normalized = _normalize_reflection_payload_object(payload)
    usage_quality = _normalize_usage_quality_payload(normalized)
    return ReflectionEvaluationResult(
        anchor=context.anchor,
        coverage=context.coverage,
        assessed_horizons=_normalize_horizon_assessments(normalized["assessed_horizons"]),
        thesis_assessment=_normalize_thesis_assessment(normalized["thesis_assessment"]),
        trade_assessment=_normalize_trade_assessment(normalized.get("trade_assessment")),
        review_decision=_require_review_decision(
            normalized["review_decision"],
            field_name="review_decision",
        ),
        decision_rationale=_require_string(
            normalized["decision_rationale"],
            field_name="decision_rationale",
        ),
        usage_quality_label=usage_quality["usage_quality_label"],
        usage_quality_summary=usage_quality["usage_quality_summary"],
        market_context_error_labels=usage_quality["market_context_error_labels"],
    )


def parse_target_reflection_output(
    receipt: AgentRunReceipt,
    *,
    context: EpisodeReflectionContext,
) -> TargetReflectionEvaluationResult:
    payload = _load_reflection_payload(
        receipt,
        required_fields=(
            "episode_id",
            "target_key",
            "watchlist_assessment",
            "learning_decision",
        ),
    )
    normalized = _normalize_reflection_payload_object(
        payload,
        required_fields=(
            "episode_id",
            "target_key",
            "watchlist_assessment",
            "learning_decision",
        ),
    )
    episode_id, target_key = _require_active_episode(normalized, context=context)
    review_decision = _require_review_decision(
        normalized["review_decision"],
        field_name="review_decision",
    )
    usage_quality = _normalize_usage_quality_payload(normalized)
    evaluation = TargetReflectionEvaluationResult(
        episode_id=episode_id,
        target_key=target_key,
        coverage=context.coverage,
        assessed_horizons=_normalize_horizon_assessments(normalized["assessed_horizons"]),
        thesis_assessment=_normalize_thesis_assessment(normalized["thesis_assessment"]),
        watchlist_assessment=_normalize_watchlist_assessment(normalized["watchlist_assessment"]),
        trade_assessment=_normalize_trade_assessment(normalized.get("trade_assessment")),
        review_decision=review_decision,
        decision_rationale=_require_string(
            normalized["decision_rationale"],
            field_name="decision_rationale",
        ),
        learning_decision=parse_learning_decision_payload(
            normalized["learning_decision"],
            review_decision=review_decision,
            target_key=target_key,
        ),
        usage_quality_label=usage_quality["usage_quality_label"],
        usage_quality_summary=usage_quality["usage_quality_summary"],
        market_context_error_labels=usage_quality["market_context_error_labels"],
    )
    violations = _target_reflection_business_contract_violations(
        evaluation,
        context=context,
    )
    if violations:
        raise ReflectionOutputContractError(
            " ".join(violation.message for violation in violations),
            repair_paths=tuple(violation.repair_path for violation in violations),
        )
    return evaluation


def parse_open_position_reflection_output(
    receipt: AgentRunReceipt,
    *,
    context: OpenPositionReflectionContext,
) -> OpenPositionEvaluationResult:
    expected_price_relations = price_relation_assessments(
        open_position_watchlist_price_facts(context)
    )
    expected_price_triggers = price_trigger_assessments(
        open_position_watchlist_trigger_facts(context)
    )
    return validate_open_position_reflection_business_contract(
        parse_open_position_reflection_output_for_scope(
            receipt,
            episode_id=context.episode.episode_id,
            target_key=context.episode.target_key,
        ),
        expected_return_pct=context.terminal_mark.strategy_return * 100.0,
        expected_price_relations=expected_price_relations,
        expected_price_triggers=expected_price_triggers,
    )


def parse_open_position_reflection_output_for_scope(
    receipt: AgentRunReceipt,
    *,
    episode_id: str,
    target_key: str,
) -> OpenPositionEvaluationResult:
    """Parse an open-position result against its frozen episode identity."""

    required_fields = (
        "episode_id",
        "target_key",
        "watchlist_assessment",
        "exposure_assessment",
        "learning_decision",
    )
    payload = _load_reflection_payload(
        receipt,
        required_fields=required_fields,
        excluded_fields=("assessed_horizons",),
    )
    normalized = _normalize_reflection_payload_object(
        payload,
        required_fields=required_fields,
        excluded_fields=("assessed_horizons",),
    )
    parsed_episode_id, parsed_target_key = _require_active_episode_scope(
        normalized,
        episode_id=episode_id,
        target_key=target_key,
    )
    trade_assessment = _normalize_trade_assessment(normalized.get("trade_assessment"))
    if trade_assessment is None:
        raise ReflectionOutputContractError("open-position reflection requires trade_assessment.")
    review_decision = _require_review_decision(
        normalized["review_decision"],
        field_name="review_decision",
    )
    usage_quality = _normalize_usage_quality_payload(normalized)
    return OpenPositionEvaluationResult(
        episode_id=parsed_episode_id,
        target_key=parsed_target_key,
        thesis_assessment=_normalize_thesis_assessment(normalized["thesis_assessment"]),
        watchlist_assessment=_normalize_watchlist_assessment(normalized["watchlist_assessment"]),
        trade_assessment=trade_assessment,
        exposure_assessment=_normalize_exposure_assessment(normalized["exposure_assessment"]),
        review_decision=review_decision,
        decision_rationale=_require_string(
            normalized["decision_rationale"],
            field_name="decision_rationale",
        ),
        learning_decision=parse_learning_decision_payload(
            normalized["learning_decision"],
            review_decision=review_decision,
            target_key=parsed_target_key,
        ),
        usage_quality_label=usage_quality["usage_quality_label"],
        usage_quality_summary=usage_quality["usage_quality_summary"],
        market_context_error_labels=usage_quality["market_context_error_labels"],
    )


def _target_reflection_business_contract_errors(
    evaluation: TargetReflectionEvaluationResult,
    *,
    context: EpisodeReflectionContext,
) -> tuple[str, ...]:
    return tuple(
        violation.message
        for violation in _target_reflection_business_contract_violations(
            evaluation,
            context=context,
        )
    )


def _target_reflection_business_contract_violations(
    evaluation: TargetReflectionEvaluationResult,
    *,
    context: EpisodeReflectionContext,
) -> tuple[_BusinessContractViolation, ...]:
    violations: list[_BusinessContractViolation] = []
    trade_assessment = evaluation.trade_assessment
    if trade_assessment is not None:
        return_error = _return_pct_contract_error(
            field_name="target trade_assessment.return_pct",
            actual_return_pct=trade_assessment.return_pct,
            expected_return_pct=(context.market_returns.episode_returns[0].strategy_return * 100.0),
            source_name="market_returns.episode_returns[0].strategy_return * 100",
        )
        if return_error is not None:
            violations.append(
                _BusinessContractViolation(
                    message=return_error,
                    repair_path=("trade_assessment", "return_pct"),
                )
            )

    baselines_by_horizon_hours = {
        baseline.horizon.total_seconds() / 3600.0: baseline
        for baseline in context.market_returns.horizon_baselines
    }
    for index, assessment in enumerate(evaluation.assessed_horizons):
        baseline = baselines_by_horizon_hours.get(assessment.horizon_hours)
        if baseline is None:
            violations.append(
                _BusinessContractViolation(
                    message=(
                        f"target assessed_horizons[{index}] has no deterministic horizon "
                        f"baseline for horizon_hours={assessment.horizon_hours:g}."
                    ),
                    repair_path=("assessed_horizons",),
                )
            )
            continue
        if baseline.status != "final" or baseline.strategy_return is None:
            violations.append(
                _BusinessContractViolation(
                    message=(
                        f"target assessed_horizons[{index}] must not assess horizon_hours="
                        f"{assessment.horizon_hours:g} because its deterministic horizon "
                        f"baseline status is {baseline.status!r}."
                    ),
                    repair_path=("assessed_horizons",),
                )
            )
            continue
        return_error = _return_pct_contract_error(
            field_name=f"target assessed_horizons[{index}].return_pct",
            actual_return_pct=assessment.return_pct,
            expected_return_pct=baseline.strategy_return * 100.0,
            source_name="the matching final horizon baseline strategy_return * 100",
        )
        if return_error is not None:
            violations.append(
                _BusinessContractViolation(
                    message=return_error,
                    repair_path=("assessed_horizons", index, "return_pct"),
                )
            )
    return tuple(violations)


def _return_pct_contract_error(
    *,
    field_name: str,
    actual_return_pct: float | None,
    expected_return_pct: float,
    source_name: str,
) -> str | None:
    if actual_return_pct is not None and isclose(
        actual_return_pct,
        expected_return_pct,
        rel_tol=0.0,
        abs_tol=_RETURN_PCT_ABS_TOLERANCE,
    ):
        return None
    return (
        f"{field_name} must equal {source_name}; "
        f"expected={expected_return_pct:.12g} actual={actual_return_pct!r}."
    )


def open_position_reflection_business_contract_errors(
    evaluation: OpenPositionEvaluationResult,
    *,
    expected_return_pct: float,
    expected_price_relations: tuple[ReflectionPriceRelationAssessment, ...],
    expected_price_triggers: tuple[ReflectionPriceTriggerAssessment, ...],
) -> tuple[str, ...]:
    """Return deterministic open-position errors that JSON shape cannot express."""

    return tuple(
        violation.message
        for violation in _open_position_reflection_business_contract_violations(
            evaluation,
            expected_return_pct=expected_return_pct,
            expected_price_relations=expected_price_relations,
            expected_price_triggers=expected_price_triggers,
        )
    )


def _open_position_reflection_business_contract_violations(
    evaluation: OpenPositionEvaluationResult,
    *,
    expected_return_pct: float,
    expected_price_relations: tuple[ReflectionPriceRelationAssessment, ...],
    expected_price_triggers: tuple[ReflectionPriceTriggerAssessment, ...],
) -> tuple[_BusinessContractViolation, ...]:
    violations: list[_BusinessContractViolation] = []
    return_error = _return_pct_contract_error(
        field_name="open-position trade_assessment.return_pct",
        actual_return_pct=evaluation.trade_assessment.return_pct,
        expected_return_pct=expected_return_pct,
        source_name="terminal_mark.strategy_return * 100",
    )
    if return_error is not None:
        violations.append(
            _BusinessContractViolation(
                message=return_error,
                repair_path=("trade_assessment", "return_pct"),
            )
        )
    if evaluation.watchlist_assessment.price_relations != expected_price_relations:
        expected = [
            {"level_id": item.level_id, "relation": item.relation}
            for item in expected_price_relations
        ]
        actual = [
            {"level_id": item.level_id, "relation": item.relation}
            for item in evaluation.watchlist_assessment.price_relations
        ]
        violations.append(
            _BusinessContractViolation(
                message=(
                    "open-position watchlist_assessment.price_relations must equal "
                    f"project-computed terminal-mark relations; expected={expected!r} "
                    f"actual={actual!r}."
                ),
                repair_path=("watchlist_assessment",),
            )
        )
    if evaluation.watchlist_assessment.price_triggers != expected_price_triggers:
        expected_triggers = [
            {
                "level_id": item.level_id,
                "trigger_kind": item.trigger_kind,
                "trigger_id": item.trigger_id,
                "status": item.status,
            }
            for item in expected_price_triggers
        ]
        actual_triggers = [
            {
                "level_id": item.level_id,
                "trigger_kind": item.trigger_kind,
                "trigger_id": item.trigger_id,
                "status": item.status,
            }
            for item in evaluation.watchlist_assessment.price_triggers
        ]
        violations.append(
            _BusinessContractViolation(
                message=(
                    "open-position watchlist_assessment.price_triggers must equal "
                    "project-computed completed-close trigger states; "
                    f"expected={expected_triggers!r} actual={actual_triggers!r}."
                ),
                repair_path=("watchlist_assessment",),
            )
        )
    for index, action in enumerate(evaluation.learning_decision.actions):
        if action.outcome != "emit_episode_memory_candidate":
            continue
        if action.payload.get("candidate_kind") != "delta":
            continue
        if action.payload.get("validation_basis") == "close_review":
            violations.append(
                _BusinessContractViolation(
                    message=(
                        "open-position learning_decision.actions"
                        f"[{index}].payload.validation_basis must not be 'close_review'; "
                        "the position has not closed."
                    ),
                    repair_path=("learning_decision",),
                )
            )
    return tuple(violations)


def validate_open_position_reflection_business_contract(
    evaluation: OpenPositionEvaluationResult,
    *,
    expected_return_pct: float,
    expected_price_relations: tuple[ReflectionPriceRelationAssessment, ...],
    expected_price_triggers: tuple[ReflectionPriceTriggerAssessment, ...],
) -> OpenPositionEvaluationResult:
    """Enforce deterministic facts before an open-position result is accepted."""

    violations = _open_position_reflection_business_contract_violations(
        evaluation,
        expected_return_pct=expected_return_pct,
        expected_price_relations=expected_price_relations,
        expected_price_triggers=expected_price_triggers,
    )
    if violations:
        raise ReflectionOutputContractError(
            " ".join(violation.message for violation in violations),
            repair_paths=tuple(violation.repair_path for violation in violations),
        )
    return evaluation


def parse_target_close_reflection_output(
    receipt: AgentRunReceipt,
    *,
    context: CloseEpisodeReflectionContext,
) -> TargetCloseReflectionEvaluationResult:
    evaluation = parse_target_close_reflection_output_for_scope(
        receipt,
        episode_id=context.episode.episode_id,
        target_key=context.episode.target_key,
    )
    return validate_target_close_reflection_business_contract(
        evaluation,
        expected_return_pct=(context.market_returns.episode_returns[0].strategy_return * 100.0),
    )


def target_close_reflection_business_contract_errors(
    evaluation: TargetCloseReflectionEvaluationResult,
    *,
    expected_return_pct: float,
) -> tuple[str, ...]:
    """Return deterministic target-close errors that JSON shape cannot express."""

    violation = _target_close_return_violation(
        evaluation,
        expected_return_pct=expected_return_pct,
    )
    return () if violation is None else (violation.message,)


def validate_target_close_reflection_business_contract(
    evaluation: TargetCloseReflectionEvaluationResult,
    *,
    expected_return_pct: float,
) -> TargetCloseReflectionEvaluationResult:
    """Enforce deterministic close-return facts before accepting a result."""

    violation = _target_close_return_violation(
        evaluation,
        expected_return_pct=expected_return_pct,
    )
    if violation is not None:
        raise ReflectionOutputContractError(
            violation.message,
            repair_paths=(violation.repair_path,),
        )
    return evaluation


def _target_close_return_violation(
    evaluation: TargetCloseReflectionEvaluationResult,
    *,
    expected_return_pct: float,
) -> _BusinessContractViolation | None:
    return_error = _return_pct_contract_error(
        field_name="target-close trade_assessment.return_pct",
        actual_return_pct=evaluation.trade_assessment.return_pct,
        expected_return_pct=expected_return_pct,
        source_name="market_returns.episode_returns[0].strategy_return * 100",
    )
    if return_error is None:
        return None
    return _BusinessContractViolation(
        message=return_error,
        repair_path=("trade_assessment", "return_pct"),
    )


def parse_target_close_reflection_output_for_scope(
    receipt: AgentRunReceipt,
    *,
    episode_id: str,
    target_key: str,
) -> TargetCloseReflectionEvaluationResult:
    """Parse a target-close result against its frozen episode identity."""

    required_fields = (
        "episode_id",
        "target_key",
        "watchlist_assessment",
        "learning_decision",
    )
    payload = _load_reflection_payload(
        receipt,
        required_fields=required_fields,
        excluded_fields=("assessed_horizons",),
    )
    normalized = _normalize_reflection_payload_object(
        payload,
        required_fields=required_fields,
        excluded_fields=("assessed_horizons",),
    )
    parsed_episode_id, parsed_target_key = _require_active_episode_scope(
        normalized,
        episode_id=episode_id,
        target_key=target_key,
    )
    trade_assessment = _normalize_trade_assessment(normalized.get("trade_assessment"))
    if trade_assessment is None:
        raise ReflectionOutputContractError("target close reflection requires trade_assessment.")
    review_decision = _require_review_decision(
        normalized["review_decision"],
        field_name="review_decision",
    )
    usage_quality = _normalize_usage_quality_payload(normalized)
    return TargetCloseReflectionEvaluationResult(
        episode_id=parsed_episode_id,
        target_key=parsed_target_key,
        thesis_assessment=_normalize_thesis_assessment(normalized["thesis_assessment"]),
        watchlist_assessment=_normalize_watchlist_assessment(normalized["watchlist_assessment"]),
        trade_assessment=trade_assessment,
        review_decision=review_decision,
        decision_rationale=_require_string(
            normalized["decision_rationale"],
            field_name="decision_rationale",
        ),
        learning_decision=parse_learning_decision_payload(
            normalized["learning_decision"],
            review_decision=review_decision,
            target_key=parsed_target_key,
        ),
        usage_quality_label=usage_quality["usage_quality_label"],
        usage_quality_summary=usage_quality["usage_quality_summary"],
        market_context_error_labels=usage_quality["market_context_error_labels"],
    )


def build_skipped_open_position_contract_evaluation(
    *,
    context: OpenPositionReflectionContext,
    failure_reason: str,
) -> OpenPositionEvaluationResult:
    review_page_path = open_position_review_page_path_for_context(context)
    rationale = (
        "Terminal open-position reflection skipped because the configured reflection "
        "agent did not return a usable final boxed JSON payload after bounded "
        "contract repair. No review or learning artifact should be written. "
        f"Failure: {failure_reason}"
    )
    return OpenPositionEvaluationResult(
        episode_id=context.episode.episode_id,
        target_key=context.episode.target_key,
        thesis_assessment=ReflectionThesisAssessment(
            verdict="unclear",
            summary_md=(
                "Reflection evaluation did not complete because the agent failed the "
                "final output contract."
            ),
        ),
        watchlist_assessment=ReflectionWatchlistAssessment(
            verdict="unclear",
            summary_md=(
                "Watchlist quality was not evaluated because the agent failed the "
                "final output contract."
            ),
            price_relations=price_relation_assessments(
                open_position_watchlist_price_facts(context)
            ),
            price_triggers=price_trigger_assessments(
                open_position_watchlist_trigger_facts(context)
            ),
        ),
        trade_assessment=ReflectionTradeAssessment(
            verdict="open",
            summary_md=(
                "Open-position trade quality was not evaluated because the agent "
                "failed the final output contract."
            ),
            return_pct=context.terminal_mark.strategy_return * 100.0,
        ),
        exposure_assessment=ReflectionExposureAssessment(
            summary_md=(
                "Open exposure was not assessed because the reflection agent "
                "failed the final output contract."
            ),
        ),
        review_decision="skip_review",
        decision_rationale=rationale,
        learning_decision=ReflectionLearningDecision(
            primary_outcome="no_learning_write",
            actions=(
                ReflectionLearningAction(
                    action_id=(f"learning:{context.episode.episode_id}:terminal_final_output_skip"),
                    outcome="no_learning_write",
                    rationale=rationale,
                    source_episode_id=context.episode.episode_id,
                    source_review_path=review_page_path,
                    effective_from=context.terminal_mark.replay_end_at,
                    error_attributions=(),
                    payload={},
                ),
            ),
        ),
        usage_quality_label="inconclusive",
        usage_quality_summary=(
            "Market-context usage was not evaluated because the reflection agent "
            "failed the final output contract."
        ),
        market_context_error_labels=(),
    )


def require_reflection_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ReflectionOutputContractError(f"{field_name} must be a datetime.")
    return value


def _load_reflection_payload(
    receipt: AgentRunReceipt,
    *,
    required_fields: tuple[str, ...] = (),
    excluded_fields: tuple[str, ...] = (),
) -> dict[str, object]:
    def _is_matching_reflection_payload(payload: dict[str, object]) -> bool:
        try:
            _normalize_reflection_payload_object(
                payload,
                required_fields=required_fields,
                excluded_fields=excluded_fields,
                enforce_text_integrity=False,
            )
        except ReflectionOutputContractError as exc:
            raise ValueError(str(exc)) from exc
        return True

    try:
        return load_first_boxed_json_object(
            final_boxed_answer=receipt.contract_output_text,
            final_summary=receipt.final_summary,
            fallback_payload_texts=receipt.fallback_payload_texts,
            payload_validator=_is_matching_reflection_payload,
            object_start_fields=(
                "assessed_horizons",
                "thesis_assessment",
                "trade_assessment",
                "review_decision",
                "decision_rationale",
                "learning_decision",
                "episode_id",
                "target_key",
            ),
            missing_error=(
                "Reflection agent did not return a boxed JSON payload. "
                f"Final summary was: {receipt.final_summary}"
            ),
            non_json_error="Reflection agent returned non-JSON boxed output.",
            non_object_error="Reflection agent must return one JSON object.",
        )
    except BoxedJsonPayloadError as exc:
        raise ReflectionOutputContractError(str(exc)) from exc


def _normalize_reflection_payload_object(
    payload: dict[str, object],
    *,
    required_fields: tuple[str, ...] = (),
    excluded_fields: tuple[str, ...] = (),
    enforce_text_integrity: bool = True,
) -> dict[str, object]:
    normalized_payload = dict(payload)
    trade_assessment = normalized_payload.get("trade_assessment")
    if "return_pct" in normalized_payload and isinstance(trade_assessment, dict):
        trade_payload = dict(trade_assessment)
        trade_payload.setdefault("return_pct", normalized_payload["return_pct"])
        normalized_payload["trade_assessment"] = trade_payload
        del normalized_payload["return_pct"]
    base_fields = {
        "assessed_horizons",
        "thesis_assessment",
        "trade_assessment",
        "review_decision",
        "decision_rationale",
    }
    optional_fields = {
        "usage_quality_label",
        "usage_quality_summary",
        "market_context_error_labels",
        "learning_decision",
    }
    expected_fields = (
        base_fields | set(required_fields) | (set(normalized_payload) & optional_fields)
    ) - set(excluded_fields)
    missing_fields = sorted(expected_fields - set(normalized_payload))
    unexpected_fields = sorted(set(normalized_payload) - expected_fields)
    if missing_fields or unexpected_fields:
        problems: list[str] = []
        if missing_fields:
            problems.append(f"missing field(s): {', '.join(missing_fields)}")
        if unexpected_fields:
            problems.append(f"unexpected field(s): {', '.join(unexpected_fields)}")
        raise ReflectionOutputContractError(
            f"Reflection agent returned the wrong payload shape; {'; '.join(problems)}."
        )
    text_violations = (
        _reflection_text_integrity_violations(normalized_payload)
        if enforce_text_integrity
        else ()
    )
    if text_violations:
        raise ReflectionOutputContractError(
            "Reflection text integrity violation: "
            + "; ".join(message for _, message in text_violations),
            repair_paths=tuple(path for path, _ in text_violations),
        )
    _normalize_usage_quality_payload(normalized_payload)
    return normalized_payload


def _reflection_text_integrity_violations(
    payload: object,
    *,
    path: ReflectionRepairPath = (),
) -> tuple[tuple[ReflectionRepairPath, str], ...]:
    violations: list[tuple[ReflectionRepairPath, str]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            violations.extend(
                _reflection_text_integrity_violations(value, path=(*path, key))
            )
        return tuple(violations)
    if isinstance(payload, list):
        for index, value in enumerate(payload):
            violations.extend(
                _reflection_text_integrity_violations(value, path=(*path, index))
            )
        return tuple(violations)
    if not isinstance(payload, str):
        return ()

    formatted_path = "/" + "/".join(str(part) for part in path)
    if "\ufffd" in payload:
        violations.append((path, f"{formatted_path} contains a replacement character"))
    if any(ord(character) < 32 and character not in "\n\r\t" for character in payload):
        violations.append((path, f"{formatted_path} contains a control character"))
    stripped = payload.rstrip()
    if stripped.endswith('"') and payload.count('"') % 2 == 1:
        violations.append((path, f"{formatted_path} ends with an unmatched quote"))
    return tuple(violations)


def _require_active_episode(
    payload: dict[str, object],
    *,
    context: EpisodeReflectionContext
    | OpenPositionReflectionContext
    | CloseEpisodeReflectionContext,
) -> tuple[str, str]:
    return _require_active_episode_scope(
        payload,
        episode_id=context.episode.episode_id,
        target_key=context.episode.target_key,
    )


def _require_active_episode_scope(
    payload: dict[str, object],
    *,
    episode_id: str,
    target_key: str,
) -> tuple[str, str]:
    parsed_episode_id = _require_string(payload["episode_id"], field_name="episode_id")
    if parsed_episode_id != episode_id:
        raise ReflectionOutputContractError("Reflection agent must return the active episode_id.")
    parsed_target_key = _require_string(payload["target_key"], field_name="target_key")
    if parsed_target_key != target_key:
        raise ReflectionOutputContractError("Reflection agent must return the active target_key.")
    return parsed_episode_id, parsed_target_key


def _normalize_horizon_assessments(
    value: object,
) -> tuple[ReflectionHorizonAssessment, ...]:
    if not isinstance(value, list) or not value:
        raise ReflectionOutputContractError("assessed_horizons must be a non-empty JSON array.")
    assessments: list[ReflectionHorizonAssessment] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ReflectionOutputContractError(
                f"assessed_horizons[{index}] must be a JSON object."
            )
        assessments.append(
            ReflectionHorizonAssessment(
                horizon_hours=_require_number(
                    item.get("horizon_hours"),
                    field_name=f"assessed_horizons[{index}].horizon_hours",
                ),
                return_pct=_require_number(
                    item.get("return_pct"),
                    field_name=f"assessed_horizons[{index}].return_pct",
                ),
                path_verdict=_require_path_verdict(
                    item.get("path_verdict"),
                    field_name=f"assessed_horizons[{index}].path_verdict",
                ),
                tradability_verdict=_require_tradability_verdict(
                    item.get("tradability_verdict"),
                    field_name=f"assessed_horizons[{index}].tradability_verdict",
                ),
                summary_md=_require_string(
                    item.get("summary_md"),
                    field_name=f"assessed_horizons[{index}].summary_md",
                ),
            )
        )
    return tuple(assessments)


def _normalize_thesis_assessment(value: object) -> ReflectionThesisAssessment:
    if not isinstance(value, dict):
        raise ReflectionOutputContractError("thesis_assessment must be a JSON object.")
    return ReflectionThesisAssessment(
        verdict=_require_thesis_verdict(
            value.get("verdict"),
            field_name="thesis_assessment.verdict",
        ),
        summary_md=_require_string(
            value.get("summary_md"),
            field_name="thesis_assessment.summary_md",
        ),
    )


def _normalize_trade_assessment(value: object) -> ReflectionTradeAssessment | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ReflectionOutputContractError("trade_assessment must be a JSON object or null.")
    return ReflectionTradeAssessment(
        verdict=_require_trade_verdict(
            value.get("verdict"),
            field_name="trade_assessment.verdict",
        ),
        summary_md=_require_string(
            value.get("summary_md"),
            field_name="trade_assessment.summary_md",
        ),
        return_pct=_optional_number(
            value.get("return_pct"),
            field_name="trade_assessment.return_pct",
        ),
    )


def _normalize_exposure_assessment(value: object) -> ReflectionExposureAssessment:
    if not isinstance(value, dict):
        raise ReflectionOutputContractError("exposure_assessment must be a JSON object.")
    required_fields = {"summary_md"}
    missing_fields = sorted(required_fields - set(value))
    unexpected_fields = sorted(set(value) - required_fields)
    if missing_fields or unexpected_fields:
        details: list[str] = []
        if missing_fields:
            details.append(f"missing fields: {', '.join(missing_fields)}")
        if unexpected_fields:
            details.append(f"unexpected fields: {', '.join(unexpected_fields)}")
        raise ReflectionOutputContractError("exposure_assessment " + "; ".join(details))
    return ReflectionExposureAssessment(
        summary_md=_require_string(
            value.get("summary_md"),
            field_name="exposure_assessment.summary_md",
        ),
    )


def _normalize_watchlist_assessment(value: object) -> ReflectionWatchlistAssessment:
    if not isinstance(value, dict):
        raise ReflectionOutputContractError("watchlist_assessment must be a JSON object.")
    return ReflectionWatchlistAssessment(
        verdict=_require_watchlist_verdict(
            value.get("verdict"),
            field_name="watchlist_assessment.verdict",
        ),
        summary_md=_require_string(
            value.get("summary_md"),
            field_name="watchlist_assessment.summary_md",
        ),
        price_relations=_normalize_price_relations(value.get("price_relations")),
        price_triggers=_normalize_price_triggers(value.get("price_triggers")),
    )


def _normalize_price_relations(
    value: object,
) -> tuple[ReflectionPriceRelationAssessment, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ReflectionOutputContractError(
            "watchlist_assessment.price_relations must be an array."
        )
    relations: list[ReflectionPriceRelationAssessment] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"level_id", "relation"}:
            raise ReflectionOutputContractError(
                "watchlist_assessment.price_relations"
                f"[{index}] must contain exactly level_id and relation."
            )
        relation = item.get("relation")
        if relation not in {"below", "within", "above"}:
            raise ReflectionOutputContractError(
                "watchlist_assessment.price_relations"
                f"[{index}].relation must be 'below', 'within', or 'above'."
            )
        relations.append(
            ReflectionPriceRelationAssessment(
                level_id=_require_string(
                    item.get("level_id"),
                    field_name=(f"watchlist_assessment.price_relations[{index}].level_id"),
                ),
                relation=cast(ReflectionPriceRelation, relation),
            )
        )
    return tuple(relations)


def _normalize_price_triggers(
    value: object,
) -> tuple[ReflectionPriceTriggerAssessment, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ReflectionOutputContractError(
            "watchlist_assessment.price_triggers must be an array."
        )
    triggers: list[ReflectionPriceTriggerAssessment] = []
    expected_fields = {"level_id", "trigger_kind", "trigger_id", "status"}
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise ReflectionOutputContractError(
                "watchlist_assessment.price_triggers"
                f"[{index}] must contain exactly level_id, trigger_kind, trigger_id, and status."
            )
        trigger_kind = item.get("trigger_kind")
        if trigger_kind not in {"refresh", "invalidation"}:
            raise ReflectionOutputContractError(
                "watchlist_assessment.price_triggers"
                f"[{index}].trigger_kind must be 'refresh' or 'invalidation'."
            )
        status = item.get("status")
        if status not in {"met", "not_met", "not_evaluable"}:
            raise ReflectionOutputContractError(
                "watchlist_assessment.price_triggers"
                f"[{index}].status must be 'met', 'not_met', or 'not_evaluable'."
            )
        triggers.append(
            ReflectionPriceTriggerAssessment(
                level_id=_require_string(
                    item.get("level_id"),
                    field_name=f"watchlist_assessment.price_triggers[{index}].level_id",
                ),
                trigger_kind=cast(ReflectionPriceTriggerKind, trigger_kind),
                trigger_id=_require_string(
                    item.get("trigger_id"),
                    field_name=f"watchlist_assessment.price_triggers[{index}].trigger_id",
                ),
                status=cast(ReflectionPriceTriggerStatus, status),
            )
        )
    return tuple(triggers)


def _normalize_usage_quality_payload(
    payload: dict[str, object],
) -> _UsageQualityPayload:
    label = _optional_usage_quality_label(
        payload.get("usage_quality_label"),
        field_name="usage_quality_label",
    )
    return {
        "usage_quality_label": label,
        "usage_quality_summary": _optional_usage_quality_summary(
            payload.get("usage_quality_summary"),
            label=label,
        ),
        "market_context_error_labels": _optional_usage_quality_error_labels(
            payload.get("market_context_error_labels"),
            usage_quality_label=label,
        ),
    }


def _optional_usage_quality_label(
    value: object,
    *,
    field_name: str,
) -> MarketContextUsageQualityLabel | None:
    if value is None:
        return None
    normalized = _require_string(value, field_name=field_name)
    for allowed_label in MARKET_CONTEXT_USAGE_QUALITY_LABELS:
        if normalized == allowed_label:
            return allowed_label
    raise ReflectionOutputContractError(
        f"{field_name} must be one of: {', '.join(MARKET_CONTEXT_USAGE_QUALITY_LABELS)}."
    )


def _optional_usage_quality_summary(
    value: object,
    *,
    label: MarketContextUsageQualityLabel | None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReflectionOutputContractError("usage_quality_summary must be a string or null.")
    normalized = value.strip()
    if not normalized and label not in {None, "not_applicable"}:
        raise ReflectionOutputContractError(
            "usage_quality_summary may be empty only when "
            "usage_quality_label is absent or not_applicable."
        )
    return normalized


def _optional_usage_quality_error_labels(
    value: object,
    *,
    usage_quality_label: MarketContextUsageQualityLabel | None,
) -> tuple[MarketContextUsageQualityLabel, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ReflectionOutputContractError("market_context_error_labels must be a JSON array.")
    if usage_quality_label == "not_applicable":
        return ()
    normalized: list[MarketContextUsageQualityLabel] = []
    seen: set[MarketContextUsageQualityLabel] = set()
    for index, item in enumerate(value):
        label = _optional_usage_quality_error_label(
            item,
            field_name=f"market_context_error_labels[{index}]",
            usage_quality_label=usage_quality_label,
        )
        if label is None:
            raise ReflectionOutputContractError(
                "market_context_error_labels must contain only strings."
            )
        if label in seen:
            continue
        seen.add(label)
        normalized.append(label)
    return tuple(normalized)


def _optional_usage_quality_error_label(
    value: object,
    *,
    field_name: str,
    usage_quality_label: MarketContextUsageQualityLabel | None,
) -> MarketContextUsageQualityLabel | None:
    if value is None:
        return None
    normalized = _require_string(value, field_name=field_name)
    for allowed_label in MARKET_CONTEXT_USAGE_QUALITY_LABELS:
        if normalized == allowed_label:
            return allowed_label
    if usage_quality_label not in {None, "not_applicable"}:
        return usage_quality_label
    return "inconclusive"


def _require_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ReflectionOutputContractError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ReflectionOutputContractError(f"{field_name} must not be blank.")
    return normalized


def _require_review_decision(
    value: object,
    *,
    field_name: str,
) -> ReflectionReviewDecision:
    normalized = _require_string(value, field_name=field_name)
    if normalized not in {"write_review", "skip_review"}:
        raise ReflectionOutputContractError(f"{field_name} must be write_review or skip_review.")
    return cast(ReflectionReviewDecision, normalized)


def _require_path_verdict(
    value: object,
    *,
    field_name: str,
) -> ReflectionPathVerdict:
    normalized = _require_string(value, field_name=field_name)
    if normalized not in {"favorable", "mixed", "adverse", "unclear"}:
        raise ReflectionOutputContractError(
            f"{field_name} must be favorable, mixed, adverse, or unclear."
        )
    return cast(ReflectionPathVerdict, normalized)


def _require_tradability_verdict(
    value: object,
    *,
    field_name: str,
) -> ReflectionTradabilityVerdict:
    normalized = _require_string(value, field_name=field_name)
    if normalized not in {"tradable", "mixed", "poor", "unclear"}:
        raise ReflectionOutputContractError(
            f"{field_name} must be tradable, mixed, poor, or unclear."
        )
    return cast(ReflectionTradabilityVerdict, normalized)


def _require_thesis_verdict(
    value: object,
    *,
    field_name: str,
) -> ReflectionThesisVerdict:
    normalized = _require_string(value, field_name=field_name)
    if normalized not in {"validated", "mixed", "invalidated", "unclear"}:
        raise ReflectionOutputContractError(
            f"{field_name} must be validated, mixed, invalidated, or unclear."
        )
    return cast(ReflectionThesisVerdict, normalized)


def _require_trade_verdict(
    value: object,
    *,
    field_name: str,
) -> ReflectionTradeVerdict:
    normalized = _require_string(value, field_name=field_name)
    if normalized not in {"win", "loss", "scratch", "missed", "not_taken", "open"}:
        raise ReflectionOutputContractError(
            f"{field_name} must be win, loss, scratch, missed, not_taken, or open."
        )
    return cast(ReflectionTradeVerdict, normalized)


def _require_watchlist_verdict(
    value: object,
    *,
    field_name: str,
) -> ReflectionWatchlistVerdict:
    normalized = _require_string(value, field_name=field_name)
    if normalized not in {"useful", "mixed", "stale", "missing", "unclear"}:
        raise ReflectionOutputContractError(
            f"{field_name} must be useful, mixed, stale, missing, or unclear."
        )
    return cast(ReflectionWatchlistVerdict, normalized)


def _require_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReflectionOutputContractError(f"{field_name} must be a number.")
    return float(value)


def _optional_number(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    return _require_number(value, field_name=field_name)


def open_position_review_page_path_for_context(
    context: OpenPositionReflectionContext,
) -> str:
    if context.review_kind == "open_position_material_update":
        if context.open_position_material_update_sequence is None:
            raise ReflectionOutputContractError(
                "open material-update reflection context is missing material-update metadata."
            )
        return (
            f"targets/{context.episode.target_key}/reviews/"
            f"{_format_review_path_timestamp(context.episode.opened_at)}_"
            f"{_format_review_path_timestamp(context.terminal_mark.replay_end_at)}_"
            "open_position_material_update_n"
            f"{context.open_position_material_update_sequence}.md"
        )
    return (
        f"targets/{context.episode.target_key}/reviews/"
        f"{_format_review_path_timestamp(context.episode.opened_at)}_"
        f"{_format_review_path_timestamp(context.terminal_mark.replay_end_at)}_"
        "open_position_horizon_"
        f"{_format_review_path_hour_label(context.review_horizon_hours)}.md"
    )


def _format_review_path_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%y%m%dT%H%M%S")


def _format_review_path_hour_label(hour: float) -> str:
    if float(hour).is_integer():
        return f"{int(hour)}h"
    return f"{format(hour, 'g')}h"


__all__ = [
    "ReflectionOutputContractError",
    "build_skipped_open_position_contract_evaluation",
    "open_position_reflection_business_contract_errors",
    "open_position_review_page_path_for_context",
    "parse_open_position_reflection_output",
    "parse_open_position_reflection_output_for_scope",
    "parse_shared_reflection_output",
    "parse_target_close_reflection_output",
    "parse_target_close_reflection_output_for_scope",
    "target_close_reflection_business_contract_errors",
    "validate_target_close_reflection_business_contract",
    "parse_target_reflection_output",
    "reflection_evaluation_schema",
    "reflection_learning_decision_schema",
    "reflection_output_schema",
    "require_reflection_datetime",
    "validate_open_position_reflection_business_contract",
]
