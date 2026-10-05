"""Research-memory market setup dashboard markdown contract helpers."""

from __future__ import annotations

import re
from pathlib import Path

from event_trader.contracts._validators import validate_target_key
from event_trader.contracts.research_memory import (
    ResearchMemoryContractError,
    initialize_target_research_layout,
)
from event_trader.storage import WorkspaceLayout

MARKET_SETUP_DASHBOARD_ERROR_CODE = "market_setup_dashboard_contract_error"
MARKET_SETUP_DASHBOARD_SECTION = "Market Setup Dashboard"
LEGACY_CURRENT_VIEW_SECTION = "Current View"
_CANONICAL_EXPOSURE_STATE_LABELS = {
    "flat",
    "weak long",
    "strong long",
    "weak short",
    "strong short",
}
_REQUIRED_DASHBOARD_SUBSECTIONS: tuple[str, ...] = (
    "Market Reference",
    "Setup Frame",
    "Watch Triggers",
    "Evidence Gaps",
)
_PROHIBITED_EXPOSURE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("target weight", re.compile(r"(?i)\btarget[_\s-]*weight\b")),
    ("entry", re.compile(r"(?i)\bentry\s*:")),
    ("PnL", re.compile(r"(?i)\b(?:pnl|p&l)\b")),
    ("MFE", re.compile(r"(?i)\bmfe\b")),
    ("MAE", re.compile(r"(?i)\bmae\b")),
    (
        "actual exposure",
        re.compile(r"(?i)\bactual\s+(?:current\s+)?(?:exposure|position)\b"),
    ),
    ("current position", re.compile(r"(?i)\bcurrent\s+position\b")),
    ("portfolio state", re.compile(r"(?i)\bportfolio\s+state\b")),
)
_MARKET_SETUP_DASHBOARD_BODY = """\
### Market Reference
- Last-reviewed analysis reference: unavailable.

### Setup Frame
- As-if-flat setup: none recorded yet.
- Activation / confirmation / invalidation are not established yet.

### Watch Triggers
- None recorded yet.

### Evidence Gaps
- None recorded yet.
"""
_LEGACY_MARKET_SETUP_DASHBOARD_BODY = """\
### Market Reference
- Last-reviewed analysis reference: unavailable.

### Setup Frame
- As-if-flat setup: none recorded yet.
- Activation / confirmation / invalidation are not established yet.

### Watch Triggers
- None recorded yet.

### Evidence Gaps
- None recorded yet.
"""


class MarketSetupDashboardError(ResearchMemoryContractError):
    """Raised when thesis.md Market Setup Dashboard is not exposure-blind."""

    error_code = MARKET_SETUP_DASHBOARD_ERROR_CODE
    recoverable = True

def validate_market_setup_dashboard(content_md: str) -> None:
    """Validate the canonical exposure-blind Market Setup Dashboard body."""

    if not isinstance(content_md, str) or not content_md.strip():
        raise MarketSetupDashboardError(
            "market_setup_dashboard_empty: Market Setup Dashboard must not be blank."
        )
    first_line = next(
        (line.strip() for line in content_md.splitlines() if line.strip()),
        "",
    )
    if _normalize_state_label_line(first_line) in _CANONICAL_EXPOSURE_STATE_LABELS:
        raise MarketSetupDashboardError(
            "market_setup_dashboard_state_label_forbidden: Market Setup Dashboard "
            "must not begin with a canonical exposure state label."
        )

    missing = [
        heading
        for heading in _REQUIRED_DASHBOARD_SUBSECTIONS
        if not _has_level_three_heading(content_md, heading)
    ]
    if missing:
        raise MarketSetupDashboardError(
            "market_setup_dashboard_incomplete: Market Setup Dashboard must include "
            "`### Market Reference`, `### Setup Frame`, `### Watch Triggers`, "
            "and `### Evidence Gaps`. Missing: " + ", ".join(missing) + "."
        )

    _validate_market_setup_reference(content_md)
    _validate_market_setup_frame(content_md)
    _reject_actual_exposure_language(content_md)

def _validate_market_setup_reference(content_md: str) -> None:
    body = _level_three_section_body(content_md, "Market Reference")
    if body is None or not body.strip():
        raise MarketSetupDashboardError(
            "market_setup_dashboard_market_reference_empty: Market Setup Dashboard "
            "`### Market Reference` must state the last-reviewed market reference "
            "or explicitly say market context was unavailable."
        )
    normalized = body.casefold()
    if "unavailable" not in normalized and "analysis reference" not in normalized:
        raise MarketSetupDashboardError(
            "market_setup_dashboard_market_reference_semantics_missing: Market "
            "reference must identify the last-reviewed analysis reference or "
            "explicitly state that no analysis reference was available."
        )


def _validate_market_setup_frame(content_md: str) -> None:
    body = _level_three_section_body(content_md, "Setup Frame")
    if body is None or not body.strip():
        raise MarketSetupDashboardError(
            "market_setup_dashboard_setup_frame_empty: Market Setup Dashboard "
            "`### Setup Frame` must summarize market hypotheses and setup conditions."
        )
    normalized = body.casefold()
    missing = [
        term
        for term in ("activation", "confirmation", "invalidation")
        if term not in normalized
    ]
    if missing:
        raise MarketSetupDashboardError(
            "market_setup_dashboard_setup_frame_incomplete: Setup Frame must include "
            "activation, confirmation, and invalidation semantics. Missing: "
            + ", ".join(missing)
            + "."
        )

