import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { UploadPanel } from "./UploadPanel";

/**
 * The ingest gate, as the user meets it — `RVB-ING-25`–`RVB-ING-30`.
 *
 * This screen is the control. `adr/0014` argues that a confirmation dialog which
 * appears every time and says nothing specific is furniture, so what is tested
 * here is not "does a modal appear" but *does it say the thing that makes
 * someone read it*.
 */

const PREVIEW = {
  staging_id: "stg123",
  title: "Pre-visit fasting instructions",
  filename: "fasting.pdf",
  kind: "pdf",
  pages: 2,
  chars: 1420,
  chunk_count: 3,
  text: "Patients must not eat for eight hours. Call [REDACTED:phone] to reschedule.",
  redactions: ["phone"],
  metadata_redactions: [],
  notes: [],
  truncated: false,
  expires_in_seconds: 1800,
};

function mockFetch(handlers: Record<string, { ok: boolean; status: number; body: unknown }>) {
  return vi.fn(async (url: string) => {
    const match = Object.keys(handlers).find((k) => String(url).includes(k));
    const h = match ? handlers[match] : { ok: false, status: 404, body: {} };
    return {
      ok: h.ok,
      status: h.status,
      json: async () => h.body,
    } as Response;
  });
}

function pdf(name = "fasting.pdf") {
  return new File([new Uint8Array([0x25, 0x50, 0x44, 0x46])], name, {
    type: "application/pdf",
  });
}

beforeEach(() => {
  window.localStorage.setItem("riverbend.token", "test-token");
});

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

async function uploadA(file: File, { applyAccept = true } = {}) {
  const user = userEvent.setup({ applyAccept });
  await user.upload(screen.getByLabelText(/^Document$/i), file);
  await user.click(screen.getByRole("button", { name: /review before adding/i }));
  return user;
}

describe("before a file is chosen", () => {
  it("states the caps up front", () => {
    // RVB-ING-30. A limit you discover by hitting it is a bug report.
    render(<UploadPanel />);
    const limits = screen.getByTestId("upload-limits");
    expect(limits).toHaveTextContent(/PDF, TXT or MD/);
    expect(limits).toHaveTextContent(/10 MB/);
    expect(limits).toHaveTextContent(/80 pages/);
  });
});

describe("phase one — the preview", () => {
  it("shows the exact text that will be indexed, not the file", () => {
    // RVB-ING-20. The uploader must review the POST-SCRUB text, because that is
    // what patients will be able to retrieve.
    vi.stubGlobal("fetch", mockFetch({ "/upload": { ok: true, status: 200, body: PREVIEW } }));
    render(<UploadPanel />);

    return uploadA(pdf()).then(async () => {
      await waitFor(() => expect(screen.getByTestId("upload-confirm")).toBeInTheDocument());
      expect(screen.getByTestId("preview-text")).toHaveTextContent(
        "Patients must not eat for eight hours."
      );
      expect(screen.getByTestId("preview-text")).toHaveTextContent("[REDACTED:phone]");
    });
  });

  it("states the consequence rather than asking 'are you sure'", async () => {
    // RVB-ING-25 / RVB-ING-26. This is the whole design decision.
    vi.stubGlobal("fetch", mockFetch({ "/upload": { ok: true, status: 200, body: PREVIEW } }));
    render(<UploadPanel />);
    await uploadA(pdf());

    await waitFor(() => expect(screen.getByTestId("consequence")).toBeInTheDocument());
    expect(screen.getByTestId("consequence")).toHaveTextContent(/readable by every patient/i);
    expect(screen.queryByText(/are you sure/i)).toBeNull();
  });

  it("shows redactions as reassurance and warning at once", async () => {
    // RVB-ING-27. Non-zero redactions mean the scrubber found identifiers in a
    // document about to be published -- exactly when to read it again.
    vi.stubGlobal("fetch", mockFetch({ "/upload": { ok: true, status: 200, body: PREVIEW } }));
    render(<UploadPanel />);
    await uploadA(pdf());

    await waitFor(() => expect(screen.getByTestId("redactions")).toBeInTheDocument());
    expect(screen.getByTestId("redactions")).toHaveTextContent(/phone/);
    expect(screen.getByTestId("redactions")).toHaveTextContent(/not names written in prose/i);
  });

  it("does not claim safety when nothing was redacted", async () => {
    vi.stubGlobal("fetch", mockFetch({
      "/upload": { ok: true, status: 200, body: { ...PREVIEW, redactions: [] } },
    }));
    render(<UploadPanel />);
    await uploadA(pdf());

    await waitFor(() => expect(screen.getByTestId("no-redactions")).toBeInTheDocument());
    expect(screen.getByTestId("no-redactions")).toHaveTextContent(/not a guarantee/i);
  });

  it("says nothing is added until Add is pressed", async () => {
    vi.stubGlobal("fetch", mockFetch({ "/upload": { ok: true, status: 200, body: PREVIEW } }));
    render(<UploadPanel />);
    await uploadA(pdf());

    await waitFor(() => expect(screen.getByTestId("expiry")).toBeInTheDocument());
    expect(screen.getByTestId("expiry")).toHaveTextContent(/nothing is added until/i);
    expect(screen.getByTestId("expiry")).toHaveTextContent(/30 minutes/);
  });
});

