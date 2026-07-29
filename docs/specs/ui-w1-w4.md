# UI Spec — Weeks 1–4, the client-visible half

- **Phase:** PRs `#17`–`#20`, sequenced, each cut from `feat/riverbend-w1-w4` after the previous merges
- **Debate:** `docs/design-debate-w1-w4.md` Part II (UI-D1 … UI-D8)
- **ADRs:** `adr/0012` (principal model), `adr/0013` (test strategy)
- **Requirement IDs:** `RVB-W<n>-U<k>`
- **Status:** specified

---

## 0. Why this phase exists

PRs #13–#16 shipped four capabilities as gateway endpoints and **zero lines of
`frontend/`**. Each PR body claimed "what the client can see" and then described
an API. Those four claims have been corrected in place.

This phase closes the gap. It also closes codex finding **R7**, which said
client-facing surfaces were not acceptance-tested — the four tests written in
response go through the *gateway*, not a browser, and were allowed to stand in
for something they do not prove.

**One correction, carried in `#17`, is a defect rather than a gap:**
`frontend/app/page.tsx:24` hard-codes `DEFAULT_PATIENT_ID = "1042"`. After #16's
authorization gate, the seeded account `james.obrien` (chart 1043) fetches 1042,
receives a 404, and lands on an empty dashboard. `maria.gonzalez` works only
because her chart happens to be the hard-coded one.

---

## 1. Re-scoped requirements from the backend phase

| ID | Was | Now |
|---|---|---|
| `RVB-W1-15` | "End-to-end surface test" — `POST /ai/summary` through the gateway | **Renamed** to an API contract test. The end-to-end requirement becomes `RVB-W1-U5`, a browser journey. |
| `RVB-W2-14` | same shape | → `RVB-W2-U5` |
| `RVB-W3-14` | same shape | → `RVB-W3-U5` |
| `RVB-W4-16` | same shape | → `RVB-W4-U6` |

The existing tests are not deleted. They are renamed to what they actually are —
`test_api_*` rather than `test_e2e_*` — because a test whose name overstates its
reach is how the gap survived four reviews.

---

## 2. Global constraints for this phase

| ID | Constraint | Source |
|---|---|---|
| `RVB-U-01` | Extend the existing design system. New markup uses `--rb-*` tokens and `rb-*` classes; new components compose `Card` / `StatusBadge` / `icons`. **No CSS framework, no component library, no second design language.** | UI-D5, ADR 0012 |
| `RVB-U-02` | One shell, one `usePrincipal()` hook. No second layout, no App Router route-group split. | UI-D2 |
| `RVB-U-03` | The gateway remains the sole authority on permission. UI capability flags (`can_ingest`, principal kind) **show and hide**; they never decide. | ADR 0012 |
| `RVB-U-04` | Vitest covers **states**; Playwright covers **journeys**. Neither is contorted to do the other's job. | UI-D7, ADR 0013 |
| `RVB-U-05` | Playwright stays out of the default CI job. **Not** claimed as equivalent to the `--live` gate — that one protects money, this one protects time. | UI-D8 |
| `RVB-U-06` | Every default-gate test runs with no AWS credential and no spend. | carried from `RVB-X-04` |
| `RVB-U-07` | No new runtime dependency in `frontend/package.json`. Test tooling is `devDependencies` only. | UI-D5 |
| `RVB-U-08` | **Deploying this phase requires invalidating existing sessions.** A token issued before #16 carries no `patient_id`, so the *gateway* resolves it as a **staff** principal — a patient holding an old token retains staff scope at the backend. That is not a UI concern and cannot be fixed in the UI. | codex R-P1-3; `docs/findings/w4-no-automatic-logoff.md` |
| `RVB-U-09` | **The gateway must preserve upstream HTTP status.** `_post`/`_get` (`services/gateway/app.py:383`) return `r.json()` and discard `r.status_code`, so a downstream 422 or 503 reaches the browser as **HTTP 200 with an error body**. Fixed in `#17`, with tests. | codex R-P1-8 |

> **Scope note on `RVB-U-09`:** gateway-**raised** exceptions
> (`require_patient_access` → 404, `require_session` → 401,
> `authz.require_ingest` → 403) are raised *before* the proxy call and already
> carry their status. Only **downstream** service errors flatten. Verified — so
> the IDOR 404 the whole of W4 rests on is unaffected, but every service-level
> failure currently looks like success to the UI.

