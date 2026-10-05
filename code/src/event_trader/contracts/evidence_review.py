"""Deterministic evidence review dimensions for replay audit."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from event_trader.source_policy import (
    EVENT_TYPE_LABEL_PREFIX,
    SOURCE_KIND_LABEL_PREFIX,
    SourcePolicyError,
    extract_event_type,
    extract_source_kind,
)

from .evidence import EvidenceLedgerRecord

EvidenceRole = Literal[
    "factual_event",
    "subjective_view",
    "price_recap",
    "mixed",
    "duplicate",
    "unknown",
]
FactSubjectivityBalance = Literal[
    "fact_dominant",
    "subjective_dominant",
    "balanced",
    "unclear",
]
RecapRisk = Literal["none", "low", "medium", "high"]

_FACTUAL_EVENT_TYPES = frozenset(
    {
        "monetary_policy",
        "macro_data",
        "fiscal_policy",
        "trade_tariff",
        "geopolitical_security",
        "central_bank_reserve",
        "market_structure",
        "flow_positioning",
        "liquidity_funding",
        "volatility_risk",
        "physical_demand_supply",
        "inventory_storage",
        "weather_disruption",
        "issuer_corporate",
        "sector_industry",
        "protocol_network",
        "regulatory_legal",
    }
)
_FACTUAL_SOURCE_KINDS = frozenset(
    {"official", "news_report", "data_page", "company_release", "protocol_release"}
)
_SUBJECTIVE_SOURCE_KINDS = frozenset({"commentary", "research_note"})
_PRICE_RECAP_TERMS = frozenset(
    {
        "rises",
        "falls",
        "surges",
        "drops",
        "settles",
        "hits",
        "trading at",
        "price action",
        "support",
        "resistance",
    }
)
_CAUSAL_TERMS = frozenset(
    {
        "because",
        "after",
        "as",
        "due to",
        "driven by",
        "following",
        "on fed",
        "on cpi",
        "policy",
        "data",
    }
)
_ROLE_PRIORITY: tuple[EvidenceRole, ...] = (
    "mixed",
    "factual_event",
    "subjective_view",
    "price_recap",
    "duplicate",
    "unknown",
)
_BALANCE_ORDER: tuple[FactSubjectivityBalance, ...] = (
    "fact_dominant",
    "subjective_dominant",
    "balanced",
    "unclear",
)
_RECAP_RISK_RANK: dict[RecapRisk, int] = {
    "none": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}


@dataclass(frozen=True, slots=True)
class EvidenceReviewDimensions:
    """Small serializable review dimensions for one evidence record."""

    evidence_role: EvidenceRole
    fact_subjectivity_balance: FactSubjectivityBalance
    recap_risk: RecapRisk
    rationale: str

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_role": self.evidence_role,
            "fact_subjectivity_balance": self.fact_subjectivity_balance,
            "recap_risk": self.recap_risk,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class EvidenceReviewSummary:
    """Aggregate dimensions for a set of admitted evidence records."""

    primary_evidence_role: EvidenceRole
    role_counts: tuple[tuple[EvidenceRole, int], ...]
    recap_risk_max: RecapRisk
    fact_subjectivity_balance_counts: tuple[tuple[FactSubjectivityBalance, int], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "primary_evidence_role": self.primary_evidence_role,
            "role_counts": dict(self.role_counts),
            "recap_risk_max": self.recap_risk_max,
            "fact_subjectivity_balance_counts": dict(
                self.fact_subjectivity_balance_counts
            ),
        }


def classify_evidence_review_dimensions(
    record: EvidenceLedgerRecord,
) -> EvidenceReviewDimensions:
    """Classify one evidence record using deterministic source labels first."""

    if not isinstance(record, EvidenceLedgerRecord):
        raise TypeError("record must be an EvidenceLedgerRecord instance.")

    labels = list(record.labels)
    event_types = _label_values(labels, EVENT_TYPE_LABEL_PREFIX)
    source_kinds = _label_values(labels, SOURCE_KIND_LABEL_PREFIX)
    text = f"{record.title} {record.content}".casefold()
    has_price_recap = _contains_any(text, _PRICE_RECAP_TERMS)
    has_causal_language = _contains_any(text, _CAUSAL_TERMS)

    if _has_duplicate_label(labels):
        return EvidenceReviewDimensions(
            evidence_role="duplicate",
            fact_subjectivity_balance="unclear",
            recap_risk="low",
            rationale="Existing duplicate metadata marked this evidence as repeated.",
        )

    if _has_mixed_labels(event_types, source_kinds) or (
        has_price_recap and has_causal_language and "market_commentary" in event_types
    ):
        return EvidenceReviewDimensions(
            evidence_role="mixed",
            fact_subjectivity_balance="balanced",
            recap_risk="medium" if has_price_recap else "low",
            rationale=(
                "Evidence combines causal/factual signals with commentary or price "
                "recap features."
            ),
        )

    event_type, source_kind = _extract_known_labels(labels)
    if event_type == "price_action":
        return EvidenceReviewDimensions(
            evidence_role="price_recap",
            fact_subjectivity_balance="unclear",
            recap_risk="high",
            rationale="event_type:price_action is treated as price recap evidence.",
        )

    if event_type == "institutional_view" or source_kind == "research_note":
        return EvidenceReviewDimensions(
            evidence_role="subjective_view",
            fact_subjectivity_balance="subjective_dominant",
            recap_risk="low" if not has_price_recap else "medium",
            rationale=(
                "Institutional view or research-note source indicates subjective "
                "market interpretation."
            ),
        )

    if event_type == "market_commentary" and source_kind in _SUBJECTIVE_SOURCE_KINDS:
        return EvidenceReviewDimensions(
            evidence_role="subjective_view",
            fact_subjectivity_balance="subjective_dominant",
            recap_risk="medium" if has_price_recap else "low",
            rationale="Market commentary from a commentary source is subjective context.",
        )

    if event_type in _FACTUAL_EVENT_TYPES and source_kind in _FACTUAL_SOURCE_KINDS:
        return EvidenceReviewDimensions(
            evidence_role="factual_event",
            fact_subjectivity_balance="fact_dominant",
            recap_risk="none",
            rationale=(
                "Existing event_type/source_kind labels identify a factual event "
                "source."
            ),
        )

    return EvidenceReviewDimensions(
        evidence_role="unknown",
        fact_subjectivity_balance="unclear",
        recap_risk="low" if has_price_recap else "none",
        rationale="Existing labels did not map cleanly to a review dimension.",
    )


def summarize_evidence_review_dimensions(
    records: Iterable[EvidenceLedgerRecord],
) -> EvidenceReviewSummary:
    """Summarize deterministic review dimensions across admitted records."""

    dimensions = tuple(classify_evidence_review_dimensions(record) for record in records)
    if not dimensions:
        return EvidenceReviewSummary(
            primary_evidence_role="unknown",
            role_counts=(),
            recap_risk_max="none",
            fact_subjectivity_balance_counts=(),
        )

    role_counter: Counter[EvidenceRole] = Counter(
        item.evidence_role for item in dimensions
    )
    balance_counter: Counter[FactSubjectivityBalance] = Counter(
        item.fact_subjectivity_balance for item in dimensions
    )
    primary_role = max(
        role_counter,
        key=lambda role: (role_counter[role], -_ROLE_PRIORITY.index(role)),
    )
    recap_risk_max = max(
        (item.recap_risk for item in dimensions),
        key=lambda risk: _RECAP_RISK_RANK[risk],
    )
    return EvidenceReviewSummary(
        primary_evidence_role=primary_role,
        role_counts=tuple(
            (role, role_counter[role])
            for role in _ROLE_PRIORITY
            if role_counter[role]
        ),
        recap_risk_max=recap_risk_max,
        fact_subjectivity_balance_counts=tuple(
            (balance, balance_counter[balance])
            for balance in _BALANCE_ORDER
            if balance_counter[balance]
        ),
    )


def _extract_known_labels(labels: list[str]) -> tuple[str, str]:
    try:
        return extract_event_type(labels), extract_source_kind(labels)
    except SourcePolicyError:
        return "other", "unknown"


def _label_values(labels: list[str], prefix: str) -> tuple[str, ...]:
    return tuple(
        label.removeprefix(prefix).strip()
        for label in labels
        if isinstance(label, str) and label.startswith(prefix)
    )


def _has_duplicate_label(labels: list[str]) -> bool:
    return any(
        isinstance(label, str)
        and (
            label.casefold() == "event:duplicate"
            or label.casefold() == "event:noop_duplicate_source_ref"
            or label.casefold() == "event:duplicate_source_ref"
            or label.casefold() == "evidence_role:duplicate"
        )
        for label in labels
    )


def _has_mixed_labels(
    event_types: tuple[str, ...],
    source_kinds: tuple[str, ...],
) -> bool:
    if len(set(event_types)) > 1:
        return True
    if len(set(source_kinds)) > 1:
        return True
    event_type = event_types[0] if event_types else "other"
    source_kind = source_kinds[0] if source_kinds else "unknown"
    return (
        event_type in _FACTUAL_EVENT_TYPES
        and source_kind in _SUBJECTIVE_SOURCE_KINDS
    )


def _contains_any(text: str, terms: frozenset[str]) -> bool:
    return any(term in text for term in terms)


__all__ = [
    "EvidenceReviewDimensions",
    "EvidenceReviewSummary",
    "EvidenceRole",
    "FactSubjectivityBalance",
    "RecapRisk",
    "classify_evidence_review_dimensions",
    "summarize_evidence_review_dimensions",
]
