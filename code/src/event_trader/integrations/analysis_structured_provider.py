"""Provider adapter for optional Analysis structured finalization."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from event_trader.config import AnalysisAgentConfig, KernelConfig
from event_trader.integrations.analysis_structured_finalizer import (
    AnalysisStructuredCapabilityResult,
    AnalysisStructuredProviderClient,
    ProviderNativeMode,
)

_FINAL_PAYLOAD_TOOL_NAME = "analysis_final_payload"
_PROBE_PROMPT = (
    "Return this JSON payload using the required structured response mechanism: "
    '{"target_key":"probe","event_ids":[],"used_lesson_ids":[],"analysis_assessment":null}'
)


class AnalysisStructuredProviderError(RuntimeError):
    """Raised when the structured provider adapter cannot be constructed or parsed."""


@dataclass(frozen=True, slots=True)
class AnalysisStructuredProviderConfig:
    provider: str
    model: str
    api_key: str
    base_url: str

    @property
    def provider_key(self) -> str:
        return self.provider.strip().lower()

    @property
    def base_url_host(self) -> str:
        parsed = urlparse(self.base_url)
        return parsed.netloc or parsed.path


OpenAIClientFactory = Callable[[AnalysisStructuredProviderConfig], object]
AnthropicClientFactory = Callable[[AnalysisStructuredProviderConfig], object]


class AnalysisStructuredProviderAdapter(AnalysisStructuredProviderClient):
    def __init__(
        self,
        config: AnalysisStructuredProviderConfig,
        *,
        openai_client_factory: OpenAIClientFactory | None = None,
        anthropic_client_factory: AnthropicClientFactory | None = None,
    ) -> None:
        self._config = config
        self._openai_client_factory = openai_client_factory or _default_openai_client
        self._anthropic_client_factory = (
            anthropic_client_factory or _default_anthropic_client
        )
        self._openai_client: object | None = None
        self._anthropic_client: object | None = None

    def probe_native_structured_output(
        self,
        *,
        provider: str,
        response_format: Mapping[str, object],
    ) -> AnalysisStructuredCapabilityResult:
        provider_key = provider.strip().lower() or self._config.provider_key
        if provider_key == "openai":
            return self._probe_openai(response_format)
        if provider_key == "anthropic":
            return self._probe_anthropic(response_format)
        return self._capability(
            native_available=False,
            native_mode="unavailable",
            reason=f"unsupported structured finalizer provider: {provider_key}",
        )

    def finalize_native_analysis_payload(
        self,
        *,
        prompt: str,
        response_format: Mapping[str, object],
    ) -> Mapping[str, object]:
        provider_key = self._config.provider_key
        if provider_key == "openai":
            content = self._call_openai(prompt=prompt, response_format=response_format)
            return _loads_json_object(content)
        if provider_key == "anthropic":
            return self._call_anthropic_tool(
                prompt=prompt,
                response_format=response_format,
            )
        raise AnalysisStructuredProviderError(
            f"unsupported structured finalizer provider: {provider_key}"
        )

    def _probe_openai(
        self,
        response_format: Mapping[str, object],
    ) -> AnalysisStructuredCapabilityResult:
        try:
            payload = _loads_json_object(
                self._call_openai(
                    prompt=_PROBE_PROMPT,
                    response_format=response_format,
                    max_tokens=256,
                )
            )
            _assert_probe_payload(payload)
        except Exception as exc:
            return self._capability(
                native_available=False,
                native_mode="unavailable",
                reason=f"OpenAI-compatible strict json_schema probe failed: {exc}",
            )
        return self._capability(
            native_available=True,
            native_mode="openai_json_schema",
            reason="OpenAI-compatible strict json_schema probe returned valid payload.",
        )

    def _probe_anthropic(
        self,
        response_format: Mapping[str, object],
    ) -> AnalysisStructuredCapabilityResult:
        try:
            payload = self._call_anthropic_tool(
                prompt=_PROBE_PROMPT,
                response_format=response_format,
                max_tokens=256,
            )
            _assert_probe_payload(payload)
        except Exception as exc:
            return self._capability(
                native_available=False,
                native_mode="unavailable",
                reason=f"Anthropic-compatible forced tool probe failed: {exc}",
            )
        return self._capability(
            native_available=True,
            native_mode="anthropic_forced_tool",
            reason="Anthropic-compatible forced tool probe returned valid payload.",
        )

    def _call_openai(
        self,
        *,
        prompt: str,
        response_format: Mapping[str, object],
        max_tokens: int = 4096,
    ) -> str:
        client = self._get_openai_client()
        chat = getattr(client, "chat")
        completions = getattr(chat, "completions")
        create = getattr(completions, "create")
        params: dict[str, object] = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": dict(response_format),
            "temperature": 0,
        }
        if "gpt-5" in self._config.model:
            params["max_completion_tokens"] = max_tokens
        else:
            params["max_tokens"] = max_tokens
        response = create(**params)
        content = _get_path(response, ("choices", 0, "message", "content"))
        if not isinstance(content, str) or not content.strip():
            raise AnalysisStructuredProviderError(
                "OpenAI-compatible response did not contain message.content."
            )
        return content

    def _call_anthropic_tool(
        self,
        *,
        prompt: str,
        response_format: Mapping[str, object],
        max_tokens: int = 4096,
    ) -> Mapping[str, object]:
        client = self._get_anthropic_client()
        messages = getattr(client, "messages")
        create = getattr(messages, "create")
        tool_schema = _schema_from_response_format(response_format)
        response = create(
            model=self._config.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            tools=[
                {
                    "name": _FINAL_PAYLOAD_TOOL_NAME,
                    "description": "Return the Analysis final payload.",
                    "input_schema": tool_schema,
                }
            ],
            tool_choice={"type": "tool", "name": _FINAL_PAYLOAD_TOOL_NAME},
        )
        return _extract_anthropic_tool_input(response)

    def _get_openai_client(self) -> object:
        if self._openai_client is None:
            self._openai_client = self._openai_client_factory(self._config)
        return self._openai_client

    def _get_anthropic_client(self) -> object:
        if self._anthropic_client is None:
            self._anthropic_client = self._anthropic_client_factory(self._config)
        return self._anthropic_client

    def _capability(
        self,
        *,
        native_available: bool,
        native_mode: ProviderNativeMode,
        reason: str,
    ) -> AnalysisStructuredCapabilityResult:
        return AnalysisStructuredCapabilityResult(
            provider=self._config.provider_key,
            native_available=native_available,
            native_mode=native_mode,
            reason=reason,
            base_url_host=self._config.base_url_host,
            model=self._config.model,
        )


def build_analysis_structured_provider_adapter(
    analysis_agent: AnalysisAgentConfig,
) -> AnalysisStructuredProviderAdapter:
    return AnalysisStructuredProviderAdapter(
        AnalysisStructuredProviderConfig(
            provider=analysis_agent.llm_provider,
            model=analysis_agent.llm_model_name,
            api_key=analysis_agent.llm_api_key,
            base_url=analysis_agent.llm_base_url,
        )
    )


def run_analysis_structured_finalizer_preflight(
    config: KernelConfig,
) -> AnalysisStructuredCapabilityResult:
    if config.analysis_agent is None:
        raise AnalysisStructuredProviderError(
            "structured finalizer preflight requires [analysis_agent] config."
        )
    adapter = build_analysis_structured_provider_adapter(config.analysis_agent)
    from event_trader.integrations.analysis_structured_finalizer import (
        analysis_final_payload_response_format,
    )

    return adapter.probe_native_structured_output(
        provider=config.analysis_agent.llm_provider,
        response_format=analysis_final_payload_response_format(),
    )


def capability_result_to_jsonable(
    result: AnalysisStructuredCapabilityResult,
) -> dict[str, object]:
    return {
        "provider": result.provider,
        "base_url_host": result.base_url_host,
        "model": result.model,
        "native_mode": result.native_mode,
        "native_available": result.native_available,
        "reason": result.reason,
    }


def _default_openai_client(config: AnalysisStructuredProviderConfig) -> object:
    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - environment dependent
        raise AnalysisStructuredProviderError(
            f"OpenAI SDK is not available: {exc}"
        ) from exc
    return OpenAI(api_key=config.api_key, base_url=config.base_url)


def _default_anthropic_client(config: AnalysisStructuredProviderConfig) -> object:
    try:
        from anthropic import Anthropic
    except Exception as exc:  # pragma: no cover - environment dependent
        raise AnalysisStructuredProviderError(
            f"Anthropic SDK is not available: {exc}"
        ) from exc
    return Anthropic(api_key=config.api_key, base_url=config.base_url)


def _schema_from_response_format(response_format: Mapping[str, object]) -> Mapping[str, object]:
    json_schema = response_format.get("json_schema")
    if not isinstance(json_schema, Mapping):
        raise AnalysisStructuredProviderError("response_format missing json_schema.")
    schema = json_schema.get("schema")
    if not isinstance(schema, Mapping):
        raise AnalysisStructuredProviderError("response_format missing json_schema.schema.")
    return schema


def _extract_anthropic_tool_input(response: object) -> Mapping[str, object]:
    content = _get_value(response, "content")
    if not isinstance(content, list):
        raise AnalysisStructuredProviderError("Anthropic response missing content blocks.")
    for block in content:
        block_type = _get_value(block, "type")
        block_name = _get_value(block, "name")
        if block_type == "tool_use" and block_name == _FINAL_PAYLOAD_TOOL_NAME:
            tool_input = _get_value(block, "input")
            if isinstance(tool_input, Mapping):
                return tool_input
            raise AnalysisStructuredProviderError(
                "Anthropic forced tool response input was not an object."
            )
    raise AnalysisStructuredProviderError(
        "Anthropic response did not force the analysis_final_payload tool."
    )


def _loads_json_object(content: str) -> Mapping[str, object]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AnalysisStructuredProviderError(
            f"structured provider returned non-JSON content: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise AnalysisStructuredProviderError(
            "structured provider returned JSON that was not an object."
        )
    return payload


def _assert_probe_payload(payload: Mapping[str, object]) -> None:
    expected = {
        "target_key": "probe",
        "event_ids": [],
        "used_lesson_ids": [],
        "analysis_assessment": None,
    }
    if dict(payload) != expected:
        raise AnalysisStructuredProviderError(
            "probe returned a payload that did not match the requested schema-shaped object."
        )


def _get_path(value: object, path: tuple[object, ...]) -> object:
    current = value
    for part in path:
        if isinstance(part, int):
            if not isinstance(current, list) or part >= len(current):
                return None
            current = current[part]
            continue
        current = _get_value(current, str(part))
    return current


def _get_value(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


__all__ = [
    "AnalysisStructuredProviderAdapter",
    "AnalysisStructuredProviderConfig",
    "AnalysisStructuredProviderError",
    "build_analysis_structured_provider_adapter",
    "capability_result_to_jsonable",
    "run_analysis_structured_finalizer_preflight",
]
