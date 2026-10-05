"""Code-owned schema text for analysis final payload contracts."""

from __future__ import annotations

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


def analysis_price_semantics_required_fields() -> tuple[str, ...]:
    return _ANALYSIS_PRICE_SEMANTICS_FIELDS


def price_level_role_required_fields() -> tuple[str, ...]:
    return _PRICE_LEVEL_ROLE_FIELDS


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
        "Analysis final payload contract:\n"
        "- Return exactly one JSON object wrapped in \\boxed{...}; no text before "
        "or after the boxed JSON.\n"
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
        f"{analysis_assessment_contract_markdown(execution_direction_mode=execution_direction_mode)}"
    )


def analysis_assessment_contract_markdown(
    *,
    execution_direction_mode: ExecutionDirectionMode = "long_short",
) -> str:
    allowed_states = allowed_view_states_for_execution_direction_mode(
        execution_direction_mode
    )
    short_role_clause = _role_if_already_short_contract_clause(
        execution_direction_mode
    )
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
    return (
        "AnalysisAssessment contract:\n"
        "- Required fields: "
        f"{_csv(analysis_assessment_required_fields(execution_direction_mode))}.\n"
        "- Do not emit analysis_price_semantics. It is runtime-owned deterministic "
        "truth derived from price_level_roles plus market context after validation.\n"
        "- assessment_id: non-blank transport value. The runtime replaces it with "
        "a deterministic identity derived from the active analysis unit, or from "
        "the request identity when no unit formation lane exists.\n"
        "- target_key: active target key.\n"
        "- business_at: timezone-aware ISO timestamp.\n"
        "- source_event_ids: non-empty array covering every active request event_id.\n"
        "- memory_write_receipt_ids: array of memory write receipt IDs, or [].\n"
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
        "- price_level_roles: array of full PriceLevelRole objects, or [].\n"
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
        f"{price_level_role_contract_markdown(execution_direction_mode=execution_direction_mode)}\n"
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
    return (
        "PriceLevelRole contract:\n"
        "- Required fields, exactly: "
        f"{_csv(price_level_role_required_fields())}.\n"
        "- Use value for one numeric level, or lower and upper together for a zone.\n"
        "- source_type: one of "
        f"{_csv(allowed_price_level_source_types())}.\n"
        "- instrument_basis: use a canonical machine label. For target "
        "market_bar_derived levels, use the market symbol only; do not append "
        "bar, timeframe, or close prose.\n"
        "- source_event_ids: required for source_quoted or market_bar_derived "
        "levels; may be [] for agent_hypothesis or round_number.\n"
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
        "with the full PriceLevelRole contract."
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
    "analysis_assessment_optional_fields",
    "analysis_assessment_required_fields",
    "analysis_final_payload_contract_markdown",
    "analysis_final_payload_required_fields",
    "analysis_price_semantics_contract_markdown",
    "analysis_price_semantics_required_fields",
    "forbidden_analysis_final_payload_fields",
    "invalid_legacy_price_level_role_shape",
    "price_level_role_contract_markdown",
    "price_level_role_required_fields",
]
