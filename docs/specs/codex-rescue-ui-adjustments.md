# codex:rescue — UI phase: findings, verdicts, and plan adjustments

- **Date:** 2026-07-29
- **Reviewer:** OpenAI Codex CLI 0.133.0, `model_reasoning_effort=high`
- **Scope:** `docs/specs/ui-w1-w4.md`, `adr/0012`, `adr/0013`, the UI debate, against the merged backend and the existing `frontend/`
- **Result:** 19 findings (15 P1, 4 P2). **18 accepted, 1 accepted-with-modification, 0 rejected.**

The review was run **before any UI code was written**, which is the only reason
most of this is cheap. The spec was written from memory of a backend I had built
days earlier, and memory was wrong about five contracts.

---

## The two that changed the design

### R1 — The backend already tells the patient what UI-D4 said it must not **[P1-6, P1-7 · ACCEPTED — and wider than reported]**

`services/ai-orchestrator/patient_view_loaders.py:74` returns, to a **patient**:

```
This record is spread across 3 charts (1042, 1330, 1588), which appear to be
the same person. They have been shown together.
```

Debate decision UI-D4 settled that the patient sees the *system fact* and not
chart-level detail, precisely because *"chart 1042 is missing your penicillin
allergy"* is a clinical interpretation delivered by a web page with no clinician
present. **The backend was already violating the decision I had just written.**

**Codex under-reported it.** Line 69 also builds:

```python
data = [f"{p.name} (chart {p.id}), date of birth {p.dob}" for p in rows]
```

so chart IDs go into the **synthesis context** as well — into the model prompt,
and from there into the patient-facing summary. Fixing only the `note` would have
left the same content arriving by a different route.

**Not a cross-patient PHI leak** — all three charts belong to the same person, and
it is her own date of birth three times. Stating that plainly matters: this is a
*clinical-communication* defect, not a breach, and calling it a breach would be
the overclaiming we criticised in the README.

**Applied:** `load_demographics(ids, audience)` where audience is `patient` or
`staff`. Patient gets the neutral count and no chart IDs, in either `note` or
`data`. Staff keep the detail. **Server-side, because P1-7 is right that hiding it
in React is not privacy** — if the API returns it to a patient, it was disclosed.

### R2 — The principal fallback in the spec was a privilege escalation **[P1-2 · ACCEPTED]**

The spec said a malformed or absent `patient_id` resolves to **staff**.
`services/gateway/scope.py:86` actually does:

```python
except (TypeError, ValueError):
    # A malformed session is not a licence to see everything.
    return AuthorizedScope(principal=PRINCIPAL_PATIENT, username=username)
```

A malformed value becomes a **patient with an empty id set** — permitted to see
nothing. Only an absent value becomes staff.

The backend fails closed. My spec described it failing **open**, and a
`usePrincipal()` built to that description would have shown staff navigation to a
principal the gateway treats as a patient who can see nothing. Confusing at best;
at worst it teaches the next developer that "malformed → staff" is the rule.

**Applied:** spec §3 corrected to mirror `scope.py` exactly, with the three cases
named separately: absent → staff, malformed → patient-with-nothing, valid →
patient-with-scope.

---

## Findings accepted and applied

### R3 — Summary reason codes do not exist **[P1-1 · ACCEPTED]**
Spec had staff seeing `invented_medication:metformin` on a withheld summary.
`SummaryResponse` (`services/ai-orchestrator/app.py:66`) carries only
`request_id, summary, grounded, needs_review, model, stubbed, usage`. The
guardrail reasons go to the **audit event** and nowhere else.

**Applied:** the staff-only reason display is **cut**, not added to the response.
Adding it would return clinical strings to patients too (P1-7), and the audit log
is the correct home for them. The withheld message says it was held for review;
that is enough to stop a retry loop, which was the actual requirement.

### R4 — Refusal copy mismatch **[P1-4 · ACCEPTED]**
Spec: *"I don't have that in the knowledge base."*
`rag_graph.py:37`: *"I don't have that information in the knowledge base."*
**Applied:** the component test asserts against the backend constant, not a
transcription of it.

### R5 — `/approvals` has no backend queue **[P1-5, P2-19 · ACCEPTED]**
The gateway exposes `POST /ai/patient-view/{id}/resume` and **no list endpoint**.
There is no way to enumerate paused runs, so a queue screen cannot be built.

**Applied:** `/approvals` is **cut from `#20`**. Resume remains reachable as an
API route and a component state on the assembled view. The missing
list-pending-approvals contract is named in the spec as the precondition, so the
screen becomes buildable the day that endpoint exists rather than being quietly
forgotten. This also does most of the work of P2-19's "W4 is too large".

### R6 — The gateway discards upstream HTTP status **[P1-8 · ACCEPTED]**
`services/gateway/app.py:383` — `_post`/`_get` return `r.json()` and drop
`r.status_code`. A downstream 422 or 503 arrives at the browser as **HTTP 200
with an error body**.

**Verified the blast radius myself, because it matters which errors are affected:**
gateway-**raised** exceptions (`require_patient_access` → 404,
`require_session` → 401, `authz.require_ingest` → 403) are raised *before* the
proxy call and keep their status. Only **downstream** errors flatten. So the IDOR
404 the whole of W4 rests on is unaffected — but every service-level failure looks
like success to the UI.

**Applied:** `_post`/`_get` preserve upstream status in `#17`, with tests. This is
backend work inside a UI PR, named as such: every subsequent UI error state
depends on being able to tell failure from success.

