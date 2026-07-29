# Spec — document ingestion and the agent-shaped UI workflow

- **Phase:** PRs `#18`–`#21`, sequenced, each cut from `feat/riverbend-w1-w4` after the previous merges
- **Debate:** `docs/design-debate-w1-w4.md` Part III (UI-D9 … UI-D14)
- **ADRs:** `adr/0014` (ingestion pipeline), `adr/0015` (agent-shaped UI)
- **Requirement IDs:** `RVB-ING-*` (ingestion), `RVB-AG-*` (agent workflow), `RVB-W<n>-U<k>` (per-week screens, continuing `docs/specs/ui-w1-w4.md`)
- **Status:** specified

---

## 0. What changed since `ui-w1-w4.md`

Two client instructions arrived after `#17` merged.

**Employees must be able to upload documents.** The existing `/ingest` takes a
JSON `text` field, so the only way to add a document is to open it, select all,
copy, paste. That is not a UI gap — it is a missing backend capability, and §1–§4
below specify it. The design is dominated by one fact established in `adr/0014`:
**the knowledge collection has no scope filter and patients can query it**, so
every ingest is a potential disclosure to every patient in the system.

**The UI should be shaped around how the agents operate.** Five rules, specified
in §5 and derived in `adr/0015`. Their common root: this system has seven
outcomes and five of them return HTTP 200, so anything built as "answer or error"
is wrong five ways — and one of those ways renders a refusal as an empty result,
which reads as *"nothing on file."*

One item is **restored**: `/approvals`, cut from `#20` for lack of a backend list
endpoint. The endpoint is now in scope (§8), along with the durable checkpointer
it requires to be honest.

---

## 1. Ingestion — endpoints

| ID | Requirement |
|---|---|
| `RVB-ING-01` | `POST /ai/knowledge/upload` accepts **multipart/form-data**, exactly one file per request, fields `file` and optional `title`. |
| `RVB-ING-02` | Phase one **never writes to the vector store.** It extracts, scrubs, stages, and returns a preview. |
| `RVB-ING-03` | `POST /ai/knowledge/upload/{staging_id}/commit` is the **only** path that writes an uploaded document to the index. |
| `RVB-ING-04` | Both phases are gated by `authz.require_ingest`. A non-privileged session receives **403** from each. |
| `RVB-ING-05` | `added_by` is stamped server-side from the session at **both** phases. A client-supplied `added_by` is ignored, not honoured. |
| `RVB-ING-06` | The commit path reuses the existing chunk/scrub/index code. No second ingestion implementation. |

### Accepted formats and caps

| ID | Constraint | Value | Enforced |
|---|---|---|---|
| `RVB-ING-07` | Formats | **PDF, TXT, MD** | gateway (extension + content type) **and** service |
| `RVB-ING-08` | File size | **10 MB** | gateway, before buffering the body |
| `RVB-ING-09` | PDF pages | **80** | service, after parse, before extraction |
| `RVB-ING-10` | Extracted characters | **200,000** | service — identical to the paste path's ceiling |
| `RVB-ING-11` | Corpus cap | existing `corpus.enforce_cap` applies at **commit**, against live index count | service |

`RVB-ING-10` is deliberately the same number as `IngestRequest.text`. An upload
must not be able to introduce a document that paste would have rejected.

| ID | Requirement |
|---|---|
| `RVB-ING-12` | A parser failure returns **422** with a generic message. Never 500, and never the parser's own error text, which can echo document content. |
| `RVB-ING-13` | `.docx` is recognised and **named**: the response and the UI say DOCX is not yet supported and to save as PDF. It is not reported as a generic unsupported type. |
| `RVB-ING-14` | A PDF that parses but yields no extractable text (a scan) is rejected with a message saying so — not staged as an empty document. |

## 2. Ingestion — staging

| ID | Requirement |
|---|---|
| `RVB-ING-15` | Staging lives in **Redis**, TTL **30 minutes**, keyed by a random id. |
| `RVB-ING-16` | The staged record carries `staged_by` (the uploading username). Commit by a different session returns **403**. |
| `RVB-ING-17` | 403, not 404, on owner mismatch. The legitimate owner is entitled to know their staging id is valid. |
| `RVB-ING-18` | An expired or unknown staging id returns **404** with a message saying the preview expired and to upload again. |
| `RVB-ING-19` | Commit is **single-use**: the staging key is deleted on success, so a replayed commit returns 404 rather than double-indexing. |

