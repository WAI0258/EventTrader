"""PM Console-local Thesis Evolution translation helpers."""

from __future__ import annotations

import json
import re
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Protocol
from urllib.request import Request, urlopen

from event_trader.pm_console.schemas import (
    PMConsoleThesisTranslationAvailabilityDTO,
    PMConsoleThesisTranslationFailureDTO,
    PMConsoleThesisTranslationRequestDTO,
    PMConsoleThesisTranslationResponseDTO,
    PMConsoleThesisTranslationTranslatorDTO,
    PMConsoleThesisTranslationVerificationDTO,
    PMConsoleTranslatedSectionDTO,
)
from event_trader.pm_console.thesis import load_persisted_thesis_revision
from event_trader.storage import WorkspaceLayout

_PROMPT_VERSION = "thesis_translate_v3"
_PROTECTED_TERMS_VERSION = "v2"
_MAX_TRANSLATION_WORKERS = 4
_TRANSLATOR_UNAVAILABLE_CODE = "translator_unavailable"
_TRANSLATOR_UNAVAILABLE_MESSAGE = (
    "no PM Console thesis translator is configured for this server process."
)
_WRAPPER_PREFIXES = (
    "here is the translation",
    "translation:",
    "translated version:",
    "以下是",
    "翻译如下",
    "这是翻译",
)
_FENCED_CODE_RE = re.compile(r"(?ms)^```.*?^```[ \t]*$")
_INLINE_CODE_RE = re.compile(r"(?<!`)`[^`\n]+`(?!`)")
_LINK_TARGET_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_URL_RE = re.compile(r"https?://[^\s)>]+")
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\[^\s`]+|(?:targets|research_memory|runtime|ledger|helpers)/[^\s`):]+)"
)
_SNAKE_CASE_ID_RE = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b")
_KEBAB_CASE_ID_RE = re.compile(r"\b[a-z0-9]+(?:-[a-z0-9]+){2,}\b")
_LEADING_THINK_BLOCK_RE = re.compile(r"^\s*<think\b[^>]*>.*?</think>\s*", re.DOTALL)


class PMConsoleThesisTranslationError(ValueError):
    """Raised when Thesis Evolution translation cannot complete."""


class PMConsoleThesisTranslationFailure(PMConsoleThesisTranslationError):
    """Raised when the translation route should return an explicit failure payload."""

    def __init__(
        self,
        *,
        status: HTTPStatus,
        response: PMConsoleThesisTranslationFailureDTO,
    ) -> None:
        super().__init__(response.error_message)
        self.status = status
        self.response = response


@dataclass(frozen=True, slots=True)
class ThesisSectionTranslationResult:
    provider: str
    model: str
    translated_content_md: str


class ThesisSectionTranslator(Protocol):
    """Translate one thesis markdown section."""

    def translate(
        self,
        *,
        prompt: str,
        source_content_md: str,
        locale: str,
    ) -> ThesisSectionTranslationResult: ...


@dataclass(frozen=True, slots=True)
class OpenAICompatibleThesisSectionTranslator:
    """Minimal OpenAI-compatible translator for PM Console-local use."""

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"

    def translate(
        self,
        *,
        prompt: str,
        source_content_md: str,
        locale: str,
    ) -> ThesisSectionTranslationResult:
        del source_content_md, locale
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _translation_system_prompt()},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        payload["max_tokens"] = 4096
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            _chat_completions_url(self.base_url),
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "event-trader-pm-console/0.1",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=90) as response:
                raw_text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_text = exc.read().decode("utf-8", errors="replace")
            raise PMConsoleThesisTranslationError(
                "pm console translation request failed: "
                f"http_status={exc.code} body={error_text[:500]}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise PMConsoleThesisTranslationError(
                f"pm console translation request failed: {exc}"
            ) from exc
        try:
            response_payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise PMConsoleThesisTranslationError(
                "pm console translation response was not valid JSON."
            ) from exc
        content = _extract_chat_message_content(response_payload)
        return ThesisSectionTranslationResult(
            provider="openai",
            model=self.model,
            translated_content_md=_strip_leading_reasoning_blocks(content),
        )


