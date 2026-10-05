"""Build the compact, review-facing TradingAgents reproduction evidence package."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import zipfile
from collections import Counter
from pathlib import Path


HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
AAPL_RUN = HERE / "runs" / "aapl_minimax_m21_2024q1"
TSLA_RUN = HERE / "runs" / "tsla_minimax_m21_google_rss_sec_ama_2025q3"
PACKAGE = HERE / "review_package"
ZIP_PATH = HERE / "tradingagents_reproduction_review_package.zip"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _json(path: Path, value: object) -> None:
    _write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _aapl_summary() -> dict:
    audits = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(AAPL_RUN.glob("2024-*/audit.json"))]
    execution: dict[str, dict] = {}
    for path in sorted((AAPL_RUN / "execution").glob("*/summary.json")):
        execution[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
    return {
        "run_path": str(AAPL_RUN),
        "sessions": len(audits),
        "ratings": dict(Counter(item["decision"] for item in audits)),
        "selected_analysts": audits[0]["selected_analysts"],
        "omitted_original_input": audits[0]["omitted_original_input"],
        "execution_sensitivities": execution,
    }


def _data_file(path: Path, **extra: object) -> dict:
    return {"path": str(path), "sha256": _sha256(path), "bytes": path.stat().st_size, **extra}


def _zip_package() -> None:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in PACKAGE.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(HERE))


def main() -> int:
    tsla_execution = TSLA_RUN / "execution_ama"
    mapping_path = tsla_execution / "mapping_summary.json"
    mapping_audit = tsla_execution / "mapping_audit.csv"
    if not mapping_path.exists() or not mapping_audit.exists():
        raise RuntimeError("Run evaluate_ama_completed_run.py before building the package")

    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    aapl = _aapl_summary()
    aapl_execution = aapl["execution_sensitivities"]
    paper_window = mapping["paper_window_aug1_sep30"]
    full_window = mapping["full_window_aug1_oct24"]
    canonical_paper = paper_window["mappings"]["pm_5tier_collapsed_3way"]
    canonical_full = full_window["mappings"]["pm_5tier_collapsed_3way"]
    full_mapping_labels = [
        ("ama_official_trader_3way", "Trader 提案三档敏感性"),
        ("pm_5tier_collapsed_3way", "PM 最终五档 / AMA 论文 PAI 公式代理"),
        ("pm_extremes_only_3way", "PM 极端档才交易"),
        ("pm_5tier_long_only", "PM 五档 long-only"),
        ("pm_buy_only_long_only", "PM Buy-only long-only"),
        ("pm_stateful_long_only", "PM 状态型 long-only"),
        ("pm_stateful_long_short", "PM 状态型 long-short"),
    ]
    mapping_rows = "\n".join(
        f"| {label} | {mapping['mapping_definitions'][key]} | {full_window['mappings'][key]['cumulative_return_pct']:+.2f}% |"
        for key, label in full_mapping_labels
    )

    aapl_price = WORKSPACE / "replication" / "data" / "tradingagents_paper_2024q1" / "price" / "aapl_daily_futu_qfq_warmup_20230101_20240329.csv"
    aapl_news = next((WORKSPACE / "replication" / "data" / "tradingagents_paper_2024q1" / "news" / "paper_window").glob("aapl_news_*.jsonl"))
    tsla_price = WORKSPACE / "replication" / "data" / "tradingagents_ama_tsla_2025" / "price" / "tsla_daily_futu_qfq_20241001_20251024.csv"
    tsla_news = WORKSPACE / "replication" / "data" / "ahf_ama_tsla_2025" / "source" / "news" / "tsla_news_by_day.jsonl"

    code_sources = [
        HERE / "run_reproduction.py",
        HERE / "local_vendor.py",
        HERE / "evaluate_completed_run.py",
        HERE / "execution_adapter.py",
        HERE / "evaluate_ama_completed_run.py",
        HERE / "build_review_package.py",
    ]
    for source in code_sources:
        destination = PACKAGE / "code" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    evidence_dir = PACKAGE / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(mapping_path, evidence_dir / "tsla_ama_mapping_summary.json")
    shutil.copy2(mapping_audit, evidence_dir / "tsla_ama_mapping_audit.csv")
    _json(evidence_dir / "aapl_run_summary.json", aapl)

    _write(
        PACKAGE / "AAPL_ORIGINAL_PAPER_COMPARISON.md",
        f"""# AAPL vs. TradingAgents Original Paper

## Published target

The TradingAgents paper reports AAPL results for `2024-01-01` to `2024-03-29`: cumulative return `26.62%`, annualized return `30.50%`, Sharpe `8.21`, and maximum drawdown `0.91%`.

## Local run

- Completed sessions: `{aapl['sessions']}`
- Model: MiniMax-M2.1
- Active analysts: `market`, `news`
- Omitted original inputs: fundamentals and historical Reddit/social sentiment
- Final PM ratings: `{aapl['ratings']}`
- Frozen local price and news files are SHA-256-pinned in `integrity.json`.

