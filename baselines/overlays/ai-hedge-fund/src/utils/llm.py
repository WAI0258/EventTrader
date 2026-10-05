"""Helper functions for LLM"""

import json
import re
from pydantic import BaseModel
from langchain_core.callbacks.base import BaseCallbackHandler
from src.llm.models import get_model, get_model_info
from src.utils.progress import progress
from src.graph.state import AgentState


def export_audit_payload(state: AgentState | None) -> dict:
    """Return a detached audit payload for callers that need to persist usage."""
    metadata = (state or {}).get("metadata", {})
    audit = metadata.get("audit")
    if not isinstance(audit, dict):
        return {}
    llm_usage = audit.get("llm_usage")
    if not isinstance(llm_usage, dict):
        return {}
    return {
        "llm_usage": {
            "llm_calls": int(llm_usage.get("llm_calls", 0) or 0),
            "tokens_in": int(llm_usage.get("tokens_in", 0) or 0),
            "tokens_out": int(llm_usage.get("tokens_out", 0) or 0),
            "tokens_total": int(llm_usage.get("tokens_total", 0) or 0),
            "usage_observed": bool(llm_usage.get("usage_observed")),
            "calls_missing_usage": int(llm_usage.get("calls_missing_usage", 0) or 0),
            "per_agent_calls": {
                str(agent): int(count or 0)
                for agent, count in (llm_usage.get("per_agent_calls") or {}).items()
            },
        }
    }


def _ensure_audit_bucket(state: AgentState | None) -> dict | None:
    if not state:
        return None
    metadata = state.setdefault("metadata", {})
    audit = metadata.get("audit")
    if audit is None:
        audit = {}
        metadata["audit"] = audit
    if not isinstance(audit, dict):
        return None
    usage = audit.setdefault(
        "llm_usage",
        {
            "llm_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_total": 0,
            "usage_observed": False,
            "calls_missing_usage": 0,
            "per_agent_calls": {},
        },
    )
    if not isinstance(usage, dict):
        return None
    return usage


def _extract_usage_fields(payload: dict | None) -> tuple[int, int, int]:
    if not payload:
        return 0, 0, 0
    candidates = [payload]
    for key in ("usage", "token_usage", "usage_metadata"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)

    for candidate in candidates:
        input_tokens = (
            candidate.get("input_tokens")
            or candidate.get("prompt_tokens")
            or candidate.get("prompt_token_count")
            or 0
        )
        output_tokens = (
            candidate.get("output_tokens")
            or candidate.get("completion_tokens")
            or candidate.get("completion_token_count")
            or 0
        )
        total_tokens = candidate.get("total_tokens") or (input_tokens + output_tokens)
        if input_tokens or output_tokens or total_tokens:
            return int(input_tokens or 0), int(output_tokens or 0), int(total_tokens or 0)
    return 0, 0, 0


def _record_llm_usage(
    state: AgentState | None,
    *,
    agent_name: str | None,
    llm_calls: int,
    usage_payload: dict | None,
) -> None:
    bucket = _ensure_audit_bucket(state)
    if bucket is None:
        return
    bucket["llm_calls"] += llm_calls
    if agent_name:
        bucket["per_agent_calls"][agent_name] = bucket["per_agent_calls"].get(agent_name, 0) + llm_calls

    tokens_in, tokens_out, tokens_total = _extract_usage_fields(usage_payload)
    if tokens_total > 0 or tokens_in > 0 or tokens_out > 0:
        bucket["usage_observed"] = True
        bucket["tokens_in"] += tokens_in
        bucket["tokens_out"] += tokens_out
        bucket["tokens_total"] += tokens_total
    else:
        bucket["calls_missing_usage"] += llm_calls


def _extract_usage_from_result(result) -> dict | None:
    for attr in ("usage_metadata", "response_metadata", "additional_kwargs"):
        usage_payload = getattr(result, attr, None)
        if isinstance(usage_payload, dict) and any(_extract_usage_fields(usage_payload)):
            return usage_payload
    return None


