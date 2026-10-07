# 运维与恢复

[文档目录](../index.md) · [English](../../en/guides/operations.md)

修改状态前，先定位第一个失败边界。新闻回执、分析判断、PM 决策和模拟执行各有所有者，也有不同恢复方式。

## 沿持久化链路排查

| 症状 | 优先检查的证据 |
| --- | --- |
| 没有新研究 | 来源 checkpoint、接纳证据、Checker 决策、CEAU unit／dispatch |
| 有研究但没有 PM | Analysis outcome、PM 请求原因、队列／outbox 和 worker 结果 |
| 有 PM 决策但没有操作 | 执行意图、deferred／rejected 结果和行情覆盖 |
| PnL 异常 | 执行记录、价格／成本基准、区间及 episode 关联 |
| 缺少反思 | eligibility、trigger／coverage 与结果数据 |

通过身份和时间戳关联记录。不要通过改研究正文来“修复”PM 问题，也不要把未执行建议解释为执行。

## 读取与修改的区别

CLI `--help` 和 saved-result 查看属于读取路径。PM preflight 可能初始化工作区；structured-provider preflight 可能调用外部模型。Catch-up、quarantine repair、replay repair、migration 与 retention pruning 可能写入或删除状态。

仓库针对来源档案、历史搜索、Analysis、PM、reflection 和 replay 有专门 repair 模块。依据实际失败选择入口，再看命令选项；不存在一个默认安全的“修复全部”命令。

修复前保留受影响 workspace、配置、run ID、有效 append-only 前缀及错误记录。Replay 还要保留 checkpoint 与已发布工作的关系。恢复后检查结果和关联，不能只看进程是否启动。

## 状态管理

新运行、公开样例和活跃 live target 使用不同 workspace。行情可通过专门 shared store 共享，研究和操作状态仍有独立边界。不要把私人 `.local` 整树复制进公开仓库。

Retention 设置只管理指定调试产物，不意味着可以删除 canonical 证据与研究历史。价格基准和 thesis migration 应视作状态变更。

运行时包含局部去重、append 回执与恢复机制，但多文件更新不是全局事务。见[运行时与状态](../development/runtime-and-state.md)。

## 先规划，再应用 replay 恢复

在 `code/` 查看实际支持的修复与 catch-up 命令：

```sh
uv run python -m event_trader.tools.runtime_repair --help
uv run python -m event_trader.tools.runtime_repair replay-failed-event --help
uv run python -m event_trader.tools.live_catchup --help
```

`replay-failed-event` 接受配置、标的、时间窗口，以及可选 run ID 和 event ID。不带 `--apply` 时规划修复，不应用修复；添加该参数前检查所选事件与受影响状态。这是该命令的行为，不应推定所有 operator 工具都采用相同约定。

恢复后同时确认阶段完成和业务效果：Analysis outcome 应关联 assessment，PM 决策应对应其预期操作，执行应关联新的敞口／episode。重复投递不应悄悄变成第二次操作。

清理调试材料时保留 canonical truth。临时工具轨迹和 staging 目录，与 ledger、已接受研究版本、执行历史采用不同生命周期。

## 实现依据

- [code/src/event_trader/operator/repair](../../../code/src/event_trader/operator/repair)
- [code/src/event_trader/tools/runtime_repair.py](../../../code/src/event_trader/tools/runtime_repair.py)
- [code/src/event_trader/tools/live_catchup.py](../../../code/src/event_trader/tools/live_catchup.py)
- [code/src/event_trader/artifact_retention.py](../../../code/src/event_trader/artifact_retention.py)

[文档目录](../index.md) · [English](../../en/guides/operations.md)
