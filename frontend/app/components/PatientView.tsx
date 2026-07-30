"use client";

import { PathProgress } from "./AnswerPanel";

/**
 * The assembled record, domain by domain — `RVB-W4-U2`, UI-D19.
 *
 * Two things the obvious implementation gets wrong, and both are the Week-2
 * allergy failure wearing different hats:
 *
 * **One failed domain must not fail the page.** Four loaders fan out; if coverage
 * times out, the three that succeeded are still worth showing. A single rejected
 * promise taking down the whole view throws away real data.
 *
 * **A failed domain must not look like an empty one.** "No labs on file" and
 * "labs could not be loaded" are the same pixels and opposite facts. One of them
 * says the record is clear; the other says we do not know. Rendering an outage as
 * an absence is how an assistant reports no allergies for a patient who has one.
 */

export interface DomainResult {
  domain: string;
  status: string; // "ok" | "empty" | "unavailable" | "denied"
  data?: unknown;
  note?: string;
}

export interface PatientViewPayload {
  patient_id?: number;
  authorized?: boolean;
  released?: boolean;
  summary?: string;
  grounded?: boolean;
  domains?: Record<string, DomainResult>;
  deny_reason?: string;
  sensitive?: boolean;
  approved?: boolean | null;
  path?: string[];
  approval_id?: string;
  approval_error?: string;
}

const DOMAIN_LABEL: Record<string, string> = {
  demographics: "Your details",
  encounters: "Visits",
  labs: "Results and allergies",
  coverage: "Insurance",
};

const ORDER = ["demographics", "encounters", "labs", "coverage"];

export function PatientView({
  view,
  isOwnRecord,
}: {
  view: PatientViewPayload;
  isOwnRecord: boolean;
}) {
  const domains = view.domains ?? {};
  const entries = ORDER.filter((d) => d in domains).map((d) => [d, domains[d]] as const);
  const failed = entries.filter(([, r]) => r.status === "unavailable").map(([d]) => d);
  const chartSpan = countCharts(domains);

  // Denied outright — the gateway should have caught this first, so reaching
  // here means the graph's own authorize node did (defence in depth, adr/0009).
  if (view.authorized === false) {
    return (
      <div className="rb-alert rb-alert--err" role="alert" data-testid="view-denied">
        <strong>This record is not available to you</strong>
        <p className="rb-alert__body">
          If you believe this is wrong, contact the clinic — do not retry.
        </p>
      </div>
    );
  }

  // Paused at the sensitivity gate. NOT a spinner: a paused graph rendered as a
  // spinner is a hang (adr/0015 rule 4). And for a patient this is explicitly
  // "being reviewed", which is neither withheld nor empty (UI-D18).
  if (view.sensitive && !view.released && view.approved == null) {
    return (
      <div data-testid="view-pending-review">
        <div className="rb-alert rb-alert--warn" role="status">
          <strong>Pending review</strong>
          <p className="rb-alert__body">
            {isOwnRecord
              ? "This record spans more than one chart, so a member of clinic staff is reviewing it before it is shown. It is not being withheld from you, and nothing is missing — it is waiting on a person."
              : "This assembly spans more than one chart and is queued for a release decision."}
          </p>
        </div>
        {view.approval_error && (
          <div className="rb-alert rb-alert--err" role="alert" data-testid="approval-error">
            {view.approval_error}
          </div>
        )}
        <PathProgress path={view.path} done />
      </div>
    );
  }

  if (view.released === false && view.approved === false) {
    return (
      <div className="rb-alert rb-alert--warn" role="status" data-testid="view-withheld">
        <strong>Not released</strong>
        <p className="rb-alert__body">
          Clinic staff reviewed this assembled view and did not release it. Contact
          the clinic to ask why.
        </p>
      </div>
    );
  }

  return (
    <div data-testid="patient-view">
      {/*
        The incompleteness notice lives at the TOP as well as in place (UI-D19).
        Someone who scrolls straight to the section they care about will never see
        a notice three sections down.
      */}
      {failed.length > 0 && (
        <div className="rb-alert rb-alert--warn" role="status" data-testid="view-incomplete">
          <strong>This view is incomplete</strong>
          <p className="rb-alert__body">
            {failed.map((d) => DOMAIN_LABEL[d] ?? d).join(" and ")} could not be
            loaded. What is shown below is accurate; it is not everything.
          </p>
        </div>
      )}

      {chartSpan > 1 && (
        <p className="rb-alert rb-alert--info" data-testid="chart-span">
          {isOwnRecord
            ? `Your record is held across ${chartSpan} charts at this clinic, and all of them are included here.`
            : `This record is assembled from ${chartSpan} charts belonging to the same person.`}
        </p>
      )}

      {view.summary && (
        <>
          <h2 className="rb-h4">Summary</h2>
          <p className="rb-answer" data-testid="view-summary">
            {view.summary}
          </p>
          {view.grounded === false && (
            <p className="rb-alert rb-alert--warn" data-testid="summary-ungrounded">
              This summary could not be tied to the record below. Read the sections
              themselves.
            </p>
          )}
        </>
      )}

      {entries.map(([name, result]) => (
        <section key={name} className="rb-domain" data-testid={`domain-${name}`}
                 data-status={result.status}>
          <h3 className="rb-h4">{DOMAIN_LABEL[name] ?? name}</h3>
          <DomainBody name={name} result={result} />
        </section>
      ))}

      <PathProgress path={view.path} done />
    </div>
  );
}

function DomainBody({ name, result }: { name: string; result: DomainResult }) {
  if (result.status === "unavailable") {
    // Worded so it can never be mistaken for "there is nothing here".
    return (
      <p className="rb-alert rb-alert--warn" data-testid={`unavailable-${name}`}>
        This section <strong>could not be loaded</strong>. That is not the same as
        it being empty — try again shortly.
        {result.note ? ` (${result.note})` : ""}
      </p>
    );
  }

  if (result.status === "denied") {
    return (
      <p className="rb-muted" data-testid={`denied-${name}`}>
        Not available to you.
      </p>
    );
  }

  const rows = asRows(result.data);

  if (rows.length === 0) {
    // The other half of UI-D19: a legitimate absence, stated as one.
    return (
      <p className="rb-muted" data-testid={`empty-${name}`}>
        Nothing recorded. This section loaded correctly and is empty.
      </p>
    );
  }

  return (
    <ul className="rb-domain__list" data-testid={`rows-${name}`}>
      {rows.map((row, i) => (
        <li key={`${name}-${i}`}>{row}</li>
      ))}
    </ul>
  );
}

function asRows(data: unknown): string[] {
  if (data == null) return [];
  if (Array.isArray(data)) {
    return data.map((d) => (typeof d === "string" ? d : JSON.stringify(d))).filter(Boolean);
  }
  if (typeof data === "string") return data ? [data] : [];
  if (typeof data === "object") {
    return Object.entries(data as Record<string, unknown>)
      .filter(([, v]) => v !== null && v !== "" && v !== undefined)
      .map(([k, v]) => `${k.replace(/_/g, " ")}: ${String(v)}`);
  }
  return [String(data)];
}

/** How many distinct charts the assembly drew on, if the payload says. */
function countCharts(domains: Record<string, DomainResult>): number {
  const demo = domains.demographics;
  if (demo && Array.isArray(demo.data)) return demo.data.length;
  const note = demo?.note ?? "";
  const m = note.match(/(\d+)\s+charts?/i);
  return m ? Number(m[1]) : 1;
}
