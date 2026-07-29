# W2 Spec — RAG retrieval + eval harness that surfaces patient fragmentation

- **Week:** 2 — RAG & Knowledge Retrieval
- **Requirements covered:** `RVB-W2-01` … `RVB-W2-12`
- **ADRs:** `adr/0006` (Chroma + retrieval design), `adr/0007` (MPI / match key)
- **Branch:** `feat/w2-rag-mpi-eval` → `feat/riverbend-w1-w4`
- **Status:** specified

---

## 1. The ask and the real job

**Dr. Okonkwo asked for:** *"Clinicians waste time hunting through a patient's
history. Build me a retrieval helper that pulls the right past records when they
open a chart."*

**The trap:** we can build exactly that, tune it to a respectable recall number,
demo it successfully, and be **wrong**. Her own data dump contains one human —
Maria Gonzalez — existing as **three patient IDs** with different MRNs and slight
name/DOB spelling variants, all created through self-service intake. The
contractor's own gold-set proves it: *"show me Maria Gonzalez's allergies"*
returns records from **one** of the three fragments.

Retrieval quality is not the problem. The source of truth is forked. A retrieval
system that is 100% accurate against a fragmented corpus is 33% accurate about the
patient — and it will *look* healthy the whole time, because precision against
what you indexed says nothing about coverage of what exists.

Dr. Nguyen already filed this as **RIV-160**: *"Why does the allergy list look
different depending on which chart I open for the same lady?"* It was triaged as a
UI inconsistency. It is a patient-safety defect.

**The headline deliverable is therefore the eval harness**, and specifically the
metric that makes the fragmentation impossible to miss.

---

## 2. Architecture

```
                       ┌──────────── LangGraph StateGraph ────────────┐
POST /ai/knowledge/query ──►│  retrieve ──► relevance_gate ──┬──► refuse  │
                       │                                │            │
                       │                                └──► generate ──► ground_gate ──┬──► answer
                       │                                                                └──► refuse
                       └──────────────────────────────────────────────┘
                                       │
                          KnowledgeIndex (port)
                                       │
                          langchain_chroma.Chroma (adapter)
                                       │
                     PersistentClient (dev/CI) | HttpClient (prod)
```

**Why a graph here and not in W1** (ADR 0004, debate D2): RAG has two genuine
branch points. "Nothing relevant came back" must be a first-class outcome, not an
exception; and "the answer isn't supported by what we retrieved" must be able to
withdraw a response that has already been generated. Two `add_conditional_edges`.
It also gives W7 a place to hang the output guardrail without reopening the design.

### 2.1 The `KnowledgeIndex` port

```python
class KnowledgeIndex(Protocol):
    def add(self, chunks: Sequence[IndexChunk]) -> int: ...
    def query(self, text: str, k: int, mode: RetrievalMode) -> list[Retrieved]: ...
    def delete_document(self, doc_id: str) -> int: ...
    def count(self) -> int: ...
    def stats(self) -> IndexStats: ...
```

Our code never imports `chromadb` or `langchain_chroma` outside the adapter.
Swapping to pgvector is one file (ADR 0006, debate D3).

### 2.2 Embeddings

| Environment | Backend | Model | Dims |
|---|---|---|---|
| production | Bedrock | `amazon.titan-embed-text-v2:0` | 256 (configurable 256/512/1024) |
| dev / CI | offline deterministic | hash-based, seeded | 256 |

Titan v2 accepts 8,192 tokens / 50,000 characters and emits 1,024 / 512 / 256
dimensions. 256 is chosen for index size and query latency on a sampled demo
corpus; the knob is env-driven.

**Embed once, cache** (`RVB-W2-03`). Cache key is `sha256(text + model_id + dims)`.
A re-ingest of unchanged content performs **zero** embedding calls, and the test
asserts the call count is zero — this is a quota-risk week and the client packet
flags it explicitly.

### 2.3 Retrieval modes

`dense` (cosine over embeddings), `sparse` (BM25-style idf-weighted lexical), and
`hybrid` (weighted Reciprocal Rank Fusion of the two ranked lists). RRF fuses
*ranks*, so it needs no score normalization between two differently-scaled
retrievers.

