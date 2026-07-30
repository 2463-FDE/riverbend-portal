import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  return proxy(req, `/ai/knowledge/staged/${encodeURIComponent(id)}/discard`, {
    method: "POST",
    body: {},
  });
}
