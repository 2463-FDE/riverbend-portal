import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// W3 front-desk assistant. `visit_id` scopes the conversation thread; the agent
// itself owns the tool call, so nothing here decides coverage.
export async function POST(req: NextRequest) {
  const body = await req.json();
  return proxy(req, "/ai/agent/eligibility", {
    method: "POST",
    body: { visit_id: body?.visit_id ?? "", message: body?.message ?? "" },
  });
}
