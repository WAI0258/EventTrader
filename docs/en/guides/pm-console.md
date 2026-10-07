# Using PM Console

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/pm-console.md)

PM Console presents the trading workflow through linked read models. It is useful for inspecting how a research judgment became a position decision, and whether that decision actually produced an execution.

## Pages and questions

| Surface | Question it helps answer |
| --- | --- |
| Overview and target sidebar | Which target/workspace am I inspecting? |
| Position | What exposure and PM/Direct performance are recorded? |
| Episodes | Which operations belong to a holding episode? |
| Thesis | How did canonical research change between revisions? |
| PM evolution | Which position decisions were made and why? |
| Operator | What operator context is supplied to the system? |

The backend reads canonical records and derived projections; the frontend does not become the source of execution truth. Linkage warnings should be interpreted with the decision and execution records, rather than hidden by treating all recommendations as trades.

## Two modes

In `--saved-results` mode, position curves come from the supplied saved numerical result. No missing raw-bar recalculation is attempted, model services are bypassed, and POST/PUT requests are rejected. Public samples omit narrative material, so they cannot demonstrate the full thesis/editor experience.

A full workspace exposes additional operations, including operator-context editing/restoration and optional thesis translation/quick brief services. A thesis comparison uses textual revision differences. Optional translations and briefs are derived, tied to the canonical content hash, and do not replace the canonical research revision.

The EN/ZH UI setting changes interface labels. Translating research content is a separate action and may require an LLM.

## Local use

The frontend runs on http://127.0.0.1:4173. Consult the API startup output for its binding and the mount catalog for target/workspace mappings. Keep the Console local: the backend has permissive CORS and no implemented multiuser authentication layer.

See [quickstart](quickstart.md) for samples and [market data](market-data.md) for an optional B&H overlay. For a full workspace, follow [research memory](../concepts/research-memory-and-thesis.md) and [portfolio execution](../concepts/portfolio-and-execution.md) before interpreting a revision as an executed operation.

## Connect a full workspace

From `code/`, the single-workspace launch takes this form:

```sh
uv run event-trader-pm-console --workspace-root /path/to/workspace --config /path/to/config.toml
```

Replace both paths with your own run. A mount catalog instead binds several named targets to their respective workspaces/configurations. The public aliases `sox`, `gold` and `btc` label SOXX, IAU and BTC samples; a target key is not a universal instrument identifier.

Use the target selector first, then inspect an episode, its PM decisions and executed records. The API exposes target discovery at `/api/targets`; aggregate mode requires target-scoped context such as `/api/targets/{target}/context`. A context request without a target is ambiguous in that mode.

`--display-start-at` changes the UTC display/PnL baseline and carries earlier PM exposure into the first displayed bar. It does not create a new entry at that bar or delete prior operations. Use this when reviewing a subwindow, and keep the corresponding report dates aligned.

The optional B&H overlay is generated from your own market data. It supplements the saved PM/Direct curves; it neither reruns agents nor replaces recorded operations.

## Implementation

- [code/src/event_trader/pm_console](../../../code/src/event_trader/pm_console)
- [code/src/event_trader/tools/pm_console_server.py](../../../code/src/event_trader/tools/pm_console_server.py)
- [code/frontend/pm-console/src/features/pm-console](../../../code/frontend/pm-console/src/features/pm-console)

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/pm-console.md)