### R7 — Missing states, both endpoints **[P1-9, P1-10 · ACCEPTED]**
The spec named three states for `/ai/summary`. The endpoint produces eight.
Same shortfall for knowledge query, `/eval/latest`, and patient-view.

**Applied:** spec §4–§7 now enumerate every response shape each endpoint can
actually produce, derived by reading the handlers rather than recalling them. A
state with no UI is a blank screen in a demo.

### R8 — Eligibility staleness has no timestamp to render **[P1-11 · ACCEPTED]**
`RVB-W3-U2` requires *"Active — as of 9:02AM"*. `POST /agent/eligibility`
(`app.py:477`) returns `stale` but no timestamp. The direct `/eligibility`
endpoint has `checked_at`; the portal has no route to it.

**Applied:** the agent response gains `checked_at_display`, which
`_lookup_eligibility` **already computes** and then discards. One line, and it
keeps the chip reading from one source rather than the UI joining two endpoints.

### R9 — The intake form has never worked against this backend **[P1-12 · ACCEPTED]**
`frontend/app/intake/page.tsx:77` sends `demographics.first_name` /
`last_name` and boolean consents. `services/intake-service/schemas.py:7` requires
`demographics.name` (with a not-blank validator) and `consents: list[str]`.

**Pre-existing — not introduced by this engagement** — but `RVB-W3-U6` ("intake
shows pending") sits directly on it, and would otherwise have passed only against
mocks.

**Applied:** fixed in `#19` where the requirement lives, and recorded as an
inherited defect rather than quietly repaired.

### R10 — `usePrincipal()` needs a loading state **[P1-14 · ACCEPTED]**
`localStorage` is `null` server-side (`session.ts:12`) and `AppShell` already
hydrates in a `useEffect` (`AppShell.tsx:81`). Reading storage during render
causes hydration drift; defaulting to staff flashes staff navigation to a patient.

**Applied:** the hook returns `{ status: "loading" | "ready", principal }` and
callers render a skeleton until ready. Matches the existing pattern rather than
introducing a second one.

### R11 — `CoverageChip` cannot reuse `StatusBadge` unchanged **[P2-16 · ACCEPTED]**
`StatusBadge.tsx:16` maps `pending` → **warn**; the spec needs `pending` → **info**
for coverage. Changing the shared map would alter unrelated badges.
**Applied:** `CoverageChip` is its own component using the same `rb-badge` classes
and `--rb-*` tokens. Still design-system-compliant (`RVB-U-01`); not a
`StatusBadge` wrapper.

### R12 — ADR 0012 contradicts its own spec **[P2-17 · ACCEPTED]**
The ADR says the hook decides *"exactly three things"*; the spec also routes
`can_ingest` visibility through it.
**Applied:** ADR widened to four, honestly, rather than pretending the spec is the
outlier. `can_ingest` was always going to live there.

### R13 — Test setup underspecified for Next 15 / React 19 **[P2-18 · ACCEPTED]**
No `test` script exists; Vitest needs jsdom, a React 19-compatible Testing
Library, and `next/navigation` mocks (`AppShell` calls `usePathname`/`useRouter`).
**Applied:** `#17`'s scope now names the exact dev dependencies and the
`next/navigation` mock as a deliverable, not an implementation detail.

### R14 — `/api/ai/*` route wrappers are scope, not scaffolding **[P1-13 · ACCEPTED]**
**Applied:** each week's section lists its route files explicitly.

---

## Accepted with modification

### R15 — Acceptance criteria overclaim **[P1-15 · 3 of 4 ACCEPTED, 1 MODIFIED]**

**Accepted as stated:**
- `RVB-W4-U5` — a component test cannot prove approve/deny resumes a paused LangGraph run. Moot now that `/approvals` is cut; the resume path is asserted by an **API test** against the real graph.
- `RVB-W1-U3` — cannot prove the submit control is disabled ahead of a retention refusal without a reachable disabled state. Fixed by having the panel derive it from `GET /healthz`'s `retention` block, which already exists.
- `RVB-W3-U5` — must assert a latency budget **and** `eligibility.status === "pending"`, or it does not prove decoupling. Both added.

**Modified:** codex read `RVB-W4-U8` as claiming to prove IDOR protection. It does
not — it is typed `D` and is about **correcting two stale comments** that still
claim the backend performs no ownership check. The protection itself is proven by
`test_har_walk_is_now_denied` in the backend suite.

The underlying concern is fair, though: a grep sitting next to IDOR requirements
invites the misreading. **Applied:** the requirement now carries an explicit
cross-reference to the test that does the proving.

---

## Net effect

| Change | Impact |
|---|---|
| `load_demographics` gains an audience parameter | The UI-D4 decision becomes real. Hiding it client-side would have been theatre. |
| Spec's principal fallback corrected to match `scope.py` | Removes a described privilege escalation before anyone built to it. |
| Gateway preserves upstream status | Every UI error state becomes distinguishable from success. |
| Staff-only reason codes cut | The field never existed, and adding it would have disclosed clinical strings to patients. |
| `/approvals` cut | No backend contract to build against. W4 returns to a plausible size. |
| Agent response gains `checked_at_display` | The staleness requirement becomes satisfiable from one endpoint. |
| Intake payload fixed | An inherited defect that `RVB-W3-U6` sat on. |
| Every endpoint's states enumerated from source | The spec stops describing a backend from memory. |

**Four small backend changes now sit inside UI PRs** — the audience parameter,
gateway status preservation, the agent timestamp field, and the intake payload.
Each is a precondition for a UI state being renderable or correct, and each is
named as backend work in its PR rather than smuggled in under a frontend heading.
