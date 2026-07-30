"""The HITL queue, and who is allowed to answer it.

Three findings converge here, and all three were reachable because the resume
route had **no gateway test at all** — the reason F9 survived four reviews.

codex F8 — the old route was guarded by `require_patient_access`, so Maria could
approve the sensitivity gate on Maria's record. The word "approve" was hiding two
different questions: *may this patient see their own record* (yes, 164.524) and
*should this assembled, cross-chart, sensitivity-flagged view be released* — a
clinical and compliance judgement about material that may include another
person's information. The gate asks the second.

codex F9 — resume took a client-supplied `thread_id` and forwarded it verbatim.
The gateway checked which patient and then trusted the client for which run.

`RVB-AG-13` — a queue over `InMemorySaver` empties on restart. That is a session,
not a control.
"""
import json
import sys
import time

import httpx
import pytest

from conftest import load_module
from test_api_knowledge_staging import FakeRedis

_sessions: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #
@pytest.fixture
def approvals(monkeypatch):
    mod = load_module("services/ai-orchestrator/approvals.py", "ap_approvals")
    fake = FakeRedis()
    monkeypatch.setattr(mod, "_redis", lambda: fake)
    mod._fake = fake
    return mod


def _register(approvals, patient_id=1042, thread="view-maria-1042",
              by="maria.gonzalez", principal="patient", ids=(1042, 1330, 1588)):
    return approvals.register(
        patient_id=patient_id, thread_id=thread, requested_by=by,
        requested_principal=principal, authorized_ids=list(ids),
        reason="disclosure_shaped_assembly",
    )


def test_a_paused_run_becomes_a_queued_decision(approvals):
    approval = _register(approvals)
    rows = approvals.list_open()
    assert [r.approval_id for r in rows] == [approval.approval_id]


def test_the_row_describes_the_decision_not_the_run(approvals):
    """adr/0015 rule 4. "Approve run view-a3f9" is a button that gets clicked."""
    row = _register(approvals).row()
    assert "1042" in row["describe"]
    assert "3 charts" in row["describe"]
    assert "maria.gonzalez" in row["describe"]


def test_the_thread_id_never_leaves_the_service(approvals):
    """UI-D20 / codex F9. It is a checkpointer detail, not part of the contract."""
    row = _register(approvals).row()
    assert "thread_id" not in row
    assert "view-maria-1042" not in str(row)


def test_the_approval_id_is_not_guessable_from_the_request(approvals):
    a = _register(approvals)
    assert a.approval_id != a.thread_id
    assert str(a.patient_id) not in a.approval_id
    assert len(a.approval_id) >= 32


def test_registering_the_same_run_twice_does_not_duplicate_it(approvals):
    """A queue that grows on refresh is a queue people stop reading."""
    first = _register(approvals)
    second = _register(approvals)
    assert first.approval_id == second.approval_id
    assert len(approvals.list_open()) == 1


def test_the_queue_is_ordered_oldest_first(approvals):
    a = _register(approvals, thread="t1", patient_id=1042)
    time.sleep(0.01)
    b = _register(approvals, thread="t2", patient_id=1601)
    assert [r.approval_id for r in approvals.list_open()] == [a.approval_id, b.approval_id]


def test_an_expired_record_is_reaped_from_the_index(approvals):
    """A row that 404s when clicked is worse than no row."""
    a = _register(approvals)
    approvals._fake.kv.clear()
    assert approvals.list_open() == []
    assert approvals._fake.scard(approvals.INDEX_KEY) == 0


# --------------------------------------------------------------------------- #
# who may decide — codex F8, UI-D18
# --------------------------------------------------------------------------- #
def test_a_patient_cannot_decide_at_all(approvals):
    a = _register(approvals)
    with pytest.raises(approvals.ApprovalForbidden):
        approvals.claim(a.approval_id, approver="maria.gonzalez",
                        approver_principal="patient", approver_patient_id=1042)


def test_the_subject_cannot_approve_their_own_release_even_as_staff(approvals):
    """The check that a role test alone would miss.

    A staff member who also holds a patient account for their own chart is
    exactly the case where "is this person staff?" answers yes and the decision
    is still a subject approving themselves.
    """
    a = _register(approvals, patient_id=1042)
    with pytest.raises(approvals.ApprovalForbidden) as e:
        approvals.claim(a.approval_id, approver="staff.who.is.also.a.patient",
                        approver_principal="staff", approver_patient_id=1042)
    assert "your own record" in str(e.value)


def test_staff_unrelated_to_the_subject_may_decide(approvals):
    a = _register(approvals, patient_id=1042)
    claimed = approvals.claim(a.approval_id, approver="frontdesk",
                              approver_principal="staff", approver_patient_id=None)
    assert claimed.thread_id == "view-maria-1042"