def _reject_actual_exposure_language(content_md: str) -> None:
    for label, pattern in _PROHIBITED_EXPOSURE_PATTERNS:
        if pattern.search(content_md) is not None:
            raise MarketSetupDashboardError(
                "market_setup_dashboard_exposure_language_forbidden: Market Setup "
                f"Dashboard must not contain actual exposure field '{label}'."
            )


def _has_level_three_heading(content_md: str, heading: str) -> bool:
    return (
        re.search(
            rf"(?im)^###\s+{re.escape(heading)}\s*$",
            content_md,
        )
        is not None
    )


def _level_three_section_body(content_md: str, heading: str) -> str | None:
    match = re.search(rf"(?im)^###\s+{re.escape(heading)}\s*$", content_md)
    if match is None:
        return None
    body = content_md[match.end() :]
    next_heading = re.search(r"(?m)^#{2,3}\s+", body)
    if next_heading is not None:
        body = body[: next_heading.start()]
    return body

def _normalize_state_label_line(line: str) -> str:
    cleaned = re.sub(r"^[#>*`\s-]+", "", line.strip())
    cleaned = re.sub(r"[*`]+", "", cleaned).strip()
    cleaned = cleaned.rstrip(":").strip()
    cleaned = cleaned.casefold().replace("_", " ")
    return re.sub(r"\s+", " ", cleaned)


def migrate_current_view_to_market_setup_dashboard(
    *,
    layout: WorkspaceLayout,
    target_key: str,
) -> Path:
    """Rewrite a target thesis page to the exposure-blind dashboard surface."""

    if not isinstance(layout, WorkspaceLayout):
        raise MarketSetupDashboardError("layout must be a WorkspaceLayout instance.")
    normalized_target = validate_target_key(
        target_key,
        error_type=MarketSetupDashboardError,
    )
    thesis_existed = (
        layout.targets_root / normalized_target / "thesis.md"
    ).resolve(strict=False).exists()
    target_layout = initialize_target_research_layout(layout, normalized_target)
    thesis_path = target_layout.thesis_file
    try:
        content = thesis_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MarketSetupDashboardError(f"failed to read thesis page: {thesis_path}") from exc

    rewritten = _rewrite_market_setup_dashboard(
        content,
        legacy_current_view=thesis_existed,
    )
    if rewritten != content:
        try:
            thesis_path.write_text(rewritten, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise MarketSetupDashboardError(
                f"failed to write Market Setup Dashboard: {thesis_path}"
            ) from exc
    return thesis_path


def _rewrite_market_setup_dashboard(
    content_md: str,
    *,
    legacy_current_view: bool,
) -> str:
    legacy_match = _level_two_heading_match(content_md, LEGACY_CURRENT_VIEW_SECTION)
    dashboard_match = _level_two_heading_match(content_md, MARKET_SETUP_DASHBOARD_SECTION)

    if legacy_match is not None:
        return _replace_level_two_section(
            content_md,
            legacy_match,
            MARKET_SETUP_DASHBOARD_SECTION,
            (
                _LEGACY_MARKET_SETUP_DASHBOARD_BODY
                if legacy_current_view
                else _MARKET_SETUP_DASHBOARD_BODY
            ),
        )
    if dashboard_match is not None:
        return _replace_level_two_section(
            content_md,
            dashboard_match,
            MARKET_SETUP_DASHBOARD_SECTION,
            _MARKET_SETUP_DASHBOARD_BODY,
        )
    return _insert_market_setup_dashboard(content_md)


def _level_two_heading_match(content_md: str, heading: str) -> re.Match[str] | None:
    return re.search(rf"(?im)^##\s+{re.escape(heading)}\s*$", content_md)


def _replace_level_two_section(
    content_md: str,
    heading_match: re.Match[str],
    heading: str,
    body: str,
) -> str:
    end_match = re.search(r"(?m)^##\s+", content_md[heading_match.end() :])
    section_end = (
        heading_match.end() + end_match.start()
        if end_match is not None
        else len(content_md)
    )
    replacement = f"## {heading}\n{body.rstrip()}\n\n"
    return content_md[: heading_match.start()] + replacement + content_md[section_end:].lstrip()


def _insert_market_setup_dashboard(content_md: str) -> str:
    heading_match = re.search(r"(?m)^#\s+.+$", content_md)
    section = f"\n## {MARKET_SETUP_DASHBOARD_SECTION}\n{_MARKET_SETUP_DASHBOARD_BODY.rstrip()}\n"
    if heading_match is None:
        return f"# Thesis\n{section}\n{content_md.lstrip()}".rstrip() + "\n"
    insert_at = heading_match.end()
    return content_md[:insert_at] + section + content_md[insert_at:].lstrip()


__all__ = [
    "LEGACY_CURRENT_VIEW_SECTION",
    "MARKET_SETUP_DASHBOARD_ERROR_CODE",
    "MARKET_SETUP_DASHBOARD_SECTION",
    "MarketSetupDashboardError",
    "migrate_current_view_to_market_setup_dashboard",
    "validate_market_setup_dashboard",
]
