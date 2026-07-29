import { expect, test } from "@playwright/test";

/**
 * W1 journey — the happy path, in a real browser, against the real stack.
 *
 * This is the test that answers codex finding R7 honestly. The four tests
 * written in response to it previously went through the *gateway* and were named
 * `test_e2e_*`; they proved the API worked and were allowed to stand in for
 * "the client can see it".
 *
 * Deliberately only the happy path. The withheld / refused / disabled states are
 * component tests (adr/0013) — the stub model is grounded by design, so reaching
 * them here would mean misconfiguring the grounding threshold, and the resulting
 * test would prove that a threshold can be misconfigured.
 */

const STAFF = { username: "frontdesk", password: "portal123" };

test("a staff member turns visit instructions into a patient-friendly summary", async ({
  page,
}) => {
  await page.goto("/login");
  await page.getByLabel(/username/i).fill(STAFF.username);
  await page.getByLabel(/password/i).fill(STAFF.password);
  await page.getByRole("button", { name: /sign in/i }).click();

  await expect(page).toHaveURL(/\/$/);

  await page.goto("/intake");
  await expect(page.getByRole("heading", { name: /visit instructions/i })).toBeVisible();

  const instructions = page.getByLabel(/instructions given to the patient/i);
  await expect(instructions).toBeVisible();
  await instructions.fill(
    "Please arrive fifteen minutes before your appointment. Bring your " +
      "insurance card and a photo ID. Do not eat or drink anything except " +
      "water for eight hours before your blood draw."
  );

  const summarise = page.getByTestId("summarise");
  await expect(summarise).toBeEnabled();
  await summarise.click();

  const result = page.getByTestId("summary-grounded");
  await expect(result).toBeVisible({ timeout: 20_000 });
  await expect(result).toContainText(/insurance card/i);
  await expect(result).toContainText(/checked against the instructions/i);

  // The guardrail's own output must never leak into the page, in any state.
  await expect(page.locator("body")).not.toContainText(/invented_medication/i);
});

test("the summariser is behind login", async ({ page }) => {
  // The AI feature adds no anonymous surface -- asserted at the browser, not
  // just at the gateway.
  await page.goto("/intake");
  await expect(page).toHaveURL(/\/login/);
});
