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

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
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


# =========================================================================== #
# W2 — knowledge retrieval.
#
# The gateway owns WHO may ingest (authz.py); this service trusts that the
# gateway already authorized the call and records who made it. Queries against
# the patient-record collection MUST carry a scope — chroma_index raises if they
# do not, because an unscoped similarity search over charts is an IDOR that no
# code reviewer would spot.
# =========================================================================== #
_index = None
_graph = None
_last_eval = None


def get_index():
    global _index
    if _index is None:
        import chroma_index
        _index = chroma_index.ChromaIndex()
    return _index


def get_graph():
    global _graph
    if _graph is None:
        import rag_graph
        _graph = rag_graph.build_graph(get_index())
    return _graph


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=200_000)
    source: str = Field(default="", max_length=300)
    added_by: str = Field(default="", max_length=120)
    doc_id: Optional[str] = Field(default=None, max_length=120)


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    k: Optional[int] = Field(default=None, ge=1, le=20)
    mode: Optional[str] = Field(default=None, pattern="^(dense|sparse|hybrid)$")
    # Present only for record-collection queries. The gateway supplies it from
    # the session; a client cannot widen its own scope.
    patient_scope: Optional[list[int]] = None


class EvalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    k: Optional[int] = Field(default=None, ge=1, le=20)
    mode: Optional[str] = Field(default=None, pattern="^(dense|sparse|hybrid)$")


@app.post("/knowledge/seed")
def seed_corpus():
    """Load the clinic knowledge docs and the handover patient dump."""
    import corpus
    from index_port import KIND_KNOWLEDGE, KIND_RECORD

    index = get_index()
    index.reset()
    knowledge = corpus.build_knowledge_chunks()
    records = corpus.build_record_chunks()
    corpus.enforce_cap(knowledge + records, existing=0)
    n_knowledge = index.add(knowledge)
    n_records = index.add(records)
    stats = index.stats()
    log.info(
        "knowledge seed loaded knowledge_chunks=%d record_chunks=%d "
        "embed_calls=%d cache_hits=%d cap=%d",
        n_knowledge, n_records, stats.embed_calls, stats.embed_cache_hits,
        settings.corpus_max_chunks,
    )
    return {
        "knowledge_chunks": n_knowledge,
        "record_chunks": n_records,
        "corpus": stats.__dict__,
    }


# =========================================================================== #
# Ingest — two phases, one write path (adr/0014, codex F4).
#
# Phase one extracts, scrubs and STAGES. It never touches the index. Phase two
# is the only thing in this service that writes to the knowledge collection.
#
# The old single-shot `POST /ingest` is gone. It was a second, unpreviewed door
# into the highest-blast-radius write in the system, and leaving it open while
# claiming a human gate is what made the first draft of adr/0014 false.
# =========================================================================== #
def _scrub_metadata(title: str, filename: str, source: str) -> tuple[dict, list]:
    """Metadata is PHI too (RVB-ING-31..33, codex F3).

    A clean body inside `Maria Gonzalez appeal.pdf` is still a disclosure: the
    filename becomes a citation and a corpus row, both of which patients see.

    Findings are RETURNED rather than silently applied, because
    identifier-shaped metadata blocks the commit. Rewriting it quietly would hide
    from the uploader that they picked a filename they should not reuse.
    """
    found = []
    cleaned = {}
    for field_name, value in (("title", title), ("filename", filename), ("source", source)):
        result = deidentify.scrub_document(value or "")
        cleaned[field_name] = result.text
        if result.found:
            found.append({"field": field_name, "kinds": sorted(set(result.found))})
    return cleaned, found


