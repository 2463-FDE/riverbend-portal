# W1 Spec — Production LLM client + PHI-safe logging

- **Week:** 1 — LLM Engineering for Production
- **Requirements covered:** `RVB-W1-01` … `RVB-W1-14`
- **ADRs:** `adr/0004` (platform baseline), `adr/0005` (this week's decisions)
- **Branch:** `feat/w1-production-llm-client` → `feat/riverbend-w1-w4`
- **Status:** specified

---

## 1. The ask and the real job

**Dr. Okonkwo asked for:** *"the registration page feels slow, and the board wants
'AI' — a little assistant that can draft a patient-friendly summary of our intake
instructions. Just get the repo running and show me something."*

**What we ship:** a production-grade Bedrock client with a summary feature on top
of it, and three findings she did not ask about and cannot see.

**What we deliberately do not ship:** a fix for the slow registration. Its cause is
a synchronous, un-timed payer call on the intake request path (D4) — that is
Week 3's work, and saying so now with evidence is worth more than a guess. Week 1
records it as *identified, scheduled*.

---

## 2. Scope boundary — the instructions-only contract

The single most important design property this week is **negative**: a patient
record cannot reach the model, because there is no way to express one at the
boundary.

```
POST /ai/summary
  { "instructions": "<intake instruction text>" }
→ { request_id, summary, grounded, needs_review, model, stubbed, usage }
```

There is no `patient_id`, no `name`, no `dob`, no `notes` field. This is not a
validation rule that can be relaxed; it is the shape of the contract. The
`deidentify.scrub_instructions` gate is the enforcement backstop for text that
*contains* an identifier despite the contract (a patient pastes an SSN into a
free-text field), not the primary control.

The clinical-record summary path — summarizing an actual encounter — is **W8**
work and is gated on an executed BAA and a Safe-Harbor scrub. W1 leaves a
deliberate tripwire so nobody wires a record through by accident.

---

## 3. Components

```
services/ai-orchestrator/
├── app.py               FastAPI surface — POST /summary, GET /healthz
├── config.py            env-driven settings, per-path model IDs, budgets
├── model_client.py      ChatBedrockConverse wrapper: deadline, retry, budget, parse
├── deidentify.py        scrub_instructions / scrub_document; PHI pattern table
├── guardrails.py        Tier-0 offline grounding + clinical-claim heuristics
├── audit.py             structured, body-free audit events
└── logging_config.py    RedactingFilter backstop
```

### 3.1 `model_client.py` — the resilience envelope

Wraps `ChatBedrockConverse`. Everything below is a requirement, not a nicety.

| Control | Behaviour | Requirement |
|---|---|---|
| **Connect timeout** | botocore `connect_timeout`, default 3 s | `RVB-W1-01` |
| **Read timeout** | botocore `read_timeout`, default 20 s | `RVB-W1-01` |
| **Wall-clock deadline** | An overall budget (default 30 s) covering *all* attempts and backoff. Checked before each attempt. | `RVB-W1-01` |
| **Retry** | Bounded attempts (default 3) with exponential backoff and jitter. `min(cap, base × 2^n) × U(0.5, 1.0)` | `RVB-W1-02` |
| **Retry classification** | Retry only throttling / 5xx / timeout classes. Validation, auth, and access-denied fail immediately. | `RVB-W1-02` |
| **Structured output** | Model constrained to a typed shape; parse with graceful degradation, never a 5xx to the caller. | `RVB-W1-03` |
| **Input cap** | Estimated input tokens above `max_input_tokens` → refuse pre-call. | `RVB-W1-04` |
| **Cost ceiling** | Projected worst-case cost above `max_cost_per_request_usd` → refuse pre-call. | `RVB-W1-04` |
| **Stub mode** | `USE_STUB_BEDROCK=true` (default) shapes the call identically but never leaves the process. | `RVB-W1-14` |

**Why an explicit deadline on top of socket timeouts.** A 20 s read timeout with
3 retries and backoff is a 70-second worst case for a caller who was promised a
page load. Socket timeouts bound a *hop*; they do not bound a *request*. The
deadline is checked before each attempt so a slow first attempt cannot buy a
fourth.

**Retry classification table** (the exact behaviour tested):

| Error | Retry? | Reason |
|---|---|---|
| `ThrottlingException`, `TooManyRequestsException` | yes | transient capacity |
| `ServiceUnavailableException`, `InternalServerException` | yes | transient server |
| `ModelTimeoutException`, `ReadTimeoutError`, `ConnectTimeoutError` | yes | transient network |
| `ValidationException` | **no** | the request is wrong; retrying sends the same wrong request |
| `AccessDeniedException`, `UnrecognizedClientException` | **no** | credentials/permissions; retrying cannot fix it and masks the real error |
| `ResourceNotFoundException` | **no** | usually a bad model ID — must surface loudly, not after 30 s |

Retrying a `ValidationException` is worse than useless: it turns a fast, clear
config error into a slow, ambiguous outage. The bad-model-ID case is the one that
bites in a demo, so it fails in under a second.

### 3.2 `deidentify.py` — two scrubs, deliberately different

| Function | Redacts | Rationale |
|---|---|---|
| `scrub_instructions` | ssn, email, phone, mrn, date, long numeric runs | Instruction text should carry no identifiers at all. Aggressive is correct. |
| `scrub_document` | ssn, mrn, long numeric runs **only** | A clinic policy document legitimately contains dates and the clinic's own phone/email. Redacting those destroys the document's meaning for no privacy gain. |

Both return `ScrubResult(text, found)`. `found` lists the *kinds* redacted. The
values are **never** logged — logging what you redacted defeats the redaction.

**Known limitation, stated not buried:** regex scrubbing does not catch patient
*names*. W1's contract makes that acceptable (instruction text has no name field).
It becomes material in W3, where staff type free-text questions containing names —
see ADR 0008 and requirement `RVB-W3-12`.

### 3.3 `guardrails.py` — Tier-0 offline validation

Two checks, both free and deterministic:

1. **Grounding score** — token-overlap of the summary against its source, stemmed
   and stopword-filtered. Below `grounding_threshold` (default 0.55) the summary
   is withheld and `needs_review=true` is returned with a safe message.
2. **Clinical-claim heuristic** — the summary is scanned for medication names,
   dosage patterns (`\d+\s?(mg|mcg|ml|units)`), and diagnosis-shaped assertions
   that do not appear in the source. Any hit fails the check regardless of score.

The contractor's version invented a medication the patient wasn't on. Check 2
exists specifically for that failure and is tested against that transcript.

**A failed check never returns raw model text.** It returns a fixed safe message.

Tier-1 upgrade path (ADR 0004): the same call site invokes Bedrock
`ApplyGuardrail` with contextual grounding, thresholds from config. Mock-tested
now; enabled by config later.

### 3.4 `audit.py` + logging — the inverse of D1

The system's current sin is logging full request bodies at INFO. The AI path does
the opposite by construction.

**Exactly one structured event per call**, containing only:

```
request_id, model_id, outcome, stubbed, grounded, grounding_score,
input_tokens, output_tokens, est_cost_usd, latency_ms, attempts,
phi_redacted_kinds
```

No prompt. No response. No instruction text. No user-supplied string of any kind.

`RedactingFilter` is installed on the service logger as a **backstop**, not the
control — it pattern-matches identifiers on every emitted record so that a future
careless `log.info("... %s", user_text)` is redacted rather than leaked. Defence
in depth: the design keeps bodies out, the filter catches the mistake.

---

## 4. Findings to produce (`docs/findings/w1-*.md`)

| ID | Finding | Evidence handed to us | Business framing |
|---|---|---|---|
| `RVB-W1-08` | **D1** — PHI in plaintext logs | `logs/intake-service.log`: `INFO request body={"name":…,"dob":"1971-03-02","ssn":…}` on every POST | The log aggregator is now a PHI store nobody classified as one. Every operator, every backup, every log-shipping vendor is in scope. 164.502(b) minimum-necessary; 164.312(b) — the thing that should be the audit trail is the largest attack surface. |
| `RVB-W1-09` | **D9** — secrets committed | `.env` tracked by git, absent from `.gitignore`, containing DB creds, payer key, Bedrock key | One repo leak is an instant ePHI compromise. "Private repo" is not a control — it is a hope. 164.308 risk management. |
| `RVB-W1-10` | **D3 / twist #1** — "HIPAA compliant" is self-asserted | README claims all PHI encrypted; `db/schema.sql` shows `ssn`, `dob`, `notes` as plaintext `TEXT`; ADR 0002 documents disk-level encryption only | Today encryption at rest is *Addressable*, so the belief is defensible-ish. The 2025 Security Rule NPRM closes that loophole. This is a scheduled failure, not a current pass. |

Each finding must state: what we saw, where we saw it, what it means in dollars /
audit exposure / patient safety, and what it would take to fix — in that order.
No finding may cite anything not present in the handover artifacts.

---

## 5. The `.env` remediation (`RVB-W1-12`)

Narrow, deliberate deviation from "W1 is discovery only" — justified because a
live Bedrock key is about to be placed in a tracked file (debate D7).

1. `git rm --cached .env`
2. Add `.env` (and `.env.*`, excluding `.env.example`) to `.gitignore`
3. `.env.example` carries key **names** and safe defaults only

**Stated prominently in the debt log and the PR body:**

> Untracking is not rotation. Any credential already committed is in the git
> history and on every clone that has ever been made. It must be rotated at the
> source — AWS, the payer, the database — or it remains live. A history rewrite
> alone is insufficient.

---

## 6. Acceptance criteria

Every row is a test that must exist and pass with **no AWS credentials present**.

| # | Test | Asserts | Req |
|---|---|---|---|
| 1 | `test_deadline_bounds_total_wall_clock` | A client whose every attempt hangs raises `BedrockUnavailable` within `deadline_s` (+ tolerance), not `read_timeout × attempts` | W1-01 |
| 2 | `test_retry_classification[error, expected]` | Parametrized over the table in §3.1 — retryable errors attempt `max_retries+1` times, non-retryable exactly once | W1-02 |
| 3 | `test_backoff_is_bounded_and_jittered` | Sleep intervals are non-decreasing in expectation, never exceed `backoff_cap_s`, and are not identical across runs | W1-02 |
| 4 | `test_structured_output_parse[variant]` | Clean JSON, JSON in a prose wrapper, truncated JSON, and plain prose all yield a string summary; none raise | W1-03 |
| 5 | `test_budget_refuses_before_any_call` | Oversized input raises `BudgetError` and the underlying client is never invoked (call count == 0) | W1-04 |
| 6 | `test_cost_ceiling_refuses` | Projected cost above ceiling raises `BudgetError` pre-call | W1-04 |
| 7 | `test_gateway_summary_requires_session` | `POST /ai/summary` without a session → 401 | W1-05 |
| 8 | `test_request_model_has_no_patient_fields` | The Pydantic model's field set is exactly `{instructions}` — a schema-level guard against someone adding `patient_id` later | W1-06 |
| 9 | `test_scrub_instructions_redacts[kind]` | SSN, email, phone, MRN, date, long-number each redacted; `found` reports the kind | W1-06 |
| 10 | `test_scrub_document_preserves_dates_and_contact` | A policy document keeps its dates/phone/email; SSN and MRN still redacted | W1-06 |
| 11 | `test_no_phi_in_any_log_record` | Post PHI-bearing instructions with a log capture; assert no identifier substring appears in **any** emitted record, and that no record contains the instruction text | W1-07 |
| 12 | `test_audit_event_fields_exact` | The audit event's key set matches §3.4 exactly — no extra key can smuggle a body in | W1-07 |
| 13 | `test_redacting_filter_catches_careless_log` | A deliberately careless `log.info` with PHI is redacted by the filter | W1-07 |
| 14 | `test_ungrounded_summary_is_withheld` | Injected ungrounded text → safe message, `grounded=False`, `needs_review=True`, and raw model text absent from the response | W1-03 |
| 15 | `test_invented_medication_is_caught` | The contractor's hallucinated-medication transcript fails the clinical-claim check even when overlap score passes | W1-03 |
| 16 | `test_env_is_not_tracked` | `git ls-files` contains no `.env` | W1-12 |
| 17 | `test_short_source_refuses` | Source below `min_source_chars` → refusal, no model call | W1-03 |
| 18 | `test_apply_guardrail_wiring` (mocked) | With Tier 1 enabled, `ApplyGuardrail` is called with `source=INPUT` before generation and `source=OUTPUT` before serving; a `BLOCKED` verdict suppresses the response | W1-14 |

**Live tier (written now, skipped without a key):**

| # | Test | Asserts |
|---|---|---|
| L1 | `test_live_model_resolves` | The configured inference-profile ID resolves and returns a completion |
| L2 | `test_live_summary_under_budget` | One real summary call; asserts `est_cost_usd` below a hard per-test ceiling |

---

## 7. Definition of done

- [ ] Every acceptance test above exists and passes offline
- [ ] Three findings written with evidence drawn only from handover artifacts
- [ ] Onboarding seam map (1 page) — services, ports, stores, trust boundaries, PHI crossings
- [ ] Debt-log entries for D1, D3, D9 with business framing
- [ ] `.env` untracked, `.gitignore` updated, rotation obligation stated
- [ ] ADR 0005 recorded with donor list from the prior WIP
- [ ] PR body answers the standing question: what can the client *see*, and what did we *find* that they could not have
