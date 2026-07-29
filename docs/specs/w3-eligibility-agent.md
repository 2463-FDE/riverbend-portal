# W3 Spec — Single-agent eligibility assistant on a resilient call path

- **Week:** 3 — Single-Agent Design + Memory
- **Requirements covered:** `RVB-W3-01` … `RVB-W3-13`
- **ADRs:** `adr/0008` (sync → async, breaker, degradation, agent runtime)
- **Branch:** `feat/w3-eligibility-agent` → `feat/riverbend-w1-w4`
- **Status:** specified

---

## 1. The ask and the real job

**Dr. Okonkwo asked for:** *"Front-desk eligibility checks are painful. Build a
little assistant the staff can chat with that checks a patient's insurance
eligibility and remembers the context of the visit."*

**What her artifacts say, when read together:**

| Artifact | Content |
|---|---|
| `eligibility-service/check.py` | The 270/271 payer call is a synchronous request with **no timeout**, executed inside the request thread |
| `intake-service` | Triggers that call **inline on the registration path** |
| `jira RIV-088` | *"Registration spins ~4–5s after Save before it confirms. Doesn't fail, just feels slow. Happens every time."* |
| `jira RIV-141` | *"Tue 9:00–9:20am the **ENTIRE intake screen** froze — front desk couldn't register any patient, **not just eligibility**."* |
| `payer-status-page.md` | ACME Clearinghouse incident **Tue 09:02–09:21 (19 min)**, elevated latency and timeouts on `/v1/eligibility` |
| `payer-status-page.md` | Portal `/intake` p95 flat ~600 ms all week, **one 20-minute spike past 30 s Tuesday morning**, overlapping the incident window |

**The finding.** These are not two tickets. RIV-088 and RIV-141 are the **same
defect at two severities**. Every registration pays the payer's latency because
the call is inline and unbounded; when the payer degraded for 19 minutes, every
registration inherited a 19-minute hang. A third party's outage became Riverbend's
outage, in a subsystem — patient registration — that has nothing to do with
insurance.

The client's mental model is "the portal is slow sometimes." The reality is an
**availability cliff with no contingency posture** — 45 CFR 164.308(a)(7)
contingency planning, and in plain business terms: during any payer blip the
clinic cannot register patients. That is revenue and it is access to care.

**The agent is the vehicle. The resilience work is the value.** Both ship.

---

## 2. Architecture

```
Staff chat ──► gateway ──► agent (LangChain create_agent, LangGraph runtime)
                              │  memory: checkpointer, thread_id = visit_id
                              │
                              └─ tool: check_eligibility(insurance_id)
                                        │
                                 EligibilityClient
                                   ├─ async, timeout-bounded
                                   ├─ circuit breaker (closed/open/half-open)
                                   ├─ last-known cache (staleness-marked)
                                   └─ never raises to the caller
```

### 2.1 The agent (`RVB-W3-01`, `RVB-W3-02`)

Built with LangChain v1 `create_agent` — **not** the deprecated
`create_react_agent` (ADR 0004). `create_agent` compiles to a LangGraph runtime,
so we get checkpointing and interrupts for free without hand-building a graph.

**Exactly one tool.** A "little assistant" with one job. Every tool added to an
agent dilutes its tool-choice accuracy; the client asked for eligibility, so the
agent gets eligibility. The tool list length is asserted in a test so nobody
quietly grows it.

**Visit-scoped memory.** `thread_id = visit_id`. Reusing the thread resumes the
conversation and its state; a new visit is a new thread with empty state. This is
LangGraph's documented persistence model — the `thread_id` in
`config["configurable"]` is the cursor into the checkpointer.

**Scope choice, stated:** memory is scoped to a *visit*, not to a patient and not
to a staff session. A visit is the unit of front-desk work, it has a natural end,
and it bounds how long PHI-bearing conversation state lives. Patient-scoped memory
would accumulate indefinitely and turn the checkpoint store into an unmanaged
clinical record.

### 2.2 Checkpoint encryption (`RVB-W3-03`)

Conversation state contains what staff typed, which will contain patient names and
insurance IDs. That is PHI at rest in a new store we just created.

| Environment | Saver | Serde |
|---|---|---|
| tests | `InMemorySaver` | default |
| production | `SqliteSaver` / `PostgresSaver` | **`EncryptedSerializer`** |

