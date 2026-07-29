# ADR 0015 — Agent-shaped UI: the five workflow rules

- **Status:** Accepted
- **Date:** 2026-07-29
- **Spec:** `docs/specs/ui-ingestion-and-agent-workflow.md` §5–§9
- **Debate:** `docs/design-debate-w1-w4.md` UI-D11, UI-D12, UI-D13
- **Extends:** `adr/0012` (frontend principal model), `adr/0009` (multi-agent topology)

## Context

The client asked that the UI be shaped around **how the agents actually
operate**, rather than being a CRUD skin over agent endpoints.

That reads like polish. It is not. A CRUD endpoint has two outcomes — it worked,
or it errored. Ours has seven, and **five of them return HTTP 200**:

| Outcome | HTTP | Field | What the user must understand |
|---|---|---|---|
| Grounded | 200 | `grounded: true` | Normal |
| Refused | 200 | `refused: true` | The system worked. The answer does not exist in scope. |
| Withheld | 200 | guardrail intervened | We produced output and chose not to show it |
| Ungrounded | 200 | `grounded: false` | Do not act on this |
| Overridden | 200 | `overridden: true` | The tool's number is authoritative; the agent's sentence was wrong |
| Stale | 200 | `stale: true` | Correct as of a time, not as of now |
| Transport failure | 5xx | — | Broken |

Built by default, every one of the middle five renders as a spinner that ends or
a red box. Both are wrong, and one is dangerous: **a refusal rendered as empty
space reads as "nothing on file."** That is the Week-2 finding restated as a UI
defect — the gold-set case that opened this engagement is an assistant
confidently reporting "No known allergies on file" for a patient with a
penicillin allergy.

## Decision

Five rules. They apply to every agent-backed screen, and each is testable.

### Rule 1 — A refusal is a first-class answer

Distinct visual treatment. Never routed through the error component, never
rendered as an empty result set, never rendered as silence.

**Absence is stated, never implied.** "No allergy was recorded at this
encounter" and a blank space are the same pixels to a component and opposite
facts to a clinician. Pinned by `test_allergy_absence_is_stated_not_implied`.

*Failure this prevents:* a refusal that looks like a bug gets retried; a refusal
that looks like an empty answer gets acted on.

### Rule 2 — Provenance is inline, not behind a disclosure

Citations render with the answer. Not in a "sources" accordion, not in a detail
pane, not one click away.

An assistant answer without visible grounding is indistinguishable from a
generated one, and this system's entire safety argument is that the difference is
observable. **If the citation is one click away, the citation does not exist.**

### Rule 3 — Show the path, not a spinner

The graph already returns `path` — the node sequence it executed — and we
already log it. Surface it.

```
authorize → assemble(4 domains) → summarise
```

Agent calls take seconds, and the W4 view fans out across four domains. A
six-second spinner reads as broken. The path turns dead time into an
explanation, costs nothing, and is the only place in the product where
"multi-agent" stops being a word on a slide.

### Rule 4 — A human gate renders as a decision, not a delay

When a run pauses at the sensitivity interrupt, the UI must present the stakes
and both outcomes explicitly. A paused graph rendered as a spinner **is a hang**.

Each queued item states what is being approved in domain terms — the patient, the
chart span, and why the gate fired — not a run id. "Approve run `view-a3f9`" is a
button that gets clicked. "Release a record assembled across 3 charts for Maria
Gonzalez, including a sensitive-flagged encounter" is a decision.

### Rule 5 — Corpus quality is a staff screen, not a report we read

The eval report is currently terminal output consumed by the engineers who wrote
it. The person who can actually act on a `0.556` fragment coverage is the
front-desk lead who knows Maria has three charts — and they will never run
`pytest`.

The quality screen therefore leads with the **contrast**: retrieval metrics
beside integrity metrics, explicitly labelled so a reader cannot mistake a data
problem for a model problem. Fragmentation renders as **named patients and chart
ids**, not only a rate — `0.556` is arguable, "Maria Gonzalez — charts 1042,
1330, 1588" is not. `clinically_incomplete_answers` gets a severity treatment,
because it is not a metric, it is a count of times the system would have told
someone the wrong thing.

## Consequences

**What this buys.** The five states that would otherwise have been invisible are
each visible and each tested. The demo has something real to show for
"multi-agent". The people who feed the corpus can see what they are feeding.

**What it costs.** Every agent-backed component carries more states than a CRUD
equivalent, which is more component tests and more design surface. The path
display couples the UI to a backend field that is currently debug-grade; if
`path` changes shape, a screen changes with it. Accepted, and pinned by a
component test so the coupling fails loudly rather than rendering nothing.

**Durability requirement this forces.** Rule 4 is only honest if a paused run
survives a restart. Under `InMemorySaver` an approvals queue empties on deploy —
that is a session, not a queue, and shipping it as a control would be the class
of overstatement this engagement keeps correcting. The checkpointer therefore
moves to the durable, encrypted path (`SqliteSaver` + `EncryptedSerializer`,
already referenced in `adr/0009`) as a **precondition** of the approvals screen,
because a paused run holds assembled PHI at rest — 45 CFR 164.312(a)(2)(iv).

**Amended after codex F10.** The sentence above originally read as though the
library choice satisfied the regulation. It does not, and stating it that way is
the overstatement pattern this engagement exists to correct. `EncryptedSerializer`
encrypts the serialized checkpoint payload. It does **not** cover SQLite WAL and
temporary files, filesystem permissions, backup handling, or key custody and
rotation — none of which are built. The honest claim is: *the checkpoint payload
is encrypted; the surrounding storage controls are not yet in place.* Tracked in
`docs/debt-register.md`.

**What we are NOT claiming.** Rule 3 shows the path the graph took. It is not an
explanation of the model's reasoning, and the UI must not label it as one.