def _build_staged(text: str, title: str, filename: str, source: str,
                  username: str, kind: str, pages: int, notes: list,
                  truncated: bool) -> "staging.StagedDoc":
    """Extract -> scrub -> count chunks. Shared by the upload and paste paths."""
    import staging
    from chunking import chunk_text

    scrub = deidentify.scrub_document(text)
    meta, meta_found = _scrub_metadata(title, filename, source)
    chunks = chunk_text(scrub.text, settings.chunk_tokens, settings.chunk_overlap_tokens)

    return staging.StagedDoc(
        staging_id="",
        title=meta["title"][:300] or "Untitled document",
        text=scrub.text,
        source=meta["source"][:300],
        filename=meta["filename"][:300],
        staged_by=username,
        kind=kind,
        pages=pages,
        chars=len(scrub.text),
        chunk_count=len(chunks),
        redactions=sorted(set(scrub.found)),
        metadata_redactions=meta_found,
        notes=notes,
        truncated=truncated,
    )


def _staging_response(doc, request_id: str) -> dict:
    import staging

    log.info("kb stage rid=%s doc=%s by=%s chunks=%d redactions=%d meta_flags=%d",
             request_id, doc.staging_id, doc.staged_by, doc.chunk_count,
             len(doc.redactions), len(doc.metadata_redactions))
    audit.emit(log, request_id=request_id, outcome="staged",
               reason=",".join(doc.redactions) or None)
    return doc.preview(staging.ttl(doc.staging_id))


def _stage_or_raise(doc):
    import staging
    try:
        return staging.stage(doc)
    except staging.TooManyStaged as e:
        raise HTTPException(status_code=429, detail=str(e))
    except staging.StagingUnavailable as e:
        # Fails CLOSED. No staging means no preview, and no preview means no
        # ingest -- we do not fall back to writing straight through.
        log.error("kb stage unavailable: %s", e)
        raise HTTPException(
            status_code=503,
            detail="The document staging store is unavailable, so uploads are "
                   "paused. Nothing was added to the knowledge base.")


@app.post("/ingest/upload")
async def ingest_upload(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    staged_by: str = Form(default=""),
):
    """Phase one, file. Extract, scrub, stage. Writes nothing to the index."""
    import extract

    rid = uuid.uuid4().hex[:12]
    filename = file.filename or "upload"

    try:
        extract.check_supported(filename)
    except extract.ExtractionError as e:
        raise HTTPException(status_code=422, detail=str(e))

    data = await file.read()
    try:
        got = extract.extract(data, filename)
    except extract.ExtractionError as e:
        log.info("kb upload rid=%s rejected=%s", rid, type(e).__name__)
        raise HTTPException(status_code=422, detail=str(e))

    doc = _build_staged(
        text=got.text, title=extract.clean_title(title, filename), filename=filename,
        source=f"uploaded file: {filename}", username=staged_by, kind=got.kind,
        pages=got.pages, notes=got.notes, truncated=got.truncated,
    )
    return _staging_response(_stage_or_raise(doc), rid)


@app.post("/ingest/stage")
def ingest_stage(req: IngestRequest):
    """Phase one, pasted text. Same staging, same preview, same commit.

    The paste path goes through the gate too (codex F4) -- otherwise "every
    knowledge write has a human gate" is false, and it was.
    """
    rid = uuid.uuid4().hex[:12]
    doc = _build_staged(
        text=req.text, title=req.title, filename="", source=req.source or "pasted text",
        username=req.added_by, kind="text", pages=1, notes=[], truncated=False,
    )
    return _staging_response(_stage_or_raise(doc), rid)


@app.get("/ingest/staged/{staging_id}")
def ingest_peek(staging_id: str, username: str = ""):
    import staging
    try:
        doc = staging.peek(staging_id, username)
    except staging.StagingForbidden as e:
        raise HTTPException(status_code=403, detail=str(e))
    except staging.StagingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except staging.StagingUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    return doc.preview(staging.ttl(staging_id))


class CommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(default="", max_length=120)


