"""
gateway — backend-for-frontend / API gateway.

The Next.js portal talks only to this service; it fans out to the internal
FastAPI services and owns login/sessions.

Inherited shortcomings (left as-is from the handoff):
  * Records fan-out forwards the caller's session but never binds it to the
    {patient_id} being requested — any logged-in user can read any chart (IDOR).
  * Sessions never expire (see security.create_session / auth.yaml).
  * One role for everyone; no per-action authorization beyond "is logged in".
"""
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

import authz
from config import settings
from db import get_db
from logging_config import configure
from models import User
from security import create_session, destroy_session, get_session, verify_password

log = configure(settings.service_name)
app = FastAPI(title="Riverbend gateway", version="1.4.0")

SERVICES = {
    "intake": settings.intake_url,
    "eligibility": settings.eligibility_url,
    "records": settings.records_url,
    "scheduling": settings.scheduling_url,
    "interop": settings.interop_url,
    "roi": settings.roi_url,
    "ai": settings.ai_orchestrator_url,
}


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    username: str
    password: str


def _bearer(authorization: Optional[str]) -> str:
    if not authorization:
        return ""
    return authorization[7:] if authorization.lower().startswith("bearer ") else authorization


def require_session(authorization: Optional[str] = Header(default=None)) -> dict:
    """Reject anonymous callers. (Does NOT scope access to a patient — see IDOR.)"""
    sess = get_session(_bearer(authorization))
    if not sess:
        raise HTTPException(status_code=401, detail="not authenticated")
    return sess


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": settings.service_name}


@app.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    """
    Issue a session token. Password only (no MFA), and the token never expires
    (no TTL on the Redis key) — see auth.yaml.
    """
    try:
        user = db.execute(select(User).where(User.username == req.username)).scalar_one_or_none()
    except Exception as e:  # DB down in local dev without compose
        log.error("login db error: %s", e)
        raise HTTPException(status_code=503, detail="auth backend unavailable")

    if not user or not user.is_active or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid username or password")

    user.last_login_at = func.now()
    db.commit()
    token = create_session(user.username, user.role)
    log.info("login ok user=%s", user.username)
    return {
        "token": token,
        "mfa": False,
        "user": {"username": user.username, "full_name": user.full_name, "role": user.role},
    }


@app.post("/logout")
def logout(authorization: Optional[str] = Header(default=None)):
    destroy_session(_bearer(authorization))
    return {"status": "ok"}


@app.get("/me")
def me(session: dict = Depends(require_session)):
    return {
        "username": session.get("username"),
        "role": session.get("role"),
        # Surfaced so the portal can show or hide the "Add document" control
        # without guessing the policy client-side. The gateway stays the single
        # authority — this is a hint for the UI, not the check.
        "can_ingest": authz.can_ingest(session),
    }


# --------------------------------------------------------------------------- #
# intake / eligibility
# --------------------------------------------------------------------------- #
@app.post("/intake")
def proxy_intake(payload: dict, session: dict = Depends(require_session)):
    return _post("intake", "/intake", payload)


@app.get("/eligibility")
def proxy_eligibility(insurance_id: str, session: dict = Depends(require_session)):
    return _get("eligibility", "/eligibility", params={"insurance_id": insurance_id})


# --------------------------------------------------------------------------- #
# patients / records
# --------------------------------------------------------------------------- #
@app.get("/patients")
def proxy_patients(
    session: dict = Depends(require_session),
    q: Optional[str] = None,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return _get("records", "/patients", params={"q": q, "limit": limit, "offset": offset})


@app.get("/patients/{patient_id}")
def proxy_patient(patient_id: int, session: dict = Depends(require_session)):
    return _get("records", f"/patients/{patient_id}")


@app.get("/patients/{patient_id}/records")
def proxy_records(patient_id: int, session: dict = Depends(require_session)):
    # IDOR: a valid session is required, but it is never checked against
    # {patient_id}. {patient_id} is the sequential primary key.
    return _get("records", f"/patients/{patient_id}/records")


@app.get("/records/search")
def proxy_search(q: str, session: dict = Depends(require_session)):
    return _get("records", "/records/search", params={"q": q})


# --------------------------------------------------------------------------- #
# scheduling
# --------------------------------------------------------------------------- #
@app.get("/slots")
def proxy_slots(
    session: dict = Depends(require_session),
    provider_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=200),
):
    return _get("scheduling", "/slots", params={"provider_id": provider_id, "limit": limit})


@app.get("/appointments")
def proxy_list_appointments(patient_id: int, session: dict = Depends(require_session)):
    return _get("scheduling", "/appointments", params={"patient_id": patient_id})


