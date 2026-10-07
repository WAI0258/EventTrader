# Market data and Buy & Hold

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/market-data.md)

Market data is a deterministic input to research context, execution, valuation, and monitoring. A provider must return the requested instrument and bar semantics; changing a data source is not just changing an HTTP URL.

## Data responsibilities

`MarketBarsProvider.read_series` is the primary bar seam. Provider adapters, target mappings, subscription purposes, session policies, and adjustment rules specify what a series means. The implementation includes Futu, Binance spot, Hyperliquid perpetuals, and local archives. Access and historical coverage depend on the selected source.

A target has one primary tradable subscription; other subscriptions can provide broad-market, rates, volatility, or cross-asset context. Context symbols do not automatically become traded assets.

Shared live data uses SQLite and observed-prefix reads. A stale or partial observed prefix is not silently equivalent to complete coverage. Match symbol, provider, session, granularity, and price basis when investigating a discrepancy.

## Add B&H to the public samples

For equities, install the Futu extra and run your own authenticated OpenD. From `code/`:

```sh
uv run python -m event_trader.tools.pm_console_buy_hold --config config/kernel.console.soxx.toml --target sox
uv run python -m event_trader.tools.pm_console_buy_hold --config config/kernel.console.iau.toml --target gold
uv run python -m event_trader.tools.pm_console_buy_hold --config config/kernel.console.btc.toml --target btc
uv run event-trader-pm-console --mount-catalog ../data/paper-trading/mounts.toml --saved-results --buy-hold-results-dir .local/buy-hold
```

The command writes normalized B&H results locally, including provider, configured buy-side entry cost, and a hash of the saved sample. It does not overwrite PM/Direct results or export raw OHLCV. Coverage checks require the full sample's bar boundaries, including intermediate points.

The BTC sample is a Hyperliquid perpetual. Binance spot is not an interchangeable instrument. A local matching archive can supply otherwise unavailable history; a new API needs an adapter.

B&H is price return with entry cost, not a dividend/funding-inclusive total return. See the [sample guide](../../../data/paper-trading/README.md#compare-with-buy--hold-using-your-own-market-data) for provider conventions and failure cases.

## Market context is more than the tradable's chart

A configured context profile can enable `price_volume`, `technical`, `macro_cross_asset`, and `derivatives`. The builder computes these surfaces deterministically and reports their availability.

Price/volume and path context describe the observed series. Technical calculation modules include EMA, RSI, Bollinger, Ichimoku, and divergence helpers. Cross-asset context uses purpose-labelled subscriptions, such as dollar, rates, broad market, or volatility proxies. Derivatives require an underlying symbol, option-provider capability, and selection policy; enabling a flag does not fabricate missing option data.

These calculations are facts about a chosen data series, not prewritten conclusions that news is “priced in.” The Workbench deliberately keeps market measurements separate from the model's interpretation.

## Keep instruments and prices comparable

| Identity | Example | Why it matters |
| --- | --- | --- |
| Business target | `gold`, `sox`, `btc` | Research/portfolio ownership |
| Tradable proxy | IAU, SOXX, BTC perpetual | The instrument whose price is valued |
| Session | Extended exchange session or continuous | Which bar boundaries are valid |
| Adjustment basis | Raw or supported visible adjustment | Whether historical levels/entries are comparable |
| Cost basis | Configured execution cost or B&H entry cost | Which charges are already included |

A BTC perpetual and BTC spot can have different prices and carrying costs even with the same label. An ETF price return also differs from a dividend-inclusive result.

For an illustrative B&H series rising from 100 to 110 with a 10 bps entry cost, normalized terminal equity is `1.1 / 1.001`, about a 9.89% return. This example explains the entry-cost calculation; it is not a reported asset result or an additional fee to subtract from the saved PM curve.

## Implementation

- [code/src/event_trader/market/provider.py](../../../code/src/event_trader/market/provider.py)
- [code/src/event_trader/market/shared_store.py](../../../code/src/event_trader/market/shared_store.py)
- [code/src/event_trader/market/session_policy.py](../../../code/src/event_trader/market/session_policy.py)
- [code/src/event_trader/tools/pm_console_buy_hold.py](../../../code/src/event_trader/tools/pm_console_buy_hold.py)

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/market-data.md)
