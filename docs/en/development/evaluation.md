# Evaluation and result provenance

[Documentation index](../index.md) · [简体中文](../../zh-CN/development/evaluation.md)

The repository contains several kinds of results. Compare them only after aligning the decision, execution, valuation, cost, and time conventions.

## Result families

| Family | Interpretation |
| --- | --- |
| Public PM/Direct samples | Recorded paper operations versus hypothetical research mapping |
| Gold baseline study | Saved historical results under project-specific adapters and evaluation |
| Time-batch study | Event-driven and fixed-clock input variants |
| CEAU ablation | Per-record invocation versus bounded-unit formation |
| Event case figures | Selected reaction examples and hypothetical forward measurements |

The homepage's SOXX cutoff and IAU interval were selected after observing performance; full available intervals accompany them. BTC has fewer than 30 days of overlapping prices. Sample timestamps also include delayed persistence/catch-up; they do not establish that every operation was recorded contemporaneously.

## Adapter and measurement details

TradingAgents, FinMem, and AI Hedge Fund adapters supply local inputs and map outputs to a shared task. That mapping is part of the experiment, not evidence of identical native environments. FinMem's training warmup can expose future records while test mode disables that setting; its signed action-score evaluation differs from native inventory accounting.

Historical execution configuration and evaluator fees are separate. The gold configuration has zero execution fees; the evaluator applies its stated overlay. Public PM curves retain their own persisted execution costs.

Selected event figures use hypothetical forward PnL rather than actual PM trades. Cases selected for favorable outcomes illustrate behavior, not representative win rates. Peak/excursion annotations are not realized profit.

## Verify the saved artifacts

```sh
python -B tools/verify_results.py
```

Run from the repository root. Successful-analysis runtime/token sums are not whole-system end-to-end latency. Match outcomes to successful task logs and preserve documented exclusions.

The calendar time-batch builder has a fractional-second boundary edge: an event at `00:04:59.900` can be assigned a packet time of `00:04:59`. This is an implementation limitation; it does not establish that saved experiments contained affected rows.

Use the linked manifests, provenance and actual evaluators to identify input scope and historical version references.

## Metric definitions and provenance

The evaluation module compounds period returns for cumulative return and uses the configured annual periods to annualize return and volatility. Volatility uses sample standard deviation. Sharpe is absent when there are too few observations or zero excess-return variance; Calmar is absent when drawdown is zero. These undefined values should not be displayed as measured zeros.

Maximum drawdown follows a running equity peak. Alpha/beta require aligned return inputs and nonzero benchmark variance; matching array lengths alone does not establish calendar alignment. Keep the asset's session and sampling frequency explicit instead of automatically assuming 252 observations per year for every series.

The numerical verifier applies the conventions of each saved study. A historical evaluator's overlay is different from execution costs already embedded in a PM curve; charging both again changes the result.

[Experiment provenance](../../../experiments/provenance.json) records source and upstream references. [The input manifest](../../../data/experiment_inputs_manifest.json) describes exported input metadata, not the omitted raw corpus. These artifacts help identify the published material; they do not prove the exact code/configuration of every historical run. Use your own permitted inputs and preserve your run-specific configuration for new experiments.

## Implementation

- [code/src/event_trader/evaluation/metrics.py](../../../code/src/event_trader/evaluation/metrics.py)
- [tools/verify_results.py](../../../tools/verify_results.py)
- [baselines](../../../baselines)
- [experiments](../../../experiments)
- [code/scripts/build_calendar_timebatch_dataset.py](../../../code/scripts/build_calendar_timebatch_dataset.py)

[Documentation index](../index.md) · [简体中文](../../zh-CN/development/evaluation.md)
