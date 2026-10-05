from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from m2_1_transport import MiniMaxOpenAITransport, USAGE_TRACKER


def _load_dotenv_if_missing(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _assert_clean_vendor(finmem_root: Path) -> str:
    git_prefix = ["git", "-c", f"safe.directory={finmem_root.as_posix()}"]
    status = subprocess.run(
        [*git_prefix, "status", "--porcelain", "--", "puppy"],
        cwd=finmem_root,
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise RuntimeError(
            "Paper-compatible runs require an unmodified vendored FinMem puppy package. "
            f"Dirty paths:\n{status.stdout.strip()}"
        )
    return subprocess.run(
        [*git_prefix, "rev-parse", "HEAD"],
        cwd=finmem_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def install_paper_compat(*, workspace_root: Path) -> str:
    """Install process-local compatibility hooks before importing the runner."""

    finmem_root = workspace_root / "FinMem-LLM-StockTrading"
    _load_dotenv_if_missing(finmem_root / ".env")
    _load_dotenv_if_missing(workspace_root / ".env")
    vendor_head = _assert_clean_vendor(finmem_root)

    embedding_key = os.environ.get("qinzhi_embed")
    if not embedding_key:
        raise RuntimeError("Missing qinzhi_embed for the original FinMem embedding client")
    os.environ["OPENAI_API_KEY"] = embedding_key
    os.environ["OPENAI_API_BASE"] = "https://qinzhiai.com/v1"

    import puppy.agent as agent_module
    import puppy.environment as environment_module

    agent_module.ChatOpenAICompatible = MiniMaxOpenAITransport

    def get_llm_usage_summary(_: Any) -> dict[str, int]:
        return USAGE_TRACKER.snapshot()

    agent_module.LLMAgent.get_llm_usage_summary = get_llm_usage_summary

    if not getattr(environment_module.MarketEnvironment, "_paper_compat_guarded", False):
        original_step = environment_module.MarketEnvironment.step

        def guarded_step(self: Any, expose_future_record: bool = True) -> Any:
            market_info = original_step(self)
            if expose_future_record or market_info[-1]:
                return market_info
            return (*market_info[:5], None, market_info[-1])

        environment_module.MarketEnvironment.step = guarded_step
        environment_module.MarketEnvironment._paper_compat_guarded = True

    return vendor_head