## 3. Ingestion — the preview response

| ID | Field | Requirement |
|---|---|---|
| `RVB-ING-20` | `text` | The exact text that will be indexed, post-scrub. Not the original. |
| `RVB-ING-21` | `redactions` | Kinds and count of what the scrubber removed. |
| `RVB-ING-22` | `chunk_count` | How many chunks commit will produce. |
| `RVB-ING-23` | `pages`, `chars`, `filename`, `title` | Provenance shown back to the uploader. |
| `RVB-ING-24` | `staging_id`, `expires_in_seconds` | So the UI can state the window rather than fail mysteriously. |

## 4. Ingestion — the confirm screen

| ID | Requirement |
|---|---|
| `RVB-ING-25` | The screen states the **consequence**, not the action: this text will be readable by every patient who asks the assistant a related question. |
| `RVB-ING-26` | It is **not** a generic "are you sure?" dialog. A confirmation that appears every time and says nothing specific is furniture. |
| `RVB-ING-27` | Redactions render as **both reassurance and warning** — evidence the scrubber ran, and a signal that identifiers were present in a document about to be published. |
| `RVB-ING-28` | The text preview is scrollable and shows redaction markers **in place**, not as a separate list only. |
| `RVB-ING-29` | Commit and discard are both explicit. Navigating away does not commit; the staged document simply expires. |
| `RVB-ING-30` | The UI states the size/format/page caps **before** the user selects a file. A limit discovered by hitting it is a bug report. |

**Limitation to state to the client, in these words:** this is a *procedural*
control backed by a *lenient automated* one. It is **not de-identification**.
`deidentify.scrub_document` is the lenient policy scrub — it preserves effective
dates and the clinic phone number by design — and `safe_harbor_scrub` still
raises. Prevention requires the full 45 CFR 164.514(b)(2) scrub (W8) and an
executed BAA. Until then, **a human reading the preview is the control.**

---

## 5. The five agent-workflow rules

Each applies to every agent-backed screen and each is testable.

| ID | Rule | Test tier |
|---|---|---|
| `RVB-AG-01` | **A refusal is a first-class answer.** Distinct treatment; never the error component, never an empty result set, never silence. | states |
| `RVB-AG-02` | **Absence is stated, never implied.** "No allergy recorded at this encounter" — a blank space is the opposite fact rendered as the same pixels. | states |
| `RVB-AG-03` | **Provenance is inline.** Citations render with the answer, not behind a disclosure or a "sources" toggle. | states |
| `RVB-AG-04` | **Show the path, not a spinner.** The graph's `path` field is the progress indicator. | states + journey |
| `RVB-AG-05` | **A human gate renders as a decision**, with stakes named and both outcomes explicit. A paused graph shown as a spinner is a hang. | states + journey |
| `RVB-AG-06` | **Corpus quality is a staff screen.** The person who can fix fragmentation will never run `pytest`. | journey |

| ID | Requirement |
|---|---|
| `RVB-AG-07` | Every one of the seven outcomes in `adr/0015` has a distinct rendering and at least one component test naming it. |
| `RVB-AG-08` | The UI must not label the `path` display as model reasoning. It is the node sequence the graph executed. |

---

## 6. `#18` — W2 (2/2): knowledge search, ingestion, quality

| ID | Screen | Requirement |
|---|---|---|
| `RVB-W2-U1` | `/knowledge` | Question in, cited answer out. Refusal renders per `RVB-AG-01`, citations inline per `RVB-AG-03`. |
| `RVB-W2-U2` | `/knowledge` | Upload control, visible only when `can_ingest` (§7), implementing `RVB-ING-25`–`RVB-ING-30`. |
| `RVB-W2-U3` | `/knowledge/quality` | Retrieval metrics **beside** integrity metrics, labelled so a data problem cannot be read as a model problem. |
| `RVB-W2-U4` | `/knowledge/quality` | Fragmentation renders as **named patients and chart ids**, not only a rate. `clinically_incomplete_answers` gets a severity treatment. |
| `RVB-W2-U5` | journey | A browser: ask a chart-shaped question, get cited results; the quality screen shows recall `1.0` beside coverage `0.556`. Replaces `RVB-W2-14`. |
| `RVB-W2-U6` | journey | A browser: upload a PDF, read the preview, commit, then ask a question the new document answers. |

