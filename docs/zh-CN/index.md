# EventTrader 文档

[项目首页](../../README.zh-CN.md) · [English](../en/index.md)

文档对应当前公开实现，按使用、设计机制与开发职责组织。查看数值样例不需要模型凭证；完整运行使用用户自己的证据、行情与环境。

## 选择阅读路径

- 查看项目：[快速开始](guides/quickstart.md) → [Console](guides/pm-console.md) → [样例与报告](../../data/paper-trading/README.md)。
- 运行自己的标的：[安装](guides/installation.md) → [行情](guides/market-data.md) → [配置](development/configuration.md) → [Live](guides/live-running.md)／[Replay](guides/historical-replay.md) → [运维](guides/operations.md)。
- 理解设计：[工作流程](concepts/workflow.md) → [证据](concepts/evidence.md) → [CEAU](concepts/checker-and-ceau.md) → [Analysis](concepts/analysis-and-context.md) → [研究记忆](concepts/research-memory-and-thesis.md) → [PM](concepts/portfolio-and-execution.md) → [反思](concepts/reflection-and-learning.md)。
- 维护或扩展：[扩展边界](development/extensions.md) → [时间可见性](concepts/temporal-visibility.md) → [输出契约](development/agent-contracts.md) → [Harness](development/agent-harness.md) → [运行状态](development/runtime-and-state.md) → [验证](development/contracts-and-testing.md)。

## 使用指南

- [快速开始](guides/quickstart.md)
- [安装与环境](guides/installation.md)
- [使用 PM Console](guides/pm-console.md)
- [行情接入与 Buy & Hold](guides/market-data.md)
- [运行实时目标](guides/live-running.md)
- [历史回放](guides/historical-replay.md)
- [运维与恢复](guides/operations.md)

## 机制与设计

- [工作流程与设计理念](concepts/workflow.md)
- [证据接纳与事实记录](concepts/evidence.md)
- [Checker 与 CEAU](concepts/checker-and-ceau.md)
- [Analysis 与有边界上下文](concepts/analysis-and-context.md)
- [研究记忆与 Thesis 版本](concepts/research-memory-and-thesis.md)
- [仓位审查与执行事实](concepts/portfolio-and-execution.md)
- [持仓监控与 Episodes](concepts/position-monitoring.md)
- [反思与学习生命周期](concepts/reflection-and-learning.md)
- [时间可见性与回放防泄露](concepts/temporal-visibility.md)

## 开发文档

- [配置参考与示例](development/configuration.md)
- [扩展项目边界](development/extensions.md)
- [运行时所有权与持久化状态](development/runtime-and-state.md)
- [契约与验证](development/contracts-and-testing.md)
- [评估与结果来源](development/evaluation.md)
- [Agent 输出契约与 Prompt 设计](development/agent-contracts.md)
- [Agent Harness 与受控执行](development/agent-harness.md)

## 源码导航

从业务职责进入模块，再沿页面底部的实现链接追踪调用。外部组件只介绍与项目的接入边界。

| 职责 | 入口 | 相关文档 |
| --- | --- | --- |
| 组合与调度 | [composition.py](../../code/src/event_trader/composition.py) · [kernel.py](../../code/src/event_trader/kernel.py) · [runtime](../../code/src/event_trader/runtime) | [详解](concepts/workflow.md) |
| 采集、接纳与发布 | [feeds](../../code/src/event_trader/feeds) · [source_archive](../../code/src/event_trader/source_archive) · [ingest](../../code/src/event_trader/ingest) · [source_release](../../code/src/event_trader/source_release) · [source_policy.py](../../code/src/event_trader/source_policy.py) · [live_source_checkpoint.py](../../code/src/event_trader/live_source_checkpoint.py) | [详解](concepts/evidence.md) |
| Checker 与分析单元 | [checker](../../code/src/event_trader/checker) · [ceau](../../code/src/event_trader/ceau) | [详解](concepts/checker-and-ceau.md) |
| 上下文、Claim 与推理 | [context_assembly](../../code/src/event_trader/context_assembly) · [reasoning](../../code/src/event_trader/reasoning) · [analysis.py](../../code/src/event_trader/analysis.py) · [analysis_request_handler.py](../../code/src/event_trader/analysis_request_handler.py) | [详解](concepts/analysis-and-context.md) |
| 研究与决策历史 | [research_memory](../../code/src/event_trader/research_memory) · [thesis_revision](../../code/src/event_trader/thesis_revision) · [decision_memory](../../code/src/event_trader/decision_memory) · [analysis_assessment_store.py](../../code/src/event_trader/analysis_assessment_store.py) | [详解](concepts/research-memory-and-thesis.md) |
| 仓位、PM 与执行 | [pm_review](../../code/src/event_trader/pm_review) · [portfolio](../../code/src/event_trader/portfolio) · [execution](../../code/src/event_trader/execution) | [详解](concepts/portfolio-and-execution.md) |
| 监控与 Validation | [position_monitoring](../../code/src/event_trader/position_monitoring) · [validation](../../code/src/event_trader/validation) | [详解](concepts/position-monitoring.md) |
| 反思与经验 | [reflection](../../code/src/event_trader/reflection) · [episode_memory](../../code/src/event_trader/episode_memory) · [learning_cards](../../code/src/event_trader/learning_cards) · [counterfactuals](../../code/src/event_trader/counterfactuals) | [详解](concepts/reflection-and-learning.md) |
| 行情与回放 | [market](../../code/src/event_trader/market) · [live_market_data_runtime.py](../../code/src/event_trader/live_market_data_runtime.py) · [replay](../../code/src/event_trader/replay) | [详解](guides/market-data.md) |
| Console、Projection 与操作员输入 | [pm_console](../../code/src/event_trader/pm_console) · [code/frontend/pm-console/src](../../code/frontend/pm-console/src) · [projection](../../code/src/event_trader/projection) · [operator_context](../../code/src/event_trader/operator_context) · [operator](../../code/src/event_trader/operator) | [详解](guides/pm-console.md) |
| 契约、配置与外部集成 | [contracts](../../code/src/event_trader/contracts) · [config.py](../../code/src/event_trader/config.py) · [agent_implementation.py](../../code/src/event_trader/agent_implementation.py) · [integrations](../../code/src/event_trader/integrations) · [code/vendor/mirothinker](../../code/vendor/mirothinker) | [详解](development/extensions.md) |
| 存储、命令、迁移与保留 | [storage](../../code/src/event_trader/storage) · [workspace.py](../../code/src/event_trader/workspace.py) · [commands](../../code/src/event_trader/commands) · [migrations](../../code/src/event_trader/migrations) · [artifact_retention.py](../../code/src/event_trader/artifact_retention.py) · [tools](../../code/src/event_trader/tools) | [详解](guides/operations.md) |
| 审计、评估与实验工具 | [audit](../../code/src/event_trader/audit) · [evaluation](../../code/src/event_trader/evaluation) · [code/scripts](../../code/scripts) · [baselines](../../baselines) · [experiments](../../experiments) · [tools](../../tools) | [详解](development/evaluation.md) |

历史实验、统计口径与复现限制见[评估](development/evaluation.md)。两种语言保持相同主题，命令与配置标识不翻译。
