"""Code-owned schema definitions for Analysis payload contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from event_trader.contracts.execution_direction_policy import (
    ExecutionDirectionMode,
    allowed_role_if_already_short_for_execution_direction_mode,
    allowed_view_states_for_execution_direction_mode,
)
from event_trader.contracts.pm_review_reason import (
    PM_REVIEW_REASON_VALUES,
    analysis_pm_annotation_only_reasons,
    analysis_pm_escalation_reasons,
)

if TYPE_CHECKING:
    from event_trader.contracts.price_level_role import PriceLevelRole

_ANALYSIS_FINAL_PAYLOAD_FIELDS = (
    "target_key",
    "event_ids",
    "used_lesson_ids",
    "analysis_assessment",
)
_FORBIDDEN_ANALYSIS_FINAL_PAYLOAD_FIELDS = (
    "view_state_change",
    "view_state_unchanged_rationale_md",
    "decision_audit",
    "target_weight",
    "actual_target_weight",
    "current_target_weight",
    "requested_target_weight",
    "requested_state",
    "action",
    "trade_action",
    "position_action",
    "open",
    "close",
    "reverse",
    "resize",
)
_ANALYSIS_ASSESSMENT_FIELDS = (
    "assessment_id",
    "target_key",
    "business_at",
    "source_event_ids",
    "memory_write_receipt_ids",
    "as_if_flat_state",
    "as_if_flat_rationale_md",
    "if_flat_implication_md",
    "if_already_long_implication_md",
    "if_already_short_implication_md",
    "price_level_roles",
    "market_setup_dashboard_md",
    "key_claim_ids",
    "contested_prior_claim_ids",
    "missing_evidence_md",
    "pm_candidate_review_required",
    "pm_current_exposure_review_required",
    "pm_review_reasons",
    "confidence",
)
_ANALYSIS_ASSESSMENT_OPTIONAL_FIELDS = ("analysis_price_semantics",)
_ANALYSIS_ASSESSMENT_RUNTIME_OWNED_FIELDS = (
    "assessment_id",
    "target_key",
    "business_at",
    "memory_write_receipt_ids",
    "analysis_price_semantics",
    "source_event_ids",
    "price_level_roles",
)
_ANALYSIS_ASSESSMENT_LONG_ONLY_OPTIONAL_FIELDS = (
    "analysis_price_semantics",
    "if_already_short_implication_md",
)
_ANALYSIS_PRICE_SEMANTICS_FIELDS = (
    "target_key",
    "instrument_basis",
    "analysis_reference_price",
    "analysis_reference_at",
    "current_leg_start_price",
    "current_leg_end_price",
    "swing_high",
    "swing_low",
    "window_high",
    "window_low",
    "active_price_level_ids",
)
_PRICE_LEVEL_ROLE_FIELDS = (
    "level_id",
    "target_key",
    "value",
    "lower",
    "upper",
    "instrument_basis",
    "source_type",
    "source_event_ids",
    "source_refs",
    "role_if_flat",
    "role_if_already_long",
    "role_if_already_short",
    "path_context_required",
    "refresh_triggers",
    "invalidation_triggers",
    "confidence",
    "rationale_md",
)
_MATERIAL_LEVEL_ROLES = (
    "entry",
    "add",
    "hold_boundary",
    "de_risk_or_take_profit",
    "exit",
    "reverse_or_cover",
    "invalidates",
    "watch_only",
    "not_relevant",
)
_PRICE_LEVEL_SOURCE_TYPES = (
    "source_quoted",
    "market_bar_derived",
    "agent_hypothesis",
    "round_number",
)
_CONFIDENCE_VALUES = ("low", "medium", "high")
_EVENT_ID_PATTERN = "^[0-9a-f]{24}$"
_INVALID_LEGACY_PRICE_LEVEL_ROLE_SHAPE = (
    '{"price":522.24,"role":"swing_low_failure_level",'
    '"stance":"bearish_confirmation"}'
)


def analysis_final_payload_required_fields() -> tuple[str, ...]:
    return _ANALYSIS_FINAL_PAYLOAD_FIELDS


def forbidden_analysis_final_payload_fields() -> tuple[str, ...]:
    return _FORBIDDEN_ANALYSIS_FINAL_PAYLOAD_FIELDS


def analysis_assessment_required_fields(
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> tuple[str, ...]:
    if execution_direction_mode == "long_only":
        return tuple(
            field_name
            for field_name in _ANALYSIS_ASSESSMENT_FIELDS
            if field_name != "if_already_short_implication_md"
        )
    return _ANALYSIS_ASSESSMENT_FIELDS


def analysis_assessment_optional_fields(
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> tuple[str, ...]:
    if execution_direction_mode == "long_only":
        return _ANALYSIS_ASSESSMENT_LONG_ONLY_OPTIONAL_FIELDS
    return _ANALYSIS_ASSESSMENT_OPTIONAL_FIELDS


def analysis_assessment_runtime_owned_fields() -> tuple[str, ...]:
    return _ANALYSIS_ASSESSMENT_RUNTIME_OWNED_FIELDS


def analysis_assessment_precommit_defaults() -> dict[str, object]:
    return {"memory_write_receipt_ids": []}


def analysis_price_semantics_required_fields() -> tuple[str, ...]:
    return _ANALYSIS_PRICE_SEMANTICS_FIELDS


def price_level_role_required_fields() -> tuple[str, ...]:
    return _PRICE_LEVEL_ROLE_FIELDS


def price_level_role_draft_required_fields() -> tuple[str, ...]:
    runtime_owned = {"target_key", "value", "lower", "upper"}
    business_fields = tuple(
        field_name
        for field_name in price_level_role_required_fields()
        if field_name not in runtime_owned
    )
    return (*business_fields, "level")


@dataclass(frozen=True, slots=True)
class AnalysisOwnerDraftConstraints:
    """Runtime constraints applied only to model-owned owner-draft fields.

    The owner does not emit the top-level request identity or assessment identity.
    These constraints only bind the model-owned lesson, citation, and price-level
    fields to the admitted request.
    """

    target_key: str
    active_event_ids: tuple[str, ...]
    included_lesson_ids: tuple[str, ...]
    active_instrument_basis: str | None
    visible_event_ids: tuple[str, ...] = ()
    baseline_price_level_ids: tuple[str, ...] = ()


def price_level_role_json_schema(
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
    target_key: str | None = None,
    active_instrument_basis: str | None = None,
) -> dict[str, object]:
    """Return the canonical JSON schema for one persisted PriceLevelRole."""

    return _price_level_role_json_schema(
        execution_direction_mode=execution_direction_mode,
        target_key=target_key,
        active_instrument_basis=active_instrument_basis,
        draft=False,
    )


def price_level_role_draft_json_schema(
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
    active_instrument_basis: str | None = None,
    allowed_source_event_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    """Return the model-owned draft schema projected from the final contract."""

    return _price_level_role_json_schema(
        execution_direction_mode=execution_direction_mode,
        target_key=None,
        active_instrument_basis=active_instrument_basis,
        draft=True,
        allowed_source_event_ids=allowed_source_event_ids,
    )


def assemble_price_level_role_draft(
    draft: Mapping[str, object],
    *,
    target_key: str,
) -> dict[str, object]:
    """Attach only runtime-determined canonical fields to one model draft."""

    payload = dict(draft)
    if "target_key" in payload:
        raise ValueError("PriceLevelRole draft must not include runtime-owned target_key.")
    level = payload.pop("level", None)
    if not isinstance(level, Mapping):
        raise ValueError("PriceLevelRole draft level must be an object.")
    level_payload = dict(level)
    kind = level_payload.pop("kind", None)
    if kind == "scalar" and set(level_payload) == {"price"}:
        price = level_payload["price"]
        payload.update({"value": price, "lower": None, "upper": None})
    elif kind == "zone" and set(level_payload) == {"lower", "upper"}:
        lower = level_payload["lower"]
        upper = level_payload["upper"]
        payload.update({"value": None, "lower": lower, "upper": upper})
    else:
        raise ValueError("PriceLevelRole draft level must be one complete scalar or zone shape.")
    return {"target_key": target_key, **payload}


def price_level_actions_json_schema(
    *,
    execution_direction_mode: ExecutionDirectionMode,
    active_instrument_basis: str | None,
    baseline_price_level_ids: tuple[str, ...],
    allowed_source_event_ids: tuple[str, ...],
) -> dict[str, object]:
    """Return the model contract for deliberate changes to persisted price levels."""

    level_schema = price_level_role_draft_json_schema(
        execution_direction_mode=execution_direction_mode,
        active_instrument_basis=active_instrument_basis,
        allowed_source_event_ids=allowed_source_event_ids,
    )
    baseline_id_schema: dict[str, object] = {"enum": list(baseline_price_level_ids)}
    retained_or_retired = {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation", "baseline_level_id"],
        "properties": {
            "operation": {"enum": ["retain", "retire"]},
            "baseline_level_id": baseline_id_schema,
        },
    }
    replacement = {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation", "baseline_level_id", "level"],
        "properties": {
            "operation": {"const": "replace"},
            "baseline_level_id": baseline_id_schema,
            "level": level_schema,
        },
    }
    addition = {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation", "level"],
        "properties": {
            "operation": {"const": "add"},
            "level": level_schema,
        },
    }
    operations = (
        [addition]
        if not baseline_price_level_ids
        else [retained_or_retired, replacement, addition]
    )
    return {
        "type": "array",
        "items": {"oneOf": operations},
    }


def assemble_price_level_actions(
    actions: object,
    *,
    target_key: str,
    baseline_price_level_roles: tuple[PriceLevelRole, ...],
    allowed_source_event_ids: tuple[str, ...],
) -> list[dict[str, object]]:
    """Compile one complete, explicit level disposition into canonical roles."""

    if not isinstance(actions, list):
        raise ValueError("price_level_actions must be a list.")
    baseline_by_id = {level.level_id: level for level in baseline_price_level_roles}
    if len(baseline_by_id) != len(baseline_price_level_roles):
        raise ValueError("runtime baseline contains duplicate PriceLevelRole level_id values.")
    seen_baseline_ids: set[str] = set()
    final_roles: list[dict[str, object]] = []
    for index, raw_action in enumerate(actions):
        if not isinstance(raw_action, Mapping):
            raise ValueError(f"price_level_actions[{index}] must be an object.")
        action = dict(raw_action)
        operation = action.get("operation")
        if operation in {"retain", "retire", "replace"}:
            baseline_level_id = action.get("baseline_level_id")
            if not isinstance(baseline_level_id, str) or baseline_level_id not in baseline_by_id:
                raise ValueError(
                    f"price_level_actions[{index}].baseline_level_id is not in the "
                    "runtime baseline."
                )
            if baseline_level_id in seen_baseline_ids:
                raise ValueError(
                    f"price_level_actions contains duplicate disposition for {baseline_level_id!r}."
                )
            seen_baseline_ids.add(baseline_level_id)
            if operation == "retain":
                if set(action) != {"operation", "baseline_level_id"}:
                    raise ValueError("retain action must not modify a baseline PriceLevelRole.")
                final_roles.append(baseline_by_id[baseline_level_id].to_json_payload())
                continue
            if operation == "retire":
                if set(action) != {"operation", "baseline_level_id"}:
                    raise ValueError("retire action must contain only its baseline level identity.")
                continue
            if operation == "replace":
                level = action.get("level")
                if not isinstance(level, Mapping):
                    raise ValueError("replace action requires one complete level draft.")
                final_roles.append(
                    _assemble_new_price_level_action(
                        level,
                        target_key=target_key,
                        allowed_source_event_ids=allowed_source_event_ids,
                    )
                )
                continue
        if operation == "add":
            if set(action) != {"operation", "level"}:
                raise ValueError(
                    "add action requires exactly operation and one complete level draft."
                )
            level = action.get("level")
            if not isinstance(level, Mapping):
                raise ValueError("add action requires one complete level draft.")
            final_roles.append(
                _assemble_new_price_level_action(
                    level,
                    target_key=target_key,
                    allowed_source_event_ids=allowed_source_event_ids,
                )
            )
            continue
        raise ValueError(f"price_level_actions[{index}].operation is not supported.")
    missing = set(baseline_by_id).difference(seen_baseline_ids)
    if missing:
        raise ValueError(
            "price_level_actions must dispose every runtime baseline level exactly once; "
            f"missing={sorted(missing)!r}."
        )
    final_level_ids = [role.get("level_id") for role in final_roles]
    if len(set(final_level_ids)) != len(final_level_ids):
        raise ValueError(
            "price_level_actions produced duplicate final PriceLevelRole level_id values."
        )
    return final_roles


def _assemble_new_price_level_action(
    level: Mapping[str, object],
    *,
    target_key: str,
    allowed_source_event_ids: tuple[str, ...],
) -> dict[str, object]:
    assembled = assemble_price_level_role_draft(level, target_key=target_key)
    source_event_ids = assembled.get("source_event_ids")
    if not isinstance(source_event_ids, list) or not all(
        isinstance(event_id, str) for event_id in source_event_ids
    ):
        raise ValueError("new PriceLevelRole source_event_ids must be a list of strings.")
    invisible = set(source_event_ids).difference(allowed_source_event_ids)
    if invisible:
        raise ValueError(
            "new PriceLevelRole source_event_ids must be admitted by this Analysis pass; "
            f"invisible={sorted(invisible)!r}."
        )
    return assembled


def _price_level_role_json_schema(
    *,
    execution_direction_mode: ExecutionDirectionMode,
    target_key: str | None,
    active_instrument_basis: str | None,
    draft: bool,
    allowed_source_event_ids: tuple[str, ...] = (),
) -> dict[str, object]:

    allowed_roles = allowed_material_level_roles()
    allowed_short_roles = allowed_role_if_already_short_for_execution_direction_mode(
        execution_direction_mode
    )
    properties: dict[str, object] = {
        "level_id": _non_blank_string_schema(),
        "instrument_basis": _non_blank_string_schema(),
        "source_type": {
            "type": "string",
            "enum": list(allowed_price_level_source_types()),
        },
        "source_event_ids": _source_event_id_array_schema(
            allowed_source_event_ids=allowed_source_event_ids if draft else (),
        ),
        "source_refs": _non_blank_string_array_schema(),
        "role_if_flat": {"type": "string", "enum": list(allowed_roles)},
        "role_if_already_long": {"type": "string", "enum": list(allowed_roles)},
        "role_if_already_short": {
            "type": "string",
            "enum": list(
                allowed_roles if allowed_short_roles is None else allowed_short_roles
            ),
        },
        "path_context_required": {"type": "boolean"},
        "refresh_triggers": _non_blank_string_array_schema(),
        "invalidation_triggers": _non_blank_string_array_schema(),
        "confidence": {"type": "string", "enum": list(allowed_confidence_values())},
        "rationale_md": _non_blank_string_schema(),
    }
    if draft:
        required = price_level_role_draft_required_fields()
        level_schema: dict[str, object] = {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "price"],
                    "properties": {
                        "kind": {"const": "scalar"},
                        "price": {"type": "number"},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "lower", "upper"],
                    "properties": {
                        "kind": {"const": "zone"},
                        "lower": {"type": "number"},
                        "upper": {"type": "number"},
                    },
                },
            ]
        }
        properties = {
            **properties,
            "level": level_schema,
        }
    else:
        properties = {
            "level_id": _non_blank_string_schema(),
            "target_key": (
                {"const": target_key}
                if target_key is not None
                else _target_key_schema()
            ),
            "value": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "lower": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "upper": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            **properties,
        }
        level_shape_schema = {
            "anyOf": [
                {
                    "required": ["value", "lower", "upper"],
                    "properties": {
                        "value": {"type": "number"},
                        "lower": {"type": "null"},
                        "upper": {"type": "null"},
                    },
                },
                {
                    "required": ["value", "lower", "upper"],
                    "properties": {
                        "value": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                        "lower": {"type": "number"},
                        "upper": {"type": "number"},
                    },
                },
            ]
        }
        required = price_level_role_required_fields()
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": properties,
        "allOf": [
            {
                "if": {
                    "properties": {
                        "source_type": {
                            "enum": ["source_quoted", "market_bar_derived"]
                        }
                    },
                    "required": ["source_type"],
                },
                "then": {
                    "properties": {"source_event_ids": _event_id_array_schema(min_items=1)}
                },
            },
            {
                "if": {
                    "properties": {"source_type": {"const": "source_quoted"}},
                    "required": ["source_type"],
                },
                "then": {
                    "properties": {
                        "role_if_flat": {"const": "not_relevant"},
                        "role_if_already_long": {"const": "not_relevant"},
                        "role_if_already_short": {"const": "not_relevant"},
                        "path_context_required": {"const": False},
                        "refresh_triggers": {"maxItems": 0},
                        "invalidation_triggers": {"maxItems": 0},
                    }
                },
            },
            *(
                [
                    {
                        "if": _active_or_market_derived_level_condition(),
                        "then": {
                            "properties": {
                                "instrument_basis": {"const": active_instrument_basis}
                            }
                        },
                    }
                ]
                if active_instrument_basis is not None
                else []
            ),
            *([] if draft else [level_shape_schema]),
        ],
    }


def analysis_final_payload_json_schema(
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> dict[str, object]:
    """Return the agent-facing canonical Analysis final-payload schema."""

    return _analysis_payload_json_schema(
        execution_direction_mode=execution_direction_mode,
    )


def analysis_decision_payload_json_schema(
    *,
    constraints: AnalysisOwnerDraftConstraints | None = None,
) -> dict[str, object]:
    """Return the model-owned Analysis materiality-decision schema."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "used_lesson_ids",
            "assessment_decision",
            "assessment_decision_rationale_md",
        ],
        "properties": {
            "used_lesson_ids": _used_lesson_ids_schema(constraints),
            "assessment_decision": {
                "type": "string",
                "enum": ["emit_assessment", "no_material_assessment"],
            },
            "assessment_decision_rationale_md": _non_blank_string_schema(),
        },
    }


