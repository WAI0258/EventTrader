# Agent 输出契约与 Prompt 设计

[文档目录](../index.md) · [English](../../en/development/agent-contracts.md)

Prompt 表达业务意图，schema 和项目 validator 决定输出能否接受。EventTrader 结合三者，不把“请返回 JSON”当作完整契约。

## 模型字段与 Runtime 字段

Analysis owner 输出刻意收窄。例如：

```json
{
  "used_lesson_ids": [],
  "assessment_decision": "no_material_assessment",
  "assessment_decision_rationale_md": "The visible evidence does not justify a material thesis change."
}
```

这是合成的阶段示例，不是实际 assessment，也不是整个 workflow 的 schema。后续 assessment synthesis 和 memory revision 各有契约；runtime ID 和禁止的 final-payload 字段由项目 schema／assembler 管理。

嵌套 schema 使用必填项、枚举、有界数组／字符串，定义处采用 `additionalProperties: false`。业务校验继续检查引用、价格角色动作、来源覆盖、PM reason、memory topology 和仓位语义，合法结构仍可能被拒绝。

## Prompt 约束与目的

| 已实现指令 | 设计目的 |
| --- | --- |
| Exposure-blind | 分开研究解释和实际仓位管理 |
| 专家仅建议，不能 write／commit | 保留单一业务 owner |
| 解释拒绝明确专家意见的原因 | 显式处理分歧 |
| 允许无实质 assessment | 不强迫每条事件修改 thesis |
| Assessment 与记忆修订分阶段 | 判断和写入独立受约束 |
| 返回 tool-input object，不返回序列化 JSON | 减少 wrapper／协议歧义 |
| 冻结上下文，按精确校验反馈修复 | 将 repair 限定在失败契约 |

PM／reflection 各有 prompt 和结果契约，这些设计不能扩展为对所有外部 runtime 的保证。

## 修复与观察

Transport retry、protocol repair 和 business-contract repair 处理不同失败。记录阶段和拒绝原因，不能把每个 attempt 算作成功 Analysis。

Tool schema 和读写回执增强可检查性，但不证明真实、推理质量或完整 lesson 使用归因。当前 Analysis supervision 初始化空 included-lesson-ID tuple，不能宣称全部所选经验已忠实追踪为使用。

执行预算见 [harness](agent-harness.md)，验证范围见[契约与测试](contracts-and-testing.md)。

## 减少歧义的 Prompt 细节

Read planner 接收项目生成的 page topology，以及业务 target／instrument proxy 区别，只规划读取，不判断、不写、不生成最终 JSON。已有 compiler coverage 时，仅针对明确 grounding 缺口补读。

Revision planner 接收已验证 Analysis payload，选择 operation／page／section，但不写正文。Writer 再接收一个由项目选定的 intent，不能自行改去其他页面或操作。

Writer repair 接收被拒 payload 和累计 violation ledger，必须返回连贯替换结果，不重新引入已解决违规。这区别于不受约束的“再试一次”。

这些规则减少意外扩展：读阶段不能悄悄做仓位决策，正文 writer 也不能因为方便就新增写入表面。

## 各阶段协议不同

上面的 owner JSON 只属于该阶段。PM 使用自己的 boxed contract-output envelope 返回 draft，reflection 有自己的 parser，不能把全部 adapter 改写成统一 JSON 习惯。

PM targeted repair 可以跳过 supplemental read，只请求指定 repair field，再确定性合并；不能把它套到 Analysis repair，后者的已接受上下文／决策和 revision-plan violation ledger 是另一套流程。

修改 prompt 时保留源码依据，确保措辞仍对应 tool schema、parser、runtime 字段及 validator 反馈。

## 实现依据

- [code/src/event_trader/contracts/analysis_assessment_schema.py](../../../code/src/event_trader/contracts/analysis_assessment_schema.py)
- [code/src/event_trader/contracts/analysis_final_payload_validation.py](../../../code/src/event_trader/contracts/analysis_final_payload_validation.py)
- [code/src/event_trader/reasoning/analysis_workflow.py](../../../code/src/event_trader/reasoning/analysis_workflow.py)
- [code/src/event_trader/pm_review/prompt.py](../../../code/src/event_trader/pm_review/prompt.py)
- [code/src/event_trader/reflection/prompt.py](../../../code/src/event_trader/reflection/prompt.py)

[文档目录](../index.md) · [English](../../en/development/agent-contracts.md)
