"""W2 — chunking, embeddings, the Chroma adapter, and the retrieval graph.

All offline, deterministic, zero spend. Chroma runs as an in-process ephemeral
client; the embedding backend is the deterministic offline projection.
"""
import sys

import pytest

from conftest import load_module

chunking = load_module("services/ai-orchestrator/chunking.py", "w2_chunking")
embeddings = load_module("services/ai-orchestrator/embeddings.py", "w2_embeddings")
chroma_index = load_module("services/ai-orchestrator/chroma_index.py", "w2_chroma")
# Use the port module the ADAPTER bound, not a second copy loaded under another
# name: `load_module` gives each alias its own module object, so two loads of
# index_port would produce two distinct ScopeRequired classes and
# `pytest.raises` would miss the one actually thrown.
index_port = sys.modules["index_port"]
corpus = load_module("services/ai-orchestrator/corpus.py", "w2_corpus")
rag_graph = load_module("services/ai-orchestrator/rag_graph.py", "w2_rag_graph")

KIND_KNOWLEDGE = index_port.KIND_KNOWLEDGE
KIND_RECORD = index_port.KIND_RECORD
settings = chroma_index.settings


@pytest.fixture
def index():
    import chromadb

    idx = chroma_index.ChromaIndex(client=chromadb.EphemeralClient())
    idx.add(corpus.build_knowledge_chunks())
    idx.add(corpus.build_record_chunks())
    return idx


# --------------------------------------------------------------------------- #
# 1/2 — chunking is deterministic and always advances
# --------------------------------------------------------------------------- #
def test_chunking_is_deterministic():
    text = " ".join(f"word{i}" for i in range(400))
    a = chunking.chunk_text(text, 120, 24)
    b = chunking.chunk_text(text, 120, 24)
    assert [c.text for c in a] == [c.text for c in b]
    assert len(a) > 1, "400 words should produce more than one chunk"


def test_chunk_overlap_never_stalls():
    """overlap >= size would be an infinite loop without the clamp."""
    text = " ".join(f"w{i}" for i in range(200))
    chunks = chunking.chunk_text(text, 20, 999)
    assert chunks
    assert len({c.index for c in chunks}) == len(chunks)


def test_empty_text_produces_no_chunks():
    assert chunking.chunk_text("") == []
    assert chunking.chunk_text("   \n  ") == []


# --------------------------------------------------------------------------- #
# 4 — embed once, cache (RVB-W2-03)
# --------------------------------------------------------------------------- #
def test_embedding_is_deterministic():
    e1 = embeddings.Embedder(embeddings.OfflineEmbedder(64))
    e2 = embeddings.Embedder(embeddings.OfflineEmbedder(64))
    assert e1.embed_one("penicillin allergy") == e2.embed_one("penicillin allergy")


def test_embed_cache_prevents_repeat_calls():
    """The client packet flags W2 as a quota-risk week: embed once, cache.

    'We added a cache' and 'the cache actually prevents the calls' are different
    claims. This asserts the second one.
    """
    embedder = embeddings.Embedder(embeddings.OfflineEmbedder(64))
    texts = ["alpha text", "beta text", "gamma text"]

    embedder.embed(texts)
    assert embedder.stats.calls == 3
    assert embedder.stats.cache_hits == 0

    embedder.embed(texts)
    assert embedder.stats.calls == 3, "re-embedding unchanged content cost calls"
    assert embedder.stats.cache_hits == 3


def test_reingest_performs_zero_embedding_calls(index):
    before = index.embedder.stats.calls
    index.add(corpus.build_record_chunks())
    assert index.embedder.stats.calls == before, (
        "a re-ingest of unchanged content must perform zero embedding calls"
    )


# --------------------------------------------------------------------------- #
# 3/5/6 — the Chroma adapter and retrieval modes
# --------------------------------------------------------------------------- #
def test_index_roundtrip(index):
    assert index.count() > 0
    assert index.count(KIND_RECORD) > 0
    assert index.count(KIND_KNOWLEDGE) > 0
    docs = index.documents()
    assert docs and all("doc_id" in d for d in docs)


@pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid"])
def test_retrieval_modes_all_return_results(index, mode):
    hits = index.query("fasting before a blood draw", k=3,
                       kind=KIND_KNOWLEDGE, mode=mode)
    assert hits, f"{mode} returned nothing"
    assert all(h.kind == KIND_KNOWLEDGE for h in hits)


def test_lexical_finds_the_exact_clinical_term(index):
    """'penicillin' is exactly the kind of token dense retrieval smears.

    It is also exactly the token a clinical error turns on, which is why hybrid
    weights the lexical retriever above the dense one.
    """
    hits = index.query("penicillin", k=3, kind=KIND_RECORD,
                       patient_scope=[1042, 1330, 1588], mode="sparse")
    assert hits
    assert hits[0].patient_id == 1330, (
        "the chart that actually records the penicillin allergy should rank first"
    )


