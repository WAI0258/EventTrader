# 运行时所有权与持久化状态

[文档目录](../index.md) · [English](../../en/development/runtime-and-state.md)

Composition 构建 kernel 和依赖，runtime graph 路由项目消息与工作。Worker 负责耗时推理，resolver 将结果应用到业务链路。

## 工作区表面

| 路径 | 含义 |
| --- | --- |
| `ledger/` | 已接纳证据事实 |
| `research_memory/` | 可修订研究事实，含共享和目标内容 |
| `runtime/` | 操作记录、决策、执行与处理状态 |
| `helpers/` | 派生辅助内容，不是 canonical truth |

Shared market data 有独立存储和 observed-prefix 语义。不能只凭界面标的名推断 workspace 所有权；Console mount 明确绑定 target、workspace 和 configuration。

## 消息与工作生命周期

Graph 组合 admission、Checker、CEAU、Analysis、PM 和 reflection。Queue、completion pump、outbox、receipt 与 resolver 将传输调度和 agent 解释分开。

Live worker capacity 与 serial replay 不同。一次 replay drain／completion 不应被称作全局静止；slow-work runtime 和 model stage runtime 也是不同指标。

## 持久化边界

Append-only ledger 和幂等 append helper 有助恢复，但不构成全局事务。Analysis 可能分步写 memory／assessment／thesis／outcome／PM／episode；execution 也分开保存 intent、result、portfolio state 和 episode。

Replay 可能先发布工作、后标记 checkpoint，失败后允许重新投递。要在实际副作用边界检查重复处理，不能凭稳定 task ID 宣称 exactly-once。

## 排查失败

定位 canonical input、queued work、worker result、resolver action 和落盘输出，使用 stage／task ID、业务时间及持久化时间。畸形尾部排查需保留有效 append-only 前缀。

Migration、source quarantine、price-basis repair 和 retention 属于明确 operator workflow。参阅[运维](../guides/operations.md)，不要从前端 projection 重建 canonical state。

## 并发有不同的负责人

Slow-work executor 按 Checker、Analysis、PM-review lane 的顺序处理。配置并发为一时串行执行；容量更大时先在线内使用 worker，再进入下一 lane。这不同于 agent 任务内部的 expert／writer 并行，也不同于历史 replay 的串行推进。

Worker result 不等于所有下游业务效果已完成。Completion pump 和 resolver 应用 outcome，还可能产生新的工作。确认 pipeline 已排空前，要追踪 durable outbox／receipt 和最终记录。

| 问题 | 应查看的负责人 |
| --- | --- |
| 构造了哪个服务？ | Composition 与配置 |
| 调度了哪些工作？ | Graph、queue、outbox |
| 推理是否完成？ | Worker result 与 task outcome |
| Canonical state 是否改变？ | Resolver 与接受的持久记录 |
| 展示是否最新？ | Projection／read model 与来源引用 |

这种布局让职责可见，而非隐藏在前端后面。它也解释了为什么恢复一个失败阶段，应保留上游事实，而不是重建整个工作区。

## 实现依据

- [code/src/event_trader/composition.py](../../../code/src/event_trader/composition.py)
- [code/src/event_trader/runtime](../../../code/src/event_trader/runtime)
- [code/src/event_trader/storage/layout.py](../../../code/src/event_trader/storage/layout.py)
- [code/src/event_trader/portfolio/pm_execution_flow.py](../../../code/src/event_trader/portfolio/pm_execution_flow.py)

[文档目录](../index.md) · [English](../../en/development/runtime-and-state.md)
