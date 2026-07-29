"""W4 — the IDOR regression at the gateway, and the knowledge graph.

`docs/handover/portal.har` captures the walk: a logged-in patient fetches
`/api/patients/1042/records` → 200, then `/api/patients/1043/records` → 200.
Two requests, two different humans' charts, one session.

The test below replays exactly that sequence and asserts it is now denied. The
pre-fix reproduction lives in `docs/findings/w4-idor-record-access.md` as
evidence — not as a permanently-failing test, which would either break CI or, if
it passed, enshrine the vulnerability.
"""
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

from conftest import load_module

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_sessions: dict[str, dict] = {}


@pytest.fixture(scope="module")
def gateway():
    security = load_module("services/gateway/security.py", "w4_gw_security")
    security.get_session = lambda token: _sessions.get(token)
    sys.modules["security"] = security

    app_mod = load_module("services/gateway/app.py", "w4_gw_app")

    proxied: list[tuple] = []
    app_mod._get = lambda service, path, params=None: (
        proxied.append((service, path, params)) or {"ok": True, "path": path}
    )
    app_mod._post = lambda service, path, payload: (
        proxied.append((service, path, payload)) or {"ok": True}
    )
    # Identity resolution normally goes through the AI service; stub it so this
    # test exercises the gate, not the network.
    app_mod._same_as_lookup = lambda pid: (
        [1042, 1330, 1588] if pid in (1042, 1330, 1588) else [pid]
    )
    app_mod.__proxied__ = proxied

    # The HAR's principal: a patient-portal login bound to chart 1042.
    _sessions["maria"] = {"username": "maria.gonzalez", "role": "patient",
                          "patient_id": "1042"}
    _sessions["frontdesk"] = {"username": "frontdesk", "role": "staff"}
    return app_mod


def _client(gateway):
    return TestClient(gateway.app)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# the HAR, replayed
# --------------------------------------------------------------------------- #
def test_the_har_captured_a_cross_patient_walk():
    """Sanity-check the evidence before asserting against it."""
    har = json.load(open(os.path.join(REPO_ROOT, "docs/handover/portal.har")))
    urls = [(e["request"]["url"], e["response"]["status"])
            for e in har["log"]["entries"]]
    assert any("1042/records" in u and s == 200 for u, s in urls)
    assert any("1043/records" in u and s == 200 for u, s in urls), (
        "the HAR should show the second, unauthorized chart returning 200"
    )


def test_har_walk_is_now_denied(gateway):
    """THE regression test. Same sequence, same session, different outcome."""
    client = _client(gateway)

    own = client.get("/patients/1042/records", headers=_auth("maria"))
    assert own.status_code == 200, "the patient can no longer see her own chart"

    walked = client.get("/patients/1043/records", headers=_auth("maria"))
    assert walked.status_code == 404, (
        "the HAR walk still succeeds — D11 is not fixed on the route the client "
        "is actually exposed on"
    )


def test_the_walk_never_reaches_records_service(gateway):
    """Denied means denied before proxying, not filtered afterwards."""
    gateway.__proxied__.clear()
    _client(gateway).get("/patients/1043/records", headers=_auth("maria"))
    assert gateway.__proxied__ == [], (
        "records-service was called for an unauthorized id"
    )


def test_patient_demographics_route_is_gated_too(gateway):
    client = _client(gateway)
    assert client.get("/patients/1042", headers=_auth("maria")).status_code == 200
    assert client.get("/patients/1043", headers=_auth("maria")).status_code == 404


def test_the_patient_sees_all_three_of_her_own_fragments(gateway):
    """W2's finding must not become W4's access denial.

    Maria is three charts and the penicillin allergy is on 1330. Authorizing only
    1042 would hide her own allergy from her.
    """
    client = _client(gateway)
    for chart in (1042, 1330, 1588):
        resp = client.get(f"/patients/{chart}/records", headers=_auth("maria"))
        assert resp.status_code == 200, f"chart {chart} denied to its own patient"


def test_denial_is_not_an_enumeration_oracle(gateway):
    """A 403 on a real id and a 404 on a nonexistent one confirms which ids
    exist, which is most of what the walk was after."""
    client = _client(gateway)
    real_but_unauthorized = client.get("/patients/1043/records", headers=_auth("maria"))
    nonexistent = client.get("/patients/999999/records", headers=_auth("maria"))

    assert real_but_unauthorized.status_code == nonexistent.status_code == 404
    assert real_but_unauthorized.json() == nonexistent.json(), (
        "responses differ, so an attacker can still distinguish real ids"
    )


def test_appointments_and_roi_are_gated_on_the_same_scope(gateway):
    client = _client(gateway)
    assert client.get("/appointments?patient_id=1043",
                      headers=_auth("maria")).status_code == 404
    assert client.get("/roi/requests?patient_id=1043",
                      headers=_auth("maria")).status_code == 404


def test_record_search_is_closed_to_patient_principals(gateway):
    """A different route to the same exposure."""
    client = _client(gateway)
    assert client.get("/records/search?q=penicillin",
                      headers=_auth("maria")).status_code == 404
    assert client.get("/records/search?q=penicillin",
                      headers=_auth("frontdesk")).status_code == 200


def test_staff_retain_access(gateway):
    """The gate narrows which PATIENT, not which staff role. D7 is still open and
    must not be oversold as fixed."""
    client = _client(gateway)
    assert client.get("/patients/1043/records",
                      headers=_auth("frontdesk")).status_code == 200


