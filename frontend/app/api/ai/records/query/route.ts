import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

/**
 * Chart-shaped questions. The gateway derives the scope from the session and
 * overwrites anything sent, so `patient_id` here is a staff routing hint, not an
 * authorization claim — staff are `open_to_context` and must name a patient.
 */
export async function POST(req: NextRequest) {
  const body = await req.json();
  return proxy(req, "/ai/records/query", {
    method: "POST",
    body: {
      query: body?.query ?? "",
      ...(typeof body?.patient_id === "number" ? { patient_id: body.patient_id } : {}),
    },
  });
}
