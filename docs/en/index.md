# EventTrader documentation

[Project home](../../README.md) · [简体中文](../zh-CN/index.md)

These documents describe the public implementation through usage, design mechanisms, and development responsibilities. Numerical sample viewing needs no model credentials; full runs use your own evidence, market data, and environment.

## Choose a reading route

- Inspect the project: [Quickstart](guides/quickstart.md) → [Console](guides/pm-console.md) → [Samples and reports](../../data/paper-trading/README.md).
- Run your own target: [Installation](guides/installation.md) → [Market data](guides/market-data.md) → [Configuration](development/configuration.md) → [Live](guides/live-running.md) / [Replay](guides/historical-replay.md) → [Operations](guides/operations.md).
- Understand the design: [Workflow](concepts/workflow.md) → [Evidence](concepts/evidence.md) → [CEAU](concepts/checker-and-ceau.md) → [Analysis](concepts/analysis-and-context.md) → [Research memory](concepts/research-memory-and-thesis.md) → [PM](concepts/portfolio-and-execution.md) → [Reflection](concepts/reflection-and-learning.md).
- Maintain or extend: [Extensions](development/extensions.md) → [Temporal visibility](concepts/temporal-visibility.md) → [Agent contracts](development/agent-contracts.md) → [Harness](development/agent-harness.md) → [Runtime state](development/runtime-and-state.md) → [Verification](development/contracts-and-testing.md).

## Guides

- [Quickstart](guides/quickstart.md)
- [Installation and environments](guides/installation.md)
- [Using PM Console](guides/pm-console.md)
- [Market data and Buy & Hold](guides/market-data.md)
- [Running a live target](guides/live-running.md)
- [Historical replay](guides/historical-replay.md)
- [Operations and recovery](guides/operations.md)

## Concepts and design

- [Workflow and design principles](concepts/workflow.md)
- [Evidence admission and fact truth](concepts/evidence.md)
- [Checker and CEAU](concepts/checker-and-ceau.md)
- [Analysis and bounded context](concepts/analysis-and-context.md)
- [Research memory and thesis revisions](concepts/research-memory-and-thesis.md)
- [Portfolio review and execution truth](concepts/portfolio-and-execution.md)
- [Position monitoring and episodes](concepts/position-monitoring.md)
- [Reflection and learning lifecycle](concepts/reflection-and-learning.md)
- [Temporal visibility and replay safeguards](concepts/temporal-visibility.md)

## Development

- [Configuration reference and examples](development/configuration.md)
- [Extending project boundaries](development/extensions.md)
- [Runtime ownership and durable state](development/runtime-and-state.md)
- [Contracts and verification](development/contracts-and-testing.md)
- [Evaluation and result provenance](development/evaluation.md)
- [Agent output contracts and prompt design](development/agent-contracts.md)
- [Agent harness and controlled execution](development/agent-harness.md)

## Source navigation

Start with a business responsibility, then follow each page's implementation links. External components are described through their project integration boundaries.

| Responsibility | Entry points | Documentation |
| --- | --- | --- |
| Composition and orchestration | [composition.py](../../code/src/event_trader/composition.py) · [kernel.py](../../code/src/event_trader/kernel.py) · [runtime](../../code/src/event_trader/runtime) | [Details](concepts/workflow.md) |
| Acquisition, admission and release | [feeds](../../code/src/event_trader/feeds) · [source_archive](../../code/src/event_trader/source_archive) · [ingest](../../code/src/event_trader/ingest) · [source_release](../../code/src/event_trader/source_release) · [source_policy.py](../../code/src/event_trader/source_policy.py) · [live_source_checkpoint.py](../../code/src/event_trader/live_source_checkpoint.py) | [Details](concepts/evidence.md) |
| Checker and unit formation | [checker](../../code/src/event_trader/checker) · [ceau](../../code/src/event_trader/ceau) | [Details](concepts/checker-and-ceau.md) |
| Context, claims and reasoning | [context_assembly](../../code/src/event_trader/context_assembly) · [reasoning](../../code/src/event_trader/reasoning) · [analysis.py](../../code/src/event_trader/analysis.py) · [analysis_request_handler.py](../../code/src/event_trader/analysis_request_handler.py) | [Details](concepts/analysis-and-context.md) |
| Research and decision history | [research_memory](../../code/src/event_trader/research_memory) · [thesis_revision](../../code/src/event_trader/thesis_revision) · [decision_memory](../../code/src/event_trader/decision_memory) · [analysis_assessment_store.py](../../code/src/event_trader/analysis_assessment_store.py) | [Details](concepts/research-memory-and-thesis.md) |
| Portfolio, PM and execution | [pm_review](../../code/src/event_trader/pm_review) · [portfolio](../../code/src/event_trader/portfolio) · [execution](../../code/src/event_trader/execution) | [Details](concepts/portfolio-and-execution.md) |
| Monitoring and validation | [position_monitoring](../../code/src/event_trader/position_monitoring) · [validation](../../code/src/event_trader/validation) | [Details](concepts/position-monitoring.md) |
| Reflection and experience | [reflection](../../code/src/event_trader/reflection) · [episode_memory](../../code/src/event_trader/episode_memory) · [learning_cards](../../code/src/event_trader/learning_cards) · [counterfactuals](../../code/src/event_trader/counterfactuals) | [Details](concepts/reflection-and-learning.md) |
| Market data and replay | [market](../../code/src/event_trader/market) · [live_market_data_runtime.py](../../code/src/event_trader/live_market_data_runtime.py) · [replay](../../code/src/event_trader/replay) | [Details](guides/market-data.md) |
| Console, projections and operator input | [pm_console](../../code/src/event_trader/pm_console) · [code/frontend/pm-console/src](../../code/frontend/pm-console/src) · [projection](../../code/src/event_trader/projection) · [operator_context](../../code/src/event_trader/operator_context) · [operator](../../code/src/event_trader/operator) | [Details](guides/pm-console.md) |
| Contracts, configuration and external integration | [contracts](../../code/src/event_trader/contracts) · [config.py](../../code/src/event_trader/config.py) · [agent_implementation.py](../../code/src/event_trader/agent_implementation.py) · [integrations](../../code/src/event_trader/integrations) · [code/vendor/mirothinker](../../code/vendor/mirothinker) | [Details](development/extensions.md) |
| Storage, commands, migrations and retention | [storage](../../code/src/event_trader/storage) · [workspace.py](../../code/src/event_trader/workspace.py) · [commands](../../code/src/event_trader/commands) · [migrations](../../code/src/event_trader/migrations) · [artifact_retention.py](../../code/src/event_trader/artifact_retention.py) · [tools](../../code/src/event_trader/tools) | [Details](guides/operations.md) |
| Audits, evaluation and experimental tools | [audit](../../code/src/event_trader/audit) · [evaluation](../../code/src/event_trader/evaluation) · [code/scripts](../../code/scripts) · [baselines](../../baselines) · [experiments](../../experiments) · [tools](../../tools) | [Details](development/evaluation.md) |

Historical studies, measurement conventions, and reproduction limits are covered in [Evaluation](development/evaluation.md). Both languages use the same topics; command and configuration identifiers stay unchanged.
