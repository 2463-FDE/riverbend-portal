"""W2 — the finding: one human, three charts, and an eval that says so.

These are the tests that carry the week's actual deliverable. The retrieval tests
prove the helper works; these prove the helper would have been *wrong* in the way
that matters, and that the report makes it impossible to miss.
"""
import pytest

from conftest import load_module

mpi = load_module("services/ai-orchestrator/mpi.py", "w2_mpi")
corpus = load_module("services/ai-orchestrator/corpus.py", "w2m_corpus")
chroma_index = load_module("services/ai-orchestrator/chroma_index.py", "w2m_chroma")
eval_harness = load_module("services/ai-orchestrator/eval_harness.py", "w2_eval")


@pytest.fixture
def index():
    import chromadb

    idx = chroma_index.ChromaIndex(client=chromadb.EphemeralClient())
    idx.add(corpus.build_knowledge_chunks())
    idx.add(corpus.build_record_chunks())
    return idx


@pytest.fixture
def run(index):
    return eval_harness.run(index)


# --------------------------------------------------------------------------- #
# identity resolution
# --------------------------------------------------------------------------- #
def test_name_key_blocks_the_three_spellings_together():
    """If they land in different blocks they are never even compared."""
    assert (
        mpi.name_key("Maria Gonzalez")
        == mpi.name_key("Maria Gonzales")
        == mpi.name_key("M. Gonzalez")
    )


def test_dob_transposition_is_detected():
    """1971-03-02 vs 1971-02-03 — a day/month swap.

    Not exotic: it is what happens when a US form and a patient's mental format
    disagree. `dob` is stored as TEXT, so the two strings are simply unequal and
    nothing in the system notices.
    """
    assert mpi.dob_is_transposition("1971-03-02", "1971-02-03")
    assert not mpi.dob_is_transposition("1971-03-02", "1971-03-02")
    assert not mpi.dob_is_transposition("1971-03-02", "1980-03-02")


def test_nickname_normalization():
    assert mpi.name_tokens("Bill Smith") == mpi.name_tokens("William Smith")


def test_the_three_maria_charts_resolve_to_one_human():
    clusters = mpi.resolve(corpus.load_patients())
    maria = mpi.cluster_for(clusters, 1042)
    assert maria is not None
    assert maria.patient_ids == [1042, 1330, 1588], (
        "the three fragments did not resolve to one human"
    )
    assert maria.certain, "identical SSN and MRN should make this a certain match"
    assert "identical_ssn" in maria.reasons
    assert "dob_day_month_transposed" in maria.reasons


def test_distinct_patients_are_not_merged():
    clusters = mpi.resolve(corpus.load_patients())
    obrien = mpi.cluster_for(clusters, 1043)
    assert obrien is not None
    assert obrien.patient_ids == [1043], (
        "merging two different humans creates a chart with one person's "
        "allergies and another's medications — a wrong merge is a NEW harm, "
        "which is why the threshold is conservative"
    )


def test_duplicate_rate_and_same_as_edges():
    clusters = mpi.resolve(corpus.load_patients())
    assert mpi.duplicate_rate(clusters) > 0
    edges = mpi.same_as_edges(clusters)
    assert (1042, 1330) in edges and (1042, 1588) in edges and (1330, 1588) in edges


# --------------------------------------------------------------------------- #
# the eval harness — standard metrics AND integrity metrics, together
# --------------------------------------------------------------------------- #
def test_eval_reports_the_four_standard_metrics(run):
    for key in ("context_recall", "context_precision", "groundedness", "answer_match"):
        assert key in run.metrics


def test_duplicate_rate_detects_the_seeded_fork(run):
    assert run.integrity["duplicate_patient_rate"] > 0
    splits = run.integrity["identity_split_examples"]
    assert splits, "the identity split was not surfaced as a concrete example"
    assert any(s["patient_ids"] == [1042, 1330, 1588] for s in splits)


