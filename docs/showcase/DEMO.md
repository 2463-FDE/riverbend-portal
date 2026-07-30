# Riverbend — Demo Walkthrough

Two ways to run this. **The browser is the demo now.**

| | Audience | Time | Needs |
|---|---|---|---|
| **A. The portal** | Anyone — this is the default | ~15 min | Docker. No AWS key, no spend. |
| **B. The scripted fallback** | A room with no Docker, or a hostile network | ~4 min | Python only. |

Path A used to be a list of `curl` commands, which is a fair description of what
existed: four weeks shipped as gateway endpoints with **zero lines of
`frontend/`**, and a terminal script let that go unnamed for a while. It is a
browser walkthrough now because there is a browser to walk through.

Keep B in your pocket. It is deterministic, it cannot fail on stage, and every
number in it is computed live from Riverbend's own handover data rather than typed
into a slide. But lead with A — *"can a person do this?"* is the question, and only
A answers it.

Everything runs in **stub mode**: no AWS key, no spend, on either path.

---

## A. The portal

```bash
make up            # postgres, redis, chroma, every service, the portal
make seed          # schema + demo data
open http://localhost:3070
```

### The three roles, and why there are three

| Login | Password | Principal | Sees | Can |
|---|---|---|---|---|
| `maria.gonzalez` | `portal123` | **patient** · chart 1042 | charts 1042, 1330, 1588 — her own, merged | nothing privileged |
| `frontdesk` | `portal123` | **staff** | any chart in context | add documents · request a release |
| `rdelgado` | `portal123` | **staff** | any chart in context | add documents · **approve someone else's** release |

The third login is not padding. Two of the controls only exist between *two
different people*:

- a **patient** cannot approve the release of their own record — that is the
  subject rubber-stamping their own disclosure;
- a **staff member** cannot approve the release *they themselves requested* — a
  queue you can empty yourself has no second pair of eyes in it.

Demonstrating either one requires `frontdesk` **and** `rdelgado`. With a single
staff login the approvals beat looks like a form, not a control.

### Set up before you present

Open **two browser profiles** side by side — sessions live in `localStorage`, so
two tabs in one profile will fight over the token and you will spend the demo
logging back in.

| Window | Signed in as |
|---|---|
| Left | `maria.gonzalez` — the patient |
| Right | `frontdesk`, then `rdelgado` for the approval |

### Which role drives which beat

| Beat | Driven by | The point |
|---|---|---|
| **A1** | `maria.gonzalez` | Her record spans 3 charts; the allergy is on one her login is *not* attached to |
| **A2** | `frontdesk` → `rdelgado` | The release decision, and both refusals — subject, then requester |
| **A3** | `maria.gonzalez` | Someone else's chart and a nonexistent one give the *identical* answer |
| **A4** | `frontdesk` | A grounded answer with sources, then a refusal that is not an error |
| **A5** | `frontdesk` | The ingest gate — read what you are about to publish to every patient |
| **A6** | `frontdesk` | Retrieval `1.000` beside coverage `0.556` — both true at once |
| **A7** | `frontdesk` | Payer stopped; registration still completes |

If you only have ten minutes: **A1, A2, A6.** A1 sets up the problem, A2 is the
control working between two people, A6 is the number that reframes the whole
engagement.

### A1 — Log in as Maria, and look at what she can see *(~2 min)*

Sign in as `maria.gonzalez`. Her dashboard opens with **Your complete record**.

Point at three things, in this order:

1. **"Your record is held across 3 charts at this clinic, and all of them are
   included here."**
2. Scroll to **Results and allergies**. It says *penicillin*.
3. The sidebar. There is no Intake, no Release of Information, no Eligibility,
   no Approvals.

> *"She signed in as one chart. The system resolved her to three, because they are
> the same person — and the allergy is on the chart her login is not attached to.
> Before this, she would have seen a record that looked complete."*

### A2 — The same record, seen by the clinic *(~3 min — this is the one they'll remember)*

Open a second browser profile. Sign in as `frontdesk` and go to
**Records → 1042**.

Then open **Approvals**.

> *"A clinician asking for Maria's assembled record does not get it immediately.
> It is a merge of three charts, and materialising that merge is a disclosure
> decision, so it waits for a person."*

The queue row reads:

