"""Resolve runtime PMReview requests from persisted analysis outcomes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from event_trader.execution.store import ExecutionRecordStore
from event_trader.pm_review import (
    PMReviewDispatchConsiderationStore,
    PMReviewFailureStore,
    PMReviewRequest,
    PMReviewRequestStore,
    is_terminal_pm_review_policy_skip_reason,
)
from event_trader.pm_review.contracts import parse_pm_review_request
from event_trader.pm_review.store import PMReviewStoreError
from event_trader.portfolio import PMDecisionStore
from event_trader.portfolio.active_exposure import (
    ActiveExposureResolverError,
    resolve_active_exposure,
)
from event_trader.portfolio.contracts import PMDecision
from event_trader.portfolio.store import PortfolioStoreError
from event_trader.storage import WorkspaceLayout

from .contracts import AnalysisOutcome
from .pm_review_queue import PMReviewWorkItem


class RuntimePMReviewResolutionError(ValueError):
    """Raised when runtime PMReview refs cannot be resolved from durable state."""


type PMReviewEngineResolutionStatus = Literal["requested", "skipped"]


@dataclass(frozen=True, slots=True)
class RuntimePMReviewEngineResolution:
    """Deterministic PMReview routing outcome for one AnalysisOutcome."""

    status: PMReviewEngineResolutionStatus
    target_key: str
    analysis_unit_id: str
    record_id: str
    outcome_record_ref: str
    pm_review_request_id: str | None = None
    pm_review_request_ref: str | None = None
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"requested", "skipped"}:
            raise RuntimePMReviewResolutionError("status must be requested or skipped.")
        for field_name in ("target_key", "analysis_unit_id", "record_id", "outcome_record_ref"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise RuntimePMReviewResolutionError(f"{field_name} must be a non-blank string.")
        if self.status == "requested":
            if (
                not isinstance(self.pm_review_request_id, str)
                or not self.pm_review_request_id.strip()
            ):
                raise RuntimePMReviewResolutionError(
                    "requested resolution requires pm_review_request_id."
                )
            if (
                not isinstance(self.pm_review_request_ref, str)
                or not self.pm_review_request_ref.strip()
            ):
                raise RuntimePMReviewResolutionError(
                    "requested resolution requires pm_review_request_ref."
                )
            if self.skip_reason is not None:
                raise RuntimePMReviewResolutionError(
                    "requested resolution must not carry skip_reason."
                )
        if self.status == "skipped":
            if not isinstance(self.skip_reason, str) or not self.skip_reason.strip():
                raise RuntimePMReviewResolutionError("skipped resolution requires skip_reason.")


@dataclass(frozen=True, slots=True)
class RuntimePMReviewResolution:
    """Canonical PMReview input rebuilt from persisted request state."""

    work_item: PMReviewWorkItem
    request: PMReviewRequest
    existing_pm_decision_ref: str | None = None
    pending_execution_decision: PMDecision | None = None
    existing_failure_record_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.work_item, PMReviewWorkItem):
            raise RuntimePMReviewResolutionError("work_item must be a PMReviewWorkItem.")
        if not isinstance(self.request, PMReviewRequest):
            raise RuntimePMReviewResolutionError("request must be a PMReviewRequest.")
        if self.request.request_id != self.work_item.pm_review_request_id:
            raise RuntimePMReviewResolutionError("request_id must match work_item.")
        if self.request.target_key != self.work_item.target_key:
            raise RuntimePMReviewResolutionError("request target_key must match work_item.")
        if (
            self.existing_pm_decision_ref is not None
            and self.existing_failure_record_ref is not None
        ):
            raise RuntimePMReviewResolutionError(
                "resolution must not carry both existing decision and failure refs."
            )
        if (
            self.pending_execution_decision is not None
            and (
                self.existing_pm_decision_ref is not None
                or self.existing_failure_record_ref is not None
            )
        ):
            raise RuntimePMReviewResolutionError(
                "pending execution decision must not carry terminal lifecycle refs."
            )
        if (
            self.pending_execution_decision is not None
            and self.pending_execution_decision.target_key != self.work_item.target_key
        ):
            raise RuntimePMReviewResolutionError(
                "pending execution decision target_key must match work_item."
            )


class RuntimePMReviewRequestResolver:
    """Reload persisted PMReview requests and lifecycle state for runtime workers."""

    def __init__(
        self,
        *,
        layout: WorkspaceLayout,
        migration_id: str,
    ) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise RuntimePMReviewResolutionError("layout must be a WorkspaceLayout instance.")
        if not isinstance(migration_id, str) or not migration_id.strip():
            raise RuntimePMReviewResolutionError("migration_id must be a non-blank string.")
        self._layout = layout
        self._migration_id = migration_id

    def resolve_analysis_outcome(
        self,
        message: AnalysisOutcome,
    ) -> RuntimePMReviewEngineResolution:
        if not isinstance(message, AnalysisOutcome):
            raise RuntimePMReviewResolutionError("message must be an AnalysisOutcome.")
        outcome_payload = _read_analysis_outcome_payload(
            message.outcome_record_ref,
            current_workspace_root=self._layout.root,
        )
        request, request_ref = self._request_from_outcome_payload(
            target_key=message.target_key,
            outcome_payload=outcome_payload,
        )
        if request is None or request_ref is None:
            return RuntimePMReviewEngineResolution(
                status="skipped",
                target_key=message.target_key,
                analysis_unit_id=message.analysis_unit_id,
                record_id=message.record_id,
                outcome_record_ref=message.outcome_record_ref,
                skip_reason="analysis_outcome_without_pm_review_request",
            )
        if self._has_terminal_skip(request=request):
            return RuntimePMReviewEngineResolution(
                status="skipped",
                target_key=message.target_key,
                analysis_unit_id=message.analysis_unit_id,
                record_id=message.record_id,
                outcome_record_ref=message.outcome_record_ref,
                pm_review_request_id=request.request_id,
                pm_review_request_ref=request_ref,
                skip_reason="pm_review_terminal_policy_skip_exists",
            )
        if self._should_skip_flat_exposure(request=request):
            return RuntimePMReviewEngineResolution(
                status="skipped",
                target_key=message.target_key,
                analysis_unit_id=message.analysis_unit_id,
                record_id=message.record_id,
                outcome_record_ref=message.outcome_record_ref,
                pm_review_request_id=request.request_id,
                pm_review_request_ref=request_ref,
                skip_reason="analysis_event_current_exposure_flat",
            )
        return RuntimePMReviewEngineResolution(
            status="requested",
            target_key=message.target_key,
            analysis_unit_id=message.analysis_unit_id,
            record_id=message.record_id,
            outcome_record_ref=message.outcome_record_ref,
            pm_review_request_id=request.request_id,
            pm_review_request_ref=request_ref,
        )

    def resolve_work_item(self, work_item: PMReviewWorkItem) -> RuntimePMReviewResolution:
        if not isinstance(work_item, PMReviewWorkItem):
            raise RuntimePMReviewResolutionError("work_item must be a PMReviewWorkItem.")
        request = self._request_from_request_ref(work_item.pm_review_request_ref)
        existing_pm_decision = self._latest_pm_decision(
            target_key=work_item.target_key,
            request_id=work_item.pm_review_request_id,
        )
        existing_pm_decision_ref = None
        pending_execution_decision = None
        if existing_pm_decision is not None:
            decision, decision_ref = existing_pm_decision
            if (
                not decision.execution_required
                or self._has_terminal_execution(decision=decision)
            ):
                existing_pm_decision_ref = decision_ref
            else:
                pending_execution_decision = decision
        existing_failure_record_ref = None
        if existing_pm_decision is None:
            existing_failure_record_ref = self._latest_terminal_failure_ref(
                target_key=work_item.target_key,
                request_id=work_item.pm_review_request_id,
            )
        return RuntimePMReviewResolution(
            work_item=work_item,
            request=request,
            existing_pm_decision_ref=existing_pm_decision_ref,
            pending_execution_decision=pending_execution_decision,
            existing_failure_record_ref=existing_failure_record_ref,
        )

    def _request_from_outcome_payload(
        self,
        *,
        target_key: str,
        outcome_payload: dict[str, object],
    ) -> tuple[PMReviewRequest | None, str | None]:
        request_payload = outcome_payload.get("pm_review_request")
        request_id = _optional_text(outcome_payload.get("pm_review_request_id"))
        request_path_value = _optional_text(outcome_payload.get("pm_review_request_path"))
        if request_payload is None and request_id is None:
            return None, None
        if request_payload is not None:
            if not isinstance(request_payload, dict):
                raise RuntimePMReviewResolutionError(
                    "embedded pm_review_request must be an object."
                )
            try:
                request = parse_pm_review_request(request_payload)
            except Exception as exc:
                raise RuntimePMReviewResolutionError(
                    "embedded pm_review_request is invalid."
                ) from exc
            request_ref = self._request_ref_from_request(
                request=request,
                request_path_value=request_path_value,
            )
            return request, request_ref
        if request_id is None:
            return None, None
        request = self._request_from_store(target_key=target_key, request_id=request_id)
        return request, self._request_ref_from_request(
            request=request,
            request_path_value=request_path_value,
        )

    def _request_ref_from_request(
        self,
        *,
        request: PMReviewRequest,
        request_path_value: str | None,
    ) -> str:
        if request_path_value is None:
            request_path = PMReviewRequestStore(self._layout).path_for(
                request.target_key,
                request.business_at,
            )
        else:
            request_path = Path(request_path_value)
        return f"{request_path.resolve(strict=False)}#{request.request_id}"

    def _request_from_request_ref(self, request_ref: str) -> PMReviewRequest:
        path, request_id = _parse_record_ref(
            request_ref,
            current_workspace_root=self._layout.root,
        )
        return _read_pm_review_request_from_path(path=path, request_id=request_id)

    def _request_from_store(self, *, target_key: str, request_id: str) -> PMReviewRequest:
        try:
            matches = tuple(
                persisted.record
                for persisted in PMReviewRequestStore(self._layout).read_records(
                    target_key=target_key
                )
                if persisted.record.request_id == request_id
            )
        except PMReviewStoreError as exc:
            raise RuntimePMReviewResolutionError(str(exc)) from exc
        if not matches:
            raise RuntimePMReviewResolutionError(f"PMReviewRequest not found: {request_id}")
        if len(matches) > 1:
            raise RuntimePMReviewResolutionError(f"PMReviewRequest id is ambiguous: {request_id}")
        return matches[0]

    def _latest_pm_decision(
        self,
        *,
        target_key: str,
        request_id: str,
    ) -> tuple[PMDecision, str] | None:
        try:
            matches = tuple(
                persisted
                for persisted in PMDecisionStore(self._layout).read_records(target_key=target_key)
                if persisted.record.pm_review_request_id == request_id
            )
        except PortfolioStoreError as exc:
            raise RuntimePMReviewResolutionError(str(exc)) from exc
        if not matches:
            return None
        persisted = sorted(
            matches,
            key=lambda item: (
                item.record.business_at,
                item.path.as_posix(),
                item.line_number,
            ),
        )[-1]
        return (
            persisted.record,
            f"{persisted.path.resolve(strict=False)}#{persisted.record.decision_id}",
        )

    def _has_terminal_execution(self, *, decision: PMDecision) -> bool:
        return any(
            persisted.record.pm_decision_id == decision.decision_id
            and persisted.record.status in {"executed", "rejected"}
            for persisted in ExecutionRecordStore(self._layout).read_records(
                target_key=decision.target_key
            )
        )

    def _latest_terminal_failure_ref(self, *, target_key: str, request_id: str) -> str | None:
        try:
            matches = tuple(
                persisted
                for persisted in PMReviewFailureStore(self._layout).read_records(
                    target_key=target_key
                )
                if persisted.record.pm_review_request_id == request_id
                and persisted.record.retry_allowed is False
            )
        except PMReviewStoreError as exc:
            raise RuntimePMReviewResolutionError(str(exc)) from exc
        if not matches:
            return None
        persisted = sorted(
            matches,
            key=lambda item: (
                item.record.failed_at,
                item.path.as_posix(),
                item.line_number,
            ),
        )[-1]
        return f"{persisted.path.resolve(strict=False)}#{persisted.record.failure_id}"

    def _has_terminal_skip(self, *, request: PMReviewRequest) -> bool:
        try:
            matches = tuple(
                persisted
                for persisted in PMReviewDispatchConsiderationStore(self._layout).read_records(
                    target_key=request.target_key
                )
                if persisted.record.request_id == request.request_id
                and persisted.record.outcome == "skipped"
                and is_terminal_pm_review_policy_skip_reason(persisted.record.skip_reason)
            )
        except PMReviewStoreError as exc:
            raise RuntimePMReviewResolutionError(str(exc)) from exc
        return bool(matches)

    def _should_skip_flat_exposure(self, *, request: PMReviewRequest) -> bool:
        if request.source != "analysis_event" or request.current_exposure_required is not True:
            return False
        try:
            active_exposure = resolve_active_exposure(
                layout=self._layout,
                target_key=request.target_key,
                migration_id=self._migration_id,
            )
        except ActiveExposureResolverError:
            return False
        return active_exposure.state == "flat"


def _parse_record_ref(
    record_ref: str,
    *,
    current_workspace_root: Path | None = None,
) -> tuple[Path, str]:
    normalized = _optional_text(record_ref)
    if normalized is None or "#" not in normalized:
        raise RuntimePMReviewResolutionError("record ref must be '<path>#<record_id>'.")
    path_text, record_id = normalized.rsplit("#", maxsplit=1)
    if not path_text.strip() or not record_id.strip():
        raise RuntimePMReviewResolutionError("record ref must include path and record_id.")
    path = Path(path_text)
    if current_workspace_root is not None:
        path = _rebase_workspace_path(path, current_workspace_root=current_workspace_root)
    return path, record_id


def _read_analysis_outcome_payload(
    outcome_record_ref: str,
    *,
    current_workspace_root: Path | None = None,
) -> dict[str, object]:
    path, record_id = _parse_record_ref(
        outcome_record_ref,
        current_workspace_root=current_workspace_root,
    )
    if not path.exists():
        raise RuntimePMReviewResolutionError(f"analysis outcome path does not exist: {path}")
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimePMReviewResolutionError(
            f"analysis outcome path is unreadable: {path}"
        ) from exc
    matched: dict[str, object] | None = None
    for raw_line in raw_lines:
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise RuntimePMReviewResolutionError(
                f"analysis outcome receipt is not valid JSON: {path}"
            ) from exc
        if not isinstance(payload, dict):
            continue
        if payload.get("record_id") != record_id:
            continue
        matched = payload
    if matched is None:
        raise RuntimePMReviewResolutionError(
            f"analysis outcome record not found: {outcome_record_ref}"
        )
    return matched


def _read_pm_review_request_from_path(*, path: Path, request_id: str) -> PMReviewRequest:
    if not path.exists():
        raise RuntimePMReviewResolutionError(f"PMReviewRequest path does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    raise RuntimePMReviewResolutionError(
                        f"PMReviewRequest path is invalid JSON: {path}"
                    ) from exc
                if not isinstance(payload, dict):
                    continue
                if payload.get("request_id") != request_id:
                    continue
                try:
                    return parse_pm_review_request(payload)
                except Exception as exc:
                    raise RuntimePMReviewResolutionError(
                        f"PMReviewRequest payload is invalid: {path}"
                    ) from exc
    except OSError as exc:
        raise RuntimePMReviewResolutionError(f"PMReviewRequest path is unreadable: {path}") from exc
    raise RuntimePMReviewResolutionError(f"PMReviewRequest not found in path: {request_id}")


def _rebase_workspace_path(path: Path, *, current_workspace_root: Path) -> Path:
    resolved_current_root = current_workspace_root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if resolved_path.exists():
        return resolved_path
    anchor = resolved_current_root.name
    if not anchor:
        return resolved_path
    parts = resolved_path.parts
    try:
        anchor_index = parts.index(anchor)
    except ValueError:
        return resolved_path
    suffix = parts[anchor_index + 1 :]
    rebased = resolved_current_root.joinpath(*suffix)
    return rebased.resolve(strict=False)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimePMReviewResolutionError("expected text value.")
    normalized = value.strip()
    if not normalized:
        raise RuntimePMReviewResolutionError("text value must not be blank.")
    return normalized


__all__ = [
    "RuntimePMReviewEngineResolution",
    "RuntimePMReviewRequestResolver",
    "RuntimePMReviewResolution",
    "RuntimePMReviewResolutionError",
]
