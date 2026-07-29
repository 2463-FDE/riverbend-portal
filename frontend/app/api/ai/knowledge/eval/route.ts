import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Staff-only upstream (codex F2): the report embeds named patients and chart
// ids. A patient session gets a 403 from the gateway, which this relays.
export async function POST(req: NextRequest) {
  return proxy(req, "/ai/knowledge/eval", { method: "POST", body: {} });
}

export async function GET(req: NextRequest) {
  return proxy(req, "/ai/knowledge/eval/latest");
}