def analysis_assessment_draft_payload_json_schema(
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
    constraints: AnalysisOwnerDraftConstraints | None = None,
) -> dict[str, object]:
    """Return the non-null model-owned Assessment draft schema."""

    return _analysis_assessment_json_schema(
        execution_direction_mode=execution_direction_mode,
        owner_draft=True,
        constraints=constraints,
    )


def _analysis_payload_json_schema(
    *,
    execution_direction_mode: ExecutionDirectionMode,
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(analysis_final_payload_required_fields()),
        "properties": {
            "target_key": _target_key_schema(),
            "event_ids": _event_id_array_schema(),
            "used_lesson_ids": _used_lesson_ids_schema(None),
            "analysis_assessment": {
                "anyOf": [
                    {"type": "null"},
                    _analysis_assessment_json_schema(
                        execution_direction_mode=execution_direction_mode,
                        owner_draft=False,
                        constraints=None,
                    ),
                ]
            },
        },
    }


def _analysis_assessment_json_schema(
    *,
    execution_direction_mode: ExecutionDirectionMode,
    owner_draft: bool,
    constraints: AnalysisOwnerDraftConstraints | None,
) -> dict[str, object]:
    required_fields = analysis_assessment_required_fields(execution_direction_mode)
    if owner_draft:
        runtime_owned_fields = set(analysis_assessment_runtime_owned_fields())
        required_fields = tuple(
            field_name
            for field_name in required_fields
            if field_name not in runtime_owned_fields
        ) + ("supporting_event_ids", "price_level_actions")
    short_implication_schema: dict[str, object] = (
        {"type": "string"}
        if "if_already_short_implication_md"
        not in analysis_assessment_optional_fields(execution_direction_mode)
        else {"anyOf": [{"type": "string"}, {"type": "null"}]}
    )
    properties: dict[str, object] = {
        (
            "supporting_event_ids" if owner_draft else "source_event_ids"
        ): (
            _source_event_id_array_schema(
                allowed_source_event_ids=(
                    () if constraints is None else constraints.visible_event_ids
                )
            )
            if owner_draft
            else _assessment_source_event_ids_schema(constraints)
        ),
        "as_if_flat_state": {
            "type": "string",
            "enum": list(
                allowed_view_states_for_execution_direction_mode(execution_direction_mode)
            ),
        },
        "as_if_flat_rationale_md": _non_blank_string_schema(),
        "if_flat_implication_md": _non_blank_string_schema(),
        "if_already_long_implication_md": _non_blank_string_schema(),
        "if_already_short_implication_md": short_implication_schema,
        (
            "price_level_actions" if owner_draft else "price_level_roles"
        ): (
            price_level_actions_json_schema(
                execution_direction_mode=execution_direction_mode,
                active_instrument_basis=(
                    None if constraints is None else constraints.active_instrument_basis
                ),
                baseline_price_level_ids=(
                    () if constraints is None else constraints.baseline_price_level_ids
                ),
                allowed_source_event_ids=(
                    () if constraints is None else constraints.visible_event_ids
                ),
            )
            if owner_draft
            else {
                "type": "array",
                "items": price_level_role_json_schema(
                    execution_direction_mode=execution_direction_mode,
                    target_key=(None if constraints is None else constraints.target_key),
                    active_instrument_basis=(
                        None
                        if constraints is None
                        else constraints.active_instrument_basis
                    ),
                ),
            }
        ),
        "market_setup_dashboard_md": _non_blank_string_schema(),
        "key_claim_ids": _non_blank_string_array_schema(),
        "contested_prior_claim_ids": _non_blank_string_array_schema(),
        "missing_evidence_md": _non_blank_string_schema(),
        "pm_candidate_review_required": {"type": "boolean"},
        "pm_current_exposure_review_required": {"type": "boolean"},
        "pm_review_reasons": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": list(allowed_pm_review_reasons()),
            },
            "uniqueItems": True,
        },
        "confidence": {
            "type": "string",
            "enum": list(allowed_confidence_values()),
        },
    }
    if not owner_draft:
        properties = {
            "assessment_id": _non_blank_string_schema(),
            "target_key": _target_key_schema(),
            "business_at": {"type": "string", "format": "date-time"},
            **properties,
        }
        properties["memory_write_receipt_ids"] = {
            "type": "array",
            "items": _non_blank_string_schema(),
            "uniqueItems": True,
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required_fields),
        "properties": properties,
    }