describe("metadata blocks the commit", () => {
  it("disables Add and says to rename the file", async () => {
    // RVB-ING-33, codex F3. A clean body in `Maria Gonzalez appeal.pdf` is still
    // a disclosure, because the filename becomes a citation.
    vi.stubGlobal("fetch", mockFetch({
      "/upload": {
        ok: true, status: 200,
        body: { ...PREVIEW, metadata_redactions: [{ field: "filename", kinds: ["name"] }] },
      },
    }));
    render(<UploadPanel />);
    await uploadA(pdf("Maria Gonzalez appeal.pdf"));

    await waitFor(() => expect(screen.getByTestId("metadata-blocked")).toBeInTheDocument());
    expect(screen.getByTestId("metadata-blocked")).toHaveTextContent(/rename this document/i);
    expect(screen.getByTestId("commit-btn")).toBeDisabled();
  });
});

describe("rejections keep the server's message", () => {
  it("relays the DOCX message rather than a generic error", async () => {
    // RVB-ING-13. "Unsupported file type" sends someone away with no next step.
    vi.stubGlobal("fetch", mockFetch({
      "/upload": {
        ok: false, status: 422,
        body: { detail: "DOCX files are not supported yet. Save the document as a PDF and upload that." },
      },
    }));
    render(<UploadPanel />);
    // `applyAccept: false` on purpose. The input's `accept` attribute is a
    // browser HINT -- every file picker offers "All files" -- so the server's
    // rejection is the real control, and this test exists to prove its message
    // reaches the user intact rather than being replaced by a generic one.
    await uploadA(
      new File([new Uint8Array([0x50, 0x4b])], "policy.docx", {
        type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      }),
      { applyAccept: false }
    );

    await waitFor(() => expect(screen.getByTestId("upload-error")).toBeInTheDocument());
    expect(screen.getByTestId("upload-error")).toHaveTextContent(/save the document as a PDF/i);
  });

  it("says nothing was added when the upload never arrived", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new Error("network");
    }));
    render(<UploadPanel />);
    await uploadA(pdf());

    await waitFor(() => expect(screen.getByTestId("upload-error")).toBeInTheDocument());
    expect(screen.getByTestId("upload-error")).toHaveTextContent(/nothing was added/i);
  });
});

describe("phase two — commit", () => {
  it("commits and reports what became searchable", async () => {
    vi.stubGlobal("fetch", mockFetch({
      "/upload": { ok: true, status: 200, body: PREVIEW },
      "/commit": { ok: true, status: 200, body: { doc_id: "doc-stg123", chunks: 3, title: PREVIEW.title } },
    }));
    render(<UploadPanel />);
    const user = await uploadA(pdf());

    await waitFor(() => expect(screen.getByTestId("commit-btn")).toBeInTheDocument());
    await user.click(screen.getByTestId("commit-btn"));

    await waitFor(() => expect(screen.getByTestId("upload-done")).toBeInTheDocument());
    expect(screen.getByTestId("upload-done")).toHaveTextContent(/3 sections/);
  });

  it("keeps the preview open when the commit fails", async () => {
    // Dropping back to an empty form would lose the reviewed text and make the
    // user re-upload and re-read, which is how people stop reading.
    vi.stubGlobal("fetch", mockFetch({
      "/upload": { ok: true, status: 200, body: PREVIEW },
      "/commit": { ok: false, status: 404, body: { detail: "that preview has expired" } },
    }));
    render(<UploadPanel />);
    const user = await uploadA(pdf());

    await waitFor(() => expect(screen.getByTestId("commit-btn")).toBeInTheDocument());
    await user.click(screen.getByTestId("commit-btn"));

    await waitFor(() => expect(screen.getByTestId("commit-error")).toBeInTheDocument());
    expect(screen.getByTestId("upload-confirm")).toBeInTheDocument();
  });

  it("discard leaves without committing", async () => {
    // RVB-ING-29. Navigating away must not commit; discard is explicit.
    const fetchMock = mockFetch({
      "/upload": { ok: true, status: 200, body: PREVIEW },
      "/discard": { ok: true, status: 200, body: { discarded: "stg123" } },
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<UploadPanel />);
    const user = await uploadA(pdf());

    await waitFor(() => expect(screen.getByTestId("discard-btn")).toBeInTheDocument());
    await user.click(screen.getByTestId("discard-btn"));

    await waitFor(() => expect(screen.getByTestId("upload-panel")).toBeInTheDocument());
    const called = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(called.some((u) => u.includes("/commit"))).toBe(false);
  });
});
