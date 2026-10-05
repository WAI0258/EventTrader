"""Build replay-ready JSONL input from archived sources and manual inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from event_trader.contracts._validators import (
    validate_labels,
    validate_target_key,
    validate_timestamp,
)
from event_trader.feeds.models import (
    HistoricalMacroApiInput,
    HistoricalManualDatasetInput,
    HistoricalMarketNewsInput,
    HistoricalNewsStreamInput,
    HistoricalWebSearchInput,
    ReplayIngressInput,
    ReplaySourceShape,
)
from event_trader.feeds.payload_mappers import replay_adapter_for
from event_trader.ingest.admission import derive_admission_event_id
from event_trader.replay.admissibility import validate_replay_admission_request
from event_trader.replay.timestamps import replay_ts_event
from event_trader.source_archive.market_news import (
    read_market_news_records,
)
from event_trader.source_archive.web_search import (
    WebSearchAcquisitionProvenance,
    read_historical_web_search_records,
)
from event_trader.source_release import (
    select_archived_market_news_records,
    select_archived_web_search_records,
)
from event_trader.source_policy import (
    ensure_source_classification_labels,
    source_classification_counts,
    validate_source_classification_labels,
)
from event_trader.storage import WorkspaceLayout

_TIMESTAMP_FIELDS = frozenset(
    {
        "published_at",
        "captured_at",
        "updated_at",
        "released_at",
        "provided_at",
        "visible_at",
        "discovered_at",
    }
)
_ARCHIVED_WEB_SEARCH_READ_FLOOR = datetime.min.replace(tzinfo=UTC)
_ARCHIVED_MARKET_NEWS_READ_FLOOR = datetime.min.replace(tzinfo=UTC)
_MANUAL_SOURCE_SHAPE: ReplaySourceShape = "historical_manual_dataset"


class ReplayBuildError(RuntimeError):
    """Raised when replay input building cannot complete deterministically."""


@dataclass(frozen=True, slots=True)
class ReplayBuildInput:
    """Selection parameters for one replay input build."""

    target_key: str
    start_at: datetime
    end_at: datetime
    include_archived_web_search: bool = False
    include_archived_market_news: bool = False
    manual_input_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ReplayBuildError),
        )
        start_at = validate_timestamp(
            self.start_at,
            field_name="start_at",
            error_type=ReplayBuildError,
        )
        end_at = validate_timestamp(
            self.end_at,
            field_name="end_at",
            error_type=ReplayBuildError,
        )
        if end_at < start_at:
            raise ReplayBuildError("end_at must be greater than or equal to start_at.")
        object.__setattr__(self, "start_at", start_at)
        object.__setattr__(self, "end_at", end_at)
        if (
            not self.include_archived_web_search
            and not self.include_archived_market_news
            and self.manual_input_path is None
        ):
            raise ReplayBuildError(
                "Replay build requires at least one selected input surface."
            )
        if self.manual_input_path is not None and not isinstance(
            self.manual_input_path,
            Path,
        ):
            raise ReplayBuildError("manual_input_path must be a pathlib.Path when set.")


@dataclass(frozen=True, slots=True)
class ReplayBuildReport:
    """Narrow structured report for one replay build."""

    target_key: str
    start_at: datetime
    end_at: datetime
    included_source_kinds: tuple[str, ...]
    input_counts_by_source_kind: tuple[tuple[str, int], ...]
    source_classification_counts: tuple[
        tuple[str, tuple[tuple[str, int], ...]],
        ...,
    ]
    archived_web_search_acquisition_provenance_mix: tuple[tuple[str, int], ...]
    suppressed_duplicate_row_count: int
    duplicate_rows_collapsed: int
    output_row_count: int


@dataclass(frozen=True, slots=True)
class ReplayBuildOutput:
    """Structured replay rows plus report metadata."""

    target_key: str
    start_at: datetime
    end_at: datetime
    rows: tuple[dict[str, object], ...]
    report: ReplayBuildReport


@dataclass(frozen=True, slots=True)
class ReplayBuildArtifacts:
    """Filesystem artifacts emitted by one replay build."""

    output_path: Path
    report_path: Path | None = None


@dataclass(frozen=True, slots=True)
class _BuilderRow:
    ingress_input: ReplayIngressInput
    labels: tuple[str, ...]
    source_kind: str


def build_replay_input(
    layout: WorkspaceLayout,
    build_input: ReplayBuildInput,
) -> ReplayBuildOutput:
    """Build replay-ready dataset rows from archived sources and manual rows."""
    if not isinstance(layout, WorkspaceLayout):
        raise ReplayBuildError("layout must be a WorkspaceLayout instance.")
    if not isinstance(build_input, ReplayBuildInput):
        raise ReplayBuildError("build_input must be a ReplayBuildInput instance.")

    loaded_rows: list[_BuilderRow] = []
    input_counts: dict[str, int] = {}
    suppressed_duplicate_row_count = 0
    duplicate_rows_collapsed = 0
    archived_web_search_acquisition_provenance_mix: tuple[tuple[str, int], ...] = ()

    if build_input.include_archived_web_search:
        (
            archive_rows,
            suppressed_count,
            raw_count,
            archived_web_search_acquisition_provenance_mix,
        ) = _load_archived_web_search_rows(
            layout=layout,
            target_key=build_input.target_key,
            start_at=build_input.start_at,
            end_at=build_input.end_at,
        )
        loaded_rows.extend(archive_rows)
        input_counts["archived_web_search"] = raw_count
        suppressed_duplicate_row_count += suppressed_count

    if build_input.include_archived_market_news:
        market_news_rows, suppressed_count, raw_count = _load_archived_market_news_rows(
            layout=layout,
            target_key=build_input.target_key,
            start_at=build_input.start_at,
            end_at=build_input.end_at,
        )
        loaded_rows.extend(market_news_rows)
        input_counts["archived_market_news"] = raw_count
        suppressed_duplicate_row_count += suppressed_count

    if build_input.manual_input_path is not None:
        manual_rows = _load_manual_rows(
            manual_input_path=build_input.manual_input_path,
            target_key=build_input.target_key,
            start_at=build_input.start_at,
            end_at=build_input.end_at,
        )
        loaded_rows.extend(manual_rows)
        input_counts["manual_jsonl"] = len(manual_rows)

    deduped_rows: dict[str, dict[str, object]] = {}
    normalized_rows: list[tuple[_BuilderRow, dict[str, object]]] = []
    for row in loaded_rows:
        normalized_payload = _serialize_dataset_row(
            ingress_input=row.ingress_input,
            labels=row.labels,
        )
        event_id = _replay_identity(
            target_key=build_input.target_key,
            ingress_input=row.ingress_input,
            labels=row.labels,
        )
        existing_payload = deduped_rows.get(event_id)
        if existing_payload is not None:
            if existing_payload != normalized_payload:
                raise ReplayBuildError(
                    "Replay build found conflicting duplicate identity "
                    f"event_id={event_id} source_ref={row.ingress_input.source_ref}."
                )
            duplicate_rows_collapsed += 1
            continue
        deduped_rows[event_id] = normalized_payload
        normalized_rows.append((row, normalized_payload))

    if not normalized_rows:
        raise ReplayBuildError("Replay build selected zero rows for the requested window.")

    sorted_rows = tuple(
        payload
        for _row, payload in sorted(
            normalized_rows,
            key=lambda item: (
                replay_ts_event(item[0].ingress_input),
                item[0].ingress_input.source_ref,
                item[0].ingress_input.source_shape,
            ),
        )
    )

    report = ReplayBuildReport(
        target_key=build_input.target_key,
        start_at=build_input.start_at,
        end_at=build_input.end_at,
        included_source_kinds=tuple(sorted(input_counts)),
        input_counts_by_source_kind=tuple(
            (source_kind, input_counts[source_kind])
            for source_kind in sorted(input_counts)
        ),
        source_classification_counts=source_classification_counts(
            [_cast_labels(payload.get("labels", [])) for payload in sorted_rows]
        ),
        archived_web_search_acquisition_provenance_mix=(
            archived_web_search_acquisition_provenance_mix
        ),
        suppressed_duplicate_row_count=suppressed_duplicate_row_count,
        duplicate_rows_collapsed=duplicate_rows_collapsed,
        output_row_count=len(sorted_rows),
    )
    return ReplayBuildOutput(
        target_key=build_input.target_key,
        start_at=build_input.start_at,
        end_at=build_input.end_at,
        rows=sorted_rows,
        report=report,
    )


def write_replay_input(
    layout: WorkspaceLayout,
    build_output: ReplayBuildOutput,
    *,
    output_path: Path | None = None,
    write_report: bool = False,
) -> ReplayBuildArtifacts:
    """Write one replay-ready dataset and optional report artifact."""
    if not isinstance(layout, WorkspaceLayout):
        raise ReplayBuildError("layout must be a WorkspaceLayout instance.")
    if not isinstance(build_output, ReplayBuildOutput):
        raise ReplayBuildError("build_output must be a ReplayBuildOutput instance.")

    resolved_output_path = (
        _default_output_path(layout=layout, build_output=build_output)
        if output_path is None
        else output_path.resolve(strict=False)
    )
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_output_path.write_text(
        "".join(
            json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n"
            for row in build_output.rows
        ),
        encoding="utf-8",
    )

    report_path: Path | None = None
    if write_report:
        report_path = resolved_output_path.with_suffix(".report.md")
        report_path.write_text(
            render_replay_build_report(build_output.report),
            encoding="utf-8",
        )

    return ReplayBuildArtifacts(
        output_path=resolved_output_path,
        report_path=report_path,
    )


def render_replay_build_report(report: ReplayBuildReport) -> str:
    """Render one narrow replay-build report artifact."""
    if not isinstance(report, ReplayBuildReport):
        raise ReplayBuildError("report must be a ReplayBuildReport instance.")

    lines = [
        "# Replay Build Report",
        "",
        f"- Target: `{report.target_key}`",
        f"- Window Start: `{_format_timestamp(report.start_at)}`",
        f"- Window End: `{_format_timestamp(report.end_at)}`",
        f"- Included Source Kinds: {', '.join(report.included_source_kinds) or '(none)' }",
        f"- Suppressed Duplicate Rows: `{report.suppressed_duplicate_row_count}`",
        f"- Duplicate Rows Collapsed: `{report.duplicate_rows_collapsed}`",
        f"- Output Row Count: `{report.output_row_count}`",
        "",
        "## Input Counts",
        "",
    ]
    for source_kind, count in report.input_counts_by_source_kind:
        lines.append(f"- `{source_kind}`: `{count}`")
    lines.extend(("", "## Source Classification Mix", ""))
    for family, counts in report.source_classification_counts:
        lines.append(f"### {family}")
        lines.append("")
        for value, count in counts:
            lines.append(f"- `{family}:{value}`: `{count}`")
        lines.append("")
    lines.extend(("## Acquisition Provenance Mix", ""))
    if report.archived_web_search_acquisition_provenance_mix:
        for value, count in report.archived_web_search_acquisition_provenance_mix:
            lines.append(f"- `{value}`: `{count}`")
    else:
        lines.append("- `(none)`")
    lines.append("")
    return "\n".join(lines)


def _load_archived_web_search_rows(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
) -> tuple[tuple[_BuilderRow, ...], int, int, tuple[tuple[str, int], ...]]:
    records = read_historical_web_search_records(
        layout,
        target_key=target_key,
        start_at=_ARCHIVED_WEB_SEARCH_READ_FLOOR,
        end_at=end_at,
    )
    release_batch = select_archived_web_search_records(records, layout=layout)
    rows: list[_BuilderRow] = []
    acquisition_provenance_counts: dict[str, int] = {}
    for decision in release_batch.decisions:
        record = decision.released.payload
        visible_at = decision.released.release_at
        if visible_at < start_at or visible_at > end_at:
            continue
        provenance_key = _format_web_search_acquisition_provenance(
            record.acquisition_provenance
        )
        acquisition_provenance_counts[provenance_key] = (
            acquisition_provenance_counts.get(provenance_key, 0) + 1
        )
        payload = {
            "query": record.query,
            "source_ref": record.source_ref,
            "title": record.title,
            "content": record.content,
            "published_at": record.published_at,
            "discovered_at": record.discovered_at,
            "visible_at": visible_at,
        }
        ingress_input = replay_adapter_for("historical_web_search")(payload)
        rows.append(
            _BuilderRow(
                ingress_input=ingress_input,
                labels=tuple(record.labels),
                source_kind="archived_web_search",
            )
        )
    return (
        tuple(rows),
        release_batch.suppressed_duplicate_count,
        len(records),
        tuple(
            (value, acquisition_provenance_counts[value])
            for value in sorted(acquisition_provenance_counts)
        ),
    )


def _load_archived_market_news_rows(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
) -> tuple[tuple[_BuilderRow, ...], int, int]:
    records = read_market_news_records(
        layout,
        target_key=target_key,
        start_at=_ARCHIVED_MARKET_NEWS_READ_FLOOR,
        end_at=end_at,
    )
    release_batch = select_archived_market_news_records(records, layout=layout)
    rows: list[_BuilderRow] = []
    for decision in release_batch.decisions:
        record = decision.released.payload
        visible_at = decision.released.release_at
        if visible_at < start_at or visible_at > end_at:
            continue
        payload = {
            "source_ref": record.source_ref,
            "headline": record.headline,
            "body": record.content_text,
            "published_at": record.created_at,
            "updated_at": record.updated_at,
            "captured_at": record.captured_at,
            "visible_at": visible_at,
        }
        ingress_input = replay_adapter_for("historical_market_news")(payload)
        rows.append(
            _BuilderRow(
                ingress_input=ingress_input,
                labels=tuple(record.labels),
                source_kind="archived_market_news",
            )
        )
    return tuple(rows), release_batch.suppressed_duplicate_count, len(records)


def _format_web_search_acquisition_provenance(
    provenance: WebSearchAcquisitionProvenance | None,
) -> str:
    if provenance is None:
        return "legacy_unset"
    retrieval_languages = ",".join(provenance.retrieval_languages)
    return (
        f"prompt_profile_id={provenance.prompt_profile_id} | "
        f"search_intent_hash={provenance.search_intent_hash} | "
        f"control_language={provenance.control_language} | "
        f"retrieval_languages={retrieval_languages}"
    )


def _load_manual_rows(
    *,
    manual_input_path: Path,
    target_key: str,
    start_at: datetime,
    end_at: datetime,
) -> tuple[_BuilderRow, ...]:
    resolved_path = manual_input_path.resolve(strict=False)
    if not resolved_path.exists() or not resolved_path.is_file():
        raise ReplayBuildError(f"Manual input file does not exist: {resolved_path}")

    rows: list[_BuilderRow] = []
    for line_number, raw_payload in enumerate(
        _iter_jsonl_objects(resolved_path),
        start=1,
    ):
        source_shape = raw_payload.get("source_shape")
        if source_shape != _MANUAL_SOURCE_SHAPE:
            raise ReplayBuildError(
                "Manual input currently supports only "
                f"{_MANUAL_SOURCE_SHAPE!r}; line={line_number} path={resolved_path}."
            )
        labels = _extract_optional_labels(
            raw_payload=raw_payload,
            line_number=line_number,
            source_path=resolved_path,
        )
        normalized_payload = _coerce_timestamp_fields(
            _strip_builder_fields(raw_payload),
            line_number=line_number,
            source_path=resolved_path,
        )
        ingress_input = replay_adapter_for(_MANUAL_SOURCE_SHAPE)(normalized_payload)
        visible_at = replay_ts_event(ingress_input)
        if visible_at < start_at or visible_at > end_at:
            continue
        _ = _replay_identity(
            target_key=target_key,
            ingress_input=ingress_input,
            labels=labels,
        )
        rows.append(
            _BuilderRow(
                ingress_input=ingress_input,
                labels=labels,
                source_kind="manual_jsonl",
            )
        )
    return tuple(rows)


def _strip_builder_fields(raw_payload: dict[str, object]) -> dict[str, object]:
    payload = dict(raw_payload)
    payload.pop("source_shape", None)
    payload.pop("labels", None)
    return payload


def _extract_optional_labels(
    *,
    raw_payload: dict[str, object],
    line_number: int,
    source_path: Path,
) -> tuple[str, ...]:
    raw_labels = raw_payload.get("labels")
    if raw_labels is None:
        return ()
    if not isinstance(raw_labels, list):
        raise ReplayBuildError(
            "Manual input field 'labels' must be a list[str] when present; "
            f"line={line_number} path={source_path}."
        )
    return tuple(
        validate_labels(raw_labels, error_type=ReplayBuildError)
    )


def _coerce_timestamp_fields(
    payload: dict[str, object],
    *,
    line_number: int,
    source_path: Path,
) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in payload.items():
        if key in _TIMESTAMP_FIELDS and value is not None:
            if not isinstance(value, str):
                raise ReplayBuildError(
                    f"{source_path} line {line_number} field {key!r} must be an ISO8601 string."
                )
            normalized[key] = _parse_timestamp(
                value,
                source_path=source_path,
                line_number=line_number,
            )
            continue
        normalized[key] = value
    return normalized


def _iter_jsonl_objects(source_path: Path) -> tuple[dict[str, object], ...]:
    payloads: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(
        source_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ReplayBuildError(
                f"JSONL line is not valid JSON: path={source_path} line={line_number}."
            ) from exc
        if not isinstance(payload, dict):
            raise ReplayBuildError(
                f"JSONL lines must decode to JSON objects: path={source_path} line={line_number}."
            )
        payloads.append(payload)
    return tuple(payloads)


def _parse_timestamp(
    raw_value: str,
    *,
    source_path: Path,
    line_number: int,
) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayBuildError(
            f"Invalid ISO8601 timestamp: path={source_path} line={line_number} value={raw_value!r}."
        ) from exc
    return validate_timestamp(
        parsed,
        field_name="timestamp",
        error_type=ReplayBuildError,
    )


def _replay_identity(
    *,
    target_key: str,
    ingress_input: ReplayIngressInput,
    labels: tuple[str, ...],
) -> str:
    request = validate_replay_admission_request(
        target_key=target_key,
        ingress_input=ingress_input,
        replay_at=replay_ts_event(ingress_input),
        labels=list(labels),
    )
    return derive_admission_event_id(request)


def _serialize_dataset_row(
    *,
    ingress_input: ReplayIngressInput,
    labels: tuple[str, ...],
) -> dict[str, object]:
    payload = _serialize_ingress_input(ingress_input)
    normalized_labels = _normalize_replay_row_labels(
        ingress_input=ingress_input,
        labels=list(labels),
    )
    payload["labels"] = normalized_labels
    return payload


def _normalize_replay_row_labels(
    *,
    ingress_input: ReplayIngressInput,
    labels: list[str],
) -> list[str]:
    if isinstance(ingress_input, HistoricalWebSearchInput):
        return validate_source_classification_labels(labels=labels)
    return ensure_source_classification_labels(
        source_ref=ingress_input.source_ref,
        title=_ingress_title(ingress_input),
        content=_ingress_content(ingress_input),
        labels=labels,
    )


def _ingress_title(ingress_input: ReplayIngressInput) -> str:
    if isinstance(ingress_input, (HistoricalNewsStreamInput, HistoricalMarketNewsInput)):
        return ingress_input.headline
    if isinstance(ingress_input, HistoricalMacroApiInput):
        return ingress_input.release_key
    return ingress_input.title


def _ingress_content(ingress_input: ReplayIngressInput) -> str:
    if isinstance(ingress_input, (HistoricalNewsStreamInput, HistoricalMarketNewsInput)):
        return ingress_input.body
    if isinstance(ingress_input, HistoricalMacroApiInput):
        return f"{ingress_input.period_label} {ingress_input.value_text}"
    return ingress_input.content


def _cast_labels(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReplayBuildError("serialized replay rows must carry list[str] labels.")
    return value


def _serialize_ingress_input(ingress_input: ReplayIngressInput) -> dict[str, object]:
    if isinstance(ingress_input, HistoricalNewsStreamInput):
        return {
            "source_shape": ingress_input.source_shape,
            "source_ref": ingress_input.source_ref,
            "headline": ingress_input.headline,
            "body": ingress_input.body,
            "published_at": _format_timestamp(ingress_input.published_at),
            "captured_at": _format_timestamp(ingress_input.captured_at),
            "visible_at": _format_timestamp(ingress_input.visible_at),
        }
    if isinstance(ingress_input, HistoricalMarketNewsInput):
        return {
            "source_shape": ingress_input.source_shape,
            "source_ref": ingress_input.source_ref,
            "headline": ingress_input.headline,
            "body": ingress_input.body,
            "published_at": _format_timestamp(ingress_input.published_at),
            "updated_at": (
                None
                if ingress_input.updated_at is None
                else _format_timestamp(ingress_input.updated_at)
            ),
            "captured_at": _format_timestamp(ingress_input.captured_at),
            "visible_at": _format_timestamp(ingress_input.visible_at),
        }
    if isinstance(ingress_input, HistoricalMacroApiInput):
        return {
            "source_shape": ingress_input.source_shape,
            "source_ref": ingress_input.source_ref,
            "release_key": ingress_input.release_key,
            "period_label": ingress_input.period_label,
            "value_text": ingress_input.value_text,
            "unit": ingress_input.unit,
            "released_at": _format_timestamp(ingress_input.released_at),
            "visible_at": _format_timestamp(ingress_input.visible_at),
        }
    if isinstance(ingress_input, HistoricalManualDatasetInput):
        return {
            "source_shape": ingress_input.source_shape,
            "source_ref": ingress_input.source_ref,
            "title": ingress_input.title,
            "content": ingress_input.content,
            "provided_at": _format_timestamp(ingress_input.provided_at),
            "visible_at": _format_timestamp(ingress_input.visible_at),
        }
    if isinstance(ingress_input, HistoricalWebSearchInput):
        return {
            "source_shape": ingress_input.source_shape,
            "query": ingress_input.query,
            "source_ref": ingress_input.source_ref,
            "title": ingress_input.title,
            "content": ingress_input.content,
            "published_at": (
                None
                if ingress_input.published_at is None
                else _format_timestamp(ingress_input.published_at)
            ),
            "discovered_at": _format_timestamp(ingress_input.discovered_at),
            "visible_at": _format_timestamp(ingress_input.visible_at),
        }
    raise ReplayBuildError("Unsupported replay ingress input type during serialization.")


def _default_output_path(
    *,
    layout: WorkspaceLayout,
    build_output: ReplayBuildOutput,
) -> Path:
    window_token = (
        f"{build_output.start_at.astimezone(UTC).strftime('%Y%m%d')}_"
        f"{build_output.end_at.astimezone(UTC).strftime('%Y%m%d')}"
    )
    return (
        layout.helpers_root
        / "replay_inputs"
        / build_output.target_key
        / f"{window_token}.jsonl"
    ).resolve(strict=False)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ReplayBuildArtifacts",
    "ReplayBuildError",
    "ReplayBuildInput",
    "ReplayBuildOutput",
    "ReplayBuildReport",
    "build_replay_input",
    "render_replay_build_report",
    "write_replay_input",
]
