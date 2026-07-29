"use client";

import { useState } from "react";
import Link from "next/link";
import Card from "@/app/components/Card";
import { AnswerPanel } from "@/app/components/AnswerPanel";
import { UploadPanel } from "@/app/components/UploadPanel";
import { usePrincipal } from "@/app/lib/principal";
import { apiFetch } from "@/app/lib/session";
import type { AgentEnvelope } from "@/app/lib/outcome";

/**
 * Ask the assistant about clinic policy, and — if you hold the capability — add
 * to what it knows. `RVB-W2-U1`, `RVB-W2-U2`.
 *
 * Two scopes on one screen, and the distinction is load-bearing:
 *
 *   * **Clinic knowledge** is unscoped by design. Policy should be answerable to
 *     whoever asks, patients included.
 *   * **Records** are scoped by the gateway from the session. This page never
 *     sends a patient scope; it cannot. Until the fix recorded in
 *     `docs/findings/w2-knowledge-query-scope-idor.md`, a client-supplied scope
 *     on the knowledge endpoint reached the record collection and any patient
 *     could read any chart.
 */
export default function KnowledgePage() {
  const { status, principal } = usePrincipal();
  const [question, setQuestion] = useState("");
  const [scope, setScope] = useState<"knowledge" | "records">("knowledge");
  const [data, setData] = useState<AgentEnvelope | null>(null);
  const [ok, setOk] = useState(true);
  const [loading, setLoading] = useState(false);

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    if (!question.trim()) return;
    setLoading(true);
    setData(null);
    try {
      const endpoint =
        scope === "records" ? "/api/ai/records/query" : "/api/ai/knowledge/query";
      const res = await apiFetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: question }),
      });
      setOk(res.ok);
      setData(await res.json());
    } catch {
      setOk(false);
      setData({ detail: "unreachable" });
    } finally {
      setLoading(false);
    }
  }

  if (status === "loading") {
    return <p className="rb-muted">Loading…</p>;
  }

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Knowledge</h1>
        <p>Ask the assistant, and keep what it knows current.</p>
      </div>
      <Card title="Ask the assistant">
        <p className="rb-muted">
          Answers are grounded in clinic policy and, where you have access, the
          record. Every claim shows its source.
        </p>
        <form onSubmit={ask}>
          <fieldset className="rb-fieldset">
            <legend className="rb-label">What are you asking about?</legend>
            <label className="rb-radio">
              <input
                type="radio"
                name="scope"
                value="knowledge"
                checked={scope === "knowledge"}
                onChange={() => setScope("knowledge")}
              />
              Clinic policy
            </label>
            <label className="rb-radio">
              <input
                type="radio"
                name="scope"
                value="records"
                checked={scope === "records"}
                onChange={() => setScope("records")}
              />
              {principal?.kind === "patient" ? "My records" : "Patient records"}
            </label>
          </fieldset>

          {scope === "records" && principal?.kind === "staff" && (
            <p className="rb-alert rb-alert--info" data-testid="staff-scope-note">
              Record searches are scoped by the server. Open a patient from
              Records to search within their chart.
            </p>
          )}

          <label className="rb-label" htmlFor="kb-question">
            Question
          </label>
          <input
            id="kb-question"
            className="rb-input"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="How long should a patient fast before a blood draw?"
          />
          <button className="rb-btn rb-btn--primary" type="submit" disabled={loading}>
            {loading ? "Asking…" : "Ask"}
          </button>
        </form>

        <AnswerPanel
          data={data}
          ok={ok}
          loading={loading}
          emptyHint="Ask a question and the answer will appear here with its sources."
        />
      </Card>

      {/*
        RVB-AG-09/10: the capability hides the WRITE CONTROL, not the page, and
        its absence is replaced by a line naming who to ask. A feature that
        vanishes teaches people it does not exist, so they request it built
        rather than requesting access.
      */}
      {principal?.canIngest ? (
        <Card title="Knowledge base">
          <UploadPanel />
        </Card>
      ) : (
        <Card title="Knowledge base">
          <p className="rb-muted" data-testid="ingest-denied">
            Adding documents to the knowledge base is restricted. Ask a clinic
            administrator if you need access.
          </p>
        </Card>
      )}

      {principal?.kind === "staff" && (
        <Card title="Answer quality">
          <p className="rb-muted">
            How well retrieval is working, and what the underlying record data
            does to it.
          </p>
          <Link className="rb-btn" href="/knowledge/quality">
            Open the quality report
          </Link>
        </Card>
      )}
    </div>
  );
}
