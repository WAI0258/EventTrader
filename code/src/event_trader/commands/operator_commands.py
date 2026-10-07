"""Operator-triggered runtime commands.

These commands stay narrow: they compose existing read/write seams without
introducing a new truth surface or bypass path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from event_trader.contracts import (
    PageRef,
    ResearchMemoryContractError,
    ResearchScope,
    resolve_page_ref,
    resolve_scope,
)
from event_trader.contracts._validators import normalize_content, validate_event_id
from event_trader.feeds.manual import (
    adapt_manual_source,
)
from event_trader.feeds.payload_mappers import (
    adapt_macro_api,
    adapt_news_stream,
)
from event_trader.feeds.web_search import SearchAgentRunner
from event_trader.ingest.admission import (
    shape_admission_outputs,
    validate_admission_request,
)
from event_trader.operator_context import operator_context_path, replace_operator_context
from event_trader.research_memory import (
    FileBackedIndexLogWriter,
    ReceiptedResearchMemoryPageWriter,
    ResearchMemoryWriteAttribution,
)
from event_trader.runtime.bootstrap import LiveRuntimeHost
from event_trader.runtime.source_publisher import RuntimeRawSourcePublisher
from event_trader.storage import WorkspaceLayout
from event_trader.web_search_runtime import release_live_web_search_materials

type OperatorInjectionMode = Literal["evidence", "wiki"]
type OperatorWikiWriteMode = Literal[
    "replace_operator_context",
    "update_index",
    "append_log_entry",
    "update_page_section",
    "rewrite_page",
    "create_page",
]

_OPERATOR_WIKI_WRITE_MODES = frozenset(
    {
        "replace_operator_context",
        "update_index",
        "append_log_entry",
        "update_page_section",
        "rewrite_page",
        "create_page",
    }
)


class OperatorCommandError(ValueError):
    """Raised when an operator command dependency or execution is malformed."""


@dataclass(frozen=True, slots=True)
class OperatorInjectionReceipt:
    """Observable receipt for one operator-triggered material injection attempt."""

    injection_mode: OperatorInjectionMode
    target_key: str | None
    scope_key: str | None = None
    admitted_event_ids: tuple[str, ...] = ()
    artifact_path: Path | None = None
    page_path: str | None = None
    write_mode: OperatorWikiWriteMode | None = None
    authoritative_write_intent: bool = False
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if self.injection_mode not in {"evidence", "wiki"}:
            raise OperatorCommandError(
                "injection_mode must be one of the committed operator modes: "
                "'evidence' or 'wiki'."
            )

        object.__setattr__(
            self,
            "target_key",
            _normalize_optional_receipt_target_key(self.target_key),
        )

        if self.scope_key is not None:
            object.__setattr__(
                self,
                "scope_key",
                _normalize_receipt_context_value(
                    self.scope_key,
                    field_name="scope_key",
                ),
            )

        object.__setattr__(
            self,
            "admitted_event_ids",
            _normalize_receipt_event_ids(self.admitted_event_ids),
        )

        if self.artifact_path is not None and not isinstance(self.artifact_path, Path):
            raise OperatorCommandError("artifact_path must be a pathlib.Path.")

        if self.page_path is not None:
            object.__setattr__(
                self,
                "page_path",
                _normalize_receipt_context_value(
                    self.page_path,
                    field_name="page_path",
                ),
            )

        if self.write_mode is not None:
            object.__setattr__(
                self,
                "write_mode",
                _normalize_receipt_write_mode(self.write_mode),
            )

        if not isinstance(self.authoritative_write_intent, bool):
            raise OperatorCommandError("authoritative_write_intent must be a boolean.")

        if self.rejection_reason is not None:
            object.__setattr__(
                self,
                "rejection_reason",
                normalize_content(
                    self.rejection_reason,
                    field_name="rejection_reason",
                    error_type=OperatorCommandError,
                ),
            )
            if self.artifact_path is not None:
                raise OperatorCommandError(
                    "rejection receipts must not include artifact_path."
                )
            if self.injection_mode == "evidence":
                _validate_evidence_receipt(self, successful=False)
            else:
                _validate_wiki_receipt(self, successful=False)
            return

        if self.injection_mode == "evidence":
            _validate_evidence_receipt(self, successful=True)
            return

        _validate_wiki_receipt(self, successful=True)


def inject_manual_evidence(
    *,
    target_key: str,
    manual_payload: Mapping[str, object],
    live_host: LiveRuntimeHost,
    labels: list[str] | None = None,
    ts_init: datetime | None = None,
) -> OperatorInjectionReceipt:
    """Direct-release operator-curated admitted evidence through the live host.

    This seam is intentionally for already-curated operator input, not raw source
    acquisition.
    """
    if not isinstance(live_host, LiveRuntimeHost):
        raise OperatorCommandError("live_host must be a LiveRuntimeHost instance.")
    try:
        ingress_input = adapt_manual_source(manual_payload)
        request = validate_admission_request(
            target_key=target_key,
            ingress_input=ingress_input,
            labels=labels,
        )
        outputs = shape_admission_outputs(
            request,
            ts_init=datetime.now(tz=UTC) if ts_init is None else ts_init,
        )
        runtime_receipt = live_host.release_admitted(outputs)
    except OperatorCommandError:
        raise
    except Exception as exc:
        raise OperatorCommandError(str(exc)) from exc

    return OperatorInjectionReceipt(
        injection_mode="evidence",
        target_key=outputs.runtime_event.target_key,
        admitted_event_ids=(runtime_receipt.appended_event_id,),
        authoritative_write_intent=False,
    )


def inject_live_web_search(
    *,
    target_key: str,
    query: str,
    discovered_at: datetime,
    run_search_agent: SearchAgentRunner,
    raw_source_publisher: RuntimeRawSourcePublisher,
    historical_layout: WorkspaceLayout,
    ts_init: datetime | None = None,
) -> OperatorInjectionReceipt:
    """Acquire live web-search source material via archive write plus raw publish."""
    if not isinstance(raw_source_publisher, RuntimeRawSourcePublisher):
        raise OperatorCommandError(
            "raw_source_publisher must be a RuntimeRawSourcePublisher instance."
        )
    if not isinstance(historical_layout, WorkspaceLayout):
        raise OperatorCommandError("historical_layout must be a WorkspaceLayout instance.")
    try:
        batch_receipt = release_live_web_search_materials(
            target_key=target_key,
            query=query,
            discovered_at=discovered_at,
            run_search_agent=run_search_agent,
            ts_init=datetime.now(tz=UTC) if ts_init is None else ts_init,
            raw_source_publisher=raw_source_publisher,
            historical_layout=historical_layout,
        )
    except OperatorCommandError:
        raise
    except Exception as exc:
        raise OperatorCommandError(str(exc)) from exc

    admitted_event_ids = tuple(
        event_id
        for receipt in batch_receipt.raw_source_receipts
        for event_id in receipt.admitted_event_ids
    )
    if not admitted_event_ids:
        return OperatorInjectionReceipt(
            injection_mode="evidence",
            target_key=batch_receipt.target_key,
            admitted_event_ids=(),
            authoritative_write_intent=False,
            rejection_reason="live web_search returned no source materials.",
        )

    return OperatorInjectionReceipt(
        injection_mode="evidence",
        target_key=batch_receipt.target_key,
        admitted_event_ids=admitted_event_ids,
        authoritative_write_intent=False,
    )


def inject_live_macro_api(
    *,
    target_key: str,
    macro_payload: Mapping[str, object],
    live_host: LiveRuntimeHost,
    ts_init: datetime | None = None,
) -> OperatorInjectionReceipt:
    """Direct-release operator-curated macro/event material through admission.

    This operator seam accepts already-shaped admitted material rather than
    performing raw source acquisition.
    """
    if not isinstance(live_host, LiveRuntimeHost):
        raise OperatorCommandError("live_host must be a LiveRuntimeHost instance.")
    try:
        ingress_input = adapt_macro_api(macro_payload)
        request = validate_admission_request(
            target_key=target_key,
            ingress_input=ingress_input,
            labels=["event:macro_release"],
        )
        outputs = shape_admission_outputs(
            request,
            ts_init=datetime.now(tz=UTC) if ts_init is None else ts_init,
        )
        runtime_receipt = live_host.release_admitted(outputs)
    except OperatorCommandError:
        raise
    except Exception as exc:
        raise OperatorCommandError(str(exc)) from exc

    return OperatorInjectionReceipt(
        injection_mode="evidence",
        target_key=outputs.runtime_event.target_key,
        admitted_event_ids=(runtime_receipt.appended_event_id,),
        authoritative_write_intent=False,
    )


def inject_live_news_stream(
    *,
    target_key: str,
    stream_payload: Mapping[str, object],
    live_host: LiveRuntimeHost,
    labels: list[str] | None = None,
    ts_init: datetime | None = None,
) -> OperatorInjectionReceipt:
    """Direct-release operator-curated news-stream material through admission.

    This operator seam accepts already-shaped admitted material rather than
    performing raw source acquisition.
    """
    if not isinstance(live_host, LiveRuntimeHost):
        raise OperatorCommandError("live_host must be a LiveRuntimeHost instance.")
    try:
        ingress_input = adapt_news_stream(stream_payload)
        request = validate_admission_request(
            target_key=target_key,
            ingress_input=ingress_input,
            labels=labels,
        )
        outputs = shape_admission_outputs(
            request,
            ts_init=datetime.now(tz=UTC) if ts_init is None else ts_init,
        )
        runtime_receipt = live_host.release_admitted(outputs)
    except OperatorCommandError:
        raise
    except Exception as exc:
        raise OperatorCommandError(str(exc)) from exc

    return OperatorInjectionReceipt(
        injection_mode="evidence",
        target_key=outputs.runtime_event.target_key,
        admitted_event_ids=(runtime_receipt.appended_event_id,),
        authoritative_write_intent=False,
    )


def inject_authoritative_wiki(
    *,
    scope_key: str,
    write_mode: OperatorWikiWriteMode,
    content_md: str,
    layout: WorkspaceLayout,
    authoritative_write_intent: bool = False,
    page_path: str | None = None,
    section_name: str | None = None,
    operator_command_id: str | None = None,
    business_at: datetime | None = None,
) -> OperatorInjectionReceipt:
    """Inject authoritative durable knowledge through approved wiki seams only."""
    if not isinstance(layout, WorkspaceLayout):
        raise OperatorCommandError("layout must be a WorkspaceLayout instance.")
    if not isinstance(authoritative_write_intent, bool):
        raise OperatorCommandError("authoritative_write_intent must be a boolean.")

    attempted_scope_key = _normalize_receipt_context_value(
        scope_key,
        field_name="scope_key",
    )
    attempted_write_mode = _normalize_receipt_write_mode(write_mode)
    attempted_page_path = (
        None
        if page_path is None
        else _normalize_receipt_context_value(page_path, field_name="page_path")
    )

    scope = resolve_scope(layout, attempted_scope_key)
    resolved_page_path, artifact_path = _execute_operator_wiki_write(
        scope=scope,
        write_mode=attempted_write_mode,
        content_md=content_md,
        layout=layout,
        authoritative_write_intent=authoritative_write_intent,
        page_path=attempted_page_path,
        section_name=section_name,
        operator_command_id=operator_command_id,
        business_at=business_at,
    )

    return OperatorInjectionReceipt(
        injection_mode="wiki",
        target_key=scope.target_key,
        scope_key=scope.scope_key,
        artifact_path=artifact_path,
        page_path=resolved_page_path,
        write_mode=attempted_write_mode,
        authoritative_write_intent=True,
    )


def _execute_operator_wiki_write(
    *,
    scope: ResearchScope,
    write_mode: OperatorWikiWriteMode,
    content_md: str,
    layout: WorkspaceLayout,
    authoritative_write_intent: bool,
    page_path: str | None,
    section_name: str | None,
    operator_command_id: str | None,
    business_at: datetime | None,
) -> tuple[str, Path]:
    if write_mode == "replace_operator_context":
        _ensure_wrapper_write_inputs(
            write_mode=write_mode,
            page_path=page_path,
            section_name=section_name,
        )
        if scope.scope_kind != "target" or scope.target_key is None:
            raise ResearchMemoryContractError(
                "replace_operator_context requires a target-scoped scope_key."
            )
        replace_operator_context(
            layout=layout,
            target_key=scope.target_key,
            content_md=content_md,
        )
        return (
            f"targets/{scope.target_key}/operator.md",
            operator_context_path(layout=layout, target_key=scope.target_key),
        )

    _ensure_authoritative_wiki_intent(authoritative_write_intent)
    wrapper = FileBackedIndexLogWriter(layout)
    committed_at = datetime.now(UTC)

    if write_mode == "update_index":
        _ensure_wrapper_write_inputs(
            write_mode=write_mode,
            page_path=page_path,
            section_name=section_name,
        )
        artifact_path = wrapper.update_index(
            scope_key=scope.scope_key,
            new_content_md=content_md,
        )
        return _logical_page_path_for_wrapper_write(
            scope=scope,
            write_mode=write_mode,
        ), artifact_path

    if write_mode == "append_log_entry":
        _ensure_wrapper_write_inputs(
            write_mode=write_mode,
            page_path=page_path,
            section_name=section_name,
        )
        artifact_path = wrapper.append_log_entry(
            scope_key=scope.scope_key,
            entry_md=content_md,
        )
        return _logical_page_path_for_wrapper_write(
            scope=scope,
            write_mode=write_mode,
        ), artifact_path

    if page_path is None:
        raise ResearchMemoryContractError(
            f"{write_mode} requires an explicit canonical page_path."
        )

    page_ref = resolve_page_ref(page_path)
    _ensure_page_path_matches_scope(page_ref=page_ref, scope=scope)
    _ensure_operator_wiki_page_allowed(page_ref=page_ref)
    writer = ReceiptedResearchMemoryPageWriter(
        layout=layout,
        attribution=ResearchMemoryWriteAttribution(
            actor_type="operator",
            actor_id=(
                operator_command_id
                or _default_operator_command_id(
                    scope=scope,
                    write_mode=write_mode,
                    page_path=page_ref.page_path,
                )
            ),
            business_at=business_at or committed_at,
            committed_at=committed_at,
        ),
    )

    if write_mode == "update_page_section":
        normalized_section_name = _normalize_required_section_name(section_name)
        artifact_path = writer.update_page_section(
            page_path=page_ref.page_path,
            section_name=normalized_section_name,
            new_content_md=content_md,
        )
        return page_ref.page_path, artifact_path

    if section_name is not None:
        raise ResearchMemoryContractError(
            f"{write_mode} must not include section_name; only update_page_section "
            "rewrites one named section."
        )

    if write_mode == "rewrite_page":
        artifact_path = writer.rewrite_page(
            page_path=page_ref.page_path,
            new_content_md=content_md,
        )
        return page_ref.page_path, artifact_path

    artifact_path = writer.create_page(
        page_path=page_ref.page_path,
        initial_content_md=content_md,
    )
    return page_ref.page_path, artifact_path


def _default_operator_command_id(
    *,
    scope: ResearchScope,
    write_mode: OperatorWikiWriteMode,
    page_path: str,
) -> str:
    return f"operator-wiki:{scope.scope_key}:{write_mode}:{page_path}"



def _validate_evidence_receipt(
    receipt: OperatorInjectionReceipt,
    *,
    successful: bool,
) -> None:
    if receipt.target_key is None:
        raise OperatorCommandError(
            "target_key must be provided for evidence injection receipts."
        )
    if receipt.scope_key is not None:
        raise OperatorCommandError(
            "scope_key is not used for evidence injection receipts."
        )
    if receipt.page_path is not None:
        raise OperatorCommandError(
            "page_path is not used for evidence injection receipts."
        )
    if receipt.write_mode is not None:
        raise OperatorCommandError(
            "write_mode is not used for evidence injection receipts."
        )
    if receipt.artifact_path is not None:
        raise OperatorCommandError(
            "artifact_path is not used for evidence injection receipts."
        )
    if receipt.authoritative_write_intent:
        raise OperatorCommandError(
            "evidence injection must report authoritative_write_intent=False "
            "because it reuses admission rather than a direct truth write path."
        )
    if successful and not receipt.admitted_event_ids:
        raise OperatorCommandError(
            "successful evidence injection receipts must include admitted_event_ids."
        )
    if not successful and receipt.admitted_event_ids:
        raise OperatorCommandError(
            "rejection receipts must not include admitted_event_ids."
        )



def _validate_wiki_receipt(
    receipt: OperatorInjectionReceipt,
    *,
    successful: bool,
) -> None:
    if receipt.scope_key is None:
        raise OperatorCommandError(
            "scope_key must be provided for wiki injection receipts."
        )
    if receipt.write_mode is None:
        raise OperatorCommandError(
            "write_mode must be provided for wiki injection receipts."
        )
    if receipt.admitted_event_ids:
        raise OperatorCommandError(
            "wiki injection receipts must not include admitted_event_ids."
        )
    if successful:
        if receipt.page_path is None:
            raise OperatorCommandError(
                "successful wiki injection receipts must include page_path."
            )
        if receipt.artifact_path is None:
            raise OperatorCommandError(
                "successful wiki injection receipts must include artifact_path."
            )
        if not receipt.authoritative_write_intent:
            raise OperatorCommandError(
                "wiki injection must report authoritative_write_intent=True "
                "because it is a direct durable knowledge write path."
            )



def _normalize_optional_receipt_target_key(value: str | None) -> str | None:
    if value is None:
        return None
    return _normalize_receipt_context_value(value, field_name="target_key")



def _normalize_receipt_context_value(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise OperatorCommandError(f"{field_name} must be a string.")

    normalized = value.strip()
    if not normalized:
        raise OperatorCommandError(f"{field_name} must not be blank.")
    if normalized != value:
        raise OperatorCommandError(
            f"{field_name} must not include leading or trailing whitespace."
        )
    if any(ch in normalized for ch in ("\n", "\r")):
        raise OperatorCommandError(f"{field_name} must stay on one line.")
    return normalized



def _normalize_receipt_write_mode(
    write_mode: OperatorWikiWriteMode,
) -> OperatorWikiWriteMode:
    if not isinstance(write_mode, str):
        raise OperatorCommandError("write_mode must be a string.")
    if write_mode not in _OPERATOR_WIKI_WRITE_MODES:
        allowed = ", ".join(sorted(_OPERATOR_WIKI_WRITE_MODES))
        raise OperatorCommandError(
            f"write_mode must be one of the approved operator wiki modes: {allowed}."
        )
    return write_mode



def _normalize_receipt_event_ids(event_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(event_ids, tuple):
        raise OperatorCommandError(
            "admitted_event_ids must be a tuple of evidence event ids."
        )

    normalized_ids: list[str] = []
    seen: set[str] = set()
    for event_id in event_ids:
        validated_event_id = validate_event_id(
            event_id,
            error_type=OperatorCommandError,
        )
        if validated_event_id in seen:
            raise OperatorCommandError(
                "admitted_event_ids must not contain duplicates."
            )
        seen.add(validated_event_id)
        normalized_ids.append(validated_event_id)

    return tuple(normalized_ids)



def _ensure_authoritative_wiki_intent(authoritative_write_intent: bool) -> None:
    if authoritative_write_intent:
        return
    raise ResearchMemoryContractError(
        "authoritative_write_intent must be True for operator wiki injection because "
        "this seam is reserved for explicit operator/admin durable knowledge writes."
    )



def _ensure_wrapper_write_inputs(
    *,
    write_mode: OperatorWikiWriteMode,
    page_path: str | None,
    section_name: str | None,
) -> None:
    if page_path is not None:
        raise ResearchMemoryContractError(
            f"{write_mode} must not include page_path; it writes through scope_key only."
        )
    if section_name is not None:
        raise ResearchMemoryContractError(
            f"{write_mode} must not include section_name."
        )



def _normalize_required_section_name(section_name: str | None) -> str:
    if section_name is None:
        raise ResearchMemoryContractError(
            "update_page_section requires a non-blank section_name."
        )
    return _normalize_receipt_context_value(section_name, field_name="section_name")



def _ensure_page_path_matches_scope(*, page_ref: PageRef, scope: ResearchScope) -> None:
    if scope.scope_kind == "shared":
        if page_ref.target_key is not None:
            raise ResearchMemoryContractError(
                "page_path must stay inside the explicitly selected shared scope."
            )
        return

    if page_ref.target_key != scope.target_key:
        raise ResearchMemoryContractError(
            "page_path must stay inside the explicitly selected target scope."
        )



def _ensure_operator_wiki_page_allowed(*, page_ref: PageRef) -> None:
    if page_ref.page_kind == "operator":
        raise ResearchMemoryContractError(
            "operator.md only accepts the replace_operator_context write mode."
        )
    if page_ref.page_kind == "review":
        raise ResearchMemoryContractError(
            "Operator wiki injection must not target reviews/ pages; reflection "
            "owns the episodic review loop."
        )



def _logical_page_path_for_wrapper_write(
    *,
    scope: ResearchScope,
    write_mode: OperatorWikiWriteMode,
) -> str:
    file_name = "index.md" if write_mode == "update_index" else "log.md"
    if scope.scope_kind == "shared":
        return f"shared/{file_name}"
    return f"targets/{scope.target_key}/{file_name}"



def _target_key_for_wiki_receipt(
    scope: ResearchScope | None,
    page_path: str | None,
) -> str | None:
    if scope is not None:
        return scope.target_key
    if page_path is None:
        return None
    try:
        return resolve_page_ref(page_path).target_key
    except ResearchMemoryContractError:
        return None


__all__ = [
    "OperatorCommandError",
    "OperatorInjectionMode",
    "OperatorInjectionReceipt",
    "OperatorWikiWriteMode",
    "inject_live_macro_api",
    "inject_live_news_stream",
    "inject_authoritative_wiki",
    "inject_live_web_search",
    "inject_manual_evidence",
]
