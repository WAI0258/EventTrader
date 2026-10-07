# Temporal visibility and replay safeguards

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/temporal-visibility.md)

Historical correctness requires controlling what was visible to a decision, not merely sorting rows by publication date. EventTrader implements explicit boundaries for evidence, market data, context, and historical state reads.

## Shared decision boundary

`DecisionVisibilityBoundary` contains `business_at`, `max_visible_event_time`, and `max_visible_market_time`. Both maxima must be no later than business time. Analysis-style construction can collapse them to business time; downstream contexts can carry narrower cutoffs.

The contract validates the boundary's shape and ordering. Actual readers must still apply it; a valid boundary object alone cannot prevent a tool from returning later data.

## Layers to inspect

| Surface | Safeguard or distinction |
| --- | --- |
| Evidence admission/replay | Source timing, observed visibility, admissibility and controlled clock |
| Context and tool reads | Bounded event/market reads, visibility audits and read receipts |
| Research/thesis | Historical snapshot policy versus current-page reads |
| PM history | Earlier business-time decisions rather than arbitrary later history |
| CEAU completeness | Event-time maturity distinguished from processing-time safety close |

Price warmup before the decision is legitimate input; an outcome price after it belongs to evaluation. A post-decision reflection may use later outcomes without making those outcomes legitimate earlier decision inputs.

## Boundaries that remain

Historical web search uses a retrospective perspective. Prompting for pre-cutoff material is not proof of historical page availability or unmodified content. Publication time and first observed/captured time must remain distinct.

Some latest portfolio/episode surfaces and option-universe membership do not constitute fully reconstructed historical as-of state. Their time semantics need inspection for a proposed experiment.

The calendar time-batch builder can place a fractional-second event before its own visibility: `00:04:59.900` maps to `00:04:59`. Do not claim universal leak prevention for that builder.

## Verification approach

For each input, trace source → admitted record → selected context/tool result → assessment → PM decision. Check timestamps and references at every boundary, including retries and recovery. Receipt coverage proves availability/access, not factual or reasoning correctness.

The system provides layered safeguards; it is not a certification that every historical experiment or external retrieval path is free of look-ahead.

## Audit a decision with a concrete cutoff

Suppose a decision's business time is 14:00. Earlier market warmup is admissible when the reader's market cutoff permits it. A source first observed at 14:05 is not made available at 14:00 simply because it claims a 13:50 publication time. A price at 15:00 can support later evaluation/reflection, but must stay outside the earlier decision context.

Check the same boundary through four layers:

1. Confirm source observation and admitted event timing.
2. Inspect the selected context and bounded tool reads, including receipts.
3. Resolve research/thesis history under the historical read policy rather than substituting today's page.
4. Check PM history and instrument-specific market coverage before interpreting the result.

This is why anti-look-ahead belongs in deterministic readers and contracts as well as prompts. A prompt tells the agent how to reason; the reader controls the records it can obtain. Read receipts make an access path inspectable, while historical snapshots preserve the view used for a past decision.

For a new tool or projection, explicitly decide whether it returns historical as-of state or current state. Reusing a current-state view inside replay needs a separate temporal justification.

## Implementation

- [code/src/event_trader/contracts/temporal_visibility.py](../../../code/src/event_trader/contracts/temporal_visibility.py)
- [code/src/event_trader/replay/admissibility.py](../../../code/src/event_trader/replay/admissibility.py)
- [code/src/event_trader/context_assembly/replay_audit.py](../../../code/src/event_trader/context_assembly/replay_audit.py)
- [code/src/event_trader/thesis_revision/historical_reader.py](../../../code/src/event_trader/thesis_revision/historical_reader.py)
- [code/scripts/build_calendar_timebatch_dataset.py](../../../code/scripts/build_calendar_timebatch_dataset.py)

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/temporal-visibility.md)