def _target_key_schema() -> dict[str, object]:
    return {
        "type": "string",
        "minLength": 1,
        "pattern": r"^\S+$",
        "not": {"enum": [".", ".."]},
    }


def _non_blank_string_schema() -> dict[str, object]:
    return {"type": "string", "minLength": 1, "pattern": r"^\S(?:[\s\S]*\S)?$"}


def _non_blank_string_array_schema() -> dict[str, object]:
    return {
        "type": "array",
        "items": _non_blank_string_schema(),
        "uniqueItems": True,
    }


def _event_id_array_schema(*, min_items: int = 0) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "array",
        "items": {"type": "string", "pattern": _EVENT_ID_PATTERN},
        "uniqueItems": True,
    }
    if min_items:
        schema["minItems"] = min_items
    return schema


def _source_event_id_array_schema(
    *,
    allowed_source_event_ids: tuple[str, ...],
) -> dict[str, object]:
    schema = _event_id_array_schema()
    if allowed_source_event_ids:
        schema["items"] = {"enum": list(allowed_source_event_ids)}
    return schema


def _assessment_source_event_ids_schema(
    constraints: AnalysisOwnerDraftConstraints | None,
) -> dict[str, object]:
    schema = _event_id_array_schema(min_items=1)
    if constraints is None:
        return schema
    schema["allOf"] = [
        {"contains": {"const": event_id}}
        for event_id in constraints.active_event_ids
    ]
    return schema