## 7. `can_ingest` gating

| ID | Requirement |
|---|---|
| `RVB-AG-09` | `can_ingest` hides the **write control**, not the page. Only `/ai/knowledge/query` is open to any authenticated session. |
| `RVB-AG-09a` | **Corrected after codex F2.** The original text said "query and eval are open to any authenticated session", which — combined with `RVB-W2-U4`'s named patients and chart ids — specified a cross-patient leak, and one that already existed. `/ai/knowledge/corpus`, `/eval`, `/eval/latest` and `/identity-clusters` are **staff-only**, enforced on the resolved principal. `/knowledge/quality` is therefore a staff screen. |
| `RVB-AG-10` | The absent control is replaced by a line naming who to ask for access — not by nothing. A vanished feature teaches people it does not exist. |
| `RVB-AG-11` | The UI hint is **never** the check. A test posts an upload with a non-privileged session and expects 403. |

## 8. `#19` — W3 (2/2) and `#20` — W4 (2/2)

| ID | Screen | Requirement |
|---|---|---|
| `RVB-W3-U1` | `CoverageChip` | Renders `active` / `inactive` / `pending` / `unknown`; stale shows "as of HH:MM" and is impossible to miss. |
| `RVB-W3-U2` | `/eligibility` | Visit-scoped assistant. Shows when the agent's reply was **overridden** because it contradicted the tool. |
| `RVB-W3-U3` | `/intake` | Coverage `pending` → resolved, the visible half of the W3 decoupling. |
| `RVB-W3-U4` | journey | Payer stopped: chip reads stale with a timestamp, registration still completes. Replaces `RVB-W3-14`. |
| `RVB-W4-U1` | shell | `AppShell` nav filtered by principal; patients do not see Intake or ROI. |
| `RVB-W4-U2` | patient landing | Assembled record across four domains, with the "spans three charts" note and per-domain unavailable states. |
| `RVB-W4-U3` | `/approvals` | The HITL queue. Each row states patient, chart span, and why the gate fired — never a bare run id. |
| `RVB-W4-U4` | `/approvals` | Approve/deny resumes the paused run and the outcome is visible. |
| `RVB-W4-U5` | stale artifacts | The two IDOR comments in `records/page.tsx` and `api/records/route.ts` are corrected; the backend does check now. |
| `RVB-W4-U6` | journey | Log in as `maria.gonzalez`: own record shows all three charts **including the penicillin allergy**; chart 1043 shows not-found. Replaces `RVB-W4-16`. |

### Backend additions required by `#20`

| ID | Requirement |
|---|---|
| `RVB-AG-12` | `GET /ai/approvals` lists runs paused at the sensitivity gate, with patient id, chart span, and gate reason. |
| `RVB-AG-13` | The checkpointer moves to the **durable, encrypted** path (`SqliteSaver` + `EncryptedSerializer`) as a **precondition** of `/approvals`. A queue over `InMemorySaver` empties on restart — that is a session, not a control. |
| `RVB-AG-14` | A paused run holds assembled PHI at rest; encryption at rest is required by 45 CFR 164.312(a)(2)(iv). |

## 9. `#21` — demo and deck

| ID | Requirement |
|---|---|
| `RVB-AG-15` | `DEMO.md` Path B is rewritten as a **browser walkthrough**. `scripts/demo.py` is demoted to a CI smoke aid and labelled as one. |
| `RVB-AG-16` | The deck gains screenshots from the running stack, including the ingestion preview and the approvals queue. |
| `RVB-AG-17` | Every PR body in this phase answers the standing question with **pasted test output**, not intent. |

---

## 10. Test strategy

Unchanged from `adr/0013` — states in the default gate, journeys opt-in — with
two additions:

| ID | Requirement |
|---|---|
| `RVB-AG-18` | The ingestion path gets **API contract tests** named `test_api_*`, covering: 403 unprivileged, oversize, bad format, scanned PDF, page cap, char cap, owner mismatch on commit, expired staging, and replayed commit. |
| `RVB-AG-19` | New fixture PDFs are generated at test time, not committed as binaries — a committed binary in a PHI repo is a review burden with no upside. |
| `RVB-AG-20` | The upload journey uses a generated PDF and asserts the **preview** contents, not only that commit returned 200. |


