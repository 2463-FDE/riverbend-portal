import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// next/navigation is unavailable outside the Next runtime, and AppShell calls
// usePathname/useRouter at module scope. Without this mock every component test
// that renders inside the shell fails on import rather than on behaviour.
export const routerMock = {
  push: vi.fn(),
  replace: vi.fn(),
  refresh: vi.fn(),
  back: vi.fn(),
  forward: vi.fn(),
  prefetch: vi.fn(),
};

let pathname = "/";
export function setPathname(next: string) {
  pathname = next;
}

vi.mock("next/navigation", () => ({
  useRouter: () => routerMock,
  usePathname: () => pathname,
  useSearchParams: () => new URLSearchParams(),
  redirect: vi.fn(),
}));

vi.mock("next/link", () => ({
  default: ({ children, href, ...rest }: any) => {
    const React = require("react");
    return React.createElement("a", { href, ...rest }, children);
  },
}));

beforeEach(() => {
  // Every test starts signed out. A test that needs a session says so, which
  // keeps auth state from leaking between cases — the failure mode there is a
  // test that passes only when run after another one.
  window.localStorage.clear();
  pathname = "/";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
