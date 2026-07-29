"""The retrieval eval harness — and the reason Week 2 is not about retrieval.

We were asked for "a retrieval helper that pulls the right past records." The
trap is that we can build exactly that, score it well against the contractor's
own gold-set, demo it successfully, and be **wrong**.

Read `db/seed/goldset.json` and then read `db/seed/encounters.csv`:

    gold case 1:  "show me Maria Gonzalez's allergies"
                  expected_patient_id: 1042
                  expected_answer:     "No known allergies on file."

    encounters:   1042  allergies: (empty)      meds: lisinopril
                  1330  allergies: PENICILLIN   meds: amoxicillin
                  1588  allergies: (empty)

Charts 1042, 1330 and 1588 are the same human — same SSN, same MRN, same address,
same phone. The penicillin allergy is on 1330.

**The gold-set encodes the bug as the correct answer.** A retrieval system that
scores 100% against it will confidently tell a clinician that Maria Gonzalez has
no known allergies. That is not a retrieval-quality problem and no amount of
chunking, reranking or embedding-model upgrade fixes it.

So the harness reports two families of number, in this order and always together:

    1. Standard retrieval metrics — recall, precision, groundedness, answer match.
       These will look FINE. They are the numbers she expects.
    2. Integrity metrics — duplicate rate, fragment coverage, and the concrete
       identity splits. These are the numbers that end the meeting.

An eval report showing only the first family would have *validated* the broken
system. That ordering is a deliberate design decision and should survive future
"can we simplify the dashboard" pressure.
"""
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

import corpus
import mpi
from config import settings
from embeddings import content_terms
from index_port import KIND_RECORD

GOLDSET_PATH = os.path.join(corpus.SEED_DIR, "goldset.json")  # resolved via corpus.SEED_DIR


@dataclass
class GoldCase:
    query: str
    expected_patient_id: int
    expected_answer: str
    cites_records: list[int] = field(default_factory=list)
    # Derived, not authored by the contractor: the content words their expected
    # answer commits to. `answer_match` is measured against this rather than
    # against a vague "contains the key facts", which is untestable.
    key_facts: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    query: str
    expected_patient_id: int
    retrieved_patient_ids: list[int]
    context_recall: float
    context_precision: float
    grounded: bool
    refused: bool
    answer_match: float
    answer: str
    # integrity
    identity_cluster: list[int] = field(default_factory=list)
    fragments_total: int = 1
    fragments_retrieved: int = 0
    fragment_coverage: float = 1.0
    clinically_incomplete: bool = False
    note: str = ""
    # what the same query returns once the fragments are linked
    linked_answer: str = ""
    linked_fragment_coverage: float = 1.0
    linked_recovers_allergy: bool = False


@dataclass
class EvalRun:
    run_id: str
    mode: str
    embed_backend: str
    metrics: dict
    integrity: dict
    cases: list[dict]
    corpus: dict
    warnings: list[str] = field(default_factory=list)
    linked_metrics: dict = field(default_factory=dict)


def load_goldset(path: Optional[str] = None) -> list[GoldCase]:
    path = path or corpus.seed_path("goldset.json")
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    cases: list[GoldCase] = []
    for case in raw.get("cases", []):
        expected = case.get("expected_answer", "")
        cases.append(GoldCase(
            query=case["query"],
            expected_patient_id=int(case["expected_patient_id"]),
            expected_answer=expected,
            cites_records=list(case.get("cites_records", [])),
            key_facts=sorted(content_terms(expected)),
        ))
    return cases


# --------------------------------------------------------------------------- #
# the integrity half
# --------------------------------------------------------------------------- #
# Stemmed forms, because `content_terms` stems: "allergies" -> "allergi".
# Getting this wrong makes the check silently never fire, which is the worst
# possible failure for a safety signal — so it is asserted by a test.
_ALLERGY_TERMS = {"allergy", "allergi", "allergie", "allergic", "penicillin", "pcn"}

# Named substances. Distinct from the terms above because every encounter renders
# an "Allergies:" line, so the generic word appears in the context of a patient
# with no allergies just as reliably as one with a documented allergy. Only a
# named substance proves the record was actually recovered.
_ALLERGY_SUBSTANCES = {
    "penicillin", "pcn", "sulfa", "latex", "peanut", "shellfish", "iodine",
    "codeine", "aspirin", "amoxicillin", "cephalosporin",
}


def _clinically_incomplete(case: GoldCase, cluster_ids: list[int],
                           retrieved_ids: list[int],
                           encounters: list[corpus.Encounter]) -> tuple[bool, str]:
    """Did we miss a fragment that carries clinically decisive information?

    Deliberately narrow: we only claim this when a fragment we did NOT retrieve
    records an allergy and the query was about allergies. A broad "you missed a
    fragment" claim would be noise; this one is a specific, checkable statement
    that the answer we produced could get someone hurt.
    """
    if not (content_terms(case.query) & _ALLERGY_TERMS):
        return False, ""
    missed = [pid for pid in cluster_ids if pid not in retrieved_ids]
    for enc in encounters:
        if enc.patient_id in missed and enc.allergies.strip():
            return True, (
                f"chart {enc.patient_id} records '{enc.allergies.strip()}' and was "
                f"not retrieved; the answer therefore reports a clean allergy "
                f"history for a patient who has a documented allergy"
            )
    return False, ""