def _used_lesson_ids_schema(
    constraints: AnalysisOwnerDraftConstraints | None,
) -> dict[str, object]:
    schema = _non_blank_string_array_schema()
    if constraints is not None:
        schema["items"] = {"enum": list(constraints.included_lesson_ids)}
    return schema


def _active_or_market_derived_level_condition() -> dict[str, object]:
    """Mirror the final Contract's active-level predicate in JSON Schema."""

    return {
        "anyOf": [
            {"properties": {"source_type": {"const": "market_bar_derived"}}},
            {"properties": {"role_if_flat": {"not": {"const": "not_relevant"}}}},
            {
                "properties": {
                    "role_if_already_long": {"not": {"const": "not_relevant"}}
                }
            },
            {
                "properties": {
                    "role_if_already_short": {"not": {"const": "not_relevant"}}
                }
            },
            {"properties": {"refresh_triggers": {"minItems": 1}}},
            {"properties": {"invalidation_triggers": {"minItems": 1}}},
            {"properties": {"path_context_required": {"const": True}}},
        ]
    }


def allowed_pm_review_reasons() -> tuple[str, ...]:
    return tuple(sorted(PM_REVIEW_REASON_VALUES))


def allowed_material_level_roles() -> tuple[str, ...]:
    return _MATERIAL_LEVEL_ROLES


