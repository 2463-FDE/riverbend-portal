import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

/**
 * Decide a queued release by its opaque id.
 *
 * Only `approved` crosses the wire. The approver's identity is taken from the
 * session at the gateway — a client that could name its own approver would
 * defeat the "you cannot approve your own record" check that is the whole point
 * (UI-D18, codex F8).
 */
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const body = await req.json().catch(() => ({}));
  return proxy(req, `/ai/approvals/${encodeURIComponent(id)}`, {
    method: "POST",
    body: { approved: Boolean(body?.approved) },
  });
}