class PMConsoleThesisTranslationService:
    """Translate Thesis Evolution revisions without changing thesis truth."""

    def __init__(
        self,
        layout: WorkspaceLayout,
        *,
        translator: ThesisSectionTranslator | None,
    ) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise PMConsoleThesisTranslationError("layout must be a WorkspaceLayout instance.")
        self._layout = layout
        self._translator = translator

    def translate_revision(
        self,
        *,
        target_key: str,
        revision_id: str,
        request: PMConsoleThesisTranslationRequestDTO,
    ) -> PMConsoleThesisTranslationResponseDTO:
        persisted = load_persisted_thesis_revision(
            self._layout,
            target_key=target_key,
            revision_id=revision_id,
        )
        if persisted is None:
            raise self._failure(
                status=HTTPStatus.NOT_FOUND,
                target_key=target_key,
                revision_id=revision_id,
                request=request,
                error_code="unknown_revision",
                error_message=(
                    f"unknown thesis revision: target={target_key} revision_id={revision_id}"
                ),
            )
        if persisted.record.canonical_bundle_sha256 != request.canonical_bundle_sha256:
            raise self._failure(
                status=HTTPStatus.CONFLICT,
                target_key=target_key,
                revision_id=revision_id,
                request=request,
                error_code="canonical_bundle_mismatch",
                error_message=(
                    "canonical_bundle_sha256 does not match the selected thesis revision."
                ),
            )
        if self._translator is None:
            raise self._failure(
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                target_key=target_key,
                revision_id=revision_id,
                request=request,
                error_code=_TRANSLATOR_UNAVAILABLE_CODE,
                error_message=_TRANSLATOR_UNAVAILABLE_MESSAGE,
            )
        translator_dto = PMConsoleThesisTranslationTranslatorDTO(
            provider="configured",
            model=_translator_model_name(self._translator),
            prompt_version=_PROMPT_VERSION,
        )
        translated_sections: list[PMConsoleTranslatedSectionDTO | None] = [None] * len(
            persisted.record.sections
        )
        verification_failures: list[str] = []
        translation_failures: list[str] = []
        translator_metadata: dict[int, PMConsoleThesisTranslationTranslatorDTO] = {}
        jobs: list[tuple[int, object]] = []
        for index, section in enumerate(persisted.record.sections):
            if not section.content_md.strip():
                translated_sections[index] = _translated_section(
                    section=section,
                    translated_content_md=section.content_md,
                )
                continue
            jobs.append((index, section))

        def translate_one(index: int, section: object):
            protected_terms = _extract_protected_terms(section.content_md)
            prompt = _build_translation_prompt(
                locale=request.locale,
                protected_terms=protected_terms,
                source_content_md=section.content_md,
            )
            translated = self._translator.translate(
                prompt=prompt,
                source_content_md=section.content_md,
                locale=request.locale,
            )
            verification = _verify_translation(
                source_content_md=section.content_md,
                translated_content_md=translated.translated_content_md,
                protected_terms=protected_terms,
            )
            return index, section, translated, verification

        worker_count = min(_MAX_TRANSLATION_WORKERS, max(1, len(jobs)))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="pm-console-translate",
        ) as executor:
            futures = {
                executor.submit(translate_one, index, section): (index, section)
                for index, section in jobs
            }
            for future in as_completed(futures):
                index, section = futures[future]
                try:
                    _, section, translated, verification = future.result()
                except Exception as exc:
                    translation_failures.append(f"{section.section_id}: {exc}")
                    continue
                translator_metadata[index] = PMConsoleThesisTranslationTranslatorDTO(
                    provider=translated.provider,
                    model=translated.model,
                    prompt_version=_PROMPT_VERSION,
                )
                if not verification.passed:
                    verification_failures.extend(
                        f"{section.section_id}: {failure}" for failure in verification.failures
                    )
                    continue
                translated_sections[index] = _translated_section(
                    section=section,
                    translated_content_md=translated.translated_content_md,
                )

        if translator_metadata:
            translator_dto = translator_metadata[min(translator_metadata)]

        if translation_failures:
            raise self._failure(
                status=HTTPStatus.BAD_GATEWAY,
                target_key=target_key,
                revision_id=revision_id,
                request=request,
                error_code="translator_error",
                error_message="; ".join(sorted(translation_failures)),
                translator=translator_dto,
            )
        if any(section is None for section in translated_sections):
            raise self._failure(
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
                target_key=target_key,
                revision_id=revision_id,
                request=request,
                error_code="verification_failed",
                error_message="translation did not produce every thesis section.",
                translator=translator_dto,
            )
        verification = PMConsoleThesisTranslationVerificationDTO(
            passed=not verification_failures,
            failures=tuple(verification_failures),
        )
        if not verification.passed:
            raise self._failure(
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
                target_key=target_key,
                revision_id=revision_id,
                request=request,
                error_code="verification_failed",
                error_message="translated output failed verification.",
                translator=translator_dto,
                verification=verification,
            )
        completed_sections = tuple(
            section for section in translated_sections if section is not None
        )
        response = PMConsoleThesisTranslationResponseDTO(
            generated_at=datetime.now(UTC).isoformat(),
            target_key=target_key,
            revision_id=revision_id,
            locale=request.locale,
            canonical_bundle_sha256=request.canonical_bundle_sha256,
            sections=completed_sections,
            translator=translator_dto,
            verification=verification,
        )
        return response

    def translation_availability(
        self,
        *,
        locale: str,
    ) -> PMConsoleThesisTranslationAvailabilityDTO:
        return inspect_thesis_translation_availability(
            translator=self._translator,
            locale=locale,
        )

    def _failure(
        self,
        *,
        status: HTTPStatus,
        target_key: str,
        revision_id: str,
        request: PMConsoleThesisTranslationRequestDTO,
        error_code: str,
        error_message: str,
        translator: PMConsoleThesisTranslationTranslatorDTO | None = None,
        verification: PMConsoleThesisTranslationVerificationDTO | None = None,
    ) -> PMConsoleThesisTranslationFailure:
        return PMConsoleThesisTranslationFailure(
            status=status,
            response=PMConsoleThesisTranslationFailureDTO(
                generated_at=datetime.now(UTC).isoformat(),
                target_key=target_key,
                revision_id=revision_id,
                locale=request.locale,
                canonical_bundle_sha256=request.canonical_bundle_sha256,
                error_code=error_code,
                error_message=error_message,
                translator=translator,
                verification=verification,
            ),
        )


