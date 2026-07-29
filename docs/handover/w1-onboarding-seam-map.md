# Riverbend Portal — Onboarding Seam Map

**Written:** 2026-07-29, end of Week 1 · **Requirement:** `RVB-W1-11`
**Audience:** an engineer on day one. Readable without opening the code.

The point of this page is not "here are the services." It is **where does PHI
cross a boundary, and what checks it there.** Every row with a ❌ is a place the
system is trusting something it has not verified.

---

## 1. The shape of the system

```
                    ┌──────────────────────────────────────────────┐
  Browser ─────────►│  Next.js portal            :3070             │
                    └────────────────────┬─────────────────────────┘
                                         │  Authorization: Bearer <token>
                                         ▼
                    ┌──────────────────────────────────────────────┐
                    │  gateway / BFF            :8070              │  ← the ONLY
                    │  login · sessions · fan-out                  │    front door
                    └───┬────┬────┬────┬────┬────┬────┬────────────┘
                        │    │    │    │    │    │    │
        intake :8071 ◄──┘    │    │    │    │    │    └──► ai-orchestrator :8077  (W1, new)
     eligibility :8072 ◄─────┘    │    │    │    └───────► roi        :8076
        records :8073 ◄───────────┘    │    └────────────► interop    :8075
                       scheduling :8074 ┘
                        │
       ┌────────────────┴─────────────────┐
       │  Postgres 15   ·   Redis         │
       └──────────────────────────────────┘

External: payer EDI gateway (eligibility) · hospital HL7 v2 feed (interop)
          AWS Bedrock (ai-orchestrator, W1)
```

The portal never calls a domain service directly. That is the one structural
thing this system got right, and it is why W4's fix has somewhere to live.

---

## 2. Where PHI lives

| Store | Contains | Encrypted at rest | Access-controlled |
|---|---|---|---|
| Postgres `patients` | name, dob, **ssn**, address, phone, email, notes | Volume only. Columns are plaintext `TEXT`. | DB credential only — and see Finding W1-2 |
| Postgres `encounters` / `records` | clinical notes, allergies, medications, labs | Volume only | ❌ no ownership check on read (Finding W4, next month) |
| Postgres `audit_logs` | often the full request body | Volume only | ❌ **mutable and soft-deletable** — logging, not auditing |
| Redis `session:<token>` | username, role | ❌ | ❌ **no TTL — sessions never expire** |
| `logs/intake-service.log` | **full request bodies: name, dob, ssn** | ❌ | ❌ filesystem only — Finding W1-1 |
| Object store | consent PDFs, lab documents | — | not reviewed in W1 |

---

## 3. The trust boundaries, and what checks each one

| # | Boundary | Crosses it | What checks it today |
|---|---|---|---|
| B1 | Browser → portal | credentials, patient input | TLS. Token in `localStorage`. |
| B2 | Portal → gateway | bearer token + payload | `require_session` — proves *a* session exists |
| B3 | Gateway → domain service | the request, unmodified | ❌ **nothing.** Services trust the gateway absolutely and are reachable on the compose network |
| B4 | Service → Postgres | SQL | The DB credential. No row-level scoping. |
| B5 | Service → `logs/*.log` | **full request bodies** | ❌ **nothing** — Finding W1-1 |
| B6 | eligibility → payer EDI | member id | ❌ no timeout, no breaker. *A payer outage takes down intake* — Week 3 |
| B7 | interop ← hospital HL7 | ADT/ORU messages | Brittle parser; silently drops AL1/RXA — Week 6 |
| B8 | **ai-orchestrator → AWS Bedrock** | instruction text only | ✅ **W1: see §4** |

**B2 is the load-bearing weakness.** `require_session` answers *"is someone
logged in?"* It has no way to answer *"is this the right someone?"*, because the
session carries only `username` and `role` — there is no patient identity in it
at all. That is why the IDOR exists, and why fixing it in Week 4 needs a schema
change rather than an `if` statement.

---

## 4. The one boundary that is checked: B8

Week 1's deliverable. Recorded here as the pattern the other seams should follow.

```
POST /ai/summary  {"instructions": "..."}
      │
      ├─ gateway: require_session                        (no new anonymous surface)
      ├─ contract: NO patient_id / name / dob / notes    (extra="forbid")
      ├─ retention preflight: Bedrock mode must be `none` — else refuse to serve
      ├─ scrub: identifier-shaped tokens redacted
      ├─ budget: input cap + per-request USD ceiling, refused BEFORE spend
      ├─ transport: connect/read timeouts + a WALL-CLOCK DEADLINE over all retries
      ├─ retry: classified — throttle/5xx/timeout only; validation/auth fail fast
      ├─ validate: grounding score + invented-clinical-claim check
      │            → fails → safe message, needs_review=true, never raw model text
      └─ audit: ONE event, CLOSED key set, no bodies
```

The negative property is the important one: **a patient record cannot be
expressed at this boundary.** It is not rejected by validation; there is no field
for it.

---

## 5. What a new engineer should read, in order

1. `docs/specs/requirements-w1-w4.md` — what the client asked for and what is actually wrong
2. `docs/design-debate-w1-w4.md` — why the architecture is what it is, including the arguments we lost
3. `adr/0004` — the AI platform baseline (Bedrock, LangGraph v1, Chroma, retention)
4. `docs/findings/w1-*.md` — the three Week-1 findings
5. `ARCHITECTURE.md` §7 — the contractor's own honest list of what they left behind

## 6. Two things that will surprise you

**Every service is a copy-paste of the same five modules** (`config`, `db`,
`models`, `schemas`, `logging_config`) with no shared library — see `adr/0001`.
It is why `tests/conftest.py` has to evict colliding module names before loading
a service: every service has a `config.py`, and `from config import settings`
resolves through `sys.modules`.

**The `audit_logs` table is not an audit log.** It is an ordinary mutable table
with a `deleted_at` column. Rows can be updated and soft-deleted. Nothing in the
system can currently answer "who viewed this patient?" — that is Week 10's
capstone, and it is worth knowing on day one so you do not go looking for a
control that isn't there.
