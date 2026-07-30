# Riverbend W1–W4 — Design Debate Record

**Participants:** Product Engineer (PE) · Senior Engineer (SE)
**Date:** 2026-07-29
**Purpose:** settle the contested decisions *before* code, on the record, with the compromise and its cost written down. Every decision that survives here becomes an ADR.

Ground rule agreed at the top: **neither role gets to win by assertion.** A position needs either a citable source, a measurement, or a named cost we're accepting.

---

## D1 — Do we reuse the existing uncommitted W1/W2 work, or rebuild?

**PE:** There are roughly two thousand lines of thoughtful, tested W1+W2 code sitting uncommitted on `feat/w2-rag-knowledge-base`. A production Bedrock client with retries and cost guards. A RAG pipeline with hybrid retrieval and an eval harness. Throwing that away to "start clean" costs us two weeks and buys nothing the client can see.

**SE:** I've read it. It's good work. It's also built on two foundations that the constraints rule out: the summary path calls raw `boto3.invoke_model` against the Anthropic-native body schema, and the vector store is a hand-rolled JSON file with an in-process BM25. We're mandated to ChatBedrockConverse and ChromaDB. Porting that isn't reuse — it's a rewrite wearing a reuse costume, and the costume is the dangerous part, because reviewers will assume the ported code was already reviewed.

**PE:** So you want a clean rebuild and a two-week slip.

**SE:** No. I want us to be precise about *what* we're reusing. The valuable part of that code isn't the plumbing — it's the judgement. The retry classification table, which errors are retryable and which fail fast. The pre-call budget guard shape. The distinction between `scrub_instructions` and `scrub_document`, which is a genuinely subtle call — dates and phone numbers are legitimate content in a policy document and PHI in a patient record. The eval-harness metric set. The seeded knowledge corpus. That's the expensive thinking. The transport layer underneath it is the cheap part and it's the part that has to change anyway.

**PE:** Then say it as a rule so we don't relitigate it per-file.

**SE:** Rule: **the prior work is a donor, not a base.** We branch from `main`, and we port deliberately, file by file, with a named donor list in the W1 ADR. Anything ported gets re-reviewed as new code. Nothing gets carried across because "it was already there."

**PE:** Accepted, with one addition — the donor list goes in the PR description too, so review effort lands where the risk is.

> **DECISION D1.** Branch from `origin/main`. Treat the uncommitted WIP as a **donor**: port the resilience-classification logic, the two-tier scrub design, the eval metric set, and the seed corpus; rebuild the Bedrock transport on `ChatBedrockConverse` and the index on Chroma. Donor list published in the W1 ADR and PR body. All ported code reviewed as new.
> **Cost accepted:** ~1–2 days of port effort we could have skipped by ignoring the constraints.
> **Also:** the user's working tree is left untouched — all work happens in a separate git worktree, so nothing they have in flight is at risk.

---

## D2 — Does LangGraph belong in Week 1 and Week 2, or only Weeks 3–4?

**PE:** The mandate is LangGraph v1.0. I read that as: use it. One runtime across all four weeks, one mental model, one thing to teach the client's team at handoff.

**SE:** Week 1 is a single request in, a single summary out. No branching, no durable state, no interrupts, no tool calls. Wrapping that in a `StateGraph` means inventing a state schema, compiling a graph, and threading a config dict — to express a straight line. That's cargo cult. It also makes the W1 diff twice as big and half as reviewable, and the client's team inherits a runtime they don't need to read a prompt.

**PE:** Here's my real worry, and it isn't ideology. If the first time we touch LangGraph is Week 3 — inside an *agent*, with tool calls and memory and a checkpointer all landing at once — then when the graph runtime does something surprising we're debugging four new things simultaneously, on the week the client is watching the front desk. I want the runtime proven on something boring first.

**SE:** That's a fair risk and it's a better argument than "the mandate says so." But the answer isn't to fake a graph in Week 1. The answer is to find the week where a graph is *genuinely* the right shape and put it there.

**PE:** Which is?

**SE:** Week 2. RAG is not a straight line — that's the standard misconception. Real retrieval is: retrieve → check whether anything relevant came back → **branch**: refuse, or generate → check the answer is grounded in what we retrieved → **branch**: serve, or refuse. Two conditional edges, and a refuse path that has to be as first-class as the answer path. That is exactly what `add_conditional_edges` is for, and it's the natural place to hang the Week 7 output guardrail later without reopening the design.

**PE:** So Week 1 is plain LangChain, Week 2 introduces LangGraph on a linear-with-branches graph, Week 3 is the agent, Week 4 is the custom graph.

**SE:** Yes. And note Week 1 still isn't off-mandate — LangChain v1 and LangGraph v1 are the same release train, and `ChatBedrockConverse` is the same model client the graphs will use. We're not introducing a second stack; we're declining to introduce a second *abstraction* before it earns its place.

**PE:** One condition. The Week 1 client has to be written so that Week 2 wraps it without touching it. If W2 has to reach in and change the W1 model client, we picked wrong.

**SE:** Agreed, and that's testable — W2's PR should show zero modifications to the W1 model-client module.

> **DECISION D2.** Escalating abstraction, justified per week:
> - **W1** — LangChain v1 only: `ChatBedrockConverse` behind a resilience wrapper. No graph.
> - **W2** — LangGraph v1 `StateGraph` with two conditional edges (relevance gate, grounding gate). The runtime is proven on a low-stakes path.
> - **W3** — LangChain `create_agent` (which compiles to LangGraph) + checkpointer.
> - **W4** — hand-built `StateGraph` with `Send` fan-out.
> **Constraint accepted:** W2's PR must not modify the W1 model client. If it does, the W1 boundary was wrong and we fix it there.

---

## D3 — ChromaDB, or pgvector in the Postgres we already run?

**SE:** Objection on the record. Postgres is already in this stack, already backed up, already in the runbook. `pgvector` would put the vectors next to the relational data they're derived from, in one transaction boundary, with one restore procedure. Chroma adds a container, a volume, a health check, a version-skew surface, and a second thing to be down at 2 a.m.

**PE:** The mandate is Chroma, and it's not arbitrary — prior research for this programme already landed on it, and it's what the client's team will see in the curriculum they're being trained against. Handing them pgvector because we prefer it means the handoff docs don't match anything they've learned.

**SE:** I'll take the mandate. I won't take it silently. Two conditions.

**PE:** Go.

**SE:** First: the vector store sits behind our own narrow port — add, query, delete, count. `langchain_chroma.Chroma` is the adapter, not the interface our code talks to. If Chroma becomes a liability in production, swapping to pgvector is one file, not a refactor. Second: the persistence and backup story goes in the runbook **in this engagement**, not discovered during an incident. Chroma in client/server mode is a container with a volume; that volume needs a documented backup and restore, and the collection needs to be rebuildable from source documents if the volume is lost. If we can't write that page, we shouldn't ship the dependency.

**PE:** Both accepted. And write the pgvector alternative into the ADR as a costed, named fallback — not as a grumble, as an option the client can exercise.

> **DECISION D3.** ChromaDB via `langchain-chroma`, `PersistentClient` in dev/CI and `HttpClient` against a compose service for production — **behind a local `KnowledgeIndex` port** so the adapter is replaceable in one file. Runbook must document volume backup/restore and full rebuild-from-source. pgvector recorded in the ADR as a costed fallback.

---

## D4 — Week 4 is "multi-agent + knowledge graphs." The client asked for a read view. Does the ask justify the architecture?

**SE:** Let me state the objection plainly, because it's the one I'd raise loudest in review. "Let patients see their own labs and visit summaries in one place" is a JOIN. Building a multi-agent system to render a read view is résumé-driven development. And it's worse than merely wasteful here — the client's own artifacts show `GET /patients/{id}/records` has no ownership check and sequential integer IDs. A system whose whole job is to *assemble more data about a patient from more sources* is an IDOR amplifier. We'd be shipping a force multiplier for the exact vulnerability we're supposed to be finding.

**PE:** I don't disagree with any of that, and I'm not going to argue "the curriculum says week 4 is multi-agent." That's the worst possible reason. But I think you're describing the naive version and calling it the only version.

**SE:** Then describe the non-naive one.

**PE:** Look at what "the full picture" actually spans in this system. Demographics live in `records-service`. Clinical encounters live in `records-service` but under different access semantics. Labs arrive over the hospital HL7 feed through `interop-service` — which we already know silently drops segments. Coverage context comes from `eligibility-service`, which is a live external call to a payer that goes down. Those are four different systems, with four different authorization rules, four different failure modes, and four different retrieval strategies — SQL, graph traversal, vector search, and a live network call to a third party.

