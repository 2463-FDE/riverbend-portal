# W4 Spec — Knowledge graph + multi-agent patient view with an authorization boundary

- **Week:** 4 — Multi-Agent + Knowledge Graphs
- **Requirements covered:** `RVB-W4-01` … `RVB-W4-13`
- **ADRs:** `adr/0009` (multi-agent topology + authz boundary), `adr/0010` (KG model)
- **Branch:** `feat/w4-kg-multiagent` → `feat/riverbend-w1-w4`
- **Status:** specified

---

## 1. The ask, the trap, and the expansion

**Dr. Okonkwo asked for:** *"Let patients see their own labs and visit summaries in
one place. Build something that assembles a patient's full picture across our
services."*

**The trap, stated plainly.** Her own artifacts show:

| Artifact | Content |
|---|---|
| `records-service` spec | `GET /patients/{id}/records` has **no ownership check**; `{id}` is the sequential integer primary key |
| `docs/handover/portal.har` | A logged-in patient fetches `/api/patients/1042/records` → **200**, then `/api/patients/1043/records` → **200** |
| `gateway/app.py` | `require_session` proves *a* valid session exists. It never binds that session to the requested `patient_id` |
| query logs / `records-service` | Assembling one chart runs **one query per encounter** (N+1); `records/search` is a full-table `ILIKE` with no index and no limit |

The feature as asked — *assemble more data about a patient, from more sources, in
one place* — is a **force multiplier for the vulnerability**. Ship it naively and
we have upgraded "walk the IDs one chart at a time" into "walk the IDs and get a
synthesized narrative of each stranger's medical history."

**The honest expansion** (debate D4). We do not build multi-agent because week 4
is multi-agent week. We build it because "the full picture" genuinely spans four
systems with four different authorization rules, four failure modes, and four
retrieval strategies:

| Domain | Source | Retrieval | Fails how |
|---|---|---|---|
| Demographics | `records-service` / Postgres | SQL by id | DB down |
| Encounters & notes | `records-service` / KG traversal | graph walk | DB down, N+1 latency |
| Labs | `interop-service` HL7 feed → records | vector + graph | **silently incomplete** (D6 drops AL1/RXA) |
| Coverage context | `eligibility-service` | live payer call | **third-party outage** (D4, W3) |

Serial assembly means the payer makes the labs slow. One agent with eight tools
means one context holding four domains' rules and choosing worse about each.
**Parallel domain retrievers, each carrying its own scope binding and its own
degradation behaviour**, means one domain can fail and the patient still sees the
other three, correctly scoped.

So the client's ask is expanded from *"assemble the full picture"* to
**"assemble the full picture safely, from four systems, degrading per-domain"** —
and the writeup says explicitly that we expanded it.

---

## 2. Architecture — Router + `Send` fan-out, hand-built

```
  request(session, patient_id)
            │
            ▼
   ┌─────────────────────┐
   │  authorize (node)   │  deterministic. no model. runs FIRST.
   └─────────┬───────────┘
             │  denied ──────────────────────────────────► deny (END)
             │  authorized → AuthorizedScope(patient_ids=[…], fields=[…])
             ▼
   ┌─────────────────────┐
   │  plan (node)        │  which domains are needed? (rule-based)
   └─────────┬───────────┘
             │  Send() fan-out — each branch receives ONLY the scope
   ┌─────────┼─────────┬─────────────┬──────────────┐
   ▼         ▼         ▼             ▼              │
demographics encounters labs      coverage          │  parallel, isolated
   └─────────┴─────────┴─────────────┴──────────────┘
             │  reducer merges partials; per-domain failures degrade
             ▼
   ┌─────────────────────┐
   │  sensitivity_gate   │  disclosure-shaped / cross-patient?
   └─────────┬───────────┘
             │  yes → interrupt()  ── HITL ──► Command(resume=True/False)
             ▼
   ┌─────────────────────┐
   │  synthesize (node)  │  ← the ONLY model call. Sees only authorized material.
   └─────────┬───────────┘
             ▼
   ┌─────────────────────┐
   │  ground_gate        │  synthesis grounded in the retrieved partials?
   └─────────────────────┘
```

