// Bug sweep E, F-5: the sign-in page. A plain HTML form posting to
// /api/login (no client script needed); the password never touches a client
// bundle or a URL. Reachable without a session, and 503 like every route
// when authentication is not configured (src/proxy.ts).
export const dynamic = "force-dynamic";

export default async function LoginPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const sp = await searchParams;
  const failed = sp.error !== undefined;
  return (
    <main style={{ maxWidth: 360, margin: "15vh auto", padding: "0 16px" }}>
      <h1 style={{ fontSize: 20, fontWeight: 600, marginBottom: 4 }}>ZBM Revenue Recovery</h1>
      <p style={{ color: "#8a8f98", marginTop: 0, fontSize: 14 }}>Sign in to view recorded findings.</p>
      {failed ? (
        <p role="alert" data-login-error="true" style={{ color: "#f28b82", fontSize: 14 }}>
          That password was not accepted.
        </p>
      ) : null}
      <form method="post" action="/api/login" style={{ display: "grid", gap: 12 }}>
        <label style={{ display: "grid", gap: 6, fontSize: 14 }}>
          Password
          <input
            type="password"
            name="password"
            autoComplete="current-password"
            required
            maxLength={1024}
            autoFocus
            style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid #4a4f58", background: "#14171c", color: "#e8eaed" }}
          />
        </label>
        <button
          type="submit"
          style={{ padding: "8px 10px", borderRadius: 6, border: "none", background: "#3fa34d", color: "#0b0d10", fontWeight: 600 }}
        >
          Sign in
        </button>
      </form>
    </main>
  );
}
