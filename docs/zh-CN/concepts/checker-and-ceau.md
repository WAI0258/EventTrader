# Checker 与 CEAU

[文档目录](../index.md) · [English](../../en/concepts/checker-and-ceau.md)

Checker 判断新证据在当前研究背景下是否值得深入分析；CEAU 再形成有边界的分析单元，避免把每条到达都当成独立 deep-research 请求。

## Checker

Single-pass 路径由证据、研究摘录和近期 timeline 构造有上限的 context pack，记录结构化决策，校验后升级。配置控制摘录／pack 大小，以及上下文覆盖不足时是否升级。

上下文不足可能是升级理由；证据接纳本身不要求新分析。Checker 回执让这个 gate 可观察。

## CEAU 单元形成

Continuous Event-to-Analysis Unit 结合 route policy、unit scope、证据时间、市场／反应要求和预算。Coordinator／store 记录把 admitted evidence 关联到 emitted unit 和 execution fence。Live／replay 使用不同的时钟和完整性来源。

Unit 可以因事件时间成熟、配置触发、上下文预算、处理时间安全关闭或 replay 最终 drain 而结束，这些原因不能混为一谈：

- 处理时间安全关闭必须将事件时间完整性声明设为 false。
- 当前实现禁止 web-search news 声称 `complete_through`。
- 最终 drain 是 replay 结束操作，不证明 live 来源完整。

## 设计收益

相关到达可以共享分析上下文，减少重复调用，同时保留重要触发。保存的一周消融报告了成功 Analysis 的运行时间与 token 降幅，不表示所有工作负载都有同样收益。

分析没触发时，检查 Checker 决策、policy hash／version、unit 成员、关闭原因与 dispatch 状态。不要直接降低全部等待阈值：reaction／context 要求可能正是单元存在的原因。

继续阅读 [Analysis](analysis-and-context.md)、[时间可见性](temporal-visibility.md)和[评估](../development/evaluation.md)。

## Route 不是一套通用定时器

已核查的黄金 replay 示例有明确 lane：

| Lane | Candidate／默认行为 | 示例上限 |
| --- | --- | --- |
| `operator_interrupt` | emit | 0 个 delay bar |
| `risk_interrupt` | emit | 0 个 delay bar |
| `news_interrupt` | emit | 0 个 delay bar |
| `context_window` | accumulate | 6 个 bar、12,000 字符 |
| `episode_append` | accumulate | 6 个 bar、12,000 字符 |

这些是示例 policy，不是全局默认，也不表示每个 unit 固定等待 30 分钟。Routing 和 completeness requirement 仍决定怎样 emit；context-budget 关闭可以先发出已有 unit，再追加新事件。

示例 policy 标为 `candidate`，并关闭 processing-time safety emission。解释运行时保留 policy hash／status，不应把 candidate 配置当作已定论研究。

## 失败行为与记录

Single-pass Checker 不进行交互式工具循环，最多初次尝试加两次契约修复。畸形输出修复耗尽后转换为保守 escalation；请求／传输失败则记录并抛出，不等于被接受的 no-action。

Checker receipt 保留 event／target 身份、rationale、validator action、decision-episode 身份以及是否形成 Analysis 请求，位于 `runtime/checker_decisions/<target>/YYYY-MM.jsonl`。

CEAU 日志位于 `runtime/ceau/`，区分 route、unit 开启／追加／发出、watermark／completeness observation，以及 Analysis queued／started／completed／failed。已 emitted 不等于分析成功；排查停滞 unit 时分别看形成、调度和完成。

## 实现依据

- [code/src/event_trader/checker](../../../code/src/event_trader/checker)
- [code/src/event_trader/ceau/coordinator.py](../../../code/src/event_trader/ceau/coordinator.py)
- [code/src/event_trader/ceau/contracts.py](../../../code/src/event_trader/ceau/contracts.py)
- [code/src/event_trader/ceau/policy.py](../../../code/src/event_trader/ceau/policy.py)

[文档目录](../index.md) · [English](../../en/concepts/checker-and-ceau.md)
