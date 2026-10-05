"""Single-pass checker routing policy over deterministic context packs."""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import sleep
from typing import Any, cast
from urllib.request import Request, urlopen

from event_trader.checker.context_pack import (
    CheckerContextPack,
    serialize_context_pack,
)
from event_trader.checker.recorder import CheckerExecutionError
from event_trader.checker.validator import (
    NoActionSupport,
    RawCheckerDecision,
)
from event_trader.contracts import LLMUsageReceipt

CHECKER_POLICY_NAME = "single_pass_context_pack"
CHECKER_POLICY_VERSION = "checker_single_pass_context_pack_2026_05"
_CHECKER_LLM_MAX_ATTEMPTS = 3
_CHECKER_LLM_RETRY_SLEEP_SECONDS = 2.0
_CHECKER_CONTRACT_REPAIR_MAX_ATTEMPTS = 2


class SinglePassCheckerPolicyError(ValueError):
    """Raised when single-pass checker dependencies or output are malformed."""


@dataclass(frozen=True, slots=True)
class SinglePassCheckerPolicy:
    """Call one OpenAI-compatible chat completion and parse raw checker output."""

    log_dir: Path
    llm_provider: str
    llm_model_name: str
    llm_api_key: str
    llm_base_url: str
    llm_reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.log_dir, Path):
            raise SinglePassCheckerPolicyError("log_dir must be a pathlib.Path.")
        llm_provider = _require_non_blank(self.llm_provider, field_name="llm_provider")
        if llm_provider != "openai":
            raise SinglePassCheckerPolicyError(
                "llm_provider must be 'openai' for the single-pass checker."
            )
        _require_non_blank(self.llm_model_name, field_name="llm_model_name")
        _require_non_blank(self.llm_api_key, field_name="llm_api_key")
        _require_non_blank(self.llm_base_url, field_name="llm_base_url")
        if self.llm_reasoning_effort is not None:
            _require_non_blank(
                self.llm_reasoning_effort,
                field_name="llm_reasoning_effort",
            )

    def __call__(self, pack: CheckerContextPack) -> RawCheckerDecision:
        if not isinstance(pack, CheckerContextPack):
            raise SinglePassCheckerPolicyError(
                "pack must be a CheckerContextPack instance."
            )
        self.log_dir.mkdir(parents=True, exist_ok=True)
        attempts: list[dict[str, object]] = []
        llm_usages: list[LLMUsageReceipt] = []
        prompt = build_single_pass_checker_prompt(pack)
        final_status = "accepted"
        final_decision: RawCheckerDecision | None = None
        for attempt_number in range(1, _CHECKER_CONTRACT_REPAIR_MAX_ATTEMPTS + 2):
            prompt_kind = "initial" if attempt_number == 1 else "contract_repair"
            prompt_sha256 = sha256(prompt.encode("utf-8")).hexdigest()
            try:
                response_text, llm_usage = _request_chat_completion(
                    base_url=self.llm_base_url,
                    api_key=self.llm_api_key,
                    model=self.llm_model_name,
                    prompt=prompt,
                    reasoning_effort=self.llm_reasoning_effort,
                )
            except SinglePassCheckerPolicyError as exc:
                attempts.append(
                    _checker_attempt_payload(
                        attempt_number=attempt_number,
                        prompt_kind=prompt_kind,
                        prompt=prompt,
                        prompt_sha256=prompt_sha256,
                        response_text=None,
                        llm_usage=None,
                        parse_status="request_error",
                        error=str(exc),
                    )
                )
                _write_checker_attempt_log(
                    log_dir=self.log_dir,
                    pack=pack,
                    attempts=attempts,
                    final_status="request_error",
                    final_decision=None,
                )
                raise
            llm_usages.append(llm_usage)
            try:
                parsed = parse_single_pass_checker_output(response_text, pack=pack)
            except SinglePassCheckerPolicyError as exc:
                attempts.append(
                    _checker_attempt_payload(
                        attempt_number=attempt_number,
                        prompt_kind=prompt_kind,
                        prompt=prompt,
                        prompt_sha256=prompt_sha256,
                        response_text=response_text,
                        llm_usage=llm_usage,
                        parse_status="contract_error",
                        error=str(exc),
                    )
                )
                if attempt_number <= _CHECKER_CONTRACT_REPAIR_MAX_ATTEMPTS:
                    prompt = build_checker_contract_repair_prompt(
                        pack,
                        previous_response_text=response_text,
                        contract_error=str(exc),
                    )
                    continue
                final_status = "synthesized_escalate"
                final_decision = _build_contract_violation_escalate(
                    pack=pack,
                    reason=str(exc),
                    llm_usage=_combine_llm_usage_receipts(tuple(llm_usages)),
                )
                _write_checker_attempt_log(
                    log_dir=self.log_dir,
                    pack=pack,
                    attempts=attempts,
                    final_status=final_status,
                    final_decision=final_decision,
                )
                return final_decision
            attempts.append(
                _checker_attempt_payload(
                    attempt_number=attempt_number,
                    prompt_kind=prompt_kind,
                    prompt=prompt,
                    prompt_sha256=prompt_sha256,
                    response_text=response_text,
                    llm_usage=llm_usage,
                    parse_status="accepted",
                    error=None,
                )
            )
            final_decision = RawCheckerDecision(
                target_key=parsed.target_key,
                event_ids=parsed.event_ids,
                decision=parsed.decision,
                rationale=parsed.rationale,
                attention_hint=parsed.attention_hint,
                requires_watchlist_maintenance=parsed.requires_watchlist_maintenance,
                uncertainty=parsed.uncertainty,
                possible_watchlist_trigger=parsed.possible_watchlist_trigger,
                no_action_support=parsed.no_action_support,
                operational_status=parsed.operational_status,
                operational_detail=parsed.operational_detail,
                llm_usage=_combine_llm_usage_receipts(tuple(llm_usages)),
            )
            _write_checker_attempt_log(
                log_dir=self.log_dir,
                pack=pack,
                attempts=attempts,
                final_status=final_status,
                final_decision=final_decision,
            )
            return final_decision
        raise SinglePassCheckerPolicyError("checker contract repair loop exhausted.")


