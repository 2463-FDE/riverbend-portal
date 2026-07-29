# ADR 0005 — Production Bedrock client + PHI-safe logging (W1)

- **Status:** Accepted
- **Date:** 2026-07-29
- **Supersedes:** the "AI summary" approach removed at commit `d0905a1` (originally ADR 0003's follow-on)
- **Spec:** `docs/specs/w1-production-llm-client.md`
- **Baseline:** ADR 0004
- **Debate:** `docs/design-debate-w1-w4.md` D1, D2, D7, D10

## Context

The board wants an AI feature. The concrete Week-1 ask is an assistant that drafts
a patient-friendly summary of intake instructions.

A prior contractor wired an `ai-orchestrator` straight to a model. That version
put the **full patient record** (name, DOB, MRN, notes) into the prompt with no
BAA, and returned raw model output with **no timeout, no retry policy, no token
budget, no cost guard, and no validation**. A saved transcript shows it inventing
a medication the patient was not taking. It was removed at `d0905a1`.

Meanwhile the repo itself carries three findings the client cannot see: PHI logged
at INFO in `intake-service` (D1), a tracked `.env` holding DB, payer and Bedrock
credentials (D9), and a README asserting "All PHI encrypted — HIPAA compliant"
over plaintext `ssn` / `dob` / `notes` columns (D3).

Week 1's theme is *LLM engineering for production*. The deliverable is the safe
client and the findings — not the flagship feature.

## Decision

### 1. Instructions-only contract, enforced by shape

`POST /ai/summary` accepts `{"instructions": "<text>"}`. There is no
`patient_id`, `name`, `dob`, or `notes` field. A patient record is not rejected
by validation — it is **inexpressible**. A schema test asserts the request model's
field set is exactly `{instructions}` so the property cannot erode.

`deidentify.scrub_instructions` is the backstop for identifiers that appear inside
otherwise-legitimate text, not the primary control.

The clinical-record summary path (summarizing a real encounter) is **W8** work,
gated on an executed BAA and a Safe-Harbor scrub. W1 leaves a tripwire so it
cannot be reached accidentally.

### 2. One model client — `ChatBedrockConverse` — behind a resilience wrapper

Per ADR 0004. The wrapper owns:

- botocore connect/read timeouts **plus an overall wall-clock deadline** checked
  before each attempt. Socket timeouts bound a hop; only a deadline bounds a
  request. 20 s read × 3 retries + backoff is a 70-second page load.
- bounded retries with exponential backoff and jitter, retrying **only**
  throttling / 5xx / timeout classes. `ValidationException`,
  `AccessDeniedException` and `ResourceNotFoundException` fail immediately —
  retrying a wrong request turns a fast, clear config error into a slow, ambiguous
  outage, and a bad model ID is the failure that shows up in front of the board.
- a pre-call token cap and per-request USD ceiling; over-budget requests are
  refused before any spend.
- structured-output parsing with graceful degradation — malformed model output
  never becomes a 5xx.

`USE_STUB_BEDROCK=true` is the default: the stack runs with no AWS credentials and
spends nothing, shaping the call identically.

### 3. Two scrubs, deliberately different

`scrub_instructions` is aggressive (ssn, email, phone, mrn, date, long numeric
runs). `scrub_document` (used by W2 ingest) redacts **only** ssn, mrn and long
numeric runs, because dates and the clinic's own phone and email are legitimate
content in a policy document and redacting them destroys meaning for no privacy
gain.

Both report the *kinds* found. The values are never logged — logging what you
redacted defeats the redaction.

**Recorded limitation:** regex scrubbing does not detect patient **names**. W1's
contract makes that acceptable; W3's free-text staff questions make it material.
Carried forward to ADR 0008 rather than left implicit.

### 4. Tier-0 output validation

Two offline checks: a grounding score (stemmed, stopword-filtered overlap of
summary against source) and a **clinical-claim heuristic** that flags medications,
dosage patterns and diagnosis-shaped assertions absent from the source. Either
failing withholds the summary and returns a fixed safe message with
`needs_review=true` — **never raw model text**.

The second check exists specifically because the contractor's version invented a
medication, and it is tested against that transcript.

Tier-1 upgrade (`ApplyGuardrail` + contextual grounding) uses the same call sites
and is mock-tested now — see ADR 0004.

### 5. PHI-safe logging — the inverse of D1

Exactly one structured audit event per call: request id, model id, outcome, stub
flag, grounded flag, grounding score, input/output tokens, estimated cost,
latency, attempts, and the *kinds* of PHI redacted. **No prompt, no response, no
user-supplied string.** A test asserts the event's key set exactly, so no future
field can smuggle a body in.

A `RedactingFilter` on the service logger is a **backstop**, not the control — it
catches a future careless `log.info("... %s", user_text)`. The design keeps bodies
out; the filter catches the mistake.

### 6. `.env` untracked — narrow, deliberate deviation

The client packet scopes W1 to discovery. We deviate on exactly one item: `.env`
is untracked and added to `.gitignore`, with `.env.example` retained for key
names. Justification: a live Bedrock key is about to be placed in a tracked file.

**Untracking is not rotation.** Anything already committed is in the history and on
every clone; it must be rotated at the source or it remains live. A history
rewrite alone is insufficient. This is stated in the debt log and the PR body,
because fixing the tracking without saying this makes the problem invisible rather
than solved — strictly worse than leaving it visible.

### 7. Donor list from the prior WIP

Per debate D1, the uncommitted `feat/w2-rag-knowledge-base` work is a **donor, not
a base**. Ported deliberately and re-reviewed as new code:

| Ported | From | Why |
|---|---|---|
| Retry classification table | `bedrock_client._retryable` | The judgement about which errors are transient is the expensive part |
| Pre-call budget guard shape | `bedrock_client._guard_budget` | Refuse-before-spend is the right ordering |
| Two-tier scrub design | `deidentify.py` | The instructions/document distinction is subtle and correct |
| Grounding + clinical-claim checks | `guardrails.py` | Tuned against the real hallucination transcript |

**Not ported:** the raw `boto3.invoke_model` transport and the Anthropic-native
body construction — both replaced by `ChatBedrockConverse`.

## Consequences

- The board demo ships and is defensible: no PHI in the prompt by construction, bounded cost and latency, validated output, an audit trail of AI calls with no bodies in it.
- Real Bedrock use still requires an executed AWS BAA and credentials supplied by IAM role or short-term key. A long-term key in `.env` is a **demo** posture, recorded as such (ADR 0004, AWS documents long-term keys as "recommended only for exploration").
- **The retention preflight is a hard gate, added after the codex:rescue review.** The service refuses to serve unless the Bedrock effective data-retention mode is `none` and the configured model's `allowed_modes` contains `none`. A model requiring `provider_data_share` would share prompts and completions with the provider for up to 30 days — recreating debt D13 through the front door. See ADR 0004 §1a; requirement `RVB-X-09`.
- Grounding checks are heuristic and offline by design — no extra model calls, runs on every commit. Weaker than Bedrock's managed check; deliberately the floor, not the ceiling.
- D1 is fixed **on the AI path only**. `intake-service` still logs request bodies; that is named, not fixed, and belongs with W7's instrumentation work.
- D3 remains open (W9). The README's compliance claim is now contradicted in writing by our own findings doc, which is the point.
- The slow-registration complaint (RIV-088) is deliberately **not** addressed. Its cause is D4 and it is Week 3's work. Saying so with evidence beats guessing.
