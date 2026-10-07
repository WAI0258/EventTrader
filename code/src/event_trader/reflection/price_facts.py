"""Deterministic completed-close facts for Reflection watchlist review."""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import isclose, isfinite
from typing import cast

from event_trader.reflection.contracts import (
    ReflectionPriceRelation,
    ReflectionPriceRelationAssessment,
    ReflectionPriceTriggerAssessment,
    ReflectionPriceTriggerKind,
    ReflectionPriceTriggerStatus,
)
from event_trader.reflection.view_contracts import OpenPositionReflectionContext

_NUMBER_PATTERN = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_ACTIVE_LEVEL_LINE = re.compile(
    rf"^- Monitor active level `(?P<level_id>[^`]+)` at "
    rf"`(?P<lower>{_NUMBER_PATTERN})(?:-(?P<upper>{_NUMBER_PATTERN}))?` "
    r"`(?P<instrument_basis>[^`]+)`;"
)
_ESCALATION_TRIGGER_LINE = re.compile(
    rf"^- Escalate on (?P<trigger_kind>refresh|invalidation) trigger "
    rf"`(?P<trigger_id>[^`]+)` for active level `(?P<level_id>[^`]+)` at "
    rf"`(?P<lower>{_NUMBER_PATTERN})(?:-(?P<upper>{_NUMBER_PATTERN}))?` "
    r"`(?P<instrument_basis>[^`]+)`\."
)
_PRICE_CLOSE_TRIGGER = re.compile(
    rf"^(?:price_(?P<price_direction>above|below)_(?P<price_threshold>{_NUMBER_PATTERN})_close|"
    rf"close_(?P<close_direction>above|below)_(?P<close_threshold>{_NUMBER_PATTERN}))$"
)
_POINT_TOLERANCE = 1e-9


class ReflectionPriceFactsError(ValueError):
    """Raised when project-owned watchlist price facts are malformed."""


@dataclass(frozen=True, slots=True)
class WatchlistPriceRelationFact:
    level_id: str
    lower: float
    upper: float
    instrument_basis: str
    observed_price: float
    relation: ReflectionPriceRelation

    def to_json_payload(self) -> dict[str, object]:
        return {
            "level_id": self.level_id,
            "lower": self.lower,
            "upper": self.upper,
            "instrument_basis": self.instrument_basis,
            "observed_price": self.observed_price,
            "relation": self.relation,
        }

    def to_assessment(self) -> ReflectionPriceRelationAssessment:
        return ReflectionPriceRelationAssessment(
            level_id=self.level_id,
            relation=self.relation,
        )


@dataclass(frozen=True, slots=True)
class WatchlistPriceTriggerFact:
    level_id: str
    trigger_kind: ReflectionPriceTriggerKind
    trigger_id: str
    observed_close: float
    status: ReflectionPriceTriggerStatus

    def to_json_payload(self) -> dict[str, object]:
        return {
            "level_id": self.level_id,
            "trigger_kind": self.trigger_kind,
            "trigger_id": self.trigger_id,
            "observed_close": self.observed_close,
            "observed_price_basis": "completed_bar_close",
            "status": self.status,
        }

    def to_assessment(self) -> ReflectionPriceTriggerAssessment:
        return ReflectionPriceTriggerAssessment(
            level_id=self.level_id,
            trigger_kind=self.trigger_kind,
            trigger_id=self.trigger_id,
            status=self.status,
        )


def open_position_watchlist_price_facts(
    context: OpenPositionReflectionContext,
) -> tuple[WatchlistPriceRelationFact, ...]:
    return watchlist_price_relation_facts(
        content_md=context.watchlist_context.content_md,
        observed_price=context.terminal_mark.mark_price,
    )


def open_position_watchlist_trigger_facts(
    context: OpenPositionReflectionContext,
) -> tuple[WatchlistPriceTriggerFact, ...]:
    return watchlist_price_trigger_facts(
        content_md=context.watchlist_context.content_md,
        observed_close=context.terminal_mark.mark_price,
    )


def watchlist_price_relation_facts(
    *,
    content_md: str,
    observed_price: float,
) -> tuple[WatchlistPriceRelationFact, ...]:
    """Recover relations from the canonical active-watchlist projection."""

    if not isinstance(content_md, str) or not content_md.strip():
        raise ReflectionPriceFactsError("watchlist content_md must be non-blank.")
    if (
        isinstance(observed_price, bool)
        or not isinstance(observed_price, int | float)
        or not isfinite(float(observed_price))
    ):
        raise ReflectionPriceFactsError("observed_price must be finite.")
    normalized_price = float(observed_price)
    facts: list[WatchlistPriceRelationFact] = []
    seen_level_ids: set[str] = set()
    for raw_line in content_md.splitlines():
        line = raw_line.strip()
        if not line.startswith("- Monitor active level "):
            continue
        match = _ACTIVE_LEVEL_LINE.match(line)
        if match is None:
            raise ReflectionPriceFactsError(
                "canonical watchlist active-level line is malformed: " + line
            )
        level_id = match.group("level_id")
        if level_id in seen_level_ids:
            raise ReflectionPriceFactsError(
                f"canonical watchlist contains duplicate active level_id {level_id!r}."
            )
        seen_level_ids.add(level_id)
        lower = float(match.group("lower"))
        upper_text = match.group("upper")
        upper = lower if upper_text is None else float(upper_text)
        if lower > upper:
            raise ReflectionPriceFactsError(
                f"canonical watchlist level {level_id!r} has lower > upper."
            )
        facts.append(
            WatchlistPriceRelationFact(
                level_id=level_id,
                lower=lower,
                upper=upper,
                instrument_basis=match.group("instrument_basis"),
                observed_price=normalized_price,
                relation=_price_relation(
                    observed_price=normalized_price,
                    lower=lower,
                    upper=upper,
                ),
            )
        )
    return tuple(facts)


