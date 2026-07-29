# ADR 0014 — Knowledge document ingestion: file upload as a two-phase disclosure gate

- **Status:** Accepted
- **Date:** 2026-07-29
- **Spec:** `docs/specs/ui-ingestion-and-agent-workflow.md` §1–§4
- **Debate:** `docs/design-debate-w1-w4.md` UI-D9, UI-D10, UI-D14
- **Supersedes nothing.** Extends `adr/0006` (Chroma behind a port) and the W2
  ingest path introduced in `#14`.

## Context

Today, adding a document to the assistant's knowledge base means pasting text:

```python
class IngestRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    text:  str = Field(min_length=1, max_length=200_000)
```

The client asked for employees to **upload documents**. Treated as a UI task
that is a file input. It is not, for one reason that decides the whole design.

### The knowledge collection has no scope filter, and patients can reach it

`riverbend_knowledge` is deliberately readable by any authenticated session —
clinic policy should be answerable to whoever asks. The gateway reflects that:

```python
@app.post("/ai/knowledge/query")
def proxy_kb_query(payload: dict, session: dict = Depends(require_session)):
    return _post("ai", "/query", payload)
```

Since `#16`, **patients have sessions**. So anything written to this collection
becomes reachable — through grounded, cited assistant answers — by every
authenticated user in the system, including every patient, indefinitely.

Paste made that survivable by accident: you cannot paste forty pages without
seeing them. Upload removes the accident. The first employee to drag in a payer
procedure note they have not opened, containing a worked example that names a
real patient, has caused an impermissible disclosure under **45 CFR 164.502(a)**
that our own audit log will record as a successful grounded answer.

`deidentify.scrub_document` does run on ingest. It is not sufficient, and it says
so itself — it is the **lenient** scrub, tuned to preserve effective dates and
the clinic's phone number because a policy stripped of those is useless.
`safe_harbor_scrub` in the same module raises on purpose; the full
164.514(b)(2) treatment is W8 scope. A regex was never the control here.

## Decision

### 1. `POST /ai/knowledge/upload` — multipart, one file, three formats

| Constraint | Value | Enforced at |
|---|---|---|
| Formats | **PDF, TXT, MD** | gateway (extension + sniffed content type) and service |
| File size | **10 MB** | gateway, before the body is buffered |
| PDF pages | **80** | service, after parse, before extraction |
| Extracted characters | **200,000** | service — the existing `IngestRequest` ceiling |

The character cap is deliberately the *same* number as the paste path. An upload
must not be able to smuggle in a document that paste would have rejected.

`pypdf` runs in-process, wrapped so any parser exception becomes a **422 with a
generic message** — never a 500, and never the parser's own error text, which can
echo document content.

**DOCX is deferred**, not silently rejected: the UI names it and tells the user
to save as PDF. DOCX is a zip archive; zip-bomb and path-traversal handling
deserves its own change with its own tests rather than riding along here.
Tracked in `docs/debt-register.md`.

### 2. Two phases. Phase one never writes to the index.

```
POST /ai/knowledge/upload                    → extract, scrub, stage. Returns a preview.
POST /ai/knowledge/upload/{id}/commit        → the ONLY thing that writes to Chroma.
```

The preview returns the exact text that will be indexed, every redaction the
scrubber made marked in place, the redaction kinds and count, and the chunk
count. Nothing reaches the vector store until a human has looked at that and
pressed commit.

This is a **human-in-the-loop gate on the highest-blast-radius write in the
system**. One bad document silently changes every future answer, for every user,
with confident citations pointing at the bad document, and there is no cheap
undo. That is the exact test `adr/0009` set for where a human gate belongs.

### 3. Staging is in Redis, TTL 30 minutes, bound to the uploader

Keyed by a random id, storing the extracted text, the scrub result, and
`staged_by`. Commit compares `staged_by` against the committing session and
returns **403** on mismatch.

403, not 404: the uploader is entitled to know their own staging id is valid, and
a 404 here would only obscure that from the person who legitimately owns it.

The binding exists for two reasons — one employee must not be able to commit
another's staged document, and `added_by` provenance is server-stamped from the
session at both phases. A staging handoff must not become the seam where a
client gets to choose its own provenance.

### 4. The confirm screen states the consequence, not the action

Copy is a decision here, not decoration. The screen does not ask *"are you
sure?"* — a dialog that appears every time is furniture and gets clicked
through. It states what commit means:

> This text will be readable by **every patient** who asks the assistant a
> related question.

and shows the redaction count as **both reassurance and warning**: it evidences
that the scrubber ran, and it tells the uploader that identifiers were found in
a document they are about to publish — precisely when they should read it again.

### 4a. One write path, not two — amended after codex F4

As first written, this ADR claimed a human gate on "the highest-blast-radius
write in the system" while `POST /ai/knowledge/ingest` continued to write
directly after a lenient scrub. A privileged user could bypass the preview
entirely by using the old endpoint, so the claim was **false as written**.

The paste flow therefore stages through the same preview and commit as upload,
and `/ai/knowledge/ingest` is removed from the gateway. A second unpreviewed door
left open "for convenience" is how the gate becomes decorative.

### 4b. Metadata is scrubbed and previewed too — amended after codex F3

Titles, filenames and `source` strings become citations and corpus entries. A
clean body inside `Maria Gonzalez appeal.pdf` is still a disclosure to every
patient who sees a citation. Metadata goes through the same scrub, appears in the
preview, and identifier-shaped metadata **blocks** the commit with a message
naming the field rather than being silently rewritten — silent rewriting hides
from the uploader that they chose a filename they should not reuse.

### 4c. Residual risk we are NOT mitigating — amended after codex F6

Extraction is bounded by a wall-clock timeout, an output-size guard that trips
during extraction, and page/size caps checked before extraction begins.

Those limits bound time and memory. **They do not contain a parser compromise.**
`pypdf` runs in-process in the service that owns the vector store, and a
memory-safety bug in it is reachable from any file an ingest-privileged employee
uploads.

Isolating extraction into a separate worker is the mitigation, and it is
deliberately **out of scope for this phase**: it introduces a queue, a second
deployable and a new failure mode into a phase already carrying a security fix
and three UI PRs, and a half-built isolation boundary is worse than a documented
in-process one. Recorded in `docs/debt-register.md` and stated to the client
rather than buried in a comment.

### 5. Authorization is unchanged and stays at the gateway

`authz.require_ingest` gates **both** phases. `can_ingest` from `/me` hides the
upload control but is never the check; a test posts an upload with a
non-privileged session and expects 403.

## Consequences

**What this buys.** Employees can do the thing they were asked to do. The
highest-risk write in the system acquires a human gate that fires every time, on
a screen that shows the actual consequence rather than a generic confirmation.
Provenance stays server-stamped across a two-request flow.

**What it costs.** Two requests instead of one, and a Redis dependency on the
ingest path. Uploading is slower than it would otherwise be — deliberately. A
staged document that is never committed expires silently after 30 minutes.

**What it does NOT do, and must be said to the client in these words.** This is a
*procedural* control backed by a *lenient automated* one. It is **not
de-identification**. It reduces the chance that PHI enters a patient-readable
collection; it does not prevent it. Prevention requires the Safe Harbor scrub
(W8) and an executed BAA, and until both exist the honest statement is that a
human reading the preview is the control.

**What we are watching.** If the preview screen becomes something employees
scroll past, the control has decayed to furniture and we should measure that
rather than assume it. The redaction-count line is the tell: if it is routinely
non-zero and routinely committed anyway, the procedure is not working.
