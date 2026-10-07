# 仓位审查与执行事实

[文档目录](../index.md) · [English](../../en/concepts/portfolio-and-execution.md)

研究方向不等于要求当前仓位直接复制该方向。PM 综合实际敞口、风险、thesis、行情与 review reason，再给出仓位决策。

## 决策优先的链路

| 记录 | 含义 |
| --- | --- |
| AnalysisAssessment | 研究判断及价格／thesis 语义 |
| PM review request | 为什么应该审查仓位 |
| PMDecision | 仓位动作／目标与决策背景 |
| ExecutionIntent | 从决策确定性生成的执行意图 |
| ExecutionRecord | 执行状态、时间及含成本价格 |
| PortfolioState | 根据已执行记录更新的状态 |

PM workflow 使用有边界读取和结构化决策。契约／仓位风险校验独立于模型解释；hold、deferred 与已执行调整不同。

## 应用决策

确定性执行流程生成 intent，经 `PaperExecutionEngine` 执行，记录 attempt，只在结果为 executed 时更新仓位，然后形成 state change 并刷新 episode 产物。

执行取决于 price basis、行情覆盖、方向策略、目标成本覆盖和缺失 bar 处理。实际使用的后续 bar 可能晚于决策时间。持久化执行价格已包含配置成本，评估或 HTML 情景不能重复扣除同一成本。

## 调度与控制

自动 dispatch 与自动 execution 是不同开关，BTC／SOX 配置组合不同。显式 operator run-and-execute 是另一条路径，因此自动流程开关不能被宣传成所有执行的总锁。

公开引擎记录模拟执行，不是券商成交。Direct 是 analysis-to-position 假设映射，不是 PM 执行记录。资金费、借券费和分红需要单独说明。

敞口异常时沿 decision→intent→execution status→portfolio state 排查，不能把 thesis 建议退出改写成已退出。

## PM 读取有决策边界的仓位视图

PM tool gateway 提供 request、evidence、market bar、先前 PM history、episode memory、learning card 和 active exposure。Supplemental read 从白名单规划，每次执行读取必须返回 canonical result 和通过的 visibility receipt。

History 排除当前 request，只保留业务时间更早的记录；market read 受 request 的 event／market cutoff 限制。Active-exposure 和 episode 表面仍需自己的时间解释，不能仅凭 history filter 就声称历史状态全部重建。

Portfolio-risk 检查 bar adjustment policy 和 active instrument basis，优先使用同基准 opening execution 的入场价格／成本，否则使用支持的 active-basis bar path。这避免没有规则地混用 raw 历史入场和另一个复权基准的当前序列。

## 实例区别：Thesis 变化不等于退出

假设一次研究更新削弱看多 thesis：Analysis 可以产生 assessment 和 PM reason，PM 仍可能 hold、减仓或选择其他风险响应。Assessment 发出不是已经卖出。

报告退出需要 PMDecision、派生 intent、executed result 和更新 state。Deferred 不会因为决策要求退出就形成完成的仓位转换。

PM 决策按月份保存在 `runtime/portfolio/pm-decisions/<target>/`，当前状态为 `runtime/portfolio/state/<target>.json`，分别回答历史决策与当前状态问题。样例数值记录才是操作报告依据。

## 实现依据

- [code/src/event_trader/pm_review/workflow.py](../../../code/src/event_trader/pm_review/workflow.py)
- [code/src/event_trader/pm_review/portfolio_risk.py](../../../code/src/event_trader/pm_review/portfolio_risk.py)
- [code/src/event_trader/portfolio/pm_execution_flow.py](../../../code/src/event_trader/portfolio/pm_execution_flow.py)
- [code/src/event_trader/execution/engine.py](../../../code/src/event_trader/execution/engine.py)

[文档目录](../index.md) · [English](../../en/concepts/portfolio-and-execution.md)
