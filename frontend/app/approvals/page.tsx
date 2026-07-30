"use client";

import { useCallback, useEffect, useState } from "react";
import Card from "@/app/components/Card";
import { usePrincipal } from "@/app/lib/principal";
import { apiFetch } from "@/app/lib/session";

/**
 * The HITL queue — `RVB-W4-U3`, `RVB-W4-U4`, ADR 0015 rule 4.
 *
 * The rule: a human gate renders as a **decision**, not a delay. So each row
 * states what is actually being released — the patient, the chart span, who
 * asked — and never a run id. *"Approve run view-a3f9"* is a button that gets
 * clicked; a sentence describing a disclosure gets read.
 *
 * The handle is an **opaque approval id** (UI-D20, codex F9). It replaced a
 * client-supplied `thread_id` that the gateway forwarded verbatim — the same
 * shape as the F1 IDOR, and it made a checkpointer implementation detail into
 * part of the API.
 *
 * Both outcomes are explicit, and neither is the default action. A queue where
 * "approve" is the primary button and "deny" is a text link has decided for you.
 */

interface ApprovalRow {
  approval_id: string;
  patient_id: number;
  requested_by: string;
  requested_principal: string;
  chart_span: number;
  authorized_ids: number[];
  reason: string;
  created_at: number;
  describe: string;
}

export default function ApprovalsPage() {
  const { status: principalStatus, principal } = usePrincipal();
  const [rows, setRows] = useState<ApprovalRow[]>([]);
  const [httpStatus, setHttpStatus] = useState<number | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<{ id: string; released: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await apiFetch("/api/ai/approvals");
      setHttpStatus(res.status);
      const body = await res.json().catch(() => null);
      setRows(res.ok ? (body?.approvals ?? []) : []);
    } catch {
      setHttpStatus(502);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function decide(id: string, approved: boolean) {
    setBusy(id);
    setError(null);
    try {
      const res = await apiFetch(`/api/ai/approvals/${encodeURIComponent(id)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        setError(body?.detail ?? "That decision could not be recorded.");
        return;
      }
      // The outcome is shown, not merely implied by the row disappearing
      // (RVB-W4-U4). A row vanishing is indistinguishable from a failed request.
      setOutcome({ id, released: Boolean(body?.released) });
      await load();
    } catch {
      setError("That decision did not reach the server. Nothing was released.");
    } finally {
      setBusy(null);
    }
  }

  if (principalStatus === "loading") {
    return <p className="rb-muted">Loading…</p>;
  }

  if (httpStatus === 403 || (principal && !principal.canApprove)) {
    return (
      <div className="rb-stack">
        <Card title="Approvals">
          <div className="rb-alert rb-alert--info" role="status" data-testid="approvals-denied">
            <strong>You cannot release record views</strong>
            <p className="rb-alert__body">
              Releasing an assembled record view is a separate permission from
              adding knowledge-base documents. Ask a clinic administrator if you
              need it.
            </p>
          </div>
        </Card>
      </div>
    );
  }

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Approvals</h1>
        <p>
          Record views that span more than one chart wait here for a person. Until
          one of these is decided, nothing has been disclosed.
        </p>
      </div>

      {httpStatus === 503 && (
        <div className="rb-alert rb-alert--err" role="alert" data-testid="approvals-unavailable">
          <strong>The approvals queue is unavailable</strong>
          <p className="rb-alert__body">
            Paused views cannot be listed or released right now. Nothing has been
            disclosed — this is a availability problem, not a decision.
          </p>
        </div>
      )}

      {outcome && (
        <div
          className={`rb-alert ${outcome.released ? "rb-alert--ok" : "rb-alert--warn"}`}
          role="status"
          data-testid="decision-outcome"
        >
          <strong>{outcome.released ? "Released" : "Not released"}</strong>
          <p className="rb-alert__body">
            {outcome.released
              ? "The assembled view has been released to the requester."
              : "The view was withheld. The requester is told it was reviewed and not released."}
          </p>
        </div>
      )}

      {error && (
        <div className="rb-alert rb-alert--err" role="alert" data-testid="decision-error">
          {error}
        </div>
      )}

      {rows.length === 0 && httpStatus !== 503 ? (
        <Card title="Nothing waiting">
          <p className="rb-muted" data-testid="approvals-empty">
            No record views are waiting on a decision.
          </p>
        </Card>
      ) : (
        rows.map((row) => (
          <Card key={row.approval_id} title={`Chart ${row.patient_id}`}>
            <p className="rb-lede" data-testid={`describe-${row.approval_id}`}>
              {row.describe}
            </p>

            <dl className="rb-meta">
              <dt>Charts included</dt>
              <dd>{row.authorized_ids.join(", ")}</dd>
              <dt>Requested by</dt>
              <dd>
                {row.requested_by || "unidentified session"}
                {row.requested_principal ? ` (${row.requested_principal})` : ""}
              </dd>
              <dt>Why this paused</dt>
              <dd>{row.reason.replace(/_/g, " ")}</dd>
            </dl>

            {/* Both outcomes explicit, neither styled as the default. */}
            <div className="rb-actions">
              <button
                className="rb-btn rb-btn--primary"
                disabled={busy === row.approval_id}
                onClick={() => decide(row.approval_id, true)}
                data-testid={`approve-${row.approval_id}`}
              >
                {busy === row.approval_id ? "Recording…" : "Release this view"}
              </button>
              <button
                className="rb-btn rb-btn--primary"
                disabled={busy === row.approval_id}
                onClick={() => decide(row.approval_id, false)}
                data-testid={`deny-${row.approval_id}`}
              >
                Do not release
              </button>
            </div>
          </Card>
        ))
      )}
    </div>
  );
}
