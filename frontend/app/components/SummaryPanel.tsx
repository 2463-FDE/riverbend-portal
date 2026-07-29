"use client";

import { useEffect, useState } from "react";
import Card from "./Card";
import { IconIntake } from "./icons";
import { apiFetch } from "../lib/session";
import type { AiHealth, SummaryResponse } from "../lib/types";

/**
 * W1 (2/2) — the intake-instruction summariser, finally on screen.
 *
 * The state machine is the whole point of this component. `POST /ai/summary`
 * produces eight distinct outcomes and an earlier draft of the spec named three;
 * a state with no rendering is a blank panel in a demo.
 *
 * The one that matters most is WITHHELD. When the guardrail rejects a summary
 * the API returns a safe message with `grounded: false` — this component must
 * never reach past that for the model's actual words, because the case it exists
 * for is a summary that reads perfectly and invents a medication.
 *
 * Note what is deliberately absent: the guardrail's reason codes. They are not
 * in the response, and they should not be. `invented_medication:metformin` is a
 * clinical string; returning it here would hand it to patients too, and hiding
 * it client-side would be theatre. It lives in the audit log.
 */

type PanelState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "grounded"; result: SummaryResponse }
  | { kind: "withheld"; result: SummaryResponse }
  | { kind: "refused"; reason: RefusalReason; message: string }
  | { kind: "failed"; message: string };

type RefusalReason =
  | "retention_policy"
  | "source_too_short"
  | "budget"
  | "guardrail_blocked"
  | "model_unavailable"
  | "unknown";

// Each refusal gets copy that tells the reader what to DO. "Something went
// wrong" for all of them would be one line of code and five different problems.
const REFUSAL_COPY: Record<RefusalReason, string> = {
  retention_policy:
    "The summary feature is unavailable pending a data-retention configuration check. Contact your administrator.",
  source_too_short:
    "There is not enough text here to summarise. Add the visit instructions and try again.",
  budget:
    "That text is too long to summarise. Try summarising one section at a time.",
  guardrail_blocked:
    "This summary was held back for review. A staff member will check it.",
  model_unavailable:
    "The summary service is temporarily unavailable. Please try again shortly.",
  unknown: "The summary could not be generated. Please try again.",
};

function refusalReason(result: SummaryResponse): RefusalReason {
  const raw = result.usage?.refused ?? "";
  const known: RefusalReason[] = [
    "retention_policy",
    "source_too_short",
    "budget",
    "guardrail_blocked",
    "model_unavailable",
  ];
  return known.find((k) => raw.startsWith(k)) ?? "unknown";
}

export default function SummaryPanel({
  instructions,
  onSummary,
}: {
  instructions: string;
  onSummary?: (summary: string) => void;
}) {
  const [state, setState] = useState<PanelState>({ kind: "idle" });
  const [health, setHealth] = useState<AiHealth | null>(null);

  // RVB-W1-U3: read the retention posture on mount so submit can be disabled
  // before anything is sent, rather than after a refused round trip.
  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/ai/health")
      .then((r) => (r.ok ? r.json() : null))
      .then((h) => !cancelled && setHealth(h))
      .catch(() => {
        /* health unknown -> stay enabled; the endpoint still refuses safely */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const retentionBlocked = health?.retention?.ok === false;
  const tooShort = instructions.trim().length < 20;
  const busy = state.kind === "submitting";
  const disabled = busy || retentionBlocked || tooShort;

  async function summarise() {
    setState({ kind: "submitting" });
    try {
      const res = await apiFetch("/api/ai/summary", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instructions }),
      });

      // Since RVB-U-09 the gateway preserves upstream status, so a non-2xx here
      // is a real failure rather than a 200 carrying an error key.
      if (!res.ok) {
        setState({
          kind: "failed",
          message:
            res.status === 422
              ? "That text could not be accepted. Check it and try again."
              : "The summary service could not be reached. Please try again.",
        });
        return;
      }

      const result: SummaryResponse = await res.json();

      if (result.usage?.refused) {
        const reason = refusalReason(result);
        setState({ kind: "refused", reason, message: REFUSAL_COPY[reason] });
        return;
      }
      if (!result.grounded) {
        setState({ kind: "withheld", result });
        return;
      }
      setState({ kind: "grounded", result });
      onSummary?.(result.summary);
    } catch {
      setState({
        kind: "failed",
        message: "The summary service could not be reached. Please try again.",
      });
    }
  }

  return (
    <Card title="Patient-friendly summary" icon={<IconIntake />}>
      <p className="rb-muted" style={{ marginTop: 0 }}>
        Turn the visit instructions above into something a patient can read.
      </p>

      {retentionBlocked && (
        <div className="rb-alert rb-alert--warn" role="status">
          {REFUSAL_COPY.retention_policy}
        </div>
      )}

      <button
        type="button"
        className="rb-btn rb-btn--primary"
        onClick={summarise}
        disabled={disabled}
        data-testid="summarise"
      >
        {busy ? "Summarising…" : "Summarise for the patient"}
      </button>

      {tooShort && !retentionBlocked && (
        <span className="rb-field__hint" data-testid="too-short-hint">
          Add the visit instructions to enable this.
        </span>
      )}

      {state.kind === "grounded" && (
        <div className="rb-alert rb-alert--ok" data-testid="summary-grounded">
          <p style={{ margin: 0 }}>{state.result.summary}</p>
          <span className="rb-field__hint">
            Checked against the instructions above.
          </span>
        </div>
      )}

      {state.kind === "withheld" && (
        <div className="rb-alert rb-alert--warn" data-testid="summary-withheld">
          <p style={{ margin: 0 }}>{state.result.summary}</p>
          <span className="rb-field__hint">
            This summary was held back for review because it contained
            information that was not in the instructions. A staff member will
            check it.
          </span>
        </div>
      )}

      {state.kind === "refused" && (
        <div
          className="rb-alert rb-alert--info"
          role="status"
          data-testid={`summary-refused-${state.reason}`}
        >
          {state.message}
        </div>
      )}

      {state.kind === "failed" && (
        <div className="rb-alert rb-alert--err" role="alert" data-testid="summary-failed">
          {state.message}
        </div>
      )}
    </Card>
  );
}
