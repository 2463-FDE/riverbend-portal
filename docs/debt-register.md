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

## D-11 · Zero data retention is available but not configured — Open

- **Source:** `docs/findings/w1-retention-preflight-called-the-wrong-api.md`
- **Live posture, 2026-07-30:** account mode `inherit` → effective `default`.
  `anthropic.claude-haiku-4-5` allows `['none', 'default', 'provider_data_share']`,
  so ZDR is **available and not requested**.
- **What `default` means:** AWS may retain prompts and completions for abuse
  detection. The **model provider does not receive them**.
- **What is missing:** one account-wide call, deliberately not made by us because
  it affects every Bedrock workload in the account:

  ```bash
  curl -X PUT https://bedrock.$AWS_REGION.amazonaws.com/data-retention \
    -H "Authorization: Bearer $AWS_BEARER_TOKEN_BEDROCK" -d '{"mode":"none"}'
  ```

- **Not to be oversold:** the live smoke tier ran with
  `BEDROCK_REQUIRE_ZERO_RETENTION=false`, against synthetic seed patients, on the
  client's instruction. It proves the model plumbing. **The ZDR control itself is
  unproven end-to-end** and stays that way until the account is set to `none`.
- **Before real PHI:** this and the AWS BAA, together. Neither is an engineering
  problem; both are decisions.

## D-12 · The only code that talks to AWS has no test — Open (partly closed)

- **Source:** the same finding
- **The pattern, now three-for-three:** `check()` takes an injectable probe so
  tests run offline, and every test injected one — so `_default_probe`, the only
  function that touched AWS, had zero coverage by construction. Same shape as the
  corpus path that resolved only outside a container, the gateway route no test
  posted through, and the sensitivity flag nothing in production ever set.
- **What is closed:** the live tier (L0–L6) now exercises the real call path, and
  L0 spends nothing so it can run on any credentialed check-in.
- **What is open:** nothing runs the live tier automatically, because it costs
  money. A nightly or pre-demo credentialed run is the obvious answer and has not
  been set up.

## D-13 · `invented_clinical_claims` is pattern-based — Open

- **Source:** `codex:rescue` F1, `adr/0016` §4
- **What it catches:** invented medications (curated list), invented dosages
  (regex), unsupported clinical directives (frame match).
- **What it misses, demonstrated:** invented conditions ("You are pregnant"),
  invented vitals ("blood pressure was 160/100"), invented labs ("elevated A1C"),
  invented severity ("anaphylactic reaction"), invented causation ("caused by
  strep throat"), and status changes ("your allergy has resolved").
- **Why it is not the gate:** because of exactly that list. It runs *alongside*
  the overlap threshold, not instead of it.

## D-14 · The grounding gate refuses correct answers — Open, and it is the client's bug

- **Source:** `adr/0016` §4, `docs/findings/w2-thresholds-measured-the-stub.md`
- **Symptom:** a patient asking "what am I allergic to?" can be told the
  assistant does not have that information. Measured: faithful answers score
  0.481–0.600 on overlap against a 0.55 threshold, so roughly three in four are
  withheld. The W4 patient view is withheld at a similar rate (0.467–0.522).
- **Why it is still open:** three replacements were built and measured, and all
  three were worse. `invented_clinical_claims` alone releases six fabrications.
  A lower threshold cannot separate the populations — a flat contradiction scores
  0.500, inside the faithful range. Requiring every clinical term to be supported
  blocks the fabrications and refuses every real answer.
- **What it needs:** sentence-level entailment against the source — does each
  clinical assertion follow from a passage? That is model work, not a threshold.
  `clinical_support()` and `unsupported_clinical_terms()` are shipped, measured
  and unwired as groundwork.
- **Meanwhile:** the gate stays strict. Of the two available failure modes —
  releasing fabricated vital signs, or refusing correct answers — only one is
  safe.
