# AHF Mapping Review (Internal Only)

> 本文件用于本地协议复核，不进入论文主表。

所有行使用同一批 129 个原始 AHF 审计，未重跑任何模型。原始 action 为 `buy: 43`、`sell: 32`、`short: 27`、`cover: 19`、`hold: 8`；原生 post-trade state 为 `long: 51`、`flat: 48`、`short: 30`。

| Case | Interpretation | Total return | Maximum drawdown | Use |
| --- | --- | ---: | ---: | --- |
| Native AHF ledger | 原生 quantity 账本与约 20% 风控上限 | +0.45% | -3.24% | 原生对照 |
| **Native state -> full exposure** | 原生 post-trade state 映射为 `+100% / 0% / -100%` | **+3.46% net** | **-19.52%** | **主 baseline** |
| AMA action mapping | `buy/cover -> +1`，`sell/short -> -1`，按真实执行区间计分 | -14.01% gross | -31.86% | AMA 映射诊断 |
| AMA shifted daily grid | EOD action 从下一可用日线开始 | -20.18% gross | -32.63% | 时序敏感性 |
| AMA same-day PAI | action 乘同日 close-to-close return | +108.63% gross | -8.47% | 无效：未来信息 |
| Re-prompted PM | 冻结分析但重新采样 PM target | -3.03% net | -23.06% | 拒绝：改变策略 |

结论：主 baseline 采用 **Native state -> full exposure**。它保存已完成的原生决策轨迹，只替换资本配置与记账层。
