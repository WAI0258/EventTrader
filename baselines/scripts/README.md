# GCUSD Baseline Scripts

This directory contains the shared runners and evaluators for the GCUSD baseline study. Generated data and reports belong under `../experiments`, never in this directory.

## Current Evaluation Entry Points

| Command | Purpose |
| --- | --- |
| `evaluate_gold_baselines.py` | Regenerates the shared GCUSD baseline table, curves, positions, and trade logs from completed artifacts. |
| `render_eventtrader_vs_baselines_progress.py --end-date 2026-06-30` | Renders the EventTrader versus baselines progress figure from the current evaluation output. |
| `prepare_gcusd_experiment_inputs.py` | Builds the frozen GCUSD experiment inputs. |
| `backfill_gold_baseline_native_actions.py` | Historical audit normalizer; preserves native action/state fields used by completed artifacts. |

## Canonical AHF Baseline

- Source audit archive: `../experiments/ai_hedge_fund_gcusd_full_20260101_20260630`
- Reported mapping: stored native post-trade state -> full `long / flat / short` exposure
- AHF protocol and rebuild instructions: `../experiments/ai_hedge_fund_gcusd_full_20260101_20260630/README.md`
- Internal mapping comparison: `../experiments/ai_hedge_fund_gcusd_full_20260101_20260630/MAPPING_REVIEW.md`

From the repository root:

```powershell
.\ai-hedge-fund\.venv\Scripts\python.exe .\scripts\evaluate_gold_baselines.py
.\ai-hedge-fund\.venv\Scripts\python.exe .\scripts\render_eventtrader_vs_baselines_progress.py --end-date 2026-06-30
```

These commands are read-only with respect to completed model decisions: they rebuild evaluation artifacts only.
