# Event-Trader: An Event-Driven LLM Research Runtime for Asynchronous Financial Evidence

[English](README.md) · [简体中文](README.zh-CN.md) · [Paper](paper/event_trader.pdf) · [Documentation](docs/en/index.md) · [Quickstart](docs/en/guides/quickstart.md) · [Results](#results)

Event-Trader is an event-driven LLM system for discretionary financial research and paper trading. It follows asynchronous evidence, maintains a revisable thesis, reviews actual positions, and records simulated operations and their outcomes. The paper studies this workflow in a gold-trading application; the repository also includes SOXX, IAU and BTC paper-trading examples.

The workflow continues across evidence arrivals. Research memory carries prior reasoning forward, while new evidence can confirm, revise or invalidate it. Analysis evaluates the thesis without actual exposure; PM then considers positions, risk and execution conditions. This separation preserves the difference between a research judgment, a portfolio decision and an executed operation.

## Results

![SOXX paper-trading PM and Direct curves](assets/paper-trading/soxx-selected.png)

![IAU paper-trading PM and Direct curves](assets/paper-trading/iau-selected.png)

| Paper-trading case (2026, UTC) | PM | Direct | Decision report | Operation review |
| --- | ---: | ---: | --- | --- |
| SOXX, June 4–September 5 | **+13.58%** | −1.49% | [Read report](data/paper-trading/soxx/REPORT.md) | [HTML review](data/paper-trading/soxx/OPERATIONS.html) |
| IAU, July 20–August 24 | +4.23% | **+8.47%** | [Read report](data/paper-trading/iau/REPORT.md) | [HTML review](data/paper-trading/iau/OPERATIONS.html) |
| BTC, September 11–15 (full available overlap) | −0.81% | +1.14% | [Read report](data/paper-trading/btc/REPORT.md) | [HTML review](data/paper-trading/btc/OPERATIONS.html) |

Each case includes a decision report and an operation review covering positions, segment returns and cost scenarios. Download the HTML review and open it in a browser to use the interactive charts.

PM follows recorded paper executions with their persisted execution-adjusted costs. Direct is a hypothetical mapping of analysis assessments to positions. SOXX's cutoff and IAU's 35-day interval were selected after observing performance; [full intervals, CSVs and original PM Console instructions](data/paper-trading/README.md) accompany these cases. BTC currently has less than 30 days of overlapping price coverage. These examples are separate from the paper's historical replay experiments.

See the [paper](paper/event_trader.pdf) for the full comparisons and experimental setup, and [Evaluation and provenance](docs/en/development/evaluation.md) for saved results and measurement conventions.

## Framework

![Event-Trader framework from the manuscript](assets/framework.png)

| Capability | How it works | Details |
| --- | --- | --- |
| Evidence with provenance | Admission records source identity and timing; an append-only ledger preserves evidence separately from interpretation. | [Evidence](docs/en/concepts/evidence.md) |
| Event-driven research | Checker gates deeper work; CEAU forms bounded analysis units with explicit closure/dispatch reasons. | [Checker and CEAU](docs/en/concepts/checker-and-ceau.md) |
| Persistent research memory | Markdown pages, citations and thesis revisions carry research across events. | [Research memory](docs/en/concepts/research-memory-and-thesis.md) |
| Controlled reasoning | Bounded reads, advisory expert views, one Analysis owner, structured output, semantic checks and finite repair. | [Agent harness](docs/en/development/agent-harness.md) |
| Position-aware PM | PM reviews research against actual exposure and risk; deterministic paper execution records costs and outcomes. | [Portfolio and execution](docs/en/concepts/portfolio-and-execution.md) |
| Monitoring and reflection | Position gates and episodes support review; reflection has explicit eligibility and learning-candidate lifecycles. | [Monitoring](docs/en/concepts/position-monitoring.md) · [Reflection](docs/en/concepts/reflection-and-learning.md) |
| Inspectable operations | PM Console links numerical performance, positions, episodes, decisions and research revisions. | [Console guide](docs/en/guides/pm-console.md) |

The design keeps admitted facts separate from revisable cognition, and deterministic transport/time/execution responsibilities separate from agent interpretation. [Workflow and design principles](docs/en/concepts/workflow.md) explains the ownership boundaries.

Replay and tool contexts use explicit [temporal visibility](docs/en/concepts/temporal-visibility.md) controls. [Output contracts and prompt design](docs/en/development/agent-contracts.md) constrain accepted responses. These mechanisms improve control and traceability; their implementation boundaries are documented alongside them.

## Documentation

The [English documentation](docs/en/index.md) and [中文文档](docs/zh-CN/index.md) share the same topic structure.

| Your goal | Start here |
| --- | --- |
| Inspect the supplied examples | [Quickstart](docs/en/guides/quickstart.md) → [PM Console](docs/en/guides/pm-console.md) |
| Add your own data or run a target | [Installation](docs/en/guides/installation.md) → [Market data](docs/en/guides/market-data.md) → [Live](docs/en/guides/live-running.md) / [Replay](docs/en/guides/historical-replay.md) |
| Understand the design | [Workflow](docs/en/concepts/workflow.md) → [Analysis](docs/en/concepts/analysis-and-context.md) → [Research memory](docs/en/concepts/research-memory-and-thesis.md) |
| Extend or maintain the system | [Configuration](docs/en/development/configuration.md) → [Extensions](docs/en/development/extensions.md) → [Runtime/state](docs/en/development/runtime-and-state.md) → [Verification](docs/en/development/contracts-and-testing.md) |

Detailed pages link to the relevant implementation and to their counterpart language. Historical experiment setup and baseline conventions are in [Evaluation](docs/en/development/evaluation.md).

## Installation

Use Python 3.13+ and [uv](https://docs.astral.sh/uv/). The core installation and CLI have been checked on Windows; full agent runs also need the separate MiroThinker environment and model credentials.

```sh
git clone https://github.com/WAI0258/EventTrader.git
cd EventTrader/code
uv sync --frozen --no-dev
uv run event-trader --help
```

The MiroThinker runtime source is included under `code/vendor/mirothinker/`. Its separate environment can be installed from `code/` with:

```sh
uv sync --project vendor/mirothinker/apps/miroflow-agent --frozen --no-dev
```

Full model-runtime installation has not yet been validated. See [Installation](docs/en/guides/installation.md) for environment requirements and [Evaluation](docs/en/development/evaluation.md) for historical-run limitations.

### Configuration

The gold replay configuration is [`code/config/kernel.replay.gold.toml`](code/config/kernel.replay.gold.toml). It specifies the model provider, research agents, replay inputs, workspace and simulated execution settings.

The included configuration uses `MINIMAX_API_KEY`. Set it in the shell that launches the replay:

```sh
export MINIMAX_API_KEY="your-key"
```

In PowerShell:

```powershell
$env:MINIMAX_API_KEY = "your-key"
```

Additional tool credentials depend on the tools you enable; variable names are listed in [`code/.env.example`](code/.env.example). Market bars and timestamped evidence must be obtained separately. The repository does not redistribute original paid-provider inputs.

## Usage

### Inspect the paper-trading examples

The original PM Console includes a saved-result mode for inspecting the supplied numerical workspaces without raw market data or API credentials:

```sh
cd code
uv run event-trader-pm-console --mount-catalog ../data/paper-trading/mounts.toml --saved-results
```

In another terminal, run `npm ci` and `npm run dev` from `code/frontend/pm-console/`, then open http://127.0.0.1:4173. Select a target to inspect its PM/Direct curves and recorded position decisions. See the [example guide](data/paper-trading/README.md) for the retained data and omitted narrative content.

Users can also connect their own Futu or compatible market-data provider and add a Buy & Hold baseline over the same historical interval. See [the baseline instructions](data/paper-trading/README.md#compare-with-buy--hold-using-your-own-market-data); fetched data stays local, and saved PM/Direct results remain unchanged.

### Replay

From `code/`, inspect the replay options:

```sh
uv run python -m event_trader.tools.replay_runtime run --help
```

After preparing the market archives and evidence dataset required by the configuration, a gold replay uses:

```sh
uv run python -m event_trader.tools.replay_runtime run \
  --config config/kernel.replay.gold.toml \
  --dataset /path/to/evidence.jsonl \
  --target-key gold \
  --window-start 2026-01-01 \
  --window-end 2026-06-30 \
  --run-id gold-replay
```

This is a command template, not a bundled complete experiment. Use a separate output workspace for new runs. Details on input formats, baseline setup and evaluation are in [Evaluation and provenance](docs/en/development/evaluation.md).

## Experiments

In a one-week ablation (February 24–March 2, 2026), Continuous Event-to-Analysis Unit (CEAU) formation reduced total successful analysis runtime by **64.2%** and successful token use by **62.9%** relative to direct per-record invocation. Returns were 1.53% and 1.44%, respectively; direct per-record invocation had the lower maximum drawdown (0.27% versus 0.48%).

The repository includes saved baseline comparisons, timebatch experiments, CEAU ablation records and case-study summaries. Check these results without model calls or API keys, from the repository root:

```sh
python -B tools/verify_results.py
python -B tools/plot_saved_results.py
```

Plots are written to `generated/`. These commands check saved results and do not rerun the agents. Exact per-run source/configuration equivalence and complete historical reruns remain unverified; the guide records these limits.

| Directory | Contents |
| --- | --- |
| `code/` | Runtime, configuration, PM Console and MiroThinker dependency |
| `docs/` | English and Chinese guides, concepts and development documentation |
| `baselines/` | Baseline adapters, evaluation and saved results |
| `experiments/` | Timebatch, ablation and case-study artifacts |
| `tools/` | Offline verification and plotting |
| `data/` | Input schemas and metadata |
| `paper/` | Manuscript PDF |

## Citation

```bibtex
@unpublished{cai2026eventtrader,
  title = {Event-Trader: An Event-Driven LLM Research Runtime for Asynchronous Financial Evidence},
  author = {Cai, Jia Wei},
  year = {2026},
  note = {Manuscript},
  url = {https://github.com/WAI0258/EventTrader}
}
```

## License

Original software is licensed under [Apache-2.0](LICENSE). Third-party code retains its [upstream licenses](THIRD_PARTY_NOTICES.md). The manuscript and data/results are outside the software license.
