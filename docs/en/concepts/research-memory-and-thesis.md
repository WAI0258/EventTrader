# Research memory and thesis revisions

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/research-memory-and-thesis.md)

Research memory is a long-lived, Markdown-first research surface. It records revisable interpretation and supporting citations, while the evidence ledger preserves admitted facts separately.

## Organize research

The canonical layout has shared topics/entities and target-specific pages. Index and log pages support navigation and revision history. Context readers can choose bounded pages, sections, or citation surroundings instead of repeatedly injecting the whole library.

A page can be created, rewritten, or updated by section through project tools. Citation and page-topology validation constrain writes. Price-semantics modules connect active roles to an instrument/adjustment basis so that a textual level does not silently drift between price conventions.

## Revision workflow

After assessment validation, Analysis plans bounded write intents. Independent revision drafts can be generated concurrently, then project tools validate and apply them sequentially to staging. The supervisor separately applies accepted receipted changes to canonical memory. The current workflow permits at most 12 planned write calls and 3 concurrent revision writers.

Thesis-revision storage builds canonical bundles and historical snapshots. The Console compares revision text; optional translation/quick briefs are derived from hash-bound canonical content. They do not become new authoritative theses.

This separation helps answer two questions: “What did the source say?” and “What did we believe after reading it?” A later research correction does not need to overwrite the original evidence.

## Boundaries and review

Check page references, citations, write receipts, assessment identity, and revision time together. A Markdown edit outside the governed workflow may not provide those links. Multi-file updates are not a single database transaction, so partial failures need operational recovery.

Historical snapshot reads and current memory reads have different semantics. `HistoricalThesisSnapshotReader` supports only the canonical target pages `index.md`, `thesis.md`, `risks.md`, `watchlist.md` and `timeline.md`. It returns a bootstrap template for `index.md`; the other supported pages are reconstructed from the latest thesis revision at or before the decision business time, or from a bootstrap template when no such revision exists. This is not historical reconstruction of arbitrary shared topics/entities or every research-memory page. Do not substitute today's page for the view used by a past decision. See [temporal visibility](temporal-visibility.md).

Learning cards and episode memory complement research pages, but they have separate eligibility/promotion contracts. See [reflection](reflection-and-learning.md) before describing every stored lesson as active learning.

## Fixed pages and editing discipline

Target entry pages include `thesis.md`, `timeline.md`, `risks.md`, and `watchlist.md`, with required heading skeletons. Index pages are navigational maps; the Analysis memory-commit path rejects direct `update_index` attempts.

The writable topology gives exact page/section pairs. A section named “Risks” on one page does not authorize the writer to assume the same pair exists on another page. Prefer `update_page_section` for local revisions, `rewrite_page` only for a materially incoherent structure, and `create_page` for a new durable research object.

A section writer returns body-only Markdown, without repeating the page title or selected section heading. Deterministic projection sections, including the Market Setup Dashboard, are not freely rewritten by the model.

## From staging to canonical memory

Supervisor prepares a task-specific workspace under `runtime/analysis_staging/<task_id>/`. Revision tools first write there and produce `tool_activity.jsonl` receipts. Independent drafts are generated concurrently; staged tool writes and repairs are applied sequentially.

The supervisor validates receipts/result consistency, then the canonical commit path applies receipted page changes with attribution. It can collapse repeated section receipts and separately project runtime-owned active-price sections from the accepted assessment.

An invalid draft therefore need not immediately become canonical research. This is a staging boundary, not an atomic transaction for the later canonical updates. Temporary staging is cleaned up; do not treat its tool-activity path as a permanent audit archive.

If an assessment is null, the revision plan must be empty. When watchlist maintenance is required and durable revisions are planned, the plan must include the active target watchlist. An empty revision plan is valid when no durable cognition changed.

## Implementation

- [code/src/event_trader/research_memory](../../../code/src/event_trader/research_memory)
- [code/src/event_trader/contracts/research_memory.py](../../../code/src/event_trader/contracts/research_memory.py)
- [code/src/event_trader/thesis_revision/historical_reader.py](../../../code/src/event_trader/thesis_revision/historical_reader.py)
- [code/src/event_trader/thesis_revision](../../../code/src/event_trader/thesis_revision)
- [code/src/event_trader/pm_console/thesis.py](../../../code/src/event_trader/pm_console/thesis.py)

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/research-memory-and-thesis.md)
