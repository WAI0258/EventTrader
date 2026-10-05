"""Admission adapters for raw Nautilus runtime source messages."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from event_trader.contracts.evidence import EvidenceLedgerRecord
from event_trader.contracts.ports import EvidenceLedgerPort
from event_trader.ingest.admission import (
    AdmissionRequest,
    shape_admission_outputs,
)

from .contracts import (
    EvidenceAdmitted,
    EvidenceQuarantined,
    NewsRaw,
    WebResultRaw,
    base_message_kwargs,
)

type NewsAdmissionRequestLoader = Callable[[NewsRaw], AdmissionRequest]
type WebAdmissionRequestLoader = Callable[[WebResultRaw], AdmissionRequest]


class RuntimeAdmissionError(ValueError):
    """Raised when raw runtime admission wiring is invalid."""


class RawSourceUnavailable(RuntimeAdmissionError):
    """Raised by loaders when a referenced raw source record is unavailable."""


class FileBackedAdmissionQuarantineStore:
    """Durable local quarantine receipts for raw source admission failures."""

    def __init__(self, *, root: Path) -> None:
        if not isinstance(root, Path):
            raise RuntimeAdmissionError("root must be a Path.")
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def append_unavailable(
        self,
        *,
        message: NewsRaw | WebResultRaw,
        reason_code: str,
        detail: str,
    ) -> Path:
        validated_reason_code = _validate_reason_code(reason_code)
        path = self._record_path(message=message, reason_code=validated_reason_code)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                json.dumps(
                    {
                        "message_id": message.message_id,
                        "target_key": message.target_key,
                        "source_ref": message.source_ref,
                        "record_id": message.record_id,
                        "raw_topic": message.topic,
                        "reason_code": validated_reason_code,
                        "detail": detail.strip(),
                        "recorded_at": datetime.now(UTC).isoformat(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        return path

    def _record_path(
        self,
        *,
        message: NewsRaw | WebResultRaw,
        reason_code: str,
    ) -> Path:
        digest = sha256(
            "\n".join(
                (
                    message.topic,
                    message.target_key,
                    message.record_id,
                    message.source_ref,
                    reason_code,
                )
            ).encode("utf-8")
        ).hexdigest()
        return (
            self._root
            / message.target_key
            / f"{message.recorded_at:%Y-%m-%d}"
            / f"{digest}.json"
        ).resolve(strict=False)


class RuntimeEvidenceAdmissionAdapter:
    """Adapt raw source refs into admitted/quarantined runtime evidence events."""

    def __init__(
        self,
        *,
        ledger: EvidenceLedgerPort,
        load_news_request: NewsAdmissionRequestLoader,
        load_web_request: WebAdmissionRequestLoader,
        quarantine_store: FileBackedAdmissionQuarantineStore | None = None,
    ) -> None:
        append = getattr(ledger, "append", None)
        if not callable(append):
            raise RuntimeAdmissionError(
                "ledger must provide an append(record) -> event_id method."
            )
        path_for_record = getattr(ledger, "path_for_record", None)
        if not callable(path_for_record):
            raise RuntimeAdmissionError(
                "ledger must provide path_for_record(record) for stable evidence refs."
            )
        if not callable(load_news_request):
            raise RuntimeAdmissionError("load_news_request must be callable.")
        if not callable(load_web_request):
            raise RuntimeAdmissionError("load_web_request must be callable.")
        if quarantine_store is not None and not isinstance(
            quarantine_store,
            FileBackedAdmissionQuarantineStore,
        ):
            raise RuntimeAdmissionError(
                "quarantine_store must be a FileBackedAdmissionQuarantineStore."
            )
        self._ledger = ledger
        self._load_news_request = load_news_request
        self._load_web_request = load_web_request
        self._quarantine_store = quarantine_store

    def admit_news_raw(self, message: NewsRaw) -> EvidenceAdmitted | EvidenceQuarantined:
        if not isinstance(message, NewsRaw):
            raise RuntimeAdmissionError("message must be a NewsRaw instance.")
        try:
            request = self._load_news_request(message)
        except RawSourceUnavailable as exc:
            return self._quarantine_unavailable(message=message, detail=str(exc))
        return self._admit(message=message, request=request)

    def admit_web_result_raw(
        self,
        message: WebResultRaw,
    ) -> EvidenceAdmitted | EvidenceQuarantined:
        if not isinstance(message, WebResultRaw):
            raise RuntimeAdmissionError("message must be a WebResultRaw instance.")
        try:
            request = self._load_web_request(message)
        except RawSourceUnavailable as exc:
            return self._quarantine_unavailable(message=message, detail=str(exc))
        return self._admit(message=message, request=request)

    def _admit(
        self,
        *,
        message: NewsRaw | WebResultRaw,
        request: AdmissionRequest,
    ) -> EvidenceAdmitted:
        if not isinstance(request, AdmissionRequest):
            raise RuntimeAdmissionError("loader must return an AdmissionRequest.")
        _validate_request_matches_message(request=request, message=message)
        outputs = shape_admission_outputs(request, ts_init=message.recorded_at)
        appended_event_id = self._ledger.append(outputs.ledger_record)
        if appended_event_id != outputs.ledger_record.event_id:
            raise RuntimeAdmissionError(
                "ledger.append(record) must return the appended record event_id."
            )
        return EvidenceAdmitted(
            **base_message_kwargs(
                message_id=f"evidence-admitted:{outputs.runtime_event.event_id}",
                event_time=outputs.runtime_event.ts_event,
                recorded_at=message.recorded_at,
                correlation_id=message.correlation_id,
                causation_id=message.message_id,
                idempotency_key=(
                    f"evidence_admitted:{outputs.runtime_event.target_key}:"
                    f"{outputs.runtime_event.event_id}:v1"
                ),
            ),
            target_key=outputs.runtime_event.target_key,
            event_id=outputs.runtime_event.event_id,
            source_ref=outputs.runtime_event.source_ref,
            evidence_record_ref=_evidence_record_ref(
                ledger=self._ledger,
                record=outputs.ledger_record,
            ),
        )

    def _quarantine_unavailable(
        self,
        *,
        message: NewsRaw | WebResultRaw,
        detail: str,
    ) -> EvidenceQuarantined:
        reason_code = "raw_source_unavailable"
        if self._quarantine_store is None:
            quarantine_record_ref = (
                f"quarantine:{message.target_key}:{message.record_id}:{reason_code}"
            )
        else:
            quarantine_path = self._quarantine_store.append_unavailable(
                message=message,
                reason_code=reason_code,
                detail=detail,
            )
            quarantine_record_ref = str(quarantine_path.resolve(strict=False))
        return EvidenceQuarantined(
            **base_message_kwargs(
                message_id=f"evidence-quarantined:{message.record_id}",
                event_time=message.event_time,
                recorded_at=message.recorded_at,
                correlation_id=message.correlation_id,
                causation_id=message.message_id,
                idempotency_key=(
                    f"evidence_quarantined:{message.target_key}:"
                    f"{message.record_id}:{reason_code}:v1"
                ),
            ),
            target_key=message.target_key,
            source_ref=message.source_ref,
            quarantine_record_ref=quarantine_record_ref,
            reason_code=reason_code,
        )


def _validate_request_matches_message(
    *,
    request: AdmissionRequest,
    message: NewsRaw | WebResultRaw,
) -> None:
    if request.target_key != message.target_key:
        raise RuntimeAdmissionError("AdmissionRequest target_key must match raw message.")
    if request.ingress_input.source_ref != message.source_ref:
        raise RuntimeAdmissionError("AdmissionRequest source_ref must match raw message.")


def _evidence_record_ref(
    *,
    ledger: EvidenceLedgerPort,
    record: EvidenceLedgerRecord,
) -> str:
    path = ledger.path_for_record(record)
    if not isinstance(path, Path):
        raise RuntimeAdmissionError("ledger.path_for_record(record) must return a Path.")
    return f"{path.resolve(strict=False)}#{record.event_id}"


def _validate_reason_code(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeAdmissionError("reason_code must not be blank.")
    normalized = value.strip()
    if any(ch.isspace() for ch in normalized):
        raise RuntimeAdmissionError("reason_code must not contain whitespace.")
    return normalized


__all__ = [
    "FileBackedAdmissionQuarantineStore",
    "NewsAdmissionRequestLoader",
    "RawSourceUnavailable",
    "RuntimeAdmissionError",
    "RuntimeEvidenceAdmissionAdapter",
    "WebAdmissionRequestLoader",
]
