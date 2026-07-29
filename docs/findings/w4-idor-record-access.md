# Finding W4-1 — Any logged-in user could read any patient's chart

- **Debt ref:** D11, twist #9 · **Requirement:** `RVB-W4-06`, `RVB-W4-07`, `RVB-W4-14`, `RVB-W4-15`
- **Severity:** **Critical**
- **Status:** **Fixed** on the route the exposure lives on (`adr/0011`). Staff-side minimum-necessary remains coarse (D7, W9).
- **Found during:** reading `docs/handover/portal.har` before building the patient view

---

## The reproduction, from your own capture

`docs/handover/portal.har` records a browser session. Two requests:

```
GET https://portal.riverbend.example.com/api/patients/1042/records  → 200
GET https://portal.riverbend.example.com/api/patients/1043/records  → 200
```

One session. Two different people's charts. **Chart 1042 is Maria Gonzalez.
Chart 1043 is James O'Brien.** They are not related, and the session belonged to
one of them.

Patient ids are `SERIAL PRIMARY KEY` — sequential integers. `1042`, `1043`,
`1044`. Anyone with a valid login could walk the entire patient table by
incrementing a number in the address bar.

## Why the code allowed it — and why it was not a missing `if`

The obvious reading is "someone forgot an ownership check." That is the symptom.

`services/gateway/app.py` guarded the route with `require_session`, which answers
**"is someone logged in?"**. It could never answer **"is this the right
someone?"** — because the session had nothing to check against:

```python
def create_session(username: str, role: str) -> str:      # before
    _redis().hset(f"session:{token}",
                  mapping={"username": username, "role": role})
```

`users` had `username`, `password_hash`, `full_name`, `role`, `is_active`. **No
reference to a patient.** And every account carried the single `staff` role.

So the cause is deeper than a forgotten clause: **the system never modelled who a
login belongs to.** "Let patients see their *own* records" — the Week-4 ask — was
not expressible in the schema. `require_session` could not have been fixed by
adding a comparison, because there was no second operand.

That is why this finding ships with a migration rather than a one-line patch.

## Why it matters

**Blast radius is the whole patient table.** Not one chart, not one clinic —
every patient Riverbend has ever registered, readable by anyone who can log in.

**No credential compromise required.** A patient with a legitimate portal account
is already inside. The attack is *editing a URL*.

**It is undetectable today.** The `audit_logs` table is an ordinary mutable table
(D2) and cannot answer "who viewed patient X?" So we cannot tell you whether this
has been used. That is a distinct finding (W10) and it compounds this one: an
exploited IDOR would leave no trace to find.

**Regulatory:**
- **164.312(a)(1)** technical access control — the safeguard exists to ensure only authorized persons access ePHI. Authentication without authorization does not satisfy it.
- **164.502(b)** minimum necessary — every user having access to every chart is the definition of a failure here.
- **164.402 / 164.404** — if this were exploited, the 60-day breach-notification clock would start on *discovery*, and there is currently no mechanism to make that discovery.

## The fix

**Three parts, and the first is the one that was missing.**

**1. Model the identity** (`db/migrations/009_user_patient_binding.sql`)

```sql
ALTER TABLE users ADD COLUMN patient_id INTEGER REFERENCES patients(id);
CREATE UNIQUE INDEX users_patient_id_uniq
    ON users (patient_id) WHERE patient_id IS NOT NULL;
```

Nullable — staff accounts legitimately have no patient identity. Unique where
present — two logins for one chart would make "who viewed this patient?"
unanswerable even after W10 lands.

**2. Carry it, server-derived** — `create_session` now reads `patient_id` from
the `users` row. A request **cannot** assert its own patient identity; that would
reintroduce the vulnerability through the front door, and there is a test for it.

**3. Resolve a scope before proxying anything** (`services/gateway/scope.py`)

| Principal | Scope |
|---|---|
| **Patient** | Own chart **plus any chart reachable by `SAME_AS`** |
| **Staff** | The patient explicitly in context |

The `SAME_AS` part matters: Maria Gonzalez is three charts (Week 2), and the
penicillin allergy is on the one she was not bound to. Authorizing only her own
chart id would have turned the Week-2 fragmentation into a Week-4 access denial
and hidden her allergy *from her*.

## Denials return 404, not 403

A `403` on a real patient id and a `404` on a nonexistent one is an **enumeration
oracle**: it confirms which ids exist, which is most of what the walk was after.
Both return the identical response, and a test asserts the bodies match.

## Verification

`tests/test_w4_idor_and_graph.py`:

- **`test_the_har_captured_a_cross_patient_walk`** — parses the HAR and confirms the evidence before asserting against it.
- **`test_har_walk_is_now_denied`** — same session, same sequence: `1042` → 200, `1043` → **404**.
- **`test_the_walk_never_reaches_records_service`** — denied means denied *before proxying*. `records-service` receives nothing.
- **`test_denial_is_not_an_enumeration_oracle`** — unauthorized-but-real and nonexistent are indistinguishable.
- **`test_the_patient_sees_all_three_of_her_own_fragments`** — the fix did not break her access to her own record.
- **`test_a_client_supplied_patient_id_is_ignored`**.

Plus `tests/test_w4_authorization.py::test_denied_request_loads_zero_rows` on the
assembler: a denied request loads **zero** rows. Not "loads then filters" — a
filtered breach is still a breach, because the rows were already in memory, in
logs, and one refactor away from a model's context.

**We did not keep a permanently-failing test that reproduces the vulnerability.**
It would either break CI or, if it passed, enshrine the bug. The evidence lives
here; the tests assert the fix.

## What this does NOT fix — please do not oversell it internally

- **Staff scope is coarse.** With one `staff` role for everyone, a real minimum-necessary determination is impossible. The gate narrows *which patient*, not *which staff role*. **D7 is still open** and it is Week 9's work. Describing this as least-privilege would be the same self-assertion we flagged in the README.
- **Sessions still never expire** (D10, 164.312(a)(2)(iii)). Worth noting a wrinkle: this change should invalidate existing sessions, because tokens issued before it carry no `patient_id` and resolve as staff principals. That is the safe direction, but it is awkward *precisely because* sessions never expire.
- **No MFA** (W9).
- **The access trail still cannot answer "who viewed this patient?"** (D2/D14, W10).

## Recommended next steps

1. **Deploy with a session flush.** Old tokens resolve as staff.
2. **Ask whether this has been exploited.** We cannot answer it from the logs, and that inability is itself the W10 finding. Worth a decision about whether to treat it as a risk-assessment input now rather than waiting.
3. **W9 splits the `staff` role** so the same gate can enforce treatment vs billing vs ROI scopes.
