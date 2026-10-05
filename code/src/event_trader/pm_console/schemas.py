"""PM Console API DTOs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime


class PMConsoleSchemaError(ValueError):
    """Raised when a PM Console DTO is malformed."""


_RUNTIME_STATUSES = frozenset({"running", "not_running", "unknown"})
_RUNTIME_MODES = frozenset({"live", "replay", "catchup", "unknown"})
_DATA_SOURCES = frozenset({"workspace_snapshot", "runtime_updating_workspace", "unknown"})
_TARGET_VIEWS = frozenset({"workbench", "position", "thesis"})
_TRANSLATION_LOCALES = frozenset({"zh-CN"})


@dataclass(frozen=True, slots=True)
class PMConsoleStatusDTO:
    code: str
    explanation: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _require_non_blank(self.code, "code"))
        object.__setattr__(
            self,
            "explanation",
            _require_non_blank(self.explanation, "explanation"),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {"code": self.code, "explanation": self.explanation}


@dataclass(frozen=True, slots=True)
class PMConsoleCurrentPortfolioStateDTO:
    status: PMConsoleStatusDTO
    state: str | None
    target_weight: float | None
    updated_at: str | None
    source_pm_decision_id: str | None
    source_execution_record_id: str | None
    decision_episode_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PMConsoleStatusDTO):
            raise PMConsoleSchemaError("status must be a PMConsoleStatusDTO.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "status": self.status.to_json_payload(),
            "state": self.state,
            "target_weight": self.target_weight,
            "updated_at": self.updated_at,
            "source_pm_decision_id": self.source_pm_decision_id,
            "source_execution_record_id": self.source_execution_record_id,
            "decision_episode_id": self.decision_episode_id,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleLineDTO:
    key: str
    label: str
    role: str
    status: PMConsoleStatusDTO

    def __post_init__(self) -> None:
        for field_name in ("key", "label", "role"):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )
        if not isinstance(self.status, PMConsoleStatusDTO):
            raise PMConsoleSchemaError("status must be a PMConsoleStatusDTO.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "role": self.role,
            "status": self.status.to_json_payload(),
            "explanation": self.status.explanation,
        }


@dataclass(frozen=True, slots=True)
class PMConsolePointDTO:
    time: str
    price: float
    pm_pipeline_value: float | None
    buy_hold_value: float | None
    analysis_direct_shadow_value: float | None
    pm_target_weight: float | None
    analysis_shadow_target_weight: float | None
    pm_state: str | None
    analysis_shadow_state: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "time", _require_non_blank(self.time, "time"))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "time": self.time,
            "price": self.price,
            "pm_pipeline_value": self.pm_pipeline_value,
            "buy_hold_value": self.buy_hold_value,
            "analysis_direct_shadow_value": self.analysis_direct_shadow_value,
            "pm_target_weight": self.pm_target_weight,
            "analysis_shadow_target_weight": self.analysis_shadow_target_weight,
            "pm_state": self.pm_state,
            "analysis_shadow_state": self.analysis_shadow_state,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleMarkerDTO:
    marker_id: str
    kind: str
    lane: str
    label: str
    time: str
    business_at: str
    state: str
    target_weight: float
    price: float
    line_value: float | None
    shape: str
    position: str
    text: str
    source_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "marker_id",
            "kind",
            "lane",
            "label",
            "time",
            "business_at",
            "state",
            "shape",
            "position",
            "text",
            "source_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "marker_id": self.marker_id,
            "kind": self.kind,
            "lane": self.lane,
            "label": self.label,
            "time": self.time,
            "business_at": self.business_at,
            "state": self.state,
            "target_weight": self.target_weight,
            "price": self.price,
            "line_value": self.line_value,
            "shape": self.shape,
            "position": self.position,
            "text": self.text,
            "source_id": self.source_id,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleDecisionDTO:
    decision_id: str
    business_at: str
    requested_state: str
    requested_target_weight: float
    execution_required: bool
    pm_review_request_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _require_non_blank(self.decision_id, "decision_id"))
        object.__setattr__(self, "business_at", _require_non_blank(self.business_at, "business_at"))
        object.__setattr__(
            self,
            "requested_state",
            _require_non_blank(self.requested_state, "requested_state"),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "business_at": self.business_at,
            "requested_state": self.requested_state,
            "requested_target_weight": self.requested_target_weight,
            "execution_required": self.execution_required,
            "pm_review_request_id": self.pm_review_request_id,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleExecutionDTO:
    execution_record_id: str
    business_at: str
    status: str
    pm_decision_id: str
    requested_target_weight: float
    executed_at: str | None
    target_weight: float | None

    def __post_init__(self) -> None:
        for field_name in ("execution_record_id", "business_at", "status", "pm_decision_id"):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "execution_record_id": self.execution_record_id,
            "business_at": self.business_at,
            "status": self.status,
            "pm_decision_id": self.pm_decision_id,
            "requested_target_weight": self.requested_target_weight,
            "executed_at": self.executed_at,
            "target_weight": self.target_weight,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleAnalysisShadowSourceDTO:
    assessment_id: str
    business_at: str
    as_if_flat_state: str
    target_weight: float

    def __post_init__(self) -> None:
        for field_name in ("assessment_id", "business_at", "as_if_flat_state"):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "assessment_id": self.assessment_id,
            "business_at": self.business_at,
            "as_if_flat_state": self.as_if_flat_state,
            "target_weight": self.target_weight,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleAuditDTO:
    source_paths: tuple[str, ...]
    counts: Mapping[str, int]
    warnings: tuple[str, ...]

    def to_json_payload(self) -> dict[str, object]:
        return {
            "source_paths": list(self.source_paths),
            "counts": dict(self.counts),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleTargetDTO:
    target_key: str
    implemented_views: tuple[str, ...]
    default_view: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_key", _require_non_blank(self.target_key, "target_key"))
        implemented_views = tuple(
            _require_allowed(view, "implemented_views", _TARGET_VIEWS)
            for view in self.implemented_views
        )
        if not implemented_views:
            raise PMConsoleSchemaError("implemented_views must not be empty.")
        object.__setattr__(self, "implemented_views", implemented_views)
        default_view = _require_allowed(self.default_view, "default_view", _TARGET_VIEWS)
        if default_view not in implemented_views:
            raise PMConsoleSchemaError("default_view must be included in implemented_views.")
        object.__setattr__(self, "default_view", default_view)

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "implemented_views": list(self.implemented_views),
            "default_view": self.default_view,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleTargetsResponseDTO:
    generated_at: str
    targets: tuple[PMConsoleTargetDTO, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_at",
            _require_non_blank(self.generated_at, "generated_at"),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "targets": [target.to_json_payload() for target in self.targets],
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisTranslationAvailabilityDTO:
    locale: str
    available: bool
    reason_code: str | None = None
    reason_message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "locale",
            _require_allowed(self.locale, "locale", _TRANSLATION_LOCALES),
        )
        if not isinstance(self.available, bool):
            raise PMConsoleSchemaError("available must be a boolean.")
        for field_name in ("reason_code", "reason_message"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _require_non_blank(value, field_name),
                )
        if self.available and (self.reason_code is not None or self.reason_message is not None):
            raise PMConsoleSchemaError(
                "available thesis translation must not include reason_code or reason_message."
            )
        if not self.available and (
            self.reason_code is None or self.reason_message is None
        ):
            raise PMConsoleSchemaError(
                "unavailable thesis translation must include reason_code and reason_message."
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "locale": self.locale,
            "available": self.available,
            "reason_code": self.reason_code,
            "reason_message": self.reason_message,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleContextResponseDTO:
    generated_at: str
    workspace_root: str
    runtime_status: str
    runtime_mode: str
    data_source: str
    explanation: str
    thesis_translation: PMConsoleThesisTranslationAvailabilityDTO

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_at",
            _require_non_blank(self.generated_at, "generated_at"),
        )
        object.__setattr__(
            self,
            "workspace_root",
            _require_non_blank(self.workspace_root, "workspace_root"),
        )
        object.__setattr__(
            self,
            "runtime_status",
            _require_allowed(
                self.runtime_status,
                "runtime_status",
                _RUNTIME_STATUSES,
            ),
        )
        object.__setattr__(
            self,
            "runtime_mode",
            _require_allowed(
                self.runtime_mode,
                "runtime_mode",
                _RUNTIME_MODES,
            ),
        )
        object.__setattr__(
            self,
            "data_source",
            _require_allowed(
                self.data_source,
                "data_source",
                _DATA_SOURCES,
            ),
        )
        object.__setattr__(
            self,
            "explanation",
            _require_non_blank(self.explanation, "explanation"),
        )
        if not isinstance(
            self.thesis_translation,
            PMConsoleThesisTranslationAvailabilityDTO,
        ):
            raise PMConsoleSchemaError(
                "thesis_translation must be a PMConsoleThesisTranslationAvailabilityDTO."
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "workspace_root": self.workspace_root,
            "runtime_status": self.runtime_status,
            "runtime_mode": self.runtime_mode,
            "data_source": self.data_source,
            "explanation": self.explanation,
            "thesis_translation": self.thesis_translation.to_json_payload(),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleMarkdownSectionDTO:
    page_path: str
    page_name: str
    section_name: str
    content_md: str
    content_sha256: str | None = None
    changed_in_revision: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "page_path", _require_non_blank(self.page_path, "page_path"))
        object.__setattr__(self, "page_name", _require_non_blank(self.page_name, "page_name"))
        object.__setattr__(
            self,
            "section_name",
            _require_non_blank(self.section_name, "section_name"),
        )
        if not isinstance(self.content_md, str):
            raise PMConsoleSchemaError("content_md must be a string.")
        if self.content_sha256 is not None:
            object.__setattr__(
                self,
                "content_sha256",
                _require_non_blank(self.content_sha256, "content_sha256"),
            )
        if not isinstance(self.changed_in_revision, bool):
            raise PMConsoleSchemaError("changed_in_revision must be a boolean.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "page_path": self.page_path,
            "page_name": self.page_name,
            "section_name": self.section_name,
            "content_md": self.content_md,
            "content_sha256": self.content_sha256,
            "changed_in_revision": self.changed_in_revision,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisSectionDiffDTO:
    page_path: str
    page_name: str
    section_name: str
    unified_diff_md: str
    previous_content_sha256: str
    content_sha256: str
    changed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "page_path", _require_non_blank(self.page_path, "page_path"))
        object.__setattr__(self, "page_name", _require_non_blank(self.page_name, "page_name"))
        object.__setattr__(
            self,
            "section_name",
            _require_non_blank(self.section_name, "section_name"),
        )
        if not isinstance(self.unified_diff_md, str):
            raise PMConsoleSchemaError("unified_diff_md must be a string.")
        object.__setattr__(
            self,
            "previous_content_sha256",
            _require_non_blank(
                self.previous_content_sha256,
                "previous_content_sha256",
            ),
        )
        object.__setattr__(
            self,
            "content_sha256",
            _require_non_blank(self.content_sha256, "content_sha256"),
        )
        if not isinstance(self.changed, bool):
            raise PMConsoleSchemaError("changed must be a boolean.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "page_path": self.page_path,
            "page_name": self.page_name,
            "section_name": self.section_name,
            "unified_diff_md": self.unified_diff_md,
            "previous_content_sha256": self.previous_content_sha256,
            "content_sha256": self.content_sha256,
            "changed": self.changed,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisRevisionSummaryDTO:
    revision_id: str
    target_key: str
    business_at: str
    committed_at: str
    source: str
    source_event_count: int
    changed_claim_count: int
    changed_section_count: int
    previous_revision_id: str | None
    is_baseline: bool

    def __post_init__(self) -> None:
        for field_name in (
            "revision_id",
            "target_key",
            "business_at",
            "committed_at",
            "source",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )
        if not isinstance(self.source_event_count, int):
            raise PMConsoleSchemaError("source_event_count must be an integer.")
        if not isinstance(self.changed_claim_count, int):
            raise PMConsoleSchemaError("changed_claim_count must be an integer.")
        if not isinstance(self.changed_section_count, int):
            raise PMConsoleSchemaError("changed_section_count must be an integer.")
        if self.previous_revision_id is not None:
            object.__setattr__(
                self,
                "previous_revision_id",
                _require_non_blank(self.previous_revision_id, "previous_revision_id"),
            )
        if not isinstance(self.is_baseline, bool):
            raise PMConsoleSchemaError("is_baseline must be a boolean.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "target_key": self.target_key,
            "business_at": self.business_at,
            "committed_at": self.committed_at,
            "source": self.source,
            "source_event_count": self.source_event_count,
            "changed_claim_count": self.changed_claim_count,
            "changed_section_count": self.changed_section_count,
            "previous_revision_id": self.previous_revision_id,
            "is_baseline": self.is_baseline,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisPriceLevelDTO:
    level_id: str
    display_role: str
    role_if_flat: str
    value: float | None
    lower: float | None
    upper: float | None
    instrument_basis: str

    def __post_init__(self) -> None:
        for field_name in (
            "level_id",
            "display_role",
            "role_if_flat",
            "instrument_basis",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "level_id": self.level_id,
            "display_role": self.display_role,
            "role_if_flat": self.role_if_flat,
            "value": self.value,
            "lower": self.lower,
            "upper": self.upper,
            "instrument_basis": self.instrument_basis,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisRevisionDetailDTO:
    revision_id: str
    target_key: str
    business_at: str
    committed_at: str
    source: str
    analysis_assessment_id: str | None
    context_packet_id: str | None
    context_packet_hash: str | None
    source_event_ids: tuple[str, ...]
    research_memory_write_receipt_ids: tuple[str, ...]
    changed_claim_ids: tuple[str, ...]
    key_claim_ids: tuple[str, ...]
    contested_prior_claim_ids: tuple[str, ...]
    price_level_role_ids: tuple[str, ...]
    price_levels: tuple[PMConsoleThesisPriceLevelDTO, ...]
    previous_revision_id: str | None
    canonical_bundle_sha256: str
    is_baseline: bool
    sections: tuple[PMConsoleMarkdownSectionDTO, ...]
    diffs_from_previous: tuple[PMConsoleThesisSectionDiffDTO, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "revision_id",
            "target_key",
            "business_at",
            "committed_at",
            "source",
            "canonical_bundle_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )
        for field_name in (
            "analysis_assessment_id",
            "context_packet_id",
            "context_packet_hash",
            "previous_revision_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _require_non_blank(value, field_name),
                )
        for field_name in (
            "source_event_ids",
            "research_memory_write_receipt_ids",
            "changed_claim_ids",
            "key_claim_ids",
            "contested_prior_claim_ids",
            "price_level_role_ids",
            "price_levels",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, tuple):
                raise PMConsoleSchemaError(f"{field_name} must be a tuple.")
        for level in self.price_levels:
            if not isinstance(level, PMConsoleThesisPriceLevelDTO):
                raise PMConsoleSchemaError(
                    "price_levels must contain PMConsoleThesisPriceLevelDTO."
                )
        if not isinstance(self.is_baseline, bool):
            raise PMConsoleSchemaError("is_baseline must be a boolean.")
        for section in self.sections:
            if not isinstance(section, PMConsoleMarkdownSectionDTO):
                raise PMConsoleSchemaError("sections must contain PMConsoleMarkdownSectionDTO.")
        for diff in self.diffs_from_previous:
            if not isinstance(diff, PMConsoleThesisSectionDiffDTO):
                raise PMConsoleSchemaError(
                    "diffs_from_previous must contain PMConsoleThesisSectionDiffDTO."
                )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "target_key": self.target_key,
            "business_at": self.business_at,
            "committed_at": self.committed_at,
            "source": self.source,
            "analysis_assessment_id": self.analysis_assessment_id,
            "context_packet_id": self.context_packet_id,
            "context_packet_hash": self.context_packet_hash,
            "source_event_ids": list(self.source_event_ids),
            "research_memory_write_receipt_ids": list(
                self.research_memory_write_receipt_ids
            ),
            "changed_claim_ids": list(self.changed_claim_ids),
            "key_claim_ids": list(self.key_claim_ids),
            "contested_prior_claim_ids": list(self.contested_prior_claim_ids),
            "price_level_role_ids": list(self.price_level_role_ids),
            "price_levels": [level.to_json_payload() for level in self.price_levels],
            "previous_revision_id": self.previous_revision_id,
            "canonical_bundle_sha256": self.canonical_bundle_sha256,
            "is_baseline": self.is_baseline,
            "sections": [section.to_json_payload() for section in self.sections],
            "diffs_from_previous": [
                diff.to_json_payload() for diff in self.diffs_from_previous
            ],
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisRevisionsResponseDTO:
    generated_at: str
    target_key: str
    revisions: tuple[PMConsoleThesisRevisionSummaryDTO, ...]
    latest_revision: PMConsoleThesisRevisionSummaryDTO | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_at",
            _require_non_blank(self.generated_at, "generated_at"),
        )
        object.__setattr__(
            self,
            "target_key",
            _require_non_blank(self.target_key, "target_key"),
        )
        for revision in self.revisions:
            if not isinstance(revision, PMConsoleThesisRevisionSummaryDTO):
                raise PMConsoleSchemaError(
                    "revisions must contain PMConsoleThesisRevisionSummaryDTO."
                )
        if self.latest_revision is not None and not isinstance(
            self.latest_revision, PMConsoleThesisRevisionSummaryDTO
        ):
            raise PMConsoleSchemaError(
                "latest_revision must be a PMConsoleThesisRevisionSummaryDTO when set."
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "target_key": self.target_key,
            "revisions": [revision.to_json_payload() for revision in self.revisions],
            "latest_revision": (
                None
                if self.latest_revision is None
                else self.latest_revision.to_json_payload()
            ),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisRevisionDetailResponseDTO:
    generated_at: str
    target_key: str
    revision: PMConsoleThesisRevisionDetailDTO

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_at",
            _require_non_blank(self.generated_at, "generated_at"),
        )
        object.__setattr__(
            self,
            "target_key",
            _require_non_blank(self.target_key, "target_key"),
        )
        if not isinstance(self.revision, PMConsoleThesisRevisionDetailDTO):
            raise PMConsoleSchemaError("revision must be a PMConsoleThesisRevisionDetailDTO.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "target_key": self.target_key,
            "revision": self.revision.to_json_payload(),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisTranslationRequestDTO:
    canonical_bundle_sha256: str
    locale: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_bundle_sha256",
            _require_non_blank(self.canonical_bundle_sha256, "canonical_bundle_sha256"),
        )
        object.__setattr__(
            self,
            "locale",
            _require_allowed(self.locale, "locale", _TRANSLATION_LOCALES),
        )


@dataclass(frozen=True, slots=True)
class PMConsoleThesisTranslationTranslatorDTO:
    provider: str
    model: str
    prompt_version: str

    def __post_init__(self) -> None:
        for field_name in ("provider", "model", "prompt_version"):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisTranslationVerificationDTO:
    passed: bool
    failures: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise PMConsoleSchemaError("passed must be a boolean.")
        if not isinstance(self.failures, tuple):
            raise PMConsoleSchemaError("failures must be a tuple.")
        for failure in self.failures:
            _require_non_blank(failure, "failure")
        if self.passed and self.failures:
            raise PMConsoleSchemaError("passed verification must not include failures.")
        if not self.passed and not self.failures:
            raise PMConsoleSchemaError("failed verification must include failures.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "failures": list(self.failures),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleTranslatedSectionDTO:
    section_id: str
    page_path: str
    page_name: str
    section_name: str
    source_content_sha256: str
    translated_content_md: str

    def __post_init__(self) -> None:
        for field_name in (
            "section_id",
            "page_path",
            "page_name",
            "section_name",
            "source_content_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )
        if not isinstance(self.translated_content_md, str):
            raise PMConsoleSchemaError("translated_content_md must be a string.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "section_id": self.section_id,
            "page_path": self.page_path,
            "page_name": self.page_name,
            "section_name": self.section_name,
            "source_content_sha256": self.source_content_sha256,
            "translated_content_md": self.translated_content_md,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisTranslationResponseDTO:
    generated_at: str
    target_key: str
    revision_id: str
    locale: str
    canonical_bundle_sha256: str
    sections: tuple[PMConsoleTranslatedSectionDTO, ...]
    cached: bool
    translator: PMConsoleThesisTranslationTranslatorDTO
    verification: PMConsoleThesisTranslationVerificationDTO

    def __post_init__(self) -> None:
        for field_name in (
            "generated_at",
            "target_key",
            "revision_id",
            "locale",
            "canonical_bundle_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "locale",
            _require_allowed(self.locale, "locale", _TRANSLATION_LOCALES),
        )
        if not isinstance(self.sections, tuple) or not self.sections:
            raise PMConsoleSchemaError("sections must be a non-empty tuple.")
        for section in self.sections:
            if not isinstance(section, PMConsoleTranslatedSectionDTO):
                raise PMConsoleSchemaError(
                    "sections must contain PMConsoleTranslatedSectionDTO."
                )
        if not isinstance(self.cached, bool):
            raise PMConsoleSchemaError("cached must be a boolean.")
        if not isinstance(self.translator, PMConsoleThesisTranslationTranslatorDTO):
            raise PMConsoleSchemaError(
                "translator must be a PMConsoleThesisTranslationTranslatorDTO."
            )
        if not isinstance(self.verification, PMConsoleThesisTranslationVerificationDTO):
            raise PMConsoleSchemaError(
                "verification must be a PMConsoleThesisTranslationVerificationDTO."
            )
        if not self.verification.passed:
            raise PMConsoleSchemaError(
                "translation success response requires passed verification."
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "target_key": self.target_key,
            "revision_id": self.revision_id,
            "locale": self.locale,
            "canonical_bundle_sha256": self.canonical_bundle_sha256,
            "sections": [section.to_json_payload() for section in self.sections],
            "cached": self.cached,
            "translator": self.translator.to_json_payload(),
            "verification": self.verification.to_json_payload(),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisTranslationFailureDTO:
    generated_at: str
    target_key: str
    revision_id: str
    locale: str
    canonical_bundle_sha256: str
    error_code: str
    error_message: str
    translator: PMConsoleThesisTranslationTranslatorDTO | None
    verification: PMConsoleThesisTranslationVerificationDTO | None

    def __post_init__(self) -> None:
        for field_name in (
            "generated_at",
            "target_key",
            "revision_id",
            "locale",
            "canonical_bundle_sha256",
            "error_code",
            "error_message",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "locale",
            _require_allowed(self.locale, "locale", _TRANSLATION_LOCALES),
        )
        if self.translator is not None and not isinstance(
            self.translator,
            PMConsoleThesisTranslationTranslatorDTO,
        ):
            raise PMConsoleSchemaError(
                "translator must be a PMConsoleThesisTranslationTranslatorDTO when set."
            )
        if self.verification is not None and not isinstance(
            self.verification,
            PMConsoleThesisTranslationVerificationDTO,
        ):
            raise PMConsoleSchemaError(
                "verification must be a PMConsoleThesisTranslationVerificationDTO when set."
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "target_key": self.target_key,
            "revision_id": self.revision_id,
            "locale": self.locale,
            "canonical_bundle_sha256": self.canonical_bundle_sha256,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "translator": (
                None if self.translator is None else self.translator.to_json_payload()
            ),
            "verification": (
                None if self.verification is None else self.verification.to_json_payload()
            ),
        }


@dataclass(frozen=True, slots=True)
class PMConsolePositionResponseDTO:
    target_key: str
    generated_at: str
    current_portfolio_state: PMConsoleCurrentPortfolioStateDTO
    market_data_status: PMConsoleStatusDTO
    comparison_status: PMConsoleStatusDTO
    market_symbol: str | None
    price_display_decimals: int
    bar_granularity: str | None
    market_data_path: str | None
    base_value: float
    lines: tuple[PMConsoleLineDTO, ...]
    points: tuple[PMConsolePointDTO, ...]
    markers: tuple[PMConsoleMarkerDTO, ...]
    latest_pm_decision: PMConsoleDecisionDTO | None
    latest_execution: PMConsoleExecutionDTO | None
    latest_analysis_shadow_source: PMConsoleAnalysisShadowSourceDTO | None
    notes: tuple[str, ...]
    audit: PMConsoleAuditDTO | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            _require_non_blank(self.target_key, "target_key"),
        )
        object.__setattr__(
            self,
            "generated_at",
            _require_non_blank(self.generated_at, "generated_at"),
        )
        if not isinstance(self.current_portfolio_state, PMConsoleCurrentPortfolioStateDTO):
            raise PMConsoleSchemaError(
                "current_portfolio_state must be a PMConsoleCurrentPortfolioStateDTO."
            )
        if not isinstance(self.market_data_status, PMConsoleStatusDTO):
            raise PMConsoleSchemaError("market_data_status must be a PMConsoleStatusDTO.")
        if not isinstance(self.comparison_status, PMConsoleStatusDTO):
            raise PMConsoleSchemaError("comparison_status must be a PMConsoleStatusDTO.")
        if (
            isinstance(self.price_display_decimals, bool)
            or not isinstance(self.price_display_decimals, int)
            or self.price_display_decimals <= 0
        ):
            raise PMConsoleSchemaError("price_display_decimals must be a positive integer.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "generated_at": self.generated_at,
            "current_portfolio_state": self.current_portfolio_state.to_json_payload(),
            "market_data_status": self.market_data_status.to_json_payload(),
            "comparison_status": self.comparison_status.to_json_payload(),
            "market_symbol": self.market_symbol,
            "price_display_decimals": self.price_display_decimals,
            "bar_granularity": self.bar_granularity,
            "market_data_path": self.market_data_path,
            "base_value": self.base_value,
            "lines": [line.to_json_payload() for line in self.lines],
            "points": [point.to_json_payload() for point in self.points],
            "markers": [marker.to_json_payload() for marker in self.markers],
            "latest_pm_decision": (
                None
                if self.latest_pm_decision is None
                else self.latest_pm_decision.to_json_payload()
            ),
            "latest_execution": (
                None if self.latest_execution is None else self.latest_execution.to_json_payload()
            ),
            "latest_analysis_shadow_source": (
                None
                if self.latest_analysis_shadow_source is None
                else self.latest_analysis_shadow_source.to_json_payload()
            ),
            "notes": list(self.notes),
            "audit": None if self.audit is None else self.audit.to_json_payload(),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleWorkbenchSectionDTO:
    page_path: str
    page_name: str
    section_name: str
    content_md: str
    status: PMConsoleStatusDTO
    content_sha256: str | None = None
    changed_in_latest_revision: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "page_path", _require_non_blank(self.page_path, "page_path"))
        object.__setattr__(self, "page_name", _require_non_blank(self.page_name, "page_name"))
        object.__setattr__(
            self,
            "section_name",
            _require_non_blank(self.section_name, "section_name"),
        )
        if not isinstance(self.content_md, str):
            raise PMConsoleSchemaError("content_md must be a string.")
        if not isinstance(self.status, PMConsoleStatusDTO):
            raise PMConsoleSchemaError("status must be a PMConsoleStatusDTO.")
        if self.content_sha256 is not None:
            object.__setattr__(
                self,
                "content_sha256",
                _require_non_blank(self.content_sha256, "content_sha256"),
            )
        if not isinstance(self.changed_in_latest_revision, bool):
            raise PMConsoleSchemaError("changed_in_latest_revision must be a boolean.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "page_path": self.page_path,
            "page_name": self.page_name,
            "section_name": self.section_name,
            "content_md": self.content_md,
            "status": self.status.to_json_payload(),
            "content_sha256": self.content_sha256,
            "changed_in_latest_revision": self.changed_in_latest_revision,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleWorkbenchPositionSummaryDTO:
    status: PMConsoleStatusDTO
    current_portfolio_state: PMConsoleCurrentPortfolioStateDTO | None
    latest_pm_decision: PMConsoleDecisionDTO | None
    latest_execution: PMConsoleExecutionDTO | None
    market_data_status: PMConsoleStatusDTO | None
    comparison_status: PMConsoleStatusDTO | None
    market_symbol: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PMConsoleStatusDTO):
            raise PMConsoleSchemaError("status must be a PMConsoleStatusDTO.")
        if self.current_portfolio_state is not None and not isinstance(
            self.current_portfolio_state,
            PMConsoleCurrentPortfolioStateDTO,
        ):
            raise PMConsoleSchemaError(
                "current_portfolio_state must be a PMConsoleCurrentPortfolioStateDTO when set."
            )
        if self.latest_pm_decision is not None and not isinstance(
            self.latest_pm_decision,
            PMConsoleDecisionDTO,
        ):
            raise PMConsoleSchemaError(
                "latest_pm_decision must be a PMConsoleDecisionDTO when set."
            )
        if self.latest_execution is not None and not isinstance(
            self.latest_execution,
            PMConsoleExecutionDTO,
        ):
            raise PMConsoleSchemaError(
                "latest_execution must be a PMConsoleExecutionDTO when set."
            )
        for field_name in ("market_data_status", "comparison_status"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, PMConsoleStatusDTO):
                raise PMConsoleSchemaError(f"{field_name} must be a PMConsoleStatusDTO when set.")
        if self.market_symbol is not None:
            object.__setattr__(
                self,
                "market_symbol",
                _require_non_blank(self.market_symbol, "market_symbol"),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "status": self.status.to_json_payload(),
            "current_portfolio_state": (
                None
                if self.current_portfolio_state is None
                else self.current_portfolio_state.to_json_payload()
            ),
            "latest_pm_decision": (
                None
                if self.latest_pm_decision is None
                else self.latest_pm_decision.to_json_payload()
            ),
            "latest_execution": (
                None
                if self.latest_execution is None
                else self.latest_execution.to_json_payload()
            ),
            "market_data_status": (
                None
                if self.market_data_status is None
                else self.market_data_status.to_json_payload()
            ),
            "comparison_status": (
                None
                if self.comparison_status is None
                else self.comparison_status.to_json_payload()
            ),
            "market_symbol": self.market_symbol,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleWorkbenchPMReviewSummaryDTO:
    status: PMConsoleStatusDTO
    request_id: str | None
    business_at: str | None
    source: str | None
    review_reasons: tuple[str, ...]
    dispatch_outcome: str | None
    dispatch_run_until: str | None
    dispatch_skip_reason: str | None
    lifecycle_state: str | None
    pm_decision_id: str | None
    execution_record_id: str | None
    tool_read_count: int
    visibility_failed_tool_read_count: int
    episode_memory_status: str | None
    failure_stage: str | None
    failure_retry_allowed: bool | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PMConsoleStatusDTO):
            raise PMConsoleSchemaError("status must be a PMConsoleStatusDTO.")
        for field_name in (
            "request_id",
            "business_at",
            "source",
            "dispatch_outcome",
            "dispatch_run_until",
            "dispatch_skip_reason",
            "lifecycle_state",
            "pm_decision_id",
            "execution_record_id",
            "episode_memory_status",
            "failure_stage",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _require_non_blank(value, field_name),
                )
        if not isinstance(self.review_reasons, tuple):
            raise PMConsoleSchemaError("review_reasons must be a tuple.")
        for reason in self.review_reasons:
            _require_non_blank(reason, "review_reason")
        for field_name in ("tool_read_count", "visibility_failed_tool_read_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PMConsoleSchemaError(f"{field_name} must be a non-negative integer.")
        if self.failure_retry_allowed is not None and not isinstance(
            self.failure_retry_allowed, bool
        ):
            raise PMConsoleSchemaError("failure_retry_allowed must be a boolean when set.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "status": self.status.to_json_payload(),
            "request_id": self.request_id,
            "business_at": self.business_at,
            "source": self.source,
            "review_reasons": list(self.review_reasons),
            "dispatch_outcome": self.dispatch_outcome,
            "dispatch_run_until": self.dispatch_run_until,
            "dispatch_skip_reason": self.dispatch_skip_reason,
            "lifecycle_state": self.lifecycle_state,
            "pm_decision_id": self.pm_decision_id,
            "execution_record_id": self.execution_record_id,
            "tool_read_count": self.tool_read_count,
            "visibility_failed_tool_read_count": self.visibility_failed_tool_read_count,
            "episode_memory_status": self.episode_memory_status,
            "failure_stage": self.failure_stage,
            "failure_retry_allowed": self.failure_retry_allowed,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleWorkbenchOperatorCardDTO:
    card_id: str
    section_name: str
    body: str

    def __post_init__(self) -> None:
        for field_name in ("card_id", "section_name", "body"):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "section_name": self.section_name,
            "body": self.body,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleWorkbenchOperatorSummaryDTO:
    status: PMConsoleStatusDTO
    active_cards: tuple[PMConsoleWorkbenchOperatorCardDTO, ...]
    retired_card_count: int
    parse_error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PMConsoleStatusDTO):
            raise PMConsoleSchemaError("status must be a PMConsoleStatusDTO.")
        for card in self.active_cards:
            if not isinstance(card, PMConsoleWorkbenchOperatorCardDTO):
                raise PMConsoleSchemaError(
                    "active_cards must contain PMConsoleWorkbenchOperatorCardDTO."
                )
        if isinstance(self.retired_card_count, bool) or not isinstance(
            self.retired_card_count, int
        ) or self.retired_card_count < 0:
            raise PMConsoleSchemaError("retired_card_count must be a non-negative integer.")
        if self.parse_error is not None:
            object.__setattr__(
                self,
                "parse_error",
                _require_non_blank(self.parse_error, "parse_error"),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "status": self.status.to_json_payload(),
            "active_cards": [card.to_json_payload() for card in self.active_cards],
            "retired_card_count": self.retired_card_count,
            "parse_error": self.parse_error,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleWorkbenchResponseDTO:
    generated_at: str
    target_key: str
    sections: tuple[PMConsoleWorkbenchSectionDTO, ...]
    latest_revision: PMConsoleThesisRevisionSummaryDTO | None
    position_summary: PMConsoleWorkbenchPositionSummaryDTO
    pm_review_summary: PMConsoleWorkbenchPMReviewSummaryDTO
    operator_summary: PMConsoleWorkbenchOperatorSummaryDTO

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_at",
            _require_non_blank(self.generated_at, "generated_at"),
        )
        object.__setattr__(self, "target_key", _require_non_blank(self.target_key, "target_key"))
        for section in self.sections:
            if not isinstance(section, PMConsoleWorkbenchSectionDTO):
                raise PMConsoleSchemaError(
                    "sections must contain PMConsoleWorkbenchSectionDTO."
                )
        if self.latest_revision is not None and not isinstance(
            self.latest_revision, PMConsoleThesisRevisionSummaryDTO
        ):
            raise PMConsoleSchemaError(
                "latest_revision must be a PMConsoleThesisRevisionSummaryDTO when set."
            )
        if not isinstance(self.position_summary, PMConsoleWorkbenchPositionSummaryDTO):
            raise PMConsoleSchemaError(
                "position_summary must be a PMConsoleWorkbenchPositionSummaryDTO."
            )
        if not isinstance(self.pm_review_summary, PMConsoleWorkbenchPMReviewSummaryDTO):
            raise PMConsoleSchemaError(
                "pm_review_summary must be a PMConsoleWorkbenchPMReviewSummaryDTO."
            )
        if not isinstance(self.operator_summary, PMConsoleWorkbenchOperatorSummaryDTO):
            raise PMConsoleSchemaError(
                "operator_summary must be a PMConsoleWorkbenchOperatorSummaryDTO."
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "target_key": self.target_key,
            "sections": [section.to_json_payload() for section in self.sections],
            "latest_revision": (
                None
                if self.latest_revision is None
                else self.latest_revision.to_json_payload()
            ),
            "position_summary": self.position_summary.to_json_payload(),
            "pm_review_summary": self.pm_review_summary.to_json_payload(),
            "operator_summary": self.operator_summary.to_json_payload(),
        }


def _require_non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PMConsoleSchemaError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _require_allowed(value: object, field_name: str, allowed: frozenset[str]) -> str:
    normalized = _require_non_blank(value, field_name)
    if normalized not in allowed:
        options = ", ".join(sorted(allowed))
        raise PMConsoleSchemaError(f"{field_name} must be one of: {options}.")
    return normalized


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


__all__ = [
    "PMConsoleMarkdownSectionDTO",
    "PMConsoleAnalysisShadowSourceDTO",
    "PMConsoleAuditDTO",
    "PMConsoleContextResponseDTO",
    "PMConsoleCurrentPortfolioStateDTO",
    "PMConsoleDecisionDTO",
    "PMConsoleExecutionDTO",
    "PMConsoleLineDTO",
    "PMConsoleMarkerDTO",
    "PMConsolePointDTO",
    "PMConsolePositionResponseDTO",
    "PMConsoleSchemaError",
    "PMConsoleStatusDTO",
    "PMConsoleTargetDTO",
    "PMConsoleTranslatedSectionDTO",
    "PMConsoleThesisRevisionDetailDTO",
    "PMConsoleThesisRevisionDetailResponseDTO",
    "PMConsoleThesisTranslationFailureDTO",
    "PMConsoleThesisPriceLevelDTO",
    "PMConsoleThesisTranslationAvailabilityDTO",
    "PMConsoleThesisTranslationRequestDTO",
    "PMConsoleThesisTranslationResponseDTO",
    "PMConsoleThesisRevisionSummaryDTO",
    "PMConsoleThesisRevisionsResponseDTO",
    "PMConsoleThesisSectionDiffDTO",
    "PMConsoleThesisTranslationTranslatorDTO",
    "PMConsoleThesisTranslationVerificationDTO",
    "PMConsoleTargetsResponseDTO",
    "PMConsoleWorkbenchOperatorCardDTO",
    "PMConsoleWorkbenchOperatorSummaryDTO",
    "PMConsoleWorkbenchPMReviewSummaryDTO",
    "PMConsoleWorkbenchPositionSummaryDTO",
    "PMConsoleWorkbenchResponseDTO",
    "PMConsoleWorkbenchSectionDTO",
    "_isoformat",
]
