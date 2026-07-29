import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

// Component-tier tests only. This config deliberately EXCLUDES e2e/ — Playwright
// owns that directory and runs on its own command against a live stack.
// See adr/0013: Vitest proves states, Playwright proves journeys, and neither is
// contorted into doing the other's job.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./test/setup.ts"],
    include: ["app/**/*.test.{ts,tsx}", "test/**/*.test.{ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**", ".next/**"],
    restoreMocks: true,
  },
  resolve: {
    // Mirrors tsconfig's "@/*" -> "./*" so imports read the same in tests as in
    // the app. A divergence here shows up as a module-not-found in CI only.
    alias: { "@": resolve(__dirname, ".") },
  },
});
