import { expect, test } from "@playwright/test";

/**
 * W2 journeys — `RVB-W2-U5`, `RVB-W2-U6`.
 *
 * Two things a person actually does: ask the assistant a question and read its
 * sources, and put a document into the knowledge base through the gate that
 * exists to make that decision deliberate.
 *
 * The second one is the point of the whole week. `adr/0014` argues the ingest
 * preview is a *control*, not a confirmation dialog — so this journey asserts
 * that the screen says the thing which makes someone read it, and that nothing
 * is indexed until they act.
 */

const STAFF = { username: "frontdesk", password: "portal123" };

async function signIn(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel(/username/i).fill(STAFF.username);
  await page.getByLabel(/password/i).fill(STAFF.password);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/$/);
}

/** A real, minimal PDF built in the browser — no committed binary fixture. */
const PDF_BUILDER = `(function (text) {
  const enc = (s) => Array.from(s, (c) => c.charCodeAt(0));
  const stream = "BT /F1 12 Tf 72 720 Td (" + text + ") Tj ET";
  const objs = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
    "<< /Length " + stream.length + " >>\\nstream\\n" + stream + "\\nendstream",
  ];
  let out = "%PDF-1.4\\n";
  const offsets = [];
  objs.forEach((body, i) => {
    offsets.push(out.length);
    out += (i + 1) + " 0 obj\\n" + body + "\\nendobj\\n";
  });
  const xref = out.length;
  out += "xref\\n0 " + (objs.length + 1) + "\\n0000000000 65535 f \\n";
  offsets.forEach((o) => { out += String(o).padStart(10, "0") + " 00000 n \\n"; });
  out += "trailer\\n<< /Size " + (objs.length + 1) + " /Root 1 0 R >>\\nstartxref\\n" + xref + "\\n%%EOF\\n";
  return new Uint8Array(enc(out));
})`;

test("a staff member asks the assistant and can see what the answer is based on", async ({
  page,
}) => {
  await signIn(page);
  await page.goto("/knowledge");

  await page.getByLabel(/^Question$/i).fill("how long must a patient fast before a blood draw?");
  await page.getByRole("button", { name: /^ask$/i }).click();

  const panel = page.getByTestId("answer-panel");
  await expect(panel).toBeVisible({ timeout: 20_000 });

  // Provenance is INLINE (ADR 0015 rule 2). If this needed a click to reveal,
  // the citation would not exist as far as a reader is concerned.
  await expect(page.getByTestId("citations")).toBeVisible();

  // The path stands in for a spinner (rule 3), and is labelled as steps run
  // rather than as reasoning (RVB-AG-08).
  await expect(page.getByTestId("agent-path")).toContainText(/steps the assistant ran/i);
});

test("an unanswerable question refuses instead of inventing, and says so", async ({ page }) => {
  await signIn(page);
  await page.goto("/knowledge");

  await page
    .getByLabel(/^Question$/i)
    .fill("what is the clinic's policy on interplanetary travel reimbursement?");
  await page.getByRole("button", { name: /^ask$/i }).click();

  const panel = page.getByTestId("answer-panel");
  await expect(panel).toBeVisible({ timeout: 20_000 });

  // Rule 1. Whatever the outcome, the reader is never left with blank space,
  // and a refusal is never dressed as an error.
  const outcome = await panel.getAttribute("data-outcome");
  expect(outcome).not.toBeNull();
  if (outcome === "refused") {
    await expect(panel).toContainText(/not an error/i);
    await expect(panel.locator(".rb-alert--err")).toHaveCount(0);
  }
});

test("adding a document goes through a preview that states the consequence", async ({ page }) => {
  await signIn(page);
  await page.goto("/knowledge");

  // The caps are visible before a file is chosen (RVB-ING-30).
  await expect(page.getByTestId("upload-limits")).toContainText(/10 MB/);

  const bytes = await page.evaluate(
    ([builder, text]) => Array.from((eval(builder) as (t: string) => Uint8Array)(text)),
    [PDF_BUILDER, "Curbside collection runs every Tuesday at the north entrance."] as const
  );

  await page.getByLabel(/^Title/i).fill("Curbside collection policy");
  await page.setInputFiles("#kb-file", {
    name: "curbside-policy.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from(bytes),
  });
  await page.getByRole("button", { name: /review before adding/i }).click();

  const confirm = page.getByTestId("upload-confirm");
  await expect(confirm).toBeVisible({ timeout: 20_000 });

  // The screen states the CONSEQUENCE, not "are you sure" (RVB-ING-25/26).
  await expect(page.getByTestId("consequence")).toContainText(/readable by every patient/i);
  await expect(page.getByText(/are you sure/i)).toHaveCount(0);

  // It shows the exact post-scrub text that will be indexed (RVB-ING-20).
  await expect(page.getByTestId("preview-text")).toContainText(/curbside collection runs/i);

  // And it is explicit that nothing has happened yet (RVB-ING-29).
  await expect(page.getByTestId("expiry")).toContainText(/nothing is added until/i);

  await page.getByTestId("commit-btn").click();
  await expect(page.getByTestId("upload-done")).toBeVisible({ timeout: 20_000 });

  // The document is now genuinely retrievable — the round trip, not just a 200.
  await page.getByLabel(/^Question$/i).fill("when does curbside collection run?");
  await page.getByRole("button", { name: /^ask$/i }).click();
  await expect(page.getByTestId("answer-panel")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("citations")).toContainText(/curbside collection policy/i);
});

test("the quality report separates retrieval from record integrity", async ({ page }) => {
  await signIn(page);
  await page.goto("/knowledge/quality");

  await page.getByRole("button", { name: /run the evaluation/i }).click();

  // Side by side, deliberately (UI-D12): apart, the integrity table reads as a
  // model failure rather than a data problem.
  await expect(page.getByTestId("retrieval-metrics")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("integrity-metrics")).toBeVisible();

  // Named patients and chart ids, not only a rate (RVB-W2-U4). "0.556" is
  // arguable; "Maria Gonzalez — 1042, 1330, 1588" is not.
  await expect(page.getByTestId("identity-splits")).toContainText("Maria Gonzalez");
  await expect(page.getByTestId("identity-splits")).toContainText("1330");
});
