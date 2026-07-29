# ADR 0011 — Session → patient identity binding (W4)

- **Status:** Accepted
- **Date:** 2026-07-29
- **Origin:** codex:rescue finding R2 — see `docs/specs/codex-rescue-adjustments.md`
- **Blocks:** ADR 0009 (the authorization boundary cannot be built without this)
- **Debt:** D11 (IDOR), and it touches D7 (role bloat) without resolving it
- **Requirement:** `RVB-W4-03`, `RVB-W4-14`

## Context

ADR 0009 specifies a deterministic authorization gate that converts a session into
an `AuthorizedScope` before any retriever runs. The W4 acceptance tests were
written as *"a session for patient 1042 requesting 1043 is denied."*

**That is not implementable against the current codebase.** Reading it:

- `services/gateway/security.py` — `create_session(username, role)` stores exactly
  those two fields in Redis under `session:<token>`.
- `services/gateway/models.py` / `db/schema.sql` — `users` has
  `(id, username, password_hash, full_name, role, is_active, last_login_at, created_at)`.
  There is **no patient reference**.
- `config/roles.yaml` — every account is `staff`.

So a session proves *"someone logged in"* and nothing else. There is no patient
identity to authorize against, which means the IDOR is not merely an omitted check
— **the information required to write the check does not exist.** That is why
`require_session` could never have been fixed by adding a comparison: there was
nothing to compare.

This is the real shape of debt D11, and it is worth saying to the client in exactly
these terms: the missing ownership check is a symptom. The cause is that the system
never modelled *who a login belongs to*.

The client's Week-4 ask makes the requirement explicit: *"Let patients see their
**own** labs and visit summaries."* "Own" is not expressible today.

## Decision

Add the minimum identity binding that makes ownership expressible, and no more.

### 1. Schema — one nullable foreign key

```sql
-- db/migrations/009_user_patient_binding.sql
ALTER TABLE users ADD COLUMN patient_id INTEGER REFERENCES patients(id);
CREATE UNIQUE INDEX users_patient_id_uniq ON users(patient_id) WHERE patient_id IS NOT NULL;
```

- **Nullable on purpose.** Staff accounts have no patient identity; that is a
  legitimate state, not missing data.
- **Unique when present.** Two logins for one patient row would make the
  accounting-of-disclosures question (W10) unanswerable.

Seed data gains a small number of patient-portal accounts, including one bound to
each of Maria Gonzalez's three fragments — which is how the W2 fragmentation
finding becomes visible from the patient side too.

### 2. Session carries the binding

`create_session(username, role, patient_id)` writes `patient_id` into the Redis
session alongside username and role. `/me` returns it so the portal can render the
right landing view without inferring policy client-side.

**Server-derived, never client-supplied.** The value comes from the `users` row at
login. A request cannot assert its own patient identity — that would reintroduce
the vulnerability through the front door.

### 3. The principal model

Two principal kinds, resolved from the session:

| Principal | Condition | `AuthorizedScope.patient_ids` |
|---|---|---|
| **Patient** | `patient_id IS NOT NULL` | Own id, **plus any id reachable by `SAME_AS`** (ADR 0010) so a fragmented human sees their whole record |
| **Staff** | `patient_id IS NULL` and role is a staff role | The patient explicitly in context for the request, subject to a treatment-relationship check |

**The staff rule is deliberately coarse this week, and we say so.** With one
`staff` role for everyone, a real minimum-necessary determination is impossible —
that is debt **D7** and it is W9's work. What W4 delivers is the *mechanism* and
the patient-side enforcement; the staff-side policy plugs into the same gate once
roles exist. Overselling that as least-privilege would be the same self-assertion
the README makes about encryption.

### 4. The gateway enforces on the existing route

`GET /patients/{patient_id}/records` and `GET /patients/{patient_id}` now resolve
`AuthorizedScope` and reject out-of-scope ids **before proxying**. This is the
route the HAR walk actually used, and it is where the client is exposed.

Rejection is `404`, not `403`. A `403` on a valid id and a `404` on an invalid one
is an enumeration oracle: it confirms which patient ids exist. Both cases return
the same response.

### 5. What this does not do

- It does **not** expire sessions or add automatic logoff (**D10**, 164.312(a)(2)(iii)) — named, W9/W10.
- It does **not** add MFA — named, W9.
- It does **not** split the `staff` role (**D7**, 164.502(b)) — named, W9.
- It does **not** make disclosure accounting possible (**D12/D14**) — named, W9/W10.

Each is listed so the client sees the boundary of what a closed IDOR buys them.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Pass `patient_id` from the client and validate it** | This is the current bug with extra steps. Any value the caller supplies is a value the caller can change. |
| **Derive ownership from a join at query time in `records-service`** | Puts the check back in the place that already forgot it once, and repeats it in every future query. ADR 0010's reachability model exists precisely to stop that. |
| **Full RBAC + patient portal identity provider now** | Correct destination, wrong week. It is a service-sized project (W9) and W4 would ship nothing. |
| **Scope by MRN instead of `patient_id`** | `db/schema.sql` annotates `mrn` as explicitly *not* a match key, and the W2 finding is that identity is forked. Binding to a non-unique field would make the authorization gate inherit the fragmentation bug. |

## Consequences

**Good.**
- "Own record" becomes expressible, so the client's Week-4 ask becomes buildable.
- D11 is closed on the route the client is actually exposed on, not only on new code.
- The `SAME_AS` traversal means a fragmented patient sees their complete record through the portal — the W2 finding and the W4 fix reinforce each other.
- The `404`-not-`403` choice removes an enumeration oracle that a naive fix would have introduced.

**Costs accepted.**
- A schema migration in what was scoped as a prototype week. Small — one column, one index, seed rows — and unavoidable: without it, W4 has no deliverable.
- Existing sessions issued before the change lack `patient_id`. They resolve as staff principals, which is the safe direction (staff scope is checked, not assumed) but means a deploy should invalidate sessions. Noted for the runbook — and it is awkward precisely *because* sessions never expire, which is itself D10.
- Staff authorization remains coarse. Stated plainly rather than dressed up.