@app.post("/ingest/commit/{staging_id}")
def ingest_commit(staging_id: str, req: CommitRequest):
    """Phase two. The ONLY path that writes to the knowledge collection."""
    import corpus as corpus_mod
    import staging
    from chunking import chunk_text
    from index_port import KIND_KNOWLEDGE, IndexChunk

    rid = uuid.uuid4().hex[:12]

    try:
        doc = staging.claim(staging_id, req.username)
    except staging.StagingForbidden as e:
        raise HTTPException(status_code=403, detail=str(e))
    except staging.StagingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except staging.StagingUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Identifier-shaped metadata blocks the commit (RVB-ING-33). Checked here as
    # well as at stage time so a document cannot be staged before a scrubber
    # change and committed after one.
    if doc.metadata_redactions:
        fields = ", ".join(m["field"] for m in doc.metadata_redactions)
        log.warning("kb commit rid=%s blocked doc=%s fields=%s", rid, staging_id, fields)
        raise HTTPException(
            status_code=422,
            detail=f"Identifiers were found in the {fields}. Rename the document "
                   f"and upload it again.")

    index = get_index()
    # Deterministic in the staging id, so a retry after a partial add overwrites
    # rather than duplicating (codex F7).
    doc_id = f"doc-{staging_id[:10]}"
    chunks = [
        IndexChunk(
            id=f"{doc_id}::{c.index}",
            text=c.text,
            doc_id=doc_id,
            doc_title=doc.title,
            chunk_index=c.index,
            kind=KIND_KNOWLEDGE,
            source=doc.source,
            added_by=doc.staged_by,
        )
        for c in chunk_text(doc.text, settings.chunk_tokens, settings.chunk_overlap_tokens)
    ]

    try:
        corpus_mod.enforce_cap(chunks, existing=index.count())
    except corpus_mod.CorpusCapExceeded as e:
        raise HTTPException(status_code=409, detail=str(e))

    added = index.add(chunks)
    log.info("kb commit rid=%s doc=%s chunks=%d by=%s%s",
             rid, doc_id, added, doc.staged_by,
             (" phi_redacted=" + ",".join(doc.redactions)) if doc.redactions else "")
    audit.emit(log, request_id=rid, outcome="committed",
               reason=",".join(doc.redactions) or None)
    return {"doc_id": doc_id, "chunks": added, "title": doc.title,
            "phi_redacted": doc.redactions}


@app.post("/ingest/discard/{staging_id}")
def ingest_discard(staging_id: str, req: CommitRequest):
    import staging
    try:
        staging.discard(staging_id, req.username)
    except staging.StagingForbidden as e:
        raise HTTPException(status_code=403, detail=str(e))
    except staging.StagingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except staging.StagingUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    log.info("kb discard doc=%s by=%s", staging_id, req.username)
    return {"discarded": staging_id}


@app.get("/corpus")
def corpus_listing():
    index = get_index()
    return {"documents": index.documents(), "stats": index.stats().__dict__}


@app.post("/query")
def query(req: QueryRequest):
    import rag_graph
    from index_port import KIND_KNOWLEDGE, KIND_RECORD, ScopeRequired

    rid = uuid.uuid4().hex[:12]
    kind = KIND_RECORD if req.patient_scope is not None else KIND_KNOWLEDGE
    try:
        result = rag_graph.run(
            get_index(), req.query, k=req.k, mode=req.mode, kind=kind,
            patient_scope=req.patient_scope, graph=get_graph(),
        )
    except ScopeRequired as e:
        log.warning("query rid=%s refused reason=scope_required", rid)
        return {"request_id": rid, "answer": rag_graph.REFUSAL, "refused": True,
                "grounded": False, "reason": str(e), "citations": []}

    log.info(
        "query rid=%s kind=%s mode=%s grounded=%s refused=%s citations=%d "
        "retrieved=%d path=%s",
        rid, kind, req.mode or settings.retrieval_mode, result.grounded,
        result.refused, len(result.citations), len(result.retrieved),
        ">".join(result.path),
    )
    out = {
        "request_id": rid,
        "answer": result.answer,
        "grounded": result.grounded,
        "refused": result.refused,
        "reason": result.reason,
        "citations": result.citations,
        "usage": result.usage,
        "path": result.path,
    }
    return out


