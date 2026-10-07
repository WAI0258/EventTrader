"""Project-owned bounded workflow for PMReview cognition."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from event_trader.pm_review.contracts import (
    PMReviewInput,
    pm_review_hold_reason_required,
)
from event_trader.pm_review.output import (
    pm_decision_draft_repair_schema,
    pm_decision_draft_schema,
)
from event_trader.pm_review.prompt import (
    parse_pm_review_contract_repair_fields,
    parse_pm_review_input_from_task_prompt,
)
from event_trader.pm_review.tools import PMReviewToolResult
from event_trader.reasoning.anthropic_structured import (
    AnthropicStructuredInference,
    AnthropicStructuredInferenceConfig,
    structured_inference_session,
)
from event_trader.reasoning.effort import normalize_agent_reasoning_effort
from event_trader.reasoning.runtime import (
    AgentRunReceipt,
    AgentRuntime,
    AgentTask,
    AgentToolGateway,
)
from event_trader.reasoning.structured_inference import (
    StructuredInference,
)
from event_trader.reasoning.token_budget import (
    StructuredPromptBudgetError,
    assert_structured_prompt_budget,
    validate_structured_token_budget,
)

_PLAN_TOOL_NAME = "pm_review_supplemental_read_plan"
_FINAL_TOOL_NAME = "pm_review_decision_draft"
_READ_PLAN_CONTRACT_MAX_ATTEMPTS = 3
_TEMPERATURE = 0.1
_TRANSPORT_MAX_ATTEMPTS = 5
_TRANSPORT_RETRY_SECONDS = 10
_STRUCTURED_PROTOCOL_MAX_ATTEMPTS = 3


class PMReviewWorkflowError(RuntimeError):
    """Raised when one bounded PMReview workflow attempt cannot be trusted."""


@dataclass(frozen=True, slots=True)
class PMReviewWorkflowConfig:
    """Explicit runtime inputs for the project-owned PMReview workflow."""

    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_reasoning_effort: str | None
    llm_max_context_length: int
    llm_max_output_tokens: int
    wall_clock_timeout_seconds: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "log_dir", _validate_path(self.log_dir, "log_dir"))
        object.__setattr__(
            self,
            "llm_provider",
            _validate_non_blank_text(self.llm_provider, "llm_provider").lower(),
        )
        object.__setattr__(
            self,
            "llm_model_name",
            _validate_non_blank_text(self.llm_model_name, "llm_model_name"),
        )
        object.__setattr__(
            self,
            "llm_api_key",
            _validate_non_blank_text(self.llm_api_key, "llm_api_key"),
        )
        object.__setattr__(
            self,
            "llm_base_url",
            _validate_non_blank_text(self.llm_base_url, "llm_base_url"),
        )
        normalized_reasoning_effort = normalize_agent_reasoning_effort(
            self.llm_reasoning_effort,
            field_name="llm_reasoning_effort",
            error_type=PMReviewWorkflowError,
        )
        if normalized_reasoning_effort is not None:
            raise PMReviewWorkflowError(
                "PMReview workflow does not support explicit llm_reasoning_effort "
                "without a provider-specific reasoning budget."
            )
        object.__setattr__(self, "llm_reasoning_effort", None)
        if self.llm_provider != "anthropic":
            raise PMReviewWorkflowError(
                "PMReview workflow currently requires llm_provider='anthropic'."
            )
        try:
            validate_structured_token_budget(
                max_context_tokens=self.llm_max_context_length,
                max_output_tokens=self.llm_max_output_tokens,
                field_prefix="PMReviewWorkflowConfig",
            )
        except StructuredPromptBudgetError as exc:
            raise PMReviewWorkflowError(str(exc)) from exc
        if (
            not isinstance(self.wall_clock_timeout_seconds, int)
            or isinstance(self.wall_clock_timeout_seconds, bool)
            or self.wall_clock_timeout_seconds < 0
        ):
            raise PMReviewWorkflowError(
                "wall_clock_timeout_seconds must be a non-negative integer."
            )


@dataclass(frozen=True, slots=True)
class _PMReviewWorkflowRuntime:
    config: PMReviewWorkflowConfig
    provider: StructuredInference

    async def run_once(
        self,
        task: AgentTask,
        tools: AgentToolGateway,
    ) -> AgentRunReceipt:
        if task.role != "pm_review":
            raise PMReviewWorkflowError(
                "PMReview workflow requires role='pm_review'."
            )
        trace: dict[str, object] = {
            "implementation": "event_trader",
            "task_id": task.task_id,
            "role": task.role,
            "task_description": task.task_description,
            "required_tool_names": list(task.required_tool_names),
            "provider": self.config.llm_provider,
            "model": self.config.llm_model_name,
            "status": "running",
        }
        try:
            async with structured_inference_session(self.provider) as attempt_provider:
                run = _execute_workflow_attempt(
                    config=self.config,
                    provider=attempt_provider,
                    task=task,
                    tools=tools,
                    trace=trace,
                )
                result = (
                    await asyncio.wait_for(
                        run,
                        timeout=self.config.wall_clock_timeout_seconds,
                    )
                    if self.config.wall_clock_timeout_seconds
                    else await run
                )
        except TimeoutError as exc:
            trace["status"] = "failed"
            trace["error"] = (
                "PMReview workflow timed out after "
                f"{self.config.wall_clock_timeout_seconds} seconds."
            )
            _write_workflow_trace(log_dir=self.config.log_dir, task=task, trace=trace)
            raise PMReviewWorkflowError(str(trace["error"])) from exc
        except Exception as exc:
            trace["status"] = "failed"
            trace["error_type"] = type(exc).__name__
            trace["error"] = str(exc).strip() or repr(exc)
            _write_workflow_trace(log_dir=self.config.log_dir, task=task, trace=trace)
            if isinstance(exc, PMReviewWorkflowError):
                raise
            raise PMReviewWorkflowError(str(trace["error"])) from exc
        trace["status"] = "succeeded"
        log_path = _write_workflow_trace(
            log_dir=self.config.log_dir,
            task=task,
            trace=trace,
        )
        return AgentRunReceipt(
            final_summary=result.final_summary,
            contract_output_text=result.contract_output_text,
            diagnostic_log_ref=str(log_path),
            diagnostics=result.diagnostics,
        )


@dataclass(frozen=True, slots=True)
class _PMReviewWorkflowAttempt:
    final_summary: str
    contract_output_text: str
    diagnostics: Mapping[str, object]


async def _execute_workflow_attempt(
    *,
    config: PMReviewWorkflowConfig,
    provider: StructuredInference,
    task: AgentTask,
    tools: AgentToolGateway,
    trace: dict[str, object],
) -> _PMReviewWorkflowAttempt:
    if not task.required_tool_names:
        raise PMReviewWorkflowError(
            "PMReview workflow requires at least one project-owned tool."
        )
    pm_review_input = parse_pm_review_input_from_task_prompt(task.task_description)
    repair_fields = parse_pm_review_contract_repair_fields(task.task_description)
    trace["contract_repair_fields"] = list(repair_fields)
    planned_tool_names: tuple[str, ...]
    read_plan_metadata: dict[str, object]
    if repair_fields:
        planned_tool_names = ()
        read_plan_metadata = {
            "contract_attempt_count": 0,
            "payload": {"supplemental_tool_names": []},
            "provider_attempts": [],
            "rejected_plans": [],
            "skipped_reason": "targeted_contract_repair",
        }
        trace["read_plan_prompt_chars"] = 0
    else:
        plan_prompt = _build_read_plan_prompt(task)
        _assert_prompt_budget(config, plan_prompt, stage="read plan")
        trace["read_plan_prompt_chars"] = len(plan_prompt)
        planned_tool_names, read_plan_metadata = await _generate_supplemental_read_plan(
            provider=provider,
            prompt=plan_prompt,
            session_id=task.task_id,
            allowed_tool_names=task.required_tool_names,
        )
    trace["read_plan"] = read_plan_metadata["payload"]
    trace["read_plan_provider_metadata"] = read_plan_metadata
    trace["planned_tool_names"] = list(planned_tool_names)

    tool_results: list[dict[str, object]] = []
    for tool_name in task.required_tool_names:
        if tool_name not in planned_tool_names:
            continue
        raw_result = await tools.call_tool(tool_name, {})
        if not isinstance(raw_result, PMReviewToolResult):
            raise PMReviewWorkflowError(
                f"PMReview tool returned a noncanonical result: {tool_name}."
            )
        if not raw_result.receipt.visibility_passed:
            raise PMReviewWorkflowError(
                f"PMReview tool visibility receipt failed: {tool_name}."
            )
        tool_results.append(
            {
                "tool_name": tool_name,
                "records": _to_jsonable(raw_result.records),
                "receipt": _to_jsonable(raw_result.receipt),
            }
        )
    trace["tool_results"] = tool_results

    final_prompt = _build_final_decision_prompt(
        task_description=task.task_description,
        tool_results=tool_results,
        pm_review_input=pm_review_input,
        repair_fields=repair_fields,
    )
    _assert_prompt_budget(config, final_prompt, stage="final decision")
    trace["final_prompt_chars"] = len(final_prompt)
    final_result = await provider.generate_structured(
        prompt=final_prompt,
        session_id=task.task_id,
        tool_name=_FINAL_TOOL_NAME,
        tool_description=(
            "Return the targeted PMDecisionDraft repair for deterministic project merging."
            if repair_fields
            else "Return the final PMDecisionDraft for deterministic project validation."
        ),
        input_schema=(
            pm_decision_draft_repair_schema(
                pm_review_input=pm_review_input,
                repair_fields=repair_fields,
            )
            if repair_fields
            else pm_decision_draft_schema(pm_review_input=pm_review_input)
        ),
    )
    final_payload = dict(final_result.payload)
    trace["final_provider_metadata"] = dict(final_result.metadata)
    trace["final_payload"] = final_payload
    payload_text = json.dumps(
        final_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _PMReviewWorkflowAttempt(
        final_summary=payload_text,
        contract_output_text=f"\\boxed{{{payload_text}}}",
        diagnostics={
            "implementation": "event_trader",
            "provider": config.llm_provider,
            "model": config.llm_model_name,
            "planned_tool_names": planned_tool_names,
            "executed_tool_names": tuple(cast(str, result["tool_name"]) for result in tool_results),
            "read_plan_provider_metadata": read_plan_metadata,
            "final_provider_metadata": dict(final_result.metadata),
        },
    )


def build_pm_review_workflow(
    *,
    config: PMReviewWorkflowConfig,
) -> AgentRuntime:
    """Build the project-owned bounded PMReview workflow."""

    if not isinstance(config, PMReviewWorkflowConfig):
        raise PMReviewWorkflowError(
            "config must be a PMReviewWorkflowConfig instance."
        )
    return _PMReviewWorkflowRuntime(
        config=config,
        provider=AnthropicStructuredInference(
            AnthropicStructuredInferenceConfig(
                model_name=config.llm_model_name,
                api_key=config.llm_api_key,
                base_url=config.llm_base_url,
                max_output_tokens=config.llm_max_output_tokens,
                temperature=_TEMPERATURE,
                transport_max_attempts=_TRANSPORT_MAX_ATTEMPTS,
                transport_retry_seconds=_TRANSPORT_RETRY_SECONDS,
                protocol_max_attempts=_STRUCTURED_PROTOCOL_MAX_ATTEMPTS,
                enable_tool_schema_cache=True,
            )
        ),
    )


def _assert_prompt_budget(
    config: PMReviewWorkflowConfig,
    prompt: str,
    *,
    stage: str,
) -> None:
    assert_structured_prompt_budget(
        prompt,
        max_context_tokens=config.llm_max_context_length,
        max_output_tokens=config.llm_max_output_tokens,
        stage=f"PMReview workflow {stage}",
        error_type=PMReviewWorkflowError,
    )


def _build_read_plan_prompt(task: AgentTask) -> str:
    return (
        f"{task.task_description.rstrip()}\n\n"
        "Supplemental read planning:\n"
        "- The complete PMReviewInput above is mandatory and already visible.\n"
        "- Do not make or draft the final PMDecisionDraft in this step.\n"
        "- Select only reads that add useful detail or audit confirmation.\n"
        "- An empty supplemental_tool_names list is valid when the PMReviewInput "
        "is already sufficient.\n"
        "- You may select each listed project-owned tool at most once."
    )


def _build_final_decision_prompt(
    *,
    task_description: str,
    tool_results: list[dict[str, object]],
    pm_review_input: PMReviewInput,
    repair_fields: tuple[str, ...],
) -> str:
    if repair_fields:
        return (
            f"{task_description.rstrip()}\n\n"
            "Final targeted repair instructions:\n"
            "- Do not reconsider or regenerate the PM decision.\n"
            "- Return only the fields named by PMReviewContractRepair.\n"
            "- Return the targeted repair through the required structured tool."
        )
    serialized_tool_results = json.dumps(
        tool_results,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    hold_instruction = (
        "- If requested_state remains "
        f"`{pm_review_input.actual_current_state}`, hold_reason_code must be a "
        "non-null lower_snake_case code; returning null violates the contract.\n"
        if pm_review_hold_reason_required(
            pm_review_input=pm_review_input,
            requested_state=pm_review_input.actual_current_state,
        )
        else "- hold_reason_code may be null when no mandatory hold condition applies.\n"
    )
    return (
        f"{task_description.rstrip()}\n\n"
        "Supplemental project-owned PMReview reads:\n"
        f"{serialized_tool_results}\n\n"
        "Final decision instructions:\n"
        "- You are the single final PM business owner for this attempt.\n"
        "- The original PMReviewInput remains the deterministic source of truth.\n"
        "- Supplemental reads may add detail but cannot override visibility or identity.\n"
        "- Cite only event ids and market bar ids visible in the original PMReviewInput.\n"
        f"{hold_instruction}"
        "- Do not claim a market price crossed, held, or sits inside an analysis level "
        "when their instrument labels or price scales conflict.\n"
        "- Copy event ids exactly. A cited market bar id must use the exact canonical "
        "bar:<visible start_at>:<visible end_at> form; never use a bare timestamp.\n"
        "- Return the final PMDecisionDraft through the required structured tool."
    )


def _supplemental_read_plan_schema(
    allowed_tool_names: tuple[str, ...],
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["supplemental_tool_names"],
        "properties": {
            "supplemental_tool_names": {
                "type": "array",
                "items": {"type": "string", "enum": list(allowed_tool_names)},
                "maxItems": len(allowed_tool_names),
                "uniqueItems": True,
            }
        },
    }


async def _generate_supplemental_read_plan(
    *,
    provider: StructuredInference,
    prompt: str,
    session_id: str,
    allowed_tool_names: tuple[str, ...],
) -> tuple[tuple[str, ...], dict[str, object]]:
    current_prompt = prompt
    provider_attempts: list[dict[str, object]] = []
    rejected_plans: list[dict[str, object]] = []
    for contract_attempt in range(1, _READ_PLAN_CONTRACT_MAX_ATTEMPTS + 1):
        plan_result = await provider.generate_structured(
            prompt=current_prompt,
            session_id=session_id,
            tool_name=_PLAN_TOOL_NAME,
            tool_description=(
                "Select only the project-owned PMReview reads that add information "
                "beyond the complete PMReviewInput already present in the task."
            ),
            input_schema=_supplemental_read_plan_schema(allowed_tool_names),
        )
        provider_attempts.append(dict(plan_result.metadata))
        try:
            planned_tool_names = _parse_supplemental_tool_names(
                plan_result.payload,
                allowed_tool_names=allowed_tool_names,
            )
        except PMReviewWorkflowError as exc:
            rejected_plans.append(
                {
                    "contract_attempt": contract_attempt,
                    "error": str(exc),
                    "payload": dict(plan_result.payload),
                }
            )
            if contract_attempt >= _READ_PLAN_CONTRACT_MAX_ATTEMPTS:
                raise PMReviewWorkflowError(
                    "PMReview workflow read-plan contract repair exhausted after "
                    f"{contract_attempt} attempts: {exc}"
                ) from exc
            current_prompt = (
                f"{prompt.rstrip()}\n\n"
                "Previous read plan failed deterministic event-trader workflow "
                "contract:\n"
                f"- {exc}\n"
                "Return only a corrected supplemental_tool_names plan for the same "
                "PMReview context. Do not make the final PM decision."
            )
            continue
        return planned_tool_names, {
            "contract_attempt_count": contract_attempt,
            "payload": dict(plan_result.payload),
            "provider_attempts": provider_attempts,
            "rejected_plans": rejected_plans,
        }
    raise AssertionError("PMReview read-plan contract repair loop ended unexpectedly")


def _parse_supplemental_tool_names(
    payload: Mapping[str, object],
    *,
    allowed_tool_names: tuple[str, ...],
) -> tuple[str, ...]:
    if set(payload) != {"supplemental_tool_names"}:
        raise PMReviewWorkflowError(
            "PMReview workflow read plan must contain exactly supplemental_tool_names."
        )
    raw_names = payload.get("supplemental_tool_names")
    if not isinstance(raw_names, list):
        raise PMReviewWorkflowError(
            "PMReview workflow supplemental_tool_names must be a list."
        )
    if any(not isinstance(name, str) or not name.strip() for name in raw_names):
        raise PMReviewWorkflowError(
            "PMReview workflow supplemental_tool_names must contain non-blank strings."
        )
    names = tuple(cast(str, name).strip() for name in raw_names)
    if len(set(names)) != len(names):
        raise PMReviewWorkflowError(
            "PMReview workflow supplemental_tool_names must not contain duplicates."
        )
    forbidden = tuple(name for name in names if name not in allowed_tool_names)
    if forbidden:
        raise PMReviewWorkflowError(
            f"PMReview workflow read plan selected forbidden tool(s): {', '.join(forbidden)}."
        )
    selected = set(names)
    return tuple(name for name in allowed_tool_names if name in selected)


def _to_jsonable(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_to_jsonable(item) for item in value]
    if hasattr(value, "to_json_payload"):
        return _to_jsonable(value.to_json_payload())
    if is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(asdict(cast(Any, value)))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _to_jsonable(model_dump())
    return str(value)


def _write_workflow_trace(
    *,
    log_dir: Path,
    task: AgentTask,
    trace: Mapping[str, object],
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    task_digest = sha256(task.task_id.encode("utf-8")).hexdigest()[:24]
    path = log_dir / f"event-trader-pm-review-{task_digest}-{uuid4().hex}.json"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=log_dir,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(
                _to_jsonable(trace),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
        os.replace(temporary_path, path)
    except Exception as exc:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        raise PMReviewWorkflowError(
            f"failed to persist PMReview workflow diagnostic log: {exc}"
        ) from exc
    return path


def _validate_path(value: Path, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise PMReviewWorkflowError(f"{field_name} must be a Path.")
    return value.expanduser().resolve(strict=False)


def _validate_non_blank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PMReviewWorkflowError(f"{field_name} must be non-blank.")
    return value.strip()


__all__ = [
    "PMReviewWorkflowConfig",
    "PMReviewWorkflowError",
    "build_pm_review_workflow",
]
