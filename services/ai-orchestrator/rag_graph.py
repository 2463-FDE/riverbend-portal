"""The RAG pipeline as a LangGraph v1 StateGraph.

Why a graph here, and not in Week 1
-----------------------------------
Week 1 was one request in, one summary out — a straight line, and wrapping it in
a graph runtime would have been cost with no benefit (ADR 0004 §2, debate D2).

Retrieval is genuinely different. It has two real branch points, and both of them
have to be first-class rather than exception paths:

    retrieve ─► relevance_gate ─┬─► refuse            "nothing relevant came back"
                                └─► generate ─► ground_gate ─┬─► answer
                                                             └─► refuse
                                                                 "the answer isn't
                                                                  supported by what
                                                                  we retrieved"

`refuse` is a successful outcome. A retrieval assistant that would rather guess
than say "I don't have that" is worse than no assistant, because a clinician
cannot tell the difference between a confident answer and a confident guess.

The graph also gives Week 7's output guardrail somewhere to hang without
reopening this design: it becomes a node between `generate` and `ground_gate`.

LangGraph v1 surface used here: `StateGraph`, `add_node`, `add_edge`,
`add_conditional_edges`, `compile()`, `START` / `END`. Pinned `>=1.0,<2.0`.
"""
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Optional, TypedDict

import guardrails
import model_client
from config import settings
from embeddings import content_terms
from index_port import KIND_KNOWLEDGE, KIND_RECORD, Retrieved

REFUSAL = "I don't have that information in the knowledge base."


class RagState(TypedDict, total=False):
    # inputs
    query: str
    k: int
    mode: str
    kind: str
    patient_scope: Optional[list[int]]
    # working
    retrieved: list[Retrieved]
    relevance: float
    top_similarity: float
    # outputs
    answer: str
    grounded: bool
    refused: bool
    reason: str
    citations: list[dict]
    usage: dict
    path: Annotated[list[str], lambda a, b: (a or []) + (b or [])]


@dataclass
class RagResult:
    query: str
    answer: str
    grounded: bool
    refused: bool
    reason: str
    citations: list[dict] = field(default_factory=list)
    retrieved: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    path: list[str] = field(default_factory=list)


_RAG_SYSTEM = (
    "You are a Riverbend clinic knowledge assistant. Answer the question USING "
    "ONLY the numbered context passages provided. Every claim must be supported "
    "by the context. Cite the passages you used with bracketed numbers like [1]. "
    "If the context does not contain the answer, say you do not have that "
    "information — do not guess. Respond ONLY with a JSON object of the form "
    '{"answer": "..."} and nothing else.'
)


def format_context(chunks: list[Retrieved]) -> str:
    return "\n\n".join(
        f"[{i + 1}] (source: {c.doc_title}) {c.text}" for i, c in enumerate(chunks)
    )


def split_sentences(text: str) -> list[str]:
    """Sentence split that survives clinical text.

    A naive ``(?<=[.!?])\\s+`` split shreds "M. Gonzalez" into "M." and
    "Gonzalez" — and "M. Gonzalez" is one of the three chart fragments we are
    specifically trying to reason about. So fragments that end in a single-letter
    abbreviation, or that are too short to be a sentence, are merged forward.
    """
    import re

    raw = re.split(r"(?<=[.!?])\s+", (text or "").replace("\n", " "))
    out: list[str] = []
    for part in raw:
        part = part.strip()
        if not part:
            continue
        if out and (len(out[-1]) < 12 or re.search(r"\b[A-Z]\.$", out[-1])):
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return out


