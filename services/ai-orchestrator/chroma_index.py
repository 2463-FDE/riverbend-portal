"""ChromaDB adapter — the ONLY module permitted to import chromadb (ADR 0006).

Deployment shape:
  dev / CI    ``chromadb.EphemeralClient()`` or ``PersistentClient(path=...)``
  production  ``chromadb.HttpClient(host, port)`` against the compose service

Two collections, and the difference is a compliance boundary
------------------------------------------------------------
``riverbend_knowledge`` holds clinic policy and procedure text. Open to any
authenticated session.

``riverbend_records`` holds patient chart text. **It is a PHI store.** Every query
against it must carry an authorized patient scope, and this adapter raises
``ScopeRequired`` if one is missing. That is deliberate: an unscoped similarity
search over patient records is the vector-store form of the IDOR we find in Week
4, and it would be far harder to notice, because a vector store has no obvious
"WHERE patient_id" for a reviewer to look for and find absent.

Retrieval
---------
``dense`` cosine over embeddings, ``sparse`` BM25-style lexical, ``hybrid``
weighted Reciprocal Rank Fusion of the two. RRF fuses *ranks*, so it needs no
score normalisation between two differently-scaled retrievers.

Hybrid is the default because clinic queries are lexically loaded — "penicillin",
"A1C", "fasting", an MRN. Those are exact tokens that dense retrieval smears, and
they are precisely the terms a clinical error turns on.

Note on scores: Chroma returns cosine **distance** (0 = identical). We convert to
similarity as ``1 - distance`` at the boundary so nothing above this file has to
remember which direction is better.
"""
import math
import re
import threading
from typing import Optional, Sequence

from config import settings
from embeddings import Embedder, content_terms, tokenize
from index_port import (
    KIND_KNOWLEDGE,
    KIND_RECORD,
    IndexChunk,
    IndexStats,
    Retrieved,
    ScopeRequired,
)

_lock = threading.RLock()

# Chroma requires 3-512 chars from [a-zA-Z0-9._-], starting and ending alphanumeric.
COLLECTIONS = {
    KIND_KNOWLEDGE: "riverbend_knowledge",
    KIND_RECORD: "riverbend_records",
}


