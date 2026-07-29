import { defineConfig, devices } from "@playwright/test";

const BASE_URL = process.env.PORTAL_URL ?? "http://localhost:3070";

// Journeys only — one per week. See adr/0013.
//
// This suite is EXCLUDED from the default CI job because it needs the whole
// stack (`make up`). That gate is deliberately weaker than the backend's
// `--live` gate and the ADR says so: `--live` protects against spending money
// and needs a mechanism a CLI flag cannot defeat. This protects against wasting
// time, so a separate npm script is proportionate.
//
// What matters instead is the failure mode. globalSetup probes the stack and
// fails with "run `make up` first" rather than twenty timeouts that read like
// broken tests.
export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false, // the journeys share one seeded database
  retries: 0,           // a flaky journey is a bug report, not something to paper over
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