def _stub_answer(question: str, chunks: list[Retrieved]) -> str:
    """A GROUNDED stub: answers from the retrieved context, never invents.

    Picks the context sentence best matching the question and cites the passage
    it came from. Dev and CI therefore produce faithful output; tests inject
    ungrounded text explicitly when they want to exercise the guardrail, so a
    hallucination is visible in the test rather than ambient in the fixture.

    Terms are weighted by inverse frequency across the retrieved context. Without
    that, "show me Maria Gonzalez's allergies" matches the sentence containing
    her *name* — which appears in every chunk of her chart — rather than the one
    containing her *allergies*, which is the whole question. The rare term is the
    informative one.
    """
    import json
    import math

    q_terms = content_terms(question)
    if not q_terms:
        return json.dumps({"answer": REFUSAL})

    sentences: list[tuple[int, str, set]] = []
    for i, chunk in enumerate(chunks):
        for sentence in split_sentences(chunk.text):
            if len(sentence) > 3:
                sentences.append((i + 1, sentence, content_terms(sentence)))
    if not sentences:
        return json.dumps({"answer": REFUSAL})

    df: dict[str, int] = {}
    for _m, _s, terms in sentences:
        for term in terms:
            df[term] = df.get(term, 0) + 1
    total = len(sentences)

    best, best_marker, best_score = "", 1, 0.0
    for marker, sentence, terms in sentences:
        score = sum(
            math.log(1 + total / (1 + df.get(term, 0)))
            for term in (q_terms & terms)
        )
        if score > best_score:
            best, best_marker, best_score = sentence, marker, score

    if not best:
        return json.dumps({"answer": REFUSAL})
    return json.dumps({"answer": f"{best} [{best_marker}]"})