def inspect_thesis_translation_availability(
    *,
    translator: ThesisSectionTranslator | None,
    locale: str,
) -> PMConsoleThesisTranslationAvailabilityDTO:
    if translator is None:
        return PMConsoleThesisTranslationAvailabilityDTO(
            locale=locale,
            available=False,
            reason_code=_TRANSLATOR_UNAVAILABLE_CODE,
            reason_message=_TRANSLATOR_UNAVAILABLE_MESSAGE,
        )
    return PMConsoleThesisTranslationAvailabilityDTO(
        locale=locale,
        available=True,
    )


def _build_translation_prompt(
    *,
    locale: str,
    protected_terms: tuple[str, ...],
    source_content_md: str,
) -> str:
    protected_block = "\n".join(f"- {term}" for term in protected_terms) or "- (none)"
    return (
        f"Translate the markdown below into {locale}.\n"
        "Translate only. Do not summarize, simplify, rewrite, explain, expand, or add wrappers.\n"
        "Preserve markdown structure, heading levels, bullets, tables, links, inline code, "
        "and fenced code blocks. Keep explicit URLs, paths, code, and IDs unchanged.\n"
        "Protected terms that must remain unchanged:\n"
        f"{protected_block}\n\n"
        "Return only translated markdown.\n"
        "<source_markdown>\n"
        f"{source_content_md}\n"
        "</source_markdown>\n"
    )


def _translation_system_prompt() -> str:
    return (
        "You are translating PM Console thesis markdown into Simplified Chinese. "
        "Return only the translated markdown. Do not add commentary, labels, summary text, "
        "or code fences around the whole answer."
    )


def _extract_protected_terms(source_content_md: str) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    for pattern in (
        _FENCED_CODE_RE,
        _INLINE_CODE_RE,
        _URL_RE,
        _PATH_RE,
        _SNAKE_CASE_ID_RE,
        _KEBAB_CASE_ID_RE,
    ):
        for match in pattern.finditer(source_content_md):
            term = match.group(0).strip()
            if pattern not in {_FENCED_CODE_RE, _INLINE_CODE_RE}:
                term = term.rstrip(".,;:")
            if not term or term in seen:
                continue
            seen.add(term)
            terms.append(term)
    return tuple(terms)


