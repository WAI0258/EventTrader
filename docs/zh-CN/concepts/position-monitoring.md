# 持仓监控与 Episodes

[文档目录](../index.md) · [English](../../en/concepts/position-monitoring.md)

研究到达之间也需要审查仓位。Monitoring 结合实际敞口、active price role、行情和 cooldown／rearm policy，决定开放仓位何时再次召集 PM。

## 分开理解触发器

Analysis-to-PM 路由与开放仓位 review gate 是不同机制：前者来自研究 outcome／reason，后者依据已有仓位与行情／角色条件。

价格角色语义和 instrument basis 很重要。沿用的 stop 或 target 不证明新证据事件发生时穿越了它。应查相关 bar 路径、role identity 和 review reason，不能只看模型声称“touched”。

Cooldown／rearm 约束重复审查，但不保证等待期间没有重要行情变化。

## Episodes 与表现

Validation 的 state-change 记录将执行操作连接为 episode 产物。开放／关闭 episode、snapshot、market mapping、linkage check 和 performance window 是同一历史的不同视图。

精选报告区间开始前可能已有仓位，不能在曲线第一点虚构入场。解释胜率或图表时区分持仓分段收益、整段 episode 收益和完整窗口权益收益。

PM 使用执行关联状态，Direct 使用研究映射。Counterfactual 回答的是另一个问题；有利 excursion 或局部峰值不等于已执行利润。

## 排查

检查 active exposure、相关已执行 state change、价格角色和基准、gate／cooldown 状态、PM 请求以及后续执行。缺少或陈旧行情区间应明确覆盖情况，不靠叙述填补。

通过 [PM Console](../guides/pm-console.md)查看 episode，统计约定见[评估](../development/evaluation.md)。

## 分开读曲线、标记与状态

| 对象 | 表示什么 |
| --- | --- |
| `pm_pipeline` | 关联 PM 执行的记录收益 |
| `analysis_direct` | Analysis assessment 的假设仓位映射 |
| `buy_hold` | 行情基准 |
| PM／Analysis marker | 决策或 assessment 注释 |
| Portfolio snapshot | 当前状态、权重和来源决策／执行引用 |

状态标签区分 `flat`、`weak_long`、`strong_long`、`weak_short`、`strong_short`。Snapshot 的来源引用解释状态如何形成；单独一个 marker 不是执行回执。

Position-review gate 记录允许或跳过工作的原因，包括首次／重新激活触发、cooldown 生效和 risk-trigger bypass。Cooldown 与 rearm 检查约束重复复核请求，同时保留明确的风险通道。诊断反复止损／目标复核时，检查触及 level／bar 的引用和 gate 原因。

跨过报告起点的持仓段属于 carried-in segment。可以报告其可见贡献，但不能虚构原始入场价，也不能把每个正收益段都称为已完成盈利交易。

## 实现依据

- [code/src/event_trader/portfolio/active_exposure.py](../../../code/src/event_trader/portfolio/active_exposure.py)
- [code/src/event_trader/pm_review/position_review_gate.py](../../../code/src/event_trader/pm_review/position_review_gate.py)
- [code/src/event_trader/position_monitoring](../../../code/src/event_trader/position_monitoring)
- [code/src/event_trader/validation](../../../code/src/event_trader/validation)

[文档目录](../index.md) · [English](../../en/concepts/position-monitoring.md)