### Backend changes that live inside UI PRs

Four, each a precondition for a UI state being renderable or correct. Named here
rather than smuggled in under a frontend heading:

| Change | PR | Why it cannot wait |
|---|---|---|
| Gateway preserves upstream status (`RVB-U-09`) | `#17` | Every subsequent UI error state depends on telling failure from success |
| `/ai/health` route exposing the retention block | `#17` | `RVB-W1-U3` must disable submit *before* a request |
| `checked_at_display` on the agent response | `#19` | `RVB-W3-U2` has no timestamp to render otherwise |
| `load_demographics(ids, audience)` | `#20` | UI-D4 cannot be honoured client-side |

`RVB-W3-U7` (the intake payload) is a **frontend** fix to an inherited defect, not
a backend change.

---

## 3. Shared foundation (lands in `#17`)

### `usePrincipal()` — `frontend/app/lib/principal.ts`

```ts
type Principal =
  | { kind: "patient"; patientId: number | null; canIngest: boolean }
  | { kind: "staff";   patientId: null;          canIngest: boolean };

type PrincipalState =
  | { status: "loading" }
  | { status: "ready"; principal: Principal };
```

Read from the session (`PortalUser.patient_id`, added to `app/lib/types.ts`) and
refreshed from `GET /api/me`, which already returns `patient_id`, `scope` and
`can_ingest` — added in #16 and #14 respectively and currently unused.

**The hook decides four things and nothing else** (UI-D2, ADR 0012): which nav
items render, what the landing page shows, whether a patient picker appears, and
whether the knowledge-ingest control is visible.

### The fallback must mirror `scope.py` exactly

An earlier draft of this spec said *"malformed or absent `patient_id` resolves to
staff"*. **That was wrong, and it described a privilege escalation.**
`services/gateway/scope.py:86` actually does this:

| Session `patient_id` | Backend resolves to | UI must resolve to |
|---|---|---|
| absent / `""` / `"None"` | **staff**, `open_to_context=true` | staff |
| present but unparseable | **patient with an empty id set** — permitted nothing | patient, `patientId: null` |
| present and valid | patient, own id + `SAME_AS` fragments | patient with that id |

> ```python
> except (TypeError, ValueError):
>     # A malformed session is not a licence to see everything.
>     return AuthorizedScope(principal=PRINCIPAL_PATIENT, username=username)
> ```

The backend **fails closed**. A UI built to the old wording would have shown staff
navigation to a principal the gateway treats as a patient who can see nothing.

### Loading is a real state

`localStorage` is `null` server-side (`app/lib/session.ts:12`) and `AppShell`
already hydrates in a `useEffect` (`AppShell.tsx:81`). Reading storage during
render causes hydration drift; defaulting to staff **flashes staff navigation to
a patient**. Callers render a skeleton until `status === "ready"`.

> Because the gateway re-derives the scope server-side on every request, a wrong
> guess in the UI is a cosmetic bug rather than a security one — **provided the
> session itself is correct.** That proviso is not free: a token issued before
> #16 carries no `patient_id` and the *gateway* resolves it as staff. See
> ADR 0012 §2 and `RVB-U-08`.

---

## 4. `#17` · Week 1 — the intake summary panel

### Requirements

| ID | Type | Requirement | Acceptance |
|---|---|---|---|
| `RVB-W1-U1` | F | `SummaryPanel` on `/intake`: instruction text in, patient-friendly summary out, through `POST /api/ai/summary`. | Component test: given a grounded response, the summary renders. |
| `RVB-W1-U2` | F | **Withheld state.** When `grounded=false`, the panel renders the safe message and a review flag — **never** the raw model text. | Component test asserts the raw text is absent from the DOM. |
| `RVB-W1-U3` | F | **Service-disabled state.** The panel reads `GET /api/ai/health`'s `retention.ok` on mount; when false it says the feature is unavailable pending a configuration check and **disables submit before any request is made**. | Component test for both `retention.ok` values. The health block already exists on `/healthz`; a gateway route is added to reach it. |
| `RVB-W1-U4` | C | **Dashboard defect fixed.** `app/page.tsx` reads the patient id from `usePrincipal()`; staff get a picker. `james.obrien` no longer lands on an empty dashboard. | Component test for both principals. |
| `RVB-W1-U5` | T | **Browser journey:** log in → `/intake` → enter instructions → a grounded summary appears. | Playwright, against `make up`. |
| `RVB-W1-U6` | T | Test infrastructure: Vitest + Testing Library in the default gate; Playwright configured and excluded from it. | `npm test` green in CI; `npm run test:e2e` not run there. |

