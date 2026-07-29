"""
intake-service — multi-step patient registration + insurance + consent capture.

Both the front desk and the self-service portal POST a full intake payload here.
We create the patient chart, attach insurance coverage (if supplied), record the
signed consents, and verify payer eligibility before returning.

Inherited shortcomings (left as-is from the handoff):
  * D1 — the full request body (PHI: name/dob/ssn/notes) is written to a file
    log at INFO. See logging_config.py.
  * D5 — no master patient index / match key: every /intake creates a brand new
    patients row, so one person forks into several charts (intake.yaml match_key:
    none).
  * D4 / RIV-088 / RIV-141 — FIXED in W3 (adr/0008). Eligibility used to be
    verified inline on the request thread with no timeout, which is why
    registration "spun ~4-5s" and why a 19-minute payer outage froze the whole
    intake screen. It is now off the critical path.
  * Consents are inserted one at a time (a commit per consent).
"""
import os
import threading
import time
from typing import Any, Optional

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config import settings
from db import SessionLocal, get_db
from logging_config import configure
from models import Consent, InsuranceCoverage, Patient
from schemas import Demographics, Insurance, IntakeRequest, IntakeResponse

log = configure(settings.service_name)
app = FastAPI(title="Riverbend intake-service", version="1.3.0")

INTAKE_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "intake.yaml")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": settings.service_name}


