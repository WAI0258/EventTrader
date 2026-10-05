"""Public thesis revision audit surface."""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "CanonicalThesisBundleSection",
    "HistoricalThesisSnapshotReader",
    "HistoricalThesisSnapshotReaderError",
    "PersistedThesisRevision",
    "ThesisRevision",
    "ThesisRevisionBuildError",
    "ThesisRevisionBuilder",
    "ThesisRevisionContractError",
    "ThesisRevisionSectionDiff",
    "ThesisRevisionSectionSnapshot",
    "ThesisRevisionSource",
    "ThesisRevisionStore",
    "ThesisRevisionStoreError",
    "canonical_thesis_bundle_sections",
    "canonical_thesis_page_sections",
    "checker_canonical_thesis_bundle_sections",
    "is_canonical_thesis_bundle_write",
    "parse_thesis_revision",
    "workbench_canonical_thesis_bundle_sections",
]

_EXPORTS = {
    "CanonicalThesisBundleSection": (".canonical_bundle", "CanonicalThesisBundleSection"),
    "HistoricalThesisSnapshotReader": (
        ".historical_reader",
        "HistoricalThesisSnapshotReader",
    ),
    "HistoricalThesisSnapshotReaderError": (
        ".historical_reader",
        "HistoricalThesisSnapshotReaderError",
    ),
    "ThesisRevisionBuildError": (".builder", "ThesisRevisionBuildError"),
    "ThesisRevisionBuilder": (".builder", "ThesisRevisionBuilder"),
    "canonical_thesis_bundle_sections": (".canonical_bundle", "canonical_thesis_bundle_sections"),
    "canonical_thesis_page_sections": (".canonical_bundle", "canonical_thesis_page_sections"),
    "checker_canonical_thesis_bundle_sections": (
        ".canonical_bundle",
        "checker_canonical_thesis_bundle_sections",
    ),
    "is_canonical_thesis_bundle_write": (
        ".canonical_bundle",
        "is_canonical_thesis_bundle_write",
    ),
    "workbench_canonical_thesis_bundle_sections": (
        ".canonical_bundle",
        "workbench_canonical_thesis_bundle_sections",
    ),
    "ThesisRevision": (".contracts", "ThesisRevision"),
    "ThesisRevisionContractError": (".contracts", "ThesisRevisionContractError"),
    "ThesisRevisionSectionDiff": (".contracts", "ThesisRevisionSectionDiff"),
    "ThesisRevisionSectionSnapshot": (".contracts", "ThesisRevisionSectionSnapshot"),
    "ThesisRevisionSource": (".contracts", "ThesisRevisionSource"),
    "parse_thesis_revision": (".contracts", "parse_thesis_revision"),
    "PersistedThesisRevision": (".store", "PersistedThesisRevision"),
    "ThesisRevisionStore": (".store", "ThesisRevisionStore"),
    "ThesisRevisionStoreError": (".store", "ThesisRevisionStoreError"),
}


def __getattr__(name: str) -> object:
    if name not in _EXPORTS:
        raise AttributeError(name)
    module_name, attribute_name = _EXPORTS[name]
    module = import_module(module_name, __name__)
    return getattr(module, attribute_name)
