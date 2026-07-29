"use client";

import {
  AgentEnvelope,
  AgentOutcome,
  PRESENTATION,
  outcomeBody,
  resolveOutcome,
} from "@/app/lib/outcome";

/**
 * One panel, seven outcomes — ADR 0015 rules 1–3.
 *
 * The rules this component exists to enforce, each of which the obvious
 * implementation gets wrong:
 *
 *   1. A refusal is a **first-class answer**. It is never routed through the
 *      error styling and never rendered as empty space. A refusal that looks
 *      like an empty result gets read as "nothing on file", which is how an
 *      assistant reports no allergies for a patient with a penicillin allergy.
 *
 *   2. **Provenance is inline.** Citations render with the answer, not behind a
 *      "sources" toggle. If the citation is one click away, the citation does
 *      not exist.
 *
 *   3. **The path replaces the spinner.** Agent calls take seconds; the graph
 *      already tells us which nodes it ran, so dead time becomes an explanation
 *      rather than a progress bar that reads as broken.
 */

const TONE_CLASS: Record<string, string> = {
  ok: "rb-alert--ok",
  warn: "rb-alert--warn",
  info: "rb-alert--info",
  error: "rb-alert--err",
};

interface Props {
  data: AgentEnvelope | null;
  ok?: boolean;
  loading?: boolean;
  /** Node names as they arrive, so the path can render while the call is open. */
  pendingPath?: string[];
  emptyHint?: string;
}

export function AnswerPanel({ data, ok = true, loading, pendingPath, emptyHint }: Props) {
  if (loading) {
    return <PathProgress path={pendingPath} />;
  }

  if (!data) {
    return (
      <p className="rb-muted" data-testid="answer-empty">
        {emptyHint ?? "Ask a question to get started."}
      </p>
    );
  }

  const outcome = resolveOutcome(data, ok);
  const presentation = PRESENTATION[outcome];
  const body = outcomeBody(data);
  const citations = data.citations ?? [];

  return (
    <div data-testid="answer-panel" data-outcome={outcome}>
      <div
        className={`rb-alert ${TONE_CLASS[presentation.tone]}`}
        role={presentation.tone === "error" ? "alert" : "status"}
      >
        <strong>{presentation.label}</strong>
        <p className="rb-alert__body">{presentation.meaning}</p>
      </div>

      {/*
        The body is shown for every outcome that HAS one, including withheld and
        ungrounded — with the qualification above it, never instead of it.
        Hiding the text on a qualified outcome pushes people to re-run the query
        until they get an unqualified one, which is worse than showing it framed.
      */}
      {body ? (
        <p className="rb-answer" data-testid="answer-body">
          {body}
        </p>
      ) : (
        <p className="rb-muted" data-testid="answer-no-body">
          Nothing was returned for this question. This is a stated absence, not a
          blank result.
        </p>
      )}

      {citations.length > 0 && (
        <div data-testid="citations">
          <h3 className="rb-h4">Sources</h3>
          <ol className="rb-citations">
            {citations.map((c) => (
              <li key={`${c.doc_id}-${c.chunk_index}`} className="rb-citation">
                <span className="rb-citation__marker">[{c.marker}]</span>
                <span className="rb-citation__title">{c.doc_title || c.doc_id}</span>
                {typeof c.score === "number" && (
                  <span className="rb-citation__score">
                    relevance {c.score.toFixed(3)}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}

      {citations.length === 0 && outcome === "grounded" && (
        // Grounded with nothing cited is a contradiction, and a silent one.
        <p className="rb-alert rb-alert--warn" data-testid="grounded-without-citations">
          This answer reported itself as grounded but returned no sources. Treat
          it as unverified and check the record directly.
        </p>
      )}

      <PathProgress path={data.path} done />
    </div>
  );
}

/**
 * The graph's node sequence, as the progress indicator (rule 3).
 *
 * Labelled "steps the assistant ran" and never "reasoning": this is the path
 * through the graph, not an explanation of the model's thinking, and calling it
 * the latter would be the kind of overstatement this codebase keeps correcting.
 */
export function PathProgress({ path, done }: { path?: string[]; done?: boolean }) {
  if (!path || path.length === 0) {
    return done ? null : (
      <p className="rb-muted" data-testid="path-waiting">
        Working…
      </p>
    );
  }
  return (
    <div className="rb-path" data-testid="agent-path">
      <span className="rb-path__label">
        {done ? "Steps the assistant ran" : "Running"}
      </span>
      <ol className="rb-path__steps">
        {path.map((node, i) => (
          <li key={`${node}-${i}`} className="rb-path__step">
            {node.replace(/_/g, " ")}
          </li>
        ))}
      </ol>
    </div>
  );
}

export function outcomeOf(data: AgentEnvelope | null, ok = true): AgentOutcome {
  return resolveOutcome(data, ok);
}
