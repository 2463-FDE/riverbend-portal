# Finding W1-3 — "HIPAA compliant" is asserted in the README and contradicted by the schema

- **Debt ref:** D3, twist #1 · **Requirement:** `RVB-W1-10`
- **Severity:** High (current-rule gap, not a future one)
- **Status:** Named. Remediation is Week 9 scope.
- **Found during:** Week 1 onboarding, reading the README against `db/schema.sql`

---

## What we saw

The README states that all PHI is encrypted and the system is HIPAA compliant.
`db/schema.sql` says otherwise, in the same repository:

```sql
CREATE TABLE IF NOT EXISTS patients (
    id          SERIAL PRIMARY KEY,
    mrn         TEXT,
    name        TEXT NOT NULL,
    dob         TEXT,                          -- stored as ISO string, not DATE
    ssn         TEXT,                          -- plain text
    ...
    notes       TEXT,                          -- free-text clinical notes, plain text
);
```

`adr/0002` confirms the actual posture: encryption is handled at the storage layer
(volume encryption) plus TLS in transit. `ARCHITECTURE.md` §7 states it plainly —
"Compliance posture is self-asserted."

## Why the common defence does not hold

The expected response is: *encryption at rest is **Addressable** under the
Security Rule, so volume encryption plus TLS is a defensible choice.*

**That is a misreading of "Addressable," and it is the single most common one.**

Under **45 CFR 164.306(d)(3)**, an addressable implementation specification means
a covered entity must:

1. assess whether it is a reasonable and appropriate safeguard in its environment; and
2. **implement it if so**; or
3. **document why it is not reasonable and appropriate, and implement an equivalent alternative measure** where reasonable.

Addressable means *"justify your decision,"* not *"optional."*

Riverbend has done **none of the three**. There is no documented assessment, no
documented rationale for declining field-level encryption, and no equivalent
alternative measure. There is a sentence in a README.

**So this is a gap under the rule that is in effect today** — not one that arrives
later. The 2025 Security Rule NPRM, which would remove the Addressable/Required
distinction and mandate encryption at rest, is *proposed*; the current rule
remains in force. The NPRM makes the gap wider and harder to argue. It does not
create it.

## What volume encryption actually protects against

Worth being precise, because the control is not worthless — it is just aimed at a
different threat:

| Threat | Volume encryption | Field-level encryption |
|---|---|---|
| Someone steals the physical disk | ✅ protects | ✅ protects |
| Snapshot/backup copied to an unsecured bucket | ✅ (if the snapshot inherits it) | ✅ protects |
| SQL injection or a compromised application credential | ❌ **no protection** | ✅ protects |
| A curious operator with `psql` access | ❌ **no protection** | ✅ protects |
| A leaked `DB_PASSWORD` (see Finding W1-2) | ❌ **no protection** | ✅ protects |

Volume encryption defends against someone walking off with the hardware. Every
realistic threat to this system is on the second list — and Finding W1-2 shows
the database credential has been in the repository the whole time.

## Why it matters to the business

The gap is not the missing encryption. **The gap is the claim.**

An auditor who reads "All PHI encrypted — HIPAA compliant" and then reads
`ssn TEXT` does not conclude that a control is missing. They conclude that the
organisation's statements about its controls cannot be relied upon — and then
they widen the scope of everything else they check. A missing control is a
finding. A contradicted claim is a credibility problem, and it costs more.

Under **164.308(a)(1)(ii)(A)** the risk analysis is supposed to be an accurate
assessment. An assessment resting on a false premise is not one.

## Recommended remediation

Not Week 1 work — this is Week 9 scope. Recorded now so the sequencing is a
schedule rather than an oversight.

1. **Immediately, and free:** correct the README. Replace the compliance claim with an accurate statement of the current posture. It costs nothing and it removes the credibility problem today, independent of any engineering.
2. Field-level encryption (or tokenisation) for `ssn`, and `dob` migrated from `TEXT` to `DATE` — which also fixes a match-key defect that Week 2 will show forks patients into duplicate charts.
3. A documented risk assessment covering what is and is not encrypted, and why. This is the artifact 164.306(d)(3) actually asks for.
4. Re-check against the NPRM's direction of travel once it is finalised.

Item 1 is the one we would do this week if you agree. The rest is Week 9.
