# ADR 0008 — Eligibility: sync → async, circuit breaker, graceful degradation, and the agent runtime (W3)

- **Status:** Accepted
- **Date:** 2026-07-29
- **Spec:** `docs/specs/w3-eligibility-agent.md`
- **Baseline:** ADR 0004
- **Debt:** D4, twist #7
- **Client tickets:** RIV-088, RIV-141

## Context

The Week-3 ask was a chat assistant for front-desk eligibility checks. Reading the
handover artifacts together produced a different headline.

| Artifact | What it says |
|---|---|
| `eligibility-service/check.py` | The 270/271 payer call is synchronous with **no timeout**, inside the request thread |
| `intake-service` | Triggers that call **inline on the registration path** |
| RIV-088 | *"Registration spins ~4–5s… Happens every time."* |
| RIV-141 | *"Tue 9:00–9:20am the ENTIRE intake screen froze — front desk couldn't register any patient, **not just eligibility**."* |
| `payer-status-page.md` | ACME Clearinghouse: eligibility endpoint degraded **Tue 09:02–09:21**, 19 minutes |
| `payer-status-page.md` | `/intake` p95 flat ~600 ms all week; **one 20-minute spike past 30 s Tuesday morning**, overlapping that window |

**RIV-088 and RIV-141 are the same defect at two severities.** Every registration
pays the payer's latency because the call is inline and unbounded. When the payer
degraded for 19 minutes, every registration inherited a 19-minute hang. A third
party's outage became Riverbend's outage, in a subsystem — patient registration —
that has nothing to do with insurance.

The client's model is *"the portal is slow sometimes."* The reality is an
availability cliff with no contingency posture: 45 CFR 164.308(a)(7), and in
business terms, during any payer blip the clinic cannot register patients.

## Decision

### 1. The agent — `create_agent`, one tool, visit-scoped memory

LangChain v1 `create_agent`, **not** the deprecated `create_react_agent`
(ADR 0004). It compiles to a LangGraph runtime, so checkpointing and interrupts
come for free without hand-building a graph.

**Exactly one tool** (`check_eligibility`), asserted by test. Every tool added to
an agent dilutes tool-choice accuracy; the client asked for eligibility.

**Memory is scoped to a visit** — `thread_id = visit_id`. A visit is the unit of
front-desk work, it has a natural end, and it bounds how long PHI-bearing
conversation state lives. Patient-scoped memory would accumulate indefinitely and
quietly turn the checkpoint store into an unmanaged clinical record.

### 2. Checkpoint state is PHI at rest — encrypt it

Conversation state contains what staff typed, which contains names and insurance
IDs.

| Environment | Saver | Serde |
|---|---|---|
| tests | `InMemorySaver` | default |
| production | `SqliteSaver` / `PostgresSaver` | **`EncryptedSerializer`** |

`langgraph.checkpoint.serde.encrypted.EncryptedSerializer` encrypts checkpoint
payloads at rest without hand-rolled crypto — a direct answer to
164.312(a)(2)(iv) for this store, and notable as the *one* place in this system
where PHI at rest is actually encrypted (contrast D3).

A config test asserts the encrypted serde is selected under the production flag,
because the failure mode is silent: an unencrypted checkpoint store looks
identical to an encrypted one until someone reads the disk.

### 3. The resilient eligibility client — with the numbers derived, not guessed

| Control | Value | Derivation |
|---|---|---|
| Transport | `httpx.AsyncClient` | The call must not occupy a sync request thread |
| Connect timeout | 2 s | |
| Read timeout | 4 s | |
| **Total budget** | **5 s** | Healthy `/intake` p95 is ~600 ms. A receptionist is standing in front of a patient; a check slower than a few seconds has already failed them. The budget comes from the **user's** tolerance, not the payer's SLA. |
| **Breaker threshold** | **5 consecutive failures** | Fewer, and one transient blip degrades us unnecessarily. More, and we keep feeding requests into a dead endpoint. At 5 the breaker opens within seconds of the Tuesday incident starting, so the remaining ~19 minutes cost the clinic nothing. |
| **Cooldown** | **30 s** | The observed outage was 19 minutes. Much shorter re-probes uselessly; much longer serves stale data after recovery. 30 s ≈ 38 probes across 19 minutes — cheap — and recovery within 30 s of the payer returning. |
| **Cache TTL** | **24 h** | Coverage rarely changes intra-day. Long enough to cover a full-day outage, short enough that a stale answer is same-day. |

**The client never raises.** Every path returns a result object; `unknown` is a
valid status.

**Staleness is surfaced, never hidden.** A degraded result carries its original
`checked_at`. The front desk sees *"Active — as of 9:03am (payer unreachable)"*,
not *"Active."* Presenting a six-hour-old coverage status as current is how a
patient gets billed for an uncovered visit.

### 4. Eligibility comes off the registration path

Registration writes the patient and returns. Eligibility resolves separately and
updates the coverage record. **Registration must complete during a total payer
outage** — that is the acceptance test and the point of the week.

### 5. Honest degradation is enforced, not prompted

When the tool returns `unknown` or `stale`, the agent must say so and must never
synthesize a coverage determination.

AWS documents Bedrock contextual grounding as supporting summarization,
paraphrasing and QA, and states that **"Conversational QA / Chatbot use cases are
not supported"** (ADR 0004). So this path deliberately does **not** get a
grounding score. It gets a control better suited to its shape: a **deterministic
post-check** that the status word in the agent's reply is consistent with the
status the tool returned. On mismatch the reply is replaced with a templated
statement of the tool's actual result.

Papering over that limitation would have shipped a guardrail that silently did not
apply.

### 6. Third-party tracing is off by default

Staff type free text; free text at a front desk contains patient names
(*"check eligibility for Maria Gonzalez"*). Our regex scrub catches SSNs, MRNs,
dates and phone numbers — **it does not catch names** (ADR 0005, recorded
limitation, now material).

Any trace sink that uploads prompt bodies is therefore an un-BAA'd disclosure path
for this endpoint. Tracing requires **both** an explicit flag and a key; a stale
ambient flag alone cannot enable it. Enabling it in production requires either
name detection on this path or a BAA covering the trace vendor.

## Consequences

- The clinic can register patients during a payer outage. That is the deliverable, and it is worth more than the assistant.
- Two open tickets collapse into one root cause. The client would otherwise have closed RIV-088 cosmetically and kept the outage.
- Eligibility becomes eventually-consistent. Front-desk workflow must tolerate "pending" and "stale," which is a **process** change, not just a code change, and needs to be walked through with the front-desk lead.
- A new persistent store (checkpoints) now holds PHI. It is encrypted, but it is also new surface: it needs retention, and retention is not defined here. Named for W9/W10.
- Nobody was alerted on Tuesday. The incident was discovered by the front desk and reconstructed by us from a vendor's status page. **The observability gap is named here and handed to W7** — W3 produces the evidence that it exists.
- `interop-service`'s HL7 path has the same synchronous shape and is **not** fixed here (W6).
