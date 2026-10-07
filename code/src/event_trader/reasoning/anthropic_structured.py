"""Narrow Anthropic-compatible transport for forced structured tool responses."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from event_trader.reasoning.structured_inference import (
    StructuredInferenceError,
    StructuredInferenceResult,
)


class AnthropicStructuredInferenceError(StructuredInferenceError):
    """Raised when a forced structured response cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        terminal_metadata: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.terminal_metadata = (
            {} if terminal_metadata is None else dict(terminal_metadata)
        )


_MAX_SCHEMA_REPAIR_LEDGER_ITEMS = 12
_NO_SINGLE_VALUE_DISCRIMINATOR = object()


@dataclass(slots=True)
class _SchemaRepairViolation:
    key: str
    violation: dict[str, object]
    attempts: list[int]

    def prompt_payload(self) -> dict[str, object]:
        return {
            "key": self.key,
            "violation": dict(self.violation),
            "attempts": list(self.attempts),
        }


@dataclass(slots=True)
class _SchemaRepairLedger:
    """Bounded local Schema failure history; rejected payloads never enter it."""

    _entries_by_key: dict[str, _SchemaRepairViolation]

    @classmethod
    def empty(cls) -> _SchemaRepairLedger:
        return cls(_entries_by_key={})

    def record(
        self,
        *,
        protocol_attempt: int,
        schema_errors: tuple[ValidationError, ...],
    ) -> None:
        for violation in _schema_error_summaries(schema_errors):
            if "omitted_error_count" in violation:
                continue
            key = sha256(
                json.dumps(
                    violation,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            entry = self._entries_by_key.get(key)
            if entry is None:
                if len(self._entries_by_key) >= _MAX_SCHEMA_REPAIR_LEDGER_ITEMS:
                    continue
                entry = _SchemaRepairViolation(
                    key=key,
                    violation=violation,
                    attempts=[],
                )
                self._entries_by_key[key] = entry
            entry.attempts.append(protocol_attempt)

    def snapshot(self) -> list[dict[str, object]]:
        return [entry.prompt_payload() for entry in self._entries_by_key.values()]


@dataclass(frozen=True, slots=True)
class AnthropicStructuredInferenceConfig:
    """Provider transport settings selected by the owning business workflow."""

    model_name: str
    api_key: str
    base_url: str
    max_output_tokens: int
    temperature: float
    transport_max_attempts: int
    transport_retry_seconds: float
    protocol_max_attempts: int
    enable_tool_schema_cache: bool = False

    def __post_init__(self) -> None:
        for field_name in ("model_name", "api_key", "base_url"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise AnthropicStructuredInferenceError(f"{field_name} must be non-blank.")
        for field_name in (
            "max_output_tokens",
            "transport_max_attempts",
            "protocol_max_attempts",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise AnthropicStructuredInferenceError(f"{field_name} must be a positive integer.")
        if (
            not isinstance(self.temperature, int | float)
            or isinstance(self.temperature, bool)
            or not isfinite(self.temperature)
        ):
            raise AnthropicStructuredInferenceError("temperature must be a finite number.")
        if (
            not isinstance(self.transport_retry_seconds, int | float)
            or isinstance(self.transport_retry_seconds, bool)
            or not isfinite(self.transport_retry_seconds)
            or self.transport_retry_seconds < 0
        ):
            raise AnthropicStructuredInferenceError(
                "transport_retry_seconds must be a non-negative finite number."
            )
        if not isinstance(self.enable_tool_schema_cache, bool):
            raise AnthropicStructuredInferenceError(
                "enable_tool_schema_cache must be a boolean."
            )


class AnthropicStructuredInference:
    """Run structured calls with an attempt-scoped client when requested.

    A caller can wrap one workflow attempt in :meth:`session` so every stage
    reuses one connection pool.  Clients created by the session are always
    closed on normal, exceptional, timeout, and cancellation exits.  Injected
    clients remain caller-owned and are never closed here.
    """

    def __init__(
        self,
        config: AnthropicStructuredInferenceConfig,
        *,
        client: object | None = None,
    ) -> None:
        self._config = config
        self._client = client

    @asynccontextmanager
    async def session(self) -> Any:
        """Yield an independent provider bound to one attempt client."""

        if self._client is not None:
            yield AnthropicStructuredInference(self._config, client=self._client)
            return
        owned_client = _build_anthropic_client(self._config)
        try:
            yield AnthropicStructuredInference(self._config, client=owned_client)
        finally:
            await _close_anthropic_client(owned_client)

    async def generate_structured(
        self,
        *,
        prompt: str,
        session_id: str,
        tool_name: str,
        tool_description: str,
        input_schema: Mapping[str, object],
    ) -> StructuredInferenceResult:
        client = self._client
        if client is None:
            async with self.session() as attempt_provider:
                return await attempt_provider.generate_structured(
                    prompt=prompt,
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_description=tool_description,
                    input_schema=input_schema,
                )
        return await self._generate_structured_with_client(
            client=client,
            prompt=prompt,
            session_id=session_id,
            tool_name=tool_name,
            tool_description=tool_description,
            input_schema=input_schema,
        )

    async def _generate_structured_with_client(
        self,
        *,
        client: object,
        prompt: str,
        session_id: str,
        tool_name: str,
        tool_description: str,
        input_schema: Mapping[str, object],
    ) -> StructuredInferenceResult:
        anthropic_client = cast(Any, client)
        rejected_response_summaries: list[dict[str, object]] = []
        schema_repair_ledger = _SchemaRepairLedger.empty()
        total_transport_attempt_count = 0
        messages: list[dict[str, object]] = [{"role": "user", "content": prompt}]
        schema = dict(input_schema)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise AnthropicStructuredInferenceError(
                f"Project-owned input schema for {tool_name} is invalid: {exc.message}",
                terminal_metadata=_terminal_metadata(
                    protocol_attempt_count=0,
                    transport_attempt_count=0,
                    total_transport_attempt_count=0,
                    rejected_response_summaries=(),
                    schema_repair_ledger=(),
                    cumulative_input_tokens=0,
                    cumulative_output_tokens=0,
                    response_count=0,
                    usage_complete=True,
                ),
            ) from exc
        schema_validator = Draft202012Validator(schema)
        cumulative_input_tokens = 0
        cumulative_output_tokens = 0
        usage_complete = True
        cache_usage_complete = True
        cumulative_cache_creation_input_tokens = 0
        cumulative_cache_read_input_tokens = 0
        response_count = 0
        for protocol_attempt_count in range(1, self._config.protocol_max_attempts + 1):
            response: object | None = None
            transport_attempt_count = 0
            for transport_attempt_count in range(
                1,
                self._config.transport_max_attempts + 1,
            ):
                total_transport_attempt_count += 1
                try:
                    response = await anthropic_client.messages.create(
                        model=self._config.model_name,
                        max_tokens=self._config.max_output_tokens,
                        temperature=self._config.temperature,
                        messages=messages,
                        tools=[
                            _tool_definition(
                                name=tool_name,
                                description=tool_description,
                                input_schema=schema,
                                enable_cache=self._config.enable_tool_schema_cache,
                            )
                        ],
                        tool_choice={"type": "tool", "name": tool_name},
                        extra_headers={"x-upstream-session-id": session_id},
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if transport_attempt_count >= self._config.transport_max_attempts:
                        message = str(exc).strip() or repr(exc)
                        failure_context = _serialized_failure_context(
                            rejected_response_summaries=rejected_response_summaries,
                            schema_repair_ledger=schema_repair_ledger.snapshot(),
                            cumulative_input_tokens=cumulative_input_tokens,
                            cumulative_output_tokens=cumulative_output_tokens,
                            response_count=response_count,
                            usage_complete=usage_complete,
                            cumulative_cache_creation_input_tokens=(
                                cumulative_cache_creation_input_tokens
                            ),
                            cumulative_cache_read_input_tokens=(
                                cumulative_cache_read_input_tokens
                            ),
                            cache_usage_complete=cache_usage_complete,
                        )
                        raise AnthropicStructuredInferenceError(
                            "Anthropic-compatible transport failed after "
                            f"{transport_attempt_count} attempts during protocol "
                            f"attempt {protocol_attempt_count} for {tool_name}: "
                            f"{type(exc).__name__}: {message}; {failure_context}",
                            terminal_metadata=_terminal_metadata(
                                protocol_attempt_count=protocol_attempt_count,
                                transport_attempt_count=transport_attempt_count,
                                total_transport_attempt_count=total_transport_attempt_count,
                                rejected_response_summaries=rejected_response_summaries,
                                schema_repair_ledger=schema_repair_ledger.snapshot(),
                                cumulative_input_tokens=cumulative_input_tokens,
                                cumulative_output_tokens=cumulative_output_tokens,
                                response_count=response_count,
                                usage_complete=False,
                                cumulative_cache_creation_input_tokens=(
                                    cumulative_cache_creation_input_tokens
                                ),
                                cumulative_cache_read_input_tokens=(
                                    cumulative_cache_read_input_tokens
                                ),
                                cache_usage_complete=False,
                            ),
                        ) from exc
                    await asyncio.sleep(self._config.transport_retry_seconds)
            if response is None:
                raise AnthropicStructuredInferenceError(
                    "Anthropic-compatible transport ended without a response.",
                    terminal_metadata=_terminal_metadata(
                        protocol_attempt_count=protocol_attempt_count,
                        transport_attempt_count=transport_attempt_count,
                        total_transport_attempt_count=total_transport_attempt_count,
                        rejected_response_summaries=rejected_response_summaries,
                        schema_repair_ledger=schema_repair_ledger.snapshot(),
                        cumulative_input_tokens=cumulative_input_tokens,
                        cumulative_output_tokens=cumulative_output_tokens,
                        response_count=response_count,
                        usage_complete=False,
                        cumulative_cache_creation_input_tokens=(
                            cumulative_cache_creation_input_tokens
                        ),
                        cumulative_cache_read_input_tokens=(
                            cumulative_cache_read_input_tokens
                        ),
                        cache_usage_complete=False,
                    ),
                )
            response_count += 1
            usage_counts = _usage_token_counts(_get_value(response, "usage"))
            if usage_counts is None:
                usage_complete = False
                if self._config.enable_tool_schema_cache:
                    cache_usage_complete = False
            else:
                cumulative_input_tokens += usage_counts[0]
                cumulative_output_tokens += usage_counts[1]
                cumulative_cache_creation_input_tokens += usage_counts[2]
                cumulative_cache_read_input_tokens += usage_counts[3]
                if self._config.enable_tool_schema_cache and not usage_counts[4]:
                    cache_usage_complete = False
            try:
                effective_payload = dict(
                    _extract_anthropic_tool_input(response, tool_name=tool_name)
                )
            except AnthropicStructuredInferenceError as exc:
                output_capacity = _get_value(response, "stop_reason") == "max_tokens"
                response_summary = {
                    **_anthropic_rejected_response_summary(response),
                    "failure_kind": (
                        "output_capacity" if output_capacity else "structured_protocol"
                    ),
                    "message": _bounded_error_message(str(exc)),
                }
                rejected_response_summaries.append(response_summary)
                if protocol_attempt_count >= self._config.protocol_max_attempts:
                    raise _structured_exhaustion_error(
                        tool_name=tool_name,
                        protocol_attempt_count=protocol_attempt_count,
                        transport_attempt_count=transport_attempt_count,
                        total_transport_attempt_count=total_transport_attempt_count,
                        rejected_response_summaries=rejected_response_summaries,
                        schema_repair_ledger=schema_repair_ledger.snapshot(),
                        cumulative_input_tokens=cumulative_input_tokens,
                        cumulative_output_tokens=cumulative_output_tokens,
                        response_count=response_count,
                        usage_complete=usage_complete,
                        cumulative_cache_creation_input_tokens=(
                            cumulative_cache_creation_input_tokens
                        ),
                        cumulative_cache_read_input_tokens=(
                            cumulative_cache_read_input_tokens
                        ),
                        cache_usage_complete=cache_usage_complete,
                    ) from exc
                messages.extend(
                    _protocol_repair_messages(
                        response=response,
                        tool_name=tool_name,
                        protocol_error=str(exc),
                        response_summary=_anthropic_response_summary(response),
                        schema_failure_ledger=schema_repair_ledger.snapshot(),
                        output_capacity=output_capacity,
                    )
                )
                continue
            schema_errors = _schema_validation_errors(schema_validator, effective_payload)
            if schema_errors:
                output_capacity = _get_value(response, "stop_reason") == "max_tokens"
                schema_repair_ledger.record(
                    protocol_attempt=protocol_attempt_count,
                    schema_errors=schema_errors,
                )
                response_summary = {
                    **_anthropic_rejected_response_summary(response),
                    "failure_kind": (
                        "output_capacity"
                        if output_capacity
                        else "local_schema_validation"
                    ),
                    "schema_errors": _schema_error_summaries(schema_errors),
                }
                rejected_response_summaries.append(response_summary)
                if protocol_attempt_count >= self._config.protocol_max_attempts:
                    raise _structured_exhaustion_error(
                        tool_name=tool_name,
                        protocol_attempt_count=protocol_attempt_count,
                        transport_attempt_count=transport_attempt_count,
                        total_transport_attempt_count=total_transport_attempt_count,
                        rejected_response_summaries=rejected_response_summaries,
                        schema_repair_ledger=schema_repair_ledger.snapshot(),
                        cumulative_input_tokens=cumulative_input_tokens,
                        cumulative_output_tokens=cumulative_output_tokens,
                        response_count=response_count,
                        usage_complete=usage_complete,
                        cumulative_cache_creation_input_tokens=(
                            cumulative_cache_creation_input_tokens
                        ),
                        cumulative_cache_read_input_tokens=(
                            cumulative_cache_read_input_tokens
                        ),
                        cache_usage_complete=cache_usage_complete,
                    )
                messages.extend(
                    _schema_repair_messages(
                        response=response,
                        tool_name=tool_name,
                        schema_errors=_schema_error_summaries(schema_errors),
                        output_capacity=output_capacity,
                    )
                )
                continue
            metadata = _anthropic_response_metadata(response)
            metadata["transport_attempt_count"] = transport_attempt_count
            metadata["total_transport_attempt_count"] = total_transport_attempt_count
            metadata["protocol_attempt_count"] = protocol_attempt_count
            metadata["rejected_response_summaries"] = rejected_response_summaries
            metadata["schema_repair_ledger"] = schema_repair_ledger.snapshot()
            metadata["prompt_cache"] = {
                "enabled": self._config.enable_tool_schema_cache,
                "scope": "tool_schema",
                "status": "requested" if self._config.enable_tool_schema_cache else "disabled",
            }
            metadata["usage_totals"] = _usage_totals(
                cumulative_input_tokens=cumulative_input_tokens,
                cumulative_output_tokens=cumulative_output_tokens,
                cumulative_cache_creation_input_tokens=(
                    cumulative_cache_creation_input_tokens
                ),
                cumulative_cache_read_input_tokens=cumulative_cache_read_input_tokens,
                response_count=response_count,
                usage_complete=usage_complete,
                cache_usage_complete=cache_usage_complete,
            )
            return StructuredInferenceResult(payload=effective_payload, metadata=metadata)
        raise AssertionError("structured protocol retry loop ended without a result")


def _protocol_repair_messages(
    *,
    response: object,
    tool_name: str,
    protocol_error: str,
    response_summary: Mapping[str, object],
    schema_failure_ledger: list[dict[str, object]],
    output_capacity: bool,
) -> list[dict[str, object]]:
    observed = json.dumps(
        dict(response_summary),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    failure_kind = "output-capacity limit" if output_capacity else "structured-response protocol"
    correction = (
        f"Previous response failed the required {failure_kind}:\n"
        f"- Required tool: {tool_name}\n"
        f"- Rejected response summary: {observed}\n"
        "Correction:\n"
        f"- Call exactly one tool named `{tool_name}`.\n"
        "- Do not call any other tool directly.\n"
        "- Do not answer with a text block.\n"
        "- Put the complete response in that tool call's input and follow its "
        "registered schema exactly.\n"
        + ("- Be concise and do not return a partial payload.\n" if output_capacity else "")
    )
    if schema_failure_ledger:
        serialized_ledger = json.dumps(
            schema_failure_ledger,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        correction += (
            "Cumulative project-local JSON Schema validation ledger:\n"
            f"- Schema violations: {serialized_ledger}\n"
        )
    tool_use_id = _anthropic_single_tool_use_id(response)
    feedback: dict[str, object]
    if tool_use_id is None:
        feedback = {"role": "user", "content": correction}
    else:
        feedback = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": _bounded_error_message(protocol_error),
                }
            ],
        }
    return [_anthropic_assistant_history_message(response), feedback]


def _schema_repair_messages(
    *,
    response: object,
    tool_name: str,
    schema_errors: list[dict[str, object]],
    output_capacity: bool,
) -> list[dict[str, object]]:
    feedback: dict[str, object] = {"schema_errors": schema_errors}
    if output_capacity:
        feedback.update(
            {
                "output_capacity": True,
                "correction": (
                    "Regenerate one complete concise tool input; do not return a partial payload."
                ),
            }
        )
    return [
        _anthropic_assistant_history_message(response),
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": _anthropic_expected_tool_use_id(
                        response,
                        tool_name=tool_name,
                    ),
                    "is_error": True,
                    "content": json.dumps(
                        feedback,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            ],
        },
    ]


def _anthropic_assistant_history_message(response: object) -> dict[str, object]:
    content = _get_value(response, "content")
    if not isinstance(content, list):
        raise AnthropicStructuredInferenceError(
            "Anthropic-compatible response missing content blocks for repair history."
        )
    return {"role": "assistant", "content": content}


def _anthropic_expected_tool_use_id(response: object, *, tool_name: str) -> str:
    tool_blocks = _anthropic_tool_use_blocks(response)
    if len(tool_blocks) != 1 or _get_value(tool_blocks[0], "name") != tool_name:
        raise AnthropicStructuredInferenceError(
            "Anthropic-compatible response cannot attach a schema repair to an invalid tool call."
        )
    return _anthropic_tool_use_id(tool_blocks[0])


def _anthropic_single_tool_use_id(response: object) -> str | None:
    tool_blocks = _anthropic_tool_use_blocks(response)
    if len(tool_blocks) != 1:
        return None
    try:
        return _anthropic_tool_use_id(tool_blocks[0])
    except AnthropicStructuredInferenceError:
        return None


def _anthropic_tool_use_blocks(response: object) -> list[object]:
    content = _get_value(response, "content")
    if not isinstance(content, list):
        return []
    return [block for block in content if _get_value(block, "type") == "tool_use"]


def _anthropic_tool_use_id(tool_block: object) -> str:
    tool_use_id = _get_value(tool_block, "id")
    if not isinstance(tool_use_id, str) or not tool_use_id.strip():
        raise AnthropicStructuredInferenceError(
            "Anthropic-compatible tool use is missing its id for schema repair."
        )
    return tool_use_id


def _structured_exhaustion_error(
    *,
    tool_name: str,
    protocol_attempt_count: int,
    transport_attempt_count: int,
    total_transport_attempt_count: int,
    rejected_response_summaries: list[dict[str, object]],
    schema_repair_ledger: list[dict[str, object]],
    cumulative_input_tokens: int,
    cumulative_output_tokens: int,
    response_count: int,
    usage_complete: bool,
    cumulative_cache_creation_input_tokens: int = 0,
    cumulative_cache_read_input_tokens: int = 0,
    cache_usage_complete: bool = True,
) -> AnthropicStructuredInferenceError:
    return AnthropicStructuredInferenceError(
        "Anthropic-compatible structured response failed after "
        f"{protocol_attempt_count} protocol attempts for {tool_name}; "
        + _serialized_failure_context(
            rejected_response_summaries=rejected_response_summaries,
            schema_repair_ledger=schema_repair_ledger,
            cumulative_input_tokens=cumulative_input_tokens,
            cumulative_output_tokens=cumulative_output_tokens,
            response_count=response_count,
            usage_complete=usage_complete,
            cumulative_cache_creation_input_tokens=(
                cumulative_cache_creation_input_tokens
            ),
            cumulative_cache_read_input_tokens=cumulative_cache_read_input_tokens,
            cache_usage_complete=cache_usage_complete,
        ),
        terminal_metadata=_terminal_metadata(
            protocol_attempt_count=protocol_attempt_count,
            transport_attempt_count=transport_attempt_count,
            total_transport_attempt_count=total_transport_attempt_count,
            rejected_response_summaries=rejected_response_summaries,
            schema_repair_ledger=schema_repair_ledger,
            cumulative_input_tokens=cumulative_input_tokens,
            cumulative_output_tokens=cumulative_output_tokens,
            response_count=response_count,
            usage_complete=usage_complete,
            cumulative_cache_creation_input_tokens=(
                cumulative_cache_creation_input_tokens
            ),
            cumulative_cache_read_input_tokens=cumulative_cache_read_input_tokens,
            cache_usage_complete=cache_usage_complete,
        ),
    )


def _terminal_metadata(
    *,
    protocol_attempt_count: int,
    transport_attempt_count: int,
    total_transport_attempt_count: int,
    rejected_response_summaries: Iterable[Mapping[str, object]],
    schema_repair_ledger: Iterable[Mapping[str, object]],
    cumulative_input_tokens: int,
    cumulative_output_tokens: int,
    response_count: int,
    usage_complete: bool,
    cumulative_cache_creation_input_tokens: int = 0,
    cumulative_cache_read_input_tokens: int = 0,
    cache_usage_complete: bool = True,
) -> dict[str, object]:
    """Expose bounded accounting for a terminal call without response payloads."""

    rejected = tuple(rejected_response_summaries)
    failure_kind_counts: dict[str, int] = {}
    for summary in rejected:
        failure_kind = summary.get("failure_kind")
        if isinstance(failure_kind, str):
            failure_kind_counts[failure_kind] = failure_kind_counts.get(failure_kind, 0) + 1
    return {
        "protocol_attempt_count": protocol_attempt_count,
        "transport_attempt_count": transport_attempt_count,
        "total_transport_attempt_count": total_transport_attempt_count,
        "rejected_response_count": len(rejected),
        "failure_kind_counts": failure_kind_counts,
        "schema_repair_violation_count": sum(1 for _ in schema_repair_ledger),
        "usage_totals": _usage_totals(
            cumulative_input_tokens=cumulative_input_tokens,
            cumulative_output_tokens=cumulative_output_tokens,
            response_count=response_count,
            usage_complete=usage_complete,
            cumulative_cache_creation_input_tokens=(
                cumulative_cache_creation_input_tokens
            ),
            cumulative_cache_read_input_tokens=cumulative_cache_read_input_tokens,
            cache_usage_complete=cache_usage_complete,
        ),
    }


def _serialized_failure_context(
    *,
    rejected_response_summaries: list[dict[str, object]],
    schema_repair_ledger: list[dict[str, object]],
    cumulative_input_tokens: int,
    cumulative_output_tokens: int,
    response_count: int,
    usage_complete: bool,
    cumulative_cache_creation_input_tokens: int = 0,
    cumulative_cache_read_input_tokens: int = 0,
    cache_usage_complete: bool = True,
) -> str:
    summaries = json.dumps(
        rejected_response_summaries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    usage_totals = json.dumps(
        _usage_totals(
            cumulative_input_tokens=cumulative_input_tokens,
            cumulative_output_tokens=cumulative_output_tokens,
            response_count=response_count,
            usage_complete=usage_complete,
            cumulative_cache_creation_input_tokens=(
                cumulative_cache_creation_input_tokens
            ),
            cumulative_cache_read_input_tokens=cumulative_cache_read_input_tokens,
            cache_usage_complete=cache_usage_complete,
        ),
        sort_keys=True,
        separators=(",", ":"),
    )
    repair_ledger = json.dumps(
        schema_repair_ledger,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"rejected_response_summaries={summaries}; "
        f"schema_repair_ledger={repair_ledger}; usage_totals={usage_totals}"
    )


def _usage_totals(
    *,
    cumulative_input_tokens: int,
    cumulative_output_tokens: int,
    response_count: int,
    usage_complete: bool,
    cumulative_cache_creation_input_tokens: int = 0,
    cumulative_cache_read_input_tokens: int = 0,
    cache_usage_complete: bool = True,
) -> dict[str, object]:
    # MiniMax documents these as mutually exclusive input categories:
    # input_tokens is the uncached suffix, while cache read/write tokens are
    # the cached prefix.  Keep the raw provider field and expose the observed
    # complete input sum separately; never count the cache categories twice.
    observed_input_tokens = (
        cumulative_input_tokens
        + cumulative_cache_creation_input_tokens
        + cumulative_cache_read_input_tokens
    )
    return {
        "input_tokens": cumulative_input_tokens,
        "output_tokens": cumulative_output_tokens,
        "total_tokens": observed_input_tokens + cumulative_output_tokens,
        "response_count": response_count,
        "cache_creation_input_tokens": cumulative_cache_creation_input_tokens,
        "cache_read_input_tokens": cumulative_cache_read_input_tokens,
        "observed_input_tokens": observed_input_tokens,
        "observed_total_tokens": observed_input_tokens + cumulative_output_tokens,
        "cache_complete": cache_usage_complete,
        # Core provider usage remains usable when an upstream compatibility
        # layer omits optional cache breakdown fields.  The cache breakdown
        # has its own explicit completeness bit and is never inferred.
        "complete": usage_complete,
    }


def _schema_validation_errors(
    validator: Draft202012Validator,
    payload: Mapping[str, object],
) -> tuple[ValidationError, ...]:
    return tuple(
        sorted(
            validator.iter_errors(payload),
            key=lambda error: (
                _json_path(error.absolute_path),
                _json_path(error.absolute_schema_path),
                error.message,
            ),
        )
    )


def _schema_error_summaries(
    errors: tuple[ValidationError, ...],
    *,
    max_items: int = 8,
) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    omitted_error_count = 0
    for root_error in errors:
        for error in _schema_error_leaves(root_error):
            summary_key = (
                _json_path(error.absolute_path),
                _json_path(error.absolute_schema_path),
                str(error.validator),
                _safe_schema_error_message(error),
            )
            if summary_key in seen:
                continue
            seen.add(summary_key)
            if len(summaries) < max_items:
                summaries.append(
                    {
                        "instance_path": summary_key[0],
                        "schema_path": summary_key[1],
                        "validator": summary_key[2],
                        "message": summary_key[3],
                    }
                )
            else:
                omitted_error_count += 1
    if omitted_error_count:
        summaries.append({"omitted_error_count": omitted_error_count})
    return summaries


def _bounded_error_message(message: str, *, max_chars: int = 512) -> str:
    if len(message) <= max_chars:
        return message
    return f"{message[:max_chars]}... [truncated]"


def _safe_schema_error_message(error: ValidationError) -> str:
    """Describe the project schema failure without serializing rejected input values."""

    validator = str(error.validator)
    if validator == "type":
        expected = error.validator_value
        if isinstance(expected, str | list):
            expected_text = json.dumps(expected, ensure_ascii=False, sort_keys=True)
            return f"Expected JSON type: {expected_text}."
        return "Value has a JSON type disallowed by the project schema."
    if validator == "required":
        required = error.validator_value
        if isinstance(required, list) and isinstance(error.instance, Mapping):
            missing = next(
                (
                    field_name
                    for field_name in required
                    if isinstance(field_name, str) and field_name not in error.instance
                ),
                None,
            )
            if missing is not None:
                return f"Required property {missing!r} is missing."
        return "A project-schema required property is missing."
    if validator == "additionalProperties":
        names = _additional_property_names(error)
        if names:
            rendered = ", ".join(repr(name) for name in names)
            return f"Properties not allowed by the project schema: {rendered}."
        return "Object contains a property not allowed by the project schema."
    descriptions = {
        "oneOf": "Value must match exactly one permitted project-schema alternative.",
        "anyOf": "Value must match at least one permitted project-schema alternative.",
        "enum": "Value is not one of the project-schema allowed enum values.",
        "const": "Value does not equal the project-schema required constant.",
        "minItems": "Array has fewer items than the project schema requires.",
        "maxItems": "Array has more items than the project schema allows.",
        "minLength": "String is shorter than the project schema requires.",
        "maxLength": "String is longer than the project schema allows.",
        "minimum": "Number is below the project-schema minimum.",
        "maximum": "Number exceeds the project-schema maximum.",
        "pattern": "String does not match the project-schema pattern.",
    }
    return descriptions.get(
        validator,
        f"Project JSON Schema validator {validator!r} rejected this value.",
    )


def _additional_property_names(error: ValidationError) -> tuple[str, ...]:
    """Return rejected object keys without serializing any rejected values."""

    if not isinstance(error.instance, Mapping) or not isinstance(error.schema, Mapping):
        return ()
    properties = error.schema.get("properties")
    known_names = set(properties) if isinstance(properties, Mapping) else set()
    pattern_properties = error.schema.get("patternProperties")
    patterns = (
        tuple(pattern_properties)
        if isinstance(pattern_properties, Mapping)
        else ()
    )
    unexpected: list[str] = []
    for key in error.instance:
        if not isinstance(key, str) or key in known_names:
            continue
        if any(re.search(pattern, key) is not None for pattern in patterns):
            continue
        unexpected.append(key)
    return tuple(sorted(unexpected))


def _schema_error_leaves(error: ValidationError) -> tuple[ValidationError, ...]:
    if not error.context:
        return (error,)
    if error.validator in {"anyOf", "oneOf"}:
        branch_errors = _schema_union_branch_errors(error)
        if branch_errors is not None:
            branch_leaves = {
                branch_index: tuple(
                    leaf
                    for child_error in child_errors
                    for leaf in _schema_error_leaves(child_error)
                )
                for branch_index, child_errors in branch_errors.items()
            }
            matching_branch = _explicit_type_matching_union_branch(
                error,
                branch_indices=tuple(branch_leaves),
            )
            if matching_branch is None:
                matching_branch = _explicit_object_discriminator_matching_union_branch(
                    error,
                    branch_indices=tuple(branch_leaves),
                )
            if matching_branch is not None:
                return _sorted_schema_errors(branch_leaves[matching_branch])
            smallest_error_count = min(len(leaves) for leaves in branch_leaves.values())
            return _sorted_schema_errors(
                leaf
                for leaves in branch_leaves.values()
                if len(leaves) == smallest_error_count
                for leaf in leaves
            )
    leaves: list[ValidationError] = []
    for child_error in error.context:
        leaves.extend(_schema_error_leaves(child_error))
    return _sorted_schema_errors(leaves)


def _explicit_type_matching_union_branch(
    error: ValidationError,
    *,
    branch_indices: tuple[int, ...],
) -> int | None:
    """Choose a union branch only when one explicit JSON type matches the value."""

    if not isinstance(error.schema, Mapping):
        return None
    branches = error.schema.get(error.validator)
    if not isinstance(branches, list):
        return None
    matching_indices: list[int] = []
    for branch_index in branch_indices:
        if branch_index >= len(branches):
            return None
        branch = branches[branch_index]
        if not isinstance(branch, Mapping):
            return None
        branch_type = branch.get("type")
        if not isinstance(branch_type, str):
            return None
        if _matches_json_schema_type(error.instance, branch_type):
            matching_indices.append(branch_index)
    return matching_indices[0] if len(matching_indices) == 1 else None


def _explicit_object_discriminator_matching_union_branch(
    error: ValidationError,
    *,
    branch_indices: tuple[int, ...],
) -> int | None:
    """Choose an object-union branch only from explicit property literals."""

    if not isinstance(error.instance, Mapping) or not isinstance(error.schema, Mapping):
        return None
    branches = error.schema.get(error.validator)
    if not isinstance(branches, list):
        return None
    branch_discriminators: dict[int, dict[str, object]] = {}
    for branch_index in branch_indices:
        if branch_index >= len(branches):
            return None
        branch = branches[branch_index]
        if not isinstance(branch, Mapping):
            return None
        discriminators = _object_branch_discriminators(branch)
        if discriminators is None or not discriminators:
            return None
        branch_discriminators[branch_index] = discriminators

    selected_branch: int | None = None
    for field_name, value in error.instance.items():
        if not isinstance(field_name, str):
            continue
        matching_indices = [
            branch_index
            for branch_index, discriminators in branch_discriminators.items()
            if discriminators.get(field_name, _NO_SINGLE_VALUE_DISCRIMINATOR) == value
        ]
        if len(matching_indices) != 1:
            continue
        matching_branch = matching_indices[0]
        if selected_branch is not None and selected_branch != matching_branch:
            return None
        selected_branch = matching_branch
    return selected_branch


def _object_branch_discriminators(
    branch: Mapping[str, object],
) -> dict[str, object] | None:
    """Read direct and allOf-wrapped single-value object property constraints."""

    discriminators: dict[str, object] = {}
    schemas = [branch]
    while schemas:
        schema = schemas.pop()
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for field_name, property_schema in properties.items():
                if not isinstance(field_name, str) or not isinstance(property_schema, Mapping):
                    continue
                discriminator = _single_value_discriminator(property_schema)
                if discriminator is not _NO_SINGLE_VALUE_DISCRIMINATOR:
                    existing = discriminators.get(field_name, _NO_SINGLE_VALUE_DISCRIMINATOR)
                    if (
                        existing is not _NO_SINGLE_VALUE_DISCRIMINATOR
                        and existing != discriminator
                    ):
                        return None
                    discriminators[field_name] = discriminator
        all_of = schema.get("allOf")
        if isinstance(all_of, list):
            schemas.extend(item for item in all_of if isinstance(item, Mapping))
    return discriminators


def _single_value_discriminator(schema: Mapping[str, object]) -> object:
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and len(enum) == 1:
        return enum[0]
    return _NO_SINGLE_VALUE_DISCRIMINATOR


def _matches_json_schema_type(value: object, schema_type: str) -> bool:
    if schema_type == "null":
        return value is None
    if schema_type == "object":
        return isinstance(value, Mapping)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return False


def _schema_union_branch_errors(
    error: ValidationError,
) -> dict[int, tuple[ValidationError, ...]] | None:
    parent_schema_path = tuple(error.absolute_schema_path)
    grouped: dict[int, list[ValidationError]] = {}
    for child_error in error.context:
        child_schema_path = tuple(child_error.absolute_schema_path)
        branch_position = len(parent_schema_path)
        if (
            child_schema_path[:branch_position] != parent_schema_path
            or len(child_schema_path) <= branch_position
            or not isinstance(child_schema_path[branch_position], int)
        ):
            return None
        branch_index = child_schema_path[branch_position]
        grouped.setdefault(branch_index, []).append(child_error)
    if not grouped:
        return None
    return {branch_index: tuple(child_errors) for branch_index, child_errors in grouped.items()}


def _sorted_schema_errors(
    errors: Iterable[ValidationError],
) -> tuple[ValidationError, ...]:
    return tuple(
        sorted(
            errors,
            key=lambda item: (
                _json_path(item.absolute_path),
                _json_path(item.absolute_schema_path),
                item.message,
            ),
        )
    )


def _json_path(path: Iterable[object]) -> str:
    parts = tuple(path)
    rendered = "$"
    for part in parts:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += f".{part}"
    return rendered


def _extract_anthropic_tool_input(
    response: object,
    *,
    tool_name: str,
) -> Mapping[str, object]:
    content = _get_value(response, "content")
    if not isinstance(content, list):
        raise AnthropicStructuredInferenceError(
            "Anthropic-compatible response missing content blocks."
        )
    tool_blocks: list[object] = []
    for block in content:
        if _get_value(block, "type") == "tool_use":
            tool_blocks.append(block)
    if len(tool_blocks) != 1:
        raise AnthropicStructuredInferenceError(
            f"Anthropic-compatible response must contain exactly one {tool_name} call."
        )
    tool_block = tool_blocks[0]
    if _get_value(tool_block, "name") != tool_name:
        raise AnthropicStructuredInferenceError(
            f"Anthropic-compatible response must call {tool_name}, not "
            f"{_get_value(tool_block, 'name')!r}."
        )
    tool_input = _get_value(tool_block, "input")
    if not isinstance(tool_input, Mapping):
        raise AnthropicStructuredInferenceError(
            f"Anthropic-compatible {tool_name} input was not an object."
        )
    return tool_input


def _anthropic_response_metadata(response: object) -> dict[str, object]:
    usage = _get_value(response, "usage")
    return {
        "response_id": _optional_scalar(_get_value(response, "id")),
        "stop_reason": _optional_scalar(_get_value(response, "stop_reason")),
        "usage": _to_jsonable(usage),
    }


def _anthropic_rejected_response_summary(response: object) -> dict[str, object]:
    return {
        **_anthropic_response_summary(response),
        "usage": _to_jsonable(_get_value(response, "usage")),
    }


def _anthropic_response_summary(response: object) -> dict[str, object]:
    content = _get_value(response, "content")
    block_summaries: list[dict[str, object]] = []
    if isinstance(content, list):
        for block in content[:8]:
            block_type = _optional_scalar(_get_value(block, "type"))
            summary: dict[str, object] = {"type": block_type}
            if block_type == "tool_use":
                summary["name"] = _optional_scalar(_get_value(block, "name"))
            block_summaries.append(summary)
    return {
        "response_id": _optional_scalar(_get_value(response, "id")),
        "stop_reason": _optional_scalar(_get_value(response, "stop_reason")),
        "content_blocks": block_summaries,
        "content_block_count": len(content) if isinstance(content, list) else None,
        "omitted_content_block_count": (
            max(0, len(content) - len(block_summaries))
            if isinstance(content, list)
            else 0
        ),
        "content_was_list": isinstance(content, list),
    }


def _optional_scalar(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _usage_token_counts(usage: object) -> tuple[int, int, int, int, bool] | None:
    input_tokens = _get_value(usage, "input_tokens")
    output_tokens = _get_value(usage, "output_tokens")
    if (
        not isinstance(input_tokens, int)
        or isinstance(input_tokens, bool)
        or input_tokens < 0
        or not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or output_tokens < 0
    ):
        return None
    cache_creation = _get_value(usage, "cache_creation_input_tokens")
    cache_read = _get_value(usage, "cache_read_input_tokens")
    if (
        not isinstance(cache_creation, int)
        or isinstance(cache_creation, bool)
        or cache_creation < 0
        or not isinstance(cache_read, int)
        or isinstance(cache_read, bool)
        or cache_read < 0
    ):
        return input_tokens, output_tokens, 0, 0, False
    return (
        input_tokens,
        output_tokens,
        cache_creation,
        cache_read,
        True,
    )


def _get_value(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _to_jsonable(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_to_jsonable(item) for item in value]
    return str(value)


def _tool_definition(
    *,
    name: str,
    description: str,
    input_schema: Mapping[str, object],
    enable_cache: bool,
) -> dict[str, object]:
    """Build a tool definition with an optional schema-only cache marker.

    The dynamic business context remains solely in the user message.  The
    marker applies only to this request's stable tool contract and schema.
    """

    definition: dict[str, object] = {
        "name": name,
        "description": description,
        "input_schema": input_schema,
    }
    if enable_cache:
        definition["cache_control"] = {"type": "ephemeral"}
    return definition


async def _close_anthropic_client(client: object) -> None:
    close = getattr(client, "close", None)
    if not callable(close):
        close = getattr(client, "aclose", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


@asynccontextmanager
async def structured_inference_session(provider: object) -> Any:
    """Open an attempt-scoped session when the provider supports one."""

    session = getattr(provider, "session", None)
    if callable(session):
        async with session() as attempt_provider:
            yield attempt_provider
        return
    yield provider


def _build_anthropic_client(config: AnthropicStructuredInferenceConfig) -> object:
    try:
        from anthropic import AsyncAnthropic
    except Exception as exc:  # pragma: no cover - environment dependent
        raise AnthropicStructuredInferenceError(f"Anthropic SDK is not available: {exc}") from exc
    return AsyncAnthropic(api_key=config.api_key, base_url=config.base_url)


__all__ = [
    "AnthropicStructuredInference",
    "AnthropicStructuredInferenceConfig",
    "AnthropicStructuredInferenceError",
    "structured_inference_session",
]
