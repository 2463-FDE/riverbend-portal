"""The corpus can find the handover dump somewhere other than the repo root.

`corpus.py` resolved the seed directory relative to its own file, which works
from a checkout and only from a checkout. In the container the module sits at
`/app`, so `../..` resolved to `/db/seed` and every corpus read raised
FileNotFoundError -- identity clusters, the knowledge seed, the eval harness and
the patient view, all of it.

The visible symptom was worse than an error page. `_same_as_lookup` narrows scope
to the patient's own chart when identity resolution fails (correct: identity
resolution must never be able to *widen* access), so a broken corpus silently
narrowed every patient to one chart. Maria Gonzalez could not reach the chart
carrying her penicillin allergy -- the Week-4 authorization fix turning the Week-2
fragmentation into an access denial, live.

Nothing caught it because the entire suite runs from the repository root, where
the relative path happens to resolve. These tests are the ones that would have.

See docs/findings/w1ui-nothing-ever-ran-the-stack.md.
"""
import os
import sys

import pytest

from conftest import load_module


_SHARED = ("corpus", "eval_harness")


@pytest.fixture
def fresh_imports():
    """Re-import `corpus`/`eval_harness` under this test's environment, and undo it.

    Two hazards, and the cleanup half is the one that bites:

    `SEED_DIR` is evaluated at import time and `load_module` deliberately does
    not register anything in `sys.modules`, so `eval_harness`'s plain
    `import corpus` picks up whatever an EARLIER test file cached -- read before
    this test set the variable. Evicting first fixes that.

    But evicting only on the way in leaves a `corpus` bound to this test's
    tmp_path sitting in `sys.modules` for everything downstream, which took out
    nine unrelated tests. A test that reaches into the import system has to put
    it back.
    """
    for name in _SHARED:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name in _SHARED:
            sys.modules.pop(name, None)


def _corpus(monkeypatch, seed_dir=None):
    """Import corpus with RIVERBEND_SEED_DIR set (or explicitly unset)."""
    if seed_dir is None:
        monkeypatch.delenv("RIVERBEND_SEED_DIR", raising=False)
    else:
        monkeypatch.setenv("RIVERBEND_SEED_DIR", str(seed_dir))
    return load_module("services/ai-orchestrator/corpus.py", "seeddir_corpus")


def test_seed_dir_honours_the_environment(monkeypatch, tmp_path):
    """The container sets this. If it stops being read, the container breaks."""
    corpus = _corpus(monkeypatch, tmp_path)
    assert corpus.SEED_DIR == str(tmp_path)


def test_seed_dir_falls_back_to_the_checkout(monkeypatch):
    """Unset, it must still work for someone running from a clone."""
    corpus = _corpus(monkeypatch)
    assert corpus.SEED_DIR.endswith(os.path.join("db", "seed"))
    assert os.path.isdir(corpus.SEED_DIR)


def test_loaders_read_through_seed_dir(monkeypatch, tmp_path):
    """The knob has to be wired to the readers, not merely present.

    A SEED_DIR that every loader ignores is the same bug with a config option
    bolted on, so this reads a patient out of a directory that is NOT the repo.
    """
    (tmp_path / "patients.csv").write_text(
        "id,name,dob,ssn,mrn,address,phone,email,created_via\n"
        "9001,Test Patient,1980-01-01,000-00-0000,MRN9001,1 Test St,555-0000,"
        "t@example.com,front_desk\n"
    )
    corpus = _corpus(monkeypatch, tmp_path)

    rows = corpus.load_patients()
    assert [r.id for r in rows] == [9001], (
        "load_patients ignored SEED_DIR and read the repo copy instead"
    )


def test_a_missing_seed_file_names_the_directory_it_looked_in(monkeypatch, tmp_path):
    """The original failure was a bare FileNotFoundError swallowed upstream.

    Whatever the next path bug is, the message has to say where it looked --
    that is the difference between a five-minute fix and re-deriving the whole
    scope-narrowing chain from a 404.
    """
    corpus = _corpus(monkeypatch, tmp_path)

    with pytest.raises(FileNotFoundError) as exc:
        corpus.load_encounters()

    message = str(exc.value)
    assert str(tmp_path) in message
    assert "RIVERBEND_SEED_DIR" in message


def test_the_eval_harness_resolves_the_goldset_the_same_way(
    monkeypatch, tmp_path, fresh_imports
):
    """eval_harness had its own copy of the relative path, so it broke identically.

    It now goes through `corpus.seed_path`, which is the only reason one env var
    fixes both. Asserted so a future edit cannot quietly reintroduce a second
    resolution rule.
    """
    (tmp_path / "goldset.json").write_text(
        '{"cases": [{"query": "q", "expected_patient_id": 1042, '
        '"expected_answer": "Penicillin allergy confirmed."}]}'
    )
    monkeypatch.setenv("RIVERBEND_SEED_DIR", str(tmp_path))

    harness = load_module("services/ai-orchestrator/eval_harness.py", "seeddir_harness")
    cases = harness.load_goldset()

    assert [c.expected_patient_id for c in cases] == [1042]
