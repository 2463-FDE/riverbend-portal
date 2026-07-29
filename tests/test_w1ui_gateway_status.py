"""W1-UI — the gateway preserves upstream HTTP status (RVB-U-09).

Before this, `_post`/`_get` returned `r.json()` and discarded `r.status_code`, so
a downstream 422 or 503 reached the browser as **HTTP 200 with an error body**.
Indistinguishable from success to any caller, which is why the UI could not
render an error state honestly.

Worth pinning the scope of what that did and did not affect, because the
distinction is load-bearing and easy to get wrong when reading the diff:
exceptions the gateway RAISES itself were always fine. Only downstream errors
flattened.
"""
import sys

import httpx
import pytest

from conftest import load_module

_sessions: dict[str, dict] = {}


@pytest.fixture(scope="module")
def gateway():
    security = load_module("services/gateway/security.py", "w1ui_gw_security")
    security.get_session = lambda token: _sessions.get(token)
    sys.modules["security"] = security

    app_mod = load_module("services/gateway/app.py", "w1ui_gw_app")
    _sessions["staff"] = {"username": "frontdesk", "role": "staff"}
    _sessions["maria"] = {"username": "maria.gonzalez", "role": "patient",
                          "patient_id": "1042"}
    return app_mod


def _resp(status: int, payload=None, text: str = ""):
    """A minimal httpx.Response standing in for a downstream service."""
    if payload is not None:
        return httpx.Response(status_code=status, json=payload)
    return httpx.Response(status_code=status, text=text)


# --------------------------------------------------------------------------- #
# _relay
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [200, 201, 400, 404, 409, 422, 500, 503])
def test_relay_preserves_every_status(gateway, status):
    out = gateway._relay(_resp(status, {"detail": "whatever"}))
    assert out.status_code == status, (
        f"a downstream {status} was rewritten to {out.status_code} — the UI "
        f"cannot tell failure from success"
    )


def test_relay_survives_a_non_json_body(gateway):
    """A downstream returning HTML or an empty body is itself a fault.

    Papering over it with an empty 200 is how a broken dependency looks like a
    working feature.
    """
    out = gateway._relay(_resp(500, text="<html>Internal Server Error</html>"))
    assert out.status_code == 500
    import json as _json
    assert "detail" in _json.loads(out.body)


def test_relay_handles_a_truly_empty_body(gateway):
    import json as _json

    out = gateway._relay(_resp(204, text=""))
    assert out.status_code == 204
    assert _json.loads(out.body)["detail"]


# --------------------------------------------------------------------------- #
# transport failure is 502, not 200
# --------------------------------------------------------------------------- #
def test_transport_failure_is_502_not_200(gateway, monkeypatch):
    def boom(*_a, **_kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(gateway.httpx, "get", boom)
    monkeypatch.setattr(gateway.httpx, "post", boom)

    assert gateway._get("records", "/patients").status_code == 502
    assert gateway._post("ai", "/summary", {}).status_code == 502


def test_transport_failure_does_not_leak_the_exception_text(gateway, monkeypatch):
    """The old code returned `str(e)`, which for a connection error can carry
    internal hostnames and ports."""
    import json as _json

    def boom(*_a, **_kw):
        raise httpx.ConnectError("failed to connect to records-service:8073")

    monkeypatch.setattr(gateway.httpx, "get", boom)
    body = _json.loads(gateway._get("records", "/patients").body)
    assert "8073" not in str(body)
    assert "records-service" not in str(body)


# --------------------------------------------------------------------------- #
# the regression this change nearly introduced
# --------------------------------------------------------------------------- #
def test_same_as_lookup_still_resolves_fragments(gateway, monkeypatch):
    """`_same_as_lookup` reads a downstream BODY, not a client response.

    When `_get` started returning a JSONResponse, this function's
    `payload.get("clusters")` began raising AttributeError inside its own
    try/except — silently narrowing every patient to their own chart and undoing
    the Week-2 fix, so Maria would no longer see the fragment carrying her
    penicillin allergy.

    It failed *quietly*, which is the only reason it needs a test.
    """
    monkeypatch.setattr(
        gateway.httpx, "get",
        lambda *_a, **_kw: _resp(200, {
            "clusters": [{"patient_ids": [1042, 1330, 1588], "fragmented": True}]
        }),
    )
    assert gateway._same_as_lookup(1042) == [1042, 1330, 1588]


def test_same_as_lookup_narrows_to_self_when_the_service_is_down(gateway, monkeypatch):
    """Identity resolution must never be able to WIDEN access."""
    def boom(*_a, **_kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(gateway.httpx, "get", boom)
    assert gateway._same_as_lookup(1042) == [1042]


def test_same_as_lookup_ignores_a_non_success_response(gateway, monkeypatch):
    monkeypatch.setattr(gateway.httpx, "get",
                        lambda *_a, **_kw: _resp(503, {"clusters": [{"patient_ids": [1, 2]}]}))
    assert gateway._same_as_lookup(1042) == [1042], (
        "a failed identity lookup must not be trusted for scope widening"
    )


# --------------------------------------------------------------------------- #
# gateway-raised errors were never affected — pin that
# --------------------------------------------------------------------------- #
def test_gateway_raised_errors_keep_their_status(gateway, monkeypatch):
    """The IDOR 404 is raised BEFORE the proxy call, so RVB-U-09 cannot have
    changed it. Asserted so a future refactor of `_relay` cannot quietly move
    the authorization denial into the relaying path."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(gateway, "_get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("records-service must not be reached")))
    monkeypatch.setattr(gateway, "_same_as_lookup", lambda pid: [1042, 1330, 1588])

    client = TestClient(gateway.app)
    r = client.get("/patients/1043/records",
                   headers={"Authorization": "Bearer maria"})
    assert r.status_code == 404


def test_ai_health_route_is_session_guarded(gateway, monkeypatch):
    """RVB-W1-U3: the panel reads the retention posture before submitting."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(gateway, "_get",
                        lambda *a, **k: {"status": "ok", "retention": {"ok": True}})
    client = TestClient(gateway.app)

    assert client.get("/ai/health").status_code == 401
    assert client.get("/ai/health",
                      headers={"Authorization": "Bearer staff"}).status_code == 200
