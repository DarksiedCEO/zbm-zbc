import { clearedSessionCookie } from "@/lib/auth";
import { sameOrigin } from "@/lib/rate-limit";

// Bug sweep E, F-5: POST /api/logout clears the session cookie (sessions are
// stateless and signed; one ends at its expiry or when DASHBOARD_SESSION_SECRET
// or the password changes — rotate either to sign every session out). The
// Origin header is required and must match scheme and host (AEGIS L-1).
export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  if (!sameOrigin(req)) {
    // AEGIS L-1: a cross-site (or Origin-less) POST cannot sign the owner out
    return Response.json({ error: "logout needs a same-origin Origin header" }, { status: 403, headers: { "cache-control": "no-store" } });
  }
  return new Response(null, {
    status: 303,
    headers: { "cache-control": "no-store", location: "/login", "set-cookie": clearedSessionCookie() },
  });
}