class UsageAuditCallback(BaseCallbackHandler):
    def __init__(self) -> None:
        self.calls = 0
        self.last_usage: dict | None = None

    def on_llm_end(self, response, **kwargs) -> None:
        del kwargs
        self.calls += 1
        llm_output = getattr(response, "llm_output", None)
        if isinstance(llm_output, dict) and any(_extract_usage_fields(llm_output)):
            self.last_usage = llm_output
            return
        generations = getattr(response, "generations", None) or []
        for generation_group in generations:
            for generation in generation_group:
                message = getattr(generation, "message", None)
                usage = _extract_usage_from_result(message)
                if usage is not None:
                    self.last_usage = usage
                    return


def _iter_exception_chain(exc: Exception):
    current = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _extract_exception_status_code(exc: Exception) -> int | None:
    for item in _iter_exception_chain(exc):
        for attr in ("status_code", "http_status", "code"):
            value = getattr(item, attr, None)
            if isinstance(value, int):
                return value
        response = getattr(item, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return status_code
    return None


def _is_fatal_provider_error(exc: Exception) -> bool:
    status_code = _extract_exception_status_code(exc)
    if status_code in {401, 403, 429}:
        return True

    messages = " | ".join(str(item).lower() for item in _iter_exception_chain(exc))
    if not messages:
        return False

    fatal_patterns = (
        r"\b401\b",
        r"\b403\b",
        r"\b429\b",
        r"unauthorized",
        r"forbidden",
        r"authentication",
        r"invalid api key",
        r"rate limit",
        r"too many requests",
        r"insufficient quota",
        r"exceeded your current quota",
        r"billing hard limit",
        r"credit balance",
    )
    return any(re.search(pattern, messages) for pattern in fatal_patterns)


def call_llm(
    prompt: any,
    pydantic_model: type[BaseModel],
    agent_name: str | None = None,
    state: AgentState | None = None,
    max_retries: int = 3,
    default_factory=None,
) -> BaseModel:
    """
    Makes an LLM call with retry logic, handling both JSON supported and non-JSON supported models.

    Args:
        prompt: The prompt to send to the LLM
        pydantic_model: The Pydantic model class to structure the output
        agent_name: Optional name of the agent for progress updates and model config extraction
        state: Optional state object to extract agent-specific model configuration
        max_retries: Maximum number of retries (default: 3)
        default_factory: Optional factory function to create default response on failure

    Returns:
        An instance of the specified Pydantic model
    """
    
    # Extract model configuration if state is provided and agent_name is available
    if state and agent_name:
        model_name, model_provider = get_agent_model_config(state, agent_name)
    else:
        # Use system defaults when no state or agent_name is provided
        model_name = "gpt-4.1"
        model_provider = "OPENAI"

    # Extract API keys from state if available
    api_keys = None
    if state:
        request = state.get("metadata", {}).get("request")
        if request and hasattr(request, 'api_keys'):
            api_keys = request.api_keys

    model_info = get_model_info(model_name, model_provider)
    llm = get_model(model_name, model_provider, api_keys)
    usage_callback = UsageAuditCallback()
    llm = llm.with_config({"callbacks": [usage_callback]})

    # For non-JSON support models, we can use structured output
    if not (model_info and not model_info.has_json_mode()):
        llm = llm.with_structured_output(
            pydantic_model,
            method="json_mode",
        )

    # Call the LLM with retries
    for attempt in range(max_retries):
        try:
            usage_callback.calls = 0
            usage_callback.last_usage = None
            # Call the LLM
            result = llm.invoke(prompt)

            # For non-JSON support models, we need to extract and parse the JSON manually
            if model_info and not model_info.has_json_mode():
                parsed_result = extract_json_from_response(result.content)
                usage_payload = usage_callback.last_usage or _extract_usage_from_result(result)
                _record_llm_usage(
                    state,
                    agent_name=agent_name,
                    llm_calls=max(usage_callback.calls, 1),
                    usage_payload=usage_payload,
                )
                if parsed_result:
                    return pydantic_model(**parsed_result)
            else:
                usage_payload = usage_callback.last_usage or _extract_usage_from_result(result)
                _record_llm_usage(
                    state,
                    agent_name=agent_name,
                    llm_calls=max(usage_callback.calls, 1),
                    usage_payload=usage_payload,
                )
                return result

        except Exception as e:
            if agent_name:
                progress.update_status(agent_name, None, f"Error - retry {attempt + 1}/{max_retries}")

            if _is_fatal_provider_error(e):
                print(f"Fatal LLM API error: {e}")
                raise

            if attempt == max_retries - 1:
                print(f"Error in LLM call after {max_retries} attempts: {e}")
                # Use default_factory if provided, otherwise create a basic default
                if default_factory:
                    return default_factory()
                return create_default_response(pydantic_model)

    # This should never be reached due to the retry logic above
    return create_default_response(pydantic_model)


def create_default_response(model_class: type[BaseModel]) -> BaseModel:
    """Creates a safe default response based on the model's fields."""
    default_values = {}
    for field_name, field in model_class.model_fields.items():
        if field.annotation == str:
            default_values[field_name] = "Error in analysis, using default"
        elif field.annotation == float:
            default_values[field_name] = 0.0
        elif field.annotation == int:
            default_values[field_name] = 0
        elif hasattr(field.annotation, "__origin__") and field.annotation.__origin__ == dict:
            default_values[field_name] = {}
        else:
            # For other types (like Literal), try to use the first allowed value
            if hasattr(field.annotation, "__args__"):
                default_values[field_name] = field.annotation.__args__[0]
            else:
                default_values[field_name] = None

    return model_class(**default_values)


def extract_json_from_response(content) -> dict | None:
    """Extracts JSON from a response, handling markdown-wrapped and raw JSON formats."""
    try:
        # Reasoning models (e.g. Anthropic extended thinking) return content as a
        # list of blocks (thinking + text). Concatenate the text blocks.
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            content = "\n".join(parts)
        # 1. Try markdown code block with ```json
        json_start = content.find("```json")
        if json_start != -1:
            json_text = content[json_start + 7:]  # Skip past ```json
            json_end = json_text.find("```")
            if json_end != -1:
                json_text = json_text[:json_end].strip()
                try:
                    return json.loads(json_text)
                except json.JSONDecodeError:
                    pass

        # 2. Try markdown code block without json specifier
        json_start = content.find("```")
        if json_start != -1:
            json_text = content[json_start + 3:]
            json_end = json_text.find("```")
            if json_end != -1:
                json_text = json_text[:json_end].strip()
                try:
                    return json.loads(json_text)
                except json.JSONDecodeError:
                    pass

        # 3. Try to parse the entire content as JSON
        try:
            return json.loads(content.strip())
        except json.JSONDecodeError:
            pass

        # 4. Find the first top-level JSON object by matching braces
        brace_start = content.find("{")
        if brace_start != -1:
            depth = 0
            for i, char in enumerate(content[brace_start:], brace_start):
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(content[brace_start:i + 1])
                        except json.JSONDecodeError:
                            break

    except Exception as e:
        print(f"Error extracting JSON from response: {e}")
    return None


def get_agent_model_config(state, agent_name):
    """
    Get model configuration for a specific agent from the state.
    Falls back to global model configuration if agent-specific config is not available.
    Always returns valid model_name and model_provider values.
    """
    request = state.get("metadata", {}).get("request")
    
    if request and hasattr(request, 'get_agent_model_config'):
        # Get agent-specific model configuration
        model_name, model_provider = request.get_agent_model_config(agent_name)
        # Ensure we have valid values
        if model_name and model_provider:
            return model_name, model_provider.value if hasattr(model_provider, 'value') else str(model_provider)
    
    # Fall back to global configuration (system defaults)
    model_name = state.get("metadata", {}).get("model_name") or "gpt-4.1"
    model_provider = state.get("metadata", {}).get("model_provider") or "OPENAI"
    
    # Convert enum to string if necessary
    if hasattr(model_provider, 'value'):
        model_provider = model_provider.value
    
    return model_name, model_provider
