# 配置参考与示例

[文档目录](../index.md) · [English](../../en/development/configuration.md)

配置决定工作区身份、来源采集、推理 provider、行情语义和模拟执行，是运行 provenance 的组成部分，不只是 API 参数集合。

## 配置分组

| 分组 | 职责 |
| --- | --- |
| 顶层 workspace／mode／interval | 状态位置、live／replay、heartbeat 和反思时间 |
| 四类 agent 配置 | implementation、provider、endpoint／model、环境密钥和日志 |
| `live` 与来源配置 | channel、target、搜索 cadence 和采集工具 |
| 行情与 mapping | provider、主交易／上下文订阅、symbol、session、granularity |
| `stream_routing` | CEAU policy 版本／状态和形成要求 |
| `pm_review_runtime` | 自动调度／执行与持仓 gate |
| `execution` | paper 价格基准、买卖成本和缺失 bar 策略 |
| `runtime_workers`、`artifact_retention` | worker 容量与指定调试产物保留 |

可接受字段、默认值和校验见 `config.py`。未使用或拼错的字段不是扩展接口。

## 示例差异

历史黄金 replay 使用 GCUSD 五分钟 bar，配置执行费为零，论文 evaluator 的成本 overlay 另算。SOX live 设置买／卖各 1.5 bps 的目标覆盖，并开启自动模拟执行；BTC 自动召集 PM，但关闭自动执行。

`implementation = "mirothinker"` 不表示选中的 Analysis／PM／reflection 全部由上游实现。实际所有权要沿项目 factory 与 structured-provider composition 确认。

## 可复现变更

变更配置时记录 target、run ID、workspace、代码版本、输入／archive 身份、模型 endpoint、routing policy、session／adjustment basis 和成本。实质不同的运行使用新 workspace。

Provider 与协议形态不同，不能只凭模型名称选择 Anthropic-compatible endpoint；所需环境变量由选定配置决定。

启动校验会拒绝不兼容组合，但不是完整环境或历史数据检查。运行前阅读[安装](../guides/installation.md)和[live](../guides/live-running.md)。

## 保守的自动流程起点

完整运行配置可以使用以下片段**替换对应设置**，不能追加重复 TOML table：

```toml
[pm_review_runtime]
auto_dispatch_after_analysis = false
execute_pm_decisions = false
require_workspace_ready = true
```

它关闭 Analysis 后自动 PM dispatch 和自动模拟执行，但不关闭来源采集、Analysis 模型调用或显式 operator execution。Position-review gate 是另一个设置，运行前检查完整 table。

同一配置还要修改 workspace／log 路径和 target／instrument mapping。公开 sample 配置用于查看样例，不是初始化 live 仓位。

## 单位与覆盖优先级

成本为 basis point（`1 bps = 0.01%`）。目标级 execution cost override 可以覆盖全局设置，应检查目标解析后的成本，不能只读 `[execution]`。

其他设置分别以秒、小时、分钟、bar 或字符计。CEAU 的 bar delay 不是 wall-clock timeout，prompt character cap 不是 token budget。Reflection horizon 表示结果成熟期，lookback 表示查找复盘窗口，两者不同。

最终值以 loader 校验的配置为准。未经 session／mapping 检查，把黄金实验参数复制到 BTC 连续交易目标，不构成等价实验。

## 实现依据

- [code/src/event_trader/config.py](../../../code/src/event_trader/config.py)
- [code/src/event_trader/agent_implementation.py](../../../code/src/event_trader/agent_implementation.py)
- [code/config](../../../code/config)

[文档目录](../index.md) · [English](../../en/development/configuration.md)
