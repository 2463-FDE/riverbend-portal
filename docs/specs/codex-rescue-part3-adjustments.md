# codex:rescue — Part III design review, findings and verdicts

- **Reviewed:** `docs/design-debate-w1-w4.md` Part III, `docs/specs/ui-ingestion-and-agent-workflow.md`, `adr/0014`, `adr/0015`
- **Date:** 2026-07-29, before any implementation
- **Findings:** 12 — 2 critical, 6 high, 4 medium
- **Verdicts:** 10 accepted, 2 accepted-with-modification, 0 rejected

Two findings turned out to describe **live vulnerabilities in already-merged
code**, not flaws in the proposed design. Both were confirmed by exploiting the
running stack, and both are fixed in this PR rather than deferred.

---

## F1 — Client-supplied `patient_scope` reaches the record collection

**Critical. ACCEPTED. Fixed in this PR.**

Not a design flaw — a live cross-patient PHI disclosure, present since `#14` and
reachable by patients since `#16`. Confirmed by exploit: `maria.gonzalez`
retrieved James O'Brien's chart 1043, grounded and cited by name.

Full write-up: `docs/findings/w2-knowledge-query-scope-idor.md`.

**Applied:** `/ai/knowledge/query` rejects a client-supplied `patient_scope` with
400; new `/ai/records/query` derives scope from the session and overwrites rather
than merges. 12 regression tests, mutation-checked.

codex's proposed fix was adopted essentially verbatim. Worth stating plainly:
the review found this by reading the *design documents*, noticing they left the
query proxy unexamined, and going to look. That is the value of the gate.

## F2 — Diagnostic routes expose cross-patient data to patients

**Critical. ACCEPTED. Fixed in this PR.**

Also live. As `maria.gonzalez`, a patient:

- `/ai/knowledge/identity-clusters` → every patient's name, chart ids, and match
  reasons including `identical_ssn` and `identical_address`
- `/ai/knowledge/corpus` → `"James O'Brien — office_visit 2026-02-20"`
- `/ai/knowledge/eval` → the same, inside `identity_split_examples`

The spec compounded it: `RVB-AG-09` said query and eval are open to any
authenticated session, and `RVB-W2-U4` then required the quality screen to render
named patients and chart ids. Those two requirements together specify a leak.

**Applied:** `require_staff()` on all four routes, keyed on the **resolved
principal** rather than the raw role string, so `scope.py` stays the single
authority on what a session is. `RVB-AG-09` is corrected: the quality screen is
staff-only; only `/ai/knowledge/query` is open to any session.

## F3 — Metadata is outside the scrub and the preview

**High. ACCEPTED.** Titles, filenames and `source` become citations and corpus
entries. A clean body in `Maria Gonzalez appeal.pdf` still leaks.

**Spec updated:** `RVB-ING-31`–`RVB-ING-33` — metadata is scrubbed on the same
path as the body, appears in the preview, and identifier-shaped metadata blocks
the commit rather than being silently rewritten. Implemented in the ingestion PR.

## F4 — The legacy JSON ingest bypasses the new gate

**High. ACCEPTED.** `adr/0014` claimed a human gate on "the highest-blast-radius
write in the system" while `POST /ai/knowledge/ingest` continued to write
directly. The claim was false as written — exactly the class of overstatement
this engagement keeps correcting.

**Applied to the design:** there is **one** write path. The paste flow stages
through the same preview and commit as upload; `/ai/knowledge/ingest` is removed
from the gateway rather than left as a quiet second door. `RVB-ING-34`.

## F5 — "10 MB enforced at the gateway" is not a specified control

**High. ACCEPTED WITH MODIFICATION.**

*Accepted:* the cap must be enforced by streaming rather than after the body is
buffered, and the ASGI/proxy limit has to be stated rather than assumed.
`RVB-ING-08` now specifies a streaming read that aborts past the limit, plus the
uvicorn/proxy setting.

*Modified:* per-user rate limiting is **not** adopted here. The gateway has no
rate limiting at all, on any route, and adding it for one endpoint would imply a
protection the other twenty-eight do not have. That is a gateway-wide concern —
recorded in `docs/debt-register.md` as such, not smuggled in as an upload
feature.

## F6 — In-process PDF parsing without isolation

**High. ACCEPTED WITH MODIFICATION.** The strongest disagreement in this review.

