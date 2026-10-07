"""Provider-neutral control signals for bounded Analysis contract repair."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS = 3
AnalysisContractSurface = Literal["write_tool", "read_audit", "final_output"]
_ANALYSIS_CONTRACT_SURFACES = frozenset({"write_tool", "read_audit", "final_output"})


@dataclass(frozen=True, slots=True)
class AnalysisContractFailure:
    surface: AnalysisContractSurface
    error_code: str
    message: str
    recoverable: bool = True
    suggested_action: str = ""
    details: Mapping[str, object] | None = None
    arguments: object | None = None


@dataclass(frozen=True, slots=True)
class AnalysisWriteToolFailure:
    """Canonical interpretation of one failed Analysis staged-write call."""

    tool_name: str
    error_code: str
    message: str
    recoverable: bool
    arguments: object
    payload: Mapping[str, object] | None


class AnalysisContractRepairRequired(BaseException):
    """Internal stop signal for bounded no-HITL contract repair."""

    def __init__(
        self,
        *,
        failure: AnalysisContractFailure,
        failure_count: int,
        threshold: int,
    ) -> None:
        self.failure = failure
        self.failure_count = failure_count
        self.threshold = threshold
        super().__init__(
            "Analysis contract repair required "
            f"(surface={failure.surface}, error_code={failure.error_code}) "
            f"[{failure_count}/{threshold}]: "
            f"{_shorten_error_text(failure.message, max_length=180)}"
        )


class AnalysisWriteFailureBudgetExceeded(BaseException):
    """Internal stop signal that must bypass agent-runtime rollback handling."""


class AnalysisContractRepairPayloadError(ValueError):
    """Raised when a supervised repair payload is malformed."""


def parse_analysis_write_tool_failure(
    *,
    tool_name: str,
    arguments: object,
    tool_result: object,
) -> AnalysisWriteToolFailure | None:
    """Normalize direct and transport-wrapped staged-write error payloads."""

    payload = _analysis_write_tool_error_payload(tool_result)
    if payload is not None:
        message = _mapping_text(payload, "error") or "analysis write tool failed"
        error_code = _mapping_text(payload, "error_code") or "write_tool_error"
        recoverable_value = payload.get("recoverable")
        return AnalysisWriteToolFailure(
            tool_name=tool_name,
            error_code=error_code,
            message=message,
            recoverable=(recoverable_value if isinstance(recoverable_value, bool) else True),
            arguments=arguments,
            payload=payload,
        )

    failure_text = _analysis_write_tool_error_text(tool_result)
    if failure_text is None:
        return None
    return AnalysisWriteToolFailure(
        tool_name=tool_name,
        error_code="write_tool_error",
        message=failure_text,
        recoverable=True,
        arguments=arguments,
        payload=None,
    )


def analysis_contract_failure_for_write_tool(
    failure: AnalysisWriteToolFailure,
) -> AnalysisContractFailure:
    """Translate a canonical write failure into supervisor repair feedback."""

    details: dict[str, object] = {
        "tool_name": failure.tool_name,
        "arguments": failure.arguments,
        "error_code": failure.error_code,
        "error": failure.message,
        "recoverable": failure.recoverable,
    }
    if failure.payload is not None:
        details.update(failure.payload)
    return AnalysisContractFailure(
        surface="write_tool",
        error_code=failure.error_code,
        message=failure.message,
        recoverable=failure.recoverable,
        suggested_action=(
            _mapping_text(failure.payload, "suggested_action")
            if failure.payload is not None
            else "Retry only if the write-tool error is a mechanical contract issue."
        )
        or "",
        details=details,
        arguments=failure.arguments,
    )


def analysis_write_failure_key(
    failure: AnalysisWriteToolFailure,
) -> tuple[str, str, str, str]:
    """Return the stable identity used by bounded repeated-failure guards."""

    payload = failure.payload or {}
    page_path = payload.get("page_path")
    section_name = payload.get("section_name")
    return (
        failure.tool_name,
        failure.error_code,
        page_path if isinstance(page_path, str) else "",
        section_name if isinstance(section_name, str) else "",
    )


def analysis_contract_repair_supervision_payload(
    exc: AnalysisContractRepairRequired,
) -> dict[str, object]:
    failure = exc.failure
    return {
        "status": "analysis_contract_repair_required",
        "error": str(exc),
        "failure_count": exc.failure_count,
        "threshold": exc.threshold,
        "failure": {
            "surface": failure.surface,
            "error_code": failure.error_code,
            "message": failure.message,
            "recoverable": failure.recoverable,
            "suggested_action": failure.suggested_action,
            "details": failure.details,
            "arguments": failure.arguments,
        },
    }


def analysis_contract_repair_from_supervision_payload(
    payload: Mapping[str, object],
) -> AnalysisContractRepairRequired:
    failure_payload = payload.get("failure")
    if not isinstance(failure_payload, Mapping):
        raise AnalysisContractRepairPayloadError(
            "Analysis worker contract-repair payload is missing failure details."
        )
    surface = _require_failure_text(failure_payload, "surface")
    if surface not in _ANALYSIS_CONTRACT_SURFACES:
        raise AnalysisContractRepairPayloadError(
            f"Analysis worker contract-repair payload has invalid surface: {surface}"
        )
    details = failure_payload.get("details")
    return AnalysisContractRepairRequired(
        failure=AnalysisContractFailure(
            surface=cast(AnalysisContractSurface, surface),
            error_code=_require_failure_text(failure_payload, "error_code"),
            message=_require_failure_text(failure_payload, "message"),
            recoverable=_optional_failure_bool(
                failure_payload,
                "recoverable",
                default=True,
            ),
            suggested_action=_optional_failure_text(
                failure_payload,
                "suggested_action",
            ),
            details=details if isinstance(details, Mapping) else None,
            arguments=failure_payload.get("arguments"),
        ),
        failure_count=_require_int(payload, "failure_count"),
        threshold=_require_int(payload, "threshold"),
    )


def render_analysis_contract_failure(
    failure: AnalysisContractFailure,
) -> str:
    """Render the public typed failure only at an executor that owns retry prompts."""

    details = _json_compact(failure.details) if failure.details is not None else "{}"
    return (
        "Previous analysis attempt failed a deterministic project contract:\n"
        f"- surface: {failure.surface}\n"
        f"- error_code: {failure.error_code}\n"
        f"- error: {_shorten_error_text(failure.message, max_length=500)}\n"
        f"- suggested_action: {failure.suggested_action}\n"
        f"- details: {details}\n\n"
        "Repeat the same business task and correct the rejected contract without "
        "weakening evidence, grounding, write, or commit requirements."
    )


def _require_failure_text(
    payload: Mapping[str, object],
    field_name: str,
) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise AnalysisContractRepairPayloadError(
            f"Analysis worker contract-repair payload field {field_name} must be text."
        )
    return value.strip()


def _optional_failure_text(
    payload: Mapping[str, object],
    field_name: str,
) -> str:
    value = payload.get(field_name)
    return value.strip() if isinstance(value, str) else ""


def _optional_failure_bool(
    payload: Mapping[str, object],
    field_name: str,
    *,
    default: bool,
) -> bool:
    value = payload.get(field_name)
    return value if isinstance(value, bool) else default


def _analysis_write_tool_error_payload(
    tool_result: object,
) -> Mapping[str, object] | None:
    raw_result: object
    if isinstance(tool_result, Mapping):
        raw_error = tool_result.get("error")
        if isinstance(raw_error, str) and raw_error.strip():
            return {
                "error": raw_error.strip(),
                "error_code": "write_tool_error",
                "recoverable": True,
            }
        raw_result = tool_result.get("result")
    else:
        raw_result = tool_result

    if isinstance(raw_result, Mapping):
        raw_error = raw_result.get("error")
        return raw_result if isinstance(raw_error, str) and raw_error.strip() else None
    if not isinstance(raw_result, str) or not raw_result.strip():
        return None
    try:
        payload = json.loads(raw_result)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    raw_error = payload.get("error")
    return payload if isinstance(raw_error, str) and raw_error.strip() else None


def _analysis_write_tool_error_text(tool_result: object) -> str | None:
    if isinstance(tool_result, Mapping):
        error = tool_result.get("error")
        if isinstance(error, str) and error.strip():
            return f"Tool call failed: {error.strip()}"
        return _analysis_write_result_error_text(tool_result.get("result"))
    return _analysis_write_result_error_text(tool_result)


def _analysis_write_result_error_text(result: object) -> str | None:
    if isinstance(result, str):
        normalized = result.strip()
        if normalized.startswith("Error executing tool "):
            return normalized
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
        return None
    if isinstance(result, Mapping):
        error = result.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
    return None


def _mapping_text(
    payload: Mapping[str, object] | None,
    field_name: str,
) -> str | None:
    if payload is None:
        return None
    value = payload.get(field_name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _require_int(
    payload: Mapping[str, object],
    field_name: str,
) -> int:
    value = payload.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AnalysisContractRepairPayloadError(
            f"Analysis worker payload field {field_name} must be an integer."
        )
    return value


def _shorten_error_text(value: str, *, max_length: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 3]}..."


def _json_compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


__all__ = [
    "ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS",
    "AnalysisContractFailure",
    "AnalysisContractRepairPayloadError",
    "AnalysisContractRepairRequired",
    "AnalysisContractSurface",
    "AnalysisWriteToolFailure",
    "AnalysisWriteFailureBudgetExceeded",
    "analysis_contract_failure_for_write_tool",
    "analysis_contract_repair_from_supervision_payload",
    "analysis_contract_repair_supervision_payload",
    "render_analysis_contract_failure",
    "analysis_write_failure_key",
    "parse_analysis_write_tool_failure",
]
