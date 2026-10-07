# 时间可见性与回放防泄露

[文档目录](../index.md) · [English](../../en/concepts/temporal-visibility.md)

历史正确性要求控制决策当时可见的信息，不能只按发布时间排序。EventTrader 对证据、行情、上下文和历史状态读取设置明确边界。

## 共享决策边界

`DecisionVisibilityBoundary` 包含 `business_at`、`max_visible_event_time`、`max_visible_market_time`，两个最大可见时间都不能晚于业务时间。Analysis-style 可以把它们设为业务时间，下游上下文也可携带更窄 cutoff。

契约只校验边界形态与顺序，reader 仍须实际应用；合法 boundary object 不能单独阻止工具返回未来信息。

## 分层检查

| 内容 | 防护或区别 |
| --- | --- |
| Admission／replay | 来源时间、观测可见时间、admissibility 和受控时钟 |
| Context／tool | 有边界 event／market 读取、可见性审计和读回执 |
| Research／thesis | 历史 snapshot policy 与当前页面读取 |
| PM history | 业务时间更早的决策，而非任意后续历史 |
| CEAU completeness | 事件时间成熟与处理时间安全关闭分开 |

决策前价格 warmup 是合法输入，决策后价格属于结果评估。之后的 reflection 可以用后续结果，不使这些结果成为之前决策的合法输入。

## 仍需注意的边界

历史网页搜索是事后历史视角，prompt 要求 cutoff 前内容不证明网页历史可用或正文未变。发布时间与首次观测／捕获时间仍要分开。

部分 latest portfolio／episode 表面，以及 option universe membership，不构成完整历史 as-of 重建；具体实验需检查这些语义。

Calendar time-batch builder 可能提前包时间：`00:04:59.900` 映射为 `00:04:59`，不能声称该 builder 全面杜绝泄露。

## 验证方式

逐输入追踪 source→admitted record→所选 context／tool result→assessment→PM decision，在各边界检查时间与引用，包括重试和恢复。回执 coverage 证明可用／访问，不证明事实或推理正确。

系统提供分层防护，不是全部历史实验和外部检索路径无 look-ahead 的认证。

## 用明确截止时间审计一次决策

假设决策业务时间为 14:00。Reader 的行情 cutoff 允许时，可以读取此前的价格 warmup。某来源 14:05 才首次观察到，不能因其声明 13:50 发布就让它在 14:00 可见。15:00 的价格可用于后续评估／reflection，但必须排除在此前决策上下文之外。

沿四层检查同一个边界：

1. 确认来源观察时间与 admitted event 时间。
2. 查看选中的上下文、受限工具读取及其 receipts。
3. 按历史读取策略解析 research／thesis，而非用今天的页面替代。
4. 检查 PM 历史和相应交易品种的行情覆盖，再解释结果。

这解释了为什么防未来数据泄露需要 deterministic reader 和 contract，也需要 prompt。Prompt 指导 agent 如何推理，reader 控制它能取得哪些记录。Read receipt 让访问路径可检查，历史 snapshot 保留过去决策使用的视图。

新增工具或 projection 时，应明确它返回历史 as-of 状态还是当前状态。在 replay 中复用当前状态视图，需要单独证明时间语义成立。

## 实现依据

- [code/src/event_trader/contracts/temporal_visibility.py](../../../code/src/event_trader/contracts/temporal_visibility.py)
- [code/src/event_trader/replay/admissibility.py](../../../code/src/event_trader/replay/admissibility.py)
- [code/src/event_trader/context_assembly/replay_audit.py](../../../code/src/event_trader/context_assembly/replay_audit.py)
- [code/src/event_trader/thesis_revision/historical_reader.py](../../../code/src/event_trader/thesis_revision/historical_reader.py)
- [code/scripts/build_calendar_timebatch_dataset.py](../../../code/scripts/build_calendar_timebatch_dataset.py)

[文档目录](../index.md) · [English](../../en/concepts/temporal-visibility.md)
