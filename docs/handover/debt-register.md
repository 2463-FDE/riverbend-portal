# Riverbend — Debt Register

Running record of inherited technical and compliance debt, in the client's terms.
Each entry is named when we find it and updated when we act on it. Started Week 1;
this is the document that becomes the Week-10 handoff package.

**Status values:** `open` · `named` (documented, not fixed) · `partial` · `fixed`

---

| ID | Debt | Status | Named | Fixed | Owner-facing risk |
|---|---|---|---|---|---|
| D1 | PHI in plaintext application logs | **partial** | W1 | AI path only | Log file is an unclassified PHI store; vendors receiving it are undeclared BAs |
| D3 | No field-level encryption; compliance claim contradicts schema | **named** | W1 | W9 | A contradicted claim widens audit scope more than a missing control does |
| D9 | Live credentials committed to the repo | **partial** | W1 | Tracking fixed W1; **rotation outstanding** | `DB_PASSWORD` is direct access to every chart, bypassing every app control |
| D4 | Synchronous payer call, no timeout or breaker, on the intake path | open | — | W3 | A payer outage stops patient registration |
| D5 | No MPI / match key → one patient, several charts | **named + measured** | W2 | ADR 0007 (design only) | **Patient safety:** clinician opens a chart with an empty allergy list for a patient with a documented penicillin allergy |
| D8 | N+1 queries + full-table scan on records search | **named + measured** | W4 | roadmap | Slowest for the patients clinicians look at most |
| D10 | Sessions never expire; no automatic logoff | **named** | W4 | W9 | Shared clinical workstations; walk-away exposure. Deploying W4 needs a session flush *because* of this. |
| D11 | IDOR on chart reads; sequential ids | **fixed** (patient side) | W4 | **W4 — gateway route + scope binding** | Any authenticated user could walk the whole patient table. Closed. |
| D13 | PHI to an LLM vendor with no BAA | **avoided by design** | W1 | W1 (by construction) + `RVB-X-09` | Would have been an unsignalled breach |
| D2, D6, D7, D12, D14 | audit-log mutability, HL7 mapping loss, role bloat, ROI authorization, breach detection | open | — | W6–W10 | Not yet assessed |

---

## Week 1 detail

### D1 — PHI in plaintext logs → `partial`
`intake-service` logs full request bodies (name, DOB, SSN) at INFO to
`logs/intake-service.log`. The new AI path is built as the inverse: console only,
one closed-key-set audit event per call, no bodies, plus a redaction filter as a
backstop. `intake-service` is unchanged and remains the exposure.
**Full finding:** `docs/findings/w1-phi-in-application-logs.md`

### D3 — Self-asserted compliance → `named`
README claims all PHI encrypted; `db/schema.sql` stores `ssn`, `dob` and `notes`
as plaintext `TEXT`. The "encryption at rest is Addressable" defence does not
hold: 164.306(d)(3) requires implementing it *or* documenting why not *and*
implementing an equivalent alternative. Riverbend has done neither, so this is a
**current-rule** gap, not one the 2025 NPRM creates.
**Zero-cost action available now:** correct the README.
**Full finding:** `docs/findings/w1-compliance-is-self-asserted.md`

### D9 — Secrets in the repo → `partial`, **action required**
`.env` was tracked and un-ignored, carrying the DB password, payer API key,
session secret and Bedrock key. This PR untracks it and adds `.gitignore` rules.

> **Untracking is not rotation.** Everything already committed is in the history
> and on every clone ever made. A history rewrite does not reach forks, existing
> clones, or CI caches. **All four credentials must be rotated at source.**

**Full finding:** `docs/findings/w1-secrets-in-repo.md`

### D13 — PHI to an LLM vendor → avoided by design
The contractor's version sent full patient records to a model with no BAA. The
rebuilt path accepts instruction text only — a patient record is *inexpressible*
at the boundary, not merely rejected.

Week 1 also added a control that did not exist in the original plan. Bedrock
exposes a data-retention **mode**, and models requiring `provider_data_share`
share prompts and completions with the model provider and retain them for up to
30 days. Selecting such a model would recreate D13 exactly. The service now
refuses to serve unless the effective mode is `none` and the chosen model permits
zero retention.
**See:** `adr/0004` §1a, requirement `RVB-X-09`.

---

## Week 2 detail

### D5 / twist #3 — patient fragmentation → `named + measured`
The handover dump contains Maria Gonzalez as **three charts** (1042, 1330, 1588)
with identical SSN, MRN, address, phone and member id. Chart 1330 records a
**penicillin allergy**; the other two record none. Chart 1588's DOB is the same
date with the day and month transposed, which `dob TEXT` cannot detect.

The contractor's own gold-set encodes the bug as the correct answer — it expects
*"No known allergies on file."* for Maria Gonzalez. A retriever scoring 100%
against it tells a clinician she has no allergies.

Our eval harness reports **recall 1.0 alongside fragment coverage 0.556**, plus
the concrete identity split and the reasons it matched. With the proposed match
key, the same query on the same corpus recovers the allergy.

**RIV-160 should be re-triaged** from a display bug to a data-integrity defect.

**Full finding:** `docs/findings/w2-patient-fragmentation.md`
**Proposed remediation:** `adr/0007` (match key on the intake write path; link,
do not merge; surface near-misses to a human).

### New in W2 — the vector index is a PHI store
`riverbend_records` holds patient chart text. It is governed like the database:
queries **must** carry an authorized patient scope, and the adapter raises rather
than serving an unscoped similarity search. Its backup tarball is a PHI artifact
and needs the same handling as a database dump (see `docs/runbook.md`).

Note the honest consequence: **D3 (no encryption at rest) now applies in one more
place than it did before.** We have not made the index safer than the database;
we have made its status explicit.

