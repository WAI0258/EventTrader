# 反思与学习生命周期

[文档目录](../index.md) · [English](../../en/concepts/reflection-and-learning.md)

Reflection 复盘有边界的决策或仓位结果，把评估与“是否写候选经验／晋升可复用 learning card”分开。

## 资格与上下文

反思使用明确 anchor、trigger policy、coverage、结果价格、决策／执行上下文及成熟 horizon。已关闭仓位与符合资格的 executed open-entry 审查条件不同。缺少结果数据可能使 review 不具资格，不能虚构行情路径。

Cadence aggregation 支持更宽的审查；replay 结束时可以评估符合条件的开放仓位，但这不等于看到了最终平仓。

实际反馈与确定性反事实上下文分开。假设替代动作可以辅助分析，不变成执行交易。

## 从复盘到候选经验

结构化输出经过契约校验。Review 写入、episode-memory candidate／delta、no-update、promotion decision、learning-card store 与 coverage resolution 各有生命周期记录。

保留的 candidate 不会自动成为 active card。Selection 与 consumer-role 约束经验进入后续上下文的方式，risk-policy review candidate 另有生命周期，让暂定经验保持暂定，而非立刻覆盖未来决策。

## 收益与边界

生命周期保留经验与被复盘 episode、价格和来源引用的关联，让修订和 promotion 可检查。

它不证明未来收益有因果改善。当前 Analysis lesson-use 身份不能完整归因所选卡片的实际使用；evaluation／write／learning／resolve 是分步操作，不是一个原子事务。部分 cadence 状态在进程内，重启后会重置。

缺少经验时沿 eligibility→trigger／coverage→evaluation→write→promotion／selection 排查，不能只因为未晋升 card 就判断“学习失败”。

继续阅读[研究记忆](research-memory-and-thesis.md)和[运行状态](../development/runtime-and-state.md)。

## 三种不同记忆

| 表面 | 用途 | 为什么分开 |
| --- | --- | --- |
| Research page | 当前可修订 thesis 与证据 | Thesis 编辑不是已完成操作 |
| Episode memory | 绑定决策／持仓 episode 的经验 | 保留经验与复盘结果关联 |
| Learning card | 可复用、按角色选择的方法指导 | Selection／promotion 可以比存储更窄 |

Risk-policy review candidate 与普通 learning-card promotion 有独立契约，保存 candidate 不自动修改 live 风险配置。

Learning application 区分 skipped write、成功 candidate receipt 和 failed action。Review 页面存在时，learning action 仍可能失败，必须看 action receipt，不能假设两者同时成功。

## Cadence 与成熟

Open-entry horizon review 要求 executed entry 和充分结果数据；closed review 使用相关关闭 episode。Weekly／monthly cadence aggregation 按自己的 coverage 规则综合符合条件的 review。

Heartbeat 可以是 not due、no eligible candidates、dependency missing、review written／skipped 或 failed。没出现 card 可能是有意 no-write／no-promote。

示例亏损交易若归因不充分，可能不产生可复用经验；盈利结果也不自动验证决策方法。Reflection 评估证据和方法，不只是把每次赚钱标为成功。

## 实现依据

- [code/src/event_trader/reflection](../../../code/src/event_trader/reflection)
- [code/src/event_trader/episode_memory](../../../code/src/event_trader/episode_memory)
- [code/src/event_trader/learning_cards](../../../code/src/event_trader/learning_cards)
- [code/src/event_trader/counterfactuals](../../../code/src/event_trader/counterfactuals)

[文档目录](../index.md) · [English](../../en/concepts/reflection-and-learning.md)
