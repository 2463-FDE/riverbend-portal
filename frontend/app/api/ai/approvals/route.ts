import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Staff-only upstream, and it requires the approval capability. Lists other
// patients by construction, so a patient session gets 403 from the gateway.
export async function GET(req: NextRequest) {
  return proxy(req, "/ai/approvals");
}
