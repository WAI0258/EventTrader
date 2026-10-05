"""Deterministic reflection-anchor eligibility and prior-coverage lookup."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast

from event_trader.contracts._validators import (
    normalize_content,
    validate_page_key,
    validate_timestamp,
)
from event_trader.contracts.research_memory import (
    PageReadResult,
    PageRef,
    resolve_page_ref,
    resolve_reflection_anchor_page_ref,
)

from .anchors import ReflectionLogEntry
from .contracts import ReviewAnchorIdentity

type ReflectionEligibilityStatus = Literal[
    "deferred",
    "eligible_new",
    "eligible_revisit",
    "skipped",
]
type ReflectionCoverageKind = Literal["target_review", "shared_log"]

_ANY_COVERAGE_COMMENT_RE = re.compile(
    r"<!--\s*reflection-coverage:\s*.+?\s*-->",
    re.IGNORECASE,
)
_TARGET_REVIEW_COVERAGE_RE = re.compile(
    r"<!--\s*reflection-coverage:\s*episode_id=(?P<episode_id>[^;]+?)\s*;\s*"
    r"covered_horizons_hours=(?P<horizons>[^>]+?)\s*-->",
    re.IGNORECASE,
)
_SHARED_LOG_COVERAGE_RE = re.compile(
    r"<!--\s*reflection-coverage:\s*anchor_id=(?P<anchor_id>[^;]+?)\s*;\s*"
    r"covered_horizons_hours=(?P<horizons>[^>]+?)\s*-->",
    re.IGNORECASE,
)


class ReflectionEligibilityError(ValueError):
    """Raised when eligibility or coverage metadata cannot be normalized."""


@dataclass(frozen=True, slots=True)
class ReflectionCoverageRecord:
    """One prior-coverage record recovered from reflection-owned markdown output."""

    source_page_path: str
    coverage_kind: ReflectionCoverageKind
    covered_horizons_hours: tuple[float, ...]
    episode_id: str | None = None
    anchor_id: str | None = None

    def __post_init__(self) -> None:
        page_ref = resolve_page_ref(self.source_page_path)
        if self.coverage_kind == "target_review":
            if page_ref.page_kind != "review":
                raise ReflectionEligibilityError(
                    "target_review coverage metadata must come from target review pages."
                )
            if self.anchor_id is not None:
                raise ReflectionEligibilityError(
                    "target_review coverage metadata must not carry anchor_id."
                )
            if not isinstance(self.episode_id, str) or not self.episode_id.strip():
                raise ReflectionEligibilityError(
                    "target_review coverage metadata requires a non-blank episode_id."
                )
        elif self.coverage_kind == "shared_log":
            if page_ref.page_path != "shared/log.md":
                raise ReflectionEligibilityError(
                    "shared_log coverage metadata must come from 'shared/log.md'."
                )
            if self.episode_id is not None:
                raise ReflectionEligibilityError(
                    "shared_log coverage metadata must not carry episode_id."
                )
            if self.anchor_id is None:
                raise ReflectionEligibilityError(
                    "shared_log coverage metadata requires anchor_id."
                )
        else:
            raise ReflectionEligibilityError(
                "coverage_kind must be either 'target_review' or 'shared_log'."
            )

        object.__setattr__(self, "source_page_path", page_ref.page_path)
        if self.episode_id is not None:
            object.__setattr__(self, "episode_id", self.episode_id.strip())
        if self.anchor_id is not None:
            object.__setattr__(self, "anchor_id", _normalize_anchor_id(self.anchor_id))
        object.__setattr__(
            self,
            "covered_horizons_hours",
            _normalize_horizons_hours(self.covered_horizons_hours),
        )


@dataclass(frozen=True, slots=True)
class ReflectionEligibilityResult:
    """Deterministic eligibility receipt for one parsed reflection log entry."""

    anchor: ReviewAnchorIdentity | None
    anchor_id: str
    scope_key: str
    target_key: str | None
    source_log_path: str
    entry_key: str
    logged_at: datetime | None
    line_start: int
    line_end: int
    status: ReflectionEligibilityStatus
    matured_horizons_hours: tuple[float, ...]
    covered_horizons_hours: tuple[float, ...]
    eligible_horizons_hours: tuple[float, ...]
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        normalized_anchor = self.anchor
        if normalized_anchor is not None and not isinstance(
            normalized_anchor,
            ReviewAnchorIdentity,
        ):
            raise ReflectionEligibilityError(
                "anchor must be a ReviewAnchorIdentity instance when provided."
            )

        object.__setattr__(self, "anchor_id", _normalize_anchor_id(self.anchor_id))

        if normalized_anchor is not None and self.anchor_id != _anchor_id_for_identity(
            normalized_anchor
        ):
            raise ReflectionEligibilityError(
                "anchor_id must match the canonical identity encoded in anchor."
            )
        if normalized_anchor is not None:
            object.__setattr__(self, "scope_key", normalized_anchor.scope_key)
            object.__setattr__(self, "target_key", normalized_anchor.target_key)
            object.__setattr__(self, "source_log_path", normalized_anchor.log_page_path)
            object.__setattr__(self, "entry_key", normalized_anchor.entry_key)
            object.__setattr__(self, "logged_at", normalized_anchor.logged_at)
        else:
            log_page_ref = resolve_reflection_anchor_page_ref(self.source_log_path)
            object.__setattr__(
                self,
                "source_log_path",
                log_page_ref.page_path,
            )
            object.__setattr__(self, "scope_key", _scope_key_for_page_ref(log_page_ref))
            object.__setattr__(self, "target_key", log_page_ref.target_key)
            object.__setattr__(
                self,
                "entry_key",
                validate_page_key(
                    self.entry_key,
                    field_name="entry_key",
                    error_type=ReflectionEligibilityError,
                ),
            )
            if self.logged_at is not None:
                object.__setattr__(
                    self,
                    "logged_at",
                    validate_timestamp(
                        self.logged_at,
                        field_name="logged_at",
                        error_type=ReflectionEligibilityError,
                    ),
                )

        if not isinstance(self.line_start, int) or self.line_start <= 0:
            raise ReflectionEligibilityError("line_start must be a positive integer.")
        if not isinstance(self.line_end, int) or self.line_end < self.line_start:
            raise ReflectionEligibilityError(
                "line_end must be an integer greater than or equal to line_start."
            )

        object.__setattr__(self, "status", _normalize_status(self.status))
        object.__setattr__(
            self,
            "matured_horizons_hours",
            _normalize_horizons_hours(self.matured_horizons_hours, allow_empty=True),
        )
        object.__setattr__(
            self,
            "covered_horizons_hours",
            _normalize_horizons_hours(self.covered_horizons_hours, allow_empty=True),
        )
        object.__setattr__(
            self,
            "eligible_horizons_hours",
            _normalize_horizons_hours(self.eligible_horizons_hours, allow_empty=True),
        )

        if self.skip_reason is not None:
            object.__setattr__(
                self,
                "skip_reason",
                normalize_content(
                    self.skip_reason,
                    field_name="skip_reason",
                    error_type=ReflectionEligibilityError,
                ).strip(),
            )

        if self.status == "skipped" and self.skip_reason is None:
            raise ReflectionEligibilityError(
                "skip_reason is required when status='skipped'."
            )
        if self.status != "skipped" and self.skip_reason is not None:
            raise ReflectionEligibilityError(
                "skip_reason may be reported only when status='skipped'."
            )
        if self.status == "deferred" and self.eligible_horizons_hours:
            raise ReflectionEligibilityError(
                "status='deferred' must not report eligible_horizons_hours."
            )
        if self.status == "eligible_new" and not self.eligible_horizons_hours:
            raise ReflectionEligibilityError(
                "status='eligible_new' must report one or more eligible horizons."
            )
        if self.status == "eligible_revisit" and not self.covered_horizons_hours:
            raise ReflectionEligibilityError(
                "status='eligible_revisit' must report prior covered horizons."
            )
        if self.status in {"eligible_new", "eligible_revisit"} and self.anchor is None:
            raise ReflectionEligibilityError(
                "Eligible results require a canonical anchor identity."
            )
        if self.status == "deferred" and self.anchor is None:
            raise ReflectionEligibilityError(
                "Deferred results require a canonical anchor identity."
            )
        if (
            self.status in {"eligible_new", "eligible_revisit"}
            and not self.matured_horizons_hours
        ):
            raise ReflectionEligibilityError(
                "Eligible results must report one or more matured horizons."
            )
        if self.status == "eligible_new" and self.covered_horizons_hours:
            raise ReflectionEligibilityError(
                "status='eligible_new' must not report prior covered horizons."
            )

        matured = set(self.matured_horizons_hours)
        covered = set(self.covered_horizons_hours)
        eligible = set(self.eligible_horizons_hours)
        if not covered.issubset(matured):
            raise ReflectionEligibilityError(
                "covered_horizons_hours must be a subset of matured_horizons_hours."
            )
        if not eligible.issubset(matured):
            raise ReflectionEligibilityError(
                "eligible_horizons_hours must be a subset of matured_horizons_hours."
            )
        if self.status == "eligible_new" and eligible != matured:
            raise ReflectionEligibilityError(
                "status='eligible_new' must mark every matured horizon as eligible."
            )
        if self.status == "eligible_revisit" and eligible != matured.difference(
            covered
        ):
            raise ReflectionEligibilityError(
                "status='eligible_revisit' must expose only newly matured uncovered horizons."
            )
        if self.status == "skipped" and eligible:
            raise ReflectionEligibilityError(
                "status='skipped' must not report eligible_horizons_hours."
            )


def parse_reflection_coverage(
    *, page: PageReadResult
) -> tuple[ReflectionCoverageRecord, ...]:
    """Recover reflection coverage metadata from one review page or shared log."""
    if not isinstance(page, PageReadResult):
        raise ReflectionEligibilityError("page must be a PageReadResult instance.")

    page_ref = resolve_page_ref(page.page_path)
    if page_ref.page_kind == "review":
        return _parse_target_review_coverage(page=page, page_ref=page_ref)
    if page_ref.page_path == "shared/log.md":
        return _parse_shared_log_coverage(page=page, page_ref=page_ref)
    raise ReflectionEligibilityError(
        "Coverage metadata must be read only from target review pages or 'shared/log.md'."
    )


def evaluate_reflection_eligibility(
    *,
    log_entries: tuple[ReflectionLogEntry, ...],
    checked_at: datetime,
    horizons_hours: tuple[float, ...],
    coverage_pages: tuple[PageReadResult, ...] = (),
) -> tuple[ReflectionEligibilityResult, ...]:
    """Evaluate deterministic maturity, skip, and revisit state for log entries."""
    if not isinstance(log_entries, tuple):
        raise ReflectionEligibilityError(
            "log_entries must be a tuple of ReflectionLogEntry values."
        )

    normalized_checked_at = validate_timestamp(
        checked_at,
        field_name="checked_at",
        error_type=ReflectionEligibilityError,
    )
    normalized_horizons = _normalize_horizons_hours(horizons_hours)
    _, coverage_by_anchor = _collect_coverage_maps(coverage_pages)
    ambiguous_anchor_ids = _find_ambiguous_anchor_ids(log_entries)

    results: list[ReflectionEligibilityResult] = []
    for entry in log_entries:
        if not isinstance(entry, ReflectionLogEntry):
            raise ReflectionEligibilityError(
                "log_entries must contain only ReflectionLogEntry instances."
            )

        results.append(
            _evaluate_one_entry(
                entry=entry,
                checked_at=normalized_checked_at,
                horizons_hours=normalized_horizons,
                covered_horizons_hours=tuple(
                    horizon
                    for horizon in normalized_horizons
                    if horizon in coverage_by_anchor.get(entry.anchor_id, set())
                ),
                ambiguous_anchor_ids=ambiguous_anchor_ids,
            )
        )

    return tuple(results)


def _evaluate_one_entry(
    *,
    entry: ReflectionLogEntry,
    checked_at: datetime,
    horizons_hours: tuple[float, ...],
    covered_horizons_hours: tuple[float, ...],
    ambiguous_anchor_ids: set[str],
) -> ReflectionEligibilityResult:
    if entry.normalization_error is not None or entry.logged_at is None:
        return ReflectionEligibilityResult(
            anchor=None,
            anchor_id=entry.anchor_id,
            scope_key=entry.scope_key,
            target_key=entry.target_key,
            source_log_path=entry.log_page_path,
            entry_key=entry.entry_key,
            logged_at=entry.logged_at,
            line_start=entry.line_start,
            line_end=entry.line_end,
            status="skipped",
            matured_horizons_hours=(),
            covered_horizons_hours=(),
            eligible_horizons_hours=(),
            skip_reason=entry.normalization_error,
        )

    anchor = entry.to_review_anchor()
    matured_horizons = tuple(
        horizon
        for horizon in horizons_hours
        if checked_at >= anchor.logged_at + timedelta(hours=horizon)
    )
    if not matured_horizons:
        return ReflectionEligibilityResult(
            anchor=anchor,
            anchor_id=entry.anchor_id,
            scope_key=anchor.scope_key,
            target_key=anchor.target_key,
            source_log_path=anchor.log_page_path,
            entry_key=anchor.entry_key,
            logged_at=anchor.logged_at,
            line_start=entry.line_start,
            line_end=entry.line_end,
            status="deferred",
            matured_horizons_hours=(),
            covered_horizons_hours=(),
            eligible_horizons_hours=(),
        )

    if entry.anchor_id in ambiguous_anchor_ids:
        return ReflectionEligibilityResult(
            anchor=anchor,
            anchor_id=entry.anchor_id,
            scope_key=anchor.scope_key,
            target_key=anchor.target_key,
            source_log_path=anchor.log_page_path,
            entry_key=anchor.entry_key,
            logged_at=anchor.logged_at,
            line_start=entry.line_start,
            line_end=entry.line_end,
            status="skipped",
            matured_horizons_hours=matured_horizons,
            covered_horizons_hours=(),
            eligible_horizons_hours=(),
            skip_reason=_format_skip_reason(
                entry=entry,
                detail=(
                    f"anchor_id {entry.anchor_id!r} is ambiguous because multiple "
                    "log entries normalized to the same identity before any "
                    "review context was loaded."
                ),
            ),
        )

    if not entry.cited_event_ids or not entry.cited_source_refs:
        return ReflectionEligibilityResult(
            anchor=anchor,
            anchor_id=entry.anchor_id,
            scope_key=anchor.scope_key,
            target_key=anchor.target_key,
            source_log_path=anchor.log_page_path,
            entry_key=anchor.entry_key,
            logged_at=anchor.logged_at,
            line_start=entry.line_start,
            line_end=entry.line_end,
            status="skipped",
            matured_horizons_hours=matured_horizons,
            covered_horizons_hours=(),
            eligible_horizons_hours=(),
            skip_reason=_format_skip_reason(
                entry=entry,
                detail=(
                    "entry must cite at least one evidence pair using the canonical "
                    "`event_id` | `source_ref` format before reflection can review it."
                ),
            ),
        )

    if set(matured_horizons).issubset(covered_horizons_hours):
        return ReflectionEligibilityResult(
            anchor=anchor,
            anchor_id=entry.anchor_id,
            scope_key=anchor.scope_key,
            target_key=anchor.target_key,
            source_log_path=anchor.log_page_path,
            entry_key=anchor.entry_key,
            logged_at=anchor.logged_at,
            line_start=entry.line_start,
            line_end=entry.line_end,
            status="skipped",
            matured_horizons_hours=matured_horizons,
            covered_horizons_hours=covered_horizons_hours,
            eligible_horizons_hours=(),
            skip_reason=_format_skip_reason(
                entry=entry,
                detail="all matured horizons are already covered by prior reflection outputs.",
            ),
        )

    if covered_horizons_hours:
        eligible_horizons = tuple(
            horizon
            for horizon in matured_horizons
            if horizon not in covered_horizons_hours
        )
        return ReflectionEligibilityResult(
            anchor=anchor,
            anchor_id=entry.anchor_id,
            scope_key=anchor.scope_key,
            target_key=anchor.target_key,
            source_log_path=anchor.log_page_path,
            entry_key=anchor.entry_key,
            logged_at=anchor.logged_at,
            line_start=entry.line_start,
            line_end=entry.line_end,
            status="eligible_revisit",
            matured_horizons_hours=matured_horizons,
            covered_horizons_hours=covered_horizons_hours,
            eligible_horizons_hours=eligible_horizons,
        )

    return ReflectionEligibilityResult(
        anchor=anchor,
        anchor_id=entry.anchor_id,
        scope_key=anchor.scope_key,
        target_key=anchor.target_key,
        source_log_path=anchor.log_page_path,
        entry_key=anchor.entry_key,
        logged_at=anchor.logged_at,
        line_start=entry.line_start,
        line_end=entry.line_end,
        status="eligible_new",
        matured_horizons_hours=matured_horizons,
        covered_horizons_hours=(),
        eligible_horizons_hours=matured_horizons,
    )


def _coverage_by_anchor_id(
    coverage_pages: tuple[PageReadResult, ...],
) -> dict[str, set[float]]:
    _, coverage_by_anchor = _collect_coverage_maps(coverage_pages)
    return coverage_by_anchor


def _coverage_by_episode_id(
    coverage_pages: tuple[PageReadResult, ...],
) -> dict[str, set[float]]:
    coverage_by_episode, _ = _collect_coverage_maps(coverage_pages)
    return coverage_by_episode


def _collect_coverage_maps(
    coverage_pages: tuple[PageReadResult, ...],
) -> tuple[dict[str, set[float]], dict[str, set[float]]]:
    if not isinstance(coverage_pages, tuple):
        raise ReflectionEligibilityError(
            "coverage_pages must be a tuple of PageReadResult values."
        )

    coverage_by_episode: dict[str, set[float]] = {}
    coverage_by_anchor: dict[str, set[float]] = {}
    for page in coverage_pages:
        if not isinstance(page, PageReadResult):
            raise ReflectionEligibilityError(
                "coverage_pages must contain only PageReadResult instances."
            )
        for record in parse_reflection_coverage(page=page):
            if record.coverage_kind == "target_review":
                if record.episode_id is None:
                    raise ReflectionEligibilityError(
                        "target_review coverage records must carry episode_id."
                    )
                coverage_by_episode.setdefault(record.episode_id, set()).update(
                    record.covered_horizons_hours
                )
                continue
            if record.anchor_id is None:
                raise ReflectionEligibilityError(
                    "shared_log coverage records must carry anchor_id."
                )
            coverage_by_anchor.setdefault(record.anchor_id, set()).update(
                record.covered_horizons_hours
            )
    return coverage_by_episode, coverage_by_anchor


def _parse_target_review_coverage(
    *,
    page: PageReadResult,
    page_ref: PageRef,
) -> tuple[ReflectionCoverageRecord, ...]:
    _reject_mismatched_coverage_comments(
        content_md=page.content_md,
        allowed_pattern=_TARGET_REVIEW_COVERAGE_RE,
        expected_identity_name="episode_id",
        page_path=page_ref.page_path,
    )
    return tuple(
        ReflectionCoverageRecord(
            source_page_path=page_ref.page_path,
            coverage_kind="target_review",
            episode_id=match.group("episode_id").strip(),
            covered_horizons_hours=_parse_horizons_csv(match.group("horizons")),
        )
        for match in _TARGET_REVIEW_COVERAGE_RE.finditer(page.content_md)
    )


def _parse_shared_log_coverage(
    *,
    page: PageReadResult,
    page_ref: PageRef,
) -> tuple[ReflectionCoverageRecord, ...]:
    _reject_mismatched_coverage_comments(
        content_md=page.content_md,
        allowed_pattern=_SHARED_LOG_COVERAGE_RE,
        expected_identity_name="anchor_id",
        page_path=page_ref.page_path,
    )
    return tuple(
        ReflectionCoverageRecord(
            source_page_path=page_ref.page_path,
            coverage_kind="shared_log",
            anchor_id=match.group("anchor_id").strip(),
            covered_horizons_hours=_parse_horizons_csv(match.group("horizons")),
        )
        for match in _SHARED_LOG_COVERAGE_RE.finditer(page.content_md)
    )


def _reject_mismatched_coverage_comments(
    *,
    content_md: str,
    allowed_pattern: re.Pattern[str],
    expected_identity_name: str,
    page_path: str,
) -> None:
    all_matches = tuple(_ANY_COVERAGE_COMMENT_RE.finditer(content_md))
    if not all_matches:
        return
    allowed_matches = tuple(allowed_pattern.finditer(content_md))
    if len(all_matches) == len(allowed_matches):
        return
    raise ReflectionEligibilityError(
        "Coverage metadata on "
        f"{page_path!r} must use {expected_identity_name} comments only."
    )



def _find_ambiguous_anchor_ids(
    log_entries: tuple[ReflectionLogEntry, ...],
) -> set[str]:
    counts: dict[str, int] = {}
    for entry in log_entries:
        if entry.normalization_error is not None or entry.logged_at is None:
            continue
        counts[entry.anchor_id] = counts.get(entry.anchor_id, 0) + 1
    return {
        anchor_id for anchor_id, count in counts.items() if count > 1
    }



def _normalize_status(value: str) -> ReflectionEligibilityStatus:
    if value not in {"deferred", "eligible_new", "eligible_revisit", "skipped"}:
        raise ReflectionEligibilityError(
            "status must be one of: deferred, eligible_new, eligible_revisit, skipped."
        )
    return cast(ReflectionEligibilityStatus, value)


def _normalize_anchor_id(value: str) -> str:
    if not isinstance(value, str):
        raise ReflectionEligibilityError("anchor_id must be a string.")

    anchor_id = value.strip()
    if not anchor_id:
        raise ReflectionEligibilityError("anchor_id must not be blank.")
    if "#" not in anchor_id:
        raise ReflectionEligibilityError(
            "anchor_id must use the canonical '<log_page_path>#<entry_key>' format."
        )
    log_page_path, separator, entry_key = anchor_id.partition("#")
    if not separator or not log_page_path or not entry_key:
        raise ReflectionEligibilityError(
            "anchor_id must use the canonical '<log_page_path>#<entry_key>' format."
        )

    page_ref = resolve_reflection_anchor_page_ref(log_page_path)
    normalized_entry_key = validate_page_key(
        entry_key,
        field_name="anchor_id entry_key",
        error_type=ReflectionEligibilityError,
    )
    return f"{page_ref.page_path}#{normalized_entry_key}"


def _anchor_id_for_identity(anchor: ReviewAnchorIdentity) -> str:
    return f"{anchor.log_page_path}#{anchor.entry_key}"


def _scope_key_for_page_ref(page_ref: PageRef) -> str:
    if page_ref.target_key is None:
        return "shared"
    return f"target:{page_ref.target_key}"


def _normalize_horizons_hours(
    value: tuple[float, ...],
    *,
    allow_empty: bool = False,
) -> tuple[float, ...]:
    if not isinstance(value, tuple):
        raise ReflectionEligibilityError(
            "horizons_hours must be a tuple of positive horizon hours."
        )
    if not value:
        if allow_empty:
            return ()
        raise ReflectionEligibilityError(
            "horizons_hours must be a non-empty tuple of positive horizon hours."
        )

    normalized: list[float] = []
    seen: set[float] = set()
    for index, item in enumerate(value):
        horizon = _validate_positive_hours(
            item,
            field_name=f"horizons_hours[{index}]",
        )
        if horizon in seen:
            raise ReflectionEligibilityError(
                "horizons_hours must not contain duplicate horizon values."
            )
        seen.add(horizon)
        normalized.append(horizon)
    return tuple(normalized)


def _parse_horizons_csv(value: str) -> tuple[float, ...]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        raise ReflectionEligibilityError(
            "covered_horizons_hours metadata must include one or more horizon values."
        )
    return tuple(float(part) for part in parts)


def _validate_positive_hours(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReflectionEligibilityError(
            f"{field_name} must be a positive number of hours."
        )
    normalized = float(value)
    if normalized <= 0:
        raise ReflectionEligibilityError(
            f"{field_name} must be greater than zero hours."
        )
    return normalized


def _format_skip_reason(*, entry: ReflectionLogEntry, detail: str) -> str:
    return (
        "Skipped reflection anchor at "
        f"{entry.log_page_path} lines {entry.line_start}-{entry.line_end}: "
        f"{detail}"
    )


__all__ = [
    "ReflectionCoverageRecord",
    "ReflectionEligibilityError",
    "ReflectionEligibilityResult",
    "ReflectionEligibilityStatus",
    "evaluate_reflection_eligibility",
    "parse_reflection_coverage",
]
