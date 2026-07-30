import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// The assembled view. The gateway resolves the scope and checks access before
// anything is proxied, so an unauthorized id never leaves that process.
export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  return proxy(req, `/ai/patient-view/${encodeURIComponent(id)}`);
}
