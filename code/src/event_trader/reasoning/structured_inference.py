"""Provider-neutral port for one forced structured inference response."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class StructuredInferenceError(RuntimeError):
    """Raised when transport or structured-response protocol is exhausted."""


@dataclass(frozen=True, slots=True)
class StructuredInferenceResult:
    payload: Mapping[str, object]
    metadata: Mapping[str, object]


class StructuredInference(Protocol):
    """Generate one structured payload without validating business semantics."""

    async def generate_structured(
        self,
        *,
        prompt: str,
        session_id: str,
        tool_name: str,
        tool_description: str,
        input_schema: Mapping[str, object],
    ) -> StructuredInferenceResult: ...


__all__ = [
    "StructuredInference",
    "StructuredInferenceError",
    "StructuredInferenceResult",
]