## Published result and local execution sensitivities

| 口径 | 明确执行规则 | 累计收益 | Sharpe | MDD |
|---|---|---:|---:|---:|
| 原文 TradingAgents | 未公开初始仓位、仓位规模、下单时点、成本、融券/保证金规则 | +26.62% | 8.21 | -0.91% |
| 本地：Trader 直接 long-only | Trader Buy→100% 多；Sell→平仓；Hold→维持 | {aapl_execution['trader_direct_long_only']['total_return_pct']:+.2f}% | {aapl_execution['trader_direct_long_only']['annualized_sharpe']:.2f} | {aapl_execution['trader_direct_long_only']['max_drawdown_pct']:.2f}% |
| 本地：Trader 方向性敏感性 | Trader Buy→100% 多；Sell→100% 空；Hold→维持 | {aapl_execution['fixed_notional_directional']['total_return_pct']:+.2f}% | {aapl_execution['fixed_notional_directional']['annualized_sharpe']:.2f} | {aapl_execution['fixed_notional_directional']['max_drawdown_pct']:.2f}% |
| 本地：PM 文本忠实 long-only | 优先执行报告中明确的仓位、减仓、止损；无明确仓位时 Buy/Overweight→10% | {aapl_execution['text_faithful_long_only']['total_return_pct']:+.2f}% | {aapl_execution['text_faithful_long_only']['annualized_sharpe']:.2f} | {aapl_execution['text_faithful_long_only']['max_drawdown_pct']:.2f}% |
| 本地：PM 人工转录，零初始 long-only | 人工逐条映射 PM 明示仓位/止损；初始仓位 0% | {aapl_execution['manual_pm_zero_initial_long_only']['total_return_pct']:+.2f}% | {aapl_execution['manual_pm_zero_initial_long_only']['annualized_sharpe']:.2f} | {aapl_execution['manual_pm_zero_initial_long_only']['max_drawdown_pct']:.2f}% |
| 本地：PM 人工转录，零初始 long-short | 同上，但允许空头；该次没有形成净空头暴露 | {aapl_execution['manual_pm_zero_initial_long_short']['total_return_pct']:+.2f}% | {aapl_execution['manual_pm_zero_initial_long_short']['annualized_sharpe']:.2f} | {aapl_execution['manual_pm_zero_initial_long_short']['max_drawdown_pct']:.2f}% |
| 本地：PM 人工转录，满仓初始 long-only | 同上；初始仓位 100% 多 | {aapl_execution['manual_pm_full_initial_long_only']['total_return_pct']:+.2f}% | {aapl_execution['manual_pm_full_initial_long_only']['annualized_sharpe']:.2f} | {aapl_execution['manual_pm_full_initial_long_only']['max_drawdown_pct']:.2f}% |
| 本地：PM 人工转录，满仓初始 long-short | 同上，但允许空头；该次没有形成净空头暴露 | {aapl_execution['manual_pm_full_initial_long_short']['total_return_pct']:+.2f}% | {aapl_execution['manual_pm_full_initial_long_short']['annualized_sharpe']:.2f} | {aapl_execution['manual_pm_full_initial_long_short']['max_drawdown_pct']:.2f}% |

所有本地适配器均为零费用、下一交易日开盘执行，且按报告中的止损规则检查。它们不是原文执行层的替代品，而是把该执行层未披露的自由度逐一显式化。因此，AAPL 只能主张角色工作流和决策产出已复现，不能主张用任一事后适配器数值复现原文 `+26.62%`。原始决策日志和逐日执行审计仍在 `../runs/aapl_minimax_m21_2024q1`。

Reference: Xiao et al., *TradingAgents: Multi-Agents LLM Financial Trading Framework* (arXiv:2412.20138): <https://arxiv.org/pdf/2412.20138>.
""",
    )
    _write(
        PACKAGE / "TSLA_AMA_COMPARISON.md",
        f"""# TSLA vs. Agent Market Arena (AMA)

## Comparable published window: 2025-08-01 to 2025-09-30

| 口径 | 累计收益 | Sharpe | MDD |
|---|---:|---:|---:|
| 我们：AMA 论文 PAI 公式 PM 代理 | {canonical_paper['cumulative_return_pct']:+.2f}% | {canonical_paper['annualized_sharpe_rf0']:.2f} | {canonical_paper['max_drawdown_pct']:.2f}% |
| 我们：Buy & Hold（Futu） | {paper_window['buy_and_hold']['cumulative_return_pct']:+.2f}% | {paper_window['buy_and_hold']['annualized_sharpe_rf0']:.2f} | {paper_window['buy_and_hold']['max_drawdown_pct']:.2f}% |
| AMA：TradeAgent Vote | -0.36% | 0.02 | -9.03% |
| AMA：TradeAgent 各模型范围 | -38.72% ～ +21.91% | — | — |
| AMA：Buy & Hold | +46.88% | 6.00 | -6.34% |

