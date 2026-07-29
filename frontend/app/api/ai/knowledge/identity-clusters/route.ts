import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Staff-only upstream. Names every patient and states why their charts matched,
// including `identical_ssn`.
export async function GET(req: NextRequest) {
  return proxy(req, "/ai/knowledge/identity-clusters");
}
