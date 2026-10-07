# 历史回放

[文档目录](../index.md) · [English](../../en/guides/historical-replay.md)

Replay 按带时间戳的证据推进受控时钟，目的是在有边界的历史输入下检查决策，而不是把今天的叙述当作过去已观测到的信息。

## 准备一次运行

自行准备所需行情档案和证据数据。仓库公开 schema、manifest、adapter 和保存结果，没有公开原始付费输入语料。除评估区间外，还要检查价格 warmup 覆盖。

配置使用独立输出工作区和不同 run ID。历史黄金配置要求五分钟 GCUSD bar，并包含评估开始前的价格 warmup。

在 `code/` 查看：

```sh
uv run python -m event_trader.tools.replay_runtime run --help
```

Shell 命令模板：

```sh
uv run python -m event_trader.tools.replay_runtime run \
  --config config/kernel.replay.gold.toml \
  --dataset /path/to/evidence.jsonl \
  --target-key gold \
  --window-start 2026-01-01 \
  --window-end 2026-06-30 \
  --run-id gold-replay
```

运行前替换数据和 workspace 路径。PowerShell 中写成一行，或使用 PowerShell 的续行语法。

## 执行与检查

Runner 使用测试时钟和串行推进调度历史工作。Checkpoint 记录进度；先发布、后标记进度允许重试，也要求下游处理重复。一次 completion-pump 不代表全部队列已经静止。

联合检查已接纳证据、上下文可见性审计、Analysis 结果、PM／执行关联及 episode 产物。区分决策业务时间、执行时间和持久化时间。

历史网页搜索是按历史视角约束的事后检索，不证明网页或当前正文在当时已被观测。见[时间可见性](../concepts/temporal-visibility.md)。

## 恢复与评估

恢复和自动 checkpoint repair 可能改变状态，先读[运维指南](operations.md)。Paper snapshot、导出的开发运行时与历史实验实际版本不同，见[评估说明](../development/evaluation.md)。

保存结果核验成功说明数值一致，不等于重新运行了 agent。

## 区分三个时间窗口

| 窗口 | 用途 |
| --- | --- |
| 区间前 warmup | 为决策提供所需的较早价格／上下文 |
| 决策／评估区间 | 定义报告统计的操作与收益 |
| 后续 reflection 区间 | 为符合条件的事后复盘提供结果观察 |

`--reflection-end` 默认等于 replay 区间终点。延长它可提供后续结果观察，但不允许此前的 Analysis 或 PM 看到这些价格。`--reflection-step-hours` 独立控制 reflection 推进，不等于行情 bar 粒度。

Checkpoint 状态为 `started`、`completed`、`failed`，失败记录保留错误。选择恢复前，对齐 run key、标的、事件和阶段；即使 checkpoint 未完成，重复发布也可能已影响下游状态。

可复现比较需要同时保留配置、输入身份、run ID、截止时间、价格基准和 evaluator 设置。应比较接受的决策与执行，而非假设相同 prompt 必然得到相同模型运行。

## 实现依据

- [code/src/event_trader/replay/runner.py](../../../code/src/event_trader/replay/runner.py)
- [code/src/event_trader/replay/admissibility.py](../../../code/src/event_trader/replay/admissibility.py)
- [code/src/event_trader/tools/replay_runtime.py](../../../code/src/event_trader/tools/replay_runtime.py)
- [code/config/kernel.replay.gold.toml](../../../code/config/kernel.replay.gold.toml)

[文档目录](../index.md) · [English](../../en/guides/historical-replay.md)
