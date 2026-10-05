"""Single source of truth for the canonical thesis bundle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_CANONICAL_THESIS_PAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "thesis.md",
        (
            "Market Setup Dashboard",
            "Why Now",
            "Key Evidence",
            "Invalidation",
        ),
    ),
    (
        "risks.md",
        (
            "Primary Risks",
            "Contradictory Evidence",
            "Failure Conditions",
        ),
    ),
    (
        "watchlist.md",
        (
            "Immediate Watch Items",
            "Questions To Resolve",
            "Triggers To Escalate",
        ),
    ),
    (
        "timeline.md",
        (
            "Recent Developments",
            "Thesis Shifts",
            "Open Threads",
        ),
    ),
)

_WORKBENCH_SUBSET_PAGE_POSITIONS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("thesis.md", (0, 1, 3)),
    ("risks.md", (0, 1, 2)),
    ("watchlist.md", (0, 1, 2)),
    ("timeline.md", (0, 1, 2)),
)

_CHECKER_SUBSET_PAGE_POSITIONS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("thesis.md", (0, 3)),
    ("risks.md", (0, 2)),
    ("watchlist.md", (0, 2)),
    ("timeline.md", (0,)),
)


@dataclass(frozen=True, slots=True)
class CanonicalThesisBundleSection:
    page_path: str
    page_name: str
    section_name: str

    @property
    def section_id(self) -> str:
        return f"{self.page_path}:{self.section_name}"


def canonical_thesis_bundle_sections(
    target_key: str,
) -> tuple[CanonicalThesisBundleSection, ...]:
    normalized_target = _require_non_blank(target_key, "target_key")
    return tuple(
        _bundle_section(
            target_key=normalized_target,
            page_name=page_name,
            section_name=section_name,
        )
        for page_name, section_names in _CANONICAL_THESIS_PAGES
        for section_name in section_names
    )


def workbench_canonical_thesis_bundle_sections(
    target_key: str,
) -> tuple[CanonicalThesisBundleSection, ...]:
    return _bundle_subset_sections(target_key, _WORKBENCH_SUBSET_PAGE_POSITIONS)


def checker_canonical_thesis_bundle_sections(
    target_key: str,
) -> tuple[CanonicalThesisBundleSection, ...]:
    return _bundle_subset_sections(target_key, _CHECKER_SUBSET_PAGE_POSITIONS)


def canonical_thesis_page_sections(page_name: str) -> tuple[str, ...]:
    normalized_page_name = _require_non_blank(page_name, "page_name")
    for candidate_page_name, section_names in _CANONICAL_THESIS_PAGES:
        if candidate_page_name == normalized_page_name:
            return section_names
    return ()


def is_canonical_thesis_bundle_write(
    page_path: str,
    section_name: str,
    operation: str,
) -> bool:
    normalized_operation = _require_non_blank(operation, "operation")
    if normalized_operation not in {"create_page", "rewrite_page", "update_page_section"}:
        return False
    normalized_page_path = _normalize_page_path(page_path)
    parts = normalized_page_path.split("/")
    if len(parts) != 3 or parts[0] != "targets":
        return False
    target_key = parts[1].strip()
    if not target_key:
        return False
    page_name = Path(parts[2]).name
    canonical_sections = canonical_thesis_page_sections(page_name)
    if not canonical_sections:
        return False
    if normalized_operation in {"create_page", "rewrite_page"}:
        return True
    normalized_section_name = _require_non_blank(section_name, "section_name")
    return normalized_section_name in canonical_sections


def _bundle_subset_sections(
    target_key: str,
    page_positions: tuple[tuple[str, tuple[int, ...]], ...],
) -> tuple[CanonicalThesisBundleSection, ...]:
    normalized_target = _require_non_blank(target_key, "target_key")
    sections: list[CanonicalThesisBundleSection] = []
    for page_name, positions in page_positions:
        page_sections = canonical_thesis_page_sections(page_name)
        if not page_sections:
            raise ValueError(f"unknown canonical thesis page: {page_name}")
        for position in positions:
            try:
                section_name = page_sections[position]
            except IndexError as exc:
                raise ValueError(
                    f"invalid canonical thesis page subset index for {page_name}: {position}"
                ) from exc
            sections.append(
                _bundle_section(
                    target_key=normalized_target,
                    page_name=page_name,
                    section_name=section_name,
                )
            )
    return tuple(sections)


def _bundle_section(
    *,
    target_key: str,
    page_name: str,
    section_name: str,
) -> CanonicalThesisBundleSection:
    return CanonicalThesisBundleSection(
        page_path=f"targets/{target_key}/{page_name}",
        page_name=page_name,
        section_name=section_name,
    )


def _normalize_page_path(page_path: object) -> str:
    if not isinstance(page_path, str):
        return ""
    normalized = page_path.strip().replace("\\", "/")
    if normalized != page_path.strip() or not normalized:
        return normalized
    return normalized


def _require_non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-blank string.")
    normalized = value.strip()
    if normalized != value:
        raise ValueError(
            f"{field_name} must not include leading or trailing whitespace."
        )
    return normalized


__all__ = [
    "CanonicalThesisBundleSection",
    "checker_canonical_thesis_bundle_sections",
    "canonical_thesis_bundle_sections",
    "canonical_thesis_page_sections",
    "is_canonical_thesis_bundle_write",
    "workbench_canonical_thesis_bundle_sections",
]
