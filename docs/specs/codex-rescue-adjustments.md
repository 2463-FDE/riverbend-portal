# codex:rescue — findings, verdicts, and plan adjustments

- **Date:** 2026-07-29
- **Reviewer:** OpenAI Codex CLI 0.133.0, `model_reasoning_effort=high`, web search enabled
- **Scope reviewed:** `docs/specs/*`, `docs/design-debate-w1-w4.md`, `adr/0004`–`0010`, against `client-packets.md` W1–W4 and the brownfield code
- **Result:** 17 findings (10 P1, 7 P2). **13 accepted, 3 accepted-with-modification, 1 rejected.**

Every accepted finding is applied to the plan. This file is the audit trail; the
plans themselves are edited in place.

---

## The two findings that changed the architecture

### R1 — Bedrock data retention is a mode, and some models force provider sharing **[P1, ACCEPTED — material]**

**Codex said:** the claim *"providers can't read prompts/completions"* is overbroad;
Bedrock defines data-retention modes and some models require `provider_data_share`.

**We verified this ourselves and Codex is right.** Per
[Bedrock data retention](https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html):

| Mode | Behaviour |
|---|---|
| `none` | **Zero data retention.** No request/response data written to durable storage by AWS or shared with the model provider. |
| `default` | Model's own policy. AWS **may retain** data for safety and abuse prevention. Provider does not receive it. |
| `provider_data_share` | AWS **retains and shares** inference data with the model provider. Required for access to certain models. |
| `inherit` | Defer to broader scope. Default for new accounts and projects. |

Resolution: `effective = first non-inherit of (project → account → model default)`.

> "For models requiring `provider_data_share` (currently Claude Mythos 5 and Claude
> Fable 5): user prompts and completions are shared with Anthropic and retained for
> up to 30 days for trust and safety purposes."

