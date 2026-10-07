"""Provider-neutral PMReview prompt and prompt-budget policy."""

from __future__ import annotations

import json
from typing import Literal

from event_trader.contracts.price_level_role import PriceLevelRole
from event_trader.pm_review.contracts import (
    PMReviewContractError,
    PMReviewInput,
    parse_pm_review_input,
)
from event_trader.pm_review.errors import PMReviewAgentRuntimeError

_PM_REVIEW_PROMPT_FIELD_REPORT_LIMIT = 8
_PM_REVIEW_INPUT_MARKER = "PMReviewInput:\n"
_PM_REVIEW_REPAIR_MARKER = "PMReviewContractRepair:\n"
_PM_REVIEW_FOCUS_LEVEL_ROLES = frozenset(
    {
        "add",
        "hold_boundary",
        "de_risk_or_take_profit",
        "exit",
        "reverse_or_cover",
        "invalidates",
    }
)

type PMReviewOutputProtocol = Literal["boxed_json", "structured_tool"]

PM_REVIEW_OUTPUT_SCHEMA_PROMPT = """\
Return exactly one JSON object wrapped in \\boxed{...}. Do not include text before
or after the boxed JSON.

The JSON object must contain exactly these fields:
- pm_review_request_id: copied from PMReviewInput.pm_review_request_id
- target_key: copied from PMReviewInput.target_key
- business_at: copied from PMReviewInput.business_at as an ISO timestamp
- requested_state: one of the strings listed in PMReviewInput.allowed_requested_states
- rationale_md: concise markdown rationale for the requested_state
- cited_event_ids: visible event ids you relied on
- cited_market_bar_ids: copied exactly from PMReviewInput.visible_market_bars[].bar_id
- fallback_state_if_clamped: explicit fallback state, or null
- fallback_rationale_md: markdown rationale for the fallback state, or null
- hold_reason_code: optional compact lower_snake_case hold/stay reason code, or null

Hard rules:
- Do not output requested_target_weight. The system derives weight from requested_state.
- Do not echo actual_current_state or actual_target_weight_before.
- Do not output prior_position_context or any execution, portfolio, or risk-gate objects.
- Do not include reflection, self-review, learning-candidate, or critique prose.
- Do not add extra fields.
- If PMReviewInput.current_exposure_required is true, review_reasons includes
  current_exposure_pressure, risk_reward_compression, or invalidation_touched,
  and you keep requested_state equal to actual_current_state, you must provide a
  non-null hold_reason_code and explain in rationale_md why no de-risk, reduce,
  or exit action is warranted despite that risk review.

If you keep exposure unchanged or stay flat, requested_state should still be the actual
PM choice. If the mandatory hold condition above does not apply, hold_reason_code may
be null.
Never return a requested_state outside PMReviewInput.allowed_requested_states.
Never construct a market-bar id from timestamps. Copy only a displayed bar_id exactly.

Treat instrument labels and price scales as binding. If an analysis level and a visible
market reference have inconsistent instrument labels or clearly incompatible scales,
do not claim price crossed, held, or sits inside that level. State the unresolved
basis/scale mismatch and use only conclusions that do not depend on that comparison.

If you provide fallback_state_if_clamped, you must also provide fallback_rationale_md.
If you provide fallback_rationale_md, you must also provide fallback_state_if_clamped.
"""


