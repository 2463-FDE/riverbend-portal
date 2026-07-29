# Riverbend — Debt Register

Running record of inherited technical and compliance debt, in the client's terms.
Each entry is named when we find it and updated when we act on it. Started Week 1;
this is the document that becomes the Week-10 handoff package.

**Status values:** `open` · `named` (documented, not fixed) · `partial` · `fixed`

---

| ID | Debt | Status | Named | Fixed | Owner-facing risk |
|---|---|---|---|---|---|
| D1 | PHI in plaintext application logs | **partial** | W1 | AI path only | Log file is an unclassified PHI store; vendors receiving it are undeclared BAs |
| D3 | No field-level encryption; compliance claim contradicts schema | **named** | W1 | W9 | A contradicted claim widens audit scope more than a missing control does |
| D9 | Live credentials committed to the repo | **partial** | W1 | Tracking fixed W1; **rotation outstanding** | `DB_PASSWORD` is direct access to every chart, bypassing every app control |
| D4 | Synchronous payer call, no timeout or breaker, on the intake path | open | — | W3 | A payer outage stops patient registration |
| D5 | No MPI / match key → one patient, several charts | open | — | W2 (ADR only) | Clinician sees an incomplete allergy list |
| D8 | N+1 queries + full-table scan on records search | open | — | W4 (measured) | Latency scales badly with chart volume |
| D10 | Sessions never expire; no automatic logoff | open | — | W9 | Shared clinical workstations; walk-away exposure |
| D11 | IDOR on chart reads; sequential ids | open | — | W4 | Any authenticated user can walk the whole patient table |
| D13 | PHI to an LLM vendor with no BAA | **avoided by design** | W1 | W1 (by construction) + `RVB-X-09` | Would have been an unsignalled breach |
| D2, D6, D7, D12, D14 | audit-log mutability, HL7 mapping loss, role bloat, ROI authorization, breach detection | open | — | W6–W10 | Not yet assessed |

---

## Week 1 detail

### D1 — PHI in plaintext logs → `partial`
`intake-service` logs full request bodies (name, DOB, SSN) at INFO to
`logs/intake-service.log`. The new AI path is built as the inverse: console only,
one closed-key-set audit event per call, no bodies, plus a redaction filter as a
backstop. `intake-service` is unchanged and remains the exposure.
**Full finding:** `docs/findings/w1-phi-in-application-logs.md`

### D3 — Self-asserted compliance → `named`
README claims all PHI encrypted; `db/schema.sql` stores `ssn`, `dob` and `notes`
as plaintext `TEXT`. The "encryption at rest is Addressable" defence does not
hold: 164.306(d)(3) requires implementing it *or* documenting why not *and*
implementing an equivalent alternative. Riverbend has done neither, so this is a
**current-rule** gap, not one the 2025 NPRM creates.
**Zero-cost action available now:** correct the README.
**Full finding:** `docs/findings/w1-compliance-is-self-asserted.md`

### D9 — Secrets in the repo → `partial`, **action required**
`.env` was tracked and un-ignored, carrying the DB password, payer API key,
session secret and Bedrock key. This PR untracks it and adds `.gitignore` rules.

> **Untracking is not rotation.** Everything already committed is in the history
> and on every clone ever made. A history rewrite does not reach forks, existing
> clones, or CI caches. **All four credentials must be rotated at source.**

**Full finding:** `docs/findings/w1-secrets-in-repo.md`

### D13 — PHI to an LLM vendor → avoided by design
The contractor's version sent full patient records to a model with no BAA. The
rebuilt path accepts instruction text only — a patient record is *inexpressible*
at the boundary, not merely rejected.

Week 1 also added a control that did not exist in the original plan. Bedrock
exposes a data-retention **mode**, and models requiring `provider_data_share`
share prompts and completions with the model provider and retain them for up to
30 days. Selecting such a model would recreate D13 exactly. The service now
refuses to serve unless the effective mode is `none` and the chosen model permits
zero retention.
**See:** `adr/0004` §1a, requirement `RVB-X-09`.
