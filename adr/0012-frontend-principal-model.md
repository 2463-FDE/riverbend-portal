# ADR 0012 — Frontend principal model: one shell, one hook

- **Status:** Accepted
- **Date:** 2026-07-29
- **Spec:** `docs/specs/ui-w1-w4.md` §3
- **Debate:** `docs/design-debate-w1-w4.md` UI-D2, UI-D3, UI-D4, UI-D5
- **Depends on:** ADR 0011 (session → patient identity binding)

## Context

ADR 0011 introduced a **patient principal** server-side: `users.patient_id`,
carried into the session, resolved into an `AuthorizedScope` at the gateway. The
portal knows nothing about it.

Three consequences, in ascending order of seriousness:

1. `GET /api/me` now returns `patient_id`, `scope` and `can_ingest`. Nothing consumes them.
2. `AppShell`'s nav is a static array. A patient sees *Intake* and *Release of Information* — internal front-desk workflow.
3. **`app/page.tsx:24` hard-codes `DEFAULT_PATIENT_ID = "1042"`.** The seeded account `james.obrien` (chart 1043) now fetches 1042, gets a 404, and lands on an empty dashboard. This is a regression we introduced in #16, not a pre-existing gap.

The Week-4 ask was *"let patients see their own labs in one place."* Until the
portal can tell a patient from a staff member, that sentence has no UI.

## Decision

### 1. One shell, one hook

```ts
type Principal =
  | { kind: "patient"; patientId: number; canIngest: boolean }
  | { kind: "staff";   patientId: null;   canIngest: boolean };
```

`usePrincipal()` reads the session and refreshes from `/api/me`. It decides
**exactly three things**:

- which nav items render,
- what the landing page shows,
- whether a patient picker appears.

Nothing else branches on principal. A second layout, a second shell, or an App
Router route-group split were all rejected: they double the surface permanently
in exchange for a difference that is three conditionals wide (UI-D2).

### 2. The UI shows and hides. It never decides.

Every capability flag — `can_ingest`, principal kind — is a **hint for rendering**.
The gateway re-derives the scope server-side on every request and is the only
authority.

This has a useful property worth stating, because it is what makes the client-side
model safe to keep simple: **a wrong guess in the UI is a cosmetic bug, not a
security one.** If `usePrincipal()` mistakenly returns staff for a patient, the
user sees nav items they should not — and every request they make is still
refused by the gate. The blast radius of a frontend bug is confusion.

Consequently, a malformed or absent `patient_id` resolves to **staff**, matching
`services/gateway/scope.py`. The UI never invents a patient identity.

### 3. The dashboard defect is corrected in the first UI PR, not the last

`#17` makes `app/page.tsx` read the principal's `patientId` and gives staff a
picker. The full principal-aware shell is `#20`.

Splitting it this way is deliberate (UI-D3): the defect is a regression we shipped,
and stacking three PRs on top of a knowingly-broken seeded account is the habit we
criticised the contractor for in `docs/findings/`.

### 4. What the patient is told about their own fragmentation

The assembled view states the **system fact** only:

> This record brings together 3 charts that appear to be the same person.

It does **not** say which chart is missing what. *"Chart 1042 does not list your
penicillin allergy"* is a clinical interpretation delivered by a web page to a
patient with no clinician present. The discrepancy detail remains visible to
**staff**, where someone qualified can act on it.

**The exact wording is flagged to the client as theirs to approve.** We are not
qualified to write patient-facing clinical copy, and quietly picking something
would be making that call without saying we made it (UI-D4).

### 5. The patient-ID input becomes a name-search picker

`records/page.tsx` currently has a free-text box for a sequential integer. The
backend now refuses unauthorised ids, so it is no longer a vulnerability — but it
is an affordance that teaches ID-guessing as a workflow. It is replaced with a
name search over the existing `GET /patients?q=`.

**This is UX hardening, not a control** (UI-D5). Re-adding an ID field would make
the portal uglier, not less secure. Saying otherwise would oversell a cosmetic
change as a security fix, which is the same category error as the README's
compliance claim.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Separate patient layout / route group** | Doubles nav, landing, empty states and error copy permanently, for a difference that is three conditionals wide. Surface added this way never comes back. |
| **Server components reading the session** | The session lives in `localStorage` (inherited, D10-adjacent). Moving it to an httpOnly cookie is the right long-term fix and a separate decision — bundling it here would make an auth change ride along inside a UI PR. |
| **Enforce permission client-side** | Would duplicate `scope.py` in TypeScript, giving two implementations to keep in agreement and inviting the belief that the client-side one matters. |
| **Show the patient the full discrepancy** | Clinical communication without a clinician. See §4. |

## Consequences

**Good.**
- "Let patients see their own record" becomes buildable, and the ADR 0011 backend work becomes visible.
- A frontend bug cannot become a data breach, because the UI has no authority to lose.
- The seeded demo accounts both work from `#17` onward.

**Costs accepted.**
- `usePrincipal()` is a fifth thing reading the session. Contained: one module, one type, three call sites.
- Staff nav is still one undifferentiated set — billing, front desk and clinicians see the same items, because the backend still has one `staff` role (**D7**, W9). The UI cannot be more granular than the role model behind it, and pretending otherwise would be theatre.
- The session remains in `localStorage` with no expiry (**D10**). Untouched here; named in `docs/findings/w4-no-automatic-logoff.md`.
