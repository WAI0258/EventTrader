# 扩展项目边界

[文档目录](../index.md) · [English](../../en/development/extensions.md)

扩展应选择直接服务交易流程的最小接口。新增 feed／provider 保留确定性事实边界；新推理组件不能变成另一套仓位事实源。

## 扩展位置

| 需求 | 现有边界 |
| --- | --- |
| 新证据来源 | Feed model／mapper、source archive identity、admission |
| 新行情 API | `MarketBarsProvider.read_series`、provider 构造、mapping／session policy |
| 新模型 backend | Agent implementation／provider capability 和 structured inference |
| 新研究工具 | Tool gateway、允许的读写名称、schema 与 receipts |
| 新展示 | Read model／projection、Console DTO／前端 |
| 新经验行为 | Reflection lifecycle、learning-card contract／selection |

来源 adapter 区分发布时间与观测可见时间，保持稳定 source reference。行情 adapter 返回 canonical bar，匹配 session／granularity／price semantics，不通过编造 bar 隐藏缺失。

## 使用 Ports，保留业务所有权

项目 protocol 提供 evidence append／read、page／section write、escalation、market read 和 outcome-context read。Composition 将实现接入 runtime。

MiroThinker／MiroFlow 提供 agent／tool 基础设施，NautilusTrader 支持传输集成。复用不应把 admission identity、历史 admissibility、研究事实或执行算术移进模型 prompt。

## 实际开发顺序

1. 跟踪相邻现有实现，明确输出所有者。
2. 定义新输入的身份、时间、错误和可见性。
3. 只增加当下所需 adapter 与 composition／config。
4. 检查畸形输入、重复身份、缺失覆盖及一条成功下游路径。
5. 同步相关指南和契约文档。

新增 canonical state 前阅读[运行状态](runtime-and-state.md)。新增工具前阅读 [agent contracts](agent-contracts.md)与[时间可见性](../concepts/temporal-visibility.md)。

任意 API 不是因为能给 OHLCV 就自动兼容；接券商执行也不同于接行情 feed。

## 先定义 adapter 的接受条件

实现 feed 前，写清来源引用、时间映射、标的路由和畸形输入处理，再沿一条记录检查 admission 与 ledger。成功标准是保留身份的下游接受记录，不只是 HTTP 成功响应。

Market provider 要同时验证交易品种映射、bar 粒度、交易时段和 cutoff。即使 OHLCV 数值看起来合理，日历不匹配也可能改变比较区间。先将缺失覆盖作为明确条件测试，再增加可选 B&H 基准。

修改模型／工具时，保留阶段原有权限：Checker 分类证据，Analysis 通过批准的工具修订研究，PM 决定敞口，确定性执行应用操作。新增推理工具不应获得直接改写组合事实的权限。

扩展评审沿因果链展开：新输入 → 现有 contract → reader／tool → 接受的 outcome → 持久效果。优先在该边界增加 adapter，而非建立第二套编排框架或并行状态存储。

## 实现依据

- [code/src/event_trader/contracts/ports.py](../../../code/src/event_trader/contracts/ports.py)
- [code/src/event_trader/market/provider.py](../../../code/src/event_trader/market/provider.py)
- [code/src/event_trader/composition.py](../../../code/src/event_trader/composition.py)
- [code/src/event_trader/integrations](../../../code/src/event_trader/integrations)

[文档目录](../index.md) · [English](../../en/development/extensions.md)