Inputs differ: AMA uses a verified, expert-reviewed multi-source stream; the local run uses frozen Futu QFQ prices plus Google RSS / SEC news. The local row applies the daily PAI formula printed in the AMA paper after compressing the final PM rating to three actions.

The local formula-proxy result is inside AMA's reported TradeAgent backbone range, but it is **not** an AMA execution replication: AMA's public return script consumes one final `recommended_action` and applies a stateful, fee-bearing long-short simulator. The published integration does not expose whether that final field originated from a raw Trader proposal or a PM-approved adapter.

## Full local window: 2025-08-01 to 2025-10-24

The following is the complete mapping sensitivity analysis over all `{full_window['sessions']}` local sessions. It is deliberately shown beside the AMA-comparable table above: this window includes October and is not the AMA result-comparison window.

| 映射 | 规则 | 60 日收益 |
|---|---|---:|
{mapping_rows}

The canonical AMA mapping is therefore `{canonical_full['cumulative_return_pct']:+.2f}%`, Sharpe `{canonical_full['annualized_sharpe_rf0']:.2f}`, and MDD `{canonical_full['max_drawdown_pct']:.2f}%`. The per-day action and mapping audit is retained in `evidence/tsla_ama_mapping_audit.csv`.

Reference: Qian et al., *When Agents Trade: Live Multi-Market Trading Benchmark for LLM Agents* (arXiv:2510.11695): <https://arxiv.org/pdf/2510.11695>.
""",
    )
    _write(
        PACKAGE / "REVIEW_RESPONSE.md",
        """# Review-Facing Claim Boundary

This package provides a reproducible local TradingAgents baseline under frozen public inputs and explicit, audit-visible timing rules.

- The AAPL run is a partial paper-window reproduction: its role workflow is retained, but only market and news analysts are active because the corresponding public frozen inputs are available locally.
- The TSLA comparison includes a final-PM three-way proxy scored with the daily formula printed in AMA; it does not claim to reproduce AMA's public stateful execution simulator.
- Deferred reflection is disabled because it is not specified as a TradingAgents mechanism in the paper and would require a separate outcome-visibility contract in historical evaluation.
- The package does not claim identical published returns or AMA execution replication. The formula-proxy value is shown beside AMA's outcome range only as a bounded diagnostic, with all deviations disclosed.
""",
    )
    _write(
        PACKAGE / "README.md",
        """# TradingAgents Reproduction Evidence Package

This compact package is the review-facing companion to the canonical raw AAPL and TSLA runs. It follows the same pattern as the AHF package: raw daily audit receipts and frozen inputs remain outside the package, while `integrity.json` pins them by path and SHA-256.

## Evidence map

| Question | Evidence |
| --- | --- |
| AAPL versus original TradingAgents paper | `AAPL_ORIGINAL_PAPER_COMPARISON.md` |
| TSLA versus AMA TradeAgent | `TSLA_AMA_COMPARISON.md` |
| Claim boundary | `REVIEW_RESPONSE.md` |
| Full TSLA mapping results | `evidence/tsla_ama_mapping_summary.json` |
| Per-day TSLA mapping audit | `evidence/tsla_ama_mapping_audit.csv` |
| AAPL local run summary and execution sensitivities | `evidence/aapl_run_summary.json` |
| Frozen inputs, code snapshots, and run paths | `integrity.json` and `code/` |

## Canonical results

- AAPL: 61 completed local sessions; the paper's undisclosed execution semantics prevent a defensible claim of matching its published PnL.
- TSLA / AMA comparable window: the complete four-metric comparison (local, Buy & Hold, AMA Vote, AMA model range) is in `TSLA_AMA_COMPARISON.md`.
- TSLA / full local window: all seven decision-to-position mappings, their explicit rules, and their 60-day returns are in `TSLA_AMA_COMPARISON.md`; the canonical AMA mapping is `-7.87%` across 60 sessions.

## Rebuild

From `baselines`:

```powershell
tradingagents\\.venv\\Scripts\\python.exe replication\\tradingagents\\evaluate_ama_completed_run.py
tradingagents\\.venv\\Scripts\\python.exe replication\\tradingagents\\build_review_package.py
```
""",
    )

    integrity = {
        "package": "tradingagents_aapl_tsla_replication_review",
        "canonical_runs": {
            "aapl": {"path": str(AAPL_RUN), "sessions": aapl["sessions"]},
            "tsla": {"path": str(TSLA_RUN), "sessions": full_window["sessions"], "mapping_summary_sha256": _sha256(mapping_path)},
        },
        "frozen_inputs": {
            "aapl_price": _data_file(aapl_price),
            "aapl_news": _data_file(aapl_news),
            "tsla_price": _data_file(tsla_price),
            "tsla_news": _data_file(tsla_news),
        },
        "code_snapshot": {source.name: _sha256(source) for source in code_sources},
    }
    _json(PACKAGE / "integrity.json", integrity)
    _zip_package()
    print(json.dumps({"package": str(PACKAGE), "zip": str(ZIP_PATH)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
