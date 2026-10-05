"""Operator-context contracts, parser, and durable writer for target operator.md."""

from .contracts import (
    OperatorContextCard,
    OperatorContextContractError,
    OperatorContextWriteMode,
    OperatorContextWriteModeError,
    OperatorContextWriteReceipt,
    make_operator_context_receipt_id,
)
from .parser import (
    OperatorContextParseError,
    parse_operator_context_cards,
    parse_operator_context_page,
    render_operator_context_card_block,
)
from .writer import (
    FileBackedOperatorContextWriteReceiptStore,
    OperatorContextWriter,
    OperatorContextWriterError,
    resolve_operator_page_path,
)

__all__ = [
    "FileBackedOperatorContextWriteReceiptStore",
    "OperatorContextCard",
    "OperatorContextContractError",
    "OperatorContextParseError",
    "OperatorContextWriteMode",
    "OperatorContextWriteModeError",
    "OperatorContextWriteReceipt",
    "OperatorContextWriter",
    "OperatorContextWriterError",
    "make_operator_context_receipt_id",
    "parse_operator_context_cards",
    "parse_operator_context_page",
    "render_operator_context_card_block",
    "resolve_operator_page_path",
]
