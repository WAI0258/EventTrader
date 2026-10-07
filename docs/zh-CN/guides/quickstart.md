# 快速开始

[文档目录](../index.md) · [English](../../en/guides/quickstart.md)

先从仓库附带的 paper-trading 样例开始。这条路径只需要核心运行环境和前端，不需要模型密钥、原始行情档案或 Futu OpenD。

## 启动 Console

在仓库根目录执行：

```sh
cd code
uv sync --frozen --no-dev
uv run event-trader-pm-console --mount-catalog ../data/paper-trading/mounts.toml --saved-results
```

另开终端：

```sh
cd code/frontend/pm-console
npm ci
npm run dev
```

打开 http://127.0.0.1:4173，选择 SOXX（`sox`）、IAU（`gold`）或 BTC（`btc`）。前端与 Python API 是两个进程，都要保持运行。

## 建议先看什么

1. 在相同起止时间下比较 PM 与 Direct 曲线。
2. 查看仓位与 episodes，沿保留的数值记录追踪决策和执行。
3. 阅读对应标的的[决策报告和 HTML 操作复盘](../../../data/paper-trading/README.md)。下载 HTML 后用浏览器打开，可使用交互图表。

PM 使用已记录的模拟执行和持久化执行成本；Direct 将分析判断映射为假设仓位。研究建议、PM 决策、已执行操作是不同记录。

样例省略研究正文、私人操作员笔记、新闻正文和服务商原始 OHLCV。叙述面板为空或标记省略，不代表原运行没有研究内容。saved-results 模式拒绝写请求，也不运行 agent；样例不能直接作为可恢复的 live 工作区。

## 后续路径

通过[行情指南](market-data.md)生成本地 Buy & Hold 对比，保留原 PM／Direct 曲线。准备好自己的输入与环境后，再使用[实时运行](live-running.md)或[历史回放](historical-replay.md)。

浏览器无法加载标的时，先检查 API 终端，再检查前端终端。对比结果被拒绝时，应检查样例哈希与行情覆盖，不要缩短区间来绕过检查。

## 五分钟完成第一次检查

先选一个标的，不必同时浏览所有面板。读报告的时间区间，找到对应的 PM 和 Direct 曲线，再沿一个 episode 从决策查看到已记录操作。对照操作前后的组合状态，比单独看图上的标记更能解释仓位变化。

API 默认地址为 `http://127.0.0.1:8765`，前端默认端口为 `4173`。前端打开但加载不到标的时，先检查 API 地址和 mount catalog。如果修改 API 监听地址，在启动 Vite 前设置前端环境变量 `VITE_PM_CONSOLE_API_BASE_URL`。

| 现象 | 应如何理解 |
| --- | --- |
| 没有模型密钥也能加载曲线 | 正在读取保存的数值结果 |
| 叙述面板没有内容 | 公开样例排除了这部分材料 |
| 图左端已有仓位 | 区间可能继承之前的敞口 |
| 没有 B&H 线 | 自行提供覆盖区间的行情并生成 overlay |

所有曲线应采用相同日期。即使本地行情服务商提供了更新的数据，saved-result 图仍是在查看已发布样例。

## 实现依据

- [code/src/event_trader/tools/pm_console_server.py](../../../code/src/event_trader/tools/pm_console_server.py)
- [code/src/event_trader/tools/pm_console_mounts.py](../../../code/src/event_trader/tools/pm_console_mounts.py)
- [data/paper-trading/README.md](../../../data/paper-trading/README.md)

[文档目录](../index.md) · [English](../../en/guides/quickstart.md)