**`langgraph-supervisor` is not used.** It is pinned at **0.0.31, last released
2025-11-19** — before LangGraph 1.0 shipped — with no release since. A production
dependency on a 0.0.x package that predates the runtime's GA is not defensible,
and this topology is ~40 lines of `StateGraph` (ADR 0009).

### 2.0 The missing precondition — session identity binding

**The rescue review found that the authorization design could not be implemented
against this codebase.** `services/gateway/security.py` stores only `username` and
`role` in the session; `users` has no patient reference; every account is `staff`.
There was no patient identity to authorize against. "Session for patient 1042" was
a test we could not have written.

That is the real shape of D11: the missing ownership check is a *symptom*; the
cause is that the system never modelled **who a login belongs to**. `require_session`
could not have been fixed by adding a comparison, because there was nothing to
compare.

**ADR 0011** adds the minimum binding — nullable `users.patient_id` with a unique
partial index, carried into the session at login, exposed on `/me`,
**server-derived and never client-supplied**. From it:

| Principal | `AuthorizedScope.patient_ids` |
|---|---|
| **Patient** (`patient_id IS NOT NULL`) | own id **+ ids reachable by `SAME_AS`** — so a fragmented human sees their whole record |
| **Staff** (`patient_id IS NULL`) | the patient in context, subject to a treatment-relationship check. **Deliberately coarse this week** — with one `staff` role, real minimum-necessary is impossible; that is D7 and W9's work. Stated, not oversold. |

**The existing route is fixed, not just the new assembler.** `GET /patients/{id}`
and `GET /patients/{id}/records` resolve the scope and reject at the gateway
*before proxying* — that is the route the HAR walk used. Out-of-scope returns
**404, not 403**: a 403 on valid ids and 404 on invalid ones is an enumeration
oracle that confirms which patient ids exist.

### 2.1 The invariant that makes this defensible

> **Agents never make authorization decisions.**

Three mechanical properties enforce it:

1. **`authorize` runs before fan-out.** It is a plain function — session → 
   `AuthorizedScope`. No model, no prompt, no tool. It is the first node and the
   graph has no edge that reaches a retriever without passing through it.
2. **Branches receive only the scope, never the raw request.** `Send` payloads
   carry `AuthorizedScope`, not `patient_id` from the caller. A retriever
   *cannot* widen scope because it never sees the un-narrowed input.
3. **Every retriever re-asserts its scope at the data layer.** Defence in depth:
   even if a branch were handed something wider, its query is parameterized by the
   scope's patient id set. A test drives this directly.

This inverts the objection. The reason a multi-agent design is safe *here* is
precisely that the multi-agent part is downstream of a deterministic gate — and
that is the ADR's headline, not a footnote.

### 2.2 Per-domain degradation

Each branch returns `DomainResult(status: ok|degraded|unavailable, data, note)`.
The reducer merges partials; the synthesis node is told which domains are missing
and must say so. A patient view with coverage unavailable renders three sections
and an honest note — it does not fail, and it does not silently omit.

This is where W3's lesson lands structurally: the payer *will* be down again.

### 2.3 HITL — the sensitivity gate (`RVB-W4-05`)

The one request that must never auto-succeed is the one that looks like the IDOR
we just found. `sensitivity_gate` calls LangGraph `interrupt()` when the assembly
is disclosure-shaped or spans more than one patient; the run pauses on the
checkpointer and resumes with `Command(resume=True|False)`.

`Command(resume=…)` is the only `Command` form used as graph *input* — `update` /
`goto` / `graph` are for returning from nodes. Resume value becomes the return
value of `interrupt()` inside the node.

Low volume, no cheap undo, catastrophic if wrong: the debate's test for a human
gate (D5).

---

## 3. Knowledge graph (`RVB-W4-01`)

