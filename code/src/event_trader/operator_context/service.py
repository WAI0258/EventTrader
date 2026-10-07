"""Dedicated PM Console boundary for human-owned operator context pages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock, RLock

from event_trader.contracts._validators import validate_target_key
from event_trader.storage import WorkspaceLayout

from .updater import (
    OperatorContextUpdateError,
    operator_context_path,
    replace_operator_context,
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TARGET_LOCKS: dict[tuple[str, str], RLock] = {}
_TARGET_LOCKS_GUARD = Lock()


class OperatorContextServiceError(RuntimeError):
    """Raised when the PM Console operator-context boundary cannot serve a request."""


@dataclass(frozen=True, slots=True)
class OperatorContextDocument:
    target_key: str
    page_path: str
    content_md: str
    content_sha256: str
    status: str

    @property
    def exists(self) -> bool:
        return self.status != "missing"

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "page_path": self.page_path,
            "content_md": self.content_md,
            "content_sha256": self.content_sha256,
            "status": self.status,
            "editable": True,
        }


@dataclass(frozen=True, slots=True)
class OperatorContextHistoryVersion:
    target_key: str
    version_id: str
    content_md: str

    @property
    def content_sha256(self) -> str:
        return self.version_id

    def to_json_payload(self, *, include_content: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "target_key": self.target_key,
            "version_id": self.version_id,
            "content_sha256": self.content_sha256,
        }
        if include_content:
            payload["content_md"] = self.content_md
        return payload


class OperatorContextConflictError(OperatorContextServiceError):
    """Raised when a client writes against a stale active-context hash."""

    def __init__(self, current: OperatorContextDocument) -> None:
        super().__init__("stale operator context: the active page changed on disk.")
        self.current = current


class OperatorContextService:
    """Read and safely replace one target's active context and hash-named history."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise OperatorContextServiceError("layout must be a WorkspaceLayout instance.")
        self._layout = layout

    def read(self, target_key: str) -> OperatorContextDocument:
        normalized_target = self._target_key(target_key)
        path = operator_context_path(layout=self._layout, target_key=normalized_target)
        try:
            content_md = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError as exc:
            raise OperatorContextServiceError(f"Failed to read {path}.") from exc
        return self._document(normalized_target, content_md, exists=path.exists())

    def list_history(self, target_key: str) -> tuple[OperatorContextHistoryVersion, ...]:
        normalized_target = self._target_key(target_key)
        history_root = self._history_root(normalized_target)
        if not history_root.exists():
            return ()
        versions: list[OperatorContextHistoryVersion] = []
        try:
            paths = sorted(history_root.glob("*.md"), key=lambda path: path.stem)
            for path in paths:
                version_id = path.stem
                if not _HASH_RE.fullmatch(version_id):
                    continue
                content_md = path.read_text(encoding="utf-8")
                if _content_hash(content_md) != version_id:
                    continue
                versions.append(
                    OperatorContextHistoryVersion(
                        target_key=normalized_target,
                        version_id=version_id,
                        content_md=content_md,
                    )
                )
        except OSError as exc:
            raise OperatorContextServiceError(
                f"Failed to list operator history for {normalized_target}."
            ) from exc
        return tuple(versions)

    def read_history(self, target_key: str, version_id: str) -> OperatorContextHistoryVersion:
        normalized_target = self._target_key(target_key)
        normalized_version = self._version_id(version_id)
        path = self._history_root(normalized_target) / f"{normalized_version}.md"
        try:
            content_md = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"unknown operator history version: {normalized_version}"
            ) from exc
        except OSError as exc:
            raise OperatorContextServiceError(
                f"Failed to read operator history {normalized_version}."
            ) from exc
        if _content_hash(content_md) != normalized_version:
            raise FileNotFoundError(f"unknown operator history version: {normalized_version}")
        return OperatorContextHistoryVersion(normalized_target, normalized_version, content_md)

    def replace(
        self,
        target_key: str,
        *,
        content_md: str,
        expected_content_sha256: str,
    ) -> OperatorContextDocument:
        if not isinstance(content_md, str):
            raise OperatorContextServiceError("content_md must be a string.")
        expected_hash = self._version_id(expected_content_sha256)
        normalized_target = self._target_key(target_key)
        with _target_lock(self._layout, normalized_target):
            current = self.read(normalized_target)
            if current.content_sha256 != expected_hash:
                raise OperatorContextConflictError(current)
            try:
                replace_operator_context(
                    layout=self._layout,
                    target_key=normalized_target,
                    content_md=content_md,
                )
            except OperatorContextUpdateError as exc:
                raise OperatorContextServiceError(str(exc)) from exc
            return self.read(normalized_target)

    def restore(
        self,
        target_key: str,
        *,
        version_id: str,
        expected_content_sha256: str,
    ) -> OperatorContextDocument:
        normalized_target = self._target_key(target_key)
        with _target_lock(self._layout, normalized_target):
            version = self.read_history(normalized_target, version_id)
            return self.replace(
                normalized_target,
                content_md=version.content_md,
                expected_content_sha256=expected_content_sha256,
            )

    def _history_root(self, target_key: str) -> Path:
        return self._layout.research_memory_root / "operator_history" / target_key

    @staticmethod
    def _target_key(target_key: str) -> str:
        return validate_target_key(target_key, error_type=OperatorContextServiceError)

    @staticmethod
    def _version_id(version_id: str) -> str:
        if not isinstance(version_id, str) or not _HASH_RE.fullmatch(version_id):
            raise OperatorContextServiceError("version_id must be a lowercase SHA-256 hash.")
        return version_id

    @staticmethod
    def _document(target_key: str, content_md: str, *, exists: bool) -> OperatorContextDocument:
        return OperatorContextDocument(
            target_key=target_key,
            page_path=f"targets/{target_key}/operator.md",
            content_md=content_md,
            content_sha256=_content_hash(content_md),
            status="missing" if not exists else ("empty" if not content_md.strip() else "ready"),
        )


def _content_hash(content_md: str) -> str:
    return sha256(content_md.encode("utf-8")).hexdigest()


def _target_lock(layout: WorkspaceLayout, target_key: str) -> RLock:
    key = (str(layout.research_memory_root.resolve(strict=False)), target_key)
    with _TARGET_LOCKS_GUARD:
        lock = _TARGET_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _TARGET_LOCKS[key] = lock
        return lock


__all__ = [
    "OperatorContextConflictError",
    "OperatorContextDocument",
    "OperatorContextHistoryVersion",
    "OperatorContextService",
    "OperatorContextServiceError",
]
