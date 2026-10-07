"""Replacement and history backup for human-owned target ``operator.md`` pages."""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from event_trader.contracts._validators import validate_target_key
from event_trader.storage import WorkspaceLayout


class OperatorContextUpdateError(RuntimeError):
    """Raised when an operator page cannot be backed up or replaced."""


def operator_context_path(*, layout: WorkspaceLayout, target_key: str) -> Path:
    normalized_target = validate_target_key(target_key, error_type=OperatorContextUpdateError)
    return layout.research_memory_root / "targets" / normalized_target / "operator.md"


def replace_operator_context(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    content_md: str,
) -> None:
    """Back up the exact current non-empty body, then atomically replace it."""
    if not isinstance(content_md, str):
        raise OperatorContextUpdateError("content_md must be a string.")
    page_path = operator_context_path(layout=layout, target_key=target_key)
    try:
        previous = page_path.read_text(encoding="utf-8") if page_path.exists() else ""
    except OSError as exc:
        raise OperatorContextUpdateError(f"Failed to read {page_path}.") from exc
    if previous == content_md:
        return
    history_path = (
        layout.research_memory_root
        / "operator_history"
        / validate_target_key(target_key, error_type=OperatorContextUpdateError)
        / f"{sha256(previous.encode('utf-8')).hexdigest()}.md"
    )
    replacement_path: Path | None = None
    try:
        page_path.parent.mkdir(parents=True, exist_ok=True)
        if previous.strip():
            history_path.parent.mkdir(parents=True, exist_ok=True)
            if not history_path.exists():
                history_path.write_text(previous, encoding="utf-8", newline="\n")
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=page_path.parent, delete=False
        ) as handle:
            handle.write(content_md)
            replacement_path = Path(handle.name)
        os.replace(replacement_path, page_path)
    except OSError as exc:
        if replacement_path is not None:
            replacement_path.unlink(missing_ok=True)
        raise OperatorContextUpdateError(f"Failed to replace {page_path}.") from exc


__all__ = ["OperatorContextUpdateError", "operator_context_path", "replace_operator_context"]
