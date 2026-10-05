"""PMReview prompt assets for the thin PM decision-draft contract."""

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
- cited_market_bar_ids: visible market bar ids you relied on
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
PM choice. Use hold_reason_code only when it adds compact value; otherwise return null.
Never return a requested_state outside PMReviewInput.allowed_requested_states.

If you provide fallback_state_if_clamped, you must also provide fallback_rationale_md.
If you provide fallback_rationale_md, you must also provide fallback_state_if_clamped.
"""

__all__ = ["PM_REVIEW_OUTPUT_SCHEMA_PROMPT"]