---

## 11. Requirements added by `codex:rescue` (Part III)

Verdicts and rationale: `docs/specs/codex-rescue-part3-adjustments.md`.

### Metadata is PHI too (F3)

| ID | Requirement |
|---|---|
| `RVB-ING-31` | `title`, `filename` and `source` are scrubbed on the same path as the body. A clean body inside `Maria Gonzalez appeal.pdf` is still a disclosure. |
| `RVB-ING-32` | Metadata appears **in the preview**, showing what will be stored, not what was uploaded. |
| `RVB-ING-33` | Identifier-shaped metadata **blocks the commit** with a message naming the field. It is not silently rewritten — silent rewriting hides that the uploader chose a filename they should not reuse. |

### One write path (F4)

| ID | Requirement |
|---|---|
| `RVB-ING-34` | Paste and upload both stage → preview → commit. `POST /ai/knowledge/ingest` is **removed from the gateway**; a second unpreviewed door would make the HITL claim in `adr/0014` false, which is how it was written the first time. |

### Resource limits (F5, F6)

| ID | Requirement |
|---|---|
| `RVB-ING-35` | The 10 MB cap is enforced by a **streaming** read that aborts once exceeded — not checked after the body is buffered. The ASGI/proxy limit is set explicitly, not assumed. |
| `RVB-ING-36` | Extraction carries a **wall-clock timeout** and an output-size guard that trips during extraction, so a decompression blowup cannot run to completion. |
| `RVB-ING-37` | Page and size caps are checked **before** extraction begins, not after. |

**Residual risk, stated and not mitigated:** extraction runs in-process in the
service that owns the vector store. The limits above bound time and memory; they
do **not** contain a parser compromise. Isolation into a separate worker is the
mitigation and is deliberately out of scope for this phase — see the F6 verdict
and `docs/debt-register.md`. Rate limiting is a gateway-wide gap, not an upload
feature.

### Commit atomicity (F7)

| ID | Requirement |
|---|---|
| `RVB-ING-38` | Staging is claimed with an atomic `GETDEL`, so exactly one commit can win an id. Chunk ids are deterministic from the staging id, so a retry after a partial add overwrites rather than duplicates. |

### Staged PHI at rest (F11)

| ID | Requirement |
|---|---|
| `RVB-ING-39` | Staged payloads are encrypted at rest, using the same key path as the checkpointer. The staged document is explicitly **not** de-identified — that is the premise of the preview. |
| `RVB-ING-40` | Stage, preview, commit and discard are each audited. |
| `RVB-ING-41` | A per-user cap on concurrent staged bytes, so abandoned uploads cannot accumulate. |

### Approvals authorization (F8, F9)

| ID | Requirement |
|---|---|
| `RVB-AG-21` | An explicit `can_approve` capability, surfaced on `/me` beside `can_ingest`, enforced at the gateway. |
| `RVB-AG-22` | Approving is **staff-only**. |
| `RVB-AG-23` | A patient may **never** approve a gate on their own record, even holding the capability. A subject approving their own sensitivity gate is not human-in-the-loop; it is a rubber stamp with extra steps. |
| `RVB-AG-24` | Resume takes an **opaque server-issued approval id** bound to patient, requester, gate state and permitted approver. `thread_id` leaves the client contract — it is currently client-supplied and forwarded verbatim, the same shape as F1. |

### The outcome contract (F12)

| ID | Requirement |
|---|---|
| `RVB-AG-25` | One `AgentOutcome` discriminator, derived once in `frontend/app/lib/outcome.ts`, with a fixture per outcome per endpoint. Same treatment as `resolvePrincipal` mirroring `scope.py`: one place, tested, so a backend shape change fails a test instead of rendering the wrong state. |

### Route coverage, not behaviour coverage

| ID | Requirement |
|---|---|
| `RVB-AG-26` | An enumeration test over **every** `/ai/*` gateway route asserting each either derives scope server-side or provably cannot reach the record collection. F1 was an unlisted route guarded by a comment; only enumeration catches that class. |