def render_pm_review_business_task_prompt(pm_review_input: PMReviewInput) -> str:
    """Render provider-neutral PMReview business context for one input."""

    return "\n".join(
        (
            _pm_review_role_prompt(),
            "",
            _pm_review_thin_operating_boundary_prompt(),
            "",
            "Use PMReviewInput as the deterministic source of truth for this PM judgment.",
            "Do not invent hidden state, execution objects, requested_target_weight, "
            "or self-review fields.",
            "",
            "Citations must come only from PMReviewInput.visible_evidence and "
            "PMReviewInput.visible_market_bars.",
            "",
            _pm_review_decision_focus_prompt(pm_review_input),
            "",
            "PMReviewInput:",
            json.dumps(
                pm_review_input.to_json_payload(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        )
    )


def render_pm_review_task_prompt(
    pm_review_input: PMReviewInput,
    *,
    output_protocol: PMReviewOutputProtocol,
) -> str:
    """Render PMReview business context plus the selected runtime output protocol."""

    business_prompt = render_pm_review_business_task_prompt(pm_review_input)
    if output_protocol == "structured_tool":
        return business_prompt
    if output_protocol == "boxed_json":
        return f"{business_prompt}\n\n{PM_REVIEW_OUTPUT_SCHEMA_PROMPT}"
    raise PMReviewAgentRuntimeError(
        f"unsupported PMReview output protocol: {output_protocol!r}."
    )


def parse_pm_review_input_from_task_prompt(task_description: str) -> PMReviewInput:
    """Recover the project-owned PMReviewInput embedded in a rendered task prompt."""

    payload = extract_pm_review_input_payload_from_task_prompt(task_description)
    try:
        return parse_pm_review_input(payload)
    except PMReviewContractError as exc:
        raise PMReviewAgentRuntimeError(
            "PMReview task description contains invalid PMReviewInput JSON."
        ) from exc


def extract_pm_review_input_payload_from_task_prompt(
    task_description: str,
) -> dict[str, object]:
    """Recover the raw PMReviewInput JSON envelope without applying its current schema."""

    marker_index = task_description.find(_PM_REVIEW_INPUT_MARKER)
    if marker_index < 0:
        raise PMReviewAgentRuntimeError(
            "PMReview task description does not contain the PMReviewInput marker."
        )
    json_text = task_description[marker_index + len(_PM_REVIEW_INPUT_MARKER) :].lstrip()
    try:
        payload, _ = json.JSONDecoder().raw_decode(json_text)
        if not isinstance(payload, dict):
            raise PMReviewAgentRuntimeError(
                "PMReview task description PMReviewInput must be a JSON object."
            )
        return payload
    except json.JSONDecodeError as exc:
        raise PMReviewAgentRuntimeError(
            "PMReview task description contains malformed PMReviewInput JSON."
        ) from exc


def render_pm_review_contract_repair_directive(
    *,
    repair_fields: tuple[str, ...],
) -> str:
    """Render the machine-readable boundary for one targeted PMReview repair."""

    if not repair_fields or any(
        not isinstance(field_name, str) or not field_name.strip()
        for field_name in repair_fields
    ):
        raise PMReviewAgentRuntimeError(
            "PMReview contract repair fields must be non-empty strings."
        )
    normalized = tuple(field_name.strip() for field_name in repair_fields)
    if len(set(normalized)) != len(normalized):
        raise PMReviewAgentRuntimeError(
            "PMReview contract repair fields must not contain duplicates."
        )
    payload = json.dumps(
        {"repair_fields": list(normalized)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{_PM_REVIEW_REPAIR_MARKER}{payload}"


def parse_pm_review_contract_repair_fields(
    task_description: str,
) -> tuple[str, ...]:
    """Recover the targeted repair boundary, or return empty for a full decision."""

    marker_index = task_description.rfind(_PM_REVIEW_REPAIR_MARKER)
    if marker_index < 0:
        return ()
    json_text = task_description[marker_index + len(_PM_REVIEW_REPAIR_MARKER) :].lstrip()
    try:
        payload, _ = json.JSONDecoder().raw_decode(json_text)
    except json.JSONDecodeError as exc:
        raise PMReviewAgentRuntimeError(
            "PMReview task contains an invalid contract repair directive."
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"repair_fields"}:
        raise PMReviewAgentRuntimeError(
            "PMReview contract repair directive must contain exactly repair_fields."
        )
    raw_fields = payload.get("repair_fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise PMReviewAgentRuntimeError(
            "PMReview contract repair directive requires a non-empty repair_fields list."
        )
    if any(
        not isinstance(field_name, str) or not field_name.strip()
        for field_name in raw_fields
    ):
        raise PMReviewAgentRuntimeError(
            "PMReview contract repair fields must be non-empty strings."
        )
    fields = tuple(field_name.strip() for field_name in raw_fields)
    if len(set(fields)) != len(fields):
        raise PMReviewAgentRuntimeError(
            "PMReview contract repair fields must not contain duplicates."
        )
    return fields


def validate_pm_review_prompt_size(
    *,
    pm_review_input: PMReviewInput,
    task_description: str,
    max_prompt_chars: int | None,
) -> None:
    """Reject an oversized PMReview prompt before any provider invocation."""

    if max_prompt_chars is None:
        return
    if (
        not isinstance(max_prompt_chars, int)
        or isinstance(max_prompt_chars, bool)
        or max_prompt_chars <= 0
    ):
        raise PMReviewAgentRuntimeError("max_prompt_chars must be a positive integer or None.")
    prompt_chars = len(task_description)
    if prompt_chars <= max_prompt_chars:
        return
    field_sizes = _pm_review_input_field_char_sizes(pm_review_input)
    largest_fields = ", ".join(
        f"{name}={size}"
        for name, size in sorted(
            field_sizes.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:_PM_REVIEW_PROMPT_FIELD_REPORT_LIMIT]
    )
    raise PMReviewAgentRuntimeError(
        "PMReview prompt exceeds local prompt budget before agent execution: "
        f"prompt_chars={prompt_chars} "
        f"max_prompt_chars={max_prompt_chars} "
        f"visible_pm_history_count={len(pm_review_input.visible_pm_history)} "
        f"visible_evidence_count={len(pm_review_input.visible_evidence)} "
        f"visible_market_bar_count={len(pm_review_input.visible_market_bars)} "
        f"largest_fields={largest_fields}."
    )


def _pm_review_input_field_char_sizes(pm_review_input: PMReviewInput) -> dict[str, int]:
    payload = pm_review_input.to_json_payload()
    return {
        key: len(json.dumps(value, ensure_ascii=False, sort_keys=True))
        for key, value in payload.items()
    }


def _pm_review_decision_focus_prompt(pm_review_input: PMReviewInput) -> str:
    lines = [
        "Decision focus:",
        "- Weigh review_reasons, active exposure, same-side level roles, analysis "
        "implications, and visible market structure together.",
        "- No single signal automatically wins. In particular, de_risk_or_take_profit "
        "is a review signal, not a mandatory action by itself.",
        "- Same-side add and hold_boundary levels justify re-evaluation, not automatic "
        "strengthening.",
        (
            "- Current exposure under review: "
            f"{pm_review_input.actual_current_state} "
            f"({pm_review_input.actual_target_weight_before:+.1f})."
        ),
        "- Review reasons: " + ", ".join(pm_review_input.review_reasons) + ".",
    ]
    focus_levels = _active_side_focus_level_summaries(pm_review_input)
    if focus_levels:
        lines.append("- Same-side actionable levels to weigh:")
        lines.extend(f"  - {summary}" for summary in focus_levels)
    elif (
        pm_review_input.current_exposure_required and pm_review_input.analysis_snapshot is not None
    ):
        lines.append(
            "- Same-side actionable levels to weigh: none identified in the analysis snapshot."
        )
    if pm_review_input.portfolio_risk_snapshot is not None:
        lines.append(
            "- Portfolio risk snapshot is present: weigh current-segment strategy return, "
            "drawdown from the best weighted strategy mark, and active instrument basis "
            "alongside the thesis."
        )
    same_direction_strengthening_focus = _same_direction_strengthening_focus_lines(pm_review_input)
    if same_direction_strengthening_focus:
        lines.extend(same_direction_strengthening_focus)
    return "\n".join(lines)


def _same_direction_strengthening_focus_lines(
    pm_review_input: PMReviewInput,
) -> tuple[str, ...]:
    state = pm_review_input.actual_current_state
    if state not in {"weak_long", "weak_short"}:
        return ()
    snapshot = pm_review_input.analysis_snapshot
    if snapshot is None:
        return ()
    role_name = "role_if_already_long" if state == "weak_long" else "role_if_already_short"
    if not any(getattr(level, role_name) == "add" for level in snapshot.price_level_roles):
        return ()
    stronger_state = "strong_long" if state == "weak_long" else "strong_short"
    return (
        "- Same-side add level is present while current exposure is "
        f"{state}: treat that as a review signal, not an automatic reason to "
        f"upgrade to {stronger_state}.",
        "- Upgrade from "
        f"{state} to {stronger_state} only when visible market structure shows "
        "improved edge, not merely continued thesis, positive PnL, or same-side "
        "continuation.",
        "- Keeping "
        f"{state} is valid; if you do not upgrade, explain why the edge did not "
        "improve enough using concrete evidence such as unconfirmed breakout or "
        "breakdown acceptance, volume/price divergence, weaker follow-through, "
        "or compressed reward relative to current path risk.",
    )


def _active_side_focus_level_summaries(
    pm_review_input: PMReviewInput,
) -> tuple[str, ...]:
    snapshot = pm_review_input.analysis_snapshot
    if snapshot is None:
        return ()
    if pm_review_input.actual_current_state in {"weak_long", "strong_long"}:
        role_name = "role_if_already_long"
    elif pm_review_input.actual_current_state in {"weak_short", "strong_short"}:
        role_name = "role_if_already_short"
    else:
        return ()
    summaries: list[str] = []
    required_level_ids = set(pm_review_input.required_price_level_ids)
    for level in snapshot.price_level_roles:
        role = getattr(level, role_name)
        if role not in _PM_REVIEW_FOCUS_LEVEL_ROLES:
            continue
        tag = "required" if level.level_id in required_level_ids else "context"
        summary = f"{level.level_id}: {role} at {_format_focus_level(level)} [{tag}]"
        if level.path_context_required:
            summary += " path_context_required"
        summaries.append(summary)
    return tuple(summaries)


def _format_focus_level(level: PriceLevelRole) -> str:
    if level.value is not None:
        return f"{level.value:.2f}"
    lower = "?" if level.lower is None else f"{level.lower:.2f}"
    upper = "?" if level.upper is None else f"{level.upper:.2f}"
    return f"[{lower}, {upper}]"


def _pm_review_role_prompt() -> str:
    return "Role:\nYou are the discretionary portfolio manager for event-trader."


def _pm_review_thin_operating_boundary_prompt() -> str:
    return (
        "Operating boundary:\n"
        "Use PMReview to make the first exposure-aware judgment. Your output surface "
        "is only a thin PMDecisionDraft, and the system materializes downstream "
        "execution truth later."
    )


__all__ = [
    "PMReviewOutputProtocol",
    "PM_REVIEW_OUTPUT_SCHEMA_PROMPT",
    "parse_pm_review_contract_repair_fields",
    "parse_pm_review_input_from_task_prompt",
    "render_pm_review_business_task_prompt",
    "render_pm_review_contract_repair_directive",
    "render_pm_review_task_prompt",
    "validate_pm_review_prompt_size",
]
