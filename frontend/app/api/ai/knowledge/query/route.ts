import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

/**
 * Clinic knowledge only. The body is forwarded as `{ query }` and nothing else.
 *
 * Not paranoia: until the fix in `docs/findings/w2-knowledge-query-scope-idor.md`,
 * a `patient_scope` field on this endpoint switched the orchestrator into the
 * PHI record collection, and any patient could read any chart. The gateway now
 * rejects that field with a 400 — this route simply never constructs one.
 */
export async function POST(req: NextRequest) {
  const body = await req.json();
  return proxy(req, "/ai/knowledge/query", {
    method: "POST",
    body: { query: body?.query ?? "" },
  });
}
