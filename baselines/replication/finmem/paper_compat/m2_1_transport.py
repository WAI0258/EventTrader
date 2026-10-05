from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from puppy.chat import LongerThanContextError


@dataclass
class UsageTracker:
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "llm_calls": self.llm_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }


USAGE_TRACKER = UsageTracker()


class MiniMaxOpenAITransport:
    """Transport-only replacement for FinMem's original chat client.

    Guardrails still owns the prompt, validation, one reask, and fallback behavior.
    This class only sends its supplied prompt to the OpenAI-compatible M2.1 endpoint.
    """

    def __init__(
        self,
        end_point: str,
        model: str,
        system_message: str = "You are a helpful assistant.",
        other_parameters: dict[str, Any] | None = None,
    ) -> None:
        self.end_point = end_point
        self.model = model
        self.system_message = system_message
        self.other_parameters = dict(other_parameters or {})
        self.api_key_env = str(self.other_parameters.pop("api_key_env", "MINIMAX_API_KEY"))
        self.api_key = os.environ.get(self.api_key_env)
        if not self.api_key:
            raise RuntimeError(f"Missing {self.api_key_env}")

    @staticmethod
    def _record_usage(payload: dict[str, Any]) -> None:
        usage = payload.get("usage") or {}
        USAGE_TRACKER.llm_calls += 1
        USAGE_TRACKER.tokens_in += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        USAGE_TRACKER.tokens_out += int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        USAGE_TRACKER.cache_creation_input_tokens += int(
            usage.get("cache_creation_input_tokens") or 0
        )
        USAGE_TRACKER.cache_read_input_tokens += int(
            usage.get("cache_read_input_tokens") or 0
        )

    def guardrail_endpoint(self) -> Callable[[str], str]:
        def end_point(input: str, **_: Any) -> str:
            # This message envelope is byte-for-byte equivalent in meaning to the
            # original FinMem ChatOpenAICompatible guardrail endpoint.
            messages = [
                {
                    "role": "system",
                    "content": "You are a helpful assistant only capable of communicating with valid JSON, and no other text.",
                },
                {"role": "user", "content": input},
            ]
            parameters = {
                key: value
                for key, value in self.other_parameters.items()
                if key not in {"tokenization_model_name"}
            }
            payload = {"model": self.model, "messages": messages, **parameters}
            response = httpx.post(
                self.end_point,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=600.0,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if response.status_code == 422 and "must have less than" in response.text:
                    raise LongerThanContextError from exc
                raise

            response_payload = response.json()
            self._record_usage(response_payload)
            choices = response_payload.get("choices") or []
            if not choices:
                raise RuntimeError("M2.1 provider returned no choices")
            content = (choices[0].get("message") or {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("M2.1 provider returned empty message content")
            return content

        return end_point
