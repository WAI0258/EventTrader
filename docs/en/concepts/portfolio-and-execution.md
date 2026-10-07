# Portfolio review and execution truth

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/portfolio-and-execution.md)

A research direction is not an instruction to reproduce that direction with the current portfolio. PM review evaluates the actual position, risk, thesis, market context, and review reason before proposing a position decision.

## Decision-first flow

| Record | Meaning |
| --- | --- |
| AnalysisAssessment | Research judgment and material price/thesis semantics |
| PM review request | Why a portfolio review should happen |
| PMDecision | Portfolio action/target and its decision context |
| ExecutionIntent | Deterministic executable intent derived from the decision |
| ExecutionRecord | Execution result, including status, timing and cost-adjusted price |
| PortfolioState | State updated from an executed record |

PM workflow uses bounded reads and a structured final decision. Contract/portfolio-risk checks are separate from the model's rationale. Hold and deferred outcomes remain distinct from executed adjustments.

## Applying a decision

The deterministic execution flow builds an intent, executes through `PaperExecutionEngine`, records the attempt, and updates portfolio state only for an executed result. It then materializes state changes and refreshes episode artifacts.

Execution depends on configured price basis, available bars, direction policy, target cost overrides, and missing-bar handling. A later eligible bar may differ from decision time. Persisted execution prices already include configured costs; an evaluator or HTML scenario must not subtract those same costs twice.

## Scheduling and control

Automatic dispatch and automatic execution are separate configuration controls. BTC and SOX examples use different combinations. Explicit operator run-and-execute commands are a separate path, so an automatic-workflow flag should not be advertised as a universal execution lock.

The public engine records paper executions, not broker fills. Direct curves are hypothetical analysis-to-position mappings, not PM records. Funding, borrow, and dividends require their own stated treatment.

For an unexpected exposure, follow decision → intent → execution status → portfolio state. Do not repair the story by treating a thesis exit recommendation as a completed exit.

## PM reads a decision-scoped portfolio view

The PM tool gateway exposes request, evidence, market bars, prior PM history, episode memory, learning cards, and active exposure reads. Supplemental reading is planned from a whitelist. Each executed read must return a canonical result with a passing visibility receipt.

History reads exclude the current request and retain only records with earlier business time. Market reads are bound by the request's event/market cutoffs. Active-exposure and episode surfaces require their own temporal interpretation; a history filter alone is not a universal historical-state reconstruction.

The portfolio-risk calculation checks bar adjustment policy and the active instrument basis. It can prefer a same-basis opening execution for entry price/cost; otherwise it uses the supported active-basis bar path. This prevents mixing a raw historical entry with a differently adjusted current series without an explicit rule.

## Practical distinction: thesis change versus exit

Consider an illustrative research update that weakens the bullish thesis. Analysis may emit an assessment and a PM reason, while PM may still hold, reduce exposure, or request a different risk response. An emitted assessment is not already a sell.

For a recorded exit, require its PMDecision, derived intent, executed result, and updated state. A deferred result leaves no completed position transition merely because the decision asked to exit.

PM decisions are month-partitioned under `runtime/portfolio/pm-decisions/<target>/`; the current portfolio state is `runtime/portfolio/state/<target>.json`. These answer historical-decision and current-state questions respectively. The retained sample's numerical record is the evidence for its operation report.

## Implementation

- [code/src/event_trader/pm_review/workflow.py](../../../code/src/event_trader/pm_review/workflow.py)
- [code/src/event_trader/pm_review/portfolio_risk.py](../../../code/src/event_trader/pm_review/portfolio_risk.py)
- [code/src/event_trader/portfolio/pm_execution_flow.py](../../../code/src/event_trader/portfolio/pm_execution_flow.py)
- [code/src/event_trader/execution/engine.py](../../../code/src/event_trader/execution/engine.py)

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/portfolio-and-execution.md)
