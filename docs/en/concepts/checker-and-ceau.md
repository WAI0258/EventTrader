# Checker and CEAU

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/checker-and-ceau.md)

Checker asks whether incoming evidence warrants deeper work in the current research context. CEAU then forms bounded analysis units rather than treating every arrival as an independent deep-research request.

## Checker

The single-pass path builds a bounded context pack from evidence, research excerpts, and recent timeline items. It records a structured decision and validates it before escalation. Configuration controls excerpt/pack limits and escalation on insufficient coverage.

A lack of context can warrant escalation. Conversely, admission alone does not require a new analysis. Checker receipts make this gate visible.

## CEAU formation

Continuous Event-to-Analysis Unit formation combines route policy, unit scope, evidence timing, market/reaction requirements, and budget limits. Coordinator/store records connect admitted evidence to emitted units and execution fences. Live and replay coordinators use different clock/completeness sources.

A unit can close for event-time maturity, configured triggers, context budget, processing-time safety, or final replay draining. These reasons are materially different. In particular:

- Processing-time safety closure must set the event-time completeness claim to false.
- Web-search news cannot claim `complete_through` in this implementation.
- A final drain is a replay termination action, not evidence that live sources are complete.

## Intended benefit

Related arrivals can share one analysis context, reducing redundant invocations while preserving important triggers. The saved one-week ablation reports successful-analysis runtime and token reductions; those statistics do not mean every workload saves the same amount.

Investigate a missed analysis by reading Checker decisions, routing-policy hash/version, unit members, closure reason, and dispatch state. Do not simply lower all waiting thresholds: a reaction/context requirement may be the reason the unit exists.

See [Analysis](analysis-and-context.md), [temporal visibility](temporal-visibility.md), and [evaluation](../development/evaluation.md).

## Routes are not one generic timer

The checked gold replay configuration has explicit lanes:

| Lane | Candidate/default behavior | Example bound |
| --- | --- | --- |
| `operator_interrupt` | emit | zero delay bars |
| `risk_interrupt` | emit | zero delay bars |
| `news_interrupt` | emit | zero delay bars |
| `context_window` | accumulate | 6 bars, 12,000 characters |
| `episode_append` | accumulate | 6 bars, 12,000 characters |

These are example-policy values, not universal defaults or a claim that every unit waits exactly 30 minutes. Routing and completeness requirements still determine what can be emitted; a context-budget closure can emit an existing unit before a new event is appended.

The example marks its policy as `candidate` and disables processing-time safety emission. Preserve the policy hash/status when interpreting a run instead of treating a candidate configuration as a finalized research finding.

## Failure behavior and records

The single-pass Checker makes a model call without an interactive tool loop. It can make an initial attempt and two contract repairs; exhausted malformed output is converted to a conservative escalation. Request/transport failure is logged and raised, a different result from an accepted no-action decision.

Checker decision receipts retain event/target identity, rationale, validator action, decision-episode identity, and whether an Analysis request resulted. They are partitioned under `runtime/checker_decisions/<target>/YYYY-MM.jsonl`.

CEAU logs live under `runtime/ceau/`. Their records distinguish routing, unit opening/appending/emission, watermark/completeness observations, and Analysis queued/started/completed/failed states. Emitted is not synonymous with successfully analyzed. Read all three boundaries—formation, dispatch, completion—when diagnosing a stalled unit.

## Implementation

- [code/src/event_trader/checker](../../../code/src/event_trader/checker)
- [code/src/event_trader/ceau/coordinator.py](../../../code/src/event_trader/ceau/coordinator.py)
- [code/src/event_trader/ceau/contracts.py](../../../code/src/event_trader/ceau/contracts.py)
- [code/src/event_trader/ceau/policy.py](../../../code/src/event_trader/ceau/policy.py)

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/checker-and-ceau.md)
