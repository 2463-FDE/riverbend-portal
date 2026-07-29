"""
gateway — backend-for-frontend / API gateway.

The Next.js portal talks only to this service; it fans out to the internal
FastAPI services and owns login/sessions.

Inherited shortcomings:
  * ~~Records fan-out never binds the session to the {patient_id} requested~~
    FIXED in W4 (adr/0011). Patient-addressed routes now resolve an
    AuthorizedScope BEFORE proxying, so an unauthorized id never reaches
    records-service. Denials are 404, not 403, to avoid an enumeration oracle.
  * Sessions never expire (D10, 164.312(a)(2)(iii)) — still open, W9.
  * One role for everyone (D7, 164.502(b)) — still open, W9. Staff scope is
    therefore COARSE: the gate narrows which PATIENT, not which staff role.
    That distinction must not be oversold as least-privilege.
"""
from typing import Optional

import httpx
from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException, Query,
                     UploadFile)
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

import authz
import scope as scope_mod
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


def _same_as_lookup(patient_id: int) -> list:
    """Identity cluster for a patient (W2 -> W4).

    Maria Gonzalez is three charts. A patient logging in must see all three, or
    the Week-4 authorization fix would turn the Week-2 fragmentation into an
    access denial and hide her own allergy from her.

    Best-effort: a failure here NARROWS the scope to self. Identity resolution
    must never be able to widen access.

    Uses ``_get_json`` rather than ``_get``: since RVB-U-09, ``_get`` returns a
    ``JSONResponse`` for the client, which has no ``.get()``. Calling it here
    would have fallen into the ``except`` on every request and silently narrowed
    every patient to their own chart — quietly undoing the Week-2 fix so that
    Maria could no longer see the fragment carrying her penicillin allergy.
    Caught by ``test_same_as_lookup_still_resolves_fragments``.
    """
    try:
        payload = _get_json("ai", "/identity/clusters")
        for cluster in (payload or {}).get("clusters", []):
            ids = cluster.get("patient_ids") or []
            if patient_id in ids:
                return ids
    except Exception:  # noqa: BLE001
        pass
    return [patient_id]


def require_scope(session: dict) -> "scope_mod.AuthorizedScope":
    return scope_mod.resolve_scope(session, same_as_lookup=_same_as_lookup)


