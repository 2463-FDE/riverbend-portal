import { expect, test } from "@playwright/test";

/**
 * W3 journey — `RVB-W3-U4`.
 *
 * The demo beat is *"the payer is down and registration still completes"*, so the
 * assertion that matters is the one about staleness being impossible to miss —
 * not that a page rendered.
 *
 * Note what this journey does NOT do: it does not stop the payer container. That
 * would make the suite's outcome depend on container orchestration from inside a
 * browser test, and a journey that manipulates infrastructure fails for reasons
 * that have nothing to do with the UI. The payer's *unreachable* path is
 * exercised by the backend suite (`test_w3_*`) and by the component tests, which
 * can produce the state deterministically. What is verified here is the half only
 * a browser can verify: that whatever the payer says, a human can read it and act
 * on it.
 */

const STAFF = { username: "frontdesk", password: "portal123" };

async function signIn(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByLabel(/username/i).fill(STAFF.username);
  await page.getByLabel(/password/i).fill(STAFF.password);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/$/);
}

test("the front desk can check coverage and always gets an actionable answer", async ({
  page,
}) => {
  await signIn(page);
  await page.goto("/eligibility");

  await page.getByLabel(/insurance member id/i).fill("BCBS-40192");
  await page.getByRole("button", { name: /check coverage/i }).click();

  const chip = page.getByTestId("coverage-chip");
  await expect(chip).toBeVisible({ timeout: 20_000 });

  // Whatever the payer said, the chip must resolve to a state that is NOT
  // "pending" and NOT a bare unqualified value.
  await expect
    .poll(async () => chip.getAttribute("data-status"), { timeout: 20_000 })
    .not.toBe("pending");

  const status = await chip.getAttribute("data-status");
  const stale = await chip.getAttribute("data-stale");

  // `unknown` must never render as a denial -- that is the difference between
  // "we could not check" and "you have no coverage", and it decides whether a
  // patient gets turned away.
  if (status === "unknown") {
    await expect(chip).toContainText(/could not verify/i);
    await expect(chip).not.toContainText(/not active/i);
  }

  // A stale value may never render bare (UI-D15), and the page must say what to
  // do about it -- the chip states the fact, the guidance states the action.
  if (stale === "true") {
    await expect(page.getByTestId("coverage-stale")).toContainText(/unreachable since/i);
    await expect(page.getByTestId("stale-guidance")).toContainText(
      /mark coverage as unverified/i
    );
    await expect(page.getByTestId("stale-guidance")).toContainText(
      /do not turn the patient away/i
    );
  }
});

test("registration advances without any coverage check having happened", async ({ page }) => {
  // The W3 decoupling from the user's side. If intake ever starts gating on the
  // payer, the clinic stops registering patients whenever a third party has an
  // outage -- which is the failure the whole week exists to prevent.
  //
  // Scoped deliberately: this walks the FIRST step only. A full intake
  // submission is W1's journey, and duplicating it here would make this test
  // fail for reasons unrelated to eligibility.
  await signIn(page);
  await page.goto("/intake");

  await expect(page.getByRole("heading", { name: /new patient intake/i })).toBeVisible();

  const advance = page.getByRole("button", { name: /^continue$/i });
  // Disabled on required demographics -- NOT on coverage.
  await expect(advance).toBeDisabled();

  await page.getByLabel(/first name/i).fill("Dana");
  await page.getByLabel(/last name/i).fill("Whitfield");
  await page.getByLabel(/date of birth/i).fill("1984-03-11");

  // Enabled with no payer call made anywhere in this flow.
  await expect(advance).toBeEnabled();
  await advance.click();

  // And it actually moved on.
  await expect(page.getByRole("button", { name: /^back$/i })).toBeEnabled();

  // Nothing in the flow claims to be waiting on coverage.
  await expect(page.locator("body")).not.toContainText(
    /waiting for (the )?payer|coverage required|verify coverage to continue/i
  );
});

test("the eligibility assistant answers, and never hides that the payer won", async ({
  page,
}) => {
  await signIn(page);
  await page.goto("/eligibility");

  await page.getByLabel(/^Message$/i).fill("Is BCBS-40192 active for today's visit?");
  await page.getByRole("button", { name: /^ask$/i }).click();

  const panel = page.getByTestId("answer-panel");
  await expect(panel).toBeVisible({ timeout: 30_000 });

  const outcome = await panel.getAttribute("data-outcome");
  expect(outcome).not.toBeNull();

  // UI-D16. When the override fires it is visible, and framed as the payer being
  // authoritative rather than the assistant being wrong.
  if (outcome === "overridden") {
    const note = page.getByTestId("override-note");
    await expect(note).toContainText(/payer record is the authority/i);
    await expect(note).not.toContainText(/wrong|incorrect|hallucinat/i);
  }

  // UI-D17. Breaker state is never surfaced -- no reading of it changes what the
  // front desk does next.
  await expect(page.locator("body")).not.toContainText(/circuit breaker|half.?open/i);
});
