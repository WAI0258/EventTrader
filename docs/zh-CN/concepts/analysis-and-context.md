# Analysis 与有边界上下文

[文档目录](../index.md) · [English](../../en/concepts/analysis-and-context.md)

Analysis 负责解释与研究修订，判断新可见证据是否实质改变 thesis，不负责实际仓位权重或模拟执行。

## 构建决策上下文

Context assembly 在 decision visibility boundary 下组织已接纳证据、目标／共享研究、操作员上下文和行情。Claim 与 citation 提供引用，coverage／read receipt 描述可用或读取过的内容。

Workflow 从有预算的读取计划开始。项目工具执行获准的 evidence、页面／章节、引用周边、wiki search 和行情面板读取。当前 workflow 读取调用上限为 16。

## 多视角，单一 owner

Evidence／causality 与 thesis／memory 专家给建议，确定性 workflow 有需要时加入 market／price 专家。这些临时专家没有提交权。

单一 Analysis owner 同时看到原上下文、实际读取和专家意见，决定 `emit_assessment` 或 `no_material_assessment`，协调冲突，并保持 exposure-blind。拒绝明确专家建议必须说明理由，不靠投票。

决策通过后生成 assessment 并做确定性校验。价格角色动作区分保留、退役和替换；runtime 身份由系统装配。

## 保存结果

Validated assessment 可以进入单独的记忆修订计划与 staged writes。Supervisor 关联 assessment、thesis／memory 变化、Analysis outcome、PM 请求和 episode／log。

没有 assessment 或交易也可能是有价值的结果。工具读取回执只证明访问，不证明模型正确使用；当前 lesson-use 身份也不能完整归因 learning card 使用。

写入机制见[研究记忆](research-memory-and-thesis.md)，校验见 [agent contracts](../development/agent-contracts.md)，阶段预算和修复见 [harness](../development/agent-harness.md)。

## Workbench：先编译，只对缺口深读

Compiler 组织有边界的 evidence、memory-impact、market 和 method 内容，不拼接整工作区。它标记摘录可用、截断或缺失，grounding 区分编译提供的内容与工具读取内容。

当前字符上限包括：单条 active-evidence 摘录 1,200、evidence lane 5,000；memory-section 摘录 700、memory-impact lane 7,000。Market／method 各有独立上限。这是字符预算，不是 token 限额，也不保证所有证据都装得下。

Read-planner prompt 要求复用完整 compiler receipt，只为省略细节、歧义、矛盾、引用背景或写入支撑补读。Grounding repair 根据明确缺口和剩余调用预算，只请求额外所需读取。

这样减少重复工具工作，同时保留查看原证据的通道，也区分“摘要已提供”和“支持章节已完整读取”。

## Target 身份与方法指导

行情工具接收业务 `target_key`，不是 proxy symbol。IAU 样例业务目标仍为 `gold`，SOXX 为 `sox`；terminal 内部解析交易工具。

Claim card 提供有边界的研究 claim 引用；所选 learning card 是较弱的方法指导，不是新事实或交易指令。Operator context 有自己的来源／读回执，它们不能无声变成另一套 ledger fact。

Context packet 按 runtime scope／stage／target／月份分区，replay 身份另包含 run ID。复现决策要使用当次 packet 和 read receipt，不能从今天的工作区重新编译一个 packet 替代。

## 实现依据

- [code/src/event_trader/context_assembly](../../../code/src/event_trader/context_assembly)
- [code/src/event_trader/reasoning/analysis_workflow.py](../../../code/src/event_trader/reasoning/analysis_workflow.py)
- [code/src/event_trader/reasoning/analysis_supervisor.py](../../../code/src/event_trader/reasoning/analysis_supervisor.py)

[文档目录](../index.md) · [English](../../en/concepts/analysis-and-context.md)
