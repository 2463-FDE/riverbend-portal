"""The knowledge query endpoint cannot be steered into the record collection.

codex finding F1, confirmed live against a running stack before the fix:

    maria.gonzalez  ->  scope [1042, 1330, 1588]

    POST /ai/knowledge/query {"query": "...", "patient_scope": [1043]}
    200  "Patient James O'Brien (chart 1043). [1]"
         citations: [{doc_id: "chart-1043-3", patient_id: 1043}]
         grounded: true

The orchestrator chooses its collection from the presence of the field:

    kind = KIND_RECORD if req.patient_scope is not None else KIND_KNOWLEDGE

and the gateway forwarded the client's payload verbatim. So any authenticated
session -- including a patient, since #16 gave patients sessions -- could read any
chart in the corpus, grounded, with a citation naming the patient.

`QueryRequest.patient_scope` carried this comment the whole time:

    # Present only for record-collection queries. The gateway supplies it from
    # the session; a client cannot widen its own scope.

The gateway supplied nothing and stripped nothing. The comment described the
design; nothing enforced it. That gap is the entire finding, and it is why these
tests assert against the gateway rather than against `scope.py` -- `scope.py` was
correct throughout.
"""
import sys

import httpx
import pytest

from conftest import load_module

_sessions: dict[str, dict] = {}


@pytest.fixture(scope="module")
def gateway():
    security = load_module("services/gateway/security.py", "idor_gw_security")
    security.get_session = lambda token: _sessions.get(token)
    sys.modules["security"] = security

    app_mod = load_module("services/gateway/app.py", "idor_gw_app")
    _sessions["maria"] = {"username": "maria.gonzalez", "role": "patient",
                          "patient_id": "1042"}
    _sessions["james"] = {"username": "james.obrien", "role": "patient",
                          "patient_id": "1043"}
    _sessions["staff"] = {"username": "frontdesk", "role": "staff"}
    return app_mod


@pytest.fixture
def client(gateway, monkeypatch):
    from fastapi.testclient import TestClient

    # Maria's three charts, the Week-2 fragmentation.
    monkeypatch.setattr(gateway, "_same_as_lookup",
                        lambda pid: [1042, 1330, 1588] if pid == 1042 else [pid])
    return TestClient(gateway.app)


@pytest.fixture
def captured(gateway, monkeypatch):
    """Record what the gateway actually sends downstream."""
    sent: list[tuple[str, dict]] = []

    def fake_post(service, path, payload):
        sent.append((path, payload))
        return httpx.Response(status_code=200, json={"answer": "ok", "citations": []})

    monkeypatch.setattr(gateway, "_post", fake_post)
    return sent


# --------------------------------------------------------------------------- #
# the finding itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scope", [[1043], [1042, 1043], [], [999999]])
def test_knowledge_query_rejects_any_client_supplied_scope(client, captured, scope):
    """Including `[]` and the caller's OWN ids.

    An empty list is not harmless: `patient_scope=[]` is still "not None", so it
    flips the orchestrator into the record collection. And accepting a caller's
    own ids would mean the endpoint's behaviour depends on a client field, which
    is the property that made this exploitable in the first place.
    """
    r = client.post("/ai/knowledge/query",
                    json={"query": "medications?", "patient_scope": scope},
                    headers={"Authorization": "Bearer maria"})

    assert r.status_code == 400, (
        f"patient_scope={scope} was accepted; the knowledge endpoint can still be "
        f"steered into the PHI record collection"
    )
    assert captured == [], "the request reached the orchestrator despite being rejected"


def test_the_exact_live_exploit_is_dead(client, captured):
    """Verbatim reproduction of the request that returned chart 1043."""
    r = client.post(
        "/ai/knowledge/query",
        json={"query": "what medications is James OBrien on?", "patient_scope": [1043]},
        headers={"Authorization": "Bearer maria"},
    )
    assert r.status_code == 400
    assert captured == []


def test_a_normal_knowledge_query_still_works(client, captured):
    """The fix must not break the feature. No scope field -> knowledge collection."""
    r = client.post("/ai/knowledge/query",
                    json={"query": "what is the fasting policy?"},
                    headers={"Authorization": "Bearer maria"})

    assert r.status_code == 200
    assert len(captured) == 1
    path, payload = captured[0]
    assert path == "/query"
    assert "patient_scope" not in payload, (
        "a knowledge query must reach the orchestrator WITHOUT a scope field, or "
        "it selects the record collection"
    )