def test_me_exposes_the_resolved_scope(gateway):
    body = _client(gateway).get("/me", headers=_auth("maria")).json()
    assert body["patient_id"] == "1042"
    assert body["scope"]["principal"] == "patient"
    assert sorted(body["scope"]["patient_ids"]) == [1042, 1330, 1588]


def test_a_client_supplied_patient_id_is_ignored(gateway):
    """A request must not be able to assert its own patient identity — that
    would reintroduce the IDOR through the front door."""
    client = _client(gateway)
    resp = client.get(
        "/patients/1043/records",
        headers={**_auth("maria"), "X-Patient-Id": "1043"},
        params={"patient_id": 1043, "scope": "1043"},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# the knowledge graph
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def loaders():
    return load_module("services/ai-orchestrator/patient_view_loaders.py", "w4_kgl")


def test_graph_has_the_four_node_kinds(loaders):
    graph = loaders.build_graph_for([1042, 1330, 1588])
    stats = graph.stats()
    for kind in ("patient", "encounter", "provider", "record"):
        assert stats["by_kind"].get(kind, 0) > 0, f"no {kind} nodes"
    for rel in ("HAD", "WITH", "PRODUCED", "SAME_AS"):
        assert stats["by_rel"].get(rel, 0) > 0, f"no {rel} edges"


def test_same_as_links_the_three_fragments(loaders):
    graph = loaders.build_graph_for([1042, 1330, 1588])
    linked = {n.props["patient_id"]
              for n in graph.neighbours("patient:1042", rel="SAME_AS")}
    assert linked == {1330, 1588}


def test_records_are_reachable_across_same_as(loaders):
    """The Week-2 fix, made operational: traversing from any fragment reaches the
    whole record, including the chart that carries the allergy."""
    graph = loaders.build_graph_for([1042, 1330, 1588])
    records = graph.records_for([1042])
    allergies = " ".join(r.props.get("allergies", "") for r in records)
    assert "penicillin" in allergies, (
        "starting from chart 1042, the penicillin allergy on 1330 was not reachable"
    )


def test_graph_only_ever_contains_authorized_nodes(loaders):
    """Rows outside the scope are never LOADED, not filtered out afterwards.

    A graph containing unauthorized nodes has already put them in memory and in
    logs, however carefully it is queried later.
    """
    graph = loaders.build_graph_for([1042])
    patient_ids = {
        n.props.get("patient_id") for n in graph.nodes.values()
        if "patient_id" in n.props
    }
    assert patient_ids == {1042}
    assert graph.stats()["authorized_patients"] == [1042]


def test_same_as_cannot_widen_the_scope(loaders):
    """A SAME_AS edge reflects an identity the caller was ALREADY granted.

    If it could pull in an unauthorized fragment, identity resolution would
    become a privilege-escalation path.
    """
    graph = loaders.build_graph_for([1042])
    assert graph.neighbours("patient:1042", rel="SAME_AS") == []
    ids = {n.props.get("patient_id") for n in graph.nodes.values()
           if "patient_id" in n.props}
    assert 1330 not in ids and 1588 not in ids


def test_reachability_is_depth_bounded(loaders):
    graph = loaders.build_graph_for([1042, 1330, 1588])
    shallow = graph.reachable_from(["patient:1042"], max_depth=1)
    deep = graph.reachable_from(["patient:1042"], max_depth=3)
    assert len(shallow) < len(deep)


def test_allergy_absence_is_stated_not_implied(loaders):
    """An empty allergy field means 'nothing was recorded at that encounter'.

    It does not mean the patient has no allergies, and saying nothing is how a
    clinician reads an absence as an all-clear.
    """
    result = loaders.load_labs([1588])
    assert "not the same as having no allergies" in result.note.lower()


# --------------------------------------------------------------------------- #
# end-to-end: the feature the client would click (RVB-W4-16)
# --------------------------------------------------------------------------- #
def test_e2e_patient_sees_their_own_assembled_view():
    """A patient logs in and gets their own record, assembled across domains.

    Straight against the assembler with a stub model, zero spend. This is the
    Week-4 deliverable as the client would experience it — and note that it
    contains the penicillin allergy from chart 1330, which the Week-2 finding
    showed she could not see.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    pvg_mod = load_module("services/ai-orchestrator/patient_view_graph.py", "w4e_pvg")
    loaders_mod = load_module("services/ai-orchestrator/patient_view_loaders.py", "w4e_l")
    scope = load_module("services/gateway/scope.py", "w4e_scope")

    graph = pvg_mod.build_graph(
        loaders=loaders_mod.default_loaders(), checkpointer=InMemorySaver()
    )
    maria = scope.AuthorizedScope(
        principal="patient", username="maria.gonzalez",
        patient_ids=frozenset([1042, 1330, 1588]),
    )

    view = pvg_mod.run(graph, patient_id=1042, scope=maria, thread_id="e2e-1")

    assert view.authorized and view.released
    assert view.summary
    assert "penicillin" in view.summary.lower(), (
        "the assembled view does not contain the allergy recorded on chart 1330 "
        "— the Week-2 fragmentation is still hiding it from the patient"
    )
    assert "1042" in view.summary and "1330" in view.summary, (
        "the view should show the patient that her record spans several charts"
    )

    denied = pvg_mod.run(graph, patient_id=1043, scope=maria, thread_id="e2e-2")
    assert denied.authorized is False
    assert denied.domains == {}