@app.post("/eval")
def run_eval(req: EvalRequest):
    global _last_eval
    import eval_harness

    run = eval_harness.run(get_index(), k=req.k, mode=req.mode, graph=get_graph())
    _last_eval = run
    log.info(
        "eval run=%s mode=%s recall=%.3f precision=%.3f dup_rate=%.3f "
        "fragment_coverage=%.3f clinically_incomplete=%d",
        run.run_id, run.mode, run.metrics["context_recall"],
        run.metrics["context_precision"], run.integrity["duplicate_patient_rate"],
        run.integrity["fragment_coverage"],
        run.integrity["clinically_incomplete_answers"],
    )
    return {**run.__dict__, "report": eval_harness.render_report(run)}


@app.get("/eval/latest")
def eval_latest():
    import eval_harness

    if _last_eval is None:
        return {"metrics": None, "note": "no eval has been run in this process"}
    return {**_last_eval.__dict__, "report": eval_harness.render_report(_last_eval)}


@app.get("/identity/clusters")
def identity_clusters():
    """The duplicate-patient finding, as data the portal can render."""
    import corpus
    import mpi

    clusters = mpi.resolve(corpus.load_patients(), settings.mpi_match_threshold)
    return {
        "duplicate_patient_rate": mpi.duplicate_rate(clusters),
        "clusters": [
            {"patient_ids": c.patient_ids, "display_name": c.display_name,
             "certain": c.certain, "reasons": c.reasons,
             "fragmented": c.is_fragmented}
            for c in clusters
        ],
        "same_as_edges": mpi.same_as_edges(clusters),
    }


# =========================================================================== #
# W3 — the front-desk eligibility assistant.
#
# One tool, visit-scoped memory, and a deterministic post-check that the agent
# never states a coverage status the tool did not return. See adr/0008.
# =========================================================================== #
_agent = None


def _lookup_eligibility(insurance_id: str) -> dict:
    """Call eligibility-service. Never raises — `unknown` is a valid answer.

    The resilience (timeout, breaker, last-known cache) lives in
    eligibility-service where it belongs. This is the transport, and its own
    failure has to degrade the same way, or we would have moved the availability
    cliff rather than removed it.
    """
    import datetime as _dt

    import httpx

    try:
        resp = httpx.get(
            f"{settings.eligibility_url}/eligibility",
            params={"insurance_id": insurance_id},
            timeout=httpx.Timeout(settings.eligibility_timeout_s),
        )
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return {"status": "unknown", "stale": False,
                "degraded_reason": "eligibility_service_unreachable"}

    checked_at = payload.get("checked_at")
    display = ""
    if checked_at:
        try:
            display = _dt.datetime.fromisoformat(
                str(checked_at).replace("Z", "+00:00")
            ).strftime("%-I:%M%p on %-d %b")
        except (ValueError, TypeError):
            display = str(checked_at)
    return {**payload, "checked_at_display": display}


def get_agent():
    global _agent
    if _agent is None:
        import eligibility_agent
        eligibility_agent.configure_tracing()
        _agent = eligibility_agent.EligibilityAgent(
            eligibility_lookup=_lookup_eligibility
        )
    return _agent


class AgentTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # thread_id for the checkpointer. A VISIT, not a patient and not a session:
    # a visit has a natural end, which bounds how long PHI-bearing conversation
    # state lives.
    visit_id: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=4000)


@app.post("/agent/eligibility")
def agent_eligibility(req: AgentTurnRequest):
    rid = uuid.uuid4().hex[:12]
    if not RETENTION.ok:
        audit.emit(log, request_id=rid, outcome="refused", reason="retention_policy",
                   retention_checked=RETENTION.checked)
        return {"request_id": rid, "reply": (
            "The assistant is disabled pending a data-retention configuration "
            "check. Contact your administrator."
        ), "tool_called": False}

    try:
        turn = get_agent().turn(req.visit_id, req.message)
    except Exception as e:  # noqa: BLE001
        audit.emit(log, request_id=rid, outcome="error", reason=type(e).__name__)
        return {"request_id": rid, "reply": (
            "The assistant is temporarily unavailable. You can still check "
            "coverage directly from the eligibility screen."
        ), "tool_called": False}

    audit.emit(
        log, request_id=rid, outcome="ok", model_id=settings.agent_model_id,
        stubbed=settings.use_stub, retention_checked=RETENTION.checked,
        reason=turn.override_reason or None,
    )
    return {
        "request_id": rid,
        "visit_id": turn.visit_id,
        "reply": turn.reply,
        "tool_called": turn.tool_called,
        "tool_status": turn.tool_status,
        "stale": turn.stale,
        "overridden": turn.overridden,
    }


