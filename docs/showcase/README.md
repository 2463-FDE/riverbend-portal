# Showcase — Riverbend Weeks 1–4

Everything needed to present this engagement to a non-technical audience.

| File | What it is |
|---|---|
| `deck.html` | The board deck. Open in any browser — self-contained, no build, prints to PDF. Also published at the artifact link in the engagement notes. |
| `DEMO.md` | The walkthrough script. Two paths: a 4-minute scripted demo needing only Python, and a 15-minute live-stack version. |
| `../../scripts/demo.py` | The scripted demo itself. Four acts, ~30 seconds, no AWS key, no spend. |
| `agentic-architecture.{mmd,svg,png,excalidraw}` | How the system fits together |
| `patient-view-flow.{mmd,svg,png,excalidraw}` | The Week-4 permission sequence |

## Running the demo

```bash
python3 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt
.venv/bin/python scripts/demo.py          # all four acts
.venv/bin/python scripts/demo.py 2        # just the one they'll remember
```

Every figure it prints is computed live from `db/seed/*.csv` — the client's own
handover data. Nothing is hard-coded, so it cannot drift out of sync with the
code, and it cannot be accused of being a mock-up.

## Editing the diagrams

The `.excalidraw` files open at [excalidraw.com](https://excalidraw.com)
(File → Open). The `.mmd` files are the source of truth — edit those and
re-render rather than hand-editing the SVG.

## Presenting

Read `DEMO.md` first. It carries the lines worth saying out loud, the moments to
pause on, and the questions to expect — including the two we cannot answer
("has anyone actually read charts they shouldn't have?") and why that inability
is itself a finding.

**The single slide that lands hardest** is the three-charts table: same Social
Security number, same address, same phone, and the penicillin allergy on only one
of them. Let it sit on screen before talking.

## A note on the exported diagrams

`agentic-architecture.{svg,png}` and `patient-view-flow.{svg,png}` are exports of
the `.mmd` sources beside them, and they are **stale relative to the `.mmd`**.
Two corrections landed in the source after the last export:

- the architecture diagram claimed *"AWS Bedrock — data retention: none"*. That
  is false. Measured 2026-07-30: the account is `inherit`, resolving to
  `default`. Zero data retention is **available and not enabled** — see
  `docs/debt-register.md` D-11.
- the patient-view diagram said "a human approves"; it is now specific that
  neither the subject nor the requester may be that person.

Re-export before using them anywhere client-facing. The deck does **not** depend
on them — slide 8 carries its own inline SVG, which follows the deck's theme and
is generated from nothing.