### Interim control — knowledge-base ingest capability
Adding a document to the knowledge base is gated on a `knowledge_admin`
capability, enforced at the gateway via an env allowlist. This is **not**
least-privilege and does **not** resolve D7 (role bloat) — it is a capability
bolted beside a role model that cannot express capabilities yet. W9 replaces it.

---

## Week 3 detail

### D4 / twist #7 — the availability cliff → `partial` (eligibility path fixed)
`check.py` called the payer with no timeout, no retry, no breaker and no cache,
and `intake-service` called it **inline on the registration request thread**.
When the clearinghouse degraded Tuesday 09:02–09:21, every registration inherited
the hang and the front desk could not register anyone for nineteen minutes.

**RIV-088 and RIV-141 are the same defect** at two severities — closing the first
cosmetically would leave the second live.

Fixed: registration no longer waits for the payer; the payer call is async,
5-second bounded, circuit-broken at 5 consecutive failures with a 30-second
cooldown, and backed by a 24-hour last-known cache. Replaying the 19-minute
outage costs registration nothing.

**Still open:** `interop-service` has the same synchronous shape on the HL7 path
(W6).

**Full finding:** `docs/findings/w3-eligibility-availability-cliff.md`
**ADR:** `adr/0008`

### New in W3 — an observability gap, named
Nobody was alerted on Tuesday. The incident was found by the front desk, reported
as a UI freeze, and reconstructed weeks later from a *vendor's* status page. W3
produces the evidence; **W7 owns the fix.** `GET /breaker` on eligibility-service
is the seam alerting hangs off.

### New in W3 — a process change the front desk needs to hear about
Eligibility is now eventually consistent. `pending` means *checking*, `stale`
means *true earlier today, payer currently unreachable*, `unknown` means *proceed
and mark unverified*. **`unknown` is not `inactive`** — the API returns
`active: null` rather than `false`, because conflating "we could not check" with
"not covered" is how a covered patient gets turned away.

### New in W3 — one place where PHI at rest IS encrypted
Agent conversation state contains what staff typed, which contains patient names.
The production checkpointer uses `EncryptedSerializer`. Worth noting precisely
because it contrasts with D3: this is the only store in the system where
encryption at rest is actually implemented rather than asserted.

### New in W3 — a disclosure path held closed
Staff free-text contains patient names, and regex scrubbing does not catch names
(recorded as a W1 limitation; here it becomes material). Third-party tracing
therefore requires **both** an explicit flag and a key — a stale ambient env flag
cannot start shipping prompt bodies off-box. Enabling it needs a BAA covering the
trace vendor, or name detection on this path.

---

## Week 4 detail

### D11 / twist #9 — IDOR on chart reads → `fixed` (patient side)
`docs/handover/portal.har` shows one session fetching `/api/patients/1042/records`
→ 200 and then `/api/patients/1043/records` → 200. Two different humans' charts,
sequential integer ids, one login.

**It was not a forgotten `if`.** `require_session` answered "is someone logged
in?" and could never answer "is this the right someone?", because `users` had no
reference to a patient and the session carried none. "Let patients see their OWN
records" was not expressible in the schema.

Fixed in three parts: a nullable `users.patient_id` with a unique partial index
(migration 009), the binding carried into the session **server-derived**, and an
`AuthorizedScope` resolved at the gateway **before proxying**. Denials return
**404, not 403** — a 403 on a real id and a 404 on a nonexistent one is an
enumeration oracle.

A patient's scope includes charts linked by `SAME_AS`, so the W2 fragmentation
does not become a W4 access denial: Maria must be able to see the chart carrying
her own penicillin allergy.

**Full finding:** `docs/findings/w4-idor-record-access.md` · **ADR:** `adr/0011`

**Not fixed:** staff scope remains coarse (D7, W9). The gate narrows which
PATIENT, not which staff role.

### D8 — N+1 and full-table scan → `named + measured`
One query per encounter to assemble a chart; `records/search` is a full-table
`ILIKE` with no index and no `LIMIT`. Measured via
`scripts/measure_n_plus_one.py`: 11 queries at 10 encounters, 101 at 100. The
system is slowest for the patients with the longest histories — backwards from
every clinical priority. Roughly a day to fix, in a different service; scheduled
separately so it does not obscure the authorization review.
**Full finding:** `docs/findings/w4-n-plus-one.md`

### D10 — sessions never expire → `named`, and it affects the W4 deploy
No TTL on the Redis session key; `auth.yaml` says `SESSION_TIMEOUT: never`. The
realistic threat is a shared clinical workstation and a walk-away, not an
attacker. 164.312(a)(2)(iii) names automatic logoff specifically.

**Deployment note:** tokens issued before W4 carry no `patient_id` and resolve as
staff principals. That is the safe direction, but it means **a deploy of W4
should flush existing sessions** — and doing that is awkward precisely BECAUSE
sessions never expire.
**Full finding:** `docs/findings/w4-no-automatic-logoff.md`

### New in W4 — a bug worth recording, caught by its own test
The graph traversal was depth-first, which marks a node `seen` at whatever depth
it is popped at rather than its shortest path. That silently dropped
`record:1330:1` — the chart carrying the penicillin allergy — from a traversal
rooted at chart 1042. The Week-2 failure, re-created inside the Week-4 fix, and
failing the same way: quietly, with a plausible-looking result. Fixed by using
BFS, which visits every node at its minimum depth.

Similarly, the HITL `cross_patient` flag was not declared in the graph's state
schema, so LangGraph dropped it and the sensitivity gate never fired while every
other test still passed. A control that fails open without any error is the worst
kind; both are now pinned by tests.
