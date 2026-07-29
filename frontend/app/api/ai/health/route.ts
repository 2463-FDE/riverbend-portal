import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// RVB-W1-U3. Carries the Bedrock data-retention posture so the panel can
// disable submit BEFORE a request is made. Without it the only way to learn the
// feature is switched off is to try it and read a refusal.
export async function GET(req: NextRequest) {
  return proxy(req, "/ai/health");
}
