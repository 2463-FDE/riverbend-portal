"use client";

import { useState } from "react";
import { apiFetch } from "@/app/lib/session";

/**
 * The ingest gate — ADR 0014, `RVB-ING-25`–`RVB-ING-30`.
 *
 * Two phases, and phase one writes nothing. The screen between them is the
 * control, so it is built to be *read* rather than clicked through:
 *
 *   * It states the **consequence**, not the action. "Are you sure?" appears
 *     every time and therefore says nothing; "every patient can read this" is
 *     specific to what is about to happen.
 *   * It shows the **exact text that will be indexed**, post-scrub — not the
 *     file you uploaded.
 *   * It shows what the scrubber removed, as reassurance *and* warning: proof
 *     the scrub ran, and a signal that identifiers were in a document you are
 *     about to publish.
 *
 * The honest framing, which belongs on the screen and not only in the ADR: this
 * is a procedural control backed by a lenient automated one. It is not
 * de-identification. A human reading this preview is the control.
 */

interface Preview {
  staging_id: string;
  title: string;
  filename: string;
  kind: string;
  pages: number;
  chars: number;
  chunk_count: number;
  text: string;
  redactions: string[];
  metadata_redactions: { field: string; kinds: string[] }[];
  notes: string[];
  truncated: boolean;
  expires_in_seconds: number;
}

type Phase = "idle" | "uploading" | "preview" | "committing" | "done";

export function UploadPanel({ onCommitted }: { onCommitted?: () => void }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ title: string; chunks: number } | null>(null);
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);

  async function upload(chosen: File) {
    // Built explicitly rather than with `new FormData(formEl)`. Two reasons:
    // it is unambiguous about exactly what leaves the browser (a file and a
    // title, nothing else), and jsdom's FormData does not pick file inputs out
    // of a form element, so the form-derived version was untestable.
    const form = new FormData();
    form.append("file", chosen, chosen.name);
    form.append("title", title);

    setError(null);
    setPhase("uploading");
    try {
      const res = await apiFetch("/api/ai/knowledge/upload", {
        method: "POST",
        body: form,
      });
      const data = await res.json();
      if (!res.ok) {
        // The gateway's message is written for a human and names the next step
        // ("save as PDF", "split it"). Replacing it with a generic string is how
        // a helpful refusal becomes a dead end.
        setError(data?.detail ?? "That file could not be uploaded.");
        setPhase("idle");
        return;
      }
      setPreview(data as Preview);
      setPhase("preview");
    } catch {
      setError("The upload did not reach the server. Nothing was added.");
      setPhase("idle");
    }
  }

  async function commit() {
    if (!preview) return;
    setPhase("committing");
    setError(null);
    try {
      const res = await apiFetch(
        `/api/ai/knowledge/staged/${encodeURIComponent(preview.staging_id)}/commit`,
        { method: "POST" }
      );
      const data = await res.json();
      if (!res.ok) {
        setError(data?.detail ?? "That document could not be added.");
        setPhase("preview");
        return;
      }
      setResult({ title: data.title ?? preview.title, chunks: data.chunks ?? 0 });
      setPreview(null);
      setPhase("done");
      onCommitted?.();
    } catch {
      setError("The commit did not reach the server. Nothing was added.");
      setPhase("preview");
    }
  }

  async function discard() {
    if (!preview) return;
    await apiFetch(
      `/api/ai/knowledge/staged/${encodeURIComponent(preview.staging_id)}/discard`,
      { method: "POST" }
    ).catch(() => undefined);
    setPreview(null);
    setPhase("idle");
  }

  if (phase === "preview" || phase === "committing") {
    return (
      <ConfirmScreen
        preview={preview!}
        busy={phase === "committing"}
        error={error}
        onCommit={commit}
        onDiscard={discard}
      />
    );
  }

  return (
    <section className="rb-upload" data-testid="upload-panel">
      <h3 className="rb-h4">Add a document to the knowledge base</h3>

      {/* Stated BEFORE the file picker (RVB-ING-30). A limit you discover by
          hitting it is a bug report. */}
      <p className="rb-muted" data-testid="upload-limits">
        PDF, TXT or MD · up to 10 MB · up to 80 pages
      </p>

      {result && (
        <div className="rb-alert rb-alert--ok" role="status" data-testid="upload-done">
          <strong>Added to the knowledge base</strong>
          <p className="rb-alert__body">
            “{result.title}” is now searchable, in {result.chunks} section
            {result.chunks === 1 ? "" : "s"}.
          </p>
        </div>
      )}

      {error && (
        <div className="rb-alert rb-alert--err" role="alert" data-testid="upload-error">
          {error}
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!file) {
            setError("Choose a file to upload.");
            return;
          }
          void upload(file);
        }}
      >
        <label className="rb-label" htmlFor="kb-title">
          Title <span className="rb-muted">(optional — we use the filename otherwise)</span>
        </label>
        <input
          id="kb-title"
          name="title"
          className="rb-input"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Pre-visit fasting instructions"
        />

        <label className="rb-label" htmlFor="kb-file">
          Document
        </label>
        <input
          id="kb-file"
          name="file"
          type="file"
          className="rb-input"
          accept=".pdf,.txt,.md,.markdown"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />

        <button className="rb-btn rb-btn--primary" type="submit" disabled={phase === "uploading"}>
          {phase === "uploading" ? "Reading the document…" : "Review before adding"}
        </button>
      </form>
    </section>
  );
}

