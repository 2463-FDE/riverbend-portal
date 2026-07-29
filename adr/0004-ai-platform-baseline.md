# ADR 0004 — AI platform baseline: Bedrock, LangGraph v1, Chroma

- **Status:** Accepted
- **Date:** 2026-07-29
- **Author:** Forward-deployed engagement (Product Engineer + Senior Engineer)
- **Scope:** cross-cutting for W1–W4. Week-specific decisions live in ADRs 0005–0010.
- **Debate record:** `docs/design-debate-w1-w4.md` D1–D3, D6, D9, D10
- **Requirements:** `docs/specs/requirements-w1-w4.md` §Global constraints

## Context

Riverbend's board wants an AI capability. The portal is a brownfield FastAPI +
Next.js system carrying real PHI with fourteen documented debt items. Any AI
surface we add lands on top of that, so the platform choice is a compliance
decision before it is a technical one.

Three constraints were set by the engagement, not chosen by us:

1. **AWS Bedrock is the only model provider.** No other LLM vendor.
2. **LangChain / LangGraph v1** — strictly the v1 line.
3. **ChromaDB** as the vector store, local for development, planned for production.

Two further facts shape everything:

- We cannot execute an AWS BAA or spend money for this engagement, so no live PHI
  and no live inference until an explicitly key-gated step.
- The system already claims "HIPAA compliant" in its README while storing `ssn`,
  `dob`, and `notes` as plaintext `TEXT` (ADR 0002, debt D3). The AI feature must
  not inherit that habit of asserting a property instead of architecting it.

### Verification

Every version and API claim below was verified against primary sources on
**2026-07-29** and recorded in the second-brain note
`projects/2463-fde/notes/riverbend-w1-w4-tech-baseline.md`. Package versions come
from the PyPI JSON API on that date.

## Decision

### 1. Model access — `ChatBedrockConverse`, one path

All inference goes through `langchain-aws`'s `ChatBedrockConverse` (Bedrock
Converse API). One client, one auth path, one retry envelope, tool-calling and
structured output built in.

**Why Bedrock, architecturally and not just because we were told:** Amazon
Bedrock appears on the [AWS HIPAA-eligible services reference][hipaa], so it
*may* carry PHI under an executed BAA. AWS documents that Bedrock runs models in
per-provider Model Deployment Accounts and that "model providers don't have any
access to those accounts… they don't have access to Amazon Bedrock logs or to
customer prompts and completions" ([Bedrock data protection][dp]), and that the
service uses a zero-operator-access and zero-data-retention model **by default**
([abuse detection][abuse]). That is *eligibility*, not compliance. Compliance
still requires the signed BAA we do not have — **and a retention decision, below.**

#### 1a. Data retention is a mode, and it is a compliance control

This is the correction that matters most in this ADR. It is not enough to say
"Bedrock is HIPAA-eligible and providers can't see prompts." Bedrock exposes an
explicit retention mode ([Bedrock data retention][ret]):

| Mode | Behaviour |
|---|---|
| `none` | **Zero data retention.** No request or response data is written to durable storage by AWS or shared with the model provider. |
| `default` | The model's own policy. AWS **may retain** data for safety and abuse prevention. The provider does not receive it. |
| `provider_data_share` | AWS **retains and shares** inference data with the model provider. Required for access to certain models. |
| `inherit` | No opinion at this scope; defer to a broader one. Default for new accounts and projects. |

Resolution is `effective = first non-inherit value of (project → account → model
default)`. Each model independently declares `allowed_modes`.

AWS states that for models requiring `provider_data_share` — currently Claude
Mythos 5 and Claude Fable 5 — "user prompts and completions are shared with
Anthropic and retained for up to 30 days for trust and safety purposes."

**Therefore:**

1. **The effective mode for this workload is `none`.** Set at account scope, and
   enforced organisation-wide by SCP on `bedrock:PutAccountDataRetention` /
   `bedrock-mantle:PutAccountDataRetention` with a
   `StringNotEquals: {DataRetentionMode: "none"}` deny.
2. **Any model whose `allowed_modes` excludes `none` is disqualified for this
   workload, regardless of capability.** Selecting one would send PHI to a third
   party for 30 days — which is precisely debt **D13**, the impermissible
   disclosure this engagement exists to prevent (164.502(e)).
3. **We verify rather than assume.** Bedrock fails closed — "if your account or
   project is configured for zero data retention… and you invoke a model that
   requires retention, Amazon Bedrock will block the request and return an error,"
   and such a model reports `status: "unavailable"`. We rely on that as a backstop,
   not as the control: a startup preflight asserts the effective mode is `none`
   **and** that the configured model's `allowed_modes` contains `none`, and refuses
   to serve otherwise. Requirement `RVB-X-09`.

