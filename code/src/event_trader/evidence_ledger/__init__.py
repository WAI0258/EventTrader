"""Thin file-backed evidence-ledger surface."""

from .file_backed import (
    FileBackedEvidenceLedger,
    FileBackedEvidenceLedgerError,
    TargetLedgerLayout,
    build_target_ledger_layout,
    initialize_target_ledger_layout,
    read_evidence,
    read_evidence_window,
)

__all__ = [
    "FileBackedEvidenceLedger",
    "FileBackedEvidenceLedgerError",
    "TargetLedgerLayout",
    "build_target_ledger_layout",
    "initialize_target_ledger_layout",
    "read_evidence",
    "read_evidence_window",
]
