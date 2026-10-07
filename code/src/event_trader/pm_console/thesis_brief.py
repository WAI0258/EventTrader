"""Ephemeral PM Console Thesis Quick Brief generation."""

from __future__ import annotations

import json
import re
import urllib.error
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Protocol
from urllib.request import Request, urlopen

from event_trader.pm_console.schemas import (
    PMConsoleThesisQuickBriefFailureDTO,
    PMConsoleThesisQuickBriefRequestDTO,
    PMConsoleThesisQuickBriefResponseDTO,
)
from event_trader.pm_console.thesis import load_persisted_thesis_revision
from event_trader.storage import WorkspaceLayout

_PROMPT_VERSION = "thesis_quick_brief_v3"
_LEADING_THINK_BLOCK_RE = re.compile(r"^\s*<think\b[^>]*>.*?</think>\s*", re.DOTALL)


class ThesisQuickBriefGenerator(Protocol):
    def generate(self, *, prompt: str) -> tuple[str, str, str]: ...


@dataclass(frozen=True, slots=True)
class OpenAICompatibleThesisQuickBriefGenerator:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"

    def generate(self, *, prompt: str) -> tuple[str, str, str]:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return a concise, non-canonical thesis reader brief in markdown. "
                        "Do not add unsupported claims."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 700,
        }
        request = Request(
            _chat_completions_url(self.base_url),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=60) as response:
                raw_text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_text = exc.read().decode("utf-8", errors="replace")
            raise ValueError(
                f"quick brief request failed: http_status={exc.code} body={error_text[:500]}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ValueError(f"quick brief request failed: {exc}") from exc
        try:
            content = _extract_content(json.loads(raw_text))
        except json.JSONDecodeError as exc:
            raise ValueError("quick brief response was not valid JSON.") from exc
        return "configured", self.model, content


class PMConsoleThesisQuickBriefError(ValueError):
    def __init__(
        self, *, status: HTTPStatus, response: PMConsoleThesisQuickBriefFailureDTO
    ) -> None:
        super().__init__(response.error_message)
        self.status = status
        self.response = response


class PMConsoleThesisQuickBriefService:
    def __init__(
        self, layout: WorkspaceLayout, *, generator: ThesisQuickBriefGenerator | None
    ) -> None:
        self._layout = layout
        self._generator = generator

    def build_brief(
        self, *, target_key: str, revision_id: str, request: PMConsoleThesisQuickBriefRequestDTO
    ) -> PMConsoleThesisQuickBriefResponseDTO:
        persisted = load_persisted_thesis_revision(
            self._layout, target_key=target_key, revision_id=revision_id
        )
        if persisted is None:
            raise self._failure(
                HTTPStatus.NOT_FOUND,
                target_key,
                revision_id,
                request,
                "unknown_revision",
                "unknown thesis revision.",
            )
        if persisted.record.canonical_bundle_sha256 != request.canonical_bundle_sha256:
            raise self._failure(
                HTTPStatus.CONFLICT,
                target_key,
                revision_id,
                request,
                "canonical_bundle_mismatch",
                "canonical_bundle_sha256 does not match the selected thesis revision.",
            )
        if self._generator is None:
            raise self._failure(
                HTTPStatus.SERVICE_UNAVAILABLE,
                target_key,
                revision_id,
                request,
                "generator_unavailable",
                "no PM Console thesis quick brief generator is configured for this server process.",
            )
        source = "\n\n".join(
            f"## {section.section_name}\n{section.content_md}"
            for section in persisted.record.sections
            if section.content_md.strip()
        )
        if not source.strip():
            raise self._failure(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                target_key,
                revision_id,
                request,
                "source_unavailable",
                "canonical thesis revision has no readable section content.",
            )
        try:
            provider, model, brief_md = self._generator.generate(
                prompt=(
                    "Create a short reader brief from this canonical thesis revision. "
                    "This is a non-canonical aid; do not add unsupported claims.\n\n" + source
                )
            )
        except Exception as exc:
            raise self._failure(
                HTTPStatus.BAD_GATEWAY,
                target_key,
                revision_id,
                request,
                "generator_error",
                str(exc),
            ) from exc
        brief_md = _strip_leading_reasoning_blocks(brief_md).strip()
        if not brief_md:
            raise self._failure(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                target_key,
                revision_id,
                request,
                "empty_brief",
                "quick brief generator returned empty content.",
            )
        return PMConsoleThesisQuickBriefResponseDTO(
            generated_at=datetime.now(UTC).isoformat(),
            target_key=target_key,
            revision_id=revision_id,
            canonical_bundle_sha256=request.canonical_bundle_sha256,
            brief_md=brief_md,
            provider=provider,
            model=model,
            prompt_version=_PROMPT_VERSION,
        )

    def _failure(self, status, target_key, revision_id, request, error_code, error_message):
        return PMConsoleThesisQuickBriefError(
            status=status,
            response=PMConsoleThesisQuickBriefFailureDTO(
                generated_at=datetime.now(UTC).isoformat(),
                target_key=target_key,
                revision_id=revision_id,
                canonical_bundle_sha256=request.canonical_bundle_sha256,
                error_code=error_code,
                error_message=error_message,
                state="unavailable" if status == HTTPStatus.SERVICE_UNAVAILABLE else "failed",
            ),
        )


def _extract_content(payload: object) -> str:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("choices"), list)
        or not payload["choices"]
    ):
        raise ValueError("quick brief response did not contain choices.")
    choice = payload["choices"][0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content
    raise ValueError("quick brief response did not contain message.content text.")


def _strip_leading_reasoning_blocks(content: str) -> str:
    while True:
        updated = _LEADING_THINK_BLOCK_RE.sub("", content, count=1)
        if updated == content:
            return content
        content = updated


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


__all__ = [
    "OpenAICompatibleThesisQuickBriefGenerator",
    "PMConsoleThesisQuickBriefError",
    "PMConsoleThesisQuickBriefService",
]