def run(
    index,
    *,
    k: Optional[int] = None,
    mode: Optional[str] = None,
    goldset: Optional[list[GoldCase]] = None,
    graph=None,
) -> EvalRun:
    import rag_graph

    cases = goldset if goldset is not None else load_goldset()
    patients = corpus.load_patients()
    encounters = corpus.load_encounters()
    clusters = mpi.resolve(patients, settings.mpi_match_threshold)

    mode = mode or settings.retrieval_mode
    k = k or settings.retrieve_k
    graph = graph or rag_graph.build_graph(index)

    results: list[CaseResult] = []
    for case in cases:
        cluster = mpi.cluster_for(clusters, case.expected_patient_id)
        cluster_ids = cluster.patient_ids if cluster else [case.expected_patient_id]

        # ------------------------------------------------------------------ #
        # AS BUILT — the system as it exists today.
        #
        # There is no MPI and no match key, so the only chart the system knows
        # about for this query is the single patient_id the gold-set names. This
        # is not a pessimistic assumption: it is literally what happens when a
        # clinician opens one of the three charts.
        # ------------------------------------------------------------------ #
        out = rag_graph.run(
            index, case.query, k=k, mode=mode, kind=KIND_RECORD,
            patient_scope=[case.expected_patient_id], graph=graph,
        )

        retrieved_ids = sorted({
            int(r["patient_id"]) for r in out.retrieved if r.get("patient_id") is not None
        })

        # ------------------------------------------------------------------ #
        # LINKED — the same query if ADR 0007's match key existed and the three
        # fragments carried a SAME_AS link. Same corpus, same retriever, same
        # embeddings. The ONLY difference is that the system knows who the
        # patient is. Any delta between these two runs is attributable to the
        # missing match key and to nothing else.
        # ------------------------------------------------------------------ #
        linked = rag_graph.run(
            index, case.query, k=k, mode=mode, kind=KIND_RECORD,
            patient_scope=cluster_ids, graph=graph,
        )
        linked_ids = sorted({
            int(r["patient_id"]) for r in linked.retrieved if r.get("patient_id") is not None
        })
        linked_coverage = round(
            len([p for p in cluster_ids if p in linked_ids]) / max(1, len(cluster_ids)), 4
        )

        # Standard metrics, measured against what the CONTRACTOR called relevant.
        relevant = {case.expected_patient_id}
        hits = [r for r in out.retrieved if r.get("patient_id") in relevant]
        recall = 1.0 if hits else 0.0
        precision = round(len(hits) / len(out.retrieved), 4) if out.retrieved else 0.0

        expected_terms = set(case.key_facts)
        answer_terms = content_terms(out.answer)
        answer_match = (
            round(len(expected_terms & answer_terms) / len(expected_terms), 4)
            if expected_terms else 0.0
        )

        fragments_total = len(cluster_ids)
        fragments_retrieved = len([pid for pid in cluster_ids if pid in retrieved_ids])
        coverage = round(fragments_retrieved / fragments_total, 4) if fragments_total else 1.0

        incomplete, note = _clinically_incomplete(case, cluster_ids, retrieved_ids, encounters)

        # Recovery is measured on the RETRIEVED CONTEXT, not on the generated
        # answer. The context is a property of the system; the answer is a
        # property of whichever model happens to be wired in, and in CI that is a
        # deterministic stub. Measuring the stub's prose would make this metric
        # depend on the fixture rather than on the fix.
        linked_context_terms: set[str] = set()
        for chunk in linked.retrieved:
            linked_context_terms |= content_terms(chunk.get("text", ""))
        # A named substance, not the word "allergies" — every encounter renders an
        # allergy line, so the generic word proves nothing.
        recovered = bool(incomplete and (linked_context_terms & _ALLERGY_SUBSTANCES))

        results.append(CaseResult(
            linked_answer=linked.answer,
            linked_fragment_coverage=linked_coverage,
            linked_recovers_allergy=recovered,
            query=case.query,
            expected_patient_id=case.expected_patient_id,
            retrieved_patient_ids=retrieved_ids,
            context_recall=recall,
            context_precision=precision,
            grounded=out.grounded,
            refused=out.refused,
            answer_match=answer_match,
            answer=out.answer,
            identity_cluster=cluster_ids,
            fragments_total=fragments_total,
            fragments_retrieved=fragments_retrieved,
            fragment_coverage=coverage,
            clinically_incomplete=incomplete,
            note=note,
        ))

    n = max(1, len(results))
    metrics = {
        "context_recall": round(sum(r.context_recall for r in results) / n, 4),
        "context_precision": round(sum(r.context_precision for r in results) / n, 4),
        "groundedness": round(sum(1 for r in results if r.grounded) / n, 4),
        "answer_match": round(sum(r.answer_match for r in results) / n, 4),
    }

    fragmented = [c for c in clusters if c.is_fragmented]
    integrity = {
        "duplicate_patient_rate": mpi.duplicate_rate(clusters),
        "distinct_humans": len(clusters),
        "patient_rows": len(patients),
        "fragmented_humans": len(fragmented),
        "fragment_coverage": round(
            sum(r.fragment_coverage for r in results) / n, 4
        ),
        "clinically_incomplete_answers": sum(1 for r in results if r.clinically_incomplete),
        "identity_split_examples": [
            {
                "display_name": c.display_name,
                "patient_ids": c.patient_ids,
                "certain": c.certain,
                "reasons": c.reasons,
            }
            for c in fragmented
        ],
    }

    linked_metrics = {
        "fragment_coverage": round(
            sum(r.linked_fragment_coverage for r in results) / n, 4
        ),
        "allergies_recovered": sum(1 for r in results if r.linked_recovers_allergy),
    }

    warnings: list[str] = []
    if integrity["clinically_incomplete_answers"]:
        warnings.append(
            "At least one gold answer is CLINICALLY INCOMPLETE. The retrieval was "
            "CORRECT against the contractor's gold-set — and the gold-set is "
            "wrong. No retrieval tuning fixes this; it is a data-integrity "
            "defect. See the per-case notes."
        )
    if metrics["context_recall"] >= 0.8 and integrity["fragment_coverage"] <= 0.6:
        warnings.append(
            f"Retrieval recall is {metrics['context_recall']} while fragment "
            f"coverage is {integrity['fragment_coverage']}. Both numbers are "
            f"true. The retriever is finding what was indexed; what was indexed "
            f"is a fraction of the patient."
        )
    if linked_metrics["allergies_recovered"]:
        warnings.append(
            f"With the ADR 0007 match key in place, {linked_metrics['allergies_recovered']} "
            f"answer(s) recover a documented allergy that today's system misses. "
            f"Same corpus, same retriever, same embeddings — the only change is "
            f"that the system knows who the patient is."
        )

    return EvalRun(
        run_id=uuid.uuid4().hex[:12],
        mode=mode,
        embed_backend=getattr(index.embedder.backend, "name", "unknown"),
        metrics=metrics,
        integrity=integrity,
        cases=[asdict(r) for r in results],
        corpus=asdict(index.stats()),
        warnings=warnings,
        linked_metrics=linked_metrics,
    )


