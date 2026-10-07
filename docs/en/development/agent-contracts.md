# Agent output contracts and prompt design

[Documentation index](../index.md) · [简体中文](../../zh-CN/development/agent-contracts.md)

A prompt expresses business intent; a schema and project validator decide what output can be accepted. EventTrader combines all three rather than treating “please return JSON” as a complete contract.

## Model-owned versus runtime-owned

Analysis owner output is intentionally narrow. An illustrative owner decision is:

```json
{
  "used_lesson_ids": [],
  "assessment_decision": "no_material_assessment",
  "assessment_decision_rationale_md": "The visible evidence does not justify a material thesis change."
}
```

This is a synthetic stage example, not a recorded assessment or a schema for the whole workflow. Subsequent assessment synthesis and memory revision use their own contracts. Runtime IDs and forbidden final-payload fields are governed by the project schema/assembler.

Nested schemas use required fields, enums, bounded arrays/strings, and `additionalProperties: false` where defined. Business validation then checks citations, price-role actions, source coverage, PM reasons, memory topology, and portfolio semantics. It can reject structurally valid output.

## Prompt choices and their purpose

| Implemented instruction | Design purpose |
| --- | --- |
| Remain exposure-blind | Keep research interpretation separate from actual position management |
| Experts are advisory and cannot write/commit | Preserve a single accountable business owner |
| Reconcile rejected definite expert advice | Require explicit handling of disagreement |
| Permit no material assessment | Avoid forcing a thesis change for every event |
| Separate assessment from memory-revision planning | Keep judgment and writes independently constrained |
| Return the tool-input object, not serialized JSON | Remove an ambiguous wrapper/protocol failure |
| Repair from frozen context and exact validation feedback | Constrain repair to the failed business contract |

PM and reflection have separate prompts and result contracts; these choices are not a universal guarantee about every external runtime.

## Repair and observability

Transport retries, protocol repair, and business-contract repair handle different failures. Record their stage and rejection reason rather than counting every attempt as a new successful analysis.

Tool schemas and read/write receipts improve inspectability. They do not prove truth, reasoning quality, or complete learning-card usage attribution. Current Analysis supervision initializes an empty included-lesson-ID tuple; avoid claiming that all selected lessons are faithfully tracked as used.

See [harness](agent-harness.md) for execution budgets and [contracts/testing](contracts-and-testing.md) for validation scope.

## Prompt details that reduce ambiguity

The read planner receives project-derived page topology and the distinction between business target and instrument proxy. It is asked to plan reads only—not decide, write, or draft final JSON. Compiler-covered content should not be reread unless a specific grounding gap remains.

Revision planning receives an already validated Analysis payload. It chooses operation/page/section, but does not draft prose. Writers then receive one project-selected intent and cannot redirect it to another page or operation.

A writer's repair receives the rejected payload and a cumulative violation ledger. It must return a coherent replacement without reintroducing resolved violations. This differs from unconstrained “try again” feedback.

These rules reduce accidental scope expansion: a read stage cannot quietly make portfolio decisions, and a prose writer cannot choose a new memory surface merely because it would be convenient.

## Different stages use different protocols

The owner-decision JSON example above is for that stage only. PM can return a validated draft through its own boxed contract-output envelope, and reflection has its own result parser. Do not replace every adapter's output with a single universal JSON convention.

PM targeted repair can skip supplemental reads, request only named repair fields, and merge them deterministically. That should not be generalized to Analysis repair: its accepted context/decision and revision-plan violation ledger follow a different workflow.

Keep source pointers near these examples when modifying prompts. A changed wording should still match tool schemas, parser behavior, runtime-owned fields, and the validator's feedback.

## Implementation

- [code/src/event_trader/contracts/analysis_assessment_schema.py](../../../code/src/event_trader/contracts/analysis_assessment_schema.py)
- [code/src/event_trader/contracts/analysis_final_payload_validation.py](../../../code/src/event_trader/contracts/analysis_final_payload_validation.py)
- [code/src/event_trader/reasoning/analysis_workflow.py](../../../code/src/event_trader/reasoning/analysis_workflow.py)
- [code/src/event_trader/pm_review/prompt.py](../../../code/src/event_trader/pm_review/prompt.py)
- [code/src/event_trader/reflection/prompt.py](../../../code/src/event_trader/reflection/prompt.py)

[Documentation index](../index.md) · [简体中文](../../zh-CN/development/agent-contracts.md)