Lexical matters disproportionately in a clinic: `penicillin`, `A1C`, `ROI`,
`fasting`, an MRN — exact tokens dense search smears. Hybrid is the default.

### 2.4 Refuse-vs-answer

Two independent floors, because they fail differently:

- **Term coverage** — fraction of the query's content words present in the top
  hit. Corpus-size independent. Primary gate.
- **Dense cosine floor** — paraphrase fallback for queries that share meaning but
  not vocabulary. Backend-dependent baseline, so the floor is per-backend.

Failing both → refuse. A refusal is a successful outcome, not an error.

---

## 3. The eval harness — the actual deliverable

`POST /ai/knowledge/eval` runs the contractor's gold-set and returns:

### 3.1 Standard retrieval metrics

| Metric | Definition |
|---|---|
| `context_recall` | fraction of gold-relevant chunks retrieved in top-k |
| `context_precision` | fraction of retrieved chunks that are gold-relevant |
| `groundedness` | fraction of generated answers passing the grounding check |
| `answer_match` | fraction of answers containing the gold answer's key facts |

These are the numbers she expects. They will look **fine**.

### 3.2 The integrity metrics — `RVB-W2-07`

These are the numbers that end the meeting.

| Metric | Definition | Why it exposes the problem |
|---|---|---|
| `duplicate_patient_rate` | fraction of distinct humans in the corpus represented by more than one `patient_id`, matched on a normalized `(name, dob)` fuzzy key | States the fork exists at all |
| `fragment_coverage` | for each gold query about a patient: `fragments_retrieved / fragments_that_exist_for_that_human` | States that a query that *looks* answered was answered from part of the record |
| `identity_split_examples` | concrete rows — the three Maria Gonzalez IDs, their MRNs, their spelling variants, and which allergy/med lists live on which | Makes it undeniable and non-abstract |

**The reporting rule that makes this work:** the harness prints the standard
metrics and the integrity metrics *together*, in that order. The narrative is:
"Retrieval recall is 0.9. Fragment coverage is 0.34. Both are true. The second one
is the one that will hurt a patient."

An eval report that showed only recall would have *validated* the broken system.

---

## 4. The MPI finding (`RVB-W2-08`) and ADR (`RVB-W2-09`)

**Finding.** Self-service intake writes a new `patients` row with no match key.
`db/schema.sql` confirms it: `patients` has an `mrn` column explicitly annotated
as *not* used as a match key, and there is no unique constraint on any identity
tuple. One human, typing their name slightly differently on two visits, becomes
two charts. Neither chart knows about the other.

**Framing for the client:** this is not a data-cleanliness annoyance. A clinician
opening fragment #2 sees an empty allergy list and prescribes against it. The
allergy is recorded — on fragment #1. The system will never tell them. That is the
patient-safety risk, and it is already live: RIV-160 is a clinician noticing the
symptom and being told it's a display bug.

**ADR 0007 proposes** (design only — implementing an MPI is service-sized and
belongs on the roadmap, per `RVB-X-06`):

1. A **deterministic match key** — normalized name + DOB + last-4 SSN where
   present — enforced by a unique index on the intake write path.
2. **Probabilistic candidate scoring** for near-misses, surfacing "possible
   duplicate" to a human rather than auto-merging. Auto-merging charts is
   irreversible and can *create* a safety incident.
3. **Link, don't merge.** A `patient_links` table asserting "these IDs are one
   human," so retrieval can span fragments immediately while the underlying rows
   stay intact and auditable.
4. **Where the check belongs:** the intake write path. Not retrieval. The ADR says
   this in one sentence because it is the whole point: *no retrieval tuning fixes a
   fragmented source of truth.*

---

## 5. Ingest as a privileged action (`RVB-W2-10`, HITL point 1)

Adding a document to the knowledge base silently changes every future grounded
answer, for every user, indefinitely — and the poisoned answers still carry
confident citations, to the poisoned document. Low volume, unbounded blast radius:
the debate's test for where a human gate belongs (D5).