### Every state this endpoint can actually produce

Enumerated by reading `services/ai-orchestrator/app.py` rather than recalling it.
An earlier draft named three of these eight; a state with no UI is a blank screen
in a demo.

| # | Trigger | Response shape | What the user sees |
|---|---|---|---|
| 1 | Grounded summary | `grounded: true` | The summary, and a quiet note that it was checked against the source |
| 2 | **Withheld** | `grounded: false`, `needs_review: true` | The safe message, and *"This summary was held back for review because it contained information not present in the instructions."* |
| 3 | Retention refused | `usage.refused == "retention_policy"` | Unavailable pending a configuration check. Also caught ahead of time by `RVB-W1-U3`. |
| 4 | Too short | `usage.refused == "source_too_short"` | Inline hint on the field — not an error banner |
| 5 | Over budget | `usage.refused == "budget"` | *"That text is too long to summarise."* Actionable, not a stack trace. |
| 6 | Guardrail blocked | `usage.refused == "guardrail_blocked"` | Same surface as **2** — the user does not need to know which layer held it |
| 7 | Model unavailable | `usage.refused == "model_unavailable"` | *"Temporarily unavailable, please try again shortly."* Retry offered. |
| 8 | Validation / transport | HTTP 422, or a `{"error": …}` body from the gateway proxy | Generic failure with retry. **See `RVB-U-09`** — until the gateway preserves upstream status, some of these arrive as HTTP 200. |

**Why the withheld message names a reason at all:** without it a user retries.
Retries cost money and produce the same result. Telling them it was *held*, not
*failed*, ends the loop.

**Reason codes are NOT shown — the field does not exist.** An earlier draft had
staff seeing `invented_medication:metformin`. `SummaryResponse` carries only
`request_id, summary, grounded, needs_review, model, stubbed, usage`; the
guardrail reasons go to the **audit event** and nowhere else. Adding them to the
response would return clinical strings to patients as well, and hiding them in
React would be exactly the theatre UI-D4 rejects. They stay in the audit log,
which is where an operator should be reading them anyway.

**Explicitly not in `#17`:** a review queue for flagged summaries. `needs_review`
currently routes nowhere. Named here; it belongs with W7's observability work.

---

## 5. `#18` · Week 2 — knowledge search and the quality dashboard

### Requirements

| ID | Type | Requirement | Acceptance |
|---|---|---|---|
| `RVB-W2-U1` | F | `/knowledge` — question in, answer out with citations resolving to named sources. | Component test. |
| `RVB-W2-U2` | F | **A refusal is a first-class answer, not an error.** No red styling, no retry prompt. | Component test asserts the refusal renders in the answer slot, not an alert — and asserts against **`rag_graph.REFUSAL`**, not a transcription of it. An earlier draft of this spec quoted the string slightly wrong (*"I don't have that in the knowledge base"* vs the actual *"…that information in the…"*), which would have baked a false contract into the test. |
| `RVB-W2-U3` | F | `/knowledge/quality` — the eval report as a screen, **leading with recall and fragment coverage side by side, same size, adjacent**. | Component test asserts both figures render in one group. |
| `RVB-W2-U4` | F | Below the headline: the identity-split table (the three charts), then the clinically-incomplete case, then everything else collapsed behind "full report". | Component test. |
| `RVB-W2-U5` | T | **Browser journey:** ask a chart-shaped clinical question, get cited results; the quality screen shows both figures. | Playwright. |
| `RVB-W2-U6` | C | The ingest control renders only when `/me` reports `can_ingest`. The gateway still enforces; this is visibility. | Component test for both cases. |

### Every state these endpoints produce

| Endpoint | States |
|---|---|
| `POST /ai/knowledge/query` | answered with citations · refused below the relevance floor · refused because generation failed (`reason` starts `generation_failed:`) · refused for a missing scope (`ScopeRequired`) · transport error |
| `GET /ai/knowledge/eval/latest` | a run · **`{metrics: null, note: "no eval has been run in this process"}`** — the first-load state, which needs an empty view rather than a crash |
| `POST /ai/knowledge/eval` | a full run · transport error |

The `metrics: null` case is the one a demo hits first: the quality screen is
opened before any eval has been run in that process. It renders a "run the
evaluation" prompt, not an error.

