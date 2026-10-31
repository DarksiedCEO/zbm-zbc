# ADR 0028 — The client acceptance page (2026-10-31)

Status: **proposed, not merged** (local branch `feat/client-acceptance-2026-10-31`, built on the local
`feat/compliance-gate-deploy` at 98aaf6f6 with PR #7 `feat/legal-founder-contract-path` 959a687 merged in). Not
AEGIS-audited yet. Amends ADR 0010 (Legal: who records a client's acceptance, a new caller, two new resources) and
ADR 0022 (decision 6, "who may record an acceptance", for client documents).

## Context

The production audit of Oct 30 2026 found that a client could not accept a contract (red team OPS-C1, Critical,
blocks the first paid client): no acceptance page and no e-signature provider existed, and the only way to record an
acceptance was Andre posting `clickwrap_unticked_box` with his onboarding token, for a page that never existed. The
money red team's RTM-13 and AEGIS r10/r11 (R10-L3 and r11's residual) found the same root from the other side: whoever
held the onboarding token, with any open invitation, could record a client's acceptance with an arbitrary signer;
there was no signer binding, no IP or user-agent evidence, and invitation ids were not secret.

The founder-approved clickwrap text v1 describes the page: a unique emailed link (Part A), the full text of each
document with its version and SHA-256 fingerprint and a download (Part B; Part C for a new SOW), one unticked box per
document, typed full name and title, the authority and electronic-records statements, **Accept and sign**, a
confirmation email with a copy of each document (Part D), and what must be recorded (Part E). Under ADR 0022 Andre
approves the client contracts himself and his CQ-19 determination makes such a clickwrap evidence-sufficient.

## Decision

1. **A separate, minimal public service, `client-portal`** (`services/client-portal-py`), the only new public surface.
   Stateless (no volume, no ledger), one replica, Railway domain only. It holds three secrets: Legal's service token
   (a Railway reference), its own `client_portal` caller token at Legal, and its evidence key. It reaches Legal only,
   over Railway's private network. Alternatives considered:
   - *A route on the orchestrator or the Stripe gateway* (both public already). Rejected: either would gain HTML,
     form handling and a Legal credential; a defect in the page would sit inside the money path (the gateway) or the
     dashboard's API (the orchestrator), and vice versa. A separate service keeps each blast radius to its purpose.
   - *A route on the Vercel sites.* Rejected: Vercel cannot reach Railway's private network, so Legal would need a
     public route; the token check would live in another repository and runtime.
   - *Legal itself public.* Rejected: Legal is the system of record for every contract and acceptance.
   The page is plain ASGI on uvicorn (no web framework), server-rendered HTML, no script, no cookie, no third-party
   resource; its only dependencies are uvicorn and httpx, both already in the shared hash lock.
2. **The link is Legal's.** `POST /legal/v1/client-acceptance-invitations` (Andre only) binds ONE client
   (`client:*`) to exact document versions by hash: the MSA, one SOW, or both. Every document must be one this client
   can validly accept now (approved and in force, this hash, bound to this client or the current standard form, its
   template in force, a CQ-19 evidence basis in force); otherwise no link is issued. The token is 32 random bytes
   (base64url, 43 characters), in that answer only; Legal stores its SHA-256 (a hash of a 256-bit random value needs
   no salt or key), never the token — not in the record, the ledger, the audit export or the idempotency record (a
   replay of the request answers `token: null`). 1-14 days, default 14. Andre revokes it before use
   (`POST .../{id}/revoke`), reads its status (`GET .../{id}`) and, once used, its evidence (`GET .../{id}/evidence`).
3. **The page's two Legal routes, and nothing else, for the `client_portal` caller.** Legal narrows that caller to
   the routes that name it: the generic reader routes every other caller may use refuse it. A stolen portal token
   reaches only:
   - `POST /legal/v1/portal/invitations/resolve` `{"token"}`: the documents' exact registered text, version and hash,
     the display block (client legal name, notice email, SOW number and title; a blob whose hash is in the record) and
     the client's latest counted MSA acceptance. **Every unusable token is the same `404 {"detail": "not found"}`**:
     unknown, malformed, expired, revoked, used, a document no longer valid to accept. Legal compares the token's
     hash with every stored hash in constant time with no early exit.
   - `POST /legal/v1/portal/acceptances`: the token, each document with `presented_sha256` and its own
     `affirmative_act`, `presentation: inline`, the page text as shown and its SHA-256, the typed signatory (full
     name, title, company), and HMAC-SHA256 values of the client's IP address and user agent with the key's id. One
     operation under the service lock records one acceptance per document (`entry_by: client_portal`,
     `invitation_id`, `portal_evidence`) and marks the link used: exactly once; a retry of the same submission is
     answered from the idempotency record; a refused submission (a tampered hash, a missing tick, another document
     set, a page hash that does not match its text) records nothing and does not use the link up. The signatory and
     the page text are blobs; records, ledger events and the export carry only their hashes.
4. **No other way to record a client's acceptance.** `POST /legal/v1/acceptances` refuses every `client:*` party for
   every caller (`ACCEPTANCE_NOT_INVITED`), the onboarding token included; this closes RTM-13's and r11's residual.
   And an acceptance of a `client:*` party not recorded by the page is never evidence-sufficient (an older record
   stays in the log and never counts). The RTM-13 single-document invitations remain for non-client parties only.
   Because Legal issues no link while evidence would be insufficient, an insufficient client acceptance can no longer
   be produced at all.
5. **Finance is unchanged.** FIN-29 reads Legal's acceptance view, which for a page acceptance says
   `evidence_sufficient: true`, `basis_in_force: true` and `via: client_portal`. The end-to-end contract
   (`services/client-portal-py/tests/contract_e2e_real.py`) runs the real legal-py, the page and the real finance-py:
   invite -> accept -> billing readiness allowed -> the invoice is ISSUED; and the used link, another client's
   invoice, a tampered hash, a link naming another client's fill, a template retired after the invitation and an
   expired link are all refused.
6. **ZBM's entity facts come from configuration.** A template's `{{zbm_*}}` variables are filled from Legal's
   `LEGAL_ZBM_FACTS` at fill time, never from a request (422) and never hard-coded; a missing fact refuses the fill
   (`ZBM_FACT_MISSING`). The MSA therefore is a template too and is filled per client; each fill is approved by Andre
   on its own hash, so the text a client accepts is exactly the text he approved, and the page shows exactly that
   text (`presented_sha256 == doc_sha256`, clickwrap Part E).
7. **The page's defenses.**
   - The token's format is checked first (a malformed token never reaches Legal); the same 404 page for every
     unusable link; pages the service does not serve (an MSA-only link; a SOW-only link for a client with no counted
     MSA acceptance) are that same 404.
   - Rate limits before any Legal call: per client address (30/min), per link (40/h), and a global ceiling on
     not-found answers (120/min); keys are HMACs, never an address or a token. In memory, one replica: a restart
     resets them.
   - Request limits: target 2 KiB, head 8 KiB, form body 8 KiB (`application/x-www-form-urlencoded` only), 10 s body
     deadline; uvicorn: 64 connections, 5 s keep-alive, 16 KiB head, no access log, no `Server` header.
   - CSRF: the form carries an HMAC over the token's hash, the page text's hash and the issue time (at most 4 hours
     old, not from the future); the POST's `Origin` must equal `PORTAL_PUBLIC_ORIGIN`; a `Sec-Fetch-Site` other than
     `same-origin` is refused. No cookies, no session.
   - Headers on every answer: CSP `default-src 'none'` with the one stylesheet by hash, `form-action 'self'`,
     `frame-ancestors 'none'`, `base-uri 'none'`; `no-store`; `Referrer-Policy: same-origin` (the token in the path
     never leaves in a Referer; with `no-referrer` browsers would send `Origin: null` on the POST); nosniff; HSTS;
     COOP/CORP; noindex.
   - The page text is re-rendered from Legal at submission; a different hash than the signed one (a document changed
     between opening and submitting) records nothing and asks the client to reload.
   - The logs carry the route name, the status and the time taken; never a token, a name, an email, an address or a
     user agent.
   - Accessibility: labels on every field and box, an alert error summary linking to each field, `aria-invalid` and
     `aria-describedby` per field, keyboard only, visible focus, light and dark schemes at WCAG AA contrast.
8. **The words.** The page uses the clickwrap text v1 Parts B and C character for character with the values filled
   (tests pin every sentence). Three changes of behaviour, none of wording, all to avoid any script:
   - each document's full text is shown inline (not in a scroll panel), directly above its own box, so the client
     passes the whole text before reaching the box; the record says `presentation: inline`;
   - the server, not a disabled button, refuses a submission with a box unticked or a field empty, and each box comes
     back unticked;
   - "Not now" opens a page that says nothing was recorded (a page cannot close itself without script).
   One addition the build brief asked for: a third field, "Company you are signing for", which must match the
   client's legal name on the documents (case, spaces and punctuation ignored).
9. **What is recorded** (clickwrap Part E): the client (party ref; legal name in the display blob), the link's id
   (not the token), each document's id, version and hash, the text shown (its hash equals the document's), the
   signatory exactly as typed, each document's own tick, the presentation, the server time, the page text as shown
   with every value filled and its SHA-256, and the IP address and browser **as HMACs** keyed by the page's evidence
   key (`hmac_key_id` names the key). With the key, anyone holding a claimed IP address or user agent can check it
   against the record (runbook step 8). Andre should know that the page's text says "We will record ... your IP
   address and browser": they are recorded in this keyed form, not in clear.
