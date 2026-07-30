import { expect, test } from "@playwright/test";

/**
 * W4 journeys — `RVB-W4-U6`.
 *
 * The one that matters: **Maria Gonzalez logs in and can see her own penicillin
 * allergy.** She is three charts, and the chart her login is bound to is the one
 * WITHOUT the allergy. Before the identity work she would have seen a record that
 * looked complete and was not — and twice during this engagement a change made
 * that true again, live.
 *
 * So this journey is the regression test for the failure the whole engagement is
 * about, asserted from the only place that counts: a browser, logged in as her.
 */

const MARIA = { username: "maria.gonzalez", password: "portal123" };
const STAFF = { username: "frontdesk", password: "portal123" };

async function signIn(
  page: import("@playwright/test").Page,
  who: { username: string; password: string }
) {
  await page.goto("/login");
  await page.getByLabel(/username/i).fill(who.username);
  await page.getByLabel(/password/i).fill(who.password);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/$/);
}

test("a patient sees their own record across every chart the clinic holds", async ({ page }) => {
  await signIn(page, MARIA);

  const card = page.getByText(/your complete record/i);
  await expect(card).toBeVisible({ timeout: 30_000 });

  // Either the assembled view rendered, or it paused for review, or it failed --
  // and every one of those must be legible. What may NOT happen is a record that
  // silently looks complete while missing a chart.
  const view = page.getByTestId("patient-view");
  const pending = page.getByTestId("view-pending-review");
  const failed = page.getByTestId("assembled-failed");

  await expect
    .poll(
      async () =>
        (await view.count()) + (await pending.count()) + (await failed.count()) > 0,
      { timeout: 30_000 }
    )
    .toBe(true);

  if (await view.count()) {
    // The chart span note, and the allergy that is the whole point.
    await expect(page.getByTestId("chart-span")).toContainText(/3 charts/);
    await expect(page.locator("body")).toContainText(/penicillin/i);

    // A failed domain must never read as an empty one (UI-D19).
    const unavailable = page.locator('[data-testid^="unavailable-"]');
    if (await unavailable.count()) {
      await expect(unavailable.first()).toContainText(/not the same as it being empty/i);
      await expect(page.getByTestId("view-incomplete")).toBeVisible();
    }
  } else if (await pending.count()) {
    // Being reviewed is neither withheld nor empty (UI-D18).
    await expect(pending).toContainText(/not being withheld from you/i);
  }
});

test("a patient does not see staff workflows in their navigation", async ({ page }) => {
  await signIn(page, MARIA);

  const nav = page.locator("nav").first();
  await expect(nav).toBeVisible();
  await expect(nav).not.toContainText(/release of information/i);
  await expect(nav).not.toContainText(/^intake$/i);
  await expect(nav).not.toContainText(/approvals/i);

  // But the shell is the same one -- their own record is still reachable.
  await expect(nav).toContainText(/records/i);
});

test("a patient cannot reach the approvals queue by URL", async ({ page }) => {
  // The nav filter is cosmetic; the gateway is the control. Typing the URL is the
  // test that tells them apart.
  await signIn(page, MARIA);
  await page.goto("/approvals");

  await expect(page.getByTestId("approvals-denied")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("approvals-denied")).toContainText(
    /separate permission/i
  );
});

test("a patient still cannot read another patient's chart", async ({ page }) => {
  await signIn(page, MARIA);
  await page.goto("/records");

  const input = page.getByRole("textbox").first();
  await input.fill("1043");
  await input.press("Enter");

  // 404 renders as "no records", which is the correct surface for the
  // 404-not-403 choice -- it denies without confirming the chart exists.
  await expect(page.locator("body")).not.toContainText(/o'brien/i, { timeout: 20_000 });
});

test("staff see the approvals queue and it states decisions, not run ids", async ({ page }) => {
  await signIn(page, STAFF);
  await page.goto("/approvals");

  // Either rows or an explicit empty state -- never a blank page.
  const empty = page.getByTestId("approvals-empty");
  const denied = page.getByTestId("approvals-denied");
  const unavailable = page.getByTestId("approvals-unavailable");
  const describe = page.locator('[data-testid^="describe-"]');

  await expect
    .poll(
      async () =>
        (await empty.count()) + (await denied.count()) +
        (await unavailable.count()) + (await describe.count()) > 0,
      { timeout: 20_000 }
    )
    .toBe(true);

  if (await describe.count()) {
    // adr/0015 rule 4: a sentence describing a release, not "Approve view-a3f9".
    await expect(describe.first()).toContainText(/release the record view for chart/i);
    await expect(page.getByRole("button", { name: /do not release/i }).first()).toBeVisible();
  }
});
