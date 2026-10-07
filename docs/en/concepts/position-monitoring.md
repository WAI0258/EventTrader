# Position monitoring and episodes

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/position-monitoring.md)

Positions need review between research arrivals. Monitoring combines actual exposure, active price roles, market observations, and cooldown/rearm policy to decide when an open position should return to PM.

## Separate the triggers

Analysis-to-PM routing and open-position review gates are different mechanisms. The former follows research outcomes and reasons; the latter checks an existing position against market/role conditions.

Price-role semantics and instrument basis matter. A carried-forward stop or target is not independent proof that a new evidence event crossed it. Investigate the relevant bar path, role identity, and review reason rather than relying only on the model's “touched” wording.

Cooldown and rearm settings constrain repeated reviews. They are portfolio workflow controls, not guarantees that no relevant market move can occur while waiting.

## Episodes and performance

Validation state-change records connect executed operations into episode artifacts. Open and closed episodes, snapshots, market mappings, linkage checks, and performance windows provide different views of the same operational history.

An episode may carry a position into the selected report window. Its entry cannot be invented at the chart's first point. Distinguish segment returns, whole-episode returns, and full-window equity returns when explaining a win rate or chart.

PM uses execution-linked state. Direct uses the research mapping. Counterfactual calculations answer a different question from realized paper operations. A favorable excursion or local peak is not an executed profit.

## Investigation

Check active exposure, latest relevant executed state change, price roles and basis, gate/cooldown state, PM request, and subsequent execution. If the bar interval is missing or stale, disclose that coverage instead of filling it with a narrative assumption.

Use [PM Console](../guides/pm-console.md) to inspect episodes and [evaluation](../development/evaluation.md) for measurement conventions.

## Read curves, markers and state separately

| Object | What it represents |
| --- | --- |
| `pm_pipeline` | Recorded PM execution-linked performance |
| `analysis_direct` | Hypothetical mapping from Analysis assessments |
| `buy_hold` | Market-data baseline |
| PM/Analysis marker | A decision or assessment annotation |
| Portfolio snapshot | Current state, weight and source decision/execution references |

State labels distinguish `flat`, `weak_long`, `strong_long`, `weak_short` and `strong_short`. The source references in a snapshot explain how that state arose. A marker alone is not an execution receipt.

The position-review gate records why it allowed or skipped work, including first/rearmed triggers, active cooldown and a risk-trigger bypass. Its cooldown and rearm checks limit repeated review requests while retaining an explicit risk path. Inspect touched level/bar references and the gate reason when diagnosing a repeated stop/target review.

A holding segment crossing the report's start is a carried-in segment. Report its visible contribution without inventing its original entry price or calling every positive segment a completed profitable trade.

## Implementation

- [code/src/event_trader/portfolio/active_exposure.py](../../../code/src/event_trader/portfolio/active_exposure.py)
- [code/src/event_trader/pm_review/position_review_gate.py](../../../code/src/event_trader/pm_review/position_review_gate.py)
- [code/src/event_trader/position_monitoring](../../../code/src/event_trader/position_monitoring)
- [code/src/event_trader/validation](../../../code/src/event_trader/validation)

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/position-monitoring.md)
