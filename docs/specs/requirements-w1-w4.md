# Riverbend W1–W4 — Client Requirements Matrix

**Client:** Dr. Maya Okonkwo, COO — Riverbend Community Health
**Source of truth:** `projects/healthcare/client-packets.md` (W1–W4) + `projects/healthcare/README.md` §3 debt table / §4 hidden problems
**Engagement model:** forward-deployed incremental improvement on a brownfield portal. Not a rewrite.
**Status:** baseline for PR 1–4. Every requirement here must be satisfied or explicitly deferred with a named reason.

---

## 0. How to read this

Each week has three obligations, and **all three** must land:

| Obligation | What it is | Why it exists |
|---|---|---|
| **SURFACE** | The feature Dr. Okonkwo asked for | She has to see the thing she paid for |
| **HIDDEN** | The latent problem discoverable in her own handover artifacts | This is the FDE differentiator — she cannot name it herself |
| **ARTIFACT** | The sized deliverable (client / harness / spec / ADR / instrument) | The engagement's unit of value; never a whole service |

A PR that ships SURFACE without HIDDEN has failed the engagement, not the sprint.

**Requirement ID format:** `RVB-W<week>-<nn>`. Types: `F` feature, `D` discovery/finding, `A` artifact, `C` compliance, `T` test/evidence, `X` constraint.

**Global constraints (apply to every week):**

| ID | Constraint | Source |
|---|---|---|
| `RVB-X-01` | All model inference goes through **AWS Bedrock**. No other LLM vendor. | Org mandate (second-brain `rag-vector-store-free-tier-research`, 2026-07-06) |
| `RVB-X-02` | Orchestration uses **LangGraph v1** (`langgraph>=1.0,<2.0`) and LangChain v1. `create_react_agent` is deprecated — use `create_agent`. | User mandate + LangGraph v1 release notes |
| `RVB-X-03` | Vector store is **ChromaDB**, local `PersistentClient` in dev/CI, `HttpClient` against a compose service for production. | User mandate + Chroma client/server docs |
| `RVB-X-04` | **Zero live LLM/embedding spend** until the key-gated smoke test. Every test in CI runs offline and deterministic. | User guideline 2/3 |
| `RVB-X-05` | Every material decision is recorded in an ADR under `adr/`, with a citable primary source. | User guideline 7 |
| `RVB-X-06` | Each week's deliverable is sized for ~40 hrs. If the real fix is service-sized, ship the **spec/ADR + vertical-slice prototype** and roadmap the rest. | `client-packets.md` §Scope guardrails |
| `RVB-X-07` | The 14 seeded debt items are **discovery material**. Only fix the ones that map to the week in play; name the rest. | `README.md` §3 ⛔ |
| `RVB-X-08` | PHI never enters a prompt, a trace, a log body, or a vector index without an explicit, tested gate. | 164.502(b), 164.502(e) |

---

## Week 1 — LLM Engineering for Production

> *"Welcome aboard. Two things — the registration page feels slow, and the board wants 'AI,' so start us off with a little assistant that can draft a patient-friendly summary of our intake instructions. Just get the repo running and show me something."*