# --------------------------------------------------------------------------- #
# nodes
# --------------------------------------------------------------------------- #
def build_graph(index, client: Optional[model_client.ModelClient] = None):
    """Compile the RAG graph against a KnowledgeIndex."""
    from langgraph.graph import END, START, StateGraph

    client = client or model_client.ModelClient(settings.rag_model_id)

    def retrieve(state: RagState) -> dict:
        chunks = index.query(
            state["query"],
            k=state.get("k") or settings.retrieve_k,
            kind=state.get("kind") or KIND_KNOWLEDGE,
            patient_scope=state.get("patient_scope"),
            mode=state.get("mode") or settings.retrieval_mode,
        )
        return {"retrieved": chunks, "path": ["retrieve"]}

    def relevance_gate(state: RagState) -> dict:
        """Two independent floors, because they fail differently.

        Term coverage is corpus-size independent and is the primary gate. Dense
        similarity is the paraphrase fallback for a query that shares meaning but
        not vocabulary. A query has to fail BOTH to be refused.
        """
        chunks = state.get("retrieved") or []
        if not chunks:
            return {"relevance": 0.0, "top_similarity": 0.0, "path": ["relevance_gate"]}
        q_terms = content_terms(state["query"])
        top = chunks[0]
        coverage = (
            len(q_terms & content_terms(top.text)) / len(q_terms) if q_terms else 0.0
        )
        similarity = max((c.dense_score for c in chunks), default=0.0)
        return {
            "relevance": round(coverage, 4),
            "top_similarity": round(similarity, 4),
            "path": ["relevance_gate"],
        }

    def route_relevance(state: RagState) -> str:
        """Term coverage is the gate; dense similarity is the paraphrase fallback.

        The fallback only applies where the dense score MEANS something. Measured,
        the offline embedder does not separate relevant from irrelevant queries
        (relevant 0.107-0.427, irrelevant 0.049-0.295), so enabling it there
        would admit "how do I bake sourdough bread" on noise. Under Titan the
        populations separate cleanly (0.328-0.706 vs 0.047-0.170) and the
        fallback is what lets "what am I allergic to?" through despite sharing no
        vocabulary with the chart. See `settings.semantic_fallback_enabled`.
        """
        if not state.get("retrieved"):
            return "refuse"
        if state.get("relevance", 0.0) >= settings.min_term_coverage:
            return "generate"
        if (settings.semantic_fallback_enabled
                and state.get("top_similarity", 0.0) >= settings.min_semantic_score):
            return "generate"
        return "refuse"

    def generate(state: RagState) -> dict:
        chunks = state["retrieved"]
        context = format_context(chunks)
        try:
            result = client.invoke(
                _RAG_SYSTEM,
                f"Context passages:\n{context}\n\nQuestion: {state['query']}\n\n"
                f"Return JSON only.",
                structured_key="answer",
                stub_text=_stub_answer(state["query"], chunks),
                grounding_source=context,
            )
        except (model_client.BudgetError, model_client.GuardrailBlocked,
                model_client.ModelUnavailable) as e:
            return {
                "answer": REFUSAL,
                "reason": f"generation_failed:{type(e).__name__}",
                "usage": {},
                "path": ["generate"],
            }
        return {
            "answer": result.text,
            "usage": {
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "est_cost_usd": result.est_cost_usd,
                "stubbed": result.stubbed,
                "retrieved": len(chunks),
            },
            "path": ["generate"],
        }

    def ground_gate(state: RagState) -> dict:
        """Withdraw an answer that the retrieved context does not support.

        This runs AFTER generation on purpose: it is the only point at which we
        can compare what the model said against what we actually gave it.

        **This gate is deliberately UNCHANGED, and that is a finding.**

        It refuses answers that are correct. Measured live, faithful answers to
        the client's own questions score 0.481-0.600 on overlap, so roughly three
        in four are withheld -- which is the defect the client reported.

        Three replacements were built, measured and rejected (ADR 0016, revised):

          * lowering the threshold -- an answer that flatly CONTRADICTS the record
            ("you have no known drug allergies") scores 0.500, inside the faithful
            range. No value separates the populations;
          * `invented_clinical_claims` alone -- codex:rescue F1 showed it releases
            six fabrications the overlap gate blocked, including "You are
            pregnant" and "Your blood pressure was 160/100";
          * requiring every clinical term to be supported -- blocks all six, and
            also refuses every real answer, because model prose always contains
            words the record does not.

        So it stays strict and it stays wrong in the safe direction. Fixing this
        properly needs sentence-level entailment against the source, which is
        model work rather than a threshold. Tracked as D-14.

        `clinical_support` and `unsupported_clinical_terms` ship measured and
        unused for that work to build on.
        """
        if state.get("reason", "").startswith("generation_failed"):
            return {"grounded": False, "path": ["ground_gate"]}
        context = format_context(state["retrieved"])
        verdict = guardrails.check(state["answer"], context,
                                   settings.grounding_threshold, strict_terms=False)
        return {
            "grounded": verdict.grounded,
            "reason": "|".join(verdict.reasons) if verdict.reasons else "",
            "path": ["ground_gate"],
        }

    def route_ground(state: RagState) -> str:
        return "answer" if state.get("grounded") else "refuse"

    def answer(state: RagState) -> dict:
        citations = [
            {"marker": i + 1, "doc_id": c.doc_id, "doc_title": c.doc_title,
             "chunk_index": c.chunk_index, "score": c.score,
             "patient_id": c.patient_id}
            for i, c in enumerate(state["retrieved"])
        ]
        return {"refused": False, "citations": citations, "path": ["answer"]}

    def refuse(state: RagState) -> dict:
        return {
            "answer": REFUSAL,
            "grounded": False,
            "refused": True,
            "citations": [],
            "reason": state.get("reason") or "below_relevance_floor",
            "path": ["refuse"],
        }

    builder = StateGraph(RagState)
    builder.add_node("retrieve", retrieve)
    builder.add_node("relevance_gate", relevance_gate)
    builder.add_node("generate", generate)
    builder.add_node("ground_gate", ground_gate)
    builder.add_node("answer", answer)
    builder.add_node("refuse", refuse)

    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "relevance_gate")
    builder.add_conditional_edges("relevance_gate", route_relevance,
                                  {"generate": "generate", "refuse": "refuse"})
    builder.add_edge("generate", "ground_gate")
    builder.add_conditional_edges("ground_gate", route_ground,
                                  {"answer": "answer", "refuse": "refuse"})
    builder.add_edge("answer", END)
    builder.add_edge("refuse", END)

    return builder.compile()


def run(
    index,
    query: str,
    *,
    k: Optional[int] = None,
    mode: Optional[str] = None,
    kind: str = KIND_KNOWLEDGE,
    patient_scope: Optional[list[int]] = None,
    graph=None,
    client: Optional[model_client.ModelClient] = None,
) -> RagResult:
    graph = graph or build_graph(index, client)
    state: Any = graph.invoke({
        "query": query,
        "k": k or settings.retrieve_k,
        "mode": mode or settings.retrieval_mode,
        "kind": kind,
        "patient_scope": list(patient_scope) if patient_scope is not None else None,
    })
    return RagResult(
        query=query,
        answer=state.get("answer", REFUSAL),
        grounded=bool(state.get("grounded")),
        refused=bool(state.get("refused")),
        reason=state.get("reason", ""),
        citations=state.get("citations", []),
        retrieved=[asdict(c) for c in state.get("retrieved", [])],
        usage=state.get("usage", {}),
        path=state.get("path", []),
    )
