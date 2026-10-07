# Running a live target

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/live-running.md)

A live process observes incoming evidence and market data, then advances the configured research and portfolio workflow. It can invoke paid models and write durable state. Start from a dedicated configuration and a fresh workspace, not the public numerical sample directories.

## Prepare the configuration

Review the target's primary tradable, context subscriptions, enabled feed channels, search cadence, provider credentials, workspace/log paths, and model endpoint. The live BTC and SOX examples are starting points with different behavior:

| Setting | BTC example | SOX example |
| --- | --- | --- |
| Target | `btc` | `sox` |
| Primary tradable | BTC perpetual | SOXX |
| Automatic PM dispatch | enabled | enabled |
| Automatic paper execution | disabled | enabled |

Set `pm_review_runtime.auto_dispatch_after_analysis` and `execute_pm_decisions` deliberately. Automatic execution requires automatic dispatch in configuration validation. These flags govern the automatic workflow; an explicit manual run-and-execute operation has its own execution path.

## Start and observe

From `code/`, inspect the options:

```sh
uv run event-trader-live --help
```

After preparing your own configuration, a launch follows this shape:

```sh
uv run event-trader-live --config config/kernel.live.btc.toml
```

This example can call external services. Do not launch an unchanged example expecting a credential-free demo.

Follow source observations, admission/Checker receipts, CEAU dispatch, Analysis outcomes, PM decisions, and executed records. A running process or collected news alone does not prove the downstream stages succeeded. Observe market-data freshness independently.

## Restart and catch-up

Live startup uses a forward-only startup anchor. Historical catch-up is a separate operator workflow with explicit source identity and recovery handling. Restarting does not imply that every missed historical event has been reconstructed.

PM readiness/preflight and bootstrap may create workspace state; structured-finalizer preflight can probe a model provider. See [operations](operations.md) before treating either as a passive inspection.

The public execution engine simulates paper operations. Live evidence acquisition is distinct from real-money brokerage execution.

## Use stage completion as the readiness signal

| Boundary | What to establish |
| --- | --- |
| Acquisition → admission | Source identity and observation time survive normalization |
| Checker → CEAU | A recorded disposition explains filtering or accumulation |
| CEAU → Analysis | A dispatch and accepted outcome refer to the same target/work |
| Analysis → PM | The request has an explicit review reason |
| PM → execution | An accepted execution result changes the portfolio state |

Prepare a separate workspace for each target/run. Inspect the primary instrument and context instruments before supplying prices: a useful contextual symbol is not automatically the execution price basis. Enable automatic dispatch and paper execution only after checking those boundaries with your own configuration.

When a stage stalls, retain its task/event identifier and last durable record. Use [operations](operations.md) to locate the first missing result. Repeatedly restarting the process obscures whether a source, agent, resolver or execution coverage check is responsible.

## Implementation

- [code/src/event_trader/tools/live_runtime.py](../../../code/src/event_trader/tools/live_runtime.py)
- [code/src/event_trader/live_runtime.py](../../../code/src/event_trader/live_runtime.py)
- [code/config/kernel.live.btc.toml](../../../code/config/kernel.live.btc.toml)
- [code/config/kernel.live.sox.toml](../../../code/config/kernel.live.sox.toml)

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/live-running.md)