```
(Patient)-[:HAD]->(Encounter)-[:WITH]->(Provider)
                      │
                      └-[:PRODUCED]->(Record {kind, status})
(Patient)-[:COVERED_BY]->(Coverage)
(Patient)-[:SAME_AS]->(Patient)      ← W2's fragmentation, made explicit
```

**Why a graph rather than the join we already have** (ADR 0010):

1. **Authorization is a reachability question.** "May this session see this
   record?" is *"is there a path from the session's patient to this record?"* — 
   which the graph answers structurally rather than by remembering to add a
   `WHERE patient_id = ?` in every query. The IDOR exists because someone forgot
   that clause once.
2. **Provenance is an edge property.** Which HL7 message produced which record
   matters when W6 proves the mapper drops allergy segments.
3. **`SAME_AS` makes W2's finding operational.** Maria Gonzalez's three fragments
   become one traversal. The graph is where the MPI link lives *before* the merge
   is implemented.

**Storage decision:** seeded, in-process graph over the existing Postgres rows —
adjacency built at load time. **No new database.** The graph is a modelling and
authorization structure this week, not an infrastructure commitment; ADR 0010
records what would justify a real graph store later (traversal depth > 3, or
cross-patient cohort queries) and what it would cost. The KG stays a seeded sample
(`RVB-W4-12`) — the security finding is the headline.

---

## 4. Findings to produce

| ID | Finding | Content |
|---|---|---|
| `RVB-W4-06` | **D11 / twist #9 — IDOR on chart reads** | Repro from the HAR, reproduced against the running stack: log in as one patient, `GET /patients/1042/records` → 200, `GET /patients/1043/records` → 200. Explain that a valid session is authentication, not authorization; that IDs are sequential and therefore enumerable; blast radius = the entire `patients` table. Cite 164.502(b) minimum-necessary and 164.312(a) access control. |
| `RVB-W4-08` | **D8 — N+1 and full-table scan** | Measured query count for assembling one chart from the seeded DB, and the `records/search` scan with no index and no limit. Project the cost at real corpus size. Named and measured, not fixed. |
| `RVB-W4-09` | **D10 — no automatic logoff** | Sessions never expire; no TTL on the Redis session key. Framed against 164.312(a)(2)(iii) and the shared clinical workstation threat — the walk-away, not the hacker. |

---

## 5. Acceptance criteria

All offline, deterministic, zero-spend.