- Query / eval / corpus-list: any authenticated session.
- **Ingest and seed: a `knowledge_admin` capability**, enforced at the gateway.
- The gateway stamps `added_by` **server-side** from the session — never client-supplied.
- Documents pass `scrub_document` before indexing.

---

## 6. Acceptance criteria

All offline, deterministic, zero-spend.

| # | Test | Asserts | Req |
|---|---|---|---|
| 1 | `test_chunking_is_deterministic` | Same input → identical chunks across runs (the eval depends on this) | W2-01 |
| 2 | `test_chunk_overlap_never_stalls` | `overlap >= chunk_size` is clamped; the window always advances | W2-01 |
| 3 | `test_index_roundtrip_persistent_client` | Add → query → delete → count against a temp `PersistentClient` | W2-01 |
| 4 | `test_embed_cache_zero_calls_on_reingest` | Second identical ingest performs **0** embedding calls | W2-03 |
| 5 | `test_retrieval_modes[dense\|sparse\|hybrid]` | Each mode returns ranked results; hybrid fuses without score normalization | W2-04 |
| 6 | `test_lexical_beats_dense_on_exact_token` | A rare exact term (`penicillin`) ranks its chunk top in sparse and hybrid | W2-04 |
| 7 | `test_offtopic_query_refuses` | A query unrelated to the corpus returns a refusal, not a low-confidence guess | W2-05 |
| 8 | `test_answer_carries_citations` | Every answered response has ≥1 citation resolving to a retrieved chunk | W2-05 |
| 9 | `test_graph_takes_refuse_edge` | With relevance below floor, the graph traverses `relevance_gate → refuse` and **never** reaches `generate` | W2-05 |
| 10 | `test_graph_ground_gate_withdraws_answer` | An ungrounded generation is withdrawn at `ground_gate` and returned as a refusal | W2-05 |
| 11 | `test_eval_reports_four_standard_metrics` | `context_recall`, `context_precision`, `groundedness`, `answer_match` all present | W2-06 |
| 12 | `test_eval_persists_across_restart` | `GET /eval/latest` returns the last run after a process restart | W2-06 |
| 13 | **`test_fragmented_patient_lowers_fragment_coverage`** | Seeded 3-fragment patient → `fragment_coverage < 1.0` for a query answered from one fragment | **W2-07** |
| 14 | **`test_duplicate_rate_detects_seeded_fork`** | `duplicate_patient_rate > 0` and the three Maria Gonzalez IDs appear in `identity_split_examples` | **W2-07** |
| 15 | **`test_recall_high_while_coverage_low`** | The scenario that proves the point: `context_recall >= 0.8` **and** `fragment_coverage <= 0.5` in the same run | **W2-07** |
| 16 | `test_ingest_requires_capability` | Non-admin session → 403; admin → 200 | W2-10 |
| 17 | `test_added_by_is_server_stamped` | A client-supplied `added_by` is overwritten by the session username | W2-10 |
| 18 | `test_scrub_document_before_index` | An SSN in a submitted document never reaches the index | W2-10 |
| 19 | `test_corpus_cap_enforced_and_logged` | Ingest beyond the configured cap is refused and the cap is logged | W2-11 |
| 20 | `test_w1_model_client_unmodified` | `git diff` on the W1 model-client module across this PR is empty — the W2 abstraction did not leak backwards (debate D2 condition) | W2-01 |

**Live tier:** `L3 test_live_titan_embeddings_dimension` — one real Titan call,
asserts vector length equals configured dims and asserts cost below ceiling.

---

## 7. Definition of done

- [ ] All acceptance tests pass offline
- [ ] Eval report renders standard **and** integrity metrics together
- [ ] `docs/findings/w2-patient-fragmentation.md` with the three concrete IDs, tied to RIV-160
- [ ] ADR 0006 (Chroma + retrieval), ADR 0007 (MPI / match key)
- [ ] Runbook section: Chroma volume backup/restore + rebuild-from-source
- [ ] W1's model client shows zero modifications in this PR
- [ ] PR body answers the standing question
