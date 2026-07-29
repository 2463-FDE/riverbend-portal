"""W3 — registration no longer waits for the payer.

This is the week's deliverable, and it is the test the client would care about if
she read one. Everything else is machinery in service of it.

What we are asserting is the PROPERTY, not a proxy for it: `POST /intake`
completes during a total payer outage, and the blocking call site is not invoked
on that path. An earlier draft of the spec asserted only "the call is awaited and
a concurrent request is served", which a still-coupled intake path could pass.
"""
import sys
import time

import pytest

from conftest import load_module


@pytest.fixture(scope="module")
def intake():
    """Load intake-service with the database stubbed out.

    We are testing the request path's coupling to the payer, not SQLAlchemy.
    """
    app_mod = load_module("services/intake-service/app.py", "w3_intake_app")

    created = {"n": 0}

    def fake_create_patient(_db, _demo):
        created["n"] += 1
        return 9000 + created["n"]

    app_mod._create_patient = fake_create_patient
    app_mod._create_coverage = lambda _db, _pid, _ins: None
    app_mod._record_consents = lambda _db, _pid, _kinds: None
    app_mod._update_coverage_status = lambda _pid, _payload: None

    from fastapi.testclient import TestClient

    app_mod.app.dependency_overrides[app_mod.get_db] = lambda: None
    app_mod.__client__ = TestClient(app_mod.app)
    return app_mod


PAYLOAD = {
    "demographics": {
        "name": "Test Patient",
        "dob": "1980-01-01",
        "ssn": "111-22-3333",
        "created_via": "front_desk",
    },
    "insurance": {"payer_name": "ACME", "member_id": "BCBS4471"},
    "consents": ["npp_ack", "treatment_consent"],
}


def test_intake_no_longer_calls_the_blocking_verifier(intake):
    """The old `_verify_eligibility` is gone from the request path entirely."""
    assert not hasattr(intake, "_verify_eligibility"), (
        "the inline, unbounded verifier is still present on the intake path"
    )
    assert hasattr(intake, "_schedule_eligibility")


def test_intake_source_has_no_blocking_sleep():
    """RIV-088's 4.2s sleep stood in for the clearinghouse round trip.

    Every registration paid it. If it comes back, the ticket comes back.
    """
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "services", "intake-service", "app.py",
    )
    source = open(path, encoding="utf-8").read()
    assert "time.sleep(4.2)" not in source
    assert "time.sleep" not in source, (
        "a blocking sleep on the registration path is what RIV-088 was"
    )


def test_registration_succeeds_during_a_total_payer_outage(intake, monkeypatch):
    """THE Tuesday regression test, at the HTTP boundary.

    Tue 09:02-09:21 the clearinghouse degraded for 19 minutes and the front desk
    could not register anyone. With eligibility off the critical path, a payer
    that never responds costs registration nothing.
    """
    hits = {"n": 0}

    def never_responds(*_a, **_kw):
        hits["n"] += 1
        time.sleep(30)          # the payer, hung
        raise AssertionError("should not be reached within the test")

    monkeypatch.setattr(intake.httpx, "get", never_responds)

    started = time.monotonic()
    resp = intake.__client__.post("/intake", json=PAYLOAD)
    elapsed = time.monotonic() - started

    assert resp.status_code == 201, "registration failed while the payer was down"
    assert elapsed < 2.0, (
        f"registration took {elapsed:.2f}s while the payer hung — it is still "
        f"coupled to the payer"
    )

    body = resp.json()
    assert body["patient_id"]
    assert body["eligibility"]["status"] == "pending"
    assert body["eligibility"]["active"] is None, (
        "'we have not checked yet' must be distinguishable from 'not covered' — "
        "conflating them is how a covered patient gets turned away"
    )


def test_registration_latency_is_unaffected_by_payer_latency(intake, monkeypatch):
    """Before: every registration inherited the payer's latency (RIV-088)."""
    def slow(*_a, **_kw):
        time.sleep(8)
        raise AssertionError("unreachable")

    monkeypatch.setattr(intake.httpx, "get", slow)

    started = time.monotonic()
    resp = intake.__client__.post("/intake", json=PAYLOAD)
    elapsed = time.monotonic() - started

    assert resp.status_code == 201
    assert elapsed < 2.0, f"an 8s payer produced a {elapsed:.2f}s registration"


def test_eligibility_still_resolves_in_the_background(intake, monkeypatch):
    """Decoupled is not the same as dropped. The status must still arrive."""
    resolved = {}

    class FakeResponse:
        @staticmethod
        def json():
            return {"status": "active", "stale": False}

    monkeypatch.setattr(intake.httpx, "get", lambda *_a, **_kw: FakeResponse())
    monkeypatch.setattr(intake, "_update_coverage_status",
                        lambda pid, payload: resolved.update({pid: payload}))

    resp = intake.__client__.post("/intake", json=PAYLOAD)
    assert resp.status_code == 201

    deadline = time.monotonic() + 3.0
    while not resolved and time.monotonic() < deadline:
        time.sleep(0.02)

    assert resolved, "eligibility never resolved in the background"
    assert next(iter(resolved.values()))["status"] == "active"


def test_registration_succeeds_with_no_insurance_at_all(intake):
    payload = {**PAYLOAD, "insurance": None}
    resp = intake.__client__.post("/intake", json=payload)
    assert resp.status_code == 201
    assert resp.json()["eligibility"] is None
