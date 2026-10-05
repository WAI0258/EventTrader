# GCUSD Analysis-only vs PM Exposure

- window: `2026-01-01` through `2026-06-30`
- source workspace: `code\.local\server-gold-20260101-20260630\replay-gold-20260101_20260630-workspace`
- initial equity: `$100,000`
- transaction cost: `0.5` bps per side on executed notional
- Analysis-only mapping: `strong_long=1.0`, `weak_long=0.5`, `flat=0.0`, `weak_short=-0.5`, `strong_short=-1.0` from `as_if_flat_state`.
- PM mapping: executed `state-changes` `target_weight`; repeated PM reviews are not treated as separate trades.
- same 5-minute GCUSD bars and daily anchor calendar are used for both curves.

| Strategy | Assessments / Changes | Final Equity | Return | MDD | MDD Date | Fees | Actions |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| analysis_only | 637 | $137,679.26 | 37.68% | 10.34% | 2026-03-27 | $377.77 | 118 |
| pm | 39 | $129,326.81 | 29.33% | 10.46% | 2026-03-08 | $115.26 | 38 |

- figure: `eventtrader_analysis_vs_pm_progress.svg`
- data: `eventtrader_analysis_vs_pm_progress.csv`