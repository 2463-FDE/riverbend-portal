# Finding W1-2 — Live credentials are committed to the repository

- **Debt ref:** D9 · **Requirement:** `RVB-W1-09`, `RVB-W1-12`
- **Severity:** Critical
- **Status:** Tracking fixed in this PR. **Rotation still outstanding — action required from Riverbend.**
- **Found during:** Week 1 onboarding, first `git ls-files`

---

## What we saw

`.env` was **tracked by git** and absent from `.gitignore`. It contains, per
`.env.example`, the shape of what was in it:

| Credential | Grants |
|---|---|
| `DB_PASSWORD` | Read/write on the patient database — every chart, every SSN |
| `PAYER_API_KEY` | Riverbend's identity at the clearinghouse |
| `SESSION_SECRET` | The ability to forge portal sessions |
| Bedrock key | Spend on the AWS account |

Anyone who has ever cloned this repository has all of them, on their laptop,
right now.

## Why it matters

**"Private repo" is not a control.** It is a hope about who has access, and it
does not survive: a contractor offboarding, a laptop being stolen, a fork made
for convenience, a CI integration being granted read scope, or the repository
being made public by a single mis-click. The credential does not care which of
those happens.

The specific escalation here is that `DB_PASSWORD` is not "a database password" —
it is **direct read access to every patient record**, bypassing the portal, the
gateway, and every application-level control we might add later. It is a strictly
larger exposure than the IDOR we will find in Week 4, because it needs no session
at all.

Under **164.308(a)(1)** (security management process) and **164.308(a)(4)**
(information access management), credentials shared this widely mean access
cannot be attributed. If we ask "who could have read the patients table last
March?", the honest answer today is "anyone who ever cloned the repo," and no
audit can narrow it.

This also compounds Finding W1-1: the credential in the repo unlocks the
database, and the log file contains the same data without needing the credential.
Two independent paths to the same PHI, neither of them access-controlled.

## What we did about it

In this PR (`RVB-W1-12`):

- `git rm --cached .env`
- `.env`, `.env.*` added to `.gitignore`, with `!.env.example` retained as the key-name reference
- `.env.example` documents every key with safe placeholder values

This is a deliberate, narrow deviation from "Week 1 is discovery only." The
justification is specific: a live Bedrock credential is about to be placed in
that file, and leaving it tracked would mean knowingly handing someone a loaded
gun while writing a memo about gun safety.

## ⚠️ What we did NOT do, and what you must do

> **Untracking is not rotation.**
>
> Every credential that was ever committed is still in the git history and on
> every clone that has ever been made. `git rm --cached` removes it from the
> *next* commit. It removes it from nothing else.
>
> A history rewrite (`filter-repo`, BFG) is **also insufficient on its own**: it
> does not reach forks, existing clones, CI caches, or anyone's local reflog.

**The only remediation that works is rotation at the source:**

| Credential | Rotate where | Blast radius until rotated |
|---|---|---|
| `DB_PASSWORD` | Postgres — change the `riverbend_app` role's password | Full patient database |
| `PAYER_API_KEY` | The clearinghouse's portal | Riverbend's payer identity, eligibility spend |
| `SESSION_SECRET` | Regenerate; this invalidates all sessions | Session forgery |
| Bedrock key | AWS console → Bedrock → API keys | AWS spend |

**Recommended, in order:**

1. **Rotate all four now.** Nothing below matters until this is done.
2. Move to short-term credentials where the platform supports it. AWS documents long-term Bedrock API keys as **"recommended only for exploration"** and short-term keys (≤12 hours, inheriting the generating principal's permissions) as **"recommended for production use."** ([AWS docs](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html))
3. Add secret scanning to CI. The pipeline currently has none, so the next `.env` commit would also go unnoticed.
4. Treat the prior exposure as a **risk-assessment input under 164.308(a)(1)(ii)(A)**, not as something to assume away. The honest position is that we cannot prove these credentials were not used, and the assessment should say so.

Step 4 is the one that is tempting to skip. We would rather it be written down
now, by us, than discovered by an auditor and characterised by them.
