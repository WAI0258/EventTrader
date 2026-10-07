from __future__ import annotations


class StructuredPromptBudgetError(ValueError):
    """Raised when a structured prompt cannot fit beside its output reserve."""


def validate_structured_token_budget(
    *,
    max_context_tokens: int,
    max_output_tokens: int,
    field_prefix: str,
) -> None:
    for field_name, value in (
        ("llm_max_context_length", max_context_tokens),
        ("llm_max_output_tokens", max_output_tokens),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise StructuredPromptBudgetError(
                f"{field_prefix}.{field_name} must be a positive integer."
            )
    if max_output_tokens >= max_context_tokens:
        raise StructuredPromptBudgetError(
            f"{field_prefix}.llm_max_output_tokens must be smaller than "
            f"{field_prefix}.llm_max_context_length."
        )


def assert_structured_prompt_budget(
    prompt: str,
    *,
    max_context_tokens: int,
    max_output_tokens: int,
    stage: str,
    error_type: type[Exception],
) -> int:
    estimated_input_tokens = estimate_prompt_tokens(prompt)
    input_budget = max_context_tokens - max_output_tokens
    if estimated_input_tokens > input_budget:
        raise error_type(
            f"{stage} prompt exceeds local prompt budget: "
            f"estimated_input_tokens={estimated_input_tokens} "
            f"input_token_budget={input_budget} "
            f"context_tokens={max_context_tokens} "
            f"reserved_output_tokens={max_output_tokens}."
        )
    return estimated_input_tokens


def estimate_prompt_tokens(prompt: str) -> int:
    non_ascii = sum(1 for character in prompt if ord(character) > 127)
    ascii_count = len(prompt) - non_ascii
    return non_ascii + (ascii_count + 2) // 3


__all__ = [
    "StructuredPromptBudgetError",
    "assert_structured_prompt_budget",
    "estimate_prompt_tokens",
    "validate_structured_token_budget",
]
