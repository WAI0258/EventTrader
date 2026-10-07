# Operations and recovery

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/operations.md)

Diagnose the first failing boundary before changing state. A news receipt, analysis assessment, PM decision, and paper execution have different owners and different recovery procedures.

## Follow the persisted chain

| Symptom | First evidence to inspect |
| --- | --- |
| No new research | Source checkpoints, admitted evidence, Checker decision, CEAU unit/dispatch |
| Research but no PM decision | Analysis outcome, PM request reason, queue/outbox and worker result |
| PM decision but no operation | Execution intent, deferred/rejected result, market-bar coverage |
| Unexpected PnL | Executed records, price/cost basis, interval and episode linkage |
| Missing reflection | Eligibility, trigger/coverage records and available outcome data |

Use identifiers and timestamps to connect records. Do not repair a PM problem by editing research prose, or reinterpret an unexecuted recommendation as an execution.

## Inspection versus mutation

CLI `--help` and saved-result viewing are inspection routes. PM preflight may bootstrap a workspace. Structured-provider preflight may call an external model. Catch-up, quarantine repair, replay repair, migrations, and retention pruning can write or remove state.

The repository has explicit operator repair modules for source archives, historical search, analysis, PM, reflection, and replay. Select the command from the actual failure and inspect its options; there is no single safe “repair everything” command.

Before a repair, preserve the affected workspace, configuration, run ID, valid append-only prefix, and error record. For replay, keep the checkpoint's relationship to published work. Check the resulting outcome and linkage after recovery; process startup is insufficient.

## State hygiene

Keep separate workspaces for new runs, public numerical examples, and active live targets. Market data can be shared through its dedicated store; target research and operational state have separate boundaries. Do not copy an entire private `.local` tree into a public release.

Artifact-retention settings govern selected debug artifacts, not a license to remove canonical evidence or research history. Price-basis and thesis migrations have dedicated paths and should be treated as state changes.

The runtime contains local deduplication, append receipts, and recovery mechanisms, but multi-file state updates are not a universal transaction. See [runtime and state](../development/runtime-and-state.md).

## Plan replay recovery before applying it

From `code/`, inspect the supported repair and catch-up commands:

```sh
uv run python -m event_trader.tools.runtime_repair --help
uv run python -m event_trader.tools.runtime_repair replay-failed-event --help
uv run python -m event_trader.tools.live_catchup --help
```

`replay-failed-event` accepts the configuration, target, window, optional run ID and optional event ID. Without `--apply`, it plans the repair instead of applying it. Inspect the selected event and affected state before adding that flag. This distinction belongs to this command; do not assume every operator tool shares it.

After recovery, confirm both stage completion and its business effect: an Analysis outcome should link to the assessment, a PM decision to its intended operation, and an execution to the resulting exposure/episode. A duplicate delivery should not silently become a second operation.

Preserve canonical truth when pruning debugging artifacts. Temporary tool traces and staging directories have a different lifecycle from ledger entries, accepted research revisions and execution history.

## Implementation

- [code/src/event_trader/operator/repair](../../../code/src/event_trader/operator/repair)
- [code/src/event_trader/tools/runtime_repair.py](../../../code/src/event_trader/tools/runtime_repair.py)
- [code/src/event_trader/tools/live_catchup.py](../../../code/src/event_trader/tools/live_catchup.py)
- [code/src/event_trader/artifact_retention.py](../../../code/src/event_trader/artifact_retention.py)

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/operations.md)
