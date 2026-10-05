# GCUSD EventTrader vs Agent Baselines

- window start: `2026-01-01`
- auto-detected window end: `2026-06-30`
- initial equity: `$100,000`
- transaction-cost assumption: `0.500` bps per side on executed notional
- action definition: actual evaluated exposure changes count once for an open, close, add, reduce, or material rebalance; a reversal that closes one side and opens the opposite side counts as 2 actions. Records labeled Hold or Unavailable, including evaluator-generated Hold rebalancing, are excluded. For FinMem, an `action_score:+0` record counts when it closes a prior evaluated `+1` or `-1` exposure; a true `0 -> 0` no-op is not logged. Floating-point residue below `1e-8` turnover is also excluded. Buy & Hold is counted as entry plus terminal liquidation, so it has 2 actions.
- fee definition: `1 bps = 0.01%`; at `0.5` bps per side, a `$100,000` notional open or close costs `$5`, and a complete round trip costs `$10`. Fees use executed notional, not margin or cash balance.
- Buy & Hold is included as a passive reference and is not an agent baseline
- event-trader note: this figure is clipped to the current EventTrader progress window, so the comparison is an in-progress snapshot rather than a full-period final ranking

| Rank | Strategy | Final Equity | Total Return | Excess vs Buy & Hold | Max Drawdown | MaxDD Date | Fees | Fees / Gross PnL | Actions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | event_trader | $137,679.26 | 37.68% | 44.64% | 10.34% | 2026-03-27 | $377.77 | 0.99% | 118 |
| 2 | tradingagents | $118,965.49 | 18.97% | 25.92% | 11.64% | 2026-02-17 | $115.18 | 0.60% | 30 |
| 3 | finmem | $111,723.37 | 11.72% | 18.68% | 12.28% | 2026-02-01 | $329.31 | 2.73% | 58 |
| 4 | ai_hedge_fund | $102,958.20 | 2.96% | 9.91% | 19.74% | 2026-05-10 | $544.44 | 15.54% | 127 |
| 5 | buy_and_hold | $93,043.34 | -6.96% | 0.00% | 25.30% | 2026-06-24 | $9.65 | -0.14% | 2 |

## Reading Guide

- `Excess vs Buy & Hold` is the return spread versus the passive reference over the same window.
- `Fees / Gross PnL` uses `fees / (final_equity - initial_equity + fees)` and is shown only when gross PnL is non-zero.
- `MaxDD Date` is the date when the worst peak-to-trough equity drawdown is observed within the clipped window.
- The Buy & Hold terminal liquidation is included so its two-sided fee treatment matches the action definition.

## Artifacts

- figure: `eventtrader_vs_baselines_progress.svg`
- data: `eventtrader_vs_baselines_progress.csv`