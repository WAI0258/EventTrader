# Extending project boundaries

[Documentation index](../index.md) · [简体中文](../../zh-CN/development/extensions.md)

Extend the smallest existing seam that serves the trading workflow. New feeds and providers should preserve deterministic truth boundaries; a new reasoning component should not become an alternative source of portfolio state.

## Extension map

| Need | Existing seam |
| --- | --- |
| New evidence source | Feed models/mappers, source archive identity, admission |
| New market API | `MarketBarsProvider.read_series`, provider construction, mappings/session policy |
| New model backend | Agent implementation/provider capability and structured-inference integration |
| New research tool | Tool gateway, approved read/write names, schema and receipts |
| New output view | Read model/projection and Console DTO/frontend |
| New reusable lesson behavior | Reflection lifecycle, learning-card contracts/selection |

A source adapter must distinguish source publication from observed visibility and maintain stable source references. A market adapter must return canonical bar objects with matching session/granularity/price semantics. It should not hide missing history with fabricated bars.

## Ports rather than framework takeover

Project protocols expose business operations such as evidence append/read, page/section writes, escalation, market reads, and outcome-context reads. Composition wires implementations into the runtime.

External MiroThinker/MiroFlow components supply agent/tool infrastructure; NautilusTrader supports runtime transport integration. Reuse does not move admission identity, temporal admissibility, research truth, or execution arithmetic into model prompts.

## A practical development sequence

1. Trace a working neighboring implementation and identify who owns its output.
2. Define the new input's identity, timing, errors, and visibility.
3. Add only the adapter and composition/configuration support needed now.
4. Verify malformed input, duplicate identity, missing coverage, and one successful downstream path.
5. Update the relevant guide and contract documentation.

If a change creates new canonical state, inspect [runtime and state](runtime-and-state.md) first. If it adds a tool, inspect [agent contracts](agent-contracts.md) and [temporal visibility](../concepts/temporal-visibility.md).

Arbitrary APIs do not work merely because they provide OHLCV. Broker execution is also a different extension from adding a price feed.

## Define an adapter's acceptance contract

Before implementing a feed, write down its source reference, timestamp mapping, target routing and malformed-input behavior. Then follow one record through admission and the ledger. Success is a downstream accepted record with preserved identity, not an HTTP response alone.

For a market provider, verify instrument mapping, bar granularity, trading session and cutoff handling together. A calendar mismatch can change the apparent interval even when OHLCV values look plausible. Test missing coverage as an explicit condition before adding an optional B&H baseline.

For a model/tool change, preserve the stage's existing authority. Checker classifies evidence; Analysis revises research through approved tools; PM decides exposure; deterministic execution applies the operation. Adding a reasoning tool should not grant it direct writes to portfolio truth.

A useful extension review follows the causal chain: new input → existing contract → reader/tool → accepted outcome → durable effect. Prefer an adapter at that seam over a second orchestration framework or a parallel state store.

## Implementation

- [code/src/event_trader/contracts/ports.py](../../../code/src/event_trader/contracts/ports.py)
- [code/src/event_trader/market/provider.py](../../../code/src/event_trader/market/provider.py)
- [code/src/event_trader/composition.py](../../../code/src/event_trader/composition.py)
- [code/src/event_trader/integrations](../../../code/src/event_trader/integrations)

[Documentation index](../index.md) · [简体中文](../../zh-CN/development/extensions.md)
