# Finding W1-1 — Patient identifiers are written to application logs in plain text

- **Debt ref:** D1 · **Requirement:** `RVB-W1-08`
- **Severity:** High
- **Status:** Named. Fixed on the AI path only; `intake-service` unchanged.
- **Found during:** Week 1 onboarding, reading the handover log excerpt

---

## What we saw

`services/intake-service/logging_config.py` attaches a file handler that writes to
a repo-level `logs/intake-service.log`, and the registration handler logs the
complete request body at `INFO` on every POST. The handover excerpt shows it:

```
INFO request body={"name":"...","dob":"1971-03-02","ssn":"...","insurance_id":"..."}
```

The service's own module docstring acknowledges it:

> "this service writes the full intake request body to a repo-level file handler
> (logs/intake-service.log) so the front desk has a record of every registration.
> That file therefore contains PHI in plain text — flagged here, not yet
> remediated."

So every patient who has ever registered has their name, date of birth, Social
Security number and insurance member ID sitting in a plain-text file on disk.

## Why it matters

**The log file is a PHI store that nobody classified as one.** That has three
consequences, in increasing order of expense:

1. **Scope.** Anything that touches logs is now in scope for PHI handling: the
   operators who tail them, the backup job that copies them, the disk they sit
   on, and any log-shipping or monitoring vendor they are forwarded to. A vendor
   receiving that stream without a Business Associate Agreement is an
   impermissible disclosure under **164.502(e)** — and it would be a disclosure
   nobody at Riverbend intended or recorded.

2. **Minimum necessary (164.502(b)).** A registration log needs to answer "did
   this registration succeed, and when?" It does not need the SSN. The identifier
   most useful to an attacker is the one least useful for the log's actual
   purpose.

3. **The audit-trail inversion (164.312(b)).** The artifact that *should* be the
   tamper-evident record of who did what has instead become the largest
   concentrated store of the data it was supposed to protect. If this file leaks,
   the breach is not "someone saw the logs" — it is a full identity-theft dataset
   for the entire patient population, and it is more damaging than a database
   compromise because it carries no access control at all.

**In Dr. Okonkwo's terms:** if a laptop with a copy of this repo is stolen, or a
backup bucket is misconfigured, the notification obligation is not theoretical.
The 60-day clock under **164.404** would start on discovery — and the file gives
an attacker everything needed for identity fraud without touching the database.

## What we did about it in Week 1

The AI path is built as the deliberate inverse, and it is the pattern the rest of
the system should adopt:

- **No file handler.** `services/ai-orchestrator/logging_config.py` logs to the console only. A log file is a durable artifact with a retention policy nobody wrote.
- **No bodies, by construction.** One structured audit event per call with a **closed key set** (`services/ai-orchestrator/audit.py`). Adding a field outside that set raises `AuditFieldError`. A comment saying "don't log bodies" survives until the first 6pm debugging session; a `ValueError` survives indefinitely.
- **A redaction filter as a backstop, not as the control.** `RedactingFilter` scrubs identifier-shaped substrings from every record the service emits, so a future careless `log.info("... %s", user_text)` is redacted rather than leaked.

Tests: `tests/test_w1_phi_boundary.py::test_no_phi_in_any_log_record`,
`::test_audit_rejects_non_allowlisted_field`,
`::test_redacting_filter_catches_a_careless_log`.

## What we did NOT do

`intake-service` still logs full request bodies. Changing it means deciding what
the front desk actually needs from that record, which is a conversation with the
front-desk lead rather than a code change we can make unilaterally. It is also
entangled with the observability work in Week 7.

**Recommended remediation, in order of cost:**

1. Stop logging the body; log a registration id, an outcome, and a duration. *(hours)*
2. Add the same `RedactingFilter` to every service logger as a backstop. *(hours)*
3. Decide a retention period for `logs/` and enforce it. Today there is none. *(discussion + hours)*
4. Purge or re-permission the existing log files, and treat their prior exposure as a risk-assessment input rather than assuming it away. *(needs a decision from you)*

Item 4 is the uncomfortable one and we would rather raise it now than have an
auditor raise it later.