### Handover artifacts containing the evidence
- Repo with a **committed, tracked `.env`** (DB creds, payer API key, Bedrock key); `.env` absent from `.gitignore` → **D9**
- `logs/intake-service.log`: `INFO request body={"name":..., "dob":"1971-03-02","ssn":...,"insurance_id":...}` on every POST → **D1**
- `README` claiming *"All PHI encrypted — HIPAA compliant"*; `db/schema.sql` shows plaintext `ssn`, `dob`, `notes` → **D3**, twist #1
- `docs/handover/jira-tickets.md` **RIV-088** — "registration spins ~4–5s" (the *stated* slowness; its root cause is W3's D4)

### Requirements

| ID | Type | Requirement | Acceptance |
|---|---|---|---|
| `RVB-W1-01` | F | A **production LLM client wrapper** over Bedrock: bounded connect/read timeout **and** an overall wall-clock deadline covering all retries. | Test proves a hung Bedrock call aborts at the deadline, not the socket timeout × retries. |
| `RVB-W1-02` | F | **Retry with jittered exponential backoff**, retrying only throttling/5xx/timeout classes; validation/auth errors fail fast. | Table-driven test over error classes asserts retry vs no-retry. |
| `RVB-W1-03` | F | **Structured-output parsing** — the model is constrained to a typed shape; malformed output degrades gracefully, never 500s. | Test feeds malformed/partial/prose responses; all parse or fall back safely. |
| `RVB-W1-04` | F | **Token + cost guard** — pre-call input-token cap and per-request USD ceiling; over-budget requests refused *before* spend. | Test asserts `BudgetError` raised with zero client calls made. |
| `RVB-W1-05` | F | The summary feature is reachable end-to-end through the existing gateway → portal path, session-guarded like every other route. No new unauthenticated surface. | Gateway test: `POST /ai/summary` returns 401 without a session. |
| `RVB-W1-06` | C | **Instructions-only contract.** The endpoint accepts intake *instruction text*; a patient record cannot be passed by construction (no such fields exist), and a scrub gate redacts anything PHI-shaped that slips in. | Test posts SSN/DOB/MRN/phone/email-bearing text; asserts redaction and that `found` kinds are reported. |
| `RVB-W1-07` | C | **PHI-safe logging policy** — no request/response bodies logged anywhere on the AI path; one structured audit event per call (id, model, latency, tokens, cost, outcome). A redaction filter is the defensive backstop. | Test captures log records for a PHI-bearing request and asserts no identifier appears in any emitted record. |
| `RVB-W1-08` | D | **Finding D1** — PHI in plaintext logs in `intake-service`. Named, evidenced (log excerpt), risk-framed in business language. | Debt-log entry with reg hook 164.502(b)/164.312(b) and a breach-exposure framing. |
| `RVB-W1-09` | D | **Finding D9** — secrets committed to the repo; `.env` tracked and un-ignored. | Debt-log entry; **plus** the tracked-`.env` is remediated in this PR (see `RVB-W1-12`). |
| `RVB-W1-10` | D | **Finding D3 / twist #1** — "HIPAA compliant" is self-asserted; encryption is disk-level only, PHI columns are plaintext `TEXT`. | Debt-log entry citing 164.312(a)(2)(iv) and the 2025 Security Rule NPRM direction of travel. |
| `RVB-W1-11` | A | **Onboarding seam map** (1 page) — services, ports, data stores, trust boundaries, and where PHI crosses each one. | Reviewable by a new engineer on day 1 without reading code. |
| `RVB-W1-12` | C | `.env` **untracked** and added to `.gitignore`; `.env.example` carries the key names only. Any previously committed secret is flagged for **rotation at source** — history rewrite alone is insufficient. | `git ls-files` shows no `.env`; README/debt-log states the rotation obligation. |
| `RVB-W1-13` | A | **ADR** for the production Bedrock client: why Bedrock (HIPAA-eligible under BAA, providers can't read prompts/completions), why Converse, the resilience/cost/validation envelope, and the key-posture finding (long-term key = exploration only). | ADR cites AWS HIPAA-eligible services list + Bedrock API-keys doc. |
| `RVB-W1-14` | T | Full test suite runs **offline**, deterministic, zero AWS calls, zero spend. | `pytest` green with no credentials present. |

**Explicitly NOT in W1:** fixing RIV-088's root cause (that is D4 → W3), field-level encryption (W9), the clinical-record summary path (W8, gated on a BAA).

---

## Week 2 — RAG & Knowledge Retrieval

> *"Clinicians waste time hunting through a patient's history. Build me a retrieval helper that pulls the right past records when they open a chart."*

### Handover artifacts containing the evidence
- A dump of `patients` + `encounters`. Buried in it: **one human, three patient IDs** — different MRNs, slight name/DOB spelling variants, all created via self-service intake → **twist #3**, **D5**
- A contractor-left **retrieval gold-set**: e.g. *"show me Maria Gonzalez's allergies"* returns records from only **one** of her three fragments
- `docs/handover/jira-tickets.md` **RIV-160** — Dr. Nguyen: *"Why does the allergy list look different depending on which chart I open for the same lady?"* — the clinician has already felt this and mis-attributed it

### Requirements

| ID | Type | Requirement | Acceptance |
|---|---|---|---|
| `RVB-W2-01` | F | Retrieval pipeline over a **sampled** patient-record corpus: chunking → embedding → **ChromaDB** index → top-k retrieval. | Ingest + query works against `PersistentClient` with no network. |
| `RVB-W2-02` | F | Embeddings via **Bedrock Titan Text Embeddings V2** (`amazon.titan-embed-text-v2:0`) in production; a deterministic offline backend in dev/CI. Backend selectable by env. | Same code path both ways; CI uses the offline backend. |
| `RVB-W2-03` | F | **Embed once, cache.** Re-running ingest or eval must not re-embed unchanged content. | Test asserts embedding-call count is zero on a second identical ingest. |
| `RVB-W2-04` | F | Retrieval supports **dense**, **sparse/lexical**, and **hybrid** fusion, selectable per query. | Eval harness reports metrics per mode. |
| `RVB-W2-05` | F | Grounded answer generation with **citations** back to retrieved chunks; refuse-to-answer when nothing relevant is retrieved. | Test: off-topic query returns a refusal, not a guess. |
| `RVB-W2-06` | A | **RAG retrieval eval harness** computing recall / precision over the contractor's gold-set, plus groundedness and answer-match. | `POST /eval` returns a run with all four metrics; results persist across restart. |
| `RVB-W2-07` | D | **The harness surfaces the fragmentation.** Report must include a **duplicate-patient rate** and a **fragment-coverage gap** metric — "for query X, we retrieved 1 of the 3 chart fragments belonging to this human." | Test: a seeded 3-fragment patient produces `fragment_coverage < 1.0` and a non-zero duplicate rate. |
| `RVB-W2-08` | D | **Finding twist #3 / D5** — no MPI / match key. Self-service intake forks one person into N charts; retrieval looks healthy while grounded on a partial record. Framed as **patient-safety** risk (clinician acts on an incomplete allergy/med list), not a tuning problem. | Debt-log + eval report narrative; ties to RIV-160. |
| `RVB-W2-09` | A | **ADR proposing an MPI / match-key approach** — deterministic match key, probabilistic candidate scoring, merge/link vs merge-hard, and where the check belongs (intake write path, not retrieval). | ADR states explicitly that *no retrieval tuning fixes a fragmented source of truth*. |
| `RVB-W2-10` | C | Documents entering the index are scrubbed of patient-identifier-shaped tokens; the index is a PHI boundary with a named owner and an ingest authorization gate. | Ingest requires a privileged capability; unprivileged session gets 403. |
| `RVB-W2-11` | X | **Quota discipline** — corpus capped to a sampled subset; embeddings cached; no re-embed per run. Cap is configurable and logged. | Documented cap; log line states corpus size and cache hits. |
| `RVB-W2-12` | T | Offline, deterministic, zero-spend test suite including the gold-set eval. | `pytest` green with no credentials. |

**Explicitly NOT in W2:** implementing the MPI merge (ADR + metric only — it is service-sized), re-embedding the full record dump, fixing D5's appointment-side idempotency (W5).

---

## Week 3 — Single-Agent Design + Memory

> *"Front-desk eligibility checks are painful. Build a little assistant the staff can chat with that checks a patient's insurance eligibility and remembers the context of the visit."*

### Handover artifacts containing the evidence
- `services/eligibility-service/check.py` — the 270/271 payer call is a **synchronous request with no timeout**, executed inside the request thread, and `intake-service` triggers it **inline on the registration path** → **D4**
- `docs/handover/jira-tickets.md` **RIV-141** — *"Tue 9:00–9:20am the ENTIRE intake screen froze — front desk couldn't register any patient, not just eligibility."*
- `docs/handover/payer-status-page.md` — ACME Clearinghouse incident **Tue 09:02–09:21 (19 min)**, elevated latency/timeouts on `/v1/eligibility`; portal `/intake` p95 flat ~600 ms all week except a single 20-minute spike past 30 s overlapping that window → **twist #7**
- **RIV-088** — "registration spins 4–5s" is the same defect at steady state

### Requirements

| ID | Type | Requirement | Acceptance |
|---|---|---|---|
| `RVB-W3-01` | F | A **single tool-using agent** built on LangGraph v1 with exactly one tool, `check_eligibility`. Not a chatbot with a hidden fleet of tools. | Graph compiles; tool list length is 1. |
| `RVB-W3-02` | F | **Visit-scoped memory** — conversation state persisted per visit via a LangGraph checkpointer keyed on a visit thread id; a new visit starts clean. | Test: two turns on one `thread_id` share context; a new `thread_id` does not. |
| `RVB-W3-03` | C | Checkpointed agent state may contain PHI → the production checkpointer uses **`EncryptedSerializer`**; dev/CI uses `InMemorySaver`. | Config test asserts encrypted serde is selected when the prod flag is set. |
| `RVB-W3-04` | F | The `check_eligibility` tool calls the payer through a **timeout-bounded, non-blocking** path. The call must not occupy the request thread. | Test asserts the call is awaited/off-thread and bounded. |
| `RVB-W3-05` | F | **Circuit breaker** — after N consecutive failures the breaker opens, short-circuits calls for a cooldown, then half-opens to probe. | Test drives closed → open → half-open → closed and asserts no payer calls while open. |
| `RVB-W3-06` | F | **Graceful degradation** — on timeout/open-breaker, return **last-known eligibility** from cache with an explicit staleness marker; if none, return `unknown`. Never an error that blocks the caller. | Test: payer down → response is `stale`/`unknown`, HTTP 200, with `checked_at` of the cached record. |
| `RVB-W3-07` | F | **Intake proceeds regardless.** Registration must complete during a total payer outage. | Test simulates a 20-minute outage and asserts `POST /intake` still succeeds within its latency budget. |
| `RVB-W3-08` | F | Agent tool-failure handling: the agent reports degraded/unknown eligibility honestly to staff and never fabricates a coverage status. | Test: tool returns `unknown` → asserted phrasing contains no active/inactive claim. |
| `RVB-W3-09` | D | **Finding D4 / twist #7** — synchronous external call with no timeout or breaker on the intake path. Reconstruct the Tuesday incident from the three artifacts (ticket + status page + latency series) and state the causal chain: payer degradation → blocked request threads → intake unavailable. | Findings doc with the correlation shown, framed as an **availability cliff** and a 164.308 contingency gap, in revenue/access terms. |
| `RVB-W3-10` | D | Name that **RIV-088 and RIV-141 are the same defect** at different severities — steady-state latency vs total outage. The client believes they are two problems. | Stated in the findings doc and the ADR. |
| `RVB-W3-11` | A | **ADR: sync → async + graceful degradation** — timeout budget derivation, breaker thresholds and why, cache TTL and staleness semantics, and what "eligibility unknown" means to front-desk workflow. | ADR includes the numbers and their justification, not just the pattern names. |
| `RVB-W3-12` | C | Staff questions are free text and **will** contain patient names. Names are not caught by regex scrubbing → the agent path must not export prompt bodies to any third-party trace sink unless covered by a BAA. Tracing is off by default and gated. | Test asserts tracing disabled unless explicitly enabled *and* keyed; documented in the ADR. |
| `RVB-W3-13` | T | Offline, deterministic tests including a simulated payer outage and breaker state machine. Zero spend. | `pytest` green with no credentials. |

**Explicitly NOT in W3:** observability/alerting on the intake path (W7 — we produce the *evidence* that it is missing), rewriting `interop-service`'s synchronous HL7 path (W6).

---

## Week 4 — Multi-Agent + Knowledge Graphs

> *"Let patients see their own labs and visit summaries in one place. Build something that assembles a patient's full picture across our services."*

### Handover artifacts containing the evidence
- `records-service` spec: `GET /patients/{id}/records` — **no ownership check**, `{id}` is the sequential integer primary key → **D11**
- `docs/handover/portal.har` — a logged-in patient fetches `/api/patients/1042/records` → **200**, then `/api/patients/1043/records` → **200**. Two requests, two different humans' charts, one session → **twist #9**
- Query logs / `records-service` code: assembling one patient view fires **one query per encounter** (N+1) and `records/search` is a full-table `ILIKE` scan with no index and no limit → **D8**
- Sessions never expire; one `staff` role for everyone → **D10**, **D7**

### Requirements

| ID | Type | Requirement | Acceptance |
|---|---|---|---|
| `RVB-W4-01` | F | **Knowledge-graph schema** over `patient → encounter → provider → record`, seeded from a sample. Edges carry the facts authorization needs (who owns what). | Schema doc + loader; graph queryable by patient. |
| `RVB-W4-02` | F | **Multi-agent retrieval prototype** on LangGraph v1 that assembles the patient view by fanning out to specialized retrievers and synthesizing one answer. | Graph runs; trace shows parallel branches and a synthesis node. |
| `RVB-W4-03` | C | **Authorization check at the graph boundary** — deterministic, executed *before* any retriever runs, on every branch. Not an LLM judgement, not a post-filter on assembled output. | Test: a session for patient 1042 requesting 1043 gets denied with **zero** record rows loaded. |
| `RVB-W4-04` | C | The multi-agent assembler cannot become an IDOR amplifier: no branch may widen scope beyond what the boundary check authorized. | Test attempts a cross-patient reference inside the graph and asserts it is refused. |
| `RVB-W4-05` | F | **Human-in-the-loop gate** — any assembled view that crosses a sensitivity threshold (e.g. a disclosure-shaped or cross-patient request) pauses via LangGraph `interrupt()` and requires an explicit approve/deny before release. | Test: run pauses, `Command(resume=False)` blocks release, `Command(resume=True)` releases. |
| `RVB-W4-06` | D | **IDOR finding writeup with repro steps taken from the HAR** — exact requests, exact responses, why a valid session is not authorization, and the blast radius (any authenticated user can walk sequential IDs across the whole patient table). | Writeup reproduces the 1042 → 1043 walk against the running stack. |
| `RVB-W4-07` | T | **Failing test that demonstrates the IDOR on the un-fixed path**, kept as a regression guard, alongside a passing test on the new authorized path. | Both tests present; the guard fails if the check is removed. |
| `RVB-W4-08` | D | **N+1 note** — measured query count for assembling one chart, the full-table-scan search path, and the projected cost at real corpus size. Named, not necessarily fixed. | Note with measured numbers from the seeded DB. |
| `RVB-W4-09` | D | **D10 note** — sessions never expire / no automatic logoff, framed against 164.312(a)(2)(iii) and the shared-clinical-workstation threat. | Debt-log entry. |
| `RVB-W4-10` | A | **ADR: why multi-agent here, and where it must not be trusted.** Must argue the client's ask honestly — a read view does not inherently need multiple agents — and justify the pattern actually chosen on parallelization/context-isolation grounds, with the authorization boundary held **outside** agent discretion. | ADR cites the LangChain multi-agent guidance including its "not every complex task requires this approach" caveat and the per-pattern model-call costs. |
| `RVB-W4-11` | A | **ADR: knowledge-graph modeling choice** — why a graph over a join, what it buys for authorization and provenance, and the production storage decision. | ADR with the alternatives rejected and why. |
| `RVB-W4-12` | X | KG stays a **seeded sample**. The security finding is the headline; the graph is the vehicle. | Documented scope line. |
| `RVB-W4-13` | T | Offline, deterministic tests including IDOR repro, HITL interrupt/resume, and boundary enforcement. Zero spend. | `pytest` green with no credentials. |

**Explicitly NOT in W4:** fixing N+1 / adding indexes (named, roadmapped), session-expiry implementation (W9/W10 scope), full RBAC split (W9).

---

## Cross-week traceability

| Debt | Where named | Where fixed | Deferred to |
|---|---|---|---|
| D1 PHI in plaintext logs | W1 | AI path fixed in W1; `intake-service` named | W7 instrumentation |
| D3 no field-level encryption | W1 | — | W9 |
| D4 sync external calls, no timeout/breaker | W3 | W3 (eligibility path) | W6 (HL7 path) |
| D5 no idempotency / no MPI | W2 (patient fork) | ADR only | W5 (appointments) |
| D7 role bloat | W4 (noted) | partial: knowledge-ingest capability in W2 | W9 |
| D8 N+1 + full-table scan | W4 | named + measured | roadmap |
| D9 secrets in repo | W1 | **W1 (`.gitignore` + untrack + rotation notice)** | — |
| D10 no automatic logoff | W4 | named | W9 |
| D11 IDOR on record reads | W4 | **W4 (boundary check)** | — |
| D13 PHI to LLM vendor, no BAA | W1 (avoided by construction) | instructions-only contract | W8 (Safe-Harbor scrub) |

| Client ticket | Real cause | Week |
|---|---|---|
| RIV-088 "registration spins 4–5s" | D4 sync eligibility call on intake path | W3 |
| RIV-141 "intake froze Tuesday" | D4 — same defect, outage severity | W3 |
| RIV-160 "allergy list differs per chart" | twist #3 — one human, three patient IDs | W2 |
| RIV-175 "charged twice / double-booked" | D5 — no idempotency, race on slot | W5 (out of scope here) |

---

## Fulfilment rule

A week's PR is **not** mergeable until every `F`, `C`, `A`, and `T` row for that week is satisfied, and every `D` row is written up in `docs/findings/` with evidence drawn from the handover artifacts — never asserted from outside them.
