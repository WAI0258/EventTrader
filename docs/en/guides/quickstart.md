# Quickstart

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/quickstart.md)

Start with the supplied paper-trading samples. This route needs the core runtime and frontend, but no model credentials, raw market archive, or Futu OpenD.

## Start the Console

From the repository root:

```sh
cd code
uv sync --frozen --no-dev
uv run event-trader-pm-console --mount-catalog ../data/paper-trading/mounts.toml --saved-results
```

In another terminal:

```sh
cd code/frontend/pm-console
npm ci
npm run dev
```

Open http://127.0.0.1:4173. Select SOXX (`sox`), IAU (`gold`), or BTC (`btc`). The frontend and Python API are separate processes; keep both running.

## What to inspect

1. Compare the PM and Direct curves over identical endpoints.
2. Open positions and episodes to follow retained numerical decision and execution records.
3. Read the asset's [decision report and HTML operation review](../../../data/paper-trading/README.md). Download the HTML file and open it locally for its interactive charts.

PM uses recorded paper executions and persisted execution-adjusted costs. Direct maps analysis assessments to hypothetical positions. A recommendation, a PM decision, and an executed operation are different records.

The samples omit research prose, private operator notes, source-news bodies, and raw provider OHLCV. Empty or omitted narrative panels do not mean that the original run lacked research. Saved-result mode rejects write requests and does not run agents. The supplied samples are not resumable live workspaces.

## Next steps

Use [market data](market-data.md) to add a locally generated Buy & Hold overlay without changing the PM/Direct curves. Use [live running](live-running.md) or [historical replay](historical-replay.md) only after preparing your own inputs and environment.

If the browser cannot load targets, check the API terminal first, then the frontend terminal. If an overlay is rejected, inspect its sample hash and bar coverage rather than shortening the comparison interval.

## A first inspection in five minutes

Start with one asset rather than opening every panel. Read its report interval, then find the matching PM and Direct curves. Follow one episode from its decision to its recorded operation. Compare the portfolio state before and after that operation; this explains a position change more reliably than its chart marker alone.

The API defaults to `http://127.0.0.1:8765`; the frontend defaults to port `4173`. If the frontend opens but targets do not load, inspect the API address and the mount catalog. When changing the API binding, set `VITE_PM_CONSOLE_API_BASE_URL` in the frontend environment before starting Vite.

| Observation | Expected interpretation |
| --- | --- |
| Curves load without model credentials | Saved numerical results are being read |
| A narrative panel has no content | The public sample excludes that material |
| A position exists at the left edge | The interval can carry earlier exposure |
| A B&H line is absent | Supply your own covered price series and generate an overlay |

Use the same dates for all lines. A saved-result chart remains a review of the published sample even if your local price provider has newer bars.

## Implementation

- [code/src/event_trader/tools/pm_console_server.py](../../../code/src/event_trader/tools/pm_console_server.py)
- [code/src/event_trader/tools/pm_console_mounts.py](../../../code/src/event_trader/tools/pm_console_mounts.py)
- [data/paper-trading/README.md](../../../data/paper-trading/README.md)

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/quickstart.md)
