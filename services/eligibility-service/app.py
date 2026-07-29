"""
eligibility-service — real-time payer eligibility (X12 270/271).

Week 3 rebuild. What changed and why:

The inherited `check.py` had, in its own words, "no timeout, no retry, no circuit
breaker, no cache" — and `intake-service` called it INLINE on the registration
request thread. On Tuesday 09:02–09:21 the clearinghouse degraded for nineteen
minutes and the front desk could not register anyone. Not "eligibility was
slow" — registration, which has nothing to do with insurance, stopped.

`payer_client.PayerClient` now owns the call: async, timeout-bounded, circuit
broken, and backed by a last-known cache. **It never raises**, because an
exception here propagates into `/intake` and rebuilds the outage.

`check.py` is left in place, unused, so the diff shows exactly what was replaced.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

import payer_client
from config import settings
from logging_config import configure

log = configure(settings.service_name)
app = FastAPI(title="Riverbend eligibility-service", version="2.0.0")

_client = payer_client.PayerClient()


class EligibilityOut(BaseModel):
    insurance_id: str
    status: str                      # active | inactive | unknown
    active: Optional[bool]           # None when unknown — NOT False
    payer: Optional[str] = None
    checked_at: datetime
    stale: bool = False
    degraded_reason: str = ""
    raw_status: Optional[int] = None
    latency_ms: int = 0
    message: str = ""


@app.get("/healthz")
def healthz():
    stats = _client.breaker.stats()
    return {
        "status": "ok",
        "service": settings.service_name,
        "breaker": {
            "state": stats.state,
            "consecutive_failures": stats.consecutive_failures,
            "trips": stats.trips,
            "short_circuited": stats.short_circuited,
        },
    }


@app.get("/eligibility", response_model=EligibilityOut)
async def check_eligibility(insurance_id: str = Query(...)):
    """Always answers. `unknown` is a valid status, and it is not `inactive`.

    Conflating "we could not check" with "not covered" is how a covered patient
    gets turned away at the desk, so `active` is None rather than False when the
    status is unknown. The distinction is the whole reason the field is nullable.
    """
    insurance_id = (insurance_id or "").strip()
    if not insurance_id:
        raise HTTPException(status_code=422, detail="insurance_id must not be blank")

    result = await _client.check(insurance_id)

    # PHI-safe: the member id is an identifier, so it is not logged (W1 policy).
    log.info(
        "eligibility status=%s stale=%s reason=%s latency_ms=%d breaker=%s",
        result.status, result.stale, result.degraded_reason or "-",
        result.latency_ms, _client.breaker.stats().state,
    )

    return EligibilityOut(
        insurance_id=result.insurance_id,
        status=result.status,
        active=result.active,
        payer=result.payer,
        checked_at=datetime.fromtimestamp(result.checked_at, tz=timezone.utc),
        stale=result.stale,
        degraded_reason=result.degraded_reason,
        raw_status=result.raw_status,
        latency_ms=result.latency_ms,
        message=payer_client.describe(result),
    )


@app.get("/breaker")
def breaker_state():
    """Operational visibility on the breaker.

    Nobody was alerted on Tuesday; the incident was found by the front desk and
    reconstructed by us from a vendor's status page. This endpoint is the seam
    Week 7's alerting hangs off. Naming that gap is part of this week's finding.
    """
    return _client.breaker.stats().__dict__
