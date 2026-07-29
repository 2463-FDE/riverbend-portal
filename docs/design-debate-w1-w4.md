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
