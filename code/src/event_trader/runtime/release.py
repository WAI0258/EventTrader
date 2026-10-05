"""Minimal runtime release contracts for admitted evidence delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from event_trader.contracts._validators import validate_event_id
from event_trader.ingest.admission import AdmissionOutputs


class RuntimeReleaseError(ValueError):
    """Raised when admitted-evidence release contracts are malformed."""


@dataclass(frozen=True, slots=True)
class RuntimeReleaseReceipt:
    """Observable result from releasing one admitted event."""

    appended_event_id: str
    delivered_count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "appended_event_id",
            validate_event_id(
                self.appended_event_id,
                error_type=RuntimeReleaseError,
            ),
        )
        if (
            not isinstance(self.delivered_count, int)
            or isinstance(self.delivered_count, bool)
            or self.delivered_count < 0
        ):
            raise RuntimeReleaseError("delivered_count must be a non-negative integer.")


class AdmissionReleasePath(Protocol):
    """Post-admission release path shared by live and replay runtimes."""

    def release_admitted(self, outputs: AdmissionOutputs) -> RuntimeReleaseReceipt:
        """Append admitted evidence truth and publish the runtime event."""


__all__ = [
    "AdmissionReleasePath",
    "RuntimeReleaseError",
    "RuntimeReleaseReceipt",
]
