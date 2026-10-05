"""Factual trace bundle and evidence-pack builders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from event_trader.storage import WorkspaceLayout

TraceArtifactStatus = Literal["available", "missing"]

REQUIRED_TRACE_ARTIFACT_TYPES: tuple[str, ...] = (
    "ledger",
    "checker_context",
    "checker_decision",
    "analysis_context",
    "analysis_outcome",
    "research_memory_diff",
    "claim_registry",
    "pm_decision",
    "execution_record",
    "portfolio_state",
    "validation_mark",
    "episode_artifact",
    "reflection_review",
    "learning_outcome",
    "projection",
    "counterfactual_report",
)


class TraceBundleError(ValueError):
    """Raised when trace bundle inputs are malformed."""


@dataclass(frozen=True, slots=True)
class TraceArtifactRef:
    artifact_type: str
    artifact_id: str
    path: Path
    line: int | None = None
    visible_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_type", _non_blank(self.artifact_type, "artifact_type"))
        object.__setattr__(self, "artifact_id", _non_blank(self.artifact_id, "artifact_id"))
        if not isinstance(self.path, Path):
            raise TraceBundleError("path must be a Path.")
        if self.line is not None:
            if isinstance(self.line, bool) or not isinstance(self.line, int) or self.line <= 0:
                raise TraceBundleError("line must be a positive integer when provided.")
        if self.visible_at is not None and not isinstance(self.visible_at, datetime):
            raise TraceBundleError("visible_at must be a datetime when provided.")


@dataclass(frozen=True, slots=True)
class TraceArtifact:
    artifact_type: str
    artifact_id: str
    path: Path
    line: int | None
    sha256: str | None
    status: TraceArtifactStatus
    visible_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_type", _non_blank(self.artifact_type, "artifact_type"))
        object.__setattr__(self, "artifact_id", _non_blank(self.artifact_id, "artifact_id"))
        if not isinstance(self.path, Path):
            raise TraceBundleError("path must be a Path.")
        if self.status not in {"available", "missing"}:
            raise TraceBundleError("status must be available or missing.")
        if self.status == "available":
            object.__setattr__(self, "sha256", _non_blank(self.sha256, "sha256"))
        elif self.sha256 is not None:
            raise TraceBundleError("missing artifacts must not carry sha256.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_id": self.artifact_id,
            "path": str(self.path),
            "line": self.line,
            "sha256": self.sha256,
            "status": self.status,
            "visible_at": None if self.visible_at is None else self.visible_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class TraceBundle:
    bundle_id: str
    target_key: str
    episode_id: str
    event_ids: tuple[str, ...]
    decision_episode_ids: tuple[str, ...]
    view_episode_id: str
    artifacts: tuple[TraceArtifact, ...]
    missing_artifacts: tuple[TraceArtifact, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "bundle_id", _non_blank(self.bundle_id, "bundle_id"))
        object.__setattr__(self, "target_key", _non_blank(self.target_key, "target_key"))
        object.__setattr__(self, "episode_id", _non_blank(self.episode_id, "episode_id"))
        object.__setattr__(self, "event_ids", _non_blank_tuple(self.event_ids, "event_ids"))
        object.__setattr__(
            self,
            "decision_episode_ids",
            _non_blank_tuple(self.decision_episode_ids, "decision_episode_ids"),
        )
        object.__setattr__(
            self,
            "view_episode_id",
            _non_blank(self.view_episode_id, "view_episode_id"),
        )
        if not isinstance(self.artifacts, tuple):
            raise TraceBundleError("artifacts must be a tuple.")
        if not isinstance(self.missing_artifacts, tuple):
            raise TraceBundleError("missing_artifacts must be a tuple.")
        for artifact in (*self.artifacts, *self.missing_artifacts):
            if not isinstance(artifact, TraceArtifact):
                raise TraceBundleError("bundle artifacts must be TraceArtifact values.")
        if not isinstance(self.created_at, datetime):
            raise TraceBundleError("created_at must be a datetime.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "target_key": self.target_key,
            "episode_id": self.episode_id,
            "event_ids": list(self.event_ids),
            "decision_episode_ids": list(self.decision_episode_ids),
            "view_episode_id": self.view_episode_id,
            "artifacts": [artifact.to_json_payload() for artifact in self.artifacts],
            "missing_artifacts": [
                artifact.to_json_payload() for artifact in self.missing_artifacts
            ],
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class EvidencePack:
    pack_id: str
    target_key: str
    episode_id: str
    artifacts: tuple[TraceArtifact, ...]
    as_of_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "pack_id", _non_blank(self.pack_id, "pack_id"))
        object.__setattr__(self, "target_key", _non_blank(self.target_key, "target_key"))
        object.__setattr__(self, "episode_id", _non_blank(self.episode_id, "episode_id"))
        if not isinstance(self.as_of_at, datetime):
            raise TraceBundleError("as_of_at must be a datetime.")
        for artifact in self.artifacts:
            if not isinstance(artifact, TraceArtifact):
                raise TraceBundleError("artifacts must contain TraceArtifact values.")
            if artifact.visible_at is not None and artifact.visible_at > self.as_of_at:
                raise TraceBundleError("evidence pack must not include future artifacts.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "pack_id": self.pack_id,
            "target_key": self.target_key,
            "episode_id": self.episode_id,
            "artifacts": [artifact.to_json_payload() for artifact in self.artifacts],
            "as_of_at": self.as_of_at.isoformat(),
        }


def build_trace_bundle(
    *,
    target_key: str,
    episode_id: str,
    artifact_refs: tuple[TraceArtifactRef, ...],
    created_at: datetime,
    required_artifact_types: tuple[str, ...] = REQUIRED_TRACE_ARTIFACT_TYPES,
    event_ids: tuple[str, ...] = ("unknown-event",),
    decision_episode_ids: tuple[str, ...] = ("unknown-decision-episode",),
    view_episode_id: str | None = None,
) -> TraceBundle:
    if not isinstance(artifact_refs, tuple):
        raise TraceBundleError("artifact_refs must be a tuple.")
    refs_by_type = {ref.artifact_type for ref in artifact_refs}
    missing_required_refs = tuple(
        TraceArtifactRef(
            artifact_type=artifact_type,
            artifact_id=f"missing:{artifact_type}",
            path=Path(""),
        )
        for artifact_type in required_artifact_types
        if artifact_type not in refs_by_type
    )
    artifacts = tuple(_materialize(ref) for ref in (*artifact_refs, *missing_required_refs))
    missing = tuple(artifact for artifact in artifacts if artifact.status == "missing")
    bundle_hash = sha256(
        "|".join(
            f"{artifact.artifact_type}:{artifact.artifact_id}:{artifact.status}"
            for artifact in artifacts
        ).encode("utf-8")
    ).hexdigest()
    return TraceBundle(
        bundle_id=f"trace-bundle:{target_key}:{episode_id}:{bundle_hash}",
        target_key=target_key,
        episode_id=episode_id,
        event_ids=event_ids,
        decision_episode_ids=decision_episode_ids,
        view_episode_id=view_episode_id or episode_id,
        artifacts=artifacts,
        missing_artifacts=missing,
        created_at=created_at,
    )


def build_episode_trace_bundle(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_ids: tuple[str, ...],
    view_episode_id: str,
    decision_episode_ids: tuple[str, ...],
    created_at: datetime,
) -> TraceBundle:
    """Build one source-to-reflection audit chain from existing workspace artifacts."""
    if not isinstance(layout, WorkspaceLayout):
        raise TraceBundleError("layout must be a WorkspaceLayout instance.")
    artifact_refs = tuple(
        _artifact_ref_from_workspace(
            layout=layout,
            artifact_type=artifact_type,
            target_key=target_key,
            tokens=_trace_tokens_for_artifact(
                artifact_type=artifact_type,
                view_episode_id=view_episode_id,
                event_ids=event_ids,
                decision_episode_ids=decision_episode_ids,
            ),
        )
        for artifact_type in REQUIRED_TRACE_ARTIFACT_TYPES
    )
    return build_trace_bundle(
        target_key=target_key,
        episode_id=view_episode_id,
        artifact_refs=artifact_refs,
        created_at=created_at,
        event_ids=event_ids,
        decision_episode_ids=decision_episode_ids,
        view_episode_id=view_episode_id,
    )


def build_evidence_pack(
    *,
    bundle: TraceBundle,
    as_of_at: datetime,
    pack_id: str | None = None,
) -> EvidencePack:
    visible_artifacts = tuple(
        artifact
        for artifact in bundle.artifacts
        if artifact.status == "available"
        and (artifact.visible_at is None or artifact.visible_at <= as_of_at)
    )
    return EvidencePack(
        pack_id=pack_id or f"evidence-pack:{bundle.bundle_id}",
        target_key=bundle.target_key,
        episode_id=bundle.episode_id,
        artifacts=visible_artifacts,
        as_of_at=as_of_at,
    )


def _materialize(ref: TraceArtifactRef) -> TraceArtifact:
    path = ref.path.resolve(strict=False)
    if not path.exists() or not path.is_file():
        return TraceArtifact(
            artifact_type=ref.artifact_type,
            artifact_id=ref.artifact_id,
            path=path,
            line=ref.line,
            sha256=None,
            status="missing",
            visible_at=ref.visible_at,
        )
    return TraceArtifact(
        artifact_type=ref.artifact_type,
        artifact_id=ref.artifact_id,
        path=path,
        line=ref.line,
        sha256=sha256(path.read_bytes()).hexdigest(),
        status="available",
        visible_at=ref.visible_at,
    )


def _artifact_ref_from_workspace(
    *,
    layout: WorkspaceLayout,
    artifact_type: str,
    target_key: str,
    tokens: tuple[str, ...],
) -> TraceArtifactRef:
    roots = _candidate_roots(layout=layout, artifact_type=artifact_type, target_key=target_key)
    if not tokens:
        return TraceArtifactRef(
            artifact_type=artifact_type,
            artifact_id=f"missing:{artifact_type}:{target_key}",
            path=layout.root / ".missing" / f"{artifact_type}.missing",
        )
    for root in roots:
        match = _find_first_file_containing(root=root, tokens=tokens)
        if match is not None:
            return TraceArtifactRef(
                artifact_type=artifact_type,
                artifact_id=f"{artifact_type}:{target_key}",
                path=match,
            )
    return TraceArtifactRef(
        artifact_type=artifact_type,
        artifact_id=f"missing:{artifact_type}:{target_key}",
        path=layout.root / ".missing" / f"{artifact_type}.missing",
    )


def _trace_tokens_for_artifact(
    *,
    artifact_type: str,
    view_episode_id: str,
    event_ids: tuple[str, ...],
    decision_episode_ids: tuple[str, ...],
) -> tuple[str, ...]:
    if artifact_type in {
        "episode_artifact",
        "reflection_review",
        "counterfactual_report",
    }:
        return _non_blank_tuple((view_episode_id,), "view_episode_id")
    return _non_blank_tuple((*event_ids, *decision_episode_ids), "trace identifiers")


def _candidate_roots(
    *,
    layout: WorkspaceLayout,
    artifact_type: str,
    target_key: str,
) -> tuple[Path, ...]:
    runtime = layout.runtime_root
    target_root = layout.targets_root / target_key
    mapping = {
        "ledger": (layout.ledger_root,),
        "checker_context": (runtime / "context_packets",),
        "checker_decision": (runtime / "checker_decisions" / target_key,),
        "analysis_context": (runtime / "context_packets",),
        "analysis_outcome": (runtime / "analysis_outcomes" / target_key,),
        "research_memory_diff": (target_root,),
        "claim_registry": (runtime / "claim_registry", layout.research_memory_root),
        "pm_decision": (runtime / "pm_decisions" / target_key,),
        "execution_record": (runtime / "execution" / "records" / target_key,),
        "portfolio_state": (runtime / "portfolio" / target_key,),
        "validation_mark": (runtime / "decision_memory" / target_key,),
        "episode_artifact": (runtime / "validation" / "episodes" / target_key,),
        "reflection_review": (target_root / "reviews",),
        "learning_outcome": (runtime / "learning", target_root / "reviews"),
        "projection": (runtime / "projection", runtime / "projections"),
        "counterfactual_report": (runtime / "evaluation" / "counterfactuals" / target_key,),
    }
    return mapping.get(artifact_type, (runtime,))


def _find_first_file_containing(*, root: Path, tokens: tuple[str, ...]) -> Path | None:
    if not root.exists():
        return None
    paths = (
        (root,)
        if root.is_file()
        else tuple(sorted(path for path in root.rglob("*") if path.is_file()))
    )
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(token in content for token in tokens):
            return path
    return None


def _non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TraceBundleError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _non_blank_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        raise TraceBundleError(f"{field_name} must be a non-empty tuple.")
    return tuple(_non_blank(item, field_name) for item in value)


__all__ = [
    "REQUIRED_TRACE_ARTIFACT_TYPES",
    "EvidencePack",
    "TraceArtifact",
    "TraceArtifactRef",
    "TraceBundle",
    "TraceBundleError",
    "build_evidence_pack",
    "build_episode_trace_bundle",
    "build_trace_bundle",
]