# =========================================================================== #
# W4 — the assembled patient view.
#
# The gateway resolves the AuthorizedScope and passes it as plain data. This
# service does NOT decide who may see what; it receives a scope and assembles
# within it. That split is deliberate: authorization lives at the one place that
# owns sessions, and the graph's own authorize node is defence in depth.
# =========================================================================== #
_view_graph = None


def get_view_graph():
    global _view_graph
    if _view_graph is None:
        import patient_view_graph
        import patient_view_loaders
        from langgraph.checkpoint.memory import InMemorySaver

        _view_graph = patient_view_graph.build_graph(
            loaders=patient_view_loaders.default_loaders(_lookup_eligibility),
            # A durable, encrypted checkpointer in production: a run paused at
            # the sensitivity gate holds assembled PHI until a human answers.
            checkpointer=(
                __import__("eligibility_agent").build_checkpointer()[0]
                if settings.agent_durable_memory else InMemorySaver()
            ),
        )
    return _view_graph


class ScopeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal: str = "patient"
    username: str = ""
    patient_ids: list[int] = Field(default_factory=list)
    open_to_context: bool = False


class PatientViewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patient_id: int
    scope: ScopeIn
    requested_domains: Optional[list[str]] = None
    cross_patient: bool = False
    thread_id: Optional[str] = None


class ViewResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patient_id: int
    thread_id: str
    approved: bool


@app.post("/patient-view")
def patient_view(req: PatientViewRequest):
    import patient_view_graph

    rid = uuid.uuid4().hex[:12]
    view = patient_view_graph.run(
        get_view_graph(),
        patient_id=req.patient_id,
        scope=req.scope.model_dump(),
        requested_domains=req.requested_domains,
        cross_patient=req.cross_patient,
        thread_id=req.thread_id or f"view-{rid}",
    )
    log.info(
        "patient_view rid=%s authorized=%s released=%s sensitive=%s domains=%d path=%s",
        rid, view.authorized, view.released, view.sensitive,
        len(view.domains), ">".join(view.path),
    )

    thread_id = req.thread_id or f"view-{rid}"
    payload = {"request_id": rid, **_view_payload(view)}

    # A run paused at the sensitivity gate becomes a queued DECISION rather than
    # a thread id the client has to hold onto (adr/0015 rule 4, UI-D20). The
    # opaque approval id is the only handle that leaves this service.
    if view.paused:
        import approvals
        try:
            approval = approvals.register(
                patient_id=req.patient_id,
                thread_id=thread_id,
                requested_by=req.scope.username,
                requested_principal=req.scope.principal,
                authorized_ids=req.scope.patient_ids,
                reason="disclosure_shaped_assembly",
            )
            payload["approval_id"] = approval.approval_id
            payload["approval"] = approval.row()
            log.info("view paused rid=%s approval=%s span=%d",
                     rid, approval.approval_id, approval.chart_span)
        except approvals.ApprovalsUnavailable as e:
            # Fails CLOSED and says so. Without a registry the pause is
            # unresolvable, and reporting it as a normal withhold would leave a
            # clinician waiting on a decision nobody can see.
            log.error("view paused but approvals registry is down rid=%s: %s", rid, e)
            payload["approval_error"] = (
                "This view needs a release decision, but the approvals queue is "
                "unavailable. Nothing was disclosed.")

    return payload


@app.get("/approvals")
def list_approvals():
    """Runs paused at the sensitivity gate (RVB-AG-12)."""
    import approvals
    try:
        return {"approvals": [a.row() for a in approvals.list_open()]}
    except approvals.ApprovalsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    approver: str = Field(default="", max_length=120)
    approver_principal: str = Field(default="", max_length=40)
    approver_patient_id: Optional[int] = None