@app.get("/intake/config")
def intake_config():
    """Return the parsed intake.yaml so the front-desk UI can adapt its form."""
    try:
        with open(INTAKE_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.error("intake config missing at %s", INTAKE_CONFIG_PATH)
        raise HTTPException(status_code=500, detail="intake config not found")
    except yaml.YAMLError as e:
        log.error("intake config parse error: %s", e)
        raise HTTPException(status_code=500, detail="intake config invalid")


@app.post("/intake", response_model=IntakeResponse, status_code=201)
def create_intake(req: IntakeRequest, db: Session = Depends(get_db)):
    started = time.time()

    # D1 (flagged, not fixed): persist the entire request body — including PHI —
    # to the file handler so the front desk has a record of every registration.
    log.info('POST /intake body=%s', req.model_dump_json())

    # D5 (flagged, not fixed): no MPI / match-key lookup on (name, dob, ssn).
    # Every intake inserts a brand new chart, even for a returning patient.
    patient_id = _create_patient(db, req.demographics)

    if req.insurance is not None:
        _create_coverage(db, patient_id, req.insurance)

    # D4 / RIV-088 / RIV-141 — FIXED in W3 (adr/0008).
    #
    # Eligibility used to be verified INLINE on this request thread, with no
    # timeout, behind an artificial 4.2s sleep standing in for the clearinghouse
    # round trip. That is why registration "spun 4-5 seconds" every time, and why
    # a 19-minute payer outage on Tuesday 09:02-09:21 froze the entire intake
    # screen: a third party's outage became Riverbend's outage in a subsystem
    # that has nothing to do with insurance.
    #
    # Registration no longer waits for the payer. The chart is created and the
    # response returns; coverage resolves separately and updates the record. The
    # front desk can register patients during a total payer outage — that is the
    # deliverable, and tests/test_w3_intake_decoupling.py is the regression test.
    _record_consents(db, patient_id, req.consents)

    eligibility = _schedule_eligibility(patient_id, req.insurance)

    elapsed = round(time.time() - started, 2)
    log.info("POST /intake 201 patient_id=%s elapsed=%.2fs", patient_id, elapsed)
    return IntakeResponse(patient_id=patient_id, elapsed_seconds=elapsed, eligibility=eligibility)


def _create_patient(db: Session, demo: Demographics) -> int:
    try:
        patient = Patient(
            name=demo.name,
            dob=demo.dob,
            ssn=demo.ssn,
            gender=demo.gender,
            address=demo.address,
            phone=demo.phone,
            email=demo.email,
            notes=demo.notes,
            created_via=demo.created_via,
        )
        db.add(patient)
        db.commit()
        db.refresh(patient)
        return patient.id
    except SQLAlchemyError as e:
        db.rollback()
        log.error("intake: failed to create patient: %s", e)
        raise HTTPException(status_code=503, detail="patient store unavailable")


def _create_coverage(db: Session, patient_id: int, ins: Insurance) -> None:
    try:
        coverage = InsuranceCoverage(
            patient_id=patient_id,
            payer_name=ins.payer_name,
            member_id=ins.member_id,
            group_number=ins.group_number,
            plan_type=ins.plan_type,
        )
        db.add(coverage)
        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        log.error("intake: failed to record coverage for patient %s: %s", patient_id, e)
        raise HTTPException(status_code=503, detail="coverage store unavailable")


def _record_consents(db: Session, patient_id: int, kinds: list[str]) -> None:
    # Inefficient by design: one INSERT + COMMIT per consent (a separate
    # transaction round-trip each) rather than a single batched insert.
    for kind in kinds:
        try:
            db.add(Consent(patient_id=patient_id, kind=kind))
            db.commit()
        except SQLAlchemyError as e:
            db.rollback()
            log.error("intake: failed to record consent %s for patient %s: %s", kind, patient_id, e)


def _schedule_eligibility(patient_id: int, ins: Optional[Insurance]) -> Optional[dict[str, Any]]:
    """Kick eligibility off the registration path. W3 / adr/0008.

    Registration returns `pending`. Coverage resolves in the background and
    updates `insurance_coverages`. The front desk gets the chart immediately and
    the coverage status when it arrives.

    Why a thread rather than a task queue: this service is synchronous FastAPI
    and there is no worker in the stack. A background thread is the smallest
    change that removes the payer from the critical path, and it is honest about
    what it is — the durable-queue version is named in adr/0008 as the next step.
    What matters for the client is that a hung payer can no longer hold a
    registration open, and that is true either way.
    """
    if ins is None or not ins.member_id:
        return None

    def _resolve() -> None:
        try:
            resp = httpx.get(
                f"{settings.eligibility_url}/eligibility",
                params={"insurance_id": ins.member_id},
                # Bounded. eligibility-service degrades internally well before
                # this fires; the ceiling exists so a hung DOWNSTREAM cannot leak
                # threads here either.
                timeout=httpx.Timeout(settings.eligibility_timeout_s),
            )
            payload = resp.json()
            _update_coverage_status(patient_id, payload)
            log.info(
                "intake: eligibility resolved patient_id=%s status=%s stale=%s",
                patient_id, payload.get("status"), payload.get("stale"),
            )
        except Exception as e:  # noqa: BLE001 — background work never breaks intake
            log.warning(
                "intake: eligibility resolution failed patient_id=%s error=%s",
                patient_id, type(e).__name__,
            )

    threading.Thread(target=_resolve, name=f"eligibility-{patient_id}", daemon=True).start()

    # `pending` is not `inactive`. The front desk must be able to tell "we have
    # not checked yet" from "not covered" — conflating them is how a covered
    # patient gets turned away.
    return {"status": "pending", "active": None,
            "message": "Coverage check in progress; registration is complete."}


def _update_coverage_status(patient_id: int, payload: dict[str, Any]) -> None:
    """Write the resolved status back to the coverage row."""
    status = payload.get("status") or "unknown"
    try:
        with SessionLocal() as db:
            coverage = (
                db.query(InsuranceCoverage)
                .filter(InsuranceCoverage.patient_id == patient_id)
                .order_by(InsuranceCoverage.id.desc())
                .first()
            )
            if coverage is None:
                return
            coverage.status = status
            if not payload.get("stale"):
                coverage.verified_at = func.now()
            db.commit()
    except SQLAlchemyError as e:
        log.error("intake: could not persist eligibility for %s: %s", patient_id, e)
