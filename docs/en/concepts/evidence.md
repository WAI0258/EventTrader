# Evidence admission and fact truth

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/evidence.md)

A feed payload is not yet a trusted project record. Admission shapes source input into an evidence record with a stable identity and explicit timing, then the evidence ledger preserves that record.

## From source to evidence

Feed mappers normalize manual, news, and web-search inputs. Admission validates the source shape, derives identity and metadata, and produces ledger/runtime outputs. Source archives retain acquisition-level identity separately from admitted evidence. Source policy, release gates, and quarantine handle inputs that cannot be released through the ordinary path.

An event's publication time, source time, observation/visibility time, and later persistence time are not interchangeable. Preserve them when tracing delayed or repeated inputs; see [temporal visibility](temporal-visibility.md).

## Storage and interpretation

The file-backed ledger exposes append and ID/window reads. Canonical evidence belongs under the workspace's `ledger/` truth surface. Research pages can cite that evidence, but editing a page does not alter the underlying evidence record.

Stable identities help downstream deduplication and references. They are not an assurance that different providers never describe the same real-world event twice. Nor does admission establish that a source's factual claims are true: it establishes a validated project record of the supplied evidence.

## Why keep acquisition separate?

A search result may be captured later, discovered again, quarantined, or rejected before research. Keeping archive observations and admitted IDs distinct lets an operator identify which boundary failed instead of assuming “search succeeded” means “Analysis used it.”

When extending a feed, map its timestamps and provenance before choosing a label or prompt. Do not set historical visibility equal to an old publication date solely to make an input pass replay checks.

The ledger is bounded and structured, not a promise to preserve every byte of an upstream page. Public examples omit source bodies; obtain source inputs under your own access terms.

## The durable record and the runtime notification

| Field | Responsibility |
| --- | --- |
| `event_id` | Stable reference used by downstream records |
| `target_key` | Research target receiving the evidence |
| `source_ref` | Source provenance/reference |
| `title`, `content`, `labels` | Normalized evidence and classification |
| `ts_source`, `ts_event`, `ts_init` | Distinct source, event and initialization timestamps |

Admission produces a durable ledger record and a lightweight runtime event with matching identity and timing. The event routes work; the ledger supplies the evidence body. This avoids making transport messages an alternative fact store and allows readers to resolve the same reference later.

For example, a page published at 12:00 but first captured at 13:00 must preserve that distinction. An old publication date does not demonstrate visibility at 12:30. The admission identity uses target, source, admission time and normalized content; two providers describing the same announcement can still produce separate evidence identities.

Research can revise its interpretation without rewriting the source record. That separation makes it possible to explain both what evidence was supplied and how the system's view changed.

## Implementation

- [code/src/event_trader/feeds/payload_mappers.py](../../../code/src/event_trader/feeds/payload_mappers.py)
- [code/src/event_trader/ingest/admission.py](../../../code/src/event_trader/ingest/admission.py)
- [code/src/event_trader/evidence_ledger/file_backed.py](../../../code/src/event_trader/evidence_ledger/file_backed.py)
- [code/src/event_trader/source_release](../../../code/src/event_trader/source_release)

[Documentation index](../index.md) · [简体中文](../../zh-CN/concepts/evidence.md)
