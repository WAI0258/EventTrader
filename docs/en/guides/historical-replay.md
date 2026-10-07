# Historical replay

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/historical-replay.md)

Replay advances a controlled clock over timestamped evidence. Its purpose is to inspect decisions using bounded historical inputs, rather than present a current narrative as something the system observed earlier.

## Prepare a run

Obtain the required market archive and evidence dataset yourself. The repository publishes schemas, manifests, adapters, and saved results, not the original paid-provider input corpus. Review warmup coverage as well as the reported evaluation window.

Use a dedicated output workspace in your configuration and a distinct run ID. The historical gold configuration expects five-minute GCUSD bars and additional pre-window price warmup.

From `code/`:

```sh
uv run python -m event_trader.tools.replay_runtime run --help
```

A shell command template is:

```sh
uv run python -m event_trader.tools.replay_runtime run \
  --config config/kernel.replay.gold.toml \
  --dataset /path/to/evidence.jsonl \
  --target-key gold \
  --window-start 2026-01-01 \
  --window-end 2026-06-30 \
  --run-id gold-replay
```

Replace the dataset and workspace paths before use. In PowerShell, put the command on one line or use PowerShell continuation syntax.

## Execution and inspection

The runner schedules historical work using a test clock and serial replay progression. Checkpoints track progress; publication before checkpoint marking permits retries and requires downstream duplicate handling. A completion-pump pass is not proof that every queue is globally quiescent.

Inspect accepted evidence, context visibility audits, analysis outcomes, PM/execution linkage, and episode artifacts together. Separate decision business time, execution time, and persistence time.

Historical web search is retrospective retrieval constrained to a historical perspective. It does not prove that a page or its current text was observed at that earlier time. See [temporal visibility](../concepts/temporal-visibility.md).

## Recovery and evaluation

Recovery and automatic checkpoint repair can change state. Inspect [operations](operations.md) before enabling them. The public paper snapshot, exported development runtime, and exact historical experiment revisions are distinct; see [evaluation](../development/evaluation.md).

A successful saved-result verifier is useful evidence about arithmetic consistency, not a replay of the agents.

## Keep three windows distinct

| Window | Purpose |
| --- | --- |
| Pre-window warmup | Supply earlier prices/context required by a decision |
| Decision/evaluation interval | Define which operations and returns are reported |
| Later reflection interval | Observe outcomes needed for eligible post-decision reviews |

`--reflection-end` defaults to the replay window end. Extending it can supply later outcome observations; it does not authorize earlier Analysis or PM to see those prices. `--reflection-step-hours` controls reflection progression independently of the market-bar granularity.

Checkpoint statuses are `started`, `completed` and `failed`. A failed record retains its error. Match the run key, target, event and stage before selecting recovery; a repeated publication may have reached downstream state even if the checkpoint was not completed.

For a reproducible comparison, retain configuration, input identity, run ID, cutoff dates, price basis and evaluator settings alongside the outputs. Compare accepted decisions and executions, rather than assuming that identical prompts yield identical model runs.

## Implementation

- [code/src/event_trader/replay/runner.py](../../../code/src/event_trader/replay/runner.py)
- [code/src/event_trader/replay/admissibility.py](../../../code/src/event_trader/replay/admissibility.py)
- [code/src/event_trader/tools/replay_runtime.py](../../../code/src/event_trader/tools/replay_runtime.py)
- [code/config/kernel.replay.gold.toml](../../../code/config/kernel.replay.gold.toml)

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/historical-replay.md)
