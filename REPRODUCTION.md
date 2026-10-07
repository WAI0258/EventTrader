# Reproduction boundaries and commands

## Offline, available now

`python -B tools/verify_results.py` recomputes reported statistics from the saved curves and the sanitized CEAU analysis records. `python -B tools/plot_saved_results.py` redraws the main agent comparison and timebatch comparison using only standard-library Python.

## Full experiment regeneration, requires additional inputs

1. Supply licensed GCUSD market bars spanning 2025-06-30 through 2026-06-30 (warmup precedes the reported 2026H1 window), including the five-minute ledger and derived daily bars.
2. Restore legally usable evidence records with their original visibility timestamps and source metadata. The original news bodies and paid-provider data are intentionally not redistributed. `data/` records hashes of the original input manifests; it is not the dataset.
3. Establish the exact historical configuration and run revision for each experiment. The July 17 source retained in `paper-v1` is a candidate snapshot, not a verified per-run execution receipt; the updated development source is not a replacement for that historical provenance. The current gold config alone does not specify every timebatch/ablation variant.
4. Prepare native baselines at the upstream revisions in `experiments/provenance.json`. Local modifications and selected Python helpers are supplied as overlays. Inspect their provenance before overwriting cloned upstream files; current overlays are not proven identical to historical run modifications. Baseline summaries contain sanitized decision/cost metadata; raw reasoning and inputs were removed.
5. Install each baseline's environment using its upstream requirements. Retain TradingAgents, FinMem and AHF native action/execution semantics described in the paper and archived evaluation reports.
6. From the prepared baseline tree, run `python scripts/prepare_gcusd_experiment_inputs.py`, followed by the appropriate native runner, then `python scripts/evaluate_gold_baselines.py`. These are historical entry points, not a complete one-command rerun recipe.
7. In the prepared Event-Trader `code/` tree inspect `python -m event_trader.tools.replay_runtime --help` and `python -m event_trader.tools.baseline_evaluation --help`. Supply the restored dataset, price snapshot, workspace and verified variant configuration. Timebatch construction scripts are in `code/scripts/`.

Historical figure scripts in `experiments/` still require excluded workspaces and raw data. Their root paths were adapted in this copy, but they are not offline verification commands. Prefer `tools/plot_saved_results.py` for plots from exported results.

Do not overwrite saved results while exploring a new run. Use a separate output workspace. No model calls or baseline runs were executed during packaging.

## Located native GCUSD FinMem entry point

The genuine paper-compatible adapter is now included in `baselines/replication/finmem/paper_compat/`, rather than only the earlier TSLA replication helpers. From the prepared `baselines/` tree, use `python replication/finmem/paper_compat/run_gcusd.py --help`, then supply explicit `--daily-ohlcv`, `--news-archive`, `--config-path` and a separate `--output-dir`. This needs a cloned, clean FinMem `puppy` vendor at the recorded revision, its Python environment, chat/embedding credentials and licensed inputs. The config preserves the historical third-party transport endpoint; its availability and equivalence have not been verified. Do not replace it silently and claim an identical run.

`EXPERIMENT_RECEIPT.json` records the corroborating vendor/config evidence. `data/SOURCE_INVENTORY.json` lists verified local market input hashes and row counts without publishing the rows. Original raw market and news inputs are not distributed in this repository.

## Core dependency correction

The historical uv.lock did not include nautilus_trader even though pyproject.toml required it. The public export regenerates the core lockfile to include the declared dependency. This packaging correction does not establish the exact dependency environment used in historical experiments. MiroThinker dependencies are separate; there is no mirothinker extra in the core project.

## Verified installation boundary

On 2026-10-05, the regenerated core lockfile installed successfully in an independent Python 3.13.14 environment. The installed event-trader CLI, replay CLI and baseline evaluation CLI displayed help successfully. No model calls were performed. Full MiroThinker model-runtime installation and baseline environment installation have not been validated. 

The vendored MiroThinker app historical lockfile also disagreed with its declared e2b-code-interpreter pin (1.5.2 versus 1.2.1). Its exported lockfile was reconciled to the declared 1.2.1 version without changing runtime source. This is a packaging correction, not proof of the original experiment environment.

## Experiment-to-artifact map

| Manuscript evidence | Saved artifact | Verification level |
| --- | --- | --- |
| Six-month trading comparison | `baselines/experiments/gold_baseline_eval_20260101_20260630/{equity_curve,eventtrader_vs_baselines_progress,trading_summary,trade_log}.csv` | Offline final returns, drawdown and common-calendar risk metrics |
| Native agent cost | Same directory's `runtime_summary.csv`; AHF's separate `cost_report.json` | Saved report; Event-Trader's full 879-call log archive excluded |
| One-month timebatch comparison | `experiments/timebatch/{summary,daily_equity,analysis_cost_clean}.csv` | Offline curves vs summary |
| CEAU formation ablation | `experiments/ceau_ablation/` | Successful-log/outcome timestamp matching, per-analysis cost, state-sequence verification |
| Qualitative trading cases | `experiments/cases/{case_audit,prebaseline_case_audit}.csv` | Saved audits; raw contexts excluded |

Start with $100,000; costs are 0.5 bps per side on executed notional. A reversal counts as two actions. Main risk metrics use 310.552 observations/year (154 observations / 181 calendar days * 365). **The saved native FinMem trading summary uses 252**; apply the common-calendar rescaling for its paper Sharpe/Sortino. Preserve native method execution semantics, rather than treating the native baseline comparison as a controlled input-formation experiment.

CEAU extraction matches a successful worker log to the committed outcome by task ID and nearest end timestamp within 15 seconds. Worker timestamps are UTC+8; outcome timestamps carry their timezone. The packaged CSV keeps both provider-reported outcome token totals and tokens summed from successful worker-log `Token Usage` records. The paper uses the latter; the direct per-record outcome receipts have a different total. Do not mix these accounting sources. No prompts, article text or environment information from these logs are exported.

The historical replay config contains zero execution fees; the study evaluators apply the 0.5 bps cost overlay. Do not silently add a second fee charge. The published manuscript table and extracted evidence are separate files so provenance remains visible.
## Code versions and saved Console examples

The `paper-v1` tag preserves the initial public paper snapshot. Development code in `main` is updated from private source revision `7abc566916e1db93477742ccf74c3085905ec6c5` (August 28, 2026), including the complete original PM Console. This does not establish that the newer code executed the paper's historical experiments.

The numerical paper-trading examples are described in [data/paper-trading/README.md](data/paper-trading/README.md). Use `--saved-results` to view fixed curves and linked numerical operations without redistributed OHLCV bars or model credentials. The saved mode rejects writes and model requests. Full agent replay and fresh dependency installation require separate validation.
