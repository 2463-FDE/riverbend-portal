# Finding W4-2 — Opening a chart runs one query per encounter

- **Debt ref:** D8 · **Requirement:** `RVB-W4-08`
- **Severity:** Medium (scalability; becomes patient-experience at volume)
- **Status:** **Measured and named. Not fixed** — outside W4's scope.
- **Found during:** building the patient-view assembler

---

## What we saw

`services/records-service/app.py::get_patient_records` fetches a patient's
encounters, then loops and runs **one additional query per encounter** to load
that encounter's records. No JOIN, no `selectinload`. The code says so:

```python
# N+1: one extra query per encounter (deliberate — do not collapse to a join)
for enc in encounters:
    recs = db.execute(select(Record).where(Record.encounter_id == enc.id)...)
```

And `GET /records/search` is a full-table `ILIKE` on `records.body` with **no
supporting index and no result limit** — every search scans every row and
materializes every match.

## The measurement

From `scripts/measure_n_plus_one.py`, run 2026-07-29 against the handover sample
(`db/seed/encounters.csv`, 5 patients / 5 encounters — the full seeded database
carries ~250 patients and ~475 encounters):

```
queries to assemble ONE chart
  patient 1042: 2  (1 encounters + 1 list query)

projection at realistic volume
   10 encounters ->  11 queries per chart open
   25 encounters ->  26 queries per chart open
   50 encounters ->  51 queries per chart open
  100 encounters -> 101 queries per chart open
```

The fixture patients have one encounter each, so the sample looks harmless. **The
shape is the finding, not the current number.** A patient with a chronic
condition accumulates encounters steadily, and the cost of opening their chart
grows linearly with their history — so the system is slowest for exactly the
patients clinicians look at most.

## Why it matters

**It degrades where it hurts.** A new patient's chart opens instantly. A
twenty-year patient with a complex history is the slow one. That is backwards
from every clinical priority.

**`records/search` has no ceiling.** No index, no `LIMIT`. A broad search term
scans the whole records table and materializes every match into memory. On the
seeded corpus that is fine; on a real one it is a self-inflicted outage waiting
for someone to search for "pain".

**It compounds with the W3 finding.** Both are the same class of defect — a cost
that is invisible at demo scale and unbounded at production scale — and both were
introduced by building fast to win the contract.

## What we did about it

Nothing, deliberately. Fixing it is a `records-service` change:

1. `selectinload` (or a JOIN) to collapse the per-encounter loop into one query.
2. An index on `records.body` — a trigram or full-text index, since `ILIKE
   '%term%'` cannot use a btree.
3. A `LIMIT` and pagination on search.

That is roughly a day of work, and it is a different service from the one Week 4
touches. Bundling it here would have made the authorization change harder to
review, and the authorization change is the one that matters this week.

**The patient-view assembler does not make this worse.** It traverses an
in-process graph built once per request from the authorized scope, rather than
re-querying per encounter.

## Recommendation

Schedule it as a standalone increment. It is low-risk, well-understood, and it
does not need to wait for anything else on the roadmap.