def build_single_pass_checker_prompt(pack: CheckerContextPack) -> str:
    """Build a tool-free routing prompt over the fixed checker context pack."""
    if not isinstance(pack, CheckerContextPack):
        raise SinglePassCheckerPolicyError("pack must be a CheckerContextPack.")
    pack_payload = serialize_context_pack(pack)
    return (
        f"{_checker_role_prompt()}"
        f"{_checker_operating_boundary_prompt()}"
        "Decision policy:\n"
        "- Do not ask for more context.\n"
        "- Do not infer that absent bounded context proves irrelevance.\n"
        "- Choose escalate if evidence may affect thesis, risks, watchlist, "
        "timeline, trading view, or future trigger conditions.\n"
        "- If market_context is present, use it only as deterministic market "
        "facts for attention/materiality: price recap risk, abnormal volume or "
        "range, derivatives activity, cross-asset confirmation or divergence, "
        "stale context, and whether price may already have reacted.\n"
        "- Do not invent unavailable market facts or treat market context as a "
        "trading decision rule.\n"
        "- Treat high_impact_event as materiality context, not as an automatic "
        "escalation override.\n"
        "- Do not turn missing, empty, or truncated memory sections into market "
        "escalation by themselves.\n"
        "- Choose escalate when the evidence may need real analysis attention or "
        "when you are genuinely uncertain about relevance/materiality.\n"
        "- Choose no_action only with positive structured support. Use "
        "prior_source_event_ids and source_identity as deterministic duplicate or "
        "already-covered evidence when they are present.\n"
        "- request.target_key is the only allowed target identity in the output.\n"
        "- request.event_ids is the only allowed event identity in the output.\n"
        "- event_ids must exactly equal request.event_ids in the same order.\n"
        "- This checker pack contains exactly one active admitted evidence item.\n"
        "- Do not copy event ids from memory sections, thesis pages, risk pages, "
        "watchlists, timelines, or prior evidence references.\n"
        "- If you are uncertain, keep target_key/event_ids unchanged and choose "
        "escalate.\n"
        "- attention_hint must be null for no_action and non-empty for escalate.\n"
        "- requires_watchlist_maintenance must be false for no_action.\n\n"
        "Output contract:\n"
        "Return exactly one JSON object and no other text. Shape:\n"
        "{\n"
        '  "target_key": string,\n'
        '  "event_ids": [string],\n'
        '  "decision": "no_action" | "escalate",\n'
        '  "rationale": string,\n'
        '  "attention_hint": string | null,\n'
        '  "requires_watchlist_maintenance": boolean,\n'
        '  "uncertainty": boolean,\n'
        '  "possible_watchlist_trigger": boolean,\n'
        '  "no_action_support": null | {\n'
        '    "support_type": "duplicate" | "already_covered" '
        '| "low_relevance" | "low_materiality",\n'
        '    "supporting_sections": [string],\n'
        '    "support_summary": string\n'
        "  }\n"
        "}\n\n"
        "Context pack JSON:\n"
        f"{json.dumps(pack_payload, ensure_ascii=False, sort_keys=True, indent=2)}\n"
    )