def test_a_refused_claim_does_not_destroy_the_pending_decision(approvals):
    """Otherwise anyone who could see the queue could empty it with 403s."""
    a = _register(approvals)
    with pytest.raises(approvals.ApprovalForbidden):
        approvals.claim(a.approval_id, approver="maria.gonzalez",
                        approver_principal="patient", approver_patient_id=1042)
    assert len(approvals.list_open()) == 1


def test_a_decision_is_single_use(approvals):
    a = _register(approvals)
    approvals.claim(a.approval_id, approver="frontdesk",
                    approver_principal="staff", approver_patient_id=None)
    with pytest.raises(approvals.ApprovalNotFound):
        approvals.claim(a.approval_id, approver="frontdesk",
                        approver_principal="staff", approver_patient_id=None)


def test_an_unknown_approval_is_not_found(approvals):
    with pytest.raises(approvals.ApprovalNotFound):
        approvals.claim("nope", approver="frontdesk",
                        approver_principal="staff", approver_patient_id=None)


def test_the_queued_record_is_encrypted_at_rest(approvals, monkeypatch):
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "unit-test-key-not-a-real-secret")
    a = _register(approvals, by="maria.gonzalez")
    stored = approvals._fake.kv[f"{approvals.KEY_PREFIX}{a.approval_id}"][0]
    assert "maria.gonzalez" not in stored
    assert "view-maria-1042" not in stored
    assert approvals.get(a.approval_id).requested_by == "maria.gonzalez"


def test_the_registry_fails_closed(monkeypatch):
    mod = load_module("services/ai-orchestrator/approvals.py", "ap_approvals_down")

    def boom():
        raise mod.ApprovalsUnavailable("redis down")

    monkeypatch.setattr(mod, "_redis", boom)
    with pytest.raises(mod.ApprovalsUnavailable):
        mod.list_open()


# --------------------------------------------------------------------------- #
# the gateway surface
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def gateway():
    security = load_module("services/gateway/security.py", "ap_gw_security")
    security.get_session = lambda token: _sessions.get(token)
    sys.modules["security"] = security

    cfg = load_module("services/gateway/config.py", "ap_gw_config")
    cfg.settings.approval_roles = frozenset({"staff"})
    cfg.settings.approval_users = frozenset()
    sys.modules["config"] = cfg

    app_mod = load_module("services/gateway/app.py", "ap_gw_app")
    _sessions["staff"] = {"username": "frontdesk", "role": "staff"}
    _sessions["maria"] = {"username": "maria.gonzalez", "role": "patient",
                          "patient_id": "1042"}
    return app_mod