### Why the layout is prescribed

The finding is a **juxtaposition**, not a metric. `recall 1.0` beside
`fragment coverage 0.556` *is* the argument; separated or differently sized, it
becomes a dashboard and the point is lost (UI-D6). This constraint is in the spec
so a later "let's tidy the metrics grid" cannot quietly remove it.

---

## 6. `#19` · Week 3 — the eligibility desk and the coverage chip

### Requirements

| ID | Type | Requirement | Acceptance |
|---|---|---|---|
| `RVB-W3-U1` | F | `CoverageChip` — an `rb-badge` variant for `active` / `inactive` / `pending` / `unknown`. | Component test, one case per status. |
| `RVB-W3-U2` | F | **Staleness is impossible to miss.** A stale result renders the status *and* its original timestamp: *"Active — as of 9:02AM"*. **Backend change required:** `POST /agent/eligibility` returns `stale` but no timestamp. `_lookup_eligibility` already computes `checked_at_display` and then discards it — the agent response gains that field. One line, and it keeps the chip reading from a single endpoint rather than joining two. | Component test asserts a timestamp is present whenever `stale` is true, and absent when it is not. |
| `RVB-W3-U3` | F | `unknown` never renders as "not covered". Distinct copy: *"Could not verify — proceed and mark unverified."* | Component test asserts `unknown` and `inactive` render differently. |
| `RVB-W3-U4` | F | `/eligibility` — the front-desk assistant, visit-scoped. When a reply was **overridden** for contradicting the tool, the UI shows the tool's result was used. | Component test. |
| `RVB-W3-U5` | T | **Browser journey:** with `eligibility-service` stopped, check coverage → the chip reads stale with a timestamp, and registration still completes. **Must assert a latency budget AND `eligibility.status === "pending"`** — without both it does not prove decoupling, only that a page rendered. | Playwright: `POST /intake` completes < 2s with the service down, response carries `status: "pending"`. |
| `RVB-W3-U6` | F | `/intake` renders `pending` on submit — the visible half of the W3 decoupling. | Component test. |
| `RVB-W3-U7` | D | **Inherited defect, fixed here.** `app/intake/page.tsx:77` sends `demographics.first_name` / `last_name` and boolean consents; `services/intake-service/schemas.py:7` requires `demographics.name` (not-blank validated) and `consents: list[str]`. **The intake form has never successfully submitted against this backend.** Not introduced by this engagement, but `RVB-W3-U6` sits directly on it and would otherwise pass only against mocks. | The journey in `RVB-W3-U5` submits the real form against the real service. |

### Status → token mapping

| Status | Token | Copy |
|---|---|---|
| `active` | `--rb-ok` | Active |
| `inactive` | `--rb-bad` | Not active |
| `pending` | `--rb-info` | Checking… |
| `unknown` | `--rb-warn` | Could not verify |
| any + `stale` | keeps its colour, gains the timestamp | *"— as of {time}"* |

`unknown` is **warn**, not **bad**. Rendering "we could not check" in the same
red as "not covered" is the visual form of the conflation the backend spent Week 3
avoiding.

---

## 7. `#20` · Week 4 — the patient portal and the approvals queue

### Requirements

| ID | Type | Requirement | Acceptance |
|---|---|---|---|
| `RVB-W4-U1` | F | Principal-aware nav: patients do not see Intake or Release of Information. | Component test for both principals. |
| `RVB-W4-U2` | F | Patient landing shows their assembled record across the four domains. | Component test. |
| `RVB-W4-U3` | C | **The fragmentation note must be fixed SERVER-SIDE.** `load_demographics` gains an `audience` parameter: a patient gets the neutral count, staff keep the chart-level detail. See below — the backend currently violates UI-D4 and the UI cannot fix it. | Python test: `load_demographics(ids, audience="patient")` contains no chart IDs; `audience="staff"` does. Plus a component test that the patient view renders the neutral note. |
| `RVB-W4-U4` | F | A domain that failed renders as unavailable and named — never silently omitted. Three statuses exist, not two: `ok`, **`degraded`** (stale coverage), `unavailable`. | Component test, one case per status. |
| `RVB-W4-U5` | F | The assembled view renders every `released=false` reason distinctly: **denied** (not authorised), **withheld pending approval** (`deny_reason: "withheld_pending_approval"`), and **synthesis failed**. A single "something went wrong" for all three is wrong — one is a permission answer, one is a pending human, one is a fault. | Component test, one case each. |
| `RVB-W4-U6` | T | **Browser journey:** log in as `maria.gonzalez` → her record shows all three charts including the penicillin allergy → navigating to chart 1043 shows "not found". | Playwright. |
| `RVB-W4-U7` | F | The free-text patient-ID input is replaced by a **name-search picker** over the existing `/patients?q=` route. | Component test. |
| `RVB-W4-U8` | D | The two stale comments claiming the backend performs no ownership check (`records/page.tsx:16`, `api/records/route.ts:6`) are corrected. **This is a documentation correction and proves nothing about the protection itself** — that is proven by `tests/test_w4_idor_and_graph.py::test_har_walk_is_now_denied`. Cross-referenced so the grep is not mistaken for the control. | Grep assertion. |

