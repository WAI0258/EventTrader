"""Canonical reflection-anchor parsing for target and shared log surfaces."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from event_trader.contracts import EvidenceLedgerRecord
from event_trader.contracts._validators import (
    normalize_content,
    validate_event_id,
    validate_page_key,
    validate_scope_key,
    validate_source_ref,
    validate_target_key,
    validate_timestamp,
)
from event_trader.contracts.ports import ResearchMemoryPort
from event_trader.contracts.research_memory import (
    PageReadResult,
    ResearchMemoryContractError,
    resolve_reflection_anchor_page_ref,
)
from event_trader.research_memory import FileBackedIndexLogWriter
from event_trader.research_memory.discipline import (
    extract_markdown_headings,
    validate_log_append_entry,
    validate_log_page,
)
from event_trader.storage import WorkspaceLayout

from .boundaries import enforce_reflection_write_surface
from .contracts import (
    ReflectionContractError,
    ReflectionEvaluationResult,
    ReviewAnchorIdentity,
)

_CITATION_RE: Final[re.Pattern[str]] = re.compile(
    r"`(?P<event_id>[0-9a-f]{24})`\s*\|\s*`(?P<source_ref>[^`]+)`"
)
_DATE_ONLY_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NON_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


type SharedLogAppendDecision = Literal["append_shared_anchor", "skip_shared_anchor"]
type InputAnchorHandling = Literal["shared_input", "target_input"]


class ReflectionAnchorParseError(ValueError):
    """Raised when a canonical reflection anchor cannot be normalized."""


class SharedAnchorWriteError(ValueError):
    """Raised when a canonical shared follow-on anchor cannot be written."""


@dataclass(frozen=True, slots=True)
class ReflectionLogEntry:
    """One parsed target/shared log entry that may normalize into a review anchor."""

    scope_key: str
    target_key: str | None
    log_page_path: str
    entry_key: str
    heading_text: str
    entry_md: str
    body_md: str
    line_start: int
    line_end: int
    cited_event_ids: tuple[str, ...] = ()
    cited_source_refs: tuple[str, ...] = ()
    logged_at: datetime | None = None
    normalization_error: str | None = None

    def __post_init__(self) -> None:
        page_ref = resolve_reflection_anchor_page_ref(self.log_page_path)
        expected_scope_key = (
            "shared" if page_ref.target_key is None else f"target:{page_ref.target_key}"
        )
        normalized_scope_key = validate_scope_key(
            self.scope_key,
            error_type=ReflectionAnchorParseError,
        )
        if normalized_scope_key != expected_scope_key:
            raise ReflectionAnchorParseError(
                "scope_key must match the canonical reflection log surface resolved from "
                f"log_page_path; expected {expected_scope_key!r}."
            )
        if self.target_key != page_ref.target_key:
            raise ReflectionAnchorParseError(
                "target_key must match the target segment resolved from log_page_path."
            )

        object.__setattr__(self, "scope_key", normalized_scope_key)
        object.__setattr__(
            self,
            "entry_key",
            validate_page_key(
                self.entry_key,
                field_name="entry_key",
                error_type=ReflectionAnchorParseError,
            ),
        )
        object.__setattr__(
            self,
            "heading_text",
            normalize_content(
                self.heading_text,
                field_name="heading_text",
                error_type=ReflectionAnchorParseError,
            ).strip(),
        )
        object.__setattr__(
            self,
            "entry_md",
            normalize_content(
                self.entry_md,
                field_name="entry_md",
                error_type=ReflectionAnchorParseError,
            ),
        )
        object.__setattr__(
            self,
            "body_md",
            normalize_content(
                self.body_md,
                field_name="body_md",
                error_type=ReflectionAnchorParseError,
            ),
        )
        if not isinstance(self.line_start, int) or self.line_start <= 0:
            raise ReflectionAnchorParseError("line_start must be a positive integer.")
        if not isinstance(self.line_end, int) or self.line_end < self.line_start:
            raise ReflectionAnchorParseError(
                "line_end must be an integer greater than or equal to line_start."
            )
        object.__setattr__(
            self,
            "cited_event_ids",
            tuple(
                validate_event_id(event_id, error_type=ReflectionAnchorParseError)
                for event_id in self.cited_event_ids
            ),
        )
        object.__setattr__(
            self,
            "cited_source_refs",
            tuple(
                validate_source_ref(source_ref, error_type=ReflectionAnchorParseError)
                for source_ref in self.cited_source_refs
            ),
        )
        if self.logged_at is not None:
            object.__setattr__(
                self,
                "logged_at",
                validate_timestamp(
                    self.logged_at,
                    field_name="logged_at",
                    error_type=ReflectionAnchorParseError,
                ),
            )
        if self.normalization_error is not None:
            object.__setattr__(
                self,
                "normalization_error",
                normalize_content(
                    self.normalization_error,
                    field_name="normalization_error",
                    error_type=ReflectionAnchorParseError,
                ).strip(),
            )

    @property
    def is_canonical(self) -> bool:
        """Return whether this parsed log entry normalized into a review anchor."""
        return self.logged_at is not None and self.normalization_error is None

    @property
    def anchor_id(self) -> str:
        """Return the deterministic canonical identifier for this parsed log entry."""
        return f"{self.log_page_path}#{self.entry_key}"

    def to_review_anchor(self) -> ReviewAnchorIdentity:
        """Return the canonical review-anchor identity for this parsed log entry."""
        if self.logged_at is None or self.normalization_error is not None:
            raise ReflectionAnchorParseError(
                self.normalization_error
                or _format_entry_error(
                    log_page_path=self.log_page_path,
                    line_start=self.line_start,
                    line_end=self.line_end,
                    detail="entry could not be normalized into a canonical review anchor.",
                )
            )
        try:
            return ReviewAnchorIdentity(
                scope_key=self.scope_key,
                target_key=self.target_key,
                log_page_path=self.log_page_path,
                entry_key=self.entry_key,
                logged_at=self.logged_at,
            )
        except ReflectionContractError as exc:
            raise ReflectionAnchorParseError(
                _format_entry_error(
                    log_page_path=self.log_page_path,
                    line_start=self.line_start,
                    line_end=self.line_end,
                    detail=str(exc),
                )
            ) from exc


@dataclass(frozen=True, slots=True)
class SharedAnchorWriteReceipt:
    """Observable receipt for one shared follow-on anchor append decision."""

    source_anchor_id: str
    artifact_path: Path
    input_anchor_handling: InputAnchorHandling
    cross_target_target_keys: tuple[str, ...]
    covered_source_horizons_hours: tuple[float, ...]
    wrote_shared_anchor: bool
    shared_log_append_decision: SharedLogAppendDecision
    appended_anchor_id: str | None = None
    decision_rationale: str = ""
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_anchor_id",
            _normalize_anchor_id(
                self.source_anchor_id,
                error_type=SharedAnchorWriteError,
            ),
        )
        if not isinstance(self.artifact_path, Path):
            raise SharedAnchorWriteError("artifact_path must be a pathlib.Path.")
        if self.input_anchor_handling not in {"shared_input", "target_input"}:
            raise SharedAnchorWriteError(
                "input_anchor_handling must be either 'shared_input' or 'target_input'."
            )
        object.__setattr__(
            self,
            "cross_target_target_keys",
            _normalize_target_keys(self.cross_target_target_keys),
        )
        object.__setattr__(
            self,
            "covered_source_horizons_hours",
            _normalize_horizons_hours(
                self.covered_source_horizons_hours,
                field_name="covered_source_horizons_hours",
                error_type=SharedAnchorWriteError,
            ),
        )
        if not isinstance(self.wrote_shared_anchor, bool):
            raise SharedAnchorWriteError("wrote_shared_anchor must be a boolean.")
        if self.shared_log_append_decision not in {
            "append_shared_anchor",
            "skip_shared_anchor",
        }:
            raise SharedAnchorWriteError(
                "shared_log_append_decision must be either "
                "'append_shared_anchor' or 'skip_shared_anchor'."
            )
        object.__setattr__(
            self,
            "decision_rationale",
            normalize_content(
                self.decision_rationale,
                field_name="decision_rationale",
                error_type=SharedAnchorWriteError,
            ),
        )
        if self.appended_anchor_id is not None:
            normalized_appended_anchor_id = _normalize_anchor_id(
                self.appended_anchor_id,
                error_type=SharedAnchorWriteError,
            )
            if not normalized_appended_anchor_id.startswith("shared/log.md#"):
                raise SharedAnchorWriteError(
                    "appended_anchor_id must resolve to the canonical shared/log.md surface."
                )
            object.__setattr__(
                self, "appended_anchor_id", normalized_appended_anchor_id
            )
        if self.skip_reason is not None:
            object.__setattr__(
                self,
                "skip_reason",
                normalize_content(
                    self.skip_reason,
                    field_name="skip_reason",
                    error_type=SharedAnchorWriteError,
                ),
            )

        if (
            self.wrote_shared_anchor
            and self.shared_log_append_decision != "append_shared_anchor"
        ):
            raise SharedAnchorWriteError(
                "wrote_shared_anchor=True requires "
                "shared_log_append_decision='append_shared_anchor'."
            )
        if (
            not self.wrote_shared_anchor
            and self.shared_log_append_decision != "skip_shared_anchor"
        ):
            raise SharedAnchorWriteError(
                "wrote_shared_anchor=False requires "
                "shared_log_append_decision='skip_shared_anchor'."
            )
        if self.wrote_shared_anchor and self.appended_anchor_id is None:
            raise SharedAnchorWriteError(
                "appended_anchor_id is required when wrote_shared_anchor=True."
            )
        if self.wrote_shared_anchor and self.skip_reason is not None:
            raise SharedAnchorWriteError(
                "skip_reason must be None when wrote_shared_anchor=True."
            )
        if not self.wrote_shared_anchor and self.skip_reason is None:
            raise SharedAnchorWriteError(
                "skip_reason is required when wrote_shared_anchor=False."
            )


def parse_reflection_log_entries(
    *,
    log_page_path: str,
    content_md: str,
) -> tuple[ReflectionLogEntry, ...]:
    """Parse one canonical target/shared log surface into reflection-log entries."""
    page_ref = resolve_reflection_anchor_page_ref(log_page_path)
    scope_kind: Literal["shared", "target"] = (
        "shared" if page_ref.target_key is None else "target"
    )
    scope_key = (
        "shared" if page_ref.target_key is None else f"target:{page_ref.target_key}"
    )
    normalized_content = _validate_surface(
        log_page_path=page_ref.page_path,
        content_md=content_md,
        scope_kind=scope_kind,
    )
    lines = normalized_content.splitlines()
    second_level_headings = tuple(
        heading
        for heading in extract_markdown_headings(
            normalized_content, field_name="content_md"
        )
        if heading.level == 2
    )
    if not second_level_headings:
        if _has_non_entry_content_after_title(lines):
            raise ReflectionAnchorParseError(
                "Failed to parse reflection log surface "
                f"{page_ref.page_path} lines 1-{len(lines)}: "
                "log surfaces scanned for reflection anchors must keep all non-title "
                "content under canonical '##' entry headings."
            )
        return ()

    entries: list[ReflectionLogEntry] = []
    for index, heading in enumerate(second_level_headings):
        line_start = heading.line_number
        line_end = (
            second_level_headings[index + 1].line_number - 1
            if index + 1 < len(second_level_headings)
            else len(lines)
        )
        while line_end > line_start and not lines[line_end - 1].strip():
            line_end -= 1
        entry_lines = lines[line_start - 1 : line_end]
        entry_md = "\n".join(entry_lines).rstrip()
        body_md = "\n".join(entry_lines[1:]).rstrip()
        logged_at, normalization_error = _normalize_logged_at(
            heading.text,
            log_page_path=page_ref.page_path,
            line_start=line_start,
            line_end=line_end,
        )
        cited_event_ids, cited_source_refs = _extract_citations(entry_md)
        entries.append(
            ReflectionLogEntry(
                scope_key=scope_key,
                target_key=page_ref.target_key,
                log_page_path=page_ref.page_path,
                entry_key=_derive_entry_key(
                    page_ref.page_path,
                    heading.text,
                    entry_md,
                ),
                heading_text=heading.text,
                entry_md=f"{entry_md}\n",
                body_md=f"{body_md}\n",
                line_start=line_start,
                line_end=line_end,
                cited_event_ids=cited_event_ids,
                cited_source_refs=cited_source_refs,
                logged_at=logged_at,
                normalization_error=normalization_error,
            )
        )

    return tuple(entries)


def write_shared_follow_on_anchor(
    *,
    source_entry: ReflectionLogEntry,
    original_evidence: tuple[EvidenceLedgerRecord, ...],
    evaluation: ReflectionEvaluationResult,
    layout: WorkspaceLayout,
    research_memory: ResearchMemoryPort | None = None,
) -> SharedAnchorWriteReceipt:
    """Append one justified shared follow-on anchor through the canonical log seam."""

    if not isinstance(source_entry, ReflectionLogEntry):
        raise SharedAnchorWriteError(
            "source_entry must be a ReflectionLogEntry instance."
        )
    if not isinstance(original_evidence, tuple) or not original_evidence:
        raise SharedAnchorWriteError(
            "original_evidence must be a non-empty tuple of EvidenceLedgerRecord instances."
        )
    normalized_original_evidence: list[EvidenceLedgerRecord] = []
    for record in original_evidence:
        if not isinstance(record, EvidenceLedgerRecord):
            raise SharedAnchorWriteError(
                "original_evidence must contain only EvidenceLedgerRecord instances."
            )
        normalized_original_evidence.append(record)
    if not isinstance(evaluation, ReflectionEvaluationResult):
        raise SharedAnchorWriteError(
            "evaluation must be a ReflectionEvaluationResult instance."
        )
    if not isinstance(layout, WorkspaceLayout):
        raise SharedAnchorWriteError("layout must be a WorkspaceLayout instance.")
    if not source_entry.is_canonical:
        raise SharedAnchorWriteError(
            "source_entry must be a canonical reflection log entry before shared follow-on writing."
        )

    source_anchor_id = source_entry.anchor_id
    expected_anchor_id = _anchor_id(
        evaluation.anchor.log_page_path,
        evaluation.anchor.entry_key,
    )
    if source_anchor_id != expected_anchor_id:
        raise SharedAnchorWriteError(
            "source_entry must match evaluation.anchor for shared follow-on writing."
        )

    input_anchor_handling: InputAnchorHandling = (
        "shared_input"
        if source_entry.log_page_path == "shared/log.md"
        else "target_input"
    )
    cross_target_target_keys = _cross_target_target_keys(
        tuple(normalized_original_evidence)
    )
    covered_source_horizons_hours = _covered_source_horizons_hours(evaluation)
    artifact_path = enforce_reflection_write_surface(
        page_path="shared/log.md",
        layout=layout,
    ).artifact_path

    if input_anchor_handling == "target_input":
        return SharedAnchorWriteReceipt(
            source_anchor_id=source_anchor_id,
            artifact_path=artifact_path,
            input_anchor_handling=input_anchor_handling,
            cross_target_target_keys=cross_target_target_keys,
            covered_source_horizons_hours=covered_source_horizons_hours,
            wrote_shared_anchor=False,
            shared_log_append_decision="skip_shared_anchor",
            decision_rationale=(
                "Skipped shared/log.md follow-on anchoring because the reflection input "
                "came from a target log and target review pages remain the canonical "
                "future-review surface for target-scoped anchors."
            ),
            skip_reason=(
                "Boundary guard rejected shared/log.md append for "
                f"source_anchor_id={source_anchor_id!r}: "
                "blocked_rule='shared_log_requires_shared_input'. "
                "target-scoped reflection inputs must preserve follow-on review "
                "state through target review pages, not through shared/log.md."
            ),
        )

    if evaluation.review_decision != "write_review":
        return SharedAnchorWriteReceipt(
            source_anchor_id=source_anchor_id,
            artifact_path=artifact_path,
            input_anchor_handling=input_anchor_handling,
            cross_target_target_keys=cross_target_target_keys,
            covered_source_horizons_hours=covered_source_horizons_hours,
            wrote_shared_anchor=False,
            shared_log_append_decision="skip_shared_anchor",
            decision_rationale=(
                "Skipped shared/log.md follow-on anchoring because the reflection "
                "evaluation did not justify a concrete completed-review signal."
            ),
            skip_reason=evaluation.decision_rationale,
        )

    if len(cross_target_target_keys) < 2:
        return SharedAnchorWriteReceipt(
            source_anchor_id=source_anchor_id,
            artifact_path=artifact_path,
            input_anchor_handling=input_anchor_handling,
            cross_target_target_keys=cross_target_target_keys,
            covered_source_horizons_hours=covered_source_horizons_hours,
            wrote_shared_anchor=False,
            shared_log_append_decision="skip_shared_anchor",
            decision_rationale=(
                "Skipped shared/log.md follow-on anchoring because the shared input "
                "did not cite evidence from more than one target partition."
            ),
            skip_reason=(
                "Boundary guard rejected shared/log.md append for "
                f"source_anchor_id={source_anchor_id!r}: "
                "blocked_rule='shared_log_requires_cross_target_evidence'. "
                "shared follow-on anchors require a cross-target evidence set "
                "spanning at least two target keys."
            ),
        )

    if not artifact_path.exists() or not artifact_path.is_file():
        raise _shared_anchor_write_error(
            source_anchor_id=source_anchor_id,
            artifact_path=artifact_path,
            detail="Expected the canonical shared log markdown file to exist.",
        )

    existing_content = artifact_path.read_text(encoding="utf-8")
    existing_coverage = _covered_horizons_for_source_anchor(
        existing_content_md=existing_content,
        source_anchor_id=source_anchor_id,
    )
    if set(covered_source_horizons_hours).issubset(existing_coverage):
        return SharedAnchorWriteReceipt(
            source_anchor_id=source_anchor_id,
            artifact_path=artifact_path,
            input_anchor_handling=input_anchor_handling,
            cross_target_target_keys=cross_target_target_keys,
            covered_source_horizons_hours=covered_source_horizons_hours,
            wrote_shared_anchor=False,
            shared_log_append_decision="skip_shared_anchor",
            decision_rationale=(
                "Skipped shared/log.md follow-on anchoring because the source anchor is "
                "already preserved there for the assessed horizons."
            ),
            skip_reason=(
                "shared/log.md already records reflection coverage for "
                f"source_anchor_id={source_anchor_id!r} across horizons "
                f"{_format_horizons_tuple(covered_source_horizons_hours)}."
            ),
        )

    writer = research_memory or FileBackedIndexLogWriter(layout)
    _validate_research_memory_port(writer)
    entry_md = _render_shared_follow_on_entry(
        source_anchor_id=source_anchor_id,
        source_entry=source_entry,
        original_evidence=tuple(normalized_original_evidence),
        evaluation=evaluation,
        cross_target_target_keys=cross_target_target_keys,
        covered_source_horizons_hours=covered_source_horizons_hours,
    )
    try:
        writer.append_log_entry("shared", entry_md)
    except Exception as exc:  # pragma: no cover - guarded by targeted tests
        raise _shared_anchor_write_error(
            source_anchor_id=source_anchor_id,
            artifact_path=artifact_path,
            detail=str(exc),
        ) from exc

    return SharedAnchorWriteReceipt(
        source_anchor_id=source_anchor_id,
        artifact_path=artifact_path,
        input_anchor_handling=input_anchor_handling,
        cross_target_target_keys=cross_target_target_keys,
        covered_source_horizons_hours=covered_source_horizons_hours,
        wrote_shared_anchor=True,
        shared_log_append_decision="append_shared_anchor",
        appended_anchor_id=_derive_appended_anchor_id(entry_md),
        decision_rationale=(
            "Appended a new shared/log.md follow-on anchor because a shared input "
            "produced a concrete cross-target review signal worth revisiting later."
        ),
        skip_reason=None,
    )


def _has_non_entry_content_after_title(lines: list[str]) -> bool:
    return any(line.strip() for line in lines[1:])


def _validate_surface(
    *,
    log_page_path: str,
    content_md: str,
    scope_kind: Literal["shared", "target"],
) -> str:
    try:
        return validate_log_page(content_md, scope_kind=scope_kind)
    except ResearchMemoryContractError as exc:
        line_count = max(len(str(content_md).splitlines()), 1)
        raise ReflectionAnchorParseError(
            f"Failed to parse reflection log surface {log_page_path} lines 1-{line_count}: {exc}"
        ) from exc


def _normalize_logged_at(
    heading_text: str,
    *,
    log_page_path: str,
    line_start: int,
    line_end: int,
) -> tuple[datetime | None, str | None]:
    normalized_heading = heading_text.strip()
    try:
        if _DATE_ONLY_RE.fullmatch(normalized_heading):
            return datetime.fromisoformat(normalized_heading).replace(tzinfo=UTC), None

        parsed = datetime.fromisoformat(normalized_heading.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(
                "heading timestamps that include a time must include an explicit timezone."
            )
        return parsed, None
    except ValueError as exc:
        return None, _format_entry_error(
            log_page_path=log_page_path,
            line_start=line_start,
            line_end=line_end,
            detail=(
                f"heading {normalized_heading!r} could not be normalized as an ISO-8601 "
                f"date or timezone-aware datetime ({exc})."
            ),
        )


def _extract_citations(entry_md: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    event_ids: list[str] = []
    source_refs: list[str] = []
    seen_event_ids: set[str] = set()
    seen_source_refs: set[str] = set()

    for match in _CITATION_RE.finditer(entry_md):
        event_id = match.group("event_id")
        source_ref = match.group("source_ref").strip()
        try:
            validated_event_id = validate_event_id(
                event_id,
                error_type=ReflectionAnchorParseError,
            )
            validated_source_ref = validate_source_ref(
                source_ref,
                error_type=ReflectionAnchorParseError,
            )
        except ReflectionAnchorParseError:
            continue

        if validated_event_id not in seen_event_ids:
            seen_event_ids.add(validated_event_id)
            event_ids.append(validated_event_id)
        if validated_source_ref not in seen_source_refs:
            seen_source_refs.add(validated_source_ref)
            source_refs.append(validated_source_ref)

    return tuple(event_ids), tuple(source_refs)


def _derive_entry_key(log_page_path: str, heading_text: str, entry_md: str) -> str:
    slug = _slugify(heading_text)
    digest = hashlib.sha1(f"{log_page_path}\n{entry_md}".encode()).hexdigest()[:12]
    return f"{slug}-{digest}"


def _validate_research_memory_port(research_memory: ResearchMemoryPort) -> None:
    if not callable(getattr(research_memory, "append_log_entry", None)):
        raise SharedAnchorWriteError(
            "research_memory must implement append_log_entry(scope_key, entry_md)."
        )


def _cross_target_target_keys(
    original_evidence: tuple[EvidenceLedgerRecord, ...],
) -> tuple[str, ...]:
    target_keys: list[str] = []
    seen: set[str] = set()
    for record in original_evidence:
        if record.target_key in seen:
            continue
        seen.add(record.target_key)
        target_keys.append(record.target_key)
    return tuple(target_keys)


def _covered_source_horizons_hours(
    evaluation: ReflectionEvaluationResult,
) -> tuple[float, ...]:
    if evaluation.receipt is not None:
        return tuple(sorted(evaluation.receipt.assessed_horizons_hours))
    return tuple(
        sorted(assessment.horizon_hours for assessment in evaluation.assessed_horizons)
    )


def _covered_horizons_for_source_anchor(
    *,
    existing_content_md: str,
    source_anchor_id: str,
) -> set[float]:
    from .eligibility import parse_reflection_coverage

    records = parse_reflection_coverage(
        page=PageReadResult(page_path="shared/log.md", content_md=existing_content_md)
    )
    covered_horizons: set[float] = set()
    for record in records:
        if record.anchor_id == source_anchor_id:
            covered_horizons.update(record.covered_horizons_hours)
    return covered_horizons


def _render_shared_follow_on_entry(
    *,
    source_anchor_id: str,
    source_entry: ReflectionLogEntry,
    original_evidence: tuple[EvidenceLedgerRecord, ...],
    evaluation: ReflectionEvaluationResult,
    cross_target_target_keys: tuple[str, ...],
    covered_source_horizons_hours: tuple[float, ...],
) -> str:
    heading_timestamp = evaluation.anchor.logged_at + timedelta(
        hours=max(covered_source_horizons_hours)
    )
    lines = [
        f"## {_format_timestamp(heading_timestamp)}",
        (
            "Reflection preserved a shared follow-on anchor because a cross-target "
            "review signal changed what should stay reviewable later."
        ),
        "",
        (
            "<!-- reflection-coverage: "
            f"anchor_id={source_anchor_id}; covered_horizons_hours="
            f"{','.join(_format_hour_value(hour) for hour in covered_source_horizons_hours)} -->"
        ),
        "",
        f"- Source Anchor: `{source_anchor_id}`",
        (
            "- Input Anchor Handling: "
            "`shared_input`"
            if source_entry.log_page_path == "shared/log.md"
            else "- Input Anchor Handling: `target_input`"
        ),
        "- Shared Log Append Decision: `append_shared_anchor`",
        (
            "- Covered Source Horizons (hours): "
            f"{_format_hours_code_list(covered_source_horizons_hours)}"
        ),
        (
            "- Cross-Target Targets: "
            f"{', '.join(f'`{target_key}`' for target_key in cross_target_target_keys)}"
        ),
        f"- Reflection Rationale: {evaluation.decision_rationale}",
        "",
        "### Why Revisit Later",
        "",
        (
            "Preserve this as a weak shared review anchor because the source shared "
            "reflection touched multiple targets and produced a concrete review signal "
            "that should remain revisit-able without mutating prompt assets or truth "
            "boundaries."
        ),
        "",
        "### Evidence",
        "",
    ]
    for record in original_evidence:
        lines.append(f"- `{record.event_id}` | `{record.source_ref}`")
    return validate_log_append_entry("\n".join(lines).rstrip() + "\n")


def _derive_appended_anchor_id(entry_md: str) -> str:
    entries = parse_reflection_log_entries(
        log_page_path="shared/log.md",
        content_md="# shared log\n\n" + entry_md,
    )
    if len(entries) != 1:
        raise SharedAnchorWriteError(
            "Expected one canonical shared follow-on anchor entry after render."
        )
    return entries[0].anchor_id


def _normalize_target_keys(value: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise SharedAnchorWriteError(
            "cross_target_target_keys must be a tuple of target keys."
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for target_key in value:
        validated_target_key = validate_target_key(
            target_key,
            error_type=SharedAnchorWriteError,
        )
        if validated_target_key in seen:
            raise SharedAnchorWriteError(
                "cross_target_target_keys must not contain duplicates."
            )
        seen.add(validated_target_key)
        normalized.append(validated_target_key)
    return tuple(normalized)


def _normalize_horizons_hours(
    value: tuple[float, ...],
    *,
    field_name: str,
    error_type: type[ValueError],
) -> tuple[float, ...]:
    if not isinstance(value, tuple) or not value:
        raise error_type(
            f"{field_name} must be a non-empty tuple of positive horizon hours."
        )
    normalized: list[float] = []
    seen: set[float] = set()
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise error_type(
                f"{field_name}[{index}] must be a positive number of hours."
            )
        horizon = float(item)
        if horizon <= 0:
            raise error_type(f"{field_name}[{index}] must be greater than zero hours.")
        if horizon in seen:
            raise error_type(f"{field_name} must not contain duplicate horizon values.")
        seen.add(horizon)
        normalized.append(horizon)
    return tuple(normalized)


def _normalize_anchor_id(
    value: str,
    *,
    error_type: type[ValueError],
) -> str:
    if not isinstance(value, str):
        raise error_type("anchor_id must be a string.")

    anchor_id = value.strip()
    if not anchor_id:
        raise error_type("anchor_id must not be blank.")
    log_page_path, separator, entry_key = anchor_id.partition("#")
    if separator != "#" or not log_page_path or not entry_key:
        raise error_type(
            "anchor_id must use the canonical '<log_page_path>#<entry_key>' format."
        )
    resolve_reflection_anchor_page_ref(log_page_path)
    validate_page_key(
        entry_key,
        field_name="anchor_id entry_key",
        error_type=error_type,
    )
    return f"{log_page_path}#{entry_key}"


def _slugify(value: str) -> str:
    slug = _NON_SLUG_RE.sub("-", value.lower()).strip("-")
    return (slug[:48] or "entry").rstrip("-") or "entry"


def _format_entry_error(
    *,
    log_page_path: str,
    line_start: int,
    line_end: int,
    detail: str,
) -> str:
    return (
        f"Failed to normalize reflection anchor at {log_page_path} lines "
        f"{line_start}-{line_end}: {detail}"
    )


def _format_timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _format_hours_code_list(hours: tuple[float, ...]) -> str:
    return ", ".join(f"`{_format_hour_value(hour)}`" for hour in hours)


def _format_hour_value(hour: float) -> str:
    if float(hour).is_integer():
        return str(int(hour))
    return format(hour, "g")


def _format_horizons_tuple(hours: tuple[float, ...]) -> str:
    return f"({', '.join(_format_hour_value(hour) for hour in hours)})"


def _anchor_id(log_page_path: str, entry_key: str) -> str:
    return f"{log_page_path}#{entry_key}"


def _shared_anchor_write_error(
    *,
    source_anchor_id: str,
    artifact_path: Path,
    detail: str,
) -> SharedAnchorWriteError:
    return SharedAnchorWriteError(
        "Failed to write canonical shared follow-on anchor: "
        f"source_anchor_id={source_anchor_id!r} "
        f"intended_artifact_path={artifact_path.as_posix()!r} "
        "blocked_page_family='log'. "
        f"{detail}"
    )


__all__ = [
    "ReflectionAnchorParseError",
    "ReflectionLogEntry",
    "parse_reflection_log_entries",
]
