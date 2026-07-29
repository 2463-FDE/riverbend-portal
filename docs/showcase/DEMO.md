# Riverbend — Demo Walkthrough

Two ways to run this. Pick by audience.

| | Audience | Time | Needs |
|---|---|---|---|
| **A. Scripted demo** | Board, COO, anyone non-technical | ~4 min | Python only. No Docker, no AWS key, no spend. |
| **B. Live stack** | Clinical leads, IT, anyone who wants to click | ~15 min | Docker. Still no AWS key. |

Both show the same four things. **A is the one to run in a board meeting** — it
is deterministic, it cannot fail on stage, and every number in it is computed
live from Riverbend's own handover data rather than typed into a slide.

---

## A. The scripted demo

```bash
cd repos/riverbend-w1-w4
python3 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt
.venv/bin/python scripts/demo.py
```

Four acts, ~30 seconds of runtime. Run one at a time with `demo.py 2`.

### Act 1 — The AI feature, and what it refuses to do *(~60s)*

Shows the same intake summary twice: once faithful (accepted), once with
`"continue taking your metformin 500 mg"` added (withheld).

**The line to say out loud:** *"The grounding score barely moved — 0.92 to 0.60.
A summary that's 95% right and invents one medication is more dangerous than an
obviously wrong one, because it reads as competent. So we don't rely on the
score. We look for clinical claims that aren't in the source, and any hit
withholds the answer."*

Then it prints the **data-retention preflight**. Some models require your prompts
be shared with the model vendor and kept 30 days; for patient data that's a
reportable disclosure. *"This isn't a policy document. It's a switch that won't
turn on."*

### Act 2 — One patient, three charts *(~90s — this is the one they'll remember)*

Prints Maria Gonzalez's three charts side by side. Same SSN, same address, same
phone. The penicillin allergy is on the middle one.

**Let the table sit on screen for a beat before talking.**

Then two scorecards. The retrieval one: recall 1.0, precision 1.0, groundedness
1.0. The one we added: fragment coverage **0.556**.

**The line:** *"Both of those are true at the same time. The search is finding
everything we indexed. What we indexed is a third of the patient."*

Then: *"Your previous vendor's own test file expects the answer 'No known
allergies on file.' The test was written to expect the bug. A system scoring 100%
against it tells a clinician this patient has no allergies."*

Finish with the delta — coverage 0.556 → 1.0, allergy recovered — and:
*"Nothing about the search changed. The only difference is that the system knows
who the patient is."*

> If someone asks "is this just the demo data?" — **no.** Dr. Nguyen already filed
> this as RIV-160: *"Why does the allergy list look different depending on which
> chart I open for the same lady?"* It was closed as a display bug.

### Act 3 — Nineteen minutes when nobody could register a patient *(~60s)*

Replays Tuesday 09:02–09:21 against the new system. 76 registrations attempted
during the outage, 76 succeeded, and the failing clearinghouse was called 40
times instead of 76 — none of it on the registration path.

**The line:** *"You have two tickets open for this. RIV-088 says registration is
slow. RIV-141 says the intake screen froze. They're the same defect, and closing
the first one cosmetically would have left the second one waiting."*

Show the receptionist's message: *"Active — as of 9:02AM on 3 Mar (payer
unreachable, showing last known)."* **Point at the timestamp.** *"We never show a
six-hour-old coverage status as if it were current. That's how a patient gets
billed for an uncovered visit."*

### Act 4 — Any login could read any chart *(~60s)*

Maria opens her own record and gets all three charts assembled — **including the
penicillin allergy from Act 2**. Then the same session tries the walk from the
browser capture.

**The two numbers to point at:**

```
response       404   (identical to a chart that doesn't exist)
records loaded 0     ← not filtered out afterwards. Never read.
steps taken    authorize → deny
```

**The line:** *"The check runs first, before anything is read. This isn't 'load
the data then hide it' — the records were never fetched. And the AI runs last, on
material already approved. It is never asked who should see what."*

---

## B. The live stack

```bash
make up                    # postgres, redis, chroma, all services, portal
make seed                  # schema + demo data
open http://localhost:3070
```

Everything runs in stub mode. **No AWS key, no spend.**

### B1 — Log in as a patient

| user | password | is |
|---|---|---|
| `maria.gonzalez` | `portal123` | patient, bound to chart 1042 |
| `frontdesk` | `portal123` | staff |

```bash
TOKEN=$(curl -s localhost:8070/login -H 'Content-Type: application/json' \
  -d '{"username":"maria.gonzalez","password":"portal123"}' | jq -r .token)

curl -s localhost:8070/me -H "Authorization: Bearer $TOKEN" | jq
```

```json
{ "username": "maria.gonzalez", "role": "patient", "patient_id": "1042",
  "scope": { "principal": "patient", "patient_ids": [1042, 1330, 1588] } }
```

