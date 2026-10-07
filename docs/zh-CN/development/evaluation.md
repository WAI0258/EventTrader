# 评估与结果来源

[文档目录](../index.md) · [English](../../en/development/evaluation.md)

仓库结果有不同类型，比较前要对齐决策、执行、估值、费用和时间口径。

## 结果类型

| 类型 | 含义 |
| --- | --- |
| 公开 PM／Direct 样例 | 记录的模拟操作与假设研究映射 |
| 黄金 baseline 研究 | 项目 adapter／evaluator 下的保存历史结果 |
| Time-batch | 事件驱动与固定时钟输入变体 |
| CEAU 消融 | 逐条调用与有边界分析单元 |
| 事件案例图 | 精选反应案例和假设前向收益 |

首页 SOXX 截止点和 IAU 区间为观察结果后选择，附完整可用区间；BTC 重叠行情不足 30 天。样例还存在延迟持久化／catch-up，不证明全部操作都当时记录。

## Adapter 与统计口径

TradingAgents、FinMem、AI Hedge Fund adapter 提供本地输入并映射为共同任务。这是实验设计，不代表原生环境完全相同。FinMem 训练 warmup 可暴露 future record，test 关闭该设置；signed action-score 评估不同于原生 inventory 核算。

历史执行配置与 evaluator fee 分开：黄金配置执行费为零，evaluator 使用声明的 overlay；公开 PM 曲线保留自己的持久化执行成本。

事件图使用假设前向 PnL，不是实际 PM 交易。按有利结果选择的案例说明行为，不代表总体胜率；峰值／excursion 不是已实现利润。

## 核验保存结果

仓库根目录执行：

```sh
python -B tools/verify_results.py
```

成功 Analysis 的 runtime／token 汇总不是全系统端到端延迟。要将 outcome 对齐成功 task log，保留已说明的 exclusion。

Calendar time-batch builder 有小数秒边界：`00:04:59.900` 的事件可能被分到 `00:04:59` 的包。这是实现限制，不证明已保存实验包含受影响行。

历史输入范围与版本引用以本页链接的 manifest、provenance 和实际 evaluator 为准。

## 指标定义与来源记录

Evaluation 模块复利累乘各期收益得到累计收益，按配置的 annual periods 年化收益与波动。波动采用样本标准差；观察数不足或超额收益方差为零时 Sharpe 缺省，drawdown 为零时 Calmar 缺省。这些未定义值不应展示为实测零值。

最大回撤相对于权益运行峰值计算。Alpha／beta 需要对齐的收益输入和非零 benchmark 方差；数组长度相同不等于日历已对齐。应明确资产时段与采样频率，而非对所有序列自动使用一年 252 个观察值。

数值 verifier 使用各保存实验自身的口径。历史 evaluator 的费用 overlay，与已嵌入 PM 曲线的执行成本不同；再次同时扣费会改变结果。

[实验 provenance](../../../experiments/provenance.json) 记录源码与上游引用。[输入 manifest](../../../data/experiment_inputs_manifest.json) 描述导出的输入元数据，并非被排除的原始语料。这些材料帮助识别公开内容，但不能证明每次历史运行的精确代码／配置。新实验使用自己有权使用的输入，并保留该次运行配置。

## 实现依据

- [code/src/event_trader/evaluation/metrics.py](../../../code/src/event_trader/evaluation/metrics.py)
- [tools/verify_results.py](../../../tools/verify_results.py)
- [baselines](../../../baselines)
- [experiments](../../../experiments)
- [code/scripts/build_calendar_timebatch_dataset.py](../../../code/scripts/build_calendar_timebatch_dataset.py)

[文档目录](../index.md) · [English](../../en/development/evaluation.md)