10. **Mail.** No mail provider is wired to the page. `ConfirmationMailer` is the port (Part D builder included); the
    stand-in sends nothing, the confirmation page then says Z Best Media will email the copy, and Andre sends Part D by
    hand (runbook step 6). Finance's client-mail adapter (ADR 0026, another branch) is the candidate; a real adapter
    uses the invitation id as the provider's idempotency key.

## Consequences and residual risk

- **Identity.** The signer is whoever holds the emailed link and types a name; the page does not verify identity
  beyond possession of the link sent to the client's recorded contact (the clickwrap text's own model). A forwarded
  link can be used by the person it was forwarded to; Part A asks the recipient not to forward it.
- **UNVERIFIED: Railway's `X-Forwarded-For`.** With `PORTAL_TRUSTED_PROXY_HOPS=1` the client address is the last
  entry. If Railway's edge sets none, every request keys on the proxy (one shared per-address limit; the recorded IP
  HMAC is the proxy's). Runbook step 8 checks it once.
- **UNVERIFIED on Railway:** the public domain's TLS and HSTS behaviour, and any edge protection against volumetric
  attacks; the page's own limits protect Legal, not the edge. Nothing here was deployed or run against Railway.
- **Idempotency window.** Legal answers a retry from its idempotency record for 15 minutes; a later identical retry
  of a used link gets the 404 page (the acceptance stands; nothing is recorded twice).
- **Part C** names the client's latest counted MSA acceptance; it does not read which MSA version the SOW's text
  references (`{{msa_version}}`). Andre fills `msa_version` with the version the client accepted.
- **One replica, in-memory limits;** a redeploy resets them.
- **Mutation evidence.** The page's tests were written with the code; 13 targeted mutants of its defenses (CSRF,
  form signature, uniform 404, rate limit, escaping, IP HMAC, token format, Referrer-Policy, page-hash recheck,
  company match, forwarded address, text-hash check, logging) were all killed. Legal's tests were written first.
