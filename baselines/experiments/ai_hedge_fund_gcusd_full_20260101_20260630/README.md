# AHF GCUSD Baseline: Canonical Record

这是 GCUSD 黄金 AHF baseline（`2026-01-01` 至 `2026-06-30`）的唯一决策档案。主评估只读取本目录的原始 AHF 审计，不读取任何重跑 PM 的产物。

## 报告口径

```text
原始 AHF action -> 原始 native post-trade state (long / flat / short)
                 -> 统一执行 (+100% / 0% / -100%)
```

不重跑 LLM；只把原生仓位状态放入统一的全仓记账层。这样 `sell` 仍可表示平多、`cover` 仍可表示平空，不会被错误翻译成相反方向的新信号。

| 当前主结果 | 数值 |
| --- | ---: |
| 原始决策审计 | 129 |
| 已填充且对齐的决策 | 128 |
| 净收益（含费用） | +3.46% |
| 最终权益 | $103,458.02 |
| 最大回撤 | -19.52% |
| 成本覆盖 | 0.062 bps / executed side |
| 调仓订单 | 126 |

| 原始 AHF 运行总时长 | 5,937.98 秒（1小时38分58秒） |
| 校准 token 估算 | 约 467,060 tokens（772 calls） |
| 校准 LLM 成本估算 | 约 $2.32 |

## Token / Cost Calibration

原始 129 份 Gold 审计均未收到 provider usage，因此 `cost_report.json` 的原始 `tokens_* = 0` 与 `usage_observed = false` 保持不变。

Token / cost 使用 MiniMax-M2.1 在 `https://model1.imfan.top/v1` 的网页账单 probe 校准：20 次请求合计 12,100 tokens、每次 $0.003。映射至本次 Gold 实际审计的 772 次 LLM calls：`12,100 / 20 × 772 = 467,060 tokens`，成本为 `$0.003 × 772 = $2.316`（表中按 $2.32 展示）。

## Canonical Paths

| 内容 | 路径 |
| --- | --- |
| 原始 AHF 审计 | `./YYYY-MM-DD/audit.json` |
| 日线输入 | `../../gold_futures/experiment_gcusd_inputs/project_inputs/ai-hedge-fund/gcusd_daily_ohlcv_20250630_20260630.csv` |
| 5 分钟执行价格 | `../../gold_futures/experiment_gcusd_inputs/project_inputs/event_trader/gcusd_5min_20250630_20260630.csv` |
| 冻结新闻 | `../../.local/ahf_gold_gcusd_20260101_20260630_processed_news_by_day` |
| 主评估产物 | `../gold_baseline_eval_20260101_20260630` |
| 内部映射复核 | [MAPPING_REVIEW.md](MAPPING_REVIEW.md) |

决策发生在每日 EOD；成交使用其后的下一根可用 GCUSD 5 分钟 bar 开盘价。26 个无原始决策的周日沿用前一日仓位；最后一天有一条未成交决定。

## Rebuild Main Evaluation

```powershell
Set-Location baselines
.\ai-hedge-fund\.venv\Scripts\python.exe .\scripts\evaluate_gold_baselines.py
.\ai-hedge-fund\.venv\Scripts\python.exe .\scripts\render_eventtrader_vs_baselines_progress.py --end-date 2026-06-30
```

这两个命令只读取完成的审计并重建表、CSV 和 SVG，不调用 LLM。

## Do Not Use

- AMA same-day PAI 直接套入本 EOD 审计会泄漏当日已实现收益。
- 本轮曾使用的 re-prompted-PM / common-execution 版本不是原生 AHF，也不是 AMA，已清除。