| # | Test | Asserts | Req |
|---|---|---|---|
| 1 | `test_kg_loads_patient_encounter_provider_record` | Seeded graph has the four node kinds and their edges | W4-01 |
| 2 | `test_kg_same_as_links_fragments` | The three Maria Gonzalez ids are connected by `SAME_AS` | W4-01 |
| 3 | `test_graph_fans_out_in_parallel` | Four branches execute; trace shows concurrent, not serial | W4-02 |
| 4 | **`test_authorize_runs_before_any_retriever`** | Instrumented retrievers record invocation order; `authorize` precedes all of them in every run | **W4-03** |
| 5 | **`test_cross_patient_denied_with_zero_rows_loaded`** | Session for 1042 requesting 1043 → denied, and the record loader's call count is **0** | **W4-03** |
| 6 | `test_branch_receives_scope_not_raw_request` | `Send` payloads contain `AuthorizedScope` and do not contain the caller's raw `patient_id` | W4-04 |
| 7 | `test_retriever_reasserts_scope` | A retriever handed a deliberately widened scope still queries only the authorized ids | W4-04 |
| 8 | `test_synthesis_sees_only_authorized_material` | Synthesis node input contains no row outside the scope | W4-04 |
| 9 | `test_sensitivity_gate_interrupts` | Disclosure-shaped request pauses; the run reports interrupted | W4-05 |
| 10 | `test_resume_false_blocks_release` | `Command(resume=False)` → no assembled view returned | W4-05 |
| 11 | `test_resume_true_releases` | `Command(resume=True)` → view returned | W4-05 |
| 12 | `test_interrupt_state_survives_process_restart` | With a durable checkpointer, a paused run resumes after restart on the same `thread_id` | W4-05 |
| 13 | **`test_har_walk_is_now_denied`** | The exact HAR sequence — session bound to 1042, then `GET /api/patients/1043/records` — is **denied on the existing gateway route**. Fails loudly if the check is removed. *(The pre-fix reproduction lives in the findings doc as evidence, not as a permanently-failing test: a test asserting the vulnerability still reproduces either breaks CI or enshrines the bug.)* | **W4-07** |
| 14 | `test_denial_is_not_an_enumeration_oracle` | A valid-but-unauthorized id and a nonexistent id return **identical** responses (404, same body) | W4-15 |
| 14b | `test_patient_id_is_server_derived` | A client-supplied `patient_id` in the request or token payload is ignored; scope comes from the `users` row | W4-14 |
| 14c | `test_patient_scope_includes_same_as_fragments` | A patient bound to one Maria Gonzalez fragment resolves a scope containing all three | W4-14 |
| 15 | `test_domain_failure_degrades_not_fails` | Coverage branch raises → view returns with three domains and `coverage: unavailable` | W4-02 |
| 16 | `test_synthesis_states_missing_domains` | Synthesis output names the unavailable domain rather than silently omitting | W4-02 |
| 17 | *(moved)* `scripts/measure_n_plus_one.py` | **No longer a test** (rescue R16 scope trim). A one-off measurement script; its output is pasted into the findings doc with the date and seed size. A test would have asserted a number that changes with seed data. | W4-08 |
| 18 | `test_no_supervisor_dependency` | `langgraph-supervisor` absent from requirements — the rejection is enforced, not just documented | — |
| 19 | `test_no_phi_in_graph_logs` | No node logs record bodies or identifiers | — |
| 20 | `test_w1_w2_w3_modules_unmodified` | This PR modifies no earlier week's core modules | — |
| 21 | **`test_e2e_patient_sees_own_view`** | A patient logs in and receives their own assembled view through the gateway; the same session requesting another patient is denied. Stub model, zero spend. **The client-visible feature, proven.** | **W4-16** |

**Live tier:** `L5 test_live_synthesis_grounded` — one real synthesis call over
seeded authorized material; asserts grounding and cost under ceiling. Gated
behind `L0` (retention preflight).

---

## 5b. Scope cuts taken after the rescue review

W4 was over budget for ~40 hours. Cut, with reasons:

| Cut | Why | Where it goes |
|---|---|---|
| `Record.source_message_id` provenance edge | The column does not exist in `db/schema.sql`; adding it is a migration W4 does not need | **W6**, where the HL7 mapper work actually requires it. ADR 0010's provenance section is marked *Proposed*. |
| Live per-domain degradation of the **coverage** branch | Reuses W3's eligibility client, which is already tested there. W4 stubs it. | Already covered by W3 |
| N+1 as a test | Asserts a number that moves with seed data | Measurement script + findings doc |

**Kept despite the budget:** HITL. It is an explicit engagement requirement and it
is one `interrupt()` call at one node — cutting it would save an hour and lose a
deliverable.

**Added by the budget:** ADR 0011's session binding. It is not optional; without
it there is no authorization gate and therefore no week. The cuts above pay for it.

---

## 6. Definition of done

- [ ] All acceptance tests pass offline, including the permanent IDOR guard
- [ ] `docs/findings/w4-idor-record-access.md` with HAR-derived repro steps verified against the running stack
- [ ] `docs/findings/w4-n-plus-one.md` with measured numbers
- [ ] D10 debt entry
- [ ] ADR 0009 (topology + authz invariant, citing LangChain's own "not every complex task requires this approach" and the per-pattern call costs) and ADR 0010 (KG model)
- [ ] No `langgraph-supervisor` dependency
- [ ] Earlier weeks' modules unmodified
- [ ] PR body answers the standing question
