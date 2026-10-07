"""Canonical dynamic reader for human-owned target ``operator.md`` pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from event_trader.contracts._validators import validate_target_key
from event_trader.operator_context.updater import operator_context_path
from event_trader.storage import WorkspaceLayout


class OperatorContextReadError(ValueError):
    """Raised when the canonical operator context cannot be read."""


@dataclass(frozen=True, slots=True)
class OperatorContextSnapshot:
    """One exact canonical operator-context read used by an analysis attempt."""

    target_key: str
    page_path: str
    content_md: str
    content_sha256: str
    read_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=OperatorContextReadError),
        )
        expected_path = f"targets/{self.target_key}/operator.md"
        if self.page_path != expected_path:
            raise OperatorContextReadError(
                f"page_path must be {expected_path!r}; got {self.page_path!r}."
            )
        if not isinstance(self.content_md, str):
            raise OperatorContextReadError("content_md must be a string.")
        expected_hash = sha256(self.content_md.encode("utf-8")).hexdigest()
        if self.content_sha256 != expected_hash:
            raise OperatorContextReadError("content_sha256 does not match content_md.")
        if self.read_at.tzinfo is None or self.read_at.utcoffset() is None:
            raise OperatorContextReadError("read_at must be timezone-aware.")
        object.__setattr__(self, "read_at", self.read_at.astimezone(UTC))


class CanonicalOperatorContextReader:
    """Read the current human-owned operator page outside ResearchMemory staging."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise OperatorContextReadError("layout must be a WorkspaceLayout instance.")
        self._layout = layout

    def read(self, target_key: str) -> OperatorContextSnapshot:
        normalized_target_key = validate_target_key(
            target_key,
            error_type=OperatorContextReadError,
        )
        page_path = f"targets/{normalized_target_key}/operator.md"
        file_path = operator_context_path(
            layout=self._layout,
            target_key=normalized_target_key,
        )
        try:
            content_md = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise OperatorContextReadError(
                f"Failed to read canonical operator context {page_path!r}: {exc}"
            ) from exc
        return OperatorContextSnapshot(
            target_key=normalized_target_key,
            page_path=page_path,
            content_md=content_md,
            content_sha256=sha256(content_md.encode("utf-8")).hexdigest(),
            read_at=datetime.now(UTC),
        )


__all__ = [
    "CanonicalOperatorContextReader",
    "OperatorContextReadError",
    "OperatorContextSnapshot",
]
