"""Provider-neutral PMReview agent runtime failures."""


class PMReviewAgentRuntimeError(RuntimeError):
    """Raised when one PMReview agent run cannot be trusted."""


class PMReviewStructuralRuntimeError(PMReviewAgentRuntimeError):
    """Raised when required runtime-owned PMReview state cannot be materialized."""


__all__ = ["PMReviewAgentRuntimeError", "PMReviewStructuralRuntimeError"]