function ConfirmScreen({
  preview,
  busy,
  error,
  onCommit,
  onDiscard,
}: {
  preview: Preview;
  busy: boolean;
  error: string | null;
  onCommit: () => void;
  onDiscard: () => void;
}) {
  const blocked = preview.metadata_redactions.length > 0;

  return (
    <section className="rb-upload" data-testid="upload-confirm">
      <h3 className="rb-h4">Review before adding</h3>

      {/* The consequence, not the action (RVB-ING-25). */}
      <div className="rb-alert rb-alert--warn" role="status" data-testid="consequence">
        <strong>This will be readable by every patient</strong>
        <p className="rb-alert__body">
          Once added, this text can be returned — with a citation — to anyone who
          asks the assistant a related question, including patients. Read it
          before you commit.
        </p>
      </div>

      <dl className="rb-meta" data-testid="preview-meta">
        <dt>Title</dt>
        <dd>{preview.title}</dd>
        <dt>File</dt>
        <dd>{preview.filename || "pasted text"}</dd>
        <dt>Length</dt>
        <dd>
          {preview.chars.toLocaleString()} characters
          {preview.kind === "pdf" ? ` · ${preview.pages} pages` : ""} ·{" "}
          {preview.chunk_count} searchable section
          {preview.chunk_count === 1 ? "" : "s"}
        </dd>
      </dl>

      {/* Reassurance and warning at once (RVB-ING-27). */}
      {preview.redactions.length > 0 ? (
        <div className="rb-alert rb-alert--warn" data-testid="redactions">
          <strong>
            We removed {preview.redactions.length} kind
            {preview.redactions.length === 1 ? "" : "s"} of identifier
          </strong>
          <p className="rb-alert__body">
            {preview.redactions.join(", ")}. The scrubber found identifiers in
            this document, so read the text below carefully — it catches patterns,
            not names written in prose.
          </p>
        </div>
      ) : (
        <p className="rb-muted" data-testid="no-redactions">
          The scrubber found no identifier patterns. It does not detect patients
          named in ordinary prose, so this is not a guarantee.
        </p>
      )}

      {blocked && (
        <div className="rb-alert rb-alert--err" role="alert" data-testid="metadata-blocked">
          <strong>Rename this document before adding it</strong>
          <p className="rb-alert__body">
            Identifiers were found in the{" "}
            {preview.metadata_redactions.map((m) => m.field).join(", ")}. Titles
            and filenames are shown as citations, so they reach patients too.
          </p>
        </div>
      )}

      {preview.notes.length > 0 && (
        <ul className="rb-notes" data-testid="preview-notes">
          {preview.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}

      <h4 className="rb-h5">Exactly what will be indexed</h4>
      <pre className="rb-preview" data-testid="preview-text">
        {preview.text}
      </pre>

      {error && (
        <div className="rb-alert rb-alert--err" role="alert" data-testid="commit-error">
          {error}
        </div>
      )}

      <p className="rb-muted" data-testid="expiry">
        This preview expires in {Math.max(1, Math.round(preview.expires_in_seconds / 60))}{" "}
        minutes. Nothing is added until you press Add.
      </p>

      <div className="rb-actions">
        <button
          className="rb-btn rb-btn--primary"
          onClick={onCommit}
          disabled={busy || blocked}
          data-testid="commit-btn"
        >
          {busy ? "Adding…" : "Add to knowledge base"}
        </button>
        <button className="rb-btn" onClick={onDiscard} disabled={busy} data-testid="discard-btn">
          Discard
        </button>
      </div>
    </section>
  );
}
