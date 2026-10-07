"""Project-owned structured output schema for PMReview decisions."""

from __future__ import annotations

from event_trader.pm_review.contracts import (
    PMReviewInput,
    allowed_requested_states_for_execution_direction_mode,
)

_TARGETED_REPAIR_FIELDS = (
    "cited_event_ids",
    "cited_market_bar_ids",
    "hold_reason_code",
)


def _required_hold_reason_code_schema() -> dict[str, object]:
    return {
        "type": "string",
        "minLength": 1,
        "pattern": "^[a-z][a-z0-9_]*$",
    }


def _nullable(schema: dict[str, object]) -> dict[str, object]:
    return {"anyOf": [schema, {"type": "null"}]}


def _visible_id_array_schema(values: tuple[str, ...]) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "array",
        "uniqueItems": True,
    }
    if values:
        schema["items"] = {"type": "string", "enum": list(values)}
    else:
        schema["maxItems"] = 0
    return schema


def _draft_id_array_schema() -> dict[str, object]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "uniqueItems": True,
    }


def _pm_decision_draft_field_schemas(
    *,
    pm_review_input: PMReviewInput,
) -> dict[str, dict[str, object]]:
    allowed_states = allowed_requested_states_for_execution_direction_mode(
        pm_review_input.execution_direction_mode
    )
    state_schema: dict[str, object] = {
        "type": "string",
        "enum": list(allowed_states),
    }
    return {
        "pm_review_request_id": {
            "type": "string",
            "const": pm_review_input.pm_review_request_id,
        },
        "target_key": {
            "type": "string",
            "const": pm_review_input.target_key,
        },
        "business_at": {"type": "string", "minLength": 1},
        "requested_state": state_schema,
        "rationale_md": {"type": "string", "minLength": 1},
        "cited_event_ids": _draft_id_array_schema(),
        "cited_market_bar_ids": _draft_id_array_schema(),
        "fallback_state_if_clamped": _nullable(dict(state_schema)),
        "fallback_rationale_md": _nullable(
            {"type": "string", "minLength": 1}
        ),
        "hold_reason_code": _nullable(_required_hold_reason_code_schema()),
    }


def pm_decision_draft_schema(
    *,
    pm_review_input: PMReviewInput,
) -> dict[str, object]:
    """Project the PMDecisionDraft business contract into structured inference."""

    field_schemas = _pm_decision_draft_field_schemas(
        pm_review_input=pm_review_input
    )
    allowed_states = allowed_requested_states_for_execution_direction_mode(
        pm_review_input.execution_direction_mode
    )
    schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "pm_review_request_id",
            "target_key",
            "business_at",
            "requested_state",
            "rationale_md",
            "cited_event_ids",
            "cited_market_bar_ids",
            "fallback_state_if_clamped",
            "fallback_rationale_md",
            "hold_reason_code",
        ],
        "properties": field_schemas,
    }
    schema["allOf"] = [
        {
            "oneOf": [
                {
                    "properties": {
                        "fallback_state_if_clamped": {"type": "null"},
                        "fallback_rationale_md": {"type": "null"},
                    }
                },
                {
                    "properties": {
                        "fallback_state_if_clamped": {
                            "type": "string",
                            "enum": list(allowed_states),
                        },
                        "fallback_rationale_md": {
                            "type": "string",
                            "minLength": 1,
                        },
                    }
                },
            ]
        },
    ]
    return schema


def pm_decision_draft_repair_schema(
    *,
    pm_review_input: PMReviewInput,
    repair_fields: tuple[str, ...],
) -> dict[str, object]:
    """Return the narrow structured schema for a targeted PMDecisionDraft repair."""

    if not repair_fields:
        raise ValueError("PMDecisionDraft targeted repair fields must not be empty.")
    if len(set(repair_fields)) != len(repair_fields):
        raise ValueError("PMDecisionDraft targeted repair fields must not repeat.")
    unsupported = tuple(
        field_name
        for field_name in repair_fields
        if field_name not in _TARGETED_REPAIR_FIELDS
    )
    if unsupported:
        raise ValueError(
            "PMDecisionDraft targeted repair contains unsupported field(s): "
            f"{', '.join(unsupported)}."
        )
    visible_id_schemas = {
        "cited_event_ids": _visible_id_array_schema(
            tuple(record.event_id for record in pm_review_input.visible_evidence)
        ),
        "cited_market_bar_ids": _visible_id_array_schema(
            tuple(
                f"bar:{bar.start_at.isoformat()}:{bar.end_at.isoformat()}"
                for bar in pm_review_input.visible_market_bars
            )
        ),
    }
    repair_schemas = {
        field_name: (
            _required_hold_reason_code_schema()
            if field_name == "hold_reason_code"
            else visible_id_schemas[field_name]
        )
        for field_name in repair_fields
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(repair_fields),
        "properties": repair_schemas,
    }


__all__ = [
    "pm_decision_draft_repair_schema",
    "pm_decision_draft_schema",
]