def test_delete_document_removes_its_chunks(index):
    before = index.count()
    removed = index.delete_document("kb-fasting")
    assert removed > 0
    assert index.count() == before - removed


# --------------------------------------------------------------------------- #
# PHI boundary on the index (RVB-W2-10)
# --------------------------------------------------------------------------- #
def test_record_query_without_scope_is_refused(index):
    """An unscoped similarity search over charts is an IDOR with no WHERE clause
    for a reviewer to notice is missing."""
    with pytest.raises(index_port.ScopeRequired):
        index.query("allergies", k=3, kind=KIND_RECORD)


def test_patient_scope_isolates_charts(index):
    hits = index.query("allergies medications", k=10, kind=KIND_RECORD,
                       patient_scope=[1043])
    assert hits
    assert {h.patient_id for h in hits} == {1043}, (
        "a scoped query leaked chunks from outside the scope"
    )


def test_empty_scope_authorizes_nothing(index):
    assert index.query("allergies", k=5, kind=KIND_RECORD, patient_scope=[]) == []


def test_record_chunks_must_carry_patient_id(index):
    bad = index_port.IndexChunk(
        id="x::0", text="some chart text", doc_id="x", doc_title="X",
        chunk_index=0, kind=KIND_RECORD, patient_id=None,
    )
    with pytest.raises(ValueError):
        index.add([bad])


def test_record_corpus_lands_in_the_phi_collection(index):
    assert chroma_index.COLLECTIONS[KIND_RECORD] == "riverbend_records"
    assert chroma_index.COLLECTIONS[KIND_KNOWLEDGE] == "riverbend_knowledge"
    hits = index.query("annual physical", k=5, kind=KIND_KNOWLEDGE)
    assert all(h.patient_id is None for h in hits), (
        "patient chart text must not be in the shared knowledge collection"
    )


# --------------------------------------------------------------------------- #
# 9/10 — the graph takes the branches it claims to (RVB-W2-05)
# --------------------------------------------------------------------------- #
def test_graph_answers_a_relevant_knowledge_query(index):
    out = rag_graph.run(index, "how long must a patient fast before a blood draw?",
                        kind=KIND_KNOWLEDGE)
    assert not out.refused
    assert out.grounded
    assert out.citations
    assert "generate" in out.path and "answer" in out.path


def test_graph_takes_the_refuse_edge_and_never_generates(index):
    """A refusal is a successful outcome, not an error path."""
    out = rag_graph.run(index, "what is the airspeed velocity of a laden swallow",
                        kind=KIND_KNOWLEDGE)
    assert out.refused
    assert out.answer == rag_graph.REFUSAL
    assert "generate" not in out.path, (
        "an irrelevant query must not reach generation — that is the whole "
        "point of the relevance gate"
    )
    assert out.citations == []


def test_ground_gate_withdraws_an_unsupported_answer(index, monkeypatch):
    """An answer already generated must still be withdrawable."""
    import model_client

    class Ungrounded:
        def invoke(self, *_a, **_k):
            return model_client.ModelResult(
                text="The patient should start warfarin 5 mg daily immediately.",
                model_id="stub", stubbed=True,
            )

    graph = rag_graph.build_graph(index, client=Ungrounded())
    out = rag_graph.run(index, "how long must a patient fast before a blood draw?",
                        kind=KIND_KNOWLEDGE, graph=graph)
    assert out.refused
    assert "generate" in out.path and "ground_gate" in out.path
    assert "warfarin" not in out.answer, "raw ungrounded model text was served"


def test_citations_resolve_to_retrieved_chunks(index):
    out = rag_graph.run(index, "what must a patient bring to check in?",
                        kind=KIND_KNOWLEDGE)
    assert out.citations
    retrieved_docs = {r["doc_id"] for r in out.retrieved}
    for citation in out.citations:
        assert citation["doc_id"] in retrieved_docs


def test_scoped_record_query_through_the_graph(index):
    out = rag_graph.run(index, "what allergies are recorded?", kind=KIND_RECORD,
                        patient_scope=[1330])
    assert not out.refused
    assert all(c["patient_id"] == 1330 for c in out.citations)


# --------------------------------------------------------------------------- #
# import boundary (ADR 0006)
# --------------------------------------------------------------------------- #
def test_only_the_adapter_imports_chromadb():
    import os
    import re

    service_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "services", "ai-orchestrator",
    )
    offenders = []
    for name in os.listdir(service_dir):
        if not name.endswith(".py") or name == "chroma_index.py":
            continue
        source = open(os.path.join(service_dir, name), encoding="utf-8").read()
        if re.search(r"^\s*(import chromadb|from chromadb)", source, re.MULTILINE):
            offenders.append(name)
    assert not offenders, (
        f"{offenders} import chromadb directly. The adapter is the only module "
        f"allowed to — that is what keeps the pgvector fallback one file (ADR 0006)."
    )
