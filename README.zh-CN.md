# Event-Trader：面向异步金融证据的事件驱动 LLM 研究运行时

[English](README.md) · [简体中文](README.zh-CN.md) · [论文](paper/event_trader.pdf) · [详细文档](docs/zh-CN/index.md) · [快速开始](docs/zh-CN/guides/quickstart.md) · [结果](#结果)

Event-Trader 是用于主观金融研究与模拟交易的事件驱动 LLM 系统。它跟随异步证据，维护可修订 thesis，审查实际仓位，记录模拟操作和结果。论文在黄金交易应用中研究这套流程，仓库另附 SOXX、IAU 和 BTC 的 paper-trading 案例。

研究跨证据到达持续进行：记忆保留已有推理，新证据可以确认、修订或使判断失效。Analysis 不接触实际敞口，先判断 thesis；PM 再结合仓位、风险和执行条件决策。因此研究判断、仓位决策和已执行操作始终分开。

## 结果

![SOXX 模拟交易 PM 与 Direct 曲线](assets/paper-trading/soxx-selected.png)

![IAU 模拟交易 PM 与 Direct 曲线](assets/paper-trading/iau-selected.png)

| 模拟交易案例（2026 年，UTC） | PM | Direct | 决策报告 | 操作复盘 |
| --- | ---: | ---: | --- | --- |
| SOXX，6 月 4 日–9 月 5 日 | **+13.58%** | −1.49% | [阅读报告](data/paper-trading/soxx/REPORT.md) | [HTML 复盘](data/paper-trading/soxx/OPERATIONS.html) |
| IAU，7 月 20 日–8 月 24 日 | +4.23% | **+8.47%** | [阅读报告](data/paper-trading/iau/REPORT.md) | [HTML 复盘](data/paper-trading/iau/OPERATIONS.html) |
| BTC，9 月 11–15 日（完整可用重叠区间） | −0.81% | +1.14% | [阅读报告](data/paper-trading/btc/REPORT.md) | [HTML 复盘](data/paper-trading/btc/OPERATIONS.html) |

各案例包含决策报告和操作复盘，覆盖仓位、分段收益和费用情景。下载 HTML 后用浏览器打开，可使用交互图表；报告正文为英文。

PM 使用记录的模拟执行及持久化执行成本，Direct 是分析判断到仓位的假设映射。SOXX 截止点和 IAU 的 35 天区间为观察表现后选择，附[完整区间、CSV 和原 PM Console 使用说明](data/paper-trading/README.md)。BTC 当前重叠行情不足 30 天。这些案例与论文历史回放实验分开。

完整比较与实验设置见[论文](paper/event_trader.pdf)，保存结果和复现细节见 [评估与结果来源](docs/zh-CN/development/evaluation.md)。

## 系统流程

![论文中的 Event-Trader 框架](assets/framework.png)

| 能力 | 实际实现 | 详解 |
| --- | --- | --- |
| 有来源依据的证据 | Admission 记录身份和时间；append-only ledger 将证据与解释分开。 | [证据](docs/zh-CN/concepts/evidence.md) |
| 事件驱动研究 | Checker 判断是否深入；CEAU 形成有边界分析单元，记录关闭与调度原因。 | [Checker／CEAU](docs/zh-CN/concepts/checker-and-ceau.md) |
| 持续研究记忆 | Markdown 页面、引用和 thesis 版本跨事件保留研究。 | [研究记忆](docs/zh-CN/concepts/research-memory-and-thesis.md) |
| 受控推理流程 | 有预算读取、建议专家、单一 Analysis owner、结构化输出、语义校验与有限修复。 | [Agent harness](docs/zh-CN/development/agent-harness.md) |
| 结合仓位的 PM | PM 结合实际敞口和风险；确定性模拟执行记录成本与结果。 | [仓位与执行](docs/zh-CN/concepts/portfolio-and-execution.md) |
| 监控与反思 | Position gate／episode 支持审查；reflection 有明确资格和候选经验生命周期。 | [监控](docs/zh-CN/concepts/position-monitoring.md) · [反思](docs/zh-CN/concepts/reflection-and-learning.md) |
| 可检查的操作链路 | PM Console 关联数值表现、仓位、episode、决策和研究版本。 | [Console 指南](docs/zh-CN/guides/pm-console.md) |

设计将已接纳事实与可修订认知分开，也将确定性传输／时间／执行职责与 agent 解释分开。[工作流程与设计理念](docs/zh-CN/concepts/workflow.md)说明模块所有权。

Replay 与工具上下文使用明确的[时间可见性](docs/zh-CN/concepts/temporal-visibility.md)控制；[输出契约与 prompt 设计](docs/zh-CN/development/agent-contracts.md)约束可接受响应。这些机制增强控制与追踪能力，文档同时说明其实现边界。

## 文档导航

[中文文档](docs/zh-CN/index.md)与 [English documentation](docs/en/index.md)采用相同主题结构。

| 你的目标 | 建议阅读路径 |
| --- | --- |
| 查看公开样例 | [快速开始](docs/zh-CN/guides/quickstart.md) → [PM Console](docs/zh-CN/guides/pm-console.md) |
| 接自己的数据或运行标的 | [安装](docs/zh-CN/guides/installation.md) → [行情](docs/zh-CN/guides/market-data.md) → [Live](docs/zh-CN/guides/live-running.md)／[Replay](docs/zh-CN/guides/historical-replay.md) |
| 理解设计 | [流程](docs/zh-CN/concepts/workflow.md) → [Analysis](docs/zh-CN/concepts/analysis-and-context.md) → [研究记忆](docs/zh-CN/concepts/research-memory-and-thesis.md) |
| 扩展与维护 | [配置](docs/zh-CN/development/configuration.md) → [扩展](docs/zh-CN/development/extensions.md) → [运行状态](docs/zh-CN/development/runtime-and-state.md) → [验证](docs/zh-CN/development/contracts-and-testing.md) |

详细页面提供实现源码和对应语言链接。历史实验与 baseline 约定见[评估](docs/zh-CN/development/evaluation.md)。

## 安装

使用 Python 3.13+ 和 [uv](https://docs.astral.sh/uv/)。核心安装与 CLI 曾在 Windows 检查；完整 agent 运行还需要独立的 MiroThinker 环境及模型凭证。

```sh
git clone https://github.com/WAI0258/EventTrader.git
cd EventTrader/code
uv sync --frozen --no-dev
uv run event-trader --help
```

MiroThinker 源码在 `code/vendor/mirothinker/`。在 `code/` 安装独立环境：

```sh
uv sync --project vendor/mirothinker/apps/miroflow-agent --frozen --no-dev
```

完整模型环境尚未验证，详见[安装指南](docs/zh-CN/guides/installation.md)与[评估与结果来源](docs/zh-CN/development/evaluation.md)。

黄金 replay 示例为 [kernel.replay.gold.toml](code/config/kernel.replay.gold.toml)，包含 provider、agent、输入、workspace 和模拟执行设置。按配置在启动 shell 设置密钥：

```sh
export MINIMAX_API_KEY="your-key"
```

PowerShell：

```powershell
$env:MINIMAX_API_KEY = "your-key"
```

附加凭证取决于启用工具，变量名见 [code/.env.example](code/.env.example)。行情和带时间戳的证据需自行准备，仓库不再分发原始付费输入。

## 使用

### 查看模拟交易样例

原 PM Console 的 saved-result 模式不需要原始行情和 API 凭证：

```sh
cd code
uv run event-trader-pm-console --mount-catalog ../data/paper-trading/mounts.toml --saved-results
```

另开终端，在 `code/frontend/pm-console/` 执行 `npm ci` 和 `npm run dev`，打开 http://127.0.0.1:4173。选择标的查看 PM／Direct 和数值决策记录。保留数据及省略叙述见[样例指南](data/paper-trading/README.md)。

用户可接自己的 Futu 或兼容 provider，在相同区间增加 Buy & Hold，获取数据留在本地，不改变 PM／Direct。具体见[行情指南](docs/zh-CN/guides/market-data.md)。

### 历史回放

在 `code/` 查看选项：

```sh
uv run python -m event_trader.tools.replay_runtime run --help
```

准备所需行情 archive 和证据后，命令模板为：

```sh
uv run python -m event_trader.tools.replay_runtime run \
  --config config/kernel.replay.gold.toml \
  --dataset /path/to/evidence.jsonl \
  --target-key gold \
  --window-start 2026-01-01 \
  --window-end 2026-06-30 \
  --run-id gold-replay
```

这是模板，不是完整实验输入包。新运行使用独立 workspace；PowerShell 中用单行或其续行语法。输入与恢复细节见[回放指南](docs/zh-CN/guides/historical-replay.md)。

## 实验

2026 年 2 月 24 日–3 月 2 日的一周消融中，CEAU 相比逐条调用减少成功 Analysis 总运行时间 **64.2%**、成功 token 使用 **62.9%**。CEAU／逐条调用收益分别为 1.53%／1.44%，最大回撤分别为 0.48%／0.27%；逐条调用回撤更低。

仓库包含保存的 baseline、time-batch、CEAU 消融与事件案例。在根目录执行：

```sh
python -B tools/verify_results.py
python -B tools/plot_saved_results.py
```

绘图写到 `generated/`。这些命令检查保存结果，不重跑 agent。每次历史 source／config 的精确一致性和完整重跑仍未验证，口径见[评估说明](docs/zh-CN/development/evaluation.md)。

| 目录 | 内容 |
| --- | --- |
| `code/` | Runtime、配置、Console 与 MiroThinker 依赖 |
| `docs/` | 中英文指南、机制与开发文档 |
| `baselines/` | Adapter、评估与保存结果 |
| `experiments/` | Time-batch、消融与案例 |
| `tools/` | 离线核验和绘图 |
| `data/` | Schema、元数据与裁剪样例 |
| `paper/` | 论文 PDF |

## 引用

```bibtex
@unpublished{cai2026eventtrader,
  title = {Event-Trader: An Event-Driven LLM Research Runtime for Asynchronous Financial Evidence},
  author = {Cai, Jia Wei},
  year = {2026},
  note = {Manuscript},
  url = {https://github.com/WAI0258/EventTrader}
}
```

## 许可证

原创软件采用 [Apache-2.0](LICENSE)，第三方代码保留[上游许可证](THIRD_PARTY_NOTICES.md)。论文与数据／结果不属于软件许可证范围。
