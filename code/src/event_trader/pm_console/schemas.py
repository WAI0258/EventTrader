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
_TARGET_VIEWS = frozenset({"operator", "episodes", "position", "thesis", "pm"})
_OVERVIEW_READINESS_STATES = frozenset(
    {"ready", "action_required", "incomplete", "unavailable"}
)
_OVERVIEW_EXECUTION_STATES = frozenset(
    {"no_decision", "no_op", "pending", "executed", "rejected", "ambiguous"}
)
_TRANSLATION_LOCALES = frozenset({"zh-CN"})
_QUICK_BRIEF_FAILURE_STATES = frozenset({"unavailable", "failed"})


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
    analysis_direct_value: float | None
    pm_target_weight: float | None
    analysis_direct_target_weight: float | None
    pm_state: str | None
    analysis_direct_state: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "time", _require_non_blank(self.time, "time"))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "time": self.time,
            "price": self.price,
            "pm_pipeline_value": self.pm_pipeline_value,
            "buy_hold_value": self.buy_hold_value,
            "analysis_direct_value": self.analysis_direct_value,
            "pm_target_weight": self.pm_target_weight,
            "analysis_direct_target_weight": self.analysis_direct_target_weight,
            "pm_state": self.pm_state,
            "analysis_direct_state": self.analysis_direct_state,
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
    pm_decision_id: str | None = None
    execution_record_id: str | None = None
    analysis_assessment_id: str | None = None

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
        for field_name in (
            "pm_decision_id",
            "execution_record_id",
            "analysis_assessment_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _require_non_blank(value, field_name))
        if self.kind == "pm_decision":
            if self.lane != "pm" or self.pm_decision_id is None or self.execution_record_id is None:
                raise PMConsoleSchemaError(
                    "PM markers must include PM lane, pm_decision_id, and execution_record_id."
                )
        elif self.kind == "analysis_assessment":
            if self.lane != "analysis" or self.analysis_assessment_id is None:
                raise PMConsoleSchemaError(
                    "Analysis markers must include the analysis lane and assessment id."
                )
        else:
            raise PMConsoleSchemaError("Position markers must represent PM or Analysis events.")

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
            "pm_decision_id": self.pm_decision_id,
            "execution_record_id": self.execution_record_id,
            "analysis_assessment_id": self.analysis_assessment_id,
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
class PMConsoleReferenceDTO:
    reference_id: str | None
    status: PMConsoleStatusDTO
    href: str | None = None

    def __post_init__(self) -> None:
        if self.reference_id is not None:
            object.__setattr__(
                self,
                "reference_id",
                _require_non_blank(self.reference_id, "reference_id"),
            )
        if not isinstance(self.status, PMConsoleStatusDTO):
            raise PMConsoleSchemaError("status must be a PMConsoleStatusDTO.")
        if self.href is not None:
            object.__setattr__(self, "href", _require_non_blank(self.href, "href"))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "reference_id": self.reference_id,
            "status": self.status.to_json_payload(),
            "href": self.href,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleAnalysisAssessmentDTO:
    assessment_id: str
    business_at: str
    as_if_flat_state: str
    target_weight: float
    as_if_flat_rationale_md: str
    source_event_ids: tuple[str, ...]
    if_flat_implication_md: str | None = None
    if_already_long_implication_md: str | None = None
    if_already_short_implication_md: str | None = None
    market_setup_dashboard_md: str | None = None
    missing_evidence_md: str | None = None
    key_claim_ids: tuple[str, ...] = ()
    contested_prior_claim_ids: tuple[str, ...] = ()
    pm_review_reasons: tuple[str, ...] = ()
    confidence: str | None = None
    thesis_anchor: PMConsoleReferenceDTO | None = None

    def to_json_payload(self) -> dict[str, object]:
        return {
            "assessment_id": self.assessment_id,
            "business_at": self.business_at,
            "as_if_flat_state": self.as_if_flat_state,
            "target_weight": self.target_weight,
            "as_if_flat_rationale_md": self.as_if_flat_rationale_md,
            "source_event_ids": list(self.source_event_ids),
            "if_flat_implication_md": self.if_flat_implication_md,
            "if_already_long_implication_md": self.if_already_long_implication_md,
            "if_already_short_implication_md": self.if_already_short_implication_md,
            "market_setup_dashboard_md": self.market_setup_dashboard_md,
            "missing_evidence_md": self.missing_evidence_md,
            "key_claim_ids": list(self.key_claim_ids),
            "contested_prior_claim_ids": list(self.contested_prior_claim_ids),
            "pm_review_reasons": list(self.pm_review_reasons),
            "confidence": self.confidence,
            "thesis_anchor": (
                None if self.thesis_anchor is None else self.thesis_anchor.to_json_payload()
            ),
        }


@dataclass(frozen=True, slots=True)
class PMConsolePMDecisionSummaryDTO:
    decision_id: str
    decision_episode_id: str
    target_key: str
    business_at: str
    decision_available_at: str
    actual_state_before_decision: str
    actual_target_weight_before_decision: float
    requested_state: str
    requested_target_weight: float
    execution_required: bool
    outcome_status: str
    pm_review_request_id: str | None
    review_reasons: tuple[str, ...] = ()
    analysis_conditional_state: str | None = None
    analysis_conditional_target_weight: float | None = None

    def to_json_payload(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "decision_episode_id": self.decision_episode_id,
            "target_key": self.target_key,
            "business_at": self.business_at,
            "decision_available_at": self.decision_available_at,
            "actual_state_before_decision": self.actual_state_before_decision,
            "actual_target_weight_before_decision": self.actual_target_weight_before_decision,
            "requested_state": self.requested_state,
            "requested_target_weight": self.requested_target_weight,
            "execution_required": self.execution_required,
            "outcome_status": self.outcome_status,
            "pm_review_request_id": self.pm_review_request_id,
            "review_reasons": list(self.review_reasons),
            "analysis_conditional_state": self.analysis_conditional_state,
            "analysis_conditional_target_weight": self.analysis_conditional_target_weight,
        }


@dataclass(frozen=True, slots=True)
class PMConsolePMDecisionDetailDTO:
    summary: PMConsolePMDecisionSummaryDTO
    review_source: str | None
    review_reasons: tuple[str, ...]
    rationale_md: str
    fallback_state_if_clamped: str | None
    fallback_rationale_md: str | None
    source_event_ids: tuple[str, ...]
    review_request: PMConsoleReferenceDTO
    execution: PMConsoleExecutionDTO | None
    execution_status: PMConsoleStatusDTO
    analysis: PMConsoleReferenceDTO
    analysis_assessment: PMConsoleAnalysisAssessmentDTO | None
    thesis_revision: PMConsoleReferenceDTO

    def to_json_payload(self) -> dict[str, object]:
        return {
            "summary": self.summary.to_json_payload(),
            "review_source": self.review_source,
            "review_reasons": list(self.review_reasons),
            "rationale_md": self.rationale_md,
            "fallback_state_if_clamped": self.fallback_state_if_clamped,
            "fallback_rationale_md": self.fallback_rationale_md,
            "source_event_ids": list(self.source_event_ids),
            "review_request": self.review_request.to_json_payload(),
            "execution": None if self.execution is None else self.execution.to_json_payload(),
            "execution_status": self.execution_status.to_json_payload(),
            "analysis": self.analysis.to_json_payload(),
            "analysis_assessment": (
                None
                if self.analysis_assessment is None
                else self.analysis_assessment.to_json_payload()
            ),
            "thesis_revision": self.thesis_revision.to_json_payload(),
        }


@dataclass(frozen=True, slots=True)
class PMConsolePMDecisionsResponseDTO:
    generated_at: str
    target_key: str
    decisions: tuple[PMConsolePMDecisionSummaryDTO, ...]

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "target_key": self.target_key,
            "decisions": [decision.to_json_payload() for decision in self.decisions],
        }


