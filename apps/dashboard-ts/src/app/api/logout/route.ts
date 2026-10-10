import { clearedSessionCookie } from "@/lib/auth";

// Bug sweep E, F-5: POST /api/logout clears the session cookie (sessions are
// stateless and signed; one ends at its expiry or when DASHBOARD_SESSION_SECRET
// or the password changes — rotate either to sign every session out).
export const dynamic = "force-dynamic";

export async function POST() {
  return new Response(null, {
    status: 303,
    headers: { "cache-control": "no-store", location: "/login", "set-cookie": clearedSessionCookie() },
  });
}
