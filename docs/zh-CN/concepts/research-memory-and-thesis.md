# 研究记忆与 Thesis 版本

[文档目录](../index.md) · [English](../../en/concepts/research-memory-and-thesis.md)

研究记忆是长期存在、以 Markdown 为中心的研究内容，保存可修订解释和引用；证据 ledger 单独保留已接纳事实。

## 组织研究

Canonical layout 包含共享 topics／entities 和目标页面，index／log 支持导航与修订记录。Context reader 可选择有限页面、章节或引用周边，不必反复注入整个研究库。

项目工具支持创建、重写页面和更新章节，引用及页面拓扑校验约束写入。价格语义模块把 active role 关联到 instrument／adjustment basis，避免文本价位在价格约定之间无声漂移。

## 修订流程

Assessment 校验后，Analysis 规划有限 write intent。独立修订草稿可并发生成，项目工具随后逐项校验并应用到 staging；supervisor 再将接受的 receipted change 应用到 canonical memory。当前最多 12 个计划写调用、3 个并发 revision writer。

Thesis-revision storage 构建 canonical bundle 和历史快照。Console 比较版本文本，可选翻译／速览从哈希绑定的正文派生，不变成新的权威 thesis。

这个划分帮助分别回答“来源说了什么”与“阅读后我们怎样判断”。后续研究纠正不需要覆盖原证据。

## 边界与检查

联合检查 page reference、citation、write receipt、assessment identity 和 revision time。绕过 workflow 的 Markdown 编辑可能缺少这些关联。多文件更新不是单一数据库事务，部分失败需要运维恢复。

历史快照与当前记忆读取语义不同。`HistoricalThesisSnapshotReader` 只支持标的的 canonical 页面 `index.md`、`thesis.md`、`risks.md`、`watchlist.md`、`timeline.md`。`index.md` 返回 bootstrap template；其余支持页从决策业务时间当时或之前的最新 thesis revision 重建，没有对应版本时也返回 bootstrap template。这不代表任意共享 topic／entity 或所有 research-memory 页面均可历史还原。不能用今天的页面替代过去决策使用的视图，见[时间可见性](temporal-visibility.md)。

Learning card 和 episode memory 补充研究页面，但有独立 eligibility／promotion 契约。先读[反思](reflection-and-learning.md)，不要把每条已保存经验都称为已启用学习。

## 固定页面与编辑纪律

目标入口含 `thesis.md`、`timeline.md`、`risks.md`、`watchlist.md`，有必需 heading skeleton。Index 是导航地图；Analysis memory-commit 拒绝直接 `update_index`。

Writable topology 给出精确 page／section 配对。某页面有 “Risks” 不授权 writer 推断其他页面也有同一配对。局部修改优先 `update_page_section`，结构实质不连贯时才 `rewrite_page`，新长期研究对象才 `create_page`。

Section writer 只返回正文 Markdown，不重复页面标题或所选 section heading。Market Setup Dashboard 等确定性 projection section 不允许模型自由重写。

## 从 Staging 到正式记忆

Supervisor 在 `runtime/analysis_staging/<task_id>/` 准备任务工作区，revision tool 先写这里，形成 `tool_activity.jsonl` 回执。独立草稿并发生成，staged tool write／repair 顺序应用。

Supervisor 校验回执与结果一致性后，canonical commit 带 attribution 应用页面变更，可以折叠重复 section receipt，并从接受的 assessment 单独投影 runtime-owned active-price section。

因此错误草稿不必立即成为正式研究，但 staging 边界不意味着之后的 canonical 更新是原子事务。临时 staging 会清理，不能把 tool-activity 路径当作永久审计档案。

Assessment 为 null 时 revision plan 必须为空；若要求 watchlist maintenance 且计划长期修订，必须包含 active target watchlist。长期认知未变时，空修订计划有效。

## 实现依据

- [code/src/event_trader/research_memory](../../../code/src/event_trader/research_memory)
- [code/src/event_trader/contracts/research_memory.py](../../../code/src/event_trader/contracts/research_memory.py)
- [code/src/event_trader/thesis_revision/historical_reader.py](../../../code/src/event_trader/thesis_revision/historical_reader.py)
- [code/src/event_trader/thesis_revision](../../../code/src/event_trader/thesis_revision)
- [code/src/event_trader/pm_console/thesis.py](../../../code/src/event_trader/pm_console/thesis.py)

[文档目录](../index.md) · [English](../../en/concepts/research-memory-and-thesis.md)