@dataclass(frozen=True, slots=True)
class PMConsolePMDecisionDetailResponseDTO:
    generated_at: str
    target_key: str
    decision: PMConsolePMDecisionDetailDTO

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "target_key": self.target_key,
            "decision": self.decision.to_json_payload(),
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
    adjusted_price: float | None = None
    rejection_reason: str | None = None

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
            "adjusted_price": self.adjusted_price,
            "rejection_reason": self.rejection_reason,
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
        if not self.available and (self.reason_code is None or self.reason_message is None):
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
    linked_pm_decisions: tuple[PMConsolePMDecisionSummaryDTO, ...] = ()

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
        for decision in self.linked_pm_decisions:
            if not isinstance(decision, PMConsolePMDecisionSummaryDTO):
                raise PMConsoleSchemaError(
                    "linked_pm_decisions must contain PMConsolePMDecisionSummaryDTO."
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
            "research_memory_write_receipt_ids": list(self.research_memory_write_receipt_ids),
            "changed_claim_ids": list(self.changed_claim_ids),
            "key_claim_ids": list(self.key_claim_ids),
            "contested_prior_claim_ids": list(self.contested_prior_claim_ids),
            "price_level_role_ids": list(self.price_level_role_ids),
            "price_levels": [level.to_json_payload() for level in self.price_levels],
            "previous_revision_id": self.previous_revision_id,
            "canonical_bundle_sha256": self.canonical_bundle_sha256,
            "is_baseline": self.is_baseline,
            "sections": [section.to_json_payload() for section in self.sections],
            "diffs_from_previous": [diff.to_json_payload() for diff in self.diffs_from_previous],
            "linked_pm_decisions": [
                decision.to_json_payload() for decision in self.linked_pm_decisions
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
                None if self.latest_revision is None else self.latest_revision.to_json_payload()
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
                raise PMConsoleSchemaError("sections must contain PMConsoleTranslatedSectionDTO.")
        if not isinstance(self.translator, PMConsoleThesisTranslationTranslatorDTO):
            raise PMConsoleSchemaError(
                "translator must be a PMConsoleThesisTranslationTranslatorDTO."
            )
        if not isinstance(self.verification, PMConsoleThesisTranslationVerificationDTO):
            raise PMConsoleSchemaError(
                "verification must be a PMConsoleThesisTranslationVerificationDTO."
            )
        if not self.verification.passed:
            raise PMConsoleSchemaError("translation success response requires passed verification.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "target_key": self.target_key,
            "revision_id": self.revision_id,
            "locale": self.locale,
            "canonical_bundle_sha256": self.canonical_bundle_sha256,
            "sections": [section.to_json_payload() for section in self.sections],
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
            "translator": (None if self.translator is None else self.translator.to_json_payload()),
            "verification": (
                None if self.verification is None else self.verification.to_json_payload()
            ),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisQuickBriefRequestDTO:
    canonical_bundle_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_bundle_sha256",
            _require_non_blank(self.canonical_bundle_sha256, "canonical_bundle_sha256"),
        )


@dataclass(frozen=True, slots=True)
class PMConsoleThesisQuickBriefResponseDTO:
    generated_at: str
    target_key: str
    revision_id: str
    canonical_bundle_sha256: str
    brief_md: str
    provider: str
    model: str
    prompt_version: str

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "target_key": self.target_key,
            "revision_id": self.revision_id,
            "canonical_bundle_sha256": self.canonical_bundle_sha256,
            "brief_md": self.brief_md,
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleThesisQuickBriefFailureDTO:
    generated_at: str
    target_key: str
    revision_id: str
    canonical_bundle_sha256: str
    error_code: str
    error_message: str
    state: str = "failed"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "state",
            _require_allowed(
                self.state,
                "state",
                _QUICK_BRIEF_FAILURE_STATES,
            ),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "target_key": self.target_key,
            "revision_id": self.revision_id,
            "canonical_bundle_sha256": self.canonical_bundle_sha256,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "state": self.state,
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
            "notes": list(self.notes),
            "audit": None if self.audit is None else self.audit.to_json_payload(),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleOverviewTargetDTO:
    target_key: str
    runtime_mode: str
    workspace_root: str
    workspace_name: str
    implemented_views: tuple[str, ...]
    default_view: str
    readiness: PMConsoleStatusDTO
    problem_codes: tuple[str, ...]
    current_portfolio_state: PMConsoleCurrentPortfolioStateDTO | None
    latest_pm_decision: PMConsolePMDecisionSummaryDTO | None
    latest_execution: PMConsoleExecutionDTO | None
    execution_status: PMConsoleStatusDTO
    latest_thesis_revision: PMConsoleThesisRevisionSummaryDTO | None
    operator_status: PMConsoleStatusDTO

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_key", _require_non_blank(self.target_key, "target_key"))
        object.__setattr__(
            self,
            "runtime_mode",
            _require_allowed(self.runtime_mode, "runtime_mode", _RUNTIME_MODES),
        )
        for field_name in ("workspace_root", "workspace_name"):
            object.__setattr__(
                self,
                field_name,
                _require_non_blank(getattr(self, field_name), field_name),
            )
        views = tuple(
            _require_allowed(view, "implemented_views", _TARGET_VIEWS)
            for view in self.implemented_views
        )
        if not views:
            raise PMConsoleSchemaError("implemented_views must not be empty.")
        object.__setattr__(self, "implemented_views", views)
        default_view = _require_allowed(self.default_view, "default_view", _TARGET_VIEWS)
        if default_view not in views:
            raise PMConsoleSchemaError("default_view must be included in implemented_views.")
        object.__setattr__(self, "default_view", default_view)
        if not isinstance(self.readiness, PMConsoleStatusDTO):
            raise PMConsoleSchemaError("readiness must be a PMConsoleStatusDTO.")
        if self.readiness.code not in _OVERVIEW_READINESS_STATES:
            raise PMConsoleSchemaError("readiness code is not supported by the overview contract.")
        if not isinstance(self.problem_codes, tuple):
            raise PMConsoleSchemaError("problem_codes must be a tuple.")
        for code in self.problem_codes:
            _require_non_blank(code, "problem_code")
        if self.current_portfolio_state is not None and not isinstance(
            self.current_portfolio_state, PMConsoleCurrentPortfolioStateDTO
        ):
            raise PMConsoleSchemaError(
                "current_portfolio_state must be a PMConsoleCurrentPortfolioStateDTO when set."
            )
        if self.latest_pm_decision is not None and not isinstance(
            self.latest_pm_decision, PMConsolePMDecisionSummaryDTO
        ):
            raise PMConsoleSchemaError(
                "latest_pm_decision must be a PMConsolePMDecisionSummaryDTO when set."
            )
        if self.latest_execution is not None and not isinstance(
            self.latest_execution, PMConsoleExecutionDTO
        ):
            raise PMConsoleSchemaError(
                "latest_execution must be a PMConsoleExecutionDTO when set."
            )
        if not isinstance(self.execution_status, PMConsoleStatusDTO):
            raise PMConsoleSchemaError("execution_status must be a PMConsoleStatusDTO.")
        if self.execution_status.code not in _OVERVIEW_EXECUTION_STATES:
            raise PMConsoleSchemaError(
                "execution_status code is not supported by the overview contract."
            )
        if self.latest_thesis_revision is not None and not isinstance(
            self.latest_thesis_revision, PMConsoleThesisRevisionSummaryDTO
        ):
            raise PMConsoleSchemaError(
                "latest_thesis_revision must be a PMConsoleThesisRevisionSummaryDTO when set."
            )
        if not isinstance(self.operator_status, PMConsoleStatusDTO):
            raise PMConsoleSchemaError("operator_status must be a PMConsoleStatusDTO.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "runtime_mode": self.runtime_mode,
            "workspace_root": self.workspace_root,
            "workspace_name": self.workspace_name,
            "implemented_views": list(self.implemented_views),
            "default_view": self.default_view,
            "readiness": self.readiness.to_json_payload(),
            "problem_codes": list(self.problem_codes),
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
                None if self.latest_execution is None else self.latest_execution.to_json_payload()
            ),
            "execution_status": self.execution_status.to_json_payload(),
            "latest_thesis_revision": (
                None
                if self.latest_thesis_revision is None
                else self.latest_thesis_revision.to_json_payload()
            ),
            "operator_status": self.operator_status.to_json_payload(),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleOverviewResponseDTO:
    generated_at: str
    mounted_target_count: int
    targets: tuple[PMConsoleOverviewTargetDTO, ...]
    reload_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_at",
            _require_non_blank(self.generated_at, "generated_at"),
        )
        if isinstance(self.mounted_target_count, bool) or not isinstance(
            self.mounted_target_count, int
        ) or self.mounted_target_count < 0:
            raise PMConsoleSchemaError("mounted_target_count must be a non-negative integer.")
        for target in self.targets:
            if not isinstance(target, PMConsoleOverviewTargetDTO):
                raise PMConsoleSchemaError(
                    "targets must contain PMConsoleOverviewTargetDTO instances."
                )
        if self.reload_error is not None:
            object.__setattr__(
                self,
                "reload_error",
                _require_non_blank(self.reload_error, "reload_error"),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "mounted_target_count": self.mounted_target_count,
            "targets": [target.to_json_payload() for target in self.targets],
            "reload_error": self.reload_error,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleEpisodeSummaryDTO:
    episode_id: str
    target_key: str
    phase: str
    direction: str
    opened_at: str
    closed_at: str | None
    close_reason: str | None
    segment_count: int
    outcome_status: PMConsoleStatusDTO
    strategy_return: float | None
    reflection_status: PMConsoleStatusDTO

    def to_json_payload(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "phase": self.phase,
            "direction": self.direction,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "close_reason": self.close_reason,
            "segment_count": self.segment_count,
            "outcome_status": self.outcome_status.to_json_payload(),
            "strategy_return": self.strategy_return,
            "reflection_status": self.reflection_status.to_json_payload(),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleEpisodeSegmentDTO:
    segment_id: str
    target_weight: float
    opened_at: str
    closed_at: str | None
    close_reason: str | None
    strategy_return: float | None
    outcome_status: PMConsoleStatusDTO

    def to_json_payload(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "target_weight": self.target_weight,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "close_reason": self.close_reason,
            "strategy_return": self.strategy_return,
            "outcome_status": self.outcome_status.to_json_payload(),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleEpisodeTransitionDTO:
    state_change_id: str
    occurred_at: str
    kind: str
    state: str
    previous_target_weight: float | None
    target_weight: float
    pm_decision_id: str | None
    execution_record_id: str | None
    adjusted_price: float | None
    raw_price: float | None
    total_cost_bps: float | None
    price_basis: str | None
    rationale_md: str

    def to_json_payload(self) -> dict[str, object]:
        return {
            "state_change_id": self.state_change_id,
            "occurred_at": self.occurred_at,
            "kind": self.kind,
            "state": self.state,
            "previous_target_weight": self.previous_target_weight,
            "target_weight": self.target_weight,
            "pm_decision_id": self.pm_decision_id,
            "execution_record_id": self.execution_record_id,
            "adjusted_price": self.adjusted_price,
            "raw_price": self.raw_price,
            "total_cost_bps": self.total_cost_bps,
            "price_basis": self.price_basis,
            "rationale_md": self.rationale_md,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleEpisodeReflectionDTO:
    status: PMConsoleStatusDTO
    obligation_count: int
    learning_recorded: bool

    def to_json_payload(self) -> dict[str, object]:
        return {
            "status": self.status.to_json_payload(),
            "obligation_count": self.obligation_count,
            "learning_recorded": self.learning_recorded,
        }


@dataclass(frozen=True, slots=True)
class PMConsoleEpisodeDetailDTO:
    summary: PMConsoleEpisodeSummaryDTO
    segments: tuple[PMConsoleEpisodeSegmentDTO, ...]
    transitions: tuple[PMConsoleEpisodeTransitionDTO, ...]
    reflection: PMConsoleEpisodeReflectionDTO

    def to_json_payload(self) -> dict[str, object]:
        return {
            "summary": self.summary.to_json_payload(),
            "segments": [segment.to_json_payload() for segment in self.segments],
            "transitions": [transition.to_json_payload() for transition in self.transitions],
            "reflection": self.reflection.to_json_payload(),
        }


@dataclass(frozen=True, slots=True)
class PMConsoleEpisodesResponseDTO:
    generated_at: str
    target_key: str
    total: int
    offset: int
    limit: int
    episodes: tuple[PMConsoleEpisodeSummaryDTO, ...]

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "target_key": self.target_key,
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "episodes": [episode.to_json_payload() for episode in self.episodes],
        }


@dataclass(frozen=True, slots=True)
class PMConsoleEpisodeDetailResponseDTO:
    generated_at: str
    target_key: str
    episode: PMConsoleEpisodeDetailDTO

    def to_json_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "target_key": self.target_key,
            "episode": self.episode.to_json_payload(),
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
    "PMConsoleAnalysisAssessmentDTO",
    "PMConsoleMarkdownSectionDTO",
    "PMConsoleAuditDTO",
    "PMConsoleContextResponseDTO",
    "PMConsoleCurrentPortfolioStateDTO",
    "PMConsoleDecisionDTO",
    "PMConsolePMDecisionDetailDTO",
    "PMConsolePMDecisionDetailResponseDTO",
    "PMConsolePMDecisionSummaryDTO",
    "PMConsolePMDecisionsResponseDTO",
    "PMConsoleExecutionDTO",
    "PMConsoleLineDTO",
    "PMConsoleMarkerDTO",
    "PMConsolePointDTO",
    "PMConsoleReferenceDTO",
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
    "PMConsoleThesisQuickBriefFailureDTO",
    "PMConsoleThesisQuickBriefRequestDTO",
    "PMConsoleThesisQuickBriefResponseDTO",
    "PMConsoleTargetsResponseDTO",
    "PMConsoleOverviewResponseDTO",
    "PMConsoleOverviewTargetDTO",
    "PMConsoleEpisodeDetailDTO",
    "PMConsoleEpisodeDetailResponseDTO",
    "PMConsoleEpisodeReflectionDTO",
    "PMConsoleEpisodeSegmentDTO",
    "PMConsoleEpisodeSummaryDTO",
    "PMConsoleEpisodeTransitionDTO",
    "PMConsoleEpisodesResponseDTO",
    "_isoformat",
]
