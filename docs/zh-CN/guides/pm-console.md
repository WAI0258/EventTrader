# 使用 PM Console

[文档目录](../index.md) · [English](../../en/guides/pm-console.md)

PM Console 用关联的 read model 呈现交易过程，帮助追踪研究判断怎样形成仓位决策，以及决策是否真的产生执行。

## 页面与问题

| 页面 | 回答的问题 |
| --- | --- |
| 总览与标的侧栏 | 当前查看哪个标的／工作区？ |
| 仓位 | 记录了什么敞口及 PM／Direct 表现？ |
| Episodes | 哪些操作属于同一个持仓段？ |
| Thesis | canonical 研究在版本之间怎样变化？ |
| PM 演进 | 做了哪些仓位决策，依据是什么？ |
| Operator | 系统接收了什么操作员上下文？ |

后端读取 canonical 记录与派生 projection；前端不是执行事实的来源。关联警告要结合决策、执行记录理解，不能把所有研究建议都当作交易。

## 两种模式

`--saved-results` 从样例读取已保存数值曲线，不尝试在缺少原始行情时重新计算，跳过模型服务，并拒绝 POST／PUT。公开样例省略研究叙述，不能完整展示 thesis／编辑体验。

完整工作区还提供操作员上下文编辑／恢复，以及可选的研究翻译和速览服务。研究版本比较使用文本差异；翻译和摘要属于派生内容，绑定 canonical 正文哈希，不替代原始研究版本。

EN／ZH 切换改变界面标签；研究正文翻译是单独操作，可能需要 LLM。

## 本地使用

前端地址为 http://127.0.0.1:4173。API 地址以启动输出为准，标的与工作区对应关系由 mount catalog 确定。Console 应保持本地使用：后端 CORS 较宽松，没有实现多用户身份认证层。

样例启动见[快速开始](quickstart.md)，可选 B&H 见[行情指南](market-data.md)。理解完整工作区时，结合[研究记忆](../concepts/research-memory-and-thesis.md)与[仓位执行](../concepts/portfolio-and-execution.md)，不要把版本修订当作已执行操作。

## 连接完整工作区

在 `code/` 中，单工作区启动命令形式如下：

```sh
uv run event-trader-pm-console --workspace-root /path/to/workspace --config /path/to/config.toml
```

将两个路径替换为自己的运行目录与配置。Mount catalog 则将多个命名标的分别绑定到工作区／配置。公开样例中的 `sox`、`gold`、`btc` 分别表示 SOXX、IAU、BTC；target key 不是通用的交易品种标识。

先选择标的，再查看 episode、相关 PM 决策及执行记录。API 的 `/api/targets` 提供标的列表；聚合模式需使用 `/api/targets/{target}/context` 等限定标的的 context 路由，未指定标的的请求在该模式下有歧义。

`--display-start-at` 调整 UTC 展示／PnL 基准，并将此前 PM 敞口带入第一根展示 bar。它不会在该 bar 虚构新入场，也不删除此前操作。检查子区间时可使用此选项，并保持报告日期一致。

可选 B&H overlay 使用用户自己的行情生成。它补充保存的 PM／Direct 曲线，不重新运行 agent，也不替换操作记录。

## 实现依据

- [code/src/event_trader/pm_console](../../../code/src/event_trader/pm_console)
- [code/src/event_trader/tools/pm_console_server.py](../../../code/src/event_trader/tools/pm_console_server.py)
- [code/frontend/pm-console/src/features/pm-console](../../../code/frontend/pm-console/src/features/pm-console)

[文档目录](../index.md) · [English](../../en/guides/pm-console.md)
