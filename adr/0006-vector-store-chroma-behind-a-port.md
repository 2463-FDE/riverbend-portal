# ADR 0006 — Vector store: ChromaDB behind a KnowledgeIndex port (W2)

- **Status:** Accepted
- **Date:** 2026-07-29
- **Spec:** `docs/specs/w2-rag-knowledge-retrieval.md`
- **Baseline:** ADR 0004
- **Debate:** `docs/design-debate-w1-w4.md` D3

## Context

Week 2 needs a retrieval index over a sampled subset of Riverbend's patient
records and clinic knowledge documents. The engagement mandates **ChromaDB**, used
locally now and planned for production.

The stack already runs Postgres 15 as its single system of record, with a
hand-rolled migration history and a documented backup path. Adding a second
stateful service is not free.

## Decision

### 1. Chroma is the adapter; `KnowledgeIndex` is the interface

Application code talks to a narrow local port:

```python
class KnowledgeIndex(Protocol):
    def add(self, chunks: Sequence[IndexChunk]) -> int: ...
    def query(self, text: str, k: int, mode: RetrievalMode) -> list[Retrieved]: ...
    def delete_document(self, doc_id: str) -> int: ...
    def count(self) -> int: ...
    def stats(self) -> IndexStats: ...
```

`langchain_chroma.Chroma` sits behind it. **No module outside the adapter imports
`chromadb` or `langchain_chroma`.** Enforced by an import-boundary test.

### 2. Deployment shape

| Environment | Client | Rationale |
|---|---|---|
| dev / CI | `chromadb.PersistentClient(path=…)` — temp dir in tests | No service to start; tests are hermetic |
| production | `chromadb.HttpClient(host, port)` against a compose service | Chroma documents client/server mode as a Docker-deployable single node; `AsyncHttpClient` is available with identical signatures |

This mirrors how every other Riverbend dependency is deployed, so the ops story is
not novel.

### 3. Embeddings

`amazon.titan-embed-text-v2:0` in production; a deterministic seeded offline
backend in dev and CI. Titan v2 accepts 8,192 tokens / 50,000 characters and
emits 1,024 (default), 512, or 256 dimensions.

**Default 256.** The demo corpus is a sampled subset; 256 dimensions cut index
size and query latency roughly fourfold against the default with no measurable
retrieval loss at this corpus size. Env-configurable — the knob exists precisely
because that trade-off changes with corpus size.

**Embed once, cache.** Cache key `sha256(text + model_id + dims)`. A re-ingest of
unchanged content performs zero embedding calls, asserted by test. This week is
flagged as a quota-risk week in the client packet; re-embedding per run is the
specific failure mode being guarded against.

### 4. Retrieval: hybrid by default

`dense` (cosine), `sparse` (BM25-style idf-weighted lexical), `hybrid`
(weighted Reciprocal Rank Fusion). RRF fuses **ranks**, so no score normalization
is needed between two differently-scaled retrievers.

Hybrid is the default because clinic queries are lexically loaded —
`penicillin`, `A1C`, `ROI`, `fasting`, an MRN. These are exact tokens dense
retrieval smears, and they are precisely the terms a clinical error turns on.

### 5. Refusal is a first-class outcome

Two independent floors: **term coverage** (fraction of the query's content words
in the top hit — corpus-size independent, primary gate) and a **dense cosine
floor** (paraphrase fallback, per-backend baseline because the scales differ).
Failing both refuses. A refusal is a successful outcome, not an error path.

## Alternatives considered

### pgvector in the existing Postgres — *rejected, recorded as the fallback*

Genuinely the lower-risk option on operational grounds: vectors beside the
relational data they derive from, one transaction boundary, one backup, one
restore, one thing to be down. Rejected because the engagement mandates Chroma and
because Chroma is what the client's team is being trained against — handing them
pgvector means the handoff docs match nothing they have learned.

**The port exists so this decision is reversible in one file.** Migration cost if
exercised: implement `PgVectorIndex` against the same protocol, backfill from
source documents, swap the factory. Estimated at 1–2 days, not a refactor.

### Qdrant / Pinecone — *rejected*
Off-mandate and off-curriculum. Prior programme research
(`projects/2463-fde/notes/rag-vector-store-free-tier-research`) already evaluated
and set them aside.

## Consequences

**Accepted costs.**
- A second stateful service in production, with a volume, a health check, and a version-skew surface against `langchain-chroma`.
- **Mandatory before ship:** the runbook must carry Chroma volume backup/restore *and* full rebuild-from-source-documents. The index is derived data; if we cannot rebuild it, we have created an unrecoverable store. This is a gate on the W2 PR, not a follow-up.

**Gained.**
- Retrieval swappable in one file.
- Embedding cost bounded by construction (cache + corpus cap + 256 dims).
- The same interface serves the offline CI backend and the Bedrock production backend, so no test needs credentials.

**Not decided here.** Whether Chroma runs single-node or clustered in a real
production deployment. The demo is single-node; the ADR records that a clustered
or Chroma Cloud deployment is the next decision if corpus size or availability
requirements grow.
