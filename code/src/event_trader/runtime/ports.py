"""Ports for the Nautilus-aligned runtime actors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .analysis_queue import (
    AnalysisQueueDeadLetter,
    AnalysisQueueEnqueueReceipt,
    AnalysisWorkItem,
)
from .contracts import (
    AnalysisRequested,
    CEAUEventRouted,
    CEAUSourceCompletenessObserved,
    CEAUUnitAppended,
    CEAUUnitClosed,
    CEAUUnitEmitted,
    CEAUUnitOpened,
    CEAUWatermarkObserved,
    CheckerDecision,
    EvidenceAdmitted,
    EvidenceQuarantined,
    NewsRaw,
    WebResultRaw,
)
from .pm_review_queue import (
    PMReviewQueueDeadLetter,
    PMReviewQueueEnqueueReceipt,
    PMReviewWorkItem,
)
from .queue import (
    CheckerQueueDeadLetter,
    CheckerQueueEnqueueReceipt,
    CheckerWorkItem,
)

type AdmissionResult = EvidenceAdmitted | EvidenceQuarantined
type CEAULifecycleMessage = (
    CEAUEventRouted
    | CEAUUnitOpened
    | CEAUUnitAppended
    | CEAUUnitEmitted
    | CEAUUnitClosed
    | CEAUWatermarkObserved
    | CEAUSourceCompletenessObserved
)


@dataclass(frozen=True, slots=True)
class CEAUEngineEffectSet:
    """Result from one deterministic CEAU engine transition."""

    lifecycle_messages: tuple[CEAULifecycleMessage, ...] = ()
    analysis_requests: tuple[AnalysisRequested, ...] = ()


class EvidenceAdmissionPort(Protocol):
    """Deterministic evidence-admission seam for raw source messages."""

    def admit_news_raw(self, message: NewsRaw) -> AdmissionResult:
        """Admit or quarantine one raw news record."""

    def admit_web_result_raw(self, message: WebResultRaw) -> AdmissionResult:
        """Admit or quarantine one raw web result record."""


class CheckerWorkQueuePort(Protocol):
    """Durable/resumable queue seam for checker work."""

    def enqueue(self, item: CheckerWorkItem) -> CheckerQueueEnqueueReceipt:
        """Persist one checker work item idempotently."""

    def dead_letter(self, item: CheckerWorkItem, *, reason: str) -> CheckerQueueDeadLetter:
        """Persist terminal queue failure metadata for one work item."""


class AnalysisWorkQueuePort(Protocol):
    """Durable/resumable queue seam for analysis work."""

    def enqueue(self, item: AnalysisWorkItem) -> AnalysisQueueEnqueueReceipt:
        """Persist one analysis work item idempotently."""

    def dead_letter(self, item: AnalysisWorkItem, *, reason: str) -> AnalysisQueueDeadLetter:
        """Persist terminal queue failure metadata for one work item."""


class PMReviewWorkQueuePort(Protocol):
    """Durable/resumable queue seam for PMReview work."""

    def enqueue(self, item: PMReviewWorkItem) -> PMReviewQueueEnqueueReceipt:
        """Persist one PMReview work item idempotently."""

    def dead_letter(self, item: PMReviewWorkItem, *, reason: str) -> PMReviewQueueDeadLetter:
        """Persist terminal queue failure metadata for one work item."""


class CEAUEnginePort(Protocol):
    """Central deterministic CEAU routing/store seam."""

    def handle_evidence_admitted(self, message: EvidenceAdmitted) -> CEAUEngineEffectSet:
        """Apply one admitted-evidence transition."""

    def handle_checker_decision(self, message: CheckerDecision) -> CEAUEngineEffectSet:
        """Apply one checker-decision transition."""


__all__ = [
    "AdmissionResult",
    "CEAUEngineEffectSet",
    "CEAUEnginePort",
    "AnalysisWorkQueuePort",
    "CEAULifecycleMessage",
    "CheckerWorkQueuePort",
    "EvidenceAdmissionPort",
    "PMReviewWorkQueuePort",
]
