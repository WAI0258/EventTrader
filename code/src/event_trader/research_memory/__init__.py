"""Thin project-owned write surfaces for business-semantic research-memory pages."""

from .active_price_basis_alignment import (
    ActivePriceBasisAlignmentError,
    active_price_basis_projection_ratio,
    align_analysis_assessment_to_active_price_basis,
    market_context_adjustment_policy_for_target,
)
from .analysis_price_basis_rebase import (
    AnalysisPriceBasisRebaseError,
    bootstrap_analysis_price_semantics_for_cutover,
    rebase_analysis_assessment,
    rebase_analysis_price_semantics,
    rebase_price_level_role,
    rescale_analysis_assessment,
)
from .claim_registry import (
    FileBackedClaimRegistry,
    ResearchClaim,
    ResearchClaimChange,
    ResearchClaimRegistryError,
    ResearchClaimStatus,
    ResearchClaimUpdate,
    apply_claim_updates_for_write,
    extract_claim_updates_from_markdown,
)
from .index_log_writer import FileBackedIndexLogWriter, ResearchMemoryWrapperError
from .page_writes import (
    FileBackedResearchMemoryPageWriter,
    ResearchMemoryPageWriteError,
)
from .write_receipts import (
    FileBackedResearchMemoryWriteReceiptStore,
    ReceiptedResearchMemoryPageWriter,
    ResearchMemoryWriteAttribution,
    ResearchMemoryWriteReceipt,
    ResearchMemoryWriteReceiptError,
)

__all__ = [
    "ActivePriceBasisAlignmentError",
    "AnalysisPriceBasisRebaseError",
    "FileBackedClaimRegistry",
    "FileBackedIndexLogWriter",
    "FileBackedResearchMemoryPageWriter",
    "FileBackedResearchMemoryWriteReceiptStore",
    "ReceiptedResearchMemoryPageWriter",
    "ResearchClaim",
    "ResearchClaimChange",
    "ResearchClaimRegistryError",
    "ResearchClaimStatus",
    "ResearchClaimUpdate",
    "ResearchMemoryPageWriteError",
    "ResearchMemoryWriteAttribution",
    "ResearchMemoryWriteReceipt",
    "ResearchMemoryWriteReceiptError",
    "ResearchMemoryWrapperError",
    "active_price_basis_projection_ratio",
    "apply_claim_updates_for_write",
    "align_analysis_assessment_to_active_price_basis",
    "bootstrap_analysis_price_semantics_for_cutover",
    "extract_claim_updates_from_markdown",
    "market_context_adjustment_policy_for_target",
    "rebase_analysis_assessment",
    "rebase_analysis_price_semantics",
    "rebase_price_level_role",
    "rescale_analysis_assessment",
]
