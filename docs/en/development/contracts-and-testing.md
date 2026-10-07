# Contracts and verification

[Documentation index](../index.md) · [简体中文](../../zh-CN/development/contracts-and-testing.md)

Verification should follow the boundary changed by an implementation. Parsing a response, validating its business meaning, and reproducing a full agent run are separate checks.

## Validation layers

1. **Input/record contracts:** source shape, timestamp awareness, identity, instrument/session and evidence references.
2. **Structured model output:** allowed fields, required values, enums and nested schemas.
3. **Business validation:** price-role consistency, citations, PM reasons, risk/exposure and write topology.
4. **Persistence/linkage:** accepted record relationships, execution status, episode references and stale hashes.
5. **Evaluation:** arithmetic, timing/cost conventions and provenance.

A syntactically valid JSON object can fail later layers. A correct numerical report does not prove that its upstream model reasoning was correct.

## Available checks

From the repository root:

```sh
python -B tools/verify_results.py
```

This parses public Python and recomputes selected saved curves, calendar scaling, time-batch summaries, and CEAU statistics. It does not run agents or validate excluded source inputs.

With development dependencies installed, from `code/`:

```sh
uv run pytest tests/test_pm_console_saved_results.py tests/test_pm_console_buy_hold.py
```

These tests cover saved-mode behavior, write rejection, result identity, B&H costs/coverage, stale overlays, and preservation of saved curves. They are not a complete core-runtime regression suite.

From `code/frontend/pm-console/`:

```sh
npm run typecheck
npm test
```

Frontend tests cover relevant pages/components; a DOM assertion is not a substitute for visual inspection or a real model/provider run.

## Extension checks

For a new feed/provider, test timestamps, duplicate identity, missing coverage and downstream records. For a schema/prompt change, exercise rejected output and finite repair as well as valid output. For a state change, inspect partial failure and retries.

Record exactly which checks ran and their scope. Keep model-dependent, paid-provider, and licensed-input checks explicit rather than treating one green verifier as universal certification.

## Match the check to the changed boundary

| Change | Useful verification |
| --- | --- |
| Evidence mapper | Aware timestamps, identity and malformed-record rejection |
| Historical reader | Before/at/after-cutoff fixtures and historical revision selection |
| Agent schema or finalizer | Valid output, rejected output and bounded repair exhaustion |
| PM/execution flow | Decision → execution → state linkage, including deferred/rejected operations |
| Saved-results view | Unchanged PM/Direct values, disabled writes and stale-overlay rejection |
| Projection or frontend | Source references, DTO/type checks and browser inspection |

Use small deterministic fixtures for the contract before trying an expensive full run. For temporal tests, vary visibility independently of publication time. For execution tests, include a decision that does not execute: that catches accidental treatment of every recommendation as a fill.

Schema validation should cover structure and allowed values; business checks cover references and stage-specific meaning. Keep the rejected-output path visible in tests rather than only asserting that an example JSON parses.

When reporting checks, separate saved-artifact arithmetic, deterministic runtime tests, UI tests and actual provider/model runs. Each establishes a different part of the workflow.

## Implementation

- [code/tests](../../../code/tests)
- [code/frontend/pm-console/package.json](../../../code/frontend/pm-console/package.json)
- [code/src/event_trader/contracts](../../../code/src/event_trader/contracts)
- [tools/verify_results.py](../../../tools/verify_results.py)

[Documentation index](../index.md) · [简体中文](../../zh-CN/development/contracts-and-testing.md)
