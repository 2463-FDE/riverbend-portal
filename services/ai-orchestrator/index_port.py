"""The vector-index port — ADR 0006.

Application code talks to `KnowledgeIndex`, never to Chroma. The adapter
(`chroma_index.py`) is the only module in the service permitted to import
`chromadb`, and a test asserts that boundary.

Why bother, given the engagement mandates Chroma? Because the Senior Engineer's
objection in the design debate was recorded and accepted: Postgres is already in
this stack, already backed up, already in the runbook, and `pgvector` would put
the vectors beside the relational data they derive from. We took the mandate. The
port is the price: if Chroma becomes a liability, swapping it is one file rather
than a refactor.

Two collections, not one
------------------------
`KIND_KNOWLEDGE` — clinic policy and procedure documents. Shared, readable by any
authenticated session.

`KIND_RECORD` — patient chart text. **This collection is a PHI store** and is
governed as one: every query MUST carry an authorized patient scope, and the
adapter refuses a record-collection query that does not. That refusal is a
`ValueError`, not a warning, because an unscoped query over patient records is
the vector-store version of the IDOR we find in Week 4.
"""
from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence

KIND_KNOWLEDGE = "knowledge"
KIND_RECORD = "record"


class ScopeRequired(ValueError):
    """Raised when a PHI-collection query arrives without a patient scope."""


@dataclass(frozen=True)
class IndexChunk:
    """One embeddable unit of text plus the metadata retrieval needs."""

    id: str
    text: str
    doc_id: str
    doc_title: str
    chunk_index: int
    kind: str = KIND_KNOWLEDGE
    source: str = ""
    added_by: str = ""
    # Only set for KIND_RECORD. This is the field the scope filter matches on.
    patient_id: Optional[int] = None

    def metadata(self) -> dict:
        meta = {
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "chunk_index": self.chunk_index,
            "kind": self.kind,
            "source": self.source,
            "added_by": self.added_by,
        }
        if self.patient_id is not None:
            meta["patient_id"] = int(self.patient_id)
        return meta


@dataclass
class Retrieved:
    id: str
    text: str
    doc_id: str
    doc_title: str
    chunk_index: int
    kind: str
    source: str
    score: float                 # higher is better, regardless of backend
    patient_id: Optional[int] = None
    dense_score: float = 0.0
    sparse_score: float = 0.0

    def citation(self) -> str:
        return f"{self.doc_title}#{self.chunk_index}"


@dataclass
class IndexStats:
    chunks: int = 0
    documents: int = 0
    by_kind: dict = field(default_factory=dict)
    embed_calls: int = 0
    embed_cache_hits: int = 0


class KnowledgeIndex(Protocol):
    def add(self, chunks: Sequence[IndexChunk]) -> int: ...

    def query(
        self,
        text: str,
        k: int = 4,
        *,
        kind: str = KIND_KNOWLEDGE,
        patient_scope: Optional[Sequence[int]] = None,
        mode: str = "hybrid",
    ) -> list[Retrieved]: ...

    def delete_document(self, doc_id: str) -> int: ...

    def count(self, kind: Optional[str] = None) -> int: ...

    def documents(self) -> list[dict]: ...

    def stats(self) -> IndexStats: ...

    def reset(self) -> None: ...
