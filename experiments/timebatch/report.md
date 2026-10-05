# Event-driven vs Time-batched Analysis Evaluation

- window: `2026-02-04T00:00:00+00:00` through `2026-03-05T23:59:59.999999+00:00`
- initial state at the window boundary: `flat` with zero scored exposure
- pre-window analysis states: ignored
- signal source: `AnalysisAssessment.as_if_flat_state`
- weights: `strong_long=1.0`, `weak_long=0.5`, `flat=0.0`, `weak_short=-0.5`, `strong_short=-1.0`
- execution: each visible state is applied at the next available five-minute GCUSD bar
- transaction cost: `0.5` bps per side on executed notional
- Sharpe/Sortino annualization factor: `310.552` GCUSD observations per year
- common canonical bar count: `6029`
- all time-batch market ledgers were checked against the event-driven ledger within the evaluation window

| Variant | Return | Sharpe | Sortino | MDD | Analyses | Actions | Fees |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Event-driven | **0.47%** | **0.423** | **0.684** | **3.58%** | 123 | 31 | $81.17 |
| Time batch (5 min) | -5.24% | -3.094 | -2.695 | 6.22% | 144 | 25 | $61.06 |
| Time batch (1 h) | -0.22% | -0.021 | -0.021 | 7.37% | 127 | 25 | $65.40 |
| Time batch (4 h) | -4.71% | -2.416 | -2.064 | 4.94% | 90 | 18 | $48.79 |
| Time batch (1 day) | -5.70% | -3.982 | -3.686 | 5.70% | 26 | 15 | $36.66 |

## Decision boundaries

- Event-driven: `123` assessments, first `2026-02-04T02:09:22+00:00`, last `2026-03-05T16:07:57+00:00`.
- Time batch (5 min): `144` assessments, first `2026-02-04T00:04:59+00:00`, last `2026-03-05T15:39:59+00:00`.
- Time batch (1 h): `127` assessments, first `2026-02-04T00:59:59+00:00`, last `2026-03-05T15:59:59+00:00`.
- Time batch (4 h): `90` assessments, first `2026-02-04T03:59:59+00:00`, last `2026-03-05T15:59:59+00:00`.
- Time batch (1 day): `26` assessments, first `2026-02-04T23:59:59+00:00`, last `2026-03-05T23:59:59+00:00`.

## Artifacts

- summary: `summary.csv`
- daily curves: `daily_equity.csv`
- paper figure: `code\docs\images\eventdriven_vs_timebatch_analysis.pdf`
- preview: `code\docs\images\eventdriven_vs_timebatch_analysis.png`