def render_report(run_: EvalRun) -> str:
    """Plain-text report. Standard metrics first, integrity second, always both."""
    lines = [
        f"RAG evaluation — run {run_.run_id}",
        f"  retrieval mode: {run_.mode}   embeddings: {run_.embed_backend}",
        f"  corpus: {run_.corpus.get('chunks')} chunks / {run_.corpus.get('documents')} documents",
        "",
        "RETRIEVAL QUALITY (what was asked for)",
    ]
    for key, value in run_.metrics.items():
        lines.append(f"  {key:<20} {value}")
    if run_.metrics.get("answer_match", 1.0) < 0.5:
        lines.append(
            "  note: answer_match is low because our answers do not reproduce the "
            "contractor's\n        expected phrasing. For gold case 1 that is "
            "correct behaviour — their expected\n        answer ('No known "
            "allergies on file.') is clinically wrong."
        )
    lines += ["", "DATA INTEGRITY (what we found)"]
    integrity = run_.integrity
    for key in ("patient_rows", "distinct_humans", "fragmented_humans",
                "duplicate_patient_rate", "fragment_coverage",
                "clinically_incomplete_answers"):
        lines.append(f"  {key:<32} {integrity.get(key)}")

    if integrity.get("identity_split_examples"):
        lines += ["", "  IDENTITY SPLITS"]
        for split in integrity["identity_split_examples"]:
            lines.append(
                f"    {split['display_name']}: charts {split['patient_ids']} "
                f"({'certain' if split['certain'] else 'probable'}) "
                f"— {', '.join(split['reasons'])}"
            )

    incomplete = [c for c in run_.cases if c.get("clinically_incomplete")]
    if incomplete:
        lines += ["", "  ⚠ CLINICALLY INCOMPLETE ANSWERS"]
        for case in incomplete:
            lines.append(f"    query:          {case['query']}")
            lines.append(f"    charts today:   {case['retrieved_patient_ids']} "
                         f"of {case['identity_cluster']}")
            lines.append(f"    answer today:   {case['answer']}")
            lines.append(f"    why it matters: {case['note']}")
            lines.append(f"    once linked:    allergy record recovered = "
                         f"{case.get('linked_recovers_allergy')}")

    if run_.linked_metrics:
        lines += ["", "WITH THE PROPOSED MATCH KEY (ADR 0007)"]
        lines.append(
            f"  fragment_coverage                "
            f"{run_.integrity.get('fragment_coverage')} -> "
            f"{run_.linked_metrics.get('fragment_coverage')}"
        )
        lines.append(
            f"  allergies recovered              "
            f"{run_.linked_metrics.get('allergies_recovered')}"
        )

    if run_.warnings:
        lines += [""] + [f"  ! {w}" for w in run_.warnings]
    return "\n".join(lines)