**SE:** Go on.

**PE:** Assembling that serially means the payer being slow makes the labs slow. Assembling it in one agent with eight tools means one model context holding every domain's rules and making a worse choice about each. Assembling it as parallel domain retrievers, each carrying its own scope check and its own degradation behaviour, means one domain can fail and the patient still sees the other three, correctly scoped. That's not a JOIN. That's a fan-out with per-branch failure isolation and per-branch authorization.

**SE:** That's a real argument. It's also not an argument for *agents* — it's an argument for concurrency. I can write that with `asyncio.gather` and no model calls at all.

**PE:** For the retrieval, yes. Not for the synthesis. The thing the client actually wants — and the thing a JOIN cannot do — is a *coherent narrative* across those four domains: "here's what happened at your visits, here's what your labs said, here's what your coverage looked like," in language a patient reads. The retrieval is deterministic. The synthesis is the model's job. And per-domain retrievers with their own prompts and their own context are how you keep the synthesis from being fed a soup of four schemas.

**SE:** Then we agree the honest architecture is: **deterministic fan-out, deterministic authorization, model only at synthesis and only over already-authorized material.** Say that out loud, because it inverts your objection into the headline. The reason this is defensible as multi-agent is precisely that the agents *never* decide who may see what.

**PE:** Yes. Authorization is a graph edge, not a prompt.

**SE:** Then I withdraw the objection, with three guardrails. One: no `langgraph-supervisor`. It's pinned at 0.0.31, last released 2025-11-19, before LangGraph 1.0 shipped, and there's been no release since. Depending on a 0.0.x package that predates the runtime's GA is not a production posture, and the supervisor shape is about forty lines of `StateGraph` anyway. Two: the authorization check runs **before** fan-out and each branch is bound to the authorized scope — a branch physically cannot widen it. Three: we keep a failing test that reproduces the IDOR against the old path, permanently, so a future refactor that removes the check fails CI loudly.

**PE:** Accepted. And I'll expand the client's ask in the writeup rather than pretending they asked for this: "assemble the full picture" becomes "assemble the full picture safely, from four systems, degrading per-domain." That's a bigger win than a JOIN, and it's honest about being an expansion.

**SE:** One more. LangChain's own multi-agent guidance opens by saying not every complex task requires the approach, and their published cost table shows the subagents pattern costs an extra model call per request versus router/handoffs. We should cite that in the ADR — an architecture decision that quotes the vendor's own argument against it is a decision that survived scrutiny.

