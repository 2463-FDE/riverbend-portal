"use client";

import { useEffect, useState } from "react";
import Card from "./Card";
import { PatientView, type PatientViewPayload } from "./PatientView";
import { apiFetch } from "../lib/session";

/**
 * A patient's own assembled record, on their landing page — `RVB-W4-U2`.
 *
 * This is the screen the whole engagement points at. Maria Gonzalez is three
 * charts, and the chart she is *bound* to is the one WITHOUT the penicillin
 * allergy. Before the identity work she would log in and see a record that looked
 * complete and was not.
 *
 * Which is why the failure states here are worded so carefully (UI-D19): a domain
 * that could not load must never read as a domain that is empty. That distinction
 * is the same one the Week-2 finding is about, one layer up.
 */
export function AssembledRecordCard({ patientId }: { patientId: number }) {
  const [view, setView] = useState<PatientViewPayload | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/ai/patient-view/${patientId}`)
      .then(async (r) => {
        if (cancelled) return;
        const body = await r.json().catch(() => null);
        if (!r.ok || !body) {
          setState("failed");
          return;
        }
        setView(body as PatientViewPayload);
        setState("ready");
      })
      .catch(() => {
        if (!cancelled) setState("failed");
      });
    return () => {
      cancelled = true;
    };
  }, [patientId]);

  return (
    <Card title="Your complete record">
      {state === "loading" && (
        <p className="rb-muted" data-testid="assembled-loading">
          Assembling your record across the clinic&apos;s charts…
        </p>
      )}

      {state === "failed" && (
        // Not an empty record. Saying "no records" here would be the Week-2
        // failure with a nicer font.
        <div className="rb-alert rb-alert--warn" role="status" data-testid="assembled-failed">
          <strong>Your record could not be assembled</strong>
          <p className="rb-alert__body">
            This is a problem loading it, not a statement that your record is
            empty. Please try again shortly or contact the clinic.
          </p>
        </div>
      )}

      {state === "ready" && view && <PatientView view={view} isOwnRecord />}
    </Card>
  );
}
