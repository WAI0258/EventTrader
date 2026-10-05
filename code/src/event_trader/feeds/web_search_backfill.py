"""Historical web-search backfill boundary for replay source preparation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from inspect import Parameter, signature

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.feeds.historical_web_search_guard import (
    detect_historical_web_search_future_leakage,
)
from event_trader.feeds.web_search import (
    LiveWebSearchGatherError,
    shape_live_web_search_material,
)
from event_trader.integrations.web_source_published_at import (
    WebSourcePublishedAtResolutionError,
    extract_exact_web_source_published_at,
    resolve_web_source_published_at,
)
from event_trader.source_archive.web_search import (
    HistoricalWebSearchSliceCompletion,
    HistoricalWebSearchWriteReceipt,
    WebSearchAcquisitionProvenance,
    append_historical_web_search_slice_completion,
    read_historical_web_search_slice_completions,
    write_historical_web_search_backfill_record,
)
from event_trader.storage import WorkspaceLayout


class HistoricalWebSearchBackfillError(ValueError):
    """Raised when historical web-search backfill cannot proceed deterministically."""


type PublishedAtResolver = Callable[..., datetime | None]


@dataclass(frozen=True, slots=True)
class HistoricalWebSearchCollectionResult:
    """One completed slice collection result with immediate archive writes."""

    write_receipts: tuple[HistoricalWebSearchWriteReceipt, ...]
    boundary_rejection_reasons: tuple[str, ...]
    eligible_for_completion: bool
    successful_append_count: int
    final_exit_reason: str
    root_cause_detail: str
    search_attempt_count: int
    search_success_count: int
    search_failure_count: int
    scrape_attempt_count: int
    scrape_success_count: int
    scrape_failure_count: int
    append_attempt_count: int
    append_rejected_count: int
    append_failed_count: int
    append_terminal_count: int
    collection_failure_details: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.write_receipts, tuple):
            raise HistoricalWebSearchBackfillError(
                "write_receipts must be a tuple of HistoricalWebSearchWriteReceipt."
            )
        if not all(
            isinstance(item, HistoricalWebSearchWriteReceipt)
            for item in self.write_receipts
        ):
            raise HistoricalWebSearchBackfillError(
                "write_receipts must contain only HistoricalWebSearchWriteReceipt instances."
            )
        if not isinstance(self.boundary_rejection_reasons, tuple):
            raise HistoricalWebSearchBackfillError(
                "boundary_rejection_reasons must be a tuple of strings."
            )
        if not all(isinstance(item, str) for item in self.boundary_rejection_reasons):
            raise HistoricalWebSearchBackfillError(
                "boundary_rejection_reasons must contain only strings."
            )
        if not isinstance(self.eligible_for_completion, bool):
            raise HistoricalWebSearchBackfillError(
                "eligible_for_completion must be a boolean."
            )
        if (
            isinstance(self.successful_append_count, bool)
            or not isinstance(self.successful_append_count, int)
        ):
            raise HistoricalWebSearchBackfillError(
                "successful_append_count must be an integer."
            )
        if self.successful_append_count < 0:
            raise HistoricalWebSearchBackfillError(
                "successful_append_count must be greater than or equal to zero."
            )
        if not isinstance(self.final_exit_reason, str):
            raise HistoricalWebSearchBackfillError(
                "final_exit_reason must be a string."
            )
        if not self.final_exit_reason.strip():
            raise HistoricalWebSearchBackfillError(
                "final_exit_reason must not be blank."
            )
        if not isinstance(self.root_cause_detail, str):
            raise HistoricalWebSearchBackfillError(
                "root_cause_detail must be a string."
            )
        if not self.root_cause_detail.strip():
            raise HistoricalWebSearchBackfillError(
                "root_cause_detail must not be blank."
            )
        if (
            self.eligible_for_completion
            and self.successful_append_count <= 0
            and self.final_exit_reason != "no_source_found"
        ):
            raise HistoricalWebSearchBackfillError(
                "eligible_for_completion requires at least one successful append "
                "unless final_exit_reason is no_source_found."
            )
        for field_name, field_value in (
            ("search_attempt_count", self.search_attempt_count),
            ("search_success_count", self.search_success_count),
            ("search_failure_count", self.search_failure_count),
            ("scrape_attempt_count", self.scrape_attempt_count),
            ("scrape_success_count", self.scrape_success_count),
            ("scrape_failure_count", self.scrape_failure_count),
            ("append_attempt_count", self.append_attempt_count),
            ("append_rejected_count", self.append_rejected_count),
            ("append_failed_count", self.append_failed_count),
            ("append_terminal_count", self.append_terminal_count),
        ):
            if isinstance(field_value, bool) or not isinstance(field_value, int):
                raise HistoricalWebSearchBackfillError(
                    f"{field_name} must be an integer."
                )
            if field_value < 0:
                raise HistoricalWebSearchBackfillError(
                    f"{field_name} must be greater than or equal to zero."
                )
        if self.append_rejected_count > self.append_attempt_count:
            raise HistoricalWebSearchBackfillError(
                "append_rejected_count must not exceed append_attempt_count."
            )
        if self.append_failed_count > self.append_attempt_count:
            raise HistoricalWebSearchBackfillError(
                "append_failed_count must not exceed append_attempt_count."
            )
        if self.append_terminal_count > self.append_attempt_count:
            raise HistoricalWebSearchBackfillError(
                "append_terminal_count must not exceed append_attempt_count."
            )
        if not isinstance(self.collection_failure_details, tuple):
            raise HistoricalWebSearchBackfillError(
                "collection_failure_details must be a tuple of strings."
            )
        if not all(isinstance(item, str) for item in self.collection_failure_details):
            raise HistoricalWebSearchBackfillError(
                "collection_failure_details must contain only strings."
            )


def derive_historical_web_search_exit_reason(
    *,
    successful_append_count: int,
    search_attempt_count: int,
    search_success_count: int,
    search_failure_count: int,
    scrape_attempt_count: int,
    scrape_success_count: int,
    scrape_failure_count: int,
    append_attempt_count: int,
    append_failed_count: int,
    append_terminal_count: int,
) -> str:
    """Derive the terminal failure category from deterministic summary counts."""
    if successful_append_count > 0:
        return "successful_append"
    if append_failed_count > 0:
        return "append_tool_failed"
    if append_attempt_count > append_terminal_count:
        return "append_stage_interrupted"
    if append_terminal_count > 0:
        return "no_unique_candidates_appended"
    if search_attempt_count > 0 and search_success_count <= 0:
        if search_failure_count > 0:
            return "search_backend_failure_dominant"
        return "zero_successful_search_results"
    if scrape_attempt_count > 0 and scrape_success_count <= 0 and scrape_failure_count > 0:
        return "scrape_followups_failed"
    return "no_candidate_formed"


def derive_historical_web_search_root_cause_detail(
    *,
    final_exit_reason: str,
    failure_codes: tuple[str, ...],
) -> str:
    """Derive the concrete deterministic detail behind one terminal failure."""
    if final_exit_reason == "successful_append":
        return "successful_append"
    if final_exit_reason == "no_source_found":
        return "no_source_found"
    if final_exit_reason == "append_tool_failed":
        for code in failure_codes:
            normalized = code.strip()
            if normalized:
                return normalized
        return "append_tool_failed"
    if final_exit_reason == "append_stage_interrupted":
        return "incomplete_append_receipt"
    if final_exit_reason == "search_backend_failure_dominant":
        return "all_search_attempts_failed"
    if final_exit_reason == "zero_successful_search_results":
        return "zero_successful_search_results"
    if final_exit_reason == "scrape_followups_failed":
        return "all_scrape_followups_failed"
    if final_exit_reason == "no_unique_candidates_appended":
        return "no_unique_candidates_appended"
    return "no_candidate_formed"


type SearchCollectionRunner = Callable[..., HistoricalWebSearchCollectionResult]


@dataclass(frozen=True, slots=True)
class HistoricalWebSearchBackfillSlice:
    """One non-overlapping historical web-search acquisition slice."""

    start_at: datetime
    end_at: datetime
    query: str


@dataclass(frozen=True, slots=True)
class HistoricalWebSearchBackfillReceipt:
    """Observable receipt for one historical web-search backfill pass."""

    target_key: str
    window_start: datetime
    window_end: datetime
    slice_hours: int
    slices: tuple[HistoricalWebSearchBackfillSlice, ...]
    resume_skipped_slices: tuple[HistoricalWebSearchBackfillSlice, ...]
    write_receipts: tuple[HistoricalWebSearchWriteReceipt, ...]


@dataclass(frozen=True, slots=True)
class ArchivedWebSearchWindowReceipt:
    """One search collection pass archived into the historical web-search store."""

    target_key: str
    query: str
    window_start: datetime
    window_end: datetime
    discovered_at: datetime
    write_receipts: tuple[HistoricalWebSearchWriteReceipt, ...]


def backfill_historical_web_search(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    search_intent: str,
    prompt_profile_id: str,
    control_language: str,
    retrieval_languages: tuple[str, ...],
    window_start: datetime,
    window_end: datetime,
    slice_hours: int,
    run_search_agent: SearchCollectionRunner,
) -> HistoricalWebSearchBackfillReceipt:
    """Backfill one replay window of historical web-search material into the archive."""
    if not isinstance(layout, WorkspaceLayout):
        raise HistoricalWebSearchBackfillError(
            "layout must be a WorkspaceLayout instance."
        )
    validated_target_key = validate_target_key(
        target_key,
        error_type=HistoricalWebSearchBackfillError,
    )
    validated_search_intent = _validate_non_blank_text(
        search_intent,
        field_name="search_intent",
    )
    acquisition_provenance = _build_web_search_acquisition_provenance(
        search_intent=validated_search_intent,
        prompt_profile_id=_validate_non_blank_text(
            prompt_profile_id,
            field_name="prompt_profile_id",
        ),
        control_language=_validate_non_blank_text(
            control_language,
            field_name="control_language",
        ),
        retrieval_languages=_validate_non_blank_string_tuple(
            retrieval_languages,
            field_name="retrieval_languages",
        ),
    )
    validated_window_start = validate_timestamp(
        window_start,
        field_name="window_start",
        error_type=HistoricalWebSearchBackfillError,
    )
    validated_window_end = validate_timestamp(
        window_end,
        field_name="window_end",
        error_type=HistoricalWebSearchBackfillError,
    )
    if validated_window_end < validated_window_start:
        raise HistoricalWebSearchBackfillError(
            "window_end must be greater than or equal to window_start."
        )
    validated_slice_hours = _validate_positive_int(
        slice_hours,
        field_name="slice_hours",
    )
    if not callable(run_search_agent):
        raise HistoricalWebSearchBackfillError("run_search_agent must be callable.")

    slices = _build_backfill_slices(
        search_intent=validated_search_intent,
        window_start=validated_window_start,
        window_end=validated_window_end,
        slice_hours=validated_slice_hours,
    )
    completed_slice_keys = _load_completed_slice_keys(
        layout=layout,
        target_key=validated_target_key,
        window_start=validated_window_start,
        window_end=validated_window_end,
    )
    resume_skipped_slices: list[HistoricalWebSearchBackfillSlice] = []
    write_receipts: list[HistoricalWebSearchWriteReceipt] = []
    for slice_item in slices:
        slice_key = _slice_completion_key(
            query=slice_item.query,
            visible_at=slice_item.end_at,
        )
        if slice_key in completed_slice_keys:
            resume_skipped_slices.append(slice_item)
            continue
        collection_result = _run_historical_web_search_collection(
            target_key=validated_target_key,
            query=slice_item.query,
            discovered_at=slice_item.end_at,
            acquisition_provenance=acquisition_provenance,
            run_search_agent=run_search_agent,
        )
        _raise_if_ineligible_collection_result(collection_result)
        write_receipts.extend(collection_result.write_receipts)
        append_historical_web_search_slice_completion(
            layout,
            completion=HistoricalWebSearchSliceCompletion(
                target_key=validated_target_key,
                query=slice_item.query,
                visible_at=slice_item.end_at,
                outcome=(
                    "source_appended"
                    if collection_result.successful_append_count > 0
                    else "no_source_found"
                ),
                acquisition_provenance=acquisition_provenance,
            ),
        )

    return HistoricalWebSearchBackfillReceipt(
        target_key=validated_target_key,
        window_start=validated_window_start,
        window_end=validated_window_end,
        slice_hours=validated_slice_hours,
        slices=slices,
        resume_skipped_slices=tuple(resume_skipped_slices),
        write_receipts=tuple(write_receipts),
    )


def collect_archived_web_search_window_once(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    search_intent: str,
    window_start: datetime,
    window_end: datetime,
    run_search_agent: SearchCollectionRunner,
) -> ArchivedWebSearchWindowReceipt:
    """Collect exactly one window into the historical archive without re-slicing it."""
    if not isinstance(layout, WorkspaceLayout):
        raise HistoricalWebSearchBackfillError(
            "layout must be a WorkspaceLayout instance."
        )
    validated_target_key = validate_target_key(
        target_key,
        error_type=HistoricalWebSearchBackfillError,
    )
    validated_search_intent = _validate_non_blank_text(
        search_intent,
        field_name="search_intent",
    )
    validated_window_start = validate_timestamp(
        window_start,
        field_name="window_start",
        error_type=HistoricalWebSearchBackfillError,
    )
    validated_window_end = validate_timestamp(
        window_end,
        field_name="window_end",
        error_type=HistoricalWebSearchBackfillError,
    )
    if validated_window_end < validated_window_start:
        raise HistoricalWebSearchBackfillError(
            "window_end must be greater than or equal to window_start."
        )
    if not callable(run_search_agent):
        raise HistoricalWebSearchBackfillError("run_search_agent must be callable.")

    query = _render_window_query(
        search_intent=validated_search_intent,
        window_start=validated_window_start,
        window_end=validated_window_end,
    )
    collection_result = _run_historical_web_search_collection(
        target_key=validated_target_key,
        query=query,
        discovered_at=validated_window_end,
        run_search_agent=run_search_agent,
    )
    _raise_if_ineligible_collection_result(collection_result)
    return ArchivedWebSearchWindowReceipt(
        target_key=validated_target_key,
        query=query,
        window_start=validated_window_start,
        window_end=validated_window_end,
        discovered_at=validated_window_end,
        write_receipts=collection_result.write_receipts,
    )


def _run_historical_web_search_collection(
    *,
    target_key: str,
    query: str,
    discovered_at: datetime,
    acquisition_provenance: WebSearchAcquisitionProvenance | None = None,
    run_search_agent: SearchCollectionRunner,
) -> HistoricalWebSearchCollectionResult:
    run_kwargs = {
        "target_key": target_key,
        "query": query,
        "discovered_at": discovered_at,
    }
    if acquisition_provenance is not None and _supports_acquisition_provenance_kwarg(
        run_search_agent
    ):
        run_kwargs["acquisition_provenance"] = acquisition_provenance
    collection_result = run_search_agent(**run_kwargs)
    if not isinstance(collection_result, HistoricalWebSearchCollectionResult):
        raise HistoricalWebSearchBackfillError(
            "run_search_agent must return a HistoricalWebSearchCollectionResult."
        )
    return collection_result


def _raise_if_ineligible_collection_result(
    collection_result: HistoricalWebSearchCollectionResult,
) -> None:
    if not collection_result.eligible_for_completion:
        raise HistoricalWebSearchBackfillError(
            "historical web-search slice failed: "
            f"final_exit_reason={collection_result.final_exit_reason} "
            f"root_cause_detail={collection_result.root_cause_detail} "
            f"search_attempt_count={collection_result.search_attempt_count} "
            f"search_success_count={collection_result.search_success_count} "
            f"search_failure_count={collection_result.search_failure_count} "
            f"scrape_attempt_count={collection_result.scrape_attempt_count} "
            f"scrape_success_count={collection_result.scrape_success_count} "
            f"scrape_failure_count={collection_result.scrape_failure_count} "
            f"append_attempt_count={collection_result.append_attempt_count} "
            f"append_rejected_count={collection_result.append_rejected_count} "
            f"successful_append_count={collection_result.successful_append_count} "
            "collection_failure_details="
            f"{list(collection_result.collection_failure_details)}"
        )


def _build_backfill_slices(
    *,
    search_intent: str,
    window_start: datetime,
    window_end: datetime,
    slice_hours: int,
) -> tuple[HistoricalWebSearchBackfillSlice, ...]:
    step = timedelta(hours=slice_hours)
    cursor = window_start.astimezone(UTC)
    ceiling = window_end.astimezone(UTC)
    slices: list[HistoricalWebSearchBackfillSlice] = []
    while cursor <= ceiling:
        slice_end = min(cursor + step - timedelta(seconds=1), ceiling)
        slices.append(
            HistoricalWebSearchBackfillSlice(
                start_at=cursor,
                end_at=slice_end,
                query=_render_window_query(
                    search_intent=search_intent,
                    window_start=cursor,
                    window_end=slice_end,
                ),
            )
        )
        cursor = slice_end + timedelta(seconds=1)
    return tuple(slices)


def _render_window_query(
    *,
    search_intent: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    return (
        f"{search_intent}\n"
        f"window_start={window_start.isoformat()}\n"
        f"window_end={window_end.isoformat()}"
    )


def _load_completed_slice_keys(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    window_start: datetime,
    window_end: datetime,
) -> set[tuple[str, datetime]]:
    return {
        _slice_completion_key(query=record.query, visible_at=record.visible_at)
        for record in read_historical_web_search_slice_completions(
            layout,
            target_key=target_key,
            start_at=window_start,
            end_at=window_end,
        )
    }


def archive_historical_web_search_candidates(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    query: str,
    discovered_at: datetime,
    raw_candidates: tuple[Mapping[str, object], ...],
    resolve_published_at: PublishedAtResolver,
    acquisition_provenance: WebSearchAcquisitionProvenance | None = None,
) -> HistoricalWebSearchCollectionResult:
    if not isinstance(raw_candidates, tuple):
        raise HistoricalWebSearchBackfillError(
            "run_search_agent must return a tuple of canonical web-search payloads."
        )

    seen_source_refs: set[str] = set()
    receipts: list[HistoricalWebSearchWriteReceipt] = []
    boundary_rejection_reasons: list[str] = []
    for index, raw_candidate in enumerate(raw_candidates, start=1):
        receipt, rejection_reason, source_ref = archive_historical_web_search_candidate(
            layout=layout,
            target_key=target_key,
            query=query,
            discovered_at=discovered_at,
            raw_candidate=raw_candidate,
            resolve_published_at=resolve_published_at,
            acquisition_provenance=acquisition_provenance,
        )
        if rejection_reason is not None:
            boundary_rejection_reasons.append(f"candidate {index}: {rejection_reason}")
            continue
        if source_ref in seen_source_refs:
            continue
        seen_source_refs.add(source_ref)
        if receipt is None:
            raise HistoricalWebSearchBackfillError(
                "archive_historical_web_search_candidate must return a receipt "
                "when rejection_reason is None."
            )
        receipts.append(receipt)
    successful_append_count = sum(
        1 for item in receipts if item.status in {"written", "noop_existing"}
    )
    append_attempt_count = len(raw_candidates)
    append_rejected_count = len(boundary_rejection_reasons)
    final_exit_reason = derive_historical_web_search_exit_reason(
        successful_append_count=successful_append_count,
        search_attempt_count=0,
        search_success_count=0,
        search_failure_count=0,
        scrape_attempt_count=0,
        scrape_success_count=0,
        scrape_failure_count=0,
        append_attempt_count=append_attempt_count,
        append_failed_count=0,
        append_terminal_count=append_attempt_count,
    )
    return HistoricalWebSearchCollectionResult(
        write_receipts=tuple(receipts),
        boundary_rejection_reasons=tuple(boundary_rejection_reasons),
        eligible_for_completion=successful_append_count > 0,
        successful_append_count=successful_append_count,
        final_exit_reason=final_exit_reason,
        root_cause_detail=derive_historical_web_search_root_cause_detail(
            final_exit_reason=final_exit_reason,
            failure_codes=(),
        ),
        search_attempt_count=0,
        search_success_count=0,
        search_failure_count=0,
        scrape_attempt_count=0,
        scrape_success_count=0,
        scrape_failure_count=0,
        append_attempt_count=append_attempt_count,
        append_rejected_count=append_rejected_count,
        append_failed_count=0,
        append_terminal_count=append_attempt_count,
        collection_failure_details=(),
    )


def archive_historical_web_search_candidate(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    query: str,
    discovered_at: datetime,
    raw_candidate: Mapping[str, object],
    resolve_published_at: PublishedAtResolver,
    acquisition_provenance: WebSearchAcquisitionProvenance | None = None,
) -> tuple[HistoricalWebSearchWriteReceipt | None, str | None, str]:
    try:
        ingress_input, labels = shape_live_web_search_material(
            target_key=target_key,
            query=query,
            discovered_at=discovered_at,
            raw_payload=raw_candidate,
            require_exact_discovered_at=True,
        )
    except LiveWebSearchGatherError as exc:
        return None, str(exc), ""
    placeholder_rejection_reason = _placeholder_rejection_reason(
        source_ref=ingress_input.source_ref,
        title=ingress_input.title,
        content=ingress_input.content,
    )
    if placeholder_rejection_reason is not None:
        return None, placeholder_rejection_reason, ingress_input.source_ref

    if (
        ingress_input.published_at is not None
        and ingress_input.published_at > discovered_at
    ):
        return (
            None,
            "published_at="
            f"{ingress_input.published_at.isoformat()} is later than "
            f"slice_end={discovered_at.isoformat()}",
            ingress_input.source_ref,
        )

    published_at, rejection_reason = _resolve_candidate_published_at(
        source_ref=ingress_input.source_ref,
        agent_published_at=ingress_input.published_at,
        resolve_published_at=resolve_published_at,
    )
    if rejection_reason is not None:
        return None, rejection_reason, ingress_input.source_ref
    if published_at is not None:
        published_at = validate_timestamp(
            published_at,
            field_name="published_at",
            error_type=HistoricalWebSearchBackfillError,
        )
    if published_at is not None and published_at > discovered_at:
        return (
            None,
            "published_at="
            f"{published_at.isoformat()} is later than "
            f"slice_end={discovered_at.isoformat()}",
            ingress_input.source_ref,
        )
    visible_at = _resolve_replay_visible_at(
        source_ref=ingress_input.source_ref,
        title=ingress_input.title,
        published_at=published_at,
        discovered_at=discovered_at,
    )
    future_leakage = detect_historical_web_search_future_leakage(
        source_ref=ingress_input.source_ref,
        title=ingress_input.title,
        content=ingress_input.content,
        visible_at=visible_at,
    )
    if future_leakage is not None:
        return None, future_leakage.rejection_reason, ingress_input.source_ref
    write_receipt = write_historical_web_search_backfill_record(
        layout,
        target_key=target_key,
        query=ingress_input.query,
        source_ref=ingress_input.source_ref,
        title=ingress_input.title,
        content=ingress_input.content,
        labels=labels,
        published_at=published_at,
        discovered_at=discovered_at,
        visible_at=visible_at,
        acquisition_provenance=acquisition_provenance,
    )
    return (
        write_receipt,
        None,
        ingress_input.source_ref,
    )


def _resolve_candidate_published_at(
    *,
    source_ref: str,
    agent_published_at: datetime | None,
    resolve_published_at: PublishedAtResolver,
) -> tuple[datetime | None, str | None]:
    extracted_published_at: datetime | None = None
    if resolve_published_at is resolve_web_source_published_at:
        try:
            extracted_published_at = extract_exact_web_source_published_at(
                source_ref=source_ref,
            )
        except WebSourcePublishedAtResolutionError:
            extracted_published_at = None

    if extracted_published_at is not None:
        if (
            agent_published_at is not None
            and agent_published_at != extracted_published_at
        ):
            return None, (
                f"agent published_at={agent_published_at.isoformat()} does not match "
                "the deterministically extracted "
                f"published_at={extracted_published_at.isoformat()}"
            )
        return extracted_published_at, None

    if resolve_published_at is resolve_web_source_published_at:
        return agent_published_at, None

    try:
        resolved_published_at = resolve_published_at(
            source_ref=source_ref,
            agent_published_at=agent_published_at,
        )
        return resolved_published_at, None
    except (HistoricalWebSearchBackfillError, WebSourcePublishedAtResolutionError) as exc:
        if agent_published_at is None:
            return None, None
        return None, str(exc)


def _slice_completion_key(
    *,
    query: str,
    visible_at: datetime,
) -> tuple[str, datetime]:
    validated_query = _validate_non_blank_text(query, field_name="query")
    validated_visible_at = validate_timestamp(
        visible_at,
        field_name="visible_at",
        error_type=HistoricalWebSearchBackfillError,
    )
    return (validated_query, validated_visible_at)


def _resolve_replay_visible_at(
    *,
    source_ref: str,
    title: str,
    published_at: datetime | None,
    discovered_at: datetime,
) -> datetime:
    normalized_discovered_at = validate_timestamp(
        discovered_at,
        field_name="discovered_at",
        error_type=HistoricalWebSearchBackfillError,
    )
    if published_at is None:
        return normalized_discovered_at
    normalized_published_at = validate_timestamp(
        published_at,
        field_name="published_at",
        error_type=HistoricalWebSearchBackfillError,
    )
    if normalized_published_at > normalized_discovered_at:
        raise HistoricalWebSearchBackfillError(
            "published_at must not be later than discovered_at when deriving replay "
            "visible_at."
        )
    return normalized_published_at + _deterministic_replay_visible_jitter(
        source_ref=source_ref,
        title=title,
        published_at=normalized_published_at,
    )


def _deterministic_replay_visible_jitter(
    *,
    source_ref: str,
    title: str,
    published_at: datetime,
) -> timedelta:
    identity_material = "\n".join((source_ref, title, published_at.isoformat()))
    jitter_microseconds = (
        1
        + (
            int(sha256(identity_material.encode("utf-8")).hexdigest()[:8], 16)
            % 1000
        )
    )
    remaining_microseconds = 999_999 - published_at.microsecond
    if remaining_microseconds <= 0:
        return timedelta(0)
    return timedelta(
        microseconds=min(jitter_microseconds, remaining_microseconds)
    )


def _placeholder_rejection_reason(
    *,
    source_ref: str,
    title: str,
    content: str,
) -> str | None:
    normalized_source_ref = source_ref.strip().lower().rstrip("/")
    if normalized_source_ref in {
        "search://google/unavailable",
        "search://no-results-found",
        "no-sources-available",
    }:
        return "source_ref must identify a real source, not a placeholder marker."
    if not normalized_source_ref.startswith(("http://", "https://")):
        return "source_ref must be an http/https source URL for historical web-search."
    normalized_title = " ".join(title.strip().lower().split())
    normalized_content = " ".join(content.strip().lower().split())
    placeholder_texts = (
        "no sources available",
        "no source available",
        "no results found",
        "no search results found",
        "no matching sources found",
        "unable to retrieve any valid sources",
        "all search attempts returned errors",
        "future window relative to current date",
    )
    if any(text in normalized_title for text in placeholder_texts) or any(
        text in normalized_content for text in placeholder_texts
    ):
        return "candidate title/content must not be a no-source placeholder."
    return None


def _build_web_search_acquisition_provenance(
    *,
    search_intent: str,
    prompt_profile_id: str,
    control_language: str,
    retrieval_languages: tuple[str, ...],
) -> WebSearchAcquisitionProvenance:
    return WebSearchAcquisitionProvenance(
        prompt_profile_id=prompt_profile_id,
        search_intent_hash=sha256(search_intent.encode("utf-8")).hexdigest(),
        control_language=control_language,
        retrieval_languages=retrieval_languages,
    )


def _supports_acquisition_provenance_kwarg(run_search_agent: SearchCollectionRunner) -> bool:
    parameters = signature(run_search_agent).parameters.values()
    return any(
        parameter.kind is Parameter.VAR_KEYWORD
        or parameter.name == "acquisition_provenance"
        for parameter in parameters
    )


def _validate_non_blank_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise HistoricalWebSearchBackfillError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise HistoricalWebSearchBackfillError(f"{field_name} must not be blank.")
    return normalized


def _validate_non_blank_string_tuple(
    value: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise HistoricalWebSearchBackfillError(f"{field_name} must be a tuple[str, ...].")
    normalized = tuple(
        _validate_non_blank_text(item, field_name=field_name)
        for item in value
    )
    if not normalized:
        raise HistoricalWebSearchBackfillError(f"{field_name} must not be empty.")
    return normalized


def _validate_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalWebSearchBackfillError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise HistoricalWebSearchBackfillError(f"{field_name} must be greater than zero.")
    return value


__all__ = [
    "ArchivedWebSearchWindowReceipt",
    "HistoricalWebSearchBackfillError",
    "HistoricalWebSearchBackfillReceipt",
    "HistoricalWebSearchBackfillSlice",
    "HistoricalWebSearchCollectionResult",
    "archive_historical_web_search_candidate",
    "archive_historical_web_search_candidates",
    "backfill_historical_web_search",
    "collect_archived_web_search_window_once",
]
