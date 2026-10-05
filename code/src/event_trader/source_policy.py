"""Deterministic source classification labels for evidence inputs."""

from __future__ import annotations

import re
from collections import Counter
from typing import Literal
from urllib.parse import urlparse


class SourcePolicyError(ValueError):
    """Raised when source classification labels violate the evidence contract."""


type EventType = Literal[
    "monetary_policy",
    "macro_data",
    "fiscal_policy",
    "trade_tariff",
    "geopolitical_security",
    "central_bank_reserve",
    "market_structure",
    "price_action",
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
    "institutional_view",
    "market_commentary",
    "other",
]
type SourceKind = Literal[
    "official",
    "news_report",
    "data_page",
    "commentary",
    "research_note",
    "company_release",
    "protocol_release",
    "operator_brief",
    "unknown",
]

EVENT_TYPE_LABEL_PREFIX = "event_type:"
SOURCE_KIND_LABEL_PREFIX = "source_kind:"
OPERATOR_CONFIDENCE_LABEL_PREFIX = "operator_confidence:"
OPERATOR_SOURCE_BASIS_LABEL_PREFIX = "operator_source_" + "basis:"
_DEPRECATED_SOURCE_AUTHORITY_PREFIX = "source_authority:"
_RETIRED_OPERATOR_BASIS_LABEL_PREFIX = OPERATOR_SOURCE_BASIS_LABEL_PREFIX.removeprefix(
    "operator_"
)
OPERATOR_CONFIDENCE_VALUES: frozenset[str] = frozenset(
    {"low", "medium", "high", "unknown"}
)

EVENT_TYPES: frozenset[str] = frozenset(
    {
        "monetary_policy",
        "macro_data",
        "fiscal_policy",
        "trade_tariff",
        "geopolitical_security",
        "central_bank_reserve",
        "market_structure",
        "price_action",
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
        "institutional_view",
        "market_commentary",
        "other",
    }
)
SOURCE_KINDS: frozenset[str] = frozenset(
    {
        "official",
        "news_report",
        "data_page",
        "commentary",
        "research_note",
        "company_release",
        "protocol_release",
        "operator_brief",
        "unknown",
    }
)

_OFFICIAL_DOMAINS = frozenset(
    {
        "federalreserve.gov",
        "bls.gov",
        "bea.gov",
        "treasury.gov",
        "whitehouse.gov",
        "state.gov",
        "cftc.gov",
        "cmegroup.com",
        "stats.gov.cn",
        "pbc.gov.cn",
        "ecb.europa.eu",
        "imf.org",
        "worldbank.org",
        "bis.org",
        "sec.gov",
    }
)
_NEWS_REPORT_DOMAINS = frozenset(
    {
        "reuters.com",
        "apnews.com",
        "bloomberg.com",
        "wsj.com",
        "ft.com",
        "cnbc.com",
        "bnnbloomberg.ca",
        "benzinga.com",
    }
)
_DATA_PAGE_DOMAINS = frozenset(
    {
        "goldprice.org",
        "pricegold.net",
        "tradingeconomics.com",
        "exchange-rates.org",
        "barchart.com",
        "macrotrends.net",
    }
)
_COMMENTARY_DOMAINS = frozenset(
    {
        "kitco.com",
        "forexlive.com",
        "dailyforex.com",
        "fxstreet.com",
        "fxleaders.com",
        "forex.com",
        "forex24.pro",
        "tradingview.com",
        "investing.com",
        "litefinance.org",
        "roboforex.com",
        "economies.com",
        "isabullion.com",
    }
)
_RESEARCH_NOTE_DOMAINS = frozenset(
    {
        "gold.org",
        "goldmansachs.com",
        "ssga.com",
        "goldsilver.com",
    }
)
_COMPANY_RELEASE_HINTS = (
    "investor relations",
    "press release",
    "shareholder letter",
    "financial results",
    "quarterly results",
)
_PROTOCOL_RELEASE_HINTS = ("github.com", "bitcoin.org", "ethereum.org")