*Accepted:* a 422 does not address CPU exhaustion, decompression blowups, or
parser CVEs. `RVB-ING-35`–`RVB-ING-37` add a wall-clock timeout, an output-size
guard during extraction, and page/size caps checked before extraction begins.

*Rejected for this phase:* running extraction in an isolated worker or container.
It is the right end state and the wrong thing to do here. It introduces a queue,
a second deployable, and a new failure mode into a phase already carrying a
security fix and three UI PRs, and a half-built isolation boundary is worse than
a documented in-process one.

**The honest statement, which goes to the client rather than into a comment:**
extraction runs in-process in the service that owns the vector store, bounded by
time, size and page limits. A parser compromise is not contained by those limits.
Recorded as **residual risk** in `adr/0014` and `docs/debt-register.md` with
isolation named as the mitigation. We are not claiming containment we do not have.

## F7 — Commit quota and single-use are not atomic

**High. ACCEPTED.** Two concurrent commits could both pass
`enforce_cap(existing=index.count())` and both add; a crash between `add` and the
Redis delete could double-index on retry.

**Spec updated:** `RVB-ING-19` uses an atomic `GETDEL` so exactly one commit can
win a staging id, and `RVB-ING-38` makes chunk ids deterministic from the staging
id so a retry after a partial add overwrites rather than duplicates.

## F8 — The approvals queue has no authorization model

**High. ACCEPTED.** The spec defined listing and deciding without saying who may.
codex also spotted the sharp edge: patient-access-based resume would let the
**subject of the record approve their own sensitivity gate**, which is not
human-in-the-loop, it is a rubber stamp with extra steps.

**Spec updated:** `RVB-AG-21`–`RVB-AG-23` — an explicit `can_approve` capability
alongside `can_ingest`, staff-only, and a patient can never approve a gate on
their own record even if they otherwise hold it.

## F9 — Resume is steerable by a client-supplied `thread_id`

**High. ACCEPTED.** `/ai/patient-view/{patient_id}/resume` checks the path
patient id and then forwards a client `thread_id` verbatim. Same shape as F1:
the server checks one thing and lets the client choose another.

**Spec updated:** `RVB-AG-24` — resume takes an opaque server-issued approval id
bound to patient, requester, gate state and permitted approver. `thread_id` stops
being part of the client contract.

## F10 — `SqliteSaver + EncryptedSerializer` overstated as a compliance claim

**Medium. ACCEPTED.** `adr/0015` turned a library choice into a 164.312(a)(2)(iv)
claim. Serializer encryption does not cover WAL and temp files, filesystem
permissions, backups, or key custody and rotation.

**Applied:** the ADR is amended to state it as an implementation component, with
key management, storage permissions and backup handling called out as unbuilt.
`RVB-AG-13` no longer reads as "encrypted therefore compliant."

## F11 — Redis staging holds possibly-PHI text unencrypted and unaudited

**Medium. ACCEPTED.** The staged document is explicitly *not* de-identified — that
is the whole premise of the preview — and it sits in Redis for 30 minutes.

**Spec updated:** `RVB-ING-39`–`RVB-ING-41` — staged payloads encrypted at rest
with the same key path as the checkpointer, stage/preview/commit/discard all
audited, and a per-user cap on concurrent staged bytes so abandoned uploads
cannot accumulate.

## F12 — The seven-outcome model has no response contract

**Medium. ACCEPTED.** `adr/0015` asserts seven outcomes across endpoints whose
payload shapes differ. Without a contract the UI invents per-endpoint mappings
and breaks silently when a backend field moves.

**Spec updated:** `RVB-AG-25` — a single `AgentOutcome` discriminator derived once
in `frontend/app/lib/outcome.ts`, with a fixture per outcome per endpoint, so a
shape change fails a test instead of rendering the wrong state. This is the same
mistake class as `resolvePrincipal` mirroring `scope.py`, and gets the same
treatment: one place, tested.

---

## What this review changed about the plan

Two criticals were **already shipped defects**, so the phase now opens with a
security PR rather than the W2 UI. The remaining ten are folded into the PRs that
implement the surfaces they concern, and the numbering shifts by one.

Both criticals share a mechanism with the two live defects found when the stack
was first started: **the components were individually correct and the seam
between them was not**. `scope.py` was right; the route that never called it was
the vulnerability. That is now the thing being tested — `RVB-AG-26` requires an
enumeration test over every `/ai/*` route asserting each one either derives scope
server-side or provably cannot reach the record collection. Route coverage, not
behaviour coverage, because the failure was an unlisted route.
