# Finding W3-1 — A payer outage stops patient registration

- **Debt ref:** D4, twist #7 · **Requirement:** `RVB-W3-09`, `RVB-W3-10`
- **Severity:** **High — availability and revenue**
- **Status:** **Fixed** for the eligibility path (`adr/0008`). The same shape remains in `interop-service` (W6).
- **Client tickets:** **RIV-088** and **RIV-141** — *these are the same defect*
- **Found during:** building the Week-3 eligibility assistant

---

## Two tickets, one defect

You have these open separately:

> **RIV-088** *(Medium)* — "When we hit Save on a new patient, the page just
> spins for about 4–5 seconds before it confirms. Doesn't fail, just feels slow.
> Happens every time."

> **RIV-141** *(High)* — "Tue 9:00–9:20am the **ENTIRE intake screen** froze —
> front desk couldn't register any patient, **not just eligibility**. Came back
> on its own around 9:20."

**They are the same defect at two severities.** Closing RIV-088 cosmetically
would leave RIV-141 live and waiting.

## The evidence, from three artifacts you gave us

| Artifact | What it says |
|---|---|
| `services/eligibility-service/check.py` | The payer call has **no timeout, no retry, no circuit breaker, no cache**. Its own docstring said so. |
| `services/intake-service/app.py` | Calls it **inline on the registration request thread**, behind a 4.2-second stand-in for the clearinghouse round trip. |
| `docs/handover/payer-status-page.md` | ACME Clearinghouse: eligibility endpoint degraded **Tuesday 09:02–09:21**, nineteen minutes. |
| Same file, latency series | `/intake` p95 flat ~600 ms all week, **one 20-minute spike past 30 s Tuesday morning** — overlapping that window exactly. |

## The causal chain

```
payer degrades 09:02
      │
      ▼
eligibility call has no timeout, so it does not fail — it WAITS
      │
      ▼
the call is inline on /intake, so a registration request thread waits with it
      │
      ▼
every registration in that window holds a thread open
      │
      ▼
front desk cannot register ANY patient, 09:02 – 09:21
```

**A third party's outage became Riverbend's outage — in a subsystem that has
nothing to do with insurance.** Nobody at the clinic could have prevented it, and
nobody at the clinic could have shortened it.

## Why it matters, in your terms

**Revenue and access.** For nineteen minutes, no patient could be registered.
Not "eligibility was unavailable" — *registration* was unavailable. Everyone in
the waiting room waited, and some of them left.

**It is not rare, it is scheduled.** Clearinghouses have incidents. The status
page exists because they expect to have them. The question was never *whether*
the payer would be down again, only whether it would take registration with it.

**Contingency planning — 45 CFR 164.308(a)(7).** The Security Rule expects
procedures for operating when systems are unavailable. There was no contingency
here because nobody had identified the dependency: the front desk understood
"eligibility is down", not "registration depends on eligibility being up."

**And nobody was told.** No alert fired. The incident was discovered by the front
desk, reported as a UI freeze, and reconstructed weeks later by us, from a
*vendor's* status page. That gap is real and it is Week 7's work — this week
produces the evidence that it exists.

## What we changed

Registration no longer waits for the payer.

```
before                                  after
──────                                  ─────
POST /intake                            POST /intake
  create patient                          create patient
  create coverage                         create coverage
  → verify eligibility  ◄── BLOCKS        record consents
      sleep 4.2s                          → schedule eligibility  ── returns immediately
      GET payer (no timeout)              return 201 (coverage: pending)
  record consents                                │
  return 201                                     └─► resolves in background,
                                                     updates the coverage row
```

And the payer call itself is now bounded, broken, and cached — `adr/0008` has the
derivation of every number:

| Control | Value | Why this value |
|---|---|---|
| Total timeout | **5 s** | Healthy `/intake` p95 is ~600 ms and a receptionist is standing in front of a patient. The budget comes from the **user's** tolerance, not the payer's SLA. |
| Breaker threshold | **5 consecutive failures** | Fewer trips on a single blip; more keeps feeding a dead endpoint. At five the breaker opens within seconds of an incident starting. |
| Cooldown | **30 s** | Tuesday was 19 minutes: ~38 probes, cheap, and recovery within 30 s of the payer returning. |
| Cache TTL | **24 h** | Coverage rarely changes intra-day. Covers a full-day outage; a stale answer is still same-day. |

**Replaying Tuesday against the new client** (`test_a_nineteen_minute_payer_outage_costs_almost_nothing`): 76 front-desk requests across the outage, all answered from last-known status, all marked stale, and the payer contacted a few dozen times instead of 76 — none of it on the registration path. Recovery within one cooldown of the payer returning.

## Staleness is surfaced, never hidden

The front desk now sees:

> **Active** — as of 9:03am (payer unreachable, showing last known).

not

> **Active**

Presenting a six-hour-old coverage status as current is how a patient gets billed
for an uncovered visit. And **"unknown" is not "inactive"** — the API returns
`active: null`, not `active: false`, because conflating "we could not check" with
"not covered" is how a covered patient gets turned away at the desk.

## What this changes for the front desk — a process question, not just a code one

Eligibility is now **eventually consistent**. A registration completes with
coverage `pending`, and the status arrives shortly after. Staff will need to know:

- `pending` means *we are checking*, not *there is a problem*.
- `stale` means *this was true earlier today and the payer is currently
  unreachable* — usable, but provisional.
- `unknown` means *proceed with registration and mark coverage unverified*.

**That is a workflow change and it needs walking through with the front-desk
lead.** We would rather flag it now than have it discovered at a busy desk.

## What we did not do

- **`interop-service` has the same synchronous shape** on the HL7 path. Named, not fixed — Week 6.
- **No alerting.** Week 7 owns it; `/breaker` on eligibility-service is the seam it hangs off.
- **The background resolution is a thread, not a durable queue.** Honest about what it is: the smallest change that removes the payer from the critical path in a synchronous FastAPI service with no worker in the stack. If the process restarts mid-resolution, that one coverage check is lost and the next visit re-checks. The durable version is named in `adr/0008` as the next step; what matters for the client is true either way — a hung payer can no longer hold a registration open.