def require_staff(session: dict) -> dict:
    """Operational / diagnostic surfaces are staff-only (codex F2).

    These endpoints are ABOUT the corpus rather than answers from it, and their
    payloads are cross-patient by construction. Verified live before this gate
    existed, as `maria.gonzalez` — a patient:

      /ai/knowledge/identity-clusters -> every patient's name and chart ids, plus
          match reasons including `identical_ssn` and `identical_address`
      /ai/knowledge/corpus            -> "James O'Brien — office_visit 2026-02-20"
      /ai/knowledge/eval              -> the same, inside identity_split_examples

    `require_session` was the only guard, which was correct while every session
    was staff. #16 gave patients sessions and did not revisit these routes.

    Uses the resolved principal rather than the raw role string: `scope.py` is
    the one place that decides what a session is, and a second opinion here is
    how the two drift apart.
    """
    if require_scope(session).principal != "staff":
        log.warning("staff-only route refused user=%s", session.get("username"))
        raise HTTPException(
            status_code=403,
            detail="This view is for clinic staff.",
        )
    return session


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
    # W4 / adr/0011: the session carries the patient binding when there is one.
    # Read from the users row -- server-derived, never client-supplied.
    token = create_session(user.username, user.role, getattr(user, "patient_id", None))
    log.info("login ok user=%s principal=%s", user.username,
             "patient" if getattr(user, "patient_id", None) else "staff")
    return {
        "token": token,
        "mfa": False,
        "user": {
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role,
            "patient_id": getattr(user, "patient_id", None),
        },
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
        # W4: the portal renders the right landing view without guessing policy.
        "patient_id": session.get("patient_id"),
        "scope": require_scope(session).as_dict(),
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
    # D11 FIXED (W4, adr/0011). The scope is resolved BEFORE anything is
    # proxied, so an unauthorized id never reaches records-service at all.
    scope_mod.require_patient_access(require_scope(session), patient_id)
    return _get("records", f"/patients/{patient_id}")


@app.get("/patients/{patient_id}/records")
def proxy_records(patient_id: int, session: dict = Depends(require_session)):
    """D11 FIXED (W4, adr/0011).

    This is the exact route the HAR walk used: a logged-in patient fetching
    /api/patients/1042/records and then /api/patients/1043/records, both 200.
    A valid session proved authentication; it never proved authorization,
    because the session carried no patient identity to check against.

    Denials are 404, not 403: a 403 on a real id and a 404 on a nonexistent one
    confirms which ids exist, which is most of what the walk was after.
    """
    scope_mod.require_patient_access(require_scope(session), patient_id)
    return _get("records", f"/patients/{patient_id}/records")


@app.get("/records/search")
def proxy_search(q: str, session: dict = Depends(require_session)):
    """Free-text search across records.

    A patient principal must not be able to full-text search the whole record
    corpus — that is a different route to the same exposure the HAR walk found.
    Staff search is unchanged (coarse, D7/W9); D8's full-table scan is measured
    and named this week, not fixed.
    """
    scope = require_scope(session)
    if not scope.open_to_context:
        raise HTTPException(
            status_code=404, detail="not found"
        )
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
    # Same gate: appointments are patient-addressed, so the same walk works here.
    scope_mod.require_patient_access(require_scope(session), patient_id)
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
    if patient_id is not None:
        scope_mod.require_patient_access(require_scope(session), patient_id)
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
@app.get("/ai/health")
def proxy_ai_health(session: dict = Depends(require_session)):
    """Health + retention posture for the AI service.

    The summary panel reads `retention.ok` on mount so it can disable submit
    BEFORE a request is made (RVB-W1-U3). Without this, the only way to discover
    the feature is disabled is to try it and read a refusal — which is a worse
    experience and spends a round trip to learn something static.
    """
    return _get("ai", "/healthz")


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
@app.get("/ai/patient-view/{patient_id}")
def proxy_patient_view(patient_id: int, session: dict = Depends(require_session)):
    """Assembled patient view (W4).

    The gateway resolves the AuthorizedScope and passes it as plain data. The
    orchestrator assembles WITHIN that scope; it never decides who may see what.
    The scope check runs here too, before anything is proxied, so an unauthorized
    id never leaves this process.
    """
    scope = require_scope(session)
    scope_mod.require_patient_access(scope, patient_id)
    return _post("ai", "/patient-view", {
        "patient_id": patient_id,
        "scope": scope.as_dict(),
        "thread_id": f"view-{session.get('username', 'anon')}-{patient_id}",
    })


@app.post("/ai/patient-view/{patient_id}/resume")
def proxy_patient_view_resume(patient_id: int, payload: dict,
                              session: dict = Depends(require_session)):
    """Answer a run paused at the sensitivity gate (HITL)."""
    scope_mod.require_patient_access(require_scope(session), patient_id)
    return _post("ai", "/patient-view/resume", {
        "patient_id": patient_id,
        "thread_id": payload.get("thread_id", ""),
        "approved": bool(payload.get("approved")),
    })


@app.post("/ai/agent/eligibility")
def proxy_agent_eligibility(payload: dict, session: dict = Depends(require_session)):
    """Front-desk eligibility assistant (W3). Session-guarded like everything else."""
    return _post("ai", "/agent/eligibility", payload)


@app.post("/ai/knowledge/query")
def proxy_kb_query(payload: dict, session: dict = Depends(require_session)):
    """Clinic knowledge only. This endpoint may NEVER reach the record collection.

    It used to forward `payload` verbatim. The orchestrator picks its collection
    from the presence of `patient_scope`:

        kind = KIND_RECORD if req.patient_scope is not None else KIND_KNOWLEDGE

    so any authenticated session -- including a patient, since #16 -- could post
    `{"query": ..., "patient_scope": [1043]}` and receive another patient's chart,
    grounded and cited by name. Verified live before the fix: maria.gonzalez,
    scope [1042, 1330, 1588], retrieved chart 1043.

    `QueryRequest.patient_scope` carried the comment "the gateway supplies it from
    the session; a client cannot widen its own scope." The gateway did neither.
    The comment described the intended design and nothing enforced it.

    Rejected loudly rather than stripped silently: a caller that tries to widen
    scope should be told no, and the 400 is what the regression test pins.
    """
    if "patient_scope" in payload:
        log.warning("kb query rejected user=%s reason=client_supplied_scope",
                    session.get("username"))
        raise HTTPException(
            status_code=400,
            detail="patient_scope is not accepted here. Use /ai/records/query, "
                   "which derives scope from your session.",
        )
    return _post("ai", "/query", payload)


@app.post("/ai/records/query")
def proxy_records_query(payload: dict, session: dict = Depends(require_session)):
    """Chart-shaped questions, scoped by the SERVER (RVB-W2-U5).

    Same split as `/patients/{id}/records`: authorization is resolved from the
    session before anything is proxied, and the scope is overwritten rather than
    merged, so a client-supplied value cannot survive.
    """
    scope = require_scope(session)
    ids = sorted(scope.patient_ids)

    # Staff are "open to context" -- no fixed id set -- so a records query needs
    # an explicit patient, checked against the scope the same way every other
    # record read is. Without this, `ids` is empty for staff and the query would
    # silently return nothing.
    if scope.open_to_context:
        requested = payload.get("patient_id")
        if not isinstance(requested, int):
            raise HTTPException(
                status_code=400,
                detail="patient_id is required when searching records as staff.",
            )
        scope_mod.require_patient_access(scope, requested)
        ids = [requested]

    if not ids:
        raise HTTPException(
            status_code=403, detail="This session has no records in scope.")

    body = {k: v for k, v in payload.items() if k not in ("patient_scope", "patient_id")}
    log.info("records query user=%s scope=%s", session.get("username"), ids)
    return _post("ai", "/query", {**body, "patient_scope": ids})


@app.get("/ai/knowledge/corpus")
def proxy_kb_corpus(session: dict = Depends(require_session)):
    # Lists every indexed document, and record titles carry patient names and
    # encounter dates. Staff-only (codex F2).
    require_staff(session)
    return _get("ai", "/corpus")


@app.post("/ai/knowledge/eval")
def proxy_kb_eval(payload: dict, session: dict = Depends(require_session)):
    # The report embeds identity_split_examples: named patients, chart ids, and
    # the match reasons behind them. Staff-only (codex F2).
    require_staff(session)
    return _post("ai", "/eval", payload)


@app.get("/ai/knowledge/eval/latest")
def proxy_kb_eval_latest(session: dict = Depends(require_session)):
    require_staff(session)
    return _get("ai", "/eval/latest")


@app.get("/ai/knowledge/identity-clusters")
def proxy_identity_clusters(session: dict = Depends(require_session)):
    # The single most sensitive diagnostic in the system: it names every patient,
    # links their duplicate charts, and states WHY they matched -- including
    # `identical_ssn`. Staff-only (codex F2).
    require_staff(session)
    return _get("ai", "/identity/clusters")


# --------------------------------------------------------------------------- #
# Knowledge ingest — two phases, one write path (adr/0014, codex F4)
#
# `POST /ai/knowledge/ingest` is GONE. It wrote straight to the index after a
# lenient scrub, which meant a privileged user could bypass the preview entirely
# and the "human gate on every knowledge write" claim was false. Paste and upload
# now stage through the same preview and the same commit.
# --------------------------------------------------------------------------- #
UPLOAD_MAX_BYTES = 10 * 1024 * 1024      # RVB-ING-08


@app.post("/ai/knowledge/upload")
async def proxy_kb_upload(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    session: dict = Depends(require_session),
):
    """Phase one. Stages a preview; writes nothing to the index."""
    authz.require_ingest(session)

    # Read in bounded chunks and abort the moment the cap is passed (RVB-ING-35).
    # Reading the whole body and then measuring it is the version of this check
    # that does not protect anything.
    buf = bytearray()
    while chunk := await file.read(64 * 1024):
        buf.extend(chunk)
        if len(buf) > UPLOAD_MAX_BYTES:
            log.warning("kb upload oversize user=%s", session.get("username"))
            raise HTTPException(
                status_code=413,
                detail=f"That file is larger than the "
                       f"{UPLOAD_MAX_BYTES // (1024 * 1024)} MB limit.")

    log.info("kb upload user=%s filename=%s bytes=%d",
             session.get("username"), file.filename, len(buf))

    # `staged_by` is server-stamped here and never read from the request, so a
    # caller cannot attribute a document to someone else.
    try:
        r = httpx.post(
            f"{SERVICES['ai']}/ingest/upload",
            files={"file": (file.filename or "upload", bytes(buf),
                            file.content_type or "application/octet-stream")},
            data={"title": title, "staged_by": session.get("username", "")},
            timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("kb upload transport failure: %s", type(e).__name__)
        return JSONResponse(status_code=502,
                            content={"detail": "the assistant service is unavailable"})
    return _relay(r)


@app.post("/ai/knowledge/stage")
def proxy_kb_stage(payload: dict, session: dict = Depends(require_session)):
    """Phase one, pasted text. Same gate as upload."""
    authz.require_ingest(session)
    payload = {**payload, "added_by": session.get("username", "")}
    log.info("kb stage user=%s title=%s", session.get("username"), payload.get("title"))
    return _post("ai", "/ingest/stage", payload)


@app.get("/ai/knowledge/staged/{staging_id}")
def proxy_kb_staged(staging_id: str, session: dict = Depends(require_session)):
    authz.require_ingest(session)
    return _get("ai", f"/ingest/staged/{staging_id}",
                params={"username": session.get("username", "")})


@app.post("/ai/knowledge/staged/{staging_id}/commit")
def proxy_kb_commit(staging_id: str, session: dict = Depends(require_session)):
    """Phase two. The only path that reaches the index.

    Takes no body from the client: the username is the session's, and the
    document is whatever was staged. There is nothing here for a client to
    influence.
    """
    authz.require_ingest(session)
    log.info("kb commit user=%s staging=%s", session.get("username"), staging_id)
    return _post("ai", f"/ingest/commit/{staging_id}",
                 {"username": session.get("username", "")})


@app.post("/ai/knowledge/staged/{staging_id}/discard")
def proxy_kb_discard(staging_id: str, session: dict = Depends(require_session)):
    authz.require_ingest(session)
    return _post("ai", f"/ingest/discard/{staging_id}",
                 {"username": session.get("username", "")})


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


def _relay(r: httpx.Response) -> JSONResponse:
    """Relay a downstream response, PRESERVING its status code.

    W1-UI / RVB-U-09. This previously returned ``r.json()`` and discarded
    ``r.status_code``, so a downstream 422 or 503 reached the browser as **HTTP
    200 with an error body** — indistinguishable from success to any caller.

    Worth being precise about what this did and did not affect, because the
    distinction is load-bearing: exceptions the gateway RAISES itself
    (``require_patient_access`` → 404, ``require_session`` → 401,
    ``authz.require_ingest`` → 403) are raised before the proxy call and always
    carried their status. Only DOWNSTREAM service errors were flattened. So the
    IDOR 404 that Week 4 rests on was never affected — but every service-level
    failure looked like success, which is why the UI could not render an error
    state honestly.
    """
    try:
        data = r.json()
    except ValueError:
        # A downstream that returned non-JSON is itself a fault worth surfacing,
        # not something to paper over with an empty body.
        data = {"detail": (r.text or "")[:500] or "upstream returned no body"}
    return JSONResponse(content=data, status_code=r.status_code)


def _get_json(service: str, path: str, params: Optional[dict] = None) -> Optional[dict]:
    """Fetch a downstream body for the gateway's OWN use, not for relaying.

    Distinct from ``_get`` on purpose. ``_get`` builds a client-facing response;
    this returns the parsed body so gateway logic can read it. Conflating the two
    is how ``_same_as_lookup`` nearly started narrowing every patient to a single
    chart.
    """
    try:
        r = httpx.get(f"{SERVICES[service]}{path}", params=_clean(params), timeout=30)
        return r.json() if r.is_success else None
    except Exception as e:  # noqa: BLE001
        log.warning("internal GET %s%s failed: %s", service, path, type(e).__name__)
        return None


def _post(service: str, path: str, payload: dict):
    try:
        r = httpx.post(f"{SERVICES[service]}{path}", json=payload, timeout=30)
        return _relay(r)
    except Exception as e:
        log.error("proxy POST %s%s failed: %s", service, path, type(e).__name__)
        # 502, not 200-with-an-error-key: the caller asked us to reach something
        # and we could not. Returning 200 here is how a broken dependency looks
        # like a working feature.
        return JSONResponse({"error": "upstream unavailable"}, status_code=502)


def _get(service: str, path: str, params: Optional[dict] = None):
    try:
        r = httpx.get(f"{SERVICES[service]}{path}", params=_clean(params), timeout=30)
        return _relay(r)
    except Exception as e:
        log.error("proxy GET %s%s failed: %s", service, path, type(e).__name__)
        return JSONResponse({"error": "upstream unavailable"}, status_code=502)