def build_checker_contract_repair_prompt(
    pack: CheckerContextPack,
    *,
    previous_response_text: str,
    contract_error: str,
) -> str:
    """Ask the checker to repair only contract-invalid output."""
    if not isinstance(pack, CheckerContextPack):
        raise SinglePassCheckerPolicyError("pack must be a CheckerContextPack.")
    if not isinstance(previous_response_text, str) or not previous_response_text.strip():
        raise SinglePassCheckerPolicyError(
            "previous_response_text must be a non-blank string."
        )
    if not isinstance(contract_error, str) or not contract_error.strip():
        raise SinglePassCheckerPolicyError("contract_error must be a non-blank string.")
    return (
        "Repair instruction:\n"
        "Your previous checker output violated the output contract.\n"
        f"Contract violation: {contract_error.strip()}\n"
        f"Allowed target_key: {pack.request.target_key}\n"
        "Allowed event_ids exact array: "
        f"{json.dumps(list(pack.request.event_ids), ensure_ascii=False)}\n"
        f"Active evidence event_id: {pack.evidence.event_id}\n"
        "Do not reuse event ids mentioned in research memory, prior evidence, or "
        "your previous invalid output.\n"
        "Return one corrected JSON object only. Keep the routing decision "
        "conservative; if uncertain, choose escalate.\n\n"
        "Previous invalid output:\n"
        f"{previous_response_text.strip()}\n\n"
        f"{build_single_pass_checker_prompt(pack)}"
    )


def _checker_role_prompt() -> str:
    return (
        "Role:\n"
        "You are the market attention gate for event-trader.\n"
        "Given one admitted evidence item and one deterministic bounded context "
        "pack, decide whether that evidence deserves analysis attention now.\n\n"
    )


def _checker_operating_boundary_prompt() -> str:
    return (
        "Operating boundary:\n"
        "- You are not the research analyst.\n"
        "- You are not the portfolio manager.\n"
        "- Your job is routing attention, not forming a thesis or trade.\n"
        "- When the bounded context is incomplete or ambiguous, route to "
        "analysis rather than forcing a quiet drop.\n\n"
    )


def parse_single_pass_checker_output(
    response_text: str,
    *,
    pack: CheckerContextPack,
) -> RawCheckerDecision:
    if not isinstance(response_text, str) or not response_text.strip():
        raise SinglePassCheckerPolicyError("checker LLM response must not be blank.")
    payload = _load_single_json_object_response(response_text)
    if not isinstance(payload, dict):
        raise SinglePassCheckerPolicyError(
            "single-pass checker response must be a JSON object."
        )
    required = {
        "target_key",
        "event_ids",
        "decision",
        "rationale",
        "attention_hint",
        "requires_watchlist_maintenance",
        "uncertainty",
        "possible_watchlist_trigger",
        "no_action_support",
    }
    missing = sorted(required - set(payload))
    unexpected = sorted(set(payload) - required)
    if missing or unexpected:
        problems: list[str] = []
        if missing:
            problems.append(f"missing fields: {', '.join(missing)}")
        if unexpected:
            problems.append(f"unexpected fields: {', '.join(unexpected)}")
        raise SinglePassCheckerPolicyError("; ".join(problems))

    target_key = _require_string(payload["target_key"], field_name="target_key")
    if target_key != pack.request.target_key:
        raise SinglePassCheckerPolicyError("target_key must match context pack.")
    event_ids = _require_string_list(payload["event_ids"], field_name="event_ids")
    if event_ids != list(pack.request.event_ids):
        raise SinglePassCheckerPolicyError("event_ids must match context pack.")
    decision = _require_string(payload["decision"], field_name="decision")
    if decision not in {"no_action", "escalate"}:
        raise SinglePassCheckerPolicyError("decision must be no_action or escalate.")

    try:
        return RawCheckerDecision(
            target_key=target_key,
            event_ids=event_ids,
            decision=cast(Any, decision),
            rationale=_require_string(payload["rationale"], field_name="rationale"),
            attention_hint=_require_optional_string(
                payload["attention_hint"],
                field_name="attention_hint",
            ),
            requires_watchlist_maintenance=_require_bool(
                payload["requires_watchlist_maintenance"],
                field_name="requires_watchlist_maintenance",
            ),
            uncertainty=_require_bool(payload["uncertainty"], field_name="uncertainty"),
            possible_watchlist_trigger=_require_bool(
                payload["possible_watchlist_trigger"],
                field_name="possible_watchlist_trigger",
            ),
            no_action_support=_parse_no_action_support(payload["no_action_support"]),
        )
    except CheckerExecutionError as exc:
        raise SinglePassCheckerPolicyError(str(exc)) from exc


