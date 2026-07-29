"""ai-orchestrator — the AI intake-summary service (W1).

Endpoint contract, deliberately narrow:

    POST /summary  {"instructions": "<non-PHI intake instruction text>"}
      -> {request_id, summary, grounded, needs_review, model, stubbed, usage}

What changed versus the contractor's version (git ``2a5039d``, removed at
``d0905a1``):

  * Accepts INSTRUCTION TEXT ONLY. There is no ``patient_id``, ``name``, ``dob``
    or ``notes`` field — a patient record is not rejected by validation, it is
    **inexpressible**. ``deidentify.scrub_instructions`` is the backstop for text
    that contains an identifier anyway.
  * The Bedrock call is deadline-bounded, retried with classified backoff,
    token-budgeted and cost-guarded (``model_client``).
  * Output is validated (``guardrails``); a failed check returns a safe message
    with ``needs_review=true``, never raw model text.
  * Logging is PHI-safe and structured: one closed-key-set audit event per call,
    no bodies (``audit``, ``logging_config``).
  * The service refuses to serve unless Bedrock's effective data-retention mode is
    ``none`` (``retention``, ADR 0004 §1a).
"""
import uuid
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

import audit
import deidentify
import guardrails
import model_client
import retention
from config import settings
from logging_config import configure

log = configure(settings.service_name)
app = FastAPI(title="Riverbend ai-orchestrator", version="1.0.0")

_SYSTEM = (
    "You rewrite clinic intake instructions into a short, plain-language summary "
    "a patient can understand. Use ONLY the information in the provided "
    "instructions. Do not add medications, dosages, diagnoses, or any fact that "
    "is not present in the text. Respond ONLY with a JSON object of the form "
    '{"summary": "..."} and nothing else.'
)

# Resolved once at import. Failing at startup is strictly better than failing at
# the first PHI-adjacent request, because by then someone is watching a demo.
RETENTION = retention.check()
if not RETENTION.ok:
    log.error(
        "retention preflight FAILED: %s — /summary will refuse to serve", RETENTION.reason
    )


class SummaryRequest(BaseModel):
    # extra="forbid" is load-bearing: it makes "someone adds patient_id later" a
    # 422 rather than a silent PHI path. Paired with a test that asserts the
    # field set is exactly {instructions}.
    model_config = ConfigDict(extra="forbid")
    instructions: str = Field(default="", max_length=20000)


class SummaryResponse(BaseModel):
    request_id: str
    summary: str
    grounded: bool
    needs_review: bool
    model: str
    stubbed: bool
    usage: dict


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "service": settings.service_name,
        "stub": settings.use_stub,
        "retention": RETENTION.as_dict(),
    }


def _refuse(rid: str, reason: str, message: Optional[str] = None) -> SummaryResponse:
    return SummaryResponse(
        request_id=rid,
        summary=message or guardrails.safe_fallback(),
        grounded=False,
        needs_review=True,
        model=settings.summary_model_id,
        stubbed=settings.use_stub,
        usage={"refused": reason},
    )


@app.post("/summary", response_model=SummaryResponse)
def summarize(req: SummaryRequest):
    rid = uuid.uuid4().hex[:12]

    # Compliance gate first: never spend, and never send anything, if we cannot
    # prove where it would be retained.
    if not RETENTION.ok:
        audit.emit(log, request_id=rid, outcome="refused", reason="retention_policy",
                   retention_checked=RETENTION.checked)
        return _refuse(
            rid, "retention_policy",
            "The summary service is disabled pending a data-retention "
            "configuration check. Contact your administrator.",
        )

    source = (req.instructions or "").strip()
    if len(source) < settings.min_source_chars:
        audit.emit(log, request_id=rid, outcome="refused", reason="source_too_short")
        return _refuse(rid, "source_too_short")

    scrub = deidentify.scrub_instructions(source)
    clean = scrub.text

    client = model_client.ModelClient(settings.summary_model_id)
    try:
        result = client.invoke(
            _SYSTEM,
            f"Intake instructions:\n{clean}\n\nReturn JSON only.",
            structured_key="summary",
            stub_text=_stub_summary(clean),
            grounding_source=clean,
        )
    except model_client.BudgetError as e:
        audit.emit(log, request_id=rid, outcome="refused", reason=f"budget:{e}",
                   phi_redacted=scrub.found or None)
        return _refuse(rid, "budget")
    except model_client.GuardrailBlocked:
        audit.emit(log, request_id=rid, outcome="refused", reason="guardrail_blocked",
                   guardrail_action="GUARDRAIL_INTERVENED", phi_redacted=scrub.found or None)
        return _refuse(rid, "guardrail_blocked")
    except model_client.ModelUnavailable as e:
        audit.emit(log, request_id=rid, outcome="error", reason=str(e)[:120],
                   phi_redacted=scrub.found or None)
        return _refuse(
            rid, "model_unavailable",
            "The summary service is temporarily unavailable. Please try again shortly.",
        )

    verdict = guardrails.check(result.text, clean, settings.grounding_threshold)
    summary = result.text if verdict.grounded else guardrails.safe_fallback()

    audit.emit(
        log,
        request_id=rid,
        outcome="ok",
        model_id=result.model_id,
        stubbed=result.stubbed,
        grounded=verdict.grounded,
        grounding_score=verdict.score,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        est_cost_usd=result.est_cost_usd,
        latency_ms=result.latency_ms,
        attempts=result.attempts,
        guardrail_action=result.guardrail_action,
        retention_checked=RETENTION.checked,
        phi_redacted=scrub.found or None,
        reason="|".join(verdict.reasons) if verdict.reasons else None,
    )

    return SummaryResponse(
        request_id=rid,
        summary=summary,
        grounded=verdict.grounded,
        needs_review=verdict.needs_review,
        model=result.model_id,
        stubbed=result.stubbed,
        usage={
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "est_cost_usd": result.est_cost_usd,
            "grounding_score": verdict.score,
        },
    )


def _stub_summary(instructions: str) -> str:
    """A GROUNDED stub: derived from the instructions, so dev output is faithful.

    The contractor's stub deliberately hallucinated. Ours does not — tests inject
    ungrounded text explicitly to exercise the guardrail, which is a better shape
    because the hallucination is then visible in the test rather than ambient in
    the fixture.
    """
    import json

    first = " ".join((instructions or "").split())[:240]
    body = first or "please contact the clinic for details"
    return json.dumps({
        "summary": "Here's what to know for your visit: "
                   + body + (" ..." if len(instructions or "") > 240 else "")
    })
