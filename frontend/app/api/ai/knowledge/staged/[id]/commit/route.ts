import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

/**
 * Phase two. Sends NO body: the username comes from the session at the gateway
 * and the document is whatever was staged. There is nothing here for a client to
 * influence, which is the property that makes the provenance stamp honest.
 */
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  return proxy(req, `/ai/knowledge/staged/${encodeURIComponent(id)}/commit`, {
    method: "POST",
    body: {},
  });
}
