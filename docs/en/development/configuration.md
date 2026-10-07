# Configuration reference and examples

[Documentation index](../index.md) · [简体中文](../../zh-CN/development/configuration.md)

Configuration determines workspace identity, source acquisition, reasoning providers, market semantics, and paper execution. It is part of a run's provenance, not merely a collection of API settings.

## Configuration groups

| Group | Responsibility |
| --- | --- |
| Top-level workspace/mode/intervals | State location, live/replay mode, heartbeat and reflection timing |
| `analysis_agent`, `checker_agent`, `pm_review_agent`, `reflection_agent` | Implementation selection, provider, endpoint/model, environment key and logs |
| `live` and source-specific settings | Enabled channels, targets, search cadence and acquisition tools |
| Market data and mappings | Provider, tradable/context subscriptions, symbol, session and granularity |
| `stream_routing` | CEAU policy version/status and formation requirements |
| `pm_review_runtime` | Automatic dispatch/execution and position-review gate policy |
| `execution` | Paper price basis, buy/sell costs and missing-bar policy |
| `runtime_workers`, `artifact_retention` | Worker capacity and selected debug-retention rules |

See `config.py` for accepted fields, defaults, and validation. An unused or misspelled field is not a supported extension point.

## Example differences

The gold historical replay uses GCUSD five-minute bars and zero configured execution fees; the paper evaluator's cost overlay is separate. The SOX live example sets a 1.5 bps buy/sell target override and enables automatic paper execution. BTC enables automatic PM dispatch while disabling automatic execution.

The `implementation = "mirothinker"` selector does not mean every selected Analysis/PM/reflection stage is owned by upstream MiroThinker. Follow the project implementation factory and structured-provider composition.

## Reproducible changes

When changing a configuration, record target, run ID, workspace, code revision, input/archive identity, model endpoint, routing policy, session/adjustment basis, and costs. Use a new workspace for materially different runs.

Provider names and protocol shape are different choices: an Anthropic-compatible endpoint is not selected solely from a model's name. Required environment variables come from the chosen configuration.

Startup validation can reject incompatible combinations, but validation is not a complete environment or historical-data check. Read [installation](../guides/installation.md) and [live running](../guides/live-running.md) before launching.

## A conservative automatic-workflow starting point

For your own full-run configuration, the following is a table excerpt to **replace the matching settings**, not to append a duplicate TOML table:

```toml
[pm_review_runtime]
auto_dispatch_after_analysis = false
execute_pm_decisions = false
require_workspace_ready = true
```

It turns off automatic post-Analysis PM dispatch and automatic paper execution. It does not disable source acquisition, Analysis model calls, or explicit operator execution commands. Position-review gate settings are separate; inspect the complete table before running.

Change the workspace/log paths and target/instrument mappings in the same configuration before launching. Public sample configurations are for sample inspection, not an initial live portfolio.

## Units and override precedence

Costs are basis points (`1 bps = 0.01%`). Target-specific execution cost overrides can change the global execution setting; inspect the resolved target cost rather than only `[execution]`.

Other settings use seconds, hours, minutes, bars, or characters. A CEAU delay measured in bars is not a wall-clock timeout, and a prompt-character cap is not a token allowance. Reflection horizon and lookback have different meanings: one describes outcome maturity, the other the review search window.

Follow the loader's validated configuration for the final values. Copying a setting from the gold experiment into a BTC continuous-market target without checking session and mapping semantics is not an equivalent experiment.

## Implementation

- [code/src/event_trader/config.py](../../../code/src/event_trader/config.py)
- [code/src/event_trader/agent_implementation.py](../../../code/src/event_trader/agent_implementation.py)
- [code/config](../../../code/config)

[Documentation index](../index.md) · [简体中文](../../zh-CN/development/configuration.md)
