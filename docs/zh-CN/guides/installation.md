# 安装与环境

[文档目录](../index.md) · [English](../../en/guides/installation.md)

运行时要求 Python 3.13 或更新版本。核心依赖覆盖 JSON Schema 校验、MCP、NautilusTrader 集成和交易日历；React／TypeScript Console 单独管理 Node／npm 依赖。

## 核心环境与前端

```sh
git clone https://github.com/WAI0258/EventTrader.git
cd EventTrader/code
uv sync --frozen --no-dev
uv run event-trader --help
```

Console 前端在 `code/frontend/pm-console/` 执行 `npm ci`，依赖按 lockfile 安装。[快速开始](quickstart.md)说明前后端分别启动的方法。

## 模型环境

仓库在 `code/vendor/mirothinker/` 包含 MiroThinker／MiroFlow 源码。现有独立环境安装路径为，在 `code/` 执行：

```sh
uv sync --project vendor/mirothinker/apps/miroflow-agent --frozen --no-dev
```

核心 `pyproject.toml` 也声明了 `mirothinker` 可选 extra 和本地 uv source 映射。声明存在不等于所有组合环境或 provider 配置都已验证。核心 CLI 曾在 Windows 检查；完整模型环境与历史实验一致性的边界见 [评估与结果来源](../development/evaluation.md)。

Futu 使用现有可选依赖：`uv sync --frozen --no-dev --extra futu`。还需要自己的账户权限和本地 OpenD。

## 密钥与路径

查看[变量模板](../../../code/.env.example)，按所选配置设置所需凭证。例如：

```powershell
$env:MINIMAX_API_KEY = "your-key"
```

```sh
export MINIMAX_API_KEY="your-key"
```

模板中的变量并非全部必需，也不意味着所有启动路径都会自动读取 `.env`。使用启动进程所在 shell 的环境变量。

配置示例从 `code/` 运行；切换目录前检查相对的 workspace、archive、vendor 和 log 路径。密钥与服务商数据保留在本地。`--help` 成功只说明命令可用，不说明模型或付费数据已经可访问。

## 按任务选择环境

| 任务 | 所需环境 |
| --- | --- |
| 阅读报告和操作 HTML | 浏览器，无需安装运行时 |
| 查看保存的 PM Console 样例 | 核心 Python 环境和 Console 前端 |
| 增加本地行情 archive B&H overlay | 核心环境和自己准备的匹配行情 archive |
| 通过富途获取行情 | 含 Futu extra 的核心环境、OpenD 和访问权限 |
| 运行 agent 研究 | 核心运行时及配置指定的模型／工具环境 |
| 开发或执行 Python 测试 | 含开发依赖的核心环境 |

`local_archive` provider 读取包含 `manifest.json` 和 `bars/<bar_granularity>/<symbol>.csv` 的正式 archive；任意 CSV 不是可直接使用的输入。Manifest 指定交易品种、交易所、时区与 bar 粒度。当前 provider 要求原始价格和 bar 结束时间，CSV 列为 `datetime`、`open`、`high`、`low`、`close`、`volume`、`amount`。配置 `validation.market_data.local_archive_root`，并在生成 B&H 前匹配样例的品种／时段／覆盖区间。[行情指南](market-data.md)说明 overlay 流程。

独立 MiroFlow 项目有自己的环境。安装核心不等于安装了该项目的依赖，前端依赖也不属于 Python 环境。诊断 provider 故障前，先确认实际采用的安装路径。

开发时在 `code/` 执行 `uv sync --frozen`；不加 `--no-dev` 会包含声明的开发依赖组。使用随仓库提供的 lockfile 解析版本；若平台不兼容，保留错误并定位具体包与平台差异，再决定是否调整版本。

## 实现依据

- [code/src/event_trader/market/local_archive_provider.py](../../../code/src/event_trader/market/local_archive_provider.py)
- [code/pyproject.toml](../../../code/pyproject.toml)
- [code/.env.example](../../../code/.env.example)
- [code/frontend/pm-console/package.json](../../../code/frontend/pm-console/package.json)
- [code/src/event_trader/integrations/mirothinker_runtime_paths.py](../../../code/src/event_trader/integrations/mirothinker_runtime_paths.py)

[文档目录](../index.md) · [English](../../en/guides/installation.md)
