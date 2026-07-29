# ADR 0009 — Multi-agent topology and the authorization boundary (W4)

- **Status:** Accepted
- **Date:** 2026-07-29
- **Spec:** `docs/specs/w4-knowledge-graph-multi-agent.md`
- **Baseline:** ADR 0004
- **Debate:** `docs/design-debate-w1-w4.md` D4, D5
- **Debt:** D11 (IDOR), D8 (N+1), D10 (no automatic logoff)

## Context

The Week-4 ask: *"Let patients see their own labs and visit summaries in one
place. Build something that assembles a patient's full picture across our
services."*

The handover artifacts show why that ask is dangerous as stated:

- `records-service`: `GET /patients/{id}/records` has **no ownership check**; `{id}` is the sequential integer primary key.
- `gateway/app.py`: `require_session` proves *a* valid session exists and never binds it to the requested `patient_id`.
- `docs/handover/portal.har`: one logged-in patient fetches `/api/patients/1042/records` → **200**, then `/api/patients/1043/records` → **200**.

A feature whose entire purpose is *assemble more data about a patient, from more
sources, in one place* is a **force multiplier for that vulnerability**. Shipped
naively it upgrades "walk the IDs one chart at a time" into "walk the IDs and
receive a synthesized narrative of each stranger's medical history."

### The argument we had, recorded

The Senior Engineer's objection stands on the record: *"'Let patients see their
labs in one place' is a JOIN. Building a multi-agent system to render a read view
is résumé-driven development, and here it is worse than wasteful — it amplifies
the exact vulnerability we are supposed to be finding."*

LangChain's own multi-agent guidance opens the same way: *"not every complex task
requires this approach — a single agent with the right (sometimes dynamic) tools
and prompt can often achieve similar results."* Their published cost comparison
shows the subagents pattern costing **4 model calls** on a one-shot request where
router, handoffs and skills cost **3**.

We are recording an architecture decision that quotes the vendor's argument
against itself, because a decision that has not survived that is not a decision.

### What changed the answer

"The full picture" genuinely spans four systems with four authorization rules,
four failure modes, and four retrieval strategies:

| Domain | Source | Retrieval | Characteristic failure |
|---|---|---|---|
| Demographics | `records-service` / Postgres | SQL by id | DB unavailable |
| Encounters & notes | `records-service` / graph traversal | graph walk | N+1 latency (D8) |
| Labs | `interop-service` HL7 feed | vector + graph | **silently incomplete** — the mapper drops AL1/RXA (D6) |
| Coverage | `eligibility-service` | live payer call | **third-party outage** (D4, W3) |

Serial assembly makes the payer's latency the labs' latency. One agent with eight
tools puts four domains' rules in one context and degrades the choice about each.
Parallel domain retrievers, each carrying its own scope binding and its own
degradation behaviour, let one domain fail while the patient still sees the other
three, correctly scoped.

That is not a JOIN. And the synthesis — a coherent cross-domain narrative in
language a patient reads — is the part a JOIN cannot do and the part the client
actually asked for.

## Decision

### 1. Router pattern with `Send` fan-out, hand-built on `StateGraph`

```
authorize → plan → Send()⇉ {demographics, encounters, labs, coverage}
          → merge → sensitivity_gate →(interrupt?)→ synthesize → ground_gate
```

**`langgraph-supervisor` is rejected.** Pinned at **0.0.31, last released
2025-11-19** — before LangGraph 1.0 shipped — with no release since. A production
dependency on a 0.0.x package predating the runtime's GA is not defensible, and
this topology is roughly forty lines of `StateGraph`. A test asserts the package
is absent from requirements, so the rejection is enforced rather than merely
documented.

### 2. The invariant — agents never make authorization decisions

Three mechanical properties, each independently tested:

1. **`authorize` is the first node and every path to a retriever passes through
   it.** It is a plain function: session → `AuthorizedScope`. No model, no prompt,
   no tool. Instrumented retrievers assert the ordering on every run.
2. **Branches receive `AuthorizedScope`, never the caller's raw request.** `Send`
   payloads carry the narrowed scope. A retriever cannot widen what it never saw.
3. **Every retriever re-asserts scope at the data layer.** Defence in depth: a
   branch handed a deliberately widened scope still queries only the authorized
   ids.

This inverts the objection into the headline. Multi-agent is safe **here**
precisely because the multi-agent part is strictly downstream of a deterministic
gate. If that ordering is ever reversed, the design is wrong and the test fails.

**Denied requests load zero rows.** Not "load then filter" — the denial happens
before any retriever executes, and the record loader's call count is asserted to
be `0`.

### 3. The model appears once

Only at `synthesize`, over material already filtered by the authorization gate.
The model never sees an unauthorized row, never chooses what to fetch, and never
decides who may see what.

### 4. Per-domain degradation

Each branch returns `DomainResult(status: ok|degraded|unavailable, data, note)`.
The reducer merges partials. The synthesis node is told which domains are missing
and **must say so** — a view with coverage unavailable renders three sections and
an honest note. It does not fail, and it does not silently omit.

This is W3's lesson made structural: the payer will be down again.

### 5. HITL at the sensitivity gate

`sensitivity_gate` calls LangGraph `interrupt()` when an assembly is
disclosure-shaped or spans more than one patient. The run pauses on the
checkpointer and resumes with `Command(resume=True|False)` — the only `Command`
form used as graph input; `update`/`goto`/`graph` are for returning from nodes.

Placement follows the debate's test for a human gate (D5): **low volume, no cheap
undo, catastrophic if wrong**. The one request that must never auto-succeed is the
one that looks like the IDOR we just found.

We deliberately do **not** gate ordinary same-patient views. A human gate on a
high-volume path is not a control — it is a workaround generator, and workarounds
are unauditable by definition.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Single agent, eight tools** | Four domains' authorization rules and failure semantics in one context; documented tool-choice degradation; no per-domain isolation. |
| **`asyncio.gather`, no model** | Correct for the *retrieval*, and we use exactly that underneath. Loses the cross-domain narrative, which is the client's actual ask. |
| **Subagents pattern** | Costs an extra model call per request (LangChain's own comparison) for centralized control we do not need, since routing here is rule-based, not model-decided. |
| **Handoffs pattern** | Optimized for multi-hop conversation with direct user interaction. This is one-shot assembly; handoffs buys nothing and adds a control-transfer surface. |
| **`langgraph-supervisor`** | 0.0.31, 2025-11-19, pre-GA, unmaintained. |
| **Post-filter assembled output** | The naive version. Loads unauthorized rows into memory, into logs, into traces, and into the model's context before removing them from the response. Filtering the *output* of a breach is not preventing one. |

## Consequences

- The IDOR is closed **on the new path**, with a permanent regression test that reproduces the HAR walk against the legacy path and fails loudly if anyone removes the check.
- The feature the client asked for is delivered as an **expansion** — "assemble the full picture *safely, from four systems, degrading per-domain*" — and the writeup says explicitly that we expanded it rather than pretending she asked.
- Authorization logic lives in one node instead of being scattered across every query. That is a real maintainability gain and also a single point of failure; it is therefore the most-tested code in the week.
- We now run four retrievals per view where the legacy path ran one N+1 loop. The N+1 is **measured and named, not fixed** (D8) — fixing it is a records-service change outside this week's scope.
- The KG remains a seeded sample (ADR 0010). The security finding is the headline; the graph is the vehicle.
- Sessions still never expire and there is still one role for everyone (D10, D7). Both named here, both W9/W10 work. The authorization gate narrows *which patient*, not *which staff role* — that distinction must not be oversold to the client.