### `/approvals` is cut — there is no contract to build against

An earlier draft specified a HITL approvals queue. **The backend has no way to
list paused runs.** The gateway exposes `POST /ai/patient-view/{id}/resume` and
nothing that enumerates what is waiting. A queue screen cannot be built against a
resume-only endpoint, and inventing a list endpoint inside a UI PR would be
backend scope arriving under a frontend heading.

**Deferred, with its precondition named:** a `GET /ai/patient-view/pending`
returning `{thread_id, patient_id, requested_at, reason}` for interrupted runs.
The day that exists, the screen is straightforward. Resume itself stays reachable
in `#20` as an API route and a component state on the assembled view, and the
resume path is asserted by an **API test against the real graph** — a component
test cannot prove a paused LangGraph run resumes.

This cut also returns `#20` to a plausible size (codex P2-19).

### The wording that is not ours to write — and the backend already broke it

`RVB-W4-U3` is a clinical communication decision wearing a UI costume (UI-D4).
We show *"this record brings together 3 charts"* — a fact about our system. We do
**not** show *"chart 1042 is missing your penicillin allergy"* — an interpretation
a patient may not have context for, delivered by a web page with no clinician
present.

**The backend already violates that decision.**
`services/ai-orchestrator/patient_view_loaders.py:74` returns, to a patient:

> This record is spread across 3 charts **(1042, 1330, 1588)**, which appear to
> be the same person. They have been shown together.

and line 69 builds the synthesis context as
`f"{p.name} (chart {p.id}), date of birth {p.dob}"` for every fragment — so the
chart IDs reach the model prompt and the patient-facing summary by a second route.
Fixing only the note would have left the same content arriving anyway.

**This is not a cross-patient leak.** All three charts belong to the same person;
it is her own date of birth three times. It is a *clinical-communication* defect,
not a breach, and calling it a breach would be the overclaiming we criticised in
the README.

**It must be fixed server-side.** If the API returns it to a patient, it was
disclosed — hiding it in React is theatre, which is the principle behind cutting
the staff-only reason codes in `#17` as well.

**The exact wording is flagged to the client as theirs to approve**, in the PR
body and in `docs/findings/w2-patient-fragmentation.md`. Picking it quietly would
be us making a clinical-communication call we are not qualified to make.

### The picker is not a security fix

Replacing the ID box removes an affordance that teaches ID-guessing. It is **UX
hardening**. The gate is the gate — re-adding an ID field would make the portal
uglier, not less secure. Stated in the PR so it cannot be oversold.

---

## 8. Verification

**Default gate, every PR:**
```bash
cd frontend && npm test && npm run build
pytest -m "not integration" -q          # 231 backend tests stay green
```

**Opt-in, every PR:**
```bash
make up
cd frontend && npm run test:e2e
```

**Regression watch:** `records/page.tsx` does `json.encounters ?? []`, so a 404
already renders as *"No records found for this patient."* — the correct surface
for the 404-not-403 choice. Easy to break while adding principal awareness. A test
pins it.

---

## 9. Definition of done for the phase

- [ ] Four journeys pass against a running stack, one per week
- [ ] Component tests cover every state named in §4–§7
- [ ] `RVB-W1-15` / `W2-14` / `W3-14` / `W4-16` renamed to API contract tests; the browser requirements replace them
- [ ] No new runtime dependency in `frontend/package.json`
- [ ] `DEMO.md` Path B is a browser walkthrough; `scripts/demo.py` demoted to a smoke aid
- [ ] The fragmentation wording is flagged to the client, not decided by us
- [ ] Each PR body answers: **"Can a person do this in a browser, and did we watch them do it?"**
