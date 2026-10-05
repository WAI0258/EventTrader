# Gold Baseline Evaluation

## Scope

- reported window: `2026-01-01` through `2026-06-30`
- instrument: `GCUSD`
- primary FinMem metric: repository action-score, `buy=+1`, `hold/empty=0`, `sell=-1`, applied to the current-close to next-close interval
- primary FinMem cost overlay: `0.500` bps per side whenever the signed action changes
- other baselines: next available `GCUSD` 5-minute bar open after each daily EOD decision
- transaction cost overlay: `0.500` bps per side on executed notional
- action count: actual evaluated exposure changes count once for an open, close, add, reduce, or material rebalance; a reversal that closes one side and opens the opposite side counts as two actions. Records labeled `Hold` or `Unavailable`, including evaluator-generated Hold rebalancing, do not count. For FinMem, `action_score:+0` counts when it closes a prior evaluated `+1` or `-1` exposure; a true `0 -> 0` no-op is not logged. Floating-point residue below `1e-08` turnover also does not count. A Buy-and-Hold entry plus terminal liquidation counts as two actions.
- fee definition: `1 bps = 0.01%`; at `0.5` bps per side, a `$100,000` notional open or close costs `$5`, and a complete round trip costs `$10`. Fees are charged on executed notional rather than margin or cash balance.
- primary FinMem annualization factor: `252` observations/year
- other-baseline annualization factor: `310.552` observations/year

## FinMem Evaluation Boundary

- `FinMem` primary: evaluated with the repository's signed daily action-score. This is the paper/repository metric and is not the native portfolio inventory carried internally for feedback.
- `AI Hedge Fund`: reuses the original completed `buy / sell / short / cover / hold` decisions and their native post-trade state. That resulting `long / flat / short` state maps to `+100% / 0% / -100%` gross exposure; no LLM is re-run.
- `TradingAgents`: final portfolio-manager rating is compressed to AMA's three-way `recommended_action` and evaluated with the AMA public stateful long-short convention:
  - `Buy` / `Overweight` -> full long exposure
  - `Hold` -> maintain exposure
  - `Underweight` / `Sell` -> full short exposure

This is the canonical AMA baseline for this Gold run. The internal Trader proposal is retained only as a diagnostic and is not used as the action source.

Trader `Buy / Hold / Sell` is preserved separately as a diagnostic layer. It is not forced into `short` semantics.

## Trading Metrics

| Baseline | Total Return | Annualized Return | Sharpe | Sortino | Max Drawdown | Hit Rate | Avg Gross Exposure | Turnover | Long / Flat / Short | Actions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ai_hedge_fund | 2.96% | 6.06% | 0.351 | 0.407 | -19.74% | 50.68% | 0.622 | 110.541 | 39.6% / 37.7% / 22.7% | 127 |
| bollinger_20_2 | -6.94% | -13.51% | -0.419 | -0.454 | -13.43% | 28.57% | 0.356 | 9.238 | 26.6% / 64.3% / 9.1% | 30 |
| buy_and_hold | -6.96% | -13.53% | -0.264 | -0.307 | -25.30% | 0.00% | 0.987 | 2.000 | 98.7% / 1.3% / 0.0% | 2 |
| finmem | 11.72% | 20.03% | 0.827 | 0.767 | -12.28% | n/a | 0.458 | 58.000 | 36.6% / 54.2% / 9.2% | 58 |
| ma_10ema_50sma | 22.02% | 49.38% | 1.398 | 1.733 | -13.11% | 70.18% | 0.993 | 8.869 | 43.5% / 0.6% / 55.8% | 95 |
| macd_12_26_9 | -14.79% | -27.58% | -0.750 | -0.956 | -27.17% | 37.50% | 0.993 | 27.363 | 41.6% / 0.6% / 57.8% | 125 |
| rsi_14 | -16.13% | -29.86% | -0.911 | -1.430 | -17.85% | 2.86% | 0.895 | 4.643 | 56.5% / 10.4% / 33.1% | 56 |
| tradingagents | 18.97% | 41.94% | 1.233 | 1.955 | -11.64% | 67.65% | 0.854 | 21.378 | 32.5% / 14.3% / 53.2% | 30 |
| tsmom_20d | 20.46% | 45.56% | 1.270 | 1.668 | -15.82% | 73.02% | 0.993 | 17.021 | 44.8% / 0.6% / 54.5% | 104 |

## Runtime And Cost Audit

