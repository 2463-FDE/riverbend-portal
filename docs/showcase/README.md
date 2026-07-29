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
