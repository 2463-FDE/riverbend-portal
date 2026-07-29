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

---

## 3. Shared foundation (lands in `#17`)

### `usePrincipal()` — `frontend/app/lib/principal.ts`

```ts
type Principal =
  | { kind: "patient"; patientId: number; canIngest: boolean }
  | { kind: "staff";   patientId: null;   canIngest: boolean };
```

Read from the session (`PortalUser.patient_id`, added to `app/lib/types.ts`) and
refreshed from `GET /api/me`, which already returns `patient_id`, `scope` and
`can_ingest` — added in #16 and #14 respectively and currently unused.

**The hook decides three things and nothing else** (UI-D2): which nav items
render, what the landing page shows, and whether a patient picker appears.

> A malformed or absent `patient_id` resolves to **staff**, matching
> `services/gateway/scope.py`. The UI must never invent a patient identity — and
> because the gateway re-derives the scope server-side on every request, a wrong
> guess here is a cosmetic bug, not a security one. That asymmetry is deliberate.

---

## 4. `#17` · Week 1 — the intake summary panel

### Requirements

| ID | Type | Requirement | Acceptance |
|---|---|---|---|
| `RVB-W1-U1` | F | `SummaryPanel` on `/intake`: instruction text in, patient-friendly summary out, through `POST /api/ai/summary`. | Component test: given a grounded response, the summary renders. |
| `RVB-W1-U2` | F | **Withheld state.** When `grounded=false`, the panel renders the safe message and a review flag — **never** the raw model text. | Component test asserts the raw text is absent from the DOM. |
| `RVB-W1-U3` | F | **Service-disabled state.** When the retention preflight has refused, the panel says the feature is unavailable pending a configuration check, and the submit control is disabled. | Component test. |
| `RVB-W1-U4` | C | **Dashboard defect fixed.** `app/page.tsx` reads the patient id from `usePrincipal()`; staff get a picker. `james.obrien` no longer lands on an empty dashboard. | Component test for both principals. |
| `RVB-W1-U5` | T | **Browser journey:** log in → `/intake` → enter instructions → a grounded summary appears. | Playwright, against `make up`. |
| `RVB-W1-U6` | T | Test infrastructure: Vitest + Testing Library in the default gate; Playwright configured and excluded from it. | `npm test` green in CI; `npm run test:e2e` not run there. |

### The three states

| State | Trigger | What the user sees |
|---|---|---|
| Grounded | `grounded: true` | The summary, plus a quiet note that it was checked against the source |
| **Withheld** | `grounded: false`, `needs_review: true` | The safe message, and: *"This summary was held back for review because it contained information not present in the instructions."* **Staff principals additionally see the reason codes** (`invented_medication:metformin`). Patients do not. |
| Disabled | `usage.refused == "retention_policy"` | *"The summary feature is unavailable pending a data-retention configuration check."* Submit disabled. |

**Why the withheld message names a reason at all:** without it, a user retries.
Retries cost money and produce the same result. Telling them it was *held*, not
*failed*, ends the loop.

**Why reason codes are staff-only:** `invented_medication:metformin` is a clinical
string. Shown to a patient with no context it is alarming; shown to a staff member
it is actionable. Same split as UI-D4.

**Explicitly not in `#17`:** a review queue for flagged summaries. `needs_review`
currently routes nowhere. Named here; it belongs with W7's observability work.

---

## 5. `#18` · Week 2 — knowledge search and the quality dashboard

### Requirements

| ID | Type | Requirement | Acceptance |
|---|---|---|---|
| `RVB-W2-U1` | F | `/knowledge` — question in, answer out with citations resolving to named sources. | Component test. |
| `RVB-W2-U2` | F | **A refusal is a first-class answer, not an error.** No red styling, no retry prompt: *"I don't have that in the knowledge base."* | Component test asserts refusal renders in the answer slot, not an alert. |
| `RVB-W2-U3` | F | `/knowledge/quality` — the eval report as a screen, **leading with recall and fragment coverage side by side, same size, adjacent**. | Component test asserts both figures render in one group. |
| `RVB-W2-U4` | F | Below the headline: the identity-split table (the three charts), then the clinically-incomplete case, then everything else collapsed behind "full report". | Component test. |
| `RVB-W2-U5` | T | **Browser journey:** ask a chart-shaped clinical question, get cited results; the quality screen shows both figures. | Playwright. |
| `RVB-W2-U6` | C | The ingest control renders only when `/me` reports `can_ingest`. The gateway still enforces; this is visibility. | Component test for both cases. |

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
| `RVB-W3-U2` | F | **Staleness is impossible to miss.** A stale result renders the status *and* its original timestamp: *"Active — as of 9:02AM"*. | Component test asserts the timestamp is present whenever `stale` is true. |
| `RVB-W3-U3` | F | `unknown` never renders as "not covered". Distinct copy: *"Could not verify — proceed and mark unverified."* | Component test asserts `unknown` and `inactive` render differently. |
| `RVB-W3-U4` | F | `/eligibility` — the front-desk assistant, visit-scoped. When a reply was **overridden** for contradicting the tool, the UI shows the tool's result was used. | Component test. |
| `RVB-W3-U5` | T | **Browser journey:** with `eligibility-service` stopped, check coverage → the chip reads stale with a timestamp, and registration still completes. | Playwright. |
| `RVB-W3-U6` | F | `/intake` renders `pending` on submit — the visible half of the W3 decoupling. | Component test. |

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
| `RVB-W4-U3` | C | **The fragmentation note states the system fact only:** *"This record brings together N charts that appear to be the same person."* It does **not** tell the patient which chart is missing what. Discrepancy detail remains staff-only. | Component test asserts patient view contains the neutral note and **not** the per-chart discrepancy. |
| `RVB-W4-U4` | F | A domain that failed renders as unavailable and named — never silently omitted. | Component test. |
| `RVB-W4-U5` | F | `/approvals` — the HITL queue. Approve or deny resumes the paused run. | Component test. |
| `RVB-W4-U6` | T | **Browser journey:** log in as `maria.gonzalez` → her record shows all three charts including the penicillin allergy → navigating to chart 1043 shows "not found". | Playwright. |
| `RVB-W4-U7` | F | The free-text patient-ID input is replaced by a **name-search picker** over the existing `/patients?q=` route. | Component test. |
| `RVB-W4-U8` | D | The two stale comments claiming the backend performs no ownership check (`records/page.tsx`, `api/records/route.ts`) are corrected. | Grep assertion in the test suite. |

### The wording that is not ours to write

`RVB-W4-U3` is a clinical communication decision wearing a UI costume (UI-D4).
We show *"this record brings together 3 charts"* — a fact about our system. We do
**not** show *"chart 1042 is missing your penicillin allergy"* — an interpretation
a patient may not have context for, delivered by a web page with no clinician
present.

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