`langgraph.checkpoint.serde.encrypted.EncryptedSerializer` encrypts checkpoint
payloads at rest without hand-rolled crypto. A config test asserts the encrypted
serde is selected whenever the production flag is set — because the failure mode
here is silent: an unencrypted checkpoint store looks identical to an encrypted
one until someone reads the disk.

### 2.3 The resilient eligibility client — the real deliverable

| Control | Design | Requirement |
|---|---|---|
| **Non-blocking** | `httpx.AsyncClient`; the call never occupies a sync request thread | `RVB-W3-04` |
| **Timeout** | connect 2 s, read 4 s, **total budget 5 s**. Derived below. | `RVB-W3-04` |
| **Circuit breaker** | `failure_threshold=5` consecutive failures → **open** for `cooldown=30 s` → **half-open** admits 1 probe → success closes, failure re-opens for the cooldown | `RVB-W3-05` |
| **Last-known cache** | On success, cache `(insurance_id → status, checked_at)` with `ttl=24 h`. On timeout or open breaker, serve the cached value marked `stale=true` | `RVB-W3-06` |
| **Never raises** | The client returns a result object in every path. `unknown` is a valid status. | `RVB-W3-06` |

**Why 5 seconds, not 30.** The portal's healthy `/intake` p95 is ~600 ms. A payer
check that takes longer than a few seconds has already failed the front desk —
the receptionist is standing in front of a patient. The budget is set from the
*user's* tolerance, not the payer's SLA. Anything slower degrades to cached or
unknown, which is a better answer than a spinner.

**Why 5 consecutive failures.** Below that, a single transient blip trips the
breaker and we degrade unnecessarily. Above it, we spend too long feeding requests
into a dead endpoint. The Tuesday incident lasted 19 minutes — at 5 failures the
breaker opens within seconds and the remaining ~19 minutes cost the clinic
nothing.

**Why a 30-second cooldown.** The status page shows a 19-minute outage. A cooldown
much shorter re-probes uselessly; much longer keeps serving stale data after the
payer recovers. 30 s means ~38 probes across a 19-minute outage — cheap — and
recovery within 30 s of the payer returning.

**Staleness is surfaced, never hidden.** A stale result carries its original
`checked_at`. The front desk sees *"Active — as of 9:03am (payer unreachable)"*,
not *"Active."* Presenting a 6-hour-old coverage status as current is how you bill
a patient for an uncovered visit.

### 2.4 Intake is decoupled (`RVB-W3-07`)

The inline call comes off the registration path. Registration writes the patient
and returns; eligibility resolves separately and updates the coverage record.
Registration must complete during a total payer outage — that is the acceptance
test, and it is the entire point of the week.

### 2.5 Honest degradation in the agent (`RVB-W3-08`)

When the tool returns `unknown` or `stale`, the agent must say so. It must never
synthesize a coverage determination. A model that says "you're covered" because
that is the statistically common answer has invented a clinical-billing fact.

This is the control that replaces contextual grounding here. AWS documents
contextual grounding as **not supporting conversational/chatbot use cases**
(ADR 0004), so W3 does not get a grounding score. It gets something better suited:
a **deterministic post-check** that the status word in the agent's reply is
consistent with the status the tool returned. Mismatch → the reply is replaced
with a templated statement of the tool's actual result.

### 2.6 Tracing is off by default (`RVB-W3-12`)

Staff type free text. Free text at a front desk contains patient names — *"check
eligibility for Maria Gonzalez."* Our regex scrub catches SSNs, MRNs, dates and
phone numbers. **It does not catch names.**

Any third-party trace sink that uploads prompt bodies is therefore an
un-BAA'd disclosure path for this endpoint. Tracing is disabled unless *both* an
explicit flag and a key are present, and the ADR records that enabling it requires
either name detection on this path or a BAA covering the trace vendor. This is the
W1 limitation becoming material exactly where we predicted it would.

---

## 3. Findings to produce

| ID | Finding | Framing |
|---|---|---|
| `RVB-W3-09` | **D4 / twist #7** — synchronous, unbounded external call on the intake path. Reconstruct Tuesday from the three artifacts: payer degradation 09:02–09:21 → blocked request threads → intake p95 past 30 s → front desk cannot register. | An availability cliff, invisible in monitoring, triggered by a third party. 164.308(a)(7) contingency. Cost = every patient not registered during any payer blip. |
| `RVB-W3-10` | **RIV-088 and RIV-141 are one defect.** Steady-state tax and total outage, same root cause. | The client is tracking two tickets and will close the wrong one. Fixing "slow registration" cosmetically leaves the outage. |
| — | **Observability gap, named not fixed** | Nobody was alerted. The incident was discovered by the front desk and reconstructed by us, from a vendor's status page. That is W7's work; W3 produces the evidence that it is missing. |