_EVENT_TYPE_KEYWORDS: tuple[tuple[EventType, tuple[str, ...]], ...] = (
    (
        "monetary_policy",
        (
            "fomc",
            "federal reserve",
            "rate cut",
            "rate hike",
            "interest rates",
            "ecb",
            "monetary policy",
        ),
    ),
    (
        "macro_data",
        (
            "cpi",
            "ppi",
            "payrolls",
            "pmi",
            "gdp",
            "inflation data",
            "manufacturing",
            "retail sales",
            "jobless claims",
        ),
    ),
    (
        "fiscal_policy",
        (
            "budget",
            "fiscal",
            "deficit",
            "treasury issuance",
            "tax bill",
            "government spending",
        ),
    ),
    ("trade_tariff", ("tariff", "trade war", "import duty", "export controls")),
    (
        "geopolitical_security",
        (
            "war",
            "strike",
            "missile",
            "ceasefire",
            "iran",
            "ukraine",
            "gaza",
            "venezuela",
            "geopolitical",
        ),
    ),
    (
        "central_bank_reserve",
        (
            "central bank buying",
            "gold reserves",
            "pboc",
            "reserve purchases",
            "official sector gold",
        ),
    ),
    (
        "market_structure",
        (
            "margin requirement",
            "trading hours",
            "settlement",
            "clearing advisory",
            "exchange notice",
            "comex",
            "holiday schedule",
        ),
    ),
    (
        "price_action",
        (
            "settles",
            "rises",
            "falls",
            "surges",
            "steady",
            "hits",
            "trading at",
            "support",
            "resistance",
        ),
    ),
    (
        "flow_positioning",
        (
            "etf flow",
            "inflows",
            "outflows",
            "positioning",
            "cftc",
            "open interest",
            "fund flows",
        ),
    ),
    (
        "liquidity_funding",
        (
            "repo",
            "funding",
            "liquidity",
            "dollar liquidity",
            "stablecoin liquidity",
            "margin funding",
        ),
    ),
    (
        "volatility_risk",
        (
            "vix",
            "volatility",
            "risk parity",
            "tail hedge",
            "vol spike",
            "vol crush",
        ),
    ),
    (
        "physical_demand_supply",
        (
            "physical demand",
            "supply",
            "mine output",
            "jewelry demand",
            "refinery",
            "production",
        ),
    ),
    ("inventory_storage", ("inventory", "stockpile", "warehouse", "storage", "eia")),
    ("weather_disruption", ("weather", "storm", "hurricane", "drought", "flood")),
    (
        "institutional_view",
        (
            "price target",
            "downgraded from",
            "downgraded to",
            "downgrade rating",
            "downgrade",
            "downgraded",
            "upgraded from",
            "upgrades rating",
            "upgrade rating",
            "analyst",
            "strategist",
            "institutional",
            "bank says",
            "rating",
            "broker",
            "wall street",
            "upgraded to",
            "upgrades to",
            "upgrades after",
        ),
    ),
    (
        "issuer_corporate",
        (
            "earnings",
            "guidance",
            "revenue",
            "eps",
            "gross margin",
            "data center revenue",
            "earnings call",
            "earnings transcript",
            "transcript",
            "shareholder",
            "dividend",
            "buyback",
            "merger",
            "acquisition",
            "board",
        ),
    ),
    (
        "sector_industry",
        (
            "sector",
            "industry",
            "supply chain",
            "industry demand",
            "ai accelerator demand",
            "hyperscaler capex",
            "semiconductor industry",
        ),
    ),
    (
        "protocol_network",
        (
            "hashrate",
            "difficulty",
            "halving",
            "fork",
            "network outage",
            "protocol upgrade",
        ),
    ),
    (
        "regulatory_legal",
        (
            "sec",
            "cftc",
            "lawsuit",
            "court",
            "regulatory",
            "legal",
            "approval",
            "enforcement",
        ),
    ),
    (
        "market_commentary",
        (
            "market commentary",
            "market analysis",
            "technical analysis",
            "daily forecast",
            "traders weigh",
        ),
    ),
)


