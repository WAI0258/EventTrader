# Runtime ownership and durable state

[Documentation index](../index.md) · [简体中文](../../zh-CN/development/runtime-and-state.md)

Composition constructs the project kernel and its dependencies. The runtime graph routes project messages and work; workers perform slow reasoning, while resolvers apply their outcomes to the business pipeline.

## Workspace surfaces

| Surface | Meaning |
| --- | --- |
| `ledger/` | Admitted evidence truth |
| `research_memory/` | Revisable research truth, shared and target-specific |
| `runtime/` | Operational records, decisions, execution and processing state |
| `helpers/` | Derived helper material, not canonical truth |

Shared market data has its own store and observed-prefix semantics. Do not infer workspace ownership from a display target name alone; Console mounts explicitly bind targets to workspaces/configurations.

## Message and work lifecycle

The graph composes admission, Checker, CEAU, Analysis, PM, and reflection handling. Queues, worker completion pumps, outboxes, receipts, and resolvers separate transport scheduling from agent-owned interpretation.

Live worker capacity and serial replay progression are distinct. A replay drain/completion pass should not be described as global quiescence. Slow-work runtime and model stage runtimes also measure different things.

## Durability boundaries

An append-only ledger and idempotent append helpers improve recoverability. They do not create a global transaction. Analysis can write memory/assessment/thesis/outcome/PM/episode artifacts in separate steps; execution also persists intents, results, portfolio state, and episodes separately.

Replay can publish work before marking its checkpoint, permitting re-delivery after failure. Downstream duplicate handling must be checked at the actual effect boundary; a stable task ID alone is not an exactly-once guarantee.

## Inspecting failures

Identify the canonical input, queued work, worker result, resolver action, and persisted output. Use stage/task IDs plus business and persistence times. Preserve the valid append-only prefix when diagnosing malformed tails.

Migrations, source quarantine, price-basis repair, and artifact retention belong to explicit operator workflows. See [operations](../guides/operations.md), rather than rebuilding canonical state from a frontend projection.

## Concurrency has several owners

The slow-work executor processes the Checker, Analysis and PM-review lanes in order. With configured concurrency of one, work is serial; greater capacity uses workers within a lane before advancing to the next lane. This differs from inner expert/writer parallelism inside an agent task and from serial historical replay progression.

A worker result is not yet every downstream business effect. Completion pumps and resolvers apply outcomes and may make further work available. Follow the durable outbox/receipt and resulting records before declaring the pipeline drained.

| Question | Owner to inspect |
| --- | --- |
| Which service is constructed? | Composition and configuration |
| Which work is scheduled? | Graph, queue and outbox |
| Did reasoning finish? | Worker result and task outcome |
| Was canonical state changed? | Resolver and accepted durable records |
| Is a display current? | Projection/read model and source references |

The layout makes these responsibilities visible rather than hiding them behind the frontend. It also explains why recovering one failed stage should preserve upstream truth instead of recreating an entire workspace.

## Implementation

- [code/src/event_trader/composition.py](../../../code/src/event_trader/composition.py)
- [code/src/event_trader/runtime](../../../code/src/event_trader/runtime)
- [code/src/event_trader/storage/layout.py](../../../code/src/event_trader/storage/layout.py)
- [code/src/event_trader/portfolio/pm_execution_flow.py](../../../code/src/event_trader/portfolio/pm_execution_flow.py)

[Documentation index](../index.md) · [简体中文](../../zh-CN/development/runtime-and-state.md)
