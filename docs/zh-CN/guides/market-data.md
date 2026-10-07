# 行情接入与 Buy & Hold

[文档目录](../index.md) · [English](../../en/guides/market-data.md)

行情是研究上下文、执行、估值与监控的确定性输入。Provider 要返回所需标的和正确的 bar 语义，换数据源不只是换 HTTP 地址。

## 数据职责

主要接口为 `MarketBarsProvider.read_series`。Provider adapter、标的映射、订阅用途、交易时段和复权规则共同定义序列的含义。实现包含 Futu、Binance spot、Hyperliquid perpetuals 与本地档案；权限和历史覆盖取决于来源。

每个目标只有一个 primary tradable；其他订阅提供大盘、利率、波动率或跨资产上下文。上下文标的不会自动成为交易标的。

共享 live 数据采用 SQLite 和 observed-prefix 读取。陈旧或不完整的观测前缀不能视作完整覆盖。排查差异时要对齐 symbol、provider、session、granularity 和 price basis。

## 给公开样例增加 B&H

股票需要安装 Futu extra 并启动自己的已认证 OpenD。在 `code/` 执行：

```sh
uv run python -m event_trader.tools.pm_console_buy_hold --config config/kernel.console.soxx.toml --target sox
uv run python -m event_trader.tools.pm_console_buy_hold --config config/kernel.console.iau.toml --target gold
uv run python -m event_trader.tools.pm_console_buy_hold --config config/kernel.console.btc.toml --target btc
uv run event-trader-pm-console --mount-catalog ../data/paper-trading/mounts.toml --saved-results --buy-hold-results-dir .local/buy-hold
```

命令在本地保存归一化 B&H 结果、provider、配置的买入成本与样例哈希，不覆盖 PM／Direct，也不导出原始 OHLCV。覆盖检查要求完整区间的 bar 边界，包括中间点。

BTC 样例是 Hyperliquid 永续，Binance 现货不是同一标的。API 无法提供历史区间时，可使用匹配的本地档案；新增 API 需要 adapter。

B&H 是包含入场成本的价格收益，不是含分红／资金费的总收益。Provider 约定和错误处理见[样例指南](../../../data/paper-trading/README.md#compare-with-buy--hold-using-your-own-market-data)。

## 行情上下文不只是主标的曲线

配置 context profile 可启用 `price_volume`、`technical`、`macro_cross_asset`、`derivatives`。Builder 确定性计算各表面并报告可用状态。

Price／volume 和 path 描述观测序列；technical 模块包含 EMA、RSI、Bollinger、Ichimoku 和 divergence helper。Cross-asset 使用带 purpose 的美元、利率、大盘或波动率 proxy。Derivatives 需要 underlying symbol、option provider 能力与 selection policy；开启配置不意味着缺失期权数据会被补造。

这些是选定序列的测量事实，不是预先写好的“消息已 priced in”结论。Workbench 刻意将行情测量与模型解释分开。

## 对齐工具与价格

| 身份 | 示例 | 作用 |
| --- | --- | --- |
| Business target | `gold`、`sox`、`btc` | 研究／仓位所有权 |
| Tradable proxy | IAU、SOXX、BTC 永续 | 实际估值价格对应的工具 |
| Session | Extended exchange 或 continuous | 合法 bar 边界 |
| Adjustment basis | Raw 或支持的 visible adjustment | 历史价位／入场是否可比 |
| Cost basis | 配置执行成本或 B&H 入场成本 | 哪些费用已计入 |

BTC 永续和现货即使同名，也可能价格和持有成本不同；ETF 价格收益也不同于含分红收益。

示例 B&H 从 100 涨到 110，入场成本为 10 bps，归一化期末权益为 `1.1 / 1.001`，约 9.89% 收益。这只解释入场成本算法，不是标的实测结果，也不是再从保存 PM 曲线扣一次费用。

## 实现依据

- [code/src/event_trader/market/provider.py](../../../code/src/event_trader/market/provider.py)
- [code/src/event_trader/market/shared_store.py](../../../code/src/event_trader/market/shared_store.py)
- [code/src/event_trader/market/session_policy.py](../../../code/src/event_trader/market/session_policy.py)
- [code/src/event_trader/tools/pm_console_buy_hold.py](../../../code/src/event_trader/tools/pm_console_buy_hold.py)

[文档目录](../index.md) · [English](../../en/guides/market-data.md)
