# Installation and environments

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/installation.md)

The runtime requires Python 3.13 or newer. Its core dependencies cover JSON Schema validation, MCP, NautilusTrader integration, and market calendars. The React/TypeScript Console has its own Node/npm dependency tree.

## Core and frontend

```sh
git clone https://github.com/WAI0258/EventTrader.git
cd EventTrader/code
uv sync --frozen --no-dev
uv run event-trader --help
```

For the Console frontend, run `npm ci` in `code/frontend/pm-console/`. The lockfile supplies the frontend dependency resolution. See [quickstart](quickstart.md) for the two-process launch.

## Model environment

The project includes MiroThinker/MiroFlow source under `code/vendor/mirothinker/`. The existing separate-environment installation route, from `code/`, is:

```sh
uv sync --project vendor/mirothinker/apps/miroflow-agent --frozen --no-dev
```

The core `pyproject.toml` also declares a `mirothinker` optional extra and local uv source mappings. That declaration does not establish that every combined environment or provider setup has been validated. The core CLI was checked on Windows; complete model-runtime installation and historical experiment equivalence have separate limits documented in [Evaluation and provenance](../development/evaluation.md).

For Futu, the existing optional installation is `uv sync --frozen --no-dev --extra futu`. Account permissions and a locally running OpenD are additional requirements.

## Credentials and paths

Inspect [the variable template](../../../code/.env.example), then set only the credentials required by your chosen configuration. For example:

```powershell
$env:MINIMAX_API_KEY = "your-key"
```

```sh
export MINIMAX_API_KEY="your-key"
```

A variable template is not evidence that every entry is needed, nor that every launch path automatically loads a `.env` file. Use the launching shell's environment.

Run configuration examples from `code/`, and inspect relative workspace, archive, vendor, and log paths before changing directories. Keep credentials and provider data local. A successful `--help` verifies command availability, not access to an LLM or a licensed dataset.

## Choose the environment by the task

| Task | Required environment |
| --- | --- |
| Read reports and operation HTML | Browser; no runtime installation |
| View saved PM Console samples | Core Python environment and Console frontend |
| Add a local-archive B&H overlay | Core environment and your own matching market archive |
| Obtain bars through Futu | Core environment with the Futu extra, OpenD and access rights |
| Run agent research | Core runtime plus the configured model/tool environment |
| Develop or run Python tests | Core environment with development dependencies |

The `local_archive` provider reads a formal archive containing `manifest.json` and `bars/<bar_granularity>/<symbol>.csv`; an arbitrary CSV is not a direct input. The manifest specifies instrument, exchange, timezone and bar granularity. The current provider requires raw prices and bar-end timestamps, with columns `datetime`, `open`, `high`, `low`, `close`, `volume` and `amount`. Configure `validation.market_data.local_archive_root` and match the sample's instrument/session/coverage before generating B&H. See [market data](market-data.md) for the overlay workflow.

The separate MiroFlow project has its own environment. Installing the core does not install that project's dependencies. Likewise, frontend dependencies do not belong in the Python environment. Resolve the installation route before diagnosing a provider failure.

For development, run `uv sync --frozen` from `code/`; omitting `--no-dev` includes the declared development group. Keep the frozen lockfile resolution for the supplied environment. If it fails on your platform, retain the error and identify the package/platform mismatch before changing dependency versions.

## Implementation

- [code/src/event_trader/market/local_archive_provider.py](../../../code/src/event_trader/market/local_archive_provider.py)
- [code/pyproject.toml](../../../code/pyproject.toml)
- [code/.env.example](../../../code/.env.example)
- [code/frontend/pm-console/package.json](../../../code/frontend/pm-console/package.json)
- [code/src/event_trader/integrations/mirothinker_runtime_paths.py](../../../code/src/event_trader/integrations/mirothinker_runtime_paths.py)

[Documentation index](../index.md) · [简体中文](../../zh-CN/guides/installation.md)
