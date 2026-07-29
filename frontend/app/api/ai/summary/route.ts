import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// W1 (2/2). The panel posts instruction text; the gateway is session-guarded and
// the orchestrator's contract accepts instruction text only -- there is no field
// in which a patient record could be expressed. See adr/0005.
export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  return proxy(req, "/ai/summary", { method: "POST", body });
}
