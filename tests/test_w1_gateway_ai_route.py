"""W1 — the AI feature adds no new unauthenticated surface (RVB-W1-05).

The gateway is the only thing the portal talks to, so `/ai/summary` must be
session-guarded exactly like every other route. Redis and the downstream service
are both stubbed: this test is about the guard, not about the summary.
"""
import sys
import types

import pytest
from fastapi.testclient import TestClient

from conftest import load_module

# The gateway imports redis and sqlalchemy at module scope. Stub the session
# store so this stays a unit test — we are asserting the dependency wiring, not
# Redis behaviour.
_fake_sessions: dict[str, dict] = {}


@pytest.fixture(scope="module")
def gateway_app():
    security = load_module("services/gateway/security.py", "w1_gw_security")

    def fake_get_session(token):
        return _fake_sessions.get(token)

    security.get_session = fake_get_session
    sys.modules["security"] = security

    # `db.get_db` opens a connection at import-time-of-use only, so the module
    # imports fine; we never hit a route that needs it.
    app_mod = load_module("services/gateway/app.py", "w1_gw_app")
    app_mod.get_session = fake_get_session
    app_mod.app.dependency_overrides = {}

    # Point the AI proxy at a sink we control, so a 200 proves the guard let us
    # through rather than proving Bedrock works.
    calls: list[tuple] = []
    app_mod._post = lambda service, path, payload: (
        calls.append((service, path, payload)) or {"ok": True, "service": service}
    )
    app_mod.__test_calls__ = calls
    return app_mod


def test_ai_summary_requires_a_session(gateway_app):
    client = TestClient(gateway_app.app)
    r = client.post("/ai/summary", json={"instructions": "x" * 40})
    assert r.status_code == 401, "the AI feature must not be an anonymous surface"


def test_ai_summary_rejects_a_bogus_token(gateway_app):
    client = TestClient(gateway_app.app)
    r = client.post(
        "/ai/summary",
        json={"instructions": "x" * 40},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401


def test_ai_summary_proxies_when_authenticated(gateway_app):
    _fake_sessions["good-token"] = {"username": "frontdesk1", "role": "staff"}
    client = TestClient(gateway_app.app)
    r = client.post(
        "/ai/summary",
        json={"instructions": "x" * 40},
        headers={"Authorization": "Bearer good-token"},
    )
    assert r.status_code == 200
    service, path, _payload = gateway_app.__test_calls__[-1]
    assert (service, path) == ("ai", "/summary")


def test_ai_route_is_registered_on_the_ai_service(gateway_app):
    """A typo in SERVICES would route the AI call at some other service."""
    assert "ai" in gateway_app.SERVICES
    assert gateway_app.SERVICES["ai"].endswith(":8077")
