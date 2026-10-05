"""Assemble the PM Console workbench response from existing read-only surfaces."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath

from event_trader.contracts._validators import validate_target_key
from event_trader.contracts.research_memory import resolve_page_path
from event_trader.execution.store import ExecutionRecordStore
from event_trader.integrations.markdown_context import MarkdownContextError, find_markdown_section
from event_trader.operator_context import OperatorContextParseError, parse_operator_context_page
from event_trader.pm_console.schemas import (
    PMConsolePositionResponseDTO,
    PMConsoleStatusDTO,
    PMConsoleWorkbenchOperatorCardDTO,
    PMConsoleWorkbenchOperatorSummaryDTO,
    PMConsoleWorkbenchPMReviewSummaryDTO,
    PMConsoleWorkbenchPositionSummaryDTO,
    PMConsoleWorkbenchResponseDTO,
    PMConsoleWorkbenchSectionDTO,
)
from event_trader.pm_console.thesis import build_thesis_revisions_response
from event_trader.pm_review.contracts import is_terminal_pm_review_policy_skip_reason
from event_trader.pm_review.store import (
    PMReviewDispatchConsiderationStore,
    PMReviewEpisodeMemoryReadReceiptStore,
    PMReviewFailureStore,
    PMReviewRequestStore,
    PMReviewToolReadReceiptStore,
)
from event_trader.portfolio.store import PMDecisionStore
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.canonical_bundle import canonical_thesis_bundle_sections
from event_trader.thesis_revision.store import ThesisRevisionStore, sort_persisted_thesis_revisions


def build_workbench_response(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    position_response: PMConsolePositionResponseDTO | None = None,
) -> PMConsoleWorkbenchResponseDTO:
    """Build the PM-facing workbench response for one target."""

    normalized_target = validate_target_key(target_key, error_type=ValueError)
    latest_revision = build_thesis_revisions_response(
        layout,
        target_key=normalized_target,
    ).latest_revision
    latest_revision_record = _latest_revision_record(layout, normalized_target)
    changed_section_ids = _latest_changed_section_ids(latest_revision_record)
    return PMConsoleWorkbenchResponseDTO(
        generated_at=datetime.now(UTC).isoformat(),
        target_key=normalized_target,
        sections=_build_sections(
            layout=layout,
            target_key=normalized_target,
            changed_section_ids=changed_section_ids,
        ),
        latest_revision=latest_revision,
        position_summary=_build_position_summary(position_response),
        pm_review_summary=_build_pm_review_summary(
            layout,
            normalized_target,
        ),
        operator_summary=_build_operator_summary(layout, normalized_target),
    )


def _latest_revision_record(layout: WorkspaceLayout, target_key: str):
    persisted = sort_persisted_thesis_revisions(
        ThesisRevisionStore(layout).read_records(target_key=target_key)
    )
    if not persisted:
        return None
    return persisted[-1]


def _latest_changed_section_ids(persisted) -> frozenset[str]:
    if persisted is None:
        return frozenset()
    return frozenset(
        f"{diff.page_path}:{diff.section_name}"
        for diff in persisted.record.diffs_from_previous
        if diff.unified_diff_md
    )


def _build_sections(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    changed_section_ids: frozenset[str],
) -> tuple[PMConsoleWorkbenchSectionDTO, ...]:
    sections: list[PMConsoleWorkbenchSectionDTO] = []
    page_cache: dict[str, str | None] = {}
    for bundle_section in canonical_thesis_bundle_sections(target_key):
        page_content = page_cache.get(bundle_section.page_path)
        if bundle_section.page_path not in page_cache:
            page_content = _read_page_content(layout, bundle_section.page_path)
            page_cache[bundle_section.page_path] = page_content
        page_name = PurePosixPath(bundle_section.page_path).name
        if page_content is None:
            sections.append(
                PMConsoleWorkbenchSectionDTO(
                    page_path=bundle_section.page_path,
                    page_name=page_name,
                    section_name=bundle_section.section_name,
                    content_md="",
                    content_sha256=None,
                    status=PMConsoleStatusDTO(
                        code="missing_page",
                        explanation=(
                            "Current ResearchMemory page is not present in the workspace."
                        ),
                    ),
                    changed_in_latest_revision=(
                        bundle_section.section_id in changed_section_ids
                    ),
                )
            )
            continue
        try:
            section_map = find_markdown_section(
                page_content,
                heading=bundle_section.section_name,
                heading_level=2,
            )
        except MarkdownContextError as exc:
            sections.append(
                PMConsoleWorkbenchSectionDTO(
                    page_path=bundle_section.page_path,
                    page_name=page_name,
                    section_name=bundle_section.section_name,
                    content_md="",
                    content_sha256=None,
                    status=PMConsoleStatusDTO(
                        code="unreadable_section",
                        explanation=str(exc),
                    ),
                    changed_in_latest_revision=(
                        bundle_section.section_id in changed_section_ids
                    ),
                )
            )
            continue
        if section_map is None:
            sections.append(
                PMConsoleWorkbenchSectionDTO(
                    page_path=bundle_section.page_path,
                    page_name=page_name,
                    section_name=bundle_section.section_name,
                    content_md="",
                    content_sha256=None,
                    status=PMConsoleStatusDTO(
                        code="missing_section",
                        explanation=(
                            "Current ResearchMemory page is missing "
                            "this canonical section."
                        ),
                    ),
                    changed_in_latest_revision=(
                        bundle_section.section_id in changed_section_ids
                    ),
                )
            )
            continue
        content_md = page_content[
            int(section_map["content_start_offset"]) : int(section_map["content_end_offset"])
        ].strip()
        if not content_md:
            sections.append(
                PMConsoleWorkbenchSectionDTO(
                    page_path=bundle_section.page_path,
                    page_name=page_name,
                    section_name=bundle_section.section_name,
                    content_md="",
                    content_sha256=str(section_map["content_sha256"]),
                    status=PMConsoleStatusDTO(
                        code="empty",
                        explanation=(
                            "Current ResearchMemory section exists "
                            "but has no PM-facing content yet."
                        ),
                    ),
                    changed_in_latest_revision=(
                        bundle_section.section_id in changed_section_ids
                    ),
                )
            )
            continue
        sections.append(
            PMConsoleWorkbenchSectionDTO(
                page_path=bundle_section.page_path,
                page_name=page_name,
                section_name=bundle_section.section_name,
                content_md=content_md,
                content_sha256=str(section_map["content_sha256"]),
                status=PMConsoleStatusDTO(
                    code="available",
                    explanation="Current ResearchMemory section is available.",
                ),
                changed_in_latest_revision=(bundle_section.section_id in changed_section_ids),
            )
        )
    return tuple(sections)


def _read_page_content(
    layout: WorkspaceLayout,
    page_path: str,
) -> str | None:
    absolute_path = resolve_page_path(layout, page_path)
    if not absolute_path.exists():
        return None
    try:
        return absolute_path.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def _build_position_summary(
    position_response: PMConsolePositionResponseDTO | None,
) -> PMConsoleWorkbenchPositionSummaryDTO:
    if position_response is None:
        return PMConsoleWorkbenchPositionSummaryDTO(
            status=PMConsoleStatusDTO(
                code="empty",
                explanation=(
                    "Position Management is not implemented for "
                    "this target in the current workspace."
                ),
            ),
            current_portfolio_state=None,
            latest_pm_decision=None,
            latest_execution=None,
            market_data_status=None,
            comparison_status=None,
            market_symbol=None,
        )
    return PMConsoleWorkbenchPositionSummaryDTO(
        status=PMConsoleStatusDTO(
            code="ready",
            explanation=(
                "Position summary is sourced from the existing "
                "Position Management read model."
            ),
        ),
        current_portfolio_state=position_response.current_portfolio_state,
        latest_pm_decision=position_response.latest_pm_decision,
        latest_execution=position_response.latest_execution,
        market_data_status=position_response.market_data_status,
        comparison_status=position_response.comparison_status,
        market_symbol=position_response.market_symbol,
    )


def _build_pm_review_summary(
    layout: WorkspaceLayout,
    target_key: str,
) -> PMConsoleWorkbenchPMReviewSummaryDTO:
    requests = tuple(
        persisted.record
        for persisted in PMReviewRequestStore(layout).read_records(
            target_key=target_key
        )
    )
    if not requests:
        return PMConsoleWorkbenchPMReviewSummaryDTO(
            status=PMConsoleStatusDTO(
                code="empty",
                explanation="No PMReview request has been persisted for this target yet.",
            ),
            request_id=None,
            business_at=None,
            source=None,
            review_reasons=(),
            dispatch_outcome=None,
            dispatch_run_until=None,
            dispatch_skip_reason=None,
            lifecycle_state=None,
            pm_decision_id=None,
            execution_record_id=None,
            tool_read_count=0,
            visibility_failed_tool_read_count=0,
            episode_memory_status=None,
            failure_stage=None,
            failure_retry_allowed=None,
        )
    latest_request = sorted(
        requests,
        key=lambda item: (item.business_at, item.request_id),
    )[-1]
    dispatches = tuple(
        persisted.record
        for persisted in PMReviewDispatchConsiderationStore(layout).read_records(
            target_key=target_key
        )
        if persisted.record.request_id == latest_request.request_id
    )
    latest_dispatch = (
        None
        if not dispatches
        else sorted(
            dispatches,
            key=lambda item: (item.business_at, item.consideration_id),
        )[-1]
    )
    failures = tuple(
        persisted.record
        for persisted in PMReviewFailureStore(layout).read_records(target_key=target_key)
        if persisted.record.pm_review_request_id == latest_request.request_id
    )
    latest_failure = (
        None
        if not failures
        else sorted(
            failures,
            key=lambda item: (item.failed_at, item.failure_id),
        )[-1]
    )
    tool_reads = tuple(
        persisted.record
        for persisted in PMReviewToolReadReceiptStore(layout).read_records(target_key=target_key)
        if persisted.record.pm_review_request_id == latest_request.request_id
    )
    episode_reads = tuple(
        persisted.record
        for persisted in PMReviewEpisodeMemoryReadReceiptStore(layout).read_records(
            target_key=target_key
        )
        if persisted.record.request_id == latest_request.request_id
    )
    latest_episode_read = (
        None
        if not episode_reads
        else sorted(
            episode_reads,
            key=lambda item: (item.business_at, item.request_id),
        )[-1]
    )
    decisions = tuple(
        persisted.record
        for persisted in PMDecisionStore(layout).read_records(target_key=target_key)
        if persisted.record.pm_review_request_id == latest_request.request_id
    )
    executions = tuple(
        persisted.record
        for persisted in ExecutionRecordStore(layout).read_records(target_key=target_key)
    )

    status = PMConsoleStatusDTO(
        code="partial",
        explanation="PMReview request is present but not terminal.",
    )
    lifecycle_state: str | None = None
    pm_decision_id: str | None = None
    execution_record_id: str | None = None

    if latest_dispatch is not None and latest_dispatch.outcome == "skipped":
        lifecycle_state = "dispatch_skipped"
        if is_terminal_pm_review_policy_skip_reason(latest_dispatch.skip_reason):
            status = PMConsoleStatusDTO(
                code="ready",
                explanation="Latest PMReview request reached a terminal policy skip state.",
            )
        else:
            status = PMConsoleStatusDTO(
                code="partial",
                explanation=(
                    "Latest PMReview request was skipped, but "
                    "the skip reason is not terminal."
                ),
            )
    elif len(decisions) > 1:
        lifecycle_state = "ambiguous_multiple_decisions"
        status = PMConsoleStatusDTO(
            code="partial",
            explanation="Multiple PMDecision records reference the latest PMReview request.",
        )
    elif len(decisions) == 1:
        decision = decisions[0]
        pm_decision_id = decision.decision_id
        matching_executions = sorted(
            (
                execution
                for execution in executions
                if execution.pm_decision_id == decision.decision_id
                and execution.status in {"executed", "rejected"}
            ),
            key=lambda item: (item.business_at, item.execution_record_id),
        )
        latest_execution = None if not matching_executions else matching_executions[-1]
        if latest_execution is not None:
            execution_record_id = latest_execution.execution_record_id
            lifecycle_state = (
                "complete_executed"
                if latest_execution.status == "executed"
                else "complete_execution_rejected"
            )
            status = PMConsoleStatusDTO(
                code="ready",
                explanation="Latest PMReview request has a terminal execution outcome.",
            )
        elif not decision.execution_required:
            lifecycle_state = "complete_hold_noop"
            status = PMConsoleStatusDTO(
                code="ready",
                explanation="Latest PMReview request resolved to a no-op PM decision.",
            )
        else:
            lifecycle_state = "decision_without_execution"
            status = PMConsoleStatusDTO(
                code="partial",
                explanation=(
                    "Latest PMReview request has a PM decision but "
                    "no terminal execution record yet."
                ),
            )
    else:
        lifecycle_state = "pending_no_decision"
        if latest_failure is not None:
            status = PMConsoleStatusDTO(
                code="partial",
                explanation="Latest PMReview request has a failure record and no PM decision yet.",
            )
        elif latest_dispatch is not None and latest_dispatch.outcome == "processed":
            status = PMConsoleStatusDTO(
                code="partial",
                explanation=(
                    "Latest PMReview request was dispatched but "
                    "no PM decision has been persisted yet."
                ),
            )
        else:
            status = PMConsoleStatusDTO(
                code="partial",
                explanation=(
                    "Latest PMReview request has not reached a "
                    "terminal lifecycle state yet."
                ),
            )

    return PMConsoleWorkbenchPMReviewSummaryDTO(
        status=status,
        request_id=latest_request.request_id,
        business_at=latest_request.business_at.isoformat(),
        source=latest_request.source,
        review_reasons=tuple(latest_request.review_reasons),
        dispatch_outcome=(None if latest_dispatch is None else latest_dispatch.outcome),
        dispatch_run_until=(
            None if latest_dispatch is None else latest_dispatch.dispatch_run_until.isoformat()
        ),
        dispatch_skip_reason=(None if latest_dispatch is None else latest_dispatch.skip_reason),
        lifecycle_state=lifecycle_state,
        pm_decision_id=pm_decision_id,
        execution_record_id=execution_record_id,
        tool_read_count=len(tool_reads),
        visibility_failed_tool_read_count=sum(
            1 for receipt in tool_reads if receipt.visibility_passed is False
        ),
        episode_memory_status=(
            None if latest_episode_read is None else latest_episode_read.status
        ),
        failure_stage=(None if latest_failure is None else latest_failure.stage),
        failure_retry_allowed=(
            None if latest_failure is None else latest_failure.retry_allowed
        ),
    )


def _build_operator_summary(
    layout: WorkspaceLayout,
    target_key: str,
) -> PMConsoleWorkbenchOperatorSummaryDTO:
    operator_page_path = f"targets/{target_key}/operator.md"
    content_md = _read_page_content(layout, operator_page_path)
    if content_md is None:
        return PMConsoleWorkbenchOperatorSummaryDTO(
            status=PMConsoleStatusDTO(
                code="empty",
                explanation="No operator context page is present for this target.",
            ),
            active_cards=(),
            retired_card_count=0,
            parse_error=None,
        )
    try:
        cards = parse_operator_context_page(content_md, target_key=target_key)
    except OperatorContextParseError as exc:
        return PMConsoleWorkbenchOperatorSummaryDTO(
            status=PMConsoleStatusDTO(
                code="partial",
                explanation="Operator context page exists but could not be parsed cleanly.",
            ),
            active_cards=(),
            retired_card_count=0,
            parse_error=str(exc),
        )
    active_cards = tuple(
        PMConsoleWorkbenchOperatorCardDTO(
            card_id=card.card_id,
            section_name=card.section_name,
            body=card.body,
        )
        for card in cards
        if not card.is_retired
    )
    retired_card_count = sum(1 for card in cards if card.is_retired)
    if not active_cards:
        return PMConsoleWorkbenchOperatorSummaryDTO(
            status=PMConsoleStatusDTO(
                code="empty",
                explanation="Operator context page has no active cards.",
            ),
            active_cards=(),
            retired_card_count=retired_card_count,
            parse_error=None,
        )
    return PMConsoleWorkbenchOperatorSummaryDTO(
        status=PMConsoleStatusDTO(
            code="ready",
            explanation="Operator summary is sourced from the parsed operator context page.",
        ),
        active_cards=active_cards,
        retired_card_count=retired_card_count,
        parse_error=None,
    )


__all__ = ["build_workbench_response"]
