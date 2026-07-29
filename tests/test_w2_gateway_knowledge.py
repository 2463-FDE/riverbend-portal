"""W2 — the knowledge-base surface through the gateway.

Two things under test: the ingest capability gate (RVB-W2-13) and the end-to-end
path a clinician would actually use (RVB-W2-14).
"""
import sys

import pytest
from fastapi.testclient import TestClient

from conftest import load_module

_sessions: dict[str, dict] = {}


@pytest.fixture(scope="module")
def gateway():
    security = load_module("services/gateway/security.py", "w2_gw_security")
    security.get_session = lambda token: _sessions.get(token)
    sys.modules["security"] = security

    app_mod = load_module("services/gateway/app.py", "w2_gw_app")
    calls: list[tuple] = []
    app_mod._post = lambda service, path, payload: (
        calls.append((service, path, payload)) or {"ok": True}
    )
    app_mod._get = lambda service, path, params=None: (
        calls.append((service, path, params)) or {"ok": True}
    )
    app_mod.__calls__ = calls

    _sessions["staff-token"] = {"username": "frontdesk1", "role": "staff"}
    _sessions["admin-token"] = {"username": "kbadmin", "role": "knowledge_admin"}
    return app_mod


def _client(gateway):
    return TestClient(gateway.app)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# reads are open to any authenticated session
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("method,path", [
    ("post", "/ai/knowledge/query"),
    ("get", "/ai/knowledge/corpus"),
    ("post", "/ai/knowledge/eval"),
    ("get", "/ai/knowledge/eval/latest"),
    ("get", "/ai/knowledge/identity-clusters"),
])
def test_knowledge_reads_require_a_session(gateway, method, path):
    client = _client(gateway)
    resp = client.post(path, json={}) if method == "post" else client.get(path)
    assert resp.status_code == 401


def test_query_is_open_to_ordinary_staff(gateway):
    resp = _client(gateway).post(
        "/ai/knowledge/query", json={"query": "fasting rules"},
        headers=_auth("staff-token"),
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# writes are gated (RVB-W2-13)
# --------------------------------------------------------------------------- #
# The route these tests used to call, `/ai/knowledge/ingest`, is GONE (adr/0014
# §4a, codex F4). It wrote straight to the index after a lenient scrub, so a
# privileged user could bypass the preview entirely -- which made "every
# knowledge write passes a human gate" false while the ADR asserted it.
#
# The capability check they pin is unchanged and still worth pinning; it now
# guards `/ai/knowledge/stage`, phase one of the two-phase gate.
def test_ingest_is_forbidden_for_ordinary_staff(gateway):
    """One bad document silently changes every future grounded answer.

    Low volume, unbounded blast radius, no cheap undo — the debate's test for
    where a human gate belongs.
    """
    resp = _client(gateway).post(
        "/ai/knowledge/stage", json={"title": "T", "text": "some policy"},
        headers=_auth("staff-token"),
    )
    assert resp.status_code == 403


def test_ingest_is_allowed_for_the_knowledge_admin_role(gateway):
    resp = _client(gateway).post(
        "/ai/knowledge/stage", json={"title": "T", "text": "some policy"},
        headers=_auth("admin-token"),
    )
    assert resp.status_code == 200


def test_ingest_by_username_allowlist(gateway, monkeypatch):
    monkeypatch.setattr(gateway.authz.settings, "knowledge_ingest_users",
                        frozenset({"frontdesk1"}))
    resp = _client(gateway).post(
        "/ai/knowledge/stage", json={"title": "T", "text": "policy"},
        headers=_auth("staff-token"),
    )
    assert resp.status_code == 200


def test_added_by_is_server_stamped(gateway):
    """A caller must not be able to attribute their document to someone else."""
    _client(gateway).post(
        "/ai/knowledge/stage",
        json={"title": "T", "text": "policy", "added_by": "someone-else"},
        headers=_auth("admin-token"),
    )
    _service, _path, payload = gateway.__calls__[-1]
    assert payload["added_by"] == "kbadmin"


def test_the_single_shot_write_path_no_longer_exists(gateway):
    """codex F4. A second, unpreviewed door makes the gate decorative."""
    resp = _client(gateway).post(
        "/ai/knowledge/ingest", json={"title": "T", "text": "policy"},
        headers=_auth("admin-token"),
    )
    assert resp.status_code == 404


def test_staging_reaches_the_stage_endpoint_not_the_index(gateway):
    """Phase one must not be wired to anything that writes."""
    _client(gateway).post(
        "/ai/knowledge/stage", json={"title": "T", "text": "policy"},
        headers=_auth("admin-token"),
    )
    _service, path, _payload = gateway.__calls__[-1]
    assert path == "/ingest/stage"


def test_seed_is_gated_too(gateway):
    """Seeding writes to the index, so it is a write."""
    assert _client(gateway).post(
        "/ai/knowledge/seed", headers=_auth("staff-token")).status_code == 403
    assert _client(gateway).post(
        "/ai/knowledge/seed", headers=_auth("admin-token")).status_code == 200


def test_me_reports_the_capability(gateway):
    """The portal shows or hides the control without guessing the policy."""
    staff = _client(gateway).get("/me", headers=_auth("staff-token")).json()
    admin = _client(gateway).get("/me", headers=_auth("admin-token")).json()
    assert staff["can_ingest"] is False
    assert admin["can_ingest"] is True


# --------------------------------------------------------------------------- #
# end-to-end: the feature a clinician would use (RVB-W2-14)
# --------------------------------------------------------------------------- #
def test_e2e_chart_shaped_query_returns_cited_results():
    """Straight against the service, stub model, zero spend.

    Scope note: this proves the retrieval endpoint answers a chart-shaped
    clinical question. Wiring retrieval into the chart-open lifecycle is a UI
    integration outside this week's sizing and is roadmapped, not dropped.
    """
    import chromadb

    chroma = load_module("services/ai-orchestrator/chroma_index.py", "w2e_chroma")
    corpus_mod = load_module("services/ai-orchestrator/corpus.py", "w2e_corpus")
    rag = load_module("services/ai-orchestrator/rag_graph.py", "w2e_rag")
    port = sys.modules["index_port"]

    index = chroma.ChromaIndex(client=chromadb.EphemeralClient())
    index.add(corpus_mod.build_knowledge_chunks())
    index.add(corpus_mod.build_record_chunks())

    out = rag.run(index, "what allergies are on file for this patient?",
                  kind=port.KIND_RECORD, patient_scope=[1330])

    assert not out.refused
    assert out.grounded
    assert out.citations, "an answer without citations is not a retrieval answer"
    assert all(c["patient_id"] == 1330 for c in out.citations)
    assert "penicillin" in " ".join(r["text"] for r in out.retrieved).lower()
