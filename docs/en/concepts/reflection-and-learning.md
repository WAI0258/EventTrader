# Reflection and learning lifecycle

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/reflection-and-learning.md)

Reflection reviews a bounded decision or position outcome. It separates evaluation from the decision to write an experience candidate or promote a reusable learning card.

## Eligibility and context

Reflection uses explicit anchors, trigger policy, coverage, outcome prices, decision/execution context, and maturity horizons. Closed-position reviews and eligible executed open-entry reviews have different requirements. Missing outcome data can make a review ineligible; the system should not invent the market path.

Cadence aggregation also supports broader reviews. Replay termination can evaluate eligible open positions at the end of the run; that is not equivalent to having observed their ultimate close.

Actual feedback and deterministic counterfactual context are distinct. A hypothetical alternative can inform analysis but does not become an executed trade.

## From review to candidate

Structured reflection output passes contract checks. Review writing, episode-memory candidates/deltas, no-update records, promotion decisions, learning-card storage, and coverage resolution have explicit lifecycle records.

A retained candidate is not automatically an active card. Selection and consumer-role constraints govern how cards are offered to later contexts. Risk-policy review candidates have their own lifecycle. This distinction lets a tentative lesson remain tentative instead of immediately overriding future decisions.

## Benefits and limits

The lifecycle preserves the connection between a lesson and its reviewed episode, prices, and source references. It makes revisions and promotion decisions inspectable.

It does not prove causal improvement in future returns. Current Analysis lesson-use identifiers do not fully attribute which selected cards were used, and reflection evaluation/write/learning/resolve are separate operations rather than one atomic transaction. Some cadence state is process-local and resets on restart.

When investigating an absent lesson, follow eligibility → trigger/coverage → evaluation → write → promotion/selection. Do not conclude “learning failed” solely because a card was not promoted.

See [research memory](research-memory-and-thesis.md) and [runtime state](../development/runtime-and-state.md).

## Three different forms of memory

| Surface | Purpose | Why it stays separate |
| --- | --- | --- |
| Research pages | Current revisable thesis and its evidence | A thesis edit is not a completed operation |
| Episode memory | Experience tied to a decision/position episode | Keeps the lesson connected to its reviewed outcome |
| Learning cards | Reusable, role-specific methodological guidance | Selection/promotion can be narrower than storage |

Risk-policy review candidates have a separate contract from ordinary learning-card promotion. Storing a candidate does not automatically edit the live risk configuration.

The learning application path distinguishes skipped writes, successful candidate receipts, and failed actions. A review page can exist while its learning action failed; inspect the action receipt rather than assuming both succeeded together.

## Cadence and maturity

Open-entry horizon reviews require an executed entry and sufficient outcome data. Closed reviews use the relevant closed episode. Weekly/monthly cadence aggregation combines eligible completed review evidence under its own coverage rules.

The heartbeat can report not due, no eligible candidates, dependency missing, review written/skipped, or failed. An absent card can be an intentional no-write/no-promote outcome.

An illustrative trade that lost money may still yield no reusable lesson if attribution is too weak. Conversely, a profitable outcome does not automatically validate its decision method. Reflection is asked to evaluate evidence and method, not merely label every winning trade a success.

## Implementation

- [code/src/event_trader/reflection](../../../code/src/event_trader/reflection)
- [code/src/event_trader/episode_memory](../../../code/src/event_trader/episode_memory)
- [code/src/event_trader/learning_cards](../../../code/src/event_trader/learning_cards)
- [code/src/event_trader/counterfactuals](../../../code/src/event_trader/counterfactuals)

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/reflection-and-learning.md)