# --------------------------------------------------------------------------- #
# the replacement route: the server owns the scope
# --------------------------------------------------------------------------- #
def test_records_query_uses_the_session_scope_not_the_payload(client, captured):
    """A client-supplied scope is overwritten, not merged."""
    r = client.post("/ai/records/query",
                    json={"query": "allergies?", "patient_scope": [1043]},
                    headers={"Authorization": "Bearer maria"})

    assert r.status_code == 200
    _, payload = captured[0]
    assert payload["patient_scope"] == [1042, 1330, 1588], (
        "the client's [1043] survived into the downstream call"
    )


def test_records_query_spans_maria_fragments(client, captured):
    """The Week-2 fix must survive the Week-4 authorization boundary.

    Narrowing her to [1042] here would hide the penicillin allergy on 1330 --
    the exact failure the corpus path already produced once.
    """
    client.post("/ai/records/query", json={"query": "allergies?"},
                headers={"Authorization": "Bearer maria"})
    assert captured[0][1]["patient_scope"] == [1042, 1330, 1588]


def test_records_query_does_not_leak_across_patients(client, captured):
    """James asking with Maria's ids gets his own scope."""
    client.post("/ai/records/query",
                json={"query": "allergies?", "patient_scope": [1042, 1330, 1588]},
                headers={"Authorization": "Bearer james"})
    assert captured[0][1]["patient_scope"] == [1043]


def test_staff_must_name_a_patient(client, captured):
    """Staff are open_to_context, so their id set is empty.

    Without an explicit patient the query would be unscoped -- either silently
    empty or, worse, unfiltered. Neither is acceptable, so it is a 400.
    """
    r = client.post("/ai/records/query", json={"query": "allergies?"},
                    headers={"Authorization": "Bearer staff"})
    assert r.status_code == 400
    assert captured == []


def test_staff_named_patient_is_scoped_to_that_patient(client, captured):
    r = client.post("/ai/records/query",
                    json={"query": "allergies?", "patient_id": 1043},
                    headers={"Authorization": "Bearer staff"})
    assert r.status_code == 200
    payload = captured[0][1]
    assert payload["patient_scope"] == [1043]
    assert "patient_id" not in payload, "the routing field leaked into the query body"


def test_records_query_is_session_guarded(client, captured):
    assert client.post("/ai/records/query", json={"query": "x"}).status_code == 401
    assert captured == []


# --------------------------------------------------------------------------- #
# codex F2 — the diagnostic routes are cross-patient by construction
# --------------------------------------------------------------------------- #
STAFF_ONLY = [
    ("get", "/ai/knowledge/corpus"),
    ("get", "/ai/knowledge/eval/latest"),
    ("get", "/ai/knowledge/identity-clusters"),
    ("post", "/ai/knowledge/eval"),
]


@pytest.fixture
def captured_get(gateway, monkeypatch):
    seen: list[str] = []

    def fake_get(service, path, params=None):
        seen.append(path)
        return httpx.Response(status_code=200, json={})

    monkeypatch.setattr(gateway, "_get", fake_get)
    return seen


@pytest.mark.parametrize("method,path", STAFF_ONLY)
def test_diagnostic_routes_refuse_a_patient(client, captured, captured_get, method, path):
    """Verified live before this gate existed, as maria.gonzalez:

        identity-clusters -> every patient's name, chart ids, and the match
                             reasons behind them, including `identical_ssn`
        corpus            -> "James O'Brien - office_visit 2026-02-20"
        eval              -> the same, inside identity_split_examples

    None of these answer a question the patient asked about themselves. They
    describe the corpus, and the corpus is everyone.
    """
    call = getattr(client, method)
    kwargs = {"json": {}} if method == "post" else {}
    r = call(path, headers={"Authorization": "Bearer maria"}, **kwargs)

    assert r.status_code == 403, f"{path} leaked cross-patient data to a patient"
    assert captured == [] and captured_get == [], (
        f"{path} reached the orchestrator despite the 403"
    )


@pytest.mark.parametrize("method,path", STAFF_ONLY)
def test_diagnostic_routes_still_serve_staff(client, captured, captured_get, method, path):
    """The gate must not break the quality dashboard it exists to protect."""
    call = getattr(client, method)
    kwargs = {"json": {}} if method == "post" else {}
    r = call(path, headers={"Authorization": "Bearer staff"}, **kwargs)
    assert r.status_code == 200


@pytest.mark.parametrize("method,path", STAFF_ONLY)
def test_diagnostic_routes_still_reject_anonymous(client, method, path):
    call = getattr(client, method)
    kwargs = {"json": {}} if method == "post" else {}
    assert call(path, **kwargs).status_code == 401
