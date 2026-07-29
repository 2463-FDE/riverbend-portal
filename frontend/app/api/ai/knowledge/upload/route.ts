import { NextRequest, NextResponse } from "next/server";
import { gatewayUrl } from "@/app/lib/gateway";

/**
 * Phase one of the ingest gate — the one route that cannot use `proxy()`.
 *
 * `proxy()` JSON-encodes its body. A multipart upload has to reach the gateway
 * as multipart, so the form is re-assembled here rather than parsed into
 * something else and rebuilt downstream.
 *
 * Two things this route deliberately does NOT do:
 *
 *   * It does not read the file into a string. The bytes pass through as a Blob.
 *   * It does not forward `staged_by`, even if the browser sends one. Provenance
 *     is stamped from the session at the gateway, and a client that could choose
 *     its own would make the audit trail decorative.
 *
 * The size cap is enforced at the gateway with a streaming read (`RVB-ING-35`).
 * The check here is a courtesy that fails fast in the browser's own round trip;
 * it is not the control, and it is not trusted as one.
 */
const MAX_BYTES = 10 * 1024 * 1024;

export async function POST(req: NextRequest) {
  let form: FormData;
  try {
    form = await req.formData();
  } catch {
    return NextResponse.json(
      { detail: "That upload could not be read. Try selecting the file again." },
      { status: 400 }
    );
  }

  const file = form.get("file");
  if (!(file instanceof File) || file.size === 0) {
    return NextResponse.json({ detail: "Choose a file to upload." }, { status: 400 });
  }
  if (file.size > MAX_BYTES) {
    return NextResponse.json(
      { detail: `That file is larger than the ${MAX_BYTES / (1024 * 1024)} MB limit.` },
      { status: 413 }
    );
  }

  const outbound = new FormData();
  outbound.append("file", file, file.name);
  outbound.append("title", String(form.get("title") ?? ""));

  const auth = req.headers.get("authorization");
  try {
    const res = await fetch(`${gatewayUrl()}/ai/knowledge/upload`, {
      method: "POST",
      // No Content-Type header: fetch sets it, with the multipart boundary.
      // Setting it by hand here produces a body the server cannot parse.
      headers: auth ? { Authorization: auth } : {},
      body: outbound,
      cache: "no-store",
    });
    const text = await res.text();
    return NextResponse.json(text ? safeParse(text) : null, { status: res.status });
  } catch (e) {
    return NextResponse.json(
      { detail: e instanceof Error ? e.message : "gateway unreachable" },
      { status: 502 }
    );
  }
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text.slice(0, 500) };
  }
}
