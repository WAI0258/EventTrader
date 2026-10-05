"""Deterministic active-section projection from structured analysis truth."""

from __future__ import annotations

from dataclasses import dataclass

from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.analysis_price_semantics import AnalysisPriceSemantics
from event_trader.contracts.price_level_role import PriceLevelRole
from event_trader.research_memory.analysis_price_basis import (
    canonicalize_instrument_basis,
    narrowed_active_price_levels,
)

_NON_PRICE_PROSE_DIGITS = frozenset("0123456789")
_ACTIONABLE_FLAT_ROLES = frozenset(
    {
        "entry",
        "add",
        "de_risk_or_take_profit",
        "exit",
        "reverse_or_cover",
        "invalidates",
        "watch_only",
    }
)
_INVALIDATION_ROLES = frozenset({"invalidates"})


@dataclass(frozen=True, slots=True)
class ActivePriceProjectionSection:
    page_path: str
    section_name: str
    content_md: str

    @property
    def section_id(self) -> str:
        return f"{self.page_path}:{self.section_name}"


def project_active_price_sections(
    assessment: AnalysisAssessment | None,
) -> tuple[ActivePriceProjectionSection, ...]:
    if assessment is None or assessment.analysis_price_semantics is None:
        return ()
    semantics = assessment.analysis_price_semantics
    active_levels = active_price_levels(assessment)
    if not active_levels:
        return ()
    target_key = assessment.target_key
    return (
        ActivePriceProjectionSection(
            page_path=f"targets/{target_key}/thesis.md",
            section_name="Market Setup Dashboard",
            content_md=_render_market_setup_dashboard(
                assessment=assessment,
                semantics=semantics,
                active_levels=active_levels,
            ),
        ),
        ActivePriceProjectionSection(
            page_path=f"targets/{target_key}/thesis.md",
            section_name="Invalidation",
            content_md=_render_invalidation(assessment, active_levels),
        ),
        ActivePriceProjectionSection(
            page_path=f"targets/{target_key}/watchlist.md",
            section_name="Immediate Watch Items",
            content_md=_render_immediate_watch_items(active_levels),
        ),
        ActivePriceProjectionSection(
            page_path=f"targets/{target_key}/watchlist.md",
            section_name="Triggers To Escalate",
            content_md=_render_triggers_to_escalate(active_levels),
        ),
        ActivePriceProjectionSection(
            page_path=f"targets/{target_key}/risks.md",
            section_name="Failure Conditions",
            content_md=_render_failure_conditions(assessment, active_levels),
        ),
    )


def active_price_levels(
    assessment: AnalysisAssessment,
) -> tuple[PriceLevelRole, ...]:
    semantics = assessment.analysis_price_semantics
    if semantics is None:
        return ()
    narrowed_levels = narrowed_active_price_levels(
        assessment.price_level_roles,
        instrument_basis=semantics.instrument_basis,
    )
    if not narrowed_levels:
        return ()
    active_ids = set(semantics.active_price_level_ids)
    semantics_basis = canonicalize_instrument_basis(semantics.instrument_basis)
    return tuple(
        level
        for level in narrowed_levels
        if level.level_id in active_ids
        and canonicalize_instrument_basis(level.instrument_basis) == semantics_basis
    )


def projected_market_setup_dashboard_md(
    assessment: AnalysisAssessment | None,
) -> str | None:
    if assessment is None or assessment.analysis_price_semantics is None:
        return None
    active_levels = active_price_levels(assessment)
    if not active_levels:
        return None
    return _render_market_setup_dashboard(
        assessment=assessment,
        semantics=assessment.analysis_price_semantics,
        active_levels=active_levels,
    )


def _render_market_setup_dashboard(
    *,
    assessment: AnalysisAssessment,
    semantics: AnalysisPriceSemantics,
    active_levels: tuple[PriceLevelRole, ...],
) -> str:
    include_short_branch = _include_short_branch(assessment)
    lines = [
        "### Market Reference",
        (
            "- Last-reviewed analysis reference: "
            f"`{_format_number(semantics.analysis_reference_price)}` "
            f"`{semantics.instrument_basis}` at "
            f"`{semantics.analysis_reference_at.isoformat()}`."
        ),
    ]
    for label, value in (
        ("Window high", semantics.window_high),
        ("Window low", semantics.window_low),
        ("Swing high", semantics.swing_high),
        ("Swing low", semantics.swing_low),
        ("Current leg start", semantics.current_leg_start_price),
        ("Current leg end", semantics.current_leg_end_price),
    ):
        if value is None:
            continue
        lines.append(f"- {label}: `{_format_number(value)}` `{semantics.instrument_basis}`.")
    lines.extend(
        [
            "",
            "### Setup Frame",
            f"- As-if-flat state: `{assessment.as_if_flat_state}`.",
        ]
    )
    rationale_line = _safe_non_price_line(assessment.as_if_flat_rationale_md)
    if rationale_line is not None:
        lines.append(f"- As-if-flat rationale: {rationale_line}")
    for level in active_levels:
        line = (
            "- Active level "
            f"`{level.level_id}` at `{_level_anchor(level)}` "
            f"`{level.instrument_basis}`; flat=`{level.role_if_flat}`, "
            f"long=`{level.role_if_already_long}`"
        )
        if include_short_branch:
            line += f", short=`{level.role_if_already_short}`"
        lines.append(f"{line}.")
    lines.extend(
        [
            "- Activation and confirmation depend on how price behaves around the "
            "active levels above.",
            "- Invalidation remains tied to the dedicated level boundaries listed below.",
            "",
            "### Watch Triggers",
        ]
    )
    lines.extend(_watch_trigger_lines(active_levels))
    lines.extend(["", "### Evidence Gaps"])
    evidence_gap_line = _safe_non_price_line(assessment.missing_evidence_md)
    lines.append(
        "- Evidence gaps remain unchanged from the broader assessment narrative."
        if evidence_gap_line is None
        else f"- {evidence_gap_line}"
    )
    return "\n".join(lines)