class ChromaIndex:
    def __init__(self, client=None, embedder: Optional[Embedder] = None):
        self._client = client
        self.embedder = embedder or Embedder()
        self._collections: dict[str, object] = {}

    # -- client ------------------------------------------------------------ #
    def _chroma(self):
        if self._client is not None:
            return self._client
        import chromadb

        if settings.chroma_mode == "http":
            self._client = chromadb.HttpClient(
                host=settings.chroma_host, port=settings.chroma_port
            )
        elif settings.chroma_path:
            self._client = chromadb.PersistentClient(path=settings.chroma_path)
        else:
            self._client = chromadb.EphemeralClient()
        return self._client

    def _collection(self, kind: str):
        name = COLLECTIONS.get(kind)
        if not name:
            raise ValueError(f"unknown collection kind {kind!r}")
        if name not in self._collections:
            self._collections[name] = self._chroma().get_or_create_collection(
                name, configuration={"hnsw": {"space": "cosine"}}
            )
        return self._collections[name]

    # -- write ------------------------------------------------------------- #
    def add(self, chunks: Sequence[IndexChunk]) -> int:
        chunks = list(chunks)
        if not chunks:
            return 0
        with _lock:
            by_kind: dict[str, list[IndexChunk]] = {}
            for chunk in chunks:
                by_kind.setdefault(chunk.kind, []).append(chunk)

            total = 0
            for kind, group in by_kind.items():
                if kind == KIND_RECORD and any(c.patient_id is None for c in group):
                    raise ValueError(
                        "a record-collection chunk must carry patient_id — it is "
                        "the field the authorization scope filters on"
                    )
                vectors = self.embedder.embed([c.text for c in group])
                self._collection(kind).upsert(
                    ids=[c.id for c in group],
                    documents=[c.text for c in group],
                    embeddings=vectors,
                    metadatas=[c.metadata() for c in group],
                )
                total += len(group)
            return total

    def delete_document(self, doc_id: str) -> int:
        with _lock:
            removed = 0
            for kind in COLLECTIONS:
                col = self._collection(kind)
                existing = col.get(where={"doc_id": {"$eq": doc_id}})
                ids = existing.get("ids") or []
                if ids:
                    col.delete(ids=ids)
                    removed += len(ids)
            return removed

    def reset(self) -> None:
        with _lock:
            for kind in COLLECTIONS:
                col = self._collection(kind)
                existing = col.get()
                ids = existing.get("ids") or []
                if ids:
                    col.delete(ids=ids)
            self.embedder.clear_cache()
            self.embedder.stats.calls = 0
            self.embedder.stats.cache_hits = 0
            self.embedder.stats.batches = 0

    # -- read -------------------------------------------------------------- #
    def count(self, kind: Optional[str] = None) -> int:
        with _lock:
            kinds = [kind] if kind else list(COLLECTIONS)
            return sum(self._collection(k).count() for k in kinds)

    def documents(self) -> list[dict]:
        with _lock:
            out: dict[str, dict] = {}
            for kind in COLLECTIONS:
                got = self._collection(kind).get()
                for meta in got.get("metadatas") or []:
                    doc_id = meta.get("doc_id", "")
                    row = out.setdefault(doc_id, {
                        "doc_id": doc_id,
                        "title": meta.get("doc_title", ""),
                        "kind": meta.get("kind", kind),
                        "source": meta.get("source", ""),
                        "added_by": meta.get("added_by", ""),
                        "chunks": 0,
                    })
                    row["chunks"] += 1
            return sorted(out.values(), key=lambda r: r["doc_id"])

    def stats(self) -> IndexStats:
        with _lock:
            by_kind = {k: self._collection(k).count() for k in COLLECTIONS}
            return IndexStats(
                chunks=sum(by_kind.values()),
                documents=len(self.documents()),
                by_kind=by_kind,
                embed_calls=self.embedder.stats.calls,
                embed_cache_hits=self.embedder.stats.cache_hits,
            )

    # -- retrieval ---------------------------------------------------------- #
    def query(
        self,
        text: str,
        k: int = 4,
        *,
        kind: str = KIND_KNOWLEDGE,
        patient_scope: Optional[Sequence[int]] = None,
        mode: str = "hybrid",
    ) -> list[Retrieved]:
        if kind == KIND_RECORD and patient_scope is None:
            raise ScopeRequired(
                "a query against the patient-record collection must carry an "
                "authorized patient scope. An unscoped similarity search over "
                "charts is an IDOR that no reviewer would see."
            )
        if not (text or "").strip():
            return []

        with _lock:
            where = None
            if patient_scope is not None:
                ids = [int(p) for p in patient_scope]
                if not ids:
                    return []          # an empty scope authorizes nothing
                where = {"patient_id": {"$in": ids}}

            col = self._collection(kind)
            pool = col.get(where=where)
            pool_ids = pool.get("ids") or []
            if not pool_ids:
                return []

            docs = pool.get("documents") or []
            metas = pool.get("metadatas") or []
            by_id = {
                cid: (docs[i] if i < len(docs) else "",
                      metas[i] if i < len(metas) else {})
                for i, cid in enumerate(pool_ids)
            }

            dense = self._dense(col, text, where, len(pool_ids)) if mode in ("dense", "hybrid") else {}
            sparse = self._sparse(text, by_id) if mode in ("sparse", "hybrid") else {}

            if mode == "dense":
                fused = dense
            elif mode == "sparse":
                fused = sparse
            else:
                fused = _rrf(
                    [_ranked(dense), _ranked(sparse)],
                    weights=[settings.hybrid_dense_weight, settings.hybrid_sparse_weight],
                )

            ranked = sorted(fused, key=lambda cid: (-fused[cid], cid))[:k]
            out: list[Retrieved] = []
            for cid in ranked:
                text_, meta = by_id.get(cid, ("", {}))
                out.append(Retrieved(
                    id=cid,
                    text=text_,
                    doc_id=meta.get("doc_id", ""),
                    doc_title=meta.get("doc_title", ""),
                    chunk_index=int(meta.get("chunk_index", 0)),
                    kind=meta.get("kind", kind),
                    source=meta.get("source", ""),
                    patient_id=meta.get("patient_id"),
                    score=round(fused[cid], 6),
                    dense_score=round(dense.get(cid, 0.0), 6),
                    sparse_score=round(sparse.get(cid, 0.0), 6),
                ))
            return out

    def _dense(self, col, text: str, where, pool_size: int) -> dict[str, float]:
        vector = self.embedder.embed_one(text)
        res = col.query(
            query_embeddings=[vector],
            n_results=max(1, min(pool_size, 100)),
            where=where,
        )
        ids = (res.get("ids") or [[]])[0]
        distances = (res.get("distances") or [[]])[0]
        # Chroma returns cosine DISTANCE (0 = identical). Convert once, here, so
        # nothing above this file has to remember which direction is better.
        return {cid: 1.0 - float(dist) for cid, dist in zip(ids, distances)}

    @staticmethod
    def _sparse(query: str, by_id: dict) -> dict[str, float]:
        """BM25-lite: idf-weighted term overlap over the (scoped) pool."""
        q_terms = [t for t in content_terms(query)]
        if not q_terms or not by_id:
            return {}
        n = len(by_id)
        doc_terms = {cid: tokenize(text) for cid, (text, _m) in by_id.items()}
        df: dict[str, int] = {}
        for terms in doc_terms.values():
            for t in set(terms):
                df[t] = df.get(t, 0) + 1
        avgdl = sum(len(t) for t in doc_terms.values()) / max(1, n)
        k1, b = 1.5, 0.75
        q_set = set(q_terms)

        scores: dict[str, float] = {}
        for cid, terms in doc_terms.items():
            if not terms:
                continue
            dl = len(terms)
            tf: dict[str, int] = {}
            for t in terms:
                if t in q_set:
                    tf[t] = tf.get(t, 0) + 1
            score = 0.0
            for t, freq in tf.items():
                idf = math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
                score += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * dl / avgdl))
            if score > 0:
                scores[cid] = score
        return scores


def _ranked(scores: dict[str, float]) -> list[str]:
    return sorted(scores, key=lambda cid: (-scores[cid], cid))


def _rrf(rank_lists: list[list[str]], weights: Optional[list[float]] = None,
         k: int = 60) -> dict[str, float]:
    """Weighted Reciprocal Rank Fusion.

    Fuses ranks rather than scores, so a cosine similarity in [0,1] and an
    unbounded BM25 score can be combined without normalising either.
    """
    weights = weights or [1.0] * len(rank_lists)
    fused: dict[str, float] = {}
    for weight, ranked in zip(weights, rank_lists):
        for rank, cid in enumerate(ranked):
            fused[cid] = fused.get(cid, 0.0) + weight / (k + rank + 1)
    return fused