def _translated_section_from_payload(payload: object) -> PMConsoleTranslatedSectionDTO:
    if not isinstance(payload, dict):
        raise PMConsoleThesisTranslationError("translated section payload must be an object.")
    translated_content_md = payload.get("translated_content_md")
    if not isinstance(translated_content_md, str):
        raise PMConsoleThesisTranslationError(
            "translated section payload must include translated_content_md."
        )
    return PMConsoleTranslatedSectionDTO(
        section_id=_require_non_blank(payload.get("section_id"), "section_id"),
        page_path=_require_non_blank(payload.get("page_path"), "page_path"),
        page_name=_require_non_blank(payload.get("page_name"), "page_name"),
        section_name=_require_non_blank(payload.get("section_name"), "section_name"),
        source_content_sha256=_require_non_blank(
            payload.get("source_content_sha256"),
            "source_content_sha256",
        ),
        translated_content_md=_strip_leading_reasoning_blocks(translated_content_md),
    )


def _verify_translation(
    *,
    source_content_md: str,
    translated_content_md: str,
    protected_terms: tuple[str, ...],
) -> PMConsoleThesisTranslationVerificationDTO:
    del protected_terms
    failures: list[str] = []
    if not translated_content_md.strip():
        failures.append("translated_content_md is empty.")
    normalized_prefix = translated_content_md.strip().lower()
    for wrapper_prefix in _WRAPPER_PREFIXES:
        if normalized_prefix.startswith(wrapper_prefix):
            failures.append("translated_content_md includes wrapper text.")
            break
    if _count_fenced_code_blocks(translated_content_md) != _count_fenced_code_blocks(
        source_content_md
    ):
        failures.append("fenced code block count changed.")
    if _link_targets(translated_content_md) != _link_targets(source_content_md):
        failures.append("link targets changed.")
    if _is_obviously_shortened_summary(
        source_content_md=source_content_md,
        translated_content_md=translated_content_md,
    ):
        failures.append("translated output is suspiciously short.")
    return PMConsoleThesisTranslationVerificationDTO(
        passed=not failures,
        failures=tuple(failures),
    )


def _count_fenced_code_blocks(content_md: str) -> int:
    return len(_FENCED_CODE_RE.findall(content_md))


def _link_targets(content_md: str) -> tuple[str, ...]:
    return tuple(match.group(1).strip() for match in _LINK_TARGET_RE.finditer(content_md))


def _is_obviously_shortened_summary(
    *,
    source_content_md: str,
    translated_content_md: str,
) -> bool:
    source_units = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", source_content_md))
    translated_units = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", translated_content_md))
    if source_units < 80:
        return False
    return translated_units < max(20, source_units // 5)


def _extract_chat_message_content(payload: object) -> str:
    if not isinstance(payload, dict):
        raise PMConsoleThesisTranslationError("translation response must be a JSON object.")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise PMConsoleThesisTranslationError("translation response did not contain choices.")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise PMConsoleThesisTranslationError("translation response choice was malformed.")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise PMConsoleThesisTranslationError("translation response message was malformed.")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        combined = "".join(text_parts).strip()
        if combined:
            return combined
    raise PMConsoleThesisTranslationError(
        "translation response did not contain message.content text."
    )


def _strip_leading_reasoning_blocks(content: str) -> str:
    sanitized = content
    while True:
        updated = _LEADING_THINK_BLOCK_RE.sub("", sanitized, count=1)
        if updated == sanitized:
            break
        sanitized = updated
    return sanitized


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _translated_section(
    *,
    section,
    translated_content_md: str,
) -> PMConsoleTranslatedSectionDTO:
    return PMConsoleTranslatedSectionDTO(
        section_id=section.section_id,
        page_path=section.page_path,
        page_name=_page_name_from_page_path(section.page_path),
        section_name=section.section_name,
        source_content_sha256=section.content_sha256,
        translated_content_md=translated_content_md,
    )


def _translator_model_name(translator: ThesisSectionTranslator | None) -> str:
    model = getattr(translator, "model", None)
    return model.strip() if isinstance(model, str) and model.strip() else "unconfigured"


def _page_name_from_page_path(page_path: str) -> str:
    normalized = page_path.replace("\\", "/").rstrip("/")
    if not normalized:
        return page_path
    return normalized.rsplit("/", 1)[-1]


def _require_non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PMConsoleThesisTranslationError(f"{field_name} must be a non-blank string.")
    return value.strip()


__all__ = [
    "OpenAICompatibleThesisSectionTranslator",
    "PMConsoleThesisTranslationError",
    "PMConsoleThesisTranslationFailure",
    "PMConsoleThesisTranslationService",
    "ThesisSectionTranslationResult",
    "ThesisSectionTranslator",
    "inspect_thesis_translation_availability",
]
