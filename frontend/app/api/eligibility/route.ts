import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// The direct coverage check, which is the authority the assistant is measured
// against. Never raises: `unknown` is a valid answer and is NOT `inactive`.
export async function GET(req: NextRequest) {
  const id = req.nextUrl.searchParams.get("insurance_id") ?? "";
  return proxy(req, `/eligibility?insurance_id=${encodeURIComponent(id)}`);
}
