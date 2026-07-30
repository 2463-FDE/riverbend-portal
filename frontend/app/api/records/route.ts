import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

export async function GET(req: NextRequest) {
  const patientId = req.nextUrl.searchParams.get("patient_id") ?? "";
  // The id comes from the query string, and the gateway is the authority on
  // whether this session may see it: since #16 (adr/0011) it resolves an
  // AuthorizedScope before proxying, so an unauthorized id never reaches
  // records-service. An unauthorized id relays as 404, not 403 — no enumeration
  // oracle.
  //
  // This comment used to describe the IDOR as live. It stopped being true at #16
  // and was left behind, which is its own hazard: a comment claiming an
  // invariant nothing enforces is what let the F1 IDOR go unexamined.
  return proxy(req, `/patients/${encodeURIComponent(patientId)}/records`);
}
