# Finding W1-UI-2 — Three defects survived 248 green tests because nothing ever started the stack

- **Severity:** High (process). One of the three was a live patient-safety regression.
- **Status:** All three fixed. The process gap is **open** and needs a CI decision.
- **Found by:** the client asking why the UI phase took twelve minutes

---

## What happened

Everything in this engagement was verified by unit and component tests. 248
backend tests, 36 frontend tests, all green, across five merged pull requests.

**`docker compose up` had never been run.** Not once, in any PR. When it finally
was, three defects surfaced in the first four minutes — and none of them were
subtle.

## The three

### 1. The database could not initialise at all

`db/schema.sql` creates `users` before `patients`, and #16 added
`patient_id INTEGER REFERENCES patients(id)` to `users` — a foreign key pointing
at a table defined twenty lines later in the same file.

```
psql:/docker-entrypoint-initdb.d/01-schema.sql:28:
  ERROR:  relation "patients" does not exist
```

Postgres exited 3 and every dependent service failed to start. **The entire stack
had been unstartable from a clean volume since #16 merged.** Every test mocks the
database, so nothing read this file.

*Fixed:* the column is declared without the inline reference; the constraint and
unique index are added after `patients` exists.

### 2. The container could not find the handover data

`corpus.py` resolved the seed directory as `../../db/seed` relative to its own
file. That works from a checkout. In the container the module sits at `/app`, so
it resolved to `/db/seed`, which does not exist.

```
FileNotFoundError: [Errno 2] No such file or directory: '/db/seed/patients.csv'
```

Everything reading the corpus was broken in Docker: identity clusters, the
knowledge seed, the eval harness, the patient view. **Every test runs from the
repository root**, where the relative path happens to resolve.

*Fixed:* `RIVERBEND_SEED_DIR`, a compose mount, and a `seed_path()` helper that
fails with a message naming the directory it looked in.

### 3. The one that matters — a live patient-safety regression

Defect 2 had a consequence worse than a 500.

`_same_as_lookup` is best-effort by design: if identity resolution fails, it
**narrows** the scope to the patient's own chart, because identity resolution
must never be able to *widen* access. That is correct, and it is deliberate.

But with the corpus unreadable, it failed on every request. So it narrowed on
every request. Live, against a real login:

```
/me scope → patient_ids: [1042]

chart 1042 -> 200
chart 1330 -> 404      ← her own chart
chart 1588 -> 404      ← her own chart
```

**Maria Gonzalez could not reach chart 1330 — the one carrying her penicillin
allergy.** The Week-4 authorization fix had turned the Week-2 fragmentation into
an access denial, live, in precisely the way `adr/0011`, the W4 spec, and the
design debate each warned it must not.

Three documents anticipated this failure by name. A test asserted it could not
happen. It was happening anyway, because the test and the running system
disagreed about where a file lived.

*Fixed with 2.* Verified live:

```
/me scope → patient_ids: [1042, 1330, 1588]

chart 1042 -> 200
chart 1330 -> 200   record: "Penicillin allergy confirmed."
chart 1588 -> 200
chart 1043 -> 404   ← another patient. Still denied.
chart 999999 -> 404 ← identical response. No enumeration oracle.
```

## Why the tests could not catch these

Not bad tests. Tests of a **different system**.

| | The tests ran | The stack runs |
|---|---|---|
| Working directory | repository root | `/app` in a container |
| Database | mocked, or absent | real Postgres, initialised from `schema.sql` |
| Service boundaries | in-process function calls | HTTP between containers |
| Config | test defaults | compose environment |

Every one of the three defects lives in a difference between those columns. No
amount of additional unit testing reaches them, because the thing that is wrong
is the gap itself.

## The honest version of what went wrong

The claim in PR #17 was:

> **Can a person do this in a browser, and did we watch them do it?** Yes.

That was false. The journey was written and gated; it had never executed. It is
the same error the whole UI phase exists to correct — treating an artifact that
*stands in for* the thing as the thing. Four PR bodies were rewritten for exactly
this, and then it recurred one PR later, in the sentence written to prevent it.

The pattern is worth naming because it is not about carelessness. Writing a test
feels like verifying. It is not. **Running it is.**

## Recommended remediation

1. **A smoke job in CI that runs `docker compose up`, `make seed`, and hits `/healthz` on every service.** Roughly fifteen minutes to add, and it catches all three of the above on the commit that introduces them. This is the one that matters.
2. **Run the Playwright journeys in that same job** once the stack is up, rather than leaving them opt-in forever. The opt-in gate (`adr/0013`) was the right call when there was no stack in CI; it stops being right once there is.
3. **A test that loads `db/schema.sql` into a throwaway Postgres.** The schema is currently the only file in the repository with no test of any kind.
4. **Treat "did it run?" as a distinct question from "did the tests pass?"** in every future PR body. The standing question at the top of each PR should be answered with output, not intent.

Items 1 and 3 would have caught all three defects. They are small. They were not
done because everything looked green.
