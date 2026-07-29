# Finding W4-3 — Sessions never expire

- **Debt ref:** D10 · **Requirement:** `RVB-W4-09`
- **Severity:** Medium–High (depends on physical environment)
- **Status:** Named. Not fixed — W9.
- **Found during:** adding the session→patient binding for the IDOR fix

---

## What we saw

`services/gateway/security.py`:

```python
def create_session(username, role, patient_id=None):
    token = uuid.uuid4().hex
    # NOTE: no expiry / TTL is set, so sessions never expire.
    _redis().hset(f"session:{token}", mapping=mapping)
```

No TTL on the Redis key. `auth.yaml` confirms the intent: `SESSION_TIMEOUT: never`.
A token, once issued, is valid forever. There is no automatic logoff, and there
is no MFA.

## Why it matters

**The realistic threat here is not a hacker.** It is a shared clinical
workstation at a nurses' station, logged in as whoever used it last, with a
browser tab open. Someone walks away. Someone else — a patient, a visitor, a
contractor — is standing in front of an authenticated session with access to
charts.

`45 CFR 164.312(a)(2)(iii)` names this specifically: *automatic logoff —
electronic procedures that terminate an electronic session after a predetermined
time of inactivity.* It is an addressable specification, which under
`164.306(d)(3)` means implement it, **or** document why it is not reasonable and
implement an equivalent alternative. Riverbend has done neither.

**A token in `localStorage` never expires either.** The portal stores it
client-side; a token lifted from a browser profile, a backup, or a shared machine
stays valid indefinitely. There is no rotation and no revocation short of the
explicit logout button.

## The awkward interaction with this week's fix

The IDOR fix binds a patient identity into the session. Tokens issued **before**
the change carry no `patient_id`, so they resolve as *staff* principals.

That is the safe direction — staff scope is checked rather than assumed — but it
means **a deploy should invalidate existing sessions**, and doing that is
awkward *precisely because* sessions never expire: there is no natural point at
which the old ones age out. Someone has to flush them.

We flag this as a deployment step in the finding rather than burying it in a
runbook line, because "old sessions keep the old behaviour" is exactly the sort
of thing that gets discovered later.

## Recommendation (W9)

1. **Set a TTL on the Redis session key** and refresh it on activity. Fifteen minutes of inactivity is the common clinical setting; the number is a policy decision, not an engineering one, and it should be made with the front desk rather than for them.
2. **Absolute lifetime as well as idle timeout** — an active tab should not keep a session alive for a week.
3. **Revocation on password change**, which currently does nothing to existing sessions.
4. **MFA** — separately tracked, and the 2025 Security Rule NPRM moves in that direction.

None of these are large. They are grouped into W9 because they belong with the
RBAC split, and because changing session semantics is a workflow change that
should be introduced once, not twice.
