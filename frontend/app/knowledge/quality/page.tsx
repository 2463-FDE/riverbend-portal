"use client";

import { useCallback, useEffect, useState } from "react";
import Card from "@/app/components/Card";
import { apiFetch } from "@/app/lib/session";

/**
 * The eval report as a staff screen — `RVB-W2-U3`, `RVB-W2-U4`, ADR 0015 rule 5.
 *
 * The finding this page exists to make actionable currently lives in terminal
 * output that engineers read. The person who can fix a 0.556 fragment coverage
 * is the front-desk lead who knows Maria has three charts, and they will never
 * run `pytest`.
 *
 * The framing is the design decision (UI-D12). `0.556` shown bare reads as "the
 * AI is 55% correct", which is not what it means and would be a true number
 * causing a false conclusion. So retrieval sits **beside** integrity, labelled,
 * with the sentence a non-engineer can repeat in a meeting.
 *
 * Staff-only. The gateway enforces it — this page relays a 403 rather than
 * pretending the data is empty (codex F2: it names every patient and states why
 * their charts matched, including `identical_ssn`).
 */

interface EvalReport {
  run_id?: string;
  mode?: string;
  embed_backend?: string;
  metrics?: Record<string, number>;
  integrity?: {
    duplicate_patient_rate?: number;
    distinct_humans?: number;
    patient_rows?: number;
    fragmented_humans?: number;
    fragment_coverage?: number;
    clinically_incomplete_answers?: number;
    identity_split_examples?: {
      display_name: string;
      patient_ids: number[];
      certain: boolean;
      reasons: string[];
    }[];
  };
  note?: string;
  detail?: string;
}

const RETRIEVAL_LABELS: Record<string, string> = {
  context_recall: "Context recall",
  context_precision: "Context precision",
  groundedness: "Groundedness",
  answer_match: "Answer match",
};

export default function QualityPage() {
  const [report, setReport] = useState<EvalReport | null>(null);
  const [status, setStatus] = useState<number | null>(null);
  const [running, setRunning] = useState(false);

  const load = useCallback(async () => {
    const res = await apiFetch("/api/ai/knowledge/eval");
    setStatus(res.status);
    setReport(await res.json().catch(() => null));
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function run() {
    setRunning(true);
    try {
      const res = await apiFetch("/api/ai/knowledge/eval", { method: "POST" });
      setStatus(res.status);
      setReport(await res.json().catch(() => null));
    } finally {
      setRunning(false);
    }
  }

  if (status === 403) {
    return (
      <div className="rb-stack">
        <Card title="Answer quality">
          <div className="rb-alert rb-alert--info" role="status" data-testid="quality-denied">
            <strong>This view is for clinic staff</strong>
            <p className="rb-alert__body">
              The quality report describes the whole record set, so it is not
              shown to patient accounts.
            </p>
          </div>
        </Card>
      </div>
    );
  }

  const integrity = report?.integrity;
  const metrics = report?.metrics;
  const splits = integrity?.identity_split_examples ?? [];
  const incomplete = integrity?.clinically_incomplete_answers ?? 0;

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Answer quality</h1>
        <p>
          Retrieval is measured separately from the data it retrieves from. The
          difference is the point.
        </p>
      </div>

      <Card title="Answer quality">
        <button className="rb-btn rb-btn--primary" onClick={run} disabled={running}>
          {running ? "Running…" : "Run the evaluation"}
        </button>

        {!metrics && !report?.note && (
          <p className="rb-muted" data-testid="quality-empty">
            No evaluation has been run yet.
          </p>
        )}
        {report?.note && (
          <p className="rb-muted" data-testid="quality-note">
            {report.note}
          </p>
        )}
      </Card>

      {metrics && (
        <div className="rb-grid rb-grid--2">
          {/* Deliberately side by side (UI-D12). Apart, the second table reads
              as a model failure; together, it reads as what it is. */}
          <Card title="Retrieval — is the assistant finding the right text?">
            <table className="rb-table" data-testid="retrieval-metrics">
              <tbody>
                {Object.entries(RETRIEVAL_LABELS).map(([key, label]) => (
                  <tr key={key}>
                    <th scope="row">{label}</th>
                    <td className="rb-num">{fmt(metrics[key])}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="rb-muted">
              These measure the search. A high score here means retrieval is
              working — it says nothing about whether the record is complete.
            </p>
          </Card>

          <Card title="Record integrity — is the underlying data whole?">
            <table className="rb-table" data-testid="integrity-metrics">
              <tbody>
                <tr>
                  <th scope="row">Fragment coverage</th>
                  <td className="rb-num">{fmt(integrity?.fragment_coverage)}</td>
                </tr>
                <tr>
                  <th scope="row">Duplicate patient rate</th>
                  <td className="rb-num">{fmt(integrity?.duplicate_patient_rate)}</td>
                </tr>
                <tr>
                  <th scope="row">People</th>
                  <td className="rb-num">
                    {integrity?.distinct_humans ?? "—"} across{" "}
                    {integrity?.patient_rows ?? "—"} charts
                  </td>
                </tr>
              </tbody>
            </table>
            <p className="rb-muted">
              These measure the data Riverbend handed over. A low score here is a
              records problem, not a model problem.
            </p>
          </Card>
        </div>
      )}

      {metrics && (
        <Card title="What this means">
          {/* The sentence someone can repeat in a meeting (UI-D12). */}
          <p className="rb-lede" data-testid="quality-lede">
            Retrieval finds the right chart every time. The charts themselves are
            split: one person can be several patient records, so an assistant
            answering from one of them gives a clinically incomplete answer while
            reporting itself as grounded.
          </p>
        </Card>
      )}

      {incomplete > 0 && (
        // Not a metric — a count of times the system would have told someone
        // the wrong thing. It gets its own treatment.
        <Card title="Clinically incomplete answers">
          <div className="rb-alert rb-alert--err" role="alert" data-testid="incomplete-answers">
            <strong>
              {incomplete} answer{incomplete === 1 ? "" : "s"} in the gold set were
              complete-looking and wrong
            </strong>
            <p className="rb-alert__body">
              Each was grounded and cited, and each omitted information held on
              another chart belonging to the same person. This is the failure mode
              that motivated the identity work — not a scoring artefact.
            </p>
          </div>
        </Card>
      )}

      {splits.length > 0 && (
        <Card title="One person, several charts">
          {/* Named patients and chart ids, not only a rate (RVB-W2-U4).
              `0.556` is arguable; "Maria Gonzalez — 1042, 1330, 1588" is not. */}
          <table className="rb-table" data-testid="identity-splits">
            <thead>
              <tr>
                <th scope="col">Patient</th>
                <th scope="col">Charts</th>
                <th scope="col">Confidence</th>
                <th scope="col">Why they matched</th>
              </tr>
            </thead>
            <tbody>
              {splits.map((s) => (
                <tr key={s.display_name + s.patient_ids.join("-")}>
                  <td>{s.display_name}</td>
                  <td className="rb-num">{s.patient_ids.join(", ")}</td>
                  <td>{s.certain ? "Certain" : "Probable"}</td>
                  <td className="rb-muted">
                    {s.reasons.map((r) => r.replace(/_/g, " ")).join("; ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}

function fmt(value: number | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "—";
  return value.toFixed(3);
}