@pytest.fixture
def client(gateway, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(gateway, "_same_as_lookup",
                        lambda pid: [1042, 1330, 1588] if pid == 1042 else [pid])
    return TestClient(gateway.app)


@pytest.fixture
def sent(gateway, monkeypatch):
    calls: list[tuple] = []

    def fake_post(service, path, payload):
        calls.append((path, payload))
        return httpx.Response(200, json={"ok": True})

    def fake_get(service, path, params=None):
        calls.append((path, params))
        return httpx.Response(200, json={"approvals": []})

    monkeypatch.setattr(gateway, "_post", fake_post)
    monkeypatch.setattr(gateway, "_get", fake_get)
    return calls


def test_api_the_queue_is_staff_only(client, sent):
    assert client.get("/ai/approvals",
                      headers={"Authorization": "Bearer maria"}).status_code == 403
    assert client.get("/ai/approvals",
                      headers={"Authorization": "Bearer staff"}).status_code == 200
    assert client.get("/ai/approvals").status_code == 401


def test_api_deciding_is_staff_only(client, sent):
    r = client.post("/ai/approvals/abc123", json={"approved": True},
                    headers={"Authorization": "Bearer maria"})
    assert r.status_code == 403
    assert sent == []


def test_api_the_approver_identity_comes_from_the_session(client, sent):
    """A client supplying its own `approver` would defeat the not-the-subject
    check, which is the entire point of the field."""
    r = client.post("/ai/approvals/abc123",
                    json={"approved": True, "approver": "someone.else",
                          "approver_patient_id": 9999},
                    headers={"Authorization": "Bearer staff"})
    assert r.status_code == 200
    _path, payload = sent[-1]
    assert payload["approver"] == "frontdesk"
    assert payload["approver_principal"] == "staff"
    assert payload["approver_patient_id"] is None


def test_api_the_old_resume_route_is_gone(client, sent):
    """codex F9. It trusted a client-supplied thread_id, and had no test."""
    r = client.post("/ai/patient-view/1042/resume",
                    json={"thread_id": "view-maria-1042", "approved": True},
                    headers={"Authorization": "Bearer maria"})
    assert r.status_code == 404, (
        "the thread_id resume route is back; a client can name which run to release"
    )


def test_api_me_exposes_the_approval_capability_separately(client, sent):
    body = client.get("/me", headers={"Authorization": "Bearer staff"}).json()
    assert "can_approve" in body
    assert "can_ingest" in body
    # Two capabilities, not one. Ingest changes what the assistant believes;
    # approval discloses one patient's assembled record.
    assert body["can_approve"] is True


def test_the_requester_cannot_release_their_own_request(approvals):
    """The subject check one step removed.

    Blocking a patient from rubber-stamping their own disclosure and then letting
    the staff member who asked for it clear their own queue item is the same
    failure with an extra hop. A queue you can empty yourself has no second pair
    of eyes in it.
    """
    a = _register(approvals, patient_id=1042, by="frontdesk", principal="staff")
    with pytest.raises(approvals.ApprovalForbidden) as e:
        approvals.claim(a.approval_id, approver="frontdesk",
                        approver_principal="staff", approver_patient_id=None)
    assert "requested by you" in str(e.value)

    # A different staff member can.
    claimed = approvals.claim(a.approval_id, approver="nurse.two",
                              approver_principal="staff", approver_patient_id=None)
    assert claimed.patient_id == 1042


def test_api_staff_pulling_a_merged_record_is_disclosure_shaped(client, sent):
    """The gate has to be REACHABLE (RVB-AG-12).

    Nothing passed `cross_patient`, so `default_sensitive` was always False and
    the queue could only ever be empty. A control nobody can reach is not a
    control -- and it would have demoed as "HITL is implemented" with an empty
    screen.
    """
    r = client.get("/ai/patient-view/1042", headers={"Authorization": "Bearer staff"})
    assert r.status_code == 200
    _path, payload = sent[-1]
    assert payload["cross_patient"] is True, (
        "staff assembling Maria's three charts did not trip the sensitivity gate"
    )


def test_api_a_patient_reading_their_own_record_is_not_gated(client, sent):
    """164.524 right of access, not a disclosure decision.

    Gating it would also deadlock: UI-D18 says a subject may never approve their
    own release, so a patient-triggered gate would have no legitimate approver
    reachable from their own session.
    """
    r = client.get("/ai/patient-view/1042", headers={"Authorization": "Bearer maria"})
    assert r.status_code == 200
    _path, payload = sent[-1]
    assert payload["cross_patient"] is False


def test_api_staff_pulling_a_single_chart_is_not_gated(client, sent):
    """Narrow on purpose. A human gate on a high-volume path is a workaround
    generator, not a control -- so only MERGED records pause.

    1043 is a single-chart patient in the `client` fixture's cluster stub.
    """
    r = client.get("/ai/patient-view/1043", headers={"Authorization": "Bearer staff"})
    assert r.status_code == 200
    _path, payload = sent[-1]
    assert payload["cross_patient"] is False


# --------------------------------------------------------------------------- #
# A withheld view must not return the thing it withheld
# --------------------------------------------------------------------------- #
def test_a_withheld_view_does_not_return_the_assembled_phi():
    """Verified live before this guard existed: a DENIED release still returned
    Maria's penicillin allergy.

    The graph's `withhold` node cannot fix this itself. `domains` carries a merge
    reducer -- `{**left, **right}` -- so a node returning `{"domains": {}}` is a
    no-op and the assembled content survives. The guard therefore lives at the
    serialization boundary, which is the last reducer-independent point.

    A gate that refuses a disclosure and then performs it is worse than no gate:
    the audit log records a refusal.
    """
    app_mod = load_module("services/ai-orchestrator/app.py", "vw_app")

    class View:
        patient_id = 1042
        authorized = True
        released = False
        summary = "Penicillin allergy confirmed."
        grounded = True
        deny_reason = "withheld_pending_approval"
        sensitive = True
        approved = False
        paused = False
        path = ["authorize", "sensitivity_gate", "withhold"]
        domains = {
            "labs": {"domain": "labs", "status": "ok",
                     "data": ["Allergies on file: penicillin"], "note": ""},
        }

    payload = app_mod._view_payload(View())
    blob = json.dumps(payload).lower()

    assert "penicillin" not in blob, "a withheld view disclosed the record it withheld"
    assert payload["summary"] == ""
    assert payload["grounded"] is False
    # The SHAPE survives -- a caller still learns which sections exist and which
    # failed -- but the content does not.
    assert payload["domains"]["labs"]["status"] == "ok"
    assert payload["domains"]["labs"]["data"] == []
    assert "not released" in payload["domains"]["labs"]["note"].lower()


def test_a_released_view_returns_everything():
    """The guard must not withhold from an approved release."""
    app_mod = load_module("services/ai-orchestrator/app.py", "vw_app_ok")

    class View:
        patient_id = 1042
        authorized = True
        released = True
        summary = "Penicillin allergy confirmed."
        grounded = True
        deny_reason = ""
        sensitive = True
        approved = True
        paused = False
        path = ["authorize", "sensitivity_gate", "synthesize"]
        domains = {"labs": {"domain": "labs", "status": "ok",
                            "data": ["Allergies on file: penicillin"], "note": ""}}

    payload = app_mod._view_payload(View())
    assert "penicillin" in json.dumps(payload).lower()
    assert payload["summary"]
