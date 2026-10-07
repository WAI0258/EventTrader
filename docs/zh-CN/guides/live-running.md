# 运行实时目标

[文档目录](../index.md) · [English](../../en/guides/live-running.md)

Live 进程观测新证据和行情，推进配置好的研究与仓位流程。它可能调用付费模型并写入持久化状态。应使用专门配置和新的工作区，不要使用公开数值样例目录。

## 准备配置

检查主交易标的、上下文订阅、启用的 feed、搜索频率、行情凭证、workspace／log 路径与模型 endpoint。BTC 和 SOX 示例行为不同：

| 设置 | BTC 示例 | SOX 示例 |
| --- | --- | --- |
| 目标 | `btc` | `sox` |
| 主交易标的 | BTC 永续 | SOXX |
| 自动 PM 调度 | 开启 | 开启 |
| 自动模拟执行 | 关闭 | 开启 |

明确设置 `pm_review_runtime.auto_dispatch_after_analysis` 与 `execute_pm_decisions`。配置校验要求自动执行必须同时开启自动调度。这些开关控制自动流程；显式手工 run-and-execute 操作另有执行路径。

## 启动与观察

在 `code/` 查看选项：

```sh
uv run event-trader-live --help
```

准备自己的配置后，启动形式为：

```sh
uv run event-trader-live --config config/kernel.live.btc.toml
```

这条示例会访问外部服务，不能把未修改的配置当作无需凭证的演示。

依次观察来源观测、admission／Checker 回执、CEAU 调度、Analysis 结果、PM 决策和执行记录。进程运行或新闻采集成功，不代表后续阶段成功；行情新鲜度也要单独观察。

## 重启与 catch-up

Live 启动使用向前运行的 startup anchor。历史 catch-up 是单独的操作流程，有来源身份和恢复处理；重启不意味着所有漏过事件已重建。

PM readiness／preflight 和 bootstrap 可能创建状态；structured-finalizer preflight 可能访问模型 provider。先读[运维指南](operations.md)，不要把它们当作纯读取。

公开执行引擎模拟 paper 操作。实时证据采集与真实资金券商执行是不同能力。

## 用阶段完成判断是否就绪

| 边界 | 需要确认什么 |
| --- | --- |
| 采集 → admission | 归一化保留来源身份与观察时间 |
| Checker → CEAU | 记录的 disposition 能解释过滤或累积 |
| CEAU → Analysis | dispatch 与接受的 outcome 对应相同标的／工作 |
| Analysis → PM | 请求具有明确复核原因 |
| PM → execution | 接受的执行结果改变组合状态 |

每个标的／运行使用独立工作区。接入价格前检查主交易品种与上下文品种：提供研究背景的 symbol 不自动成为执行价格基准。在自己的配置下确认这些边界后，再开启自动 dispatch 与模拟执行。

阶段停滞时，保留 task／event 标识和最后一条持久记录。沿[运维指南](operations.md)定位第一个缺失结果。反复重启会掩盖问题究竟来自来源、agent、resolver 还是执行所需的行情覆盖。

## 实现依据

- [code/src/event_trader/tools/live_runtime.py](../../../code/src/event_trader/tools/live_runtime.py)
- [code/src/event_trader/live_runtime.py](../../../code/src/event_trader/live_runtime.py)
- [code/config/kernel.live.btc.toml](../../../code/config/kernel.live.btc.toml)
- [code/config/kernel.live.sox.toml](../../../code/config/kernel.live.sox.toml)

[文档目录](../index.md) · [English](../../en/guides/live-running.md)