def ensure_source_classification_labels(
    *,
    source_ref: str,
    title: str,
    content: str,
    labels: list[str],
) -> list[str]:
    """Preserve committed classification labels or derive only the missing ones."""
    _reject_legacy_operator_basis_labels(labels)
    _reject_deprecated_source_authority(labels)
    event_type_label = _optional_existing_committed_label(
        labels=labels,
        prefix=EVENT_TYPE_LABEL_PREFIX,
        allowed_values=EVENT_TYPES,
    )
    if event_type_label is None:
        event_type_label = (
            f"{EVENT_TYPE_LABEL_PREFIX}"
            f"{infer_event_type(title=title, content=content, labels=labels)}"
        )
    source_kind_label = _optional_existing_committed_label(
        labels=labels,
        prefix=SOURCE_KIND_LABEL_PREFIX,
        allowed_values=SOURCE_KINDS,
    )
    if source_kind_label is None:
        source_kind_label = (
            f"{SOURCE_KIND_LABEL_PREFIX}{infer_source_kind(source_ref=source_ref, title=title)}"
        )
    existing = [
        label
        for label in labels
        if not label.startswith((EVENT_TYPE_LABEL_PREFIX, SOURCE_KIND_LABEL_PREFIX))
    ]
    normalized = [*existing, event_type_label, source_kind_label]
    _validate_operator_source_metadata(normalized, source_kind_label=source_kind_label)
    return normalized


def validate_source_classification_labels(*, labels: list[str]) -> list[str]:
    """Validate existing committed classification labels without re-deriving them."""
    _reject_legacy_operator_basis_labels(labels)
    _reject_deprecated_source_authority(labels)
    source_kind_label = _required_existing_committed_label(
        labels=labels,
        prefix=SOURCE_KIND_LABEL_PREFIX,
        allowed_values=SOURCE_KINDS,
    )
    _required_existing_committed_label(
        labels=labels,
        prefix=EVENT_TYPE_LABEL_PREFIX,
        allowed_values=EVENT_TYPES,
    )
    _validate_operator_source_metadata(labels, source_kind_label=source_kind_label)
    return list(labels)


def infer_source_kind(*, source_ref: str, title: str) -> SourceKind:
    """Infer objective source form from source identity and explicit page title."""
    if not isinstance(source_ref, str):
        raise SourcePolicyError("source_ref must be a string.")
    parsed = urlparse(source_ref.strip())
    scheme = parsed.scheme.lower()
    domain = _normalized_domain(parsed.netloc)
    title_text = title.casefold() if isinstance(title, str) else ""
    if scheme == "manual":
        return "unknown"
    if scheme == "operator":
        return "operator_brief"
    if _domain_matches(domain, _OFFICIAL_DOMAINS):
        return "official"
    if _domain_matches(domain, _NEWS_REPORT_DOMAINS):
        return "news_report"
    if _domain_matches(domain, _DATA_PAGE_DOMAINS):
        return "data_page"
    if _domain_matches(domain, _COMMENTARY_DOMAINS):
        return "commentary"
    if _domain_matches(domain, _RESEARCH_NOTE_DOMAINS):
        return "research_note"
    if any(hint in title_text for hint in _COMPANY_RELEASE_HINTS):
        return "company_release"
    if any(domain == hint or domain.endswith(f".{hint}") for hint in _PROTOCOL_RELEASE_HINTS):
        return "protocol_release"
    return "unknown"


def infer_event_type(*, title: str, content: str, labels: list[str]) -> EventType:
    """Infer one explicit event type from material text and existing objective labels."""
    text = _normalized_infer_text(
        title=title,
        content=content,
        labels=labels,
    )
    text_tokens = _infer_text_tokens(text)
    for event_type, keywords in _EVENT_TYPE_KEYWORDS:
        if any(
            _text_contains_keyword(text=text, text_tokens=text_tokens, keyword=keyword)
            for keyword in keywords
        ):
            return event_type
    return "other"


def _normalized_infer_text(*, title: str, content: str, labels: list[str]) -> str:
    return " ".join(
        (
            title if isinstance(title, str) else "",
            content if isinstance(content, str) else "",
            " ".join(label for label in labels if isinstance(label, str)),
        )
    ).casefold()


