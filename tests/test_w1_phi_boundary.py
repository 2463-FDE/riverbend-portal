"""W1 — the PHI boundary: contract shape, scrubbing, logging, and audit events.

This file is the inverse of debt D1. `intake-service` logs the full request body
at INFO to a repo-level file; these tests assert that the AI path cannot do that
even if someone tries.
"""
import logging

import pytest
from fastapi.testclient import TestClient

from conftest import load_module

deid = load_module("services/ai-orchestrator/deidentify.py", "w1_deidentify")
audit = load_module("services/ai-orchestrator/audit.py", "w1_audit")
logging_config = load_module("services/ai-orchestrator/logging_config.py", "w1_logging")
app_mod = load_module("services/ai-orchestrator/app.py", "w1_app")

client = TestClient(app_mod.app)

# A deliberately identifier-rich instruction blob. Every value here must be
# absent from every log record the request produces.
PHI_TEXT = (
    "Patient Maria Gonzalez, DOB 1971-03-02, SSN 123-45-6789, "
    "MRN: RB-88213, phone 555-867-5309, email maria.g@example.com. "
    "Please arrive fifteen minutes before your appointment and bring your "
    "insurance card and a photo ID. Do not eat for eight hours beforehand."
)

IDENTIFIERS = ["1971-03-02", "123-45-6789", "RB-88213", "555-867-5309",
               "maria.g@example.com"]


# --------------------------------------------------------------------------- #
# 8 — a patient record is inexpressible at the boundary
# --------------------------------------------------------------------------- #
def test_request_model_has_no_patient_fields():
    """Schema-level guard: the field set is exactly {instructions}.

    This is not style. It is the primary control — a patient record is not
    rejected by validation, it cannot be expressed. The test exists so that
    adding `patient_id` later fails here rather than silently opening a PHI path.
    """
    assert set(app_mod.SummaryRequest.model_fields) == {"instructions"}


@pytest.mark.parametrize("field", ["patient_id", "name", "dob", "ssn", "notes", "mrn"])
def test_patient_fields_are_rejected(field):
    r = client.post("/summary", json={"instructions": "x" * 40, field: "value"})
    assert r.status_code == 422, f"{field} was accepted — extra='forbid' is not in force"


# --------------------------------------------------------------------------- #
# 9/10 — the two scrubs, deliberately different
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind,sample", [
    ("ssn", "SSN 123-45-6789 on file"),
    ("email", "reach me at maria.g@example.com please"),
    ("phone", "call 555-867-5309 to reschedule"),
    ("mrn", "MRN: RB-88213 attached"),
    ("date", "date of birth 1971-03-02 confirmed"),
    ("long_num", "reference 12345678901 in the system"),
])
def test_scrub_instructions_redacts(kind, sample):
    result = deid.scrub_instructions(sample)
    assert kind in result.found, f"{kind} not detected in {sample!r}"
    assert f"[REDACTED-{kind.upper()}]" in result.text
    assert not result.clean


def test_scrub_document_preserves_dates_and_clinic_contact():
    """A policy document legitimately contains dates and the clinic's number.

    Redacting them destroys the document's meaning for no privacy gain — which is
    why `scrub_document` is deliberately more lenient than `scrub_instructions`.
    """
    doc = (
        "Effective 2026-01-01, cancellations must be made 24 hours ahead. "
        "Call 555-0100 or email frontdesk@riverbend.example.com. "
        "Do not include SSN 123-45-6789 in correspondence."
    )
    result = deid.scrub_document(doc)
    assert "2026-01-01" in result.text, "a policy effective date is content, not PHI"
    assert "555-0100" in result.text, "the clinic's own number is content"
    assert "frontdesk@riverbend.example.com" in result.text
    assert "123-45-6789" not in result.text, "an SSN is never content"
    assert "ssn" in result.found


def test_scrub_record_strips_direct_identifiers_but_keeps_clinical_content():
    rec = (
        "Encounter 2026-03-14. Patient reports penicillin allergy. "
        "SSN 123-45-6789. Contact 555-867-5309."
    )
    result = deid.scrub_record(rec)
    assert "penicillin" in result.text, "clinical content must survive"
    assert "2026-03-14" in result.text, "an encounter date is what makes a record useful"
    assert "123-45-6789" not in result.text
    assert "555-867-5309" not in result.text


def test_clinical_path_is_a_tripwire():
    """Routing a real record through W1 must fail loudly, not ship PHI."""
    with pytest.raises(deid.ClinicalPathNotAvailable):
        deid.safe_harbor_scrub("any encounter text")


# --------------------------------------------------------------------------- #
# 11 — no PHI in any log record
# --------------------------------------------------------------------------- #
def test_no_phi_in_any_log_record(caplog):
    with caplog.at_level(logging.DEBUG):
        # caplog attaches to the root logger; our service logger sets
        # propagate=False, so attach the capture handler to it directly.
        svc = logging.getLogger("ai-orchestrator")
        svc.addHandler(caplog.handler)
        try:
            r = client.post("/summary", json={"instructions": PHI_TEXT})
        finally:
            svc.removeHandler(caplog.handler)

    assert r.status_code == 200
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    for identifier in IDENTIFIERS:
        assert identifier not in blob, f"{identifier} leaked into a log record"
    assert "Please arrive fifteen minutes" not in blob, "instruction body leaked"


def test_redacting_filter_catches_a_careless_log(caplog):
    """Backstop, not the control: a future careless log.info is still redacted."""
    logger = logging.getLogger("test-careless")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = caplog.handler
    handler.addFilter(logging_config.RedactingFilter())
    logger.addHandler(handler)
    try:
        with caplog.at_level(logging.INFO):
            logger.info("request body=%s", PHI_TEXT)
    finally:
        handler.removeFilter(handler.filters[-1])
        logger.removeHandler(handler)

    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    for identifier in IDENTIFIERS:
        assert identifier not in blob


# --------------------------------------------------------------------------- #
# 12 — the audit event's key set is closed
# --------------------------------------------------------------------------- #
def test_audit_rejects_non_allowlisted_field():
    """A comment saying 'do not log bodies' survives until 6pm on a Friday.

    A ValueError survives indefinitely.
    """
    with pytest.raises(audit.AuditFieldError):
        audit.build(request_id="abc", instructions="the actual body")


def test_audit_event_fields_are_all_allowlisted():
    event = audit.build(
        request_id="abc", outcome="ok", model_id="m", stubbed=True,
        grounded=True, grounding_score=0.9, input_tokens=10, output_tokens=5,
        est_cost_usd=0.0001, latency_ms=12, attempts=1, phi_redacted=["ssn"],
    )
    assert set(event) <= audit.AUDIT_FIELDS
    rendered = audit.render(event)
    assert "request_id=abc" in rendered
    assert "phi_redacted=ssn" in rendered


def test_audit_never_carries_a_body_key():
    for forbidden in ("prompt", "response", "instructions", "summary", "body",
                      "patient_id", "name", "text"):
        assert forbidden not in audit.AUDIT_FIELDS, (
            f"{forbidden!r} in AUDIT_FIELDS would let a body into the audit trail"
        )
