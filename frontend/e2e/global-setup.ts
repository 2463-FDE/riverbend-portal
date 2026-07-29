const PORTAL = process.env.PORTAL_URL ?? "http://localhost:3070";
const GATEWAY = process.env.GATEWAY_PUBLIC_URL ?? "http://localhost:8070";

/**
 * Fail fast, and say what to do about it.
 *
 * Without this, running the journeys without the stack up produces a wall of
 * 30-second timeouts that read like broken tests. The gate on this suite is
 * weak by design (adr/0013) — so the failure mode is where the care goes.
 */
async function probe(name: string, url: string): Promise<string | null> {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(4000) });
    return res.ok ? null : `${name} answered ${res.status} at ${url}`;
  } catch (e) {
    return `${name} is unreachable at ${url} (${(e as Error).name})`;
  }
}

export default async function globalSetup() {
  const problems = (
    await Promise.all([
      probe("The portal", PORTAL),
      probe("The gateway", `${GATEWAY}/healthz`),
    ])
  ).filter(Boolean) as string[];

  if (problems.length) {
    throw new Error(
      [
        "",
        "The end-to-end journeys need the full stack running.",
        "",
        ...problems.map((p) => `  · ${p}`),
        "",
        "  Start it with:   make up && make seed",
        "  Then re-run:     npm run test:e2e",
        "",
        "These journeys are excluded from the default CI job on purpose —",
        "they need Postgres, Redis, Chroma and every service. See adr/0013.",
        "",
      ].join("\n")
    );
  }
}