> **DECISION D4.** Week 4 uses the **Router pattern with `Send` fan-out**, hand-built on `StateGraph`. Four domain retrievers (demographics, encounters, labs, coverage) run in parallel with per-branch failure isolation; a **deterministic authorization gate runs before fan-out** and binds each branch to the authorized scope; the model appears **only at the synthesis node**, over already-authorized material.
> **Rejected:** `langgraph-supervisor` (0.0.31, 2025-11-19, pre-GA, unmaintained). **Rejected:** single agent with eight tools (context dilution, per-domain rules collapse). **Rejected:** plain `asyncio.gather` with no model (loses the cross-domain narrative that is the client's actual ask).
> **Non-negotiable:** agents never make authorization decisions. Permanent IDOR regression test.

---

## D5 — Where does human-in-the-loop actually belong?

**PE:** Requirement is that we have HITL. My instinct is to gate anything that touches PHI.

**SE:** Then the front desk will register patients in a spreadsheet by Thursday. A human gate on a high-volume path isn't a control, it's a workaround generator — that's how you get shadow IT, and shadow IT is unauditable by definition. HITL belongs where the volume is low and the blast radius is asymmetric.

**PE:** Give me the test for "asymmetric."

**SE:** One action, unbounded downstream consequence, and no cheap undo. Two places in W1–W4 pass that test.

First: **knowledge-base ingest** in Week 2. One bad or malicious document silently changes every future grounded answer the assistant gives, for every user, indefinitely. Nobody notices, because the answers still look confident and still carry citations — to the poisoned document. Volume is a handful of documents a month. Blast radius is every future answer. That's the definition.

Second: **cross-patient or disclosure-shaped assembly** in Week 4. The one request that must never auto-succeed is the one that looks like the IDOR we just found. Low volume, catastrophic if wrong, and there's no undoing a disclosure.

**PE:** What about Week 1 and Week 3? I don't want to tell the client "no human oversight on the AI feature."

**SE:** They get *automated* gates, which are strictly better on high-volume paths because they're consistent and they can't be tired at 4 p.m. — the grounding check and the budget guard in W1, the circuit breaker and the honest-degradation contract in W3. And both emit a `needs_review` signal into an asynchronous queue. A human reviews the flagged output *after* it was safely withheld, rather than standing in front of every request.

**PE:** So the story to the client is: humans approve what changes the system's future behaviour, machines gate what's high-volume and repetitive, and everything a machine withholds gets a human's eyes afterwards.

**SE:** That's a better story than "we gate everything," and it's the one we can defend in an audit.

> **DECISION D5.** HITL at exactly two synchronous points: **W2 knowledge ingest** (privileged capability + approval) and **W4 sensitive/cross-patient assembly** (LangGraph `interrupt()` → `Command(resume=…)`). W1 and W3 use automated gates plus a `needs_review` flag routed to an **asynchronous** review queue. Rationale recorded so the absence of HITL on high-volume paths is a decision, not an omission.

---

## D6 — How much compliance can we honestly build when we can't demo HIPAA?

**PE:** We can't sign a BAA and we can't spend money for this demo. Is the compliance work theatre?

**SE:** Only if we conflate architecture with attestation. Attestation needs a signature and a budget — that's true and we should say it plainly. Architecture is whether the control *exists and is exercised*. Those are separable, and keeping them separate is the entire value of the phrase "simple lift and shift." Right now that phrase is a claim. I want it to be a tested property.

**PE:** How do you test a control you can't turn on?

**SE:** Two tiers behind one interface. Tier 0 is always on, costs nothing, and runs in CI on every commit: the offline heuristic grounding check, the regex PHI scrub, structured audit events with no bodies, encrypted checkpoint serialization, the deterministic authorization gate, the budget guards. Tier 1 is the money-and-signature version of the *same* controls: Bedrock `ApplyGuardrail` with contextual grounding, real Titan embeddings, real Converse calls. Same call sites, same interfaces, selected by config.

**PE:** And you test Tier 1 how, without a key?

**SE:** With recorded response shapes and mocks at the boundary. The test doesn't prove AWS's guardrail works — that's AWS's job. It proves *our wiring* is correct: that `ApplyGuardrail` is invoked with `source=INPUT` before retrieval and `source=OUTPUT` before serving, that a `BLOCKED` verdict actually suppresses the response, that thresholds are read from config. Then "flip the switch" is genuinely config, and we can say so with a straight face.

**PE:** There's a limit I want stated, not buried. Contextual grounding explicitly doesn't support conversational chatbot use cases, per AWS's own doc. Week 3 is a chat assistant.

**SE:** Correct, and that's exactly why it must be in the ADR. W3 doesn't get a grounding score — it gets a deterministic check that the eligibility status it reports matches what the tool returned. Different control for a different shape. If we'd papered over that we'd have shipped a guardrail that silently doesn't apply.

**PE:** Then the compliance line to the client is: "every control is built and tested; two of them need your signature and your account to switch from the offline implementation to the AWS-managed one, and here is the exact config diff."

> **DECISION D6.** Two-tier control design behind one interface.
> **Tier 0 (always on, free, CI-tested):** offline grounding heuristic, regex PHI scrub, body-free structured audit events, `EncryptedSerializer` checkpoints, deterministic authz, token/cost budget guards.
> **Tier 1 (key/BAA-gated, same call sites):** Bedrock `ApplyGuardrail` + contextual grounding (summarization/QA paths **only** — AWS documents chatbot/conversational QA as unsupported), Titan embeddings, live Converse.
> Tier 1 wiring is mock-tested now so enabling it is configuration, not code. The config diff is documented.

---

## D7 — Do we fix the committed `.env` in Week 1, when the packet says Week 1 is discovery?

**PE:** The client packet is explicit that Week 1's deliverable is the safe client plus the *finding*. D9 is discovery material. Fixing it is scope creep, and worse, it's the kind of scope creep that teaches everyone the packet boundaries are soft.

**SE:** Normally I'd agree with you and I'd be the one saying it. Here's why this one is different: the very next thing that happens in this engagement is a live Bedrock API key going into that file. `.env` is currently tracked by git. If we leave it, we have knowingly handed someone a loaded gun and written a memo about gun safety.

**PE:** …That's a two-line fix.

**SE:** Two lines: untrack, and add to `.gitignore`. And one paragraph that matters more than the two lines — untracking does **not** un-leak. Anything already committed is in the history and on every clone. The obligation is rotation at the source. If we fix the tracking and don't say that, we've made the problem invisible instead of solved, which is strictly worse than leaving it visible.

**PE:** Accepted, and I want it logged as a deliberate deviation from the packet with the reason, so the next engineer doesn't cite it as precedent for "we fix debt whenever we notice it."

> **DECISION D7.** Fix D9's *tracking* in W1 — untrack `.env`, add to `.gitignore`, keep `.env.example` as the key-name reference. Recorded as a **deliberate, narrow deviation** from the packet, justified by the imminent live-key handover. The debt-log entry states explicitly that untracking is not rotation and that any previously committed credential must be rotated at source.

---

## D8 — Four sequential PRs: how do we keep PR 4 reviewable?

**SE:** If all four PRs target the integration branch and are cut from `main`, PR 4's diff contains weeks 1 through 4 and nobody reads it. If they're stacked as a chain of branches, every rebase after a review comment on PR 1 rewrites the other three.

**PE:** The client and the reviewers both want to see week-by-week value, in order.

**SE:** Then merge as we go. Each week's branch is cut from the integration branch *after* the previous week merged. PR N's diff is exactly week N's work. The integration branch is the cumulative artifact, and it's always in a state where the tests pass.

**PE:** That serializes us — we can't start W3 until W2 lands.

**SE:** We couldn't anyway. W3's agent calls the model client W1 builds and the graph patterns W2 establishes. The dependency is real, not procedural.

> **DECISION D8.** `feat/riverbend-w1-w4` is the integration branch. Each week's branch is cut from it after the prior week merges; each PR's diff is exactly one week. Integration branch stays green after every merge.

---

## D9 — One model or several?

**PE:** The board demo should feel smart. I'd like the strongest model on the summary path.

**SE:** Every additional model is a second price row in the cost guard, a second set of latency characteristics, a second failure mode under throttling, and a second thing to be wrong in the region we deploy to. Start with one, measure, split only with evidence.

**PE:** And when the demo feels flat?

**SE:** Then it's a config change, not a code change — so make the model ID overridable **per path** (summary, RAG generation, agent, synthesis) with one default. Nobody has to touch code to upgrade the demo, and if we never override it we never paid for the complexity.

**PE:** Fine. What's the operational trap?

**SE:** Model identifiers. Current Claude models on Bedrock are invoked through region-scoped inference profile IDs, not bare model IDs, and getting that wrong surfaces as a validation error at call time rather than at config time — which means it fails in the demo, not in CI. So: the ID is env-driven with a documented default, and the key-gated smoke test asserts we can actually resolve and invoke it before anyone stands in front of the board.

> **DECISION D9.** One default Bedrock model, overridable **per path** by env (`summary` / `rag` / `agent` / `synthesis`). Model IDs are region-scoped inference-profile IDs, documented, never hard-coded. The key-gated smoke test verifies resolution and invocation ahead of any demo.

---

## D10 — What does a test suite prove when there's no API key?

**PE:** I'll be blunt — mocked LLM tests prove nothing about whether the feature works. We're going to hand the client a green CI badge that says nothing.

**SE:** They prove nothing about the *model*, and they're not supposed to. We're not testing Anthropic's weights. Everything that can actually break in production here is around the model, not inside it: the timeout that isn't bounded, the retry that hammers a throttled endpoint, the parse that explodes on a malformed response, the budget guard that doesn't fire, the PHI that reaches a log, the authorization check that runs after retrieval instead of before. Every one of those is deterministically testable with zero spend, and every one of those is what actually takes a system down.

**PE:** That still leaves "does the summary read well" untested.

**SE:** Which is why there's a third tier. Three, explicitly:

Tier 1 — unit and contract tests, fully synthetic, no network. Timeouts, retry classification, parse fallbacks, budget refusal, scrub coverage, log-body assertions, breaker state machine, authz-before-fan-out, interrupt/resume.

Tier 2 — golden-set evaluation with a deterministic offline embedding backend. Real retrieval math, real recall/precision numbers, reproducible run to run. This is where the fragmentation metric lives, and it's the tier that produces a *number we hand the client*.

Tier 3 — live smoke tests, marked and skipped by default, each with a hard budget assertion and a tiny fixed corpus. Written **now**, so the day the key arrives it's one command, not a scramble.

**PE:** Tier 3 written now is the part I care about. If we write those tests on key-day we'll write them badly and we'll spend more than we meant to.

**SE:** Agreed — and each live test asserts its own cost ceiling, so an accidental loop can't run up a bill.

> **DECISION D10.** Three test tiers. **T1** synthetic unit/contract (default, CI). **T2** golden-set eval with deterministic offline embeddings (default, CI, produces client-facing numbers). **T3** `@pytest.mark.live` smoke tests — written up front, skipped without a key, each asserting a hard per-test cost ceiling.

---

## Standing tension to re-check each week

Both roles agreed to keep one question live and answer it in every PR description:

> **"What did we build this week that the client can see, and what did we find this week that the client could not have found?"**

If either half is empty, the week failed regardless of what merged.

And a second, aimed at ourselves:

> **"What did we add this week that we'd have to defend in a review as necessary rather than interesting?"**

The answer is allowed to be "nothing." It is not allowed to be unexamined.

---

# Part II — the UI phase

**Date:** 2026-07-29, after PRs #13–#16 merged
**Trigger:** the client asked why the demo was a Python script. The honest answer
was that four weeks shipped as gateway endpoints with **zero lines of
`frontend/`**, and a terminal script let that go unnamed.

The standing question above was answered wrongly four times. Every PR claimed
"what the client can see" and described an API. The bodies of #13–#16 have been
corrected in place rather than quietly left.

---

## UI-D1 — Folding mechanic: four new PRs, or rewrite history into four?

**PE:** The client said fold it into the four PRs. Four PRs means four PRs.

**SE:** Those four are merged. To literally get four, I have to reset the
integration branch, rebuild each week's commit with UI folded in, and force-push.
That discards the review history of #13 through #16.

**PE:** Which matters why? Nobody outside the team reads a merged PR.

**SE:** Because of what we have spent four weeks telling this client. We wrote a
finding saying their audit log cannot answer "who viewed this patient", and
another saying a claim they cannot evidence is worse than a control they are
missing. Then we would delete our own review trail to make a branch look tidier.
I would rather have eight PRs and be able to answer "who reviewed the
authorization gate, and when."

**PE:** Then the unit of review has to stay the week, or we have lost the thing
the client actually asked for — they want to see W1, W2, W3, W4, not "backend"
and "frontend."

**SE:** Agreed, and that is a naming problem, not a history problem.

> **DECISION UI-D1.** Four new PRs, `#17`–`#20`, each titled as its week's second
> half. The review unit stays the week. **Rejected:** force-pushing the
> integration branch into four commits.
> **Cost accepted:** eight PRs instead of four. Confirmed with the client.

---

## UI-D2 — How far does principal-awareness go?

**PE:** A patient logs in and lands on their own record. That is the Week-4 ask,
finally visible.

**SE:** That is a second application. Different navigation, different landing,
different empty states, different error copy. We could spend this entire phase on
routing and ship no screens.

**PE:** Or we ship the screens and patients see the front desk's navigation,
which is worse than useless — it is confusing and it exposes internal workflow.

**SE:** Then define the smallest thing that is not confusing. What actually
differs?

**PE:** Three things. Which nav items exist. What the landing page shows. Whether
there is a patient picker.

**SE:** That is a hook and two conditionals. I will take that. What I will not
take is a second layout, a second shell, or a route group split — those are the
things that double the surface and never come back.

> **DECISION UI-D2.** One shell. One `usePrincipal()` hook reading `patient_id`
> off the session, returning `{ kind, patientId, canIngest }`. It drives nav
> visibility, the landing view, and whether a picker renders. **Rejected:** a
> separate patient layout or App Router route group.

---

## UI-D3 — The dashboard defect: fix now or fix in W4?

**SE:** `app/page.tsx` hard-codes `DEFAULT_PATIENT_ID = "1042"`. After #16, the
seeded account `james.obrien` is bound to chart 1043, so his dashboard now fetches
1042, gets a 404, and renders empty. `maria.gonzalez` works by coincidence.

**PE:** So one of two demo accounts is broken. That is W4's screen work.

**SE:** It is not a screen gap, it is a regression we shipped. We changed a
backend contract and left a seeded account non-functional. Stacking three more
PRs on top of that is exactly the habit we criticised the contractor for.

**PE:** Fixing it properly means the principal work, which is W4.

**SE:** Fixing it *properly* does. Fixing it *correctly* means reading the
patient id from the session instead of a constant — four lines. The full
principal-aware shell can still be W4.

> **DECISION UI-D3.** `#17` carries the minimal correction: the dashboard reads
> the session's `patient_id`, and staff get a picker. `#20` does the full
> principal-aware shell. The branch is never knowingly broken between PRs.

---

## UI-D4 — What the patient is told about their own fragmentation

This was the longest argument and it is not really an engineering one.

**PE:** Maria's record spans three charts. The assembled view says so. That is
transparency and it is the finding made real.

**SE:** Careful. There is a difference between "this record brings together three
charts" and "one of your charts records a penicillin allergy that the other two
do not." The second sentence is a clinical communication, delivered by a web page,
to a patient, with no clinician present.

**PE:** She has a right to her own record. Withholding it is paternalistic.

**SE:** I am not proposing withholding anything — every record we are authorised
to show, we show. I am saying we should not *editorialise* about what the
discrepancy means. "Chart 1042 is missing your allergy" is an interpretation, and
if it is wrong, or right in a way she does not have context for, we have caused
harm from a UI string.

**PE:** So where is the line?

**SE:** Show the fact, not the inference. "This record brings together 3 charts we
believe belong to the same person" is a fact about our system. "Your allergy is
missing from two of them" is a clinical claim.

**PE:** Then the client has to own the wording.

**SE:** Yes — and that is the right outcome. We are not qualified to write it and
we should say so rather than quietly picking something.

> **DECISION UI-D4.** The assembled view states the neutral system fact only:
> *"This record brings together N charts that appear to be the same person."*
> It does **not** tell the patient which chart is missing what. The exact wording
> is flagged to the client as theirs to approve, in the PR and the finding.
> The full discrepancy detail remains visible to **staff**, where a clinician can
> act on it.

---

## UI-D5 — The free-text patient ID box

**SE:** `records/page.tsx` has an input where you type a patient number and press
Load. That is the IDOR's user interface. The backend now refuses unauthorised
ids, so it is no longer a vulnerability — but it is still an affordance that
teaches ID-guessing as a workflow.

**PE:** Staff genuinely need to look up patients. Removing lookup is not an option.

**SE:** Lookup is not the problem. *Lookup by guessing a sequential integer* is
the problem. There is already a `GET /patients?q=` endpoint doing name search, and
the portal already proxies it.

**PE:** So it is a better tool anyway.

**SE:** It is. But I want this stated precisely in the PR, because it is easy to
oversell: **replacing the box is UX hardening, not a security fix.** The gate is
the gate. If someone later re-adds an ID field, nothing becomes insecure — it just
becomes ugly again.

> **DECISION UI-D5.** Replace the free-text id input with a name-search picker
> backed by the existing `/patients?q=` route. Documented as UX hardening, not a
> control. The two stale comments claiming "the backend performs no ownership
> check" are corrected in `#20`.

---

## UI-D6 — How much of the eval report becomes a screen?

**PE:** All of it. It is the strongest thing we found.

**SE:** The report has four standard metrics, six integrity numbers, per-case
detail and three warnings. Rendered flat, that is a wall of figures and the
finding drowns in it. The reason the terminal version lands is that it is *read
aloud in a specific order*.

**PE:** Then keep the order.

**SE:** More than that — the finding is a *juxtaposition*. Recall 1.0 next to
fragment coverage 0.556. If those two numbers are not adjacent and the same size,
we have rendered a dashboard instead of an argument.

> **DECISION UI-D6.** `/knowledge/quality` leads with the two numbers side by
> side, same size, adjacent. Then the identity-split table. Then the
> clinically-incomplete case. Everything else collapses behind "full report".
> Progressive disclosure with the argument on top, not a metric grid.

---

## UI-D7 — What each test tier is allowed to prove

**PE:** The Playwright journey should cover the withheld-summary case. It is the
most important state in Week 1.

**SE:** It cannot, honestly. The stub model is deliberately grounded — it derives
its answer from the source text — so it will not produce an ungrounded summary to
be withheld. To force it in a browser I would have to set the grounding threshold
to something impossible via a compose override, and then the journey proves
nothing about the guardrail. It proves I can misconfigure a threshold.

**PE:** So the state goes untested?

**SE:** No. It goes tested at the tier that can actually test it. Given a withheld
API response, does the panel render the safe message and the review flag rather
than raw model text? That is a component test, it is deterministic, and it runs in
CI in milliseconds. The guardrail *logic* already has its own Python test against
the real hallucination transcript.

**PE:** And Playwright covers?

**SE:** The journeys — that a real browser, against the real stack, can complete
each week's task. Not every state. Contorting an E2E to reach a state it cannot
naturally produce is how suites become slow and false.

> **DECISION UI-D7.** Vitest covers **states** (withheld, stale, refused,
> unavailable, denied). Playwright covers **journeys** (one per week, happy path,
> against the running stack). Neither is asked to do the other's job, and ADR 0013
> records what each does *not* prove.

---

## UI-D8 — Can the Playwright gate be as strong as the `--live` gate?

**PE:** Same pattern as the backend — make it impossible to run accidentally.

**SE:** I will not claim equivalence, because it is not true. The `--live` gate
protects against *spending money*, and a pytest collection hook survives a `-m`
override. Playwright here spends nothing — the stack runs in stub mode. The only
cost of running it accidentally is time.

**PE:** So a weaker gate is fine?

**SE:** A weaker gate is *appropriate*. What matters instead is the failure mode:
a developer who runs it without the stack up should get "run `make up` first",
not twenty mystery timeouts.

> **DECISION UI-D8.** Playwright lives in a separate npm script, excluded from
> the default CI job. **Not** claimed as equivalent to the `--live` gate — the
> risk is time, not money, and the ADR says so. A `globalSetup` probe fails fast
> with an actionable message when the stack is down.

---

## Standing question, restated for this phase

The original still applies, with one addition forced by how we got here:

> **"Can a person do this thing in a browser, and did we watch them do it?"**

An endpoint that returns the right JSON is not a delivered feature. We answered
that question wrongly four times, and the corrections are in the PR bodies.

---

# Part III — ingestion, and designing a UI for an agent

**Date:** 2026-07-29, after `#17` merged and the stack was started for the first time
**Trigger:** two client instructions. Employees must be able to **upload
documents**, not paste text. And the UI should be shaped around **how the agents
actually operate**, rather than being a CRUD skin that happens to call them.

The first looked like a file-input control. It is not. The second looked like
polish. It is not either.

---

## UI-D9 — "Employees can upload documents." What is actually being asked for?

**PE:** The client's words. Front desk has a folder of clinic PDFs — fasting
instructions, the new cancellation policy, payer procedure notes. They should
drag them in and have the assistant know them. Today `/ingest` takes a JSON
`text` field, which means the only way to add a document is to open it, select
all, copy, and paste into a textarea. Nobody will do that twice.

**SE:** Agreed on the problem. I want to be precise about what we are opening,
because a file input in a HIPAA system is not a widget.

Right now `/ingest` accepts a string that has already passed through a browser,
a JSON encoder, and Pydantic's `max_length=200_000`. A file upload replaces that
with an arbitrary binary from an arbitrary desktop, parsed by a third-party
library, inside the service that holds the vector store. PDF parsers are a
historically rich source of memory-safety bugs. That is a new attack surface in
the one service that can rewrite what the assistant believes.

**PE:** So we validate. Every product ships file upload.

**SE:** Every product ships file upload with a threat model. Mine has four
entries and I want all four answered before we write the control:

1. **Parser reachability.** What formats, and does the parser run in-process?
2. **Resource exhaustion.** A 4KB zip bomb or a PDF with 60,000 pages.
3. **Content trust.** The parsed text goes straight into the assistant's beliefs.
4. **PHI.** This is the one that actually decides the design — see UI-D10.

**PE:** Then let me push back on scope, because "answer all four" can mean six
weeks. What is the smallest honest version?

**SE:** Formats first. PDF, DOCX, TXT, MD covers the folder you described. I want
to argue us *down* to three: **PDF, TXT, MD**, and defer DOCX.

**PE:** Why? DOCX is what a policy is actually written in.

**SE:** Because DOCX is a zip archive. Adding it means adding zip handling, which
means adding the zip-bomb and path-traversal cases, on the same PR as everything
else here. It is not that we cannot do it — it is that it belongs in its own
change with its own tests. I would rather ship three formats that are genuinely
tested than four where one is the reason the review is shallow.

**PE:** I will take that if the UI names the gap instead of silently rejecting.
An employee dropping a `.docx` should be told "DOCX support is coming; save as
PDF for now", not handed "unsupported file type."

**SE:** Fair, and cheap.

**PE:** And the caps have to be visible before the drop, not after. A limit you
discover by hitting it is a bug report.

**SE:** Agreed. Stated on the control, enforced at the gateway, enforced again in
the service. The UI stating a limit is a courtesy; it is never the check.

> **DECISION UI-D9.** Add `POST /ai/knowledge/upload` — multipart, one file per
> request. **Accepted formats: PDF, TXT, MD.** Explicit caps: **10 MB**,
> **80 pages**, **200,000 extracted characters** (the existing `IngestRequest`
> ceiling, so the upload path cannot smuggle a document the paste path would
> reject). Extraction runs with `pypdf` in-process, wrapped so a parser failure
> is a 422 rather than a 500.
> **Rejected:** DOCX in this PR — deferred with a named UI message, tracked as
> debt, because zip handling deserves its own review.
> **Rejected:** parsing client-side in the browser — it would put the only copy
> of the extraction logic somewhere we cannot audit, and the server would have to
> trust text a client claims came from a PDF.

---

## UI-D10 — The upload is a PHI disclosure path, and the existing preview model does not cover it

**SE:** This is the one I want to stop on, because I think we were about to ship
a real breach.

Look at what the knowledge collection actually is. `/ai/knowledge/query` is
gated by `require_session` and nothing else:

```python
@app.post("/ai/knowledge/query")
def proxy_kb_query(payload: dict, session: dict = Depends(require_session)):
    return _post("ai", "/query", payload)
```

Since `#16`, patients have sessions. So the knowledge collection is readable —
through the assistant's answers — by **every authenticated user, including every
patient**. It is the one collection with no scope filter, by design, because
clinic policy should be answerable to anyone who asks.

**PE:** Right, that is the feature.

**SE:** Now add upload. A front-desk employee drags in "Payer procedure notes",
which happens to contain a worked example naming a real patient and their
condition. We chunk it, embed it, and file it in the collection with no scope
filter. Two days later a different patient asks the assistant a coverage
question, and the retrieval surfaces that chunk — **with a citation**, because
our grounding pipeline is good at what it does.

That is an impermissible disclosure under 164.502(a), and our own audit log
records it as a successful grounded answer.

**PE:** We scrub on ingest. `scrub_document` already runs.

**SE:** It runs, and it is deliberately **lenient**. Read its own docstring — it
is the policy-document scrub, tuned to *keep* effective dates and the clinic's
phone number, because a policy stripped of those is useless. It is the correct
scrub for a policy. It is not a Safe Harbor scrub, and `safe_harbor_scrub` in the
same module raises on purpose because we have not built it.

So today's ingest quietly assumes the human pasting text has already read it.
With paste, that assumption is nearly true — you cannot paste 40 pages without
seeing them. With upload, the assumption is false the first time somebody drags
in a file they have not opened.

**PE:** So the fix is a better scrubber.

**SE:** The fix is **not pretending a regex is a compliance control**. We wrote
that finding for this client already. A stronger scrubber is worth having and it
still will not catch a patient named in prose.

**PE:** Then what? If I put a modal in front of every upload saying "are you
sure", people click through it. Confirmation dialogs that appear every time are
furniture.

**SE:** Which is why it must not be a confirmation dialog. It has to be a
**preview of the actual consequence**, and it has to be different every time so
it cannot be muscle-memoried.

Two-phase. Phase one, `POST /ai/knowledge/upload`, extracts and scrubs and
returns a **staged** document: the extracted text as it will be indexed, every
redaction the scrubber made, marked in place, and the chunk count. Nothing is
written to the index. Phase two, `POST /ai/knowledge/upload/{staging_id}/commit`,
is the only thing that writes.

**PE:** I like it, and I want to sharpen the copy. The question on that screen
is not "confirm?" It is *"this text will be readable by every patient who asks
the assistant a related question."* Say the consequence, not the action.

**SE:** Yes. And I want the redaction list shown as **reassurance and warning at
once**: "we removed 3 things that looked like identifiers" tells the uploader the
scrubber is real, and it tells them the scrubber found identifiers in a document
they were about to publish — which is exactly when they should read it again.

**PE:** What is the staging lifetime? If it is a database table I have opinions
about cleanup.

**SE:** Redis, TTL 30 minutes, keyed by a random id and **bound to the uploading
username**. Two reasons for the binding. It stops one employee committing another
employee's staged document, and it keeps `added_by` provenance honest — the
gateway already stamps that server-side and a staging handoff must not become the
gap where a client gets to choose it.

**PE:** Then the HITL story gets better too. The client asked for human-in-the-
loop. Right now our only HITL is the W4 sensitivity gate, which is genuinely a
gate but fires rarely. This one fires on every single knowledge write, which is
the highest-blast-radius write in the system.

**SE:** Agreed, and it is a better example for the deck than the one we had.

> **DECISION UI-D10.** Upload is **two-phase**: stage → preview → commit. Phase
> one never writes to the index. The preview shows the exact text to be indexed,
> in-place redaction markers, the redaction count and kinds, and chunk count.
> Staging lives in Redis, **TTL 30 minutes, bound to the uploading username**;
> commit by anyone else is 403 (not 404 — the uploader is allowed to know their
> own staging id is valid).
> The confirm copy states the **consequence** — readable by every authenticated
> user including patients — not the action.
> **Rejected:** single-shot upload-and-index. **Rejected:** a generic "are you
> sure" modal. **Rejected:** treating `scrub_document` as sufficient without a
> human read; it is a lenient policy scrub and `safe_harbor_scrub` still raises.
> **Recorded as a limitation for the client:** this is a *procedural* control
> backed by a lenient automated one. It is not de-identification.

---

## UI-D11 — What changes in a UI because an agent is behind it, not an API?

**PE:** The client's second instruction. I want to be careful not to answer it
with vibes. What concretely changes?

**SE:** Start from the failure modes, because that is where agent-backed and
CRUD-backed genuinely diverge. A CRUD endpoint has two outcomes: it worked, or
it errored. Ours has at least six, and five of them return HTTP 200:

| Outcome | HTTP | What the user must understand |
|---|---|---|
| Grounded answer | 200 | Normal |
| **Refused** — retrieval found nothing in scope | 200 | The system worked; the answer does not exist here |
| **Withheld** — guardrail blocked the output | 200 | We produced something and chose not to show it |
| **Ungrounded** — generated but grounding score below threshold | 200 | Do not act on this |
| **Overridden** — the agent's prose contradicted the tool, tool won | 200 | The number is authoritative, the sentence was wrong |
| **Stale** — served from a last-known value, payer unreachable | 200 | Correct as of a time, not as of now |
| Transport / service down | 5xx | Broken |

**PE:** And in a normal product every one of the middle five renders as either a
spinner that ends, or a red box. That is the actual insight — I would have built
"answer or error" and been wrong five times.

**SE:** Which produces a specific failure I care about more than any of them:
**a refusal that looks like a bug gets retried**, and a refusal that looks like an
empty answer gets treated as "no allergies on file." That second one is the
Week-2 finding restated as a UI defect. The gold-set case that started this whole
engagement was an assistant confidently saying "No known allergies on file" about
a patient with a penicillin allergy.

**PE:** So rule one: **a refusal is a first-class answer with its own visual
treatment.** Not an error, not empty state, and never rendered as silence.

**SE:** Rule two follows from the same place. **Provenance is not a detail
view.** Every grounded answer shows what it was grounded on, inline, before the
user has to ask. If the citation is one click away, the citation does not exist.

**PE:** Rule three is mine and it is about time. These calls are seconds, not
milliseconds, and a multi-agent view fans out across four domains. A spinner for
six seconds reads as broken.

**SE:** The graph already returns `path` — the node sequence it executed. We log
it. Surfacing it turns dead time into an explanation: *authorize → assemble →
summarise*. It is honest, it is free, and it is the single best asset in the
demo, because it is the only place where "multi-agent" stops being a word on a
slide.

**PE:** Rule four: **when a human gate fires, it has to look like a decision, not
a delay.** The W4 sensitivity interrupt currently pauses a graph run. If the UI
renders that as a spinner, we have built a hang.

**SE:** And rule five, which is the one nobody asks for: **the people who feed
the corpus have to see its quality.** The eval report is currently terminal
output read by us. The person who can actually fix a 0.556 fragment coverage is
the front-desk lead who knows Maria has three charts — and they will never run
`pytest`.

**PE:** That reframes the quality dashboard. I had it as a credibility artifact
for the client. You are saying it is an operational tool for staff.

**SE:** Both, and if it is only the first we should not build it.

> **DECISION UI-D11.** Five workflow rules, applied across every agent-backed
> screen and recorded in `adr/0015`:
> 1. **Refusal is a first-class answer.** Distinct treatment, never an error,
>    never empty. Absence is stated ("no allergy recorded at this encounter"),
>    never implied by blank space.
> 2. **Provenance is inline.** Citations render with the answer, not behind a
>    disclosure.
> 3. **Show the path, not a spinner.** The graph's `path` is the progress
>    indicator.
> 4. **A human gate renders as a decision**, with the stakes named and both
>    outcomes explicit.
> 5. **Corpus quality is a staff screen**, not a report we read.
> **Rejected:** a generic loading spinner on agent calls. **Rejected:** citations
> behind a "sources" toggle. **Rejected:** rendering refusals through the error
> component, which is what every one of these screens would have done by default.

---

## UI-D12 — Does the quality dashboard show the client a number that makes us look bad?

**PE:** Blunt version: the eval reports context recall `1.0` and fragment
coverage `0.556`. If we put that on a screen the client sees a failing grade.

**SE:** They see a true number. The alternative is that we know it and they do
not.

**PE:** I am not arguing for hiding it. I am arguing that `0.556` with no frame
is worse than useless — it reads as "the AI is 55% correct", which is not what it
means. It means the *data they gave us* splits one person across three charts,
and the retrieval is doing exactly what it should.

**SE:** Then the fix is the framing, not the number. Put the two side by side and
label them for what they are: retrieval is working, the record is fragmented.

**PE:** And name the consequence in a sentence a non-engineer can repeat in a
meeting. "One patient, three charts. An assistant that answers from one chart
gives a clinically incomplete answer." That sentence is the whole engagement.

**SE:** I will add one requirement. The three-chart split renders as **actual
patient rows with actual chart ids**, not as a percentage. `0.556` is arguable.
"Maria Gonzalez — charts 1042, 1330, 1588" is not.

**PE:** Agreed. And the `clinically_incomplete_answers` count gets its own
treatment — that is not a metric, that is a list of times the system would have
told someone the wrong thing.

> **DECISION UI-D12.** The quality screen leads with the **contrast**: retrieval
> metrics beside integrity metrics, explicitly labelled so a reader cannot
> mistake a data problem for a model problem. Fragmentation renders as named
> patients and chart ids, not only a rate. `clinically_incomplete_answers` gets a
> severity treatment.
> **Rejected:** omitting or rounding the number. **Rejected:** showing it raw
> without the frame — an unframed true number that reliably causes a false
> conclusion is not honesty, it is abdication.

---

## UI-D13 — The approvals queue we cut

**PE:** `/approvals` was in the plan for `#20` and I cut it, because there is no
backend endpoint listing paused runs. I want to reopen that, because the client
asked for HITL and a gate nobody can find is not a gate.

**SE:** The cut was correct at the time and the reasoning still holds: I will not
invent a list endpoint in a UI PR. But the conclusion — no queue — is wrong.

**PE:** So we add the endpoint.

**SE:** With a caveat I want written down, because it is the kind of thing that
gets sold as more than it is. Our checkpointer in this configuration is
`InMemorySaver` unless `agent_durable_memory` is set. A paused run lives in one
process's memory. An approvals queue backed by that is a queue that empties on
restart.

**PE:** That is not a queue, that is a session.

**SE:** Correct. So either we ship it honestly labelled, or we make the
checkpointer durable first.

**PE:** What does durable cost?

**SE:** `SqliteSaver` behind the existing flag is small. Making it *encrypted* —
which is what a paused run holding assembled PHI actually requires under
164.312(a)(2)(iv) — is `EncryptedSerializer`, which we already researched and
already reference in `adr/0009`. The work is wiring and tests, not discovery.

**PE:** Then do that, and the queue is real.

**SE:** One more thing and then I am satisfied. The queue must show **what is
being approved**, not just that something is. "Approve run `view-a3f9`" is a
button that gets clicked. "Release a record assembled across 3 charts for Maria
Gonzalez, including a sensitive-flagged encounter" is a decision.

> **DECISION UI-D13.** `/approvals` is **restored**, and gains the backend it
> needed: `GET /ai/approvals` listing runs paused at the sensitivity gate. The
> checkpointer moves to the durable, encrypted path so the queue survives a
> restart. Each row states the patient, the chart span, and why the gate fired.
> **Rejected:** a queue over `InMemorySaver` — it looks like a control and is a
> session. **Rejected:** deferring HITL surfacing to a later week; the client
> asked for it and we have exactly one gate to show.

---

## UI-D14 — Does `can_ingest` gate the control, or the page?

**PE:** Small one, but I have seen it done wrong. `/me` returns `can_ingest`. Do
we hide the whole knowledge admin page from staff who lack it, or show the page
and disable the upload?

**SE:** Show the page, hide the write control. Two reasons. Query and eval are
open to any authenticated user by design, so the page has content for everyone.
And a page that vanishes teaches people the feature does not exist, so they ask
for it to be built rather than asking for access.

**PE:** Agreed, with the copy requirement: the absent control is replaced by a
line saying who to ask, not by nothing.

**SE:** And the standing rule holds — the UI hiding a control is cosmetic. The
gateway's `require_ingest` is the check, and there is a test that posts an
upload as a non-privileged session and expects 403.

> **DECISION UI-D14.** `can_ingest` hides the **write control**, not the page,
> and is replaced by a line naming who grants access. Enforcement stays at the
> gateway; the UI hint is never the check, and a test pins the 403.

---

## Standing question, Part III

> **Before any of these merges: can a person do this in a browser, and did we
> watch them do it?**

Answered wrongly five times now — four backend PR bodies and `#17`. `#17`'s body
carries the retraction. The stack is up, the journeys run, and the answer for
every PR in this part must be a pasted test result, not an intention.


---

# Part IV — the front desk, the patient, and the approval

**Date:** 2026-07-29, after `#19` merged
**Scope:** the W3 and W4 screens, and the workflow shifts each forces.

Part III set five rules for agent-backed UI in the abstract. This part is where
they meet two specific surfaces, and both turn out to need a decision the rules
do not settle on their own.

---

## UI-D15 — Coverage staleness: how loud, and whose problem is it?

**PE:** The chip has four states — active, inactive, pending, unknown — plus
stale. I want stale to read as a small timestamp. "Active · as of 9:02am". It is
information, not an alarm, and front desk sees this control fifty times a shift.

**SE:** I want it loud, and I want to argue from what stale actually means rather
than from how often it appears.

`stale: true` means the circuit breaker was open or the payer timed out, so what
we are showing is **the last value we successfully retrieved**. We do not know
the current value. The patient standing at the desk may have lost coverage
yesterday.

**PE:** And if we make it a red banner, by week two everyone has learned to
ignore red banners. That is worse than a timestamp, because then stale is
invisible *and* we have trained people to skip warnings.

**SE:** That is a fair mechanism and I accept it. But the timestamp alone fails a
different way: `9:02am` next to `Active` reads as "verified at 9:02", which is
the opposite of what happened. It was verified at 9:02 *and has not been
verifiable since*. The words have to carry that, not the styling.

**PE:** So the fix is the copy, not the colour.

**SE:** Yes. "Active — last confirmed 9:02am, payer unreachable since." Two
facts, no alarm. And the word "unreachable" is the one that has to be there,
because it is the difference between a stale value and an old one.

**PE:** Then I want one more thing: the chip must never render a bare status when
it is stale. If the payer is unreachable and we show "Active" with no
qualification anywhere, we have asserted something we do not know.

**SE:** Agreed, and that is the testable version. A stale chip without the
qualifier is a defect, not a style choice.

> **DECISION UI-D15.** `CoverageChip` renders the status **plus** an explicit
> unreachable-since clause whenever `stale` is true. Tone stays neutral — no red,
> no icon-shouting — because a warning shown fifty times a shift is a warning
> nobody reads. **The load is carried by the words, not the colour.** A stale chip
> that renders a bare status is a defect; pinned by a component test.
> **Rejected:** a timestamp alone ("as of 9:02am"), which reads as *verified at*
> rather than *not verifiable since*. **Rejected:** red-alert styling.

---

## UI-D16 — Do we tell the user the assistant was wrong?

**PE:** This is the one I have been avoiding. When `overridden: true`, the agent
said something that contradicted the tool and we replaced it. Do we tell the
front desk that happened?

My instinct is no. It undermines confidence in a tool they have to use fifty
times a day, and the *outcome* is already correct — we showed them the tool's
number. Surfacing the mechanism only makes them distrust the whole thing.

**SE:** I think that instinct is exactly backwards, and I want to be careful
about why, because "always be transparent" is not the argument.

The argument is: they are going to notice anyway. Not every time — but once. One
day the reply reads slightly oddly against the chip beside it, and they will
either quietly stop trusting the assistant with no way to say why, or they will
escalate it as a bug. Both are worse than a one-line label.

**PE:** Or they never notice, and we have spent trust for nothing.

**SE:** Then consider the other direction. We built `check_consistency` because
we expected the model to occasionally assert a coverage status the tool did not
support. That override is the single most valuable safety control in Week 3 —
and if it fires silently, we cannot tell whether it fires *usefully*. Nobody
reports "the assistant was right in a way I did not see."

**PE:** So the argument is observability, not honesty.

**SE:** It is both, but observability is the one that survives scrutiny. A
control whose activations are invisible cannot be evaluated, and an unevaluated
safety control is how we end up claiming a protection we cannot evidence. We have
written that finding for this client twice.

**PE:** Fine — but I get the copy, and it is not going to say "the AI was wrong."
It says what happened in terms of what is authoritative. "Corrected from the
payer record." The payer is the authority; the assistant is a convenience. That
framing is true and it does not invite distrust, it explains the hierarchy.

**SE:** Better than mine. Take it.

> **DECISION UI-D16.** An override is **shown**, labelled *"Corrected from the
> payer record"* — framing the payer as authoritative rather than the assistant
> as wrong. Rationale is observability first: a safety control whose activations
> are invisible cannot be evaluated, and we will not claim a protection we cannot
> evidence.
> **Rejected:** silent override. **Rejected:** copy naming the assistant as
> mistaken.

---

## UI-D17 — Does the front desk see the circuit breaker?

**PE:** No. Absolutely not. "Circuit breaker half-open" means nothing to a
receptionist and it is our internal plumbing.

**SE:** I agree with the conclusion and want to reject the reasoning, because
"users do not need to know" is how we end up hiding things that matter.

The breaker state is genuinely not actionable *by them*. Whether the payer is
unreachable because of an open breaker or a timeout changes nothing about what
they should do — proceed with registration, mark coverage unverified. That is why
it stays hidden: not because it is internal, but because **no reading of it
changes their next action.**

**PE:** And the thing that *is* actionable is already on the chip.

**SE:** Right. But I want the distinction recorded, because it is the test we
should apply to every internal signal we are tempted to surface: does knowing it
change what this person does next? Staleness passes. Breaker state does not. The
graph path in `#19` passes, narrowly, because it changes whether they wait.

> **DECISION UI-D17.** Breaker state is **not** surfaced to the front desk. The
> test applied — and to be reused for future internal signals — is *"does knowing
> this change what this person does next?"* Staleness passes; breaker state does
> not. **Rejected:** the reasoning "it is internal"; that is not a sufficient
> reason on its own and would justify hiding staleness too.

---

## UI-D18 — Can a patient approve the sensitivity gate on their own record?

**SE:** codex F8 caught this and it is the sharpest finding in the set. The
existing resume route is guarded by `require_patient_access`:

```python
scope_mod.require_patient_access(require_scope(session), patient_id)
```

Maria has access to Maria's record. So Maria can approve the sensitivity gate on
Maria's record. The subject of the release is the approver.

**PE:** Which is... arguably fine? It is her record. Patients have a right of
access under 164.524.

**SE:** They do, and that is a genuinely good objection, so let me separate two
things the single word "approve" is hiding.

There is *"may this patient see their own record"* — yes, and the sensitivity
gate should not be what decides that. And there is *"should this assembled,
cross-chart, sensitivity-flagged view be released"* — which is a clinical and
compliance judgement about material that may include another person's
information, a provider's note about a third party, or a result whose delivery
has a protocol.

**PE:** So the gate is not asking the question I thought it was asking.

**SE:** It is asking the second one. And a subject approving that is not
human-in-the-loop; it is a rubber stamp with extra steps. Worse, it is a rubber
stamp that our audit log will record as an approval, which is the exact class of
unevidenced claim we keep correcting.

**PE:** Then the honest answer is that the gate never had an authorization model
and we did not notice because there was one kind of session.

**SE:** Correct. And I want to say plainly that this is the same shape as the
IDOR we fixed in `#18`: a check that was right about one question and silent
about another.

**PE:** Then I want the product consequence stated too, because "patients cannot
approve" must not become "patients cannot see their record." If the gate fires on
a patient's own view, the patient sees a clear *pending review* state — not an
error, not an empty record, and not a spinner.

**SE:** Yes. And that is the honest surface anyway: their record is not being
withheld, it is being reviewed, and they should be told which.

> **DECISION UI-D18.** A new `can_approve` capability, **staff-only**, surfaced on
> `/me` beside `can_ingest`. A patient can **never** approve a gate on their own
> record, even if they somehow hold the capability. When the gate fires on a
> patient's own view, the patient sees an explicit **pending review** state that
> distinguishes *being reviewed* from *withheld* and from *empty*.
> **Rejected:** treating record access as approval authority — it conflates
> 164.524 right-of-access with a release judgement about cross-chart,
> sensitivity-flagged material.

---

## UI-D19 — Partial assembly: what does a patient see when one domain fails?

**PE:** The view fans out across four domains — demographics, encounters, labs,
coverage. If coverage times out, what do they see?

**SE:** Not a failed page. That is the easy part and also where the default
implementation goes wrong: one rejected promise, whole screen error, and the
three domains that *did* load are thrown away.

**PE:** Agreed on that. My question is the opposite one — if coverage is missing,
do we render the page as if it were complete?

**SE:** No, and this is Part III rule 1 applied to a shape it did not anticipate.
An absent domain rendered as an empty section is indistinguishable from a domain
that legitimately has nothing in it. "No labs on file" and "labs could not be
loaded" are the same pixels and opposite facts — and one of them is the Week-2
allergy failure wearing a different hat.

**PE:** So per-domain state, not per-page.

**SE:** Per-domain, and the two absences are worded differently. That is the whole
requirement.

**PE:** One addition. If any domain failed, the page header has to say the view is
incomplete. Someone who scrolls to the section they care about will never see a
notice that lives three sections down.

**SE:** Accepted — a summary at the top, detail in place. Both, not either.

> **DECISION UI-D19.** Domains render **independently**. A failed domain shows a
> load failure distinct from a legitimately empty one — *"could not be loaded"*
> never renders as *"none on file"*. If any domain failed, the page states that
> the view is incomplete **at the top**, in addition to marking the domain in
> place.
> **Rejected:** whole-page error on one domain failure. **Rejected:** a single
> notice at the bottom, which a reader who jumps to their section never sees.

---

## UI-D20 — What identifies an approval?

**SE:** codex F9. Resume currently forwards a client-supplied `thread_id`:

```python
"thread_id": payload.get("thread_id", ""),
```

The gateway checks the path `patient_id` and then trusts the client for which run
to resume. Same shape as F1 — right about one input, silent about another.

**PE:** Is it exploitable? The GET builds the thread id server-side as
`view-{username}-{patient_id}`, so it is guessable but scoped to a patient the
caller already passed the access check on.

**SE:** Today, probably not exploitable — and I do not want to defend a boundary
on "probably not". The thread id is a *graph implementation detail* that has
become part of the client contract. That is the actual defect: the moment we
change the checkpointer's threading scheme, we change an API, and the moment a
queue lists other people's runs, the guess becomes a lookup.

**PE:** So an opaque id.

**SE:** Server-issued, bound to patient, requester, gate state and permitted
approver. The client gets a token it cannot construct and cannot reason about,
which is the right amount for it to know.

**PE:** And the queue row shows the human meaning, not the token. "Release a
record assembled across 3 charts for Maria Gonzalez, including a
sensitivity-flagged encounter" — not `view-a3f9`.

**SE:** Which is Part III rule 4. A button labelled with a run id gets clicked; a
sentence describing a release gets read.

> **DECISION UI-D20.** Resume takes an **opaque server-issued approval id**, bound
> to patient, requester, gate state and permitted approver. `thread_id` leaves the
> client contract entirely. Queue rows state the decision in domain terms; the id
> is never the label.
> **Rejected:** keeping `thread_id` as the client-facing handle — it makes a
> checkpointer implementation detail into an API, and turns a guess into a lookup
> the moment a queue exists.

---

## What Part IV changed about how the UI treats the agents

Three of these six decisions are the same underlying shift, and it is worth
naming because it is the client's actual question — *how should the UI change
because agents are behind it?*

**A CRUD UI reports outcomes. An agent UI has to report the provenance of
outcomes.** Staleness (UI-D15) is a coverage value plus where it came from.
Override (UI-D16) is a coverage value plus which of two sources won. Partial
assembly (UI-D19) is a record plus which parts of it are actually there. In each
case the value alone is true and misleading, and the qualifier is what makes it
usable.

The other three are authorization decisions that only became visible once
principals were plural: who may approve (UI-D18), what identifies the thing being
approved (UI-D20), and which internal state is worth surfacing at all (UI-D17).
All three were latent from the moment `#16` gave patients sessions, and none were
caught by tests that were individually correct.


---

# Part V — the thresholds were measuring the stub

**Date:** 2026-07-30, after the first live credential
**Trigger:** the client, using the portal as `maria.gonzalez`, could not get the
assistant to tell her about her own penicillin allergy.

Three defects, one root cause, and a guardrail that has to be redesigned rather
than retuned.

---

## UI-D21 — A config value that fails silently

**SE:** Smallest one first because it is not arguable. `RAG_EMBED_BACKEND=bedrock`
does nothing:

```python
if settings.embed_backend == "titan" and not settings.use_stub:
```

Anything that is not the literal string `titan` falls through to the offline
hashed bag-of-terms. No warning, no log line. I set `bedrock` — a name the AWS
docs use everywhere — and got identical retrieval scores to four decimal places,
which is the only reason I noticed.

**PE:** Is there an argument for the fallback? Degrading to something that works
rather than refusing to boot?

**SE:** There would be if the fallback were equivalent. It is not: one is a
semantic embedder and the other is lexical overlap, and the difference is whether
"what am I allergic to?" retrieves anything at all. Silently swapping a
production component for a dev stub on a typo is how you demo a system that is
not the system.

> **DECISION UI-D21.** An unrecognised `RAG_EMBED_BACKEND` **raises at startup**
> and names the accepted values. `offline` stays available and must be chosen
> explicitly. **Rejected:** silent fallback — the two backends are not
> interchangeable, and the one you get by accident is the weaker one.

---

## UI-D22 — Two floors set above the entire range of legitimate output

**PE:** The client's question refuses. Walk me through why.

**SE:** Six realistic patient questions, measured against real Titan embeddings
and the real model:

| Query | coverage | dense | grounding | invented claims |
|---|---|---|---|---|
| what am I allergic to? | 0.00 | 0.332 | 0.521 | none |
| do I have any drug allergies? | 0.33 | 0.292 | 0.176 | none |
| am I allergic to penicillin? | 0.33 | 0.483 | 0.342 | none |
| what medications am I taking? | 0.33 | 0.328 | 0.353 | none |
| when was my last visit? | 0.50 | 0.259 | 0.429 | none |
| what were my lab results? | 0.50 | 0.389 | 0.462 | none |
| **configured floor** | **0.30** | **0.55** | **0.55** | |

Dense tops out at 0.483 against a 0.55 floor. Grounding tops out at 0.521 against
a 0.55 threshold. **Every legitimate answer fails both.**

**PE:** So they were never calibrated.

**SE:** They were calibrated — against the stub. And that is the part worth
writing down, because it looked like diligence at the time.

The stub answers by selecting a sentence from the retrieved context and echoing
it. An echo scores ~1.0 on a term-overlap grounding check **by construction**. The
lexical retriever scored well on exact-token queries because the gold-set queries
were written with exact tokens in them. Every number looked comfortable, and
every number was measuring a component we do not ship.

**PE:** Which is the same shape as the retention probe and the corpus path.

**SE:** The same shape a fourth time. The thing that makes development hermetic —
the stub, the injected probe, the repo-root path — is the thing that hides
whether the real component behaves. I would like to stop treating that as bad
luck.

> **DECISION UI-D22.** `RAG_MIN_SEMANTIC_SCORE` **0.55 → 0.20**, below the
> observed minimum (0.259) with margin, so a paraphrase fallback actually falls
> back. The floor is documented with the distribution it was derived from, and
> `docs/runbook.md` says to re-measure when the embedding model changes — a
> threshold with no recorded provenance is the thing being fixed here.
> **Rejected:** tuning until the demo passes. The number comes from a measured
> range or it is the same mistake again.

---

## UI-D23 — Grounding by term overlap is not a grounding check

**PE:** Lower the grounding threshold too and we are done.

**SE:** No. That one is not miscalibrated, it is measuring the wrong thing, and
lowering it would let real hallucinations through to buy back false refusals.

`grounding_score` is the fraction of the **output's** content terms that appear in
the source. So an answer is penalised for the words it adds — and the words a
good answer adds are "conflicting", "clarify", "healthcare provider", "you
should". It scores *hedging* as *hallucination*. The safest possible output is
the one it punishes hardest.

**PE:** That is an argument for a better metric, not for removing a guardrail.
What replaces it?

**SE:** `invented_clinical_claims`, which already exists in the same file, is
already called, and does the semantically correct thing: it looks for invented
dosages, medications not in the source, and unsupported clinical directives.

Adversarial cases against the same context:

| Case | overlap | invented_clinical_claims |
|---|---|---|
| faithful, hedged synthesis | 0.643 | clean |
| faithful, terse | 1.000 | clean |
| invented drug (amoxicillin) | 0.400 | **flagged** |
| invented dosage (metformin 500 mg) | 0.125 | **flagged** |
| unsupported clinical directive | 0.429 | **flagged** |
| **contradicts the source outright** | **0.500** | **flagged** |

Six for six. Now put that beside the real-model numbers: faithful answers scored
0.176 to 0.521 on overlap, and the **dangerous contradiction scored 0.500**.

**PE:** The distributions overlap.

**SE:** Completely. There is no threshold anywhere between 0.176 and 0.643 that
passes faithful answers and blocks hallucinations, because overlap does not
measure faithfulness. It measures *paraphrase distance*. A confident lie built
from source vocabulary scores well; an honest hedge scores badly.

**PE:** Then I want two things before I agree to demote it. It does not
disappear, and something still catches the case where the model answers about
something else entirely.

**SE:** Agreed on both. Overlap stays computed and stays in the response and the
logs as a **signal** — it is genuinely useful for spotting drift across a corpus,
and throwing away a measurement because it is a bad gate is overcorrecting. And a
very low overlap does mean something: an answer sharing almost no vocabulary with
its own sources is off-topic even if it invents no drug. So a **floor stays, set
far below the legitimate range** — a backstop, not the gate.

**PE:** And the client hears which sentence?

**SE:** That the check which decides whether an answer is released now looks for
invented clinical content rather than for word reuse, that it catches every case
the old one caught plus the ones it missed, and that the old number is still
reported so nothing is lost. Not "we lowered a safety threshold".

> **DECISION UI-D23.** `invented_clinical_claims` becomes the **release gate** for
> RAG answers. Term overlap is **demoted to a signal**: still computed, still
> returned, still logged, with a backstop floor at **0.15** — below the observed
> legitimate minimum of 0.176, catching only answers essentially unrelated to
> their sources.
> **Rejected:** lowering the overlap threshold and keeping it as the gate. It
> cannot separate the two populations at any value, so a "tuned" threshold is a
> guess wearing a number.
> **Rejected:** removing overlap entirely — it is a useful drift signal and the
> only cheap one available.
> **Precondition, met:** the replacement was proven on adversarial cases *before*
> the demotion, not after. Six for six, including the contradiction case, which is
> the one that matters clinically.

---

## What Part V changes about how we set numbers

Every threshold in this system was chosen before there was anything real to
measure, and three of the four were wrong once something real arrived. The
correction is procedural, not arithmetic:

**A threshold ships with the distribution it was derived from, or it does not
ship.** `RAG_MIN_SEMANTIC_SCORE = 0.20` is defensible because six measured
queries sit between 0.259 and 0.483. `0.55` was indefensible not because it was
too high but because nobody could say where it came from.

And the stub earns a standing caveat. It is not a neutral stand-in: it echoes its
source, so it passes overlap checks by construction and makes any metric built on
overlap look calibrated. Any threshold validated only in stub mode is unvalidated.