def _render_invalidation(
    assessment: AnalysisAssessment,
    active_levels: tuple[PriceLevelRole, ...],
) -> str:
    lines = _invalidation_lines(
        active_levels,
        include_short_branch=_include_short_branch(assessment),
    )
    if not lines:
        lines = ["- No separate invalidation boundary is active beyond the current setup levels."]
    return "\n".join(lines)


def _render_immediate_watch_items(active_levels: tuple[PriceLevelRole, ...]) -> str:
    lines: list[str] = []
    for level in active_levels:
        refresh_text = _join_values(level.refresh_triggers)
        path_text = (
            "path context required"
            if level.path_context_required
            else "path context optional"
        )
        lines.append(
            "- Monitor active level "
            f"`{level.level_id}` at `{_level_anchor(level)}` "
            f"`{level.instrument_basis}`; flat=`{level.role_if_flat}`; "
            f"refresh_triggers={refresh_text}; {path_text}."
        )
    return "\n".join(lines)


def _render_triggers_to_escalate(active_levels: tuple[PriceLevelRole, ...]) -> str:
    lines: list[str] = []
    for level in active_levels:
        for trigger in level.refresh_triggers:
            lines.append(
                "- Escalate on refresh trigger "
                f"`{trigger}` for active level `{level.level_id}` at "
                f"`{_level_anchor(level)}` `{level.instrument_basis}`."
            )
        for trigger in level.invalidation_triggers:
            lines.append(
                "- Escalate on invalidation trigger "
                f"`{trigger}` for active level `{level.level_id}` at "
                f"`{_level_anchor(level)}` `{level.instrument_basis}`."
            )
    if not lines:
        return "- No separate escalation trigger is active beyond the current setup levels."
    return "\n".join(lines)


def _render_failure_conditions(
    assessment: AnalysisAssessment,
    active_levels: tuple[PriceLevelRole, ...],
) -> str:
    include_short_branch = _include_short_branch(assessment)
    lines: list[str] = []
    for level in active_levels:
        should_render = bool(level.invalidation_triggers) or any(
            role in _INVALIDATION_ROLES
            for role in (
                level.role_if_flat,
                level.role_if_already_long,
                *((level.role_if_already_short,) if include_short_branch else ()),
            )
        )
        if not should_render:
            continue
        line = (
            "- Failure boundary "
            f"`{level.level_id}` at `{_level_anchor(level)}` "
            f"`{level.instrument_basis}`; flat=`{level.role_if_flat}`, "
            f"long=`{level.role_if_already_long}`"
        )
        if include_short_branch:
            line += f", short=`{level.role_if_already_short}`"
        line += (
            "; "
            f"invalidation_triggers={_join_values(level.invalidation_triggers)}."
        )
        lines.append(line)
    if not lines:
        return "- No separate failure-condition boundary is active beyond the current setup levels."
    return "\n".join(lines)


def _invalidation_lines(
    active_levels: tuple[PriceLevelRole, ...],
    *,
    include_short_branch: bool,
) -> list[str]:
    lines: list[str] = []
    for level in active_levels:
        invalidates_for = [
            state_name
            for state_name, role in (
                ("flat", level.role_if_flat),
                ("already_long", level.role_if_already_long),
                *(
                    (("already_short", level.role_if_already_short),)
                    if include_short_branch
                    else ()
                ),
            )
            if role == "invalidates"
        ]
        if not invalidates_for and not level.invalidation_triggers:
            continue
        lines.append(
            "- Active invalidation level "
            f"`{level.level_id}` at `{_level_anchor(level)}` "
            f"`{level.instrument_basis}`; invalidates_for={_join_values(invalidates_for)}; "
            f"invalidation_triggers={_join_values(level.invalidation_triggers)}."
        )
    return lines


def _watch_trigger_lines(active_levels: tuple[PriceLevelRole, ...]) -> list[str]:
    lines: list[str] = []
    for level in active_levels:
        triggers = tuple(
            dict.fromkeys((*level.refresh_triggers, *level.invalidation_triggers))
        )
        if level.role_if_flat not in _ACTIONABLE_FLAT_ROLES and not triggers:
            continue
        line = (
            "- Active watch focus "
            f"`{level.level_id}` at `{_level_anchor(level)}` "
            f"`{level.instrument_basis}`; flat=`{level.role_if_flat}`"
        )
        if triggers:
            line += f"; triggers={_join_values(triggers)}"
        lines.append(f"{line}.")
    return lines


def _level_anchor(level: PriceLevelRole) -> str:
    if level.value is not None:
        return _format_number(level.value)
    assert level.lower is not None
    assert level.upper is not None
    return f"{_format_number(level.lower)}-{_format_number(level.upper)}"


def _format_number(value: float) -> str:
    return str(value)


def _join_values(values: tuple[str, ...] | list[str]) -> str:
    normalized = tuple(value for value in values if value)
    return "`none`" if not normalized else ", ".join(f"`{value}`" for value in normalized)


def _safe_non_price_line(content_md: str) -> str | None:
    normalized = " ".join(content_md.split())
    if not normalized or any(char in _NON_PRICE_PROSE_DIGITS for char in normalized):
        return None
    return normalized


def _include_short_branch(assessment: AnalysisAssessment) -> bool:
    return assessment.if_already_short_implication_md is not None


__all__ = [
    "ActivePriceProjectionSection",
    "active_price_levels",
    "project_active_price_sections",
    "projected_market_setup_dashboard_md",
]
