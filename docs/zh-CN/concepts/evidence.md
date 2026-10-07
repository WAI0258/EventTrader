# 证据接纳与事实记录

[文档目录](../index.md) · [English](../../en/concepts/evidence.md)

Feed payload 尚不是项目认可的记录。Admission 将来源输入转为带稳定身份和明确时间的证据，ledger 再保存该记录。

## 从来源到证据

Feed mapper 规范 manual、news 和 web-search 输入。Admission 校验来源形态，生成身份与元数据，形成 ledger／runtime 输出。来源 archive 单独保留采集身份，source policy、release gate 和 quarantine 处理不能走普通发布路径的输入。

发布时间、来源时间、观测／可见时间与后续持久化时间不可互换。追踪延迟或重复输入时保留它们，见[时间可见性](temporal-visibility.md)。

## 保存与解释

File-backed ledger 提供 append 以及按 ID／区间读取，位于工作区 `ledger/` truth surface。研究页面可以引用证据，但编辑页面不会改变原证据。

稳定身份有助于下游去重和引用，不保证不同 provider 永远不会重复描述同一现实事件。Admission 也不证明来源内容真实，只证明输入已成为符合契约的项目证据记录。

## 为什么采集与接纳分开？

搜索结果可能晚捕获、再次发现、隔离，或在研究前被拒绝。分开 archive observation 和 admitted ID，可以定位失败边界，避免把“搜索成功”视为“Analysis 已使用”。

新增 feed 时先映射时间戳与 provenance，再确定标签和 prompt。不能仅因为发布时间较早，就把历史可见时间设置为该时间以通过 replay 检查。

Ledger 有内容边界和结构，不保证保存上游网页全部字节。公开样例省略来源正文，原始输入需用户按自己的访问条件取得。

## 持久记录与运行时通知

| 字段 | 职责 |
| --- | --- |
| `event_id` | 下游记录引用的稳定标识 |
| `target_key` | 接收证据的研究标的 |
| `source_ref` | 来源溯源／引用 |
| `title`、`content`、`labels` | 归一化证据与分类 |
| `ts_source`、`ts_event`、`ts_init` | 分开的来源、事件与初始化时间 |

Admission 生成持久 ledger record 和轻量 runtime event，两者身份与时间对应。Event 负责路由工作，ledger 提供证据正文。这避免 transport message 成为另一套事实存储，也让后续 reader 能解析相同引用。

例如，页面 12:00 发布、13:00 才首次采集，应保留这一区别。旧发布日期不能证明 12:30 已可见。Admission 身份使用标的、来源、admission 时间和归一化内容；两个服务商描述同一公告仍可能生成不同证据标识。

研究可以修正解释，而不改写来源记录。因此能够分别说明系统收到了什么，以及判断如何变化。

## 实现依据

- [code/src/event_trader/feeds/payload_mappers.py](../../../code/src/event_trader/feeds/payload_mappers.py)
- [code/src/event_trader/ingest/admission.py](../../../code/src/event_trader/ingest/admission.py)
- [code/src/event_trader/evidence_ledger/file_backed.py](../../../code/src/event_trader/evidence_ledger/file_backed.py)
- [code/src/event_trader/source_release](../../../code/src/event_trader/source_release)

[文档目录](../index.md) · [English](../../en/concepts/evidence.md)
