import { NextResponse, type NextRequest } from "next/server";

import { loadRecordedFindings } from "@/lib/api";
import { encodeHandoff, HANDOFF_HEADER } from "@/lib/handoff";
import { httpStatusFor } from "@/lib/load-outcome";

// LOW-A (fix wave 1, Sep 24 2026): "/" used to render an orchestrator
// failure with HTTP 200. The status is decided here, before the page renders
// (see src/lib/handoff.ts for why the page itself cannot set it):
//
//   1. every request: drop any client-supplied handoff header;
//   2. GET/HEAD "/": read the recorded findings ONCE, hand the outcome to the
//      page, and answer with its status — 200, 502 or 503
//      (src/lib/load-outcome.ts). NextResponse.next({ status }) keeps the
//      page's rendered body and puts this status on the wire;
//      tests/status.live.test.mjs checks it against the built server;
//   3. any other method on "/": 405. The page is read-only and has no
//      actions; without an outcome it would fail closed with a 500.
//
// Runs on the Node.js runtime (the Next 16 proxy default).
export async function proxy(req: NextRequest) {
  const headers = new Headers(req.headers);
  headers.delete(HANDOFF_HEADER);

  if (req.nextUrl.pathname !== "/") {
    return NextResponse.next({ request: { headers } });
  }
  if (req.method !== "GET" && req.method !== "HEAD") {
    return new NextResponse(null, { status: 405, headers: { Allow: "GET, HEAD" } });
  }

  const outcome = await loadRecordedFindings();
  headers.set(HANDOFF_HEADER, encodeHandoff(outcome));
  return NextResponse.next({ status: httpStatusFor(outcome), request: { headers } });
}

// No matcher: the proxy must see every request so the handoff header can
// never reach the page unscrubbed. Only "/" does upstream work.