def test_fragmented_patient_lowers_fragment_coverage(run):
    assert run.integrity["fragment_coverage"] < 1.0
    allergy_case = next(c for c in run.cases if "allergies" in c["query"])
    assert allergy_case["fragments_total"] == 3
    assert allergy_case["fragments_retrieved"] == 1
    assert allergy_case["fragment_coverage"] == pytest.approx(1 / 3, abs=1e-3)


def test_recall_is_high_while_coverage_is_low(run):
    """THE point of the week, as a single assertion.

    Both numbers are true at the same time. The retriever is finding what was
    indexed; what was indexed is a third of the patient. An eval reporting only
    the first number would have VALIDATED the broken system.
    """
    assert run.metrics["context_recall"] >= 0.8
    assert run.integrity["fragment_coverage"] <= 0.6
    assert any("recall" in w and "fragment coverage" in w for w in run.warnings)


def test_the_gold_answer_is_clinically_incomplete(run):
    """The contractor's gold-set encodes the bug as the correct answer.

    Gold case 1 expects "No known allergies on file." for Maria Gonzalez. The
    penicillin allergy is on chart 1330. A retriever scoring 100% against this
    gold-set tells a clinician she has no allergies.
    """
    assert run.integrity["clinically_incomplete_answers"] >= 1
    case = next(c for c in run.cases if c["clinically_incomplete"])
    assert "1330" in case["note"]
    assert "penicillin" in case["note"]
    assert any("gold-set is wrong" in w for w in run.warnings)


def test_linking_the_fragments_recovers_the_allergy(run):
    """Same corpus, same retriever, same embeddings.

    The only difference is that the system knows who the patient is. That delta
    is attributable to the missing match key and to nothing else.
    """
    assert run.linked_metrics["fragment_coverage"] == 1.0
    assert run.linked_metrics["allergies_recovered"] >= 1
    case = next(c for c in run.cases if c["clinically_incomplete"])
    assert case["linked_recovers_allergy"] is True


def test_allergy_terms_are_stemmed_correctly():
    """A safety signal that silently never fires is worse than no signal.

    `content_terms` stems, so "allergies" becomes "allergi". If the term set
    holds only the unstemmed forms, `clinically_incomplete` is always False and
    the report looks clean.
    """
    from embeddings import content_terms

    assert content_terms("show me her allergies") & eval_harness._ALLERGY_TERMS


def test_report_puts_integrity_after_quality_and_shows_both(run):
    report = eval_harness.render_report(run)
    assert "RETRIEVAL QUALITY" in report
    assert "DATA INTEGRITY" in report
    assert report.index("RETRIEVAL QUALITY") < report.index("DATA INTEGRITY"), (
        "the narrative depends on this order: 'recall is 1.0' first, then "
        "'fragment coverage is 0.55'"
    )
    assert "IDENTITY SPLITS" in report
    assert "CLINICALLY INCOMPLETE" in report
    assert "1042" in report and "1330" in report and "1588" in report


def test_eval_is_reproducible(index):
    a = eval_harness.run(index)
    b = eval_harness.run(index)
    assert a.metrics == b.metrics
    assert a.integrity["fragment_coverage"] == b.integrity["fragment_coverage"]


# --------------------------------------------------------------------------- #
# quota discipline (RVB-W2-11)
# --------------------------------------------------------------------------- #
def test_corpus_cap_is_enforced(monkeypatch):
    monkeypatch.setattr(corpus.settings, "corpus_max_chunks", 2)
    with pytest.raises(corpus.CorpusCapExceeded):
        corpus.enforce_cap(corpus.build_record_chunks(), existing=0)


def test_record_text_keeps_clinical_content_and_strips_identifiers():
    chunks = corpus.build_record_chunks()
    blob = " ".join(c.text for c in chunks)
    assert "penicillin" in blob, "clinical content must survive the scrub"
    assert "2026-03-04" in blob, (
        "encounter dates must survive — a record corpus stripped of dates cannot "
        "answer 'what did her last three visits say?'"
    )
    assert "412-55-9981" not in blob, "an SSN must never reach the index"
    assert "310-555-0147" not in blob, "a phone number is a Safe-Harbor identifier"
