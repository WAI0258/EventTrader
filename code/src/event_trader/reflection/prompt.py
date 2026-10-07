"""Project-owned prompts and bounded context serialization for Reflection."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from event_trader.contracts import EvidenceLedgerRecord, PageReadResult, ViewStateChange
from event_trader.counterfactuals import (
    CounterfactualBaselineResult,
    CounterfactualEvaluationReport,
)
from event_trader.decision_memory.projection import DecisionEpisodeProjection
from event_trader.episode_memory.contracts import (
    EPISODE_MEMORY_LEARNING_CARD_CONSUMER_ROLES,
)
from event_trader.integrations.bounded_context import bounded_text_payload
from event_trader.integrations.markdown_context import (
    MarkdownContextError,
    build_markdown_page_map,
)
from event_trader.reflection.agent_contract import (
    REFLECTION_TOOL_SERVER_NAME,
    REQUIRED_REFLECTION_TOOL_NAMES,
)
from event_trader.reflection.anchors import ReflectionLogEntry
from event_trader.reflection.context import ReflectionLedgerContext
from event_trader.reflection.contracts import (
    MARKET_CONTEXT_USAGE_QUALITY_LABELS,
    ReflectionTradeVerdict,
)
from event_trader.reflection.market_context_usage import MarketContextUsageFacts
from event_trader.reflection.output import open_position_review_page_path_for_context
from event_trader.reflection.price_facts import (
    open_position_watchlist_price_facts,
    open_position_watchlist_trigger_facts,
)
from event_trader.reflection.view_contracts import (
    CloseEpisodeReflectionContext,
    EpisodeReflectionContext,
    OpenPositionReflectionContext,
)
from event_trader.validation.portfolio_feedback import PortfolioFeedbackSnapshot
from event_trader.validation.returns import HorizonBaseline, ValidationReturnsResult


class ReflectionPromptContractError(RuntimeError):
    """Raised when deterministic Reflection context cannot form a valid prompt."""


_REFLECTION_PAGE_EXCERPT_CHAR_LIMIT = 4_000
_REFLECTION_EVIDENCE_EXCERPT_CHAR_LIMIT = 1_200
_REFLECTION_STATE_RATIONALE_EXCERPT_CHAR_LIMIT = 1_200
_REFLECTION_PAGE_MAP_SECTION_EXCERPT_CHAR_LIMIT = 280
_REFLECTION_PAGE_MAP_SECTION_LIMIT = 10
_REFLECTION_BRIEF_EVIDENCE_SAMPLE_LIMIT = 3
_REFLECTION_BRIEF_STATE_CHANGE_SAMPLE_LIMIT = 5
_REFLECTION_DECISION_EPISODE_SINCE_FULL_THRESHOLD = 32
_REFLECTION_DECISION_EPISODE_SINCE_HARD_CAP = 48
_REFLECTION_DECISION_EPISODE_RECENT_TAIL_COUNT = 12
_REFLECTION_DECISION_EPISODE_ABSOLUTE_ITEM_FUSE = 80


def _format_quoted_enum(values: tuple[str, ...]) -> str:
    if len(values) == 1:
        return f'"{values[0]}"'
    head = ", ".join(f'"{value}"' for value in values[:-1])
    return f'{head}, or "{values[-1]}"'


_REFLECTION_TRADE_VERDICTS: tuple[ReflectionTradeVerdict, ...] = (
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
_REFLECTION_MEMORY_STATUSES = (
    "provisional",
    "validated",
    "invalidated",
    "finalized",
)
_REFLECTION_VALIDATION_BASES = (
    "current_evidence",
    "later_evidence",
    "market_return",
    "execution_feedback",
    "portfolio_feedback",
    "close_review",
)
_REFLECTION_SOURCE_REF_KEYS = (
    "evidence_event_ids",
    "market_bar_refs",
    "review_paths",
    "pm_review_request_ids",
    "pm_decision_ids",
    "execution_record_ids",
    "portfolio_record_ids",
)
_THESIS_ASSESSMENT_VERDICT_INSTRUCTION = (
    '"verdict" must be "validated", "mixed", "invalidated", or "unclear"'
)
_ALL_TRADE_ASSESSMENT_VERDICT_INSTRUCTION = (
    "trade_assessment.verdict must be one of "
    + _format_quoted_enum(_REFLECTION_TRADE_VERDICTS)
    + "."
)
_CLOSE_TRADE_ASSESSMENT_VERDICT_INSTRUCTION = (
    "trade_assessment.verdict must be one of "
    + _format_quoted_enum(_CLOSED_REFLECTION_TRADE_VERDICTS)
    + '. Close review must never use "open".'
)
_MARKET_CONTEXT_USAGE_QUALITY_LABEL_INSTRUCTION = (
    '"usage_quality_label" must be one of '
    + ", ".join(f'"{label}"' for label in MARKET_CONTEXT_USAGE_QUALITY_LABELS)
)
_MARKET_CONTEXT_ERROR_LABELS_INSTRUCTION = (
    '"market_context_error_labels" must be [] or an array of distinct strings '
    "chosen only from "
    + _format_quoted_enum(MARKET_CONTEXT_USAGE_QUALITY_LABELS)
    + ". Do not use learning-decision error_attributions such as "
    '"interpretation_error".'
)
_LEARNING_MEMORY_STATUS_INSTRUCTION = (
    '"memory_status" must be one of ' + _format_quoted_enum(_REFLECTION_MEMORY_STATUSES) + "."
)
_LEARNING_VALIDATION_BASIS_INSTRUCTION = (
    '"validation_basis" must be one of ' + _format_quoted_enum(_REFLECTION_VALIDATION_BASES) + "."
)
_LEARNING_TIMESTAMP_INSTRUCTION = (
    '"source_visible_through" and "usable_from" must be ISO datetimes '
    '(for example "2026-03-12T02:30:00+00:00").'
)
_LEARNING_SOURCE_REFS_INSTRUCTION = (
    '"source_refs" must be a JSON object, not an array. Allowed keys are '
    + _format_quoted_enum(_REFLECTION_SOURCE_REF_KEYS)
    + "; each present key must map to an array of non-blank strings. "
    "Omit unused keys or set them to []."
)
_LEARNING_CONFIDENCE_INSTRUCTION = (
    '"confidence" must be a number between 0 and 1, not a label like "medium".'
)
_MARKET_CONTEXT_USAGE_QUALITY_PROMPT = (
    "Market-context usage evaluation:\n"
    "- If market_context_usage is present, evaluate market-context usage only from "
    "market_context_usage.decision_usages, "
    "market_context_usage.post_decision_outcome_summary, portfolio_feedback, and "
    "existing episode/terminal outcome facts.\n"
    "- Do not infer unavailable market data. Do not use or ask for raw "
    "decision-time market audit payloads.\n"
    "- Do not claim market context caused returns unless supported by validation "
    "facts.\n"
    "- Distinguish ignored context from context that existed but was not relevant.\n"
    "- Ask: Was market context visible at decision time? Which components were "
    "available, partial, unavailable, or missing? Did the decision text actually "
    "reference available context? Did ignored context plausibly matter after "
    "outcomes were known? Did the decision overtrust partial/unavailable context? "
    "Was context reasonable but exposure/positioning wrong? Was context itself "
    "misleading in hindsight?\n"
    "- When available context was not referenced and you conclude that omission "
    "materially mattered, usage_quality_label must be "
    "ignored_available_market_context. If positioning was also wrong, include "
    "correct_context_wrong_positioning in market_context_error_labels rather than "
    "using it to erase the ignored-context finding.\n"
    '- Use "inconclusive" when evidence is insufficient.\n'
    '- Use "not_applicable" when market_context_usage is absent.\n\n'
)
_LEARNING_DECISION_PROMPT = (
    "Learning-decision rules:\n"
    "- Always emit learning_decision. There is no fallback writeback heuristic.\n"
    "- learning_decision and every nested payload must be native JSON objects. Never "
    "serialize an object into a JSON string.\n"
    "- Reflection learning writes only typed EpisodeMemory candidates. Shared lessons, "
    "prompt assets, research claims, learning cards, and risk-policy candidates are "
    "controlled by deterministic promotion paths, not this output contract.\n"
    "- Reflection emits only EpisodeMemory delta/no-update candidates. Promotion intent "
    "is not a direct artifact write.\n"
    "- Close/finalized review must end with a promotion intent batch after the finalized "
    "close_review delta is written.\n"
    "- If a close/finalized review warrants downstream promotion, include one positive "
    "promotion object inside the delta payload. If not, omit promotion and the lifecycle "
    "will deterministically write a no_promote intent.\n"
    "- Do not use removed shared-lesson or prompt-asset write surfaces.\n"
    "- If review_decision is skip_review, learning_decision.primary_outcome must be "
    "no_learning_write and actions must contain no write action.\n"
    "- If review_decision is write_review, learning_decision.primary_outcome must be "
    "emit_episode_memory_candidate.\n"
    "- If no durable delta is warranted for a written review, emit "
    "candidate_kind='no_update'.\n"
    "- An EpisodeMemory delta does not require a prior memory write. Missing prior "
    "memory or previous_open_memory_update_at=null is not by itself a reason to choose "
    "skip_review or no_update; judge the current visible episode and outcome facts.\n"
    "- actions must be non-empty. no_learning_write still requires one non-writing "
    "action object that records the rationale.\n"
    "- When the brief contains expected_source_review_path, use "
    "expected_source_review_path exactly as each action's source_review_path; do "
    "not construct review paths yourself.\n"
    "- effective_from must be an ISO timestamp at or after the reviewed outcome time.\n"
    "- error_attributions must use only: attention_error, interpretation_error, "
    "timing_error, sizing_error, risk_management_error, execution_error, exit_error, "
    "market_regime_error, unavoidable_noise.\n\n"
    "learning_decision shape:\n"
    '{"primary_outcome": string, "actions": ['
    '{"action_id": string, "outcome": string, "rationale": string, '
    '"source_episode_id": string, "source_review_path": string, '
    '"effective_from": string, "error_attributions": [string], "payload": object}'
    "]}\n"
    "Allowed outcomes: no_learning_write, emit_episode_memory_candidate.\n"
    "EpisodeMemory payload requirements:\n"
    "- candidate_kind='delta' requires delta_kind, memory_status, validation_basis, "
    "source_visible_through, usable_from, source_refs, summary_md, thesis_delta, "
    "pm_management_delta, risk_delta, invalidation_delta, error_attributions, "
    "confidence, and supersedes_delta_ids.\n"
    "- candidate_kind='no_update' requires reason or reason_md, reason_code, "
    "source_visible_through, usable_from, source_refs, and reviewed_delta_ids.\n"
    f"- {_LEARNING_MEMORY_STATUS_INSTRUCTION}\n"
    f"- {_LEARNING_VALIDATION_BASIS_INSTRUCTION}\n"
    f"- {_LEARNING_TIMESTAMP_INSTRUCTION}\n"
    f"- {_LEARNING_SOURCE_REFS_INSTRUCTION}\n"
    f"- {_LEARNING_CONFIDENCE_INSTRUCTION}\n"
    "- Promotion is allowed only for a delta candidate with "
    'memory_status="finalized" and validation_basis="close_review".\n\n'
)


def _build_learning_decision_prompt(*, target_key: str) -> str:
    return (
        f"{_LEARNING_DECISION_PROMPT}"
        "Promotion consumer-role rule:\n"
        "- promotion.consumer_role identifies the downstream business consumer of "
        "the proposed learning card, not the review or validation event that produced "
        "it. Use exactly one of: "
        f"{', '.join(EPISODE_MEMORY_LEARNING_CARD_CONSUMER_ROLES)}.\n"
        "- Use analysis for future analysis/research synthesis, pm_review for future "
        "position/portfolio decision review, and reflection_learning for future "
        "Reflection evaluation.\n"
        "- Do not invent values such as 'reflection_close_review'. A close_review "
        "belongs in the EpisodeMemory validation_basis field; it is not a consumer role.\n\n"
        "Promotion scope rule:\n"
        f'- promotion.scope_key must be exactly "shared" or "target:{target_key}". '
        "Use shared only for a genuinely cross-target lesson; use the target scope "
        "for a lesson belonging to this target. Do not use episode IDs, thesis IDs, "
        "or any other scope format.\n\n"
    )

_OPEN_POSITION_LEARNING_PROMPT = (
    "Open-position-specific rules:\n"
    "- trade_assessment.return_pct is expressed in percentage points and must equal "
    "terminal_mark.strategy_return * 100.\n"
    "- An open position can produce a durable provisional or otherwise supported delta "
    "when visible outcome facts establish a material reusable lesson. The position "
    "remaining open or the final outcome remaining unresolved is not by itself a reason "
    "for no_update. If no_update is selected, identify the missing durable-learning "
    "condition beyond the position merely being open.\n"
    "- An open-position review must never use validation_basis='close_review'; no close "
    "review has occurred.\n\n"
)

_EPISODE_VERDICT_SEMANTICS_PROMPT = (
    "Episode verdict semantics:\n"
    "- thesis_assessment.verdict is the thesis status at the review cutoff, not an "
    "average of earlier and later phases. If a thesis held initially but controlling "
    "outcome facts later invalidated it, use invalidated and explain the earlier "
    "validation in summary_md. Use mixed only when material support and contradiction "
    "remain unresolved at the cutoff.\n"
    "- Assess the thesis that controlled the actual position and decision. A different "
    "macro narrative that later proved correct belongs in summary_md as an explanation "
    "of why the position thesis failed; it does not make an invalidated position thesis "
    "mixed.\n"
    "- watchlist_assessment judges whether the watchlist actually helped subsequent "
    "attention. A level that looks relevant only in hindsight but was not used does not "
    "become useful. Saying that a trigger would have fired is also insufficient without "
    "evidence that it actually guided attention, a decision, or position management. "
    "Use stale when items existed but did not reflect the active context or did not "
    "guide attention; use mixed only when they materially helped in part.\n\n"
)


def _reflection_role_prompt(*, mission: str) -> str:
    return f"Role:\nYou are the post-trade reviewer for event-trader.\n{mission}\n\n"


def _reflection_operating_boundary_prompt() -> str:
    return (
        "Operating boundary:\n"
        "- Reflection is a review and learning surface, not an execution surface.\n"
        "- Use only deterministic reflection context plus reflection read tools.\n"
        "- If the bounded context is insufficient for a defensible review, choose "
        "skip_review rather than inventing certainty.\n\n"
    )


def build_shared_reflection_prompt(
    *,
    context: ReflectionLedgerContext,
) -> str:
    serialized_context = json.dumps(
        _build_shared_reflection_brief(context),
        ensure_ascii=False,
        indent=2,
    )
    return (
        _reflection_role_prompt(
            mission=(
                "Review this shared reflection anchor like a disciplined human "
                "trader. Decide whether the anchor plus later evidence and "
                "outcome context produce reusable review value worth writing "
                "forward."
            )
        )
        + _reflection_operating_boundary_prompt()
        + "Hard rules:\n"
        "- Use only the provided context and reflection read tools.\n"
        "- Before any final boxed JSON, call at least one reflection read tool to "
        "inspect evidence, state changes, or target memory.\n"
        "- The compact brief is orientation only and is not sufficient for a final "
        "decision.\n"
        "- Bounded text snippets and page maps are read hints, not full context.\n"
        "- Do not invent evidence, trades, or market outcomes.\n"
        "- Review from a human trader perspective: what happened, whether the "
        "path was favorable or adverse, whether it was tradable, and whether the "
        "prior thesis held up.\n"
        "- If the bounded context is insufficient or there is not enough concrete "
        "review value, choose skip_review.\n\n"
        "Return exactly one JSON object wrapped in \\boxed{...}. Do not include "
        "text before or after the boxed JSON.\n\n"
        "The JSON object must contain exactly these fields:\n"
        '- "assessed_horizons": array of objects with '
        '"horizon_hours", "return_pct", "path_verdict", '
        '"tradability_verdict", "summary_md"\n'
        '- "thesis_assessment": object with "verdict", "summary_md"; '
        f"{_THESIS_ASSESSMENT_VERDICT_INSTRUCTION}\n"
        '- "trade_assessment": object or null with "verdict", "summary_md", '
        '"return_pct"\n'
        '- "review_decision": "write_review" | "skip_review"\n'
        '- "decision_rationale": string\n\n'
        f"Reflection context:\n{serialized_context}\n"
    )


def build_target_reflection_prompt(
    *,
    context: EpisodeReflectionContext,
) -> str:
    serialized_context = json.dumps(
        _build_target_reflection_brief(context),
        ensure_ascii=False,
        indent=2,
    )
    return (
        _reflection_role_prompt(
            mission=(
                "Review this closed target episode like a disciplined human "
                "trader. Judge whether the thesis held up, whether the path was "
                "tradable, and whether the result is worth writing as a durable "
                "target review."
            )
        )
        + _reflection_operating_boundary_prompt()
        + "Hard rules:\n"
        "- Use only the provided context and reflection read tools.\n"
        "- Before any final boxed JSON, call at least one reflection read tool to "
        "inspect evidence, state changes, or target memory.\n"
        "- The compact brief is orientation only and is not sufficient for a final "
        "decision.\n"
        "- Bounded text snippets and page maps are read hints, not full context.\n"
        "- The decision_episodes brief is selected review navigation, not the "
        "full audit trail; use list_decision_episodes/read_decision_episode "
        "when omitted decision context matters.\n"
        "- Treat portfolio feedback as deterministic validation facts. Use it to "
        "judge exposure and timing, but do not treat it as an already-written "
        "lesson or trading rule.\n"
        "- Treat counterfactual_evaluation, when present, as a post-horizon "
        "evaluation artifact for error attribution only. It was not visible at "
        "decision time and must not rewrite the decision-time context.\n"
        "- When trade_assessment is present, trade_assessment.return_pct is expressed "
        "in percentage points and must equal "
        "market_returns.episode_returns[0].strategy_return * 100.\n"
        "- Each assessed_horizons item must match a final horizon_baseline with the "
        "same horizon_hours, and return_pct must equal its strategy_return * 100. "
        "Do not assess a pending_market_data horizon.\n"
        "- Do not invent evidence, market data, or trade actions.\n"
        "- Review from a human trader perspective, not a schema-filling perspective.\n"
        "- If the bounded context is insufficient or the episode does not produce "
        "concrete review value yet, choose skip_review.\n\n"
        f"{_EPISODE_VERDICT_SEMANTICS_PROMPT}"
        f"{_MARKET_CONTEXT_USAGE_QUALITY_PROMPT}"
        f"{_build_learning_decision_prompt(target_key=context.episode.target_key)}"
        "Return exactly one JSON object wrapped in \\boxed{...}. Do not include "
        "text before or after the boxed JSON.\n\n"
        "The JSON object must contain exactly these fields:\n"
        '- "episode_id": string\n'
        '- "target_key": string\n'
        '- "assessed_horizons": array of objects with '
        '"horizon_hours", "return_pct", "path_verdict", '
        '"tradability_verdict", "summary_md"\n'
        '- "thesis_assessment": object with "verdict", "summary_md"; '
        f"{_THESIS_ASSESSMENT_VERDICT_INSTRUCTION}\n"
        '- "watchlist_assessment": object with "verdict", "summary_md"; '
        '"verdict" must be "useful", "mixed", "stale", "missing", or "unclear"\n'
        '- "trade_assessment": object or null with "verdict", "summary_md", '
        '"return_pct"\n'
        f'- "usage_quality_label": string; '
        f"{_MARKET_CONTEXT_USAGE_QUALITY_LABEL_INSTRUCTION}\n"
        '- "usage_quality_summary": string; compact usage-quality rationale; '
        'may be "" only when label is "not_applicable"\n'
        '- "market_context_error_labels": array of strings from the same fixed '
        "label set; use [] when no market-context usage error is supported\n"
        '- "review_decision": "write_review" | "skip_review"\n'
        '- "decision_rationale": string\n'
        '- "learning_decision": object following the learning-decision rules above\n\n'
        f"Episode reflection context:\n{serialized_context}\n"
    )


def build_open_position_reflection_prompt(
    *,
    context: OpenPositionReflectionContext,
) -> str:
    serialized_context = json.dumps(
        _build_open_position_reflection_brief(context),
        ensure_ascii=False,
        indent=2,
    )
    return (
        _reflection_role_prompt(
            mission=(
                "Review this open-position mark like a disciplined human trader. "
                "The episode is still open; do not treat this as a closed trade. "
                "Judge whether the thesis, open trade, and watchlist still make "
                "sense as of the replay/live cut-off."
            )
        )
        + _reflection_operating_boundary_prompt()
        + "Hard rules:\n"
        "- Use only the provided context and reflection read tools.\n"
        "- Before any final boxed JSON, call at least one reflection read tool to "
        "inspect evidence, state changes, or target memory.\n"
        "- The compact brief is orientation only and is not sufficient for a final "
        "decision.\n"
        "- Bounded text snippets and page maps are read hints, not full context.\n"
        "- The decision_episodes brief is selected review navigation, not the "
        "full audit trail; use list_decision_episodes/read_decision_episode "
        "when omitted decision context matters.\n"
        "- Treat portfolio feedback as deterministic validation facts. Use it to "
        "judge exposure and timing, but do not treat it as an already-written "
        "lesson or trading rule.\n"
        "- Always separate profit from risk: profit alone is not a sell reason, "
        "but profitable open exposure lowers the tolerance for independent "
        "reversal, invalidation, margin, liquidity, positioning, or market-context "
        "divergence risk.\n"
        "- exposure_assessment is reflection-only audit text. It does not "
        "execute, recommend, or encode a trade action. Do not return trade "
        "actions or sizing labels.\n"
        "- This is an open-position review. If no durable delta is warranted for "
        "a written review, emit an EpisodeMemory no_update candidate.\n"
        "- Do not invent evidence, market data, exits, or trade actions.\n"
        "- Review from a human trader perspective, not a schema-filling perspective.\n"
        '- trade_assessment.verdict must be "open".\n'
        "- terminal_mark.mark_price is the close_price of the completed bar identified "
        "by terminal_mark.mark_bar_start_at. It is not an intrabar or provisional quote.\n"
        "- watchlist_assessment.price_relations must reproduce every project-computed "
        "watchlist_price_facts level_id and relation exactly, in the same order. The "
        "watchlist verdict and summary must agree with those relations.\n"
        "- watchlist_assessment.price_triggers must reproduce every project-computed "
        "watchlist_price_trigger_facts item exactly, in the same order. A trigger with "
        "status='met' has been satisfied by the completed terminal close; never describe "
        "it as pending, merely intrabar, or not closed. status='not_evaluable' means the "
        "available terminal close alone cannot evaluate that trigger.\n"
        "- If the bounded context is insufficient or the terminal mark does not "
        "produce concrete review value yet, choose skip_review.\n\n"
        f"{_EPISODE_VERDICT_SEMANTICS_PROMPT}"
        f"{_MARKET_CONTEXT_USAGE_QUALITY_PROMPT}"
        f"{_OPEN_POSITION_LEARNING_PROMPT}"
        f"{_build_learning_decision_prompt(target_key=context.episode.target_key)}"
        "Return exactly one JSON object wrapped in \\boxed{...}. Do not include "
        "text before or after the boxed JSON.\n\n"
        "The JSON object must contain exactly these fields:\n"
        '- "episode_id": string\n'
        '- "target_key": string\n'
        '- "thesis_assessment": object with "verdict", "summary_md"; '
        f"{_THESIS_ASSESSMENT_VERDICT_INSTRUCTION}\n"
        '- "watchlist_assessment": object with "verdict", "summary_md", '
        '"price_relations", "price_triggers"; "price_relations" is an ordered array '
        'of objects with '
        'exactly "level_id" and "relation" ("below" | "within" | "above"); '
        '"price_triggers" is an ordered array of objects with exactly "level_id", '
        '"trigger_kind" ("refresh" | "invalidation"), "trigger_id", and "status" '
        '("met" | "not_met" | "not_evaluable"); '
        '"verdict" must be "useful", "mixed", "stale", "missing", or "unclear"\n'
        '- "trade_assessment": object with "verdict", "summary_md", "return_pct"\n'
        '- Do not include a top-level "return_pct"; return_pct belongs only '
        'inside "trade_assessment".\n'
        '- "exposure_assessment": object with "summary_md"; describe open '
        "exposure quality and risk, without trade-action fields\n"
        f'- "usage_quality_label": string; '
        f"{_MARKET_CONTEXT_USAGE_QUALITY_LABEL_INSTRUCTION}\n"
        '- "usage_quality_summary": string; compact usage-quality rationale; '
        'may be "" only when label is "not_applicable"\n'
        '- "market_context_error_labels": array of strings from the same fixed '
        "label set; use [] when no market-context usage error is supported\n"
        '- "review_decision": "write_review" | "skip_review"\n'
        '- "decision_rationale": string\n'
        '- "learning_decision": object following the learning-decision rules above\n\n'
        f"Terminal open-position reflection context:\n{serialized_context}\n"
    )


def build_target_close_reflection_prompt(
    *,
    context: CloseEpisodeReflectionContext,
) -> str:
    serialized_context = json.dumps(
        _build_target_close_reflection_brief(context),
        ensure_ascii=False,
        indent=2,
    )
    return (
        _reflection_role_prompt(
            mission=(
                "Review this just-closed target episode like a disciplined human "
                "trader. This is a close/reversal review, not a horizon "
                "follow-up. Judge the trade that actually ended, whether the "
                "thesis held up through close, and whether the watchlist helped "
                "attention before the close."
            )
        )
        + _reflection_operating_boundary_prompt()
        + "Hard rules:\n"
        "- Use only the provided context and reflection read tools.\n"
        "- Before any final boxed JSON, call at least one reflection read tool to "
        "inspect evidence, state changes, or target memory.\n"
        "- The compact brief is orientation only and is not sufficient for a final "
        "decision.\n"
        "- Bounded text snippets and page maps are read hints, not full context.\n"
        "- The decision_episodes brief is selected review navigation, not the "
        "full audit trail; use list_decision_episodes/read_decision_episode "
        "when omitted decision context matters.\n"
        "- Treat portfolio feedback as deterministic validation facts. Use it to "
        "judge exposure and timing, but do not treat it as an already-written "
        "lesson or trading rule.\n"
        "- trade_assessment.return_pct is expressed in percentage points and must "
        "equal market_returns.episode_returns[0].strategy_return * 100.\n"
        "- Do not invent evidence, market data, exits, or trade actions.\n"
        "- Do not request or assess 24h/72h/168h horizons in this close review.\n"
        f"- {_CLOSE_TRADE_ASSESSMENT_VERDICT_INSTRUCTION}\n"
        "- If the bounded context is insufficient or the close does not produce "
        "concrete review value, choose skip_review.\n\n"
        f"{_EPISODE_VERDICT_SEMANTICS_PROMPT}"
        f"{_MARKET_CONTEXT_USAGE_QUALITY_PROMPT}"
        f"{_build_learning_decision_prompt(target_key=context.episode.target_key)}"
        "Return exactly one JSON object wrapped in \\boxed{...}. Do not include "
        "text before or after the boxed JSON.\n\n"
        "The JSON object must contain exactly these fields:\n"
        '- "episode_id": string\n'
        '- "target_key": string\n'
        '- "thesis_assessment": object with "verdict", "summary_md"; '
        f"{_THESIS_ASSESSMENT_VERDICT_INSTRUCTION}\n"
        '- "watchlist_assessment": object with "verdict", "summary_md"; '
        '"verdict" must be "useful", "mixed", "stale", "missing", or "unclear"\n'
        '- "trade_assessment": object with "verdict", "summary_md", "return_pct"; '
        f"{_CLOSE_TRADE_ASSESSMENT_VERDICT_INSTRUCTION}\n"
        f'- "usage_quality_label": string; '
        f"{_MARKET_CONTEXT_USAGE_QUALITY_LABEL_INSTRUCTION}\n"
        '- "usage_quality_summary": string; compact usage-quality rationale; '
        'may be "" only when label is "not_applicable"\n'
        f"- {_MARKET_CONTEXT_ERROR_LABELS_INSTRUCTION}\n"
        '- "review_decision": "write_review" | "skip_review"\n'
        '- "decision_rationale": string\n'
        '- "learning_decision": object following the learning-decision rules above\n\n'
        f"Close episode reflection context:\n{serialized_context}\n"
    )


def _build_shared_reflection_brief(
    context: ReflectionLedgerContext,
) -> dict[str, object]:
    return {
        "task_kind": "shared_anchor_reflection",
        "anchor": {
            "anchor_id": context.anchor_id,
            "scope_key": context.anchor.scope_key,
            "target_key": context.anchor.target_key,
            "log_page_path": context.anchor.log_page_path,
            "entry_key": context.anchor.entry_key,
            "logged_at": context.anchor.logged_at.isoformat(),
        },
        "coverage": {
            "lookback_hours": context.coverage.lookback_hours,
            "horizons_hours": list(context.coverage.horizons_hours),
        },
        "anchor_entry": {
            "heading_text": context.anchor_entry.heading_text,
            "line_start": context.anchor_entry.line_start,
            "line_end": context.anchor_entry.line_end,
            "entry": _serialize_brief_markdown(context.anchor_entry.entry_md),
            "body": _serialize_brief_markdown(context.anchor_entry.body_md),
            "cited_event_ids": list(context.anchor_entry.cited_event_ids),
            "cited_source_refs": list(context.anchor_entry.cited_source_refs),
        },
        "original_evidence": _serialize_evidence_set_brief(context.original_evidence),
        "later_evidence": _serialize_evidence_set_brief(context.later_evidence),
        "outcome_context": (
            None
            if context.outcome_context is None
            else {
                "observed_at": context.outcome_context.observed_at.isoformat(),
                "summary": _serialize_brief_markdown(context.outcome_context.summary_md),
            }
        ),
        "trade_context": (
            None
            if context.trade_context is None
            else {
                "observed_at": context.trade_context.observed_at.isoformat(),
                "summary": _serialize_brief_markdown(context.trade_context.summary_md),
            }
        ),
        "tool_reads": _serialize_reflection_tool_read_brief(),
    }


def _build_target_reflection_brief(
    context: EpisodeReflectionContext,
) -> dict[str, object]:
    return {
        "task_kind": "closed_target_episode_reflection",
        "episode": {
            "episode_id": context.episode.episode_id,
            "target_key": context.episode.target_key,
            "direction": context.episode.direction,
            "opened_at": context.episode.opened_at.isoformat(),
            "closed_at": (
                None if context.episode.closed_at is None else context.episode.closed_at.isoformat()
            ),
            "close_reason": context.episode.close_reason,
        },
        "coverage": {
            "lookback_hours": context.coverage.lookback_hours,
            "horizons_hours": list(context.coverage.horizons_hours),
        },
        "expected_source_review_path": _target_review_page_path(
            target_key=context.episode.target_key,
            opened_at=context.episode.opened_at,
            closed_at=_require_datetime(
                context.episode.closed_at,
                field_name="episode.closed_at",
            ),
        ),
        "state_changes": _serialize_state_change_set_brief(context.state_changes),
        "decision_episodes": _serialize_decision_episode_summaries(context.decision_episodes),
        "original_evidence": _serialize_evidence_set_brief(context.original_evidence),
        "later_evidence": _serialize_evidence_set_brief(context.later_evidence),
        "market_mapping": {
            "market_symbol": context.market_mapping.market_symbol,
            "market_session": context.market_mapping.market_session,
            "exchange_session_scope": context.market_mapping.exchange_session_scope,
            "exchange": context.market_mapping.exchange,
            "bar_granularity": context.market_mapping.bar_granularity,
        },
        "market_returns": _serialize_market_returns(context.market_returns),
        "portfolio_feedback": _serialize_portfolio_feedback_brief(context.portfolio_feedback),
        "market_context_usage": _serialize_market_context_usage(context.market_context_usage),
        "counterfactual_evaluation": _serialize_counterfactual_summary(
            context.counterfactual_report
        ),
        "target_log_context": (
            None
            if context.target_log_context is None
            else _serialize_page_brief(context.target_log_context)
        ),
        "watchlist_context": (
            None
            if context.watchlist_context is None
            else _serialize_page_brief(context.watchlist_context)
        ),
        "tool_reads": _serialize_reflection_tool_read_brief(),
    }


def _build_open_position_reflection_brief(
    context: OpenPositionReflectionContext,
) -> dict[str, object]:
    watchlist_price_facts = open_position_watchlist_price_facts(context)
    watchlist_trigger_facts = open_position_watchlist_trigger_facts(context)
    return {
        "task_kind": "open_position_reflection",
        "episode": {
            "episode_id": context.episode.episode_id,
            "target_key": context.episode.target_key,
            "direction": context.episode.direction,
            "opened_at": context.episode.opened_at.isoformat(),
            "closed_at": None,
        },
        "open_segment": {
            "segment_id": context.open_segment.segment_id,
            "target_weight": context.open_segment.target_weight,
            "opened_at": context.open_segment.opened_at.isoformat(),
            "opened_by_state_change_id": context.open_segment.opened_by_state_change_id,
        },
        "state_changes": _serialize_state_change_set_brief(context.state_changes),
        "decision_episodes": _serialize_decision_episode_summaries(
            context.decision_episodes,
            previous_memory_update_at=context.previous_open_memory_update_at,
        ),
        "original_evidence": _serialize_evidence_set_brief(context.original_evidence),
        "later_evidence": _serialize_evidence_set_brief(context.later_evidence),
        "market_mapping": {
            "market_symbol": context.market_mapping.market_symbol,
            "market_session": context.market_mapping.market_session,
            "exchange_session_scope": context.market_mapping.exchange_session_scope,
            "exchange": context.market_mapping.exchange,
            "bar_granularity": context.market_mapping.bar_granularity,
        },
        "terminal_mark": {
            "episode_id": context.terminal_mark.episode_id,
            "replay_end_at": context.terminal_mark.replay_end_at.isoformat(),
            "entry_bar_start_at": context.terminal_mark.entry_bar_start_at.isoformat(),
            "mark_bar_start_at": context.terminal_mark.mark_bar_start_at.isoformat(),
            "entry_price": context.terminal_mark.entry_price,
            "mark_price": context.terminal_mark.mark_price,
            "mark_price_basis": "completed_bar_close",
            "underlying_return": context.terminal_mark.underlying_return,
            "strategy_return": context.terminal_mark.strategy_return,
            "target_weight": context.terminal_mark.target_weight,
        },
        "watchlist_price_facts": [fact.to_json_payload() for fact in watchlist_price_facts],
        "watchlist_price_trigger_facts": [
            fact.to_json_payload() for fact in watchlist_trigger_facts
        ],
        "review_kind": context.review_kind,
        "previous_open_memory_update_at": (
            None
            if context.previous_open_memory_update_at is None
            else context.previous_open_memory_update_at.isoformat()
        ),
        "open_position_material_update_sequence": (context.open_position_material_update_sequence),
        "expected_source_review_path": open_position_review_page_path_for_context(context),
        "expected_source_review_path_note": (
            "Use the review artifact path produced by the open-position review writer "
            "for source_review_path. For open_position_material_update, assess both "
            "changes since the previous open memory update and the full "
            "position-to-date thesis. For write_review with no durable delta, emit "
            "emit_episode_memory_candidate with candidate_kind='no_update'."
        ),
        "portfolio_feedback": _serialize_portfolio_feedback_brief(context.portfolio_feedback),
        "market_context_usage": _serialize_market_context_usage(context.market_context_usage),
        "watchlist_context": _serialize_page_brief(context.watchlist_context),
        "target_log_context": (
            None
            if context.target_log_context is None
            else _serialize_page_brief(context.target_log_context)
        ),
        "tool_reads": _serialize_reflection_tool_read_brief(),
    }


def _build_target_close_reflection_brief(
    context: CloseEpisodeReflectionContext,
) -> dict[str, object]:
    return {
        "task_kind": "close_episode_reflection",
        "episode": {
            "episode_id": context.episode.episode_id,
            "target_key": context.episode.target_key,
            "direction": context.episode.direction,
            "opened_at": context.episode.opened_at.isoformat(),
            "closed_at": (
                None if context.episode.closed_at is None else context.episode.closed_at.isoformat()
            ),
            "close_reason": context.episode.close_reason,
        },
        "expected_source_review_path": _target_close_review_page_path(
            target_key=context.episode.target_key,
            opened_at=context.episode.opened_at,
            closed_at=_require_datetime(
                context.episode.closed_at,
                field_name="episode.closed_at",
            ),
        ),
        "state_changes": _serialize_state_change_set_brief(context.state_changes),
        "decision_episodes": _serialize_decision_episode_summaries(context.decision_episodes),
        "original_evidence": _serialize_evidence_set_brief(context.original_evidence),
        "later_evidence": _serialize_evidence_set_brief(context.later_evidence),
        "market_mapping": {
            "market_symbol": context.market_mapping.market_symbol,
            "market_session": context.market_mapping.market_session,
            "exchange_session_scope": context.market_mapping.exchange_session_scope,
            "exchange": context.market_mapping.exchange,
            "bar_granularity": context.market_mapping.bar_granularity,
        },
        "market_returns": _serialize_market_returns(context.market_returns),
        "portfolio_feedback": _serialize_portfolio_feedback_brief(context.portfolio_feedback),
        "market_context_usage": _serialize_market_context_usage(context.market_context_usage),
        "watchlist_context": _serialize_page_brief(context.watchlist_context),
        "target_log_context": (
            None
            if context.target_log_context is None
            else _serialize_page_brief(context.target_log_context)
        ),
        "tool_reads": _serialize_reflection_tool_read_brief(),
    }


def _serialize_evidence_set_brief(
    records: tuple[EvidenceLedgerRecord, ...],
) -> dict[str, object]:
    if not records:
        return {"count": 0, "event_time_range": None, "sample_latest": []}
    ordered = tuple(
        sorted(
            records,
            key=lambda record: (record.ts_event, record.ts_init, record.event_id),
        )
    )
    sample = ordered[-_REFLECTION_BRIEF_EVIDENCE_SAMPLE_LIMIT:]
    return {
        "count": len(ordered),
        "event_time_range": {
            "start": ordered[0].ts_event.isoformat(),
            "end": ordered[-1].ts_event.isoformat(),
        },
        "sample_latest": [_serialize_evidence_record_brief(record) for record in sample],
    }


def _serialize_state_change_set_brief(
    state_changes: tuple[ViewStateChange, ...],
) -> dict[str, object]:
    if not state_changes:
        return {"count": 0, "effective_time_range": None, "sample_latest": []}
    ordered = tuple(
        sorted(
            state_changes,
            key=lambda state_change: (
                state_change.effective_at,
                state_change.state_change_id,
            ),
        )
    )
    sample = ordered[-_REFLECTION_BRIEF_STATE_CHANGE_SAMPLE_LIMIT:]
    return {
        "count": len(ordered),
        "effective_time_range": {
            "start": ordered[0].effective_at.isoformat(),
            "end": ordered[-1].effective_at.isoformat(),
        },
        "sample_latest": [_serialize_state_change(state_change) for state_change in sample],
    }


def _target_review_page_path(
    *,
    target_key: str,
    opened_at: datetime,
    closed_at: datetime,
) -> str:
    return (
        f"targets/{target_key}/reviews/"
        f"{_review_timestamp(opened_at)}_{_review_timestamp(closed_at)}.md"
    )


def _target_close_review_page_path(
    *,
    target_key: str,
    opened_at: datetime,
    closed_at: datetime,
) -> str:
    return (
        f"targets/{target_key}/reviews/"
        f"{_review_timestamp(opened_at)}_{_review_timestamp(closed_at)}_close.md"
    )


def _review_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%y%m%dT%H%M%S")


def _format_hour_label(hour: float) -> str:
    return f"{_format_hour_value(hour)}h"


def _format_hour_value(hour: float) -> str:
    if float(hour).is_integer():
        return str(int(hour))
    return format(hour, "g")


def _serialize_decision_episode_summaries(
    episodes,
    *,
    previous_memory_update_at: datetime | None = None,
) -> dict[str, object]:
    if not episodes:
        return {
            "count": 0,
            "included_count": 0,
            "omitted_count": 0,
            "selection_policy": "adaptive_review_window",
            "selection_counts": {},
            "omitted_by_status": {},
            "limits": _decision_episode_selection_limits(),
            "window": _decision_episode_selection_window(previous_memory_update_at),
            "items": [],
        }

    ordered = tuple(
        sorted(
            episodes,
            key=lambda episode: (
                episode.opened_at,
                episode.target_key,
                episode.episode_id,
            ),
        )
    )
    selected_reasons = _select_decision_episode_reasons(
        ordered,
        previous_memory_update_at=previous_memory_update_at,
    )
    selected_ids = set(selected_reasons)
    omitted = tuple(episode for episode in ordered if episode.episode_id not in selected_ids)
    selection_counts: dict[str, int] = {}
    for reasons in selected_reasons.values():
        for reason in reasons:
            selection_counts[reason] = selection_counts.get(reason, 0) + 1

    return {
        "count": len(ordered),
        "included_count": len(selected_reasons),
        "omitted_count": len(omitted),
        "selection_policy": "adaptive_review_window",
        "selection_counts": dict(sorted(selection_counts.items())),
        "omitted_by_status": _decision_episode_status_counts(omitted),
        "limits": _decision_episode_selection_limits(),
        "window": _decision_episode_selection_window(previous_memory_update_at),
        "items": [
            _serialize_decision_episode_summary_item(
                episode,
                selection_reasons=selected_reasons[episode.episode_id],
            )
            for episode in ordered
            if episode.episode_id in selected_ids
        ],
    }


def _decision_episode_selection_limits() -> dict[str, object]:
    return {
        "since_previous_memory_update_full_threshold": (
            _REFLECTION_DECISION_EPISODE_SINCE_FULL_THRESHOLD
        ),
        "since_previous_memory_update_hard_cap": (_REFLECTION_DECISION_EPISODE_SINCE_HARD_CAP),
        "recent_tail_count": _REFLECTION_DECISION_EPISODE_RECENT_TAIL_COUNT,
        "absolute_item_fuse": _REFLECTION_DECISION_EPISODE_ABSOLUTE_ITEM_FUSE,
    }


def _decision_episode_selection_window(
    previous_memory_update_at: datetime | None,
) -> dict[str, object]:
    return {
        "previous_memory_update_at": (
            None if previous_memory_update_at is None else previous_memory_update_at.isoformat()
        ),
        "since_previous_memory_update_enabled": previous_memory_update_at is not None,
    }


def _select_decision_episode_reasons(
    episodes: tuple[DecisionEpisodeProjection, ...],
    *,
    previous_memory_update_at: datetime | None,
) -> dict[str, tuple[str, ...]]:
    reasons_by_id: dict[str, set[str]] = {}

    def add(episode: DecisionEpisodeProjection, reason: str) -> None:
        reasons_by_id.setdefault(episode.episode_id, set()).add(reason)

    if episodes:
        add(episodes[0], "opening")

    for episode in episodes:
        if _is_decision_episode_anchor(episode):
            add(episode, "anchor")

    if previous_memory_update_at is None:
        if len(episodes) <= _REFLECTION_DECISION_EPISODE_ABSOLUTE_ITEM_FUSE:
            selected_initial = episodes
        else:
            selected_initial = tuple(
                sorted(
                    episodes,
                    key=_decision_episode_selection_priority,
                    reverse=True,
                )[:_REFLECTION_DECISION_EPISODE_ABSOLUTE_ITEM_FUSE]
            )
        for episode in selected_initial:
            add(episode, "initial_window")
    else:
        since_previous = tuple(
            episode
            for episode in episodes
            if _decision_episode_touched_after(episode, previous_memory_update_at)
        )
        if len(since_previous) <= _REFLECTION_DECISION_EPISODE_SINCE_FULL_THRESHOLD:
            selected_since = since_previous
        else:
            selected_since = tuple(
                sorted(
                    since_previous,
                    key=_decision_episode_selection_priority,
                    reverse=True,
                )[:_REFLECTION_DECISION_EPISODE_SINCE_HARD_CAP]
            )
        for episode in selected_since:
            add(episode, "since_previous_review")

    recent_tail = tuple(
        episode for episode in reversed(episodes) if episode.episode_id not in reasons_by_id
    )[:_REFLECTION_DECISION_EPISODE_RECENT_TAIL_COUNT]
    for episode in recent_tail:
        add(episode, "recent_tail")

    if len(reasons_by_id) > _REFLECTION_DECISION_EPISODE_ABSOLUTE_ITEM_FUSE:
        anchor_ids = {
            episode.episode_id
            for episode in episodes
            if "opening" in reasons_by_id.get(episode.episode_id, set())
            or "anchor" in reasons_by_id.get(episode.episode_id, set())
        }
        remaining_capacity = max(
            0,
            _REFLECTION_DECISION_EPISODE_ABSOLUTE_ITEM_FUSE - len(anchor_ids),
        )
        non_anchor = tuple(
            episode
            for episode in episodes
            if episode.episode_id in reasons_by_id and episode.episode_id not in anchor_ids
        )
        kept_non_anchor = {
            episode.episode_id
            for episode in sorted(
                non_anchor,
                key=_decision_episode_selection_priority,
                reverse=True,
            )[:remaining_capacity]
        }
        kept_ids = anchor_ids | kept_non_anchor
        reasons_by_id = {
            episode_id: reasons
            for episode_id, reasons in reasons_by_id.items()
            if episode_id in kept_ids
        }

    return {episode_id: tuple(sorted(reasons)) for episode_id, reasons in reasons_by_id.items()}


def _is_decision_episode_anchor(episode: DecisionEpisodeProjection) -> bool:
    analysis = episode.analysis_record
    return (
        bool(episode.validation_marks)
        or episode.pm_decision_record is not None
        or bool(episode.reflection_records)
        or bool(episode.repair_records)
        or (analysis is not None and analysis.status == "failed")
    )


def _decision_episode_touched_after(
    episode: DecisionEpisodeProjection,
    previous_memory_update_at: datetime,
) -> bool:
    return any(
        value > previous_memory_update_at for value in _decision_episode_business_times(episode)
    )


def _decision_episode_business_times(episode: DecisionEpisodeProjection) -> tuple[datetime, ...]:
    values: list[datetime] = []
    for record in (
        episode.attention_record,
        episode.analysis_record,
        episode.pm_decision_record,
    ):
        if record is not None:
            values.append(record.business_at)
    for collection in (
        episode.validation_marks,
        episode.reflection_records,
        episode.repair_records,
    ):
        values.extend(record.business_at for record in collection)
    if not values:
        values.append(episode.opened_at)
    return tuple(values)


def _decision_episode_selection_priority(
    episode: DecisionEpisodeProjection,
) -> tuple[int, int, datetime]:
    analysis = episode.analysis_record
    has_analysis = 1 if analysis is not None else 0
    has_non_attention_status = 1 if episode.status != "open_attention" else 0
    return (has_non_attention_status, has_analysis, episode.opened_at)


def _decision_episode_status_counts(
    episodes: tuple[DecisionEpisodeProjection, ...],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for episode in episodes:
        status = str(episode.status)
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _serialize_decision_episode_summary_item(
    episode: DecisionEpisodeProjection,
    *,
    selection_reasons: tuple[str, ...],
) -> dict[str, object]:
    analysis = episode.analysis_record
    validation_state_change_ids = tuple(
        str(mark.payload.get("state_change_id"))
        for mark in episode.validation_marks
        if isinstance(mark.payload.get("state_change_id"), str)
    )
    context_packet_ids = tuple(
        record.context_packet_id
        for record in (episode.attention_record, analysis)
        if record is not None and record.context_packet_id is not None
    )
    return {
        "episode_id": episode.episode_id,
        "status": episode.status,
        "opened_at": episode.opened_at.isoformat(),
        "selection_reasons": list(selection_reasons),
        "event_ids": list(episode.event_ids),
        "context_packet_ids": list(context_packet_ids),
        "analysis_outcome": None if analysis is None else analysis.status,
        "validation_state_change_ids": list(validation_state_change_ids),
    }


def _serialize_evidence_record_brief(record: EvidenceLedgerRecord) -> dict[str, object]:
    return {
        "event_id": record.event_id,
        "target_key": record.target_key,
        "source_ref": record.source_ref,
        "title": record.title,
        "content_sha256": sha256(record.content.encode("utf-8")).hexdigest(),
        "content_char_count": len(record.content),
        "content_available_via": "read_evidence/list_evidence_window",
        "labels": list(record.labels),
        "ts_source": record.ts_source.isoformat(),
        "ts_event": record.ts_event.isoformat(),
        "ts_init": record.ts_init.isoformat(),
    }


def _serialize_page_brief(page: PageReadResult) -> dict[str, object]:
    return {
        "page_path": page.page_path,
        "content_sha256": sha256(page.content_md.encode("utf-8")).hexdigest(),
        "content_char_count": len(page.content_md),
        "content_available_via": "read_target_page/read_target_section/search_target_memory",
        "page_map": _serialize_page_map_brief(page.content_md),
    }


def _serialize_page_map_brief(content_md: str) -> list[dict[str, object]]:
    try:
        page_map = build_markdown_page_map(
            content_md,
            excerpt_char_limit=_REFLECTION_PAGE_MAP_SECTION_EXCERPT_CHAR_LIMIT,
        )
    except MarkdownContextError as exc:
        raise ReflectionPromptContractError(
            f"Failed to build reflection prompt page map: {exc}"
        ) from exc
    return [
        {
            "heading": section["heading"],
            "level": section["level"],
            "start_offset": section["start_offset"],
            "end_offset": section["end_offset"],
            "content_sha256": section["content_sha256"],
            "content_char_count": section["content_char_count"],
            "content_truncated": section["content_truncated"],
        }
        for section in page_map[:_REFLECTION_PAGE_MAP_SECTION_LIMIT]
    ]


def _serialize_brief_markdown(value: str) -> dict[str, object]:
    return _serialize_bounded_markdown(value)


def _serialize_reflection_tool_read_brief() -> dict[str, object]:
    return {
        "server_hint": REFLECTION_TOOL_SERVER_NAME,
        "tool_names": list(REQUIRED_REFLECTION_TOOL_NAMES),
        "required_before_strong_conclusion": [
            "Use read_evidence for original/later evidence records tied to the conclusion.",
            "Use list_evidence_window when later evidence count or time range matters.",
            "Use list_state_changes for state-change rationale and source-event links.",
            "Use list_decision_episodes/read_decision_episode when omitted decision "
            "audit trail context matters.",
            "Use read_target_section or read_target_page when page excerpts are truncated.",
        ],
    }


def target_reflection_window_end(context: EpisodeReflectionContext) -> datetime:
    closed_at = _require_datetime(
        context.episode.closed_at,
        field_name="episode.closed_at",
    )
    horizon_end = context.episode.opened_at + timedelta(hours=max(context.coverage.horizons_hours))
    return max(closed_at, horizon_end)


def _serialize_shared_context(context: ReflectionLedgerContext) -> dict[str, object]:
    return {
        "anchor": {
            "scope_key": context.anchor.scope_key,
            "target_key": context.anchor.target_key,
            "log_page_path": context.anchor.log_page_path,
            "entry_key": context.anchor.entry_key,
            "logged_at": context.anchor.logged_at.isoformat(),
        },
        "anchor_entry": _serialize_anchor_entry(context.anchor_entry),
        "coverage": {
            "lookback_hours": context.coverage.lookback_hours,
            "horizons_hours": list(context.coverage.horizons_hours),
        },
        "original_evidence": [
            _serialize_evidence_record(record) for record in context.original_evidence
        ],
        "later_evidence": [_serialize_evidence_record(record) for record in context.later_evidence],
        "outcome_context": (
            None
            if context.outcome_context is None
            else {
                "observed_at": context.outcome_context.observed_at.isoformat(),
                "summary": _serialize_bounded_markdown(context.outcome_context.summary_md),
            }
        ),
        "trade_context": (
            None
            if context.trade_context is None
            else {
                "observed_at": context.trade_context.observed_at.isoformat(),
                "summary": _serialize_bounded_markdown(context.trade_context.summary_md),
            }
        ),
    }


def _serialize_target_context(context: EpisodeReflectionContext) -> dict[str, object]:
    return {
        "episode": {
            "episode_id": context.episode.episode_id,
            "target_key": context.episode.target_key,
            "direction": context.episode.direction,
            "opened_at": context.episode.opened_at.isoformat(),
            "closed_at": (
                None if context.episode.closed_at is None else context.episode.closed_at.isoformat()
            ),
            "close_reason": context.episode.close_reason,
        },
        "coverage": {
            "lookback_hours": context.coverage.lookback_hours,
            "horizons_hours": list(context.coverage.horizons_hours),
        },
        "state_changes": [
            _serialize_state_change(state_change) for state_change in context.state_changes
        ],
        "original_evidence": [
            _serialize_evidence_record(record) for record in context.original_evidence
        ],
        "later_evidence": [_serialize_evidence_record(record) for record in context.later_evidence],
        "market_mapping": {
            "market_symbol": context.market_mapping.market_symbol,
            "market_session": context.market_mapping.market_session,
            "exchange_session_scope": context.market_mapping.exchange_session_scope,
            "exchange": context.market_mapping.exchange,
            "bar_granularity": context.market_mapping.bar_granularity,
        },
        "market_returns": _serialize_market_returns(context.market_returns),
        "portfolio_feedback": _serialize_portfolio_feedback(context.portfolio_feedback),
        "market_context_usage": _serialize_market_context_usage(context.market_context_usage),
        "counterfactual_evaluation": _serialize_counterfactual_summary(
            context.counterfactual_report
        ),
        "target_log_context": (
            None
            if context.target_log_context is None
            else _serialize_page(context.target_log_context)
        ),
        "watchlist_context": (
            None
            if context.watchlist_context is None
            else _serialize_page(context.watchlist_context)
        ),
    }


def _serialize_open_position_context(
    context: OpenPositionReflectionContext,
) -> dict[str, object]:
    return {
        "episode": {
            "episode_id": context.episode.episode_id,
            "target_key": context.episode.target_key,
            "direction": context.episode.direction,
            "opened_at": context.episode.opened_at.isoformat(),
            "closed_at": None,
        },
        "open_segment": {
            "segment_id": context.open_segment.segment_id,
            "target_weight": context.open_segment.target_weight,
            "opened_at": context.open_segment.opened_at.isoformat(),
            "opened_by_state_change_id": context.open_segment.opened_by_state_change_id,
        },
        "state_changes": [
            _serialize_state_change(state_change) for state_change in context.state_changes
        ],
        "original_evidence": [
            _serialize_evidence_record(record) for record in context.original_evidence
        ],
        "later_evidence": [_serialize_evidence_record(record) for record in context.later_evidence],
        "market_mapping": {
            "market_symbol": context.market_mapping.market_symbol,
            "market_session": context.market_mapping.market_session,
            "exchange_session_scope": context.market_mapping.exchange_session_scope,
            "exchange": context.market_mapping.exchange,
            "bar_granularity": context.market_mapping.bar_granularity,
        },
        "terminal_mark": {
            "episode_id": context.terminal_mark.episode_id,
            "replay_end_at": context.terminal_mark.replay_end_at.isoformat(),
            "entry_bar_start_at": context.terminal_mark.entry_bar_start_at.isoformat(),
            "mark_bar_start_at": context.terminal_mark.mark_bar_start_at.isoformat(),
            "entry_price": context.terminal_mark.entry_price,
            "mark_price": context.terminal_mark.mark_price,
            "underlying_return": context.terminal_mark.underlying_return,
            "strategy_return": context.terminal_mark.strategy_return,
            "target_weight": context.terminal_mark.target_weight,
        },
        "portfolio_feedback": _serialize_portfolio_feedback(context.portfolio_feedback),
        "market_context_usage": _serialize_market_context_usage(context.market_context_usage),
        "watchlist_context": _serialize_page(context.watchlist_context),
        "target_log_context": (
            None
            if context.target_log_context is None
            else _serialize_page(context.target_log_context)
        ),
    }


def _serialize_target_close_context(
    context: CloseEpisodeReflectionContext,
) -> dict[str, object]:
    return {
        "episode": {
            "episode_id": context.episode.episode_id,
            "target_key": context.episode.target_key,
            "direction": context.episode.direction,
            "opened_at": context.episode.opened_at.isoformat(),
            "closed_at": (
                None if context.episode.closed_at is None else context.episode.closed_at.isoformat()
            ),
            "close_reason": context.episode.close_reason,
        },
        "state_changes": [
            _serialize_state_change(state_change) for state_change in context.state_changes
        ],
        "original_evidence": [
            _serialize_evidence_record(record) for record in context.original_evidence
        ],
        "later_evidence": [_serialize_evidence_record(record) for record in context.later_evidence],
        "market_mapping": {
            "market_symbol": context.market_mapping.market_symbol,
            "market_session": context.market_mapping.market_session,
            "exchange_session_scope": context.market_mapping.exchange_session_scope,
            "exchange": context.market_mapping.exchange,
            "bar_granularity": context.market_mapping.bar_granularity,
        },
        "market_returns": _serialize_market_returns(context.market_returns),
        "portfolio_feedback": _serialize_portfolio_feedback(context.portfolio_feedback),
        "market_context_usage": _serialize_market_context_usage(context.market_context_usage),
        "watchlist_context": _serialize_page(context.watchlist_context),
        "target_log_context": (
            None
            if context.target_log_context is None
            else _serialize_page(context.target_log_context)
        ),
    }


def _serialize_anchor_entry(entry: ReflectionLogEntry) -> dict[str, object]:
    entry_text = bounded_text_payload(
        entry.entry_md,
        limit=_REFLECTION_PAGE_EXCERPT_CHAR_LIMIT,
    )
    body_text = bounded_text_payload(
        entry.body_md,
        limit=_REFLECTION_PAGE_EXCERPT_CHAR_LIMIT,
    )
    return {
        "heading_text": entry.heading_text,
        "entry_sha256": entry_text["sha256"],
        "entry_char_count": entry_text["char_count"],
        "entry_truncated": entry_text["truncated"],
        "entry_excerpt_md": entry_text["excerpt"],
        "body_sha256": body_text["sha256"],
        "body_char_count": body_text["char_count"],
        "body_truncated": body_text["truncated"],
        "body_excerpt_md": body_text["excerpt"],
        "cited_event_ids": list(entry.cited_event_ids),
        "cited_source_refs": list(entry.cited_source_refs),
    }


def _serialize_evidence_record(record: EvidenceLedgerRecord) -> dict[str, object]:
    content = bounded_text_payload(
        record.content,
        limit=_REFLECTION_EVIDENCE_EXCERPT_CHAR_LIMIT,
    )
    return {
        "event_id": record.event_id,
        "target_key": record.target_key,
        "source_ref": record.source_ref,
        "title": record.title,
        "content_sha256": content["sha256"],
        "content_char_count": content["char_count"],
        "content_truncated": content["truncated"],
        "content_excerpt": content["excerpt"],
        "labels": list(record.labels),
        "ts_source": record.ts_source.isoformat(),
        "ts_event": record.ts_event.isoformat(),
        "ts_init": record.ts_init.isoformat(),
    }


def _serialize_page(page: PageReadResult) -> dict[str, object]:
    content = bounded_text_payload(
        page.content_md,
        limit=_REFLECTION_PAGE_EXCERPT_CHAR_LIMIT,
    )
    return {
        "page_path": page.page_path,
        "content_sha256": content["sha256"],
        "content_char_count": content["char_count"],
        "content_truncated": content["truncated"],
        "content_excerpt_md": content["excerpt"],
    }


def _serialize_bounded_markdown(value: str) -> dict[str, object]:
    content = bounded_text_payload(
        value,
        limit=_REFLECTION_PAGE_EXCERPT_CHAR_LIMIT,
    )
    return {
        "sha256": content["sha256"],
        "char_count": content["char_count"],
        "truncated": content["truncated"],
        "excerpt_md": content["excerpt"],
    }


def _serialize_state_change(state_change: ViewStateChange) -> dict[str, object]:
    rationale = bounded_text_payload(
        state_change.rationale_md,
        limit=_REFLECTION_STATE_RATIONALE_EXCERPT_CHAR_LIMIT,
    )
    return {
        "state_change_id": state_change.state_change_id,
        "state": state_change.state,
        "direction": state_change.direction,
        "conviction": state_change.conviction,
        "target_weight": state_change.target_weight,
        "effective_at": state_change.effective_at.isoformat(),
        "source_event_ids": list(state_change.source_event_ids),
        "rationale_sha256": rationale["sha256"],
        "rationale_char_count": rationale["char_count"],
        "rationale_truncated": rationale["truncated"],
        "rationale_excerpt_md": rationale["excerpt"],
    }


def _serialize_market_returns(result: ValidationReturnsResult) -> dict[str, object]:
    return {
        "episode_returns": [
            {
                "episode_id": item.episode_id,
                "target_key": item.target_key,
                "direction": item.direction,
                "entry_bar_start_at": item.entry_bar_start_at.isoformat(),
                "exit_bar_start_at": item.exit_bar_start_at.isoformat(),
                "entry_price": item.entry_price,
                "exit_price": item.exit_price,
                "underlying_return": item.underlying_return,
                "strategy_return": item.strategy_return,
            }
            for item in result.episode_returns
        ],
        "horizon_baselines": [
            _serialize_horizon_baseline(item) for item in result.horizon_baselines
        ],
    }


def _serialize_portfolio_feedback(
    feedback: PortfolioFeedbackSnapshot | None,
) -> dict[str, object] | None:
    if feedback is None:
        return None
    return {
        "target_key": feedback.target_key,
        "run_id": feedback.run_id,
        "window_start": feedback.window_start.isoformat(),
        "window_end": feedback.window_end.isoformat(),
        "portfolio_facts": feedback.portfolio_facts.to_dict(),
        "segment_facts": [item.to_dict() for item in feedback.segment_facts],
        "decision_facts": [item.to_dict() for item in feedback.decision_facts],
        "exposure_path_count": len(feedback.exposure_path),
    }


def _serialize_portfolio_feedback_brief(
    feedback: PortfolioFeedbackSnapshot | None,
) -> dict[str, object] | None:
    if feedback is None:
        return None
    return {
        "target_key": feedback.target_key,
        "run_id": feedback.run_id,
        "window_start": feedback.window_start.isoformat(),
        "window_end": feedback.window_end.isoformat(),
        "portfolio_facts": feedback.portfolio_facts.to_dict(),
        "segment_facts": [item.to_dict() for item in feedback.segment_facts],
        "decision_facts": [item.to_dict() for item in feedback.decision_facts],
        "exposure_path_count": len(feedback.exposure_path),
        "exposure_path_available_from_validation": True,
    }


def _serialize_counterfactual_summary(
    report: CounterfactualEvaluationReport | None,
) -> dict[str, object] | None:
    if report is None:
        return None
    available = tuple(
        result
        for result in report.baseline_results
        if result.status == "available" and result.baseline_return is not None
    )
    best = max(available, key=_counterfactual_baseline_return, default=None)
    worst = min(available, key=_counterfactual_baseline_return, default=None)
    return {
        "artifact_kind": "post_horizon_counterfactual_evaluation",
        "usage_boundary": "post_horizon_error_attribution_only_not_decision_time_context",
        "episode_id": report.episode_id,
        "target_key": report.target_key,
        "actual_return": report.actual_return,
        "market_provenance_hash": report.market_provenance_hash,
        "baseline_returns": {
            result.baseline_type: {
                "status": result.status,
                "return": result.baseline_return,
                "delta_vs_actual": result.delta_vs_actual,
                "reason": result.reason,
            }
            for result in report.baseline_results
        },
        "best_baseline": None if best is None else best.baseline_type,
        "worst_baseline": None if worst is None else worst.baseline_type,
        "episode_metrics": (
            None if report.episode_metrics is None else report.episode_metrics.to_json_payload()
        ),
        "decision_quality_attribution": (
            None
            if report.decision_quality_attribution is None
            else report.decision_quality_attribution.to_json_payload()
        ),
    }


def _counterfactual_baseline_return(result: CounterfactualBaselineResult) -> float:
    if result.baseline_return is None:
        raise RuntimeError("available counterfactual baseline is missing baseline_return.")
    return result.baseline_return


def _serialize_market_context_usage(
    usage: MarketContextUsageFacts | None,
) -> dict[str, object] | None:
    if usage is None:
        return None
    return {
        "context_visible": usage.context_visible,
        "source_event_ids": list(usage.source_event_ids),
        "warnings": list(usage.warnings),
        "decision_usages": [
            {
                "state_change_id": item.state_change_id,
                "effective_at": item.effective_at.isoformat(),
                "context_visible": item.context_visible,
                "market_context_hash": item.market_context_hash,
                "component_statuses": dict(sorted(item.component_statuses.items())),
                "calculation_versions": dict(sorted(item.calculation_versions.items())),
                "available_components": list(item.available_components),
                "partial_or_unavailable_components": list(item.partial_or_unavailable_components),
                "source_receipt_type": item.source_receipt_type,
                "source_event_ids": list(item.source_event_ids),
                "decision_text_reference_status": item.decision_text_reference_status,
                "referenced_components": list(item.referenced_components),
                "unreferenced_available_components": list(item.unreferenced_available_components),
                "market_tool_call_count": item.market_tool_call_count,
                "market_tool_statuses": list(item.market_tool_statuses),
                "market_tool_calls": [call.to_dict() for call in item.market_tool_calls],
                "warnings": list(item.warnings),
            }
            for item in usage.decision_usages
        ],
        "post_decision_outcome_summary": usage.post_decision_outcome_summary,
    }


def _serialize_horizon_baseline(item: HorizonBaseline) -> dict[str, object]:
    return {
        "episode_id": item.episode_id,
        "target_key": item.target_key,
        "horizon_hours": item.horizon.total_seconds() / 3600.0,
        "horizon_end_at": item.horizon_end_at.isoformat(),
        "status": item.status,
        "entry_bar_start_at": item.entry_bar_start_at.isoformat(),
        "exit_bar_start_at": (
            None if item.exit_bar_start_at is None else item.exit_bar_start_at.isoformat()
        ),
        "underlying_return": item.underlying_return,
        "strategy_return": item.strategy_return,
        "bar_count": item.bar_count,
    }


def _require_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ReflectionPromptContractError(f"{field_name} must be a datetime.")
    return value


__all__ = [
    "ReflectionPromptContractError",
    "build_open_position_reflection_prompt",
    "build_shared_reflection_prompt",
    "build_target_close_reflection_prompt",
    "build_target_reflection_prompt",
    "target_reflection_window_end",
]
