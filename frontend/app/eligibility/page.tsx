"use client";

import { useState } from "react";
import Card from "@/app/components/Card";
import { AnswerPanel } from "@/app/components/AnswerPanel";
import { CoverageChip, type Coverage } from "@/app/components/CoverageChip";
import { apiFetch } from "@/app/lib/session";
import type { AgentEnvelope } from "@/app/lib/outcome";

/**
 * The front-desk eligibility desk — `RVB-W3-U2`, `RVB-W3-U3`.
 *
 * Two things on one screen, deliberately side by side: the **direct coverage
 * check**, which is authoritative, and the **assistant**, which is a
 * convenience. When the assistant's prose disagrees with the check, the check
 * wins and the UI says so (UI-D16, `overridden`).
 *
 * That override is shown rather than silently applied, and the reason is
 * observability before honesty: a safety control whose activations are invisible
 * cannot be evaluated, and this engagement has twice written findings about
 * claiming protections we cannot evidence. The copy frames the *payer* as
 * authoritative rather than the assistant as wrong — which is both true and
 * avoids teaching staff to distrust a tool they must use fifty times a shift.
 *
 * What is NOT here: the circuit-breaker state (UI-D17). Not because it is
 * internal, but because no reading of it changes what the person does next —
 * they proceed with registration and mark coverage unverified either way.
 * Staleness passes that test; breaker state does not.
 */
export default function EligibilityPage() {
  const [insuranceId, setInsuranceId] = useState("");
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);

  const [visitId, setVisitId] = useState("visit-1001");
  const [message, setMessage] = useState("");
  const [turn, setTurn] = useState<AgentEnvelope | null>(null);
  const [turnOk, setTurnOk] = useState(true);
  const [asking, setAsking] = useState(false);

  async function check(e: React.FormEvent) {
    e.preventDefault();
    if (!insuranceId.trim()) return;
    setChecking(true);
    setCheckError(null);
    // Rendered as `pending` while in flight -- the visible half of the W3
    // decoupling (RVB-W3-U3). Registration never waits on the payer.
    setCoverage({ status: "pending" });
    try {
      const res = await apiFetch(
        `/api/eligibility?insurance_id=${encodeURIComponent(insuranceId)}`
      );
      const data = await res.json();
      if (!res.ok) {
        setCheckError(data?.detail ?? "That coverage check could not be completed.");
        setCoverage(null);
        return;
      }
      setCoverage(data as Coverage);
    } catch {
      setCheckError("The coverage check did not reach the server.");
      setCoverage(null);
    } finally {
      setChecking(false);
    }
  }

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    if (!message.trim()) return;
    setAsking(true);
    setTurn(null);
    try {
      const res = await apiFetch("/api/ai/agent/eligibility", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ visit_id: visitId, message }),
      });
      setTurnOk(res.ok);
      setTurn(await res.json());
    } catch {
      setTurnOk(false);
      setTurn({ detail: "unreachable" });
    } finally {
      setAsking(false);
    }
  }

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Eligibility</h1>
        <p>
          Check coverage before the visit. If the payer cannot be reached,
          register the patient anyway and mark coverage unverified.
        </p>
      </div>

      <Card title="Coverage check">
        <form onSubmit={check}>
          <label className="rb-label" htmlFor="ins-id">
            Insurance member ID
          </label>
          <input
            id="ins-id"
            className="rb-input"
            value={insuranceId}
            onChange={(e) => setInsuranceId(e.target.value)}
            placeholder="BCBS-40192"
          />
          <button className="rb-btn rb-btn--primary" type="submit" disabled={checking}>
            {checking ? "Checking…" : "Check coverage"}
          </button>
        </form>

        <div className="rb-coverage-result">
          <span className="rb-label">Status</span>
          <CoverageChip coverage={coverage} />
        </div>

        {checkError && (
          <div className="rb-alert rb-alert--err" role="alert" data-testid="check-error">
            {checkError}
          </div>
        )}

        {coverage?.stale && (
          // The action, spelled out. The chip states the fact; this states what
          // to do about it, which is the thing the front desk actually needs.
          <p className="rb-alert rb-alert--warn" data-testid="stale-guidance">
            Proceed with registration and mark coverage as unverified. Do not turn
            the patient away — this is the last value we retrieved, not a denial.
          </p>
        )}
      </Card>

      <Card title="Ask the eligibility assistant">
        <p className="rb-muted">
          The assistant can look coverage up for you. The payer record is
          authoritative — where the two disagree, you will see the payer&apos;s
          answer.
        </p>

        <form onSubmit={ask}>
          <label className="rb-label" htmlFor="visit-id">
            Visit
          </label>
          <input
            id="visit-id"
            className="rb-input"
            value={visitId}
            onChange={(e) => setVisitId(e.target.value)}
          />

          <label className="rb-label" htmlFor="agent-msg">
            Message
          </label>
          <input
            id="agent-msg"
            className="rb-input"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="Is BCBS-40192 active for today's visit?"
          />
          <button className="rb-btn rb-btn--primary" type="submit" disabled={asking}>
            {asking ? "Asking…" : "Ask"}
          </button>
        </form>

        <AnswerPanel
          data={turn}
          ok={turnOk}
          loading={asking}
          emptyHint="Ask the assistant about this visit's coverage."
        />

        {turn?.overridden === true && (
          // UI-D16. Framed as the payer being authoritative, not the assistant
          // being wrong -- true, and it explains the hierarchy rather than
          // inviting distrust.
          <p className="rb-alert rb-alert--info" data-testid="override-note">
            The assistant&apos;s wording disagreed with the payer check, so the
            checked value is what you see above. The payer record is the
            authority here.
          </p>
        )}

        {turn?.stale === true && (
          <p className="rb-alert rb-alert--warn" data-testid="agent-stale-note">
            This answer used a last-known coverage value — the payer was
            unreachable when the assistant checked.
          </p>
        )}

        {turn && turn.overridden !== true && turn.stale !== true && (
          <p className="rb-muted" data-testid="tool-note">
            {turn.reply || turn.answer
              ? turn.request_id
                ? `Checked against the payer record · ${turn.request_id}`
                : "Checked against the payer record"
              : null}
          </p>
        )}
      </Card>
    </div>
  );
}