> Release the record view for chart 1042, assembled across 3 charts, requested by
> frontdesk.

Point out that it says **what is being released**, not a run id.

Now try to approve it as `frontdesk` — the person who asked for it:

> **403** — this view was requested by you, someone else has to release it.

Sign in as `rdelgado` and release it. The clinician now sees all three charts,
including the penicillin allergy.

> *"Two things there. The person who wants the record cannot be the person who
> approves it. And until someone approved it, the record was assembled and not
> returned — we checked that, because the first version handed it over anyway."*

### A3 — Try to read someone else's chart *(~2 min)*

Still as `maria.gonzalez`, go to **Records** and type `1043` — a different
patient.

> *"No records found for this patient."*

Now type `999999`, which does not exist. **The same message.**

> *"If unauthorized said 'forbidden' and missing said 'not found', you could probe
> for which patient IDs exist. Both say the same thing."*

The original capture is in `docs/handover/portal.har` — both of those requests
returning **200** with a full chart. Worth opening side by side.

### A4 — Ask the assistant, and watch it refuse *(~2 min)*

Go to **Knowledge**. Ask:

> How long must a patient fast before a blood draw?

You get an answer with its **sources listed underneath**, and a line reading
*Steps the assistant ran: retrieve → relevance gate → generate → ground gate →
answer*.

Now ask something the clinic has no policy on:

> What is the clinic's policy on interplanetary travel reimbursement?

> **No answer in scope.** The assistant found nothing relevant in the records it
> is allowed to read. This is not an error, and it does not mean the answer is no.

> *"That is the failure mode that matters. A system that guesses here is a system
> that tells someone a patient has no allergies."*

### A5 — Add a document, and read what you are about to publish *(~3 min)*

Still on **Knowledge**, as `frontdesk`. Drop in any PDF.

Nothing is indexed yet. You get a review screen that says:

> **This will be readable by every patient.** Once added, this text can be
> returned — with a citation — to anyone who asks the assistant a related
> question, including patients.

It shows the **exact text that will be indexed**, and what the scrubber removed.

> *"The knowledge base has no per-patient filter, by design — clinic policy should
> be answerable to whoever asks. Which means one document with a patient's name in
> it becomes readable by every patient, indefinitely, with a citation. So it is
> two steps, and the second one shows you the consequence rather than asking 'are
> you sure'."*

Press **Add**, then ask a question only that document answers. It comes back
cited.

Try dropping a `.docx`:

> **DOCX files are not supported yet.** Save the document as a PDF and upload
> that.

> *"Not 'unsupported file type'. DOCX is a zip archive and zip handling deserves
> its own review, so we said so instead of shipping it thin."*

### A6 — The quality report *(~2 min)*

**Knowledge → Answer quality → Run the evaluation.**

Two tables, side by side:

| Retrieval | | Record integrity | |
|---|---|---|---|
| Context recall | **1.000** | Fragment coverage | **0.556** |
| Context precision | 1.000 | Duplicate patient rate | 0.333 |

> *"Both true at the same time. The search finds the right chart every time. The
> charts are the problem — one person is several records, so an assistant
> answering from one of them is confidently incomplete."*

Below it, named patients and chart ids: **Maria Gonzalez — 1042, 1330, 1588.**

> *"0.556 is arguable. That row is not."*

This screen is staff-only. Sign in as Maria and try it: *"This view is for clinic
staff."*

### A7 — Break the payer on purpose *(~2 min)*

```bash
docker compose stop eligibility-service
```

Go to **Eligibility** as `frontdesk`, check any member ID.

> Could not verify · payer unreachable since
>
> Proceed with registration and mark coverage as unverified. Do not turn the
> patient away — this is the last value we retrieved, not a denial.

Now go to **Intake** and register a patient. It completes.

```bash
docker compose start eligibility-service
```

> *"On the Tuesday in question, that hung for nineteen minutes and nobody could
> register anyone. Registration no longer waits on a third party — and notice the
> screen says what to do, not just what happened."*

---

## B. The scripted fallback

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

### Act 2 — One patient, three charts *(~90s)*

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
| **340 tests, $0** | `make test` | "Green without an AI key" |
| **14 browser journeys** | `npm run test:e2e` | "A person doing it, not an API answering" |
