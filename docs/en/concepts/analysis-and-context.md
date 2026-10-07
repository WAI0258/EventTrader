# Analysis and bounded context

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/analysis-and-context.md)

Analysis owns interpretation and research revision. It decides whether new visible evidence materially changes the thesis; it does not own actual portfolio weights or paper execution.

## Build the decision context

Context assembly brings together admitted evidence, target/shared research, operator context, and market surfaces under a decision visibility boundary. Claims and citations provide references; coverage/read receipts describe what was available or read.

The workflow starts with a bounded reading plan. Project tools execute approved reads from evidence, pages/sections, citation surroundings, wiki search, and selected market panels. The default workflow limits reading calls to 16.

## Separate views, one owner

Evidence/causality and thesis/memory experts provide advisory views; a market/price expert is added when required by the deterministic workflow. These temporary experts have no commit authority.

A single Analysis owner sees original context, executed reads, and expert views. It decides `emit_assessment` or `no_material_assessment`, reconciles conflicting advice, and remains exposure-blind. A definite rejected expert recommendation requires a rationale rather than a vote count.

The accepted decision proceeds to assessment synthesis and deterministic validation. Price-level actions distinguish retained, retired, and replaced roles; runtime-owned identifiers are assembled by the system.

## Persist the result

A validated assessment can lead to a separate memory-revision plan and staged writes. Supervisor processing connects assessment, thesis/memory changes, Analysis outcome, PM requests, and episode/log records.

An outcome can be useful without emitting an assessment or executing a trade. A tool-read receipt is evidence of access, not proof that the model correctly used the information. Current lesson-use identifiers also do not provide complete learning-card attribution.

Read [research memory](research-memory-and-thesis.md) for writes, [agent contracts](../development/agent-contracts.md) for validation, and [agent harness](../development/agent-harness.md) for stage budgets and repair.

## Workbench: compile first, deepen only where needed

The compiler organizes bounded evidence, memory-impact, market, and method surfaces rather than concatenating the entire workspace. It records whether an excerpt is available, truncated, or unavailable. Grounding distinguishes compiler-supplied content from tool-read content.

The current compiler's character caps include 1,200 per active-evidence excerpt and 5,000 for its evidence lane; memory-section excerpts have a 700-character cap and the memory-impact lane a 7,000-character cap. Market and method lanes have separate caps. These are character budgets, not token limits or promises that every piece of evidence fits.

The read-planner prompt tells the model to reuse complete compiler receipts, and deepen reads for omitted detail, ambiguity, contradiction, citation context, or write support. A grounding repair requests only additional reads needed to close the stated gap, using the remaining call slots.

This reduces repeated tool work while retaining a path to inspect the original evidence. It also makes “the summary was present” distinguishable from “the full supporting section was read.”

## Target identity and method guidance

Market tools take the business `target_key`, not the proxy symbol. For the supplied IAU example, the business target remains `gold`; SOXX uses `sox`. The terminal resolves the configured instrument internally.

Claim cards provide bounded references to research claims. Selected learning cards provide weak methodological guidance rather than new evidence or commands to trade. Operator context also has its own source/read receipt. None of these surfaces should silently become an alternative ledger fact.

Context packets are partitioned by runtime scope/stage/target/month. Replay adds the run ID to that identity. When reproducing a decision, use its actual packet and read receipts, not a newly compiled packet from today's workspace.

## Implementation

- [code/src/event_trader/context_assembly](../../../code/src/event_trader/context_assembly)
- [code/src/event_trader/reasoning/analysis_workflow.py](../../../code/src/event_trader/reasoning/analysis_workflow.py)
- [code/src/event_trader/reasoning/analysis_supervisor.py](../../../code/src/event_trader/reasoning/analysis_supervisor.py)

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/analysis-and-context.md)
