"""Deterministic active-chain price-basis cutover for populated workspaces."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from event_trader.analysis_assessment_store import (
    AnalysisAssessmentStore,
    AnalysisAssessmentStoreError,
    PersistedAnalysisAssessment,
)
from event_trader.config import KernelConfig, ValidationMarketMappingConfig
from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.analysis_direction_policy import (
    normalize_analysis_assessment_for_execution_direction_mode,
)
from event_trader.contracts.view_state_change import MarketMapping
from event_trader.market.adjustments import (
    apply_adjustment_policy_to_series,
    load_adjustment_sidecar_from_archive_root,
    normalize_archive_symbol,
    read_adjustment_sidecar_hash,
)
from event_trader.market.local_archive_provider import LocalArchiveMarketDataProvider
from event_trader.market.provider import MarketBarsProvider, build_default_market_bars_provider
from event_trader.market.stock_bar_adapter import parse_stock_bar_granularity
from event_trader.research_memory.active_price_projection import (
    ActivePriceProjectionSection,
    project_active_price_sections,
)
from event_trader.research_memory.analysis_price_basis_rebase import (
    AnalysisPriceBasisRebaseError,
    bootstrap_analysis_price_semantics_for_cutover,
    rebase_analysis_assessment,
)
from event_trader.research_memory.write_receipts import (
    FileBackedResearchMemoryWriteReceiptStore,
    ReceiptedResearchMemoryPageWriter,
    ResearchMemoryWriteAttribution,
    ResearchMemoryWriteReceipt,
    parse_research_memory_write_receipt,
)
from event_trader.pm_review.contracts import (
    analysis_snapshot_from_assessment,
    PMReviewContractError,
    PMReviewInput,
    PMReviewRequest,
)
from event_trader.pm_review.position_review import (
    PMPositionReviewError,
    build_pm_position_review_triggers,
)
from event_trader.pm_review.store import (
    PMPositionReviewTriggerStore,
    PMReviewRequestStore,
    PMReviewStoreError,
)
from event_trader.portfolio.contracts import PMDecision, PortfolioState
from event_trader.portfolio.store import (
    PMDecisionStore,
    PortfolioStateStore,
    PortfolioStoreError,
)
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.builder import ThesisRevisionBuildError, ThesisRevisionBuilder
from event_trader.thesis_revision.store import ThesisRevisionStore, ThesisRevisionStoreError

_RAW_POLICY = "raw"
_ACTOR_TYPE = "operator"
_BASIS_POINTER_DIR = "active_price_basis"


class ActivePriceBasisCutoverError(ValueError):
    """Raised when active-chain basis cutover cannot complete deterministically."""


@dataclass(frozen=True, slots=True)
class ActivePriceBasisPointer:
    basis_id: str
    policy: str
    anchor_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "basis_id", _required_text(self.basis_id, "basis_id"))
        object.__setattr__(self, "policy", _required_text(self.policy, "policy"))
        object.__setattr__(
            self,
            "anchor_at",
            validate_timestamp(
                self.anchor_at,
                field_name="anchor_at",
                error_type=ActivePriceBasisCutoverError,
            ),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "basis_id": self.basis_id,
            "policy": self.policy,
            "anchor_at": self.anchor_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ResolvedActivePriceBasis:
    basis_id: str
    policy: str
    anchor_at: datetime
    effective_factor: float
    archive_symbol: str
    sidecar_hash: str | None


@dataclass(frozen=True, slots=True)
class ActivePriceBasisCutoverResult:
    target_key: str
    cutover_applied: bool
    old_basis_id: str | None
    new_basis_id: str
    predecessor_assessment_id: str | None
    successor_assessment_id: str | None
    successor_revision_id: str | None
    pm_refresh_required: bool


def run_active_price_basis_cutover(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    cutover_at: datetime,
    market_mapping: ValidationMarketMappingConfig,
    local_archive_root: Path | None,
    market_data_provider: MarketBarsProvider | None = None,
    legacy_policy: str | None = None,
    require_rebase_ratio_change: bool = False,
) -> ActivePriceBasisCutoverResult:
    if not isinstance(layout, WorkspaceLayout):
        raise ActivePriceBasisCutoverError("layout must be a WorkspaceLayout instance.")
    normalized_target = validate_target_key(
        target_key,
        error_type=ActivePriceBasisCutoverError,
    )
    normalized_cutover_at = validate_timestamp(
        cutover_at,
        field_name="cutover_at",
        error_type=ActivePriceBasisCutoverError,
    )
    archive_root = None if local_archive_root is None else local_archive_root.resolve(strict=False)
    assessment_store = AnalysisAssessmentStore(layout)
    latest = _latest_assessment(assessment_store.read_records(target_key=normalized_target))
    current_policy = _policy_or_raw(market_mapping.adjustment_policy)
    current_basis = resolve_active_price_basis(
        market_mapping=market_mapping,
        local_archive_root=archive_root,
        policy=current_policy,
        anchor_at=normalized_cutover_at,
    )
    pointer = read_active_price_basis_pointer(layout=layout, target_key=normalized_target)
    if latest is None:
        write_active_price_basis_pointer(
            layout=layout,
            target_key=normalized_target,
            pointer=ActivePriceBasisPointer(
                basis_id=current_basis.basis_id,
                policy=current_policy,
                anchor_at=normalized_cutover_at,
            ),
        )
        return ActivePriceBasisCutoverResult(
            target_key=normalized_target,
            cutover_applied=False,
            old_basis_id=None if pointer is None else pointer.basis_id,
            new_basis_id=current_basis.basis_id,
            predecessor_assessment_id=None,
            successor_assessment_id=None,
            successor_revision_id=None,
            pm_refresh_required=False,
        )

    prior_assessment = latest.record
    force_pointerless_legacy_successor_upgrade = (
        pointer is None
        and legacy_policy is not None
        and prior_assessment.analysis_price_semantics is None
    )
    old_basis = _resolve_prior_basis(
        pointer=pointer,
        legacy_policy=legacy_policy,
        market_mapping=market_mapping,
        local_archive_root=archive_root,
        prior_assessment=prior_assessment,
    )
    ratio = _rebase_ratio(
        market_mapping=market_mapping,
        local_archive_root=archive_root,
        old_basis=old_basis,
        current_basis=current_basis,
    )
    if old_basis.basis_id == current_basis.basis_id and not force_pointerless_legacy_successor_upgrade:
        if pointer is None or pointer.basis_id != current_basis.basis_id:
            write_active_price_basis_pointer(
                layout=layout,
                target_key=normalized_target,
                pointer=ActivePriceBasisPointer(
                    basis_id=current_basis.basis_id,
                    policy=current_policy,
                    anchor_at=normalized_cutover_at,
                ),
            )
        return ActivePriceBasisCutoverResult(
            target_key=normalized_target,
            cutover_applied=False,
            old_basis_id=old_basis.basis_id,
            new_basis_id=current_basis.basis_id,
            predecessor_assessment_id=prior_assessment.assessment_id,
            successor_assessment_id=prior_assessment.assessment_id,
            successor_revision_id=None,
            pm_refresh_required=False,
        )
    if (
        require_rebase_ratio_change
        and abs(ratio - 1.0) <= 1e-12
        and not force_pointerless_legacy_successor_upgrade
    ):
        return ActivePriceBasisCutoverResult(
            target_key=normalized_target,
            cutover_applied=False,
            old_basis_id=old_basis.basis_id,
            new_basis_id=current_basis.basis_id,
            predecessor_assessment_id=prior_assessment.assessment_id,
            successor_assessment_id=prior_assessment.assessment_id,
            successor_revision_id=None,
            pm_refresh_required=False,
        )

    successor_assessment_id = _successor_assessment_id(
        predecessor_assessment_id=prior_assessment.assessment_id,
        old_basis_id=old_basis.basis_id,
        new_basis_id=current_basis.basis_id,
        cutover_at=normalized_cutover_at,
    )
    existing_successor = _find_assessment_by_id(
        assessment_store.read_records(target_key=normalized_target),
        assessment_id=successor_assessment_id,
    )
    if existing_successor is None:
        reference_price = _load_market_reference_price(
            target_key=normalized_target,
            market_mapping=market_mapping,
            local_archive_root=archive_root,
            market_data_provider=market_data_provider,
            cutover_at=normalized_cutover_at,
        )
        successor = rebase_analysis_assessment(
            prior_assessment,
            ratio=ratio,
            successor_assessment_id=successor_assessment_id,
            successor_business_at=normalized_cutover_at,
            successor_memory_write_receipt_ids=(),
        )
        if successor.analysis_price_semantics is None:
            successor = rebase_analysis_assessment(
                successor,
                ratio=1.0,
                successor_assessment_id=successor_assessment_id,
                successor_business_at=normalized_cutover_at,
                successor_analysis_price_semantics=bootstrap_analysis_price_semantics_for_cutover(
                    successor,
                    analysis_reference_price=reference_price,
                    analysis_reference_at=normalized_cutover_at,
                ),
                successor_memory_write_receipt_ids=(),
            )
    else:
        successor = existing_successor.record
    successor = normalize_analysis_assessment_for_execution_direction_mode(
        successor,
        execution_direction_mode=market_mapping.execution_direction_mode,
        error_type=ActivePriceBasisCutoverError,
    )

    projected_sections = project_active_price_sections(successor)
    if not projected_sections:
        raise ActivePriceBasisCutoverError(
            "active price basis cutover requires deterministic projected active sections."
        )
    dashboard_content = _required_projected_section_content(
        projected_sections,
        page_path=f"targets/{normalized_target}/thesis.md",
        section_name="Market Setup Dashboard",
    )
    successor = rebase_analysis_assessment(
        successor,
        ratio=1.0,
        successor_assessment_id=successor.assessment_id,
        successor_business_at=successor.business_at,
        successor_market_setup_dashboard_md=dashboard_content,
        successor_analysis_price_semantics=successor.analysis_price_semantics,
        successor_memory_write_receipt_ids=successor.memory_write_receipt_ids,
    )
    receipts = _persist_projected_sections(
        layout=layout,
        target_key=normalized_target,
        cutover_at=normalized_cutover_at,
        actor_id=successor.assessment_id,
        projected_sections=projected_sections,
    )
    receipt_ids = tuple(receipt.receipt_id for receipt in receipts)
    successor = rebase_analysis_assessment(
        successor,
        ratio=1.0,
        successor_assessment_id=successor.assessment_id,
        successor_business_at=successor.business_at,
        successor_market_setup_dashboard_md=dashboard_content,
        successor_analysis_price_semantics=successor.analysis_price_semantics,
        successor_memory_write_receipt_ids=receipt_ids,
    )
    try:
        assessment_store.append(successor)
    except AnalysisAssessmentStoreError as exc:
        raise ActivePriceBasisCutoverError(str(exc)) from exc
    revision = _append_successor_revision(
        layout=layout,
        target_key=normalized_target,
        cutover_at=normalized_cutover_at,
        successor=successor,
        receipts=receipts,
    )
    _refresh_active_pm_observation_chain(
        layout=layout,
        target_key=normalized_target,
        predecessor_assessment_id=prior_assessment.assessment_id,
        successor_assessment=successor,
        market_mapping=market_mapping,
        cutover_at=normalized_cutover_at,
    )
    write_active_price_basis_pointer(
        layout=layout,
        target_key=normalized_target,
        pointer=ActivePriceBasisPointer(
            basis_id=current_basis.basis_id,
            policy=current_policy,
            anchor_at=normalized_cutover_at,
        ),
    )
    return ActivePriceBasisCutoverResult(
        target_key=normalized_target,
        cutover_applied=True,
        old_basis_id=old_basis.basis_id,
        new_basis_id=current_basis.basis_id,
        predecessor_assessment_id=prior_assessment.assessment_id,
        successor_assessment_id=successor.assessment_id,
        successor_revision_id=revision.revision_id,
        pm_refresh_required=True,
    )


def run_active_price_basis_cutover_from_config(
    *,
    layout: WorkspaceLayout,
    config: KernelConfig,
    target_key: str,
    cutover_at: datetime,
    legacy_policy: str | None = None,
    require_rebase_ratio_change: bool = False,
) -> ActivePriceBasisCutoverResult:
    if config.validation is None:
        raise ActivePriceBasisCutoverError("validation config is required for basis cutover.")
    mapping = config.validation.market_mappings.get(target_key)
    if mapping is None:
        raise ActivePriceBasisCutoverError(
            f"validation.market_mappings is missing target_key: {target_key}"
        )
    market_data_provider = None
    if config.validation.market_data.local_archive_root is None:
        market_data_provider = build_default_market_bars_provider(config)
    try:
        return run_active_price_basis_cutover(
            layout=layout,
            target_key=target_key,
            cutover_at=cutover_at,
            market_mapping=mapping,
            local_archive_root=config.validation.market_data.local_archive_root,
            market_data_provider=market_data_provider,
            legacy_policy=legacy_policy,
            require_rebase_ratio_change=require_rebase_ratio_change,
        )
    finally:
        _close_market_data_provider(market_data_provider)


def read_active_price_basis_pointer(
    *,
    layout: WorkspaceLayout,
    target_key: str,
) -> ActivePriceBasisPointer | None:
    path = _pointer_path(layout=layout, target_key=target_key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActivePriceBasisCutoverError(
            f"failed to read active price basis pointer: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ActivePriceBasisCutoverError(
            "active price basis pointer must be a JSON object."
        )
    anchor_at_value = payload.get("anchor_at")
    if not isinstance(anchor_at_value, str):
        raise ActivePriceBasisCutoverError("active price basis pointer anchor_at is invalid.")
    try:
        anchor_at = datetime.fromisoformat(anchor_at_value)
    except ValueError as exc:
        raise ActivePriceBasisCutoverError(
            "active price basis pointer anchor_at must be an ISO timestamp."
        ) from exc
    return ActivePriceBasisPointer(
        basis_id=_required_text(payload.get("basis_id"), "basis_id"),
        policy=_required_text(payload.get("policy"), "policy"),
        anchor_at=anchor_at,
    )


def write_active_price_basis_pointer(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    pointer: ActivePriceBasisPointer,
) -> Path:
    path = _pointer_path(layout=layout, target_key=target_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    payload = pointer.to_json_payload()
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        raise ActivePriceBasisCutoverError(
            f"failed to write active price basis pointer: {path}"
        ) from exc
    return path


def resolve_active_price_basis(
    *,
    market_mapping: ValidationMarketMappingConfig,
    local_archive_root: Path | None,
    policy: str,
    anchor_at: datetime,
) -> ResolvedActivePriceBasis:
    normalized_anchor_at = validate_timestamp(
        anchor_at,
        field_name="anchor_at",
        error_type=ActivePriceBasisCutoverError,
    )
    archive_symbol = normalize_archive_symbol(market_mapping.market_symbol)
    effective_factor = 1.0
    sidecar_hash: str | None = None
    if policy == _RAW_POLICY:
        pass
    elif local_archive_root is not None:
        sidecar_hash = read_adjustment_sidecar_hash(
            root=local_archive_root,
            market_symbol=market_mapping.market_symbol,
            policy=policy,
        )
        if sidecar_hash is not None:
            sidecar = load_adjustment_sidecar_from_archive_root(
                archive_root=local_archive_root,
                market_symbol=market_mapping.market_symbol,
                policy=policy,
            )
            effective_factor = sidecar.factor_for_timestamp(normalized_anchor_at)
        else:
            raise ActivePriceBasisCutoverError(
                "configured adjustment basis requires an adjustment sidecar for "
                f"{market_mapping.market_symbol!r}."
            )
    else:
        raise ActivePriceBasisCutoverError(
            "non-raw adjustment basis cutover requires validation.market_data.local_archive_root."
        )
    basis_id = _basis_id(
        archive_symbol=archive_symbol,
        policy=policy,
        effective_factor=effective_factor,
        sidecar_hash=sidecar_hash,
    )
    return ResolvedActivePriceBasis(
        basis_id=basis_id,
        policy=policy,
        anchor_at=normalized_anchor_at,
        effective_factor=effective_factor,
        archive_symbol=archive_symbol,
        sidecar_hash=sidecar_hash,
    )


def _resolve_prior_basis(
    *,
    pointer: ActivePriceBasisPointer | None,
    legacy_policy: str | None,
    market_mapping: ValidationMarketMappingConfig,
    local_archive_root: Path | None,
    prior_assessment: AnalysisAssessment,
) -> ResolvedActivePriceBasis:
    if pointer is not None:
        return resolve_active_price_basis(
            market_mapping=market_mapping,
            local_archive_root=local_archive_root,
            policy=pointer.policy,
            anchor_at=pointer.anchor_at,
        )
    if legacy_policy is None:
        raise ActivePriceBasisCutoverError(
            "legacy workspace cutover requires legacy_policy when the active basis "
            "pointer is missing."
        )
    return resolve_active_price_basis(
        market_mapping=market_mapping,
        local_archive_root=local_archive_root,
        policy=legacy_policy,
        anchor_at=_legacy_anchor_at(prior_assessment),
    )


def _legacy_anchor_at(assessment: AnalysisAssessment) -> datetime:
    if assessment.analysis_price_semantics is not None:
        return assessment.analysis_price_semantics.analysis_reference_at
    return assessment.business_at


def _load_market_reference_price(
    *,
    target_key: str,
    market_mapping: ValidationMarketMappingConfig,
    local_archive_root: Path | None,
    market_data_provider: MarketBarsProvider | None,
    cutover_at: datetime,
) -> float:
    mapping = MarketMapping(
        target_key=target_key,
        market_symbol=market_mapping.market_symbol,
        market_session=market_mapping.market_session,
        exchange=market_mapping.exchange,
        bar_granularity=market_mapping.bar_granularity,
        exchange_session_scope=market_mapping.exchange_session_scope,
    )
    if local_archive_root is not None:
        provider = LocalArchiveMarketDataProvider(local_archive_root)
        start_at = datetime(1970, 1, 1, tzinfo=UTC)
    elif market_data_provider is not None:
        provider = market_data_provider
        start_at = _provider_reference_window_start(
            cutover_at=cutover_at,
            bar_granularity=market_mapping.bar_granularity,
        )
    else:
        raise ActivePriceBasisCutoverError(
            "deterministic market reference bootstrap requires local_archive_root "
            "or a configured market-data provider."
        )
    series = provider.read_series(
        mapping,
        start_at=start_at,
        end_at=cutover_at + timedelta(seconds=1),
    )
    adjusted_bars = series.bars
    if local_archive_root is not None and market_mapping.adjustment_policy is not None:
        sidecar = load_adjustment_sidecar_from_archive_root(
            archive_root=local_archive_root,
            market_symbol=market_mapping.market_symbol,
            policy=market_mapping.adjustment_policy,
        )
        adjusted_bars = apply_adjustment_policy_to_series(
            series=series,
            sidecar=sidecar,
            reference_at=cutover_at,
        ).series.bars
    visible = tuple(bar for bar in adjusted_bars if bar.end_at <= cutover_at)
    if not visible:
        raise ActivePriceBasisCutoverError(
            "no market bars are available at or before cutover_at for deterministic "
            f"reference-price bootstrap on {target_key!r}."
        )
    return visible[-1].close_price


def _provider_reference_window_start(*, cutover_at: datetime, bar_granularity: str) -> datetime:
    # Provider-backed cutover only needs the latest visible bar, so bound the
    # live fetch to a recent deterministic lookback instead of requesting full history.
    lookback = max(parse_stock_bar_granularity(bar_granularity) * 32, timedelta(days=30))
    return cutover_at - lookback


def _close_market_data_provider(provider: MarketBarsProvider | None) -> None:
    if provider is None:
        return
    close = getattr(provider, "close", None)
    if callable(close):
        close()


def _persist_projected_sections(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    cutover_at: datetime,
    actor_id: str,
    projected_sections: tuple[ActivePriceProjectionSection, ...],
) -> tuple[ResearchMemoryWriteReceipt, ...]:
    attribution = ResearchMemoryWriteAttribution(
        actor_type=_ACTOR_TYPE,
        actor_id=actor_id,
        business_at=cutover_at,
        committed_at=cutover_at,
    )
    writer = ReceiptedResearchMemoryPageWriter(
        layout=layout,
        attribution=attribution,
    )
    for section in projected_sections:
        writer.update_page_section(
            section.page_path,
            section.section_name,
            section.content_md,
        )
    expected_sections = {
        (section.page_path, section.section_name)
        for section in projected_sections
    }
    receipt_store = FileBackedResearchMemoryWriteReceiptStore(layout)
    parsed_receipts = {
        (receipt.page_path, receipt.section_name): receipt for receipt in writer.receipts
    }
    for payload in receipt_store.read_receipts(
        scope_key=target_key,
        year_month=cutover_at.strftime("%Y-%m"),
    ):
        receipt = parse_research_memory_write_receipt(payload)
        receipt_key = (receipt.page_path, receipt.section_name)
        if receipt_key not in expected_sections:
            continue
        if receipt.actor_type != attribution.actor_type or receipt.actor_id != attribution.actor_id:
            continue
        if receipt.business_at != attribution.business_at or receipt.committed_at != attribution.committed_at:
            continue
        parsed_receipts[receipt_key] = receipt
    missing_sections = sorted(expected_sections - set(parsed_receipts))
    if missing_sections:
        raise ActivePriceBasisCutoverError(
            "active price basis cutover could not resolve projected section write "
            f"receipts: {missing_sections!r}."
        )
    return tuple(
        parsed_receipts[receipt_key]
        for receipt_key in sorted(expected_sections)
    )


def _append_successor_revision(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    cutover_at: datetime,
    successor: AnalysisAssessment,
    receipts: tuple[ResearchMemoryWriteReceipt, ...],
) -> object:
    store = ThesisRevisionStore(layout)
    builder = ThesisRevisionBuilder(layout=layout, store=store)
    try:
        revision = builder.build(
            target_key=target_key,
            business_at=cutover_at,
            committed_at=cutover_at,
            source="active_price_basis_cutover",
            analysis_assessment=successor,
            context_packet_id=None,
            context_packet_hash=None,
            source_event_ids=successor.source_event_ids,
            research_memory_write_receipt_ids=successor.memory_write_receipt_ids,
            committed_write_receipts=receipts,
        )
        store.append(revision)
    except (ThesisRevisionBuildError, ThesisRevisionStoreError) as exc:
        raise ActivePriceBasisCutoverError(str(exc)) from exc
    return revision


def _refresh_active_pm_observation_chain(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    predecessor_assessment_id: str,
    successor_assessment: AnalysisAssessment,
    market_mapping: ValidationMarketMappingConfig,
    cutover_at: datetime,
) -> None:
    try:
        requests = tuple(
            persisted.record
            for persisted in PMReviewRequestStore(layout).read_records(target_key=target_key)
        )
        decisions = tuple(
            persisted.record
            for persisted in PMDecisionStore(layout).read_records(target_key=target_key)
        )
        portfolio_state = PortfolioStateStore(layout).read(target_key=target_key)
    except (PMReviewStoreError, PortfolioStoreError) as exc:
        raise ActivePriceBasisCutoverError(str(exc)) from exc
    request_lookup = {request.request_id: request for request in requests}
    decided_request_ids = {
        decision.pm_review_request_id
        for decision in decisions
        if decision.pm_review_request_id is not None
    }
    active_source_decision = (
        None
        if portfolio_state is None or portfolio_state.state == "flat"
        else _active_pm_position_review_source_decision(
            decisions=decisions,
            portfolio_state=portfolio_state,
        )
    )
    pending_request = _latest_pending_cutover_request(
        requests=requests,
        predecessor_assessment_id=predecessor_assessment_id,
        decided_request_ids=decided_request_ids,
    )
    active_root_request = None
    if active_source_decision is not None:
        linked_request = request_lookup.get(active_source_decision.pm_review_request_id or "")
        if (
            linked_request is not None
            and linked_request.source_assessment_id == predecessor_assessment_id
        ):
            active_root_request = linked_request
    if pending_request is None and active_root_request is None:
        return
    request_store = PMReviewRequestStore(layout)
    successor_requests_by_prior_id: dict[str, PMReviewRequest] = {}

    def _ensure_successor_request(prior_request: PMReviewRequest) -> PMReviewRequest:
        cached = successor_requests_by_prior_id.get(prior_request.request_id)
        if cached is not None:
            return cached
        successor_request = _build_successor_pm_review_request(
            prior_request=prior_request,
            successor_assessment_id=successor_assessment.assessment_id,
            cutover_at=cutover_at,
        )
        try:
            request_store.append(successor_request)
        except PMReviewStoreError as exc:
            raise ActivePriceBasisCutoverError(str(exc)) from exc
        successor_requests_by_prior_id[prior_request.request_id] = successor_request
        return successor_request

    if pending_request is not None:
        _ensure_successor_request(pending_request)
    if active_source_decision is None or portfolio_state is None or portfolio_state.state == "flat":
        return
    if active_root_request is None:
        return
    successor_request = _ensure_successor_request(active_root_request)
    successor_decision = _build_successor_noop_pm_decision(
        successor_request=successor_request,
        source_decision=active_source_decision,
        portfolio_state=portfolio_state,
        cutover_at=cutover_at,
    )
    try:
        PMDecisionStore(layout).append(successor_decision)
    except PortfolioStoreError as exc:
        raise ActivePriceBasisCutoverError(str(exc)) from exc
    successor_triggers = _build_successor_pm_position_review_triggers(
        successor_request=successor_request,
        successor_assessment=successor_assessment,
        market_mapping=market_mapping,
        portfolio_state=portfolio_state,
        prior_requests=requests,
    )
    if successor_triggers is None:
        return
    try:
        PMPositionReviewTriggerStore(layout).append(successor_triggers)
    except PMReviewStoreError as exc:
        raise ActivePriceBasisCutoverError(str(exc)) from exc


def _required_projected_section_content(
    sections: tuple[ActivePriceProjectionSection, ...],
    *,
    page_path: str,
    section_name: str,
) -> str:
    for section in sections:
        if section.page_path == page_path and section.section_name == section_name:
            return section.content_md
    raise ActivePriceBasisCutoverError(
        f"missing projected active section: {page_path}:{section_name}"
    )


def _latest_assessment(
    records: tuple[PersistedAnalysisAssessment, ...],
) -> PersistedAnalysisAssessment | None:
    if not records:
        return None
    return max(
        records,
        key=lambda item: (
            item.record.business_at,
            item.path.as_posix(),
            item.line_number,
        ),
    )


def _latest_pending_cutover_request(
    *,
    requests: tuple[PMReviewRequest, ...],
    predecessor_assessment_id: str,
    decided_request_ids: set[str],
) -> PMReviewRequest | None:
    pending = tuple(
        request
        for request in requests
        if request.source_assessment_id == predecessor_assessment_id
        and request.request_id not in decided_request_ids
    )
    if not pending:
        return None
    return sorted(
        pending,
        key=lambda item: (item.business_at, item.request_id),
    )[-1]


def _find_assessment_by_id(
    records: tuple[PersistedAnalysisAssessment, ...],
    *,
    assessment_id: str,
) -> PersistedAnalysisAssessment | None:
    for persisted in records:
        if persisted.record.assessment_id == assessment_id:
            return persisted
    return None


def _successor_assessment_id(
    *,
    predecessor_assessment_id: str,
    old_basis_id: str,
    new_basis_id: str,
    cutover_at: datetime,
) -> str:
    payload = {
        "predecessor_assessment_id": predecessor_assessment_id,
        "old_basis_id": old_basis_id,
        "new_basis_id": new_basis_id,
        "cutover_at": cutover_at.isoformat(),
    }
    digest = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"analysis-assessment:active-price-basis-cutover:{digest[:24]}"


def _successor_pm_review_request_id(
    *,
    prior_request_id: str,
    successor_assessment_id: str,
    cutover_at: datetime,
) -> str:
    payload = {
        "prior_request_id": prior_request_id,
        "successor_assessment_id": successor_assessment_id,
        "cutover_at": cutover_at.isoformat(),
    }
    digest = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"pm-review-request:active-price-basis-cutover:{digest[:24]}"


def _successor_noop_pm_decision_id(
    *,
    successor_request_id: str,
    source_decision_id: str,
    cutover_at: datetime,
) -> str:
    payload = {
        "successor_request_id": successor_request_id,
        "source_decision_id": source_decision_id,
        "cutover_at": cutover_at.isoformat(),
    }
    digest = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"pm-decision:active-price-basis-cutover:{digest[:24]}"


def _build_successor_pm_review_request(
    *,
    prior_request: PMReviewRequest,
    successor_assessment_id: str,
    cutover_at: datetime,
) -> PMReviewRequest:
    return PMReviewRequest(
        request_id=_successor_pm_review_request_id(
            prior_request_id=prior_request.request_id,
            successor_assessment_id=successor_assessment_id,
            cutover_at=cutover_at,
        ),
        target_key=prior_request.target_key,
        business_at=cutover_at,
        source=prior_request.source,
        source_assessment_id=successor_assessment_id,
        source_episode_id=prior_request.source_episode_id,
        source_event_ids=prior_request.source_event_ids,
        review_reasons=prior_request.review_reasons,
        current_exposure_required=prior_request.current_exposure_required,
        candidate_review_allowed=prior_request.candidate_review_allowed,
        max_visible_event_time=max(prior_request.max_visible_event_time, cutover_at),
        max_visible_market_time=max(prior_request.max_visible_market_time, cutover_at),
        required_price_level_ids=prior_request.required_price_level_ids,
        required_claim_ids=prior_request.required_claim_ids,
        candidate_anchor_id=prior_request.candidate_anchor_id,
    )


def _build_successor_noop_pm_decision(
    *,
    successor_request: PMReviewRequest,
    source_decision: PMDecision,
    portfolio_state: PortfolioState,
    cutover_at: datetime,
) -> PMDecision:
    source_event_ids = (
        successor_request.source_event_ids
        if successor_request.source_event_ids
        else source_decision.source_event_ids
    )
    return PMDecision(
        decision_id=_successor_noop_pm_decision_id(
            successor_request_id=successor_request.request_id,
            source_decision_id=source_decision.decision_id,
            cutover_at=cutover_at,
        ),
        decision_episode_id=source_decision.decision_episode_id,
        pm_review_request_id=successor_request.request_id,
        target_key=successor_request.target_key,
        business_at=cutover_at,
        decision_available_at=cutover_at,
        source_event_ids=source_event_ids,
        actual_state_before_decision=portfolio_state.state,
        actual_target_weight_before_decision=portfolio_state.target_weight,
        execution_required=False,
        requested_state=portfolio_state.state,
        requested_target_weight=portfolio_state.target_weight,
        rationale_md=(
            "Deterministic no-op PM decision appended during active price basis cutover."
        ),
    )


def _build_successor_pm_position_review_triggers(
    *,
    successor_request: PMReviewRequest,
    successor_assessment: AnalysisAssessment,
    market_mapping: ValidationMarketMappingConfig,
    portfolio_state: PortfolioState,
    prior_requests: tuple[PMReviewRequest, ...],
) -> object:
    if portfolio_state.state == "flat":
        return None
    try:
        pm_review_input = PMReviewInput(
            pm_review_request_id=successor_request.request_id,
            target_key=successor_request.target_key,
            business_at=successor_request.business_at,
            source=successor_request.source,
            source_assessment_id=successor_request.source_assessment_id,
            source_episode_id=successor_request.source_episode_id,
            source_event_ids=successor_request.source_event_ids,
            review_reasons=successor_request.review_reasons,
            current_exposure_required=successor_request.current_exposure_required,
            candidate_review_allowed=successor_request.candidate_review_allowed,
            execution_direction_mode=market_mapping.execution_direction_mode,
            decision_visibility=successor_request.decision_visibility,
            required_price_level_ids=successor_request.required_price_level_ids,
            required_claim_ids=successor_request.required_claim_ids,
            actual_current_state=portfolio_state.state,
            actual_target_weight_before=portfolio_state.target_weight,
            analysis_snapshot=analysis_snapshot_from_assessment(successor_assessment),
            portfolio_risk_snapshot=None,
            visible_evidence=(),
            visible_market_bars=(),
            visible_pm_history=_read_visible_pm_history_for_cutover(
                request=successor_request,
                requests=(*prior_requests, successor_request),
            ),
            source_episode_memory_view=None,
            source_episode_memory_read_receipt=None,
            candidate_anchor=None,
        )
        return build_pm_position_review_triggers(
            pm_review_input=pm_review_input,
            requested_state=portfolio_state.state,
        )
    except (PMPositionReviewError, PMReviewContractError) as exc:
        raise ActivePriceBasisCutoverError(str(exc)) from exc
def _read_visible_pm_history_for_cutover(
    *,
    request: PMReviewRequest,
    requests: tuple[PMReviewRequest, ...],
) -> tuple[PMReviewRequest, ...]:
    return tuple(
        sorted(
            (
                item
                for item in requests
                if item.target_key == request.target_key
                and item.request_id != request.request_id
                and item.business_at < request.business_at
            ),
            key=lambda item: (item.business_at, item.request_id),
        )
    )


def _active_pm_position_review_source_decision(
    *,
    decisions: tuple[PMDecision, ...],
    portfolio_state: PortfolioState,
) -> PMDecision | None:
    aligned = tuple(
        decision
        for decision in decisions
        if decision.requested_state == portfolio_state.state
        and decision.business_at >= portfolio_state.updated_at
    )
    if aligned:
        return sorted(
            aligned,
            key=lambda item: (item.business_at, item.decision_id),
        )[-1]
    return next(
        (
            decision
            for decision in decisions
            if decision.decision_id == portfolio_state.source_pm_decision_id
        ),
        None,
    )


def _basis_id(
    *,
    archive_symbol: str,
    policy: str,
    effective_factor: float,
    sidecar_hash: str | None,
) -> str:
    payload = {
        "archive_symbol": archive_symbol,
        "policy": policy,
        "effective_factor": effective_factor,
        "sidecar_hash": sidecar_hash,
    }
    digest = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"active-price-basis:{digest[:24]}"


def _pointer_path(*, layout: WorkspaceLayout, target_key: str) -> Path:
    normalized_target = validate_target_key(
        target_key,
        error_type=ActivePriceBasisCutoverError,
    )
    return (
        layout.runtime_root / _BASIS_POINTER_DIR / f"{normalized_target}.json"
    ).resolve(strict=False)


def _policy_or_raw(value: str | None) -> str:
    return _RAW_POLICY if value is None else value


def _rebase_ratio(
    *,
    market_mapping: ValidationMarketMappingConfig,
    local_archive_root: Path | None,
    old_basis: ResolvedActivePriceBasis,
    current_basis: ResolvedActivePriceBasis,
) -> float:
    old_multiplier = _basis_projection_multiplier(
        market_mapping=market_mapping,
        local_archive_root=local_archive_root,
        policy=old_basis.policy,
        price_at=old_basis.anchor_at,
        reference_at=old_basis.anchor_at,
    )
    current_multiplier = _basis_projection_multiplier(
        market_mapping=market_mapping,
        local_archive_root=local_archive_root,
        policy=current_basis.policy,
        price_at=old_basis.anchor_at,
        reference_at=current_basis.anchor_at,
    )
    return current_multiplier / old_multiplier


def _basis_projection_multiplier(
    *,
    market_mapping: ValidationMarketMappingConfig,
    local_archive_root: Path | None,
    policy: str,
    price_at: datetime,
    reference_at: datetime,
) -> float:
    if policy == _RAW_POLICY:
        return 1.0
    if local_archive_root is None:
        raise ActivePriceBasisCutoverError(
            "non-raw adjustment basis cutover requires validation.market_data.local_archive_root."
        )
    sidecar = load_adjustment_sidecar_from_archive_root(
        archive_root=local_archive_root,
        market_symbol=market_mapping.market_symbol,
        policy=policy,
    )
    if policy == "forward_adjusted_visible":
        return sidecar.factor_for_timestamp(price_at) / sidecar.factor_for_timestamp(reference_at)
    return sidecar.factor_for_timestamp(price_at)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActivePriceBasisCutoverError(f"{field_name} must be a non-blank string.")
    return value.strip()


__all__ = [
    "ActivePriceBasisCutoverError",
    "ActivePriceBasisCutoverResult",
    "ActivePriceBasisPointer",
    "ResolvedActivePriceBasis",
    "read_active_price_basis_pointer",
    "resolve_active_price_basis",
    "run_active_price_basis_cutover",
    "run_active_price_basis_cutover_from_config",
    "write_active_price_basis_pointer",
]