Note also that cross-region inference stores any retained inputs and outputs in
the **destination** region — relevant if inference profiles ever route outside the
agreed BAA geography.

**Model identifiers** are region-scoped inference-profile IDs, supplied by env
with a documented default, never hard-coded. They are overridable **per path**
(`summary`, `rag`, `agent`, `synthesis`) so the demo can be upgraded without a
code change, defaulting to one model so the cost model stays single-rowed
(debate D9).

**Credentials.** botocore reads `AWS_BEARER_TOKEN_BEDROCK` (requires
`boto3 >= 1.40`). AWS is explicit that long-term API keys are
**"Recommended only for exploration"** and short-term keys (≤12 h, inheriting the
generating principal's permissions) are **"Recommended for production use"**
([Bedrock API keys][keys]). A long-term key in `.env` is therefore a *demo*
posture and is logged as such — see ADR 0005 and the D9 debt entry.

### 2. Orchestration — escalating abstraction, justified per week

We reject "use LangGraph everywhere for consistency." LangGraph v1's own release
notes describe it as a runtime for durable execution, checkpointing, streaming
and human-in-the-loop ([LangGraph v1][lgv1]); a single request/response
summarization uses none of that. Abstraction is introduced on the week it is
first *needed*:

| Week | Abstraction | Why it earns its place |
|---|---|---|
| W1 | LangChain v1 only — `ChatBedrockConverse` behind a resilience wrapper | Straight line in, straight line out. No state, no branching, no tools. |
| W2 | LangGraph `StateGraph`, two conditional edges | RAG genuinely branches: relevance gate (refuse vs retrieve-and-answer) and grounding gate (serve vs refuse). Proves the runtime on a low-stakes path before W3. |
| W3 | LangChain `create_agent` + checkpointer | Tool use + durable visit-scoped memory. |
| W4 | Hand-built `StateGraph` with `Send` fan-out | Parallel domain retrieval with a deterministic authorization edge. |

**`create_react_agent` is not used.** LangGraph v1 deprecates it in favour of
LangChain's `create_agent`, which "provides a simpler interface, and offers
greater customization potential through the introduction of middleware"
([LangGraph v1][lgv1]).

**Pinning.** `langgraph>=1.0,<2.0` *is* the v1 API contract. LangGraph v1 is
explicitly a stability release — "It keeps the core graph APIs and execution
model unchanged" — so the minor line moves without breaking the surface we
depend on.

### 3. Vector store — Chroma behind a local port

`langchain-chroma`'s `Chroma` is the adapter; our code talks to a narrow
`KnowledgeIndex` port (`add` / `query` / `delete` / `count` / `stats`).
`PersistentClient` in development and CI, `HttpClient` against a Chroma service in
the compose stack for production — Chroma documents client/server mode as a
Docker-deployable single node with `HttpClient` and `AsyncHttpClient`
([Chroma client/server][chroma]).

The port exists so the pgvector fallback is one file, not a refactor
(debate D3). The runbook must carry volume backup/restore and rebuild-from-source
before this dependency ships.

**Embeddings:** `amazon.titan-embed-text-v2:0` — 8,192 tokens / 50,000 characters
in, output vector **1,024 (default), 512, or 256** ([Titan embeddings][titan]).
We default to 256 dimensions for the sampled demo corpus (index size and latency)
and keep it configurable. A deterministic offline backend covers dev and CI so no
test spends money.

### 4. Controls — two tiers behind one interface

Attestation needs a signature and a budget. Architecture needs neither. We build
every control and exercise it offline; the ones that need AWS become a config
flip, and the flip is mock-tested so "lift and shift" is a tested property rather
than a claim (debate D6).

| Tier | Always on? | Contents |
|---|---|---|
| **Tier 0** | Yes — free, runs in CI | Offline grounding heuristic; regex PHI scrub; body-free structured audit events; `EncryptedSerializer` checkpoints; deterministic authorization; token + cost budget guards |
| **Tier 1** | Key/BAA-gated | Bedrock `ApplyGuardrail` + contextual grounding; Titan embeddings; live Converse |

`ApplyGuardrail` is usable independently of model invocation — AWS documents it as
"decoupled from foundational models… You can use Guardrails without invoking
Foundation Models" ([ApplyGuardrail][guard]).

**The two sides are not the same policy set, and conflating them was an error in
an earlier draft of this ADR.**

| Side | `source` | Policies that apply | Used for |
|---|---|---|---|
| Input | `INPUT` | Content filters, denied topics, word lists, **sensitive-information (PII) filters** | Screen the user's text *before* retrieval or generation — catch a pasted identifier or an out-of-scope request without spending a generation |
| Output | `OUTPUT` | The above, **plus contextual grounding** | Screen the model's response before it is served |

**Contextual grounding is output-side only.** AWS is explicit that it "require[s]
3 components to perform the check: the grounding source, the query, and the content
to guard (or the model response)" ([contextual grounding][cgc]). There is no model
response at input time, so grounding cannot be evaluated there. It returns separate
**grounding** and **relevance** confidence scores, thresholds configurable between
0 and 0.99 (1 is invalid — it blocks everything), with limits of 100,000 characters
of grounding source, 1,000 of query and 5,000 of response.

**Documented limitation, carried into ADR 0008:** AWS states contextual grounding
supports summarization, paraphrasing and question answering, and that
"Conversational QA / Chatbot use cases are not supported." W1's summary path and
W2's RAG answer path qualify. **W3's conversational eligibility assistant does
not** — it gets a deterministic check that the status it reports equals the status
the tool returned, which is the correct control for that shape anyway. W3 may
still use `source: INPUT` PII filtering, which is unaffected by that limitation.

### 5. Persistence of agent state

Checkpointed graph state can contain PHI. LangGraph ships
`EncryptedSerializer` (`langgraph.checkpoint.serde.encrypted`) which can be
handed to `SqliteSaver` or `PostgresSaver`, encrypting checkpoint payloads at
rest without hand-rolled crypto ([checkpointers][ckpt]). `InMemorySaver` in tests;
an encrypted durable saver anywhere state persists.

### 6. Testing — three tiers, no spend by default

| Tier | Marker | Runs | Proves |
|---|---|---|---|
| T1 | default | CI | Timeouts, retry classification, parse fallbacks, budget refusal, scrub coverage, no-PHI-in-logs, breaker state machine, authz ordering, interrupt/resume, **retention preflight fails closed** |
| T2 | default | CI | Golden-set retrieval eval with deterministic offline embeddings — reproducible recall/precision/groundedness numbers we hand the client |
| T2e | default | CI | **One end-to-end happy path per week through the gateway, against the stub model** — proves the surface the client actually clicks, not only its guardrails |
| T3 | `@pytest.mark.live` | skipped unless keyed | Live Bedrock resolution + invocation, each test asserting a hard per-test cost ceiling. **L0 runs first and spends nothing:** `GET /v1/models/{model}` asserting `allowed_modes` contains `none`. |

T3 is written **up front**, not on key-day.

The T2e tier exists because an earlier draft tested only that an unauthenticated
call returns 401 — which an endpoint that rejects *everything* would also pass.
Controls without a working feature is not a delivery.

## Consequences

**Good.**
- One model client, one auth path, one place to change a timeout.
- The AI surface is defensible under audit: no PHI in prompts by construction, bounded cost and latency, validated output, body-free audit events, encrypted agent memory.
- Abstraction cost is paid only where it buys something, so each week's PR stays reviewable.
- Enabling the AWS-managed controls is a documented config diff, and the diff is exercised by tests today.

**Costs we are accepting.**
- Chroma is a second stateful service in a stack that already runs Postgres. Mitigated by the port and the runbook requirement; pgvector remains a costed fallback (ADR 0006).
- Four call sites can be pointed at four different models. Mitigated by defaulting all four to one model and requiring evidence before splitting.
- Offline grounding heuristics are weaker than Bedrock's managed check. Accepted deliberately: they cost nothing, run on every commit, and are the floor rather than the ceiling.
- `langgraph>=1.0,<2.0` floats the minor version. Accepted on the strength of the stability guarantee; the lockfile pins exactly for reproducibility.

**Explicitly not decided here.** Field-level encryption of PHI columns (D3, W9),
the Safe-Harbor de-identification scrub and vendor governance memo (D13, W8), and
the append-only audit trail (D2, W10). Named so their absence is a schedule, not
an oversight.

[hipaa]: https://aws.amazon.com/compliance/hipaa-eligible-services-reference/
[dp]: https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html
[ret]: https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html
[abuse]: https://docs.aws.amazon.com/bedrock/latest/userguide/abuse-detection.html
[keys]: https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html
[titan]: https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html
[guard]: https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-independent-api.html
[cgc]: https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-contextual-grounding-check.html
[lgv1]: https://docs.langchain.com/oss/python/releases/langgraph-v1
[ckpt]: https://docs.langchain.com/oss/python/langgraph/checkpointers
[chroma]: https://docs.trychroma.com/guides/deploy/client-server-mode
