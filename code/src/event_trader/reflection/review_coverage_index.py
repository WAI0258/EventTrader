"""Machine-readable coverage index for target reflection reviews."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.contracts.research_memory import build_target_research_layout
from event_trader.storage import WorkspaceLayout

ReviewCoverageKind = Literal[
    "target_review",
    "episode_close",
    "open_position_horizon",
    "open_position_material_update",
]

_COVERAGE_COMMENT_RE = re.compile(
    r"<!--\s*reflection-coverage:\s*episode_id=(?P<episode_id>[^;]+?)\s*;\s*"
    r"covered_horizons_hours=(?P<horizons>[^>]+?)\s*-->",
    re.IGNORECASE,
)
_TARGET_CLOSE_REVIEW_RE = re.compile(
    r"<!--\s*target-close-review:\s*episode_id=(?P<episode_id>[^;]+?)\s*;",
    re.IGNORECASE,
)
_OPEN_POSITION_REVIEW_RE = re.compile(
    r"<!--\s*open-position-review:\s*(?P<attrs>.*?)-->",
    re.IGNORECASE | re.DOTALL,
)


class ReviewCoverageIndexError(ValueError):
    """Raised when reflection review coverage index records are malformed."""


@dataclass(frozen=True, slots=True)
class ReviewCoverageIndexRecord:
    """One indexed reflection review artifact used for scheduling only."""

    target_key: str
    episode_id: str
    review_kind: ReviewCoverageKind
    review_page_path: str
    artifact_path: Path
    opened_at: datetime
    recorded_at: datetime
    closed_at: datetime | None = None
    replay_end_at: datetime | None = None
    covered_horizons_hours: tuple[float, ...] = ()
    material_update_sequence: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=ReviewCoverageIndexError,
            ),
        )
        if not isinstance(self.episode_id, str) or not self.episode_id.strip():
            raise ReviewCoverageIndexError("episode_id must be a non-blank string.")
        object.__setattr__(self, "episode_id", self.episode_id.strip())
        if self.review_kind not in {
            "target_review",
            "episode_close",
            "open_position_horizon",
            "open_position_material_update",
        }:
            raise ReviewCoverageIndexError("review_kind is not supported.")
        if (
            not isinstance(self.review_page_path, str)
            or not self.review_page_path.strip()
        ):
            raise ReviewCoverageIndexError(
                "review_page_path must be a non-blank string."
            )
        object.__setattr__(self, "review_page_path", self.review_page_path.strip())
        if not isinstance(self.artifact_path, Path):
            raise ReviewCoverageIndexError("artifact_path must be a pathlib.Path.")
        object.__setattr__(
            self,
            "opened_at",
            validate_timestamp(
                self.opened_at,
                field_name="opened_at",
                error_type=ReviewCoverageIndexError,
            ),
        )
        object.__setattr__(
            self,
            "recorded_at",
            validate_timestamp(
                self.recorded_at,
                field_name="recorded_at",
                error_type=ReviewCoverageIndexError,
            ),
        )
        if self.closed_at is not None:
            object.__setattr__(
                self,
                "closed_at",
                validate_timestamp(
                    self.closed_at,
                    field_name="closed_at",
                    error_type=ReviewCoverageIndexError,
                ),
            )
        if self.replay_end_at is not None:
            object.__setattr__(
                self,
                "replay_end_at",
                validate_timestamp(
                    self.replay_end_at,
                    field_name="replay_end_at",
                    error_type=ReviewCoverageIndexError,
                ),
            )
        horizons = _normalize_horizons(self.covered_horizons_hours)
        object.__setattr__(self, "covered_horizons_hours", horizons)
        if self.review_kind in {"target_review", "open_position_horizon"} and not horizons:
            raise ReviewCoverageIndexError(
                "horizon review records require covered_horizons_hours."
            )
        if self.review_kind in {"target_review", "episode_close"}:
            if self.closed_at is None:
                raise ReviewCoverageIndexError(
                    f"{self.review_kind} records require closed_at."
                )
            if self.closed_at <= self.opened_at:
                raise ReviewCoverageIndexError(
                    "closed_at must be later than opened_at."
                )
        if self.review_kind in {
            "open_position_horizon",
            "open_position_material_update",
        }:
            if self.replay_end_at is None:
                raise ReviewCoverageIndexError(
                    f"{self.review_kind} records require replay_end_at."
                )
            if self.replay_end_at < self.opened_at:
                raise ReviewCoverageIndexError(
                    "replay_end_at must be no earlier than opened_at."
                )
        if self.review_kind == "open_position_material_update":
            if (
                self.material_update_sequence is None
                or self.material_update_sequence <= 0
            ):
                raise ReviewCoverageIndexError(
                    "open_position_material_update records require a positive "
                    "material-update sequence."
                )
        if self.review_kind != "open_position_material_update":
            if self.material_update_sequence is not None:
                raise ReviewCoverageIndexError(
                    "material-update metadata is only valid for "
                    "open_position_material_update records."
                )


def review_coverage_index_path(layout: WorkspaceLayout, target_key: str) -> Path:
    target_layout = build_target_research_layout(layout, target_key)
    return (target_layout.reviews_root / "coverage.jsonl").resolve(strict=False)


def read_review_coverage_index(
    layout: WorkspaceLayout,
    *,
    target_key: str | None = None,
) -> tuple[ReviewCoverageIndexRecord, ...]:
    if not isinstance(layout, WorkspaceLayout):
        raise ReviewCoverageIndexError("layout must be a WorkspaceLayout instance.")
    index_paths: list[Path] = []
    if target_key is None:
        if not layout.targets_root.exists():
            return ()
        for target_dir in sorted(layout.targets_root.iterdir()):
            if target_dir.is_dir():
                index_paths.append(review_coverage_index_path(layout, target_dir.name))
    else:
        index_paths.append(review_coverage_index_path(layout, target_key))

    records: list[ReviewCoverageIndexRecord] = []
    for index_path in index_paths:
        if not index_path.exists():
            continue
        if not index_path.is_file():
            raise ReviewCoverageIndexError(
                f"review coverage index path is not a file: {index_path}"
            )
        for lineno, raw_line in enumerate(
            index_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReviewCoverageIndexError(
                    f"review coverage index line {lineno} is invalid JSON."
                ) from exc
            records.append(_record_from_payload(payload, layout=layout))
    return _dedupe_records(tuple(records))


def upsert_review_coverage_record(
    layout: WorkspaceLayout,
    record: ReviewCoverageIndexRecord,
) -> None:
    if not isinstance(layout, WorkspaceLayout):
        raise ReviewCoverageIndexError("layout must be a WorkspaceLayout instance.")
    if not isinstance(record, ReviewCoverageIndexRecord):
        raise ReviewCoverageIndexError(
            "record must be a ReviewCoverageIndexRecord instance."
        )
    existing = [
        item
        for item in read_review_coverage_index(layout, target_key=record.target_key)
        if item.review_page_path != record.review_page_path
    ]
    existing.append(record)
    _write_target_records(layout, record.target_key, tuple(existing))


def rebuild_review_coverage_index(
    layout: WorkspaceLayout,
    *,
    target_key: str | None = None,
) -> tuple[ReviewCoverageIndexRecord, ...]:
    if not isinstance(layout, WorkspaceLayout):
        raise ReviewCoverageIndexError("layout must be a WorkspaceLayout instance.")
    target_keys: list[str] = []
    if target_key is None:
        if not layout.targets_root.exists():
            return ()
        target_keys = [
            target_dir.name
            for target_dir in sorted(layout.targets_root.iterdir())
            if target_dir.is_dir()
        ]
    else:
        target_keys = [validate_target_key(target_key, error_type=ReviewCoverageIndexError)]

    rebuilt: list[ReviewCoverageIndexRecord] = []
    for one_target_key in target_keys:
        target_layout = build_target_research_layout(layout, one_target_key)
        records: list[ReviewCoverageIndexRecord] = []
        if target_layout.reviews_root.exists():
            for artifact_path in sorted(target_layout.reviews_root.glob("*.md")):
                parsed = parse_review_coverage_artifact(
                    layout=layout,
                    artifact_path=artifact_path,
                )
                if parsed is not None:
                    records.append(parsed)
        _write_target_records(layout, one_target_key, tuple(records))
        rebuilt.extend(records)
    return tuple(rebuilt)


def parse_review_coverage_artifact(
    *,
    layout: WorkspaceLayout,
    artifact_path: Path,
) -> ReviewCoverageIndexRecord | None:
    if not isinstance(layout, WorkspaceLayout):
        raise ReviewCoverageIndexError("layout must be a WorkspaceLayout instance.")
    if not isinstance(artifact_path, Path):
        raise ReviewCoverageIndexError("artifact_path must be a pathlib.Path.")
    if not artifact_path.exists() or not artifact_path.is_file():
        return None
    resolved_artifact_path = artifact_path.resolve(strict=False)
    try:
        relative_path = resolved_artifact_path.relative_to(layout.research_memory_root)
    except ValueError as exc:
        raise ReviewCoverageIndexError(
            "artifact_path must live under layout.research_memory_root."
        ) from exc
    review_page_path = relative_path.as_posix()
    target_key, opened_at, path_second_at = _parse_flat_review_path(review_page_path)
    content_md = resolved_artifact_path.read_text(encoding="utf-8")
    recorded_at = _file_recorded_at(resolved_artifact_path)

    close_matches = tuple(_TARGET_CLOSE_REVIEW_RE.finditer(content_md))
    if len(close_matches) > 1:
        raise ReviewCoverageIndexError(
            "target close review artifact must contain at most one metadata record."
        )
    if len(close_matches) == 1:
        return ReviewCoverageIndexRecord(
            target_key=target_key,
            episode_id=close_matches[0].group("episode_id").strip(),
            review_kind="episode_close",
            review_page_path=review_page_path,
            artifact_path=resolved_artifact_path,
            opened_at=opened_at,
            closed_at=path_second_at,
            recorded_at=recorded_at,
        )

    open_matches = tuple(_OPEN_POSITION_REVIEW_RE.finditer(content_md))
    if len(open_matches) > 1:
        raise ReviewCoverageIndexError(
            "open-position review artifact must contain at most one metadata record."
        )
    coverage_matches = tuple(_COVERAGE_COMMENT_RE.finditer(content_md))
    if len(coverage_matches) > 1:
        raise ReviewCoverageIndexError(
            "review artifact must contain at most one reflection-coverage record."
        )

    if len(open_matches) == 1:
        attrs = _parse_metadata_attrs(open_matches[0].group("attrs"))
        episode_id = _required_attr(attrs, "episode_id")
        review_kind = _required_attr(attrs, "review_kind")
        replay_end_at = _parse_metadata_timestamp(_required_attr(attrs, "replay_end_at"))
        if review_kind == "open_position_horizon":
            if len(coverage_matches) != 1:
                raise ReviewCoverageIndexError(
                    "open-position horizon reviews require one coverage record."
                )
            return ReviewCoverageIndexRecord(
                target_key=target_key,
                episode_id=episode_id,
                review_kind="open_position_horizon",
                review_page_path=review_page_path,
                artifact_path=resolved_artifact_path,
                opened_at=opened_at,
                replay_end_at=replay_end_at,
                covered_horizons_hours=_parse_horizons_csv(
                    coverage_matches[0].group("horizons")
                ),
                recorded_at=recorded_at,
            )
        if review_kind == "open_position_material_update":
            return ReviewCoverageIndexRecord(
                target_key=target_key,
                episode_id=episode_id,
                review_kind="open_position_material_update",
                review_page_path=review_page_path,
                artifact_path=resolved_artifact_path,
                opened_at=opened_at,
                replay_end_at=replay_end_at,
                material_update_sequence=int(
                    _required_attr(attrs, "material_update_sequence")
                ),
                recorded_at=recorded_at,
            )
        return None

    if len(coverage_matches) == 1:
        return ReviewCoverageIndexRecord(
            target_key=target_key,
            episode_id=coverage_matches[0].group("episode_id").strip(),
            review_kind="target_review",
            review_page_path=review_page_path,
            artifact_path=resolved_artifact_path,
            opened_at=opened_at,
            closed_at=path_second_at,
            covered_horizons_hours=_parse_horizons_csv(
                coverage_matches[0].group("horizons")
            ),
            recorded_at=recorded_at,
        )
    return None


def _write_target_records(
    layout: WorkspaceLayout,
    target_key: str,
    records: tuple[ReviewCoverageIndexRecord, ...],
) -> None:
    index_path = review_coverage_index_path(layout, target_key)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    deduped = tuple(
        sorted(
            _dedupe_records(records),
            key=lambda item: (
                item.opened_at,
                item.closed_at or item.replay_end_at or item.recorded_at,
                item.review_kind,
                item.review_page_path,
            ),
        )
    )
    text = "".join(json.dumps(_record_payload(item), sort_keys=True) + "\n" for item in deduped)
    index_path.write_text(text, encoding="utf-8")


def _dedupe_records(
    records: tuple[ReviewCoverageIndexRecord, ...],
) -> tuple[ReviewCoverageIndexRecord, ...]:
    by_path: dict[str, ReviewCoverageIndexRecord] = {}
    for record in records:
        previous = by_path.get(record.review_page_path)
        if previous is None or previous.recorded_at <= record.recorded_at:
            by_path[record.review_page_path] = record
    return tuple(by_path.values())


def _record_payload(record: ReviewCoverageIndexRecord) -> dict[str, object]:
    payload: dict[str, object] = {
        "target_key": record.target_key,
        "episode_id": record.episode_id,
        "review_kind": record.review_kind,
        "review_page_path": record.review_page_path,
        "opened_at": _format_timestamp(record.opened_at),
        "recorded_at": _format_timestamp(record.recorded_at),
        "covered_horizons_hours": list(record.covered_horizons_hours),
    }
    if record.closed_at is not None:
        payload["closed_at"] = _format_timestamp(record.closed_at)
    if record.replay_end_at is not None:
        payload["replay_end_at"] = _format_timestamp(record.replay_end_at)
    if record.material_update_sequence is not None:
        payload["material_update_sequence"] = record.material_update_sequence
    return payload


def _record_from_payload(
    payload: object,
    *,
    layout: WorkspaceLayout,
) -> ReviewCoverageIndexRecord:
    if not isinstance(payload, dict):
        raise ReviewCoverageIndexError("review coverage index rows must be objects.")
    try:
        target_key = str(payload["target_key"])
        review_page_path = str(payload["review_page_path"])
        review_kind = str(payload["review_kind"])
        artifact_path_raw = payload.get("artifact_path")
        artifact_path = (
            Path(str(artifact_path_raw))
            if artifact_path_raw is not None
            else layout.research_memory_root / review_page_path
        )
        if not artifact_path.is_absolute():
            artifact_path = (layout.root / artifact_path).resolve(strict=False)
        return ReviewCoverageIndexRecord(
            target_key=target_key,
            episode_id=str(payload["episode_id"]),
            review_kind=review_kind,  # type: ignore[arg-type]
            review_page_path=review_page_path,
            artifact_path=artifact_path.resolve(strict=False),
            opened_at=_parse_metadata_timestamp(str(payload["opened_at"])),
            closed_at=(
                _parse_metadata_timestamp(str(payload["closed_at"]))
                if payload.get("closed_at") is not None
                else None
            ),
            replay_end_at=(
                _parse_metadata_timestamp(str(payload["replay_end_at"]))
                if payload.get("replay_end_at") is not None
                else None
            ),
            covered_horizons_hours=tuple(
                float(item) for item in payload.get("covered_horizons_hours", ())
            ),
            material_update_sequence=(
                int(payload["material_update_sequence"])
                if payload.get("material_update_sequence") is not None
                else None
            ),
            recorded_at=_parse_metadata_timestamp(str(payload["recorded_at"])),
        )
    except KeyError as exc:
        raise ReviewCoverageIndexError(
            f"review coverage index row missing field: {exc.args[0]}"
        ) from exc


def _parse_flat_review_path(page_path: str) -> tuple[str, datetime, datetime]:
    parts = tuple(part for part in page_path.split("/") if part)
    if len(parts) != 4 or parts[0] != "targets" or parts[2] != "reviews":
        raise ReviewCoverageIndexError(
            "review coverage index rebuild only supports flat review artifacts."
        )
    target_key = validate_target_key(parts[1], error_type=ReviewCoverageIndexError)
    stem_parts = tuple(part for part in Path(parts[3]).stem.split("_") if part)
    if len(stem_parts) < 2:
        raise ReviewCoverageIndexError("flat review artifact name is not canonical.")
    return (
        target_key,
        _parse_compact_utc_timestamp(stem_parts[0], field_name="opened_at"),
        _parse_compact_utc_timestamp(stem_parts[1], field_name="review_time"),
    )


def _parse_metadata_attrs(raw_attrs: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for raw_part in raw_attrs.split(";"):
        part = raw_part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ReviewCoverageIndexError(
                f"Invalid reflection metadata attribute: {part!r}."
            )
        key, value = part.split("=", 1)
        attrs[key.strip()] = value.strip()
    return attrs


def _required_attr(attrs: dict[str, str], key: str) -> str:
    value = attrs.get(key)
    if value is None or not value:
        raise ReviewCoverageIndexError(f"review metadata requires {key}.")
    return value


def _parse_metadata_timestamp(raw_value: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewCoverageIndexError(
            f"Invalid reflection metadata timestamp: {raw_value!r}."
        ) from exc
    if value.tzinfo is None:
        raise ReviewCoverageIndexError(
            f"Reflection metadata timestamp must include timezone: {raw_value!r}."
        )
    return value.astimezone(UTC)


def _parse_compact_utc_timestamp(value: str, *, field_name: str) -> datetime:
    if not isinstance(value, str) or len(value) != 13 or value[6] != "T":
        raise ReviewCoverageIndexError(
            f"{field_name} must use compact UTC format YYMMDDTHHMMSS."
        )
    try:
        parsed = datetime.strptime(value, "%y%m%dT%H%M%S")
    except ValueError as exc:
        raise ReviewCoverageIndexError(
            f"{field_name} must use compact UTC format YYMMDDTHHMMSS."
        ) from exc
    return parsed.replace(tzinfo=UTC)


def _parse_horizons_csv(value: str) -> tuple[float, ...]:
    horizons: list[float] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        try:
            horizon = float(item)
        except ValueError as exc:
            raise ReviewCoverageIndexError(
                f"Invalid covered horizon value: {item!r}."
            ) from exc
        horizons.append(horizon)
    return _normalize_horizons(tuple(horizons))


def _normalize_horizons(value: tuple[float, ...]) -> tuple[float, ...]:
    if not isinstance(value, tuple):
        raise ReviewCoverageIndexError("covered_horizons_hours must be a tuple.")
    normalized: set[float] = set()
    for horizon in value:
        horizon_float = float(horizon)
        if horizon_float <= 0:
            raise ReviewCoverageIndexError(
                "covered_horizons_hours must contain only positive values."
            )
        normalized.add(horizon_float)
    return tuple(sorted(normalized))


def _file_recorded_at(artifact_path: Path) -> datetime:
    return datetime.fromtimestamp(artifact_path.stat().st_mtime, tz=UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ReviewCoverageIndexError",
    "ReviewCoverageIndexRecord",
    "ReviewCoverageKind",
    "parse_review_coverage_artifact",
    "read_review_coverage_index",
    "rebuild_review_coverage_index",
    "review_coverage_index_path",
    "upsert_review_coverage_record",
]