---

## 4. Acceptance criteria

All offline, deterministic, zero-spend. The payer is a controllable fake.

| # | Test | Asserts | Req |
|---|---|---|---|
| 1 | `test_agent_has_exactly_one_tool` | Tool list length == 1 and it is `check_eligibility` | W3-01 |
| 2 | `test_visit_memory_persists_across_turns` | Two turns on one `thread_id` share context | W3-02 |
| 3 | `test_new_visit_starts_clean` | A different `thread_id` sees no prior state | W3-02 |
| 4 | `test_encrypted_serde_selected_in_prod` | Production flag → `EncryptedSerializer`; dev → plain `InMemorySaver` | W3-03 |
| 5 | `test_eligibility_call_is_bounded` | A payer that never responds returns within the total budget | W3-04 |
| 6 | `test_intake_does_not_invoke_blocking_eligibility` | **The property, not a proxy for it.** With the payer hung, `POST /intake` completes **and** the blocking eligibility call site is never invoked on that path (call-site spy asserts zero invocations). An earlier draft asserted only "the call is awaited and a concurrent request is served," which a still-coupled intake path could pass. | W3-04 |
| 7 | `test_breaker_opens_after_threshold` | 5 consecutive failures → state `open` | W3-05 |
| 8 | `test_breaker_short_circuits_while_open` | While open, **zero** payer calls are attempted | W3-05 |
| 9 | `test_breaker_half_open_probe` | After cooldown, exactly one probe is admitted | W3-05 |
| 10 | `test_breaker_closes_on_probe_success` | Successful probe → `closed`, normal traffic resumes | W3-05 |
| 11 | `test_breaker_reopens_on_probe_failure` | Failed probe → `open` for a further cooldown | W3-05 |
| 12 | `test_degrades_to_cached_with_staleness` | Payer down + warm cache → cached status, `stale=True`, original `checked_at` | W3-06 |
| 13 | `test_degrades_to_unknown_without_cache` | Payer down + cold cache → `unknown`, HTTP 200, no exception | W3-06 |
| 14 | **`test_intake_succeeds_during_payer_outage`** | Simulated 20-minute total outage → `POST /intake` still succeeds, within latency budget. **This is the Tuesday regression test.** | **W3-07** |
| 15 | `test_intake_latency_unaffected_by_payer_latency` | Payer at 8 s → `/intake` still returns near its baseline | W3-07 |
| 16 | `test_agent_reports_unknown_honestly` | Tool returns `unknown` → reply contains no active/inactive claim | W3-08 |
| 17 | `test_agent_status_mismatch_is_overridden` | Model asserts "active" while tool said `unknown` → reply replaced with the tool's actual result | W3-08 |
| 18 | `test_tracing_disabled_without_flag_and_key` | Tracing off unless both present; env with a stale flag alone does not enable it | W3-12 |
| 19 | `test_no_phi_in_agent_logs` | A name-bearing question produces no log record containing the name | W3-12 |
| 20 | `test_w1_w2_modules_unmodified` | This PR does not modify the W1 model client or the W2 index port | — |
| 21 | **`test_e2e_staff_chat_through_gateway`** | Log in → a staff chat turn through the gateway → the agent invokes `check_eligibility` and returns a status the front desk can act on. Stub model, faked payer, zero spend. **The client-visible feature, proven.** | **W3-14** |

**Live tier:** `L4 test_live_agent_single_tool_call` — one real agent turn against
Bedrock with a faked payer; asserts a tool call occurred and cost is under ceiling.
Gated behind `L0` (retention preflight).

---

## 5. Definition of done

- [ ] All acceptance tests pass offline, including the Tuesday outage regression
- [ ] `docs/findings/w3-eligibility-availability-cliff.md` with the three-artifact correlation
- [ ] ADR 0008 recorded with the derived numbers (5 s / 5 failures / 30 s / 24 h) and their justification
- [ ] Observability gap named and handed to W7
- [ ] W1 and W2 modules unmodified
- [ ] PR body answers the standing question
