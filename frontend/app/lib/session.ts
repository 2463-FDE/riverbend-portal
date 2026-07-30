"use client";

import type { PortalUser } from "./types";

// Client-side session helpers. Per RIV teaching notes, the only "auth" the
// portal does is stash the token in localStorage — there is no refresh, no
// expiry handling, and no real route-guard enforcement on the backend.

const TOKEN_KEY = "riverbend.token";
const USER_KEY = "riverbend.user";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function getUser(): PortalUser | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as PortalUser;
  } catch {
    return null;
  }
}

/**
 * Broadcast that the session changed.
 *
 * `AppShell` lives in the root layout, so it mounts ONCE — on `/login`, before a
 * token exists. Anything that resolved identity in a mount-only effect therefore
 * resolved it as "no session" and never revisited: after logging in, client-side
 * navigation re-renders the shell but does not remount it.
 *
 * That is not hypothetical. It shipped: the principal-aware navigation fell
 * through to the STAFF default and showed a patient the staff menu, live, while
 * every component test passed because tests mount fresh. Storage is not reactive,
 * so the write has to say so.
 */
export const SESSION_EVENT = "riverbend:session";

function announce(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(SESSION_EVENT));
}

export function setSession(token: string, user: PortalUser): void {
  window.localStorage.setItem(TOKEN_KEY, token);
  window.localStorage.setItem(USER_KEY, JSON.stringify(user));
  announce();
}

export function clearSession(): void {
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
  announce();
}

// fetch wrapper that attaches the bearer token to our own /api routes. The
// route handlers forward it to the gateway.
export async function apiFetch(
  input: string,
  init: RequestInit = {}
): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(input, { ...init, headers });
}