@app.post("/approvals/{approval_id}")
def decide_approval(approval_id: str, req: ApprovalDecision):
    """Resolve a queued decision by its OPAQUE id.

    The thread id is looked up here, server-side, from a record bound to the
    patient and the requester. The client never names a run.
    """
    import approvals
    import patient_view_graph

    rid = uuid.uuid4().hex[:12]
    try:
        approval = approvals.claim(
            approval_id,
            approver=req.approver,
            approver_principal=req.approver_principal,
            approver_patient_id=req.approver_patient_id,
        )
    except approvals.ApprovalForbidden as e:
        log.warning("approval refused rid=%s id=%s approver=%s reason=%s",
                    rid, approval_id, req.approver, e)
        raise HTTPException(status_code=403, detail=str(e))
    except approvals.ApprovalNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except approvals.ApprovalsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))

    view = patient_view_graph.resume(
        get_view_graph(), approved=req.approved,
        patient_id=approval.patient_id, thread_id=approval.thread_id,
    )
    log.info("approval decided rid=%s id=%s approved=%s by=%s released=%s",
             rid, approval_id, req.approved, req.approver, view.released)
    audit.emit(log, request_id=rid,
               outcome="released" if view.released else "withheld",
               reason=f"approval:{approval_id}")
    return {"request_id": rid, "approval_id": approval_id,
            "approved": req.approved, **_view_payload(view)}


@app.post("/patient-view/resume")
def patient_view_resume(req: ViewResumeRequest):
    """Resume a run paused at the sensitivity gate (HITL)."""
    import patient_view_graph

    rid = uuid.uuid4().hex[:12]
    view = patient_view_graph.resume(
        get_view_graph(), approved=req.approved,
        patient_id=req.patient_id, thread_id=req.thread_id,
    )
    log.info("patient_view_resume rid=%s approved=%s released=%s",
             rid, req.approved, view.released)
    return {"request_id": rid, "thread_id": req.thread_id, **_view_payload(view)}


def _view_payload(view) -> dict:
    """What leaves this service — and the one place that decides it.

    **Assembled PHI is withheld unless the view was RELEASED.** The graph's
    `withhold` node cannot do this itself: `domains` carries a merge reducer
    (`{**left, **right}`), so a node returning `{"domains": {}}` is a no-op and
    the assembled content survives into the response. Verified live before this
    guard existed -- a DENIED release still returned Maria's penicillin allergy,
    which is the exact disclosure the human gate exists to prevent, with an audit
    record saying it was refused.

    Enforced at the serialization boundary rather than in a node because this is
    the last point where the answer is unambiguous and reducer-independent.
    """
    released = bool(view.released)
    return {
        "patient_id": view.patient_id,
        "authorized": view.authorized,
        "released": released,
        "summary": view.summary if released else "",
        "grounded": view.grounded if released else False,
        # Domain STATUS is still useful to a caller -- it says which parts exist
        # and which failed -- so the shape is preserved and the content is not.
        "domains": view.domains if released else _redact_domains(view.domains),
        "deny_reason": view.deny_reason,
        "sensitive": view.sensitive,
        "approved": view.approved,
        # Distinct from `sensitive`: an interrupted invoke returns state from
        # BEFORE the gate, so `sensitive` is still False while the run is parked.
        "paused": view.paused,
        "path": view.path,
    }


def _redact_domains(domains: dict) -> dict:
    """Keep the shape, drop the content."""
    out = {}
    for name, result in (domains or {}).items():
        out[name] = {
            "domain": result.get("domain", name),
            "status": result.get("status", ""),
            "data": [],
            "note": "Not released. This section was assembled and withheld pending a release decision.",
        }
    return out


@app.get("/graph/stats")
def graph_stats(patient_ids: str = ""):
    """Knowledge-graph shape for an authorized id set. Demo/inspection only."""
    import patient_view_loaders

    ids = [int(p) for p in patient_ids.split(",") if p.strip().isdigit()]
    if not ids:
        return {"error": "patient_ids required"}
    return patient_view_loaders.build_graph_for(ids).stats()


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