And [abuse detection](https://docs.aws.amazon.com/bedrock/latest/userguide/abuse-detection.html):

> "Amazon Bedrock uses a zero operator access (ZOA) data security model… Also,
> Amazon Bedrock uses a zero data retention (ZDR) data security model. This means
> that by default, Amazon Bedrock does not store model inputs or outputs."
> …with named exceptions where classifier-flagged traffic is retained up to 30 days.

**Why this matters more than a wording fix.** A PHI workload that selects a
`provider_data_share`-only model has made an **impermissible disclosure to a third
party** — exactly debt D13, the thing this engagement is supposed to be preventing.
Model choice is now a compliance control, not a performance preference.

**The saving grace is that Bedrock fails closed:** *"If your account or project is
configured for zero data retention (`data_retention_mode: none`) and you invoke a
model that requires retention, Amazon Bedrock will block the request and return an
error."* A model whose `allowed_modes` excludes `none` reports
`status: "unavailable"` rather than silently sharing.

**Adjustments applied:**
- **New global constraint `RVB-X-09`** — effective data-retention mode must be `none` for every PHI-adjacent path. Verified, not assumed.
- **New acceptance test (all four weeks)** — a preflight assertion that the effective mode is `none` and that the configured model's `allowed_modes` contains `none`. Fail closed at startup, not at first PHI request.
- **ADR 0004 §1 rewritten** — the retention-mode table, the fail-closed behaviour, the SCP that enforces `none` org-wide, and the explicit statement that models requiring `provider_data_share` are **disqualified for this workload regardless of capability**.
- **Live smoke test L0 added** — `GET /v1/models/{model}` and assert `allowed_modes` contains `none` *before* any inference test spends a token.
- The old claim is corrected: the Model Deployment Account statement remains true and is retained, but it is no longer presented as the whole story.

### R2 — W4's authorization cannot be implemented as specified **[P1, ACCEPTED — blocker]**

**Codex said:** the W4 tests say *"session for patient 1042"*, but
`services/gateway/security.py` stores only `username` and `role` in the session,
and every user has the single `staff` role. **There is no patient identity to
authorize against.**

This is correct, and it is the single most valuable finding in the review. The
whole W4 authorization design rested on a claim the codebase cannot satisfy. It
would have been discovered halfway through implementation.

**Adjustments applied:**
- **New ADR 0011 — session identity binding.** The client's ask is *"let **patients** see their **own** labs"*, so a patient-portal account must resolve to a patient. Adds a nullable `users.patient_id` FK, populates the session with it, and makes `AuthorizedScope` derivable. This is the actual, missing precondition for closing the IDOR — and it is small: one migration, one gateway change, seed data.
- **W4 spec §2.1 rewritten** to derive `AuthorizedScope` from the bound session identity, with distinct rules for a patient principal (self only, plus `SAME_AS` fragments) and a staff principal (treatment relationship, deliberately coarse this week and flagged for W9's RBAC split).
- Requirement `RVB-W4-03` restated in terms that are implementable.

---

## Findings accepted and applied

### R3 — `ApplyGuardrail(source=INPUT)` cannot run contextual grounding **[P1, ACCEPTED]**
The plan said `ApplyGuardrail` runs with `source=INPUT` before generation and
`source=OUTPUT` before serving, and implied contextual grounding on both.
Contextual grounding requires three components — grounding source, query, and the
content to guard (the model response) — so it is an **output-side** check only.
`source=INPUT` is valid for content filters, topic avoidance, word lists and **PII
detectors**, which is genuinely useful before retrieval, but it is a different
policy set.
**Applied:** ADR 0004 §4 and the W1/W2 specs now separate the two explicitly, and
the Tier-1 wiring tests assert the correct policy on the correct side.

### R4 — D11 is not "fixed"; only a new path is protected **[P1, ACCEPTED]**
The traceability table claimed D11 fixed in W4, but `gateway/app.py:144` and
`records-service/app.py:86` remain open.
**Applied:** W4 now fixes the **existing** `GET /patients/{id}/records` route at
the gateway — which is where the client's actual exposure lives — and the
traceability table is corrected. Protecting only a new assembler while leaving the
walked route open would have been a paper fix.

### R5 — The "permanent failing IDOR test" is incoherent **[P1, ACCEPTED]**
A test asserting the vulnerability still reproduces either enshrines the bug (if
passing) or breaks CI (if failing).
**Applied:** replaced with (a) a regression test asserting the walk is now
**denied**, and (b) the pre-fix reproduction preserved as evidence in
`docs/findings/w4-idor-record-access.md` with the HAR excerpt. Evidence belongs in
the finding; tests assert the fix.

### R6 — W2 indexes patient records but scrubs them like policy documents **[P1, ACCEPTED, modified]**
`scrub_document` deliberately preserves dates, phone and email — correct for a
clinic policy PDF, wrong for a patient record, where those are Safe-Harbor
identifiers under 164.514(b)(2).
**Modification:** we reject the implied remedy of scrubbing patient records
aggressively — retrieval over patient history is the client's ask, and a scrubbed
corpus cannot serve it.
**Applied:** two distinct ingest paths. `ingest_document` (clinic knowledge,
lenient scrub, shared collection) and `ingest_record` (patient corpus, **separate
Chroma collection declared a PHI store**, direct identifiers stripped where not
needed for retrieval, patient-scoped query filter, access-controlled and audited
like the database). The honest position is that the index *is* a PHI store and
must be governed as one — not that it can be scrubbed into not being one.

### R7 — Surface asks are not acceptance-tested **[P1, ACCEPTED]**
Every week tested its controls and none tested the feature the client would
actually click. W1 tested only that an unauthenticated call gets 401 — an endpoint
that rejects everything would pass.
**Applied:** each week gains an end-to-end happy-path test through the gateway
proving the client-visible surface works: W1 summary renders, W2 retrieval returns
cited results for a chart query, W3 staff chat completes a turn with a tool call,
W4 a patient sees their own assembled view. Zero spend — all four run against the
stub model.

### R8 — `knowledge_admin` is not implementable from current auth **[P1, ACCEPTED]**
Only the `staff` role exists.
**Applied:** W2 specifies the mechanism concretely — an env-driven username
allowlist plus a role allowlist, enforced at the gateway, with `/me` exposing the
capability so the portal does not guess policy client-side. Explicitly an interim
control until the W9 RBAC split, and recorded as such.

### R9 — `Record.source_message_id` does not exist **[P1, ACCEPTED]**
ADR 0010's provenance edge depends on a column absent from `db/schema.sql`.
**Applied:** provenance demoted to **Proposed**, with the migration named as W6
scope where it is actually needed. W4 builds the graph without it. This also
relieves W4's budget (see R16).

### R10 — `RVB-X-08` mis-cites 164.502 **[P1, ACCEPTED]**
164.502(b) is minimum necessary; 164.502(e) is business-associate assurances.
Neither is an absolute technical prohibition.
**Applied:** the constraint is restated as our engineering policy, with the
citations correctly described as what motivates it rather than what mandates it.
Overstating a citation is the same failure mode as the README's "HIPAA compliant"
claim, and we would have been doing it in a document criticising it.

### R11 — `langgraph-supervisor` facts are wrong **[P2, ACCEPTED]**
We claimed 0.0.31 predates LangGraph 1.0. Verified via PyPI: **LangGraph 1.0.0
released 2025-10-17; `langgraph-supervisor` 0.0.31 released 2025-11-19** — a month
*after* — and it declares `langgraph>=1.0.2,<2.0.0`, `langchain-core>=1.0.0,<2.0.0`,
so it **is** v1-compatible.
**Applied:** ADR 0009's rejection is rewritten honestly. The version argument is
downgraded to what it actually supports (0.0.x, no release in eight months). The
**real** reason is promoted to primary: a supervisor delegates routing to a model,
and this design requires a *deterministic* authorization edge before fan-out.
Model-decided routing fights that invariant. That argument was always the strong
one; we were leaning on a weaker factual claim that happened to be false.

### R12 — "Addressable" is misused **[P2, ACCEPTED]**
Addressable under 164.306(d) does not mean optional — a covered entity must
implement the specification if reasonable and appropriate, or document why not and
implement an equivalent alternative. The 2025 Security Rule NPRM is proposed; the
current rule is in effect.
**Applied:** the W1 spec and ADR 0005 language corrected. "Defensible-ish" and
"loophole" removed. The finding is *stronger* stated correctly: Riverbend has
neither implemented encryption at rest nor documented an equivalent alternative,
which is a current-rule gap, not a future one.

### R13 — The backoff test is statistical, not deterministic **[P2, ACCEPTED]**
"Non-decreasing in expectation" and "not identical across runs" is a flaky test.
**Applied:** RNG and clock are injected; the test asserts exact bounds under a
fixed seed, plus that two different seeds produce different sequences.

### R14 — `answer_match` has no definition **[P2, ACCEPTED]**
**Applied:** the gold-set schema now carries an explicit `key_facts: [str]` list
per query, and `answer_match` is defined as the fraction of `key_facts` present in
the answer after normalization. Stated in the spec so it cannot drift into string
matching theatre.

### R15 — The W3 non-blocking test is too weak **[P2, ACCEPTED]**
**Applied:** strengthened to assert that `POST /intake` under a hung payer
completes **and** that the blocking eligibility call site is not invoked on that
path — the property, not a proxy for it.

### R16 — W4 is overloaded for ~40 hours **[P2, ACCEPTED, modified]**
Codex proposed cutting HITL and KG provenance.
**Modification:** HITL stays — it is an explicit engagement requirement and it is
one `interrupt()` call at one node. Provenance is cut (see R9).
**Also cut from W4:** the live per-domain degradation of the *coverage* branch
(reuses W3's client, tested there); the N+1 measurement moves from a test to a
one-off measurement script whose output is pasted into the findings doc.
**Net:** W4 ships the session binding, the gateway IDOR fix, the graph, the
fan-out, the deterministic authz gate, one HITL gate, and the findings.

### R17 — W2 is oversized **[P2, ACCEPTED, modified]**
**Applied cuts:** eval-run persistence across restart drops to in-process only
(the run is re-runnable in seconds); the citation *UI* is dropped, citations remain
in the API payload; hybrid retrieval is **kept** — it is ~40 lines, it is the
week's curriculum point, and lexical matching is where clinical terms live.
**Kept as headline:** the fragmentation metrics. Everything else yields to them.

---

## Finding rejected

### R-rej — "W2 does not prove retrieval runs *when they open a chart*"
Part of R7, and R7's E2E test is accepted. But the *specific* framing — that
retrieval must be wired into the chart-open lifecycle — is **rejected as scope**.
The client packet sizes W2's deliverable as *"a RAG retrieval eval harness whose
report surfaces the fragmentation, plus an ADR proposing an MPI approach"* — a
harness and a finding, explicitly not a chart-integration feature. Wiring
retrieval into the chart view is a UI integration we would be inventing.
**Recorded in the roadmap instead**, so the omission is a decision rather than a
gap. The E2E test proves the retrieval endpoint answers a chart-shaped query;
it does not pretend the chart page calls it.

---

## Net effect on the plan

| Change | Impact |
|---|---|
| Bedrock retention mode `none`, verified and fail-closed | **New hard control.** Prevents the flagship AI feature from becoming debt D13. |
| Session → patient identity binding (ADR 0011) | **Unblocks W4.** Without it the authorization design was unimplementable. |
| Gateway IDOR fix on the existing route | D11 genuinely mitigated on the path the client is actually exposed on. |
| Separate PHI collection + patient-scoped query for the record corpus | The vector index is governed as the PHI store it is. |
| Four end-to-end surface tests | The client-visible feature is proven, not just its guardrails. |
| Provenance, eval persistence, coverage degradation, N+1-as-test cut | W2 and W4 brought back inside a ~40 hr week. |
| Citation and factual corrections (supervisor dates, Addressable, 164.502) | The documents stop making the mistake they criticise. |

**One week's budget moved.** ADR 0011's session binding is W4 work by dependency,
but it is small and it is the precondition for the week's headline. The cuts above
pay for it.
