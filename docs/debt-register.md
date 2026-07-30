# Debt register

Things we chose not to build, with the reason and the consequence. Referenced by
ADR and by finding, so a claim in either can be checked against what is actually
here.

**The rule this file exists to enforce:** if a control is not built, it is named
here rather than implied by silence. An unstated gap reads as a covered one.

| Status | Meaning |
|---|---|
| **Open** | Not built. The consequence stands. |
| **Scheduled** | Named to a week in `docs/implementation.md`. |
| **Accepted** | Deliberately permanent for this engagement. |

---

## D-01 · Extraction runs in-process — Open

- **Source:** codex F6, `adr/0014` §4c
- **What is missing:** `pypdf` parses uploaded files inside the service that owns
  the vector store.
- **What IS built:** wall-clock timeout, page cap checked before reading, output
  cap checked during extraction, size cap enforced by a streaming read.
- **Residual risk:** those bound *time and memory*. They do **not** contain a
  parser compromise. A memory-safety bug in `pypdf` is reachable from any file an
  ingest-privileged employee uploads.
- **Why deferred:** isolation means a queue, a second deployable, and a new
  failure mode, added to a phase already carrying a security fix and three UI
  PRs. A half-built isolation boundary is worse than a documented in-process one.
- **Mitigation when scheduled:** extraction moves to a separate worker with CPU,
  memory and wall-clock limits.

## D-02 · No rate limiting anywhere on the gateway — Open

- **Source:** codex F5 (partially rejected)
- **What is missing:** per-user or per-IP request limits on **all 30+ gateway
  routes**, not only upload.
- **Why it is here rather than in the upload work:** adding it to one endpoint
  would imply a protection the other twenty-nine do not have. It is a
  gateway-wide concern and belongs in one change that covers login too — where
  the absence matters more, since sessions never expire (D10).

## D-03 · DOCX upload — Scheduled

- **Source:** UI-D9, `adr/0014` §1
- **What is missing:** `.docx` / `.doc` / `.rtf` / `.odt` ingestion.
- **Current behaviour:** recognised **by name** and refused with "save the
  document as a PDF", not reported as a generic unsupported type.
- **Why deferred:** DOCX is a zip archive. Zip-bomb and path-traversal handling
  deserves its own change with its own tests rather than riding along.

## D-04 · Checkpoint encryption is not storage encryption — Open

- **Source:** codex F10, `adr/0015` amendment
- **What IS built:** `EncryptedSerializer` encrypts the serialized checkpoint
  payload.
- **What is missing:** SQLite WAL and temp files, filesystem permissions, backup
  handling, key custody and rotation.
- **Why it matters:** the first draft of `adr/0015` read as though the library
  choice satisfied 45 CFR 164.312(a)(2)(iv). It does not. The honest claim is
  *the payload is encrypted; the surrounding storage controls are not in place.*

## D-05 · Staged documents are PHI with a 30-minute life — Open (partly mitigated)

- **Source:** codex F11, `RVB-ING-39`–`RVB-ING-41`
- **What IS built:** AES-GCM at rest under `LANGGRAPH_AES_KEY`, per-user cap on
  concurrent staged documents, audited transitions, 30-minute TTL.
- **What is missing:** the key is shared with the checkpointer and has no
  rotation story; Redis persistence/snapshot policy is not governed.

## D-06 · The ingest preview is a procedural control — Accepted

- **Source:** `adr/0014`, `RVB-ING-25`
- **The limitation, in the words to use with the client:** this is a *procedural*
  control backed by a *lenient automated* one. It is **not de-identification**.
  `deidentify.scrub_document` preserves effective dates and the clinic phone
  number by design, and `safe_harbor_scrub` still raises.
- **What would change it:** the full 45 CFR 164.514(b)(2) scrub (W8) and an
  executed BAA. Until then, **a human reading the preview is the control.**
- **How we would know it has decayed:** if the redaction count is routinely
  non-zero and documents are committed anyway, the procedure is not working.
  That is measurable and currently unmeasured.

## D-07 · Nothing in CI starts the stack — CLOSED

- **Source:** `docs/findings/w1ui-nothing-ever-ran-the-stack.md`
- **Closed by:** the `stack-smoke` job in `.github/workflows/ci.yml`.
- **What it does:** `docker compose up --wait` from a clean volume (which fails
  the job if any container never reaches healthy — exactly what an unloadable
  schema looks like), `/healthz` on all eight services plus the portal, `make
  seed` followed by a real login, `pytest -m integration`, and the Playwright
  journeys.
- **Why it matters:** three defects — an unloadable schema, a container-broken
  corpus path, and a live scope narrowing that hid a patient's penicillin
  allergy — survived 248 green tests because nothing ever ran the stack.
- **Found while building it:** chroma's healthcheck shelled out to `curl`, which
  is not in the image, so chroma sat permanently `unhealthy` while serving
  requests fine. Harmless only because `ai-orchestrator` waits on
  `service_started`; it would have deadlocked `--wait` and the first dependant to
  ask for `service_healthy`. Now a `/dev/tcp` probe via the bash that IS in the
  image — weaker than an HTTP 200, and labelled as such.

## D-08 · Route coverage for the `/ai/*` surface — Open

- **Source:** codex F1, `RVB-AG-26`
- **What is missing:** a test that enumerates **every** `/ai/*` gateway route and
  asserts each either derives scope server-side or provably cannot reach the
  record collection.
- **Why enumeration specifically:** the IDOR was an *unlisted route* guarded by a
  comment claiming an invariant nothing enforced. Behaviour coverage cannot find
  a route nobody thought about; only enumeration can.

## D-09 · The ingest capability is an allowlist, not a role — Scheduled

- **Source:** `services/gateway/authz.py`, D7
- **What is missing:** real role hierarchy (W9). `KNOWLEDGE_INGEST_USERS` /
  `KNOWLEDGE_INGEST_ROLES` are env allowlists because the inherited system has
  exactly one role and cannot express a capability.
- **Not to be oversold:** this is not least-privilege. It is one narrow capability
  bolted beside a role model that cannot express capabilities.

## D-10 · The Playwright gate is no longer opt-in — resolved with a caveat

- **Source:** `adr/0013`, revisited
- **What changed:** the journeys were gated behind `make up` because CI had no
  stack. That was right then and stops being right now that `stack-smoke` exists —
  an opt-in journey suite is a suite that does not run, and `#17` shipped claiming
  a journey passed when it had never once executed.
- **Caveat:** they still are not in the fast `frontend` job, so a pure-UI PR gets
  its component feedback in ~1 minute and its journey feedback with the stack job.
  That is a deliberate latency trade, not an exemption.