def _load_single_json_object_response(
    response_text: str,
) -> object:
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass
    extracted = _extract_single_json_object(response_text)
    if extracted is not None:
        return json.loads(extracted)
    raise SinglePassCheckerPolicyError(
        "checker LLM response was not a single JSON object."
    )


def _extract_single_json_object(response_text: str) -> str | None:
    candidates: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escape = False
    for index, char in enumerate(response_text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
            continue
        if char != "}":
            continue
        if depth == 0:
            return None
        depth -= 1
        if depth == 0 and start is not None:
            candidate = response_text[start : index + 1]
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                start = None
                continue
            if isinstance(payload, dict):
                candidates.append(candidate)
            start = None
    if depth != 0 or in_string:
        return None
    return candidates[0] if len(candidates) == 1 else None


def _request_chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    reasoning_effort: str | None,
) -> tuple[str, LLMUsageReceipt]:
    payload: dict[str, object] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Return only valid JSON. Do not use tools.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 2048,
    }
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = _chat_completions_url(base_url)
    raw_text: str | None = None
    last_error: BaseException | None = None
    response_payload: object | None = None
    for attempt in range(1, _CHECKER_LLM_MAX_ATTEMPTS + 1):
        request = Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "event-trader-checker/0.1",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=90) as response:
                raw_text = response.read().decode("utf-8")
            if raw_text is None:
                raise SinglePassCheckerPolicyError(
                    "single-pass checker LLM response was empty."
                )
            try:
                response_payload = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                last_error = exc
                if attempt == _CHECKER_LLM_MAX_ATTEMPTS:
                    break
                sleep(_CHECKER_LLM_RETRY_SLEEP_SECONDS)
                continue
            try:
                content = _extract_chat_message_content(response_payload)
            except SinglePassCheckerPolicyError as exc:
                last_error = exc
                if not _is_retryable_chat_response_error(exc):
                    raise
                if attempt == _CHECKER_LLM_MAX_ATTEMPTS:
                    raw_text = _blank_checker_fallback_text()
                    break
                sleep(_CHECKER_LLM_RETRY_SLEEP_SECONDS)
                continue
            return content.strip(), _extract_llm_usage(
                payload=response_payload,
                provider="openai",
                model=model,
            )
        except urllib.error.HTTPError as exc:
            error_text = exc.read().decode("utf-8", errors="replace")
            if not _is_retryable_http_status(exc.code) or attempt == _CHECKER_LLM_MAX_ATTEMPTS:
                raise SinglePassCheckerPolicyError(
                    "single-pass checker LLM request failed: "
                    f"http_status={exc.code} attempts={attempt} body={error_text[:500]}"
                ) from exc
            last_error = exc
            sleep(_CHECKER_LLM_RETRY_SLEEP_SECONDS)
        except (urllib.error.URLError, OSError) as exc:
            if attempt == _CHECKER_LLM_MAX_ATTEMPTS:
                raise SinglePassCheckerPolicyError(
                    "single-pass checker LLM request failed after "
                    f"{attempt} attempts: {exc}"
                ) from exc
            last_error = exc
            sleep(_CHECKER_LLM_RETRY_SLEEP_SECONDS)
    if raw_text is None:
        raise SinglePassCheckerPolicyError(
            "single-pass checker LLM request failed after "
            f"{_CHECKER_LLM_MAX_ATTEMPTS} attempts: {last_error}"
        )
    if response_payload is None:
        raise SinglePassCheckerPolicyError(
            "single-pass checker LLM response was not valid JSON."
        )
    return raw_text, _extract_llm_usage(
        payload=response_payload,
        provider="openai",
        model=model,
    )