*"She logged in as one chart. The system resolved her to three, because they're
the same person."*

### B2 — The IDOR, then and now

```bash
curl -s -o /dev/null -w "own chart 1042 → %{http_code}\n" \
  localhost:8070/patients/1042/records -H "Authorization: Bearer $TOKEN"

curl -s -o /dev/null -w "someone else 1043 → %{http_code}\n" \
  localhost:8070/patients/1043/records -H "Authorization: Bearer $TOKEN"

curl -s -o /dev/null -w "nonexistent 999999 → %{http_code}\n" \
  localhost:8070/patients/999999/records -H "Authorization: Bearer $TOKEN"
```

```
own chart 1042 → 200
someone else 1043 → 404
nonexistent 999999 → 404
```

**Point at the last two being identical.** *"If unauthorized returned 403 and
missing returned 404, you could probe for which patient IDs exist. Both say the
same thing."*

The original capture is in `docs/handover/portal.har` — both requests returning
200. Worth opening side by side.

### B3 — The assembled patient view

```bash
curl -s localhost:8070/ai/patient-view/1042 -H "Authorization: Bearer $TOKEN" | jq '.summary, .path'
```

Point at `path`: `authorize → plan → retrieve:demographics → retrieve:encounters
→ retrieve:labs → retrieve:coverage → sensitivity_gate → synthesize`.

*"Authorize is first. The four retrievals run in parallel. The model call is
last, and only ever sees what authorize approved."*

### B4 — The knowledge base and the duplicate finding

```bash
STAFF=$(curl -s localhost:8070/login -H 'Content-Type: application/json' \
  -d '{"username":"frontdesk","password":"portal123"}' | jq -r .token)

curl -s -XPOST localhost:8070/ai/knowledge/eval \
  -H "Authorization: Bearer $STAFF" -d '{}' | jq -r .report
```

Prints the full eval report — retrieval quality first, data integrity second, the
identity split, and the clinically-incomplete answer.

```bash
curl -s localhost:8070/ai/knowledge/identity-clusters \
  -H "Authorization: Bearer $STAFF" | jq '.clusters[] | select(.fragmented)'
```

### B5 — Break the payer on purpose

```bash
docker compose stop eligibility-service
curl -s -w "\nregistration took %{time_total}s\n" -XPOST localhost:8070/intake \
  -H "Authorization: Bearer $STAFF" -H 'Content-Type: application/json' \
  -d '{"demographics":{"name":"Demo Patient","dob":"1980-01-01","created_via":"front_desk"},
       "insurance":{"payer_name":"ACME","member_id":"BCBS4471"},"consents":["npp_ack"]}'
docker compose start eligibility-service
```

Registration returns **201** with `"status": "pending"`, in well under a second,
with the eligibility service completely stopped.

*"Before this change, that request would have hung. On the Tuesday in question,
for nineteen minutes, it hung for everyone."*

---

## Questions you should expect

**"Can we turn this on for real patients tomorrow?"**
No, and the reason is worth hearing. Three things first: rotate the credentials
that are in the repository history, sign the AWS Business Associate Agreement,
and flush existing login sessions when this deploys. All three are on the list;
none is an engineering problem.

**"How much does the AI cost?"**
Every request has a spending ceiling checked *before* the call is made, and
refuses if it would exceed it. The whole demo you just watched cost zero — it
runs without an AI key at all.

**"You said the search scored 100%. Why is that bad?"**
Because it scored 100% against a test written by the people who built the thing
being tested, over data that was already split. The score was measuring the wrong
question. We added a second scorecard measuring *how much of the patient we
actually saw*, and that one reads 0.556.

**"Has anyone actually read charts they shouldn't have?"**
We can't tell you, and that's a finding in itself. The audit table is an ordinary
table — rows can be edited and deleted — so it can't answer "who viewed this
patient?" That's Week 10's work, and it compounds this one: an exploited version
of what we found would have left nothing to find.

**"What's the biggest risk you didn't fix?"**
Every member of staff still sees every chart. We narrowed *which patient* a login
can reach, not *which staff role*. Billing, front desk, and clinicians share one
permission set. That's scheduled for Week 9, and we'd rather say so than let this
week's fix sound bigger than it is.

---

## Cheat sheet

| Number | Where it comes from | Say it as |
|---|---|---|
| **3 charts, 1 patient** | `db/seed/patients.csv` | "Same SSN, same address, same phone" |
| **recall 1.0 / coverage 0.556** | `POST /ai/knowledge/eval` | "Both true at once" |
| **76 / 76 registrations** | Act 3 replay | "During a nineteen-minute outage" |
| **404 vs 404** | `GET /patients/1043` vs `/999999` | "Nobody can probe for real IDs" |
| **0 records loaded** | Act 4 | "Never read, not hidden afterwards" |
| **231 tests, $0** | `make test` | "Green without an AI key" |