@app.post("/appointments")
def proxy_book(payload: dict, session: dict = Depends(require_session)):
    return _post("scheduling", "/appointments", payload)


@app.post("/appointments/{appointment_id}/cancel")
def proxy_cancel(appointment_id: int, session: dict = Depends(require_session)):
    return _post("scheduling", f"/appointments/{appointment_id}/cancel", {})


# --------------------------------------------------------------------------- #
# release of information
# --------------------------------------------------------------------------- #
@app.get("/roi/requests")
def proxy_roi_list(session: dict = Depends(require_session), patient_id: Optional[int] = None):
    return _get("roi", "/roi/requests", params={"patient_id": patient_id})


@app.post("/roi/requests")
def proxy_roi_create(payload: dict, session: dict = Depends(require_session)):
    return _post("roi", "/roi/requests", payload)


@app.post("/roi/requests/{request_id}/fulfill")
def proxy_roi_fulfill(request_id: int, session: dict = Depends(require_session)):
    return _post("roi", f"/roi/requests/{request_id}/fulfill", {})


# --------------------------------------------------------------------------- #
# interop
# --------------------------------------------------------------------------- #
@app.post("/hl7/ingest")
def proxy_hl7(payload: dict, session: dict = Depends(require_session)):
    return _post("interop", "/hl7/ingest", payload)


# --------------------------------------------------------------------------- #
# ai — intake-instruction summarizer (W1)
#
# Session-guarded like every other route: the AI feature adds NO new
# unauthenticated surface. The orchestrator's contract accepts instruction text
# only, so no patient record crosses this boundary — see adr/0005.
# --------------------------------------------------------------------------- #
@app.post("/ai/summary")
def proxy_ai_summary(payload: dict, session: dict = Depends(require_session)):
    return _post("ai", "/summary", payload)


# --------------------------------------------------------------------------- #
# ai — knowledge retrieval (W2)
#
# Query / corpus / eval are open to any authenticated session. INGEST is gated on
# the knowledge-admin capability (authz.py): one bad document silently changes
# every future grounded answer, so it is not part of the blanket `staff` role.
# --------------------------------------------------------------------------- #
@app.post("/ai/knowledge/query")
def proxy_kb_query(payload: dict, session: dict = Depends(require_session)):
    return _post("ai", "/query", payload)


@app.get("/ai/knowledge/corpus")
def proxy_kb_corpus(session: dict = Depends(require_session)):
    return _get("ai", "/corpus")


@app.post("/ai/knowledge/eval")
def proxy_kb_eval(payload: dict, session: dict = Depends(require_session)):
    return _post("ai", "/eval", payload)


@app.get("/ai/knowledge/eval/latest")
def proxy_kb_eval_latest(session: dict = Depends(require_session)):
    return _get("ai", "/eval/latest")


@app.get("/ai/knowledge/identity-clusters")
def proxy_identity_clusters(session: dict = Depends(require_session)):
    return _get("ai", "/identity/clusters")


@app.post("/ai/knowledge/ingest")
def proxy_kb_ingest(payload: dict, session: dict = Depends(require_session)):
    authz.require_ingest(session)  # 403 unless knowledge admin
    # Provenance is server-stamped from the session, never client-supplied: a
    # caller must not be able to attribute their document to someone else.
    payload = {**payload, "added_by": session.get("username", "")}
    log.info("kb ingest user=%s title=%s", session.get("username"), payload.get("title"))
    return _post("ai", "/ingest", payload)


@app.post("/ai/knowledge/seed")
def proxy_kb_seed(session: dict = Depends(require_session)):
    authz.require_ingest(session)  # seeding writes to the index → gated too
    log.info("kb seed user=%s", session.get("username"))
    return _post("ai", "/knowledge/seed", {})


# --------------------------------------------------------------------------- #
# transport helpers
# --------------------------------------------------------------------------- #
def _clean(params: Optional[dict]) -> dict:
    return {k: v for k, v in (params or {}).items() if v is not None}


def _post(service: str, path: str, payload: dict):
    try:
        r = httpx.post(f"{SERVICES[service]}{path}", json=payload, timeout=30)
        return r.json()
    except Exception as e:
        log.error("proxy POST %s%s failed: %s", service, path, e)
        return {"error": str(e)}


def _get(service: str, path: str, params: Optional[dict] = None):
    try:
        r = httpx.get(f"{SERVICES[service]}{path}", params=_clean(params), timeout=30)
        return r.json()
    except Exception as e:
        log.error("proxy GET %s%s failed: %s", service, path, e)
        return {"error": str(e)}