def allowed_price_level_source_types() -> tuple[str, ...]:
    return _PRICE_LEVEL_SOURCE_TYPES


def allowed_confidence_values() -> tuple[str, ...]:
    return _CONFIDENCE_VALUES


def allowed_analysis_view_states() -> tuple[str, ...]:
    return allowed_view_states_for_execution_direction_mode("long_short")


def invalid_legacy_price_level_role_shape() -> str:
    return _INVALID_LEGACY_PRICE_LEVEL_ROLE_SHAPE


def analysis_final_payload_contract_markdown(
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> str:
    return (
        "Analysis final payload output protocol:\n"
        "- Return exactly one JSON object wrapped in \\boxed{...}; no text before "
        "or after the boxed JSON.\n\n"
        f"{analysis_final_payload_requirements_markdown(execution_direction_mode=execution_direction_mode)}"
    )


def analysis_final_payload_requirements_markdown(
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> str:
    """Render provider-neutral final-payload business requirements."""

    return _analysis_final_payload_requirements_markdown(
        execution_direction_mode=execution_direction_mode,
        assessment_contract_md=analysis_assessment_contract_markdown(
            execution_direction_mode=execution_direction_mode
        ),
    )


def analysis_decision_requirements_markdown() -> str:
    """Render the model-owned materiality-decision contract."""

    return (
        "Analysis decision contract:\n"
        "- Required top-level fields, exactly: used_lesson_ids, "
        "assessment_decision, assessment_decision_rationale_md.\n"
        "- used_lesson_ids: IDs from visible shared lessons, or [].\n"
        "- assessment_decision: emit_assessment or no_material_assessment.\n"
        "- assessment_decision_rationale_md: non-blank explanation of the owner's "
        "materiality decision, including why any contrary advisory expert "
        "recommendation was rejected.\n"
    )


def _analysis_final_payload_requirements_markdown(
    *,
    execution_direction_mode: ExecutionDirectionMode,
    assessment_contract_md: str,
) -> str:

    return (
        "Analysis final payload contract:\n"
        "- Required top-level fields, exactly: "
        f"{_csv(analysis_final_payload_required_fields())}.\n"
        "- Active target execution_direction_mode: "
        f"{execution_direction_mode}.\n"
        "- target_key: active target key string.\n"
        "- event_ids: active request event_id strings, same order, no additions.\n"
        "- used_lesson_ids: IDs from visible shared lessons, or [].\n"
        "- analysis_assessment: null only when no material market/thesis "
        "assessment is emitted; otherwise a full AnalysisAssessment object.\n"
        "- Do not include outcome, view_state_change, decision_audit, target_weight, "
        "requested_state, requested_target_weight, action, or runtime-derived fields. "
        "The runtime derives no_update or memory_updated from actual tool writes.\n\n"
        f"{assessment_contract_md}"
    )


def analysis_assessment_contract_markdown(
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> str:
    return _analysis_assessment_contract_markdown(
        execution_direction_mode=execution_direction_mode,
        owner_draft=False,
    )


def analysis_assessment_draft_contract_markdown(
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> str:
    return _analysis_assessment_contract_markdown(
        execution_direction_mode=execution_direction_mode,
        owner_draft=True,
    )


def _analysis_assessment_contract_markdown(
    *,
    execution_direction_mode: ExecutionDirectionMode,
    owner_draft: bool,
) -> str:
    allowed_states = allowed_view_states_for_execution_direction_mode(
        execution_direction_mode
    )
    short_role_clause = _role_if_already_short_contract_clause(
        execution_direction_mode
    )
    if owner_draft:
        optional_fields = (
            "- Optional fields: if_already_short_implication_md.\n"
            if execution_direction_mode == "long_only"
            else "- Optional fields: none.\n"
        )
    else:
        optional_fields = (
            "- Optional fields: analysis_price_semantics, "
            "if_already_short_implication_md.\n"
            if execution_direction_mode == "long_only"
            else "- Optional fields: analysis_price_semantics.\n"
        )
    short_implication_clause = (
        "- if_already_short_implication_md: omit it or set it to null for "
        "execution_direction_mode long_only.\n"
        if execution_direction_mode == "long_only"
        else "- if_already_short_implication_md: required markdown for the "
        "already-short branch.\n"
    )
    required_fields = analysis_assessment_required_fields(execution_direction_mode)
    if owner_draft:
        runtime_owned_fields = set(analysis_assessment_runtime_owned_fields())
        required_fields = tuple(
            field_name
            for field_name in required_fields
            if field_name not in runtime_owned_fields
        ) + ("supporting_event_ids", "price_level_actions")
    contract_title = (
        "AnalysisAssessment owner-draft contract"
        if owner_draft
        else "AnalysisAssessment contract"
    )
    runtime_field_contract = (
        ""
        if owner_draft
        else (
            "- analysis_price_semantics: optional runtime-derived deterministic truth "
            "from price_level_roles plus market context.\n"
            "- assessment_id: non-blank canonical assessment identity.\n"
            "- target_key: active target key.\n"
            "- business_at: timezone-aware ISO timestamp.\n"
            "- memory_write_receipt_ids: array of memory write receipt IDs, or [].\n"
        )
    )
    price_level_role_contract = _price_level_role_contract_markdown(
        execution_direction_mode=execution_direction_mode,
        draft=owner_draft,
    )
    ownership_fields_contract = (
        "- supporting_event_ids: admitted evidence IDs visible to this frozen "
        "Analysis pass, or []. Runtime always adds active request event IDs to "
        "the canonical source_event_ids.\n"
        "- price_level_actions: explicitly retain, retire, replace, or add every "
        "baseline PriceLevelRole. Retain actions preserve the runtime-owned "
        "canonical level and its provenance unchanged.\n"
        if owner_draft
        else "- source_event_ids: non-empty array covering every active request event_id.\n"
        "- price_level_roles: array of full PriceLevelRole objects, or [].\n"
    )
    return (
        f"{contract_title}:\n"
        "- Required fields: "
        f"{_csv(required_fields)}.\n"
        f"{runtime_field_contract}"
        f"{ownership_fields_contract}"
        "- as_if_flat_state: one of "
        f"{_csv(allowed_states)} for execution_direction_mode "
        f"{execution_direction_mode}.\n"
        "- strong_long is valid when loaded evidence and visible market structure "
        "show confirmed trend continuation, breakout acceptance, or thesis "
        "acceleration; do not downgrade solely because price is near a window "
        "high.\n"
        "- as_if_flat_rationale_md and implication fields: exposure-blind markdown.\n"
        f"{short_implication_clause}"
        f"{optional_fields}"
        "- market_setup_dashboard_md: exposure-blind Market Setup Dashboard markdown.\n"
        "- key_claim_ids and contested_prior_claim_ids: arrays of strings, or [].\n"
        "- missing_evidence_md: markdown describing unsettled evidence.\n"
        "- pm_candidate_review_required: boolean. Set true only when analysis is "
        "explicitly requesting candidate-style PM review from an as-if-flat "
        "starting point.\n"
        "- pm_current_exposure_review_required: boolean. Set true only when "
        "analysis is explicitly requesting current-exposure PM review.\n"
        "- pm_review_reasons: array using only "
        f"{_csv(allowed_pm_review_reasons())}.\n"
        "- Analysis-origin PMReviewRequest materialization should ordinarily follow "
        "pm_candidate_review_required and pm_current_exposure_review_required. If "
        "those flags are missing but a non-flat live position later matches "
        "actionable role_if_already_long/short levels, runtime may conservatively "
        "promote the assessment into current-exposure PM review.\n"
        "- annotation-only pm_review_reasons may be recorded for explanation "
        "without by themselves summoning downstream PM judgment.\n"
        "- Standalone annotation-only reasons are "
        f"{_csv(analysis_pm_annotation_only_reasons())}; by themselves they do not "
        "materialize an analysis-origin PMReviewRequest.\n"
        "- If pm_review_reasons includes any PM escalation reason, at least one of "
        "the PM trigger booleans must be true.\n"
        "- Use PM escalation reasons only when downstream PM judgment is actually "
        "needed: "
        f"{_csv(analysis_pm_escalation_reasons())}.\n"
        "- Use [\"none\"] or [] only when no PM explanation or escalation reason "
        "applies.\n"
        "- confidence: one of "
        f"{_csv(allowed_confidence_values())}.\n"
        "- Do not include PortfolioState, actual target_weight, entry price, PnL, "
        "MFE/MAE, PMDecision, ExecutionRecord, or other "
        "exposure-aware facts.\n\n"
        f"{price_level_role_contract}\n"
        f"{short_role_clause}"
    )


def analysis_price_semantics_contract_markdown() -> str:
    return (
        "AnalysisPriceSemantics contract:\n"
        "- Required fields, exactly: "
        f"{_csv(analysis_price_semantics_required_fields())}.\n"
        "- target_key: active target key matching the parent AnalysisAssessment.\n"
        "- instrument_basis: non-blank canonical price basis label. For target "
        "market-bar semantics, use the market symbol only; do not append prose "
        "such as bar, 1h bar, or bar close.\n"
        "- analysis_reference_price: finite numeric reference price.\n"
        "- analysis_reference_at: timezone-aware ISO timestamp.\n"
        "- current_leg_start_price, current_leg_end_price, swing_high, swing_low, "
        "window_high, and window_low: finite numbers or null.\n"
        "- active_price_level_ids: unique non-blank level_id strings from the "
        "parent price_level_roles, or []."
    )


def price_level_role_contract_markdown(
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> str:
    return _price_level_role_contract_markdown(
        execution_direction_mode=execution_direction_mode,
        draft=False,
    )


def price_level_role_draft_contract_markdown(
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> str:
    """Render the model-owned projection of the final PriceLevelRole contract."""

    return _price_level_role_contract_markdown(
        execution_direction_mode=execution_direction_mode,
        draft=True,
    )


def _price_level_role_contract_markdown(
    *,
    execution_direction_mode: ExecutionDirectionMode,
    draft: bool,
) -> str:
    allowed_short_roles = allowed_role_if_already_short_for_execution_direction_mode(
        execution_direction_mode
    )
    short_role_text = (
        f"role_if_already_short: must be {_csv(allowed_short_roles)} for "
        f"execution_direction_mode {execution_direction_mode}."
        if allowed_short_roles is not None
        else "role_if_already_short: one of "
        f"{_csv(allowed_material_level_roles())}."
    )
    required_fields = (
        price_level_role_draft_required_fields()
        if draft
        else price_level_role_required_fields()
    )
    ownership_contract = (
        "- target_key is runtime-owned and must not be emitted.\n"
        "- level is required and is exactly one nested shape: {kind: scalar, price: "
        "number} or {kind: zone, lower: number, upper: number}. "
        "Runtime deterministically assembles canonical value/lower/upper null slots.\n"
        if draft
        else "- Use value for one numeric level, or lower and upper together for a zone.\n"
    )
    contract_name = "PriceLevelRole draft contract" if draft else "PriceLevelRole contract"
    full_contract_name = "PriceLevelRole draft" if draft else "PriceLevelRole"
    return (
        f"{contract_name}:\n"
        f"- {'Required common business fields' if draft else 'Required fields, exactly'}: "
        f"{_csv(required_fields)}.\n"
        f"{ownership_contract}"
        "- source_type: one of "
        f"{_csv(allowed_price_level_source_types())}.\n"
        "- instrument_basis: use a canonical machine label. For target "
        "market_bar_derived levels, use the market symbol only; do not append "
        "bar, timeframe, or close prose.\n"
        "- An actionable level must use the active tradable instrument basis. "
        "A source_quoted numeric level may remain only as informational context: "
        "every role must be not_relevant, "
        "refresh_triggers and invalidation_triggers must be empty, and "
        "path_context_required must be false. Never relabel its numeric value as "
        "the active instrument. If the quote identifies a tradable setup, emit a "
        "separate active-basis level supported by visible target market bars.\n"
        "- source_event_ids: required for source_quoted or market_bar_derived "
        "levels; may be [] for agent_hypothesis or round_number. Each entry "
        "must be a unique 24-character lowercase hexadecimal admitted-evidence "
        "ID. Do not use pivot labels, URLs, or source_refs as event IDs.\n"
        "- role_if_flat and role_if_already_long: each one of "
        f"{_csv(allowed_material_level_roles())}.\n"
        f"- {short_role_text}\n"
        "- Use role_if_already_long=add for either pullback adds or confirmed "
        "breakout/momentum-continuation adds. A high or window-high level is not "
        "automatically de_risk_or_take_profit.\n"
        "- When one area can support both breakout add and failed-breakout "
        "de-risk paths, express the paths with distinct levels or explicit "
        "path_context_required triggers instead of collapsing them into one "
        "generic high-level role.\n"
        "- path_context_required: boolean.\n"
        "- refresh_triggers and invalidation_triggers: arrays of strings, or [].\n"
        "- confidence: one of "
        f"{_csv(allowed_confidence_values())}.\n"
        "- rationale_md: markdown rationale with loaded evidence support.\n"
        "- Incomplete legacy objects such as "
        f"{invalid_legacy_price_level_role_shape()} are invalid; replace them "
        f"with the full {full_contract_name} contract."
    )


def _csv(values: tuple[str, ...]) -> str:
    return ", ".join(values)


def _role_if_already_short_contract_clause(
    execution_direction_mode: ExecutionDirectionMode,
) -> str:
    allowed_short_roles = allowed_role_if_already_short_for_execution_direction_mode(
        execution_direction_mode
    )
    if allowed_short_roles is None:
        return ""
    return (
        "- Long-only direction policy: because this target cannot open or hold "
        "short exposure, every price_level_roles[].role_if_already_short must be "
        f"{_csv(allowed_short_roles)}.\n"
    )


__all__ = [
    "allowed_analysis_view_states",
    "allowed_confidence_values",
    "allowed_material_level_roles",
    "allowed_pm_review_reasons",
    "allowed_price_level_source_types",
    "analysis_assessment_contract_markdown",
    "analysis_assessment_draft_contract_markdown",
    "analysis_assessment_draft_payload_json_schema",
    "analysis_assessment_optional_fields",
    "analysis_assessment_required_fields",
    "analysis_assessment_runtime_owned_fields",
    "analysis_decision_payload_json_schema",
    "analysis_decision_requirements_markdown",
    "analysis_final_payload_contract_markdown",
    "analysis_final_payload_json_schema",
    "analysis_final_payload_requirements_markdown",
    "analysis_final_payload_required_fields",
    "analysis_assessment_precommit_defaults",
    "analysis_price_semantics_contract_markdown",
    "analysis_price_semantics_required_fields",
    "forbidden_analysis_final_payload_fields",
    "invalid_legacy_price_level_role_shape",
    "price_level_role_contract_markdown",
    "price_level_role_draft_contract_markdown",
    "price_level_role_draft_json_schema",
    "assemble_price_level_role_draft",
    "assemble_price_level_actions",
    "price_level_actions_json_schema",
    "price_level_role_json_schema",
    "price_level_role_required_fields",
]