def _extract_llm_usage(
    *,
    payload: object,
    provider: str,
    model: str,
) -> LLMUsageReceipt:
    if not isinstance(payload, dict):
        return LLMUsageReceipt.unavailable(
            agent_role="checker",
            provider=provider,
            model=model,
        )
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return LLMUsageReceipt.unavailable(
            agent_role="checker",
            provider=provider,
            model=model,
        )
    input_tokens = _optional_non_negative_int(
        usage.get("prompt_tokens"),
        "usage.prompt_tokens",
    )
    output_tokens = _optional_non_negative_int(
        usage.get("completion_tokens"),
        "usage.completion_tokens",
    )
    if input_tokens is None or output_tokens is None:
        return LLMUsageReceipt.unavailable(
            agent_role="checker",
            provider=provider,
            model=model,
        )
    return LLMUsageReceipt(
        agent_role="checker",
        usage_source="provider_reported",
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _is_retryable_chat_response_error(exc: SinglePassCheckerPolicyError) -> bool:
    return str(exc) == "chat completion message content is blank."


def _blank_checker_fallback_text() -> str:
    return "checker LLM response was blank after retries"


def _chat_completions_url(base_url: str) -> str:
    normalized = _require_non_blank(base_url, field_name="llm_base_url").rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _extract_chat_message_content(payload: object) -> str:
    if not isinstance(payload, dict):
        raise SinglePassCheckerPolicyError("chat completion response must be an object.")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise SinglePassCheckerPolicyError("chat completion response has no choices.")
    first = choices[0]
    if not isinstance(first, dict):
        raise SinglePassCheckerPolicyError("chat completion choice must be an object.")
    message = first.get("message")
    if not isinstance(message, dict):
        raise SinglePassCheckerPolicyError("chat completion choice has no message.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise SinglePassCheckerPolicyError("chat completion message content is blank.")
    return content


def _parse_no_action_support(value: object) -> NoActionSupport | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SinglePassCheckerPolicyError("no_action_support must be object or null.")
    required = {"support_type", "supporting_sections", "support_summary"}
    if set(value) != required:
        raise SinglePassCheckerPolicyError(
            "no_action_support must contain support_type, supporting_sections, support_summary."
        )
    return NoActionSupport(
        support_type=cast(
            Any,
            _require_string(value["support_type"], field_name="support_type"),
        ),
        supporting_sections=tuple(
            _require_string_list(
                value["supporting_sections"],
                field_name="supporting_sections",
            )
        ),
        support_summary=_require_string(
            value["support_summary"],
            field_name="support_summary",
        ),
    )


def _require_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SinglePassCheckerPolicyError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _optional_non_negative_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SinglePassCheckerPolicyError(f"{field_name} must be non-negative.")
    return value


def _require_optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, field_name=field_name)


def _require_string_list(value: object, *, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise SinglePassCheckerPolicyError(
            f"{field_name} must be an array of non-blank strings."
        )
    return [item.strip() for item in value]


def _require_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise SinglePassCheckerPolicyError(f"{field_name} must be a boolean.")
    return value


def _require_non_blank(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SinglePassCheckerPolicyError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _build_contract_violation_escalate(
    *,
    pack: CheckerContextPack,
    reason: str,
    llm_usage: LLMUsageReceipt | None,
) -> RawCheckerDecision:
    return RawCheckerDecision(
        target_key=pack.request.target_key,
        event_ids=list(pack.request.event_ids),
        decision="escalate",
        rationale=(
            "Checker contract repair escalated because the checker output violated "
            f"the target/event identity contract after bounded retries: {reason}"
        ),
        attention_hint=(
            "Analyze the admitted evidence because checker output could not satisfy "
            "the strict target/event identity contract."
        ),
        requires_watchlist_maintenance=False,
        uncertainty=False,
        possible_watchlist_trigger=False,
        no_action_support=None,
        llm_usage=llm_usage,
    )


def _combine_llm_usage_receipts(
    receipts: tuple[LLMUsageReceipt, ...],
) -> LLMUsageReceipt | None:
    if not receipts:
        return None
    if any(receipt.usage_source != "provider_reported" for receipt in receipts):
        first = receipts[0]
        return LLMUsageReceipt.unavailable(
            agent_role="checker",
            provider=first.provider,
            model=first.model,
        )
    first = receipts[0]
    if any(
        receipt.provider != first.provider or receipt.model != first.model
        for receipt in receipts
    ):
        return LLMUsageReceipt.unavailable(agent_role="checker")
    return LLMUsageReceipt(
        agent_role="checker",
        usage_source="provider_reported",
        provider=first.provider,
        model=first.model,
        input_tokens=sum(receipt.input_tokens for receipt in receipts),
        output_tokens=sum(receipt.output_tokens for receipt in receipts),
        total_tokens=sum(receipt.total_tokens for receipt in receipts),
    )


def _checker_attempt_payload(
    *,
    attempt_number: int,
    prompt_kind: str,
    prompt: str,
    prompt_sha256: str,
    response_text: str | None,
    llm_usage: LLMUsageReceipt | None,
    parse_status: str,
    error: str | None,
) -> dict[str, object]:
    return {
        "attempt_number": attempt_number,
        "prompt_kind": prompt_kind,
        "prompt": prompt,
        "prompt_sha256": prompt_sha256,
        "prompt_char_count": len(prompt),
        "response_text": response_text,
        "response_sha256": (
            None
            if response_text is None
            else sha256(response_text.encode("utf-8")).hexdigest()
        ),
        "response_char_count": (0 if response_text is None else len(response_text)),
        "llm_usage": None if llm_usage is None else llm_usage.to_json_payload(),
        "parse_status": parse_status,
        "error": error,
    }


def _write_checker_attempt_log(
    *,
    log_dir: Path,
    pack: CheckerContextPack,
    attempts: list[dict[str, object]],
    final_status: str,
    final_decision: RawCheckerDecision | None,
) -> None:
    path = _checker_attempt_log_path(log_dir=log_dir, pack=pack)
    payload = {
        "checker_policy": CHECKER_POLICY_NAME,
        "checker_policy_version": CHECKER_POLICY_VERSION,
        "target_key": pack.request.target_key,
        "event_id": pack.evidence.event_id,
        "request_event_ids": list(pack.request.event_ids),
        "business_at": pack.evidence.ts_event.isoformat(),
        "context_pack_hash": pack.context_pack_hash,
        "context_pack_schema": pack.context_pack_schema,
        "final_status": final_status,
        "final_decision": (
            None if final_decision is None else _serialize_raw_checker_decision(final_decision)
        ),
        "attempts": attempts,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _checker_attempt_log_path(*, log_dir: Path, pack: CheckerContextPack) -> Path:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H-%M-%S-%fZ")
    event_id = _safe_path_token(pack.evidence.event_id)
    target_key = _safe_path_token(pack.request.target_key)
    return log_dir / f"task_checker_{target_key}_{event_id}_{stamp}.json"


def _safe_path_token(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in {"-", "_"} else "-"
        for char in value.strip()
    )


def _serialize_raw_checker_decision(
    decision: RawCheckerDecision,
) -> dict[str, object]:
    support = decision.no_action_support
    return {
        "target_key": decision.target_key,
        "event_ids": list(decision.event_ids),
        "decision": decision.decision,
        "rationale": decision.rationale,
        "attention_hint": decision.attention_hint,
        "requires_watchlist_maintenance": decision.requires_watchlist_maintenance,
        "uncertainty": decision.uncertainty,
        "possible_watchlist_trigger": decision.possible_watchlist_trigger,
        "operational_status": decision.operational_status,
        "operational_detail": decision.operational_detail,
        "no_action_support": (
            None
            if support is None
            else {
                "support_type": support.support_type,
                "supporting_sections": list(support.supporting_sections),
                "support_summary": support.support_summary,
            }
        ),
        "llm_usage": (
            None if decision.llm_usage is None else decision.llm_usage.to_json_payload()
        ),
    }


__all__ = [
    "CHECKER_POLICY_NAME",
    "CHECKER_POLICY_VERSION",
    "SinglePassCheckerPolicy",
    "SinglePassCheckerPolicyError",
    "build_checker_contract_repair_prompt",
    "build_single_pass_checker_prompt",
    "parse_single_pass_checker_output",
]