def price_relation_assessments(
    facts: tuple[WatchlistPriceRelationFact, ...],
) -> tuple[ReflectionPriceRelationAssessment, ...]:
    return tuple(fact.to_assessment() for fact in facts)


def watchlist_price_trigger_facts(
    *,
    content_md: str,
    observed_close: float,
) -> tuple[WatchlistPriceTriggerFact, ...]:
    """Evaluate canonical escalation triggers against the completed terminal close."""

    if not isinstance(content_md, str) or not content_md.strip():
        raise ReflectionPriceFactsError("watchlist content_md must be non-blank.")
    if (
        isinstance(observed_close, bool)
        or not isinstance(observed_close, int | float)
        or not isfinite(float(observed_close))
    ):
        raise ReflectionPriceFactsError("observed_close must be finite.")
    normalized_close = float(observed_close)
    facts: list[WatchlistPriceTriggerFact] = []
    seen: set[tuple[str, ReflectionPriceTriggerKind, str]] = set()
    for raw_line in content_md.splitlines():
        line = raw_line.strip()
        if not line.startswith("- Escalate on "):
            continue
        match = _ESCALATION_TRIGGER_LINE.match(line)
        if match is None:
            raise ReflectionPriceFactsError(
                "canonical watchlist escalation-trigger line is malformed: " + line
            )
        trigger_kind = match.group("trigger_kind")
        if trigger_kind not in {"refresh", "invalidation"}:
            raise ReflectionPriceFactsError(
                f"unsupported watchlist trigger kind {trigger_kind!r}."
            )
        normalized_kind = cast(ReflectionPriceTriggerKind, trigger_kind)
        trigger_id = match.group("trigger_id")
        level_id = match.group("level_id")
        identity = (level_id, normalized_kind, trigger_id)
        if identity in seen:
            raise ReflectionPriceFactsError(
                "canonical watchlist contains duplicate escalation trigger "
                f"{identity!r}."
            )
        seen.add(identity)
        facts.append(
            WatchlistPriceTriggerFact(
                level_id=level_id,
                trigger_kind=normalized_kind,
                trigger_id=trigger_id,
                observed_close=normalized_close,
                status=_price_trigger_status(
                    trigger_id=trigger_id,
                    observed_close=normalized_close,
                ),
            )
        )
    return tuple(facts)


def price_trigger_assessments(
    facts: tuple[WatchlistPriceTriggerFact, ...],
) -> tuple[ReflectionPriceTriggerAssessment, ...]:
    return tuple(fact.to_assessment() for fact in facts)


def _price_relation(
    *,
    observed_price: float,
    lower: float,
    upper: float,
) -> ReflectionPriceRelation:
    if observed_price < lower and not isclose(
        observed_price,
        lower,
        rel_tol=0.0,
        abs_tol=_POINT_TOLERANCE,
    ):
        return "below"
    if observed_price > upper and not isclose(
        observed_price,
        upper,
        rel_tol=0.0,
        abs_tol=_POINT_TOLERANCE,
    ):
        return "above"
    return "within"


def _price_trigger_status(
    *,
    trigger_id: str,
    observed_close: float,
) -> ReflectionPriceTriggerStatus:
    match = _PRICE_CLOSE_TRIGGER.fullmatch(trigger_id)
    if match is None:
        return "not_evaluable"
    direction = match.group("price_direction") or match.group("close_direction")
    threshold_text = match.group("price_threshold") or match.group("close_threshold")
    if threshold_text is None:
        raise ReflectionPriceFactsError(
            f"price-close trigger {trigger_id!r} is missing its threshold."
        )
    threshold = float(threshold_text)
    if direction == "above":
        return "met" if observed_close > threshold else "not_met"
    if direction == "below":
        return "met" if observed_close < threshold else "not_met"
    raise ReflectionPriceFactsError(
        f"price-close trigger {trigger_id!r} has an unsupported direction."
    )


__all__ = [
    "ReflectionPriceFactsError",
    "WatchlistPriceRelationFact",
    "WatchlistPriceTriggerFact",
    "open_position_watchlist_price_facts",
    "open_position_watchlist_trigger_facts",
    "price_relation_assessments",
    "price_trigger_assessments",
    "watchlist_price_relation_facts",
    "watchlist_price_trigger_facts",
]
