"""Subprocess worker for one supervised MiroThinker analysis vendor run."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path

from event_trader.integrations.analysis_mcp_transport import (
    ANALYSIS_MCP_SERVER_MODULE,
    ANALYSIS_MCP_SERVER_NAME,
)
from event_trader.integrations.mirothinker_analysis import (
    _build_analysis_tool_manager,
    _install_analysis_write_failure_budget_guard,
    _load_vendor_runtime,
    _reset_analysis_write_failure_budget_guard,
)
from event_trader.integrations.mirothinker_llm_config import (
    build_mirothinker_llm_config,
)
from event_trader.integrations.strict_mirothinker_agent import (
    StrictMiroThinkerAgentContract,
    raise_on_mirothinker_limit_failure,
    run_strict_mirothinker_agent,
)
from event_trader.reasoning.analysis_agent import ANALYSIS_AGENT_CONTRACT
from event_trader.reasoning.analysis_contract_repair import (
    AnalysisContractRepairRequired,
    AnalysisWriteFailureBudgetExceeded,
    analysis_contract_repair_supervision_payload,
)


class AnalysisWorkerError(RuntimeError):
    """Raised when the supervised analysis worker cannot run safely."""


_MIROTHINKER_ANALYSIS_AGENT_CONTRACT = StrictMiroThinkerAgentContract(
    agent_name="MiroThinker analysis agent",
    required_tools=tuple(
        (ANALYSIS_MCP_SERVER_NAME, tool_name)
        for tool_name in ANALYSIS_AGENT_CONTRACT.required_tool_names
    ),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m event_trader.integrations.mirothinker_analysis_worker",
        description="Run one vendor MiroThinker analysis task under parent supervision.",
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output_path = Path(args.output).resolve(strict=False)
    try:
        payload = _load_payload(Path(args.input).resolve(strict=False))
        run_result = asyncio.run(_run_worker(payload))
    except AnalysisContractRepairRequired as exc:
        output = analysis_contract_repair_supervision_payload(exc)
        output["traceback"] = traceback.format_exc()
        _write_output(output_path, output)
        return 1
    except AnalysisWriteFailureBudgetExceeded as exc:
        _write_output(
            output_path,
            {
                "status": "analysis_write_failure_budget_exceeded",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            },
        )
        return 1
    except BaseException as exc:
        _write_output(
            output_path,
            {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            },
        )
        return 1
    _write_output(output_path, {"status": "success", "run_result": run_result})
    return 0


async def _run_worker(payload: Mapping[str, object]) -> dict[str, object]:
    vendor_root = _require_path(payload, "vendor_root")
    log_dir = _require_path(payload, "log_dir")
    llm_api_key_env = _require_text(payload, "llm_api_key_env")
    llm_api_key = os.environ.get(llm_api_key_env)
    if not isinstance(llm_api_key, str) or not llm_api_key.strip():
        raise AnalysisWorkerError(f"Missing required worker env var {llm_api_key_env}.")
    (
        execute_task_pipeline,
        OutputFormatter,
        ToolManager,
        StdioServerParameters,
        OmegaConf,
    ) = _load_vendor_runtime(vendor_root)
    agent_cfg = OmegaConf.create(
        {
            "project_name": "event-trader",
            "debug_dir": str(log_dir),
            "llm": build_mirothinker_llm_config(
                provider=_require_text(payload, "llm_provider"),
                model_name=_require_text(payload, "llm_model_name"),
                api_key=llm_api_key,
                base_url=_require_text(payload, "llm_base_url"),
                temperature=0.2,
                max_tokens=4096,
                max_context_length=_require_positive_int(payload, "llm_max_context_length"),
                reasoning_effort=_optional_text(payload, "llm_reasoning_effort"),
            ),
            "agent": {
                "main_agent": {
                    "tools": [],
                    "tool_blacklist": [],
                    "max_turns": 200,
                },
                "sub_agents": {},
                "keep_tool_result": 5,
                "context_compress_limit": 0,
            },
            "benchmark": {},
        }
    )
    analysis_server_env = _require_string_mapping(payload, "analysis_server_env")
    server_config = {
        "name": ANALYSIS_MCP_SERVER_NAME,
        "params": StdioServerParameters(
            command=sys.executable,
            args=["-m", ANALYSIS_MCP_SERVER_MODULE],
            env=dict(analysis_server_env),
        ),
    }
    tool_manager = _build_analysis_tool_manager(
        tool_manager_cls=ToolManager,
        server_config=server_config,
    )
    _install_analysis_write_failure_budget_guard(tool_manager=tool_manager)
    _reset_analysis_write_failure_budget_guard(tool_manager=tool_manager)
    output_formatter = OutputFormatter()
    try:
        run_result = await run_strict_mirothinker_agent(
            contract=_MIROTHINKER_ANALYSIS_AGENT_CONTRACT,
            tool_manager=tool_manager,
            execute_task_pipeline=execute_task_pipeline,
            cfg=agent_cfg,
            task_id=_require_text(payload, "task_id"),
            task_description=_require_text(payload, "task_description"),
            task_file_name="",
            sub_agent_tool_managers={},
            output_formatter=output_formatter,
            log_dir=log_dir,
            error_factory=AnalysisWorkerError,
        )
        raise_on_mirothinker_limit_failure(
            log_file_path=run_result.log_file_path,
            error_factory=AnalysisWorkerError,
            agent_name=_MIROTHINKER_ANALYSIS_AGENT_CONTRACT.agent_name,
        )
        return {
            "final_summary": run_result.final_summary,
            "final_boxed_answer": run_result.final_boxed_answer,
            "log_file_path": run_result.log_file_path,
            "failure_experience_summary": run_result.failure_experience_summary,
        }
    finally:
        close_tool_manager = getattr(tool_manager, "aclose", None)
        if callable(close_tool_manager):
            await close_tool_manager()


def _load_payload(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisWorkerError(f"Invalid worker input {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AnalysisWorkerError("Worker input must be a JSON object.")
    return payload


def _write_output(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        suffix=".tmp",
    ) as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _require_path(payload: Mapping[str, object], field_name: str) -> Path:
    return Path(_require_text(payload, field_name)).resolve(strict=False)


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise AnalysisWorkerError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _require_positive_int(payload: Mapping[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AnalysisWorkerError(f"{field_name} must be a positive integer.")
    return value


def _optional_text(payload: Mapping[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnalysisWorkerError(f"{field_name} must be a string when provided.")
    normalized = value.strip()
    return normalized or None


def _require_string_mapping(
    payload: Mapping[str, object],
    field_name: str,
) -> dict[str, str]:
    value = payload.get(field_name)
    if not isinstance(value, dict):
        raise AnalysisWorkerError(f"{field_name} must be a JSON object.")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise AnalysisWorkerError(f"{field_name} must contain only string keys/values.")
        result[key] = item
    return result


if __name__ == "__main__":
    raise SystemExit(main())
