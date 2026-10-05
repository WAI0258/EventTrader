"""Dedicated writer and receipt store for operator-owned `operator.md` writes."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from event_trader.contracts.research_memory import ResearchMemoryContractError, resolve_page_ref
from event_trader.operator_context.contracts import (
    OperatorContextCard,
    OperatorContextWriteMode,
    OperatorContextWriteModeError,
    OperatorContextWriteReceipt,
    make_operator_context_receipt_id,
)
from event_trader.operator_context.parser import (
    OperatorContextParseError,
    parse_operator_context_page,
    render_operator_context_card_block,
)
from event_trader.storage import WorkspaceLayout

_CONTEXT_BLOCK_RE = re.compile(
    r"<!-- operator-context:start (?P<attrs>[^\n]+) -->\n"
    r"(?P<body>.*?)\n<!-- operator-context:end -->",
    re.DOTALL,
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)
_SECTION_NAME = "Context"
_VALID_MODES = frozenset(
    {
        "append_context_card",
        "retire_context_card",
        "rewrite_operator_page",
    }
)
_CANONICAL_SECTION_PLACEHOLDER = "operator.md"


class OperatorContextWriterError(ValueError):
    """Raised when a dedicated operator-context write call is invalid."""


class OperatorContextWriter:
    """Apply parser-validated changes to `targets/<target>/operator.md`."""

    def __init__(
        self,
        *,
        layout: WorkspaceLayout,
        target_key: str,
        operator_command_id: str,
        receipt_store: FileBackedOperatorContextWriteReceiptStore | None = None,
    ) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise OperatorContextWriterError("layout must be a WorkspaceLayout instance.")
        page_path = f"targets/{target_key}/operator.md"
        page_ref = resolve_page_ref(page_path)
        if page_ref.page_kind != "operator":
            raise OperatorContextWriterError(
                "operator writer supports only operator pages."
            )
        if page_ref.target_key != target_key:
            raise OperatorContextWriterError("target_key does not match operator page path.")

        self._layout = layout
        self._target_key = target_key
        self._operator_command_id = _normalize_text(
            operator_command_id,
            field_name="operator_command_id",
        )
        self._page_path = page_path
        self._file_path = resolve_operator_page_path(layout=layout, target_key=target_key)
        self._receipt_store = (
            receipt_store or FileBackedOperatorContextWriteReceiptStore(layout=layout)
        )

        if not self._file_path.exists():
            raise OperatorContextWriterError(
                f"Expected operator page to exist at: {self._file_path}"
            )

    @property
    def target_key(self) -> str:
        return self._target_key

    @property
    def page_path(self) -> str:
        return self._page_path

    def append_context_card(
        self,
        *,
        card: OperatorContextCard,
        business_at: datetime | None = None,
        committed_at: datetime | None = None,
    ) -> OperatorContextWriteReceipt:
        """Append one context card block and validate the complete resulting page."""
        before = self._read_operator_page()
        before_cards = self._parse_operator_page(before)

        if any(existing.card_id == card.card_id for existing in before_cards):
            raise OperatorContextWriterError(
                f"operator context card id already exists: {card.card_id!r}."
            )
        if card.section_name != _SECTION_NAME:
            raise OperatorContextWriterError(
                f"append_context_card section is not writable: {card.section_name!r}."
            )

        after = self._insert_card_block(before, card=card)
        after_cards = self._parse_operator_page(after)
        return self._commit_change(
            before_content=before,
            after_content=after,
            section_name=card.section_name,
            write_mode="append_context_card",
            before_cards=before_cards,
            after_cards=after_cards,
            business_at=business_at,
            committed_at=committed_at,
        )

    def retire_context_card(
        self,
        *,
        card_id: str,
        business_at: datetime | None = None,
        committed_at: datetime | None = None,
    ) -> OperatorContextWriteReceipt:
        """Mark one card as retired and preserve full markdown deterministically."""
        before = self._read_operator_page()
        before_cards = self._parse_operator_page(before)

        card_id = _normalize_text(card_id, field_name="card_id")
        target_card = next(
            (card for card in before_cards if card.card_id == card_id),
            None,
        )
        if target_card is None:
            raise OperatorContextWriterError(f"no operator context card {card_id!r} found.")
        if target_card.is_retired:
            raise OperatorContextWriterError(
                f"operator context card {card_id!r} is already retired."
            )

        after = _retire_card_block(before, card_id=card_id)
        after_cards = self._parse_operator_page(after)
        return self._commit_change(
            before_content=before,
            after_content=after,
            section_name=target_card.section_name,
            write_mode="retire_context_card",
            before_cards=before_cards,
            after_cards=after_cards,
            business_at=business_at,
            committed_at=committed_at,
        )

    def rewrite_operator_page(
        self,
        *,
        content_md: str,
        business_at: datetime | None = None,
        committed_at: datetime | None = None,
    ) -> OperatorContextWriteReceipt:
        """Replace the complete operator.md payload after full-page validation."""
        before = self._read_operator_page()
        try:
            before_cards = self._parse_operator_page(before)
            after_cards = self._parse_operator_page(content_md)
        except OperatorContextWriterError as exc:
            raise OperatorContextWriterError(
                f"operator.md fixed section skeleton is invalid: {exc}"
            ) from exc

        return self._commit_change(
            before_content=before,
            after_content=content_md,
            section_name=_CANONICAL_SECTION_PLACEHOLDER,
            write_mode="rewrite_operator_page",
            before_cards=before_cards,
            after_cards=after_cards,
            business_at=business_at,
            committed_at=committed_at,
        )

    def _read_operator_page(self) -> str:
        return self._file_path.read_text(encoding="utf-8")

    def _parse_operator_page(self, content_md: str) -> tuple[OperatorContextCard, ...]:
        try:
            return parse_operator_context_page(content_md)
        except (OperatorContextParseError, ResearchMemoryContractError) as exc:
            raise OperatorContextWriterError(str(exc)) from exc

    def _commit_change(
        self,
        *,
        before_content: str,
        after_content: str,
        section_name: str,
        write_mode: OperatorContextWriteMode,
        before_cards: tuple[OperatorContextCard, ...],
        after_cards: tuple[OperatorContextCard, ...],
        business_at: datetime | None,
        committed_at: datetime | None,
    ) -> OperatorContextWriteReceipt:
        if write_mode not in _VALID_MODES:
            raise OperatorContextWriteModeError(f"unsupported write_mode: {write_mode!r}.")
        if not isinstance(before_cards, tuple):
            raise OperatorContextWriterError("before_cards must be a tuple.")
        if not isinstance(after_cards, tuple):
            raise OperatorContextWriterError("after_cards must be a tuple.")

        receipt = self._build_receipt_for_commit(
            section_name=section_name,
            write_mode=write_mode,
            before_content=before_content,
            before_cards=before_cards,
            after_content=after_content,
            after_cards=after_cards,
            business_at=business_at,
            committed_at=committed_at,
        )
        self._file_path.write_text(after_content, encoding="utf-8")
        try:
            self._receipt_store.append_receipt(receipt)
        except Exception:
            self._file_path.write_text(before_content, encoding="utf-8")
            raise
        return receipt

    def _build_receipt_for_commit(
        self,
        *,
        section_name: str,
        write_mode: OperatorContextWriteMode,
        before_content: str,
        before_cards: tuple[OperatorContextCard, ...],
        after_content: str,
        after_cards: tuple[OperatorContextCard, ...],
        business_at: datetime | None,
        committed_at: datetime | None,
    ) -> OperatorContextWriteReceipt:
        committed_at = _coerce_committed_time(committed_at)
        business_at = _coerce_business_time(business_at, committed_at)
        changed_card_ids = self._diff_card_ids(
            before_cards=before_cards,
            after_cards=after_cards,
        )
        return OperatorContextWriteReceipt(
            receipt_id=make_operator_context_receipt_id(
                section_name=section_name,
                write_mode=write_mode,
                target_key=self._target_key,
                operator_command_id=self._operator_command_id,
                committed_at=committed_at,
            ),
            target_key=self._target_key,
            page_path=self._page_path,
            section_name=section_name,
            write_mode=write_mode,
            operator_command_id=self._operator_command_id,
            authoritative_write_intent=True,
            business_at=business_at,
            committed_at=committed_at,
            before_sha256=sha256(before_content.encode("utf-8")).hexdigest(),
            after_sha256=sha256(after_content.encode("utf-8")).hexdigest(),
            changed_card_ids=changed_card_ids,
        )

    def _insert_card_block(self, content_md: str, *, card: OperatorContextCard) -> str:
        sections = _parse_sections(content_md)
        section_map = {name: (start, end) for name, start, end in sections}
        start, end = section_map[_SECTION_NAME]
        section = content_md[start:end]
        block = render_operator_context_card_block(card)
        normalized_section = section.rstrip()
        if normalized_section:
            replacement = f"{normalized_section}\n\n{block}\n"
        else:
            replacement = f"{section}\n{block}\n"
        return f"{content_md[:start]}{replacement}{content_md[end:]}"

    def _diff_card_ids(
        self,
        before_cards: tuple[OperatorContextCard, ...],
        after_cards: tuple[OperatorContextCard, ...],
    ) -> tuple[str, ...]:
        before_map = {card.card_id: card for card in before_cards}
        after_map = {card.card_id: card for card in after_cards}
        changed_ids = [
            card_id
            for card_id in sorted(set(before_map) | set(after_map))
            if before_map.get(card_id) != after_map.get(card_id)
        ]
        return tuple(changed_ids)


class FileBackedOperatorContextWriteReceiptStore:
    """Dedicated JSONL-backed store for operator-context write receipts."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise OperatorContextWriterError(
                "layout must be a WorkspaceLayout instance."
            )
        self._layout = layout

    def append_receipt(self, receipt: OperatorContextWriteReceipt) -> str:
        path = self.receipt_path(
            target_key=receipt.target_key,
            year_month=receipt.business_at.strftime("%Y-%m"),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if _jsonl_record_exists(path, receipt_id=receipt.receipt_id):
            return receipt.receipt_id
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(receipt.to_json_payload(), ensure_ascii=False))
            handle.write("\n")
        return receipt.receipt_id

    def read_receipts(
        self,
        *,
        target_key: str,
        year_month: str,
    ) -> tuple[dict[str, object], ...]:
        path = self.receipt_path(target_key=target_key, year_month=year_month)
        if not path.exists():
            return ()
        payloads: list[dict[str, object]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                normalized = line.strip()
                if not normalized:
                    continue
                payload = json.loads(normalized)
                if not isinstance(payload, dict):
                    raise OperatorContextWriterError(
                        "operator-context receipt records must be JSON objects."
                    )
                payloads.append(payload)
        return tuple(payloads)

    def receipt_path(self, *, target_key: str, year_month: str) -> Path:
        normalized_target = target_key.strip()
        if not normalized_target:
            raise OperatorContextWriterError("target_key must not be blank.")
        normalized_month = year_month.strip()
        if not normalized_month:
            raise OperatorContextWriterError("year_month must not be blank.")
        return (
            self._layout.runtime_root
            / "operator_context"
            / "writes"
            / normalized_target
            / f"{normalized_month}.jsonl"
        ).resolve(strict=False)


def resolve_operator_page_path(layout: WorkspaceLayout, target_key: str) -> Path:
    """Return absolute path for one target `operator.md` page."""
    page_ref = resolve_page_ref(f"targets/{target_key}/operator.md")
    if page_ref.target_key != target_key:
        raise OperatorContextWriterError(
            "target_key does not match operator page resolution."
        )
    from event_trader.contracts.research_memory import resolve_page_path

    return resolve_page_path(layout, page_ref.page_path)


def _parse_sections(content_md: str) -> tuple[tuple[str, int, int], ...]:
    headings = list(_HEADING_RE.finditer(content_md))
    sections: list[tuple[str, int, int]] = []
    for index, heading in enumerate(headings):
        if len(heading.group(1)) != 2:
            continue
        title = heading.group(2).strip()
        if title != _SECTION_NAME:
            continue
        start = heading.end()
        end = len(content_md)
        for later in headings[index + 1 :]:
            if len(later.group(1)) == 2:
                end = later.start()
                break
        sections.append((title, start, end))
    if [name for name, _, _ in sections] == [_SECTION_NAME]:
        return tuple(sections)
    raise OperatorContextWriterError("operator.md missing required fixed sections.")


def _retire_card_block(content_md: str, *, card_id: str) -> str:
    block_match = None
    for candidate in _CONTEXT_BLOCK_RE.finditer(content_md):
        attrs = _parse_card_attrs(candidate.group("attrs"))
        if attrs.get("id") == card_id:
            block_match = candidate
            break
    if block_match is None:
        raise OperatorContextWriterError(f"no card block found for id {card_id!r}.")
    attrs = _parse_card_attrs(block_match.group("attrs"))
    attrs["is_retired"] = "true"
    rendered = (
        f"<!-- operator-context:start {_render_card_attrs(attrs)} -->\n"
        f"{block_match.group('body').rstrip()}\n"
        "<!-- operator-context:end -->"
    )
    return (
        f"{content_md[:block_match.start()]}"
        f"{rendered}"
        f"{content_md[block_match.end():]}"
    )


def _parse_card_attrs(raw: str) -> dict[str, str]:
    parts = raw.split()
    fields: dict[str, str] = {}
    for part in parts:
        key, equals, value = part.partition("=")
        if not equals:
            raise OperatorContextWriterError(f"Malformed operator-context attribute: {part!r}.")
        if key in fields:
            raise OperatorContextWriterError(
                f"Duplicate operator-context attribute {key!r}."
            )
        fields[key] = _strip_quoted_text(value)
    return fields


def _render_card_attrs(fields: dict[str, str]) -> str:
    values = []
    for key in ("id", "is_retired"):
        if key == "is_retired" and key not in fields:
            continue
        value = fields[key]
        if " " in value:
            value = f'"{value}"'
        values.append(f"{key}={value}")
    return " ".join(values)


def _coerce_business_time(
    business_at: datetime | None,
    committed_at: datetime,
) -> datetime:
    if business_at is not None:
        return business_at.astimezone(UTC)
    return committed_at.astimezone(UTC)


def _coerce_committed_time(committed_at: datetime | None) -> datetime:
    if committed_at is None:
        return datetime.now(tz=UTC)
    return committed_at.astimezone(UTC)


def _strip_quoted_text(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def _normalize_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperatorContextWriterError(f"{field_name} must be a non-blank string.")
    normalized = value.strip()
    if normalized != value:
        raise OperatorContextWriterError(f"{field_name} must not include whitespace.")
    return normalized


def _jsonl_record_exists(path: Path, *, receipt_id: str) -> bool:
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            normalized = line.strip()
            if not normalized:
                continue
            payload = json.loads(normalized)
            if (
                isinstance(payload, dict)
                and payload.get("receipt_id") == receipt_id
            ):
                return True
    return False


__all__ = [
    "FileBackedOperatorContextWriteReceiptStore",
    "OperatorContextWriter",
    "OperatorContextWriterError",
    "resolve_operator_page_path",
]