def _infer_text_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", text))


def _text_contains_keyword(*, text: str, text_tokens: set[str], keyword: str) -> bool:
    normalized_keyword = " ".join(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", keyword.casefold()))
    if not normalized_keyword:
        return False
    if " " in normalized_keyword:
        return normalized_keyword in text
    return normalized_keyword in text_tokens


def extract_event_type(labels: list[str]) -> EventType:
    return _extract_required_label_value(
        labels=labels,
        prefix=EVENT_TYPE_LABEL_PREFIX,
        allowed_values=EVENT_TYPES,
    )


def extract_source_kind(labels: list[str]) -> SourceKind:
    return _extract_required_label_value(
        labels=labels,
        prefix=SOURCE_KIND_LABEL_PREFIX,
        allowed_values=SOURCE_KINDS,
    )


def extract_operator_source_metadata(labels: list[str]) -> dict[str, str] | None:
    """Return operator source metadata when the evidence source is an operator brief."""
    _reject_legacy_operator_basis_labels(labels)
    source_kind = extract_source_kind(labels)
    confidence = _optional_single_label_value(
        labels=labels,
        prefix=OPERATOR_CONFIDENCE_LABEL_PREFIX,
        allowed_values=OPERATOR_CONFIDENCE_VALUES,
    )
    operator_source_basis = _optional_single_label_value(
        labels=labels,
        prefix=OPERATOR_SOURCE_BASIS_LABEL_PREFIX,
        allowed_values=None,
    )
    if source_kind != "operator_brief":
        if confidence is not None or operator_source_basis is not None:
            raise SourcePolicyError(
                "operator source metadata is allowed only with source_kind:operator_brief."
            )
        return None
    if confidence is None or operator_source_basis is None:
        raise SourcePolicyError(
            "source_kind:operator_brief requires operator_confidence and "
            "operator_source_basis labels."
        )
    if not operator_source_basis.strip():
        raise SourcePolicyError("operator_source_basis label must not be blank.")
    return {
        "operator_confidence": confidence,
        "operator_source_basis": operator_source_basis,
    }


def source_classification_counts(
    labels_by_row: list[list[str]],
) -> tuple[tuple[str, tuple[tuple[str, int], ...]], ...]:
    """Count event types and source kinds for operator-facing source mix reports."""
    event_type_counter: Counter[str] = Counter()
    source_kind_counter: Counter[str] = Counter()
    for labels in labels_by_row:
        event_type_counter[extract_event_type(labels)] += 1
        source_kind_counter[extract_source_kind(labels)] += 1
    return (
        (
            "event_type",
            tuple((key, event_type_counter[key]) for key in sorted(event_type_counter)),
        ),
        (
            "source_kind",
            tuple(
                (key, source_kind_counter[key]) for key in sorted(source_kind_counter)
            ),
        ),
    )


def _optional_existing_committed_label(
    *,
    labels: list[str],
    prefix: str,
    allowed_values: frozenset[str],
) -> str | None:
    existing = [label for label in labels if isinstance(label, str) and label.startswith(prefix)]
    if len(existing) > 1:
        raise SourcePolicyError(f"labels must contain exactly one {prefix.rstrip(':')} label.")
    if not existing:
        return None
    value = existing[0].removeprefix(prefix)
    if value not in allowed_values:
        raise SourcePolicyError(f"{prefix.rstrip(':')} label is not a committed value.")
    return existing[0]


def _required_existing_committed_label(
    *,
    labels: list[str],
    prefix: str,
    allowed_values: frozenset[str],
) -> str:
    existing = _optional_existing_committed_label(
        labels=labels,
        prefix=prefix,
        allowed_values=allowed_values,
    )
    if existing is None:
        raise SourcePolicyError(f"labels must contain exactly one {prefix.rstrip(':')} label.")
    return existing


def _extract_required_label_value(
    *,
    labels: list[str],
    prefix: str,
    allowed_values: frozenset[str],
):
    _reject_legacy_operator_basis_labels(labels)
    _reject_deprecated_source_authority(labels)
    matches = [
        label.removeprefix(prefix)
        for label in labels
        if isinstance(label, str) and label.startswith(prefix)
    ]
    if len(matches) != 1:
        raise SourcePolicyError(f"labels must contain exactly one {prefix.rstrip(':')} label.")
    value = matches[0]
    if value not in allowed_values:
        raise SourcePolicyError(f"{prefix.rstrip(':')} label is not a committed value.")
    return value


def _validate_operator_source_metadata(
    labels: list[str],
    *,
    source_kind_label: str,
) -> None:
    _reject_legacy_operator_basis_labels(labels)
    source_kind = source_kind_label.removeprefix(SOURCE_KIND_LABEL_PREFIX)
    confidence = _optional_single_label_value(
        labels=labels,
        prefix=OPERATOR_CONFIDENCE_LABEL_PREFIX,
        allowed_values=OPERATOR_CONFIDENCE_VALUES,
    )
    operator_source_basis = _optional_single_label_value(
        labels=labels,
        prefix=OPERATOR_SOURCE_BASIS_LABEL_PREFIX,
        allowed_values=None,
    )
    if source_kind != "operator_brief":
        if confidence is not None or operator_source_basis is not None:
            raise SourcePolicyError(
                "operator source metadata is allowed only with source_kind:operator_brief."
            )
        return
    if confidence is None or operator_source_basis is None:
        raise SourcePolicyError(
            "source_kind:operator_brief requires operator_confidence and "
            "operator_source_basis labels."
        )
    if not operator_source_basis.strip():
        raise SourcePolicyError("operator_source_basis label must not be blank.")


def _optional_single_label_value(
    *,
    labels: list[str],
    prefix: str,
    allowed_values: frozenset[str] | None,
) -> str | None:
    matches = [
        label.removeprefix(prefix)
        for label in labels
        if isinstance(label, str) and label.startswith(prefix)
    ]
    if len(matches) > 1:
        raise SourcePolicyError(f"labels must contain at most one {prefix.rstrip(':')} label.")
    if not matches:
        return None
    value = matches[0]
    if allowed_values is not None and value not in allowed_values:
        raise SourcePolicyError(f"{prefix.rstrip(':')} label is not a committed value.")
    return value


def _reject_deprecated_source_authority(labels: list[str]) -> None:
    if any(
        isinstance(label, str) and label.startswith(_DEPRECATED_SOURCE_AUTHORITY_PREFIX)
        for label in labels
    ):
        raise SourcePolicyError(
            "source_authority labels are deprecated; use event_type and source_kind."
        )


def _reject_legacy_operator_basis_labels(labels: list[str]) -> None:
    if any(
        isinstance(label, str) and label.startswith(_RETIRED_OPERATOR_BASIS_LABEL_PREFIX)
        for label in labels
    ):
        raise SourcePolicyError(
            "legacy operator basis labels are retired; use operator_source_basis "
            "only with source_kind:operator_brief."
        )


def _normalized_domain(netloc: str) -> str:
    domain = netloc.lower().split("@")[-1].split(":")[0].strip(".")
    if domain.startswith("www."):
        domain = domain.removeprefix("www.")
    return domain


def _domain_matches(domain: str, candidates: frozenset[str]) -> bool:
    return any(domain == candidate or domain.endswith(f".{candidate}") for candidate in candidates)


__all__ = [
    "EVENT_TYPE_LABEL_PREFIX",
    "EVENT_TYPES",
    "OPERATOR_CONFIDENCE_LABEL_PREFIX",
    "OPERATOR_CONFIDENCE_VALUES",
    "OPERATOR_SOURCE_BASIS_LABEL_PREFIX",
    "SOURCE_KIND_LABEL_PREFIX",
    "SOURCE_KINDS",
    "EventType",
    "SourceKind",
    "SourcePolicyError",
    "ensure_source_classification_labels",
    "extract_operator_source_metadata",
    "extract_event_type",
    "extract_source_kind",
    "infer_event_type",
    "infer_source_kind",
    "source_classification_counts",
    "validate_source_classification_labels",
]