| Baseline | Eval Mode | Raw Decisions | Aligned Decisions | Excluded | Synthetic Holds | LLM Calls | Tokens In | Tokens Out | Avg Latency (s) | P90 Latency (s) | Transient Failures |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ai_hedge_fund | native_state_to_full_exposure | 129 | 128 | 1 | 26 | 772 | 0 | 0 | 46.031 | 53.959 | 0 |
| finmem | repository_action_score_same_close | 154 | 154 | 0 | 0 | 216 | 627612 | 207262 | 30.216 | 49.313 | 0 |
| tradingagents | ama_recommended_action_stateful_long_short | 154 | 154 | 0 | 0 | 2465 | 15832880 | 2809857 | 332.959 | 407.040 | 17 |
| buy_and_hold | deterministic_price_rule | 1 | 1 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 |
| tsmom_20d | deterministic_price_rule | 154 | 154 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 |
| ma_10ema_50sma | deterministic_price_rule | 154 | 154 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 |
| rsi_14 | deterministic_price_rule | 154 | 154 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 |
| macd_12_26_9 | deterministic_price_rule | 154 | 154 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 |
| bollinger_20_2 | deterministic_price_rule | 154 | 154 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 |

## Action Distribution

| Baseline | Family | Action | Count | Ratio |
| --- | --- | --- | --- | --- |
| ai_hedge_fund | native_action | buy | 43 | 33.33% |
| ai_hedge_fund | native_action | sell | 32 | 24.81% |
| ai_hedge_fund | native_action | short | 27 | 20.93% |
| ai_hedge_fund | native_action | cover | 19 | 14.73% |
| ai_hedge_fund | native_action | hold | 8 | 6.20% |
| ai_hedge_fund | native_state | long | 51 | 39.53% |
| ai_hedge_fund | native_state | flat | 48 | 37.21% |
| ai_hedge_fund | native_state | short | 30 | 23.26% |
| finmem | native_action | hold | 60 | 38.96% |
| finmem | native_action | buy | 56 | 36.36% |
| finmem | native_action | None | 23 | 14.94% |
| finmem | native_action | sell | 15 | 9.74% |
| tradingagents | pm_rating | Hold | 127 | 82.47% |
| tradingagents | pm_rating | Underweight | 15 | 9.74% |
| tradingagents | pm_rating | Overweight | 7 | 4.55% |
| tradingagents | pm_rating | Sell | 4 | 2.60% |
| tradingagents | pm_rating | Buy | 1 | 0.65% |
| tradingagents | trader_action | Hold | 126 | 81.82% |
| tradingagents | trader_action | Sell | 16 | 10.39% |
| tradingagents | trader_action | Buy | 9 | 5.84% |
| tradingagents | trader_action | Unavailable | 3 | 1.95% |
| buy_and_hold | rule_target | long | 1 | 100.00% |
| tsmom_20d | rule_target | short | 85 | 55.19% |
| tsmom_20d | rule_target | long | 69 | 44.81% |
| ma_10ema_50sma | rule_target | short | 87 | 56.49% |
| ma_10ema_50sma | rule_target | long | 67 | 43.51% |
| rsi_14 | rule_target | long | 88 | 57.14% |
| rsi_14 | rule_target | short | 51 | 33.12% |
| rsi_14 | rule_target | flat | 15 | 9.74% |
| macd_12_26_9 | rule_target | short | 90 | 58.44% |
| macd_12_26_9 | rule_target | long | 64 | 41.56% |
| bollinger_20_2 | rule_target | flat | 98 | 63.64% |
| bollinger_20_2 | rule_target | long | 42 | 27.27% |
| bollinger_20_2 | rule_target | short | 14 | 9.09% |

## TradingAgents Diagnostic

- stored PM decisions: `154`
- PM `Underweight` with Trader `Hold`: `3`

This diagnostic exists because TradingAgents' final PM rating and Trader execution proposal can diverge in the official report tree.

## Artifacts

- trading summary: `trading_summary.csv`
- runtime summary: `runtime_summary.csv`
- action distribution: `action_distribution.csv`
- TradingAgents diagnostics: `tradingagents_decision_diagnostics.csv`
- equity curve: `equity_curve.csv`
- daily positions: `position_timeline.csv`
- executed orders: `trade_log.csv`
- equity curve chart: `equity_curve.svg`

All files are written under:

```text
baselines\experiments\gold_baseline_eval_20260101_20260630
```
